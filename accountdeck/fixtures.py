"""Example /api/state payloads and SwitchPlans (PLAN §3.3-3.4): every scenario the dashboard renders,
built from a few raw readings through the same rules (`accountdeck.rules`) the engine and the validator
apply, so the fixtures, the validator and the browser model cannot drift apart. The demo seeds its
synthetic accounts from these scenarios; the dashboard's tests render the written fixtures.

    python -m accountdeck.fixtures typical            # print one /api/state payload
    python -m accountdeck.fixtures --plan busy        # print one SwitchPlan
    python -m accountdeck.fixtures --write-fixtures   # regenerate tests/fixtures/state-*.json, plan-*.json
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

from . import resets, rules, updates
from .alerts import signin_due
from .cli_policy import evaluate
from .contract import ACCOUNT_PROVIDERS, PROVIDERS, UNSAVED_IDS, platform_block, validate_plan, validate_state
from .rules import PRIMARY_KINDS, REFILL_KEEP, clock_text
from .runway import forecast
from .usage import claude_reset_block, reset_credits as codex_reset_rows

BASE_NOW = 1790000000.0
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
_UTC = dt.timezone.utc

SCENARIOS = {
    "terminal": "Terminal accounts, automatic order, enrollment and Undo",
    "typical": "Mixed: out / watch / plenty / refilled / not-started, one busy session, recommendation",
    "resets-soon": "Four windows inside the 15 min / 1 h / 6 h / 24 h buckets; hero at 12m 30s; Back-in and Use-it chips",
    "refilled": "A confirmed refill 4 min old; hero kind refilled",
    "not-started": "Every 5-hour window not started; weekly windows live",
    "codex-blocked": "Codex blocked and ordinaryUsageAllowed=false, reset credits, every Codex account exhausted",
    "needs-signin": "One account reauth_required, one expired (last known 3 h), one web_only",
    "blocked-cloudflare": "error.code=blocked on two inactive accounts, state unchanged, last-known readings",
    "stale": "Readings 42 min / 3 h old, age_tier aging/stale, best state unknown",
    "fable-binding": "Fable 5.1 weekly binding at 92 % used with the account-wide weekly at 40 %",
    "resetting": "A reset passed 2 min ago (percent hidden) and expected_refill on a stale account",
    "rate-limited": "paused_until set, checks paused copy",
    "outage": "Claude outage with incident text, recommendation kept, verification unconfirmed in report",
    "switching": "pending in waiting_idle with waiting_on",
    "stopping": "pending in stopping, not cancellable",
    "report": "report with verified unconfirmed and carried / account-bound lists",
    "needs-reconfirm": "pending.phase needs_reconfirm with the fresh plan attached",
    "empty": "No accounts",
    "loading": "ready=false: first readings still in flight",
    "fifty": "50 accounts (35 Claude / 15 Codex), rows density",
    "read-only": "Another AccountDeck 2 holds the sign-ins: read_only=true with its banner",
    "demo-banner": "Demo and safe-mode banners",
    "first-run": "First start on this Mac: the terminal login saved by itself, Claude Desktop signed in but not saved",
    "ultra": "Verified Ultra: every account weekly and Fable 5.1 only; one near the threshold, one with Fable 5.1 used, one locked",
    "mixed": "Side by side: Max near its 5-hour limit, verified Ultra, temporary-limit Ultra, Ultra at 100 % weekly, needs sign-in",
    "runway": "The terminals burn fast: the pool runs out within a day; one terminal account spends usage credits",
    "update-available": "A newer AccountDeck release is out: update.available, the banner's version and release page",
    "antigravity": "Antigravity's hard parts: agy running, an unsaved account signed in, one needing a new sign-in, a "
                   "limit at 0 that has reset since, the notice about Google's terms not yet accepted",
    "resets": "Limit resets in all three apps: several rows, one not usable, a note, a blocked account, a doubt, none "
              "left; using one works in the demo (one use ends not known)",
    "antigravity-recover": "An Antigravity switch that could not finish or undo itself waits for Recover",
}
PLANS = {
    "idle": "Claude switch with only idle sessions: Switch now is the default",
    "busy": "Claude switch while a Code session is working: Switch when Claude is idle is the default",
    "blocked": "A blocker replaces the buttons",
    "quit": "Close Claude (purpose quit)",
    "add": "Add a Claude account (purpose add)",
    "codex-consumers": "Codex switch with running Codex consumers",
}
DEFAULT_SETTINGS = {
    "cli_autoswitch": True, "cli_threshold": 95, "cli_order": "soonest_reset", "cli_manual_order": [], "cli_early_switch": True,
    "usage_display": "used", "confirm_when_idle": True, "sort": "stable", "density": "auto", "pinned_model": "",
    "use_soon": False, "show_other_limits": False, "cookie_route": True, "alert_low_left": 15, "alerts_enabled": True,
    "quiet_hours": [22, 8], "notify_reset_minutes": 15, "notifications": True, "tray": True, "start_on_login": False,
    "cwd": "/Users/me", "log_level": "info", "refresh_paused": False, "update_check": True, "agy_usage_on_open": True,
    "claude_resets": True, "auto_reset": False,
}


# Window id -> (kind, label, short, model, minutes), as usage.py normalises them.
_SHAPES = {
    "five_hour": ("session", "5-hour", "5h", None, 300),
    "seven_day": ("weekly", "Weekly", "7d", None, 10080),
    "seven_day_fable": ("model", "Fable 5.1 weekly", "Fable 5.1", "Fable", 10080),
    "seven_day_oauth_apps": ("other", "Connected apps weekly", "Apps", None, 10080),
    "seven_day_cowork": ("other", "Cowork weekly", "Cowork", None, 10080),
    "codex:primary": ("session", "5-hour", "5h", None, 300),
    "codex:secondary": ("weekly", "Weekly", "7d", None, 10080),
}
_CODEX_IDS = {"five_hour": "codex:primary", "seven_day": "codex:secondary"}
_CODENAMES = ("iguana_necktie", "nimbus_quill")
_SAME = object()


def _w(wid, used, reset=None, *, blocked=None, refilled=None, samples=6):
    """A raw window reading; ``reset`` and ``refilled=(at, confirmed, previous_used)`` are offsets from now."""
    return {"id": wid, "used": used, "reset": reset, "blocked": blocked, "refilled": refilled, "samples": samples}


def _claude(aid, label, windows=(), **spec):
    return {"id": aid, "provider": "claude", "label": label, "email": f"{label.lower()}@example.test",
            "plan": "max", "windows": list(windows), **spec}


def _codex(aid, label, windows=(), **spec):
    """A Codex account; scenarios name its windows five_hour/seven_day, usage.py ids them codex:primary/secondary."""
    email = f"{label.lower().replace(' ', '.')}@example.test"
    return {"id": aid, "provider": "codex", "label": label, "email": email, "plan": "pro",
            "windows": [{**w, "id": _CODEX_IDS[w["id"]]} for w in windows], **spec}


def terminal_accounts(scenario):
    switched = scenario in ("terminal", "resets-soon")
    rows = []
    for key, label, five, weekly, reset, excluded, health in (
            ("alpha", "Alpha", 95 if switched else 60, 70, 4 * 3600, False, "ok"),
            ("beta", "Beta", 30, 60, 9 * 3600, False, "ok"),
            ("delta", "Delta", 90, 50, 5 * 3600, False, "ok"),
            ("gamma", "Gamma", 20, 20, 3 * 86400, True, "ok"),
            ("epsilon", "Epsilon", 20, 35, 2 * 86400, False, "needs_signin")):
        row = _claude("cli_" + key, label, [_w("five_hour", five, 2400), _w("seven_day", weekly, reset)],
                      active=key == ("beta" if switched else "alpha"), excluded=excluded, health=health,
                      login_ends=2 * 86400 if key == "delta" and scenario == "terminal" else 20 * 86400,
                      credential={"state": "reauth_required" if health == "needs_signin" else "fresh"})
        row["provider"] = "claude_cli"
        rows.append(row)
    if scenario == "first-run":          # the terminal login AccountDeck saved by itself on first start
        rows[0]["label"] = "alpha"
        return rows[:1]
    return rows


def _at(now, offset):
    return None if offset is None else now + offset


def _window(raw, now, at, stale):
    kind, label, short, model, minutes = _SHAPES[raw["id"]]
    used = None if raw["used"] is None else float(raw["used"])
    reset = _at(now, raw["reset"])
    started = not (used == 0 and reset is None)
    reset_state = rules.reset_state(reset, now, at)
    refilled = None
    if raw["refilled"] is not None:
        refilled = {"at": now + raw["refilled"][0], "confirmed": now + raw["refilled"][1], "from": float(raw["refilled"][2])}
        if now - refilled["confirmed"] > REFILL_KEEP:
            refilled = None
    shown = used if started and reset_state != "passed" else None
    base = rules.band(None if shown is None else max(0.0, 100.0 - shown), raw["blocked"])
    pace = None if base in ("out", "unknown") else rules.pace(shown, reset, minutes, at, raw["samples"])
    severity = rules.severity(shown, raw["blocked"], pace)
    return {"id": raw["id"], "kind": kind, "label": label, "short": short, "model": model, "minutes": minutes,
            "used": shown, "left": None if shown is None else max(0.0, 100.0 - shown), "known": shown is not None,
            "started": started, "reset": reset, "reset_state": reset_state,
            "state": rules.window_state(started, reset_state, severity, refilled, stale, now),
            "severity": severity, "binding": False, "pace": pace, "blocked": raw["blocked"],
            "primary": kind in PRIMARY_KINDS, "refilled": refilled}


def _trend(raws, now, at):
    """A few weekly readings from the current reset epoch (PLAN §3.3 trend)."""
    weekly = next((r for r in raws if _SHAPES[r["id"]][0] == "weekly" and r["used"] is not None
                   and r["reset"] is not None), None)
    if weekly is None or at is None:
        return []
    start = now + weekly["reset"] - 604800
    points = [[at - days * 86400, round(weekly["used"] * share, 1)] for days, share in ((3, 0.55), (2, 0.7), (1, 0.85))]
    return [p for p in points if p[0] >= start] + [[at, float(weekly["used"])]]


def _account(spec, now, pinned, tz, order):
    provider, active = spec["provider"], spec.get("active", False)
    checked = _at(now, spec.get("checked", -300))
    reading = spec.get("reading", _SAME)
    at = checked if reading is _SAME else _at(now, reading)
    tier = rules.age_tier(None if at is None else now - at, active)
    raws = spec["windows"] if at is not None else []
    others = spec.get("other", []) if at is not None else []
    windows = sorted((_window(r, now, at, tier == "stale") for r in raws), key=rules.window_order)
    credential = {"state": "fresh", "expires_in": None, "last_outcome": "2xx" if at is not None else None,
                  "dormant_days": None, "session_expires_in_days": None, "revoked_hint": False,
                  **spec.get("credential", {})}
    error = spec.get("error")
    if error is not None:
        error = {"code": error["code"], "message": error["message"], "since": now + error["since"],
                 "outage": error.get("outage", False)}
    paused_until = _at(now, spec.get("paused_until"))
    raw_windows = [{"key": r["id"], "used": None if r["used"] is None else float(r["used"]), "reset": _at(now, r["reset"])}
                   for r in raws + others]
    if provider == "claude" and at is not None:
        raw_windows += [{"key": key, "used": 0.0, "reset": None} for key in _CODENAMES]
    account = {
        "id": spec["id"], "provider": provider, "label": spec["label"], "email": spec.get("email"),
        "plan": spec.get("plan"), "active": active, "switchable": spec.get("switchable", True),
        "saved": spec.get("saved", True), "imported_from": spec.get("imported_from"), "owner": spec.get("owner", "v2"),
        "state": None, "headline": None, "credential": credential, "checked": checked, "last_good_at": at,
        "stale": tier == "stale", "age_tier": tier, "refreshing": spec.get("refreshing", False), "error": error,
        "next_check": paused_until or (None if checked is None else checked + (300 if active else 900)),
        "paused_until": paused_until, "score": None, "recommended": False, "rank": None, "sort_order": order,
        "windows": windows,
        "other_windows": sorted((_window(r, now, at, tier == "stale") for r in others), key=rules.window_order),
        "raw_windows": raw_windows, "credits": spec.get("credits", []), "tokens": spec.get("tokens"),
        "trend": _trend(raws, now, at),
    }
    limit_resets = _limit_resets(spec, now) if at is not None or provider != "codex" else None
    if limit_resets is not None:
        if limit_resets["rows"]:
            account["reset_credits"] = limit_resets["rows"]
        account["resets"] = limit_resets["block"]
    account["temporary_limit"] = rules.temporary_limit(account)
    binding = rules.pick_binding(windows, pinned)
    if binding is not None:
        binding["binding"] = True
    account["state"] = rules.account_state(account)
    account["headline"] = rules.headline(account, binding, now, tz)
    return account


# ---- limit resets (resets.py): what the provider answered, and the rows and block AccountDeck makes of it ----

def _grant(gid, *, left, ends, kind=None, label="", usable=True, needs_limit=False, unavailable=None, clears=None,
           paused=False, fable=False):
    """One grant of Claude's `cedar_ember` block as the provider sends it; `ends` is an offset from now."""
    grant = {"id": gid, "label": label, "resets_total": left, "resets_left": left, "ends_at": ends, "paused": paused,
             "usable_now": usable, "use_requires_limit": needs_limit, "unavailable_reason": unavailable,
             "clears": clears or {"session": ["five_hour"], "weekly": ["seven_day"]}.get(kind, ["five_hour", "seven_day"])}
    if kind:
        grant["type"] = kind
    if fable:
        grant["gives_back_weekly"] = True
    return grant


