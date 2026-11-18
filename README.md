# manyclaude

A small macOS and Linux CLI for multiple Claude accounts, including **Arch Linux and Omarchy**. No pip/npm packages, server, or background chat service. Requires Python 3 and an installed Claude Desktop app. macOS also needs Apple's Command Line Tools (`xcode-select --install`); Linux does not. Claude Code can be installed natively, through a package manager, or through **mise**, including Omarchy's agent installer.

```sh
./install.sh
source ~/.zshrc
manyclaude
```

## One-line install

macOS / Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/NZ-Linix/manyclaude/main/install.sh | sh
```

Windows (PowerShell; installs into WSL, since ManyClaude runs on Linux there):

```powershell
irm https://raw.githubusercontent.com/NZ-Linix/manyclaude/main/install.ps1 | iex
```

While the repository is private, those raw URLs need authentication. With the GitHub CLI signed in (`gh auth login`), use:

```sh
gh api repos/NZ-Linix/manyclaude/contents/install.sh -H 'Accept: application/vnd.github.raw' | sh
```

```powershell
gh api repos/NZ-Linix/manyclaude/contents/install.ps1 -H 'Accept: application/vnd.github.raw' | Out-String | iex
```

The installers use `gh auth token` or `GITHUB_TOKEN` to download the source. `MANYCLAUDE_REF` picks a branch or tag; `MANYCLAUDE_WSL_DISTRO` picks the WSL distribution on Windows.

## Arch Linux / Omarchy installation

On Arch, install Python and Claude Desktop, then install ManyClaude as your normal user:

```sh
sudo pacman -S python
# Claude Desktop from AUR (the claude-desktop executable is detected automatically):
yay -S claude-desktop

# Normal native Claude Code installation (works on Arch and Omarchy; no mise needed):
curl -fsSL https://claude.ai/install.sh | bash

./install.sh
source ~/.bashrc
manyclaude
```

If Claude Code is already installed through Omarchy's **default coding agent** selector, reuse that installation; no second CLI is needed. [Omarchy's agent selector](https://github.com/omacom/omarchy/blob/master/bin/omarchy-default-agent) installs Claude with `mise use -g claude`. ManyClaude preserves the caller's PATH selection, finds mise shims and `~/.local/bin` when they are absent from GUI environments, resolves the current CLI with `mise which claude`, and loads its runtime environment before applying profile isolation. It supports `MISE_DATA_DIR` and `MISE_SHIMS_DIR`. Version choices in trusted project mise configs still apply. No shell startup files are executed for discovery, and no installed CLI version is pinned in a profile.

The [native Linux installer](https://code.claude.com/docs/en/setup) works on Omarchy just as it does on other Linux distributions. ManyClaude finds `~/.local/bin/claude` even when a desktop launcher has a limited PATH. Mise is optional: an already selected mise version is respected, while an unusable mise shim falls back to a normal CLI installation. For GUI discovery, the native user install is checked before the supplemental mise shims.

If you prefer Omarchy’s mise-managed installation instead:

```sh
# Omarchy includes mise. On plain Arch, install it first: sudo pacman -S mise
mise use -g claude@latest
```

AUR packages are community maintained. The requested `yay -S claude-desktop` installation is explicitly supported whenever that package supplies the `claude-desktop` executable. As of October 10, 2026, [the upstream repackaging project](https://github.com/aaddrick/claude-desktop-debian) reports its AUR package as unavailable. Package names/availability can change; ManyClaude cannot fix an unavailable AUR package. It also detects `claude-desktop-unofficial`, or you can select an installed executable/AppImage explicitly:

```sh
manyclaude create Work --claude-app /path/to/claude-desktop
```

On Linux, `create Work` creates a named app-menu launcher at `~/.local/share/applications/manyclaude-work.desktop` plus the same `claude-work` executable command used on macOS. `--app-name` changes its displayed name; `--apps-dir` changes the desktop-entry directory. Isolated account data lives at `~/.claude-<slug>/` (for example `~/.claude-work/`). `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, and `XDG_CACHE_HOME` are respected for manager metadata, app-menu entries, and regular Claude; `MANYCLAUDE_HOME` overrides the manager metadata root, not isolated account directories. Automatic profile commands support bash, zsh, and fish. Bash users reload `~/.bashrc`, zsh users reload `~/.zshrc`, and fish users reload `~/.config/fish/config.fish` (or `$XDG_CONFIG_HOME/fish/config.fish`). Fish commands use native functions that forward arguments unchanged. Run `./install.sh` from fish to install its PATH setup; each profile uses the shell it was created from, or an explicit `--rc /path/to/config.fish`. Other shells can use `--no-alias` and `manyclaude code <profile>`.

