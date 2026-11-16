#!/bin/bash
# Proves that installing this build over an older AccountDeck 2 keeps every saved account, and that install.sh's
# update guards hold. Hermetic: everything runs below a temporary folder, with a temporary HOME and TMPDIR and in
# safe mode; the real home, the live service, launchd and the Keychain are never touched.
#
#     tools/upgrade_test.sh [--downloads DIR]
#
# Part 1, under --prefix (no service). For each older release (the tree of the tag vX.Y.Z when it resolves, installed
# with its own install.sh; otherwise this tree with its version line rewritten): install the old version, put a
# released version's data folder in place (tests/fixtures/*X.Y.Z*.zip when there is one, else baseline-data.zip,
# which 2.0.0 wrote), hash every file of the data folder (all but the installer's and a service's own), install this
# build over it and require: the data byte for byte as it was, this version in app/, the old app in app-backups/, a
# snapshot in update-backups/ equal to vault/ and the database, and this build's code on the installed runtime
# reading every account row and every saved record with the fixture's key (no Keychain). Then on the 2.1.0 folder: a
# second run changes nothing; --prefix without --no-launch calls neither open nor launchctl (logging fakes first on
# PATH); an install while another holds the lock stops with nothing changed, a lock whose holder is gone does not
# block, and a lock that cannot be made is named as such; 2.10.0 counts as newer than 2.2.0; going back is refused without --allow-downgrade (and --dry-run says so)
# and done with it; uninstall.sh removes app-backups/ and keeps the data and update-backups/.
# Part 2, with ACCOUNTDECK2_INSTALL_ROOT, a fake launchctl and open (tools/upgrade/bin) and a stub service
# (tools/upgrade/stub_service.py): a busy service is refused with nothing changed (2.1's /_health, and 2.0's, where
# only /api/state tells), an unhealthy new version is rolled back with the data untouched and the old service
# answering again, a new version that loses a saved sign-in is rolled back and the message names the snapshot, a
# healthy update succeeds and prints the census, and a downgrade is refused while the service runs. Then, with two
# Antigravity accounts saved by this build in the folder: an update keeps them and counts them (--dry-run too), and a
# new version that loses one (its row and its record) is rolled back.
#
# Prints one PASS or FAIL line per case and exits 1 when any failed. --downloads DIR is passed to every install
# (the pinned runtime files, still SHA-256 checked); without it each install downloads them (about 30 MB).
# UPGRADE_TEST_KEEP=1 keeps the temporary folder for a look afterwards.
set -euo pipefail
export COPYFILE_DISABLE=1

REPO=$(cd "$(dirname "$0")/.." && pwd -P)
HERE="$REPO/tools/upgrade"
DL=()
while [ $# -gt 0 ]; do
    case "$1" in
    --downloads) [ $# -ge 2 ] && [ -d "$2" ] || { echo "--downloads needs a folder" >&2; exit 2; }
                 DL=(--downloads "$(cd "$2" && pwd -P)"); shift ;;
    *) echo "usage: tools/upgrade_test.sh [--downloads DIR]" >&2; exit 2 ;;
    esac
    shift
done
version_in() { sed -n 's/^__version__ = "\([^"]*\)".*$/\1/p' "$1/accountdeck/__init__.py"; }
NEW=$(version_in "$REPO")
[ -n "$NEW" ] || { echo "no version in accountdeck/__init__.py" >&2; exit 2; }
KEY=$(printf %s accountdeck-test-vault-key | shasum -a 256 | cut -d ' ' -f 1)   # the fixtures' vault key
LOST_KEY=""

T=$(mktemp -d "${TMPDIR:-/tmp}/accountdeck-upgrade-test-XXXXXX")
T=$(cd "$T" && pwd -P)              # install.sh resolves its folders, and SERVICE_MATCH must match the stub's arguments
mkdir "$T/home" "$T/tmp" "$T/pids" "$T/logs" "$T/fakes" "$T/holder"
PASSED=0 FAILED=0 HOLDER=""
# Logging stand-ins for open and launchctl (part 1's --prefix case): each writes down its call and does nothing else.
for tool in open launchctl; do
    printf '#!/bin/bash\nprintf "%%s %%s\\n" "${0##*/}" "$*" >> %q\n' "$T/fakes/called" > "$T/fakes/$tool"
    chmod 755 "$T/fakes/$tool"
done
# This build's code on an installed runtime reading a data folder: every account row and every saved record
# (quarantined bytes are kept unreadable on purpose), with the fixture key set directly: no Keychain access.
cat > "$T/read_check.py" <<'PYTHON'
import os, re, sys
from pathlib import Path
sys.path.insert(0, os.getcwd())                         # the installed app folder: run from there
from accountdeck.core import Store, Vault
from accountdeck.platform.keychain import VaultCipher
data = Path(sys.argv[1])
cipher = VaultCipher(data)
cipher._key = bytes.fromhex(sys.argv[2])              # the fixture's key; no Keychain access
vault, store = Vault(data / "vault", protect=cipher), Store(data)
rows = store.accounts()
read = 0
for path in sorted((data / "vault").glob("*.dpapi")):
    name = path.name[: -len(".dpapi")]
    if name.startswith("quarantine-"):                  # unreadable bytes kept for review, by design
        continue
    found = re.fullmatch(r"(.+)\.g(\d+)", name)
    vault.get(found.group(1), int(found.group(2))) if found else vault.get(name)
    read += 1
