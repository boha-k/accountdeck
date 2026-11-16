# AccountDeck 2 on macOS and the PC-to-Mac move: build plan

> Design notes of the Mac port as of October 2026; the code and [docs/GUIDE.md](../GUIDE.md) are current
> where they differ. AccountDeck 2 is now Mac-only: the Windows build, the PC export and
> AccountDeck 1 support were removed since (see CONTRACT-ADDITIONS.md). Section numbers stay as they are:
> code comments cite them.

`INVENTORY.md`, `MAC-TEST-STATUS.md` and `docs/v2/`, cited below, were working papers of the port and are
not published.

Status: COMPLETE (2026-10-07). Reference for the Mac port and the one-time PC→Mac account move.
Sources: docs/mac/INVENTORY.md (code map, `file:line`), docs/mac/PROVIDERS-MAC.md (crypto, stores,
rotation, §6 unknowns), docs/mac/PLATFORM-MAC.md (processes, menu bar, bundle, runtime, Keychain),
the code, docs/v2/. Every builder can work from this file without re-reading the research.

This plan **decides**; it does not build. One codebase, Windows behaviour and all 1,382 Python /
40 Node tests unchanged. The pinned Windows Python is
`C:\Users\<user>\AppData\Local\Python\pythoncore-3.14-64\python.exe`.

---

## 0. Key decisions (the spine of the whole plan)

- **D1 — Platform split by package, not `if os.name`.** A new `accountdeck/platform/` package holds
  the per-OS implementations behind today's seams. `base.py` (shared ABCs/helpers), `windows.py`
  (today's Win32 code moved in), `mac.py` (new), and `detect.py` picks one at runtime from
  `sys.platform`. Modules above the seams (`engine`, `switch_*`, `stop_ladder`, `accounts`,
  `cli_accounts`) keep importing the seam, never an OS module. This keeps the grep tests (process
  control only in procs, machine paths only in config/run) satisfiable with a short skip-list edit
  rather than scattering OS branches.
- **D2 — `procs.py` stays the single process-control module** but its Win32 half moves to
  `platform/procs_win.py` and the Mac half to `platform/procs_mac.py`, both imported *lazily inside
  `procs.py`* so `import accountdeck.procs` works on macOS. `procs.py` keeps the pure parts
  (`ProcessInfo`, `ProcessSet`, `ConsentToken`, `classify`, `consent_refusal`, the ownership
  registry, `ProcessController` ABC, `get_controller`). The grep tests count call sites inside
  `procs.py`; the sub-modules join procs' own skip set (see §3.4). All `os.kill`/`subprocess` for
  Mac live in `procs_mac.py`, so the "process control only in procs" rule holds by treating the two
  sub-modules as part of procs.
- **D3 — Bundle id `local.accountdeck2`** everywhere PLATFORM-MAC.md wrote a `com.*` placeholder
  (Info.plist `CFBundleIdentifier`, LaunchAgent `Label`/`AssociatedBundleIdentifiers`, TCC).
- **D4 — Vault cipher is pluggable (already a seam).** Windows keeps DPAPI. Mac uses a random
  32-byte key in the login Keychain (`security`, service `AccountDeck2 vault`, account
  `sha256(data_dir)[:16]`) + AES-256-GCM record files. Record format and `Vault` API unchanged.
- **D5 — Chromium crypto becomes a `ChromiumCrypto` seam** on the Claude provider: Windows = Local
  State `encrypted_key` + DPAPI + AES-256-GCM; Mac = Keychain `Claude Safe Storage` → PBKDF2-SHA1
  → AES-128-CBC. Vectors in PROVIDERS-MAC.md §2.
- **D6 — Claude Code credential store becomes a `CredStore` seam**: file backend (Windows/Linux,
  unchanged) and Keychain backend (Mac, service `Claude Code-credentials`). Rollback keeps the
  before-value in the encrypted transaction doc, as `oauth_change` already does for `.claude.json`.
- **D7 — The move = "move everything".** Export on the PC writes a passphrase-encrypted bundle and
  immediately marks every exported account `owner="moved"`, which freezes all read/refresh/mirror/
  keep-alive/switch paths for it. The PC deletes its vault copies only after the owner types a
  confirmation code the Mac shows post-import. "Cancel move" un-freezes before that. Accounts the
  PC's own apps are signed into (`live_on_pc`) carry no secrets in the main export and stay working
  on the PC; a **separate, explicit, never-automatic** hand-over step captures their latest
  sign-ins, removes them from the PC without any provider's Log out, and moves them in a second
  bundle (§2.5).
- **D8 — Menu bar replaces tray + taskbar widget** via one `NSStatusItem` (AppKit through ctypes)
  in the app-bundle process's main thread; the service runs on background threads. JXA helper is
  the documented fallback. The taskbar-widget concept has no Mac form; its setting is hidden on Mac.
- **D9 — `/api/state` gains a `platform` block** (`{os, labels…}`) so the frontend renames the
  "Windows" settings section to "Menu bar & startup" and hides Windows-only controls on Mac,
  Mac-only controls on Windows. Mac wording replaces Start-menu/tray text.
- **D10 — Unknowns fail closed or are flagged, never guessed silently** (§1.12). Anything that would
  need a running provider is discovered at runtime and, if absent, shown to the owner rather than
  assumed.

---

## 1. Architecture

### 1.1 The platform package and the detector

```
accountdeck/platform/
  __init__.py      # re-exports the chosen implementations via detect
  detect.py        # IS_MAC = sys.platform == "darwin"; IS_WINDOWS = os.name == "nt"
  procs_win.py     # today's _Win32, wrappers, LiveProcessController  (moved, not rewritten)
  procs_mac.py     # libproc/sysctl/kqueue, MacProcessController       (new)
  facts_win.py     # today's windows.py body                          (moved)
  facts_mac.py     # Mac equivalents of windows.py                    (new)
  keychain.py      # /usr/bin/security wrapper (Mac only; pure no-op import on Windows)
  locks.py         # OwnerLock: msvcrt (win) / fcntl.flock (posix)
  autostart_win.py # today's autostart.py Registry path
  autostart_mac.py # LaunchAgent plist + launchctl seam
  statusbar_mac.py # NSStatusItem seam (menu bar)
  notify_mac.py    # UNUserNotificationCenter / osascript seam
```

`accountdeck/windows.py` becomes a thin shim that re-exports from `platform.facts_win` on Windows
and `platform.facts_mac` on Mac, under the **same function names** (`secure_directory`,
`package_family`/`app_identity`, `claude_base`, `data_folder_files`, `claude_aumid`/`claude_launch_id`,
`codex_exe`, `file_is_free`/`files_released`, `read_menu_bar_enabled`). Callers keep importing
`from . import windows`. This is how Windows behaviour stays bit-identical: its code is moved, not
edited, and re-exported.

Reasoning: INVENTORY.md §2.2 lists exactly these seams as "needed"; a package keeps each OS's code
in one file, keeps the grep tests tractable, and lets a fake Mac backend be injected on Windows for
the simulated suite.

### 1.2 Process identity without `.exe` names or handles

`ProcessInfo` keeps its fields; two change meaning by platform (INVENTORY.md §2.2 item 2):

- **`ctime`** — Windows FILETIME stays. Mac: `pbi_start_tvsec*10_000_000 + pbi_start_tvusec*10 +
  116444736000000000` (PLATFORM-MAC.md §1), so `filetime()`, `ProcessSet` and `ConsentToken.covers`
  comparisons are unchanged. A row with **no** start time (EPERM/other user) → `ctime=0` → treated
  as unknown and **fails closed** exactly as Windows does.
- **`family`** — Windows MSIX family stays. Mac: the bundle id of the enclosing `.app`
  (`Contents/Info.plist CFBundleIdentifier` via `plistlib`), or `None` when unpackaged, `"?"` when
  the lookup failed. `classify`/`_kind` become **table-driven by platform**:
  - name match: Windows `claude.exe`/`codex.exe`; Mac `Claude`/`claude`/`codex` (no suffix).
  - Claude Desktop main: Windows family==`claude_family`; Mac family==`local.anthropic` Claude
    bundle id (confirmed at runtime from `/Applications/Claude.app`, PROVIDERS-MAC.md §1).
  - Code child path: Windows `…\Claude\claude-code\<ver>\claude.exe`; Mac
    `~/Library/Application Support/Claude/claude-code/<ver>/claude` (PROVIDERS-MAC.md §1). Add a
    second compiled regex `CODE_CHILD_PATH_POSIX` and have `is_code_child_path` try both.
  - Codex: Windows `codex.exe` cmdline branches stay; Mac `codex` with the same cmdline substrings
    (`app-server`, `daemon pid-update-loop`, `--listen unix://`); Codex Desktop process name is an
    **unknown** (PROVIDERS-MAC.md §6.6) → until confirmed, a `ChatGPT`/`Codex` GUI row with unknown
    identity classifies as `codex_desktop` (a consumer, fail-closed), never as stoppable.

`MacProcessController(ProcessController)` implements the ABC with PLATFORM-MAC.md §1 code:
`snapshot` (proc_listallpids+bsdinfo+pidpath+KERN_PROCARGS2), `creation_time`, `io_delta`
(proc_pid_rusage v2), `descendants`/`ancestors_of_self` on the POSIX table, `wait_exit`
(kqueue `NOTE_EXIT`). `main_window`/`app_user_model_id`/`start_apps_aumid` return `None` on Mac
(no equivalent; the bundle id replaces the AUMID).

### 1.3 The Mac stop ladder (keeps every interlock)

The interlock order is preserved (safe-mode ownership → consent → re-verify on the live target →
syscall). Because macOS has no handle, step 3's "verify on the same handle" becomes the
kqueue-pin sequence of PLATFORM-MAC.md §1 "Signalling a verified PID safely":

1. `kqueue EVFILT_PROC|NOTE_EXIT` register (raises ESRCH→already gone).
2. re-read start time + `proc_pidpath`; mismatch → `_refusal("ctime_mismatch"/"path_mismatch")`.
3. drain kqueue timeout 0; if already exited → return False.
4. `os.kill(pid, SIGTERM)`; SIGKILL only as the straggler step.

The **PID-reuse window** between 3 and 4 is microseconds and sequential PID allocation makes it not
practical; the safety rules **explicitly accept** this (it is the one place Mac is weaker than
Windows — INVENTORY.md §7.4). `MacProcessController` never signals a process of another user
(`os.kill` raises PermissionError) — a free safety property.

Stop-ladder tiers on Mac (stop_ladder.py logic unchanged; only the controller methods differ):
- **T1a graceful = SIGTERM to the verified Electron main** (Electron installs SIGTERM→`app.quit()`,
  so `before-quit`/`will-quit` run and flush config.json + cookie DB — PLATFORM-MAC.md §1). This
  replaces WM_CLOSE and, unlike Windows, is the default tier **regardless of the menu-bar setting**
  (there is no "close to tray" on Mac). `request_close(token, target)` on Mac sends SIGTERM under
  the full interlock. An `osascript 'quit app id'` path is available but needs Automation (TCC)
  consent and can prompt; SIGTERM is preferred and carries no prompt. `read_menu_bar_enabled`
  returns the preference if present but the Mac `_stop_tier` does not gate T1a on it.
- **T2 managed = SIGTERM main, wait `helper_self_exit`, SIGKILL stragglers** (same shape).
- **Settle**: `files_released(paths)` replaces `file_is_free`. macOS has no mandatory locks
  (INVENTORY.md §7.3), so the Mac probe is **"no Claude desktop row alive" + `lsof -t <file>`
  through procs returning empty + `settle_grace`**. `lsof` is spawned inside `procs_mac` (respects
  the grep rule). If `lsof` is unavailable or ambiguous, fall back to "no desktop row alive" + a
  longer grace; never report released on doubt.
- Markers: Claude's own quit lines in `~/Library/Logs/Claude/main.log` (same Electron strings;
  PROVIDERS-MAC.md §1 confirms the log path). Verify on a real Mac that SIGTERM writes them (owner
  check, §6).
