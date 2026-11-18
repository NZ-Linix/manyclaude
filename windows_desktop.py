"""Windows Desktop integration: Start Menu shortcuts and per-profile Electron data folders."""
import os
from pathlib import Path
import re
import subprocess
import sys

from compat import lock_file, processes
from runtime import claude_command, tool_environment

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def known_folder(variable, fallback):
    value = os.environ.get(variable)
    return Path(value) if value and Path(value).is_absolute() else Path.home() / fallback


def roaming():
    return known_folder('APPDATA', 'AppData/Roaming')


def local():
    return known_folder('LOCALAPPDATA', 'AppData/Local')


def applications_dir():
    return roaming() / 'Microsoft/Windows/Start Menu/Programs/ManyClaude'


def regular_data():
    return roaming() / 'Claude'


def shortcut_name(app_name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', app_name).strip(' .') or 'Claude'


def source_app(path=None):
    candidates = [Path(path).expanduser()] if path else [
        local() / 'AnthropicClaude/claude.exe', local() / 'Programs/Claude/Claude.exe',
        local() / 'Programs/claude-desktop/Claude.exe']
    for candidate in candidates:
        if candidate.is_file() and candidate.suffix.lower() == '.exe':
            return candidate.absolute()
    raise ValueError('Install Claude Desktop from https://claude.ai/download, '
                     'or pass --claude-app C:\\path\\to\\claude.exe.')


def version_key(folder):
    return tuple(int(part) if part.isdigit() else 0 for part in re.split(r'[.-]', folder.name[4:]))


def executable(source):
    """Squirrel installs a stub that exits after starting app-<version>; run the newest app directly."""
    source = Path(source)
    versions = sorted((p for p in source.parent.glob('app-*') if (p / source.name).is_file()), key=version_key)
    return versions[-1] / source.name if versions else source


def marker(manager, key):
    return 'ManyClaude profile ' + str(manager.BASE / 'profiles' / key)


def powershell(script, *args):
    env = dict(os.environ, **{f'MANYCLAUDE_ARG{i}': str(value) for i, value in enumerate(args)})
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            env=env, capture_output=True, text=True, creationflags=NO_WINDOW)
    if result.returncode:
        raise ValueError('PowerShell failed: ' + (result.stderr.strip() or result.stdout.strip()))
    return result.stdout.strip()


def owned(manager, key, path):
    path = Path(path)
    description = None
    if path.is_file() and not path.is_symlink():
        description = powershell('(New-Object -ComObject WScript.Shell).CreateShortcut($env:MANYCLAUDE_ARG0).Description',
                                 path)
    if description != marker(manager, key):
        raise ValueError(f'Refusing to modify a shortcut not owned by this profile: {path}')


def gui_python():
    candidate = Path(sys.executable).with_name('pythonw.exe')
    return candidate if candidate.is_file() else Path(sys.executable)


def build_app(manager, key, profile):
    target = Path(profile['app'])
    if target.exists() or target.is_symlink():
        owned(manager, key, target)
    source = source_app(profile['source'])
    if manager.running(profile):
        raise ValueError('Quit this profile’s Claude Desktop before refreshing its shortcut.')
    target.parent.mkdir(parents=True, exist_ok=True)
    arguments = subprocess.list2cmdline([str(manager.ROOT / 'manyclaude'), '_desktop-run', key,
                                         '--home', str(manager.BASE)])
    powershell('$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:MANYCLAUDE_ARG0); '
               '$s.TargetPath = $env:MANYCLAUDE_ARG1; $s.Arguments = $env:MANYCLAUDE_ARG2; '
               '$s.WorkingDirectory = $env:USERPROFILE; $s.IconLocation = $env:MANYCLAUDE_ARG3 + ",0"; '
               '$s.Description = $env:MANYCLAUDE_ARG4; $s.Save()',
               target, gui_python(), arguments, source, marker(manager, key))


def running(profile):
    if not profile.get('regular'):
        lock_path = Path(profile['desktop']).parent / 'desktop.lock'
        if lock_path.exists():
            with lock_path.open('a+') as lock:
                try:
                    lock_file(lock, blocking=False)
                except BlockingIOError:
                    return True
        return False
    folder = str(Path(profile['source']).parent).lower()
    for _, command in processes():
        lowered = command.lower()
        if folder in lowered and '--type=' not in lowered and '--user-data-dir' not in lowered:
            return True
    return False


def launch(manager, key, profile, background=False):
    source = source_app(profile['source'])
    env = tool_environment()
    if background:
        log = manager.BASE / 'desktop-launch.log'
        with log.open('ab') as out:
            process = subprocess.Popen([str(gui_python()), str(manager.ROOT / 'manyclaude'), '_desktop-run', key],
                                       env=dict(env, MANYCLAUDE_HOME=str(manager.BASE)), stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=out,
                                       creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            status = process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            return
        if status:
            raise ValueError(f'Claude Desktop failed to launch. See {log}.')
        return
    try:
        _, env = claude_command()
    except ValueError:
        pass
    args = [str(executable(source))]
    for variable in ('CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR', 'CLAUDE_USER_DATA_DIR'):
        env.pop(variable, None)
    manager.prepare_profile(profile)
    if profile.get('regular'):
        return subprocess.run(args, env=env, check=True)
    for variable in manager.AUTH_ENV:
        env.pop(variable, None)
    env.update(CLAUDE_CONFIG_DIR=profile['code'], CLAUDE_SECURESTORAGE_CONFIG_DIR=profile.get('code_session', profile['code']),
               CLAUDE_USER_DATA_DIR=profile['desktop'])
    args.append('--user-data-dir=' + profile['desktop'])
    lock_path = Path(profile['desktop']).parent / 'desktop.lock'
    with lock_path.open('a+') as lock:
        try:
            lock_file(lock, blocking=False)
        except BlockingIOError:
            # Electron routes this invocation to the existing profile window.
            pass
        return subprocess.run(args, env=env, check=True)
