"""Importing the PC's accounts on the Mac: the receiving half of the move (docs/mac/PLAN.md §2.3-§2.6).

Actions (engine `_do_*`, contract.ACTIONS):
- `import-bundle-preview` {path, passphrase}: opens the bundle and says, per account, what an import would
  do. Nothing is written.
- `import-bundle` {path, passphrase, wait?}: ImportBundle, one step per account.
  Applying a bundle again is safe: every account is an identity-keyed upsert where the newer sign-in wins.
- `import-bundle-dismiss` {bundle_id}: hides that import's confirmation code or failure list.

What each account becomes:
- Claude Code and Codex: a vault record in the shape cli_accounts.CLIAccounts.save and AccountService.save
  write, with the same row flags, so the Mac's switch flows use it unchanged.
- Claude Desktop: converted at once with claude_portable.from_portable when this Mac's Claude Desktop key
  can be read and Claude Desktop has created its cookie database (the template). Otherwise the portable
  profile is kept, encrypted by the vault cipher, in the record `deferred-desktop-<account id>` (its
  plaintext values under the "portable" key), and the row carries flags["deferred_desktop"]: "waiting"
  (no usable profile yet: never read, not switchable) or "newer" (a usable older profile stays in use
  meanwhile). The engine tick's `_convert_deferred` (convert_deferred here) finishes them later.
- An account `live_on_pc` (still signed in on the PC): a row with owner ON_PC ("on_pc") and its history,
  without any secret. Such a row is never read, refreshed or switched: Engine.targets skips it
  (readable), check_action refuses account actions naming it, and its flags keep it out of switching and
  terminal auto-switch. A hand-over bundle fills it: the row gets its sign-in and becomes "v2".

The confirmation code (§2.4) is move_bundle.code_for_imported over the accounts this run stored, so it
exists only when every account was stored (a deferred Desktop account and an "on_pc" row count). It is kept
in the encrypted ledger record `move-import` and shown in the state until dismissed; it never goes into
logs, events or error messages. Neither does any token, cookie or the passphrase.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import weakref

from . import config, move_bundle as mb
from .config import SafetyViolation
from .core import ON_PC_MESSAGE, DeckError, is_frozen
from .diagnostics import account_ref, log
from .operations import Operation
from .procs import is_test_owned_path
from .providers import codex as codex_module
from .providers.chromium_crypto import ChromiumKeyNotReady
from .providers.claude_cli import expires as cli_expires, fingerprint as cli_fingerprint
from .providers.claude_portable import PORTABLE_FORMAT, PortableError, cookie_template, from_portable, meta_version

ON_PC = "on_pc"
IMPORTED_FROM = "pc"
IMPORT_KIND, PREVIEW_KIND, DISMISS_KIND = "import-bundle", "import-bundle-preview", "import-bundle-dismiss"
LEDGER_KEY = "move-import"
DEFERRED_PREFIX = "deferred-desktop-"
GENERATION_SOURCE = "import-pc"
MIN_SCRYPT_N = mb.SCRYPT_MIN_N           # tests lower it together with the scrypt cost of the bundles they seal
IMPORT_WAIT = 30.0                       # how long `import-bundle` waits for its operation before answering
LEDGER_KEEP = 10
SNAPSHOT_DAYS, READING_DAYS, REFILL_DAYS = 90, 7, 30   # what core.Store keeps
_DAY = 86400
# Deferred conversion back-off (seconds). A missing key or template is cheap to look for again (no dialog);
# a refused or locked Keychain read may have shown macOS's access dialog, so each such wait doubles.
CHECK_EVERY = 60.0
REFUSED_FIRST, REFUSED_MAX = 600.0, 6 * 3600.0
FAILED_RETRY = 6 * 3600.0
LEDGER_RETRY = 60.0

CANCEL_HINT = "Use Cancel move on the PC and export again."
CODE_HINT = "Type this code on the PC to finish the move there."
WAITING_REFUSED = "Open Claude Desktop once on this Mac to finish importing this account."
REASONS = {
    "identity": "Its sign-in in the bundle belongs to another account.",
    "damaged": "Its sign-in in the bundle is damaged.",
    "unsupported": "Its sign-in comes from an AccountDeck version this one cannot read.",
    "conflict": "Another account saved here holds this sign-in.",
    "not_waiting": "It is not waiting for a hand-over on this Mac.",
    "unsaved": "It could not be saved here.",
    "history": "Its sign-in was saved, but its usage history could not be copied.",
}
ON_PC_ALLOWED = frozenset({"rename", "remove"})       # the only actions that may name an "on_pc" account
OUTCOMES = ("new", "updated", "filled", "kept", "on_pc", "deferred", "failed")
_NOTES = {"new": "Brought over.", "updated": "Already here; it now has the newer sign-in from the PC.",
          "filled": "Its sign-in from the PC is now saved here.", "kept": "Already here; the newer sign-in here is kept.",
          "on_pc": "Still signed in on the PC: listed here with its history, without its sign-in.",
          "deferred": "Saved. It finishes importing once Claude Desktop has been opened on this Mac."}
_NO_HISTORY = " Its usage history could not be copied."


# ---- the bundle file ----

def open_file(path, passphrase, *, paths, expect_kind=None):
    """(header, payload) of the bundle at `path`, or DeckError with a fixed, secret-free message. In safe
    mode only a file under a registered test root is read."""
    if not isinstance(path, str) or not path.strip():
        raise DeckError("Choose the move bundle file.", "invalid")
    if not isinstance(passphrase, str) or not passphrase:
        raise DeckError("Enter the bundle passphrase.", "invalid")
    target = paths.user_path(path.strip())
    if config.safe_mode() and not is_test_owned_path(target):
        raise SafetyViolation("Safe mode: AccountDeck 2 reads only a synthetic move bundle.")
    failure = None
    try:
        data = mb.read_bundle(target)
        return mb.open_bundle(data, passphrase, expect_kind=expect_kind, min_scrypt_n=MIN_SCRYPT_N)
    except mb.BundleError as exc:
        failure = DeckError(str(exc), exc.code)
        failure.reason = exc.reason
    except FileNotFoundError:
        failure = DeckError("The move bundle file was not found.", "not_found")
    except OSError:
        failure = DeckError("The move bundle file could not be read.", "not_found")
    raise failure       # outside the except blocks, so it carries no chained traceback


# ---- planning (shared by the preview and the import) ----

def _plan(engine, header, payload, ledger_entry):
    """[(account, row here or None, action, reason)] in bundle order. action: "new", "update", "on_pc"
    (listed without a sign-in), "fill" (a waiting "on_pc" row gets its sign-in) or "refused" (a hand-over
    account no waiting row matches)."""
    done = set((ledger_entry or {}).get("accounts") or ())
    started = (ledger_entry or {}).get("started") or {}
    plan = []
    for account in payload["accounts"]:
        row = engine.store.find(account["provider"], account["identity"])
        if header.kind == "handover":
            if row is not None and row["owner"] == ON_PC:
                plan.append((account, row, "fill", None))
            elif row is not None and (account["id"] in done or started.get(account["id"]) == row["id"]):
                # This same hand-over, including a crash after saving a row but before finish.
                plan.append((account, row, "update", None))
            else:
                plan.append((account, row, "refused", REASONS["not_waiting"]))
        elif account["live_on_pc"]:
            plan.append((account, row, "on_pc" if row is None or row["owner"] == ON_PC else "update", None))
        elif row is None:
            plan.append((account, row, "new", None))
        else:
            plan.append((account, row, "fill" if row["owner"] == ON_PC else "update", None))
    return plan


def _template_ready(engine):
    """Whether Claude Desktop on this computer has created the cookie database a conversion needs. Reads no
    Keychain item."""
    try:
        return _template_path(engine.providers["claude"]).is_file()
    except (OSError, DeckError):
        return False


def preview(engine, path, passphrase):
    """The `import-bundle-preview` payload (see the module docstring). Writes nothing."""
    header, payload = open_file(path, passphrase, paths=engine.paths)
    plan = _plan(engine, header, payload, _ledger(engine).entry(header.bundle_id))
    template = _template_ready(engine)
    accounts, counts = [], {"new": 0, "update": 0, "on_pc": 0, "fill": 0, "refused": 0, "deferred": 0}
    for account, row, action, reason in plan:
        counts[action] += 1
        desktop = None
        if account["provider"] == "claude" and "secret" in account and action != "refused":
            desktop = "now" if template else "deferred"
            counts["deferred"] += desktop == "deferred"
        accounts.append({"id": account["id"], "provider": account["provider"], "label": _label(account),
                         "plan": account["plan"], "action": action, "desktop": desktop,
                         "conflict": row["label"] if row is not None and action == "update" else None,
                         "reason": reason})
    refused = counts["refused"] > 0
    log("move.import_preview", kind=header.kind, accounts=len(accounts), refused=counts["refused"])
    return {"bundle_id": header.bundle_id, "kind": header.kind, "created": header.created,
            "source_platform": payload["source_platform"], "accounts": accounts, "counts": counts,
            "refused": refused, "message": _preview_message(header.kind, counts, refused)}


def _preview_message(kind, counts, refused):
    if refused:
        return (f"{_count(counts['refused'], 'account')} in this hand-over bundle {_verb(counts['refused'], 'is')} "
                "not waiting on this Mac. Import the main move bundle first; nothing will be imported.")
    parts = []
    if kind == "handover":
        total = counts["fill"] + counts["update"]
        parts.append(f"{_count(total, 'account')} will get {'its' if total == 1 else 'their'} sign-in from the PC.")
    else:
        parts.append(f"{_count(counts['new'] + counts['update'] + counts['fill'], 'account')} will move to this Mac.")
        if counts["on_pc"]:
            parts.append(f"{_count(counts['on_pc'], 'account')} still signed in on the PC will be listed here "
                         "without a sign-in.")
    if counts["deferred"]:
        parts.append(f"Claude Desktop is not set up on this Mac yet, so {_count(counts['deferred'], 'Claude Desktop account')} "
                     "will finish importing once you open it here.")
    return " ".join(parts)


# ---- the import ----

class ImportBundle(Operation):
    """`import-bundle`: one step per account, then "finish". The bundle is opened (and the passphrase let
    go) when this is built, so a wrong passphrase or a refused hand-over reaches the action that asked;
    the decrypted payload lives in memory only until run() ends. `result` is the public result
    (_entry_public) once run() finished."""

    def __init__(self, engine, path, passphrase):
        header, payload = open_file(path, passphrase, paths=engine.paths)
        plan = _plan(engine, header, payload, _ledger(engine).entry(header.bundle_id))
        refused = sum(1 for _account, _row, action, _reason in plan if action == "refused")
        if refused:
            raise DeckError(f"{_count(refused, 'account')} in this hand-over bundle {_verb(refused, 'is')} not waiting "
                            "on this Mac. Import the main move bundle first. Nothing was imported.", "invalid")
        steps = [(f"account-{number}", f"Bringing over {_label(account)}")
                 for number, (account, _row, _action, _reason) in enumerate(plan, 1)]
        super().__init__(IMPORT_KIND, steps=steps + [("finish", "Finishing the move")])
        self.cancellable = False     # accounts are saved one by one; a cancel could not say nothing changed
        self.engine, self.header, self._plan = engine, header, plan
        self._settings = config.validate_settings(payload["settings"])
        self.result = None

    def run(self):
        began = time.monotonic()
        engine, header = self.engine, self.header
        stored, failures, mapping = set(), [], {}
        counts = dict.fromkeys(OUTCOMES, 0)
        code = None
        try:
            self.set_phase("saving", "Bringing over your accounts from the PC…")
            importer = _Importer(engine, self.clock)
            for number, (account, _row, _action, _reason) in enumerate(self._plan, 1):
                with self.step(f"account-{number}"):
                    if header.kind == "handover":
                        _ledger(engine).begin_account(header, account["id"], _row["id"], self.clock())
                    outcome, key, reason, history = importer.bring_over(account)
                    counts[outcome] += 1
                    if outcome == "failed":
                        failures.append({"label": _label(account), "provider": account["provider"], "reason": reason})
                        self.note(f"Not imported. {reason}", status="failed")
                        continue
                    stored.add(account["id"])
                    mapping[account["id"]] = key
                    self.note(_NOTES[outcome] + ("" if history else _NO_HISTORY),
                              status=None if history else "warning")
            with self.step("finish"):
                if not failures:
                    settings = copy.deepcopy(self._settings)
                    if "cli_manual_order" in settings:
                        settings["cli_manual_order"] = [mapping[key] for key in settings["cli_manual_order"]
                                                        if key in mapping]
                    # Persist (one transaction) before updating the preference cache, so a failed
                    # write cannot look like an unchanged setting on the retry.
                    engine.store.set_settings(settings)
                    engine.update_settings(settings)
                code = None if failures else mb.code_for_imported(header, header.keys, stored)
                entry = _ledger(engine).record(header, at=self.clock(), code=code, counts=counts, failures=failures,
                                              mapping=mapping)
                self.result = _entry_public(entry)
                ready = [key for key in mapping.values() if readable(engine.store.account(key))]
                engine.scheduler.request_all(ready, self.clock())
        finally:
            self._plan = None        # drops the decrypted payload
            self._settings = None
        self.report["summary"] = _summary(header.kind, counts, failures, code)
        log("move.import_done", kind=header.kind, stored=len(stored), failed=len(failures),
            deferred=counts["deferred"], on_pc=counts["on_pc"], complete=code is not None,
            ms=round((time.monotonic() - began) * 1000))
        if failures:     # the operation ends as failed; `result` (and the ledger) still list what was stored
            raise DeckError(self.report["summary"], "invalid")


class _Refused(Exception):
    """One account cannot be stored; `reason` is a REASONS key."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _Importer:
    """Stores one bundle account: bring_over returns (outcome, account id here, failure reason, whether
    its history was copied). Whatever goes wrong with one account leaves the others unaffected."""

    def __init__(self, engine, clock):
        self.engine, self.store, self.vault, self.clock = engine, engine.store, engine.vault, clock
        self.created = None

    def bring_over(self, account):
        self.created = None
        try:
            outcome, key = self._save(account)
        except SafetyViolation:
            raise
        except _Refused as exc:
            log("move.import_account", "warning", provider=account["provider"], result="failed", reason=exc.reason)
            self._forget()
            return "failed", None, REASONS[exc.reason], True
        except Exception as exc:
            log("move.import_account", "warning", provider=account["provider"], result="failed",
                error_type=type(exc).__name__, code=getattr(exc, "code", None))
            self._forget()
            return "failed", None, REASONS["unsaved"], True
        history = True
        try:
            _copy_history(self.store, key, account, self.clock())
        except Exception as exc:
            history = False
            log("move.import_history", "warning", acct=account_ref(key), error_type=type(exc).__name__)
        log("move.import_account", acct=account_ref(key), provider=account["provider"], result=outcome)
        self.engine.bump()
        if not history:
            return "failed", key, REASONS["history"], False
        return outcome, key, None, history

    def _forget(self):
        """A row this import created for an account that then failed goes again, with anything stored."""
        key, self.created = self.created, None
        if key is None:
            return
        try:
            self.vault.remove(key)
            self.vault.remove(DEFERRED_PREFIX + key)
            self.store.remove(key)
        except Exception as exc:
            log("move.import_cleanup", "warning", error_type=type(exc).__name__)

    def _save(self, account):
        provider = account["provider"]
        row = self.store.find(provider, account["identity"])
        if "secret" not in account:                       # still signed in on the PC: a listing only
            if row is None:
                key = self._create(account)
                flags = {"restorable": False, **({"excluded": True} if provider == "claude_cli" else {})}
                self.store.update(key, flags=flags)
                return "on_pc", key
            self._refresh_metadata(row, account)
            return ("on_pc" if row["owner"] == ON_PC else "kept"), row["id"]
        if row is None:
            # A new row stays "on_pc" (never read) until its sign-in is stored, so an interruption never
            # leaves a "v2" row without one.
            key = self._create(account)
            self.store.update(key, flags={"restorable": False})
            row = self.store.account(key)
        else:
            self._refresh_metadata(row, account)
            row = self.store.account(row["id"])
        waiting = row["owner"] == ON_PC
        with _account_lock(self.engine, row["id"]):
            if provider == "claude":
                outcome = self._desktop(row, account)
            elif provider == "claude_cli":
                outcome = self._terminal(row, account)
            else:
                outcome = self._codex(row, account)
            if waiting:
                self.store.update(row["id"], owner="v2")
        if self.created is not None:
            return ("deferred" if outcome == "deferred" else "new"), row["id"]
        if waiting and outcome != "deferred":
            outcome = "filled"
        return outcome, row["id"]

    def _create(self, account):
        key = self.store.save_account(account["provider"], account["identity"], _label(account), account["email"],
                                      account["plan"], account["org"], imported_from=IMPORTED_FROM, owner=ON_PC)
        self.created = key
        return key

    def _refresh_metadata(self, row, account):
        self.store.save_account(row["provider"], row["identity"], row["label"], account["email"] or row["email"] or "",
                                account["plan"] or row["plan"] or "", account["org"] or row["org"] or "")

    # ---- Claude Code: CLIAccounts.save's record and flags ----
    def _terminal(self, row, account):
        record = _terminal_record(account, row, self.clock())
        key, cli = row["id"], self.engine.cli
        with cli.mutex, cli.key_lock(key):
            previous = _vault_record(self.vault, key)
            if isinstance(previous, dict) and previous.get("identity") == row["identity"]:
                if previous.get("credentials") == record.get("credentials") and previous.get("health") == record["health"]:
                    return "kept"
                if self.engine.active_identity.get("claude_cli") == row["identity"]:
                    return "kept"            # the login the terminals use here is the one to keep
                healthier = record["health"] == "ok" and previous.get("health") != "ok"
                if not healthier and (_terminal_time(previous) >= _terminal_time(record) or record["health"] != "ok"):
                    return "kept"
                record["excluded"] = bool(previous.get("excluded", record["excluded"]))
            digest = self.vault.put(key, record)
            healthy = record["health"] == "ok"
            flags = dict(self.store.account(key)["flags"])
            flags.update(restorable=healthy, health=record["health"], excluded=record["excluded"], live_expired=False)
            self.store.update(key, flags=flags, credential_state="fresh" if healthy else "reauth_required", error=None,
                              error_code=None, expires_at=cli_expires(record["credentials"]) if healthy else None)
            self.store.record_generation(key, digest, source=GENERATION_SOURCE, health=record["health"])
        return "updated"

    # ---- Codex: AccountService.save's record (put_guarded for an account saved here already) ----
    def _codex(self, row, account):
        auth = account["secret"]["data"]
        identity = codex_module.identity_key(auth)
        if identity is None:
            raise _Refused("damaged")
        if identity != row["identity"]:
            raise _Refused("identity")
        key, auth = row["id"], copy.deepcopy(auth)
        previous = _vault_record(self.vault, key)
        if previous is not None:
            if previous == auth:
                return "kept"
            status = self.vault.put_guarded(key, auth, "import", codex_module.identity_key, codex_module.freshness)
            if status == "conflict":
                raise _Refused("conflict")
            if status == "kept":
                return "kept"
        else:
            digest = self.vault.put(key, auth)
            self.store.record_generation(key, digest, source=GENERATION_SOURCE)
        self.store.update(key, flags=dict(self.store.account(key)["flags"], restorable=True), error=None, error_code=None)
        return "updated"

    # ---- Claude Desktop: convert now, or keep the portable profile for later ----
    def _desktop(self, row, account):
        portable = account["secret"]["data"]
        _check_portable(portable, row["identity"])
        key = row["id"]
        incoming = _captured(portable["profile"])
        own = _usable_profile(_vault_record(self.vault, key))
        waiting = _vault_record(self.vault, DEFERRED_PREFIX + key)
        held = [_captured(own)] if own is not None else []
        if _is_deferred(waiting):
            held.append(_captured(waiting["portable"]["profile"]))
        if held and max(held) >= incoming:                  # the newer copy is here already
            return "kept" if own is not None and _captured(own) >= max(held) else "deferred"
        converted = _convert(self.engine, portable)
        if converted is not None:
            _store_converted(self.engine, self.store.account(key), converted, own)
            return "updated"
        self.vault.put(DEFERRED_PREFIX + key, {"version": 1, "identity": row["identity"], "captured": incoming,
                                               "portable": portable}, generation=False)
        flags = dict(self.store.account(key)["flags"], deferred_desktop="newer" if own is not None else "waiting")
        if own is None:
            flags["restorable"] = False
        self.store.update(key, flags=flags)
        return "deferred"


