# Changelog

All notable changes to AccountDeck are listed here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Each version has a heading
`## [X.Y.Z] - YYYY-MM-DD`; the release notes on GitHub are cut from these headings.

## [Unreleased]

- Claude limit resets show and can be used. Anthropic lists an account's resets only to Claude Code and
  claude.ai, and answered AccountDeck's own name with "claude.ai only", so the resets read and the request that
  uses one now go out under Claude Code's name (`claude-cli/<installed version> (external, cli)`). Every other
  request still names AccountDeck.
- Keep the data saved by 2.2.0 as a synthetic fixture and test future updates against it, including Antigravity sign-ins.

### Fixed

- A terminal switch that runs while `/login` is half done (Claude Code has the new account's login but still
  names the old account) no longer saves the new login under the old account. The switch waits instead, so
  neither account needs a new sign-in.
- Recover no longer gets stuck after an interrupted Codex switch when Codex renewed or changed its sign-in
  afterwards. Codex keeps the sign-in it has now, AccountDeck saves it, and Codex and Claude Desktop switches
  work again. Before, every Recover said "Recovery paused", and the account Codex had before could end up
  needing a new sign-in.

## [2.2.0] - 2026-11-15

The first public release. Installing it over 2.0.0 or 2.1.0 is an update that keeps every saved account.

### Added

- Install with one command:
  `curl -fsSL https://github.com/boha-k/accountdeck/releases/latest/download/get.sh | bash`. The script
  comes from the latest release, never from unreleased code. Running it again updates AccountDeck. An
  installed copy updates itself with `bash "$HOME/Library/Application Support/AccountDeck2/app/get.sh"`.
