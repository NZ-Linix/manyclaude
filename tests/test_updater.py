import json
from pathlib import Path
import plistlib
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import updater


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.source = self.home / 'Claude.app'
        self.write_app(self.source, '1.0.0')
        self.manager = SimpleNamespace(read_profiles=lambda: {'work': {'source': str(self.source), 'name': 'Work'}},
                                       source_app=lambda *args: self.source, running=Mock(return_value=False),
                                       refresh=Mock())
        self.release = {'currentRelease': '2.0.0', 'releases': [{'updateTo': {
            'version': '2.0.0', 'url': 'https://downloads.claude.ai/releases/Claude.zip'}}]}

    def write_app(self, path, version):
        (path / 'Contents').mkdir(parents=True, exist_ok=True)
        (path / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': 'com.anthropic.claudefordesktop', 'CFBundleShortVersionString': version}))

    def fake_download(self, url, path):
        path.write_text(json.dumps(self.release) if url == updater.FEED else 'fixture archive')

    def fake_run(self, args, **kwargs):
        if args[:3] == ['/usr/bin/ditto', '-x', '-k']:
            self.write_app(Path(args[-1]) / 'Claude.app', '2.0.0')
        return SimpleNamespace(stderr=f'TeamIdentifier={updater.TEAM}\n', returncode=0)

    def test_update_verifies_download_updates_shared_cli_then_refreshes(self):
        with patch.object(updater, 'download', side_effect=self.fake_download), \
                patch.object(updater.shutil, 'which', return_value='/fixture/claude'), \
                patch.object(updater.subprocess, 'run', side_effect=self.fake_run) as run, \
                patch.object(updater, 'replace_app') as replace:
            updater.update(self.manager)
        replace.assert_called_once()
        self.assertEqual(replace.call_args.args[0], self.source)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(['/fixture/claude', 'install', 'latest'], commands)
        self.assertIn(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(replace.call_args.args[1])], commands)
        self.manager.refresh.assert_called_once()

    def test_running_profile_blocks_all_downloads(self):
        self.manager.running.return_value = True
        with patch.object(updater, 'download') as download:
            with self.assertRaisesRegex(ValueError, 'Quit Work'):
                updater.update(self.manager)
        download.assert_not_called()
        self.manager.refresh.assert_not_called()

    def test_wrong_signer_leaves_original_and_cli_untouched(self):
        def run(args, **kwargs):
            result = self.fake_run(args, **kwargs)
            result.stderr = 'TeamIdentifier=OTHER\n'
            return result
        with patch.object(updater, 'download', side_effect=self.fake_download), \
                patch.object(updater.shutil, 'which', return_value='/fixture/claude'), \
                patch.object(updater.subprocess, 'run', side_effect=run) as calls, \
                patch.object(updater, 'replace_app') as replace:
            with self.assertRaisesRegex(ValueError, 'not signed by Anthropic'):
                updater.update(self.manager)
        replace.assert_not_called()
        self.assertFalse(any(call.args[0][1:3] == ['install', 'latest'] for call in calls.call_args_list))
        self.assertEqual(updater.app_version(self.source), '1.0.0')

    def test_current_desktop_does_not_download_archive(self):
        self.write_app(self.source, '2.0.0')
        with patch.object(updater, 'download', side_effect=self.fake_download) as download, \
                patch.object(updater.shutil, 'which', return_value='/fixture/claude'), \
                patch.object(updater.subprocess, 'run', side_effect=self.fake_run), \
                patch.object(updater, 'replace_app') as replace:
            updater.update(self.manager)
        self.assertEqual(download.call_count, 1)
        replace.assert_not_called()
        self.manager.refresh.assert_called_once()

    def test_cli_failure_still_refreshes_updated_desktop_profiles(self):
        def run(args, **kwargs):
            if args[1:3] == ['install', 'latest']:
                raise updater.subprocess.CalledProcessError(1, args)
            return self.fake_run(args, **kwargs)
        with patch.object(updater, 'download', side_effect=self.fake_download), \
                patch.object(updater.shutil, 'which', return_value='/fixture/claude'), \
                patch.object(updater.subprocess, 'run', side_effect=run), \
                patch.object(updater, 'replace_app'):
            with self.assertRaisesRegex(ValueError, 'Code update failed'):
                updater.update(self.manager)
        self.manager.refresh.assert_called_once()

    def test_release_feed_rejects_unofficial_download_host(self):
        self.release['releases'][0]['updateTo']['url'] = 'https://example.com/Claude.zip'
        with self.assertRaises(ValueError):
            updater.latest_release(self.release)

    def test_failed_replacement_restores_original(self):
        downloaded = self.home / 'download.app'
        self.write_app(downloaded, '2.0.0')
        original_rename = Path.rename
        def rename(path, target):
            if path.name == 'Claude.app' and path.parent.name.startswith('.manyclaude-update-'):
                raise OSError('fixture replacement failure')
            return original_rename(path, target)
        def run(args, **kwargs):
            if args[0] == '/usr/bin/ditto':
                updater.shutil.copytree(args[1], args[2])
        with patch.object(updater.subprocess, 'run', side_effect=run), patch.object(Path, 'rename', rename):
            with self.assertRaises(OSError):
                updater.replace_app(self.source, downloaded)
        self.assertEqual(updater.app_version(self.source), '1.0.0')


class LinuxUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.binary = str(self.home / '.local/share/claude/versions/1')
        self.source = self.home / 'claude-desktop'
        self.env = {'PATH': '/usr/bin:/bin', 'MISE_DATA_DIR': str(self.home / 'mise'),
                    'ANTHROPIC_API_KEY': 'fixture-key', 'CLAUDE_CONFIG_DIR': '/wrong'}
        self.manager = SimpleNamespace(LINUX=True, AUTH_ENV=('ANTHROPIC_API_KEY',),
                                       read_profiles=lambda: {'work': {'name': 'Work', 'source': str(self.source)}},
                                       source_app=lambda *args: self.source,
                                       running=Mock(return_value=False), refresh=Mock())
        self.tools = {'pacman': '/usr/bin/pacman', 'yay': '/usr/bin/yay', 'mise': '/usr/bin/mise',
                      'npm': '/usr/bin/npm', 'sudo': '/usr/bin/sudo'}
        self.cli_package = None
        self.addCleanup(patch.stopall)
        import runtime
        patch.object(runtime, 'claude_command', side_effect=lambda: (self.binary, dict(self.env))).start()
        patch.object(updater.shutil, 'which', side_effect=lambda name, **kw: self.tools.get(name)).start()
        patch.object(updater, 'package_owner', side_effect=lambda pm, path, env: 'claude-desktop' if Path(path) == self.source else self.cli_package).start()
        self.run = patch.object(updater.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')).start()

    def commands(self):
        return [call.args[0] for call in self.run.call_args_list]

    def test_native_and_desktop_update_then_refresh_without_profile_environment(self):
        updater.update(self.manager, yes=True)
        self.assertEqual(self.commands(), [['/usr/bin/yay', '-S', '--needed', 'claude-desktop', '--noconfirm'],
                                           [self.binary, 'install', 'latest'], [self.binary, '--version']])
        self.manager.refresh.assert_called_once()
        for call in self.run.call_args_list:
            self.assertNotIn('CLAUDE_CONFIG_DIR', call.kwargs['env'])
            self.assertNotIn('ANTHROPIC_API_KEY', call.kwargs['env'])

    def test_mise_updates_via_mise(self):
        self.binary = str(self.home / 'mise/installs/claude/1/bin/claude')
        updater.update(self.manager)
        self.assertIn(['/usr/bin/mise', 'upgrade', 'claude'], self.commands())
        self.assertNotIn('--noconfirm', self.commands()[0])

    def test_packaged_cli_updates_with_desktop_not_native_installer(self):
        self.cli_package = 'claude-code'
        updater.update(self.manager)
        self.assertEqual(self.commands()[0], ['/usr/bin/yay', '-S', '--needed', 'claude-code', 'claude-desktop'])
        self.assertEqual(len(self.commands()), 2)

    def test_package_failure_still_refreshes_and_does_not_run_cli_installer(self):
        self.run.side_effect = subprocess.CalledProcessError(1, ['/usr/bin/yay'])
        with self.assertRaises(subprocess.CalledProcessError):
            updater.update(self.manager)
        self.manager.refresh.assert_called_once()
        self.assertEqual(len(self.commands()), 1)

    def test_running_desktop_blocks_every_update(self):
        self.manager.running.return_value = True
        with self.assertRaisesRegex(ValueError, 'Quit Work'):
            updater.update(self.manager)
        self.run.assert_not_called()
        self.manager.refresh.assert_not_called()

    def test_binary_repo_fallback_without_aur_helper(self):
        self.tools.pop('yay')
        updater.update(self.manager)
        self.assertIn(['/usr/bin/sudo', '/usr/bin/pacman', '-S', '--needed', 'claude-desktop'], self.commands())


if __name__ == '__main__':
    unittest.main()
