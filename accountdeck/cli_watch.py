"""Claude Code's transcripts as a limit signal: a session writes an error line the moment a limit stops it, so
AccountDeck learns of a limit within seconds, also while its own usage reads fail. Everything here only reads:
no transcript is written and no session is messaged.

`TranscriptWatcher` (thread `ad2-watch`) tails `paths.claude_projects`, subagents' transcripts too: only bytes
appended since its first pass, only complete lines, capped per pass. Of an error line it keeps the classification
(`classify`), never the text.

`LimitWatch` (`Engine.watch`, on the tick thread) turns those signals into cli_policy's existing `account["out"]`
input; there is no other decision path:
- a limit error counts only at most FRESH old and SETTLE after the last change of the active account: running
  sessions keep their old token for a while, so an earlier error belongs to the account just left;
- a counted error asks for one read of the active account past the normal spacing (`request_signal`) and marks the
  account out. The read decides first, for READ_GRACE at most, so a read that fails or cannot go never delays a switch;
- a reading taken after the error that shows room in the limit it named (below the threshold, no lock) clears it,
  as does the window's reset. A reading that confirms it, or none, leaves it. When the terminals leave the account it
  keeps standing against it (`left_out`, also in `cli_history`), so auto-switch does not return to it, not even as a
  fallback, until then or until the owner makes that account active again;
- FLAPS signal-driven switches an hour are the most: beyond them signals are only logged, and one alert says so.

Its tick also takes Claude Code's free usage answer into the readings (`CLIAccounts.cached_reading`) and raises one
alert while the account in use cannot be read at all (`_blind_spot`)."""
from __future__ import annotations

from collections import deque
import json
import math
import os
from pathlib import Path
import re
from stat import S_ISREG
import threading
import time

from . import activity, cli_auto, diagnostics
from .config import cli_custom_home, safe_mode
from .core import is_frozen
from .procs import is_test_owned_path
from .rules import FABLE
from .usage import number, timestamp

MARK = b'"isApiErrorMessage":true'       # Claude Code's JSON has no spaces, and a text quoting this has `\"`
# The lines that say a session went on after a limit error. last-prompt, cost-state, tools-used and queue-operation
# lines also follow one in a session that stays stopped.
SPOKEN = (b'"type":"user"', b'"type":"assistant"')
POLL = 3.0
# Every pass stats the files changed within HOT_AGE and their folders. Every SLUG_EVERY passes also each project
# folder (a new session), every FULL_EVERY passes every folder and file (6000 files, 1800 folders: about 15 ms).
HOT_AGE = 86400.0
SLUG_EVERY, FULL_EVERY = 2, 5
MAX_CHUNK, MAX_PASS = 4 << 20, 32 << 20    # bytes read per file and per pass; a longer line is no error line and is dropped
MAX_DEPTH = 6                              # <slug>/<session>/subagents/workflows/<run>/
SKIPPED_FOLDERS = frozenset({"tool-results"})
GONE = (FileNotFoundError, NotADirectoryError)
MAX_SIGNALS = 256
STOPPED_KEPT = 86400.0
NAME_EVERY = 30.0                          # a stopped session without a registry row is looked for again this often
NAME_CHARS = 200
TROUBLE_EVERY = 600.0                      # one log line per kind of trouble this often

FRESH, SETTLE, FUTURE = 300.0, 90.0, 60.0  # how old an error may be; the margin after an account change; clock skew
READ_GRACE = 3.0                           # how long a counted error waits for its read (a read takes 0.3 to 1 s)
REACTION = 30.0                            # after a counted error, a read that ends wakes the tick this long
FLAPS, FLAP_SPAN = 3, 3600.0
CACHED_EVERY = 30.0
BLIND_AGE = 900.0
CEILING = {"session": 18000.0, "weekly": 604800.0, "model": 604800.0}    # a limit whose reset nobody named ends after this
LONGEST = 604800.0                         # and none stands longer than a week
# The sessions that use the terminals' login (the program's entrypoint names; a line without one is a terminal's too).
# Claude Desktop, Cowork, remote and chat-app sessions sign in on their own.
TERMINAL_ENTRYPOINTS = frozenset({"cli", "sdk-ts", "sdk-cli", "sdk-py", "claude-vscode"})

