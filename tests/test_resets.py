"""Limit resets (accountdeck/resets.py): a reset is spent only through the plan the user confirmed, never twice for one
click (the same request key while the outcome is not known, across a restart too), only after a last look shows the
row unchanged; Claude's separate read stays within its limits; and no provider id, request key or token ever reaches
the state, the log, the history or an error."""
import copy
import json
import threading
import time

from tests.helpers import SyntheticTestCase, new_uuid, terminal_login

from accountdeck import cli_auto, diagnostics, reset_auto
from accountdeck.core import DeckError
from accountdeck.operations import Operation
from accountdeck.providers.base import (CLAUDE_CODE_VERSION, OAUTH_PROFILE_URL, OAUTH_USAGE_URL, RESETS_QUERY,
                                        USER_AGENT)
from accountdeck.providers.errors import ProviderError
from accountdeck.providers.fake import FakeCodex, ResetLedger
from accountdeck.providers.http import request_headers
from accountdeck.resets import MEMORY
from accountdeck.usage import claude_reset_block, claude_usage

FAR = "2099-01-01T00:00:00+00:00"
# A Codex reading with a covered limit below 80% left, so its reset can be used.
HOT_RATES = {"rateLimits": {"limitId": "codex",
                            "primary": {"usedPercent": 70.0, "windowDurationMins": 300, "resetsAt": None},
                            "secondary": {"usedPercent": 40.0, "windowDurationMins": 10080, "resetsAt": None}}}


def grant(gid="grant_weekly", left=1, **fields):
    return {"id": gid, "type": "weekly", "label": "", "resets_left": left, "resets_total": left, "ends_at": FAR,
            "paused": False, "usable_now": True, "use_requires_limit": False, "clears": ["seven_day"], **fields}


def cedar(*grants, eligible=True, reason=None):
    return {"eligible": eligible, "ineligible_reason": reason, "at_limit": False, "exhausted": [], "grants": list(grants),
            "next_grant_id": None, "cooldown_until": None}


def credits(account, count=1):
    return {"availableCount": count, "credits": [
        {"id": f"RateLimitResetCredit_{account}{index:02d}", "resetType": "codexRateLimits", "status": "available",
         "grantedAt": 1790000000, "expiresAt": 4070908800, "title": "Full reset (Weekly + 5 hr)",
         "description": "Ready to redeem"} for index in range(count)]}


