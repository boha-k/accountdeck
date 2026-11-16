"""When the terminal accounts run dry at the recent pace, and what refills them (the state's
`providers.claude_cli.runway`). Pure: no I/O or clock reads.

`forecast` takes the terminal accounts (as `cli_auto.inputs` gives them; the state's rows work too), each
account's weekly readings of the last BASIS (oldest first, as `Store.readings` gives them), the settings
and `now`:
- usable: an account with a weekly reading that is not left out, frozen, on hold, locked or in need of a
  sign-in and whose login has not ended; a login that ends later stops counting at that time;
- pace: weekly points used per hour over the last BASIS, summed over every account, usable or not (it says
  what the terminals burn, whichever account they were on). A drop is a refill, not negative use;
- the simulation: STEP by STEP for up to HORIZON, each step spends the pace from the room below
  DRAIN_CEILING in auto-switch's own order (`cli_order`) and a weekly reset gives an account its full room
  back, a week apart. Auto-switch's other limits on a target (a login ending within 12 hours, an old
  reading) move the room it reaches only by hours, so they are left out;
- confidence: "low" under MIN_BASIS of readings or MIN_POINTS used: an idle night says nothing about the pool.

A 5-hour window limits the rate only roughly and only while it lasts, and terminal accounts may report none,
so it is ignored whether a reading has one or not. So is a model's own weekly limit: the forecast follows only
the account's weekly limit (WEEKLY_ID).
"""
from __future__ import annotations

import math

from .cli_policy import DRAIN_CEILING, SAME_RESET, blocked, number, ordered
from .core import is_frozen

WEEKLY_ID = "seven_day"          # the weekly limit's window id in `readings` (usage._limit_shape)
BASIS = 6 * 3600                 # the pace comes from this much history
STEP, HORIZON, WEEK = 15 * 60, 7 * 86400, 7 * 86400
MIN_BASIS, MIN_POINTS = 3600, 5.0
MIN_GAIN = 5.0                   # a reset must give back at least this much to be the "next refill" (pool.MIN_GAIN)


def pace(readings_by_account, now):
    """(weekly points used per hour, points used, seconds of readings behind them) over the last BASIS.
    The seconds count up to `now`: a stretch without readings (the Mac asleep) is a stretch without use."""
    points, first = 0.0, None
    for readings in (readings_by_account or {}).values():
        last_used, last_reset = None, None
        for reading in readings or ():
            at, used, reset = number(reading.get("at")), number(reading.get("used")), number(reading.get("reset"))
            if at is None or used is None or not now - BASIS <= at <= now:
                continue
            first = at if first is None else min(first, at)
            if last_used is not None:
                if None not in (reset, last_reset) and abs(reset - last_reset) > SAME_RESET:
                    points += used         # a new window: all of it was used since the refill
                elif used > last_used:
                    points += used - last_used
            last_used, last_reset = used, reset
    basis = 0.0 if first is None else now - first
    return points / basis * 3600 if basis > 0 else 0.0, points, int(basis)


def _row(account, settings, now):
    """What the simulation tracks of a usable account, or None: its room below DRAIN_CEILING, its next weekly
    reset (`weekly_reset`, as cli_policy.ordered sorts by it) and when its login ends."""
    ends = number(account.get("login_ends_at"))
    if (is_frozen(account) or account.get("excluded") or account.get("health", "ok") != "ok"
            or (account.get("credential") or {}).get("state") == "reauth_required"
            or blocked(account, settings) or (ends is not None and ends <= now)):
        return None
    weekly = next((w for w in account.get("windows") or () if isinstance(w, dict) and w.get("kind") == "weekly"), None)
    if weekly is None:
        return None
    used, reset, at = number(weekly.get("used")), number(weekly.get("reset")), number(account.get("last_good_at"))
    if reset is not None and reset <= now:
        if at is None or at < reset:
            used = 0.0                 # passed since the reading: it counts as refilled, like cli_policy.refilled
        reset += WEEK * math.ceil((now - reset) / WEEK + 1e-9)
    elif weekly.get("started") is False:
        used = 0.0
    if used is None:
        return None
    return {"id": account["id"], "label": str(account.get("label") or account["id"]), "ends": ends,
            "room": min(DRAIN_CEILING, max(0.0, DRAIN_CEILING - used)), "weekly_reset": reset}


def _refills(row, at):
    """True when the row's login still stands at its reset `at`."""
    return at is not None and (row["ends"] is None or row["ends"] > at)


def _simulate(rows, rate, settings, now):
    """(when the pool runs dry, the row whose reset refills it first after that), or (None, None). Changes `rows`."""
    mode, manual = settings.get("cli_order", "soonest_reset"), settings.get("cli_manual_order") or ()
    per_step = rate * STEP / 3600
    for step in range(HORIZON // STEP):
        at = now + step * STEP
        for row in rows:
            if row["ends"] is not None and row["ends"] <= at:
                row["room"], row["weekly_reset"] = 0.0, None
            elif row["weekly_reset"] is not None and row["weekly_reset"] <= at:
                row["room"], row["weekly_reset"] = DRAIN_CEILING, row["weekly_reset"] + WEEK
        need = per_step
        for row in ordered([row for row in rows if row["room"] > 0], mode, manual):
            spent = min(row["room"], need)
            row["room"] -= spent
            need -= spent
            if need <= 0:
                break
        if need > 0:
            waiting = [row for row in rows if _refills(row, row["weekly_reset"])]
            first = min(waiting, key=lambda row: (row["weekly_reset"], row["label"].casefold(), row["id"]), default=None)
            return at + (per_step - need) / rate * 3600, first
    return None, None


def forecast(accounts, readings_by_account, settings, now):
    """The `providers.claude_cli.runway` block: `runs_out_at` is None when the pool lasts the whole horizon at
    this pace; `refill_after_out_at` / `refill_label` / `gap_s` are the first refill after the run-out and
    the wait for it (None without a run-out, or when no reset is known to come after it)."""
    settings = settings or {}
    rate, points, basis = pace(readings_by_account, now)
    rows = [row for row in (_row(account, settings, now) for account in accounts or ()) if row is not None]
    room = sum(row["room"] for row in rows)
    next_refill = min((row["weekly_reset"] for row in rows if DRAIN_CEILING - row["room"] >= MIN_GAIN
                       and row["weekly_reset"] is not None and _refills(row, row["weekly_reset"])), default=None)
    out, first = _simulate(rows, rate, settings, now) if rate > 0 and rows else (None, None)
    refill = None if first is None else first["weekly_reset"]
    return {"pace_per_hour": round(rate, 2), "room": round(float(room), 1), "accounts": len(rows),
            "runs_out_at": None if out is None else round(out), "next_refill_at": next_refill,
            "refill_after_out_at": refill, "refill_label": None if first is None else first["label"],
            "gap_s": None if refill is None else max(0, round(refill - out)),
            "confidence": "ok" if rows and basis >= MIN_BASIS and points >= MIN_POINTS else "low", "basis_s": basis}