def _terminal_record(account, row, now):
    """The bundle's Claude Code record in the vault shape cli_accounts writes, or _Refused. The login's
    own facts travel (verified, refreshed, excluded); this machine's activity (refresh_attempt) does not."""
    record = account["secret"]["data"]
    if record.get("provider", "claude_cli") != "claude_cli" or not isinstance(record.get("identity"), str):
        raise _Refused("damaged")
    if record["identity"] != row["identity"]:
        raise _Refused("identity")
    oauth = record.get("oauthAccount")
    if oauth is not None and (not isinstance(oauth, dict) or oauth.get("accountUuid", row["identity"]) != row["identity"]):
        raise _Refused("identity")
    record = copy.deepcopy(record)
    record.pop("refresh_attempt", None)
    health = record.get("health", "ok")
    if health not in ("ok", "needs_signin"):
        raise _Refused("damaged")
    credentials = record.get("credentials")
    if credentials is None and health == "needs_signin":
        record["fingerprint"] = None
        record.pop("verified", None)
    else:
        try:
            if not isinstance(credentials, dict):
                raise TypeError("credentials")
            print_ = cli_fingerprint(credentials)
            cli_expires(credentials)
        except (DeckError, TypeError, ValueError, AttributeError):
            raise _Refused("damaged") from None
        if record.get("verified") != print_:
            record.pop("verified", None)
        record["fingerprint"] = print_
    record.update(provider="claude_cli", health=health, label=row["label"],
                  excluded=bool(record.get("excluded", False)))
    for name in ("email", "organizationUuid", "plan"):
        if not isinstance(record.get(name), str):
            record[name] = ""
    if not _is_number(record.get("captured")):
        record["captured"] = now
    if not _is_number(record.get("refreshed")):
        record["refreshed"] = record["captured"]
    return record


