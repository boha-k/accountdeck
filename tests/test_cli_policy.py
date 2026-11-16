"""The terminal auto-switch policy (cli_policy.evaluate): the decisions that move every open terminal to
another account. Pure function, so these tests feed it account rows directly."""
import copy
import datetime as dt
import itertools
import json
import time
import types
import unittest

from tests.helpers import SyntheticTestCase, new_uuid, terminal_login

from accountdeck import cli_auto, cli_policy, cli_watch
from accountdeck.cli_watch import LimitWatch
from accountdeck.usage import claude_usage

NOW = 1_800_000_000.0
RESET = "2099-01-01T00:00:00+00:00"
HOUR, DAY = 3600.0, 86400.0
SETTINGS = {"cli_autoswitch": True, "cli_threshold": 95, "cli_order": "soonest_reset", "cli_manual_order": [],
            "cli_early_switch": False, "pinned_model": ""}


def account(key, five, weekly, *, active=False, five_reset=3 * HOUR, weekly_reset=3 * DAY, age=60, **extra):
    windows = [{"id": "five_hour", "kind": "session", "used": five, "reset": NOW + five_reset},
               {"id": "seven_day", "kind": "weekly", "used": weekly, "reset": NOW + weekly_reset}]
    return {"id": key, "label": key.title(), "active": active, "owner": "v2", "excluded": False, "health": "ok",
            "live_expired": False, "last_good_at": NOW - age, "forecast": {}, "windows": windows, **extra}


def decide(accounts, *, history=None, **settings):
    return cli_policy.evaluate(accounts, {**SETTINGS, **settings}, history or {}, NOW)


def locked(key, **extra):
    """An account whose weekly window is blocked, with every point of room otherwise."""
    row = account(key, 0, 0, **extra)
    row["windows"][1]["blocked"] = "locked"
    return row


def ultra(key, weekly, **extra):
    """A verified Ultra account: no 5-hour window."""
    row = account(key, 0, weekly, **extra)
    row["windows"] = row["windows"][1:]
    return row


def fable(key, weekly, own, **extra):
    """An Ultra account that also reports Fable's own weekly limit, as Claude names it today."""
    row = ultra(key, weekly, **extra)
    row["windows"].append({"id": "seven_day_fable", "kind": "model", "model": "Fable", "label": "Fable 5.1 weekly",
                           "used": own, "reset": NOW + 3 * DAY})
    return row


def meter(kind, percent=100.0, scope=None, **row):
    return {"kind": kind, "percent": percent, "resets_at": RESET, "scope": scope, **row}


def answered(key, *meters, top=None, **extra):
    """An account whose windows are the reading of a usage answer as Claude sends it today, with further limits[]
    rows and top-level keys."""
    data = {"five_hour": None, "seven_day": {"utilization": 10.0, "resets_at": RESET},
            "limits": [meter("weekly_all", 10.0), *meters], **(top or {})}
    return {**account(key, 0, 0, **extra), "windows": claude_usage(data, now=NOW)["windows"]}


class _Settings:
    """Just enough of the store for cli_auto.history()."""
    def __init__(self):
        self.values = {}

    def setting(self, key, default=None):
        return copy.deepcopy(self.values.get(key, default))

    def set_setting(self, key, value):
        self.values[key] = copy.deepcopy(value)


