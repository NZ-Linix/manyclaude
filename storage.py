"""Resumable migration of isolated account stores out of the manager's data root."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def profile_dir(key):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', key) or len(key) > 64:
        raise ValueError('Invalid profile slug in storage registry.')
    return Path.home() / ('.claude-' + key)


def safe_path(path):
    # Reject redirected ancestors as well as redirected account roots.
    if path.is_symlink() or path.resolve() != Path.home().resolve() / path.relative_to(Path.home()):
        raise ValueError(f'Refusing to migrate a redirected profile folder: {path}')


def check_closed(manager, profile, action='migrating its data'):
    if manager.running(profile):
        raise ValueError(f"Quit {profile['name']}'s Claude Desktop before {action}.")
    for file in (Path(profile['code']) / 'sessions').glob('*.json'):
        if file.stem.isdigit():
            result = subprocess.run(['/bin/ps', '-p', file.stem, '-o', 'command='], capture_output=True, text=True)
            if result.returncode == 0 and 'claude' in result.stdout.lower():
                raise ValueError(f"Quit {profile['name']}'s Claude Code sessions before {action}.")
    # Cover clients that have not written a sessions/PID file yet. Keep process
    # environments in memory: they may contain tokens and must never be logged.
    directories = {profile['code'], profile.get('code_session', profile['code'])}
    processes = subprocess.run(['/bin/ps', '-ax', '-o', 'pid=,uid=,command='],
                               capture_output=True, text=True, check=True)
    for line in processes.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or fields[1] != str(os.getuid()):
            continue
        pid, _, command = fields
        if not re.search(r'(?:^|/)claude(?:\s|$)|/claude/versions/|@anthropic-ai/claude-code/', command):
            continue
        if manager.LINUX:
            try:
                environment = Path('/proc', pid, 'environ').read_bytes().decode(errors='replace').replace('\0', '\n')
            except OSError:
                continue
        else:
            result = subprocess.run(['/bin/ps', 'eww', '-p', pid, '-o', 'command='], capture_output=True, text=True)
            environment = result.stdout
        regular = profile.get('regular') and not re.search(r'CLAUDE_CONFIG_DIR=\S+', environment)
        if regular or any(re.search(r'CLAUDE_(?:CONFIG_DIR|SECURESTORAGE_CONFIG_DIR)=' + re.escape(directory) + r'(?=\s|$)', environment)
                          for directory in directories):
            raise ValueError(f"Quit {profile['name']}'s Claude Code sessions before {action}.")


def plan(manager, profiles, journal):
    legacy_root = manager.BASE / 'profiles'
    keys = set(profiles)
    if legacy_root.exists():
        if legacy_root.is_symlink():
            raise ValueError('Refusing to migrate a redirected legacy profiles folder.')
        keys.update(p.name for p in legacy_root.iterdir() if p.is_dir() or p.is_symlink())
    keys.update(journal)
    result = {}
    for key in sorted(keys):
        target = profile_dir(key)
        old = legacy_root / key
        profile = profiles.get(key)
        if profile and profile.get('regular'):
            if profile['code'] != str(Path.home() / '.claude'):
                raise ValueError('Profile registry belongs to a different home directory; refusing to migrate it.')
            continue
        if key not in journal and profile and profile['code'] == str(target) and profile['desktop'] == str(target / 'desktop'):
            continue
        if key not in journal and not old.exists() and not profile:
            continue
        if key not in journal and profile and (profile['code'] != str(old / 'code') or profile['desktop'] != str(old / 'desktop')):
            raise ValueError(f"Unexpected data paths for {profile['name']}; migration left them unchanged.")
        safe_path(target)
        if old.is_symlink() or old.resolve() != manager.BASE.resolve() / 'profiles' / key:
            raise ValueError(f'Refusing to migrate redirected legacy data: {old}')
        for child in ('code', 'desktop'):
            if (old / child).is_symlink():
                raise ValueError(f'Refusing to migrate redirected legacy data: {old / child}')
        if key not in journal and (target.exists() or target.is_symlink()):
            raise ValueError(f'Migration destination already exists: {target}. Move it aside before retrying; no data was overwritten.')
        # During resume, clients may use either path. Check both locks/session stores.
        original = dict(profile or {}, name=(profile or {}).get('name', key), code=str(old / 'code'), desktop=str(old / 'desktop'))
        check_closed(manager, original)
        if target.exists():
            check_closed(manager, dict(original, code=str(target), desktop=str(target / 'desktop')))
        session = profile.get('code_session') if profile else None
        if session:
            link = Path(session)
            expected_parent = Path.home() / '.claude-swap-backup/sessions'
            if (not link.is_symlink() or link.readlink() not in (old / 'code', target)
                    or link.parent != expected_parent or link.parent.is_symlink()
                    or link.parent.resolve() != Path.home().resolve() / '.claude-swap-backup/sessions'):
                raise ValueError('Unexpected OpenUsage session link; migration left it unchanged.')
        result[key] = (old, target, profile)
    return result


def migrate(manager, check=False, rebuild=True):
    """Caller holds .manager.lock. Originals remain until the registry is committed."""
    state_file = manager.BASE / 'storage-migrations.json'
    journal = json.loads(state_file.read_text()) if state_file.exists() else {}
    profiles = manager.read_profiles()
    work = plan(manager, profiles, journal)
    if check:
        return
    def save():
        manager.atomic_write(state_file, json.dumps(journal, indent=2) + '\n')
    for key, (old, target, profile) in work.items():
        owned_stage = key in journal
        state = journal.setdefault(key, {'phase': 'copy', 'stage': str(target) + '.manyclaude-migration'})
        stage = Path(state['stage'])
        if stage != Path(str(target) + '.manyclaude-migration') or stage.is_symlink():
            raise ValueError('Unexpected migration staging directory; refusing to alter it.')
        if state['phase'] == 'copy':
            # Only a journal we created grants ownership of the staging directory.
            if not owned_stage and stage.exists():
                raise ValueError(f'Migration staging directory already exists: {stage}')
            save()
            if stage.exists():
                shutil.rmtree(stage)
            if (old / 'code').exists():
                shutil.copytree(old / 'code', stage, symlinks=True)
            else:
                stage.mkdir(mode=0o700)
            if (stage / 'desktop').exists() or (stage / 'desktop').is_symlink():
                raise ValueError('Legacy Code data contains a desktop folder; migration cannot overwrite it.')
            if (old / 'desktop').exists():
                shutil.copytree(old / 'desktop', stage / 'desktop', symlinks=True)
            else:
                (stage / 'desktop').mkdir(mode=0o700)
            if old.exists():
                for extra in old.iterdir():
                    if extra.name in ('code', 'desktop'):
                        continue
                    destination = stage / extra.name
                    if destination.exists() or destination.is_symlink():
                        raise ValueError(f'Legacy profile files collide: {extra.name}')
                    if extra.is_dir() and not extra.is_symlink():
                        shutil.copytree(extra, destination, symlinks=True)
                    else:
                        shutil.copy2(extra, destination, follow_symlinks=False)
            stage.chmod(0o700)
            (stage / 'desktop').chmod(0o700)
            state['phase'] = 'ready'
            save()
        if state['phase'] == 'ready':
            if stage.exists():
                if target.exists() or target.is_symlink():
                    raise ValueError(f'Migration destination already exists: {target}')
                stage.rename(target)
            elif not target.is_dir():
                raise ValueError('Migration destination is missing; original data retained.')
            # Keychain service names depend on the literal config path. Linked
            # OpenUsage profiles retain their service; only their symlink moves.
            if not manager.LINUX and not (profile or {}).get('code_session'):
                import openusage
                openusage.copy_keychain_store(str(old / 'code'), str(target))
            if profile and profile.get('code_session'):
                link = Path(profile['code_session'])
                if link.readlink() != target:
                    fd, temporary = tempfile.mkstemp(prefix='.manyclaude-link-', dir=link.parent)
                    os.close(fd)
                    os.unlink(temporary)
                    try:
                        Path(temporary).symlink_to(target, target_is_directory=True)
                        os.replace(temporary, link)
                    finally:
                        Path(temporary).unlink(missing_ok=True)
            if profile:
                profile.update(code=str(target), desktop=str(target / 'desktop'))
                manager.save_profiles(profiles)
            state['phase'] = 'committed'
            save()
        if state['phase'] == 'committed':
            if not manager.LINUX and not (profile or {}).get('code_session'):
                import openusage
                openusage.delete_keychain_store(str(old / 'code'))
            state['phase'] = 'launcher'
            save()
        if profile and rebuild:
            manager.build_app(key, profile)
        if profile and not rebuild:
            continue
        # Keep originals until the launcher is successfully rebuilt. Archive
        # them outside Application Support as a private recovery copy.
        if old.exists():
            backup_root = Path.home() / '.manyclaude-storage-backup'
            safe_path(backup_root)
            backup_root.mkdir(mode=0o700, exist_ok=True)
            backup = backup_root / key
            if backup.exists() or backup.is_symlink():
                raise ValueError(f'Recovery backup already exists: {backup}; original data retained.')
            shutil.move(str(old), str(backup))
        del journal[key]
        save()
        print(f'Migrated {key}: {target}')
    if not journal:
        state_file.unlink(missing_ok=True)
        try:
            (manager.BASE / 'profiles').rmdir()
        except OSError:
            pass
