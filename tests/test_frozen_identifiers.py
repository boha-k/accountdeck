"""The names an installed AccountDeck 2 finds its data by. An update keeps every saved account only because these
never changed since the build that brought them: the database, the vault's records and their Keychain key, the
support folder, the owner lock, the port and the app's identity, and Antigravity's own table, records and settings.
They are spelled out here on purpose, so that changing one takes a deliberate edit of this file too.
"""
from pathlib import Path
import re
import sqlite3
import unittest
from unittest import mock

import tests.helpers  # noqa: F401  (safe mode before any accountdeck module loads)

from accountdeck import config, core
from accountdeck.platform import keychain
from accountdeck.providers import antigravity

REPO = Path(__file__).resolve().parent.parent
# Antigravity's table as sqlite keeps it (sqlite_master.sql). tests/test_data_compat.py allows this table, and nothing
# else, beside the schema an older release left.
AGY_TABLE_SQL = ("CREATE TABLE antigravity_accounts(id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,\n"
                 "  label TEXT NOT NULL, plan TEXT, tier TEXT, created REAL NOT NULL, updated REAL NOT NULL,\n"
                 "  sort_order INTEGER NOT NULL DEFAULT 0, saved_at REAL, usage TEXT, usage_at REAL, error TEXT, "
                 "error_code TEXT,\n  flags TEXT)")
STRANDS = ("{what} is frozen at {expected!r} but is now {actual!r}. Every existing install finds its saved accounts, "
           "their Keychain key or its running service by this value: changing it strands every existing user's saved "
           "accounts or breaks their update. It needs a migration, a compatibility fixture (tests/test_data_compat.py) "
           "and the maintainer's yes.")


class FrozenIdentifierTests(unittest.TestCase):
    def frozen(self, actual, expected, what):
        self.assertEqual(actual, expected, STRANDS.format(what=what, expected=expected, actual=actual))

    def shell_values(self, path, name):
        """Every value `name` is assigned in a shell script (indented assignments too)."""
        text = (REPO / path).read_text(encoding="utf-8")
        return sorted({found.group(2) for found in re.finditer(
            rf'^\s*{name}=("?)([^"\n#]*?)\1\s*(?:#.*)?$', text, re.M)}) or [None]

    def python_value(self, path, name):
        found = re.search(rf'^{name} = "([^"]*)"', (REPO / path).read_text(encoding="utf-8"), re.M)
        return found.group(1) if found else None

    def test_the_data_formats(self):
        self.frozen(core.SCHEMA_VERSION, 2, "The database schema version (core.SCHEMA_VERSION)")
        self.frozen(core.DB_FILE, "accounts.sqlite3", "The database file name (core.DB_FILE)")
        self.frozen(core.VAULT_SUFFIX, ".dpapi", "The vault record extension (core.VAULT_SUFFIX)")
        self.frozen(keychain.VAULT_MAGIC, b"ADK1", "The vault record header (keychain.VAULT_MAGIC)")
        self.frozen(keychain.VAULT_SERVICE, "AccountDeck2 vault", "The vault key's Keychain service (keychain.VAULT_SERVICE)")
        self.frozen(keychain.vault_account(Path("/Users/someone/Library/Application Support/AccountDeck2")),
                    "7563086ba0b92934", "The vault key's Keychain account for a data folder (keychain.vault_account)")

    def test_antigravity_data(self):
        """Antigravity's accounts are kept apart from the other apps' (an older build stops reading every account when
        it meets an app it does not know), under names of their own, frozen from the release that ships them."""
        db = sqlite3.connect(":memory:")
        db.execute(core._AGY_TABLE)
        self.frozen(db.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall(),
                    [("antigravity_accounts", AGY_TABLE_SQL)], "Antigravity's table and its columns (core._AGY_TABLE)")
        db.close()
        self.frozen(core.AGY_TABLE, "antigravity_accounts", "Antigravity's table name (core.AGY_TABLE)")
        self.frozen(core.agy_key("0123456789abcdef" * 2), "agy-0123456789abcdef0123456789abcdef",
                    "A saved Antigravity sign-in's vault record (core.agy_key)")
        self.frozen(re.sub(r"[0-9a-f]{32}\Z", "<32 hex>", core.agy_transaction_key()), "agy-transaction-<32 hex>",
                    "An unfinished Antigravity switch's vault record (core.agy_transaction_key)")
        self.frozen(core.AGY_ENROLLMENT, "agy-enrollment",
                    "An unfinished Antigravity Add's vault record (core.AGY_ENROLLMENT)")
        self.frozen(antigravity.PROVIDER, "antigravity",
                    "Antigravity's app id in stored records (providers.antigravity.PROVIDER)")
        login = {"token": {"refresh_token": "synthetic-refresh"}, "auth_method": "consumer",
                 "id_token_email": "Ada@Example.test"}
        self.frozen(antigravity.saved_record("Ada@Example.test", login, saved_at=1.0),
                    {"v": 1, "email": "ada@example.test", "saved_at": 1.0, "stores": {"cli": login}},
                    "A saved Antigravity sign-in's record and its version (providers.antigravity.saved_record)")
        self.frozen({name: getattr(config.SETTINGS_SCHEMA.get(name), "kind", None)
                     for name in ("agy_usage_on_open", "agy_notice_seen")},
                    {"agy_usage_on_open": "bool", "agy_notice_seen": "bool"},
                    "Antigravity's settings (config.SETTINGS_SCHEMA)")

    def test_where_an_install_keeps_them(self):
        with mock.patch.dict("os.environ", {"ACCOUNTDECK2_DATA": ""}):
            data = config._mac_paths(Path("/Users/someone"), applications=()).data_dir
        self.frozen(data, Path("/Users/someone/Library/Application Support/AccountDeck2"),
                    "The data folder (config._mac_paths)")
        self.frozen(config.OWNER_LOCK_NAME, "AccountDeck2-Owner.lock", "The owner lock's name (config.OWNER_LOCK_NAME)")
        self.frozen(config.DEFAULT_PORT, 53157, "The service's port (config.DEFAULT_PORT)")
        support = "$base/Library/Application Support/AccountDeck2"
        self.frozen(self.shell_values("install.sh", "SUPPORT"), [support], "install.sh's support folder (SUPPORT)")
        for script, names in (("install.sh", ("BUNDLE_ID", "LABEL", "APP_NAME", "EXECUTABLE")),
                              ("uninstall.sh", ("BUNDLE_ID", "LABEL", "APP_NAME"))):
            for name in names:
                expected = {"APP_NAME": "AccountDeck 2", "EXECUTABLE": "AccountDeck2"}.get(name, "local.accountdeck2")
                self.frozen(self.shell_values(script, name), [expected], f"{script}'s {name}")
        for name in ("BUNDLE_ID", "LABEL"):
            self.frozen(self.python_value("accountdeck/platform/autostart_mac.py", name), "local.accountdeck2",
                        f"autostart_mac.py's {name}")


if __name__ == "__main__":
    unittest.main()
