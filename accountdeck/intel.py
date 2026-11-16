"""Usage intelligence (PLAN §6.2-6.8): contract windows and accounts from normalized readings,
refill detection, pace, trend, the recommender with hysteresis and the use-soon policy, outage
classification and the Now band.

The rules the state contract checks are the very functions `contract.validate_state` applies
(`rules.band`, `rules.pace`, `rules.window_state`, `rules.pick_binding`, `rules.headline`, `rules.strip`, ...), so an engine
snapshot, a fixture and the validator cannot drift apart; the functions named as in the plan
(severity, window_state, age_tier, pace, binding, headline, resetting_soon, now_band) give them
plan-shaped signatures. Everything here is pure: the caller passes `now`, the settings and the
previous recommender picks, and nothing reads a clock, a file or the network. Unknown is never 0:
a window without a percentage stays `None`, a passed reset hides the percentage until a fresh
reading confirms it, and a stale reading is marked, never presented as current.
"""
from __future__ import annotations

import copy
import math

from . import rules
from .contract import (ACCOUNT_PROVIDERS, CREDENTIAL_STATES, ERROR_CODES, PROVIDER_FAILURES, STATUS_LEVELS,
                       WINDOW_KINDS)
from .rules import PRIMARY_KINDS, REFILL_KEEP, age_text, countdown_text

REFILL_DROP = 25.0            # a used figure this much lower at the next reading means the window refilled
HYSTERESIS_POINTS = 10.0      # a challenger must beat the previous pick by this much headroom ...
HYSTERESIS_HOLD = 300.0       # ... and a new pick is held at least this long (PLAN §6.7)
USE_SOON_CEILING = 95.0       # use-soon ranks accounts whose weekly window has at most this much left
FRESH_ACTIVE, FRESH_INACTIVE = 600.0, 1800.0      # reading age a recommendation counts as fresh (PLAN §6.7)
TREND_POINTS = 32
# Antigravity has no status page here: AccountDeck sends no request to Google, so its status stays unknown.
STATUS_URLS = {"claude": "https://status.claude.com", "codex": "https://status.openai.com", "claude_cli": "https://status.claude.com",
               "antigravity": None}
PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex", "claude_cli": "Claude Code (terminal)", "antigravity": "Antigravity"}
STATUS_SUMMARIES = {"ok": "All systems operational", "degraded": "Partially degraded service",
                    "outage": "Major outage", "unknown": "Status not checked yet"}
_ERROR_CODES = frozenset(ERROR_CODES + PROVIDER_FAILURES)


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) \
        else None


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


# ---- the plan's rule functions (§6.2-6.6) ----

def severity(win, pacing=None):
    """Severity band of a window (§6.2). It keys off `left` whatever `usage_display` shows: `unknown`
    without a percentage, `out` when blocked or at most 0.5 % is left, then critical / low / watch
    below 5 / 20 / 50 % left. A ready pace demotes `watch` to `plenty` when on pace (delta <= +2)
    and promotes `plenty` to `watch` when far ahead of an even pace (delta >= +15)."""
    return rules.severity(win.get("used"), win.get("blocked"), pacing)


def window_state(win, refilled, stale, now):
    """State of a window (§6.3) from its started / reset_state / severity fields: `not_started`,
    `resetting` (a reset passed and the reading predates it; the percentage is hidden) or
    `expected_refill` (the same, on a stale account), `out`, `refilled` (a refill confirmed in the
    last 60 min), `last_known` (stale account) or `live`."""
    return rules.window_state(win["started"], win["reset_state"], win["severity"], refilled, stale, now)


def age_tier(age, active):
    """`fresh` up to 6 min (active account) or 20 min, `aging` up to 60 min, else `stale`; None without a reading."""
    return rules.age_tier(age, active)


def pace(readings, win, at):
    """Pace of one window at the time of its reading `at` (§6.5), or None while hidden, out, unknown or
    too early; `readings` are the window's stored readings, of which those of the current reset
    epoch are counted as `samples`."""
    return _pace_at(win, at, epoch_samples(readings, win.get("reset"), win.get("minutes")))


