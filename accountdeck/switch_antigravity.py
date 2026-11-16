"""Switching Antigravity accounts: the plan the user confirms and the job that puts a saved sign-in in agy's keychain
item. switching.dispatch routes every switch-family action of app "antigravity" here: switch-plan, switch, capture
(Save), add (Add another account, Sign In Again), cancel-login and recover.

What a switch does, in order: it waits until no agy runs (it never stops one: a running agy would adopt the new
account or sign itself out); reads the live record and keeps it (a saved account's vault copy is replaced by it, an
unsaved account is saved); writes an `agy-transaction-<uuid>` record (both records whole, and the target); writes the
target's record to agy's keychain item and reads it back (the e-mail must match); then deletes the transaction
record. On a failure it puts the outgoing record back; when that fails too, or agy started meanwhile, the transaction
stays for Recover (AgyService.recover). The restored record's access token is old: agy renews it at its next start,
and AccountDeck renews nothing. Never a `transaction-` record: every older build would block all switching on one.

Save, Add and Switch go through a plan and the user's confirmation (switching.claim), as the other apps' do; Cancel
and Recover change nothing a running agy uses without saying so first (Recover refuses while agy runs), so they need
none.
"""
from __future__ import annotations

from pathlib import Path
import time

from . import contract
from .agy_accounts import (ADD_WHILE_RUNNING, BUSY, PUT_BACK_WHILE_RUNNING, READ_ONLY, UNSAVED_ID, USAGE_RUNNING,
                           limit_text, holds_record)
from .core import DeckError, agy_key, agy_transaction_key
from .diagnostics import Manifest
from .operations import Operation
from .procs import ProcessSet, unix_time
from .switching import QuickOp, assemble, claim, context, note, register, submit

__all__ = ["PURPOSES", "processes", "build_plan", "AgySwitch", "dispatch"]

PURPOSES = ("switch", "capture", "add", "cancel-login", "recover")
NOTICE_FIRST = "Read the note about Google's terms for Antigravity first."
STARTED_AGAIN = "agy started again. Nothing was changed."
# Reasons a store reads `unknown` that mean a whole sign-in of someone's is there. AccountDeck's own writes never read
# back this way (it writes only personal sign-ins it could read); an item that does not decode may be its own.
THIRD_SIGN_IN = ("not_consumer", "incomplete")
WAIT_EXPIRED = ("agy was still running when this switch's confirmation expired. Nothing was changed; review the "
                "switch again.")
# Google documents one sign-in shared by Antigravity's local surfaces; whether the desktop app and the IDE read agy's
# keychain item is not verified, so the plan promises nothing about them. T3 Code's agent keeps its own (verified).
NOT_SEEN = ("Antigravity's desktop app and IDE may share agy's sign-in: AccountDeck cannot see whether they run, so "
            "quit them before you switch. T3 Code keeps a sign-in of its own, which a switch here does not change.")
PRESERVED = ["agy's settings, MCP servers and conversations (nothing under ~/.gemini is changed)"]


def processes(ctx):
    """(the agy processes a switch or an Add waits for, their ProcessSet), from one look at the processes."""
    consumers = ctx.procs.agy_consumers(rows=ctx.procs.snapshot())
    return consumers, ProcessSet.of(consumers)


def _plan_account(row, now):
    """A row of `providers.antigravity.accounts` as a plan's target: no meters (windows) of the other apps' kind;
    its tightest limit is the plan's headroom text."""
    return {"id": row["id"], "label": row["label"], "email": row["email"], "plan": row["plan"], "windows": [],
            "checked": (row["usage"] or {}).get("at"), "age_tier": None, "dormant_days": None}