_UNSET = object()
_LIMIT_TEXT = re.compile(r"You['’]ve (?:hit|reached) your ([^·\n]{0,40}?) ?limit\b")
_MODEL = re.compile(r"(?:Fable|Haiku|Mythos|Opus|Sonnet)(?: \S{1,20})?")     # Claude Code's own list of model families
# Claude Code's words for what is no plan limit: usage credits, spend limits, budgets, seats, Fast mode, and "API Error:"
# for plain 429s, overload and capacity. Another limit-looking text is logged by class and is no signal either.
_NOT_A_LIMIT = re.compile(r"API Error|You['’]re out of|Your (?:org|seat|usage allocation|group)\b|This service\b"
                          r"|\S+(?: \S+)? requires usage credits|\S+ is experiencing high load"
                          r"|You['’]ve (?:hit|reached) your (?:fast limit|usage credit limit|monthly |team's |org's |channel's |individual )")
_LIMIT_WORDS = re.compile(r"(?i)\b(?:limit|quota|usage)\b")
_CODE = re.compile(r"[\w.-]{1,40}")
# What a text without a name ("You've hit your limit", "... your usage limit") means, from the same line.
_RATE_LIMIT_TYPES = {"five_hour": ("session", None), "seven_day": ("weekly", None), "seven_day_opus": ("model", "Opus"),
                     "seven_day_sonnet": ("model", "Sonnet"), "seven_day_overage_included": ("model", "Fable")}


def _message_text(record):
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    for block in content if isinstance(content, list) else ():
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            return block["text"]
    return ""


def classify(record):
    """What an API error line (a decoded transcript line) says: {"kind", "model", "resets"} with kind "session",
    "weekly" or "model" (a limit), "auth", or "unknown" (limit-looking text, logged only); None when it is none of
    these. A limit's kind comes from its words, as "You've hit your team's shared budget · your 5-hour limit resets
    5pm" carries the same fields as a 5-hour limit; its reset from `apiErrorParams.rate_limit_info.resetsAt`. Only
    sessions on the terminals' login (TERMINAL_ENTRYPOINTS) count."""
    entrypoint = record.get("entrypoint")
    if entrypoint is not None and not (isinstance(entrypoint, str) and entrypoint in TERMINAL_ENTRYPOINTS):
        return None
    error, status = record.get("error"), record.get("apiErrorStatus")
    if error == "authentication_failed" or status == 401:
        return {"kind": "auth", "model": None, "resets": None}
    if error != "rate_limit" and status != 429:
        return None
    text = _message_text(record)
    params = record.get("apiErrorParams")
    info = params.get("rate_limit_info") if isinstance(params, dict) else None
    # The text's "resets ..." is printed from this `resetsAt`; `quotaLimits` carries the same fields.
    info = info if isinstance(info, dict) else record.get("quotaLimits") if isinstance(record.get("quotaLimits"), dict) else {}
    found = _LIMIT_TEXT.match(text)
    name, kind = found.group(1).strip() if found else None, None
    model = None
    if name in ("session", "5-hour"):
        kind = "session"
    elif name == "weekly":
        kind = "weekly"
    elif name and _MODEL.fullmatch(name):
        kind, model = "model", name
    elif name in ("", "usage") and info.get("status") == "rejected" and isinstance(info.get("rateLimitType"), str):
        kind, model = _RATE_LIMIT_TYPES.get(info["rateLimitType"], (None, None))
    if kind is not None:
        return {"kind": kind, "model": model, "resets": timestamp(info.get("resetsAt"))}
    if _NOT_A_LIMIT.match(text) or not _LIMIT_WORDS.search(text):
        return None
    return {"kind": "unknown", "model": None, "resets": None}


def _code(value):
    return value if isinstance(value, (str, int)) and _CODE.fullmatch(str(value)) else None


class _Tail:
    """One transcript: `pos` is where its first unhandled line starts, `seen` how far its bytes were looked at (a
    partial line is read again only once it grew), `skip` that the rest of a dropped line comes first, `session`
    its id when it is a main transcript (`<slug>/<session>.jsonl`)."""
    __slots__ = ("pos", "seen", "ino", "mtime", "skip", "session")

    def __init__(self, pos, ino, mtime, session):
        self.pos, self.seen, self.ino, self.mtime, self.skip, self.session = pos, pos, ino, mtime, False, session


