"""Terminal vault ownership, passive capture, consume gate and isolated enrollment."""
import copy
from contextlib import contextmanager, nullcontext
from pathlib import Path
import threading
import time
import uuid

from . import diagnostics, intel
from .cli_locks import credential_locks
from .core import DeckError, OwnerLock, is_frozen
from .providers.claude_cli import expires, fingerprint, login_end, object_file, oauth, plan_of, profile
from .platform.facts_mac import remove_work_directory
from .platform.keychain import KeychainError
from .resets import offer as offer_resets_read
from .usage import claude_usage, has_weekly, timestamp


REFRESH_TIMEOUT = 5.0         # the token POST runs while Claude Code's credential locks are held
# A switch goes ahead on its target's login without a renewal (as it always did) only while the access
# token has this long left: when the token endpoint refused for a reason that says nothing about the login,
# or gave no answer lately.
SWITCH_MARGIN = 600.0
# After a renewal got no answer (or could not connect), none is sent for 15 minutes, doubling while the silence
# lasts, up to an hour: each unanswered renewal leaves a login that must be signed in again (it is never replayed).
QUIET, QUIET_CAP = 900.0, 3600.0
REFUSALS = 2                  # usage reads in a row that refuse the live login (401) before it counts as dead
REFUSAL_GAP = 180.0           # a later refusal counts only this long after the first: a one-minute glitch kills no login
NEWER_END = 86400             # a sign-in whose login ends this much later than another login is the newer one
HOLD_DESCRIPTION = "account_on_hold"
CACHED_AGE = 600.0            # how old a usage answer of Claude Code's (`cached_reading`) may be
# A failed read whose message ("your last reading is kept") a newer reading ends. Others speak of the login.
CACHED_ENDS = frozenset({"network", "timeout", "outage", "blocked", "busy", "rate_limited"})
REAUTH = "Sign in again to repair this terminal account."
UNVERIFIABLE = "Sign in again so AccountDeck can confirm which account this saved login belongs to."
MISFILED = "This saved login belongs to a different Claude account. Sign in again with the right account."
ON_HOLD = "Claude has put this account on hold, so AccountDeck won't switch to it. Details or appeal: claude.ai/restricted."
NO_ANSWER = "Claude's sign-in service did not answer a moment ago, so this login is not renewed yet. AccountDeck tries again later."
REFUSED_ONCE = "Claude refused this terminal login. AccountDeck checks once more before asking you to sign in again."
REFUSED = "Claude refused this terminal login twice. Sign in again to repair this terminal account."
LIVE_REFUSED = "Claude refused the login your terminals hold. The other login saved for this account is kept."
LIVE_LOST = "Your terminals lost this account's login. AccountDeck is giving them the login saved here."
RECOVERING = "An interrupted switch is not settled yet, so this terminal login is not renewed yet. Its last reading is kept."
# A /login caught half way (its token is written before the account's name), or one that ended there.
DISAGREE = ("Claude Code's account and login disagree. If a sign-in is running in a terminal, let it finish; "
            "if not, run /login in a terminal again.")
LIVE_RECORD = "cli-live"      # the vault record of `_live`


class TargetError(DeckError):
    """A failure that says something about one login or its record, not about this Mac: an automatic switch
    that fails with it backs that account off, and the others can still be tried (cli_auto.switch_settled)."""


def login_ends(flags):
    """When the login that counts for the owner ends, from a terminal row's flags (unix seconds, or None): a
    sign-in Claude Code has not got yet (`save`) outlasts the login it holds, which `login_ends_at` tells the
    auto-switch policy while the terminals are on it."""
    return flags.get("signed_in_end") or flags.get("login_ends_at")


def unheld(record):
    """True when the vault record holds a login from a sign-in (Add terminal account) that Claude Code has not
    held yet: `save` names it (`signed_in`); Claude Code getting it (`_handed_over`) or a renewal ends that."""
    return bool(record) and record.get("signed_in") is not None and record["signed_in"] == record.get("fingerprint")


def older(incoming, stored):
    """True when the login `incoming` must not replace `stored`, a vault record whose login can still be used
    (ok or on hold): it is another login whose access token expires sooner, or `stored` is a sign-in Claude Code
    has not held (`unheld`) and `incoming` ends more than NEWER_END before it or has no known end (Claude Code
    renews its own login after a sign-in, so only the end tells which login is older). A refresh token is single use, so of two logins
    of one account the older was renewed past and only the newer works (the freshness rule Codex saves follow,
    core.Vault.put_guarded). A login found dead may be replaced by any other. The end never protects a login
    Claude Code holds or held: its renewal may carry an earlier end than the 30 days it assumes at sign-in."""
    if not stored or stored.get("health") not in ("ok", "on_hold") or not stored.get("credentials"):
        return False
    try:
        if incoming["fingerprint"] == stored.get("fingerprint"):
            return False
        if expires(incoming["credentials"]) < expires(stored["credentials"]):
            return True
    except DeckError:            # a stored login without tokens protects nothing
        return False
    ends, end = login_end(stored["credentials"]), login_end(incoming["credentials"])
    return unheld(stored) and ends is not None and (end is None or ends - end > NEWER_END)


def with_end(flags, credentials):
    """`flags` (a copy) with the end of the login in `credentials` as `login_ends_at` (unix seconds),
    or without it when that end is unknown."""
    flags, end = dict(flags), login_end(credentials)
    if end is None:
        flags.pop("login_ends_at", None)
    else:
        flags["login_ends_at"] = end
    return flags


def renewed(flags, credentials):
    """`with_end` for a login that replaced the saved one: no sign-in waits for Claude Code any more."""
    flags = with_end(flags, credentials)
    flags.pop("signed_in_end", None)
    return flags


