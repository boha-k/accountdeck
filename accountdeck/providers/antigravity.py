"""Antigravity's command-line tool `agy`: where it keeps its Google sign-in, the record AccountDeck saves and puts
back whole, its usage answer, and where `agy` is installed.

AccountDeck never sends a request to Google and never uses an Antigravity token itself. It reads the keychain item
agy keeps its sign-in in, keeps a copy in its vault (`agy-<account id>`, core.agy_key), and writes a copy back when
the user switches. The only program that talks to Google is the user's own `agy`. AccountDeck starts it for its
usage command (`procs.ProcessController.run_agy_usage`, parsed by `parse_usage`) on Refresh or when the user opens
AccountDeck, for `agy --version` once per installed binary when the user looks or acts on Antigravity (never by
itself on a timer: `Antigravity.version`), and in the Terminal window of an Add. Nothing under ~/.gemini is ever
written; a file there is only looked at to see whether it exists (`file_store`).

The store. Version 1 knows one, "cli": the generic password agy keeps in the login keychain, service "gemini",
account "antigravity" (agy's item, not AccountDeck's). Its value is STORE_PREFIX followed by the base64 of the
record's JSON. Reading and writing go through platform/keychain.py, never a new way of running `security`.
AccountDeck never changes a field of the record: it stores and restores it whole.

A saved sign-in in the vault (`saved_record`): {"v": 1, "email": <lower case>, "saved_at": <time>, "stores":
{"cli": <the record exactly as read from the keychain, decoded>}}. `stores` is a dictionary so that another store
can join later.

What agy prints is untrusted: `parse_usage` reads at most USAGE_MAX_CHARS of it, strictly, and no text of it reaches
a log or an error message; `run_outcome` tells the ways a usage run can fail apart without guessing.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import unicodedata

from ..contract import AGY_BUCKETS_MAX, AGY_GROUPS_MAX, AGY_STORE_REASONS, AGY_STORE_STATES, AGY_TEXT_MAX
from ..core import DeckError
from ..platform import facts_mac, keychain as keychains

__all__ = ["PROVIDER", "STORE_SERVICE", "STORE_ACCOUNT", "STORE_PREFIX", "RECORD_VERSION", "NOT_CHECKED",
           "PROBE_TIMEOUT", "StoreRead", "KeychainStore", "Antigravity", "saved_record", "parse_usage",
           "check_record", "encode_value", "decode_value", "run_outcome", "reading_from_run", "RUN_OUTCOMES"]

PROVIDER = "antigravity"
STORE_SERVICE, STORE_ACCOUNT = "gemini", "antigravity"    # agy's keychain item
STORE_PREFIX = "go-keyring-base64:"                       # how agy's keychain value starts
RECORD_VERSION = 1                                        # of a saved sign-in in the vault (`saved_record`)
NOT_CHECKED = "AccountDeck has not checked Antigravity's sign-in yet."
# The probe's own time limit: it asks nothing (no dialog needs the time), and the engine's tick waits for it.
PROBE_TIMEOUT = 5.0


@dataclass(frozen=True)
class StoreRead:
    """What one read of agy's store found. `state` is one of contract.AGY_STORE_STATES:

    absent   no item: nobody is signed in to agy
    ok       `record` is the decoded record, `email` its id_token_email in lower case, `plan` its tier_display_name
             and `tier` its user_tier (each text or None)
    unknown  `reason` (one of contract.AGY_STORE_REASONS) and `message` (short, for the dashboard) say why; it blocks
             Save, Add and Switch

    `record` holds the refresh token: it is left out of repr, and nothing logs or shows it."""
    state: str
    email: str | None = None
    plan: str | None = None
    tier: str | None = None
    reason: str | None = None
    message: str | None = None
    record: dict | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.state not in AGY_STORE_STATES:
            raise ValueError("not a store state")
        if (self.state == "unknown") != (self.reason is not None) or (
                self.reason is not None and self.reason not in AGY_STORE_REASONS):
            raise ValueError("a reason is given exactly when the state is unknown")
        if (self.state == "ok") != (self.record is not None and bool(self.email)):
            raise ValueError("a record and its e-mail are given exactly when the state is ok")

    @classmethod
    def absent(cls) -> StoreRead:
        return cls("absent")

    @classmethod
    def ok(cls, record: dict, email: str, *, plan: str | None = None, tier: str | None = None) -> StoreRead:
        return cls("ok", email=email.strip().lower(), plan=plan, tier=tier, record=record)

    @classmethod
    def unknown(cls, reason: str, message: str) -> StoreRead:
        return cls("unknown", reason=reason, message=message)


# agy's own write (github.com/zalando/go-keyring on macOS): `security -i` reading the line
# "add-generic-password -U -s <service> -a <account> -w <value>\n", each part shell-quoted, refused when that line is
# longer than 4096 bytes (agy then falls back to a file). AccountDeck writes the same item with the same command
# through keychain.set(stdin_only=True): on stdin only, never in argv, and only when the value also fits agy's line.
AGY_COMMAND_MAX = 4096
STORE_VALUE_MAX = 64 << 10           # far beyond anything agy can write; a longer value is not agy's
_SHELL_SAFE = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")   # what go-shellescape leaves unquoted
_EMAIL = re.compile(r"[^\s@]{1,64}@[^\s@]{1,255}")

UNREADABLE = {
    "locked": "The keychain is locked, or macOS did not let AccountDeck read Antigravity's sign-in. Unlock it, or "
              "choose Allow, and try again.",
    "timeout": "The keychain did not answer in time when AccountDeck read Antigravity's sign-in. If macOS is asking "
               "for permission, answer it and try again.",
}
UNREADABLE_OTHER = "AccountDeck could not read Antigravity's sign-in from the keychain."
UNDECODABLE = "Antigravity's sign-in in the keychain is in a form AccountDeck does not know, so it leaves it alone."
INCOMPLETE = "Antigravity's sign-in in the keychain lacks a part AccountDeck needs (its refresh token or its e-mail)."
NOT_CONSUMER = ("Antigravity is signed in with a work or Google Cloud sign-in. AccountDeck handles personal Google "
                "sign-ins only.")
FILE_STORE = ("agy keeps its sign-in in a file on this Mac instead of the keychain. AccountDeck does not handle that "
              "yet, so it saves and switches nothing.")
TOO_LARGE = ("This Antigravity sign-in is too large for agy's keychain item, so AccountDeck cannot put it there. "
             "Nothing was changed.")
NOT_A_RECORD = "That is not an Antigravity sign-in AccountDeck can put in the keychain. Nothing was changed."
NOT_WRITTEN = "AccountDeck wrote Antigravity's sign-in to the keychain, but reading it back gave another account."
KEYCHAIN_BUSY = ("The keychain is locked or did not answer, so AccountDeck could not {what}. Unlock it, or choose "
                 "Allow when macOS asks, and try again.")
KEYCHAIN_FAILED = "The keychain did not let AccountDeck {what}."


class KeychainStore:
    """One place agy keeps its sign-in: a generic password in the login keychain (`keychain` None; a test passes a
    keychain file of its own). `name` is the store's name in a saved record's `stores` ("cli").

    Every call is one `security` command through platform/keychain.py; nothing here logs, raises or shows a value or
    a part of the record."""

    def __init__(self, name: str, service: str, account: str, *, keychain=None):
        self.name, self.service, self.account, self.keychain = name, service, account, keychain

    def read(self) -> StoreRead:
        """Reads and decodes the item. Absent when there is none; unknown when it cannot be read (locked, refused,
        timed out: "unreadable"), does not decode ("undecodable"), is not a personal sign-in (auth_method is not
        "consumer": "not_consumer"), or lacks token.refresh_token or id_token_email ("incomplete"). Never raises for
        any of those."""
        try:
            raw = keychains.get_raw(self.service, self.account, keychain=self.keychain)
        except keychains.KeychainError as exc:
            return StoreRead.unknown("unreadable", UNREADABLE.get(exc.reason, UNREADABLE_OTHER))
        if raw is None:
            return StoreRead.absent()
        try:
            record = decode_value(raw)
        except ValueError:
            return StoreRead.unknown("undecodable", UNDECODABLE)
        return check_record(record)

    def fits(self, record) -> bool:
        """Whether `write` would take `record`: a personal sign-in that agy's own write could store and one
        `security -i` line can carry. Add asks this before it deletes the item, so the earlier sign-in can always go
        back."""
        try:
            self._value(record)
        except DeckError:
            return False
        return True

    def write(self, record: dict) -> None:
        """Writes `record` whole, in agy's encoding and with agy's own command, then reads it back: the e-mail must
        be the one written, else DeckError (code "antigravity"). Refused before anything runs (DeckError,
        "antigravity") when `record` is not a personal sign-in or is too long for agy's own write: it is never
        written another way. Only Switch, Add's put-back and Recover write."""
        value, email = self._value(record)
        try:
            keychains.set(self.service, self.account, value, keychain=self.keychain, replace=True, stdin_only=True)
        except keychains.KeychainError as exc:
            if exc.reason == "unsupported":
                raise DeckError(TOO_LARGE, "antigravity") from None
            raise _keychain_error(exc, "put Antigravity's sign-in there") from None
        back = self.read()
        if back.state != "ok" or back.email != email:
            raise DeckError(NOT_WRITTEN, "antigravity")

    def delete(self) -> bool:
        """Removes the item (Add, before agy shows its sign-in screen): a local sign-out that revokes nothing at
        Google. False when there was none."""
        try:
            return keychains.delete(self.service, self.account, keychain=self.keychain)
        except keychains.KeychainError as exc:
            raise _keychain_error(exc, "remove Antigravity's sign-in") from None

    def probe(self, *, timeout: float = PROBE_TIMEOUT) -> str | None:
        """A cheap marker of the item's last change, from its attributes without its secret (agy rewrites the item
        about hourly as it renews its token): sensing reads the record only when this changed. None when there is no
        item; DeckError when the keychain does not answer within `timeout` seconds (its `reason` is the keychain's:
        "timeout", "locked", …). The keychain keeps the modification date to the second, so two writes within one
        second can leave the same marker: after a write of its own, read the store again instead of trusting the
        marker."""
        try:
            out = keychains.attributes(self.service, self.account, keychain=self.keychain, timeout=timeout)
        except keychains.KeychainError as exc:
            reason = exc.reason
            error = _keychain_error(exc, "look at Antigravity's sign-in")
        else:
            return None if out is None else "item:" + hashlib.sha256(out).hexdigest()[:32]
        error.reason = reason           # raised outside the handler, so the keychain's own error is not chained to it
        raise error

    def _value(self, record) -> tuple[str, str]:
        """(the keychain value, the e-mail it must read back as) for `record`, or DeckError."""
        found = check_record(record) if isinstance(record, dict) else None
        value = None
        if found is not None and found.state == "ok":
            try:
                value = encode_value(record)
            except (TypeError, ValueError, RecursionError):
                pass                    # refused below, outside the handler, so no error of the record's hangs behind
        if value is None:
            raise DeckError(NOT_A_RECORD, "antigravity")
        if (agy_command_length(self.service, self.account, value) > AGY_COMMAND_MAX
                or not keychains.fits_line(self.service, self.account, value, keychain=self.keychain)):
            raise DeckError(TOO_LARGE, "antigravity")
        return value, found.email


