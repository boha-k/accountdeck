"""Where Claude Code keeps its terminal login: the `CredStore` seam (docs/mac/PLAN.md D6, §1.7, §1.12).

`KeychainCredStore` (the live Mac, and tests that name a test keychain) is Claude Code's login-keychain item,
with `~/.claude/.credentials.json` as Claude Code's own plaintext fallback. `FileCredStore` is that file
alone: the base class, and the store of every synthetic home that names no test keychain.

Facts from Claude Code 2.1.293's program code (the native build's embedded JS; quoted strings are
verbatim). That build is the Windows one, so code under a macOS-only branch is compiled out of it; the
one fact that rests on such a branch is marked [inferred].
- On macOS the store is "keychain-with-plaintext-fallback" ("the macOS Keychain or the credentials
  file"). A read takes the keychain item and only when there is none the file. A write goes to the
  keychain; when the keychain held no item before, the file is then deleted; a write that falls back to
  the file deletes the keychain item ("plaintext_fallback_used", "plaintext_fallback_primary_not_cleared").
  Locked/transient primary failures skip fallback ("keychain_locked_skip_fallback",
  "primary_transient_skip_fallback"). Failure to clear the primary after a fallback write is itself
  a failure. The Windows binary compiles out the macOS credentials writer; its device-key writer's
  4032-character stdin/argv split is documented in keychain.py, not assumed to be this writer.
- Service: cQ("-credentials") = `Claude Code${OAUTH_FILE_SUFFIX}-credentials${c}`. OAUTH_FILE_SUFFIX is
  "" in the production OAuth config ("-local-oauth" and "-custom-oauth" are development setups). `c` is
  "" unless CLAUDE_CONFIG_DIR (or CLAUDE_SECURESTORAGE_CONFIG_DIR) is set, and then
  `-${sha256(dir.normalize("NFC")).hex.substring(0,8)}` of the variable's exact text.
- Account: TH(). The Windows build reduces it to `return"claude-code-user"`, but its module still
  imports `userInfo` from "os" and defines /^[a-zA-Z0-9._-]+$/ next to it: the macOS branch uses the
  login user name when it passes that check, else "claude-code-user" [inferred]. AccountDeck looks under
  both names and fails closed when both hold an item.
- Value: the JSON text of the whole credentials object, the same object as the file. Claude Code reads
  it with `security find-generic-password -a <account> -w -s <service>` and JSON.parse's the output.
- Locks: every credential write runs `wfo`, a proper-lockfile lock on `<config>/.storage-write`
  (lock directory `.storage-write.lock`, stale after 15 s) around a read-modify-write. A token refresh
  first holds `<config>/.oauth_refresh.lock` and the legacy `~/.claude.lock` (stale after 60 s) and
  saves only if the stored refresh token is still the one it renewed. See cli_locks.

So KeychainCredStore reads exactly what Claude Code reads, never creates a keychain item (it updates
the one Claude Code made, under the account found there) and, when the keychain holds no item, writes
the file, which is then what Claude Code reads; Claude Code moves it into the keychain on its own next
write. A keychain that cannot be read fails closed: the file is not consulted then, because it may be
stale. Nothing here logs or raises a value.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata

from . import keychain
from ..core import DeckError, digest, json_bytes, read_json

SERVICE = "Claude Code-credentials"
FALLBACK_ACCOUNT = "claude-code-user"
CREDENTIALS_FILE = ".credentials.json"
_ACCOUNT = re.compile(r"[a-zA-Z0-9._-]+")

AMBIGUOUS = ("Claude Code's keychain holds two terminal logins, so AccountDeck can't tell which one it "
             "uses. Nothing was changed.")
TOO_LARGE = "Claude Code's login is too large for AccountDeck to write to the keychain. Nothing was changed."
UNAVAILABLE = ("Claude Code's login in the keychain could not be read. Unlock the keychain, or allow "
               "AccountDeck when macOS asks, and try again. Nothing was changed.")


def object_file(path, *, missing=False):
    """A JSON object file of Claude Code's (credentials or `.claude.json`)."""
    try:
        value = read_json(path)
    except FileNotFoundError:
        if missing:
            return {}
        raise DeckError("The terminal login file is missing.", "login_needed") from None
    except (OSError, ValueError):
        raise DeckError("Claude Code's login or settings could not be read. Nothing was overwritten.", "identity") from None
    if not isinstance(value, dict):
        raise DeckError("Claude Code's login or settings is not a JSON object.", "identity")
    return value


