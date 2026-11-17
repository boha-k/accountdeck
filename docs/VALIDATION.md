# AccountDeck 2 for Mac: validation

What the automated checks prove, and the checks a maintainer runs by hand on real accounts.
[CONTRIBUTING.md](../CONTRIBUTING.md) says how to set up a development Python (`$PY` below).

## Automated (safe mode, synthetic only)

```sh
bash tools/check.sh                                                      # everything below except the install tests
ACCOUNTDECK_SAFE_MODE=1 "$PY" -E -s -m unittest discover -s tests -t .   # the unit tests alone
```

A deliberately small suite (a few seconds) that guards the paths where a regression would lose or
corrupt a sign-in or switch the wrong account:

| Area | Tests |
| --- | --- |
| Vault and database | Mac vault cipher round trip, three generations, tamper/wrong-key fail closed; a guarded Codex save never replaces a newer refresh token; an older terminal login never replaces a newer one; a damaged database goes back to its backup with every login following the vault, and with no good backup the start stops and changes nothing; Antigravity's sign-ins stay in their own table and `agy-` records, which a restore never takes for another app's, and removing one removes only its records (`test_vault.py`) |
| Data compatibility | A data folder saved by each released version (2.0.0 and 2.1.0, in `tests/fixtures/`, made by `make_baseline_data.py`) loads in this build with every account, owner, secret, setting and import record, and its schema is unchanged but for Antigravity's table, added after a backup; the old builds named by `ACCOUNTDECK_BASELINES` (`tools/check.sh` makes them from the tags `v2.0.0` and `v2.1.0`) still open the folder after this build used it, with every kind of Antigravity data in it (two accounts, a reading, an error, an older copy, an unfinished switch and Add, both settings), and see none of it (`test_data_compat.py`). The frozen identifiers that live in code keep their values, Antigravity's table, columns, record names and settings among them (the frozen-identifiers test) |
| Updates | `tools/upgrade_test.sh`, in a temporary prefix: each older version installed with a released version's data, this build installed over it; the data files are unchanged byte for byte, the old app is in `app-backups/`, the snapshot in `update-backups/` equals the vault, and the installed code reads every account and secret. With a stub service: a healthy update reports every saved account still there, an unhealthy one rolls back with the data untouched, a busy service and a downgrade are refused. With two Antigravity accounts saved, an update keeps and counts them (`--dry-run` too), and a new version that loses one is rolled back |
| Update check | GitHub's answer is untrusted: only a redirect to exactly this repository's tag page of a plain version names one (another repository or host, extra path parts, an over-long or non-numeric version are refused; the release list and a 404 mean no release); the API, asked only when the redirect cannot be read and at most once a day, offers nothing for a draft, a prerelease or a malformed, hostile or oversized answer; the last good answer stays; checks that fail wait 5, 10, 20, 40, then 60 minutes, GitHub's Retry-After holds every check back up to 6 hours, and one success resets the wait; a check that found nothing new writes and logs nothing, and what earlier builds saved still loads; versions compare as dotted numbers; a failing or hanging check never fails a tick and never raises, and a look at the app never waits for it; safe mode and a source checkout never reach the network; a version is notified once and a skipped one never; start-update runs only the offered, validated version and refuses where it must; `updates.REPO` equals `REPO=` in `get.sh` (`test_updates.py`, with a stand-in for GitHub) |
| Release package | `tools/mac_package_test.sh`: the package installs into an empty folder, holds exactly the shipped file set and nothing private, and `get.sh` installs it from a `file://` release folder |
| Terminal auto-switch | Threshold order and eligibility, the 99 % fallback, refill/outage/expired-token holds, burn-rate early switch; accounts with and without a 5-hour window side by side; a new meter moves terminals and a codename does not; a lock counts only on a limit that holds terminals; Fable's weekly limit holds them whatever is pinned, and an account whose week is spent is left for one that is only out of Fable; the user's own choice holds against the threshold until 99 %; the early switch never trades places nor follows an automatic switch; a limit error counts only after the settle margin (a Fable one with no model pinned too), a reading with room takes it back, and auto-switch never returns to an account left at a limit; the forecast forgets readings from before the terminals came or the Mac slept (`test_cli_policy.py`) |
| Switches and rollback | Terminal switch keeps shared MCP/plugin logins and saves the outgoing login; an unverifiable switch restores the exact previous bytes; a rollback never undoes a later Claude Code write and keeps its encrypted backup; a Claude Desktop switch with Claude closed restores the target and saves the outgoing account; two refused reads at least 3 minutes apart mark the live login dead; a sign-in on the account in use goes live at once and never over a newer login; an interrupted switch settles by itself and keeps a newer login; the account in use is never removed, also while Claude Code holds no login; an automatic switch ends the user's own choice; a failure about this Mac holds auto-switch and backs off no target (`test_switching.py`) |
| Codex | A sign-in Codex dropped (`account: null`) reads as "sign in again", not as another account; a login Codex holds or may hold, or one Recover may put back, is never renewed; a renewed login is recovered after each interruption without a second send; an uncertain renewal asks for sign-in; a login without a refresh token never replaces a saved one; only a session certainly on another login is left out of a switch (`test_codex_refresh.py`) |
| Antigravity | Save is refused before the notice about Google's terms is accepted (Add and Switch pass the same gate); a switch never loses the outgoing sign-in: a failed write puts the earlier one back, and when that fails too both sign-ins wait for **Recover**, which refuses an unreadable current sign-in and saves another account before writing; a switch waits while `agy` runs and never stops it, **Switch Now** and Add are refused then; an Add that is cancelled, loses its sign-in window, times out or outlives a restart puts the earlier sign-in back only once no `agy` runs; `test_ending_an_add_never_writes_while_agy_runs` checks that a sign-in finishing after Cancel is saved and kept; a Switch, an Add or a Recover looks again right before it writes, and stops with nothing changed when `agy` started or its sign-in changed since the first look; a third account's sign-in found after a failed switch is left for **Recover**, which saves it first (a work or school sign-in is left as well); Sign In Again as someone else saves that sign-in apart; a reading is dropped when `agy`'s account changed during the run; a day of ticks with `agy` renewing hourly and its account at its limits runs no `agy` usage or version check and switches no Antigravity account, even while a terminal account at its limit is switched automatically; opening AccountDeck reads once and checks the changed `agy` file's version; no Antigravity id enters alerts or events, including when a waiting switch gives way; a restored database gets its rows back from the saved sign-ins and marks a row whose sign-in is gone (`test_antigravity.py`). With a stand-in for `security` and a stand-in `agy`: the real usage sample and the shapes `agy` may print parse, including missing limit names or windows and empty group names, and hostile or broken answers fail cleanly; a sign-in is read and put back whole with `agy`'s own `security -i` command on stdin, every store state reads without raising, one too long is refused and never sent another way, and no part of a sign-in reaches a log, an argument or a message; the usage run's command, environment, folder and log; a run counts as AccountDeck's own and ends at its limits; failures are told apart; `agy` counts as running even behind a link, T3 Code's agent does not, and the sign-in window unsets what would move its sign-in (`test_antigravity_store.py`) |
| Limit resets | `test_resets.py`: a reset is used only through the plan the user confirmed, once per plan and never for another account's plan; a use whose outcome is not known sends the same request key again, also after a restart, and a reading settles it; a second click then never spends a second reset (the look that shows the first went through sends nothing, Claude's `unavailable`, a try that never left and a `cooldown` answering the key sent again keep the key, a count Codex left out proves nothing); a damaged memory never stops the state; the last look refuses a reset that changed; Claude's resets read runs only after a successful ordinary read, within its limits and never with the setting off; no reset id, request key, organisation id or token reaches the state, the log or an error text. Automatic resets: none with the setting off, with one account that has room or one whose reading is too old, and accounts left out or in need of sign-in do not count; the account in use comes first; a Fable reset is used when only Fable is out, and another account's weekly reset (the one expiring first) when the one in use holds none that fits, where auto-switch then moves; none while another job runs, while a use the user started or the automatic one has no known outcome, within the hour, or within 10 minutes of a try that used nothing; a reset that did not refill stops them, across a restart, until the setting is turned off and on; nothing is used by a reading from before a reset the user used on that account or from the minutes after it, for a limit about to refill by itself, or when the reset no longer refills what is out at the last look; an idle account between 97 % and a higher switch point counts as out |
| Move import | Main bundle (Desktop, terminal, Codex, still-on-PC), confirmation code, settings remapping, idempotent re-import, wrong passphrase writes nothing; hand-over fills only a waiting account (`test_move_import.py`) |
| Dashboard API | Loopback/session/origin rules, a valid Mac state on the demo engine, retired actions and settings refused (`test_server.py`) |
| Usage | An answer without the weekly limit is a failed read that keeps the last reading; malformed answers never raise; `limits[]` is the list of meters; only a rise in money spent alerts (`test_usage.py`) |