def binding(windows, pinned=""):
    """The window that limits an account (§6.4): among the session, weekly and model windows that are at
    least 50 % used, out or the pinned model, the one that is out for longest, else the lowest `left`."""
    return rules.pick_binding(windows, pinned)


def headline(account, window, now, tz=None):
    """{"kind", "text", "window"} of an account (§6.4); `window` is its binding window or None."""
    return rules.headline(account, window, now, tz)


def resetting_soon(accounts, now):
    """The Resetting-soon strip (§6.6): refilled windows first, then by reset, each with its chip and bucket."""
    return rules.strip(accounts, now)


def now_band(accounts, best, now, tz=None, strip=None):
    """{"next_reset", "best", "attention"} (§6.6). `best` is the per-provider recommendation from
    `recommend`; `strip` is `resetting_soon(accounts, now)` when the caller already has it."""
    strip = resetting_soon(accounts, now) if strip is None else strip
    return {"next_reset": rules.next_reset(accounts, strip, now), "best": best, "attention": rules.attention(accounts, now, tz)}


# ---- windows (§6.1-6.3, §6.5) ----

def normalize_window(raw):
    """A usage.py window cleaned for display, or None when it cannot be shown. Readings stored by older
    builds (or carried over from the PC) may miss fields; nothing is guessed beyond what usage.py itself
    would have derived."""
    if not isinstance(raw, dict) or not _text(raw.get("id")):
        return None
    kind = raw.get("kind") if raw.get("kind") in WINDOW_KINDS else "other"
    label = _text(raw.get("label")) or raw["id"].replace("_", " ").capitalize()
    model = _text(raw.get("model"))
    if kind == "model" and model is None:
        kind = "other"
    used = _number(raw.get("used"))
    used = None if used is None else max(0.0, used)
    reset = _number(raw.get("reset"))
    minutes = _number(raw.get("minutes"))
    started = raw.get("started")
    if not isinstance(started, bool) or (not started and reset is not None):
        started = not (used == 0 and reset is None)
    return {"id": raw["id"], "kind": kind, "label": label, "short": _text(raw.get("short")) or label[:12],
            "model": model if kind == "model" else None, "minutes": minutes if minutes and minutes >= 1 else None,
            "used": used, "reset": reset, "started": started, "blocked": _text(raw.get("blocked")),
            "gates": raw.get("gates") is True, "scope": _text(raw.get("scope"))}


def _refill(refilled, now):
    """A refill record kept for 60 min after its confirmation, or None."""
    if not isinstance(refilled, dict):
        return None
    at, confirmed = _number(refilled.get("at")), _number(refilled.get("confirmed"))
    if at is None or confirmed is None or not (at <= confirmed <= now + 1 and now - confirmed <= REFILL_KEEP):
        return None
    return {"at": at, "confirmed": confirmed, "from": _number(refilled.get("from"))}


def _pace_at(window, at, samples):
    used = window["used"]
    if used is None or rules.band(max(0.0, 100.0 - used), window["blocked"]) == "out":
        return None
    return rules.pace(used, window["reset"], window["minutes"], at, samples)


def build_window(raw, *, now, at, stale, refilled=None, samples=0):
    """One contract window (§3.3) from a normalized window read at `at`. The percentage is shown only
    for a started window whose reset has not passed since the reading."""
    reset_state = rules.reset_state(raw["reset"], now, at)
    used = None if not raw["started"] or reset_state == "passed" else raw["used"]
    window = {"id": raw["id"], "kind": raw["kind"], "label": raw["label"], "short": raw["short"], "model": raw["model"],
              "minutes": raw["minutes"], "used": used, "left": None if used is None else max(0.0, 100.0 - used),
              "known": used is not None, "started": raw["started"], "reset": raw["reset"], "reset_state": reset_state,
              "state": None, "severity": "unknown", "binding": False, "pace": None, "blocked": raw["blocked"],
              "primary": raw["kind"] in PRIMARY_KINDS, "refilled": _refill(refilled, now)}
    window["pace"] = _pace_at(window, at, _count(samples))
    window["severity"] = severity(window, window["pace"])
    window["state"] = window_state(window, window["refilled"], stale, now)
    return window


