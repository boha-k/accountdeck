"""The encrypted vault: what keeps every saved sign-in. A regression here loses or leaks credentials."""
import os
import stat
import time
import unittest
from unittest import mock

from tests.helpers import FakeTerminal, SyntheticTestCase, codex_auth, new_uuid, terminal_login

from accountdeck.core import (AGY_ENROLLMENT, DatabaseDamaged, DeckError, Store, Vault, agy_key, agy_transaction_key,
                              recover_transactions)
from accountdeck.platform.keychain import VaultCipher
from accountdeck.providers.antigravity import saved_record
from accountdeck.providers.claude_cli import profile
from accountdeck.providers.codex import freshness, identity_key


def keyed_cipher(data_dir, key=None):
    """The Mac vault cipher with its key set directly (no Keychain access)."""
    cipher = VaultCipher(data_dir)
    cipher._key = key or os.urandom(32)
    return cipher


class VaultTests(SyntheticTestCase):
    def test_records_round_trip_encrypted_with_three_generations_and_fail_closed(self):
        vault = Vault(self.paths.data_dir / "vault", protect=keyed_cipher(self.paths.data_dir))
        for n in range(1, 5):
            vault.put("acc", {"token": f"secret-{n}"})
        self.assertEqual([vault.get("acc", g)["token"] for g in range(3)], ["secret-4", "secret-3", "secret-2"])
        self.assertEqual(len(vault.generations("acc")), 3)
        raw = vault.path("acc").read_bytes()
        self.assertTrue(raw.startswith(b"ADK1"))
        self.assertNotIn(b"secret-4", raw)
        # A record written again unchanged keeps the older generations (a repeated capture never evicts them).
        vault.put("acc", {"token": "secret-4"})
        self.assertEqual(vault.get("acc", 2)["token"], "secret-2")
        # Damaged bytes and a different key both fail closed with the "vault" code, never garbage.
        vault.path("acc").write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        with self.assertRaises(DeckError) as damaged:
            vault.get("acc")
        self.assertEqual(damaged.exception.code, "vault")
        other = Vault(self.paths.data_dir / "vault", protect=keyed_cipher(self.paths.data_dir))
        with self.assertRaises(DeckError) as foreign:
            other.get("acc", 1)
        self.assertEqual(foreign.exception.code, "vault")

    def test_guarded_codex_save_never_replaces_a_newer_refresh_token(self):
        vault = Vault(self.paths.data_dir / "vault", protect=keyed_cipher(self.paths.data_dir))
        now = time.time()
        older = codex_auth("acct-1", exp=now + 3600, refresh="rt-old")
        newer = codex_auth("acct-1", exp=now + 86400, refresh="rt-new")
        self.assertEqual(vault.put_guarded("c", newer, "probe", identity_key, freshness), "accepted")
        self.assertEqual(vault.put_guarded("c", older, "live", identity_key, freshness), "kept")
        self.assertEqual(vault.get("c")["tokens"]["refresh_token"], "rt-new")
        stranger = codex_auth("acct-2", exp=now + 9 * 86400, refresh="rt-other")
        self.assertEqual(vault.put_guarded("c", stranger, "live", identity_key, freshness), "conflict")
        self.assertEqual(vault.get("c")["tokens"]["refresh_token"], "rt-new")

    def test_an_older_terminal_login_never_replaces_a_newer_one(self):
        identity = new_uuid()
        older = terminal_login(identity, "a@example.test", token="older")
        newer = terminal_login(identity, "a@example.test", now=time.time() + 3600, token="newer")
        self.sign_in_terminal(*older)          # Claude Code holds the older login, e.g. put back from before a renewal
        engine = self.make_engine(owners={"refresh-older": identity, "refresh-newer": identity})
        key = self.save_terminal(engine, *newer)            # AccountDeck holds the newer one

        def saved():
            return engine.vault.get(key)["credentials"]["claudeAiOauth"]["refreshToken"]
        engine.cli.mirror()
        engine.cli.save(profile(*older, engine.clock()), source="sync-back")
        self.assertEqual(saved(), "refresh-newer")
        engine.cli.save(profile(*older, engine.clock()), source="enrollment")    # a sign-in always counts
        self.assertEqual(saved(), "refresh-older")