`tests/test_mac_platform.py` covers process classification, the stop ladder, Keychain access on a
temporary keychain and the platform modules' imports. These checks do not prove that real providers
accept a switched or imported sign-in, or that the real `agy` behaves as its stand-in does; the manual
live checks below do.

## Manual live checks (maintainers)

These use a real install and real accounts, so a maintainer runs them by hand, on their own Mac, before a
release that touches the area. An automated agent never runs them. Save your work first and use accounts
you can afford to sign in to again.

- **Install and app:** install without preinstalled Python; open from Finder and Spotlight, then again
  while running — one service, the existing dashboard. Menu bar item in light/dark beside the notch;
  Quit; the browser can close while the menu bar keeps working.
- **Open at login:** toggle it; check the `local.accountdeck2` LaunchAgent, logout/login, no second
  service. Uninstall keeps data and removes the LaunchAgent.
- **Notifications:** send a test notification; check the permission prompt, denial and re-enabling.
- **Keychain:** note the access-dialog wording for `AccountDeck2 vault`, `Claude Safe Storage`,
  `Claude Code-credentials` and `gemini` (`agy`'s item) (metadata only; never copy values).
- **Claude Desktop:** switch while idle and with disposable work: SIGTERM quit, relaunch, two-way account
  verification, shared settings/MCP definitions intact. Cancel while waiting. In the dashboard's
  **Claude Desktop accounts**: **Save Current Account** saves the signed-in account; **Add Desktop Account**
  through to the end (Claude reopens on its sign-in screen, sign in with another account, **Save Account**)
  and once with **Cancel Sign-In** (the previous sign-in comes back); switch both ways between two saved
  accounts with **Switch**; **Sign In Again** on an account whose sign-in Claude no longer accepts.
- **Claude Code terminals:** enroll an account (saved only after its terminal closes); switch with a
  terminal open and check `/status` after its next message; VS Code extension follows; MCP OAuth and
  plugin logins survive both directions. Lower the threshold temporarily to watch an automatic switch,
  its reason and the 10-minute Undo.
- **Codex:** consumer detection (terminal, ChatGPT.app), file-backed store check, two-way switch, **Add
  Codex Account** in the isolated folder. An account whose refresh token was refused shows **Sign In Again**;
  re-add it with **Add Codex Account**.
- **Antigravity** (personal Google accounts you can sign in to again):
  `security find-generic-password -s gemini -a antigravity` (never with `-w` or `-g`) shows the item
  without asking, and its modification date moves about hourly while `agy` runs. Read the note about
  Google's terms, then **Save Current Account**; **Add Antigravity Account** through to the end (the
  Terminal window shows `agy`'s Google sign-in) and once with **Cancel** while the sign-in window is open:
  the earlier sign-in comes back only after the window is closed and no `agy` runs. **Switch** both ways. Most
  important: after a switch, `agy` starts as the target account with no Keychain prompt, so the item
  AccountDeck rewrote stays readable by `agy`. Then `agy` keeps saving its renewals into that item (the
  modification date moves again about an hour later; a record `agy` takes for another process's, by its
  `writer_id`, would stop that), and the account switched away from still works when you switch back to it
  later. A switch started with `agy` running waits and leaves it running, and finishes after you quit
  `agy`. **Refresh Usage** matches `/usage` in `agy`, with no self-update during the read;
  `logs/agy-usage.log` is readable only by you; a read that hangs is asked to end after 60 seconds and stopped 5 seconds later; an account whose
  sign-in Google refused shows **Sign-in needed**. Opening AccountDeck or acting on Antigravity checks
  the installed `agy` version; **Health** shows the remembered version after its next report, or says
  it is not checked yet. An `agy` started through a link counts as running too (T3 Code's agent,
  `agy_acp_server.par`, does not).
  The only file stores looked for are `~/.gemini/antigravity-cli/antigravity-oauth-token` and
  `~/.gemini/jetski-standalone-oauth-token`. Where the Antigravity desktop app or IDE is installed: whether
  a switch changes its account too (Google describes a shared sign-in; AccountDeck's plan says "may").
- **Limit resets** (an account that holds one, and that you mean to use it on): the count and the use-by
  date match Codex's `/usage` or claude.ai → Settings → Usage for the same account. For Claude, read what
  Anthropic answers to AccountDeck: **Health** names how many accounts answered that they are eligible and
  the reasons the others gave; an account answered "claude.ai only" offers no use. Open the sheet and choose
  **Keep It**: nothing is sent (no `resets.use` line in the log). Then one real use on an account that
  needs it, through to the report: the count goes down and the next reading shows the limits refilled. A
  Codex account whose limits are all above 80% left offers no use.
- **Recovery:** with disposable accounts only, interrupt a switch and Recover; a later app change must
  not be overwritten and the encrypted backup must remain. For terminals, Recover then closes the switch
  and the terminals keep the login Claude Code changed.
- **Usage:** compare readings with provider screens; wait for a real reset (Resetting, then Refilled);
  check 429 pauses and outage banners against status pages.
- **Logs:** `~/Library/Application Support/AccountDeck2/logs/accountdeck2.log` must hold no secrets.
- **Update:** update from the previous release with `get.sh`: it names both versions, ends with every
  saved account still there, and `update-backups/` holds a snapshot readable only by you (folder 0700).
  `get.sh --check` exits 0 afterwards.
- **Update prompt** (once a release newer than the installed one is published): within 5 minutes, or at once
  when you open the menu bar menu or the dashboard (or choose **Check Now** in Settings → About), the
  dashboard shows "AccountDeck X is available. You have Y.", the menu bar menu shows **Update to X…**, and one
  notification arrives (it waits for the end of quiet hours). **What's New** opens that release's page in the
  browser; **Skip This Version** hides it until a later version or **Check Now**. **Update…** with no switch
  running: the confirmation, then a Terminal window running `get.sh --version X` that ends with "All N saved
  accounts are still here" (or "The saved account is still here"), and the new version in the dashboard footer
  once you open it again. With **Check for updates automatically** off, the log gets no `update.checked` or
  `update.check_failed` line. While nothing changes, a running copy adds no `update.` line to the log.
- **After an update:** `run.py --doctor` exits 0 and the dashboard footer names the installed build; every
  terminal account reads **Ultra**, **Max** or **Pro** as on claude.ai; an Ultra account without a 5-hour
  limit shows none; nothing shows a codename such as "Iguana necktie".
- **Limit errors:** with a disposable account near its limit, let a terminal session hit it: the terminals
  move within seconds, the switch alert says how fast, and the stopped session is listed until you type
  "continue" in it.
- **Sign-ins due:** a login ending within a week is listed with **Sign In Again**; signing in on the
  account the terminals use reaches them within seconds (check `/status` after the next message).
