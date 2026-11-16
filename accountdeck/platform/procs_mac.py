"""The live process controller of this Mac (docs/mac/PLAN.md §1.2-1.4): part of procs, built on platform/libproc.

It holds AccountDeck's one signal call site (`_signal`) and starts the system tools it needs through procs'
self-guarded spawn: /usr/bin/open (apps by bundle id, a Terminal.app window for a command, a folder in
Finder) and /usr/sbin/lsof (the file-release probe, never in safe mode).

A row's `family` is the bundle id of the outermost `.app` it runs from (None outside any bundle, "?" when
unknown); its `ctime` is the start time in FILETIME units. A process macOS will not describe (another
user's) keeps start time 0 and family "?", so it can be listed but never acted on.

Signals run the interlock of procs.py's docstring: safe-mode ownership and consent, then step 3 behind a
kqueue NOTE_EXIT watch that pins the process (start time, executable path and bundle id are re-read, the
watch is drained) and only then the signal. SIGTERM is the graceful quit (Electron runs app.quit());
SIGKILL is only the straggler step, for a process that outlived a SIGTERM this controller sent to it or to
its parent. The microsecond window between the last check and kill(2) is the one weakness macOS has
against a handle-pinned kill: the pid would have to exit, be reaped and be recycled inside it, and macOS
hands out pids sequentially, so the safety rules accept it.
"""
from __future__ import annotations

import ctypes
from dataclasses import replace
import errno
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import time
import uuid

from .. import config, diagnostics
from ..core import DeckError
from ..updates import update_command
from ..procs import (
    AGY_ENV, CHANGED_MESSAGE, CMDLINE_NAMES, CODEX_APP_BUNDLE_IDS, DAEMON_EXIT_WAIT, DAEMON_START_WAIT,
    DAEMON_STOP_MARGIN, DEVNULL, INSPECTED_NAMES, KINDS, LAUNCH_FAILED_MESSAGE, PIPE, REMEMBERED_LIMIT,
    SAFE_MODE_MESSAGE, ConsentToken,
    ProcessController, ProcessInfo, _check_spawn_target, _family_attribution, _is_owned_pid, _is_registered,
    _is_test_bundle_id, _log, _refusal, filetime, is_agy, is_code_child_path, is_test_owned_path, process_name,
    with_kinds)
from . import facts_mac, libproc
from .libproc import BUNDLE_ID_PATTERN, OS_ERROR, bundle_family, valid_pid

OPEN_TOOL = "/usr/bin/open"
LSOF_TOOL = "/usr/sbin/lsof"
TERMINAL_BUNDLE_ID = "com.apple.Terminal"
OPEN_WAIT = 15.0              # LaunchServices normally answers within a second
LSOF_WAIT = 20.0
TERMINAL_PID_WAIT = 300.0     # a window that has not run its command by then is claimed (it never will)
TERMINAL_CLAIMED = "claimed"  # a pid file AccountDeck wrote itself: that window runs nothing
TERMINAL_RUN = re.compile(r"[0-9a-f]{32}\.pid")
SNAPSHOT_SLOW_MS = 50.0       # a snapshot slower than this is always logged
CLI_AUTH_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                      "CLAUDE_SECURESTORAGE_CONFIG_DIR")   # unset in the enrollment window
PACKAGE_SUFFIXES = (".app", ".bundle", ".framework", ".plugin", ".pkg", ".mpkg", ".prefpane", ".appex", ".xpc",
                    ".kext", ".saver")
NOT_ALLOWED_MESSAGE = "macOS did not allow AccountDeck to control this process."
# Test switches of get.sh and install.sh (another download folder, another install root, safe mode): an update the
# owner starts never inherits them from the shell's profile.
UPDATE_UNSET = ("ACCOUNTDECK_RELEASE_BASE", "ACCOUNTDECK2_INSTALL_ROOT", "ACCOUNTDECK_SAFE_MODE")
# Unset in agy's sign-in window: with an SSH variable agy keeps its sign-in in a file instead of the keychain item
# AccountDeck watches; the others sign it in another way (Application Default Credentials, a token or an API key)
# or move its folders.
AGY_SIGNIN_UNSET = ("SSH_TTY", "SSH_CONNECTION", "SSH_CLIENT", "AGY_ADC_AUTH", "JETSKI_OAUTH_TOKEN", "GEMINI_API_KEY",
                    "ANTIGRAVITY_APP_DATA_DIR", "ANTIGRAVITY_GEMINI_DIR")