class AntigravityStorageTests(SyntheticTestCase):
    def test_antigravity_sign_ins_are_kept_apart_and_removing_one_removes_only_its_records(self):
        """Antigravity's accounts live in their own table and `agy-` records, where no other app's code and no older
        build looks: a database restore adopts a bare <32 hex> record as a Claude account, and every build blocks all
        switching on an open `transaction-` record. A regression here shows a sign-in as the wrong app's, blocks
        switching, starts empty beside saved sign-ins, or loses the wrong record on Remove."""
        root = self.paths.data_dir
        vault = Vault(root / "vault", protect=keyed_cipher(root))
        store = Store(root)
        codex = store.save_account("codex", "acct-1", "Codex", "c@example.test", "plus")
        vault.put(codex, codex_auth("acct-1", exp=time.time() + 3600, refresh="rt-codex"))
        live = {"token": {"access_token": "at-ada", "token_type": "Bearer", "refresh_token": "rt-ada",
                          "expiry": "2026-11-10T13:00:00Z"}, "auth_method": "consumer",
                "id_token_email": "Ada@Example.test", "user_tier": "g1-pro-tier", "tier_display_name": "Google AI Pro"}
        ada = store.agy_add("Ada@Example.test", plan="Google AI Pro", tier="g1-pro-tier", saved_at=100.0)
        vault.put(agy_key(ada), saved_record("Ada@Example.test", live, saved_at=100.0))
        renewed = {**live, "token": {**live["token"], "refresh_token": "rt-ada-2"}}
        vault.put(agy_key(ada), saved_record("ada@example.test", renewed, saved_at=150.0))
        bob = store.agy_add("bob@example.test", saved_at=200.0)
        vault.put(agy_key(bob), saved_record("bob@example.test", {**live, "id_token_email": "bob@example.test"},
                                             saved_at=200.0))
        switch, add = agy_transaction_key(), AGY_ENROLLMENT
        for n in range(2):          # unfinished, as a switch or an Add a crash interrupted leaves them
            vault.put(switch, {"status": "prepared", "account": bob, "n": n})
            vault.put(add, {"started": 300.0 + n})
        with self.assertRaises(DeckError):
            store.agy_add("ADA@example.test")       # one row per e-mail, whatever its case
        store.close()

        store = Store(root)          # written and read back, after a reopen
        self.addCleanup(store.close)
        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual([(row["id"], row["email"], row["plan"]) for row in store.agy_accounts()],
                         [(ada, "ada@example.test", "Google AI Pro"), (bob, "bob@example.test", None)])
        self.assertEqual(vault.get(agy_key(ada))["stores"]["cli"], renewed)
        self.assertEqual(vault.get(agy_key(ada), 1)["stores"]["cli"], live)       # the usual ring of copies
        self.assertEqual([len(vault.generations(key)) for key in (switch, add)], [1, 1])      # one copy each
        # Never another app's: not an account row, not an account record, not a switch to recover.
        self.assertEqual([row["id"] for row in store.accounts()], [codex])
        self.assertEqual(vault.account_keys(), {codex})
        self.assertEqual(vault.agy_account_keys(), {agy_key(ada), agy_key(bob)})
        self.assertEqual(vault.agy_transaction_keys(), [switch])
        self.assertEqual(recover_transactions(vault), [])

        # Remove: the row and every copy of its record go; nothing else does.
        self.assertTrue(store.agy_remove(ada))
        vault.remove(agy_key(ada))
        self.assertIsNone(store.agy_account(ada))
        self.assertEqual(vault.generations(agy_key(ada)), [])
        self.assertEqual([row["id"] for row in store.agy_accounts()], [bob])
        self.assertEqual(vault.agy_account_keys(), {agy_key(bob)})
        self.assertEqual(vault.get(agy_key(bob))["email"], "bob@example.test")
        self.assertEqual(vault.get(codex)["tokens"]["refresh_token"], "rt-codex")
        self.assertEqual(store.account(codex)["provider"], "codex")
        self.assertEqual(vault.get(switch)["n"], 1)
        self.assertEqual(vault.get(add)["started"], 301.0)

        # A database gone beside nothing but an Antigravity sign-in is not a first start.
        other = root.parent / "only-antigravity"
        Vault(other / "vault", protect=keyed_cipher(other)).put(agy_key(bob), {"v": 1})
        with self.assertRaises(DatabaseDamaged):
            Store(other)


