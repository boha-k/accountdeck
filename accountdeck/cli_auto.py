"""The terminal auto-switch executor: feeds cli_policy.evaluate with the saved terminal accounts and acts
on its decision. Reads the decision needs still go through the scheduler (and its request governor). Another
running job holds it, except a Codex or Claude Desktop job still waiting for sessions to finish: a needed
switch cancels that job (`_make_way`) and runs on the next tick. A login the mirror found should go into
Claude Code comes before any decision (`_repair`).

`tick` runs only on the engine's tick thread; `cli_history` is also written on `ad2-ops` by CLISwitch and
by `switch_settled` (the engine, after every terminal switch), so every read-modify-write of it goes
through `history()`."""
from contextlib import contextmanager
import copy
import threading

from . import cli_policy, contract, diagnostics
from .core import DeckError, is_frozen

_history_lock = threading.Lock()


@contextmanager
def history(engine):
    """The `cli_history` setting for one read-modify-write; saved on exit only when it changed."""
    with _history_lock:
        value = engine.store.setting("cli_history", {})
        before = copy.deepcopy(value)
        yield value
        if value != before:
            engine.store.set_setting("cli_history", value)


FORECAST_MARGIN, FORECAST_CAP = 60.0, 1800.0
FRESH_READ = 180.0        # a target is switched to on a reading this recent (the out / login fallback excepted)
# A failed automatic switch keeps its target out of auto-switch for 10 minutes, doubling with each failure
# in a row up to an hour, instead of a new attempt every tick; a switch to it that works, or a day without
# a failure, starts over.
BACKOFF, BACKOFF_CAP, FAILURES_KEPT = 600.0, 3600.0, 86400.0
# One that says nothing about its target (switch_cli.environment_failure) keeps every target eligible and holds
# the automatic switch as a whole for a minute, doubling to 15 minutes; half an hour without one starts over. A
# failed repair (`_repair`) has such a timer of its own.
ENV_BACKOFF, ENV_CAP, ENV_KEPT = 60.0, 900.0, 1800.0
ALERT_EVERY = 3600.0      # a failure that goes on is told again this often
APPS = {"claude": "Claude Desktop", "codex": "Codex", "claude_cli": "terminal", "antigravity": "Antigravity"}
JOBS = {"add": "sign-in", "finish-login": "sign-in", "cancel-login": "sign-in", "capture": "save", "recover": "recovery",
        "remove": "account removal", "open": "launch", "stop-consumer": "session stop", "import-bundle": "import",
        "use-reset": "limit reset"}
# The switches a Codex or Claude Desktop job still waiting for sessions to finish gives way to: the active account
# reached the threshold, is out or needs sign-in (or the terminals leave an all-low stretch). Never an early one.
NEEDED = frozenset({"threshold", "out", "login", "refilled"})
MADE_WAY = "Cancelled so your terminals could change accounts. Nothing was changed; start it again when you're ready."
REPAIRED_WAY = "Cancelled so your terminals could get their login back. Nothing was changed; start it again when you're ready."


def _forecast(engine, row, now):
    """Projected % used per window id of the active account when its next read can land (planned time or
    the governor's wait, whichever is later, plus a margin for the read itself)."""
    planned = engine.scheduler.planned(row["id"])
    wait = engine.scheduler.governor.delay(("claude_cli", row["identity"]), now, urgent=True)
    horizon = min(max((planned or now) - now, wait, 0.0) + FORECAST_MARGIN, FORECAST_CAP)
    # Only readings since the terminals came to this account, and none from before the Mac last slept: a rate from
    # when it was last in use, or from before a sleep in which nothing was used, says nothing about now.
    came = cli_policy.number(engine.store.setting("cli_history", {}).get("left_at"))
    since = max([now - cli_policy.FORECAST_SPAN - 600, *(at for at in (came, engine.woke_at) if at is not None and at <= now)])
    result = {}
    for window in (row["last_good_usage"] or {}).get("windows", []):
        if isinstance(window, dict) and isinstance(window.get("id"), str):
            value = cli_policy.forecast(engine.store.readings(row["id"], window["id"], since=since), now, horizon)
            if value is not None:
                result[window["id"]] = value
    return result


