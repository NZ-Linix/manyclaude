import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

from custom_profiles import PLACEHOLDER, apply_prompt, prompt_files, take_prompt_option
from global_code import digest
from global_gateway import GlobalGateway

ROOT = Path(__file__).resolve().parents[1]
SONNET = 'claude-sonnet-5-5'


class CustomProfileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.base = self.home / 'manager'
        self.base.mkdir()
        self.work = self.home / '.claude-work'
        self.work.mkdir()
        profiles = {'work': {'name': 'Work', 'code': str(self.work), 'desktop': str(self.work / 'desktop'),
                             'alias': 'claude-work'}}
        (self.base / 'profiles.json').write_text(json.dumps(profiles))
        self.bin = self.home / '.local/bin'
        self.env = {**os.environ, 'HOME': str(self.home), 'MANYCLAUDE_HOME': str(self.base),
                    'PATH': f'{self.home}{os.pathsep}{os.defpath}', 'SHELL': '/bin/zsh',
                    'ANTHROPIC_API_KEY': 'caller-key', 'CALLER_WINS': 'caller'}
        fake = self.home / 'claude'
        fake.write_text(f'''#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
if args[:2] == ['auth', 'status']:
    print(json.dumps(dict(loggedIn=True, authMethod='claude.ai', email='work')))
elif '--input-format' in args:
    request = json.loads(sys.stdin.readline())
    models = [dict(value='sonnet', resolvedModel={SONNET!r}, displayName='Sonnet 5.5')]
    print(json.dumps(dict(type='control_response', response=dict(subtype='success',
          request_id=request['request_id'], response=dict(models=models)))))
else:
    print(json.dumps(dict(args=args, directory=os.getenv('CLAUDE_CONFIG_DIR'),
                          api_key=os.getenv('ANTHROPIC_API_KEY'), custom=os.getenv('CUSTOM_VALUE'),
                          prompt=os.getenv('PROMPT_PATH'), caller=os.getenv('CALLER_WINS'))))
''')
        fake.chmod(0o755)
        self.prompts = self.home / 'Prompts'
        (self.prompts / 'team').mkdir(parents=True)
        (self.prompts / '.hidden').mkdir()
        (self.prompts / 'review.md').write_text('# Review\n')
        (self.prompts / 'team' / 'writer.md').write_text('# Writer\n')
        (self.prompts / '.hidden' / 'secret.md').write_text('no')
        (self.prompts / 'notes.txt').write_text('no')

    def cli(self, *args, expected=0, executable=None):
        command = [str(executable)] if executable else [sys.executable, ROOT / 'manyclaude']
        result = subprocess.run([*command, *args], text=True, capture_output=True, cwd=self.home,
                                env=self.env, stdin=subprocess.DEVNULL, timeout=20)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result.stdout if expected == 0 else result.stderr

    def create(self, *extra):
        self.cli('prompt-folders', '--add', str(self.prompts))
        self.cli('custom', 'set', 'Custom Claude', '--command', 'custom-claude',
                 f'--flags=--system-prompt-file {PLACEHOLDER} --verbose',
                 '--env', 'CUSTOM_VALUE=yes', '--env', f'PROMPT_PATH={PLACEHOLDER}',
                 '--env', 'CALLER_WINS=profile', *extra)

    def test_executable_substitutes_prompt_env_and_flags_through_global_routing(self):
        self.create()
        executable = self.bin / 'custom-claude'
        self.assertTrue(executable.is_file())
        prompt = self.prompts / 'review.md'
        result = json.loads(self.cli('--prompt', str(prompt), '-p', 'hello there', executable=executable))
        self.assertEqual(result['args'], ['--system-prompt-file', str(prompt), '--verbose', '-p', 'hello there'])
        self.assertEqual(result['directory'], str(self.base / 'global-code'))
        self.assertEqual(result['custom'], 'yes')
        self.assertEqual(result['prompt'], str(prompt))
        self.assertEqual(result['caller'], 'caller')
        self.assertIsNone(result['api_key'])

    def test_no_prompt_drops_placeholder_and_noninteractive_launch_requires_a_choice(self):
        self.create('--store', 'separate')
        result = json.loads(self.cli('custom-run', 'custom-claude', '--no-prompt', '-p', 'x'))
        self.assertEqual(result['args'], ['--verbose', '-p', 'x'])
        self.assertIsNone(result['prompt'])
        self.assertEqual(result['directory'], str(self.base / 'custom-code' / 'custom-claude'))
        self.assertIn('--prompt PATH or --no-prompt', self.cli('custom-run', 'custom-claude', '-p', 'x', expected=1))
        self.assertIn('not found', self.cli('custom-run', 'custom-claude', '--prompt', 'missing.md', expected=1))

    def test_command_collisions_protected_env_and_removal(self):
        self.create()
        self.assertIn('belongs to Work', self.cli('custom', 'set', 'Other', '--command', 'claude-work', expected=1))
        self.assertIn('belongs to the custom profile',
                      self.cli('custom', 'set', 'Other', '--command', 'custom-claude', expected=1))
        self.assertIn('cannot override', self.cli('custom', 'set', 'Other', '--env', 'CLAUDE_CONFIG_DIR=/x',
                                                  expected=1))
        self.assertIn('belongs to the custom profile',
                      self.cli('global-alias', 'custom-claude', expected=1))
        self.assertFalse((self.bin / 'other').exists())
        self.cli('custom', 'set', 'Custom Claude', '--command', 'cc-custom')
        self.assertFalse((self.bin / 'custom-claude').exists())
        self.assertTrue((self.bin / 'cc-custom').is_file())
        listing = self.cli('custom', 'list')
        self.assertIn('cc-custom', listing)
        self.assertIn('CUSTOM_VALUE=yes', listing)
        self.cli('custom', 'remove', 'Custom Claude')
        self.assertFalse((self.bin / 'cc-custom').exists())
        self.assertEqual(json.loads((self.base / 'custom-profiles.json').read_text()), {})

    def test_prompt_files_skip_hidden_and_non_markdown(self):
        [(root, files)] = prompt_files([str(self.prompts)])
        self.assertEqual([str(path.relative_to(root)) for path in files], ['review.md', 'team/writer.md'])

    def test_apply_prompt_and_option_parsing(self):
        item = {'env': {'A': PLACEHOLDER, 'B': 'keep'},
                'args': ['--model', 'opus', '--system-prompt-file', PLACEHOLDER, f'--append={PLACEHOLDER}']}
        self.assertEqual(apply_prompt(item, '/p.md'), (
            {'A': '/p.md', 'B': 'keep'}, ['--model', 'opus', '--system-prompt-file', '/p.md', '--append=/p.md']))
        self.assertEqual(apply_prompt(item, None), ({'B': 'keep'}, ['--model', 'opus']))
        self.assertEqual(take_prompt_option(['--prompt=a.md', '-p', 'x']), ('a.md', ['-p', 'x']))
        self.assertEqual(take_prompt_option(['--no-prompt']), (None, []))


