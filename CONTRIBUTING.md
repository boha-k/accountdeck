# Contributing to AccountDeck

AccountDeck holds people's Claude, Codex and Antigravity sign-ins and switches the apps that use them. A
mistake can sign someone out, spend a refresh token or put the wrong account in front of their work. The rules
below exist for that reason. Read them, and [AGENTS.md](AGENTS.md) for the layout and the design rules, before
you change anything.

For a security problem, do not open an issue or a pull request: follow [SECURITY.md](SECURITY.md).

## A development Python

You need a Mac. The code runs on Python 3.14 with `cryptography` and nothing else at run time.

- **With AccountDeck installed**, `tools/check.sh` uses its pinned runtime
  (`~/Library/Application Support/AccountDeck2/runtime/python/bin/python3.14`). Running that Python is fine;
  never write into that folder.
- **Without an installed AccountDeck**, use any Python 3.14 (from python.org, Homebrew or uv, for example)
  with `cryptography` at the version pinned in `requirements.txt`, and name it with `ACCOUNTDECK_PYTHON`:

  ```sh
  python3.14 -m venv .venv-dev
  .venv-dev/bin/python -m pip install -r requirements.txt "ruff==0.16.10"
  export ACCOUNTDECK_PYTHON="$PWD/.venv-dev/bin/python3.14"
  ```

  `.venv-dev/` is ignored by git, and `tools/check.sh` runs `ruff` from it when it is there (otherwise it says
  that lint was skipped). CI pins `ruff` to that version (`.github/workflows/check.yml`); use the same one so
  your lint matches.

Below, `$PY` is that Python: `PY=${ACCOUNTDECK_PYTHON:-"$HOME/Library/Application Support/AccountDeck2/runtime/python/bin/python3.14"}`.

## Checks, tests and demos

Run everything from the repository root.

```sh
bash tools/check.sh                                                      # the full gate
ACCOUNTDECK_SAFE_MODE=1 "$PY" -E -s -m unittest discover -s tests -t .   # the tests alone
"$PY" -E -s run.py --demo typical --port 0 --no-browser                  # a demo; prints its URL
"$PY" -E -s tools/menubar_preview.py --scenario typical                  # the real menu bar item on demo data
```

`tools/check.sh` runs lint, the unit tests in safe mode, a syntax check of the shell scripts, the
dashboard's JavaScript and the workflow files, every demo scenario's state against the contract, and the
shape of the `run.py --doctor` report. The JavaScript check needs `node` on your `PATH` and the workflow
check needs `ruby` (macOS has one); without them, check.sh reports that part as skipped (CI runs both). It
also checks that older releases can still open data this build used (see below) when their builds are at
hand; 2.0.0 and 2.1.0 were released before this repository's history begins, so in a clone of it check.sh
checks the available public releases and skips those two old builds. Extra arguments go to `unittest`, for example
`bash tools/check.sh -k test_vault`.

Pull requests also run `tools/upgrade_test.sh` (old versions installed and updated in a temporary folder) and
`tools/mac_package_test.sh` (the release package installs and `get.sh` installs it). Both install under a
temporary prefix, never over your own AccountDeck; the comment at the top of each says how to run it.

Demo scenarios include `typical`, `terminal`, `ultra`, `mixed`, `runway`, `resets-soon`, `outage`, `fifty`,
`first-run`, `update-available` (the update prompt, with a stand-in for GitHub), `antigravity` (a running `agy`,
an unsaved sign-in and one needing a new sign-in) and `antigravity-recover` (a switch waiting for Recover);
`accountdeck/fixtures.py` lists them all. A demo runs the real service on synthetic accounts in a temporary
folder; its `agy` is a stand-in too.

## Safety rules

- **Demo and safe mode only.** Tests and demos run with `ACCOUNTDECK_SAFE_MODE=1` on synthetic home folders,
  fake providers, a fake process table and temporary keychains. They never touch real apps, files, Keychain
  items or providers. Keep it that way in every test you write.
