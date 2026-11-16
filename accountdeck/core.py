"""Storage primitives: user-facing errors, atomic writes, the encrypted vault with its generation ring,
the SQLite store (schema v2), write-ahead file transactions and the per-user owner lock."""
from __future__ import annotations

import base64
import calendar
import fcntl
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import uuid
import zlib

# The owner lock (§9.1, flock) lives with the other macOS code; it is re-exported here for its callers
# (run.py, cli_accounts). platform.locks imports nothing else from the package.
from .platform.locks import OwnerLock  # noqa: F401  (re-export)


# "system": macOS refused something (a launch, a folder, free disk space) rather than a provider.
# "antigravity": `agy` answered something AccountDeck cannot read (as "codex" is for Codex); "notice_required": an
# Antigravity Save, Add or Switch before the user accepted the notice about Google's terms.
ERROR_CODES = frozenset({
    "invalid", "not_found", "busy", "login_needed", "reauth_required", "blocked", "identity",
    "unsupported", "network", "rate_limited", "timeout", "vault", "recovery", "not_installed",
    "system", "codex", "safety", "cancelled", "confirm_required", "needs_reconfirm", "stale_plan",
    "outage", "read_only", "antigravity", "notice_required",
})


class DeckError(Exception):
    """A failure the user can act on (§3.1). `str(exc)` is the message shown and `code` one of
    ERROR_CODES; `retry_after` is set (seconds) when a provider asked us to wait; `plan` carries a
    fresh SwitchPlan for `confirm_required` and `needs_reconfirm`."""

    def __init__(self, message, code="invalid", retry_after=None, *, plan=None):
        super().__init__(message)
        self.code, self.retry_after, self.plan = code, retry_after, plan


def __getattr__(name):
    # SafetyViolation lives in config.py, which imports DeckError from this module; resolving it on
    # first use keeps the two modules free of an import cycle.
    if name == "SafetyViolation":
        from .config import SafetyViolation
        return SafetyViolation
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    """Replaces `path` with `data` durably: the bytes reach the disk (F_FULLFSYNC, which a plain fsync
    does not guarantee on macOS) before the rename, and the rename itself is synced through the folder."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            fcntl.fcntl(stream.fileno(), fcntl.F_FULLFSYNC)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def vault_cipher(data_dir, *, keychain=None):
    """The Vault's `protect` cipher (docs/mac/PLAN.md D4, §1.5): AES-256-GCM under a per-data-folder key
    in the keychain (`keychain=None` is the login keychain, which safe mode refuses)."""
    from .platform.keychain import VaultCipher   # imports core: taken in on use
    return VaultCipher(data_dir, keychain=keychain)


# Vault files end in ".dpapi" because the format predates the Mac port (the Windows build protected them
# with DPAPI). The suffix is part of the on-disk format: existing data folders keep loading as they are.
VAULT_SUFFIX = ".dpapi"
GENERATIONS = 3          # current copy + g1 + g2 of every account profile (A3)
PREV_TTL = 2 * 3600      # how long a guarded overwrite keeps the displaced copy (A9)
_ORPHAN_TTL = 3600       # temporary files an interrupted atomic_write left behind
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,159}")
_ACCOUNT_KEY = re.compile(r"[0-9a-f]{32}")      # the ids Store.save_account gives out
# Antigravity's records carry a prefix of their own, so no build ever takes one for another app's: a bare <32 hex>
# record is adopted as a Claude account after a database restore (Engine._readopt, in 2.1.0 too), and every build
# shows an open `transaction-` record as an interrupted switch that blocks all switching. Frozen once released.
AGY_RECORD_PREFIX = "agy-"                      # agy-<32 hex account id>: a saved sign-in, the usual ring of copies
AGY_TRANSACTION_PREFIX = "agy-transaction-"     # agy-transaction-<32 hex>: a switch under way, one copy
AGY_ENROLLMENT = "agy-enrollment"               # an Add under way, one copy
_AGY_ACCOUNT_KEY = re.compile(r"agy-([0-9a-f]{32})")
_AGY_TRANSACTION_KEY = re.compile(r"agy-transaction-[0-9a-f]{32}")
_SINGLE_COPY = ("transaction-", "quarantine-", AGY_TRANSACTION_PREFIX, AGY_ENROLLMENT)


def agy_key(account_id):
    """The vault key of an Antigravity account's saved sign-in (`agy-<id>`)."""
    if not isinstance(account_id, str) or not _ACCOUNT_KEY.fullmatch(account_id):
        raise ValueError("an Antigravity account id is 32 lower-case hex digits")
    return AGY_RECORD_PREFIX + account_id


def agy_transaction_key():
    """A new key for the record of an Antigravity switch (`agy-transaction-<32 hex>`)."""
    return AGY_TRANSACTION_PREFIX + uuid.uuid4().hex


def _blob_digest(blob):
    return digest(blob)[:16]