- An update prompt. Every 5 minutes, and when you open the menu or the dashboard, an installed copy asks
  GitHub whether a newer release is out, so it hears of one within minutes. Each check is one small request
  with no body in either direction whose user agent names AccountDeck's version, and nothing else; after
  checks that fail it waits longer, up to an hour. GitHub's API is asked only if that answer cannot be read,
  at most once a day. A newer release shows as a banner in the dashboard ("AccountDeck X is available. You
  have Y." with **Update…**, **What's New** and **Skip This Version**), as **Update to X…** and **Check for
  Updates…** in the menu bar menu, and as one notification. **Update…** says what will happen, then runs the
  installed `get.sh` in a Terminal window. **Check for updates automatically** in Settings → About turns the
  check off; **Check Now** there asks at once. AccountDeck still sends no telemetry.
- `get.sh --check` says whether a newer release exists and changes nothing; `get.sh --version X.Y.Z`
  installs a given release.
- A Claude Desktop section in the dashboard: save the account Claude Desktop is signed in to, add another
  through Claude's own sign-in screen, switch between saved accounts, sign in again, rename and remove. Each
  step that closes Claude shows what will happen first and waits for your go-ahead.
- Antigravity, a fourth app: an **Antigravity accounts** section for the personal Google sign-ins of
  Antigravity's command-line tool `agy`. Save the account `agy` is signed in to, add another through `agy`'s
  own sign-in in a Terminal window, switch between saved accounts, sign in again, rename, remove, and recover
  a switch that could not finish. Ending an Add puts the earlier sign-in back only once no `agy` runs;
  a sign-in that finishes meanwhile is saved and kept, even after Cancel. A switch shows what will happen
  first and waits until no `agy` runs; AccountDeck never stops an `agy` session. The menu bar shows the
  signed-in account and **Switch Antigravity To**.
- Antigravity's limits, read by `agy`'s own usage command only when you click **Refresh Usage** (at most once
  a minute) or open AccountDeck when the last reading and the last try are over 15 minutes old. **Read
  Antigravity usage when I open AccountDeck** in Settings → General turns the second off. AccountDeck never
  reads them in the background and never switches Antigravity by itself. It sends nothing to Google: the only
  program that talks to Google is your own `agy`.
- Before the first Antigravity Save, Add or Switch, a note about Google's terms for Antigravity, shown once;
  the user guide quotes it.
- Limit resets: each Claude Code, Claude Desktop and Codex account shows how many limit resets it holds, what
  each one refills and when it expires, and **Use a limit reset…** uses one for one account after you confirm
  what will happen. It is never automatic, and a use whose answer is lost is never repeated with a new request.
  Codex's come with every reading and are used through Codex's own app-server. Claude's take one more read after
  a successful usage read, at most hourly (every 15 minutes when you refresh), which **Read Claude limit
  resets** in Settings turns off.
- Antigravity's accounts are kept in a table and vault records of their own, so 2.0.0 and 2.1.0 still open
  the data folder after this version used it, and an update back to this version finds them again. The
  installer counts them with the other saved accounts, and the health report checks `agy`, its Keychain item
  (by its attributes only when run by hand) and the saved Antigravity accounts. The service's report uses
  the remembered sign-in state and `agy` version; the version is checked only when you open AccountDeck
  or act on Antigravity, once per installed file during a service run.
- Releases on GitHub: `AccountDeck2-mac.tar.gz`, its `.sha256` and `get.sh`. `get.sh` checks the checksum
  and the archive's contents before it runs anything. A release tarball or a `git clone` installs with the
  same `bash install.sh`.
- The installer says what it is doing: "Installing AccountDeck 2 X" or "Updating AccountDeck 2 from X to Y".
- Before an update replaces the app, the installer copies the saved sign-ins and the database into
  `update-backups/` in the data folder (still encrypted under the same Keychain key; the last 3 are kept). It
  is a last resort, never restored automatically.
- The installer counts the saved accounts and sign-ins before and after an update. If any is missing
  afterwards, the update counts as failed and the previous version is put back.
- Installing an older version over a newer one stops with nothing changed, unless you add `--allow-downgrade`.
- A second install or update started while one is running stops with nothing changed. When the installer
  cannot make its lock file (a full disk, for example), it says that instead. The lock is released by macOS
  when the installer exits, even after a crash.
- After a first install, a closing message says where the app is and how to update and uninstall it.
- Every pull request and release is tested against the data folders 2.0.0 and 2.1.0 saved: this version is
  installed over each, and every account and sign-in must still be readable with the data files unchanged.
- A user guide, upgrading and troubleshooting pages, an architecture overview, a contributing guide, a
  security policy, issue and pull request templates, the MIT License and third-party notices.

### Changed

- AccountDeck needs a Mac with Apple silicon and macOS 14 (Sonoma) or newer. An Intel Mac gets a clear
  message instead of a failed install.
- `uninstall.sh` also removes old copies of the app (`app-backups/`). It keeps `update-backups/` with your
  data unless you add `--delete-data`.
- The installed app folder holds only what AccountDeck needs to run, update and uninstall, plus `README.md`,
  `CHANGELOG.md`, `LICENSE` and `THIRD_PARTY_NOTICES.md`.

### Fixed

- Claude limit reset requests verify the sign-in's owner, protect both app entries of the same account,
  and keep uncertain requests through incomplete readings and failed saves. A reset count that could not
  be read is shown as unknown. Different reset kinds remain selectable when eligible, including Fable's
  partial weekly refill.
- On a very busy Mac the dashboard could come up with a part missing, because the local service refused some
  of the files the page asked for at once. It now lets them wait.

## [2.1.0] - 2026-11-05

### Added

- Accounts with and without a 5-hour limit side by side. Since Claude's change of 2026-10-30, verified
  Ultra accounts (formerly Max 20x) have only weekly limits. Plans read Ultra, Max or Pro from the login.
- A limit-error watch: AccountDeck reads Claude Code's transcripts (read-only), and when a session hits its
  limit it moves the terminals within seconds, also while usage reads fail. Sessions a limit stopped are
  listed.
- Claude Code's own saved usage answer counts as a free reading.
- A Sign-ins due card. Login alerts wait for the end of quiet hours; critical alerts come through them.
- A forecast of when the terminal accounts run dry at the recent pace, with a warning when that is near.
- Alerts when usage credits (real money) are being spent.
- One critical alert when Claude's usage answer stops giving the weekly limit.
- Reliability: a watchdog restarts a stalled service; the database is checked at start and daily, with 7
  daily backups and an automatic restore.
- `run.py --doctor` (a read-only health report) and `run.py --version`.
- The installer runs the tests first, keeps the previous app, checks the new service's health and puts the
  previous version back if it is not healthy. `--dry-run` changes nothing.
- The Codex section of the dashboard.

### Changed

- A sign-in on the account your terminals use reaches them at once.
- An interrupted terminal switch settles by itself; only an unconfirmed login or an unreadable record waits
  for you.
- A failure on the Mac itself (Keychain, disk, Claude Code's locks) pauses auto-switch briefly instead of
  counting against an account.
- Limits come from Claude's own list of meters; codenames are never shown.
- An account's status sits above its plan and email, and Sign-ins due shows each account's email.

## [2.0.0] - 2026-10-09

The first version of AccountDeck 2 for Mac.

### Added

- A menu bar app and a local dashboard (on 127.0.0.1 only) for Claude Code in terminals, Claude Desktop
  and Codex: usage, reset countdowns and saved-account switching.
- Saved sign-ins in an encrypted vault whose key lives in your login keychain.
- Claude Code terminal switching under Claude Code's own locks. Open terminals and the VS Code extension
  follow on their next message; MCP logins, plugins, settings and history stay shared.
- Auto-switch at 95 % used, in three orders (soonest reset, most room left, your order), with a 99 %
  fallback, an early switch when a limit fills fast, Leave out and a 10-minute Undo.
- Claude Desktop switches (when Claude is idle, or now) and Codex switches, each written as an encrypted
  transaction first, with Recover.
- Alerts with quiet hours, as macOS notifications.
- Import of saved accounts from AccountDeck on a Windows PC.
- An installer that downloads its own pinned, checksum-checked Python runtime, and an uninstaller that keeps
  your data unless asked.
- A "Claude usage left" card on the overview (added on 2026-10-14).

### Fixed

Later fixes shipped under the same version number, up to 2026-11-02:

- No more login-keychain password prompts after a terminal switch.
- A status-page incident no longer holds auto-switch.
- Auto-switch keeps working when Claude reports no 5-hour limit, and never waits on an account that is out or
  needs sign-in; it says why it holds.
- A usage answer without the weekly limit counts as a failed read and keeps the last reading.
- A limit Claude locked reads "Locked by Claude", and an account on hold reads "On hold"; neither is counted
  or picked.
- AccountDeck tracks when each terminal login ends, alerts before it does, and renews a switch's target
  first.
- A spent terminal login is never put back, a newer saved login is never replaced by an older one, and a
  rollback conflict no longer stops renewals.
- Other jobs no longer pause terminal auto-switch indefinitely.

[2.2.0]: https://github.com/boha-k/accountdeck/releases/tag/v2.2.0
