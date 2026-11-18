"""OpenUsage discovery with shared live stores for isolated Code profiles."""
import base64
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import shlex
import unicodedata
import uuid


def service(directory):
    if directory is None:
        return 'Claude Code-credentials'
    return 'Claude Code-credentials-' + hashlib.sha256(unicodedata.normalize('NFC', str(directory)).encode()).hexdigest()[:8]


def copy_keychain_store(old, target):
    """Verify a relocated login before the storage migration commits its registry."""
    def read(directory):
        result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', service(directory),
                                 '-a', getpass.getuser(), '-w'], capture_output=True, text=True)
        if result.returncode not in (0, 44):
            raise ValueError('Cannot read the Code Keychain store; original login retained.')
        return result.stdout.strip() if result.returncode == 0 else None
    text = read(old)
    if text is None:
        return
    existing = read(target)
    if existing is not None and json.loads(existing) != json.loads(text):
        raise ValueError('The destination Keychain store already contains a different login; original retained.')
    if existing is None:
        command = shlex.join(['add-generic-password', '-s', service(target),
                              '-a', getpass.getuser(), '-w', text]) + '\n'
        subprocess.run(['/usr/bin/security', '-i'], input=command, capture_output=True, text=True, check=True)
    copied = read(target)
    if copied is None or json.loads(copied) != json.loads(text):
        raise ValueError('Could not verify the relocated Code Keychain store; original login retained.')


def delete_keychain_store(directory):
    result = subprocess.run(['/usr/bin/security', 'delete-generic-password', '-s', service(directory),
                             '-a', getpass.getuser()], capture_output=True, text=True)
    if result.returncode not in (0, 44):
        raise ValueError('Could not remove the old Code Keychain store; retry migration.')


def read_credentials(profile):
    directory = None if profile.get('regular') else profile.get('code_session', profile['code'])
    if sys.platform == 'darwin':
        result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', service(directory),
                                 '-a', getpass.getuser(), '-w'], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip(), True
    try:
        return (Path(profile['code']) / '.credentials.json').read_text(), False
    except OSError:
        return None, False


def move_store(manager, key, profile, target):
    """Switch the config alias and move its Keychain item, never rotate a token copy."""
    old = profile.get('code_session', profile['code'])
    if old == str(target):
        return
    # A running client can still write its old Keychain item after migration.
    for file in (Path(profile['code']) / 'sessions').glob('*.json'):
        if file.stem.isdigit():
            result = subprocess.run(['/bin/ps', '-p', file.stem, '-o', 'command='], capture_output=True, text=True)
            if result.returncode == 0 and 'claude' in result.stdout.lower():
                raise ValueError(f"Quit {profile['name']}'s Claude Code sessions, then run manyclaude openusage again.")
    if manager.running(profile):
        raise ValueError(f"Quit {profile['name']}'s Desktop before changing its OpenUsage credential store.")
    text, keychain = read_credentials(profile)
    if keychain:
        text = json.dumps(json.loads(text), ensure_ascii=True, separators=(',', ':'))
        # Interactive security input avoids putting credential JSON in process arguments.
        command = shlex.join(['add-generic-password', '-U', '-s', service(target),
                              '-a', getpass.getuser(), '-w', text]) + '\n'
        subprocess.run(['/usr/bin/security', '-i'], input=command, capture_output=True, text=True, check=True)
        check, found = read_credentials(dict(profile, code_session=str(target)))
        if not found or json.loads(check) != json.loads(text):
            raise ValueError('Could not migrate the live Code Keychain store; original login retained.')
    updated = manager.read_profiles()
    if str(target) == profile['code']:
        updated[key].pop('code_session', None)
    else:
        updated[key]['code_session'] = str(target)
    try:
        manager.save_profiles(updated)
    except BaseException:
        if keychain:
            subprocess.run(['/usr/bin/security', 'delete-generic-password', '-s', service(target),
                             '-a', getpass.getuser()], capture_output=True, text=True)
        raise
    updated_profile = dict(updated[key])
    profile.clear()
    profile.update(updated_profile)
    if keychain:
        result = subprocess.run(['/usr/bin/security', 'delete-generic-password', '-s', service(old),
                                 '-a', getpass.getuser()], capture_output=True, text=True)
        if result.returncode not in (0, 44):
            raise ValueError('Live store configured, but the old Code Keychain item could not be removed.')


