"""Generic passwords in a macOS keychain through /usr/bin/security, and the Mac vault cipher built on
one of them (docs/mac/PLAN.md D4, §1.5; docs/mac/PLATFORM-MAC.md §8).

Calls: `get`/`set`/`delete` for printable-text values (what another app's item holds, for example
Claude Code's JSON), `attributes` for an item's attributes without its value (to notice a change),
`get_bytes`/`set_bytes` for binary values (stored as unpadded base64url text, so
`find-generic-password -w` output is never ambiguous) and `get_or_create_bytes` for a random key that
two processes may create at once. `VaultCipher` is the `protect=` cipher `core.Vault` uses on macOS.

Rules the implementation keeps (PLATFORM-MAC.md §8): `security` is the only process this module starts
and the only program that ever writes an item (procs.run_security); a value that fits one `security -i`
line reaches it on stdin, never argv; every call has a timeout, because an access dialog blocks
`security`; `-A` is never used; the keychain search list and default keychain are never changed (only
the three generic-password commands run); no value, command line or `security` output is ever logged or
put in an error.

Values too long for one `security -i` line. `security -i` reads each command into a 4096-byte buffer and
runs whatever is left over as a further command (measured on macOS 26.6: a 4095-character line works,
4135 fails; a cut line even fell through to the default keychain), and `-X` hex doubles the length, so
stdin carries at most MAX_LINE characters. A longer value (up to MAX_VALUE) is passed to
`security add-generic-password -X <hex>` in argv. Claude Code (seen in 2.1.293) writes its own keychain
items the same way: a value of up to 4032 characters through `security -i` on stdin, a longer one through
`add-generic-password -X` in argv. Its credentials item comes back from its own writes in the
`apple-tool:` partition, so `security` writes that item too. Claude Code's login grows past 4032
characters once its MCP servers' sign-ins are stored with it, often several times past. This is the one
exception to "never in argv": another login user of the same Mac could read argv through the setuid `ps`
while `security` runs (PLATFORM-MAC.md §7), but Claude Code already puts this item in argv on each of its
own long writes, the user's own processes read the item through `security` without a prompt anyway, and
`security`'s `-w` prompt reads at most 128 characters (getpass). `set(..., stdin_only=True)` refuses such a
value instead, for an item whose own program never puts it in argv (agy's sign-in).

Why never Security.framework in this process: an item's data written by a program becomes that program's
in the keychain's partition list. A write from AccountDeck's python (ad-hoc signed, partition
`cdhash:<its hash>`) left Claude Code's login readable without a prompt only by that python, so every
later `security` read, AccountDeck's and Claude Code's, asked for the login keychain password until
Claude Code's next own write (seen on 2026-10-09: securityd "ACL partition mismatch: client apple-tool:").
A write by `security` keeps the item in `apple-tool:`, where both read it silently.

`security` exit codes: 36 locked, interaction not allowed or denied; 44 missing; 45 duplicate (an add
without -U of an item that exists). `-w` prints a printable-ASCII value literally and any other value as
lowercase hex without a prefix; `get_raw` undoes the hex form, and `set` refuses the one kind of
printable value that would read back as hex.

Safe mode (tests, the demo) needs an explicit keychain file inside a registered test root
(procs.register_test_root) and refuses the user's default and login keychains; procs repeats that check
before `security` starts. Outside safe mode `keychain=None` means the user's default (login) keychain.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import threading

from .. import config
from ..config import SafetyViolation
from ..core import DeckError

SECURITY = "/usr/bin/security"
TIMEOUT = 30.0          # seconds: an access dialog blocks security until someone answers it
RC_LOCKED = 36          # keychain locked, user interaction not allowed, or access denied
RC_MISSING = 44         # "The specified item could not be found in the keychain."
RC_DUPLICATE = 45       # "The specified item already exists in the keychain."
REASONS = {RC_LOCKED: "locked", RC_MISSING: "missing", RC_DUPLICATE: "duplicate"}
MESSAGES = {
    "locked": "The keychain is locked, or macOS did not let AccountDeck use it. Unlock it, or choose Allow, "
              "and try again.",
    "missing": "The keychain has no such item.",
    "duplicate": "The keychain already holds this item.",
    "failed": "The keychain could not complete the request.",
    "timeout": "The keychain did not answer in time. If macOS is asking for permission, answer it and "
               "try again.",
    "unreadable": "The keychain item holds a value AccountDeck cannot read.",
    "too_long": "The value is too long for one keychain command, so it was not stored.",
}
MAX_FIELD = 255         # service and account
MAX_VALUE = 1 << 18     # 256 KiB: its argv hex (512 KiB) stays well inside ARG_MAX (1 MiB)
MAX_LINE = 4032         # the longest `security -i` line sent; Claude Code keeps to the same 4032
_TEXT = re.compile(r"[\x20-\x7e]+")                       # printable ASCII; no newline or NUL
_PRINTABLE = re.compile(rb"[\x20-\x7e]*")
_HEX = re.compile(rb"(?:[0-9a-f]{2})+")
_B64URL = re.compile(r"[A-Za-z0-9_-]*")
_LOGIN = re.compile(r"login\.keychain(?:-db)?", re.IGNORECASE)


class KeychainError(DeckError):
    """A keychain call that did not succeed. `reason` is "locked", "missing", "duplicate", "failed",
    "timeout" or "unsupported"; `rc` is security's exit code when it ran, else None. Never carries a value."""

    def __init__(self, message: str, reason: str, rc: int | None = None):
        super().__init__(message, "vault")
        self.reason, self.rc = reason, rc


