"""Provider HTTP (A1, §5.10): one request with classification, AccountDeck's own user agent (Claude Code's on Claude's
resets read and claim only, `base.is_reset_request`) and the safe-mode host gate, and `head`, the bodiless request of
the update check (accountdeck/updates.py).

In safe mode (tests, demos) a request may only reach an in-process loopback server registered with
`register_test_host`; everything else raises SafetyViolation before a socket is opened.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import diagnostics
from ..config import DEFAULT_PORT, V1_PORT, SafetyViolation, safe_mode
from .base import (CLAUDE_CODE_AGENT, CLAUDE_CODE_VERSION, OAUTH_BETA, OAUTH_HOST, OAUTH_PATH, RESET_CLAIM_PATH,
                   USER_AGENT, is_reset_request)
from .errors import (ProviderError, classify_http, error_type, header, rate_limit_wait, retry_after_seconds,
                     token_description)

DEFAULT_TIMEOUT = 18
MAX_BODY = 4 * 1024 * 1024
HEAD_BYTES = 2048          # only this much of an error body is read, and only to classify it
BODY_TIMEOUTS = 3          # a whole answer gets this many timeouts: the socket's bounds each read, not their sum
LOOKUPS = 8                # name lookups at one time (`_look_up`)
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_LOOPBACK = ("127.0.0.1", "::1")
_CONTROLLED = frozenset({"user-agent", "authorization", "cookie", "anthropic-beta"})
_ORG_IN_PATH = re.compile(r"(/api/organizations/)[^/]+")     # a logged path never names the organisation
_VERSION = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,6}")

_test_hosts: set[str] = set()
_test_hosts_lock = threading.Lock()
_lookups = threading.BoundedSemaphore(LOOKUPS)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None        # never forward a credential request; the 3xx surfaces as an HTTPError


class _Opening:
    """Marks a failure to open the connection (name lookup, connect, a proxy tunnel, the TLS handshake) as
    `unsent`: http.client opens it before it sends the first byte of the request, so nothing was sent."""

    def connect(self):
        try:
            _look_up(self.host, self.port, self.timeout)
            super().connect()
        except OSError as exc:
            exc.unsent = True
            raise


class _HTTPConnection(_Opening, http.client.HTTPConnection):
    pass


class _HTTPSConnection(_Opening, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


def _look_up(host, port, timeout):
    """Name lookup is the one step of a connection no socket timeout covers, and a stuck resolver or VPN can hold
    it for minutes, pinning a read: it runs on a thread of its own and the request gives up when the time is over.
    The connection's own lookup then answers from the system's cache, and reports any failure. A stuck lookup keeps
    its thread: past LOOKUPS of them a request gives up at once rather than leave another."""
    if not _lookups.acquire(blocking=False):
        raise TimeoutError("name lookup")
    done = threading.Event()

    def lookup():
        try:
            socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            pass
        finally:
            _lookups.release()
            done.set()
    threading.Thread(target=lookup, name="ad2-lookup", daemon=True).start()
    if not done.wait(timeout):
        raise TimeoutError("name lookup")


def _read_body(response, deadline):
    """At most MAX_BODY + 1 bytes of the answer; TimeoutError when it is still dripping in at `deadline`."""
    chunks, size = [], 0
    while size <= MAX_BODY:
        chunk = response.read1(MAX_BODY + 1 - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if time.monotonic() > deadline:
            raise TimeoutError("answer body")
    return b"".join(chunks)


def register_test_host(host_port):
    """Lets requests reach an in-process test server at "127.0.0.1:<port>" (safe mode only)."""
    host, port = _split(host_port)
    if not safe_mode():
        raise SafetyViolation("Test servers can only be registered in safe mode.")
    if host not in _LOOPBACK or port in (V1_PORT, DEFAULT_PORT):
        raise SafetyViolation("Only a loopback test server on a free test port can be registered.")
    with _test_hosts_lock:
        _test_hosts.add(f"{host}:{port}")


def unregister_test_host(host_port):
    host, port = _split(host_port)
    with _test_hosts_lock:
        _test_hosts.discard(f"{host}:{port}")


def request_headers(url, *, headers=None, token=None, cookie=None):
    """The headers sent to `url`: the caller's, except that the user agent (AccountDeck's own, Claude Code's on
    Claude's resets read and claim only), credentials and the OAuth beta flag (api.anthropic.com OAuth endpoints
    only) are always decided here."""
    parts = urllib.parse.urlsplit(url)
    result = {"Accept": "application/json"}
    result.update({key: value for key, value in (headers or {}).items() if key.lower() not in _CONTROLLED})
    host = (parts.hostname or "").lower()
    result["User-Agent"] = (CLAUDE_CODE_AGENT.format(version=claude_code_version())
                            if is_reset_request(host, parts.path, parts.query) else USER_AGENT)
    # The OAuth endpoints, and the one claim of a limit reset (resets.py), which Claude Code sends with the same flag.
    if host == OAUTH_HOST and (parts.path.startswith(OAUTH_PATH) or RESET_CLAIM_PATH.fullmatch(parts.path)):
        result["anthropic-beta"] = OAUTH_BETA
    if token:
        result["Authorization"] = "Bearer " + token
    if cookie:
        result["Cookie"] = cookie
    return result


def claude_code_version():
    """The installed Claude Code's version, from the name of the file ~/.local/bin/claude links to (nothing run), or
    CLAUDE_CODE_VERSION when there is none or in safe mode."""
    if safe_mode():
        return CLAUDE_CODE_VERSION
    try:
        name = Path(os.readlink(Path.home() / ".local" / "bin" / "claude")).name
    except OSError:
        return CLAUDE_CODE_VERSION
    return name if _VERSION.fullmatch(name) else CLAUDE_CODE_VERSION


def request(method, url, *, headers=None, timeout=DEFAULT_TIMEOUT, cookie=None, token=None,
            token_was_expired=False, incident=False, json_body=None):
    """Sends one request. Returns (parsed JSON or None, meta) for a 2xx answer and raises a
    classified ProviderError otherwise. meta = {"status", "class", "ms", "cf_mitigated", "server",
    "error_type", "error_description", "retry_after", "unsent"}; bodies, tokens and cookies never reach meta
    or the log (`error_description` is only kept, sanitized, for a 4xx from the token endpoint). `unsent` is
    True only when the connection never opened, so the request certainly never left this Mac; a failure
    after it opened may follow a request that reached the server."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    opener = _opener(_gate(parts.scheme, host, parts.port or (443 if parts.scheme == "https" else 80)))
    sent_headers = request_headers(url, headers=headers, token=token, cookie=cookie)
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        sent_headers["Content-Type"] = "application/json"
    outgoing = urllib.request.Request(url, method=method, data=data, headers=sent_headers)
    meta = {"status": None, "class": "network", "ms": 0, "cf_mitigated": None, "server": None,
            "error_type": None, "error_description": None, "retry_after": None, "unsent": False}
    started = time.monotonic()
    try:
        with opener.open(outgoing, timeout=timeout) as response:
            meta.update(status=response.status, **_header_meta(response.headers))
            raw = _read_body(response, started + BODY_TIMEOUTS * timeout)
    except urllib.error.HTTPError as exc:
        error = _http_error(url, exc, meta, token_was_expired, incident)
    except (OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", None)
        timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
        meta["class"] = "timeout" if timed_out else "network"
        # urllib wraps what the request raised, connecting included, in a URLError; nothing after that.
        meta["unsent"] = isinstance(exc, urllib.error.URLError) and getattr(reason, "unsent", False) is True
        error = ProviderError(classify_http(url, None, incident=incident), detail=meta["class"], meta=meta)
    else:
        meta["class"] = diagnostics.http_class(meta["status"])
        error = None
        if len(raw) > MAX_BODY:
            error = ProviderError("network", http=meta["status"], detail="oversize", meta=meta,
                                  message="The provider's answer was too large. Your last reading is kept.")
    meta["ms"] = round((time.monotonic() - started) * 1000)
    diagnostics.log("http.request", "info" if error else "debug", host=host, path=_ORG_IN_PATH.sub(r"\1-", parts.path),
                    status=meta["status"], cls=meta["class"], ms=meta["ms"], cf_mitigated=meta["cf_mitigated"],
                    error_type=meta["error_type"], error_description=meta["error_description"],
                    kind=error.kind if error else None)
    if error:
        raise error
    try:
        return json.loads(raw), meta
    except ValueError:
        return None, meta


def head(url, *, timeout=DEFAULT_TIMEOUT):
    """One HEAD request: no body either way, no redirect followed, and nothing sent but the user agent (no cookie,
    no token, no header of the caller's). Returns (status, headers) of whatever answered, a 3xx or 4xx included:
    reading the answer is the caller's. Raises a ProviderError only when nothing answered (no network, a timeout)
    and, in safe mode, SafetyViolation before any socket opens, as `request` does. Logged at debug level only: the
    caller says what the answer meant."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    opener = _opener(_gate(parts.scheme, host, parts.port or (443 if parts.scheme == "https" else 80)))
    outgoing = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    started = time.monotonic()
    try:
        with opener.open(outgoing, timeout=timeout) as response:
            status, headers = response.status, response.headers
    except urllib.error.HTTPError as exc:      # a 3xx (never followed) or an error status: an answer all the same
        status, headers = exc.code, exc.headers
        exc.close()
    except (OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", None)
        failure = "timeout" if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) else "network"
        meta = {"status": None, "class": failure, "ms": round((time.monotonic() - started) * 1000)}
        diagnostics.log("http.head", "debug", host=host, path=parts.path, status=None, cls=failure, ms=meta["ms"])
        raise ProviderError("network", detail=failure, meta=meta) from None
    diagnostics.log("http.head", "debug", host=host, path=parts.path, status=status,
                    cls=diagnostics.http_class(status), ms=round((time.monotonic() - started) * 1000))
    return status, headers


def _opener(test_server):
    """An opener that never follows a redirect (a 3xx surfaces as an HTTPError); a registered test server is
    reached directly, past any proxy."""
    handlers = [_NoRedirect(), _HTTPHandler(), _HTTPSHandler()]
    if test_server:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _gate(scheme, host, port):
    """Refuses what safe mode does not allow, before any socket exists; True for a registered test server."""
    if safe_mode():
        with _test_hosts_lock:
            registered = f"{host}:{port}" in _test_hosts
        if not registered:
            raise SafetyViolation(f"Network access to {host or 'that address'} is blocked in safe mode.")
        return True
    if scheme != "https":
        raise ValueError("Provider requests must use HTTPS.")
    return False


def _http_error(url, exc, meta, token_was_expired, incident):
    try:
        head = exc.read(HEAD_BYTES)
    except (OSError, http.client.HTTPException):
        head = b""
    finally:
        exc.close()
    headers = exc.headers or {}
    meta.update(status=exc.code, **_header_meta(headers))
    meta["class"] = diagnostics.http_class(exc.code)
    meta["error_type"] = error_type(headers, head)
    if url == TOKEN_URL:
        answer = _json_head(head)
        verdict = answer.get("error")
        if verdict in ("invalid_grant", "invalid_client"):
            meta["error_type"] = verdict
        if 400 <= exc.code < 500:
            # Says whether a refused renewal is an expired login, a revoked one or an account on hold.
            meta["error_description"] = token_description(answer.get("error_description"))
    wait = None
    if exc.code == 429:
        meta["retry_after"] = retry_after_seconds(header(headers, "retry-after"))
        wait = rate_limit_wait(meta["retry_after"])
    kind = classify_http(url, exc.code, headers, head, token_was_expired, incident)
    return ProviderError(kind, http=exc.code, retry_after=wait, detail=meta["error_type"], meta=meta)


def _json_head(head):
    """The JSON object an error body starts with, or {} (only read here, never kept)."""
    try:
        answer = json.loads(head)
    except ValueError:
        return {}
    return answer if isinstance(answer, dict) else {}


def _header_meta(headers):
    return {"cf_mitigated": _clean(header(headers, "cf-mitigated"), 20), "server": _clean(header(headers, "server"), 40)}


def _clean(value, limit):
    if not value:
        return None
    text = "".join(ch for ch in str(value) if ch.isprintable())[:limit].strip()
    return text or None


def _split(host_port):
    parts = urllib.parse.urlsplit("//" + str(host_port))
    try:
        port = parts.port
    except ValueError:
        port = None
    if not parts.hostname or not port:
        raise ValueError("A test host is written as 127.0.0.1:<port>.")
    return parts.hostname.lower(), port
