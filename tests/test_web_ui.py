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
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from channel_config import channels_from_config, read_config
from stream_auth import CookieSession


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
        timezone_patcher = patch.dict(os.environ, {'TZ': 'UTC'})
        timezone_patcher.start()
        self.addCleanup(timezone_patcher.stop)
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
            self.assertIsNone(self.web.get_history()[0]['error_message'])
            self.web.save_to_history('bcdefghijkl', {'status': 'Finished'}, info={'title': 'Recorded title', 'channel': 'Example channel'})
            self.assertEqual(self.web.get_history()[0]['title'], 'Recorded title')
            self.assertEqual(self.client.get('/data/history').status_code, 200)

    def test_recording_rows_escape_titles_and_remove_hides_stream(self):
        downloader = SimpleNamespace(info_dict={'title': '<script>alert(1)</script>', 'channel': 'Example channel'}, embed_info={},
                                     livestream_downloader=SimpleNamespace(stats={'status': 'Recording', 'video': {'current_filesize': 1024}}),
                                     kill_this=threading.Event())
        started = datetime.now() - timedelta(hours=2)
        self.web.active_downloads['abcdefghijk'] = {'downloader': downloader, 'type': 'stream', 'start_time': started, 'recording_start_time': started}
        response = self.client.get('/data/active')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'&lt;script&gt;', response.data)
        self.assertNotIn(b'<script>alert(1)', response.data)
        self.assertIn(b'Started at', response.data)
        self.assertNotIn(b'running time', response.data)
        self.assertNotIn(b'data-started=', response.data)
        self.assertEqual(self.client.get('/api/active').json[0]['start_timestamp'], started.timestamp())
        self.assertEqual(self.post('/actions/cancel/abcdefghijk').status_code, 302)
        self.assertTrue(downloader.kill_this.is_set())
        self.assertTrue(self.web.active_downloads['abcdefghijk']['remove_requested'])
        self.assertTrue(self.web.is_stream_removed('abcdefghijk'))
        self.assertEqual(self.client.get('/api/active').json, [])

    def test_waiting_streams_share_the_stream_list_without_a_timer(self):
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
        self.assertIn(b'Upcoming stream', response.data)
        self.assertIn(b'Waiting for stream', response.data)
        self.assertNotIn(b'data-recording-tab', response.data)
        self.assertNotIn(b'data-started=', response.data)
        self.assertNotIn(b'running time', response.data)

        downloader.livestream_downloader.stats['status'] = 'Recording'
        recording_job = self.client.get('/api/active').json[0]
        self.assertFalse(recording_job['is_waiting'])
        self.assertEqual(recording_job['start_timestamp'], self.web.active_downloads['abcdefghijk']['start_time'].timestamp())
        self.assertIsNone(self.web.active_downloads['abcdefghijk']['recording_start_time'])
        response = self.client.get('/data/active')
        self.assertIn(b'Started at', response.data)
        self.assertNotIn(b'running time', response.data)

    def test_stream_start_comes_from_metadata_and_page_views_do_not_reset_it(self):
        released_at = datetime(2026, 7, 1, 22, 30, tzinfo=timezone.utc)
        for tracker, fragment, api in (
                (self.web.active_downloads, '/data/active', '/api/active'),
                (self.web.active_unarchived_downloads, '/data/unarchived', '/api/unarchived')):
            with self.subTest(fragment=fragment):
                downloader = SimpleNamespace(
                    info_dict={'title': 'Live stream', 'release_timestamp': released_at.timestamp()},
                    embed_info={}, livestream_downloader=SimpleNamespace(stats={'status': 'Recording'}))
                entry = {'downloader': downloader, 'type': 'stream', 'start_time': datetime.now()}
                tracker['abcdefghijk'] = entry
                original = entry.copy()
                for _ in range(2):
                    self.client.get('/')
                    response = self.client.get(fragment)
                    self.assertIn(b'Started at', response.data)
                    self.assertIn(b'datetime="2026-07-01T22:30:00+00:00"', response.data)
                    self.assertNotIn(b'data-started=', response.data)
                    self.assertEqual(self.client.get(api).json[0]['start_timestamp'], released_at.timestamp())
                    self.assertEqual(entry, original)

    def test_scheduled_streams_sort_by_time_and_move_to_the_top_when_recording(self):
        queued_at = datetime.now()
        released_at = datetime(2026, 7, 1, 18, tzinfo=timezone.utc).timestamp()
        for video_id, status, release in (
                ('laterstream', 'Waiting for scheduled time', released_at + 7200),
                ('unknownplan', 'Waiting', None),
                ('earlierplan', 'Waiting', released_at + 3600),
                ('livestream1', 'Recording', released_at)):
            self.web.active_downloads[video_id] = {
                'downloader': SimpleNamespace(
                    info_dict={'title': video_id, 'release_timestamp': release}, embed_info={},
                    livestream_downloader=SimpleNamespace(stats={'status': status})),
                'type': 'stream', 'start_time': queued_at,
            }
        jobs = self.client.get('/api/active').json
        self.assertEqual([job['id'] for job in jobs],
                         ['livestream1', 'earlierplan', 'laterstream', 'unknownplan'])
        self.assertEqual(jobs[1]['scheduled_datetime'], '2026-07-01T19:00:00+00:00')
        response = self.client.get('/data/active')
        self.assertIn(b'Scheduled for', response.data)
        self.assertIn(b'datetime="2026-07-01T19:00:00+00:00"', response.data)
        self.assertNotIn(b'data-started=', response.data)

        downloader = self.web.active_downloads['earlierplan']['downloader']
        downloader.livestream_downloader.stats['status'] = 'Recording'
        downloader.info_dict['release_timestamp'] = released_at + 3660
        jobs = self.client.get('/api/active').json
        self.assertEqual([job['id'] for job in jobs],
                         ['earlierplan', 'livestream1', 'laterstream', 'unknownplan'])
        self.assertIsNone(jobs[0]['scheduled_datetime'])
        self.assertEqual(jobs[0]['start_datetime'], '2026-07-01T19:01:00+00:00')

    def test_history_dates_keep_utc_storage_and_expose_iso_times_for_display(self):
        self.web.save_to_history('abcdefghijk', {'status': 'Finished'})
        with closing(sqlite3.connect(self.web.DB_FILE)) as conn, conn:
            conn.execute("UPDATE history SET timestamp = '2026-07-01 22:30:00'")
        for route in ('/', '/activity', '/data/history', '/data/recent'):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertIn(b'<time datetime="2026-07-01T22:30:00+00:00"', response.data)
                self.assertIn(b'22:30 UTC', response.data)
        self.assertEqual(self.client.get('/api/history').json[0]['timestamp'], '2026-07-01 22:30:00')

    def test_history_and_stream_dates_use_configured_timezone_and_dst_for_each_date(self):
        self.web.save_to_history('abcdefghijk', {'status': 'Finished'})
        scenarios = (
            ('Europe/Berlin', '2026-07-01 22:30:00', '2026-07-02T00:30:00+02:00', '00:30 CEST', 'Jul 2, 2026'),
            ('Europe/Berlin', '2026-01-01 10:30:00', '2026-01-01T11:30:00+01:00', '11:30 CET', 'Jan 1, 2026'),
            ('Europe/Berlin', '2026-03-29 00:30:00', '2026-03-29T01:30:00+01:00', '01:30 CET', 'Mar 29, 2026'),
            ('Europe/Berlin', '2026-03-29 01:30:00', '2026-03-29T03:30:00+02:00', '03:30 CEST', 'Mar 29, 2026'),
            ('Europe/Berlin', '2026-10-25 00:30:00', '2026-10-25T02:30:00+02:00', '02:30 CEST', 'Oct 25, 2026'),
            ('Europe/Berlin', '2026-10-25 01:30:00', '2026-10-25T02:30:00+01:00', '02:30 CET', 'Oct 25, 2026'),
            ('America/New_York', '2026-07-01 02:30:00', '2026-06-30T22:30:00-04:00', '22:30 EDT', 'Jun 30, 2026'),
            ('UTC', '2026-07-01 22:30:00', '2026-07-01T22:30:00+00:00', '22:30 UTC', 'Jul 1, 2026'),
        )
        for zone, timestamp, expected_iso, expected_time, expected_date in scenarios:
            with self.subTest(zone=zone, timestamp=timestamp), patch.dict(os.environ, {'TZ': zone}):
                with closing(sqlite3.connect(self.web.DB_FILE)) as conn, conn:
                    conn.execute('UPDATE history SET timestamp = ?', (timestamp,))
                self.web.cache.clear()
                released_at = datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)
                for video_id, status in (('livestream1', 'Recording'), ('earlierplan', 'Waiting')):
                    self.web.active_downloads[video_id] = {
                        'downloader': SimpleNamespace(
                            info_dict={'release_timestamp': released_at.timestamp()}, embed_info={},
                            livestream_downloader=SimpleNamespace(stats={'status': status})),
                        'type': 'stream', 'start_time': datetime.now(timezone.utc),
                    }
                for route in ('/', '/activity', '/data/history', '/data/recent', '/data/active'):
                    response = self.client.get(route)
                    self.assertIn(f'datetime="{expected_iso}"'.encode(), response.data)
                    self.assertIn(f'>{expected_time}<small>{expected_date}</small>'.encode(), response.data)
                self.assertEqual(self.client.get('/api/history').json[0]['timestamp'], timestamp)

    def test_remove_cleans_only_stream_temp_files_and_allows_readding(self):
        temp_root = self.path.parent / 'temp'
        stream_folder = temp_root / 'Example channel' / 'Upcoming (abcdefghijk)'
        stream_folder.mkdir(parents=True)
        temp_file = stream_folder / 'video.mp4.temp'
        temp_file.write_bytes(b'partial')
        other_stream_file = temp_root / 'Example channel' / 'Other (bcdefghijkl)' / 'video.mp4.temp'
        other_stream_file.parent.mkdir(parents=True)
        other_stream_file.write_bytes(b'other partial')
        final_file = self.path.parent / 'Done' / 'video.mp4'
        final_file.parent.mkdir()
        final_file.write_bytes(b'archived')
        replacement = SimpleNamespace()
        downloader = SimpleNamespace(
            info_dict={},
            embed_info={},
            config=SimpleNamespace(get_temp_folder=lambda: str(temp_root)),
            temp_output_dir=str(stream_folder),
            livestream_downloader=SimpleNamespace(stats={'status': 'Cancelled'}, file_names={'databases': []}),
            main=Mock(),
        )
        self.web.active_downloads['abcdefghijk'] = {
            'downloader': downloader,
            'type': 'stream',
            'remove_requested': True,
        }

        self.web.thread_worker('abcdefghijk', downloader)

        self.assertFalse(stream_folder.exists())
        self.assertTrue(other_stream_file.exists())
        self.assertTrue(final_file.exists())
        self.assertFalse(self.web.get_history())
        self.assertNotIn('abcdefghijk', self.web.active_downloads)
        self.web.suppress_stream('abcdefghijk')
        self.web.init_db()
        self.assertTrue(self.web.is_stream_removed('abcdefghijk'))
        self.assertFalse(self.web.start_unarchived_download('abcdefghijk'))
        with patch.object(self.web.downloadVid, 'VideoDownloader', return_value=replacement, create=True), \
                patch.object(self.web.threading, 'Thread', return_value=Mock()):
            self.assertFalse(self.web.start_download('abcdefghijk'))
            self.assertTrue(self.web.start_download('abcdefghijk', manual=True))
        self.assertIs(self.web.active_downloads['abcdefghijk']['downloader'], replacement)
        self.assertFalse(self.web.is_stream_removed('abcdefghijk'))

    def test_removing_recording_deletes_untracked_partial_files_for_all_temp_layouts(self):
        modules = {
            'common': SimpleNamespace(FileLock=Mock(), setup_umask=Mock(),
                                      kill_all=threading.Event(), initialize_logging=Mock()),
            'discord_web': SimpleNamespace(main=Mock()),
            'getConfig': SimpleNamespace(ConfigHandler=Mock()),
            'livestream_dl': SimpleNamespace(download_Live=SimpleNamespace(), getUrls=SimpleNamespace()),
        }
        spec = importlib.util.spec_from_file_location('recording_under_test', ROOT / 'downloadVid.py')
        recording = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(recording)

        for layout in ('shared', 'template', 'stream-template'):
            with self.subTest(layout=layout):
                temp_root = self.path.parent / layout / 'temp'
                temp_root.mkdir(parents=True)
                configured_temp = {
                    'shared': temp_root,
                    'template': temp_root / '%(channel)s' / '%(title)s',
                    'stream-template': temp_root / '%(channel)s' / '%(title)s (%(id)s)',
                }[layout]
                other_file = temp_root / 'bcdefghijkl' / 'video.temp'
                other_file.parent.mkdir()
                other_file.write_bytes(b'other recording')
                shared_file = temp_root / 'unrelated.temp'
                shared_file.write_bytes(b'keep shared files')
                archive = temp_root.parent / 'Done'
                archive.mkdir()
                final_file = archive / 'finished.mp4'
                final_file.write_bytes(b'archived recording')
                info = {'id': 'abcdefghijk', 'title': 'Example stream',
                        'fulltitle': 'Example stream', 'channel': 'Example channel'}
                started = threading.Event()
                folders = []
                downloader = recording.VideoDownloader.__new__(recording.VideoDownloader)
                downloader.id = info['id']
                downloader.kill_this = threading.Event()
                downloader.logger = logging.getLogger('recording-removal-tests')
                downloader.cookie_session = CookieSession(None, downloader.logger)
                downloader.info_dict = {}
                downloader.embed_info = {}
                downloader.temp_output_dir = None
                downloader.download_video_info = Mock(return_value=('Example stream', info))
                downloader.config = SimpleNamespace(
                    get_temp_folder=lambda: str(configured_temp),
                    get_livestream_dl_options=lambda **kwargs: {
                        'temp_folder': str(configured_temp), 'output': str(archive / 'Example stream'),
                    },
                )

                def output_filename(metadata, template):
                    with recording.yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                        return ydl.prepare_filename(metadata, outtmpl=template)

                def download_segments(info_dict, resolution, options):
                    # A library may copy its options and leave these files out
                    # of file_names when cancellation interrupts recording.
                    local_options = options.copy()
                    folder = Path(output_filename(info_dict, local_options['temp_folder']))
                    local_options['temp_folder'] = str(folder)
                    folder.mkdir(parents=True, exist_ok=True)
                    folders.append(folder)
                    for filename in ('video.137.temp', 'video.137.temp-wal', 'audio.140.ts', 'chat.json.part'):
                        (folder / filename).write_bytes(b'partial recording')
                    started.set()
                    if not downloader.kill_this.wait(5):
                        raise TimeoutError('Recording was not removed')
                    raise KeyboardInterrupt('Recording was removed')

                downloader.livestream_downloader = SimpleNamespace(
                    stats={}, file_names={'streams': {}, 'databases': []},
                    output_filename=output_filename, download_segments=download_segments,
                )
                self.web.active_downloads[info['id']] = {'downloader': downloader, 'type': 'stream'}
                worker = threading.Thread(target=self.web.thread_worker,
                                          args=(info['id'], downloader), daemon=True)
                try:
                    worker.start()
                    self.assertTrue(started.wait(5))
                    self.assertEqual(self.post(f'/actions/cancel/{info["id"]}').status_code, 302)
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                    self.assertFalse(folders[0].exists())
                    self.assertEqual(other_file.read_bytes(), b'other recording')
                    self.assertEqual(shared_file.read_bytes(), b'keep shared files')
                    self.assertEqual(final_file.read_bytes(), b'archived recording')
                    self.assertNotIn(info['id'], self.web.active_downloads)
                    self.assertFalse(self.web.get_history())
                finally:
                    downloader.kill_this.set()
                    worker.join(5)

    def test_finished_job_stores_embed_fallback_and_refreshes_history(self):
        self.client.get('/data/history')
        downloader = SimpleNamespace(info_dict={'title': None}, embed_info={'title': 'Fallback title', 'author_name': 'Example channel'},
                                     livestream_downloader=SimpleNamespace(stats={'status': 'Finished'}), main=Mock())
        self.web.active_downloads['abcdefghijk'] = {'downloader': downloader, 'type': 'stream'}
        self.web.thread_worker('abcdefghijk', downloader)
        self.assertIn(b'Fallback title', self.client.get('/data/history').data)
        self.assertNotIn('abcdefghijk', self.web.active_downloads)

    def test_worker_failure_still_removes_temporary_files_and_clears_the_job(self):
        temp_root = self.path.parent / 'temp'
        stream_folder = temp_root / 'Example (abcdefghijk)'
        stream_folder.mkdir(parents=True)
        (stream_folder / 'video.mp4.temp').write_bytes(b'partial')
        downloader = SimpleNamespace(
            config=SimpleNamespace(get_temp_folder=lambda: str(temp_root)),
            temp_output_dir=str(stream_folder),
            livestream_downloader=SimpleNamespace(stats={}, file_names={}),
            main=Mock(side_effect=RuntimeError('Download failed')),
        )
        self.web.active_downloads['abcdefghijk'] = {'downloader': downloader, 'remove_requested': True}
        with self.assertLogs(self.web.common.logger, level='ERROR') as logs:
            self.web.thread_worker('abcdefghijk', downloader)
        self.assertIn('Download failed', logs.output[0])
        self.assertFalse(stream_folder.exists())
        self.assertNotIn('abcdefghijk', self.web.active_downloads)
        self.assertFalse(self.web.get_history())

    def test_failed_streams_show_escaped_error_details_and_can_be_scanned_again(self):
        message = 'DownloadError: <script>alert(1)</script> requested format is not available'
        downloader = SimpleNamespace(
            info_dict={'title': 'Failed stream'}, embed_info={}, main=Mock(),
            livestream_downloader=SimpleNamespace(stats={'status': 'Error', 'error_message': message}),
        )
        self.web.active_downloads['abcdefghijk'] = {'downloader': downloader, 'type': 'stream'}
        self.web.thread_worker('abcdefghijk', downloader)
        self.web.init_db()
        row = self.web.get_history()[0]
        self.assertEqual(row['status'], 'Error')
        self.assertEqual(row['error_message'], message)
        self.assertEqual(self.client.get('/api/history').json[0]['error_message'], message)
        for route in ('/data/history', '/data/recent', '/activity'):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertIn(b'status-danger', response.data)
                self.assertIn(b'<details class="history-error">', response.data)
                self.assertIn(b'&lt;script&gt;', response.data)
                self.assertNotIn(b'<script>alert(1)', response.data)
        self.assertNotIn('abcdefghijk', self.web.active_downloads)
        self.assertFalse(self.web.is_stream_removed('abcdefghijk'))
        with patch.object(self.web.downloadVid, 'VideoDownloader', return_value=downloader, create=True), \
                patch.object(self.web.threading, 'Thread', return_value=Mock()):
            self.assertTrue(self.web.start_download('abcdefghijk'))

    def test_uncaught_download_errors_are_saved_in_history_and_clear_the_active_job(self):
        downloader = SimpleNamespace(
            info_dict={}, embed_info={},
            main=Mock(side_effect=RuntimeError('Extraction failed')),
            livestream_downloader=SimpleNamespace(stats={'status': 'Waiting'}),
        )
        self.web.active_downloads['abcdefghijk'] = {'downloader': downloader, 'type': 'stream'}
        with self.assertLogs(self.web.common.logger, level='ERROR'):
            self.web.thread_worker('abcdefghijk', downloader)
        row = self.web.get_history()[0]
        self.assertEqual(row['status'], 'Error')
        self.assertEqual(row['error_message'], 'RuntimeError: Extraction failed')
        self.assertNotIn('abcdefghijk', self.web.active_downloads)
        self.assertFalse(self.web.is_stream_removed('abcdefghijk'))

    def test_remove_still_cancels_when_blacklist_storage_is_unavailable(self):
        downloader = SimpleNamespace(kill_this=threading.Event())
        self.web.active_downloads['abcdefghijk'] = {'downloader': downloader}
        with patch.object(self.web, 'suppress_stream', side_effect=sqlite3.OperationalError('database locked')), \
                self.assertLogs(self.web.common.logger, level='ERROR'):
            self.assertEqual(self.post('/actions/cancel/abcdefghijk').status_code, 302)
        self.assertTrue(downloader.kill_this.is_set())
        self.assertTrue(self.web.active_downloads['abcdefghijk']['remove_requested'])
        self.assertEqual(self.client.get('/api/active').json, [])
        with self.client.session_transaction() as session:
            self.assertTrue(any(category == 'warning' and 'could not be saved' in message
                                for category, message in session['_flashes']))

    def test_cleanup_preserves_files_outside_the_temporary_root(self):
        temp_root = self.path.parent / 'temp'
        temp_root.mkdir()
        archive = self.path.parent / 'Done' / 'Example (abcdefghijk)'
        archive.mkdir(parents=True)
        final_file = archive / 'video.mp4'
        final_file.write_bytes(b'archived')
        (temp_root / 'linked-archive').symlink_to(archive, target_is_directory=True)
        downloader = SimpleNamespace(
            config=SimpleNamespace(get_temp_folder=lambda: str(temp_root)),
            temp_output_dir=str(archive),
            livestream_downloader=SimpleNamespace(file_names={
                'merged': final_file, 'thumbnail': temp_root / 'linked-archive' / 'video.mp4',
            }),
        )
        with self.assertLogs(self.web.common.logger, level='ERROR'):
            self.web.remove_download_temp_files('abcdefghijk', downloader)
        self.assertEqual(final_file.read_bytes(), b'archived')
        self.assertTrue((temp_root / 'linked-archive').is_symlink())

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

    def test_manual_add_rechecks_waiting_job_without_starting_another_download(self):
        downloader = SimpleNamespace(
            kill_this=threading.Event(), request_live_check=Mock(return_value=True),
            livestream_downloader=SimpleNamespace(stats={'status': 'Waiting'}),
        )
        self.web.active_downloads['abcdefghijk'] = {'downloader': downloader}
        with patch.object(self.web.downloadVid, 'VideoDownloader', create=True) as create, \
                patch.object(self.web.threading, 'Thread') as thread:
            self.assertEqual(self.post('/actions/add', video_id='https://youtu.be/abcdefghijk').status_code, 302)
            create.assert_not_called()
            thread.assert_not_called()
        downloader.request_live_check.assert_called_once_with()
        self.assertIs(self.web.active_downloads['abcdefghijk']['downloader'], downloader)
        self.assertFalse(downloader.kill_this.is_set())
        with self.client.session_transaction() as session:
            category, message = session['_flashes'][-1]
            self.assertEqual(category, 'success')
            self.assertEqual(message, 'Stream abcdefghijk is already added and waiting to start. Checking whether it is live now.')

    def test_manual_add_keeps_recording_and_removing_jobs(self):
        for removing in (False, True):
            with self.subTest(removing=removing):
                downloader = SimpleNamespace(
                    request_live_check=Mock(return_value=False),
                    livestream_downloader=SimpleNamespace(stats={'status': 'Recording'}),
                )
                self.web.active_downloads['abcdefghijk'] = {
                    'downloader': downloader, 'remove_requested': removing,
                }
                with patch.object(self.web.downloadVid, 'VideoDownloader', create=True) as create:
                    self.assertEqual(self.post('/actions/add', video_id='abcdefghijk').status_code, 302)
                    create.assert_not_called()
                if removing:
                    downloader.request_live_check.assert_not_called()
                with self.client.session_transaction() as session:
                    message = session['_flashes'][-1][1]
                    self.assertIn('being removed' if removing else 'already recording', message)

    def test_invalid_manual_video_does_not_start_download(self):
        with patch.object(self.web, 'start_download') as start:
            self.assertEqual(self.post('/actions/add', video_id='invalid').status_code, 302)
            start.assert_not_called()
            self.assertEqual(self.post('/actions/add', video_id='https://youtu.be/abcdefghijk').status_code, 302)
            start.assert_called_once_with('abcdefghijk', manual=True)


if __name__ == '__main__':
    unittest.main()
