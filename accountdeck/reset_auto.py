"""Automatic limit resets (the setting `auto_reset`, off by default): when every terminal account auto-switch could
use is out, AccountDeck uses one limit reset on one account, and the next one only once the accounts are all out again.

This module decides and waits. The use itself is resets.py's: `Resets.use_automatically` starts the same
ResetOperation a click on Use Limit Reset starts (the last look at Anthropic's fresh answer, the request key saved
before the request, one claim, the readings asked for afterwards). Nothing here talks to a provider, and `tick` runs
on the engine's tick thread only, just before auto-switch.

When (`due`, pure). Only Claude Code accounts count, and only with auto-switch (`cli_autoswitch`) and `claude_resets`
on. The pool is the account in use plus every account auto-switch could move the terminals to: not left out, not
frozen, signed in, not on hold, not locked by Claude, its login not about to end. An account is out when its weekly
limit or Fable's weekly limit is at the bar, by a reading at most 15 minutes old, and that limit's reset is still
ahead. The bar is 97 %; for the account in use it is the user's switch point when that is higher (`bar`). A reset
that passed since the reading counts as room, and a limit that refills by itself within REFILL_SOON (10 minutes) is
waited for. An account whose reading cannot say is "not known": nothing is used, and it is read again. That is a
reading that is missing, too old or without a percentage or a reset time, and one that shows the account out but was
taken before a limit reset AccountDeck used on that account (the user's own included) or under 10 minutes after it.
The 5-hour limit, another model's limit and a meter of a new kind never make an account out, but one that is at the
bar too must be refilled by the same reset (`fitting`).

Which (`choose`). A reset fits when it is one `reset-plan` would offer now (`Resets.usable_rows`: usable, nothing
blocking) and refills every limit that holds the account back. The account in use comes first: the terminals then
stay where they are. Otherwise the reset that refills the least beyond what is out (a Fable reset before a weekly one
when only Fable is out), then the one that expires first, then auto-switch's own order; auto-switch moves the
terminals there once a reading shows the room.

One at a time. Nothing is used while another job runs (it never makes one give way), while auto-switch itself is
held, while any account's request key is open, before a reading of the account shows how the last automatic reset
went, within GAP (1 hour) of it, or within RETRY (10 minutes, doubling to an hour) of a try that used nothing. A
reading taken REFILL_GRACE (10 minutes) or more after a reset that still shows its limit at the bar stops automatic
resets, with an alert, until the user turns the setting off and on.

What it remembers is the `auto` part of resets.py's memory (`Resets.auto`, `Resets.set_auto`): `last` (the account,
when, which limits, `open` until a reading settled it, `unsure` while it is not known whether it was used), `failed`
(tries in a row that used nothing) and `stopped`. `last` is written before the request is sent, as the request key
is, so a failure after it can never lead to a second reset.
"""
import math

from . import cli_auto, cli_policy, diagnostics, rules
from .core import DeckError, is_frozen

# An account is out with this much of a limit used, or more. Also the most auto-switch moves the terminals to
# (cli_policy.DRAIN_CEILING - DRAIN_ROOM): from here on no switch helps.
BAR = 97
FRESH = 900.0             # a reading older than this says nothing of now (auto-switch's own age for a target)
GAP = 3600.0              # at least this long between two automatic resets
REFILL_GRACE = 600.0      # a reading this long after a reset that still shows its limit out: it did not refill it
REFILL_SOON = 600.0       # a limit that refills by itself this soon is waited for (as auto-switch waits for a 5-hour one)
RETRY, RETRY_CAP = 600.0, 3600.0     # after a try that used nothing
READING_WAIT = 120.0      # auto-switch waits this long for the reading after an automatic reset
WEEKLY, FABLE = "seven_day", "seven_day_overage_included"     # Claude's names of the two limits in a grant's `clears`
NAMES = {WEEKLY: "weekly", FABLE: "Fable weekly", "five_hour": "5-hour"}
STOPPED = ("AccountDeck stopped using limit resets by itself: the last one did not refill its account, or what "
           "AccountDeck remembered of it could not be read. Turn this setting off and on to start again.")


