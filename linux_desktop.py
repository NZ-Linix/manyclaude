"""Linux/XDG Desktop integration; uses the package-owned Claude runtime."""
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

from runtime import claude_command, tool_environment


def xdg_directory(variable, fallback):
    value = os.environ.get(variable)
    return Path(value) if value and Path(value).is_absolute() else Path.home() / fallback


def applications_dir():
    return xdg_directory('XDG_DATA_HOME', '.local/share') / 'applications'


def regular_data():
    return xdg_directory('XDG_CONFIG_HOME', '.config') / 'Claude'


def source_app(path=None):
    env = tool_environment()
    if path:
        candidate = Path(path).expanduser().absolute()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    else:
        for name in ('claude-desktop', 'claude-desktop-unofficial'):
            binary = shutil.which(name, path=env['PATH'])
            if binary:
                # Keep the package launcher, including its Linux/Wayland setup.
                return Path(binary).absolute()
    raise ValueError('Install Claude Desktop from AUR: yay -S claude-desktop; '
                     'or pass --claude-app /path/to/claude-desktop (an executable or AppImage).')


def marker(manager, key):
    return str(manager.BASE / 'profiles' / key)


def owned(manager, key, path):
    if path.is_symlink() or not path.is_file() or ('X-ManyClaude-Profile=' + marker(manager, key) + '\n') not in path.read_text():
        raise ValueError(f'Refusing to modify a launcher not owned by this profile: {path}')


def entry_string(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')


def exec_arg(value):
    # Desktop Entry Exec quoting is not shell quoting. Percent introduces field codes.
    value = str(value).replace('%', '%%')
    for char in ('\\', '"', '`', '$'):
        value = value.replace(char, '\\' + char)
    return entry_string('"' + value + '"')


def build_app(manager, key, profile):
    target = Path(profile['app'])
    if target.exists() or target.is_symlink():
        owned(manager, key, target)
    source_app(profile['source'])
    if manager.running(profile):
        raise ValueError('Quit this profile’s Claude Desktop before refreshing its launcher.')
    args = [sys.executable, str(manager.ROOT / 'manyclaude'), '_desktop-run', key]
    # An explicit home keeps pinned launchers attached to the correct registry.
    args = ['/usr/bin/env', 'MANYCLAUDE_HOME=' + str(manager.BASE), *args]
    identity = hashlib.sha256(str(manager.BASE).encode()).hexdigest()[:8] + '-' + key
    text = ('[Desktop Entry]\nType=Application\nVersion=1.0\n'
            'Name=' + entry_string(profile.get('app_name', 'Claude ' + profile['name'])) + '\n'
            'Comment=Claude Desktop account managed by ManyClaude\n'
            'Exec=' + ' '.join(exec_arg(arg) for arg in args) + '\n'
            'Icon=claude-desktop\nTerminal=false\nCategories=Office;\n'
            'StartupWMClass=manyclaude-' + identity + '\n'
            'X-ManyClaude-Profile=' + marker(manager, key) + '\n')
    manager.atomic_write(target, text, 0o755)
    refresh_database(target.parent)


def refresh_database(folder):
    binary = shutil.which('update-desktop-database')
    if binary:
        subprocess.run([binary, str(folder)], capture_output=True, check=True)


def commands():
    """Read only this user's process arguments, including forked Electron runtimes."""
    for process in Path('/proc').glob('[0-9]*'):
        try:
            if process.stat().st_uid == os.getuid():
                yield process.joinpath('cmdline').read_bytes().decode(errors='replace').rstrip('\0').split('\0')
        except OSError:
            continue


def running(profile):
    if not profile.get('regular'):
        lock_path = Path(profile['desktop']).parent / 'desktop.lock'
        if lock_path.exists():
            with lock_path.open('a+') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
    for args in commands():
        if any(a.startswith('--type=') for a in args):
            continue
        if profile.get('regular'):
            if args and (any('claude-desktop' in a for a in args[:2]) or Path(args[0]).name == 'Claude') and not any(a.startswith('--user-data-dir') for a in args):
                return True
        elif '--user-data-dir=' + profile['desktop'] in args:
            return True
    return False


def launch(manager, key, profile, background=False):
    binary = source_app(profile['source'])
    env = tool_environment()
    if background:
        log = manager.BASE / 'desktop-launch.log'
        with log.open('ab') as out:
            os.chmod(log, 0o600)
            process = subprocess.Popen([sys.executable, str(manager.ROOT / 'manyclaude'), '_desktop-run', key],
                                       env=dict(env, MANYCLAUDE_HOME=str(manager.BASE)), stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=out, start_new_session=True)
        try:
            status = process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            return
        if status:
            raise ValueError(f'Claude Desktop failed to launch. See {log}.')
        return
    if shutil.which('claude', path=env['PATH']):
        _, env = claude_command()
    args = [str(binary)]
    for variable in ('CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR', 'CLAUDE_USER_DATA_DIR'):
        env.pop(variable, None)
    manager.prepare_profile(profile)
    if profile.get('regular'):
        return subprocess.run(args, env=env, check=True)
    for variable in manager.AUTH_ENV:
        env.pop(variable, None)
    env.update(CLAUDE_CONFIG_DIR=profile['code'], CLAUDE_SECURESTORAGE_CONFIG_DIR=profile.get('code_session', profile['code']),
               CLAUDE_USER_DATA_DIR=profile['desktop'])
    identity = hashlib.sha256(str(manager.BASE).encode()).hexdigest()[:8] + '-' + key
    args += ['--user-data-dir=' + profile['desktop'], '--class=manyclaude-' + identity,
             '--name=' + profile.get('app_name', 'Claude ' + profile['name'])]
    lock_path = Path(profile['desktop']).parent / 'desktop.lock'
    with lock_path.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Electron routes this invocation to the existing profile window.
            return subprocess.run(args, env=env, check=True)
        return subprocess.run(args, env=env, check=True)
