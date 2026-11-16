"""Terminal login swap (the credentials file, or on macOS Claude Code's keychain item). Never launches,
stops or signals a process."""
from contextlib import ExitStack, contextmanager, nullcontext
import copy
import errno
import json
import sqlite3

from . import cli_auto, diagnostics
from .cli_accounts import DISAGREE, ON_HOLD, TargetError
from .cli_locks import config_lock, credential_locks
from .cli_policy import DRAIN_CEILING, considered
from .rules import plan_current, plan_target
from .core import UNFINISHED, DeckError, FileTransaction, atomic_write, is_frozen, json_bytes
from .operations import Operation
from .platform.keychain import KeychainError
from .providers.claude_cli import expires, fingerprint, merge, object_file, profile, shared
from .switching import assemble, context, register

CHANGED = "Claude Code's login changed while switching. Nothing was changed; try again."
KEPT = "Claude Code changed its login after the interrupted switch, so AccountDeck left it as it is."
RECOVERED = "The interrupted terminal switch was put back."
REPAIR_KEPT = "AccountDeck was interrupted while giving your terminals a saved login. They keep the login they have now."
UNCONFIRMED = ("AccountDeck can't confirm yet that Claude Code's login belongs to {label}, so the interrupted switch "
               "stays open and terminal auto-switch waits. It keeps checking; if this lasts, run /login in a terminal "
               "with {label}. AccountDeck then finishes by itself.")
MISMATCHED = ("Claude Code's login belongs to another account than the one it names, so the interrupted switch stays "
              "open and terminal auto-switch waits. Run /login in a terminal with the account you want; AccountDeck "
              "then finishes by itself.")
SET_ASIDE = "The unreadable record of an interrupted switch was set aside. It is kept, not deleted."
DISK_FULL = "Your Mac's disk is full, so AccountDeck could not save what a switch needs. Free some space."
DATABASE = "AccountDeck's database could not be written. Check that your Mac's disk has free space."
# Recover's work done by itself (`auto_recover`): as a job ends and on every start, then again after 30 s,
# doubling to 15 minutes, and at once after the Mac wakes. Its wait for each of Claude Code's locks is short:
# it runs on the tick thread, and those locks are held for moments unless a dead process left them, which
# they outlive by a minute at most.
RETRY, RETRY_CAP, RETRY_LOCKS = 30.0, 900.0, 3.0


