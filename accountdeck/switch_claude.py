"""Claude Desktop's switch flows (PLAN §5): the plan, the stop ladder, capture, restore, relaunch and the
hand-off to verification.

Every job that stops Claude Desktop (switch, quit, capture, add, finish-login, cancel-login, recover) runs
the same way. `dispatch` builds a plan the user confirms, `switching.claim` binds a consent token to that
plan and to the verified process set, and the job runs on the engine's operation runner. The job
re-checks the process set, stops Claude through `stop_ladder.StopLadder` (exactly one attempt) and
touches the sign-in files only through a `FileTransaction`. Until the files are replaced, any failure or
cancel reopens Claude with the account it had. Nothing here signals or starts a process itself: every
signal goes through `procs`. Whether Claude came back as the target is `claude_verify`'s question.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
import shutil
import time

from . import activity, contract, diagnostics, switch_codex
from .accounts import HEALTH_MESSAGES
from .claude_verify import (NOT_OPENED, OPEN_IT, BackgroundVerifier, Verdict, Verifier, cache_org, config_keys,
                            files_present, generation_digest, log_size, log_text, mcp_names, token_cache_summary,
                            _SERVER_COUNT)
from .rules import (DORMANT_DAYS, PRESERVED, age_text, clock_text, countdown_text, headroom_line, plan_current,
                    plan_target)
from .core import DeckError, FileTransaction, is_frozen
from .diagnostics import Manifest
from .operations import Operation, OperationCancelled
from .platform import facts_mac
from .procs import MAX_DESKTOP_ROWS, ProcessSet, desktop_main
from .sessions import sync_sessions, valid_uuid
from .stop_ladder import (MANUAL_MESSAGE, NOT_RELEASED_MESSAGE, NOT_RUNNING_MESSAGE, NOT_STOPPED_MESSAGE,
                          RECONFIRM_MESSAGE, SELF_PROTECT_MESSAGE, TOO_MANY_MESSAGE, UNKNOWN_MESSAGE, graceful_first)
from .switching import (BUSY_MESSAGE, DryRun, Planned, QuickOp, assemble, claim, context, note, register,
                        submit)

MIN_DISK_FREE = 50 * 1024 * 1024
DORMANT_WARN_DAYS = 14           # the strong wording starts at rules.DORMANT_DAYS
SESSION_EXPIRING_DAYS = 3
TIP_RESET_SECONDS = 900          # a tip only when the current window refills this soon ...
TIP_MARGIN = 25                  # ... and switching would gain less than this many points
ENROLLMENT = "claude_enrollment"

OTHER_APP_MESSAGE = "Another AccountDeck 2 is managing your sign-ins. Quit it to make changes here."
RECOVERY_MESSAGE = "Finish the pending recovery first."
LOGIN_BUSY_MESSAGE = "Finish or cancel the current sign-in first."
NO_APP_MESSAGE = ("AccountDeck cannot find Claude in Applications, so it left Claude alone. "
                  "Install Claude in the Applications folder, then try again.")
REOPENED = "Claude was reopened with the same account."
NOT_REOPENED = f"Claude was closed and could not be reopened by AccountDeck. {OPEN_IT}."
STILL_QUITTING_MESSAGE = "Claude is still quitting, so AccountDeck could not open it again yet."
NOT_FOUND_MESSAGE = "AccountDeck cannot find Claude in Applications."
CHROME_HOSTS = ("chrome-native-host",)
CHROME_HINT = "If Chrome is open, close it first: the Claude in Chrome connector can keep Claude from reopening."
ADD_HINT = ("Sign in with the account you intend to add; a browser already signed into another Claude "
            "account can approve the wrong one.")
INCIDENT_WARNING = "Claude is reporting problems; sign-in checks may fail. Verification will keep retrying."
SAFETY_MESSAGES = (SELF_PROTECT_MESSAGE, UNKNOWN_MESSAGE, TOO_MANY_MESSAGE)
LAUNCH_FAILURES = ("system", "not_installed")         # DeckError codes of a launch macOS refused


# ---- one look at Claude's processes ----

@dataclass(frozen=True)
class Observed:
    """One look at Claude's processes: the rows a stop would cover and what the Code sessions are doing."""
    desktop: list
    code: list
    unknown: list
    report: object
    process_set: ProcessSet
    main: object
    self_protect: bool


def observe(ctx) -> Observed:
    procs = ctx.procs
    kinds = procs.classify(procs.snapshot())
    desktop, code = kinds["claude_desktop"], kinds["claude_code"]
    report = activity.report(ctx.paths, procs, now=time.time(), idle_hold_s=ctx.timings.idle_hold, kinds=kinds)
    mine = {row.pid for row in desktop + code}
    protected = bool({row.pid for row in procs.ancestors_of_self()} & mine)
    return Observed(desktop, code, kinds["claude_unknown"], report, ProcessSet.of(desktop + code),
                    desktop_main(desktop), protected)


def claude_login(engine):
    login = engine.login
    return login if isinstance(login, dict) and login.get("provider") == "claude" else None


# ---- the plan (§5.2) ----

def plan_claude_switch(ctx, account_id=None, purpose="switch", *, ignore_op=None) -> dict:
    """The side-effect-free plan the user confirms, registered for the claim that follows."""
    planned = build_plan(ctx, account_id, purpose, ignore_op=ignore_op)
    register(ctx, planned)
    return planned.plan


def build_plan(ctx, account_id, purpose, *, ignore_op=None) -> Planned:
    """`plan_claude_switch` without registering the plan; a job re-plans with its own id in `ignore_op`."""
    if purpose not in contract.PURPOSES:
        raise DeckError("That action is not available.", "invalid")
    engine = ctx.engine
    if purpose == "switch" and is_frozen(engine.store.account(account_id)):
        raise DeckError("This account's sign-in belongs to another computer.", "read_only")
    engine.sense_active()
    state = engine.snapshot()
    now = engine.clock()
    views = {view["id"]: view for view in state["accounts"] if view["provider"] == "claude"}
    current = next((view for view in views.values() if view["active"]), None)
    observed = observe(ctx)
    target = _check_purpose(engine, views, observed, current, account_id, purpose)
    blockers = _blockers(ctx, observed, purpose, ignore_op)
    warnings = []
    pre = Preflight.inspect(ctx, target) if purpose == "switch" else None
    if pre is not None:
        blockers += pre.blockers
    if purpose != "quit":
        blockers += _disk_blocker(ctx)
    if engine.status("claude", now)["level"] in ("degraded", "outage"):
        warnings.append(note("incident", INCIDENT_WARNING))
    if target is not None:
        warnings += _target_warnings(target, now)
        warnings += pre.warnings
    warnings += _session_warnings(observed.report)
    saves = current is not None and current["id"] in contract.UNSAVED_IDS.values() and purpose in ("switch", "add")
    sessions = observed.report.sessions
    running = {"desktop": bool(observed.desktop),
               "sessions": [{"pid": s.pid, "name": s.name, "status": s.status, "since": s.since} for s in sessions],
               "busy": observed.report.busy, "unknown": observed.report.unknown,
               "foreign_claude": observed.report.foreign, "codex_consumers": []}
    planned = assemble(
        ctx, provider="claude", purpose=purpose, account=account_id if purpose == "switch" else None,
        target=plan_target(target) if target is not None else None,
        current=plan_current(current) if current is not None else None,
        headroom=({"now": headroom_line(current, now) or None if current is not None else None,
                   "after": headroom_line(target, now) or None} if target is not None else None),
        running=running, pairs=observed.process_set.pairs, blockers=blockers, warnings=warnings,
        tip=_tip(current, target, now) if target is not None else None,
        will=_will(ctx, purpose, observed, current, target, saves),
        preserved=list(PRESERVED),
        account_bound=([f"Hosted connectors (may ask you to sign in once on {target['label']})",
                        "Claude in Chrome pairing and Remote Control"] if target is not None else []),
        invisible="Cowork and Chat activity cannot be detected." if observed.desktop else None,
        stop_tier=_stop_tier(ctx, observed), saves_outgoing=saves)
    diagnostics.log("plan", provider="claude", purpose=purpose, busy=running["busy"], unknown=running["unknown"],
                    foreign=running["foreign_claude"], tier=planned.plan["stop_tier"], blockers=len(blockers))
    planned.entry["identity"] = engine.active_identity["claude"]
    planned.entry["activity"] = sorted((s.pid, s.status) for s in sessions)
    return planned


