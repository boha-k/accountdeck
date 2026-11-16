"""Builds a released version's data folder for tests/test_data_compat.py, with that release's own code.

    git archive v2.1.0 | tar -x -C /tmp/ad-2.1.0
    ACCOUNTDECK_SAFE_MODE=1 python3.14 -E -s tests/fixtures/make_baseline_data.py /tmp/ad-2.1.0 [out.zip]

The checkout's `accountdeck/__init__.py` names the version; the zip goes to tests/fixtures/ under that version's
name (FIXTURES) unless `out.zip` is given. Everything is synthetic: no real account, file or Keychain item is
involved. The vault uses the Mac format (VaultCipher: ADK1 + AES-256-GCM) under the fixed test key VAULT_KEY.

2.0.0 (`baseline-data.zip`, committed on 2026-10-09 and kept byte for byte; built before this script made its
output reproducible): what a Mac install held after the PC→Mac move. The `typical` demo's saved accounts and
history (Claude Desktop, Claude Code terminals, Codex), plus rows the move and AccountDeck 1 left behind
(owners `on_pc`, `moved`, `v2-pending`, `v1`, `imported_from` "pc"/"v1", a deferred Desktop conversion), the
move-import ledger, a quarantined record and settings that no longer exist.

2.1.0 (`baseline-data-2.1.0.zip`): the same, saved by 2.1.0's code, plus what 2.1 keeps in the data folder:
Ultra and Max terminal accounts read with the answers Claude gives since 2026-11 (one with a locked weekly
limit, one at 100 %, one needing sign-in, one on hold), the terminals' live-login record (`cli-live`), a Codex
login whose refresh token may be spent (`codex-stash-<id>`), a terminal renewal a crash interrupted after
the renewed login was stashed (`cli-stash-<id>` holding it), the usage-shape memory (`usage_shapes`), the
Claude Code watch's memory (`claude_code_compat`), a "service" alert and a daily database backup (`backups/`).
Reproducible: the script runs itself again under PYTHONHASHSEED=0 with seeded randomness, a stopped demo
clock and fixed zip timestamps, so the same checkout writes the same bytes.

2.2.0 (`baseline-data-2.2.0.zip`, made from the tag v2.2.0 once it is released): 2.1's recipe on 2.2's code, plus
what 2.2 keeps in the data folder: the update check's setting and memory (`update_memory`), and Antigravity's
accounts in their own table (`antigravity_accounts`) with their `agy-<id>` records, one with a reading and an
older copy of its sign-in, one needing sign-in, and both Antigravity settings, all in the daily backup too.
"""
import copy
import hashlib
import io
import itertools
import os
from pathlib import Path
import random
import re
import secrets
import subprocess
import sys
import tempfile
import time
import zipfile

BASE_NOW = 1790000000.0
VAULT_KEY = hashlib.sha256(b"accountdeck-test-vault-key").digest()
HERE = Path(__file__).resolve().parent
FIXTURES = {"2.0.0": "baseline-data.zip", "2.1.0": "baseline-data-2.1.0.zip", "2.2.0": "baseline-data-2.2.0.zip"}
ZIP_TIME = (2026, 11, 5, 0, 0, 0)          # 2.1.0's release day; every entry carries it
WORK = "accountdeck-fixture"               # the temporary folder's name (vault records name no path, but logs might)


def version_of(checkout):
    found = re.search(r'^__version__ = "([^"]+)"', (Path(checkout) / "accountdeck" / "__init__.py").read_text(), re.M)
    if not found:
        raise SystemExit(f"{checkout}: accountdeck/__init__.py names no version")
    return found.group(1)