class CLITransaction(FileTransaction):
    """Credentials use byte ownership (the file) or value ownership (a keychain item, `keychain_changes`);
    oauthAccount uses field ownership on rollback. What a rollback puts back is the pre-switch login, or
    that account's newer login when it was renewed since (`restore_login`)."""
    def __init__(self, service, key=None):
        super().__init__(service.vault, [service.paths.claude_cli_home], key)
        self.service = service
        self.doc["provider"] = "claude_cli"

    def write_login(self, credentials):
        """Writes the live login through the machine's CredStore, its before-state recorded first."""
        self.service.adapter.store.write(self, credentials)

    @property
    def wrote(self):
        return bool(self.doc["changes"] or self.doc.get("keychain_changes"))

    @property
    def repairs(self):
        """True for a repair's record (`repair`): it names one account before and after, as no switch does."""
        change = self.doc.get("oauth_change") or {}
        before, after = change.get("before"), change.get("after")
        return (isinstance(before, dict) and isinstance(after, dict) and isinstance(before.get("accountUuid"), str)
                and bool(before["accountUuid"]) and before["accountUuid"] == after.get("accountUuid"))

    def restore_login(self, before):
        """The credentials object a rollback puts back for the pre-switch one, `before`: itself, or, when
        the vault holds a newer login of the pre-switch account (the account whose oauthAccount the switch
        replaced) confirmed as its own, that login with `before`'s shared entries. A refresh token is single
        use: once the vault's login was renewed from `before`, `before` is spent and must never reach Claude
        Code again. Claude Code still holds what the switch wrote whenever this is asked."""
        service = self.service
        identity = ((self.doc.get("oauth_change") or {}).get("before") or {}).get("accountUuid")
        row = service.store.find("claude_cli", identity) if isinstance(identity, str) and identity else None
        if row is None or is_frozen(row):
            return before
        try:
            spent, saved = fingerprint(before), service.vault.get(row["id"])
            newer = (saved.get("identity") == identity and bool(saved.get("credentials"))
                     and saved.get("fingerprint") not in (None, spent) and saved.get("verified") == saved["fingerprint"]
                     and expires(saved["credentials"]) >= expires(before))
        except DeckError:             # no pre-switch login to compare, or no readable record: put back as it was
            return before
        if not newer:
            return before
        diagnostics.log("cli.rollback_renewed", acct=diagnostics.account_ref(row["id"]))
        return merge(saved["credentials"], before)

    def restore_bytes(self, path, before):
        """The credentials file gets `restore_login`'s login; every other file its own bytes."""
        if path != self.check(self.service.paths.claude_cli_credentials):
            return before
        try:
            value = json.loads(before.decode("utf-8-sig"))
        except ValueError:
            return before
        replaced = self.restore_login(value) if isinstance(value, dict) else value
        return before if replaced is value else json_bytes(replaced)

    def write_account(self, target):
        service = self.service
        with config_lock(service.paths, timeout=service.lock_timeout) as check:
            current = object_file(service.paths.claude_user_config, missing=True)
            self.doc["oauth_change"] = {"present": "oauthAccount" in current,
                                        "file_present": service.paths.claude_user_config.exists(),
                                        "before": current.get("oauthAccount"), "after": copy.deepcopy(target)}
            self.save()
            current["oauthAccount"] = target
            check()
            atomic_write(service.paths.claude_user_config, json_bytes(current))

    def rollback(self):
        """Puts back the login and the account it names together, or neither: when Claude Code changed either
        since the switch (renewed the login it was given, say), nothing is put back, its newer state is never
        undone, and the transaction is a conflict, left for Recover (`keep`). `.claude.json`'s lock is held
        throughout, so Claude Code cannot change the account between the comparison and the write.

        A repair's (`repairs`) puts nothing back and is closed ("kept"), so it needs no lock: it wrote only that
        account's own confirmed login over another of the same account's, or none, and the one it replaced (an
        older login, a blank, a refused or spent one) never beats it. Claude Code keeps what it holds now, which the
        mirror then files as usual."""
        if self.repairs:
            self.doc["status"] = "kept"
            self.save()
            diagnostics.log("cli.repair_closed", wrote=self.wrote)
            return
        service = self.service
        conflict = False
        change = self.doc.get("oauth_change")
        with config_lock(service.paths, timeout=service.lock_timeout) as check:
            if change:
                try:
                    current = object_file(service.paths.claude_user_config,
                                          missing=change.get("file_present") is False)
                except DeckError:
                    conflict = True
                else:
                    conflict = current.get("oauthAccount") not in (change["after"], change["before"])
            if not conflict:
                conflict = service.adapter.store.rollback(self, restore=self.restore_login)
                conflict = bool(self.rollback_files()) or conflict
            if not conflict and change and current.get("oauthAccount") == change["after"]:
                if change["present"]:
                    current["oauthAccount"] = change["before"]
                else:
                    current.pop("oauthAccount", None)
                check()
                if not current and change.get("file_present") is False:
                    service.paths.claude_user_config.unlink(missing_ok=True)
                else:
                    atomic_write(service.paths.claude_user_config, json_bytes(current))
        self.doc["status"] = "conflict" if conflict else "rolled_back"
        self.save()
        if conflict:
            raise DeckError("Claude Code changed its account after the switch. The encrypted backup was preserved.", "recovery")

    def keep(self):
        """Recover's answer to a conflict: what Claude Code holds now is its own newer state, so it is left as it
        is and the transaction is finished ("kept"; its encrypted record stays until it is pruned); the mirror
        then saves that login as usual. Only once that login is confirmed as the account's that `.claude.json`
        names (or there is none): an older build's rollback put back the account without the login, and saving
        from such a pair would file one account's login under another. Its login stays Claude Code's, so it runs
        outside Claude Code's locks (its question to the provider among them); no terminal switch starts
        meanwhile, as the transaction is still pending. A login the provider gives to the other account of this
        switch is no unknown either: only the step that names the account is missing (`_name_owner`). A record
        found in conflict once is only ever closed so (`recover`): Claude Code may have renewed what it was given,
        or hold the pre-switch account's login again, so putting anything back later could spend a login twice."""
        service = self.service
        live = service.adapter.store.read(missing=True)
        try:
            fp = fingerprint(live)
        except DeckError:             # no login left that could be filed under the wrong account
            fp = None
        if fp is not None:
            account = object_file(service.paths.claude_user_config, missing=True).get("oauthAccount")
            identity = account.get("accountUuid") if isinstance(account, dict) else None
            row = service.store.find("claude_cli", identity) if isinstance(identity, str) and identity else None
            try:
                saved = service.vault.get(row["id"]) if row else {}
            except DeckError:
                saved = {}
            # Confirmed by the provider already (the mirror saved it as that account's), or asked now.
            if not saved.get("fingerprint") == fp == saved.get("verified"):
                found = service.owner(live)
                if found is None or found != identity:
                    self._name_owner(found, identity, row)
        self.doc["status"] = "kept"
        self.save()
        diagnostics.log("cli.recover_kept", login=fp is not None)

    def _name_owner(self, found, named, row):
        """Claude Code holds `found`'s login (None: the provider cannot say) while `.claude.json` names `named`.
        When `found` is one of the two accounts this switch went between, `.claude.json` is made to agree, as the
        switch or a rollback would have left it; any other case stays open: the provider's answer, once it can
        give one, settles the first, and Claude Code's own /login either (a login that is confirmed). No sign-in
        saved in AccountDeck does: a repair waits while the record is open (cli_auto._repair)."""
        service = self.service
        change = self.doc.get("oauth_change") or {}
        side = next((change[name] for name in ("after", "before") if found and isinstance(change.get(name), dict)
                     and change[name].get("accountUuid") == found), None)
        if side is None:
            raise DeckError(MISMATCHED if found else UNCONFIRMED.format(label=row["label"] if row else "the account it shows"),
                            "recovery")
        with config_lock(service.paths, timeout=service.lock_timeout) as check:
            current = object_file(service.paths.claude_user_config, missing=True)
            account = current.get("oauthAccount")
            if (account.get("accountUuid") if isinstance(account, dict) else None) != named:
                raise DeckError(CHANGED, "busy")     # it changed since it was read: the next try sees what it says now
            current["oauthAccount"] = copy.deepcopy(side)
            check()
            atomic_write(service.paths.claude_user_config, json_bytes(current))
        diagnostics.log("cli.recover_named")


