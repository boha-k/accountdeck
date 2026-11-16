"""Consent-bound Codex switching; only auth.json is exchanged (docs/mac/PLAN.md §1.8).

The consumers are what the process controller classifies (ChatGPT.app, the codex processes it bundles,
Codex CLI terminals); Codex Desktop is never signalled, and the Codex CLI is the one
`Codex.executable()` resolves."""
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import uuid

from .rules import plan_current, plan_target
from .core import DeckError, FileTransaction, is_frozen
from .diagnostics import Manifest, log
from .operations import Operation
from .platform import facts_mac
from .procs import ProcessSet, unix_time
from .providers.codex import RPC, Codex, daemon_settings, identity_key, token_state
from .sessions import valid_uuid
from .switching import DryRun, QuickOp, assemble, claim, context, note, register, submit


def processes(ctx):
    rows = ctx.procs.snapshot()
    kinds = ctx.procs.classify(rows)
    consumers = ctx.procs.codex_consumers(rows=rows)     # one look: a process seen by neither would be missed
    daemons = [row for row in kinds["codex_daemon"] if not ctx.procs.codex_elsewhere(row)]
    return consumers, daemons, ProcessSet.of(consumers + daemons)


def build_plan(ctx, account, purpose="switch", *, ignore_op=None):
    if purpose not in ("switch", "capture", "add", "finish-login", "cancel-login", "recover"):
        raise DeckError("That Codex action is unavailable.", "invalid")
    engine = ctx.engine
    if purpose == "switch" and is_frozen(engine.store.account(account)):
        raise DeckError("This account's sign-in belongs to another computer.", "read_only")
    engine.sense_active()
    views = [a for a in engine.snapshot()["accounts"] if a["provider"] == "codex"]
    current = next((a for a in views if a["active"]), None)
    target = next((a for a in views if a["id"] == account), None) if purpose == "switch" else None
    if purpose == "switch" and (target is None or not target["switchable"]):
        raise DeckError("Choose a saved Codex account.", "not_found")
    if target and target["active"]:
        raise DeckError("This account is already active.", "invalid")
    consumers, daemons, found = processes(ctx)
    blockers = []
    if engine.read_only:
        blockers.append(note("read_only", "Another AccountDeck 2 is managing your sign-ins. Quit it to make changes here."))
    if not engine.identity_known["codex"] and purpose != "recover":
        blockers.append(note("identity", "The current Codex sign-in cannot be identified. Nothing will change."))
    if engine.recovery and purpose != "recover":
        blockers.append(note("recovery", "Finish the pending recovery first."))
    if purpose == "recover" and found.pairs:
        blockers.append(note("busy", "Close Codex and its daemon before recovering the previous sign-in."))
    if (engine.login and purpose not in ("finish-login", "cancel-login")) or engine.pending and engine.pending["id"] != ignore_op:
        blockers.append(note("busy", "Finish or cancel the current action first."))
    # ctime 0 means "start time unknown" (on macOS, e.g. another user's process): unverifiable, so it blocks.
    if len(daemons) > 1 or any(not r.ctime or r.family == "?" or r.kind == "unknown" for r in consumers + daemons):
        blockers.append(note("safety", "The Codex process set cannot be verified."))
    try:
        ctx.codex.validate_store()
        if target:
            profile = ctx.vault.get(account)
            if identity_key(profile) != engine.store.account(account)["identity"]:
                raise DeckError("The saved sign-in has a different identity.", "identity")
            if target["credential"]["state"] == "reauth_required":
                raise DeckError("Sign in again before switching to this account.", "reauth_required")
            ctx.codex.restore(profile, DryRun())
    except DeckError as exc:
        blockers.append(note(exc.code, str(exc)))
    running = {"desktop": False, "sessions": [], "busy": 0, "unknown": 0, "foreign_claude": 0,
               "codex_consumers": [{"pid": r.pid, "kind": r.kind.removeprefix("codex_"),
                                    "started": unix_time(r.ctime),
                                    "parent": None} for r in consumers]}
    saved = current is not None and not current["saved"] and purpose == "switch"
    result = assemble(ctx, provider="codex", purpose=purpose, account=account, target=plan_target(target) if target else None,
                      current=plan_current(current) if current else None, headroom=None, running=running,
                      pairs=found.pairs, blockers=blockers, warnings=[], tip=None,
                      will=(["Wait for Codex sessions to close.", "Save the outgoing sign-in.",
                             "Replace only auth.json and verify the selected account."] if purpose == "switch" else
                            ["Use an isolated sign-in folder. The current Codex sign-in stays in place."]),
                      preserved=["Configuration, MCP servers, plugins and session history"], account_bound=[],
                      invisible=None, stop_tier="not_running", saves_outgoing=saved)
    result.entry["identity"] = engine.active_identity["codex"]
    return result


