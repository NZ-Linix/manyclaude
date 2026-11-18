import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('manyclaude_commands', str(ROOT / 'manyclaude'))
spec = importlib.util.spec_from_loader(loader.name, loader)
mc = importlib.util.module_from_spec(spec)
loader.exec_module(mc)


class CommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.root = self.home / "tool's $directory"
        self.root.mkdir()
        self.bin = self.home / 'global bin'
        (self.root / 'command-bin-dir').write_text(str(self.bin) + '\n')
        (self.root / 'manyclaude').write_text(
            'import json, os, sys\n'
            'print(json.dumps({"args": sys.argv[1:], "feature": os.getenv("FEATURE"), '
            '"home": os.getenv("MANYCLAUDE_HOME")}))\n'
            'sys.exit(int(os.getenv("FIXTURE_EXIT", "0")))\n')
        self.addCleanup(patch.stopall)
        patch.object(mc, 'ROOT', self.root).start()
        patch.object(mc, 'BASE', self.home / "profile's home").start()
        patch.object(Path, 'home', return_value=self.home).start()
        self.profiles = {'work': {'name': 'Work', 'alias': 'clawde', 'rc': str(self.home / '.zshrc')}}

    def test_both_executables_forward_environment_arguments_and_exit_status(self):
        mc.sync_commands(self.profiles)
        arguments = ['--model', 'sonnet', 'prompt with spaces', '', "quote'", '$HOME; $(touch bad)']
        for executable in (mc.profile_binary('work'), self.bin / 'clawde'):
            result = subprocess.run([str(executable), *arguments], capture_output=True, text=True,
                                    env={**os.environ, 'FEATURE': 'caller', 'FIXTURE_EXIT': '7',
                                         'MANYCLAUDE_HOME': '/wrong'})
            self.assertEqual(result.returncode, 7, result.stderr)
            record = json.loads(result.stdout)
            self.assertEqual(record['args'], ['code', 'work', *arguments])
            self.assertEqual(record['feature'], 'caller')
            self.assertEqual(record['home'], str(mc.BASE))
        self.assertFalse((self.home / 'bad').exists())

    def test_migration_rename_and_removal(self):
        rc = self.home / '.zshrc'
        rc.write_text('# keep\n' + mc.START + '\nalias clawde=old\n' + mc.END + '\n')
        mc.sync_aliases(self.profiles)
        self.assertNotIn('alias clawde=', rc.read_text())
        self.assertIn('# keep', rc.read_text())
        self.assertTrue((self.bin / 'clawde').exists())
        unrelated = self.bin / 'keep'
        unrelated.write_text('user data')
        self.profiles['work']['alias'] = 'new-name'
        mc.sync_aliases(self.profiles)
        self.assertFalse((self.bin / 'clawde').exists())
        self.assertTrue((self.bin / 'new-name').exists())
        self.assertTrue(mc.profile_binary('work').exists())
        mc.sync_aliases({}, [rc])
        self.assertFalse((self.bin / 'new-name').exists())
        self.assertFalse(mc.profile_binary('work').exists())
        self.assertEqual(unrelated.read_text(), 'user data')

    def test_conflict_and_dry_run_preserve_files(self):
        self.bin.mkdir()
        target = self.bin / 'clawde'
        target.write_text('unrelated executable')
        with self.assertRaises(ValueError):
            mc.sync_aliases(self.profiles)
        self.assertEqual(target.read_text(), 'unrelated executable')
        self.assertFalse(mc.profile_binary('work').exists())
        self.assertFalse((self.home / '.zshrc').exists())
        target.unlink()
        target.symlink_to(self.home / 'missing')
        with self.assertRaises(ValueError):
            mc.sync_commands(self.profiles)
        target.unlink()
        mc.sync_commands(self.profiles, dry_run=True)
        self.assertFalse(target.exists())
        self.assertFalse(mc.profile_binary('work').exists())

    def test_regular_profile_never_overwrites_claude(self):
        self.bin.mkdir()
        original = self.bin / 'claude'
        original.write_text('original CLI')
        mc.sync_commands({'main': {'regular': True, 'alias': 'claude'}})
        self.assertEqual(original.read_text(), 'original CLI')
        self.assertTrue(mc.profile_binary('main').exists())

    def test_global_executable_uses_the_same_command_lifecycle(self):
        from global_code import configure_launcher
        with patch.object(mc, 'read_profiles', return_value=self.profiles), patch.dict(os.environ, PATH=os.defpath):
            configure_launcher(mc, None, False)
            target = self.bin / 'claude-global'
            result = subprocess.check_output([target, '--model', 'sonnet@work', 'prompt with spaces'], text=True)
            self.assertEqual(json.loads(result)['args'], ['global', '--model', 'sonnet@work', 'prompt with spaces'])
            with self.assertRaisesRegex(ValueError, 'belongs to|already a system'):
                configure_launcher(mc, 'clawde', False)
            self.assertTrue(target.exists())
            configure_launcher(mc, 'cc-global', False)
            self.assertFalse(target.exists())
            self.assertTrue((self.bin / 'cc-global').exists())
            configure_launcher(mc, None, True)
            self.assertFalse((self.bin / 'cc-global').exists())
            self.assertTrue((self.bin / 'clawde').exists())
