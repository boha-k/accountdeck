#!/bin/bash
# Installs or updates AccountDeck 2 for this Mac user: Apple silicon, macOS 14 (Sonoma) or newer
# (docs/mac/PLATFORM-MAC.md §4-6, docs/UPGRADING.md).
#
#     bash install.sh [--no-launch] [--start-at-login] [--allow-downgrade] [--prefix DIR] [--downloads DIR]
#                     [--skip-tests] [--dry-run]
#
# Run it from the unpacked AccountDeck2-mac package or a checkout; get.sh downloads the latest release and runs
# it for you. A brand-new Mac needs nothing beforehand: the script downloads its own Python and the two packages
# AccountDeck uses, each pinned and checked against its SHA-256, and never calls /usr/bin/python3 or pip3, nor
# git without the developer tools (on a Mac without them those only show an install dialog).
#
# What it creates (DIR replaces your home folder with --prefix, which tests use):
#   ~/Library/Application Support/AccountDeck2/runtime   Python 3.14.8 + cffi + cryptography
#   ~/Library/Application Support/AccountDeck2/app       the AccountDeck 2 code (never under ~/Documents,
#                                                        which iCloud may sync or evict)
#   ~/Applications/AccountDeck 2.app                     the app: Spotlight and Finder find it. Its
#                                                        Contents/MacOS/python3 is a copy of the runtime's
#                                                        python3.14 signed (ad hoc) with the app's bundle id:
#                                                        macOS only lets a process whose code identity matches
#                                                        its bundle id send notifications
#   ~/Library/LaunchAgents/local.accountdeck2.plist      only with --start-at-login
# Everything else in ~/Library/Application Support/AccountDeck2 is your data: vault/ (the saved sign-ins), the
# database, the Keychain's vault key. install.sh reads it and never changes it.
#
# Options:
#   --no-launch        do not open AccountDeck 2 at the end
#   --start-at-login   open AccountDeck 2 when you log in (a LaunchAgent; switch it off in the dashboard)
#   --allow-downgrade  go ahead when the installed version is newer than this one (data a newer version saved
#                      may not open in an older one; without it install.sh stops and changes nothing)
#   --prefix DIR       install below DIR instead of your home folder, for tests: the app, its support
#                      folder and the LaunchAgents folder all go there, and launchctl and the Finder
#                      registration are skipped. It implies --no-launch: the app installed there would
#                      still take its data from your real home folder, so it is never opened
#   --downloads DIR    take the pinned files from DIR when they are there (still checked against their
#                      SHA-256) instead of downloading them: an offline or repeated test install
#   --skip-tests       do not run the test suite against the new code first (the only way to skip it)
#   --dry-run          print what would happen and change nothing
#
# Running it again updates: the runtime is replaced only when the pins changed (or it fails its self-test),
# the app code only when the files changed. An update keeps every saved account and never leaves a broken
# service behind:
#   1. It refuses a downgrade (the installed version is newer) unless --allow-downgrade.
#   2. It refuses while the running service is in the middle of a switch.
#   3. It runs the test suite against the new code; a failure changes nothing.
#   4. It counts the saved accounts: the rows per app in the database and the saved sign-in records in vault/
#      (counts and file names only, never a secret).
#   5. When the app code changes, it keeps a snapshot of the saved sign-ins first, in
#      update-backups/<date>-<time>-v<old version>/ (the last 3): the database through sqlite's backup API,
#      vault/ while the service is stopped. The records stay encrypted under the same Keychain key. It is a
#      last resort and never restored automatically: a sign-in renewed after it was taken is stale in it.
#   6. It stops the service, keeps the previous app in app-backups/ (the last 5), swaps in the new one and
#      restarts the service with launchctl (or opens the app when no LaunchAgent is there).
#   7. It polls /_health for up to 60 s and requires the new version and build, moving ticks and no stall,
#      then counts again: every account and saved sign-in counted before must still be there.
#      Otherwise it puts the previous app back, restarts it, checks it answers, says what failed and exits 1.
# With --no-launch or --prefix it never restarts or opens anything (quit a running copy first); the count after
# the swap still runs. One install or update runs at a time per data folder: a second one stops with nothing
# changed while the first holds the lock (.install.lock.guard in the support folder; the file stays,
# but the kernel releases the lock when the install ends, even after a crash).
set -euo pipefail
umask 022

BUNDLE_ID=local.accountdeck2
LABEL=local.accountdeck2        # the LaunchAgent's (accountdeck/platform/autostart_mac.py)
APP_NAME="AccountDeck 2"
EXECUTABLE=AccountDeck2
MIN_MACOS=14                    # Sonoma: the dashboard uses the popover attribute, which the system WebKit has from
                                # Safari 17 on (the runtime and the cryptography wheel would run from macOS 11)
PY_VERSION=3.14.8
HEALTH_WAIT=60                  # seconds the service has to come up healthy after a restart
STOP_WAIT=90                    # seconds a Quit may take before it is KILLed: past the service's own limit (engine.QUIT_WAIT,
                                # 60 s), as launchd restarts a KILLed job and a KILL mid-swap would start the old copy again
BACKUPS_KEPT=5                  # previous app folders in app-backups/
SNAPSHOTS_KEPT=3                # sign-in snapshots in update-backups/
FIRST_RUN_URL=https://github.com/boha-k/accountdeck#first-run
SNAPSHOT_URL=https://github.com/boha-k/accountdeck/blob/main/docs/UPGRADING.md#the-sign-in-snapshot-update-backups
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
# What install.sh copies from a package or checkout into the app folder: everything except these, so that a
# checkout installs the release's file set (accountdeck/, run.py, make_icon.py, AccountDeck2.icns,
# requirements.txt, install.sh, uninstall.sh, get.sh, README.md, CHANGELOG.md, LICENSE, THIRD_PARTY_NOTICES.md).
# A name matches at any depth. The rest of the tree comes across whole, the dashboard (accountdeck/web) included:
# it is never merged with what was there. Data files are listed too: a checkout used for a demo must not carry any.
EXCLUDES=('.git*' .handoff .claude '.venv*' .ruff_cache .pytest_cache .mypy_cache node_modules package.json package-lock.json
          __pycache__ '*.pyc' .DS_Store third_party docs tests tools dist AGENTS.md CLAUDE.md CONTRIBUTING.md SECURITY.md
          CODE_OF_CONDUCT.md VALIDATION.md OPEN_SOURCE.md '*.sqlite3*' '*.dpapi' '*.adeck' vault runtime.json service.lock
          startup-error.txt '*.log' 'accountdeck2.log*' install.json build.json .source-sha256)

say() { printf '%s\n' "$*"; }
die() { printf 'AccountDeck 2 was not installed: %s\n' "$*" >&2; exit 1; }

