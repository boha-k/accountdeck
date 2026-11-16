"""The macOS platform layer's safety-critical paths, on this Mac's real kernel and a throwaway keychain.

Safe mode is on before anything loads. Real processes touched: only children this test starts itself (safe
mode refuses any other target). The keychain used is a new file in a temporary folder; the login keychain,
the search list and the default keychain are never touched.
"""
from __future__ import annotations

import os

os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"

import signal  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

from accountdeck import procs  # noqa: E402
from accountdeck.config import Paths, SafetyViolation  # noqa: E402
from accountdeck.platform import libproc  # noqa: E402

SECURITY = "/usr/bin/security"


def token_for(rows, *, issued=None, ttl=600.0, families=()):
    issued = time.time() if issued is None else issued
    return procs.ConsentToken(job_id=uuid.uuid4().hex, provider="claude", purpose="switch", account=None, mode="now",
                              pairs=tuple((row.pid, row.ctime) for row in rows), families=tuple(families),
                              issued=issued, expires=issued + ttl)


class ModulesImport(unittest.TestCase):
    def test_platform_modules_import_without_loading_appkit(self):
        code = ("import accountdeck.platform, accountdeck.procs, accountdeck.stop_ladder, accountdeck.autostart\n"
                "from accountdeck.platform import (appkit, autostart_mac, credstore, dashboard_window, facts_mac, "
                "keychain, libproc, menubar, notify_mac, procs_mac, statusbar_mac)\n"
                "assert appkit.ObjC._shared is None and libproc._api is None\n")
        result = subprocess.run([sys.executable, "-E", "-s", "-c", code], cwd=Path(__file__).resolve().parent.parent,
                                env={**os.environ, "ACCOUNTDECK_SAFE_MODE": "1"}, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


class LiveProcessIdentity(unittest.TestCase):
    """The live controller against a real child of this test: verify-then-signal behind the kqueue pin."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.paths = Paths.synthetic(Path(self.folder.name) / "root")
        self.controller = procs.get_controller(self.paths, demo=False, allow_readonly=True)
        self.child = self.controller.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self._reap)
        path = libproc.api().pidpath(self.child.pid)
        ctime = libproc.creation_time(self.child.pid)
        self.assertTrue(path and ctime)
        # A stoppable consumer as classification would report one: what consent and step 3 are checked against.
        self.row = procs.ProcessInfo(self.child.pid, os.getpid(), "codex", path, ctime, None, kind="codex_tui")

    def _reap(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(10)
        self.folder.cleanup()

    def assert_refused(self, target, token, reason):
        with self.assertRaises(SafetyViolation) as caught:
            self.controller.stop_consumer(token, target)
        self.assertEqual(getattr(caught.exception, "reason", None), reason)
        time.sleep(0.05)
        self.assertIsNone(self.child.poll(), "a refused stop must leave the process running")

    def test_changed_or_unknown_identity_fails_closed_and_the_verified_one_stops(self):
        from dataclasses import replace
        changed = replace(self.row, ctime=self.row.ctime + 10)       # the pid now names another start
        # Safe mode's ownership registry is keyed by pid and start time, so step 1 refuses it already...
        self.assert_refused(changed, token_for([changed]), "not_owned")
        # ...and step 3, behind the kqueue pin, refuses it on its own as well.
        with libproc.api().exit_watch() as watch, self.assertRaises(SafetyViolation) as caught:
            self.controller._verified(changed, watch)
        self.assertEqual(caught.exception.reason, "ctime_mismatch")
        moved = replace(self.row, path=self.row.path + "-elsewhere")
        self.assert_refused(moved, token_for([moved]), "path_mismatch")
        unknown = replace(self.row, ctime=0)                          # start time macOS would not give
        self.assert_refused(unknown, token_for([unknown]), "not_owned")
        self.assert_refused(self.row, token_for([]), "not_covered")   # consent for another set
        self.assert_refused(self.row, token_for([self.row], issued=time.time() - 3600), "expired")
        self.assertTrue(self.controller.stop_consumer(token_for([self.row]), self.row))
        self.assertEqual(self.controller.wait_exit([self.row], 5.0), [])
        self.assertEqual(self.child.wait(5), -signal.SIGTERM)

    def test_claude_code_registry_start_time_matches_the_kernel(self):
        # Claude Code on macOS writes `procStart` as `ps -o lstart` text in UTC; it must name this process.
        text = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(self.child.pid)], capture_output=True,
                              text=True, env={"TZ": "UTC", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}).stdout
        self.assertTrue(procs.proc_start_matches(self.row.ctime, text))
        self.assertFalse(procs.proc_start_matches(self.row.ctime + 20_000_000, text))
        self.assertTrue(procs.proc_start_matches(self.row.ctime, str(self.row.ctime)))   # synthetic registries


class ConsentCannotWiden(unittest.TestCase):
    """The stop ladder's rules on the synthetic Mac: only what the user confirmed may be signalled."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.paths = Paths.synthetic(Path(self.folder.name) / "root")
        self.procs = procs.FakeProcessController(self.paths)
        self.main = self.procs.add_claude_desktop(helpers=2)
        self.code = self.procs.add_code_session("work")

    def kinds(self):
        return self.procs.classify(self.procs.snapshot())

    def test_classification_and_token_limits(self):
        kinds = self.kinds()
        self.assertEqual(len(kinds["claude_desktop"]), 3)
        self.assertEqual([row.pid for row in kinds["claude_code"]], [self.code.pid])
        desktop = kinds["claude_desktop"]
        token = token_for(desktop + kinds["claude_code"], families=(self.paths.claude_family,))
        # A Code child that appeared after the confirmation is not covered, though its parent is.
        time.sleep(0.01)
        late = self.procs.add_code_session("late")
        late_row = next(row for row in self.procs.snapshot() if row.pid == late.pid)
        with self.assertRaises(SafetyViolation) as caught:
            self.procs.terminate(token, late_row)
        self.assertEqual(caught.exception.reason, "not_covered")
        # Claude's updater and anything outside Claude's bundle are never signalled, even when listed.
        updater = self.procs.add_row("ShipIt", path="/x/Claude.app/Contents/Frameworks/Squirrel.framework/ShipIt",
                                     family=self.paths.claude_family, parent=self.main.pid)
        terminal = self.procs.add_foreign_claude()
        rows = {row.pid: row for row in self.procs.snapshot()}
        self.assertEqual(rows[terminal.pid].kind, "claude_foreign")
        wide = token_for(list(rows.values()), families=(self.paths.claude_family,))
        for pid, reason in ((updater.pid, "protected"), (terminal.pid, "kind")):
            with self.assertRaises(SafetyViolation) as caught:
                self.procs.terminate(wide, rows[pid])
            self.assertEqual(caught.exception.reason, reason)
        # The confirmed main may be asked to quit; it runs Claude's quit and the whole set exits.
        self.procs.request_close(token, procs.desktop_main(desktop))
        self.assertEqual(self.procs.wait_exit(desktop, 2.0), [])

    def test_stop_ladder_asks_again_when_processes_appear(self):
        import threading
        from accountdeck import stop_ladder
        from accountdeck.config import Timings
        kinds = self.kinds()
        plan_set = procs.ProcessSet.of(kinds["claude_desktop"] + kinds["claude_code"])
        token = token_for(kinds["claude_desktop"] + kinds["claude_code"], families=(self.paths.claude_family,))
        self.procs.add_code_session("new since the plan")
        timings = Timings(graceful_wait=1, terminate_wait=1, helper_self_exit=0.2, settle_file=0.5, settle_grace=0)
        quiet = type("Diagnostics", (), {"log": staticmethod(lambda *a, **k: None)})
        ladder = stop_ladder.StopLadder(self.paths, self.procs, None, timings, quiet)
        result = ladder.run(token, plan_set, cancel=threading.Event(), progress=lambda *a: None)
        self.assertEqual(result.tier, "reconfirm")
        self.assertFalse(any(call[0] in ("request_close", "terminate") for call in self.procs.calls))


class KeychainRoundTrip(unittest.TestCase):
    """keychain.py and the Claude Code credential store against a keychain file made for this test."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name).resolve()
        procs.register_test_root(root)
        self.keychain = root / "test.keychain-db"
        self.before = self._search_list()
        password = uuid.uuid4().hex             # a throwaway keychain's password: fine in argv
        for args in (["create-keychain", "-p", password], ["set-keychain-settings"], ["unlock-keychain", "-p", password]):
            subprocess.run([SECURITY, *args, str(self.keychain)], check=True, capture_output=True, timeout=30)
        self.addCleanup(self._delete)

    def _search_list(self):
        return subprocess.run([SECURITY, "list-keychains", "-d", "user"], capture_output=True, text=True).stdout

    def _delete(self):
        subprocess.run([SECURITY, "delete-keychain", str(self.keychain)], capture_output=True, timeout=30)
        self.assertEqual(self._search_list(), self.before, "the keychain search list must not change")

    def test_values_vault_key_and_claude_code_login(self):
        from accountdeck.platform import credstore, keychain
        kc = self.keychain
        self.assertIsNone(keychain.get("AD2 test", "acct", keychain=kc))
        keychain.set("AD2 test", "acct", 'value with "quotes" and spaces', keychain=kc)
        self.assertEqual(keychain.get("AD2 test", "acct", keychain=kc), 'value with "quotes" and spaces')
        long_value = "x" * 6000                  # longer than one `security -i` line: the `-X` argv route
        keychain.set("AD2 test", "long", long_value, keychain=kc)
        self.assertEqual(keychain.get("AD2 test", "long", keychain=kc), long_value)
        self.assertTrue(keychain.delete("AD2 test", "acct", keychain=kc))
        self.assertFalse(keychain.delete("AD2 test", "acct", keychain=kc))
        with self.assertRaises(SafetyViolation):
            keychain.get("AD2 test", "acct")      # the login keychain is refused in safe mode

        cipher = keychain.VaultCipher(Path(self.folder.name) / "data", keychain=kc)
        sealed = cipher(b"secret record")
        self.assertEqual(keychain.VaultCipher(Path(self.folder.name) / "data", keychain=kc)(sealed, decrypt=True),
                         b"secret record")
        other = keychain.VaultCipher(Path(self.folder.name) / "other", keychain=kc)
        with self.assertRaises(keychain.KeychainError):
            other(sealed, decrypt=True)          # a missing key fails closed; it is never created on decrypt

        paths = Paths.synthetic(Path(self.folder.name) / "root")
        store = credstore.for_paths(paths, keychain=kc, accounts=("tester",))
        self.assertEqual(store.kind, "keychain")
        before = {"claudeAiOauth": {"accessToken": "a1", "refreshToken": "r1"}}
        keychain.set(credstore.SERVICE, "tester", credstore.encode(before), keychain=kc)

        class Tx:
            doc = {}

            def save(self):
                pass
        tx = Tx()
        after = {"claudeAiOauth": {"accessToken": "a2", "refreshToken": "r2"}}
        store.write(tx, after)
        self.assertEqual(store.read(), after)
        self.assertEqual(tx.doc["keychain_changes"][0]["before"], credstore.encode(before))
        self.assertFalse(store.rollback(tx))
        self.assertEqual(store.read(), before)


if __name__ == "__main__":
    unittest.main()
