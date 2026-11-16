"""Claude usage answers the switch policy must not trust as they come. An answer without the weekly limit
(the API already dropped the 5-hour one on 2026-10-31) is a failed read that keeps the last good reading:
stored as a reading, the active account could never trigger a switch and every other account would read
"weekly unknown". A locked weekly limit blocks the account whatever its percentage, so it reads out and
is never a target."""
import time
import types
import unittest

from tests.helpers import SyntheticTestCase, new_uuid, terminal_login

from accountdeck import rules
from accountdeck.alerts import AlertEngine
from accountdeck.providers.claude import is_hollow
from accountdeck.providers.claude_cli import ClaudeCLI
from accountdeck.providers.errors import NO_WEEKLY
from accountdeck.providers.fake import FakeClaude
from accountdeck.usage import LOCKED, claude_usage, has_weekly

RESET = "2099-01-01T00:00:00+00:00"
FABLE = {"kind": "weekly_scoped", "group": "weekly", "percent": 12.0, "severity": "normal", "resets_at": RESET,
         "scope": {"model": {"id": None, "display_name": "Fable"}}, "is_active": False}
# Today's answer: no 5-hour window; the weekly limit both as `seven_day` and as the limits[] "weekly_all" row.
GOOD = {"five_hour": None, "seven_day": {"utilization": 40.0, "resets_at": RESET, "locked_reason": None},
        "limits": [{"kind": "weekly_all", "group": "weekly", "percent": 41.0, "severity": "normal",
                    "resets_at": RESET, "scope": None, "is_active": True}, FABLE]}
# The same answer with the weekly limit gone as well: only the model-scoped row is left.
NO_WEEKLY_ANSWER = {"five_hour": None, "seven_day": None, "iguana_necktie": None, "limits": [FABLE]}


def weekly(usage):
    return next(w for w in usage["windows"] if w["id"] == "seven_day")