def inputs(engine):
    rows = []
    now = engine.clock()
    for row in engine.store.accounts():
        if row["provider"] != "claude_cli":
            continue
        active = row["identity"] == engine.active_identity.get("claude_cli")
        rows.append({"id": row["id"], "label": row["label"], "active": active,
                     "forecast": _forecast(engine, row, now) if active else {},
                     "owner": row["owner"],
                     "excluded": is_frozen(row) or row["flags"].get("excluded", False),
                     "health": row["flags"].get("health", "ok"),
                     "live_expired": row["flags"].get("live_expired", False),
                     # A limit error in its sessions' transcripts stands against it (cli_watch.LimitWatch), also
                     # after the terminals left it: then it is no target.
                     "out": engine.watch.out(row, now) if active else engine.watch.left_out(row, now),
                     # Reads that cannot renew the reading now: paused after a 429, or the last one failed (a
                     # good reading clears error_code).
                     "paused_until": row["paused_until"], "read_failed": row["error_code"] is not None,
                     "login_ends_at": row["flags"].get("login_ends_at"),     # unix seconds, once known
                     "last_good_at": row["last_good_at"],
                     "windows": (row["last_good_usage"] or {}).get("windows", [])})
    return rows


def _job(op):
    """What an operation is, as the hold and the notices name it: "Codex switch", "Claude Desktop quit"."""
    op = op or {}
    kind = str(op.get("kind") or "")
    what = " ".join(filter(None, (APPS.get(op.get("provider")), JOBS.get(kind, kind.replace("-", " ")))))
    return what or "current action"


def _hold(engine):
    """(why auto-switch cannot act right now, or None; True when that is only a Codex or Claude Desktop job
    still waiting for sessions to finish, which a needed switch cancels: OperationRunner.preempt). An open
    Add terminal account window holds nothing: its sign-in has its own folder and Keychain item
    (CLIAccounts.begin_enrollment). Nor does a Claude Desktop or Codex switch waiting for Recover: only an
    interrupted terminal switch (or a record that cannot be read) does. After an automatic limit reset it waits, two
    minutes at most, for the reading that shows the room (reset_auto.awaited)."""
    if not engine.owns_login:
        return "Another AccountDeck 2 is managing your sign-ins", False
    if engine.cli.adapter.readonly_reason:
        return engine.cli.adapter.readonly_reason, False
    if engine.recovering("claude_cli"):
        return engine.recovery_words()[0], False
    if engine.prefs.get("refresh_paused"):
        return "Usage checks are paused", False
    if not engine.identity_known.get("claude_cli"):
        return "Checking which account your terminals use", False
    failed = engine.store.setting("cli_history", {}).get("environment")
    if isinstance(failed, dict) and failed.get("until", 0) > engine.clock():     # no job gives way to a switch that cannot run
        return f"{str(failed.get('why')).rstrip('.')}. Next try in {cli_policy.duration(failed['until'] - engine.clock())}.", False
    from . import reset_auto          # it imports this module
    refilling = reset_auto.awaited(engine, engine.clock())
    if refilling is not None:         # a switch decided now would go by what the limit reset has just changed
        return f"Reading {refilling} again after its limit reset", False
    if engine.runner.busy:
        what = _job(engine.runner.pending())
        if engine.runner.yields():
            return f"Waiting for the {what} — a needed terminal switch cancels it", True
        return f"Waiting for the {what} to finish", False
    return None, False


def paused(engine):
    """Why auto-switch cannot act right now, or None: `state` shows it as the hold and `tick` returns early,
    except where a job that gives way to a needed switch is all that holds it (`_hold`)."""
    return _hold(engine)[0]


def decision(engine, settings=None):
    return cli_policy.evaluate(inputs(engine), settings or engine.prefs.public(),
                               engine.store.setting("cli_history", {}), engine.clock())


def _reads_first(result, last_good_at, now):
    """True when the decided target's reading is too old to switch on: `tick` asks for a read and decides
    again after it. The out / login fallback never waits, as its reads may be failing."""
    return result["read_first"] and (last_good_at is None or now - last_good_at > FRESH_READ)


