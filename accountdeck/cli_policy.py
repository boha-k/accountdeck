"""The terminal auto-switch policy (§7). Pure: no I/O or clock reads.

`evaluate` decides, for the active terminal account, whether to switch now and to which account:
- threshold: any considered window (5-hour while Claude reports one, weekly, Fable's weekly, the pinned model's
  weekly, a meter of a new kind: rules.holds_terminals) at or past `cli_threshold`, or projected to pass it before
  the next read can land (the burn-rate forecast);
- out / login: a limit reached or locked (`blocked`), a sign-in that needs renewing, or a login that ends within
  LOGIN_ACTIVE (1 hour) switches whatever the threshold;
- eligible targets have a reading at most 15 minutes old and at least 10 points of room below the
  threshold in every considered window; left-out, frozen, on-hold, needs-sign-in and locked accounts never
  qualify, nor one whose login ends within LOGIN_TARGET (12 hours), whose reads are paused or failed last
  time, or whose automatic switch failed lately (cli_auto backs it off: 10 minutes, doubling to an hour);
  when none qualifies, those only too old, and those whose last read failed (not paused) but may have room,
  are read again (`refresh`, spaced by the scheduler's governor); only too old ones hold a threshold switch;
- a window whose reset time has passed since its reading counts as refilled (0 used) until the next
  reading; a blocked window stays blocked until a reading clears it;
- out / login and none eligible: the account with the most room by its last good reading (at most
  FALLBACK_AGE old) is used at once, without the fresh read cli_auto otherwise waits for (`read_first`):
  it beats staying on a dead account, so a login ending within LOGIN_TARGET still
  qualifies while it has LOGIN_FALLBACK (2 hours) left; never a needs-sign-in, left-out, frozen, on-hold,
  locked or full one. An account in use that works for no model (its 5-hour or weekly limit is spent, it is on
  hold or its login is gone) is also left for one whose week has room though a model's own limit there is used
  up (`spare`): other models still work on it. One that is only out of a model's limit is never left for such;
- once no account qualifies, every account is used up to DRAIN_CEILING (99 %): the active one stays
  until it reaches it, then a target with DRAIN_ROOM points left below it is used. "Every account is
  near or past N %" (under 10 points of room) and "All terminal accounts are low" are said only when that
  is why none qualifies; otherwise the reason names why (`why_none`, e.g. "2 need sign-in, 1 can't be read");
- holds: an expired idle terminal token (not when out / login), and a 5-hour refill within 10 minutes when only the 5-hour
  window is past. An Anthropic incident never holds: a switch only
  needs the target's own fresh reading;
- order (`ordered`): soonest weekly reset, with resets within an hour of their group's earliest counting
  as equal and broken by room; most room, with room within a point of its group's most broken by the
  sooner reset; or the manual order. It sorts by keys, so the same accounts always give the same target;
- the user's own choice (a switch in AccountDeck, Undo included) holds against the threshold switch for
  MANUAL_HOLD (2 hours) while the terminals stay on the account chosen, but only until a reading or the forecast
  says DRAIN_CEILING: a limit reached or a login that ends still moves them;
- the early switch (a setting) spends weekly allowance that expires within 24 hours: only for a week that ends
  over an hour sooner than the active account's (resets within the hour count as equal, and Claude reports one
  reset a second apart from reading to reading), never within 2 hours of any automatic switch or of a manual
  choice.
"""
from collections import Counter
from datetime import datetime
import math

from .core import is_frozen
from .rules import LOCKED_TEXT, holds_terminals, model_names, watched_model