def _check_purpose(engine, views, observed, current, account_id, purpose):
    """The target account of a switch; DeckError when the plan cannot even be shown."""
    if purpose == "switch":
        target = views.get(account_id)
        if target is None:
            raise DeckError("That account no longer exists.", "not_found")
        if target["active"]:
            raise DeckError(f"You're already on {target['label']}.", "invalid")
        if not target["switchable"]:
            raise DeckError("Save this account once while Claude is closed, then switch to it.", "login_needed")
        if target["credential"]["state"] == "reauth_required":
            raise DeckError(f"Claude no longer accepts {target['label']}'s saved sign-in. Sign in to it again.",
                            "reauth_required")
        return target
    if purpose == "quit" and not observed.desktop:
        raise DeckError("Claude is not running.", "invalid")
    if purpose == "recover" and not engine.recovery:
        raise DeckError("There is nothing to recover.", "invalid")
    if purpose in ("finish-login", "cancel-login") and claude_login(engine) is None:
        raise DeckError("No sign-in is waiting.", "invalid")
    if purpose == "capture" and current is None:
        raise DeckError("Sign in to Claude Desktop first.", "login_needed")
    return None


def _blockers(ctx, observed, purpose, ignore_op):
    engine, found = ctx.engine, []
    if not engine.owns_login:
        found.append(note("read_only", OTHER_APP_MESSAGE))
    if not engine.identity_known["claude"] and purpose not in ("recover", "quit"):
        found.append(note("identity", "The current Claude sign-in cannot be identified. Nothing will change."))
    if engine.recovery and purpose != "recover":
        found.append(note("recovery", RECOVERY_MESSAGE))
    pending = engine.pending
    if pending is not None and pending["id"] != ignore_op:
        found.append(note("busy", BUSY_MESSAGE))
    login = engine.login if isinstance(engine.login, dict) else None
    if login is not None and purpose not in ("finish-login", "cancel-login") and (
            login.get("provider") == "claude" or purpose == "add"):
        found.append(note("busy", LOGIN_BUSY_MESSAGE))
    if observed.self_protect:
        found.append(note("safety", SELF_PROTECT_MESSAGE))
    if observed.unknown:
        found.append(note("safety", UNKNOWN_MESSAGE))
    if observed.report.unknown:
        found.append(note("busy", "A Claude session's activity cannot be verified. Wait until its status is known."))
    if len(observed.desktop) > MAX_DESKTOP_ROWS:
        found.append(note("safety", TOO_MANY_MESSAGE))
    return found


def _disk_blocker(ctx):
    try:
        if shutil.disk_usage(ctx.paths.data_dir).free >= MIN_DISK_FREE:
            return []
        message = "Less than 50 MB is free on the drive AccountDeck saves to. Free some space, then try again."
    except OSError:
        message = "AccountDeck could not check the free space on its drive, so it left Claude alone."
    return [note("system", message)]


def _stop_tier(ctx, observed):
    if observed.self_protect or observed.unknown or len(observed.desktop) > MAX_DESKTOP_ROWS:
        return "manual"
    if not observed.desktop:
        return "not_running"
    return "graceful" if graceful_first(ctx.paths, ctx.procs, observed.main) else "managed"


# ---- opening Claude (§5.7; docs/mac/PLAN.md §1.4) ----

def launch_target(ctx):
    """Claude's bundle id, read from the installed Claude.app (what `open -b` opens); None when Claude is
    not installed."""
    return facts_mac.claude_launch_id(ctx.paths)


def start_claude(ctx, target, *, relaunch=True):
    """Opens Claude through procs. Claude holds a single-instance lock, so a quitting instance must be gone
    first; then `launch_claude(bundle id)` (`open -g -b`, in the background) for a relaunch, or
    `activate_app(bundle id)` (`open -b`) when the user asked to open it."""
    procs = ctx.procs
    if not target:
        raise DeckError(NOT_FOUND_MESSAGE, "not_installed")
    if not procs.wait_until(lambda: not procs.claude_desktop(), ctx.timings.terminate_wait, interval=0.25):
        raise DeckError(STILL_QUITTING_MESSAGE, "system")
    return procs.launch_claude(target) if relaunch else procs.activate_app(target)


def _target_warnings(target, now):
    label, found = target["label"], []
    if target["age_tier"] == "stale" and target["checked"] is not None:
        found.append(note("stale", f"{label}'s usage was last read {age_text(now - target['checked'])} ago."))
    for window in target["windows"]:
        if window["state"] == "out" and window["primary"]:
            name = window["label"].lower()
            until = f" until {clock_text(window['reset'], now)}" if window["reset"] is not None else ""
            found.append(note("limited", f"{label}'s {name} limit is at 100%{until}."))
    return found


def _names(sessions, limit=3):
    names = [f'"{s.name}"' if s.name else f"session {s.pid}" for s in sessions]
    text = ", ".join(names[:limit])
    return text + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _session_warnings(report):
    found = []
    busy = [s for s in report.sessions if s.status == "busy"]
    if busy:
        head = ("1 Code session is working" if len(busy) == 1 else f"{len(busy)} Code sessions are working")
        found.append(note("busy", f"{head}: {_names(busy)}."))
    if report.unknown:
        found.append(note("unknown", "1 session's status is unknown; it is treated as working." if report.unknown == 1
                           else f"{report.unknown} sessions' status is unknown; they are treated as working."))
    if report.foreign:
        found.append(note("foreign", "1 Claude Code process outside Claude Desktop stays untouched."
                           if report.foreign == 1 else
                           f"{report.foreign} Claude Code processes outside Claude Desktop stay untouched."))
    return found


def _headroom(view):
    lefts = [w["left"] for w in view["windows"] if w["kind"] in ("session", "weekly") and w["left"] is not None]
    return min(lefts) if lefts else None


def _tip(current, target, now):
    """Only when waiting is nearly free: the current account refills within 15 minutes and switching gains little."""
    if current is None:
        return None
    binding = next((w for w in current["windows"] if w["binding"]), None)
    if binding is None or binding["reset"] is None or not 0 < binding["reset"] - now <= TIP_RESET_SECONDS:
        return None
    have, gain = _headroom(current), _headroom(target)
    if have is None or gain is None or gain - have >= TIP_MARGIN:
        return None
    return (f"{current['label']} refills in {countdown_text(binding['reset'] - now)}. "
            "Waiting costs nothing; switching restarts Claude.")


def _close_line(observed):
    count = len(observed.report.sessions)
    if not count:
        return "Close Claude"
    return f"Close Claude ({count} Code session{'s' if count != 1 else ''} will end)"