- Wording via the `platform` labels (D9): "Quit Claude from the menu bar", "Open it from
  Applications/Launchpad".

### 1.4 Launching Claude Desktop

Replace `claude_aumid`/`launch_claude(aumid)` with a platform launch identity:
- Windows: AUMID + COM activation + explorer fallback (unchanged).
- Mac: bundle id; `launch_claude` = `open -b <id>` (background `-g` for relaunch). `launch_id` is
  read at runtime from `/Applications/Claude.app/Contents/Info.plist` (`CFBundleIdentifier`; expect
  `com.anthropic.claudefordesktop`, PROVIDERS-MAC.md §1), never hard-coded. The `claude_aumid`
  setting and `aumid` setting-kind become **Windows-only in the schema**; Mac adds nothing the owner
  must set (the id is discovered). `set-aumid` action is hidden on Mac (`platform` gate in the UI).
  The single-instance lock means relaunch must wait for exit first (PROVIDERS-MAC.md §1) — the
  existing verifier already polls for Claude to reappear.

### 1.5 Vault key in the Keychain (D4)

`platform/keychain.py` is the PLATFORM-MAC.md §8 wrapper (base64url value, `security -i` on stdin
for writes, `find-generic-password -w` for reads, rc 44 = missing, rc 36 = locked/denied). The Mac
vault cipher:

```
mac_cipher(data, decrypt=False):
    key = keychain.get_or_create(account=sha256(data_dir)[:16])   # 32 random bytes, b64url in Keychain
    AES-256-GCM(key) over the record  (nonce||ct||tag, like today's safeStorage-less records)
```

`Engine.__init__` builds `Vault(data_dir/"vault", protect=<platform cipher>)`. On Windows the
default stays `dpapi`. In **safe mode the Keychain wrapper requires a `keychain=` path inside a
registered test root** and refuses the login keychain (PLATFORM-MAC.md §8, mirrors
`autostart.Registry(sandbox=)`). `core.dpapi` stays but its import of `ctypes.wintypes` moves
*inside* `dpapi()` (or guarded) so `import core` works on darwin (INVENTORY.md §1.1 flags the
import as the only risk; verify `ctypes.wintypes` imports on the pinned Mac Python, else guard).