usage() {   # the comment at the top of this file, up to `set -euo pipefail`
    awk 'NR == 1 { next } /^set -euo pipefail/ { exit } { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

# ---- pinned runtime (PLATFORM-MAC.md §6): Apple silicon only ----
PY_URL=https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython-3.14.8%2B20261003-aarch64-apple-darwin-install_only.tar.gz
PY_SHA=d15291f940cfecd2e54010d5e37d2e03aa192f076a65d26ab741372fff2dabfe
CFFI_URL=https://files.pythonhosted.org/packages/b3/a9/9db617d05d7367c1ad0ab00b3aa6e6f9281edd689b4ee9ea0e5a84e89c97/cffi-2.1.1-cp314-cp314-macosx_11_0_arm64.whl
CFFI_SHA=661c298b4821edebead0c91edd2b00374d67ad7c5a1f7a91d4442633b79d6a72
CRYPTO_URL=https://files.pythonhosted.org/packages/ba/19/797e2aaac9df6a66f1550f49979dc1b1e39ecd2077501c30efa81e8d5d67/cryptography-50.0.1-cp311-abi3-macosx_11_0_arm64.whl
CRYPTO_SHA=b8f852c65863251b9e3a1b8c150ce21e59b522dbb6a7d4bc80e680d38388e986

# The runtime self-test: a real AES-GCM round trip, which needs cffi's _cffi_backend, not only the import.
SELF_TEST='import os, sys
assert sys.version_info >= (3, 14), sys.version
import ctypes, fcntl, sqlite3, ssl, plistlib
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
key = AESGCM.generate_key(256); nonce = os.urandom(12)
box = AESGCM(key).encrypt(nonce, b"AccountDeck 2 self-test", b"aad")
assert AESGCM(key).decrypt(nonce, box, b"aad") == b"AccountDeck 2 self-test"
try:
    AESGCM(key).decrypt(nonce, box[:-1] + bytes([box[-1] ^ 1]), b"aad")
except InvalidTag:
    pass
else:
    raise AssertionError("a changed ciphertext was accepted")
print("runtime self-test passed:", sys.version.split()[0], "cryptography", __import__("cryptography").__version__)'

# This install's running service on loopback, as `SERVICE_TOOL <data folder> <command>` (stdlib only: it also has
# to work against the old version and the new code's failures). The token in runtime.json opens a dashboard session
# for the old versions' /api/state and for /api/stop, and is never printed.
#   state                  down | hung | idle VERSION | busy WHAT
#   peek                   the same from GET /_health alone, for --dry-run: it opens no session, so a service before 2.1
#                          is `unknown VERSION` (its /_health does not say whether a switch runs)
#   stop                   stopped | busy | down | unreachable | refused CODE. /api/stop answers 409 while an operation runs
#   wait-up V BUILD SECS   ok, or why not, after polling /_health for SECS: version V, build BUILD (a build.json, or -
#                          for none), ticks moving, not stalled, no tick phase failing, the page loads
SERVICE_TOOL='import http.client, http.cookiejar, json, sys, time, urllib.error, urllib.request
data, command = sys.argv[1], sys.argv[2]
PLAIN = urllib.request.build_opener(urllib.request.ProxyHandler({}))
UNANSWERED = (OSError, ValueError, http.client.HTTPException)
NOT_RUNNING = ("done", "unconfirmed", "failed", "cancelled", "needs_reconfirm")    # operations.TERMINAL_PHASES

def runtime():
    try:
        with open(data + "/runtime.json", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    good = isinstance(value, dict) and value.get("app") == "accountdeck2" and value.get("host") == "127.0.0.1"
    return value if good and type(value.get("port")) is int else None

def origin(run):
    return "http://127.0.0.1:%d" % run["port"]

def health(run):
    with PLAIN.open(origin(run) + "/_health", timeout=2) as answer:
        body = json.load(answer)
    if not isinstance(body, dict) or body.get("app") != "accountdeck2":
        raise ValueError("not AccountDeck")
    return body

def post(opener, run, path, body):
    request = urllib.request.Request(origin(run) + path, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "X-AccountDeck": "1", "Origin": origin(run)})
    with opener.open(request, timeout=10) as answer:
        answer.read()

def session(run):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    post(opener, run, "/api/session", {"token": run.get("token")})
    return opener

def state(ask=True):
    run = runtime()
    if run is None:
        return "down"
    try:
        body = health(run)
    except UNANSWERED:
        return "hung"
    engine = body.get("engine") if isinstance(body.get("engine"), dict) else {}
    pending = engine.get("pending")
    if "pending" not in engine:     # before 2.1 /_health does not say; /api/state does, behind a session
        if not ask:
            return "unknown %s" % body.get("version")
        try:
            with session(run).open(origin(run) + "/api/state", timeout=15) as answer:
                pending = json.load(answer).get("pending")
            pending = None if pending and pending.get("phase") in NOT_RUNNING else pending
        except UNANSWERED + (AttributeError,):
            pending = None  # /api/stop still refuses while one runs
    if pending:
        return "busy %s %s" % (pending.get("provider"), pending.get("kind"))
    return "idle %s" % body.get("version")

def stop():
    run = runtime()
    if run is None:
        return "down"
    try:
        post(session(run), run, "/api/stop", {})
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        return "busy" if code == 409 else "refused %d" % code
    except UNANSWERED:
        return "unreachable"
    return "stopped"

def look(version, build, ticks):
    """(why it is not healthy or None, the ticks seen first)."""
    run = runtime()
    if run is None:
        return "it did not write runtime.json: the service did not start", ticks
    try:
        body = health(run)
    except UNANSWERED:
        return "nothing answers /_health on port %d" % run["port"], ticks
    if body.get("version") != version:
        return "it answers as version %s, expected %s" % (body.get("version"), version), ticks
    if build is not None and body.get("build") != build:
        return "it answers with build %s, expected %s" % (json.dumps(body.get("build")), json.dumps(build)), ticks
    engine = body.get("engine")
    if not isinstance(engine, dict) or type(engine.get("ticks")) is not int:
        return "it reports no ticks: its engine does not answer", ticks
    if body.get("stalled"):
        return "it reports stalled (%s)" % ", ".join(map(str, engine.get("stall_reasons") or ())), ticks
    if ticks is None:
        ticks = engine["ticks"]
    if engine["ticks"] <= ticks:
        return "its ticks are not moving (still %d, last tick %s s ago)" % (ticks, engine.get("tick_age_s")), ticks
    failing = engine.get("failing_phases") or {}
    if failing:         # a phase that succeeds once leaves this list
        return "tick phases keep failing: %s" % ", ".join("%s (%s)" % (name, failing[name].get("error_type")) for name in sorted(failing)), ticks
    try:
        with PLAIN.open(origin(run) + "/", timeout=2) as answer:
            answer.read()
    except UNANSWERED:
        return "the dashboard page does not load", ticks
    return None, ticks

def wait_up(version, build_file, seconds):
    build = None
    if build_file != "-":
        with open(build_file, encoding="utf-8") as handle:
            build = json.load(handle)
    deadline, ticks = time.monotonic() + float(seconds), None
    while True:
        why, ticks = look(version, build, ticks)
        if why is None:
            return "ok"
        if time.monotonic() >= deadline:
            return why
        time.sleep(1)

print({"state": state, "peek": lambda: state(False), "stop": stop}[command]() if command != "wait-up" else wait_up(*sys.argv[3:6]))'

# The saved accounts, read-only, as `DB_TOOL <data folder> <work folder> <command>` (stdlib only, so it depends on
# neither version's code). Counts and file names only: it never reads a secret, and writes only below the work folder
# and the snapshot file it is given.
#   census OUT    writes what is saved now to OUT (JSON) and prints it in words: the rows of `accounts` per app, the
#                 rows of `antigravity_accounts` (Antigravity's own table; none where it is not there yet) and the
#                 saved sign-in records, one <account id>.dpapi per account and agy-<account id>.dpapi per
#                 Antigravity account (core.Vault). Stashes, older generations, .prev, quarantined and deferred records
#                 are left out: a healthy service removes those by itself.
#   check BEFORE  `ok <in words>` when every row per app and every record in BEFORE is still there, else `lost <what>`
#   snapshot GZ   a consistent copy of the database through sqlite's backup API, gzipped into GZ (0600); prints
#                 `<rows> <schema>`
DB_TOOL='import gzip, json, os, re, shutil, sqlite3, sys, tempfile, urllib.parse
data, work, command = sys.argv[1], sys.argv[2], sys.argv[3]
DB = os.path.join(data, "accounts.sqlite3")
RECORD = re.compile(r"(agy-)?[0-9a-f]{32}\.dpapi")
APPS = {"claude": "Claude Desktop", "claude_cli": "Claude Code", "codex": "Codex", "antigravity": "Antigravity"}

def connect():
    """A read-only view of the database that creates nothing beside it. A read-only connection to a WAL database
    whose -wal and -shm are gone (it was closed cleanly) would create them; then the file holds everything and is
    read as immutable. A -wal nobody indexes is read from a private copy, where recovering it writes nothing here."""
    wal, shm, uri = DB + "-wal", DB + "-shm", "file:" + urllib.parse.quote(DB)
    if os.path.exists(wal) and os.path.exists(shm):          # a service has it open, or had
        try:
            db = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=10)
            db.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return db
        except sqlite3.Error:
            pass
    elif not os.path.exists(wal) or os.path.getsize(wal) == 0:
        return sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True)
    copy = os.path.join(tempfile.mkdtemp(dir=work), "accounts.sqlite3")
    for suffix in ("", "-wal"):
        if os.path.exists(DB + suffix):
            shutil.copyfile(DB + suffix, copy + suffix)
    return sqlite3.connect(copy)

def census():
    if not os.path.isfile(DB):
        raise OSError("accounts.sqlite3 is not there")
    db = connect()
    try:
        rows = dict(db.execute("SELECT provider, count(*) FROM accounts GROUP BY provider").fetchall())
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?", ("table", "antigravity_accounts")).fetchone():
            agy = db.execute("SELECT count(*) FROM antigravity_accounts").fetchone()[0]
            if agy:
                rows["antigravity"] = rows.get("antigravity", 0) + agy
    finally:
        db.close()
    vault = os.path.join(data, "vault")
    records = sorted(name for name in os.listdir(vault) if RECORD.fullmatch(name)) if os.path.isdir(vault) else []
    return rows, records

def words(rows, records):
    order = list(APPS)                    # as listed: Antigravity after the three apps that came first
    apps = ", ".join("%d %s" % (rows[key], APPS.get(key, key))
                     for key in sorted(rows, key=lambda key: (order.index(key) if key in APPS else len(order), key)))
    return "%s, with %d saved sign-in%s" % (apps or "none", len(records), "" if len(records) == 1 else "s")

def counted(rows, records):
    total = sum(rows.values())
    return "%d saved account%s (%s)" % (total, "" if total == 1 else "s", words(rows, records))

