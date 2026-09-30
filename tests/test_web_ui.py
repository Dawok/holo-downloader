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
            self.web.save_to_history('bcdefghijkl', {'status': 'Finished'}, info={'title': 'Recorded title', 'channel': 'Example channel'})
            self.assertEqual(self.web.get_history()[0]['title'], 'Recorded title')
            self.assertEqual(self.client.get('/data/history').status_code, 200)

    def test_recording_rows_escape_titles_and_stop_sets_event(self):
        downloader = SimpleNamespace(info_dict={'title': '<script>alert(1)</script>', 'channel': 'Example channel'}, embed_info={},
                                     livestream_downloader=SimpleNamespace(stats={'status': 'Recording', 'video': {'current_filesize': 1024}}),
                                     kill_this=threading.Event())
        self.web.active_downloads['abcdefghijk'] = {'downloader': downloader, 'type': 'stream', 'start_time': datetime.now() - timedelta(hours=2)}
        response = self.client.get('/data/active')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'&lt;script&gt;', response.data)
        self.assertNotIn(b'<script>alert(1)', response.data)
        self.assertIn(b'02:00:', response.data)
        self.assertIn('start_timestamp', self.client.get('/api/active').json[0])
        self.assertEqual(self.post('/actions/cancel/abcdefghijk').status_code, 302)
        self.assertTrue(downloader.kill_this.is_set())

    def test_finished_job_stores_embed_fallback_and_refreshes_history(self):
        self.client.get('/data/history')
        downloader = SimpleNamespace(info_dict={'title': None}, embed_info={'title': 'Fallback title', 'author_name': 'Example channel'},
                                     livestream_downloader=SimpleNamespace(stats={'status': 'Finished'}), main=Mock())
        self.web.active_downloads['abcdefghijk'] = {'downloader': downloader, 'type': 'stream'}
        self.web.thread_worker('abcdefghijk', downloader)
        self.assertIn(b'Fallback title', self.client.get('/data/history').data)
        self.assertNotIn('abcdefghijk', self.web.active_downloads)

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
