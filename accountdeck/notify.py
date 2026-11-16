"""Notification delivery policy (PLAN §8.3): which stored alerts reach a sink, and when.

Pure: `Notifier.poll` depends only on its arguments and on what the notifier remembers (the alert
ids it has settled, when its sink last delivered, the start/resume hold). Alerts are the state's
`alerts` list (`Store.alerts()`, newest first) and settings are the state's public settings.

Two sinks read the same alerts: `page` (in-page toasts) and `desktop` (macOS notifications from the menu bar
item, platform/notify_mac.py). A sink
delivers an alert at most once. A disabled sink drops it, and so do quiet hours unless it is critical (the
dashboard still lists it; alerts.py holds a sign-in alert back until they end); the start/resume hold, the
spacing between deliveries and a desktop that is not accepting notifications only defer it, for at most
`EXPIRE` seconds after it was raised.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

from .contract import ALERT_KINDS, LEVELS

HOLD = 120.0      # nothing is delivered for 2 min after start and after a resume
EXPIRE = 300.0    # an alert still waiting after 5 min is old news: only the dashboard keeps it
# One in-page toast a minute (§6.10); desktop notifications at least 20 s apart (§8.2). Alerts that arrive
# while a sink waits for its gap are merged into its next delivery.
GAP = {"page": 60.0, "desktop": 20.0}
# Desktop notifications carry the kinds that are on by default for browser notifications (§7.14).
KINDS = {"page": frozenset(ALERT_KINDS),
         "desktop": frozenset({"reset_confirmed", "limit_reached", "credential_expired", "switch_result", "service"})}
SINKS = tuple(GAP)


@dataclass(frozen=True)
class Delivery:
    sink: str
    key: str                # "kind|account|window" of the newest alert: a newer delivery with the same key replaces it
    ids: tuple[str, ...]    # every alert this delivery covers, newest first
    title: str
    message: str
    level: str              # the most severe level among the covered alerts
    at: float


def in_quiet_hours(hours, local_hour: int) -> bool:
    """Whether `local_hour` falls in the quiet window [start, end), which may wrap past midnight."""
    if not hours:
        return False
    start, end = hours
    if start <= end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end


class Notifier:
    """The delivery policy of one sink. `started` is when this run of AccountDeck began: alerts
    raised earlier were someone else's news and are never delivered."""

    def __init__(self, sink: str, *, started: float, localtime=time.localtime):
        if sink not in GAP:
            raise ValueError(f"Unknown notification sink {sink!r}.")
        self.sink, self.started, self.localtime = sink, started, localtime
        self.hold_until = started + HOLD
        # Delivered or dropped alert ids, with when that happened. An id is forgotten after EXPIRE:
        # by then the alert is too old to be delivered anyway.
        self.settled: dict[str, float] = {}
        self.last: float | None = None      # when this sink last delivered

    def resumed(self, now: float) -> None:
        """A resume from sleep: hold again for `HOLD` seconds."""
        self.hold_until = max(self.hold_until, now + HOLD)

    def poll(self, alerts, settings, now: float, *, accepting: bool = True) -> Delivery | None:
        """The delivery due now, or None. `accepting` is the desktop's answer to "may I notify?" (False
        while macOS has not allowed notifications yet); False defers."""
        settings = settings or {}
        self.settled = {alert_id: at for alert_id, at in self.settled.items() if now - at <= EXPIRE}
        waiting = []
        for alert in alerts or ():
            alert_id = alert.get("id")
            if not isinstance(alert_id, str) or alert_id in self.settled:
                continue
            if self._deliverable(alert, now):
                waiting.append(alert)
            else:
                self.settled[alert_id] = now
        if not waiting:
            return None
        if not self._enabled(settings):
            self._settle(waiting, now)
            return None
        if in_quiet_hours(settings.get("quiet_hours"), self.localtime(now).tm_hour):
            # A critical alert is let through: money being spent, a service that is stuck, the terminals stopped.
            self._settle([alert for alert in waiting if alert.get("level") != "critical"], now)
            waiting = [alert for alert in waiting if alert.get("level") == "critical"]
            if not waiting:
                return None
        if now < self.hold_until or not accepting or (self.last is not None and now - self.last < GAP[self.sink]):
            return None
        self._settle(waiting, now)
        self.last = now
        return _merge(self.sink, waiting, now)

    def _settle(self, alerts, now) -> None:
        self.settled.update((alert["id"], now) for alert in alerts)

    def _deliverable(self, alert, now) -> bool:
        at = alert.get("at")
        return (alert.get("kind") in KINDS[self.sink] and not alert.get("dismissed")
                and alert.get("snoozed_until") is None and isinstance(at, (int, float))
                and self.started <= at and now - at <= EXPIRE)

    def _enabled(self, settings) -> bool:
        return bool(settings.get("alerts_enabled", True)) and (
            self.sink != "desktop" or bool(settings.get("notifications", True)))


def _merge(sink, alerts, now) -> Delivery:
    """One delivery for every waiting alert: the newest one's text, "(+n more)" for the rest (the
    page's `alerts.more` copy)."""
    alerts = sorted(alerts, key=lambda alert: alert["at"], reverse=True)
    newest = alerts[0]
    message = newest.get("message") or ""
    if len(alerts) > 1:
        message = f"{message} (+{len(alerts) - 1} more)".strip()
    level = max((alert.get("level") for alert in alerts), key=lambda lvl: LEVELS.index(lvl) if lvl in LEVELS else 0)
    key = "|".join(str(newest.get(part) or "") for part in ("kind", "account", "window"))
    return Delivery(sink, key, tuple(alert["id"] for alert in alerts), newest.get("title") or "", message,
                    level if level in LEVELS else "info", now)