def main(checkout, out=None):
    checkout = Path(checkout).resolve()
    version = version_of(checkout)
    if version not in FIXTURES:
        raise SystemExit(f"no recipe for {version}: add one beside build_2_2 (it uses that release's own functions)")
    target = Path(out).resolve() if out else HERE / FIXTURES[version]
    if os.environ.get("PYTHONHASHSEED") != "0":          # set iteration order is part of what lands in the files
        env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "LANG") if key in os.environ}
        env.update(PYTHONHASHSEED="0", ACCOUNTDECK_SAFE_MODE="1")
        raise SystemExit(subprocess.run([sys.executable, "-s", "-B", __file__, str(checkout), str(target)],
                                        env=env, check=False).returncode)
    os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"
    sys.path.insert(0, str(checkout))
    with tempfile.TemporaryDirectory(prefix=WORK + "-") as temp:
        root = Path(temp) / "baseline"
        data = root / "data"
        _stop_the_world(data)
        {"2.0.0": build_2_0, "2.1.0": build_2_1, "2.2.0": build_2_2}[version](root, data)
        for name in ("accounts.sqlite3-wal", "accounts.sqlite3-shm"):
            (data / name).unlink(missing_ok=True)
        payload = pack(data, with_backups=version != "2.0.0")
    target.write_bytes(payload)
    print(f"wrote {target} ({len(payload)} bytes, sha256 {hashlib.sha256(payload).hexdigest()[:16]}) from {version}")


def _stop_the_world(data):
    """Only ever in this generator's own process, so a rebuild writes the same bytes: account ids (uuid4), the
    vault's nonces (os.urandom) and the demo's synthetic tokens (secrets) come from a seeded stream; every demo
    clock stands still at BASE_NOW, and so does the vault's own (it names quarantined records by the time); the
    demo saves into the real vault format under the fixed test key instead of its stand-in cipher."""
    counter = itertools.count()

    def urandom(size):
        out = b""
        while len(out) < size:
            out += hashlib.sha256(b"accountdeck-fixture/%d" % next(counter)).digest()
        return out[:size]
    os.urandom = urandom
    random.seed("accountdeck-fixture")
    secrets._sysrand = random.Random("accountdeck-fixture-secrets")

    from accountdeck import core, demo

    class StoppedTime:
        def __getattr__(self, name):
            return getattr(time, name)

        @staticmethod
        def time():
            return BASE_NOW
    core.time = StoppedTime()
    real_vault, real_clock = demo.Vault, demo.DemoClock
    demo.Vault = lambda path, protect=None: real_vault(path, protect=_cipher(data))
    demo.DemoClock = lambda start=None, speed=1.0: real_clock(start, speed=0.0)


def _cipher(data):
    from accountdeck.platform.keychain import VaultCipher
    found = VaultCipher(data)
    found._key = VAULT_KEY            # the fixed test key; no Keychain access
    return found


def _demo(root, scenario):
    """The release's own demo of `scenario`, not started (on the stopped world above)."""
    from accountdeck import demo
    return demo.build_demo(root, scenario, now=BASE_NOW, start=False, inline=True)