class Antigravity:
    """agy on this Mac, for the Antigravity service (agy_accounts.AgyService): whether and where it is installed, its
    version, and its sign-in store. It is not one of the engine's usage providers (`engine.providers`), so nothing
    automatic (reads, auto-switch, the runway, alerts) ever reaches it.

    `procs` is the engine's process controller (it runs the usage command and `agy --version`, and opens the
    sign-in's Terminal). In a synthetic home (tests, the demo) the store is read only when a test gave it a keychain
    file of its own; otherwise it reads as not checked and nothing is touched."""

    provider = PROVIDER

    def __init__(self, paths, procs=None):
        self.paths, self.procs = paths, procs
        self.stores = {"cli": KeychainStore("cli", STORE_SERVICE, STORE_ACCOUNT)}
        self._versions: dict[tuple, str | None] = {}
        self._versions_lock = threading.Lock()

    def store(self, name: str = "cli") -> KeychainStore:
        return self.stores[name]

    def executable(self) -> Path | None:
        """agy's executable, found as Codex's is (facts_mac.agy_exe): ~/.local/bin/agy, the Homebrew folders, then
        PATH; `paths.agy_exe` in a synthetic home. None when agy is not installed. Cheap: a few file checks, no
        process (sensing asks on every tick)."""
        return facts_mac.agy_exe(self.paths)

    def known_version(self) -> tuple[bool, str | None]:
        """(checked, version) for the agy installed now, without running anything: whether `version` has asked this
        binary already, and what it said (None when it could not tell). (False, None) before the first check, or
        when agy is not installed. Cheap: file checks only (the doctor's cycle in the service asks this)."""
        exe = self.executable()
        key = facts_mac.file_identity(exe) if exe is not None else None
        with self._versions_lock:
            if key is None or key not in self._versions:
                return False, None
            return True, self._versions[key]

    def version(self) -> str | None:
        """agy's version text, from `agy --version` through the process controller, remembered for each executable
        until its file changes (its resolved path, size and modification time): it runs at most once for each
        installed binary. None when it cannot be told. Only the user's look or action asks for it (AgyService);
        the doctor's cycle in the service shows `known_version`."""
        exe = self.executable()
        key = facts_mac.file_identity(exe) if exe is not None else None
        if key is None or self.procs is None:
            return None
        with self._versions_lock:
            if key in self._versions:
                return self._versions[key]
        try:
            found = self.procs.agy_version(exe)
        except (DeckError, OSError):
            found = None
        with self._versions_lock:
            if len(self._versions) >= 8:
                self._versions.clear()
            self._versions[key] = found
        return found

    def file_store(self) -> Path | None:
        """The file agy would keep its sign-in in instead of the keychain (`paths.agy_file_stores`), when one exists
        (a link counts too): then the store reads as unknown ("file_store") and nothing is saved or switched. Only
        whether it exists is looked at; it is never opened."""
        for path in self.paths.agy_file_stores:
            if os.path.lexists(path):
                return Path(path)
        return None

    def read(self, name: str = "cli") -> StoreRead:
        """The store's state: unknown ("file_store") while agy keeps a file store, else `KeychainStore.read`."""
        store = self.store(name)
        if self.file_store() is not None:
            return StoreRead.unknown("file_store", FILE_STORE)
        if not self._reachable(store):
            return StoreRead.unknown("not_checked", NOT_CHECKED)
        return store.read()

    def probe(self, name: str = "cli") -> str | None:
        """`KeychainStore.probe` of the store, marked while a file store exists, so a file store that appears or goes
        away moves the marker too. None when there is neither."""
        store = self.store(name)
        marker = store.probe() if self._reachable(store) else None
        if self.file_store() is not None:
            return "file:" + (marker or "none")
        return marker

    def _reachable(self, store: KeychainStore) -> bool:
        return self.paths.mode == "live" or store.keychain is not None