@contextmanager
def _undoing(service, tx):
    """Runs a change of the live login: what fails once something was written is rolled back under Claude Code's
    locks, or left for Recover when that cannot be done. A repair's is closed with what it wrote kept, with no
    lock needed (CLITransaction.rollback)."""
    store = service.adapter.store
    try:
        yield
    except BaseException:
        if tx.wrote:
            try:
                with nullcontext() if tx.repairs else credential_locks(service.paths, timeout=service.lock_timeout,
                                                                        storage_write=store.storage_lock):
                    tx.rollback()
            finally:
                if tx.doc["status"] not in ("rolled_back", "kept"):
                    # Left for Recover, which pauses the renewal of the logins it concerns (engine.login_held). Set
                    # while the outgoing account's key lock is still held, so its login is not renewed in between.
                    service.engine.recovery = service.engine._scan_recovery()
        elif service.vault.path(tx.key).exists():
            # Its record was saved but Claude Code's login was not touched: nothing for Recover to put back.
            tx.doc["status"] = "rolled_back"
            try:
                tx.save()
            except Exception:         # still "prepared": Recover then finds nothing to change
                pass
        raise


def perform(service, key):
    """§6, shared by manual/automatic/Undo switches; returns the outgoing account id.

    Locks, in the order every terminal path takes them (CLIAccounts): the mutex, the target's key lock, the
    outgoing account's key lock, then Claude Code's locks, under which no key lock is waited for. The
    outgoing account's key lock is held until the switch commits or rolls back, so its login, which a
    rollback puts back, is not renewed meanwhile (CLIAccounts._refresh)."""
    service.guard()
    with service.mutex, service.key_lock(key), ExitStack() as locks:
        # An open Add terminal account window does not stop it: that sign-in has its own folder and Keychain
        # item (CLIAccounts.begin_enrollment).
        row = service.row(key)
        service.require_local(key)
        target = service.target_record(key)
        if target["identity"] != row["identity"] or (target.get("oauthAccount") or {}).get("accountUuid", target["identity"]) != row["identity"]:
            raise TargetError("The saved terminal login has a different identity.", "identity")
        if target["health"] == "on_hold":
            raise TargetError(ON_HOLD, "unsupported")
        if target["health"] != "ok":
            raise DeckError("Sign in again before switching to this account.", "reauth_required")
        live_identity = service.mirror()
        if live_identity == row["identity"]:
            raise DeckError("Your terminals already use this account.", "invalid")
        outgoing_row = service.store.find("claude_cli", live_identity) if live_identity else None
        held = outgoing_row["id"] if outgoing_row else None
        if held:
            locks.enter_context(service.key_lock(held))
        # Renewed now, its recovery stash written first: a dead login fails here and needs sign-in, instead
        # of failing in every terminal later (CLIAccounts._refresh says when the switch goes ahead without).
        target = service.refresh(key, for_switch=True)
        # Claude Code renews whatever is swapped in, so only a login confirmed as this account's goes in.
        target = service.verified(key, target)
        store = service.adapter.store
        tx = CLITransaction(service)
        with _undoing(service, tx):
            with credential_locks(service.paths, timeout=service.lock_timeout, storage_write=store.storage_lock) as check:
                service.guard()
                # Re-read under CC's locks, then persist outgoing rotations BEFORE writing.
                # Only the Mac store permits initial setup without an outgoing login.
                # missing=True accepts absence, never unreadable or malformed existing data.
                fresh_mac = store.kind == "keychain"
                absent = False
                try:
                    credentials = store.read()
                except DeckError as exc:
                    if not fresh_mac or exc.code != "login_needed":
                        raise
                    credentials, absent = {}, True
                config = object_file(service.paths.claude_user_config, missing=fresh_mac)
                account_uuid = (config.get("oauthAccount") or {}).get("accountUuid")
                if account_uuid == target["identity"]:
                    raise DeckError("Your terminals already use this account.", "invalid")
                known = service.store.find("claude_cli", account_uuid) if account_uuid else None
                if (known["id"] if known else None) != held:
                    raise DeckError(CHANGED, "busy")     # that account's key lock cannot be taken under these locks
                if absent and account_uuid is None:
                    previous = None
                else:
                    try:
                        outgoing = profile(credentials, config.get("oauthAccount"), service.clock())
                    except DeckError as exc:
                        if exc.code != "login_needed" or known is None or known["flags"].get("health") != "needs_signin":
                            raise
                        previous = known["id"]  # blank revoked stub: retain the last encrypted generation
                    else:
                        # A /login writes its token before `.claude.json` names the new account: a token the
                        # provider said is another account's is never filed under the one named. Only what
                        # `mirror` already asked counts, so no network call runs under these locks; an owner not
                        # known yet does not stop the switch (an outage of that endpoint would stop every one).
                        if service.owners.get(outgoing["fingerprint"]) not in (None, outgoing["identity"]):
                            raise DeckError(DISAGREE, "identity")
                        # Its cross-check refuses a login another record holds rather than wait for that key lock.
                        previous = service.save(outgoing, source="sync-back", drop=False)
                composed = merge(target["credentials"], credentials)
                expected_shared = shared(credentials)
                account = target.get("oauthAccount") or {"accountUuid": target["identity"],
                    "emailAddress": target["email"], "organizationUuid": target["organizationUuid"]}
                tx.doc["oauth_change"] = {"present": "oauthAccount" in config,
                    "file_present": service.paths.claude_user_config.exists(),
                    "before": config.get("oauthAccount"), "after": copy.deepcopy(account)}
                tx.save()     # encrypted account-field recovery exists before either live write
                check()
                tx.write_login(composed)
                tx.write_account(account)
                check()
            # Verify after releasing CC's locks as required. On failure reacquire before
            # conditional rollback, so recovery cannot interleave with CC's refresh.
            verified = service.adapter.current()
            if (verified["identity"] != target["identity"] or verified["fingerprint"] != target["fingerprint"]
                    or shared(store.read()) != expected_shared):
                raise TargetError("The terminal account could not be verified.", "identity")
            tx.commit()
            service.engine.active_identity["claude_cli"] = target["identity"]
            service.engine.identity_known["claude_cli"] = True
            service.engine.bump()
            return previous