def plan_codex_switch(ctx, account=None, purpose="switch", *, ignore_op=None):
    return register(ctx, build_plan(ctx, account, purpose, ignore_op=ignore_op))


WAIT_EXPIRED = ("Codex sessions were still open when this switch's confirmation expired. Nothing was changed; "
                "review the switch again.")
RESTORED = "The previous Codex sign-in was restored."
KEPT = "Codex changed its sign-in after the interrupted switch, so AccountDeck left that sign-in in place and saved it."


def recover_one(ctx, tx):
    """Recover for one interrupted Codex switch (`tx`); True when Codex keeps what it holds now. auth.json gets the
    login from before the switch while it still holds the one the switch wrote. When Codex wrote another since (it
    renewed the login it was given, or was signed in again), that one is newer than anything the record could put
    back, so it stays: it is saved under the account its own tokens name, and the record is closed ("kept"), as a
    record is never put back once it was found in conflict. Nothing is written to auth.json then, so no Codex
    session is touched. The login from before the switch was saved by the switch before it wrote anything."""
    try:
        tx.rollback()
        return False
    except DeckError as exc:
        if exc.code != "recovery":
            raise
    try:
        ctx.accounts.capture("codex", owner="v2")
    except DeckError as exc:
        # Signed out, or a login only another computer's AccountDeck saves: nothing to keep here. Anything else
        # (the vault, the keychain) leaves the record open for the next Recover.
        if exc.code not in ("login_needed", "read_only"):
            raise
    tx.doc["status"] = "kept"
    tx.save()
    log("codex.recover_kept")
    return True