### 1.6 Claude Desktop profile conversion (the hardest piece — D5)

`ChromiumCrypto` seam on `Claude(paths, http, cipher=…)` becomes `Claude(paths, http, crypto=…)`
exposing `key_ref()` (what `profile["key"]` stores), `decrypt(blob)->bytes`, `encrypt(bytes)->blob`:

- **Windows** (`ChromiumCryptoWin`): `key_ref` = Local State `os_crypt.encrypted_key`;
  decrypt/encrypt = AES-256-GCM with the DPAPI-unwrapped 32-byte key (today's `_aes_key`/`_cache`/
  `cookie_header` move behind it). Behaviour identical.
- **Mac** (`ChromiumCryptoMac`): `key_ref` = a marker (e.g. `"keychain:Claude Safe Storage"`);
  `decrypt`/`encrypt` = AES-128-CBC, IV sixteen `0x20`, PKCS#7, `v10` prefix, key =
  PBKDF2-HMAC-SHA1(Keychain password, `saltysalt`, 1003, 16). The Keychain password is read via the
  existing `security` wrapper from item **`Claude Safe Storage` / account `Claude`** (PROVIDERS-MAC
  §2; **unknown exact names → §6.1, discovered at runtime, failing closed with a clear message if
  absent**).

`claude.py` `_cache`, `cookie_header`, and `restore`'s key check route through `crypto`. `restore`
refuses unless `crypto.key_ref() == profile["key"]` — on Mac that means the converted profile
carries the Mac key marker, so an **unconverted** (Windows-keyed) profile fails loudly rather than
corrupting (INVENTORY.md §7.2). Cookie rows are rewritten per PROVIDERS-MAC.md §2 "rewrite the
rows" recipe, preserving the 32-byte `sha256(host_key)` prefix iff the **target** cookie DB
`meta.version >= 24` (Mac DB meta version is an **unknown → §6.4**; the import reads it from a
Mac-created `Network/Cookies`, failing closed if it cannot).

**Where conversion runs: on the Mac, after Claude Desktop has been installed and opened once**, so
`Claude Safe Storage` exists and a real Mac cookie DB gives the target schema. Until then the import
stores the profile in a **deferred, un-converted** state (§2.3).

### 1.7 Claude Code credential store (D6)

`CredStore` seam used by `ClaudeCLI`, `switch_cli.CLITransaction`, enrollment:
- `read()`/`write(value)`/`delete()` + `before_value()` for rollback.
- **File backend** (Windows/Linux): today's `.credentials.json` reads/writes under the existing
  `cli_locks` mkdir locks. Unchanged.
- **Keychain backend** (Mac): read/write the generic password **service `Claude Code-credentials`,
  account = login user**, value = `{"claudeAiOauth":…}` JSON string (PROVIDERS-MAC.md §3;
  service string is an **unknown → §6.2**, confirmed at runtime). Writes go **under the keychain
  write lock semantics**: write the Keychain item (authoritative), and also keep `.credentials.json`
  as the documented fallback. Running sessions re-read the Keychain within ~30 s (PROVIDERS-MAC.md
  §3), matching the Windows "re-reads on change" assumption.
- Rollback: `CLITransaction` already stores `oauth_change` before-values for `.claude.json`; add the
  same for the Keychain credential (store `before` JSON in the encrypted tx doc, restore on
  rollback). The byte-level `FileTransaction` cannot cover a Keychain item, so the Keychain write is
  an explicit tx step with its own before/after, exactly like `write_account`.
- Enrollment: a custom `CLAUDE_CONFIG_DIR` scopes the Keychain item by an **unknown** suffix
  (§6.3). AccountDeck already refuses a custom `CLAUDE_CONFIG_DIR` (`readonly_reason`,
  terminal-mode.md), so the default-dir enrollment is what matters; the enrollment terminal is
  opened with `open -a Terminal`/`osascript` via `procs_mac` and the enrolled login read from its
  folder's `.credentials.json` (the enrollment subprocess writes the file in its scoped dir).
- `cli_accounts.gate` uses `OwnerLock` → the `platform/locks.py` `fcntl.flock` version on Mac
  (INVENTORY.md §1.7). `cli_locks.proper_lockfile` is mkdir-based and portable; keep it.

### 1.8 Codex

- Default store on macOS is **`file`** (`~/.codex/auth.json`), no OS override (PROVIDERS-MAC.md §4).
  `validate_store` is already correct: it refuses non-file modes. On Mac `codex_exe` resolves
  `codex` on PATH (no `.exe` requirement) or the research path; `facts_mac.codex_exe` drops the
  `.endswith(".exe")` check.
- `switch_codex` blockers key on `r.family == "?"`; on Mac "unknown identity" is the same `"?"`
  marker, so the portable check stands. Daemon stop/start, enrollment temp `CODEX_HOME`, and
  `remove_work_directory` are portable once procs imports.
- Codex Desktop process name is an **unknown (§6.6)**; a GUI Codex/ChatGPT row with unknown
  identity stays a non-stoppable consumer (fail closed). If keyring mode is ever set, the port
  writes the `"Codex Auth"` item (`cli|<16hex of sha256(CODEX_HOME)>`, PROVIDERS-MAC.md §4) via the
  keychain wrapper, or simpler, writes `auth.json` after setting file mode; keep this behind
  `validate_store` so the default path is unaffected.

### 1.9 Paths, locks, atomic write, autostart, notifications, run.py

- **Paths.live** gains a `sys.platform=="darwin"` branch (config.py is already the only env reader):
  data `~/Library/Application Support/AccountDeck2` (0700), logs `~/Library/Logs/AccountDeck2`,
  profile `~/Library/Application Support/Claude`, main log `~/Library/Logs/Claude/main.log`,
  sessions/projects `~/.claude/...`, codex `~/.codex`, no v1 (`v1_port=0`, `v1_data` absent),
  owner lock `~/Library/Application Support/AccountDeck2-Owner.lock`. Home from
  `pwd.getpwuid(os.getuid()).pw_dir` (not `$HOME`, which launchd sets oddly — PLATFORM-MAC.md §7).
  `claude_profile_alias=None` (no MSIX split). `claude_base` returns `(profile, kind, "single")`.
  `codex_exe` from PATH. `Paths.synthetic` is unchanged on both OSes (it is OS-neutral already).
- **`cwd` setting** validates with the platform path class (`PureWindowsPath` on Windows,
  `PurePosixPath` on Mac); error text and the `C:\Users\you` example come from the `platform`
  labels.
- **OwnerLock** → `platform/locks.py`: `fcntl.flock(LOCK_EX|LOCK_NB)` on POSIX, kept open for the
  process (PLATFORM-MAC.md §7, "do not use lockf"). `replace_with_retry`/`atomic_write` stay; add
  `F_FULLFSYNC` + directory fsync on darwin for crash-safe vault writes (PLATFORM-MAC.md §7).