def error_for(rc: int) -> KeychainError:
    """The KeychainError for a non-zero `security` exit code: 36 locked or denied, 44 missing,
    45 duplicate, anything else failed."""
    reason = REASONS.get(rc, "failed")
    return KeychainError(MESSAGES[reason], reason, rc)


def check_keychain(keychain) -> Path | None:
    """The keychain file a call may use, or None for the user's default keychain. A path must be absolute
    (`security` would look a bare name up in ~/Library/Keychains). In safe mode the default keychain, the
    login keychain and any file outside a registered test root are refused before anything runs."""
    if keychain is None:
        if config.safe_mode():
            raise _refused("Safe mode never uses the login keychain: pass a test keychain file.")
        return None
    path = Path(keychain)
    if not path.is_absolute():
        raise DeckError("A keychain must be given as a full file path.", "invalid")
    if config.safe_mode():
        from ..procs import is_test_owned_path   # procs imports config; imported on use, like config does
        if _LOGIN.fullmatch(path.name) or not is_test_owned_path(path):
            raise _refused("Safe mode uses only a keychain file inside a test folder.")
    return path


def check_item(service: str, account: str, value: str | None = None) -> None:
    """Service, account and value must be printable ASCII within `security`'s limits."""
    fields = [("service", service, MAX_FIELD), ("account", account, MAX_FIELD)]
    if value is not None:
        fields.append(("value", value, MAX_VALUE))
    for name, text, limit in fields:
        if not isinstance(text, str) or len(text) > limit or not _TEXT.fullmatch(text):
            raise DeckError(f"A keychain {name} must be printable text of at most {limit} characters.", "invalid")


# ---- text values ----

def get(service: str, account: str, *, keychain=None) -> str | None:
    """The item's value as text, or None when the keychain has no such item (rc 44)."""
    raw = get_raw(service, account, keychain=keychain)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise KeychainError(MESSAGES["unreadable"], "failed") from None


def get_raw(service: str, account: str, *, keychain=None) -> bytes | None:
    """The item's exact stored bytes (the `-w` hex form undone), or None when there is no such item."""
    path = check_keychain(keychain)
    check_item(service, account)
    rc, out, _err = _run(["find-generic-password", "-a", account, "-s", service, "-w", *_tail(path)], path)
    if rc == RC_MISSING:
        return None
    if rc != 0:
        raise error_for(rc)
    return decode_w(out)


def set(service: str, account: str, value: str, *, keychain=None, replace: bool = True,  # noqa: A001
        stdin_only: bool = False) -> None:
    """Stores `value` (printable ASCII) through `security -i` on stdin. With `replace=False` an existing
    item is left alone and KeychainError("duplicate") is raised (rc 45): two processes creating the same
    key at once then keep the first one's value. With `stdin_only` a value too long for one stdin line is
    refused (KeychainError "unsupported", before anything runs) instead of going to `-X` in argv: for an
    item whose value must never appear in a process list (agy's sign-in, providers/antigravity.py)."""
    path = check_keychain(keychain)
    check_item(service, account, value)
    if _reads_back_as_hex(value.encode("ascii")):
        raise DeckError("A keychain value made only of hex digits would read back differently.", "invalid")
    line = _add_line(service, account, value, path, replace)
    if len(line) > MAX_LINE:
        if stdin_only:
            raise KeychainError(MESSAGES["too_long"], "unsupported")
        _set_long(service, account, value.encode("ascii"), path, replace)
        return
    rc, _out, err = _run(["-i"], path, stdin=(line + "\n").encode("ascii"))
    if rc != 0:
        raise error_for(rc)
    if err.strip():                     # security -i printed an error but exited 0: not stored for sure
        raise KeychainError(MESSAGES["failed"], "failed", rc)


