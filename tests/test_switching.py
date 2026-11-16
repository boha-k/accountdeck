"""Switch flows end to end against fakes: which account each app is signed in to afterwards, what is kept,
and what a failed switch puts back. These are the paths where a regression switches the wrong account or
loses a sign-in."""
from contextlib import contextmanager
import json
import socket
import threading
import time
import types
import unittest
from unittest import mock

from tests.helpers import CLAUDE_USAGE, FAST, SyntheticTestCase, new_uuid, terminal_login

from accountdeck import activity, cli_accounts, cli_auto, switch_cli
from accountdeck.core import DeckError, atomic_write, json_bytes, recover_transactions
from accountdeck.procs import FakeProcessController, unix_time
from accountdeck.providers import claude_cli
from accountdeck.providers.claude_cli import ClaudeCLI
from accountdeck.providers.errors import ProviderError
from accountdeck.providers.http import register_test_host, request, unregister_test_host
from accountdeck.switch_cli import perform
from accountdeck.usage import claude_usage

LOGIN_DAYS = 20          # what the scripted token endpoint says is left of a renewed login


def weekly(used, now):
    """A terminal usage reading with the weekly limit `used` % used."""
    return claude_usage({**CLAUDE_USAGE, "seven_day": {"utilization": used, "resets_at": "2099-01-01T00:00:00+00:00"}},
                        now=now)


def reach(engine, phase):
    """Waits (at most 5 s) until the running operation is in `phase`."""
    deadline = time.monotonic() + 5
    while (engine.runner.pending() or {}).get("phase") != phase:
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