def _will(ctx, purpose, observed, current, target, saves):
    close = [_close_line(observed)] if observed.desktop else []
    if current is None:
        save = []
    elif saves:
        save = [f"Save {current['label']} so you can switch back to it"]
    else:
        save = ["Save the current sign-in"]
    if purpose == "switch":
        return close + save + [f"Restore {target['label']}'s sign-in", "Carry over your local Code session list",
                               "Reopen Claude and check the sign-in"]
    if purpose == "quit":
        return close
    if purpose == "add":
        return close + save + ["Open Claude's sign-in screen", "Save the new account after you sign in"]
    if purpose == "finish-login":
        return close + ["Save the account you signed in to", "Carry over your local Code session list",
                        "Reopen Claude and check the sign-in"]
    if purpose == "cancel-login":
        return close + [_undo_line(ctx), "Reopen Claude"]
    if purpose == "recover":
        return close + ["Put your previous sign-in files back", "Reopen Claude"]
    return close + ["Save the current sign-in"] + (["Reopen Claude"] if observed.desktop else [])


def _undo_line(ctx):
    enrollment = ctx.store.setting(ENROLLMENT) or {}
    previous = ctx.store.account(enrollment.get("previous")) if enrollment.get("previous") else None
    return f"Restore {previous['label']}'s sign-in" if previous else "Put the sign-in Claude had before back"


# ---- preflight (§5.3) ----

@dataclass
class Inspection:
    blockers: list
    warnings: list
    profile: dict | None


class Preflight:
    """Read-only checks of a saved account before anything is stopped."""

    @classmethod
    def run(cls, ctx, target):
        """(blockers, warnings) for restoring `target` (a state account view or any dict with `id` and `label`)."""
        found = cls.inspect(ctx, target)
        return found.blockers + _disk_blocker(ctx), found.warnings

    @staticmethod
    def inspect(ctx, target) -> Inspection:
        row = ctx.store.account(target["id"])
        label = row["label"] if row else target["label"]
        unusable = note("vault", f"{label}'s saved sign-in is unusable. Reconnect it.")
        if row is None:
            return Inspection([note("not_found", "That account no longer exists.")], [], None)
        claude = ctx.claude
        try:
            found = ctx.vault.newest_good(row["id"], lambda p: claude.health(p) == "ok" and bool(p.get("cookies")))
        except DeckError:
            found = None
        if found is None:
            return Inspection([unusable], [], None)
        profile = found[1]
        if profile.get("identity") != row["identity"]:
            return Inspection([note("identity", "The saved sign-in belongs to a different account.")], [], None)
        try:
            claude.restore(profile, DryRun())
        except DeckError as exc:
            code = exc.code if exc.code in contract.ERROR_CODES else "vault"
            return Inspection([note(code, str(exc))], [], profile)
        except (OSError, ValueError):
            return Inspection([unusable], [], profile)
        now, warnings = ctx.clock(), []
        expiry = claude.sessionkey_expiry(profile)
        if expiry is not None and expiry <= now:
            warnings.append(note("session_expired", f"{label}'s web sign-in has expired; Claude may ask you to sign in again."))
        elif expiry is not None and expiry - now < SESSION_EXPIRING_DAYS * 86400:
            warnings.append(note("session_expiring", f"{label}'s web sign-in expires in {countdown_text(expiry - now)}."))
        seen = (row["flags"] or {}).get("last_active_at") or profile.get("captured")
        days = int((now - seen) / 86400) if isinstance(seen, (int, float)) else 0
        if days >= DORMANT_DAYS:
            warnings.append(note("dormant", f"{label} has not been opened for {days} days; its sign-in has probably expired."))
        elif days >= DORMANT_WARN_DAYS:
            warnings.append(note("dormant", f"{label} has not been opened for {days} days; its sign-in may have expired."))
        return Inspection([], warnings, profile)


def restore_profile(ctx, account_id, tx) -> dict:
    """Writes the newest good saved profile of `account_id` into Claude's data folder through `tx`;
    returns the profile. DeckError `vault` when no saved copy can be used."""
    claude = ctx.claude
    found = ctx.vault.newest_good(account_id, lambda p: claude.health(p) == "ok" and bool(p.get("cookies")))
    if found is None:
        raise DeckError("This account's saved sign-in is unusable. Reconnect it.", "vault")
    row = ctx.store.account(account_id)
    if row is None or found[1].get("identity") != row["identity"]:
        raise DeckError("The saved sign-in identity changed.", "identity")
    claude.restore(found[1], tx)
    return found[1]


# ---- the jobs (§5.4): what every job that stops Claude has in common ----

class _Stages:
    """Drives the wait, stop and settle steps from the stop ladder's progress calls. A ladder callback must
    not enter `op.step` (that runs a checkpoint inside the ladder), so the steps are moved by hand."""

    ORDER = ("wait", "stop", "settle")
    PHASES = {"wait": ("waiting_idle", "Waiting for Claude to finish…"),
              "stop": ("stopping", "Closing Claude…"),
              "settle": ("settling", "Waiting for Claude to let go of its files…")}

    def __init__(self, job, ladder):
        self.job, self.ladder, self.active = job, ladder, None

    def __call__(self, step_id, detail):
        if step_id != self.active:
            self._begin(step_id)
        self.job.note(detail, step_id=step_id)
        if step_id == "wait":
            self.job.set_waiting(self.ladder.waiting_on)

    def _begin(self, step_id):
        job, skipped = self.job, self._skipped_reason()
        with job.lock:
            now = job.clock()
            for earlier in self.ORDER[:self.ORDER.index(step_id)]:
                row = job.row(earlier)
                if row.status == "active":
                    row.status, row.ended = "done", now
                elif row.status == "pending":
                    row.status, row.detail, row.ended = "skipped", skipped, now
            row = job.row(step_id)
            row.status, row.started, row.ended = "active", now, None
            job.current_step = step_id
        self.active = step_id
        job.set_phase(*self.PHASES[step_id])

    def close(self, result):
        """Settles the three steps once the ladder is done."""
        job = self.job
        if result.tier == "not_running":
            final, unseen = "skipped", NOT_RUNNING_MESSAGE
        elif result.ok:
            final, unseen = "done", self._skipped_reason()
        elif result.tier in ("cancelled", "reconfirm"):
            final, unseen = "skipped", ""
        else:
            final, unseen = "failed", ""
        if final == "failed" and self.active is None:
            self._begin("stop")
        with job.lock:
            now = job.clock()
            for step_id in self.ORDER:
                row = job.row(step_id)
                if row.status == "active":
                    row.status, row.ended = final, now
                elif row.status == "pending":
                    row.status, row.detail, row.ended = "skipped", unseen, now
            if final != "failed":
                job.current_step = None
        job.set_phase(None)

    def _skipped_reason(self):
        return "You chose to go ahead now." if self.job.token.mode == "now" else "Nothing was working."


