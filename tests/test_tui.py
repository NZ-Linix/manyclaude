import curses
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tui import ProfileUI
import test_manyclaude as fixture
import tempfile

ROOT, mc = fixture.ROOT, fixture.mc


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='manyclaude-ui-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.base = self.home / 'manager'
        self.base.mkdir()
        self.profiles = {}
        for key in ('personal', 'work'):
            root = self.home / ('.claude-' + key)
            root.mkdir()
            self.profiles[key] = {'name': key.title(), 'code': str(root), 'desktop': str(root / 'desktop'),
                                  'app': str(self.home / (key + '.app'))}
        self.source = self.home / '.claude-personal/history.jsonl'
        self.source.write_text('private history')
        self.addCleanup(patch.stopall)
        patch.object(Path, 'home', return_value=self.home).start()
        patch.object(mc, 'BASE', self.base).start()
        mc.save_profiles(self.profiles)

    def ui(self):
        ui = ProfileUI.__new__(ProfileUI)
        ui.manager = mc
        ui.notice = 'Select a profile.'
        return ui


    def test_cancel_rename_returns_to_profile_menu(self):
        ui = self.ui()
        with patch.object(ui, 'menu', side_effect=[2, None]) as menu, patch.object(ui, 'prompt', return_value=None), \
                patch.object(ui, 'operation') as operation:
            ui.profile('personal')
        self.assertEqual(menu.call_count, 2)
        self.assertEqual(menu.call_args_list[0].args[0], menu.call_args_list[1].args[0])
        operation.assert_not_called()

    def test_create_wizard_back_returns_to_previous_field_then_profile_type(self):
        ui = self.ui()
        with patch.object(ui, 'menu', side_effect=[0, None]) as menu, \
                patch.object(ui, 'prompt', side_effect=['Personal', 'Desktop', None, None, None]) as prompt, \
                patch.object(ui, 'operation') as operation:
            ui.create()
        self.assertEqual([call.args[0] for call in prompt.call_args_list],
                         ['New profile · 1 of 4', 'New profile · 2 of 4', 'New profile · 3 of 4',
                          'New profile · 2 of 4', 'New profile · 1 of 4'])
        self.assertEqual(menu.call_count, 2)
        operation.assert_not_called()

    def test_code_defaults_back_returns_from_environment_to_flags_then_menu(self):
        ui = self.ui()
        with patch.object(ui, 'menu', side_effect=[7, 1, None, None]), \
                patch.object(ui, 'prompt', side_effect=['--model sonnet', None, None]) as prompt, \
                patch.object(ui, 'operation') as operation:
            ui.profile('personal')
        self.assertEqual([call.args[1] for call in prompt.call_args_list], ['Default flags', 'Environment variables', 'Default flags'])
        operation.assert_not_called()

    def test_delete_confirmation_back_returns_to_review_without_deletion(self):
        ui = self.ui()
        with patch.object(ui, 'menu', side_effect=[6, None]), patch.object(ui, 'prompt', return_value=None), \
                patch.object(ui, 'review_reset', side_effect=[True, False]) as review, patch.object(ui, 'operation') as operation:
            ui.profile('personal')
        self.assertEqual(review.call_count, 2)
        operation.assert_not_called()
        self.assertTrue(self.source.exists())

    def test_custom_profile_store_toggle_and_creation_choice(self):
        ui = self.ui()
        with patch.object(ui, 'prompt', side_effect=['Writer', 'writer-claude', '--verbose', '']), \
                patch.object(ui, 'menu', return_value=1), patch.object(ui, 'operation', return_value=False) as operation:
            ui.custom_edit()
        self.assertEqual(operation.call_args.args[0][-2:], ['--store', 'separate'])
        (self.base / 'custom-profiles.json').write_text(json.dumps(
            {'writer': {'name': 'Writer', 'command': 'writer-claude', 'store': 'separate'}}))
        with patch.object(ui, 'menu', side_effect=[3, None]) as menu, patch.object(ui, 'operation') as operation:
            ui.custom_profile('writer')
        self.assertIn('Separate store (history & settings): ON', menu.call_args_list[0].args[2][3])
        operation.assert_called_once()
        self.assertEqual(operation.call_args.args[0], ['custom', 'set', 'Writer', '--store', 'global'])

    def test_export_wizard_lists_all_projects_and_saves_to_current_folder(self):
        for index, project in enumerate(('-elsewhere', '-other-project')):
            folder = self.home / '.claude-work' / 'projects' / project
            folder.mkdir(parents=True)
            path = folder / f'id-{index}.jsonl'
            path.write_text(json.dumps({'type': 'user', 'message': {'content': f'Task {index}'}}) + '\n' +
                            json.dumps({'type': 'ai-title', 'aiTitle': f'Old name {index}'}) + '\n' +
                            json.dumps({'type': 'ai-title', 'aiTitle': f'Named task {index}'}) + '\n')
            os.utime(path, (1000 + index, 1000 + index))
        ui = self.ui()
        cwd = self.home / 'here'
        cwd.mkdir()
        os.chdir(cwd)
        self.addCleanup(os.chdir, ROOT)
        for source, menu_choice in (('work', 2), ('work', 0)):
            with patch.object(ui, 'menu', side_effect=[menu_choice]), \
                    patch.object(ui, 'choose_filtered', side_effect=lambda title, items, *rest: items[0]) as chooser, \
                    patch.object(ui, 'prompt', side_effect=lambda *args: args[4](args[2])) as prompt, \
                    patch.object(ui, 'operation', return_value=True) as operation, patch.object(ui, 'message'):
                ui.export_wizard()
            items = chooser.call_args.args[1]
            self.assertEqual([item['name'] for item in items], ['Named task 1', 'Named task 0'])
            self.assertEqual(Path(prompt.call_args.args[2]), cwd.resolve() / 'named-task-1.jsonl')
            self.assertEqual(operation.call_args.args[0][:3], ['export', source, 'id-1'])
        import convo_export
        self.assertIn('id-0', convo_export.search_text(items[1]))

    def test_global_menu_launches_claude_and_can_cancel_command_edit(self):
        ui = self.ui()
        with patch.object(ui, 'menu', side_effect=[0, 1, None]), \
                patch.object(ui, 'launch_code') as launch, patch.object(ui, 'prompt', return_value=None) as prompt, \
                patch.object(ui, 'operation') as operation:
            ui.global_code()
        launch.assert_called_once_with('global')
        self.assertEqual(prompt.call_args.args, ('Install command', 'Executable name', 'claude-global'))
        operation.assert_not_called()


    def test_escape_and_mouse_reports_do_not_swallow_the_next_real_key(self):
        ui = self.ui()
        ui.screen = Mock()
        ui.screen.get_wch.side_effect = ['\x1b', curses.error()]
        self.assertEqual(ui.resize_key(), '\x1b')
        self.assertEqual(ui.screen.timeout.call_args.args, (-1,))
        for report in ('\x1b[<0;12;8M', '\x1b[<0;12;8m', '\x1b[M !!', '\x1b[I', '\x1b[O'):
            ui.screen.get_wch.side_effect = [*report, '\n']
            self.assertEqual(ui.resize_key(), '\n')
        ui.screen.get_wch.side_effect = [curses.KEY_MOUSE, '\n']
        with patch.object(curses, 'getmouse') as mouse:
            self.assertEqual(ui.resize_key(), '\n')
        mouse.assert_called_once()

    def test_real_terminal_single_escape_back_mouse_and_enter_keep_app_open(self):
        mc.save_profiles({key: profile for key, profile in self.profiles.items() if key != 'main'})
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 28, 112, 0, 0))
        process = subprocess.Popen([sys.executable, str(ROOT / 'manyclaude')],
                                   env=dict(os.environ, HOME=str(self.home), MANYCLAUDE_HOME=str(self.base),
                                            TERM='xterm-256color', ESCDELAY='5000'),
                                   stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        self.addCleanup(os.close, master)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        def wait_for(text, timeout=5):
            output = b''
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], .02)
                if ready:
                    output += os.read(master, 65536)
                    if text.encode() in output:
                        return
            self.fail(f'TUI did not show {text!r}: {output!r}')
        wait_for('Your accounts')
        # The removed Sync shortcut must leave Accounts open.
        os.write(master, b's')
        os.write(master, b'\x1b')
        time.sleep(.15)
        self.assertIsNone(process.poll())
        os.write(master, b'\x1b[<0;12;8M\x1b[<0;12;8m\x1b[I\x1b[O\n')
        wait_for('Manage this account')
        self.assertIsNone(process.poll())
        os.write(master, b'jj\n')
        wait_for('current command:')
        os.write(master, b'\x1b')
        wait_for('Manage this account', timeout=.8)
        os.write(master, b'\x1b')
        wait_for('Your accounts', timeout=.8)
        os.write(master, b'g')
        wait_for('Global Claude Code')
        os.write(master, b'j\n')
        wait_for('Executable name')
        name = 'cc-global-' + uuid4().hex[:8]
        os.write(master, b'\x15' + name.encode() + b'\n')
        wait_for('Global command set to ' + name)
        self.assertTrue((self.home / '.local/bin' / name).is_file())
        os.write(master, b'j\n')
        wait_for('Executable name')
        renamed = name + '-renamed'
        os.write(master, b'-renamed\n')
        wait_for('Global command set to ' + renamed)
        self.assertFalse((self.home / '.local/bin' / name).exists())
        self.assertTrue((self.home / '.local/bin' / renamed).is_file())
        os.write(master, b'jj\n')
        wait_for('Removed the global command')
        self.assertFalse((self.home / '.local/bin' / renamed).exists())
        os.write(master, b'\x1b')
        wait_for('Your accounts', timeout=.8)
        os.write(master, b'q')
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], .05)
            if ready:
                try:
                    os.read(master, 65536)
                except OSError:
                    break
        self.assertEqual(process.wait(timeout=5), 0)


if __name__ == '__main__':
    unittest.main()
