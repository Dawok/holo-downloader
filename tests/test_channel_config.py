from pathlib import Path
import tempfile
import unittest

import tomlkit

from channel_config import (ConfigConflict, canonical_id, channel_url,
                            channels_from_config, delete_channel, read_config,
                            save_channel, write_config)


CHANNEL_ID = 'UC' + 'a' * 22
SECOND_ID = 'UC' + 'b' * 22


class ChannelConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'config.toml'
        self.path.write_text('[download_options]\nquality = "best"\n', encoding='utf-8')
        self.channel = dict(id=CHANNEL_ID, name='Example channel', public=True,
                            members=True, unarchived=True, community=True,
                            title_regex=r'(?i)(karaoke|歌枠)', description_regex='',
                            output_template='%(channel)s/%(title)s')

    def save(self, data=None, editing=False):
        return save_channel(self.path, data or self.channel, read_config(self.path)[1], editing=editing)

    def test_save_synchronizes_all_tables(self):
        self.save()
        doc, _ = read_config(self.path)
        for table in ('channel_ids_to_match', 'members_only', 'unarchived_channel_ids_to_match', 'community_tab'):
            self.assertEqual(doc[table]['Example channel'], CHANNEL_ID)
        self.assertEqual(doc['title_filter'][CHANNEL_ID], self.channel['title_regex'])
        self.assertEqual(doc['per_channel_output_template'][CHANNEL_ID], self.channel['output_template'])
        self.assertEqual(channels_from_config(doc), [self.channel])

    def test_rename_and_disable_remove_old_entries(self):
        self.save()
        updated = {**self.channel, 'name': 'Renamed channel', 'members': False,
                   'community': False, 'title_regex': '', 'output_template': ''}
        self.save(updated, editing=True)
        doc, _ = read_config(self.path)
        self.assertEqual(dict(doc['channel_ids_to_match']), {'Renamed channel': CHANNEL_ID})
        self.assertEqual(dict(doc['unarchived_channel_ids_to_match']), {'Renamed channel': CHANNEL_ID})
        self.assertEqual(dict(doc['members_only']), {})
        self.assertEqual(dict(doc['community_tab']), {})
        self.assertNotIn(CHANNEL_ID, doc['title_filter'])
        self.assertNotIn(CHANNEL_ID, doc['per_channel_output_template'])

    def test_preserves_unrelated_settings_comments_and_inode(self):
        original = '# Keep this setting\n[download_options]\nquality = "best" # unchanged\n\n[webhook]\nurl = "https://example.com/hook"\n'
        self.path.write_text(original)
        inode = self.path.stat().st_ino
        self.save()
        self.assertTrue(self.path.read_text().startswith(original))
        self.assertEqual(self.path.stat().st_ino, inode)

    def test_stale_save_leaves_file_unchanged(self):
        stale_revision = read_config(self.path)[1]
        self.save()
        before = self.path.read_bytes()
        with self.assertRaises(ConfigConflict):
            save_channel(self.path, {**self.channel, 'name': 'Stale'}, stale_revision, editing=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_stale_delete_leaves_file_unchanged(self):
        self.save()
        stale_revision = read_config(self.path)[1]
        self.save({**self.channel, 'name': 'New name'}, editing=True)
        before = self.path.read_bytes()
        with self.assertRaises(ConfigConflict):
            delete_channel(self.path, CHANNEL_ID, stale_revision)
        self.assertEqual(self.path.read_bytes(), before)

    def test_rejects_invalid_regex_without_writing(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Title filter'):
            self.save({**self.channel, 'title_regex': '[invalid'})
        self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_id_and_name_are_rejected(self):
        self.save()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'already added'):
            self.save()
        with self.assertRaisesRegex(ValueError, 'another channel'):
            self.save({**self.channel, 'id': SECOND_ID, 'name': 'EXAMPLE CHANNEL'})
        self.assertEqual(self.path.read_bytes(), before)

    def test_delete_cleans_all_channel_rules_but_keeps_others(self):
        self.save()
        self.save({**self.channel, 'id': SECOND_ID, 'name': 'Another channel'})
        delete_channel(self.path, CHANNEL_ID, read_config(self.path)[1])
        doc, _ = read_config(self.path)
        self.assertEqual([channel['id'] for channel in channels_from_config(doc)], [SECOND_ID])
        self.assertEqual(doc['download_options']['quality'], 'best')

    def test_legacy_playlist_ids_aggregate_and_survive_edit(self):
        doc = tomlkit.document()
        doc['channel_ids_to_match'] = {'Public alias': CHANNEL_ID}
        doc['unarchived_channel_ids_to_match'] = {'Monitor alias': 'UU' + 'a' * 22}
        doc['members_only'] = {'Member alias': 'UUMO' + 'a' * 22}
        doc['title_filter'] = {CHANNEL_ID: r'(?i)karaoke'}
        self.path.write_text(tomlkit.dumps(doc))
        channels = channels_from_config(doc)
        self.assertEqual(len(channels), 1)
        self.assertTrue(channels[0]['members'])
        self.assertTrue(channels[0]['unarchived'])
        self.save({**channels[0], 'name': 'Unified name'}, editing=True)
        doc, _ = read_config(self.path)
        self.assertEqual(dict(doc['members_only']), {'Unified name': 'UUMO' + 'a' * 22})
        self.assertEqual(dict(doc['unarchived_channel_ids_to_match']), {'Unified name': 'UU' + 'a' * 22})

    def test_filter_only_channels_remain_visible(self):
        doc = tomlkit.parse(f'[title_filter]\n{CHANNEL_ID} = "karaoke"\n')
        channels = channels_from_config(doc)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]['id'], CHANNEL_ID)
        self.assertFalse(channels[0]['public'])

    def test_windows_line_endings_can_be_saved(self):
        self.path.write_bytes(b'[download_options]\r\nquality = "best"\r\n')
        self.save()
        self.assertEqual(read_config(self.path)[0]['channel_ids_to_match']['Example channel'], CHANNEL_ID)

    def test_invalid_raw_toml_does_not_replace_config(self):
        before = self.path.read_bytes()
        with self.assertRaises(Exception):
            write_config(self.path, '[invalid', read_config(self.path)[1])
        self.assertEqual(self.path.read_bytes(), before)

    def test_validates_identity_and_archive_modes(self):
        for data in ({**self.channel, 'id': 'not-a-channel'}, {**self.channel, 'name': ''},
                     {**self.channel, 'public': False, 'members': False, 'unarchived': False, 'community': False}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.save(data)

    def test_channel_lookup_accepts_only_channel_sources(self):
        self.assertEqual(channel_url('@example'), 'https://www.youtube.com/@example')
        self.assertEqual(channel_url('youtube.com/@example/streams'), 'https://www.youtube.com/@example')
        self.assertEqual(channel_url(CHANNEL_ID), f'https://www.youtube.com/channel/{CHANNEL_ID}')
        self.assertEqual(canonical_id('UUMO' + 'a' * 22), CHANNEL_ID)
        for source in ('https://example.com/channel/name', 'https://youtube.com.evil.test/@name',
                       'https://www.youtube.com/watch?v=abcdefghijk', 'http://127.0.0.1/',
                       'https://example.com@youtube.com/@name', 'javascript:alert(1)'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                channel_url(source)


if __name__ == '__main__':
    unittest.main()