class ClaudeJob(Operation):
    """Base of every job that stops Claude Desktop. A subclass sets `purpose` (also the operation kind) and
    implements `_body`; the helpers here are the shared steps: prepare (S0), the ladder (S1-S3), saving the
    sign-in Claude is leaving (S4), the launch and the verification (S7-S8)."""

    purpose = ""
    reopens = True       # closing Claude is never the end of a job: a failure before the files change reopens it
    preemptible = True   # nothing changes while the ladder waits for idle Code sessions (stop_ladder T0)

    def __init__(self, ctx, claimed, *, steps, account=None, label=None):
        super().__init__(self.purpose, provider="claude", account=account, label=label, consent=claimed.mode,
                         steps=steps, token=claimed.token)
        self.ctx, self.confirmed = ctx, claimed.entry
        self.plan_set = ProcessSet(tuple(tuple(pair) for pair in claimed.entry["process_set"]))
        self.was_running = bool(claimed.plan["running"]["desktop"])
        self.app_id = self.tx = self.manifest = self.profile = None    # app_id: Claude's bundle id
        self.paused = self.opened = False
        self.launch_attempted = False
        self.kept = None          # {"id", "label"}: the account whose sign-in S4 saved or kept
        self.saved = None         # ("new" | "updated", label) once S4 saved a sign-in
        self.launch_problem = None
        self.mcp_before = {}
        self.hosted_count = None

    # ---- running ----
    def run(self):
        ctx = self.ctx
        self.report["manifest"] = self.id
        self.manifest = Manifest.start(self.id, self.kind, directory=Path(ctx.paths.data_dir) / "logs" / "manifests")
        self.manifest.set("consent.mode", self.token.mode).set("consent.pair_count", len(self.token.pairs))
        outcome, code = "failed", None
        try:
            self._body()
            outcome = "success"
        except DeckError as exc:
            self._rollback_unlaunched()
            outcome, code = {"cancelled": "cancelled", "needs_reconfirm": "needs_reconfirm"}.get(exc.code, "failed"), exc.code
            raise
        except BaseException:
            self._rollback_unlaunched()
            code = "internal"
            raise
        finally:
            if self.paused:
                ctx.scheduler.resume_provider("claude")
            self.manifest.set("outcome", outcome)
            if code:
                self.manifest.set("error_code", code)
            self.manifest.finish()

    def _rollback_unlaunched(self):
        if (self.purpose == "switch" and self.tx is not None and not self.launch_attempted
                and self.tx.doc.get("status") not in ("complete", "rolled_back")):
            current = observe(self.ctx)
            if current.desktop or current.code or current.unknown:
                self.tx.doc["status"] = "conflict"
                self.tx.save()
                return
            self.tx.rollback()
            self._reopen_unchanged()

    def _body(self):
        raise NotImplementedError

    def row(self, step_id):
        return next(step for step in self.steps if step.id == step_id)

    # ---- S0 prepare ----
    def _prepare(self, *, refresh=False):
        ctx = self.ctx
        with self.step("prepare", phase="planning", message="Checking everything is ready…"):
            fresh = build_plan(ctx, self.confirmed["account"], self.purpose, ignore_op=self.id)
            plan = fresh.plan
            if plan["blockers"]:
                first = plan["blockers"][0]
                raise DeckError(first["message"], first["code"])
            if (fresh.entry["signature"], fresh.entry["saves_outgoing"]) != (
                    self.confirmed["signature"], self.confirmed["saves_outgoing"]):
                register(ctx, fresh)
                raise DeckError(RECONFIRM_MESSAGE, "needs_reconfirm", plan=plan)
            if fresh.entry.get("identity") != self.confirmed.get("identity"):
                raise DeckError("The outgoing sign-in changed. Review the switch again.", "identity")
            if self.token.mode == "now" and fresh.entry.get("activity") != self.confirmed.get("activity"):
                register(ctx, fresh)
                raise DeckError(RECONFIRM_MESSAGE, "needs_reconfirm", plan=plan)
            self._ready(plan)
            ctx.scheduler.pause("claude")
            self.paused = True
            self._wait_for_reads(plan)
            if self.reopens:
                self.app_id = launch_target(ctx)
                if self.app_id is None and self.was_running:
                    raise DeckError(NO_APP_MESSAGE, "not_installed")
            self.mcp_before = mcp_names(ctx.paths, ctx.claude)
            self._header()
            if refresh:
                self._refresh_outgoing(plan)

    def _ready(self, plan):
        """Extra checks of a subclass, made before anything is paused or stopped."""

    def _wait_for_reads(self, plan):
        """A usage read of an account this job touches must have finished: the job takes and releases its lock."""
        scheduler, wait = self.ctx.scheduler, self.ctx.timings.lock_wait
        for view in (plan["target"], plan["current"]):
            if view is None or view["id"] in contract.UNSAVED_IDS.values():
                continue
            lock = scheduler.account_lock(view["id"])
            if not lock.acquire(timeout=wait):
                raise DeckError("A usage check is still running for this account. Try again in a moment.", "busy")
            lock.release()

    def _header(self):
        ctx, manifest = self.ctx, self.manifest
        folder, kind, reason = facts_mac.claude_base(ctx.paths)
        manifest.set("data_folder.path_kind", kind).set("data_folder.reason", reason)
        manifest.set("mainlog.offset_before", log_size(ctx.paths))
        manifest.set("config_keys_before", config_keys(ctx.claude))
        manifest.set("files_present", files_present(ctx.paths))
        manifest.save()

    def _refresh_outgoing(self, plan):
        """While Claude still runs, saves a fresh generation of the saved account it is signed in to (tokens only,
        its cookies stay as saved), so a settings journal torn by the stop still leaves a good copy."""
        current = plan["current"]
        if current is None or current["id"] in contract.UNSAVED_IDS.values():
            return
        try:
            self.ctx.accounts.capture("claude", claude_running=True, owner="v2")
        except DeckError as exc:
            diagnostics.log("capture.skipped", stage="pre_stop", code=exc.code)

    # ---- S1-S3 the ladder ----
    def _stop(self):
        """Runs the stop ladder once. Returns its result when Claude is closed; anything else ends the job."""
        ctx = self.ctx
        if self.token.mode == "now":
            statuses = sorted((s.pid, s.status) for s in observe(ctx).report.sessions)
            if statuses != self.confirmed.get("activity", []):
                raise DeckError(RECONFIRM_MESSAGE, "needs_reconfirm",
                                plan=plan_claude_switch(ctx, self.account, self.purpose, ignore_op=self.id))
        ladder = ctx.ladder()
        stages = _Stages(self, ladder)
        with self._guard():
            result = ladder.run(self.token, self.plan_set, cancel=self.cancel_event, progress=stages)
            stages.close(result)
            self._record_stop(result)
            self._refuse(result)
        return result

    def _refuse(self, result):
        if result.ok:
            return
        if result.tier == "reconfirm":
            fresh = build_plan(self.ctx, self.confirmed["account"], self.purpose, ignore_op=self.id)
            register(self.ctx, fresh)
            raise DeckError(RECONFIRM_MESSAGE, "needs_reconfirm", plan=fresh.plan)
        if result.tier == "cancelled":
            raise OperationCancelled()
        if result.tier == "expired":
            raise DeckError(result.detail, "stale_plan")
        if result.detail == NOT_STOPPED_MESSAGE:
            raise DeckError(f"{result.detail} {MANUAL_MESSAGE}", "busy")
        raise DeckError(result.detail, "busy" if result.detail == NOT_RELEASED_MESSAGE else "safety")

    def _record_stop(self, result):
        tier = result.tier if result.tier in contract.STOP_TIERS else ("manual" if result.tier == "aborted" else None)
        self.report.update(stop_tier=tier, stop_ms=int(result.elapsed * 1000),
                           helpers_self_exited=result.helpers_self_exited, stragglers=result.stragglers)
        manifest = self.manifest
        manifest.set("stop.tier", result.tier).set("stop.ms", int(result.elapsed * 1000))
        manifest.set("stop.helpers_self_exited", result.helpers_self_exited).set("stop.stragglers", result.stragglers)
        manifest.set("stop.settled", result.settled).set("stop.markers", list(result.markers))
        manifest.save()

    @contextlib.contextmanager
    def _guard(self):
        """Until the sign-in files change, a failure or a cancel leaves Claude the way it was: closed by this job
        means reopened with the same account."""
        try:
            yield
        except OperationCancelled:
            if self._reopen_unchanged():
                raise OperationCancelled(f"Cancelled. {REOPENED}") from None
            raise
        except DeckError as exc:
            if self._reopen_unchanged() and exc.code != "needs_reconfirm":
                raise DeckError(f"{exc} {REOPENED}", exc.code, exc.retry_after, plan=exc.plan) from None
            raise
        except BaseException:
            self._reopen_unchanged()
            raise

    def _reopen_unchanged(self) -> bool:
        """Opens Claude again when this job closed it and it is not running; True when Claude was started."""
        procs = self.ctx.procs
        if not (self.reopens and self.was_running) or procs.claude_desktop():
            return False
        try:
            start_claude(self.ctx, self.app_id)
        except (DeckError, OSError) as exc:
            diagnostics.log("launch.reopen_failed", lvl="warning", code=getattr(exc, "code", "os"))
            self.ctx.engine.notice(NOT_REOPENED, "warning")
            return False
        return True

    def _point_of_no_return(self, message):
        """The last cancellation point: from here the job finishes or puts everything back."""
        self.set_phase("restoring", message, cancellable=False)
        with self._guard():
            self.checkpoint()

    def _undo(self, exc):
        """A write failed: the transaction puts the files back, Claude reopens as it was and the job ends."""
        reason = str(exc) if isinstance(exc, DeckError) and exc.code != "safety" else "The sign-in files could not be written."
        try:
            self.tx.rollback()
        except OSError:
            raise DeckError("Your previous sign-in could not be put back. Use Recover to restore it.", "recovery") from None
        text = f"{reason} Your previous sign-in is back."
        if self._reopen_unchanged():
            text += f" {REOPENED}"
        code = exc.code if isinstance(exc, DeckError) and exc.code in contract.ERROR_CODES else "busy"
        raise DeckError(text, code)

    def _new_transaction(self, **fields):
        tx = FileTransaction(self.ctx.vault, self.ctx.engine.roots())
        tx.doc.update(provider="claude", purpose=self.purpose,
                      outgoing_id=self.kept["id"] if self.kept else None, **fields)
        tx.save()
        return tx

    # ---- S4 save the sign-in Claude holds ----
    def _save_current(self, *, required=False, label=None):
        """Saves the sign-in Claude is leaving, under the health gate. A signed-in account that is not saved yet is
        saved as a new one, so a switch never loses a sign-in (§9.4). `required` (capture, finish-login): anything
        but a healthy sign-in fails the job."""
        ctx, claude, manifest = self.ctx, self.ctx.claude, self.manifest
        required = required or self.confirmed.get("saves_outgoing", False)
        with self._guard(), self.step("capture", phase="saving", message="Saving the current sign-in…"):
            try:
                profile = claude.capture(heal_copy_dir=Path(ctx.paths.data_dir) / "work")
                health, reason = claude.diagnose(profile)
            except (DeckError, OSError) as exc:
                signed_out = getattr(exc, "code", "") in ("login_needed", "not_installed")
                profile, health = None, ("logged_out" if signed_out else "unreadable")
            manifest.set("capture.health", health)
            if profile is not None:
                self._record_files(profile)
            identity = valid_uuid(profile.get("identity")) if profile is not None else None
            if self.purpose in ("switch", "add") and identity != self.confirmed.get("identity"):
                raise DeckError("The outgoing sign-in changed. Nothing was replaced.", "identity")
            row = ctx.store.find("claude", identity) if identity else None
            if health == "ok":
                self._store(profile, row, label)
            else:
                self._not_saved(health, row, required)
            manifest.save()
        self.profile = profile
        return profile

    def _record_files(self, profile):
        manifest = self.manifest
        journal, heal = profile.get("journal") or {}, profile.get("cookie_heal") or {}
        for key in ("present", "consistent", "used"):
            manifest.set(f"journal.{key}", journal.get(key))
        manifest.set("cookies.hot_journal", heal.get("hot_journal")).set("cookies.healed", heal.get("healed"))

    def _store(self, profile, row, label):
        ctx = self.ctx
        if row is not None:
            info = {"identity": row["identity"], "email": row["email"], "plan": row["plan"], "org": row["org"]}
        else:
            info = self._identity_info(profile)
        profile["org"] = info["org"]
        key = ctx.accounts.save("claude", profile, info, label=label if row is None else None, source="switch",
                                owner="v2")
        account = ctx.store.account(key)
        self.kept = {"id": key, "label": account["label"]}
        self.saved = ("updated" if row is not None else "new", account["label"])
        self.manifest.set("capture.saved", self.saved[0]).set("capture.generation", generation_digest(ctx.vault, key))
        self.note(f"Saved {account['label']}." if row is None else f"Updated {account['label']}'s saved sign-in.")

    def _identity_info(self, profile):
        """Who the provider says the signed-in account is; when it cannot be asked, what the unsaved row showed."""
        ctx = self.ctx
        try:
            info = ctx.accounts.identity("claude", profile)
        except DeckError as exc:
            if exc.code == "identity":
                raise DeckError(HEALTH_MESSAGES["identity_mismatch"], "login_needed") from None
            unsaved = ctx.accounts.unsaved.get("claude") or {}
            info = {"identity": profile["identity"], "email": unsaved.get("email") or "", "plan": unsaved.get("plan") or "",
                    "org": ""}
        except (OSError, ValueError):
            info = {"identity": profile["identity"], "email": "", "plan": "", "org": ""}
        if not info.get("org"):
            info["org"] = cache_org(ctx.claude, profile)
        return info

    def _not_saved(self, health, row, required):
        manifest = self.manifest
        manifest.set("capture.reason", health)
        message = HEALTH_MESSAGES.get(health, HEALTH_MESSAGES["unreadable"])
        if required:
            raise DeckError(message, "login_needed")
        if row is not None:
            self.kept = {"id": row["id"], "label": row["label"]}
            manifest.set("capture.saved", "kept")
            self.note(f"Couldn't save {row['label']}'s latest sign-in; the copy saved earlier is kept.", status="warning")
        elif health in ("logged_out", "half_login", "sentinel"):
            manifest.set("capture.saved", "skipped")
            self.note("Claude wasn't signed in to an account, so there was nothing to save.", status="skipped")
        else:
            raise DeckError(message + " Nothing was changed.", "login_needed")

    # ---- S7 launch and S8 verify ----
    def _launch_claude(self) -> bool:
        """Opens Claude by its bundle id, once more with a freshly read one; False when macOS would not
        start it."""
        ctx = self.ctx
        self.launch_attempted = True
        try:
            try:
                start_claude(ctx, self.app_id)
            except DeckError as exc:
                if exc.code not in LAUNCH_FAILURES:
                    raise
                self.app_id = launch_target(ctx)
                start_claude(ctx, self.app_id)
        except DeckError as exc:
            if exc.code not in LAUNCH_FAILURES:
                raise
            self.launch_problem = str(exc)
            found = False
        else:
            found = True
        last = getattr(ctx.procs, "last_launch", None) or {}
        self.manifest.set("launch.method", last.get("method"))
        self.manifest.set("launch.exit", last.get("exit"))
        self.manifest.set("launch.retries", last.get("retries"))
        return found

    def _unopened_reason(self):
        reason = self.launch_problem or NOT_OPENED
        if any(row.name.lower() in CHROME_HOSTS for row in self.ctx.procs.snapshot()):
            reason += " " + CHROME_HINT
        return reason

    def _open(self, *, identity=None, label=None, restored_cipher=None):
        """S7 (and S8 with an identity to check). Returns the Verifier when a check ran, else None."""
        ctx, manifest = self.ctx, self.manifest
        verifier = None
        offset = log_size(ctx.paths)
        if identity is not None:
            verifier = Verifier(ctx, identity=identity, label=label, restored_cipher=restored_cipher,
                                log_offset=offset, manifest=manifest)
        with self.step("launch", phase="launching", message="Opening Claude…"):
            opened = self._launch_claude()
            manifest.set("mainlog.offset_after_launch", log_size(ctx.paths))
            entries = None
            if opened:
                waiter = verifier or Verifier(ctx, identity=None, label=label or "", restored_cipher=None,
                                              log_offset=offset)
                opened, entries = waiter.wait_ready(self)
            manifest.set("mainlog.token_cache_entries_on_load", entries)
            self._read_log_facts(offset)
            self.opened = opened
            if not opened:
                self.note(self._unopened_reason(), status="warning")
            elif entries == 0 and verifier is not None:
                self.note("Claude loaded no saved tokens, so it will sign in again by itself.", status="warning")
        return verifier

    def _read_log_facts(self, offset):
        text = log_text(self.ctx.paths, offset)
        matched = [name for name, needle in (("ipc", "Account details received via IPC"),
                                             ("initialized", "Initialization succeeded"),
                                             ("token_cache", "loaded token cache from disk")) if needle in text]
        self.manifest.set("mainlog.matched", matched)
        counts = {}
        for found in _SERVER_COUNT.finditer(text):
            kind = "first_session_server_count" if found.group(0).lower().startswith("servercount") else "sdk_total_servers"
            counts.setdefault(kind, int(found["n"]))
        for kind in ("first_session_server_count", "sdk_total_servers"):
            self.manifest.set(f"mainlog.{kind}", counts.get(kind))
        self.hosted_count = counts.get("first_session_server_count")

    def _verify(self, verifier, *, label, summary, tx):
        """S8: the hard window, then "verified", "unconfirmed" (a background check follows) or a failure."""
        ctx, manifest = self.ctx, self.manifest
        if not self.opened:
            self.note("Claude did not open, so there was nothing to check yet.", step_id="verify", status="skipped")
            verdict = Verdict("pending", "not_open", self._unopened_reason())
            ctx.engine.notice(NOT_OPENED, "warning")
        else:
            with self.step("verify", phase="verifying", message="Checking that Claude signed in…"):
                verdict = verifier.run(self)
                self._token_cache_after()
        manifest.set("verification.result", {"verified": "verified", "pending": "unconfirmed"}.get(verdict.state, "failed"))
        manifest.set("verification.reason", verdict.code)
        manifest.set("token_cache_ciphertext_changed", verifier.cipher_changed)
        if verdict.state == "verified":
            self.report.update(verified="verified", verified_at=ctx.clock(), verify_reason=None)
            return "verified"
        if verdict.state == "pending":
            self.report.update(verified="unconfirmed", verify_reason=verdict.message)
            ctx.engine.background_verifiers.append(BackgroundVerifier(
                ctx, verifier, op_id=self.id, account=self.account, label=label, summary=summary,
                tx_key=tx.key if tx is not None else None, manifest=manifest, started=ctx.clock()))
            return "unconfirmed"
        self.report.update(verified="failed", verify_reason=verdict.message)
        raise DeckError(f"{verdict.message} Recovery is available.", "identity")

    def _token_cache_after(self):
        try:
            live = self.ctx.claude.capture(config_only=True)
        except DeckError:
            return
        summary = token_cache_summary(self.ctx.claude, live, self.ctx.clock())
        if summary is not None:
            self.manifest.set("token_cache.after_launch", summary)
        self.manifest.set("config_keys_after", config_keys(self.ctx.claude))


    # ---- S6 the local Code session list ----
    def _sync_sessions(self, tx, account):
        """Carries the local Code session list over to `account` (a store row). A failure is a warning, never a
        reason to undo a sign-in that has been written. Returns the counts, or None."""
        ctx, manifest = self.ctx, self.manifest
        with self.step("sessions", phase="restoring", message="Carrying over your Code session list…", cancellable=False):
            try:
                found = sync_sessions(ctx.claude.base, ctx.paths.claude_projects, account["identity"],
                                      account["org"] or "", tx)
            except (DeckError, OSError) as exc:
                manifest.set("sessions.error", getattr(exc, "code", "os"))
                self.note(f"Couldn't carry over the session list. {exc}" if isinstance(exc, DeckError)
                          else "Couldn't carry over the session list.", status="warning")
                return None
            manifest.set("sessions.added", found["added"]).set("sessions.updated", found["updated"])
            manifest.set("sessions.removed", found["deleted"]).set("sessions.stripped_fields", found["stripped_fields"])
            self.note(f"{found['added']} added, {found['updated']} updated, {found['deleted']} removed")
        return found

    def _carried(self, sessions):
        """The report's "carried over" and "account-bound" lists, and the connections part of the manifest."""
        ctx = self.ctx
        after = mcp_names(ctx.paths, ctx.claude)
        unchanged = after == self.mcp_before
        self.manifest.set("connections.local_mcp_names_unchanged", unchanged)
        self.manifest.set("connections.hosted_count", self.hosted_count)
        names = after["chat"]
        chat = (f"{len(names)} server{'s' if len(names) != 1 else ''}: " + ", ".join(names[:5])
                + (f" and {len(names) - 5} more" if len(names) > 5 else "")) if names else "None configured"
        carried = []
        if sessions is not None:
            carried.append({"label": "Local Code sessions", "status": "carried",
                            "detail": f"{sessions['added']} added, {sessions['updated']} updated, {sessions['deleted']} removed"})
        carried.append({"label": "Local MCP servers (Claude chat)", "status": "carried", "detail": chat})
        carried.append({"label": "Claude Code MCP definitions", "status": "carried",
                        "detail": "unchanged" if after["code"] == self.mcp_before.get("code") else "changed"})
        return carried

    def _saved_sentence(self):
        return f" Saved {self.saved[1]} so you can switch back to it." if self.saved and self.saved[0] == "new" else ""


