"""Portable Claude Desktop profiles: the core of the PC-to-Mac move (docs/mac/PLAN.md §1.6, §2.1, §2.3;
docs/mac/PROVIDERS-MAC.md §2 "Decrypt → re-encrypt recipe").

`to_portable(profile, crypto)` opens a saved profile with the key it was written with and returns a
plain dict: the token caches as their JSON text, every cookie as its plaintext value plus the row's other
columns, the cookie database's schema and `meta` rows, and the profile's non-secret fields. The source
machine's key ("key") is not carried. `from_portable(portable, crypto, target_meta_version)` seals it
again with `crypto`'s current key and writes the cookie rows into a database of the target schema,
returning a normal profile whose "key" is `crypto.key_ref()`.

Rules:
- The 32-byte SHA-256(host_key) prefix inside a cookie's plaintext is checked and stripped when the
  source database's `meta.version` >= 24, and added when the target's is >= 24 (Chromium's rule).
- Dropped (§2.1): the `cf_clearance` cookie (bound to the PC's user agent and IP; Cloudflare issues a new
  one) and device-bound profile files (`ant-device-registry.json`, `bridge-state.json`; the Mac registers
  its own). Unknown top-level profile fields are not carried. `portable["dropped"]` names what was left
  out (never a value).
- A value that does not open, a cookie whose host hash does not match, an unknown format or a schema the
  rows do not fit raises `PortableError`; nothing is skipped silently. A missing or refused macOS
  Keychain key raises `chromium_crypto.ChromiumKeyNotReady` (defer and retry later).
- Pure functions: no file, process or network access, and no logging. A portable dict holds plaintext
  credentials: keep it in memory or inside the encrypted move bundle only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3

from ..core import DeckError
from .chromium_crypto import ChromiumDecryptError
from .claude import SQLITE_MAGIC, _checked_cookies, _snapshot

PORTABLE_FORMAT = "accountdeck.claude-desktop.portable"
PORTABLE_VERSION = 1
TOKEN_CACHE_KEYS = ("oauth:tokenCache", "oauth:tokenCacheV2")
HOST_PREFIX_VERSION = 24          # Chromium cookie DB meta.version from which values carry sha256(host_key)
DROPPED_COOKIES = ("cf_clearance",)
DEVICE_BOUND_FILES = ("ant-device-registry.json", "bridge-state.json")
PROFILE_FIELDS = ("version", "identity", "captured", "journal", "cookie_heal", "org")   # carried as they are
_CARRIED = set(PROFILE_FIELDS) | {"auth", "key", "cookies", "files"}
_SECRET_COLUMNS = ("value", "encrypted_value")
# The carried schema is re-run only as table and index definitions (sqlite3 runs one statement per call).
_SCHEMA_SQL = re.compile(r"\s*CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s", re.IGNORECASE)


class PortableError(DeckError):
    """A profile or portable dict that cannot be converted. The message names the item (a cookie's host
    and name, an auth key), never a value."""

    def __init__(self, message, code="vault"):
        super().__init__(message, code)


# ---- export side ----

def to_portable(profile: dict, crypto) -> dict:
    """The plaintext, OS-independent form of a saved Claude Desktop profile (see the module docstring).
    `crypto` is the ChromiumCrypto of the machine that wrote the profile."""
    if not isinstance(profile, dict) or not isinstance(profile.get("auth"), dict):
        raise PortableError("This saved Claude profile is damaged.")
    key = crypto.key_for(profile.get("key") or "")
    auth, caches = {}, {}
    for name, value in profile["auth"].items():
        if name in TOKEN_CACHE_KEYS and value not in (None, ""):
            caches[name] = _open_token_cache(key, name, value)
        else:
            auth[name] = value
    cookies, dropped_cookies = None, {}
    if profile.get("cookies"):
        cookies, dropped_cookies = _portable_cookies(profile["cookies"], key)
    files, dropped_files = {}, []
    for rel, data in (profile.get("files") or {}).items():
        if not isinstance(rel, str) or not (data is None or isinstance(data, str)):
            raise PortableError("A saved Claude profile file is damaged.")
        if rel.replace("\\", "/").rsplit("/", 1)[-1] in DEVICE_BOUND_FILES:
            dropped_files.append(rel)
        else:
            files[rel] = data
    return {"format": PORTABLE_FORMAT, "version": PORTABLE_VERSION, "source_os": crypto.os,
            "profile": {name: profile[name] for name in PROFILE_FIELDS if name in profile},
            "auth": auth, "token_caches": caches, "cookies": cookies, "files": files,
            "dropped": {"cookies": dropped_cookies, "files": sorted(dropped_files),
                        "fields": sorted(str(name) for name in profile if name not in _CARRIED)}}


def _open_token_cache(key, name, value) -> str:
    try:
        blob = base64.b64decode(value, validate=True) if isinstance(value, str) else b""
    except ValueError:
        blob = b""
    if not blob.startswith(b"v10"):
        raise PortableError(f"The saved {name} has an unknown format.", "unsupported")
    try:
        text = key.decrypt(blob).decode("utf-8")
        parsed = json.loads(text)
    except (ChromiumDecryptError, ValueError):
        raise PortableError(f"The saved {name} does not open with this profile's key.") from None
    if not isinstance(parsed, dict):
        raise PortableError(f"The saved {name} does not open with this profile's key.")
    return text


def _portable_cookies(encoded, key):
    db = _open_db(_decode_db(encoded, "The saved cookie database is damaged."))
    try:
        version = _meta_version(db, "The saved cookie database")
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        if any(not isinstance(name, str) or not (value is None or isinstance(value, (int, str)))
               for name, value in meta.items()):
            raise PortableError("The saved cookie database's version table is not recognized.", "unsupported")
        schema = [sql for (sql,) in db.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, rowid")]
        columns = _columns(db, "The saved cookie database")
        rows, dropped = [], {}
        names = ", ".join(_quote(name) for name in columns)
        for record in db.execute(f"SELECT {names} FROM cookies ORDER BY rowid"):
            row = dict(zip(columns, record, strict=True))
            if row["name"] in DROPPED_COOKIES:
                dropped[row["name"]] = dropped.get(row["name"], 0) + 1
                continue
            plaintext = _open_cookie(key, row, version)
            fields = {name: row[name] for name in columns if name not in _SECRET_COLUMNS}
            if any(not (value is None or isinstance(value, (int, float, str))) for value in fields.values()):
                raise PortableError(f"Cookie {_label(row)} has a column type this version cannot carry.",
                                    "unsupported")
            rows.append({"fields": fields, "plaintext": base64.b64encode(plaintext).decode()})
    except sqlite3.Error:
        raise PortableError("The saved cookie database could not be read.") from None
    finally:
        db.close()
    return {"meta_version": version, "meta": meta, "schema": schema, "rows": rows}, dropped


def _open_cookie(key, row, version) -> bytes:
    """Chromium's read: the decrypted encrypted_value when there is one (minus the verified host hash
    from meta version 24), else the plaintext `value` column."""
    encrypted = row["encrypted_value"]
    if encrypted:
        if not isinstance(encrypted, bytes) or encrypted[:3] not in key.prefixes:
            raise PortableError(f"Cookie {_label(row)} has an unknown encryption format.", "unsupported")
        try:
            plaintext = key.decrypt(encrypted)
        except ChromiumDecryptError:
            raise PortableError(f"Cookie {_label(row)} does not open with this profile's key.") from None
        if version >= HOST_PREFIX_VERSION:
            digest = _host_hash(row["host_key"])
            if len(plaintext) < len(digest) or not hmac.compare_digest(plaintext[:len(digest)], digest):
                raise PortableError(f"Cookie {_label(row)} does not open with this profile's key.")
            plaintext = plaintext[len(digest):]
        return plaintext
    value = row["value"]
    if value is None:
        return b""
    return value if isinstance(value, bytes) else str(value).encode("utf-8")


# ---- import side ----

def from_portable(portable: dict, crypto, target_meta_version: int, *, template: bytes | None = None) -> dict:
    """A saved profile for the machine `crypto` belongs to, built from `to_portable`'s output.

    `target_meta_version` is the cookie database version Claude Desktop on the target uses. `template` is
    the bytes of a cookie database that Claude Desktop created there (see `cookie_template`): its schema
    and `meta` rows are used and its own rows discarded; its version must equal `target_meta_version`.
    Without a template the source schema is reused, which is allowed only when the versions are equal.
    Raises ChromiumKeyNotReady (macOS key not available: defer) or PortableError; returns nothing partial."""
    _check_portable(portable)
    if isinstance(target_meta_version, bool) or not isinstance(target_meta_version, int) or target_meta_version < 1:
        raise PortableError("The target cookie database version is not valid.", "invalid")
    key_ref = crypto.key_ref()
    key = crypto.key_for(key_ref)
    auth = dict(portable["auth"])
    for name, text in portable["token_caches"].items():
        auth[name] = base64.b64encode(key.encrypt(text.encode("utf-8"))).decode()
    cookies = None
    if portable["cookies"] is not None:
        cookies = _build_cookies(portable["cookies"], key, target_meta_version, template)
    profile = {name: value for name, value in portable["profile"].items() if name in PROFILE_FIELDS}
    profile.update({"auth": auth, "key": key_ref, "cookies": cookies})
    if portable["files"]:
        profile["files"] = dict(portable["files"])
    return profile


def _check_portable(portable):
    if not isinstance(portable, dict) or portable.get("format") != PORTABLE_FORMAT:
        raise PortableError("This is not a portable Claude Desktop profile.", "unsupported")
    if portable.get("version") != PORTABLE_VERSION:
        raise PortableError("This portable Claude Desktop profile comes from another AccountDeck version.",
                            "unsupported")
    shapes = (("profile", dict), ("auth", dict), ("token_caches", dict), ("files", dict))
    if any(not isinstance(portable.get(name), kind) for name, kind in shapes):
        raise PortableError("This portable Claude Desktop profile is damaged.")
    for name, text in portable["token_caches"].items():
        if name not in TOKEN_CACHE_KEYS or not isinstance(text, str):
            raise PortableError("This portable Claude Desktop profile is damaged.")
    cookies = portable.get("cookies")
    if cookies is not None and not (isinstance(cookies, dict) and isinstance(cookies.get("rows"), list)
                                    and isinstance(cookies.get("schema"), list)
                                    and all(isinstance(sql, str) and _SCHEMA_SQL.match(sql) for sql in cookies["schema"])
                                    and isinstance(cookies.get("meta"), dict)
                                    and all(isinstance(name, str) and (value is None or isinstance(value, (int, str)))
                                            for name, value in cookies["meta"].items())
                                    and _is_int(cookies.get("meta_version"))):
        raise PortableError("This portable Claude Desktop profile's cookies are damaged.")


def _build_cookies(cookies, key, target_version, template) -> str:
    db = sqlite3.connect(":memory:")
    try:
        if template is not None:
            db.deserialize(_decode_db(template, "Claude's cookie database on this computer is not readable.",
                                      encoded=False))
            found = _meta_version(db, "Claude's cookie database on this computer")
            if found != target_version:
                raise PortableError(f"Claude's cookie database on this computer is version {found}, "
                                    f"not {target_version}.", "unsupported")
            columns = _columns(db, "Claude's cookie database on this computer")
            db.execute("DELETE FROM cookies")
        else:
            if target_version != cookies["meta_version"]:
                raise PortableError("Moving cookies to another database version needs a cookie database made by "
                                    "Claude Desktop on this computer.", "unsupported")
            for sql in cookies["schema"]:
                db.execute(sql)
            db.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", list(cookies["meta"].items()))
            columns = _columns(db, "The saved cookie database")
        info = {row[1]: row for row in db.execute("PRAGMA table_info(cookies)")}
        for row in cookies["rows"]:
            db.execute(*_insert(row, key, target_version, columns, info))
        db.commit()
        db.execute("VACUUM")
        data = bytearray(db.serialize())
    except sqlite3.Error:
        raise PortableError("The cookie database could not be written in the target format.", "unsupported") from None
    finally:
        db.close()
    data[18:20] = b"\x01\x01"          # rollback-journal mode, as _snapshot leaves a copy
    encoded = base64.b64encode(bytes(data)).decode()
    _checked_cookies(encoded)
    return encoded


def _insert(row, key, target_version, columns, info):
    fields = row.get("fields") if isinstance(row, dict) else None
    if not isinstance(fields, dict) or not isinstance(fields.get("host_key"), str) \
            or not isinstance(row.get("plaintext"), str):
        raise PortableError("A portable cookie is damaged.")
    label = _label(fields)
    unknown = sorted(name for name in fields if name not in columns or name in _SECRET_COLUMNS)
    if unknown:
        raise PortableError(f"Cookie {label} has columns the target database lacks: {', '.join(unknown)}.",
                            "unsupported")
    missing = sorted(name for name, (_cid, _name, _type, notnull, default, _pk) in info.items()
                     if name not in fields and name not in _SECRET_COLUMNS and notnull and default is None)
    if missing:
        raise PortableError(f"The target cookie database needs columns the saved cookies lack: {', '.join(missing)}.",
                            "unsupported")
    try:
        plaintext = base64.b64decode(row["plaintext"], validate=True)
    except ValueError:
        raise PortableError(f"Cookie {label} is damaged.") from None
    if target_version >= HOST_PREFIX_VERSION:
        plaintext = _host_hash(fields["host_key"]) + plaintext
    names = list(fields) + list(_SECRET_COLUMNS)
    values = list(fields.values()) + ["", key.encrypt(plaintext)]
    sql = (f"INSERT INTO cookies ({', '.join(_quote(name) for name in names)}) "
           f"VALUES ({', '.join('?' * len(names))})")
    return sql, values


# ---- cookie database helpers ----

def cookie_template(path) -> bytes:
    """A consistent read-only snapshot of a cookie database file (for `from_portable(template=)`)."""
    return _snapshot(path)


def meta_version(db_bytes: bytes) -> int:
    """The `meta.version` of a cookie database given as bytes; PortableError when it has none."""
    db = _open_db(_decode_db(db_bytes, "The cookie database is not readable.", encoded=False))
    try:
        return _meta_version(db, "The cookie database")
    except sqlite3.Error:
        raise PortableError("The cookie database is not readable.") from None
    finally:
        db.close()


def _decode_db(data, message, *, encoded=True) -> bytes:
    try:
        raw = base64.b64decode(data, validate=True) if encoded else bytes(data)
    except (TypeError, ValueError):
        raw = b""
    if not raw.startswith(SQLITE_MAGIC):
        raise PortableError(message)
    raw = bytearray(raw)
    raw[18:20] = b"\x01\x01"          # an in-memory copy cannot use a WAL sidecar
    return bytes(raw)


def _open_db(raw) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(raw)
    except sqlite3.Error:
        db.close()
        raise PortableError("The cookie database is not readable.") from None
    return db


def _meta_version(db, what) -> int:
    try:
        found = db.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
    except sqlite3.Error:
        found = None
    try:
        version = int(str(found[0]).strip()) if found else 0
    except ValueError:
        version = 0
    if version < 1:
        raise PortableError(f"{what} has no version, so its format cannot be told.", "unsupported")
    return version


def _columns(db, what) -> list[str]:
    columns = [row[1] for row in db.execute("PRAGMA table_info(cookies)")]
    if not {"host_key", "name", "value", "encrypted_value"} <= set(columns):
        raise PortableError(f"{what} is not a Chromium cookie database.", "unsupported")
    return columns


def _host_hash(host) -> bytes:
    return hashlib.sha256(str(host).encode()).digest()


def _quote(name) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _label(row) -> str:
    return f"{row.get('name')!s} on {row.get('host_key')!s}"


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