def bar(settings, active=True):
    """The percentage from which an account counts as out. The account in use: never below the user's own switch
    point, where auto-switch first looks for another account. Any other: BAR whatever the switch point, as
    auto-switch moves the terminals to no account above it (an account between BAR and a higher switch point would
    otherwise be neither switched to nor reset)."""
    threshold = cli_policy.number(settings.get("cli_threshold")) if active else None
    return max(BAR, threshold if threshold is not None else BAR)


def limit_key(window):
    """Claude's name, in a grant's `clears`, of the limit a window shows."""
    return FABLE if rules.is_fable(window) else window.get("id")


def out_limits(windows, read_at, pinned, level, now):
    """The limits holding the terminals that a reading taken at `read_at` shows at `level` or more: {Claude's name of
    the limit: when it resets by itself}, empty when all have room; None when the reading cannot say (no weekly limit
    in it, a percentage missing, or a limit that is out with no reset time ahead). A limit whose reset passed since
    the reading has room."""
    windows = [w for w in windows or () if isinstance(w, dict)]
    if read_at is None or not any(w.get("kind") == "weekly" for w in windows):
        return None
    found = {}
    for window in windows:
        if not rules.holds_terminals(window, pinned) or cli_policy.refilled(window, now, read_at):
            continue
        if window.get("kind") == "session" and window.get("started") is False:
            continue                  # a 5-hour window that has not started is unused
        used, ends = cli_policy.number(window.get("used")), cli_policy.reset(window)
        if used is None:
            return None
        if used >= level:
            if ends is None or ends <= now:
                return None
            found[limit_key(window)] = max(ends, found.get(limit_key(window), ends))
    return found


def in_pool(account, settings, now):
    """Whether the terminals could run on this account as far as its sign-in goes (an account row of
    cli_auto.inputs). The account in use counts even when the user left it out: the terminals are on it."""
    active = bool(account.get("active"))
    if not active and (is_frozen(account) or account.get("excluded")):
        return False
    if account.get("health", "ok") != "ok" or cli_policy.blocked(account, settings):
        return False
    ends = cli_policy.number(account.get("login_ends_at"))
    return ends is None or ends - now >= (cli_policy.LOGIN_ACTIVE if active else cli_policy.LOGIN_FALLBACK)


def due(accounts, settings, now, used=None):
    """Whether a limit reset is called for, from the rows auto-switch decides on (cli_auto.inputs). Pure.
    {"due", "out": {account id: [limits out]} when due, "refresh": [ids to read again], "why"}: `why` is "room" when
    an account has room, "soon" when one refills by itself within REFILL_SOON, "unknown" when a reading cannot say,
    "nobody" without an account to judge. `used` (Resets.used_at): {account id: when a limit reset was last used on
    it through AccountDeck}; a reading that shows the account out cannot say when it was taken before that reset, or
    less than REFILL_GRACE after it (usage may lag behind a reset)."""
    result = {"due": False, "out": {}, "refresh": [], "why": "nobody"}
    active = next((account for account in accounts if account.get("active")), None)
    if active is None or is_frozen(active):
        return result
    pinned, out, unknown, soon = settings.get("pinned_model"), {}, [], False
    for account in accounts:
        if not in_pool(account, settings, now):
            continue
        age, limits = cli_policy.reading_age(account, now), None
        if age is not None and age <= FRESH:
            limits = out_limits(account.get("windows"), now - age, pinned, bar(settings, account.get("active")), now)
        if limits is None:
            unknown.append(account)
        elif WEEKLY in limits or FABLE in limits:
            if now - age - (used or {}).get(account["id"], -math.inf) < REFILL_GRACE:
                unknown.append(account)      # read before a reset used on it, or so soon after that usage may lag
                continue
            out[account["id"]] = list(limits)
            # What makes it out refills by itself within minutes: the account then has room without a reset.
            soon = soon or max(limits[key] for key in (WEEKLY, FABLE) if key in limits) - now <= REFILL_SOON
        else:
            return {**result, "why": "room"}
    if not out:
        return result
    if unknown:
        # Everything known is out: read the others again rather than wait for their turn (the governor spaces the
        # reads; reads paused after a 429 are not asked for).
        refresh = [account["id"] for account in unknown
                   if not (cli_policy.number(account.get("paused_until")) or 0) > now]
        return {**result, "refresh": refresh, "why": "unknown"}
    if soon:
        return {**result, "why": "soon"}
    return {**result, "due": True, "out": out, "why": None}


