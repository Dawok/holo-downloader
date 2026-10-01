import importlib.util
import logging
from copy import deepcopy
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch
import yt_dlp

try:
    from livestream_dl import getUrls as dependency_get_urls
except ModuleNotFoundError:
    dependency_get_urls = None


class DownloadVidTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        modules = {name: ModuleType(name) for name in
                   ('common', 'discord_web', 'livestream_dl', 'getConfig')}
        modules['common'].FileLock = Mock()
        modules['common'].setup_umask = Mock()
        modules['common'].kill_all = threading.Event()
        modules['common'].initialize_logging = Mock()
        modules['discord_web'].main = Mock()
        modules['getConfig'].ConfigHandler = Mock()
        modules['livestream_dl'].download_Live = SimpleNamespace()
        modules['livestream_dl'].getUrls = SimpleNamespace(get_Video_Info=Mock())
        path = Path(__file__).resolve().parents[1] / 'downloadVid.py'
        spec = importlib.util.spec_from_file_location('download_vid_under_test', path)
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        self.downloader = self.module.VideoDownloader.__new__(self.module.VideoDownloader)
        self.downloader.id = 'abcdefghijk'
        self.downloader.channel_id = None
        self.downloader.outputFile = None
        self.downloader.info_dict = {}
        self.downloader.logger = logging.getLogger('download-vid-tests')
        self.downloader.kill_this = threading.Event()
        self.downloader.check_now = threading.Event()
        self.downloader.livestream_downloader = SimpleNamespace(stats={})
        self.downloader.downloader = Mock()
        self.downloader.config = SimpleNamespace(
            get_ytdlp=lambda channel: '%(title)s.%(ext)s',
            upcoming_video_max_wait=lambda: 900,
            get_ytdlp_options=lambda: '{}',
            get_cookies_file=lambda: None,
            get_proxy=lambda: None,
            get_include_dash=lambda: False,
            get_include_m3u8=lambda: False,
            get_clean_info_json=lambda: False,
        )
        self.upcoming = {'id': 'abcdefghijk', 'title': 'Upcoming stream', 'ext': 'mp4',
                         'live_status': 'is_upcoming', 'release_timestamp': 3600}
        self.live = {**self.upcoming, 'title': 'Live stream', 'live_status': 'is_live'}
        self.module.discord_web.main.reset_mock()

    def test_upcoming_stream_keeps_waiting_across_maximum_polling_intervals(self):
        responses = [(self.upcoming, 'is_upcoming'), (self.upcoming, 'is_upcoming'), (self.live, 'is_live')]
        with patch.object(self.module.getUrls, 'get_Video_Info', side_effect=responses) as extract, \
                patch.object(self.downloader, '_wait_for_live_check') as wait, \
                patch.object(self.module, 'time', side_effect=[0, 900]):
            output, info = self.downloader.download_video_info('abcdefghijk')
        self.assertEqual(output, 'Live stream.mp4')
        self.assertIs(info, self.live)
        self.assertEqual(extract.call_count, 3)
        wait.assert_has_calls([call(900), call(900)])
        self.assertTrue(all(arguments.kwargs['wait'] is False for arguments in extract.call_args_list))

    def test_upcoming_polling_interval_respects_the_scheduled_start_and_bounds(self):
        for release_timestamp, expected_wait in ((30, 60), (120, 120), (3600, 900), (None, 300)):
            with self.subTest(release_timestamp=release_timestamp):
                upcoming = {**self.upcoming, 'release_timestamp': release_timestamp}
                with patch.object(self.module.getUrls, 'get_Video_Info',
                                  side_effect=[(upcoming, 'is_upcoming'), (self.live, 'is_live')]), \
                        patch.object(self.downloader, '_wait_for_live_check') as wait, \
                        patch.object(self.module, 'time', return_value=0), \
                        patch.object(self.module, 'uniform', return_value=300):
                    self.downloader.download_video_info('abcdefghijk')
                wait.assert_called_once_with(expected_wait)

    def test_manual_check_wakes_waiting_job_and_starts_recording_when_live(self):
        waiting = threading.Event()
        original_wait = self.downloader.check_now.wait

        def wait(timeout):
            waiting.set()
            return original_wait(timeout)

        worker = threading.Thread(target=self.downloader.main, daemon=True)
        try:
            with patch.object(self.module.getUrls, 'get_Video_Info',
                              side_effect=[(self.upcoming, 'is_upcoming'), (self.live, 'is_live')]) as extract, \
                    patch.object(self.downloader.check_now, 'wait', side_effect=wait):
                worker.start()
                self.assertTrue(waiting.wait(2))
                self.assertTrue(self.downloader.request_live_check())
                worker.join(2)
                self.assertFalse(worker.is_alive())
            self.assertEqual(extract.call_count, 2)
            self.downloader.downloader.assert_called_once_with(self.live)
            self.assertFalse(self.downloader.kill_this.is_set())
        finally:
            self.downloader.kill_this.set()
            self.downloader.check_now.set()
            worker.join(2)

    def test_manual_check_during_metadata_lookup_is_not_lost(self):
        looking_up = threading.Event()
        finish_lookup = threading.Event()

        def extract(**kwargs):
            if not looking_up.is_set():
                looking_up.set()
                if not finish_lookup.wait(2):
                    raise TimeoutError('Metadata lookup was not released')
                return self.upcoming, 'is_upcoming'
            return self.live, 'is_live'

        worker = threading.Thread(target=self.downloader.main, daemon=True)
        try:
            with patch.object(self.module.getUrls, 'get_Video_Info', side_effect=extract) as lookup:
                worker.start()
                self.assertTrue(looking_up.wait(2))
                self.assertTrue(self.downloader.request_live_check())
                finish_lookup.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
            self.assertEqual(lookup.call_count, 2)
            self.downloader.downloader.assert_called_once_with(self.live)
        finally:
            self.downloader.kill_this.set()
            self.downloader.check_now.set()
            finish_lookup.set()
            worker.join(2)

    def test_manual_check_keeps_monitoring_when_stream_is_still_upcoming(self):
        waiting = threading.Event()
        waiting_again = threading.Event()
        original_wait = self.downloader.check_now.wait

        def wait(timeout):
            (waiting if lookup.call_count == 1 else waiting_again).set()
            return original_wait(timeout)

        worker = threading.Thread(target=self.downloader.main, daemon=True)
        try:
            with patch.object(self.module.getUrls, 'get_Video_Info',
                              return_value=(self.upcoming, 'is_upcoming')) as lookup, \
                    patch.object(self.downloader.check_now, 'wait', side_effect=wait):
                worker.start()
                self.assertTrue(waiting.wait(2))
                self.assertTrue(self.downloader.request_live_check())
                self.assertTrue(waiting_again.wait(2))
                self.assertEqual(lookup.call_count, 2)
                self.assertTrue(worker.is_alive())
                self.assertFalse(self.downloader.check_now.is_set())
                self.downloader.downloader.assert_not_called()
                self.downloader.kill_this.set()
                self.downloader.check_now.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
        finally:
            self.downloader.kill_this.set()
            self.downloader.check_now.set()
            worker.join(2)

    def test_manual_check_does_not_interrupt_recording_or_removed_jobs(self):
        for status, cancelled in (('Recording', False), ('Muxing', False), ('Waiting', True)):
            with self.subTest(status=status, cancelled=cancelled):
                self.downloader.livestream_downloader.stats['status'] = status
                if cancelled:
                    self.downloader.kill_this.set()
                self.assertFalse(self.downloader.request_live_check())
                self.assertFalse(self.downloader.check_now.is_set())

    def test_remove_interrupts_a_waiting_stream_without_starting_recording(self):
        waiting = threading.Event()
        original_wait = self.downloader.check_now.wait

        def wait(timeout):
            waiting.set()
            return original_wait(timeout)

        worker = threading.Thread(target=self.downloader.main, daemon=True)
        try:
            with patch.object(self.module.getUrls, 'get_Video_Info', return_value=(self.upcoming, 'is_upcoming')), \
                    patch.object(self.downloader.check_now, 'wait', side_effect=wait):
                worker.start()
                self.assertTrue(waiting.wait(2))
                self.downloader.kill_this.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
            self.assertEqual(self.downloader.livestream_downloader.stats['status'], 'Cancelled')
            self.downloader.downloader.assert_not_called()
            self.module.discord_web.main.assert_called_once_with('abcdefghijk', 'waiting', config=self.downloader.config)
        finally:
            self.downloader.kill_this.set()
            worker.join(2)

    def test_remove_during_metadata_lookup_does_not_start_recording(self):
        def extract(**kwargs):
            self.downloader.kill_this.set()
            return self.live, 'is_live'

        with patch.object(self.module.getUrls, 'get_Video_Info', side_effect=extract):
            self.downloader.main()
        self.assertEqual(self.downloader.livestream_downloader.stats['status'], 'Cancelled')
        self.downloader.downloader.assert_not_called()

    def test_metadata_failure_marks_the_stream_error_and_keeps_the_message(self):
        with patch.object(self.module.getUrls, 'get_Video_Info',
                          side_effect=yt_dlp.utils.DownloadError('Video unavailable')), \
                self.assertLogs(self.downloader.logger, level='ERROR'):
            self.downloader.main()
        self.assertEqual(self.downloader.livestream_downloader.stats['status'], 'Error')
        self.assertEqual(self.downloader.livestream_downloader.stats['error_message'], 'DownloadError: Video unavailable')
        self.downloader.downloader.assert_not_called()

    @unittest.skipIf(dependency_get_urls is None, 'livestream_dl is supplied by the Docker base image')
    def test_dependency_keeps_upcoming_streams_without_formats_waiting(self):
        metadata = {**self.upcoming, 'formats': [], 'extractor': 'youtube', 'extractor_key': 'Youtube'}

        def extract(ydl, url, **kwargs):
            return ydl.process_ie_result(deepcopy(metadata), download=False)

        def remove(timeout):
            self.downloader.kill_this.set()
            return True

        with patch.object(self.module, 'getUrls', dependency_get_urls), \
                patch.object(yt_dlp.YoutubeDL, 'extract_info', extract), \
                patch.object(self.downloader, '_wait_for_live_check', side_effect=remove), \
                self.assertLogs(self.downloader.logger, level='WARNING'):
            with self.assertRaises(InterruptedError):
                self.downloader.download_video_info('abcdefghijk')

    @unittest.skipIf(dependency_get_urls is None, 'livestream_dl is supplied by the Docker base image')
    def test_dependency_returns_formats_when_an_upcoming_stream_goes_live(self):
        responses = iter([
            {**self.upcoming, 'formats': [], 'extractor': 'youtube', 'extractor_key': 'Youtube'},
            {**self.live, 'extractor': 'youtube', 'extractor_key': 'Youtube', 'formats': [{
                'format_id': '18', 'url': 'https://example.com/live.mp4',
                'ext': 'mp4', 'vcodec': 'h264', 'acodec': 'aac',
            }]},
        ])

        def extract(ydl, url, **kwargs):
            return ydl.process_ie_result(deepcopy(next(responses)), download=False)

        with patch.object(self.module, 'getUrls', dependency_get_urls), \
                patch.object(yt_dlp.YoutubeDL, 'extract_info', extract), \
                patch.object(self.downloader, '_wait_for_live_check') as wait, \
                self.assertLogs(self.downloader.logger, level='WARNING'):
            output, info = self.downloader.download_video_info('abcdefghijk')
        self.assertTrue(output.startswith('Live stream'))
        self.assertTrue(output.endswith('.mp4'))
        self.assertEqual(info['live_status'], 'is_live')
        self.assertEqual(info['formats'][0]['url'], 'https://example.com/live.mp4')
        wait.assert_called_once()

    @unittest.skipIf(dependency_get_urls is None, 'livestream_dl is supplied by the Docker base image')
    def test_dependency_still_rejects_unavailable_videos(self):
        with patch.object(self.module, 'getUrls', dependency_get_urls), \
                patch.object(yt_dlp.YoutubeDL, 'extract_info',
                             side_effect=yt_dlp.utils.DownloadError('Video unavailable')):
            with self.assertRaises(dependency_get_urls.VideoUnavailableError):
                self.downloader.download_video_info('abcdefghijk')


if __name__ == '__main__':
    unittest.main()