class CodexSwitch(Operation):
    preemptible = True        # nothing changes while it waits for Codex sessions to close (the "wait" step)

    def __init__(self, ctx, claimed, target):
        super().__init__("switch", provider="codex", account=target["id"], label=target["label"],
                         consent=claimed.mode, token=claimed.token,
                         steps=(("wait", "Waiting for Codex sessions"), ("save", "Saving the current sign-in"),
                                ("restore", "Restoring the selected sign-in"), ("verify", "Checking the sign-in")))
        self.ctx, self.claimed, self.target = ctx, claimed, target

    def run(self):
        ctx, engine = self.ctx, self.ctx.engine
        manifest = Manifest.start(self.id, "switch", directory=Path(ctx.paths.data_dir) / "logs" / "manifests")
        self.report["manifest"] = self.id
        manifest.set("consent.mode", self.consent).set("consent.pair_count", len(self.token.pairs))
        tx, stopped, outcome = None, False, "failed"
        codex_exe = ctx.codex.executable()
        ctx.scheduler.pause("codex")
        try:
            with ExitStack() as stack:
                for row in engine.store.accounts():
                    if row["provider"] == "codex":
                        lock = ctx.scheduler.account_lock(row["id"])
                        if not lock.acquire(timeout=ctx.timings.lock_wait):
                            raise DeckError("A Codex usage check is still running.", "busy")
                        stack.callback(lock.release)
                self.target = engine.store.account(self.account)
                if self.target is None:
                    raise DeckError("That account no longer exists.", "not_found")
                if is_frozen(self.target):
                    raise DeckError("This account's sign-in belongs to another computer.", "read_only")
                with self.step("wait", phase="waiting_idle", message="Waiting for Codex sessions to close…"):
                    allowed = set(self.token.pairs)
                    while True:
                        consumers, daemons, found = processes(ctx)
                        if not set(found.pairs) <= allowed:
                            raise DeckError("The Codex process set changed. Review the switch again.", "needs_reconfirm",
                                            plan=plan_codex_switch(ctx, self.account, ignore_op=self.id))
                        if not consumers:
                            break
                        if self.consent != "idle":
                            raise DeckError("Close the Codex sessions or choose Wait.", "busy")
                        if time.time() >= self.token.expires:
                            # The confirmation covers the wait: a switch confirmed long ago never starts unasked.
                            raise DeckError(WAIT_EXPIRED, "stale_plan")
                        self.set_waiting([{"pid": r.pid, "name": r.name, "status": "busy"} for r in consumers])
                        self.sleep(.25)
                    engine.sense_active()
                    if not engine.identity_known["codex"] or engine.active_identity["codex"] != self.claimed.entry["identity"]:
                        raise DeckError("The outgoing sign-in changed. Review the switch again.", "identity")
                    if engine.read_only:
                        raise DeckError("Codex sign-ins are read-only.", "read_only")
                    if time.time() >= self.token.expires:
                        raise DeckError("The switch confirmation expired. Review it again.", "stale_plan")
                    # The wait ends before the cancel is checked: a cancel, or a preempt (allowed only while
                    # waiting), that came first ends the job here; after the phase change both are refused.
                    self.set_phase("stopping", "Stopping the Codex daemon…", cancellable=False)
                    self.checkpoint()
                    if daemons:
                        result = ctx.procs.codex_daemon_stop(codex_exe, ctx.paths.codex_home,
                                                            daemon_settings(ctx.paths.codex_home))
                        if result == "timeout":
                            raise DeckError("The Codex daemon is still running. Nothing was changed.", "busy")
                        stopped = True
                    if processes(ctx)[2].pairs:
                        raise DeckError("Codex is still running. Nothing was changed.", "busy")
                with self.step("save", phase="saving", message="Saving the current Codex sign-in…"):
                    if engine.active_identity["codex"] is not None:
                        ctx.accounts.capture("codex", owner="v2")
                with self.step("restore", phase="restoring", message="Restoring the selected sign-in…"):
                    if processes(ctx)[2].pairs:
                        raise DeckError("Codex started again. Nothing was changed.", "busy")
                    active = ctx.codex.active_identity()
                    if active.status == "unknown" or active.identity != self.claimed.entry["identity"]:
                        raise DeckError("The outgoing sign-in changed.", "identity")
                    profile = ctx.accounts.codex_profile(self.target)
                    if identity_key(profile) != self.target["identity"]:
                        raise DeckError("The saved sign-in identity changed.", "identity")
                    remaining = token_state(profile, ctx.clock()).remaining
                    if remaining is not None and remaining < 86400:
                        reading = ctx.accounts.renew_codex(self.target, profile)
                        if reading.error is not None:
                            raise reading.error
                        profile = ctx.vault.get(self.account)
                    if processes(ctx)[2].pairs or engine.read_only:
                        raise DeckError("The process set changed before the swap. Review it again.", "busy")
                    # The renewal took seconds: what is saved over must still be what was saved above.
                    active = ctx.codex.active_identity()
                    if active.status == "unknown" or active.identity != self.claimed.entry["identity"]:
                        raise DeckError("The outgoing sign-in changed while the target was checked. Review it again.", "identity")
                    tx = FileTransaction(ctx.vault, engine.roots())
                    tx.doc.update(provider="codex", purpose="switch")
                    ctx.codex.restore(profile, tx)
                with self.step("verify", phase="verifying", message="Checking the selected Codex account…"):
                    if ctx.codex.active_identity().identity != self.target["identity"]:
                        raise DeckError("Codex did not retain the selected sign-in.", "identity")
                    reading = ctx.codex.usage(profile, mode="read_only")
                    if getattr(reading.error, "kind", None) in ("identity", "unauthorized", "refresh_rejected"):
                        self.report.update(verified="failed", verify_reason="The provider rejected the selected sign-in.")
                        raise DeckError("The selected Codex sign-in could not be verified.", "identity")
                    verified = "verified" if reading.error is None and reading.usage is not None else "unconfirmed"
                    self.report.update(verified=verified, verified_at=ctx.clock() if verified == "verified" else None,
                                       verify_reason=None if verified == "verified" else "The provider check is unavailable; the local identity matches.",
                                       summary=f"Codex is ready as {self.label}." +
                                       (" The outgoing account was saved." if self.claimed.plan["saves_outgoing"] else ""))
                    tx.commit()
                    manifest.set("verification.result", verified)
                    outcome = "success"
        except BaseException:
            if tx is not None and tx.doc["status"] != "complete":
                try:
                    if processes(ctx)[2].pairs:
                        tx.doc["status"] = "conflict"
                        tx.save()
                    else:
                        tx.rollback()
                finally:
                    # Left for Recover, which holds the renewal of the login it puts back (engine.login_held). Set
                    # before reads of Codex resume below, so that login is not renewed in between.
                    engine.recovery = engine._scan_recovery()
            raise
        finally:
            if stopped:
                ctx.procs.codex_daemon_start(codex_exe, ctx.paths.codex_home)
            ctx.scheduler.resume_provider("codex")
            manifest.set("outcome", outcome).finish()


