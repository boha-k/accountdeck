"""Data saved by every released AccountDeck 2 must load in this build with nothing lost, and each released build
must still open a folder this build has used: an update never strands a saved account, and a rollback (install.sh
puts the previous app back when the new one fails its health check) finds the folder it left.

One data folder per release, made with that release's own code by tests/fixtures/make_baseline_data.py (see its
docstring for what each holds), under the fixed test vault key below. The old builds come from
ACCOUNTDECK_BASELINES: checkouts separated by ":", each naming its version in accountdeck/__init__.py
(tools/check.sh makes them from released tags with git archive where a checkout has them; 2.0.0 and 2.1.0
predate this repository's history).

What this build adds to a folder is Antigravity's data: a table of its own inside schema 2 (the one change the
schema check allows), `agy-` vault records and two settings. The old builds must open a folder holding every kind of
it and see none of it: an Antigravity account among theirs stops all their usage reads, and an open `transaction-`
record blocks all their switching.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import unittest
import zipfile

from tests.helpers import SyntheticTestCase
from tests.test_frozen_identifiers import AGY_TABLE_SQL

from accountdeck import resets, updates
from accountdeck.core import AGY_ENROLLMENT, DeckError, Store, Vault, agy_key, agy_transaction_key
from accountdeck.platform.keychain import VaultCipher
from accountdeck.providers.antigravity import saved_record

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VAULT_KEY = hashlib.sha256(b"accountdeck-test-vault-key").digest()
LEGACY_SETTINGS = ("taskbar_widget", "taskbar_widget_gap", "claude_aumid", "v1_retired_at", "first_run_done")
OWNERS = {"v2", "on_pc", "moved", "v2-pending", "v1"}


@dataclass(frozen=True)
class Release:
    fixture: str
    rows: int                  # account rows
    saved: int                 # rows of this Mac's own (owner v2) with a saved sign-in
    threshold: int             # the cli_threshold setting it saved


RELEASES = {"2.0.0": Release("baseline-data.zip", rows=16, saved=11, threshold=90),
            "2.1.0": Release("baseline-data-2.1.0.zip", rows=23, saved=18, threshold=88),
            "2.2.0": Release("baseline-data-2.2.0.zip", rows=23, saved=18, threshold=88)}


def version_key(text):
    return tuple(int(part) for part in text.split("."))


def baselines():
    """[(version, checkout)] of the old builds ACCOUNTDECK_BASELINES names."""
    found = []
    for folder in filter(None, os.environ.get("ACCOUNTDECK_BASELINES", "").split(":")):
        init = Path(folder) / "accountdeck" / "__init__.py"
        named = re.search(r'^__version__ = "([^"]+)"', init.read_text(encoding="utf-8"), re.M) if init.is_file() else None
        if not named:
            raise AssertionError(f"ACCOUNTDECK_BASELINES: {folder} is not an AccountDeck checkout naming its version")
        found.append((named.group(1), Path(folder)))
    return found


def record_digests(vault):
    """{file name: digest of what it decrypts to} for every vault file, older generations too, but the quarantined
    ones (those hold raw bytes); and every record's current copy must parse."""
    found = {path.name: hashlib.sha256(vault.protect(path.read_bytes(), True)).hexdigest()
             for path in sorted(vault.root.glob("*.dpapi")) if not path.name.startswith("quarantine-")}
    for key in {name.split(".")[0] for name in found}:
        vault.get(key)
    return found


def keyed_vault(data):
    cipher = VaultCipher(data)
    cipher._key = VAULT_KEY                                  # the fixture's key; no Keychain access
    return Vault(data / "vault", protect=cipher)