missing = [row["id"] for row in rows if row["owner"] == "v2" and not row["flags"].get("deferred_desktop")
           and not (data / "vault" / (row["id"] + ".dpapi")).exists()]
missing.extend(row["id"] for row in store.agy_accounts()
               if not (data / "vault" / ("agy-" + row["id"] + ".dpapi")).exists())
assert not missing, "accounts without their saved sign-in: %s" % missing
print(len(rows), read)
PYTHON
# This build's code saves two Antigravity accounts in a data folder (their own table, agy- records with an older copy
# each), with the fixture key set directly: no Keychain access.
cat > "$T/agy_data.py" <<'PYTHON'
import os, sys
from pathlib import Path
sys.path.insert(0, os.getcwd())                         # this build's code: run from the checkout
from accountdeck.core import Store, Vault, agy_key
from accountdeck.platform.keychain import VaultCipher
from accountdeck.providers.antigravity import saved_record
data = Path(sys.argv[1])
cipher = VaultCipher(data)
cipher._key = bytes.fromhex(sys.argv[2])              # the fixture's key; no Keychain access
vault, store = Vault(data / "vault", protect=cipher), Store(data)
for email in ("ada@example.test", "bob@example.test"):
    key = store.agy_add(email, saved_at=1.0)
    for saved in (1.0, 2.0):
        vault.put(agy_key(key), saved_record(email, {"token": {"refresh_token": "synthetic"}, "id_token_email": email},
                                             saved_at=saved))
print(len(store.agy_accounts()))
store.close()
PYTHON

stop_stubs() {   # only stubs this test started (their pid files), each checked by its command line first
    local file pid waited
    for file in "$T/pids"/*; do
        [ -f "$file" ] || continue
        pid=${file##*/}
        case "$(ps -p "$pid" -o command= 2>/dev/null || true)" in
        *"$HERE/stub_service.py"*) kill "$pid" 2>/dev/null || true ;;
        *) rm -f "$file"; continue ;;
        esac
        waited=0
        while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 50 ]; do sleep 0.1; waited=$((waited + 1)); done
        rm -f "$file"
    done
}

stop_holder() {   # the stand-in lock holder of part1_lock, checked by its command line first
    if [ -n "$HOLDER" ]; then
        case "$(ps -p "$HOLDER" -o command= 2>/dev/null || true)" in
        *"$T/holder/install.sh"*) kill "$HOLDER" 2>/dev/null || true; wait "$HOLDER" 2>/dev/null || true ;;
        esac
        HOLDER=""
    fi
}