class CLIAccounts:
    """Locks: `mutex` covers cross-account work (the live files, capture, the cross-check, enrollment and
    a switch); `key_lock(key)` and the per-account file lock (`gate`) cover one record's vault/store
    changes and its token consumption. Every path takes them in one order: the scheduler's account lock
    (a read), the mutex, key locks (more than one only under the mutex: a switch holds its target's and the
    outgoing account's), the file lock, then Claude Code's locks (cli_locks: its credential locks, then
    `.claude.json`'s). Nothing holding a key lock takes the mutex, and nothing holding Claude Code's locks
    waits for a key lock (`crosscheck(drop=False)` refuses instead). No network call runs under the mutex
    except a switch's own, a conflict check and a finished sign-in's."""

    def __init__(self, engine, adapter):
        self.engine, self.adapter = engine, adapter
        self.paths, self.store, self.vault = engine.paths, engine.store, engine.vault
        self.clock = engine.clock
        self.mutex = threading.RLock()
        self._keys, self._keys_lock = {}, threading.Lock()
        self.enrollment = None
        self.phase = {"phase": "idle", "message": ""}
        self.lock_timeout = 9.0
        # Which account each token really belongs to (fingerprint -> accountUuid), and when an
        # unanswered question may be asked again. `.claude.json` names the account separately from
        # the token and can lag it, so the provider is asked once per new token.
        self.owners, self.owner_retry = {}, {}
        self._deferred = None        # (fingerprint, reason) of the capture deferral last logged
        # The token endpoint's silence (`_refresh`): no renewal is sent before `until`; `count` silences in a row.
        self.quiet = {"until": 0.0, "count": 0}
        self._refusals = {}          # account id -> (fingerprint, refused (401) usage reads of that live login in a row, first at)
        self._kept = {}              # account id -> fingerprint of the older login last kept out of its record
        # (account id, fingerprint) of the live login `mirror` last saved, kept out or matched; a restart keeps it.
        self._live = self._held_login()
        self.repair_wanted = None    # (account id, `_repair_due`'s reason) when `mirror` last found the saved login should go in
        # Recover's work done by itself (switch_cli.auto_recover): when next, tries in a row, what stops it now.
        self.recovery_wait = {"next": 0.0, "count": 0, "problem": None, "needs_you": False, "told": False}

    def _held_login(self):
        try:
            saved = self.vault.get(LIVE_RECORD) if self.vault.path(LIVE_RECORD).exists() else {}
        except DeckError:
            saved = {}
        return (saved["id"], saved["fingerprint"]) if saved.get("id") and saved.get("fingerprint") else None

    def _note_live(self, key, fp):
        """Claude Code holds login `fp` of account `key`: what `_repair_due` compares once it has lost it."""
        if self._live != (key, fp):
            self._live = (key, fp)
            self.vault.put(LIVE_RECORD, {"id": key, "fingerprint": fp}, generation=False)

    def key_lock(self, key):
        with self._keys_lock:
            return self._keys.setdefault(key, threading.RLock())

    def owner(self, credentials):
        """The accountUuid this token belongs to, or None when the provider cannot say right now."""
        fp = fingerprint(credentials)
        if fp in self.owners:
            return self.owners[fp]
        if self.clock() < self.owner_retry.get(fp, 0):
            return None
        try:
            found = self.adapter.resolve_account(credentials, timeout=5).get("accountUuid")
        except DeckError:
            found = None
        if not isinstance(found, str) or not found:
            self.owner_retry[fp] = self.clock() + 60
            return None
        if len(self.owners) >= 64:
            self.owners.clear()
        self.owners[fp] = found
        self.owner_retry.pop(fp, None)
        return found

    def confirm(self, key, saved, *, active):
        """False when the login filed under this account belongs to another one. An inactive account
        then loses that login (`_drop_foreign`), so its readings and switches never use it."""
        if saved.get("verified") == saved["fingerprint"]:
            return True
        found = self.owner(saved["credentials"])
        if found is None:            # the provider could not answer; the read-only usage GET decides
            return True
        if found == saved["identity"]:
            if not active and self.engine.owns_login:
                with self.gate(key):
                    self._mark_verified(key, saved["fingerprint"])
            return True
        diagnostics.log("cli.identity_mismatch", lvl="warning", acct=diagnostics.account_ref(key), active=active)
        if not active and self.engine.owns_login:
            with self.gate(key):
                self._drop_foreign(key, saved["fingerprint"])
        return False

    def verified(self, key, saved):
        """`saved` once the provider confirmed it is this account's login; raises otherwise. A switch
        swaps in only a confirmed login (Claude Code would otherwise renew someone else's)."""
        with self.gate(key):
            saved = self._ensure_verified(key, saved, self.clock())
            self._handed_over(key, saved["fingerprint"])
            return saved

    def confirmed(self, key, saved):
        """`verified` for a login that may yet stay out of Claude Code (switch_cli.repair): still `unheld`."""
        with self.gate(key):
            return self._ensure_verified(key, saved, self.clock())

    def target_record(self, key):
        """The saved login a switch wants: a record that cannot be read says something about that account, a
        keychain that cannot be read does not."""
        try:
            return self.vault.get(key)
        except KeychainError:
            raise
        except DeckError as exc:
            raise TargetError(str(exc), exc.code) from None

    def _handed_over(self, key, fp):
        """Claude Code gets, or holds, login `fp`: if that is a sign-in, it is no longer one Claude Code has not
        held (`unheld`), as Claude Code renews it from then on."""
        with self.key_lock(key):
            current = self._record(key)
            if unheld(current) and current["fingerprint"] == fp:
                current.pop("signed_in")
                self.vault.put(key, current, generation=False)
            row = self.store.account(key)
            if row is not None and "signed_in_end" in row["flags"]:
                flags = dict(row["flags"])
                flags.pop("signed_in_end")
                self.store.update(key, flags=flags)

    def _repair_due(self, key, saved, live):
        """Why `saved`, the record of the account the terminals use, should replace what Claude Code holds
        (`live`, None when it holds no login), or None. "lost": Claude Code lost its login and the saved one is
        another: a sign-in, or a newer login kept over the one it had (`_live`). While it holds one, only a sign-in
        it has not held (`unheld`) replaces it, as any other saved login is the one it holds or one it renewed past
        (spent): "refused", Claude refused the one it holds REFUSALS times in a row (`_refusal`), or "older", what
        it holds is `older`. A login that is the same, or newer, stays. What it says about a login nobody has
        confirmed yet is settled by switch_cli.repair, which asks before anything goes in."""
        if saved is None or saved.get("health") != "ok" or not saved.get("credentials"):
            return None
        fp = saved.get("fingerprint")
        if live is None:
            lost = self._live[1] if self._live and self._live[0] == key else None
            return "lost" if unheld(saved) or lost not in (None, fp) else None
        if live["fingerprint"] == fp or not unheld(saved):
            return None
        refused = self._refusals.get(key)
        if refused is not None and refused[0] == live["fingerprint"] and refused[1] >= REFUSALS:
            return "refused"
        return "older" if older(live, saved) else None

    def repaired(self, key, saved):
        """The saved login went in (switch_cli.repair): the row says so at once, without waiting for a read."""
        fp, credentials = saved["fingerprint"], saved["credentials"]
        with self.key_lock(key):
            self._handed_over(key, fp)
            self._note_live(key, fp)
            self._refusals.pop(key, None)
            self.repair_wanted = None
            flags = renewed(dict(self.row(key)["flags"], health="ok", restorable=True, live_expired=False), credentials)
            self.store.update(key, flags=flags, credential_state="fresh", error=None, error_code=None,
                              expires_at=expires(credentials))
        self.engine.bump()

    def _ensure_verified(self, key, saved, now):
        """Caller holds `gate(key)`. Confirms ownership before a token is consumed or swapped in."""
        fp = saved["fingerprint"]
        if saved.get("verified") == fp:
            return saved
        if expires(saved["credentials"]) > now + 5:       # the profile endpoint still accepts it
            found = self.owner(saved["credentials"])
            if found == saved["identity"]:
                self._mark_verified(key, fp)
                return {**saved, "verified": fp}
            if found is not None:
                diagnostics.log("cli.identity_mismatch", lvl="warning", acct=diagnostics.account_ref(key),
                                active=False, via="consume")
                self._drop_foreign(key, fp)
                raise TargetError(MISFILED, "identity")
            diagnostics.log("cli.refresh", acct=diagnostics.account_ref(key), outcome="unverified")
            raise TargetError("AccountDeck could not confirm this terminal login yet. Its last reading is kept.", "network")
        # Expired and never confirmed: renewing could rotate another account's token family (even
        # the one Claude Code is using), and there is no way to ask whose it is. Fail closed.
        diagnostics.log("cli.refresh", lvl="warning", acct=diagnostics.account_ref(key), outcome="unverifiable")
        current = self.vault.get(key)
        if current.get("fingerprint") == fp:
            current["health"] = "needs_signin"
            self.vault.put(key, current)
            row = self.row(key)
            self.store.update(key, flags=dict(row["flags"], health="needs_signin", restorable=False, checked=now),
                              credential_state="reauth_required", error_code="reauth_required", error=UNVERIFIABLE)
            self.engine.bump()
        raise DeckError(UNVERIFIABLE, "reauth_required")

    def _mark_verified(self, key, fp):
        current = self.vault.get(key)
        if current.get("fingerprint") == fp and current.get("verified") != fp:
            current["verified"] = fp
            self.vault.put(key, current, generation=False)

    def _drop_foreign(self, key, fp):
        """Caller holds `gate(key)`. Removes another account's login from this record (identity, label
        and settings stay) without keeping it in an older generation, so it is never renewed or
        swapped in; the account needs sign-in again."""
        current = self.vault.get(key)
        if current.get("fingerprint") != fp:      # replaced since it was checked; the new one is unchecked
            return
        current.update(credentials=None, fingerprint=None, health="needs_signin")
        current.pop("verified", None)
        self.vault.put(key, current, generation=False)
        self.vault.remove(self._stash_key(key))
        row = self.row(key)
        self.store.update(key, flags=dict(row["flags"], health="needs_signin", restorable=False, checked=self.clock()),
                          credential_state="reauth_required", error_code="identity", error=MISFILED, expires_at=None)
        diagnostics.log("cli.login_dropped", lvl="warning", acct=diagnostics.account_ref(key))
        self.engine.bump()

    def guard(self):
        self.adapter.guard()
        if not self.engine.owns_login:
            raise DeckError("Another AccountDeck 2 is managing your sign-ins.", "read_only")

    def row(self, key):
        row = self.store.account(key)
        if not row or row["provider"] != "claude_cli":
            raise DeckError("That terminal account no longer exists.", "not_found")
        return row

    def require_local(self, key):
        if is_frozen(self.row(key)):
            raise DeckError("This account's sign-in belongs to another computer, so it is not used here.", "read_only")

    @contextmanager
    def gate(self, key):
        self.guard()
        self.row(key)
        with self.key_lock(key):
            self.require_local(key)
            lock = OwnerLock(self.paths.data_dir / "cli-locks" / (key + ".lock"))
            if not lock.acquire():
                raise DeckError("This terminal login is being renewed. Try again shortly.", "busy")
            try:
                yield
            finally:
                lock.release()

    def _record(self, key):
        """The vault record, or None when it is missing or unreadable."""
        try:
            return self.vault.get(key)
        except DeckError:
            return None

    def crosscheck(self, live, *, drop=True):
        """Refuses a live login whose token is saved under another account. Records without a usable
        login (needs sign-in, dropped or unreadable) never block. When an unconfirmed record holds the
        live token, the provider decides: if the token is the live account's, that record loses it.
        Without `drop` (a caller holding Claude Code's locks, which come after every key lock) such a
        record is not taken from, and the live login is refused instead."""
        with self.mutex:
            for row in self.store.accounts():
                if is_frozen(row) or row["provider"] != "claude_cli" or row["identity"] == live["identity"]:
                    continue
                saved = self._record(row["id"])
                if not saved or saved.get("health", "ok") != "ok" or not saved.get("credentials"):
                    continue
                if saved.get("fingerprint") != live["fingerprint"]:
                    continue
                if (drop and saved.get("verified") != saved["fingerprint"] and self.engine.owns_login
                        and self.owner(live["credentials"]) == live["identity"]):
                    diagnostics.log("cli.identity_mismatch", lvl="warning", acct=diagnostics.account_ref(row["id"]),
                                    active=False, via="crosscheck")
                    with self.gate(row["id"]):
                        self._drop_foreign(row["id"], saved["fingerprint"])
                    continue
                raise DeckError(DISAGREE, "identity")
        return live

    def save(self, incoming, *, source="capture", drop=True):
        """Saves a login as its account's record (a new account for a new identity) and returns the id.
        An older login never replaces a newer one (`older`) except from a sign-in (source "enrollment"):
        the record is then kept as it is. A sign-in is saved as one Claude Code has not held (`unheld`); any
        other save is of a login Claude Code holds or held. `drop` is the cross-check's (`crosscheck`)."""
        self.guard()
        with self.mutex:
            old = self.store.find("claude_cli", incoming["identity"])
            if is_frozen(old):
                self.engine.notice("This terminal account's sign-in belongs to another computer, so it was not saved here.", "warning")
                self.require_local(old["id"])
            incoming = self.crosscheck(copy.deepcopy(incoming), drop=drop)
            with self.key_lock(old["id"]) if old else nullcontext():
                if old:
                    self.require_local(old["id"])
                previous = self._record(old["id"]) if old else None
                if source != "enrollment" and older(incoming, previous):
                    self._keep(old["id"], incoming, source)
                    return old["id"]
                if source == "enrollment":
                    incoming["signed_in"] = incoming["fingerprint"]
                if old:
                    incoming["label"] = old["label"]
                    if previous:
                        incoming["excluded"] = previous.get("excluded", False)
                        if incoming["fingerprint"] == previous.get("fingerprint"):
                            incoming["refreshed"] = previous.get("refreshed", incoming["refreshed"])
                            if previous.get("verified") == incoming["fingerprint"]:
                                incoming.setdefault("verified", incoming["fingerprint"])
                        if (previous.get("health") in ("needs_signin", "on_hold") and source != "enrollment"
                                and incoming["fingerprint"] in (previous.get("fingerprint"), previous.get("refused"))):
                            # A login found dead or on hold stays so when it is saved again (a switch saves the
                            # outgoing login): only another login, or a sign-in, starts it over.
                            incoming["health"] = previous["health"]
                            if previous.get("refused"):
                                incoming["refused"] = previous["refused"]
                else:
                    incoming["label"] = incoming["email"].partition("@")[0] or "Terminal account"
                key = old["id"] if old else self.store.save_account("claude_cli", incoming["identity"], incoming["label"],
                          incoming["email"], incoming["plan"], incoming["organizationUuid"])
                try:
                    digest = self.vault.put(key, incoming)
                except BaseException:
                    if not old:
                        self.store.remove(key)
                    raise
                self.store.save_account("claude_cli", incoming["identity"], incoming["label"], incoming["email"],
                                        incoming["plan"], incoming["organizationUuid"])
                health = incoming["health"]
                flags = renewed(self.row(key)["flags"], incoming["credentials"])
                flags.update(restorable=health == "ok", health=health, excluded=incoming["excluded"], live_expired=False)
                if source == "enrollment" and flags.get("login_ends_at"):
                    flags["signed_in_end"] = flags["login_ends_at"]
                # A login kept dead keeps asking for sign-in; one kept on hold keeps saying so.
                fields = ({"credential_state": "fresh", "error": None, "error_code": None} if health == "ok"
                          else {"credential_state": "reauth_required"} if health == "needs_signin" else {})
                self.store.update(key, flags=flags, expires_at=expires(incoming["credentials"]), **fields)
                self.store.record_generation(key, digest, source=source, health=incoming["health"])
                self._kept.pop(key, None)
            self.engine.bump()
            return key

    def _keep(self, key, incoming, source):
        """Logs, once per login, that an older login was not saved over the account's newer one (`older`)."""
        if self._kept.get(key) != incoming["fingerprint"]:
            self._kept[key] = incoming["fingerprint"]
            diagnostics.log("cli.login_kept", lvl="warning", acct=diagnostics.account_ref(key), source=source)

    def _prefetch_owner(self):
        """Asks the provider about a new live token before `mirror` takes the mutex, so that network
        call never blocks the terminal reads; the locked pass then answers from `owners`."""
        if not self.engine.owns_login:
            return
        try:
            live = self.adapter.current()
        except DeckError:
            return
        fp = live["fingerprint"]
        if fp in self.owners or self.clock() < self.owner_retry.get(fp, 0):
            return
        old = self.store.find("claude_cli", live["identity"])
        if is_frozen(old):
            return
        previous = self._record(old["id"]) if old else None
        if previous and previous.get("fingerprint") == fp:
            return
        self.owner(live["credentials"])

    def mirror(self):
        self.adapter.guard()
        self._prefetch_owner()
        with self.mutex:
            self.repair_wanted = None
            try:
                live = self.adapter.current()
                if is_frozen(self.store.find("claude_cli", live["identity"])):
                    return live["identity"]
                live = self.crosscheck(live)
            except DeckError as exc:
                if exc.code != "login_needed":
                    raise
                account = object_file(self.paths.claude_user_config, missing=True).get("oauthAccount") or {}
                known = self.store.find("claude_cli", account.get("accountUuid"))
                if known and not is_frozen(known) and self.engine.owns_login:
                    with self.key_lock(known["id"]):
                        saved = self._record(known["id"])
                        # Claude Code lost its login (it clears one whose renewal was refused). The row says so, which
                        # moves the terminals off unless the login saved here goes in first (switch_cli.repair), but
                        # the record is dead only while the vault holds that login: a sign-in Claude Code never had,
                        # or a newer login kept over the one it had (`older`), stays usable, asks no sign-in, and the
                        # account's first read once the terminals have left clears the row.
                        newer = self._repair_due(known["id"], saved, None) is not None
                        if not newer and saved is not None and saved.get("health") != "needs_signin":
                            saved["health"] = "needs_signin"
                            self.vault.put(known["id"], saved)
                    flags = dict(known["flags"], health="needs_signin", restorable=False, live_expired=False)
                    if newer:
                        self.repair_wanted = (known["id"], "lost")
                        if flags != known["flags"] or known["error"] != LIVE_LOST:
                            self.store.update(known["id"], flags=flags, error=LIVE_LOST, error_code="reauth_required")
                    else:
                        # Nothing newer is saved (any more): the row asks for a sign-in, and no longer says one goes in.
                        stale = known["error"] in (LIVE_LOST, LIVE_REFUSED)
                        if flags != known["flags"] or known["credential_state"] != "reauth_required" or stale:
                            self.store.update(known["id"], flags=flags, credential_state="reauth_required",
                                              **({"error": REAUTH, "error_code": "reauth_required"} if stale else {}))
                return known["identity"] if known else None
            old = self.store.find("claude_cli", live["identity"])
            if is_frozen(old):
                return live["identity"]
            if self.engine.owns_login:
                previous = self._record(old["id"]) if old else None
                due = self._repair_due(old["id"], previous, live) if old else None
                if due or older(live, previous):
                    # Claude Code holds an older login than the one saved (put back from before a renewal, say), or
                    # one Claude refused: the other stays saved, as `save` would keep it, and goes in when it is due.
                    self._keep(old["id"], live, "mirror")
                    self._note_live(old["id"], live["fingerprint"])
                    self.repair_wanted = (old["id"], due) if due else None
                elif not previous or any(previous.get(k) != live.get(k) for k in
                                         ("fingerprint", "credentials", "oauthAccount")):
                    # A new token is saved only once the provider confirms whose it is: Claude Code
                    # writes the token and the account name to different files at different times.
                    unchanged = bool(previous) and previous.get("fingerprint") == live["fingerprint"]
                    found = None if unchanged else self.owner(live["credentials"])
                    if unchanged or found == live["identity"]:
                        if found == live["identity"]:
                            live["verified"] = live["fingerprint"]
                        key = self.save(live, source="mirror")
                        self._deferred = None
                        self._note_live(key, live["fingerprint"])
                        diagnostics.log("cli.mirror_saved", acct=diagnostics.account_ref(key),
                                        reason="new" if not old else "updated" if unchanged else "rotated")
                        if not old:
                            self.engine.notice(f"Saved your current terminal login as {live['email']}.", "good")
                    else:
                        reason = "owner_unknown" if found is None else "mismatch"
                        if self._deferred != (live["fingerprint"], reason):
                            self._deferred = (live["fingerprint"], reason)
                            diagnostics.log("cli.capture_deferred", lvl="warning" if found else "info", reason=reason,
                                            acct=diagnostics.account_ref(old["id"]) if old else None)
                else:
                    self._note_live(old["id"], live["fingerprint"])
                    if unheld(previous):        # it got the sign-in with no switch saying so (restore_login, a cut repair)
                        self._handed_over(old["id"], live["fingerprint"])
                    if old["error"] in (LIVE_LOST, LIVE_REFUSED) and previous.get("health") == "ok":
                        self.repaired(old["id"], previous)       # a repair cut short never said it went in
                row = self.store.find("claude_cli", live["identity"])
                if row is None:
                    return live["identity"]
                expired = expires(live["credentials"]) <= self.clock() + 60
                if row["flags"].get("live_expired") != expired:
                    self.store.update(row["id"], flags=dict(row["flags"], live_expired=expired))
                    self.engine.bump()
            return live["identity"]

    def _stash_key(self, key):
        return "cli-stash-" + key

    def adopt_stashes(self):
        self.guard()
        with self.mutex:
            for row in self.store.accounts():
                if row["provider"] != "claude_cli" or is_frozen(row):
                    continue
                try:
                    with self.gate(row["id"]):
                        self._adopt(row["id"])
                except DeckError as exc:      # a locked keychain or a damaged stash never stops the start: its next renewal asks again
                    diagnostics.log("cli.adopt_failed", lvl="warning", acct=diagnostics.account_ref(row["id"]), code=exc.code)

    def _adopt(self, key):
        """Settles a renewal a stopped process left (its stash). A damaged stash or record, or a stash of another
        account's, is this account's failure, not this Mac's (TargetError); a locked keychain is this Mac's."""
        self.require_local(key)
        name = self._stash_key(key)
        if not self.vault.path(name).exists():
            return
        try:
            stash = self.vault.get(name)
        except KeychainError:
            raise
        except DeckError as exc:
            raise TargetError(str(exc), exc.code) from None
        saved = self.target_record(key)
        if saved.get("fingerprint") == stash["before"]:
            if stash.get("result"):
                if stash["result"]["identity"] != saved["identity"]:
                    raise TargetError("A terminal recovery record has a different identity. It was preserved.", "recovery")
                saved = stash["result"]
            else:
                # The process may have died after consuming but before saving the answer.
                # Never replay an uncertain single-use token.
                saved["health"] = "needs_signin"
            digest = self.vault.put(key, saved)
            flags = renewed(dict(self.row(key)["flags"], health=saved["health"], restorable=saved["health"] == "ok"),
                            saved["credentials"])
            self.store.update(key, flags=flags, credential_state="fresh" if saved["health"] == "ok" else "reauth_required")
            self.store.record_generation(key, digest, source="renewal", health=saved["health"])
        self.vault.remove(name)

    def refresh(self, key, *, for_switch=False):
        return self._refresh(key, for_switch=for_switch)[0]

    def _refresh(self, key, *, for_switch=False):
        """(saved, renewed now). A token is consumed only once its owner is confirmed (`verified`):
        directly, or because it was renewed from a confirmed one (the same token family).

        A switch renews its target every time, so a dead login fails the switch (it needs sign-in) before
        it reaches Claude Code. It goes ahead on the login as it is only where a renewal says nothing about
        the login and the access token has SWITCH_MARGIN left: the endpoint refused for another reason, or
        could not be reached at all (nothing was consumed), or a renewal got no answer lately (none is sent
        then; see QUIET, which is checked again once Claude Code's locks are held). An account
        on hold is renewed when due, so a renewal that works ends the hold.

        While an interrupted terminal switch's record is open, the two accounts it concerns are not renewed
        (engine.login_held): the one it replaced, whose login Recover may put back or Claude Code may hold again
        (renewing it would leave only a spent one), and the one it gave Claude Code. Nor is the outgoing account
        of a switch in progress: switch_cli.perform holds its key lock until the switch commits or rolls back.
        An open Add terminal account window holds no renewal (begin_enrollment)."""
        with self.gate(key):
            self._adopt(key)
            saved = self.target_record(key)
            if saved["identity"] != self.row(key)["identity"]:
                raise TargetError("The saved terminal login has a different identity.", "identity")
            if saved["health"] not in ("ok", "on_hold") or not saved.get("credentials"):
                raise DeckError(REAUTH, "reauth_required")
            if for_switch and saved["health"] == "on_hold":
                raise TargetError(ON_HOLD, "unsupported")
            now = self.clock()
            left = expires(saved["credentials"]) - now
            if not for_switch and left > 60 and now - saved.get("refreshed", 0) < 7 * 86400:
                return saved, False
            if not for_switch and now - saved.get("refresh_attempt", -1e20) < 1800:
                raise DeckError("This terminal login was checked recently. Its last reading is kept.", "busy")
            if self.engine.login_held(saved["identity"]):
                raise DeckError(RECOVERING, "busy")
            if self._quiet(key, saved, for_switch):
                return saved, False
            saved = self._ensure_verified(key, saved, now)
            # Also hold CC's locks across the live comparison and consumption. This
            # closes the race with a concurrent CLI refresh or another file swap.
            with credential_locks(self.paths, timeout=self.lock_timeout) as check:
                live_credentials = self.adapter.store.read(missing=True)
                live_account = object_file(self.paths.claude_user_config, missing=True).get("oauthAccount") or {}
                try:
                    live_fingerprint = fingerprint(live_credentials)
                except DeckError:
                    live_fingerprint = None
                if (live_account.get("accountUuid") == saved["identity"] or
                        live_fingerprint == saved["fingerprint"]):
                    raise DeckError("Claude Code owns this active login; AccountDeck will not renew it.", "busy")
                # Asked again: a renewal that got no answer may have held these locks while this one waited.
                if self._quiet(key, saved, for_switch):
                    return saved, False
                check()
                saved["refresh_attempt"] = now
                self.vault.put(key, saved)
                stash = {"before": saved["fingerprint"], "result": None}
                self.vault.put(self._stash_key(key), stash, generation=False)
                started = time.perf_counter()
                try:
                    credentials = self.adapter.refresh(saved["credentials"], timeout=REFRESH_TIMEOUT)
                except DeckError as exc:
                    http, cls = getattr(exc, "http", None), getattr(exc, "http_class", None)
                    meta = getattr(exc, "meta", None) or {}
                    # Claude Code's fixed strings tell an expired login from a revoked one or an account on hold.
                    description = meta.get("error_description")
                    outcome = ("on_hold" if http in (400, 401, 403) and description == HOLD_DESCRIPTION
                               else "invalid_grant" if http in (400, 401) and getattr(exc, "detail", None) == "invalid_grant"
                               else "rejected" if http in (400, 401, 403, 429)
                               # The connection never opened (providers.http): the token never left this Mac.
                               else "unsent" if meta.get("unsent") is True else "ambiguous")
                    diagnostics.log("cli.refresh", lvl="warning", acct=diagnostics.account_ref(key), outcome=outcome,
                                    http=cls, ms=round((time.perf_counter() - started) * 1000), for_switch=for_switch,
                                    code=exc.code, description=description)
                    self._heard(now, answered=outcome not in ("ambiguous", "unsent"))
                    if outcome == "invalid_grant":
                        saved["health"] = "needs_signin"
                        self.vault.put(self._stash_key(key), {**stash, "result": saved}, generation=False)
                        self._adopt(key)
                        raise DeckError(REAUTH, "reauth_required") from None
                    # A definite answer, or a request that never left, did not consume the token. Ambiguous
                    # network failures retain the intent and are never automatically replayed.
                    if outcome != "ambiguous":
                        self.vault.remove(self._stash_key(key))
                    if outcome == "on_hold":
                        self._hold(key, saved, now)
                        error = TargetError(ON_HOLD, "unsupported")
                    elif outcome in ("rejected", "unsent") and for_switch and left > SWITCH_MARGIN:
                        return saved, False      # that says nothing about the login, which is unchanged
                    else:
                        # This login cannot be used as it is; one with its margin left may still be switched to.
                        error = TargetError("The terminal login could not be renewed. Its last reading is kept.",
                                            exc.code, exc.retry_after)
                    error.http_class = cls        # a provider answer (or its absence) counts as observed
                    raise error from None
                self._heard(now, answered=True)
                new = fingerprint(credentials)
                saved.update(credentials=credentials, fingerprint=new, refreshed=now, verified=new, health="ok")
                self.vault.put(self._stash_key(key), {**stash, "result": saved}, generation=False)
                digest = self.vault.put(key, saved)       # must succeed before a caller sees the access token
                self.vault.remove(self._stash_key(key))
                row = self.row(key)
                self.store.update(key, flags=renewed(dict(row["flags"], health="ok", restorable=True), credentials))
                self.store.record_generation(key, digest, source="renewal", health="ok")
                diagnostics.log("cli.refresh", acct=diagnostics.account_ref(key), outcome="ok", http="2xx",
                                ms=round((time.perf_counter() - started) * 1000), for_switch=for_switch)
                return saved, True

    def _quiet(self, key, saved, for_switch):
        """While a renewal got no answer moments ago (QUIET), another could leave one more login uncertain:
        True when the login goes on as it is (it has the margin left), NO_ANSWER when it cannot; False when
        a renewal may be sent."""
        now = self.clock()
        if now >= self.quiet["until"]:
            return False
        if expires(saved["credentials"]) - now > (SWITCH_MARGIN if for_switch else 60):
            diagnostics.log("cli.refresh", acct=diagnostics.account_ref(key), outcome="quiet", for_switch=for_switch)
            return True
        raise TargetError(NO_ANSWER, "network")

    def _heard(self, now, *, answered):
        """Records whether the token endpoint answered a renewal: any answer, a refusal too, ends its
        silence; no answer, or no connection at all, holds every renewal for QUIET, doubling while it lasts,
        up to QUIET_CAP."""
        if answered:
            self.quiet = {"until": 0.0, "count": 0}
            return
        count = self.quiet["count"] + 1
        self.quiet = {"until": now + min(QUIET * 2 ** (count - 1), QUIET_CAP), "count": count}
        diagnostics.log("cli.refresh_quiet", lvl="warning", count=count, seconds=round(self.quiet["until"] - now))

    def _hold(self, key, saved, now):
        """Caller holds `gate(key)`. Claude put the account on hold: Claude Code keeps such a login (it shows
        an appeal link) instead of treating it as dead, so no sign-in is asked for. Auto-switch skips it
        (its health is not "ok") until a renewal or a read with it works again."""
        if saved.get("health") != "on_hold":
            saved["health"] = "on_hold"
            self.vault.put(key, saved)
        row = self.row(key)
        self.store.update(key, flags=dict(row["flags"], health="on_hold", restorable=False, checked=now),
                          error=ON_HOLD, error_code="unsupported")
        self.engine.bump()

    def _plan(self, row, saved):
        """The plan field for a read's row, when the login's own data knows better: so a saved account gets its label
        without a sign-in or a network call. The same account's Claude Desktop row follows (it has no such data)."""
        plan = plan_of(saved["credentials"], saved.get("oauthAccount"))
        if not plan:
            return {}
        desktop = self.store.find("claude", row["identity"])
        if desktop is not None and desktop["plan"] != plan and (not desktop["org"] or not row["org"] or desktop["org"] == row["org"]):
            self.store.update(desktop["id"], plan=plan)
        return {"plan": plan} if plan != row["plan"] else {}

    def _settle_read(self, key, flags, *, credentials=None, **fields):
        """Writes a read's result onto the row as it is now (another thread may have changed its flags),
        with the end of the login the read used (`credentials`) when it has one."""
        with self.mutex:
            row = self.store.account(key)
            if row is None:
                return
            merged = dict(row["flags"], **flags)
            self.store.update(key, flags=merged if credentials is None else with_end(merged, credentials), **fields)

    def cached_reading(self, *, not_before=None):
        """Stores Claude Code's own last usage answer (`.claude.json`'s `cachedUsageUtilization`, written when it
        fetches, at most every 60 s) as a reading of the account in use: no API call, and the file is only read.
        Only an answer naming that account, at most CACHED_AGE old, newer than the account's last good reading and
        fetched after `not_before` (just after a switch, a running session may fetch with its old token and name
        the new account). Anything else is skipped silently. True when one was stored.

        It is numbers and nothing else: no proof that a login works, so health, refusals, a read pause and the
        observed status stay as they are, and so does the message of a read that failed, unless CACHED_ENDS."""
        now = self.clock()
        identity = self.engine.active_identity.get("claude_cli")
        row = self.store.find("claude_cli", identity) if identity else None
        if row is None or is_frozen(row):
            return False
        try:
            self.adapter.guard()
            cached = object_file(self.paths.claude_user_config, missing=True).get("cachedUsageUtilization")
            fetched = timestamp(cached.get("fetchedAtMs"))
            if (cached.get("accountUuid") != identity or fetched is None or not now - CACHED_AGE <= fetched <= now + 60
                    or (not_before is not None and fetched <= not_before)):
                return False
            usage = claude_usage(cached["utilization"], now=fetched)
            if not has_weekly(usage):
                return False
        except (DeckError, AttributeError, KeyError, TypeError, ValueError):
            return False
        with self.store.lock:
            current = self.store.account(row["id"])
            if current is None or fetched <= (current["last_good_at"] or 0):
                return False
            self._observe(row["id"], usage, fetched, current["last_good_usage"])      # clears the row's error
            if current["error_code"] is not None and (current["error_code"] not in CACHED_ENDS
                                                      or current["flags"].get("health", "ok") != "ok"
                                                      or (current["paused_until"] or 0) > now):
                self.store.update(row["id"], error=current["error"], error_code=current["error_code"])
        diagnostics.log("cli.cached_reading", lvl="debug", acct=diagnostics.account_ref(row["id"]), age=round(now - fetched))
        self.engine.bump()
        return True

    def _observe(self, key, usage, at, previous):
        """Stores a reading, and first each refill it shows against `previous`, the account's last good reading, as
        the other apps' reads do (intel.detect_refill): the row says "refilled" and the refill alert comes."""
        for refill in intel.detect_refill(previous, usage):
            self.store.record_refill(key, refill["window"], reset_at=refill["reset_at"],
                                     confirmed_at=refill["confirmed_at"], previous_used=refill["previous_used"])
        self.store.observe(key, usage, at=at)

    def read(self, key):
        lock_for = getattr(self.engine.scheduler, "account_lock", None)
        with lock_for(key) if lock_for is not None else nullcontext():
            if is_frozen(self.store.account(key)):
                return "skipped"
            return self._read(key)

    def _read(self, key):
        """Reads one terminal account's usage. Network calls (profile, renewal, usage) run outside the
        mutex, so the tick's `mirror` never waits for them. The live login is never renewed: usage reads
        that refuse it twice in a row mark it dead (`_refusal`)."""
        started = time.perf_counter()
        row = self.row(key)
        now = self.clock()
        note = {"active": None, "refreshed": False, "http": None, "outcome": "error"}
        try:
            try:
                self.adapter.guard()
                try:
                    live = self.crosscheck(self.adapter.current())
                except DeckError as exc:
                    if exc.code != "login_needed":
                        raise
                    live = None
                active = (live["identity"] if live else self.engine.active_identity.get("claude_cli")) == row["identity"]
                note["active"] = active
                if not active:
                    self._refusals.pop(key, None)        # refusals count while the account is in use
                elif self.engine.owns_login:
                    self._confirm_signin(key, now)
                if active and live is None:
                    note["outcome"] = "skipped"
                    return "skipped"
                saved = live if active else self.vault.get(key)
                if not active and not saved.get("credentials"):
                    raise DeckError(REAUTH, "reauth_required")
                if not active and self.engine.owns_login:
                    # Check before renewing so a wrongly filed login is never consumed.
                    if expires(saved["credentials"]) > now + 60 and not self.confirm(key, saved, active=False):
                        note["outcome"] = "identity"
                        return "identity"
                    saved, note["refreshed"] = self._refresh(key)
                credentials = saved["credentials"]
                expired = expires(credentials) <= now + 60
                flags = {"checked": now, "live_expired": active and expired}
                if not active:
                    flags["health"] = saved["health"]    # the live login's own "ok" undoes no mark until a read works
                if expired:
                    self._settle_read(key, flags, credentials=credentials,
                                      credential_state="access_expired" if active else "expired")
                    note["outcome"] = "skipped"
                    return "skipped"
                if not self.confirm(key, saved, active=active):
                    note["outcome"] = "skipped" if active else "identity"
                    return note["outcome"]
                try:
                    data = self.adapter.usage(credentials)
                except DeckError as exc:
                    raise self._refusal(key, saved, exc) if active else exc
                note["http"] = "2xx"
                # The login just worked: the engine may read its limit resets with it (resets.py).
                offer_resets_read(lambda: self.adapter.read_resets(credentials))
                self._refusals.pop(key, None)
                if saved["health"] == "on_hold" or (active and row["flags"].get("health") == "needs_signin"):
                    # The login works after all: no longer on hold, nor dead by refused reads.
                    if self._cleared(key, saved["fingerprint"]):
                        flags.update(health="ok", restorable=True)
                elif not active and saved["health"] == "ok":
                    flags["restorable"] = True           # a target again, if its row said the terminals lost its login
                if active:
                    flags.setdefault("health", "ok")     # what the terminals hold works
                with self.mutex:
                    current = self.store.account(key)
                    self._observe(key, data, now, current["last_good_usage"] if current else None)
                    self._settle_read(key, flags, credentials=credentials, credential_state="live" if active else "fresh",
                                      last_outcome="2xx", paused_until=None, expires_at=expires(credentials),
                                      **self._plan(row, saved))
                self.engine.observed["claude_cli"].record(key, "2xx", at=now)
                note["outcome"] = "ok"
                return "ok"
            except DeckError as exc:
                # Only static messages escape this boundary (every DeckError's text is written in this code);
                # HTTP bodies and secrets do not. Local refusals (sign in again, checked recently) are not
                # provider failures, so only an answer from the provider, or its absence, feeds the observed outage.
                cls = getattr(exc, "http_class", None)
                note.update(http=cls, outcome=exc.code)
                current = self.store.account(key)
                paused = current["paused_until"] if current else row["paused_until"]
                # The read's own reason, except that an account on hold keeps saying so until it is usable again.
                held = bool(current) and current["flags"].get("health") == "on_hold"
                self._settle_read(key, {"checked": now}, error=ON_HOLD if held else str(exc) or "Terminal usage is unavailable. Your last reading is kept.",
                                  error_code="unsupported" if held else exc.code,
                                  paused_until=now + (exc.retry_after or 900) if exc.code == "rate_limited" else paused)
                if cls is not None:
                    self.engine.observed["claude_cli"].record(key, cls, at=now)
                return exc.code
        finally:
            self.engine.bump()
            diagnostics.log("cli.read", acct=diagnostics.account_ref(key), active=note["active"],
                            outcome=note["outcome"], http=note["http"], refreshed=note["refreshed"],
                            ms=round((time.perf_counter() - started) * 1000))

    def _confirm_signin(self, key, now):
        """Asks whose it is, while its access token lasts, about a sign-in saved for the account in use: no
        read of that account uses it, and an expired login never confirmed can't be renewed (`_ensure_verified`)."""
        saved = self._record(key)
        if unheld(saved) and expires(saved["credentials"]) > now + 60:
            self.confirm(key, saved, active=False)

    def _refusal(self, key, live, exc):
        """The error to raise for a failed usage read of the live login. Each read takes the login fresh
        from Claude Code's keychain and sends only an unexpired access token, so REFUSALS refusals (401)
        in a row of one login mean Claude no longer accepts it: it then needs sign-in,
        which moves the terminals off it. A refusal counts only while that login is the one Claude Code holds:
        it says nothing about a login Claude Code renewed to during the read. A sign-in saved since that Claude Code
        has not held (`unheld`) is never marked for it; that one replaces the refused login instead (`_repair_due`).
        Any other saved login is the refused one or one Claude Code renewed past, spent: the record is marked, and
        never goes back in. Any other outcome, or another login, starts the count over; the live login is never
        renewed here."""
        if getattr(exc, "http", None) != 401:
            self._refusals.pop(key, None)
            return exc
        fp = live["fingerprint"]
        with self.gate(key) if self.engine.owns_login else nullcontext():
            saved = self._record(key)
            try:
                held = fingerprint(self.adapter.store.read(missing=True)) == fp
            except DeckError:
                held = False
            other = unheld(saved) and saved.get("fingerprint") != fp
            last = self._refusals.get(key)
            same = held and last is not None and last[0] == fp
            count = 0 if not held else last[1] + (self.clock() - last[2] >= REFUSAL_GAP) if same else 1
            diagnostics.log("cli.live_refused", lvl="warning", acct=diagnostics.account_ref(key), count=count,
                            held=held, other=other)
            if not held:
                self._refusals.pop(key, None)
                return exc                # Claude Code holds another login now: the next read tells
            self._refusals[key] = (fp, count, last[2] if same else self.clock())
            if other:
                error = DeckError(LIVE_REFUSED, exc.code)
            elif count < REFUSALS or not self.engine.owns_login:
                error = DeckError(REFUSED_ONCE, exc.code)
            else:
                if saved is not None and saved.get("health") == "ok":
                    # `refused` names the login, so a later read that works with it undoes this (`_cleared`).
                    saved.update(health="needs_signin", refused=fp)
                    self.vault.put(key, saved)
                row = self.row(key)
                self.store.update(key, flags=dict(row["flags"], health="needs_signin", restorable=False),
                                  credential_state="reauth_required")
                error = DeckError(REFUSED, "reauth_required")
        error.http_class = getattr(exc, "http_class", None)
        return error

    def _cleared(self, key, fp):
        """True when the saved record was on hold, or dead by refused reads of this same login, and is
        usable again now that a read with login `fp` worked."""
        if not self.engine.owns_login:
            return False
        with self.gate(key):
            saved = self._record(key)
            if saved is None or not ((saved.get("health") == "on_hold" and saved.get("fingerprint") == fp)
                                     or (saved.get("health") == "needs_signin" and saved.get("refused") == fp)):
                return False
            saved["health"] = "ok"
            saved.pop("refused", None)
            self.vault.put(key, saved)
        diagnostics.log("cli.login_usable", acct=diagnostics.account_ref(key))
        return True

    def set_excluded(self, key, excluded):
        if type(excluded) is not bool:
            raise DeckError("Choose whether to leave this account out.", "invalid")
        with self.gate(key):
            saved = self.vault.get(key)
            saved["excluded"] = excluded
            self.vault.put(key, saved)
            self.store.update(key, flags=dict(self.row(key)["flags"], excluded=excluded))

    def rename(self, key, label):
        with self.gate(key):
            self.engine.accounts.rename(key, label)
            saved = self.vault.get(key)
            saved["label"] = self.row(key)["label"]
            self.vault.put(key, saved)

    def begin_enrollment(self):
        """Opens the Add terminal account window: Claude Code with CLAUDE_CONFIG_DIR set to a new private folder,
        so its `.claude.json` stays in that folder, its locks in it or beside it (`<folder>.lock`), and its login
        in that folder's own Keychain item (credstore.service_name), never the live login, `~/.claude.json` or
        their locks. Switches, renewals and auto-switch therefore go on while it is open: they share only the
        vault and the store with it, whose writes take the mutex and key locks, and `poll_enrollment` saves the
        new login once the window closes (a sign-in, so it replaces an older login of the same account: `save`).
        Nor does an interrupted switch stop it: a rollback only ever puts the sign-in back as the account's newer
        login, and it goes into Claude Code once the switch's record is closed (cli_auto._repair)."""
        self.guard()
        with self.mutex:
            if self.enrollment or self.engine.runner.busy:
                raise DeckError("Finish the current terminal operation first.", "busy")
            executable = self.engine.procs.find_claude_cli()
            if executable is None:
                raise DeckError("Install Claude Code first", "not_installed")
            folder = self.paths.cli_enroll / uuid.uuid4().hex
            folder.mkdir(parents=True)
            try:
                process = self.engine.procs.launch_claude_cli(executable, folder, self.paths.home)
            except Exception:
                folder.rmdir()
                raise DeckError("The enrollment terminal could not be opened.", "system") from None
            self.enrollment = {"folder": folder, "process": process, "started": self.clock(), "cancel": False}
            self.vault.put("cli-enrollment", {"folder": str(folder), "process": self.engine.procs.cli_enrollment_token(process),
                           "started": self.clock(), "cancel": False}, generation=False)
            self.phase = {"phase": "signing_in", "message": "Sign in with the account you want to add in the new terminal window. If the browser picks the wrong account, use a private window."}
            self.engine.bump()

    def restore_enrollment(self):
        self.guard()
        if not self.vault.path("cli-enrollment").exists():
            return
        try:
            saved = self.vault.get("cli-enrollment")
        except DeckError as exc:      # a locked keychain never stops the start; the window's login stays in its folder
            diagnostics.log("cli.enrollment_unread", lvl="warning", code=exc.code)
            return
        folder = Path(saved["folder"])
        if folder.resolve().parent != self.paths.cli_enroll.resolve():
            raise DeckError("The saved enrollment folder is outside the private data folder.", "safety")
        self.enrollment = {**saved, "folder": folder}
        self.phase = {"phase": "waiting_exit", "message": "Close the enrollment terminal to finish saving its login."}

    def _prefetch_signin(self):
        """Asks whose the login in the open Add terminal account window is, outside the mutex and while its access
        token lasts, so `poll_enrollment` answers from `owners` once the window closes, however much later: a
        sign-in never confirmed and expired since could never go in (`_ensure_verified`)."""
        session = self.enrollment
        if not session or not self.engine.owns_login:
            return
        try:
            credentials = self.adapter.store.read_enrollment(session["folder"])
            if expires(credentials) > self.clock() + 60:
                self.owner(credentials)
        except DeckError:
            pass

    def poll_enrollment(self, *, cancel=False):
        self._prefetch_signin()
        with self.mutex:
            session = self.enrollment
            if not session:
                return
            self.guard()
            session["cancel"] |= cancel or self.clock() - session["started"] >= 1200
            if cancel:
                saved = self.vault.get("cli-enrollment")
                saved["cancel"] = True
                self.vault.put("cli-enrollment", saved, generation=False)
            folder = session["folder"]
            # Where the enrollment's login lands: the folder's credentials file, or on macOS the keychain
            # item Claude Code scopes to that CLAUDE_CONFIG_DIR (platform/credstore.py).
            store = self.adapter.store
            exited = self.engine.procs.cli_enrollment_exited(session["process"])
            found = None
            try:
                credentials = store.read_enrollment(folder)
                oauth(credentials)
                account = object_file(folder / ".claude.json", missing=True).get("oauthAccount")
                if not account:
                    account = self.adapter.resolve_account(credentials, timeout=5)
                found = profile(credentials, account, self.clock())
            except DeckError:
                pass
            if not exited:
                if session["cancel"]:
                    self.phase = {"phase": "waiting_exit", "message": "Close the enrollment terminal to finish cancelling."}
                elif found:
                    self.phase = {"phase": "waiting_exit", "email": found["email"],
                                  "message": f"Signed in as {found['email']} — close that terminal window to finish"}
                self.engine.bump()
                return
            # This read happened AFTER observed exit. A valid login (including a repaired
            # duplicate on cancel) is saved before any helper files can be discarded.
            if found:
                duplicate = self.store.find("claude_cli", found["identity"]) is not None
                # Asked while its access token is fresh: expired and never confirmed, it could never go in.
                if self.owner(found["credentials"]) == found["identity"]:
                    found["verified"] = found["fingerprint"]
                key = self.save(found, source="enrollment")
                message = "Already saved — its login was refreshed" if duplicate else f"Saved {found['email']}."
                for alert in self.store.alerts():     # a sign-in answers what asked for it: read on, it is a false alarm
                    if alert["kind"] == "credential_expired" and alert["account"] == key:
                        self.store.update_alert(alert["id"], dismissed=True)
                self.engine.scheduler.request(key, self.clock())
                self.phase = {"phase": "saved", "email": found["email"], "message": message}
            elif not session["cancel"]:
                self.phase = {"phase": "failed", "message": "The terminal closed before a complete login was saved. Try adding the account again."}
            else:
                self.phase = {"phase": "cancelled", "message": "Enrollment cancelled."}
            if not found and store.enrollment_pending(folder):
                self.phase = {"phase": "failed", "message": "The enrollment login could not be saved yet. Its private folder is preserved; AccountDeck will retry."}
                self.engine.bump()
                return
            # Refuse recursive cleanup if a reparse point moved the directory out of scope.
            if folder.resolve().parent != self.paths.cli_enroll.resolve() or folder.is_symlink():
                raise DeckError("The enrollment folder moved; it was preserved.", "safety")
            # The login is saved (or there is none): its isolated keychain item goes first, so a failure
            # here keeps the folder that names it for the next poll.
            store.discard_enrollment(folder)
            # Claude Code clones plugin marketplaces with read-only git pack files; a plain rmtree
            # raises PermissionError on those and the enrollment never finishes.
            remove_work_directory(folder, self.paths.cli_enroll)
            self.vault.remove("cli-enrollment")
            self.enrollment = None
            self.engine.bump()