class Vault:
    """Encrypted JSON records, one file per key; `protect(data, decrypt=False)` is the cipher
    (vault_cipher in the service, a stand-in in tests and the demo).

    Account profiles keep a ring of GENERATIONS copies (`<key>.dpapi`, `<key>.g1.dpapi`,
    `<key>.g2.dpapi`); transaction and quarantine records, and Antigravity's switch and Add records, are
    single-copy. Keys are ASCII letters, digits and dashes, so a key can never name a path outside the vault."""

    def __init__(self, root, protect):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.protect = protect
        self.lock = threading.RLock()

    def path(self, key):
        return self._file(key)

    def put(self, key, value, *, generation=True):
        """Store value as the current copy and return its digest.

        With `generation` (account profiles) the replaced copy moves to g1 and g1 to g2. Storing a
        value equal to the current one changes nothing, so repeated captures never push the older
        fallbacks out of the ring."""
        data = json_bytes(value)
        with self.lock:
            current = self._file(key)
            rotate = generation and not key.startswith(_SINGLE_COPY) and current.exists()
            if rotate and self._holds(current, data):
                return _blob_digest(current.read_bytes())
            blob = self.protect(data)
            if rotate:
                self._shift(key, GENERATIONS - 1)
            atomic_write(current, blob)
        return _blob_digest(blob)

    def get(self, key, generation=0):
        """Decrypt one generation (0 = current); DeckError("vault") when missing or unreadable."""
        try:
            raw = self._slot(key, generation).read_bytes()
        except FileNotFoundError:
            raise DeckError("This account's saved sign-in is missing. Reconnect it.", "vault") from None
        return self._decode(raw)

    def generations(self, key):
        """Stored generations, newest first: [{"n", "at", "digest"}]. The digest (of the encrypted
        file) links a generation to its Store.generations() metadata and reveals nothing secret."""
        found = []
        for n in range(GENERATIONS):
            path = self._slot(key, n)
            try:
                raw, at = path.read_bytes(), path.stat().st_mtime
            except FileNotFoundError:
                continue
            found.append({"n": n, "at": at, "digest": _blob_digest(raw)})
        return found

    def newest_good(self, key, is_good):
        """(n, profile) of the newest generation that decrypts and satisfies is_good(profile), or
        None. Restores prefer it (A3), so one bad capture never strands an account."""
        for generation in self.generations(key):
            try:
                profile = self.get(key, generation["n"])
            except DeckError:
                continue
            if is_good(profile):
                return generation["n"], profile
        return None

    def restore_generation(self, key, n):
        """Make generation n current; the newer generations each move one slot older. Returns the
        promoted digest."""
        with self.lock:
            source = self._slot(key, n)
            if not source.exists():
                raise DeckError("That saved sign-in is no longer available.", "vault")
            if n:
                staged = self._file(key, ".restore")
                self._copy(source, staged)      # the shift below overwrites slot n
                self._shift(key, n)
                os.replace(staged, self._file(key))
            return _blob_digest(self._file(key).read_bytes())

    def put_guarded(self, key, incoming, source, identity_of, freshness_of, *, previous=None):
        """Freshness-guarded overwrite for rotating credentials (Codex, A9).

        `incoming` replaces the stored copy only when identity_of() gives the same identity and
        freshness_of() says it is at least as fresh; a tie goes to incoming only when `source` is
        "live" (read from the real credential file), since that copy is the one the app itself uses.
        The displaced copy is kept as `<key>.prev` for PREV_TTL. Returns "accepted", "kept"
        (the stored copy is fresher) or "conflict" (identity differs or is unknown); the caller
        records conflicts. The generation ring is never used here: an older refresh token is spent,
        and restoring one would trip the provider's reuse detection. `previous` is the login `incoming`
        was renewed from: while that is still the stored copy, it is spent and freshness has no say."""
        identity = identity_of(incoming)
        data = json_bytes(incoming)
        with self.lock:
            current = self._file(key)
            if identity is None:
                return "conflict"
            if current.exists():
                try:
                    stored = self._decode(current.read_bytes())
                except DeckError:
                    stored = None       # unreadable: replace it and keep its bytes as .prev
                if stored is not None and identity_of(stored) is not None:
                    if identity_of(stored) != identity:
                        return "conflict"
                    if stored == json.loads(data):
                        return "accepted"
                    if previous is None or stored != previous:
                        fresh, held = freshness_of(incoming), freshness_of(stored)
                        if held is not None and (fresh is None or fresh < held):
                            return "kept"
                        if fresh == held and source != "live":
                            return "kept"
                atomic_write(self._file(key, ".prev"), current.read_bytes())
            atomic_write(current, self.protect(data))
        return "accepted"

    def quarantine(self, key, raw_bytes):
        """Keep unreadable credential bytes, encrypted, as `quarantine-<key>-<ms>` for review instead
        of deleting them (A9); returns the record key."""
        self._file(key)                         # refuses a key that is not a plain identifier
        with self.lock:
            stamp = int(time.time() * 1000)
            while self._file(f"quarantine-{key}-{stamp}").exists():
                stamp += 1
            name = f"quarantine-{key}-{stamp}"
            atomic_write(self._file(name), self.protect(bytes(raw_bytes)))
        return name

    def prune(self, now=None):
        """Delete .prev copies older than PREV_TTL and orphaned temporary files; returns their names."""
        now = time.time() if now is None else now
        expired = [(path, PREV_TTL) for path in self.root.glob("*.prev" + VAULT_SUFFIX)]
        expired += [(path, _ORPHAN_TTL) for path in self.root.glob(".write-*")]
        removed = []
        with self.lock:
            for path, ttl in expired:
                try:
                    if now - path.stat().st_mtime > ttl:
                        path.unlink()
                        removed.append(path.name)
                except OSError:
                    pass                        # already gone, or briefly held open: the next prune retries
        return removed

    def remove(self, key):
        """Delete every copy of key: all generations, the .prev copy and a staged restore."""
        with self.lock:
            for n in range(GENERATIONS):
                self._slot(key, n).unlink(missing_ok=True)
            self._file(key, ".prev").unlink(missing_ok=True)
            self._file(key, ".restore").unlink(missing_ok=True)

    def account_keys(self):
        """The keys of the accounts' own records, without the transaction, stash and other records beside them.
        Antigravity's (`agy-<id>`) are not among them: they belong to `agy_account_keys`."""
        return {key for key in self._current_keys() if _ACCOUNT_KEY.fullmatch(key)}

    def agy_account_keys(self):
        """The keys of the saved Antigravity sign-ins (`agy-<32 hex>`), current copies only."""
        return {key for key in self._current_keys() if _AGY_ACCOUNT_KEY.fullmatch(key)}

    def agy_transaction_keys(self):
        """The keys of Antigravity switch records (`agy-transaction-<32 hex>`), sorted by name, which says nothing of
        their age (the names are random: a caller wanting the oldest compares the records' own `at`).
        `recover_transactions` never lists them: Antigravity puts its own right."""
        return sorted(key for key in self._current_keys() if _AGY_TRANSACTION_KEY.fullmatch(key))

    def _current_keys(self):
        return (path.name.removesuffix(VAULT_SUFFIX) for path in self.root.glob("*" + VAULT_SUFFIX))

    def _file(self, key, suffix=""):
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise DeckError("Invalid credential identifier.")
        return self.root / f"{key}{suffix}{VAULT_SUFFIX}"

    def _slot(self, key, n):
        if n not in range(GENERATIONS):
            raise ValueError(f"generation must be between 0 and {GENERATIONS - 1}")
        return self._file(key, f".g{n}" if n else "")

    def _decode(self, raw):
        try:
            return json.loads(self.protect(raw, decrypt=True))
        except ValueError:
            raise DeckError("This saved sign-in could not be read. Reconnect this account.", "vault") from None

    def _holds(self, path, data):
        try:
            return self._decode(path.read_bytes()) == json.loads(data)
        except (DeckError, OSError):
            return False

    def _shift(self, key, count):
        """Move generations 0..count-1 one slot older (overwriting slot count). The current copy
        stays in place until the caller replaces it, so a crash never leaves a key without one."""
        for n in range(count - 1, 0, -1):
            if self._slot(key, n).exists():
                os.replace(self._slot(key, n), self._slot(key, n + 1))
        current = self._slot(key, 0)
        if current.exists():
            self._copy(current, self._slot(key, 1))

    @staticmethod
    def _copy(source, target):
        """Copy bytes and modification time, so a generation's age survives rotation."""
        stat = source.stat()
        atomic_write(target, source.read_bytes())
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))


SCHEMA_VERSION = 2
# Who manages an account's sign-in. "v2": this app. "on_pc": imported from the PC while it was still
# signed in there; the row has history but no secret until a hand-over bundle fills it. "moved": the PC
# build's mark for an account it exported. "v1" / "v2-pending": AccountDeck 1 era owners on the PC.
# Every value stays valid so any data folder loads; only "v2" (and the AccountDeck 1 owners, which behave
# like it for reads) has a usable sign-in here.
OWNERS = ("v2", "v2-pending", "v1", "moved", "on_pc")
FROZEN_OWNERS = frozenset({"moved", "on_pc"})