class LimitResetTests(SyntheticTestCase):
    def setUp(self):
        super().setUp()
        self.ledger = ResetLedger()
        self.owners = {}
        self.clock = [time.time()]

    def engine(self, **kwargs):
        codex = FakeCodex(self.paths, [(0, HOT_RATES)], ledger=self.ledger)
        engine = self.make_engine(codex=codex, owners=self.owners, clock=lambda: self.clock[0], **kwargs)
        self.terminal.ledger = self.ledger
        self.codex = codex
        return engine

    def terminal_account(self, engine, label="cli", *grants):
        """A saved terminal account (not the one in use) whose login lasts days, with these grants."""
        identity = new_uuid()
        credentials, account = terminal_login(identity, f"{label}@example.test", now=self.clock[0], token=label)
        credentials["claudeAiOauth"]["expiresAt"] = int((self.clock[0] + 3 * 86400) * 1000)
        self.owners[f"refresh-{label}"] = identity
        self.ledger.claude[identity] = cedar(*(grants or (grant(),)))
        return self.save_terminal(engine, credentials, account), identity, account["organizationUuid"]

    def codex_account(self, engine, count=1):
        """The signed-in Codex account, saved, holding `count` credits."""
        identity = "acct-" + new_uuid().replace("-", "")[:12]
        self.fake_home(codex_identity=identity)
        key = engine.accounts.capture("codex", claude_running=False)
        self.ledger.codex[identity] = credits(identity[-6:], count)
        engine.sense_active()
        return key, identity

    def read(self, engine, key):
        self.assertEqual(engine._refresh(key), "ok")

    def account(self, engine, key):
        return next(a for a in engine.snapshot()["accounts"] if a["id"] == key)

    def finish(self, engine):
        op = engine.runner.current
        if op is not None:
            self.assertTrue(op.wait(10))
        for _ in range(400):                 # `_finished` records the outcome just after the operation ends
            if not engine.runner.busy:
                break
            time.sleep(0.005)
        return engine.runner.snapshot()["report"]

    def use(self, engine, key, choice=None):
        plan = engine.action("reset-plan", {"id": key})["plan"]
        self.assertEqual(plan["blockers"], [], plan["blockers"])
        engine.action("use-reset", {"id": key, "plan_id": plan["plan_id"], "choice": choice or plan["default"]})
        return self.finish(engine)

    def refused(self, engine, body):
        with self.assertRaises(DeckError) as caught:
            engine.action("use-reset", body)
        return caught.exception.code

    # ---- the regressions ----
    def test_a_reset_never_uses_an_unverified_live_terminal_identity(self):
        engine = self.engine()
        cli, expected, org = self.terminal_account(engine, "expected")
        self.read(engine, cli)
        foreign = new_uuid()
        credentials, _ = terminal_login(foreign, "foreign@example.test", now=self.clock[0], token="foreign")
        self.sign_in_terminal(credentials, {"accountUuid": expected, "emailAddress": "expected@example.test",
                                            "organizationUuid": org})
        engine.active_identity["claude_cli"] = expected
        self.ledger.claude[foreign] = cedar(grant())
        for owner in (foreign, None):
            self.owners["refresh-foreign"] = owner
            engine.cli.owners.clear()
            engine.cli.owner_retry.clear()
            self.assertEqual(self.use(engine, cli)["error"]["code"], "identity")
            self.assertEqual(self.ledger.uses, [])
        # A new live token positively resolved to this account is usable; no renewal or vault edit is needed.
        self.owners["refresh-foreign"] = expected
        engine.cli.owner_retry.clear()
        self.assertEqual(self.use(engine, cli)["outcome"], "success")
        self.assertEqual(len(self.ledger.uses), 1)

    def test_claude_app_entries_cannot_spend_separate_resets_for_one_uncertain_request(self):
        for origin in ("claude_cli", "claude"):
            engine = self.engine()
            cli, identity, org = self.terminal_account(engine, origin, grant("shared", 2))
            self.fake_home(claude_identity=identity, claude_org=org, now=self.clock[0])
            engine.providers["claude"].ledger = self.ledger
            engine.providers["claude"].identities[identity] = {"org": org}
            desktop = engine.accounts.capture("claude", claude_running=False)
            engine.sense_active()
            first, second = (cli, desktop) if origin == "claude_cli" else (desktop, cli)
            for key in (first, second):
                self.read(engine, key)
            self.ledger.scripts[identity] = ["lost"]
            self.assertEqual(self.use(engine, first)["error"]["code"], "timeout")
            original = self.ledger.uses[-1][2]
            engine.stop()
            engine = self.engine()
            engine.providers["claude"].ledger = self.ledger
            engine.providers["claude"].identities[identity] = {"org": org}
            for key in (first, second):
                self.assertIsNotNone(self.account(engine, key)["resets"]["doubt"])
            plan = engine.action("reset-plan", {"id": second})["plan"]
            self.assertTrue(plan["blockers"])
            self.assertFalse(self.account(engine, second)["resets"]["can_use"])
            for key in (first, second):
                with self.assertRaises(DeckError):
                    engine.resets.check_remove(engine.store.account(key))
            self.assertEqual(self.use(engine, first)["outcome"], "success")
            sent = [request for who, _, request in self.ledger.uses if who == identity]
            self.assertEqual(sent, [original, original])
            for key in (first, second):
                self.assertEqual(self.account(engine, key)["resets"]["available"], 1)
                self.assertIsNone(self.account(engine, key)["resets"]["doubt"])
            engine.stop()

    def test_a_missing_claude_count_never_clears_an_expired_uncertain_request(self):
        engine = self.engine()
        cli, identity, _ = self.terminal_account(engine, "missing", grant("first", 2), grant("second", 2, type="session"))
        self.read(engine, cli)
        self.ledger.scripts[identity] = ["lost"]
        self.assertEqual(self.use(engine, cli)["error"]["code"], "timeout")
        pending = engine.resets.entry(cli)["pending"]
        self.clock[0] += 601
        for incomplete in ([], [dict(grant("first", 2), resets_left=None)], [{"id": "first"}]):
            parsed = claude_reset_block(cedar(*incomplete, grant("second", 2, type="session")))
            engine.resets._record_read(engine.store.account(cli), "ok", parsed, self.clock[0])
            self.assertEqual(engine.resets.entry(cli)["pending"], pending)
            self.assertIsNotNone(self.account(engine, cli)["resets"]["doubt"])
            self.assertFalse(self.account(engine, cli)["resets"]["can_use"])
        engine.stop()
        engine = self.engine()
        self.assertEqual(engine.resets.entry(cli)["pending"], pending)
        # The matching count returning is evidence; expiry or a missing count alone is not.
        parsed = claude_reset_block(cedar(grant("first", 1)))
        engine.resets._record_read(engine.store.account(cli), "ok", parsed, self.clock[0])
        self.assertNotIn("pending", engine.resets.entry(cli))
        self.assertEqual(len(self.ledger.uses), 1)

    def test_failed_saves_keep_pending_requests_consistent_with_disk(self):
        engine = self.engine()
        cli, identity, _ = self.terminal_account(engine, "disk", grant("weekly", 2))
        self.read(engine, cli)
        original = engine.store.set_setting

        def fail_pending(key, value):
            if key == MEMORY and value.get("accounts", {}).get(cli, {}).get("pending"):
                raise OSError("synthetic write failure before send")
            return original(key, value)

        engine.store.set_setting = fail_pending
        self.assertEqual(self.use(engine, cli)["outcome"], "failed")
        self.assertEqual(self.ledger.uses, [])
        self.assertNotIn("pending", engine.resets.entry(cli))
        engine.store.set_setting = original
        self.ledger.scripts[identity] = ["lost"]
        self.use(engine, cli)
        pending = engine.resets.entry(cli)["pending"]

        def fail_settlement(key, value):
            if key == MEMORY and not value.get("accounts", {}).get(cli, {}).get("pending"):
                raise OSError("synthetic settlement failure")
            return original(key, value)

        engine.store.set_setting = fail_settlement
        try:
            parsed = claude_reset_block(cedar(grant("weekly", 1)))
            with self.assertRaises(OSError):
                engine.resets._record_read(engine.store.account(cli), "ok", parsed, self.clock[0])
            self.assertEqual(engine.resets.entry(cli)["pending"], pending)
            self.assertEqual(self.use(engine, cli)["error"]["code"], "timeout")
            self.assertEqual(engine.resets.entry(cli)["pending"], pending)
            self.assertEqual(engine.store.setting(MEMORY)["accounts"][cli]["pending"], pending)
            self.assertEqual(len({request for _, _, request in self.ledger.uses}), 1)
        finally:
            engine.store.set_setting = original
        self.read(engine, cli)
        self.assertNotIn("pending", engine.resets.entry(cli))

    def test_a_limit_reset_is_used_only_through_the_plan_the_user_confirmed(self):
        engine = self.engine()
        cli, _identity, _org = self.terminal_account(engine, "cli", grant("grant_one", 2))
        codex, _codex_identity = self.codex_account(engine, count=2)
        for key in (cli, codex):
            self.read(engine, key)
            self.assertTrue(self.account(engine, key)["resets"]["can_use"])
        for _ in range(3):                   # nothing automatic ever uses one: ticks, reads, a refresh of everything
            self.clock[0] += 400
            engine.action("refresh", {})
            engine.tick()
        self.assertEqual((self.ledger.uses, self.codex.reset_sessions), ([], []))
        plan = engine.action("reset-plan", {"id": cli})["plan"]
        other = engine.action("reset-plan", {"id": codex})["plan"]
        row = plan["default"]
        self.assertEqual(self.refused(engine, {"id": cli, "choice": row}), "invalid")
        self.assertEqual(self.refused(engine, {"id": cli, "plan_id": "rp_" + "0" * 32, "choice": row}), "stale_plan")
        self.assertEqual(self.refused(engine, {"id": cli, "plan_id": other["plan_id"], "choice": row}), "stale_plan")
        self.assertEqual(self.refused(engine, {"id": cli, "plan_id": plan["plan_id"], "choice": other["default"]}),
                         "stale_plan")
        self.clock[0] += 301                 # the plan's five minutes are over
        self.assertEqual(self.refused(engine, {"id": cli, "plan_id": plan["plan_id"], "choice": row}), "stale_plan")
        self.assertEqual((self.ledger.uses, self.codex.reset_sessions), ([], []))
        self.read(engine, cli)
        plan = engine.action("reset-plan", {"id": cli})["plan"]
        engine.action("use-reset", {"id": cli, "plan_id": plan["plan_id"], "choice": plan["default"]})
        self.assertEqual(self.finish(engine)["outcome"], "success")
        self.assertEqual(self.refused(engine, {"id": cli, "plan_id": plan["plan_id"], "choice": plan["default"]}),
                         "stale_plan")    # one confirmation, one use
        self.assertEqual(len(self.ledger.uses), 1)
        claim = self.terminal.claims[0]
        self.assertEqual((claim["method"], claim["body"]["program"], claim["body"]["grant_id"]),
                         ("POST", "cedar_ember", "grant_one"))
        self.assertEqual(request_headers(claim["url"], token="t").get("anthropic-beta"), "oauth-2025-04-20")
        # Anthropic lists grants only to Claude Code: its name goes on the resets read and the claim, and nowhere else.
        claude_code = f"claude-cli/{CLAUDE_CODE_VERSION} (external, cli)"
        for url in (claim["url"], OAUTH_USAGE_URL + RESETS_QUERY):
            self.assertEqual(request_headers(url, token="t")["User-Agent"], claude_code)
        for url in (OAUTH_USAGE_URL, OAUTH_PROFILE_URL, OAUTH_USAGE_URL + "?cedar_ember=1",
                    "https://claude.ai/api/organizations/" + new_uuid() + "/usage" + RESETS_QUERY,
                    "https://api.anthropic.com/api/organizations/" + new_uuid() + "/usage"):
            self.assertEqual(request_headers(url, token="t")["User-Agent"], USER_AGENT)

    def test_a_use_whose_outcome_is_not_known_sends_the_same_key_again_also_after_a_restart(self):
        engine = self.engine()
        cli, cli_identity, _org = self.terminal_account(engine, "cli")
        codex, codex_identity = self.codex_account(engine)
        for key, identity in ((cli, cli_identity), (codex, codex_identity)):
            self.read(engine, key)
            self.ledger.scripts[identity] = ["lost", "lost"]          # sent, and no answer came back, twice
            for _click in range(2):
                report = self.use(engine, key)
                self.assertEqual((report["outcome"], report["error"]["code"]), ("failed", "timeout"))
                self.assertIsNotNone(self.account(engine, key)["resets"]["doubt"])
        engine.stop()
        engine = self.engine()                                         # a restart of the service
        for key, identity in ((cli, cli_identity), (codex, codex_identity)):
            self.read(engine, key)                                     # the reading still shows the reset: in doubt
            self.assertIsNotNone(self.account(engine, key)["resets"]["doubt"])
            self.assertEqual(self.use(engine, key)["outcome"], "success")
            sent = [use for use in self.ledger.uses if use[0] == identity]
            self.assertEqual(len(sent), 3)
            self.assertEqual(len({request for _who, _target, request in sent}), 1, "a new key for the same reset")
            self.assertIsNone(self.account(engine, key)["resets"]["doubt"])
        self.assertTrue(all(session["refresh_blank"] for session in self.codex.reset_sessions))
        self.assertTrue(all(calls["calls"][0] == ("account/read", {"refreshToken": False})
                            for calls in self.codex.reset_sessions))

    def test_a_second_click_while_the_outcome_is_not_known_never_spends_a_second_reset(self):
        engine = self.engine()
        cli, identity, _org = self.terminal_account(engine, "cli", grant("grant_two", 2))
        other, other_identity, _ = self.terminal_account(engine, "spare", grant("grant_two", 2))
        third, third_identity, _ = self.terminal_account(engine, "third")
        late, late_identity, _ = self.terminal_account(engine, "late", grant("grant_two", 2))
        codex, codex_identity = self.codex_account(engine)
        for key in (cli, other, third, late, codex):
            self.read(engine, key)
        # Claude used the reset and the answer was lost. The second click's own look shows it went through: nothing
        # more is sent, although the account still holds another reset of that kind.
        self.ledger.scripts[identity] = ["timeout"]
        self.assertEqual(self.use(engine, cli)["error"]["code"], "timeout")
        report = self.use(engine, cli)
        self.assertEqual(report["outcome"], "success")
        self.assertTrue(report["summary"].startswith("The earlier "))
        # The same when Claude answers "unavailable": Claude Code reads that as not confirmed, never as "not used".
        self.ledger.scripts[other_identity] = ["unavailable"]
        report = self.use(engine, other)
        self.assertEqual(report["error"]["code"], "timeout")
        self.assertTrue(report["summary"].startswith("It is not known whether"))
        self.assertIsNotNone(self.account(engine, other)["resets"]["doubt"])
        self.assertEqual(self.use(engine, other)["outcome"], "success")
        for who in (identity, other_identity):
            self.assertEqual(len([use for use in self.ledger.uses if use[0] == who]), 1)
            self.assertEqual(self.ledger.claude[who]["grants"][0]["resets_left"], 1)
        # A try that never reaches Anthropic says nothing of the request before it: the key stays for the next one.
        self.ledger.scripts[third_identity] = ["lost", "unreachable"]
        for _click in range(2):
            self.assertEqual(self.use(engine, third)["error"]["code"], "timeout")
            self.assertIsNotNone(self.account(engine, third)["resets"]["doubt"])
        self.assertEqual(self.use(engine, third)["outcome"], "success")
        sent = [request for who, _target, request in self.ledger.uses if who == third_identity]
        self.assertEqual((len(sent), len(set(sent))), (2, 1), "a new key while the outcome was not known")
        # Nor does Anthropic's "no" to a request sent again: the earlier one with that key may just have gone through
        # (here it did). The key stays, the next look shows it, and the second reset of that kind is never sent.
        self.ledger.scripts[late_identity] = ["lost", "cooldown"]
        for _click in range(2):
            report = self.use(engine, late)
            self.assertTrue(report["summary"].startswith("It is not known whether"), report["summary"])
            self.assertIsNotNone(self.account(engine, late)["resets"]["doubt"])
        report = self.use(engine, late)
        self.assertTrue(report["summary"].startswith("The earlier "), report["summary"])
        sent = [request for who, _target, request in self.ledger.uses if who == late_identity]
        self.assertEqual((len(sent), len(set(sent)), self.ledger.claude[late_identity]["grants"][0]["resets_left"]),
                         (2, 1, 1))
        # Codex: an answer that leaves the credits out, or lists none, is no proof that the reset was used.
        self.ledger.scripts[codex_identity] = ["lost"]
        self.assertEqual(self.use(engine, codex)["error"]["code"], "timeout")
        held = copy.deepcopy(self.ledger.codex[codex_identity])
        for answer in ("hidden", {"credits": []}):
            if answer == "hidden":
                self.ledger.hidden.add(codex_identity)
            else:
                self.ledger.hidden.clear()
                self.ledger.codex[codex_identity] = answer
            report = self.use(engine, codex)
            self.assertEqual((report["outcome"], report["error"]["code"]), ("failed", "busy"))
            self.assertIsNotNone(self.account(engine, codex)["resets"]["doubt"])
        self.ledger.codex[codex_identity] = held
        self.assertEqual(self.use(engine, codex)["outcome"], "success")
        sent = [request for who, _target, request in self.ledger.uses if who == codex_identity]
        self.assertEqual((len(sent), len(set(sent))), (2, 1))

    def test_a_damaged_memory_of_the_resets_never_stops_the_state(self):
        engine = self.engine()
        cli, _identity, _org = self.terminal_account(engine, "cli")
        codex, _codex_identity = self.codex_account(engine)
        self.read(engine, cli)
        engine.stop()
        damaged = {"accounts": {cli: "junk", codex: {"claude": ["x"], "pending": {"key": "not-a-key"}},
                                "gone": {"claude": {"checked": 1.0, "tried": 1.0,
                                                    "block": {"eligible": True, "grants": [{"id": "g"}]}}}}}
        for value in (damaged, "junk", {"accounts": []}, {"accounts": {cli: {"claude": {
                "checked": self.clock[0], "tried": self.clock[0], "block": {"eligible": True, "grants": [{"id": "g"}]}}}}}):
            engine = self.engine()
            engine.store.set_setting("limit_resets", value)
            engine.stop()
            engine = self.engine()
            self.assertIsNone(self.account(engine, cli)["resets"]["checked"])  # unknown, never a fabricated count
            self.assertFalse(self.account(engine, cli)["resets"]["can_use"])
            self.clock[0] += 3601
            self.read(engine, cli)                                     # and the next read fills it again
            self.assertEqual(self.account(engine, cli)["resets"]["available"], 1)
            engine.stop()

    def test_the_last_look_refuses_a_reset_that_changed_since_the_plan(self):
        engine = self.engine()
        cli, cli_identity, _org = self.terminal_account(engine, "cli")
        codex, codex_identity = self.codex_account(engine)
        for key in (cli, codex):
            self.read(engine, key)
        plans = {key: engine.action("reset-plan", {"id": key})["plan"] for key in (cli, codex)}
        self.ledger.claude[cli_identity]["grants"][0]["paused"] = True            # meanwhile, at the provider
        self.ledger.codex[codex_identity]["credits"][0]["status"] = "redeemed"     # used in Codex itself
        self.ledger.codex[codex_identity]["availableCount"] = 0
        for key, plan in plans.items():
            engine.action("use-reset", {"id": key, "plan_id": plan["plan_id"], "choice": plan["default"]})
            report = self.finish(engine)
            self.assertEqual((report["outcome"], report["error"]["code"]), ("failed", "stale_plan"))
            self.assertTrue(report["summary"].startswith("No limit reset was used: "))
        self.assertEqual(self.ledger.uses, [])
        self.assertNotIn("account/rateLimitResetCredit/consume",
                         [method for session in self.codex.reset_sessions for method, _ in session["calls"]])

    def test_claudes_resets_read_runs_only_after_a_good_read_with_the_setting_on_and_within_its_limits(self):
        engine = self.engine()
        cli, identity, _org = self.terminal_account(engine, "cli")
        attempts, read_resets = [], self.terminal.read_resets
        self.terminal.read_resets = lambda credentials: (attempts.append(self.clock[0]), read_resets(credentials))[1]
        engine.update_settings({"claude_resets": False})
        self.read(engine, cli)
        self.assertEqual(attempts, [])
        self.assertNotIn("resets", self.account(engine, cli))
        engine.update_settings({"claude_resets": True})
        self.read(engine, cli)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.account(engine, cli)["resets"]["available"], 1)
        self.clock[0] += 1200
        self.read(engine, cli)                                   # not again within the hour
        engine.action("refresh", {"id": cli})                    # asked: 15 minutes are enough, and 20 have passed
        self.read(engine, cli)
        self.assertEqual(len(attempts), 2)
        self.clock[0] += 60
        engine.action("refresh", {"id": cli})
        self.read(engine, cli)                                   # asked again a minute later: not yet
        self.assertEqual(len(attempts), 2)
        usage = self.terminal.usage
        self.terminal.usage = lambda credentials: (_ for _ in ()).throw(ProviderError("network", detail="timeout"))
        self.clock[0] += 7200
        self.assertNotEqual(engine._refresh(cli), "ok")          # no resets read after a failed ordinary read
        self.assertEqual(len(attempts), 2)
        self.terminal.usage = usage
        answer = self.ledger.claude_answer
        self.ledger.claude_answer = lambda who: (_ for _ in ()).throw(ProviderError("token_refused", http=401))
        self.read(engine, cli)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(self.account(engine, cli)["resets"]["available"], 1)    # the earlier answer stays shown
        self.ledger.claude_answer = answer
        self.clock[0] += 5 * 3600                                # a 401: not for 6 hours
        self.read(engine, cli)
        self.assertEqual(len(attempts), 3)
        self.clock[0] += 3600 + 1
        self.read(engine, cli)
        self.assertEqual(len(attempts), 4)
        self.assertEqual(self.ledger.reads, [identity, identity, identity])

    def test_no_reset_id_request_key_organisation_or_token_reaches_the_state_the_log_or_an_error(self):
        diagnostics.setup(self.paths, "debug")
        engine = self.engine()
        cli, cli_identity, org = self.terminal_account(engine, "cli", grant("grant_hidden7"))
        codex, codex_identity = self.codex_account(engine)
        for key, identity in ((cli, cli_identity), (codex, codex_identity)):
            self.read(engine, key)
            self.ledger.scripts[identity] = ["lost"]
            report = self.use(engine, key)
            self.assertEqual(report["error"]["code"], "timeout")
        plans = [engine.action("reset-plan", {"id": key}) for key in (cli, codex)]
        secrets = {org, "grant_hidden7", "access-cli", "refresh-cli"}
        secrets |= {credit["id"] for credit in self.ledger.codex[codex_identity]["credits"]}
        secrets |= {request for _who, _target, request in self.ledger.uses}
        auth = copy.deepcopy(engine.vault.get(codex))["tokens"]
        secrets |= {auth["access_token"], auth["id_token"]}
        texts = [json.dumps(engine.snapshot()), json.dumps(plans), json.dumps(engine.store.events(50)),
                 *(json.dumps(engine.history(key)) for key in (cli, codex))]
        logs = self.paths.data_dir / "logs"
        texts += [path.read_text(encoding="utf-8") for path in logs.glob("*.log*")]
        self.assertGreater(sum(len(text) for text in texts[-1:]), 0)
        for secret in secrets:
            self.assertEqual([index for index, text in enumerate(texts) if secret in text], [],
                             "a reset id, request key, organisation or token leaked")


