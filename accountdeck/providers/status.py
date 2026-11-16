"""Outage awareness (§6.8, A2): provider status pages plus an outage inferred from our own failed reads."""
from __future__ import annotations

from collections import deque
import copy
import datetime as dt
import threading
import time
import urllib.parse

from .. import diagnostics
from ..config import SafetyViolation
from ..core import DeckError
from . import http as http_module

LEVELS = ("unknown", "ok", "degraded", "outage")       # ascending severity
CLAUDE_STATUS_URL = "https://status.claude.com/api/v2/summary.json"
OPENAI_STATUS_URL = "https://status.openai.com/api/v2/status.json"
CLAUDE_COMPONENTS = ("claude.ai", "Claude API", "Claude Code")
INDICATORS = {"none": "ok", "minor": "degraded", "maintenance": "degraded", "major": "outage", "critical": "outage"}
COMPONENT_LEVELS = {"operational": "ok", "degraded_performance": "degraded", "partial_outage": "degraded",
                    "under_maintenance": "degraded", "major_outage": "outage"}
POLL_OK, POLL_ALERT = 300, 60
FETCH_TIMEOUT = 10
CACHE_TTL = 900               # a failed fetch reuses the last good status for up to 15 minutes
OBSERVED_WINDOW = 600
OBSERVED_GOOD = 360           # a good read this recent rules out an outage (the active account is read every 5 min)
FAILURE_CLASSES = frozenset({"5xx", "529", "timeout", "network"})


def merge_levels(*levels):
    """The most severe of the given levels (None and unrecognised values are ignored)."""
    known = [level for level in levels if level in LEVELS]
    return max(known, key=LEVELS.index) if known else "unknown"


class StatusSource:
    """One provider status page. fetch() never raises: a failure returns the last good status while
    it is at most 15 minutes old, else level "unknown", which never gates anything."""

    def __init__(self, url, *, http=None, clock=time.time):
        self.url, self.clock = url, clock
        self.http = http or http_module.request
        self.last_good = None
        self.level = None             # level of the last fetch() result
        self._lock = threading.Lock()

    def fetch(self):
        now = self.clock()
        try:
            status = self._load()
        except SafetyViolation:
            raise
        except (DeckError, ValueError, TypeError, KeyError, AttributeError) as exc:
            diagnostics.log("status.fetch_failed", "info", host=urllib.parse.urlsplit(self.url).hostname,
                            error_type=type(exc).__name__, kind=getattr(exc, "kind", None))
            with self._lock:
                cached = self.last_good
            if cached is not None and now - cached["checked"] <= CACHE_TTL:
                status = copy.deepcopy(cached)
            else:
                status = self._status("unknown", "Status page unavailable", checked=now)
        else:
            status["checked"] = now
            with self._lock:
                self.last_good = copy.deepcopy(status)
        with self._lock:
            self.level = status["level"]
        return status

    def poll_interval(self):
        """Seconds until the next fetch: 5 minutes, or 60 seconds while the last result is not ok."""
        with self._lock:
            level = self.level
        return POLL_OK if level in (None, "ok") else POLL_ALERT

    def _load(self):
        data, _ = self.http("GET", self.url, timeout=FETCH_TIMEOUT)
        return self.parse(data)

    def parse(self, data):
        raise NotImplementedError

    def _status(self, level, summary, *, since=None, incident=None, checked=None):
        return {"level": level, "summary": summary, "since": since, "url": self._page_url(), "checked": checked,
                "incident": incident}

    def _page_url(self, data=None):
        page = (data or {}).get("page") if isinstance(data, dict) else None
        url = page.get("url") if isinstance(page, dict) else None
        if isinstance(url, str) and url.startswith("https://"):
            return url
        parts = urllib.parse.urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"


class StatuspageStatus(StatusSource):
    """A Statuspage status.json: the overall indicator only (OpenAI; LV-14)."""

    def __init__(self, url=OPENAI_STATUS_URL, **kwargs):
        super().__init__(url, **kwargs)

    def parse(self, data):
        status = data["status"]
        level = INDICATORS.get(status["indicator"])
        if level is None:
            raise ValueError("unknown status indicator")
        result = self._status(level, _text(status.get("description")) or level)
        result["url"] = self._page_url(data)
        return result


class StatuspageSummary(StatuspageStatus):
    """A Statuspage summary.json: indicator, the first unresolved incident and component levels (Claude)."""

    def __init__(self, url=CLAUDE_STATUS_URL, *, components=CLAUDE_COMPONENTS, **kwargs):
        super().__init__(url, **kwargs)
        self.components = components

    def parse(self, data):
        result = super().parse(data)
        for item in data.get("incidents") or []:
            if isinstance(item, dict) and item.get("status") not in ("resolved", "postmortem"):
                started = _timestamp(item.get("started_at") or item.get("created_at"))
                result["incident"] = {"title": _text(item.get("name")) or "Incident", "impact": _text(item.get("impact")),
                                      "status": _text(item.get("status")), "started": started}
                result["since"] = started
                break
        result["components"] = {}
        for item in data.get("components") or []:
            name = _component(item.get("name"), self.components) if isinstance(item, dict) else None
            if name is not None:
                level = COMPONENT_LEVELS.get(item.get("status"), "unknown")
                result["components"][name] = merge_levels(result["components"].get(name), level)
        return result


class ObservedStatus:
    """An outage inferred from our own reads (§6.8): within the last 10 minutes at least 2 distinct
    accounts (all of them, when fewer are enrolled) failed with 5xx, 529, a timeout or a network
    error, no account read successfully after the first of those failures, and none did in the last
    6 minutes: auto-switch reads failing targets more often than the active account (cli_policy `refresh`),
    so a few of them failing between two good reads of it are no outage."""

    def __init__(self, *, window=OBSERVED_WINDOW, clock=time.time):
        self.window, self.clock = window, clock
        self._events = deque()
        self._lock = threading.Lock()

    def record(self, account, http_class, at=None):
        at = self.clock() if at is None else at
        with self._lock:
            self._events.append((at, account, http_class))
            self._prune(at)

    def evaluate(self, now=None, accounts=None):
        """{"level": "outage" | "ok", "since", "accounts"}; `accounts` is how many accounts the
        provider has (None: assume at least two)."""
        now = self.clock() if now is None else now
        with self._lock:
            self._prune(now)
            events = sorted((event for event in self._events if now - self.window <= event[0] <= now),
                            key=lambda event: event[0])
        failures, good = [], None
        for at, account, klass in events:
            if klass == "2xx":
                failures, good = [], at
            elif klass in FAILURE_CLASSES:
                failures.append((at, account))
        failing = {account for _, account in failures}
        needed = 2 if accounts is None else min(2, accounts)
        if needed and len(failing) >= needed and (good is None or now - good >= OBSERVED_GOOD):
            return {"level": "outage", "since": failures[0][0], "accounts": len(failing)}
        return {"level": "ok", "since": None, "accounts": len(failing)}

    def _prune(self, now):
        while self._events and self._events[0][0] < now - self.window:
            self._events.popleft()


def _component(name, wanted):
    """The wanted component a status-page name refers to: the exact name, or the name followed by
    its host, as in "Claude API (api.anthropic.com)"."""
    if not isinstance(name, str):
        return None
    return next((item for item in wanted if name == item or name.startswith(item + " (")), None)


def _text(value, limit=200):
    return str(value)[:limit] if isinstance(value, str) and value else None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        when = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)).timestamp()