HOUR = 3600
# How long the user's own choice holds, and how long after any automatic switch an early switch waits.
MANUAL_HOLD = EARLY_GAP = 2 * HOUR
LOGIN_TARGET, LOGIN_ACTIVE = 12 * HOUR, HOUR     # a login ending this soon is no target / is left at once
# The out / login fallback still takes a login ending within LOGIN_TARGET while it has this much left: the
# terminals then get at least an hour on it before its last hour (LOGIN_ACTIVE) moves them on again.
LOGIN_FALLBACK = LOGIN_ACTIVE + HOUR
# How old a last good reading the out / login fallback still acts on. Usage-API outages and rate-limit
# pauses last minutes to hours; an account not read for a whole day keeps failing on its own (most often
# a login that no longer renews), so its room is no longer worth a switch without a check.
FALLBACK_AGE = 24 * HOUR


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def reading_age(account, now):
    """Seconds since the account's last good reading, or None without one."""
    age = number(account.get("reading_age"))
    if age is None and number(account.get("last_good_at")) is not None:
        age = max(0, now - account["last_good_at"])
    return age


def refilled(window, now, read_at):
    """True when the window's reset time passed after its reading: it counts as refilled until the next
    reading (targets are read again before a switch; the active account is not left right after it)."""
    at = reset(window)
    return None not in (at, now, read_at) and read_at < at <= now


def _limit_name(window):
    """How the policy's words name a limit beyond the 5-hour and weekly ones: "Fable weekly", a meter's own label."""
    if window.get("kind") == "model":
        return (window.get("model") or model_names(window)[1]) + " weekly"
    return window.get("label") or window.get("id")


def considered(account, settings, now=None):
    """[(label, % used or None, window)]: every limit that holds the terminals (rules.holds_terminals): the
    5-hour and weekly windows, always first, then Fable's and the pinned model's weekly windows and any meter of
    a new kind. Given `now`, a window whose reset has passed since the reading counts as 0 % used."""
    windows, pinned = account.get("windows") or {}, settings.get("pinned_model")
    if isinstance(windows, list):
        five = next((w for w in windows if w.get("kind") == "session"), None)
        weekly = next((w for w in windows if w.get("kind") == "weekly"), {})
        more = [(_limit_name(w), w) for w in (*windows, *(account.get("other_windows") or ()))
                if w is not five and w is not weekly and holds_terminals(w, pinned)]
    else:
        five, weekly = windows.get("five_hour"), windows.get("weekly", {})
        more = [(label + " weekly", w) for label, w in windows.get("models", {}).items()
                if watched_model((label, model_names(w)[1]), pinned)]
    age = None if now is None else reading_age(account, now)
    read_at = None if age is None else now - age

    def used(window):
        return 0 if refilled(window, now, read_at) else number(window.get("used"))
    # A reading without a 5-hour window has no 5-hour limit to respect, like a window that has not started.
    # Claude's usage API stopped reporting one on 2026-10-31 (`five_hour: null`, no "session" limit); reading
    # that as unknown left no account eligible, so nothing switched and the active account ran out.
    return [("5-hour", 0 if five is None or five.get("started") is False else used(five), five or {}),
            ("weekly", used(weekly), weekly), *((label, used(w), w) for label, w in more)]


def blocked(account, settings):
    """The one lock rule, for targets and the active account alike: True when Claude locked a limit that holds
    the terminals (whatever its percentage). A lock on another model or surface does not."""
    return any(w.get("blocked") for _, _, w in considered(account, settings))


FORECAST_SPAN, FORECAST_MIN_SPAN = 1200, 120    # the burn rate comes from the last 20 min of readings
SAME_RESET = 300     # reset times of one window drift by a second between readings (21:29:59 vs 21:30:00)


def same_window(a, b):
    """True when two readings' reset times name the same window; a new window resets hours later."""
    a, b = number(a), number(b)
    return a == b if a is None or b is None else abs(a - b) <= SAME_RESET


def forecast(readings, now, horizon):
    """Projected % used at `now + horizon` from one window's recent readings (oldest first, {"at", "used",
    "reset"}), or None without a rising trend or when the window refills first."""
    if not readings:
        return None
    last = readings[-1]
    recent = [r for r in readings if r["at"] >= last["at"] - FORECAST_SPAN and same_window(r.get("reset"), last.get("reset"))]
    first = recent[0]
    span = last["at"] - first["at"]
    if span < FORECAST_MIN_SPAN or last["used"] <= first["used"]:
        return None
    if number(last.get("reset")) is not None and last["reset"] <= now + horizon:
        return None
    return last["used"] + (last["used"] - first["used"]) / span * (now + horizon - last["at"])