def detect_refill(prev, cur):
    """Refills between two readings (§6.3): [{"window", "reset_at", "confirmed_at", "previous_used"}].

    A window refilled when the previous reading's reset had passed by the time of the new reading
    and either the reset moved forward, the used figure dropped by at least 25 points, or the
    window is gone. `prev` and `cur` are usage.py readings; `prev` may be None (a first reading)."""
    if not isinstance(prev, dict) or not isinstance(cur, dict):
        return []
    checked = _number(cur.get("checked"))
    if checked is None:
        return []
    current = {w["id"]: w for w in map(normalize_window, cur.get("windows") or ()) if w is not None}
    found = []
    for old in map(normalize_window, prev.get("windows") or ()):
        if old is None or old["reset"] is None or old["reset"] > checked:
            continue
        new = current.get(old["id"])
        refilled = (new is None or (new["reset"] is not None and new["reset"] > old["reset"])
                    or (old["used"] is not None and new["used"] is not None and new["used"] <= old["used"] - REFILL_DROP))
        if refilled:
            found.append({"window": old["id"], "reset_at": old["reset"], "confirmed_at": checked,
                          "previous_used": old["used"]})
    return found


def epoch_readings(readings, reset, minutes):
    """The readings of one window from its current reset epoch, oldest first."""
    if reset is None or not minutes:
        return []
    start = reset - minutes * 60
    return sorted((r for r in readings if _number(r.get("at")) is not None and _number(r.get("used")) is not None
                   and start <= r["at"] <= reset), key=lambda r: r["at"])


def epoch_samples(readings, reset, minutes):
    """How many readings of the current reset epoch back a pace: readings of an earlier epoch never count."""
    return len(epoch_readings(readings, reset, minutes))


def trend(readings, reset, minutes, now):
    """At most 32 [ts, used] points of the current epoch, in time order, never in the future."""
    points = [[float(r["at"]), round(float(r["used"]), 1)] for r in epoch_readings(readings, reset, minutes)
              if r["at"] <= now]
    if len(points) > TREND_POINTS:
        step = (len(points) - 1) / (TREND_POINTS - 1)
        points = [points[round(i * step)] for i in range(TREND_POINTS)]
    return points


def trend_window(usage):
    """The window a trend follows: the weekly window, else the first primary window, with a reset."""
    windows = [w for w in map(normalize_window, (usage or {}).get("windows") or ()) if w is not None]
    primary = sorted((w for w in windows if w["kind"] in PRIMARY_KINDS and w["reset"] is not None and w["minutes"]),
                     key=rules.window_order)
    return next((w for w in primary if w["kind"] == "weekly"), primary[0] if primary else None)


# ---- accounts (§3.3, §6.4) ----

def _credential(raw):
    raw = raw if isinstance(raw, dict) else {}
    dormant = _number(raw.get("dormant_days"))
    return {"state": raw.get("state") if raw.get("state") in CREDENTIAL_STATES else "unknown",
            "expires_in": _number(raw.get("expires_in")), "last_outcome": _text(raw.get("last_outcome")),
            "dormant_days": None if dormant is None or dormant < 0 else int(dormant),
            "session_expires_in_days": _number(raw.get("session_expires_in_days")),
            "revoked_hint": bool(raw.get("revoked_hint"))}


def _error(raw):
    if not isinstance(raw, dict):
        return None
    return {"code": raw.get("code") if raw.get("code") in _ERROR_CODES else "network",
            "message": _text(raw.get("message")) or "The last check failed.", "since": _number(raw.get("since")),
            "outage": bool(raw.get("outage"))}


def _credits(usage):
    return [dict(c) for c in usage.get("credits") or () if isinstance(c, dict) and _text(c.get("label"))]


def _reset_credits(usage):
    """A reading's limit-reset rows as the state has them, for an account the engine says nothing about: not usable."""
    rows = []
    for index, row in enumerate(usage.get("reset_credits") or ()):
        if not isinstance(row, dict):
            continue
        count = row.get("count")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 1:
            rows.append({"count": count, "expires": _number(row.get("expires")), "title": (_text(row.get("title")) or "")[:80] or None,
                         "key": f"r-{index}", "restores": None, "usable": False,
                         "reason": "AccountDeck does not read this account's limit resets."})
    return rows