def service_name(config_dir=None) -> str:
    """Claude Code's credentials service for its default config folder (None), or for a CLAUDE_CONFIG_DIR
    whose exact text is `config_dir`."""
    if config_dir is None:
        return SERVICE
    text = unicodedata.normalize("NFC", os.fspath(config_dir))
    return f"{SERVICE}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"


def account_candidates() -> tuple[str, ...]:
    """The keychain accounts Claude Code may file its login under: this user's login name when it passes
    Claude Code's check, then "claude-code-user". Read from the user database, never the environment."""
    names = []
    try:
        import pwd
        name = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, AttributeError, KeyError, OSError):
        name = None
    if name and _ACCOUNT.fullmatch(name):
        names.append(name)
    if FALLBACK_ACCOUNT not in names:
        names.append(FALLBACK_ACCOUNT)
    return tuple(names)


def encode(value: dict) -> str:
    """The keychain text of a credentials object: compact JSON, ASCII only, so `security -w` prints it as is."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def decode(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError:
        raise DeckError("Claude Code's login or settings could not be read. Nothing was overwritten.", "identity") from None
    if not isinstance(value, dict):
        raise DeckError("Claude Code's login or settings is not a JSON object.", "identity")
    return value


def for_paths(paths, *, keychain=None, accounts=None):
    """The store for `paths`: the login keychain when live; a synthetic home uses the keychain only when a
    test names its keychain file, else the credentials file."""
    if paths.mode == "live" or keychain is not None:
        return KeychainCredStore(paths, keychain=keychain, accounts=accounts)
    return FileCredStore(paths)


class FileCredStore:
    """`.credentials.json` in Claude Code's config folder."""

    kind = "file"
    storage_lock = False      # cli_locks.credential_locks(storage_write=…): see the module docstring

    def __init__(self, paths):
        self.paths = paths

    # ---- the live login (callers hold cli_locks.credential_locks for anything they write) ----
    def read(self, *, missing=False) -> dict:
        """The live credentials object. Without a stored login: {} with `missing`, else
        DeckError("login_needed"); unreadable: DeckError("identity")."""
        return object_file(self.paths.claude_cli_credentials, missing=missing)

    def write(self, tx, value: dict) -> None:
        """Writes `value` as the live login, with its before-state in `tx` (a CLITransaction) first."""
        tx.write(self.paths.claude_cli_credentials, json_bytes(value))

    def rollback(self, tx, restore=None) -> bool:
        """Restores what `write` changed outside files (FileTransaction restores files); True on a conflict.
        `restore` maps a before-value (a credentials object) to the one to put back instead."""
        return False

    # ---- an enrollment's isolated config folder (CLAUDE_CONFIG_DIR=<folder>) ----
    def read_enrollment(self, folder) -> dict:
        return object_file(Path(folder) / CREDENTIALS_FILE)

    def enrollment_pending(self, folder) -> bool:
        """True while the folder's store may still hold a login that was not saved."""
        return (Path(folder) / CREDENTIALS_FILE).exists()

    def discard_enrollment(self, folder) -> None:
        """Removes the enrollment's stored login outside its folder (the folder itself is removed by the caller)."""


