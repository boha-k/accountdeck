# Changelog

All notable changes to AccountDeck are listed here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Each version has a heading
`## [X.Y.Z] - YYYY-MM-DD`; the release notes on GitHub are cut from these headings.

## [Unreleased]

### Added

- **Use a limit reset when every terminal account is out**, a setting in Settings → Auto-Switch, off unless
  you turn it on. When every terminal account auto-switch could use is at 97 % or more of its weekly or Fable
  weekly limit (the account in use: at your switch point, when you set that higher), AccountDeck uses one
  Claude Code limit reset by itself: on the account in use when it holds one that fits (a Fable reset when
  only Fable is out, a weekly one when the week is), else on another account, where auto-switch then moves
  the terminals. One at a time: never while another action runs or an outcome is not known, never before a
  reading shows the refill, and at most one an hour. Never for a limit that refills by itself within 10
  minutes, and never by a reading taken before a limit reset you or AccountDeck used on that account, or in
  the 10 minutes after it. If a reset does
  not refill its limit, automatic resets stop with an alert until you turn the setting off and on. Each use
  is told as a report, a line in Recent activity and a notification. Codex and Claude Desktop resets are
  never used automatically, and an account you left out of auto-switch is never reset.

### Changed

- Claude limit resets show and can be used. Anthropic lists an account's resets only to Claude Code and
  claude.ai, and answered AccountDeck's own name with "claude.ai only", so the resets read and the request that
  uses one now go out under Claude Code's name (`claude-cli/<installed version> (external, cli)`). Every other
  request still names AccountDeck.
- Keep the data saved by 2.2.0 as a synthetic fixture and test future updates against it, including Antigravity sign-ins.
- Auto-switch always counts Fable's weekly limit, whatever model is pinned (Settings already said so). An
  account at your threshold on Fable is left, one that is out of Fable is no target, and a "You've hit your
  Fable limit" error in a terminal moves the terminals. Before, with no model pinned, an account out of Fable
  looked healthy: nothing moved, and the next account could be out of Fable too. When Fable is all that is
  low everywhere, the alert says so ("other models still have room"), and an account that works for no model
  at all is left for one whose week has room.
- Your own choice stands. After **Undo**, or a switch you make in AccountDeck, auto-switch leaves the terminals
  on that account for 2 hours even past your threshold, until a reading says 99 %. A limit reached or a login
  that ends still moves them, and your choice ends there: it does not hold the terminals on that account when
  auto-switch brings them back to it later. Before, the threshold switch could reverse your choice on the next
  check.
- The early switch ("Use weekly allowance before it resets") only moves to an account whose week ends more
  than an hour sooner, and never within 2 hours of another automatic switch. Two accounts with the same reset
  time no longer trade places.
- The burn-rate forecast counts only readings since the terminals came to the account and since the Mac last
  woke, so an account that comes back fuller than it was left no longer looks as if it is filling fast.

### Fixed

- A terminal switch that runs while `/login` is half done (Claude Code has the new account's login but still
  names the old account) no longer saves the new login under the old account. The switch stops with nothing
  changed and says so (auto-switch tries again by itself), so neither account needs a new sign-in.
- Recover no longer gets stuck after an interrupted Codex switch when Codex renewed or changed its sign-in
  afterwards. Codex keeps the sign-in it has now, AccountDeck saves it (or says that Codex holds none it can
  save), and Codex and Claude Desktop switches work again. Before, every Recover said "Recovery paused", and
  the account Codex had before could end up needing a new sign-in.
- Pressing Recover for Claude Desktop after an earlier Recover was cut short (AccountDeck quit or crashed in the
  middle) no longer leaves Claude signed in to the account the switch was moving to.
- A Claude Desktop switch, Add or Recover now stops with nothing changed when Claude is opened again while
  AccountDeck is working, instead of replacing Claude's sign-in files while it runs.
- When Claude or OpenAI asks AccountDeck to wait before checking usage again, the wait is now at most an hour
  and 15 minutes. One answer asking for years could pause an account's checks, its Refresh and auto-switch to
  it for that long. A longer pause saved by an earlier version is cut to that at the next start.
- Terminal accounts now show "refilled" and send the refill alert when a limit resets, like Claude Desktop and
  Codex accounts.
- A saved terminal account can be removed while Claude Code is signed out (all but the one still shown as in
  use). Before, Remove failed with "The terminal login file is missing."
- The dashboard no longer shows two terminal accounts in use, or none, when a switch or a Remove ends at the
  moment the page is updated.
- With three or more Codex accounts, usage checks of Codex accounts waiting their turn no longer hold up the
  check of the account your terminals use, which auto-switch decides on.
- After a Claude Desktop switch that could not be confirmed at once, AccountDeck keeps checking which account
  Claude opened even when another job (an automatic terminal switch, say) runs meanwhile. Before, that job
  ended the check, so a wrong account was never reported and Recover was never offered.
- Importing accounts from your PC now says which of this Mac's settings will change to match the PC, before
  you import and once it is done, for example "switching accounts automatically (on)". Before, a Mac with
  auto-switch off could come out of an import with it on, without a word.
- If AccountDeck quits or crashes during a Codex Add, the sign-in that finished meanwhile is saved at the next
  start and its unencrypted copy is deleted. Before, that copy stayed in AccountDeck's data folder and the
  sign-in was not saved.
- `uninstall.sh` stopped because AccountDeck was still running no longer turns off Open at login: the app stays
  installed and still opens at login.
- An account paused by a provider incident says so once on its row, not twice. A limit's countdown with no room
  left ends in dots instead of stopping mid-word. The user guide now says the early switch is on by default.
- Cancel pressed just as a job reaches a step that cannot be stopped now cancels the job, instead of being
  accepted and then ignored.
- An account's row stops saying it is being checked as soon as the check has ended, and a new reading shows
  without waiting for Claude's limit resets to be read.
- Terminal auto-switch keeps deciding when a stored number is far too large to use (a damaged setting or
  reading), and noting which Claude Desktop or Codex account is in use no longer drops a note a usage check
  wrote at the same moment.

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
