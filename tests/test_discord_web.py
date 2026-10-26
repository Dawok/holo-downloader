import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class DiscordWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        modules = {name: ModuleType(name) for name in ('common', 'getConfig')}
        modules['common'].initialize_logging = Mock()
        modules['getConfig'].ConfigHandler = Mock()
        path = Path(__file__).resolve().parents[1] / 'discord_web.py'
        spec = importlib.util.spec_from_file_location('discord_web_under_test', path)
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(cls.module)

    def send(self, info=None, oembed=None):
        """Sends a waiting webhook and returns the embed passed to Discord."""
        webhook = Mock()
        response = SimpleNamespace(status_code=200, json=lambda: dict(oembed or {}))
        with patch.object(self.module, 'DiscordWebhook', return_value=webhook), \
                patch.object(self.module.httpx, 'get', return_value=response) as get:
            self.module.send_webhook('https://discord.example/webhook', 'abcdefghijk', 'waiting',
                                     config=Mock(), logger=Mock(), info=info)
        webhook.execute.assert_called_once()
        return webhook.add_embed.call_args.args[0], get

    def test_uses_yt_dlp_details_without_requesting_oembed(self):
        embed, get = self.send(info={
            'title': 'Stream 2026-10-26 20:00', 'fulltitle': 'Stream',
            'channel': 'Example Ch.', 'channel_url': 'https://www.youtube.com/channel/UCexample',
            'thumbnail': 'https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg'})
        get.assert_not_called()
        self.assertEqual(embed.description, '[Stream](https://youtu.be/abcdefghijk)')
        self.assertEqual(embed.author['name'], 'Example Ch.')
        self.assertEqual(embed.author['url'], 'https://www.youtube.com/channel/UCexample')
        self.assertEqual(embed.thumbnail['url'], 'https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg')

    def test_upcoming_oembed_title_comes_from_the_player_html(self):
        embed, _ = self.send(oembed={
            'type': 'video', 'version': '1.0', 'provider_name': 'YouTube',
            'html': '<iframe width="200" title="Tom &amp; Jerry &quot;live&quot;" src="x"></iframe>'})
        self.assertEqual(embed.description, '[Tom & Jerry "live"](https://youtu.be/abcdefghijk)')
        self.assertIsNone(embed.author)
        self.assertIsNone(embed.thumbnail)

    def test_missing_title_shows_the_plain_link_instead_of_none(self):
        embed, _ = self.send(oembed={'type': 'video', 'version': '1.0'})
        self.assertEqual(embed.description, 'https://youtu.be/abcdefghijk')
        self.assertNotIn('None', embed.description)


if __name__ == '__main__':
    unittest.main()
