"""List, search, and export Claude Code conversations from profile, Global, and custom-profile stores."""
from datetime import datetime
import json
import os
from pathlib import Path
import re

from convo_file import atomic_write

MAX_LISTED = 500
PROMPT_SCAN = 300
# Claude Code appends naming entries during a session; the last one of each kind wins.
NAME_FIELDS = {b'"custom-title"': 'customTitle', b'"agent-name"': 'agentName', b'"ai-title"': 'aiTitle',
               b'"summary"': 'summary'}
NAME_ORDER = ('customTitle', 'agentName', 'aiTitle', 'summary')
MESSAGE = re.compile(rb'"type":\s*"(?:user|assistant)"')


def sources(manager):
    """[(source, display name, store)] for every profile, Global, and custom profile."""
    import custom_profiles

    result = [(key, profile['name'], Path(profile['code'])) for key, profile in manager.read_profiles().items()]
    result.append(('global', 'Global', manager.BASE / 'global-code'))
    for key, item in custom_profiles.read(manager.BASE).items():
        result.append(('custom:' + key, item['name'], custom_profiles.store_directory(manager.BASE, key, item)))
    return result


def store_for(manager, source):
    """Resolve 'global', 'custom:<name>', or a profile name to (display name, store folder)."""
    import custom_profiles

    if source.lower() == 'global':
        return 'Global', manager.BASE / 'global-code'
    if source.lower().startswith('custom:'):
        key = manager.slugify(source.split(':', 1)[1])
        profiles = custom_profiles.read(manager.BASE)
        if key not in profiles:
            raise ValueError(f"Unknown custom profile '{source}'. Run manyclaude custom list.")
        return profiles[key]['name'], custom_profiles.store_directory(manager.BASE, key, profiles[key])
    _, profile = manager.get_profile(source)
    return profile['name'], Path(profile['code'])


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(part.get('text', '') for part in content if isinstance(part, dict) and part.get('type') == 'text')
    return ''


def describe(path):
    """Summarize one transcript: its name (as /resume shows it), first prompt, project, and size."""
    names, prompt, project, messages, scanned = {}, None, None, 0, 0
    try:
        with path.open('rb') as stream:
            for raw in stream:
                message = MESSAGE.search(raw) is not None
                messages += message
                naming = next((field for marker, field in NAME_FIELDS.items() if marker in raw), None)
                wanted = naming or (message and prompt is None and scanned < PROMPT_SCAN) or (
                    project is None and b'"cwd"' in raw)
                if not wanted:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                project = project or entry.get('cwd')
                if naming and isinstance(entry.get(naming), str) and entry[naming].strip():
                    names[naming] = entry[naming].strip()
                if entry.get('type') == 'user' and prompt is None and not entry.get('isMeta'):
                    scanned += 1
                    text = text_of((entry.get('message') or {}).get('content')).strip()
                    if text and not text.startswith('<'):
                        prompt = ' '.join(text.split())[:120]
    except OSError:
        pass
    name = next((names[field] for field in NAME_ORDER if field in names), None)
    return {'id': path.stem, 'path': path, 'modified': path.stat().st_mtime,
            'project': project or path.parent.name, 'name': name or prompt or ('(untitled)' if messages else '(no messages)'),
            'prompt': prompt or '', 'messages': messages}


def conversations(store, project=None):
    """Newest-first top-level transcripts; optionally only those for one working directory."""
    root = Path(store) / 'projects'
    if not root.is_dir():
        return []
    folders = [root / re.sub(r'[^a-zA-Z0-9]', '-', str(project))] if project else root.iterdir()
    paths = [path for folder in folders if folder.is_dir() for path in folder.glob('*.jsonl') if path.is_file()]
    paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [describe(path) for path in paths[:MAX_LISTED]]


def everything(manager):
    """All conversations across every store, newest first, each tagged with its source."""
    items = []
    for source, name, store in sources(manager):
        for item in conversations(store):
            items.append({**item, 'source': source, 'source_name': name})
    items.sort(key=lambda item: item['modified'], reverse=True)
    return items


def label(item, with_source=False):
    when = datetime.fromtimestamp(item['modified']).strftime('%Y-%m-%d %H:%M')
    prefix = f"{item['source_name']} · " if with_source else ''
    return f"{when}  {prefix}{item['name']}"


def search_text(item):
    return ' '.join(str(value) for value in (item['name'], item['id'], item['project'], item['prompt'],
                                             item.get('source_name', ''))).lower()


def find(store, session):
    if session == 'latest':
        items = conversations(store, os.getcwd())
        if not items:
            raise ValueError('No conversations for this folder. Pass a session ID, or run --list.')
        return items[0]['path']
    if not re.fullmatch(r'[0-9A-Za-z-]{4,64}', session):
        raise ValueError('Pass a session ID (or a unique prefix of one), or latest.')
    matches = [path for path in (Path(store) / 'projects').glob(f'*/{session}*.jsonl') if path.is_file()]
    if not matches:
        raise ValueError(f"No conversation '{session}' in this store. Run with --list.")
    if len({path.stem for path in matches}) > 1:
        raise ValueError(f"'{session}' matches several conversations; use more of the ID.")
    return max(matches, key=lambda path: path.stat().st_mtime)


def find_anywhere(manager, session):
    found = []
    for source, _, store in sources(manager):
        try:
            found.append((source, find(store, session)))
        except ValueError as error:
            if 'several' in str(error):
                raise
    if not found:
        raise ValueError(f"No conversation '{session}' in any profile, Global, or custom profile.")
    if len({path.stem for _, path in found}) > 1:
        raise ValueError(f"'{session}' matches conversations in several stores; use more of the ID.")
    return found[0]


def default_name(item):
    slug = re.sub(r'[^a-z0-9]+', '-', item['name'].lower()).strip('-')[:48]
    return (slug or item['id'][:8]) + '.jsonl'


def export(path, output, force=False):
    target = Path(output).expanduser().absolute()
    if target.is_dir():
        target = target / (default_name(describe(Path(path))))
    if target.suffix != '.jsonl':
        raise ValueError('Export to a file ending in .jsonl.')
    if target.exists() and not force:
        raise ValueError(f'{target} already exists. Pass --force to replace it.')
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, Path(path).read_bytes())
    return target


def command(manager, source, session=None, output=None, list_all=False, force=False, session_id=None):
    if session_id:
        # `manyclaude export --id ID [OUTPUT]`: positional arguments shift left.
        output = output or session or source
        _, path = find_anywhere(manager, session_id)
        target = export(path, output or os.getcwd(), force)
        print(f'Exported to {target}. Open it with --convo {target}')
        return
    if source is None:
        import sys
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise ValueError("Choose a source: manyclaude export <profile|global|custom:NAME> [--all], or --id ID.")
        from tui import export_ui
        return export_ui(manager)
    name, store = store_for(manager, source)
    if session is None:
        items = conversations(store, None if list_all else os.getcwd())
        if not items:
            print(f'No conversations in {name}' + ('' if list_all else ' for this folder; add --all') + '.')
        for item in items:
            print(f"{item['id']}  {label(item)}  ({item['messages']} messages)")
        return
    target = export(find(store, session), output or os.getcwd(), force)
    print(f'Exported to {target}. Open it with --convo {target}')