def fits_line(service: str, account: str, value: str, *, keychain=None, replace: bool = True) -> bool:
    """Whether `set` sends `value` on one `security -i` line (stdin) rather than in argv. Runs nothing."""
    return len(_add_line(service, account, value, None if keychain is None else Path(keychain), replace)) <= MAX_LINE


def _add_line(service: str, account: str, value: str, path: Path | None, replace: bool) -> str:
    return " ".join(["add-generic-password", *(["-U"] if replace else []), "-a", quote(account),
                     "-s", quote(service), "-w", quote(value), *[quote(str(p)) for p in _tail(path)]])


def attributes(service: str, account: str, *, keychain=None, timeout: float | None = None) -> bytes | None:
    """What `find-generic-password` prints about the item without -w or -g: its attributes (keychain, class,
    service, account, creation and modification dates), never its value. It asks nothing, also on a locked
    keychain (doctor.py's measurement), so it is the cheap way to notice that an item changed: compare this
    output, or a digest of it. None when there is no such item (rc 44). Callers never log it. `timeout` (seconds)
    replaces TIMEOUT for this one call: a caller on a loop that must not stall gives up sooner (it asks nothing, so
    no dialog needs the time); KeychainError "timeout" then."""
    path = check_keychain(keychain)
    check_item(service, account)
    rc, out, _err = _run(["find-generic-password", "-a", account, "-s", service, *_tail(path)], path,
                         timeout=TIMEOUT if timeout is None else timeout)
    if rc == RC_MISSING:
        return None
    if rc != 0:
        raise error_for(rc)
    return out


def delete(service: str, account: str, *, keychain=None) -> bool:
    """True when the item was deleted, False when there was none (rc 44)."""
    path = check_keychain(keychain)
    check_item(service, account)
    rc, _out, _err = _run(["delete-generic-password", "-a", account, "-s", service, *_tail(path)], path)
    if rc == RC_MISSING:
        return False
    if rc != 0:
        raise error_for(rc)
    return True


def _set_long(service: str, account: str, data: bytes, path: Path | None, replace: bool) -> None:
    """`set` for a value too long for one `security -i` line: `-X` hex in argv (see the module docstring)."""
    rc, _out, _err = _run(["add-generic-password", *(["-U"] if replace else []), "-a", account, "-s", service,
                           "-X", data.hex(), *_tail(path)], path)
    if rc != 0:
        raise error_for(rc)


# ---- binary values (base64url text in the keychain) ----

def set_bytes(service: str, account: str, data: bytes, *, keychain=None, replace: bool = True) -> None:
    """Stores binary `data` (1 byte to 192 KiB) as unpadded base64url text."""
    if not isinstance(data, (bytes, bytearray)):
        raise DeckError("A keychain binary value must be bytes.", "invalid")
    set(service, account, base64.urlsafe_b64encode(bytes(data)).decode("ascii").rstrip("="),
        keychain=keychain, replace=replace)


def get_bytes(service: str, account: str, *, keychain=None) -> bytes | None:
    """A value `set_bytes` stored, or None when there is no such item."""
    text = get(service, account, keychain=keychain)
    if text is None:
        return None
    if not _B64URL.fullmatch(text) or len(text) % 4 == 1:
        raise KeychainError(MESSAGES["unreadable"], "failed")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def get_or_create_bytes(service: str, account: str, size: int = 32, *, keychain=None) -> bytes:
    """The item's `size`-byte value, created from os.urandom when there is none. Safe against another
    process creating it at the same moment: the create never replaces (rc 45), and the loser reads the
    winner's value. A stored value of another length is never overwritten: KeychainError("failed")."""
    for _attempt in range(3):
        found = get_bytes(service, account, keychain=keychain)
        if found is not None:
            if len(found) != size:
                raise KeychainError(MESSAGES["unreadable"], "failed")
            return found
        fresh = os.urandom(size)
        try:
            set_bytes(service, account, fresh, keychain=keychain, replace=False)
            return fresh
        except KeychainError as error:
            if error.reason != "duplicate":
                raise
    raise KeychainError(MESSAGES["failed"], "failed")