def _terminal_time(record):
    values = [record.get(name) for name in ("refreshed", "captured")]
    return max((value for value in values if _is_number(value)), default=0.0)


def _check_portable(portable, identity):
    if not isinstance(portable, dict):
        raise _Refused("damaged")
    if portable.get("format") != PORTABLE_FORMAT:
        raise _Refused("unsupported" if "format" in portable else "damaged")
    profile = portable.get("profile")
    if not isinstance(profile, dict) or not isinstance(portable.get("auth"), dict):
        raise _Refused("damaged")
    if profile.get("identity") != identity or portable["auth"].get("lastKnownAccountUuid", identity) != identity:
        raise _Refused("identity")


def _usable_profile(record):
    return record if isinstance(record, dict) and isinstance(record.get("auth"), dict) and record.get("key") else None


def _is_deferred(record):
    return (isinstance(record, dict) and isinstance(record.get("portable"), dict)
            and isinstance(record["portable"].get("profile"), dict))


# ---- Claude Desktop conversion (now, or later from the tick) ----

def _template_path(claude):
    path = Path(claude.cookie_dir) / claude.scope.cookie_files[0]
    if config.safe_mode() and not is_test_owned_path(path):
        raise SafetyViolation("Safe mode: AccountDeck 2 reads only a synthetic Claude Desktop folder.")
    return path


