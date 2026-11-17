# Working on AccountDeck

AccountDeck 2 is a Mac menu bar app with a local dashboard that tracks Claude, Codex and Antigravity usage and
switches saved accounts for Claude Code, Claude Desktop, Codex and Antigravity's command-line tool `agy`. It
supports Macs with Apple silicon on macOS 14 (Sonoma) or newer, and is developed and tested on macOS 27. Windows
support has been retired; of the move from the old Windows version, only the Mac's import side is kept.

Read [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, the safety rules, the test policy and pull
requests. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains how the parts fit together.

## Layout

- `run.py`: entry point (`--app`, `--serve`, `--demo <scenario>`, `--doctor`, `--version`, `--set-autostart`,
  `--import-from-pc`, `--move-status`).
- `accountdeck/`: the Python service. `engine`, `operations`, `switching` (plan and consent shared by all apps)
  with `switch_claude`/`claude_verify`/`switch_cli`/`switch_codex`/`switch_antigravity`, `providers/`; vault and
  database (integrity check, daily backups, restore) in `core`; the API in `server` + `contract` (validator) +
  `rules` (display rules) + `fixtures` (example states). Antigravity is outside the engine's usage providers on
  purpose: `agy_accounts` (its accounts in their own table, Save, Add, Remove, usage on request, recovery at
  start) and `providers/antigravity` (agy's keychain item, its usage answer, where agy is installed). `procs`
  holds the process rows, kinds and consent, and AccountDeck's own runs of `agy` (the usage command and
  `agy --version`, checked only when the user opens AccountDeck or acts on Antigravity, once per installed
  file during a service run; each run is ended at its time limit). `cli_policy` decides terminal switches (pure),
  `cli_auto` acts on it, `cli_watch` reads Claude Code's transcripts for limit errors (read-only), `runway`
  forecasts the pool (pure), `doctor` is the read-only health report (`run.py --doctor`), `updates` is the update
  check on its own thread (every 5 minutes and when the user opens AccountDeck) and the update prompt's actions
  (`REPO` there equals `REPO=` in `get.sh`). `resets` is limit resets: the rows and the `resets` block of Claude
  and Codex accounts, Claude's separate resets read after an ordinary read, and the actions `reset-plan` and
  `use-reset` (its memory is the internal setting `limit_resets`; no table, no vault record). `reset_auto`
  decides the automatic use of one (the setting `auto_reset`, off by default; `due` is pure) and acts only
  through `Resets.use_automatically`, on the tick just before auto-switch.
- `accountdeck/platform/`: macOS integration. `appkit` (ctypes Objective-C bridge), `menubar` + `statusbar_mac`
  (menu bar item), `dashboard_window` (WKWebView window), `notify_mac`, `autostart_mac` (LaunchAgent),
  `keychain` + `credstore` (`keychain` also writes agy's item on stdin only and reads an item's attributes
  without its secret), `facts_mac` (where `codex` and `agy` are installed), `libproc` (read-only process
  facts), `procs_mac` (the only place a signal is sent to a program AccountDeck did not start; it also opens
  the Terminal windows for a sign-in, Claude Code's or agy's, and for an update).
- `accountdeck/web/`: the dashboard (static HTML/CSS/JS, no build step, no CDNs).
- `install.sh` / `uninstall.sh`: install into `~/Library/Application Support/AccountDeck2` and
  `~/Applications/AccountDeck 2.app` (the bundle's `python3` is a signed copy so notifications work). An update
  refuses a downgrade, snapshots the saved sign-ins into `update-backups/`, runs the tests, checks the new
  service's health and that every saved account is still there, and rolls back otherwise (`--dry-run`,
  `--prefix DIR` for tests). A second install or update while one runs is refused.
- `get.sh`: the one-line installer and the installed updater; downloads a GitHub release, checks its SHA-256 and
  runs its `install.sh`. `REPO=` at its top names the repository. The one-line install fetches it from the latest
  release (`https://github.com/boha-k/accountdeck/releases/latest/download/get.sh`), never from `main`.
- `tests/`: a deliberately small unittest suite guarding the risky paths (synthetic homes, safe mode), with one
  data fixture per released version in `tests/fixtures/`. This repository's history starts at 2.2.0: the
  builds of 2.0.0 and 2.1.0 are not in it, only the data folders they saved.
- `tools/check.sh`: lint, tests, shell, dashboard and workflow file syntax, every demo scenario's state against
  the contract, the doctor's shape and the old builds against this build's data. `tools/upgrade_test.sh`
  (+ `tools/upgrade/`): updates from every older version in a temporary prefix. `tools/build_mac_package.sh` and
  `tools/mac_package_test.sh`: the release package and its install test. `tools/menubar_preview.py`: the menu bar
  item and window on demo data.
- `.github/workflows/`: `check.yml` runs the checks on pull requests and pushes to `main`; `release.yml` builds,
  tests and publishes a release for a tag `vX.Y.Z`. `.github/ISSUE_TEMPLATE/` and `PULL_REQUEST_TEMPLATE.md`.
- `docs/`: the user guide, upgrading, troubleshooting and architecture pages (`docs/README.md` lists them),
  `docs/VALIDATION.md` (what the tests cover and the manual live checks), and `docs/mac/` (the design notes of
  the Mac port, cited by section number in code comments).
- `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `LICENSE`, `THIRD_PARTY_NOTICES.md`.

## Runtime

Python 3.14 with `cryptography` (and the `cffi` it needs) only. No other runtime packages. The installer pins
a standalone runtime: `~/Library/Application Support/AccountDeck2/runtime/python/bin/python3.14`, which
`tools/check.sh` uses by default. Without an installed AccountDeck, any Python 3.14 with `requirements.txt`
installed works; name it with `ACCOUNTDECK_PYTHON`.

## Developing

- `bash tools/check.sh`, or `ACCOUNTDECK_SAFE_MODE=1 <python> -E -s -m unittest discover -s tests -t .`
- Keep tests few and important: add one only where a regression would lose credentials, corrupt data or switch
  the wrong account.
- `<python> run.py --demo typical --port 0 --no-browser` serves synthetic accounts in a temporary folder.
  Scenarios include `typical`, `terminal`, `ultra`, `mixed`, `runway`, `resets-soon`, `outage`, `fifty`,
  `first-run`, `update-available`, `antigravity`, `antigravity-recover` (`accountdeck/fixtures.py` lists them
  all).
- On a Mac where AccountDeck is installed, the live service (port 53157) manages that user's real Claude Code,
  Claude Desktop, Codex and `agy` logins, including the login of any Claude Code session working on this code.
  Develop against demo mode and synthetic tests: switching accounts for real changes which account your own
  session runs on.
- Antigravity has one more rule: nobody working on this code runs `agy`, reads or writes its Keychain item
  (service `gemini`, account `antigravity`) or touches `~/.gemini/`. Use the fakes (`FakeAntigravity`, the
  fake process controller) and synthetic homes; safe mode refuses the login keychain and real processes.
- While developing, run `install.sh`, `uninstall.sh` and `get.sh` only with `--prefix DIR` or `--dry-run`;
  never over a live install. `install.sh --prefix DIR` never opens the app (it implies `--no-launch`), which is
  what makes it safe for testing. Never call the live service's API or read or write the login keychain.
- Never print tokens, cookies or secret-bearing URLs. Runtime credentials live only in the data folder.

## Releasing

Bump `__version__`, date the changelog section, merge, then push the tag `vX.Y.Z`; the steps are in
[CONTRIBUTING.md](CONTRIBUTING.md#releasing). A release tag publishes to every user: within minutes every installed
copy that is running offers it in its update prompt. Push one only when the maintainer asks for that release.

## Design invariants worth keeping

- Mutations keep encrypted recovery state; rollback preserves later application changes.
- Read-only usage never rotates an official client's refresh token.
- Process control stays in `procs.py` / `platform/procs_mac.py` (`platform/libproc.py` only reads); unknown
  process identity fails closed.
- Shared Claude configuration, MCP definitions, plugins and transcripts are never touched by a switch.
- AccountDeck never sends a request to Google and never uses an Antigravity token itself. The only
  program that talks to Google is the user's own `agy`, which AccountDeck starts only for its usage
  command, `agy --version` and the Terminal window of an Add. The version check runs only when the user
  opens AccountDeck or acts on Antigravity, once per installed `agy` file during a service run. agy's
  sign-in record moves whole between its keychain item and the vault, never changed, and never reaches
  a log, an error text, a process argument or the state.
- No automatic switching and no background usage reads for Antigravity. It is not in `engine.providers` and
  its accounts are not `state.accounts` rows, so the scheduler, auto-switch, the limit-error watch, the runway,
  usage alerts and the recommender never reach it. The usage command runs only on Refresh (at most once a
  minute) and when the user opens AccountDeck (`agy_usage_on_open`, a reading over 15 minutes old).
- A running `agy` is never stopped, moved or signed out: a switch waits until none runs, Add refuses while one
  runs, and an `agy` that cannot be told apart counts as running. An ending Add puts the earlier sign-in back
  only once no `agy` runs; a new sign-in that finishes meanwhile is saved and kept, also after Cancel.
  AccountDeck ends only its own runs of `agy`, at their time limits.
- Nothing under `~/.gemini/` is written; AccountDeck only checks whether agy's file stores exist.
- A limit reset is used in two ways only, and both run the one operation in `resets.py`. `use-reset` uses one
  for the one account and row of a plan the user confirmed within its 5 minutes (one use per plan). With the
  setting `auto_reset` on (off by default; it acts only while `cli_autoswitch` and `claude_resets` are on),
  `reset_auto` uses one Claude Code reset by itself, and only when every account in the terminal pool is out. The
  pool is the account in use and every account auto-switch could move to. An account that is frozen, on hold,
  locked by Claude, in need of sign-in or about to lose its login is outside it, and so is one the user left out
  unless the terminals are on it. Out means 97 % or more (for the account in use the switch point when that is
  higher) of the weekly or the Fable weekly limit, by a reading at most 15 minutes old, with that limit's reset
  still ahead; one account with room (the account in use too while only its sign-in keeps it out of the pool),
  or one whose reading cannot say, and nothing is used. A reading that shows
  an account out cannot say when it was taken before a reset used on that account through AccountDeck (the
  user's own included) or under 10 minutes after it, and a limit that refills by itself within 10 minutes is
  waited for. The 5-hour limit never makes an account out.
  The reset used is one `reset-plan` would offer now that refills every limit holding that account back: on the
  account in use first, else the one that refills the least beyond what is out (a Fable reset when only Fable is
  out), then the one that expires first, then auto-switch's order. Never an account the user left out, never a
  Codex or Claude Desktop reset. One at a time: none while another job runs (no job gives way to it), while
  auto-switch itself is held, while any Claude account's request key is open, before a reading of that account showed
  the refill, within an hour of the last one, or within 10 minutes (doubling to an hour) of a try that used
  nothing. The tick only decides and makes no request; the operation's last look checks that the reset still
  refills what is out, it asks once more right before sending (still due, the three settings still on), and
  the record of the use (`last` in `limit_resets`) is saved before the request is sent, as the request key is. A
  reading taken 10 minutes or more after a reset that still shows its limit at the bar stops automatic resets,
  with an alert, until the user turns the setting off and on; so does a record that cannot be read back. Every
  automatic use writes an event and an alert that name the account, the reset and the reason. Nothing else uses
  a reset: the scheduler, auto-switch, the limit-error watch, the runway, alerts and the recommender never reach
  it. Never two resets for one click or one decision: the request key
  (Codex's `idempotencyKey`, Claude's `request_id`, a UUID 4) is saved before the request is sent and sent again
  for the same reset while its outcome is not known (Claude 10 minutes, Codex 24 hours); a new one is made only
  once the provider's answer about the reset or a reading settled it (its count down: used; still there after
  that time: not used). An answer that says nothing of the reset (the network, the sign-in, a refused request,
  Claude's `unavailable`, an outcome this build does not know, a count the provider left out) never closes an
  open request; nor does a provider's "no" to a request sent again (Claude's `cooldown`, `not_limited` or
  `ineligible`, Codex's "nothing to reset"), which may be the earlier request with that key having gone through.
  A look that shows the earlier request went through sends nothing more. A last look right
  before sending refuses a row that changed. Codex's use goes only through its own app-server, set up as a
  read-only probe (blank refresh token). Claude's claim is one POST through the OAuth route only, with the
  `cedar_ember` program and a body, never `juniper_tide`; only `result: "reset"` counts as used (and
  `already_used` answering a request sent again). Claude's resets
  read runs only straight after a successful ordinary read of that account, on its route, at most hourly (15
  minutes when the user asked), never with a token that needs renewing, not for 6 hours after a 401, 403, 429
  or three failures, and not at all with `claude_resets` off. No token is renewed for a reset. The resets read
  on the OAuth route and the claim go out under Claude Code's User-Agent (`claude-cli/<installed version>
  (external, cli)`), because Anthropic lists an account's grants to no other client there (maintainer,
  2026-11-16); every other request keeps AccountDeck's own. No reset id, organisation id, token or request key reaches the state, the log, an
  error text or a process argument.
- Antigravity data never enters the tables and record names older builds read: its accounts are in
  `antigravity_accounts` (inside schema 2), its vault records are named `agy-…` (never a bare `<32 hex>` or a
  `transaction-` record), and no Antigravity account id is written to `accounts`, `readings`, `alerts` or
  `events`.
- Frozen identifiers: an update finds a user's accounts only because these never changed since 2.0.0. The app
  name `AccountDeck 2` and executable `AccountDeck2`; the bundle id and LaunchAgent label `local.accountdeck2`;
  the support folder `~/Library/Application Support/AccountDeck2`; the bundle `~/Applications/AccountDeck 2.app`;
  the vault key in the login keychain (service `AccountDeck2 vault`, account = first 16 hex digits of sha256 of
  the resolved data folder path); the vault records `vault/*.dpapi` (`ADK1` + nonce + AES-256-GCM); the database
  `accounts.sqlite3` at schema 2 (a newer schema is refused, never downgraded); port 53157, `runtime.json`,
  `/_health`, `/api/session`, `/api/state` and `/api/stop` as `install.sh` uses them; the owner lock
  `~/Library/Application Support/AccountDeck2-Owner.lock`. Every installed `get.sh` also depends on the release:
  the asset names `AccountDeck2-mac.tar.gz`, `AccountDeck2-mac.tar.gz.sha256` and `get.sh`, the `.sha256` line
  (`<sha256>  AccountDeck2-mac.tar.gz`), the archive's one top folder `AccountDeck2-mac/`, and the tags `vX.Y.Z`
  that `get.sh --version X.Y.Z` downloads. Antigravity's names are frozen in the same way from 2.2.0: the table
  `antigravity_accounts` and its columns, the vault records `agy-<32 hex>`, `agy-transaction-<32 hex>` and
  `agy-enrollment`, the app id `antigravity` in the state and the API, and the settings `agy_usage_on_open` and
  `agy_notice_seen`. They are apart from the other apps' data because an older build stops reading every account
  when it meets an account of an app it does not know, and stops every switch while it finds an unfinished
  `transaction-` record. Changing one needs a migration, a test and the maintainer's yes
  ([CONTRIBUTING.md](CONTRIBUTING.md#data-compatibility) has the tables and the tests).
- No telemetry. AccountDeck talks only to Claude's and OpenAI's own endpoints and their status pages
  (Antigravity's usage is read by running the user's own `agy`, which talks to Google itself), plus the
  update check of an installed copy (`updates.py`): every 5 minutes and when the user opens the menu or the
  dashboard (at most once a minute), one HEAD of `github.com/<REPO>/releases/latest` with no body either way,
  no redirect followed and nothing in it but AccountDeck's version in the User-Agent; after checks that fail
  it waits longer (up to an hour, or GitHub's Retry-After up to 6 hours). GitHub's API is asked only when that
  answer cannot be read, at most once a day. Off with the `update_check` setting, never from `run.py` run
  straight from a checkout, safe mode or the demo, never on the engine's tick.
  GitHub's answer is untrusted: only a checked version is kept and no text from it is shown.

## Shipping on the maintainer's Mac (maintainer only)

This section is for the maintainer's own Mac, whose installed AccountDeck follows `main`. It gives no one else
permission to merge or install anything: a contributor, a fork, or an agent working for either never merges a
pull request and never installs over anyone's live service because of this section.

- On the maintainer's Mac, every finished change goes live without asking (maintainer, 2026-11-05): commit
  (stage by name), push, open the PR, merge it once CI passes, then run `bash install.sh` from a clean checkout
  of the merged code and confirm that `/_health` shows the new build and `run.py --doctor` stays healthy.
- Its two limits: no running session is interrupted (no Claude Code, Claude Desktop, Codex or `agy` process is
  quit, restarted or moved to another account), and no saved or live login ends up needing a new sign-in. Ask
  first, naming the risk, before shipping a change that could cross either one (saved logins, the vault or
  Keychain format, a frozen identifier, a data migration, anything that quits or restarts an app).
- `install.sh` restarts only AccountDeck's own service, which no session depends on; it refuses during a switch,
  runs the tests first and rolls back when the new service fails its health check or an account is missing.
  Report a rollback and fix forward.
