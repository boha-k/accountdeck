"""What every switch flow shares (PLAN §4.3, §5.2): the context a flow works in, the plan registry, the
consent claim, job submission and the action router.

A flow (switch_claude, switch_codex, switch_cli, switch_antigravity) builds a SwitchPlan the user confirms (`assemble`,
registered under its plan id), then `claim` checks the confirmation against a freshly built plan and
mints the ConsentToken bound to that plan's process set, and `submit` hands the job to the engine's
operation runner. `dispatch` routes an action to the provider's flow.
"""
from __future__ import annotations

from dataclasses import dataclass
import secrets
import time

from . import activity, contract, diagnostics
from .core import DeckError
from .operations import Operation
from .procs import ConsentToken, ProcessSet
from .stop_ladder import RECONFIRM_MESSAGE, StopLadder

CONFIRM_MESSAGE = "Confirm what will happen first."
STALE_PLAN_MESSAGE = "This plan has expired. Review it again."
BUSY_MESSAGE = "Finish or cancel the current action first."


@dataclass(frozen=True)
class SwitchContext:
    """The engine's members a switch flow uses (public ones only) plus a fresh stop ladder per job."""
    engine: object
    paths: object
    procs: object
    providers: dict
    vault: object
    store: object
    accounts: object
    scheduler: object
    settings: object
    timings: object
    clock: object

    @property
    def claude(self):
        return self.providers["claude"]

    @property
    def codex(self):
        return self.providers["codex"]

    def ladder(self):
        return StopLadder(self.paths, self.procs, activity, self.timings, diagnostics)


def context(engine) -> SwitchContext:
    return SwitchContext(engine, engine.paths, engine.procs, engine.providers, engine.vault, engine.store,
                         engine.accounts, engine.scheduler, engine.prefs, engine.timings, engine.clock)


def note(code, message):
    """A plan blocker or warning."""
    return {"code": code, "message": message}


class DryRun:
    """Stands in for a FileTransaction so a provider's restore() can validate a profile without writing."""

    def write(self, path, data):
        return None


# ---- the plan registry ----

@dataclass
class Planned:
    plan: dict
    entry: dict        # what engine.plans keeps for the claim: purpose, account, signature, set, expiry


def register(ctx, planned) -> dict:
    engine, now = ctx.engine, ctx.engine.clock()
    with engine.lock:
        for key, entry in list(engine.plans.items()):
            if isinstance(entry, dict) and now >= entry.get("expires", now + 1):
                del engine.plans[key]
        engine.plans[planned.plan["plan_id"]] = planned.entry
    return planned.plan


def assemble(ctx, *, provider, purpose, account, target, current, headroom, running, pairs, blockers, warnings,
             tip, will, preserved, account_bound, invisible, stop_tier, saves_outgoing=False) -> Planned:
    """The SwitchPlan of §3.4 from what a flow found, and the registry entry a later claim checks it against."""
    process_set = ProcessSet(tuple(pairs))
    idle = (running["busy"] == 0 and running["unknown"] == 0 and not running["codex_consumers"]
            and not running.get("agy_consumers"))
    plan = {"plan_id": "pl_" + secrets.token_hex(16), "expires": ctx.engine.clock() + ctx.timings.plan_ttl,
            "provider": provider, "purpose": purpose, "target": target, "current": current, "headroom": headroom,
            "running": running, "process_set": [list(pair) for pair in process_set.pairs],
            "signature": process_set.signature(), "blockers": blockers, "warnings": warnings, "tip": tip,
            "will": will, "preserved": preserved, "account_bound": account_bound, "invisible": invisible,
            "stop_tier": stop_tier,
            "confirmation_required": not (idle and ctx.settings.get("confirm_when_idle") is False),
            "idle": idle, "remember_available": idle, "default_button": "now" if idle else "idle",
            "saves_outgoing": saves_outgoing}
    entry = {"purpose": purpose, "account": account, "provider": provider, "signature": plan["signature"],
             "process_set": plan["process_set"], "expires": plan["expires"], "saves_outgoing": saves_outgoing}
    return Planned(plan, entry)


# ---- consent ----

@dataclass
class Claim:
    """A validated plan and the consent minted for it."""
    plan: dict
    entry: dict
    plan_id: str | None
    mode: str
    remember: bool
    token: ConsentToken