def dispatch(engine, name, body):
    ctx = context(engine)
    account = body.get("id")
    if name == "switch-plan":
        return {"plan": plan_codex_switch(ctx, account, body.get("purpose", "switch"))}
    if name == "recover":
        claimed = claim(ctx, "recover", body, provider="codex", build=build_plan)
        def recover():
            if processes(ctx)[2].pairs or engine.read_only:
                raise DeckError("Close Codex before recovering.", "busy")
            kept = False
            for key in engine.recovery:
                tx = FileTransaction.load(ctx.vault, engine.roots(), key)
                if tx.doc.get("provider") == "codex":
                    kept = recover_one(ctx, tx) or kept
            return KEPT if kept else RESTORED
        return submit(ctx, claimed, QuickOp("recover", recover, provider="codex", message="Recovering Codex…",
                                           step=("recover", "Restoring the previous sign-in")))
    if name in ("add", "finish-login", "cancel-login"):
        claimed = claim(ctx, name, body, provider="codex", build=build_plan, families=ctx.paths.codex_families)
        def login_action():
            if name == "add":
                flow = Enrollment(ctx, device=body.get("device_code") is True or body.get("method") == "device_code")
                engine.login = {"provider": "codex", "started": ctx.clock(), "url": flow.url,
                                "user_code": flow.user_code, "_flow": flow}
                engine.closers.append(flow.close)
                return "Open the sign-in page, then choose Save Sign-In. Your current account stays in place."
            flow = (engine.login or {}).get("_flow")
            if flow is None:
                raise DeckError("No Codex sign-in is waiting.", "invalid")
            if name == "finish-login":
                flow.save()
            flow.close()
            engine.login = None
            return "Codex account saved." if name == "finish-login" else "Sign-in cancelled."
        return submit(ctx, claimed, QuickOp(name, login_action, provider="codex", message="Updating the Codex sign-in…",
                                           step=("login", "Codex sign-in")))
    if name in ("switch", "capture"):
        claimed = claim(ctx, name, body, account=account if name == "switch" else None,
                        provider="codex", build=build_plan, families=ctx.paths.codex_families)
        if name == "switch":
            return submit(ctx, claimed, CodexSwitch(ctx, claimed, engine.store.account(account)))
        def capture():
            ctx.accounts.capture("codex", label=body.get("label"), owner="v2")
            return "The current Codex account was saved."
        return submit(ctx, claimed, QuickOp("capture", capture, provider="codex", message="Saving Codex…",
                                           step=("capture", "Saving the sign-in")))
    if name == "open":
        if engine.read_only or engine.pending or engine.recovery:
            raise DeckError("Finish the current action before opening Codex.", "busy")
        session = body.get("session")
        if session is not None and not valid_uuid(session):
            raise DeckError("Choose a valid session.", "invalid")
        ctx.procs.launch_codex(ctx.codex.executable(), ctx.paths.codex_home, ctx.settings.get("cwd"), session)
        return {}
    if name == "stop-consumer":
        entry = engine.plans.get(body.get("plan_id"))
        if not entry or entry["provider"] != "codex" or engine.clock() >= entry["expires"]:
            raise DeckError("Review the Codex switch again.", "stale_plan")
        claimed = claim(ctx, entry["purpose"], {**body, "consent": "now"}, account=entry["account"],
                        provider="codex", build=build_plan, families=ctx.paths.codex_families)
        row = next((r for r in ctx.procs.codex_consumers() if r.pid == body.get("pid")), None)
        if row is None:
            raise DeckError("That Codex session has ended.", "not_found")
        def stop():
            ctx.procs.stop_consumer(claimed.token, row)
            return "The selected Codex session was stopped."
        return submit(ctx, claimed, QuickOp("stop-consumer", stop, provider="codex", message="Stopping the selected session…",
                                           step=("stop", "Stopping the selected session")))
    raise DeckError("Sign in using Codex, then save the signed-in account here.", "unsupported")


