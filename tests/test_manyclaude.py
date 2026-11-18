import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import struct
import fcntl
import time
import unittest
from unittest.mock import patch
import runtime

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('manyclaude', str(ROOT / 'manyclaude'))
spec = importlib.util.spec_from_loader(loader.name, loader)
mc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mc
loader.exec_module(mc)


class ShellTests(unittest.TestCase):
    def test_fish_config_uses_xdg_directory(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'SHELL': '/usr/bin/fish', 'XDG_CONFIG_HOME': temp}):
            self.assertEqual(mc.shell_rc(), Path(temp) / 'fish/config.fish')
            os.environ['XDG_CONFIG_HOME'] = 'relative'
            self.assertEqual(mc.shell_rc(), Path.home() / '.config/fish/config.fish')

    def test_fish_user_functions_and_aliases_are_protected(self):
        with tempfile.TemporaryDirectory() as temp:
            rc = Path(temp) / 'config.fish'
            profiles = {'work': {'rc': str(rc)}}
            for text in ('function claude-work\n    echo keep\nend\n', "alias claude-work 'echo keep'\n"):
                rc.write_text(text)
                with self.assertRaises(ValueError):
                    mc.sync_aliases(profiles)
                self.assertEqual(rc.read_text(), text)
            rc.write_text('')
            (rc.parent / 'functions').mkdir()
            (rc.parent / 'functions/claude-work.fish').write_text('function claude-work\nend\n')
            with self.assertRaises(ValueError):
                mc.sync_aliases(profiles)

    def test_import_code_alias_preserves_quoted_arguments_without_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            rc = Path(temp) / '.zshrc'
            rc.write_text("alias cclaude='env \\\n  FEATURE=1 claude --tools \"Read,Write\" --system-prompt-file \"$HOME/file with spaces.md\"'\necho never-executed\n")
            env, arguments = mc.parse_code_alias('cclaude', rc)
            self.assertEqual(env, {'FEATURE': '1'})
            self.assertEqual(arguments, ['--tools', 'Read,Write', '--system-prompt-file',
                                         str(Path.home() / 'file with spaces.md')])
            rc.write_text("alias cclaude='claude $(touch unsafe)'\n")
            with self.assertRaises(ValueError):
                mc.parse_code_alias('cclaude', rc)

    def test_shell_content_preserved_and_block_idempotent(self):
        original = "# user's config\nexport CUSTOM='hello'\n"
        block = mc.START + '\nalias test=true\n' + mc.END + '\n'
        updated = mc.shell_block(original, block)
        self.assertTrue(updated.startswith(original))
        self.assertEqual(mc.shell_block(updated, block), updated)
        self.assertIn("export CUSTOM='hello'", mc.shell_block(updated, ''))
        self.assertNotIn(mc.START, mc.shell_block(updated, ''))

    def test_bad_markers_rejected(self):
        for text in [mc.START, mc.END, mc.END + mc.START, mc.START * 2 + mc.END]:
            with self.assertRaises(ValueError):
                mc.shell_block(text, '')

    def test_names_and_command_injection(self):
        self.assertEqual(mc.slugify('Client Ä'), 'client-a')
        for name in ['', '../Work', 'evil\nname', 'Work:bad', '🍕']:
            with self.assertRaises(ValueError):
                mc.validate_name(name)
        with tempfile.TemporaryDirectory() as temp, patch.object(mc, 'ROOT', Path(temp) / "path's $(touch bad)"), \
                patch.object(mc, 'BASE', Path(temp) / 'manager'), patch.object(Path, 'home', return_value=Path(temp)):
            rc = Path(temp) / '.zshrc'
            profiles = {'work': {'rc': str(rc)}}
            mc.sync_aliases(profiles)
            result = (Path(temp) / '.local/bin/claude-work').read_text()
            self.assertEqual(mc.shlex.split(result.split('exec ', 1)[1])[:-1],
                             ['env', 'MANYCLAUDE_HOME=' + str(mc.BASE), sys.executable, str(mc.ROOT / 'manyclaude'), 'code', 'work'])
            self.assertFalse((Path(temp) / 'bad').exists())