def check(before_file):
    with open(before_file, encoding="utf-8") as handle:
        before = json.load(handle)
    try:
        rows, records = census()
    except (OSError, sqlite3.Error) as error:
        return "lost the database could not be read (%s)" % error
    lost = ["%d of %d %s account%s" % (count - rows.get(key, 0), count, APPS.get(key, key), "" if count == 1 else "s")
            for key, count in sorted(before["rows"].items()) if rows.get(key, 0) < count]
    gone = sorted(set(before["records"]) - set(records))
    if gone:
        lost.append("%d saved sign-in record%s in vault/ (%s)" % (len(gone), "" if len(gone) == 1 else "s", ", ".join(gone)))
    if lost:
        return "lost " + "; ".join(lost)
    total = sum(before["rows"].values())
    if total == 1:
        return "ok The saved account is still here: " + words(rows, records) + "."
    return "ok All %d saved accounts are still here: %s." % (total, words(rows, records))

def snapshot(target):
    copy = os.path.join(tempfile.mkdtemp(dir=work), "accounts.sqlite3")
    source, out = connect(), sqlite3.connect(copy)
    try:
        source.backup(out)
        out.execute("PRAGMA journal_mode=DELETE")       # a file of its own, without WAL sidecars
        rows = out.execute("SELECT count(*) FROM accounts").fetchone()[0]
        schema = out.execute("PRAGMA user_version").fetchone()[0]
    finally:
        out.close()
        source.close()
    with os.fdopen(os.open(target + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as raw:
        with open(copy, "rb") as plain, gzip.GzipFile(filename="accounts.sqlite3", mode="wb", compresslevel=6, fileobj=raw) as packed:
            shutil.copyfileobj(plain, packed, 1 << 20)
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(target + ".tmp", target)
    return "%d %d" % (rows, schema)

if command == "census":
    rows, records = census()
    with open(sys.argv[4], "w", encoding="utf-8") as handle:
        json.dump({"rows": rows, "records": records}, handle)
    print(counted(rows, records))
elif command == "check":
    print(check(sys.argv[4]))
elif command == "snapshot":
    print(snapshot(sys.argv[4]))
else:
    raise SystemExit("unknown command " + command)'

# Runs a runtime's python with a clean environment: no PYTHON*/PIP* variables, no user site, HOME in
# the work folder so nothing (pip's cache included) is written to the real home.
clean_python() {
    local python=$1; shift
    env -i PATH=/usr/bin:/bin HOME="$WORK" LANG=en_US.UTF-8 PIP_CONFIG_FILE=/dev/null PIP_NO_INPUT=1 \
        "$python" -I -B "$@"
}

self_test() { clean_python "$1" -c "$SELF_TEST"; }

service_tool() {   # the new runtime until it is in place; no python at all means nothing of ours can run
    local python="$RT/python/bin/python3.14"
    if [ -n "${RT_NEW:-}" ] && [ -x "$RT_NEW/python/bin/python3.14" ]; then python="$RT_NEW/python/bin/python3.14"; fi
    [ -x "$python" ] || { printf 'down\n'; return 0; }
    clean_python "$python" -c "$SERVICE_TOOL" "$SUPPORT" "$@"
}

db_tool() {   # DB_TOOL with the new runtime until it is in place
    local python="$RT/python/bin/python3.14"
    if [ -n "${RT_NEW:-}" ] && [ -x "$RT_NEW/python/bin/python3.14" ]; then python="$RT_NEW/python/bin/python3.14"; fi
    clean_python "$python" -c "$DB_TOOL" "$SUPPORT" "$WORK" "$@"
}

last_line() { printf '%s\n' "$1" | sed '/^[[:space:]]*$/d' | tail -n 1; }

version_newer() {   # version_newer A B: A is newer than B, compared as dotted numbers (2.10.0 > 2.9.1; a missing part is 0)
    local a=$1 b=$2 x y
    while [ -n "$a$b" ]; do
        x=${a%%.*}; y=${b%%.*}
        case "$a" in *.*) a=${a#*.} ;; *) a="" ;; esac
        case "$b" in *.*) b=${b#*.} ;; *) b="" ;; esac
        x=${x%%[!0-9]*}; y=${y%%[!0-9]*}             # the number a part starts with ("0rc1" counts as 0)
        x=$((10#${x:-0})); y=$((10#${y:-0}))
        if [ "$x" -gt "$y" ]; then return 0; fi
        if [ "$x" -lt "$y" ]; then return 1; fi
    done
    return 1
}

data_present() {   # an earlier install left saved accounts or sign-ins
    [ -f "$SUPPORT/accounts.sqlite3" ] || [ -n "$(ls -A "$SUPPORT/vault" 2>/dev/null)" ]
}

start_snapshot() {   # update-backups/.partial-…, with the database copied now: the service may still run, and its WAL keeps
                     # the copy consistent, so it is down for no longer than the swap
    local made
    mkdir -p "$UPDATE_BACKUPS"
    chmod 700 "$UPDATE_BACKUPS"
    SNAP_STAMP=$(date +%Y%m%d-%H%M%S)
    SNAP_PART=$(mktemp -d "$UPDATE_BACKUPS/.partial-XXXXXX")
    SNAP_DB=""
    if [ -f "$SUPPORT/accounts.sqlite3" ]; then
        made=$(db_tool snapshot "$SNAP_PART/accounts.sqlite3.gz" 2>&1) \
            || die "the database could not be copied for the snapshot before the update ($(last_line "$made")). Nothing was changed."
        SNAP_DB=$made
    fi
}

finish_snapshot() {   # vault/ while the service is stopped, the manifest, and the folder gets its name
    local files=0 rows schema
    if [ -d "$SUPPORT/vault" ]; then
        cp -Rp "$SUPPORT/vault" "$SNAP_PART/vault" || die "the saved sign-ins could not be copied for the snapshot."
        diff -rq "$SUPPORT/vault" "$SNAP_PART/vault" >/dev/null 2>&1 \
            || die "the snapshot's copy of the saved sign-ins does not match vault/."
        files=$(find "$SNAP_PART/vault" -type f | wc -l | tr -d ' ')
    fi
    rows=${SNAP_DB%% *}; schema=${SNAP_DB#* }
    {
        say "AccountDeck 2: the saved sign-ins before an update (written by install.sh)"
        say "Taken:    $SNAP_STAMP, before the update from ${OLD_VERSION:-an unknown version} to $NEW_VERSION"
        say "Data:     $SUPPORT"
        if [ -n "$SNAP_DB" ]; then
            say "Database: accounts.sqlite3.gz, accounts.sqlite3 copied through sqlite's backup API: $rows account rows, schema $schema"
        else
            say "Database: none (there was no accounts.sqlite3)"
        fi
        say "Vault:    vault/, $files files, still encrypted under this Mac user's Keychain item \"AccountDeck2 vault\""
        say ""
        say "A last resort, never restored automatically: a sign-in renewed after this was taken is stale here (its"
        say "refresh token may be spent). It opens only for the same Mac user and data folder. How to put it back by"
        say "hand: $SNAPSHOT_URL"
    } > "$SNAP_PART/MANIFEST.txt"
    find "$SNAP_PART" -type d -exec chmod 700 {} +
    find "$SNAP_PART" -type f -exec chmod 600 {} +
    SNAP="$UPDATE_BACKUPS/$SNAP_STAMP-v${OLD_VERSION:-unknown}"
    while [ -e "$SNAP" ]; do SNAP="$SNAP-1"; done
    mv "$SNAP_PART" "$SNAP"; SNAP_PART=""
    say "Snapshot: the saved sign-ins and the database are copied to $SNAP"
}

census_after() {   # every account and saved sign-in counted before the swap must still be there, or the update fails
    local answer
    if [ "$CENSUS" != 1 ] || [ "$CENSUS_DONE" = 1 ]; then return 0; fi
    CENSUS_DONE=1
    answer=$(db_tool check "$WORK/census-before" 2>&1) || answer="lost the count did not run ($(last_line "$answer"))"
    case "$answer" in
    "ok "*) CENSUS_LINE=${answer#ok }; return 0 ;;
    esac
    printf '%s %s lost saved accounts: %s\n' "$APP_NAME" "$NEW_VERSION" "${answer#lost }" >&2
    # Printed after the rollback, last, so it is what stays on the screen.
    if [ -n "$SNAP" ]; then
        ROLLBACK_NOTE="The saved sign-ins were not restored automatically. A copy of them and of the database from just
before the update is in
  $SNAP
It is not put back by itself: AccountDeck renews sign-ins as it works, and putting back an older copy of a renewed
sign-in would leave that account needing a new sign-in. How to put it back by hand:
  $SNAPSHOT_URL"
    else
        ROLLBACK_NOTE="The saved sign-ins were not restored automatically, and this run took no snapshot (the app code
did not change). AccountDeck's own daily copies of the database are in $SUPPORT/backups."
    fi
    exit 1
}

fetch() {   # fetch URL SHA256 OUT
    local url=$1 sha=$2 out=$3 actual
    if [ -n "$DOWNLOADS" ] && [ -f "$DOWNLOADS/${url##*/}" ]; then
        cp "$DOWNLOADS/${url##*/}" "$out"
    else
        curl -fsSL --proto '=https' --tlsv1.2 --retry 2 --connect-timeout 30 -o "$out" "$url" \
            || die "could not download ${url##*/}. Check the internet connection and run install.sh again."
    fi
    actual=$(shasum -a 256 "$out" | cut -d ' ' -f 1)
    [ "$actual" = "$sha" ] || die "${url##*/} did not match its pinned SHA-256 (got $actual). Nothing was changed."
}

tree_hash() {   # one SHA-256 over every file's path and content below $1
    (cd "$1" && find . -type f ! -name install.json ! -name build.json ! -name .source-sha256 -print0 | LC_ALL=C sort -z \
        | xargs -0 shasum -a 256 | shasum -a 256 | cut -d ' ' -f 1)
}

stage_app() {   # stage_app SRC DIR: what the app folder will hold. The dashboard must arrive exactly as it is in SRC.
    local pattern excludes=()
    for pattern in "${EXCLUDES[@]}"; do excludes+=(--exclude "$pattern"); done
    (cd "$1" && tar -cf - "${excludes[@]}" .) | (cd "$2" && tar -xf -)
    diff -r -x .DS_Store "$1/accountdeck/web" "$2/accountdeck/web" >/dev/null \
        || die "the dashboard files were not copied whole from $1. Nothing was changed."
}

code_version() { sed -n 's/^__version__ = "\([^"]*\)".*$/\1/p' "$1/accountdeck/__init__.py"; }

git_usable() {   # on a Mac without the developer tools /usr/bin/git only shows an install dialog
    local path
    path=$(command -v git 2>/dev/null) || return 1
    [ "$path" != /usr/bin/git ] || xcode-select -p >/dev/null 2>&1
}

write_build() {   # write_build SRC OUT: build.json, what `run.py --version` and /_health report (package: no commit)
    local commit=null dirty=null found changes
    if [ -e "$1/.git" ] && git_usable && found=$(git -C "$1" rev-parse --short HEAD 2>/dev/null); then
        commit="\"$found\""
        if changes=$(git -C "$1" status --porcelain 2>/dev/null); then
            if [ -n "$changes" ]; then dirty=true; else dirty=false; fi
        fi
    fi
    printf '{"commit": %s, "dirty": %s, "installed_at": %s}\n' "$commit" "$dirty" "$(date +%s)" > "$2"
}

run_suite() {   # run_suite PYTHON SRC CODE: the new code's tests in safe mode; a package has none, so its code must import
    local python=$1 src=$2 code=$3 status=0
    if [ ! -d "$src/tests" ]; then
        (cd "$code" && env -i PATH=/usr/bin:/bin HOME="$WORK" LANG=en_US.UTF-8 ACCOUNTDECK_SAFE_MODE=1 "$python" -B -E -s \
            -c 'import run, accountdeck.server, accountdeck.engine, accountdeck.doctor') >"$WORK/suite.log" 2>&1 || status=$?
        if [ "$status" -ne 0 ]; then
            tail -n 20 "$WORK/suite.log" >&2
            die "the new code does not import. Nothing was changed."
        fi
        say "Tests: this package carries none; the new code imports."
        return 0
    fi
    say "Tests: running the suite against the new code..."
    SUITE_TMP=$(mktemp -d "${TMPDIR:-/tmp}/accountdeck-suite-XXXXXX")
    (cd "$src" && env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME="$SUITE_TMP" TMPDIR="$SUITE_TMP" LANG=en_US.UTF-8 ACCOUNTDECK_SAFE_MODE=1 \
        "$python" -B -E -s -m unittest discover -s tests -t .) >"$WORK/suite.log" 2>&1 || status=$?
    if [ "$status" -ne 0 ]; then
        tail -n 40 "$WORK/suite.log" >&2
        die "the test suite failed on the new code. Nothing was changed (--skip-tests skips it)."
    fi
    say "Tests: $(tail -n 3 "$WORK/suite.log" | tr '\n' ' ' | sed 's/  */ /g')"
}

sq() {   # a single-quoted shell word
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

app_running() {   # a python of this user runs this install's run.py (started by the app or the agent)
    pgrep -U "$(id -u)" -f -- "$SERVICE_MATCH" >/dev/null 2>&1
}

wait_gone() {   # wait_gone SECONDS: true once nothing of this install runs any more
    local waited=0
    while app_running; do
        [ "$waited" -lt "$1" ] || return 1
        sleep 1
        waited=$((waited + 1))
    done
}

end_service() {   # what still runs from this install's folder, by its command line: TERM (a clean Quit), then KILL
    pkill -TERM -U "$(id -u)" -f -- "$SERVICE_MATCH" >/dev/null 2>&1 || true
    if wait_gone "$STOP_WAIT"; then return 0; fi
    pkill -KILL -U "$(id -u)" -f -- "$SERVICE_MATCH" >/dev/null 2>&1 || true
    wait_gone 5
}

stop_service() {   # stops the running service through its own /api/stop, which refuses while an operation runs
    local answer
    SERVICE_STOPPED=1
    answer=$(service_tool stop 2>/dev/null) || answer=unreachable
    if [ "$answer" = busy ]; then
        SWAPPING=0
        die "$APP_NAME is in the middle of a switch. Let it finish, then run install.sh again. Nothing was changed."
    fi
    if [ "$answer" = stopped ] && wait_gone "$STOP_WAIT"; then return 0; fi
    say "$APP_NAME did not stop by itself ($answer): ending it."
    end_service || die "$APP_NAME could not be stopped."
}

start_service() {   # launchd's job when it is loaded (kickstart -k replaces anything launchd restarted meanwhile), else its plist, else the app
    local domain target plist
    domain="gui/$(id -u)"
    target="$domain/$LABEL"
    plist="$AGENTS_DIR/$LABEL.plist"
    if launchctl print "$target" >/dev/null 2>&1; then
        if launchctl kickstart -k "$target" 9>&- >/dev/null 2>&1; then return 0; fi
    elif [ -f "$plist" ] && launchctl bootstrap "$domain" "$plist" 9>&- >/dev/null 2>&1; then
        return 0
    fi
    open "$APP" 9>&-                # a launched app must not inherit and keep the installer's lock
}

job_pid() {   # the pid of launchd's job while it runs ("pid = N" in launchctl print), else nothing
    { launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null || true; } \
        | sed -n 's/^[[:space:]]*pid[[:space:]]*=[[:space:]]*\([0-9][0-9]*\)[[:space:]]*$/\1/p'
}

prune_dated() {   # prune_dated FOLDER KEEP: the installer's own dated folders only; what someone put there by hand stays
    local found=() name index=0
    for name in "$1"/[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]-v*; do
        if [ -d "$name" ]; then found+=("$name"); fi
    done
    while [ $((${#found[@]} - index)) -gt "$2" ]; do
        rm -rf "${found[$index]}"
        index=$((index + 1))
    done
}

take_back() {   # take_back LIVE SAVED: what a swap saved goes back; nothing saved (yet) means LIVE is still the old one
    if [ -e "$2" ]; then
        rm -rf "$1"
        mv "$2" "$1"
    fi
}

roll_back() {   # the previous app, bundle and runtime back in place and running; never exits by itself
    local answer build=-
    if [ "$RT_SWAPPED$APP_SWAPPED$BUNDLE_SWAPPED$SERVICE_STOPPED$START_TRIED" = 00000 ] && [ -z "$BACKUP" ]; then
        say "Nothing was changed."
        return 0
    fi
    say "Putting the previous version back..."
    if [ "$SERVICE_STOPPED$START_TRIED" != 00 ]; then end_service || say "What was running did not stop."; fi
    if [ -n "$BACKUP" ]; then
        take_back "$APPDIR" "$BACKUP"
    elif [ "$APP_SWAPPED" = 1 ]; then
        rm -rf "$APPDIR"                    # a first install: nothing came before it
    fi
    if [ "$BUNDLE_SWAPPED" = 1 ]; then take_back "$APP" "$APP.old"; fi
    if [ "$RT_SWAPPED" = 1 ]; then take_back "$RT" "$RT.old"; fi
    if [ ! -d "$APPDIR" ]; then
        if [ -n "$BACKUP" ]; then
            printf 'The previous app could not be put back: it is in %s\n' "$BACKUP" >&2
        else
            rm -rf "$APP"                   # it would open a run.py that is not there
            say "There was no earlier version to go back to: the new app was removed."
        fi
        return 0
    fi
    if [ "$SERVICE_STOPPED$START_TRIED" = 00 ]; then
        say "The previous files are back in place."
        return 0
    fi
    start_service || say "The previous version could not be started."
    [ ! -f "$APPDIR/build.json" ] || build="$APPDIR/build.json"
    answer=$(service_tool wait-up "$OLD_VERSION" "$build" "$HEALTH_WAIT" 2>&1) || answer="its check did not run"
    if [ "$answer" = ok ]; then
        say "The previous version ($OLD_VERSION) is running again."
    else
        printf 'The previous version (%s) did not come back healthy: %s\n' "$OLD_VERSION" "$answer" >&2
    fi
}

cleanup() {
    local status=$?
    trap '' INT TERM HUP                    # nothing interrupts the cleanup, the rollback least of all
    trap - EXIT
    if [ "${SWAPPING:-0}" = 1 ]; then       # any failure or interruption between the first swap and a healthy service
        set +e
        SWAPPING=0
        roll_back
        [ "$status" -ne 0 ] || status=1
    fi
    if [ -n "${ROLLBACK_NOTE:-}" ]; then printf '%s\n' "$ROLLBACK_NOTE" >&2; fi
    if [ -n "${SNAP_PART:-}" ]; then              # a snapshot cut short: never left looking like a whole one
        rm -rf "$SNAP_PART"
        rmdir "$UPDATE_BACKUPS" 2>/dev/null || true
    fi
    [ -n "${WORK:-}" ] && rm -rf "$WORK"
    [ -n "${SUITE_TMP:-}" ] && rm -rf "$SUITE_TMP"
    [ -n "${RT_NEW:-}" ] && rm -rf "$RT_NEW"
    [ -n "${APP_NEW:-}" ] && rm -rf "$APP_NEW"
    [ -n "${BUNDLE_NEW:-}" ] && rm -rf "$BUNDLE_NEW"
    if [ "${LOCKED:-0}" = 1 ]; then rm -f "$LOCK"; fi     # the pid for compatibility with older installers
    exec 9>&-                       # last: release the kernel lock only after staging and rollback are finished
    exit "$status"
}

take_lock() {   # one install or update per data folder: a second one would share app.new, runtime.new and the bundle's
                # .new folder and stop the same service, and its cleanup would remove the first one's staging
    local status=64
    # Keep the pid file understood by older installers, but arbitrate new installers with a kernel lock.
    # Never unlink the guard: another opener could otherwise lock a different inode at the same path.
    if lock_holder_runs "$LOCK"; then
        die "another install or update of AccountDeck 2 is running. Let it finish, then run install.sh again. Nothing was changed."
    fi
    if ! { exec 9>>"$LOCK.guard"; } 2>/dev/null; then
        die "the install lock $LOCK.guard could not be made (the guard file could not be opened). Nothing was changed."
    fi
    # lockf's fd form leaves the lock on the descriptor this shell holds. Older macOS releases lack that
    # form (or lockf itself); their system Perl can take the same flock on the inherited descriptor.
    if [ -x /usr/bin/lockf ]; then
        status=0
        /usr/bin/lockf -s -t 0 9 2>/dev/null || status=$?
    fi
    if [ "$status" = 64 ] && [ -x /usr/bin/perl ]; then
        status=0
        /usr/bin/perl -T -MFcntl=:flock -e '
            open(my $lock, "<&=9") or exit 74;
            flock($lock, LOCK_EX | LOCK_NB) or exit ($!{EWOULDBLOCK} ? 75 : 74);
        ' 2>/dev/null || status=$?
    fi
    if [ "$status" = 75 ]; then
        die "another install or update of AccountDeck 2 is running. Let it finish, then run install.sh again. Nothing was changed."
    fi
    [ "$status" = 0 ] || die "the install lock $LOCK.guard could not be made (file locking failed, code $status). Nothing was changed."
    # An older installer might have started while this one acquired the guard.
    if lock_holder_runs "$LOCK"; then
        die "another install or update of AccountDeck 2 is running. Let it finish, then run install.sh again. Nothing was changed."
    fi
    if ! { printf '%s\n' "$$" > "$LOCK"; } 2>/dev/null; then
        die "the install lock $LOCK could not be made (the pid file could not be written). Nothing was changed."
    fi
    LOCKED=1
}

lock_holder_runs() {   # lock_holder_runs FILE: the pid in FILE is another run's and runs an install.sh, however it was
                       # started (bash install.sh, ./install.sh, get.sh's bash)
    local holder command=""
    holder=$(head -c 16 "$1" 2>/dev/null | tr -cd '0-9' || true)
    if [ -n "$holder" ] && [ "$holder" != "$$" ]; then command=$(ps -p "$holder" -o command= 2>/dev/null || true); fi
    case "$command" in *install.sh*) return 0 ;; esac
    return 1
}

plan() {   # --dry-run: every fact is read, nothing is written outside a temporary folder that goes at exit
    local src=$1 launch=$2 skip_tests=$3 prefix=$4 hash state changed=0 code_changes=0 pins counted files
    WORK=$(mktemp -d "${TMPDIR:-/tmp}/accountdeck-plan-XXXXXX")
    say "$APP_NAME install plan (dry run: nothing is changed)"
    say "  Source:   $src, version $NEW_VERSION"
    if [ "$HAD_APP" = 0 ]; then
        say "  Action:   install $NEW_VERSION (no earlier app at $APPDIR)"
    elif [ "$DOWNGRADE" = 1 ] && [ "$ALLOW_DOWNGRADE" = 0 ]; then
        say "  Action:   refused: the installed version $OLD_VERSION is newer than $NEW_VERSION. install.sh would stop here"
        say "            and change nothing; --allow-downgrade goes ahead (data a newer version saved may not open)"
        exit 1
    elif [ "$DOWNGRADE" = 1 ]; then
        say "  Action:   downgrade from $OLD_VERSION to $NEW_VERSION (--allow-downgrade)"
    elif [ "$OLD_VERSION" = "$NEW_VERSION" ]; then
        say "  Action:   $NEW_VERSION is installed already: only what differs would be replaced"
    else
        say "  Action:   update from ${OLD_VERSION:-an unknown version} to $NEW_VERSION"
    fi
    mkdir "$WORK/app"
    stage_app "$src" "$WORK/app"
    hash=$(tree_hash "$WORK/app")
    if [ -f "$APPDIR/.source-sha256" ] && [ "$(cat "$APPDIR/.source-sha256")" = "$hash" ]; then
        say "  App code: unchanged at $APPDIR"
    elif [ "$HAD_APP" = 1 ]; then
        changed=1 code_changes=1
        say "  App code: ${OLD_VERSION:-an unknown version} at $APPDIR would be replaced whole by $NEW_VERSION (the dashboard included)"
        say "  Backup:   the previous app would go to $BACKUPS/<date>-<time>-v${OLD_VERSION:-unknown}; the last $BACKUPS_KEPT dated ones stay"
    else
        changed=1 code_changes=1
        say "  App code: $NEW_VERSION would be installed at $APPDIR"
    fi
    pins=$(printf '%s %s\n' "$PY_URL" "$PY_SHA" "$CFFI_URL" "$CFFI_SHA" "$CRYPTO_URL" "$CRYPTO_SHA")
    if [ -x "$RT/python/bin/python3.14" ] && [ -f "$RT/PINS" ] && [ "$(cat "$RT/PINS")" = "$pins" ] \
            && self_test "$RT/python/bin/python3.14" >/dev/null 2>&1; then
        say "  Runtime:  Python $PY_VERSION with cryptography, unchanged"
    else
        changed=1
        say "  Runtime:  Python $PY_VERSION, cffi and cryptography would be downloaded and checked against their pins"
    fi
    if [ "$changed" = 0 ]; then
        say "  Tests:    not needed: nothing changes"
    elif [ "$skip_tests" = 1 ]; then
        say "  Tests:    skipped (--skip-tests)"
    elif [ -d "$src/tests" ]; then
        say "  Tests:    the suite in $src/tests would run against the new code first; a failure stops the install"
    else
        say "  Tests:    this package carries none; the new code would have to import"
    fi
    state=$(service_tool peek 2>/dev/null) || state=down
    case "$state" in
    busy*) say "  Service:  in the middle of a switch (${state#busy }): the install would refuse until it finishes" ;;
    idle*) say "  Service:  running ${state#idle }" ;;
    unknown*) say "  Service:  running ${state#unknown }, which does not say over /_health whether a switch is running:"
              say "            the install itself asks it and refuses during one" ;;
    hung) say "  Service:  it does not answer" ;;
    *) say "  Service:  not running" ;;
    esac
    if [ "$changed" = 1 ] && [ -f "$SUPPORT/accounts.sqlite3" ]; then
        if [ ! -x "$RT/python/bin/python3.14" ]; then
            say "  Accounts: counted with the new runtime before the swap, and again after it"
        elif counted=$(db_tool census "$WORK/census" 2>&1); then
            say "  Accounts: $counted now; each must still be there after the update, or the previous version goes back"
        else
            say "  Accounts: the database cannot be read now ($(last_line "$counted")): the install would stop there"
        fi
    fi
    if [ "$code_changes" = 1 ] && data_present; then
        files=0
        if [ -d "$SUPPORT/vault" ]; then files=$(find "$SUPPORT/vault" -type f | wc -l | tr -d ' '); fi
        say "  Snapshot: vault/ ($files files) and the database would be copied to $UPDATE_BACKUPS/<date>-<time>-v${OLD_VERSION:-unknown}"
        say "            first; the last $SNAPSHOTS_KEPT stay; never restored automatically"
    fi
    if [ "$changed" = 1 ] && [ "$launch" = 1 ]; then
        say "  Restart:  stop through the service, swap, launchctl kickstart (or open the app), then up to $HEALTH_WAIT s of /_health:"
        say "            version $NEW_VERSION and the new build, ticks moving, not stalled; otherwise the previous app goes back"
    elif [ "$changed" = 1 ]; then
        say "  Restart:  none (--no-launch or --prefix); a running copy would have to be quit first"
    fi
    if [ -n "$prefix" ]; then
        say "  Open:     never: --prefix implies --no-launch (the app installed there would take its data from the real"
        say "            home folder)"
    elif [ "$launch" = 0 ]; then
        say "  Open:     never (--no-launch)"
    fi
    say "  Never changed: $SUPPORT/vault, the database, the Keychain"
}

DOWNLOADS=""
SWAPPING=0 SERVICE_STOPPED=0 START_TRIED=0 RT_SWAPPED=0 APP_SWAPPED=0 BUNDLE_SWAPPED=0 BACKUP="" OLD_VERSION=""
NEW_VERSION="" HAD_APP=0 DOWNGRADE=0 ALLOW_DOWNGRADE=0 SNAP="" SNAP_PART="" SNAP_STAMP="" SNAP_DB=""
CENSUS=0 CENSUS_DONE=0 CENSUS_LINE="" ROLLBACK_NOTE="" LOCK="" LOCKED=0

installed_version() {
    HAD_APP=0 OLD_VERSION="" DOWNGRADE=0
    if [ -d "$APPDIR" ]; then
        HAD_APP=1
        OLD_VERSION=$(code_version "$APPDIR" 2>/dev/null || true)
        if [ -n "$OLD_VERSION" ] && version_newer "$OLD_VERSION" "$NEW_VERSION"; then DOWNGRADE=1; fi
    fi
}

main() {
    local launch=1 at_login=0 prefix="" manage=true dry_run=0 skip_tests=0
    while [ $# -gt 0 ]; do
        case "$1" in
        --no-launch) launch=0 ;;
        --start-at-login|--start-on-login) at_login=1 ;;
        --prefix) [ $# -ge 2 ] && [ -n "$2" ] || die "--prefix needs a folder."; prefix=$2; shift ;;
        --prefix=*) prefix=${1#--prefix=}; [ -n "$prefix" ] || die "--prefix needs a folder." ;;
        --downloads) [ $# -ge 2 ] && [ -d "$2" ] || die "--downloads needs an existing folder."; DOWNLOADS=$2; shift ;;
        --skip-tests) skip_tests=1 ;;
        --allow-downgrade) ALLOW_DOWNGRADE=1 ;;
        --dry-run) dry_run=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (see bash install.sh --help)." ;;
        esac
        shift
    done
    # --prefix implies --no-launch: the app it installs reads its data folder from the real home, so opening it would
    # make the code under test the live service on the real accounts.
    if [ -n "$prefix" ]; then launch=0; fi

    # ---- this Mac ----
    [ "$(uname -s)" = Darwin ] || die "this installer is for macOS."
    [ "$(id -u)" -ne 0 ] || die "run it as yourself, not with sudo."
    local version major
    version=$(sw_vers -productVersion)
    major=${version%%.*}
    [ "$major" -ge "$MIN_MACOS" ] 2>/dev/null || die "AccountDeck 2 needs macOS $MIN_MACOS (Sonoma) or newer, and this Mac has \
macOS $version. Update macOS in System Settings > General > Software Update, then run the installer again. Nothing was changed."
    # hw.optional.arm64 is 1 on Apple silicon, also from a shell running under Rosetta.
    [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = 1 ] || die "AccountDeck 2 runs only on a Mac with Apple silicon \
(M1 or later), and this Mac has an Intel processor: the Python it installs is built for Apple silicon. Nothing was changed."

    # ---- where things go ----
    local src base root_override=${ACCOUNTDECK2_INSTALL_ROOT:-} tool
    src=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
    [ -f "$src/run.py" ] && [ -f "$src/accountdeck/__init__.py" ] && [ -f "$src/make_icon.py" ] \
        || die "run install.sh from the unpacked AccountDeck2-mac package folder."
    if [ -n "$prefix" ]; then
        [ "$dry_run" = 1 ] || mkdir -p "$prefix"
        base=$(cd "$prefix" 2>/dev/null && pwd -P) || base=$prefix
        manage=false
    elif [ -n "$root_override" ]; then
        # This script's end-to-end test only: a synthetic root that behaves like a home folder, with launchctl and
        # open faked on PATH and safe mode on. Without them it stops here: the real ones would act on the real service.
        [ "${ACCOUNTDECK_SAFE_MODE:-}" = 1 ] || die "ACCOUNTDECK2_INSTALL_ROOT needs ACCOUNTDECK_SAFE_MODE=1."
        for tool in launchctl open; do
            case "$(command -v "$tool" || true)" in /bin/*|/usr/bin/*|"") die "ACCOUNTDECK2_INSTALL_ROOT needs a fake $tool first on PATH." ;; esac
        done
        [ -d "$root_override" ] || die "ACCOUNTDECK2_INSTALL_ROOT must be an existing folder."
        base=$(cd "$root_override" && pwd -P)
        [ "$base" != "$(cd ~ && pwd -P)" ] || die "ACCOUNTDECK2_INSTALL_ROOT must not be your home folder."
    else
        base=${HOME:-}
        [ -n "$base" ] && [ -d "$base" ] || die "your home folder was not found."
    fi
    APPS="$base/Applications"
    SUPPORT="$base/Library/Application Support/AccountDeck2"
    AGENTS_DIR="$base/Library/LaunchAgents"
    LOGS="$base/Library/Logs/AccountDeck2"
    RT="$SUPPORT/runtime"
    APPDIR="$SUPPORT/app"
    BACKUPS="$SUPPORT/app-backups"
    UPDATE_BACKUPS="$SUPPORT/update-backups"
    LOCK="$SUPPORT/.install.lock"
    APP="$APPS/$APP_NAME.app"
    SERVICE_MATCH="python3[^ ]* .*$APPDIR/run\.py"     # the service's command line: an editor with run.py open is not it
    case "$src/" in "$APPDIR/"*|"$RT/"*) die "run install.sh from the package folder, not from the installed copy." ;; esac

    # ---- which versions: a first install, an update, the same version again, or a downgrade ----
    NEW_VERSION=$(code_version "$src")
    [ -n "$NEW_VERSION" ] || die "the version of this package was not found in accountdeck/__init__.py."
    installed_version

    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP
    if [ "$dry_run" = 1 ]; then          # it takes no lock: it changes nothing
        plan "$src" "$launch" "$skip_tests" "$prefix"
        exit 0
    fi
    mkdir -p "$SUPPORT" "$APPS"
    chmod 700 "$SUPPORT"            # the data folder: private to you (PLATFORM-MAC.md §7)
    take_lock                       # before anything is staged
    installed_version               # another installer may have finished while this one did its preflight
    if [ "$DOWNGRADE" = 1 ] && [ "$ALLOW_DOWNGRADE" = 0 ]; then
        die "the installed $APP_NAME is version $OLD_VERSION, newer than this one ($NEW_VERSION). Nothing was changed.
To install $NEW_VERSION anyway, run install.sh again with --allow-downgrade. Data a newer version saved may not open in
an older one, and an older version does not know every mark a newer one leaves on a sign-in: it can try to renew a
sign-in that is already used up, and that account then needs a new sign-in."
    fi
    WORK=$(mktemp -d "$SUPPORT/.install-XXXXXX")
    local who
    who="for $(id -un) (macOS $version)"
    if [ "$HAD_APP" = 0 ]; then
        say "Installing $APP_NAME $NEW_VERSION $who into $base"
        if data_present; then say "The data folder already holds saved accounts from an earlier install: they stay as they are."; fi
    elif [ "$DOWNGRADE" = 1 ]; then
        say "Downgrading $APP_NAME from $OLD_VERSION to $NEW_VERSION (--allow-downgrade) $who in $base"
    elif [ "$OLD_VERSION" = "$NEW_VERSION" ]; then
        say "Checking $APP_NAME $NEW_VERSION, the version already installed, $who in $base"
    else
        say "Updating $APP_NAME from ${OLD_VERSION:-an unknown version} to $NEW_VERSION $who in $base"
    fi

    # ---- the app code: staged, compared with the installed copy ----
    local app_changed=0 new_hash
    APP_NEW="$SUPPORT/app.new"
    rm -rf "$APP_NEW"
    mkdir "$APP_NEW"
    stage_app "$src" "$APP_NEW"
    new_hash=$(tree_hash "$APP_NEW")
    if [ -f "$APPDIR/.source-sha256" ] && [ "$(cat "$APPDIR/.source-sha256")" = "$new_hash" ]; then
        rm -rf "$APP_NEW"; APP_NEW=""
        say "App code: unchanged."
    else
        printf '%s\n' "$new_hash" > "$APP_NEW/.source-sha256"
        write_build "$src" "$APP_NEW/build.json"
        app_changed=1
    fi

    # ---- the runtime: kept when its pins match and it passes the self-test ----
    local pins rt_changed=0 crypto_whl cffi_whl
    pins=$(printf '%s %s\n' "$PY_URL" "$PY_SHA" "$CFFI_URL" "$CFFI_SHA" "$CRYPTO_URL" "$CRYPTO_SHA")
    if [ -x "$RT/python/bin/python3.14" ] && [ -f "$RT/PINS" ] && [ "$(cat "$RT/PINS")" = "$pins" ] \
            && self_test "$RT/python/bin/python3.14" >/dev/null 2>&1; then
        say "Runtime: Python $PY_VERSION with cryptography, unchanged."
    else
        say "Downloading Python $PY_VERSION, cffi and cryptography (about 30 MB)..."
        cffi_whl="$WORK/${CFFI_URL##*/}"           # wheels keep their real names: pip reads the tags
        crypto_whl="$WORK/${CRYPTO_URL##*/}"
        fetch "$PY_URL" "$PY_SHA" "$WORK/python.tar.gz"
        fetch "$CFFI_URL" "$CFFI_SHA" "$cffi_whl"
        fetch "$CRYPTO_URL" "$CRYPTO_SHA" "$crypto_whl"
        RT_NEW="$SUPPORT/runtime.new"
        rm -rf "$RT_NEW"
        mkdir "$RT_NEW"
        tar -xzf "$WORK/python.tar.gz" -C "$RT_NEW"
        [ -x "$RT_NEW/python/bin/python3.14" ] || die "the Python download did not unpack as expected."
        clean_python "$RT_NEW/python/bin/python3.14" -m pip install --no-index --no-deps --no-cache-dir \
            --disable-pip-version-check --no-warn-script-location --quiet "$cffi_whl" "$crypto_whl" \
            || die "the packages could not be installed into the downloaded Python."
        self_test "$RT_NEW/python/bin/python3.14" >/dev/null || die "the downloaded runtime failed its self-test."
        printf '%s\n' "$pins" > "$RT_NEW/PINS"
        rt_changed=1
    fi

    # ---- the running service: never touched mid-switch, and only restarted when this run may start it ----
    local state running=0 restart=0 test_python
    state=$(service_tool state 2>/dev/null) || state=down
    case "$state" in
    busy*) die "$APP_NAME is in the middle of a switch (${state#busy }). Let it finish, then run install.sh again. Nothing was changed." ;;
    esac
    if [ -d "$APPDIR" ] && app_running; then running=1; fi
    if [ "$manage" = true ] && [ "$launch" = 1 ]; then restart=1; fi
    if { [ "$app_changed" = 1 ] || [ "$rt_changed" = 1 ]; } && [ "$running" = 1 ] && [ "$restart" = 0 ]; then
        die "$APP_NAME is running. Choose Quit in its menu bar menu, then run install.sh again. Nothing was changed."
    fi

    # ---- the new code must pass before anything is swapped ----
    if [ "$app_changed" = 1 ]; then
        test_python="$RT/python/bin/python3.14"
        [ -z "${RT_NEW:-}" ] || test_python="$RT_NEW/python/bin/python3.14"
        if [ "$skip_tests" = 1 ]; then
            say "Tests: skipped (--skip-tests)."
        else
            run_suite "$test_python" "$src" "$APP_NEW"
        fi
    fi

    # ---- the saved accounts: counted before anything moves, and a snapshot when the app code changes ----
    local counted
    if { [ "$app_changed" = 1 ] || [ "$rt_changed" = 1 ]; } && [ -f "$SUPPORT/accounts.sqlite3" ]; then
        CENSUS=1
        counted=$(db_tool census "$WORK/census-before" 2>&1) \
            || die "the saved accounts could not be counted before the update ($(last_line "$counted")). Nothing was changed."
        say "Counted before the update: $counted. They are counted again after it."
    fi
    if [ "$app_changed" = 1 ] && data_present; then start_snapshot; fi

    # ---- from here on any failure or interruption puts everything back (cleanup → roll_back) ----
    SWAPPING=1
    if [ "$rt_changed" = 1 ]; then
        if [ "$running" = 1 ]; then stop_service; running=0; fi
        rm -rf "$RT.old"
        RT_SWAPPED=1
        if [ -d "$RT" ]; then mv "$RT" "$RT.old"; fi
        mv "$RT_NEW" "$RT"; RT_NEW=""
    fi
    PY="$RT/python/bin/python3.14"
    self_test "$PY" || die "the installed runtime failed its self-test."

    local code="$APPDIR"            # where the app's files are until the swap
    if [ "$app_changed" = 1 ]; then code="$APP_NEW"; fi

    # The install record autostart_mac reads (accountdeck/platform/autostart_mac.py, MANIFEST_VERSION).
    clean_python "$PY" -c 'import json, sys