FABLE = "seven_day_overage_included"       # Claude's name of Fable's weekly limit in a grant's `clears`
DAY = 86400.0


def _in(days):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + days * DAY))


def weekly_grant(gid, days=30, **fields):
    """A weekly reset as Claude gives it today: it refills the week and Fable's limit."""
    return grant(gid, clears=["seven_day", FABLE], ends_at=_in(days), **fields)


def fable_grant(gid, days=30, **fields):
    return grant(gid, clears=[FABLE], gives_back_weekly=True, ends_at=_in(days), **fields)


class _OtherJob(Operation):
    """Any other job, running until `go` is set."""

    def __init__(self):
        super().__init__("capture")
        self.go = threading.Event()

    def run(self):
        self.go.wait(10)


class AutomaticResetTests(SyntheticTestCase):
    """Automatic limit resets (accountdeck/reset_auto.py, the setting `auto_reset`): a reset is spent by itself only
    with the setting on and every terminal account out, on the right account and with the reset that fits, one at a
    time, and never again after one that did not refill."""

    def setUp(self):
        super().setUp()
        self.clock = [time.time()]
        self.ledger, self.owners, self.levels, self.ids, self.stuck = ResetLedger(), {}, {}, {}, False
        self.ends = {}                       # identity -> when its limits reset by themselves (FAR unless set)
        identity = new_uuid()
        self.sign_in_terminal(*self.login("alpha", identity))
        self.engine = self.start()           # saves the login in use by itself
        self.alpha = self.engine.store.find("claude_cli", identity)["id"]
        self.ids[self.alpha] = identity

    def start(self):
        engine = self.make_engine(owners=self.owners, clock=lambda: self.clock[0])
        self.terminal.ledger, self.terminal.usage = self.ledger, self.usage
        return engine

    def login(self, label, identity):
        credentials, account = terminal_login(identity, f"{label}@example.test", now=self.clock[0], token=label)
        credentials["claudeAiOauth"]["expiresAt"] = int((self.clock[0] + 3 * DAY) * 1000)
        self.owners[f"refresh-{label}"] = identity
        self.ledger.claude[identity], self.levels[identity] = cedar(), [30.0, None]
        return credentials, account

    def add(self, label):
        identity = new_uuid()
        key = self.save_terminal(self.engine, *self.login(label, identity))
        self.ids[key] = identity
        return key

    def have(self, key, weekly, fable=None, grants=None):
        """What Claude answers for this account from now on: its weekly and Fable percentages, and its resets."""
        self.levels[self.ids[key]] = [float(weekly), fable]
        if grants is not None:
            self.ledger.claude[self.ids[key]] = cedar(*grants)

    def usage(self, credentials):
        identity = self.terminal._owner(credentials)
        level = self.levels[identity]
        cleared = self.ledger.cleared.pop(identity, ())
        if not self.stuck:                   # a reset that worked: the limits it refills read as unused
            level[0] = 0.0 if "seven_day" in cleared else level[0]
            level[1] = 0.0 if FABLE in cleared and level[1] is not None else level[1]
        ends = self.ends.get(identity, FAR)
        limits = [meter for meter in (
            {"kind": "weekly_all", "percent": level[0], "resets_at": ends},
            {"kind": "weekly_scoped", "percent": level[1], "resets_at": ends, "scope": {"model": {"display_name": "Fable"}}})
            if meter["percent"] is not None]
        return claude_usage({"five_hour": None, "seven_day": {"utilization": level[0], "resets_at": ends}, "limits": limits},
                            now=self.clock[0])

    def read(self, *keys, after=0):
        self.clock[0] += after
        for key in keys or self.ids:
            self.assertEqual(self.engine._refresh(key), "ok")

    def finish(self):
        op = self.engine.runner.current
        if op is not None:
            self.assertTrue(op.wait(10))
        for _ in range(400):                 # `_finished` records the outcome just after the operation ends
            if not self.engine.runner.busy:
                break
            time.sleep(0.005)
        return [target for _who, target, _key in self.ledger.uses]

    def look(self):
        """One look of the automatic reset at the accounts, and the job it started to its end: the resets used so far."""
        reset_auto.tick(self.engine)
        return self.finish()

    def test_a_reset_is_used_by_itself_only_with_the_setting_on_and_every_pool_account_out(self):
        engine = self.engine
        beta, spare, signin = self.add("beta"), self.add("spare"), self.add("signin")
        self.have(self.alpha, 98, grants=[weekly_grant("a_weekly")])
        self.have(beta, 99, grants=[weekly_grant("b_weekly", days=2)])
        self.have(spare, 10)                 # left out of auto-switch by the user: its room is not the terminals'
        engine.store.update(spare, flags={**engine.store.account(spare)["flags"], "excluded": True})
        self.have(signin, 10)                # nor that of an account that needs a new sign-in
        self.read()
        engine.store.update(signin, flags={**engine.store.account(signin)["flags"], "health": "needs_signin"})
        self.assertEqual(self.look(), [])    # off, as it is unless the user turns it on
        engine.update_settings({"auto_reset": True})
        # One account with room, or one whose reading cannot say, and nothing is used.
        self.have(beta, 96)
        self.read(beta)
        self.assertEqual(self.look(), [])
        self.have(beta, 97)
        self.read(beta)
        self.read(self.alpha, after=901)     # beta's reading is now too old to count
        self.assertEqual(self.look(), [])
        self.read(beta)
        # Every account auto-switch could use is at 97% or more: one reset, on the account in use, though beta's
        # expires sooner.
        self.assertEqual(self.look(), ["a_weekly"])
        report = engine.runner.snapshot()["report"]
        self.assertEqual((report["kind"], report["outcome"]), ("use-reset", "success"))
        said = (f"AccountDeck used one Weekly reset on {engine.store.account(self.alpha)['label']} by itself: every "
                "terminal account was at 97% or more.")
        self.assertEqual(engine.store.events(1)[0]["message"], said)
        alert = engine.store.alerts()[0]
        self.assertEqual((alert["kind"], alert["level"], alert["account"], alert["message"]),
                         ("reset_confirmed", "good", self.alpha, said))
        self.assertEqual(engine.snapshot()["providers"]["claude_cli"]["auto_reset"]["last"]["account"], self.alpha)
        # Auto-switch waits for the reading that shows the room, and that reading settles the reset.
        self.assertIn("after its limit reset", cli_auto.paused(engine))
        self.read(self.alpha, after=5)
        self.assertIsNone(cli_auto.paused(engine))
        self.assertEqual(self.look(), ["a_weekly"])
        self.assertFalse(engine.resets.auto()["last"]["open"])
        # Out again within the hour: the next reset waits for the hour to pass.
        self.have(self.alpha, 99, grants=[weekly_grant("a_second")])
        self.read(self.alpha, beta, after=1800)
        self.assertEqual(self.look(), ["a_weekly"])
        self.read(self.alpha, beta, after=1900)
        self.assertEqual(self.look(), ["a_weekly", "a_second"])

    def test_nothing_is_used_on_numbers_a_reset_changed_or_that_change_by_themselves(self):
        engine = self.engine
        beta = self.add("beta")
        self.have(self.alpha, 99, grants=[weekly_grant("a_weekly")])
        self.have(beta, 98, grants=[weekly_grant("b_weekly")])
        self.read()
        # The user's own reset on alpha, and the setting turned on before alpha is read again: its reading is from
        # before that reset and says nothing now. Without this, beta's reset went too.
        plan = engine.action("reset-plan", {"id": self.alpha})["plan"]
        engine.action("use-reset", {"id": self.alpha, "plan_id": plan["plan_id"], "choice": plan["default"]})
        self.assertEqual(self.finish(), ["a_weekly"])
        self.clock[0] += 30
        engine.update_settings({"auto_reset": True})
        self.assertEqual(self.look(), ["a_weekly"])
        self.stuck = True                    # nor does a reading right after it that still says 99%: it may lag
        self.read(self.alpha, after=5)
        self.assertEqual(self.look(), ["a_weekly"])
        self.stuck = False
        # A switch point of 99: auto-switch moves the terminals to no account above 97%, so beta at 98% has no room
        # for them, and the account in use counts from the user's own switch point.
        engine.update_settings({"cli_threshold": 99})
        self.have(self.alpha, 98)
        self.read(after=601)
        self.assertEqual(self.look(), ["a_weekly"])
        self.have(self.alpha, 100)
        self.read(self.alpha, after=1)
        self.assertEqual(self.look(), ["a_weekly", "b_weekly"])
        self.read(beta, after=5)
        self.assertEqual(self.look(), ["a_weekly", "b_weekly"])
        self.assertFalse(engine.resets.auto()["last"]["open"])
        # Out again an hour later, and beta's week ends by itself in five minutes: that is waited for.
        self.have(beta, 99, grants=[weekly_grant("b_second")])
        self.ends[self.ids[beta]] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(self.clock[0] + 3700 + 300))
        self.read(after=3700)
        self.assertEqual(self.look(), ["a_weekly", "b_weekly"])
        # Not soon any more, but Anthropic now says that reset refills Fable's limit only: the last look, which asks
        # again what it refills, uses nothing.
        del self.ends[self.ids[beta]]
        self.read(after=1)
        self.ledger.claude[self.ids[beta]]["grants"][0]["clears"] = [FABLE]
        self.assertEqual(self.look(), ["a_weekly", "b_weekly"])
        self.assertEqual(engine.resets.auto()["failed"]["account"], beta)      # it tried, and stopped at the last look

    def test_the_reset_that_fits_is_used_fable_for_fable_and_another_accounts_when_the_one_in_use_has_none(self):
        engine = self.engine
        engine.update_settings({"auto_reset": True})
        beta, gamma = self.add("beta"), self.add("gamma")
        # Only Fable's limit is out everywhere: the Fable reset is used, not the weekly one that expires sooner.
        self.have(self.alpha, 60, 99, grants=[weekly_grant("a_weekly", days=1), fable_grant("a_fable", left=2)])
        self.have(beta, 50, 98, grants=[weekly_grant("b_weekly", days=20)])
        self.have(gamma, 40, 100, grants=[weekly_grant("c_weekly", days=5)])
        self.read()
        self.assertEqual(self.look(), ["a_fable"])
        self.read(self.alpha, after=5)
        self.assertEqual(self.levels[self.ids[self.alpha]], [60.0, 0.0])
        # The week of the account in use is out, and all it holds is a Fable reset, which does not refill a week:
        # another account's weekly reset is used, the one that expires first, and auto-switch moves the terminals there.
        self.have(self.alpha, 98, 20, grants=[fable_grant("a_fable")])
        self.have(beta, 99, 20)
        self.have(gamma, 97, 20)
        self.read(after=3700)
        self.assertEqual(self.look(), ["a_fable", "c_weekly"])
        self.read(gamma, after=5)
        moved = cli_auto.decision(engine)
        self.assertEqual((moved["action"], moved["target"], moved["kind"]), ("switch", gamma, "threshold"))

    def test_never_a_second_reset_while_a_job_runs_or_an_outcome_is_not_known(self):
        engine = self.engine
        engine.update_settings({"auto_reset": True})
        beta = self.add("beta")
        self.have(self.alpha, 98, grants=[weekly_grant("a_weekly", left=3)])
        self.have(beta, 99, grants=[weekly_grant("b_weekly")])
        self.read()
        job = _OtherJob()
        engine.submit(job)                   # any other job: it is never made to give way
        reset_auto.tick(engine)
        self.assertEqual(self.ledger.uses, [])
        job.go.set()
        self.finish()
        # A use the user started on another account, its outcome not known: nothing is used by itself meanwhile.
        self.ledger.scripts[self.ids[beta]] = ["lost"]
        plan = engine.action("reset-plan", {"id": beta})["plan"]
        engine.action("use-reset", {"id": beta, "plan_id": plan["plan_id"], "choice": plan["default"]})
        self.assertEqual(self.finish(), ["b_weekly"])
        self.assertEqual(self.look(), ["b_weekly"])
        self.read(after=601)                 # the key's time is over and beta's reset is still there: it was not used
        # The automatic request itself goes out and no answer comes back: nothing more is sent, on any account,
        # however long it takes, until a reading says what became of it.
        self.ledger.scripts[self.ids[self.alpha]] = ["lost"]
        self.assertEqual(self.look(), ["b_weekly", "a_weekly"])
        self.assertTrue(engine.resets.auto()["last"]["unsure"])
        for wait in (200, 200, 200, 3601):
            self.read(beta, after=wait)
            self.assertEqual(self.look(), ["b_weekly", "a_weekly"])
        self.read(self.alpha)                # its reset is still there after the key's time: it was not used
        self.assertNotIn("last", engine.resets.auto())
        self.assertEqual(self.look(), ["b_weekly", "a_weekly"])      # and the next try waits its ten minutes
        self.read(after=601)
        self.assertEqual(self.look(), ["b_weekly", "a_weekly", "a_weekly"])
        self.assertEqual(len({key for _who, _target, key in self.ledger.uses}), 3)
        self.assertEqual(self.ledger.claude[self.ids[self.alpha]]["grants"][0]["resets_left"], 2)

    def test_a_reset_that_did_not_refill_stops_automatic_resets_until_the_user_turns_them_on_again(self):
        engine = self.engine
        beta = self.add("beta")
        self.have(self.alpha, 98, grants=[weekly_grant("a_weekly", left=3)])
        self.have(beta, 99, grants=[weekly_grant("b_weekly")])
        self.read()
        self.stuck = True                    # Claude takes the reset and the limit stays where it was
        engine.tick()                        # a whole tick, the setting off: nothing
        self.assertEqual(self.finish(), [])
        engine.update_settings({"auto_reset": True})
        engine.tick()
        self.assertEqual(self.finish(), ["a_weekly"])
        self.read(self.alpha, after=5)       # a reading right after may lag: nothing is decided, and nothing more used
        self.assertEqual(self.look(), ["a_weekly"])
        self.assertNotIn("stopped", engine.resets.auto())
        self.read(after=601)
        self.assertEqual(self.look(), ["a_weekly"])
        self.assertIn("stopped", engine.resets.auto())
        alert = engine.store.alerts()[0]
        self.assertEqual((alert["kind"], alert["title"]), ("limit_reached", "Automatic limit resets stopped"))
        self.assertIsNotNone(engine.snapshot()["providers"]["claude_cli"]["auto_reset"]["stopped"])
        for _ in range(2):                   # for good: hours later, with every account still out
            self.read(after=3601)
            self.assertEqual(self.look(), ["a_weekly"])
        engine.stop()                        # and after a restart
        self.engine = engine = self.start()
        self.read()
        self.assertEqual(self.look(), ["a_weekly"])
        # Off and on again is the user's word to go on.
        engine.update_settings({"auto_reset": False})
        engine.update_settings({"auto_reset": True})
        self.stuck = False
        self.assertEqual(self.look(), ["a_weekly", "a_weekly"])