def check_record(record: dict) -> StoreRead:
    """What a decoded record is to AccountDeck: ok (with its e-mail, plan and tier), or unknown when it is not a
    personal sign-in ("not_consumer") or lacks its refresh token or a usable e-mail ("incomplete")."""
    if record.get("auth_method") != "consumer":
        return StoreRead.unknown("not_consumer", NOT_CONSUMER)
    token = record.get("token")
    refresh = token.get("refresh_token") if isinstance(token, dict) else None
    email = record.get("id_token_email")
    if not isinstance(refresh, str) or not refresh.strip() or not _is_email(email):
        return StoreRead.unknown("incomplete", INCOMPLETE)
    return StoreRead.ok(record, email, plan=_label(record.get("tier_display_name")),
                        tier=_label(record.get("user_tier")))


def encode_value(record: dict) -> str:
    """agy's keychain value for a record: STORE_PREFIX and the standard, padded base64 of its compact JSON (ASCII
    escapes, so it encodes whatever the record holds). json.loads gives back the same record."""
    data = json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    return STORE_PREFIX + base64.b64encode(data).decode("ascii")


def decode_value(raw: bytes) -> dict:
    """The record in agy's keychain value (what `security -w` gave back). ValueError unless it is exactly
    STORE_PREFIX, standard base64 and a JSON object (no duplicate keys, no NaN), within STORE_VALUE_MAX."""
    record = _decoded(raw)
    if record is None:
        # A fresh error with nothing behind it: the ones decoding raises keep the text they read (the secret).
        raise ValueError("not agy's value")
    return record