finish() {
    local status=$?
    trap - EXIT
    stop_stubs
    stop_holder
    if [ "${UPGRADE_TEST_KEEP:-0}" = 1 ]; then echo "kept: $T"; else rm -rf "$T"; fi
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# The logging fakes come first on PATH for everything here (part 2 puts its own in front): no real open or launchctl
# ever runs, even when an install under test would call one.
hermetic() { env PATH="$T/fakes:$PATH" HOME="$T/home" TMPDIR="$T/tmp" ACCOUNTDECK_SAFE_MODE=1 UPGRADE_STUB_PIDS="$T/pids" "$@"; }
support() { printf '%s\n' "$1/Library/Application Support/AccountDeck2"; }
py() { "$(support "$1")/runtime/python/bin/python3.14" -I -B "${@:2}"; }   # py ROOT ARGS…: the root's runtime

why() { printf '%s\n' "$*" > "$T/why"; return 1; }
show() {   # show LOG: its end, for a failure
    if [ -f "$1" ]; then sed 's/^/    | /' "$1" | tail -n 25 >&2; fi
}
run() {   # run NAME FUNCTION [ARGS…]: one case, one line
    local name=$1 status=0
    shift
    rm -f "$T/why"
    "$@" || status=$?
    if [ "$status" = 0 ]; then
        echo "PASS: $name"
        PASSED=$((PASSED + 1))
    else
        echo "FAIL: $name: $(cat "$T/why" 2>/dev/null || echo "status $status")"
        FAILED=$((FAILED + 1))
    fi
}

copy_tree() {   # copy_tree DEST: this working tree without git, builds and dependencies
    mkdir -p "$1"
    (cd "$REPO" && tar -cf - --exclude .git --exclude dist --exclude node_modules --exclude __pycache__ .) | (cd "$1" && tar -xf -)
}

set_version() {   # set_version TREE VERSION
    sed -i '' "s/^__version__ = \"[^\"]*\"/__version__ = \"$2\"/" "$1/accountdeck/__init__.py"
    [ "$(version_in "$1")" = "$2" ]
}

old_tree() {   # old_tree VERSION DEST: prints where the old tree came from
    if command -v git >/dev/null 2>&1 && git -C "$REPO" rev-parse -q --verify "refs/tags/v$1^{commit}" >/dev/null 2>&1; then
        mkdir -p "$2"
        git -C "$REPO" archive "v$1" | tar -xf - -C "$2"
        echo "the tag v$1"
    else
        copy_tree "$2"
        set_version "$2" "$1"
        echo "this tree as $1 (no tag v$1 here)"
    fi
}

fixture_for() {   # the data folder a released version wrote: its own fixture when there is one
    local found
    for found in "$REPO"/tests/fixtures/*"$1"*.zip; do
        if [ -f "$found" ]; then echo "$found"; return 0; fi
    done
    echo "$REPO/tests/fixtures/baseline-data.zip"
}

install_old() {   # install_old ROOT TREE LOG: the old version with its own install.sh, under --prefix
    local args=(--prefix "$1" --no-launch)
    if grep -q -- '--skip-tests' "$2/install.sh"; then args+=(--skip-tests); fi
    hermetic bash "$2/install.sh" "${args[@]}" ${DL[@]+"${DL[@]}"} > "$3" 2>&1
}

data_hash() {   # data_hash ROOT: every file below the data folder, so every one an update must leave alone (the database,
                # vault/, the daily backups/, settings a version keeps beside them), but what the installer or a running
                # service owns: the code, the runtime, their backups, the snapshots, logs, the installer's work folder
                # and lock, runtime.json and service.lock (and the stub's runtime.json before its rename)
    local name
    (cd "$(support "$1")" && for name in * .[!.]* ..?*; do
        [ -e "$name" ] || continue
        case "$name" in
        app|app.*|app-backups|runtime|runtime.*|update-backups|logs|.install-*|.install.lock|.install.lock.guard|service.lock|.runtime.json.stub) ;;
        *) find "$name" -type f -print0 ;;
        esac
    done | LC_ALL=C sort -z | xargs -0 shasum -a 256)
}

app_hash() {   # app_hash ROOT: the installed app folder, every file
    (cd "$(support "$1")/app" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256 | shasum -a 256)
}

count_dirs() { find "$1" -mindepth 1 -maxdepth 1 -type d ! -name '.*' 2>/dev/null | wc -l | tr -d ' '; }
newest() { find "$1" -mindepth 1 -maxdepth 1 -type d -name "$2" 2>/dev/null | LC_ALL=C sort | tail -n 1; }

put_fixture() {   # put_fixture ROOT ZIP: the released data folder, as the old version left it
    local data
    data=$(support "$1")
    unzip -q -o "$2" -d "$data" || return 1
    [ -f "$data/accounts.sqlite3" ] || return 1
    chmod 700 "$data/vault"
}

rows_in() {   # rows_in ROOT: account rows in the data folder's database, read without changing it
    py "$1" -c 'import sqlite3, sys, urllib.parse
db = sqlite3.connect("file:%s?mode=ro&immutable=1" % urllib.parse.quote(sys.argv[1] + "/accounts.sqlite3"), uri=True)
total = db.execute("SELECT count(*) FROM accounts").fetchone()[0]
if sys.argv[2] == "all" and db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", ("antigravity_accounts",)).fetchone():
    total += db.execute("SELECT count(*) FROM antigravity_accounts").fetchone()[0]
print(total)' "$(support "$1")" "${2:-legacy}"
}

health_of() {   # health_of ROOT: the version the service answering for ROOT reports, or down
    py "$1" -c 'import json, sys, urllib.request
try:
    run = json.load(open(sys.argv[1] + "/runtime.json"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    print(json.load(opener.open("http://127.0.0.1:%d/_health" % run["port"], timeout=2))["version"])
except Exception:
    print("down")' "$(support "$1")"
}

start_stub() {   # start_stub ROOT [MODE]: as opening the app would, then waits (up to 10 s) until it answers
    local waited=0
    hermetic env PATH="$HERE/bin:$PATH" UPGRADE_STUB_MODE="${2-}" "$HERE/bin/open" "$1/Applications/AccountDeck 2.app" || return 1
    while [ "$(health_of "$1")" = down ]; do
        [ "$waited" -lt 100 ] || return 1
        sleep 0.1
        waited=$((waited + 1))
    done
}

stubs_running() {   # how many stubs serve ROOT's app folder
    local found
    found=$(pgrep -U "$(id -u)" -f -- "python3[^ ]* .*$(support "$1")/app/run\.py" 2>/dev/null || true)
    printf '%s\n' "$found" | grep -c . || true
}

update_in_root() {   # update_in_root ROOT TREE LOG [ARGS…]: install.sh of TREE in the test mode, with a service to restart
    local root=$1 tree=$2 log=$3
    shift 3
    hermetic env ACCOUNTDECK2_INSTALL_ROOT="$root" PATH="$HERE/bin:$PATH" \
        bash "$tree/install.sh" --skip-tests ${DL[@]+"${DL[@]}"} "$@" > "$log" 2>&1
}

said() { grep -qF -- "$2" "$1" || { show "$1"; why "it did not say: $2"; }; }

# ---------------------------------------------------------------- Part 1: updates under --prefix, no service

read_everything() {   # read_everything ROOT ROWS: this build's code on the installed runtime reads every row and record
    local data copy answer rows records
    data=$(support "$1")
    copy="$T/read-$(basename "$1")"
    mkdir "$copy" && cp -Rp "$data/accounts.sqlite3" "$data/vault" "$copy/" || return 1
    answer=$(cd "$data/app" && hermetic "$data/runtime/python/bin/python3.14" -E -s -B "$T/read_check.py" "$copy" "$KEY" \
             2>"$T/read.err" | tail -n 1) && [ -n "$answer" ] \
        || { why "this build could not read the data: $(tail -n 3 "$T/read.err" | tr '\n' ' ')"; return 1; }
    read -r rows records <<< "$answer"
    [ "$rows" = "$2" ] || why "this build read $rows account rows, expected $2" || return 1
    [ "$records" -gt 0 ] 2>/dev/null || why "this build read no saved record ($records)" || return 1
    echo "$rows account rows, $records saved records" > "$T/read-count"
}

part1_update() {   # part1_update OLD: install OLD, put its data in place, update to this build
    local old=$1 root="$T/p1-$1" tree="$T/src-$1" log="$T/logs/p1-$1" data rows total before snap backup origin fixture mode
    origin=$(old_tree "$old" "$tree") || { why "no $old tree"; return 1; }
    install_old "$root" "$tree" "$log.old" || { show "$log.old"; why "$old ($origin) did not install"; return 1; }
    [ "$(version_in "$(support "$root")/app")" = "$old" ] || why "the old install is not $old" || return 1
    fixture=$(fixture_for "$old")
    put_fixture "$root" "$fixture" || why "the fixture ${fixture##*/} could not be put in place" || return 1
    data=$(support "$root")
    rows=$(rows_in "$root") || why "the fixture's rows could not be counted" || return 1
    total=$(rows_in "$root" all) || return 1
    before=$(data_hash "$root")
    echo "    $old from $origin, data from ${fixture##*/} ($rows account rows)"

    hermetic bash "$REPO/install.sh" --prefix "$root" --no-launch --skip-tests ${DL[@]+"${DL[@]}"} > "$log" 2>&1 \
        || { show "$log"; why "the update from $old failed"; return 1; }
    said "$log" "Updating AccountDeck 2 from $old to $NEW" || return 1
    said "$log" "All $total saved accounts are still here" || return 1
    [ "$(data_hash "$root")" = "$before" ] || why "a data file changed" || return 1
    [ "$(version_in "$data/app")" = "$NEW" ] || why "app/ is not $NEW" || return 1
    backup=$(newest "$data/app-backups" "*-v$old")
    [ -n "$backup" ] && [ "$(version_in "$backup")" = "$old" ] || why "no $old app in app-backups/" || return 1
    snap=$(newest "$data/update-backups" "*-v$old")
    [ -n "$snap" ] || why "no snapshot in update-backups/" || return 1
    diff -r "$snap/vault" "$data/vault" >/dev/null || why "the snapshot's vault/ differs from vault/" || return 1
    mode=$( { stat -f '%Lp' "$data/update-backups" "$snap"; find "$snap" -mindepth 1 -type d -exec stat -f '%Lp' {} +; } | sort -u)
    [ "$mode" = 700 ] || why "a snapshot folder is not 0700 ($mode)" || return 1
    mode=$(find "$snap" -type f -exec stat -f '%Lp' {} + | sort -u)
    [ "$mode" = 600 ] || why "a snapshot file is not 0600 ($mode)" || return 1
    grep -qF "never restored automatically" "$snap/MANIFEST.txt" || why "MANIFEST.txt does not say it is never restored" || return 1
    gunzip -c "$snap/accounts.sqlite3.gz" > "$T/snap-$old.sqlite3" || why "the snapshot's database does not unpack" || return 1
    py "$root" -c 'import sqlite3, sys, urllib.parse
def dump(path):
    db = sqlite3.connect("file:%s?mode=ro&immutable=1" % urllib.parse.quote(path), uri=True)
    return list(db.iterdump()), db.execute("PRAGMA user_version").fetchone()[0]
assert dump(sys.argv[1]) == dump(sys.argv[2] + "/accounts.sqlite3")' "$T/snap-$old.sqlite3" "$data" \
        || why "the snapshot's database is not the database" || return 1
    read_everything "$root" "$rows" || return 1
    echo "    read with this build: $(cat "$T/read-count"); snapshot ${snap##*/}"
}

