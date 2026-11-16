# Mac contract additions

> Design notes of the Mac port as of October 2026; the code and [docs/GUIDE.md](../GUIDE.md) are current
> where they differ.

Current as of the Mac-only build (2026-10-08). The state contract itself is executable: enums and the
validator in `accountdeck/contract.py`, the shared rules in `accountdeck/rules.py`, example payloads in
`accountdeck/fixtures.py`. All actions use the authenticated loopback `POST /api/action` envelope
`{"action": name, ...body}`; IDs and paths are strings, timestamps Unix seconds.

## Import actions (the Mac side of the PC move)

| Action | Body | Response |
| --- | --- | --- |
| `import-bundle-preview` | `{path, passphrase}` | `{preview: ImportPreview}` |
| `import-bundle` | `{path, passphrase, wait?}` | `{op, result: ImportResult \| null}` |
| `import-bundle-dismiss` | `{bundle_id}` | `{}` |

`ImportPreview` is `{bundle_id, kind, created, source_platform, accounts, counts, refused, message}`;
accounts are `{id, provider, label, plan, action, desktop, conflict, reason}` with actions
`new | update | on_pc | fill | refused` and `desktop` `now | deferred | null`.

`ImportResult` is `{bundle_id, kind, at, complete, code, counts, failures, message}`; counts are
`{new, updated, filled, kept, on_pc, deferred, failed}`. `code` (XXXX-XXXX) exists only when every account
and its history were stored; a partial import withholds it and importing the same bundle again resumes.
`wait` (default true) waits up to 30 s. Dismiss only hides an entry. Portable settings are validated and
stored in one transaction before the code is issued; `cli_manual_order` is mapped to this Mac's ids.

`state.move_import` is `{imports: [ImportResult], deferred: {count, message, reason, problem}, on_pc}`.
The `move_import` banner asks to open Claude Desktop once when a Desktop profile waits for conversion.

## Account owners

`owner` is one of `v2`, `on_pc`, `moved`, `v1`, `v2-pending` (every value an existing data folder can
hold). `on_pc` (imported while still signed in on the PC: history, no secret) and `moved` are frozen:
never read, renewed, saved over or switched to, `switchable: false`, left out of auto-switch and the menu
bar totals; only `rename` and `remove` may name them. A hand-over bundle turns an `on_pc` row into `v2`.

## Platform block

`state.platform` is always `{os: "mac", features, labels, notifications}`: features `{tray: false, menu_bar:
true, taskbar_widget: false, aumid: false, start_on_login: true}`, labels with the Mac wording ("Menu bar &
startup", "Start at login", Applications instructions, `/Users/me`). `notifications` is `{state,
settings_url}`: the permission the running menu bar item reports (`authorized`, `provisional`, `pending`,
`not_determined`, `denied`, `rejected` — macOS refused this copy's identity, reinstall — or `unavailable`
without a menu bar item) and the System Settings › Notifications URL. The `tray` setting (the menu bar
item) now defaults to on.

## Removed with the Windows build

- State: `v1`; banner kinds `v1` and `folder_warning` (`providers.claude.folder_warning` stays `null`);
  plan field `handover_v1` and the switch step `handover`.
- Actions: `import-v1-preview`, `import-v1`, `retire-v1`, `export-preview`, `export-accounts`,
  `cancel-move`, `confirm-moved`, `handover-live`, `set-aumid`; endpoint `GET /api/move-status`.
- Settings: `taskbar_widget`, `taskbar_widget_gap`, `claude_aumid`, and the internal `v1_retired_at`,
  `first_run_done`. Stored values stay in the database and are ignored; `/api/settings` refuses them.
- Error code `windows` is now `system` ("macOS refused").

## Bundle format (read-only here)

`.adeck`, magic `ADMOVE`, container version 1, payload schema 1: magic + version byte + big-endian header
length + canonical JSON header + SHA-256 prefix digest + AES-256-GCM ciphertext. scrypt (N=2^17, r=8, p=1;
at least 2^15 accepted) and HKDF-SHA256 derive the encryption key, the confirmation key and a passphrase
check. The payload carries portable settings, accounts, usage history and per-provider secrets
(`claude-desktop-portable`, `claude-code`, `codex-auth`); `live_on_pc` entries carry none.
`move_bundle.seal` documents the writer the PC used.

## Schema

The SQLite schema stays version 2 and the vault format is unchanged (`<key>.dpapi`, `.g1`, `.g2`, ADK1 +
AES-256-GCM under the `AccountDeck2 vault` Keychain key). `tests/test_data_compat.py` checks a data folder
written by the baseline build and that the baseline still opens it afterwards.
