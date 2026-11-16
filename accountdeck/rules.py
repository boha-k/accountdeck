"""The usage and display rules of the state contract (PLAN §3.3, §6.2-6.7), shared by everything that
derives what the dashboard shows: the engine's intel, the alert engine, the state validator, the menu
bar and the switch plans. `web/js/model.js` implements the same rules for the browser; keep the two in
step. Pure: every function takes `now` (and a time zone for clock copy) and reads no clock or file.
"""
from __future__ import annotations

import datetime as dt
import math

from .usage import LOCKED

PRIMARY_KINDS = frozenset({"session", "weekly", "model"})
HIDDEN_STATES = frozenset({"not_started", "resetting", "expected_refill"})
KIND_ORDER = {"session": 0, "weekly": 1, "model": 2, "other": 3}
ATTENTION_KINDS = ("out", "signin", "revoked_hint", "rate_limited", "stale", "dormant", "outage")   # display order
REFILL_KEEP = 3600            # a confirmed refill is shown for 60 min
STRIP_HORIZON = 86400
STRIP_MIN_USED = 5.0
BUCKET_LIMITS = (("minute", 900), ("hour", 3600), ("six_hours", 21600), ("day", 86400))
RELATIVE_LIMIT = 21600        # countdown copy up to 6 h, clock copy after
QUIET_ERRORS = frozenset({"blocked", "rate_limited", "outage"})   # keep the account's state (PLAN §3.3)
SEVERITY_RANK = {"watch": 1, "low": 2, "critical": 3, "out": 4}
LOCKED_TEXT = "Locked by Claude"     # a window Claude reports locked: not a limit used up, so never "back in …"
# `blocked` in the state of a lock on a limit that does not hold terminals (`holds_terminals`): it stays on that
# limit, whose percentage still shows, and neither the account nor the policy is held by it.
LOCKED_ELSEWHERE = "locked_elsewhere"
DORMANT_DAYS = 25
PRESERVED = ["Local Code sessions and archives", "Local MCP servers and extensions (shared config)",
             "Settings and preferences"]
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ---- copy shared with model.js (PLAN §7.5) ----

def countdown_text(secs):
    """Countdown copy: ceil minutes, seconds only under 5 min ("2d 4h", "1h 12m", "45m", "4m 59s", "20s", "now")."""
    if not _is_num(secs):
        return "—"
    if secs <= 0:
        return "now"
    if secs < 300:
        minutes, seconds = divmod(math.ceil(secs), 60)
        return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
    days, rest = divmod(math.ceil(secs / 60), 1440)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def clock_text(ts, now, tz=None):
    """"3:40 PM" within 24 h of ``now``, else "Thu 9:00 AM"; local time unless ``tz`` is given."""
    moment = dt.datetime.fromtimestamp(ts, tz) if tz is not None else dt.datetime.fromtimestamp(ts).astimezone()
    text = f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"
    return text if abs(ts - now) < 86400 else f"{_WEEKDAYS[moment.weekday()]} {text}"