part1_second_run() {   # the same build again changes nothing
    local root="$T/p1-2.1.0" log="$T/logs/p1-again" data app before snaps
    data=$(support "$root")
    [ -d "$data/app" ] || why "no install to run again over" || return 1
    app=$(app_hash "$root"); before=$(data_hash "$root"); snaps=$(count_dirs "$data/update-backups")
    hermetic bash "$REPO/install.sh" --prefix "$root" --no-launch --skip-tests ${DL[@]+"${DL[@]}"} > "$log" 2>&1 \
        || { show "$log"; why "the second run failed"; return 1; }
    said "$log" "nothing needed changing" || return 1
    [ "$(app_hash "$root")" = "$app" ] || why "app/ changed" || return 1
    [ "$(data_hash "$root")" = "$before" ] || why "a data file changed" || return 1
    [ "$(count_dirs "$data/update-backups")" = "$snaps" ] || why "a snapshot was taken for no change"
}

part1_prefix_opens_nothing() {   # --prefix alone, without --no-launch: nothing is opened and launchd is not asked
    local root="$T/p1-2.1.0" log="$T/logs/p1-prefix" data app before
    data=$(support "$root")
    [ -d "$data/app" ] || why "no install to run again over" || return 1
    app=$(app_hash "$root"); before=$(data_hash "$root")
    rm -f "$T/fakes/called"
    # A real open here would start the code under test on the real home's data: the fakes only write down their call.
    hermetic bash "$REPO/install.sh" --prefix "$root" --skip-tests ${DL[@]+"${DL[@]}"} > "$log" 2>&1 \
        || { show "$log"; why "install.sh --prefix without --no-launch failed"; return 1; }
    [ ! -e "$T/fakes/called" ] || why "it called $(tr '\n' ';' < "$T/fakes/called")" || return 1
    [ "$(app_hash "$root")" = "$app" ] && [ "$(data_hash "$root")" = "$before" ] || why "something changed"
}