class ClaudeSwitch(ClaudeJob):
    """The switch of PLAN §5.4: prepare, stop, save the outgoing sign-in, restore the target's, carry the session
    list over, reopen Claude and verify that it signed in as the target."""

    purpose = "switch"
    STEPS = (("prepare", "Checking {label}'s saved sign-in"), ("wait", "Waiting for Claude to finish"),
             ("stop", "Closing Claude"), ("settle", "Waiting for Claude to let go of its files"),
             ("capture", "Saving the current sign-in"), ("restore", "Restoring {label}"),
             ("sessions", "Carrying over Code sessions"), ("launch", "Opening Claude"),
             ("verify", "Checking the sign-in"), ("report", "Done"))

    def __init__(self, ctx, claimed, target):
        steps = [(sid, text.format(label=target["label"])) for sid, text in self.STEPS]
        super().__init__(ctx, claimed, steps=steps, account=target["id"], label=target["label"])
        self.target = target

    def _body(self):
        target = self.target
        self._prepare(refresh=True)
        self._stop()
        self._save_current()
        self._point_of_no_return("Restoring the saved sign-in…")
        tx, profile = self._restore()
        sessions = self._sync_sessions(tx, target)
        tx.doc["status"] = "verifying"
        tx.save()
        verifier = self._open(identity=target["identity"], label=target["label"],
                              restored_cipher=profile["auth"].get("oauth:tokenCacheV2"))
        summary = f"Claude opened as {target['label']}." + self._saved_sentence()
        state = self._verify(verifier, label=target["label"], summary=summary, tx=tx)
        with self.step("report"):
            tx.commit()
            self.report["carried"] = self._carried(sessions)
            self.report["account_bound"] = [
                {"label": "Hosted connectors", "detail": f"May ask you to sign in once on {target['label']}.",
                 **({"count": self.hosted_count} if self.hosted_count is not None else {})},
                {"label": "Claude in Chrome and Remote Control", "detail": "Pair them again if they stop working."}]
            if state == "verified":
                self.report["summary"] = summary
            elif self.opened:
                self.report["summary"] = (f"Claude opened as {target['label']}. Still confirming the sign-in in the "
                                          "background." + self._saved_sentence())
            else:
                self.report["summary"] = (f"{target['label']}'s sign-in is in place, but Claude did not open by "
                                          "itself. AccountDeck will confirm it once Claude is open."
                                          + self._saved_sentence())

    def _restore(self):
        """S5: the target's saved sign-in goes into Claude's data folder through one transaction."""
        ctx, target = self.ctx, self.target
        with self.step("restore", phase="restoring", message="Restoring the saved sign-in…", cancellable=False):
            tx = self.tx = self._new_transaction(target_id=target["id"])
            try:
                profile = restore_profile(ctx, target["id"], tx)
            except (DeckError, OSError, ValueError) as exc:
                self._undo(exc)
            self.manifest.set("config_keys_after", config_keys(ctx.claude))
            found = token_cache_summary(ctx.claude, profile, ctx.clock())
            if found is not None:
                self.manifest.set("token_cache.before_launch", found)
            self.manifest.save()
        return tx, profile