def fitting(rows, limits):
    """The row to use for the limits that are out, among an account's usable rows (each with its `_grant`), or None:
    one that refills every one of them; a Fable reset first when only Fable is out, then the one that refills the
    least beyond them, then the one that expires first. A row that does not say what it refills never fits."""
    wanted = set(limits)

    def rank(row):
        grant = row["_grant"]
        return (wanted == {FABLE} and grant["kind"] != "fable", len(set(grant["clears"]) - wanted),
                row["expires"] is None, row["expires"] or 0, row["key"])
    fits = [row for row in rows if wanted and wanted <= set(row["_grant"]["clears"])]
    return min(fits, key=rank, default=None)


def choose(engine, found, accounts, settings, now, last_failed=None):
    """(the account's row of the store, the reset row to use, the limits it is used for) or None when no account
    that is out holds a reset that fits. The account in use first; the one whose try failed last comes after the
    others, so that one account's trouble does not keep the rest waiting."""
    order = [row["id"] for row in cli_policy.evaluate(accounts, settings, engine.store.setting("cli_history", {}),
                                                      now)["next_up"]]
    picks = []
    for account in accounts:
        limits = found["out"].get(account["id"])
        if not limits or is_frozen(account) or account.get("excluded"):
            continue                  # an account the user left out is never reset, also while it is in use
        row = engine.store.account(account["id"])
        chosen = fitting(engine.resets.usable_rows(row, now), limits) if row is not None else None
        if chosen is None:
            continue
        grant = chosen["_grant"]
        picks.append(((account["id"] == last_failed, not account.get("active"),
                       set(limits) == {FABLE} and grant["kind"] != "fable", len(set(grant["clears"]) - set(limits)),
                       chosen["expires"] is None, chosen["expires"] or 0,
                       order.index(account["id"]) if account["id"] in order else len(order)), row, chosen, limits))
    return min(picks, key=lambda pick: pick[0])[1:] if picks else None


def _names(limits):
    known = [NAMES[key] for key in limits if key in NAMES]
    return (" and ".join(known) + " limit" + ("s" if len(known) > 1 else "")) if known else "limit"


def _label(row):
    return row["label"] or row["email"] or "an account"


def _alert(engine, kind, level, key, title, message, now):
    if engine.prefs.get("alerts_enabled"):
        engine.store.add_alert({"kind": kind, "level": level, "account": key, "window": None, "title": title,
                                "message": message, "at": now})


def _retry_in(failed):
    return min(RETRY * 2 ** min(max(int(failed.get("count") or 1), 1) - 1, 8), RETRY_CAP)


def _stop(engine, row, left, now):
    """The last automatic reset did not do what it was used for: no more by itself until the user says so."""
    label = _label(row)
    text = (f"The limit reset AccountDeck used on {label} did not refill its {_names(left)}. AccountDeck uses no "
            "more limit resets by itself until you turn “Use a limit reset when every terminal account is out” off "
            "and on in Settings.")
    engine.resets.set_auto(stopped={"at": now}, update={"open": False, "unsure": False})
    engine.store.event("use-reset", f"Automatic limit resets stopped: the one used on {label} did not refill its {_names(left)}.")
    engine.notice(text, "warning")
    _alert(engine, "limit_reached", "warning", row["id"], "Automatic limit resets stopped", text, now)
    diagnostics.log("resets.auto_stopped", "warning", acct=diagnostics.account_ref(row["id"]), limits=",".join(left))
    engine.bump()