class DamagedDatabaseTests(SyntheticTestCase):
    """A damaged database must never lose a login, send a refresh token twice or start empty beside the vault."""

    def _terminal(self, engine, name, *, now=None):
        identity = new_uuid()
        login = terminal_login(identity, f"{name}@example.test", now=now, token=name)
        self.owners[f"refresh-{name}"] = identity
        return self.save_terminal(engine, *login), identity

    def test_a_damaged_database_goes_back_to_its_backup_and_every_login_follows_the_vault(self):
        self.owners = {}
        engine = self.make_engine(owners=self.owners)
        now = time.time()
        renewed, renewed_identity = self._terminal(engine, "renewed")
        uncertain, _ = self._terminal(engine, "uncertain", now=now - 9 * 3600)   # its access token is over: a renewal is due
        removed, _ = self._terminal(engine, "removed")
        self.assertTrue(engine.store.backup())
        folder = self.paths.data_dir / "backups"
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual([stat.S_IMODE(path.stat().st_mode) for path in folder.iterdir()], [0o600])
        # After the backup: a renewal rotates a token, another leaves it uncertain, one account comes, one goes.
        newer = terminal_login(renewed_identity, "renewed@example.test", now=now + 3600, token="renewed2")
        login_end = int((now + 20 * 86400) * 1000)
        newer[0]["claudeAiOauth"]["refreshTokenExpiresAt"] = login_end
        self.owners["refresh-renewed2"] = renewed_identity
        engine.cli.save(profile(*newer, now), source="renewal")
        engine.vault.put("cli-stash-" + uncertain, {"before": engine.vault.get(uncertain)["fingerprint"], "result": None},
                         generation=False)
        added, _ = self._terminal(engine, "added")
        engine.accounts.remove(removed)
        engine.stop()
        path = self.paths.data_dir / "accounts.sqlite3"
        raw = path.read_bytes()
        damaged = raw[:4096] + b"\xff" * (len(raw) - 4096)
        path.write_bytes(damaged)

        with mock.patch.object(FakeTerminal, "refresh", side_effect=AssertionError("a refresh token was sent")) as refresh:
            engine = self.make_engine(owners=self.owners)
            self.assertEqual(next(folder.glob("damaged-*/accounts.sqlite3")).read_bytes(), damaged)   # moved aside, whole
            self.assertEqual(len(list(folder.glob("accounts-*"))), 1)
            rows = {row["id"]: row for row in engine.store.accounts()}
            # The rows say what the vault says, not what they said at the backup.
            saved = engine.vault.get(renewed)
            self.assertEqual(saved["credentials"]["claudeAiOauth"]["refreshToken"], "refresh-renewed2")
            self.assertEqual(rows[renewed]["flags"]["login_ends_at"], login_end / 1000)
            self.assertEqual(rows[renewed]["expires_at"], newer[0]["claudeAiOauth"]["expiresAt"] / 1000)
            # The account added since is back under its own key, so `mirror` will not save it as a new one.
            self.assertEqual((rows[added]["provider"], rows[added]["email"]), ("claude_cli", "added@example.test"))
            # A renewal left uncertain stays so: its token may be spent, so it asks for sign-in and is never sent.
            self.assertEqual((rows[uncertain]["flags"]["health"], rows[uncertain]["credential_state"]),
                             ("needs_signin", "reauth_required"))
            self.assertEqual(engine.vault.get(uncertain)["health"], "needs_signin")
            # An account removed since shows up again and asks for sign-in rather than claim a login.
            self.assertEqual((rows[removed]["flags"]["restorable"], rows[removed]["credential_state"]),
                             (False, "reauth_required"))
            engine.tick()
            engine.tick()
            refresh.assert_not_called()
        engine.announce()
        alert = next(a for a in engine.store.alerts() if a["kind"] == "service")
        self.assertEqual(alert["level"], "critical")
        self.assertIn("readings", alert["message"])
        self.assertIn("1 saved login(s) could not be read", alert["message"])
        self.assertIn("1 account(s) added since the backup", alert["message"])
        self.assertEqual(engine.service_state()["db"]["restored_from"], engine._restored["backup_at"])
        engine.stop()
        engine = self.make_engine(owners=self.owners)      # said once; the next start is an ordinary one
        engine.announce()
        self.assertEqual(sum(a["kind"] == "service" for a in engine.store.alerts()), 1)
        self.assertIsNone(engine._restored)

    def test_no_good_backup_stops_the_start_and_changes_nothing(self):
        self.owners = {}
        engine = self.make_engine(owners=self.owners)
        self._terminal(engine, "kept")
        engine.stop()
        data, path = self.paths.data_dir, self.paths.data_dir / "accounts.sqlite3"

        def files():
            return {str(item): item.read_bytes() for item in data.rglob("*") if item.is_file()}
        path.write_bytes(b"this is no database " * 2048)
        (data / "backups").mkdir()
        (data / "backups" / "accounts-20260101T000000Z.sqlite3.gz").write_bytes(b"a backup cut short")
        for attempt in ("damaged", "missing", "empty"):
            if attempt == "missing":
                path.unlink()
            elif attempt == "empty":
                path.write_bytes(b"")
            before = files()
            with self.assertRaises(DatabaseDamaged):
                Store(data)
            self.assertEqual(files(), before)


if __name__ == "__main__":
    unittest.main()