class TranscriptWatcher:
    """A file present at the first pass begins at its end, a later one at 0; a replaced or shrunk one restarts at its
    new end. `signals` come out through `drain`; `notify(found_signals)` is told after a pass that changed something.
    Missing folders, permission errors, invalid JSON and huge lines cost a log line at most."""

    def __init__(self, paths, *, clock=time.time, notify=None):
        self.paths, self.clock, self.notify = paths, clock, notify
        self._root = str(paths.claude_projects)
        self._files, self._dirs = {}, {}                     # path -> _Tail; folder -> st_mtime_ns at its last scan
        self._slugs, self._hot_files, self._hot_dirs = set(), set(), set()
        self._passes, self._budget = 0, MAX_PASS
        self._allowed = False
        self._signals = deque(maxlen=MAX_SIGNALS)
        self._stopped = {}                                   # main session id -> its entry in `stopped()`
        self._named_at = -math.inf
        self._trouble_at = {}
        self._changed = self._found = False
        self._lock, self._passing = threading.Lock(), threading.Lock()
        self._stopping = threading.Event()
        self._thread = None

    # ---- the thread ----
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="ad2-watch", daemon=True)
            self._thread.start()

    def stop(self):
        self._stopping.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(5)

    @property
    def running(self):
        return self._allowed and self._thread is not None and self._thread.is_alive()

    def _run(self):
        while not self._stopping.is_set():
            try:
                self.poll()
            except Exception as exc:     # a pass must never end the watching
                self._trouble(type(exc).__name__)
            self._stopping.wait(POLL)

    def _trouble(self, cause):
        now = time.monotonic()
        if now - self._trouble_at.get(cause, -math.inf) >= TROUBLE_EVERY:
            self._trouble_at[cause] = now
            diagnostics.log("cli.watch_trouble", lvl="warning", cause=cause)

    def _permitted(self):
        """Never the real folders in tests and demos, and not a folder Claude Code isn't using."""
        if safe_mode():
            return self.paths.mode == "synthetic" and is_test_owned_path(self.paths.claude_projects) \
                and is_test_owned_path(self.paths.claude_sessions)
        return not cli_custom_home()

    # ---- one pass ----
    def poll(self):
        """One pass, also the thread's. The first takes in every file at its end."""
        with self._passing:
            self._allowed = self._permitted()
            if not self._allowed:
                return
            first = self._passes == 0
            self._passes += 1
            self._budget = MAX_PASS
            full = self._passes % FULL_EVERY == 1
            folders = {self._root, *(self._dirs if full else self._hot_dirs)}
            if not full and self._passes % SLUG_EVERY == 0:
                folders |= self._slugs
            for folder in folders:
                self._look(folder, first)
            for path in list(self._files if full else self._hot_files):
                self._follow(path)
            if full:
                self._cool(time.time())
            self._tidy_stopped()
            with self._lock:
                changed, found, self._changed, self._found = self._changed, self._found, False, False
            if changed and self.notify is not None:
                self.notify(found)

    def _look(self, folder, first):
        try:
            stamp = os.stat(folder).st_mtime_ns
        except GONE:
            self._dirs.pop(folder, None)
            self._slugs.discard(folder)
            self._hot_dirs.discard(folder)
            return
        except OSError as exc:      # unreadable for now: it keeps its place
            self._trouble(type(exc).__name__)
            return
        if self._dirs.get(folder) != stamp:
            self._scan(folder, first)

    def _scan(self, folder, first):
        """Takes in what is new in `folder`, a new subfolder at once. Its time is taken first, so what appears
        meanwhile shows on the next pass."""
        depth = folder[len(self._root):].count(os.sep)
        try:
            self._dirs[folder] = None          # not taken in until this worked: a later pass tries again
            stamp = os.stat(folder).st_mtime_ns
            with os.scandir(folder) as found:
                entries = list(found)
            self._dirs[folder] = stamp
        except OSError as exc:
            self._trouble(type(exc).__name__)
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.path not in self._dirs and depth < MAX_DEPTH and entry.name not in SKIPPED_FOLDERS:
                        if depth == 0:
                            self._slugs.add(entry.path)
                        self._hot_dirs.add(entry.path)
                        self._scan(entry.path, first)
                elif entry.name.endswith(".jsonl") and entry.path not in self._files and entry.is_file(follow_symlinks=False):
                    info = entry.stat(follow_symlinks=False)
                    self._files[entry.path] = _Tail(info.st_size if first else 0, info.st_ino, info.st_mtime,
                                                    entry.name[:-len(".jsonl")] if depth == 1 else None)
                    self._hot_files.add(entry.path)
            except OSError:
                continue

    def _cool(self, wall):
        """Hot again from what the full pass just saw: recent files, and the folders where a recent session's next
        transcript would appear."""
        self._hot_files = {path for path, tail in self._files.items() if wall - tail.mtime < HOT_AGE}
        self._hot_dirs = {os.path.dirname(path) for path in self._hot_files}
        for path in self._hot_files:
            if self._files[path].session is not None:
                folder = path[:-len(".jsonl")]
                self._hot_dirs.update(f for f in (folder, folder + os.sep + "subagents") if f in self._dirs)

    def _follow(self, path):
        tail = self._files[path]
        try:
            info = os.stat(path, follow_symlinks=False)
        except GONE:
            info = None
        except OSError as exc:
            self._trouble(type(exc).__name__)
            return
        if info is None or not S_ISREG(info.st_mode):
            del self._files[path]
            self._hot_files.discard(path)
            return
        tail.mtime = info.st_mtime
        if info.st_ino != tail.ino or info.st_size < tail.seen:
            tail.pos, tail.seen, tail.ino, tail.skip = info.st_size, info.st_size, info.st_ino, False
        elif info.st_size > tail.seen and self._budget > 0:
            self._read(path, tail, info.st_size)

    def _read(self, path, tail, size):
        wanted = min(size - tail.pos, MAX_CHUNK, self._budget)
        try:
            handle = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            try:
                data = os.pread(handle, wanted, tail.pos)
            finally:
                os.close(handle)
        except OSError as exc:
            self._trouble(type(exc).__name__)
            return
        self._budget -= len(data)
        tail.seen = tail.pos + len(data)
        end = data.rfind(b"\n")
        if end < 0:
            if len(data) >= MAX_CHUNK:      # a line no chunk can hold: the rest of it waits to be dropped too
                tail.pos, tail.skip = tail.seen, True
            return
        tail.pos += end + 1
        lines = data[:end]
        if tail.skip:
            cut = lines.find(b"\n")
            lines, tail.skip = b"" if cut < 0 else lines[cut + 1:], False
        session = tail.session
        if MARK not in lines and session not in self._stopped:
            return
        for line in lines.split(b"\n"):
            if MARK in line:
                self._error_line(tail, line)
            elif session in self._stopped and (SPOKEN[0] in line or SPOKEN[1] in line):
                self._went_on(session)

    def _went_on(self, session):
        with self._lock:
            self._changed = self._stopped.pop(session, None) is not None or self._changed

    def _error_line(self, tail, line):
        try:
            record = json.loads(line)
            if not isinstance(record, dict) or record.get("isApiErrorMessage") is not True:
                return             # such text nested in a tool result, say
            found, at = classify(record), timestamp(record.get("timestamp"))
        except (ValueError, TypeError, AttributeError, RecursionError, OverflowError, OSError):
            self._trouble("invalid_line")
            return
        kind = found["kind"] if found else None
        if kind == "unknown":      # the class only: Claude Code's own codes, never the text
            diagnostics.log("cli.limit_unknown", error=_code(record.get("error")), status=_code(record.get("apiErrorStatus")),
                            api_error=_code(record.get("apiError")))
        signal = at is not None and kind not in (None, "unknown")
        limit = signal and kind != "auth"
        with self._lock:
            if signal:
                self._signals.append({"kind": kind, "model": found["model"], "resets": found["resets"], "at": at,
                                      "subagent": tail.session is None})
                self._changed = self._found = True
            if limit and tail.session is not None:
                old = self._stopped.get(tail.session) or {}
                self._stopped[tail.session] = {"session": tail.session, "name": old.get("name"),
                                               "project": old.get("project"), "kind": kind, "at": at}
                self._named_at = -math.inf if not old else self._named_at
            elif tail.session is not None and self._stopped.pop(tail.session, None) is not None:
                self._changed = True       # went on and failed some other way: its latest line is no limit error

    def _tidy_stopped(self):
        now = self.clock()
        with self._lock:
            expired = [key for key, row in self._stopped.items() if now - row["at"] >= STOPPED_KEPT]
            for key in expired:
                del self._stopped[key]
            self._changed = self._changed or bool(expired)
            wanted = {key for key, row in self._stopped.items() if row["name"] is None and row["project"] is None}
            if not wanted or now - self._named_at < NAME_EVERY:
                return
            self._named_at = now
        names = self._registry_names(wanted)
        with self._lock:
            for key, (name, project) in names.items():
                if key in self._stopped:
                    self._stopped[key].update(name=name, project=project)
                    self._changed = True

    def _registry_names(self, wanted):
        """session id -> (name, project folder's basename) of the wanted sessions in Claude Code's registry."""
        names = {}
        try:
            with os.scandir(self.paths.claude_sessions) as found:
                entries = list(found)
        except OSError:
            return names
        for entry in entries:
            if not activity.REGISTRY_FILE.fullmatch(entry.name):
                continue
            try:
                if entry.stat(follow_symlinks=False).st_size > activity.MAX_ROW_BYTES:
                    continue
                with open(entry.path, "rb") as handle:
                    row = json.loads(handle.read(activity.MAX_ROW_BYTES + 1))
            except (OSError, ValueError, RecursionError):
                continue
            key = row.get("sessionId") if isinstance(row, dict) else None
            if key in wanted:
                name, cwd = row.get("name"), row.get("cwd")
                names[key] = ((name[:NAME_CHARS] if isinstance(name, str) and name else None),
                              (Path(cwd).name[:NAME_CHARS] or None) if isinstance(cwd, str) else None)
        return names

    # ---- what the engine takes ----
    def drain(self):
        with self._lock:
            signals = list(self._signals)
            self._signals.clear()
        return signals

    def stopped(self, now):
        """The main sessions whose latest conversation line is a limit error, newest first."""
        with self._lock:
            rows = [dict(row) for row in self._stopped.values() if now - row["at"] < STOPPED_KEPT]
        return sorted(rows, key=lambda row: -row["at"])


