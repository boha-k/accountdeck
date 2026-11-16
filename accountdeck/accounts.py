"""Saved accounts (PLAN §2.1 AccountService): saving the signed-in account, reading usage, and the
unsaved signed-in account of §9.4.

Every Claude capture is health-gated. A capture taken while Claude runs holds its tokens but not
its cookie database; the account can be read at once and becomes switchable after its next
complete capture (the next switch away from it, or a save while Claude is closed). Codex
captures of known accounts go through `Vault.put_guarded`, so an older refresh token never
replaces a newer one. Usage reads never write a provider file, and the active account is read
with the token its official app holds, which a read cannot rotate. A saved Codex login is renewed
only by `renew_codex`, with an encrypted record around the helper: AccountDeck never asks twice with one
refresh token.
"""
from __future__ import annotations

import contextlib
from dataclasses import replace
import threading
import time

from . import diagnostics, intel
from .contract import ERROR_CODES, PROVIDER_FAILURES, UNSAVED_IDS
from .core import DeckError, FrozenAccountError, is_frozen
from .providers import CredentialState, OutcomeLog
from .providers.codex import fingerprint, freshness, identity_key, refresh_due, token_state
from .resets import offer as offer_resets_read

HEALTH_MESSAGES = {
    "half_login": "Claude has not finished signing in. Open Claude, wait until your chats appear, then save again.",
    "logged_out": "Sign in to Claude Desktop first.",
    "identity_mismatch": "Claude's sign-in data disagree. Open Claude once, then save again.",
    "unreadable": "Claude's saved sign-in could not be read. Open Claude once, then save again.",
    "stale_after_kill": "Claude did not finish writing its settings. Open Claude once, then save again.",
    "sentinel": "Claude has not stored this account's tokens yet. Open Claude once, then save again.",
}
READ_FAILED = "Could not refresh this account. Your last reading is kept."
BUSY_ACCOUNT = "This account is being refreshed. Try again in a moment."
FRESH_MARGIN = 180
ACTIVE_TOUCH = 3600.0       # how often an active account's `last_active_at` is renewed
# The flags a usage read owns. It merges only these into the stored flags, so a save or a rename
# made while the read ran is never overwritten by the read's older copy.
READ_FLAGS = ("checked", "outcomes", "error_since", "session_expires_at", "refresh_attempt", "refresh_failures")
RENEWAL_UNCLEAR = ("A renewal of this Codex login gave no clear answer, so its refresh token may be used up and is "
                   "not sent again. Sign in again to repair this account.")
SIGN_IN_FIRST = "Sign in again before switching to this account."
RENEWAL_HELD = "An interrupted Codex switch is not settled yet, so this login is not renewed yet. Its last reading is kept."
RENEWAL_FINISHING = "A renewal of this Codex login is still finishing. Try again in a moment."


def default_label(email, provider):
    local = (email or "").partition("@")[0].strip()
    return local or ("Claude account" if provider == "claude" else "Codex account")


def restorable(provider, profile) -> bool:
    """Whether a saved profile can be switched to: Claude needs its cookie database too."""
    return provider == "codex" or bool((profile or {}).get("cookies"))


def error_code(exc) -> str:
    kind = getattr(exc, "kind", None)
    if kind in PROVIDER_FAILURES:
        return kind
    code = getattr(exc, "code", None)
    return code if code in ERROR_CODES else "network"