def _cedar(grants=(), *, eligible=True, reason=None):
    return {"eligible": eligible, "ineligible_reason": reason, "at_limit": False, "exhausted": [], "grants": list(grants),
            "next_grant_id": None, "weekly_resets_at": None, "cooldown_until": None}


def _iso(when):
    return dt.datetime.fromtimestamp(when, _UTC).isoformat()


def _credit_id(account, index):
    return "RateLimitResetCredit_" + hashlib.sha256(f"{account}/{index}".encode()).hexdigest()[:32]


def claude_resets_raw(spec, now):
    """A Claude account's `cedar_ember` block with its times as the provider writes them (ISO), or None."""
    raw = spec.get("cedar")
    if raw is None:
        return None
    raw = copy.deepcopy(raw)
    for grant in raw["grants"]:
        grant["ends_at"] = None if grant["ends_at"] is None else _iso(now + grant["ends_at"])
    return raw


def codex_resets_raw(spec, now):
    """A Codex account's `rateLimitResetCredits` as `account/rateLimits/read` answers it (ids included), or None:
    `spec["credits_raw"]` is [(count, expires offset, title)] (`reset_credits` too, the older spelling)."""
    groups = spec.get("credits_raw", spec.get("reset_credits"))
    if groups is None:
        return None
    credits = []
    for count, expires, title in groups:
        for _ in range(count):
            credits.append({"id": _credit_id(spec["id"], len(credits)), "resetType": "codexRateLimits",
                            "status": "available", "grantedAt": int(now - 86400),
                            "expiresAt": None if expires is None else int(now + expires), "title": title,
                            "description": "Ready to redeem"})
    return {"availableCount": len(credits), "credits": credits}


def _limit_resets(spec, now):
    """{"rows", "block"} of an account as the engine would show them (resets.py, on the scenario's answers)."""
    if spec["provider"] == "codex":
        raw = codex_resets_raw(spec, now)
        if raw is None:
            return None
        windows = [{"id": w["id"], "kind": _SHAPES[w["id"]][0], "used": w["used"], "blocked": w["blocked"]}
                   for w in spec["windows"]]
        rows = resets.codex_rows(spec["id"], {"windows": windows, "reset_credits": codex_reset_rows(raw)}, spec.get("plan"))
        note = None
    else:
        parsed = claude_reset_block(claude_resets_raw(spec, now))
        if parsed is None:
            return None
        windows = [{"id": w["id"], "used": w["used"], "blocked": w["blocked"]} for w in spec["windows"]]
        rows = resets.claude_rows(spec["id"], parsed, {"windows": windows}, now)
        note = None if parsed["eligible"] else resets.ineligible_words(parsed["reason"])
    doubt = spec.get("doubt")
    pending = {"row": rows[0]["key"], "since": now + doubt} if doubt is not None and rows else None
    blockers = [{"code": "blocked", "message": spec["resets_blocked"]}] if spec.get("resets_blocked") else []
    block = resets.assemble(rows, checked=_at(now, spec.get("checked", -300)), note=note, pending=pending, running=False,
                            blockers=blockers)
    return {"rows": resets.public(rows), "block": block}


def _status(provider, now, level="ok", *, incident=None, since=None, source="statuspage", checked=-120):
    summary = {"ok": "All systems operational", "degraded": "Partially degraded service",
               "outage": "Major outage", "unknown": "Status not checked yet"}[level]
    url = {"claude": "https://status.claude.com", "claude_cli": "https://status.claude.com", "codex": "https://status.openai.com",
           "antigravity": None}[provider]
    return {"level": level, "summary": summary, "source": source, "checked": _at(now, checked),
            "since": _at(now, since), "url": url, "incident": incident}


