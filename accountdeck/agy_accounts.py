"""Antigravity's accounts (`engine.agy`): who agy is signed in to, the saved accounts and their last usage readings,
Save, Remove, Rename, the order, the usage command, Add (the sign-in in a Terminal window) and recovery at start.
Switching itself is switch_antigravity's job.

Antigravity is not one of the engine's usage providers (`engine.providers`), and its accounts are not rows of
`state.accounts`: they live in their own table (`antigravity_accounts`, Store.agy_*) and vault records (`agy-<id>`,
`agy-transaction-<uuid>`, `agy-enrollment`), and everything about them is in the state's `providers.antigravity`
block (contract._check_agy). So nothing automatic reaches them: no scheduled read, no auto-switch, no runway, no
alert. The rules this service keeps:

- AccountDeck never sends a request to Google and never switches an Antigravity account by itself.
- agy's usage command runs only on the user's click: Refresh (at most once a minute) and `looked()` (the menu or the
  dashboard opened) when `agy_usage_on_open` is on, an account is signed in, agy is installed and the last reading or
  try is older than 15 minutes. Never on a timer and never on the engine's tick.
- No running agy is moved or signed out: Switch and Add wait or refuse while one runs.
- Nothing under ~/.gemini is written; no token reaches a log, an error text, a process argument or the state.

The records this service keeps in the vault (all `agy-`, so no older build ever takes one for its own):
- `agy-<id>`: a saved sign-in, providers.antigravity.saved_record (the usual ring of copies).
- `agy-enrollment`: an Add under way, {"v": 1, "started", "expires", "account" (the saved id signed in again, or
  null), "before" (the record agy held when it began, or null), "before_email", "window" (the record of the Terminal
  window agy's sign-in runs in, procs.cli_enrollment_token, or null), "ending" (null while it waits for a sign-in;
  else why it ends without one, ENDINGS: the earlier sign-in goes back once no agy runs)}. It outlives a restart.
- `agy-transaction-<uuid>` (written by switch_antigravity): a switch under way, {"v": 1, "status", "at", "account"
  (the target), "before", "before_email", "after", "after_email"}: both records whole, so Recover can put either back
  whatever happened to the saved accounts since.
"""
from __future__ import annotations

import os
import threading
import uuid

from . import contract, diagnostics
from .core import AGY_ENROLLMENT, AGY_RECORD_PREFIX, DeckError, agy_key
from .platform import facts_mac
from .platform.keychain import KeychainError
from .providers.antigravity import NOT_CHECKED, OTHER_WINDOW, StoreRead, saved_record

__all__ = ["AgyService", "USAGE_ON_OPEN_AGE", "REFRESH_EVERY", "USAGE_TIMEOUT", "ENROLLMENT_TIMEOUT", "holds_record"]

UNSAVED_ID = contract.UNSAVED_IDS["antigravity"]
USAGE_ON_OPEN_AGE = 15 * 60       # looked() runs the usage command when the last reading or try is older than this
REFRESH_EVERY = 60                # Refresh runs it at most this often
USAGE_TIMEOUT = 60                # one usage run is stopped after this long
ENROLLMENT_TIMEOUT = 15 * 60      # an Add gives up (and puts the earlier sign-in back) after this long
ENDED_SHOWN = 10 * 60             # how long the state still shows how an Add ended
SENSE_EVERY = 30                  # the keychain is looked at no more often than this, but see `sense`
SENSE_BACKOFF = 60                # after a keychain look that timed out, none for this long
OPEN_OWED = 30                    # for this long a look still decides usage on open again (`sense`)
# Why an Add ends without a new sign-in to keep: Cancel, agy's sign-in window closed (it ends as cancelled), the time
# limit, or a window that never opened (it ends as failed). Each puts the earlier sign-in back once no agy runs.
ENDINGS = ("cancelled", "closed", "timed_out", "unopened")
TRANSIENT = ("unreadable", "not_checked")    # a store that reads as unknown for these may read again in a moment

READ_ONLY = "Another AccountDeck 2 is managing your sign-ins. Quit it to make changes here."
BUSY = "Finish or cancel the current action first."
NOT_SIGNED_IN = "Nobody is signed in to Antigravity on this Mac."
NOT_INSTALLED = "agy is not installed on this Mac."
ALREADY_SAVED = "This Antigravity account is saved already."
ADD_WHILE_RUNNING = ("Close every agy session first: while you add an account agy has no sign-in on this Mac, so a "
                     "running session would be signed out.")
PUT_BACK_WHILE_RUNNING = "Close every agy session first: a running session would move to the account put back."
STORE_MOVED = "agy's sign-in changed just now, so AccountDeck changed nothing. Try again."
SIGNING_IN = ("Sign in with Google in the Terminal window AccountDeck opened. If the browser picks the wrong account, "
              "use a private window.")
RESUMED = ("AccountDeck restarted while you were adding an Antigravity account. Finish the sign-in in its Terminal "
           "window, or cancel to get the earlier sign-in back.")
MISSING_RECORD = "Its saved sign-in is missing. Sign in again to use it."
# While an Add ends (`restoring`): what it waits for before it puts {email}, the earlier sign-in, back.
WAIT_AGY = "Close agy's sign-in window. AccountDeck puts {email} back as soon as agy has closed."
WAIT_OTHER = "Close every agy session. AccountDeck puts {email} back as soon as none runs."    # the window has ended
WAIT_KEYCHAIN = ("The keychain is locked or did not answer, so AccountDeck cannot put {email} back yet. Unlock it, "
                 "or choose Allow when macOS asks: AccountDeck tries again in a moment.")
WAIT_WRITE = "AccountDeck could not put {email} back yet. It tries again in a moment."
EARLIER = "the earlier sign-in"     # {email} when the record kept no e-mail
RESTORING_REFUSAL = "Wait until the Add has ended. {message}"
ENDED = {"cancelled": "Cancelled.", "closed": "agy's sign-in window closed before anyone signed in, so the Add ended.",
         "timed_out": "Nobody signed in within 15 minutes, so the Add ended.",
         "unopened": "agy's sign-in could not be opened, so the Add ended."}
UNCHANGED = "The Add ended: agy is still signed in as {email}. Nothing was changed."
LANDED_DESPITE_CANCEL = " The sign-in had finished, so it stays and the earlier one was not put back."
# A sign-in landed that AccountDeck cannot save (a work or school account, or one it cannot read): left alone.
WORK_SIGN_IN = "AccountDeck cannot save a work or school sign-in, so it left the one in agy alone."
OTHER_SIGN_IN = "{message} AccountDeck left agy's sign-in alone."
GO_BACK = " {label} is saved: to go back to it, sign out of the other account in agy first, then choose Switch."
KEYCHAIN_SLOW = ("The keychain did not answer in time when AccountDeck looked at Antigravity's sign-in. AccountDeck "
                 "looks again in a minute.")
USAGE_NOT_STARTED = "AccountDeck could not start checking Antigravity's usage. Try again in a moment."
USAGE_RUNNING = "Antigravity's usage is being checked. Try again in a moment."
RECOVER_UNREAD = "Recover changes nothing until AccountDeck can read agy's sign-in."
# A usage run that could not start because agy is gone (the other failures: providers.antigravity.RUN_ERRORS).
RUN_NOT_FOUND = ("agy was not found on this Mac.", "not_installed")
# What makes the vault copy of a saved account follow the live record (`_differs`): a new sign-in or a new plan, not
# agy's hourly renewal of its access token.
FOLLOWED = (("token", "refresh_token"), ("user_tier",), ("tier_display_name",))