class CodeTrustTests(unittest.TestCase):
    def test_permission_denied_during_trust_still_launches_claude(self):
        for regular in (True, False):
            for operation in ('read', 'backup', 'write'):
                with self.subTest(regular=regular, operation=operation), tempfile.TemporaryDirectory() as temp:
                    home = Path(temp)
                    base = home / 'manager'
                    base.mkdir()
                    directory = home / 'isolated'
                    directory.mkdir()
                    config = home / '.claude.json' if regular else directory / '.claude.json'
                    original = {'oauthAccount': {'fixture': 'preserved'}}
                    config.write_text(json.dumps(original))
                    if operation == 'read':
                        config.chmod(0)
                    target = {'backup': (mc.shutil, 'copy2'), 'write': (mc, 'atomic_write')}.get(operation)
                    profile = {'regular': regular, 'code': str(directory), 'trust_folders': True}
                    error = PermissionError(13, 'Permission denied', str(config))
                    with patch.object(mc, 'BASE', base), patch.object(mc.Path, 'home', return_value=home), \
                            patch.object(mc, 'get_profile', return_value=('main', profile)), \
                            patch.object(runtime, 'claude_command', return_value=('/fake/claude', {})), \
                            patch.object(mc.os, 'execvpe') as launch, \
                            patch.object(mc.sys, 'stderr', new_callable=io.StringIO) as stderr:
                        if target:
                            with patch.object(*target, side_effect=error):
                                mc.code('main', ['--version'])
                        else:
                            try:
                                mc.code('main', ['--version'])
                            finally:
                                config.chmod(0o600)
                        launch.assert_called_once()
                        binary, arguments, env = launch.call_args.args
                        self.assertEqual(binary, '/fake/claude')
                        self.assertEqual(arguments, ['/fake/claude', '--version'])
                        self.assertEqual(env.get('CLAUDE_CONFIG_DIR'), None if regular else str(directory))
                        self.assertIn('could not automatically trust this folder', stderr.getvalue())
                        self.assertIn(str(config), stderr.getvalue())
                    self.assertEqual(json.loads(config.read_text()), original)