class Enrollment:
    """A fresh helper home prevents the official login flow from revoking the current account."""
    def __init__(self, ctx, *, device=False):
        self.ctx, self.rpc, self.closed = ctx, None, False
        work = Path(ctx.paths.data_dir) / "work"
        work.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="login-", dir=work))
        self.url, self.user_code = None, None
        self.adapter = Codex(replace(ctx.paths, codex_home=self.home), ctx.procs)
        ctx.codex.setup_home(self.home)
        if ctx.engine.mode == "demo":
            from .config import Paths
            from .providers.fake import build_fake_home
            fake = Paths.synthetic(self.home / "synthetic")
            build_fake_home(fake, claude_identity=str(uuid.uuid4()), claude_org=str(uuid.uuid4()),
                            codex_identity=str(uuid.uuid4()))
            self.adapter = Codex(fake, ctx.procs)
            return
        try:
            self.rpc = RPC(ctx.codex.command(), self.home, procs=ctx.procs, grace=ctx.timings.helper_refresh_grace)
            response = self.rpc.call("account/login/start", {"type": "chatgptDeviceCode" if device else "chatgpt"})
            self.url = response.get("authUrl") or response.get("verificationUrl")
            self.user_code = response.get("userCode")
        except BaseException:
            self.close()
            raise

    def save(self):
        profile = self.adapter.current()
        info = self.adapter.identity(profile)
        self.ctx.accounts.save("codex", profile, info, source="login", owner="v2")

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.rpc is not None:
            self.rpc.close()
        # Preserve any completed sign-in before deleting a helper home, even on cancel/shutdown.
        auth = self.adapter.codex_home / "auth.json"
        if auth.exists():
            try:
                profile = self.adapter.current()
                self.ctx.accounts.save("codex", profile, self.adapter.identity(profile), source="login", owner="v2")
            except Exception:
                self.ctx.vault.quarantine("codex-login", auth.read_bytes())
        facts_mac.remove_work_directory(self.home, Path(self.ctx.paths.data_dir) / "work")
