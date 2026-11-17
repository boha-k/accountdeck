"""How a Codex probe reports a sign-in Codex can no longer renew (the live log's `refresh.failed
code=identity` after the move): Codex 0.161 answers account/read with `{"account": null}` once its
refresh token is refused for good. That must read as "sign in again", never as another account.
And what a renewal may do: AccountDeck never asks twice with one refresh token, and a renewed one is never lost."""
import json
import time
import unittest
from unittest import mock

from tests.helpers import CODEX_RATES, SyntheticTestCase, codex_auth

from accountdeck import switch_codex
from accountdeck.core import DeckError
from accountdeck.procs import FakeProcessController
from accountdeck.providers import codex as codex_module


UNAUTHORIZED = {"code": -32603, "message": "GET /wham/usage failed: 401 Unauthorized"}


def scripted_rpc(account, *, refused=None, later=UNAUTHORIZED):
    """An RPC stand-in answering account/read with `account` (or the error `refused`) and refusing the rate-limit read."""
    class ScriptedRPC:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def call(self, method, params=None, timeout=25):
            if method == "account/read" and not refused:
                return {"account": account, "requiresOpenaiAuth": True}
            raise codex_module.classify_rpc_error(refused or later)
    return ScriptedRPC


class CodexRefreshTests(SyntheticTestCase):
    def setUp(self):
        super().setUp()
        self.fake_home()                                   # writes paths.codex_exe (the CLI the probe would run)
        self.codex = codex_module.Codex(self.paths, FakeProcessController(self.paths))
        self.expired = codex_auth("acct-moved", exp=time.time() - 190_000, refresh="rt-refused")

    def probe(self, account, mode):
        stored = []
        with mock.patch.object(codex_module, "RPC", scripted_rpc(account)):
            return self.codex.usage(self.expired, mode=mode, persist=stored.append, quarantine=stored.append)

    def test_a_dropped_sign_in_needs_signing_in_again_not_another_account(self):
        refreshed = self.probe(None, "refresh")
        self.assertEqual((refreshed.error.kind, refreshed.error.code), ("refresh_rejected", "reauth_required"))
        self.assertEqual(refreshed.credential, "reauth_required")
        read_only = self.probe(None, "read_only")
        self.assertEqual(read_only.credential, "access_expired")       # the official client renews it when it runs
        api_key = self.probe({"type": "apiKey"}, "read_only")
        self.assertEqual(api_key.error.kind, "identity")                # a real non-subscription login stays refused