def _claude_running(now, sessions=None, *, desktop=True, foreign=1):
    rows = [{"pid": 4464, "name": "Refactor billing", "status": "busy", "since": now - 2000, "cwd_short": "billing"},
            {"pid": 784, "name": "Docs pass", "status": "idle", "since": now - 100, "cwd_short": "docs"}]
    rows = rows if sessions is None else sessions
    return {"desktop": desktop, "desktop_pids": 14 if desktop else 0, "checked": now - 5, "sessions": rows,
            "busy": sum(r["status"] == "busy" for r in rows), "unknown": sum(r["status"] == "unknown" for r in rows),
            "foreign_claude": foreign, "registry_version": "2.1.284" if rows else None}


def _codex_running(now, consumers=True):
    rows = [{"pid": 9001, "kind": "tui", "started": now - 3600, "parent": "Terminal"}] if consumers else []
    return {"consumers": rows, "daemon": bool(consumers), "desktop": False, "checked": now - 5}


def antigravity_block(now, *, switching=False, paused=False, **parts):
    """`providers.antigravity` as the engine builds it (contract._check_agy), empty: agy not installed, nobody signed
    in, no saved account. `parts` replaces any of its keys (a scenario's spec["antigravity"])."""
    block = {"status": _status("antigravity", now, "unknown", source="none", checked=None), "active": None,
             "running": {"consumers": [], "checked": now - 5}, "switching": switching, "paused": paused,
             "installed": False, "store": {"state": "absent", "reason": None, "message": None}, "signed_in": None,
             "accounts": [], "refresh": {"busy": False, "last_try": None, "next_allowed": None}, "enrollment": None,
             "recovery": None, "notice_seen": False, "t3_code": False}
    block.update(copy.deepcopy(parts))
    block["active"] = next((a["id"] for a in block["accounts"] if a["active"]), None)
    return block


# Antigravity's plans (tier_display_name) and the user_tier agy's record carries with each (synthetic, for the demo).
AGY_TIERS = {"Google AI Pro": "g1-pro-tier", "Google AI Ultra": "g1-ultra-tier", None: "free-tier"}
AGY_REFUSED = "Google no longer accepts this account's Antigravity sign-in. Sign in again."


def agy_reading(at, buckets=None):
    """An Antigravity usage reading as providers.antigravity.parse_usage makes it from agy's answer taken at `at`
    (providers.fake.agy_usage_output: the real sample's shape; `buckets` as there)."""
    from .providers.antigravity import parse_usage       # on use: the adapter and the fakes import far more
    from .providers.fake import agy_usage_output
    return parse_usage(agy_usage_output(at, buckets), now=at)


def agy_id(label):
    """A saved Antigravity account's id in the example states (32 hex digits, as the table's)."""
    return hashlib.sha256(f"antigravity/{label}".encode()).hexdigest()[:32]


def _agy(label, email, *, plan="Google AI Pro", saved=True, active=False, switchable=None, usage=None, error=None):
    return {"id": agy_id(label) if saved else UNSAVED_IDS["antigravity"], "label": label, "email": email,
            "plan": plan, "saved": saved, "active": active, "switchable": saved if switchable is None else switchable,
            "usage": usage, "error": error}


def antigravity_parts(scenario, now):
    """A scenario's Antigravity (the parts antigravity_block takes), or {} where agy is not installed (the section
    is then absent, as in `terminal`)."""
    signed_in = {"installed": True, "store": {"state": "ok", "reason": None, "message": None}, "notice_seen": True}
    if scenario == "typical":
        return {**signed_in, "signed_in": "ada.gemini@example.test",
                "accounts": [_agy("Personal", "ada.gemini@example.test", active=True,
                                  usage=agy_reading(now - 240, {"gemini-weekly": 0.72, "gemini-5h": 0.45}))],
                "refresh": {"busy": False, "last_try": now - 240, "next_allowed": None}}
    if scenario == "mixed":
        return {**signed_in, "signed_in": "ada.gemini@example.test",
                "accounts": [_agy("Personal", "ada.gemini@example.test", active=True,
                                  usage=agy_reading(now - 600, {"gemini-weekly": 0.4, "gemini-5h": 0.12})),
                             _agy("Studio", "studio.gemini@example.test", plan="Google AI Ultra",
                                  usage=agy_reading(now - 2 * 86400, {"gemini-weekly": 0.55}))],
                "refresh": {"busy": False, "last_try": now - 600, "next_allowed": None}}
    if scenario == "antigravity":
        return {**signed_in, "signed_in": "new.face@example.test", "notice_seen": False, "t3_code": True,
                "running": {"consumers": [{"pid": 7301, "kind": "agy", "started": now - 1800, "parent": "Terminal"}],
                            "checked": now - 5},
                "accounts": [
                    # Its 5-hour limit was used up at the reading and has reset since: it counts as full again.
                    _agy("Personal", "ada.gemini@example.test",
                         usage=agy_reading(now - 3 * 3600, {"gemini-5h": (0.0, 7200), "gemini-weekly": 0.35})),
                    _agy("Old laptop", "old.gemini@example.test", usage=agy_reading(now - 5 * 86400),
                         error={"code": "reauth_required", "message": AGY_REFUSED, "since": now - 86400}),
                    _agy("new.face", "new.face@example.test", plan=None, saved=False, active=True,
                         usage=agy_reading(now - 60, {"gemini-5h": 0.8, "gemini-weekly": 0.93}))],
                "refresh": {"busy": False, "last_try": now - 30, "next_allowed": now + 30}}
    if scenario == "antigravity-recover":
        return {**signed_in, "signed_in": "studio.gemini@example.test",
                "accounts": [_agy("Personal", "ada.gemini@example.test", usage=agy_reading(now - 1800)),
                             _agy("Studio", "studio.gemini@example.test", plan="Google AI Ultra", active=True,
                                  usage=agy_reading(now - 7200, {"gemini-weekly": 0.6}))],
                "recovery": {"started": now - 120, "account": agy_id("Studio"), "before": "ada.gemini@example.test",
                             "after": "studio.gemini@example.test"}}
    return {}


def _banner(kind, title, message, actions=(), provider=None, link=None):
    return {"kind": kind, "title": title, "message": message, "actions": list(actions), "provider": provider, "link": link}


_CLAUDE_STEPS = (("prepare", "Checking {label}'s saved sign-in"), ("wait", "Waiting for Claude to finish"),
                 ("stop", "Closing Claude"), ("settle", "Waiting for Claude to let go of its files"),
                 ("capture", "Saving the current sign-in"), ("restore", "Restoring {label}"),
                 ("sessions", "Carrying over Code sessions"), ("launch", "Opening Claude"),
                 ("verify", "Checking the sign-in"), ("report", "Done"))


def _steps(label, now, progress):
    """Claude switch steps; ``progress`` maps step id -> (status, detail, started offset, ended offset)."""
    steps = []
    for sid, text in _CLAUDE_STEPS:
        status, detail, started, ended = progress.get(sid, ("pending", "", None, None))
        steps.append({"id": sid, "label": text.format(label=label), "status": status, "detail": detail,
                      "started": _at(now, started), "ended": _at(now, ended)})
    return steps


def _pending(now, phase, message, cancellable, progress, *, consent="idle", waiting_on=(), plan=None):
    pending = {"id": "op_7", "kind": "switch", "provider": "claude", "account": "acc_4", "label": "Delta",
               "phase": phase, "message": message, "cancellable": cancellable, "started": now - 40,
               "eta_seconds": None if phase == "waiting_idle" else 25, "consent": consent,
               "waiting_on": list(waiting_on), "steps": _steps("Delta", now, progress)}
    if plan is not None:
        pending["plan"] = plan
    return pending


def _report(now, verify_reason):
    return {"op": "op_7", "kind": "switch", "provider": "claude", "account": "acc_4", "label": "Delta",
            "finished": now - 120, "outcome": "success", "verified": "unconfirmed", "verified_at": None,
            "verify_reason": verify_reason, "stop_tier": "managed", "stop_ms": 2140, "helpers_self_exited": True,
            "stragglers": 0, "summary": "Claude opened as Delta. Still confirming the sign-in in the background.",
            "carried": [{"label": "Local Code sessions", "status": "carried", "detail": "12 added, 3 updated, 0 removed"},
                        {"label": "Local MCP servers (Claude chat)", "status": "carried",
                         "detail": "3 servers: filesystem, github, notion"},
                        {"label": "Claude Code MCP definitions", "status": "carried", "detail": "unchanged"}],
            "account_bound": [{"label": "Hosted connectors",
                               "detail": "Tied to each account; some may ask you to sign in once.", "count": 4},
                              {"label": "Claude in Chrome / Remote Control",
                               "detail": "May need re-pairing on this account."}],
            "manifest": "op_7", "error": None}