class GatewayCacheTests(unittest.TestCase):
    def gateway(self, directory, usage):
        manager = SimpleNamespace(BASE=Path(directory))
        return GlobalGateway(manager, {'main': {}, 'work': {}}, usage, {SONNET: ['main', 'work']},
                             directory, 'main', [])

    def test_conversation_stays_on_its_cache_account_while_eligible(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as directory:
            usage = {'main': {'identity': 'main', 'windows': {
                'five_hour': {'used_percentage': 10, 'resets_at': now + 900, 'observed_at': now}}},
                'work': {'identity': 'work', 'windows': {
                    'five_hour': {'used_percentage': 60, 'resets_at': now + 900, 'observed_at': now}}}}
            gateway = self.gateway(directory, usage)
            self.assertEqual(gateway.resolve(SONNET, 'session')[1][0], 'main')
            gateway.remember('session', SONNET, 'work')
            self.assertEqual(gateway.resolve(SONNET, 'session')[1], ['work', 'main'])
            self.assertEqual(gateway.resolve(SONNET, 'other')[1][0], 'main')
            usage['work']['windows']['five_hour']['used_percentage'] = 100
            self.assertEqual(gateway.resolve(SONNET, 'session')[1], ['main'])
            gateway.server.server_close()

    def test_resumed_session_prefers_account_it_warmed(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as directory:
            usage = {'main': {'identity': 'main'}, 'work': {'identity': 'work', 'cache': {
                digest(directory, SONNET): {'expires_at': now + 600, 'session_id': 'resumed'}}}}
            gateway = self.gateway(directory, usage)
            self.assertEqual(gateway.resolve(SONNET, 'resumed')[1][0], 'work')
            gateway.server.server_close()

    def test_cache_reads_keep_the_ttl_the_cache_was_written_with(self):
        with tempfile.TemporaryDirectory() as directory:
            usage = {'main': {'identity': 'a' * 64}, 'work': {'identity': 'b' * 64}}
            (Path(directory) / '.manager.lock').touch()
            manager = SimpleNamespace(BASE=Path(directory), atomic_write=lambda path, data: (
                path.parent.mkdir(parents=True, exist_ok=True), path.write_text(data)))
            gateway = GlobalGateway(manager, {'main': {}, 'work': {}}, usage, {SONNET: ['main', 'work']},
                                    directory, 'main', [])
            gateway.observe('main', SONNET, [], {'cache_creation_input_tokens': 10,
                                                 'cache_creation': {'ephemeral_1h_input_tokens': 10}}, 's')
            gateway.observe('main', SONNET, [], {'cache_read_input_tokens': 10}, 's')
            state = json.loads((Path(directory) / 'global-usage' / ('a' * 64 + '.json')).read_text())
            [entry] = state['cache'].values()
            self.assertGreater(entry['expires_at'], time.time() + 3000)
            gateway.server.server_close()