def claim(ctx, purpose, body, *, build, account=None, provider="claude", families=None,
          optional=False) -> Claim:
    """Checks the plan the user confirmed and mints the consent token for this job (PLAN §4.3).

    A missing plan gives `confirm_required` with a fresh one; an expired or foreign plan `stale_plan`; a
    process set that changed since the plan `needs_reconfirm` with a fresh one; a blocker refuses with its
    own code. `build(ctx, account, purpose)` is the provider's planner. `optional` lets a flow that has nothing to close (a recover with Claude closed) go without a plan."""
    engine = ctx.engine
    plan_id, consent, remember = body.get("plan_id"), body.get("consent"), body.get("remember_idle", False)
    if plan_id is not None and not (isinstance(plan_id, str) and plan_id):
        raise DeckError("That plan is not valid.", "invalid")
    if consent is not None and consent not in contract.CONSENTS:
        raise DeckError("Choose Wait for idle or Switch now.", "invalid")
    if not isinstance(remember, bool):
        raise DeckError("That setting is not valid.", "invalid")
    fresh = build(ctx, account, purpose)
    running = (fresh.plan["running"]["desktop"] or bool(fresh.plan["running"]["codex_consumers"])
               or bool(fresh.plan["running"].get("agy_consumers")))
    if plan_id is None:
        if not (optional and not running):
            register(ctx, fresh)
            raise DeckError(CONFIRM_MESSAGE, "confirm_required", plan=fresh.plan)
    else:
        with engine.lock:
            entry = engine.plans.get(plan_id)
        if (not isinstance(entry, dict) or engine.clock() >= entry["expires"]
                or (entry["purpose"], entry["account"], entry["provider"]) != (purpose, account, provider)):
            raise DeckError(STALE_PLAN_MESSAGE, "stale_plan")
    if fresh.plan["blockers"]:
        first = fresh.plan["blockers"][0]
        raise DeckError(first["message"], first["code"])
    if plan_id is not None and (fresh.entry["signature"], fresh.entry["saves_outgoing"], fresh.entry.get("identity")) != (
            entry["signature"], entry["saves_outgoing"], entry.get("identity")):
        _reissue(ctx, fresh, plan_id)
        raise DeckError(RECONFIRM_MESSAGE, "needs_reconfirm", plan=fresh.plan)
    if consent is None and plan_id is not None and fresh.plan["confirmation_required"]:
        _reissue(ctx, fresh, plan_id)
        raise DeckError(CONFIRM_MESSAGE, "confirm_required", plan=fresh.plan)
    if plan_id is not None and (consent or "now") == "now" and fresh.entry.get("activity") != entry.get("activity"):
        _reissue(ctx, fresh, plan_id)
        raise DeckError(RECONFIRM_MESSAGE, "needs_reconfirm", plan=fresh.plan)
    if remember and not fresh.plan["remember_available"]:
        raise DeckError("That choice is only available when nothing is working.", "invalid")
    mode = consent or "now"
    issued = time.time()
    token = ConsentToken(job_id="job_" + secrets.token_hex(8), provider=provider, purpose=purpose, account=account,
                         mode=mode, pairs=tuple(tuple(pair) for pair in fresh.entry["process_set"]),
                         families=tuple(families if families is not None else
                                        ((ctx.paths.claude_family,) if provider == "claude" else ())),
                         issued=issued, expires=issued + ctx.timings.consent_ttl)
    return Claim(fresh.plan, fresh.entry, plan_id, mode, remember, token)


def _reissue(ctx, fresh, old_id):
    register(ctx, fresh)
    if old_id:
        with ctx.engine.lock:
            ctx.engine.plans.pop(old_id, None)


def submit(ctx, claimed, job):
    """Starts `job`; the plan is spent, a Claude job ends the background checks of an earlier switch (it
    is about to change the files they watch), and a remembered choice is saved once the job is running."""
    engine = ctx.engine
    result = engine.submit(job)
    if claimed.plan_id:
        with engine.lock:
            engine.plans.pop(claimed.plan_id, None)
    if job.provider == "claude":
        del engine.background_verifiers[:]
    if claimed.remember:
        engine.prefs.update({"confirm_when_idle": False})
    return result


class QuickOp(Operation):
    """A short action that still runs as an operation: one at a time, visible as `pending`, ending with a report."""

    def __init__(self, kind, fn, *, provider, message, step, account=None, label=None):
        super().__init__(kind, provider=provider, account=account, label=label, steps=(step,))
        self.fn, self.working = fn, message

    def run(self):
        with self.step(self.steps[0].id, phase="saving", message=self.working, cancellable=False):
            self.report["summary"] = self.fn()


# ---- routing ----

def dispatch(engine, name, body):
    """Hands a switch-family action to its provider's flow: Antigravity's when the body names that app or one of its
    accounts, else the account's provider, the waiting sign-in's for finish/cancel-login, the interrupted
    transaction's for a recovery, else Claude Desktop."""
    from . import switch_antigravity, switch_claude, switch_cli, switch_codex
    account = body.get("id")
    if body.get("provider") == "antigravity" or engine.agy.owns(account):
        return switch_antigravity.dispatch(engine, name, body)
    row = engine.store.account(account) if isinstance(account, str) else None
    provider = body.get("provider") or (row["provider"] if row else None)
    if provider is None and name in ("finish-login", "cancel-login"):
        provider = (engine.login or {}).get("provider")
    if name == "recover" or (name == "switch-plan" and body.get("purpose") == "recover"):
        provider = _recovery_provider(engine) or provider
    provider = provider or "claude"
    if provider == "claude_cli":
        return switch_cli.dispatch(engine, name, body)
    if provider not in ("claude", "codex"):
        raise DeckError("Choose Claude or Codex.", "invalid")
    if provider == "codex" or name == "stop-consumer":
        return switch_codex.dispatch(engine, name, body)
    return switch_claude.dispatch(engine, name, body)


def _recovery_provider(engine):
    """Whose interrupted transaction a recovery puts right first: a terminal one, then Codex; None when
    only Claude Desktop's are pending (its Recover job also reports an unreadable record)."""
    found = set()
    for key in engine.recovery:
        try:
            found.add(engine.vault.get(key).get("provider"))
        except DeckError:
            continue
    return next((provider for provider in ("claude_cli", "codex") if provider in found), None)