The Linux workflow supports creating/listing profiles, isolated Desktop and Code launches, regular Claude, Code defaults and alias renaming, refresh, reset, removal/purge, and the OpenUsage file credential bridge. Each Desktop launcher invokes the package-owned runtime with `CLAUDE_USER_DATA_DIR`, `--user-data-dir`, and the profile's Code directories. Package updates apply on the next launch, without copying Electron or overwriting AUR files. The app menu uses the installed `claude-desktop` icon. X11 window classes distinguish profiles where the app honors Electron's `--class`; Wayland/Hyprland grouping and window titles depend on the packaged app/compositor. Login callbacks still depend on the package's `claude://` handler; log into Desktop profiles one at a time. Desktop features such as Cowork depend on the Linux package and its system requirements.

`--regular` uses the installed Desktop executable, `~/.config/Claude`, and `~/.claude`. Linux reset removes the corresponding file-based credentials and profile data without calling macOS Keychain tools. Regular reset also clears Claude's XDG cache and `~/.claude.json` backups. `open` detaches Desktop from the terminal; launch errors are recorded in `<ManyClaude home>/desktop-launch.log`.

On Arch/Omarchy, `manyclaude update-claude` updates Desktop and Code, then refreshes all profile launchers. It identifies the installed package names with pacman and invokes yay or paru; binary-repository packages can also update through sudo/pacman. Native Code updates through its own installer, mise installs through mise, and npm installs through npm. Package-manager confirmations and sudo prompts remain interactive; `manyclaude update-claude --yes` accepts package confirmations but may still request your sudo password. Quit Desktop before updating. Standalone AppImages and other Linux package managers still need their distributor’s update workflow.

Run the combined updater:

```sh
manyclaude update-claude
```

You can also update manually with the owners, then refresh launchers:

```sh
yay -S claude-desktop         # or the actual Desktop package you installed
mise upgrade claude          # for Omarchy/mise-managed Code
# Native Code installations instead use: claude update
manyclaude refresh
```

The remaining `.app`, Dock, signing, and Keychain details below describe macOS. Linux uses the XDG paths and desktop entries described above. Linux integration tests exercise fixture executables; real AUR Desktop authentication, Wayland behavior, and Cowork still require testing with the installed package on your desktop.

The full-screen terminal UI shows profiles, their Code aliases, app/data paths, and Desktop running status. Use ↑/↓ (or j/k) and Enter to navigate; `n` creates a profile, `r` refreshes all launchers, and Esc goes back. It uses a muted warm accent, gray labels, and a charcoal selection row instead of terminal-dependent yellow/cyan. Wide terminals show account details beside the profile list; narrow terminals use a compact layout. It respects the terminal background, adapts when `COLORFGBG` identifies a light terminal, supports resizing, and honors `NO_COLOR`. Press q or Ctrl-C to exit. Claude Code takes over the terminal during a session; exiting Code returns to the manager.

Or use commands directly:

```sh
manyclaude create Personal
manyclaude create Work --alias cc-work
manyclaude open personal
claude-personal                  # in a new terminal, after creating the profile
cc-work --resume
manyclaude code work -- --model sonnet
manyclaude list
manyclaude refresh               # refresh apps/icons and repair aliases
manyclaude alias personal cc-personal  # change an existing profile’s command
manyclaude remove work           # keeps account data
manyclaude remove personal --purge  # permanently delete app, alias, and data
```

`create "Client A"` generates `/Applications/Claude Client A.app` and `claude-client-a`. Profile names may contain spaces or Unicode; command names use lowercase ASCII slugs. Colliding names are rejected. If `/Applications` isn't writable, use `--apps-dir ~/Applications`; don't run the manager with sudo.