launcher, agents, logs, manage, target = sys.argv[1:]
record = {"version": 1, "launcher": launcher, "launch_agents": agents, "logs": logs, "manage": manage == "true"}
with open(target + ".tmp", "w", encoding="utf-8") as handle:
    json.dump(record, handle, indent=2)
    handle.write("\n")
' "$APP/Contents/MacOS/$EXECUTABLE" "$AGENTS_DIR" "$LOGS" "$manage" "$code/install.json"
    mv "$code/install.json.tmp" "$code/install.json"

    # ---- the app bundle (PLATFORM-MAC.md §5): built aside, swapped in only when it differs ----
    local app_version icns bundle_changed=0
    app_version=$(code_version "$code")
    [ -n "$app_version" ] || die "the app version was not found."
    BUNDLE_NEW="$APPS/.$APP_NAME.app.new"
    rm -rf "$BUNDLE_NEW"
    mkdir -p "$BUNDLE_NEW/Contents/MacOS" "$BUNDLE_NEW/Contents/Resources"
    icns="$code/AccountDeck2.icns"
    if [ ! -f "$icns" ] || [ "$(head -c 4 "$icns")" != icns ]; then
        say "Drawing the app icon..."
        icns="$WORK/AccountDeck2.icns"
        clean_python "$PY" "$code/make_icon.py" --icns "$icns" >/dev/null
    fi
    cp "$icns" "$BUNDLE_NEW/Contents/Resources/AccountDeck2.icns"
    printf 'APPL????' > "$BUNDLE_NEW/Contents/PkgInfo"
    clean_python "$PY" -c 'import plistlib, sys