def _field(record, path):
    value = record
    for name in path:
        value = value.get(name) if isinstance(value, dict) else None
    return value


def _differs(kept, live) -> bool:
    """True when the live record is a new sign-in or plan compared with the kept one. Compared only, never logged."""
    return any(_field(kept, path) != _field(live, path) for path in FOLLOWED)


def store_refusal(found) -> DeckError:
    """Why nothing can be saved, added or switched with the store as `found` reads."""
    if found.state == "absent":
        return DeckError(NOT_SIGNED_IN, "login_needed")
    return DeckError(found.message or NOT_CHECKED, "antigravity")


def check_label(label) -> str:
    label = label.strip() if isinstance(label, str) else ""
    if not 1 <= len(label) <= 80:
        raise DeckError("Use a name of 1 to 80 characters.", "invalid")
    return label


def tightest(reading, now):
    """(group name, bucket) of the bucket with the least left in `reading`, or None. A bucket whose reset has passed
    since the reading counts as full (PLAN §8), a disabled one or one without a fraction not at all."""
    best = None
    for group in (reading or {}).get("groups") or ():
        for bucket in group.get("buckets") or ():
            left = bucket.get("remaining")
            if bucket.get("disabled") or left is None:
                continue
            if bucket.get("resets_at") is not None and bucket["resets_at"] <= now:
                left = 1.0
            if best is None or left < best[0]:
                best = (left, group.get("name") or "", bucket)
    return None if best is None else (best[1], {**best[2], "remaining": best[0]})


def limit_text(reading, now) -> str | None:
    """The tightest limit in words, e.g. "Gemini Models 5-hour 25% left", or None without a reading."""
    found = tightest(reading, now)
    if found is None:
        return None
    group, bucket = found
    window = {"5h": "5-hour", "weekly": "weekly", OTHER_WINDOW: ""}.get(bucket.get("window"), bucket.get("window") or "")
    return f"{group} {window} {round(bucket['remaining'] * 100)}% left".replace("  ", " ").strip()