class TerminalPolicyTests(unittest.TestCase):
    def test_threshold_switches_to_the_first_eligible_account_in_order(self):
        rows = [account("alpha", 96, 40, active=True),
                account("later", 10, 20, weekly_reset=4 * DAY),
                account("sooner", 30, 50, weekly_reset=1 * DAY),
                account("left_out", 0, 0, weekly_reset=HOUR, excluded=True),
                account("signin", 0, 0, weekly_reset=HOUR, health="needs_signin"),
                account("other_mac", 0, 0, weekly_reset=HOUR, owner="on_pc"),
                account("stale", 0, 0, weekly_reset=HOUR, age=1200),
                account("near", 87, 10, weekly_reset=HOUR),         # 8 points below the threshold: too little room
                locked("locked", weekly_reset=HOUR),
                account("ending", 0, 0, weekly_reset=HOUR, login_ends_at=NOW + 5 * HOUR)]
        result = decide(rows)
        self.assertEqual((result["action"], result["target"], result["kind"]), ("switch", "sooner", "threshold"))
        skipped = {row["id"]: row["skip"] for row in result["next_up"]}
        self.assertEqual(skipped["left_out"], "left out")
        self.assertEqual(skipped["other_mac"], "left out")
        self.assertEqual(skipped["signin"], "needs sign-in")
        self.assertIn("min old", skipped["stale"])
        self.assertEqual(skipped["near"], "5-hour at 87%")
        self.assertEqual((skipped["locked"], skipped["ending"]), ("Locked by Claude", "login ends in 5h 0m"))
        # A login that ends within the hour is left like one that needs sign-in.
        ending = decide([account("alpha", 10, 10, active=True, login_ends_at=NOW + 1800), rows[2]])
        self.assertEqual((ending["action"], ending["target"], ending["kind"]), ("switch", "sooner", "login"))
        self.assertEqual(decide(rows, cli_order="most_room")["target"], "later")
        self.assertEqual(decide(rows, cli_autoswitch=False)["action"], "none")
        # Only an old reading in the way: read it again rather than call every account low.
        waiting = decide([rows[0], rows[6], rows[7]])
        self.assertEqual((waiting["action"], waiting["refresh"], waiting["exhausted"]), ("none", ["stale"], False))

    def test_when_every_account_is_past_the_threshold_each_is_used_up_to_99(self):
        others = [account("beta", 90, 50), account("gamma", 92, 60)]
        held = decide([account("alpha", 96, 40, active=True), *others])
        self.assertEqual(held["action"], "none")
        self.assertIn("up to 99%", held["hold"])
        drained = decide([account("alpha", 99, 40, active=True), *others])
        self.assertEqual((drained["action"], drained["target"]), ("switch", "beta"))
        full = decide([account("alpha", 99, 40, active=True), account("beta", 98, 50)])
        self.assertEqual((full["action"], full["exhausted"], full["all_low"]), ("none", True, True))
        # "Past the threshold" is claimed only when it is why none qualifies; otherwise the reason says why.
        signin = account("delta", 0, 0, health="needs_signin")
        self.assertIn("1 needs sign-in", decide([account("alpha", 96, 40, active=True), *others, signin])["hold"])
        stuck = decide([account("alpha", 99, 40, active=True), account("beta", 98, 50), signin])
        self.assertEqual((stuck["exhausted"], stuck["all_low"]), (True, False))
        self.assertTrue(stuck["reason"].startswith("No terminal account can take over: 1 needs sign-in, 1 is near or past 95%."))

    def test_refill_soon_and_expired_terminal_token_hold_a_threshold_switch_and_an_outage_does_not(self):
        target = account("beta", 10, 10)
        refill = decide([account("alpha", 96, 40, active=True, five_reset=300), target])
        self.assertEqual(refill["action"], "none")
        self.assertIn("refills in", refill["hold"])
        out = decide([account("alpha", 100, 40, active=True, five_reset=300), target])
        self.assertEqual((out["action"], out["kind"]), ("switch", "out"))      # out is never held
        self.assertEqual(decide([account("alpha", 96, 40, active=True), target])["action"], "switch")
        expired = decide([account("alpha", 96, 40, active=True, live_expired=True), target])
        self.assertEqual(expired["action"], "none")
        # The expired idle token holds a threshold switch only: a limit reached still moves the terminals.
        expired_out = decide([account("alpha", 100, 40, active=True, live_expired=True), target])
        self.assertEqual((expired_out["action"], expired_out["kind"]), ("switch", "out"))
        # A weekly reset that passed since the reading counts as refilled: no switch away right after it.
        self.assertEqual(decide([account("alpha", 10, 96, active=True, weekly_reset=-60, age=300), target])["action"], "none")

    def test_readings_without_a_5_hour_window_still_switch(self):
        # Claude's usage API stopped reporting a 5-hour window on 2026-10-31. Read as unknown, it left no
        # account eligible: the active one ran to 100% weekly while the others were nearly unused.
        def weekly_only(key, weekly, **extra):
            row = account(key, 0, weekly, **extra)
            row["windows"] = [w for w in row["windows"] if w["kind"] != "session"] + [
                {"id": "seven_day_fable", "kind": "model", "model": "Fable", "used": 0.0, "reset": NOW + DAY}]
            return row
        others = [weekly_only("beta", 10, weekly_reset=DAY), weekly_only("gamma", 0)]
        result = decide([weekly_only("alpha", 96, active=True), *others], pinned_model="Fable")
        self.assertEqual((result["action"], result["target"], result["kind"]), ("switch", "beta", "threshold"))
        out = decide([weekly_only("alpha", 100, active=True), *others], pinned_model="Fable")
        self.assertEqual((out["action"], out["target"], out["kind"], out["exhausted"]), ("switch", "beta", "out", False))
        # The pinned model matches by prefix, as the dashboard does: "Fable" is also a limit named "Fable 5.1".
        renamed = weekly_only("alpha", 10, active=True)
        renamed["windows"][-1].update(model="Fable 5.1", used=97.0)
        self.assertEqual(decide([renamed, *others], pinned_model="fable")["action"], "switch")
        # Missing data still never qualifies: no readings at all, or no weekly window.
        blank, no_weekly = {**weekly_only("blank", 0), "windows": []}, weekly_only("no_weekly", 0)
        no_weekly["windows"] = [w for w in no_weekly["windows"] if w["kind"] != "weekly"]
        self.assertEqual(decide([weekly_only("alpha", 100, active=True), blank, no_weekly], pinned_model="Fable")["action"], "none")

    def test_accounts_with_and_without_a_5_hour_window_side_by_side(self):
        # Max near its 5-hour limit moves to a weekly-only Ultra account with room.
        moved = decide([account("max", 97, 40, active=True), ultra("verified", 20)])
        self.assertEqual((moved["action"], moved["target"], moved["kind"]), ("switch", "verified", "threshold"))
        # A missing 5-hour window never holds a weekly-only account: not as a target, nor by "refills soon" when in use.
        self.assertIsNone(next(r for r in moved["next_up"] if r["id"] == "verified")["skip"])
        leaving = decide([ultra("verified", 96, active=True), account("max", 10, 10)])
        self.assertEqual((leaving["action"], leaving["target"], leaving["hold"]), ("switch", "max", None))
        staying = decide([ultra("verified", 40, active=True), account("max", 10, 10)])
        self.assertEqual((staying["action"], staying["hold"]), ("none", None))

    def test_a_new_meter_holds_and_moves_terminals_and_a_codename_does_not(self):
        target = ultra("beta", 10)
        opus = {"model": {"display_name": "Opus"}}
        for row, pinned in ((meter("burst"), ""), (meter("burst", scope={"surface": {"display_name": "Claude Code"}}), ""),
                            (meter("burst", scope={"model": {"display_name": "Fable"}}), ""), (meter("burst", scope=opus), "Opus")):
            moved = decide([answered("alpha", row, active=True), target], pinned_model=pinned)
            self.assertEqual((moved["action"], moved["target"], moved["kind"]), ("switch", "beta", "out"), row)
            skipped = decide([ultra("alpha", 96, active=True), answered("gamma", row)], pinned_model=pinned)
            self.assertEqual(skipped["next_up"][1]["id"], "gamma")
            self.assertRegex(skipped["next_up"][1]["skip"], "at 100%")
        # Past the threshold it moves them as any limit does, under the server's own label.
        scoped = meter("burst", 96.0, {"surface": {"display_name": "Claude Code"}, "model": None}, group="daily")
        self.assertIn("Claude Code daily", decide([answered("alpha", scoped, active=True), target])["reason"])
        for row in (meter("burst", scope={"surface": {"display_name": "Cowork"}}), meter("burst", scope=opus)):
            self.assertEqual(decide([answered("alpha", row, active=True), target])["action"], "none", row)
        # A codename window limits[] does not list is no limit, whatever its percentage.
        codename = {"iguana_necktie": {"utilization": 100.0, "resets_at": RESET}}
        self.assertEqual(decide([answered("alpha", top=codename, active=True), target])["action"], "none")
        self.assertIsNone(decide([ultra("alpha", 96, active=True), answered("gamma", top=codename)])["next_up"][1]["skip"])

    def test_the_fable_weekly_limit_holds_the_terminals_whatever_is_pinned(self):
        # With no model pinned, an account out of Fable was "healthy" (weekly 40%), so nothing moved and the next
        # account chosen could be out of Fable too.
        moved = decide([fable("alpha", 40, 96, active=True), fable("gone", 10, 100, weekly_reset=DAY), fable("beta", 30, 20)])
        self.assertEqual((moved["action"], moved["target"], moved["kind"]), ("switch", "beta", "threshold"))
        self.assertIn("Fable weekly", moved["reason"])
        self.assertEqual(next(r for r in moved["next_up"] if r["id"] == "gone")["skip"], "Fable weekly at 100%")
        self.assertEqual(decide([fable("alpha", 40, 100, active=True), fable("beta", 30, 20)])["kind"], "out")
        # Every account out of Fable only: said so, as the terminals still work for other models, and the account in
        # use is not left for another one that is out of Fable too.
        low = decide([fable("alpha", 40, 100, active=True), fable("beta", 30, 100)])
        self.assertEqual((low["action"], low["exhausted"], low["all_low"]), ("none", True, True))
        self.assertTrue(low["reason"].startswith("All terminal accounts are low on their Fable weekly limit; other models"))
        both = decide([fable("alpha", 99, 99, active=True), fable("beta", 98, 98)])
        self.assertTrue(both["reason"].startswith("All terminal accounts are low."))
        # An account whose week is spent works for no model: it is left for one whose week has room.
        spare = decide([fable("alpha", 100, 20, active=True), fable("beta", 30, 100), fable("gamma", 20, 100, health="needs_signin")])
        self.assertEqual((spare["action"], spare["target"], spare["kind"], spare["read_first"]), ("switch", "beta", "out", False))

    def test_the_users_own_choice_holds_against_the_threshold_until_99(self):
        # Undo, or a switch in AccountDeck, to an account past the threshold was reversed on the next tick.
        chosen = {"manual_at": NOW - 600, "manual_for": "alpha"}
        target = account("beta", 10, 10)
        held = decide([account("alpha", 10, 96, active=True), target], history=chosen)
        self.assertEqual(held["action"], "none")
        self.assertIn("You chose Alpha", held["hold"])
        for row in (account("alpha", 10, 99, active=True), account("alpha", 10, 96, active=True, forecast={"seven_day": 99.5})):
            moved = decide([row, target], history=chosen)
            self.assertEqual((moved["action"], moved["target"]), ("switch", "beta"))
        self.assertEqual(decide([account("alpha", 10, 100, active=True), target], history=chosen)["kind"], "out")
        self.assertEqual(decide([account("alpha", 10, 96, active=True, health="needs_signin"), target], history=chosen)["kind"], "login")
        # Not once the 2 hours are over, nor for another account than the one chosen.
        for other in ({"manual_at": NOW - 3 * HOUR, "manual_for": "alpha"}, {"manual_at": NOW - 600, "manual_for": "beta"}):
            self.assertEqual(decide([account("alpha", 10, 96, active=True), target], history=other)["action"], "switch")

    def test_the_early_switch_never_trades_places_nor_follows_an_automatic_switch(self):
        active = account("alpha", 10, 30, active=True, weekly_reset=10 * HOUR)

        def early(reset, history=None):
            return decide([active, account("beta", 10, 20, weekly_reset=reset)], history=history, cli_early_switch=True)
        # Two accounts with one reset time, reported a second apart, moved the terminals back and forth.
        self.assertEqual(early(10 * HOUR - 1)["action"], "none")
        self.assertEqual(early(9 * HOUR + 60)["action"], "none")
        moved = early(8 * HOUR)
        self.assertEqual((moved["action"], moved["target"], moved["kind"]), ("switch", "beta", "early"))
        # Not within 2 hours of any automatic switch (its Undo was gone within seconds), nor of the user's choice.
        after = early(8 * HOUR, {"last": {"kind": "threshold", "at": NOW - HOUR}})
        self.assertEqual(after["action"], "none")
        self.assertIn("automatic switch", after["hold"])
        self.assertEqual(early(8 * HOUR, {"manual_at": NOW - HOUR, "manual_for": "gamma"})["action"], "none")
        self.assertEqual(early(8 * HOUR, {"last": {"kind": "threshold", "at": NOW - 3 * HOUR}})["action"], "switch")

    def test_a_lock_counts_only_on_a_limit_that_holds_terminals(self):
        reason = {"locked_reason": "Locked for this account."}
        target = ultra("beta", 10)
        opus = meter("weekly_scoped", 5.0, {"model": {"display_name": "Opus"}}, **reason)
        cowork = {"seven_day_cowork": {"utilization": 5.0, "resets_at": RESET, **reason}}
        codename = {"iguana_necktie": {"utilization": None, "resets_at": None, **reason}}
        for elsewhere in (answered("alpha", opus, active=True), answered("alpha", top=cowork, active=True),
                          answered("alpha", top=codename, active=True)):
            self.assertEqual(decide([elsewhere, target])["action"], "none")
            as_target = decide([ultra("gamma", 96, active=True), {**elsewhere, "active": False}])
            self.assertEqual((as_target["action"], as_target["target"]), ("switch", "alpha"))
        # The same lock on the pinned model holds both ways, in the dashboard's words.
        moved = decide([answered("alpha", opus, active=True), target], pinned_model="Opus")
        self.assertEqual((moved["action"], moved["target"], moved["kind"]), ("switch", "beta", "out"))
        self.assertIn("is locked by Claude", moved["reason"])
        ruled_out = decide([ultra("gamma", 96, active=True), answered("alpha", opus)], pinned_model="Opus")
        self.assertEqual(next(r for r in ruled_out["next_up"] if r["id"] == "alpha")["skip"], "Locked by Claude")

    def test_a_fast_burn_switches_before_the_threshold_is_reached(self):
        active = account("alpha", 90, 40, active=True, forecast={"five_hour": 97.5})
        result = decide([active, account("beta", 10, 10)])
        self.assertEqual((result["action"], result["target"]), ("switch", "beta"))
        self.assertIn("before the next check", result["reason"])
        readings = [{"at": NOW - 600, "used": 80.0, "reset": NOW + HOUR}, {"at": NOW, "used": 90.0, "reset": NOW + HOUR}]
        self.assertAlmostEqual(cli_policy.forecast(readings, NOW, 300), 95.0)
        self.assertIsNone(cli_policy.forecast(readings, NOW, 2 * HOUR))     # it refills first
        readings[0]["reset"] += 0.4          # the API's reset time drifts by a second between readings
        self.assertAlmostEqual(cli_policy.forecast(readings, NOW, 300), 95.0)

    def test_an_out_account_moves_to_the_most_room_by_last_reading_when_none_qualifies(self):
        # Reads failing everywhere: waiting for fresh readings kept the terminals on a dead account.
        rows = [account("alpha", 100, 40, active=True),
                account("old", 0, 30, age=3 * HOUR),
                account("failing", 0, 20, age=600, read_failed=True),
                account("paused", 0, 10, age=1200, paused_until=NOW + 900, read_failed=True),     # a 429
                account("signin", 0, 0, health="needs_signin"),
                locked("locked"),
                account("ancient", 0, 0, age=2 * DAY),          # not read for a day: not trusted
                account("full", 0, 98, age=1200)]
        result = decide(rows)
        self.assertEqual((result["action"], result["target"], result["kind"], result["read_first"]),
                         ("switch", "paused", "out", False))
        skipped = {row["id"]: row["skip"] for row in result["next_up"]}
        self.assertEqual((skipped["failing"], skipped["paused"]), ("last read failed", "reads paused for 15 min"))
        # A failed automatic switch backs its target off (one alert, not one per retry) until a switch works.
        engine = types.SimpleNamespace(store=_Settings())
        repeats = [cli_auto.switch_settled(engine, "paused", ok=False, automatic=True, now=at) for at in (NOW - 1, NOW)]
        self.assertEqual(repeats, [False, True])
        cooled = decide(rows, history=engine.store.setting("cli_history"))
        self.assertEqual(cooled["target"], "failing")
        self.assertEqual(next(r["skip"] for r in cooled["next_up"] if r["id"] == "paused"), "switch failed, next try in 20 min")
        cli_auto.switch_settled(engine, "paused", ok=True, automatic=False, now=NOW)
        self.assertEqual(decide(rows, history=engine.store.setting("cli_history"))["target"], "paused")
        # Never to one that needs sign-in, is blocked, full or only known from a reading over a day old.
        stuck = decide([rows[0], *rows[4:]])
        self.assertEqual((stuck["action"], stuck["exhausted"], stuck["all_low"], stuck["refresh"]),
                         ("none", True, False, ["ancient"]))
        # Below the limit the active account still works: old readings are read again first, and so is a read
        # that failed (not one paused): one 503 kept threshold switches waiting for the scheduler's back-off.
        self.assertEqual(decide([account("alpha", 96, 40, active=True), *rows[1:]])["refresh"], ["ancient", "failing", "old"])
        # With no account 10 points below the threshold, still the most room rather than the owner's order:
        # Bravo resets sooner but is fuller, by a reading 20 hours old.
        nearly = decide([rows[0], account("bravo", 0, 96.5, weekly_reset=DAY, age=20 * HOUR),
                         account("charlie", 0, 86, weekly_reset=5 * DAY, age=600)])
        self.assertEqual((nearly["action"], nearly["target"], nearly["read_first"]), ("switch", "charlie", False))
        # A login ending within 12 hours beats staying on a dead account while it has 2 hours left (never a
        # blocked one); a threshold switch still needs 12 hours.
        ending = account("ending", 0, 10, login_ends_at=NOW + 6 * HOUR)
        self.assertEqual(decide([rows[0], ending, locked("gone", login_ends_at=NOW + 6 * HOUR)])["target"], "ending")
        self.assertEqual(decide([rows[0], {**ending, "login_ends_at": NOW + 1.5 * HOUR}])["action"], "none")
        self.assertEqual(decide([account("alpha", 96, 40, active=True), ending])["action"], "none")

    def test_the_same_accounts_give_the_same_target_in_any_input_order(self):
        # Pairwise "near-equal" comparisons were not transitive: these trios gave a different target per input order.
        soonest = [account("a", 0, 85, weekly_reset=DAY), account("b", 0, 45, weekly_reset=DAY + 3000),
                   account("c", 0, 5, weekly_reset=DAY + 6000)]
        roomiest = [account("x", 0, 43, weekly_reset=3 * DAY), account("y", 0, 43.75, weekly_reset=2 * DAY),
                    account("z", 0, 44.5, weekly_reset=DAY)]
        for mode, trio, expected in (("soonest_reset", soonest, "b"), ("most_room", roomiest, "y")):
            targets = {decide([account("alpha", 96, 40, active=True), *order], cli_order=mode)["target"]
                       for order in itertools.permutations(trio)}
            self.assertEqual(targets, {expected}, mode)


