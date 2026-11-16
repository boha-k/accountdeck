"""Quitting Claude Desktop for a job (PLAN §4.6): exactly one stop attempt, every signal within consent.

T0   gate, no signals: re-snapshot; refuse unknown rows, 0 or > 40 desktop rows and self-stops; ask
     again ("reconfirm") when processes appeared that the user did not confirm; in "idle" mode wait
     until every Code session has been idle for `idle_hold` seconds, at most until the consent expires
     ("expired"). The job leaves its wait (phase `stopping`) before the last cancel check that comes
     ahead of the first signal, so a preempt (operations.OperationRunner.preempt, allowed only while
     the job waits) can never be followed by a signal.
T1a  graceful (docs/mac/PLAN.md §1.3): SIGTERM to the verified Electron main, which runs app.quit(); if
     Claude is still running `terminate_wait` s after its readyForQuit marker (or after `graceful_wait` s),
     the same job continues with T2.
T2   managed: terminate the main process, give the helpers `helper_self_exit` s to exit on their own,
     then terminate each straggler; anything still alive `terminate_wait` s later aborts the job.
T3   manual: without consent the user is asked to quit Claude from the menu bar.
Settle: the desktop set is gone and Claude's sign-in files are released (`facts_mac.files_released`: macOS
     has no mandatory locks, so the controller scans for open descriptors), then `settle_grace`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import time

from .config import SafetyViolation
from .core import DeckError
from .platform import facts_mac
from .procs import MAX_DESKTOP_ROWS, READY_FOR_QUIT, ProcessInfo, ProcessSet, desktop_main

MANUAL_MESSAGE = "Quit Claude from the menu bar, then try again."
START_HINT = "Open it from Applications."
UNKNOWN_MESSAGE = ("macOS would not say what some Claude processes are, so AccountDeck left Claude alone. "
                   + MANUAL_MESSAGE)
TOO_MANY_MESSAGE = ("Claude is running more processes than AccountDeck expects, so it left Claude alone. "
                    + MANUAL_MESSAGE)
SELF_PROTECT_MESSAGE = ("AccountDeck 2 was started from Claude's terminal, so it cannot stop Claude. "
                        + START_HINT)
RECONFIRM_MESSAGE = "Claude's processes changed since you confirmed. Please confirm again."
CANCELLED_MESSAGE = "Cancelled. Nothing was changed."
EXPIRED_MESSAGE = "Claude was still busy when this confirmation expired. Nothing was changed; try again when it is idle."
NOT_RUNNING_MESSAGE = "Claude is not running."
NOT_STOPPED_MESSAGE = "Claude did not stop. Nothing was changed."
NOT_RELEASED_MESSAGE = "Claude is still holding its sign-in files. Nothing was changed."
CLOSE_DETAIL = "Asked Claude to close…"
CLEANUP_DETAIL = "Claude is finishing its cleanup…"
MANAGED_DETAIL = "Managed stop · main process"
HELPERS_DETAIL = "Waiting for helpers"
SETTLE_DETAIL = "Waiting for Claude to let go of its files"
MARKER_TEXTS = ("Starting onQuitCleanup", READY_FOR_QUIT, "[quit-cleanup] previous quit")
LOG_PREFIX = re.compile(r"^\[[^\]]*\]\s*(?:\[[a-z]+\]\s*)?")
MAX_LOG_READ = 1 << 20
MAX_MARKERS = 20
POLL = 0.25


@dataclass
class StopResult:
    ok: bool
    tier: str   # "not_running" | "graceful" | "managed" | "manual" | "aborted" | "cancelled" | "reconfirm" | "expired"
    elapsed: float = 0.0       # seconds from the first signal to the end (0 when nothing was sent)
    survivors: list[ProcessInfo] = field(default_factory=list)
    helpers_self_exited: bool | None = None
    stragglers: int = 0
    detail: str = ""
    markers: list[str] = field(default_factory=list)
    settled: bool | None = None   # None when the ladder did not reach the settle step


class StopLadder:
    """One stop attempt per job. `progress(step_id, detail)` receives "wait", "stop" and "settle";
    while waiting for idle sessions `waiting_on` lists them as {pid, name, status}."""

    def __init__(self, paths, procs, activity, timings, diagnostics):
        self.paths, self.procs, self.activity = paths, procs, activity
        self.timings, self.diagnostics = timings, diagnostics
        self.waiting_on: list[dict] = []
        self._first_signal: float | None = None

    def run(self, token, plan_set: ProcessSet, *, cancel, progress) -> StopResult:
        self.waiting_on, self._first_signal = [], None
        log_offset = self._log_size()
        gate = self._gate(token, plan_set, cancel, progress)
        result = gate if isinstance(gate, StopResult) else self._stop(token, *gate, cancel, progress)
        result.elapsed = 0.0 if self._first_signal is None else round(time.monotonic() - self._first_signal, 3)
        result.markers = self._markers(log_offset)
        self.diagnostics.log("stop.result", tier=result.tier, ok=result.ok, ms=int(result.elapsed * 1000),
                             self_exit=result.helpers_self_exited, stragglers=result.stragglers,
                             settled=result.settled)
        return result

    # ---- T0 ----
    def _gate(self, token, plan_set, cancel, progress):
        """(desktop rows, Code rows) to stop, or the StopResult that ends the job without a signal."""
        if token is None:
            return StopResult(False, "manual", detail=MANUAL_MESSAGE)
        idle_state: dict = {}
        confirmed = set(plan_set.pairs)
        while True:
            kinds = self.procs.classify(self.procs.snapshot())
            desktop, code = kinds["claude_desktop"], kinds["claude_code"]
            if kinds["claude_unknown"]:
                return StopResult(False, "aborted", detail=UNKNOWN_MESSAGE)
            if not desktop:
                return StopResult(True, "not_running", detail=NOT_RUNNING_MESSAGE)
            if len(desktop) > MAX_DESKTOP_ROWS:
                return StopResult(False, "aborted", detail=TOO_MANY_MESSAGE)
            if {row.pid for row in self.procs.ancestors_of_self()} & {row.pid for row in desktop + code}:
                return StopResult(False, "aborted", detail=SELF_PROTECT_MESSAGE)
            if set(ProcessSet.of(desktop + code).pairs) - confirmed:
                return StopResult(False, "reconfirm", detail=RECONFIRM_MESSAGE)
            if token.mode != "idle":
                break
            if time.time() >= token.expires:
                # The consent covers the wait: Claude is never closed on a confirmation given long ago.
                return StopResult(False, "expired", detail=EXPIRED_MESSAGE)
            report = self.activity.report(self.paths, self.procs, now=time.time(),
                                          idle_hold_s=self.timings.idle_hold, state=idle_state)
            if report.all_idle:
                break
            self.waiting_on = [{"pid": s.pid, "name": s.name, "status": s.status} for s in report.blockers]
            progress("wait", _waiting_detail(report.blockers))
            if cancel.wait(self.timings.idle_poll):
                return StopResult(False, "cancelled", detail=CANCELLED_MESSAGE)
        self.waiting_on = []
        if cancel.is_set():
            return StopResult(False, "cancelled", detail=CANCELLED_MESSAGE)
        return desktop, code

    # ---- T1a / T2 ----
    def _stop(self, token, desktop, code, cancel, progress) -> StopResult:
        main = desktop_main(desktop)
        tier = "managed"
        progress("stop", "")         # out of the wait before the cancel check (module docstring, T0)
        if cancel.is_set():
            return StopResult(False, "cancelled", detail=CANCELLED_MESSAGE)
        if graceful_first(self.paths, self.procs, main):
            progress("stop", CLOSE_DETAIL)
            try:
                self.procs.request_close(token, main)
            except SafetyViolation as refused:
                return StopResult(False, "aborted", detail=str(refused))
            except DeckError:
                pass   # macOS refused the quit signal: the managed stop follows in this job
            else:
                self._signal()
                if self._graceful_exit(desktop, progress):
                    tier = "graceful"
        if tier == "graceful":
            outcome = self._stop_stragglers(token, [], code, progress, helpers_self_exited=None)
        else:
            if self._first_signal is None and cancel.is_set():
                return StopResult(False, "cancelled", detail=CANCELLED_MESSAGE)
            outcome = self._managed(token, main, desktop, code, progress)
        if not outcome.ok:
            return outcome
        outcome.tier = tier
        outcome.settled = self._settle(progress)
        if not outcome.settled:
            outcome.ok, outcome.detail = False, NOT_RELEASED_MESSAGE
        return outcome

    def _graceful_exit(self, desktop, progress) -> bool:
        """Waits for Claude's own quit; False when T2 has to finish the job."""
        offset = self._log_size()
        started = time.monotonic()
        deadline, marker_deadline, told = started + self.timings.graceful_wait, None, False
        while True:
            limit = deadline if marker_deadline is None else min(deadline, marker_deadline)
            if not self.procs.wait_exit(desktop, max(0.0, min(POLL, limit - time.monotonic()))):
                return True
            now = time.monotonic()
            if not told and now - started >= self.timings.graceful_progress:
                progress("stop", CLEANUP_DETAIL)
                told = True
            if marker_deadline is None and READY_FOR_QUIT in self._log_text(offset):
                marker_deadline = now + self.timings.terminate_wait
            if now >= limit:
                return False

    def _managed(self, token, main, desktop, code, progress) -> StopResult:
        progress("stop", MANAGED_DETAIL)
        try:
            sent = self.procs.terminate(token, main)
        except SafetyViolation as refused:
            return StopResult(False, "aborted", detail=str(refused))
        except DeckError:
            return StopResult(False, "aborted", detail=NOT_STOPPED_MESSAGE)
        if sent:
            self._signal()
        others = [row for row in desktop + code if row.pid != main.pid]
        survivors = self.procs.wait_exit(others, self.timings.helper_self_exit)
        helpers_left = [row for row in survivors if row.kind == "claude_desktop"]
        return self._stop_stragglers(token, [main] + helpers_left,
                                     [row for row in survivors if row.kind != "claude_desktop"], progress,
                                     helpers_self_exited=not helpers_left)

    def _stop_stragglers(self, token, desktop_left, code, progress, *, helpers_self_exited) -> StopResult:
        """Terminates each remaining desktop row and each surviving Code child the consent covers
        (one by one, each re-verified by the controller); any of them still alive aborts the job."""
        main = desktop_left[:1]
        stragglers = list(desktop_left[1:])
        stragglers += [row for row in self.procs.wait_exit(code, 0.0) if token.covers(row)]
        if stragglers:
            progress("stop", HELPERS_DETAIL)
        for row in stragglers:
            try:
                sent = self.procs.terminate(token, row)
            except SafetyViolation as refused:
                return StopResult(False, "aborted", detail=str(refused), helpers_self_exited=helpers_self_exited,
                                  stragglers=len(stragglers))
            except DeckError:
                break   # the wait below reports it as a survivor
            if sent:
                self._signal()
        survivors = self.procs.wait_exit(main + stragglers, self.timings.terminate_wait)
        if survivors:
            return StopResult(False, "aborted", survivors=survivors, detail=NOT_STOPPED_MESSAGE,
                              helpers_self_exited=helpers_self_exited, stragglers=len(stragglers))
        return StopResult(True, "managed", helpers_self_exited=helpers_self_exited, stragglers=len(stragglers))

    # ---- settle ----
    def _settle(self, progress) -> bool:
        progress("settle", SETTLE_DETAIL)
        released = self.procs.wait_until(lambda: facts_mac.files_released(self.paths, self.procs),
                                         self.timings.settle_file, interval=0.5)
        if released:
            time.sleep(self.timings.settle_grace)
        return released

    # ---- helpers ----
    def _signal(self) -> None:
        """Records the first signal actually sent: from then on a cancel waits for the ladder to finish."""
        if self._first_signal is None:
            self._first_signal = time.monotonic()

    def _log_size(self) -> int:
        try:
            return self.paths.claude_main_log.stat().st_size
        except OSError:
            return 0

    def _log_text(self, offset: int) -> str:
        """main.log from `offset` (from the start when the log was rotated meanwhile), at most 1 MB."""
        try:
            with open(self.paths.claude_main_log, "rb") as handle:
                handle.seek(0, 2)
                handle.seek(offset if handle.tell() >= offset else 0)
                return handle.read(MAX_LOG_READ).decode("utf-8", "replace")
        except OSError:
            return ""

    def _markers(self, offset: int) -> list[str]:
        lines = [LOG_PREFIX.sub("", line).strip()[:200] for line in self._log_text(offset).splitlines()
                 if any(text in line for text in MARKER_TEXTS)]
        return lines[:MAX_MARKERS]


def graceful_first(paths, procs, main) -> bool:
    """Whether the stop starts with T1a: whenever there is a main process to send SIGTERM to (PLAN §1.3).
    (`paths` and `procs` are kept for switch_claude's stop-tier preview, which passes them.)"""
    return main is not None


def _waiting_detail(blockers) -> str:
    """"Waiting for A, B, C and 2 more to finish" (`blockers` is never empty while waiting)."""
    names = [session.name or f"Session {session.pid}" for session in blockers]
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    return f"Waiting for {shown} to finish"
