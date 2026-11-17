"""User operations (§2.2, §3.5): switch, quit, add, capture, remove, recover and the move import run
one at a time on the `ad2-ops` thread and are observed as the PendingOperation and SwitchReport of
/api/state."""
from __future__ import annotations

import contextlib
import copy
from dataclasses import dataclass
import secrets
import threading
import time

from .core import DeckError
from .diagnostics import account_ref, frames, log


PHASES = ("queued", "planning", "waiting_idle", "stopping", "settling", "saving", "restoring", "launching",
          "verifying", "done", "unconfirmed", "failed", "cancelled", "needs_reconfirm")
TERMINAL_PHASES = frozenset({"done", "unconfirmed", "failed", "cancelled", "needs_reconfirm"})
STEP_STATUSES = ("pending", "active", "done", "skipped", "warning", "failed")
GENERIC_FAILURE = "The action could not be completed. Your saved accounts are preserved."
# The contract requires a non-empty pending message and report summary; these stand in until the
# operation supplies its own copy.
STARTING_MESSAGE = "Starting…"
DONE_SUMMARY = "Done."


def _report_defaults():
    """SwitchReport fields an Operation may fill through `report` (§3.5), with their values when it
    does not. The runner sets op, kind, provider, account, label, finished, outcome and error."""
    return {"verified": None, "verified_at": None, "verify_reason": None, "stop_tier": None, "stop_ms": None,
            "helpers_self_exited": None, "stragglers": None, "summary": None, "carried": [],
            "account_bound": [], "manifest": None}


_REPORT_FIELDS = frozenset(_report_defaults())


class OperationCancelled(DeckError):
    """Raised at a checkpoint once the user cancelled; the operation ends as `cancelled`. An
    operation that had to put something back first raises it again with its own message, e.g.
    "Cancelled. Claude was reopened with the same account." """

    def __init__(self, message="Cancelled. Nothing was changed."):
        super().__init__(message, "cancelled")


@dataclass
class Step:
    id: str
    label: str
    status: str = "pending"
    detail: str = ""
    started: float | None = None
    ended: float | None = None

    def snapshot(self):
        return {"id": self.id, "label": self.label, "status": self.status, "detail": self.detail,
                "started": self.started, "ended": self.ended}