def _settled(engine, last, now):
    """Whether a reading has shown how the last automatic reset went, so that another may follow. A reading taken
    after it that shows its limits under the bar settles it; one taken REFILL_GRACE or more after it that still shows
    one at the bar stops automatic resets (`_stop`). Until then it waits: usage may lag behind a reset."""
    resets, key = engine.resets, last["account"]
    row = engine.store.account(key)
    if row is None:                    # removed since: nothing is left to wait for
        resets.set_auto(update={"open": False, "unsure": False})
        return True
    if last.get("unsure"):
        if resets.open_request(row):
            return False               # resets.py settles the request key first (`Resets._auto_verdict`)
        # Marked as about to be sent, and no request key says more (the service stopped in between): it counts as
        # used, so only a reading that shows the refill lets another one follow.
        resets.set_auto(update={"unsure": False})
    read_at = row["last_good_at"]
    if read_at is None or read_at <= last["at"]:
        if now - last["at"] >= READING_WAIT:
            engine.scheduler.request(key, now)
        return False
    settings = engine.prefs.public()
    still = out_limits((row["last_good_usage"] or {}).get("windows"), read_at, settings.get("pinned_model"),
                       bar(settings, row["identity"] == engine.active_identity.get("claude_cli")), now)
    if still is None:
        return False
    left = [limit for limit in last["limits"] if limit in still]
    if not left:
        resets.set_auto(update={"open": False})
        diagnostics.log("resets.auto_settled", acct=diagnostics.account_ref(key))
        return True
    if read_at - last["at"] < REFILL_GRACE:
        if now - last["at"] >= REFILL_GRACE:
            engine.scheduler.request(key, now)
        return False
    _stop(engine, row, left, now)
    return False


def awaited(engine, now):
    """The label of the account auto-switch waits for, or None: an automatic reset was just used on it and no reading
    since shows the room, so a switch decided now would go by what the reset has just changed (READING_WAIT at most)."""
    last = engine.resets.auto().get("last") or {}
    if not last.get("open") or last.get("unsure") or not 0 <= now - last["at"] < READING_WAIT:
        return None
    row = engine.store.account(last["account"])
    if row is None or (row["last_good_at"] is not None and row["last_good_at"] > last["at"]):
        return None
    return _label(row)


def enabled(prefs):
    """The three settings an automatic reset needs."""
    return bool(prefs.get("auto_reset") and prefs.get("cli_autoswitch") and prefs.get("claude_resets"))


def tick(engine):
    prefs, resets = engine.prefs, engine.resets
    if not enabled(prefs):
        return
    now = engine.clock()
    auto = resets.auto()
    if auto.get("stopped"):
        return
    last = auto.get("last") or {}
    if last.get("open") and not _settled(engine, last, now):
        return
    # Auto-switch's own reasons to wait (another AccountDeck, read-only, an interrupted switch, paused checks, an
    # unknown account in use, a failure about this Mac) and any job that runs: nothing gives way to a limit reset.
    if cli_auto.paused(engine) is not None or resets.any_open_request():
        return
    if last and now - last["at"] < GAP:
        return
    failed = auto.get("failed") or {}
    if failed and now - failed["at"] < _retry_in(failed):
        return
    accounts, settings = cli_auto.inputs(engine), prefs.public()
    found = due(accounts, settings, now, resets.used_at())
    for key in found["refresh"]:
        engine.scheduler.request(key, now)
    if not found["due"]:
        if failed and found["why"] == "room":
            resets.set_auto(failed=None)             # the accounts have room again: the next need starts afresh
        return
    # Two tries in a row that used nothing: the account they were made on now comes after the others.
    picked = choose(engine, found, accounts, settings, now,
                    last_failed=failed.get("account") if int(failed.get("count") or 0) >= 2 else None)
    if picked is None:
        return                         # auto-switch's own alert says that every account is low
    row, chosen, limits = picked
    key = row["id"]

    def confirm():
        """Asked on `ad2-ops` right before the request (the last look took a moment): still called for, still
        switched on, the account still not left out? Then `last` goes to disk first, as the request key does."""
        rows = cli_auto.inputs(engine)
        again = due(rows, engine.prefs.public(), engine.clock(), resets.used_at())
        left_out = any(account["id"] == key and account.get("excluded") for account in rows)
        if not (enabled(engine.prefs) and again["due"] and not left_out
                and set(limits) <= set(again["out"].get(key, ()))):
            automatic["dropped"] = True
            return False
        resets.set_auto(last={"account": key, "at": engine.clock(), "limits": list(limits), "open": True, "unsure": True})
        automatic["marked"] = True
        return True

    automatic = {"confirm": confirm, "why": f"every terminal account was at {BAR}% or more", "limits": list(limits),
                 "marked": False, "dropped": False}
    try:
        resets.use_automatically(row, chosen, automatic)
    except DeckError as exc:
        diagnostics.log("resets.auto_deferred", code=exc.code)
        return
    diagnostics.log("resets.auto", acct=diagnostics.account_ref(key), kind=chosen["_grant"]["kind"],
                    limits=",".join(limits), in_use=row["identity"] == engine.active_identity.get("claude_cli"))