def leftovers(store, vault):
    """Rows the move and AccountDeck 1 left behind, the move-import ledger, a quarantined record and settings
    that no longer exist: what a Mac folder that went through the PC→Mac move still holds."""
    codex = next(row for row in store.accounts() if row["provider"] == "codex")
    claude = next(row for row in store.accounts() if row["provider"] == "claude")
    # A Claude Code login still signed in on the PC at export: a row without a secret.
    on_pc = store.save_account("claude_cli", "6f1d1c3a-1111-4b8e-9c1e-0a6c5e2d9f01", "Still on PC",
                               "pc@example.test", "max", "", imported_from="pc", owner="on_pc")
    store.update(on_pc, flags={"restorable": False, "excluded": True})
    # Rows AccountDeck 1 and the PC export marked (never on a Mac in practice; must still load).
    moved = store.save_account("codex", "acct-moved-0001", "Moved Codex", "moved@example.test", "plus", "",
                               owner="moved")
    vault.put(moved, vault.get(codex["id"]))
    pending = store.save_account("claude", "2b7f0e55-2222-4d0b-8a77-5c1f0e3d9a02", "From v1",
                                 "v1@example.test", "max", "", imported_from="v1", owner="v2-pending")
    store.update(pending, flags={"restorable": True})
    old = store.save_account("codex", "acct-v1-0002", "AccountDeck 1 Codex", "v1c@example.test", "plus", "",
                             imported_from="v1", owner="v1")
    # A Desktop account imported from the PC whose conversion still waits for Claude Desktop.
    waiting = store.save_account("claude", "3c8a1f66-3333-4e1c-9b88-6d2a1f4e0b03", "Waiting Desktop",
                                 "wait@example.test", "max", "", imported_from="pc")
    store.update(waiting, flags={"restorable": False, "deferred_desktop": "waiting"})
    vault.put("deferred-desktop-" + waiting, {"version": 1, "identity": "3c8a1f66-3333-4e1c-9b88-6d2a1f4e0b03",
                                              "captured": BASE_NOW - 7200,
                                              "portable": {"format": "accountdeck.claude-desktop.portable",
                                                           "profile": {"identity": "x"}}}, generation=False)
    store.update(claude["id"], imported_from="pc")
    vault.put("move-import", {"version": 1, "imports": [{
        "bundle_id": "0123456789abcdef0123456789abcdef", "kind": "main", "created": BASE_NOW - 9000,
        "at": BASE_NOW - 8000, "complete": True, "code": "ABCD-EFGH", "dismissed": False,
        "counts": {"new": 5, "updated": 0, "filled": 0, "kept": 0, "on_pc": 1, "deferred": 1, "failed": 0},
        "failures": [], "accounts": {"pc-1": claude["id"]}, "started": {}}]}, generation=False)
    vault.quarantine("codex-login", b"not json")
    for key, value in {"taskbar_widget": False, "taskbar_widget_gap": 240, "tray": True,
                       "claude_aumid": "Claude_pzs8sxrjxfjjc!Claude", "v1_retired_at": BASE_NOW - 86400,
                       "first_run_done": True, "v1_imported_at": BASE_NOW - 90000,
                       "cli_threshold": 90, "pinned_model": "Fable", "start_on_login": True}.items():
        store.set_setting(key, value)
    store.event("import", "Imported 6 accounts from the PC.")
    return {"on_pc": on_pc, "moved": moved, "pending": pending, "v1": old, "waiting": waiting}


def build_2_0(root, data):
    from accountdeck.core import Store, Vault
    _demo(root, "typical").stop()
    store = Store(data, clock=lambda: BASE_NOW)
    leftovers(store, Vault(data / "vault", protect=_cipher(data)))
    store.close()


