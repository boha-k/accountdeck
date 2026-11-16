"""Claude Desktop's Chromium/Electron credential encryption on macOS (docs/mac/PLAN.md D5, §1.6;
docs/mac/PROVIDERS-MAC.md §2).

Electron's safeStorage (the `oauth:tokenCache*` values in config.json) and Chromium's os_crypt (every cookie
`encrypted_value`) share one key and one framing: the key is PBKDF2-HMAC-SHA1(password, b"saltysalt", 1003
rounds, 16 bytes) of the login-keychain password of service "Claude Safe Storage", and a value is b"v10" +
AES-128-CBC with an IV of sixteen spaces and PKCS#7 padding.

The seam (`ChromiumCryptoMac`):
- `key_ref()` is what a profile's "key" field holds for profiles this Mac writes: the constant `MAC_KEY_REF`
  (the real key lives in the Keychain, so reading the marker never prompts).
- `key_for(key_ref)` unlocks the key a saved profile was written with; `current_key()` unlocks this Mac's key.
  Both return a `ChromiumKey` with `decrypt(blob)` and `encrypt(plaintext)`.
- `decrypt(blob)` / `encrypt(plaintext)` are shorthands for `current_key()`.

Errors: a blob that does not open (unknown prefix, wrong key, tampered bytes) raises `ChromiumDecryptError`
(a ValueError). A Keychain item that is missing (Claude Desktop never opened), locked or refused raises
`ChromiumKeyNotReady`, a DeckError callers catch to defer work. A key, password or plaintext is never logged
and never put in an error message.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import hashlib
import threading
import time

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..core import DeckError
from ..platform import keychain as keychain_store

V10 = b"v10"
MAC_SALT = b"saltysalt"
MAC_ITERATIONS = 1003
MAC_KEY_LENGTH = 16
MAC_IV = b" " * 16
# Electron sets the Keychain service to "<app name> Safe Storage" and looks the password up under the
# account "<app name> Key" first, then under the legacy "<app name>", which it migrates to the first and
# deletes (electron/patches/chromium/feat_ensure_mas_builds_of_the_same_application_can_use_safestorage
# .patch; shell/browser/electron_browser_main_parts.cc). The lookup here keeps that order. The Mac App
# Store account ("<app name> App Store Key") is deliberately absent: Claude ships outside the store, and a
# stale store item must never be taken for the live key.
SAFE_STORAGE_SERVICE = "Claude Safe Storage"
SAFE_STORAGE_ACCOUNTS = ("Claude Key", "Claude")
MAC_KEY_REF = "keychain:Claude Safe Storage"
# How long a failed Keychain read is remembered before the next real attempt (seconds). A refused or
# timed-out read may have shown an access dialog, so it is not retried on every usage read.
NOT_READY_RETRY = {"missing": 60.0, "locked": 600.0}
NOT_READY_RETRY_DEFAULT = 600.0

_NOT_READY_MESSAGES = {
    "missing": "Open Claude Desktop on this Mac once, then try again. It creates the key AccountDeck needs.",
    "locked": "macOS did not let AccountDeck read Claude Desktop's key. Unlock the keychain or choose Allow "
              "when macOS asks, then try again.",
}
_NOT_READY_DEFAULT = "Claude Desktop's key could not be read from the keychain. Try again in a moment."


class ChromiumDecryptError(ValueError):
    """A value that does not open with the key: unknown prefix, wrong key or changed bytes."""


class ChromiumKeyNotReady(DeckError):
    """Claude Desktop's Keychain key cannot be read now. `reason` is "missing" (Claude Desktop
    has not been opened on this Mac), "locked" (keychain locked, or access refused), or another
    KeychainError reason ("failed", "unsupported"). Callers defer the work; nothing was changed."""

    def __init__(self, reason: str, rc: int | None = None):
        super().__init__(_NOT_READY_MESSAGES.get(reason, _NOT_READY_DEFAULT),
                         "not_installed" if reason == "missing" else "vault")
        self.reason, self.rc = reason, rc


# ---- primitives (pure; the known-answer tests call these directly) ----

def derive_mac_key(password: str | bytes) -> bytes:
    """Chromium's macOS key: PBKDF2-HMAC-SHA1(password, "saltysalt", 1003, 16 bytes)."""
    secret = password.encode("utf-8") if isinstance(password, str) else bytes(password)
    return hashlib.pbkdf2_hmac("sha1", secret, MAC_SALT, MAC_ITERATIONS, MAC_KEY_LENGTH)


def seal_cbc(key: bytes, plaintext: bytes) -> bytes:
    """b"v10" + AES-128-CBC(PKCS#7(plaintext)) with the fixed IV, as macOS Chromium writes it."""
    padder = padding.PKCS7(128).padder()
    padded = padder.update(bytes(plaintext)) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_cbc_key(key)), modes.CBC(MAC_IV)).encryptor()
    return V10 + encryptor.update(padded) + encryptor.finalize()


def open_cbc(key: bytes, blob: bytes) -> bytes:
    """The plaintext of a macOS Chromium value; ChromiumDecryptError when it does not open. CBC carries no
    tag, so a wrong key is caught only by the padding check here and by what the caller checks next (the
    cookie host hash, the token cache JSON)."""
    blob = bytes(blob)
    body = blob[3:]
    if blob[:3] != V10 or not body or len(body) % 16:
        raise ChromiumDecryptError("unknown encryption format")
    decryptor = Cipher(algorithms.AES(_cbc_key(key)), modes.CBC(MAC_IV)).decryptor()
    padded = decryptor.update(body) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        raise ChromiumDecryptError("the value does not open with this key") from None


