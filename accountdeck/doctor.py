"""The one-command health report: `run.py --doctor [--json]`, and the same checks behind `state.health`.

A check is {"id", "level", "text", "action"}: level ok, warn or fail, one plain sentence, and what to do (None
when nothing is). A fail means AccountDeck cannot do its job; the report's `ok` is "no fail".

It only reads, with the service up or down, and never shows a secret, an email or any personal value:
- the service through GET /_health on the port in runtime.json (nothing else of that file is kept);
- the database read-only: `mode=ro` while a service may write it, else `immutable=1` (see _Database);
- the Keychain item by its attributes only: `security find-generic-password` without -w or -g reads no secret and
  asks nothing, also on a locked keychain (measured on macOS 27 with a locked test keychain: rc 0 in 10 ms);
- Claude Code's files by key names and classes, nothing executed: the version comes from the ~/.local/bin/claude
  link or the session files.
- Antigravity's agy: whether it is installed (file checks), its sign-in store by the keychain item's attributes only
  (or, inside the service, as the service last sensed it), a file store only by whether the file exists; the saved
  accounts by id, label and reading time, and whether each one's vault record file exists (never opened). agy's
  version is the one the service remembers: the service runs `agy --version` once for each installed binary when the
  user looks or acts (AgyService.check_version), never for this report, which says "not checked yet" until then.
  The report run by hand shows no version.
Only `launchctl print`/`print-disabled` and `security` run, through procs. Nothing is written or downloaded.

`watch` is the Claude Code compatibility watch on top of the report (`Engine._doctor_cycle`).
"""
from __future__ import annotations

from collections import Counter
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import time
import urllib.request

from . import BUILD, __version__, activity, alerts, build_text, cli_auto, cli_policy, config, core, diagnostics, procs
from .config import SETTINGS_SCHEMA, Settings, safe_mode
from .platform import autostart_mac as agent, credstore, keychain as keychains
from .rules import age_text
from .resets import MEMORY as RESETS_MEMORY
from .usage import claude_usage, has_weekly, report_status, reset_rows, shape_change

# Claude Code's formats: a fail in one of these is what the compatibility watch reports. Not cc.usage_answers: those
# come from Claude's servers whatever the version, and AlertEngine.usage_report already has its one alert for them.
STRUCTURAL = frozenset({"cc.config", "cc.login", "cc.projects", "cc.sessions", "cc.usage_cache"})
COMPAT_KEY = "claude_code_compat"       # the watch's memory in the store: {"seen", "passed", "alerted", "strikes"}
STRIKES = 2                             # cycles a fail must last before it is reported: a file caught mid-write isn't news
# What else the app reads in oauthAccount (accountUuid is the one it cannot do without).
ACCOUNT_KEYS = ("emailAddress", "organizationUuid", "planDisplayName", "organizationRateLimitTier", "organizationType")
SESSION_KEYS = ("status", "version")
SERVICE_TIMEOUT, KEYCHAIN_TIMEOUT = 2.0, 5.0
PHASE_FAIL = 3                          # a phase failing this many ticks in a row is a fail, fewer a warning
READ_OLD = 3600.0                       # a failing account's reading older than this is worth a warning
DISK_WARN, DISK_FAIL = 5 * 1024 ** 3, 512 * 1024 ** 2
LOG_SPAN = 3600.0
VERSION = re.compile(r"\d+\.\d+\.\d+[\w.+-]*")
PINNED = re.compile(r"cpython-(\d+\.\d+\.\d+)")
OPEN_APP = "Open AccountDeck 2 from Applications."
REOPEN = "Choose Quit in the AccountDeck 2 menu bar menu, then open it from Applications."
REINSTALL = "Run install.sh again from the AccountDeck 2 package."
START_AT_LOGIN = "Turn on Open at login in Settings → Menu Bar & Login."
UPDATE = "Update AccountDeck 2; until then check that terminal switches still work."
DAMAGED = "Quit and reopen AccountDeck 2: at its start it replaces a damaged database with its newest good backup."
NOTIFICATION_SETTINGS = "Allow AccountDeck 2 in System Settings › Notifications, or reinstall if macOS rejected this copy."
STALL_WORDS = {"tick": "its tick loop has not finished a tick for 30 seconds",
               "claude_cli_reads": "no terminal account was read successfully for 20 minutes"}


def _check(key, level, text, action=None):
    return {"id": key, "level": level, "text": text, "action": action}


def _number(value):
    return value if type(value) in (int, float) else None


def _date(ts):
    moment = dt.datetime.fromtimestamp(ts)
    return f"{moment:%b} {moment.day}"