def repair(service, key):
    """Gives Claude Code the login saved for the account it already uses, when that should replace what it holds
    (`CLIAccounts._repair_due`, asked again under its locks): the owner signed in again, or its own login is dead
    or gone. A switch's locks and transaction (`perform`), never put back: one that fails or is cut short keeps
    what it wrote (CLITransaction.rollback); `.claude.json` already names the account and stays as it is. No
    refresh token is sent: a login AccountDeck hands over needs no renewal, Claude Code renews
    what it gets. True when the login went in, False when it no longer should (one Claude Code renewed since stays)."""
    service.guard()
    with service.mutex, service.key_lock(key):
        row = service.row(key)
        service.require_local(key)
        if service.mirror() != row["identity"]:
            return False
        target = service.target_record(key)
        if target["identity"] != row["identity"] or (target.get("oauthAccount") or {}).get("accountUuid", target["identity"]) != row["identity"]:
            raise TargetError("The saved terminal login has a different identity.", "identity")
        # Claude Code renews whatever is swapped in, so only a login confirmed as this account's goes in; and it
        # replaces only a login of this account (`.claude.json` can lag the token while /login runs).
        target = service.confirmed(key, target)
        try:
            found = service.owner(service.adapter.current()["credentials"])
        except DeckError as exc:
            if exc.code != "login_needed":
                raise
            found = None
        if found not in (None, row["identity"]):
            raise DeckError(DISAGREE, "identity")
        store = service.adapter.store
        tx = CLITransaction(service)
        with _undoing(service, tx):
            with credential_locks(service.paths, timeout=service.lock_timeout, storage_write=store.storage_lock) as check:
                service.guard()
                credentials = store.read(missing=True)
                account = object_file(service.paths.claude_user_config).get("oauthAccount")
                if not isinstance(account, dict) or account.get("accountUuid") != target["identity"]:
                    raise DeckError(CHANGED, "busy")
                try:
                    live = service.crosscheck(profile(credentials, account, service.clock()), drop=False)
                except DeckError as exc:
                    if exc.code != "login_needed":
                        raise
                    live = None
                if service._repair_due(key, target, live) is None:
                    return False
                expected_shared = shared(credentials)
                tx.doc["oauth_change"] = {"present": True, "file_present": True, "before": copy.deepcopy(account),
                                          "after": copy.deepcopy(account)}
                tx.save()     # encrypted recovery exists before the live write
                check()
                tx.write_login(merge(target["credentials"], credentials))
                check()
            verified = service.adapter.current()
            if (verified["identity"] != target["identity"] or verified["fingerprint"] != target["fingerprint"]
                    or shared(store.read()) != expected_shared):
                raise TargetError("The terminal account could not be verified.", "identity")
            tx.commit()
        service.repaired(key, target)
        return True