part1_lock() {   # an install while another holds the lock stops with nothing changed; a lock nobody holds does not block
    local root="$T/p1-2.1.0" log="$T/logs/p1-lock" data app before listing status=0 sleeper
    data=$(support "$root")
    [ -d "$data/app" ] || why "no install to work on" || return 1
    # The first install, as the lock sees it: a bash running an install.sh, alive, its pid in the lock. Ended, it ends
    # its sleep too.
    printf '%s\n' 'trap '"'"'kill "$child" 2>/dev/null; exit 0'"'"' TERM' 'sleep 300 &' 'child=$!' 'wait "$child"' \
        > "$T/holder/install.sh"
    bash "$T/holder/install.sh" &
    HOLDER=$!
    (set -C; printf '%s\n' "$HOLDER" > "$data/.install.lock") || why "the test could not take the lock" || return 1
    app=$(app_hash "$root"); before=$(data_hash "$root"); listing=$(ls -A "$data")
    hermetic bash "$REPO/install.sh" --prefix "$root" --no-launch --skip-tests ${DL[@]+"${DL[@]}"} > "$log" 2>&1 || status=$?
    [ "$status" = 1 ] || { show "$log"; why "an install while the lock is held returned $status, expected 1"; return 1; }
    said "$log" "another install or update of AccountDeck 2 is running. Let it finish, then run install.sh again. Nothing was changed." \
        || return 1
    [ "$(app_hash "$root")" = "$app" ] && [ "$(data_hash "$root")" = "$before" ] && [ "$(ls -A "$data")" = "$listing" ] \
        || why "the refused install changed something" || return 1
    [ "$(cat "$data/.install.lock")" = "$HOLDER" ] || why "the refused install took or removed the other one's lock" || return 1
    stop_holder
    [ -s "$data/.install.lock" ] || why "the stale lock is not there for the next install to face" || return 1
    # Its holder is gone (a killed install): the lock is stale, and the next install takes it, then frees it.
    hermetic bash "$REPO/install.sh" --prefix "$root" --no-launch --skip-tests ${DL[@]+"${DL[@]}"} > "$log.stale" 2>&1 \
        || { show "$log.stale"; why "a dead holder's lock blocked the install"; return 1; }
    said "$log.stale" "nothing needed changing" || return 1
    [ ! -e "$data/.install.lock" ] || why "the install did not free its lock" || return 1
    # A pid that now belongs to something else than an install (reused after a restart) holds nothing either.
    sleep 300 &
    sleeper=$!
    (set -C; printf '%s\n' "$sleeper" > "$data/.install.lock") || why "the test could not take the lock" || return 1
    status=0
    hermetic bash "$REPO/install.sh" --prefix "$root" --no-launch --skip-tests ${DL[@]+"${DL[@]}"} > "$log.reused" 2>&1 || status=$?
    kill "$sleeper" 2>/dev/null || true
    wait "$sleeper" 2>/dev/null || true
    [ "$status" = 0 ] || { show "$log.reused"; why "a lock holding a pid that is no install blocked the install"; return 1; }
    [ ! -e "$data/.install.lock" ] && [ "$(app_hash "$root")" = "$app" ] && [ "$(data_hash "$root")" = "$before" ] \
        || why "the lock was not freed, or something changed" || return 1
    # A lock that cannot be made (here a folder is in its way) is said to be that, not another install running.
    mkdir "$data/.install.lock"
    status=0
    hermetic bash "$REPO/install.sh" --prefix "$root" --no-launch --skip-tests ${DL[@]+"${DL[@]}"} > "$log.blocked" 2>&1 || status=$?
    rmdir "$data/.install.lock"
    [ "$status" = 1 ] || { show "$log.blocked"; why "an install whose lock could not be made returned $status, expected 1"; return 1; }
    said "$log.blocked" "/.install.lock could not be made (" || return 1
    [ "$(app_hash "$root")" = "$app" ] && [ "$(data_hash "$root")" = "$before" ] \
        || why "the install whose lock could not be made changed something" || return 1
    hermetic "$data/runtime/python/bin/python3.14" -I "$HERE/lock_check.py" "$REPO/install.sh" \
        || why "the installer lock allowed concurrent updates or failed to recover after a crash"
}