def _template(claude):
    """(cookie database bytes, meta version) that Claude Desktop made on this computer, or None while there
    is none or it cannot be read yet. Reads no Keychain item."""
    path = _template_path(claude)
    if not path.is_file():
        return None
    try:
        data = cookie_template(path)
        return data, meta_version(data)
    except (PortableError, sqlite3.Error, OSError, ValueError) as exc:
        log("move.template_unreadable", "warning", error_type=type(exc).__name__)
        return None


class _NotReady(Exception):
    """The key or the template is not available yet; `reason` says which (it picks the back-off)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _convert(engine, portable):
    """The converted profile, or None when it must wait (no template yet, key not ready, or a profile that
    cannot be converted now; the tick tries again later)."""
    runtime = _runtime(engine)
    if engine.clock() < runtime.next_at and runtime.reason not in (None, "template", "missing"):
        return None                  # a refused Keychain read is not asked again before its back-off ends
    try:
        return _convert_now(engine.providers["claude"], portable)
    except _NotReady as exc:
        runtime.not_ready(exc.reason, engine.clock())
        return None
    except PortableError as exc:
        log("move.convert_failed", "warning", code=exc.code)
        runtime.problem = str(exc)
        return None


def _convert_now(claude, portable):
    """from_portable with this computer's key and Claude Desktop's own cookie database: _NotReady while
    either is missing, PortableError when the profile cannot be converted."""
    found = _template(claude)
    if found is None:
        raise _NotReady("template")
    template, version = found
    crypto = claude.crypto
    try:
        crypto.key_ref()
        return from_portable(portable, crypto, version, template=template)
    except ChromiumKeyNotReady as exc:
        raise _NotReady(exc.reason) from None
    except PortableError:
        raise
    except DeckError as exc:            # e.g. a key this crypto cannot use yet
        raise _NotReady(exc.code) from None


def _store_converted(engine, row, profile, own):
    """Saves a converted profile as the account's current one unless `own` (the usable profile held) is
    newer, then clears the deferred state. Caller holds the account lock."""
    key, store, vault = row["id"], engine.store, engine.vault
    if own is None or _captured(profile) > _captured(own):
        try:
            health = engine.providers["claude"].health(profile)
        except DeckError:
            health = "unreadable"
        digest = vault.put(key, profile)
        store.record_generation(key, digest, source=GENERATION_SOURCE, health=health, at=_captured(profile) or None)
    flags = dict(store.account(key)["flags"])
    flags.pop("deferred_desktop", None)
    current = _usable_profile(_vault_record(vault, key))
    flags["restorable"] = bool(current is not None and current.get("cookies"))
    store.update(key, flags=flags, error=None, error_code=None)
    vault.remove(DEFERRED_PREFIX + key)


def convert_deferred(engine, now):
    """The tick phase (`Engine._convert_deferred`): converts deferred Claude Desktop profiles once this
    computer's key and Claude Desktop's cookie database are available. Its own back-off gates every
    attempt, and ChromiumCryptoMac also remembers a failed Keychain read, so a missing or refused key
    never turns into repeated access dialogs."""
    runtime = _runtime(engine)
    if now < runtime.next_at or engine.runner.busy or not engine.owns_login:
        return
    rows = [row for row in engine.store.accounts()
            if row["provider"] == "claude" and not is_frozen(row) and row["flags"].get("deferred_desktop")]
    if not rows:
        runtime.reset(now)
        return
    claude, converted = engine.providers["claude"], 0
    with runtime.lock:
        for row in rows:
            if now < runtime.row_retry.get(row["id"], 0):
                continue
            with _account_lock(engine, row["id"]):
                current = engine.store.account(row["id"])      # re-read under the lock
                if current is None or is_frozen(current) or not current["flags"].get("deferred_desktop"):
                    continue
                record = _vault_record(engine.vault, DEFERRED_PREFIX + current["id"])
                own = _usable_profile(_vault_record(engine.vault, current["id"]))
                if not _is_deferred(record):
                    _clear_lost(engine, current, own)
                    continue
                try:
                    profile = _convert_now(claude, record["portable"])
                except _NotReady as exc:
                    runtime.not_ready(exc.reason, now)
                    log("move.convert_waiting", reason=exc.reason)
                    break
                except PortableError as exc:
                    runtime.row_retry[row["id"]] = now + FAILED_RETRY
                    runtime.problem = str(exc)
                    log("move.convert_failed", "warning", acct=account_ref(row["id"]), code=exc.code)
                    continue
                _store_converted(engine, current, profile, own)
                converted += 1
                engine.scheduler.request(row["id"], now)
                log("move.converted", acct=account_ref(row["id"]))
        else:
            runtime.reset(now)
    if converted:
        engine.notice(f"Finished importing {_count(converted, 'Claude Desktop account')} from the PC.", "good")
        engine.bump()


def _clear_lost(engine, row, own):
    """A deferred flag whose record is gone: the row keeps whatever usable profile it has."""
    flags = dict(row["flags"])
    flags.pop("deferred_desktop", None)
    flags["restorable"] = bool(own is not None and own.get("cookies"))
    engine.store.update(row["id"], flags=flags)
    log("move.deferred_lost", "warning", acct=account_ref(row["id"]))


class _Runtime:
    """Per engine, in memory: the ledger cache and the deferred conversion back-off."""

    def __init__(self, engine):
        self.lock = threading.RLock()
        self.ledger = _LedgerStore(engine.vault, engine.clock)
        self.next_at = 0.0
        self.refused_delay = REFUSED_FIRST
        self.row_retry = {}
        self.reason = None          # why the last attempt waited: "template", "missing", "locked", …
        self.problem = None         # the last conversion failure (names a cookie or key, never a value)

    def not_ready(self, reason, now):
        self.reason = reason
        if reason in ("template", "missing", "not_installed", "unsupported"):
            self.next_at = now + CHECK_EVERY
        else:                        # refused, locked or failed: the Keychain may have asked the owner
            self.next_at = now + self.refused_delay
            self.refused_delay = min(self.refused_delay * 2, REFUSED_MAX)

    def reset(self, now):
        self.next_at, self.refused_delay, self.reason = now + CHECK_EVERY, REFUSED_FIRST, None


_runtimes = weakref.WeakKeyDictionary()
_runtimes_lock = threading.Lock()


def _runtime(engine):
    with _runtimes_lock:
        found = _runtimes.get(engine)
        if found is None:
            found = _runtimes[engine] = _Runtime(engine)
        return found


def _ledger(engine):
    return _runtime(engine).ledger


class _account_lock:
    """The account's read lock (the scheduler's), so no usage read of it runs while its sign-in changes."""

    def __init__(self, engine, key):
        self.lock, self.timeout = engine.scheduler.account_lock(key), engine.timings.lock_wait

    def __enter__(self):
        if not self.lock.acquire(timeout=self.timeout):
            raise DeckError("This account is being refreshed. Try again in a moment.", "busy")
        return self

    def __exit__(self, *exc):
        self.lock.release()
        return False


# ---- the ledger (encrypted vault record) ----

class _LedgerStore:
    """The `move-import` vault record: {"version": 1, "imports": [entry, …]}, newest last. An entry is
    {"bundle_id", "kind", "created", "at", "complete", "code", "dismissed", "counts", "failures",
    "accounts": {PC account id: account id here}}. Cached in memory; an unreadable record is tried
    again after LEDGER_RETRY seconds, never on every snapshot."""

    def __init__(self, vault, clock):
        self.vault, self.clock = vault, clock
        self.lock = threading.RLock()
        self._doc, self._retry_at = None, 0.0

    def _load(self):
        if self._doc is not None:
            return self._doc
        empty = {"version": 1, "imports": []}
        if self.clock() < self._retry_at:
            return empty
        try:
            doc = self.vault.get(LEDGER_KEY) if self.vault.path(LEDGER_KEY).exists() else empty
        except DeckError:
            log("move.ledger_unreadable", "warning")
            self._retry_at = self.clock() + LEDGER_RETRY
            return empty
        self._doc = doc if isinstance(doc, dict) and isinstance(doc.get("imports"), list) else empty
        return self._doc

    def entries(self):
        with self.lock:
            return copy.deepcopy(self._load()["imports"])

    def entry(self, bundle_id):
        return next((item for item in reversed(self.entries()) if item.get("bundle_id") == bundle_id), None)

    def begin_account(self, header, source_id, target_id, at):
        """Persist a hand-over's matching row before changing it, for crash-safe resumption."""
        with self.lock:
            doc = copy.deepcopy(self._load())
            entry = next((item for item in doc["imports"] if item.get("bundle_id") == header.bundle_id), None)
            if entry is None:
                entry = {"bundle_id": header.bundle_id, "kind": header.kind, "created": header.created,
                         "at": at, "complete": False, "code": None, "dismissed": False,
                         "counts": {}, "failures": [], "accounts": {}}
                doc["imports"].append(entry)
            entry.setdefault("started", {})[source_id] = target_id
            self.vault.put(LEDGER_KEY, doc, generation=False)
            self._doc = doc

    def record(self, header, *, at, code, counts, failures, mapping):
        with self.lock:
            doc = copy.deepcopy(self._load())
            previous = next((item for item in doc["imports"] if item.get("bundle_id") == header.bundle_id), None)
            accounts = {**((previous or {}).get("accounts") or {}), **mapping}
            entry = {"bundle_id": header.bundle_id, "kind": header.kind, "created": header.created, "at": at,
                     "complete": code is not None, "code": code, "dismissed": False, "counts": dict(counts),
                     "failures": [dict(item) for item in failures], "accounts": accounts,
                     "started": dict((previous or {}).get("started") or {})}
            imports = [item for item in doc["imports"] if item.get("bundle_id") != header.bundle_id] + [entry]
            while len(imports) > LEDGER_KEEP:
                imports.remove(next((item for item in imports if item.get("dismissed")), imports[0]))
            doc["imports"] = imports
            self.vault.put(LEDGER_KEY, doc, generation=False)
            self._doc = doc
            return copy.deepcopy(entry)

    def dismiss(self, bundle_id):
        with self.lock:
            doc = copy.deepcopy(self._load())
            found = False
            for item in doc["imports"]:
                if item.get("bundle_id") == bundle_id and not item.get("dismissed"):
                    item["dismissed"], found = True, True
            if found:
                self.vault.put(LEDGER_KEY, doc, generation=False)
                self._doc = doc
            return found