class LimitErrorTests(SyntheticTestCase):
    """A limit error in a transcript moves every terminal (cli_watch.LimitWatch): it must be charged to the account
    that hit it, and a reading that shows room must take it back."""

    def setUp(self):
        super().setUp()
        self.now = time.time()
        ids = {name: new_uuid() for name in ("alpha", "bravo")}
        self.logins = {name: terminal_login(ids[name], f"{name}@example.test", now=self.now, token=name) for name in ids}
        self.sign_in_terminal(*self.logins["alpha"])
        self.engine = self.make_engine(owners={f"refresh-{name}": ids[name] for name in ids}, clock=lambda: self.now)
        self.alpha = self.engine.store.find("claude_cli", ids["alpha"])["id"]
        self.bravo = self.save_terminal(self.engine, *self.logins["bravo"])
        self.reading(self.alpha, 40)
        self.reading(self.bravo, 40)
        self.transcript = self.paths.claude_projects / "-Users-me-work" / "session.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.touch()
        self.engine.watch.transcripts.poll()
        self.engine.watch.tick()

    def reading(self, key, used):
        self.engine.store.observe(key, claude_usage({"seven_day": {"utilization": used, "resets_at": RESET}}, now=self.now), at=self.now)

    def error_at(self, at, limit="weekly", kind="seven_day"):
        """Claude Code writes a limit error (the weekly one unless named), and the watcher and the tick see it."""
        info = {"status": "rejected", "rateLimitType": kind, "resetsAt": int(at + 5 * DAY)}
        line = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit", "apiErrorStatus": 429,
                "timestamp": dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat(), "apiErrorParams": {"rate_limit_info": info},
                "message": {"content": [{"type": "text", "text": f"You've hit your {limit} limit · resets Nov 7 at 12pm (America/Denver)"}]}}
        with open(self.transcript, "ab") as handle:
            handle.write(json.dumps(line, separators=(",", ":")).encode() + b"\n")
        self.engine.watch.transcripts.poll()
        self.engine.watch.tick()

    def out(self):
        """The active account's `out` once the read an error asks for has had its moment (it never comes here)."""
        self.now += cli_watch.READ_GRACE + 1
        return next(row for row in cli_auto.inputs(self.engine) if row["active"])["out"]

    def test_an_error_up_to_the_margin_after_an_account_change_belongs_to_the_account_left(self):
        self.sign_in_terminal(*self.logins["bravo"])
        self.engine.sense_active()
        self.engine.watch.tick()
        changed = self.now
        self.now += 60
        self.error_at(changed - 10)
        self.assertFalse(self.out())
        self.error_at(changed + cli_watch.SETTLE - 30)
        self.assertFalse(self.out())
        self.assertEqual(cli_auto.decision(self.engine)["action"], "none")
        # A switch before a restart still counts, from the store: nothing in memory knows of it then.
        self.engine.store.set_setting("cli_history", {"left_at": self.now})
        self.engine.watch = restarted = LimitWatch(self.engine)
        restarted.tick()
        restarted.transcripts.poll()
        self.now += 10
        self.error_at(self.now - 1)
        self.assertFalse(self.out())
        # Past the margin it is this account's own, unless it is over 5 minutes old.
        self.now += cli_watch.SETTLE + 301
        self.error_at(self.now - 301)
        self.assertFalse(self.out())
        self.error_at(self.now - 1)
        self.assertTrue(self.out())
        decided = cli_auto.decision(self.engine)
        self.assertEqual((decided["action"], decided["kind"], decided["target"]), ("switch", "out", self.alpha))

    def test_a_reading_with_room_takes_an_error_back_and_no_reading_leaves_it_standing(self):
        self.now += 10
        self.error_at(self.now - 1)
        self.assertTrue(self.out())               # no reading since the error: its read failed, or could not go
        self.engine.store.update(self.alpha, error="No connection.", error_code="network")
        self.assertTrue(self.out())
        self.assertEqual(cli_auto.decision(self.engine)["target"], self.bravo)
        self.reading(self.alpha, 97)              # one that confirms it
        self.assertTrue(self.out())
        self.reading(self.alpha, 94)              # below the threshold in the limit it named
        self.assertFalse(self.out())
        self.assertEqual(cli_auto.decision(self.engine)["action"], "none")
        self.now += 10
        self.error_at(self.now - 1)               # a further error after that reading
        self.assertTrue(self.out())
        self.sign_in_terminal(*self.logins["bravo"])
        self.engine.sense_active()
        self.engine.watch.tick()                  # it stood against alpha, not against the account now in use
        self.assertFalse(self.out())

    def test_a_fable_limit_error_counts_with_no_model_pinned(self):
        # It was "unwatched" unless Fable was pinned: the terminals stayed on an account that refused every message.
        self.now += 10
        self.error_at(self.now - 1, "Fable", "seven_day_overage_included")
        self.assertTrue(self.out())
        self.assertEqual(cli_auto.decision(self.engine)["target"], self.bravo)
        self.now += 10
        self.reading(self.alpha, 40)              # no Fable limit in it: it shows no room there
        self.assertTrue(self.out())

    def test_the_forecast_forgets_readings_from_before_the_terminals_came_or_the_mac_slept(self):
        # An account last in use at 40% that came back at 90% looked as if it burned 50 points in minutes.
        def projected():
            return next(row for row in cli_auto.inputs(self.engine) if row["active"])["forecast"]
        self.now += 900
        self.reading(self.alpha, 90)
        self.assertGreater(projected()["seven_day"], 90)
        self.engine.store.set_setting("cli_history", {"left_at": self.now - 300})
        self.assertEqual(projected(), {})
        self.engine.store.set_setting("cli_history", {})
        self.engine.woke_at = self.now - 300
        self.assertEqual(projected(), {})

    def test_auto_switch_never_returns_to_an_account_left_at_a_limit(self):
        # Every read fails, so the out fallback goes by last good readings, and those predate the limit errors.
        charlie = self.save_terminal(self.engine, *terminal_login(new_uuid(), "charlie@example.test", now=self.now, token="charlie"))
        for key, used in ((self.alpha, 50), (self.bravo, 30), (charlie, 80)):
            self.engine.store.observe(key, claude_usage({"seven_day": {"utilization": used, "resets_at": RESET}}, now=self.now),
                                      at=self.now - 600)
            self.engine.store.update(key, error="No connection.", error_code="network")
        self.now += 10
        self.error_at(self.now - 1)
        self.assertTrue(self.out())
        self.assertEqual(cli_auto.decision(self.engine)["target"], self.bravo)
        self.sign_in_terminal(*self.logins["bravo"])
        self.engine.sense_active()
        self.engine.watch.tick()
        self.now += 200
        self.error_at(self.now - 1)
        self.assertTrue(self.out())
        # Not alpha, though its last reading has the most room: it hit its limit and nothing since says otherwise.
        self.assertEqual(cli_auto.decision(self.engine)["target"], charlie)
        self.engine.watch = LimitWatch(self.engine)          # a restart keeps it
        self.engine.watch.tick()
        skips = {row["id"]: row["skip"] for row in cli_auto.decision(self.engine)["next_up"]}
        self.assertEqual(skips[self.alpha], "reached a limit")


if __name__ == "__main__":
    unittest.main()