def _raw_windows(usage):
    return [{"key": r["key"], "used": _number(r.get("used")), "reset": _number(r.get("reset"))}
            for r in usage.get("raw_windows") or () if isinstance(r, dict) and _text(r.get("key"))]


def _trend_points(acct, usage, at, now):
    """The engine's trend points, unless the window they follow has reset since the reading: its
    percentage is hidden then, and the old epoch's line would still climb to it."""
    followed = trend_window(usage)
    if at is None or followed is None or rules.reset_state(followed["reset"], now, at) == "passed":
        return []
    points = []
    for point in acct.get("trend") or ():
        stamp, used = (_number(v) for v in point) if isinstance(point, (list, tuple)) and len(point) == 2 else (None, None)
        if stamp is not None and used is not None and stamp <= now:
            points.append([stamp, used])
    return sorted(points)[-TREND_POINTS:]


def build_account(acct, *, now, pinned="", tz=None):
    """A contract account (§3.3) from the engine's view of one account:

    id, provider, label, email, plan, active, switchable, saved, imported_from, owner, credential,
    checked, last_good_at, refreshing, error, paused_until, next_check, sort_order, usage (the last
    good normalized reading or None), refills {window: {"at", "confirmed", "from"}}, samples
    {window: int} and trend [[ts, used]]. Only these fields reach the result: identities, org ids
    and secrets stay behind."""
    usage = acct.get("usage") if isinstance(acct.get("usage"), dict) else None
    at = _number(acct.get("last_good_at")) if usage is not None else None
    active = bool(acct.get("active"))
    tier = age_tier(None if at is None else now - at, active)
    stale = tier == "stale"
    refills, samples = acct.get("refills") or {}, acct.get("samples") or {}
    windows, others = [], []
    if at is not None:
        for raw in map(normalize_window, usage.get("windows") or ()):
            if raw is None or any(w["id"] == raw["id"] for w in windows + others):
                continue
            holds = rules.holds_terminals(raw, pinned)
            if acct["provider"] == "claude_cli" and rules.locked(raw) and not holds:
                raw["blocked"] = rules.LOCKED_ELSEWHERE     # shown on this limit, but no hold on the account
            window = build_window(raw, now=now, at=at, stale=stale, refilled=refills.get(raw["id"]),
                                  samples=samples.get(raw["id"], 0))
            if window["primary"]:
                windows.append(window)
            elif (window["used"] or 0) > 0 or window["reset"] is not None or window["blocked"]:
                if raw["gates"] and holds:
                    window["gates"] = True
                others.append(window)
    windows.sort(key=rules.window_order)
    others.sort(key=rules.window_order)
    known = at is not None
    account = {
        "id": acct["id"], "provider": acct["provider"],
        "label": _text(acct.get("label")) or _text(acct.get("email")) or acct["id"],
        "email": _text(acct.get("email")), "plan": _text(acct.get("plan")), "active": active,
        "switchable": bool(acct.get("switchable")), "saved": bool(acct.get("saved", True)),
        "imported_from": acct.get("imported_from"), "owner": acct.get("owner") or "v2", "state": None,
        "headline": None, "credential": _credential(acct.get("credential")), "checked": _number(acct.get("checked")),
        "last_good_at": at, "stale": stale, "age_tier": tier, "refreshing": bool(acct.get("refreshing")),
        "error": _error(acct.get("error")), "next_check": _number(acct.get("next_check")),
        "paused_until": _number(acct.get("paused_until")), "score": None, "recommended": False, "rank": None,
        "sort_order": int(_number(acct.get("sort_order")) or 0), "windows": windows, "other_windows": others,
        "raw_windows": _raw_windows(usage) if known else [], "credits": _credits(usage) if known else [],
        "tokens": usage.get("tokens") if known and isinstance(usage.get("tokens"), dict) else None,
        "trend": _trend_points(acct, usage, at, now),
    }
    # Limit resets: the engine's rows and block (resets.py); without them, the reading's rows, which nothing can use.
    resets = acct.get("limit_resets") if isinstance(acct.get("limit_resets"), dict) else None
    reset_credits = copy.deepcopy(resets["rows"]) if resets is not None else _reset_credits(usage) if known else []
    if acct["provider"] == "claude_cli":
        account.update(excluded=bool(acct.get("excluded")), health=acct.get("health", "ok"),
                       live_expired=bool(acct.get("live_expired")), login_ends_at=_number(acct.get("login_ends_at")))
    if reset_credits:
        account["reset_credits"] = reset_credits
    if resets is not None and resets.get("block") is not None:
        account["resets"] = copy.deepcopy(resets["block"])
    account["temporary_limit"] = rules.temporary_limit(account)
    limiting = binding(windows, pinned)
    if limiting is not None:
        limiting["binding"] = True
    account["state"] = rules.account_state(account)
    account["headline"] = headline(account, limiting, now, tz)
    return account


