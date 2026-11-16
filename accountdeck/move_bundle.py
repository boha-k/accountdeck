"""The PC-to-Mac transfer bundle (docs/mac/PLAN.md §2.1, §2.4, §2.5): a passphrase-encrypted file that
carries accounts, their history and their decrypted credentials from the PC, plus the confirmation code
the Mac shows once it has stored them all. This Mac build only reads bundles (`read_bundle`,
`open_bundle`, `code_for_imported`); `seal` is the writer the PC build used, kept as the format's
executable definition for tests.

Pure module: stdlib plus `cryptography`, no imports from the rest of the package. Nothing here logs or
prints, and no error message carries secret material.

File layout (all integers big-endian):

    MAGIC b"ADMOVE" | format version (1 byte) | header length L (2 bytes) | header JSON (L bytes)
    | SHA-256 of everything before it (32 bytes) | AES-256-GCM ciphertext with its 16-byte tag

The header JSON is canonical (sorted keys, no spaces) and holds `bundle_id`, `kind`, `created`,
`kdf` (scrypt `n`, `r`, `p`, `salt`), `cipher` (`nonce`) and `check`. The SHA-256 catches accidental
damage to the header so it reads as "corrupt", not "wrong passphrase". The whole prefix (magic to
digest) is the GCM associated data, so any change to the header, even with a fixed-up digest,
fails decryption.

Keys: master = scrypt(passphrase); HKDF-SHA256 then splits it into the encryption key, the
confirmation key and a 16-byte passphrase check stored in the header. The check only tells a wrong
passphrase apart from a damaged file: anyone holding the file can already test a guess against
the GCM tag, and scrypt dominates the cost either way.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import struct
import time
import unicodedata

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

__all__ = [
    "MAGIC", "FORMAT_VERSION", "PAYLOAD_FORMAT", "KINDS", "PROVIDERS", "SECRET_TYPES", "PORTABLE_SETTINGS",
    "SOURCE_PLATFORMS", "SCRYPT_N", "SCRYPT_MIN_N", "SCRYPT_MAX_N", "SCRYPT_R", "SCRYPT_P",
    "MAX_BUNDLE_BYTES", "FILE_SUFFIX",
    "BundleError", "WrongPassphrase", "CorruptBundle", "UnsupportedVersion", "WrongKind",
    "BundleTooLarge", "InvalidPayload", "NotABundle",
    "BundleKeys", "BundleHeader",
    "seal", "open_bundle", "peek_header", "validate_payload", "canonical_json",
    "confirm_code", "code_for_imported", "read_bundle",
]

MAGIC = b"ADMOVE"
FORMAT_VERSION = 1                 # the container layout above
PAYLOAD_FORMAT = 1                 # the payload schema in validate_payload
FILE_SUFFIX = ".adeck"
KINDS = ("main", "handover")       # §2.1 main export, §2.5 hand-over of the PC's live sign-ins

SCRYPT_N = 2 ** 17                 # 128 MiB with r=8; about half a second
SCRYPT_MIN_N = 2 ** 15             # the plan's floor; open_bundle refuses less unless told otherwise
SCRYPT_MAX_N = 2 ** 18             # a crafted header cannot ask for more than 256 MiB
SCRYPT_R = 8
SCRYPT_P = 1
_SCRYPT_MAX_MEMORY = 2 ** 28       # 128 * n * r bytes

MAX_BUNDLE_BYTES = 128 * 1024 * 1024
_MAX_HEADER = 4096
_DIGEST_LEN = 32
_TAG_LEN = 16
_SALT_LEN = 16
_NONCE_LEN = 12
_CHECK_LEN = 16
_KEY_LEN = 32
_PREFIX = struct.Struct(">6sBH")   # magic, format version, header length

_INFO_ENCRYPT = b"accountdeck-move/v1/encrypt"
_INFO_CONFIRM = b"accountdeck-move/v1/confirm"
_INFO_CHECK = b"accountdeck-move/v1/check"
_CODE_LABEL = b"accountdeck-move-confirm"
_MIN_PASSPHRASE = 12

PROVIDERS = ("claude", "claude_cli", "codex")
# `secret.type` per provider: the Claude Desktop portable profile (task A2), the Claude Code
# terminal record and the Codex auth.json copy. The record itself (`secret.data`) is opaque here.
SECRET_TYPES = {
    "claude": "claude-desktop-portable",
    "claude_cli": "claude-code",
    "codex": "codex-auth",
}
SOURCE_PLATFORMS = ("windows", "darwin", "linux")
# The settings that mean the same on both machines; machine settings (menu bar, start at login, cwd,
# the PC's taskbar and AccountDeck 1 settings) never travel.
PORTABLE_SETTINGS = frozenset({
    "cli_autoswitch", "cli_threshold", "cli_order", "cli_manual_order", "cli_early_switch",
    "usage_display", "confirm_when_idle", "sort", "density", "pinned_model", "use_soon",
    "show_other_limits", "cookie_route", "alert_low_left", "alerts_enabled", "quiet_hours",
    "notify_reset_minutes", "notifications", "log_level", "refresh_paused",
})
_MAX_ACCOUNTS = 1000
_MAX_TEXT = 1024
_MAX_ROWS = 200_000
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_BUNDLE_ID = re.compile(r"[0-9a-f]{32}")


# ---- errors ----

class BundleError(Exception):
    """A bundle failure the owner can act on. `str(exc)` is a fixed, secret-free message; `code`
    is one of core.ERROR_CODES (callers raise DeckError(str(exc), exc.code)); `reason` is a stable
    machine id for the UI and tests."""
    code = "invalid"
    reason = "bundle"

    def __init__(self, message, reason=None):
        super().__init__(message)
        if reason:
            self.reason = reason


class WrongPassphrase(BundleError):
    reason = "wrong_passphrase"


class CorruptBundle(BundleError):
    """Damaged, truncated or not a bundle at all (`reason` says which)."""
    reason = "corrupt"


class UnsupportedVersion(BundleError):
    code = "unsupported"
    reason = "unsupported_version"


class WrongKind(BundleError):
    reason = "wrong_kind"


class BundleTooLarge(BundleError):
    reason = "too_large"


class InvalidPayload(BundleError):
    """The payload breaks the schema. The message names the field, never its value."""
    reason = "invalid_payload"


class NotABundle(BundleError):
    reason = "not_a_bundle"


# ---- keys and header ----

@dataclass(frozen=True, repr=False)
class BundleKeys:
    """The passphrase-derived key material a reader keeps after opening: only the confirmation
    key. The encryption key is never kept."""
    confirm_key: bytes

    def __repr__(self):
        return "BundleKeys(<hidden>)"


@dataclass(frozen=True)
class BundleHeader:
    """The parsed header. `account_ids` (every account in the payload) and `keys` are filled in by
    open_bundle; peek_header leaves them empty."""
    format_version: int
    bundle_id: str
    kind: str
    created: int
    scrypt_n: int
    scrypt_r: int
    scrypt_p: int
    salt: bytes = field(repr=False)
    nonce: bytes = field(repr=False)
    check: bytes = field(repr=False, compare=False)
    account_ids: frozenset = frozenset()
    keys: BundleKeys | None = field(default=None, repr=False, compare=False)

    def to_json(self):
        """The public fields, for the UI and logs (no key material)."""
        return {"format_version": self.format_version, "bundle_id": self.bundle_id, "kind": self.kind,
                "created": self.created, "accounts": len(self.account_ids)}


# ---- seal and open ----

def seal(payload, passphrase, kind, *, scrypt_n=SCRYPT_N, min_scrypt_n=SCRYPT_MIN_N,
         max_bytes=MAX_BUNDLE_BYTES, now=None):
    """Validate `payload` for `kind`, encrypt it under `passphrase` and return the bundle bytes, as the
    PC build did. `scrypt_n` below `min_scrypt_n` is refused; tests lower both to stay fast."""
    if kind not in KINDS:
        raise WrongKind("Unknown bundle kind.")
    validate_payload(payload, kind)
    data, _header, _keys = _encrypt(canonical_json(payload), passphrase, kind, scrypt_n=scrypt_n,
                                    min_scrypt_n=min_scrypt_n, max_bytes=max_bytes, now=now)
    return data


def open_bundle(data, passphrase, *, expect_kind=None, min_scrypt_n=SCRYPT_MIN_N, max_bytes=MAX_BUNDLE_BYTES):
    """Decrypt and validate a bundle; return (header, payload). `header.account_ids` and
    `header.keys` are filled in for code_for_imported. Raises a BundleError subclass."""
    header, aad, ciphertext = _parse(data, min_scrypt_n=min_scrypt_n, max_bytes=max_bytes)
    if expect_kind is not None and header.kind != expect_kind:
        raise WrongKind("This is a hand-over bundle, not the main move bundle." if header.kind == "handover"
                        else "This is the main move bundle, not a hand-over bundle.")
    master = _master(passphrase, header.salt, header.scrypt_n, header.scrypt_r, header.scrypt_p)
    if not hmac.compare_digest(_hkdf(master, _INFO_CHECK, _CHECK_LEN), header.check):
        raise WrongPassphrase("The passphrase does not open this bundle.")
    plain = None
    try:
        plain = AESGCM(_hkdf(master, _INFO_ENCRYPT, _KEY_LEN)).decrypt(header.nonce, ciphertext, aad)
    except InvalidTag:
        pass
    if plain is None:
        raise CorruptBundle("The bundle is damaged: its contents do not match its seal.")
    payload = _loads(plain)
    if payload is None:
        raise CorruptBundle("The bundle is damaged: its contents are not readable.")
    validate_payload(payload, header.kind)
    ids = frozenset(account["id"] for account in payload["accounts"])
    keys = BundleKeys(_hkdf(master, _INFO_CONFIRM, _KEY_LEN))
    return replace(header, account_ids=ids, keys=keys), payload


def peek_header(data, *, min_scrypt_n=SCRYPT_MIN_N, max_bytes=MAX_BUNDLE_BYTES):
    """The header without the passphrase (kind, created, bundle id) for a preview; nothing in it
    is secret. Same checks as open_bundle up to decryption."""
    return _parse(data, min_scrypt_n=min_scrypt_n, max_bytes=max_bytes)[0]


def canonical_json(value):
    """Sorted keys, no spaces, UTF-8, no NaN/Infinity: the same bytes on every machine."""
    failed = False
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        failed = True
    if failed:      # raised outside the except block: the original names no values, but stays out
        raise InvalidPayload("The bundle contents are not plain JSON.")


def _encrypt(body, passphrase, kind, *, scrypt_n, min_scrypt_n, max_bytes, now):
    if not _valid_n(scrypt_n, min_scrypt_n) or SCRYPT_R * scrypt_n * 128 > _SCRYPT_MAX_MEMORY:
        raise BundleError("The key settings are outside the accepted range.", "kdf")
    if len(body) + _MAX_HEADER + _DIGEST_LEN + _TAG_LEN > max_bytes:
        raise BundleTooLarge("The accounts are too large for one bundle.")
    salt, nonce = secrets.token_bytes(_SALT_LEN), secrets.token_bytes(_NONCE_LEN)
    master = _master(passphrase, salt, scrypt_n, SCRYPT_R, SCRYPT_P)
    created = int(time.time() if now is None else now)
    header_json = {
        "bundle_id": secrets.token_hex(16), "kind": kind, "created": created,
        "kdf": {"name": "scrypt", "n": scrypt_n, "r": SCRYPT_R, "p": SCRYPT_P, "salt": _b64(salt)},
        "cipher": {"name": "aes-256-gcm", "nonce": _b64(nonce)},
        "check": _b64(_hkdf(master, _INFO_CHECK, _CHECK_LEN)),
    }
    raw = canonical_json(header_json)
    head = _PREFIX.pack(MAGIC, FORMAT_VERSION, len(raw)) + raw
    aad = head + hashlib.sha256(head).digest()
    data = aad + AESGCM(_hkdf(master, _INFO_ENCRYPT, _KEY_LEN)).encrypt(nonce, body, aad)
    if len(data) > max_bytes:
        raise BundleTooLarge("The accounts are too large for one bundle.")
    header = _header_from(header_json)
    return data, header, BundleKeys(_hkdf(master, _INFO_CONFIRM, _KEY_LEN))


def _parse(data, *, min_scrypt_n, max_bytes):
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise CorruptBundle("This is not an AccountDeck move bundle.", "not_a_bundle")
    data = bytes(data)
    if len(data) > max_bytes:
        raise BundleTooLarge("The file is larger than any move bundle can be.")
    if len(data) < len(MAGIC) or data[:len(MAGIC)] != MAGIC:
        raise CorruptBundle("This is not an AccountDeck move bundle.", "not_a_bundle")
    if len(data) < _PREFIX.size:
        raise CorruptBundle("The bundle is incomplete (cut off).", "truncated")
    _, version, length = _PREFIX.unpack_from(data)
    if version != FORMAT_VERSION:
        raise UnsupportedVersion("This bundle was made by a different AccountDeck version; update both "
                                 "machines to the same version and export again.")
    end = _PREFIX.size + length
    if len(data) < end + _DIGEST_LEN + _TAG_LEN:
        raise CorruptBundle("The bundle is incomplete (cut off).", "truncated")
    if length == 0 or length > _MAX_HEADER:
        raise CorruptBundle("The bundle header is damaged.")
    if not hmac.compare_digest(hashlib.sha256(data[:end]).digest(), data[end:end + _DIGEST_LEN]):
        raise CorruptBundle("The bundle header is damaged.")
    parsed = _loads(data[_PREFIX.size:end])
    header = _header_from(parsed) if isinstance(parsed, dict) else None
    if header is None:
        raise CorruptBundle("The bundle header is damaged.")
    if not _valid_n(header.scrypt_n, min_scrypt_n) or not 1 <= header.scrypt_r <= 16 \
            or not 1 <= header.scrypt_p <= 4 or 128 * header.scrypt_n * header.scrypt_r > _SCRYPT_MAX_MEMORY:
        raise UnsupportedVersion("This bundle uses key settings this version does not accept.")
    aad_end = end + _DIGEST_LEN
    return header, data[:aad_end], data[aad_end:]


def _header_from(value):
    """A BundleHeader from header JSON, or None when any field is missing, extra or malformed."""
    if set(value) != {"bundle_id", "kind", "created", "kdf", "cipher", "check"}:
        return None
    kdf, cipher = value["kdf"], value["cipher"]
    if not (isinstance(kdf, dict) and set(kdf) == {"name", "n", "r", "p", "salt"} and kdf["name"] == "scrypt"
            and all(_is_int(kdf[k]) for k in ("n", "r", "p"))
            and isinstance(cipher, dict) and set(cipher) == {"name", "nonce"} and cipher["name"] == "aes-256-gcm"
            and isinstance(value["bundle_id"], str) and _BUNDLE_ID.fullmatch(value["bundle_id"])
            and value["kind"] in KINDS and _is_int(value["created"]) and value["created"] >= 0):
        return None
    try:
        salt = _unb64(kdf["salt"], _SALT_LEN)
        nonce = _unb64(cipher["nonce"], _NONCE_LEN)
        check = _unb64(value["check"], _CHECK_LEN)
    except (TypeError, ValueError):
        return None
    return BundleHeader(FORMAT_VERSION, value["bundle_id"], value["kind"], value["created"],
                        kdf["n"], kdf["r"], kdf["p"], salt, nonce, check)


def _master(passphrase, salt, n, r, p):
    secret = _normalize_passphrase(passphrase)
    return Scrypt(salt=salt, length=_KEY_LEN, n=n, r=r, p=p).derive(secret)


def _hkdf(master, info, length):
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(master)


def _normalize_passphrase(passphrase):
    """NFKC and trimmed. A passphrase made only of base32 letters, digits 2-7, spaces and dashes
    (what the PC suggested) also loses its spaces and dashes and is upper-cased, so typing it in
    another case or grouping still works."""
    if not isinstance(passphrase, str):
        raise WrongPassphrase("Enter the bundle passphrase.")
    text = unicodedata.normalize("NFKC", passphrase).strip()
    if re.fullmatch(r"[A-Za-z2-7\s-]+", text):
        text = re.sub(r"[\s-]+", "", text).upper()
    if len(text) < _MIN_PASSPHRASE:
        raise WrongPassphrase(f"The passphrase must have at least {_MIN_PASSPHRASE} characters.", "short_passphrase")
    return text.encode("utf-8")


def _valid_n(n, floor):
    return _is_int(n) and n >= max(2, floor) and n <= SCRYPT_MAX_N and n & (n - 1) == 0


# ---- payload schema (both sides) ----

_TOP_KEYS = frozenset({"format", "source_platform", "accounts", "settings"})
_ACCOUNT_REQUIRED = frozenset({"id", "provider", "identity", "label", "email", "plan", "org", "created",
                               "live_on_pc", "history", "refills"})
_ACCOUNT_OPTIONAL = frozenset({"secret"})
_HISTORY_KEYS = frozenset({"last_good_usage", "last_good_at", "readings", "snapshots"})


def _is_account_id(value):
    return isinstance(value, str) and _ACCOUNT_ID.fullmatch(value) is not None


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))


def _is_text(value):
    return isinstance(value, str) and 0 < len(value) <= _MAX_TEXT


# Row specs: field -> (check, nullable). They follow the SQLite columns (core.py) minus the
# row id and the account column, which the account entry itself implies.
_READING_KEYS = {"window_id": (_is_text, False), "at": (_is_number, False), "used": (_is_number, False),
                 "reset": (_is_number, True)}
_SNAPSHOT_KEYS = {"at": (_is_number, False), "usage": (lambda v: isinstance(v, dict), True)}
_REFILL_KEYS = {"window_id": (_is_text, False), "reset_at": (_is_number, False),
                "confirmed_at": (_is_number, False), "previous_used": (_is_number, True)}


def validate_payload(payload, kind):
    """Raise InvalidPayload (or UnsupportedVersion for another payload format) unless `payload` is
    a valid bundle payload for `kind`. Messages name fields, never values.

    {"format": 1, "source_platform": "windows"|"darwin"|"linux",
     "settings": {portable key: JSON value},
     "accounts": [{"id", "provider", "identity", "label", "email", "plan", "org", "created",
                   "live_on_pc": bool,
                   "history": {"last_good_usage": {}|null, "last_good_at": num|null,
                               "readings": [{"window_id", "at", "used", "reset"}],
                               "snapshots": [{"at", "usage"}]},
                   "refills": [{"window_id", "reset_at", "confirmed_at", "previous_used"}],
                   "secret": {"type": SECRET_TYPES[provider], "data": {...}}}]}

    `secret` is required unless `live_on_pc` and forbidden when it is; in a hand-over bundle every
    account carries one (so none is `live_on_pc`)."""
    if kind not in KINDS:
        raise WrongKind("Unknown bundle kind.")
    if not isinstance(payload, dict):
        _bad("the payload is not an object")
    unknown = set(payload) - _TOP_KEYS
    if unknown:
        _bad("unknown top-level field" + ("s " if len(unknown) > 1 else " ") + ", ".join(sorted(map(_safe_key, unknown))))
    missing = _TOP_KEYS - set(payload)
    if missing:
        _bad("missing " + ", ".join(sorted(missing)))
    if not _is_int(payload["format"]):
        _bad("format")
    if payload["format"] != PAYLOAD_FORMAT:
        raise UnsupportedVersion("This bundle was made by a different AccountDeck version; update both "
                                 "machines to the same version and export again.")
    if payload["source_platform"] not in SOURCE_PLATFORMS:
        _bad("source_platform")
    settings = payload["settings"]
    if not isinstance(settings, dict):
        _bad("settings")
    extra = set(settings) - PORTABLE_SETTINGS
    if extra:
        _bad("settings that do not travel: " + ", ".join(sorted(map(_safe_key, extra))))
    accounts = payload["accounts"]
    if not isinstance(accounts, list) or not accounts:
        _bad("accounts (needs at least one)")
    if len(accounts) > _MAX_ACCOUNTS:
        _bad("accounts (too many)")
    ids, identities = set(), set()
    for index, account in enumerate(accounts):
        _check_account(account, f"accounts[{index}]", kind)
        if account["id"] in ids:
            _bad(f"accounts[{index}].id (duplicate)")
        if (account["provider"], account["identity"]) in identities:
            _bad(f"accounts[{index}].identity (duplicate)")
        ids.add(account["id"])
        identities.add((account["provider"], account["identity"]))


def _check_account(account, where, kind):
    if not isinstance(account, dict):
        _bad(where)
    unknown = set(account) - _ACCOUNT_REQUIRED - _ACCOUNT_OPTIONAL
    if unknown:
        _bad(f"{where}: unknown field" + ("s " if len(unknown) > 1 else " ") + ", ".join(sorted(map(_safe_key, unknown))))
    missing = _ACCOUNT_REQUIRED - set(account)
    if missing:
        _bad(f"{where}: missing " + ", ".join(sorted(missing)))
    if not _is_account_id(account["id"]):
        _bad(f"{where}.id")
    if account["provider"] not in PROVIDERS:
        _bad(f"{where}.provider")
    if not (isinstance(account["identity"], str) and 0 < len(account["identity"]) <= _MAX_TEXT):
        _bad(f"{where}.identity")
    for name in ("label", "email", "plan", "org"):
        if not (isinstance(account[name], str) and len(account[name]) <= _MAX_TEXT):
            _bad(f"{where}.{name}")
    if not _is_number(account["created"]):
        _bad(f"{where}.created")
    if not isinstance(account["live_on_pc"], bool):
        _bad(f"{where}.live_on_pc")
    _check_history(account["history"], f"{where}.history")
    _check_rows(account["refills"], _REFILL_KEYS, f"{where}.refills")
    has_secret = "secret" in account
    if account["live_on_pc"] and has_secret:
        _bad(f"{where}.secret (an account still signed in on the PC must not carry a secret)")
    if kind == "handover" and not has_secret:
        _bad(f"{where}.secret (every hand-over account needs its secret)")
    if not account["live_on_pc"] and not has_secret:
        _bad(f"{where}.secret (missing)")
    if has_secret:
        secret = account["secret"]
        if not (isinstance(secret, dict) and set(secret) == {"type", "data"} and isinstance(secret["data"], dict)):
            _bad(f"{where}.secret (needs exactly a type and a data object)")
        if secret["type"] != SECRET_TYPES[account["provider"]]:
            _bad(f"{where}.secret.type (does not match the provider)")


def _check_history(history, where):
    if not isinstance(history, dict) or set(history) != _HISTORY_KEYS:
        _bad(f"{where} (needs exactly {', '.join(sorted(_HISTORY_KEYS))})")
    if history["last_good_usage"] is not None and not isinstance(history["last_good_usage"], dict):
        _bad(f"{where}.last_good_usage")
    if history["last_good_at"] is not None and not _is_number(history["last_good_at"]):
        _bad(f"{where}.last_good_at")
    _check_rows(history["readings"], _READING_KEYS, f"{where}.readings")
    _check_rows(history["snapshots"], _SNAPSHOT_KEYS, f"{where}.snapshots")


def _check_rows(rows, spec, where):
    if not isinstance(rows, list) or len(rows) > _MAX_ROWS:
        _bad(where)
    keys = set(spec)
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != keys:
            _bad(f"{where}[{index}] (needs exactly {', '.join(sorted(keys))})")
        for name, (check, nullable) in spec.items():
            value = row[name]
            if not (check(value) or (nullable and value is None)):
                _bad(f"{where}[{index}].{name}")


def _bad(detail):
    raise InvalidPayload(f"The bundle contents are not valid ({detail}).")


def _safe_key(key):
    """A field name for an error message: only short snake_case names are echoed; anything else
    (which could be a token pasted in the wrong place) shows as <unnamed>."""
    return key if isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) else "<unnamed>"


# ---- confirmation code (§2.4) ----

def confirm_code(confirm_key, bundle_id, account_ids):
    """8 base32 characters as XXXX-XXXX: HMAC-SHA256(confirm_key, label, bundle id, sorted ids),
    first 40 bits. Each id is length-prefixed, so no two id lists share an input."""
    if not isinstance(confirm_key, (bytes, bytearray)) or len(confirm_key) != _KEY_LEN:
        raise BundleError("The confirmation key is not valid.", "invalid_record")
    if not (isinstance(bundle_id, str) and _BUNDLE_ID.fullmatch(bundle_id)):
        raise BundleError("The bundle id is not valid.", "invalid_record")
    ids = sorted(set(account_ids))
    if not ids or not all(_is_account_id(key) for key in ids):
        raise BundleError("The account list is not valid.", "invalid_record")
    message = bytearray(_CODE_LABEL + b"\x00" + bytes.fromhex(bundle_id))
    for key in ids:
        raw = key.encode("utf-8")
        message += struct.pack(">I", len(raw)) + raw
    mac = hmac.new(bytes(confirm_key), bytes(message), hashlib.sha256).digest()
    code = base64.b32encode(mac[:5]).decode("ascii")
    return f"{code[:4]}-{code[4:]}"


def code_for_imported(header, keys, imported_ids):
    """The Mac side: the code for the opened bundle, but only when `imported_ids` (the accounts it
    stored) is exactly the bundle's account set; otherwise None. `keys` defaults to header.keys."""
    keys = keys if keys is not None else header.keys
    if keys is None or not header.account_ids:
        return None
    try:
        stored = set(imported_ids)
    except TypeError:
        return None
    if stored != set(header.account_ids):
        return None
    return confirm_code(keys.confirm_key, header.bundle_id, header.account_ids)


# ---- files ----

def read_bundle(path, *, max_bytes=MAX_BUNDLE_BYTES):
    """The bundle file's bytes; refuses anything over the size cap or that is not a regular file."""
    with open(path, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise NotABundle("That is not a bundle file.")
        if info.st_size > max_bytes:
            raise BundleTooLarge("The file is larger than any move bundle can be.")
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise BundleTooLarge("The file is larger than any move bundle can be.")
    return data


# ---- small helpers ----

def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _unb64(text, length):
    if not isinstance(text, str):
        raise TypeError("not text")
    raw = base64.b64decode(text.encode("ascii"), validate=True)
    if len(raw) != length:
        raise ValueError("wrong length")
    return raw


def _loads(raw):
    """Strict JSON (UTF-8, no duplicate keys, no NaN) or None. Never raises, so no decoder
    exception holding the plaintext is left on a traceback."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_name):
        raise ValueError("non-finite number")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None