def build_2_1(root, data, more=None):
    """`more(store, vault)`: what a later release adds, before the daily backup (build_2_2)."""
    from accountdeck import alerts, demo, doctor
    from accountdeck.core import Store, Vault
    from accountdeck.providers import codex as codex_provider
    from accountdeck.providers.claude_cli import fingerprint as cli_fingerprint, profile as cli_profile
    from accountdeck.usage import take_shape, watch_shape

    engine = _demo(root, "typical")
    store, vault, now = engine.store, engine.vault, BASE_NOW
    # Ultra and Max terminal accounts as 2.1 saves and reads them: the `mixed` demo's (Max near its 5-hour limit,
    # Ultra, Ultra on the temporary 5-hour limit, Ultra at 100 % weekly, Ultra needing sign-in) and the `ultra`
    # demo's locked one. Their builders only lend their recipes; everything lands in this engine's folder.
    shapes, added = None, {}
    for scenario, wanted in (("mixed", ("cli_alpha", "cli_beta", "cli_gamma", "cli_delta", "cli_epsilon")),
                             ("ultra", ("cli_delta",))):
        source = demo._Builder(root.parent / scenario, scenario, now, start=False, inline=True)
        for account in (a for a in source.accounts if a["id"] in wanted):
            label = "Locked Ultra" if scenario == "ultra" else f"{account['label']} {account['plan']}"
            # The demo names a login's tokens after its spec id, which the typical demo's terminals share: these get
            # their own, so each is a login of its own.
            lent = source._cli_profile(account)
            credentials, tag = copy.deepcopy(lent["credentials"]), f"{scenario}-{account['id']}"
            credentials["claudeAiOauth"].update(accessToken="demo-access-" + tag, refreshToken="demo-refresh-" + tag)
            credentials["trustedDeviceToken"] = "demo-device-" + tag
            profile = cli_profile(credentials, lent["oauthAccount"], now)
            profile.update(health=account.get("health", "ok"), excluded=account.get("excluded", False),
                           verified=profile["fingerprint"])
            key = engine.cli.save(profile, source="demo")
            source._state(engine, key, account)        # its reading and history, as 2.1 parses those answers
            seen = take_shape()                         # the shape of its last answer, as Engine._watch_shape takes it
            if seen is not None:
                shapes, _news = watch_shape(shapes, "claude_cli", key, seen, now)
            store.update(key, label=label)
            added[label] = key
    store.set_setting("usage_shapes", shapes)
    # Claude put "Beta Ultra" on hold (a renewal answered account_on_hold): 2.1 keeps the login and marks it.
    with engine.cli.gate(added["Beta Ultra"]):
        engine.cli._hold(added["Beta Ultra"], vault.get(added["Beta Ultra"]), now)
    # The login Claude Code holds, as the terminals' live-login record.
    alpha = next(row for row in store.accounts() if row["provider"] == "claude_cli" and row["label"] == "Alpha")
    engine.cli._note_live(alpha["id"], vault.get(alpha["id"])["fingerprint"])
    # A second Codex account, saved the way 2.1 saves one (signed in, then captured), with the `codex-blocked` demo's
    # reading of it; then the typical demo's own logins are signed back in. Its renewal may have spent its refresh
    # token: it needs signing in until another login is saved.
    typical = demo._Builder(root, "typical", now, start=False, inline=True)
    blocked = demo._Builder(root.parent / "codex-blocked", "codex-blocked", now, start=False, inline=True)
    typical._home(typical.ids["acc_1"], blocked.ids["acc_7"])
    spare = engine.accounts.capture("codex", label="Codex spare", claude_running=False)
    blocked._state(engine, spare, next(a for a in blocked.accounts if a["id"] == "acc_7"))
    typical._home(typical.ids["acc_1"], typical.ids["acc_6"])
    engine.sense_active()
    engine.accounts._mark_spent(store.account(spare), codex_provider.fingerprint(vault.get(spare)))
    # A terminal renewal the process died in after the renewed login was stashed and before it was saved: exactly
    # what CLIAccounts._refresh leaves at that point. The next start puts the stashed login in.
    delta = next(row for row in store.accounts() if row["provider"] == "claude_cli" and row["label"] == "Delta")
    saved = vault.get(delta["id"])
    saved["refresh_attempt"] = now
    vault.put(delta["id"], saved)
    credentials = dict(saved["credentials"])
    credentials["claudeAiOauth"] = dict(credentials["claudeAiOauth"], accessToken="demo-access-cli_delta-renewed",
                                        refreshToken="demo-refresh-cli_delta-renewed",
                                        expiresAt=int((now + 8 * 3600) * 1000))
    renewed = cli_fingerprint(credentials)
    vault.put("cli-stash-" + delta["id"], {"before": saved["fingerprint"], "result": dict(
        saved, credentials=credentials, fingerprint=renewed, refreshed=now, verified=renewed, health="ok")},
        generation=False)
    # The Claude Code watch's memory, a "service" alert, the events 2.1 writes and settings away from the defaults.
    store.set_setting("claude_code_compat", doctor.compat_step(None, "2.1.300", [], ["2.1.300"])[0])
    store.add_alert(alerts.restarted_itself("tick", now - 600))
    store.event("usage", "Beta Ultra no longer has a 5-hour limit")
    engine.update_settings({"cli_threshold": 88, "cli_order": "most_room", "alert_low_left": 20})
    engine.stop()

    store = Store(data, clock=lambda: BASE_NOW)
    vault = Vault(data / "vault", protect=_cipher(data))
    leftovers(store, vault)
    store.set_setting("cli_threshold", 88)        # leftovers() sets the 2.0 fixture's 90
    if more is not None:
        more(store, vault)
    if not store.backup():                        # the daily backup, holding all of the above
        raise SystemExit("the backup did not pass its check")
    store.close()