def build_plan(ctx, account, purpose="switch", *, ignore_op=None, keep=None):
    """The switching.Planned for `purpose` (PURPOSES; `account`: the saved account to switch to, or for "add" the one
    to sign in again), built with switching.assemble for provider "antigravity". Its `running.agy_consumers` lists
    the agy processes ({"pid", "kind": "agy", "started", "parent"}), which the job waits for and never stops
    (stop_tier "not_running"; `idle` only when none runs). Blockers: a read-only copy, the notice not accepted
    ("notice_required"), agy's store unknown, a switch left for Recover (except for "recover"), an open Add (except
    for "cancel-login"; an Add that is ending says what it waits for), another job, an agy process that cannot be
    told apart; for Add, any running agy and AccountDeck's own usage run. It looks at agy's store first
    (Engine.sense_active), so the plan is made for the account signed in now."""
    if purpose not in PURPOSES:
        raise DeckError("That Antigravity action is unavailable.", "invalid")
    engine, agy = ctx.engine, ctx.engine.agy
    agy.check_version()
    engine.sense_active(only="antigravity")
    now = ctx.clock()
    block = agy.state(now)
    rows = block["accounts"]
    current = next((row for row in rows if row["active"]), None)
    target = None
    if purpose in ("switch", "add") and account is not None:
        target = next((row for row in rows if row["id"] == account and row["saved"]), None)
        if target is None:
            raise DeckError("Choose a saved Antigravity account.", "not_found")
    if purpose == "switch":
        if target is None:
            raise DeckError("Choose a saved Antigravity account.", "not_found")
        if target["active"]:
            raise DeckError("This account is already active.", "invalid")
    if purpose == "recover" and block["recovery"] is None:
        raise DeckError("No Antigravity switch needs recovering.", "invalid")
    if purpose == "cancel-login" and not agy.enrolling:
        raise DeckError("No Antigravity sign-in is open.", "invalid")
    consumers, found = processes(ctx)
    store = block["store"]
    blockers, warnings = [], []
    # The first blocker is the refusal's (switching.claim): what must happen first comes first.
    if engine.read_only:
        blockers.append(note("read_only", READ_ONLY))
    if block["recovery"] is not None and purpose != "recover":
        blockers.append(note("recovery", "Finish the pending Antigravity recovery first."))
    if agy.enrolling and purpose != "cancel-login":
        restoring = agy.restoring_refusal()
        blockers.append(note("busy", str(restoring) if restoring is not None
                             else "Finish or cancel adding the Antigravity account first."))
    if engine.pending and engine.pending["id"] != ignore_op:
        blockers.append(note("busy", BUSY))
    if purpose in ("switch", "capture", "add") and not block["notice_seen"]:
        blockers.append(note("notice_required", NOTICE_FIRST))
    if purpose in ("switch", "capture", "add") and store["state"] == "unknown":
        blockers.append(note("antigravity", store["message"]))
    if purpose == "capture":
        if store["state"] == "absent":
            blockers.append(note("login_needed", "Nobody is signed in to Antigravity on this Mac."))
        elif current is not None and current["saved"]:
            blockers.append(note("invalid", "This Antigravity account is saved already."))
    if purpose == "switch":
        if not target["switchable"]:
            blockers.append(note("vault", "This account's saved sign-in is missing. Sign in again to use it."))
        elif (target["error"] or {}).get("code") == "reauth_required":
            blockers.append(note("reauth_required", "Sign in again before switching to this account."))
        else:
            try:
                saved_sign_in(ctx, target)
            except DeckError as exc:
                blockers.append(note(exc.code, str(exc)))
    if purpose == "add":
        if not block["installed"]:
            blockers.append(note("not_installed", "Install agy first: the sign-in happens in agy."))
        if consumers:
            blockers.append(note("busy", ADD_WHILE_RUNNING))
        if agy.refresh_busy:     # its run reads the store before and after: an Add in between would mix two accounts
            blockers.append(note("busy", USAGE_RUNNING))
    if purpose == "recover" and keep in ("before", "after") and consumers:
        blockers.append(note("busy", PUT_BACK_WHILE_RUNNING))
    # ctime 0 means "start time unknown" (another user's process, say): it cannot be told apart, so it blocks.
    if any(not row.ctime or row.kind != "agy" for row in consumers):
        blockers.append(note("safety", "The agy processes cannot be verified."))
    if consumers and purpose == "switch":
        count = len(consumers)
        warnings.append(note("consumers", f"agy is running ({count} session{'' if count == 1 else 's'}). The switch "
                                          "waits until it closes: a running agy would move to the other account."))
    running = {"desktop": False, "sessions": [], "busy": 0, "unknown": 0, "foreign_claude": 0, "codex_consumers": [],
               "agy_consumers": [{"pid": row.pid, "kind": "agy", "started": unix_time(row.ctime), "parent": None}
                                 for row in consumers]}
    saves = purpose in ("switch", "add") and current is not None and not current["saved"]
    headroom = None
    if purpose == "switch":
        headroom = {"now": limit_text(current["usage"], now) if current else None,
                    "after": limit_text(target["usage"], now)}
        if headroom == {"now": None, "after": None}:
            headroom = None
    result = assemble(ctx, provider="antigravity", purpose=purpose, account=account,
                      target=_plan_account(target, now) if target else None,
                      current={"id": current["id"], "label": current["label"], "windows": []} if current else None,
                      headroom=headroom, running=running, pairs=found.pairs, blockers=blockers, warnings=warnings,
                      tip=None, will=_will(purpose, target, current, keep, consumers), preserved=list(PRESERVED),
                      account_bound=[], invisible=NOT_SEEN, stop_tier="not_running", saves_outgoing=saves)
    # The account agy held when the plan was made: one that changed before the confirmation asks again.
    result.entry["identity"] = block["signed_in"]
    return result