class QuitClaude(ClaudeJob):
    """`quit-claude`: S0-S3 only. Claude stays closed."""

    purpose = "quit"
    reopens = False

    def __init__(self, ctx, claimed):
        super().__init__(ctx, claimed, steps=(("prepare", "Checking Claude"), ("wait", "Waiting for Claude to finish"),
                                              ("stop", "Closing Claude"),
                                              ("settle", "Waiting for Claude to let go of its files"),
                                              ("report", "Done")))

    def _body(self):
        self._prepare()
        self._stop()
        with self.step("report"):
            self.report["summary"] = "Claude was closed."


class CaptureClaude(ClaudeJob):
    """`capture` with Claude closed first, so the cookie database is complete and the account becomes switchable."""

    purpose = "capture"

    def __init__(self, ctx, claimed, label=None):
        super().__init__(ctx, claimed, steps=(
            ("prepare", "Checking Claude"), ("wait", "Waiting for Claude to finish"), ("stop", "Closing Claude"),
            ("settle", "Waiting for Claude to let go of its files"), ("capture", "Saving the current sign-in"),
            ("launch", "Opening Claude"), ("report", "Done")))
        self.requested = label

    def _body(self):
        self._prepare()
        self._stop()
        self._save_current(required=True, label=self.requested)
        if self.was_running:
            self._open()
        else:
            self.note("Claude was not running.", step_id="launch", status="skipped")
        with self.step("report"):
            kind, label = self.saved
            self.report["summary"] = (f"Saved {label}." if kind == "new" else f"Updated {label}'s saved sign-in.") + (
                " Claude was reopened." if self.was_running and self.opened else "")