def age_text(secs):
    """Reading age copy: "42m", "3h", "2d"."""
    minutes = int(secs // 60)
    if minutes < 60:
        return f"{max(minutes, 1)}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"


def js_round(value):
    # JavaScript Math.round semantics, so server copy and model.js agree on .5 values.
    return math.floor(value + 0.5)


def pct_left(left):
    """Whole-percent left as displayed: 100 − round(used), matching the used figure."""
    return 100 - js_round(100 - left)


# ---- rules (PLAN §6.2–6.7) ----

def locked(window):
    """True for a window Claude reports locked (`blocked` == usage.LOCKED, its reason in `blocked_reason`)."""
    return isinstance(window, dict) and window.get("blocked") == LOCKED


def pinned_match(names, pinned):
    """Whether any of a model's names starts with the pinned model: "Fable" is also the API's "Fable 5.1"."""
    pinned = (pinned or "").strip().casefold()
    return bool(pinned) and any(isinstance(name, str) and name.casefold().startswith(pinned) for name in names)


def holds_terminals(window, pinned):
    """The one rule for which limits hold Claude Code terminals, at any percentage and when locked (the policy's
    `considered` and `blocked`, and the state's `blocked` code): the 5-hour and weekly limits, the pinned model's,
    and a meter usage.py marked `gates`, one scoped to a model only while that model is pinned. Not another
    model's, another surface's, nor a codename's (`usage._no_meter`: its lock shows, but a lock on all of
    them at once must never leave the terminals nowhere to go, and hitting a real one stops a terminal, which
    the transcript watch reports)."""
    kind = window.get("kind")
    if kind in ("session", "weekly"):
        return True
    if kind == "model":
        return pinned_match((window.get("model"), (window.get("label") or "").removesuffix(" weekly")), pinned)
    return bool(window.get("gates")) and (not window.get("scope") or pinned_match((window["scope"],), pinned))


def locks(account):
    """The account's locked windows. A lock holds the whole account, whatever the window's percentage and
    also without one, until a reading clears it; as a window without a percentage keeps severity "unknown"
    (the contract), a lock counts as out at the account level: its state, headline, attention entry and
    eligibility, like the policy's `cli_policy.blocked`. A lock on a limit that does not hold terminals has
    the code LOCKED_ELSEWHERE instead, so it is none of these. A lock has no known end, so none of the
    account's resets is shown as a refill (`strip`, `next_reset`)."""
    return [w for w in account["windows"] + (account.get("other_windows") or []) if locked(w)]


def meters_out(account):
    """The account's used-up meters of kind "other" that hold terminals (usage.py `gates`, kept in the state
    only while they do): out like a primary window, so a new meter reads right without a release."""
    return [w for w in account.get("other_windows") or () if w.get("gates") and w.get("state") == "out"]


def temporary_limit(account):
    """True for an Ultra account that reports a 5-hour window: a verified Ultra account has none, so this one
    is on Anthropic's temporary limit until it is verified. Derived from the plan and the reading alone."""
    return (account.get("provider") in ("claude", "claude_cli") and str(account.get("plan") or "").casefold() == "ultra"
            and any(w.get("kind") == "session" for w in account.get("windows") or ()))


def held(account):
    """True while no reset brings the account back: Claude locked a limit of it (`locks`) or put it on hold."""
    return bool(locks(account)) or account.get("health") == "on_hold"


def band(left, blocked):
    """A blocked window is out whatever is left, also when that is unknown."""
    if blocked and blocked != LOCKED_ELSEWHERE:
        return "out"
    if left is None:
        return "unknown"
    if left <= 0.5:
        return "out"
    if left < 5:
        return "critical"
    if left < 20:
        return "low"
    if left < 50:
        return "watch"
    return "plenty"


def severity(used, blocked, pace):
    if used is None:
        return "unknown"         # also when blocked: a window without a percentage is "unknown" (`locks`)
    base = band(max(0.0, 100.0 - used), blocked)
    if pace is not None and pace.get("ready"):
        if base == "watch" and pace["delta"] <= 2:
            return "plenty"
        if base == "plenty" and pace["delta"] >= 15:
            return "watch"
    return base


def pace_stage(delta, ready):
    if not ready:
        return "too_early"
    size = abs(delta)
    if size <= 2:
        return "on_pace"
    side = "ahead" if delta > 0 else "under"
    if size <= 6:
        return f"slightly_{side}"
    if size <= 12:
        return side
    return f"far_{side}"


def pace(used, reset, minutes, at, samples):
    """Pace at reading time (PLAN §6.5); None when hidden or not computable."""
    if used is None or reset is None or not minutes or at is None:
        return None
    length = minutes * 60
    secs = reset - at
    if not 0 < secs <= length:
        return None
    elapsed = 1 - secs / length
    expected = round(elapsed * 100, 1)
    if elapsed < 0.03 or expected < 3:
        return None
    delta = round(used - expected, 1)
    ready = elapsed >= (0.10 if minutes >= 1440 else 0.06)
    eta = None
    if used > 0:
        to_full = max(0.0, 100.0 - used) / (used / (elapsed * length))
        if to_full < secs:
            eta = float(round(at + to_full))
    return {"expected": expected, "delta": delta, "stage": pace_stage(delta, ready), "ready": ready,
            "eta_full": eta, "samples": samples}


def age_tier(age, active):
    if age is None:
        return None
    if age <= (360 if active else 1200):
        return "fresh"
    if age <= 3600:
        return "aging"
    return "stale"


def reset_state(reset, now, at):
    if reset is None:
        return "none"
    if reset > now:
        return "scheduled"
    return "passed" if at is None or at < reset else "none"


def window_state(started, reset_state, severity, refilled, stale, now):
    if not started:
        return "not_started"
    if reset_state == "passed":
        return "expected_refill" if stale else "resetting"
    if severity == "out":
        return "out"
    if refilled is not None and now - refilled["confirmed"] <= REFILL_KEEP:
        return "refilled"
    return "last_known" if stale else "live"


def is_gating(window, pinned):
    if not window.get("primary") or window.get("used") is None:
        return False
    return window["severity"] == "out" or window["used"] >= 50 or pinned_match((window.get("model"),), pinned)


def pick_binding(windows, pinned):
    gating = [w for w in windows if is_gating(w, pinned)]
    if not gating:
        return None
    out = [w for w in gating if w["severity"] == "out"]
    if out:
        # The account is back only when its last out window resets.
        return max(out, key=lambda w: math.inf if w["reset"] is None else w["reset"])
    return min(gating, key=lambda w: w["left"])


def account_state(account):
    error = account["error"]
    if account["credential"]["state"] == "reauth_required":
        return "needs_signin"
    if error is not None and not error.get("outage") and error.get("code") not in QUIET_ERRORS:
        return "error"
    if account["last_good_at"] is None:
        return "unknown"
    if account_out(account):
        return "limited"
    return "stale" if account["age_tier"] == "stale" else "ok"


def headline_kind(account, binding):
    if account["state"] == "needs_signin":
        return "signin"
    if account["last_good_at"] is None:
        return "checking" if account["refreshing"] else "unknown"
    if (binding is not None and binding["severity"] == "out") or locks(account) or meters_out(account):
        return "out"
    if account["error"] is not None and account["error"].get("outage"):
        return "outage"
    if account["age_tier"] == "stale":
        return "stale"
    if binding is not None and binding["severity"] in ("critical", "low", "watch"):
        return binding["severity"]
    return "ready"


def out_text(window, now, tz):
    reset = window["reset"]
    if reset is None or reset <= now:
        return "Out"
    secs = reset - now
    return f"Out · back in {countdown_text(secs)}" if secs <= RELATIVE_LIMIT else f"Out until {clock_text(reset, now, tz)}"


def headline(account, binding, now, tz):
    kind = headline_kind(account, binding)
    text = {"signin": "Sign-in needed", "checking": "Checking…", "unknown": "No reading yet",
            "outage": "Paused · provider incident", "ready": "Ready"}.get(kind)
    if kind == "out":
        out = binding if binding is not None and binding["severity"] == "out" else next(iter(meters_out(account)), None)
        text = LOCKED_TEXT if locks(account) else out_text(out, now, tz)
    elif kind == "stale":
        text = f"Reading is {age_text(now - account['last_good_at'])} old"
    elif kind == "watch":
        text = f"Watch · {binding['label']} is the limit"
    elif kind in ("low", "critical"):
        lead = "Getting low" if kind == "low" else "Almost out"
        text = f"{lead} · {binding['label']} {pct_left(binding['left'])}% left"
    window = binding["id"] if kind in ("out", "critical", "low", "watch") and binding is not None else None
    return {"kind": kind, "text": text, "window": window}


def bucket(secs):
    for name, limit in BUCKET_LIMITS:
        if secs <= limit:
            return name
    return None


def chip(kind, left, severity, secs, account_out):
    # A blocked window can be out with quota left; it still comes "back in".
    if severity == "out" or (left is not None and left < 20):
        return "back_in"
    # "Use it" is meaningless on an account that is out: its quota cannot be spent.
    if not account_out and left is not None:
        if kind in ("weekly", "model") and left >= 30 and secs <= STRIP_HORIZON:
            return "use_it"
        if kind == "session" and left >= 50 and secs <= 1800:
            return "use_it"
    return "plain"


def account_out(account):
    return any(w.get("state") == "out" for w in account["windows"]) or bool(locks(account) or meters_out(account))


def strip(accounts, now):
    """Resetting-soon rows (PLAN §3.3/§6.6): refilled first, then by reset. None of a locked or on-hold
    account: neither has a known end, so no reset refills it (`held`)."""
    rows = []
    for account in accounts:
        if held(account):
            continue
        out = account_out(account)
        for w in account["windows"]:
            row = {"account": account["id"], "label": account["label"], "provider": account["provider"],
                   "window": w["id"], "window_label": w["label"], "reset": w["reset"], "left": w["left"],
                   "severity": w["severity"]}
            if w["state"] == "refilled":
                rows.append({**row, "reset": w["refilled"]["at"], "chip": "refilled", "bucket": "refilled",
                             "confirmed": w["refilled"]["confirmed"]})
            elif w["state"] == "resetting":
                rows.append({**row, "chip": "plain", "bucket": "minute"})
            elif (w["reset_state"] == "scheduled" and w["state"] != "not_started" and w["reset"] - now <= STRIP_HORIZON
                  and (out or (w["used"] is not None and w["used"] >= STRIP_MIN_USED))):
                secs = w["reset"] - now
                rows.append({**row, "chip": chip(w["kind"], w["left"], w["severity"], secs, out), "bucket": bucket(secs)})
    rows.sort(key=lambda r: (r["chip"] != "refilled", r["reset"]))
    return rows


def phrase(window):
    return window["label"] if window["kind"] == "model" else window["label"][:1].lower() + window["label"][1:]


def hero_candidates(accounts):
    return [(a, w) for a in accounts if not held(a) for w in a["windows"]       # like `strip`
            if w["reset_state"] == "scheduled" and w["state"] not in HIDDEN_STATES
            and w["severity"] in SEVERITY_RANK and w["used"] is not None and w["used"] >= STRIP_MIN_USED]


def next_reset(accounts, strip, now):
    hero = {"kind": "empty", "account": None, "label": None, "provider": None, "window": None, "window_label": None,
            "reset": None, "left": None, "severity": None, "message": "No accounts yet", "more_within_24h": 0}
    if not accounts:
        return hero
    upcoming = [r for r in strip if r["chip"] != "refilled" and r["reset"] > now]
    if all(a["last_good_at"] is None for a in accounts):
        return {**hero, "kind": "unknown", "message": "Waiting for the first readings", "more_within_24h": len(upcoming)}
    candidates = hero_candidates(accounts)
    if candidates:
        account, w = min(candidates, key=lambda p: (p[1]["reset"], -SEVERITY_RANK[p[1]["severity"]], -p[1]["used"]))
        reset, kind, message = w["reset"], "next_reset", f"{account['label']}'s {phrase(w)} limit refills"
        extra = {}
    else:
        refills = [(a, w) for a in accounts if not held(a) for w in a["windows"] if w["state"] == "refilled"]
        if not refills:
            return {**hero, "kind": "all_clear", "message": "Nothing is running low", "more_within_24h": len(upcoming)}
        account, w = max(refills, key=lambda p: p[1]["refilled"]["confirmed"])
        reset, kind, message = w["refilled"]["at"], "refilled", f"{account['label']}'s {phrase(w)} limit refilled"
        extra = {"confirmed": w["refilled"]["confirmed"]}
    more = [r for r in upcoming if (r["account"], r["window"]) != (account["id"], w["id"])]
    return {"kind": kind, "account": account["id"], "label": account["label"], "provider": account["provider"],
            "window": w["id"], "window_label": w["label"], "reset": reset, "left": w["left"],
            "severity": w["severity"], "message": message, "more_within_24h": len(more), **extra}


def headroom(account, pinned):
    """Recommender headroom: min left over session, weekly, the pinned model and model windows ≥ 50 % used."""
    lefts = [w["left"] for w in account["windows"] if w["left"] is not None and (
        w["kind"] in ("session", "weekly") or (w["kind"] == "model" and (w["used"] >= 50 or is_gating(w, pinned))))]
    return min(lefts) if lefts else None


def eligible(account, pinned):
    windows = account["windows"]
    error = account["error"]
    return (account["switchable"] and account["saved"] and account["credential"]["state"] != "reauth_required"
            and (error is None or error.get("outage"))
            and not any(w["state"] in ("resetting", "expected_refill", "out") for w in windows)
            and not locks(account) and not meters_out(account)
            and headroom(account, pinned) is not None
            and all(w["left"] >= 3 for w in windows if is_gating(w, pinned)))


def recommend(provider, accounts, pinned, now, tz):
    """Per-provider best account (PLAN §6.7, without hysteresis): returns (best, ranked accounts)."""
    mine = [a for a in accounts if a["provider"] == provider]
    active = next((a for a in mine if a["active"]), None)
    switchable = [a for a in mine if a["switchable"]]
    best = {"account": None, "label": None, "headroom": None, "policy": "availability", "reason": None,
            "confidence": "high", "suggest_switch": False, "current_is_best": False, "alternatives": [], "state": "none"}
    if len(switchable) < 2:
        return best, []
    candidates = [a for a in switchable if eligible(a, pinned)]
    if not candidates:
        if any(a["last_good_at"] is None for a in switchable):
            # Not every account has been read yet, so "no account has room" would be a guess.
            return {**best, "state": "unknown", "confidence": "low", "reason": "Waiting for the first readings"}, []
        return {**best, "state": "all_exhausted", "reason": exhausted_reason(switchable, pinned, now, tz)}, []

    def age(a):
        return math.inf if a["last_good_at"] is None else now - a["last_good_at"]

    fresh = [a for a in candidates if age(a) <= (600 if a["active"] else 1800)]
    pool = fresh or candidates
    ranked = sorted(pool, key=lambda a: (-headroom(a, pinned), a["sort_order"]))
    top = ranked[0]
    room = headroom(top, pinned)
    shown = pct_left(room)
    best.update(account=top["id"], label=top["label"], headroom=room, state="ok",
                current_is_best=top is active, alternatives=[a["id"] for a in ranked[1:] if a is not active][:3])
    if active is not None and age(active) > 1800:
        when = "not checked yet" if active["last_good_at"] is None else f"last checked {age_text(age(active))} ago"
        best.update(state="unknown", confidence="low", reason=f"Usage {when}")
        return best, ranked
    if top is active:
        best["reason"] = "You're on the best account"
    elif pinned:
        best["reason"] = f"Most {pinned} headroom ({shown}% left)"
    elif len(pool) == 1:
        best["reason"] = "Only account with headroom"
    elif fresh:
        best["reason"] = f"Most headroom ({shown}% left) and a fresh reading"
    else:
        best["reason"] = f"Most headroom ({shown}% left), based on a {age_text(age(top))} old reading"
    if not fresh:
        best["confidence"] = "low"
    if active is not None and top is not active and age(active) <= 600:
        binding = pick_binding(active["windows"], pinned)
        held = binding["left"] if binding is not None else headroom(active, pinned)
        best["suggest_switch"] = held is not None and held <= 20 and room - held >= 25
    return best, ranked


def exhausted_reason(accounts, pinned, now, tz):
    backs = []
    for account in accounts:
        binding = pick_binding(account["windows"], pinned)
        if binding is None or binding["reset"] is None or binding["reset"] <= now or locks(account):
            continue            # a locked account has no known time to come back (`locks`)
        backs.append((binding["reset"], account["label"]))
    if not backs:
        return "No account has room right now."
    reset, label = min(backs)
    return f"No account has room right now. Soonest back: {label} in {countdown_text(reset - now)} — {clock_text(reset, now, tz)}"


def attention(accounts, now, tz):
    items = []
    for account in accounts:
        base = {"account": account["id"], "label": account["label"]}
        out = [w for w in account["windows"] if w["state"] == "out"] + meters_out(account)
        credential = account["credential"]
        if locks(account):
            items.append({"kind": "out", **base, "message": LOCKED_TEXT, "reset": None})    # no known end
        elif out:
            window = max(out, key=lambda w: math.inf if w["reset"] is None else w["reset"])
            items.append({"kind": "out", **base, "message": out_text(window, now, tz), "reset": window["reset"]})
        if credential["state"] == "reauth_required":
            items.append({"kind": "signin", **base, "message": "Sign-in needs renewing"})
        if credential["revoked_hint"]:
            items.append({"kind": "revoked_hint", **base, "message": "Sign-in may have been revoked"})
        if account["paused_until"] is not None and account["paused_until"] > now:
            items.append({"kind": "rate_limited", **base,
                          "message": f"Checks paused until {clock_text(account['paused_until'], now, tz)}"})
        if account["age_tier"] == "stale":
            items.append({"kind": "stale", **base, "message": f"Reading is {age_text(now - account['last_good_at'])} old"})
        if (credential["dormant_days"] or 0) >= DORMANT_DAYS:
            items.append({"kind": "dormant", **base,
                          "message": f"Not opened for {credential['dormant_days']} days; its sign-in may expire"})
    order = {kind: index for index, kind in enumerate(ATTENTION_KINDS)}
    return sorted(items, key=lambda item: order[item["kind"]])[:6]


def window_order(window):
    return KIND_ORDER[window["kind"]], window["label"].casefold()


def headroom_line(account, now):
    parts = []
    for w in account["windows"]:
        if locked(w) or w.get("blocked") == LOCKED_ELSEWHERE:
            parts.append(f"{w['label']} locked by Claude")
        elif w["state"] == "not_started":
            parts.append(f"{w['label']} not started")
        elif w["left"] is None:
            parts.append(f"{w['label']} not reported")
        else:
            text = f"{w['label']} {pct_left(w['left'])}% left"
            if w["left"] < 20 and w["reset_state"] == "scheduled":
                text += f" (back in {countdown_text(w['reset'] - now)})"
            parts.append(text)
    return " · ".join(parts)


def plan_target(account):
    return {"id": account["id"], "label": account["label"], "email": account["email"], "plan": account["plan"],
            "windows": account["windows"], "checked": account["checked"], "age_tier": account["age_tier"],
            "dormant_days": account["credential"]["dormant_days"]}


def plan_current(account):
    return {"id": account["id"], "label": account["label"], "windows": account["windows"]}