def account(profile):
    directory = Path(profile['code'])
    config = Path.home() / '.claude.json' if profile.get('regular') else directory / '.claude.json'
    try:
        identity = json.loads(config.read_text())['oauthAccount']
        user = str(uuid.UUID(identity['accountUuid']))
        org = str(uuid.UUID(identity['organizationUuid']))
        email = identity['emailAddress']
        if not isinstance(email, str) or not email or any(c in email for c in '/\\\0'):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    text, _ = read_credentials(profile)
    try:
        oauth = json.loads(text)['claudeAiOauth']
        if not isinstance(oauth.get('accessToken'), str) or not oauth['accessToken']:
            return None
        # Never export refresh tokens: OpenUsage must not rotate a copied login.
        snapshot = {key: oauth[key] for key in ('accessToken', 'expiresAt', 'scopes', 'subscriptionType',
                                               'rateLimitTier') if key in oauth}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {'email': email, 'uuid': user, 'organizationUuid': org,
            'organizationName': identity.get('organizationName') or profile['name']}, snapshot


def paths(root, slot, row):
    email = row['email']
    if not str(slot).isdigit() or not isinstance(email, str) or any(c in email for c in '/\\\0'):
        raise ValueError('Invalid managed OpenUsage account; refusing to alter its files.')
    slug = ''.join(c if c.isascii() and (c.isalnum() or c in '._-') else '_'
                   for c in unicodedata.normalize('NFC', email))
    return root / 'credentials' / f'.creds-{slot}-{email}.enc', root / 'sessions' / f'{slot}-{slug}'