part1_downgrade() {   # 2.10.0 is newer than 2.2.0; going back needs --allow-downgrade
    local root="$T/p1-2.1.0" tree="$T/src-2.10.0" log="$T/logs/p1-downgrade" data app before snaps backups status=0
    data=$(support "$root")
    [ -d "$data/app" ] || why "no install to work on" || return 1
    copy_tree "$tree" && set_version "$tree" 2.10.0 || why "no 2.10.0 tree" || return 1
    hermetic bash "$tree/install.sh" --prefix "$root" --no-launch --skip-tests ${DL[@]+"${DL[@]}"} > "$log.up" 2>&1 \
        || { show "$log.up"; why "2.10.0 over $NEW did not install (taken for a downgrade?)"; return 1; }
    said "$log.up" "Updating AccountDeck 2 from $NEW to 2.10.0" || return 1
    app=$(app_hash "$root"); before=$(data_hash "$root")
    snaps=$(count_dirs "$data/update-backups"); backups=$(count_dirs "$data/app-backups")
    hermetic bash "$REPO/install.sh" --prefix "$root" --no-launch --skip-tests ${DL[@]+"${DL[@]}"} > "$log" 2>&1 || status=$?
    [ "$status" = 1 ] || { show "$log"; why "going back to $NEW without --allow-downgrade returned $status"; return 1; }
    said "$log" "--allow-downgrade" || return 1
    said "$log" "Nothing was changed." || return 1
    [ "$(app_hash "$root")" = "$app" ] && [ "$(data_hash "$root")" = "$before" ] \
        && [ "$(count_dirs "$data/update-backups")" = "$snaps" ] && [ "$(count_dirs "$data/app-backups")" = "$backups" ] \
        || why "the refused downgrade changed something" || return 1
    status=0
    hermetic bash "$REPO/install.sh" --prefix "$root" --dry-run > "$log.dry" 2>&1 || status=$?
    [ "$status" = 1 ] || { show "$log.dry"; why "--dry-run of the downgrade returned $status"; return 1; }
    said "$log.dry" "refused: the installed version 2.10.0 is newer than $NEW" || return 1
    hermetic bash "$REPO/install.sh" --prefix "$root" --no-launch --skip-tests --allow-downgrade ${DL[@]+"${DL[@]}"} \
        > "$log.allowed" 2>&1 || { show "$log.allowed"; why "--allow-downgrade did not install"; return 1; }
    said "$log.allowed" "Downgrading AccountDeck 2 from 2.10.0 to $NEW (--allow-downgrade)" || return 1
    [ "$(version_in "$data/app")" = "$NEW" ] || why "app/ is not $NEW after --allow-downgrade" || return 1
    [ "$(data_hash "$root")" = "$before" ] || why "a data file changed"
}

part1_uninstall() {   # uninstall.sh (the installed copy): the program and app-backups/ go, the data and update-backups/ stay
    local root="$T/p1-2.1.0" log="$T/logs/p1-uninstall" data before snaps
    data=$(support "$root")
    [ -d "$data/app-backups" ] && [ -d "$data/update-backups" ] || why "nothing to uninstall" || return 1
    before=$(data_hash "$root"); snaps=$(count_dirs "$data/update-backups")
    hermetic bash "$data/app/uninstall.sh" --prefix "$root" > "$log" 2>&1 || { show "$log"; why "uninstall.sh failed"; return 1; }
    [ ! -e "$data/app" ] && [ ! -e "$data/runtime" ] && [ ! -e "$data/app-backups" ] \
        && [ ! -e "$root/Applications/AccountDeck 2.app" ] || why "the program or app-backups/ is still there" || return 1
    [ "$(data_hash "$root")" = "$before" ] || why "a data file changed" || return 1
    [ "$(count_dirs "$data/update-backups")" = "$snaps" ] || why "update-backups/ was not kept" || return 1
    said "$log" "update-backups"
}

# ---------------------------------------------------------------- Part 2: updates with a running (stub) service

part2_setup() {   # part2_setup OLD ROOT: OLD installed, its data in place, its service running
    local old=$1 root=$2 tree="$T/src-$1" origin
    if [ -d "$tree" ]; then origin="the same $old tree as in part 1"; else origin=$(old_tree "$old" "$tree") || return 1; fi
    install_old "$root" "$tree" "$T/logs/p2-$old.old" || { show "$T/logs/p2-$old.old"; why "$old did not install"; return 1; }
    put_fixture "$root" "$(fixture_for "$old")" || why "the fixture could not be put in place" || return 1
    start_stub "$root" || why "the $old stub did not answer" || return 1
    [ "$(health_of "$root")" = "$old" ] || why "the stub does not answer as $old" || return 1
    echo "    $old from $origin, its service running (stub)"
}

stop_stub_of() {   # stop_stub_of ROOT: the stubs this test started for ROOT
    local file pid waited
    for file in "$T/pids"/*; do
        [ -f "$file" ] || continue
        pid=${file##*/}
        case "$(ps -p "$pid" -o command= 2>/dev/null || true)" in
        *"$HERE/stub_service.py"*"$(support "$1")/app/run.py"*)
            kill "$pid" 2>/dev/null || true
            waited=0
            while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 50 ]; do sleep 0.1; waited=$((waited + 1)); done
            rm -f "$file" ;;
        esac
    done
}

part2_busy() {   # part2_busy ROOT OLD: a service in the middle of a switch makes the update stop with nothing changed
    local root=$1 old=$2 log="$T/logs/p2-busy-$2" data app before status=0
    data=$(support "$root")
    stop_stub_of "$root"
    start_stub "$root" busy || why "the busy stub did not answer" || return 1
    app=$(app_hash "$root"); before=$(data_hash "$root")
    update_in_root "$root" "$REPO" "$log" || status=$?
    [ "$status" = 1 ] || { show "$log"; why "install.sh returned $status, expected 1"; return 1; }
    said "$log" "in the middle of a switch" || return 1
    said "$log" "Nothing was changed." || return 1
    [ "$(app_hash "$root")" = "$app" ] && [ "$(data_hash "$root")" = "$before" ] \
        && [ ! -e "$data/app-backups" ] && [ ! -e "$data/update-backups" ] || why "something changed" || return 1
    [ "$(health_of "$root")" = "$old" ] && [ "$(stubs_running "$root")" = 1 ] || why "the busy service is not running as it was" || return 1
    stop_stub_of "$root"
    start_stub "$root" || why "the stub did not come back" || return 1
}