ON_PC_MESSAGE = ("This account is still signed in on the PC, so this Mac never uses it. Hand it over from the PC "
                 "to use it here.")
MOVED_MESSAGE = "This account was moved to another computer, so this Mac does not use or renew its sign-in."


def is_frozen(row, store=None):
    """True for an account whose sign-in belongs to another computer ("moved", "on_pc"): it is never
    read, renewed, saved over or switched to. `store` is accepted for older call sites and unused."""
    return bool(row) and row.get("owner") in FROZEN_OWNERS


class FrozenAccountError(DeckError):
    """Refuses to use or save the sign-in of an account another computer owns (code `read_only`)."""

    def __init__(self, row=None):
        super().__init__(ON_PC_MESSAGE if (row or {}).get("owner") == "on_pc" else MOVED_MESSAGE, "read_only")


EVENT_LIMIT = 500
HISTORY_LIMIT = 700
SNAPSHOT_EVERY = 900                     # one v1-style snapshot per account per quarter hour
GENERATION_ROWS = 10                     # metadata rows kept per account (the vault ring holds 3)
_DAY = 86400
_SNAPSHOT_DAYS, _READING_DAYS, _REFILL_DAYS, _ALERT_DAYS = 90, 7, 30, 7

_ACCOUNTS = """
CREATE TABLE accounts(id TEXT PRIMARY KEY, provider TEXT NOT NULL, identity TEXT NOT NULL,
  label TEXT NOT NULL, email TEXT, plan TEXT, org TEXT, created REAL NOT NULL, updated REAL NOT NULL,
  error TEXT, error_code TEXT, next_refresh REAL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0,
  imported_from TEXT, owner TEXT NOT NULL DEFAULT 'v2', credential_state TEXT NOT NULL DEFAULT 'unknown',
  last_good_usage TEXT, last_good_at REAL, expires_at REAL, last_outcome TEXT, paused_until REAL,
  flags TEXT, UNIQUE(provider, identity));
"""

_TABLES = """
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at REAL, kind TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, account TEXT, at REAL, usage TEXT);
CREATE TABLE IF NOT EXISTS readings(id INTEGER PRIMARY KEY, account TEXT NOT NULL,
  window_id TEXT NOT NULL, at REAL NOT NULL, used REAL NOT NULL, reset REAL);
CREATE TABLE IF NOT EXISTS refills(id INTEGER PRIMARY KEY, account TEXT NOT NULL,
  window_id TEXT NOT NULL, reset_at REAL NOT NULL, confirmed_at REAL NOT NULL, previous_used REAL,
  UNIQUE(account, window_id, reset_at));
CREATE TABLE IF NOT EXISTS generations(id INTEGER PRIMARY KEY, account TEXT NOT NULL,
  digest TEXT NOT NULL, at REAL NOT NULL, source TEXT NOT NULL, health TEXT, meta TEXT,
  UNIQUE(account, digest));
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, level TEXT,
  account TEXT, window_id TEXT, title TEXT NOT NULL, message TEXT, at REAL NOT NULL,
  snoozed_until REAL, dismissed INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS snapshots_by_account ON snapshots(account, at);
CREATE INDEX IF NOT EXISTS readings_by_window ON readings(account, window_id, at);
CREATE INDEX IF NOT EXISTS readings_by_time ON readings(at);
CREATE INDEX IF NOT EXISTS refills_by_time ON refills(confirmed_at);
CREATE INDEX IF NOT EXISTS generations_by_account ON generations(account, at);
CREATE INDEX IF NOT EXISTS alerts_by_time ON alerts(at);
"""

# Antigravity's accounts live in a table of their own, inside schema 2: a row of an app they do not know in
# `accounts` stops every older build's usage reads, and none of them looks at a table it does not know. The
# e-mail (lower case) is the account's identity; `usage` is the last normalised reading (JSON), `saved_at` when
# its vault record (`agy-<id>`) was last written. Its name and columns are frozen once released.
AGY_TABLE = "antigravity_accounts"
_AGY_TABLE = """
CREATE TABLE IF NOT EXISTS antigravity_accounts(id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL, plan TEXT, tier TEXT, created REAL NOT NULL, updated REAL NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0, saved_at REAL, usage TEXT, usage_at REAL, error TEXT, error_code TEXT,
  flags TEXT)
"""
_AGY_COLUMNS = ("id", "email", "label", "plan", "tier", "created", "updated", "sort_order", "saved_at", "usage",
                "usage_at", "error", "error_code", "flags")
_SELECT_AGY = "SELECT " + ", ".join(_AGY_COLUMNS) + " FROM antigravity_accounts"
_AGY_UPDATABLE = frozenset({"label", "plan", "tier", "saved_at", "usage", "usage_at", "error", "error_code", "flags"})
_AGY_JSON = ("usage", "flags")
# Every table of schema 2: a database with a row in any of them holds data a migration backs up first.
_V2_TABLES = ("accounts", "settings", "events", "snapshots", "readings", "refills", "generations", "alerts")

_ACCOUNT_COLUMNS = (
    "id", "provider", "identity", "label", "email", "plan", "org", "created", "updated", "error",
    "error_code", "next_refresh", "sort_order", "imported_from", "owner", "credential_state",
    "last_good_usage", "last_good_at", "expires_at", "last_outcome", "paused_until", "flags",
)
_SELECT_ACCOUNTS = "SELECT " + ", ".join(_ACCOUNT_COLUMNS) + " FROM accounts"
_UPDATABLE = frozenset(_ACCOUNT_COLUMNS) - {"id", "provider", "identity", "created", "updated"}
_JSON_COLUMNS = ("last_good_usage", "flags")
_ALERT_ID = re.compile(r"al_(\d+)")


def _statements(script):
    return [part.strip() for part in script.split(";") if part.strip()]


def _encode(value):
    return None if value is None else json.dumps(value, separators=(",", ":"))


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _account(row):
    account = dict(row)
    account["last_good_usage"] = json.loads(account["last_good_usage"]) if account["last_good_usage"] else None
    account["flags"] = json.loads(account["flags"]) if account["flags"] else {}
    return account


def _agy_account(row):
    account = dict(row)
    account["usage"] = json.loads(account["usage"]) if account["usage"] else None
    account["flags"] = json.loads(account["flags"]) if account["flags"] else {}
    return account


def _agy_email(email):
    """The identity of an Antigravity account: its e-mail, trimmed and in lower case."""
    if not isinstance(email, str) or "@" not in email or not email.strip().isprintable() or len(email.strip()) > 320:
        raise ValueError("an Antigravity account needs its e-mail")
    return email.strip().lower()


# ---- the database file: checked, backed up, restored ----

