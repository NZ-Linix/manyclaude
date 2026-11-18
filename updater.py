"""Update shared Claude installations, then rebuild profile applications."""
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse

FEED = 'https://downloads.claude.ai/releases/darwin/universal/RELEASES.json'
TEAM = 'Q6L2SF6YDW'


def download(url, destination):
    if urlparse(url).scheme != 'https' or urlparse(url).hostname != 'downloads.claude.ai':
        raise ValueError('Desktop release URL is not on Anthropic’s official download server.')
    subprocess.run(['/usr/bin/curl', '--fail', '--location', '--show-error', '--silent',
                    '--proto', '=https', '--proto-redir', '=https', '--connect-timeout', '30',
                    '--max-time', '600', '--output', str(destination), url], check=True)


def latest_release(data):
    version = data.get('currentRelease')
    for release in data.get('releases', []):
        update = release.get('updateTo', {})
        if update.get('version') == version and update.get('url'):
            # Validate before downloading or changing installations.
            url = update['url']
            if urlparse(url).scheme != 'https' or urlparse(url).hostname != 'downloads.claude.ai':
                break
            return version, url
    raise ValueError('Claude Desktop release feed has no usable current release.')


def app_version(app):
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != 'com.anthropic.claudefordesktop' or info.get('ManyClaudeProfile'):
        raise ValueError(f'Refusing to update an app that is not the original Claude Desktop: {app}')
    return info.get('CFBundleShortVersionString', info.get('CFBundleVersion'))


def check_closed(manager, profiles, sources):
    for profile in profiles.values():
        if manager.running(profile):
            raise ValueError(f"Quit {profile['name']}'s Claude Desktop, then run manyclaude update-claude again.")
    for source in sources:
        if manager.running({'regular': True, 'app': str(source)}):
            raise ValueError('Quit the original Claude Desktop, then run manyclaude update-claude again.')


def replace_app(source, downloaded):
    # Stage on the same filesystem; restore the old installation if replacement fails.
    with tempfile.TemporaryDirectory(prefix='.manyclaude-update-', dir=source.parent) as folder:
        staged, backup = Path(folder) / 'Claude.app', Path(folder) / 'previous.app'
        subprocess.run(['/usr/bin/ditto', str(downloaded), str(staged)], check=True)
        subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(staged)], check=True)
        source.rename(backup)
        try:
            staged.rename(source)
        except BaseException:
            backup.rename(source)
            raise


def package_owner(pacman, path, env):
    for candidate in dict.fromkeys((str(path), str(Path(path).resolve()))):
        result = subprocess.run([pacman, '-Qoq', candidate], env=env, capture_output=True, text=True)
        if result.returncode == 0:
            package = result.stdout.strip()
            if re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9@._+-]*', package):
                return package
    return None


def update_linux(manager, yes=False):
    from runtime import claude_command, mise_data
    binary, env = claude_command()
    for field in (*manager.AUTH_ENV, 'CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR', 'CLAUDE_USER_DATA_DIR'):
        env.pop(field, None)
    profiles = manager.read_profiles()
    sources = sorted({manager.source_app(p['source']) for p in profiles.values()}) or [manager.source_app()]
    check_closed(manager, profiles, sources)
    pacman = shutil.which('pacman', path=env.get('PATH'))
    if not pacman:
        raise ValueError('Automatic Linux Desktop updates currently require Arch/Omarchy with pacman.')
    packages = set()
    for source in sources:
        package = package_owner(pacman, source, env)
        if not package:
            raise ValueError(f'{source} is not owned by an installed Arch package. Update this executable/AppImage with its distributor.')
        packages.add(package)
    cli_package = package_owner(pacman, binary, env)
    mise_owned = Path(binary).is_relative_to(mise_data(env) / 'installs') or '/mise/' in binary
    if cli_package:
        packages.add(cli_package)
        code_update = None
    elif mise_owned:
        mise = shutil.which('mise', path=env.get('PATH'))
        if not mise:
            raise ValueError('The mise-managed CLI requires mise on PATH to update.')
        code_update = [mise, 'upgrade', 'claude']
    elif '/node_modules/@anthropic-ai/claude-code/' in str(Path(binary).resolve()):
        npm = shutil.which('npm', path=env.get('PATH'))
        if not npm:
            raise ValueError('The npm-managed CLI requires npm on PATH to update.')
        code_update = [npm, 'install', '-g', '@anthropic-ai/claude-code@latest']
    else:
        code_update = [binary, 'install', 'latest']
    helper = shutil.which('yay', path=env.get('PATH')) or shutil.which('paru', path=env.get('PATH'))
    if helper:
        desktop_update = [helper, '-S', '--needed', *sorted(packages)]
    else:
        # Without an AUR helper, only packages in configured binary repos can update.
        for package in packages:
            result = subprocess.run([pacman, '-Si', package], env=env, capture_output=True, text=True)
            if result.returncode:
                raise ValueError(f'Install yay or paru to update the AUR package {package}.')
        sudo = shutil.which('sudo', path=env.get('PATH'))
        if not sudo:
            raise ValueError('Install sudo or an AUR helper to update system Claude packages.')
        desktop_update = [sudo, pacman, '-S', '--needed', *sorted(packages)]
    if yes:
        desktop_update.append('--noconfirm')
    print('Updating installed Claude packages: ' + ', '.join(sorted(packages)) + '…', flush=True)
    try:
        subprocess.run(desktop_update, env=env, check=True)
        if code_update:
            print('Updating the shared Claude Code installation…', flush=True)
            subprocess.run(code_update, env=env, check=True)
    finally:
        # Even a partially successful package transaction must refresh launchers.
        print('Refreshing all Desktop profiles…', flush=True)
        manager.refresh()
    current, current_env = claude_command()
    for field in (*manager.AUTH_ENV, 'CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR', 'CLAUDE_USER_DATA_DIR'):
        current_env.pop(field, None)
    subprocess.run([current, '--version'], env=current_env, check=True)
    print('Claude updated. Reopen Desktop apps and restart Code sessions to use the new versions.')