- **autostart**: `platform/autostart_mac.py` writes `~/Library/LaunchAgents/local.accountdeck2.plist`
  and manages it with `launchctl bootstrap/bootout/enable/print-disabled` (PLATFORM-MAC.md §4),
  behind a fakeable seam like `Registry(sandbox=)`. `Status(registered, command, approved)` maps as
  §4 describes (`approved=False` when disabled in Login Items or bootstrap fails). Tests never
  bootstrap a real agent (AGENTS); the seam is faked. The LaunchAgent runs the **bundle launcher**,
  not bare Python, so the process has the bundle identity.
- **notify**: pure policy stays. Mac `balloon` sink → `UNUserNotificationCenter` from the bundled
  process (needs bundle identity), fallback `osascript display notification` (attributes to Script
  Editor, exit 0 even on failure — never trust its exit code). PLATFORM-MAC.md §3. The `page` sink
  (browser notifications) already works and is the always-available path.
- **run.py**: `--serve` already portable (`CREATE_NO_WINDOW` via `getattr`). `--tray` becomes
  `--menubar` meaning "attach the status item"; keep `--tray` as a hidden alias so install scripts
  and the LaunchAgent command are stable. `serve()` starts the menu-bar status item on Mac instead
  of tray+widget. `secure_directory` already has the `chmod 0700` POSIX branch. `webbrowser.open`
  stays (Python 3.14 pipes the URL over stdin, so the `#token=` secret never hits argv —
  PLATFORM-MAC.md §7; **do not** `open <url>`).

### 1.10 Menu bar (D8)