def write_antigravity(engine):
    """Every kind of data this build keeps for Antigravity, through its own storage: two accounts (one with a reading,
    one with an error), a saved sign-in with an older copy beside it, an unfinished switch and Add, both settings.
    Returns the accounts' ids."""
    store, vault = engine.store, engine.vault
    login = {"token": {"access_token": "synthetic-access", "token_type": "Bearer", "refresh_token": "synthetic-refresh",
                       "expiry": "2026-11-10T13:00:00Z"}, "auth_method": "consumer",
             "id_token_email": "ada@example.test", "user_tier": "g1-pro-tier", "tier_display_name": "Google AI Pro"}
    ada = store.agy_add("ada@example.test", plan="Google AI Pro", tier="g1-pro-tier", saved_at=1.0)
    for n in (1.0, 2.0):                                     # the second write keeps the first as an older copy
        vault.put(agy_key(ada), saved_record("ada@example.test", dict(login, writer_id=n), saved_at=n))
    store.agy_record_usage(ada, {"at": 3.0, "groups": [{"name": "Gemini", "description": None, "buckets": [
        {"id": "g5h", "name": "5-hour", "window": "5h", "remaining": 0.5, "amount": None, "resets_at": 9.0,
         "disabled": False}]}]}, at=3.0)
    bob = store.agy_add("bob@example.test", saved_at=4.0)
    vault.put(agy_key(bob), saved_record("bob@example.test", dict(login, id_token_email="bob@example.test"),
                                         saved_at=4.0))
    store.agy_record_error(bob, "Sign in again.", "reauth_required")
    vault.put(agy_transaction_key(), {"status": "prepared", "provider": "antigravity", "account": bob, "before": None})
    vault.put(AGY_ENROLLMENT, {"started": 5.0, "before": None})
    engine.update_settings({"agy_usage_on_open": False})
    engine.prefs.set_internal("agy_notice_seen", True)
    return {ada, bob}


def antigravity_data(data):
    """What this build finds of Antigravity in a data folder, read without acting on it (an engine would settle the
    unfinished switch and Add): its rows, every copy of its records, its settings."""
    store = Store(data)
    try:
        return (store.agy_accounts(), [store.setting(name) for name in ("agy_usage_on_open", "agy_notice_seen")],
                {name: found for name, found in record_digests(keyed_vault(data)).items() if name.startswith("agy-")})
    finally:
        store.close()


# Run by an old build, in its own checkout: what it reads of the folder, and whether its engine starts on it, lists
# its targets, ticks and makes a state its own contract accepts (in safe mode an invalid state raises AssertionError).
OLD_BUILD_READS = """
import hashlib, json, sys
from pathlib import Path
from accountdeck.config import Paths
from accountdeck.core import Store, Vault
from accountdeck.engine import Engine
from accountdeck.platform.keychain import VaultCipher
from accountdeck.procs import FakeProcessController
from accountdeck.providers.claude_cli import ClaudeCLI
from accountdeck.providers.fake import FakeClaude, FakeCodex
paths = Paths.synthetic(Path(sys.argv[1]))
cipher = VaultCipher(paths.data_dir)
cipher._key = bytes.fromhex(sys.argv[2])
vault = Vault(paths.data_dir / "vault", protect=cipher)
store = Store(paths.data_dir)
files = sorted(p for p in vault.root.glob("*.dpapi") if not p.name.startswith("quarantine-"))
seen = {"rows": {r["id"]: [r["provider"], r["identity"], r["owner"], r["label"]] for r in store.accounts()},
        "records": {p.name: hashlib.sha256(vault.protect(p.read_bytes(), True)).hexdigest() for p in files},
        "threshold": store.setting("cli_threshold")}
for key in {p.name.split(".")[0] for p in files}:
    vault.get(key)
store.close()
engine = Engine(paths, providers={"claude": FakeClaude(paths), "codex": FakeCodex(paths), "claude_cli": ClaudeCLI(paths)},
                procs=FakeProcessController(paths), vault=vault, status_sources={}, start=False, inline=True)
try:
    seen["targets"] = sorted(target.id for target in engine.targets())
    engine.tick()
    seen["labels"] = {a["id"]: a["label"] for a in engine.snapshot()["accounts"]}
    seen["recovery"], seen["failing"] = list(engine.recovery), sorted(engine.health()["failing_phases"])
finally:
    engine.stop()
seen["saved_after"] = {p.name: hashlib.sha256(vault.protect(p.read_bytes(), True)).hexdigest()
                       for p in vault.root.glob("agy-*.dpapi")
                       if not p.name.startswith(("agy-transaction-", "agy-enrollment"))}
print(json.dumps(seen))
"""