part2_unhealthy() {   # a new version that never gets healthy is rolled back; the old one answers again
    local root=$1 old=$2 tree="$T/new-unhealthy" log="$T/logs/p2-unhealthy" app before status=0
    copy_tree "$tree" && printf '{"mode": "unhealthy"}\n' > "$tree/upgrade-stub.json" || why "no tree" || return 1
    app=$(app_hash "$root"); before=$(data_hash "$root")
    update_in_root "$root" "$tree" "$log" || status=$?
    [ "$status" = 1 ] || { show "$log"; why "install.sh returned $status, expected 1"; return 1; }
    said "$log" "failed its health check: it reports stalled" || return 1
    said "$log" "The previous version ($old) is running again." || return 1
    [ "$(app_hash "$root")" = "$app" ] || why "the previous app is not back as it was" || return 1
    [ "$(data_hash "$root")" = "$before" ] || why "a data file changed" || return 1
    [ "$(health_of "$root")" = "$old" ] && [ "$(stubs_running "$root")" = 1 ] || why "the old service does not answer" || return 1
}

part2_lost_record() {   # a new version that loses a saved sign-in is rolled back, and the message names the snapshot
    local root=$1 old=$2 tree="$T/new-lose" log="$T/logs/p2-lose" data app before snap lost status=0
    data=$(support "$root")
    copy_tree "$tree" && printf '{"mode": "lose-record"}\n' > "$tree/upgrade-stub.json" || why "no tree" || return 1
    app=$(app_hash "$root"); before=$(data_hash "$root")
    update_in_root "$root" "$tree" "$log" || status=$?
    [ "$status" = 1 ] || { show "$log"; why "install.sh returned $status, expected 1"; return 1; }
    said "$log" "lost saved accounts: 1 saved sign-in record in vault/" || return 1
    said "$log" "The previous version ($old) is running again." || return 1
    said "$log" "The saved sign-ins were not restored automatically" || return 1
    snap=$(newest "$data/update-backups" "*-v$old")
    [ -n "$snap" ] || why "no snapshot" || return 1
    said "$log" "$snap" || return 1
    [ "$(app_hash "$root")" = "$app" ] && [ "$(health_of "$root")" = "$old" ] || why "the previous version is not back" || return 1
    lost=$(find "$root/lost-records" -type f -name '*.dpapi' | head -n 1)
    [ -n "$lost" ] && [ ! -e "$data/vault/${lost##*/}" ] || why "the stub did not lose a record" || return 1
    cmp -s "$lost" "$snap/vault/${lost##*/}" || why "the snapshot does not hold the lost record" || return 1
    cp -p "$snap/vault/${lost##*/}" "$data/vault/"                   # what the docs tell the owner to do
    [ "$(data_hash "$root")" = "$before" ] || why "the data is not as before once the record is back from the snapshot"
}

part2_healthy() {   # part2_healthy ROOT OLD: the update goes through, with the census
    local root=$1 old=$2 log="$T/logs/p2-healthy-$2" data before rows snap
    data=$(support "$root")
    before=$(data_hash "$root"); rows=$(rows_in "$root")
    update_in_root "$root" "$REPO" "$log" || { show "$log"; why "the update failed"; return 1; }
    said "$log" "Updating AccountDeck 2 from $old to $NEW" || return 1
    said "$log" "Healthy: version $NEW answers on /_health" || return 1
    said "$log" "All $rows saved accounts are still here" || return 1
    [ "$(health_of "$root")" = "$NEW" ] && [ "$(stubs_running "$root")" = 1 ] || why "the service does not answer as $NEW" || return 1
    [ "$(version_in "$data/app")" = "$NEW" ] || why "app/ is not $NEW" || return 1
    [ "$(version_in "$(newest "$data/app-backups" "*-v$old")")" = "$old" ] || why "no $old app in app-backups/" || return 1
    snap=$(newest "$data/update-backups" "*-v$old")
    [ -n "$snap" ] && diff -r "$snap/vault" "$data/vault" >/dev/null || why "the newest snapshot is not the vault" || return 1
    [ "$(data_hash "$root")" = "$before" ] || why "a data file changed"
}

part2_downgrade() {   # with the service running, an older package changes nothing
    local root=$1 tree="$T/src-2.1.9" log="$T/logs/p2-downgrade" app before status=0
    copy_tree "$tree" && set_version "$tree" 2.1.9 || why "no 2.1.9 tree" || return 1
    app=$(app_hash "$root"); before=$(data_hash "$root")
    update_in_root "$root" "$tree" "$log" || status=$?
    [ "$status" = 1 ] || { show "$log"; why "install.sh returned $status, expected 1"; return 1; }
    said "$log" "is version $NEW, newer than this one (2.1.9). Nothing was changed." || return 1
    [ "$(app_hash "$root")" = "$app" ] && [ "$(data_hash "$root")" = "$before" ] || why "something changed" || return 1
    [ "$(health_of "$root")" = "$NEW" ] || why "the service is not running as it was"
}

