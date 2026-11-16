"""AccountDeck 2 engine (PLAN §2.2): the state snapshot, the tick loop and the action facade.

`Engine` implements `server.EngineAPI` and is the one object the switch flows, the move import and
the demo build against.

- `snapshot()` builds the §3.3 state from the store, the provider adapters and one observation of
  the machine, enriches it with `intel.enrich_state` and validates it with
  `contract.validate_state` (an assertion in demo and safe mode, one log line in live mode). The
  result is cached until `bump()` raises `rev`.
- The `ad2-sched` thread ticks every few seconds: it takes one look at the processes, senses the
  signed-in accounts, polls the status pages, hands due accounts to the `RefreshScheduler`, runs
  the background verifiers and the terminal auto-switch, and evaluates the alert engine. Nothing in
  the tick signals a process.
- `action()` answers the quick actions itself and hands every switch flow to `switching.dispatch`, which
  shares the engine only through its public members.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import itertools
import json
import os
from pathlib import Path
import sys
import threading
import time
import tomllib

from . import (__version__, activity, alerts, cli_auto, contract, diagnostics, intel, move_import, reset_auto,
               switch_claude, switch_cli, switching, updates)
from . import BUILD, doctor
from .accounts import AccountService, default_label, restorable
from .agy_accounts import AgyService
from .cli_accounts import LIVE_LOST, ON_HOLD, REAUTH, CLIAccounts, login_ends, unheld, with_end
from .cli_policy import considered
from .cli_watch import LimitWatch
from .config import SETTINGS_SCHEMA, Settings, Timings, safe_mode, validate_settings
from .core import (RESTORED, DeckError, FrozenAccountError, Store, Vault, atomic_write, is_frozen, read_json,
                   recover_transactions, vault_cipher)
from .diagnostics import Manifest
from .operations import TERMINAL_PHASES, Operation, OperationRunner
from .platform import facts_mac
from .platform.keychain import KeychainError
from .procs import ProcessSet, get_controller, unix_time
from .resets import Resets
from .providers import make_providers
from .providers.antigravity import Antigravity
from .providers.claude_cli import ClaudeCLI, expires
from .providers.codex import Codex
from .providers.status import ObservedStatus, StatuspageStatus, StatuspageSummary
from . import runway
from .rules import REFILL_KEEP
from .scheduler import HOT_RESET, RefreshScheduler, Target
from .usage import PRIMARY_KINDS, report_status, shape_change, take_shape, watch_shape

NOTICE_TTL = 120.0            # a non-sticky notice leaves the state after this long
MAX_NOTICES = 30
HEARTBEAT = 60.0              # the rev moves at least this often, so countdown-derived fields stay fresh
HEARTBEAT_LOG = 600.0         # engine.heartbeat is logged this often (real seconds); health() is always live
STALL_TICK = 30.0             # `stalled` when no tick completed for this long (real seconds)
STALL_READ = 1200.0           # ... or no terminal read succeeded for this long while terminal reads should run
PHASE_RELOG = 60              # a phase failing on every tick is logged again every this many ticks
SLOW_EVERY = 60.0             # shared setup, Codex conversations and the data-folder check
RUNWAY_EVERY = 180.0          # the terminal pool's forecast (`providers.claude_cli.runway`)
DOCTOR_FIRST, DOCTOR_EVERY = 15.0, 300.0     # the doctor's cycle behind `state.health` (real seconds)
# The watchdog (real seconds; `time.monotonic` stands still while the Mac sleeps).
WATCHDOG_EVERY = 15.0         # it looks this often
WATCHDOG_TICK = 300.0         # no tick completed for this long: hung, so the service restarts
WATCHDOG_READS = 600.0        # ... or reads held every refresh worker and none ended for this long
WATCHDOG_MUTATION = 900.0     # ... this long instead while a change is under way: cutting one short is the last resort
WATCHDOG_GAP = 60.0           # its own look this late, or the clocks this far apart: a sleep, a stop or a new time, no hang
READ_STUCK = 300.0            # one read running this long is logged: none should
WATCHDOG_EXIT = 75            # EX_TEMPFAIL. launchd restarts on any exit but 0 (KeepAlive {SuccessfulExit: false})
EXIT_DEADLINE = 20.0          # logging and the state file get this long before the restart goes ahead without them
QUIT_WAIT, QUIT_WAIT_BUSY = 60.0, 300.0     # a Quit still not over after this long just ends (stop() has its own limits)
RESTARTS_PER_HOUR = 3         # past this the service stays up as it is and says so, once
BRAKE_ALERT_EVERY = 86400.0
WATCHDOG_FILE = "watchdog.json"
BACKUP_EVERY = 86400.0        # the database's check and backup (wall clock: a Mac asleep at the time does it on waking)
CARE_RETRY, CARE_URGENT = 3600.0, 600.0     # ... again after a failure; after a tick phase saw a damaged database
LOST_LOGIN = "This account's saved sign-in is missing or can't be read. Sign in again, or remove the account."
STATUS_MIN = 1.0
MACHINE_TTL = 1.5             # how old a process observation a snapshot may reuse (real seconds)
READY_GRACE = 30.0            # `ready` turns true after this long even if a first reading is still missing
PLAN_GRACE = 600.0            # a plan is kept this long after it expired
MAX_SETUP_BYTES = 8 * 1024 * 1024
HOT_USED, EXHAUSTED_USED = 95.0, 99.5
DEAD_CREDENTIALS = frozenset({"reauth_required", "access_expired", "web_only"})   # the active account still shows these
MANIFEST_ID = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
CONSUMER_KINDS = {"codex_tui": "tui", "codex_app_server_child": "app_server_child", "codex_desktop": "desktop"}
SETUP_MESSAGE = ("Settings, local MCP definitions, skills, plugins and project files stay shared. "
                 "Hosted connectors may require sign-in on each account.")
SETUP_NOTE = "Claude Chat local MCPs and Claude Code MCPs are separate configurations."
READ_ONLY_OTHER = ("Another AccountDeck 2 is managing your sign-ins",
                   "Quit that copy to make changes here. This one only shows usage.")
SELF_PROTECT = ("Open AccountDeck 2 from Applications",
                "AccountDeck was started from inside Claude, so it cannot close Claude to switch accounts. "
                "Quit it and open AccountDeck 2 from Applications or the menu bar.")
OTHER_OWNER = "Another AccountDeck 2 is managing your sign-ins. Quit it to make changes here."
OUTAGE_TAIL = " Your last good readings are kept. Switching still works; verification may take longer."
# What an interrupted switch still waiting says (`recovery_words`).
RECOVERY_REVIEW = ("An interrupted switch needs review", "Your previous sign-in files are kept safely. Recover to put them back.")
RECOVERY_LOCKED = ("An interrupted switch can't be read yet",
                   "The keychain is locked or refused, so AccountDeck can't read its record of an interrupted switch. "
                   "Unlock it, or allow AccountDeck when macOS asks; it then finishes by itself.")
RECOVERY_DAMAGED = ("A record of an interrupted switch can't be read",
                    "AccountDeck can't tell what that switch changed, so switches and auto-switch wait. If your terminals "
                    "work and show the right account, choose Recover to set the record aside. It is kept, not deleted.")
RECOVERY_UNLISTED = ("AccountDeck can't check for interrupted switches",
                     "macOS refused its vault folder, so switches and auto-switch wait. It goes on by itself once the folder can be read.")
RECOVERY_NEEDS_YOU = "An interrupted terminal switch needs you"
RECOVERY_WAITING = "Finishing an interrupted terminal switch"
RECOVERY_BY_ITSELF = "AccountDeck is putting it right by itself; terminal auto-switch waits until then."


class ServiceOperation(Operation):
    """A quick mutation that still runs as an operation (one at a time, visible as `pending`)."""

    def __init__(self, kind, fn, *, message, step, **fields):
        super().__init__(kind, steps=(step,), **fields)
        self.fn, self.working = fn, message

    def run(self):
        with self.step(self.steps[0].id, phase="saving", message=self.working, cancellable=False):
            self.fn()


@dataclass(frozen=True)
class Machine:
    """One observation of the processes, shared by the tick and the snapshots built after it."""
    kinds: dict
    report: object                # activity.ActivityReport
    consumers: list
    registry: dict
    names: dict
    self_protect: bool
    fingerprint: tuple
    taken: float                  # real monotonic time of the observation
    agy: tuple = ()               # the agy processes (procs.agy_consumers)

    @staticmethod
    def empty():
        blank = activity.ActivityReport([], 0, 0, 0, 0, [], True, None)
        return Machine({kind: [] for kind in ("claude_desktop", "claude_code", "claude_foreign", "codex_daemon",
                                              "codex_desktop")}, blank, [], {}, {}, False, ("unavailable",), 0.0)


class Engine:
    tick_seconds = 5.0

    def __init__(self, paths, *, owner_lock=None, providers=None, procs=None, vault=None, status_sources=None,
                 timings=None, clock=time.time, start=True, inline=False, mode="live",
                 real_time=time.time, real_monotonic=time.monotonic, update_fetch=None, antigravity=None):
        """`owner_lock` is the machine-wide lock run.py took: None means this engine owns the
        sign-ins outright (tests, demo), an unheld lock means another AccountDeck 2 does. `clock`
        is the engine's time (the demo passes a virtual one); `real_time` and `real_monotonic`
        only detect a resume from sleep and age cached observations. `update_fetch(url)` stands in for GitHub in
        the update check (tests, the demo; updates.Updater, updates.stand_in). `antigravity` stands in for agy
        (providers.fake.FakeAntigravity in tests and the demo); it is not one of `providers`, which nothing
        automatic about Antigravity may reach (agy_accounts)."""
        self.paths, self.owner_lock, self.clock, self.mode = paths, owner_lock, clock, mode
        self.real_time, self.real_monotonic, self.inline = real_time, real_monotonic, inline
        self.timings = timings or Timings()
        self.lock = threading.RLock()
        # Liveness (`health()`): ticks, failing tick phases and the last read per provider.
        self._health_lock = threading.Lock()
        self._ticks, self._last_tick, self._last_tick_real, self._failing, self._reads = 0, None, None, {}, {}
        self._reading, self._read_done = {}, None      # reads under way by thread, when one last ended (`_read_tracked`)
        self.watchdog = None                           # run.py starts it (`start_watchdog`)
        self._tick_assertions = []
        self._beat = {"at": real_monotonic(), "ticks": 0, "ok": 0, "failed": 0}
        self._read_baseline = real_monotonic()     # start or last resume: read staleness counts from here
        self._build_lock, self._machine_lock = threading.Lock(), threading.Lock()
        self._sense_lock, self._slow_lock = threading.Lock(), threading.Lock()
        self.stopping = threading.Event()
        self._wake = threading.Event()       # ends the tick loop's sleep early (`wake`)
        self.rev, self._cache, self._stopped, self.thread = 1, None, False, None
        self.store = Store(paths.data_dir, clock=clock)
        if self.store.migrated:          # this start added Antigravity's table to an older database
            diagnostics.log("db.table_added", "info" if self.store.migrated["backup"] != "failed" else "warning",
                            **self.store.migrated)
        self.vault = vault or Vault(paths.data_dir / "vault", protect=vault_cipher(paths.data_dir))
        stored = {key: value for key in SETTINGS_SCHEMA if (value := self.store.setting(key)) is not None}
        self.prefs = Settings(stored, save=self.store.set_setting, home=paths.home)
        if "log_level" in stored:
            diagnostics.set_level(self.prefs.get("log_level"))
        self.procs = procs or get_controller(paths, demo=mode == "demo")
        self.providers = dict(providers) if providers else make_providers(paths, procs=self.procs)
        self.providers.setdefault("claude_cli", ClaudeCLI(paths, clock=clock))
        self.observed = {name: ObservedStatus(clock=clock) for name in self.providers}
        self.status_sources = self._default_sources() if status_sources is None else dict(status_sources)
        self.status_pages, self._status_due, self._status_busy = {}, {}, set()
        self._status_threads = []
        self._counts = {}
        self.notices, self._notice_ids = [], itertools.count(1)
        self.plans, self.login, self.holds = {}, None, set()
        self.active_identity = {name: None for name in self.providers}
        self.identity_known = {name: False for name in self.providers}
        self.background_verifiers = []
        self.closers = []                    # called once by stop(), e.g. a Codex sign-in helper
        self.scheduler = RefreshScheduler(self._read_tracked, inline=inline)
        self.accounts = AccountService(self.store, self.vault, self.providers, self.prefs, clock=clock,
                                       observed=self.observed, levels=self.levels,
                                       lock_for=self.scheduler.account_lock,
                                       lock_wait=self.timings.lock_wait, on_change=self.bump, held=self.login_held)
        self.runner = OperationRunner(on_change=self.bump, on_finish=self._finished, on_settle=self._settled,
                                      clock=clock)
        self._recovery = ([], frozenset(), frozenset(), False)     # until the scan below
        self.unreadable = []                 # (key, error) of each transaction record the last scan could not read
        self.cli = CLIAccounts(self, self.providers["claude_cli"])
        self.agy = AgyService(self, antigravity or Antigravity(paths, procs=self.procs))
        self.resets = Resets(self)           # limit resets: the count, Claude's resets read, plans and the use
        self._restored = self._reconcile_restored()
        if self.owns_login and not self.cli.adapter.readonly_reason:
            self.cli.adopt_stashes()
            self.cli.restore_enrollment()
        if self.owns_login:
            self.accounts.settle_renewals()
            try:
                self.agy.recover_at_start()
            except Exception as exc:     # Claude's and Codex's accounts must still start
                diagnostics.log_exception("agy.recover_failed", exc)
        self.recovery = self._scan_recovery()
        self.alert_engine = alerts.AlertEngine(started=clock())
        self.watch = LimitWatch(self)        # limit errors in Claude Code's transcripts (cli_watch)
        self.updates = updates.Updater(self, fetch=update_fetch)     # its own thread, never the tick (updates.py)
        self._machine, self._slow, self._idle_state = None, None, {}
        self._runway, self._runway_at = None, None
        self._read_once = set()
        self._shape_lock, self._shape_keys = threading.Lock(), set()
        self._doctor_report, self._doctor_thread, self._doctor_due = None, None, real_monotonic() + DOCTOR_FIRST
        self._started_at = real_monotonic()
        self._wall, self._mono, self._last_heartbeat = real_time(), real_monotonic(), clock()
        self.woke_at = None                  # the engine's time when the Mac last woke from sleep (`_check_resume`)
        self._previous_state, self._alerts_rev, self._invalid_logged = None, None, False
        self.tray_handle = None              # the menu bar item while it shows (run.py, update_settings)
        self.sense_active()
        self._restore_login()
        self._counts = self._count_accounts(self.store.accounts())
        if start:
            self.start()

    def _default_sources(self):
        """The real status pages, only where the network is allowed at all."""
        if self.mode != "live" or safe_mode():
            return {}
        return {"claude": StatuspageSummary(clock=self.clock), "codex": StatuspageStatus(clock=self.clock)}

    def start(self):
        """Starts the `ad2-sched` thread (once)."""
        with self.lock:
            if self.thread is None and not self._stopped:
                self.thread = threading.Thread(target=self._loop, name="ad2-sched", daemon=True)
                self.thread.start()
                self.watch.start()
                self.updates.start()

    # ---- what switch code and the server read ----
    @property
    def pending(self):
        """The running operation (§3.5), or None; an operation waiting in `needs_reconfirm` is not running."""
        pending = self.runner.pending()
        return pending if pending is not None and pending["phase"] not in TERMINAL_PHASES else None

    @property
    def owns_login(self):
        return self.owner_lock is None or bool(self.owner_lock.held)

    @property
    def read_only(self):
        """Another AccountDeck 2 holds the owner lock: this one only shows usage."""
        return not self.owns_login

    def bump(self):
        with self.lock:
            self.rev += 1
            self._cache = None

    def notice(self, message, level="info", sticky=True):
        """Posts a notice; a sticky one stays until dismissed, another leaves after two minutes."""
        with self.lock:
            self.notices.append({"id": f"n_{next(self._notice_ids)}", "level": level if level in contract.LEVELS else "info",
                                 "message": str(message), "at": self.clock(), "sticky": bool(sticky)})
            del self.notices[:-MAX_NOTICES]
        self.bump()

    def submit(self, op):
        """Runs an operation on `ad2-ops`; DeckError `busy` while another one runs."""
        result = {"op": self.runner.submit(op)}
        self.bump()
        return result

    def hold_readings(self, reason):
        """No usage reads start, and `ready` stays false, until every hold is released."""
        with self.lock:
            self.holds.add(reason)
        self.bump()

    def release_readings(self, reason):
        with self.lock:
            self.holds.discard(reason)
        self.bump()

    def roots(self):
        return [self.paths.claude_profile, self.paths.codex_home, self.paths.claude_projects]

    def _scan_recovery(self):
        unreadable = []
        try:
            keys = recover_transactions(self.vault, unreadable)
        except Exception as exc:       # a folder that cannot be listed: it must not end the operation that scans
            diagnostics.log_exception("recovery.scan_failed", exc)
            keys = ["unreadable"]
        self.unreadable = unreadable   # an unreadable record still means a switch was interrupted
        return keys or (["unreadable"] if unreadable else [])

    @property
    def recovery(self):
        """The keys of the transactions a switch left unfinished (["unreadable"] when only one that cannot be
        read is left: `unreadable` lists those), which Recover puts right. Setting it reads each one once, for
        `recovering` and `login_held`."""
        return self._recovery[0]

    @property
    def recovery_unknown(self):
        """True while a record, or the folder of them, cannot be read: whose it is cannot be told."""
        return self._recovery[3]

    def recovery_words(self):
        """(title, text) of the interrupted switch waiting now, said alike by its banner, its alerts and auto-switch's
        hold (the title). switch_cli.auto_recover tells what only the owner can settle from what settles by itself."""
        if self.recovery_unknown:
            if not self.unreadable:
                return RECOVERY_UNLISTED
            return RECOVERY_LOCKED if all(isinstance(exc, KeychainError) for _, exc in self.unreadable) else RECOVERY_DAMAGED
        if not self.recovering("claude_cli"):
            return RECOVERY_REVIEW
        wait = self.cli.recovery_wait
        if wait["needs_you"] and wait["problem"]:
            return RECOVERY_NEEDS_YOU, wait["problem"]
        return RECOVERY_WAITING, f"{wait['problem'].rstrip('.')}. AccountDeck keeps trying by itself." if wait["problem"] else RECOVERY_BY_ITSELF

    @recovery.setter
    def recovery(self, keys):
        apps, held, unknown = set(), set(), bool(self.unreadable)
        if unknown:                    # whose it is cannot be told: it holds every app's switches and every login
            apps.update(self.providers)
        for key in keys:
            try:
                doc = self.vault.get(key)
            except DeckError:          # likewise
                apps.update(self.providers)
                unknown = True
                continue
            apps.add(doc.get("provider") or "claude")         # Claude Desktop's records name no provider
            if doc.get("provider") == "claude_cli":
                change = doc.get("oauth_change") or {}
                for side in ("after", "before"):
                    identity = (change.get(side) or {}).get("accountUuid")
                    if isinstance(identity, str) and identity:
                        held.add(identity)
            elif doc.get("provider") == "codex":     # whatever its status: Recover still puts that login back
                held.update(Codex.put_back(doc))
        self._recovery = (list(keys), frozenset(apps), frozenset(held), unknown)     # one swap: readers see one scan

    def recovering(self, provider):
        """True while an interrupted transaction of `provider`'s, or one that cannot be read, is not settled."""
        return provider in self._recovery[1]

    def login_held(self, identity):
        """True when the terminal account's (accountUuid) saved login is not renewed while an interrupted switch's
        record is open (CLIAccounts._refresh): the account the switch gave Claude Code, which may have renewed past
        the saved copy, and the one before it, in conflict too until the record is closed: Recover may put that
        login back, or Claude Code may hold that account's login again (switch_cli.CLITransaction._name_owner), and
        a renewal would leave only a spent one. And any account while a record cannot be read, as it may be that
        record's. Likewise a Codex account (its id) whose login an interrupted Codex switch puts back on Recover
        (AccountService.renew_codex)."""
        return self._recovery[3] or identity in self._recovery[2]

    def _restore_login(self):
        """Once after construction: an add-account sign-in that was waiting when the service last stopped
        (`engine.login`) is shown again. It never stops the start."""
        try:
            switch_claude.restore_enrollment(self)
        except Exception as exc:
            diagnostics.log_exception("engine.restore_login_failed", exc)

    # ---- who is signed in ----
    def sense_active(self, only=None):
        """Reads which account each provider (or `only` that one) is signed in to (read-only) and tracks the
        unsaved one. `only="antigravity"` (an Antigravity plan or action) reads agy's store whatever its change
        marker says (AgyService.sense)."""
        changed = False
        with self._sense_lock:
            now = self.clock()
            for name, adapter in self.providers.items():
                if only not in (None, name):
                    continue
                try:
                    if name == "claude_cli":
                        identity = self.cli.mirror()
                        known = True
                    elif name == "codex":
                        found = adapter.active_identity()
                        known, identity = found.status != "unknown", found.identity if found.status == "ok" else None
                    else:
                        known, identity = True, adapter.active_identity()
                except DeckError as exc:
                    known, identity = name == "claude_cli" and exc.code == "login_needed", None
                except Exception as exc:       # a vault/store/disk error: unknown, and the next provider still runs
                    diagnostics.log_exception("engine.sense_failed", exc, provider=name)
                    known, identity = False, None
                if not known:            # keep what was last known; the switch code sees identity_known False
                    changed |= self.identity_known[name]
                    self.identity_known[name] = False
                    continue
                changed |= (identity, True) != (self.active_identity[name], self.identity_known[name])
                self.active_identity[name], self.identity_known[name] = identity, True
                if name == "claude_cli":
                    continue
                before = self.accounts.unsaved[name]
                self.accounts.sense_unsaved(name, identity)
                changed |= (before is None) != (self.accounts.unsaved[name] is None)
                if identity is not None:
                    self.accounts.touch_active(name, identity, now)
            if only in (None, "antigravity"):
                try:
                    changed |= self.agy.sense(force=only == "antigravity")
                except Exception as exc:       # as for a provider: the state keeps what was last sensed
                    diagnostics.log_exception("engine.sense_failed", exc, provider="antigravity")
        if changed:
            self.bump()

    # ---- provider status ----
    def status(self, provider, now=None):
        """The contract status of a provider: the status page and what our own reads observed."""
        now = self.clock() if now is None else now
        with self.lock:
            page = self.status_pages.get("claude" if provider == "claude_cli" else provider)
        observed = self.observed[provider].evaluate(now=now, accounts=self._counts.get(provider))
        return intel.classify_outage(provider, page, observed, now=now)

    def levels(self):
        now = self.clock()
        return {name: self.status(name, now)["level"] for name in self.providers}

    def _poll_status(self, now):
        for name, source in self.status_sources.items():
            if name in self._status_busy or now < self._status_due.get(name, 0):
                continue
            self._status_busy.add(name)
            if self.inline:
                self._fetch_status(name, source)
                continue
            thread = threading.Thread(target=self._fetch_status, args=(name, source), name="ad2-status", daemon=True)
            self._status_threads = [t for t in self._status_threads if t.is_alive()] + [thread]
            thread.start()

    def _fetch_status(self, name, source):
        try:
            before = self.status(name)["level"]
            try:
                page = source.fetch()
            except DeckError as exc:       # a safety refusal must not stop the polling loop
                diagnostics.log("status.fetch_refused", lvl="warning", provider=name, code=exc.code)
                page = None
            except Exception as exc:       # nor may a parsing bug turn it into a request every tick
                diagnostics.log_exception("status.fetch_failed", exc, provider=name)
                page = None
            with self.lock:
                if page is not None:
                    self.status_pages[name] = page
            now = self.clock()
            self._status_due[name] = now + max(source.poll_interval(), STATUS_MIN)
            if before in ("degraded", "outage") and self.status(name, now)["level"] == "ok":
                self.notice(f"{intel.PROVIDER_NAMES.get(name, name)} is back to normal.", "good", sticky=False)
                self.scheduler.request_all([row["id"] for row in self.store.accounts() if row["provider"] == name], now)
            self.bump()
        finally:
            self._status_busy.discard(name)

    # ---- reads ----
    def _pressure(self, usage, now):
        """(hot, exhausted, resets) of a last good reading (§6.9)."""
        windows_ = [w for w in map(intel.normalize_window, (usage or {}).get("windows") or ())
                    if w is not None and w["kind"] in PRIMARY_KINDS]
        current = [w for w in windows_ if w["reset"] is None or w["reset"] > now]     # a passed reset says nothing yet
        hot = any((w["used"] or 0) >= HOT_USED or w["blocked"] or (w["reset"] is not None and w["reset"] - now <= HOT_RESET)
                  for w in current)
        exhausted = bool(current) and all((w["used"] or 0) >= EXHAUSTED_USED or w["blocked"] for w in current)
        return hot, exhausted, tuple(sorted({w["reset"] for w in windows_ if w["reset"] is not None}))

    def targets(self, now=None, rows=None):
        """One scheduler Target per readable saved account and per unsaved signed-in account. Another
        computer's account and a Desktop profile still waiting for conversion are never read."""
        now = self.clock() if now is None else now
        rows = self.store.accounts() if rows is None else rows
        read_only = self.read_only
        levels = {name: self.status(name, now)["level"] for name in self.providers}
        targets = []
        for row in rows:
            if is_frozen(row) or not move_import.readable(row):
                continue
            hot, exhausted, resets = self._pressure(row["last_good_usage"], now)
            if row["provider"] == "claude_cli":
                values = considered({"windows": (row["last_good_usage"] or {}).get("windows", [])}, self.prefs.public())
                hot = any(used is not None and used >= 80 for _, used, _ in values)
                exhausted = any(used is not None and used >= 100 for _, used, _ in values)
            targets.append(Target(row["id"], row["provider"], row["identity"],
                                  active=row["identity"] == self.active_identity.get(row["provider"]),
                                  hot=hot, exhausted=exhausted, resets=resets, paused_until=row["paused_until"],
                                  read_only=read_only, level=levels[row["provider"]]))
        for name, unsaved in self.accounts.unsaved.items():
            if unsaved is not None:
                hot, exhausted, resets = self._pressure(unsaved["usage"], now)
                targets.append(Target(contract.UNSAVED_IDS[name], name, unsaved["identity"], active=True, hot=hot,
                                      exhausted=exhausted, resets=resets, read_only=read_only, level=levels[name]))
        return targets

    def _refresh(self, key):
        """One read on an `ad2-refresh` worker. The terminal auto-switch policy is not evaluated here:
        it runs on the tick thread only (one writer of `cli_history`, and a policy error can never
        turn a good reading into a failed one); the next tick, at most 5 s later, sees this reading."""
        row = self.store.account(key)
        provider = row["provider"] if row else next((name for name, unsaved in contract.UNSAVED_IDS.items()
                                                     if unsaved == key), None)
        result = "error"
        self.resets.begin_read()
        try:
            if is_frozen(row):
                result = "skipped"           # another computer's sign-in is never read or renewed here
            elif row and row["provider"] == "claude_cli":
                result = self.cli.read(key)
            else:
                result = self.accounts.read(key, active_identity=dict(self.active_identity), read_only=self.read_only)
            return result
        finally:
            self._record_read(provider, result)
            self.resets.after_read(key, provider, result)      # Claude's resets read when due; it never raises
            self._watch_shape(provider, key, row)
            self._read_once.add(key)
            self.bump()
            if self.watch.urgent():          # a limit error waits for this reading: the next tick decides at once
                self.wake()

    def _watch_shape(self, provider, key, row):
        """Remembers the shape of the Claude answer this read got (usage.watch_shape): a change goes to the log, an
        account gaining or losing its 5-hour window to the activity history. It can never hurt a read."""
        seen = take_shape()
        if seen is None or provider not in ("claude", "claude_cli"):
            return
        try:
            with self._shape_lock:
                stored = self.store.setting("usage_shapes")
                memory, news = watch_shape(stored, provider, key, seen, self.clock())
                if memory != stored:
                    self.store.set_setting("usage_shapes", memory)
                unknown = [name for name in news["unknown"] if name not in self._shape_keys]
                self._shape_keys.update(unknown)
            if news["added"] or news["removed"]:
                diagnostics.log("usage.shape_changed", "warning", provider=provider, added=news["added"], removed=news["removed"])
            if unknown:
                diagnostics.log("usage.unknown_keys", provider=provider, keys=unknown)
            if news["session"] is not None and provider == "claude_cli" and row:
                self.store.event("usage", f"{row['label']} has a 5-hour limit again" if news["session"]
                                 else f"{row['label']} no longer has a 5-hour limit")
        except Exception as exc:
            diagnostics.log_exception("engine.shape_failed", exc)

    def _read_tracked(self, key):
        """`_refresh` as the scheduler's `run`, noted for the watchdog: a read should never last minutes."""
        thread = threading.get_ident()
        with self._health_lock:
            self._reading[thread] = {"key": key, "since": self.real_monotonic(), "logged": False}
        try:
            return self._refresh(key)
        finally:
            with self._health_lock:
                del self._reading[thread]
                self._read_done = self.real_monotonic()

    def _record_read(self, provider, outcome):
        if provider is None or outcome == "skipped":
            return
        now, real = self.clock(), self.real_monotonic()
        with self._health_lock:
            entry = self._reads.setdefault(provider, {"ok": 0, "failed": 0, "last_ok_at": None, "last_failed_at": None,
                                                      "last_outcome": None, "ok_real": None})
            entry["last_outcome"] = outcome if isinstance(outcome, str) else "error"
            if outcome == "ok":
                entry.update(ok=entry["ok"] + 1, last_ok_at=now, ok_real=real)
                self._beat["ok"] += 1
            else:
                entry.update(failed=entry["failed"] + 1, last_failed_at=now)
                self._beat["failed"] += 1

    # ---- the tick ----
    def tick(self):
        """Every phase runs on its own: one that raises is logged (`engine.phase_failed`) and listed in
        `health()`, and the phases after it, the scheduler above all, still run."""
        now = self.clock()
        self._tick_assertions = []
        phase = self._phase
        phase("resume", self._check_resume, now)
        phase("observe", self._observe, fresh=True)
        phase("recover", switch_cli.auto_recover, self)
        # A job waiting for sessions touches no terminal login, so the terminals stay in view (and a sign-in lands).
        if not self.runner.busy or self.runner.yields():
            phase("sense", self.sense_active, only="claude_cli" if self.runner.busy else None)
            # A stuck enrollment must not stop every account's reads.
            phase("enrollment", self.cli.poll_enrollment, event="cli.enrollment_failed")
            phase("agy_enrollment", self.agy.poll_enrollment, event="agy.enrollment_failed")
        phase("status", self._poll_status, now)
        phase("watch", self.watch.tick)      # before `schedule`: the read a limit error asks for starts in this tick
        phase("schedule", self._schedule, now)
        phase("slow", self._refresh_slow, now)
        phase("runway", self._refresh_runway, now)
        phase("doctor", self._doctor_tick)
        phase("verifiers", self._tick_verifiers, now)
        # Before auto-switch: with every terminal account out, a limit reset comes first (the setting `auto_reset`),
        # and auto-switch then waits for the reading that shows the room.
        phase("autoreset", reset_auto.tick, self)
        phase("autoswitch", cli_auto.tick, self)
        phase("tidy", self._tidy, now)
        phase("convert", self._convert_deferred, now)
        if now - self._last_heartbeat >= HEARTBEAT:
            self._last_heartbeat = now
            self.bump()
        phase("alerts", self._evaluate_alerts, now)
        self._ticked(now)
        if self._tick_assertions:       # an invalid state stays loud in safe mode and the demo
            raise self._tick_assertions[0]

    def _schedule(self, now):
        rows = self.store.accounts()
        self._counts = self._count_accounts(rows)
        self.scheduler.set_paused(bool(self.prefs.get("refresh_paused")))
        if not self.holds and self.scheduler.tick(self.targets(now, rows), now, self.clock):
            self.bump()

    def _phase(self, name, fn, *args, event="engine.phase_failed", **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            with self._health_lock:
                entry = self._failing.setdefault(name, {"since": self.clock(), "count": 0, "error_type": None})
                entry["count"] += 1
                entry["error_type"] = type(exc).__name__
                count = entry["count"]
            if count == 1 or count % PHASE_RELOG == 0:
                diagnostics.log_exception(event, exc, phase=name, count=count)
            if isinstance(exc, AssertionError) and (safe_mode() or self.mode == "demo"):
                self._tick_assertions.append(exc)
            return None
        if name in self._failing:
            with self._health_lock:
                entry = self._failing.pop(name, None)
            if entry is not None:
                diagnostics.log("engine.phase_recovered", phase=name, count=entry["count"],
                                seconds=round(self.clock() - entry["since"]))
        return result

    def _ticked(self, now):
        real = self.real_monotonic()
        with self._health_lock:
            self._ticks += 1
            self._last_tick, self._last_tick_real = now, real
            self._beat["ticks"] += 1
            beat = None
            if real - self._beat["at"] >= HEARTBEAT_LOG:
                beat, self._beat = self._beat, {"at": real, "ticks": 0, "ok": 0, "failed": 0}
        if beat is not None:
            health = self.health()
            diagnostics.log("engine.heartbeat", ticks=beat["ticks"], total=health["ticks"], reads_ok=beat["ok"],
                            reads_failed=beat["failed"], failing=sorted(health["failing_phases"]),
                            stalled=health["stall_reasons"] or None)

    def health(self):
        """Liveness for /_health and /api/diagnostics: no secrets, only times, counts and phase names.
        `stalled`: no tick completed for 30 s, or (with terminal accounts whose checks are on) no terminal
        read succeeded for 20 min since start, the last resume or the last success. `pending`, `autoswitch` and
        `notifications` are for install.sh (it refuses to restart a service mid-switch) and `run.py --doctor`."""
        real, now = self.real_monotonic(), self.clock()
        with self._health_lock:
            ticks, last_tick, last_real = self._ticks, self._last_tick, self._last_tick_real
            failing = {name: dict(entry) for name, entry in self._failing.items()}
            reads = {name: {k: v for k, v in entry.items() if k != "ok_real"} for name, entry in self._reads.items()}
            cli_ok = (self._reads.get("claude_cli") or {}).get("ok_real")
            baseline = self._read_baseline
        reasons = []
        if not self._stopped:
            if real - (self._started_at if last_real is None else last_real) >= STALL_TICK:
                reasons.append("tick")
            paused = (bool(self.prefs.get("refresh_paused")) or self.scheduler.provider_pause.get("claude_cli")
                      or bool(self.cli.adapter.readonly_reason))
            if (self._counts.get("claude_cli") and not paused
                    and real - max(baseline, cli_ok if cli_ok is not None else baseline) >= STALL_READ):
                reasons.append("claude_cli_reads")
        return {"ticks": ticks, "last_tick_at": last_tick,
                "tick_age_s": None if last_real is None else round(real - last_real, 1), "now": now,
                "failing_phases": failing, "reads": reads, "stalled": bool(reasons), "stall_reasons": reasons,
                "pending": {key: pending[key] for key in ("kind", "provider", "phase")} if (pending := self.pending) else None,
                "autoswitch": {"enabled": bool(self.prefs.get("cli_autoswitch")), "paused": cli_auto.paused(self)},
                "notifications": self._notification_state()}

    def _loop(self):
        try:
            while not self.stopping.is_set():
                wait = self.tick_seconds
                try:
                    self.tick()
                    wait = self.watch.tick_wait(self.tick_seconds)
                except Exception as exc:
                    diagnostics.log_exception("engine.tick_failed", exc)
                self._wake.wait(wait)
                self._wake.clear()
        except BaseException as exc:         # the end of this thread: the watchdog sees it, the log says why
            diagnostics.log_exception("engine.tick_died", exc, lvl="error")
            raise

    def wake(self):
        """The next tick runs now rather than after the tick loop's sleep."""
        self._wake.set()

    def _check_resume(self, now):
        wall, mono = self.real_time(), self.real_monotonic()
        jumped = wall - self._wall > 3 * self.tick_seconds
        # The wall clock ran on while the monotonic one stood still: the Mac slept (a tick that only took long is no
        # sleep, and usage went on under it). The burn-rate forecast forgets the readings from before (cli_auto).
        if (wall - self._wall) - (mono - self._mono) > WATCHDOG_GAP:
            self.woke_at = now
        self._wall, self._mono = wall, mono
        if jumped:
            self._read_baseline = self.real_monotonic()
            self.scheduler.resume(now)
            self.alert_engine.resumed(now)
            for name in self.status_sources:
                self._status_due[name] = max(self._status_due.get(name, 0), now + 30)
            self.cli.recovery_wait["next"] = 0.0
            diagnostics.log("engine.resumed")
            self.bump()

    def _tick_verifiers(self, now):
        for verifier in list(self.background_verifiers):
            try:
                finished = verifier.tick(now)
            except Exception as exc:       # a broken verifier is dropped, never retried into a loop
                diagnostics.log_exception("engine.verifier_failed", exc)
                finished = True
            if finished:
                try:
                    self.background_verifiers.remove(verifier)
                except ValueError:
                    pass

    def _tidy(self, now):
        with self.lock:
            kept = [n for n in self.notices if n["sticky"] or now - n["at"] <= NOTICE_TTL]
            expired = len(kept) != len(self.notices)
            self.notices = kept
            for key, plan in list(self.plans.items()):
                expires = plan.get("expires") if isinstance(plan, dict) else None
                if isinstance(expires, (int, float)) and now - expires > PLAN_GRACE:
                    self.plans.pop(key, None)
        if expired:
            self.bump()

    def _convert_deferred(self, now):
        """Claude Desktop profiles imported from the PC before Claude Desktop was set up here (move_import)."""
        move_import.convert_deferred(self, now)

    def _evaluate_alerts(self, now):
        try:
            state = self.snapshot()
            if state["rev"] == self._alerts_rev:
                return
            latches = self.prefs.get("alert_latches")
            was_down = "down" in ((latches or {}).get("report") or {})
            # When each terminal login ends (CLIAccounts keeps it on the row): announced ahead and once over.
            logins = {row["id"]: login_ends(row["flags"]) for row in self.store.accounts()
                      if row["provider"] == "claude_cli" and not is_frozen(row)}
            new, latches = self.alert_engine.evaluate(
                self._previous_state, state, latches, now, low_left=self.prefs.get("alert_low_left"),
                reset_minutes=self.prefs.get("notify_reset_minutes"), logins=logins,
                # Whether auto-switch is on or not: nothing could take over from the active terminal account.
                stranded=cli_auto.decision(self, {**state["settings"], "cli_autoswitch": True})["exhausted"],
                report=report_status(self.store.setting("usage_shapes"), now))
            self.prefs.set_internal("alert_latches", latches)
            if was_down and "down" not in latches["report"]:       # readings returned: the alert is over
                for alert in self.store.alerts():
                    if alert["window"] == alerts.USAGE_REPORT_WINDOW and alert["level"] == "critical":
                        self.store.update_alert(alert["id"], dismissed=True)
                self.store.event("usage", "AccountDeck can read Claude's limits again")
                self.bump()
            self._previous_state, self._alerts_rev = state, state["rev"]
            if self.prefs.get("alerts_enabled") and new:
                for item in new:
                    # Stamped now, not at the tick's start: a notification merges what waits under the newest
                    # alert's text, and auto-switch's own alert of this tick says less than these.
                    self.store.add_alert({**item, "at": self.clock()})
                self.bump()
        except (DeckError, AssertionError):
            raise
        except Exception as exc:
            diagnostics.log_exception("engine.alerts_failed", exc)

    # ---- observing the machine ----
    def _observe(self, *, fresh=False):
        with self._machine_lock:
            cached = self._machine
            if not fresh and cached is not None and self.real_monotonic() - cached.taken < MACHINE_TTL:
                return cached
            try:
                rows = self.procs.snapshot()          # one look per observation, shared by everything below
                kinds = self.procs.classify(rows)
                report = activity.report(self.paths, self.procs, now=self.clock(), idle_hold_s=self.timings.idle_hold,
                                         state=self._idle_state, kinds=kinds)
                consumers = self.procs.codex_consumers(rows=rows)
                agy = self.procs.agy_consumers(rows=rows)
                ancestors = {row.pid for row in self.procs.ancestors_of_self()}
            except DeckError as exc:
                diagnostics.log("engine.observe_failed", lvl="warning", code=exc.code)
                machine = Machine.empty()
            except Exception as exc:       # snapshots build on this too; an OS error must not fail /api/state
                diagnostics.log_exception("engine.observe_failed", exc)
                machine = Machine.empty()
            else:
                mine = {row.pid for row in kinds["claude_desktop"] + kinds["claude_code"]}
                shown = (kinds["claude_desktop"], kinds["claude_code"], consumers, kinds["codex_daemon"], agy)
                machine = Machine(
                    kinds, report, consumers, activity.read_registry(self.paths), {row.pid: row.name for row in rows},
                    bool(ancestors & mine),
                    (tuple(ProcessSet.of(group).signature() for group in shown),
                     tuple((s.pid, s.status, s.name) for s in report.sessions), report.foreign, report.registry_version,
                     bool(ancestors & mine)),
                    self.real_monotonic(), tuple(agy))
            self._machine = machine
        if cached is not None and cached.fingerprint != machine.fingerprint:
            self.bump()
        return machine

    def _doctor_tick(self):
        """Starts the doctor's slow cycle when it is due, on its own thread: it reads files and starts `security`
        and `launchctl`, which the tick never waits for."""
        real = self.real_monotonic()
        if real < self._doctor_due or (self._doctor_thread is not None and self._doctor_thread.is_alive()):
            return
        self._doctor_due = real + DOCTOR_EVERY
        if self.inline:
            self._doctor_cycle()
            return
        self._doctor_thread = threading.Thread(target=self._doctor_cycle, name="ad2-doctor", daemon=True)
        self._doctor_thread.start()

    def _doctor_cycle(self):
        """`state.health`, then the Claude Code compatibility watch on it (only the copy that owns the sign-ins)."""
        try:
            self._doctor_report = report = doctor.run(self.paths, engine=self)
            self.bump()
            if self.owns_login:
                doctor.watch(self, report)
        except Exception as exc:       # a report that cannot be made must not stop anything else
            diagnostics.log_exception("engine.doctor_failed", exc)

    def _refresh_slow(self, now):
        with self._slow_lock:
            if self._slow is None or now - self._slow["at"] >= SLOW_EVERY:
                try:
                    base, kind, _reason = facts_mac.claude_base(self.paths)
                except Exception as exc:   # snapshots build on this too; fall back to the configured folder
                    diagnostics.log_exception("engine.data_folder_failed", exc)
                    base, kind = Path(self.paths.claude_profile), None
                try:
                    conversations = self.providers["codex"].sessions()
                except (DeckError, OSError, ValueError, KeyError):
                    conversations = []
                self._slow = {"at": now, "data_folder": kind, "sessions": conversations,
                              "setup": shared_setup(self.paths, base)}
            return self._slow

    def _refresh_runway(self, now):
        """The terminal pool's forecast for the state, every RUNWAY_EVERY: until the first one, or while one
        fails (the phase logs it), the state keeps what it has. It reads 6 hours of one window per account,
        a few hundred indexed rows, so it stays on the tick thread."""
        if self._runway_at is not None and 0 <= now - self._runway_at < RUNWAY_EVERY:
            return
        self._runway_at = now
        accounts = cli_auto.inputs(self)
        readings = {account["id"]: self.store.readings(account["id"], runway.WEEKLY_ID, since=now - runway.BASIS)
                    for account in accounts}
        self._runway = runway.forecast(accounts, readings, self.prefs.public(), now)
        self.bump()

    @staticmethod
    def _count_accounts(rows):
        counts = {}
        for row in rows:
            counts[row["provider"]] = counts.get(row["provider"], 0) + 1
        return counts

    # ---- the snapshot ----
    def snapshot(self, rev=None):
        """The state, or None when `rev` is the current revision."""
        with self.lock:
            if rev is not None and rev == self.rev:
                return None
            if self._cache is not None:
                return copy.deepcopy(self._cache)
        with self._build_lock:
            with self.lock:
                if self._cache is not None:
                    return copy.deepcopy(self._cache)
                built_rev = self.rev
            state = self._build(built_rev)
            with self.lock:
                if self.rev == built_rev:
                    self._cache = state
            return copy.deepcopy(state)

    def _build(self, rev):
        now = self.clock()
        settings = self.prefs.public()
        machine, slow = self._observe(), self._refresh_slow(now)
        rows = self.store.accounts()
        self._counts = self._count_accounts(rows)
        status = {name: self.status(name, now) for name in self.providers}
        refills = {}
        for refill in self.store.refills(now - REFILL_KEEP):
            refills.setdefault(refill["account"], {})[refill["window"]] = {
                "at": refill["reset_at"], "confirmed": refill["confirmed_at"], "from": refill["previous_used"]}
        accounts = [self._raw_account(row, now, status, refills.get(row["id"], {})) for row in rows]
        accounts += self._unsaved_rows(now, status)
        runner = self.runner.snapshot()
        pending = runner["pending"]
        with self.lock:
            notices = [dict(n) for n in self.notices if n["sticky"] or now - n["at"] <= NOTICE_TTL]
            login = self._public_login()
            holds = bool(self.holds)
        providers = {}
        for name in self.providers:
            providers[name] = {
                "status": status[name],
                "active": next((a["id"] for a in accounts if a["provider"] == name and a["active"]), None),
                "running": self._running(name, machine, now),
                "switching": bool(pending and pending["provider"] == name and pending["phase"] not in TERMINAL_PHASES),
                "paused": bool(self.prefs.get("refresh_paused")) or self.scheduler.provider_pause.get(name, False)}
        providers["claude"].update(data_folder=slow["data_folder"], folder_warning=None)
        providers["claude_cli"].update(autoswitch=cli_auto.state(self), enrollment=copy.deepcopy(self.cli.phase),
                                        readonly_reason=self.cli.adapter.readonly_reason,
                                        usage_shape=shape_change(self.store.setting("usage_shapes"), "claude_cli", now))
        providers["claude_cli"]["auto_reset"] = reset_auto.state(self)
        providers["claude_cli"]["runway"] = self._runway
        providers["claude_cli"]["watch"] = self.watch.state(now)
        providers["claude_cli"]["signin_due"] = alerts.signin_due(accounts, now)
        providers["antigravity"] = self._agy_block(machine, pending, now)
        raw = {"app": "accountdeck2", "version": __version__, "rev": rev, "mode": self.mode, "safe_mode": safe_mode(),
               "ready": not holds and self._ready(rows, now), "read_only": self.read_only, "settings": settings,
               "providers": providers, "accounts": accounts, "pending": pending, "report": runner["report"],
               "login": login, "recovery": {"pending": bool(self.recovery), "count": len(self.recovery)},
               "notices": notices, "alerts": self.store.alerts(), "banners": self._banners(status, machine, rows),
               "setup": slow["setup"], "sessions": slow["sessions"], "cwd": settings["cwd"],
               "events": self.store.events(), "last_manifest": self.store.setting("last_manifest"),
               "platform": contract.platform_block(self._notification_state())}
        raw["move_import"] = move_import.state(self, rows)       # the PC-to-Mac import (docs/mac/PLAN.md §2.3)
        raw["service"] = self.service_state()
        raw["health"], raw["build"] = self._doctor_report, BUILD
        raw["update"] = self.updates.view()
        state, picks = intel.enrich_state(raw, now=now, settings=settings,
                                          recommender_state=self.prefs.get("recommender_state"))
        self.prefs.set_internal("recommender_state", picks)
        errors = contract.validate_state(state)
        if errors:
            if safe_mode() or self.mode == "demo":
                raise AssertionError("Invalid engine snapshot: " + "; ".join(errors[:8]))
            if not self._invalid_logged:
                self._invalid_logged = True
                diagnostics.log("state.invalid", lvl="error", count=len(errors), first=errors[0][:200])
        return state

    def _notification_state(self):
        """macOS notification permission as the running menu bar item reports it, else "unavailable"."""
        handle = self.tray_handle
        if handle is None or not handle.running:
            return "unavailable"
        try:
            state = handle.notification_state()
        except Exception as exc:          # a broken platform answer must not fail /api/state
            diagnostics.log_exception("menubar.notification_state_failed", exc)
            return "unavailable"
        return state if state in contract.NOTIFICATION_STATES else "unavailable"

    def _ready(self, rows, now):
        """True once every account has been read (or failed to read) since start, or after a grace period."""
        if self.real_monotonic() - self._started_at >= READY_GRACE:
            return True
        if any(row["last_good_at"] is None and not row["error"] and row["id"] not in self._read_once for row in rows):
            return False
        return all(u["last_good_at"] is not None or u["error"] for u in self.accounts.unsaved.values() if u)

    def _public_login(self):
        login = self.login
        if not isinstance(login, dict) or login.get("provider") not in contract.ACCOUNT_PROVIDERS:
            return None
        return {key: value for key, value in login.items() if not str(key).startswith("_")
                and isinstance(value, (str, int, float, bool, type(None)))}

    def _running(self, provider, machine, now):
        kinds = machine.kinds
        if provider == "claude_cli":
            return {"checked": now}
        if provider == "claude":
            report = machine.report
            sessions = []
            for s in report.sessions:
                folder = Path(str((machine.registry.get(s.pid) or {}).get("cwd") or "")).name or None
                sessions.append({"pid": s.pid, "name": s.name, "status": s.status, "since": s.since, "cwd_short": folder})
            return {"desktop": bool(kinds["claude_desktop"]), "desktop_pids": len(kinds["claude_desktop"]),
                    "checked": now, "sessions": sessions, "busy": report.busy, "unknown": report.unknown,
                    "foreign_claude": report.foreign, "registry_version": report.registry_version}
        consumers = [{"pid": row.pid, "kind": CONSUMER_KINDS[row.kind], "parent": machine.names.get(row.parent),
                      "started": unix_time(row.ctime)} for row in machine.consumers if row.kind in CONSUMER_KINDS]
        return {"consumers": consumers, "daemon": bool(kinds["codex_daemon"]), "desktop": bool(kinds["codex_desktop"]),
                "checked": now}

    def _agy_block(self, machine, pending, now):
        """`providers.antigravity`: the keys every app has, then AgyService's own. No status page is read for it
        (AccountDeck sends Google nothing), and its processes are unknown while the machine cannot be observed."""
        block = self.agy.state(now)
        consumers = [{"pid": row.pid, "kind": "agy", "parent": machine.names.get(row.parent),
                      "started": unix_time(row.ctime)} for row in machine.agy]
        return {"status": intel.classify_outage("antigravity", None, None, now=now),
                "active": next((a["id"] for a in block["accounts"] if a["active"]), None),
                "running": {"consumers": consumers, "checked": None if machine.fingerprint == ("unavailable",) else now},
                "switching": bool(pending and pending["provider"] == "antigravity"
                                  and pending["phase"] not in TERMINAL_PHASES),
                "paused": bool(self.prefs.get("refresh_paused")) or self.scheduler.provider_pause.get("antigravity", False),
                **block}

    def _raw_account(self, row, now, status, refills):
        name, key, flags = row["provider"], row["id"], row["flags"]
        active = row["identity"] == self.active_identity.get(name)
        error = None
        if row["error"]:
            error = {"code": row["error_code"] or "network", "message": row["error"],
                     "since": flags.get("error_since", now), "outage": status[name]["level"] in ("degraded", "outage")}
        paused = row["paused_until"] if row["paused_until"] and row["paused_until"] > now else None
        active_at = flags.get("last_active_at")
        samples, trend = self.accounts.series(row, now)
        return {"id": key, "provider": name, "label": row["label"], "email": row["email"], "plan": row["plan"],
                # A login lost with a newer one saved here asks no sign-in: that one is on its way in (cli_auto._repair).
                **({"excluded": flags.get("excluded", False),
                    "health": "ok" if row["error"] == LIVE_LOST else flags.get("health", "ok"),
                    "live_expired": flags.get("live_expired", False),
                    "login_ends_at": None if is_frozen(row) else login_ends(flags)} if name == "claude_cli" else {}),
                "saved": True, "active": active,
                "switchable": bool(flags.get("restorable", True)) and not is_frozen(row),
                "imported_from": row["imported_from"], "owner": row["owner"], "usage": row["last_good_usage"],
                "credential": {"state": "live" if active and row["credential_state"] not in DEAD_CREDENTIALS
                               else row["credential_state"],
                               "expires_in": None if row["expires_at"] is None else row["expires_at"] - now,
                               "last_outcome": row["last_outcome"],
                               "dormant_days": None if active_at is None else max(0, int((now - active_at) / 86400)),
                               "session_expires_in_days": None if flags.get("session_expires_at") is None
                               else (flags["session_expires_at"] - now) / 86400,
                               "revoked_hint": bool(flags.get("revoked_hint"))},
                "last_good_at": row["last_good_at"], "checked": flags.get("checked", row["last_good_at"]),
                "refreshing": self.scheduler.running(key), "error": error, "paused_until": paused,
                "next_check": self.scheduler.planned(key), "sort_order": row["sort_order"], "refills": refills,
                "samples": samples, "trend": trend, "limit_resets": self.resets.view(row, now)}

    def _unsaved_rows(self, now, status):
        rows = []
        for name, row in self.accounts.unsaved.items():
            if row is None:
                continue
            key = contract.UNSAVED_IDS[name]
            error = copy.deepcopy(row["error"])
            if error:
                error["outage"] = status[name]["level"] in ("degraded", "outage")
            rows.append({"id": key, "provider": name, "label": row["label"] or f"Current {name.title()} account",
                         "email": row["email"], "plan": row["plan"], "saved": False, "active": True,
                         "switchable": False, "usage": row["usage"], "last_good_at": row["last_good_at"],
                         "checked": row["checked"], "error": error, "refreshing": self.scheduler.running(key),
                         "next_check": self.scheduler.planned(key),
                         "credential": {"state": "live", "expires_in": row["expires_in"],
                                        "last_outcome": row["last_outcome"], "dormant_days": None,
                                        "session_expires_in_days": None, "revoked_hint": False},
                         "limit_resets": self.resets.view_unsaved(name, row)})
        return rows

    def _banners(self, status, machine, rows):
        items = []
        if self.mode == "demo":
            items.append(banner("demo", "Demo mode", "Demo mode — synthetic accounts; no real apps are touched."))
        if safe_mode():
            items.append(banner("safe_mode", "Safe mode",
                                "Safe mode is on: AccountDeck 2 will not touch real apps or files."))
        if not self.owns_login:
            items.append(banner("read_only", *READ_ONLY_OTHER))
        if self.recovery:
            items.append(banner("recovery", *self.recovery_words(), actions=["recover"],
                                provider="claude_cli" if self.recovering("claude_cli") else None))
        if self.prefs.get("refresh_paused"):
            items.append(banner("paused", "Usage checks are paused", "Turn them back on in Settings."))
        if machine.self_protect:
            items.append(banner("self_protect", *SELF_PROTECT))
        for name, page in status.items():
            if page["level"] not in ("degraded", "outage"):
                continue
            if name == "claude_cli" and page["source"] == "statuspage" and status["claude"]["level"] == page["level"]:
                continue        # the terminal reads Claude's own status page: one banner says it
            title = (page["incident"] or {}).get("title") or page["summary"]
            shown = intel.PROVIDER_NAMES[name]
            if page["level"] == "outage":
                items.append(banner("outage", f"{shown} is having an incident",
                                    title.rstrip(".") + "." + OUTAGE_TAIL, provider=name, link=page["url"]))
            else:
                items.append(banner("degraded", f"{shown} is running slowly",
                                    title.rstrip(".") + ". Readings may be delayed; your last good readings are kept.",
                                    provider=name, link=page["url"]))
        items += move_import.banners(rows)
        return items

    # ---- settings ----
    def settings(self):
        return self.prefs.public()

    def update_settings(self, patch):
        """Saves `patch` and applies its desktop side: `start_on_login` is the per-user LaunchAgent
        (autostart → platform/autostart_mac.py) and `tray` shows or hides the menu bar item
        (platform/statusbar_mac.py). Safe mode stores the values and touches nothing."""
        validate_settings(patch)
        if "start_on_login" in patch and not safe_mode():
            from . import autostart
            autostart.set_enabled(patch["start_on_login"])
        changed = self.prefs.update(patch)
        if changed:
            diagnostics.log("settings.changed", keys=sorted(changed))
        if "log_level" in changed:
            diagnostics.set_level(changed["log_level"])
        if "tray" in changed and not safe_mode():
            self._show_menu_bar(changed["tray"])
        if "update_check" in changed:
            self.updates.wake()
        if "auto_reset" in changed:
            reset_auto.rearm(self)           # off and on again is how the user starts it after it stopped itself
        if changed.get("claude_resets"):
            keys = [row["id"] for row in self.store.accounts() if row["provider"] in ("claude", "claude_cli")]
            now = self.clock()
            self.resets.note_asked(keys, now)
            for key in keys:
                self.scheduler.request(key, now)
        self.bump()

    def _show_menu_bar(self, wanted):
        handle = self.tray_handle
        if wanted:
            if handle is not None and handle.running:
                return                      # `run.py --app` already shows it: never a second item
            try:
                from .platform import statusbar_mac
                self.attach_tray(statusbar_mac.start(self))
            except Exception as exc:
                diagnostics.log_exception("menubar.start_failed", exc)
                self.notice("The menu bar item could not be shown.", "warning")
        else:
            if handle is not None:
                handle.stop()
            self.attach_tray(None)

    def attach_tray(self, handle):
        self.tray_handle = handle

    # ---- actions ----
    def action(self, name, body):
        if name not in contract.ACTIONS or not isinstance(body, dict):
            raise DeckError("That action is not available.", "invalid")
        if name in ("switch-plan", "switch", "cli-exclude", "refresh", "use-reset") and isinstance(body.get("id"), str):
            row = self.store.account(body["id"])
            if is_frozen(row):
                raise FrozenAccountError(row)   # every provider's switch and read path, before any planner runs
        if move_import.check_action(self, name, body):      # accounts still on the PC, Desktop waiting to convert
            self.bump()
            return {}
        handler = getattr(self, "_do_" + name.replace("-", "_"), None)
        if handler is None:
            return self._dispatch(name, body)
        result = handler(body)
        self.bump()
        return result or {}

    def _dispatch(self, name, body):
        result = switching.dispatch(self, name, body)
        self.bump()
        return result or {}

    def _account_id(self, body):
        key = body.get("id")
        if not isinstance(key, str) or not key:
            raise DeckError("Choose an account.", "invalid")
        return key

    def _do_demo_clock(self, body):
        seconds = body.get("seconds")
        if self.mode != "demo" or not safe_mode() or not hasattr(self.clock, "advance"):
            raise DeckError("The clock control is only available in the synthetic demo.", "safety")
        if type(seconds) not in (float, int) or not -86400 <= seconds <= 86400:
            raise DeckError("Choose a demo offset within one day.", "invalid")
        self.paths.require_cli_paths()
        self.clock.advance(seconds - getattr(self, "_demo_offset", 0))
        self._demo_offset = seconds
        self.scheduler.request_all([r["id"] for r in self.store.accounts()], self.clock())
        return {"now": self.clock()}

    def _do_refresh(self, body):
        if self._agy_action(body):
            return self.agy.refresh()
        now = self.clock()
        key = body.get("id")
        if self.prefs.get("refresh_paused"):
            raise DeckError("Usage checks are paused. Turn them back on in Settings.", "invalid")
        targets = self.targets(now)
        if key is None:
            due = [t.id for t in targets if not t.paused_until or t.paused_until <= now]
            self.resets.note_asked(due, now)
            self.scheduler.request_all(due, now)
            return None
        target = next((t for t in targets if t.id == key), None) if isinstance(key, str) else None
        if target is None:
            raise DeckError("That account no longer exists.", "not_found")
        if target.paused_until and target.paused_until > now:
            raise DeckError("Usage checks are paused for this account.", "rate_limited", target.paused_until - now)
        self.resets.note_asked([key], now)
        self.scheduler.request(key, now)
        return None

    def _do_rename(self, body):
        key = self._account_id(body)
        if key in contract.UNSAVED_IDS.values():
            raise DeckError("Save this account first.", "invalid")
        if self.agy.owns(key):
            return self.agy.rename(key, body.get("label"))
        row = self.store.account(key)
        if row and row["provider"] == "claude_cli":
            self.cli.rename(key, body.get("label"))
        else:
            self.accounts.rename(key, body.get("label"))

    def _do_reorder(self, body):
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise DeckError("Provide an account order.", "invalid")
        if body.get("provider") == "antigravity" or (ids and all(self.agy.owns(i) for i in ids)):
            return self.agy.reorder(ids)
        self.store.reorder(ids)

    def _do_remove(self, body):
        key = self._account_id(body)
        self._own_the_login()
        if key in contract.UNSAVED_IDS.values():
            raise DeckError("This account isn't saved yet, so there is nothing to remove.", "invalid")
        if self.agy.owns(key):
            return self.agy.remove(key)
        account = self.store.account(key)
        if account is None:
            raise DeckError("That account no longer exists.", "not_found")

        def remove():
            self.resets.check_remove(account)
            if account["provider"] == "claude_cli":
                with self.scheduler.account_lock(key):
                    if is_frozen(self.store.account(key)):
                        # Remote rows have no usable local login. Keep any retained move
                        # recovery credentials until confirmation; remove only metadata.
                        self.store.remove(key)
                        op.metadata_only = True
                        self.scheduler.forget(key)
                        self.bump()
                        return
                    with self.cli.gate(key):
                        live = self.cli.adapter.current()
                        if live["identity"] == account["identity"]:
                            raise DeckError("The account in use by your terminals cannot be removed.", "invalid")
                        self.accounts.remove(key)
                self.scheduler.forget(key)
                self.resets.forget(key)
                return
            self.accounts.remove(key)
            self.scheduler.forget(key)
            self.resets.forget(key)
        op = ServiceOperation("remove", remove, provider=account["provider"], account=key,
                              label=account["label"], message="Removing the account…",
                              step=("remove", "Removing the account"))
        return self.submit(op)

    def _do_cancel(self, body):
        self.runner.cancel(body.get("op"))

    def _do_dismiss(self, body):
        wanted = body.get("notice")
        with self.lock:
            self.notices = [n for n in self.notices if wanted is not None and n["id"] != wanted]

    def _do_dismiss_alert(self, body):
        self.store.update_alert(body.get("alert"), dismissed=True)

    def _do_snooze_alert(self, body):
        minutes = body.get("minutes")
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise DeckError("Choose 1 to 1440 minutes.", "invalid")
        self.store.update_alert(body.get("alert"), snoozed_until=self.clock() + minutes * 60)

    def _do_dismiss_report(self, body):
        self.runner.dismiss_report()

    # The Mac side of the move (docs/mac/PLAN.md §2.3-§2.5; accountdeck/move_import.py has the shapes).
    def _do_import_bundle_preview(self, body):
        if self.runner.busy:
            raise DeckError("Finish or cancel the current action first.", "busy")
        return {"preview": move_import.preview(self, body.get("path"), body.get("passphrase"))}

    def _do_import_bundle(self, body):
        self._own_the_login()
        return move_import.start_import(self, body)

    def _do_import_bundle_dismiss(self, body):
        move_import.dismiss(self, body.get("bundle_id"))

    def _do_open_folder(self, body):
        folders = {"data": self.paths.data_dir, "logs": self.paths.data_dir / "logs"}
        if body.get("which") not in folders:
            raise DeckError("Choose data or logs.", "invalid")
        self.procs.open_folder(folders[body["which"]])

    def _do_test_notification(self, body):
        handle = self.tray_handle
        if handle is not None and handle.running:
            handle.test_notification()
            return {}
        raise DeckError("Notifications need the menu bar item. Turn it on in Settings.", "unsupported")

    # The update check (accountdeck/updates.py has the shapes and the rules).
    def _do_check_update(self, body):
        return self.updates.check_now()

    def _do_skip_update(self, body):
        return self.updates.skip(body.get("version"))

    def _do_start_update(self, body):
        return self.updates.start_update(body.get("version"))

    def _do_agy_accept_notice(self, body):
        return self.agy.accept_notice()

    def _do_reset_plan(self, body):
        """What using one of this account's limit resets would do (resets.py); nothing is sent to a provider."""
        plan = self.resets.plan(body.get("id"))
        errors = contract.validate_reset_plan(plan)
        if errors and (safe_mode() or self.mode == "demo"):
            raise AssertionError("Invalid limit reset plan: " + "; ".join(errors[:8]))
        return {"plan": plan}

    def _do_use_reset(self, body):
        """Uses the one limit reset the user confirmed in a plan, as an operation (resets.py)."""
        return self.resets.use(body.get("id"), body.get("plan_id"), body.get("choice"))

    def _agy_action(self, body):
        """True when an action names Antigravity: its app, or one of its account ids (AgyService.owns)."""
        return body.get("provider") == "antigravity" or self.agy.owns(body.get("id"))

    def looked(self):
        """The user looked at AccountDeck (the menu bar's menu opened, a dashboard page loaded): the update check asks
        GitHub in the background when its last answer is over a minute old, and Antigravity's usage may be read
        (AgyService.looked). Returns at once, True when that changed the state (a usage run started); never raises."""
        self.updates.look()
        self.resets.note_looked(self.clock())
        try:
            return bool(self.agy.looked())
        except Exception as exc:
            diagnostics.log_exception("agy.looked_failed", exc)
            return False

    def _own_the_login(self):
        if not self.owns_login:
            raise DeckError(OTHER_OWNER, "read_only")

    # ---- after an operation ----
    def _settled(self, op):
        """Runs on `ad2-ops` after run() returned and before the operation shows as ended, so whoever
        sees it finished also sees the account it signed in."""
        if not getattr(op, "metadata_only", False):
            self.sense_active()

    def _finished(self, op, report):
        """Runs on `ad2-ops` after any operation ended (`report` is None for a `needs_reconfirm`)."""
        self.recovery = self._scan_recovery()
        now = self.clock()
        if report is not None:
            # First: how a switch left for Recover ended is known before its alert says so.
            self._record_outcome(op, report, now, switch_cli.auto_recover(self, ending=True))
        for name in ([op.provider] if op.provider else list(self.providers)):
            active = self.store.find(name, self.active_identity.get(name))
            if active is not None and report is not None and op.kind != "remove":
                self.scheduler.request(active["id"], now)
        self.bump()

    def _record_outcome(self, op, report, now, settled=None):
        """`settled`: what `auto_recover` did as this job ended."""
        if getattr(op, "preempted", None) and report["outcome"] == "cancelled":
            cli_auto.made_way(self, op)
        # A use of a limit reset records its own line: who, which reset, the outcome (resets.ResetOperation). An
        # automatic one tells of itself (reset_auto.finished: its event, its alert, what the next tick waits for).
        told = reset_auto.finished(self, op, report, now)
        summary = diagnostics.redact_text(getattr(op, "event_text", None) or report["summary"] or "")
        if not told and report["outcome"] in ("success", "failed") and op.kind != "remove" and summary and summary != "Done.":
            self.store.event(op.kind, summary)
        if report.get("manifest"):
            self.store.set_setting("last_manifest", report["manifest"])
        if op.kind == "switch" and op.provider and report["outcome"] in ("success", "failed"):
            verified = report["verified"] or ("failed" if report["outcome"] == "failed" else "unconfirmed")
            # A failed automatic terminal switch alerts at most hourly while it goes on (cli_auto.switch_settled),
            # except when it stays unsettled (`recovery` was just scanned), for which auto-switch waits. A repair that
            # found nothing to do counts as a failure there, so that it is not asked for on every tick, and says nothing.
            auto_kind, environment = getattr(op, "auto_kind", "manual"), getattr(op, "environment", None)
            changed = getattr(op, "changed", True)
            repeat = op.provider == "claude_cli" and cli_auto.switch_settled(
                self, op.account, ok=report["outcome"] == "success" and changed, automatic=auto_kind != "manual",
                now=now, environment=environment, repair=auto_kind == "repair")
            interrupted = op.provider == "claude_cli" and report["outcome"] == "failed" and self.recovering("claude_cli")
            told = (self.watch.switched(op, now) if op.provider == "claude_cli" and report["outcome"] == "success"
                    and auto_kind != "repair" else "")       # a repair stays on the account: its limit episode goes on
            if self.prefs.get("alerts_enabled") and (interrupted or not repeat) and changed:
                alert = alerts.switch_result(op.label or "your account", op.provider, verified, now)
                if auto_kind == "repair" and report["outcome"] == "failed":
                    alert.update(title=f"Couldn't give your terminals {op.label}'s newest login")
                if op.provider == "claude_cli" and getattr(op, "reason", "") and verified == "verified":
                    alert.update(title="Terminal login updated" if auto_kind == "repair" else "Switched terminals",
                                 message=op.reason + told)
                elif interrupted:
                    alert.update(message=self.recovery_words()[1])
                elif settled and report["error"]:
                    alert.update(message=f"{report['error']['message'].rstrip('.')}. {settled}")
                elif environment and auto_kind not in ("manual", "repair"):
                    alert.update(title="Terminal auto-switch can't switch right now", message=environment, level="warning")
                elif report["verified"] is None and report["error"]:
                    # It failed before any sign-in was checked (a stale confirmation, say): its own reason, not
                    # "opened a different account".
                    alert.update(message=report["error"]["message"])
                self.store.add_alert(alert)

    # ---- what the server asks for ----
    def history(self, account, since=None, limit=700):
        if account in contract.UNSAVED_IDS.values() or self.agy.owns(account):     # Antigravity keeps no history
            return {"account": account, "readings": [], "snapshots": []}
        if not self.store.account(account):
            raise DeckError("That account no longer exists.", "not_found")
        return self.store.history(account, since=since, limit=limit)

    def manifest(self, op=None):
        directory = self.paths.data_dir / "logs" / "manifests"
        try:
            if op is not None:
                return Manifest.load(op, directory=directory) if op and set(op) <= MANIFEST_ID else None
            last = self.store.setting("last_manifest")
            found = Manifest.load(last, directory=directory) if isinstance(last, str) and set(last) <= MANIFEST_ID else None
            return found or Manifest.latest(directory=directory)
        except (OSError, ValueError):
            return None

    def diagnostics_tail(self, lines=200):
        return {"lines": diagnostics.tail(lines),
                "path": str(diagnostics.log_path() or self.paths.data_dir / "logs" / diagnostics.LOG_FILE),
                "health": self.health()}

    # ---- the service looking after itself ----
    def start_watchdog(self, relaunch=None):
        """Starts the watchdog (run.py: never in the demo, nor in tests unless they ask). `relaunch` replaces this
        process when nothing would restart it."""
        self.watchdog = Watchdog(self, relaunch=relaunch)
        self.watchdog.start()
        return self.watchdog

    def service_state(self):
        """The `service` block of the state, and what /_health adds: from memory, no lock or query, as it is polled.
        Times, counts and fixed words only: no paths and nothing secret."""
        last = self._last_tick_real
        watchdog = self.watchdog.view() if self.watchdog is not None else {
            "state": "off", "tick_age_s": None if last is None else round(self.real_monotonic() - last, 1),
            "restarts_last_hour": 0, "reads_stuck": 0, "last": None}
        restored = self._restored or {}
        return {"watchdog": watchdog, "db": {**self.store.care(), "restored_from": restored.get("backup_at")}}

    def announce(self, *, moved=None):
        """What this start has to say, as alerts: the database went back to a backup, the watchdog restarted the
        service, the port was taken (`moved`: wanted, chosen). run.py calls it once the notifier runs, which delivers
        only what is raised after it starts. Only the critical one ignores `alerts_enabled`."""
        try:
            now, told = self.clock(), []
            note = self._restored
            if note is not None:
                if not note.get("alerted"):
                    self.store.add_alert(alerts.database_restored(note.get("backup_at"), now, why=note.get("why"),
                                                                  back=note["back"], lost=note["lost"], checked=note["checked"]))
                    note["alerted"] = True
                self.store.set_setting(RESTORED, None if note["checked"] else note)   # unchecked: the next start goes on
            restart = self.watchdog.take_pending() if self.watchdog is not None else None
            if restart is not None:
                told.append(alerts.restarted_itself(restart.get("reason"), now, interrupted=restart.get("op")))
            if moved is not None:
                alert = alerts.port_moved(*moved, now)
                if not any(old["title"] == alert["title"] and now - old["at"] < 86400 for old in self.store.alerts()):
                    told.append(alert)
            if self.prefs.get("alerts_enabled"):
                for alert in told:
                    self.store.add_alert(alert)
            self.bump()
        except Exception as exc:     # the service goes on: the log and the state still say what happened
            diagnostics.log_exception("engine.announce_failed", exc)

    def _reconcile_restored(self):
        """core._restore put an older database in place; the vault stayed as it was. Every login, and what guards
        it (a renewal's stash, its last attempt, whether it is dead), is in the vault alone, so no refresh token can
        be sent twice through this: reads and renewals only ever use the newest record. But the rows may be older than
        the records. Before anything reads a row, then: an account added since the backup gets its row back under its
        own key (`mirror` would save its live login as a new account), and each row follows its record. A row whose
        record is gone stays and asks for sign-in. Safe to repeat: a keychain that does not answer leaves the note in
        place for the next start. The note, for `announce`; None when nothing was restored."""
        note = self.store.setting(RESTORED)
        if not isinstance(note, dict):
            return None
        back, lost, checked = int(note.get("back") or 0), 0, True
        try:
            known = {row["id"] for row in self.store.accounts()}
            for key in sorted(self.vault.account_keys() - known):
                back += self._readopt(key)
            for row in self.store.accounts():
                if not is_frozen(row):
                    lost += self._follow_record(row)
            agy_back, agy_lost = self.agy.reconcile_restored()      # Antigravity's own table and records
            back, lost = back + agy_back, lost + agy_lost
        except (DeckError, OSError) as exc:
            diagnostics.log_exception("db.restore_unchecked", exc)
            checked = False
        note.update(back=back, lost=lost, checked=checked)
        self.store.set_setting(RESTORED, note)
        diagnostics.log("db.restored", lvl="error", why=note.get("why"), backup_at=note.get("backup_at"), back=back,
                        lost=lost, checked=checked)
        return note

    def _readopt(self, key):
        """A row for the account record `key`, which has none: 1 when it got one. Read offline, without a provider."""
        try:
            record = self.vault.get(key)
        except KeychainError:
            raise
        except DeckError:
            diagnostics.log("db.record_unreadable", lvl="warning", acct=diagnostics.account_ref(key))
            return 0
        account = _record_account(record)
        if account is None:
            diagnostics.log("db.record_unknown", lvl="warning", acct=diagnostics.account_ref(key))
            return 0
        other = self.store.find(account[0], account[1])
        if other is not None:
            if is_frozen(other) or self.vault.path(other["id"]).exists():
                diagnostics.log("db.record_left", lvl="warning", acct=diagnostics.account_ref(key))
                return 0
            self.store.remove(other["id"])      # removed and added again since the backup: this record is the account
        self.store.adopt_account(key, *account)
        return 1

    def _follow_record(self, row):
        """Makes a row say what its vault record says, as the save or renewal it missed would have: 1 when the
        record is gone or unreadable. `refresh_attempt` starts over: an older one could only bring a renewal forward."""
        key, provider, flags = row["id"], row["provider"], dict(row["flags"])
        try:
            record = self.vault.get(key)
        except KeychainError:
            raise
        except DeckError:
            flags["restorable"] = False
            if provider == "claude_cli":
                flags["health"] = "needs_signin"
            self.store.update(key, flags=flags, credential_state="reauth_required", error=LOST_LOGIN,
                              error_code="reauth_required")
            return 1
        fields = {}
        if provider == "claude_cli":
            credentials = record.get("credentials")
            health = record.get("health", "ok") if credentials else "needs_signin"
            flags = with_end(flags, credentials)
            if unheld(record) and flags.get("login_ends_at"):
                flags["signed_in_end"] = flags["login_ends_at"]
            else:
                flags.pop("signed_in_end", None)
            flags.update(health=health, restorable=health == "ok", excluded=bool(record.get("excluded")), live_expired=False)
            fields = ({"credential_state": "fresh", "error": None, "error_code": None} if health == "ok"
                      else {"error": ON_HOLD, "error_code": "unsupported"} if health == "on_hold"
                      else {"credential_state": "reauth_required", "error": REAUTH, "error_code": "reauth_required"})
            if credentials:
                fields["expires_at"] = expires(credentials)
        elif provider == "codex":
            flags.update(restorable=True, refresh_attempt=self.clock())
        else:
            flags["restorable"] = restorable(provider, record)
            flags.pop("outcomes", None)         # about an older login, perhaps (`_save_over`)
        self.store.update(key, flags=flags, **fields)
        return 0

    def stop(self):
        """Cancels what can be cancelled and stops every background thread; safe to call twice."""
        with self.lock:
            if self._stopped:
                return
            self._stopped = True
        self.stopping.set()
        self._wake.set()
        self.watch.stop()
        self.updates.stop()
        if self.tray_handle is not None:
            try:
                self.tray_handle.stop()
            except Exception as exc:
                diagnostics.log_exception("menubar.stop_failed", exc)
        self.runner.stop(self.timings.lock_wait + self.timings.verify_hard + self.timings.graceful_wait + 30)
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(15)
        for thread in self._status_threads:
            thread.join(5)
        if self._doctor_thread is not None:
            self._doctor_thread.join(5)
        self.scheduler.shutdown()
        for close in self.closers:
            try:
                close()
            except Exception as exc:
                diagnostics.log_exception("engine.closer_failed", exc)
        self.store.close()




def banner(kind, title, message, *, actions=(), provider=None, link=None):
    return {"kind": kind, "title": title, "message": message, "actions": list(actions), "provider": provider,
            "link": link}


def _server_names(path, key, fmt):
    """(present, [server names], error) of one config file: names only, never commands or secrets."""
    path = Path(path)
    if not path.is_file():
        return False, [], False
    try:
        if path.stat().st_size > MAX_SETUP_BYTES:
            return True, [], True
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text) if fmt == "toml" else json.loads(text)
        servers = data.get(key) if isinstance(data, dict) else None
        return True, [str(name)[:80] for name in servers][:50] if isinstance(servers, dict) else [], False
    except (OSError, ValueError):
        return True, [], True