def reset(window):
    return number(window.get("resets_at", window.get("reset")))


def duration(seconds):
    minutes = max(0, math.ceil(seconds / 60))
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes} min"


def ordered(rows, mode, manual=()):
    """`rows` in `mode`'s order. Near-equal values form groups anchored at the group's best value, so the
    order is a plain sort by keys: the same accounts come out in the same order whatever order they came in."""
    def room(row):
        return row["room"] if row["room"] is not None else -math.inf

    def resets(row):
        return row["weekly_reset"] if row["weekly_reset"] is not None else math.inf

    def name(row):
        return row["label"].casefold(), row["id"]
    if mode == "most_room":
        primary, secondary, near = (lambda row: -room(row)), resets, 1
    else:
        primary, secondary, near = resets, (lambda row: -room(row)), HOUR
    anchor, keys = None, {}
    for row in sorted(rows, key=lambda row: (primary(row), secondary(row), name(row))):
        if anchor is None or not primary(row) - anchor <= near:      # unknown values (inf - inf) stand alone
            anchor = primary(row)
        keys[row["id"]] = (anchor, secondary(row), primary(row), name(row))
    rows = sorted(rows, key=lambda row: keys[row["id"]])
    if mode == "manual":
        position = {key: index for index, key in enumerate(manual)}
        rows.sort(key=lambda row: position.get(row["id"], math.inf))
    return rows


# Why a non-active account is not eligible, grouped for "No account can take over (…)": (key, one, many).
WHY = (("signin", "needs sign-in", "need sign-in"), ("ending", "has a login ending soon", "have logins ending soon"),
       ("unread", "can't be read", "can't be read"), ("hold", "is on hold", "are on hold"),
       ("blocked", "is locked by Claude", "are locked by Claude"), ("out", "reached a limit", "reached a limit"),
       ("failed", "couldn't be switched to", "couldn't be switched to"),
       ("old", "has an old reading", "have old readings"), ("past", "is near or past {t}%", "are near or past {t}%"),
       ("left", "is left out", "are left out"))


def why_none(rows, threshold):
    """(the accounts in play are all near or past the threshold (under 10 points of room), why the others
    do not qualify, e.g. "2 need sign-in, 1 can't be read"). Left-out accounts are the user's choice: they
    never make the first false."""
    counts = Counter(row["why"] for row in rows if row["why"])
    parts = [f"{counts[key]} {(one if counts[key] == 1 else many).format(t=threshold)}"
             for key, one, many in WHY if counts[key]]
    return counts["past"] > 0 and set(counts) <= {"past", "left"}, ", ".join(parts) or "no other account is saved"


def _models_only(active, others, threshold):
    """The per-model limits ("Fable weekly") that alone leave the account in use and the `others` (those past the
    threshold) without room, or None when a limit every model shares is in the way too: "All terminal accounts are
    low" then says which limit, as the terminals still work for other models."""
    limiting = [(label, w) for label, used, w in active["values"] if used is not None and used >= threshold]
    if not limiting:
        return None
    for row in others:
        limiting += [(label, w) for label, used, w in row["values"] if used is not None and used > threshold - 10]
    if any(w.get("kind") != "model" for _, w in limiting):
        return None
    return " and ".join(sorted({label for label, _ in limiting}))


# Once every account is near or past the threshold, each is used up to the ceiling rather than idling
# there: the active account stays until it reaches it, and a target needs this much room below it.
DRAIN_CEILING, DRAIN_ROOM = 99, 2