def _same_model(a, b):
    """Prefixes either way: an error says "Fable", the API's limit "Fable 5.1", the pinned model either."""
    a, b = a.strip().casefold(), b.strip().casefold()
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def _names_window(limit, window):
    if window.get("kind") != limit["kind"]:
        return False
    if limit["kind"] != "model":
        return True
    return any(_same_model(limit["model"], name)
               for name in (window.get("model"), (window.get("label") or "").removesuffix(" weekly")) if name)


def _ended(limit, row, threshold, now):
    """Why a limit error no longer stands against the account in `row`: "reset", "reading" (taken after the error and
    showing room in that limit), or None. A reading without that limit shows no room in it."""
    windows = [w for w in (row["last_good_usage"] or {}).get("windows") or ()
               if isinstance(w, dict) and _names_window(limit, w)]
    resets = limit["resets"]
    if resets is None:
        resets = max((reset for reset in (number(w.get("reset")) for w in windows)
                      if reset is not None and reset > limit["at"]), default=limit["at"] + CEILING[limit["kind"]])
    if now >= min(resets, limit["at"] + LONGEST):
        return "reset"
    read_at = row["last_good_at"]
    if (windows and read_at is not None and read_at > limit["at"]
            and all(number(w.get("used")) is not None and w["used"] < threshold and not w.get("blocked") for w in windows)):
        return "reading"
    return None


