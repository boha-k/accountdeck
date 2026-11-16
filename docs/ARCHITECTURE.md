# Architecture

One page for a new contributor: the pieces, how a usage read and a switch move through them, how
Antigravity sits apart from both, the rules nothing may break, and where the design notes fit. Working
rules, the test suite and the demo are in [CONTRIBUTING.md](../CONTRIBUTING.md); what the app does for its
user is in [GUIDE.md](GUIDE.md).

## One process, a few threads

AccountDeck is one Python 3.14 process with one dependency, `cryptography`. `AccountDeck 2.app` runs
`run.py --app`: when a healthy service already runs for the data folder it opens that service's dashboard
and exits; otherwise it becomes the service, holding a lock in the data folder and the per-user owner
lock (`~/Library/Application Support/AccountDeck2-Owner.lock`). A copy that cannot take the owner lock
runs read-only.

| Thread | What runs there |
|---|---|
| main | AppKit: the menu bar item and the dashboard window (`platform/appkit.py`, `statusbar_mac.py`, `dashboard_window.py`) |
| `ad2-http` | the loopback HTTP server (`server.py`) |
| `ad2-sched` | the engine's tick loop (`engine.py`) |
| `ad2-refresh` ×3 | usage reads handed out by the scheduler (`scheduler.py`) |
| `ad2-ops` | one user operation at a time: switch, add, capture, remove, recover, import (`operations.py`) |
| `ad2-agy-usage` | one run of Antigravity's usage command, started by **Refresh Usage** or for the user opening AccountDeck, never on a timer (`agy_accounts.py`) |
| `ad2-agy-version` | `agy --version` when the user opens AccountDeck or acts on Antigravity, at most once per installed `agy` file during a service run (`agy_accounts.py`) |
| `ad2-menubar` | the menu bar's content and commands, re-read from the snapshot every 5 s (`platform/menubar.py`) |
| `ad2-update` | the update check: one bodiless HEAD to GitHub every 5 minutes and when the user opens the menu or the dashboard (longer apart after failures), only in an installed copy (`updates.py`) |

A watchdog (`engine.py`, started by `run.py`) restarts the process when the tick loop or every usage
check hangs.

## The pieces

```text
 menu bar item (AppKit)          dashboard: accountdeck/web (HTML/CSS/JS, no build step, no CDN)
        |                                   |  GET /api/state, POST /api/action
        |                        server.py  (127.0.0.1 only; contract.py validates every state)
        v                                   v
 +------------------------------ engine.py ------------------------------+
 |  snapshot() ── intel.py, rules.py, alerts.py, notify.py, doctor.py     |
 |  tick ──────── scheduler.py ── accounts.py / cli_accounts.py           |
 |                cli_auto.py ─── cli_policy.py (pure), cli_watch.py       |
 |  action() ──── switching.py ── switch_cli.py / switch_claude.py /      |
 |                                switch_codex.py /                       |
 |                                switch_antigravity.py ── operations.py  |
 |  agy ───────── agy_accounts.py (Antigravity: not a provider)           |
 +----------+-----------------------+-----------------------+------------+
            |                       |                       |
     providers/                 core.py                 procs.py
     claude.py (Desktop)        Vault: vault/*.dpapi    platform/libproc.py (reads)
     claude_cli.py (terminal)   Store: accounts.sqlite3 platform/procs_mac.py (signals)
     codex.py, http.py,         FileTransaction
     status.py,                 platform/keychain.py, platform/credstore.py
     antigravity.py (agy)
```