def _typical_accounts(active="acc_1"):
    usage_credits = [{"label": "Usage credits", "enabled": False, "used": None, "limit": None, "balance": None,
                      "unit": "minor currency units", "exponent": 2, "currency": "USD", "percent": None}]
    accounts = [
        _claude("acc_1", "Alpha", [_w("five_hour", 0), _w("seven_day", 100, 5400), _w("seven_day_fable", 63, 5400)],
                checked=-120, credential={"state": "live", "expires_in": 5400, "session_expires_in_days": 21},
                credits=usage_credits),
        _claude("acc_2", "Beta", [_w("five_hour", 32, 7200), _w("seven_day", 70, 259200)],
                other=[_w("seven_day_oauth_apps", 12, 259200)], checked=-600,
                credential={"state": "expired", "expires_in": -3600, "session_expires_in_days": 18}),
        _claude("acc_3", "Gamma", [_w("five_hour", 4, 16200, refilled=(-3200, -800, 88)), _w("seven_day", 42, 72000)],
                checked=-800, credential={"expires_in": 2400, "session_expires_in_days": 26}),
        _claude("acc_4", "Delta", [_w("five_hour", 8, 10800), _w("seven_day", 9, 432000)], checked=-300,
                credential={"expires_in": 3000, "dormant_days": 24, "session_expires_in_days": 4}),
        _claude("acc_5", "Epsilon", [_w("five_hour", 0), _w("seven_day", 22, 518400)], checked=-900,
                credential={"state": "expired", "expires_in": -86400, "session_expires_in_days": 12}),
        _codex("acc_6", "Codex main", [_w("five_hour", 30, 10800), _w("seven_day", 36, 172800)], checked=-60,
               credential={"state": "live", "expires_in": 518400},
               credits=[{"label": "Codex credits", "balance": "0", "unlimited": False, "hasCredits": False}]),
    ]
    for account in accounts:
        if account["provider"] == "claude":
            account["active"] = account["id"] == active
            if account["id"] == active:
                account["credential"] = {**account.get("credential", {}), "state": "live"}
        else:
            account["active"] = True
    return accounts


def _events(now):
    return [{"id": 88, "at": now - 10, "kind": "refresh", "message": "Refreshed 6 accounts."},
            {"id": 87, "at": now - 900, "kind": "switch", "message": "Switched Claude to Alpha."}]


