import importlib.machinery
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import linux_desktop
import runtime

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('mc_skills_test', str(ROOT / 'manyclaude'))
spec = importlib.util.spec_from_loader(loader.name, loader)
mc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mc
loader.exec_module(mc)


class SharedSkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='manyclaude-skills-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.base = self.home / 'manager'
        self.base.mkdir()
        self.code = self.home / '.claude-work'
        self.code.mkdir()
        self.source = self.home / '.claude/skills'
        self.target = self.code / 'skills'
        self.profile = {'name': 'Work', 'code': str(self.code),
                        'desktop': str(self.code / 'desktop'), 'source': '/fixture/Claude'}
        self.addCleanup(patch.stopall)
        patch.object(Path, 'home', return_value=self.home).start()
        patch.object(mc, 'BASE', self.base).start()

    def skill(self, directory, text='fixture'):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'SKILL.md').write_text(text)
        return directory

    def test_npx_relative_symlinks_updates_and_uninstall(self):
        canonical = self.skill(self.home / '.agents/skills/humanizer')
        self.source.mkdir(parents=True)
        (self.source / 'humanizer').symlink_to('../../.agents/skills/humanizer')
        mc.prepare_skills(self.profile)
        linked = self.target / 'humanizer'
        self.assertEqual((linked / 'SKILL.md').read_text(), 'fixture')
        (canonical / 'SKILL.md').write_text('updated')
        self.assertEqual((linked / 'SKILL.md').read_text(), 'updated')
        self.skill(self.source / 'new-skill')
        mc.prepare_skills(self.profile)
        self.assertTrue((self.target / 'new-skill/SKILL.md').exists())
        (self.source / 'humanizer').unlink()
        mc.prepare_skills(self.profile)
        self.assertFalse(linked.is_symlink())
        self.assertTrue(canonical.exists())
        shutil.rmtree(self.source)
        mc.prepare_skills(self.profile)
        self.assertFalse((self.target / 'new-skill').is_symlink())

    def test_private_skills_synced_data_and_unmanaged_links_are_preserved(self):
        self.skill(self.source / 'custom', 'shared')
        self.skill(self.source / 'synced', 'Main account')
        self.skill(self.source / '.metadata')
        self.skill(self.target / 'custom', 'Work override')
        self.skill(self.target / 'synced', 'Work account')
        (self.target / 'manual-link').symlink_to(self.home / 'missing-user-target')
        mc.prepare_skills(self.profile)
        mc.prepare_skills(self.profile)
        self.assertEqual((self.target / 'custom/SKILL.md').read_text(), 'Work override')
        self.assertEqual((self.target / 'synced/SKILL.md').read_text(), 'Work account')
        self.assertFalse((self.target / '.metadata').exists())
        self.assertTrue((self.target / 'manual-link').is_symlink())
        self.assertEqual((self.source / 'synced/SKILL.md').read_text(), 'Main account')

    def test_cli_uses_live_openusage_session_without_changing_credentials(self):
        self.skill(self.source / 'custom')
        session = self.home / 'session alias'
        session.symlink_to(self.code, target_is_directory=True)
        self.profile['code_session'] = str(session)
        credential = self.code / '.credentials.json'
        credential.write_text('private fixture')
        with patch.object(mc, 'get_profile', return_value=('work', self.profile)), \
                patch.object(runtime, 'claude_command', return_value=('/fixture/claude', {'ANTHROPIC_API_KEY': 'fixture'})), \
                patch.object(mc.os, 'execvpe') as launch:
            mc.code('work', ['--resume'])
        _, args, env = launch.call_args.args
        self.assertEqual(args, ['/fixture/claude', '--resume'])
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(self.code))
        self.assertEqual(env['CLAUDE_SECURESTORAGE_CONFIG_DIR'], str(session))
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertEqual((Path(env['CLAUDE_CONFIG_DIR']) / 'skills/custom/SKILL.md').read_text(), 'fixture')
        self.assertEqual(credential.read_text(), 'private fixture')

    def test_linux_desktop_prepares_skills_before_runtime_starts(self):
        self.skill(self.source / 'after-install')
        with patch.object(linux_desktop, 'source_app', return_value=Path('/fixture/Claude')), \
                patch.object(linux_desktop, 'tool_environment', return_value={'PATH': '/usr/bin'}), \
                patch.object(linux_desktop.shutil, 'which', return_value=None), \
                patch.object(linux_desktop.subprocess, 'run') as launch:
            def verify(args, env, check):
                self.assertEqual((Path(env['CLAUDE_CONFIG_DIR']) / 'skills/after-install/SKILL.md').read_text(), 'fixture')
                self.assertEqual(env['CLAUDE_USER_DATA_DIR'], self.profile['desktop'])
                return SimpleNamespace(returncode=0)
            launch.side_effect = verify
            linux_desktop.launch(mc, 'work', self.profile)
            launch.assert_called_once()

    def test_regular_profile_is_untouched_and_desktop_helper_reads_registry(self):
        mc.prepare_skills({'regular': True})
        self.assertFalse(self.target.exists())
        self.skill(self.source / 'custom')
        mc.save_profiles({'work': self.profile})
        with patch.object(mc, 'ensure_storage') as migration:
            mc.main(['_prepare-skills', 'work'])
            migration.assert_not_called()
        self.assertTrue((self.target / 'custom/SKILL.md').exists())
        with self.assertRaises(ValueError):
            mc.main(['_prepare-skills'])

    def test_removing_profile_data_preserves_shared_skills(self):
        self.skill(self.source / 'custom')
        mc.prepare_skills(self.profile)
        shutil.rmtree(self.code)
        self.assertEqual((self.source / 'custom/SKILL.md').read_text(), 'fixture')
        mc.prepare_skills(self.profile)
        self.assertTrue((self.target / 'custom/SKILL.md').exists())

    def test_redirected_skills_directory_is_not_written_through(self):
        self.skill(self.source / 'custom')
        outside = self.home / 'user-managed'
        outside.mkdir()
        self.target.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'redirected skills folder'):
            mc.prepare_skills(self.profile)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