def build_2_2(root, data):
    build_2_1(root, data, more=keeps_2_2)


def keeps_2_2(store, vault):
    """What 2.2 adds, through its own storage (Store's agy_* methods, core.agy_key, providers.antigravity's
    saved_record): two Antigravity accounts, both Antigravity settings, the update check's setting and memory."""
    from accountdeck import updates
    from accountdeck.core import agy_key
    from accountdeck.providers.antigravity import saved_record

    def login(email, tag):
        return {"token": {"access_token": "demo-access-" + tag, "token_type": "Bearer",
                          "refresh_token": "demo-refresh-" + tag, "expiry": "2026-09-21T15:13:20Z"},
                "auth_method": "consumer", "id_token_email": email, "user_tier": "g1-pro-tier",
                "tier_display_name": "Google AI Pro", "writer_id": "demo-writer-" + tag}

    ada = store.agy_add("ada.fixture@example.test", plan="Google AI Pro", tier="g1-pro-tier", saved_at=BASE_NOW - 7200)
    for tag, saved_at in (("agy_ada", BASE_NOW - 7200), ("agy_ada-renewed", BASE_NOW - 3600)):   # an older copy stays
        vault.put(agy_key(ada), saved_record("ada.fixture@example.test", login("ada.fixture@example.test", tag),
                                             saved_at=saved_at))
    store.agy_update(ada, saved_at=BASE_NOW - 3600)
    store.agy_record_usage(ada, {"at": BASE_NOW - 600, "groups": [{"name": "Gemini Pro", "description": None, "buckets": [
        {"id": "gemini-pro-5h", "name": "5-hour limit", "window": "5h", "remaining": 0.6, "amount": None,
         "resets_at": BASE_NOW + 3600, "disabled": False},
        {"id": "gemini-pro-weekly", "name": "Weekly limit", "window": "weekly", "remaining": 0.25, "amount": None,
         "resets_at": BASE_NOW + 4 * 86400, "disabled": False}]}]}, at=BASE_NOW - 600)
    bob = store.agy_add("bob.fixture@example.test", label="Bob", saved_at=BASE_NOW - 86400)
    vault.put(agy_key(bob), saved_record("bob.fixture@example.test", login("bob.fixture@example.test", "agy_bob"),
                                         saved_at=BASE_NOW - 86400))
    store.agy_record_error(bob, "Sign in to this account again.", "reauth_required")
    store.set_setting("agy_usage_on_open", False)
    store.set_setting("agy_notice_seen", True)
    store.set_setting("update_check", False)
    store.set_setting(updates.MEMORY_KEY, updates.load_memory({"checked_at": BASE_NOW - 300, "tried_at": BASE_NOW - 300,
                                                               "latest": "2.2.0", "skipped": "2.2.0"}))


def pack(data, *, with_backups):
    """The data folder's database, vault (and backups) as a zip with fixed timestamps and modes."""
    keep = ("vault", "accounts.sqlite3") + (("backups",) if with_backups else ())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(data.rglob("*")):
            relative = path.relative_to(data)
            if path.is_file() and relative.parts[0] in keep:
                info = zipfile.ZipInfo(relative.as_posix(), ZIP_TIME)
                info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o600 << 16
                archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        raise SystemExit(__doc__)
    main(*sys.argv[1:])
