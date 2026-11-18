"""Remove obsolete ManyClaude services and their metadata during installation."""
import hashlib
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import time

from compat import lock_file


def remove_chat_sync(manager):
    if os.name == 'nt':
        return  # The chat sync service never existed on Windows.
    label = 'local.manyclaude.sync.' + hashlib.sha256(str(manager.BASE).encode()).hexdigest()[:12]
    if manager.LINUX:
        config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
        if not config.is_absolute():
            config = Path.home() / '.config'
        agent = config / 'systemd/user' / (label + '.service')
        if agent.is_symlink():
            raise ValueError('Refusing a redirected legacy chat service.')
        if agent.exists():
            if agent.read_text().partition('\n')[0] != '# ManyClaude Sync: ' + str(manager.BASE):
                raise ValueError('The legacy service file is not owned by this installation.')
            subprocess.run(['systemctl', '--user', 'disable', '--now', agent.name], check=True, capture_output=True)
            agent.unlink()
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, capture_output=True)
    else:
        agent = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
        if agent.is_symlink():
            raise ValueError('Refusing a redirected legacy chat service.')
        if agent.exists():
            data = plistlib.loads(agent.read_bytes())
            if (data.get('Label') != label or data.get('EnvironmentVariables', {}).get('MANYCLAUDE_HOME') != str(manager.BASE)
                    or '_sync-daemon' not in data.get('ProgramArguments', [])):
                raise ValueError('The legacy service file is not owned by this installation.')
        service = 'gui/' + str(os.getuid()) + '/' + label
        loaded = subprocess.run(['/bin/launchctl', 'print', service], capture_output=True, text=True)
        if loaded.returncode == 0:
            if not agent.exists() and ('_sync-daemon' not in loaded.stdout or str(manager.BASE) not in loaded.stdout):
                raise ValueError('Cannot verify ownership of the loaded legacy chat service.')
            subprocess.run(['/bin/launchctl', 'bootout', service], check=True, capture_output=True)
        agent.unlink(missing_ok=True)
    lock_path = manager.BASE / '.sync-service.lock'
    if lock_path.is_symlink():
        raise ValueError('Refusing a redirected legacy service lock.')
    if lock_path.exists():
        with lock_path.open('r+') as lock:
            try:
                lock_file(lock, blocking=False)
            except BlockingIOError:
                pid = int(lock.read().strip())
                if pid <= 1:
                    raise ValueError('Invalid legacy chat watcher process.')
                process = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'uid=,command='], capture_output=True, text=True)
                fields = process.stdout.strip().split(None, 1)
                if (len(fields) != 2 or fields[0] != str(os.getuid()) or 'manyclaude' not in fields[1]
                        or '_sync-daemon' not in fields[1]):
                    raise ValueError('Cannot verify the legacy chat watcher process; no process was stopped.')
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                for _ in range(250):
                    try:
                        lock_file(lock, blocking=False)
                        break
                    except BlockingIOError:
                        time.sleep(.1)
                else:
                    raise ValueError('The old chat watcher has not stopped yet. Retry installation shortly.')
    for name in ('sync-links.json', 'sync-status.json', 'chat-sync.json', 'sync-service.log', '.sync-service.lock'):
        (manager.BASE / name).unlink(missing_ok=True)