class TerminalSwitchTests(SyntheticTestCase):
    def setUp(self):
        super().setUp()
        self.alpha, self.beta = new_uuid(), new_uuid()
        self.alpha_login = terminal_login(self.alpha, "alpha@example.test", token="alpha")
        self.beta_login = terminal_login(self.beta, "beta@example.test", token="beta")
        self.sign_in_terminal(*self.alpha_login, shared={"mcpOAuth": {"server": "shared-mcp-login"}})
        owners = {"refresh-alpha": self.alpha, "refresh-beta": self.beta}
        self.engine = self.make_engine(owners=owners)          # saves the signed-in terminal login by itself
        self.alpha_key = self.engine.store.find("claude_cli", self.alpha)["id"]
        self.beta_key = self.save_terminal(self.engine, *self.beta_login)
        # The real renewal against a scripted token endpoint: it renews, or raises `refusal`.
        self.renewals, self.refusal = [], None
        self.terminal.http = self.token_endpoint
        self.terminal.refresh = types.MethodType(ClaudeCLI.refresh, self.terminal)

    def token_endpoint(self, method, url, *, json_body=None, **_):
        self.renewals.append(json_body["refresh_token"])
        if self.refusal:
            raise self.refusal
        token = json_body["refresh_token"] + "-renewed"
        return ({"access_token": "access-" + token, "refresh_token": token, "expires_in": 8 * 3600,
                 "refresh_token_expires_in": LOGIN_DAYS * 86400}, {"status": 200, "class": "2xx"})

    def live(self):
        return (json.loads(self.paths.claude_cli_credentials.read_bytes()),
                json.loads(self.paths.claude_user_config.read_bytes()))

    def live_bytes(self):
        return self.paths.claude_cli_credentials.read_bytes(), self.paths.claude_user_config.read_bytes()

    def test_switch_moves_every_terminal_to_the_target_and_keeps_shared_settings(self):
        plan = self.engine.action("switch-plan", {"id": self.beta_key})["plan"]
        self.assertEqual((plan["provider"], plan["blockers"]), ("claude_cli", []))
        self.engine.action("switch", {"id": self.beta_key, "plan_id": plan["plan_id"]})
        self.assertTrue(self.engine.runner.wait(10))
        self.assertEqual(self.engine.runner.report()["outcome"], "success")
        credentials, config = self.live()
        # The target was renewed for the switch (never the live login), and Claude Code got the renewed login
        # with its end, which the account's row keeps too.
        self.assertEqual(self.renewals, ["refresh-beta"])
        self.assertEqual(credentials["claudeAiOauth"]["refreshToken"], "refresh-beta-renewed")
        ends = credentials["claudeAiOauth"]["refreshTokenExpiresAt"] / 1000
        self.assertAlmostEqual(ends, time.time() + LOGIN_DAYS * 86400, delta=60)
        self.assertEqual(self.engine.store.account(self.beta_key)["flags"]["login_ends_at"], ends)
        self.assertEqual(credentials["mcpOAuth"], {"server": "shared-mcp-login"})     # shared login untouched
        self.assertEqual(config["oauthAccount"]["accountUuid"], self.beta)
        self.assertIn("projects", config)                                             # the rest of .claude.json stays
        self.assertEqual(self.engine.active_identity["claude_cli"], self.beta)
        self.assertEqual(self.engine.vault.get(self.alpha_key)["credentials"]["claudeAiOauth"]["refreshToken"],
                         "refresh-alpha")                                             # the outgoing login is kept
        self.assertEqual(recover_transactions(self.engine.vault), [])

    def test_a_target_login_that_cannot_be_renewed_never_reaches_claude_code(self):
        before = self.live_bytes()
        gamma = self.save_terminal(self.engine, *terminal_login(new_uuid(), "gamma@example.test", token="gamma"))
        delta = self.save_terminal(self.engine, *terminal_login(new_uuid(), "delta@example.test", token="delta"))

        def switch(key, refusal):
            self.refusal = refusal
            with self.assertRaises(DeckError) as failed:
                perform(self.engine.cli, key)
            self.assertEqual(self.live_bytes(), before)           # Claude Code's login is untouched
            return failed.exception.code
        # A dead login (invalid_grant) fails the switch and needs sign-in.
        self.assertEqual(switch(self.beta_key, ProviderError("network", http=400, detail="invalid_grant", meta={
            "status": 400, "class": "4xx", "error_description": "Refresh token expired"})), "reauth_required")
        self.assertEqual(self.engine.vault.get(self.beta_key)["health"], "needs_signin")
        # An account on hold is no dead login: no sign-in is asked for, it is not switched to, and a read
        # that works ends the hold.
        self.assertEqual(switch(gamma, ProviderError("network", http=403, meta={
            "status": 403, "class": "4xx", "error_description": "account_on_hold"})), "unsupported")
        row = self.engine.store.account(gamma)
        self.assertEqual((row["flags"]["health"], row["credential_state"]), ("on_hold", "fresh"))
        self.assertEqual(switch(gamma, None), "unsupported")
        self.assertEqual(self.engine.cli.read(gamma), "ok")
        self.assertEqual(self.engine.vault.get(gamma)["health"], "ok")
        # A renewal without an answer may have used the login up: the switch fails, the login is never sent
        # again and asks for sign-in; while the endpoint is silent, a switch goes ahead on a valid login unrenewed.
        self.assertEqual(switch(delta, ProviderError("network", detail="timeout", meta={"class": "timeout"})), "timeout")
        self.refusal, sent = None, len(self.renewals)
        perform(self.engine.cli, gamma)
        self.assertEqual(self.live()[0]["claudeAiOauth"]["refreshToken"], "refresh-gamma")
        self.assertEqual(self.engine.cli.read(delta), "reauth_required")
        self.assertEqual(len(self.renewals), sent)
        self.assertEqual(recover_transactions(self.engine.vault), [])
        # Nor is one sent that waited for Claude Code's locks while the renewal before it got no answer.
        real_locks = cli_accounts.credential_locks

        @contextmanager
        def behind_a_silent_renewal(*args, **kwargs):
            with real_locks(*args, **kwargs) as check:
                self.engine.cli._heard(time.time(), answered=False)
                yield check
        zeta = self.save_terminal(self.engine, *terminal_login(new_uuid(), "zeta@example.test",
                                                               now=time.time() - 9 * 3600, token="zeta"))
        self.engine.cli.quiet = {"until": 0.0, "count": 0}                  # the endpoint answered again since
        with mock.patch.object(cli_accounts, "credential_locks", behind_a_silent_renewal):
            self.assertEqual(self.engine.cli.read(zeta), "network")
        self.assertEqual(len(self.renewals), sent)
        # A renewal whose connection never opened (the Mac woke before its network was up) used nothing up: its
        # login stays usable and the endpoint counts as silent. Through the real HTTP client, to a loopback port
        # nothing listens on; a request that something took before hanging up may have been used.
        closed, endpoint = socket.socket(), socket.socket()
        hosts = []
        for sock in (closed, endpoint):
            self.addCleanup(sock.close)
            sock.bind(("127.0.0.1", 0))
            hosts.append("127.0.0.1:%d" % sock.getsockname()[1])
            register_test_host(hosts[-1])
            self.addCleanup(unregister_test_host, hosts[-1])
        closed.close()
        endpoint.listen(1)
        self.terminal.http, self.engine.cli.quiet = request, {"until": 0.0, "count": 0}
        with mock.patch.object(claude_cli, "TOKEN_URL", f"http://{hosts[0]}/v1/oauth/token"):
            self.assertEqual(self.engine.cli.read(zeta), "network")
        self.engine.cli.adopt_stashes()                    # nothing is left that asks for sign-in, after a restart too
        self.assertEqual((self.engine.vault.get(zeta)["health"], self.engine.cli.quiet["count"]), ("ok", 1))

        def take_and_hang_up():
            connection, _ = endpoint.accept()
            with connection:
                connection.recv(65536)
        threading.Thread(target=take_and_hang_up, daemon=True).start()
        with self.assertRaises(ProviderError) as received:
            request("POST", f"http://{hosts[1]}/v1/oauth/token", json_body={"refresh_token": "taken"}, timeout=5)
        self.assertFalse(received.exception.meta["unsent"])

    def test_two_refused_reads_in_a_row_mark_the_live_login_dead(self):
        usage = claude_usage(CLAUDE_USAGE, now=time.time())
        refused = ProviderError("token_refused", http=401, meta={"status": 401, "class": "4xx"})
        outcomes, now = [], [time.time()]
        self.engine.cli.clock = lambda: now[0]
        with mock.patch.object(self.terminal, "usage", side_effect=[refused, usage, refused, refused, refused, usage]):
            for later in (0, 0, 0, 60, cli_accounts.REFUSAL_GAP - 60, 0):      # seconds after the read before
                now[0] += later
                outcomes.append(self.engine.cli.read(self.alpha_key))
                outcomes.append((self.engine.store.account(self.alpha_key)["flags"]["health"],
                                 self.engine.vault.get(self.alpha_key)["health"]))
        self.assertEqual(outcomes, [
            "login_needed", ("ok", "ok"), "ok", ("ok", "ok"),               # one refusal is not enough
            "login_needed", ("ok", "ok"),
            "login_needed", ("ok", "ok"),                                    # nor a second within a minute (a glitch)
            "reauth_required", ("needs_signin", "needs_signin"),             # one REFUSAL_GAP after the first
            "ok", ("ok", "ok")])                                             # a read that works undoes it
        self.assertEqual(self.renewals, [])                                  # the live login is never renewed

    def sign_in_again(self, credentials):
        """Signs alpha in through the Add terminal account window."""
        self.engine.action("add", {"provider": "claude_cli"})
        folder = self.engine.cli.enrollment["folder"]
        atomic_write(folder / ".credentials.json", json_bytes(credentials))
        atomic_write(folder / ".claude.json", json_bytes({"oauthAccount": self.alpha_login[1]}))
        self.engine.cli.enrollment["process"]["exited"] = True
        self.engine.cli.poll_enrollment()

    @mock.patch.object(cli_accounts, "REFUSAL_GAP", 0.0)      # which login refusals mark, not how far apart they are
    def test_a_refused_login_never_marks_or_replaces_a_newer_saved_one(self):
        cli = self.engine.cli
        refused = ProviderError("token_refused", http=401, meta={"status": 401, "class": "4xx"})
        self.terminal.owners.update({"refresh-alpha-2": self.alpha, "refresh-alpha-3": self.alpha})

        def saved():
            record = self.engine.vault.get(self.alpha_key)
            return (record["credentials"]["claudeAiOauth"]["refreshToken"], record["health"],
                    self.engine.store.account(self.alpha_key)["flags"]["health"])

        # Claude Code renews its login while the second refused read is on its way: that refusal was of a login it
        # no longer holds, and marks nothing (the tick then saves the renewed login).
        calls = []

        def renewed_by_the_second(credentials):
            calls.append(credentials)
            if len(calls) == 2:
                self.sign_in_terminal(terminal_login(self.alpha, "", now=time.time() + 60, token="alpha-2")[0],
                                      self.alpha_login[1])
            raise refused
        with mock.patch.object(self.terminal, "usage", side_effect=renewed_by_the_second):
            cli.read(self.alpha_key)
            cli.read(self.alpha_key)
        self.assertEqual(saved(), ("refresh-alpha", "ok", "ok"))
        cli.mirror()
        self.assertEqual(saved(), ("refresh-alpha-2", "ok", "ok"))
        # Two refusals in a row of the login it holds do mark it; the owner then signs in again while Claude Code
        # keeps the dead login. Its refusals neither mark the new login nor get the dead one saved over it ...
        with mock.patch.object(self.terminal, "usage", side_effect=refused):
            cli.read(self.alpha_key)
            cli.read(self.alpha_key)
            self.assertEqual(saved(), ("refresh-alpha-2", "needs_signin", "needs_signin"))
            self.sign_in_again(terminal_login(self.alpha, "", now=time.time() + 120, token="alpha-3")[0])
            for _ in range(3):
                cli.read(self.alpha_key)
                cli.mirror()
        self.assertEqual(saved(), ("refresh-alpha-3", "ok", "ok"))
        # ... nor does Claude Code clearing its login once its own renewal is refused. Only the row says the terminals
        # lost their login, which moves them off; the account's next read clears it, and it is a target again.
        atomic_write(self.paths.claude_cli_credentials, json_bytes({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}}))
        cli.mirror()
        self.assertEqual(saved(), ("refresh-alpha-3", "ok", "needs_signin"))
        self.assertEqual(self.renewals, [])
        now = time.time()
        for key, used in ((self.alpha_key, 20), (self.beta_key, 10)):
            self.engine.store.observe(key, weekly(used, now), at=now)
        self.engine.sense_active()
        self.assertEqual(cli_auto.decision(self.engine)["target"], self.beta_key)
        perform(cli, self.beta_key)
        self.assertEqual(saved(), ("refresh-alpha-3", "ok", "needs_signin"))
        self.assertEqual(cli.read(self.alpha_key), "ok")
        self.assertEqual(saved(), ("refresh-alpha-3", "ok", "ok"))
        self.assertTrue(self.engine.store.account(self.alpha_key)["flags"]["restorable"])
        self.assertEqual(self.engine.action("switch-plan", {"id": self.alpha_key})["plan"]["blockers"], [])
        perform(cli, self.alpha_key)
        self.assertEqual(self.live()[0]["claudeAiOauth"]["refreshToken"], "refresh-alpha-3-renewed")

    def test_signing_in_again_to_the_account_in_use_lasts_until_the_next_switch_to_it(self):
        cli, day = self.engine.cli, 86400
        self.terminal.owners.update({f"refresh-{n}": self.alpha for n in ("A", "A2", "B", "C", "C2", "D", "D2")})

        def login(token, *, access, ends):
            credentials = terminal_login(self.alpha, "", token=token)[0]
            credentials["claudeAiOauth"].update(expiresAt=int((time.time() + access) * 1000),
                                                refreshTokenExpiresAt=int((time.time() + ends) * 1000))
            return credentials

        def saved():
            return self.engine.vault.get(self.alpha_key)["credentials"]["claudeAiOauth"]["refreshToken"]
        # The terminals' login of alpha ends in two days; the owner signs alpha in again (30 days) in AccountDeck.
        self.sign_in_terminal(login("A", access=2 * 3600, ends=2 * day), self.alpha_login[1])
        cli.mirror()
        self.sign_in_again(login("B", access=8 * 3600, ends=30 * day))
        # Claude Code renews its own login, which keeps its end (its access token now outlasts the sign-in's): the
        # sign-in stays saved, and alpha's row keeps the end of the login the terminals use, to move them off in time.
        self.sign_in_terminal(login("A2", access=9 * 3600, ends=2 * day), self.alpha_login[1])
        cli.mirror()
        self.assertEqual(cli.read(self.alpha_key), "ok")
        self.assertEqual(saved(), "refresh-B")
        ends = self.engine.store.account(self.alpha_key)["flags"]["login_ends_at"]
        self.assertAlmostEqual(ends, time.time() + 2 * day, delta=60)
        # The next switch to alpha gives the terminals the sign-in, however long after: its access token has expired
        # by then, but that read confirmed whose it is, so it can be renewed.
        perform(cli, self.beta_key)
        self.assertEqual(saved(), "refresh-B")
        record = self.engine.vault.get(self.alpha_key)
        record["credentials"]["claudeAiOauth"]["expiresAt"] = int((time.time() - 60) * 1000)
        self.engine.vault.put(self.alpha_key, record)
        perform(cli, self.alpha_key)
        self.assertEqual(self.live()[0]["claudeAiOauth"]["refreshToken"], "refresh-B-renewed")
        # A sign-in handed over as it is (the endpoint asks to wait) is Claude Code's from then on: its renewals are
        # saved, even with an end before the 30 days it assumed at sign-in.
        perform(cli, self.beta_key)
        self.sign_in_again(login("C", access=8 * 3600, ends=30 * day))
        self.refusal = ProviderError("rate_limited", http=429, meta={"status": 429, "class": "4xx"})
        perform(cli, self.alpha_key)
        self.assertEqual(self.live()[0]["claudeAiOauth"]["refreshToken"], "refresh-C")
        self.sign_in_terminal(login("C2", access=9 * 3600, ends=5 * day), self.alpha_login[1])
        cli.mirror()
        self.assertEqual(saved(), "refresh-C2")
        # So is a sign-in a rollback puts back as the newest login (restore_login), with no switch to hand it over.
        signed_in = login("D", access=10 * 3600, ends=30 * day)
        self.sign_in_again(signed_in)
        self.sign_in_terminal(signed_in, self.alpha_login[1])
        cli.mirror()
        self.sign_in_terminal(login("D2", access=11 * 3600, ends=5 * day), self.alpha_login[1])
        cli.mirror()
        self.assertEqual(saved(), "refresh-D2")

    def test_a_switch_that_cannot_be_verified_puts_the_previous_login_back(self):
        before = (self.paths.claude_cli_credentials.read_bytes(), self.paths.claude_user_config.read_bytes())
        real = self.engine.cli.adapter.current

        def unverifiable():
            found = real()
            return {**found, "identity": new_uuid()} if found["identity"] == self.beta else found
        with mock.patch.object(self.engine.cli.adapter, "current", side_effect=unverifiable):
            with self.assertRaises(DeckError) as failed:
                perform(self.engine.cli, self.beta_key)
        self.assertEqual(failed.exception.code, "identity")
        self.assertEqual((self.paths.claude_cli_credentials.read_bytes(), self.paths.claude_user_config.read_bytes()),
                         before)
        self.assertEqual(recover_transactions(self.engine.vault), [])

    def test_a_sign_in_caught_half_way_is_never_saved_under_the_account_it_replaces(self):
        # /login as gamma wrote gamma's token, and `.claude.json` still names alpha when a switch runs.
        gamma = new_uuid()
        self.terminal.owners["refresh-gamma"] = gamma
        atomic_write(self.paths.claude_cli_credentials,
                     json_bytes(terminal_login(gamma, "gamma@example.test", token="gamma")[0]))
        before = self.live_bytes()
        with self.assertRaises(DeckError) as refused:
            perform(self.engine.cli, self.beta_key)
        self.assertEqual(refused.exception.code, "identity")
        self.assertEqual(self.live_bytes(), before)             # gamma's sign-in stays where it is
        self.assertEqual(self.engine.vault.get(self.alpha_key)["credentials"]["claudeAiOauth"]["refreshToken"],
                         "refresh-alpha")                       # and alpha keeps its own login

    def test_rollback_keeps_a_login_claude_code_wrote_after_the_switch(self):
        real = self.engine.cli.adapter.current
        # Claude Code renews the swapped-in login (a later end, the shared entries kept); it is beta's.
        login = terminal_login(self.beta, "beta@example.test", now=time.time() + 3600, token="by-claude-code")[0]
        renewed = json_bytes({**login, "mcpOAuth": {"server": "shared-mcp-login"}})
        self.terminal.owners["refresh-by-claude-code"] = self.beta

        def renewed_then_unverifiable():
            found = real()
            if found["identity"] != self.beta:
                return found
            self.paths.claude_cli_credentials.write_bytes(renewed)      # Claude Code renews the swapped-in login
            return {**found, "identity": new_uuid()}
        with mock.patch.object(self.engine.cli.adapter, "current", side_effect=renewed_then_unverifiable):
            with self.assertRaises(DeckError) as failed:
                perform(self.engine.cli, self.beta_key)
        self.assertEqual(failed.exception.code, "recovery")
        self.assertEqual(self.paths.claude_cli_credentials.read_bytes(), renewed)   # the later change is not undone
        self.assertEqual(self.live()[1]["oauthAccount"]["accountUuid"], self.beta)  # nor the account it belongs to
        pending = recover_transactions(self.engine.vault)
        self.assertEqual(len(pending), 1)                                           # its encrypted backup is kept
        self.assertEqual(self.engine.vault.get(pending[0])["status"], "conflict")
        # Recover puts nothing back here, yet alpha's login (its access token expired) is not renewed until the record
        # is closed: Claude Code may hold that account's login again meanwhile.
        record = self.engine.vault.get(self.alpha_key)
        record["credentials"]["claudeAiOauth"]["expiresAt"] = int((time.time() - 60) * 1000)
        self.engine.vault.put(self.alpha_key, record)
        self.assertEqual(self.engine.cli.read(self.alpha_key), "busy")
        # Recover closes it, leaving Claude Code's login as it is; the mirror then saves that newer login as beta's.
        self.engine.action("recover", {})
        self.assertEqual((recover_transactions(self.engine.vault), self.engine.recovery), ([], []))
        self.assertEqual(self.engine.vault.get(pending[0])["status"], "kept")
        self.assertEqual(self.paths.claude_cli_credentials.read_bytes(), renewed)
        self.engine.sense_active()
        self.assertEqual(self.engine.vault.get(self.beta_key)["credentials"]["claudeAiOauth"]["refreshToken"],
                         "refresh-by-claude-code")
        self.assertEqual(self.engine.cli.read(self.alpha_key), "ok")                # renewals go on
        # Switches go on: back to alpha, its renewed login; beta keeps the login Claude Code renewed.
        self.assertEqual(self.engine.action("switch-plan", {"id": self.alpha_key})["plan"]["blockers"], [])
        perform(self.engine.cli, self.alpha_key)
        credentials, config = self.live()
        self.assertEqual((credentials["claudeAiOauth"]["refreshToken"], config["oauthAccount"]["accountUuid"]),
                         ("refresh-alpha-renewed-renewed", self.alpha))
        self.assertEqual(self.engine.vault.get(self.beta_key)["credentials"]["claudeAiOauth"]["refreshToken"],
                         "refresh-by-claude-code")
        self.assertEqual(self.renewals, ["refresh-beta", "refresh-alpha", "refresh-alpha-renewed"])

    def test_recover_never_puts_a_spent_login_back(self):
        # A switch that cannot be verified, and whose rollback cannot get Claude Code's locks, waits for Recover.
        real_current, real_locks = self.engine.cli.adapter.current, switch_cli.credential_locks

        def unverifiable():
            found = real_current()
            return {**found, "identity": new_uuid()} if found["identity"] == self.beta else found

        def locks(*args, **kwargs):
            locks.calls += 1
            if locks.calls > 1:
                raise DeckError("Claude Code is renewing its login — try again in a few seconds", "busy")
            return real_locks(*args, **kwargs)
        locks.calls = 0
        with mock.patch.object(self.engine.cli.adapter, "current", side_effect=unverifiable), \
                mock.patch.object(switch_cli, "credential_locks", side_effect=locks):
            with self.assertRaises(DeckError):
                perform(self.engine.cli, self.beta_key)
        self.assertEqual(len(self.engine.recovery), 1)
        self.assertEqual(self.live()[1]["oauthAccount"]["accountUuid"], self.beta)
        # Hours later the outgoing account's access token has expired. It is not renewed while Recover waits:
        # Recover puts that login back.
        record = self.engine.vault.get(self.alpha_key)
        record["credentials"]["claudeAiOauth"]["expiresAt"] = int((time.time() - 60) * 1000)
        self.engine.vault.put(self.alpha_key, record)
        self.assertEqual(self.engine.cli.read(self.alpha_key), "busy")
        self.assertEqual(self.renewals, ["refresh-beta"])                  # only the switch's own target renewal
        # A record that cannot be read may be that switch's, so it holds every login's renewal too.
        pending, self.engine.recovery = self.engine.recovery, ["unreadable"]
        self.assertEqual(self.engine.cli.read(self.alpha_key), "busy")
        self.assertEqual(self.renewals, ["refresh-beta"])
        # Renewed all the same (as builds before this one did), the spent login never goes back: Recover gives
        # Claude Code the account's renewed login, with the shared entries it had.
        self.engine.recovery = []
        self.assertEqual(self.engine.cli.read(self.alpha_key), "ok")
        self.engine.recovery = pending
        self.engine.action("recover", {})
        credentials, config = self.live()
        self.assertEqual(credentials["claudeAiOauth"]["refreshToken"], "refresh-alpha-renewed")
        self.assertEqual(credentials["mcpOAuth"], {"server": "shared-mcp-login"})
        self.assertEqual(config["oauthAccount"]["accountUuid"], self.alpha)
        self.assertEqual((recover_transactions(self.engine.vault), self.engine.recovery), ([], []))

    def test_a_needed_switch_is_held_neither_by_a_waiting_codex_switch_nor_an_open_enrollment(self):
        engine = self.engine
        engine.action("add", {"provider": "claude_cli"})      # the Add terminal account window stays open
        folder = engine.cli.enrollment["folder"]
        # A Codex switch set to "Wait" waits for a Codex session that stays open.
        self.fake_home()
        codex_key = engine.accounts.capture("codex")
        self.fake_home()
        engine.sense_active()
        auth = (self.paths.codex_home / "auth.json").read_bytes()
        engine.procs.add_codex("codex_tui")
        plan = engine.action("switch-plan", {"id": codex_key})["plan"]
        engine.action("switch", {"id": codex_key, "plan_id": plan["plan_id"], "consent": "idle"})
        self.assertTrue(reach(engine, "waiting_idle"))
        # Alpha, in use, reaches the threshold; beta has room by a fresh reading.
        now = time.time()
        engine.store.observe(self.alpha_key, weekly(96, now), at=now)
        engine.store.observe(self.beta_key, weekly(20, now), at=now)
        self.assertEqual(cli_auto.state(engine)["hold"], "Waiting for the Codex switch — a needed terminal switch cancels it")
        cli_auto.tick(engine)
        # The Codex switch, which had changed nothing, is cancelled and the owner told; the next tick switches.
        self.assertTrue(engine.runner.wait(5))
        report = engine.runner.report()
        self.assertEqual((report["provider"], report["outcome"], report["summary"]), ("codex", "cancelled", cli_auto.MADE_WAY))
        self.assertEqual((self.paths.codex_home / "auth.json").read_bytes(), auth)
        self.assertTrue(any("cancelled so your terminals could change accounts" in n["message"] for n in engine.notices))
        cli_auto.tick(engine)
        self.assertTrue(engine.runner.wait(10))
        self.assertEqual((engine.runner.report()["provider"], engine.runner.report()["outcome"]), ("claude_cli", "success"))
        self.assertEqual(self.live()[1]["oauthAccount"]["accountUuid"], self.beta)
        # The enrollment then finishes as usual: its login is saved as signed in, and the live login stays beta's.
        gamma = new_uuid()
        credentials, account = terminal_login(gamma, "gamma@example.test", token="gamma")
        atomic_write(folder / ".credentials.json", json_bytes(credentials))
        atomic_write(folder / ".claude.json", json_bytes({"oauthAccount": account}))
        engine.cli.enrollment["process"]["exited"] = True
        engine.cli.poll_enrollment()
        saved = engine.vault.get(engine.store.find("claude_cli", gamma)["id"])
        self.assertEqual(saved["credentials"]["claudeAiOauth"]["refreshToken"], "refresh-gamma")
        self.assertEqual(self.live()[0]["claudeAiOauth"]["refreshToken"], "refresh-beta-renewed")
        self.assertEqual((engine.cli.enrollment, recover_transactions(engine.vault)), (None, []))

    def test_a_sign_in_on_the_account_in_use_goes_live_at_once_and_never_over_a_newer_login(self):
        engine, cli, day = self.engine, self.engine.cli, 86400
        refused = ProviderError("token_refused", http=401, meta={"status": 401, "class": "4xx"})
        self.terminal.owners.update({f"refresh-{n}": self.alpha for n in ("A", "B", "B2", "C", "D", "E", "F")})

        def login(token, *, access=8 * 3600, ends=30 * day):
            credentials = terminal_login(self.alpha, "", token=token)[0]
            credentials["claudeAiOauth"].update(expiresAt=int((time.time() + access) * 1000),
                                                refreshTokenExpiresAt=int((time.time() + ends) * 1000))
            return credentials

        def tick():
            """What the tick does about the terminals' login: the mirror looks, auto-switch acts on what it found."""
            engine.sense_active()
            cli_auto.tick(engine)
            self.assertTrue(engine.runner.wait(5))
            self.assertEqual(recover_transactions(engine.vault), [])

        def live():
            return self.live()[0]["claudeAiOauth"]["refreshToken"]

        def saved():
            return engine.vault.get(self.alpha_key)["credentials"]["claudeAiOauth"]["refreshToken"]
        # The terminals' login ends in two days and the owner signs in again: it reaches them on the next tick, with
        # the shared entries they had. No refresh token goes anywhere, and the row says what is live now.
        self.sign_in_terminal(login("A", ends=2 * day), self.alpha_login[1], shared={"mcpOAuth": {"server": "kept"}})
        cli.mirror()
        self.sign_in_again(login("B"))
        self.assertEqual(live(), "refresh-A")
        tick()
        self.assertEqual((live(), self.live()[0]["mcpOAuth"], saved()), ("refresh-B", {"server": "kept"}, "refresh-B"))
        self.assertFalse(cli_accounts.unheld(engine.vault.get(self.alpha_key)))
        row = engine.store.account(self.alpha_key)
        self.assertAlmostEqual(row["flags"]["login_ends_at"], time.time() + 30 * day, delta=60)
        self.assertEqual((row["error"], row["credential_state"]), (None, "fresh"))
        self.assertEqual(self.renewals, [])
        # Claude Code renews it: the newer live login is saved and stays, and so does the same login.
        renewed = json_bytes(login("B2", access=9 * 3600))
        atomic_write(self.paths.claude_cli_credentials, renewed)
        tick()
        tick()
        self.assertEqual((self.paths.claude_cli_credentials.read_bytes(), saved()), (renewed, "refresh-B2"))
        # A sign-in older than what Claude Code holds meanwhile (its end is no earlier) never replaces it.
        self.sign_in_again(login("C", access=8 * 3600))
        atomic_write(self.paths.claude_cli_credentials, renewed)
        with mock.patch.object(switch_cli, "credential_locks", side_effect=AssertionError("nothing to write")):
            cli.mirror()
            self.assertIsNone(cli.repair_wanted)
        self.assertEqual(self.paths.claude_cli_credentials.read_bytes(), renewed)
        # A login Claude refuses twice gives way to a sign-in saved meanwhile (here the live one is no older, and
        # nobody can say whose it is); one refusal is not enough.
        self.sign_in_again(login("D"))
        atomic_write(self.paths.claude_cli_credentials, json_bytes(login("B3", access=9 * 3600)))
        with mock.patch.object(self.terminal, "usage", side_effect=refused), mock.patch.object(cli_accounts, "REFUSAL_GAP", 0.0):
            cli.mirror()
            self.assertEqual(cli.read(self.alpha_key), "login_needed")
            tick()
            self.assertEqual(live(), "refresh-B3")
            cli.read(self.alpha_key)
            tick()
        self.assertEqual((live(), saved()), ("refresh-D", "refresh-D"))
        # Claude Code renews it, and Claude refuses that login twice too: the saved one is what it renewed past, spent,
        # so it never goes back in, and the record asks for a sign-in instead.
        atomic_write(self.paths.claude_cli_credentials, json_bytes(login("D2", access=9 * 3600)))
        with mock.patch.object(self.terminal, "usage", side_effect=refused), mock.patch.object(cli_accounts, "REFUSAL_GAP", 0.0):
            for _ in range(2):
                cli.mirror()
                cli.read(self.alpha_key)
        cli.mirror()
        self.assertIsNone(cli.repair_wanted)
        self.assertEqual((live(), saved(), engine.vault.get(self.alpha_key)["health"]), ("refresh-D2", "refresh-D", "needs_signin"))
        # Claude Code clears its login: what it last held is all that is saved, so nothing goes in and it asks for
        # a sign-in. A sign-in saved then goes in by itself.
        wiped = json_bytes({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}})
        atomic_write(self.paths.claude_cli_credentials, wiped)
        engine.sense_active()
        self.assertIsNone(cli.repair_wanted)
        self.assertEqual(self.paths.claude_cli_credentials.read_bytes(), wiped)
        self.assertEqual(engine.vault.get(self.alpha_key)["health"], "needs_signin")
        self.sign_in_again(login("C"))
        tick()
        self.assertEqual((live(), self.live()[1]["oauthAccount"]["accountUuid"], self.renewals), ("refresh-C", self.alpha, []))
        self.assertEqual(engine.store.account(self.alpha_key)["flags"]["health"], "ok")
        # Claude Code renews its own login just as the repair takes its locks: what it holds then decides.
        self.sign_in_again(login("E"))
        engine.sense_active()
        self.assertEqual(cli.repair_wanted, (self.alpha_key, "older"))
        newer, real_locks = json_bytes(login("F", access=11 * 3600)), switch_cli.credential_locks

        def renewed_first(*args, **kwargs):
            atomic_write(self.paths.claude_cli_credentials, newer)
            return real_locks(*args, **kwargs)
        with mock.patch.object(switch_cli, "credential_locks", side_effect=renewed_first):
            cli_auto.tick(engine)
            self.assertTrue(engine.runner.wait(5))
        self.assertEqual(self.paths.claude_cli_credentials.read_bytes(), newer)

    def test_an_interrupted_switch_settles_by_itself_and_keeps_a_newer_login(self):
        real_locks = switch_cli.credential_locks

        def interrupted(after=None):
            """A switch to beta that cannot be verified, and whose rollback cannot get Claude Code's locks."""
            real_current = self.engine.cli.adapter.current

            def unverifiable():
                found = real_current()
                if found["identity"] != self.beta:
                    return found
                if after:
                    after()
                return {**found, "identity": new_uuid()}

            def locks(*args, **kwargs):
                locks.calls += 1
                if locks.calls > 1:
                    raise DeckError("Claude Code is renewing its login — try again in a few seconds", "busy")
                return real_locks(*args, **kwargs)
            locks.calls = 0
            with mock.patch.object(self.engine.cli.adapter, "current", side_effect=unverifiable), \
                    mock.patch.object(switch_cli, "credential_locks", side_effect=locks):
                with self.assertRaises(DeckError):
                    perform(self.engine.cli, self.beta_key)
            self.assertEqual(len(self.engine.recovery), 1)

        def started():
            """A new service start: the first tick does what Recover does, and renews nothing for it (a test login
            must never be renewed, FakeTerminal)."""
            self.engine = self.make_engine(owners=self.terminal.owners)
            self.engine.tick()
            self.terminal.http, self.terminal.refresh = self.token_endpoint, types.MethodType(ClaudeCLI.refresh, self.terminal)
        # Nothing changed since: it is put back, and nothing is renewed for it.
        before = self.live_bytes()
        interrupted()
        self.assertEqual(self.live()[1]["oauthAccount"]["accountUuid"], self.beta)
        started()
        self.assertEqual((self.engine.recovery, self.live_bytes()), ([], before))
        # Claude Code renewed the login it was given: that newer login stays and is saved as beta's.
        by_claude_code = json_bytes(terminal_login(self.beta, "", now=time.time() + 3600, token="by-claude-code")[0])
        self.terminal.owners["refresh-by-claude-code"] = self.beta
        interrupted(lambda: self.paths.claude_cli_credentials.write_bytes(by_claude_code))
        started()
        self.assertEqual(self.engine.recovery, [])
        self.assertEqual(self.paths.claude_cli_credentials.read_bytes(), by_claude_code)
        self.assertEqual(self.live()[1]["oauthAccount"]["accountUuid"], self.beta)
        self.assertEqual(self.engine.vault.get(self.beta_key)["credentials"]["claudeAiOauth"]["refreshToken"],
                         "refresh-by-claude-code")
        # A login nobody can confirm stays where it is, filed under nobody, and auto-switch waits, saying why.
        unknown = json_bytes(terminal_login(self.beta, "", now=time.time() + 7200, token="nobody-knows")[0])
        perform(self.engine.cli, self.alpha_key)
        interrupted(lambda: self.paths.claude_cli_credentials.write_bytes(unknown))
        saved = self.engine.vault.get(self.beta_key)["fingerprint"]
        started()
        self.assertEqual(len(self.engine.recovery), 1)
        self.assertEqual(self.paths.claude_cli_credentials.read_bytes(), unknown)
        self.assertEqual(self.engine.vault.get(self.beta_key)["fingerprint"], saved)
        self.assertEqual(cli_auto.paused(self.engine), "An interrupted terminal switch needs you")
        self.assertIn("run /login in a terminal with beta", self.engine.recovery_words()[1])
        # /login in a terminal settles it: Claude confirms that login as beta's, and it is kept.
        relogin = json_bytes(terminal_login(self.beta, "", now=time.time() + 600, token="beta-relogin")[0])
        self.terminal.owners["refresh-beta-relogin"] = self.beta
        self.paths.claude_cli_credentials.write_bytes(relogin)
        self.engine.cli.recovery_wait["next"] = 0.0          # its back-off is over
        self.engine.tick()
        self.assertEqual((self.engine.recovery, self.paths.claude_cli_credentials.read_bytes()), ([], relogin))
        # A repair cut short between its write and its commit (the service stops there): after the restart its record
        # is closed, the sign-in it gave the terminals stays, and nothing asks for a sign-in.
        perform(self.engine.cli, self.alpha_key)
        atomic_write(self.paths.claude_cli_credentials, json_bytes({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}}))
        self.engine.sense_active()
        self.terminal.owners["refresh-alpha-again"] = self.alpha
        self.sign_in_again(terminal_login(self.alpha, "", now=time.time() + 600, token="alpha-again")[0])
        self.engine.sense_active()
        self.assertEqual(self.engine.cli.repair_wanted, (self.alpha_key, "lost"))

        class Stopped(BaseException):
            pass

        @contextmanager
        def nothing_undone(service, tx):
            yield

        def stop(tx):
            raise Stopped()
        with mock.patch.object(switch_cli, "_undoing", nothing_undone), mock.patch.object(switch_cli.CLITransaction, "commit", stop):
            with self.assertRaises(Stopped):
                switch_cli.repair(self.engine.cli, self.alpha_key)
        started()
        record, row = self.engine.vault.get(self.alpha_key), self.engine.store.account(self.alpha_key)
        self.assertEqual((self.engine.recovery, self.live()[0]["claudeAiOauth"]["refreshToken"]), ([], "refresh-alpha-again"))
        self.assertEqual((record["credentials"]["claudeAiOauth"]["refreshToken"], record["health"], cli_accounts.unheld(record)),
                         ("refresh-alpha-again", "ok", False))
        self.assertEqual((row["flags"]["health"], row["error"]), ("ok", None))
        self.assertIn(row["credential_state"], ("fresh", "live"))
        self.assertIn(switch_cli.REPAIR_KEPT, [notice["message"] for notice in self.engine.notices])

    def test_an_environment_failure_holds_auto_switch_and_backs_off_no_target(self):
        engine, now = self.engine, time.time()
        engine.store.observe(self.alpha_key, weekly(96, now), at=now)
        engine.store.observe(self.beta_key, weekly(20, now), at=now)
        before = self.live_bytes()
        busy = DeckError("Claude Code is renewing its login — try again in a few seconds", "busy")
        with mock.patch.object(switch_cli, "credential_locks", side_effect=busy):
            cli_auto.tick(engine)
            self.assertTrue(engine.runner.wait(5))
        self.assertEqual(self.live_bytes(), before)
        past = engine.store.setting("cli_history")
        self.assertEqual(past.get("failures", {}), {})
        self.assertAlmostEqual(past["environment"]["until"], now + cli_auto.ENV_BACKOFF, delta=10)
        # The automatic switch as a whole waits, saying what is wrong, once in an hour; the target stays eligible.
        self.assertTrue(cli_auto.paused(engine).startswith(str(busy)))
        self.assertEqual(cli_auto.decision(engine)["target"], self.beta_key)
        told = [alert for alert in engine.store.alerts() if alert["kind"] == "switch_result"]
        self.assertEqual([(alert["title"], alert["message"]) for alert in told],
                         [("Terminal auto-switch can't switch right now", str(busy))])
        repeats = [cli_auto.switch_settled(engine, self.beta_key, ok=False, automatic=True, now=now + later, environment=str(busy))
                   for later in (70, 900, 1800, 2700, cli_auto.ALERT_EVERY + 10)]
        self.assertEqual(repeats, [True, True, True, True, False])
        self.assertLessEqual(engine.store.setting("cli_history")["environment"]["until"], now + 3610 + cli_auto.ENV_CAP)
        with cli_auto.history(engine) as past:
            past.pop("environment")
        # A failure about the target backs that target off, and the hold ends.
        real = engine.cli.adapter.current

        def unverifiable():
            found = real()
            return {**found, "identity": new_uuid()} if found["identity"] == self.beta else found
        with mock.patch.object(engine.cli.adapter, "current", side_effect=unverifiable):
            cli_auto.tick(engine)
            self.assertTrue(engine.runner.wait(5))
        past = engine.store.setting("cli_history")
        self.assertNotIn("environment", past)
        self.assertEqual(list(past["failures"]), [self.beta_key])
        self.assertEqual(self.live_bytes(), before)


