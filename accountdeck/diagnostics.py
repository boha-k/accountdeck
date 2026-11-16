"""Secret-free diagnostics: a rotating JSON-lines log and per-operation manifests (PLAN §10).

Every string that reaches the log or a manifest is redacted first: tokens, cookies,
keys and emails are masked, UUIDs are shortened and long strings truncated.
Callers log opaque references (`account_ref`) and status classes (`http_class`),
never labels, bodies or raw errors.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import logging.handlers
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import traceback

LOG_FILE = "accountdeck2.log"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 5
MAX_STRING = 200
MANIFESTS_KEPT = 100
LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}

_REDACTIONS = (
    (re.compile(r"eyJ[\w-]*(?:\.[\w-]*){0,2}"), "<jwt>"),                 # JWTs, also unsigned or partial
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer <redacted>"),
    # The replacement deliberately avoids the cookie name, so a search for "sessionKey" finds nothing.
    (re.compile(r"sessionKey[^;\s=]*=[^;\s]+"), "<session-cookie>"),
    (re.compile(r"\bsk-[\w-]+"), "<key>"),
    (re.compile(r"(?i)\b(access_?token|refresh_?token|id_?token|api_?key|client_?secret|password)"
                r"([\"']?\s*[:=]\s*[\"']?)[^\s\"',;&}]+"), r"\1\2<redacted>"),
    (re.compile(r"[\w.+%-]+@[\w-]+(?:\.[\w-]+)*"), "<email>"),
    (re.compile(r"\b([0-9a-fA-F]{8})-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), r"\1"),
)
# Values under these field names are never written, whatever they look like.
SECRET_FIELDS = frozenset({"token", "access_token", "refresh_token", "id_token", "cookie", "cookies",
                           "authorization", "password", "secret", "api_key", "session_key", "email", "label"})

_logger = logging.getLogger("accountdeck")
_logger.addHandler(logging.NullHandler())
_logger.propagate = False
_lock = threading.Lock()
_handler: logging.Handler | None = None
_logs_dir: Path | None = None


def redact_text(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text if len(text) <= MAX_STRING else text[:MAX_STRING] + "…"


def redact(value, key: str | None = None):
    """A JSON-ready copy of `value` with every string redacted and secret-named fields masked."""
    if key is not None and value is not None and str(key).lower() in SECRET_FIELDS:
        return "<redacted>"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


class RedactingFilter(logging.Filter):
    """Redacts the message and every field of a record before any handler formats it (PLAN §10.1)."""

    def filter(self, record):
        try:
            record.msg, record.args = redact_text(record.getMessage()), ()
            fields = getattr(record, "fields", None)
            record.fields = redact(fields) if isinstance(fields, dict) else {}
        except Exception as exc:
            # A record that cannot be redacted (a bad format string, an object that fails str())
            # is replaced, so nothing unredacted is written and the caller never sees the error.
            record.msg, record.args = "log.unredactable", ()
            record.fields = {"error_type": type(exc).__name__}
        return True


class _JsonLines(logging.Formatter):
    def format(self, record):
        seconds = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        entry = {"t": f"{seconds}.{int(record.msecs):03d}Z", "lvl": record.levelname.lower(),
                 "ev": record.getMessage()}
        for key, value in getattr(record, "fields", {}).items():   # a dict once RedactingFilter ran
            entry[key + "_" if key in entry else key] = value
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str)


def setup(paths, level: str = "info") -> Path:
    """Writes logs/accountdeck2.log in the data folder (1 MB, 5 rotations). Calling it again moves the log."""
    global _handler, _logs_dir
    logs = Path(paths.data_dir) / "logs"
    (logs / "manifests").mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(logs / LOG_FILE, maxBytes=MAX_BYTES,
                                                   backupCount=BACKUP_COUNT, encoding="utf-8", delay=True)
    handler.setFormatter(_JsonLines())
    handler.addFilter(RedactingFilter())
    with _lock:
        previous, _handler, _logs_dir = _handler, handler, logs
        _logger.addHandler(handler)
        if previous is not None:
            _logger.removeHandler(previous)
            previous.close()
    set_level(level)
    return logs / LOG_FILE


def close() -> None:
    """Stops writing and releases the log file (shutdown, and test teardown before deleting the folder)."""
    global _handler, _logs_dir
    with _lock:
        previous, _handler, _logs_dir = _handler, None, None
        if previous is not None:
            _logger.removeHandler(previous)
            previous.close()


def set_level(level: str) -> None:
    _logger.setLevel(LEVELS.get(level, logging.INFO))


def log_path() -> Path | None:
    logs = _logs_dir
    return logs / LOG_FILE if logs else None


def log(ev: str, /, lvl: str = "info", **fields) -> None:
    """One JSON line: {"t", "lvl", "ev", **fields}; a field that repeats one of those names is
    written with a "_" suffix. A no-op until `setup`."""
    _emit(ev, lvl, fields)


def log_exception(ev: str, exc: BaseException, /, lvl: str = "warning", **fields) -> None:
    """Logs an unexpected exception by class and where it was raised: `module:line:function` of the
    innermost FRAMES frames. Never its message or args, which may carry tokens, paths or bodies."""
    extra = {"error_type": type(exc).__name__, "frames": frames(exc)}
    if isinstance(exc, OSError) and isinstance(exc.errno, int):
        extra["errno"] = exc.errno
    code = getattr(exc, "code", None)
    if isinstance(code, str) and len(code) <= 40:
        extra["code"] = code
    _emit(ev, lvl, {**fields, **extra})


FRAMES = 5


def frames(exc: BaseException, limit: int = FRAMES) -> list[str]:
    found = []
    try:
        for frame in traceback.extract_tb(exc.__traceback__)[-limit:]:
            found.append(f"{Path(frame.filename).stem}:{frame.lineno}:{frame.name}")
    except Exception:      # a traceback that cannot be walked still logs its class
        pass
    return found


@contextlib.contextmanager
def timer(ev: str, /, **fields):
    """Logs `ev` with its duration as `ms`; fields added to the yielded dict are logged too.
    An escaping exception is logged as a warning with its class name and re-raised."""
    started = time.perf_counter()
    try:
        yield fields
    except BaseException as exc:
        _emit(ev, "warning", {**fields, "ms": _elapsed_ms(started), "error_type": type(exc).__name__})
        raise
    _emit(ev, "info", {**fields, "ms": _elapsed_ms(started)})


def _emit(ev: str, lvl: str, fields: dict) -> None:
    _logger.log(LEVELS.get(lvl, logging.INFO), ev, extra={"fields": fields})


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def tail(n: int = 200) -> list[str]:
    """The last `n` log lines, oldest first (already redacted when they were written)."""
    logs = _logs_dir
    if logs is None or n <= 0:
        return []
    lines: list[str] = []
    for name in (LOG_FILE, LOG_FILE + ".1"):
        if len(lines) >= n:
            break
        try:
            lines = (logs / name).read_text(encoding="utf-8", errors="replace").splitlines() + lines
        except OSError:
            continue
    return lines[-n:]


def account_ref(account_id) -> str | None:
    """A short stable reference for logs; account labels may be emails and are never logged."""
    if not account_id:
        return None
    return "acc_" + hashlib.sha256(str(account_id).encode("utf-8")).hexdigest()[:6]


def http_class(status) -> str:
    """The status class for logs: 2xx, 4xx, 5xx... with 429 and 529 kept apart; "network" without a response."""
    if status in ("timeout", "network"):
        return status
    if status is None:
        return "network"
    if isinstance(status, bool) or not isinstance(status, (int, str)) or not str(status).isdigit():
        return "other"
    code = int(status)
    if code in (429, 529):
        return str(code)
    return f"{code // 100}xx" if 100 <= code <= 599 else "other"


_OP_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class Manifest:
    """The per-operation record behind "Explain last switch" (PLAN §10.2), stored as
    logs/manifests/<op>.json. It holds names, counts and outcomes; every value is redacted
    like a log field, so a secret passed by mistake is masked rather than stored."""

    def __init__(self, op: str, kind: str, directory: Path):
        self.op, self.directory = op, Path(directory)
        self.data = {"op": op, "kind": kind, "started": time.time(), "finished": None}
        self._lock = threading.Lock()

    @classmethod
    def start(cls, op: str, kind: str, *, directory: Path | None = None) -> "Manifest":
        if not isinstance(op, str) or not _OP_ID.fullmatch(op):
            raise ValueError("Manifest ids are 1-64 letters, digits, '_' or '-'.")
        return cls(op, kind, directory or _manifest_dir())

    def set(self, path: str, value) -> "Manifest":
        """Sets a dotted path such as "stop.tier", creating the groups on the way."""
        with self._lock:
            group, key = self._slot(path)
            group[key] = redact(value, key)
        return self

    def append(self, path: str, value) -> "Manifest":
        """Appends to the list at a dotted path such as "verification.signals"."""
        with self._lock:
            group, key = self._slot(path)
            group.setdefault(key, []).append(redact(value))
        return self

    def save(self) -> Path | None:
        """Writes the manifest so far; returns None (and logs) when the disk refuses."""
        with self._lock:
            raw = json.dumps(self.data, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        path = self.directory / (self.op + ".json")
        try:
            _write_atomic(path, raw)
            _prune(self.directory)
        except OSError as exc:
            # A diagnostics failure must never fail the operation it describes.
            log("manifest.error", "warning", op=self.op, error_type=type(exc).__name__)
            return None
        return path

    def finish(self) -> Path | None:
        with self._lock:
            self.data["finished"] = time.time()
        return self.save()

    @staticmethod
    def load(op: str, *, directory: Path | None = None) -> dict | None:
        if not isinstance(op, str) or not _OP_ID.fullmatch(op):
            return None
        return _read_manifest((directory or _manifest_dir()) / (op + ".json"))

    @staticmethod
    def latest(*, directory: Path | None = None) -> dict | None:
        for path in _newest_first(directory or _manifest_dir()):
            manifest = _read_manifest(path)
            if manifest is not None:
                return manifest
        return None

    def _slot(self, path: str):
        *groups, key = path.split(".")
        node = self.data
        for name in groups:
            node = node.setdefault(name, {})
            if not isinstance(node, dict):
                raise TypeError(f"Manifest field {name!r} is not a group.")
        return node, key


def _manifest_dir() -> Path:
    logs = _logs_dir
    if logs is None:
        raise RuntimeError("diagnostics.setup(paths) must run before manifests are used.")
    return logs / "manifests"


def _read_manifest(path: Path) -> dict | None:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _newest_first(directory: Path) -> list[Path]:
    stamped = []
    for path in Path(directory).glob("*.json"):
        try:
            stamped.append((path.stat().st_mtime_ns, path.name, path))
        except OSError:
            continue
    return [path for _, _, path in sorted(stamped, reverse=True)]


def _prune(directory: Path) -> None:
    for path in _newest_first(directory)[MANIFESTS_KEPT:]:
        path.unlink(missing_ok=True)


def _write_atomic(path: Path, data: bytes) -> None:
    """Manifests are diagnostics, not credentials: a plain write-then-rename (no full sync) is enough."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