class ClaudeUsageTests(SyntheticTestCase):
    def test_an_answer_without_the_weekly_limit_is_a_failed_read_that_keeps_the_last_reading(self):
        identity = new_uuid()
        self.sign_in_terminal(*terminal_login(identity, "cli@example.test", token="cli"))
        self.fake_home()                                         # Claude Desktop signed in as well
        clock = [time.time()]
        desktop_reads = FakeClaude(self.paths, [(0, GOOD), (60, NO_WEEKLY_ANSWER)], now=lambda: clock[0])
        engine = self.make_engine(claude=desktop_reads, owners={"refresh-cli": identity})
        desktop = engine.accounts.capture("claude", claude_running=False)
        terminal = engine.store.find("claude_cli", identity)["id"]
        answers = [GOOD]
        self.terminal.http = lambda method, url, **kwargs: (answers[-1], {"status": 200, "class": "2xx"})
        self.terminal.usage = types.MethodType(ClaudeCLI.usage, self.terminal)    # the real read on scripted answers
        reads = {desktop: lambda: engine.accounts.read(desktop, active_identity=dict(engine.active_identity)),
                 terminal: lambda: engine.cli.read(terminal)}
        good = {}
        for key, read in reads.items():
            self.assertEqual(read(), "ok")
            good[key] = engine.store.account(key)
            self.assertEqual(weekly(good[key]["last_good_usage"])["used"], 41.0)
        clock[0] += 120
        answers.append(NO_WEEKLY_ANSWER)
        for key, read in reads.items():
            self.assertEqual(read(), "network")
            row = engine.store.account(key)
            self.assertEqual(row["error_code"], "network")
            self.assertEqual((row["last_good_usage"], row["last_good_at"]),
                             (good[key]["last_good_usage"], good[key]["last_good_at"]))
        self.assertEqual(engine.store.account(desktop)["error"], NO_WEEKLY)

    def test_a_locked_weekly_limit_is_blocked_through_the_weekly_all_replacement(self):
        reason = "Weekly usage is locked for this account."
        window = weekly(claude_usage({**GOOD, "seven_day": {**GOOD["seven_day"], "locked_reason": reason}}))
        self.assertEqual((window["used"], window["blocked"], window["blocked_reason"]), (41.0, LOCKED, reason))
        self.assertEqual(rules.severity(window["used"], window["blocked"], None), "out")     # 59 % left, still out
        # Not a lock: a null reason, or extra_usage.spend_limit_reached (paid usage credits, not the plan's limits).
        credits = claude_usage({**GOOD, "extra_usage": {"is_enabled": False, "spend_limit_reached": True}})
        self.assertEqual([w["blocked"] for w in credits["windows"]], [None, None])
        # A lock reported without a percentage is still a reading (the account is out) and never hollow.
        bare = claude_usage({"seven_day": {"utilization": None, "resets_at": None, "locked_reason": reason}})
        self.assertTrue(has_weekly(bare))
        self.assertFalse(is_hollow(bare))

    def test_a_malformed_answer_never_raises_and_reads_as_a_failed_read(self):
        row = {"kind": "weekly_all", "percent": None, "resets_at": "soon", "scope": "all"}
        for answer in (None, [], "text", 7, {}, {"limits": "rows"}, {"limits": {"kind": "weekly_all"}},
                       {"limits": [None, 3, [], "x"]}, {"limits": [row]},
                       {"seven_day": {"utilization": "40"}}, {"seven_day": {"utilization": 10 ** 400}},
                       {"five_hour": {"utilization": float("nan")}, "seven_day": []}):
            self.assertFalse(has_weekly(claude_usage(answer)), answer)
        # Garbage beside a good weekly row is left out, and a reset that is no time is no reset.
        usage = claude_usage({"limits": ["x", {**row, "percent": 40.0, "scope": None},
                                         {"percent": None, "kind": "session"}]})
        self.assertEqual([(w["id"], w["used"], w["reset"]) for w in usage["windows"]], [("seven_day", 40.0, None)])

    def test_limits_is_the_list_of_meters(self):
        codename = {"iguana_necktie": {"utilization": 100.0, "resets_at": RESET}}
        # A row makes its window whatever the top-level keys say.
        session = {"kind": "session", "percent": 30.0, "resets_at": RESET, "scope": None}
        usage = claude_usage({"five_hour": None, "limits": [session, {**GOOD["limits"][0]}], **codename})
        self.assertEqual([w["id"] for w in usage["windows"]], ["five_hour", "seven_day"])
        # A codename window the list does not have is neither shown nor counted; "Raw usage" keeps it.
        self.assertEqual({r["key"]: r["used"] for r in usage["raw_windows"]}["iguana_necktie"], 100.0)
        # With no list at all nothing says what it is, so it shows as before; with a lock it always shows.
        bare = claude_usage({"seven_day": GOOD["seven_day"], **codename})
        self.assertIn("iguana_necktie", [w["id"] for w in bare["windows"]])
        locked = {**codename["iguana_necktie"], "utilization": None, "resets_at": None, "locked_reason": {"code": "x"}}
        held = next(w for w in claude_usage({**GOOD, "iguana_necktie": locked})["windows"] if w["id"] == "iguana_necktie")
        self.assertEqual((held["used"], held["blocked"]), (None, LOCKED))
        # A lock without a percentage holds on its alias, which keeps its percentage, and needs none of its own.
        lock = {"kind": "weekly_all", "percent": None, "locked_reason": "Locked."}
        self.assertEqual(weekly(claude_usage({"seven_day": GOOD["seven_day"], "limits": [lock]}))["used"], 40.0)
        self.assertEqual(weekly(claude_usage({"seven_day": GOOD["seven_day"], "limits": [lock]}))["blocked"], LOCKED)
        self.assertTrue(has_weekly(claude_usage({"limits": [lock]})))
        # A new meter keeps the server's label and holds terminals unless it is scoped elsewhere.
        burst = {"kind": "burst", "group": "daily", "percent": 12.0, "resets_at": RESET}
        scoped = [{**burst, "scope": {"surface": {"display_name": name}}} for name in ("Claude Code", "Cowork")]
        windows = claude_usage({**GOOD, "limits": [burst, *scoped]})["windows"]
        self.assertEqual([(w["label"], w["gates"]) for w in windows if w["kind"] == "other"],
                         [("Burst", True), ("Claude Code daily", True), ("Cowork daily", False)])


class UsageCreditAlertTests(unittest.TestCase):
    def test_only_a_rise_in_money_spent_alerts_not_a_first_reading_or_a_new_month(self):
        """A first reading of an amount spent long ago and a month's reset to 0 say nothing; any rise is real money."""
        def state(used):
            credits = [] if used is None else [{"label": "Usage credits", "enabled": True, "used": used, "limit": 10000,
                                                "exponent": 2, "currency": "USD"}]
            return {"accounts": [{"id": "a", "provider": "claude_cli", "label": "a", "email": "a@example.test",
                                  "active": False, "credential": {"state": "fresh"}, "last_good_at": 1.0, "error": None,
                                  "windows": [], "credits": credits}]}

        engine, latches, said = AlertEngine(started=0), {}, []
        for hour, used in enumerate((None, 820, 820, 1020, 1020, 0, 0, 40), 1):
            alerts, latches = engine.evaluate(None, state(used), latches, hour * 3600.0)
            said.append([alert["message"] for alert in alerts])
        self.assertEqual(said, [[], [], [], ["a spent $10.20 of usage credits this month ($10.20 of $100)."], [], [], [],
                                ["a spent $0.40 of usage credits this month ($0.40 of $100)."]])


if __name__ == "__main__":
    unittest.main()