def _scenario(name, now):
    requested = name
    if name in ("terminal", "update-available"):
        name = "typical"
    tz = _UTC
    spec = {"accounts": _typical_accounts(), "events": _events(now)}
    if name == "typical":
        spec["alerts"] = [{"id": "al_3", "kind": "reset_confirmed", "level": "good", "account": "acc_3", "window": "five_hour",
                           "title": "Gamma's 5-hour limit refilled", "message": "Gamma is ready again.", "at": now - 800,
                           "snoozed_until": None, "dismissed": False}]
        spec["notices"] = [{"id": "n_12", "level": "info", "message": "Alpha is ready.", "at": now - 900, "sticky": False}]
    elif name == "resets-soon":
        spec["accounts"] = [
            _claude("acc_1", "Alpha", [_w("five_hour", 0), _w("seven_day", 100, 750)], active=True, checked=-90,
                    credential={"state": "live", "expires_in": 4000}),
            _claude("acc_2", "Beta", [_w("five_hour", 97, 2400), _w("seven_day", 55, 345600)], checked=-400),
            _claude("acc_3", "Gamma", [_w("five_hour", 3, 7200), _w("seven_day", 45, 18000)], checked=-500),
            _claude("acc_4", "Delta", [_w("five_hour", 10, 10800), _w("seven_day", 60, 72000)], checked=-300),
            _claude("acc_5", "Epsilon", [_w("five_hour", 22, 14400), _w("seven_day", 30, 518400)], checked=-700),
            _codex("acc_6", "Codex main", [_w("five_hour", 20, 1200), _w("seven_day", 50, 172800)], active=True,
                   checked=-60, credential={"state": "live", "expires_in": 518400}),
        ]
    elif name == "refilled":
        spec["accounts"] = [
            _claude("acc_1", "Alpha", [_w("five_hour", 2, 17400, refilled=(-600, -240, 100)), _w("seven_day", 30, 345600)],
                    active=True, checked=-240, credential={"state": "live", "expires_in": 6000}),
            _claude("acc_2", "Beta", [_w("five_hour", 10, 7200), _w("seven_day", 20, 259200)], checked=-500),
            _claude("acc_3", "Gamma", [_w("five_hour", 0), _w("seven_day", 15, 432000)], checked=-700),
            _codex("acc_6", "Codex main", [_w("five_hour", 5, 16000), _w("seven_day", 12, 500000)], active=True,
                   checked=-60, credential={"state": "live", "expires_in": 518400}),
        ]
        spec["alerts"] = [{"id": "al_9", "kind": "reset_confirmed", "level": "good", "account": "acc_1", "window": "five_hour",
                           "title": "Alpha's 5-hour limit refilled", "message": "Alpha is ready again.", "at": now - 240,
                           "snoozed_until": None, "dismissed": False}]
    elif name == "not-started":
        spec["accounts"] = [
            _claude("acc_1", "Alpha", [_w("five_hour", 0), _w("seven_day", 35, 259200)], active=True, checked=-100,
                    credential={"state": "live", "expires_in": 7000}),
            _claude("acc_2", "Beta", [_w("five_hour", 0), _w("seven_day", 48, 172800)], checked=-400),
            _claude("acc_3", "Gamma", [_w("five_hour", 0), _w("seven_day", 10, 518400)], checked=-600),
            _codex("acc_6", "Codex main", [_w("five_hour", 0), _w("seven_day", 20, 432000)], active=True, checked=-60,
                   credential={"state": "live", "expires_in": 518400}),
        ]
    elif name == "codex-blocked":
        spec["accounts"] = [
            _claude("acc_1", "Alpha", [_w("five_hour", 12, 12000), _w("seven_day", 30, 300000)], active=True, checked=-100,
                    credential={"state": "live", "expires_in": 7000}),
            _claude("acc_2", "Beta", [_w("five_hour", 0), _w("seven_day", 20, 400000)], checked=-500),
            _codex("acc_6", "Codex main", [_w("five_hour", 100, 7200, blocked="not_allowed"), _w("seven_day", 64, 259200)],
                   active=True, checked=-60, credential={"state": "live", "expires_in": 518400},
                   credits=[{"label": "Codex credits", "balance": "0", "unlimited": False, "hasCredits": False},
                            {"label": "Limit reset credits", "details": {"availableCount": 1}}],
                   reset_credits=[(1, 1728000, "Limit reset")]),
            _codex("acc_7", "Codex spare", [_w("five_hour", 20, 3000, blocked="workspace_member_credits_depleted"),
                                            _w("seven_day", 35, 200000, blocked="workspace_member_credits_depleted")],
                   checked=-700),
        ]
    elif name == "resets":
        spec["accounts"] = _resets_accounts()
        spec["events"] = [{"id": 92, "at": now - 7200, "kind": "use-reset", "message": "Used one 5-hour reset on Work."},
                          *spec["events"]]
    elif name == "needs-signin":
        spec["accounts"] = [
            _claude("acc_1", "Alpha", [_w("five_hour", 25, 9000), _w("seven_day", 40, 300000)], active=True, checked=-90,
                    credential={"state": "live", "expires_in": 6000}),
            _claude("acc_2", "Beta", [_w("five_hour", 60, 3600), _w("seven_day", 75, 200000)], checked=-300, reading=-7200,
                    credential={"state": "reauth_required", "last_outcome": "4xx"},
                    error={"code": "reauth_required", "message": "Sign-in needs renewing.", "since": -2400}),
            _claude("acc_3", "Gamma", [_w("five_hour", 15, 2000), _w("seven_day", 50, 250000)], checked=-10800,
                    credential={"state": "expired", "expires_in": -14400}),
            _claude("acc_4", "Delta", [_w("five_hour", 5, 12000), _w("seven_day", 12, 400000)], checked=-400,
                    credential={"state": "web_only"}),
            _codex("acc_6", "Codex main", [_w("five_hour", 10, 15000), _w("seven_day", 20, 300000)], active=True,
                   checked=-60, reading=-1500, credential={"state": "access_expired", "expires_in": -300, "last_outcome": "4xx"}),
        ]
    elif name == "blocked-cloudflare":
        blocked = {"code": "blocked", "message": "Check refused by Cloudflare · sign-in not known to be bad"}
        spec["accounts"] = [
            _claude("acc_1", "Alpha", [_w("five_hour", 40, 6000), _w("seven_day", 55, 250000)], active=True, checked=-100,
                    credential={"state": "live", "expires_in": 6000}),
            _claude("acc_2", "Beta", [_w("five_hour", 20, 9000), _w("seven_day", 35, 300000)], checked=-60, reading=-1500,
                    credential={"state": "expired", "last_outcome": "4xx"}, error={**blocked, "since": -1500}),
            _claude("acc_3", "Gamma", [_w("five_hour", 0), _w("seven_day", 61, 90000)], checked=-60, reading=-5400,
                    credential={"state": "expired", "last_outcome": "4xx"}, error={**blocked, "since": -5400}),
            _claude("acc_4", "Delta", [_w("five_hour", 5, 15000), _w("seven_day", 10, 500000)], checked=-200),
            _codex("acc_6", "Codex main", [_w("five_hour", 30, 10800), _w("seven_day", 36, 172800)], active=True,
                   checked=-60, credential={"state": "live", "expires_in": 518400}),
        ]
    elif name == "stale":
        spec["accounts"] = [
            _claude("acc_1", "Alpha", [_w("five_hour", 45, 5000), _w("seven_day", 62, 200000)], active=True, checked=-2520,
                    credential={"state": "live", "expires_in": 3000}),
            _claude("acc_2", "Beta", [_w("five_hour", 30, 3000), _w("seven_day", 80, 150000)], checked=-10800,
                    credential={"state": "expired", "expires_in": -7200}),
            _claude("acc_3", "Gamma", [_w("five_hour", 10, 10000), _w("seven_day", 20, 400000)], checked=-300),
            _claude("acc_4", "Delta", [_w("five_hour", 0), _w("seven_day", 33, 300000)], checked=-900),
            _codex("acc_6", "Codex main", [_w("five_hour", 30, 10800), _w("seven_day", 36, 172800)], active=True,
                   checked=-60, credential={"state": "live", "expires_in": 518400}),
        ]
    elif name == "fable-binding":
        spec["settings"] = {"pinned_model": "Fable"}
        spec["accounts"] = [
            _claude("acc_1", "Alpha", [_w("five_hour", 20, 9000), _w("seven_day", 40, 170000),
                                       _w("seven_day_fable", 92, 170000)], active=True, checked=-100,
                    credential={"state": "live", "expires_in": 6000}),
            _claude("acc_2", "Beta", [_w("five_hour", 10, 12000), _w("seven_day", 30, 300000),
                                      _w("seven_day_fable", 35, 300000)], checked=-400),
            _claude("acc_3", "Gamma", [_w("five_hour", 0), _w("seven_day", 50, 100000),
                                       _w("seven_day_fable", 70, 100000)], checked=-600),
            _codex("acc_6", "Codex main", [_w("five_hour", 30, 10800), _w("seven_day", 36, 172800)], active=True,
                   checked=-60, credential={"state": "live", "expires_in": 518400}),
        ]
    elif name == "resetting":
        spec["accounts"] = [
            _claude("acc_1", "Alpha", [_w("five_hour", 100, -120), _w("seven_day", 55, 259000)], active=True, checked=-200,
                    credential={"state": "live", "expires_in": 5000}),
            _claude("acc_2", "Beta", [_w("five_hour", 80, -1800), _w("seven_day", 30, 400000)], checked=-7200,
                    credential={"state": "expired", "expires_in": -3600}),
            _claude("acc_3", "Gamma", [_w("five_hour", 15, 8000), _w("seven_day", 25, 350000)], checked=-400),
            _codex("acc_6", "Codex main", [_w("five_hour", 30, 10800), _w("seven_day", 36, 172800)], active=True,
                   checked=-60, credential={"state": "live", "expires_in": 518400}),
        ]
    elif name == "rate-limited":
        paused = 1920
        spec["accounts"][1] = _claude("acc_2", "Beta", [_w("five_hour", 35, 7000), _w("seven_day", 45, 300000)],
                                      checked=-60, reading=-900, paused_until=paused,
                                      credential={"state": "expired", "expires_in": -3600, "last_outcome": "429"},
                                      error={"code": "rate_limited", "since": -60,
                                             "message": f"Usage checks paused until {clock_text(now + paused, now, tz)}"})
        spec["accounts"][1]["active"] = False
    elif name == "outage":
        paused = {"code": "outage", "message": "Reading paused · provider incident", "since": -1500, "outage": True}
        spec["status"] = {"claude": _status("claude", now, "outage", since=-1800, checked=-60, incident={
            "title": "Elevated errors on claude.ai", "impact": "major", "status": "investigating", "started": now - 1800})}
        spec["accounts"] = [
            _claude("acc_1", "Alpha", [_w("five_hour", 0), _w("seven_day", 70, 200000)], checked=-60, reading=-1500,
                    error=paused),
            _claude("acc_2", "Beta", [_w("five_hour", 30, 8000), _w("seven_day", 40, 300000)], checked=-60, reading=-1500,
                    error=paused),
            _claude("acc_4", "Delta", [_w("five_hour", 8, 10000), _w("seven_day", 12, 400000)], active=True, checked=-60,
                    reading=-1500, credential={"state": "live", "expires_in": 2000}, error=paused),
            _codex("acc_6", "Codex main", [_w("five_hour", 30, 10800), _w("seven_day", 36, 172800)], active=True,
                   checked=-60, credential={"state": "live", "expires_in": 518400}),
        ]
        spec["report"] = _report(now, "Claude is reporting an incident, so the sign-in could not be confirmed yet. Still checking.")
        spec["banners"] = [_banner("outage", "Claude is having an incident",
                                   "Elevated errors on claude.ai. Your last good readings are kept. Switching still works; "
                                   "verification may take longer.", provider="claude", link="https://status.claude.com")]
        spec["last_manifest"] = "op_7"
    elif name == "switching":
        spec["pending"] = _pending(now, "waiting_idle", "Waiting for Claude to finish…", True,
                                   {"prepare": ("done", "", -40, -38), "wait": ("active", "1 session is working", -38, None)},
                                   waiting_on=[{"pid": 4464, "name": "Refactor billing", "status": "busy"}])
    elif name == "stopping":
        spec["pending"] = _pending(now, "stopping", "Closing Claude…", False,
                                   {"prepare": ("done", "", -40, -38), "wait": ("done", "All sessions idle for 20 s", -38, -8),
                                    "stop": ("active", "Managed stop · main process", -8, None)})
        spec["accounts"] = _typical_accounts()
        spec["claude_running"] = _claude_running(now, [{"pid": 4464, "name": "Refactor billing", "status": "idle",
                                                        "since": now - 30, "cwd_short": "billing"},
                                                       {"pid": 784, "name": "Docs pass", "status": "idle",
                                                        "since": now - 100, "cwd_short": "docs"}])
    elif name == "report":
        spec["accounts"] = _typical_accounts(active="acc_4")
        spec["report"] = _report(now, "Delta's saved token has expired; Claude will renew it. Still checking.")
        spec["last_manifest"] = "op_7"
    elif name == "needs-reconfirm":
        plan = example_plan("busy", now)
        plan["running"]["sessions"].append({"pid": 5120, "name": "New session", "status": "busy", "since": now - 15})
        plan["running"]["busy"] += 1
        plan["process_set"].append([5120, 133812345690000000])
        plan["signature"] = _signature(plan["process_set"])
        plan["warnings"][0] = {"code": "busy", "message": '2 Code sessions are working: "Refactor billing", "New session".'}
        spec["pending"] = _pending(now, "needs_reconfirm", "Claude's sessions changed — please confirm again", True,
                                   {"prepare": ("done", "", -40, -38),
                                    "wait": ("warning", "A new Code session started", -38, -15)}, plan=plan)
    elif name == "empty":
        spec.update(accounts=[], events=[], claude_running=_claude_running(now, [], desktop=False, foreign=0),
                    codex_running=_codex_running(now, consumers=False))
    elif name == "loading":
        spec.update(ready=False, events=[], status={p: _status(p, now, "unknown", source="none", checked=None) for p in PROVIDERS})
        spec["accounts"] = [
            _claude("acc_1", "Alpha", active=True, checked=None, refreshing=True,
                    credential={"state": "unknown", "last_outcome": None}),
            _claude("acc_2", "Beta", checked=None, credential={"state": "unknown", "last_outcome": None}),
            _codex("acc_6", "Codex main", active=True, checked=None, refreshing=True,
                   credential={"state": "unknown", "last_outcome": None}),
        ]
    elif name == "fifty":
        spec["settings"] = {"density": "rows"}
        spec["accounts"] = _fifty_accounts()
        spec["events"] = [{"id": 412, "at": now - 30, "kind": "refresh", "message": "Refreshed 50 accounts."}]
    elif name == "read-only":
        spec["read_only"] = True
        spec["banners"] = [_banner("read_only", "Another AccountDeck 2 is managing your sign-ins",
                                   "Quit that copy to make changes here. This one only shows usage.")]
    elif name == "demo-banner":
        spec.update(mode="demo", safe_mode=True)
        spec["banners"] = [_banner("demo", "Demo mode", "Demo mode — synthetic accounts; no real apps are touched."),
                           _banner("safe_mode", "Safe mode", "Safe mode is on: AccountDeck 2 will not touch real apps or files.")]
    elif name == "first-run":
        spec["accounts"] = [
            _claude("unsaved-claude", "gamma", [_w("five_hour", 18, 12600), _w("seven_day", 46, 280000)], active=True,
                    switchable=False, saved=False, checked=-20, email="gamma@example.test",
                    credential={"state": "live", "expires_in": 6800})]
        spec["notices"] = [{"id": "n_1", "level": "good", "message": "Saved your current terminal login as alpha@example.test.",
                            "at": now - 30, "sticky": True}]
        spec["events"] = []
    elif name == "runway":
        spec["accounts"] += _runway_accounts()
        spec["alerts"] = [{"id": "al_7", "kind": "limit_reached", "level": "warning", "account": "cli_beta", "window": "credits",
                           "title": "Beta is spending usage credits",
                           "message": "Beta spent $3.20 of usage credits this month ($3.20 of $100).", "at": now - 300,
                           "snoozed_until": None, "dismissed": False}]
    elif name == "ultra":
        spec["settings"] = {"pinned_model": "Fable"}
        spec["accounts"] = _ultra_accounts()
        spec["events"] = [{"id": 89, "at": now - 5400, "kind": "usage", "message": "Beta no longer has a 5-hour limit"},
                          *spec["events"]]
        spec["usage_shape"] = {"changed_at": now - 5400, "added": [], "removed": ["five_hour", "limits:session"]}
    elif name == "mixed":
        spec["settings"] = {"pinned_model": "Fable"}
        spec["accounts"] = _mixed_accounts()
    elif name in ("antigravity", "antigravity-recover"):
        pass                    # the typical Claude and Codex accounts beside them
    else:
        raise KeyError(name)
    if requested in ("typical", "terminal", "resets-soon", "first-run", "update-available"):
        spec["accounts"] += terminal_accounts(requested)
    agy = antigravity_parts(requested, now)
    if agy:
        spec["antigravity"] = agy
    return spec