One `NSStatusItem` via ctypes→libobjc/AppKit on the app process's **main thread**; the HTTP server,
engine and scheduler run on background threads that hand UI work over with
`performSelectorOnMainThread:` (PLATFORM-MAC.md §2). A `MacStatusBar` seam
(`set_title`,`set_tooltip`,`set_menu`,`set_image`,`alert`) lets tests use a fake, as `Win32` is
faked today. **Reuse unchanged**: `tray.menu_model`, `tooltip`, `switch_prompt`, `_status_lines`,
`pool.summarize`, `ring_pixels` (RGBA→PNG via stdlib zlib/struct → `NSImage`). Title carries the
pool totals (the taskbar widget's old job); menu is the tray menu; first item "Open dashboard".
Consent dialogs = `NSAlert`. Keep a Python reference to every `CFUNCTYPE`/block for process life
(a collected callback crashes on the next click). This is a real-UI owner check (§6); if ctypes
AppKit proves unstable, the JXA helper is the documented fallback.

### 1.11 Frontend and API (D9)

- `/api/state` gains `"platform": {"os": "windows"|"mac", "menubar": bool, "autostart_label": …,
  "quit_hint": …, "reopen_hint": …, "cwd_example": …, "features": {tray, taskbar_widget,
  menu_bar}}`. `contract.validate_state` learns the block; fixtures gain a Mac variant.
- Settings: the "Windows" section becomes **"Menu bar & startup"**; on Mac it shows the menu-bar
  toggle + "Start at login" (LaunchAgent) and hides tray/taskbar-widget/gap/set-aumid; on Windows it
  is unchanged. Banner/stop wording comes from the `platform` labels, not hard-coded "Start menu".
- Error code `"windows"` is **kept** (frontend may branch on it) but its **message** is
  platform-specific; add no new code. `cwd` example and consumer-parent display strings
  (`WindowsTerminal.exe` → `Terminal`) come from the platform block.
- The **move** adds actions (§2): `export-preview`, `export-accounts`, `cancel-move`,
  `confirm-moved`, `import-bundle`, `import-bundle-apply`, `handover-live`. Register each in
  `contract.ACTIONS`, `engine._dispatch`/`_do_*`, and the HANDOFF contract.

### 1.12 How each PROVIDERS-MAC.md §6 unknown is handled

| # | Unknown | Handling |
|---|---|---|
| 1 | `Claude Safe Storage` item names | **Runtime discovery, fail closed.** Try `Claude Safe Storage`/`Claude`; if read fails, surface "Open Claude Desktop on this Mac and allow Keychain access", defer conversion. Never guess a wrong name silently. |
| 2 | `Claude Code-credentials` service string | **Runtime discovery.** Read the item; if absent, the file fallback (`.credentials.json`) is authoritative and a banner notes Keychain unavailable. |
| 3 | `CLAUDE_CONFIG_DIR` keychain suffix | **Flagged + failing closed.** Custom dir already makes terminal mode read-only (existing `readonly_reason`); default-dir path is the only supported one. |
| 4 | Mac cookie DB `meta.version` | **Runtime discovery.** Read from a Mac-created `Network/Cookies` at import; decide the 32-byte prefix from it; fail closed if unreadable. |
| 5 | Whether Local/Session Storage & IndexedDB must travel | **Owner check (§6) + conservative default.** Ship the minimal set (config.json auth keys + cookies + token cache); if the owner's live check shows Desktop needs the LevelDB stores, add them to `ProfileScope.extra_files` (the seam already exists). |
| 6 | Codex Desktop process name | **Fail closed.** Unknown GUI Codex row = non-stoppable consumer. |
| 7 | Whether Desktop accepts hand-written token cache without a fresh `/authorize` | **Owner check.** Same open question as Windows; the move relies on the durable `sessionKey` cookie re-minting the cache. If Desktop requires a fresh authorize, the cookie jar still carries the session, so sign-in is not needed. |
| 8 | Login-keychain availability (GUI vs headless) | **Fail closed to file fallback.** Keychain writes need an unlocked login keychain; headless uses the fallback files; the installer/menu-bar run in the GUI session. |

---

## 2. The transfer, end to end

The precedent is v1→v2 import (INVENTORY.md §4.1): staged preview→import→finish, `v2-pending`
owner, per-account skip reasons, newest-wins merge, history copy. What does **not** carry: DPAPI
reuse (cross-machine) and HTTP stop of the other app (the PC must stop *itself*).

### 2.1 Export on the PC

**Precondition:** `engine.recovery` must be empty (no pending transactions) — refuse export
otherwise and tell the owner to finish/rollback first (INVENTORY.md §3.1 transactions are not
transferable). v1 must be retired or absent.

**What goes in the bundle** (one passphrase-encrypted file, `accountdeck-move-<date>.adeck`):
- For every account row: the **portable** SQLite columns (id, provider, identity, label, email,
  plan, org, created, `last_good_usage`, readings, refills) with `owner` reset to `v2` and
  `imported_from="pc"`; **dropped**: `paused_until`, `refresh_attempt`, `last_active_at`, `error*`,
  `next_refresh`; `generations` metadata reset (digests won't match after re-encryption).
- For every vault record, the **decrypted** JSON (DPAPI-unwrapped on the PC), re-wrapped only in the
  bundle's transport encryption:
  - **Claude Desktop**: decrypt `oauth:tokenCache`/`oauth:tokenCacheV2` **and every cookie
    `encrypted_value`** with the Windows AES-256-GCM key → store **plaintext** token-cache JSON and
    per-cookie plaintext values (+ host_key, name, metadata needed to rebuild rows). This makes the
    profile **portable**; the Mac re-encrypts (§2.3). The Windows `key` field is **not** carried
    (it is DPAPI-bound and useless on Mac).
  - **Claude Code**: the record already has plaintext tokens inside — copy as-is.
  - **Codex**: `auth.json` copy as-is (plain JWTs).
- **Settings**: only the portable keys (INVENTORY.md §3.3 "Portable" list). Never: tray/widget/gap/
  start_on_login/claude_aumid/cwd, `v1_*`, `first_run_done`, internal machine-history keys.

**What never goes in** (INVENTORY.md §3.5 item 6): the Windows `key`/DPAPI material, transactions,
quarantine, `cli-stash`, `cli-enrollment`, `runtime.json`, PIDs/ctimes, device keys
(`ant-device-registry.json`, `Claude Code-device-keys` — per-machine, the Mac registers its own),
`cf_clearance` cookie (UA/IP-bound; Cloudflare regenerates it), Windows-only settings.

**Secrets never logged.** The export path writes only to the bundle; diagnostics log counts and
account refs, never token/cookie bytes (AGENTS). The bundle is encrypted with
**scrypt(passphrase) → AES-256-GCM** (N=2^15 or higher, random salt+nonce, versioned header); the
passphrase is owner-chosen and shown/entered only in the dashboard, never persisted. File written
0600 in a folder the owner picks (not the data dir).

**Mirror `adopt_stashes` first**: before freezing, fold any in-flight `cli-stash` into the vault so
no rotation is lost (INVENTORY.md §3.1).

### 2.2 The "moved" account state on the PC (freeze)

Add `"moved"` to `OWNERS` in `core.py:323` (`save_account`/`update` validation) and
`contract.py:37` + `validate_state` enum (`contract.py:1461`). The **moment export succeeds**, every
exported account row is set `owner="moved"`. Guards (INVENTORY.md §4.4):

1. `Engine.targets` skips `owner=="moved"` rows → the scheduler never reads/refreshes them.
2. Defence in depth: `AccountService.read` returns `"skipped"` and `CLIAccounts.read`/`_refresh`
   raise `read_only` for moved rows; Codex refresh already gated by `refresh_due`/owner.
3. Switch planning: `switchable=False` for moved targets (engine.py:790) + blockers in
   `switch_claude.build_plan`, `switch_codex.build_plan`, `switch_cli.plan`; `cli_policy`/`pool`
   treat moved rows as excluded.
4. `CLIAccounts.mirror`/`save` and `AccountService.save` must **not silently re-own** a moved
   identity: if the PC's live app is still signed in to a moved account, raise a **banner**
   (the official client keeps rotating that token — the hard constraint, §2.5).
5. Keep the vault copies until the Mac confirms; the row is a tombstone with history.
6. UI: a **"Moved to Mac"** chip + card state, Remove only, no refresh/switch actions.

**Cancel move** (`cancel-move` action, before confirmation): restores `owner` to its pre-export
value (`v2`) for every frozen account, re-enabling all paths. Used when the Mac import fails. Only
valid while the PC still holds the vault copies (i.e. before `confirm-moved`).

### 2.3 Import on the Mac

`import-bundle` (preview, like `import-v1-preview`): decrypt the bundle with the passphrase, show
per-account rows + skip reasons, no writes. `import-bundle-apply` (like `ImportV1`, one step per
account):
- SQLite rows inserted with `owner="v2"`, `imported_from="pc"`; history/refills copied; generations
  reset.
- **Claude Code** records → written to the Mac Keychain (`Claude Code-credentials`) at switch time,
  or stored in the vault now and materialised on first switch (same as today: the vault holds the
  record; the Keychain write happens when that account becomes active). Store in vault immediately.
- **Codex** records → vault now; `~/.codex/auth.json` written at switch time (unchanged flow).
- **Claude Desktop** profiles need re-encryption with the Mac's `Claude Safe Storage` key
  (§1.6). Two cases:
  - **Claude Desktop installed + opened once on the Mac** (so `Claude Safe Storage` exists and a
    real cookie DB gives the target `meta.version`): convert now — re-encrypt token cache + rewrite
    cookie rows into a Mac-schema `Network/Cookies`, set `profile["key"]` to the Mac key marker,
    store in vault as a ready profile.
  - **Not installed/opened yet**: store the profile in a **deferred** sub-state (plaintext values
    kept in the vault record under a `deferred_desktop` key, encrypted by the vault cipher). The
    dashboard shows "Open Claude Desktop once on this Mac to finish importing N accounts". A tick
    phase (`_convert_deferred`) retries conversion each time Desktop is detected installed; on
    success it rewrites the record to a normal profile and clears the deferred flag.
- **Keychain prompts**: reading `Claude Safe Storage` (an item Claude.app created) may show the
  macOS access dialog; the UI explains it and prefers plain *Allow* over *Always Allow* (don't widen
  the ACL — PLATFORM-MAC.md §8). A denied/locked read (`security` rc 36) → defer, don't fail the
  whole import.

### 2.4 Confirming the move back to the PC

After `import-bundle-apply` has stored **every** account in the bundle (a deferred Claude Desktop
profile counts as stored: its data is safe in the Mac vault), the Mac shows a **short confirmation
code** (8 base32 chars, grouped 4-4) = `HMAC-SHA256(confirm_key, "accountdeck-move-confirm" ||
bundle_id || sorted(account ids))` truncated, where `confirm_key` is derived from the bundle's
**passphrase-derived key** (HKDF, separate label), never from the plaintext header alone — so the
code cannot be computed from the file without the passphrase. If any account failed to store, the
Mac shows **no code**; it lists the failures and tells the owner to use Cancel move on the PC and
export again. The owner types the code on the PC (`confirm-moved` action). The PC keeps what it needs
to recompute the code (bundle id + account ids + the HKDF output, in its encrypted vault, never the
passphrase); on match it **deletes the vault copies** (`Vault.remove`) for every account in that
bundle, keeping the tombstone rows + history. On mismatch it refuses and keeps everything, so a typo
never strands credentials. There is no automatic deletion — only this explicit code entry triggers
it. (No network between PC and Mac is assumed; the code is the owner's courier.) The hand-over
bundle (§2.5) has its own code and confirmation, the same way.

### 2.5 The PC's own signed-in apps (the hard constraint)

Hard constraint (INVENTORY.md §4.4): an account **signed in on the PC** (Desktop active, terminal
active, or Codex active identity) is rotated by the official client there. If the Mac also holds it,
the two race on one single-use refresh-token family and one side gets signed out.

A snapshot taken at export time is **stale for a live account** as soon as the PC's client renews
it, and replaying a stale single-use refresh token on the Mac can make the provider revoke the whole
login on both machines. So live accounts move in a **second, explicit stage**, at the moment the PC
lets go of them:

1. **Main export (§2.1)** marks every account that is live on the PC at export time (Claude Desktop
   active profile, terminal active login, Codex active identity) as `live_on_pc`. For those it
   carries **metadata and history only, no tokens, cookies or token caches**, and does **not** freeze
   them; the PC keeps using them normally. On the Mac they import as **"Still signed in on the PC"**:
   listed with their history, not switchable, no secrets.
2. **Hand-over (`handover-live`, explicit, owner-initiated, needs `confirm:true`, under the existing
   consent + stop ladder)**, offered on the PC once the main move is confirmed:
   - Claude Desktop: quit it gracefully (stop ladder), capture the profile (as a switch-away save
     does), then `Claude.clear_for_login()` (removes `oauth:tokenCacheV2`/`tokenCache`/
     `lastKnownAccountUuid` from config.json and the claude.ai cookies) — **not** Desktop's "Log
     out", which revokes the server session (PROVIDERS-MAC.md §5).
   - Claude Code: take the terminal login under Claude Code's own locks (as a switch-away save does),
     then remove the PC's local credential (`.credentials.json` on Windows). Not `/logout`. Running
     terminals lose their login on their next message; the confirmation names that.
   - Codex: wait for or end consumers as today's Codex switch does, capture `auth.json`, then delete
     it. Not `account/logout`.
   - Then freeze those accounts (`owner="moved"`) and write a **hand-over bundle** (same format and
     passphrase rules as §2.1, own bundle id) holding exactly these freshly captured secrets. Each
     capture-then-remove is transaction-backed: if anything fails before the bundle is fsynced, the
     PC's sign-in is restored and nothing is frozen.
   - The Mac imports the hand-over bundle onto the waiting rows, which become normal accounts, and
     shows a second confirmation code; the PC then deletes those vault copies (§2.4).

**The hand-over must never run automatically**, because the owner's live sessions (including agent
sessions) use these clients. The UI states plainly: *these accounts are still in use on this PC.
Handing them over signs this PC's Claude Desktop, Claude Code and Codex out of them (open terminals
stop working on their next message). If you skip it, they simply stay on the PC and keep working
here; the Mac shows them as still on the PC and never uses them, so nothing gets signed out.*
`handover-live` refuses any account that is not `live_on_pc` from the confirmed main move.

### 2.6 Recovery at every step

- **Export interrupted**: nothing is frozen until the bundle is fully written and fsynced; a partial
  bundle is discarded; accounts stay `v2`. Freeze is a single DB transaction after the bundle
  exists.
- **Freeze done, Mac import fails**: `cancel-move` restores `owner=v2`.
- **Import interrupted on Mac**: each account applied in its own step (like `ImportV1`); re-running
  `import-bundle-apply` is idempotent (identity-keyed upsert, newest-wins merge). Deferred-desktop
  conversion retries on its own.
- **Confirmation mismatch**: PC keeps everything; owner re-exports or re-reads the code.
- **`handover-live` interrupted**: each capture-then-remove is transaction-backed (the stop ladder
  + a FileTransaction over config.json/credentials); until the hand-over bundle is fsynced, rollback
  restores the PC sign-in and nothing is frozen; it never runs on an account that is not
  `live_on_pc` in the confirmed main move.
- All mutations keep encrypted recovery state; rollback preserves later app changes (AGENTS).

---

## 3. Tests

### 3.1 Windows-run tests with simulated macOS backends

The whole point: run on the Windows CI box with fakes so Mac logic is covered without a Mac.
- **Fake Keychain** (`tests/fakes/fake_keychain.py`): an in-memory dict behind the `platform/
  keychain.py` wrapper interface (and a variant that drives the real `security` arg-parsing against
  a scripted fake `security` for the arg contract). Used by the Mac vault cipher, `ChromiumCryptoMac`
  key read, and `CredStore` Keychain backend tests.
- **Fake libproc/kqueue** (`tests/fakes/fake_mac_procs.py`): feeds `MacProcessController` a scripted
  POSIX process table (pid/ppid/start-µs/path/argv/bundle-id) and scripted `NOTE_EXIT` events, so
  every stop-ladder/activity/switch flow runs on the Mac controller the way `FakeProcessController`
  runs today. Also a `MacFakeProcessController` (the demo/synthetic seam, Mac-shaped rows: no `.exe`,
  POSIX Code-child paths, bundle ids) selected by `get_controller` when a `platform="mac"` hint is
  set in safe mode.
- **Crypto known-answer tests** (`tests/test_chromium_crypto.py`): the exact vectors in
  PROVIDERS-MAC.md §2 — Mac AES-128-CBC round trip (key `d9a09d49…`, cookie + tokenCacheV2 hex),
  Windows AES-256-GCM round trip, the 32-byte host-hash prefix rule, `v10` framing both directions.
- **Transfer round trips** (`tests/test_move.py`): synthetic PC vault + DB → export bundle →
  scrypt/AES-GCM round trip → import on a synthetic Mac home (fake Keychain) → assert identities,
  usage history, Codex/Code records survive; assert freeze guards (moved rows are skipped by
  `targets`, read, switch planning); assert `cancel-move` restores; assert `confirm-moved` code
  match/mismatch and no code on a partial import; assert `live_on_pc` accounts carry no secrets in
  the main bundle; assert `handover-live` refuses other accounts, rolls back on failure and its
  bundle completes the waiting Mac rows.
- **Fake Mac facts**: `facts_mac` functions (`claude_base` single-folder, `files_released`,
  `codex_exe` PATH) tested with synthetic homes.
- **Platform split import test**: `import accountdeck.procs`, `engine`, `switch_*`, `stop_ladder`,
  `providers`, `tray`→menu-bar on a simulated darwin (monkeypatch `sys.platform`) must succeed —
  this is the pilot's done criterion.

### 3.2 Real-macOS run on the test Mac

The suite also ran on a real Mac, in safe mode inside a temporary folder with a temporary keychain, to
cover what fakes cannot: the pinned runtime's self-test, the Keychain wrapper, process listing and locks.

### 3.3 One-command remote runner

During the port a script, not kept in the repository, copied the checkout to such a temporary folder on
the test Mac and ran the suites there.

### 3.4 Node and grep/architecture tests

- Node: a Mac fixture (`tests/fixtures/state-mac.json`) + settings DOM test that the "Menu bar &
  startup" section renders Mac controls and hides Windows-only ones; `model.test.cjs` learns the
  `platform` block. Keep all 40 existing Windows Node assertions.
- Grep tests: add `accountdeck/platform/procs_win.py` and `procs_mac.py` to the "process control
  only in procs" skip set (they are procs); keep `windows.py` clean (it re-exports, no process
  control); machine-path reads stay in config.py/run.py (the `pwd`/Library logic lives in config).
  The per-signal single-call-site counts in `test_procs.py` become per-file (one `os.kill` site in
  `procs_mac`, one `TerminateProcess` in `procs_win`). Update these counting tests to sum across the
  two sub-modules rather than loosen them.

