import importlib.util
import json
import logging
from pathlib import Path
import queue
import signal
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from yt_dlp.utils import DownloadError
from stream_auth import CookieSession


ROOT = Path(__file__).resolve().parents[1]


class CookieSessionTests(unittest.TestCase):
    def session(self, **kwargs):
        return CookieSession('example-cookies.txt', logging.getLogger('cookie-tests'), **kwargs)

    def test_cookie_fallback_is_per_stream_and_runs_only_once(self):
        session = self.session()
        operation = Mock(side_effect=[DownloadError('Sign in to confirm you’re not a bot'), 'live', 'live'])
        self.assertEqual(session.run(operation), 'live')
        self.assertEqual(session.run(operation), 'live')
        self.assertEqual([call.args[0] for call in operation.call_args_list],
                         [None, 'example-cookies.txt', 'example-cookies.txt'])
        another_stream = Mock(return_value='live')
        self.session().run(another_stream)
        another_stream.assert_called_once_with(None)

    def test_original_authentication_error_survives_dependency_wrapping(self):
        for message in ('Sign in to confirm your age', 'This video is available to members-only',
                        'Join this channel to get access', "Confirm you're not a robot"):
            with self.subTest(message=message):
                try:
                    try:
                        raise DownloadError(message)
                    except DownloadError:
                        raise PermissionError('Video abcdefghijk is private')
                except PermissionError as error:
                    operation = Mock(side_effect=[error, 'live'])
                    self.assertEqual(self.session().run(operation), 'live')
                    self.assertEqual(operation.call_count, 2)

    def test_wrapped_unrelated_error_does_not_enable_cookies(self):
        try:
            try:
                raise DownloadError('HTTP Error 429: Too Many Requests')
            except DownloadError:
                raise ConnectionRefusedError('Rate limited or blocked by YouTube anti-bot measures')
        except ConnectionRefusedError as error:
            operation = Mock(side_effect=error)
            with self.assertRaises(ConnectionRefusedError):
                self.session().run(operation)
            operation.assert_called_once_with(None)

    def test_missing_cookies_members_and_cancellation_do_not_loop(self):
        for session, first_cookies in (
                (CookieSession(None, logging.getLogger('cookie-tests')), None),
                (self.session(members_only=True), 'example-cookies.txt'),
                (self.session(cancelled=lambda: True), None)):
            with self.subTest(session=session):
                operation = Mock(side_effect=DownloadError('Sign in to confirm your age'))
                with self.assertRaises(DownloadError):
                    session.run(operation)
                operation.assert_called_once_with(first_cookies)


class DiscoveryCookieTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        modules = {name: ModuleType(name) for name in (
            'livestream_dl', 'livestream_dl.YoutubeURL', 'livestream_dl.download_Live', 'discord_web')}
        modules['livestream_dl.YoutubeURL'].quality_aliases = {}
        modules['livestream_dl.YoutubeURL'].YTDLPLogger = Mock()
        modules['livestream_dl.download_Live'].setup_logging = Mock(return_value=logging.getLogger('discovery-cookie-tests'))
        modules['livestream_dl.download_Live'].LiveStreamDownloader = Mock(side_effect=lambda **kwargs: Mock(stats={}))
        modules['livestream_dl.download_Live'].FileInfo = Mock()
        modules['livestream_dl'].getUrls = SimpleNamespace(
            get_Video_Info=Mock(), VideoInaccessibleError=PermissionError, VideoProcessedError=ValueError)
        modules['discord_web'].main = Mock()

        def load(name, filename):
            spec = importlib.util.spec_from_file_location(name, ROOT / filename)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        with patch.dict(sys.modules, modules), patch.object(signal, 'signal'):
            cls.config_module = load('cookie_config_under_test', 'getConfig.py')
            modules['getConfig'] = cls.config_module
            with patch.dict(sys.modules, modules):
                cls.common = load('cookie_common_under_test', 'common.py')
            modules['common'] = cls.common
            with patch.dict(sys.modules, modules):
                cls.members = load('cookie_members_under_test', 'getMembers.py')
                cls.chat = load('cookie_chat_under_test', 'getChatOnly.py')
            modules['getChatOnly'] = cls.chat
            with patch.dict(sys.modules, modules):
                cls.recovery = load('cookie_recovery_under_test', 'unarchived.py')

    def setUp(self):
        self.common.kill_all.clear()
        self.config = self.config_module.ConfigHandler(config={
            'download_options': {'cookies_file': 'example-cookies.txt'},
            'members_only': {'Example channel': 'UC' + 'a' * 22},
        })

    def extraction(self, responses):
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=SimpleNamespace(extract_info=Mock(side_effect=responses)))
        factory.return_value.__exit__ = Mock(return_value=False)
        return factory

    def test_public_discovery_uses_no_cookies_and_members_use_them_immediately(self):
        video = {'id': 'abcdefghijk', 'title': 'Live', 'live_status': 'is_live'}
        for tab, expected in (('streams', None), ('membership', 'example-cookies.txt')):
            with self.subTest(tab=tab):
                factory = self.extraction([{'entries': [video]}])
                with patch.object(self.common, 'YoutubeDL', factory):
                    self.assertEqual(self.common.get_upcoming_or_live_videos('UC' + 'a' * 22, self.config, tab), ['abcdefghijk'])
                self.assertEqual(factory.call_args.args[0]['cookiefile'], expected)

    def test_public_playlist_authentication_fallback_does_not_affect_individual_streams(self):
        video = {'id': 'abcdefghijk', 'url': 'https://example.com/video', 'title': 'Live'}
        factory = self.extraction([
            DownloadError("Sign in to confirm you're not a bot"), {'entries': [video]},
            {**video, 'live_status': 'is_live'},
        ])
        with patch.object(self.common, 'YoutubeDL', factory):
            self.assertEqual(self.common.get_upcoming_or_live_videos('UC' + 'a' * 22, self.config, 'streams'), ['abcdefghijk'])
        self.assertEqual([call.args[0]['cookiefile'] for call in factory.call_args_list],
                         [None, 'example-cookies.txt', None])

    def test_individual_public_stream_authentication_fallback_does_not_affect_next_stream(self):
        first = {'id': 'abcdefghijk', 'url': 'https://example.com/first', 'title': 'First'}
        second = {'id': 'lmnopqrstuv', 'url': 'https://example.com/second', 'title': 'Second'}
        factory = self.extraction([
            {'entries': [first, second]}, DownloadError('Sign in to confirm your age'),
            {**first, 'live_status': 'is_live'}, {**second, 'live_status': 'is_live'},
        ])
        with patch.object(self.common, 'YoutubeDL', factory):
            self.assertEqual(set(self.common.get_upcoming_or_live_videos('UC' + 'a' * 22, self.config, 'streams')),
                             {'abcdefghijk', 'lmnopqrstuv'})
        self.assertEqual([call.args[0]['cookiefile'] for call in factory.call_args_list],
                         [None, None, 'example-cookies.txt', None])

    def test_member_discovery_keeps_the_flag_in_queued_and_returned_jobs(self):
        with patch.object(self.common, 'get_upcoming_or_live_videos', return_value=['abcdefghijk']), \
                patch.object(self.members, 'sleep'), \
                patch.object(self.common, 'vid_executor', side_effect=lambda **kwargs: kwargs['streams']):
            expected = {'id': 'abcdefghijk', 'channel_id': 'UC' + 'a' * 22, 'members_only': True}
            self.assertEqual(self.members.main(config=self.config, return_dict=True), [expected])
            pending = queue.Queue()
            self.members.main(config=self.config, queue=pending, return_dict=True)
            self.assertEqual(pending.get_nowait(), expected)

    def test_spawned_members_recording_gets_the_cookie_flag(self):
        with patch.object(self.common, 'Popen') as spawn:
            self.common.vid_executor(['abcdefghijk'], 'spawn', self.config, members_only=True)
        self.assertEqual(spawn.call_args.args[0][-3:], ['--members-only', '--', 'abcdefghijk'])

    def test_recording_options_use_cookies_only_for_members(self):
        for availability, cookies in (('public', None), ('subscriber_only', 'example-cookies.txt')):
            with self.subTest(availability=availability):
                options = self.config.get_livestream_dl_options(
                    {'id': 'abcdefghijk', 'availability': availability}, 'stream.mp4')
                self.assertEqual(options['cookies'], cookies)

    def test_unarchived_monitoring_starts_without_cookies_and_keeps_its_authentication_retry(self):
        with patch.object(self.recovery.httpx, 'get', return_value=SimpleNamespace(status_code=404)):
            monitor = self.recovery.UnarchivedDownloader('abcdefghijk', config=self.config,
                                                        logger=logging.getLogger('recovery-cookie-tests'))
        upcoming = {'id': 'abcdefghijk', 'live_status': 'is_upcoming'}
        with patch.object(self.recovery.getUrls, 'get_Video_Info', side_effect=[
                DownloadError("Sign in to confirm you're not a bot"),
                (upcoming, 'is_upcoming'), (upcoming, 'is_upcoming')]) as extract:
            monitor.is_video_private(monitor.id)
            monitor.is_video_private(monitor.id)
        self.assertEqual([call.kwargs['cookies'] for call in extract.call_args_list],
                         [None, 'example-cookies.txt', 'example-cookies.txt'])
        with patch.object(self.recovery, 'ChatOnlyDownloader') as chat:
            monitor._run_chat_thread('example.info.json', 'example.live_chat.zip')
        self.assertIs(chat.call_args.kwargs['cookie_session'], monitor.cookie_session)

    def test_chat_uses_member_or_shared_cookie_choice_and_can_retry_authentication(self):
        for availability, shared_cookies, retry in (
                ('public', False, False), ('subscriber_only', False, False),
                ('public', True, False), ('public', False, True)):
            with self.subTest(availability=availability, shared_cookies=shared_cookies, retry=retry), \
                    tempfile.TemporaryDirectory() as directory:
                info_path = Path(directory) / 'example.info.json'
                info_path.write_text(json.dumps({'id': 'abcdefghijk', 'availability': availability}))
                session = CookieSession('example-cookies.txt', logging.getLogger('chat-cookie-tests'),
                                        members_only=shared_cookies)
                chat = self.chat.ChatOnlyDownloader(str(info_path), output_path=str(Path(directory) / 'chat'),
                                                   config=self.config, logger=session.logger, cookie_session=session)
                attempts = []

                def download(**kwargs):
                    attempts.append(kwargs['options']['cookies'])
                    if retry and len(attempts) == 1:
                        raise DownloadError('Sign in to confirm your age')

                chat.downloader.download_live_chat.side_effect = download
                chat.main()
                expected = ([None, 'example-cookies.txt'] if retry else
                            ['example-cookies.txt' if shared_cookies or availability == 'subscriber_only' else None])
                self.assertEqual(attempts, expected)


if __name__ == '__main__':
    unittest.main()
