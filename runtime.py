"""Resolve user-installed tools, including Omarchy's mise-managed Claude Code."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess


def shell_quote(value, fish=False):
    value = str(value)
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'" if fish else shlex.quote(value)


def mise_data(env):
    data = Path(env.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    if not data.is_absolute():
        data = Path.home() / '.local/share'
    return Path(env.get('MISE_DATA_DIR', data / 'mise'))


def mise_shims(env):
    return Path(env.get('MISE_SHIMS_DIR', mise_data(env) / 'shims'))


def tool_environment():
    env = os.environ.copy()
    # Preserve the caller's activated tool versions; fill gaps for GUI launches.
    extra = [str(Path.home() / '.local/bin'), str(mise_shims(env))]
    paths = env.get('PATH', os.defpath).split(os.pathsep)
    env['PATH'] = os.pathsep.join(paths + [p for p in extra if p not in paths])
    return env


def claude_command():
    env = tool_environment()
    binary = shutil.which('claude', path=env['PATH'])
    mise = shutil.which('mise', path=env['PATH'])
    shim_dir = mise_shims(env)
    shim = binary and (Path(binary).parent == shim_dir or '/shims/' in binary or
                       Path(binary).resolve().name in ('mise', 'mise-shim'))
    if (not binary or shim) and mise:
        result = subprocess.run([mise, 'which', 'claude'], env=env, capture_output=True, text=True)
        if result.returncode == 0:
            binary = result.stdout.strip()
            # npm-backed mise installs need their selected Node runtime on PATH too.
            result = subprocess.run([mise, 'env', '--json'], env=env, capture_output=True, text=True, check=True)
            values = json.loads(result.stdout)
            if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
                raise ValueError('mise returned an invalid environment.')
            env.update(values)
        else:
            binary = None
    elif shim:
        binary = None
    if not binary:
        # A stale shim must not hide an independently installed native CLI.
        paths = [p for p in env['PATH'].split(os.pathsep) if Path(p) != shim_dir and '/shims' not in p]
        binary = shutil.which('claude', path=os.pathsep.join(paths))
        if binary:
            # Desktop's child Code processes must also bypass the unusable shim.
            folder = str(Path(binary).parent)
            env['PATH'] = os.pathsep.join([folder, *[p for p in env['PATH'].split(os.pathsep) if p != folder]])
    if not binary or not Path(binary).is_file() or not os.access(binary, os.X_OK):
        raise ValueError('Claude Code is not installed. Native install (including Omarchy): '
                         'curl -fsSL https://claude.ai/install.sh | bash; '
                         'or, if you prefer mise: mise use -g claude@latest.')
    return binary, env


def is_claude_subcommand(binary, env, name):
    # These public commands are dispatched before the main CLI's help is built.
    if name in {'daemon', 'remote-control', 'rc', 'self-hosted-runner'}:
        return True
    if not re.fullmatch(r'[a-z][a-z0-9-]*', name):
        return False
    try:
        help_text = subprocess.check_output([binary, '--help'], env={**env, 'FORCE_COLOR': '0'},
                                            stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            text=True, errors='replace', timeout=2)
    except (OSError, subprocess.SubprocessError):
        return False
    _, heading, commands = help_text.partition('\nCommands:\n')
    if not heading:
        return False
    for line in commands.splitlines():
        if line and not line[0].isspace():
            break
        match = re.match(r'^ {2}(\S+)', line)
        if match and name in match[1].split('|'):
            return True
    return False
