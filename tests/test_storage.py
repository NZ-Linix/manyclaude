import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import storage
import openusage

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('mc_storage_test', str(ROOT / 'manyclaude'))
spec = importlib.util.spec_from_loader(loader.name, loader)
mc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mc
loader.exec_module(mc)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.base = self.home / 'manager'
        self.old = self.base / 'profiles/work'
        (self.old / 'code').mkdir(parents=True)
        (self.old / 'desktop').mkdir()
        (self.old / 'code/.credentials.json').write_text('{"fixture":"login"}')
        (self.old / 'code/history.jsonl').write_text('fixture history\n')
        (self.old / 'desktop/Cookies').write_bytes(b'fixture desktop login')
        (self.old / 'launcher.log').write_text('fixture log')
        self.profile = {'name': 'Work', 'code': str(self.old / 'code'), 'desktop': str(self.old / 'desktop'),
                        'app': str(self.home / 'Work.app'), 'code_args': ['--model', 'sonnet']}
        self.addCleanup(patch.stopall)
        patch.object(Path, 'home', return_value=self.home).start()
        patch.object(mc, 'BASE', self.base).start()
        self.running = patch.object(mc, 'running', return_value=False).start()
        self.build = patch.object(mc, 'build_app').start()
        self.copy_login = patch.object(openusage, 'copy_keychain_store').start()
        self.delete_login = patch.object(openusage, 'delete_keychain_store').start()
        mc.save_profiles({'work': self.profile})

    def test_both_platforms_preserve_account_data_and_repeat_safely(self):
        for linux in (False, True):
            # Use a fresh destination/source for each platform.
            if linux:
                self.old.parent.mkdir(parents=True, exist_ok=True)
                (self.home / '.manyclaude-storage-backup/work').rename(self.old)
                import shutil
                shutil.rmtree(self.home / '.claude-work')
                mc.save_profiles({'work': self.profile})
            with patch.object(mc, 'LINUX', linux):
                storage.migrate(mc)
                storage.migrate(mc)
            target = self.home / '.claude-work'
            self.assertEqual((target / '.credentials.json').read_text(), '{"fixture":"login"}')
            self.assertEqual((target / 'history.jsonl').read_text(), 'fixture history\n')
            self.assertEqual((target / 'desktop/Cookies').read_bytes(), b'fixture desktop login')
            self.assertEqual((target / 'launcher.log').read_text(), 'fixture log')
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            self.assertFalse(self.old.exists())
            result = mc.read_profiles()['work']
            self.assertEqual(result['code'], str(target))
            self.assertEqual(result['desktop'], str(target / 'desktop'))
            self.assertEqual(result['code_args'], ['--model', 'sonnet'])
        self.copy_login.assert_called_once_with(str(self.old / 'code'), str(target))
        self.delete_login.assert_called_once_with(str(self.old / 'code'))

    def test_linked_openusage_keeps_service_and_retargets_link(self):
        session = self.home / '.claude-swap-backup/sessions/1-fixture'
        session.parent.mkdir(parents=True)
        session.symlink_to(self.old / 'code')
        self.profile['code_session'] = str(session)
        mc.save_profiles({'work': self.profile})
        with patch.object(mc, 'LINUX', False):
            storage.migrate(mc)
        self.assertEqual(session.readlink(), self.home / '.claude-work')
        self.assertEqual(mc.read_profiles()['work']['code_session'], str(session))
        self.assertTrue((session / '.credentials.json').exists())
        self.copy_login.assert_not_called()
        self.delete_login.assert_not_called()

    def test_preflight_and_collision_never_change_original(self):
        storage.migrate(mc, check=True)
        self.assertTrue(self.old.exists())
        self.assertFalse((self.base / 'storage-migrations.json').exists())
        target = self.home / '.claude-work'
        target.mkdir()
        (target / 'keep').write_text('unrelated')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            storage.migrate(mc)
        self.assertTrue(self.old.exists())
        self.assertEqual((target / 'keep').read_text(), 'unrelated')

    def test_active_desktop_or_code_defers_migration(self):
        self.running.return_value = True
        with self.assertRaisesRegex(ValueError, 'Quit'):
            storage.migrate(mc)
        self.running.return_value = False
        sessions = self.old / 'code/sessions'
        sessions.mkdir()
        (sessions / '123.json').write_text('{}')
        from types import SimpleNamespace
        with patch.object(storage.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='claude')):
            with self.assertRaisesRegex(ValueError, 'Code sessions'):
                storage.migrate(mc)
        self.assertFalse((self.home / '.claude-work').exists())

    def test_resume_after_registry_or_launcher_failure_retains_original(self):
        with patch.object(mc, 'LINUX', True):
            with patch.object(mc, 'save_profiles', side_effect=OSError('fixture interruption')):
                with self.assertRaises(OSError):
                    storage.migrate(mc)
            self.assertTrue(self.old.exists())
            self.assertEqual(mc.read_profiles()['work']['code'], str(self.old / 'code'))
            self.build.side_effect = OSError('fixture build failure')
            with self.assertRaises(OSError):
                storage.migrate(mc)
            self.assertTrue(self.old.exists())
            self.build.side_effect = None
            storage.migrate(mc)
            storage.migrate(mc)
        self.assertFalse(self.old.exists())
        self.assertFalse((self.base / 'storage-migrations.json').exists())
        self.assertTrue((self.home / '.manyclaude-storage-backup/work/code/history.jsonl').exists())

    def test_regular_profile_unchanged_and_retained_orphan_migrated(self):
        regular = {'name': 'Main', 'regular': True, 'code': str(self.home / '.claude'), 'desktop': 'regular'}
        mc.save_profiles({'main': regular})
        with patch.object(mc, 'LINUX', True):
            storage.migrate(mc)
        self.assertEqual(mc.read_profiles(), {'main': regular})
        self.assertTrue((self.home / '.claude-work/history.jsonl').exists())
        self.build.assert_not_called()

    def test_different_home_and_redirected_directories_rejected(self):
        mc.save_profiles({'main': {'regular': True, 'code': '/different/home/.claude'}, 'work': self.profile})
        with self.assertRaisesRegex(ValueError, 'different home'):
            storage.migrate(mc)
        mc.save_profiles({'work': self.profile})
        self.old.rename(self.home / 'elsewhere')
        self.old.symlink_to(self.home / 'elsewhere')
        with self.assertRaisesRegex(ValueError, 'redirected'):
            storage.migrate(mc)
        self.assertTrue((self.home / 'elsewhere/code/history.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
