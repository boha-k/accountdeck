"""Provider failure classification (A1): one kind per failure, a stable error code, and the re-auth escalation."""
from __future__ import annotations

from collections import namedtuple
import datetime as dt
import email.utils
import json
import re
import threading
import time
import urllib.parse

from .. import diagnostics
from ..core import DeckError
from .base import OAUTH_HOST

RATE_LIMIT_MARGIN = 900       # added to every Retry-After, so "Retry-After: 0" (or none) waits 15 min
RETRY_AFTER_CAP = 3600        # a longer Retry-After counts as this: one answer never pauses an account for long,
                              # and asking again after it costs one request
BLOCKED_RETRY = 1800
ESCALATION_COUNT = 3
ESCALATION_SPAN = 1800
STALE_SESSION_CODES = (b"session_stale_relogin", b"elevated_auth")
NO_WEEKLY = "Claude's usage answer has no weekly limit. Your last reading is kept."
SHAPE_LOG_EVERY = 3600        # an answer shape without a weekly limit is logged once per route and hour
# Claude Code 2.1.312's fixed token-endpoint error_description values (it records one only on an exact
# match): an expired login, a refresh token the server no longer accepts (revoked or replaced), a token
# without an account or organization, and an account on hold.
TOKEN_DESCRIPTIONS = ("Refresh token expired", "Refresh token not found or invalid",
                      "No organization associated with this token", "No account associated with this token",
                      "account_on_hold")
DESCRIPTION_CHARS = 80
WITHHELD = "(withheld)"

# kind -> (DeckError code, message shown to the user)
KINDS = {
    "token_refused": ("login_needed", "Claude refused this account's saved sign-in. AccountDeck will check again; "
                                      "open the account in Claude if this keeps happening."),
    "token_expired": ("login_needed", "Open this Claude account once to renew its usage access."),
    "blocked": ("blocked", "The usage check was refused by Cloudflare. Your last reading is kept."),
    "scope_missing": ("unsupported", "This saved sign-in cannot read usage. Reconnect this account."),
    "web_session_stale": ("login_needed", "Claude's saved web sign-in has expired. Reconnect this account."),
    "rate_limited": ("rate_limited", "The provider asked us to wait. Your last reading is kept."),
    "outage": ("outage", "The provider reports an incident. Your last reading is kept."),
    "network": ("network", "Could not reach the provider. Your last reading is kept."),
    "unauthorized": ("codex", "Codex refused this saved sign-in for now. It will be checked again."),
    "needs_attention": ("codex", "Codex reported a sign-in problem. It will be checked again."),
    "refresh_rejected": ("reauth_required", "Codex no longer accepts this saved sign-in. Reconnect this account."),
    "identity": ("identity", "The provider returned a different account. Nothing was changed."),
    "codex": ("codex", "Codex could not return this information yet."),
}
# A "network" failure reads differently depending on what actually happened.
_NETWORK_BY_CLASS = {
    "timeout": ("timeout", "The provider took too long to respond. Your last reading is kept."),
    "3xx": ("network", "The provider redirected a credential request. Refresh is paused."),
    "4xx": ("network", "The provider declined this usage check. Your last reading is kept."),
}
_ERROR_TYPE = re.compile(r"[a-z][a-z0-9_]{0,39}")
# What could be a credential, an address or an identifier: a long unbroken run, a long word with a digit,
# key and JWT prefixes, an email or URL, a bearer header or an assignment of a secret-named field.
_TOKEN_LIKE = re.compile(r"\S{24,}|\b(?=\w*\d)\w{12,}|sk-|eyJ|@|://"
                         r"|(?i:bearer|\b(?:token|code|key|secret|password)\s*=)")
_shapes_logged = {}
_shapes_lock = threading.Lock()


class ProviderError(DeckError):
    """A classified provider failure. `kind` is a KINDS key, `http` the status (None without a
    response), `detail` a short sanitized hint (an error type or RPC code, never a body) and `meta`
    the request diagnostics of http.request(). `retry_after` already includes the A1 margin."""

    def __init__(self, kind, http=None, retry_after=None, detail=None, meta=None, *, message=None):
        if kind not in KINDS:
            raise ValueError(f"unknown provider failure kind: {kind}")
        meta = dict(meta or {})
        code, text = KINDS[kind]
        if kind == "network" and meta.get("class") in _NETWORK_BY_CLASS:
            code, text = _NETWORK_BY_CLASS[meta["class"]]
        if retry_after is None:
            retry_after = {"rate_limited": RATE_LIMIT_MARGIN, "blocked": BLOCKED_RETRY}.get(kind)
        super().__init__(message or text, code, retry_after)
        self.kind, self.http, self.detail, self.meta = kind, http, detail, meta

    @property
    def http_class(self):
        return self.meta.get("class") or diagnostics.http_class(self.http)


def classify_http(url, status, headers=None, body_head=b"", token_was_expired=False, incident=False):
    """The A1 kind of one failed request. `status` None means no response (timeout, refused
    connection); `body_head` is at most the first 2 KB of the body and is only inspected here."""
    if status is None or status == 529 or 500 <= status <= 599:
        return "outage" if incident else "network"
    if status == 401:
        return "token_expired" if token_was_expired else "token_refused"
    if status == 403:
        headers, body_head = headers or {}, (body_head or b"")[:2048]
        kind = error_type(headers, body_head)
        if header(headers, "cf-mitigated") or kind == "html":
            return "blocked"
        if any(code in body_head.lower() for code in STALE_SESSION_CODES):
            return "web_session_stale"
        if kind == "permission_error":
            return "scope_missing" if urllib.parse.urlsplit(url).hostname == OAUTH_HOST else "web_session_stale"
        return "network"
    if status == 429:
        return "rate_limited"
    return "network"