def _decoded(raw) -> dict | None:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > STORE_VALUE_MAX:
        return None
    try:
        text = bytes(raw).decode("ascii").strip()      # go-keyring trims the same way
        if not text.startswith(STORE_PREFIX):
            return None
        data = base64.b64decode(text[len(STORE_PREFIX):], validate=True)
        record = json.loads(data.decode("utf-8"), object_pairs_hook=_record_pairs, parse_constant=_record_constant)
    except (ValueError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def agy_command_length(service: str, account: str, value: str) -> int:
    """The length in bytes of the line agy's own write sends to `security -i` for this item and value."""
    line = f"add-generic-password -U -s {_shell_quote(service)} -a {_shell_quote(account)} -w {_shell_quote(value)}\n"
    return len(line.encode("utf-8"))


def _shell_quote(text: str) -> str:
    if not text:
        return "''"
    return text if _SHELL_SAFE.fullmatch(text) else "'" + text.replace("'", "'\"'\"'") + "'"


def _is_email(value) -> bool:
    return (isinstance(value, str) and len(value.strip()) <= 320 and _EMAIL.fullmatch(value.strip()) is not None
            and value.strip().isprintable())


def _label(value) -> str | None:
    """A plan or tier name from the record, cleaned for display; None when there is none."""
    if not isinstance(value, str):
        return None
    return _clean(value) or None


def _keychain_error(exc, what: str) -> DeckError:
    if getattr(exc, "reason", None) in ("locked", "timeout"):
        return DeckError(KEYCHAIN_BUSY.format(what=what), "busy")
    return DeckError(KEYCHAIN_FAILED.format(what=what), "antigravity")


def _record_pairs(pairs):
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key appears twice")      # the record could not be stored back whole
    return dict(pairs)


def _record_constant(_name):
    raise ValueError("not JSON")


def saved_record(email: str, record: dict, *, saved_at: float) -> dict:
    """The vault record of a saved sign-in: the store's record kept whole under `stores`."""
    return {"v": RECORD_VERSION, "email": email.strip().lower(), "saved_at": saved_at, "stores": {"cli": record}}


USAGE_MAX_CHARS = 256 << 10         # a usage answer is about 2 KB; anything this long is not one
USAGE_UNREADABLE = "AccountDeck could not read agy's answer."
_BUCKET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
# ASCII digits only ([0-9], not \d: that takes any script's digits).
_RFC3339 = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,9})?"
                      r"(?:([Zz])|([+-])([0-9]{2}):([0-9]{2}))")
