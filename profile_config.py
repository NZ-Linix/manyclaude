"""Synchronize user-owned Claude configuration without sharing account stores."""
import hashlib
import json
from pathlib import Path
import stat

STATE = 'profile-config-sync.json'
ASSET_DIRS = ('agents', 'commands', 'hooks', 'output-styles', 'rules', 'skills')
CODE_PREFERENCES = {'autoUpdates', 'claudeInChromeDefaultEnabled', 'deepLinkTerminal',
                    'lspRecommendationDisabled', 'theme', 'verbose'}
DESKTOP_PREFERENCES = {'dictationShortcut', 'menuBarEnabled', 'quickEntryShortcut', 'sidebarMode'}
AUTH_SETTINGS = {'apiKeyHelper', 'awsAuthRefresh', 'awsCredentialExport', 'gcpAuthRefresh',
                 'otelHeadersHelper', 'forceLoginMethod', 'forceLoginOrgUUID',
                 'skipDangerousModePermissionPrompt', 'skipAutoPermissionPrompt'}
PRIVATE_ENV = {'CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR', 'CLAUDE_USER_DATA_DIR',
               'ANTHROPIC_BASE_URL', 'ANTHROPIC_CUSTOM_HEADERS', 'CLAUDE_CODE_USE_BEDROCK',
               'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CODE_USE_VERTEX', 'AWS_PROFILE'}
SPLIT_SETTINGS = {'env', 'enabledPlugins', 'extraKnownMarketplaces', 'hooks', 'modelSettings', 'permissions'}
MISSING = object()


def config_path(profile):
    code = Path(profile['code'])
    legacy = code / '.config.json'
    if legacy.exists() or legacy.is_symlink():
        return legacy
    return code.parent / '.claude.json' if profile.get('regular') else code / '.claude.json'


def read_file(path):
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError(f'Expected a regular configuration file at {path}.')
        return path.read_bytes()
    except FileNotFoundError:
        return None


def object_data(raw, path):
    try:
        data = json.loads(raw) if raw is not None else {}
    except (ValueError, UnicodeError) as error:
        raise ValueError(f'Could not read configuration JSON at {path}.') from error
    if not isinstance(data, dict):
        raise ValueError(f'Expected a configuration object at {path}.')
    return data


def entries(manager, kind, data):
    result = {}

    def add_map(prefix, mapping, allowed=lambda key: True):
        if isinstance(mapping, dict):
            result.update({(*prefix, key): value for key, value in mapping.items() if allowed(key)})

    if kind == 'settings':
        add_map((), data, lambda key: key not in AUTH_SETTINGS | SPLIT_SETTINGS)
        for key in SPLIT_SETTINGS - {'env'}:
            add_map((key,), data.get(key))
        private_env = PRIVATE_ENV.union(manager.AUTH_ENV)
        add_map(('env',), data.get('env'), lambda key: key not in private_env and not any(
            word in key.upper() for word in ('TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL', 'API_KEY')))
    elif kind == 'code':
        add_map((), data, CODE_PREFERENCES.__contains__)
        add_map(('mcpServers',), data.get('mcpServers'))
        projects = data.get('projects')
        if isinstance(projects, dict):
            for project, settings in projects.items():
                if isinstance(settings, dict):
                    add_map(('projects', project, 'mcpServers'), settings.get('mcpServers'))
    else:
        add_map(('mcpServers',), data.get('mcpServers'))
        add_map(('preferences',), data.get('preferences'), DESKTOP_PREFERENCES.__contains__)
    return result


def fingerprint(value):
    if value is MISSING:
        return None
    if isinstance(value, tuple):
        content, mode = value
        raw = bytes([mode >> 6, mode & 63]) + content
    else:
        raw = json.dumps(value, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def reconcile(snapshots, previous, known, blocked=()):
    merged, hashes, conflicts = {}, {}, []
    for entry in sorted(set().union(*snapshots.values())):
        identifier = json.dumps(entry)
        old = previous.get(identifier)
        current = [(key, values.get(entry, MISSING)) for key, values in snapshots.items()
                   if (key, entry) not in blocked]
        changes = [value for key, value in current if key in known and fingerprint(value) != old]
        present = [value for _, value in current if value is not MISSING]
        established = [value for key, value in current if key in known and value is not MISSING]
        candidates = changes or (established if old and established else present)
        selected = candidates[0]
        if any(fingerprint(value) != fingerprint(selected) for value in candidates[1:]):
            conflicts.append(entry)
        if selected is not MISSING:
            merged[entry] = selected
            hashes[identifier] = fingerprint(selected)
    return merged, hashes, conflicts


def update_json(data, before, after):
    for entry in sorted(before.keys() | after.keys()):
        value = after.get(entry, MISSING)
        if before.get(entry, MISSING) == value:
            continue
        node = data
        for key in entry[:-1]:
            node = node.setdefault(key, {})
            if not isinstance(node, dict):
                raise ValueError(f'Expected an object at configuration key {key!r}.')
        if value is MISSING:
            del node[entry[-1]]
        else:
            node[entry[-1]] = value
    return json.dumps(data, indent=2, ensure_ascii=False) + '\n'


def asset_files(root):
    paths = [root / 'CLAUDE.md']
    for name in ASSET_DIRS:
        directory = root / name
        if not directory.is_symlink():
            paths.extend(directory.rglob('*'))
    for path in paths:
        relative = path.relative_to(root)
        if relative.parts[:2] == ('skills', 'synced') or 'node_modules' in relative.parts:
            continue
        if any(part.startswith('.') for part in relative.parts) or path.is_symlink() or not path.is_file():
            continue
        raw = read_file(path)
        if raw is not None:
            yield relative.parts, (raw, stat.S_IMODE(path.stat().st_mode) & 0o755)


def redirected(path, root):
    return any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(root))