def _resets_accounts():
    """`resets`: every case of limit resets the dashboard draws, on accounts of all three apps (resets.py)."""
    day = 86400
    work = _claude("acc_1", "Work", [_w("five_hour", 40, 9000), _w("seven_day", 70, 300000)], active=True, checked=-120,
                   credential={"state": "live", "expires_in": 7000},
                   cedar=_cedar([_grant("grant_weekly", kind="weekly", left=2, ends=14 * day),
                                 _grant("grant_session", kind="session", left=1, ends=5 * day, usable=False,
                                        needs_limit=True, unavailable="needs_limit")]))
    lab = _claude("acc_2", "Lab", [_w("five_hour", 20, 12000), _w("seven_day", 50, 200000)], checked=-400,
                  credential={"state": "fresh", "expires_in": 6000},
                  cedar=_cedar([_grant("grant_full", kind="full", left=1, ends=10 * day, label="Opus 5.5 launch reset")]),
                  reset_unknown=True)          # the demo's use of it ends "not known" (the answer is lost)
    alpha = _terminal("alpha", "Alpha", "Max", [_w("five_hour", 55, 6000), _w("seven_day", 80, 250000)], active=True,
                      credential={"state": "live", "expires_in": 20000},
                      cedar=_cedar([_grant("grant_fable", left=1, ends=7 * day, fable=True,
                                           clears=["seven_day_fable", "five_hour"])]))
    beta = _terminal("beta", "Beta", "Max", [_w("five_hour", 10, 15000), _w("seven_day", 30, 400000)],
                     credential={"state": "fresh", "expires_in": 20000}, cedar=_cedar(eligible=False, reason="surface"))
    main = _codex("acc_6", "Codex main", [_w("five_hour", 85, 1200), _w("seven_day", 40, 172800)], active=True,
                  checked=-60, plan="plus", credential={"state": "live", "expires_in": 518400},
                  credits_raw=[(2, 20 * day, "Full reset (Weekly + 5 hr)")])
    team = _codex("acc_7", "Codex team", [_w("five_hour", 30, 9000), _w("seven_day", 65, 300000)], checked=-200,
                  plan="pro", credential={"state": "fresh", "expires_in": 400000},
                  credits_raw=[(1, 12 * day, "Weekly reset")], doubt=-120)
    personal = _codex("acc_8", "Codex personal", [_w("five_hour", 10, 16000), _w("seven_day", 15, 500000)], checked=-300,
                      plan="plus", credential={"state": "fresh", "expires_in": 400000},
                      credits_raw=[(1, 25 * day, "Full reset (Weekly + 5 hr)")])
    spare = _codex("acc_9", "Codex spare", [_w("five_hour", 50, 10000), _w("seven_day", 30, 450000)], checked=-500,
                   plan="plus", credential={"state": "fresh", "expires_in": 400000}, credits_raw=[])
    for account in (main, team, personal, spare):
        count = sum(n for n, _e, _t in account["credits_raw"])
        account["credits"] = [{"label": "Limit reset credits", "details": {"availableCount": count}}]
    return [work, lab, alpha, beta, main, team, personal, spare]


def reset_specs(scenario, now):
    """What the providers of `scenario`'s accounts answer about limit resets, by account id (the demo's ledger and
    memory): {"claude": cedar_ember block, "codex": rateLimitResetCredits, "doubt": when an open use was sent,
    "unknown": the next use's answer is lost}."""
    found = {}
    for spec in _scenario(scenario, now)["accounts"]:
        claude, codex = claude_resets_raw(spec, now), codex_resets_raw(spec, now) if spec["provider"] == "codex" else None
        if claude is not None or codex is not None:
            found[spec["id"]] = {"claude": claude, "codex": codex, "unknown": bool(spec.get("reset_unknown")),
                                 "doubt": None if spec.get("doubt") is None else now + spec["doubt"]}
    return found


def _terminal(key, label, plan, windows, **spec):
    """A terminal account whose plan is named as Claude Code's own data names it (demo._cli_profile)."""
    row = _claude("cli_" + key, label, windows, plan=plan, **{"credential": {"state": "fresh"}, **spec})
    row["provider"] = "claude_cli"
    return row


def _week(used, days, blocked=None):
    return _w("seven_day", used, days * 86400, blocked=blocked)


def _fable(used, days):
    return _w("seven_day_fable", used, days * 86400)


def _ultra_accounts():
    """Verified Ultra accounts, which have no 5-hour window: Alpha, in use, near the threshold, Gamma with most of its
    Fable 5.1 limit used, Delta locked, and the same people's Claude Desktop row."""
    return [_terminal("alpha", "Alpha", "Ultra", [_week(93, 2), _fable(40, 2)], active=True),
            _terminal("beta", "Beta", "Ultra", [_week(30, 4), _fable(12, 4)]),
            _terminal("gamma", "Gamma", "Ultra", [_week(45, 3), _fable(88, 3)]),
            _terminal("delta", "Delta", "Ultra", [_week(20, 5, "locked"), _fable(5, 5)]),
            _claude("acc_1", "Alpha", [_week(93, 2), _fable(40, 2)], plan="Ultra", active=True,
                    credential={"state": "live", "expires_in": 5400})]


def _mixed_accounts():
    """Every kind of account side by side. Alpha, in use, is on Max near its 5-hour limit; Beta is a verified Ultra
    account; Gamma an Ultra account on the temporary limit; Delta an Ultra account with its weekly limit used up."""
    return [_terminal("alpha", "Alpha", "Max", [_w("five_hour", 92, 2400), _week(40, 3), _fable(15, 3)], active=True),
            _terminal("beta", "Beta", "Ultra", [_week(30, 4), _fable(12, 4)]),
            _terminal("gamma", "Gamma", "Ultra", [_w("five_hour", 25, 3 * 3600), _week(20, 5), _fable(8, 5)]),
            _terminal("delta", "Delta", "Ultra", [_week(100, 1), _fable(60, 1)]),
            _terminal("epsilon", "Epsilon", "Ultra", [_week(15, 6)], health="needs_signin",
                      credential={"state": "reauth_required"})]


def _runway_accounts():
    """Terminal accounts close to the end of their weeks. Alpha's week began 6 hours ago: the demo's history of
    it (its earlier readings grow linearly over the week so far) is the fast burn the forecast sees."""
    credits_on = [{"label": "Usage credits", "enabled": True, "used": 320, "limit": 10000, "balance": None,
                   "unit": "minor currency units", "exponent": 2, "currency": "USD", "percent": 3.2}]
    rows = [_claude("cli_alpha", "Alpha", [_w("seven_day", 60, 7 * 86400 - 6 * 3600)], active=True, credential={"state": "fresh"}),
            _claude("cli_beta", "Beta", [_w("seven_day", 92, 3 * 86400)], credits=credits_on, credential={"state": "fresh"}),
            _claude("cli_delta", "Delta", [_w("seven_day", 88, 2 * 86400)], credential={"state": "fresh"})]
    for row in rows:
        row["provider"] = "claude_cli"
    return rows