def _entry_public(entry):
    """An import as the state and the action result show it; the PC-to-Mac id mapping stays inside."""
    failures = [dict(item) for item in entry.get("failures") or []]
    complete = bool(entry.get("code")) and not failures
    counts = {key: int((entry.get("counts") or {}).get(key, 0)) for key in OUTCOMES}
    if complete:
        message = (f"Every account from this {'hand-over' if entry.get('kind') == 'handover' else 'move'} bundle is "
                   f"saved on this Mac. {CODE_HINT}")
    elif failures:
        message = (f"{_count(len(failures), 'account')} could not be imported, so there is no confirmation code. "
                   f"{CANCEL_HINT}")
    else:
        message = "This import has not finished. Import the same bundle again to resume it."
    return {"bundle_id": entry.get("bundle_id"), "kind": entry.get("kind"), "at": entry.get("at"),
            "complete": complete, "code": entry.get("code") if complete else None, "counts": counts,
            "failures": failures, "message": message}


def dismiss(engine, bundle_id):
    """`import-bundle-dismiss`: hides that import's code or failure list."""
    if not isinstance(bundle_id, str) or not bundle_id:
        raise DeckError("Choose the import to dismiss.", "invalid")
    if not _ledger(engine).dismiss(bundle_id):
        raise DeckError("That import is no longer shown.", "not_found")


