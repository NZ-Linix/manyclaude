import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import linux_desktop
import openusage
import runtime
import updater

loader = importlib.machinery.SourceFileLoader('mc_linux_test', str(ROOT / 'manyclaude'))
spec = importlib.util.spec_from_loader(loader.name, loader)
mc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mc
loader.exec_module(mc)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(Path, 'home', return_value=self.home).start()
        patch.object(mc, 'BASE', self.home / 'manager').start()
        patch.dict(os.environ, {'HOME': str(self.home), 'PATH': '/usr/bin:/bin'}, clear=True).start()

    def executable(self, path, text='#!/bin/sh\nexit 0\n'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)
        return path

    def test_gui_environment_includes_mise_and_native_without_shadowing_caller(self):
        shim = self.executable(self.home / '.local/share/mise/shims/claude')
        self.assertEqual(shutil.which('claude', path=runtime.tool_environment()['PATH']), str(shim))
        native = self.executable(self.home / 'custom/claude')
        os.environ['PATH'] = str(native.parent) + ':/usr/bin:/bin'
        self.assertEqual(runtime.claude_command()[0], str(native))

    def test_mise_resolves_current_version_and_runtime_environment(self):
        shim = self.executable(self.home / '.local/share/mise/shims/claude')
        real = self.executable(self.home / '.local/share/mise/installs/claude/1/bin/claude')
        mise = self.executable(self.home / '.local/bin/mise',
                               '#!/bin/sh\nif [ "$1" = which ]; then\n  printf "%s\\n" ' + shlex.quote(str(real)) +
                               '\nelse\n  printf "%s\\n" ' + shlex.quote(json.dumps({'PATH': str(real.parent), 'FEATURE': 'mise'})) + '\nfi\n')
        os.environ['PATH'] = str(mise.parent) + ':/usr/bin:/bin'
        binary, env = runtime.claude_command()
        self.assertEqual(binary, str(real))
        self.assertEqual(env['FEATURE'], 'mise')
        self.assertEqual(env['PATH'], str(real.parent))
        shim.unlink()
        self.assertEqual(runtime.claude_command()[0], str(real))
        mise.write_text('#!/bin/sh\nexit 1\n')
        with self.assertRaisesRegex(ValueError, 'mise use -g claude'):
            runtime.claude_command()

    def test_custom_mise_directories(self):
        os.environ['MISE_DATA_DIR'] = str(self.home / 'custom-mise')
        self.assertIn(str(self.home / 'custom-mise/shims'), runtime.tool_environment()['PATH'])
        os.environ['MISE_SHIMS_DIR'] = str(self.home / 'custom-shims')
        self.assertIn(str(self.home / 'custom-shims'), runtime.tool_environment()['PATH'])

    def test_native_install_on_omarchy_does_not_require_mise(self):
        native = self.executable(self.home / '.local/bin/claude')
        self.executable(self.home / '.local/bin/mise', '#!/bin/sh\nexit 99\n')
        self.executable(self.home / '.local/share/mise/shims/claude', '#!/bin/sh\nexit 99\n')
        self.assertEqual(runtime.claude_command()[0], str(native))

    def test_stale_mise_shim_falls_back_to_native_without_loading_mise_env(self):
        native = self.executable(self.home / '.local/bin/claude')
        self.executable(self.home / '.local/bin/mise', '#!/bin/sh\nexit 1\n')
        shim = self.executable(self.home / '.local/share/mise/shims/claude')
        os.environ['PATH'] = str(shim.parent) + ':' + str(native.parent) + ':/usr/bin:/bin'
        binary, env = runtime.claude_command()
        self.assertEqual(binary, str(native))
        self.assertEqual(shutil.which('claude', path=env['PATH']), str(native))

    @unittest.skipUnless(shutil.which('mise'), 'real mise is not installed')
    def test_real_mise_linked_install_and_gui_resolution(self):
        mise = shutil.which('mise')
        if not mise:
            self.skipTest('mise is not on the isolated test PATH')
        install = self.home / 'fixture-install'
        self.executable(install / 'bin/claude', '#!/bin/sh\nprintf "mise-fixture\\n"\n')
        env = dict(os.environ, MISE_YES='1')
        for args in ([mise, 'link', 'claude@fixture', str(install)], [mise, 'use', '-g', 'claude@fixture']):
            result = subprocess.run(args, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        binary, resolved = runtime.claude_command()
        self.assertIn('/mise/installs/', binary)
        result = subprocess.check_output([binary], env=resolved, text=True)
        self.assertEqual(result.strip(), 'mise-fixture')

    def test_aur_discovery_and_missing_install_hint(self):
        binary = self.executable(self.home / '.local/bin/claude-desktop')
        os.environ['PATH'] = str(binary.parent) + ':/usr/bin:/bin'
        self.assertEqual(linux_desktop.source_app(), binary)
        binary.unlink()
        with patch.object(linux_desktop.shutil, 'which', return_value=None), \
                self.assertRaisesRegex(ValueError, 'yay -S claude-desktop'):
            linux_desktop.source_app()

    def test_linux_credentials_never_use_keychain(self):
        code = self.home / 'code'
        code.mkdir()
        credentials = json.dumps({'claudeAiOauth': {'accessToken': 'fixture'}})
        (code / '.credentials.json').write_text(credentials)
        with patch.object(openusage.sys, 'platform', 'linux'), patch.object(openusage.subprocess, 'run') as run:
            self.assertEqual(openusage.read_credentials({'code': str(code)}), (credentials, False))
        run.assert_not_called()

    def test_linux_update_rejects_unowned_desktop_without_macos_downloads(self):
        binary = self.executable(self.home / '.local/share/mise/installs/claude/1/bin/claude')
        source = self.executable(self.home / '.local/bin/claude-desktop')
        with patch.object(mc, 'LINUX', True), patch.object(runtime, 'claude_command', return_value=(str(binary), {})), \
                patch.object(mc, 'read_profiles', return_value={}), patch.object(mc, 'source_app', return_value=source), \
                patch.object(updater.shutil, 'which', return_value='/usr/bin/pacman'), \
                patch.object(updater, 'package_owner', return_value=None), patch.object(updater, 'download') as download:
            with self.assertRaisesRegex(ValueError, 'not owned'):
                updater.update(mc)
        download.assert_not_called()

    def test_internal_desktop_runner_dispatch_and_argument_validation(self):
        profile = {'name': 'Work'}
        with patch.object(mc, 'LINUX', True), patch.object(mc, 'get_profile', return_value=('work', profile)), \
                patch.object(linux_desktop, 'launch') as launch:
            mc.main(['_desktop-run', 'work'])
            launch.assert_called_once_with(mc, 'work', profile)
            with self.assertRaisesRegex(ValueError, 'Invalid Linux'):
                mc.main(['_desktop-run', 'work', '12345678-1234-4234-8234-123456789abc'])
            with self.assertRaisesRegex(ValueError, 'Invalid Linux'):
                mc.main(['_desktop-run'])


    def test_trust_uses_canonical_config_and_preserves_usage_bridge_settings(self):
        base = self.home / 'manager'
        base.mkdir()
        code = self.home / 'code'
        session = self.home / 'live-session'
        code.mkdir()
        session.mkdir()
        canonical = code / '.claude.json'
        canonical.write_text('{"keep": "original"}')
        legacy = session / '.config.json'
        original = {'keep': 'session', 'projects': {'/existing': {'keep': True}}}
        legacy.write_text(json.dumps(original))
        cwd = self.home / 'project'
        cwd.mkdir()
        profile = {'code': str(code), 'code_session': str(session), 'trust_folders': True}
        with patch.object(mc, 'BASE', base), patch.object(Path, 'cwd', return_value=cwd), \
                patch.object(mc.shutil, 'which', return_value=None):
            mc.configure_code_project(profile)
        data = json.loads(canonical.read_text())
        self.assertTrue(data['projects'][str(cwd)]['hasTrustDialogAccepted'])
        self.assertEqual(data['keep'], 'original')
        self.assertEqual(json.loads(legacy.read_text()), original)

    def test_regular_trust_uses_legacy_store_and_ask_trust_stops_writes(self):
        base = self.home / 'manager'
        base.mkdir()
        code = self.home / '.claude'
        code.mkdir()
        legacy = code / '.config.json'
        legacy.write_text('{}')
        with patch.object(mc, 'BASE', base), patch.object(Path, 'cwd', return_value=self.home), \
                patch.object(mc.shutil, 'which', return_value=None):
            mc.configure_code_project({'regular': True, 'code': str(code), 'trust_folders': False})
            self.assertEqual(legacy.read_text(), '{}')
            mc.configure_code_project({'regular': True, 'code': str(code), 'trust_folders': True})
        self.assertTrue(json.loads(legacy.read_text())['projects'][str(self.home)]['hasTrustDialogAccepted'])
        self.assertFalse((self.home / '.claude.json').exists())


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process and desktop integration')
class LinuxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='manyclaude-linux-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.apps = self.home / '.local/share/applications'
        self.base = self.home / '.local/share/manyclaude'
        self.bin = self.home / '.local/bin'
        self.bin.mkdir(parents=True)
        self.record = self.home / 'desktop.json'
        self.source = self.bin / 'claude-desktop'
        self.source.write_text(f'''#!{sys.executable}
import json, os, sys, time
from pathlib import Path
Path({str(self.record)!r}).write_text(json.dumps({{'args': sys.argv[1:], 'desktop': os.getenv('CLAUDE_USER_DATA_DIR'), 'code': os.getenv('CLAUDE_CONFIG_DIR'), 'key': os.getenv('ANTHROPIC_API_KEY'), 'path': os.getenv('PATH')}}))
time.sleep(0.7)
''')
        self.source.chmod(0o755)
        self.cli_binary = self.bin / 'claude'
        self.cli_binary.write_text(f'''#!{sys.executable}
import json, os, sys
print(json.dumps({{'args': sys.argv[1:], 'code': os.getenv('CLAUDE_CONFIG_DIR'), 'key': os.getenv('ANTHROPIC_API_KEY'), 'feature': os.getenv('FEATURE')}}))
''')
        self.cli_binary.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), SHELL='/bin/bash', PATH=str(self.bin) + ':/usr/bin:/bin',
                        ANTHROPIC_API_KEY='fixture-key')
        for name in ('MANYCLAUDE_HOME', 'XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'MISE_DATA_DIR', 'MISE_SHIMS_DIR'):
            self.env.pop(name, None)

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(ROOT / 'manyclaude'), *args], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, ok, result.stderr + result.stdout)
        return result

    def profiles(self):
        return json.loads((self.base / 'profiles.json').read_text())

    def test_create_code_alias_refresh_reset_remove_and_purge(self):
        self.cli('create', 'Work', '--app-name', 'Work account')
        profile = self.profiles()['work']
        app = Path(profile['app'])
        self.assertEqual(app, self.apps / 'manyclaude-work.desktop')
        self.assertIn('Name=Work account\n', app.read_text())
        self.assertIn('X-ManyClaude-Profile=', app.read_text())
        if shutil.which('desktop-file-validate'):
            subprocess.run(['desktop-file-validate', str(app)], check=True)
        self.cli('code-options', 'work', '--env', 'FEATURE=saved', '--flags=--model sonnet')
        self.env['FEATURE'] = 'caller'
        record = json.loads(self.cli('code', 'work', '--', 'prompt with spaces').stdout)
        self.assertEqual(record, {'args': ['--model', 'sonnet', 'prompt with spaces'], 'code': profile['code'], 'key': None, 'feature': 'caller'})
        self.cli('alias', 'work', 'cc-work')
        invocation = 'shopt -s expand_aliases\nsource ~/.bashrc\ncc-work --resume'
        record = json.loads(subprocess.check_output(['/bin/bash', '-c', invocation], env=self.env, text=True))
        self.assertEqual(record['args'], ['--model', 'sonnet', '--resume'])
        self.cli('refresh')
        credential = Path(profile['code']) / '.credentials.json'
        credential.write_text('fixture')
        self.cli('reset', 'work', '--yes')
        self.assertFalse(credential.exists())
        self.assertTrue(app.exists())
        self.cli('remove', 'work')
        self.assertFalse(app.exists())
        self.assertTrue(Path(profile['code']).exists())
        self.cli('create', 'Work')
        self.cli('remove', 'work', '--purge', '--yes')
        self.assertFalse(Path(profile['code']).exists())
        self.assertTrue(self.source.exists())

    def test_desktop_launch_env_lock_and_package_updates(self):
        self.cli('create', 'Work')
        profile = self.profiles()['work']
        process = subprocess.Popen([sys.executable, str(ROOT / 'manyclaude'), '_desktop-run', 'work'], env=self.env)
        self.addCleanup(lambda: process.poll() is None and process.terminate())
        for _ in range(100):
            if self.record.exists():
                break
            time.sleep(0.01)
        record = json.loads(self.record.read_text())
        self.assertEqual(record['desktop'], profile['desktop'])
        self.assertEqual(record['code'], profile['code'])
        self.assertIsNone(record['key'])
        self.assertIn('--user-data-dir=' + profile['desktop'], record['args'])
        self.cli('reset', 'work', '--yes', ok=False)
        self.cli('remove', 'work', ok=False)
        self.cli('refresh', 'work', ok=False)
        process.wait(timeout=5)
        # Package replacement takes effect without making/copying another runtime.
        self.source.write_text(self.source.read_text().replace("'args': sys.argv[1:]", "'updated': True, 'args': sys.argv[1:]"))
        self.cli('_desktop-run', 'work')
        self.assertTrue(json.loads(self.record.read_text())['updated'])
        self.record.unlink()
        self.cli('open', 'work')
        for _ in range(100):
            if self.record.exists():
                break
            time.sleep(0.01)
        self.assertTrue(self.record.exists())
        time.sleep(0.8)

    def test_regular_profile_cleans_inherited_paths_and_keeps_original(self):
        self.cli('create', 'Main', '--regular')
        self.cli('create', 'Second', '--regular', ok=False)
        self.env.update(CLAUDE_CONFIG_DIR='/wrong', CLAUDE_USER_DATA_DIR='/wrong')
        record = json.loads(self.cli('code', 'main').stdout)
        self.assertIsNone(record['code'])
        self.assertEqual(record['key'], 'fixture-key')
        self.cli('_desktop-run', 'main')
        desktop = json.loads(self.record.read_text())
        self.assertIsNone(desktop['desktop'])
        self.assertEqual(desktop['args'], [])
        profile = self.profiles()['main']
        for path in (Path(profile['desktop']), Path(profile['code'])):
            path.mkdir(parents=True)
            (path / 'fixture').write_text('fixture')
        self.cli('remove', 'main', '--purge', '--yes')
        self.assertTrue(self.source.exists())
        self.assertFalse(Path(profile['code']).exists())

    def test_foreign_launcher_and_redirected_data_are_protected(self):
        self.cli('create', 'Work')
        profile = self.profiles()['work']
        app = Path(profile['app'])
        app.write_text('[Desktop Entry]\nName=unrelated\n')
        self.cli('refresh', 'work', ok=False)
        self.cli('remove', 'work', ok=False)
        self.assertEqual(app.read_text(), '[Desktop Entry]\nName=unrelated\n')
        desktop = Path(profile['desktop'])
        desktop.rmdir()
        outside = self.home / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('safe')
        desktop.symlink_to(outside)
        self.cli('reset', 'work', '--yes', ok=False)
        self.assertEqual((outside / 'keep').read_text(), 'safe')

    def test_custom_xdg_and_launch_exec_escaping(self):
        self.env['XDG_DATA_HOME'] = str(self.home / 'data spaces % $ quotes " back\\slash')
        self.base = Path(self.env['XDG_DATA_HOME']) / 'manyclaude'
        self.cli('create', 'Work')
        app = Path(self.profiles()['work']['app'])
        if shutil.which('desktop-file-validate'):
            subprocess.run(['desktop-file-validate', str(app)], check=True)
        self.assertIn('%%', app.read_text())
        self.assertIn('Name=Claude Work', app.read_text())
        if shutil.which('gio'):
            result = subprocess.run(['gio', 'launch', str(app)], env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for _ in range(100):
                if self.record.exists():
                    break
                time.sleep(0.01)
            self.assertEqual(json.loads(self.record.read_text())['code'], self.profiles()['work']['code'])
            time.sleep(0.8)
        self.env['XDG_CONFIG_HOME'] = str(self.home / 'config')
        self.cli('create', 'Main', '--regular')
        self.assertEqual(self.profiles()['main']['desktop'], str(self.home / 'config/Claude'))

    def test_mise_environment_applies_before_isolation_and_resolves_each_launch(self):
        self.cli('create', 'Work')
        real = self.home / '.local/share/mise/installs/claude/1/bin/claude'
        real.parent.mkdir(parents=True)
        shutil.copy2(self.cli_binary, real)
        self.cli_binary.unlink()
        shims = self.home / '.local/share/mise/shims'
        shims.mkdir(parents=True)
        (shims / 'claude').write_text('#!/bin/sh\nexit 99\n')
        (shims / 'claude').chmod(0o755)
        mise = self.bin / 'mise'
        values = {'FEATURE': 'mise', 'CLAUDE_CONFIG_DIR': '/wrong', 'ANTHROPIC_API_KEY': 'mise-key', 'PATH': str(real.parent) + ':/usr/bin:/bin'}
        mise.write_text('#!/bin/sh\nif [ "$1" = which ]; then\nprintf "%s\\n" ' + shlex.quote(str(real)) +
                        '\nelse\nprintf "%s\\n" ' + shlex.quote(json.dumps(values)) + '\nfi\n')
        mise.chmod(0o755)
        self.env['PATH'] = str(self.bin) + ':/usr/bin:/bin'
        record = json.loads(self.cli('code', 'work').stdout)
        self.assertEqual(record['feature'], 'mise')
        self.assertIsNone(record['key'])
        self.assertEqual(record['code'], self.profiles()['work']['code'])
        real.write_text('#!/bin/sh\nprintf "new-version\\n"\n')
        self.assertEqual(self.cli('code', 'work').stdout.strip(), 'new-version')

    def test_install_migrates_legacy_profile_and_rewires_desktop_entry(self):
        self.cli('create', 'Work')
        target = self.home / '.claude-work'
        legacy = self.base / 'profiles/work'
        legacy.mkdir(parents=True)
        (target / 'desktop').rename(legacy / 'desktop')
        target.rename(legacy / 'code')
        (legacy / 'code/.credentials.json').write_text('fixture credentials')
        (legacy / 'desktop/Cookies').write_text('fixture cookies')
        profiles = self.profiles()
        profiles['work'].update(code=str(legacy / 'code'), desktop=str(legacy / 'desktop'))
        (self.base / 'profiles.json').write_text(json.dumps(profiles))
        installed = self.home / 'installed'
        env = dict(self.env, MANYCLAUDE_INSTALL_DIR=str(installed))
        result = subprocess.run(['/bin/sh', str(ROOT / 'install.sh')], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((target / '.credentials.json').read_text(), 'fixture credentials')
        self.assertEqual((target / 'desktop/Cookies').read_text(), 'fixture cookies')
        self.assertEqual(self.profiles()['work']['code'], str(target))
        self.assertIn(str(installed / 'manyclaude'), Path(self.profiles()['work']['app']).read_text())
        self.assertFalse(legacy.exists())
        self.cli('remove', 'work', '--purge', '--yes')
        self.assertFalse((self.home / '.manyclaude-storage-backup/work').exists())

    def test_install_idempotent_and_no_apple_dependencies(self):
        target = self.home / 'installed'
        env = dict(self.env, MANYCLAUDE_INSTALL_DIR=str(target))
        for _ in range(2):
            result = subprocess.run(['/bin/sh', str(ROOT / 'install.sh')], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((target / 'native/launcher').exists())
        self.assertTrue((target / 'runtime.py').exists())
        self.assertEqual((self.home / '.bashrc').read_text().count('# >>> manyclaude PATH >>>'), 1)
        result = subprocess.run([str(self.bin / 'manyclaude'), 'create', 'Work'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('fish'), 'fish is not installed')
    def test_fish_install_and_profile_lifecycle_with_quoted_paths(self):
        target = self.home / "tool's (touch injected) \\ copy"
        bin_dir = self.home / "global's \\ bin"
        config = self.home / "config's \\ files"
        rc = config / 'fish/config.fish'
        rc.parent.mkdir(parents=True)
        original = "set -g user_setting 'keep me'\n"
        rc.write_text(original)
        self.env.update(SHELL=shutil.which('fish'), XDG_CONFIG_HOME=str(config),
                        MANYCLAUDE_INSTALL_DIR=str(target), MANYCLAUDE_BIN_DIR=str(bin_dir))
        for _ in range(2):
            result = subprocess.run(['/bin/sh', str(ROOT / 'install.sh')], env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rc.read_text().count('# >>> manyclaude PATH >>>'), 1)
        self.assertTrue(rc.read_text().startswith(original))
        installed = bin_dir / 'manyclaude'

        def run(*args):
            return subprocess.run([str(installed), *args], env=self.env, capture_output=True, text=True, check=True)

        def fish(command):
            return subprocess.check_output([self.env['SHELL'], '-c', command], env=self.env, text=True)

        self.assertEqual(fish('manyclaude --version').strip(), mc.VERSION)
        run('create', 'Work')
        profile = self.profiles()['work']
        self.assertEqual(profile['rc'], str(rc))
        run('code-options', 'work', '--env', 'FEATURE=saved', '--flags=--model sonnet')
        arguments = ['--resume', 'prompt with spaces', '', '$HOME; (touch injected)', "quote'and\\slash"]
        record = json.loads(fish('claude-work ' + ' '.join(runtime.shell_quote(arg, True) for arg in arguments)))
        self.assertEqual(record['args'], ['--model', 'sonnet', *arguments])
        self.assertEqual(record['code'], profile['code'])
        self.assertIsNone(record['key'])
        self.assertEqual(record['feature'], 'saved')
        self.assertFalse((self.home / 'injected').exists())
        run('alias', 'work', 'cc-work')
        self.assertNotIn('function claude-work\n', rc.read_text())
        self.assertTrue((bin_dir / 'cc-work').is_file())
        self.assertNotIn('function cc-work\n', rc.read_text())
        run('refresh')
        self.assertEqual(json.loads(fish('cc-work --version'))['args'], ['--model', 'sonnet', '--version'])
        run('remove', 'work', '--purge', '--yes')
        self.assertNotIn('function cc-work\n', rc.read_text())
        self.assertTrue(rc.read_text().startswith(original))
        self.assertFalse(Path(profile['code']).exists())

    @unittest.skipUnless(shutil.which('fish'), 'fish is not installed')
    def test_fish_regular_code_defaults_and_removal(self):
        self.env['SHELL'] = shutil.which('fish')
        self.cli('create', 'Main', '--regular')
        self.cli('code-options', 'main', '--env', 'FEATURE=saved', '--flags=--model sonnet')
        record = json.loads(subprocess.check_output([self.env['SHELL'], '-c', 'claude --resume'], env=self.env, text=True))
        self.assertEqual(record['args'], ['--model', 'sonnet', '--resume'])
        self.assertEqual(record['feature'], 'saved')
        self.assertIsNone(record['code'])
        self.cli('code-options', 'main', '--clear')
        rc = self.home / '.config/fish/config.fish'
        self.assertNotIn('function claude\n', rc.read_text())


if __name__ == '__main__':
    unittest.main()