def terminal_watch(now, scenario):
    """`providers.claude_cli.watch` of a scenario: only `terminal` shows a limit error that drove a switch and a
    session it stopped, with a name and one without a registry row."""
    if scenario != "terminal":
        return {"running": True, "last": None, "stopped": []}
    at = now - 3 * 3600
    return {"running": True, "last": {"at": at, "kind": "weekly", "account": "cli_alpha", "counted": True, "reaction_s": 6.2},
            "stopped": [{"session": "4f0c7d3a-9d1b-4a52-8a43-0f6e2b7c1d11", "name": "Fix the flaky importer test", "project": "billing",
                         "kind": "weekly", "at": at},
                        {"session": "b83a1e60-27c4-4d0e-9f15-6c2d8e4a7b22", "name": None, "project": None, "kind": "weekly", "at": at - 40}]}


def terminal_runway(accounts, settings, now, scenario):
    """`providers.claude_cli.runway` of a scenario: only the `runway` scenario has history behind it, 10 weekly
    points an hour on the active account over the last 6 hours."""
    rows = [a for a in accounts if a["provider"] == "claude_cli"]
    readings = {}
    if scenario == "runway":
        readings = {a["id"]: [{"at": now - hours * 3600, "used": a["windows"][0]["used"] - hours * 10.0, "reset": a["windows"][0]["reset"]}
                              for hours in range(6, -1, -1)] for a in rows if a["active"]}
    return forecast(rows, readings, settings, now)


def _fifty_accounts():
    accounts = []
    for n in range(1, 36):
        stale = n % 9 == 0
        age = 3900 + 60 * n if stale else 60 + (n * 97) % 1100
        five = (0, 4, 12, 27, 45, 61, 78, 88, 96, 100)[n % 10]
        week = (n * 13 + 7) % 101
        week_reset = 3600 + (n * 104729) % 590000
        windows = [_w("five_hour", five, None if five == 0 else min(18000 - age - 60, 600 + (n * 1543) % 16000)),
                   _w("seven_day", week, week_reset)]
        if n % 5 == 0:
            windows.append(_w("seven_day_fable", min(100, week + 20), week_reset))
        state = "reauth_required" if n == 17 else "expired" if n % 11 == 5 else "live" if n == 1 else "fresh"
        error = {"code": "blocked", "message": "Check refused by Cloudflare · sign-in not known to be bad",
                 "since": -age} if n == 23 else None
        accounts.append(_claude(f"acc_{n}", f"Claude {n:02d}", windows, active=n == 1, checked=-age, error=error,
                                email=f"claude{n:02d}@example.test", credential={"state": state}))
    for n in range(36, 51):
        age = 60 + (n * 211) % 1500
        five = (0, 9, 22, 35, 58, 73, 90, 100)[n % 8]
        week = (n * 17 + 3) % 101
        five_raw = _w("five_hour", five, None if five == 0 else min(18000 - age - 60, 900 + (n * 977) % 15000),
                      blocked="rate_limit_reached" if five == 100 else None)
        week_raw = _w("seven_day", None if n == 40 else week, 7200 + (n * 7919) % 580000)
        accounts.append(_codex(f"acc_{n}", f"Codex {n - 35:02d}", [five_raw, week_raw], active=n == 36, checked=-age,
                               email=f"codex{n - 35:02d}@example.test",
                               credential={"state": "live" if n == 36 else "fresh"}))
    return accounts


def example_state(scenario, now=BASE_NOW):
    """The /api/state payload for one of ``SCENARIOS`` at ``now`` (clock copy in UTC)."""
    if scenario not in SCENARIOS:
        raise KeyError(f"Unknown scenario {scenario!r}; choose one of {', '.join(SCENARIOS)}")
    tz = _UTC
    spec = _scenario(scenario, now)
    settings = copy.deepcopy({**DEFAULT_SETTINGS, **spec.get("settings", {})})    # callers may edit the payload
    pinned = settings["pinned_model"]
    accounts = [_account(a, now, pinned, tz, index) for index, a in enumerate(spec["accounts"])]
    for account, source in zip(accounts, spec["accounts"], strict=True):
        if account["provider"] == "claude_cli":
            account.update(excluded=source.get("excluded", False), health=source.get("health", "ok"), live_expired=False,
                           login_ends_at=_at(now, source.get("login_ends", 20 * 86400)))
    status = {p: spec.get("status", {}).get(p) or _status(p, now) for p in ACCOUNT_PROVIDERS}
    best = {}
    for provider in ACCOUNT_PROVIDERS:          # never Antigravity: nothing recommends its accounts
        best[provider], ranked = rules.recommend(provider, accounts, pinned, now, tz)
        for rank, account in enumerate(ranked, 1):
            account["rank"], account["score"] = rank, round(rules.headroom(account, pinned) / 100, 2)
        if best[provider]["state"] in ("ok", "unknown") and best[provider]["account"] is not None:
            next(a for a in accounts if a["id"] == best[provider]["account"])["recommended"] = True
    strip = rules.strip(accounts, now)
    pending = spec.get("pending")
    switching = {p: pending is not None and pending["provider"] == p for p in PROVIDERS}
    version = "2.0.0"
    active = {p: next((a["id"] for a in accounts if a["provider"] == p and a["active"]), None) for p in ACCOUNT_PROVIDERS}
    agy = dict(spec.get("antigravity", {}))       # a scenario's Antigravity parts (antigravity_block)
    if spec.get("status", {}).get("antigravity") is not None:
        agy.setdefault("status", spec["status"]["antigravity"])
    return {
        "app": "accountdeck2", "version": version, "rev": 4812, "now": now,
        "mode": spec.get("mode", "live"), "safe_mode": spec.get("safe_mode", False), "ready": spec.get("ready", True),
        "read_only": spec.get("read_only", False), "settings": settings,
        "providers": {
            "claude": {"status": status["claude"], "active": active["claude"],
                       "running": spec.get("claude_running") or _claude_running(now), "switching": switching["claude"],
                       "paused": settings["refresh_paused"] or switching["claude"], "data_folder": "Application Support",
                       "folder_warning": None},
            "codex": {"status": status["codex"], "active": active["codex"],
                      "running": spec.get("codex_running") or _codex_running(now), "switching": switching["codex"],
                      "paused": settings["refresh_paused"] or switching["codex"]},
            "claude_cli": {"status": status["claude_cli"], "active": active["claude_cli"], "running": {"checked": now},
                           "switching": switching["claude_cli"], "paused": settings["refresh_paused"],
                           "autoswitch": terminal_autoswitch(accounts, settings, now,
                               {"last": {"from": "cli_alpha", "to": "cli_beta", "at": now - 120,
                                "kind": "threshold", "reason": "Alpha reached 95% of its 5-hour limit; switched terminals to Beta.",
                                "undo_until": now + 480}} if scenario in ("terminal", "resets-soon") else None),
                           "enrollment": {"phase": "idle", "message": ""}, "readonly_reason": None,
                           "runway": terminal_runway(accounts, settings, now, scenario),
                           "watch": terminal_watch(now, scenario), "usage_shape": spec.get("usage_shape"),
                           "signin_due": signin_due(accounts, now)},
            "antigravity": antigravity_block(now, switching=switching["antigravity"],
                                             paused=settings["refresh_paused"] or switching["antigravity"], **agy),
        },
        "accounts": accounts,
        "now_band": {"next_reset": rules.next_reset(accounts, strip, now), "best": best, "attention": rules.attention(accounts, now, tz)},
        "resetting_soon": strip,
        "pending": pending, "report": spec.get("report"), "login": None,
        "recovery": {"pending": False, "count": 0},
        "notices": spec.get("notices", []), "alerts": spec.get("alerts", []), "banners": spec.get("banners", []),
        "setup": {"message": "Your settings stay shared.", "note": "Hosted connections are account-specific.", "groups": []},
        "sessions": [], "cwd": settings["cwd"], "events": spec.get("events", []),
        "last_manifest": spec.get("last_manifest"), "platform": platform_block(),
        "health": doctor_report(scenario, now), "build": {"commit": "c58fa73", "dirty": False, "installed_at": now - 86400},
        "service": {"watchdog": {"state": "watching", "tick_age_s": 2.1, "restarts_last_hour": 0, "reads_stuck": 0, "last": None},
                    "db": {"quick_check": {"result": "ok", "at": now - 3600, "ms": 26},
                           "last_backup": {"at": now - 3600, "bytes": 7_100_000}, "backups": 7, "restored_from": None}},
        "update": update_block(scenario, version, now, mode=spec.get("mode", "live")),
    }


def next_release(version):
    """The release after `version` in the examples: its next minor version (2.2.0 -> 2.3.0)."""
    major, minor = updates.parse_version(version)[:2]
    return f"{major}.{minor + 1}.0"


def update_block(scenario, version, now, *, mode="live"):
    """`update` of a scenario, as updates.view makes it: the demo does not check, `loading` has not checked yet,
    `update-available` offers the next minor release, and every other one is up to date, checked a few minutes ago
    (a check runs every 5 minutes)."""
    if mode == "demo":
        return updates.view(updates.load_memory(None), current=version, enabled=True, reason=updates.DEMO)
    memory = updates.load_memory({"checked_at": now - 180, "tried_at": now - 180, "latest": version})
    if scenario == "loading":
        memory = updates.load_memory(None)
    elif scenario == "update-available":
        memory.update(latest=next_release(version))
    return updates.view(memory, current=version, enabled=True, reason=None)