# ---- what the engine asks ----

def readable(row):
    """False for a row that must never be read: "on_pc", or a Claude Desktop account whose only profile
    still waits for conversion."""
    if row is None:
        return False
    return row["owner"] != ON_PC and (row.get("flags") or {}).get("deferred_desktop") != "waiting"


def check_action(engine, name, body):
    """Refuses an account action naming an "on_pc" row (only rename and remove may), and the reads and
    switches of a Claude Desktop account still waiting for conversion. True when it carried the action
    out itself: renaming an "on_pc" Claude Code row, which has no saved sign-in for CLIAccounts.rename
    to relabel."""
    key = body.get("id") if isinstance(body, dict) else None
    if not isinstance(key, str) or not key:
        return False
    row = engine.store.account(key)
    if row is None:
        return False
    if row["owner"] == ON_PC:
        if name == "rename" and row["provider"] == "claude_cli":
            engine.accounts.rename(key, body.get("label"))
            return True
        if name not in ON_PC_ALLOWED:
            raise DeckError(ON_PC_MESSAGE, "invalid")
        return False
    if row["flags"].get("deferred_desktop") == "waiting" and name in ("refresh", "switch-plan", "switch"):
        raise DeckError(WAITING_REFUSED, "login_needed")
    return False


def _waiting_desktop(rows):
    return sum(1 for row in rows if row["provider"] == "claude" and row["flags"].get("deferred_desktop"))