def _will(purpose, target, current, keep, consumers):
    if purpose == "switch":
        return ((["Wait until agy is closed (it is never closed for you)."] if consumers else [])
                + ["Save the current sign-in." if current else "Nothing is signed in now, so nothing is saved.",
                   f"Put {target['label']}'s saved sign-in in agy's place in the keychain and check it.",
                   "agy picks it up the next time it starts. AccountDeck itself sends nothing to Google."])
    if purpose == "capture":
        return ["Save the account agy is signed in to now. agy itself is not changed."]
    if purpose == "add":
        return [("Save the current sign-in, then sign" if current else "Sign") + " agy out on this Mac only (its "
                "keychain item is removed; nothing is revoked).",
                "Open agy in a Terminal window: sign in with Google there" + (f" as {target['label']}." if target else "."),
                "Save the new sign-in. If you cancel, close agy's window, or nobody signs in within 15 minutes, the "
                "earlier sign-in comes back once agy has closed."]
    if purpose == "cancel-login":
        return ["Put the earlier sign-in back once agy has closed (never while it runs), unless a new one has landed "
                "already: that one is saved and stays."]
    words = {"before": "Put the sign-in from before the switch back.", "after": "Put the selected sign-in in place.",
             "keep": "Leave agy's sign-in as it is now."}
    return [words.get(keep, "Choose which sign-in agy keeps.")]


def plan_switch(ctx, account=None, purpose="switch", *, ignore_op=None, keep=None):
    return register(ctx, build_plan(ctx, account, purpose, ignore_op=ignore_op, keep=keep))


def saved_sign_in(ctx, row) -> dict:
    """The record saved for `row` (a saved account), checked: its e-mail is the row's and agy's item can take it
    (KeychainStore.fits). DeckError otherwise; nothing is written."""
    try:
        saved = ctx.vault.get(agy_key(row["id"]))
    except DeckError:
        raise DeckError("This account's saved sign-in cannot be read now.", "vault") from None
    record = (saved.get("stores") or {}).get("cli") if isinstance(saved, dict) else None
    if (not isinstance(record, dict) or saved.get("email") != row["email"]
            or str(record.get("id_token_email") or "").strip().lower() != row["email"]):
        raise DeckError("The saved sign-in is not this account's. Nothing was changed.", "identity")
    if not ctx.engine.agy.adapter.store().fits(record):
        raise DeckError("This saved sign-in does not fit agy's keychain item. Sign in again to use it.", "antigravity")
    return record