GROUP_NAME = "Models"           # a group agy gave no name
OTHER_WINDOW = "other"          # a bucket agy gave no window


class _Unreadable(Exception):
    """Inside parse_usage: the answer is not what agy prints. `detail` is a fixed word, never a piece of the text."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def parse_usage(text: str, *, now: float) -> dict:
    """The normalised reading (contract._check_agy_reading) of what `agy -p "/usage" --output-format json` printed,
    taken at `now`. Accepted only when it is one JSON object with status "SUCCESS", command.name "usage" and
    num_turns 0; parsed strictly (types, 0 ≤ fraction ≤ 1, times that parse, at most contract.AGY_GROUPS_MAX groups of
    contract.AGY_BUCKETS_MAX buckets, every text cut to contract.AGY_TEXT_MAX characters); unknown fields are
    ignored and an unknown window is kept as text. A bucket needs only its id and a remaining fraction or amount: one
    without a name is named by its id, one without a window gets the window OTHER_WINDOW, and a group without a name
    (or one that is empty once cleaned) is called GROUP_NAME. DeckError (code "antigravity") otherwise; its message
    never quotes the output. Pure: no file, process or clock.

    agy's output is untrusted: it is read only up to USAGE_MAX_CHARS, a fraction must be a real number (not a bool,
    NaN or infinity), a bucket id short plain text unique in the reading, and every text loses its control and
    formatting characters. The DeckError's `detail` names what was wrong in one fixed word (for a log line)."""
    if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
        raise ValueError("now must be a time")
    try:
        return _parse_usage(text, float(now))
    except _Unreadable as problem:
        detail = problem.detail
    # Raised here, outside the handler, so that nothing hangs behind it: a JSON error keeps the whole text it read.
    error = DeckError(USAGE_UNREADABLE, "antigravity")
    error.detail = detail
    raise error