bundle_id, name, executable, version, minimum, target = sys.argv[1:]
info = {
    "CFBundleIdentifier": bundle_id, "CFBundleName": name, "CFBundleDisplayName": name,
    "CFBundleExecutable": executable, "CFBundleIconFile": "AccountDeck2", "CFBundlePackageType": "APPL",
    "CFBundleInfoDictionaryVersion": "6.0", "CFBundleDevelopmentRegion": "en",
    "CFBundleShortVersionString": version, "CFBundleVersion": version,
    "LSMinimumSystemVersion": minimum + ".0", "LSArchitecturePriority": ["arm64"],
    "LSApplicationCategoryType": "public.app-category.utilities",
    "LSUIElement": True,                    # the menu bar item is the app: no Dock icon, no app menu
    "LSMultipleInstancesProhibited": True,  # opening it again reaches the running copy (it opens the dashboard)
    "NSHighResolutionCapable": True, "NSSupportsAutomaticGraphicsSwitching": True,
}
with open(target, "wb") as handle:
    plistlib.dump(info, handle, sort_keys=False)
' "$BUNDLE_ID" "$APP_NAME" "$EXECUTABLE" "$app_version" "$MIN_MACOS" "$BUNDLE_NEW/Contents/Info.plist"
    plutil -lint "$BUNDLE_NEW/Contents/Info.plist" >/dev/null || die "the app's Info.plist is not valid."
    # The process that runs is this copy of the runtime's python, signed with the bundle id: macOS checks that
    # the running code's identity matches the bundle before it allows notifications. pyvenv.cfg points the
    # copy at the runtime's standard library and packages.
    cp -p "$PY" "$BUNDLE_NEW/Contents/MacOS/python3"
    codesign --force --sign - --identifier "$BUNDLE_ID" "$BUNDLE_NEW/Contents/MacOS/python3" >/dev/null 2>&1 \
        || die "the app's python could not be signed."
    printf 'home = %s\ninclude-system-site-packages = true\nversion = %s\n' "$RT/python/bin" "$PY_VERSION" \
        > "$BUNDLE_NEW/Contents/pyvenv.cfg"
    cat > "$BUNDLE_NEW/Contents/MacOS/$EXECUTABLE" <<LAUNCHER