def state(engine):
    """The state's `providers.claude_cli.autoswitch` block: the decision and the order under each mode
    (one pass over the store, evaluated four times). `paused` names what stops auto-switch now."""
    settings, rows = engine.prefs.public(), inputs(engine)
    history, now = engine.store.setting("cli_history", {}), engine.clock()
    result = cli_policy.evaluate(rows, settings, history, now)
    reason = paused(engine)
    hold = reason or result["hold"]
    target = next((row for row in rows if row["id"] == result["target"]), None)
    if hold is None and result["action"] == "switch" and target and _reads_first(result, target["last_good_at"], now):
        hold = f"Checking {target['label']} before switching"
    return {"enabled": settings["cli_autoswitch"], "threshold": settings["cli_threshold"],
            "order_mode": settings["cli_order"], "early_switch": settings["cli_early_switch"],
            "next_up": result["next_up"], "last": history.get("last"), "hold": hold, "paused": reason,
            "previews": {mode: cli_policy.evaluate(rows, {**settings, "cli_order": mode}, history, now)["next_up"]
                         for mode in ("soonest_reset", "most_room", "manual")}}


def _backed_off(entry, base, cap, now, why=None):
    """(the record of one more failure after `entry`, doubling from `base` to `cap`; whether it should be told):
    the first of a run, one ALERT_EVERY after the last one told, and one with another `why`."""
    count = entry.get("count", 0) + 1
    told = entry.get("alerted", entry.get("at"))
    tell = told is None or now - told >= ALERT_EVERY or entry.get("why") != why
    return {"at": now, "count": count, "until": now + min(base * 2 ** min(count - 1, 8), cap),
            "alerted": now if tell else told, "why": why}, tell


def switch_settled(engine, key, *, ok, automatic, now, environment=None, repair=False):
    """Records how a terminal switch to `key` ended. One that worked clears the account's failures and the timers
    below. A failed automatic one backs the account off (BACKOFF, doubling to BACKOFF_CAP), unless it says nothing
    about the account (`environment`, switch_cli.environment_failure): that holds the automatic switch as a whole
    (`_hold`) and backs off nobody. A failed repair only delays the next repair. A manual switch leaves no trace:
    whoever asked sees how it ended. True when the failure repeats an earlier one too soon to alert again."""
    with history(engine) as past:
        before = past.get("failures") if isinstance(past.get("failures"), dict) else {}
        failures = {k: v for k, v in before.items() if isinstance(v, dict) and now - v.get("at", 0) < FAILURES_KEPT}
        record, tell = None, True
        if ok:
            failures.pop(key, None)
            past.pop("environment", None)
            past.pop("repair", None)
        elif repair or (automatic and environment):
            name = "repair" if repair else "environment"
            entry = past.get(name) if isinstance(past.get(name), dict) else {}
            record, tell = _backed_off(entry if now - entry.get("at", 0) < ENV_KEPT else {}, ENV_BACKOFF, ENV_CAP, now, environment)
            past[name] = record
        elif automatic:
            record, tell = _backed_off(failures.get(key, {}), BACKOFF, BACKOFF_CAP, now)
            failures[key] = record
        if failures or "failures" in past:
            past["failures"] = failures
    if record:
        diagnostics.log("cli.switch_backoff", acct=diagnostics.account_ref(key), count=record["count"],
                        seconds=round(record["until"] - now), environment=bool(environment), repair=repair)
    return not tell


def _make_way(engine, kind, why, message=MADE_WAY):
    """Cancels the Codex or Claude Desktop job that only waits for sessions to finish, so what the terminals need
    runs on the next tick. It ends a poll later, and only then is the owner told (`made_way`)."""
    pending = engine.runner.pending()
    if pending is None or not engine.runner.preempt(message, pending["id"], why=why):
        return       # it left its wait meanwhile: it finishes first, as any other job does
    diagnostics.log("cli.made_way", kind=kind, provider=pending.get("provider"), job=pending.get("kind"))