class AgySwitch(Operation):
    """The switch job (consent "idle": it waits while agy runs, changing nothing, so it may be pre-empted then)."""

    preemptible = True

    def __init__(self, ctx, claimed, target):
        """`claimed`: the switching.Claim of the confirmed plan; `target`: the saved account's row
        (Store.agy_account)."""
        super().__init__("switch", provider="antigravity", account=target["id"], label=target["label"],
                         consent=claimed.mode, token=claimed.token,
                         steps=(("wait", "Waiting for agy to close"), ("save", "Saving the current sign-in"),
                                ("restore", "Putting the selected sign-in in place"),
                                ("verify", "Checking the sign-in")))
        self.ctx, self.claimed, self.target = ctx, claimed, target

    def run(self):
        ctx, engine = self.ctx, self.ctx.engine
        agy = engine.agy
        manifest = Manifest.start(self.id, "switch", directory=Path(ctx.paths.data_dir) / "logs" / "manifests")
        self.report["manifest"] = self.id
        manifest.set("consent.mode", self.consent).set("consent.pair_count", len(self.token.pairs))
        tx_key, outcome = None, "failed"
        try:
            with self.step("wait", phase="waiting_idle", message="Waiting for agy to close…"):
                allowed = set(self.token.pairs)
                while True:
                    consumers, found = processes(ctx)
                    if not set(found.pairs) <= allowed:
                        raise DeckError("agy started again. Review the switch again.", "needs_reconfirm",
                                        plan=plan_switch(ctx, self.account, ignore_op=self.id))
                    if not consumers and not agy.refresh_busy:
                        break
                    if consumers and self.consent != "idle":
                        raise DeckError("Close agy or choose Wait.", "busy")
                    if time.time() >= self.token.expires:
                        # The confirmation covers the wait: a switch confirmed long ago never starts unasked.
                        raise DeckError(WAIT_EXPIRED, "stale_plan")
                    self.set_waiting([{"pid": row.pid, "name": row.name, "status": "busy"} for row in consumers])
                    self.sleep(.25)
                if engine.read_only:
                    raise DeckError(READ_ONLY, "read_only")
                # The wait ends before the cancel is checked: a cancel, or a preempt (allowed only while waiting),
                # that came first ends the job here; after the phase change both are refused.
                self.set_phase("saving", "Saving the current Antigravity sign-in…", cancellable=False)
                self.checkpoint()
            with self.step("save", phase="saving", message="Saving the current Antigravity sign-in…"):
                live = agy.adapter.read()
                if live.state == "unknown":
                    raise DeckError(live.message or "agy's sign-in cannot be read.", "antigravity")
                outgoing = live.email if live.state == "ok" else None
                if outgoing != self.claimed.entry.get("identity"):
                    raise DeckError("agy's sign-in changed. Review the switch again.", "identity")
                if live.state == "ok":
                    if not agy.adapter.store().fits(live.record):     # it could not go back after a failure
                        raise DeckError("AccountDeck could not put the current sign-in back if the switch failed, so "
                                        "it leaves it alone.", "antigravity")
                    agy.keep_outgoing(live)
            with self.step("restore", phase="restoring", message="Putting the selected sign-in in place…"):
                target = engine.store.agy_account(self.account)
                if target is None:
                    raise DeckError("That account no longer exists.", "not_found")
                record = saved_sign_in(ctx, target)
                key = agy_transaction_key()
                ctx.vault.put(key, {"v": 1, "status": "prepared", "at": ctx.clock(), "account": self.account,
                                    "before": live.record if live.state == "ok" else None, "before_email": outgoing,
                                    "after": record, "after_email": target["email"]})
                # The last look comes once the switch's record is written, straight before agy's item is: an agy
                # that started meanwhile would move to the other account, and a sign-in that changed meanwhile is
                # not the one saved above. Nothing of agy's was changed yet, so the record goes again.
                try:
                    if agy.refresh_busy or engine.read_only:
                        raise DeckError(STARTED_AGAIN, "busy")
                    agy.last_look(live, STARTED_AGAIN)
                except BaseException:
                    ctx.vault.remove(key)
                    raise
                tx_key = key               # from here on a failure puts the outgoing record back (or leaves this)
                agy.adapter.store().write(record)
            with self.step("verify", phase="verifying", message="Checking the selected Antigravity account…"):
                after = agy.adapter.read()
                agy.noted_write(after)
                if after.state != "ok" or after.email != target["email"]:
                    raise DeckError("agy's sign-in did not read back as the selected account.", "identity")
                ctx.vault.remove(tx_key)
                tx_key = None
                self.report.update(verified="verified", verified_at=ctx.clock(), verify_reason=None,
                                   summary=f"Antigravity is signed in as {self.label}. agy picks it up the next time "
                                           "it starts." + (" The outgoing account was saved."
                                                           if self.claimed.plan["saves_outgoing"] else ""))
                manifest.set("verification.result", "verified")
                outcome = "success"
        except DeckError as exc:
            if tx_key is not None:
                raise self._undo(tx_key, exc) from exc
            raise
        except BaseException as exc:
            if tx_key is not None:
                raise self._undo(tx_key, DeckError("The switch stopped unexpectedly.", "invalid")) from exc
            raise
        finally:
            agy.scan_transactions()
            manifest.set("outcome", outcome).finish()

    def _undo(self, tx_key, error):
        """Puts the outgoing record back after a failed write or check. While agy runs, while agy's item holds a
        third account's sign-in, or when the put-back fails too, the transaction stays for Recover. Returns the error
        the job ends with."""
        ctx, agy = self.ctx, self.ctx.engine.agy
        try:
            doc = ctx.vault.get(tx_key)
            if processes(ctx)[0]:
                raise DeckError("agy is running", "busy")
            held = agy.adapter.read()
            if (held.state == "unknown" and held.reason in THIRD_SIGN_IN) or (
                    held.state == "ok" and not any(holds_record(held, doc[name]) for name in ("before", "after"))):
                # A sign-in that is neither the outgoing record nor the target's is in agy's item (another program
                # wrote it meanwhile), also one AccountDeck cannot save (a work or school account). It is saved
                # nowhere, so it stays: Recover saves it before it writes, or leaves it.
                raise DeckError("another sign-in is there", "identity")
            if doc["before"] is None:
                agy.adapter.store().delete()
                found = agy.adapter.read()
                back = found.state == "absent"
            else:
                agy.adapter.store().write(doc["before"])
                found = agy.adapter.read()
                back = found.state == "ok" and found.email == doc["before_email"]
            agy.noted_write(found)
        except Exception:      # a keychain that does not answer, a record that does not read: Recover decides
            back = False
            agy.noted_write(None)
        if back:
            ctx.vault.remove(tx_key)
            return DeckError(f"{str(error).rstrip('.')}. The earlier sign-in is back.", error.code)
        try:
            ctx.vault.put(tx_key, {**ctx.vault.get(tx_key), "status": "conflict"})
        except DeckError:
            pass
        return DeckError(f"{str(error).rstrip('.')}. Choose Recover to decide which sign-in agy keeps.", "recovery")