| Piece | Files | Role |
|---|---|---|
| Entry point | `run.py` | the app, `--serve`, `--demo`, `--doctor`, `--version`, `--set-autostart`, the PC import (`--import-from-pc`, `--move-status`) |
| Service and API | `server.py`, `contract.py`, `rules.py`, `fixtures.py` | HTTP on 127.0.0.1 with a session cookie, an Origin check and a strict CSP; the `/api/state` contract and its validator; the display rules (mirrored in `web/js/model.js`); example states |
| Engine | `engine.py`, `scheduler.py`, `intel.py`, `alerts.py`, `notify.py`, `runway.py`, `pool.py`, `doctor.py`, `diagnostics.py` | the cached state snapshot, the tick loop, the action facade; when to read each account; windows, refills and recommendations; alerts and their delivery; the forecast; the menu bar totals; the health report; the log |
| Updates | `updates.py`, `get.sh` | whether a newer release exists (`state.update`), Skip This Version, and Update…, which opens a Terminal window running the installed `get.sh`; the installer then stops and restarts the service by itself |
| Providers | `providers/claude.py`, `claude_cli.py`, `codex.py`, `http.py`, `status.py`, `chromium_crypto.py`, `errors.py`, `fake.py` | reading usage and identity for Claude Desktop, Claude Code and Codex; one HTTP request with classification; the status pages; Claude Desktop's `Claude Safe Storage` encryption; synthetic providers, and a stand-in for `agy`, for tests and the demo |
| Accounts | `accounts.py`, `cli_accounts.py` | saving the signed-in account, reading usage, the unsaved signed-in row; terminal vault ownership, passive capture and isolated enrollment |
| Antigravity | `agy_accounts.py`, `providers/antigravity.py`, `web/js/antigravity.js` | the fourth app, kept apart from the providers ([below](#antigravity)): its saved accounts, usage on request, Add, Sign In Again, Rename, Remove and Recover; `agy`'s Keychain item, its usage answer and where `agy` is installed; its section of the dashboard |
| Switching | `switching.py`, `operations.py`, `switch_cli.py`, `cli_locks.py`, `switch_claude.py`, `stop_ladder.py`, `claude_verify.py`, `switch_codex.py`, `switch_antigravity.py` | plan, consent and dispatch shared by all apps; the operation runner; one flow per app |
| Terminal auto-switch | `cli_policy.py`, `cli_auto.py`, `cli_watch.py` | the decision (pure), acting on it, and reading Claude Code's transcripts for limit errors (read-only) |
| Limit resets | `resets.py`, `reset_auto.py` | what each account holds, Claude's resets read and the one operation that uses a reset; when a reset is used automatically (off by default, [below](#limit-resets)) |
| Storage | `core.py`, `config.py` | user-facing errors, atomic writes, the encrypted vault with its generation ring, the SQLite store (schema 2, Antigravity's own table, integrity check, daily backups, restore), write-ahead file transactions, the owner lock; paths, settings and safe mode |
| Processes | `procs.py`, `platform/libproc.py`, `platform/procs_mac.py`, `activity.py` | process rows, classification and consent, and AccountDeck's own runs of `agy`, each ended at its time limit; read-only libSystem facts; the one place a signal is sent to a program AccountDeck did not start, and the Terminal windows it opens; which of Claude Desktop's Code sessions are working |
| Platform | `platform/appkit.py`, `menubar.py`, `statusbar_mac.py`, `dashboard_window.py`, `notify_mac.py`, `autostart_mac.py`, `keychain.py`, `credstore.py`, `facts_mac.py`, `locks.py` | the ctypes Objective-C bridge, the menu bar, the WKWebView window, notifications, the LaunchAgent, the Keychain through `/usr/bin/security`, Claude Code's login store, Mac facts |
| PC import | `move_bundle.py`, `move_import.py`, `web/js/move.js` | reading an `.adeck` bundle from the retired Windows build |
| Dashboard | `web/index.html`, `web/css/`, `web/js/` | `app.js` polls `/api/state` and renders each module; `format.js` holds the words; `model.js` the rules |

## How a usage read flows

1. The tick (`ad2-sched`, every few seconds) takes one look at the processes, senses which accounts the
   apps are signed in to, polls the status pages and asks `RefreshScheduler` which accounts are due.
2. The scheduler hands due accounts to the `ad2-refresh` pool through a per-identity request governor:
   never two reads of one account at once, at most one Codex probe at a time, backoff after failures.
3. The provider adapter reads. Claude (terminal and Desktop) asks `api.anthropic.com/api/oauth/usage`;
   the account an app is using is read with the token that app holds, which a read cannot rotate. A
   Claude Desktop account whose token cannot be used may be read through its claude.ai web sign-in.
   Codex is read through a private app-server helper of the installed Codex CLI, in a throw-away
   `CODEX_HOME` and with a blank refresh token, so it can never spend the token Codex holds.
4. `usage.py` normalises the answer (unknown stays `None`, never 0); the store keeps the reading.
5. `snapshot()` builds the state from the store, the adapters and that one look at the machine,
   enriches it (`intel.py`), validates it (`contract.validate_state`) and caches it until something
   changes. The dashboard polls it; the menu bar re-reads it every 5 s.
6. `AlertEngine` compares the previous and the new state and raises each alert once per episode;
   `Notifier` delivers it to the page and to macOS notifications.
7. On the same tick, with the `auto_reset` setting on, `reset_auto` looks whether every terminal account is
   out and starts the use of one limit reset; then `cli_auto` feeds `cli_policy.evaluate`, and a decision
   to move the terminals becomes a terminal switch job.

None of this reaches Antigravity, whose usage is read only when the user asks ([below](#antigravity)).

## How a switch flows

1. The dashboard or the menu bar asks for a plan (`switch-plan`). `switching.dispatch` routes it to the
   app's flow, which builds a `SwitchPlan`: what will happen, what stays, which sessions are running,
   what blocks it.
2. The user confirms (`switch`). `switching.claim` rebuilds the plan, checks the confirmation against it
   and mints a consent token bound to that plan's process set.
3. The job runs on `ad2-ops` and shows in `/api/state` as `pending`, then as a `report`.
4. Before anything changes, the job writes an encrypted transaction (its before-state goes into the
   vault). Then, per app:
   - **Claude Code** (`switch_cli.py`): under Claude Code's own locks (`cli_locks.py`), renew the target's
     login, then write it to Claude Code's Keychain item or credentials file (`platform/credstore.py`).
     It never launches, stops or signals a process; open terminals pick the login up on their next
     message.
   - **Claude Desktop** (`switch_claude.py`): one stop attempt through `stop_ladder.py` (signals go through
     `procs`, which re-verifies each process against the consent), the sign-in files swapped inside a
     `FileTransaction`, Claude reopened, and `claude_verify.py` checks which account came back.
   - **Codex** (`switch_codex.py`): waits for Codex sessions to close, then exchanges `~/.codex/auth.json`.
   - **Antigravity** (`switch_antigravity.py`): waits until no `agy` runs, keeps the outgoing sign-in, writes
     its own record of the switch and puts the target's sign-in in `agy`'s Keychain item, then reads it back
     and checks the account. It never starts, stops or signals `agy`.
5. A failure rolls back without undoing a later change the app made itself; an interrupted switch is
   settled at start (terminals) or offered as **Recover**.

## Antigravity

Antigravity's command-line tool `agy` is the fourth app, and it is kept apart from the other three on
purpose. `engine.agy` is an `AgyService` (`agy_accounts.py`), not one of `engine.providers`, and its saved
accounts are rows of their own table, `antigravity_accounts`, never of `accounts`. The scheduler,
auto-switch, the limit-error watch, the forecast, usage alerts and the recommender work on the providers
and the `accounts` rows, so none of them can reach it. The state shows it as one block,
`providers.antigravity` (`contract.py`), which the dashboard draws as a section of its own.

- **The store.** `agy` keeps its Google sign-in in the login keychain: a generic password, service
  `gemini`, account `antigravity`, whose value is `go-keyring-base64:` followed by the base64 of a JSON
  record. `providers/antigravity.py` reaches it only through `platform/keychain.py`. `sense()` looks at
  the item's attributes, a marker of its last change, normally at most every 30 seconds. Opening
  AccountDeck makes the next tick look at once; a plan or action reads at once, and an open Add is
  checked on every tick. The attributes probe has a 5 second limit; after a timeout, sensing waits a
  minute and shows the sign-in as unreadable. The record is read when the marker changes, for a plan or
  action, and after AccountDeck's own writes; while `agy` is not installed and nothing is saved or
  under way, the keychain is not asked at all. A write goes on the standard input of `security -i`,
  never in an argument, and a record too long for `agy`'s own line is refused. The record is kept and
  put back whole, never changed. When `agy` keeps a file store under `~/.gemini/` instead, AccountDeck
  only sees that the file exists, and saves and switches nothing.
- **The usage run.** Only on **Refresh Usage** (at most once a minute) and when the user opens the menu or
  a dashboard page (`looked()`, with `agy_usage_on_open` on and the signed-in account's last reading and
  last try both over 15 minutes old); never on a timer. A look decides for the sign-in last seen; when
  the tick's next look at the store, within 30 seconds, finds another sign-in, it decides once more for
  that one. The `ad2-agy-usage` thread runs
  `agy -p "/usage" --output-format json` through `procs.py`, in a folder of AccountDeck's own
  (`agy-work` in the data folder, into which AccountDeck puts nothing), with a small fixed environment
  that turns `agy`'s self-update off, its input closed and in a session of its own. After 60 seconds it
  is asked to end, and 5 seconds later it is stopped. `agy`'s own log goes to `logs/agy-usage.log`, readable only by the user, because it can
  name the account's email address. The answer is parsed strictly, and a reading is kept only when `agy`
  was signed in to the same account before and after the run. `agy` talks to Google itself; AccountDeck
  sends Google nothing and never uses an Antigravity token.
- **The switch** follows [How a switch flows](#how-a-switch-flows). Its plan lists the running `agy`
  sessions, and the job waits until none runs: a running `agy` reads its sign-in again when it renews it
  and would move to the other account. Its record is `agy-transaction-<32 hex>`, never a `transaction-`
  record, which every older build would take for one of its own and stop all switching on. When the
  earlier sign-in cannot go back, that record waits for **Recover**. Before writing, Recover reads what
  `agy` holds: the same account is left alone, another account is saved first, and an unreadable sign-in
  stops the recovery with nothing changed. Keeping the current sign-in needs no write. Switch, Add and
  Recover read the store and the process list once more right before they delete or write `agy`'s item
  (`last_look`): saving the outgoing sign-in and writing their own record take a moment, and an `agy`
  that started or a sign-in that changed in it refuses the action with nothing changed. A failed switch
  puts the outgoing sign-in back by itself unless `agy`'s item then holds a third account's sign-in:
  that one is saved nowhere, so it stays and the record waits for Recover, which saves it first (one
  AccountDeck cannot save, a work or school account, stays as well: Recover can leave it there).
- **Add and Sign In Again.** With no `agy` running and no Antigravity usage read under way, the job
  saves the current sign-in, writes `agy-enrollment` (the earlier record whole), deletes `agy`'s item
  and opens a Terminal window running `agy` (`platform/procs_mac.py`), which then asks for a Google
  sign-in. The tick's `agy_enrollment` phase watches the item and saves the sign-in that lands.
  **Cancel**, a closed sign-in window or 15 minutes without a sign-in starts ending the Add. With an
  earlier sign-in to put back, it waits in `restoring` until no `agy` runs and the keychain answers;
  `expires` is null and the message says what it waits for. Other Antigravity actions are refused
  meanwhile. A new sign-in that lands is saved and kept, also after Cancel. A sign-in whose form
  AccountDeck cannot read or save is left alone and ends the Add as `failed`. With no earlier sign-in,
  the Add ends without waiting for `agy` to close. The record keeps the window and the reason for
  ending and outlives a restart, so an Add goes on after one.
- **The version.** `agy --version` runs only when the user opens AccountDeck or acts on Antigravity,
  on `ad2-agy-version`. Its answer is kept in memory for each installed file (resolved path, inode,
  size and modification time). The service's health report uses that answer and says when the version is not
  checked yet; the report run by hand (`run.py --doctor`) shows no `agy` version and never runs `agy`.
- **T3 Code.** `t3_code` says whether T3 Code's own Antigravity folder exists. It controls the dashboard's
  note about that separate sign-in. Only the folder's existence is checked; nothing in it is read or written.
- **At start and after a restore,** `reconcile()` makes the table and the `agy-` vault records agree: a
  record without a row gets its row back, and a row without its record asks for a new sign-in.
- **The notice.** Save, Add and Switch are refused (`notice_required`) until the user has read the note
  about Google's terms once (the internal setting `agy_notice_seen`).

## Limit resets

`resets.py` holds everything about limit resets; it is called from the engine and has no thread of its own.

- **Rows.** `usage.py` turns Codex's `rateLimitResetCredits` and Claude's `cedar_ember` block into rows
  without any provider id; `resets.py` adds `key` (a hash, AccountDeck's own name for a row), what the row
  refills, and whether it could be used now. The state gets `accounts[].reset_credits` and
  `accounts[].resets`.
- **Claude's read.** The ordinary usage answer has no resets, so after a successful ordinary read the engine
  offers the account's route to `Resets.after_read`, which asks once more with `cedar_ember=1&skip_spend=1`
  when the last answer is over an hour old (15 minutes after a Refresh or after AccountDeck was opened). It
  never renews a token, counts against the request governor, and a failure only delays the next try (6 hours
  after a 401, 403, 429 or three failures). Off with the `claude_resets` setting.
- **A use.** `reset-plan` builds a plan from the last reading (5 minutes, one use). `use-reset` checks the
  plan and the row again and starts a `ResetOperation`: a last look at the provider's fresh answer, the
  request key saved in the internal setting `limit_resets`, then one request. Codex: one private
  `codex app-server` session set up as the read-only probe is, `account/rateLimitResetCredit/consume` with
  the credit's id and the key. Claude: one POST to `…/api/organizations/<org>/reset_rate_limits` on
  `api.anthropic.com` with the account's OAuth token. This POST and the resets read on the OAuth route carry
  Claude Code's User-Agent (`claude-cli/<installed version> (external, cli)`, `providers.base.is_reset_request`):
  to any other client Anthropic answers `ineligible_reason: "surface"` and lists no grants. Every other request
  keeps AccountDeck's own.
- **An outcome not known.** A timeout, a lost answer, a 5xx, Claude's `unavailable` or an outcome this build
  does not know leaves the key in place and the account shows `resets.doubt`. The same reset is sent again
  only with that key (Claude: for 10 minutes; Codex: for 24 hours), and a try that fails before the provider
  answers about the reset keeps it too, as does the provider's "no" to a request sent again (the earlier one
  with that key may just have gone through). A reading settles it: the count down means used (a use that finds this
  in its own last look sends nothing more), still there after the key's time means not used. A count the
  provider left out is never taken for zero.
- **An automatic use.** `reset_auto.py` (the setting `auto_reset`, off by default, and only with
  `cli_autoswitch` and `claude_resets` on) runs on the tick just before auto-switch. `due` (pure) takes the rows
  auto-switch decides on (`cli_auto.inputs`) and says whether every account of the terminal pool is at the bar
  (97 %; for the account in use the switch point when higher) in its weekly or Fable weekly limit, by readings
  at most 15 minutes old; an unknown reading means no, and that account is read again. A reading that shows an
  account out is unknown too when it was taken before a reset used on that account through AccountDeck
  (`used` in the memory, the user's own included) or under 10 minutes after it, and a limit that refills by
  itself within 10 minutes is waited for. `choose` takes the rows `reset-plan` would
  offer (`Resets.usable_rows`) and picks one that refills every limit holding its account back: the account in
  use first, then the least reset that suffices, the earliest expiry, auto-switch's order.
  `Resets.use_automatically` starts the same `ResetOperation`, whose last look also checks that the reset
  still refills what is out, and which asks `confirm` once more right before the
  request; `confirm` writes `last` (account, time, limits, `open`, `unsure`) to the memory first, as the request
  key is written. Afterwards `reset_auto.finished` (from `engine._record_outcome`) records the outcome, the
  event and the alert. The next tick waits for a reading of that account: under the bar closes `last`; still at
  the bar 10 minutes or more after the reset sets `stopped`, which only turning the setting off and on clears.
  The tick does nothing while a job runs or records its end, and it settles an open `last` also while the
  setting is off (then without stopping anything), so that it is never judged by a later week's reading.
  An outcome not known keeps `unsure` until the request key settles (`Resets._auto_verdict`). Auto-switch waits
  up to 2 minutes for that reading (`reset_auto.awaited` in `cli_auto._hold`), so the two never act on the same
  stale numbers; while the operation runs, auto-switch is held like under any other job. The state gets
  `providers.claude_cli.auto_reset` (`enabled`, `stopped`, `last`).
- **A damaged memory.** `clean_memory` drops whatever is not in the shape this module writes when the service
  starts, and the state's view of an account's resets never raises. A damaged `auto` part counts as `stopped`:
  what held the next automatic reset back is gone.
- **One Claude account in two apps.** Matching identity and organization link their pending uses. An unresolved
  use blocks the other entry and removing either entry; retrying stays on the original entry. Complete reads
  from either entry can settle it, and confirmed uses update both cached counts. Incomplete reads keep the
  request after its retry window. Failed setting writes restore the last committed in-memory state.
- **What it keeps.** The internal setting `limit_resets`: per account Claude's last block with its grant ids,
  the read's timing, an open request key and `used`, when a reset was last used on the account through
  AccountDeck; and `auto`, what `reset_auto` remembers (`last`, `failed`, `stopped`). An older build drops
  `used` and `auto` unread. No table, no vault record; it never reaches the state or
  `/api/settings`.

## Rules nothing may break

These hold across every change (`AGENTS.md` keeps the full list):

- Mutations keep encrypted recovery state; rollback preserves later application changes.
- Read-only usage never rotates an official client's refresh token.
- Process control stays in `procs.py` / `platform/procs_mac.py` (`platform/libproc.py` only reads);
  unknown process identity fails closed.
- Shared Claude configuration, MCP definitions, plugins and transcripts are never touched by a switch.
- The Keychain is reached only through `/usr/bin/security`, secrets on stdin, never `-A`
  (`platform/keychain.py` explains the one argv exception, which `agy`'s item never takes). Replacing the
  Python runtime therefore never changes who may read the vault key.
- No token, cookie, key, email or secret-bearing URL is printed or logged by AccountDeck. `agy`'s own
  log of a usage read (`logs/agy-usage.log`, readable only by the user) can name the email address.
- No telemetry. Besides Claude's and OpenAI's endpoints and their status pages, the only request is the
  update check, from an installed copy only and off with the `update_check` setting: every 5 minutes and
  when the user opens the menu or the dashboard, one HEAD of `github.com/<REPO>/releases/latest` with no body
  either way and nothing in it but the version in the User-Agent (longer apart after checks that fail, up to
  an hour, or GitHub's Retry-After up to 6 hours), and GitHub's API only when that answer cannot be read, at
  most once a day. It runs on its own thread, so it can never fail or delay a tick, a read, a switch, the
  menu or the page, and of GitHub's answer only a checked version is kept. A check that found nothing new
  writes nothing to the database. Antigravity's usage comes from running the user's own `agy`, which talks
  to Google itself; AccountDeck sends Google nothing.
- Nothing automatic reaches Antigravity: no switch by itself and no usage read in the background. A running
  `agy` is never stopped, moved or signed out, AccountDeck ends only its own runs of `agy`, and nothing under
  `~/.gemini/` is written.
- Safe mode (`ACCOUNTDECK_SAFE_MODE=1`, used by every test and the demo) refuses the login keychain, real
  processes, the network outside registered loopback test servers and the live service.
- The names, paths and formats an existing install depends on are frozen: the app name and bundle id,
  the support folder, the vault key's Keychain service and account, the vault record format, the
  database schema and the service port, and from 2.2.0 Antigravity's table, record names and settings,
  which stay apart from everything an older build reads. [UPGRADING.md](UPGRADING.md#for-contributors)
  lists them and the tests that guard them.

## The design notes in `docs/mac/`

[`docs/mac/`](mac/) holds the research and the build plan of the Windows-to-Mac port (October 2026). They
are a historical record, not a description of the current app: where they and the code differ, the code
is right. They are still useful for the why: the Keychain and process facts behind `platform/`, the
provider formats behind `providers/`, and the import contract. Code comments cite their sections (for
example `docs/mac/PLATFORM-MAC.md §8`); a bare `PLAN §…` points to the original design plan of the
Windows build, which is not part of this repository.