class Operation:
    """One user operation. Subclasses implement run(), which executes on the `ad2-ops` thread and
    reports progress with step(), set_phase(), note() and set_waiting().

    Cancelling: while `cancellable`, OperationRunner.cancel() sets `cancel_event`; the request takes
    effect at the next checkpoint() (entering a step is one, and so is sleep()), so run() decides
    where stopping is safe. Once `cancellable` is False, cancel is refused as `busy`.

    A `preemptible` operation changes nothing while it waits for sessions to finish (phase
    `waiting_idle`); there, and only there, OperationRunner.preempt() cancels it the same way for a
    terminal switch auto-switch needs. It leaves that phase before its first change and checks for a
    cancel after the phase change, so a preempt either ends it before anything changed or is refused.

    run() ends by returning (outcome success; phase `unconfirmed` when report["verified"] is
    "unconfirmed", else `done`), raising OperationCancelled (cancelled), raising DeckError with code
    `needs_reconfirm` and `plan=<fresh SwitchPlan>` (the operation stays pending in that phase until
    another one is submitted or it is cancelled), or raising anything else (failed; an exception that
    is not a DeckError is reported with code `invalid` and GENERIC_FAILURE, never its own text).
    Fields placed in `report` (see _report_defaults) are merged into the SwitchReport; the summary
    defaults to the ending message ("Done." on success).

    `token` holds the ConsentToken of this job (§4.3); it is never serialised."""

    not_cancellable_message = "The switch is past the point of no return."
    preemptible = False

    def __init__(self, kind, *, provider=None, account=None, label=None, consent=None, steps=(),
                 eta_seconds=None, token=None):
        self.kind, self.provider, self.account, self.label = kind, provider, account, label
        self.consent, self.eta_seconds, self.token = consent, eta_seconds, token
        self.steps = [Step(step_id, step_label) for step_id, step_label in steps]
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("step ids must be unique")
        self.id = None
        self.started = None
        self.phase = "queued"
        self.message = STARTING_MESSAGE
        self.cancellable = True
        self.preempted = None        # the ending message once OperationRunner.preempt cancelled it
        self.preempted_for = None    # and what it gave way to, for the owner once it has ended
        self.waiting_on = []
        self.current_step = None
        self.report = {}
        self.plan = None
        self.clock = time.time
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.done_event = threading.Event()
        self._notify = None

    def run(self):
        raise NotImplementedError

    # ---- progress ----
    @contextlib.contextmanager
    def step(self, step_id, *, phase=None, message=None, cancellable=None, eta_seconds=None):
        """Run the block as step `step_id`: active, then done (or the warning/skipped status note()
        gave it), failed on an error, skipped when cancelled or reconfirming. Entering is a checkpoint, made under the
        lock OperationRunner.cancel takes: a cancel it accepted ends the job here, and is never left unheeded by a step
        that cannot be cancelled."""
        step = self._step(step_id)
        with self.lock:
            self.checkpoint()
            self._apply(phase, message, cancellable, eta_seconds)
            step.status, step.started, step.ended = "active", self.clock(), None
            self.current_step = step.id
        self._changed()
        began = time.monotonic()
        try:
            yield step
        except BaseException as exc:
            status = "skipped" if getattr(exc, "code", None) in ("cancelled", "needs_reconfirm") else "failed"
            self._end_step(step, status, began, force=True)
            raise
        self._end_step(step, "done", began)

    def set_phase(self, phase, message=None, *, cancellable=None, eta_seconds=None):
        """Move to a running phase; the terminal phases are set when run() ends."""
        with self.lock:
            self._apply(phase, message, cancellable, eta_seconds)
        self._changed()

    def note(self, detail, *, status=None, step_id=None):
        """Set the detail line (and optionally the status) of a step, by default the active one."""
        if status is not None and status not in STEP_STATUSES:
            raise ValueError(f"unknown step status {status!r}")
        step = self._step(step_id or self.current_step)
        with self.lock:
            step.detail = detail
            if status is not None:
                step.status = status
        self._changed()

    def set_waiting(self, sessions):
        """What the operation waits for in `waiting_idle`: [{"pid", "name", "status"}]."""
        rows = [{"pid": row["pid"], "name": row["name"], "status": row["status"]} for row in sessions]
        with self.lock:
            if self.phase != "waiting_idle":
                raise ValueError("waiting_on is only shown in the waiting_idle phase")
            self.waiting_on = rows
        self._changed()

    # ---- cancellation ----
    @property
    def cancel_requested(self):
        return self.cancel_event.is_set()

    def checkpoint(self):
        if self.cancel_event.is_set():
            raise OperationCancelled()

    def sleep(self, seconds):
        """Wait, ending early through OperationCancelled when the user cancels."""
        if self.cancel_event.wait(seconds):
            raise OperationCancelled()

    def _yields(self):
        """Caller holds `lock`: whether OperationRunner.preempt may cancel it now."""
        return (self.preemptible and self.phase == "waiting_idle" and self.cancellable
                and not self.cancel_event.is_set())

    # ---- observation ----
    def snapshot(self):
        """The PendingOperation of §3.5; `plan` is added while the phase is `needs_reconfirm`."""
        with self.lock:
            pending = {"id": self.id, "kind": self.kind, "provider": self.provider, "account": self.account,
                       "label": self.label, "phase": self.phase, "message": self.message,
                       "cancellable": self.cancellable, "started": self.started, "eta_seconds": self.eta_seconds,
                       "consent": self.consent, "waiting_on": [dict(row) for row in self.waiting_on],
                       "steps": [step.snapshot() for step in self.steps]}
            if self.phase == "needs_reconfirm":
                pending["plan"] = copy.deepcopy(self.plan)
        return pending

    def wait(self, timeout=None):
        return self.done_event.wait(timeout)

    # ---- internals ----
    def _apply(self, phase, message, cancellable, eta_seconds):
        if phase is not None and (phase not in PHASES or phase in TERMINAL_PHASES):
            raise ValueError(f"{phase!r} is not a running phase")
        if message is not None and (not isinstance(message, str) or not message):
            raise ValueError("the pending message must be a non-empty string")
        if phase is not None:
            self.phase = phase
            if phase != "waiting_idle":
                self.waiting_on = []
        if message is not None:
            self.message = message
        if cancellable is not None:
            self.cancellable = cancellable
        if eta_seconds is not None:
            self.eta_seconds = eta_seconds

    def _step(self, step_id):
        for step in self.steps:
            if step.id == step_id:
                return step
        raise ValueError(f"unknown step {step_id!r}")

    def _end_step(self, step, status, began, force=False):
        with self.lock:
            if force or step.status == "active":
                step.status = status
            step.ended = self.clock()
            if status == "done":
                self.current_step = None
        log("op.step", op=self.id, kind=self.kind, step=step.id, status=step.status,
             ms=round((time.monotonic() - began) * 1000))
        self._changed()

    def _finish(self, phase, message):
        with self.lock:
            self.phase, self.message, self.waiting_on = phase, message, []
            self.cancellable = phase == "needs_reconfirm"      # cancelling it just closes the prompt
            for step in self.steps:
                if step.status in ("pending", "active"):
                    step.status = "skipped"

    def _changed(self):
        if self._notify is not None:
            self._notify()