# ---- the Mac vault cipher (D4, §1.5) ----

VAULT_SERVICE = "AccountDeck2 vault"
VAULT_KEY_SIZE = 32
VAULT_MAGIC = b"ADK1"           # record: magic(4) || nonce(12) || AES-256-GCM ciphertext || tag(16)
_NONCE = 12
_TAG = 16


def vault_account(data_dir) -> str:
    """The keychain account of a data folder's vault key: sha256 of its resolved path, 16 hex digits."""
    return hashlib.sha256(str(Path(data_dir).resolve()).encode("utf-8")).hexdigest()[:16]


class VaultCipher:
    """`protect(data, decrypt=False)` for core.Vault on macOS: AES-256-GCM under a random 32-byte key kept
    in the keychain (service "AccountDeck2 vault", account vault_account(data_dir)). The key is created on
    the first encryption only; decrypting never creates one, so a missing key fails closed instead of
    hiding records behind a new key. The key is read once and kept for the life of this object."""

    def __init__(self, data_dir, *, keychain=None):
        self.account = vault_account(data_dir)
        self.keychain = keychain
        self._key: bytes | None = None
        self._lock = threading.Lock()

    def __call__(self, data, decrypt: bool = False) -> bytes:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        data = bytes(data)
        if not decrypt:
            nonce = os.urandom(_NONCE)
            return VAULT_MAGIC + nonce + AESGCM(self._vault_key(create=True)).encrypt(nonce, data, VAULT_MAGIC)
        if not data.startswith(VAULT_MAGIC) or len(data) < len(VAULT_MAGIC) + _NONCE + _TAG:
            raise DeckError("This saved sign-in was not protected on this Mac. Reconnect this account.", "vault")
        nonce, body = data[len(VAULT_MAGIC):len(VAULT_MAGIC) + _NONCE], data[len(VAULT_MAGIC) + _NONCE:]
        try:
            return AESGCM(self._vault_key(create=False)).decrypt(nonce, body, VAULT_MAGIC)
        except InvalidTag:
            raise DeckError("This saved sign-in could not be read. Reconnect this account.", "vault") from None

    def _vault_key(self, *, create: bool) -> bytes:
        with self._lock:
            if self._key is None:
                if create:
                    self._key = get_or_create_bytes(VAULT_SERVICE, self.account, VAULT_KEY_SIZE,
                                                    keychain=self.keychain)
                else:
                    key = get_bytes(VAULT_SERVICE, self.account, keychain=self.keychain)
                    if key is None:
                        raise KeychainError("The keychain no longer holds this data folder's vault key.",
                                            "missing", RC_MISSING)
                    if len(key) != VAULT_KEY_SIZE:
                        raise KeychainError(MESSAGES["unreadable"], "failed")
                    self._key = key
            return self._key


# ---- helpers ----

def quote(text: str) -> str:
    """One `security -i` argument: double quotes, with backslash and double quote escaped."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def decode_w(out: bytes) -> bytes:
    """The value `find-generic-password -w` printed: one trailing newline removed, and the hex form (a
    value security would not print literally, shown as lowercase hex digits) decoded."""
    text = out[:-1] if out.endswith(b"\n") else out
    if _HEX.fullmatch(text):
        raw = bytes.fromhex(text.decode("ascii"))
        if not _PRINTABLE.fullmatch(raw):
            return raw
    return text


def _reads_back_as_hex(value: bytes) -> bool:
    return decode_w(value + b"\n") != value


def _tail(path: Path | None) -> list[str]:
    return [] if path is None else [str(path)]


def _run(args: list[str], path: Path | None, stdin: bytes | None = None, *,
         timeout: float = TIMEOUT) -> tuple[int, bytes, bytes]:
    from .. import procs                 # procs imports this package; imported on use
    try:
        return procs.run_security(args, keychain=path, stdin=stdin, timeout=timeout)
    except TimeoutError:
        raise KeychainError(MESSAGES["timeout"], "timeout") from None
    except OSError:
        raise KeychainError(MESSAGES["failed"], "failed") from None


def _refused(message: str) -> SafetyViolation:
    error = SafetyViolation(message)
    error.reason = "keychain"
    return error
