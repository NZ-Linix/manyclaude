import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from convo_file import ConvoFile, take

ROOT = Path(__file__).resolve().parents[1]
SONNET = 'claude-sonnet-5-5'


def line(**entry):
    return (json.dumps(entry) + '\n').encode()


class ConvoFileCLITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.base = self.home / 'manager'
        self.base.mkdir()
        self.work = self.home / '.claude-work'
        (self.work / 'projects').mkdir(parents=True)
        (self.work / 'projects' / 'untouched.jsonl').write_text('normal history\n')
        profiles = {'work': {'name': 'Work', 'code': str(self.work), 'desktop': str(self.work / 'desktop'),
                             'alias': 'claude-work'}}
        (self.base / 'profiles.json').write_text(json.dumps(profiles))
        self.env = {**os.environ, 'HOME': str(self.home), 'MANYCLAUDE_HOME': str(self.base),
                    'PATH': f'{self.home}{os.pathsep}{os.defpath}'}
        # A fake Claude that appends one turn to its transcript, like a resumed session does.
        fake = self.home / 'claude'
        fake.write_text(f'''#!{sys.executable}
import json, os, re, sys
args = sys.argv[1:]
if args[:2] == ['auth', 'status']:
    print(json.dumps(dict(loggedIn=True, authMethod='claude.ai', email='work')))
elif '--input-format' in args:
    request = json.loads(sys.stdin.readline())
    models = [dict(value='sonnet', resolvedModel={SONNET!r}, displayName='Sonnet 5.5')]
    print(json.dumps(dict(type='control_response', response=dict(subtype='success',
          request_id=request['request_id'], response=dict(models=models)))))
else:
    flag = '--resume' if '--resume' in args else '--session-id'
    session = args[args.index(flag) + 1]
    project = os.path.join(os.environ['CLAUDE_CONFIG_DIR'], 'projects', re.sub(r'[^a-zA-Z0-9]', '-', os.getcwd()))
    path = os.path.join(project, session + '.jsonl')
    if flag == '--resume':
        assert os.path.exists(path), path
    os.makedirs(project, exist_ok=True)
    with open(path, 'a') as stream:
        stream.write(json.dumps(dict(sessionId=session, uuid='new', message='reply')) + '\\n')
    print(json.dumps(dict(args=args, directory=os.environ['CLAUDE_CONFIG_DIR'])))
''')
        fake.chmod(0o755)

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, ROOT / 'manyclaude', *args], text=True, capture_output=True,
                                cwd=self.home, env=self.env, stdin=subprocess.DEVNULL, timeout=20)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result.stdout if expected == 0 else result.stderr

    def test_profile_and_global_resume_and_append_to_the_file_only(self):
        convo = self.home / 'chats' / 'refactor.jsonl'
        convo.parent.mkdir()
        original = line(sessionId='abc-123', uuid='first', message='hello')
        convo.write_bytes(original)
        result = json.loads(self.cli('code', 'work', '--convo', str(convo), '-p', 'x'))
        self.assertEqual(result['args'][:2], ['--resume', 'abc-123'])
        self.assertTrue(result['directory'].startswith(str(self.base / 'file-sessions' / 'work-')))
        self.assertEqual(convo.read_bytes(), original + line(sessionId='abc-123', uuid='new', message='reply'))
        self.assertEqual(sorted(p.name for p in (self.work / 'projects').iterdir()), ['untouched.jsonl'])
        result = json.loads(self.cli('global', '--convo', str(convo), '-p', 'x'))
        self.assertTrue(result['directory'].startswith(str(self.base / 'file-sessions' / 'global-')))
        self.assertEqual(len(convo.read_bytes().splitlines()), 3)
        self.assertFalse((self.base / 'global-code' / 'projects').exists())

    def test_export_lists_and_copies_conversations_then_opens_them(self):
        project = self.work / 'projects' / re.sub(r'[^a-zA-Z0-9]', '-', str(self.home))
        project.mkdir()
        transcript = project / 'feed-1234.jsonl'
        transcript.write_bytes(line(type='user', sessionId='feed-1234', uuid='a', cwd=str(self.home),
                                    message={'role': 'user', 'content': '<command-name>/x</command-name>'})
                               + line(type='user', sessionId='feed-1234', uuid='b', cwd=str(self.home),
                                      message={'role': 'user', 'content': [{'type': 'text', 'text': 'Fix the bug'}]}))
        listing = self.cli('export', 'work')
        self.assertIn('feed-1234', listing)
        self.assertIn('Fix the bug', listing)
        self.assertIn('Fix the bug', self.cli('export', 'work', '--all'))
        output = self.home / 'out' / 'bug.jsonl'
        self.cli('export', 'work', 'feed', str(output))
        self.assertEqual(output.read_bytes(), transcript.read_bytes())
        self.assertIn('already exists', self.cli('export', 'work', 'latest', str(output), expected=1))
        self.cli('export', 'work', 'latest', str(output), '--force')
        json.loads(self.cli('code', 'work', '--convo', str(output), '-p', 'x'))
        self.assertEqual(transcript.read_bytes().count(b'\n'), 2)
        self.assertEqual(output.read_bytes().count(b'\n'), 3)
        self.assertIn('No conversations', self.cli('export', 'global'))
        with open(transcript, 'ab') as stream:
            stream.write(line(type='custom-title', customTitle='Bug hunt', sessionId='feed-1234'))
        self.assertIn('Bug hunt', self.cli('export', 'work'))
        self.cli('export', '--id', 'feed-12')
        self.assertTrue((self.home / 'bug-hunt.jsonl').is_file())
        by_id = self.home / 'by-id.jsonl'
        self.cli('export', '--id', 'feed', str(by_id))
        self.assertEqual(by_id.read_bytes(), transcript.read_bytes())
        self.assertIn('No conversation', self.cli('export', '--id', 'zzzz', expected=1))
        self.assertIn('Unknown custom profile', self.cli('export', 'custom:nope', expected=1))

    def test_new_file_starts_session_and_conflicting_flags_are_rejected(self):
        convo = self.home / 'new.jsonl'
        result = json.loads(self.cli('code', 'work', '--convo', str(convo), '-p', 'x'))
        self.assertEqual(result['args'][0], '--session-id')
        self.assertIn('"reply"', convo.read_text())
        self.assertIn('remove --continue', self.cli('code', 'work', '--convo', str(convo), '--continue',
                                                    expected=1))
        self.assertIn('.jsonl', self.cli('code', 'work', '--convo', str(self.home / 'x.txt'), expected=1))


class ConvoFileUnitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.target = self.root / 'chat.jsonl'
        self.original = line(sessionId='s1', uuid='u1')
        self.target.write_bytes(self.original)
        self.convo = ConvoFile(self.target, 'work')
        self.convo.prepare(self.root / 'store', ['-p', 'x'])

    def test_take_option_stops_at_separator(self):
        self.assertEqual(take(['--convo=a.jsonl', '-p', '--', '--convo']), ('a.jsonl', ['-p', '--', '--convo']))

    def test_follows_a_forked_resume_and_ignores_unrelated_new_conversations(self):
        unrelated = self.convo.project / 'cleared.jsonl'
        unrelated.write_bytes(line(sessionId='other', uuid='x'))
        fork = self.convo.project / 's2.jsonl'
        fork.write_bytes(self.original + line(sessionId='s2', uuid='u2'))
        os.utime(unrelated, ns=(fork.stat().st_mtime_ns + 10**9,) * 2)
        self.convo.sync()
        self.assertEqual(self.target.read_bytes(), fork.read_bytes())

    def test_external_edit_is_not_overwritten(self):
        self.target.write_bytes(self.original + line(edited=True))
        with self.convo.live.open('ab') as stream:
            stream.write(line(sessionId='s1', uuid='u2'))
        self.convo.sync()
        self.assertIn(b'edited', self.target.read_bytes())
        conflict = self.root / 'chat.manyclaude-conflict.jsonl'
        self.assertIn(b'u2', conflict.read_bytes())
