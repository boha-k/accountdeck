"""The local HTTP server: dashboard files and the JSON API of PLAN §3.1.

Security rules: listen on 127.0.0.1 only; every request must name exactly
`Host: 127.0.0.1:<port>`; the bootstrap token (handed to the page in the URL fragment) is
exchanged for an HttpOnly, SameSite=Strict session cookie; every POST needs the dashboard's
Origin, `X-AccountDeck: 1` and a JSON body; every response carries a strict CSP.
"""
from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import threading
from typing import Protocol
import urllib.parse

from . import BUILD, __version__, config, diagnostics
from .core import DeckError

WEB_ROOT = Path(__file__).resolve().parent / "web"
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
MAX_BODY = 65536
HISTORY_LIMIT = 700
DIAGNOSTICS_LINES = 200
DIAGNOSTICS_MAX_LINES = 1000
MAX_ACCOUNT_ID = 128
OPERATION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# DeckError codes that are not state conflicts; every other code is answered with 409.
ERROR_STATUS = {"invalid": 400, "not_found": 404}

SIGN_IN = "Open AccountDeck 2 from Applications or its menu bar item."
LOAD_FAILED = "Could not load the dashboard. Try refreshing."
ACTION_FAILED = "The action could not be completed. Your saved accounts are preserved."