def doctor_report(scenario, now):
    """`state.health` as doctor.run makes it: none yet while loading, a fail where the scenario has a dead active login."""
    if scenario == "loading":
        return None
    checks = [("service.running", "ok", "AccountDeck 2.1.0 (c58fa73, installed 2026-11-02 18:40) answers on port 53157.", None),
              ("service.ticks", "ok", "Its ticks are moving: the last one finished 3 s ago.", None),
              ("db", "ok", "The database (70 MB, schema 2) passes its integrity check.", None),
              ("cc.binary", "ok", "Claude Code 2.1.312 is installed.", None),
              ("cc.config", "ok", "~/.claude.json has oauthAccount with the keys AccountDeck reads.", None),
              ("account.cli_alpha", "ok", "Alpha (in use): reads ok, last reading 3m ago, login ends Nov 28.", None),
              ("accounts.autoswitch", "ok", "Terminals use Alpha; auto-switch is on; 2 accounts could take over now: Beta, Gamma.", None),
              ("machine.disk", "ok", "412.0 GB of disk space is free.", None)]
    if scenario == "needs-signin":
        checks[5] = ("account.cli_alpha", "fail", "Alpha (in use): reads failing, last reading 3h ago, login ended Nov 2, needs sign-in.",
                     "Choose Sign In Again for Alpha in AccountDeck.")
        checks[6] = ("accounts.autoswitch", "warn", "Terminals use Alpha; auto-switch is on, but no account could take over now "
                     "(Beta: needs sign-in).", "Sign in again or add a terminal account.")
    return {"ok": not any(check[1] == "fail" for check in checks), "at": now - 120,
            "checks": [dict(zip(("id", "level", "text", "action"), check, strict=True)) for check in checks]}


# ---- SwitchPlan fixtures (PLAN §3.4) ----

def terminal_autoswitch(accounts, settings, now, history=None):
    rows = [a for a in accounts if a["provider"] == "claude_cli"]
    result = evaluate(rows, settings, history or {}, now)
    return {"enabled": settings["cli_autoswitch"], "threshold": settings["cli_threshold"],
            "order_mode": settings["cli_order"], "early_switch": settings["cli_early_switch"],
            "next_up": result["next_up"], "last": (history or {}).get("last"), "hold": result["hold"],
            "previews": {mode: evaluate(rows, {**settings, "cli_order": mode}, history or {}, now)["next_up"]
                         for mode in ("soonest_reset", "most_room", "manual")}}




def _signature(pairs):
    return "sha256:" + hashlib.sha256(json.dumps(sorted(pairs)).encode()).hexdigest()


def example_plan(name, now=BASE_NOW):
    """The SwitchPlan fixture ``plan-<name>`` (one of ``PLANS``) at ``now``."""
    if name not in PLANS:
        raise KeyError(f"Unknown plan {name!r}; choose one of {', '.join(PLANS)}")
    state = example_state("typical", now)
    accounts = {a["id"]: a for a in state["accounts"]}
    alpha, delta = accounts["acc_1"], accounts["acc_4"]
    sessions = [{k: s[k] for k in ("pid", "name", "status", "since")} for s in state["providers"]["claude"]["running"]["sessions"]]
    dormant = {"code": "dormant", "message": "Delta has not been opened for 24 days; its sign-in may have expired."}
    will = ["Close Claude (2 Code sessions will end)", "Save the current sign-in", "Restore Delta's sign-in",
            "Carry over your local Code session list", "Reopen Claude and check the sign-in"]
    plan = {
        "plan_id": "pl_" + hashlib.sha256(name.encode()).hexdigest()[:32], "expires": now + 120,
        "provider": "claude", "purpose": "switch", "target": rules.plan_target(delta), "current": rules.plan_current(alpha),
        "headroom": {"now": rules.headroom_line(alpha, now), "after": rules.headroom_line(delta, now)},
        "running": {"desktop": True, "sessions": sessions, "busy": 1, "unknown": 0, "foreign_claude": 1, "codex_consumers": []},
        "process_set": [[37408, 133812345678901234], [4464, 133812345680000000], [784, 133812345681000000]],
        "signature": "", "blockers": [],
        "warnings": [{"code": "busy", "message": '1 Code session is working: "Refactor billing".'},
                     {"code": "foreign", "message": "1 Claude Code process outside Claude Desktop stays untouched."}, dormant],
        "tip": None, "will": will, "preserved": list(rules.PRESERVED),
        "account_bound": ["Hosted connectors (may ask you to sign in once on Delta)",
                          "Claude in Chrome pairing and Remote Control"],
        "invisible": "Cowork and Chat activity cannot be detected.", "stop_tier": "managed",
        "confirmation_required": True, "idle": False, "remember_available": False, "default_button": "idle",
        "saves_outgoing": False,
    }
    if name == "idle":
        plan["running"].update(sessions=[s for s in sessions if s["status"] == "idle"], busy=0, foreign_claude=0)
        plan["process_set"] = [[37408, 133812345678901234], [784, 133812345681000000]]
        plan["warnings"] = [dormant]
        plan["will"][0] = "Close Claude (1 Code session will end)"
        plan.update(idle=True, remember_available=True, default_button="now")
    elif name == "blocked":
        plan["blockers"] = [{"code": "recovery", "message": "Finish the pending recovery first."}]
    elif name in ("quit", "add"):
        plan.update(purpose=name, target=None, headroom=None, account_bound=[])
        plan["will"] = (["Close Claude (2 Code sessions will end)"] if name == "quit" else
                        ["Close Claude (2 Code sessions will end)", "Save the current sign-in",
                         "Open Claude's sign-in screen", "Save the new account after you sign in"])
        plan["warnings"] = plan["warnings"][:2]
    elif name == "codex-consumers":
        target = _account(_codex("acc_7", "Codex spare", [_w("five_hour", 10, 4000), _w("seven_day", 25, 300000)],
                                 checked=-400), now, "", _UTC, 6)
        main = accounts["acc_6"]
        consumers = [{"pid": 9001, "kind": "tui", "started": now - 3600, "parent": "Terminal"},
                     {"pid": 9100, "kind": "app_server_child", "started": now - 600, "parent": "Code"}]
        plan.update(
            provider="codex", target=rules.plan_target(target), current=rules.plan_current(main),
            headroom={"now": rules.headroom_line(main, now), "after": rules.headroom_line(target, now)},
            running={"desktop": False, "sessions": [], "busy": 0, "unknown": 0, "foreign_claude": 0,
                     "codex_consumers": consumers},
            process_set=[[9001, 133812300000000000], [9100, 133812340000000000]],
            warnings=[{"code": "consumers",
                       "message": "2 Codex sessions are open. Wait for them to finish or end them one at a time."}],
            will=["Wait for Codex sessions to finish", "Stop Codex's background service", "Save the current sign-in",
                  "Restore Codex spare's sign-in", "Start Codex's background service again"],
            preserved=["Shared Codex settings, MCP servers and sessions"],
            account_bound=["ChatGPT-linked apps and connectors"], invisible=None, stop_tier="manual")
    plan["signature"] = _signature(plan["process_set"])
    return plan


# ---- fixtures and CLI ----

def write_fixtures(directory=FIXTURE_DIR):
    """Writes state-<scenario>.json and plan-<name>.json into ``directory``; returns the paths."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for prefix, names, build in (("state", SCENARIOS, example_state), ("plan", PLANS, example_plan)):
        for name in names:
            path = target / f"{prefix}-{name}.json"
            path.write_text(json.dumps(build(name), indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
            written.append(path)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m accountdeck.fixtures",
                                     description="Print an example /api/state payload or regenerate the contract fixtures.")
    parser.add_argument("scenario", nargs="?", default="typical", choices=list(SCENARIOS))
    parser.add_argument("--plan", choices=list(PLANS), help="print a SwitchPlan fixture instead")
    parser.add_argument("--now", type=float, default=BASE_NOW, help="Unix time of the payload (default BASE_NOW)")
    parser.add_argument("--write-fixtures", nargs="?", const=str(FIXTURE_DIR), metavar="DIR",
                        help=f"write every fixture (default {FIXTURE_DIR})")
    args = parser.parse_args(argv)
    if args.write_fixtures is not None:
        written = write_fixtures(args.write_fixtures)
        print(f"Wrote {len(written)} fixtures to {Path(args.write_fixtures)}")
        return 0
    payload = example_plan(args.plan, args.now) if args.plan else example_state(args.scenario, args.now)
    problems = validate_plan(payload) if args.plan else validate_state(payload)
    print(json.dumps(payload, indent=2))
    for problem in problems:
        print(f"contract violation: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
