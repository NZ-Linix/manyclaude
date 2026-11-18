"""Keyboard-driven terminal UI using Python's built-in curses module."""
import contextlib
import curses
import io
import os
import shlex
from pathlib import Path
import subprocess
import sys
import textwrap
import unicodedata


def export_ui(manager):
    return curses.wrapper(lambda screen: ProfileUI(screen, manager).export_wizard())


def pick_prompt(manager, name, groups):
    return curses.wrapper(lambda screen: ProfileUI(screen, manager).choose_prompt(name, groups))


class ProfileUI:
    def __init__(self, screen, manager):
        self.screen, self.manager = screen, manager
        self.selected = 0
        self.notice = 'Select a profile to launch it or manage its settings.'
        self.error = False
        curses.curs_set(0)
        curses.set_escdelay(50)
        screen.keypad(True)
        screen.notimeout(False)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        self.colors = curses.has_colors() and 'NO_COLOR' not in os.environ
        self.extended_palette = False
        self.left = 0
        if self.colors:
            curses.start_color()
            curses.use_default_colors()
            light = os.environ.get('COLORFGBG', '').split(';')[-1] in {'7', '15'}
            self.extended_palette = curses.COLORS >= 256
            if self.extended_palette:
                # Fixed xterm colors avoid the terminal's often garish ANSI yellow/cyan.
                accent, muted = (130, 242) if light else (173, 245)
                foreground, surface = (16, 254) if light else (255, 236)
                pairs = [(1, accent, -1), (2, muted, -1), (3, 28 if light else 108, -1),
                         (4, 124 if light else 174, -1), (5, foreground, surface)]
            else:
                # Default colors + inverse selection remain readable on either background.
                pairs = [(1, -1, -1), (2, -1, -1), (3, curses.COLOR_GREEN, -1),
                         (4, curses.COLOR_RED, -1), (5, -1, -1)]
            for pair, foreground, background in pairs:
                curses.init_pair(pair, foreground, background)

    def style(self, pair=0, bold=False):
        attribute = (curses.color_pair(pair) if self.colors else 0) | (curses.A_BOLD if bold else 0)
        if pair == 5 and not self.extended_palette:
            attribute |= curses.A_REVERSE
        return attribute

    def geometry(self):
        height, actual_width = self.screen.getmaxyx()
        width = min(actual_width, 112)
        self.left = max(0, (actual_width - width) // 2)
        return height, width

    @staticmethod
    def fit(text, width):
        text = ''.join(c if c.isprintable() else ' ' for c in str(text))
        used, result = 0, ''
        for char in text:
            size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in 'WF' else 1
            if used + size > width:
                return result[:-1] + '…' if result else ''
            result += char
            used += size
        return result

    def put(self, y, x, text, style=0, limit=None):
        height, width = self.geometry()
        if 0 <= y < height and 0 <= x < width - 1:
            text = self.fit(text, min(width - x - 1, limit) if limit is not None else width - x - 1)
            with contextlib.suppress(curses.error):
                self.screen.addstr(y, self.left + x, text, style)

    def row(self, y, text, active, width=None):
        _, screen_width = self.geometry()
        width = width or screen_width - 7
        if active:
            self.put(y, 3, ' ' * width, self.style(5))
        self.put(y, 3, (' › ' if active else '   ') + text,
                 self.style(5, True) if active else self.style(), limit=width)

    def frame(self, title, subtitle, footer='↑/↓ navigate   enter select   esc / q back'):
        self.screen.erase()
        height, width = self.geometry()
        if height < 22 or width < 48:
            self.put(0, 0, 'Make the terminal at least 48 columns × 22 rows.', self.style(1))
            self.put(2, 0, 'Resize to continue · q to quit')
            self.screen.refresh()
            return False
        self.put(1, 3, 'manyclaude', self.style(0, True))
        self.put(1, width - 12, 'v' + self.manager.VERSION, self.style(2))
        self.put(3, 3, title, self.style(1, True))
        self.put(4, 3, subtitle, self.style(2))
        self.put(5, 3, '─' * (width - 7), self.style(2))
        self.put(height - 3, 3, '─' * (width - 7), self.style(2))
        self.put(height - 2, 3, footer, self.style(2))
        return True

    def resize_key(self):
        while True:
            key = self.screen.get_wch()
            if key == curses.KEY_MOUSE:
                with contextlib.suppress(curses.error):
                    curses.getmouse()
                continue
            if key != '\x1b':
                return key
            # Unknown terminal reports also begin with Escape. Consume the
            # complete report so a click/focus event cannot act as Back.
            self.screen.timeout(0)
            try:
                try:
                    following = self.screen.get_wch()
                except curses.error:
                    return key
                if following not in ('[', 'O'):
                    curses.unget_wch(following)
                    return key
                self.screen.timeout(50)
                for index in range(128):
                    try:
                        part = self.screen.get_wch()
                    except curses.error:
                        break
                    if isinstance(part, str) and '@' <= part <= '~':
                        if following == '[' and index == 0 and part == 'M':
                            for _ in range(3):
                                with contextlib.suppress(curses.error):
                                    self.screen.get_wch()
                        break
            finally:
                self.screen.timeout(-1)

    def menu(self, title, subtitle, options, details=()):
        selected = 0
        while True:
            fits = self.frame(title, subtitle)
            height, width = self.geometry()
            if fits:
                capacity = max(1, height - 10 - min(len(details), 3))
                first = max(0, selected - capacity + 1)
                for row, label in enumerate(options[first:first + capacity]):
                    active = first + row == selected
                    self.row(7 + row, label, active)
                    if 'permanently' in label and not active:
                        self.put(7 + row, 6, label, self.style(4))
                for row, text in enumerate(details[:3]):
                    self.put(height - 6 + row, 3, text, self.style(2))
                self.screen.refresh()
            key = self.resize_key()
            if key in ('q', '\x1b'):
                return None
            if not fits:
                continue
            if key in (curses.KEY_UP, 'k'):
                selected = (selected - 1) % len(options)
            elif key in (curses.KEY_DOWN, 'j', '\t'):
                selected = (selected + 1) % len(options)
            elif key in ('\n', '\r', curses.KEY_ENTER):
                return selected

    def prompt(self, title, label, default='', help_text='', validator=None, hints=()):
        value = default
        error = ''
        try:
            while True:
                fits = self.frame(title, 'enter save / continue · esc cancel', 'enter continue   esc cancel   ctrl-u clear')
                height, width = self.geometry()
                if fits:
                    self.put(7, 3, label, self.style(0, True))
                    visible = value[-max(1, width - 9):]
                    self.put(9, 3, ' ' * (width - 7), self.style(5))
                    self.put(9, 3, '› ' + visible, self.style(5, True))
                    for row, line in enumerate(textwrap.wrap(help_text, width - 7)[:2]):
                        self.put(11 + row, 3, line, self.style(2))
                    for row, hint in enumerate(hints):
                        self.put(14 + row, 3, hint, self.style(2))
                    if error:
                        self.put(height - 4, 3, error, self.style(4))
                    curses.curs_set(1)
                    self.screen.move(9, self.left + min(width - 2, 5 + len(visible)))
                    self.screen.refresh()
                key = self.resize_key()
                if key == '\x1b':
                    return None
                if not fits:
                    continue
                if key in ('\n', '\r', curses.KEY_ENTER):
                    try:
                        return validator(value) if validator else value
                    except ValueError as exc:
                        error = str(exc)
                elif key in (curses.KEY_BACKSPACE, '\x7f', '\b'):
                    value = value[:-1]
                elif key == '\x15':
                    value = ''
                elif isinstance(key, str) and key.isprintable():
                    value += key
        finally:
            curses.curs_set(0)

    def operation(self, arguments, message):
        self.frame('Working…', 'Updating ManyClaude. This may take a moment.', 'Please wait')
        self.screen.refresh()
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.manager.main(arguments)
            self.notice, self.error = message, False
            return True
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            self.notice, self.error = str(exc), True
            self.message('Could not complete the action', str(exc), error=True)
            return False

    def message(self, title, text, error=False):
        while True:
            if self.frame(title, 'Press enter or escape to return', 'enter / esc return'):
                height, width = self.geometry()
                for row, line in enumerate(textwrap.wrap(text, width - 7)[:height - 10]):
                    self.put(7 + row, 3, line, self.style(4 if error else 2))
                self.screen.refresh()
            if self.resize_key() in ('\n', '\r', curses.KEY_ENTER, '\x1b', 'q'):
                return

    def create(self):
        while True:
            kind = self.menu('New profile', 'Choose where this account lives',
                             ['Create an isolated profile', 'Use the regular Claude app + claude command'],
                             ['Regular Claude uses your existing login and settings. Only one profile can use it.'])
            if kind is None:
                return
            if kind == 1:
                name = self.prompt('Regular Claude profile', 'Profile name', 'Main',
                                   'Uses your existing Claude Desktop and normal claude command. No credentials are moved.',
                                   self.manager.validate_name)
                if name is None:
                    continue
                self.operation(['create', name, '--regular'], f'Registered {name} · regular Claude')
                return
            values = ['', '', '', '']
            step = 0
            while step >= 0:
                name = values[0]
                fields = [
                    ('Profile name', '', 'Example: Personal, Work, or Client A.', self.manager.validate_name),
                    ('Desktop app name', 'Claude ' + name,
                     'Use a display name such as Work. Do not include .app.', self.manager.validate_name),
                    ('Claude Code command', 'claude-' + self.manager.slugify(name) if name else '',
                     'The command you will type in your shell. Use letters, numbers, - or _.',
                     lambda value: self.manager.validate_alias(value, self.manager.read_profiles())),
                    ('Applications folder', self.manager.default_apps_dir(),
                     'Desktop entries go in your user applications folder; no sudo needed.' if self.manager.LINUX else
                     'Use ~/Applications if the system Applications folder is not writable.',
                     lambda value: value.strip() or self.manager.default_apps_dir())]
                label, default, help_text, validator = fields[step]
                value = self.prompt(f'New profile · {step + 1} of 4', label, values[step] or default, help_text, validator)
                if value is None:
                    step -= 1
                    continue
                values[step] = value
                step += 1
                if step == 4:
                    name, app_name, alias, apps = values
                    if self.operation(['create', name, '--app-name', app_name, '--alias', alias, '--apps-dir', apps],
                                      f'Created {name} · command: {alias}'):
                        self.message('Your profile is ready', f'{name} has its own Desktop app and Code command: {alias}. '
                                     'Open a new terminal to use the command. Sign in separately in Desktop and Claude Code.')
                    return

    def launch_code(self, *arguments):
        # Give Claude a normal terminal, then return to the profile manager when it exits.
        curses.def_prog_mode()
        curses.endwin()
        try:
            result = subprocess.run([sys.executable, str(self.manager.ROOT / 'manyclaude'), *arguments])
            self.notice = 'Claude Code session ended.' if result.returncode == 0 else f'Claude Code exited with status {result.returncode}.'
            self.error = result.returncode != 0
            if result.returncode != 0:
                input('\nPress enter to return to ManyClaude…')
        finally:
            curses.reset_prog_mode()
            self.screen.clear()
            self.screen.refresh()


    def open_convo(self, *command):
        from convo_file import validate_target
        path = self.prompt('Open conversation file', 'Path to a .jsonl conversation', '~/',
                           'Claude reads this file and keeps writing the conversation into it. '
                           'A new path starts a new conversation there. The normal history is not touched.',
                           lambda value: str(validate_target(value.strip())))
        if path is not None:
            self.launch_code(*command, '--convo', path)

    def export_wizard(self):
        """Standalone `manyclaude export`: choose a store, then a conversation; saves to the current folder."""
        import custom_profiles

        while True:
            profiles = self.manager.read_profiles()
            custom = custom_profiles.read(self.manager.BASE)
            sources = [('*', 'Search everything · name or ID across all profiles')]
            sources += [(key, f'{p["name"]}  ·  {self.manager.alias_name(key, p)}') for key, p in profiles.items()]
            sources.append(('global', 'Global Claude Code'))
            sources += [('custom:' + key, f'{item["name"]}  ·  custom') for key, item in custom.items()]
            choice = self.menu('Export a conversation', 'Choose where the conversation lives',
                               [text for _, text in sources], [f'Exports to {Path.cwd()}'])
            if choice is None or self.export_convo(sources[choice][0]):
                return

    def choose_filtered(self, title, items, label, search, details):
        """Type-to-search list. Returns the chosen item, or None on escape."""
        query, selected, offset = '', 0, 0
        while True:
            terms = query.lower().split()
            shown = [item for item in items if all(term in search(item) for term in terms)]
            selected = max(0, min(selected, len(shown) - 1))
            subtitle = f'Search: {query}▏  ·  {len(shown)} of {len(items)}' if query else \
                f'{len(items)} conversations · type a name or ID to search'
            fits = self.frame(title, subtitle, '↑/↓ select   type search   enter choose   esc back')
            height, width = self.geometry()
            if fits:
                capacity = max(1, height - 13)
                offset = min(max(offset, selected - capacity + 1), selected)
                for row, item in enumerate(shown[offset:offset + capacity]):
                    self.row(7 + row, label(item), offset + row == selected)
                if not shown:
                    self.put(7, 6, 'No matches.', self.style(2))
                else:
                    for row, text in enumerate(details(shown[selected])[:3]):
                        self.put(height - 6 + row, 3, text, self.style(2))
                self.screen.refresh()
            key = self.resize_key()
            if key == '\x1b':
                return None
            if not fits:
                continue
            if key == curses.KEY_UP and shown:
                selected = (selected - 1) % len(shown)
            elif key in (curses.KEY_DOWN, '\t') and shown:
                selected = (selected + 1) % len(shown)
            elif key == curses.KEY_PPAGE:
                selected = max(0, selected - (height - 13))
            elif key == curses.KEY_NPAGE:
                selected = min(len(shown) - 1, selected + (height - 13))
            elif key in ('\n', '\r', curses.KEY_ENTER):
                if shown:
                    return shown[selected]
            elif key in (curses.KEY_BACKSPACE, '\x7f', '\b'):
                query, selected = query[:-1], 0
            elif key == '\x15':
                query, selected = '', 0
            elif isinstance(key, str) and key.isprintable():
                query, selected = query + key, 0

    def export_convo(self, source):
        import convo_export
        everything = source == '*'
        try:
            if everything:
                name, items = 'all profiles', convo_export.everything(self.manager)
            else:
                name, store = convo_export.store_for(self.manager, source)
                items = convo_export.conversations(store)
        except (ValueError, OSError) as exc:
            self.message('Could not list conversations', str(exc), error=True)
            return False
        if not items:
            self.message('No conversations', f'{name} has no saved conversations yet.')
            return False

        def details(item):
            project = str(item['project']).replace(str(Path.home()), '~', 1)
            first = 'First prompt: ' + item['prompt'] if item['prompt'] and item['prompt'] != item['name'] else ''
            return [f'ID {item["id"]}  ·  {item["messages"]} messages' +
                    (f'  ·  {item["source_name"]}' if everything else ''), project, first]

        while True:
            item = self.choose_filtered('Export a conversation · ' + name, items,
                                        lambda entry: convo_export.label(entry, everything),
                                        convo_export.search_text, details)
            if item is None:
                return False

            def new_file(value):
                path = Path(value.strip()).expanduser().absolute()
                if path.suffix != '.jsonl':
                    raise ValueError('Use a file name ending in .jsonl.')
                if path.exists():
                    raise ValueError(f'{path.name} already exists here. Choose another name.')
                return str(path)

            output = self.prompt('Export conversation', 'Save as', str(Path.cwd() / convo_export.default_name(item)),
                                 f'{item["name"]} · {item["messages"]} messages. Saves to the current folder.',
                                 new_file)
            if output is None:
                continue
            if self.operation(['export', item.get('source', source), item['id'], output], 'Exported conversation'):
                self.message('Conversation exported', f'Saved to {output}. '
                             'Open it with --convo or Open conversation file (.jsonl).')
                return True
            return False

    def review_reset(self, name, paths, keep_text):
        offset = 0
        while True:
            fits = self.frame('Clear ' + name + '?', 'Permanent deletion · quit Desktop and Code first',
                              '↑/↓ review paths   enter continue   esc cancel')
            height, width = self.geometry()
            if fits:
                lines = ['Local logins, settings, and history will be deleted.', keep_text, '']
                for path in paths:
                    lines.extend(textwrap.wrap(str(path), width - 9) + [''])
                capacity = height - 11
                offset = min(offset, max(0, len(lines) - capacity))
                for row, line in enumerate(lines[offset:offset + capacity]):
                    self.put(7 + row, 3, line, self.style(2))
                if len(lines) > capacity:
                    self.put(height - 4, 3, f'Lines {offset + 1}–{min(len(lines), offset + capacity)} of {len(lines)}', self.style(2))
                self.screen.refresh()
            key = self.resize_key()
            if key in ('q', '\x1b'):
                return False
            if not fits:
                continue
            if key in ('\n', '\r', curses.KEY_ENTER):
                return True
            if key in (curses.KEY_UP, 'k'):
                offset = max(0, offset - 1)
            elif key in (curses.KEY_DOWN, 'j'):
                offset += 1
            elif key == curses.KEY_NPAGE:
                offset += capacity
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - capacity)

    def profile(self, key):
        while key in self.manager.read_profiles():
            if not self.profile_action(key):
                return

    def profile_action(self, key):
        profile = self.manager.read_profiles()[key]
        alias = self.manager.alias_name(key, profile)
        regular = profile.get('regular', False)
        choice = self.menu(profile['name'], 'Manage this account', [
            'Open Claude Desktop', 'Launch Claude Code', 'Code command: claude (regular)' if regular else 'Rename Code command',
            'Check regular app' if regular else 'Refresh app & commands',
            'Unregister profile · keep data' if regular else 'Remove profile · keep data',
            'Unregister + clear regular Claude permanently' if regular else 'Remove profile + data permanently',
            'Reset logins, settings & history · keep profile', 'Code defaults · environment & flags',
            'Open conversation file (.jsonl)', 'Export a conversation to .jsonl'],
            [f'Code command: {alias}', f'Desktop data: {profile["desktop"]}', self.notice])
        if choice is None:
            return False
        if choice == 0:
            self.operation(['open', key], f'Opened {profile["name"]}')
        elif choice == 1:
            self.launch_code('code', key)
        elif choice == 2:
            if regular:
                self.message('Regular Claude command', 'This profile uses the original claude command. Create an isolated profile to use a custom command.')
                return True
            new_alias = self.prompt('Rename Code command', f'{profile["name"]} · current command: {alias}', alias,
                                    'Settings and credentials stay in the same profile. Open a new shell after renaming.',
                                    lambda value: self.manager.validate_alias(value, self.manager.read_profiles(), key))
            if new_alias is not None:
                self.operation(['alias', key, new_alias], f'Code command changed to {new_alias} · open a new terminal')
        elif choice == 3:
            self.operation(['refresh', key], f'Refreshed {profile["name"]}')
        elif choice == 7:
            self.code_defaults(key)
        elif choice == 8:
            self.open_convo('code', key)
        elif choice == 9:
            self.export_convo(key)
        elif choice == 4:
            confirm = self.menu('Remove ' + profile['name'] + '?', 'Remove the app and commands. Keep logins, settings, and history.',
                                ['Cancel', 'Remove profile · keep data'])
            if confirm == 1:
                self.operation(['remove', key], f'Removed {profile["name"]} · data retained')
        elif choice in (5, 6):
            paths = self.manager.reset_paths(key, profile)
            # The separate review screen shows every target rather than clipping a long list of paths.
            keep_text = ('Regular Claude stays installed.' if regular else 'App and commands stay installed.'
                         if choice == 6 else 'The profile app and commands will also be removed.')
            while self.review_reset(profile['name'], paths, keep_text):
                confirmation = self.prompt('Permanently clear ' + profile['name'], 'Type the profile name to confirm',
                                          help_text='Quit Desktop and Code sessions first. This clears stored logins, settings, and history.',
                                          hints=[f'Profile: {profile["name"]}', f'Desktop data: {profile["desktop"]}', f'Code data: {profile["code"]}'])
                if confirmation is None:
                    continue
                if confirmation == profile['name']:
                    command = ['reset', key, '--yes'] if choice == 6 else ['remove', key, '--purge', '--yes']
                    self.operation(command, f'Cleared {profile["name"]} data')
                else:
                    self.notice, self.error = 'Confirmation did not match. Nothing deleted.', True
                break
        return True

    def code_defaults(self, key):
        while True:
            profile = self.manager.read_profiles()[key]
            allow_external_imports = profile.get('allow_external_imports', False)
            import_action = ('Stop auto-allowing external imports' if allow_external_imports
                             else 'Auto-allow external imports')
            action = self.menu('Code defaults', 'Applies when launching Code through ManyClaude',
                               ['Import a shell alias', 'Edit environment & flags', 'Clear defaults', import_action],
                               ['Caller environment variables take precedence. Profile credentials stay isolated.',
                                'For regular Claude, saving defaults adds a managed claude alias. Reload your shell.'])
            if action is None:
                return
            if action == 0:
                source = self.prompt('Import shell alias', 'Alias to copy', 'cclaude',
                                     'Reads a simple env/claude alias from your shell config. Does not execute it.')
                if source:
                    self.operation(['code-options', key, '--from-alias', source], 'Imported Code defaults')
            elif action == 1:
                flags = shlex.join(profile.get('code_args', []))
                while True:
                    flags = self.prompt('Code defaults', 'Default flags', flags,
                                        'Example: --model sonnet --no-chrome. Quote values containing spaces.')
                    if flags is None:
                        break
                    variables = self.prompt('Code defaults', 'Environment variables',
                                            shlex.join([f'{k}={v}' for k, v in profile.get('code_env', {}).items()]),
                                            'Example: CLAUDE_CODE_DISABLE_AUTO_MEMORY=1. Quote values containing spaces.')
                    if variables is None:
                        continue
                    try:
                        args = ['code-options', key, '--flags=' + flags]
                        for assignment in shlex.split(variables):
                            args.extend(['--env', assignment])
                        # An empty input explicitly clears existing environment defaults.
                        if not variables.strip():
                            args.append('--clear-env')
                        self.operation(args, 'Saved Code defaults')
                    except ValueError as exc:
                        self.message('Invalid Code defaults', str(exc), error=True)
                    break
            elif action == 2:
                self.operation(['code-options', key, '--clear'], 'Cleared Code defaults')
            elif action == 3:
                flag = '--ask-external-imports' if allow_external_imports else '--allow-external-imports'
                self.operation(['code-options', key, flag], 'Updated external import defaults')

    def config_sync(self):
        from profile_config import STATE
        enabled = (self.manager.BASE / STATE).exists()
        actions = ['Sync now', 'Disable sync'] if enabled else ['Enable and sync now']
        choice = self.menu('Profile configuration sync', 'Enabled' if enabled else 'Disabled', actions,
                           ['Shares MCP definitions, Code settings and user customization files.',
                            'Logins, chats and folder trust stay separate.',
                            'Conflicts favor regular Claude, then alphabetical profile order.'])
        if choice is not None:
            action = ['--now', '--disable'][choice] if enabled else '--enable'
            message = 'Configuration sync disabled' if action == '--disable' else 'Profile configuration synced'
            self.operation(['sync-config', action], message)

    def global_code(self):
        from global_code import launcher_name

        while True:
            name = launcher_name(self.manager.BASE)
            actions = ['Launch Claude Code', 'Rename command' if name else 'Install command']
            if name:
                actions.append('Remove command')
            actions += ['Open conversation file (.jsonl)', 'Export a conversation to .jsonl']
            command = str(self.manager.command_bin_dir() / name) if name else 'Command is not installed'
            choice = self.menu('Global Claude Code', 'Use /model to switch models and accounts in one session', actions,
                               [command, self.notice])
            if choice is None:
                return
            if choice == len(actions) - 1:
                self.export_convo('global')
            elif choice == len(actions) - 2:
                self.open_convo('global')
            elif choice == 0:
                self.launch_code('global')
            elif choice == 1:
                new_name = self.prompt(actions[choice], 'Executable name', name or 'claude-global')
                if new_name is None:
                    continue
                new_name = new_name.strip()
                if new_name:
                    self.operation(['global-alias', '--', new_name], f'Global command set to {new_name}')
                else:
                    self.message('Executable name required', 'Enter a name such as claude-global.', error=True)
            elif choice == 2:
                self.operation(['global-alias', '--disable'], 'Removed the global command')

    def custom_profiles(self):
        import custom_profiles

        while True:
            profiles = custom_profiles.read(self.manager.BASE)
            keys = list(profiles)
            options = [f'{item["name"]}  ·  {item["command"]}' for item in profiles.values()]
            options += ['+ New custom profile', 'Prompt folders']
            choice = self.menu('Custom profiles', 'Global Claude Code with its own flags, environment & prompts', options,
                               ['Every custom profile can use all accounts and /model routes, like Global.',
                                f'Put {custom_profiles.PLACEHOLDER} in flags or env to pick a .md prompt at launch.',
                                self.notice])
            if choice is None:
                return
            if choice == len(keys):
                self.custom_edit()
            elif choice == len(keys) + 1:
                self.prompt_folders()
            else:
                self.custom_profile(keys[choice])

    def custom_edit(self, key=None):
        """Create (key=None) or edit a custom profile in back-navigable steps."""
        import custom_profiles

        item = custom_profiles.read(self.manager.BASE).get(key, {}) if key else {}
        values = [item.get('name', ''), item.get('command', ''), shlex.join(item.get('args', [])),
                  shlex.join(f'{k}={v}' for k, v in item.get('env', {}).items())]
        placeholder = custom_profiles.PLACEHOLDER
        step = 1 if key else 0
        while True:
            if step < (1 if key else 0):
                return
            if step == 4:
                break
            name = values[0]
            fields = [
                ('Profile name', '', 'Example: Reviewer, Writer, or Custom Claude.', self.manager.validate_name),
                ('Command', self.manager.slugify(name) if name and not key else '',
                 'The executable you will type in your shell. Use letters, numbers, - or _.',
                 lambda value: value if value == item.get('command') else self.manager.validate_alias(
                     value, self.manager.read_profiles(), own_custom=key or self.manager.slugify(values[0]))),
                ('Default Claude Code flags', '',
                 f'Example: --model claude-opus-5-5 --system-prompt-file {placeholder}. '
                 'Quote values containing spaces.', None),
                ('Environment variables', '',
                 'Example: CLAUDE_CODE_DISABLE_AUTO_MEMORY=1. Leave empty for none. '
                 f'{placeholder} also works in values.', None)]
            label, default, help_text, validator = fields[step]
            hints = [f'{placeholder} opens a picker of .md files from your prompt folders at launch.'] if step >= 2 else []
            value = self.prompt(('Edit ' + item['name'] if key else 'New custom profile') + f' · {step + 1} of 4',
                                label, values[step] or default, help_text, validator, hints)
            if value is None:
                step -= 1
                continue
            values[step] = value
            step += 1
        name, command, flags, variables = values
        try:
            args = ['custom', 'set', name, '--command', command, '--flags=' + flags]
            assignments = shlex.split(variables)
        except ValueError as exc:
            self.message('Invalid custom profile', str(exc), error=True)
            return
        for assignment in assignments:
            args.extend(['--env', assignment])
        if not assignments:
            args.append('--clear-env')
        if not key:
            store = self.menu('New custom profile · history', 'Where should its settings and transcripts live?',
                              ['Share with Global (default)', 'Separate store'],
                              ['Shared: /resume lists Global conversations too.',
                               'Separate: own settings, memory and transcripts. Toggle later in its menu.'])
            if store is None:
                return
            args.extend(['--store', ('global', 'separate')[store]])
        if self.operation(args, f'Saved {name} · command: {command}') and not key:
            self.message('Custom profile ready', f'Run {command} in a new terminal, or launch it from this menu. '
                         'It uses every signed-in account through Global routing.')

    def custom_profile(self, key):
        import custom_profiles

        while True:
            item = custom_profiles.read(self.manager.BASE).get(key)
            if item is None:
                return
            separate = item.get('store') == 'separate'
            choice = self.menu(item['name'], 'Custom Global Claude Code profile', [
                'Launch Claude Code', 'Edit flags & environment', 'Rename command',
                f'Separate store (history & settings): {"ON" if separate else "OFF"} · toggle',
                'Remove custom profile', 'Open conversation file (.jsonl)', 'Export a conversation to .jsonl'],
                [f'Command: {self.manager.command_bin_dir() / item["command"]}',
                 'Flags: ' + (shlex.join(item.get('args', [])) or '(none)') + '   Env: ' +
                 (shlex.join(f'{k}={v}' for k, v in item.get('env', {}).items()) or '(none)'),
                 'Store: ' + str(custom_profiles.store_directory(self.manager.BASE, key, item))])
            if choice is None:
                return
            if choice == 0:
                self.launch_code('custom-run', key)
            elif choice == 5:
                self.open_convo('custom-run', key)
            elif choice == 6:
                self.export_convo('custom:' + key)
            elif choice == 1:
                self.custom_edit(key)
            elif choice == 2:
                command = self.prompt('Rename command', f'{item["name"]} · current command: {item["command"]}',
                                      item['command'], 'Open a new terminal after renaming.',
                                      lambda value: value if value == item['command'] else self.manager.validate_alias(
                                          value, self.manager.read_profiles(), own_custom=key))
                if command is not None and command != item['command']:
                    self.operation(['custom', 'set', item['name'], '--command', command],
                                   f'Command changed to {command}')
            elif choice == 3:
                self.operation(['custom', 'set', item['name'], '--store', 'global' if separate else 'separate'],
                               'Separate store ' + ('off · sharing Global history' if separate else
                                                    'on · own history & settings'))
            elif choice == 4:
                confirm = self.menu('Remove ' + item['name'] + '?', 'Removes the profile and its command',
                                    ['Cancel', 'Remove custom profile'],
                                    ['Accounts and Global history are not affected.'])
                if confirm == 1:
                    self.operation(['custom', 'remove', key], f'Removed {item["name"]}')
                    return

    def prompt_folders(self):
        import custom_profiles

        while True:
            folders = custom_profiles.read_folders(self.manager.BASE)
            counts = [sum(len(files) for _, files in custom_profiles.prompt_files([folder])) for folder in folders]
            options = [f'{folder}  ·  {count} .md' for folder, count in zip(folders, counts)] + ['+ Add folder']
            choice = self.menu('Prompt folders', 'Their .md files appear in the custom profile prompt picker', options,
                               ['Subfolders are included; hidden folders are skipped.', self.notice])
            if choice is None:
                return
            if choice == len(folders):
                def existing_folder(value):
                    path = Path(value.strip()).expanduser()
                    if not value.strip() or not path.is_dir():
                        raise ValueError(f'Not a folder: {path}')
                    return str(path.absolute())
                folder = self.prompt('Add prompt folder', 'Folder path', '~/', 'Example: ~/Prompts', existing_folder)
                if folder is not None:
                    self.operation(['prompt-folders', '--add', folder], f'Added {folder}')
            else:
                confirm = self.menu('Remove folder?', folders[choice], ['Cancel', 'Remove from prompt picker'],
                                    ['The folder and its files are not deleted.'])
                if confirm == 1:
                    self.operation(['prompt-folders', '--remove', folders[choice]], 'Removed prompt folder')

    def choose_prompt(self, name, groups):
        """Return the chosen prompt path, or None to launch without one. Escape cancels the launch."""
        query, selected, offset, previews = '', 0, 0, {}
        while True:
            rows = [('none', 'No prompt · launch without one', None)]
            for root, files in groups:
                matches = [path for path in files if query.lower() in str(path.relative_to(root)).lower()]
                if matches or not query:
                    rows.append(('header', str(root).replace(str(Path.home()), '~', 1), None))
                    rows.extend(('file', str(path.relative_to(root)), path) for path in matches)
                    if not files:
                        rows.append(('header', '  (no .md files)', None))
            if not groups:
                rows.append(('header', 'No prompt folders yet. Add them under p in manyclaude.', None))
            choices = [index for index, row in enumerate(rows) if row[0] != 'header']
            selected = min(selected, len(choices) - 1)
            fits = self.frame('Choose a prompt · ' + name, 'Filter: ' + query + '▏' if query else 'Type to filter',
                              '↑/↓ select   type filter   enter launch   esc cancel')
            height, width = self.geometry()
            if fits:
                wide = width >= 100
                list_width = 48 if wide else width - 7
                capacity = max(1, height - 11)
                current = choices[selected]
                offset = min(max(offset, current - capacity + 1), current)
                if selected == 0:
                    offset = 0
                for line, (kind, label, _) in enumerate(rows[offset:offset + capacity]):
                    if kind == 'header':
                        self.put(7 + line, 4, label, self.style(2), limit=list_width)
                    else:
                        self.row(7 + line, ('  ' if kind == 'file' else '') + label, offset + line == current, list_width)
                path = rows[current][2]
                if wide and path is not None:
                    if path not in previews:
                        try:
                            with path.open(errors='replace') as stream:
                                previews[path] = stream.read(4096).splitlines()
                        except OSError as exc:
                            previews[path] = [str(exc)]
                    lines = [wrapped for text in previews[path]
                             for wrapped in (textwrap.wrap(text, width - 60) or [''])]
                    for line, text in enumerate(lines[:capacity]):
                        self.put(7 + line, 56, text, self.style(2))
                self.screen.refresh()
            key = self.resize_key()
            if key == '\x1b':
                raise KeyboardInterrupt
            if not fits:
                continue
            if key == curses.KEY_UP:
                selected = (selected - 1) % len(choices)
            elif key in (curses.KEY_DOWN, '\t'):
                selected = (selected + 1) % len(choices)
            elif key == curses.KEY_PPAGE:
                selected = max(0, selected - (height - 11))
            elif key == curses.KEY_NPAGE:
                selected = min(len(choices) - 1, selected + (height - 11))
            elif key in ('\n', '\r', curses.KEY_ENTER):
                path = rows[choices[selected]][2]
                return None if path is None else str(path)
            elif key in (curses.KEY_BACKSPACE, '\x7f', '\b'):
                query, selected = query[:-1], 0
            elif key == '\x15':
                query, selected = '', 0
            elif isinstance(key, str) and key.isprintable():
                query += key
                selected = 1 if len(choices) > 1 else 0

    def run(self):
        while True:
            profiles = self.manager.read_profiles()
            keys = list(profiles)
            self.selected = min(self.selected, len(keys))
            fits = self.frame('Your accounts', 'Desktop and Code profiles · g global · c custom · p prompts',
                              '↑/↓ select   enter manage   s sync   n new   r refresh   u usage   q quit')
            height, width = self.geometry()
            if fits:
                wide = width >= 100
                stacked = width < 72
                list_width = 41 if wide else width - 7
                capacity = max(1, (height - (14 if wide else 18)) // 2) if wide or stacked else max(1, height - 18)
                first = max(0, self.selected - capacity + 1)
                self.put(7, 6, 'PROFILE' if keys else 'GET STARTED', self.style(2))
                if keys and not wide and not stacked:
                    self.put(7, 30, 'CODE ALIAS', self.style(2))
                    self.put(7, width - 17, 'DESKTOP', self.style(2))
                for row, index in enumerate(range(first, min(len(keys) + 1, first + capacity))):
                    active = index == self.selected
                    y = 9 + row * (2 if wide or stacked else 1)
                    if index == len(keys):
                        self.row(y, '+ Create a profile', active, list_width)
                        continue
                    p = profiles[keys[index]]
                    display_name = p['name'] + (' · regular' if p.get('regular') else '')
                    self.row(y, self.fit(display_name, 33 if wide or stacked else 21), active, list_width)
                    alias = self.manager.alias_name(keys[index], p)
                    if wide or stacked:
                        self.put(y + 1, 6, alias, self.style(2), limit=list_width - 5)
                    else:
                        self.put(y, 30, alias, self.style(5) if active else self.style(), limit=max(1, width - 49))
                        self.put(y, width - 17, 'Running' if self.manager.running(p) else 'Closed',
                                 self.style(5) if active else self.style(2))
                if keys and self.selected < len(keys):
                    key = keys[self.selected]
                    p = profiles[key]
                    alias = self.manager.alias_name(key, p)
                    def path_label(path):
                        value = str(path)
                        home = str(Path.home())
                        return '~' + value[len(home):] if value.startswith(home + '/') else value
                    if wide:
                        x = 49
                        self.put(7, x, p['name'], self.style(0, True))
                        self.put(8, x, '● Desktop running' if self.manager.running(p) else '○ Desktop closed',
                                 self.style(3) if self.manager.running(p) else self.style(2))
                        self.put(10, x, 'Code command', self.style(2))
                        self.put(11, x, alias, self.style(0, True))
                        if p.get('regular'):
                            self.put(12, x, 'Uses the original app and command', self.style(2))
                        elif not p.get('rc'):
                            self.put(12, x, 'Shell PATH setup disabled', self.style(1))
                        self.put(13, x, 'Desktop app', self.style(2))
                        self.put(14, x, path_label(p['app']))
                        self.put(16, x, 'Profile data', self.style(2))
                        self.put(17, x, path_label(p['desktop'] if p.get('regular') else Path(p['desktop']).parent))
                    else:
                        self.put(height - 7, 3, 'App   ' + path_label(p['app']), self.style(2))
                        self.put(height - 6, 3, 'Data  ' + path_label(Path(p['desktop']).parent), self.style(2))
                elif not keys:
                    self.put(12, 3, 'Keep one account in each profile.', self.style(2))
                    self.put(13, 3, 'Create Personal or Work to get started.', self.style(2))
                notice = 'Press enter to create your first profile.' if not keys and not self.error else self.notice
                self.put(height - 4, 3, notice, self.style(4 if self.error else 2))
                self.screen.refresh()
            key = self.resize_key()
            if key == 'q':
                return
            if not fits:
                continue
            if key in (curses.KEY_UP, 'k'):
                self.selected = (self.selected - 1) % (len(keys) + 1)
            elif key in (curses.KEY_DOWN, 'j', '\t'):
                self.selected = (self.selected + 1) % (len(keys) + 1)
            elif key in ('n', 'N'):
                self.create()
            elif key in ('r', 'R'):
                self.operation(['refresh'], 'Refreshed all apps and commands')
            elif key in ('s', 'S'):
                self.config_sync()
            elif key in ('g', 'G'):
                self.global_code()
            elif key in ('c', 'C'):
                self.custom_profiles()
            elif key in ('p', 'P'):
                self.prompt_folders()
            elif key in ('u', 'U'):
                choice = self.menu('OpenUsage', 'Account discovery for usage reporting',
                                   ['Enable / sync accounts', 'Disable and remove exports'],
                                   ['Sign in to Code once per profile. Restart OpenUsage after syncing.',
                                    'Usage reporting only. Chats stay in their own profiles.'])
                if choice in (0, 1):
                    args = ['openusage'] + (['--disable'] if choice == 1 else [])
                    if self.operation(args, 'OpenUsage exports updated'):
                        self.message('OpenUsage', 'Exports updated. Restart OpenUsage to discover account changes. '
                                     'Unsigned-in profiles are skipped. Isolated profile logins can renew in OpenUsage.')
            elif key in ('\n', '\r', curses.KEY_ENTER):
                if self.selected == len(keys):
                    self.create()
                else:
                    self.profile(keys[self.selected])