- **Never switch a real account while developing.** If AccountDeck is installed on your Mac, its service
  (port 53157) manages your real Claude Code, Claude Desktop, Codex and `agy` logins, including the one an
  editor or coding agent on this code may be using. Switching for real changes which account that session
  runs on.
- **Never run `agy` or touch its sign-in while developing.** Do not run `agy`, read or write its Keychain item
  (service `gemini`, account `antigravity`) or open anything under `~/.gemini/`. Use the fakes
  (`FakeAntigravity`, the fake process controller) and synthetic home folders; safe mode refuses the login
  keychain and real processes.
- **Never install over your own AccountDeck to test a change.** Run `install.sh`, `uninstall.sh` and `get.sh`
  with `--prefix DIR` or `--dry-run` while developing. `install.sh --prefix DIR` installs below `DIR` and
  never opens the app (it implies `--no-launch`), which is what makes it safe for testing.
- **Never print tokens, cookies or secret-bearing URLs**, in code, logs, tests, issues or pull requests. The
  dashboard URL carries a session token after `#token=`.
- **No new runtime packages.** Python 3.14 and `cryptography` only; the dashboard has no build step and loads
  nothing from a CDN.

## Test policy

Keep the suite small and important. Add a test only where a regression would lose a sign-in, corrupt data or
switch the wrong account, and prefer extending an existing test file to adding a new one. Tests must run in a
few seconds and in safe mode.

## Data compatibility

Updating AccountDeck must never lose a saved account. An existing install finds its accounts again only because
these values have not changed since the first release:

| What | Value |
|---|---|
| App name, executable | `AccountDeck 2`, `AccountDeck2` |
| Bundle id and LaunchAgent label | `local.accountdeck2` |
| Support folder (data, `app/`, `runtime/`, `app-backups/`) | `~/Library/Application Support/AccountDeck2` |
| App bundle | `~/Applications/AccountDeck 2.app` |
| Vault key in the login keychain | service `AccountDeck2 vault`, account = first 16 hex digits of sha256(resolved data folder path) |
| Vault records | `vault/*.dpapi`: `ADK1` + nonce + AES-256-GCM (the extension is a leftover name; it stays) |
| Database | `accounts.sqlite3`, schema 2 (`PRAGMA user_version`); a newer schema is refused, never downgraded |
| Service | port 53157, `runtime.json`, `/_health`, `/api/session`, `/api/state`, `/api/stop` as `install.sh` uses them (it must keep talking to 2.0.0 and 2.1.0) |
| Owner lock | `~/Library/Application Support/AccountDeck2-Owner.lock` |

Antigravity's data is kept apart from the other apps' because an older build stops reading every account when it
meets an account of an app it does not know, and stops every switch while it finds an unfinished `transaction-`
record. Its names are frozen in the same way from the release that ships them (2.2.0):

| What | Value |
|---|---|
| Accounts | the table `antigravity_accounts` in `accounts.sqlite3` (still schema 2), its columns as `core.py` creates it; never a row in `accounts` |
| Vault records | `agy-<32 hex account id>` (a saved sign-in: `{"v": 1, "email", "saved_at", "stores": {"cli": …}}`), `agy-transaction-<32 hex>` (a switch under way), `agy-enrollment` (an Add under way); never a bare `<32 hex>` or `transaction-` name |
| App id | `antigravity`: the app's name in the state and the API. The vault records above carry no app id; their names tell them apart |
| Settings | `agy_usage_on_open`, `agy_notice_seen` |

An installed copy can update itself only because its own `get.sh` finds each new release where it expects it.
These are frozen in the same way:

| What | Value |
|---|---|
| Release assets | `AccountDeck2-mac.tar.gz`, `AccountDeck2-mac.tar.gz.sha256` and `get.sh`, under `releases/latest/download/` and `releases/download/vX.Y.Z/` |
| Checksum file | one line: the SHA-256 in hex, two spaces, `AccountDeck2-mac.tar.gz` |
| Package layout | one top folder `AccountDeck2-mac/` holding `install.sh` and `accountdeck/__init__.py` with its `__version__` line |
| Versions | tags `vX.Y.Z`, which `get.sh --version X.Y.Z` (what **Update…** runs) downloads |