def finished(engine, op, report, now):
    """An operation ended (engine._record_outcome, on `ad2-ops`). False unless it was an automatic use; then True: it
    keeps what the next tick needs and tells the user itself (an event, an alert that becomes a notification, and
    the report the dashboard shows anyway). Never raises."""
    automatic = getattr(op, "automatic", None)
    if op.kind != "use-reset" or automatic is None:
        return False
    try:
        _record(engine, op, report, automatic, now)
    except Exception as exc:
        diagnostics.log_exception("resets.auto_record_failed", exc, acct=diagnostics.account_ref(op.account))
    return True


def _record(engine, op, report, automatic, now):
    resets, key, label = engine.resets, op.account, op.label or "an account"
    text = diagnostics.redact_text(getattr(op, "event_text", None) or report["summary"] or "")
    if report["outcome"] == "success":
        resets.set_auto(last={"account": key, "at": now, "limits": list(automatic["limits"]), "open": True,
                              "unsure": False}, failed=None)
        engine.store.event("use-reset", text)
        _alert(engine, "reset_confirmed", "good", key, f"Limit reset used on {label}", text, now)
        return
    if getattr(op, "unsure", False):
        # Not known whether it was used: `last` stays as marked, and the open request key holds everything back
        # until a reading settles it.
        engine.store.event("use-reset", text)
        _alert(engine, "limit_reached", "warning", key, f"Limit reset on {label}: outcome not known",
               f"AccountDeck tried a limit reset on {label} by itself, and it is not known yet whether it was used. "
               "AccountDeck is reading the account again and uses no other limit reset until that is known.", now)
        return
    # Nothing was used (a cancel too): the next try waits, longer each time, and the other accounts come first.
    failed = resets.auto().get("failed") or {}
    count = int(failed.get("count") or 0) + 1
    changes = {"failed": {"at": now, "count": count, "account": key}}
    if automatic["marked"]:
        changes["last"] = None         # it was marked as about to be sent, and was not
    resets.set_auto(**changes)
    diagnostics.log("resets.auto_unused", acct=diagnostics.account_ref(key), count=count,
                    code=(report.get("error") or {}).get("code"), outcome=report["outcome"])
    # Told once per run of tries, and not at all when the need went away during the last look or the user cancelled.
    if count == 1 and report["outcome"] == "failed" and not automatic["dropped"]:
        words = text or "No limit reset was used."
        minutes = round(_retry_in(changes["failed"]) / 60)
        engine.store.event("use-reset", f"AccountDeck tried a limit reset by itself. {words}")
        _alert(engine, "limit_reached", "warning", key, "A limit reset could not be used automatically",
               f"Every terminal account is out, and AccountDeck tried a limit reset on {label}. {words} "
               f"It tries again in about {minutes} minutes.", now)


def rearm(engine):
    """The user turned the setting off or on: a stop and the wait after failed tries end. What is still open stays."""
    engine.resets.set_auto(stopped=None, failed=None)


def state(engine):
    """The state's `providers.claude_cli.auto_reset` block: whether it is on, why it stopped (words, or None), and
    the last automatic reset ({"account", "label", "at"}, or None)."""
    auto = engine.resets.auto()
    last = auto.get("last") or {}
    row = engine.store.account(last["account"]) if last.get("account") and not last.get("unsure") else None
    return {"enabled": bool(engine.prefs.get("auto_reset")), "stopped": STOPPED if auto.get("stopped") else None,
            "last": {"account": row["id"], "label": _label(row), "at": last["at"]} if row is not None else None}