class AgyService:
    """Antigravity for one engine. `adapter` is a providers.antigravity.Antigravity (FakeAntigravity in tests and the
    demo). Sensing and the open Add's watch run on the engine's tick; the actions on the server's threads (the slow
    ones as operations on `ad2-ops`), the usage run on a thread of its own. `lock` guards what the state reads;
    `gate` makes the steps that change agy's store (an Add's start, end and put-back, Recover) one at a time."""

    def __init__(self, engine, adapter):
        self.engine, self.adapter = engine, adapter
        self.store, self.vault, self.prefs, self.clock = engine.store, engine.vault, engine.prefs, engine.clock
        self.paths = engine.paths
        self.lock, self.gate = threading.RLock(), threading.RLock()
        self.installed = False
        self.found = StoreRead.unknown("not_checked", NOT_CHECKED)     # the store as last sensed
        self._marker, self._sensed = None, False
        # When the keychain was last looked at, until when it is left alone after a look that timed out, and whether
        # the next sense looks at once (the user looked, or AccountDeck wrote agy's item itself).
        self._sensed_at, self._backoff_until, self._due = None, None, False
        self._opened = None             # when the user last looked, until agy's store is looked at next (`looked`)
        self.refresh_busy, self.last_try = False, None     # the usage command (`refresh`, `looked`, `read_usage`)
        self._tries = {}                # e-mail -> when its last usage run started (looked() asks per account)
        # The signed-in account's last reading and error while it is not saved: {"email", "reading", "error"}.
        self.unsaved_usage = None
        self.enrollment = None          # an Add under way or just ended, as the state shows it
        self._enroll, self._enroll_ended = None, None      # the open Add's record (`agy-enrollment`), when one ended
        self.recovery = None            # a switch left for Recover, as the state shows it
        self.recovery_key = None        # its `agy-transaction-` record
        self._usage_thread = None
        self._version_busy = False      # an `agy --version` run under way (`check_version`)

    # ---- at start ----
    def recover_at_start(self) -> None:
        """Once at start, when this copy owns the sign-ins: an Add left open (`agy-enrollment`) resumes, watching
        for a new sign-in, or, when it was ending, putting the earlier one back once no agy runs; a switch left
        unfinished (`agy-transaction-*`) shows in `recovery` for Recover (core.recover_transactions never lists one);
        and the saved accounts and their records are made to agree (`reconcile`)."""
        if self.vault.path(AGY_ENROLLMENT).exists():
            try:
                doc = self.vault.get(AGY_ENROLLMENT)
            except DeckError as exc:     # a locked keychain never stops the start; the record stays for later
                diagnostics.log("agy.enrollment_unread", lvl="warning", code=exc.code)
                doc = None
            if _enrollment_doc(doc):
                with self.lock:
                    self._enroll = doc
                    if doc.get("ending") is None:
                        self.enrollment = {"phase": "signing_in", "account": doc["account"], "started": doc["started"],
                                           "expires": doc["expires"], "email": None, "message": RESUMED}
                    else:
                        self.enrollment = {"phase": "restoring", "account": doc["account"], "started": doc["started"],
                                           "expires": None, "email": None, "message": _wait_agy(doc)}
                diagnostics.log("agy.enrollment_resumed", ending=doc.get("ending"))
            elif doc is not None:
                diagnostics.log("agy.enrollment_unknown", lvl="warning")
        self.scan_transactions()
        self.reconcile()

    def reconcile_restored(self) -> tuple[int, int]:
        """After core put an older database in place (Engine._reconcile_restored): `reconcile`, and its counts."""
        return self.reconcile()

    def reconcile(self) -> tuple[int, int]:
        """Makes the table and the records agree (at every start and after a database restore). A saved sign-in
        (`agy-<id>`) without its row gets the row back from the record's e-mail under its own id, unless a row with
        that e-mail has its own record; a row with that e-mail and no record of its own takes this record's id
        (Store.agy_rekey: no record is ever renamed, so an update that compares the records' names before and after
        finds them all). A row whose record is gone stays and asks for a new sign-in (error_code "reauth_required").
        Returns (rows back, rows without a record). A record that cannot be read is left as it is."""
        back = 0
        known = {row["id"] for row in self.store.agy_accounts()}
        for key in sorted(self.vault.agy_account_keys()):
            account_id = key.removeprefix(AGY_RECORD_PREFIX)
            if account_id in known:
                continue
            try:
                saved = self.vault.get(key)
            except KeychainError:
                raise
            except DeckError:
                diagnostics.log("agy.record_unreadable", lvl="warning", acct=diagnostics.account_ref(account_id))
                continue
            record = (saved.get("stores") or {}).get("cli") if isinstance(saved, dict) else None
            email = saved.get("email") if isinstance(saved, dict) else None
            if not isinstance(record, dict) or not isinstance(email, str) or "@" not in email:
                diagnostics.log("agy.record_unknown", lvl="warning", acct=diagnostics.account_ref(account_id))
                continue
            other = self.store.agy_find(email)
            if other is not None:
                if self.vault.path(agy_key(other["id"])).exists():
                    continue            # that account has its own record: this one is a leftover, kept as it is
                self.store.agy_rekey(other["id"], account_id)
                known.add(account_id)
                row = self.store.agy_account(account_id)
                if row is not None and row["error_code"] == "reauth_required" and row["error"] == MISSING_RECORD:
                    self._clear_error(row)
                diagnostics.log("agy.row_rekeyed", acct=diagnostics.account_ref(account_id))
                back += 1
                continue
            self.store.agy_add(email, plan=_text(record.get("tier_display_name")), tier=_text(record.get("user_tier")),
                               saved_at=saved.get("saved_at") if _number(saved.get("saved_at")) else None,
                               key=account_id)
            known.add(account_id)
            diagnostics.log("agy.row_back", acct=diagnostics.account_ref(account_id))
            back += 1
        lost = 0
        for row in self.store.agy_accounts():
            if self.vault.path(agy_key(row["id"])).exists():
                continue
            lost += 1
            if row["error_code"] != "reauth_required":
                self._set_error(row, MISSING_RECORD, "reauth_required")
                diagnostics.log("agy.record_missing", lvl="warning", acct=diagnostics.account_ref(row["id"]))
        if back or lost:
            self.engine.bump()
        return back, lost

    def scan_transactions(self) -> None:
        """`recovery` from the `agy-transaction-` records: the oldest open one, or None."""
        found = []
        for key in self.vault.agy_transaction_keys():
            try:
                doc = self.vault.get(key)
            except DeckError as exc:
                diagnostics.log("agy.transaction_unread", lvl="warning", code=exc.code)
                continue
            if isinstance(doc, dict) and _number(doc.get("at")):
                found.append((doc["at"], key, doc))
        with self.lock:
            if not found:
                self.recovery, self.recovery_key = None, None
                return
            at, key, doc = min(found)
            self.recovery_key = key
            self.recovery = {"started": at, "account": _text(doc.get("account")), "before": _text(doc.get("before_email")),
                             "after": _text(doc.get("after_email"))}

    # ---- who is signed in (the engine's tick and sense_active) ----
    def sense(self, *, force=False) -> bool:
        """Reads (read-only) whether agy is installed and who its store is signed in to, and returns True when what
        the state shows changed. With agy not installed and nothing saved or open, the keychain is not asked at all.
        Otherwise the keychain is looked at no more than every SENSE_EVERY seconds: at once when the user looked
        (`looked`) or AccountDeck wrote agy's item itself (`noted_write`), on every call while an Add is open, and
        with `force` (a plan or an action of the user's, which then also reads the record whatever the marker says).
        A look is the store's change marker (`probe`, a short time limit of its own), and the record is read only
        when the marker changed; a look that timed out leaves the keychain alone for SENSE_BACKOFF seconds, the store
        showing as unknown meanwhile, so a hanging keychain never stalls the tick. A saved account's vault copy then
        follows the live record when its refresh token or plan changed (a new sign-in), never on agy's hourly
        renewal alone."""
        installed = self.adapter.executable() is not None
        if not installed and not self._in_use():
            # Nobody uses Antigravity on this Mac: the keychain is not asked at all (else a `security` run every tick).
            found = StoreRead.unknown("not_checked", NOT_CHECKED)
            with self.lock:
                before = (self.installed, self.found)
                self.installed, self.found, self._marker, self._sensed = False, found, None, True
            return before != (False, found)
        now = self.clock()
        with self.lock:
            if self._backoff_until is not None and now < self._backoff_until:
                changed = self.installed != installed
                self.installed = installed
                return changed
            due = (force or self._due or self._enroll is not None or not self._sensed or self._sensed_at is None
                   or not 0 <= now - self._sensed_at < SENSE_EVERY)
            if not due:
                return False
            self._due, self._sensed_at, self._backoff_until = False, now, None
            opened, self._opened = self._opened, None
        try:
            marker = self.adapter.probe()
        except DeckError as exc:
            if not _timed_out(exc):       # it answered with an error: read once, then wait for it to answer again
                marker = ("unanswered", exc.code)
            else:                         # it did not answer: left alone for a while, and nothing is read meanwhile
                found = StoreRead.unknown("unreadable", KEYCHAIN_SLOW)
                with self.lock:
                    before = (self.installed, self.found)
                    self.installed, self.found, self._marker, self._sensed = installed, found, None, False
                    self._backoff_until = now + SENSE_BACKOFF
                diagnostics.log("agy.keychain_slow", lvl="warning", wait=SENSE_BACKOFF)
                return before != (installed, found)
        with self.lock:
            if not force and self._sensed and (installed, marker) == (self.installed, self._marker):
                return False
        found = self.adapter.read()
        with self.lock:
            before = (self.installed, self.found)
            self.installed, self.found, self._marker, self._sensed = installed, found, marker, True
        if found.state == "ok":
            self._follow(found)
        if (opened is not None and not force and 0 <= now - opened < OPEN_OWED
                and (before[1].state, before[1].email) != (found.state, found.email)):
            # The user opened AccountDeck a moment ago, and usage on open was decided for the sign-in last seen: it
            # is decided once more for the one found now. (An action of the user's, `force`, decides for itself.)
            self._usage_on_open()
        return before != (installed, found)

    def noted_write(self, found) -> None:
        """AccountDeck has just written or deleted agy's item itself (Switch, Add, its end, Recover) and read it back
        as `found` (a StoreRead, or None when that read failed): the state shows that read, and the next sense reads
        the store again whatever the change marker says. The marker keeps the item's modification time to the
        second, so a write of AccountDeck's own can leave it unmoved."""
        with self.lock:
            if found is not None:
                self.found = found
            self._marker, self._sensed, self._due = None, False, True
        self.engine.bump()

    def _seen(self, found) -> None:
        """A fresh read of agy's store made for an action (Add, its end, Recover) is what the state shows."""
        with self.lock:
            changed = self.found != found
            self.found = found
        if changed:
            self.engine.bump()

    def _backed_off(self) -> bool:
        with self.lock:
            return self._backoff_until is not None and self.clock() < self._backoff_until

    def _in_use(self) -> bool:
        """An account is saved, or an Add or a recovery is open: agy's store is watched even while agy is missing."""
        with self.lock:
            if self._enroll is not None or self.recovery is not None:
                return True
        return bool(self.store.agy_accounts())

    def _follow(self, found) -> None:
        if not self.engine.owns_login:
            return
        with self.lock:
            if self._enroll is not None:      # an open Add saves what appears itself (poll_enrollment)
                return
        row = self.store.agy_find(found.email)
        if row is None:
            return
        key = agy_key(row["id"])
        if self.vault.path(key).exists():
            try:
                kept = self.vault.get(key)["stores"]["cli"]
            except (DeckError, KeyError, TypeError):
                return          # unreadable now (a locked keychain): it stays as it is until it can be compared
            if not _differs(kept, found.record):
                return
        self.keep(row, found)
        diagnostics.log("agy.copy_followed", acct=diagnostics.account_ref(row["id"]))

    def keep(self, row, found) -> None:
        """The live record (`found`, of `row`'s e-mail) becomes the saved account's vault copy. A new sign-in ends a
        "needs a new sign-in" error."""
        key, now = agy_key(row["id"]), self.clock()
        renewed = True
        if self.vault.path(key).exists():
            try:
                renewed = _field(self.vault.get(key)["stores"]["cli"], ("token", "refresh_token")) != _field(
                    found.record, ("token", "refresh_token"))
            except (DeckError, KeyError, TypeError):
                renewed = True
        self.vault.put(key, saved_record(found.email, found.record, saved_at=now))
        self.store.agy_update(row["id"], plan=found.plan, tier=found.tier, saved_at=now)
        if renewed and row["error_code"] == "reauth_required":
            self._clear_error(self.store.agy_account(row["id"]))
        self.engine.bump()

    def keep_outgoing(self, found) -> str:
        """Before agy's store changes (Switch, Add): the account signed in now is kept, a saved one's vault copy
        replaced by the live record, an unsaved one saved as a new account. Returns its id."""
        row = self.store.agy_find(found.email)
        if row is None:
            return self.save_new(found)
        self.keep(row, found)
        return row["id"]

    def save_new(self, found, label=None) -> str:
        """A new saved account from the store's record: its vault record first, then its row (so no row is ever
        without a record), and the unsaved row's reading carried over. Returns its id."""
        key, now = uuid.uuid4().hex, self.clock()
        self.vault.put(agy_key(key), saved_record(found.email, found.record, saved_at=now))
        try:
            self.store.agy_add(found.email, label=label, plan=found.plan, tier=found.tier, saved_at=now, key=key)
        except BaseException:
            self.vault.remove(agy_key(key))
            raise
        with self.lock:
            carried = self.unsaved_usage if (self.unsaved_usage or {}).get("email") == found.email else None
            if carried is not None:
                self.unsaved_usage = None
        if carried is not None and carried.get("reading"):
            self.store.agy_record_usage(key, carried["reading"], at=carried["reading"]["at"])
        diagnostics.log("agy.saved", acct=diagnostics.account_ref(key))
        self.engine.bump()
        return key

    # ---- the state ----
    def owns(self, account_id) -> bool:
        """True for the id of a saved Antigravity account and for "unsaved-antigravity": the engine and
        switching.dispatch route such an action here."""
        if account_id == UNSAVED_ID:
            return True
        return isinstance(account_id, str) and self.store.agy_account(account_id) is not None

    @property
    def enrolling(self) -> bool:
        with self.lock:
            return self._enroll is not None

    @property
    def restoring(self) -> bool:
        """An Add is ending (phase `restoring`): it waits to put the earlier sign-in back."""
        with self.lock:
            return self._enroll is not None and self._enroll.get("ending") is not None

    def state(self, now) -> dict:
        """The keys of `providers.antigravity` only Antigravity has (contract._AGY_KEYS; contract._check_agy has the
        shapes). The engine adds the keys every app has (status, active, running, switching, paused)."""
        with self.lock:
            found, installed = self.found, self.installed
            unsaved, enrollment, recovery = self.unsaved_usage, self.enrollment, self.recovery
            busy, last_try, ended, open_add = self.refresh_busy, self.last_try, self._enroll_ended, self._enroll
        if enrollment is not None and open_add is None and ended is not None and now - ended >= ENDED_SHOWN:
            enrollment = None
        signed_in = found.email if found.state == "ok" else None
        rows = []
        for row in self.store.agy_accounts():
            error = None
            if row["error"]:
                error = {"code": row["error_code"] or "antigravity", "message": row["error"],
                         "since": (row["flags"] or {}).get("error_since")}
            rows.append({"id": row["id"], "label": row["label"], "email": row["email"], "plan": row["plan"],
                         "saved": True, "active": row["email"] == signed_in,
                         "switchable": self.vault.path(agy_key(row["id"])).exists(), "usage": row["usage"],
                         "error": error})
        if signed_in is not None and not any(row["active"] for row in rows):
            own = unsaved if unsaved is not None and unsaved["email"] == signed_in else {}
            rows.append({"id": UNSAVED_ID, "label": signed_in.partition("@")[0] or "Current Antigravity account",
                         "email": signed_in, "plan": found.plan, "saved": False, "active": True, "switchable": False,
                         "usage": own.get("reading"), "error": own.get("error")})
        next_allowed = None if last_try is None or now - last_try >= REFRESH_EVERY else last_try + REFRESH_EVERY
        return {"installed": installed,
                "store": {"state": found.state, "reason": found.reason, "message": found.message},
                "signed_in": signed_in, "accounts": rows,
                "refresh": {"busy": busy, "last_try": last_try, "next_allowed": next_allowed},
                "enrollment": None if enrollment is None else dict(enrollment),
                "recovery": None if recovery is None else dict(recovery),
                "notice_seen": bool(self.prefs.get("agy_notice_seen")),
                # T3 Code keeps an Antigravity sign-in of its own there: the page says a switch here leaves it as it
                # is. Only whether the folder exists is looked at.
                "t3_code": os.path.isdir(self.paths.t3_antigravity)}

    # ---- usage ----
    def looked(self) -> bool:
        """The user looked at AccountDeck (Engine.looked, once the state they see was built): the next tick looks at
        agy's store at once (so that whoever asks never waits for the keychain), `agy --version` runs once for an agy
        not checked yet (`check_version`), and usage on open is decided (`_usage_on_open`) for the sign-in last seen.
        When that next look at the store finds another sign-in (one made in the last seconds), `sense` decides it
        once more for that one. Returns at once: True when a usage run started (the state changed)."""
        self.check_version()
        with self.lock:
            self._due, self._opened = True, self.clock()
        return self._usage_on_open()

    def _usage_on_open(self) -> bool:
        """Starts the usage command in the background when `agy_usage_on_open` is on, usage checks are not paused,
        an account is signed in, agy is installed and the signed-in account's last reading and last try are both
        older than USAGE_ON_OPEN_AGE. Only for a look of the user's (`looked`). True when a run started."""
        if not self.prefs.get("agy_usage_on_open") or self.prefs.get("refresh_paused"):
            return False
        now = self.clock()
        if self._usage_refusal(now) is not None:
            return False
        with self.lock:
            email = self.found.email
            tried = self._tries.get(email)
            unsaved = self.unsaved_usage if (self.unsaved_usage or {}).get("email") == email else None
        row = self.store.agy_find(email)
        reading = row["usage"] if row is not None else (unsaved or {}).get("reading")
        latest = max((at for at in ((reading or {}).get("at"), tried) if at is not None), default=None)
        if latest is not None and now - latest < USAGE_ON_OPEN_AGE:
            return False
        try:
            self._start_usage(now)
        except DeckError as exc:        # logged: nobody asked, so nobody waits for an answer
            diagnostics.log("agy.usage_on_open_failed", lvl="warning", code=exc.code)
            return False
        return True

    def refresh(self) -> dict:
        """Refresh: starts the usage command for the signed-in account on a thread of its own, at most once every
        REFRESH_EVERY seconds (DeckError "rate_limited" with retry_after otherwise), and returns {} at once; the state
        shows `refresh.busy` until it ends. Paused usage checks pause it too, as for the other apps. It looks at agy's
        store first (Engine.sense_active), so it answers for the account signed in now."""
        if self.prefs.get("refresh_paused"):
            raise DeckError("Usage checks are paused. Turn them back on in Settings.", "invalid")
        self.check_version()
        self.engine.sense_active(only="antigravity")
        now = self.clock()
        refusal = self._usage_refusal(now)
        if refusal is not None:
            raise refusal
        self._start_usage(now)
        return {}

    def _usage_refusal(self, now):
        refusal = self.restoring_refusal()
        if refusal is not None:
            return refusal
        with self.lock:
            installed, found, busy, last_try = self.installed, self.found, self.refresh_busy, self.last_try
        if not installed:
            return DeckError(NOT_INSTALLED, "not_installed")
        if found.state != "ok":
            return store_refusal(found)
        if busy:
            return DeckError("Antigravity's usage is being checked.", "busy")
        pending = self.engine.pending
        if (pending and pending["provider"] == "antigravity") or self.enrolling or self.recovery is not None:
            return DeckError(BUSY, "busy")
        if last_try is not None and now - last_try < REFRESH_EVERY:
            wait = REFRESH_EVERY - (now - last_try)
            return DeckError("Antigravity's usage was checked less than a minute ago.", "rate_limited", wait)
        return None

    def _start_usage(self, now) -> None:
        with self.lock:
            if self.refresh_busy:
                return
            email = self.found.email
            before = (self.last_try, self._tries.get(email))
            self.refresh_busy, self.last_try = True, now
            self._tries[email] = now
        self.engine.bump()
        if self.engine.inline:
            self.read_usage()
            return
        thread = threading.Thread(target=self.read_usage, name="ad2-agy-usage", daemon=True)
        try:
            thread.start()
        except RuntimeError as exc:      # no thread could be started: nothing runs, so nothing is busy either
            with self.lock:
                self.refresh_busy, self.last_try = False, before[0]
                if before[1] is None:
                    self._tries.pop(email, None)
                else:
                    self._tries[email] = before[1]
            diagnostics.log("agy.usage_not_started", lvl="warning")
            self.engine.bump()
            raise DeckError(USAGE_NOT_STARTED, "system") from exc
        self._usage_thread = thread

    def read_usage(self) -> None:
        """One usage run (the thread `refresh` and `looked` start): reads the store's e-mail before, runs the
        command (procs.run_agy_usage, USAGE_TIMEOUT; the controller prepares its folder and log), reads the store
        again and keeps the result only when the e-mail is the same: the reading (providers.antigravity.
        reading_from_run) goes to the saved account with that e-mail (Store.agy_record_usage) or to the unsaved row
        (memory only). A failure keeps the last good reading and sets a short error (a refused sign-in:
        "reauth_required")."""
        from .providers import antigravity       # how agy's answer reads, and how a failed run does, are the adapter's
        outcome, email = "failed", None
        try:
            before = self.adapter.read()
            if before.state != "ok":
                outcome = "not_signed_in"
                return
            email = before.email
            run, failure, reading = None, None, None
            exe = self.adapter.executable()
            if exe is None:
                failure = RUN_NOT_FOUND
            else:
                try:
                    run = self.engine.procs.run_agy_usage(exe, cwd=self.paths.agy_work,
                                                          log_file=self.paths.agy_usage_log, timeout=USAGE_TIMEOUT)
                except DeckError as exc:
                    failure = (str(exc), exc.code)
            after = self.adapter.read()
            if after.state != "ok" or after.email != email:
                outcome = "dropped"         # the account changed while agy ran: the answer may be either one's
                return
            if run is not None:
                try:
                    reading = antigravity.reading_from_run(run, now=self.clock(), after=after)
                except DeckError as exc:
                    failure = (str(exc), exc.code)
            if self.engine.stopping.is_set():
                outcome = "stopped"
                return
            if failure is not None:
                outcome = failure[1]
                self._keep_failure(email, *failure)
            else:
                outcome = "ok"
                self._keep_reading(email, reading)
        except Exception as exc:       # a bug or a store that went away: logged, and the next click tries again
            diagnostics.log_exception("agy.usage_failed", exc)
        finally:
            with self.lock:
                self.refresh_busy = False
            diagnostics.log("agy.usage", outcome=outcome)
            self.engine.bump()

    def _keep_reading(self, email, reading) -> None:
        row = self.store.agy_find(email)
        if row is None:
            with self.lock:
                self.unsaved_usage = {"email": email, "reading": reading, "error": None}
            return
        self.store.agy_record_usage(row["id"], reading, at=reading["at"])
        if "error_since" in row["flags"]:
            self.store.agy_update(row["id"], flags={k: v for k, v in row["flags"].items() if k != "error_since"})

    def _keep_failure(self, email, message, code) -> None:
        row = self.store.agy_find(email)
        if row is None:
            with self.lock:
                kept = self.unsaved_usage if (self.unsaved_usage or {}).get("email") == email else {}
                since = (kept.get("error") or {}).get("since") or self.clock()
                self.unsaved_usage = {"email": email, "reading": kept.get("reading"),
                                      "error": {"code": code, "message": message, "since": since}}
            return
        self._set_error(row, message, code)

    def _set_error(self, row, message, code) -> None:
        self.store.agy_record_error(row["id"], message, code if code in contract.ERROR_CODES else "antigravity")
        if not (row["error"] and row["flags"].get("error_since")):
            self.store.agy_update(row["id"], flags={**row["flags"], "error_since": self.clock()})

    def _clear_error(self, row) -> None:
        if row is None:
            return
        self.store.agy_update(row["id"], error=None, error_code=None,
                              flags={k: v for k, v in row["flags"].items() if k != "error_since"})

    # ---- agy's version (the doctor) ----
    def check_version(self) -> None:
        """`agy --version` for the doctor, only for the user (a look, Refresh, a plan or an action of theirs) and at
        most once for each installed binary (the adapter remembers the answer): on a thread of its own, so nobody
        waits for it. Never on the tick or a timer."""
        if self.adapter.executable() is None or self.adapter.known_version()[0]:
            return
        with self.lock:
            if self._version_busy:
                return
            self._version_busy = True
        if self.engine.inline:
            self._run_version()
            return
        thread = threading.Thread(target=self._run_version, name="ad2-agy-version", daemon=True)
        try:
            thread.start()
        except RuntimeError:          # the next look tries again
            with self.lock:
                self._version_busy = False
            diagnostics.log("agy.version_not_started", lvl="warning")

    def _run_version(self) -> None:
        try:
            self.adapter.version()
        except Exception as exc:       # the doctor then says "not checked yet"
            diagnostics.log_exception("agy.version_failed", exc)
        finally:
            with self.lock:
                self._version_busy = False

    # ---- the account list ----
    def capture(self, label=None) -> str:
        """Save: the signed-in, unsaved account becomes a saved one (its record to `agy-<id>`, then its row). Runs
        inside the operation switch_antigravity submits; returns the report's summary."""
        self._own()
        self._not_restoring()
        label = None if label is None else check_label(label)
        found = self.adapter.read()
        self._seen(found)
        if found.state != "ok":
            raise store_refusal(found)
        if self.store.agy_find(found.email) is not None:
            raise DeckError(ALREADY_SAVED, "invalid")
        key = self.save_new(found, label=label)
        return f"Saved {self.store.agy_account(key)['label']}."

    def rename(self, account_id, label) -> None:
        """Renames a saved account (its label only)."""
        self._not_restoring()
        label = check_label(label)
        if not self.store.agy_update(account_id, label=label):
            raise DeckError("That account no longer exists.", "not_found")
        self.engine.bump()

    def reorder(self, ids) -> None:
        """Puts the saved accounts in the order of `ids` (Store.agy_reorder): the given ones first."""
        self._not_restoring()
        if UNSAVED_ID in ids:
            raise DeckError("Save this account first.", "invalid")
        self.store.agy_reorder(ids)
        self.engine.bump()

    def remove(self, account_id) -> dict:
        """Removes a saved account: its `agy-<id>` records, then its row, as an operation ({"op": id}). agy's keychain
        item stays as it is, also when the account removed is the one signed in (it then shows as not saved)."""
        from .switching import QuickOp
        self._own()
        self._not_restoring()
        row = self.store.agy_account(account_id)
        if row is None:
            raise DeckError("That account no longer exists.", "not_found")

        def remove():
            self._not_restoring()
            current = self.store.agy_account(account_id)
            if current is None:
                raise DeckError("That account no longer exists.", "not_found")
            self.vault.remove(agy_key(account_id))
            self.store.agy_remove(account_id)
            diagnostics.log("agy.removed", acct=diagnostics.account_ref(account_id))
            with self.lock:
                signed_in = self.found.email if self.found.state == "ok" else None
                if current["email"] == signed_in and current["usage"]:
                    self.unsaved_usage = {"email": signed_in, "reading": current["usage"], "error": None}
            self.engine.bump()
            if current["email"] == signed_in:
                return f"Removed {current['label']}. agy stays signed in to it on this Mac."
            return f"Removed {current['label']}."
        return self.engine.submit(QuickOp("remove", remove, provider="antigravity", account=account_id,
                                          label=row["label"], message="Removing the account…",
                                          step=("remove", "Removing the account")))

    def accept_notice(self) -> None:
        """The user accepted the notice about Google's terms (shown once before the first Save, Add or Switch):
        `agy_notice_seen` turns on."""
        self._not_restoring()
        self.prefs.set_internal("agy_notice_seen", True)
        self.engine.bump()

    # ---- Add another account / Sign In Again ----
    #
    # An Add takes agy's sign-in out (after saving it), opens agy in a Terminal window and watches the store until a
    # new sign-in lands, which is saved. It ends without one on Cancel, when agy's window closes, after
    # ENROLLMENT_TIMEOUT, or when the window could not be opened (`ending`): then the earlier sign-in goes back, but
    # only once no agy runs (a running agy would sign in to it, or be signed out by it) and the keychain answers.
    # Until then the Add stays open in phase `restoring`, also across a restart (`agy-enrollment` keeps `ending`).
    # A sign-in that lands meanwhile wins: a finished sign-in is never thrown away.
    def begin_enrollment(self, account_id=None) -> str:
        """Add (inside the operation switch_antigravity submits, once no agy runs): keeps the outgoing sign-in as
        Switch does (an unsaved one becomes a saved account), writes `agy-enrollment` (that record and when it
        started), deletes agy's keychain item and opens a Terminal window running agy (procs.open_agy_signin), whose
        record (procs.cli_enrollment_token) joins `agy-enrollment`. `account_id`: Sign In Again for that saved
        account. Refused while AccountDeck's own usage run is under way: that run reads the store before and after,
        and an Add in between would mix two accounts. Returns the report's summary."""
        self._own()
        with self.gate:
            self._not_restoring()
            if self.enrolling:
                raise DeckError("An Antigravity sign-in is open already.", "busy")
            if self.refresh_busy:
                raise DeckError(USAGE_RUNNING, "busy")
            exe = self.adapter.executable()
            if exe is None:
                raise DeckError("Install agy first: the sign-in happens in agy.", "not_installed")
            if account_id is not None and self.store.agy_account(account_id) is None:
                raise DeckError("That account no longer exists.", "not_found")
            if self.agy_running():
                raise DeckError(ADD_WHILE_RUNNING, "busy")
            found = self.adapter.read()
            self._seen(found)
            if found.state == "unknown":
                raise store_refusal(found)
            if found.state == "ok" and not self.adapter.store().fits(found.record):
                # It could not be written back after a cancel: nothing is taken out.
                raise DeckError("AccountDeck could not put this Antigravity sign-in back after an Add, so it leaves "
                                "it alone.", "antigravity")
            facts_mac.secure_directory(self.paths.agy_work)     # agy runs there: a folder of AccountDeck's own
            if found.state == "ok":
                self.keep_outgoing(found)
            now = self.clock()
            doc = {"v": 1, "started": now, "expires": now + ENROLLMENT_TIMEOUT, "account": account_id,
                   "before": found.record if found.state == "ok" else None,
                   "before_email": found.email if found.state == "ok" else None, "window": None, "ending": None}
            self.vault.put(AGY_ENROLLMENT, doc)
            if found.state == "ok":
                try:
                    self.last_look(found, ADD_WHILE_RUNNING)
                except BaseException:       # nothing of agy's was changed: the Add never began
                    self._remove_doc()
                    raise
            try:
                if found.state == "ok":
                    self.adapter.store().delete()
                    after = self.adapter.read()
                    self.noted_write(after)
                    if after.state != "absent":
                        raise DeckError("agy's sign-in could not be taken out for the new one.", "antigravity")
                window = self.engine.procs.open_agy_signin(exe, cwd=self.paths.agy_work)
            except BaseException as exc:
                code = exc.code if isinstance(exc, DeckError) else "system"
                cause = str(exc) if isinstance(exc, DeckError) else "agy's sign-in could not be opened."
                raise DeckError(self._unopened(doc, cause), code) from exc
            doc["window"] = self.engine.procs.cli_enrollment_token(window)
            with self.lock:
                self._enroll, self._enroll_ended = doc, None
                self.enrollment = {"phase": "signing_in", "account": account_id, "started": now,
                                   "expires": doc["expires"], "email": None, "message": SIGNING_IN}
            self._save_doc(doc)
            diagnostics.log("agy.enrollment_started", again=account_id is not None)
        self.engine.bump()
        return "Sign in with Google in the Terminal window AccountDeck opened."

    def _unopened(self, doc, cause) -> str:
        """begin_enrollment failed after it wrote `agy-enrollment`: the Add stays open (and shown) while the earlier
        sign-in goes back, at once when it can. Returns the error text: `cause` and how the Add stands."""
        doc["ending"] = "unopened"
        with self.lock:
            self._enroll, self._enroll_ended = doc, None
        self._save_doc(doc)
        self.noted_write(None)          # the delete may have happened: the next look reads the store
        try:
            shown = self._try_restore(doc, cause=cause)
        except Exception as exc:        # the open Add's watch tries again on the next tick
            diagnostics.log_exception("agy.put_back_failed", exc)
            shown = self._restoring(doc, _wait_write(doc))
        with self.lock:
            still = self._enroll is doc
        return f"{cause} {shown}" if still else shown

    def poll_enrollment(self) -> None:
        """On the tick while an Add is open. While it waits for a sign-in: a new one in agy's store is saved (or
        updates the saved account with its e-mail) and closes the Add, with the new account signed in; the record
        from before the Add still there closes it as it was (a restart came before agy's item was taken out); when
        agy's sign-in window has closed, or ENROLLMENT_TIMEOUT has passed, the Add ends. An Add that ends puts the
        earlier sign-in back once it can (`_try_restore`). An unreadable store is waited out, never written over."""
        if not self.enrolling or not self.engine.owns_login:
            return
        with self.gate:
            doc = self._enroll
            if doc is None:
                return
            if doc.get("ending") is None:
                if self._backed_off():
                    return              # the keychain did not answer a moment ago: asked again after the back-off
                found = self.adapter.read()
                self._seen(found)
                if found.state == "ok":
                    if _is_new(doc, found):
                        self._finish_enrollment(doc, found)
                    else:
                        self._close_unchanged(doc, found)
                    return
                if found.state == "unknown" and found.reason not in TRANSIENT:
                    # A sign-in landed that AccountDeck cannot save (a work or school account, say): it stays.
                    self._end_enrollment(doc, left=found)
                    return
                if self._window_closed(doc):
                    self._begin_ending(doc, "closed")
                elif self.clock() >= doc["expires"]:
                    self._begin_ending(doc, "timed_out")
                else:
                    if found.state == "unknown":
                        self._enrollment_message(f"{found.message} AccountDeck waits until it can read agy's sign-in.")
                    return
            self._try_restore(doc)

    def cancel_enrollment(self) -> str:
        """Cancel an Add. A sign-in that landed already is saved and stays (putting the earlier one over it would
        move the agy session that made it). Otherwise the Add ends: the earlier sign-in goes back as soon as no agy
        runs, which is at once unless agy's sign-in window is still open; until then the Add shows `restoring` and
        the summary says what it waits for. Returns the report's summary."""
        self._own()
        with self.gate:
            doc = self._enroll
            if doc is None:
                raise DeckError("No Antigravity sign-in is open.", "invalid")
            if doc.get("ending") is None:
                self._begin_ending(doc, "cancelled")
            return self._try_restore(doc)

    def _try_restore(self, doc, *, cause=None) -> str:
        """An Add that ends (doc["ending"]) puts the earlier sign-in back, but only while no agy runs and the keychain
        answers; until then it stays open (`restoring`) and the next poll tries again. What agy's store holds
        decides, read first (reading changes nothing a running agy uses): a new sign-in is saved and kept, also after
        a Cancel; the record from before the Add means it is back already; a sign-in AccountDeck cannot read or save
        is left alone; only an empty store gets the earlier record, written once no agy runs and read back. With no
        earlier record there is nothing to write, so the Add just ends. `cause`: the error text that ended the Add
        (begin_enrollment). Returns what the Add shows now."""
        before = doc.get("before")
        if self._backed_off():          # the keychain did not answer a moment ago: asked again after the back-off
            if before is None:
                return self._end_enrollment(doc, cause=cause)
            return self._restoring(doc, _wait_keychain(doc))
        found = self.adapter.read()
        self._seen(found)
        if found.state == "ok":
            if _is_new(doc, found):
                tail = LANDED_DESPITE_CANCEL if doc.get("ending") == "cancelled" else ""
                return self._finish_enrollment(doc, found, tail=tail)
            return self._end_enrollment(doc, back=True, cause=cause)
        if found.state == "unknown" and found.reason not in TRANSIENT:
            return self._end_enrollment(doc, left=found, cause=cause)
        if before is None:
            return self._end_enrollment(doc, cause=cause)
        if found.state == "unknown":
            return self._restoring(doc, _wait_keychain(doc))
        if self.agy_running():          # it could sign in to the record put back, or be signed out by it
            return self._restoring(doc, self._wait_for_agy(doc))
        try:
            self.adapter.store().write(before)
        except DeckError as exc:
            diagnostics.log("agy.put_back_failed", lvl="warning", code=exc.code)
            self.noted_write(None)
            return self._restoring(doc, _wait_keychain(doc) if exc.code == "busy" else _wait_write(doc))
        after = self.adapter.read()
        self.noted_write(after)
        if after.state != "ok" or after.email != doc.get("before_email"):
            return self._restoring(doc, _wait_write(doc))
        return self._end_enrollment(doc, back=True, cause=cause)

    def _finish_enrollment(self, doc, found, *, tail="") -> str:
        """A sign-in landed during an Add: saved, and the Add closed with the new account signed in. `tail` ends the
        message (a Cancel came too)."""
        wanted = self.store.agy_account(doc["account"]) if doc.get("account") else None
        row = self.store.agy_find(found.email)
        if row is None:
            key = self.save_new(found)
            label = self.store.agy_account(key)["label"]
            phase, message = "saved", f"Saved {label}."
        else:
            self.keep(row, found)
            label = row["label"]
            phase, message = "saved", (f"{label} is signed in again." if wanted and wanted["id"] == row["id"]
                                       else f"{label} was saved already; its saved sign-in was updated.")
        if wanted is not None and wanted["email"] != found.email:
            # Sign In Again as someone else: that sign-in is kept too, and the account asked for stays as it was.
            phase = "failed"
            message = (f"You signed in as {found.email}, not {wanted['email']}. That sign-in was saved as {label}; "
                       f"{wanted['label']} still needs a new sign-in.")
        message += tail
        self._remove_doc()
        with self.lock:
            self._enroll, self._enroll_ended = None, self.clock()
            self.enrollment = {"phase": phase, "account": doc.get("account"), "started": doc["started"],
                               "expires": doc["expires"], "email": found.email, "message": message}
        diagnostics.log("agy.enrollment_saved", phase=phase, ending=doc.get("ending"), cancelled=bool(tail))
        self.engine.bump()
        return message

    def _close_unchanged(self, doc, found) -> str:
        """agy's store holds the record from before the Add again (a restart came after `agy-enrollment` was written
        but before agy's item was taken out, say): the Add closes and nothing changes."""
        message = UNCHANGED.format(email=found.email)
        self._remove_doc()
        with self.lock:
            self._enroll, self._enroll_ended = None, self.clock()
            self.enrollment = {"phase": "cancelled", "account": doc.get("account"), "started": doc["started"],
                               "expires": doc["expires"], "email": None, "message": message}
        diagnostics.log("agy.enrollment_ended", phase="cancelled", ending="unchanged")
        self.engine.bump()
        return message

    def _end_enrollment(self, doc, *, back=False, left=None, cause=None) -> str:
        """An Add ends without a sign-in to save: `back` when the earlier sign-in is in agy's store again; `left` (a
        StoreRead) when agy holds a sign-in AccountDeck cannot read or save, which it leaves alone (the earlier one is
        saved, so it can be switched to once agy is signed out of that one). Then `agy-enrollment` goes and the
        state shows how the Add ended for ENDED_SHOWN. `cause`: the error text that ended it, if any."""
        ending = doc.get("ending") or "cancelled"
        if left is not None:
            phase = "failed"
            message = WORK_SIGN_IN if left.reason == "not_consumer" else OTHER_SIGN_IN.format(message=left.message)
            row = self.store.agy_find(doc["before_email"]) if doc.get("before_email") else None
            if row is not None:
                message += GO_BACK.format(label=row["label"])
            if cause:
                message = f"{cause} {message}"
        else:
            phase = {"timed_out": "timed_out", "unopened": "failed"}.get(ending, "cancelled")
            message = (cause or ENDED[ending]) + (" The earlier sign-in is back." if back else "")
        self._remove_doc()
        with self.lock:
            self._enroll, self._enroll_ended = None, self.clock()
            self.enrollment = {"phase": phase, "account": doc.get("account"), "started": doc["started"],
                               "expires": doc["expires"], "email": None, "message": message}
        diagnostics.log("agy.enrollment_ended", phase=phase, ending=ending, left=left is not None)
        self.engine.bump()
        return message

    def _begin_ending(self, doc, ending) -> None:
        """The Add will end without a new sign-in (ENDINGS): kept in `agy-enrollment`, so a restart goes on ending
        it instead of waiting for a sign-in again."""
        doc["ending"] = ending
        self._save_doc(doc)
        diagnostics.log("agy.enrollment_ending", ending=ending)

    def _restoring(self, doc, message) -> str:
        """The Add waits to put the earlier sign-in back: phase `restoring`, its message what it waits for."""
        shown = {"phase": "restoring", "account": doc.get("account"), "started": doc["started"], "expires": None,
                 "email": None, "message": message}
        with self.lock:
            changed = self.enrollment != shown
            self.enrollment, self._enroll_ended = shown, None
        if changed:
            self.engine.bump()
        return message

    def _wait_for_agy(self, doc) -> str:
        if doc.get("ending") == "unopened" or self._window_closed(doc):
            return WAIT_OTHER.format(email=_earlier(doc))
        return _wait_agy(doc)

    def _window_closed(self, doc) -> bool:
        """Whether agy's sign-in window is known to have ended (procs.cli_enrollment_exited). False when it cannot
        tell, or when the Add has no window record."""
        window = doc.get("window")
        if not isinstance(window, dict):
            return False
        try:
            return bool(self.engine.procs.cli_enrollment_exited(window))
        except Exception as exc:        # not known: the Add goes on waiting, and ends at its time limit at the latest
            diagnostics.log("agy.window_unknown", lvl="warning", error=type(exc).__name__)
            return False

    def _enrollment_message(self, message) -> None:
        with self.lock:
            if self.enrollment is not None and self.enrollment.get("message") != message:
                self.enrollment = {**self.enrollment, "message": message}
                changed = True
            else:
                changed = False
        if changed:
            self.engine.bump()

    def _save_doc(self, doc) -> None:
        """`agy-enrollment` again, as it stands now (for a restart). A failure is logged: the Add goes on as it is,
        and a restart resumes the record as last written."""
        try:
            self.vault.put(AGY_ENROLLMENT, doc)
        except DeckError as exc:
            diagnostics.log("agy.enrollment_unsaved", lvl="warning", code=exc.code)

    def _remove_doc(self) -> None:
        self.vault.remove(AGY_ENROLLMENT)

    def restoring_refusal(self):
        """While an Add ends (`restoring`), the refusal of every other Antigravity action: DeckError "busy" saying
        what the Add waits for. None otherwise."""
        with self.lock:
            doc, enrollment = self._enroll, self.enrollment
        if doc is None or doc.get("ending") is None:
            return None
        waiting = (enrollment or {}).get("message") or _wait_agy(doc)
        return DeckError(RESTORING_REFUSAL.format(message=waiting), "busy")

    def _not_restoring(self) -> None:
        refusal = self.restoring_refusal()
        if refusal is not None:
            raise refusal

    # ---- Recover ----
    def recover(self, keep) -> str:
        """Recover a switch left unfinished: puts the record `keep` names (contract.AGY_RECOVERY_CHOICES) in agy's
        keychain item ("before": the one from before the switch, none deleting the item; "after": the target's;
        "keep": what is there now) and closes the `agy-transaction-` record. Before it writes, it reads the store:
        the account to put back there already means nothing is written; another account's sign-in there is saved
        first, as a switch saves its outgoing account; a store it cannot read means it refuses. Returns the report's
        summary."""
        self._own()
        if keep not in contract.AGY_RECOVERY_CHOICES:
            raise DeckError("Choose which sign-in to keep.", "invalid")
        saved, written = None, False
        with self.gate:
            self._not_restoring()
            key = self.recovery_key
            if key is None:
                raise DeckError("No Antigravity switch needs recovering.", "invalid")
            doc = self.vault.get(key)
            if keep != "keep":
                if self.agy_running():
                    raise DeckError(PUT_BACK_WHILE_RUNNING, "busy")
                record, email = (doc.get("before"), doc.get("before_email")) if keep == "before" else (
                    doc.get("after"), doc.get("after_email"))
                found = self.adapter.read()
                self._seen(found)
                if found.state == "unknown":
                    raise DeckError(f"{found.message} {RECOVER_UNREAD}", "antigravity")
                there = found.email if found.state == "ok" else None
                if there != email:
                    if found.state == "ok":       # a sign-in AccountDeck has not saved is never written over
                        saved = self.keep_outgoing(found)
                    self.last_look(found, PUT_BACK_WHILE_RUNNING)
                    written = True
                    try:
                        if record is None:
                            self.adapter.store().delete()
                            after = self.adapter.read()
                            self.noted_write(after)
                            if after.state != "absent":
                                raise DeckError("agy's sign-in could not be taken out.", "antigravity")
                        else:
                            self.adapter.store().write(record)
                            after = self.adapter.read()
                            self.noted_write(after)
                            if after.state != "ok" or after.email != email:
                                raise DeckError("agy's sign-in did not read back as the one put back.", "antigravity")
                    except BaseException:
                        # The item may have changed all the same (a write whose check failed): the state shows what
                        # the store holds at the next look, not the sign-in from before. The transaction stays.
                        self.noted_write(None)
                        raise
            self.vault.remove(key)
            diagnostics.log("agy.recovered", keep=keep, written=written, saved_first=saved is not None)
            self.scan_transactions()
        self.engine.sense_active(only="antigravity")
        if keep == "keep":
            return "agy keeps the sign-in it has now."
        if not written:
            return ("agy already has the sign-in from before the switch. Nothing was changed." if keep == "before"
                    else "agy already has the selected sign-in. Nothing was changed.")
        summary = ("The sign-in from before the switch is back." if keep == "before"
                   else "The selected sign-in is in place.")
        if saved is not None:
            row = self.store.agy_account(saved)
            summary += f" The account agy was signed in to ({row['label'] if row else 'unknown'}) was saved first."
        return summary

    # ---- shared ----
    def last_look(self, found, running) -> None:
        """Right before a Switch, an Add or a Recover changes agy's item. `found` is the read the action decided on;
        reading, saving the outgoing sign-in and writing the action's own record took a moment since. The store must
        still hold that sign-in (a sign-in that landed meanwhile would be lost) and no agy may run (one that started
        meanwhile would be signed out or moved by the write): else DeckError "busy", `running` its text while an
        agy runs, and nothing of agy's was changed. The process check comes last, straight before the write."""
        again = self.adapter.read()
        self._seen(again)
        if not _same_sign_in(found, again):
            raise DeckError(STORE_MOVED, "busy")
        if self.agy_running():
            raise DeckError(running, "busy")

    def agy_running(self) -> bool:
        """Whether any agy process runs (AccountDeck's own usage run aside). One that cannot be told apart counts;
        so does a process table that cannot be read."""
        try:
            return bool(self.engine.procs.agy_consumers())
        except DeckError:
            return True

    def _own(self) -> None:
        if not self.engine.owns_login:
            raise DeckError(READ_ONLY, "read_only")