def shared_setup(paths, claude_base):
    """The `setup` block: which MCP definitions the two apps share (names only)."""
    groups = []
    for label, path, key, fmt in (("Codex settings & MCPs", paths.codex_home / "config.toml", "mcp_servers", "toml"),
                                  ("Claude Code settings & MCPs", paths.claude_user_config, "mcpServers", "json"),
                                  ("Claude Chat local MCPs", Path(claude_base) / "claude_desktop_config.json",
                                   "mcpServers", "json")):
        present, servers, error = _server_names(path, key, fmt)
        groups.append({"label": label, "present": present, "servers": servers, "error": error})
    return {"message": SETUP_MESSAGE, "note": SETUP_NOTE, "groups": groups}


def _record_account(record):
    """(provider, identity, label, email, plan, org) from a saved account's vault record, or None."""
    if record.get("provider") == "claude_cli":
        email = record.get("email") or ""
        account = ("claude_cli", record.get("identity"), record.get("label") or email.partition("@")[0] or "Terminal account",
                   email, record.get("plan") or "", record.get("organizationUuid") or "")
    elif isinstance(record.get("tokens"), dict):
        try:
            info = Codex.identity(record)
        except DeckError:
            return None
        account = ("codex", info["identity"], default_label(info["email"], "codex"), info["email"], info["plan"], info["org"])
    else:
        account = ("claude", record.get("identity"), default_label("", "claude"), "", "", record.get("org") or "")
    return account if isinstance(account[1], str) and account[1] else None