class AddClaude(ClaudeJob):
    """Add account, part one: save the sign-in Claude holds, sign Claude out and open its sign-in screen."""

    purpose = "add"

    def __init__(self, ctx, claimed):
        super().__init__(ctx, claimed, steps=(
            ("prepare", "Checking Claude"), ("wait", "Waiting for Claude to finish"), ("stop", "Closing Claude"),
            ("settle", "Waiting for Claude to let go of its files"), ("capture", "Saving the current sign-in"),
            ("clear", "Signing Claude out"), ("launch", "Opening Claude"), ("report", "Done")))

    def _body(self):
        ctx = self.ctx
        self._prepare(refresh=True)
        self._stop()
        self._save_current()
        self._point_of_no_return("Signing Claude out for the new account…")
        started = ctx.clock()
        with self.step("clear", phase="restoring", message="Signing Claude out for the new account…", cancellable=False):
            tx = self.tx = self._new_transaction(status="enrolling")
            ctx.store.set_setting(ENROLLMENT, {"transaction": tx.key, "previous": self.kept["id"] if self.kept else None,
                                               "started": started})
            try:
                ctx.claude.clear_for_login(tx)
            except (DeckError, OSError, ValueError) as exc:
                ctx.store.set_setting(ENROLLMENT, None)
                self._undo(exc)
            self.manifest.set("config_keys_after", config_keys(ctx.claude)).save()
        ctx.engine.login = {"provider": "claude", "started": started}
        self._open()
        with self.step("report"):
            self.report["summary"] = ("Claude is open on its sign-in screen. " + ADD_HINT if self.opened else
                                      f"Claude is signed out and ready for the new account. {OPEN_IT}. "
                                      + ADD_HINT) + self._saved_sentence()


def _enrollment(ctx) -> dict:
    found = ctx.store.setting(ENROLLMENT)
    return found if isinstance(found, dict) else {}


def _end_enrollment(ctx):
    ctx.store.set_setting(ENROLLMENT, None)
    ctx.engine.login = None


class _Enrolled(ClaudeJob):
    """Finish-login and cancel-login work on the transaction the add-account flow started."""

    def _enrollment_transaction(self):
        key = _enrollment(self.ctx).get("transaction")
        if isinstance(key, str) and key:
            try:
                return FileTransaction.load(self.ctx.vault, self.ctx.engine.roots(), key)
            except DeckError:
                pass
        return self._new_transaction(status="enrolling")


class FinishLogin(_Enrolled):
    """Add account, part two: the user signed in; save that account, carry the session list over and reopen."""

    purpose = "finish-login"

    def __init__(self, ctx, claimed):
        super().__init__(ctx, claimed, steps=(
            ("prepare", "Checking the sign-in"), ("wait", "Waiting for Claude to finish"), ("stop", "Closing Claude"),
            ("settle", "Waiting for Claude to let go of its files"), ("capture", "Saving the new account"),
            ("sessions", "Carrying over Code sessions"), ("launch", "Opening Claude"),
            ("verify", "Checking the sign-in"), ("report", "Done")))

    def _ready(self, plan):
        """Nothing is stopped until Claude shows a finished sign-in."""
        try:
            live = self.ctx.claude.capture(config_only=True)
            health = self.ctx.claude.diagnose(live)[0]
        except (DeckError, OSError):
            health = "logged_out"
        if health != "ok":
            raise DeckError("Sign in to Claude first, then choose Finish. " + (
                "" if health == "logged_out" else HEALTH_MESSAGES[health]), "login_needed")

    def _body(self):
        ctx = self.ctx
        self._prepare()
        self._stop()
        profile = self._save_current(required=True)
        account = ctx.store.account(self.kept["id"])
        tx = self.tx = self._enrollment_transaction()
        self._point_of_no_return("Saving the new account…")
        sessions = self._sync_sessions(tx, account)
        tx.commit()
        _end_enrollment(ctx)
        verifier = self._open(identity=account["identity"], label=account["label"],
                              restored_cipher=profile["auth"].get("oauth:tokenCacheV2"))
        summary = f"Saved {account['label']}. Claude opened as {account['label']}."
        state = self._verify(verifier, label=account["label"], summary=summary, tx=tx)
        with self.step("report"):
            self.report["carried"] = self._carried(sessions)
            self.report["summary"] = summary if state == "verified" else (
                f"Saved {account['label']}. Still confirming the sign-in in the background.")