def update(manager, yes=False):
    if getattr(manager, 'LINUX', False):
        return update_linux(manager, yes)
    profiles = manager.read_profiles()
    sources = sorted({manager.source_app(p['source']) for p in profiles.values()})
    if not sources:
        sources = [manager.source_app()]
    check_closed(manager, profiles, sources)
    binary = shutil.which('claude')
    if not binary:
        raise ValueError('Claude Code is not installed/on PATH. Install it before updating.')
    # Package managers own their launchers; do not install a second competing CLI.
    if any(part in str(Path(binary).resolve()) for part in ('/Caskroom/', '/Cellar/')):
        raise ValueError('This command requires the native Claude Code installation. Update this CLI with Homebrew instead.')
    with tempfile.TemporaryDirectory(prefix='manyclaude-update-') as folder:
        folder = Path(folder)
        print('Checking Claude Desktop’s latest release…', flush=True)
        feed = folder / 'RELEASES.json'
        download(FEED, feed)
        try:
            version, url = latest_release(json.loads(feed.read_text()))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Could not read Claude Desktop’s official release feed.') from None
        pending = [source for source in sources if app_version(source) != version]
        for source in pending:
            if source.is_symlink() or not os.access(source.parent, os.W_OK):
                raise ValueError(f'Cannot safely replace {source}. Its parent folder must be writable and the app must not be a symlink.')
        if pending:
            print(f'Downloading Claude Desktop {version}…', flush=True)
            archive = folder / 'Claude.zip'
            download(url, archive)
            extracted = folder / 'download'
            subprocess.run(['/usr/bin/ditto', '-x', '-k', str(archive), str(extracted)], check=True)
            app = extracted / 'Claude.app'
            if app_version(app) != version:
                raise ValueError('Downloaded Desktop version does not match the release feed.')
            subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
            signature = subprocess.run(['/usr/bin/codesign', '-dv', str(app)], capture_output=True, text=True, check=True)
            if f'TeamIdentifier={TEAM}' not in signature.stderr.splitlines():
                raise ValueError('Downloaded Desktop is not signed by Anthropic; existing apps left unchanged.')
            check_closed(manager, profiles, sources)
            for source in pending:
                replace_app(source, app)
                print(f'Updated {source} to {version}.', flush=True)
        else:
            print(f'Claude Desktop is current ({version}).', flush=True)
    env = os.environ.copy()
    for field in ('CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR', 'CLAUDE_USER_DATA_DIR'):
        env.pop(field, None)
    print('Updating the shared Claude Code binary to latest…', flush=True)
    cli_error = None
    try:
        subprocess.run([binary, 'install', 'latest'], env=env, check=True)
    except subprocess.CalledProcessError as exc:
        cli_error = exc
    # Even a CLI failure must not leave profile apps on the previous Desktop version.
    print('Refreshing all Desktop profiles…', flush=True)
    manager.refresh()
    if cli_error:
        raise ValueError('Desktop profiles refreshed, but the Claude Code update failed. Retry manyclaude update-claude.') from cli_error
    subprocess.run([shutil.which('claude') or binary, '--version'], env=env, check=True)
    print('Claude updated. Reopen Desktop apps and restart Code sessions to use the new versions.')