def _os_failure(exc):
    if isinstance(exc, sqlite3.Error):
        return DATABASE
    if exc.errno == errno.ENOSPC:
        return DISK_FULL
    return f"macOS refused a file AccountDeck needs ({exc.strerror})." if exc.strerror else "macOS refused a file AccountDeck needs."


def environment_failure(exc):
    """What is wrong when a failed switch says nothing about its target, or None (cli_auto.switch_settled): this
    Mac's keychain, disk or files, or Claude Code's locks or files, hold back a switch to any account. A login
    refused, a bad record or a decision gone stale are the target's, and so is a bug's exception, as retrying
    that target for good would be worse than trying the next."""
    if isinstance(exc, (OSError, sqlite3.Error)):
        return _os_failure(exc)
    if not isinstance(exc, DeckError) or isinstance(exc, TargetError) or exc.code in ("reauth_required", "invalid", "not_found"):
        return None
    return str(exc)


class CLISwitch(Operation):
    """A terminal switch; `kind` "repair" gives the account in use its saved login instead (`repair`)."""

    def __init__(self, engine, key, *, kind="manual", reason=""):
        row = engine.cli.row(key)
        super().__init__("switch", provider="claude_cli", account=key, label=row["label"], consent="now",
                         steps=(("switch", "Switching the terminal login"),))
        self.engine, self.auto_kind, self.reason = engine, kind, reason
        self.environment = None      # `environment_failure` of how it failed
        self.changed = True          # False: the repair found nothing to do

    def run(self):
        with self.step("switch", phase="restoring", message="Switching the terminal login…", cancellable=False):
            manifest = diagnostics.Manifest.start(self.id, "switch", directory=self.engine.paths.data_dir / "logs" / "manifests")
            manifest.set("provider", "claude_cli").set("account", diagnostics.account_ref(self.account))
            self.report["manifest"] = self.id
            try:
                if self.auto_kind == "repair":
                    self.changed = repair(self.engine.cli, self.account)
                else:
                    previous = perform(self.engine.cli, self.account)
            except BaseException as exc:
                manifest.set("verification.result", "failed").set("error_type", type(exc).__name__).finish()
                self.environment = environment_failure(exc)
                if isinstance(exc, (OSError, sqlite3.Error)):
                    diagnostics.log_exception("cli.switch_system", exc)
                    raise DeckError(self.environment, "system") from None
                raise
            if self.auto_kind == "repair":
                self.report.update(verified="verified", summary=self.reason if self.changed else
                                   f"Your terminals keep their own login of {self.label}: it is the newest.")
                if self.changed:
                    self.engine.notice(self.reason, "good", sticky=False)
                diagnostics.log("cli.repaired", acct=diagnostics.account_ref(self.account), changed=self.changed)
                manifest.set("verification.result", "verified").set("kind", self.auto_kind).finish()
                return
            now = self.engine.clock()
            with cli_auto.history(self.engine) as history:
                history.update(left=previous, left_at=now, exhausted=False)    # any switch, manual too, ends it
                if self.auto_kind == "manual":
                    # The user's own choice, Undo included: cli_policy holds it against the threshold switch.
                    history["manual_at"], history["manual_for"] = now, self.account
                    if history.get("last"):
                        history["last"]["undo_until"] = now
                else:
                    # The choice held while the terminals stayed on the account chosen: one they come back to by
                    # themselves is left at the switch point like any other.
                    history.pop("manual_for", None)
                    history["last"] = {"from": previous, "to": self.account, "at": now, "kind": self.auto_kind,
                        "reason": self.reason, "undo_until": now + 600}
                    if self.auto_kind == "early":
                        history["early_at"] = now
            self.report.update(verified="verified", summary=f"Terminals now use {self.label}. Open terminals switch on their next message; the first reply may be slower.")
            self.engine.notice(self.report["summary"], "good")
            diagnostics.log("cli.switched", acct=diagnostics.account_ref(self.account), kind=self.auto_kind)
            manifest.set("verification.result", "verified").set("kind", self.auto_kind).finish()