class KeychainCredStore(FileCredStore):
    """Claude Code's login-keychain item, with the credentials file as its plaintext fallback."""

    kind = "keychain"
    storage_lock = True

    def __init__(self, paths, *, keychain=None, accounts=None):
        super().__init__(paths)
        self.keychain = keychain                      # None: the user's default (login) keychain
        self.accounts = tuple(accounts) if accounts else account_candidates()

    def _find(self, services) -> tuple[str, str, str] | None:
        """(service, account, text) of the one item Claude Code would read, or None when there is none."""
        found = []
        for service in services:
            for account in self.accounts:
                try:
                    text = keychain.get(service, account, keychain=self.keychain)
                except keychain.KeychainError as exc:
                    raise _unavailable(exc) from None
                if text is not None:
                    found.append((service, account, text))
        if len(found) > 1:
            raise DeckError(AMBIGUOUS, "identity")
        return found[0] if found else None

    def read(self, *, missing=False) -> dict:
        item = self._find((SERVICE,))
        if item is None:
            return super().read(missing=missing)
        return decode(item[2])

    def write(self, tx, value: dict) -> None:
        item = self._find((SERVICE,))
        if item is None:
            super().write(tx, value)          # Claude Code reads the file while the keychain holds no item
            return
        service, account, before = item
        text = encode(value)
        if len(text) > keychain.MAX_VALUE:
            raise DeckError(TOO_LARGE, "unsupported")
        tx.doc.setdefault("keychain_changes", []).append(
            {"service": service, "account": account, "before": before, "after": digest(text.encode("ascii"))})
        tx.save()                             # the encrypted before-value exists before the item changes
        try:
            keychain.set(service, account, text, keychain=self.keychain, replace=True)
        except keychain.KeychainError as exc:
            raise _unavailable(exc) from None

    def rollback(self, tx, restore=None) -> bool:
        """Puts each changed item back while it still holds what AccountDeck wrote; an item Claude Code
        changed since (a refresh, a new sign-in) is kept, and that is a conflict. What goes back is the
        before-value, or `restore`'s replacement for its credentials object (CLITransaction.restore_login:
        the account's newer login, never a spent one); a replacement's digest is recorded first, so a
        rollback run again counts that value as put back."""
        conflict = False
        for change in reversed(tx.doc.get("keychain_changes", [])):
            service, account = change["service"], change["account"]
            try:
                current = keychain.get(service, account, keychain=self.keychain)
                if current == change["before"] or (current is not None and change.get("restored")
                                                   == digest(current.encode("utf-8"))):
                    continue
                if current is None or digest(current.encode("utf-8")) != change["after"]:
                    conflict = True
                    continue
                if change["before"] is None:
                    keychain.delete(service, account, keychain=self.keychain)
                    continue
                text = _put_back(change["before"], restore)
                if text != change["before"]:
                    if len(text) > keychain.MAX_VALUE:
                        raise DeckError(TOO_LARGE, "unsupported")
                    change["restored"] = digest(text.encode("ascii"))
                    tx.save()
                keychain.set(service, account, text, keychain=self.keychain, replace=True)
            except keychain.KeychainError as exc:
                raise _unavailable(exc) from None
        return conflict

    def _enrollment_services(self, folder) -> tuple[str, ...]:
        """The folder's service under the text the enrollment terminal was given, and under the resolved
        path should the launcher have resolved it (/tmp and /var are links on macOS)."""
        given = service_name(os.fspath(folder))
        resolved = service_name(os.fspath(Path(folder).resolve()))
        return (given,) if given == resolved else (given, resolved)

    def read_enrollment(self, folder) -> dict:
        item = self._find(self._enrollment_services(folder))
        if item is None:
            return super().read_enrollment(folder)
        return decode(item[2])

    def enrollment_pending(self, folder) -> bool:
        try:
            if self._find(self._enrollment_services(folder)) is not None:
                return True
        except DeckError:
            return True                       # cannot tell: keep the folder and the item for a retry
        return super().enrollment_pending(folder)

    def discard_enrollment(self, folder) -> None:
        for service in self._enrollment_services(folder):
            for account in self.accounts:
                try:
                    keychain.delete(service, account, keychain=self.keychain)
                except keychain.KeychainError as exc:
                    raise _unavailable(exc) from None


def _put_back(before: str, restore) -> str:
    """The keychain text a rollback writes for the before-value `before`: itself, or the encoded value
    `restore` gives for its credentials object when that is another one."""
    if restore is None:
        return before
    try:
        value = decode(before)
    except DeckError:
        return before
    replaced = restore(value)
    return before if replaced is value else encode(replaced)


def _unavailable(exc) -> DeckError:
    reason = getattr(exc, "reason", "failed")
    if reason == "unsupported":
        return DeckError(str(exc), "unsupported")
    return DeckError(UNAVAILABLE, "busy" if reason in ("locked", "timeout") else "vault")