class EngineAPI(Protocol):
    """What the server needs from `accountdeck.engine.Engine`; called from HTTP handler threads.

    Failures the user should see are raised as DeckError (codes of §3.1); anything else is a 500.
    """

    @property
    def pending(self) -> dict | None:
        """The running operation (§3.5); `/api/stop` answers 409 `busy` while it is set."""

    def snapshot(self, rev: int | None) -> dict | None:
        """The state of §3.3, or None when `rev` equals the current revision (answered 304). An engine may also
        offer `looked()`, which a page's first read (one without `rev`) calls once its state is built; it must return
        at once, and True from it means the state changed (the answer is built again)."""

    def action(self, name: str, body: dict) -> dict | None:
        """Answers `switch-plan` and other quick actions inline and enqueues operations (§3.2).

        Returns the fields merged into `{"ok": true}`. A DeckError may carry `retry_after`
        and, for `confirm_required`/`needs_reconfirm`, a fresh `plan` for the error body.
        """

    def settings(self) -> dict:
        """The settings of §3.6 as the dashboard sees them."""

    def update_settings(self, patch: dict) -> None:
        """Validates and applies a patch; unknown keys or bad values raise DeckError `invalid`."""

    def history(self, account: str, since: float | None, limit: int) -> dict:
        """`{"account", "readings", "snapshots"}` newer than `since`, at most `limit` (≤ 700) each."""

    def manifest(self, op: str | None) -> dict | None:
        """A per-switch manifest (§10.2), the latest when `op` is None; None when there is none."""

    def diagnostics_tail(self, lines: int) -> dict:
        """`{"lines": [redacted log lines], "path": log file, "health": Engine.health()}` for the last
        `lines` (≤ 1000) entries. An engine may also offer `health()`, which `/_health` then includes."""

    def stop(self) -> None:
        """Cancels the running operation and stops background work before the server shuts down."""


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Without it the port stays refused for 30 s after the last request (TIME_WAIT), so a restart found it "taken" and
    # moved to another port. A live listener on this address still refuses the bind: a second copy can't share it.
    allow_reuse_address = True
    # The dashboard asks for some thirty files when it opens, each on a new connection, about six at a time, beside its
    # polls. Python's default of five waiting connections is too few on a busy Mac: the ones past it are refused and
    # the page comes up without a script.
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], engine: EngineAPI, token: str | None = None, *,
                 web_root: Path = WEB_ROOT):
        if address[0] != "127.0.0.1":
            raise ValueError("AccountDeck 2 listens on 127.0.0.1 only.")
        self.engine = engine
        self.token = token or secrets.token_urlsafe(32)
        self.session = secrets.token_urlsafe(32)
        self.web_root = Path(web_root).resolve()
        super().__init__(address, Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        # Browsers share cookies between the ports of one host (RFC 6265 §8.5); a per-port name keeps
        # another local app (or a demo on another port) from replacing this session.
        self.cookie_name = f"ad2_session_{self.server_port}"

    def stop_soon(self) -> None:
        """Stops the engine, then ends `serve_forever` so run.py can finish its shutdown order."""
        def stop():
            try:
                self.engine.stop()
            finally:
                self.shutdown()  # blocks until serve_forever returns, hence its own thread
        threading.Thread(target=stop, name="ad2-http-stop", daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "AccountDeck2"
    timeout = 10

    def log_message(self, format, *args):
        """No access log: request lines carry account ids and query values."""

    def do_GET(self):
        self._handle(self._route_get, LOAD_FAILED)

    def do_POST(self):
        self._handle(self._route_post, ACTION_FAILED)

    def send_error(self, code, message=None, explain=None):
        """Protocol errors found by BaseHTTPRequestHandler (malformed request, unsupported method)
        get the same JSON body and security headers as every other response."""
        self._reply(code, {"error": message or HTTPStatus(code).phrase})

    def _handle(self, route, failure_message):
        try:
            route()
        except DeckError as exc:
            self._reply(*_error_response(exc))
        except Exception as exc:
            _log_failure(self.command, self.path.partition("?")[0], exc)     # logged before the client hears
            self._reply(500, {"error": failure_message})

    def _route_get(self):
        if not self._valid_host():
            self._reply(403, {"error": "Local access only."})
            return
        path, query = self._target()
        if path == "/_health":
            self._health()
        elif path == "/":
            self._static("index.html")
        elif path == "/icon.svg":
            self._static("icon.svg")
        elif path.startswith("/static/"):
            self._static(urllib.parse.unquote(path.removeprefix("/static/")))
        elif not path.startswith("/api/"):
            raise _not_found()
        elif not self._authenticated():
            self._reply(401, {"error": SIGN_IN})
        else:
            route = {"/api/state": self._state, "/api/history": self._history,
                     "/api/diagnostics": self._diagnostics, "/api/manifest": self._manifest}.get(path)
            if route is None:
                raise _not_found()
            route(query)

    def _route_post(self):
        if not self._valid_host() or self.headers.get("Origin") != self.server.origin:
            self._reply(403, {"error": "Requests must come from this local dashboard."})
            return
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if self.headers.get("X-AccountDeck") != "1" or content_type != "application/json":
            self._reply(403, {"error": "Invalid request."})
            return
        path, _ = self._target()
        if path != "/api/session" and not self._authenticated():
            self._reply(401, {"error": SIGN_IN})
            return
        route = {"/api/session": self._open_session, "/api/action": self._action,
                 "/api/settings": self._settings, "/api/stop": self._stop}.get(path)
        if route is None:
            raise _not_found()
        route(self._read_body())

    # ---- routes -------------------------------------------------------------------------

    def _health(self):
        body = {"app": "accountdeck2", "version": __version__, "build": BUILD,
                "mode": "demo" if config.demo_mode() else "live", "safe_mode": config.safe_mode()}
        # Liveness (no secrets): whether the tick loop and the terminal reads are still moving.
        health = getattr(self.server.engine, "health", None)
        if callable(health):
            try:
                body.update(stalled=None, engine=None)
                state = health()
                body.update(stalled=bool(state["stalled"]), engine=state)
            except Exception as exc:      # /_health must answer even when the engine cannot
                _log_failure("GET", "/_health", exc)
        # The watchdog and the database's care: read from memory, so polling stays cheap.
        service = getattr(self.server.engine, "service_state", None)
        if callable(service):
            try:
                body.update(watchdog=None, db=None)
                body.update(service())
            except Exception as exc:
                _log_failure("GET", "/_health", exc)
        self._reply(200, body)

    def _static(self, relative: str):
        file = _static_file(self.server.web_root, relative)
        if file is None:
            raise _not_found()
        self._send(200, file.read_bytes(), STATIC_TYPES[file.suffix.lower()])

    def _open_session(self, body: dict):
        if not _same_secret(body.get("token"), self.server.token):
            self._reply(401, {"error": SIGN_IN})
            return
        cookie = f"{self.server.cookie_name}={self.server.session}; HttpOnly; SameSite=Strict; Path=/"
        self._reply(200, {"ok": True}, cookie=cookie)

    def _state(self, query: dict[str, str]):
        rev = _whole(query, "rev")
        state = self.server.engine.snapshot(rev)
        looked = getattr(self.server.engine, "looked", None)
        # A page's first read (later polls name their revision) counts as the user looking only once its state was
        # built: an update check in the background (at most once a minute), and Antigravity's usage on open, which
        # the answer then shows under way.
        if rev is None and callable(looked) and looked() is True and state is not None:
            state = self.server.engine.snapshot(None) or state
        if state is None:
            self._not_modified()
        else:
            self._reply(200, state)

    def _history(self, query: dict[str, str]):
        account = query.get("id", "")
        if not 0 < len(account) <= MAX_ACCOUNT_ID:
            raise DeckError("Choose an account.", "invalid")
        since = _timestamp(query, "since")
        limit = _whole(query, "limit", HISTORY_LIMIT, low=1, high=HISTORY_LIMIT)
        self._reply(200, self.server.engine.history(account, since, limit))

    def _diagnostics(self, query: dict[str, str]):
        lines = _whole(query, "lines", DIAGNOSTICS_LINES, low=1, high=DIAGNOSTICS_MAX_LINES)
        self._reply(200, self.server.engine.diagnostics_tail(lines))

    def _manifest(self, query: dict[str, str]):
        op = query.get("op") or None
        if op is not None and not OPERATION_ID.fullmatch(op):
            raise DeckError("That switch record name is not valid.", "invalid")
        manifest = self.server.engine.manifest(op)
        if manifest is None:
            raise DeckError("No switch has been recorded yet." if op is None
                            else "That switch record is no longer available.", "not_found")
        self._reply(200, manifest)

    def _action(self, body: dict):
        name = body.get("action")
        if not isinstance(name, str) or not name:
            raise DeckError("Choose an action.", "invalid")
        self._reply(200, {"ok": True, **(self.server.engine.action(name, body) or {})})

    def _settings(self, body: dict):
        patch = body.get("patch")
        if not isinstance(patch, dict):
            raise DeckError('Send the settings to change as {"patch": {...}}.', "invalid")
        engine = self.server.engine
        engine.update_settings(patch)
        self._reply(200, {"ok": True, "settings": engine.settings()})

    def _stop(self, _body: dict):
        if self.server.engine.pending:
            raise DeckError("Finish or cancel the current action first.", "busy")
        self._reply(200, {"ok": True})
        self.server.stop_soon()

    # ---- request helpers ----------------------------------------------------------------

    def _valid_host(self) -> bool:
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def _target(self) -> tuple[str, dict[str, str]]:
        path, _, query = self.path.partition("?")
        return path, dict(urllib.parse.parse_qsl(query, keep_blank_values=True))

    def _authenticated(self) -> bool:
        # Parsed by hand: SimpleCookie rejects the whole header when another local app on
        # 127.0.0.1 has set a cookie with an unusual name.
        for header in self.headers.get_all("Cookie", ()):
            for pair in header.split(";"):
                name, _, value = pair.strip().partition("=")
                if name == self.server.cookie_name and _same_secret(value, self.server.session):
                    return True
        return False

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        body = None
        if 0 < length <= MAX_BODY:
            try:
                body = json.loads(self.rfile.read(length))
            except (OSError, ValueError, RecursionError):
                pass  # answered below like any other malformed body
        if not isinstance(body, dict):
            raise DeckError("Invalid request body.", "invalid")
        return body

    # ---- responses ----------------------------------------------------------------------

    def _reply(self, status: int, body: dict, *, cookie: str | None = None):
        data = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send(status, data, "application/json; charset=utf-8", cookie)

    def _not_modified(self):
        try:
            self.send_response(304)
            self._security_headers()
            self.end_headers()
        except (ConnectionError, TimeoutError):
            pass  # the dashboard went away; nothing left to answer

    def _send(self, status: int, data: bytes, content_type: str, cookie: str | None = None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self._security_headers()
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionError, TimeoutError):
            pass  # the dashboard went away; nothing left to answer

    def _security_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)


def _error_response(exc: DeckError) -> tuple[int, dict]:
    """The HTTP status and body for a DeckError: 400 `invalid`, 404 `not_found`, otherwise 409."""
    body = {"error": str(exc), "code": exc.code}
    if getattr(exc, "retry_after", None):
        body["retry_after"] = exc.retry_after
    if getattr(exc, "plan", None) is not None:
        body["plan"] = exc.plan
    return ERROR_STATUS.get(exc.code, 409), body


def _not_found() -> DeckError:
    return DeckError("Not found.", "not_found")


def _same_secret(candidate: object, secret: str) -> bool:
    """Constant-time comparison that refuses non-strings and non-ASCII input up front."""
    return isinstance(candidate, str) and candidate.isascii() and hmac.compare_digest(candidate, secret)


def _whole(query: dict[str, str], name: str, default: int | None = None, *, low: int = 0,
           high: int | None = None) -> int | None:
    """A whole-number query parameter of at least `low`, clamped to `high`; `default` when absent."""
    raw = query.get(name, "")
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 15) or int(raw) < low:
        raise DeckError(f"'{name}' must be a whole number of at least {low}.", "invalid")
    return int(raw) if high is None else min(int(raw), high)


def _timestamp(query: dict[str, str], name: str) -> float | None:
    raw = query.get(name, "")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value >= 0):
        raise DeckError(f"'{name}' must be a Unix timestamp.", "invalid")
    return value


def _static_file(root: Path, relative: str) -> Path | None:
    """The file `relative` names under `root`, or None unless it is a servable asset inside it."""
    parts = relative.split("/")
    if not all(_plain_name(part) for part in parts) or Path(parts[-1]).suffix.lower() not in STATIC_TYPES:
        return None
    try:
        path = root.joinpath(*parts).resolve()
        servable = path.is_relative_to(root) and path.is_file()
    except OSError:
        return None
    return path if servable else None


def _plain_name(part: str) -> bool:
    """A file or folder name: no dot segments or hidden names, separators, drives or NUL."""
    return bool(part) and not part.startswith(".") and not any(ch in part for ch in "\\:\x00")


def _log_failure(method: str, path: str, exc: Exception) -> None:
    diagnostics.log_exception("http.error", exc, lvl="info", method=method, path=path)