def state(engine, rows=None):
    """The `move_import` block of /api/state: {"imports": [undismissed imports, newest first, as
    _entry_public], "deferred": {"count", "message", "reason", "problem"}, "on_pc": count}."""
    rows = engine.store.accounts() if rows is None else rows
    waiting, runtime = _waiting_desktop(rows), _runtime(engine)
    imports = [_entry_public(item) for item in reversed(runtime.ledger.entries()) if not item.get("dismissed")]
    return {"imports": imports,
            "deferred": {"count": waiting, "message": deferred_message(waiting),
                         "reason": runtime.reason if waiting else None,
                         "problem": runtime.problem if waiting else None},
            "on_pc": sum(1 for row in rows if row["owner"] == ON_PC)}


def deferred_message(count):
    if not count:
        return None
    return f"Open Claude Desktop once on this Mac to finish importing {_count(count, 'account')}."


def banners(rows):
    """[banner] for Engine._banners: Claude Desktop accounts (`rows` are the store's) waiting for Claude
    Desktop on this Mac."""
    waiting = _waiting_desktop(rows)
    if not waiting:
        return []
    return [{"kind": "move_import", "title": "Finish importing Claude Desktop accounts",
             "message": deferred_message(waiting), "actions": [], "provider": "claude", "link": None}]