# ---- recommender (§6.7) ----

def _weekly(account):
    return next((w for w in account["windows"] if w["kind"] == "weekly" and w["left"] is not None
                 and w["reset_state"] == "scheduled"), None)


def _empty_best(policy):
    return {"account": None, "label": None, "headroom": None, "policy": policy, "reason": None, "confidence": "high",
            "suggest_switch": False, "current_is_best": False, "alternatives": [], "state": "none"}


def _keep_previous(ranked, favoured, prev, pinned, now):
    """(top, since): the previous pick stays while it is still ranked, unless the hold has passed and
    the challenger beats it by 10 points of headroom (or is the use-soon favourite)."""
    challenger = ranked[0]
    held = next((a for a in ranked if prev is not None and a["id"] == prev.get("account")), None)
    if held is None:
        return challenger, now
    since = _number(prev.get("since"))
    since = now if since is None else since
    if held is challenger:
        return held, since
    beats = challenger is favoured or rules.headroom(challenger, pinned) - rules.headroom(held, pinned) >= HYSTERESIS_POINTS
    if now - since >= HYSTERESIS_HOLD and beats:
        return challenger, now
    return held, since


def recommend(provider, accounts, settings, prev_state, now, *, tz=None):
    """(best, ranked accounts, pick) for one provider, never across providers (§6.7).

    `settings` supplies `pinned_model` and `use_soon`; `prev_state` is the recommender_state
    mapping {provider: {"account", "since"}} that implements the hysteresis. A provider incident does
    not pause the recommendation: incidents often name another product. `pick` is this provider's new
    recommender_state entry, or None when nothing is recommended."""
    pinned = _text(settings.get("pinned_model")) or ""
    policy = "use_soon" if settings.get("use_soon") else "availability"
    prev = prev_state.get(provider) if isinstance(prev_state, dict) else None
    prev = prev if isinstance(prev, dict) else None
    mine = [a for a in accounts if a["provider"] == provider]
    active = next((a for a in mine if a["active"]), None)
    switchable = [a for a in mine if a["switchable"]]
    best = _empty_best(policy)
    if len(switchable) < 2:
        return best, [], None
    candidates = [a for a in switchable if rules.eligible(a, pinned)]
    if not candidates:
        if any(a["last_good_at"] is None for a in switchable):
            return {**best, "state": "unknown", "confidence": "low", "reason": "Waiting for the first readings"}, [], None
        return {**best, "state": "all_exhausted", "reason": rules.exhausted_reason(switchable, pinned, now, tz)}, [], None

    def age(a):
        return math.inf if a["last_good_at"] is None else now - a["last_good_at"]

    fresh = [a for a in candidates if age(a) <= (FRESH_ACTIVE if a["active"] else FRESH_INACTIVE)]
    pool = fresh or candidates
    ranked = sorted(pool, key=lambda a: (-rules.headroom(a, pinned), a["sort_order"]))
    favoured = None
    if policy == "use_soon":
        perishable = [a for a in ranked if (w := _weekly(a)) is not None and w["left"] <= USE_SOON_CEILING]
        if perishable:
            favoured = min(perishable, key=lambda a: (_weekly(a)["reset"], -rules.headroom(a, pinned), a["sort_order"]))
            ranked.remove(favoured)
            ranked.insert(0, favoured)
    top, since = _keep_previous(ranked, favoured, prev, pinned, now)
    ranked.remove(top)
    ranked.insert(0, top)
    headroom = rules.headroom(top, pinned)
    shown = rules.pct_left(headroom)
    best.update(account=top["id"], label=top["label"], headroom=headroom, state="ok", current_is_best=top is active,
                alternatives=[a["id"] for a in ranked[1:] if a is not active][:3])
    pick = {"account": top["id"], "since": since}
    if active is not None and age(active) > FRESH_INACTIVE:
        when = "not checked yet" if active["last_good_at"] is None else f"last checked {age_text(age(active))} ago"
        best.update(state="unknown", confidence="low", reason=f"Usage {when}")
        return best, ranked, pick
    if top is active:
        best["reason"] = "You're on the best account"
    elif top is favoured:
        best["reason"] = f"Weekly quota expires in {countdown_text(_weekly(top)['reset'] - now)} — use it first"
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
    if active is not None and top is not active and age(active) <= FRESH_ACTIVE:
        limiting = binding(active["windows"], pinned)
        left = limiting["left"] if limiting is not None else rules.headroom(active, pinned)
        best["suggest_switch"] = left is not None and left <= 20 and headroom - left >= 25
    return best, ranked, pick