class OperationRunner:
    """Runs one Operation at a time on a thread named `ad2-ops`.

    `on_change()` is called after every visible change (for the state `rev`), `on_settle(op)` after
    run() returned but before the operation is published as ended (so observers that see it finished
    also see what it changed, e.g. the re-sensed active account), `on_finish(op, report)` after an
    operation ended (`report` is None for `needs_reconfirm`); all three run outside every lock
    and their exceptions are logged, never propagated into an operation. `clock` stamps the
    timestamps shown to the user (the demo passes its virtual clock)."""

    def __init__(self, *, on_change=None, on_finish=None, on_settle=None, clock=time.time):
        self.on_change, self.on_finish, self.on_settle, self.clock = on_change, on_finish, on_settle, clock
        self.lock = threading.Lock()
        self._current = None      # the running operation, or one left pending in needs_reconfirm
        self._thread = None
        self._report = None
        self._settling = False    # `on_finish` of the operation that just ended still runs
        self._closed = False

    @property
    def current(self):
        with self.lock:
            return self._current

    @property
    def busy(self):
        """True while an operation runs, and until `on_finish` has recorded how it ended: a tick in between
        would decide without that record (a failed switch's back-off, an interrupted switch's recovery) and
        submit the same job again."""
        with self.lock:
            return self._running() or self._settling

    def submit(self, op):
        """Start op and return its id; `busy` while another operation runs or settles. An operation left in
        `needs_reconfirm` is replaced, and the previous report is cleared."""
        if op.id is not None:
            raise ValueError("an operation can be submitted once")
        with self.lock:
            if self._closed:
                raise DeckError("AccountDeck is shutting down.", "busy")
            if self._running() or self._settling:
                raise DeckError("Finish or cancel the current action first.", "busy")
            op.id = f"op_{int(self.clock() * 1000):x}{secrets.token_hex(3)}"
            op.started, op.clock, op._notify = self.clock(), self.clock, self._changed
            thread = threading.Thread(target=self._execute, args=(op,), name="ad2-ops", daemon=True)
            self._current, self._thread, self._report = op, thread, None
        thread.start()
        self._changed()
        return op.id

    def cancel(self, op_id=None):
        """Ask the running operation to stop at its next checkpoint, or drop one left in
        `needs_reconfirm`. False when there is nothing to cancel; `busy` once the operation is past
        its last cancellation point."""
        with self.lock:
            op = self._current
            if op is None or op_id not in (None, op.id):
                return False
            if not op.done_event.is_set():
                with op.lock:
                    if not op.cancellable:
                        raise DeckError(op.not_cancellable_message, "busy")
                    op.cancel_event.set()
                return True
            self._current = None
        self._changed()
        return True

    def yields(self):
        """True while the running operation only waits for sessions to finish and gives way to a terminal
        switch auto-switch needs (`preempt`)."""
        with self.lock:
            op = self._current if self._running() else None
            if op is None:
                return False
            with op.lock:
                return op._yields()

    def preempt(self, message, op_id=None, why=None):
        """Cancels the running operation (`op_id`, when given) for a terminal switch auto-switch needs, only
        while it waits for sessions to finish (see Operation): it ends at its next checkpoint, a poll later,
        as cancelled with `message`, and `why` stays on it (`preempted_for`). False when nothing was
        cancelled, e.g. once it has left its wait."""
        with self.lock:
            op = self._current if self._running() else None
            if op is None or op_id not in (None, op.id):
                return False
            with op.lock:
                if not op._yields():
                    return False
                op.preempted, op.preempted_for = message, why
                op.cancel_event.set()
        log("op.preempted", op=op.id, kind=op.kind, provider=op.provider)
        self._changed()
        return True

    def pending(self):
        with self.lock:
            op = self._current
        return op.snapshot() if op is not None else None

    def report(self):
        with self.lock:
            return copy.deepcopy(self._report)

    def snapshot(self):
        return {"pending": self.pending(), "report": self.report()}

    def update_report(self, op_id, **fields):
        """Change fields of the last report, e.g. when the background verifier settles a switch.
        False when the last report belongs to another operation or was dismissed."""
        if not set(fields) <= _REPORT_FIELDS | {"outcome", "error"}:
            raise ValueError("unknown report field")
        with self.lock:
            if self._report is None or self._report["op"] != op_id:
                return False
            self._report = {**self._report, **copy.deepcopy(fields)}
        self._changed()
        return True

    def dismiss_report(self):
        with self.lock:
            dismissed, self._report = self._report is not None, None
        if dismissed:
            self._changed()
        return dismissed

    def wait(self, timeout=None):
        """Wait until the current operation and its callbacks have finished; False on timeout."""
        with self.lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def stop(self, timeout=None):
        """Shutdown: refuse new operations, cancel the running one while it still can be, and wait."""
        with self.lock:
            self._closed = True
            op = self._current if self._running() else None
        if op is not None:
            with op.lock:
                if op.cancellable:
                    op.cancel_event.set()
        return self.wait(timeout)

    # ---- the ad2-ops thread ----
    def _running(self):
        return self._current is not None and not self._current.done_event.is_set()

    def _execute(self, op):
        began = time.monotonic()
        log("op.start", op=op.id, kind=op.kind, acct=account_ref(op.account))
        failure = None
        try:
            op.set_phase("planning")
            op.run()
        except BaseException as exc:     # every failure ends the operation; nothing escapes this thread
            failure = exc
        code = failure.code if isinstance(failure, DeckError) else None
        error = None
        if failure is None:
            phase = "unconfirmed" if op.report.get("verified") == "unconfirmed" else "done"
            outcome, message = "success", op.report.get("summary") or DONE_SUMMARY
        elif code in ("cancelled", "needs_reconfirm"):
            phase, outcome, message = code, "cancelled", str(failure)
            if code == "cancelled" and op.preempted:
                message = op.preempted
        else:
            phase, outcome = "failed", "failed"
            message = (str(failure) if code else "") or GENERIC_FAILURE
            # The contract has no code for "unexpected"; such a failure is reported like a
            # DeckError raised without a code. diagnostics.log keeps its exception type.
            error = {"code": code or "invalid", "message": message, "step": op.current_step}
        if phase == "needs_reconfirm":
            op.plan = failure.plan
        if self.on_settle is not None:      # still pending (not terminal) while the result is sensed
            self._callback(self.on_settle, op)
        op._finish(phase, message)
        report = None
        if phase != "needs_reconfirm":
            report = {**_report_defaults(), **copy.deepcopy(op.report), "op": op.id, "kind": op.kind,
                      "provider": op.provider, "account": op.account, "label": op.label,
                      "finished": self.clock(), "outcome": outcome, "error": error}
            report["summary"] = report["summary"] or message
        with self.lock:
            if report is not None or op.cancel_event.is_set():    # a cancelled prompt does not linger
                self._current = None
            if report is not None:
                self._report = report
            self._settling = True
            op.done_event.set()
        ms = round((time.monotonic() - began) * 1000)
        if phase == "failed":
            log("op.fail", "warning", op=op.id, kind=op.kind, step=op.current_step, status=phase, ms=ms,
                 code=code, error_type=None if code else type(failure).__name__,
                 frames=None if code else frames(failure))
        else:
            log("op.done", op=op.id, kind=op.kind, status=phase, ms=ms)
        self._changed()
        try:
            if self.on_finish is not None:
                self._callback(self.on_finish, op, copy.deepcopy(report))
        finally:
            with self.lock:
                self._settling = False
        self._changed()

    def _changed(self):
        if self.on_change is not None:
            self._callback(self.on_change)

    @staticmethod
    def _callback(function, *args):
        try:
            function(*args)
        except Exception as exc:         # a listener's bug must not break the operation it observes
            log("op.callback_error", "warning", error_type=type(exc).__name__, frames=frames(exc))