def made_way(engine, op):
    """A job `_make_way` cancelled has ended so: the owner starts it again (`preempted_for` says why it had to go).
    Its alert names the job's account only for an app whose accounts are rows of `accounts`: an Antigravity account
    id never enters a table older builds read (an Antigravity switch waiting for agy may be the job cancelled)."""
    job = _job({"provider": op.provider, "kind": op.kind}) + (f" to {op.label}" if op.kind == "switch" and op.label else "")
    engine.notice(op.preempted.replace("Cancelled", f"The {job} was cancelled", 1), "warning")
    if engine.prefs.get("alerts_enabled"):
        account = op.account if op.provider in contract.ACCOUNT_PROVIDERS else None
        engine.store.add_alert({"kind": "switch_result", "level": "warning", "account": account,
                                "window": None, "title": f"{job[:1].upper()}{job[1:]} cancelled",
                                "message": f"{op.preempted_for} Nothing was changed; start it again when you're ready.",
                                "at": engine.clock()})


def _repair(engine):
    """The login saved for the account in use goes into Claude Code as soon as the mirror asks for it
    (`CLIAccounts.repair_wanted`) and no interrupted terminal switch's record is open, before any decision: a
    sign-in takes effect at once, and terminals that lost their login need not leave a working account. Usage
    checks being paused holds nothing here. A job waiting for sessions gives way only when the terminals' own
    login does not work. True when it was submitted."""
    service = engine.cli
    wanted = service.repair_wanted
    if wanted is None or not engine.owns_login or service.adapter.readonly_reason:
        return False
    # Not while an interrupted switch's record is open (switch_cli.auto_recover settles it, or the owner): the repair
    # could write over what that switch wrote, which a rollback would then put a renewed-since login back over.
    if engine.recovering("claude_cli"):
        return False
    failed = engine.store.setting("cli_history", {}).get("repair")
    if isinstance(failed, dict) and failed.get("until", 0) > engine.clock():
        return False
    key, why = wanted
    try:
        label = service.row(key)["label"]
    except DeckError:          # removed since the mirror asked
        return False
    if engine.runner.busy:
        if why != "older" and engine.runner.yields():
            _make_way(engine, "repair", f"Your terminals could no longer use {label}, and AccountDeck had a newer login for it.",
                      REPAIRED_WAY)
        return False
    from .switch_cli import CLISwitch
    try:
        engine.submit(CLISwitch(engine, key, kind="repair", reason=f"Your terminals now use the newest login saved for {label}."))
    except DeckError as exc:
        diagnostics.log("cli.deferred", code=exc.code)
        return False
    return True


def tick(engine):
    if _repair(engine):
        return
    reason, yields = _hold(engine)
    if reason is not None and not yields:
        return
    result = decision(engine)
    with history(engine) as past:
        entered = result["exhausted"] and not past.get("exhausted")
        if entered:
            past["exhausted"] = True
        elif not result["exhausted"] and result["action"] == "none" and not result["hold"] and past.get("exhausted"):
            past["exhausted"] = False
    if entered:
        engine.notice(result["reason"], "warning")
        if engine.prefs.get("alerts_enabled"):
            # "Low" only when the others really are near or past the threshold; otherwise the reason says why none qualifies.
            title = "All terminal accounts are low" if result["all_low"] else "No terminal account can take over"
            engine.store.add_alert({"kind": "limit_reached", "level": "warning", "account": None, "window": None,
                                    "title": title, "message": result["reason"], "at": engine.clock()})
        diagnostics.log("cli.all_low", all_low=result["all_low"])
    for key in result["refresh"]:          # candidates too old to switch on, or whose last read failed
        engine.scheduler.request(key, engine.clock())
    if result["action"] != "switch":
        return
    try:
        target = engine.cli.row(result["target"])
    except DeckError:          # removed since the decision was made; the next tick decides again
        return
    if _reads_first(result, target["last_good_at"], engine.clock()):
        engine.scheduler.request(target["id"], engine.clock())
        return     # re-evaluate after the governed read, including every changed window
    if yields:
        if result["kind"] in NEEDED:
            _make_way(engine, result["kind"], f"Your terminals had to change accounts: {result['reason']}")
        return     # the switch is submitted once the runner is free (an early one waits for the job)
    from .switch_cli import CLISwitch
    try:
        engine.submit(CLISwitch(engine, target["id"], kind=result["kind"], reason=result["reason"]))
        diagnostics.log("cli.decision", kind=result["kind"], acct=diagnostics.account_ref(target["id"]),
                        read_first=result["read_first"])
    except DeckError as exc:
        diagnostics.log("cli.deferred", code=exc.code)