# ---- outage (§6.8) ----

def classify_outage(provider, page, observed, *, now):
    """The contract status of one provider: level = max(status page, observed), with the source that
    decided it. `page` is a StatusSource.fetch() result or None; `observed` is
    ObservedStatus.evaluate() or None. An unreachable status page is `unknown` and gates nothing."""
    page = page if isinstance(page, dict) else None
    level = page["level"] if page is not None and page.get("level") in STATUS_LEVELS else "unknown"
    incident = page.get("incident") if page is not None and isinstance(page.get("incident"), dict) else None
    if incident is not None and not (_text(incident.get("title")) and _number(incident.get("started")) is not None):
        incident = None
    url = page.get("url") if page is not None else None
    status = {"level": level, "summary": _text(page.get("summary")) if page is not None else None,
              "source": "none" if level == "unknown" else "statuspage",
              "checked": _number(page.get("checked")) if page is not None else None,
              "since": _number(page.get("since")) if page is not None else None,
              "url": url if isinstance(url, str) and url.startswith("https://") else STATUS_URLS[provider],
              "incident": incident and {"title": incident["title"].strip(), "impact": _text(incident.get("impact")),
                                        "status": _text(incident.get("status")), "started": _number(incident["started"])}}
    status["summary"] = status["summary"] or STATUS_SUMMARIES[level]
    if level != "outage" and isinstance(observed, dict) and observed.get("level") == "outage":
        status.update(level="outage", source="observed", summary=f"{PROVIDER_NAMES[provider]} isn't responding",
                      since=_number(observed.get("since")), checked=now if status["checked"] is None else status["checked"])
    return status


# ---- the whole state ----

def enrich_state(raw, *, now, settings, recommender_state=None, tz=None):
    """The contract state from the engine's raw snapshot: `raw` carries every §3.3 key except that
    `accounts` holds build_account() inputs and `now_band` / `resetting_soon` are absent. Returns
    (state, recommender_state); the caller persists the picks for the next call's hysteresis."""
    pinned = _text(settings.get("pinned_model")) or ""
    accounts = [build_account(a, now=now, pinned=pinned, tz=tz) for a in raw["accounts"]]
    picks, best = {}, {}
    for provider in ACCOUNT_PROVIDERS:          # never Antigravity: nothing recommends or switches its accounts
        best[provider], ranked, pick = recommend(provider, accounts, settings, recommender_state, now, tz=tz)
        for rank, account in enumerate(ranked, 1):
            account["rank"], account["score"] = rank, round(rules.headroom(account, pinned) / 100, 2)
        if best[provider]["state"] in ("ok", "unknown") and best[provider]["account"] is not None:
            next(a for a in accounts if a["id"] == best[provider]["account"])["recommended"] = True
        if pick is not None:
            picks[provider] = pick
    strip = resetting_soon(accounts, now)
    state = {**raw, "now": now, "accounts": accounts, "resetting_soon": strip,
             "now_band": now_band(accounts, best, now, tz, strip)}
    return state, picks
