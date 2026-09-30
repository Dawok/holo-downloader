import base64
import importlib.util
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timedelta
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from channel_config import channels_from_config, read_config


CHANNEL_ID = 'UC' + 'a' * 22
ROOT = Path(__file__).resolve().parents[1]
THUMBNAIL_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII=')


class WebUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.runtime.cleanup)
        initial_config = Path(cls.runtime.name) / 'initial.toml'
        initial_config.write_text('[cron_schedule]\n')
        modules = {name: ModuleType(name) for name in ('common', 'downloadVid', 'getMembers', 'communityPosts', 'unarchived', 'getVids', 'livestream_dl', 'livestream_dl.YoutubeURL')}
        modules['common'].logger = logging.getLogger('web-ui-tests')
        modules['common'].setup_umask = Mock()
        modules['livestream_dl.YoutubeURL'].quality_aliases = {}
        spec = importlib.util.spec_from_file_location('web_ui_under_test', ROOT / 'web.py')
        cls.web = importlib.util.module_from_spec(spec)
        modules['web_ui_under_test'] = cls.web
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {
            'CONFIG_FILE': str(initial_config),
            'HISTORY_DB': str(Path(cls.runtime.name) / 'initial.db'),
        }):
            spec.loader.exec_module(cls.web)
        cls.web.app.config.update(TESTING=True)
        cls.addClassCleanup(cls.web.scheduler.shutdown)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'config.toml'
        self.path.write_text('# Keep this comment\n[download_options]\nquality = "best"\n[cron_schedule]\n')
        for attribute, value in [('config_file_path', str(self.path)), ('DB_FILE', str(Path(directory.name) / 'history.db'))]:
            patcher = patch.object(self.web, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.web.init_db()
        self.web.cache.clear()
        self.web.scheduler.remove_all_jobs()
        self.web.active_downloads.clear()
        self.web.active_unarchived_downloads.clear()
        self.client = self.web.app.test_client()
        self.client.get('/')
        with self.client.session_transaction() as session:
            self.token = session['csrf_token']

    def post(self, path, **data):
        return self.client.post(path, data={'csrf_token': self.token, **data})

    def channel_data(self, **updates):
        return dict(id=CHANNEL_ID, name='Example channel', public='on', unarchived='on',
                    members='on', community='on', title_regex='(?i)karaoke',
                    description_regex='', output_template='',
                    revision=read_config(self.path)[1], **updates)

    def add_channel(self):
        response = self.post('/channels/new', **self.channel_data())
        self.assertEqual(response.status_code, 302)

    def test_pages_fragments_and_json_render(self):
        for route in ('/', '/activity', '/channels', '/channels/new', '/config', '/schedules',
                      '/data/active', '/data/unarchived', '/data/history', '/data/recent', '/data/scheduler'):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 200)
        for route in ('/api/active', '/api/unarchived', '/api/history', '/api/scheduler'):
            with self.subTest(route=route):
                self.assertIsInstance(self.client.get(route).json, list)

    def test_posts_require_session_token(self):
        before = self.path.read_bytes()
        for route in ('/channels/new', '/config', '/schedules', '/actions/add', '/actions/check',
                      '/actions/cancel/abcdefghijk', '/actions/toggle_theme'):
            with self.subTest(route=route):
                self.assertEqual(self.client.post(route).status_code, 400)
        self.assertEqual(self.client.post('/api/channels/resolve', json={'source': '@example'}).status_code, 400)
        self.assertEqual(self.path.read_bytes(), before)

    def test_channel_create_edit_and_delete_grouped_settings(self):
        self.add_channel()
        doc, revision = read_config(self.path)
        self.assertEqual(len(channels_from_config(doc)), 1)
        self.assertEqual(doc['title_filter'][CHANNEL_ID], '(?i)karaoke')
        response = self.post(f'/channels/{CHANNEL_ID}/edit', id='UC' + 'b' * 22,
                             name='Renamed channel', public='on', title_regex='', revision=revision)
        self.assertEqual(response.status_code, 302)
        doc, revision = read_config(self.path)
        channel = channels_from_config(doc)[0]
        self.assertEqual(channel['id'], CHANNEL_ID)
        self.assertEqual(channel['name'], 'Renamed channel')
        self.assertFalse(channel['unarchived'])
        self.assertFalse(channel['members'])
        self.assertFalse(channel['community'])
        self.web.save_to_history('abcdefghijk', {'status': 'Finished'})
        response = self.post(f'/channels/{CHANNEL_ID}/delete', revision=revision)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(channels_from_config(read_config(self.path)[0]), [])
        self.assertEqual(len(self.web.get_history()), 1)
        self.assertIn('# Keep this comment', self.path.read_text())

    def test_bad_regex_preserves_unsaved_form_and_saved_file(self):
        data = self.channel_data()
        data['title_regex'] = '[invalid'
        before = self.path.read_bytes()
        response = self.post('/channels/new', **data)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'[invalid', response.data)
        self.assertIn(b'Example channel', response.data)
        self.assertEqual(self.path.read_bytes(), before)

    def test_stale_edit_is_rejected(self):
        self.add_channel()
        stale = read_config(self.path)[1]
        self.post(f'/channels/{CHANNEL_ID}/edit', name='Current name', public='on', revision=stale)
        before = self.path.read_bytes()
        response = self.post(f'/channels/{CHANNEL_ID}/edit', name='Stale name', public='on', revision=stale)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Configuration changed', response.data)
        self.assertEqual(self.path.read_bytes(), before)

    def test_raw_toml_validation_and_revision(self):
        before = self.path.read_bytes()
        response = self.post('/config', toml_content='[broken', revision=read_config(self.path)[1])
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'[broken', response.data)
        self.assertEqual(self.path.read_bytes(), before)
        revision = read_config(self.path)[1]
        with patch.object(self.web, 'update_scheduler', return_value=(True, 'Updated')):
            self.assertEqual(self.post('/config', toml_content=before.decode() + '\nextra = true\n', revision=revision).status_code, 302)
        self.assertEqual(self.post('/config', toml_content=before.decode(), revision=revision).status_code, 400)

    def test_schedules_validate_all_fields_before_saving(self):
        before = self.path.read_bytes()
        response = self.post('/schedules', streams='*/30 * * * *', members_only='invalid', revision=read_config(self.path)[1])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.path.read_bytes(), before)
        with patch.object(self.web, 'update_scheduler') as update:
            response = self.post('/schedules', streams='*/30 * * * *', revision=read_config(self.path)[1])
            self.assertEqual(response.status_code, 302)
            update.assert_called_once()
            self.assertEqual(read_config(self.path)[0]['cron_schedule']['streams'], '*/30 * * * *')
            response = self.post('/schedules', revision=read_config(self.path)[1])
            self.assertEqual(response.status_code, 302)
            self.assertEqual(dict(read_config(self.path)[0]['cron_schedule']), {})

    def test_channel_lookup_and_duplicate_link(self):
        headers = {'X-CSRF-Token': self.token}
        with patch('yt_dlp.YoutubeDL') as factory:
            extractor = factory.return_value.__enter__.return_value
            extractor.extract_info.return_value = {'channel_id': CHANNEL_ID, 'channel': 'Example channel'}
            response = self.client.post('/api/channels/resolve', json={'source': '@example'}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['id'], CHANNEL_ID)
            self.assertIsNone(response.json['edit_url'])
            extractor.extract_info.assert_called_with('https://www.youtube.com/@example', download=False)
            self.add_channel()
            response = self.client.post('/api/channels/resolve', json={'source': '@example'}, headers=headers)
            self.assertEqual(response.json['edit_url'], f'/channels/{CHANNEL_ID}/edit')
            extractor.extract_info.side_effect = RuntimeError('Unavailable')
            self.assertEqual(self.client.post('/api/channels/resolve', json={'source': '@example'}, headers=headers).status_code, 502)
        with patch('yt_dlp.YoutubeDL') as factory:
            self.assertEqual(self.client.post('/api/channels/resolve', json={'source': 'https://example.com/'}, headers=headers).status_code, 400)
            factory.assert_not_called()

    def test_existing_history_is_migrated_without_losing_rows(self):
        legacy_db = Path(self.web.DB_FILE).with_name('legacy.db')
        with closing(sqlite3.connect(legacy_db)) as connection, connection:
            connection.execute('CREATE TABLE history (id INTEGER PRIMARY KEY, video_id TEXT, type TEXT, status TEXT, total_size INTEGER, timestamp TEXT)')
            connection.execute("INSERT INTO history VALUES (1, 'abcdefghijk', 'stream', 'Finished', 1024, '2026-01-01 12:00:00')")
        with patch.object(self.web, 'DB_FILE', str(legacy_db)):
            self.web.init_db()
            self.assertEqual(self.web.get_history()[0]['video_id'], 'abcdefghijk')
            self.assertFalse(self.web.get_history()[0]['has_thumbnail'])
            self.web.save_to_history('bcdefghijkl', {'status': 'Finished'}, info={'title': 'Recorded title', 'channel': 'Example channel'})
            self.assertEqual(self.web.get_history()[0]['title'], 'Recorded title')
            self.assertEqual(self.client.get('/data/history').status_code, 200)

    def test_recording_rows_escape_titles_and_stop_sets_event(self):
        downloader = SimpleNamespace(info_dict={'title': '<script>alert(1)</script>', 'channel': 'Example channel'}, embed_info={},
                                     livestream_downloader=SimpleNamespace(stats={'status': 'Recording', 'video': {'current_filesize': 1024}}),
                                     kill_this=threading.Event())
        started = datetime.now() - timedelta(hours=2)
        self.web.active_downloads['abcdefghijk'] = {'downloader': downloader, 'type': 'stream', 'start_time': started, 'recording_start_time': started}
        response = self.client.get('/data/active')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'&lt;script&gt;', response.data)
        self.assertNotIn(b'<script>alert(1)', response.data)
        self.assertIn(b'02:00:', response.data)
        self.assertIn('start_timestamp', self.client.get('/api/active').json[0])
        self.assertEqual(self.post('/actions/cancel/abcdefghijk').status_code, 302)
        self.assertTrue(downloader.kill_this.is_set())

    def test_waiting_streams_have_a_separate_tab_and_no_timer(self):
        downloader = SimpleNamespace(info_dict={'title': 'Upcoming stream', 'channel': 'Example channel'}, embed_info={},
                                     livestream_downloader=SimpleNamespace(stats={'status': 'Waiting', 'video': {}, 'audio': {}}),
                                     kill_this=threading.Event())
        self.web.active_downloads['abcdefghijk'] = {
            'downloader': downloader,
            'type': 'stream',
            'start_time': datetime.now() - timedelta(seconds=90),
            'recording_start_time': None,
        }

        waiting_job = self.client.get('/api/active').json[0]
        self.assertTrue(waiting_job['is_waiting'])
        self.assertIsNone(waiting_job['start_timestamp'])
        self.assertIsNone(waiting_job['elapsed'])

        response = self.client.get('/data/active')
        self.assertIn(b'Waiting <span class="recording-tab-count">1</span>', response.data)
        waiting_panel = response.data.split(b'id="waiting-panel"', 1)[1]
        self.assertIn(b'Upcoming stream', waiting_panel)
        self.assertIn(b'Waiting for stream', waiting_panel)
        self.assertNotIn(b'data-started=', waiting_panel)
        self.assertNotIn(b'running time', waiting_panel)

        downloader.livestream_downloader.stats['status'] = 'Recording'
        recording_job = self.client.get('/api/active').json[0]
        self.assertFalse(recording_job['is_waiting'])
        self.assertIsNotNone(recording_job['start_timestamp'])
        self.assertEqual(recording_job['elapsed'], '00:00:00')

    def test_finished_job_stores_embed_fallback_and_refreshes_history(self):
        self.client.get('/data/history')
        downloader = SimpleNamespace(info_dict={'title': None}, embed_info={'title': 'Fallback title', 'author_name': 'Example channel'},
                                     livestream_downloader=SimpleNamespace(stats={'status': 'Finished'}), main=Mock())
        self.web.active_downloads['abcdefghijk'] = {'downloader': downloader, 'type': 'stream'}
        self.web.thread_worker('abcdefghijk', downloader)
        self.assertIn(b'Fallback title', self.client.get('/data/history').data)
        self.assertNotIn('abcdefghijk', self.web.active_downloads)

    def test_finished_jobs_keep_the_archived_thumbnail_with_history(self):
        for download_type in ('stream', 'unarchived'):
            with self.subTest(download_type=download_type):
                archive = self.path.parent / download_type / 'Example stream'
                archive.parent.mkdir()
                thumbnail = Path(f'{archive}.png')
                thumbnail.write_bytes(THUMBNAIL_PNG)
                stale_temp_path = self.path.parent / 'removed-temp-thumbnail.png'
                downloader = SimpleNamespace(
                    info_dict={'title': 'Archived stream'}, embed_info={},
                    thumbnail_output=str(archive), main=Mock(),
                    livestream_downloader=SimpleNamespace(
                        stats={'status': 'Finished'}, file_names={'thumbnail': stale_temp_path}))
                tracker = (self.web.active_unarchived_downloads if download_type == 'unarchived'
                           else self.web.active_downloads)
                tracker['abcdefghijk'] = {'downloader': downloader, 'type': download_type}
                self.web.cache.clear()
                self.client.get('/data/history')
                self.web.thread_worker('abcdefghijk', downloader, tracker)
                row = self.web.get_history()[0]
                self.assertTrue(row['has_thumbnail'])
                self.assertNotIn('abcdefghijk', tracker)
                url = f"/history/{row['id']}/thumbnail"
                for route in ('/data/history', '/data/recent', '/activity'):
                    self.assertIn(f'src="{url}"'.encode(), self.client.get(route).data)
                # History keeps its own copy if archived files are later moved or removed.
                thumbnail.unlink()
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, 'image/png')
                self.assertEqual(response.data, THUMBNAIL_PNG)
                self.assertIn('max-age=86400', response.headers['Cache-Control'])
                api_row = self.client.get('/api/history').json[0]
                self.assertTrue(api_row['has_thumbnail'])
                self.assertNotIn('thumbnail', api_row)
                self.assertNotIn('thumbnail_path', api_row)

    def test_thumbnail_can_still_be_found_at_its_original_path(self):
        thumbnail = self.path.parent / 'thumbnail.png'
        thumbnail.write_bytes(THUMBNAIL_PNG)
        downloader = SimpleNamespace(
            thumbnail_output=str(self.path.parent / 'missing-archive'),
            livestream_downloader=SimpleNamespace(file_names={'thumbnail': thumbnail}))
        self.assertEqual(self.web.get_download_thumbnail(downloader), thumbnail)
        thumbnail.unlink()
        self.assertIsNone(self.web.get_download_thumbnail(downloader))

    def test_missing_or_unsupported_thumbnails_do_not_prevent_history(self):
        unsupported = self.path.parent / 'thumbnail.svg'
        unsupported.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        for thumbnail in (None, self.path.parent / 'missing.png', unsupported):
            with self.subTest(thumbnail=thumbnail):
                self.web.save_to_history('abcdefghijk', {'status': 'Finished'}, thumbnail_path=thumbnail)
                row = self.web.get_history()[0]
                self.assertFalse(row['has_thumbnail'])
                self.assertEqual(self.client.get(f"/history/{row['id']}/thumbnail").status_code, 404)
        self.web.cache.clear()
        response = self.client.get('/data/history')
        self.assertIn(b'src="https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg"', response.data)
        self.assertIn(b'data-history-thumbnail', response.data)
        self.assertEqual(self.client.get('/history/999999/thumbnail').status_code, 404)

    def test_unreadable_thumbnail_does_not_prevent_history(self):
        with patch.object(Path, 'read_bytes', side_effect=PermissionError):
            self.web.save_to_history('abcdefghijk', {'status': 'Finished'}, thumbnail_path='thumbnail.png')
        self.assertEqual(len(self.web.get_history()), 1)
        self.assertFalse(self.web.get_history()[0]['has_thumbnail'])
        downloader = SimpleNamespace(
            info_dict={}, embed_info={}, main=Mock(), thumbnail_output='archive/stream',
            livestream_downloader=SimpleNamespace(
                stats={'status': 'Finished'}, file_names={'thumbnail': Path('thumbnail.png')}))
        self.web.active_downloads['bcdefghijkl'] = {'downloader': downloader, 'type': 'stream'}
        with patch.object(Path, 'is_file', side_effect=PermissionError):
            self.web.thread_worker('bcdefghijkl', downloader)
        self.assertEqual(len(self.web.get_history()), 2)
        self.assertFalse(self.web.get_history()[0]['has_thumbnail'])

    def test_thumbnail_copies_expire_with_their_history_entries(self):
        thumbnail = self.path.parent / 'thumbnail.png'
        thumbnail.write_bytes(THUMBNAIL_PNG)
        self.web.save_to_history('abcdefghijk', {'status': 'Finished'}, thumbnail_path=thumbnail)
        first_id = self.web.get_history()[0]['id']
        for _ in range(50):
            self.web.save_to_history('abcdefghijk', {'status': 'Finished'}, thumbnail_path=thumbnail)
        self.assertEqual(len(self.web.get_history()), 50)
        self.assertEqual(self.client.get(f'/history/{first_id}/thumbnail').status_code, 404)
        latest_id = self.web.get_history()[0]['id']
        self.assertEqual(self.client.get(f'/history/{latest_id}/thumbnail').data, THUMBNAIL_PNG)

    def test_theme_is_per_session_and_keeps_current_page(self):
        other_client = self.web.app.test_client()
        response = self.post('/actions/toggle_theme', return_to='/channels')
        self.assertEqual(response.location, '/channels')
        self.assertIn(b'data-theme="light"', self.client.get('/channels').data)
        self.assertIn(b'data-theme="dark"', other_client.get('/channels').data)
        response = self.client.post('/actions/toggle_theme', data={'csrf_token': self.token},
                                    headers={'X-Requested-With': 'fetch'})
        self.assertEqual(response.json, {'theme': 'dark'})
        self.assertEqual(self.post('/actions/toggle_theme', return_to='//example.com').location, '/')

    def test_invalid_manual_video_does_not_start_download(self):
        with patch.object(self.web, 'start_download') as start:
            self.assertEqual(self.post('/actions/add', video_id='invalid').status_code, 302)
            start.assert_not_called()
            self.assertEqual(self.post('/actions/add', video_id='https://youtu.be/abcdefghijk').status_code, 302)
            start.assert_called_once_with('abcdefghijk')


if __name__ == '__main__':
    unittest.main()