def _is_new(doc, found) -> bool:
    """Whether the record in agy's store (`found`, ok) is a sign-in made during the Add: there was none before, or
    its e-mail or refresh token differs from the record agy held when the Add began. agy's hourly renewal keeps the
    refresh token, so the earlier record put back (or never taken out) is not new. Compared only, never logged."""
    before = doc.get("before")
    if before is None:
        return True
    return (found.email != doc.get("before_email")
            or _field(before, ("token", "refresh_token")) != _field(found.record, ("token", "refresh_token")))


def holds_record(found, record) -> bool:
    """Whether agy's store as read (`found`, a StoreRead) holds `record`, a sign-in record or None: the same e-mail
    (as agy's record names it) and refresh token. Compared only."""
    if found.state != "ok" or not isinstance(record, dict):
        return False
    return all(_field(found.record, path) == _field(record, path) and _field(record, path) is not None
               for path in (("id_token_email",), ("token", "refresh_token")))


def _same_sign_in(first, again) -> bool:
    """Whether two reads of agy's store (StoreRead) hold the same sign-in: both empty, or the same e-mail and refresh
    token (agy's hourly renewal keeps both). A store that cannot be read is never the same. Compared only."""
    if first.state != again.state or first.state == "unknown":
        return False
    if first.state != "ok":
        return True
    path = ("token", "refresh_token")
    return first.email == again.email and _field(first.record, path) == _field(again.record, path)


def _earlier(doc) -> str:
    return doc.get("before_email") or EARLIER


def _wait_agy(doc) -> str:
    return WAIT_AGY.format(email=_earlier(doc))


def _wait_keychain(doc) -> str:
    return WAIT_KEYCHAIN.format(email=_earlier(doc))


def _wait_write(doc) -> str:
    return WAIT_WRITE.format(email=_earlier(doc))


def _timed_out(exc) -> bool:
    """Whether a keychain error was its time limit (the probe's DeckError carries the keychain's `reason`)."""
    return getattr(exc, "reason", None) == "timeout" or getattr(exc, "code", None) == "timeout"


def _enrollment_doc(doc) -> bool:
    return (isinstance(doc, dict) and _number(doc.get("started")) and _number(doc.get("expires"))
            and (doc.get("before") is None or isinstance(doc["before"], dict))
            and (doc.get("before") is None or isinstance(doc.get("before_email"), str))
            and (doc.get("account") is None or isinstance(doc["account"], str))
            and (doc.get("window") is None or isinstance(doc["window"], dict))
            and (doc.get("ending") is None or doc["ending"] in ENDINGS))


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(value):
    return value if isinstance(value, str) and value else None