class CodexRenewalTests(SyntheticTestCase):
    def setUp(self):
        super().setUp()
        self.fake_home()                                   # Codex is signed in to another account
        self.codex = codex_module.Codex(self.paths, FakeProcessController(self.paths))
        self.auth = self.paths.codex_home / "auth.json"
        self.before = codex_auth("acct-saved", exp=time.time() + 3600, refresh="rt-before")     # due for renewal
        self.engine = self.make_engine(codex=self.codex)
        self.key = self.engine.accounts.save("codex", self.before, self.codex.identity(self.before))
        self.stash = "codex-stash-" + self.key
        self.sent = []          # the refresh token each helper was given ("" for a read-only one)

    def helper(self, *, rotate_to=None, failure=None, meanwhile=None):
        def probe(work, identity, refresh):
            self.sent.append(json.loads((work / "auth.json").read_bytes())["tokens"]["refresh_token"])
            if refresh:         # the record exists before a helper can spend the token
                self.assertTrue(self.engine.vault.path(self.stash).exists())
                if rotate_to:
                    (work / "auth.json").write_text(json.dumps(rotate_to))
            if meanwhile:
                meanwhile()
            if failure:
                raise failure
            return CODEX_RATES, None
        return mock.patch.object(self.codex, "_probe", side_effect=probe)

    def read_later(self):
        """A read of the account once its next renewal attempt is allowed."""
        flags = self.engine.store.account(self.key)["flags"]
        self.engine.store.update(self.key, flags={**flags, "refresh_attempt": time.time() - 7 * 3600})
        self.engine._refresh(self.key)

    def helper_folders(self):
        return list((self.paths.data_dir / "work").glob("probe-*"))

    def test_a_login_codex_holds_or_may_hold_is_never_renewed(self):
        for live in (json.dumps(self.before).encode(), b'{"partial":'):      # no tick has seen either yet
            self.auth.write_bytes(live)
            with self.helper():
                self.engine._refresh(self.key)
            self.assertEqual(self.auth.read_bytes(), live)
            with mock.patch.object(self.codex, "_probe") as probe, self.assertRaises(DeckError):
                self.engine.accounts.renew_codex(self.engine.store.account(self.key), self.before)
            probe.assert_not_called()
        self.assertEqual(self.sent, ["", ""])
        self.assertEqual(self.engine.vault.get(self.key), self.before)

    def test_a_renewed_login_is_recovered_after_each_interruption_without_a_second_send(self):
        after = codex_auth("acct-saved", exp=time.time() + 3600, refresh="rt-after")       # same expiry: it still wins
        with self.helper(rotate_to=after), self.assertRaises(OSError):
            with mock.patch.object(self.engine.vault, "put_guarded", side_effect=OSError("disk unavailable")):
                self.engine._refresh(self.key)
        self.assertEqual(self.engine.vault.get(self.key), self.before)
        for folder in self.helper_folders():      # the encrypted copy alone is enough
            (folder / "auth.json").unlink()
        self.assertEqual(self.engine.accounts.settle_renewal(self.engine.store.account(self.key)), after)
        self.assertEqual(self.helper_folders(), [])
        self.assertEqual(self.sent, ["rt-before"])

        # A crash after the helper wrote its file and before anything was stored; the next start finds it.
        later = codex_auth("acct-saved", exp=time.time() + 3600, refresh="rt-later")
        with self.helper(rotate_to=later), self.assertRaises(OSError):
            with mock.patch.object(self.codex, "_settle", side_effect=OSError("died here")):
                self.engine.accounts.renew_codex(self.engine.store.account(self.key), after)
        self.assertEqual(len(self.helper_folders()), 1)
        engine = self.make_engine(codex=self.codex)
        self.assertEqual(engine.vault.get(self.key), later)
        self.assertFalse(engine.vault.path(self.stash).exists())
        self.assertEqual(self.helper_folders(), [])
        self.assertEqual(self.sent, ["rt-before", "rt-after"])

        # A crash while its helper still runs (one outlives the service by seconds): a start leaves the record for as
        # long as a Codex process may use the helper's folder, not while one uses only Codex's own.
        last = codex_auth("acct-saved", exp=time.time() + 10 * 86400, refresh="rt-last")
        with self.helper(), self.assertRaises(OSError):
            with mock.patch.object(self.codex, "_settle", side_effect=OSError("died here")):
                self.engine.accounts.renew_codex(self.engine.store.account(self.key), later)
        [folder] = self.helper_folders()
        session, helper = self.codex.procs.add_codex("codex_tui"), self.codex.procs.add_codex("codex_app_server_child")
        lock = "{}/tmp/arg0/codex-arg0AbC/.lock".format
        files = {session.pid: [lock(self.paths.codex_home.resolve())]}
        with mock.patch.object(self.codex.procs, "_open_paths", side_effect=lambda row: files.get(row.pid)):
            for readable in (None, [lock(folder.resolve())]):         # its files cannot be read, then can
                files[helper.pid] = readable
                engine = self.make_engine(codex=self.codex)
                self.assertTrue(engine.vault.path(self.stash).exists())
                self.assertNotEqual(engine.store.account(self.key)["credential_state"], "reauth_required")
                self.assertEqual(self.helper_folders(), [folder])
                with self.assertRaises(DeckError):
                    engine.accounts.codex_profile(engine.store.account(self.key))     # nor handed to Codex meanwhile
            (folder / "auth.json").write_text(json.dumps(last))      # its renewal ends, and then the helper
            self.codex.procs.exit_process(helper.pid)
            self.assertFalse(self.codex.helper_running(folder.name))
            engine = self.make_engine(codex=self.codex)
        self.assertEqual(engine.vault.get(self.key), last)
        self.assertFalse(engine.vault.path(self.stash).exists())
        self.assertEqual(self.helper_folders(), [])
        self.assertEqual(self.sent, ["rt-before", "rt-after", "rt-later"])

    def test_an_uncertain_renewal_is_never_replayed_and_asks_for_sign_in(self):
        with self.helper(failure=codex_module.ProviderError("network")):      # say, its reply was lost
            self.engine._refresh(self.key)
            self.read_later()
            self.make_engine(codex=self.codex)        # a restart forgets nothing
            self.read_later()
        self.assertEqual(self.sent, ["rt-before", "", ""])
        row = self.engine.store.account(self.key)
        self.assertEqual(row["credential_state"], "reauth_required")
        self.assertEqual(self.engine.vault.get(self.key), self.before)
        blockers = self.engine.action("switch-plan", {"id": self.key})["plan"]["blockers"]
        self.assertIn("reauth_required", [blocker["code"] for blocker in blockers])
        with self.assertRaises(DeckError):
            self.engine.accounts.codex_profile(row)     # Codex itself would replay it

        # The sign-in that was asked for ends it; the next renewal publishes its expiry.
        signed_in = codex_auth("acct-saved", exp=time.time() + 7200, refresh="rt-signed-in")
        self.engine.accounts.save("codex", signed_in, self.codex.identity(signed_in), source="login")
        row = self.engine.store.account(self.key)
        self.assertEqual((row["credential_state"], row["error"]), ("fresh", None))
        renewed = codex_auth("acct-saved", exp=time.time() + 10 * 86400, refresh="rt-renewed")
        with self.helper(rotate_to=renewed):
            self.read_later()
        self.assertEqual(self.sent[-1], "rt-signed-in")
        self.assertEqual(self.engine.vault.get(self.key), renewed)
        self.assertFalse(self.engine.vault.path(self.stash).exists())
        row = self.engine.store.account(self.key)
        self.assertEqual(row["credential_state"], "fresh")
        self.assertAlmostEqual(row["expires_at"], codex_module.token_state(renewed, 0).exp, delta=2)

    def test_a_login_recover_may_put_back_is_not_renewed_meanwhile(self):
        self.auth.write_text(json.dumps(self.before))               # Codex holds the saved account's login
        self.engine.sense_active()
        other = codex_auth("acct-other", exp=time.time() + 10 * 86400, refresh="rt-other")
        target = self.engine.accounts.save("codex", other, self.codex.identity(other))
        opened = []

        def session_starts():           # while the switch checks its target: the switch is left for Recover
            if not opened:
                opened.append(self.engine.procs.add_codex("codex_tui").pid)
                raise codex_module.ProviderError("identity", message="Codex returned a different account.")
        plan = self.engine.action("switch-plan", {"id": target})["plan"]
        held, resume = [], self.engine.scheduler.resume_provider

        def reads_resume(provider):     # reads of Codex start again here, before the operation has ended
            held.append(self.engine.login_held("acct-saved"))
            resume(provider)
        with self.helper(meanwhile=session_starts):
            with mock.patch.object(self.engine.scheduler, "resume_provider", side_effect=reads_resume):
                self.engine.action("switch", {"id": target, "plan_id": plan["plan_id"], "consent": "now"})
                self.assertTrue(self.engine.runner.wait(10))
            self.read_later()           # due, and no longer Codex's
        self.assertEqual((self.engine.runner.report()["outcome"], held), ("failed", [True]))
        self.assertIsNone(self.engine.store.account(self.key)["error_code"])
        with mock.patch.object(self.codex, "_probe") as probe, self.assertRaises(DeckError):
            self.engine.accounts.renew_codex(self.engine.store.account(self.key), self.before)
        probe.assert_not_called()

        self.engine.procs.exit_process(opened[0])
        plan = self.engine.action("switch-plan", {"provider": "codex", "purpose": "recover"})["plan"]
        self.engine.action("recover", {"provider": "codex", "plan_id": plan["plan_id"], "consent": "now"})
        self.assertTrue(self.engine.runner.wait(10))
        self.assertEqual(json.loads(self.auth.read_text()), self.before)     # put back, its token never sent
        self.assertNotIn("rt-before", self.sent)
        self.assertFalse(self.engine.login_held("acct-saved"))

    def test_recover_keeps_a_login_codex_renewed_after_the_interrupted_switch(self):
        self.auth.write_text(json.dumps(self.before))
        self.engine.sense_active()
        other = codex_auth("acct-other", exp=time.time() + 10 * 86400, refresh="rt-other")
        target = self.engine.accounts.save("codex", other, self.codex.identity(other))
        opened = []

        def session_starts():           # while the switch checks its target: the switch is left for Recover
            if not opened:
                opened.append(self.engine.procs.add_codex("codex_tui").pid)
                raise codex_module.ProviderError("identity", message="Codex returned a different account.")
        plan = self.engine.action("switch-plan", {"id": target})["plan"]
        with self.helper(meanwhile=session_starts):
            self.engine.action("switch", {"id": target, "plan_id": plan["plan_id"], "consent": "now"})
            self.assertTrue(self.engine.runner.wait(10))
        # That session renews the login it found in auth.json, the target's, and ends.
        renewed = codex_auth("acct-other", exp=time.time() + 20 * 86400, refresh="rt-other-renewed")
        self.auth.write_text(json.dumps(renewed))
        self.engine.procs.exit_process(opened[0])
        plan = self.engine.action("switch-plan", {"provider": "codex", "purpose": "recover"})["plan"]
        self.engine.action("recover", {"provider": "codex", "plan_id": plan["plan_id"], "consent": "now"})
        self.assertTrue(self.engine.runner.wait(10))
        self.assertEqual(self.engine.runner.report()["outcome"], "success")
        self.assertEqual(self.engine.runner.report()["summary"], switch_codex.KEPT)
        self.assertEqual(json.loads(self.auth.read_text()), renewed)          # Codex keeps the newer login ...
        self.assertEqual(self.engine.vault.get(target), renewed)              # ... saved as its own account's
        self.assertEqual(self.engine.vault.get(self.key), self.before)
        self.assertEqual((self.engine.recovery, self.engine.login_held("acct-saved")), ([], False))

    def test_a_sign_in_left_by_an_add_cut_short_is_saved_at_start(self):
        # A crash during a Codex Add leaves its sign-in folder behind, with the sign-in that finished there in the clear.
        home = self.paths.data_dir / "work" / "login-cut-short"
        home.mkdir(parents=True)
        added = codex_auth("acct-added", exp=time.time() + 5 * 86400, refresh="rt-added")
        (home / "auth.json").write_text(json.dumps(added))
        self.engine.stop()
        engine = self.make_engine(codex=self.codex)     # the next start
        self.assertEqual(engine.vault.get(engine.store.find("codex", "acct-added")["id"]), added)
        self.assertFalse(home.exists())

    def test_a_renewal_that_never_left_this_mac_costs_nothing(self):
        with mock.patch.object(self.codex, "token_endpoint_answers", return_value=False), self.helper():
            self.engine._refresh(self.key)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.engine.store.account(self.key)["error_code"], "network")
        self.assertNotIn("refresh_attempt", self.engine.store.account(self.key)["flags"])  # tried at the next read
        with mock.patch.object(self.codex.procs, "spawn_helper", side_effect=FileNotFoundError):
            self.engine._refresh(self.key)
        unknown_method = {"code": -32601, "message": "no such method"}
        with mock.patch.object(codex_module, "RPC", scripted_rpc(None, refused=unknown_method)):      # refused unread
            self.read_later()
        self.assertEqual(self.engine.store.account(self.key)["credential_state"], "fresh")
        self.assertFalse(self.engine.vault.path(self.stash).exists())
        with mock.patch.object(codex_module, "RPC", scripted_rpc({"type": "chatgpt"}, later=unknown_method)):   # read, then refused
            self.read_later()
        self.assertEqual(self.engine.store.account(self.key)["credential_state"], "reauth_required")

        # A helper that never started sent nothing, however old its access token. One that started renews an access
        # token near its end by itself (Codex 0.161: within 5 minutes of it), before it is asked anything.
        spawn = mock.patch.object(self.codex.procs, "spawn_helper", side_effect=FileNotFoundError)
        start = mock.patch.object(codex_module.RPC, "call", side_effect=codex_module.ProviderError("codex"))
        for failure, left, unsent in ((spawn, -60, True), (start, 300, False), (start, 900, True)):
            now = mock.patch.object(self.codex, "clock", return_value=time.time() + 3600 - left)
            with self.subTest(left=left), failure, now:
                reading = self.codex.usage(self.before, mode="refresh", persist=self.fail, quarantine=self.fail)
                self.assertEqual(reading.meta["unsent"], unsent)

    def test_a_login_without_a_refresh_token_never_replaces_a_saved_one(self):
        blank = codex_auth("acct-saved", exp=time.time() + 86400, refresh="")
        with self.helper(rotate_to=blank):
            self.engine._refresh(self.key)
        self.assertEqual(self.engine.vault.get(self.key), self.before)
        self.assertTrue(list(self.engine.vault.root.glob("quarantine-*")))
        self.assertEqual(self.engine.store.account(self.key)["credential_state"], "reauth_required")
        self.auth.write_text(json.dumps(blank))
        self.engine.accounts.capture("codex")
        self.assertEqual(self.engine.vault.get(self.key), self.before)

    def test_a_sign_in_that_changes_during_the_targets_renewal_is_never_overwritten(self):
        third = json.dumps(codex_auth("acct-third", exp=time.time() + 86400, refresh="rt-third"))
        after = codex_auth("acct-saved", exp=time.time() + 86400, refresh="rt-after")
        plan = self.engine.action("switch-plan", {"id": self.key})["plan"]
        with self.helper(rotate_to=after, meanwhile=lambda: self.auth.write_text(third)):
            self.engine.action("switch", {"id": self.key, "plan_id": plan["plan_id"], "consent": "now"})
            self.assertTrue(self.engine.runner.wait(10))
        self.assertEqual(self.engine.runner.report()["outcome"], "failed")
        self.assertEqual(self.auth.read_text(), third)
        self.assertEqual(self.engine.vault.get(self.key), after)     # the renewal itself is kept

    def test_only_a_session_certainly_on_another_login_is_left_out_of_a_switch(self):
        procs = FakeProcessController(self.paths)
        session = procs.add_codex("codex_tui")
        ours = self.paths.codex_home
        own_login, no_login, linked_login, dangling, linked_home = (self.paths.temp_root / name for name in
                                                                    ("own", "none", "linked", "dangling", "alias"))
        for home in (own_login, no_login, linked_login, dangling):
            home.mkdir()
        (own_login / "auth.json").write_text("{}")
        (linked_login / "auth.json").symlink_to(self.auth)
        (dangling / "auth.json").symlink_to(self.paths.temp_root / "gone")
        linked_home.symlink_to(ours)
        scratch, nested = self.paths.temp_root / "scratch", ours / "nested"
        (scratch / "tmp").mkdir(parents=True)
        (ours / "tmp").symlink_to(scratch / "tmp")            # the kernel names a lock under it by scratch's path
        nested.mkdir()
        (nested / "auth.json").write_text("{}")
        mark = "{}/tmp/arg0/codex-arg0AbC/.lock".format
        for paths, left_out in ((None, False), ([], False), ([str(ours / "state_5.sqlite")], False), ([mark(ours)], False),
                                ([mark(linked_home)], False), ([mark(linked_login)], False), ([mark(dangling)], False),
                                ([mark("/gone")], False), ([mark(scratch.resolve())], False), ([mark(nested)], False),
                                ([mark(own_login), mark(no_login)], False), ([mark(own_login)], True), ([mark(no_login)], True)):
            with self.subTest(paths=paths), mock.patch.object(procs, "_open_paths", return_value=paths):
                self.assertEqual([row.pid for row in procs.codex_consumers()], [] if left_out else [session.pid])


if __name__ == "__main__":
    unittest.main()