def start_import(engine, body):
    """`import-bundle`: submits ImportBundle and, unless body["wait"] is false, waits up to IMPORT_WAIT for
    it. {"op": id, "result": the import (_entry_public: code, counts, failures, message; also after a
    partial import, which ends the operation as failed), or None while it still runs or when it stopped
    before recording anything (the operation's report says why)}."""
    op = ImportBundle(engine, body.get("path"), body.get("passphrase"))
    started = engine.submit(op)
    if body.get("wait", True) is not False:
        op.wait(IMPORT_WAIT)
    return {**started, "result": copy.deepcopy(op.result)}


def last_result(engine, bundle_id=None):
    """The newest import (or that bundle's) as _entry_public shows it, also once dismissed; or None."""
    found = [item for item in _ledger(engine).entries() if bundle_id is None or item.get("bundle_id") == bundle_id]
    return _entry_public(found[-1]) if found else None


def import_from_file(engine, path, passphrase, *, timeout=None) -> dict:
    """For the automated setup (run.py): imports the bundle at `path`; returns the result
    ({"bundle_id", "kind", "at", "complete", "code", "counts", "failures", "message"}). With an Engine it
    runs ImportBundle and waits for it (`timeout` seconds, None = no limit); anything else must offer
    `action(name, body)` (the server's EngineAPI) and answers within IMPORT_WAIT. Read the passphrase from
    stdin or a private file, never argv. A partial import returns its result (complete False, no code,
    failures listed). DeckError when the bundle does not open, a hand-over is refused, nothing was recorded
    or the import is still running."""
    from .engine import Engine          # engine imports this module: imported on use
    if isinstance(engine, Engine):
        engine._own_the_login()
        op = ImportBundle(engine, str(path), passphrase)
        engine.submit(op)
        if not op.wait(timeout):
            raise DeckError("The import is still running. Check the dashboard for its code.", "busy")
        if op.result is None:
            report = engine.runner.report() or {}
            error = report.get("error") or {}
            raise DeckError(error.get("message") or "The import did not finish.", error.get("code") or "invalid")
        return copy.deepcopy(op.result)
    answer = engine.action(IMPORT_KIND, {"path": str(path), "passphrase": passphrase}) or {}
    if answer.get("result") is None:
        raise DeckError("The import is still running or did not finish. Check the dashboard.", "busy")
    return answer["result"]


# ---- history ----

def _copy_history(store, key, account, now):
    """The bundle's readings, snapshots and refills of one account with core.Store's retention, never a
    row twice, and its last good reading when newer than the one here."""
    history = account["history"]
    with store.lock:
        try:
            present = {row[0] for row in store.db.execute("SELECT at FROM snapshots WHERE account=?", (key,))}
            store.db.executemany(
                "INSERT INTO snapshots(account, at, usage) VALUES(?, ?, ?)",
                [(key, item["at"], _encode(item["usage"])) for item in history["snapshots"]
                 if item["usage"] is not None and item["at"] >= now - SNAPSHOT_DAYS * _DAY and item["at"] not in present])
            present = {(row[0], row[1]) for row in store.db.execute(
                "SELECT window_id, at FROM readings WHERE account=?", (key,))}
            store.db.executemany(
                "INSERT INTO readings(account, window_id, at, used, reset) VALUES(?, ?, ?, ?, ?)",
                [(key, item["window_id"], item["at"], item["used"], item["reset"]) for item in history["readings"]
                 if item["at"] >= now - READING_DAYS * _DAY and (item["window_id"], item["at"]) not in present])
            store.db.executemany(
                "INSERT OR IGNORE INTO refills(account, window_id, reset_at, confirmed_at, previous_used) "
                "VALUES(?, ?, ?, ?, ?)",
                [(key, item["window_id"], item["reset_at"], item["confirmed_at"], item["previous_used"])
                 for item in account["refills"] if item["confirmed_at"] >= now - REFILL_DAYS * _DAY])
            store.db.commit()
        except BaseException:
            store.db.rollback()
            raise
    usage, at = history["last_good_usage"], history["last_good_at"]
    if usage is not None and at is not None:
        held = store.account(key)["last_good_at"]
        if held is None or at > held:
            store.update(key, last_good_usage=usage, last_good_at=at)


# ---- small helpers ----

def _vault_record(vault, key):
    """The decrypted record, or None when it is missing or unreadable."""
    try:
        return vault.get(key) if vault.path(key).exists() else None
    except DeckError:
        return None


def _captured(profile):
    value = (profile or {}).get("captured")
    return float(value) if _is_number(value) else 0.0


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _encode(value):
    return json.dumps(value, separators=(",", ":"))


def _label(account):
    return account.get("label") or account.get("email") or account["provider"].replace("_", " ").title()


def _count(number, noun):
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _verb(number, verb):
    return {"was": ("was", "were"), "is": ("is", "are")}[verb][number != 1]


def _summary(kind, counts, failures, code):
    """The operation's summary, which the engine also files as an event: never the code."""
    stored = sum(counts[key] for key in ("new", "updated", "filled", "kept", "deferred"))
    if kind == "handover":
        text = f"Took over {_count(stored, 'account')} handed over from the PC."
    else:
        text = f"Imported {_count(stored, 'account')} from the PC."
        if counts["on_pc"]:
            text += (f" {_count(counts['on_pc'], 'account')} still signed in on the PC "
                     f"{_verb(counts['on_pc'], 'is')} listed without a sign-in.")
    if failures:
        return f"{text} {_count(len(failures), 'account')} could not be imported. {CANCEL_HINT}"
    if counts["deferred"]:
        text += " " + deferred_message(counts["deferred"])
    return text + (" The confirmation code is shown on the dashboard." if code else "")
