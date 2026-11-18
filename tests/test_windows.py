import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compat
import windows_desktop


class WindowsHelperTests(unittest.TestCase):
    def test_batch_launcher_quotes_and_forwards_arguments(self):
        with patch.object(compat, 'WINDOWS', True):
            text = compat.command_script('@echo off\r\nrem marker\r\n',
                                         [r'C:\Py 3\python.exe', r'C:\Users\a%b\manyclaude', 'code', 'work'],
                                         {'MANYCLAUDE_HOME': r'C:\Users\a%b\mc'})
            self.assertEqual(compat.command_path('C:/bin', 'claude-work').name, 'claude-work.cmd')
        self.assertTrue(text.startswith('@echo off\r\nrem marker\r\nsetlocal\r\n'))
        self.assertIn('set "MANYCLAUDE_HOME=C:\\Users\\a%%b\\mc"', text)
        self.assertIn('"C:\\Py 3\\python.exe" "C:\\Users\\a%%b\\manyclaude" "code" "work" %*', text)
        self.assertTrue(text.endswith('exit /b %ERRORLEVEL%\r\n'))

    def test_posix_launcher_is_unchanged(self):
        with patch.object(compat, 'WINDOWS', False):
            text = compat.command_script('#!/bin/sh\n', ['/usr/bin/python3', '/x/manyclaude', 'code', 'w'],
                                         {'MANYCLAUDE_HOME': '/h'})
        self.assertEqual(text, '#!/bin/sh\nexec env MANYCLAUDE_HOME=/h /usr/bin/python3 /x/manyclaude code w "$@"\n')

    def test_shortcut_names_drop_reserved_characters(self):
        self.assertEqual(windows_desktop.shortcut_name('Claude A/B: "x"?'), 'Claude A-B- -x--')
        self.assertEqual(windows_desktop.shortcut_name('...'), 'Claude')

    def test_squirrel_stub_resolves_newest_app_folder(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'claude.exe').write_text('')
            for version in ('0.9.2', '0.10.1'):
                (root / ('app-' + version)).mkdir()
                (root / ('app-' + version) / 'claude.exe').write_text('')
            self.assertEqual(windows_desktop.executable(root / 'claude.exe'), root / 'app-0.10.1/claude.exe')

    def test_nonblocking_lock_reports_contention(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'x.lock'
            with path.open('a+') as first:
                compat.lock_file(first)
                if compat.WINDOWS:
                    with path.open('a+') as second, self.assertRaises(BlockingIOError):
                        compat.lock_file(second, blocking=False)


if __name__ == '__main__':
    unittest.main()