### 3.5 Owner-only live checklist

Extend VALIDATION.md with a **macOS live acceptance** section and a **PC export** section (owner
only, agents must not run them): the PLATFORM-MAC.md §"Owner checks" list (menu bar visible/clickable
light+dark+notch; UN permission prompt + banner for the ad-hoc bundle; `launchctl bootstrap` +
Login Items name; Finder/Spotlight open + reopen; Claude quit via SIGTERM writes its log markers and
Automation-prompt behaviour; Keychain access dialog text + partition-list prompt; cffi AESGCM
self-test), **plus** the move: export on the PC with two non-primary accounts, import on the Mac,
confirm the code, verify the PC freezes and the Mac reads usage, then the explicit
`handover-live` for one live account and what happens if skipped.

---

## 4. Work breakdown

The port was split into tasks that each own their own files: a pilot first, then two waves.

### Pilot (run first, alone, prove the approach)

- **P0 — Platform seam + import safety + first test-Mac run.** (The pilot. The Mac process
  controller itself is A4a.)
  - Goal: the whole suite **imports and runs** on macOS (Mac-specific behaviour may still fail
    closed with a clear "not yet supported on macOS" error), the platform package exists, the
    detector works, and every later task has its seam to code against.
  - Owns: `accountdeck/platform/__init__.py`, `detect.py`, `procs_win.py` (move Win32 out of
    procs.py, byte-equivalent), `procs_mac.py` (**stub**: a `MacProcessController` whose methods
    fail closed, so `get_controller` works on darwin), `facts_win.py` (today's windows.py body,
    moved) + `facts_mac.py` (**stub** with the §1.1 names), `locks.py` (msvcrt/fcntl.flock),
    `keychain.py` (**interface only**: `get`/`set`/`delete` generic password, the safe-mode
    keychain-path guard, and a `KeychainError` with the rc mapping; real `security` calls are A1)
    + `tests/fakes/fake_keychain.py`; edits to `procs.py` (lazy imports, keep pure parts + ABC +
    `get_controller`), `windows.py` (shim), `core.py` (`wintypes` import guard, OwnerLock → locks
    seam), `config.py` (`Paths.live` darwin branch, cwd path-class), `tools/mac_remote_test.sh`; the
    grep skip-list edits in `tests/test_config.py`/`test_procs.py`.
  - `tools/mac_remote_test.sh` (run from Git Bash on the PC): rsync/scp the checkout without
    node_modules to a fresh `mktemp -d /tmp/accountdeck-XXXXXX` on the test Mac, download the pinned
    CPython 3.14.8 + cryptography 50.0.1 + cffi 2.1.1 into it with checksum checks, install the
    wheels offline, run the AESGCM self-test, run the full Python suite with
    `ACCOUNTDECK_SAFE_MODE=1` and `TMPDIR` inside the folder (optionally a module list), run the
    demo smoke, copy the results back, and delete the folder (also on failure).
  - Done: the full Windows suite still passes (1,382 OK, 2 skipped; 40 Node); on the test Mac the whole
    suite imports and runs, with every remaining failure listed by test and owning task in
    `docs/mac/MAC-TEST-STATUS.md`; the demo starts there.
  - Fixes what later tasks trip on before they launch.

