import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import runtime
import upgrade
import test_tui as fixture

ROOT, mc = fixture.ROOT, fixture.mc


class ChatSyncRemovalTests(unittest.TestCase):
    setUp = fixture.NavigationTests.setUp

    def test_removed_commands_are_rejected_and_not_advertised(self):
        with contextlib.redirect_stdout(io.StringIO()) as help_text:
            with self.assertRaises(SystemExit) as status:
                mc.main(['--help'])
        self.assertEqual(status.exception.code, 0)
        self.assertNotIn('sync-chat', help_text.getvalue())
        with patch.object(mc, 'ensure_storage') as migrate, contextlib.redirect_stderr(io.StringIO()):
            for command in ('sync', 'sync-chat', '_sync-daemon'):
                with self.assertRaises(SystemExit) as status:
                    mc.main([command])
                self.assertEqual(status.exception.code, 2)
        migrate.assert_not_called()

    def test_usage_refresh_stays_enabled_without_copying_resumed_history(self):
        (self.base / 'openusage-enabled').touch()
        identifier = str(uuid4())
        transcript = Path(self.profiles['personal']['code']) / 'projects/fixture' / (identifier + '.jsonl')
        transcript.parent.mkdir(parents=True)
        transcript.write_text('private source transcript')
        project = self.home / 'project with spaces'
        project.mkdir()
        session = self.home / '.claude-swap-backup/sessions/2-fixture'
        session.parent.mkdir(parents=True)
        session.symlink_to(self.profiles['work']['code'])
        self.profiles['work']['code_session'] = str(session)
        mc.save_profiles(self.profiles)
        with patch.object(mc, 'sync_openusage') as usage, patch.object(mc, 'prepare_skills'), \
                patch.object(mc, 'configure_code_project'), patch.object(runtime, 'claude_command', return_value=('/fixture/claude', {})), \
                patch.object(mc.subprocess, 'run', return_value=subprocess.CompletedProcess([], 17)) as launch:
            with self.assertRaises(SystemExit) as status:
                mc.code('work', ['--resume', identifier])
        self.assertEqual(status.exception.code, 17)
        self.assertEqual(usage.call_count, 2)
        self.assertEqual(launch.call_args.args[0], ['/fixture/claude', '--resume', identifier])
        env = launch.call_args.kwargs['env']
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], self.profiles['work']['code'])
        self.assertEqual(env['CLAUDE_SECURESTORAGE_CONFIG_DIR'], str(session))
        self.assertFalse((Path(self.profiles['work']['code']) / 'projects').exists())
        self.assertEqual(transcript.read_text(), 'private source transcript')

    def test_actual_cli_keeps_callers_cwd_and_account_histories_separate(self):
        project = self.home / 'project with spaces'
        project.mkdir()
        binary = self.home / 'bin/claude'
        binary.parent.mkdir()
        binary.write_text(f'#!{sys.executable}\nimport json,os,sys\nprint(json.dumps({{"cwd":os.getcwd(),"config":os.getenv("CLAUDE_CONFIG_DIR"),"args":sys.argv[1:]}}))\n')
        binary.chmod(0o755)
        (self.base / 'openusage-enabled').touch()
        identifier = str(uuid4())
        env = dict(os.environ, HOME=str(self.home), MANYCLAUDE_HOME=str(self.base), PATH=str(binary.parent) + ':' + os.environ['PATH'])
        for profile in ('personal', 'work'):
            result = subprocess.run([sys.executable, str(ROOT / 'manyclaude'), 'code', profile, '--resume', identifier],
                                    cwd=project, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(result.stdout)
            self.assertEqual(record['cwd'], str(project.resolve()))
            self.assertEqual(record['config'], self.profiles[profile]['code'])
            self.assertEqual(record['args'], ['--resume', identifier])
        self.assertEqual(self.source.read_text(), 'private history')
        self.assertFalse((Path(self.profiles['work']['code']) / 'history.jsonl').exists())
        self.assertFalse((self.base / 'chat-sync.json').exists())

    def test_upgrade_removes_owned_agent_and_metadata_keeps_usage_and_histories(self):
        label = 'local.manyclaude.sync.' + upgrade.hashlib.sha256(str(self.base).encode()).hexdigest()[:12]
        agent = self.home / 'Library/LaunchAgents' / (label + '.plist')
        agent.parent.mkdir(parents=True)
        agent.write_bytes(plistlib.dumps({'Label': label, 'EnvironmentVariables': {'MANYCLAUDE_HOME': str(self.base)},
                                         'ProgramArguments': [sys.executable, str(ROOT / 'manyclaude'), '_sync-daemon']}))
        for name in ('sync-links.json', 'sync-status.json', 'chat-sync.json', 'sync-service.log'):
            (self.base / name).write_text('{}')
        (self.base / 'openusage-enabled').touch()
        with patch.object(mc, 'LINUX', False), patch.object(upgrade.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as command:
            upgrade.remove_chat_sync(mc)
        self.assertFalse(agent.exists())
        self.assertTrue(any('bootout' in call.args[0] for call in command.call_args_list))
        self.assertFalse((self.base / 'sync-links.json').exists())
        self.assertTrue((self.base / 'openusage-enabled').exists())
        self.assertEqual(self.source.read_text(), 'private history')
        self.assertEqual(mc.read_profiles(), self.profiles)

    def test_upgrade_refuses_unowned_service_without_touching_files(self):
        label = 'local.manyclaude.sync.' + upgrade.hashlib.sha256(str(self.base).encode()).hexdigest()[:12]
        agent = self.home / 'Library/LaunchAgents' / (label + '.plist')
        agent.parent.mkdir(parents=True)
        content = plistlib.dumps({'Label': 'someone.else'})
        agent.write_bytes(content)
        with patch.object(mc, 'LINUX', False), patch.object(upgrade.subprocess, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'not owned'):
                upgrade.remove_chat_sync(mc)
        self.assertEqual(agent.read_bytes(), content)
        command.assert_not_called()

    def test_upgrade_stops_manually_started_old_watcher(self):
        script = self.home / 'manyclaude'
        script.write_text('import fcntl,os,sys,time\n'
                          'with open(sys.argv[2],"w+") as lock:\n'
                          ' fcntl.flock(lock,fcntl.LOCK_EX)\n'
                          ' lock.write(str(os.getpid()));lock.flush()\n'
                          ' time.sleep(60)\n')
        lock = self.base / '.sync-service.lock'
        process = subprocess.Popen([sys.executable, str(script), '_sync-daemon', str(lock)])
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (not lock.exists() or not lock.read_text()):
            time.sleep(.02)
        original = subprocess.run
        def command(args, **kwargs):
            if args[0] == '/bin/launchctl':
                return subprocess.CompletedProcess(args, 113, '', '')
            return original(args, **kwargs)
        with patch.object(mc, 'LINUX', False), patch.object(upgrade.subprocess, 'run', side_effect=command):
            upgrade.remove_chat_sync(mc)
        self.assertNotEqual(process.wait(timeout=5), 0)
        self.assertFalse(lock.exists())


if __name__ == '__main__':
    unittest.main()
