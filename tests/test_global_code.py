import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from global_code import digest, picker_arguments

ROOT = Path(__file__).resolve().parents[1]
SONNET = 'claude-sonnet-5-5'
OPUS = 'claude-opus-4-8'


class GlobalCodeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.base = self.home / 'manager'
        self.base.mkdir()
        self.work = self.home / '.claude-work'
        self.work.mkdir()
        profiles = {
            'main': {'name': 'Main', 'regular': True, 'code': str(self.home / '.claude')},
            'work': {'name': 'Work', 'code': str(self.work), 'desktop': str(self.work / 'desktop')},
        }
        (self.base / 'profiles.json').write_text(json.dumps(profiles))
        self.env = {**os.environ, 'HOME': str(self.home), 'MANYCLAUDE_HOME': str(self.base),
                    'PATH': f'{self.home}{os.pathsep}{os.defpath}',
                    'ANTHROPIC_API_KEY': 'caller-key', 'CLAUDE_CONFIG_DIR': '/wrong'}
        fake = self.home / 'claude'
        fake.write_text(f'''#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
config = os.getenv('CLAUDE_CONFIG_DIR')
if args[:2] == ['auth', 'status']:
    print(json.dumps(dict(loggedIn=True, authMethod='claude.ai', email='work' if config else 'main')))
elif '--input-format' in args:
    request = json.loads(sys.stdin.readline())
    variant = dict(value={OPUS!r}, displayName='Opus 4.8')
    models = [dict(value='default', resolvedModel={SONNET!r}, displayName='Default'),
              dict(value='sonnet', resolvedModel={SONNET!r}, displayName='Sonnet 5.5')]
    if config:
        models.append(variant)
    print(json.dumps(dict(type='control_response', response=dict(subtype='success',
          request_id=request['request_id'], response=dict(models=models)))))
else:
    print(json.dumps(dict(args=args, directory=config, api_key=os.getenv('ANTHROPIC_API_KEY'))))
''')
        fake.chmod(0o755)

    def cli(self, *args, input=None, expected=0):
        result = subprocess.run([sys.executable, ROOT / 'manyclaude', *args], text=True, capture_output=True,
                                cwd=self.home, env=self.env, input=input, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result.stdout

    def observe(self, account, short, weekly, resets, warm=False):
        identity = digest(account, account, None, 'claude.ai')
        data = {
            'rate_limits': {
                'five_hour': {'used_percentage': short, 'resets_at': resets[0]},
                'seven_day': {'used_percentage': weekly, 'resets_at': resets[1]},
            },
            'model': {'id': SONNET},
            'workspace': {'project_dir': str(self.home.resolve())},
            'prompt_cache': {'warm': warm, 'expires_at': time.time() + 300},
        }
        self.assertEqual(self.cli('_global-status', identity, input=json.dumps(data)), '')

    def test_discovery_pin_and_pass_through(self):
        listing = self.cli('global', '--list-models')
        self.assertIn(f'{SONNET}\tSonnet 5.5  ·  Auto → Main', listing)
        self.assertIn(f'{SONNET}@main\tSonnet 5.5 (Main)', listing)
        self.assertIn(f'{OPUS}@work\tOpus 4.8 (Work)', listing)
        self.assertNotIn(f'{OPUS}@main', listing)
        self.assertNotIn('Default', listing)
        result = json.loads(self.cli('global', '--model=sonnet@Work', '-p', 'prompt with spaces'))
        self.assertEqual(result['directory'], str(self.base / 'global-code'))
        self.assertEqual(result['args'], ['--model', SONNET, '-p', 'prompt with spaces'])
        self.assertIsNone(result['api_key'])
        self.cli('global', '--model', f'{OPUS}@main', expected=1)
        result = json.loads(self.cli('global', '--continue'))
        self.assertEqual(result['directory'], str(self.base / 'global-code'))
        self.assertIsNone(result['api_key'])
        self.assertEqual(result['args'], ['--continue'])

    def test_auto_prefers_warmth_and_resets_and_skips_either_exhausted_window(self):
        now = time.time()
        self.observe('main', 25, 24, (now + 100, now + 200))
        self.observe('work', 26, 24, (now + 150, now + 200))
        self.assertIn('Auto → Main', self.cli('global', '--list-models').splitlines()[0])
        self.observe('work', 26, 24, (now + 150, now + 200), warm=True)
        self.assertIn('Auto → Work', self.cli('global', '--list-models').splitlines()[0])
        self.observe('work', 26, 100, (now + 150, now + 200), warm=True)
        self.assertIn('Auto → Main', self.cli('global', '--list-models').splitlines()[0])
        self.observe('work', 27, 25, (now + 150, now + 200), warm=True)
        self.assertIn('Auto → Main', self.cli('global', '--list-models').splitlines()[0])
        self.observe('main', 100, 24, (now + 100, now + 200))
        self.cli('global', '--model', SONNET, expected=1)
        result = json.loads(self.cli('global', '--model', f'{SONNET}@work'))
        self.assertEqual(result['directory'], str(self.base / 'global-code'))

    def test_existing_status_line_and_settings_arguments_are_preserved(self):
        project_settings = self.home / '.claude'
        project_settings.mkdir()
        (project_settings / 'settings.local.json').write_text('{"statusLine":{"type":"command","command":"date"}}')
        result = json.loads(self.cli('global', '--profile', 'work', 'prompt'))
        self.assertEqual(result['args'], ['prompt'])
        (project_settings / 'settings.local.json').unlink()
        settings = '{"permissions":{"defaultMode":"plan"}}'
        result = json.loads(self.cli('global', '--profile', 'work', '--settings', settings, 'prompt'))
        self.assertEqual(result['args'], ['--settings', settings, 'prompt'])

    def test_global_starts_regular_profile_with_its_default_model(self):
        result = json.loads(self.cli('global'))
        self.assertEqual(result['directory'], str(self.base / 'global-code'))
        self.assertNotIn('--model', result['args'])
        self.assertNotIn('/model', result['args'])

    def test_global_store_does_not_import_existing_transcripts_or_share_model_settings(self):
        regular = self.home / '.claude'
        projects = regular / 'projects'
        projects.mkdir(parents=True)
        (projects / 'existing.jsonl').write_text('regular history')
        (regular / 'settings.json').write_text('{"model":"sonnet"}')
        self.cli('global', '--continue')
        store = self.base / 'global-code'
        self.assertFalse((store / 'projects').exists())
        self.assertEqual(json.loads((store / 'settings.json').read_text()), {'model': 'sonnet'})
        (store / 'settings.json').write_text('{"model":"opus"}')
        self.assertEqual(json.loads((regular / 'settings.json').read_text()), {'model': 'sonnet'})

    def test_global_settings_catch_up_and_inherit_later_changes(self):
        regular = self.home / '.claude'
        regular.mkdir()
        source = regular / 'settings.json'
        store = self.base / 'global-code'
        store.mkdir()
        path = store / 'settings.json'
        path.write_text(json.dumps({'model': f'manyclaude/{OPUS}@work',
                                    'env': {'UPDATED': 'old', 'LOCAL': 'keep'}}))
        source.write_text(json.dumps({'model': 'sonnet', 'env': {'UPDATED': 'new', 'REMOVE': 'yes'},
                                      'theme': 'dark'}))
        self.cli('global')
        self.assertEqual(json.loads(path.read_text()), {
            'model': f'{OPUS}@work', 'env': {'UPDATED': 'new', 'LOCAL': 'keep', 'REMOVE': 'yes'}, 'theme': 'dark',
        })
        local = json.loads(path.read_text())
        local['theme'] = 'light'
        path.write_text(json.dumps(local))
        source.write_text(json.dumps({'model': 'haiku', 'env': {'UPDATED': 'newer', 'ADDED': 'yes'},
                                      'theme': 'dark-daltonized'}))
        self.cli('global', '--profile', 'work', '--continue')
        self.assertEqual(json.loads(path.read_text()), {
            'model': f'{OPUS}@work', 'env': {'UPDATED': 'newer', 'LOCAL': 'keep', 'ADDED': 'yes'}, 'theme': 'light',
        })
        source.write_text('{}')
        self.cli('global')
        self.assertEqual(json.loads(path.read_text()), {
            'model': f'{OPUS}@work', 'env': {'LOCAL': 'keep'}, 'theme': 'light',
        })
        settings = json.loads(path.read_text())
        settings['model'] = f'manyclaude/{OPUS}'
        path.write_text(json.dumps(settings))
        self.cli('global')
        self.assertEqual(json.loads(path.read_text())['model'], OPUS)

    def test_picker_adds_native_models_and_preserves_settings(self):
        profile = {'code': str(self.work)}
        (self.work / 'settings.json').write_text(json.dumps({
            'modelPicker': {'options': [{'model': OPUS, 'label': 'My Opus'}]},
        }))
        flags = {'permissions': {'defaultMode': 'plan'}, 'hooks': {'PreModelSwitch': []}}
        args = picker_arguments(profile, ['--settings', json.dumps(flags), 'prompt'],
                                {OPUS: 'Opus 4.8', SONNET: 'Sonnet 5.5'})
        self.assertEqual(args[2:], ['prompt'])
        settings = json.loads(args[1])
        self.assertNotIn('statusLine', settings)
        for key, value in flags.items():
            self.assertEqual(settings[key], value)
        self.assertEqual(settings['modelPicker']['options'], [
            {'model': OPUS, 'label': 'My Opus'}, {'model': SONNET, 'label': 'Sonnet 5.5'},
        ])

    def test_old_picker_hook_blocks_routing_without_requesting_a_restart(self):
        selection = self.base / 'selection'
        event = {'requested_model': 'manyclaude-route-0'}
        self.cli('_global-selection', str(selection), input=json.dumps(event), expected=2)
        self.assertFalse(selection.exists())

    def test_global_picker_uses_only_routed_rows(self):
        profile = {'code': str(self.work)}
        settings = {'modelPicker': {'options': [{'model': 'opus', 'label': 'Custom Opus'}]}}
        routes = [{'model': SONNET}, {'model': f'{SONNET}@work'}]
        args = picker_arguments(profile, ['--settings', json.dumps(settings)], {}, routes=routes)
        self.assertEqual(json.loads(args[1])['modelPicker'], {
            'options': routes, 'replaceBuiltInOptions': True,
        })

    def test_routed_subagents_are_advertised_and_preserve_custom_agents_in_acp(self):
        model = f'{SONNET}@work'
        custom = {'reviewer': {'description': 'Review changes', 'prompt': 'Check diffs.'}}
        path = self.home / 'agents.json'
        path.write_text(json.dumps(custom))
        args = picker_arguments({'code': str(self.work)}, ['-p', '--agents', str(path)], {},
                                routes=[{'model': SONNET}, {'model': model}],
                                efforts={model: ['low', 'medium', 'high', 'xhigh', 'max']}, sdk=True)
        agents = json.loads(args[args.index('--agents') + 1])
        self.assertEqual(agents['reviewer'], custom['reviewer'])
        self.assertEqual(agents[SONNET]['model'], SONNET)
        self.assertEqual(agents[model]['model'], model)
        self.assertIn(model, agents[model]['description'])
        self.assertIn('omit', agents[model]['description'])
        self.assertIn('Set Agent effort for this task when useful: low, medium, high, xhigh, max.',
                      agents[model]['description'])
        self.assertNotIn('Agent effort', agents[SONNET]['description'])
        self.assertEqual(args.count('--agents'), 1)
        self.assertIn('-p', args)