def _resolved(path) -> str | None:
    """`path` with every link resolved, as the kernel names a running process's file; None for none."""
    if path is None:
        return None
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return None


def _check_signal(pid: int, sig: int) -> None:
    """Interlock step 1 at the syscall, plus what no caller may ever ask: only SIGTERM or SIGKILL, only a
    single real pid (never 0, a negative group or launchd), never this process."""
    if sig not in (signal.SIGTERM, signal.SIGKILL) or not valid_pid(pid) or pid == 1 or pid == os.getpid():
        raise _refusal("signal", "AccountDeck only sends a quit or stop signal to one other process.")
    if config.safe_mode():
        # A scripted stand-in for libSystem (libproc.use_api) describes no real process: never signal behind one.
        if type(libproc.api()) is not libproc.LibSystem:
            raise _refusal("signal", SAFE_MODE_MESSAGE)
        if not _is_owned_pid(pid, libproc.creation_time(pid)):
            raise _refusal("not_owned", SAFE_MODE_MESSAGE)


def _signal(pid: int, sig: int) -> None:
    """AccountDeck's one signal call site; it repeats interlock step 1 itself."""
    _check_signal(pid, sig)
    os.kill(pid, sig)


class MacProcessController(ProcessController):
    """The macOS implementation. In safe mode it exists only when asked for explicitly
    (`allow_readonly=True`); its snapshot then holds test-owned rows only, and every mutating method acts
    only on test-owned targets."""

    def __init__(self, paths, *, allow_readonly: bool = False, timings=None):
        if config.safe_mode() and not allow_readonly:
            raise _refusal("live_controller", "The live process controller is not available in safe mode.")
        super().__init__(paths, timings)
        self._signalled: dict[tuple[int, int], int] = {}   # (pid, ctime) -> the last signal sent to it
        self._logged_counts = None

    def _owns(self, pid: int, ctime: int) -> bool:
        return bool(ctime) and (self.owned.get(pid) == ctime or _is_owned_pid(pid, ctime))

    # ---- rows ----
    def _row(self, pid: int, path: str | None = None, *, parent: int | None = None) -> ProcessInfo | None:
        """One row; None when the process has exited. A process macOS will not describe keeps start time 0
        and family "?" (unknown identity)."""
        lib = libproc.api()
        raw, error = lib.bsdinfo(pid)
        info = libproc.parse_bsdinfo(raw) if raw is not None else None
        if info is None:
            if error == errno.ESRCH:
                return None
            info = libproc.parse_shortinfo(lib.shortinfo(pid))
        if info is None or info.pid != pid or not info.alive:
            return None
        path = path if path is not None else lib.pidpath(pid)
        name = process_name(path, info.name)
        if not name:
            return None
        ctime = info.ctime
        command = None
        if ctime and name.lower() in CMDLINE_NAMES:
            argv = libproc.parse_procargs2(lib.procargs(pid))
            command = shlex.join(argv) if argv is not None else None
        if ctime:
            # Everything read above must belong to the process the start time names: a pid reused meanwhile
            # leaves an unknown row (start time 0, family "?"), which nothing acts on.
            again = libproc.parse_bsdinfo(lib.bsdinfo(pid)[0] or b"")
            if again is None or again.ctime != ctime:
                return None if again is None else ProcessInfo(pid, again.ppid, name, None, 0, "?")
        family = bundle_family(path) if path and ctime else "?"
        return ProcessInfo(pid, info.ppid if parent is None else parent, name, path, ctime, family, command)

    def _relevant(self, name: str, path: str | None) -> bool:
        """Whether a process is one of Claude's, Codex's or agy's: by name, by the app bundle it runs from, or (agy)
        by being the file the agy AccountDeck found resolves to (`agy_path`, procs.is_agy)."""
        if name.lower() in INSPECTED_NAMES or is_agy(name, path, self.agy_path):
            return True
        if not path or ".app/" not in path.lower():
            return False
        family = bundle_family(path)
        return family not in (None, "?") and (family == self.paths.claude_family or family in self.paths.codex_families
                                              or family in CODEX_APP_BUNDLE_IDS)

    def snapshot(self) -> list[ProcessInfo]:
        started = time.perf_counter()
        synthetic = self.paths.mode != "live"
        self.agy_path = _resolved(facts_mac.agy_exe(self.paths))     # file checks only, as sensing does
        lib = libproc.api()
        buffer = ctypes.create_string_buffer(libproc.PATH_MAX)
        rows = []
        for pid in lib.listpids():
            path = lib.pidpath(pid, buffer)
            if path is None:
                short = libproc.parse_shortinfo(lib.shortinfo(pid))
                name = short.name if short is not None and short.alive else ""
            else:
                name = process_name(path)
            if not name:
                continue
            inspected = self._relevant(name, path)
            if not (inspected or (synthetic and (pid in self.owned or _is_registered(pid)))):
                continue
            row = self._row(pid, path)
            if row is None:
                continue
            if synthetic:
                # Keep the real machine out of synthetic runs: only rows a test owns or placed under a test root.
                if not (self._owns(pid, row.ctime) or (inspected and is_test_owned_path(row.path))):
                    continue
                family = _family_attribution(pid, row.ctime)
                if family:
                    row = replace(row, family=family)
            rows.append(row)
        rows = with_kinds(rows, self.paths, agy_path=self.agy_path)
        self._log_snapshot(rows, (time.perf_counter() - started) * 1000)
        return rows

    def _log_snapshot(self, rows, ms: float) -> None:
        """proc.snapshot at info level when what it found changed (or it was slow), else at debug level: the
        engine takes one every few seconds."""
        counts = {kind: sum(row.kind == kind for row in rows) for kind in KINDS}
        summary = (counts["claude_desktop"], counts["claude_code"], counts["claude_foreign"],
                   sum(counts[kind] for kind in KINDS if kind.startswith("codex")), counts["agy"])
        level = "debug" if summary == self._logged_counts and ms < SNAPSHOT_SLOW_MS else "info"
        self._logged_counts = summary
        diagnostics.log("proc.snapshot", level, desktop=summary[0], code=summary[1], foreign=summary[2],
                        codex=summary[3], agy=summary[4], ms=round(ms, 1))

    def creation_time(self, pid: int) -> int | None:
        return libproc.creation_time(pid)

    def descendants(self, pid: int, exclude=()) -> list[ProcessInfo]:
        root_ctime = libproc.creation_time(pid)
        if root_ctime is None:
            return []
        children = libproc.children_by_parent()
        skipped = {name.lower() for name in exclude}
        found, queue = [], [(pid, root_ctime)]
        while queue and len(found) < 512:
            parent, parent_ctime = queue.pop()
            for child in children.get(parent, ()):
                row = self._row(child, parent=parent)
                if row is not None and row.name.lower() not in skipped and parent_ctime < row.ctime:
                    found.append(row)        # an older "child" is pid reuse
                    queue.append((child, row.ctime))
        return found

    def ancestors_of_self(self) -> list[ProcessInfo]:
        chain = []
        current, current_ctime = os.getpid(), libproc.creation_time(os.getpid())
        for _ in range(64):
            parent = libproc.parent_of(current)
            row = self._row(parent) if parent else None
            if row is None or current_ctime is None or not 0 < row.ctime < current_ctime:
                break
            chain.append(row)
            current, current_ctime = parent, row.ctime
        return chain

    def file_is_free(self, path) -> bool:
        """True only when nothing holds `path` open: a missing file is free; anything but a regular file is
        not. The libproc scan (the descriptors lsof reads) must find no holder, and outside safe mode
        `lsof -t` must also run cleanly and list none. Doubt is always "held"."""
        try:
            status = os.stat(path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if not stat.S_ISREG(status.st_mode):
            return False
        holders = libproc.file_holders(path)
        if holders is None or holders:
            return False
        if config.safe_mode():
            return True    # safe mode starts no system tool (procs' spawn policy); the scan above is the probe
        return self._lsof_free(path)

    def _lsof_free(self, path) -> bool:
        try:
            process = self.spawn([LSOF_TOOL, "-t", "--", os.fspath(path)], stdin=DEVNULL, stdout=PIPE, stderr=PIPE)
        except (DeckError, OSError):
            return False
        try:
            output, problems = process.communicate(timeout=LSOF_WAIT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return False
        # lsof exits 1 with no output when no process has the file open; 0 lists the holders' pids.
        return process.returncode == 1 and not (output or b"").strip() and not (problems or b"").strip()

    def _helper_tree(self) -> set[tuple[int, int]]:
        tree = {(pid, ctime) for pid, ctime in self.helpers.items() if ctime and libproc.creation_time(pid) == ctime}
        if not tree:
            return tree
        children = libproc.children_by_parent()
        queue = list(tree)
        while queue:
            parent, parent_ctime = queue.pop()
            for pid in children.get(parent, ()):
                ctime = libproc.creation_time(pid)
                if ctime and ctime > parent_ctime and (pid, ctime) not in tree:
                    tree.add((pid, ctime))
                    queue.append((pid, ctime))
        return tree

    def _open_paths(self, row: ProcessInfo) -> list[str] | None:
        try:
            paths = libproc.open_paths(row.pid)
            return paths if libproc.creation_time(row.pid) == row.ctime else None      # the pid is another process now
        except (DeckError, OSError):
            return None

    # ---- interlocked ----
    def _require_test_owned(self, action: str, target: ProcessInfo) -> None:
        if config.safe_mode() and not self._owns(target.pid, target.ctime):
            raise _refusal("not_owned", f"{SAFE_MODE_MESSAGE} ({action})")

    def _require_test_path(self, action: str, path) -> None:
        if config.safe_mode() and not is_test_owned_path(path):
            raise _refusal("not_test_path", f"{SAFE_MODE_MESSAGE} ({action})")

    def _require_bundle_id(self, bundle_id) -> str:
        if not isinstance(bundle_id, str) or len(bundle_id) > 255 or not BUNDLE_ID_PATTERN.fullmatch(bundle_id):
            raise DeckError("That is not a valid app ID.", "invalid")
        if config.safe_mode() and not _is_test_bundle_id(bundle_id):
            raise _refusal("bundle_id", SAFE_MODE_MESSAGE)
        return bundle_id

    def _verified(self, target: ProcessInfo, watch: libproc.ExitWatch) -> bool:
        """Interlock step 3 behind the kqueue pin: False when the target already exited; refuses one whose
        start time, executable or bundle id changed (the pid now names another process) or cannot be read."""
        if not valid_pid(target.pid) or not watch.watch(target.pid):
            return False
        state, ctime = libproc.start_time(target.pid)
        if state == "gone":
            return False
        if state == "unreadable":
            raise DeckError(NOT_ALLOWED_MESSAGE, OS_ERROR)
        path = libproc.api().pidpath(target.pid)
        family = bundle_family(path) if path else "?"
        if config.safe_mode():
            family = _family_attribution(target.pid, ctime) or family
        if not target.ctime or ctime != target.ctime:
            raise _refusal("ctime_mismatch", CHANGED_MESSAGE)
        if not target.path or not path or path != target.path:
            raise _refusal("path_mismatch", CHANGED_MESSAGE)
        if family == "?" or family != target.family:
            raise _refusal("family_mismatch", CHANGED_MESSAGE)
        return target.pid not in watch.drain(0)     # it exited while being checked: nothing to send

    def _signal_for(self, target: ProcessInfo, graceful: bool) -> int:
        """SIGTERM, except the straggler step: SIGKILL for a process that outlived a SIGTERM this controller
        sent to it, or to the process it was started under (its parent was stopped and it stayed)."""
        if graceful:
            return signal.SIGTERM
        if (target.pid, target.ctime) in self._signalled:
            return signal.SIGKILL
        parent = next((ctime for (pid, ctime) in self._signalled if pid == target.parent), None)
        return signal.SIGKILL if parent is not None and parent < target.ctime else signal.SIGTERM

    def _send(self, action: str, token, target: ProcessInfo, *, graceful: bool = False) -> bool:
        self._interlock(action, token, target)                         # steps 1 and 2
        if action == "request_close":
            main = self.claude_desktop_main()
            if main is None or (main.pid, main.ctime) != (target.pid, target.ctime):
                raise _refusal("not_main", CHANGED_MESSAGE)
        with libproc.api().exit_watch() as watch:
            if not self._verified(target, watch):                       # step 3
                return False
            sig = self._signal_for(target, graceful)
            try:
                _signal(target.pid, sig)                                # step 4
            except ProcessLookupError:
                return False
            except PermissionError:
                raise DeckError(NOT_ALLOWED_MESSAGE, OS_ERROR) from None
        self._signalled[(target.pid, target.ctime)] = sig
        while len(self._signalled) > REMEMBERED_LIMIT:
            del self._signalled[next(iter(self._signalled))]
        _log("proc.signal", pid=target.pid, kind=target.kind, action=action,
             signal="SIGKILL" if sig == signal.SIGKILL else "SIGTERM", verified=True)
        return True

    def request_close(self, token: ConsentToken, target: ProcessInfo) -> None:
        """T1a (PLAN §1.3): SIGTERM to the verified Claude Desktop main, which runs Electron's app.quit()."""
        self._send("request_close", token, target, graceful=True)

    def terminate(self, token: ConsentToken, target: ProcessInfo) -> bool:
        return self._send("terminate", token, target)

    def stop_consumer(self, token: ConsentToken, target: ProcessInfo) -> bool:
        return self._send("stop_consumer", token, target)

    def wait_exit(self, targets, timeout: float) -> list[ProcessInfo]:
        """Waits on kqueue NOTE_EXIT; returns the targets still running (unreadable ones count as running)."""
        deadline = time.monotonic() + max(0.0, timeout)
        running, unreadable = set(), set()
        with libproc.api().exit_watch() as watch:
            for target in targets:
                if not valid_pid(target.pid):
                    continue
                try:
                    if not watch.watch(target.pid):
                        continue
                except OSError:
                    unreadable.add((target.pid, target.ctime))
                    continue
                state, ctime = libproc.start_time(target.pid)
                if state == "unreadable" or (state == "alive" and ctime == target.ctime):
                    running.add((target.pid, target.ctime))
            while running:
                exited = watch.drain(max(0.0, deadline - time.monotonic()))
                running = {pair for pair in running if pair[0] not in exited}
                if time.monotonic() >= deadline:
                    break
        return [target for target in targets if (target.pid, target.ctime) in running | unreadable]

    # ---- launch ----
    def _open_app(self, bundle_id: str, *, background: bool) -> None:
        argv = [OPEN_TOOL] + (["-g"] if background else []) + ["-b", bundle_id]
        process = self.spawn(argv, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL)
        try:
            code = process.wait(timeout=OPEN_WAIT)
        except subprocess.TimeoutExpired:
            code = None   # LaunchServices is still handing the request over; the verifier watches for the app
        self.last_launch = {"method": "open", "hresult": None, "retries": 0, "exit": code}
        _log("proc.launch", method="open", exit=code)
        if code not in (0, None):
            raise DeckError(LAUNCH_FAILED_MESSAGE, OS_ERROR)

    def activate_app(self, bundle_id: str) -> int | None:
        """Opens (or brings forward) the app with this bundle id: `open -b`. The pid is not known (None)."""
        self._open_app(self._require_bundle_id(bundle_id), background=False)
        return None

    def launch_claude(self, bundle_id: str | None) -> int | None:
        """Starts Claude Desktop by bundle id in the background: `open -g -b` (PLAN §1.4). The new app's
        parent is launchd, so its pid is not known here (None); the verifier watches for it."""
        self._open_app(self._require_bundle_id(bundle_id or self.paths.claude_family), background=True)
        return None

    def open_terminal_command(self, argv, *, cwd, env=None, unset=(), require=()) -> dict:
        """Runs `argv` in a new Terminal.app window (enrollment uses it through launch_claude_cli) and
        returns its record {"pid", "ctime", "pidfile", "opened"}; the pid and start time are filled in by
        `cli_enrollment_exited` once the window has started it.

        The window runs a one-shot launcher script: it deletes itself first, stops if a `require` folder is
        gone, changes to `cwd`, unsets `unset`, exports `env`, writes its pid and then execs `argv` (so the
        pid is the command's). The environment values are written only into that script (mode 0700, in the
        private data folder); `open`'s argv holds the script path alone, so no value appears in any process
        list. Its arguments are the caller's: never put a secret there."""
        argv = [os.fspath(arg) for arg in argv]
        if not argv or not os.path.isabs(argv[0]) or not os.path.isfile(argv[0]):
            raise DeckError("There is nothing to start.", "invalid")
        return self._terminal(argv, cwd=cwd, env=env, unset=unset, require=require)

    def _terminal(self, argv, *, cwd, env=None, unset=(), require=()) -> dict:
        folder = Path(cwd)
        if not folder.is_dir():
            raise DeckError("That folder no longer exists.", "not_found")
        exports = {str(name): str(value) for name, value in (env or {}).items()}
        for variable in list(exports) + [str(item) for item in unset]:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", variable):
                raise DeckError("A terminal setting has an invalid name.", "invalid")
        if any("\0" in value for value in exports.values()) or any("\0" in arg for arg in argv):
            raise DeckError("A terminal setting has an invalid value.", "invalid")
        _check_spawn_target(OPEN_TOOL)    # safe mode refuses here, before anything is written
        runs = Path(self.paths.data_dir) / "terminal"
        runs.mkdir(mode=0o700, parents=True, exist_ok=True)
        runs.chmod(0o700)
        run = uuid.uuid4().hex
        script, pidfile = runs / f"{run}.command", runs / f"{run}.pid"
        # The pid file is created with noclobber (O_EXCL): whoever creates it first owns the run. The window
        # writes its pid there before anything else runs; AccountDeck writes CLAIMED there when it gives up on
        # a window that never ran (`_resolve_terminal`), and a window opened after that runs nothing.
        lines = ["#!/bin/sh", "# AccountDeck opened this window. This script removes itself before it runs anything.",
                 f"rm -f -- {shlex.quote(str(script))}",
                 f"set -C; printf '%s' \"$$\" > {shlex.quote(str(pidfile))} || exit 1; set +C"]
        lines += [f"[ -d {shlex.quote(os.fspath(item))} ] || exit 1" for item in require]
        lines.append(f"cd -- {shlex.quote(str(folder))} || exit 1")
        lines += [f"unset {variable}" for variable in sorted({str(item) for item in unset} - set(exports))]
        lines += [f"export {variable}={shlex.quote(value)}" for variable, value in sorted(exports.items())]
        lines.append("exec " + (shlex.join(argv) if argv else '"${SHELL:-/bin/zsh}" -l'))
        descriptor = os.open(script, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        try:
            process = self.spawn([OPEN_TOOL, "-b", TERMINAL_BUNDLE_ID, str(script)], stdin=DEVNULL, stdout=DEVNULL,
                                 stderr=DEVNULL)
            code = process.wait(timeout=OPEN_WAIT)
        except subprocess.TimeoutExpired:
            code = 0      # still handing over: the window reports its pid when it runs
        except BaseException:
            script.unlink(missing_ok=True)
            raise
        if code != 0:
            script.unlink(missing_ok=True)
            raise DeckError("The terminal could not be opened.", OS_ERROR)
        _log("proc.terminal", exit=code)
        return {"pid": None, "ctime": None, "pidfile": str(pidfile), "opened": time.time()}

    def _terminal_pidfile(self, record: dict) -> Path:
        pidfile = Path(str(record.get("pidfile") or ""))
        if pidfile.parent != Path(self.paths.data_dir) / "terminal" or not TERMINAL_RUN.fullmatch(pidfile.name):
            raise DeckError("The terminal record is not AccountDeck's.", "safety")
        return pidfile

    def _resolve_terminal(self, record: dict) -> str:
        """The state of a terminal window's command, from its pid file: "running" (its pid and start time are
        then filled into `record`), "exited", or "unknown" (not run yet, or not readable).

        A window that has not written its pid within TERMINAL_PID_WAIT is claimed: AccountDeck creates the pid
        file itself (O_EXCL, CLAIMED), so that window can never run its command, and only then answers
        "exited". If the window wins that race, it has just started: "unknown" until its pid is readable."""
        pidfile = self._terminal_pidfile(record)
        try:
            status = pidfile.stat()
            text = pidfile.read_text(encoding="ascii")[:32].strip()
        except FileNotFoundError:
            opened = record.get("opened")
            if isinstance(opened, (int, float)) and 0 <= time.time() - opened < TERMINAL_PID_WAIT:
                return "unknown"
            try:
                descriptor = os.open(pidfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except OSError:
                return "unknown"                  # FileExistsError: the window just wrote its pid
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(TERMINAL_CLAIMED)
            pidfile.with_suffix(".command").unlink(missing_ok=True)
            _log("proc.terminal_claimed")
            return "exited"
        except (OSError, ValueError):
            return "unknown"
        if text == TERMINAL_CLAIMED:
            return "exited"
        if not text.isdigit() or not valid_pid(int(text)):
            return "unknown"                      # being written, or not a pid: never a guess
        pid = int(text)
        state, ctime = libproc.start_time(pid)
        if state == "unreadable":
            return "unknown"
        # The window's shell wrote this file, so a process that started after it was written is not that shell
        # (its pid was reused after the command ended).
        if state == "gone" or ctime > filetime(status.st_mtime + 1):
            pidfile.unlink(missing_ok=True)
            return "exited"
        record.update(pid=pid, ctime=ctime)
        return "running"

    def cli_enrollment_exited(self, process):
        """Whether the enrollment terminal's command has ended: a Popen, or the record of a Terminal.app
        window (also the saved one after an AccountDeck restart, which is re-read from its pid file). When
        it cannot tell, the answer is False ("not yet"), so the enrollment folder is kept."""
        if not isinstance(process, dict):
            return process.poll() is not None
        if process.get("pidfile") and not process.get("ctime"):
            state = self._resolve_terminal(process)
            if state != "running":
                return state == "exited"
        pid, ctime = process.get("pid"), process.get("ctime")
        if not ctime or not valid_pid(pid):
            return False
        if config.safe_mode() and not self._owns(pid, ctime):
            raise _refusal("not_owned", "Only a test-owned enrollment process can be inspected in safe mode.")
        state, now = libproc.start_time(pid)
        if state == "unreadable":
            return False
        exited = state == "gone" or now != ctime
        if exited and process.get("pidfile"):
            self._terminal_pidfile(process).unlink(missing_ok=True)
        return exited

    def cli_enrollment_token(self, process):
        """What survives a restart: for a terminal window its pid file and opening time (the pid is re-read)."""
        if isinstance(process, dict):
            return {key: process.get(key) for key in ("pid", "ctime", "pidfile", "opened")}
        return {"pid": process.pid, "ctime": self.owned.get(process.pid)}

    def launch_claude_cli(self, claude_exe, config_dir, cwd):
        """Opens Claude Code for enrollment in a new Terminal.app window, signed in to `config_dir` only:
        CLAUDE_CONFIG_DIR is exactly `str(config_dir)` (Claude Code names its Keychain item after that text),
        and the API-key, token and secure-storage variables are unset there. If the enrollment folder is gone
        by the time the window runs (AccountDeck gave up on it), the window starts nothing."""
        for path in (claude_exe, config_dir, cwd):
            self._require_test_path("launch_claude_cli", path)
        return self.open_terminal_command([claude_exe], cwd=cwd, env={"CLAUDE_CONFIG_DIR": str(config_dir)},
                                          unset=CLI_AUTH_VARIABLES, require=(config_dir,))

    def find_claude_cli(self):
        """Claude Code on PATH, else `~/.local/bin/claude` (the native installer's place, which a
        LaunchAgent's PATH lacks). Claude Desktop's own copy never counts."""
        found = super().find_claude_cli()
        if found is not None:
            return found
        candidate = Path(self.paths.home) / ".local" / "bin" / "claude"
        if not candidate.is_file() or is_code_child_path(candidate):
            return None
        if config.safe_mode() and not is_test_owned_path(candidate):
            raise _refusal("not_test_path", "Only a synthetic Claude Code executable may be used in safe mode.")
        return candidate

    def launch_codex(self, codex_exe, home, cwd, session=None) -> None:
        """Opens Codex in a new Terminal.app window (a real user of the login, not a helper)."""
        self._require_test_path("launch_codex", codex_exe)
        folder = Path(cwd)
        if not folder.is_dir():
            raise DeckError("That project folder no longer exists.", "not_found")
        args = [str(codex_exe), "--no-daemon"]
        if session:
            try:
                uuid.UUID(str(session))
            except ValueError:
                raise DeckError("Invalid conversation identifier.", "invalid") from None
            args += ["resume", str(session)]
        self.open_terminal_command(args, cwd=folder, env={"CODEX_HOME": str(home)})

    def open_agy_signin(self, agy_exe, *, cwd) -> dict:
        """Add (and Sign In Again) for Antigravity: agy in a new Terminal.app window, in `cwd`, where it shows its
        sign-in screen because AccountDeck has just removed its keychain item; the user signs in with Google in the
        browser, and AccountDeck watches the item for the new sign-in. Nothing is typed into the window. agy must
        not update itself there (AGY_ENV), and the variables that would make it keep its sign-in in a file or sign
        in another way are unset (AGY_SIGNIN_UNSET). Returns the window's record (open_terminal_command), which
        `cli_enrollment_exited` reads to tell when that agy has ended. Refused in safe mode unless agy and `cwd`
        are a test's own; opening the window itself (`open`) is refused there as always."""
        for path in (agy_exe, cwd):
            self._require_test_path("open_agy_signin", path)
        record = self.open_terminal_command([agy_exe], cwd=cwd, env=dict(AGY_ENV), unset=AGY_SIGNIN_UNSET)
        _log("proc.agy_signin_terminal")
        return record

    def open_update_terminal(self, script, version) -> dict:
        """start-update (accountdeck/updates.py): the installed get.sh for `version` in a new Terminal.app window, where
        the user sees the installer's progress and its result. The command is updates.update_command's alone (which
        checks the version again), in the home folder, with the installers' test switches unset. Never in safe mode.
        Returns the window's record (open_terminal_command), which `cli_enrollment_exited` reads to tell when the
        update's command has ended: until then the service offers no second update."""
        if config.safe_mode():
            raise _refusal("update", SAFE_MODE_MESSAGE)
        argv = update_command(script, version)
        if not Path(argv[1]).is_file():
            raise DeckError("AccountDeck's updater (get.sh) is missing. Install AccountDeck again.", "not_installed")
        record = self.open_terminal_command(argv, cwd=self.paths.home, unset=UPDATE_UNSET)
        _log("proc.update_terminal", version=version)
        return record

    def codex_daemon_stop(self, codex_exe, home, grace_s: float) -> str:
        """`codex app-server daemon stop`: "absent" (none ran), "stopped" or "timeout"."""
        self._require_test_path("codex_daemon_stop", codex_exe)
        if self.codex_daemon() is None:
            return "absent"
        process = self.spawn_helper([str(codex_exe), "app-server", "daemon", "stop"], env={"CODEX_HOME": str(home)},
                                    stdout=DEVNULL, stderr=DEVNULL)
        try:
            process.wait(timeout=max(0.0, grace_s) + DAEMON_STOP_MARGIN)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return "timeout"
        return "stopped" if self.wait_until(lambda: self.codex_daemon() is None, DAEMON_EXIT_WAIT) else "timeout"

    def codex_daemon_start(self, codex_exe, home) -> bool:
        self._require_test_path("codex_daemon_start", codex_exe)
        process = self.spawn_helper([str(codex_exe), "app-server", "daemon", "start"], env={"CODEX_HOME": str(home)},
                                    stdout=DEVNULL, stderr=DEVNULL)
        try:
            return process.wait(timeout=DAEMON_START_WAIT) == 0
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return False

    def open_folder(self, path) -> None:
        """Shows a folder in Finder. A bundle (Name.app and the like) would be launched, not shown: refused."""
        self._require_test_path("open_folder", path)
        folder = Path(path)
        if not folder.is_dir():
            raise DeckError("That folder no longer exists.", "not_found")
        resolved = os.path.realpath(folder)
        if not resolved.startswith("/") or any(part.lower().endswith(PACKAGE_SUFFIXES) for part in resolved.split("/")):
            raise DeckError("That is not a folder AccountDeck opens.", "invalid")
        self.spawn([OPEN_TOOL, resolved], stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL)

    def open_terminal(self, cwd, env) -> None:
        """A new Terminal.app window with the login shell in `cwd`, with `env` exported there."""
        self._require_test_path("open_terminal", cwd)
        self._terminal([], cwd=cwd, env=env)
