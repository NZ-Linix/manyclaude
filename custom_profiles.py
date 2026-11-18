"""Named launchers built on Global Claude Code with their own environment, flags, and prompt picker."""
import json
import os
from pathlib import Path
import re
import shlex
import sys

PLACEHOLDER = '<MANYCLAUDE_PROMPT>'
PROFILES_FILE = 'custom-profiles.json'
FOLDERS_FILE = 'prompt-folders.json'
STORES = ('global', 'separate')
MAX_PROMPT_FILES = 2000
SKIPPED_DIRS = {'node_modules', '__pycache__'}
_UNSET = object()


def read(base):
    path = Path(base) / PROFILES_FILE
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return data if isinstance(data, dict) else {}


def read_folders(base):
    path = Path(base) / FOLDERS_FILE
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [folder for folder in data if isinstance(folder, str)] if isinstance(data, list) else []


def store_directory(base, key, item):
    return Path(base) / ('global-code' if item.get('store', 'global') == 'global' else f'custom-code/{key}')


def protected_env(manager):
    return {*manager.AUTH_ENV, *manager.PROFILE_DIR_ENV, 'ANTHROPIC_BASE_URL'}


def configure(manager, name, *, command=None, env=None, clear_env=False, flags=None, store=None):
    name = manager.validate_name(name)
    key = manager.slugify(name)
    profiles = read(manager.BASE)
    previous = profiles.get(key)
    item = dict(previous or {'name': name, 'env': {}, 'args': [], 'store': 'global'})
    item['name'] = name
    command = command.strip() if command is not None else item.get('command') or key
    if command != item.get('command'):
        manager.validate_alias(command, manager.read_profiles(), own_custom=key)
    item['command'] = command
    if clear_env:
        item['env'] = {}
    if env:
        # Like code-options, --env replaces the saved variables as a set.
        variables = {}
        for assignment in env:
            if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', assignment):
                raise ValueError('Environment variables must use KEY=VALUE.')
            field, value = assignment.split('=', 1)
            variables[field] = value
        blocked = protected_env(manager).intersection(variables)
        if blocked:
            raise ValueError('Custom profiles cannot override ' + ', '.join(sorted(blocked)) +
                             '; ManyClaude manages accounts and routing.')
        item['env'] = variables
    if flags is not None:
        item['args'] = shlex.split(flags)
    if store is not None:
        if store not in STORES:
            raise ValueError('Store must be global or separate.')
        item['store'] = store
    profiles[key] = item
    write(manager, profiles)
    try:
        manager.sync_commands(manager.read_profiles())
    except (ValueError, OSError):
        if previous is None:
            profiles.pop(key)
        else:
            profiles[key] = previous
        write(manager, profiles)
        raise
    print(f"{'Saved' if previous else 'Created'} custom profile {name}. "
          f"Executable: {manager.command_bin_dir() / command}")
    if uses_prompt(item) and not read_folders(manager.BASE):
        print('Add prompt folders with: manyclaude prompt-folders --add <folder>')


def write(manager, profiles):
    manager.atomic_write(Path(manager.BASE) / PROFILES_FILE, json.dumps(profiles, indent=2) + '\n')


def remove(manager, name):
    key = manager.slugify(name)
    profiles = read(manager.BASE)
    if key not in profiles:
        raise ValueError(f"Unknown custom profile '{name}'. Run manyclaude custom list.")
    item = profiles.pop(key)
    write(manager, profiles)
    manager.sync_commands(manager.read_profiles())
    message = f"Removed custom profile {item['name']} and its command {item['command']}."
    if item.get('store') == 'separate':
        message += f' Its separate history is kept at {store_directory(manager.BASE, key, item)}.'
    print(message)


def listing(manager):
    profiles = read(manager.BASE)
    if not profiles:
        print('No custom profiles. Create one with: manyclaude custom set "My Profile" --flags="..."')
    for key, item in profiles.items():
        print(f"{item['name']} ({key})\n  Command: {manager.command_bin_dir() / item['command']}"
              f"\n  Flags: {shlex.join(item.get('args', [])) or '(none)'}"
              f"\n  Env: {shlex.join(f'{k}={v}' for k, v in item.get('env', {}).items()) or '(none)'}"
              f"\n  History: {'shared with Global' if item.get('store', 'global') == 'global' else 'separate'}"
              f" · {store_directory(manager.BASE, key, item)}\n")
    folders = read_folders(manager.BASE)
    print('Prompt folders: ' + (', '.join(folders) if folders else '(none)'))