class CancelLogin(_Enrolled):
    """Add account, cancelled: Claude gets the sign-in it had before the add (or none, when it had none)."""

    purpose = "cancel-login"

    def __init__(self, ctx, claimed):
        super().__init__(ctx, claimed, steps=(
            ("prepare", "Checking Claude"), ("wait", "Waiting for Claude to finish"), ("stop", "Closing Claude"),
            ("settle", "Waiting for Claude to let go of its files"), ("restore", "Putting the previous sign-in back"),
            ("launch", "Opening Claude"), ("report", "Done")))

    def _body(self):
        ctx = self.ctx
        self._prepare()
        self._stop()
        previous = _enrollment(ctx).get("previous")
        self._point_of_no_return("Putting the previous sign-in back…")
        with self.step("restore", phase="restoring", message="Putting the previous sign-in back…", cancellable=False):
            tx = self.tx = self._enrollment_transaction()
            try:
                if previous and ctx.store.account(previous) is not None:
                    restore_profile(ctx, previous, tx)
                elif previous:
                    raise DeckError("The previous account is unavailable.", "vault")
                else:
                    ctx.claude.clear_for_login(tx)
            except (DeckError, OSError, ValueError):
                self._reopen_unchanged()
                raise DeckError("The previous account is unavailable. Your encrypted backup was retained.",
                                "recovery") from None
            tx.commit()
            _end_enrollment(ctx)
            self.manifest.set("config_keys_after", config_keys(ctx.claude))
        if self.was_running:
            self._open()
        else:
            self.note("Claude was not running.", step_id="launch", status="skipped")
        with self.step("report"):
            self.report["summary"] = "The sign-in was cancelled. Your previous sign-in is back." + (
                " Claude was reopened." if self.was_running and self.opened else "")


class Recover(ClaudeJob):
    """Puts the sign-in files back to what they were before a switch whose transaction never finished."""

    purpose = "recover"
    preemptible = False   # an interrupted switch is put right first; auto-switch waits for Recover anyway

    def __init__(self, ctx, claimed):
        super().__init__(ctx, claimed, steps=(
            ("prepare", "Checking what to recover"), ("wait", "Waiting for Claude to finish"), ("stop", "Closing Claude"),
            ("settle", "Waiting for Claude to let go of its files"), ("restore", "Putting the previous sign-in back"),
            ("launch", "Opening Claude"), ("report", "Done")))

    def _body(self):
        ctx = self.ctx
        self._prepare()
        pending = self._load_pending()
        involves_claude = any(tx.doc.get("provider") != "codex" for tx in pending)
        if involves_claude:
            self._stop()
        else:
            for step_id in ("wait", "stop", "settle"):
                self.note("Claude isn't involved.", step_id=step_id, status="skipped")
        self._point_of_no_return("Putting the previous sign-in back…")
        kept_sessions = False
        with self.step("restore", phase="restoring", message="Putting the previous sign-in back…", cancellable=False):
            for tx in pending:
                kept_sessions = self._recover_one(tx) or kept_sessions
            self.manifest.set("recovered", len(pending)).set("config_keys_after", config_keys(ctx.claude))
        if involves_claude and self.was_running:
            self._open()
        else:
            self.note("Claude was not running." if involves_claude else "Claude isn't involved.", step_id="launch",
                      status="skipped")
        with self.step("report"):
            self.report["summary"] = "Your previous sign-in is back." + (
                " Code sessions started since were kept." if kept_sessions else "") + (
                " Claude was reopened." if involves_claude and self.was_running and self.opened else "")

    def _load_pending(self):
        ctx, found = self.ctx, []
        for key in list(ctx.engine.recovery):
            try:
                found.append(FileTransaction.load(ctx.vault, ctx.engine.roots(), key))
            except DeckError:
                raise DeckError("A recovery backup could not be read. Your encrypted backups were retained.",
                                "recovery") from None
        return found

    def _recover_one(self, tx) -> bool:
        """Puts one interrupted transaction right. True when Code sessions created since were kept."""
        ctx = self.ctx
        if tx.doc.get("provider") == "codex":
            if ctx.procs.codex_consumers() or ctx.procs.codex_daemon():
                raise DeckError("Close your Codex sessions, then try again.", "busy")
            switch_codex.recover_one(ctx, tx)
            return False
        outgoing = tx.doc.get("outgoing_id")
        if not outgoing or ctx.store.account(outgoing) is None:
            tx.rollback()
            return False
        self._keep_live_tokens()
        undo = self._new_transaction(status="prepared")
        restore_profile(ctx, outgoing, undo)
        undo.commit()
        status = self._roll_back_sessions(tx)
        tx.doc["status"] = status
        tx.save()
        return status == "preserved_newer_sessions"

    def _keep_live_tokens(self):
        """The account Claude opened may have renewed its tokens since it was saved; keep that copy when it is healthy."""
        ctx = self.ctx
        try:
            profile = ctx.claude.capture(heal_copy_dir=Path(ctx.paths.data_dir) / "work")
            identity = valid_uuid(profile.get("identity"))
            row = ctx.store.find("claude", identity) if identity else None
            if row is not None and ctx.claude.diagnose(profile)[0] == "ok":
                info = {"identity": row["identity"], "email": row["email"], "plan": row["plan"], "org": row["org"]}
                profile["org"] = row["org"]
                ctx.accounts.save("claude", profile, info, source="recover", owner="v2")
        except (DeckError, OSError) as exc:
            diagnostics.log("capture.skipped", stage="recover", code=getattr(exc, "code", "os"))

    def _roll_back_sessions(self, original) -> str:
        """Undoes only the Code session list changes of an interrupted switch; sessions created since stay."""
        ctx = self.ctx
        changes = [c for c in original.doc["changes"] if "claude-code-sessions" in Path(c["path"]).parts]
        if not changes:
            return "rolled_back"
        partial = FileTransaction(ctx.vault, ctx.engine.roots())
        partial.doc["changes"] = changes
        try:
            partial.rollback()
        except DeckError:
            partial.doc["status"] = "preserved_newer_sessions"
            partial.save()
            return "preserved_newer_sessions"
        return "rolled_back"


def restore_enrollment(engine):
    saved = _enrollment(context(engine))
    if saved.get("transaction"):
        engine.login = {"provider": "claude", "started": saved.get("started", engine.clock())}


def dispatch(engine, name, body):
    """Claude Desktop's actions (switching.dispatch routes them here)."""
    ctx = context(engine)
    account = body.get("id")
    if name == "switch-plan":
        return {"plan": plan_claude_switch(ctx, account, body.get("purpose", "switch"))}
    if name == "verify":
        for verifier in engine.background_verifiers:
            verifier.request()
        return {}
    if name == "open":
        engine._own_the_login()
        if engine.pending or engine.recovery or engine.login:
            raise DeckError(BUSY_MESSAGE, "busy")

        def launch():
            if not ctx.procs.claude_desktop():
                start_claude(ctx, launch_target(ctx), relaunch=False)
            return "Claude is open."
        return engine.submit(QuickOp("open", launch, provider="claude", message="Opening Claude…",
                                     step=("launch", "Opening Claude")))
    purpose = {"quit-claude": "quit"}.get(name, name)
    jobs = {"quit": QuitClaude, "capture": CaptureClaude, "add": AddClaude,
            "finish-login": FinishLogin, "cancel-login": CancelLogin, "recover": Recover}
    if purpose not in jobs and purpose != "switch":
        raise DeckError("That action is not available.", "invalid")
    claimed = claim(ctx, purpose, body, build=build_plan, account=account if purpose == "switch" else None)
    if purpose == "switch":
        job = ClaudeSwitch(ctx, claimed, engine.store.account(account))
    elif purpose == "capture":
        job = CaptureClaude(ctx, claimed, body.get("label"))
    else:
        job = jobs[purpose](ctx, claimed)
    return submit(ctx, claimed, job)