class ReleaseData:
    """The tests for one release's data folder; a subclass names the release."""
    VERSION = None

    def setUp(self):
        super().setUp()
        self.release = RELEASES[self.VERSION]
        with zipfile.ZipFile(FIXTURES / self.release.fixture) as archive:
            archive.extractall(self.paths.data_dir)
        self.schema = self.read_schema()
        self.backups = len(list((self.paths.data_dir / "backups").glob("accounts-*.sqlite3.gz")))
        self.engine = self.make_engine(vault=keyed_vault(self.paths.data_dir))

    def read_schema(self):
        db = sqlite3.connect(self.paths.data_dir / "accounts.sqlite3")
        try:
            return (db.execute("PRAGMA user_version").fetchone()[0],
                    sorted(db.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()))
        finally:
            db.close()

    def expected_schema(self):
        """The fixture's schema with Antigravity's table beside it, and nothing else."""
        version, objects = self.schema
        return version, sorted(set(objects) | {("antigravity_accounts", AGY_TABLE_SQL)})

    def test_this_build_loads_every_account_secret_and_setting(self):
        engine, release = self.engine, self.release
        rows = engine.store.accounts()
        self.assertEqual(len(rows), release.rows)
        self.assertEqual({row["owner"] for row in rows}, OWNERS)
        records = {name.split(".")[0] for name in record_digests(engine.vault)}     # every record decrypts
        saved = {row["id"] for row in rows if row["owner"] == "v2" and not row["flags"].get("deferred_desktop")}
        self.assertEqual(len(saved), release.saved)
        self.assertTrue(saved <= records)
        state = engine.snapshot()                            # safe mode: an invalid state raises AssertionError
        self.assertTrue(saved <= {a["id"] for a in state["accounts"]})
        frozen = {row["id"] for row in rows if row["owner"] in ("on_pc", "moved")}
        self.assertEqual(len(frozen), 2)
        self.assertFalse(any(a["switchable"] for a in state["accounts"] if a["id"] in frozen))
        self.assertFalse(frozen & {target.id for target in engine.targets()})
        for key in frozen:
            with self.assertRaises(DeckError):
                engine.action("switch-plan", {"id": key})
        self.assertEqual(state["move_import"]["deferred"]["count"], 1)
        self.assertEqual(state["move_import"]["imports"][0]["code"], "ABCD-EFGH")
        settings = engine.settings()
        self.assertEqual((settings["cli_threshold"], settings["pinned_model"], settings["tray"]),
                         (release.threshold, "Fable", True))
        self.assertFalse(set(LEGACY_SETTINGS) & set(settings))
        self.assertEqual(engine.store.setting("claude_aumid"), "Claude_pzs8sxrjxfjjc!Claude")   # kept, not deleted
        # Antigravity's table, added inside schema 2 the first time this build opens the folder, after a backup.
        added = "antigravity_accounts" not in dict(self.schema[1])
        self.assertEqual(engine.store.migrated, {"added": "antigravity_accounts", "backup": "ok"} if added else None)
        self.assertEqual(engine.store.care()["backups"], self.backups + added)
        self.assertEqual(self.read_schema(), self.expected_schema())

    def test_the_old_builds_still_open_the_folder_afterwards(self):
        builds = baselines()
        if not builds:
            self.skipTest("no old build to check: ACCOUNTDECK_BASELINES names none (tools/check.sh makes them from "
                          "released tags)")
        engine = self.engine
        # What only this build writes is in the folder too: the update check's setting and its internal memory, and
        # every kind of Antigravity data.
        engine.update_settings({"cli_threshold": 92, "update_check": False})
        memory = {"checked_at": 1790000000.0, "tried_at": 1790000600.0, "failed": True, "latest": "9.9.9",
                  "notified": "9.9.9", "skipped": "9.9.9", "failures": 3, "wait_until": 1790003600.0,
                  "fallback_at": 1790000300.0}
        engine.prefs.set_internal(updates.MEMORY_KEY, memory)
        self.assertEqual((engine.store.setting("update_check"), engine.store.setting(updates.MEMORY_KEY)), (False, memory))
        key = next(row["id"] for row in engine.store.accounts() if row["provider"] == "codex" and row["owner"] == "v2")
        engine.accounts.rename(key, "Renamed here")
        # Limit resets: the setting, and what the service keeps of them (resets.MEMORY): a Claude block and an open
        # request key, as a use whose answer was lost leaves them.
        engine.update_settings({"claude_resets": False})
        resets_memory = {"accounts": {key: {
            "claude": {"checked": 1790000000.0, "tried": 1790000000.0, "failures": 0, "block": {
                "eligible": True, "reason": None, "at_limit": False, "exhausted": [], "cooldown_until": None, "next": None,
                "grants": [{"id": "grant_weekly", "kind": "weekly", "label": None, "left": 1, "total": 1, "starts": None,
                            "ends": 4070908800.0, "paused": False, "usable_now": True, "needs_limit": False,
                            "clears": ["seven_day"], "blocking": [], "unavailable": None}]}},
            "pending": {"key": "6f1c2d3e-4a5b-4c6d-8e7f-001122334455", "provider": "codex", "row": "c-0123456789ab",
                        "target": "RateLimitResetCredit_00", "made": 1790000000.0, "until": 1790086400.0, "before": 1,
                        "sent": True, "since": 1790000000.0}}}}
        engine.store.set_setting(resets.MEMORY, resets_memory)
        agy_ids = write_antigravity(engine)
        self.assertTrue(engine.store.backup())               # this build's daily backup lands beside the database
        here = {"rows": {r["id"]: [r["provider"], r["identity"], r["owner"], r["label"]] for r in engine.store.accounts()},
                "records": record_digests(engine.vault), "threshold": 92}
        engine.stop()
        self.assertEqual(self.read_schema(), self.expected_schema())
        agy = antigravity_data(self.paths.data_dir)
        checked = 0
        for version, checkout in builds:
            # A build is asked to open the data of its own release and earlier: a rollback only ever goes back to the
            # build that ran the folder before. (A 2.0.0 build does read every row and secret of 2.1.0 data and starts
            # on it, but its state contract refuses the "service" alerts 2.1 added: its live mode logs that once.)
            if version_key(version) < version_key(self.VERSION):
                continue
            with self.subTest(old_build=version):
                root = self.paths.temp_root.with_name("after-" + version)        # a copy of its own: its tick writes
                shutil.copytree(self.paths.temp_root, root)
                result = subprocess.run([sys.executable, "-E", "-s", "-B", "-c", OLD_BUILD_READS, str(root),
                                         VAULT_KEY.hex()],
                                        cwd=checkout, capture_output=True, text=True, timeout=120, check=False,
                                        env={**os.environ, "ACCOUNTDECK_SAFE_MODE": "1"})
                self.assertEqual(result.returncode, 0, result.stderr[-3000:])
                seen = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(seen["rows"], here["rows"])
                self.assertEqual(seen["records"], here["records"])     # every secret, as this build left it
                self.assertEqual(seen["threshold"], 92)
                self.assertEqual(seen["labels"][key], "Renamed here")
                # Antigravity stays outside the other apps' state and scheduler, and the tick remains healthy.
                # Pre-2.2 builds leave all of its data alone; newer builds can settle its recovery journals.
                self.assertFalse(agy_ids & (set(seen["labels"]) | set(seen["targets"])))
                self.assertEqual(seen["failing"], [])
                after = antigravity_data(root / self.paths.data_dir.relative_to(self.paths.temp_root))
                if version_key(version) < (2, 2, 0):
                    self.assertEqual(seen["recovery"], [])
                    self.assertEqual(after, agy)
                else:
                    # 2.2 knows Antigravity and may settle its unfinished operations at startup.
                    # The saved accounts, settings and every generation of each login must remain.
                    self.assertEqual(after[:2], agy[:2])
                    saved = {name: value for name, value in agy[2].items()
                             if not name.startswith(("agy-transaction-", "agy-enrollment"))}
                    self.assertEqual(seen["saved_after"], saved)
                checked += 1
        if not checked:
            self.skipTest(f"no old build given opens {self.VERSION} data")


