"""Importing a PC move bundle on the Mac (move_import): every account's sign-in lands in the vault in the
shape the switch flows use, a still-on-PC account stays frozen until its hand-over, and a wrong
passphrase or a repeated import changes nothing it should not."""
import time
import unittest
from unittest import mock

from tests.helpers import SyntheticTestCase, codex_auth, new_uuid, terminal_login

from accountdeck import move_bundle as mb, move_import
from accountdeck.core import DeckError
from accountdeck.providers.claude_cli import profile as cli_profile
from accountdeck.providers.claude_portable import to_portable

PASSPHRASE = "correct horse battery staple"
SCRYPT_N = 2 ** 10                 # fast for tests; the import accepts it while MIN_SCRYPT_N is patched


def entry(pc_id, provider, identity, *, secret=None, live_on_pc=False, label=None):
    account = {"id": pc_id, "provider": provider, "identity": identity, "label": label or pc_id,
               "email": f"{pc_id}@example.test", "plan": "max", "org": "", "created": time.time() - 86400,
               "live_on_pc": live_on_pc, "refills": [],
               "history": {"last_good_usage": None, "last_good_at": None, "readings": [
                   {"window_id": "five_hour", "at": time.time() - 600, "used": 42.0, "reset": None}], "snapshots": []}}
    if secret is not None:
        account["secret"] = {"type": mb.SECRET_TYPES[provider], "data": secret}
    return account


class MoveImportTests(SyntheticTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(move_import, "MIN_SCRYPT_N", SCRYPT_N)
        patcher.start()
        self.addCleanup(patcher.stop)
        home = self.fake_home()                            # Claude Desktop set up on this Mac (cookie template)
        self.engine = self.make_engine()
        desktop = self.engine.providers["claude"].current(cookies=True)
        self.desktop_identity = home["claude"]["identity"]
        self.portable = to_portable(desktop, self.engine.providers["claude"].crypto)
        self.codex_identity, self.cli_identity, self.waiting_identity = "acct-pc-codex", new_uuid(), new_uuid()
        self.codex_auth = codex_auth(self.codex_identity, exp=time.time() + 5 * 86400)
        credentials, account = terminal_login(self.cli_identity, "term@example.test", token="pc")
        self.cli_record = cli_profile(credentials, account, time.time())
        waiting_credentials, waiting_account = terminal_login(self.waiting_identity, "wait@example.test", token="w")
        self.waiting_record = cli_profile(waiting_credentials, waiting_account, time.time())

    def bundle(self, accounts, kind="main", settings=None, name="move.adeck"):
        payload = {"format": 1, "source_platform": "windows", "settings": settings or {}, "accounts": accounts}
        path = self.paths.temp_root / name
        path.write_bytes(mb.seal(payload, PASSPHRASE, kind, scrypt_n=SCRYPT_N, min_scrypt_n=SCRYPT_N))
        return path

    def import_(self, path, passphrase=PASSPHRASE):
        return move_import.import_from_file(self.engine, path, passphrase, timeout=20)

    def main_bundle(self):
        return self.bundle([
            entry("pc-desktop", "claude", self.desktop_identity, secret=self.portable),
            entry("pc-codex", "codex", self.codex_identity, secret=self.codex_auth),
            entry("pc-cli", "claude_cli", self.cli_identity, secret=self.cli_record),
            entry("pc-waiting", "claude_cli", self.waiting_identity, live_on_pc=True)],
            settings={"cli_threshold": 90, "cli_manual_order": ["pc-cli", "pc-gone"]})

    def test_main_bundle_imports_every_sign_in_and_withholds_nothing_twice(self):
        path = self.main_bundle()
        with self.assertRaises(DeckError):
            self.import_(path, "a wrong passphrase!")
        self.assertEqual(self.engine.store.accounts(), [])                    # a wrong passphrase writes nothing
        result = self.import_(path)
        self.assertTrue(result["complete"], result)
        header, payload = mb.open_bundle(path.read_bytes(), PASSPHRASE, min_scrypt_n=SCRYPT_N)
        self.assertEqual(result["code"], mb.code_for_imported(header, None, header.account_ids))
        self.assertEqual((result["counts"]["new"], result["counts"]["on_pc"]), (3, 1))
        store, vault = self.engine.store, self.engine.vault
        codex = store.find("codex", self.codex_identity)
        self.assertEqual((codex["owner"], codex["imported_from"]), ("v2", "pc"))
        self.assertEqual(vault.get(codex["id"]), self.codex_auth)
        terminal = store.find("claude_cli", self.cli_identity)
        self.assertEqual(vault.get(terminal["id"])["credentials"], self.cli_record["credentials"])
        self.assertTrue(terminal["flags"]["restorable"])
        desktop = store.find("claude", self.desktop_identity)
        self.assertTrue(desktop["flags"]["restorable"])
        self.assertEqual(self.engine.providers["claude"].health(vault.get(desktop["id"])), "ok")
        self.assertEqual(len(store.readings(desktop["id"], "five_hour")), 1)    # history travelled too
        waiting = store.find("claude_cli", self.waiting_identity)
        self.assertEqual(waiting["owner"], "on_pc")
        with self.assertRaises(DeckError) as refused:
            self.engine.action("refresh", {"id": waiting["id"]})
        self.assertEqual(refused.exception.code, "read_only")
        self.assertEqual(self.engine.prefs.get("cli_threshold"), 90)
        self.assertEqual(self.engine.prefs.get("cli_manual_order"), [terminal["id"]])   # PC ids mapped to this Mac's
        state = self.engine.snapshot()
        self.assertEqual(state["move_import"]["imports"][0]["code"], result["code"])
        self.assertFalse(next(a for a in state["accounts"] if a["id"] == waiting["id"])["switchable"])
        again = self.import_(path)                                             # importing again resumes, never duplicates
        self.assertTrue(again["complete"])
        self.assertEqual(len(store.accounts()), 4)
        self.assertEqual(vault.get(codex["id"]), self.codex_auth)

    def test_hand_over_fills_only_an_account_waiting_on_this_mac(self):
        self.import_(self.main_bundle())
        stranger = self.bundle([entry("pc-other", "claude_cli", new_uuid(), secret=self.cli_record)], kind="handover",
                               name="other.adeck")
        with self.assertRaises(DeckError):
            self.import_(stranger)
        handover = self.bundle([entry("pc-waiting", "claude_cli", self.waiting_identity, secret=self.waiting_record)],
                               kind="handover", name="handover.adeck")
        result = self.import_(handover)
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["counts"]["filled"], 1)
        waiting = self.engine.store.find("claude_cli", self.waiting_identity)
        self.assertEqual(waiting["owner"], "v2")
        self.assertEqual(self.engine.vault.get(waiting["id"])["credentials"], self.waiting_record["credentials"])
        self.assertEqual(len(self.engine.store.accounts()), 4)


if __name__ == "__main__":
    unittest.main()