def _cbc_key(key: bytes) -> bytes:
    key = bytes(key)
    if len(key) != MAC_KEY_LENGTH:
        raise ValueError("A macOS Chromium key is 16 bytes.")
    return key


# ---- unlocked keys ----

class ChromiumKey(ABC):
    """An unlocked key. `prefixes` are the version tags its values carry. Never printed."""
    os: str = ""
    prefixes: tuple[bytes, ...] = (V10,)

    @abstractmethod
    def decrypt(self, blob: bytes) -> bytes: ...

    @abstractmethod
    def encrypt(self, plaintext: bytes) -> bytes: ...

    def __repr__(self):
        return f"<ChromiumKey {self.os}>"


class _CbcKey(ChromiumKey):
    os, prefixes = "mac", (V10,)

    def __init__(self, key: bytes):
        self._key = _cbc_key(key)

    def decrypt(self, blob):
        return open_cbc(self._key, blob)

    def encrypt(self, plaintext):
        return seal_cbc(self._key, plaintext)


# ---- the seam ----

class ChromiumCrypto(ABC):
    """See the module docstring. `os` names the format ("mac"); `verifies_restore` asks the Claude adapter
    to prove the current key opens a profile before restoring it (needed where `key_ref` is a marker)."""
    os: str = ""
    verifies_restore: bool = False

    @abstractmethod
    def key_ref(self) -> str:
        """What a profile captured on this machine stores in its "key" field."""

    @abstractmethod
    def key_for(self, key_ref) -> ChromiumKey:
        """The key a profile with this "key" field was written with. Pass the field as saved (a missing
        field is treated as unrecognized, never as "this machine's key")."""

    def current_key(self) -> ChromiumKey:
        """This machine's key: what Claude Desktop here encrypts with now."""
        return self.key_for(self.key_ref())

    def decrypt(self, blob: bytes) -> bytes:
        return self.current_key().decrypt(blob)

    def encrypt(self, plaintext: bytes) -> bytes:
        return self.current_key().encrypt(plaintext)

    def __repr__(self):
        return f"<{type(self).__name__}>"


class ChromiumCryptoMac(ChromiumCrypto):
    """macOS: the "Claude Safe Storage" Keychain password through PBKDF2 to an AES-128-CBC key.

    `keychain` is the keychain file (None: the user's default keychain; safe mode refuses that and
    accepts only a test keychain inside a registered test root). `password` (tests and the demo) replaces
    the Keychain read with a callable returning the Safe Storage password. The password is read only when a
    key is first needed, never by `key_ref()`, and the derived key is then kept in memory for this object's
    lifetime, so macOS shows its access dialog at most once per process. A failed read is remembered for
    `NOT_READY_RETRY` seconds (no dialog storm from background reads); `forget()` clears both."""
    os = "mac"
    verifies_restore = True

    def __init__(self, *, keychain=None, service: str = SAFE_STORAGE_SERVICE,
                 accounts: tuple[str, ...] = SAFE_STORAGE_ACCOUNTS, clock=time.monotonic, password=None):
        if not accounts:
            raise ValueError("At least one Keychain account name is needed.")
        self.keychain, self.service, self.accounts = keychain, service, tuple(accounts)
        self.clock = clock
        self._read_password = password
        self._lock = threading.Lock()
        self._key: _CbcKey | None = None
        self._failure: tuple[str, int | None, float] | None = None   # (reason, rc, retry at)

    def key_ref(self) -> str:
        return MAC_KEY_REF

    def key_for(self, key_ref) -> ChromiumKey:
        if key_ref != MAC_KEY_REF:
            raise DeckError("This Claude account was saved on another computer and is not converted for this Mac "
                            "yet.", "unsupported")
        with self._lock:
            if self._key is None:
                now = self.clock()
                if self._failure is not None and now < self._failure[2]:
                    raise ChromiumKeyNotReady(self._failure[0], self._failure[1])
                try:
                    password = self._password()
                except ChromiumKeyNotReady as exc:
                    retry = NOT_READY_RETRY.get(exc.reason, NOT_READY_RETRY_DEFAULT)
                    self._failure = (exc.reason, exc.rc, now + retry)
                    raise
                self._key, self._failure = _CbcKey(derive_mac_key(password)), None
            return self._key

    def forget(self) -> None:
        """Drops the remembered key or failure; the next use reads the Keychain again."""
        with self._lock:
            self._key = self._failure = None

    def _password(self) -> str:
        """Electron's lookup: the first account that has the item wins; any error other than "no such
        item" stops the lookup (Electron does not fall back either). Safety refusals propagate."""
        if self._read_password is not None:
            value = self._read_password()
            if not isinstance(value, str) or not value:
                raise ChromiumKeyNotReady("missing")
            return value
        for account in self.accounts:
            try:
                value = keychain_store.get(self.service, account, keychain=self.keychain)
            except keychain_store.KeychainError as exc:
                raise ChromiumKeyNotReady(exc.reason, exc.rc) from None
            if value is not None:
                if not value:
                    raise ChromiumKeyNotReady("failed")
                return value
        raise ChromiumKeyNotReady("missing")
