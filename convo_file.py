"""Run a Claude Code conversation backed by an arbitrary .jsonl file.

Each file gets a private config store under <ManyClaude home>/file-sessions. Claude reads
the conversation from a copy in that store, and every change it writes is mirrored back
into the chosen file, so the profile's normal store and its other conversations are untouched.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from threading import Event, Thread
import uuid

OPTION = '--convo'
CONFLICTING = {'--resume', '-r', '--continue', '-c', '--session-id', '--fork-session', '--no-session-persistence'}
INTERVAL = 1.0


def take(arguments):
    """Remove --convo PATH from the options before any `--`; return (path or None, arguments)."""
    path, remaining = None, []
    values = iter(arguments)
    for value in values:
        if value == '--':
            remaining.extend([value, *values])
            break
        if value == OPTION:
            path = next(values, None)
            if not path:
                raise ValueError('Expected --convo FILE.jsonl.')
        elif value.startswith(OPTION + '='):
            path = value.split('=', 1)[1]
        else:
            remaining.append(value)
    return path, remaining


def validate_target(path):
    target = Path(path).expanduser().absolute()
    if target.suffix != '.jsonl':
        raise ValueError('Conversation files must end in .jsonl.')
    if target.exists() and not target.is_file():
        raise ValueError(f'Not a file: {target}')
    if not target.parent.is_dir():
        raise ValueError(f'Folder does not exist: {target.parent}')
    return target


class ConvoFile:
    def __init__(self, path, scope):
        self.target = validate_target(path)
        self.store_name = 'file-sessions/' + scope + '-' + hashlib.sha256(str(self.target).encode()).hexdigest()[:16]
        self.conflict = None

    def prepare(self, store, arguments):
        """Seed the private store and return the Claude arguments that open the conversation."""
        options = arguments[:arguments.index('--')] if '--' in arguments else arguments
        clash = sorted({value.partition('=')[0] for value in options} & CONFLICTING)
        if clash:
            raise ValueError(f'{OPTION} chooses the conversation; remove {", ".join(clash)}.')
        name = re.sub(r'[^a-zA-Z0-9]', '-', os.getcwd())
        if len(name) > 200:
            raise ValueError(f'{OPTION} needs a shorter working directory path.')
        self.project = Path(store) / 'projects' / name
        self.project.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = self.target.read_bytes() if self.target.exists() else b''
        session, self.uuids = parse(data)
        if session:
            self.live = self.project / (session + '.jsonl')
            atomic_write(self.live, data)
            opening = ['--resume', session]
        else:
            session = str(uuid.uuid4())
            self.live = self.project / (session + '.jsonl')
            opening = ['--session-id', session]
        self.before = {path for path in self.project.glob('*.jsonl') if path != self.live}
        self.written = data
        self.mark = stamp(self.target)
        self.seen = None
        return [*opening, *arguments]

    def source(self):
        """The file holding this conversation, following a resume that forked into a new file."""
        best = self.live if self.live.exists() else None
        if self.uuids:
            for path in self.project.glob('*.jsonl'):
                if path in self.before or path == self.live:
                    continue
                if (best is None or path.stat().st_mtime_ns > best.stat().st_mtime_ns) and shares(path, self.uuids):
                    best = path
        return best

    def sync(self):
        source = self.source()
        if source is None:
            return
        current = stamp(source)
        state = (source, current)
        if current is None or state == self.seen:
            return
        data = source.read_bytes()
        self.seen = state
        if data == self.written:
            return
        if self.conflict or (self.target.exists() and stamp(self.target) != self.mark):
            # Someone else changed the file; never overwrite their edit.
            self.conflict = self.target.with_name(self.target.stem + '.manyclaude-conflict.jsonl')
            atomic_write(self.conflict, data)
            self.written = data
            return
        if self.target.exists() and data.startswith(self.written):
            with self.target.open('ab') as stream:
                stream.write(data[len(self.written):])
        else:
            atomic_write(self.target, data)
        self.written = data
        self.mark = stamp(self.target)

    def runner(self, inner=None):
        def run(command, env):
            stop = Event()

            def mirror():
                while not stop.wait(INTERVAL):
                    try:
                        self.sync()
                    except OSError:
                        pass

            thread = Thread(target=mirror, daemon=True)
            thread.start()
            try:
                return inner(command, env) if inner else subprocess.run(command, env=env).returncode
            finally:
                stop.set()
                thread.join()
                self.finish()
        return run

    def finish(self):
        try:
            self.sync()
        except OSError as error:
            print(f'manyclaude: could not write {self.target}: {error}', file=sys.stderr)
        if self.conflict:
            print(f'manyclaude: {self.target} changed outside this session; '
                  f'this conversation was saved to {self.conflict}.', file=sys.stderr)
        source = self.source()
        others = [path for path in self.project.glob('*.jsonl')
                  if path not in self.before and path not in (self.live, source)]
        if others:
            print(f'manyclaude: {len(others)} other conversation(s) started in this session (for example /clear) '
                  f'were kept in {self.project}, not in {self.target.name}.', file=sys.stderr)


def parse(data):
    session, uuids = None, set()
    for line in data.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            if isinstance(entry.get('sessionId'), str):
                session = entry['sessionId']
            if isinstance(entry.get('uuid'), str):
                uuids.add(entry['uuid'])
    if session and not re.fullmatch(r'[0-9A-Za-z-]{1,64}', session):
        raise ValueError('The conversation file has an invalid sessionId.')
    return session, uuids


def shares(path, uuids, limit=400):
    try:
        with path.open('rb') as stream:
            for _, line in zip(range(limit), stream):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get('uuid') in uuids:
                    return True
    except OSError:
        pass
    return False


def stamp(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_size, info.st_mtime_ns


def atomic_write(path, data):
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.manyclaude-')
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
