#!/bin/sh
set -eu

MANYCLAUDE_REPO=${MANYCLAUDE_REPO:-NZ-Linix/manyclaude}
MANYCLAUDE_REF=${MANYCLAUDE_REF:-main}

# Piped installs (curl ... | sh) have no checkout next to them: download the
# repository and rerun its installer. Wrapped in a function so a truncated
# download never runs a partial script.
bootstrap() {
    WORK_DIR=$(mktemp -d)
    trap 'rm -rf "$WORK_DIR"' EXIT HUP INT TERM
    ARCHIVE="$WORK_DIR/manyclaude.tar.gz"
    echo "Downloading $MANYCLAUDE_REPO@$MANYCLAUDE_REF..."
    if [ -z "${GITHUB_TOKEN:-}" ] && command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
        GITHUB_TOKEN=$(gh auth token)
    fi
    URL="https://api.github.com/repos/$MANYCLAUDE_REPO/tarball/$MANYCLAUDE_REF"
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        curl -fsSL --proto '=https' -H "Authorization: Bearer $GITHUB_TOKEN" -o "$ARCHIVE" "$URL"
    elif ! curl -fsSL --proto '=https' -o "$ARCHIVE" "$URL"; then
        echo "Download failed. For a private repository, run 'gh auth login' or set GITHUB_TOKEN." >&2
        exit 1
    fi
    mkdir "$WORK_DIR/src"
    tar -xzf "$ARCHIVE" -C "$WORK_DIR/src" --strip-components=1
    if [ -r /dev/tty ] && (: </dev/tty) 2>/dev/null; then
        sh "$WORK_DIR/src/install.sh" </dev/tty
    else
        sh "$WORK_DIR/src/install.sh" </dev/null
    fi
}

case "$0" in
    */install.sh|install.sh) ;;
    *) bootstrap; exit 0 ;;
esac
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ -f "$SCRIPT_DIR/manyclaude" ] || { bootstrap; exit 0; }

PLATFORM=$(uname -s)
case "$PLATFORM" in
    Darwin|Linux) ;;
    *) echo "manyclaude supports macOS and Linux." >&2; exit 1 ;;
esac
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MANYCLAUDE_INSTALL_DIR=${MANYCLAUDE_INSTALL_DIR:-"$HOME/.local/share/manyclaude"}
MANYCLAUDE_BIN_DIR=${MANYCLAUDE_BIN_DIR:-"$HOME/.local/bin"}
PYTHON=$(command -v python3 || true)
if [ -z "$PYTHON" ]; then
    echo 'Python 3 is required. On Arch/Omarchy: sudo pacman -S python; on macOS: brew install python.' >&2
    exit 1
fi
if [ "$PLATFORM" = Darwin ] && ! xcrun --find clang >/dev/null 2>&1; then
    echo 'Install Apple Command Line Tools: xcode-select --install' >&2
    exit 1
fi

# Build everything before touching an existing installation or the shell file.
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT HUP INT TERM
if [ "$PLATFORM" = Darwin ]; then
    clang -fobjc-arc -O2 -framework AppKit "$SOURCE_DIR/native/launcher.m" -o "$BUILD_DIR/launcher"
fi

# Reject busy accounts or conflicting destinations before replacing the tool.
"$PYTHON" "$SOURCE_DIR/manyclaude" migrate --check

"$PYTHON" - "$SOURCE_DIR" "$MANYCLAUDE_INSTALL_DIR" "$MANYCLAUDE_BIN_DIR" "$BUILD_DIR" "$PYTHON" <<'PY'
import os
import importlib.machinery
import importlib.util
from pathlib import Path
import shlex
import shutil
import sys

source, target, bin_dir, build, python = map(Path, sys.argv[1:])
sys.path.insert(0, str(source))
from runtime import shell_quote
# A mise Python shim is for interactive version selection, not a durable shebang.
python = Path(sys.executable)
target, bin_dir = target.expanduser().absolute(), bin_dir.expanduser().absolute()
if target == source:
    raise SystemExit('Choose an install directory different from the source folder.')
shell = Path(os.environ.get('SHELL', '/bin/zsh')).name
config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
if not config.is_absolute():
    config = Path.home() / '.config'
rc = (Path(os.environ.get('ZDOTDIR', str(Path.home()))) / '.zshrc' if shell == 'zsh'
      else Path.home() / '.bashrc' if shell == 'bash'
      else config / 'fish/config.fish' if shell == 'fish' else None)
start, end = '# >>> manyclaude PATH >>>', '# <<< manyclaude PATH <<<'
if rc:
    rc = rc.resolve()
    original = rc.read_text() if rc.exists() else ''
    if (start in original or end in original) and (original.count(start) != 1 or original.count(end) != 1 or original.index(start) > original.index(end)):
        raise SystemExit(f'Malformed manyclaude PATH block in {rc}; fix its markers first.')