def _parse_usage(text, now: float) -> dict:
    if not isinstance(text, str):
        raise _Unreadable("not_text")
    if len(text) > USAGE_MAX_CHARS:
        raise _Unreadable("too_large")
    try:
        answer = json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except (ValueError, RecursionError):          # JSONDecodeError is a ValueError; so is a 5000-digit number
        raise _Unreadable("not_json") from None
    if not isinstance(answer, dict):
        raise _Unreadable("not_object")
    if answer.get("status") != "SUCCESS":
        raise _Unreadable("status")
    turns = answer.get("num_turns")
    if not _is_int(turns) or turns != 0:
        raise _Unreadable("turns")
    command = answer.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage" or not isinstance(command.get("data"), dict):
        raise _Unreadable("command")
    groups = command["data"].get("groups")
    if not isinstance(groups, list):
        raise _Unreadable("shape")
    if len(groups) > AGY_GROUPS_MAX:
        raise _Unreadable("too_many")
    seen, normalised = set(), []
    for group in groups:
        if not isinstance(group, dict):
            raise _Unreadable("shape")
        buckets = group.get("buckets")
        buckets = [] if buckets is None else buckets
        if not isinstance(buckets, list):
            raise _Unreadable("shape")
        if len(buckets) > AGY_BUCKETS_MAX:
            raise _Unreadable("too_many")
        normalised.append({"name": _text(group.get("name"), required=False) or GROUP_NAME,
                           "description": _text(group.get("description"), required=False),
                           "buckets": [_bucket(bucket, seen) for bucket in buckets]})
    return {"at": now, "groups": normalised}


def _bucket(bucket, seen: set) -> dict:
    if not isinstance(bucket, dict):
        raise _Unreadable("shape")
    ident = bucket.get("id")
    if not isinstance(ident, str) or not _BUCKET_ID.fullmatch(ident):
        raise _Unreadable("id")
    if ident in seen:
        raise _Unreadable("duplicate_id")
    seen.add(ident)
    disabled = bucket.get("disabled", False)
    if disabled is None:
        disabled = False
    if not isinstance(disabled, bool):
        raise _Unreadable("type")
    remaining = _number(bucket.get("remaining_fraction"), high=1.0)
    amount = _number(bucket.get("remaining_amount"))
    if remaining is None and amount is None:
        raise _Unreadable("no_value")         # a limit that says nothing about what is left is not one
    # Only the id and one of the two values are required: a missing name or window does not cost the whole reading.
    return {"id": ident, "name": _text(bucket.get("name"), required=False) or ident,
            "window": _text(bucket.get("window"), required=False) or OTHER_WINDOW,
            "remaining": remaining, "amount": amount, "resets_at": _time(bucket.get("reset_time")),
            "disabled": disabled}


def _text(value, *, required: bool = True) -> str | None:
    """A text of agy's, cleaned for display: control and formatting characters become spaces, runs of white space
    one space, cut to AGY_TEXT_MAX characters. A missing or empty optional text is None."""
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise _Unreadable("type")
    cleaned = _clean(value)
    if not cleaned:
        if required:
            raise _Unreadable("type")
        return None
    return cleaned


def _clean(value: str) -> str:
    """Text from agy for display: control, formatting and separator characters become spaces, runs of white space
    one space, cut to AGY_TEXT_MAX characters."""
    spaced = "".join(" " if unicodedata.category(c)[0] in "CZ" else c for c in value[:AGY_TEXT_MAX * 4])
    return " ".join(spaced.split())[:AGY_TEXT_MAX].rstrip()


def _number(value, *, high: float | None = None):
    """A remaining fraction (0..1) or amount (≥ 0): a finite JSON number, never a bool. None when it is not given."""
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise _Unreadable("type")
    if value < 0 or (high is not None and value > high):
        raise _Unreadable("range")
    return float(value) if high is not None or isinstance(value, float) else value


