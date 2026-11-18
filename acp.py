"""Run the upstream Claude ACP adapter with ManyClaude's model router."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

ADAPTER_PACKAGE = '@agentclientprotocol/claude-agent-acp@0.107.0'


def adapter_command(env, executable=None):
    path = executable or env.get('MANYCLAUDE_ACP_ADAPTER')
    if path:
        if not Path(path).is_absolute() or not os.access(path, os.X_OK):
            raise ValueError('Set MANYCLAUDE_ACP_ADAPTER to an absolute path to claude-agent-acp.')
        return [path]
    if sys.platform == 'darwin':
        root = Path.home() / 'Library/Application Support/Delta/acp/claude-acp'
        try:
            folder = (root / 'cached-installation').read_text().strip()
            candidate = root / folder / 'claude-agent-acp'
            if Path(folder).name == folder and candidate.is_file() and os.access(candidate, os.X_OK):
                return [str(candidate)]
        except OSError:
            pass
    direct = shutil.which('claude-agent-acp', path=env['PATH'])
    if direct:
        return [direct]
    npx = shutil.which('npx', path=env['PATH'])
    if npx:
        return [npx, '--yes', '--package=' + ADAPTER_PACKAGE, 'claude-agent-acp']
    raise ValueError('Install Delta’s Claude Agent, install claude-agent-acp, or add Node.js 22+ and npx to PATH.')


def start(manager, arguments):
    from global_code import launch_code

    parser = argparse.ArgumentParser(prog='glaude acp', description='Serve Claude Code over ACP on stdin/stdout.')
    parser.add_argument('--adapter', help='absolute path to claude-agent-acp')
    parser.add_argument('--profile', help='profile providing the initial Claude Code settings')
    args = parser.parse_args(arguments)

    def run(binary, env, store, profile, gateway, rows, efforts):
        command = adapter_command(env, args.adapter)
        with tempfile.TemporaryDirectory(prefix='manyclaude-acp-') as directory:
            directory = Path(directory)
            payload = directory / 'launch.json'
            payload.write_text(json.dumps({
                'binary': binary, 'store': str(store), 'rows': rows, 'efforts': efforts,
                'base_url': gateway.environment['ANTHROPIC_BASE_URL'],
            }))
            wrapper = directory / 'claude'
            wrapper.write_text('#!/bin/sh\nexec ' + shlex.join([
                sys.executable, str(manager.ROOT / 'manyclaude'), '_acp-code', str(payload),
            ]) + ' "$@"\n')
            wrapper.chmod(0o700)
            env.update(CLAUDE_CODE_EXECUTABLE=str(wrapper), CLAUDE_CONFIG_DIR=str(store),
                       CLAUDE_SECURESTORAGE_CONFIG_DIR='' if profile.get('regular') else
                       profile.get('code_session', profile['code']))
            with subprocess.Popen(command, env=gateway.session_environment(env), start_new_session=True) as process:
                def stop(signum, _):
                    try:
                        os.killpg(process.pid, signum)
                    except ProcessLookupError:
                        pass

                signals = (signal.SIGTERM, signal.SIGINT)
                previous = {sig: signal.signal(sig, stop) for sig in signals}
                try:
                    return process.wait()
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig, handler)

    code_args = ['--profile', args.profile] if args.profile else []
    result = launch_code(manager, code_args, adapter=run)
    raise SystemExit(result if result >= 0 else 128 - result)


def code(path, arguments):
    from global_code import picker_arguments

    payload = json.loads(Path(path).read_text())
    binary = payload['binary']
    if any(arg.partition('=')[0] in ('--print', '-p', '--input-format', '--output-format') for arg in arguments):
        arguments = picker_arguments({'code': payload['store']}, arguments, {}, routes=payload['rows'],
                                     efforts=payload.get('efforts'),
                                     gateway_env={'ANTHROPIC_BASE_URL': payload['base_url']}, sdk=True)
    os.execv(binary, [binary, *arguments])


def open_delta(manager, arguments):
    parser = argparse.ArgumentParser(prog='glaude delta', description='Launch Delta with ManyClaude as its Claude Agent.')
    parser.add_argument('--app', default='Delta', help='app name or path on macOS')
    args = parser.parse_args(arguments)
    if sys.platform != 'darwin':
        raise ValueError('On Linux, launch Delta with DELTA_CLAUDE_ACP_EXECUTABLE set to the ManyClaude ACP executable.')
    executable = manager.ROOT / 'manyclaude-acp'
    if not os.access(executable, os.X_OK):
        raise ValueError('Run ./install.sh to install the ManyClaude ACP executable.')
    subprocess.run(['/usr/bin/open', '-n', '-a', args.app, '--env',
                    'DELTA_CLAUDE_ACP_EXECUTABLE=' + str(executable)], check=True)
