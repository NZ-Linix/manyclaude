import base64
import importlib.machinery
import importlib.util
import json
import shlex
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import openusage

loader = importlib.machinery.SourceFileLoader('mc_usage_test', str(ROOT / 'manyclaude'))
spec = importlib.util.spec_from_loader(loader.name, loader)
mc = importlib.util.module_from_spec(spec)
loader.exec_module(mc)


class OpenUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.base = self.home / 'ManyClaude'
        self.code = self.base / 'profiles/work/code'
        self.code.mkdir(parents=True, mode=0o700)
        self.identity = {'accountUuid': '11111111-1111-4111-8111-111111111111',
                         'organizationUuid': '22222222-2222-4222-8222-222222222222',
                         'emailAddress': 'work@example.com', 'organizationName': 'Work org'}
        (self.code / '.claude.json').write_text(json.dumps({'oauthAccount': self.identity}))
        self.oauth = {'accessToken': 'fixture-access', 'refreshToken': 'never-export-this',
                      'expiresAt': 123456789, 'scopes': ['user:profile']}
        self.profiles = {'work': {'name': 'Work', 'code': str(self.code)}}
        self.manager = SimpleNamespace(BASE=self.base, atomic_write=mc.atomic_write,
                                       read_profiles=lambda: self.profiles, save_profiles=self.save_profiles,
                                       running=lambda profile: False)
        self.root = self.home / '.claude-swap-backup'
        self.addCleanup(patch.stopall)
        # These tests exercise macOS Keychain migration, even on a Linux host.
        patch.object(openusage.sys, 'platform', 'darwin').start()
        patch.object(Path, 'home', return_value=self.home).start()
        document = json.dumps({'claudeAiOauth': self.oauth, 'mcpOAuth': {'fixture': 'preserve'}})
        self.stores = {openusage.service(self.code): document, openusage.service(None): document}
        self.security = patch.object(openusage.subprocess, 'run', side_effect=self.run_security).start()

    def save_profiles(self, profiles):
        self.profiles = profiles

    def run_security(self, args, **kwargs):
        if args[0] == '/bin/ps':
            return SimpleNamespace(returncode=1, stdout='')
        if args[-1] == '-i':
            words = shlex.split(kwargs['input'])
            self.stores[words[words.index('-s') + 1]] = words[words.index('-w') + 1]
            return SimpleNamespace(returncode=0, stdout='')
        service = args[args.index('-s') + 1]
        if 'delete-generic-password' in args:
            self.stores.pop(service, None)
            return SimpleNamespace(returncode=0, stdout='')
        return SimpleNamespace(returncode=0 if service in self.stores else 44,
                               stdout=self.stores.get(service, ''))

    def registry(self):
        return json.loads((self.root / 'sequence.json').read_text())

    def test_shared_live_store_history_and_stable_slots(self):
        openusage.sync(self.manager, enable=True)
        data = self.registry()
        row = data['accounts']['1']
        self.assertEqual(row['uuid'], self.identity['accountUuid'])
        vault, session = openusage.paths(self.root, '1', row)
        self.assertFalse(vault.exists())
        self.assertEqual(session.readlink(), self.code)
        self.assertEqual(self.profiles['work']['code_session'], str(session))
        self.assertNotIn(openusage.service(self.code), self.stores)
        live = json.loads(self.stores[openusage.service(session)])
        self.assertEqual(live['claudeAiOauth']['refreshToken'], 'never-export-this')
        self.assertEqual(live['mcpOAuth'], {'fixture': 'preserve'})
        self.assertEqual(session.stat().st_mode & 0o777, 0o700)
        openusage.sync(self.manager)
        self.assertEqual(self.registry()['sequence'], ['1'])
        self.assertFalse((session / '.credentials.json').exists())
        # OpenUsage rotates the SAME item Code reads; later sync does not overwrite it.
        live['claudeAiOauth']['accessToken'] = 'renewed-by-openusage'
        self.stores[openusage.service(session)] = json.dumps(live)
        openusage.sync(self.manager)
        self.assertEqual(json.loads(openusage.read_credentials(self.profiles['work'])[0])['claudeAiOauth']['accessToken'],
                         'renewed-by-openusage')

    def test_preserves_existing_swap_account_and_disable_cleans_only_ours(self):
        self.root.mkdir()
        other = {'email': 'other@example.com', 'uuid': 'other', 'organizationUuid': 'other-org'}
        initial = {'accounts': {'5': other}, 'sequence': ['5'], 'activeAccountNumber': '5', 'custom': 'keep'}
        (self.root / 'sequence.json').write_text(json.dumps(initial))
        openusage.sync(self.manager, enable=True)
        row = self.registry()['accounts']['6']
        vault, session = openusage.paths(self.root, '6', row)
        openusage.sync(self.manager, disable=True)
        self.assertEqual(self.registry()['accounts'], {'5': other})
        self.assertEqual(self.registry()['sequence'], ['5'])
        self.assertEqual(self.registry()['activeAccountNumber'], '5')
        self.assertEqual(self.registry()['custom'], 'keep')
        self.assertFalse(vault.exists())
        self.assertFalse(session.exists())
        self.assertTrue(self.code.exists())
        self.assertFalse((self.base / 'openusage-enabled').exists())
        self.assertNotIn('code_session', self.profiles['work'])
        self.assertIn(openusage.service(self.code), self.stores)
        self.assertNotIn(openusage.service(session), self.stores)

    def test_identity_change_removes_old_token_and_profile_removal_cleans_export(self):
        openusage.sync(self.manager, enable=True)
        old_vault, _ = openusage.paths(self.root, '1', self.registry()['accounts']['1'])
        self.identity['emailAddress'] = 'new@example.com'
        self.identity['accountUuid'] = '33333333-3333-4333-8333-333333333333'
        (self.code / '.claude.json').write_text(json.dumps({'oauthAccount': self.identity}))
        openusage.sync(self.manager)
        self.assertFalse(old_vault.exists())
        self.assertEqual(len(self.registry()['accounts']), 1)
        openusage.sync(self.manager, exclude=('work',))
        self.assertEqual(self.registry()['accounts'], {})

    def test_malformed_registry_left_untouched(self):
        self.root.mkdir()
        file = self.root / 'sequence.json'
        file.write_text('{ broken')
        with self.assertRaises(ValueError):
            openusage.sync(self.manager, enable=True)
        self.assertEqual(file.read_text(), '{ broken')
        self.security.assert_not_called()

    def test_missing_login_skipped_and_file_fallback(self):
        self.stores.clear()
        messages = openusage.sync(self.manager, enable=True)
        self.assertIn('Skipped Work', messages[0])
        self.assertEqual(self.registry()['accounts'], {})
        (self.code / '.credentials.json').write_text(json.dumps({'claudeAiOauth': self.oauth}))
        openusage.sync(self.manager)
        self.assertEqual(len(self.registry()['accounts']), 1)
        row = self.registry()['accounts']['1']
        _, session = openusage.paths(self.root, '1', row)
        self.assertEqual((session / '.credentials.json').read_bytes(), (self.code / '.credentials.json').read_bytes())

    def test_duplicate_identity_not_exported_twice(self):
        self.profiles['another'] = {'name': 'Another', 'code': str(self.code)}
        openusage.sync(self.manager, enable=True)
        self.assertEqual(len(self.registry()['accounts']), 1)

    def test_regular_identity_uses_home_config_and_default_keychain(self):
        (self.home / '.claude.json').write_text(json.dumps({'oauthAccount': self.identity}))
        self.profiles = {'main': {'name': 'Main', 'code': str(self.home / '.claude'), 'regular': True}}
        openusage.sync(self.manager, enable=True)
        self.assertEqual(len(self.registry()['accounts']), 1)
        self.assertEqual(self.security.call_args.args[0][3], 'Claude Code-credentials')
        row = self.registry()['accounts']['1']
        vault, _ = openusage.paths(self.root, '1', row)
        self.assertNotIn('refreshToken', json.loads(base64.b64decode(vault.read_bytes()))['claudeAiOauth'])

    def test_upgrades_snapshot_bridge_without_copying_profile_data(self):
        self.root.mkdir()
        row = {'email': self.identity['emailAddress'], 'uuid': self.identity['accountUuid'],
               'organizationUuid': self.identity['organizationUuid'],
               'manyclaude': {'home': str(self.base), 'profile': 'work'}}
        (self.root / 'sequence.json').write_text(json.dumps({'accounts': {'2': row}, 'sequence': ['2']}))
        vault, session = openusage.paths(self.root, '2', row)
        session.mkdir(parents=True)
        (session / 'projects').symlink_to(self.code / 'projects', target_is_directory=True)
        vault.parent.mkdir()
        vault.write_bytes(b'old snapshot')
        (self.code / 'history.jsonl').write_text('fixture existing history')
        openusage.sync(self.manager, enable=True)
        self.assertTrue(session.is_symlink())
        self.assertFalse(vault.exists())
        self.assertEqual((session / 'history.jsonl').read_text(), 'fixture existing history')
        self.assertEqual(self.registry()['sequence'], ['2'])

    def test_running_profile_prevents_credential_migration(self):
        self.manager.running = lambda profile: True
        with self.assertRaisesRegex(ValueError, 'Quit Work'):
            openusage.sync(self.manager, enable=True)
        self.assertIn(openusage.service(self.code), self.stores)
        self.assertNotIn('code_session', self.profiles['work'])

    def test_failed_registry_save_keeps_original_login_and_removes_copy(self):
        self.manager.save_profiles = lambda profiles: (_ for _ in ()).throw(OSError('fixture save failure'))
        with self.assertRaises(OSError):
            openusage.sync(self.manager, enable=True)
        self.assertIn(openusage.service(self.code), self.stores)
        row = self.registry()['accounts']['1']
        _, session = openusage.paths(self.root, '1', row)
        self.assertNotIn(openusage.service(session), self.stores)

    def test_storage_keychain_copy_is_verified_and_idempotent(self):
        target = self.home / '.claude-work'
        original = self.stores[openusage.service(self.code)]
        openusage.copy_keychain_store(str(self.code), str(target))
        openusage.copy_keychain_store(str(self.code), str(target))
        self.assertEqual(self.stores[openusage.service(target)], original)
        self.assertEqual(self.stores[openusage.service(self.code)], original)
        adds = [call for call in self.security.call_args_list if call.args[0][-1] == '-i']
        self.assertEqual(len(adds), 1)
        self.assertNotIn('fixture-access', str(adds[0].args))
        openusage.delete_keychain_store(str(self.code))
        self.assertNotIn(openusage.service(self.code), self.stores)
        self.assertEqual(self.stores[openusage.service(target)], original)

    def test_storage_keychain_copy_refuses_different_destination_login(self):
        target = self.home / '.claude-work'
        self.stores[openusage.service(target)] = '{"fixture":"different login"}'
        with self.assertRaisesRegex(ValueError, 'different login'):
            openusage.copy_keychain_store(str(self.code), str(target))
        self.assertIn(openusage.service(self.code), self.stores)
        self.assertEqual(self.stores[openusage.service(target)], '{"fixture":"different login"}')

    def test_storage_keychain_copy_access_failure_keeps_original(self):
        target = self.home / '.claude-work'
        with patch.object(openusage.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='')):
            with self.assertRaisesRegex(ValueError, 'Cannot read'):
                openusage.copy_keychain_store(str(self.code), str(target))
        self.assertIn(openusage.service(self.code), self.stores)
        self.assertNotIn(openusage.service(target), self.stores)


if __name__ == '__main__':
    unittest.main()