def plan(engine, key, purpose="switch"):
    service = engine.cli
    if purpose == "recover":
        blockers = []
        try:
            service.guard()
            # An open Add terminal account window does not stop it: that sign-in has its own folder and Keychain
            # item (CLIAccounts.begin_enrollment), and Recover touches only the terminals' login.
            if engine.runner.busy:
                raise DeckError("Finish the current operation first.", "busy")
        except DeckError as exc:
            blockers.append({"code": exc.code, "message": str(exc)})
        planned = assemble(context(engine), provider="claude_cli", purpose="recover", account=None,
            target=None, current=None, headroom=None,
            running={"desktop": False, "sessions": [], "busy": 0, "unknown": 0, "foreign_claude": 0, "codex_consumers": []},
            pairs=(), blockers=blockers, warnings=[], tip=None,
            will=["Restore only terminal login fields still owned by the interrupted switch. No terminal is closed.",
                  "A login Claude Code changed since is kept as it is."] +
                 (["Set aside the record that can't be read. It is kept, not deleted."] if engine.unreadable else []),
            preserved=["Later application changes"], account_bound=[], invisible=None, stop_tier="not_running")
        planned.plan.update(confirmation_required=True, remember_available=False)
        return register(context(engine), planned)
    row = service.row(key)
    service.require_local(key)
    views = engine.snapshot()["accounts"]
    target = next(a for a in views if a["id"] == key)
    current = next((a for a in views if a["provider"] == "claude_cli" and a["active"]), None)
    blockers = []
    try:
        service.guard()
        # Only an interrupted terminal switch holds one: a Claude Desktop or Codex recovery touches none of its files.
        if engine.runner.busy or engine.recovering("claude_cli"):
            raise DeckError("Finish the current operation or recovery first.", "busy")
        if row["flags"].get("health") == "on_hold":
            raise DeckError(ON_HOLD, "unsupported")
        if row["flags"].get("health") == "needs_signin":
            raise DeckError("Sign in again before switching to this account.", "reauth_required")
        if target["active"]:
            raise DeckError("Your terminals already use this account.", "invalid")
    except DeckError as exc:
        blockers.append({"code": exc.code, "message": str(exc)})
    planned = assemble(context(engine), provider="claude_cli", purpose="switch", account=key,
        target=plan_target(target), current=plan_current(current) if current else None, headroom=None,
        running={"desktop": False, "sessions": [], "busy": 0, "unknown": 0, "foreign_claude": 0, "codex_consumers": []},
        pairs=(), blockers=blockers, warnings=[], tip=None,
        will=["Open terminals continue on the new account from their next message — nothing is closed.",
              "The first reply after switching is slower."], preserved=["MCP logins, projects, settings and history"],
        account_bound=[], invisible=None, stop_tier="not_running")
    planned.plan.update(confirmation_required=True, remember_available=False)
    if any(used is not None and used >= engine.prefs.get("cli_threshold") for _, used, _ in considered(target, engine.prefs.public())):
        planned.plan["warnings"].append({"code": "rate_limited", "message": (
            "This account is over your auto-switch threshold. Auto-switch leaves your choice alone for 2 hours, or "
            f"until the account reaches {DRAIN_CEILING}% or a limit.")})
    return register(context(engine), planned)