Custom aliases can be chosen in the creation wizard, edited under **Rename Code alias**, or changed with `manyclaude alias <profile> <new-alias>`. Existing profiles retain their original `claude-<slug>` alias until changed. Renaming leaves the app name, data folders, and credentials intact. Duplicate aliases, unsafe characters, existing system commands, and aliases/functions already defined in the startup file are rejected. Open a new terminal after renaming; existing shells retain the old alias until restarted.

Use `manyclaude create Work --app-name Work` to create `Work.app` instead of `Claude Work.app`. The TUI also asks for a Desktop app name, defaulting to `Claude <profile>`. This changes only the launcher name; profile folders and the Code alias still use the profile name. Existing profiles retain their app names.

Optional create flags: `--app-name Work`, `--alias cc-personal`, `--claude-app /path/to/Claude.app`, `--rc /path/to/.zshrc`, `--no-alias`, `--apps-dir /path/to/apps`.

## Regular Claude and reset

Choose **Use the regular Claude app + claude command** when creating a profile in the TUI, or run:

```sh
manyclaude create Main --regular
manyclaude open main
manyclaude code main
```

Only one profile can use the regular setup. It registers the existing Claude Desktop app and normal `claude` command, without creating an app, adding a shell alias, moving credentials, or migrating an isolated account. Its app and command continue to update normally. Other profiles remain isolated. Launching Code through this profile clears inherited profile-directory overrides but keeps normal authentication environment variables.

**Reset logins, settings & history · keep profile** clears a profile's local account data while keeping the profile and its app/command:

```sh
manyclaude reset main
```

For regular Claude, reset clears `~/Library/Application Support/Claude` (and the legacy `Claude-3p` folder), `~/.claude`, `~/.claude.json` and its backups, the normal Claude Code Keychain credential, and Claude Desktop logs, cache, preferences, and saved window state. The TUI provides a scrollable list of targets and requires typing the exact profile name. The CLI also lists targets and requires that confirmation; `--yes` confirms explicitly for scripts. Quit all Desktop and Code sessions before resetting regular Claude. Symlinks redirecting these data paths are rejected.

Reset does not uninstall `/Applications/Claude.app` or Claude Code, delete cloud conversations, remove project-local `.claude` folders, clear exported API keys, or delete the shared Desktop Keychain encryption key. Desktop preferences are shared by its instances; regular reset resets those preferences too. Isolated profile folders and credentials are kept.

For a regular profile, `manyclaude remove main` only unregisters it. `manyclaude remove main --purge` resets its regular data and unregisters it, while keeping the original app and CLI installed. For isolated profiles, reset clears their account data while preserving their launchers and aliases; removal behavior is unchanged.

## Account separation

Each isolated profile gets a private `~/.claude-<slug>/` directory on both macOS and Linux:

- `desktop/`: Desktop login, cookies, local settings, caches, and logs through `CLAUDE_USER_DATA_DIR` and `--user-data-dir`.
- The directory itself: Claude Code settings, sessions, plugins, and credentials through `CLAUDE_CONFIG_DIR` and `CLAUDE_SECURESTORAGE_CONFIG_DIR`. The latter also selects a distinct macOS Keychain service. Linux Code credentials stay in the profile’s `.credentials.json` file.

Desktop's Code processes inherit the profile's Code configuration environment, but Desktop and terminal OAuth logins remain separate. Sign into the same account in both. Storage migration locally verifies and relocates existing macOS Code Keychain credentials before retiring their old service names. Inherited API keys/OAuth tokens and `ANTHROPIC_PROFILE` are cleared on launch so they don't silently select a different account. Project-local `.claude` settings and machine-wide managed policies still apply. This is account/config separation, not a filesystem security boundary; apps retain your normal user permissions. Desktop may use a shared Keychain encryption key; its login data is in each separate Desktop folder. Claude Console sign-ins without an API key are not isolated by `CLAUDE_CONFIG_DIR`; use claude.ai subscription accounts for this workflow.