def dispatch(engine, name, body):
    """Runs an Antigravity switch-family action (switching.dispatch): {"plan": …} for switch-plan, {"op": id} for one
    that starts an operation, DeckError otherwise (confirm_required with a fresh plan, as the other apps do)."""
    ctx, agy = context(engine), engine.agy
    account = body.get("id")
    if account is not None and not isinstance(account, str):
        raise DeckError("Choose an account.", "invalid")
    if name == "switch-plan":
        purpose = body.get("purpose", "switch")
        if purpose not in PURPOSES:
            raise DeckError("That Antigravity action is unavailable.", "invalid")
        wanted = account if purpose in ("switch", "add") and account != UNSAVED_ID else None
        return {"plan": plan_switch(ctx, wanted, purpose, keep=body.get("keep"))}
    if name in ("switch", "capture", "add", "cancel-login", "recover") and engine.read_only:
        raise DeckError(READ_ONLY, "read_only")
    if name == "switch":
        claimed = claim(ctx, "switch", body, account=account, provider="antigravity", build=build_plan, families=())
        if claimed.mode == "now" and claimed.plan["running"]["agy_consumers"]:
            raise DeckError("agy is running. Close it, or choose to switch when it closes.", "busy")
        target = engine.store.agy_account(account)
        if target is None:
            raise DeckError("That account no longer exists.", "not_found")
        return submit(ctx, claimed, AgySwitch(ctx, claimed, target))
    if name == "capture":
        label = body.get("label")
        claimed = claim(ctx, "capture", body, provider="antigravity", build=build_plan, families=())
        return submit(ctx, claimed, QuickOp("capture", lambda: agy.capture(label), provider="antigravity",
                                           message="Saving the Antigravity account…",
                                           step=("capture", "Saving the sign-in")))
    if name == "add":
        again = account if account != UNSAVED_ID else None
        claimed = claim(ctx, "add", body, account=again, provider="antigravity", build=build_plan, families=())
        row = engine.store.agy_account(again) if again else None
        return submit(ctx, claimed, QuickOp("add", lambda: agy.begin_enrollment(again), provider="antigravity",
                                           account=again, label=row["label"] if row else None,
                                           message="Opening agy's sign-in…", step=("add", "Opening the sign-in")))
    if name == "cancel-login":
        if not agy.enrolling:
            raise DeckError("No Antigravity sign-in is open.", "invalid")
        return engine.submit(QuickOp("cancel-login", agy.cancel_enrollment, provider="antigravity",
                                     message="Ending the Antigravity sign-in…", step=("cancel", "Ending the sign-in")))
    if name == "recover":
        keep = body.get("keep")
        if keep not in contract.AGY_RECOVERY_CHOICES:
            raise DeckError("Choose which sign-in agy keeps.", "invalid")
        if agy.recovery is None:
            raise DeckError("No Antigravity switch needs recovering.", "invalid")
        restoring = agy.restoring_refusal()
        if restoring is not None:
            raise restoring
        if keep != "keep" and agy.agy_running():
            raise DeckError(PUT_BACK_WHILE_RUNNING, "busy")
        return engine.submit(QuickOp("recover", lambda: agy.recover(keep), provider="antigravity",
                                     message="Recovering Antigravity…", step=("recover", "Recovering the sign-in")))
    raise DeckError("That Antigravity action is unavailable.", "unsupported")