#!/bin/sh
# $APP_NAME launcher, written by install.sh. It execs this bundle's python3 (same process, so the app keeps
# its identity, $BUNDLE_ID), and run.py --app opens the dashboard of the running service or becomes the
# service with the menu bar item.
APPROOT=$(sq "$APPDIR")
HERE=\$(cd "\$(dirname "\$0")" && pwd) || exit 1
cd "\$APPROOT" || exit 1
case "\${1-}" in -psn_*) shift ;; esac
case "\${1-}" in --app) ;; *) set -- --app "\$@" ;; esac
# -E -s, not -I: -I also drops run.py's folder from sys.path, and then accountdeck does not import.
exec "\$HERE/python3" -E -s "\$APPROOT/run.py" "\$@"
LAUNCHER
    chmod 755 "$BUNDLE_NEW/Contents/MacOS/$EXECUTABLE"
    "$BUNDLE_NEW/Contents/MacOS/python3" -E -s -c 'import cryptography.hazmat.primitives.ciphers.aead' 2>/dev/null \
        || die "the app's python does not find its runtime."
    xattr -dr com.apple.quarantine "$BUNDLE_NEW" "$RT" "$code" 2>/dev/null || true
    if [ -d "$APP" ] && diff -r "$APP" "$BUNDLE_NEW" >/dev/null 2>&1; then
        rm -rf "$BUNDLE_NEW"; BUNDLE_NEW=""
        say "App: $APP unchanged."
    else
        bundle_changed=1
    fi

    # ---- the swap: the service is stopped first, the previous code stays until the new one proved itself ----
    if [ "$running" = 1 ] && [ "$restart" = 1 ] && { [ "$app_changed" = 1 ] || [ "$bundle_changed" = 1 ]; }; then
        stop_service
        running=0
    fi
    # Nothing of this install runs now (stopped above, or a running copy made the install stop earlier).
    if [ -n "$SNAP_PART" ]; then finish_snapshot; fi
    if [ "$app_changed" = 1 ]; then
        if [ -d "$APPDIR" ]; then
            mkdir -p "$BACKUPS"
            BACKUP="$BACKUPS/$(date +%Y%m%d-%H%M%S)-v${OLD_VERSION:-unknown}"
            while [ -e "$BACKUP" ]; do BACKUP="$BACKUP-1"; done
        fi
        APP_SWAPPED=1                   # each flag is set before its move: take_back moves back only what has moved
        if [ -n "$BACKUP" ]; then mv "$APPDIR" "$BACKUP"; fi
        mv "$APP_NEW" "$APPDIR"; APP_NEW=""
        say "App code: installed $app_version${BACKUP:+ (the previous app is in $BACKUP)}."
    fi
    if [ "$bundle_changed" = 1 ]; then
        rm -rf "$APP.old"
        BUNDLE_SWAPPED=1
        if [ -e "$APP" ]; then mv "$APP" "$APP.old"; fi
        mv "$BUNDLE_NEW" "$APP"; BUNDLE_NEW=""
        say "App: $APP"
        if [ "$manage" = true ] && [ -x "$LSREGISTER" ] && [ -z "$root_override" ]; then
            "$LSREGISTER" -f "$APP" 9>&- >/dev/null 2>&1 || true    # Finder and Spotlight pick up the name and icon
        fi
    fi

    # ---- Open at login first: then launchd's job is what runs, and what comes back after a crash ----
    local started=0 before=""
    if [ "$restart" = 1 ] && [ "$running" = 0 ]; then START_TRIED=1; fi
    if [ "$at_login" = 1 ]; then
        if [ "$START_TRIED" = 1 ]; then before=$(job_pid); fi
        (cd "$APPDIR" && "$PY" -E -s "$APPDIR/run.py" --set-autostart on) 9>&- \
            || die "Open at login could not be switched on."
        # Loading the agent starts it (RunAtLoad): unless its job ran already, that start is the one.
        if [ "$START_TRIED" = 1 ] && [ -z "$before" ] && launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then started=1; fi
        [ "$manage" = true ] || say "Open at login: the LaunchAgent was written to $AGENTS_DIR (not loaded: --prefix)."
    fi

    # ---- the new service must prove itself, or the previous one goes back ----
    local answer build=- healthy="Healthy: version $app_version answers on /_health, its ticks are moving and it has not stalled."
    if [ -f "$APPDIR/build.json" ]; then build="$APPDIR/build.json"; fi
    if [ "$restart" = 1 ] && [ "$running" = 1 ]; then       # nothing changed, so it keeps running; opening the app shows its dashboard
        answer=$(service_tool wait-up "$app_version" "$build" 15 2>&1) || answer="its check did not run"
        if [ "$answer" = ok ]; then say "$healthy"; else say "$APP_NAME is running but does not look healthy: $answer"; fi
        open "$APP" 9>&- || die "the app is installed at $APP but could not be opened."
    elif [ "$restart" = 1 ]; then
        say "Starting the service and checking its health (up to $HEALTH_WAIT s)..."
        if [ "$started" = 0 ]; then start_service || die "the service could not be started."; fi
        answer=$(service_tool wait-up "$app_version" "$build" "$HEALTH_WAIT" 2>&1) || answer="its check did not run"
        if [ "$answer" != ok ]; then
            printf '%s %s failed its health check: %s\n' "$APP_NAME" "$app_version" "$answer" >&2
            printf 'Logs: %s and %s\n' "$SUPPORT/logs/accountdeck2.log" "$LOGS/launchd.log" >&2
            exit 1
        fi
        say "$healthy"
        census_after
    elif [ "$launch" = 1 ]; then
        census_after
        if [ "$bundle_changed" = 1 ] && app_running; then
            say "$APP_NAME is running the previous app: choose Quit in its menu bar menu and open it again."
        else
            open "$APP" 9>&- || die "the app is installed at $APP but could not be opened."
        fi
    fi

    census_after                    # without a restart: the count after the swap

    # ---- it holds: the old copies go, the dated backups and snapshots stay (the last few) ----
    SWAPPING=0
    rm -rf "$RT.old" "$APP.old" "$APPDIR.old"
    prune_dated "$BACKUPS" "$BACKUPS_KEPT"
    prune_dated "$UPDATE_BACKUPS" "$SNAPSHOTS_KEPT"
    rm -rf "$WORK"; WORK=""

    local update="run install.sh from a newer AccountDeck2-mac package"
    if [ -f "$APPDIR/get.sh" ]; then update="bash $(sq "$APPDIR/get.sh")"; fi
    say ""
    if [ "$HAD_APP" = 0 ]; then
        say "$APP_NAME $app_version is installed."
        say "  The app:      $APP"
        say "  Where it is:  in the menu bar at the top right of the screen. It has no Dock icon: the menu bar item is the"
        say "                app, and its menu opens the dashboard."
        say "  First run:    $FIRST_RUN_URL"
        if [ -n "$CENSUS_LINE" ]; then say "  Your data:    $CENSUS_LINE"; fi
        if [ -n "$SNAP" ]; then say "  Snapshot:     the saved sign-ins as they were before this install are in $SNAP"; fi
        say "  To update:    $update"
        say "  To uninstall: bash $(sq "$APPDIR/uninstall.sh") (your data stays unless you add --delete-data)"
    else
        if [ "$app_changed$rt_changed$bundle_changed" = 000 ]; then
            say "$APP_NAME $app_version is installed: nothing needed changing."
        elif [ "$OLD_VERSION" = "$app_version" ]; then
            say "$APP_NAME $app_version is installed again."
        else
            say "$APP_NAME is updated from ${OLD_VERSION:-an unknown version} to $app_version."
        fi
        if [ -n "$CENSUS_LINE" ]; then say "$CENSUS_LINE"; fi
        if [ -n "$SNAP" ]; then
            say "A snapshot of the saved sign-ins from before the update is in $SNAP"
            say "(a last resort, never restored automatically; the last $SNAPSHOTS_KEPT are kept)."
        fi
        say "Your data in $SUPPORT was kept."
        say "To update later: $update"
        say "To remove the app: bash $(sq "$APPDIR/uninstall.sh")"
    fi
    say "Check its health any time: $(sq "$PY") -E -s $(sq "$APPDIR/run.py") --doctor"
}

main "$@"
exit 0