[Claude Code's documented multiple-account setup](https://code.claude.com/docs/en/authentication#log-in-with-multiple-accounts) uses `CLAUDE_CONFIG_DIR`. Desktop's environment override was verified in the locally installed app (2.19675.0), and is not a public compatibility promise from Anthropic.

### Custom skills across profiles

Install personal skills for Claude Code once, choosing a global installation:

```sh
npx skills add <repository> --global --agent claude-code
```

ManyClaude links custom skills from `~/.claude/skills` into each isolated profile's skills directory when creating or refreshing its launcher and before launching CLI or Desktop (including Finder/Dock launches). Skills installed later are picked up on the next launch; changes to linked skill files are shared directly. Existing profile-local skills take precedence when names match. Desktop's account-specific `skills/synced` directory and hidden metadata remain private. Logins, settings, plugins, and history remain isolated. Resetting or purging an isolated profile removes its links without deleting the shared skill files. Project-local skill installations continue to apply to that project.

### Profile configuration sync

Press **s** in the ManyClaude wizard and select **Enable and sync now**. Sync is off by default. You can also use the CLI:

```sh
manyclaude sync-config --enable
manyclaude sync-config           # show status
manyclaude sync-config --now     # sync without launching Claude
manyclaude sync-config --disable
```

Sync shares Claude Code user settings, user and local-project MCP definitions, selected global Code preferences, Desktop MCP definitions and basic UI preferences, and user-owned `CLAUDE.md`, agents, commands, rules, hooks, output styles, and local skill files. Existing skill links work as before. Plugin settings sync, while installed plugins and marketplaces are managed by Claude in each profile. MCP definitions are copied as configured, including any tokens inlined in them.

ManyClaude merges existing configuration across registered profiles and picks up additions, edits, and removals before launching Code or Desktop. When multiple profiles change the same setting, MCP server, or file between syncs, regular Claude wins if it changed that entry. Otherwise, profile slugs determine priority in alphabetical order. This same order resolves differences when first enabling sync. Active sessions may need to restart to read updated settings. Launch regular Claude through ManyClaude or run **Sync now** to pick up changes made using its original launcher.

Logins, histories, cloud connectors, project trust, credential stores, managed settings, and profile-specific ManyClaude launch defaults stay separate. Disabling sync leaves the configuration already present in each profile. Resetting a profile clears its local data, and its next sync picks up the shared configuration again.

## Apps, icons, and updates

Quit all Claude Desktop apps, then run `manyclaude update-claude`. It reads [Anthropic's Desktop release feed](https://downloads.claude.ai/releases/darwin/universal/RELEASES.json), downloads the current release when needed, verifies Anthropic's signature, replaces the original app, runs the shared native CLI's `install latest`, and refreshes all custom apps. It bypasses your shell aliases and saved profile flags. Logins, settings, profile names, and icons are preserved. Reopen Desktop and restart Code sessions afterward. Original app locations must be writable; the command requires an existing native Claude Code installation (Homebrew-owned installations must be updated through Homebrew). The command updates Claude, not ManyClaude itself.

Each named `.app` contains its own Claude Desktop runtime plus a native profile launcher. Its unique bundle identifier and display name also apply to the **running Dock app**, so `Clawde.app` appears as **Clawde**, separately from the normal Claude app. The original icon resources and asset catalog are preserved so macOS applies the same appearance, lighting, and dark-mode treatment as the regular Claude app. Pin the named app from `/Applications` or use **Keep in Dock** on its running icon; launching that pinned app always goes through the profile launcher and uses the correct account folders. The internal Electron bundle name stays `Claude` so its helper processes still work.

The profile app is cloned from the main Claude installation using APFS copy-on-write when available, with a normal copy fallback on other filesystems. Its launcher and runtime are signed locally, preserving Electron's JIT and other non-identity entitlements. Anthropic's restricted signing identity and Keychain access groups cannot be assigned to a local signature. macOS may request permissions or Keychain access again on the first launch; the original app and all profile data are retained. Features specifically requiring Anthropic's restricted signing identity may differ in these custom copies.

Update Desktop through the regular Claude app in its original location. A profile launcher detects changes to the source version/executable and rebuilds its copy before the next launch. Profiles must be quit before their apps are refreshed. `manyclaude refresh` also rebuilds profile apps/icons manually, including upgrading launchers created by older manyclaude versions. Update Code normally with `claude update` (or your package manager); aliases resolve `claude` from PATH each time. If Claude moves to a different app path, recreate the profile with `--claude-app` (remove retains its data). The installed manyclaude CLI is required for automatic Desktop-copy refresh.

Log into Desktop profiles one at a time: browser `claude://` login callbacks can route to the default Claude app. If a profile stays logged out, quit the other Desktop instances and retry. This is a limitation of running multiple instances of Claude's application.

## Installation and removal

The installer copies the tool into `~/.local/share/manyclaude`, installs a command in `~/.local/bin`, and adds one managed PATH block to your bash/zsh/fish startup file. Fish uses `fish_add_path --path` without changing universal variables. Profile command PATH setup and regular Claude defaults occupy their own managed block; unrelated shell content is preserved. Existing startup files are backed up before editing (`.manyclaude-install.bak` for installation, `.manyclaude.bak` for alias updates). Reinstalling upgrades existing launchers and replaces isolated-profile aliases with executable commands. The installer automatically migrates legacy account folders on macOS and Linux; normal manager startup also retries unfinished migrations. Quit the affected Desktop and Code sessions first. Conflicting destination folders and redirected paths are rejected without overwriting them. The migration preserves Code settings/history, Desktop data, macOS Keychain logins, and OpenUsage session links. Regular Claude stays at its standard paths. Old account files are retained as private recovery copies in `~/.manyclaude-storage-backup/<slug>/`; reset and permanent removal clear the corresponding recovery copy too. `manyclaude migrate --check` checks prerequisites; `manyclaude migrate` retries migration. Keep the same `MANYCLAUDE_HOME` when updating a custom installation.

For a different install location:

```sh
MANYCLAUDE_BIN_DIR=/usr/local/bin ./install.sh
```

That folder must be writable by your user. `MANYCLAUDE_INSTALL_DIR` changes the support-files location; `MANYCLAUDE_HOME` changes the manager metadata root (use the same value for every command); account data always uses `~/.claude-<slug>/`.

Removal has two separate actions in the UI: **Remove profile · keep data** and **Remove profile + data permanently**. Permanent deletion requires typing the exact profile name, removes the profile’s Desktop/Code folders, logs and session history, and deletes its normal Claude Code Keychain credential. It leaves Claude’s shared Desktop encryption key intact. Quit this profile’s Desktop and Code sessions first. For scripts, `manyclaude remove <profile> --purge --yes` supplies explicit confirmation.

To uninstall, first `manyclaude remove <name>` for each profile, then delete the installed command/support-files directory and the marked PATH block from your shell startup file. Profile data is deliberately retained. Delete its folder manually only when you want to discard its login/settings. Already-running shells retain old aliases until restarted.

## Executable profile commands

Every profile has an executable launcher: `~/.local/share/manyclaude/manyclaude-code-<slug>`.
Isolated profiles also get their chosen command name in `~/.local/bin`, such as `clawde`.
`manyclaude code work`, `~/.local/share/manyclaude/manyclaude-code-work`, and `clawde`
all use the same Work profile, saved defaults, skills, and credential bridge.
The launchers preserve caller environment variables and forward arguments unchanged:

```sh
FEATURE=enabled clawde --model sonnet "prompt with spaces"
```

For T3 Code or another app's **Binary path**, use the absolute executable path
`/Users/your-name/.local/share/manyclaude/manyclaude-code-work` or
`/Users/your-name/.local/bin/clawde`. Set **CLAUDE_CONFIG_DIR** to the absolute
profile data path shown by `manyclaude list`. The terminal command `manyclaude code work`
contains arguments and cannot be entered in a field that accepts only an executable path.

Creating, renaming, refreshing, and removing profiles keeps these launchers in sync.
Existing unrelated executables are never overwritten. After upgrading, restart your shell
or run `unalias clawde` in an existing bash/zsh shell to clear its old alias.
`--alias` and `manyclaude alias` retain their names for compatibility but now choose the
isolated profile's executable name. `--no-alias` skips shell PATH setup; executables are
still created. Regular Claude's original `claude` binary is kept; regular-profile defaults
continue to use its managed shell alias. Custom installation directories apply to the
profile executables too. When running from source, stable launchers live in `<ManyClaude home>/bin`.

## Claude Code environment and flags

Profile commands inherit caller environment variables and forward all trailing arguments, including quoted values. Save per-profile defaults with:

```sh
manyclaude code-options Work --env CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 --flags='--model sonnet --no-chrome'
manyclaude code-options Work --from-alias cclaude
clawde --resume
CLAUDE_CODE_DISABLE_AUTO_MEMORY=0 clawde --model opus
```

Import reads a simple `env KEY=VALUE ... claude ...` alias from your zsh/bash startup file without executing it. It copies the variables and flags, expanding environment references such as `$HOME` at import time. Reimport after changing the source alias. Use `--rc` for a different startup file. Caller environment values take precedence over defaults. Saved flags apply to sessions, and Claude decides how repeated flags behave. When you start with a Claude Code subcommand, such as `claude plugin` or `claude auth`, ManyClaude passes the command and its arguments through unchanged with the profile environment. Credential variables and config-directory overrides remain protected to preserve profile isolation.

Use **Code defaults · environment & flags** in a profile's TUI menu to import, edit, or clear defaults. `manyclaude code-options Work` displays them; `--clear` removes them. Saving defaults for the regular profile adds a managed `claude` alias that runs ManyClaude with the normal account folders. Reload your shell with `source ~/.zshrc` to activate it. Clearing all defaults or unregistering that profile removes the alias. `command claude` bypasses the shell alias when you want to run the binary directly. Defaults live in ManyClaude's private `profiles.json`, so aliases stay short and refreshes preserve your choices.

To suppress the folder-trust prompt for a profile command, use `manyclaude code-options Main --trust-folders` (or `Work`). ManyClaude records the current folder and its Git repository root as trusted in the effective Code store before starting Code, including profiles connected to OpenUsage and legacy `.config.json` stores. For a regular profile, enabling this also creates a managed `claude` alias so plain `claude` launches use the trust behavior; reload your shell afterward. This is opt-in per profile and applies to future folders launched through that command. Use `--ask-trust` to stop automatically trusting new folders; previously recorded trust remains. It does not alter your tool-permission settings.

If automatic folder trust encounters a permission error, ManyClaude warns and still launches Claude. A regular install needs access to `~/.claude.json`; if that file is owned by root after a previous sudo launch, restore ownership with `sudo chown "$(id -un):$(id -gn)" ~/.claude.json`. Keep its private permissions and run Claude and ManyClaude as your normal user.

Use `manyclaude code-options Work --allow-external-imports` or **Auto-allow external imports** in the profile’s Code defaults menu to skip Claude Code’s external `CLAUDE.md` import prompt. This approves external instruction files for every project launched through that profile’s terminal command. Enable it for profiles you use with repositories you trust. Use `--ask-external-imports` to stop automatic approval for future launches. Claude Code keeps previously saved project approvals.

## OpenUsage

Sign in using each profile's **Claude Code command**, then run:

```sh
manyclaude openusage
```

Or press **u** in the wizard and choose **Enable / sync accounts**. Restart OpenUsage after adding/removing accounts. [OpenUsage discovers Claude-Swap accounts](https://github.com/robinebers/openusage/blob/main/docs/providers/claude.md#claude-swap-accounts) through `~/.claude-swap-backup/sequence.json`; ManyClaude merges its accounts there without replacing existing Swap entries. Cards use organization and email, and OpenUsage merges identical account/organization identities.

Isolated profiles use a **shared live credential store**: the expected Swap session folder is a symlink to the profile's existing Code folder. ManyClaude uses the profile’s normal Code folder (`~/.claude-<slug>`) for settings, sessions, skills and history. Only the credential-store selector uses the usage bridge’s session path, matching the Keychain item OpenUsage reads and renews. The caller’s working directory is preserved. Existing Keychain credentials are moved to the matching service; settings and histories remain in their original ManyClaude folders. Both apps read/write the same credential item, avoiding stale access-token snapshots or duplicate refresh-token copies. Quit the profile's Desktop and Code sessions before initially enabling this or upgrading from the old snapshot bridge. OpenUsage may request Keychain access to the new item.

The regular account keeps its default store, which OpenUsage already reads directly. Its discovery fallback still contains only an access-token snapshot (mode 600, base64 `.enc` format, not encryption). Sync runs before and after ManyClaude Code launches to discover newly logged-in accounts. Once an isolated account is linked, OpenUsage can renew it without launching `cswap run` or repeatedly syncing. If the actual login is revoked, sign in again through the profile's Code command. Desktop-only sign-ins are not exported; sign in to Code for each account you want tracked. Requires an OpenUsage version with live Claude-Swap session support. The Swap-compatible folder exists only for OpenUsage account discovery and live credentials. ManyClaude does not switch or sync conversations between accounts.

Removing/resetting a profile clears its OpenUsage entry and link. `manyclaude openusage --disable` moves isolated credentials back to their original Code Keychain service, removes ManyClaude's entries and links, and stops automatic syncing. Profile data and other Swap accounts are preserved. Quit the affected profiles before disabling, so no old client can write to the previous store.

## Global Claude Code launcher

Run `manyclaude global` or your named executable to start Claude Code with its own settings and transcript store under `<ManyClaude home>/global-code`. Each launch picks up changes from `~/.claude/settings.json`, including environment variables, and preserves the global model default. The first refresh of an older store uses regular settings for overlapping values. After that, edits made in the global store take precedence for the settings they change. If the regular settings file is absent, the starting profile supplies the settings. Open **/model** to choose an automatic model or a pinned row such as **Sonnet 5.5 (Work)**. The router uses the selected account for inference while Claude Code keeps the same process, session ID, and local transcript. Press **s** to switch for the current session only. Enter saves the route as the global launcher's default.

In the ManyClaude TUI, press **g** on the accounts screen to open **Global Claude Code**. From there you can launch Claude Code or install, rename, and remove the executable.

```sh
manyclaude global-alias                         # install claude-global
manyclaude global-alias cc-global               # or give it a different name
claude-global --list-models
claude-global --model claude-sonnet-5-5         # automatic account choice
claude-global --model claude-sonnet-5-5@work    # pin Work
claude-global --continue                       # resume the latest global conversation
```

Choose one `global-alias` command and use that executable name in the examples. Install ManyClaude first to add its bin directory to your shell's PATH. `manyclaude global-alias --disable` removes the named executable. The direct `manyclaude global` command is also available from the source tree.

Plain model IDs such as `claude-opus-5-5` route automatically using recent five-hour and weekly usage, reset times, and observed cache activity. IDs such as `claude-opus-5-5@work` pin the named profile. Global replaces the built-in model rows with these options, and Claude Code retains its **Default** control. Saved `manyclaude/` IDs remain usable in older transcripts. The loopback router reads the live sign-in from each profile and learns from response headers and cache usage. Sign in through the profile's Claude Code command again if its access token expires. Switching accounts can send the existing conversation context to the selected account. Gateway requests use Anthropic's Messages endpoint.

Global also advertises each model ID as a native Claude Code subagent type. The parent can delegate with `subagent_type: "claude-opus-5-5"` for automatic routing or `subagent_type: "claude-opus-5-5@work"` to pin Work. Omit the Agent tool's optional `model` override so the subagent uses its configured model. Set `effort` on the same Agent call to choose a supported level for that spawn: `low`, `medium`, `high`, `xhigh`, or `max`. The parent sees which levels each route supports. Automatic routes advertise levels shared by the eligible accounts for that model. These types are available in interactive Global and ACP sessions.

Routing keeps each conversation on the account that holds its prompt cache. After the first successful turn, automatic routes stay on that account while it remains under its usage limits, instead of re-ranking every turn. A resumed conversation prefers the account its session warmed last. Cache lifetimes follow the TTL the cache was written with (5 minutes or 1 hour), including turns that only read the cache. A rate-limit response still falls through to the next eligible account.

### Custom profiles

Custom profiles are additional named Global launchers. Each one uses every signed-in account and the same `/model` routes, with its own saved flags, environment variables, and command name. Press **c** on the accounts screen, or use the CLI:

```sh
manyclaude prompt-folders --add ~/Prompts          # .md files here appear in the picker
manyclaude custom set "Custom Claude" --command custom-claude \
    --flags='--system-prompt-file <MANYCLAUDE_PROMPT>' --env CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
custom-claude                                      # pick a prompt, then Claude Code starts
custom-claude --prompt ~/Prompts/review.md -p "check this diff"
custom-claude --no-prompt                          # drop the placeholder flag for this launch
manyclaude custom list
manyclaude custom remove "Custom Claude"
```

`<MANYCLAUDE_PROMPT>` can appear in any flag or environment value. When it is present, the command first opens a picker listing the `.md` files in your prompt folders (subfolders included, hidden folders skipped; type to filter, a preview appears on wide terminals). The placeholder is replaced with the chosen file's absolute path. **No prompt** removes the placeholder and the flag that takes it. Non-interactive launches need `--prompt PATH` or `--no-prompt`. Manage folders with **p** on the accounts screen or `manyclaude prompt-folders --add/--remove`.

By default a custom profile shares Global's settings and history (`/resume` lists both). `--store separate` (or **Separate store** in the profile's TUI menu, also asked when creating one) keeps its settings and transcripts in `<ManyClaude home>/custom-code/<slug>`. Saved variables behave like Code defaults: variables already exported in the calling shell take precedence. Account credentials, config directories, and `ANTHROPIC_BASE_URL` cannot be overridden. `--env` replaces the saved variables; `--clear-env` removes them.

### Conversation files

Any profile command, Global, or custom profile can run a conversation stored in a `.jsonl` file of your choice:

```sh
claude-work --convo ~/convos/refactor.jsonl          # resume it (or start it, if the file is new)
manyclaude code main --convo ./notes.jsonl
claude-global --convo ~/convos/research.jsonl --model claude-opus-5-5
```

In the TUI, choose **Open conversation file (.jsonl)** in a profile's, Global's, or a custom profile's menu. ManyClaude gives each file its own private store at `<ManyClaude home>/file-sessions/<profile>-<hash>`. That store inherits the launcher's settings, `CLAUDE.md`, skills, agents, and login. Claude resumes from a copy of your file, and everything it writes is mirrored back into your file about once per second, with a final sync on exit. It appends new lines when possible. The profile's normal history, Global's store, and their other conversations are never read or written. If the file changes outside the session, ManyClaude stops writing to it and saves the session next to it as `<name>.manyclaude-conflict.jsonl`. Conversations started inside the session with `/clear` stay in the private store. `--convo` cannot be combined with `--resume`, `--continue`, `--session-id`, or `--fork-session`.

Export an existing conversation to a file, then open it with `--convo`:

```sh
manyclaude export                            # TUI: pick a profile, Global or custom profile, then any conversation
manyclaude export main                       # this folder's conversations, newest first
manyclaude export global --all               # every project folder
manyclaude export work latest ~/convos/last.jsonl
manyclaude export custom:reviewer 3f2a9c ~/convos/   # ID or unique prefix; a folder keeps the ID as the name
```

The source is a profile name, `global`, or `custom:<name>`. Export copies the transcript and leaves the original in place. Existing output files are kept unless you pass `--force`. Running `manyclaude export` without arguments opens a picker: choose a profile, Global, or a custom profile, then pick from all of its conversations across every project, newest first. The file is saved in the folder where you ran the command, named after the conversation's title (editable). In the main TUI, **Export a conversation to .jsonl** in a profile's, Global's, or a custom profile's menu does the same, saving to the folder `manyclaude` was started from.

### Delta and ACP

On macOS, run `glaude delta` (substitute your global command name) to launch a Delta instance using ManyClaude for its **Claude Agent**. Enable Claude Agent Integration in Delta, then choose **Claude Agent** under **Other Agents** in a thread. Delta may populate available models after the first message. This uses the executable override in Delta's desktop build. Delta's settings UI currently offers no custom ACP agent entry. [Delta Claude Agent documentation](https://delta.dev/docs/agents/claude-agent)

`glaude acp` runs the upstream Claude ACP adapter over stdin and stdout with the same model routes and global transcript directory. It reuses Delta's installed adapter on macOS when available. You can also install `claude-agent-acp` on PATH, pass `--adapter /absolute/path`, or use the Node.js 22+ and npx fallback. On Linux, set `DELTA_CLAUDE_ACP_EXECUTABLE` to the absolute path of the installed `manyclaude-acp` executable when starting Delta.

New terminal and ACP conversations in this store are excluded from profile commands' `/resume` lists. Conversations created by earlier ManyClaude versions stay where they were written.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

Linux tests validate desktop entries when `desktop-file-validate` is installed (`sudo pacman -S desktop-file-utils` on Arch). Platform-specific integration tests run only on their respective OS; mise resolution tests run on either OS.

Tests use temporary homes/apps and a fake Claude executable: no real credentials or existing Claude configuration are touched.
