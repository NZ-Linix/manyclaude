"""Cross-platform primitives: file locks, links, process listings and exec."""
import os
from pathlib import Path
import re
import subprocess
import sys
import time

WINDOWS = os.name == 'nt'
MACOS = sys.platform == 'darwin'
LINUX = sys.platform.startswith('linux')

if WINDOWS:
    import msvcrt
else:
    import fcntl

# Windows byte-range locks are mandatory; lock far past any content so readers are unaffected.
_LOCK_OFFSET = 1 << 30
CODE_PROCESS = re.compile(r'(?:^|[/\\"])claude(?:\.exe)?(?:"|\s|$)|[/\\]claude[/\\]versions[/\\]|@anthropic-ai[/\\]claude-code[/\\]',
                          re.IGNORECASE)


def lock_file(stream, blocking=True):
    """Exclusive lock held until the file is closed; BlockingIOError when busy and not blocking."""
    if not WINDOWS:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    stream.flush()
    fd = stream.fileno()
    while True:
        position = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if not blocking:
                raise BlockingIOError('File is locked by another process.') from None
            time.sleep(.05)
        finally:
            os.lseek(fd, position, os.SEEK_SET)


def is_link(path):
    path = Path(path)
    return path.is_symlink() or (WINDOWS and hasattr(path, 'is_junction') and path.is_junction())


def read_link(path):
    target = os.readlink(path)
    return Path(target[4:] if target.startswith('\\\\?\\') else target)


def link(target, path, directory=None):
    """Symlink; on Windows without symlink rights fall back to a junction (folders) or a copy (files)."""
    target, path = Path(target), Path(path)
    directory = target.is_dir() if directory is None else directory
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if not WINDOWS or isinstance(error, FileExistsError):
            raise
        if os.path.lexists(path):
            raise FileExistsError(str(path)) from None
        if directory:
            import _winapi
            _winapi.CreateJunction(str(target.absolute()), str(path))
        else:
            import shutil
            shutil.copy2(target, path)


def processes():
    """(pid, command line) of this user's processes."""
    if WINDOWS:
        script = ("Get-CimInstance Win32_Process | ForEach-Object { "
                  "if ($_.CommandLine) { [string]$_.ProcessId + [char]9 + $_.CommandLine } }")
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, text=True, errors='replace',
                                creationflags=subprocess.CREATE_NO_WINDOW)
        rows = []
        for line in result.stdout.splitlines():
            pid, _, command = line.partition('\t')
            if pid.strip().isdigit():
                rows.append((int(pid), command.strip()))
        return rows
    if LINUX:
        rows = []
        for process in Path('/proc').glob('[0-9]*'):
            try:
                if process.stat().st_uid == os.getuid():
                    args = process.joinpath('cmdline').read_bytes().decode(errors='replace').rstrip('\0')
                    rows.append((int(process.name), args.replace('\0', ' ')))
            except OSError:
                continue
        return rows
    result = subprocess.run(['/bin/ps', '-ax', '-o', 'pid=,uid=,command='], capture_output=True, text=True, check=True)
    rows = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3 and fields[1] == str(os.getuid()):
            rows.append((int(fields[0]), fields[2]))
    return rows


def code_commands():
    """Command lines of running Claude Code processes (not Claude Desktop)."""
    rows = []
    for _, command in processes():
        lowered = command.lower()
        if WINDOWS and ('anthropicclaude' in lowered or 'windowsapps' in lowered or '--type=' in lowered
                        or '--user-data-dir' in lowered):
            continue
        if CODE_PROCESS.search(command):
            rows.append(command)
    return rows


def pid_command(pid):
    if WINDOWS:
        return next((command for process, command in processes() if process == int(pid)), None)
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def exec_replace(binary, arguments, env=None):
    """os.execvpe on POSIX. Windows has no exec: run the child in this console and exit with its status."""
    if not WINDOWS:
        if env is None:
            os.execv(binary, arguments)
        os.execvpe(binary, arguments, env)
    import signal
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        status = subprocess.run([binary, *arguments[1:]], env=env).returncode
    finally:
        signal.signal(signal.SIGINT, previous)
    raise SystemExit(status)


def command_path(directory, name):
    return Path(directory) / (name + '.cmd' if WINDOWS else name)


def cmd_quote(value):
    return '"' + str(value).replace('%', '%%').replace('"', '""') + '"'


def command_script(marker, argv, env=None):
    """A launcher forwarding its arguments to argv: POSIX sh or Windows batch."""
    env = env or {}
    if WINDOWS:
        lines = [marker.rstrip('\r\n'), 'setlocal']
        lines += ['set "' + key + '=' + str(value).replace('%', '%%') + '"' for key, value in env.items()]
        lines += [' '.join(cmd_quote(arg) for arg in argv) + ' %*', 'exit /b %ERRORLEVEL%']
        return '\r\n'.join(lines) + '\r\n'
    import shlex
    prefix = ['env', *(key + '=' + str(value) for key, value in env.items())] if env else []
    return marker + 'exec ' + shlex.join([*prefix, *map(str, argv)]) + ' "$@"\n'