DB_FILE = "accounts.sqlite3"
BACKUP_DIR = "backups"
BACKUPS_KEPT = 7
RESTORED = "db_restored"                 # a setting a restored copy carries until the engine has dealt with it
WAL_LIMIT = 32 * 1024 * 1024             # a checkpointed WAL is cut back to this: it would keep its largest size
_BACKUP_NAME = re.compile(r"accounts-(\d{8}T\d{6}Z)\.sqlite3\.gz")
_STAMP = "%Y%m%dT%H%M%SZ"
_DAMAGE_CODES = frozenset({11, 26})      # SQLITE_CORRUPT, SQLITE_NOTADB; any other error says nothing about the file


class DatabaseDamaged(DeckError):
    """The database cannot be used and no good backup exists. Nothing was changed: the app stops here rather
    than start empty beside a vault full of logins."""

    TITLE = "AccountDeck 2 can't open its database"

    def __init__(self):
        super().__init__("accounts.sqlite3 is damaged or missing and there is no good backup to restore, so AccountDeck 2 "
                         "did not start and changed nothing. Your saved logins stay in the encrypted vault. To start "
                         "over without them, move accounts.sqlite3 and the vault and backups folders out of the data "
                         "folder.", "system")


def _is_damage(exc):
    return (getattr(exc, "sqlite_errorcode", None) or 0) & 0xff in _DAMAGE_CODES


def _read_only(path, *, immutable=False):
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro" + ("&immutable=1" if immutable else "")
    return sqlite3.connect(uri, uri=True, timeout=10)


def quick_check(path, *, immutable=False):
    """PRAGMA quick_check on a read-only connection of its own: True for a sound file, False for a damaged one.
    Anything else that goes wrong (locked, disk I/O, no permission) says nothing about the file and is raised."""
    try:
        db = _read_only(path, immutable=immutable)
        try:
            return db.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        if _is_damage(exc):
            return False
        raise