def configure_folders(manager, add=(), remove_folders=()):
    folders = read_folders(manager.BASE)
    for folder in add or ():
        path = Path(folder).expanduser().absolute()
        if not path.is_dir():
            raise ValueError(f'Not a folder: {path}')
        if str(path) not in folders:
            folders.append(str(path))
    for folder in remove_folders or ():
        path = str(Path(folder).expanduser().absolute())
        if path not in folders and folder not in folders:
            raise ValueError(f'Prompt folder not configured: {folder}')
        folders = [entry for entry in folders if entry not in (path, folder)]
    if add or remove_folders:
        manager.atomic_write(Path(manager.BASE) / FOLDERS_FILE, json.dumps(folders, indent=2) + '\n')
    for folder in folders:
        count = sum(len(files) for _, files in prompt_files([folder]))
        print(f'{folder}  ({count} .md file{"s" if count != 1 else ""})')
    if not folders:
        print('No prompt folders. Add one with: manyclaude prompt-folders --add <folder>')


def prompt_files(folders):
    """Return [(folder, [markdown files])] sorted by relative path, skipping hidden folders."""
    groups = []
    total = 0
    for folder in folders:
        root = Path(folder).expanduser()
        files = []
        if root.is_dir():
            for directory, subdirectories, names in os.walk(root):
                subdirectories[:] = sorted(entry for entry in subdirectories
                                           if not entry.startswith('.') and entry not in SKIPPED_DIRS)
                files.extend(Path(directory) / entry for entry in sorted(names)
                             if entry.lower().endswith('.md') and not entry.startswith('.'))
                if total + len(files) >= MAX_PROMPT_FILES:
                    break
        files = sorted(files, key=lambda path: str(path.relative_to(root)).lower())[:MAX_PROMPT_FILES - total]
        total += len(files)
        groups.append((root, files))
    return groups


def uses_prompt(item):
    return (any(PLACEHOLDER in value for value in item.get('env', {}).values())
            or any(PLACEHOLDER in value for value in item.get('args', [])))


def apply_prompt(item, path):
    """Substitute the chosen prompt; without one, drop the placeholder and the flag that takes it."""
    env, arguments = {}, []
    for field, value in item.get('env', {}).items():
        if PLACEHOLDER not in value:
            env[field] = value
        elif path is not None:
            env[field] = value.replace(PLACEHOLDER, path)
    for value in item.get('args', []):
        if PLACEHOLDER not in value:
            arguments.append(value)
        elif path is not None:
            arguments.append(value.replace(PLACEHOLDER, path))
        elif value == PLACEHOLDER and arguments and arguments[-1].startswith('-') and '=' not in arguments[-1]:
            arguments.pop()
    return env, arguments


def take_prompt_option(options):
    prompt, remaining = _UNSET, []
    values = iter(options)
    for value in values:
        if value == '--prompt':
            prompt = next(values, None)
            if not prompt:
                raise ValueError('Expected --prompt PATH.')
        elif value.startswith('--prompt='):
            prompt = value.split('=', 1)[1]
        elif value == '--no-prompt':
            prompt = None
        else:
            remaining.append(value)
    return prompt, remaining


def run(manager, key, arguments):
    from global_code import launch_code, split_separator

    profiles = read(manager.BASE)
    if key not in profiles:
        raise ValueError(f"Unknown custom profile '{key}'. Run manyclaude custom list.")
    item = profiles[key]
    options, passthrough = split_separator(arguments)
    env, defaults = item.get('env', {}), item.get('args', [])
    if uses_prompt(item):
        prompt, options = take_prompt_option(options)
        if prompt is _UNSET:
            if options[:1] in (['--help'], ['-h'], ['--version'], ['-v']) or subcommand(options):
                prompt = None
            elif not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise ValueError(f"{item['name']} needs a prompt. Pass --prompt PATH or --no-prompt.")
            else:
                from tui import pick_prompt
                prompt = pick_prompt(manager, item['name'], prompt_files(read_folders(manager.BASE)))
        if prompt is not None:
            path = Path(prompt).expanduser().absolute()
            if not path.is_file():
                raise ValueError(f'Prompt file not found: {path}')
            prompt = str(path)
        env, defaults = apply_prompt(item, prompt)
    custom = {'key': key, 'name': item['name'], 'env': env,
              'store': store_directory(manager.BASE, key, item).relative_to(manager.BASE)}
    return launch_code(manager, [*defaults, *options, *passthrough], custom=custom)


def subcommand(options):
    if not options or not re.fullmatch(r'[a-z][a-z0-9-]*', options[0]):
        return False
    from runtime import claude_command, is_claude_subcommand
    binary, env = claude_command()
    return is_claude_subcommand(binary, env, options[0])
