"""Which Claude Code sessions of Claude Desktop are working (PLAN §4.5).

The source is Claude Code's own sessions registry (`<pid>.json` rows in `paths.claude_sessions`),
trusted only for a live Code-tab process whose creation time equals the row's `procStart` and whose
entrypoint is Claude Desktop. Everything else is "unknown", which counts as busy everywhere; "idle"
counts only once it has held for `idle_hold_s` seconds.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
import re

from .procs import proc_start_matches

WHITELIST = ("pid", "status", "kind", "entrypoint", "name", "cwd", "updatedAt", "statusUpdatedAt", "procStart",
             "version")
REGISTRY_FILE = re.compile(r"\d{1,10}\.json")      # never `<pid>.<hash>.key` or anything else
MAX_ROW_BYTES = 64 * 1024
DESKTOP_ENTRYPOINT = "claude-desktop"


@dataclass(frozen=True)
class SessionActivity:
    pid: int
    name: str | None
    status: str                 # "idle" | "busy" | "unknown"
    reason: str
    since: float | None         # when the current registry status began (epoch seconds), if known
    version: str | None


@dataclass(frozen=True)
class ActivityReport:
    sessions: list[SessionActivity]
    busy: int
    idle: int
    unknown: int
    foreign: int                # Claude Code processes outside Claude Desktop (informational)
    blockers: list[SessionActivity]   # busy + unknown sessions
    all_idle: bool
    registry_version: str | None


def read_registry(paths) -> dict[int, dict]:
    """pid -> whitelisted fields of each well-formed `<pid>.json` row (at most 64 KB each)."""
    rows: dict[int, dict] = {}
    try:
        entries = list(os.scandir(paths.claude_sessions))
    except OSError:
        return rows
    for entry in entries:
        if not REGISTRY_FILE.fullmatch(entry.name):
            continue
        try:
            if not entry.is_file(follow_symlinks=False) or entry.stat(follow_symlinks=False).st_size > MAX_ROW_BYTES:
                continue
            with open(entry.path, "rb") as handle:
                data = json.loads(handle.read(MAX_ROW_BYTES + 1).decode("utf-8"))
        except (OSError, ValueError):
            continue
        pid = int(entry.name[:-len(".json")])
        if isinstance(data, dict) and data.get("pid") == pid:
            rows[pid] = {key: data[key] for key in WHITELIST if key in data}
    return rows


def report(paths, procs, *, now: float, idle_hold_s: float = 20.0, state: dict | None = None,
           kinds: dict | None = None) -> ActivityReport:
    """The activity of every Code-tab session of Claude Desktop. `state` (kept by the caller between
    polls) remembers when a session without `statusUpdatedAt` was first seen idle. `kinds` is
    procs.classify() of a snapshot the caller already took; without it one is taken here."""
    if kinds is None:
        kinds = procs.classify(procs.snapshot())
    registry = read_registry(paths)
    seen: set = set()
    sessions = [_session(row, registry.get(row.pid), procs, now, idle_hold_s, state, seen)
                for row in kinds["claude_code"]]
    if state is not None:
        for key in [key for key in state if key not in seen]:
            del state[key]
    blockers = [session for session in sessions if session.status != "idle"]
    busy = sum(session.status == "busy" for session in sessions)
    versions = [session.version for session in sessions if session.version]
    return ActivityReport(
        sessions=sessions, busy=busy, idle=len(sessions) - len(blockers), unknown=len(blockers) - busy,
        foreign=len(kinds["claude_foreign"]), blockers=blockers, all_idle=not blockers,
        registry_version=max(versions, key=_version_key) if versions else None)


def _session(row, entry, procs, now, hold, state, seen) -> SessionActivity:
    if entry is None:
        children = len(procs.descendants(row.pid))
        reason = "no registry row" + (f"; {children} child process{'es' if children != 1 else ''}" if children else "")
        return SessionActivity(row.pid, None, "unknown", reason, None, None)
    name = entry.get("name") if isinstance(entry.get("name"), str) else None
    version = entry.get("version") if isinstance(entry.get("version"), str) else None

    def unknown(reason):
        return SessionActivity(row.pid, name, "unknown", reason, None, version)

    # Claude Code on macOS writes procStart as `ps -o lstart` text in UTC; a synthetic registry writes the
    # creation time itself. Either way it must name this very process, else the row is someone else's.
    if not proc_start_matches(procs.creation_time(row.pid), entry.get("procStart")):
        return unknown("registry row belongs to another process")
    if entry.get("entrypoint") != DESKTOP_ENTRYPOINT:
        return unknown("not started by Claude Desktop")
    status = entry.get("status")
    if not isinstance(status, str) or not status:
        return unknown("no status")
    changed = _timestamp(entry.get("statusUpdatedAt"))
    if status != "idle":
        return SessionActivity(row.pid, name, "busy", status[:40], changed, version)
    key = (row.pid, str(row.ctime))
    seen.add(key)
    first_seen = state.setdefault(key, now) if state is not None else now
    since = changed if changed is not None else first_seen
    held = now - since
    if held >= hold:
        return SessionActivity(row.pid, name, "idle", f"idle for {int(held)} s", since, version)
    return SessionActivity(row.pid, name, "busy", f"idle for {max(0, int(held))} s", since, version)


def _timestamp(value) -> float | None:
    """Epoch seconds from milliseconds, seconds or an ISO 8601 string."""
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"\d{1,16}(?:\.\d+)?", text):
            value = float(text)
        else:
            try:
                moment = datetime.fromisoformat(text)
            except ValueError:
                return None
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return value / 1000 if value > 1e11 else float(value)


def _version_key(version: str) -> tuple:
    return tuple(int(part) if part.isdigit() else -1 for part in re.split(r"[.+-]", version))