def _examine(path):
    """"sound", "damaged", or "missing" for no file, an empty one or one without tables. Reads only: read-only,
    and immutable when no WAL exists, so not even sidecar files appear."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return "missing"
    wal = path.with_name(path.name + "-wal")
    immutable = not (wal.is_file() and wal.stat().st_size > 0)
    if not quick_check(path, immutable=immutable):
        return "damaged"
    db = _read_only(path, immutable=immutable)
    try:
        return "sound" if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone() else "missing"
    finally:
        db.close()


def _backups(folder):
    """The backup files, newest stamp first. Stamps more than a day ahead (a clock that ran fast, or one set back
    since) follow, newest first among them, then names that are no date: neither is restored first nor kept for good,
    as pruning takes the tail."""
    try:
        names = [name for name in os.listdir(folder) if _BACKUP_NAME.fullmatch(name)]
    except OSError:
        return []
    ahead = time.time() + 86400

    def rank(name):
        try:
            at = _backup_time(name)
        except ValueError:
            return (2, 0.0, name)
        return (1 if at > ahead else 0, -at, name)
    return [Path(folder) / name for name in sorted(names, key=rank)]


def _backup_time(path):
    return float(calendar.timegm(time.strptime(_BACKUP_NAME.fullmatch(Path(path).name).group(1), _STAMP)))


def _private_file(path):
    return os.fdopen(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "wb")


def _settle_file(stream):
    """The bytes of an open file reach the disk (F_FULLFSYNC: atomic_write's reason)."""
    stream.flush()
    fcntl.fcntl(stream.fileno(), fcntl.F_FULLFSYNC)


def _holds_saved_data(root):
    """True when a missing or empty database can't be a first start: backups or account records exist, an
    Antigravity account's (`agy-<id>`) included."""
    if _backups(Path(root) / BACKUP_DIR):
        return True
    try:
        names = os.listdir(Path(root) / "vault")
    except OSError:
        return False
    return any(_ACCOUNT_KEY.fullmatch(key) or _AGY_ACCOUNT_KEY.fullmatch(key)
               for key in (name.removesuffix(VAULT_SUFFIX) for name in names))


def _restore(root, now, why):
    """Puts the newest backup that passes quick_check in place of the database. In this order, so a crash anywhere
    leaves what the next start deals with: the copy is unpacked, checked and given its RESTORED note; only then do
    the old files move aside (never deleted) and the copy take their place. DatabaseDamaged, nothing changed, when no
    backup is good. The vault is not touched: it holds every login, and the engine corrects what the restored rows
    say about them (Engine._reconcile_restored)."""
    root = Path(root)
    folder, work = root / BACKUP_DIR, root / ".accounts-restoring"
    work.with_name(work.name + "-journal").unlink(missing_ok=True)      # of a crash during the note below
    try:
        for backup in _backups(folder):
            try:
                with gzip.open(backup, "rb") as packed, _private_file(work) as plain:
                    shutil.copyfileobj(packed, plain, 1 << 20)
            except (EOFError, gzip.BadGzipFile, zlib.error):
                continue
            if quick_check(work, immutable=True):
                break
        else:
            raise DatabaseDamaged()
        aside = folder / f"damaged-{time.strftime(_STAMP, time.gmtime(now))}"
        aside.mkdir(mode=0o700, exist_ok=True)
        db = sqlite3.connect(work)
        try:
            db.execute("INSERT OR REPLACE INTO settings VALUES(?, ?)",
                       (RESTORED, json.dumps({"why": why, "at": now, "backup_at": _backup_time(backup),
                                              "aside": aside.name})))
            db.commit()
        finally:
            db.close()
        with open(work, "rb+") as stream:
            _settle_file(stream)
        for suffix in ("", "-wal", "-shm"):
            part = root / (DB_FILE + suffix)
            if part.exists():
                os.replace(part, aside / part.name)
        os.replace(work, root / DB_FILE)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        work.unlink(missing_ok=True)


class Store:
    """Accounts, readings and app state in SQLite (schema v2, `PRAGMA user_version`).

    No credentials or raw provider payloads enter this database: `last_good_usage` holds the last
    successful normalized reading and every other column is metadata. Antigravity's accounts are in a table
    of their own (`agy_*` methods), never in `accounts` or a table shared with it. `clock` supplies timestamps
    (the demo passes its virtual clock). The file is checked as it opens (`_open_checked`, which puts back a
    backup when it is damaged); `check_integrity` and `backup` run on connections of their own, off `lock`."""

    def __init__(self, root, *, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.checked, self.backed_up, self.backups_kept = None, None, 0     # `care()`
        self.migrated = None     # what this open added to the schema (`_migrate`), for the engine's log
        self._open_checked()
        self.db = sqlite3.connect(self.root / DB_FILE, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute(f"PRAGMA journal_size_limit={WAL_LIMIT}")
            self.db.execute("PRAGMA checkpoint_fullfsync=ON")
            self._migrate()
        except BaseException:
            self.db.close()
            raise

    def close(self):
        with self.lock:
            self.db.close()

    # ---- the file: integrity and backups ----
    def _open_checked(self):
        """Before the database opens. A sound file, or a first start, changes nothing. A damaged file, or a missing
        or empty one beside saved accounts or backups (starting empty would hide them), is replaced by its newest
        good backup (`_restore`), or stops the start (DatabaseDamaged). Empty counts a file without tables: sqlite
        reads the first checkpoint's WAL as a sound empty database when that WAL is gone."""
        path = self.root / DB_FILE
        started = time.perf_counter()
        found = _examine(path)
        if found == "damaged" or (found == "missing" and _holds_saved_data(self.root)):
            _restore(self.root, self.clock(), found)
        self.checked = {"result": "ok", "at": self.clock(), "ms": round((time.perf_counter() - started) * 1000)}
        self._note_backups()

    def _note_backups(self):
        found = _backups(self.root / BACKUP_DIR)
        self.backups_kept = len(found)
        try:
            self.backed_up = {"at": _backup_time(found[0]), "bytes": found[0].stat().st_size} if found else None
        except OSError:
            self.backed_up = None

    def care(self):
        """What /_health and the state say about the file: read from memory, no query and no lock."""
        return {"quick_check": self.checked, "last_backup": self.backed_up, "backups": self.backups_kept}

    def check_integrity(self):
        """quick_check of the live file ("ok", "damaged", or "error" when the check itself could not run), on a
        connection of its own: in WAL mode nothing waits for it."""
        started = time.perf_counter()
        try:
            result = "ok" if quick_check(self.root / DB_FILE) else "damaged"
        except (sqlite3.Error, OSError):
            result = "error"
        self.checked = {"result": result, "at": self.clock(), "ms": round((time.perf_counter() - started) * 1000)}
        return result

    def backup(self):
        """One gzipped copy of the database in backups/ (folder 0700, files 0600), BACKUPS_KEPT kept. True when it
        is kept; False, keeping nothing, when the copy fails quick_check (a sound newest backup stays the newest).
        The sqlite backup API in one step on connections of its own: that is one WAL read snapshot, so no writer
        waits for it, and `lock` is not taken. Files named damaged-* are never pruned. Raises when the disk refuses."""
        folder = self.root / BACKUP_DIR
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
        work, packed = folder / ".copy.tmp", folder / ".packed.tmp"
        now = self.clock()
        try:
            for leftover in (work, folder / ".copy.tmp-journal"):
                leftover.unlink(missing_ok=True)
            os.close(os.open(work, os.O_CREAT | os.O_WRONLY, 0o600))
            try:
                source, target = _read_only(self.root / DB_FILE), sqlite3.connect(work)
                try:
                    source.backup(target)
                    target.execute("PRAGMA journal_mode=DELETE")       # a file of its own, without WAL sidecars
                    sound = target.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
                finally:
                    target.close()
                    source.close()
            except sqlite3.DatabaseError as exc:
                if not _is_damage(exc):
                    raise
                sound = False
            if not sound:
                return False
            with _private_file(packed) as raw, open(work, "rb") as plain:
                with gzip.GzipFile(filename="", mode="wb", compresslevel=1, fileobj=raw, mtime=int(now)) as zipped:
                    shutil.copyfileobj(plain, zipped, 1 << 20)
                _settle_file(raw)
            os.replace(packed, folder / f"accounts-{time.strftime(_STAMP, time.gmtime(now))}.sqlite3.gz")
        finally:
            for leftover in (work, packed):
                leftover.unlink(missing_ok=True)
        for old in _backups(folder)[BACKUPS_KEPT:]:
            old.unlink(missing_ok=True)
        self._note_backups()
        return True

    def _migrate(self):
        """Create schema v2 in a new folder. One transaction: all or nothing. A schema-v2 database (every
        AccountDeck 2 build) opens unchanged but for the one table this build adds inside schema 2,
        `antigravity_accounts`: created when it is missing, after a `backup()` of a database that holds data.
        The version stays 2, because older builds refuse a newer one and ignore a table they do not know. An
        older or newer database is refused before anything changes."""
        with self.lock:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            adding = version == SCHEMA_VERSION and not self._has_table(AGY_TABLE)
            backup = None
            if adding and self._holds_rows():
                # Best effort: the table is created in one transaction that changes no other table, so a
                # backup that cannot be made (a full disk) does not stop the start; the engine logs it.
                try:
                    backup = "ok" if self.backup() else "failed"
                except (OSError, sqlite3.Error):
                    backup = "failed"
            self.db.execute("BEGIN IMMEDIATE")
            try:
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise DeckError("This data was saved by a newer AccountDeck. Update AccountDeck 2 to open it.",
                                    "unsupported")
                if version < SCHEMA_VERSION:
                    if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone():
                        # Only AccountDeck 1 (Windows) wrote an unversioned accounts table.
                        raise DeckError("This folder holds AccountDeck 1 data, which this Mac build cannot open.",
                                        "unsupported")
                    for statement in _statements(_ACCOUNTS + _TABLES):
                        self.db.execute(statement)
                    self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self.db.execute(_AGY_TABLE)
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            if adding:
                self.migrated = {"added": AGY_TABLE, "backup": backup}

    def _has_table(self, name):
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def _holds_rows(self):
        """True when a schema-2 table holds a row (one that cannot be read counts: back up rather than guess)."""
        for table in _V2_TABLES:
            try:
                if self.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    return True
            except sqlite3.Error:
                return True
        return False

    # ---- accounts ----
    def accounts(self):
        """Every account in creation order per provider; JSON columns decoded (flags defaults to {})."""
        with self.lock:
            rows = self.db.execute(_SELECT_ACCOUNTS + " ORDER BY provider, created, rowid").fetchall()
        return [_account(row) for row in rows]

    def account(self, key):
        with self.lock:
            row = self.db.execute(_SELECT_ACCOUNTS + " WHERE id=?", (key,)).fetchone()
        return _account(row) if row else None

    def find(self, provider, identity):
        with self.lock:
            row = self.db.execute(_SELECT_ACCOUNTS + " WHERE provider=? AND identity=?",
                                  (provider, identity)).fetchone()
        return _account(row) if row else None

    def save_account(self, provider, identity, label, email="", plan="", org="", *,
                     imported_from=None, owner="v2"):
        """Add an account, or refresh email/plan/org of the saved one with the same identity (its
        label, owner and position stay). Returns the account id."""
        if owner not in OWNERS:
            raise ValueError(f"Invalid account owner {owner!r}")
        now = self.clock()
        with self.lock:
            old = self.db.execute("SELECT id FROM accounts WHERE provider=? AND identity=?",
                                  (provider, identity)).fetchone()
            if old:
                self.db.execute("UPDATE accounts SET email=?, plan=?, org=?, updated=? WHERE id=?",
                                (email, plan, org, now, old["id"]))
                self.db.commit()
                return old["id"]
            key = uuid.uuid4().hex
            self.db.execute(
                """INSERT INTO accounts(id, provider, identity, label, email, plan, org, created, updated,
                     sort_order, imported_from, owner)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort_order) + 1, 0) FROM accounts), ?, ?)""",
                (key, provider, identity, label or email or provider.title(), email, plan, org, now, now,
                 imported_from, owner))
            self.db.commit()
        return key

    def adopt_account(self, key, provider, identity, label, email="", plan="", org=""):
        """A row for the vault record `key`, whose own row was lost with a damaged database."""
        now = self.clock()
        with self.lock:
            self.db.execute(
                """INSERT OR IGNORE INTO accounts(id, provider, identity, label, email, plan, org, created, updated,
                     sort_order) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort_order) + 1, 0) FROM accounts))""",
                (key, provider, identity, label, email, plan, org, now, now))
            self.db.commit()

    def update(self, key, **fields):
        """Change account columns other than id/provider/identity/created/updated (ValueError
        otherwise); last_good_usage and flags are stored as JSON."""
        if not fields or not set(fields) <= _UPDATABLE:
            raise ValueError("Invalid account update")
        if "owner" in fields and fields["owner"] not in OWNERS:
            raise ValueError(f"Invalid account owner {fields['owner']!r}")
        values = [_encode(value) if name in _JSON_COLUMNS else value for name, value in fields.items()]
        with self.lock:
            self.db.execute("UPDATE accounts SET " + ", ".join(name + "=?" for name in fields) + " WHERE id=?",
                            (*values, key))
            self.db.commit()

    def reorder(self, ids):
        """Manual order: the given accounts first, in that order, then the rest as they were."""
        if len(set(ids)) != len(ids):
            raise DeckError("Each account can appear only once in the order.")
        with self.lock:
            existing = [row[0] for row in self.db.execute("SELECT id FROM accounts ORDER BY sort_order, created, rowid")]
            if not set(ids) <= set(existing):
                raise DeckError("That order names an account that no longer exists.")
            order = list(ids) + [key for key in existing if key not in set(ids)]
            self.db.executemany("UPDATE accounts SET sort_order=? WHERE id=?",
                                [(position, key) for position, key in enumerate(order)])
            self.db.commit()

    def remove(self, key):
        with self.lock:
            self.db.execute("DELETE FROM accounts WHERE id=?", (key,))
            for table in ("snapshots", "readings", "refills", "generations", "alerts"):
                self.db.execute(f"DELETE FROM {table} WHERE account=?", (key,))
            self.db.commit()

    # ---- Antigravity accounts (their own table; no row of theirs in any shared table) ----
    def agy_accounts(self):
        """Every saved Antigravity account in its manual order: the columns as a dict, `usage` decoded (None
        without a reading) and `flags` decoded ({} by default)."""
        with self.lock:
            rows = self.db.execute(_SELECT_AGY + " ORDER BY sort_order, created, rowid").fetchall()
        return [_agy_account(row) for row in rows]

    def agy_account(self, key):
        with self.lock:
            row = self.db.execute(_SELECT_AGY + " WHERE id=?", (key,)).fetchone()
        return _agy_account(row) if row else None

    def agy_find(self, email):
        """The saved account with this e-mail (any case), or None."""
        with self.lock:
            row = self.db.execute(_SELECT_AGY + " WHERE email=?", (_agy_email(email),)).fetchone()
        return _agy_account(row) if row else None

    def agy_add(self, email, *, label=None, plan=None, tier=None, saved_at=None, key=None):
        """Adds an account last in the order and returns its id (`key`, 32 hex digits, when the caller already
        wrote its vault record under it; a new one otherwise). DeckError("invalid") when this e-mail is saved
        already: look it up first (`agy_find`) and update that row instead."""
        email = _agy_email(email)
        if key is None:
            key = uuid.uuid4().hex
        elif not isinstance(key, str) or not _ACCOUNT_KEY.fullmatch(key):
            raise ValueError("an Antigravity account id is 32 lower-case hex digits")
        now = self.clock()
        with self.lock:
            try:
                self.db.execute(
                    """INSERT INTO antigravity_accounts(id, email, label, plan, tier, created, updated, sort_order,
                         saved_at) VALUES(?, ?, ?, ?, ?, ?, ?,
                         (SELECT COALESCE(MAX(sort_order) + 1, 0) FROM antigravity_accounts), ?)""",
                    (key, email, (label or "").strip() or email.partition("@")[0] or "Antigravity account", plan, tier,
                     now, now, saved_at))
                self.db.commit()
            except sqlite3.IntegrityError:
                self.db.rollback()
                raise DeckError("This Antigravity account is saved already.", "invalid") from None
        return key

    def agy_update(self, key, **fields):
        """Changes `label`, `plan`, `tier`, `saved_at`, `usage`, `usage_at`, `error`, `error_code` or `flags`
        (ValueError for anything else: the id and e-mail never change, the order changes with `agy_reorder`);
        `usage` and `flags` are stored as JSON. True when the account exists."""
        if not fields or not set(fields) <= _AGY_UPDATABLE:
            raise ValueError("Invalid Antigravity account update")
        values = [_encode(value) if name in _AGY_JSON else value for name, value in fields.items()]
        with self.lock:
            cursor = self.db.execute("UPDATE antigravity_accounts SET " + ", ".join(name + "=?" for name in fields)
                                     + ", updated=? WHERE id=?", (*values, self.clock(), key))
            self.db.commit()
        return cursor.rowcount == 1

    def agy_record_usage(self, key, usage, *, at):
        """A good reading (the normalised shape `providers.antigravity.parse_usage` gives) taken at `at`: it
        replaces the last one and clears the account's error. True when the account exists."""
        return self.agy_update(key, usage=usage, usage_at=at, error=None, error_code=None)

    def agy_record_error(self, key, message, code):
        """A failed reading or a sign-in that stopped working: the last good reading stays. True when the
        account exists."""
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        return self.agy_update(key, error=str(message), error_code=code)

    def agy_reorder(self, ids):
        """Manual order: the given Antigravity accounts first, in that order, then the rest as they were."""
        if len(set(ids)) != len(ids):
            raise DeckError("Each account can appear only once in the order.")
        with self.lock:
            existing = [row[0] for row in self.db.execute(
                "SELECT id FROM antigravity_accounts ORDER BY sort_order, created, rowid")]
            if not set(ids) <= set(existing):
                raise DeckError("That order names an account that no longer exists.")
            order = list(ids) + [key for key in existing if key not in set(ids)]
            self.db.executemany("UPDATE antigravity_accounts SET sort_order=? WHERE id=?",
                                [(position, key) for position, key in enumerate(order)])
            self.db.commit()

    def agy_remove(self, key):
        """Deletes the account's row (its vault records are the caller's: `Vault.remove(agy_key(key))`). True
        when it existed."""
        with self.lock:
            cursor = self.db.execute("DELETE FROM antigravity_accounts WHERE id=?", (key,))
            self.db.commit()
        return cursor.rowcount == 1

    def agy_rekey(self, key, new):
        """Gives the account `key` the id `new` (32 lower-case hex digits that no other account has; ValueError or
        DeckError "invalid" otherwise). Only Antigravity's reconcile uses it: a row whose saved sign-in record is gone
        takes the id of the record its e-mail still has, so no vault record is ever renamed (an update compares the
        records' names before and after). Nothing else names an Antigravity id. True when the account existed."""
        if not isinstance(new, str) or not _ACCOUNT_KEY.fullmatch(new):
            raise ValueError("an Antigravity account id is 32 lower-case hex digits")
        with self.lock:
            try:
                cursor = self.db.execute("UPDATE antigravity_accounts SET id=?, updated=? WHERE id=?",
                                         (new, self.clock(), key))
                self.db.commit()
            except sqlite3.IntegrityError:
                self.db.rollback()
                raise DeckError("That Antigravity account id is taken.", "invalid") from None
        return cursor.rowcount == 1

    # ---- readings ----
    def observe(self, key, usage, *, at=None):
        """Persist a successful reading: the account's last good reading, one `readings` row per
        window with a known percentage (kept 7 days for pace, trend and history) and at most one
        v1-style snapshot per quarter hour (kept 90 days). Clears the account's error."""
        now = self.clock()
        at = now if at is None else at
        payload = _encode(usage)
        rows = [(key, window["id"], at, window["used"], window.get("reset"))
                for window in usage.get("windows") or ()
                if isinstance(window, dict) and isinstance(window.get("id"), str) and _is_number(window.get("used"))]
        with self.lock:
            self.db.execute("UPDATE accounts SET last_good_usage=?, last_good_at=?, error=NULL, error_code=NULL "
                            "WHERE id=?", (payload, at, key))
            self.db.executemany("INSERT INTO readings(account, window_id, at, used, reset) VALUES(?, ?, ?, ?, ?)", rows)
            last = self.db.execute("SELECT MAX(at) FROM snapshots WHERE account=?", (key,)).fetchone()[0]
            if last is None or at - last >= SNAPSHOT_EVERY:
                self.db.execute("INSERT INTO snapshots(account, at, usage) VALUES(?, ?, ?)", (key, at, payload))
            self.db.execute("DELETE FROM snapshots WHERE at < ?", (now - _SNAPSHOT_DAYS * _DAY,))
            self.db.execute("DELETE FROM readings WHERE at < ?", (now - _READING_DAYS * _DAY,))
            self.db.commit()

    def readings(self, key, window, since=None):
        """[{"at", "used", "reset"}] of one window, oldest first."""
        with self.lock:
            rows = self.db.execute("SELECT at, used, reset FROM readings WHERE account=? AND window_id=? AND at>=? "
                                   "ORDER BY at, id", (key, window, since or 0)).fetchall()
        return [dict(row) for row in rows]

    def history(self, key, since=None, limit=HISTORY_LIMIT):
        """/api/history payload, oldest first, each list capped at limit (at most 700):
        {"account", "readings": [{"at", "windows": {window: used}}], "snapshots": [{"at", "usage"}]}."""
        limit = max(1, min(int(limit), HISTORY_LIMIT))
        with self.lock:
            rows = self.db.execute("SELECT at, window_id, used FROM readings WHERE account=? AND at>=? ORDER BY at, id",
                                   (key, since or 0)).fetchall()
            snapshots = self.db.execute("SELECT at, usage FROM snapshots WHERE account=? AND at>=? "
                                        "ORDER BY at DESC, id DESC LIMIT ?", (key, since or 0, limit)).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(row["at"], {})[row["window_id"]] = row["used"]
        return {"account": key,
                "readings": [{"at": at, "windows": windows} for at, windows in grouped.items()][-limit:],
                "snapshots": [{"at": row["at"], "usage": json.loads(row["usage"])} for row in reversed(snapshots)]}

    def record_refill(self, account, window, *, reset_at, confirmed_at, previous_used):
        """Record a confirmed refill once per (account, window, reset); True when it is new (§6.3)."""
        with self.lock:
            cursor = self.db.execute("INSERT OR IGNORE INTO refills(account, window_id, reset_at, confirmed_at, "
                                     "previous_used) VALUES(?, ?, ?, ?, ?)",
                                     (account, window, reset_at, confirmed_at, previous_used))
            self.db.execute("DELETE FROM refills WHERE confirmed_at < ?", (self.clock() - _REFILL_DAYS * _DAY,))
            self.db.commit()
        return cursor.rowcount == 1

    def refills(self, since):
        """Refills confirmed at or after since, oldest first:
        [{"account", "window", "reset_at", "confirmed_at", "previous_used"}]."""
        with self.lock:
            rows = self.db.execute("SELECT account, window_id, reset_at, confirmed_at, previous_used FROM refills "
                                   "WHERE confirmed_at>=? ORDER BY confirmed_at, id", (since,)).fetchall()
        return [{"account": row["account"], "window": row["window_id"], "reset_at": row["reset_at"],
                 "confirmed_at": row["confirmed_at"], "previous_used": row["previous_used"]} for row in rows]

    # ---- vault generation metadata ----
    def record_generation(self, account, digest, *, source, health=None, meta=None, at=None):
        """Non-secret facts about a vault generation (digest from Vault.put): when and how it was
        captured, its health and e.g. the Desktop version or token-expiry summary in `meta`."""
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO generations(account, digest, at, source, health, meta) "
                            "VALUES(?, ?, ?, ?, ?, ?)",
                            (account, digest, self.clock() if at is None else at, source, health, _encode(meta)))
            self.db.execute("DELETE FROM generations WHERE account=? AND id NOT IN (SELECT id FROM generations "
                            "WHERE account=? ORDER BY at DESC, id DESC LIMIT ?)", (account, account, GENERATION_ROWS))
            self.db.commit()

    def generations(self, account):
        """Recorded generation metadata, newest first: [{"digest", "at", "source", "health", "meta"}]."""
        with self.lock:
            rows = self.db.execute("SELECT digest, at, source, health, meta FROM generations WHERE account=? "
                                   "ORDER BY at DESC, id DESC", (account,)).fetchall()
        return [{**dict(row), "meta": json.loads(row["meta"]) if row["meta"] else None} for row in rows]

    # ---- alerts ----
    def add_alert(self, alert):
        """Persist a delivered alert ({kind, title, level?, account?, window?, message?, at?,
        snoozed_until?}); returns its id "al_<n>". Alerts older than a week are dropped."""
        now = self.clock()
        with self.lock:
            cursor = self.db.execute(
                "INSERT INTO alerts(kind, level, account, window_id, title, message, at, snoozed_until) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (alert["kind"], alert.get("level"), alert.get("account"), alert.get("window"), alert["title"],
                 alert.get("message"), alert.get("at", now), alert.get("snoozed_until")))
            self.db.execute("DELETE FROM alerts WHERE at < ?", (now - _ALERT_DAYS * _DAY,))
            self.db.commit()
        return f"al_{cursor.lastrowid}"

    def alerts(self, *, include_dismissed=False, limit=50):
        """Newest first, in the §3.3 alert shape."""
        with self.lock:
            rows = self.db.execute("SELECT * FROM alerts" + ("" if include_dismissed else " WHERE dismissed=0")
                                   + " ORDER BY at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": f"al_{row['id']}", "kind": row["kind"], "level": row["level"], "account": row["account"],
                 "window": row["window_id"], "title": row["title"], "message": row["message"], "at": row["at"],
                 "snoozed_until": row["snoozed_until"], "dismissed": bool(row["dismissed"])} for row in rows]

    def update_alert(self, alert_id, *, dismissed=None, snoozed_until=None):
        """Dismiss or snooze an alert; False when the id is unknown."""
        match = _ALERT_ID.fullmatch(alert_id) if isinstance(alert_id, str) else None
        fields = {}
        if dismissed is not None:
            fields["dismissed"] = int(bool(dismissed))
        if snoozed_until is not None:
            fields["snoozed_until"] = snoozed_until
        if not match or not fields:
            return False
        with self.lock:
            cursor = self.db.execute("UPDATE alerts SET " + ", ".join(name + "=?" for name in fields) + " WHERE id=?",
                                     (*fields.values(), int(match.group(1))))
            self.db.commit()
        return cursor.rowcount == 1

    def cap_pauses(self, longest):
        """No account's usage checks stay paused for more than `longest` seconds from now: a pause saved from a
        provider's wait before such waits were capped (errors.rate_limit_wait) ends then."""
        until = self.clock() + longest
        with self.lock:
            self.db.execute("UPDATE accounts SET paused_until=? WHERE paused_until > ?", (until, until))
            self.db.commit()

    # ---- settings and events ----
    def setting(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        self.set_settings({key: value})

    def set_settings(self, values):
        """Stores several settings in one transaction: all of them or none."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.executemany("INSERT OR REPLACE INTO settings VALUES(?, ?)",
                                    [(key, json.dumps(value)) for key, value in values.items()])
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def event(self, kind, message):
        with self.lock:
            self.db.execute("INSERT INTO events(at, kind, message) VALUES(?, ?, ?)", (self.clock(), kind, message))
            self.db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                            (EVENT_LIMIT,))
            self.db.commit()

    def events(self, limit=12):
        """Newest first: [{"id", "at", "kind", "message"}]."""
        with self.lock:
            rows = self.db.execute("SELECT id, at, kind, message FROM events ORDER BY id DESC LIMIT ?",
                                   (limit,)).fetchall()
        return [dict(row) for row in rows]


class FileTransaction:
    """Encrypted write-ahead backup; rollback only bytes still owned by this transaction."""

    def __init__(self, vault, roots, key=None):
        self.vault = vault
        self.roots = [Path(p).resolve() for p in roots]
        self.key = key or "transaction-" + uuid.uuid4().hex
        self.doc = {"status": "prepared", "at": time.time(), "changes": []}

    @classmethod
    def load(cls, vault, roots, key):
        """Reopen a persisted transaction, e.g. one recover_transactions() reported."""
        tx = cls(vault, roots, key)
        tx.doc = vault.get(key)
        return tx

    def check(self, path):
        path = Path(path).resolve()
        if not any(path.is_relative_to(root) for root in self.roots):
            raise DeckError("A file is outside the permitted application folders.", "safety")
        return path

    def write(self, path, data):
        path = self.check(path)
        before = path.read_bytes() if path.exists() else None
        change = {"path": str(path), "before": base64.b64encode(before).decode() if before is not None else None,
                  "after": digest(data) if data is not None else None}
        self.doc["changes"].append(change)
        self.save()
        if data is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, data)

    def save(self):
        """Persist doc, e.g. after the caller set doc["status"] = "verifying"."""
        self.vault.put(self.key, self.doc, generation=False)

    def commit(self):
        self.doc["status"] = "complete"
        self.save()

    def restore_bytes(self, path, before):
        """The bytes a rollback writes back to `path` for its bytes from before the transaction: `before`
        itself here; a subclass may put back a newer equivalent (CLITransaction never puts back a spent login)."""
        return before

    def rollback(self):
        """`rollback_files`, then the transaction is finished ("rolled_back") or, when a file changed since,
        left for Recover ("conflict")."""
        conflicts = self.rollback_files()
        self.doc["status"] = "conflict" if conflicts else "rolled_back"
        self.save()
        if conflicts:
            raise DeckError("Recovery paused because the app changed files after the switch. Your encrypted backup is preserved.", "recovery")

    def rollback_files(self):
        """Restores each file that still holds what this transaction wrote (`restore_bytes` of its earlier
        bytes) and returns the names of those changed since, which are conflicts and left alone. Bytes put
        back other than the earlier ones are recorded first, so a rollback run again counts them as restored."""
        conflicts = []
        for item in reversed(self.doc["changes"]):
            path = self.check(item["path"])
            current = path.read_bytes() if path.exists() else None
            before = base64.b64decode(item["before"]) if item["before"] is not None else None
            if current == before or (current is not None and item.get("restored") == digest(current)):
                continue
            if (digest(current) if current is not None else None) != item["after"]:
                conflicts.append(path.name)
                continue
            if before is None:
                path.unlink(missing_ok=True)
            else:
                data = self.restore_bytes(path, before)
                if data != before:
                    item["restored"] = digest(data)
                    self.save()
                atomic_write(path, data)
        return conflicts


UNFINISHED = ("prepared", "conflict", "verifying")      # a transaction in one of these waits for Recover


def recover_transactions(vault, unreadable=None):
    """The keys of transactions a switch left unfinished (prepared, verifying or conflict), which the
    Recover action puts right; finished ones older than 30 days, a terminal conflict Recover closed ("kept")
    among them, are deleted. DeckError("vault") when a record cannot be read (the caller treats that as a
    pending recovery too), unless `unreadable` is a list: (key, error) of such a record then goes there and
    the scan goes on. Antigravity's records (`agy-transaction-`) are never among them: every build would block
    all switching on one, so Antigravity keeps and settles its own (`Vault.agy_transaction_keys`)."""
    pending = []
    for path in vault.root.glob("transaction-*" + VAULT_SUFFIX):
        key = path.name.removesuffix(VAULT_SUFFIX)
        if not _KEY.fullmatch(key):
            continue
        try:
            doc = vault.get(key)
        except DeckError as exc:
            if unreadable is None:
                raise
            unreadable.append((key, exc))
            continue
        if doc.get("status") in UNFINISHED:
            pending.append(key)
        elif doc.get("at", time.time()) < time.time() - 30 * _DAY:
            path.unlink()
    return pending