entry = bin_dir / 'manyclaude'
if entry.exists() or entry.is_symlink():
    expected = '# manyclaude global launcher'
    if entry.is_symlink() or expected not in entry.read_text():
        raise SystemExit(f'Refusing to overwrite an unrelated command: {entry}')
loader = importlib.machinery.SourceFileLoader('manyclaude_upgrade', str(source / 'manyclaude'))
spec = importlib.util.spec_from_loader(loader.name, loader)
manager = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = manager
loader.exec_module(manager)
from upgrade import remove_chat_sync
remove_chat_sync(manager)
target.mkdir(parents=True, exist_ok=True)
(target / 'native').mkdir(exist_ok=True)
bin_dir.mkdir(parents=True, exist_ok=True)
if source.resolve() != target.resolve():
    shutil.copy2(source / 'manyclaude', target / 'manyclaude')
    shutil.copy2(source / 'tui.py', target / 'tui.py')
    shutil.copy2(source / 'openusage.py', target / 'openusage.py')
    shutil.copy2(source / 'updater.py', target / 'updater.py')
    shutil.copy2(source / 'runtime.py', target / 'runtime.py')
    shutil.copy2(source / 'global_code.py', target / 'global_code.py')
    shutil.copy2(source / 'global_gateway.py', target / 'global_gateway.py')
    shutil.copy2(source / 'acp.py', target / 'acp.py')
    shutil.copy2(source / 'custom_profiles.py', target / 'custom_profiles.py')
    shutil.copy2(source / 'convo_file.py', target / 'convo_file.py')
    shutil.copy2(source / 'convo_export.py', target / 'convo_export.py')
    shutil.copy2(source / 'linux_desktop.py', target / 'linux_desktop.py')
    shutil.copy2(source / 'storage.py', target / 'storage.py')
    shutil.copy2(source / 'profile_config.py', target / 'profile_config.py')
    shutil.copy2(source / 'upgrade.py', target / 'upgrade.py')
    shutil.copy2(source / 'compat.py', target / 'compat.py')
    shutil.copy2(source / 'windows_desktop.py', target / 'windows_desktop.py')
    shutil.copy2(source / 'native/launcher.m', target / 'native/launcher.m')
    shutil.copy2(source / 'README.md', target / 'README.md')
for obsolete in ('chatsync', 'sync_links', 'sync_service'):
    (target / (obsolete + '.py')).unlink(missing_ok=True)
    for cache in (target / '__pycache__').glob(obsolete + '.*.pyc'):
        cache.unlink()
if sys.platform == 'darwin':
    shutil.copy2(build / 'launcher', target / 'native/launcher')
# Both the global entry point and aliases use the detected interpreter.
text = (target / 'manyclaude').read_text().split('\n', 1)[1]
(target / 'manyclaude').write_text('#!' + str(python) + '\n' + text)
(target / 'manyclaude').chmod(0o755)
entry.write_text('#!/bin/sh\n# manyclaude global launcher\nexec ' + shlex.quote(str(python)) + ' ' + shlex.quote(str(target / 'manyclaude')) + ' "$@"\n')
entry.chmod(0o755)
acp = target / 'manyclaude-acp'
acp.write_text('#!/bin/sh\nexec ' + shlex.join(['env', 'MANYCLAUDE_HOME=' + str(manager.BASE),
                                               str(python), str(target / 'manyclaude'), 'global', 'acp']) +
               ' "$@"\n')
acp.chmod(0o755)
(target / 'command-bin-dir').write_text(str(bin_dir) + '\n')
if rc:
    import re
    path_setup = ('fish_add_path --path ' + shell_quote(bin_dir, True) if shell == 'fish'
                  else 'export PATH=' + shlex.quote(str(bin_dir)) + ':"$PATH"')
    block = start + '\n' + path_setup + '\n' + end + '\n'
    if start in original:
        updated = re.sub(re.escape(start) + r'.*?' + re.escape(end) + r'\n?', lambda _: block, original, flags=re.S)
    else:
        updated = original.rstrip('\n') + ('\n\n' if original else '') + block
    if updated != original:
        rc.parent.mkdir(parents=True, exist_ok=True)
        if rc.exists():
            shutil.copy2(rc, rc.with_name(rc.name + '.manyclaude-install.bak'))
        rc.write_text(updated)
print(f'Installed: {entry}')
if rc:
    print(f'Run: source {shell_quote(rc, shell == "fish")}')
else:
    print(f'Add {bin_dir} to your shell PATH.')
print('Then run: manyclaude')
PY

# Startup migrates legacy account data; refresh also upgrades pinned launchers
# and rewires shell aliases to this installed copy on both supported platforms.
"$PYTHON" "$MANYCLAUDE_INSTALL_DIR/manyclaude" refresh