class _Database:
    """The store's two read paths on a read-only connection. `quiet`: no service can be writing the file (none runs
    here and there is no runtime.json); with an empty -wal it was then closed cleanly and opens `immutable=1`, as a
    `mode=ro` open of a cleanly closed WAL database creates -wal and -shm files and leaves them (measured). Anything
    else opens `mode=ro`: immutable would read a file that a checkpoint may be changing."""

    def __init__(self, path, quiet):
        wal = Path(str(path) + "-wal")
        self.db = core._read_only(path, immutable=quiet and not (wal.is_file() and wal.stat().st_size > 0))
        self.db.row_factory = sqlite3.Row

    def accounts(self):
        return [core._account(row) for row in self.db.execute(core._SELECT_ACCOUNTS + " ORDER BY provider, created, rowid")]

    def setting(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def close(self):
        self.db.close()


class _View:
    """What the checks read: a running engine's own view, or the disk and /_health."""

    def __init__(self, paths, engine, keychain):
        self.paths, self.engine, self.keychain = paths, engine, keychain
        self.now = engine.clock() if engine is not None else time.time()
        self.runtime = _runtime_file(paths.data_dir)
        self.store, self.database, self.store_error = None, None, None
        self.db_file = paths.data_dir / "accounts.sqlite3"
        if engine is not None:
            state = engine.health()
            self.body, self.down = {"app": config.APP, "version": __version__, "build": BUILD, "stalled": state["stalled"],
                                    "engine": state, "db": engine.store.care()}, None
            self.store = engine.store
        else:
            self.body, self.down = _fetch_health(self.runtime)
            if self.db_file.exists():
                try:
                    self.store = self.database = _Database(self.db_file, quiet=not self.runtime)
                    self.store.setting(COMPAT_KEY)             # the open is lazy: fail here, not in every section
                except (sqlite3.Error, ValueError) as exc:
                    self.close()
                    self.store, self.store_error = None, type(exc).__name__
        self.config, self.config_problem = _claude_config(paths)
        self._terminals = None

    def terminals(self):
        if self._terminals is None:
            self._terminals = [] if self.store is None else [row for row in self.store.accounts()
                                                             if row["provider"] == "claude_cli"]
        return self._terminals

    def live_engine(self):
        """The `engine` block of /_health."""
        block = (self.body or {}).get("engine")
        return block if isinstance(block, dict) else {}

    def close(self):
        if self.database is not None:
            self.database.close()
            self.database = None


def _runtime_file(data_dir):
    """runtime.json without its token."""
    try:
        value = json.loads((Path(data_dir) / "runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {key: value.get(key) for key in ("app", "host", "port", "pid")} if isinstance(value, dict) else {}


def _fetch_health(runtime):
    """(the /_health answer, None) or (None, why not as a sentence)."""
    port = runtime.get("port")
    if not runtime:
        return None, "The service is not running: there is no runtime.json."
    if (runtime.get("app") != config.APP or runtime.get("host") != "127.0.0.1"
            or type(port) is not int or not 1 <= port <= 65535):
        return None, "runtime.json does not name a running service."
    if safe_mode() and port in (config.DEFAULT_PORT, config.V1_PORT):     # tests and demos never contact the live one
        return None, f"Safe mode does not contact port {port}."
    # Loopback requests must never be routed through a system or environment proxy.
    local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with local.open(f"http://127.0.0.1:{port}/_health", timeout=SERVICE_TIMEOUT) as response:
            body = json.load(response)
    except (OSError, ValueError, http.client.HTTPException):
        return None, f"Nothing answers /_health on port {port}, the port in runtime.json."
    if not isinstance(body, dict) or body.get("app") != config.APP:
        return None, f"Something other than AccountDeck answers on port {port}."
    return body, None


def _claude_config(paths):
    """(~/.claude.json as an object or None, None | "missing" | "unreadable")."""
    try:
        return core.read_json(paths.claude_user_config), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "unreadable"


# ---- the sections ----

def _service(v):
    yield _agent(v)
    if v.body is None:
        yield _check("service.running", "fail", v.down, OPEN_APP)
        return
    live, version = v.live_engine(), v.body.get("version")
    port = v.runtime.get("port")
    where = "runs" if port is None else f"answers on port {port}"
    if version == __version__ or v.engine is not None:
        yield _check("service.running", "ok", f"AccountDeck {version} ({build_text(v.body.get('build'))}) {where}.")
    else:
        yield _check("service.running", "warn", f"AccountDeck {version} {where}, but the code here is {__version__}.", REOPEN)
    age = _number(live.get("tick_age_s"))
    if v.body.get("stalled"):
        why = ", ".join(STALL_WORDS.get(reason, str(reason)) for reason in live.get("stall_reasons") or ()) or "reported by itself"
        yield _check("service.ticks", "fail", f"The service has stalled: {why}.", REOPEN)
    elif age is None:
        yield _check("service.ticks", "warn", "The service has not finished its first tick yet.", "Check again in a minute.")
    else:
        yield _check("service.ticks", "ok", f"Its ticks are moving: the last one finished {age:.0f} s ago.")
    failing = live.get("failing_phases") if isinstance(live.get("failing_phases"), dict) else {}
    if failing:
        worst = max((_number(entry.get("count")) or 0 for entry in failing.values()), default=0)
        names = ", ".join(f"{name} ({entry.get('error_type')}, {entry.get('count')} in a row)" for name, entry in sorted(failing.items()))
        yield _check("service.phases", "fail" if worst >= PHASE_FAIL else "warn", f"Tick phases are failing: {names}.",
                     "The log (logs/accountdeck2.log in the data folder) has where; " + REOPEN)
    else:
        yield _check("service.phases", "ok", "No tick phase is failing.")


def _agent(v):
    try:
        layout = agent.Layout.installed() if v.paths.mode == "live" else agent.Layout.sandbox(v.paths.home)
    except core.DeckError:      # a checkout has no install record: the usual place
        layout = agent.Layout.sandbox(v.paths.home, manage=True)
    launchctl = agent.Launchctl() if layout.manage else None
    status = agent.status(layout=layout, launchctl=launchctl)
    if not status.registered:
        return _check("service.agent", "warn", "Open at login is off, so nothing restarts AccountDeck after a crash or a reboot.",
                      START_AT_LOGIN)
    if status.approved is False:
        return _check("service.agent", "warn", "macOS has AccountDeck's login item switched off.",
                      "Switch it on in System Settings › General › Login Items.")
    if launchctl is None:
        return _check("service.agent", "ok", "The LaunchAgent file is in place.")
    loaded, pid = agent._job(launchctl)
    if not loaded:
        return _check("service.agent", "warn", "The LaunchAgent file is in place but not loaded; it loads at the next login.",
                      "Nothing to do unless this stays so after the next login.")
    if pid is None and v.body is None:
        return _check("service.agent", "warn", "The LaunchAgent is loaded but runs nothing.", OPEN_APP)
    return _check("service.agent", "ok", "The LaunchAgent is loaded" + (f" and runs pid {pid}." if pid else "."))


def _runtime(v):
    running = sys.version.split()[0]
    if sys.version_info < (3, 14):
        yield _check("runtime.python", "fail", f"Python {running} is too old: AccountDeck needs 3.14.", REINSTALL)
    else:
        yield _python(v.paths, running)
    try:
        from cryptography import __version__ as version
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        cipher = AESGCM(AESGCM.generate_key(256))
        if cipher.decrypt(nonce, cipher.encrypt(nonce, b"doctor", b""), b"") != b"doctor":
            raise ValueError("round trip")
    except Exception as exc:
        yield _check("runtime.crypto", "fail", f"cryptography does not work here ({type(exc).__name__}).", REINSTALL)
    else:
        yield _check("runtime.crypto", "ok", f"cryptography {version} works.")
    yield _data_folder(v.paths)


def _python(paths, running):
    runtime = paths.data_dir / "runtime"
    try:
        found = PINNED.search((runtime / "PINS").read_text(encoding="utf-8"))
    except OSError:
        found = None
    if found is None:
        if paths.mode == "live":
            return _check("runtime.python", "fail", "The pinned runtime is missing from the data folder.", REINSTALL)
        return _check("runtime.python", "ok", f"Python {running} (no installed runtime on this synthetic root).")
    if not os.access(runtime / "python" / "bin" / "python3.14", os.X_OK):
        return _check("runtime.python", "fail", "The pinned runtime's python3.14 is missing.", REINSTALL)
    if found.group(1) != running:
        return _check("runtime.python", "warn", f"This report ran on Python {running}; the pinned runtime is {found.group(1)}.",
                      "Run it with the installed runtime for a full answer.")
    return _check("runtime.python", "ok", f"Python {running}, the pinned runtime.")


def _data_folder(paths):
    try:
        info = paths.data_dir.stat()
    except OSError:
        return _check("runtime.data", "fail", "The data folder does not exist.", REINSTALL)
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != os.getuid():
        return _check("runtime.data", "fail", "The data folder belongs to another user.", "Give it back to your user with chown.")
    if mode & 0o077 and paths.mode == "live":      # a synthetic or demo root is the temp folder's
        return _check("runtime.data", "warn", f"Others may read the data folder (mode {mode:o}).",
                      "chmod 700 the data folder; AccountDeck does this itself at its next start.")
    return _check("runtime.data", "ok", "The data folder is private to you.")


def _database(v):
    if v.store_error:
        yield _check("db", "fail", f"The database could not be opened ({v.store_error}).", REOPEN)
        return
    if not v.db_file.exists():
        yield _check("db", "fail" if v.paths.mode == "live" else "warn", "There is no database yet.", OPEN_APP)
        return
    # What the service says about its file (Store.care, the `db` block of /_health), else a check of our own.
    care = (v.body or {}).get("db")
    care = care if isinstance(care, dict) else {}
    checked = care.get("quick_check") if isinstance(care.get("quick_check"), dict) else {}
    database = _Database(v.db_file, quiet=v.engine is None and not v.runtime)
    try:
        version = database.db.execute("PRAGMA user_version").fetchone()[0]
        result = checked.get("result") or ("ok" if database.db.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok" else "damaged")
    except sqlite3.DatabaseError as exc:
        yield _check("db", "fail", f"The database cannot be read ({type(exc).__name__}).", DAMAGED)
        return
    finally:
        database.close()
    if version != core.SCHEMA_VERSION:
        yield _check("db", "fail", f"The database has schema {version}; this code expects {core.SCHEMA_VERSION}.", REINSTALL)
    elif result == "damaged":
        yield _check("db", "fail", "The database fails its integrity check.", DAMAGED)
    elif result != "ok":
        yield _check("db", "warn", "The database's integrity check could not run.", "Check again in a minute.")
    else:
        size = v.db_file.stat().st_size // (1024 * 1024)
        yield _check("db", "ok", f"The database ({size} MB, schema {version}) passes its integrity check.")


def claude_code_version(paths):
    """(installed version or None, the link exists, versions of the registered sessions), nothing executed."""
    link = paths.home / ".local" / "bin" / "claude"
    try:
        name = Path(os.readlink(link)).name
    except OSError:
        name = ""
    running = {row["version"] for row in activity.read_registry(paths).values() if isinstance(row.get("version"), str)}
    if VERSION.fullmatch(name):
        return name, True, running
    newest = max(running, key=activity._version_key) if running else None
    return newest, os.path.lexists(link), running


def _login_item(paths, keychain):
    """Where Claude Code's login is, by name only: "keychain", "file", "none", "two" or "unknown" (module docstring).
    A synthetic home without a test keychain has the file only, like credstore.for_paths."""
    if paths.mode != "live" and keychain is None:
        return "file" if paths.claude_cli_credentials.exists() else "none"
    found = 0
    for account in credstore.account_candidates():
        try:
            rc, _out, _err = procs.run_security(["find-generic-password", "-a", account, "-s", credstore.SERVICE,
                                                 *([] if keychain is None else [str(keychain)])],
                                                keychain=keychain, timeout=KEYCHAIN_TIMEOUT)
        except (TimeoutError, OSError):
            return "unknown"
        if rc == 0:
            found += 1
        elif rc != keychains.RC_MISSING:
            return "unknown"
    if found:
        return "two" if found > 1 else "keychain"
    return "file" if paths.claude_cli_credentials.exists() else "none"


def _claude_code(v):
    version, linked, _running = claude_code_version(v.paths)
    terminals = v.terminals()
    passed = (v.store.setting(COMPAT_KEY) or {}).get("passed") if v.store is not None else None
    if version is None:
        yield _check("cc.binary", "fail" if terminals else "warn",
                     "Claude Code was not found: no ~/.local/bin/claude link, and no session reports a version.",
                     "Install Claude Code, or open a session once.")
    else:
        tail = f"; {passed} last passed these checks" if passed and passed != version else ""
        yield _check("cc.binary", "ok", f"Claude Code {version} is installed{tail}." if linked
                     else f"Claude Code {version} is in use{tail} (no ~/.local/bin/claude link).")
    if config.cli_custom_home():
        yield _check("cc.custom_home", "warn", "A custom CLAUDE_CONFIG_DIR makes terminal management read-only in this environment.",
                     "Unset CLAUDE_CONFIG_DIR and CLAUDE_SECURESTORAGE_CONFIG_DIR.")
    login = _login_item(v.paths, v.keychain)
    yield from _config_checks(v.config, v.config_problem, login, terminals)
    yield _sessions(v.paths)
    yield _projects(v.paths)
    yield _usage_cache(v.config, v.now)
    yield _usage_answers(v)


def _config_checks(value, problem, login, terminals):
    if problem == "unreadable" or (problem is None and not isinstance(value, dict)):
        yield _check("cc.config", "fail", "~/.claude.json can no longer be read as a JSON object.", UPDATE)
        return
    account = None if value is None else value.get("oauthAccount")
    if account is None and login == "none":                 # signed out: nothing changed its format
        yield _check("cc.signed_out", "fail" if terminals else "warn", "Claude Code is not signed in on this Mac.",
                     "Choose Sign In Again on a terminal account in AccountDeck." if terminals else "Sign in to Claude Code.")
        return
    if account is None:
        yield _check("cc.config", "fail", "Claude Code has a login, but ~/.claude.json has no oauthAccount.", UPDATE)
    elif not isinstance(account, dict) or not isinstance(account.get("accountUuid"), str) or not account["accountUuid"]:
        yield _check("cc.config", "fail", "oauthAccount in ~/.claude.json no longer holds an accountUuid.", UPDATE)
    else:
        missing = [key for key in ACCOUNT_KEYS if key not in account]
        yield (_check("cc.config", "warn", f"oauthAccount in ~/.claude.json has no {', '.join(missing)}.",
                      "Sign in again if plan names look wrong.") if missing
               else _check("cc.config", "ok", "~/.claude.json has oauthAccount with the keys AccountDeck reads."))
    if login == "two":
        yield _check("cc.login", "fail", credstore.AMBIGUOUS, "Remove the extra item in Keychain Access.")
    elif login == "unknown":
        yield _check("cc.login", "warn", f"macOS did not say whether the {credstore.SERVICE} Keychain item exists.",
                     "Check again in a minute.")
    elif login == "none":
        yield _check("cc.login", "fail", f"~/.claude.json names an account, but there is no {credstore.SERVICE} Keychain item "
                     "and no credentials file.", UPDATE)
    else:
        yield _check("cc.login", "ok", f"The {credstore.SERVICE} Keychain item exists (its secret was not read)." if login == "keychain"
                     else "Claude Code keeps its login in ~/.claude/.credentials.json.")


def _sessions(paths):
    try:
        names = [entry.name for entry in os.scandir(paths.claude_sessions) if activity.REGISTRY_FILE.fullmatch(entry.name)]
    except OSError:
        names = []
    rows = activity.read_registry(paths)
    if names and not rows:
        return _check("cc.sessions", "fail", f"None of the {len(names)} files in ~/.claude/sessions can be read as a session any more.", UPDATE)
    missing = [key for key in SESSION_KEYS if rows and not any(key in row for row in rows.values())]
    if missing:
        return _check("cc.sessions", "fail", f"Claude Code's session files no longer have {', '.join(missing)}.", UPDATE)
    count = "1 Claude Code session is" if len(rows) == 1 else f"{len(rows)} Claude Code sessions are"
    return _check("cc.sessions", "ok", f"{count} registered, in the expected format." if rows
                  else "No Claude Code session is registered now.")


def _projects(paths):
    try:
        with os.scandir(paths.claude_projects) as entries:
            has = any(entries)
    except OSError:
        return _check("cc.projects", "fail" if activity.read_registry(paths) else "warn", "~/.claude/projects is not there.", UPDATE)
    return _check("cc.projects", "ok", "~/.claude/projects is there." if has else "~/.claude/projects is there, still empty.")


def _usage_cache(value, now):
    cached = value.get("cachedUsageUtilization") if isinstance(value, dict) else None
    if cached is None:
        return _check("cc.usage_cache", "ok", "Claude Code has not cached a usage answer yet.")
    if isinstance(cached, dict):
        utilization = cached.get("utilization")
        missing = [key for key, good in (("fetchedAtMs", _number(cached.get("fetchedAtMs")) is not None),
                                         ("accountUuid", isinstance(cached.get("accountUuid"), str)),
                                         ("utilization", isinstance(utilization, dict))) if not good]
        if not missing:
            try:
                if not has_weekly(claude_usage(utilization, now=now)):
                    missing = ["a weekly limit"]
            except Exception:         # what the service's reads parse with: it would fail on this answer too
                missing = ["a shape AccountDeck can read"]
    else:
        missing = ["an object"]
    if missing:
        return _check("cc.usage_cache", "fail", f"cachedUsageUtilization in ~/.claude.json no longer has {', '.join(missing)}.", UPDATE)
    return _check("cc.usage_cache", "ok", "Claude Code's cached usage answer has the expected shape.")


def _usage_answers(v):
    """Claude's usage answers as the service saw them (usage.watch_shape keeps that in the store). One account's answer
    without a weekly limit is that account's plan, and its own row says so: only answers that no longer tell the limits
    are a fail. Names only: these are the answers' key names, never their values."""
    memory = v.store.setting("usage_shapes") if v.store is not None else None
    status, change = report_status(memory, v.now), shape_change(memory, "claude_cli", v.now)
    if status["down"]:
        return _check("cc.usage_answers", "fail", "Claude's usage answers no longer tell the limits; the last readings are kept.", UPDATE)
    if change and change.get("removed"):
        return _check("cc.usage_answers", "warn", f"Claude's usage answers lost {', '.join(change['removed'][:4])} in the last week.",
                      "Nothing to do unless readings look wrong.")
    if status["kinds"]:
        return _check("cc.usage_answers", "warn", f"Claude's usage answers have limits with no rule here: {', '.join(status['kinds'][:4])}.",
                      "They count for your terminals until AccountDeck is updated.")
    reads = (v.live_engine().get("reads") or {}).get("claude_cli")
    if isinstance(reads, dict):
        return _check("cc.usage_answers", "ok", f"The service read {reads.get('ok', 0)} terminal usage answers since it started "
                      f"({reads.get('failed', 0)} failed), in the shape it expects.")
    return _check("cc.usage_answers", "ok", "The usage answers last seen have the shape AccountDeck expects.")


def _accounts(v):
    rows = v.terminals()
    if v.engine is not None:
        active = v.engine.active_identity.get("claude_cli")
    else:
        active = (v.config or {}).get("oauthAccount", {}).get("accountUuid") if isinstance((v.config or {}).get("oauthAccount"), dict) else None
    for row in rows:
        if not core.is_frozen(row):
            yield _account_line(row, row["identity"] == active, v.now)
    yield _takeover(v, rows, active)


def _account_line(row, active, now):
    flags, label = row["flags"], diagnostics.redact_text(row["label"])
    left_out, health = bool(flags.get("excluded")), flags.get("health", "ok")
    ends = _number(flags.get("login_ends_at"))
    age = None if row["last_good_at"] is None else max(0, now - row["last_good_at"])
    failing = row["error_code"] is not None or (row["paused_until"] or 0) > now
    parts = ["reads failing" if failing else "reads ok", "no reading yet" if age is None else f"last reading {age_text(age)} ago"]
    level, action = "ok", None
    sign_in = f"Choose Sign In Again for {label} in AccountDeck."
    if age is None or (failing and age > READ_OLD):
        level = "warn"
    if ends is not None:
        parts.append(f"login ended {_date(ends)}" if ends <= now else f"login ends {_date(ends)}")
        if ends - now <= alerts.LOGIN_AHEAD:
            level, action = "warn", sign_in
    if health == "on_hold":
        parts.append("on hold")
        level, action = "warn", "Claude put this account on hold; signing in again does not help."
    elif health != "ok" or row["credential_state"] == "reauth_required":
        parts.append("needs sign-in")
        level, action = "warn", sign_in
    dead = health != "ok" or row["credential_state"] == "reauth_required" or (ends is not None and ends <= now)
    if active and dead:
        level = "fail"
    if left_out and not active:
        parts.append("left out")
        level, action = "ok", None           # the user left it out on purpose: nothing about it is news
    in_use = " (in use)" if active else ""
    return _check(f"account.{row['id']}", level, f"{label}{in_use}: {', '.join(parts)}.", action)


def _policy_rows(rows, active):
    """cli_auto.inputs without an engine; the burn-rate forecast and `out` only move the active account's trigger,
    never who can take over."""
    return [{"id": row["id"], "label": row["label"], "active": row["identity"] == active, "forecast": {}, "owner": row["owner"],
             "excluded": core.is_frozen(row) or row["flags"].get("excluded", False), "health": row["flags"].get("health", "ok"),
             "live_expired": row["flags"].get("live_expired", False), "out": False, "paused_until": row["paused_until"],
             "read_failed": row["error_code"] is not None, "login_ends_at": row["flags"].get("login_ends_at"),
             "last_good_at": row["last_good_at"], "windows": (row["last_good_usage"] or {}).get("windows", [])} for row in rows]


def _takeover(v, rows, active):
    """The active account, auto-switch on or why it is paused, and how many accounts could take over now."""
    if not rows:
        return _check("accounts.autoswitch", "ok", "No terminal account is saved.")
    if v.engine is not None:
        settings, inputs, paused = v.engine.prefs.public(), cli_auto.inputs(v.engine), cli_auto.paused(v.engine)
    else:
        stored = {key: value for key in SETTINGS_SCHEMA if (value := v.store.setting(key)) is not None}
        settings, inputs = Settings(stored, home=v.paths.home).public(), _policy_rows(rows, active)
        paused = (v.live_engine().get("autoswitch") or {}).get("paused")
    result = cli_policy.evaluate(inputs, settings, v.store.setting("cli_history", {}), v.now)
    ready = [row for row in result["next_up"] if row["skip"] is None]
    current = next((row["label"] for row in rows if row["identity"] == active), None)
    who = f"Terminals use {diagnostics.redact_text(current)}" if current else "Terminals use no saved account"
    count = f"{len(ready)} account{' could' if len(ready) == 1 else 's could'} take over now"
    if ready:
        count += ": " + ", ".join(diagnostics.redact_text(row["label"]) for row in ready[:5]) + (f" and {len(ready) - 5} more" if len(ready) > 5 else "")
    if not settings["cli_autoswitch"]:
        return _check("accounts.autoswitch", "warn", f"{who}; auto-switch is off; {count}.", "Turn on Auto-switch in the Claude Code section.")
    if v.body is None:
        return _check("accounts.autoswitch", "warn", f"{who}; auto-switch cannot act while the service is down; {count}.", OPEN_APP)
    if paused:                              # it can name an account: masked like the labels
        return _check("accounts.autoswitch", "warn", f"{who}; auto-switch is paused: {diagnostics.redact_text(str(paused))}; {count}.",
                      "It resumes by itself once that clears.")
    if not ready:
        why = "; ".join(f"{diagnostics.redact_text(row['label'])}: {row['skip']}" for row in result["next_up"] if row["skip"]
                        and row["label"] != current)[:300]
        return _check("accounts.autoswitch", "warn", f"{who}; auto-switch is on, but no account could take over now ({why or 'no other account'}).",
                      "Sign in again or add a terminal account.")
    if current is None:
        return _check("accounts.autoswitch", "warn", f"{who}; auto-switch is on; {count}.", "Switch terminals to a saved account in AccountDeck.")
    return _check("accounts.autoswitch", "ok", f"{who}; auto-switch is on; {count}.")


def _machine(v):
    yield _notifications(v)
    free = shutil.disk_usage(v.paths.data_dir if v.paths.data_dir.exists() else v.paths.home).free
    level = "fail" if free < DISK_FAIL else "warn" if free < DISK_WARN else "ok"
    yield _check("machine.disk", level, f"{free / 1024 ** 3:.1f} GB of disk space is free.",
                 None if level == "ok" else "Free some disk space: a full disk stops saving logins and readings.")
    yield _log(v.paths.data_dir / "logs")


def _notifications(v):
    state = v.engine._notification_state() if v.engine is not None else v.live_engine().get("notifications")
    if state in ("authorized", "provisional"):
        return _check("machine.notifications", "ok", "macOS allows AccountDeck's notifications.")
    if state in ("denied", "rejected", "pending", "not_determined"):
        return _check("machine.notifications", "warn", f"macOS has not allowed AccountDeck's notifications ({state.replace('_', ' ')}).",
                      NOTIFICATION_SETTINGS)
    if state == "unavailable" and v.paths.mode == "live":
        return _check("machine.notifications", "warn", "The menu bar item is not running, so no notification can be shown.", REOPEN)
    return _check("machine.notifications", "ok", "Notifications cannot be asked about from here.")


def _log(folder):
    """Warnings and errors of the last hour by event name (the log is secret-free; only names are shown)."""
    counts, since = Counter(), time.time() - LOG_SPAN
    for name in (diagnostics.LOG_FILE, diagnostics.LOG_FILE + ".1"):
        path = folder / name
        try:
            if path.stat().st_mtime < since:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"lvl":"warning"' not in line and '"lvl":"error"' not in line:
                continue
            try:
                entry = json.loads(line)
                at = dt.datetime.strptime(entry["t"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
            except (ValueError, KeyError, TypeError):
                continue
            if at >= since:
                counts[str(entry.get("ev"))[:60]] += 1
    if not counts:
        return _check("machine.log", "ok", "The log has no warnings from the last hour.")
    listed = ", ".join(f"{name} ×{count}" for name, count in counts.most_common(6))
    return _check("machine.log", "warn", f"The log has {sum(counts.values())} warnings from the last hour: {listed}.",
                  "Look at logs/accountdeck2.log in the data folder if this keeps growing.")


def _antigravity(v):
    """agy found and its version, its sign-in store, the saved accounts and whether each still has its saved
    sign-in, the last usage run (PLAN §11 of the Antigravity work). Never a fail: Antigravity is optional, and none
    of it stops AccountDeck's job for the other apps."""
    from .providers.antigravity import FILE_STORE, NOT_CHECKED, STORE_ACCOUNT, STORE_SERVICE, Antigravity, KeychainStore
    engine = v.engine
    adapter = engine.agy.adapter if engine is not None else Antigravity(v.paths)
    exe, rows = adapter.executable(), _agy_rows(v)
    if exe is None and not rows:
        yield _check("agy.binary", "ok", "agy, Antigravity's command-line tool, is not installed.")
        return
    count = f"{len(rows)} Antigravity account{'' if len(rows) == 1 else 's'}"
    if exe is None:
        yield _check("agy.binary", "warn", f"agy is not installed, but {count} {'is' if len(rows) == 1 else 'are'} saved.",
                     "Install agy again to switch to them or read their usage.")
    else:
        checked, version = adapter.known_version() if engine is not None else (True, None)
        place = str(exe).replace(str(v.paths.home), "~", 1)
        yield _check("agy.binary", "ok", f"agy {version} is installed ({place})." if version
                     else f"agy is installed ({place}); its version is not checked yet." if not checked
                     else f"agy is installed ({place}).")
    if engine is not None:
        found = engine.agy.found
        state, reason, message = found.state, found.reason, found.message
    elif adapter.file_store() is not None:
        state, reason, message = "unknown", "file_store", FILE_STORE
    elif v.paths.mode != "live" and v.keychain is None:
        state, reason, message = "unknown", "not_checked", NOT_CHECKED
    else:
        try:
            item = KeychainStore("cli", STORE_SERVICE, STORE_ACCOUNT, keychain=v.keychain).probe()
            state, reason, message = ("absent" if item is None else "present"), None, None
        except core.DeckError as exc:
            state, reason, message = "unknown", "unreadable", str(exc)
    if state == "absent":
        yield _check("agy.store", "ok", "Nobody is signed in to agy on this Mac.")
    elif state == "ok":
        yield _check("agy.store", "ok", "agy is signed in with a personal Google account AccountDeck can save and switch.")
    elif state == "present":
        yield _check("agy.store", "ok", "agy's sign-in is in the keychain (looked at by its attributes only).")
    elif reason == "not_checked":
        yield _check("agy.store", "ok", "agy's sign-in has not been checked from here.")
    else:
        yield _check("agy.store", "warn", message or NOT_CHECKED, "Until then AccountDeck saves and switches no Antigravity account.")
    missing = [row for row in rows if not _agy_record_kept(v, row["id"])]
    tried = [at for at in [engine.agy.last_try if engine is not None else None, *(row["usage_at"] for row in rows)]
             if _number(at) is not None]
    last = f" Antigravity usage was last read {age_text(max(0.0, v.now - max(tried)))} ago." if tried else ""
    one = len(missing) == 1
    if missing:
        names = ", ".join(diagnostics.redact_text(row["label"]) for row in missing[:4])
        yield _check("agy.accounts", "warn", f"{len(missing)} of {count} saved {'has' if one else 'have'} lost "
                     f"{'its' if one else 'their'} saved sign-in: {names}.{last}",
                     f"Choose Sign In Again for {'it' if one else 'them'} in AccountDeck.")
    elif rows:
        kept = "and its sign-in is kept" if len(rows) == 1 else "and each one's sign-in is kept"
        yield _check("agy.accounts", "ok", f"{count} {'is' if len(rows) == 1 else 'are'} saved, {kept}.{last}")
    else:
        yield _check("agy.accounts", "ok", f"No Antigravity account is saved yet.{last}")


def _agy_rows(v):
    """The saved Antigravity accounts: {"id", "label", "usage_at"}; none from a database older than their table."""
    if v.engine is not None:
        return v.engine.store.agy_accounts()
    if v.database is None:
        return []
    try:
        return [dict(row) for row in v.database.db.execute(
            f"SELECT id, label, usage_at FROM {core.AGY_TABLE} ORDER BY sort_order, created, rowid")]
    except sqlite3.Error:
        return []


def _agy_record_kept(v, account_id):
    """Whether the account's vault record (`agy-<id>`) is there, by its file alone."""
    try:
        key = core.agy_key(account_id)
    except ValueError:
        return False
    if v.engine is not None:
        return v.engine.vault.path(key).exists()
    return (v.paths.data_dir / "vault" / f"{key}{core.VAULT_SUFFIX}").exists()


def _limit_resets(v):
    """One line about limit resets, from what the service saved only (its readings and resets.MEMORY): how many
    accounts hold some, what Claude answered about each account it read (eligible, or the reason it gave, a plain
    word), and any use whose outcome is still not known. Nothing is asked of a provider."""
    if v.store is None:
        return []
    rows = [row for row in v.store.accounts() if row["provider"] in ("claude", "claude_cli", "codex") and not core.is_frozen(row)]
    memory = v.store.setting(RESETS_MEMORY)
    saved = memory.get("accounts") if isinstance(memory, dict) and isinstance(memory.get("accounts"), dict) else {}
    holding, eligible, reasons, doubts, unanswered = 0, 0, Counter(), 0, 0
    for row in rows:
        entry = saved.get(row["id"]) if isinstance(saved.get(row["id"]), dict) else {}
        doubts += bool(isinstance(entry.get("pending"), dict) and entry["pending"].get("sent"))
        if row["provider"] == "codex":
            count = sum(r["count"] for r in (row["last_good_usage"] or {}).get("reset_credits") or ()
                        if isinstance(r, dict) and type(r.get("count")) is int)
        else:
            block = (entry.get("claude") or {}).get("block") if isinstance(entry.get("claude"), dict) else None
            if not isinstance(block, dict):
                unanswered += 1
                continue
            if block.get("eligible") is True:
                eligible += 1
            else:
                reasons[block.get("reason") if isinstance(block.get("reason"), str) else "unknown"] += 1
            count = sum(grant["left"] for grant in reset_rows(block, v.now))
        holding += count > 0
    text = f"{holding} of {len(rows)} accounts are known to hold limit resets."
    if unanswered:
        text += f" Claude's reset count is not known for {unanswered}."
    if v.store.setting("claude_resets", True) is False:
        text += " Reading Claude's is off in Settings."
    elif eligible or reasons:
        text += f" Claude answered for {eligible + sum(reasons.values())}: {eligible} eligible"
        text += (", " + ", ".join(f"{count} not ({reason})" for reason, count in reasons.most_common(4)) + ".") if reasons else "."
    if doubts:
        return [_check("resets.held", "warn", text + f" {doubts} use{'s' if doubts > 1 else ''} of a limit reset "
                       f"{'have' if doubts > 1 else 'has'} no known outcome yet.",
                       "AccountDeck settles it at the account's next reading; trying again sends the same request.")]
    return [_check("resets.held", "ok", text)]


SECTIONS = (("service", _service), ("runtime", _runtime), ("db", _database), ("cc", _claude_code), ("accounts", _accounts),
            ("agy", _antigravity), ("resets", _limit_resets), ("machine", _machine))


def run(paths, *, engine=None, keychain=None):
    """{"ok", "checks", "at"}: `state.health`, and what `--doctor` prints. With `engine` (the slow cycle, off the tick
    thread) the engine's own view replaces /_health and the read-only database. A section that raises is one warning
    about itself, never a broken report."""
    view = _View(paths, engine, keychain)
    checks = []
    try:
        for name, section in SECTIONS:
            try:
                checks.extend(section(view))
            except Exception as exc:
                diagnostics.log_exception("doctor.section_failed", exc, section=name)
                checks.append(_check(f"{name}.unchecked", "warn", f"The {name} checks could not finish ({type(exc).__name__}).",
                                     "Check again in a minute."))
    finally:
        view.close()
    return {"ok": not any(check["level"] == "fail" for check in checks), "checks": checks, "at": view.now}


def public(report):
    """What `--doctor --json` prints: the report, with what it ran as."""
    return {"version": __version__, "build": BUILD, **report}


def text(report):
    """What `--doctor` prints: one line per check, and what to do under it."""
    counts = Counter(check["level"] for check in report["checks"])
    head = "healthy" if report["ok"] else f"{counts['fail']} problem{'s' if counts['fail'] != 1 else ''}"
    if counts["warn"]:
        head += f", {counts['warn']} warning{'s' if counts['warn'] != 1 else ''}"
    lines = [f"AccountDeck 2 {__version__} ({build_text(BUILD)}) doctor: {head}."]
    for check in report["checks"]:
        lines.append(f"  {check['level'].upper():4}  {check['text']}")
        if check["action"]:
            lines.append(f"        → {check['action']}")
    return "\n".join(lines)


# ---- the Claude Code compatibility watch ----

def compat_step(memory, version, failing, running):
    """One look of the watch. Pure: (the new memory, the failing checks to report or None).

    A new version is recorded and looked at until it has passed: it passes once the structural checks hold while a
    session reports that version, as only then what is on disk is what that version wrote. A fail lasting STRIKES
    cycles while the version has not passed is reported once per version. `memory` is {"seen", "passed", "alerted",
    "strikes"}; `failing` is the structural fails of the report, `running` the versions sessions report."""
    memory = dict(memory) if isinstance(memory, dict) else {}
    if version is not None and version != memory.get("seen"):
        memory.update(seen=version, strikes=0)
    seen = memory.get("seen")
    if seen is None or seen == memory.get("passed"):
        return memory, None
    if not failing:
        memory["strikes"] = 0
        if seen in running:
            memory["passed"] = seen
        return memory, None
    memory["strikes"] = memory.get("strikes", 0) + 1
    if memory["strikes"] < STRIKES or memory.get("alerted") == seen:
        return memory, None
    memory["alerted"] = seen
    return memory, failing


def watch(engine, report):
    """Runs after each report (`Engine._doctor_cycle`). Claude Code updates itself almost daily: a new version is one
    log line, and a critical alert only when what AccountDeck relies on no longer matches. It only reads the disk
    (`claude_code_version`); nothing is downloaded or run."""
    version, _linked, running = claude_code_version(engine.paths)
    before = engine.store.setting(COMPAT_KEY)
    failing = [check for check in report["checks"] if check["id"] in STRUCTURAL and check["level"] == "fail"]
    memory, reported = compat_step(before, version, failing, running)
    if memory == before:
        return
    engine.store.set_setting(COMPAT_KEY, memory)
    previous = before.get("seen") if isinstance(before, dict) else None
    if memory.get("seen") != previous:
        diagnostics.log("claude_code.version_changed", previous=previous, version=memory.get("seen"))
    if memory.get("passed") != (before.get("passed") if isinstance(before, dict) else None):
        diagnostics.log("claude_code.version_passed", version=memory.get("passed"))
    if reported:
        diagnostics.log("claude_code.mismatch", lvl="error", version=version, checks=[check["id"] for check in reported])
        engine.store.add_alert(alerts.claude_code_mismatch(version, memory.get("passed"), [check["text"] for check in reported],
                                                           engine.clock()))
        engine.bump()