class Release200Data(ReleaseData, SyntheticTestCase):
    VERSION = "2.0.0"


class Release210Data(ReleaseData, SyntheticTestCase):
    VERSION = "2.1.0"

    def test_what_2_1_keeps_survives_the_load(self):
        """The records 2.1 added keep their meaning: an account on hold stays on hold, a Codex login whose refresh
        token may be spent is never handed to Codex, an interrupted renewal's stashed login becomes the saved one."""
        engine, vault = self.engine, self.engine.vault
        rows = {(row["provider"], row["label"]): row for row in engine.store.accounts()}
        self.assertEqual(sum(row["plan"] == "Ultra" for row in rows.values()), 5)
        held = rows["claude_cli", "Beta Ultra"]
        self.assertEqual((held["flags"]["health"], vault.get(held["id"])["health"]), ("on_hold", "on_hold"))
        self.assertEqual(next(a for a in engine.snapshot()["accounts"] if a["id"] == held["id"])["health"], "on_hold")
        spent = rows["codex", "Codex spare"]
        self.assertEqual(spent["credential_state"], "reauth_required")
        self.assertTrue(vault.get("codex-stash-" + spent["id"])["spent"])
        with self.assertRaises(DeckError) as refused:
            engine.accounts.codex_profile(spent)
        self.assertEqual(refused.exception.code, "reauth_required")
        renewed = rows["claude_cli", "Delta"]["id"]
        self.assertFalse(vault.path("cli-stash-" + renewed).exists())               # adopted as the engine started
        self.assertEqual(vault.get(renewed)["credentials"]["claudeAiOauth"]["refreshToken"], "demo-refresh-cli_delta-renewed")
        self.assertEqual(vault.get("cli-live")["id"], rows["claude_cli", "Alpha"]["id"])
        # Its daily backup is kept, beside the one this build takes before it first adds its table.
        self.assertEqual(engine.store.care()["backups"], self.backups + 1)
        self.assertEqual(engine.store.setting("claude_code_compat")["passed"], "2.1.300")
        self.assertIn("claude_cli", engine.store.setting("usage_shapes"))


class Release220Data(ReleaseData, SyntheticTestCase):
    VERSION = "2.2.0"

    def test_antigravity_signins_and_update_settings_survive(self):
        engine = self.engine
        expected = {"ada.fixture@example.test", "bob.fixture@example.test"}
        rows = [row for row in engine.store.agy_accounts() if row["email"] in expected]
        self.assertEqual({row["email"] for row in rows}, expected)
        for row in rows:
            record = engine.vault.get(agy_key(row["id"]))
            self.assertEqual(record["email"], row["email"])
            self.assertTrue(record["stores"]["cli"]["token"]["refresh_token"].startswith("demo-refresh-"))
        self.assertFalse(engine.store.setting("agy_usage_on_open"))
        self.assertTrue(engine.store.setting("agy_notice_seen"))
        self.assertFalse(engine.store.setting("update_check"))
        self.assertEqual(engine.store.setting(updates.MEMORY_KEY)["latest"], "2.2.0")


if __name__ == "__main__":
    unittest.main()