class DesktopSwitchTests(SyntheticTestCase):
    def test_a_code_session_registered_by_macos_claude_code_counts_as_idle(self):
        # Claude Code on macOS writes procStart as `ps -o lstart` text in UTC, not a number: a session must
        # still be recognised, or "Switch when Claude is idle" waits forever on an "unknown" session.
        procs = FakeProcessController(self.paths, timings=FAST)
        procs.add_claude_desktop()
        session = procs.add_code_session("Docs pass", status="idle", status_age_s=120)
        row_file = self.paths.claude_sessions / f"{session.pid}.json"

        def report(proc_start):
            row = json.loads(row_file.read_bytes())
            row_file.write_text(json.dumps({**row, "procStart": proc_start}))
            return activity.report(self.paths, procs, now=time.time(), idle_hold_s=20).sessions[0].status
        started = time.gmtime(unix_time(session.ctime))
        lstart = (f"{time.strftime('%a %b', started)} {started.tm_mday:2d} "
                  f"{time.strftime('%H:%M:%S %Y', started)}")
        self.assertEqual(report(lstart), "idle")
        self.assertEqual(report("Thu Jan  1 00:00:00 2026"), "unknown")       # another process's row


    def test_switch_with_claude_closed_restores_the_target_and_saves_the_outgoing_account(self):
        alpha = self.fake_home()["claude"]
        procs = FakeProcessController(self.paths, timings=FAST, relaunch_identity=alpha["identity"])
        engine = self.make_engine(procs=procs)
        alpha_key = engine.accounts.capture("claude", claude_running=False)
        beta = self.fake_home()["claude"]                      # Claude Desktop now signed in to another account
        engine.sense_active()
        plan = engine.action("switch-plan", {"id": alpha_key})["plan"]
        self.assertEqual((plan["stop_tier"], plan["blockers"], plan["saves_outgoing"]), ("not_running", [], True))
        engine.action("switch", {"id": alpha_key, "plan_id": plan["plan_id"], "consent": "now"})
        self.assertTrue(engine.runner.wait(20))
        report = engine.runner.report()
        self.assertEqual(report["outcome"], "success", report["error"])
        config = json.loads((self.paths.claude_profile / "config.json").read_bytes())
        self.assertEqual(config["lastKnownAccountUuid"], alpha["identity"])
        self.assertEqual(config["userThemeMode"], "system")                           # settings stay as they were
        beta_key = engine.store.find("claude", beta["identity"])["id"]               # saved before switching away
        self.assertTrue(engine.store.account(beta_key)["flags"]["restorable"])
        self.assertEqual(engine.vault.get(beta_key)["identity"], beta["identity"])
        self.assertEqual(recover_transactions(engine.vault), [])

    def test_a_job_that_has_left_its_wait_is_never_cancelled_for_a_terminal_switch(self):
        alpha = self.fake_home()["claude"]
        procs = FakeProcessController(self.paths, timings=FAST, relaunch_identity=alpha["identity"], exit_delay_s=0.3)
        live, spare = new_uuid(), new_uuid()
        self.sign_in_terminal(*terminal_login(live, "live@example.test", token="live"))
        engine = self.make_engine(procs=procs, owners={"refresh-live": live, "refresh-spare": spare})
        alpha_key = engine.accounts.capture("claude", claude_running=False)
        self.fake_home()
        procs.add_claude_desktop()
        engine.sense_active()
        # The terminals need to move (the live account is out, the spare one has room) ...
        now = time.time()
        engine.store.observe(engine.store.find("claude_cli", live)["id"], weekly(100, now), at=now)
        engine.store.observe(self.save_terminal(engine, *terminal_login(spare, "spare@example.test", token="spare")),
                             weekly(10, now), at=now)
        self.assertEqual(cli_auto.decision(engine)["kind"], "out")
        # ... while a Desktop switch is closing Claude, still cancellable by the owner but past its wait.
        plan = engine.action("switch-plan", {"id": alpha_key})["plan"]
        engine.action("switch", {"id": alpha_key, "plan_id": plan["plan_id"], "consent": "now"})
        self.assertTrue(reach(engine, "stopping"))
        self.assertTrue(engine.runner.pending()["cancellable"])
        self.assertFalse(engine.runner.preempt(cli_auto.MADE_WAY))
        cli_auto.tick(engine)
        self.assertTrue(engine.runner.wait(20))
        report = engine.runner.report()
        self.assertEqual((report["provider"], report["outcome"]), ("claude", "success"), report["error"])
        config = json.loads((self.paths.claude_profile / "config.json").read_bytes())
        self.assertEqual(config["lastKnownAccountUuid"], alpha["identity"])


if __name__ == "__main__":
    unittest.main()
