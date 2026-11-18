"""Choose a model and account before starting native Claude Code."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from runtime import claude_command, is_claude_subcommand

LAUNCHER = 'global-launcher'
LEGACY_ROUTE_PREFIX = 'manyclaude/'
WINDOWS = ('five_hour', 'seven_day')
USAGE_MAX_AGE = 15 * 60
EFFORT_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max')


def launcher_name(base):
    path = base / LAUNCHER
    if path.exists():
        name = path.read_text().strip()
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name):
            return name
        raise ValueError(f'Invalid global command name in {path}.')
    return None


def configure_launcher(manager, name, disable):
    if name and disable:
        raise ValueError('Use a new command name or --disable.')
    previous = launcher_name(manager.BASE)
    path = manager.BASE / LAUNCHER
    profiles = manager.read_profiles()
    if disable:
        path.unlink(missing_ok=True)
    else:
        name = name or 'claude-global'
        if name != previous:
            manager.validate_alias(name, profiles)
        manager.atomic_write(path, name + '\n')
    try:
        manager.sync_commands(profiles)
    except (ValueError, OSError):
        if previous:
            manager.atomic_write(path, previous + '\n')
        else:
            path.unlink(missing_ok=True)
        raise
    print('Removed the global command.' if disable else f'Executable: {manager.command_bin_dir() / name}')


def json_object(raw):
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_json(path):
    try:
        return json_object(path.read_text())
    except (OSError, UnicodeError):
        return {}


def digest(*values):
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def discover(manager, binary, env, cwd, key, profile):
    env = manager.code_environment(profile, env, own_login=True)
    try:
        auth = subprocess.run([binary, 'auth', 'status', '--json'], env=env, cwd=cwd,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=8)
        account = json_object(auth.stdout)
        if auth.returncode or not account.get('loggedIn') or account.get('authMethod') != 'claude.ai':
            return key, {}, {}, 'sign in with its Code command'
        request_id = 'manyclaude-models'
        request = {'type': 'control_request', 'request_id': request_id, 'request': {'subtype': 'initialize'}}
        # The initialize exchange returns picker options without sending a user prompt.
        result = subprocess.run([binary, '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
                                 '--verbose', '--safe-mode', '--setting-sources', 'user', '--tools', '',
                                 '--strict-mcp-config', '--no-session-persistence'],
                                input=(json.dumps(request) + '\n').encode(), env=env, cwd=cwd,
                                capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        return key, {}, {}, 'Claude Code did not respond'
    models, aliases, effort_levels = {}, {}, {}
    for line in result.stdout.splitlines():
        reply = json_object(line)
        response = reply.get('response')
        if (reply.get('type') != 'control_response' or not isinstance(response, dict)
                or response.get('request_id') != request_id or response.get('subtype') != 'success'):
            continue
        for row in response.get('response', {}).get('models', []) or []:
            value = row.get('value')
            model = row.get('resolvedModel') or value
            if not isinstance(model, str) or value in ('default', 'opusplan'):
                continue
            label = row.get('displayName') or model
            models.setdefault(model, label)
            levels = row.get('supportedEffortLevels')
            if isinstance(levels, list):
                effort_levels[model] = [level for level in EFFORT_LEVELS if level in levels]
            if isinstance(value, str) and value != model:
                aliases[value] = model
        break
    if not models:
        return key, {}, {}, 'Claude Code returned no models'
    identity = digest(key, account.get('email'), account.get('orgId'), account.get('authMethod'))
    usage = read_json(manager.BASE / 'global-usage' / (identity + '.json'))
    return key, models, {**usage, 'identity': identity, 'aliases': aliases, 'effort_levels': effort_levels}, None


def active_windows(usage, now):
    return {
        name: window for name, window in usage.get('windows', {}).items()
        if window['resets_at'] > now
        and (window['used_percentage'] >= 100 or now - window['observed_at'] <= USAGE_MAX_AGE)
    }


def route_score(usage, workspace, model, now):
    windows = active_windows(usage, now)
    utilization = max((window['used_percentage'] for window in windows.values()), default=50)
    if utilization >= 100:
        return None
    cache = usage.get('cache', {}).get(digest(workspace, model), {})
    warm = cache.get('expires_at', 0) > now
    # Group similar headroom so cache and an imminent reset can break close calls.
    bottleneck_reset = min((window['resets_at'] for window in windows.values()
                            if window['used_percentage'] == utilization), default=math.inf)
    next_reset = min((window['resets_at'] for window in windows.values()), default=math.inf)
    return int(utilization // 10), -int(warm), bottleneck_reset, next_reset, utilization, -len(windows)


def eligible_accounts(accounts, usage, workspace, model, now):
    ranks = [(route_score(usage[key], workspace, model, now), key) for key in accounts]
    return [key for _, key in sorted((score, key) for score, key in ranks if score is not None)]


def status_line(manager, identity, _previous_name=None):
    save_usage(manager, identity, json_object(sys.stdin.read()))


def save_usage(manager, identity, data):
    if not re.fullmatch(r'[0-9a-f]{64}', identity):
        raise ValueError('Invalid usage cache identifier.')
    now = time.time()
    path = manager.BASE / 'global-usage' / (identity + '.json')
    rate_limits = data.get('rate_limits') or {}
    cache = data.get('prompt_cache') or {}
    model = data.get('model') or {}
    workspace = data.get('workspace') or {}
    with (manager.BASE / '.manager.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(path)
        windows = state.setdefault('windows', {})
        cached = state.setdefault('cache', {})
        for key in WINDOWS:
            limit = rate_limits.get(key) or {}
            used, reset = limit.get('used_percentage'), limit.get('resets_at')
            previous = windows.get(key, {})
            if (isinstance(used, (float, int)) and 0 <= used <= 100
                    and isinstance(reset, (float, int)) and math.isfinite(reset) and reset > now
                    and (reset, used) >= (previous.get('resets_at', 0), previous.get('used_percentage', 0))):
                windows[key] = {'used_percentage': used, 'resets_at': reset, 'observed_at': now}
        cwd, model_id = workspace.get('project_dir'), model.get('id')
        if cwd and model_id and 'warm' in cache:
            key = digest(str(Path(cwd).resolve()), model_id)
            expiry = cache.get('expires_at')
            if cache['warm'] and isinstance(expiry, (float, int)) and math.isfinite(expiry) and expiry > now:
                if expiry >= cached.get(key, {}).get('expires_at', 0):
                    cached[key] = {'expires_at': expiry, 'session_id': data.get('session_id')}
            elif cached.get(key, {}).get('session_id') == data.get('session_id'):
                cached.pop(key, None)
        state['cache'] = {key: entry for key, entry in cached.items() if entry['expires_at'] > now}
        if windows or state['cache'] or path.exists():
            manager.atomic_write(path, json.dumps(state))


def split_separator(arguments):
    try:
        i = arguments.index('--')
    except ValueError:
        i = len(arguments)
    return arguments[:i], arguments[i:]


def refresh_user_settings(manager, store, source):
    from profile_config import MISSING, object_data, read_file

    path = store / 'settings.json'
    baseline_path = store / '.manyclaude-inherited-settings.json'
    source_raw = read_file(source.resolve())
    local_raw = read_file(path)
    baseline_raw = read_file(baseline_path)
    incoming = object_data(source_raw, source)
    local = object_data(local_raw, path)
    baseline = object_data(baseline_raw, baseline_path)

    def merge(current, previous, updated):
        if updated == previous:
            return current
        if current == previous:
            return updated
        if isinstance(current, dict) and (isinstance(updated, dict)
                                          or updated is MISSING and isinstance(previous, dict)):
            removed = updated is MISSING
            updated = {} if removed else updated
            previous = previous if isinstance(previous, dict) else {}
            result = {}
            for key in dict.fromkeys((*current, *updated)):
                value = merge(current.get(key, MISSING), previous.get(key, MISSING), updated.get(key, MISSING))
                if value is not MISSING:
                    result[key] = value
            return MISSING if removed and not result else result
        # The first refresh has no common baseline for pre-existing stores.
        return updated if baseline_raw is None else current

    settings = (incoming if local_raw is None else merge(local, baseline, incoming)).copy()
    if local_raw is not None:
        if 'model' in local:
            settings['model'] = local['model']
        else:
            settings.pop('model', None)
    if isinstance(settings.get('model'), str):
        settings['model'] = settings['model'].removeprefix(LEGACY_ROUTE_PREFIX)
    if settings != local or (local_raw is None and source_raw is not None):
        manager.atomic_write(path, json.dumps(settings, indent=2) + '\n')
    if baseline_raw is None or incoming != baseline:
        manager.atomic_write(baseline_path, json.dumps(incoming, indent=2) + '\n')


def session_store(manager, profile, directory='global-code', settings=None):
    from profile_config import ASSET_DIRS, config_path

    store = manager.BASE / directory
    with (manager.BASE / '.manager.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        store.mkdir(mode=0o700, exist_ok=True)
        source = Path(profile['code'])
        regular_settings = Path.home() / '.claude/settings.json'
        if settings is None:
            settings = regular_settings if regular_settings.exists() else source / 'settings.json'
        refresh_user_settings(manager, store, settings)
        target = store / '.claude.json'
        origin = config_path(profile)
        if not target.exists() and origin.is_file():
            manager.atomic_write(target, origin.read_bytes())
        for name in (*ASSET_DIRS, 'CLAUDE.md', 'keybindings.json'):
            origin = source / name
            if origin.exists():
                try:
                    (store / name).symlink_to(origin, target_is_directory=origin.is_dir())
                except FileExistsError:
                    pass
        if sys.platform.startswith('linux'):
            credentials = source / '.credentials.json'
            link = store / '.credentials.json'
            if credentials.is_file() and not os.path.lexists(link):
                link.symlink_to(credentials)
    return store


def picker_arguments(profile, arguments, models, *, routes=(), efforts=None, gateway_env=None, sdk=False):
    options, passthrough = split_separator(arguments)
    if not sdk and any(arg.partition('=')[0] in {'--bare', '--safe-mode', '--print', '-p'} for arg in options):
        return arguments
    settings = {}
    agents = {}
    forwarded = []
    remaining = iter(options)
    for arg in remaining:
        name, equals, value = arg.partition('=')
        if name != '--settings' and not (routes and name == '--agents'):
            forwarded.append(arg)
            continue
        value = value if equals else next(remaining, '')
        raw = value if value.lstrip().startswith('{') else Path(value).expanduser().read_text()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f'Expected {name} to contain a JSON object.')
        if name == '--agents':
            agents = data
        else:
            settings = data
    picker = settings.get('modelPicker')
    if picker is None:
        picker = read_json(Path(profile['code']) / 'settings.json').get('modelPicker')
    picker = picker if isinstance(picker, dict) else {}
    previous = picker.get('options')
    previous = previous if isinstance(previous, list) else []
    configured = {row['model'] for row in previous if isinstance(row, dict) and isinstance(row.get('model'), str)}
    rows = [{'model': model, 'label': label} for model, label in models.items() if model not in configured]
    settings['modelPicker'] = ({'replaceBuiltInOptions': True, 'options': list(routes)}
                               if routes else {**picker, 'options': [*previous, *rows]})
    if gateway_env:
        settings['env'] = {**(settings.get('env') or {}), **gateway_env}
    if routes:
        for row in routes:
            model = row['model']
            account = 'the named account' if '@' in model else 'automatic account routing'
            description = (f'Use to delegate a task to {model} with {account}. '
                           'Select this subagent_type and omit the Agent tool’s model override.')
            levels = (efforts or {}).get(model, ())
            if levels:
                description += f' Set Agent effort for this task when useful: {", ".join(levels)}.'
            agents.setdefault(model, {
                'model': model,
                'description': description,
                'prompt': 'Complete the delegated task and report the result to the parent agent.',
            })
        forwarded = ['--agents', json.dumps(agents), *forwarded]
    return ['--settings', json.dumps(settings), *forwarded, *passthrough]


def model_choices(model_names, available, profiles, usage, workspace, pinned=None):
    now = time.time()
    for model, accounts in available.items():
        label = model_names[model]
        if pinned is None:
            ranked = eligible_accounts(accounts, usage, workspace, model, now)
            routed = profiles[ranked[0]]['name'] if ranked else 'all accounts at limit'
            yield model, None, label, f'Auto → {routed}'
        for key in accounts:
            if pinned is None or pinned == key:
                yield model, key, f'{label} ({profiles[key]["name"]})', 'Pinned account'


def launch(manager, arguments):
    if arguments[:1] == ['delta']:
        from acp import open_delta
        return open_delta(manager, arguments[1:])
    if arguments[:1] == ['acp']:
        from acp import start
        return start(manager, arguments[1:])
    return launch_code(manager, arguments)


def launch_code(manager, arguments, *, adapter=None, custom=None):
    options, passthrough = split_separator(arguments)
    custom = custom or {}
    directory = str(custom.get('store', 'global-code'))
    extra_env = custom.get('env') or None
    parser = argparse.ArgumentParser(prog='manyclaude ' + ('custom-run ' + custom['key'] if custom else 'global'),
                                     allow_abbrev=False,
                                     description='Pick a model across accounts and launch Claude Code.',
                                     epilog='Use glaude delta to launch Delta or glaude acp to serve ACP. '
                                            'Additional arguments pass through to Claude Code.')
    parser.add_argument('--model', metavar='MODEL[@PROFILE]', help='select a model with optional account pin')
    parser.add_argument('--profile', help='use this account for the session')
    parser.add_argument('--list-models', action='store_true', help='list model IDs and account pins')
    parser.add_argument('--convo', metavar='FILE.jsonl',
                        help='read and write this conversation file instead of the normal store')
    flags, forwarded = parser.parse_known_args(options)
    convo = None
    if flags.convo and not adapter:
        from convo_file import ConvoFile
        convo = ConvoFile(flags.convo, custom.get('key', 'global'))

    profiles = manager.read_profiles()
    if not profiles:
        raise ValueError('Create a ManyClaude profile and sign into Claude Code first.')
    requested_model, separator, model_profile = (flags.model or '').removeprefix(LEGACY_ROUTE_PREFIX).partition('@')
    if flags.model is not None and (not requested_model or separator and not model_profile):
        raise ValueError('Expected --model MODEL[@PROFILE].')
    selected_profile = flags.profile or model_profile or None
    if selected_profile:
        selected_profile = manager.slugify(selected_profile)
        if selected_profile not in profiles:
            raise ValueError(f"Unknown profile '{selected_profile}'. Run manyclaude list.")
        if model_profile and manager.slugify(model_profile) != selected_profile:
            raise ValueError('--profile and the model suffix specify different accounts.')
    binary, env = claude_command()
    subcommand = bool(not requested_model and forwarded and is_claude_subcommand(binary, env, forwarded[0]))
    interactive = (not flags.list_models and not subcommand and sys.stdin.isatty() and sys.stdout.isatty()
                   and not any(arg.partition('=')[0] in {'--bare', '--safe-mode', '--print', '-p',
                                                         '--version', '-v'} for arg in forwarded))
    keys = sorted(profiles) if adapter or interactive or not selected_profile else [selected_profile]
    result = {}
    usage = {}
    with tempfile.TemporaryDirectory(prefix='manyclaude-models-') as cwd, ThreadPoolExecutor(max_workers=4) as pool:
        probes = [pool.submit(discover, manager, binary, env, cwd, key, profiles[key]) for key in keys]
        for probe in probes:
            key, models, account, error = probe.result()
            if error:
                print(f"{profiles[key]['name']}: {error}.", file=sys.stderr)
            else:
                result[key], usage[key] = models, account
    if selected_profile and selected_profile not in result:
        raise ValueError(f"{profiles[selected_profile]['name']} did not return a model lineup.")
    if not result:
        raise ValueError('No signed-in profiles returned a model lineup.')

    model_names = {}
    available = {}
    for key, models in result.items():
        for model, label in models.items():
            model_names.setdefault(model, label)
            available.setdefault(model, []).append(key)
    workspace = str(Path.cwd().resolve())
    if flags.list_models:
        for model, key, label, description in model_choices(model_names, available, profiles, usage, workspace,
                                                            selected_profile):
            if key is None:
                label += f'  ·  {description}'
            print(f'{model + "@" + key if key else model}\t{label}')
        return

    pinned = selected_profile
    allowed_models = result[selected_profile] if selected_profile else available
    if requested_model and requested_model not in allowed_models:
        accounts = [usage[selected_profile]] if selected_profile else usage.values()
        targets = {account['aliases'][requested_model] for account in accounts
                   if requested_model in account['aliases']}
        if len(targets) != 1:
            raise ValueError(f"Specify a model ID for '{requested_model}'. Run with --list-models to see the lineup.")
        requested_model = targets.pop()
    if selected_profile is None:
        if requested_model:
            ranked = eligible_accounts(available[requested_model], usage, workspace, requested_model, time.time())
            if not ranked:
                raise ValueError(f"All accounts reporting access to {model_names[requested_model]} are at a usage limit.")
            selected_profile = ranked[0]
        else:
            selected_profile = next((key for key in result if profiles[key].get('regular')), next(iter(result)))

    profile = profiles[selected_profile]
    if requested_model and not interactive:
        forwarded = ['--model', requested_model, *forwarded]
        print(f"ManyClaude → {profile['name']} · {model_names[requested_model]}", file=sys.stderr)
    arguments = [*forwarded, *passthrough]
    if convo:
        if subcommand:
            raise ValueError('--convo cannot be combined with a Claude Code subcommand.')
        # The file's private store inherits the launcher's own settings, including its /model default.
        settings = manager.BASE / directory / 'settings.json'
        convo_store = session_store(manager, profile, convo.store_name, settings if settings.exists() else None)
        arguments = convo.prepare(convo_store, arguments)
    if not interactive and not adapter:
        store = convo_store if convo else session_store(manager, profile, directory) if not subcommand else None
        return manager.code(selected_profile, arguments, own_login=True, session_directory=store,
                            extra_env=extra_env, runner=convo.runner() if convo else None)

    from global_gateway import GlobalGateway, route_id
    rows = []
    efforts = {}
    for model, accounts in available.items():
        automatic = route_id(model)
        rows.append({'model': automatic, 'label': model_names[model], 'description': 'Automatic account',
                     'behavesAs': model})
        account_levels = [usage[key]['effort_levels'].get(model, ()) for key in accounts]
        efforts[automatic] = [level for level in EFFORT_LEVELS if all(level in levels for levels in account_levels)]
        for key, levels in zip(accounts, account_levels):
            pinned_id = route_id(model, key)
            efforts[pinned_id] = levels
            rows.append({'model': pinned_id, 'label': f'{model_names[model]} ({profiles[key]["name"]})',
                         'description': 'Pinned account', 'behavesAs': model})
    if requested_model:
        arguments = ['--model', route_id(requested_model, pinned), *arguments]
    store = convo_store if convo else session_store(manager, profile, directory)
    session_profile = {**profile, 'code': str(store)}
    with GlobalGateway(manager, profiles, usage, available, workspace, selected_profile, rows) as gateway:
        def configure(args):
            return picker_arguments(session_profile, args, {}, routes=rows, efforts=efforts, gateway_env={
                'ANTHROPIC_BASE_URL': gateway.environment['ANTHROPIC_BASE_URL'],
            })

        if adapter:
            return adapter(binary, manager.code_environment(profile, env, own_login=True), store, profile,
                           gateway, rows, efforts)
        return manager.code(selected_profile, arguments, own_login=True, global_session=configure,
                            runner=convo.runner(gateway.run) if convo else gateway.run,
                            session_directory=store, extra_env=extra_env)
