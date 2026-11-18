import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_manyclaude import mc


class ProfileConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='manyclaude-config-test-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.base = self.home / 'manager'
        self.work = self.home / '.claude-work'
        self.main = self.home / '.claude'
        self.desktop = self.home / 'main-desktop'
        home = patch.object(Path, 'home', return_value=self.home)
        base = patch.object(mc, 'BASE', self.base)
        home.start()
        base.start()
        self.addCleanup(home.stop)
        self.addCleanup(base.stop)
        self.profiles = {
            'work': {'name': 'Work', 'code': str(self.work), 'desktop': str(self.work / 'desktop')},
            'main': {'name': 'Main', 'regular': True, 'code': str(self.main), 'desktop': str(self.desktop)},
        }
        mc.save_profiles(self.profiles)

    def write(self, path, data):
        mc.atomic_write(path, json.dumps(data))

    def test_merges_config_without_copying_private_account_state(self):
        main_state = self.home / '.claude.json'
        work_state = self.work / '.config.json'
        self.write(main_state, {'oauthAccount': {'id': 'main'}, 'mcpServers': {'main': {'command': 'first'}},
                                'projects': {'/work': {'hasTrustDialogAccepted': True,
                                                      'mcpServers': {'local': {'command': 'local'}}}}})
        self.write(work_state, {'oauthAccount': {'id': 'work'}, 'mcpServers': {'work': {'command': 'second'}}})
        self.write(self.main / 'settings.json', {'model': 'main', 'env': {'ANTHROPIC_API_KEY': 'private'}})
        self.write(self.work / 'settings.json', {'model': 'work', 'theme': 'dark'})
        self.write(self.desktop / 'claude_desktop_config.json',
                   {'mcpServers': {'desktop': {'command': 'third'}},
                    'preferences': {'quickEntryShortcut': 'a', 'bypassPermissionsOptInByAccount': {'main': True}}})
        (self.main / 'history.jsonl').write_text('private')
        mc.atomic_write(self.work / 'hooks/hook.sh', '#!/bin/sh\n', 0o700)
        mc.atomic_write(self.work / 'skills/custom/SKILL.md', 'local skill')
        mc.atomic_write(self.work / 'skills/synced/private/SKILL.md', 'account skill')

        mc.main(['sync-config', '--enable'])
        self.assertEqual(json.loads((self.work / 'settings.json').read_text()), {'model': 'main', 'theme': 'dark'})
        self.assertEqual(json.loads((self.main / 'settings.json').read_text())['env']['ANTHROPIC_API_KEY'], 'private')
        merged = json.loads(work_state.read_text())
        self.assertEqual(merged['oauthAccount'], {'id': 'work'})
        self.assertEqual(set(merged['mcpServers']), {'main', 'work'})
        self.assertEqual(merged['projects']['/work'], {'mcpServers': {'local': {'command': 'local'}}})
        desktop = json.loads((self.work / 'desktop/claude_desktop_config.json').read_text())
        self.assertEqual(desktop['mcpServers'], {'desktop': {'command': 'third'}})
        self.assertEqual(desktop['preferences'], {'quickEntryShortcut': 'a'})
        self.assertFalse((self.work / 'history.jsonl').exists())
        self.assertEqual((self.main / 'hooks/hook.sh').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.main / 'skills/custom/SKILL.md').read_text(), 'local skill')
        self.assertFalse((self.main / 'skills/synced').exists())
        self.assertFalse((self.work / '.claude.json').exists())

    def test_launch_propagates_edits_and_deletions_and_disable_stops_syncing(self):
        self.write(self.main / 'settings.json', {'model': 'sonnet', 'theme': 'dark'})
        mc.main(['sync-config', '--enable'])
        self.write(self.work / 'settings.json', {'model': 'opus'})
        mc.main(['_prepare-skills', 'work'])
        self.assertEqual(json.loads((self.main / 'settings.json').read_text()), {'model': 'opus'})
        mc.main(['sync-config', '--disable'])
        self.write(self.work / 'settings.json', {'model': 'haiku'})
        mc.main(['_prepare-skills', 'work'])
        self.assertEqual(json.loads((self.main / 'settings.json').read_text()), {'model': 'opus'})

    def test_reset_rejoins_without_propagating_deletions(self):
        self.write(self.main / 'settings.json', {'model': 'sonnet'})
        mc.main(['sync-config', '--enable'])
        with patch.object(mc, 'LINUX', True), patch.object(mc, 'running', return_value=False):
            mc.reset_data('work')
        self.assertFalse((self.work / 'settings.json').exists())
        mc.main(['sync-config', '--now'])
        self.assertEqual(json.loads((self.work / 'settings.json').read_text()), {'model': 'sonnet'})
        self.assertEqual(json.loads((self.main / 'settings.json').read_text()), {'model': 'sonnet'})

    def test_invalid_configuration_aborts_before_writing(self):
        self.write(self.main / 'settings.json', {'model': 'sonnet'})
        mc.atomic_write(self.work / '.claude.json', '{')
        with self.assertRaisesRegex(ValueError, 'Could not read configuration JSON'):
            mc.main(['sync-config', '--enable'])
        self.assertFalse((self.work / 'settings.json').exists())