A change to any of these needs a migration, a test and the maintainer's agreement before it is merged. These
hold the promise:

- `tests/test_data_compat.py`: a data folder saved by each released version (`tests/fixtures/`, made by
  `tests/fixtures/make_baseline_data.py`) loads in this build with every account, secret, setting and import
  record, and the schema stays as it was but for Antigravity's table, added after a backup. In reverse,
  the old builds named by `ACCOUNTDECK_BASELINES` (checkouts, `:`-separated; `tools/check.sh` makes them from
  the tags `v2.0.0`, `v2.1.0` and `v2.2.0` where a checkout has them; the first two releases
  predate this repository's history) still open the folder after this build used it, with every kind of
  Antigravity data in it. Pre-2.2 builds leave that data alone; newer builds preserve its saved sign-ins
  while recovering any unfinished operations, which is what a rollback needs.
- A frozen-identifiers test: the values above that live in code, with a message that says what changing one
  costs.
- `tools/upgrade_test.sh`: each old version installed with saved data, this build installed over it, then the
  data files must be unchanged byte for byte and every account and secret readable; an unhealthy update must
  roll back with the data untouched, an update that loses a saved account or sign-in (an Antigravity one
  included) must roll back too, and a downgrade must be refused.

When a release starts writing something new to the data folder, add a data fixture for it, made with that
release's own code once its tag is pushed. For 2.2.0, the first release with Antigravity's data, the recipe is
ready:

```sh
mkdir /tmp/ad-2.2.0 && git archive v2.2.0 | tar -x -C /tmp/ad-2.2.0
ACCOUNTDECK_SAFE_MODE=1 "$PY" -E -s tests/fixtures/make_baseline_data.py /tmp/ad-2.2.0   # writes tests/fixtures/baseline-data-2.2.0.zip
```

Then add the release to `RELEASES` in `tests/test_data_compat.py`, to the old builds `tools/check.sh` makes from
tags and to the updates `tools/upgrade_test.sh` runs, so that every later build is checked against its data and
its rollback.

## Pull requests

- One change per pull request, with a description of what changed, why, and how you tested it.
- `bash tools/check.sh` passes.
- Tested in demo or safe mode only.
- No frozen identifier or data format changed, or the migration, its test and a data fixture are included.
- A line in `CHANGELOG.md` under `## [Unreleased]` at the top (add that heading if it is missing), written for
  users.
- Plain words in messages and docs; comments say why, not what.

The [pull request template](.github/PULL_REQUEST_TEMPLATE.md) repeats this list.

## Releasing

For maintainers.

1. Set `__version__` in `accountdeck/__init__.py` to the new version `X.Y.Z`.
2. Turn `## [Unreleased]` in `CHANGELOG.md` into `## [X.Y.Z] - YYYY-MM-DD`. The release notes are cut from
   that section.
3. Merge to `main` once CI passes.
4. Tag the merged commit and push the tag: `git tag vX.Y.Z && git push origin vX.Y.Z`. The tag must be `v`
   followed by `__version__`.
5. The release workflow (`.github/workflows/release.yml`) builds and tests the package again, then publishes
   three assets on the GitHub release: `AccountDeck2-mac.tar.gz`, `AccountDeck2-mac.tar.gz.sha256` and
   `get.sh`. The one-line install downloads that `get.sh`, which installs the latest release from there.

A published release reaches every user: within minutes each running installed copy learns of it from GitHub
(`accountdeck/updates.py`) and offers it in its update prompt, and **Update…** installs it with `get.sh`. A draft
or a prerelease is never offered, by the prompt or by `get.sh`. Commits on `main` between releases reach nobody
this way.

The one-line install downloads `get.sh` from the latest release, not from `main`, so a merged change to
`get.sh` reaches new users with the next release, like every other change.

A fork that publishes its own releases changes `REPO=` at the top of `get.sh` and `REPO` in
`accountdeck/updates.py` together; `tests/test_updates.py` checks that they match.