def recover(engine, *, automatic=False):
    """Recover's work on the interrupted terminal switches: each is put back, or, where Claude Code changed its
    login since, closed with that login kept (`keep`); one found so before (a conflict) is only ever closed so, and
    a repair's is closed with what it wrote kept (CLITransaction.rollback). Returns what it did (KEPT, RECOVERED,
    REPAIR_KEPT), or None without such a record. DeckError "recovery" when a login cannot be confirmed, or whatever
    stops the locks or the keychain."""
    service = engine.cli
    restored, repaired, kept = False, False, False
    try:
        with service.mutex:
            pending = []
            for tx_key in engine.recovery:
                if tx_key != "unreadable":       # nothing can be put back from that one: `_set_aside`
                    doc = service.vault.get(tx_key)
                    if doc.get("provider") == "claude_cli" and doc.get("status") in UNFINISHED:
                        tx = CLITransaction(service, tx_key)
                        tx.doc = doc
                        pending.append(tx)
            if not pending:
                return None
            undo = [tx for tx in pending if not tx.repairs and tx.doc["status"] != "conflict"]
            for tx in pending:
                if tx.repairs:
                    tx.rollback()        # puts nothing back
                    repaired = True
            if undo:
                with credential_locks(service.paths, timeout=min(RETRY_LOCKS, service.lock_timeout) if automatic else service.lock_timeout,
                                      storage_write=service.adapter.store.storage_lock):
                    for tx in undo:
                        try:
                            tx.rollback()
                            restored = True
                        except DeckError as exc:     # now a conflict
                            if exc.code != "recovery":
                                raise
        for tx in pending:       # Claude Code changed its login since, now or before: nothing is put back
            if tx.doc["status"] == "conflict":
                tx.keep()
                kept = True
    finally:
        engine.recovery = engine._scan_recovery()
        if kept and not automatic:
            engine.notice(KEPT, "warning")
    return KEPT if kept else RECOVERED if restored else REPAIR_KEPT if repaired else None


def _set_aside(engine):
    """The user's Recover on a record that cannot be read: nothing can be put back from it and it would hold
    every switch for good, so it moves out of the way with its bytes kept (Vault.quarantine). Never by itself,
    and not while the keychain, rather than the record, is what cannot be read."""
    engine.recovery = engine._scan_recovery()
    for _, exc in engine.unreadable:
        if isinstance(exc, KeychainError):
            raise exc
    if not engine.unreadable:
        return
    for key, _ in engine.unreadable:
        path = engine.vault.path(key)
        engine.vault.quarantine(key, path.read_bytes())
        path.unlink(missing_ok=True)
        diagnostics.log("cli.recover_set_aside", lvl="warning")
    engine.recovery = engine._scan_recovery()
    engine.notice(SET_ASIDE, "warning")


