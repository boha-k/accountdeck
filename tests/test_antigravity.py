"""Antigravity (agy_accounts, switch_antigravity): the paths where a saved sign-in could be lost, a usage reading could
land on the wrong account, or something could happen without the user's click."""
from __future__ import annotations

import copy
import time

from tests.helpers import (FAST, SyntheticTestCase, new_uuid, terminal_login,  # noqa: F401  (safe mode first)
                           test_cipher)
from accountdeck import cli_auto, contract
from accountdeck.core import AGY_ENROLLMENT, DeckError, Vault, agy_key, agy_transaction_key
from accountdeck.fixtures import agy_reading
from accountdeck.procs import FakeProcessController
from accountdeck.providers.fake import FakeAntigravity, agy_record, agy_usage_output
from accountdeck.usage import claude_usage

ALPHA, BETA, CAROL, DAVE = "alpha@example.test", "beta@example.test", "carol@example.test", "dave@example.test"
ERIN = "erin@example.test"


class Clock:
    """The engine's time, moved by hand."""

    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class AgyCase(SyntheticTestCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.agy = FakeAntigravity(self.paths, installed=True, clock=self.clock)
        self.procs = FakeProcessController(self.paths, timings=FAST)
        self.engine = self.start()

    def start(self):
        return self.make_engine(antigravity=self.agy, procs=self.procs, clock=self.clock,
                                vault=Vault(self.paths.data_dir / "vault", protect=test_cipher))

    def act(self, name, **body):
        return self.engine.action(name, {"provider": "antigravity", **body})

    def confirmed(self, name, purpose, consent="now", **body):
        """`name` through its plan, as the dashboard sends it: switch-plan, then the action with the plan's id."""
        plan = self.act("switch-plan", purpose=purpose, **body)["plan"]
        self.assertEqual((plan["blockers"], contract.validate_plan(plan)), ([], []))
        return self.act(name, plan_id=plan["plan_id"], consent=consent, **body)

    def finish(self):
        self.assertTrue(self.engine.runner.wait(5))
        return self.engine.runner.report()

    def sign_in(self, email):
        self.agy.sign_in(agy_record(email, now=self.clock()))
        self.engine.sense_active(only="antigravity")

    def save(self, email):
        """agy signed in to `email` (as the user would in agy itself), then Save. Returns the account's id."""
        self.sign_in(email)
        self.confirmed("capture", "capture")
        self.assertEqual(self.finish()["outcome"], "success")
        return self.engine.store.agy_find(email)["id"]

    def holds(self):
        found = self.agy.read()
        return found.email if found.state == "ok" else found.state

    def saved_email(self, account_id):
        return self.engine.vault.get(agy_key(account_id))["stores"]["cli"]["id_token_email"]

    def transactions(self):
        return list(self.engine.vault.agy_transaction_keys())

    def block(self):
        return self.engine.snapshot()["providers"]["antigravity"]

    def named(self, *ids):
        """Those of `ids` that the alerts table names as its accounts or an event's text holds: older builds read
        both as their own apps', so no Antigravity id may be there."""
        with self.engine.store.lock:
            accounts = {row[0] for row in self.engine.store.db.execute("SELECT account FROM alerts")}
            texts = [row[0] for row in self.engine.store.db.execute("SELECT message FROM events")]
        return {key for key in ids if key in accounts or any(key in (text or "") for text in texts)}

    def wait_phase(self, phase):
        deadline = time.monotonic() + 5
        while (self.engine.pending or {}).get("phase") != phase and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual((self.engine.pending or {}).get("phase"), phase)


class SwitchTests(AgyCase):
    def test_a_switch_never_loses_the_outgoing_sign_in(self):
        self.sign_in(ALPHA)
        plan = self.act("switch-plan", purpose="capture")["plan"]    # nothing before the notice about Google's terms
        with self.assertRaises(DeckError) as refused:
            self.act("capture", plan_id=plan["plan_id"], consent="now")
        self.assertEqual(refused.exception.code, "notice_required")
        self.act("agy-accept-notice")
        alpha = self.save(ALPHA)
        beta = self.save(BETA)
        # Two ways the write can fail: refused, and the item left undecodable.
        for outcome in ("fail", "garble"):
            with self.subTest(write=outcome):
                self.agy.script_writes(outcome)
                self.confirmed("switch", "switch", id=alpha)
                report = self.finish()
                self.assertEqual(report["outcome"], "failed")
                self.assertIn("The earlier sign-in is back.", report["error"]["message"])
                self.assertEqual(self.holds(), BETA)
                self.assertEqual((self.saved_email(alpha), self.saved_email(beta)), (ALPHA, BETA))
                self.assertEqual(self.transactions(), [])
        # A third account's sign-in is in agy's item after the write (another program wrote it): it is saved nowhere,
        # so the switch leaves it there, and Recover saves it before it puts Beta's back.
        self.agy.script_writes(agy_record(ERIN, now=self.clock()))
        self.confirmed("switch", "switch", id=alpha)
        report = self.finish()
        self.assertEqual((report["outcome"], report["error"]["code"], self.holds()), ("failed", "recovery", ERIN))
        self.act("recover", keep="before")
        self.assertEqual((self.finish()["outcome"], self.holds(), self.transactions()), ("success", BETA, []))
        erin = self.engine.store.agy_find(ERIN)
        self.assertEqual(self.saved_email(erin["id"]) if erin else None, ERIN)
        # The same for a sign-in AccountDeck cannot save (a work account): it stays too, and Recover can leave it.
        self.agy.script_writes(agy_record(DAVE, auth_method="enterprise", now=self.clock()))
        self.confirmed("switch", "switch", id=alpha)
        report = self.finish()
        self.assertEqual((report["outcome"], report["error"]["code"], self.agy.read().reason),
                         ("failed", "recovery", "not_consumer"))
        self.act("recover", keep="keep")
        self.assertEqual((self.finish()["outcome"], self.agy.read().reason, self.transactions()),
                         ("success", "not_consumer", []))
        self.sign_in(BETA)
        # The write leaves the item undecodable and putting the earlier record back fails too: the transaction keeps
        # both records for Recover.
        self.agy.script_writes("garble", "fail")
        self.confirmed("switch", "switch", id=alpha)
        report = self.finish()
        self.assertEqual((report["outcome"], report["error"]["code"]), ("failed", "recovery"))
        self.assertEqual(self.holds(), "unknown")
        self.assertEqual(len(self.transactions()), 1)
        self.assertEqual(self.block()["recovery"], {"started": self.block()["recovery"]["started"], "account": alpha,
                                                    "before": BETA, "after": ALPHA})
        plan = self.act("switch-plan", purpose="switch", id=alpha)["plan"]
        with self.assertRaises(DeckError) as blocked:     # nothing else changes agy's store until it is recovered
            self.act("switch", id=alpha, plan_id=plan["plan_id"], consent="now")
        self.assertEqual(blocked.exception.code, "recovery")
        # Recover writes nothing over a store it cannot read: what is there may be a sign-in it cannot save.
        self.act("recover", keep="before")
        self.assertEqual((self.finish()["outcome"], self.holds(), len(self.transactions())), ("failed", "unknown", 1))
        # The user signs agy in again meanwhile (as Dave): Recover saves that sign-in before it puts Beta's back.
        self.agy.sign_in(agy_record(DAVE, now=self.clock()))
        self.act("recover", keep="before")
        self.assertEqual(self.finish()["outcome"], "success")
        self.assertEqual((self.holds(), self.transactions(), self.block()["recovery"]), (BETA, [], None))
        dave = self.engine.store.agy_find(DAVE)
        self.assertEqual(self.saved_email(dave["id"]) if dave else None, DAVE)
        # And the switch itself: the target in place, the outgoing account's copy kept, nothing left over.
        self.agy.renew()          # agy's hourly renewal: the saved copy follows only a new sign-in, not this
        self.confirmed("switch", "switch", id=alpha)
        report = self.finish()
        self.assertEqual((report["outcome"], report["verified"]), ("success", "verified"))
        self.assertEqual((self.holds(), self.saved_email(beta), self.transactions()), (ALPHA, BETA, []))
        self.assertEqual(self.block()["active"], alpha)
        label = self.engine.store.agy_account(alpha)["label"]
        self.assertIn(f"Switched Antigravity to {label}.", [a["title"] for a in self.engine.snapshot()["alerts"]])
        self.assertEqual(self.named(alpha, beta, dave["id"]), set())
        # Switching away from an account that is not saved saves it first.
        self.sign_in(CAROL)
        self.confirmed("switch", "switch", id=beta)
        self.assertEqual(self.finish()["outcome"], "success")
        carol = self.engine.store.agy_find(CAROL)
        self.assertEqual((self.holds(), self.saved_email(carol["id"])), (BETA, CAROL))

    def test_a_switch_waits_for_agy_and_never_stops_it(self):
        self.engine.agy.accept_notice()
        alpha = self.save(ALPHA)
        beta = self.save(BETA)
        running = self.procs.add_agy()
        with self.assertRaises(DeckError) as now:            # "Switch now" only when no agy runs
            self.confirmed("switch", "switch", consent="now", id=alpha)
        self.assertEqual(now.exception.code, "busy")
        plan = self.act("switch-plan", purpose="add")["plan"]
        with self.assertRaises(DeckError) as add:            # Add would sign the running agy out: refused
            self.act("add", plan_id=plan["plan_id"], consent="idle")
        self.assertEqual(add.exception.code, "busy")
        for given_way in (True, False):
            plan = self.act("switch-plan", purpose="switch", id=alpha)["plan"]
            self.assertEqual([row["pid"] for row in plan["running"]["agy_consumers"]], [running.pid])
            self.assertFalse(plan["idle"])
            self.act("switch", id=alpha, plan_id=plan["plan_id"], consent="idle")
            self.wait_phase("waiting_idle")
            time.sleep(0.3)
            self.assertEqual(self.engine.pending["phase"], "waiting_idle")
            self.assertEqual((self.agy.writes, self.holds()), ([], BETA))     # nothing changes while agy runs
            self.assertIn(running.pid, [row.pid for row in self.procs.snapshot()])     # and it is never stopped
            if given_way:
                # The terminals' auto-switch needs the job slot (cli_auto._make_way): the waiting switch gives way,
                # and the alert saying so names no Antigravity account.
                self.assertTrue(self.engine.runner.preempt(cli_auto.MADE_WAY, self.engine.pending["id"]))
                self.assertEqual(self.finish()["outcome"], "cancelled")
                self.assertEqual((self.named(alpha, beta), self.holds()), (set(), BETA))
        self.procs.exit_process(running.pid)                 # the user closes agy
        report = self.finish()
        self.assertEqual((report["outcome"], self.holds()), ("success", ALPHA))


class AddTests(AgyCase):
    def setUp(self):
        super().setUp()
        self.engine.agy.accept_notice()
        self.alpha = self.save(ALPHA)

    def add(self, **body):
        """Add (Sign In Again with `id`) through its plan. Returns the pid of the agy in the window it opened."""
        self.confirmed("add", "add", **body)
        self.assertEqual(self.finish()["outcome"], "success")
        self.assertEqual(self.holds(), "absent")             # agy shows its sign-in screen in the window opened
        self.assertTrue(self.engine.vault.path(AGY_ENROLLMENT).exists())
        window = self.procs.agy_consumers()
        self.assertEqual(len(window), 1)
        return window[0].pid

    def ended(self, window):
        """The user closes agy's window; the next tick sees it."""
        self.procs.exit_process(window)
        self.engine.tick()

    def test_ending_an_add_never_writes_while_agy_runs(self):
        window = self.add()
        write = self.agy._write_store

        def racing(record):          # agy's own write of a new sign-in lands just before AccountDeck's
            if window in [row.pid for row in self.procs.snapshot()]:
                self.agy.sign_in(agy_record(BETA, now=self.clock()))
            return write(record)
        self.agy._write_store = racing
        # Cancel while agy's window still runs: a sign-in may finish there at any moment, so the earlier record does
        # not go back yet (it would land over that sign-in, or move the agy that made it).
        self.act("cancel-login")
        self.assertEqual(self.finish()["outcome"], "success")
        enrollment = self.block()["enrollment"]
        self.assertEqual((enrollment["phase"], enrollment["expires"], self.holds()), ("restoring", None, "absent"))
        self.assertEqual(enrollment["message"],
                         f"Close agy's sign-in window. AccountDeck puts {ALPHA} back as soon as agy has closed.")
        with self.assertRaises(DeckError) as refused:        # every other Antigravity action waits, saying why
            self.act("refresh")
        self.assertEqual((refused.exception.code, enrollment["message"] in str(refused.exception)), ("busy", True))
        # The user finishes signing in there after all: that sign-in is saved, and nothing is written over it.
        self.agy.sign_in(agy_record(BETA, now=self.clock()))
        self.engine.tick()
        beta = self.engine.store.agy_find(BETA)
        self.assertEqual((self.block()["enrollment"]["phase"], self.holds()), ("saved", BETA))
        self.assertEqual((self.saved_email(beta["id"]) if beta else None, self.saved_email(self.alpha)), (BETA, ALPHA))
        self.ended(window)
        self.assertEqual((self.holds(), self.agy.writes), (BETA, []))
        self.assertFalse(self.engine.vault.path(AGY_ENROLLMENT).exists())

    def test_an_add_or_a_recover_looks_again_right_before_it_writes(self):
        # Reading the store, saving the outgoing sign-in and writing its own record take AccountDeck a moment. An agy
        # the user starts in that moment, and a sign-in it makes, are safe: the last look comes right before the write.
        put, keep = self.engine.vault.put, self.engine.agy.keep_outgoing

        def lands_after_the_record(key, value):           # agy starts and signs in as Beta while the Add prepares
            put(key, value)
            if key == AGY_ENROLLMENT and not self.procs.agy_consumers():
                self.procs.add_agy()
                self.agy.sign_in(agy_record(BETA, now=self.clock()))
        self.engine.vault.put = lands_after_the_record
        self.confirmed("add", "add")
        report = self.finish()
        self.assertEqual((report["outcome"], report["error"]["code"], self.holds()), ("failed", "busy", BETA))
        self.assertEqual((self.block()["enrollment"], self.engine.vault.path(AGY_ENROLLMENT).exists()), (None, False))
        self.engine.vault.put = put
        self.procs.exit_process(self.procs.agy_consumers()[0].pid)
        # Recover, with a switch left unfinished: an agy starts after Recover saved the sign-in agy holds (Beta's).
        tx = agy_transaction_key()
        put(tx, {"v": 1, "status": "conflict", "at": self.clock(), "account": self.alpha,
                 "before": agy_record(ALPHA, now=self.clock()), "before_email": ALPHA,
                 "after": agy_record(CAROL, now=self.clock()), "after_email": CAROL})
        self.engine.agy.scan_transactions()

        def starts_after_the_save(found):
            kept = keep(found)
            self.procs.add_agy()
            return kept
        self.engine.agy.keep_outgoing = starts_after_the_save
        self.act("recover", keep="before")
        report = self.finish()
        self.assertEqual((report["outcome"], report["error"]["code"]), ("failed", "busy"))
        self.assertEqual((self.holds(), self.transactions(), self.agy.writes), (BETA, [tx], []))
        beta = self.engine.store.agy_find(BETA)                      # saved all the same: nothing of agy's changed
        self.assertIsNotNone(beta)
        # A switch, with nothing left to recover: an agy starts after the switch wrote its own record.
        self.engine.agy.keep_outgoing = keep
        self.procs.exit_process(self.procs.agy_consumers()[0].pid)
        self.act("recover", keep="keep")
        self.assertEqual((self.finish()["outcome"], self.transactions()), ("success", []))

        def starts_after_the_record(key, value):
            put(key, value)
            if key.startswith("agy-transaction-"):
                self.procs.add_agy()
        self.engine.vault.put = starts_after_the_record
        self.confirmed("switch", "switch", id=self.alpha)
        report = self.finish()
        self.assertEqual((report["outcome"], report["error"]["code"]), ("failed", "busy"))
        self.assertEqual((self.holds(), self.transactions(), self.agy.writes), (BETA, [], []))
        # And a sign-in that lands in that moment while no agy runs any more (an agy that signed in and closed).
        self.procs.exit_process(self.procs.agy_consumers()[0].pid)

        def lands_after_the_switch_record(key, value):
            put(key, value)
            if key.startswith("agy-transaction-"):
                self.agy.sign_in(agy_record(CAROL, now=self.clock()))
        self.engine.vault.put = lands_after_the_switch_record
        self.confirmed("switch", "switch", id=self.alpha)
        report = self.finish()
        self.assertEqual((report["outcome"], report["error"]["code"]), ("failed", "busy"))
        self.assertEqual((self.holds(), self.transactions(), self.agy.writes), (CAROL, [], []))

    def test_an_add_that_ends_without_a_sign_in_puts_the_earlier_one_back(self):
        window = self.add()                                   # cancelled: Alpha goes back once agy's window closed
        self.act("cancel-login")
        self.assertEqual(self.finish()["outcome"], "success")
        self.assertEqual((self.holds(), self.block()["enrollment"]["phase"]), ("absent", "restoring"))
        self.ended(window)
        self.assertEqual((self.holds(), self.block()["enrollment"]["phase"]), (ALPHA, "cancelled"))
        self.ended(self.add())                                # the user closes agy's window: the Add ends at once
        self.assertEqual((self.holds(), self.block()["enrollment"]["phase"]), (ALPHA, "cancelled"))
        window = self.add()                                   # nobody signs in within 15 minutes
        self.clock.advance(15 * 60 + 1)
        self.engine.tick()
        self.assertEqual((self.holds(), self.block()["enrollment"]["phase"]), ("absent", "restoring"))
        self.ended(window)
        self.assertEqual((self.holds(), self.block()["enrollment"]["phase"]), (ALPHA, "timed_out"))
        window = self.add()                                   # AccountDeck restarts in the middle...
        self.engine.stop()
        self.engine = self.start()
        self.assertEqual(self.block()["enrollment"]["phase"], "signing_in")
        self.act("cancel-login")
        self.assertEqual(self.finish()["outcome"], "success")
        self.engine.stop()                                    # ...and again while the Add waits for agy's window
        self.engine = self.start()
        self.assertEqual((self.holds(), self.block()["enrollment"]["phase"]), ("absent", "restoring"))
        self.ended(window)
        self.assertEqual(self.holds(), ALPHA)
        self.assertFalse(self.engine.vault.path(AGY_ENROLLMENT).exists())
        self.assertEqual(self.saved_email(self.alpha), ALPHA)

    def test_an_add_saves_the_sign_in_that_lands(self):
        window = self.add()
        self.agy.sign_in(agy_record(BETA, now=self.clock()))     # the user signs in in agy's window
        self.engine.tick()
        beta = self.engine.store.agy_find(BETA)
        self.assertEqual((self.holds(), self.block()["enrollment"]["phase"]), (BETA, "saved"))
        self.assertEqual((self.saved_email(beta["id"]), self.saved_email(self.alpha)), (BETA, ALPHA))
        self.ended(window)                                       # and closes agy, signed in as Beta
        # Sign In Again as someone else: that sign-in is saved too, and the account asked for keeps its own.
        self.add(id=self.alpha)
        self.agy.sign_in(agy_record(CAROL, now=self.clock()))
        self.engine.tick()
        self.assertEqual(self.block()["enrollment"]["phase"], "failed")
        self.assertEqual((self.saved_email(self.alpha), self.holds()), (ALPHA, CAROL))
        self.assertIsNotNone(self.engine.store.agy_find(CAROL))


class UsageTests(AgyCase):
    def test_a_reading_is_never_kept_for_the_wrong_account(self):
        self.engine.agy.accept_notice()
        alpha = self.save(ALPHA)

        def changed_meanwhile():                 # the user signs agy in to another account while the usage runs
            self.agy.sign_in(agy_record(BETA, now=self.clock()))
            return agy_usage_output(self.clock())
        self.procs.script_agy_usage(changed_meanwhile)
        self.act("refresh")
        self.engine.sense_active(only="antigravity")
        self.assertIsNone(self.engine.store.agy_account(alpha)["usage"])
        unsaved = next(row for row in self.block()["accounts"] if not row["saved"])
        self.assertEqual((unsaved["email"], unsaved["usage"]), (BETA, None))
        # A run that stays on one account: the reading is that account's, saved or not.
        self.clock.advance(61)
        self.act("refresh")
        unsaved = next(row for row in self.block()["accounts"] if not row["saved"])
        self.assertIsNotNone(unsaved["usage"])
        self.assertIsNone(self.engine.store.agy_account(alpha)["usage"])
        self.sign_in(ALPHA)
        self.clock.advance(61)
        self.act("refresh")
        self.assertIsNotNone(self.engine.store.agy_account(alpha)["usage"])
        self.assertEqual(contract.validate_state(self.engine.snapshot()), [])

    def test_nothing_runs_agy_or_switches_by_itself(self):
        self.engine.agy.accept_notice()
        alpha = self.save(ALPHA)
        beta = self.save(BETA)
        # Something for every automatic part to act on: agy's account has nothing left and the other one has room;
        # the terminals' account is at its 5-hour limit and another one has room.
        store, now = self.engine.store, self.clock()
        store.agy_record_usage(beta, agy_reading(now, {"gemini-5h": 0.0, "gemini-weekly": 0.0}), at=now)
        store.agy_record_usage(alpha, agy_reading(now, {"gemini-5h": 1.0, "gemini-weekly": 1.0}), at=now)
        logins = {name: terminal_login(new_uuid(), f"{name}@example.test", now=now, token=name)
                  for name in ("full", "spare")}
        self.terminal.owners.update({f"refresh-{name}": login[1]["accountUuid"] for name, login in logins.items()})

        def usage(credentials):
            used = 100.0 if credentials["claudeAiOauth"]["refreshToken"] == "refresh-full" else 10.0
            return claude_usage({"five_hour": {"utilization": used, "resets_at": None},
                                 "seven_day": {"utilization": 30.0, "resets_at": "2099-01-01T00:00:00+00:00"}},
                                now=self.clock())

        def renew(credentials, *, timeout=None):     # the token endpoint, for the terminal switch's renewal
            renewed = copy.deepcopy(credentials)
            renewed["claudeAiOauth"]["expiresAt"] = int((self.clock() + 8 * 3600) * 1000)
            return renewed
        self.terminal.usage, self.terminal.refresh = usage, renew
        self.sign_in_terminal(*logins["full"])
        self.engine.sense_active()
        self.save_terminal(self.engine, *logins["spare"])
        self.engine.prefs.update({"cli_autoswitch": True, "agy_usage_on_open": True})
        self.agy.update_binary()                 # a new agy whose version is not known yet
        for tick in range(24 * 12):              # a day, a tick every five minutes, agy renewing hourly
            self.clock.advance(300)
            if tick % 12 == 0:
                self.agy.renew()
            self.engine.tick()
            self.engine.runner.wait(5)
        # Auto-switch did act, for the terminals only.
        self.assertEqual(self.engine.active_identity["claude_cli"], logins["spare"][1]["accountUuid"])
        agy_runs = [call for call in self.procs.calls if call[0] in ("run_agy_usage", "open_agy_signin")]
        self.assertEqual((agy_runs, self.agy.writes, self.holds(), self.agy.version_runs), ([], [], BETA, 1))
        self.assertEqual([event for event in self.engine.store.events() if "Antigravity" in event["message"]], [])
        self.assertEqual(self.named(alpha, beta), set())
        self.engine.looked()                     # the user opens AccountDeck: then, and only then, it reads
        self.assertEqual([call[0] for call in self.procs.calls].count("run_agy_usage"), 1)
        self.assertEqual(self.agy.version_runs, 2)
        self.assertEqual(self.engine.store.agy_find(BETA)["usage"]["at"], self.clock())
        self.assertEqual(self.engine.store.agy_account(alpha)["usage"]["at"], now)


class RestoreTests(AgyCase):
    def test_a_restored_database_keeps_every_saved_sign_in(self):
        self.engine.agy.accept_notice()
        alpha = self.save(ALPHA)
        beta = self.save(BETA)
        # An older database: alpha's row is missing (its record is still in the vault), beta's record is gone.
        self.engine.store.agy_remove(alpha)
        self.engine.vault.remove(agy_key(beta))
        self.assertEqual(self.engine.agy.reconcile_restored(), (1, 1))
        self.assertEqual(self.engine.store.agy_account(alpha)["email"], ALPHA)
        self.assertEqual(self.engine.store.agy_account(beta)["error_code"], "reauth_required")
        rows = {row["id"]: row for row in self.block()["accounts"]}
        self.assertTrue(rows[alpha]["switchable"])
        self.assertFalse(rows[beta]["switchable"])
        # Remove forgets an account here only: agy stays signed in to it.
        self.act("remove", id=beta)
        self.assertEqual(self.finish()["outcome"], "success")
        self.assertEqual((self.engine.store.agy_account(beta), self.holds()), (None, BETA))
