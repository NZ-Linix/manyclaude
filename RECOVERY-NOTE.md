# Local data incident — October 12, 2026

During implementation, a unit test mocked the home directory without isolating the already-imported manager metadata path. The test invoked the new startup migration against this device's real registry, moved the Work profile and retained, unregistered `nz` profile into a temporary home, and subsequently deleted those files during test cleanup.

The Work profile's local Code history/settings and Desktop login, cookies, settings, and caches were lost. The retained `nz` profile's local files were also lost. No local APFS snapshots or Time Machine backups were available, and those files have not been recovered. Cloud conversations are unaffected by this local file operation.

Recovery performed:

- Restored Work's account identity from existing OpenUsage registry metadata into `~/.claude-work/.claude.json`.
- Reconnected its existing OpenUsage session link to `~/.claude-work`; verified that its Code credential remains present in macOS Keychain. Its refresh token was not deleted or exported.
- Recreated Work's Desktop directory at `~/.claude-work/desktop`. Desktop requires signing in again.
- Recreated the retained `~/.claude-nz` directory and attempted to relocate any surviving legacy Code Keychain item, retaining the old item as an additional recovery option.
- Preserved the damaged registry and migration journal at `~/.manyclaude-storage-backup/incident-2026-10-12/` for reference. These are metadata copies, not recovered account files.

The regular Main account's files were not moved or reset. Work's registered profile name, app, alias, and saved Code defaults were preserved.

Preventive fixes: unit tests now isolate their manager metadata path; verification runs use an additional temporary HOME and MANYCLAUDE_HOME; migration rejects a regular profile registry belonging to a different home; migrations retain original files until the launcher is rebuilt and then archive them outside Application Support at `~/.manyclaude-storage-backup/<slug>/`. Interrupted migrations, destination conflicts, busy clients, redirected paths, Keychain conflicts, and linked OpenUsage sessions are tested. macOS and Linux installers now perform migration preflight and rebuild existing launchers.