def evaluate(accounts, settings, history, now):
    threshold = settings.get("cli_threshold", 95)
    failures = history.get("failures") if isinstance(history.get("failures"), dict) else {}
    rows = []
    for account in accounts:
        values = considered(account, settings, now)
        known = all(used is not None for _, used, _ in values)
        room = min(threshold - used for _, used, _ in values) if known else None
        weekly = values[1][2]
        age = reading_age(account, now)
        read_at = None if age is None else now - age
        ends, paused = number(account.get("login_ends_at")), number(account.get("paused_until"))
        failed = failures.get(account["id"])
        retry = number(failed.get("until")) if isinstance(failed, dict) else None
        skip, why, reread = None, None, False
        if account.get("active"):
            skip = "in use by your terminals"
        elif is_frozen(account) or account.get("excluded"):
            skip, why = "left out", "left"
        elif account.get("health") == "on_hold":
            skip, why = "on hold", "hold"            # Claude put it on hold: signing in again does not help
        elif account.get("health", "ok") != "ok":
            skip, why = "needs sign-in", "signin"
        elif blocked(account, settings):
            skip, why = LOCKED_TEXT, "blocked"
        elif account.get("out"):
            skip, why = "reached a limit", "out"     # a limit error in its sessions still stands (cli_watch)
        elif retry is not None and retry > now:
            skip, why = f"switch failed, next try in {duration(retry - now)}", "failed"
        # Checked after those two so it hides neither: the out / login fallback takes it with LOGIN_FALLBACK left.
        elif ends is not None and ends - now < LOGIN_TARGET:
            skip, why = f"login ends in {duration(ends - now)}" if ends > now else "login ended", "ending"
        # Reads that cannot refresh the reading now: a target is never waited on for them, but one whose
        # last read failed (not paused) is read again while none qualifies, unless its last good reading
        # already leaves too little room.
        elif paused is not None and paused > now:
            skip, why = f"reads paused for {duration(paused - now)}", "unread"
        elif account.get("read_failed"):
            skip, why, reread = "last read failed", "unread", not (known and room < 10)
        elif age is None:
            skip, why = "reading unavailable", "unread"
        elif not known:
            skip, why = "weekly reading unknown" if values[1][1] is None else "reading incomplete", "unread"
        elif room < 10:
            label, used, _ = max(values, key=lambda v: v[1])
            skip, why = f"{label} at {used:g}%", "past"
        elif age > 900:
            # Room at its last reading, too old to act on: read it again when it is needed.
            skip, why = f"reading {duration(age)} old", "old"
        room_left = known and all(used <= DRAIN_CEILING - DRAIN_ROOM for _, used, _ in values)
        # Fit for the out / login fallback by its last good reading alone, however it is read now.
        fit = ((skip is None or why in ("past", "old", "unread") or (why == "ending" and ends - now > LOGIN_FALLBACK))
               and age is not None and age <= FALLBACK_AGE)
        shared = [used for _, used, w in values if w.get("kind") != "model"] if known else []    # not one model's own
        rows.append({"id": account["id"], "label": account["label"], "skip": skip, "room": room,
                     # A refilled weekly window's next reset is unknown until the next reading.
                     "weekly_reset": None if refilled(weekly, now, read_at) else reset(weekly),
                     "account": account, "values": values, "why": why, "reread": reread,
                     # Skipped only for being past the threshold (not health, freshness or choice): drained when all are.
                     "drainable": (skip is None or why == "past") and room_left,
                     "usable": fit and room_left,
                     # Its week has room though a model's own limit there may be used up: other models work on it.
                     "spare": fit and known and all(used <= DRAIN_CEILING - DRAIN_ROOM for used in shared),
                     "shared": max(shared, default=None)})
    rows = ordered(rows, settings.get("cli_order", "soonest_reset"), settings.get("cli_manual_order", []))
    result = {"action": "none", "target": None, "kind": None, "reason": "", "hold": None, "refresh": [],
              "read_first": True, "exhausted": False, "all_low": False,
              "next_up": [{key: row[key] for key in ("id", "label", "skip", "room", "weekly_reset")} for row in rows]}
    active = next((row for row in rows if row["account"].get("active")), None)
    if not settings.get("cli_autoswitch", True) or active is None or is_frozen(active["account"]):
        return result
    values, account, name = active["values"], active["account"], active["label"]
    over = [(label, used, w) for label, used, w in values if used is not None and used >= threshold]
    # A limit filling fast can pass the threshold, or run out, before the next read is allowed: act on the
    # forecast for that moment rather than the last reading.
    ahead = account.get("forecast") or {}
    projected = [(label, max(used, ahead[w["id"]]) if used is not None and w.get("id") in ahead else used, w)
                 for label, used, w in values]
    soon = not over and [(label, used, w) for label, used, w in projected if used is not None and used >= threshold]
    if soon:
        over = soon
    peak = max((used for _, used, _ in projected if used is not None), default=0)
    on_hold = account.get("health") == "on_hold"        # Claude put it on hold: the terminals cannot use it
    locked = blocked(account, settings)
    out = on_hold or locked or account.get("out") or any(used is not None and used >= 100 for _, used, _ in values)
    ends = number(account.get("login_ends_at"))
    ending = ends is not None and ends - now < LOGIN_ACTIVE
    login = account.get("health") == "needs_signin" or ending
    # It works for no model: a limit every model shares is spent or locked, Claude put it on hold, or its login is gone.
    dead = login or on_hold or any((used is not None and used >= 100) or w.get("blocked")
                                   for _, used, w in values if w.get("kind") != "model")
    trigger = "login" if login else "out" if out else "threshold" if over else None
    if account.get("live_expired") and trigger not in ("out", "login"):
        # Its reading waits for the terminals' next message; a limit reached or a dead login still switches.
        result["hold"] = "Usage updates after your next message in Claude Code"
        return result
    eligible = [row for row in rows if row["skip"] is None]
    if trigger:
        five_reset = reset(values[0][2])
        if trigger == "threshold" and all(label == "5-hour" for label, _, _ in over) and five_reset is not None and 0 <= five_reset - now <= 600:
            result["hold"] = f"{name} refills in {duration(five_reset - now)} — staying"
            return result
        # Undo, or any switch the user made here: it stands against the threshold for a while, but not until the
        # limit is reached, where a session would stop before anything moved.
        chose = (history.get("manual_for") == account["id"]
                 and 0 <= now - (number(history.get("manual_at")) or -1e20) < MANUAL_HOLD)
        if trigger == "threshold" and chose and peak < DRAIN_CEILING:
            result["hold"] = f"You chose {name} — using it up to {DRAIN_CEILING}%"
            return result
        if account.get("health") == "needs_signin":
            lead = f"{name} needs sign-in"
        elif ending:
            lead = f"{name}'s login ends in {duration(ends - now)}" if ends > now else f"{name}'s login has ended"
        elif on_hold:
            lead = f"{name} is on hold"
        elif locked:
            lead = f"{name} is locked by Claude"
        elif out:
            lead = f"{name} reached a limit"
        elif soon:
            lead = f"{name} will pass {threshold}% of its {over[0][0]} limit before the next check"
        else:
            lead = f"{name} reached {threshold}% of its {over[0][0]} limit"
        kind = "refilled" if history.get("exhausted") else trigger
        if eligible:
            target = eligible[0]
            result.update(action="switch", target=target["id"], kind=kind,
                          reason=f"{lead}; {target['label']} is first in your eligible order.")
            return result
        all_past, summary = why_none(rows, threshold)
        # Read again what may have room: readings too old to act on, and accounts whose last read failed (a 503
        # or a timeout says nothing about the room, and the scheduler's back-off would wait 15 to 30 minutes).
        # The governor keeps each account's reads 180 s apart; reads paused after a 429 are not asked for.
        refresh = [row["id"] for row in rows if row["why"] == "old" or row["reread"]]
        if out or login:
            # Staying is no option: act on last good readings (reads may be failing) rather than wait for fresh ones.
            usable = [row for row in rows if row["usable"]]
            roomy = [row for row in usable if row["room"] >= 10]
            if roomy:
                target = ordered(roomy, "most_room")[0]
                result.update(action="switch", target=target["id"], kind=kind, read_first=False,
                              reason=f"{lead}; no account qualifies ({summary}), so {target['label']} is used: "
                                     f"it has the most room by its last reading.")
                return result
            # The most room again, not the user's order: a soon-resetting account can be the fullest.
            drain, result["refresh"] = ordered(usable, "most_room"), refresh
            spare = [row for row in rows if row["spare"]] if dead and not drain else []
            if spare:
                target = min(spare, key=lambda row: (row["shared"], row["label"].casefold(), row["id"]))
                result.update(action="switch", target=target["id"], kind=kind, read_first=False,
                              reason=f"{lead}; no account has room in every limit ({summary}), so {target['label']} is "
                                     f"used: its week has room, though a model's own limit there is used up.")
                return result
        else:
            result["refresh"] = refresh
            stale = [row for row in rows if row["why"] == "old"]
            if stale:
                result["hold"] = f"Checking {', '.join(row['label'] for row in stale[:3])} before switching"
                return result
            if peak < DRAIN_CEILING:
                because = f"Every account is near or past {threshold}%" if all_past else f"No account can take over ({summary})"
                result["hold"] = f"{because} — using {name} up to {DRAIN_CEILING}%"
                return result
            drain, lead = [row for row in rows if row["drainable"]], f"{name} reached {DRAIN_CEILING}%"
        if drain:
            target = drain[0]
            why = f"every account is near or past {threshold}%" if all_past else f"no account qualifies ({summary})"
            pick = ": it has the most room by its last reading" if out or login else ""
            result.update(action="switch", target=target["id"], kind=kind, read_first=not (out or login),
                          reason=f"{lead}; {why}, so {target['label']} is used up to {DRAIN_CEILING}%{pick}.")
            return result
        resets = []
        for row in rows:
            if row["why"] != "past":
                continue
            limiting = [w for _, used, w in row["values"] if used is not None and used > threshold - 10]
            if limiting and all(reset(w) is not None and reset(w) > now for w in limiting):
                # Every limiting window has to refill before this account has room.
                resets.append((max(reset(w) for w in limiting), row))
        first_reset, first = min(resets, key=lambda pair: pair[0]) if resets else (None, None)
        tail = f" {first['label']} refills first at {datetime.fromtimestamp(first_reset).strftime('%I:%M %p').lstrip('0')}." if first else ""
        only = _models_only(active, [row for row in rows if row["why"] == "past"], threshold) if all_past else None
        said = (f"All terminal accounts are low on their {only} limit; other models still have room." if only
                else "All terminal accounts are low." if all_past else f"No terminal account can take over: {summary}.")
        result.update(exhausted=True, all_low=all_past, reason=said + tail)
        return result
    if not settings.get("cli_early_switch", True):
        return result
    active_reset = active["weekly_reset"]
    # A week that ends within the hour of the active account's is no reason to move: the order counts those as equal,
    # and two accounts with one reset time would trade places each time Claude reports it a second apart.
    early = [row for row in eligible if row["weekly_reset"] is not None and active_reset is not None
             and 0 < row["weekly_reset"] - now <= 24 * HOUR and row["weekly_reset"] < active_reset - HOUR
             and row["values"][1][1] <= 80
             and not (row["id"] == history.get("left") and now - history.get("left_at", -1e20) < 1800)]
    if not early:
        return result
    # Not right after another automatic switch (a threshold switch's Undo would be gone within seconds), nor
    # against the user's own choice.
    last = history.get("last") if isinstance(history.get("last"), dict) else {}
    if now - max(number(last.get("at")) or -1e20, number(history.get("early_at")) or -1e20) < EARLY_GAP:
        result["hold"] = "The early switch waits: an automatic switch happened less than 2 hours ago"
        return result
    if now - (number(history.get("manual_at")) or -1e20) < MANUAL_HOLD:
        result["hold"] = "The early switch waits: your manual choice is held for 2 hours"
        return result
    target = min(early, key=lambda row: (row["weekly_reset"], row["label"].casefold(), row["id"]))
    result.update(action="switch", target=target["id"], kind="early",
                  reason=f"Using {target['label']}'s weekly allowance before it resets in {duration(target['weekly_reset'] - now)} ({100 - target['values'][1][1]:g}% left).")
    return result