part2_agy_kept() {   # part2_agy_kept ROOT: with two Antigravity accounts saved, an update keeps them and counts them
    local root=$1 tree="$T/new-agy" log="$T/logs/p2-agy" data before total signins
    data=$(support "$root")
    copy_tree "$tree" && printf '{"mode": "healthy"}\n' > "$tree/upgrade-stub.json" || why "no tree" || return 1
    signins=$(ls "$data/vault" | grep -cE '^[0-9a-f]{32}\.dpapi$' || true)
    (cd "$REPO" && hermetic "$data/runtime/python/bin/python3.14" -E -s -B "$T/agy_data.py" "$data" "$KEY") \
        > "$T/agy.out" 2>&1 && [ "$(tail -n 1 "$T/agy.out")" = 2 ] \
        || { show "$T/agy.out"; why "this build did not save two Antigravity accounts"; return 1; }
    total=$(( $(rows_in "$root") + 2 )); signins=$((signins + 2))
    before=$(data_hash "$root")
    hermetic bash "$tree/install.sh" --prefix "$root" --dry-run > "$log.dry" 2>&1 \
        || { show "$log.dry"; why "--dry-run failed"; return 1; }
    said "$log.dry" "Accounts: $total saved accounts (" || return 1
    said "$log.dry" ", 2 Antigravity, with $signins saved sign-ins) now" || return 1
    update_in_root "$root" "$tree" "$log" || { show "$log"; why "the update failed"; return 1; }
    said "$log" "All $total saved accounts are still here: " || return 1
    said "$log" ", 2 Antigravity, with $signins saved sign-ins." || return 1
    [ "$(health_of "$root")" = "$NEW" ] && [ "$(stubs_running "$root")" = 1 ] || why "the service does not answer as $NEW" || return 1
    [ "$(data_hash "$root")" = "$before" ] || why "a data file changed"
}

part2_agy_lost() {   # part2_agy_lost ROOT: a new version that loses an Antigravity account is rolled back
    local root=$1 tree="$T/new-agy-lose" log="$T/logs/p2-agy-lose" data app before previous gone status=0
    data=$(support "$root")
    copy_tree "$tree" && printf '{"mode": "lose-antigravity"}\n' > "$tree/upgrade-stub.json" || why "no tree" || return 1
    app=$(app_hash "$root"); before=$(data_hash "$root"); previous=$(health_of "$root")
    update_in_root "$root" "$tree" "$log" || status=$?
    [ "$status" = 1 ] || { show "$log"; why "install.sh returned $status, expected 1"; return 1; }
    gone=$(ls "$root/lost-antigravity" 2>/dev/null | grep -E '^agy-[0-9a-f]{32}\.dpapi$' || true)
    [ -n "$gone" ] || why "the stub did not lose an Antigravity account" || return 1
    said "$log" "lost saved accounts: 1 of 2 Antigravity accounts; 1 saved sign-in record in vault/ ($gone)" || return 1
    said "$log" "The previous version ($previous) is running again." || return 1
    [ "$(app_hash "$root")" = "$app" ] && [ "$(health_of "$root")" = "$previous" ] || why "the previous version is not back" || return 1
    cp -p "$root/lost-antigravity/accounts.sqlite3" "$data/" && cp -p "$root/lost-antigravity"/agy-*.dpapi "$data/vault/" \
        || why "the lost account could not be put back" || return 1
    [ "$(data_hash "$root")" = "$before" ] || why "the data is not as before once the account is back"
}

echo "AccountDeck 2 upgrade test: this build is $NEW (temporary folder $T)"
echo "Part 1: updates under --prefix"
for old in 2.0.0 2.1.0 2.2.0; do
    [ "$old" != "$NEW" ] || continue
    run "update $old -> $NEW keeps every account (data unchanged, backups, snapshot, all rows and records read)" part1_update "$old"
done
run "a second run of the same build changes nothing" part1_second_run
run "--prefix without --no-launch opens nothing and does not ask launchd" part1_prefix_opens_nothing
run "an install while another holds the lock stops with nothing changed; a stale lock does not block" part1_lock
run "2.10.0 is newer than $NEW; going back is refused (also by --dry-run) unless --allow-downgrade" part1_downgrade
run "uninstall.sh removes the program and app-backups/, keeps the data and update-backups/" part1_uninstall

echo "Part 2: updates with a running service (ACCOUNTDECK2_INSTALL_ROOT, fake launchctl/open, stub service)"
P21="$T/p2-2.1.0" P20="$T/p2-2.0.0"
run "2.1.0 installed with its data and a running service" part2_setup 2.1.0 "$P21"
run "a busy service (2.1: /_health says so) is refused with nothing changed" part2_busy "$P21" 2.1.0
run "an unhealthy new version is rolled back: data untouched, the old service answers again" part2_unhealthy "$P21" 2.1.0
run "a new version that loses a saved sign-in is rolled back; the message names the snapshot" part2_lost_record "$P21" 2.1.0
run "a healthy update 2.1.0 -> $NEW succeeds and prints the census" part2_healthy "$P21" 2.1.0
run "a downgrade is refused while the service runs, nothing changed" part2_downgrade "$P21"
run "with Antigravity accounts saved, an update keeps them and counts them (--dry-run too)" part2_agy_kept "$P21"
run "a new version that loses an Antigravity account is rolled back" part2_agy_lost "$P21"
run "2.0.0 installed with its data and a running service" part2_setup 2.0.0 "$P20"
run "a busy service (2.0: only /api/state says so) is refused with nothing changed" part2_busy "$P20" 2.0.0
run "a healthy update 2.0.0 -> $NEW succeeds and prints the census" part2_healthy "$P20" 2.0.0

echo "upgrade test: $PASSED passed, $FAILED failed"
[ "$FAILED" = 0 ]