def sync(manager, exclude=(), enable=False, disable=False):
    flag = manager.BASE / 'openusage-enabled'
    if not enable and not disable and not flag.exists():
        return []
    root = Path.home() / '.claude-swap-backup'
    if root.is_symlink():
        raise ValueError('Refusing to export through a redirected Claude-Swap folder.')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    messages = []
    with (root / '.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Claude-Swap is busy; retry OpenUsage sync shortly.')
        registry = root / 'sequence.json'
        try:
            data = json.loads(registry.read_text()) if registry.exists() else {
                'activeAccountNumber': None, 'sequence': [], 'accounts': {}}
            if (not isinstance(data, dict) or not isinstance(data.get('accounts'), dict)
                    or not isinstance(data.get('sequence'), list)
                    or not all(isinstance(row, dict) for row in data['accounts'].values())):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('Malformed Claude-Swap sequence.json; existing file left unchanged.') from None
        accounts = data['accounts']
        next_slot = max([int(n) for n in accounts if n.isdigit()] + [0]) + 1
        owner = str(manager.BASE)
        all_profiles = manager.read_profiles()
        profiles = {} if disable else {k: p for k, p in all_profiles.items() if k not in exclude}
        candidates = {}
        for key, profile in profiles.items():
            value = account(profile)
            if value:
                candidates[key] = value
            else:
                messages.append(f"Skipped {profile['name']}: sign in with its Code command, then sync again.")
        # Remove only our entries; preserve unrelated Swap accounts and history.
        for slot, row in list(accounts.items()):
            marker = row.get('manyclaude')
            if not isinstance(marker, dict) or marker.get('home') != owner:
                continue
            key = marker.get('profile')
            current = candidates.get(key)
            if current and all(row.get(f) == current[0][f] for f in ('email', 'uuid', 'organizationUuid')):
                continue
            vault, session = paths(root, slot, row)
            profile = all_profiles.get(key)
            if profile and not profile.get('regular') and profile.get('code_session') == str(session):
                move_store(manager, key, profile, profile['code'])
            if vault.parent.is_symlink() or session.parent.is_symlink():
                raise ValueError('Refusing to alter redirected Claude-Swap folders.')
            vault.unlink(missing_ok=True)
            if session.is_symlink():
                if not profile or session.readlink() != Path(profile['code']):
                    raise ValueError('Refusing to remove an unexpected OpenUsage session link.')
                session.unlink()
            else:
                projects = session / 'projects'
                if projects.is_symlink():
                    projects.unlink()
                try:
                    session.rmdir()
                except OSError:
                    pass
            del accounts[slot]
            data['sequence'] = [n for n in data['sequence'] if str(n) != slot]
            if str(data.get('activeAccountNumber')) == slot:
                data['activeAccountNumber'] = None
        for key, (identity, oauth) in candidates.items():
            matches = [(slot, row) for slot, row in accounts.items()
                       if all(row.get(f) == identity[f] for f in ('uuid', 'organizationUuid'))]
            if matches:
                slot, row = matches[0]
                if row.get('manyclaude') != {'home': owner, 'profile': key}:
                    messages.append(f"{profiles[key]['name']}: account already registered; existing entry preserved.")
                    continue
            else:
                slot = str(next_slot)
                next_slot += 1
                row = dict(identity, added=datetime.now(timezone.utc).isoformat(),
                           alias=profiles[key]['name'], manyclaude={'home': owner, 'profile': key})
                vault, session = paths(root, slot, row)
                if vault.exists() or vault.is_symlink() or session.exists() or session.is_symlink():
                    raise ValueError('Unregistered Claude-Swap files occupy the next slot; move them before syncing.')
                accounts[slot] = row
                data['sequence'].append(slot)
            row.update(identity, alias=profiles[key]['name'])
            vault, session = paths(root, slot, row)
            profile = profiles[key]
            if vault.parent.is_symlink() or session.parent.is_symlink():
                raise ValueError('Refusing to write through redirected Claude-Swap folders.')
            vault.parent.mkdir(mode=0o700, exist_ok=True)
            if not profile.get('regular'):
                # Persist the owned slot before migration so an interrupted sync can resume.
                manager.atomic_write(registry, json.dumps(data, indent=2) + '\n')
                if session.is_symlink():
                    if session.readlink() != Path(profile['code']):
                        raise ValueError('OpenUsage session points to an unexpected folder.')
                else:
                    if session.exists():
                        projects = session / 'projects'
                        if projects.is_symlink() and projects.readlink() == Path(profile['code']) / 'projects':
                            projects.unlink()
                        try:
                            session.rmdir()
                        except OSError:
                            raise ValueError('Existing Swap session contains data; refusing to replace it.') from None
                    session.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    session.symlink_to(Path(profile['code']), target_is_directory=True)
                move_store(manager, key, profile, session)
                vault.unlink(missing_ok=True)
                messages.append(f"Synced {profile['name']} with a shared live credential store.")
                continue
            if session.is_symlink():
                raise ValueError('Refusing to export regular-account snapshots through a symlink.')
            session.mkdir(mode=0o700, parents=True, exist_ok=True)
            payload = json.dumps({'claudeAiOauth': oauth}).encode()
            manager.atomic_write(vault, base64.b64encode(payload))
            projects = session / 'projects'
            if not projects.exists() and not projects.is_symlink():
                projects.symlink_to(Path(profiles[key]['code']) / 'projects', target_is_directory=True)
            messages.append(f"Synced {profiles[key]['name']}.")
        data['lastUpdated'] = datetime.now(timezone.utc).isoformat()
        manager.atomic_write(registry, json.dumps(data, indent=2) + '\n')
        if disable:
            flag.unlink(missing_ok=True)
        elif enable:
            manager.atomic_write(flag, '')
    return messages