def _count(number_of, thing):
    return f"{number_of} {thing}{'' if number_of == 1 else 's'}"


def _took(seconds):
    """"2 s", "4 min", "1 h 5 min"."""
    seconds = max(1, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{minutes} min" if not hours else f"{hours} h {minutes} min" if minutes else f"{hours} h"


def _left_from(value):
    """`cli_history["limits_left"]` ({account id: [limit]}) as `LimitWatch._left`; anything malformed is dropped."""
    left = {}
    for key, rows in value.items() if isinstance(value, dict) else ():
        for row in rows if isinstance(rows, list) else ():
            if (isinstance(row, dict) and row.get("kind") in CEILING and number(row.get("at")) is not None
                    and (row.get("model") is None or isinstance(row.get("model"), str))
                    and (row.get("resets") is None or number(row.get("resets")) is not None)):
                left.setdefault(key, {})[(row["kind"], row.get("model"))] = {
                    "kind": row["kind"], "model": row.get("model"), "at": row["at"], "resets": row.get("resets")}
    return left


def _saved(left):
    return {key: [dict(limit) for limit in limits.values()] for key, limits in left.items()}


class LimitWatch:
    """The engine's side. `tick` and `out` may run on different threads (`out` is called wherever `cli_auto.inputs`
    is), so all that they share is under `_lock`, which is only ever followed by the store's, the settings' and the
    scheduler's own locks (`cli_history` is written outside it)."""

    def __init__(self, engine):
        self.engine = engine
        self.transcripts = TranscriptWatcher(engine.paths, clock=engine.clock, notify=self._notified)
        self._lock = threading.Lock()
        self._identity, self._account, self._changed_at = _UNSET, None, None
        self._limits = {}            # (kind, model) -> {"kind", "model", "at", "resets"}: what stands against `_account`
        # account id -> its `_limits` when the terminals left it, until `_ended`; `_kept` is what cli_history holds.
        self._left = _left_from(engine.store.setting("cli_history", {}).get("limits_left"))
        self._kept = _saved(self._left)
        self._last, self._episode = None, None    # the latest signal; the one the limits standing began with
        self._read_by, self._asked_at = -math.inf, math.inf      # a counted error's read: answer due by; asked at
        self._reacting_until = -math.inf
        self._future_left = (None, None)                         # a `left_at` later than now; when it was first seen
        self._switches = deque()     # when signal-driven switches happened (FLAP_SPAN)
        self._flap_due = self._flap_told = False
        self._cached_at = -math.inf
        self._blind = engine.store.setting("cli_history", {}).get("blind_alert")

    def start(self):
        self.transcripts.start()

    def stop(self):
        self.transcripts.stop()

    def _notified(self, found):
        self.engine.bump()
        if found:
            self.engine.wake()

    # ---- the tick ----
    def tick(self):
        engine = self.engine
        now = engine.clock()
        identity = engine.active_identity.get("claude_cli")
        row = engine.store.find("claude_cli", identity) if identity else None
        row = None if is_frozen(row) else row
        with self._lock:
            self._note_active(identity, row, now)
        settled = self._settled_after()          # every tick, so a `left_at` later than now is seen at once
        if row is not None and now - self._cached_at >= CACHED_EVERY:
            self._cached_at = now
            if engine.cli.cached_reading(not_before=settled):
                row = engine.store.account(row["id"])
        signals = self.transcripts.drain()
        with self._lock:
            for signal in signals:
                self._signal(signal, row, now)
            if row is not None:
                self._standing(row, now)
            flapping = self._flapping(now)
            tell = self._flap_due and not self._flap_told
            self._flap_due, self._flap_told = False, (self._flap_told or tell) and flapping
        if tell:
            self._tell_flapping(row, now)
        self._tidy_left(now)
        self._blind_spot(row, now)
        if signals:
            engine.bump()

    def _note_active(self, identity, row, now):
        """Caller holds `_lock`. Any change of the active account, however it came, moves the margin. What stood
        against the account left keeps standing against it (`left_out`). An account that becomes active again
        starts afresh: auto-switch skips it, so only the user's choice (here or with /login) made it so."""
        if identity != self._identity:
            if self._identity is not _UNSET:
                self._changed_at = now
            self._identity = identity
            if self._limits and self._account is not None:
                kept = self._left.setdefault(self._account, {})
                for name, limit in self._limits.items():
                    if name not in kept or limit["at"] > kept[name]["at"]:
                        kept[name] = dict(limit)
                diagnostics.log("cli.limit_left", kinds=sorted(name[0] for name in self._limits), acct=diagnostics.account_ref(self._account))
            self._limits = {}
            if row is not None and self._left.pop(row["id"], None) is not None:
                diagnostics.log("cli.limit_ended", kind=None, why="chosen", acct=diagnostics.account_ref(row["id"]))
        self._account = row["id"] if row else None

    def _settled_after(self):
        """When errors stop belonging to the account before this one. A switch by this service is also in the
        store, so one from before a restart still counts; one dated later than now (the clock was set back) counts
        as the moment it was first seen. Tick thread only."""
        left, now = number(self.engine.store.setting("cli_history", {}).get("left_at")), self.engine.clock()
        if left is not None and left > now:
            if self._future_left[0] != left:
                self._future_left = (left, now)
            left = self._future_left[1]
        return max(-math.inf if left is None else left, self._changed_at if self._changed_at is not None else -math.inf) + SETTLE

    def _flapping(self, now):
        while self._switches and self._switches[0] <= now - FLAP_SPAN:
            self._switches.popleft()
        return len(self._switches) >= FLAPS

    def _refused(self, signal, row, now):
        """Why a signal does not count, or None. Caller holds `_lock`."""
        engine = self.engine
        if row is None:
            return "no_account"
        if not engine.owns_login or engine.cli.adapter.readonly_reason:
            return "read_only"
        age = now - signal["at"]
        if age > FRESH or age < -FUTURE:
            return "old"
        if signal["at"] <= self._settled_after():
            return "settling"
        if signal["kind"] == "auth":
            return None
        # Auto-switch watches Fable's limit and the pinned model's (cli_policy.considered), so no other can pick a target.
        if signal["kind"] == "model" and not any(_same_model(signal["model"], name)
                                                 for name in (FABLE, engine.prefs.get("pinned_model") or "")):
            return "unwatched"
        ended = _ended(signal, row, engine.prefs.get("cli_threshold"), now)
        if ended:
            return ended
        if self._flapping(now):
            self._flap_due = True
            return "flap_guard"
        return None

    def _signal(self, signal, row, now):
        """Caller holds `_lock`."""
        why = self._refused(signal, row, now)
        kind, key = signal["kind"], row["id"] if row else None
        diagnostics.log("cli.limit_signal", kind=kind, age=round(now - signal["at"], 1), counted=why is None, why=why,
                        subagent=signal["subagent"], acct=diagnostics.account_ref(key) if key else None)
        entry = {"at": signal["at"], "kind": kind, "account": key, "counted": why is None, "reaction_s": None}
        if why is None and kind != "auth":
            began = not self._limits
            limit = (kind, signal["model"])
            if limit not in self._limits or signal["at"] > self._limits[limit]["at"]:
                self._limits[limit] = {name: signal[name] for name in ("kind", "model", "at", "resets")}
            if began:
                self._last = self._episode = {**entry, "since": self._settled_after()}
            self._reacting_until = now + REACTION
        # A switch's trailing errors, which belong to the account left, don't hide how it went.
        elif not (self._last and self._last["counted"] and self._last["kind"] != "auth"
                  and signal["at"] - self._last["at"] <= FRESH):
            self._last = entry
        if why is None and self.engine.scheduler.request_signal(key, now):
            self._read_by, self._asked_at = now + READ_GRACE, now

    def _standing(self, row, now):
        """True when a limit error stands against the account in `row`. Caller holds `_lock`."""
        threshold = self.engine.prefs.get("cli_threshold")
        for name, limit in list(self._limits.items()):
            why = _ended(limit, row, threshold, now)
            if why:
                del self._limits[name]
                diagnostics.log("cli.limit_ended", kind=limit["kind"], why=why, acct=diagnostics.account_ref(row["id"]))
        if not self._limits and self._episode is not None and self._episode["account"] == row["id"]:
            self._episode = None
        return bool(self._limits)

    def out(self, row, now):
        """The active account's `out` input: a limit error stands against it, and its read has had its moment."""
        with self._lock:
            if row["id"] != self._account or not self._standing(row, now):
                return False
            return not (now < self._read_by and self._awaited(row))

    def _awaited(self, row):
        """True while the read a counted error asked for has neither answered (`checked` is stamped when a read starts
        and stored when it ends) nor been found unable to go. Caller holds `_lock`."""
        return (number(row["flags"].get("checked")) or -math.inf) < self._asked_at and self.engine.scheduler.settling(row["id"])

    def left_out(self, row, now):
        """A non-active account's `out` input: a limit error still stands against it from when the terminals left it,
        so cli_policy never picks it, not even as a fallback."""
        with self._lock:
            return row["id"] in self._left and self._prune(row["id"], row, self.engine.prefs.get("cli_threshold"), now)

    def _prune(self, key, row, threshold, now):
        """Ends what `_ended` (or the account's removal) ends of what stands against an account left. Caller holds `_lock`."""
        limits = self._left.get(key) or {}
        for name, limit in list(limits.items()):
            why = "gone" if row is None else _ended(limit, row, threshold, now)
            if why:
                del limits[name]
                diagnostics.log("cli.limit_ended", kind=limit["kind"], why=why, acct=diagnostics.account_ref(key))
        if not limits:
            self._left.pop(key, None)
        return bool(limits)

    def _tidy_left(self, now):
        """Prunes what stands against the accounts left and keeps the rest in `cli_history`: a restart keeps it."""
        engine = self.engine
        with self._lock:
            threshold = engine.prefs.get("cli_threshold")
            for key in list(self._left):
                self._prune(key, engine.store.account(key), threshold, now)
            kept = _saved(self._left)
        if kept != self._kept:
            self._kept = kept
            with cli_auto.history(engine) as past:
                past.pop("limits_left", None)
                if kept:
                    past["limits_left"] = kept

    def urgent(self):
        """True for REACTION after a counted error while it stands: a read that ends then wakes the tick
        (Engine._refresh), the read the error asked for or a target's read the switch waits for (`_reads_first`)."""
        with self._lock:
            return bool(self._limits) and self.engine.clock() < self._reacting_until

    def tick_wait(self, seconds):
        """How long the tick loop sleeps: while a counted error's read is awaited, no longer than until it is due to
        have answered (its end and any new signal wake the loop earlier). No faster ticks otherwise."""
        with self._lock:
            key = self._account
            wait = self._read_by - self.engine.clock() if self._limits and key is not None else 0.0
        if wait <= 0:
            return seconds
        row = self.engine.store.account(key)
        with self._lock:
            awaited = row is not None and self._awaited(row)
        return min(seconds, wait + 0.05) if awaited else seconds

    # ---- after a switch ----
    def switched(self, op, now):
        """A terminal switch worked. When limit errors drove it, counts it for the flap guard and returns the sentence
        that says how fast it reacted and what the limit stopped (" " first); otherwise ""."""
        last = self.engine.store.setting("cli_history", {}).get("last") or {}
        with self._lock:
            episode, self._episode = self._episode, None
            if not (episode and getattr(op, "auto_kind", "manual") in ("out", "refilled")
                    and last.get("from") == episode["account"] and last.get("to") == op.account):
                return ""
            reaction = max(0.0, now - episode["at"])
            episode["reaction_s"] = round(reaction, 1)
            self._switches.append(now)
        stopped = sum(row["at"] >= episode["since"] for row in self.transcripts.stopped(now))
        diagnostics.log("cli.limit_reaction", seconds=round(reaction, 1), stopped=stopped, acct=diagnostics.account_ref(op.account))
        told = f"Switched {_took(reaction)} after the limit error."
        if stopped:
            told += f" The limit stopped {_count(stopped, 'session')}: say \"continue\" there to go on."
        self.engine.notice(told, "info", sticky=False)
        return " " + told

    def _tell_flapping(self, row, now):
        engine = self.engine
        title = "Limit errors keep coming after switching"
        message = (f"AccountDeck switched your terminals {FLAPS} times in the last hour because of limit errors and they "
                   "keep coming, so it won't switch for them for now. Usage readings still switch as usual.")
        engine.notice(message, "warning")
        if engine.prefs.get("alerts_enabled"):
            engine.store.add_alert({"kind": "limit_reached", "level": "warning", "account": row["id"] if row else None,
                                    "window": None, "title": title, "message": message, "at": now})
        diagnostics.log("cli.limit_flapping", switches=FLAPS)

    # ---- no readings at all ----
    def _blind_spot(self, row, now):
        """One alert while the account in use has no reading younger than BLIND_AGE because its reads fail and no free
        reading came either: AccountDeck then only learns of a limit from the transcripts. It ends when readings return
        or another account is in use. A login that needs sign-in or is on hold has its own alert."""
        engine = self.engine
        blind = (row is not None and not engine.prefs.get("refresh_paused") and row["error_code"] is not None
                 and row["flags"].get("health", "ok") == "ok"
                 and now - (row["last_good_at"] or row["created"]) > BLIND_AGE)
        if blind == (self._blind is not None) or (blind and now < engine.alert_engine.hold_until):
            return
        if blind:
            label = row["label"]
            self._blind = "off"
            if engine.prefs.get("alerts_enabled"):
                self._blind = engine.store.add_alert({
                    "kind": "limit_reached", "level": "warning", "account": row["id"], "window": None,
                    "title": f"AccountDeck can't read {label}'s usage",
                    "message": f"Usage checks of {label}, the account your terminals use, have failed for over "
                               "15 minutes, so its numbers are old. A limit error in a session still switches your terminals.",
                    "at": now})
            diagnostics.log("cli.blind", acct=diagnostics.account_ref(row["id"]), code=row["error_code"])
        else:
            engine.store.update_alert(self._blind, dismissed=True)
            self._blind = None
            diagnostics.log("cli.blind_ended")
        with cli_auto.history(engine) as past:
            past.pop("blind_alert", None)
            if self._blind is not None:
                past["blind_alert"] = self._blind
        engine.bump()

    # ---- the state ----
    def state(self, now):
        """`providers.claude_cli.watch`."""
        with self._lock:
            last = None if self._last is None else {key: self._last[key] for key in ("at", "kind", "account", "counted", "reaction_s")}
        return {"running": self.transcripts.running, "last": last, "stopped": self.transcripts.stopped(now)}