def auto_recover(engine, *, ending=False):
    """Recover without the owner, on the tick (at start among them) and as a job ends (`ending`). Its outcome is
    not in doubt: `rollback` puts back only what still holds what the switch wrote, and a spent login never (a
    repair's nothing at all), and `keep` leaves a login Claude Code changed since as it is, and only once it is
    confirmed as the account's that `.claude.json` names. What stops it (Claude Code's locks, a locked keychain, no answer from the provider) is
    tried again (RETRY), and what only the owner can settle (`needs_you`: a login nobody can confirm, a damaged
    record) stays as it is, with one alert: the banner and the hold say what is wrong (Engine.recovery_words).
    Returns what it did, for the job that just ended, or None."""
    service, wait = engine.cli, engine.cli.recovery_wait
    if not engine.recovering("claude_cli"):
        wait.update(next=0.0, count=0, problem=None, needs_you=False, told=False)
        return None
    now = engine.clock()
    busy = engine.runner.busy and not engine.runner.yields()      # a job waiting for sessions touches no terminal login
    if not engine.owns_login or service.adapter.readonly_reason or (not ending and (busy or now < wait["next"])):
        return None
    done, problem, needs_you = None, None, False
    try:
        if engine.recovery_unknown:          # the keychain may be open by now
            engine.recovery = engine._scan_recovery()
        done = recover(engine, automatic=True)
    except DeckError as exc:
        problem, needs_you = str(exc), exc.code == "recovery"
        diagnostics.log("cli.auto_recover", lvl="warning", code=exc.code, tries=wait["count"] + 1)
    except Exception as exc:         # a full disk, a refused file or a bug: it never stops the job's outcome or the tick
        problem = _os_failure(exc) if isinstance(exc, (OSError, sqlite3.Error)) else None
        diagnostics.log_exception("cli.auto_recover_failed", exc, tries=wait["count"] + 1)
    if engine.recovering("claude_cli"):
        needs_you = needs_you or any(not isinstance(exc, KeychainError) for _, exc in engine.unreadable)
        wait.update(next=now + min(RETRY * 2 ** min(wait["count"], 8), RETRY_CAP), count=wait["count"] + 1,
                    problem=problem, needs_you=needs_you)
        if needs_you and not wait["told"]:
            wait["told"] = True
            if engine.prefs.get("alerts_enabled"):
                title, message = engine.recovery_words()
                engine.store.add_alert({"kind": "switch_result", "level": "critical", "account": None, "window": None,
                                        "title": title, "message": message, "at": now})
        engine.bump()
        return None
    wait.update(next=0.0, count=0, problem=None, needs_you=False, told=False)
    if done:
        engine.notice(done, "info", sticky=False)
        engine.store.event("recover", done)
    diagnostics.log("cli.auto_recovered", did=done)
    engine.sense_active()
    engine.bump()
    return done


def dispatch(engine, name, body):
    service = engine.cli
    key = body.get("id")
    if name == "switch-plan":
        return {"plan": plan(engine, key, body.get("purpose", "switch"))}
    service.guard()
    if name == "cli-exclude":
        service.set_excluded(key, body.get("excluded"))
        return {}
    if name == "add":
        service.begin_enrollment()
        return {}
    if name in ("finish-login", "cancel-login"):
        service.poll_enrollment(cancel=name == "cancel-login")
        return {}
    if name == "cli-undo":
        history = engine.store.setting("cli_history", {})
        last = history.get("last")
        if not last or last["undo_until"] < engine.clock():
            raise DeckError("Undo is no longer available.", "invalid")
        return {"plan": plan(engine, last["from"])}
    if name == "recover":
        if engine.runner.busy:
            raise DeckError("Finish the current operation first.", "busy")
        _set_aside(engine)
        recover(engine)
        return {}
    if name == "switch":
        if engine.recovering("claude_cli"):
            raise DeckError("Finish the current operation or recovery first.", "busy")
        entry = engine.plans.get(body.get("plan_id"))
        if not entry or entry.get("provider") != "claude_cli" or entry.get("account") != key or entry["expires"] < engine.clock():
            raise DeckError("Confirm the terminal switch again.", "stale_plan")
        engine.plans.pop(body["plan_id"], None)
        return engine.submit(CLISwitch(engine, key))
    raise DeckError("That terminal action is not available.", "invalid")