@unittest.skipUnless(sys.platform == 'darwin', 'macOS native integration')
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mc.native_launcher()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='manyclaude-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.base = self.home / 'private profiles'
        self.apps = self.home / 'Applications'
        self.rc = self.home / '.zshrc'
        self.rc.write_text('# original\nexport KEEP_ME=yes\n')
        self.source = self.home / 'Fake Claude.app'
        macos = self.source / 'Contents/MacOS'
        macos.mkdir(parents=True)
        (self.source / 'Contents/Resources').mkdir()
        (self.source / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleExecutable': 'Claude', 'CFBundleIdentifier': 'local.manyclaude.fixture',
            'CFBundlePackageType': 'APPL', 'CFBundleIconFile': 'electron.icns'}))
        shutil.copy2('/Applications/Claude.app/Contents/Resources/electron.icns', self.source / 'Contents/Resources/electron.icns')
        self.record = self.home / 'desktop-record.json'
        # No full environment dump: only explicitly selected fixture values.
        fake = f'''#!{sys.executable}
import json, os, sys, time
from pathlib import Path
out = Path({str(self.record)!r})
previous = json.loads(out.read_text())['count'] if out.exists() else 0
out.write_text(json.dumps({{'count': previous + 1, 'args': sys.argv[1:], 'desktop': os.getenv('CLAUDE_USER_DATA_DIR'), 'code': os.getenv('CLAUDE_CONFIG_DIR'), 'secure': os.getenv('CLAUDE_SECURESTORAGE_CONFIG_DIR'), 'key': os.getenv('ANTHROPIC_API_KEY'), 'skills': sorted(p.name for p in (Path(os.environ['CLAUDE_CONFIG_DIR']) / 'skills').iterdir()) if os.getenv('CLAUDE_CONFIG_DIR') else []}}))
time.sleep(2)
'''
        (macos / 'Claude').write_text(fake)
        (macos / 'Claude').chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), SHELL='/bin/zsh', MANYCLAUDE_HOME=str(self.base))
        self.env.pop('ZDOTDIR', None)

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(ROOT / 'manyclaude'), *args], env=self.env, capture_output=True, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def create(self, name='Personal'):
        self.cli('create', name, '--apps-dir', str(self.apps), '--claude-app', str(self.source))
        return self.apps / f'Claude {name}.app'

    def test_create_collision_remove_and_keep_data(self):
        app = self.create()
        info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        self.assertEqual(info['ManyClaudeCode'], str(self.home / '.claude-personal'))
        self.assertEqual(info['ManyClaudeDesktop'], str(self.home / '.claude-personal/desktop'))
        self.assertEqual(info['CFBundleExecutable'], 'ManyClaude')
        self.assertEqual(info['ManyClaudeRuntime'], 'Contents/MacOS/Claude')
        self.assertFalse(info['LSUIElement'])
        self.assertTrue((app / info['ManyClaudeRuntime']).exists())
        self.assertNotEqual(info['CFBundleIdentifier'], 'com.anthropic.claudefordesktop')
        subprocess.run(['/usr/bin/codesign', '--verify', '--deep', str(app)], check=True, capture_output=True)
        self.assertEqual(info['CFBundleIconFile'], 'electron.icns')
        self.assertEqual((app / 'Contents/Resources/electron.icns').read_bytes(),
                         (self.source / 'Contents/Resources/electron.icns').read_bytes())
        self.cli('create', 'personal', '--apps-dir', str(self.apps), '--claude-app', str(self.source), ok=False)
        self.assertTrue((self.home / '.local/bin/claude-personal').is_file())
        self.assertNotIn('alias claude-personal=', self.rc.read_text())
        self.assertIn('KEEP_ME=yes', self.rc.read_text())
        self.cli('remove', 'personal')
        self.assertFalse(app.exists())
        self.assertTrue((self.home / '.claude-personal').exists())
        self.assertNotIn('alias claude-personal', self.rc.read_text())
        self.assertIn('KEEP_ME=yes', self.rc.read_text())

    def test_code_environment_arguments_and_current_binary(self):
        self.create()
        bindir = self.home / 'bin'
        bindir.mkdir()
        binary = bindir / 'claude'
        binary.write_text(f'#!{sys.executable}\nimport json,os,sys\nprint(json.dumps({{"args":sys.argv[1:],"code":os.getenv("CLAUDE_CONFIG_DIR"),"secure":os.getenv("CLAUDE_SECURESTORAGE_CONFIG_DIR"),"key":os.getenv("ANTHROPIC_API_KEY")}}))\n')
        binary.chmod(0o755)
        self.env.update(PATH=str(bindir) + ':' + self.env['PATH'], ANTHROPIC_API_KEY='test-only')
        result = json.loads(self.cli('code', 'personal', '--', '--model', 'sonnet', 'prompt with spaces').stdout)
        self.assertEqual(result['args'], ['--model', 'sonnet', 'prompt with spaces'])
        self.assertEqual(result['code'], result['secure'])
        self.assertEqual(result['code'], str(self.home / '.claude-personal'))
        self.assertIsNone(result['key'])
        binary.write_text('#!/bin/sh\nprintf "updated\\n"\n')
        self.assertEqual(self.cli('code', 'personal', '--version').stdout.strip(), 'updated')

    def test_code_defaults_and_shell_argument_forwarding(self):
        self.create()
        bindir = self.home / 'bin'
        bindir.mkdir()
        binary = bindir / 'claude'
        binary.write_text(f'#!{sys.executable}\nimport json,os,sys\nprint(json.dumps({{"args":sys.argv[1:],"feature":os.getenv("FEATURE"),"code":os.getenv("CLAUDE_CONFIG_DIR")}}))\n')
        binary.chmod(0o755)
        self.env['PATH'] = str(bindir) + ':' + self.env['PATH']
        self.cli('code-options', 'personal', '--env', 'FEATURE=default', '--flags=--tools "Read,Write" --system-prompt "two words"')
        # Run the generated executable through zsh, including caller overrides.
        invocation = 'FEATURE=caller claude-personal --resume "session with spaces"'
        command = f'source {mc.shlex.quote(str(self.rc))}\neval {mc.shlex.quote(invocation)}'
        result = subprocess.run(['/bin/zsh', '-f', '-c', command], env=self.env, capture_output=True, text=True, check=True)
        record = json.loads(result.stdout)
        self.assertEqual(record['feature'], 'caller')
        self.assertEqual(record['args'], ['--tools', 'Read,Write', '--system-prompt', 'two words', '--resume', 'session with spaces'])
        self.assertEqual(record['code'], str(self.home / '.claude-personal'))
        for executable in (self.home / '.local/bin/claude-personal', self.base / 'bin/manyclaude-code-personal'):
            result = subprocess.run([str(executable), '--resume', 'session with spaces', ''],
                                    env={**self.env, 'FEATURE': 'direct'}, capture_output=True, text=True, check=True)
            record = json.loads(result.stdout)
            self.assertEqual(record['feature'], 'direct')
            self.assertEqual(record['args'], ['--tools', 'Read,Write', '--system-prompt', 'two words',
                                             '--resume', 'session with spaces', ''])
            self.assertEqual(record['code'], str(self.home / '.claude-personal'))
        self.cli('code-options', 'personal', '--env', 'CLAUDE_CONFIG_DIR=/wrong', ok=False)
        self.cli('code-options', 'personal', '--clear')
        record = json.loads(self.cli('code', 'personal', '--version').stdout)
        self.assertEqual(record['args'], ['--version'])
        self.assertIsNone(record['feature'])

    def test_openusage_launch_keeps_normal_config_and_shared_credentials(self):
        self.create()
        registry = self.base / 'profiles.json'
        profiles = json.loads(registry.read_text())
        original = Path(profiles['personal']['code'])
        original.mkdir(parents=True, exist_ok=True)
        shared = self.home / '.claude-swap-backup/sessions/2-fixture_example.com'
        shared.parent.mkdir(parents=True)
        shared.symlink_to(original, target_is_directory=True)
        profiles['personal']['code_session'] = str(shared)
        registry.write_text(json.dumps(profiles))
        bindir = self.home / 'bin'
        bindir.mkdir()
        binary = bindir / 'claude'
        binary.write_text(f'#!{sys.executable}\nimport json,os\nprint(json.dumps({{"config":os.getenv("CLAUDE_CONFIG_DIR"),"secure":os.getenv("CLAUDE_SECURESTORAGE_CONFIG_DIR")}}))\n')
        binary.chmod(0o755)
        self.env['PATH'] = str(bindir) + ':' + self.env['PATH']
        result = json.loads(self.cli('code', 'personal').stdout)
        self.assertEqual(result, {'config': str(original), 'secure': str(shared)})
        # Desktop reads the same live path dynamically; no credential copy or data move.
        entry = self.apps / 'Claude Personal.app/Contents/MacOS/ManyClaude'
        subprocess.run([str(entry)], env=self.env, check=True, timeout=5)
        record = json.loads(self.record.read_text())
        self.assertEqual(record['code'], str(original))
        self.assertEqual(record['secure'], str(shared))

    def test_opt_in_folder_trust_records_git_root_and_preserves_config(self):
        self.create()
        config = self.home / '.claude-personal/.claude.json'
        original = {'oauthAccount': {'fixture': 'preserved'}, 'projects': {'/existing': {'keep': True}}}
        config.write_text(json.dumps(original))
        repo = self.home / 'new repo'
        nested = repo / 'nested'
        nested.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        bindir = self.home / 'bin'
        bindir.mkdir()
        binary = bindir / 'claude'
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        self.env['PATH'] = str(bindir) + ':' + self.env['PATH']
        self.cli('code-options', 'personal', '--trust-folders')
        subprocess.run([sys.executable, str(ROOT / 'manyclaude'), 'code', 'personal'],
                       cwd=nested, env=self.env, check=True)
        saved = json.loads(config.read_text())
        self.assertTrue(saved['projects'][str(nested.resolve())]['hasTrustDialogAccepted'])
        self.assertTrue(saved['projects'][str(repo.resolve())]['hasTrustDialogAccepted'])
        self.assertEqual(saved['oauthAccount'], original['oauthAccount'])
        self.assertEqual(saved['projects']['/existing'], original['projects']['/existing'])
        self.assertEqual(json.loads(config.with_name(config.name+'.manyclaude-trust.bak').read_text()), original)
        self.cli('code-options', 'personal', '--ask-trust')
        untouched = self.home / 'different folder'
        untouched.mkdir()
        subprocess.run([sys.executable, str(ROOT / 'manyclaude'), 'code', 'personal'],
                       cwd=untouched, env=self.env, check=True)
        self.assertNotIn(str(untouched.resolve()), json.loads(config.read_text())['projects'])

    def test_external_import_approval_is_independent_and_uses_the_git_root(self):
        self.create()
        config = self.home / '.claude-personal/.config.json'
        config.write_text(json.dumps({'projects': {}, 'theme': 'dark'}))
        repo = self.home / 'repo'
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                        'commit', '-qm', 'Initial', '--allow-empty'], check=True)
        worktree = self.home / 'worktree'
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-q', '--detach', str(worktree)], check=True)
        nested = worktree / 'nested'
        nested.mkdir()
        bindir = self.home / 'bin'
        bindir.mkdir()
        binary = bindir / 'claude'
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        self.env['PATH'] = str(bindir) + ':' + self.env['PATH']
        self.cli('code-options', 'personal', '--allow-external-imports')
        self.assertTrue(json.loads(self.cli('code-options', 'personal').stdout)['allow_external_imports'])
        launch = [sys.executable, str(ROOT / 'manyclaude'), 'code', 'personal']
        subprocess.run(launch, cwd=nested, env=self.env, check=True)
        approved = json.loads(config.read_text())
        self.assertEqual(approved, {'theme': 'dark', 'projects': {str(repo.resolve()): {
            'hasClaudeMdExternalIncludesApproved': True,
            'hasClaudeMdExternalIncludesWarningShown': True,
        }}})
        self.cli('code-options', 'personal', '--ask-external-imports')
        subprocess.run(launch, cwd=self.home, env=self.env, check=True)
        self.assertEqual(json.loads(config.read_text()), approved)

    def test_native_launch_isolation_duplicate_lock_and_source_update(self):
        app = self.create()
        entry = app / 'Contents/MacOS/ManyClaude'
        env = dict(self.env, ANTHROPIC_API_KEY='test-only')
        child = subprocess.Popen([str(entry)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        deadline = time.monotonic() + 5
        while not self.record.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(self.record.exists())
        record = json.loads(self.record.read_text())
        self.assertEqual(record['desktop'], str(self.home / '.claude-personal/desktop'))
        self.assertEqual(record['code'], record['secure'])
        self.assertEqual(record['args'], ['--user-data-dir=' + record['desktop']])
        self.assertIsNone(record['key'])
        self.cli('remove', 'personal', ok=False)
        subprocess.run([str(entry)], env=env, check=True, timeout=5)
        self.assertEqual(json.loads(self.record.read_text())['count'], 1)
        self.assertEqual(child.wait(timeout=5), 0)
        # The next launch runs the replaced upstream executable without rebuilding the app.
        (self.source / 'Contents/MacOS/Claude').write_text('#!/bin/sh\nexit 42\n')
        self.assertEqual(subprocess.run([str(entry)], env=env).returncode, 42)


    def test_protect_unrelated_app(self):
        app = self.apps / 'Claude Personal.app'
        app.mkdir(parents=True)
        marker = app / 'keep.txt'
        marker.write_text('keep')
        self.cli('create', 'Personal', '--apps-dir', str(self.apps), '--claude-app', str(self.source), ok=False)
        self.assertEqual(marker.read_text(), 'keep')

    def test_native_pinned_launcher_picks_up_skills_installed_after_creation(self):
        app = self.create()
        profile = json.loads((self.base / 'profiles.json').read_text())['personal']
        session = self.home / 'live session'
        session.symlink_to(profile['code'], target_is_directory=True)
        profile['code_session'] = str(session)
        (self.base / 'profiles.json').write_text(json.dumps({'personal': profile}))
        canonical = self.home / '.agents/skills/humanizer'
        canonical.mkdir(parents=True)
        (canonical / 'SKILL.md').write_text('fixture custom skill')
        shared = self.home / '.claude/skills'
        shared.mkdir(parents=True)
        (shared / 'humanizer').symlink_to('../../.agents/skills/humanizer')
        (shared / 'synced').mkdir()
        (shared / 'synced/main-account').write_text('private main')
        private = Path(profile['code']) / 'skills/synced'
        private.mkdir()
        (private / 'work-account').write_text('private work')
        # Invoke the bundle entry directly, as Finder/Dock does, without refresh.
        result = subprocess.run([str(app / 'Contents/MacOS/ManyClaude')], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(self.record.read_text())
        self.assertEqual(record['code'], profile['code'])
        self.assertIn('humanizer', record['skills'])
        self.assertEqual((session / 'skills/humanizer/SKILL.md').read_text(), 'fixture custom skill')
        self.assertEqual((private / 'work-account').read_text(), 'private work')
        self.assertFalse((private / 'main-account').exists())

    def test_preserves_native_icon_catalog_and_appearance_metadata(self):
        file = self.source / 'Contents/Info.plist'
        info = plistlib.loads(file.read_bytes())
        info['CFBundleIconName'] = 'Claude'
        file.write_bytes(plistlib.dumps(info))
        catalog = self.source / 'Contents/Resources/Assets.car'
        catalog.write_bytes(b'fixture native icon catalog')
        app = self.create()
        copied = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        self.assertEqual(copied['CFBundleIconName'], 'Claude')
        self.assertEqual(copied['CFBundleIconFile'], 'electron.icns')
        self.assertEqual((app / 'Contents/Resources/Assets.car').read_bytes(), catalog.read_bytes())
        self.assertFalse((app / 'Contents/Resources/Claude.icns').exists())

    def test_custom_app_name_and_refresh(self):
        self.cli('create', 'Work', '--app-name', 'Work', '--apps-dir', str(self.apps), '--claude-app', str(self.source))
        app = self.apps / 'Work.app'
        self.assertTrue(app.exists())
        self.assertFalse((self.apps / 'Claude Work.app').exists())
        for _ in range(2):
            info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
            # Electron requires its internal helper name; the Dock uses CFBundleDisplayName.
            self.assertEqual(info['CFBundleName'], 'Claude')
            self.assertEqual(info['CFBundleDisplayName'], 'Work')
            self.cli('refresh', 'work')
        profile = json.loads((self.base / 'profiles.json').read_text())['work']
        self.assertEqual(profile['name'], 'Work')
        self.assertEqual(profile['alias'], 'claude-work')
        self.assertEqual(str(self.home / '.claude-work'), profile['code'])
        self.cli('remove', 'work')
        self.assertFalse(app.exists())

    def test_custom_alias_rename_and_legacy_profiles(self):
        self.cli('create', 'Work', '--alias', 'cc-work', '--apps-dir', str(self.apps), '--claude-app', str(self.source))
        self.assertTrue((self.home / '.local/bin/cc-work').is_file())
        self.assertNotIn('alias cc-work=', self.rc.read_text())
        before = json.loads((self.base / 'profiles.json').read_text())['work']
        self.cli('alias', 'work', 'work-code')
        after = json.loads((self.base / 'profiles.json').read_text())['work']
        self.assertEqual(after['code'], before['code'])
        self.assertEqual(after['app'], before['app'])
        self.assertNotIn('alias cc-work=', self.rc.read_text())
        self.assertTrue((self.home / '.local/bin/work-code').is_file())
        self.assertNotIn('alias work-code=', self.rc.read_text())
        self.assertIn('work-code', self.cli('list').stdout)
        self.cli('alias', 'work', 'claude', ok=False)
        self.cli('alias', 'work', 'bad;touch-pwn', ok=False)
        self.create()
        self.cli('alias', 'personal', 'work-code', ok=False)
        registry = json.loads((self.base / 'profiles.json').read_text())
        del registry['personal']['alias']
        (self.base / 'profiles.json').write_text(json.dumps(registry))
        self.cli('alias', 'personal', 'my-code')
        self.assertNotIn('alias claude-personal=', self.rc.read_text())
        self.assertTrue((self.home / '.local/bin/my-code').is_file())
        self.assertNotIn('alias my-code=', self.rc.read_text())

    def test_unmanaged_shell_alias_protected(self):
        self.create()
        with self.rc.open('a') as out:
            out.write("alias my-code='echo keep'\n")
        before = self.rc.read_text()
        self.cli('alias', 'personal', 'my-code', ok=False)
        self.assertEqual(self.rc.read_text(), before)
        self.assertNotIn('my-code', self.cli('list').stdout)

    def test_tui_navigation_rename_and_cancel_deletion(self):
        self.create()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 26, 100, 0, 0))
        tui_env = dict(self.env, TERM='xterm-256color', COLORFGBG='15;0')
        tui_env.pop('NO_COLOR', None)
        process = subprocess.Popen([sys.executable, str(ROOT / 'manyclaude')],
                                   env=tui_env, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        self.addCleanup(os.close, master)
        self.addCleanup(lambda: process.poll() is None and process.kill())

        def wait_for(text):
            output = b''
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], .1)
                if ready:
                    output += os.read(master, 65536)
                    if text.encode() in output:
                        return
            self.fail(f'TUI did not show {text!r}: {output!r}')

        wait_for('Your accounts')
        os.write(master, b'\n')
        wait_for('Manage this account')
        os.write(master, b'jj\n')
        wait_for('current command:')
        os.write(master, b'\x15tui-code\n')
        wait_for('Code command changed to tui-code')
        self.assertTrue((self.home / '.local/bin/tui-code').is_file())
        self.assertNotIn('alias tui-code=', self.rc.read_text())
        os.write(master, b'jjjjj\n')
        wait_for('Permanent deletion')
        os.write(master, b'\n')
        wait_for('Type the profile name to confirm')
        os.write(master, b'wrong-name\n')
        wait_for('Nothing deleted.')
        self.assertTrue((self.home / '.claude-personal').exists())
        os.write(master, b'q')
        wait_for('Your accounts')
        os.write(master, b'q')
        output = b''
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], .1)
            if ready:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
        self.assertEqual(process.poll(), 0, output.decode(errors='replace'))

    def test_purge_confirmation_and_data_deletion(self):
        app = self.create()
        data = self.home / '.claude-personal'
        (data / 'history.txt').write_text('fixture history')
        self.cli('remove', 'personal', '--purge', ok=False)
        self.assertTrue(app.exists())
        self.assertTrue(data.exists())
        self.cli('remove', 'personal', '--purge', '--yes')
        self.assertFalse(app.exists())
        self.assertFalse(data.exists())
        self.assertNotIn('claude-personal', self.rc.read_text())
        self.assertEqual(json.loads((self.base / 'profiles.json').read_text()), {})

    def register_regular(self):
        self.cli('create', 'Main', '--regular', '--claude-app', str(self.source))

    def test_regular_profile_uses_original_and_singleton(self):
        original = self.rc.read_text()
        self.register_regular()
        self.assertEqual(self.rc.read_text(), original)
        p = json.loads((self.base / 'profiles.json').read_text())['main']
        self.assertEqual(p['app'], str(self.source))
        self.assertEqual(p['alias'], 'claude')
        self.assertFalse(self.apps.exists())
        self.cli('create', 'Other', '--regular', '--claude-app', str(self.source), ok=False)
        self.cli('alias', 'main', 'other-code', ok=False)
        self.cli('refresh', 'main')
        self.assertIn('regular Claude', self.cli('list').stdout)
        self.cli('remove', 'main')
        self.assertTrue(self.source.exists())

    def test_profile_commands_keep_saved_session_flags_out_of_subcommands(self):
        self.create()
        self.register_regular()
        bindir = self.home / 'bin'
        bindir.mkdir()
        binary = bindir / 'claude'
        binary.write_text(f'#!{sys.executable}\n' + '''
import json, os, sys

if sys.argv[1:] == ['--help']:
    print('Usage: claude [options] [command] [prompt]\\n\\nCommands:\\n'
          '  plugin|plugins   Manage plugins\\n  mcp              Manage servers\\n'
          '  future-command   A command introduced in a new release')
    sys.exit(0)
print(json.dumps({'args': sys.argv[1:], 'feature': os.getenv('FEATURE'),
                  'code': os.getenv('CLAUDE_CONFIG_DIR')}))
sys.exit(7)
''')
        binary.chmod(0o755)
        self.env.update(PATH=str(bindir) + ':' + self.env['PATH'], FEATURE='caller')
        defaults = ['--allowedTools', 'Read', 'Write', '--resume']
        for profile in ('main', 'personal'):
            self.cli('code-options', profile, '--env', 'FEATURE=saved', '--flags=' + mc.shlex.join(defaults))
        for command, directory in (('claude', None), ('claude-personal', str(self.home / '.claude-personal'))):
            for args in (
                ['plugin', 'marketplace', 'add', 'koahlabs/sidequest-plugin', '--json'],
                ['plugins', 'marketplace', 'list', '--help'],
                ['mcp', 'add', 'example', '--', 'server', 'path with spaces', ''],
                ['future-command', '--help'],
                ['daemon', 'status'],
                ['hello'],
            ):
                with self.subTest(command=command, args=args):
                    invocation = mc.shlex.join([command, *args])
                    shell = f'source {mc.shlex.quote(str(self.rc))}\neval {mc.shlex.quote(invocation)}'
                    result = subprocess.run(['/bin/zsh', '-f', '-c', shell], env=self.env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 7, result.stderr)
                    expected = [*defaults, *args] if args == ['hello'] else args
                    self.assertEqual(json.loads(result.stdout),
                                     {'args': expected, 'feature': 'caller', 'code': directory})

    def test_regular_code_keeps_normal_auth_and_clears_profile_pointers(self):
        self.register_regular()
        bindir = self.home / 'bin'
        bindir.mkdir()
        binary = bindir / 'claude'
        binary.write_text(f'#!{sys.executable}\nimport json,os\nprint(json.dumps({{"config":os.getenv("CLAUDE_CONFIG_DIR"),"secure":os.getenv("CLAUDE_SECURESTORAGE_CONFIG_DIR"),"key":os.getenv("ANTHROPIC_API_KEY")}}))\n')
        binary.chmod(0o755)
        self.env.update(PATH=str(bindir) + ':' + self.env['PATH'], CLAUDE_CONFIG_DIR='isolated',
                        CLAUDE_SECURESTORAGE_CONFIG_DIR='isolated', ANTHROPIC_API_KEY='test-only')
        result = json.loads(self.cli('code', 'main').stdout)
        self.assertIsNone(result['config'])
        self.assertIsNone(result['secure'])
        self.assertEqual(result['key'], 'test-only')

    def test_regular_defaults_wrap_claude_and_clear_or_remove_restores_binary(self):
        self.register_regular()
        bindir = self.home / 'bin'
        bindir.mkdir()
        binary = bindir / 'claude'
        binary.write_text(f'#!{sys.executable}\nimport json,os,sys\nprint(json.dumps({{"args":sys.argv[1:],"config":os.getenv("CLAUDE_CONFIG_DIR")}}))\n')
        binary.chmod(0o755)
        self.env['PATH'] = str(bindir) + ':' + self.env['PATH']
        self.cli('code-options', 'main', '--flags=--dangerously-skip-permissions')
        self.assertIn('alias claude=', self.rc.read_text())
        invocation = 'claude --model opus-4-6'
        command = f'source {mc.shlex.quote(str(self.rc))}\neval {mc.shlex.quote(invocation)}'
        record = json.loads(subprocess.check_output(['/bin/zsh', '-f', '-c', command], env=self.env, text=True))
        self.assertEqual(record['args'], ['--dangerously-skip-permissions', '--model', 'opus-4-6'])
        self.assertIsNone(record['config'])
        record = json.loads(self.cli('code', 'main', '--restricted', '--model', 'opus-4-6').stdout)
        self.assertEqual(record['args'], ['--restricted', '--model', 'opus-4-6'])
        record = json.loads(self.cli('code', 'main', '--restricted', '--dangerously-skip-permissions').stdout)
        self.assertEqual(record['args'], ['--restricted', '--dangerously-skip-permissions'])
        self.cli('code-options', 'main', '--clear')
        self.assertNotIn('alias claude=', self.rc.read_text())
        self.cli('code-options', 'main', '--flags=--dangerously-skip-permissions')
        self.cli('remove', 'main')
        self.assertNotIn('alias claude=', self.rc.read_text())
        self.assertIn('KEEP_ME', self.rc.read_text())
        self.assertTrue(binary.exists())

    def test_reset_regular_deletes_data_not_original_app_or_other_profiles(self):
        self.register_regular()
        isolated = self.create()
        desktop = self.home / 'Library/Application Support/Claude'
        desktop.mkdir(parents=True)
        (desktop / 'cookies').write_text('test-only')
        code = self.home / '.claude'
        code.mkdir()
        (code / 'settings.json').write_text('{}')
        (self.home / '.claude.json').write_text('{}')
        (self.home / '.claude.json.backup.1').write_text('{}')
        self.cli('reset', 'main', ok=False)
        self.assertTrue(desktop.exists())
        # Mock only Keychain mutation: subprocess HOME does not isolate macOS's actual login Keychain.
        registry = json.loads((self.base / 'profiles.json').read_text())
        def fake_process(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(mc, 'BASE', self.base), patch.object(Path, 'home', return_value=self.home), \
                patch.object(mc.subprocess, 'run', side_effect=fake_process) as command:
            mc.reset_data('main')
        self.assertFalse(desktop.exists())
        self.assertFalse(code.exists())
        self.assertFalse((self.home / '.claude.json').exists())
        self.assertFalse((self.home / '.claude.json.backup.1').exists())
        self.assertTrue(self.source.exists())
        self.assertTrue(isolated.exists())
        self.assertTrue((self.home / '.claude-personal').exists())
        self.assertEqual(json.loads((self.base / 'profiles.json').read_text()), registry)
        self.assertIn('Claude Code-credentials', command.call_args.args[0])

    def test_reset_isolated_keeps_launcher_and_alias(self):
        app = self.create()
        code = self.home / '.claude-personal'
        (code / 'settings.json').write_text('{}')
        self.cli('reset', 'personal', '--yes')
        self.assertTrue(app.exists())
        self.assertTrue(code.exists())
        self.assertFalse((code / 'settings.json').exists())
        self.assertTrue((self.home / '.local/bin/claude-personal').is_file())
        self.assertNotIn('alias claude-personal=', self.rc.read_text())

    def test_regular_reset_refuses_active_sessions_and_keychain_failure(self):
        self.register_regular()
        isolated = self.create()
        desktop = self.home / 'Library/Application Support/Claude'
        desktop.mkdir(parents=True)
        marker = desktop / 'keep'
        marker.write_text('fixture')
        for command_line in [str(self.source / 'Contents/MacOS/Claude'), str(isolated / 'Contents/MacOS/Claude'),
                             '/Users/test/.local/share/claude/versions/2.1.0']:
            def active(args, **kwargs):
                return subprocess.CompletedProcess(args, 0, f'{os.getuid()} {command_line}\n', '')
            with patch.object(mc, 'BASE', self.base), patch.object(Path, 'home', return_value=self.home), \
                    patch.object(mc.subprocess, 'run', side_effect=active):
                with self.assertRaises(ValueError):
                    mc.reset_data('main')
            self.assertTrue(marker.exists())
        def denied(args, **kwargs):
            return subprocess.CompletedProcess(args, 1 if args[0].endswith('security') else 0, '', 'denied')
        with patch.object(mc, 'BASE', self.base), patch.object(Path, 'home', return_value=self.home), \
                patch.object(mc.subprocess, 'run', side_effect=denied):
            with self.assertRaises(ValueError):
                mc.reset_data('main')
        self.assertTrue(marker.exists())

    def test_regular_purge_keeps_installed_app(self):
        self.register_regular()
        with patch.object(mc, 'BASE', self.base), patch.object(mc, 'reset_data') as reset:
            mc.remove('main', purge=True)
        reset.assert_called_once_with('main')
        self.assertTrue(self.source.exists())
        self.assertNotIn('main', json.loads((self.base / 'profiles.json').read_text()))

    def test_purge_rejects_redirected_folder(self):
        app = self.create()
        data = self.home / '.claude-personal'
        elsewhere = self.home / 'do-not-delete'
        data.rename(elsewhere)
        data.symlink_to(elsewhere, target_is_directory=True)
        self.cli('remove', 'personal', '--purge', '--yes', ok=False)
        self.assertTrue(elsewhere.exists())
        self.assertTrue(app.exists())

    def test_install_migrates_legacy_profile_and_rebuilds_pinned_app(self):
        app = self.create()
        target = self.home / '.claude-personal'
        legacy = self.base / 'profiles/personal'
        legacy.mkdir(parents=True)
        (target / 'desktop').rename(legacy / 'desktop')
        target.rename(legacy / 'code')
        (legacy / 'code/history.jsonl').write_text('fixture history')
        (legacy / 'desktop/Cookies').write_text('fixture cookies')
        registry = self.base / 'profiles.json'
        profiles = json.loads(registry.read_text())
        profiles['personal'].update(code=str(legacy / 'code'), desktop=str(legacy / 'desktop'))
        registry.write_text(json.dumps(profiles))
        env = dict(self.env, MANYCLAUDE_INSTALL_DIR=str(self.home / 'installed'),
                   MANYCLAUDE_BIN_DIR=str(self.home / 'bin'))
        result = subprocess.run([str(ROOT / 'install.sh')], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((target / 'history.jsonl').read_text(), 'fixture history')
        self.assertEqual((target / 'desktop/Cookies').read_text(), 'fixture cookies')
        self.assertFalse(legacy.exists())
        info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        self.assertEqual(info['ManyClaudeCode'], str(target))
        self.assertEqual(info['ManyClaudeDesktop'], str(target / 'desktop'))
        self.assertEqual(info['ManyClaudeTool'], str((self.home / 'installed/manyclaude').resolve()))
        self.cli('reset', 'personal', '--yes')
        self.assertFalse((self.home / '.manyclaude-storage-backup/personal').exists())

    def test_install_twice_on_path_and_aliases(self):
        env = dict(self.env, MANYCLAUDE_INSTALL_DIR=str(self.home / 'installed tool'), MANYCLAUDE_BIN_DIR=str(self.home / 'global bin'))
        for _ in range(2):
            result = subprocess.run([str(ROOT / 'install.sh')], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.rc.read_text().count('# >>> manyclaude PATH >>>'), 1)
        result = subprocess.run(['/bin/zsh', '-f', '-c', f'source {mc.shlex.quote(str(self.rc))}; manyclaude --version'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), mc.VERSION)
        installed = self.home / 'global bin/manyclaude'
        result = subprocess.run([str(installed), 'create', 'Work', '--apps-dir', str(self.apps), '--claude-app', str(self.source)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.home / 'installed tool/manyclaude'), (self.home / 'global bin/claude-work').read_text())
        self.assertTrue((self.home / 'installed tool/manyclaude-code-work').is_file())
        result = subprocess.run([str(installed), 'refresh'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