### Wave A (parallel after P0)

- **A1 — Vault key in Keychain + Mac vault cipher.** Owns `platform/keychain.py` (the real
  `security` calls behind P0's interface), `tests/fakes/fake_keychain.py`, `tests/test_keychain.py`; edits `engine.py` vault construction,
  `core.py` cipher wiring. Depends on P0 locks/detect. Done: fake-Keychain cipher round trip +
  the test Mac temp-keychain test.
- **A2 — ChromiumCrypto seam + Claude Desktop Mac crypto.** Risky: lockout. Owns the new
  `ChromiumCrypto{Win,Mac}` (in `providers/`), edits `providers/claude.py` (`_cache`,`cookie_header`,
  `restore` key check), `providers/fake.py` (Mac-format builder). `tests/test_chromium_crypto.py`
  with PROVIDERS-MAC.md §2 vectors. Depends on A1 (Keychain read). Done: KAT vectors both OSes;
  Windows provider tests unchanged.
- **A3 — Claude Code CredStore seam + Keychain backend.** Owns the `CredStore` abstraction,
  edits `providers/claude_cli.py`, `switch_cli.py` (Keychain rollback step), `cli_accounts.py`
  (enrollment terminal via procs_mac, gate via locks). `tests/test_cli_switch.py` Mac variant.
  Depends on P0 + A1. Done: switch round trip on fake Keychain; file backend unchanged.
- **A4a — Mac process controller.** Risky: process safety. Owns `platform/procs_mac.py` (replace
  the P0 stub: libproc/sysctl snapshot, start-time identity, bundle-id family, descendants, kqueue
  pin, SIGTERM/SIGKILL under the full interlock, `wait_exit`, `io_delta`, `lsof` probe, `open -b`
  launch) + `tests/test_procs_mac.py` + `tests/fakes/fake_mac_procs.py`. Depends on P0. Done: unit
  tests on Windows with fakes; on the test Mac, real enumeration and a stop-ladder run against a child
  process the test spawned itself; Windows procs tests unchanged.
- **A4b — Mac facts + stop ladder tiers + Claude launch/quit.** Owns `platform/facts_mac.py`
  (replace the P0 stub: `files_released`, `claude_base`, `read_menu_bar_enabled`, `codex_exe`,
  launch id), edits `stop_ladder.py` (Mac tier gating, §1.3), launch wiring in `switch_claude.py`.
  Codes against the `ProcessController` ABC with the fake controller, so it runs in parallel with
  A4a. Depends on P0. Done: stop-ladder tests on the Mac fake controller; Windows stop_ladder tests
  unchanged.
- **A5 — Codex on Mac.** Owns edits to `providers/codex.py` (`codex_exe` no-suffix,
  Codex-Desktop unknown classification), `switch_codex.py`. Depends on P0. Done: `test_codex_tokens`
  Mac variant; file-store path unchanged.

### Wave B (after Wave A)

- **B1 — The export bundle + freeze state.** Risky: money and lockout. Owns a new `move.py`
  (export, bundle crypto scrypt+AES-GCM, freeze), `"moved"` added to `core.OWNERS`/`contract`,
  `Engine.targets`/`AccountService.read`/`CLIAccounts` moved-row guards, new actions
  `export-preview`/`export-accounts`/`cancel-move`/`confirm-moved`. `tests/test_move.py` (export +
  freeze + cancel + confirm). Depends on A2/A3/A5 (decrypt-then-portable needs the Windows crypto
  seam). Done: round-trip + freeze guard tests.
- **B2 — Import on the Mac + deferred Desktop conversion + live hand-over.** Owns the
  import half (`move_import.py`), `_convert_deferred` tick phase, `import-bundle*` and
  `handover-live` actions (PC side capture-remove-freeze-bundle, Mac side completion), the
  confirmation code display. Depends on B1 + A2. Done: import round trip incl. deferred path and a
  partial import (no code); `handover-live` refuses non-`live_on_pc` accounts, rolls back on
  failure, and its bundle completes the waiting rows.
- **B3 — Menu bar status item.** Owns `platform/statusbar_mac.py`, `platform/notify_mac.py`,
  edits `run.py` (`--menubar`, Mac serve path), `engine.update_settings` (menu-bar start/stop).
  Reuses `tray.py` model functions unchanged. Depends on P0. Done: `MacStatusBar` fake tests; the
  real UI is an owner check.
- **B4 — autostart LaunchAgent + install.sh + app bundle + icon.** Owns
  `platform/autostart_mac.py`, `install.sh`, `make_icon.py --icns`, bundle builder. Depends on P0.
  Done: plist lint + fake-launchctl seam tests; the test Mac builds the bundle and the pinned runtime
  self-test passes (PLATFORM-MAC.md §5/§6). Pins incl. cffi 2.1.1 (§6).
- **B5 — Frontend platform block + Mac wording + move UI.** Owns `contract.py` (`platform`
  block + validate + Mac fixtures), `web/js/settings.js`, `web/js/banners.js`, `web/js/format.js`,
  `web/js/import.js` (reuse for move), new move modal; DOM tests. Depends on B1/B2 for action shapes,
  B3 for feature flags. Done: Node suite + a Mac settings DOM test; Windows DOM tests unchanged.

### Dependency order / parallelism

P0 → {A1,A2,A3,A4a,A4b,A5 parallel; A2/A3 code against the P0 keychain interface} → {B1,B3,B4 parallel; B2 after B1; B5 after B1/B2/B3}.
Two tasks that edit `engine.py` in the same wave (B1 actions and guards, B3 `update_settings`) each touch
only their own functions.

### Deep-review task list (what the reviewer must check hardest)

1. **Refresh-token races** (INVENTORY.md §7.1): no PC path (keep-alive, Codex refresh, switch-time
   refresh, live client) can touch a `moved` account; the freeze is airtight across every path in
   INVENTORY.md §4.2; `live_on_pc` accounts carry no secrets in the main bundle; `handover-live`
   never runs automatically.
2. **Chromium crypto correctness** against the KAT vectors, both directions, incl. the host-hash
   prefix and `meta.version` gate — a wrong cipher locks the owner out.
3. **Interlock parity on Mac**: the kqueue-pin re-verify really runs before every `os.kill`; unknown
   identity/start-time/process-set fails closed; the PID-reuse window is the only accepted weakening
   and is documented.
4. **No secret ever logged or in argv** (bundle secrets, Keychain `-w`, dashboard token), except a Claude Code
   login too long for one `security -i` line, which goes to `security` as `-X` hex like Claude Code's own writes.
5. **Grep/architecture rules** still hold with the package split (process control only in procs'
   sub-modules; machine paths only in config/run).
6. **Windows unchanged**: the moved Win32 code is byte-equivalent; all 1,382 Python + 40 Node tests
   still pass.
Then one follow-up review of only the requested fixes.

### Docs updates

- README: a macOS section (install.sh, menu bar, "move everything" from the PC, the explicit final
  removal step and what skipping it means).
- VALIDATION.md: the macOS live acceptance + PC export checklists (§3.5), kept separate from the
  simulated checks.
- HANDOFF contract: the new actions, the `platform` state block, the `moved` owner, the bundle
  format version.

---

## 5. Open items for the owner

- **O1 — decided:** Intel Macs are supported with cryptography 48.0.1 universal2 and stay
  untested (no Rosetta on the test Mac); Apple Silicon uses 50.0.1. Code must work on both versions.
- **O2 — decided:** cffi 2.1.1 is pinned as the third download (it is part of installing
  cryptography); AGENTS.md allows it.
- **O3 — Menu bar / notifications on an ad-hoc bundle** need a real-Mac owner check; if UN or TCC
  refuse the thin symlink bundle, switch to the self-contained signed layout (PLATFORM-MAC.md §5),
  or fall back to osascript notifications + browser-only shell.
- **O4 — Confirm the §6 unknowns** on a real signed-in Mac (Keychain item names, cookie DB meta
  version, whether Desktop needs a fresh authorize, whether LevelDB stores must travel). The plan
  fails closed on each, but confirming turns deferred paths into direct ones.

## 6. Owner real-Mac checks (collected)

From PLATFORM-MAC.md "Owner checks": menu-bar visibility/clicks (light/dark/notch); UN permission
prompt + banner for the ad-hoc bundle; `launchctl bootstrap gui/$UID` + Login Items name + disable
behaviour; Finder/Spotlight open + reopen-while-running; Claude Desktop bundle id, whether
`osascript quit` prompts, whether SIGTERM runs its quit cleanup (log markers); Keychain access
dialog text + partition-list password prompt for `Claude Safe Storage`/`Claude Code-credentials`;
cffi AESGCM self-test. Plus the move's live checklist (§3.5).

## 7. Not achievable (or weaker) on macOS

- **Taskbar totals widget**: no macOS equivalent; folded into the menu-bar title. The setting is
  hidden on Mac. (Not a loss — the menu bar carries the totals.)
- **Mandatory file locks**: macOS has none, so the settle "Claude let go of its files" probe is
  weaker ("no process alive" + `lsof` + grace) than the Windows share-mode probe (INVENTORY.md
  §7.3).
- **Handle-pinned verify-then-kill**: replaced by the kqueue pin, which leaves a microsecond
  PID-reuse window the safety rules accept (INVENTORY.md §7.4). Not eliminable without a pidfd
  (Linux-only).
- **No-prompt native UI without a signed bundle**: notifications/TCC may re-prompt after each
  ad-hoc rebuild; a Developer-ID signature (owner's Apple account) would remove this, otherwise
  expect occasional re-prompts (O3).
- **Claude Desktop profile import without opening Desktop once on the Mac**: conversion needs the
  Mac's `Claude Safe Storage` key, so it is **deferred** until Desktop is installed+opened — it
  cannot be fully done at import time on a fresh Mac.

## 8. Facts read from a Mac with the apps installed (2026-10-07, before they were first opened)

Read from the app bundles and install folders only; no credential store was touched. The versions are
the ones installed then.

- macOS 27 on Apple silicon (arm64). On a Mac without the Command Line Tools, `/usr/bin/python3` and
  `git` are only shims that open an install dialog, so AccountDeck never calls them.
- **Claude Desktop**: `/Applications/Claude.app`, bundle id `com.anthropic.claudefordesktop`,
  executable `Claude`, version 2.26454.2.
- **Codex app**: on macOS the Codex app ships as **`/Applications/ChatGPT.app`**, bundle id
  **`com.openai.codex`**, executable **`ChatGPT`**, version 26.1002.52244. It bundles its own CLI at
  `ChatGPT.app/Contents/Resources/codex-cli/bin/codex` (+ `codex-code-mode-host`). Classify a
  `ChatGPT` process from bundle `com.openai.codex` as the Codex desktop consumer (§1.2), and its
  bundled `codex` children as Codex processes.
- **Codex CLI** 0.161.0: package layout (`bin/codex`, `bin/codex-code-mode-host`, `codex-resources/`,
  `codex-path/rg`) in `~/.local/share/openai-codex/0.161.0`, `current` symlink beside it; the PATH
  entry `~/.local/bin/codex` is a **shell-script launcher** that `exec`s the real binary. `codex_exe`
  discovery must accept that (resolve the launcher or look in the package), and running Codex
  processes show the package path, not `~/.local/bin`.
- **Claude Code** 2.1.293 (native installer), `~/.local/bin/claude`.