def header(headers, name):
    """A header value from a Message or a plain dict, matched case-insensitively."""
    if hasattr(headers, "get_all"):
        return headers.get(name)
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


def error_type(headers, body_head):
    """"html" for an HTML page, the JSON `error.type` (or `type`) when it is a plain identifier,
    else None. Used for classification and logs, so it never carries free text."""
    content_type = (header(headers, "content-type") or "").lower()
    if "text/html" in content_type or body_head.lstrip()[:1] == b"<":
        return "html"
    try:
        data = json.loads(body_head.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    value = error.get("type") if isinstance(error, dict) else data.get("type")
    return value if isinstance(value, str) and _ERROR_TYPE.fullmatch(value) else None


def token_description(value):
    """A token-endpoint `error_description` that may be kept in meta and logged: one of Claude Code's
    fixed strings as it is, else at most DESCRIPTION_CHARS printable characters with nothing token-like
    in them, else WITHHELD; None when the answer has none. It tells an expired login from a revoked one."""
    if not isinstance(value, str) or not value.strip():
        return None
    if value in TOKEN_DESCRIPTIONS:
        return value
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())
    return WITHHELD if _TOKEN_LIKE.search(text) else text[:DESCRIPTION_CHARS]


def no_weekly(answer, meta=None, *, route):
    """The failed read of a Claude usage answer that reports no account-wide weekly limit (S3a): the last
    reading is kept, and the provider, having answered, counts as reachable (class 2xx). The answer's key
    names, never its values, are logged once per shape, route and hour so the next API change shows in
    the log: `keys` carry something, `null_keys` are null, `seven_day_keys` are inside `seven_day`."""
    meta = dict(meta or {})
    meta["class"] = meta.get("class") or "2xx"
    weekly = answer.get("seven_day")
    shape = {"keys": _names(key for key, value in answer.items() if value is not None),
             "null_keys": _names(key for key, value in answer.items() if value is None),
             "seven_day_keys": _names(weekly) if isinstance(weekly, dict) else None}
    signature, now = (route, json.dumps(shape, sort_keys=True)), time.monotonic()
    with _shapes_lock:
        due = now - _shapes_logged.get(signature, now - SHAPE_LOG_EVERY) >= SHAPE_LOG_EVERY
        if due:
            if len(_shapes_logged) >= 64:
                _shapes_logged.clear()
            _shapes_logged[signature] = now
    if due:
        diagnostics.log("usage.no_weekly", "warning", route=route, status=meta.get("status"), **shape)
    return ProviderError("network", http=meta.get("status"), detail="no_weekly", meta=meta, message=NO_WEEKLY)


def _names(keys):
    """Sorted key names for the log: at most 40, each cut at 40 characters."""
    return sorted(str(key)[:40] for key in keys)[:40]


def retry_after_seconds(value, now=None):
    """Seconds requested by a Retry-After header (delta-seconds or HTTP-date); None when absent or invalid."""
    if value is None:
        return None
    value = str(value).strip()
    if value.isascii() and value.isdigit():
        return float(int(value)) if len(value) <= 18 else 1e18      # no float or int() limit is reached
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return max(0.0, when.timestamp() - (time.time() if now is None else now))


def rate_limit_wait(retry_after):
    """The pause after a 429 (A1): the requested seconds, at most RETRY_AFTER_CAP, plus a 15-minute margin."""
    return min(retry_after or 0, RETRY_AFTER_CAP) + RATE_LIMIT_MARGIN


Outcome = namedtuple("Outcome", "at kind route incident")


class OutcomeLog:
    """Recent refresh outcomes of one Claude account: `kind` is "ok" or a failure kind, `route`
    "oauth" or "cookie". The engine persists it with to_list()/from_list() and starts a new log
    when a new profile is saved for the account."""
    LIMIT = 50

    def __init__(self, entries=()):
        self.entries = [Outcome(*entry) for entry in entries][-self.LIMIT:]

    def record(self, kind, *, route, at, incident=False):
        self.entries.append(Outcome(float(at), str(kind), str(route), bool(incident)))
        del self.entries[:-self.LIMIT]

    def to_list(self):
        return [list(entry) for entry in self.entries]

    @classmethod
    def from_list(cls, data):
        """Rebuilds a stored log, dropping malformed entries."""
        entries = []
        for item in data if isinstance(data, list) else ():
            if (isinstance(item, (list, tuple)) and len(item) == 4 and isinstance(item[0], (int, float))
                    and isinstance(item[1], str) and isinstance(item[2], str)):
                entries.append((float(item[0]), item[1], item[2], bool(item[3])))
        return cls(entries)

    def __iter__(self):
        return iter(self.entries)


def reauth_escalation(outcomes, now, *, incident=False):
    """True when a saved Claude sign-in should become reauth_required (A1): at least 3 token_refused
    outcomes since the last success, across both routes, spanning at least 30 minutes. A single 401
    never escalates, refusals recorded during an incident do not count, and nothing escalates while
    an incident is in progress."""
    if incident:
        return False
    refused = []
    for at, kind, _route, during in sorted(outcomes, key=lambda entry: entry[0]):
        if at > now:
            continue
        if kind == "ok":
            refused = []
        elif kind == "token_refused" and not during:
            refused.append(at)
    return len(refused) >= ESCALATION_COUNT and refused[-1] - refused[0] >= ESCALATION_SPAN