def _time(value) -> float | None:
    """An RFC 3339 time (agy prints UTC, `…Z`) as Unix seconds. None when it is not given, empty, or Go's zero time
    (any time before 1970 means "not set")."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _Unreadable("type")
    match = _RFC3339.fullmatch(value)
    if match is None:
        raise _Unreadable("time")
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    fraction, sign = match.group(7), match.group(9)
    try:
        offset = dt.timedelta(0)
        if sign:
            hours, minutes = int(match.group(10)), int(match.group(11))
            if hours > 23 or minutes > 59:
                raise ValueError("offset")
            offset = dt.timedelta(hours=hours, minutes=minutes) * (1 if sign == "+" else -1)
        when = dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.timezone(offset)).timestamp()
    except (ValueError, OverflowError):
        raise _Unreadable("time") from None
    when += float(fraction) if fraction else 0.0
    return when if when >= 0 else None


def _unique_keys(pairs):
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        raise _Unreadable("duplicate_key")
    return dict(pairs)


def _no_constant(_name):
    raise _Unreadable("not_json")        # NaN, Infinity and -Infinity are not JSON


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ---- what a usage run came to (agy_accounts.AgyService.read_usage) ----

RUN_OUTCOMES = ("ok", "signed_out", "reauth", "timed_out", "not_started", "unreadable")
RUN_ERRORS = {                 # an outcome's short error for the account's row: (message, DeckError code)
    "signed_out": ("Antigravity is not signed in.", "login_needed"),
    "reauth": ("Google no longer accepts this account's Antigravity sign-in. Sign in again.", "reauth_required"),
    "timed_out": ("agy did not answer in time.", "timeout"),
    "not_started": ("agy could not be started. Check that it is installed.", "not_installed"),
    "unreadable": (USAGE_UNREADABLE, "antigravity"),
}
# The one sign accepted that Google refused the account's sign-in itself: OAuth's error code for a refresh token that
# is expired, revoked or otherwise no longer valid (RFC 6749 §5.2), which Google's token endpoint answers and Go's
# oauth2 package carries into its error text word for word. Nothing else is taken as one (a 401, "UNAUTHENTICATED",
# "please sign in"): those have other causes too, and a wrong "sign in again" asks the user to redo a sign-in that
# still works. Without this sign a failed run is "could not read agy's answer".
_REFUSED_GRANT = re.compile(r"(?<![A-Za-z0-9_])invalid_grant(?![A-Za-z0-9_])")
_SCANNED = 1 << 20


def run_outcome(run, *, after: StoreRead | None = None) -> str:
    """What one usage run (a procs.AgyRun) came to, one of RUN_OUTCOMES, without guessing: "timed_out" (stopped at
    the time limit), "not_started" (agy missing or not runnable), "ok" (exit 0 and an answer parse_usage takes),
    "reauth" (a failed run naming OAuth's refused grant, `_REFUSED_GRANT`), "signed_out" (a failed run after which
    `after`, the store read once the run ended, holds no sign-in), else "unreadable". Pure."""
    return _outcome(run, after, answered=_answered(run))


def reading_from_run(run, *, now: float, after: StoreRead | None = None) -> dict:
    """The normalised reading (parse_usage) of a usage run that answered, else DeckError with its outcome's short
    error (RUN_ERRORS: message and code), `outcome` set, and `detail` set to parse_usage's word when agy exited 0
    with an answer that did not parse. Pure."""
    detail = None
    if not run.timed_out and run.status == 0:
        try:
            return parse_usage(run.output, now=now)
        except DeckError as problem:
            detail = getattr(problem, "detail", None)
    outcome = _outcome(run, after, answered=False)
    message, code = RUN_ERRORS[outcome]
    error = DeckError(message, code)
    error.outcome, error.detail = outcome, detail
    raise error


def _answered(run) -> bool:
    if run.timed_out or run.status != 0:
        return False
    try:
        parse_usage(run.output, now=0.0)
    except DeckError:
        return False
    return True


def _outcome(run, after, *, answered: bool) -> str:
    if run.timed_out:
        return "timed_out"
    if run.status is None:
        return "not_started"
    if answered:
        return "ok"
    if any(isinstance(text, str) and _REFUSED_GRANT.search(text[:_SCANNED]) for text in (run.errors, run.output)):
        return "reauth"
    if after is not None and after.state == "absent":
        return "signed_out"
    return "unreadable"