class AccountService:
    """`levels()` gives each provider's status level; `observed` the ObservedStatus per provider.
    `lock_for(account_id)` is the account's lock (the scheduler's): saving or removing an account
    waits up to `lock_wait` seconds for a read of it to finish. `on_change()` is called after the
    saved accounts changed (the engine moves its revision). `held(identity)` is True while a Codex
    login must not be renewed (engine.login_held: Recover may still put it back)."""

    def __init__(self, store, vault, providers, settings, *, clock=time.time, observed=None,
                 levels=lambda: {"claude": "ok", "codex": "ok"}, lock_for=None, lock_wait=30.0, on_change=None,
                 held=lambda identity: False):
        self.store, self.vault, self.providers, self.settings = store, vault, providers, settings
        self.clock, self.observed, self.levels = clock, observed or {}, levels
        self.lock_for, self.lock_wait, self.on_change, self.held = lock_for, lock_wait, on_change, held
        self.unsaved = {"claude": None, "codex": None}
        self._identities, self._series = {}, {}
        self._series_lock = threading.Lock()

    def _changed(self):
        if self.on_change is not None:
            self.on_change()

    @contextlib.contextmanager
    def _locked(self, account_id):
        lock = self.lock_for(account_id) if self.lock_for is not None else None
        if lock is not None and not lock.acquire(timeout=self.lock_wait):
            raise DeckError(BUSY_ACCOUNT, "busy")
        try:
            yield
        finally:
            if lock is not None:
                lock.release()

    # ---- saving ----
    def capture(self, provider, *, label=None, claude_running=True, owner="v2") -> str:
        """Saves the account signed in right now; returns its id. With Claude running only its
        tokens are read (the cookie database is Claude's while it runs)."""
        if provider == "claude":
            claude = self.providers["claude"]
            try:
                profile = claude.current(cookies=not claude_running)
            except DeckError as exc:
                if exc.code != "busy" or claude_running:
                    raise
                profile = claude.current(cookies=False)
            health, reason = claude.diagnose(profile)
            if health != "ok":
                diagnostics.log("capture.refused", lvl="warning", provider="claude", health=health)
                raise DeckError(HEALTH_MESSAGES.get(health, reason or HEALTH_MESSAGES["unreadable"]), "login_needed")
            existing = self.store.find("claude", profile["identity"])
            if is_frozen(existing):
                raise FrozenAccountError(existing)
            if existing is not None and not profile.get("cookies"):
                try:
                    held = self.vault.get(existing["id"])
                except DeckError:
                    held = None
                if held and held.get("key") == profile.get("key") and held.get("cookies"):
                    profile["cookies"] = held["cookies"]
            info = self.identity("claude", profile)
            profile["org"] = info["org"]
        elif provider == "codex":
            codex = self.providers["codex"]
            profile = codex.current()
            info = codex.identity(profile)
        else:
            raise DeckError("Choose Claude or Codex.", "invalid")
        return self.save(provider, profile, info, label=label, source="capture", owner=owner)

    def save(self, provider, profile, info, *, label=None, source="capture", owner="v2") -> str:
        """Stores a captured profile under its identity (a new account or the saved one). An account whose
        sign-in belongs to another computer is never saved over (FrozenAccountError, kept as an event)."""
        existing = self.store.find(provider, info["identity"])
        if is_frozen(existing):
            diagnostics.log("capture.refused", lvl="warning", provider=provider, reason=existing["owner"],
                            acct=diagnostics.account_ref(existing["id"]))
            self.store.event("frozen", f"Did not save {existing['label']}: its sign-in belongs to another computer.")
            raise FrozenAccountError(existing)
        if existing is None:
            key = self.store.save_account(provider, info["identity"], label or default_label(info.get("email"), provider),
                                          info.get("email", ""), info.get("plan", ""), info.get("org", ""), owner=owner)
            try:
                digest = self.vault.put(key, profile)
            except BaseException:
                self.store.remove(key)
                raise
            self.store.record_generation(key, digest, source=source, health="ok" if provider == "claude" else None)
            self.store.update(key, flags={"restorable": restorable(provider, profile)})
            self._carry_over(key, provider, info["identity"])
        else:
            key = existing["id"]
            with self._locked(key):
                self._save_over(existing, provider, profile, info, source)
        with self._series_lock:
            self._series.pop(key, None)
        if self.unsaved.get(provider) and self.unsaved[provider]["identity"] == info["identity"]:
            self.unsaved[provider] = None
        diagnostics.log("capture.saved", provider=provider, acct=diagnostics.account_ref(key),
                        complete=restorable(provider, profile), new=existing is None)
        self._changed()
        return key

    def _carry_over(self, key, provider, identity):
        """A newly saved account keeps the reading its unsaved row showed, so it never goes blank."""
        row = self.unsaved.get(provider)
        if row and row["identity"] == identity and row["usage"]:
            self.store.observe(key, row["usage"], at=row["last_good_at"])
            self.store.update(key, last_outcome=row["last_outcome"] or "2xx")

    def _save_over(self, existing, provider, profile, info, source):
        """Replaces a saved account's profile with a newer capture of the same identity (under its lock)."""
        key = existing["id"]
        if is_frozen(self.store.account(key)):
            raise FrozenAccountError(existing)
        self.store.save_account(provider, info["identity"], existing["label"], info.get("email", ""),
                                info.get("plan", ""), info.get("org", "") or existing["org"] or "")
        if provider == "codex":
            if self.vault.put_guarded(key, profile, "live", identity_key, freshness) == "conflict":
                raise DeckError("The saved Codex sign-in belongs to another account. Nothing was changed.",
                                "identity")
        else:
            digest = self.vault.put(key, profile)
            self.store.record_generation(key, digest, source=source, health="ok")
        flags = dict((self.store.account(key) or existing)["flags"])
        if provider == "claude":
            flags.pop("outcomes", None)     # a new profile starts a new outcome log (A1)
        flags["restorable"] = restorable(provider, profile)
        repaired = {}
        if provider == "codex" and existing["credential_state"] == "reauth_required" and \
                self._renewal(key, self.vault.get(key)) != "spent":
            repaired = {"credential_state": "fresh", "error": None, "error_code": None}     # the sign-in that was asked for
        self.store.update(key, flags=flags, **repaired)

    def identity(self, provider, profile):
        """{identity, email, plan, org} of a profile, asked of the provider once per identity."""
        ident = profile.get("identity") if provider == "claude" else identity_key(profile)
        cached = self._identities.get((provider, ident))
        if cached is None:
            cached = self.providers[provider].identity(profile)
            self._identities[(provider, ident)] = cached
        return dict(cached)

    # ---- removing and renaming ----
    def rename(self, account_id, label):
        label = label.strip() if isinstance(label, str) else ""
        if not 1 <= len(label) <= 80:
            raise DeckError("Use a name of 1 to 80 characters.", "invalid")
        if self.store.account(account_id) is None:
            raise DeckError("That account no longer exists.", "not_found")
        self.store.update(account_id, label=label)
        self._changed()

    def remove(self, account_id):
        account = self.store.account(account_id)
        if account is None:
            raise DeckError("That account no longer exists.", "not_found")
        with self._locked(account_id):
            self.vault.remove(account_id)
            self.vault.remove(self._stash_key(account_id))
            self.store.remove(account_id)
        with self._series_lock:
            self._series.pop(account_id, None)
        self.store.event("remove", f"Removed {account['label']}.")
        diagnostics.log("account.removed", acct=diagnostics.account_ref(account_id), provider=account["provider"])
        self._changed()

    # ---- reading ----
    def read(self, account_id, *, active_identity, read_only=False) -> str:
        with self._locked(account_id):
            return self._read(account_id, active_identity=active_identity, read_only=read_only)

    def _read(self, account_id, *, active_identity, read_only=False) -> str:
        """Reads one account (a saved id or an unsaved row id) and records the outcome; returns
        "ok", "rate_limited", "outage" or another failure kind."""
        if account_id in UNSAVED_IDS.values():
            return self.read_unsaved(account_id.removeprefix("unsaved-"))
        account = self.store.account(account_id)
        if account is None:
            return "not_found"
        if is_frozen(account):       # another computer's sign-in: never read or renewed here
            return "skipped"
        now = self.clock()
        flags = dict(account["flags"])
        flags["checked"] = now
        active = active_identity.get(account["provider"]) == account["identity"]
        try:
            profile = self.vault.get(account_id)
            if account["provider"] == "claude":
                reading, credential = self._read_claude(account, profile, flags, active=active, now=now)
            else:
                reading, credential = self._read_codex(account, profile, flags, active=active, now=now,
                                                       read_only=read_only)
        except DeckError as exc:
            self._failed(account, flags, exc, now)
            return error_code(exc)
        expires_in = flags.pop("expires_in", None)
        fields = {"credential_state": credential, "expires_at": None if expires_in is None else now + expires_in}
        if reading.usage is not None:
            for refill in intel.detect_refill(account["last_good_usage"], reading.usage):
                self.store.record_refill(account_id, refill["window"], reset_at=refill["reset_at"],
                                         confirmed_at=refill["confirmed_at"], previous_used=refill["previous_used"])
            self.store.observe(account_id, reading.usage, at=reading.checked or now)
            flags.pop("error_since", None)
            self.store.update(account_id, flags=self._flags(account_id, flags), last_outcome="2xx",
                              paused_until=None, **fields)
            return "ok"
        if reading.error is None and reading.meta.get("hollow"):   # answered without new numbers
            flags.pop("error_since", None)
            self.store.update(account_id, flags=self._flags(account_id, flags), last_outcome="2xx", error=None,
                              error_code=None, paused_until=None, **fields)
            return "ok"
        self.store.update(account_id, flags=self._flags(account_id, flags), **fields)
        if reading.error is None:        # no request was made (a sign-in to renew, a route skipped)
            return "skipped"
        return self._failed(self.store.account(account_id), flags, reading.error, now,
                            http=reading.outcomes[-1][2] if reading.outcomes else None)

    def _flags(self, account_id, flags):
        """The stored flags with this read's own keys (READ_FLAGS) laid over them."""
        current = dict((self.store.account(account_id) or {"flags": {}})["flags"])
        for key in READ_FLAGS:
            if key in flags:
                current[key] = flags[key]
            else:
                current.pop(key, None)
        return current

    def _failed(self, account, flags, exc, now, http=None):
        code = error_code(exc)
        message = str(exc) if isinstance(exc, DeckError) and str(exc) else READ_FAILED
        if account["error_code"] != code or "error_since" not in flags:
            flags["error_since"] = now
        fields = {"error": message[:300], "error_code": code, "flags": self._flags(account["id"], flags)}
        if http:
            fields["last_outcome"] = http
        if code == "rate_limited":
            fields["paused_until"] = now + float(getattr(exc, "retry_after", None) or 900)
        self.store.update(account["id"], **fields)
        diagnostics.log("refresh.failed", lvl="info", acct=diagnostics.account_ref(account["id"]),
                        provider=account["provider"], code=code)
        return code

    def _incident(self, provider):
        return self.levels().get(provider) in ("degraded", "outage")

    def _record(self, provider, account_id, outcomes, now):
        watcher = self.observed.get(provider)
        if watcher is not None:
            for _route, _kind, klass in outcomes:
                watcher.record(account_id, klass, at=now)

    def _read_claude(self, account, profile, flags, *, active, now):
        claude = self.providers["claude"]
        if active:
            profile = self._live_claude(profile, account["identity"])
        incident = self._incident("claude")
        log = OutcomeLog.from_list(flags.get("outcomes"))
        state, _info = claude.credential_state(profile, active=active, now=now, outcomes=log, incident=incident)
        reading = claude.read_usage(profile, state, now=now, last_good=account["last_good_usage"],
                                    cookie_route=bool(self.settings.get("cookie_route")), incident=incident,
                                    account=account["id"])
        for route, kind, _klass in reading.outcomes:
            log.record(kind, route=route, at=now, incident=incident)
        flags["outcomes"] = log.to_list()
        self._record("claude", account["id"], reading.outcomes, now)
        if reading.usage is not None and reading.source in ("oauth", "cookie"):
            # The sign-in just worked on this route: the engine may read the account's limit resets on it (resets.py).
            route = reading.source
            offer_resets_read(lambda: claude.read_resets(profile, route))
        state, info = claude.credential_state(profile, active=active, now=now, outcomes=log, incident=incident)
        flags["expires_in"] = info["expires_in"]
        flags["session_expires_at"] = claude.sessionkey_expiry(profile)
        return reading, state.value

    def _live_claude(self, profile, identity):
        """The active account's tokens as Claude Desktop holds them now (they may be newer than the
        saved copy); the saved profile when Claude's files cannot be read."""
        try:
            live = self.providers["claude"].capture(config_only=True)
        except DeckError:
            return profile
        if live.get("identity") != identity:
            return profile
        return {**profile, "auth": live["auth"], "key": live["key"], "journal": live["journal"]}

    def _read_codex(self, account, profile, flags, *, active, now, read_only):
        codex, key = self.providers["codex"], account["id"]
        if not read_only:
            profile = self.settle_renewal(account)
        renewal = self._renewal(key, profile)
        # Who Codex holds now, not at the tick's last look: that can be seconds old, or much older while another
        # action runs, and a switch can have just given it this login. One that cannot be told may be any saved login.
        found = codex.active_identity()
        if found.status == "unknown":
            read_only = True
        else:
            active = found.identity == account["identity"]
        mode = "read_only"
        if active:
            try:
                live = codex.current()
            except DeckError:
                live = None
            if live is not None and identity_key(live) == account["identity"]:
                profile = live
        elif not read_only and renewal is None and not self.held(account["identity"]) and refresh_due(
                token_state(profile, now), owner=account["owner"], last_attempt=flags.get("refresh_attempt"),
                now=now, failures=int(flags.get("refresh_failures") or 0)):
            mode = "refresh"
        if mode == "refresh":
            reading = self.renew_codex(account, profile)
            flags["refresh_attempt"] = now
            flags["refresh_failures"] = 0 if reading.meta.get("rotated") else int(flags.get("refresh_failures") or 0) + 1
            profile = self.vault.get(key)
            renewal = self._renewal(key, profile)
        else:
            reading = codex.usage(profile, mode="read_only", account=key)
        self._record("codex", key, reading.outcomes, now)
        remaining = token_state(profile, now).remaining
        flags["expires_in"] = None if remaining is None else round(remaining)
        if active:
            credential = "live"
        elif renewal == "spent":
            credential = "reauth_required"
        elif reading.credential:
            credential = reading.credential
        else:
            credential = "fresh" if remaining is not None and remaining > FRESH_MARGIN else "expired"
        return reading, credential

    # ---- renewing a saved Codex login ----
    @staticmethod
    def _stash_key(key):
        return "codex-stash-" + key

    def renew_codex(self, account, profile):
        """Renews a saved Codex login (`profile`, for a read of an account due or a switch's target), the only
        way one is renewed. As for terminal logins (CLIAccounts._refresh) an encrypted stash exists before a
        helper can spend the refresh token and holds the renewed login before the saved copy changes, so a
        crash or a failed save can be put right (`settle_renewal`). Without a renewed login the token may be
        spent, whatever the helper said (see providers.codex), so the login needs signing in again and
        AccountDeck never asks with that token again; only a helper that was never asked (`unsent`) leaves it
        as it was. Never while `held`: Recover would put back a login whose token this spent."""
        key, name = account["id"], self._stash_key(account["id"])
        with self._locked(key):
            if is_frozen(self.store.account(key)):
                raise FrozenAccountError(account)
            if self.held(account["identity"]):
                raise DeckError(RENEWAL_HELD, "busy")
            if identity_key(profile) != account["identity"]:
                raise DeckError("The saved Codex sign-in has a different identity.", "identity")
            stash = {"before": fingerprint(profile), "result": None, "home": None}

            def prepare(work):
                stash["home"] = work.name
                self.vault.put(name, stash, generation=False)

            def persist(login):
                stash["result"] = login
                self.vault.put(name, stash, generation=False)
                # This login was renewed from the saved one, which is spent: it wins whatever its expiry says.
                return self.vault.put_guarded(key, login, "refresh", identity_key, freshness, previous=profile)

            reading = self.providers["codex"].usage(profile, mode="refresh", owner=account["owner"], persist=persist,
                                                    quarantine=lambda raw: self.vault.quarantine(key, raw), account=key,
                                                    prepare=prepare)
            if reading.meta.get("rotated") or reading.meta.get("unsent"):
                self.vault.remove(name)
                return reading
            self._mark_spent(account, stash["before"])
            if reading.credential == "reauth_required":          # refused for good: its own message says so
                return reading
            return replace(reading, credential="reauth_required", error=DeckError(RENEWAL_UNCLEAR, "reauth_required"))

    def settle_renewal(self, account):
        """Puts right a renewal a crash or a failed save interrupted (its stash is still there) and returns the
        saved login. The renewed login goes in, from the stash or from the helper's folder; without one the token
        may be spent. A login saved since (a sign-in) makes the stash meaningless. A helper outlives a dead
        service by seconds and may still write its renewed login: while a process may use its folder the stash
        stays as it is ("unsettled", see `_renewal`), for a later read to settle."""
        key, name = account["id"], self._stash_key(account["id"])
        with self._locked(key):
            saved = self.vault.get(key)
            if not self.vault.path(name).exists():
                return saved
            before = fingerprint(saved)
            try:
                stash = self.vault.get(name)
            except DeckError:       # unreadable while the login beside it reads: what happened cannot be told
                stash = {"before": before, "result": None, "home": None}
            if stash.get("before") != before:
                self.vault.remove(name)
            elif not stash.get("spent"):
                # Only once no process uses the folder is what its file holds final: a helper may renew more than
                # once in one read (Codex does when its account check fails), each renewal spending the last.
                if not stash.get("result") and self.providers["codex"].helper_running(stash.get("home")):
                    return saved
                login = stash.get("result") or self._helper_renewed(account, saved, stash.get("home"))
                if login is None:
                    self._mark_spent(account, before)
                elif self.vault.put_guarded(key, login, "refresh", identity_key, freshness, previous=saved) == "conflict":
                    raise DeckError("A Codex recovery record has a different identity. It was preserved.", "recovery")
                else:
                    self.vault.remove(name)
                diagnostics.log("codex.renewal_settled", lvl="warning", acct=diagnostics.account_ref(key),
                                recovered=login is not None)
            self.providers["codex"].drop_helper(stash.get("home"))
            return self.vault.get(key)

    def _helper_renewed(self, account, saved, home):
        """The renewed login a crashed service's helper left in its folder once it ended (the last it wrote): one
        complete and changed, for this account; otherwise None."""
        login = self.providers["codex"].helper_login(home)
        return login if identity_key(login) == account["identity"] and fingerprint(login) and login != saved else None

    def _mark_spent(self, account, before):
        """The saved login's token may be used up: it needs signing in again until another login is saved. The
        record stays in the vault, the login's own truth; a read that sees it (`_renewal`) keeps the row saying so."""
        self.vault.put(self._stash_key(account["id"]), {"before": before, "spent": True}, generation=False)
        self.store.update(account["id"], credential_state="reauth_required")

    def _renewal(self, key, login):
        """What a renewal left of `login` (the saved one): "spent" while its refresh token may be used up (also when
        it holds none or the record cannot be read), "unsettled" while its helper may still be finishing it, None
        when nothing is pending. Either of the first two forbids asking with that token."""
        name = self._stash_key(key)
        if fingerprint(login) is None:
            return "spent"
        if not self.vault.path(name).exists():
            return None
        try:
            stash = self.vault.get(name)
        except DeckError:
            return "spent"
        if stash.get("before") != fingerprint(login):
            return None
        return "spent" if stash.get("spent") else "unsettled"

    def codex_profile(self, account):
        """The saved login a switch may hand to Codex: any interrupted renewal settled first, and never a login
        whose refresh token may be spent, which Codex would replay."""
        profile = self.settle_renewal(account)
        renewal = self._renewal(account["id"], profile)
        if renewal == "spent":
            raise DeckError(SIGN_IN_FIRST, "reauth_required")
        if renewal == "unsettled":
            raise DeckError(RENEWAL_FINISHING, "busy")
        return profile

    def settle_renewals(self):
        """Once at start, before any read: every interrupted renewal is put right, then the helper folders a crash
        left behind are removed. A record that cannot be settled yet keeps them; it never stops the start."""
        settled = True
        for row in self.store.accounts():
            if row["provider"] == "codex" and not is_frozen(row) and self.vault.path(self._stash_key(row["id"])).exists():
                try:
                    settled = self._renewal(row["id"], self.settle_renewal(row)) != "unsettled" and settled
                except Exception as exc:
                    settled = False
                    diagnostics.log_exception("codex.settle_failed", exc, acct=diagnostics.account_ref(row["id"]))
        try:
            if settled:
                self.providers["codex"].tidy_helpers()
        except Exception as exc:
            diagnostics.log_exception("codex.tidy_failed", exc)

    # ---- what the snapshot shows about a saved account ----
    def series(self, account, now):
        """({window: readings in its current reset epoch}, trend points) of a saved account, worked out
        once per new reading (the stored readings never change between two readings)."""
        usage = account["last_good_usage"]
        if not usage:
            return {}, []
        with self._series_lock:
            cached = self._series.get(account["id"])
            if cached is not None and cached[0] == account["last_good_at"]:
                return cached[1], cached[2]
        samples, readings = {}, {}
        for window in map(intel.normalize_window, usage.get("windows") or ()):
            if window is None:
                continue
            since = window["reset"] - window["minutes"] * 60 if window["reset"] and window["minutes"] else None
            readings[window["id"]] = self.store.readings(account["id"], window["id"], since=since)
            samples[window["id"]] = intel.epoch_samples(readings[window["id"]], window["reset"], window["minutes"])
        followed = intel.trend_window(usage)
        trend = [] if followed is None else intel.trend(readings.get(followed["id"], []), followed["reset"],
                                                        followed["minutes"], now)
        with self._series_lock:
            self._series[account["id"]] = (account["last_good_at"], samples, trend)
        return samples, trend

    def touch_active(self, provider, identity, now):
        """Notes that a saved account is the signed-in one (`flags.last_active_at`, renewed hourly):
        the dashboard's "unused for N days" hint reads it."""
        account = self.store.find(provider, identity)
        if account is None or is_frozen(account):
            return
        last = account["flags"].get("last_active_at")
        if last is None or now - last >= ACTIVE_TOUCH:
            self.store.update(account["id"], flags={**account["flags"], "last_active_at": now})

    # ---- the unsaved signed-in account (§9.4) ----
    def sense_unsaved(self, provider, identity):
        """Tracks the account signed in to `provider` when it is not saved (None clears it)."""
        if identity is None or self.store.find(provider, identity) is not None:
            self.unsaved[provider] = None
            return None
        current = self.unsaved.get(provider)
        if current is None or current["identity"] != identity:
            current = self.unsaved[provider] = {"identity": identity, "label": None, "email": None, "plan": None,
                                                "usage": None, "last_good_at": None, "checked": None, "error": None,
                                                "expires_in": None, "last_outcome": None}
        return current

    def read_unsaved(self, provider) -> str:
        row = self.unsaved.get(provider)
        if row is None:
            return "not_found"
        if is_frozen(self.store.find(provider, row["identity"])):
            return "skipped"
        now = self.clock()
        row["checked"] = now
        try:
            if provider == "claude":
                claude = self.providers["claude"]
                profile = claude.current(cookies=False)
                if profile["identity"] != row["identity"]:
                    return "identity"
                if row["label"] is None:
                    try:
                        info = self.identity("claude", profile)
                        row.update(label=default_label(info["email"], "claude"), email=info["email"] or None,
                                   plan=info["plan"] or None)
                    except DeckError:
                        pass
                reading = claude.read_usage(profile, CredentialState.LIVE, now=now, last_good=row["usage"],
                                            cookie_route=False, incident=self._incident("claude"))
                try:
                    entry = claude.token_entry(profile)
                except DeckError:
                    entry = None
                row["expires_in"] = None if entry is None else round(entry.get("expiresAt", 0) / 1000 - now)
            else:
                codex = self.providers["codex"]
                profile = codex.current()
                if identity_key(profile) != row["identity"]:
                    return "identity"
                if row["label"] is None:
                    info = codex.identity(profile)
                    row.update(label=default_label(info["email"], "codex"), email=info["email"] or None,
                               plan=info["plan"] or None)
                reading = codex.usage(profile, mode="read_only", account=UNSAVED_IDS["codex"])
                remaining = token_state(profile, now).remaining
                row["expires_in"] = None if remaining is None else round(remaining)
        except DeckError as exc:
            row["error"] = {"code": error_code(exc), "message": str(exc)[:300] or READ_FAILED,
                            "since": (row["error"] or {}).get("since", now)}
            return error_code(exc)
        self._record(provider, UNSAVED_IDS[provider], reading.outcomes, now)
        if reading.usage is not None:
            row.update(usage=reading.usage, last_good_at=reading.checked or now, error=None, last_outcome="2xx")
            return "ok"
        if reading.error is None:
            if reading.meta.get("hollow"):
                row.update(error=None, last_outcome="2xx")
                return "ok"
            return "skipped"
        code = error_code(reading.error)
        row["error"] = {"code": code, "message": str(reading.error)[:300] or READ_FAILED,
                        "since": (row["error"] or {}).get("since", now)}
        return code