def sync(manager, enable=False):
    """Caller holds the manager lock. Plan all writes before touching any profile."""
    state_path = manager.BASE / STATE
    raw_state = read_file(state_path)
    if raw_state is None and not enable:
        return
    state = object_data(raw_state, state_path)
    profiles = manager.read_profiles()
    keys = sorted(profiles, key=lambda key: (not profiles[key].get('regular'), key))
    if not keys and enable:
        raise ValueError('Create a profile before enabling configuration sync.')
    known = set(state.get('profiles', []))
    next_state = {'profiles': keys, 'hashes': {}}
    writes, conflicts = [], []
    roots = {key: Path(profiles[key]['code']) for key in keys}
    if len(set(roots.values())) != len(keys):
        raise ValueError('Configuration sync requires separate profile directories.')
    for key in keys:
        for root in (roots[key], Path(profiles[key]['desktop'])):
            boundary = Path.home() if root.is_relative_to(Path.home()) else Path(root.anchor)
            if redirected(root, boundary):
                raise ValueError(f'Refusing to sync a redirected profile directory: {root}.')

    def merge(kind, snapshots, blocked=()):
        merged, hashes, collisions = reconcile(snapshots, state.get('hashes', {}).get(kind, {}), known, blocked)
        next_state['hashes'][kind] = hashes
        conflicts.extend(f'{kind}: {"/".join(entry)}' for entry in collisions)
        return merged

    for kind in ('settings', 'code', 'desktop'):
        if kind == 'settings':
            paths = {key: roots[key] / 'settings.json' for key in keys}
        elif kind == 'code':
            paths = {key: config_path(profiles[key]) for key in keys}
        else:
            paths = {key: Path(profiles[key]['desktop']) / 'claude_desktop_config.json' for key in keys}
        if len(set(paths.values())) != len(keys):
            raise ValueError(f'Configuration sync requires separate {kind} files for each profile.')
        raw = {key: read_file(path) for key, path in paths.items()}
        data = {key: object_data(raw[key], path) for key, path in paths.items()}
        snapshots = {key: entries(manager, kind, data[key]) for key in keys}
        merged = merge(kind, snapshots)
        for key in keys:
            if snapshots[key] != merged:
                content = update_json(data[key], snapshots[key], merged)
                writes.append((paths[key], raw[key], content, 0o600))

    snapshots = {key: dict(asset_files(root)) for key, root in roots.items()}
    asset_entries = set().union(*snapshots.values())
    blocked = {(key, entry) for key, root in roots.items() for entry in asset_entries
               if redirected(root.joinpath(*entry), root)}
    merged = merge('files', snapshots, blocked)
    for key, root in roots.items():
        before = snapshots[key]
        for entry in sorted(before.keys() | merged.keys()):
            if (key, entry) not in blocked and before.get(entry) != merged.get(entry):
                content, mode = merged.get(entry, (None, 0o600))
                old_content, _ = before.get(entry, (None, 0o600))
                writes.append((root.joinpath(*entry), old_content, content, mode))

    for path, old, _, _ in writes:
        if read_file(path) != old:
            raise ValueError(f'{path} changed during configuration sync. Retry the sync.')
    for path, old, content, mode in writes:
        if read_file(path) != old:
            raise ValueError(f'{path} changed during configuration sync. Retry the sync.')
        if content is None:
            path.unlink()
        else:
            manager.atomic_write(path, content, mode)
    if next_state != state:
        manager.atomic_write(state_path, json.dumps(next_state, indent=2) + '\n')
    return conflicts


def forget_profile(manager, key):
    path = manager.BASE / STATE
    raw = read_file(path)
    if raw is not None:
        state = object_data(raw, path)
        state['profiles'] = [profile for profile in state['profiles'] if profile != key]
        manager.atomic_write(path, json.dumps(state, indent=2) + '\n')