def _stack(frame, limit=30):
    """Where a thread is, outermost first, as `module:line:function`: frames only, never a value."""
    found = []
    while frame is not None and len(found) < limit:
        found.append(f"{Path(frame.f_code.co_filename).stem}:{frame.f_lineno}:{frame.f_code.co_name}")
        frame = frame.f_back
    return found[::-1]


class Watchdog:
    """Restarts a service that stopped working (`ad2-watchdog`, a daemon thread). Hung means no tick completed for
    WATCHDOG_TICK, or the tick thread is gone, or reads held every refresh worker and none ended for WATCHDOG_READS;
    while an operation is changing something all of these wait until WATCHDOG_MUTATION. The state and every thread's
    stack then go to the log, why goes to `watchdog.json`, and the process exits WATCHDOG_EXIT: launchd restarts it,
    the kernel releases its port and locks, and recovery records answer for whatever was cut short. Past
    RESTARTS_PER_HOUR it stays up and raises one critical alert instead.

    Time is `real_monotonic`, which stands still while the Mac sleeps; a look that comes late, or clocks that moved
    apart, start the counting over, so a wake (or a stopped process, or a changed clock) never fires it. It takes no
    lock a hung thread could hold, and its exit has a deadline. After stop() it only sees that a Quit ends: a stuck
    read holds a worker, which Python would wait for forever. It also starts the database's daily check and backup.
    Not covered: a hang that holds the GIL, which stops this thread too."""

    def __init__(self, engine, *, relaunch=None):
        self.engine, self.relaunch = engine, relaunch
        self.path = Path(engine.paths.data_dir) / WATCHDOG_FILE
        self.status, self.stuck = "watching", 0
        self.since = self._looked = engine.real_monotonic()      # counting starts here: start, or the last gap in time
        self._looked_wall = engine.real_time()
        self._pinned, self._quit_since, self._exit_asked = None, None, None
        self._care_thread, self._care_tried, self._damage_told, self._failures = None, 0.0, False, 0
        self.saved = self._load()

    def start(self):
        threading.Thread(target=self._run, name="ad2-watchdog", daemon=True).start()

    def _run(self):
        while True:
            time.sleep(WATCHDOG_EVERY)
            try:
                self.check()
            except Exception as exc:
                self._failures += 1
                if self._failures % PHASE_RELOG == 1:
                    diagnostics.log_exception("watchdog.failed", exc, count=self._failures)

    def _load(self):
        try:
            saved = read_json(self.path)
        except (OSError, ValueError):
            return {}
        return saved if isinstance(saved, dict) else {}

    def _save(self):
        try:
            atomic_write(self.path, json.dumps(self.saved).encode("utf-8"))
        except OSError as exc:
            diagnostics.log_exception("watchdog.save_failed", exc)

    def _restarts(self, wall):
        """The times of the watchdog's restarts in the last hour."""
        found = self.saved.get("restarts")
        return [at for at in found if isinstance(at, (int, float)) and wall - at < 3600] if isinstance(found, list) else []

    def view(self):
        engine, last = self.engine, self.engine._last_tick_real
        return {"state": self.status, "tick_age_s": None if last is None else round(engine.real_monotonic() - last, 1),
                "restarts_last_hour": len(self._restarts(engine.real_time())), "reads_stuck": self.stuck,
                "last": self.saved.get("last")}

    def take_pending(self):
        """Why the last run ended (`Engine.announce`), once."""
        pending = self.saved.pop("pending", None)
        if pending is not None:
            self._save()
        return pending if isinstance(pending, dict) else None

    def request_exit(self, reason):
        """A restart for `reason` as soon as nothing is being changed (at most WATCHDOG_MUTATION from now)."""
        if self._exit_asked is None:
            self._exit_asked = (reason, self.engine.real_monotonic())

    def _mutation(self):
        """The operation changing something now, or None. Read without `runner.lock`. One in `waiting_idle` has
        changed nothing yet, and it can wait there for hours: ending it costs only the wait."""
        runner = self.engine.runner
        op = runner._current
        if op is None or op.phase == "waiting_idle" or (op.done_event.is_set() and not runner._settling):
            return None
        return op

    def _reads_held(self, now):
        """How long reads have held every worker with none ending (0 when they do not)."""
        engine = self.engine
        if not engine.scheduler.pinned():
            self._pinned = None
            return 0.0
        if self._pinned is None or self._pinned[1] != engine._read_done:
            self._pinned = (now, engine._read_done)
        return now - max(self._pinned[0], self.since)

    def _note_stuck_reads(self, now):
        stuck = 0
        for thread, read in list(self.engine._reading.items()):
            age = now - max(read["since"], self.since)
            if age < READ_STUCK:
                continue
            stuck += 1
            if not read["logged"]:
                read["logged"] = True
                frame = sys._current_frames().get(thread)
                diagnostics.log("refresh.stuck", lvl="warning", acct=diagnostics.account_ref(read["key"]), seconds=round(age),
                                frames=_stack(frame) if frame is not None else [])
        self.stuck = stuck

    def check(self):
        engine = self.engine
        now, wall = engine.real_monotonic(), engine.real_time()
        late = now - self._looked
        drift = abs(wall - self._looked_wall - late)
        self._looked, self._looked_wall = now, wall
        if late > WATCHDOG_GAP or drift > WATCHDOG_GAP:
            self.since = now
            diagnostics.log("watchdog.gap", late=round(late), drift=round(drift))
        if engine._stopped:
            self._see_quit_through(now)
            return
        self._note_stuck_reads(now)
        op = self._mutation()
        last = engine._last_tick_real
        idle = now - max(self.since, engine._started_at if last is None else last)
        gone = engine.thread is not None and not engine.thread.is_alive()
        if idle >= (WATCHDOG_MUTATION if op else WATCHDOG_TICK) or (gone and not op):
            self._fire("tick", idle, op, wall)
            return
        held = self._reads_held(now)
        if held >= (WATCHDOG_MUTATION if op else WATCHDOG_READS):
            self._fire("reads", held, op, wall)
            return
        if self._exit_asked is not None and (not op or now - self._exit_asked[1] >= WATCHDOG_MUTATION):
            self._fire(self._exit_asked[0], 0.0, op, wall)
            return
        if self.status == "braked":
            self.status = "watching"
            diagnostics.log("watchdog.recovered")
        self._care(wall)

    def _fire(self, reason, stalled, op, wall):
        restarts = self._restarts(wall)
        why = {"at": wall, "reason": reason, "stalled_s": round(stalled), "op": op.kind if op is not None else None,
               "reads_stuck": self.stuck}
        if len(restarts) >= RESTARTS_PER_HOUR:
            self._brake(why, restarts, op)
            return
        self.status = "stopping"
        deadline = threading.Timer(EXIT_DEADLINE, self._leave)     # whatever hangs may block anything below
        deadline.daemon = True
        deadline.start()
        self.saved.update(restarts=[*restarts, wall], pending=why, last=why)
        self._save()
        self._report("watchdog.fired", why, op)
        self._forget_runtime()
        self._leave()

    def _brake(self, why, restarts, op):
        """Restarting has not helped: it stays up as it is and says so, once a day at most. It restarts again when
        the hour lets it; a tick completing again ends this."""
        if self.status == "braked":
            return
        self.status = "braked"
        told = self.saved.get("brake_alerted_at")
        tell = not isinstance(told, (int, float)) or why["at"] - told >= BRAKE_ALERT_EVERY
        self.saved.update(last=why, restarts=restarts, **({"brake_alerted_at": why["at"]} if tell else {}))
        self._save()
        self._report("watchdog.braked", why, op)
        if tell:      # on a thread of its own: this one must not wait for the store's lock, which the hang may hold
            threading.Thread(target=self._raise_alert, args=(alerts.restarts_braked(why["reason"], len(restarts),
                                                                                    self.engine.clock()),),
                             name="ad2-watchdog-alert", daemon=True).start()

    def _raise_alert(self, alert):
        try:
            self.engine.store.add_alert(alert)
            self.engine.bump()
        except Exception as exc:
            diagnostics.log_exception("watchdog.alert_failed", exc)

    def _report(self, event, why, op):
        """The state and every thread's stack, in the normal log: names, numbers and frames, no values."""
        engine = self.engine
        diagnostics.log(event, lvl="error", **why, ticks=engine._ticks, phase=op.phase if op is not None else None,
                        cancellable=op.cancellable if op is not None else None, failing=sorted(engine._failing),
                        threads=threading.active_count())
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        for ident, frame in sys._current_frames().items():
            diagnostics.log("watchdog.stack", lvl="error", thread=names.get(ident, "unknown"), frames=_stack(frame))

    def _forget_runtime(self):
        """runtime.json names a service that is going away (best effort: the next start writes it again)."""
        try:
            (Path(self.engine.paths.data_dir) / "runtime.json").unlink(missing_ok=True)
        except OSError:
            pass

    def _leave(self):
        try:
            if self.relaunch is not None:
                self.relaunch()
        finally:
            os._exit(WATCHDOG_EXIT)

    def _see_quit_through(self, now):
        """After stop(): the process ends once its threads do, unless a stuck one never does."""
        if self._quit_since is None:
            self._quit_since = now
        if now - self._quit_since >= (QUIT_WAIT_BUSY if self._mutation() else QUIT_WAIT):
            self._report("watchdog.quit_stuck", {"reason": "quit"}, None)
            self._forget_runtime()
            os._exit(0)

    # ---- the database's daily care ----
    def _care(self, wall):
        """Starts the daily check and backup when due, on a thread of its own: neither takes the store's lock, and
        in WAL mode no writer waits for them. A tick phase that met a damaged file brings the check forward."""
        if self._care_thread is not None and self._care_thread.is_alive():
            return
        engine = self.engine
        urgent = any(entry.get("error_type") == "DatabaseError" for entry in list(engine._failing.values()))
        backed_up = (engine.store.backed_up or {}).get("at") or 0.0
        if not urgent and 0 <= wall - backed_up < BACKUP_EVERY:      # one from the future: a clock that was set wrong
            return
        if wall - self._care_tried < (CARE_URGENT if urgent else CARE_RETRY):
            return
        self._care_tried = wall
        self._care_thread = threading.Thread(target=self._care_for_database, name="ad2-dbcare", daemon=True)
        self._care_thread.start()

    def _care_for_database(self):
        store = self.engine.store
        try:
            result = store.check_integrity()
            diagnostics.log("db.quick_check", lvl="info" if result == "ok" else "error", result=result,
                            ms=store.checked["ms"])
            if result == "ok":
                started = time.perf_counter()
                if store.backup():
                    diagnostics.log("db.backup", ms=round((time.perf_counter() - started) * 1000),
                                    bytes=store.backed_up["bytes"], kept=store.backups_kept)
                else:
                    result = "damaged"
            if result == "damaged":
                self._found_damage()
        except Exception as exc:
            diagnostics.log_exception("db.care_failed", exc, lvl="error")

    def _found_damage(self):
        """The running database is damaged. A start restores its newest backup, so with one the service restarts;
        without one a start would refuse to go on, so it keeps running on the file as it is, and says so."""
        engine, has_backup = self.engine, bool(self.engine.store.backups_kept)
        if has_backup:
            self.request_exit("db_damaged")
        if not self._damage_told:
            self._damage_told = True
            self._raise_alert(alerts.database_damaged(has_backup, engine.clock()))


def build_engine(paths, *, owner_lock):
    """The live engine run.py serves (PLAN §8.1)."""
    return Engine(paths, owner_lock=owner_lock)
