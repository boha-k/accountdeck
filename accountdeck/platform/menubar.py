"""The menu bar item's content and behaviour, free of AppKit (docs/mac/PLAN.md D8, §1.10).

What it shows, from the engine's cached snapshot (pure functions: state + now in, text and pixels out):

- the icon: a ring whose arc is the share left of the active account's binding window, with a dot when a
  reset is minutes away. It is a template image (drawn by macOS in the menu bar's own colour, right in light,
  dark and highlighted menu bars) while all is well or nothing is known (dimmed), and coloured amber or red
  only when the account needs attention;
- the title: Claude Code terminal room left in accounts' worth, "5h 4.2 · wk 5.6" ("wk 5.6" while no account
  reports a 5-hour limit), with the labels in the secondary colour (empty when there are no terminal accounts,
  so only the ring takes room by the notch);
- the tooltip: the active account in words, work in progress, and every terminal account's room;
- the menu: what is happening (active account, next reset, best account, totals), Open Dashboard ⌘O (in the
  app's own window; with ⌥ held, in the browser), one "Switch … to" submenu per provider, Refresh Now ⌘R, a way to turn notifications on when macOS has
  them off, Start at Login, "Update to X…" while a newer release is offered and Check for Updates… where this copy
  checks (updates.py), Quit AccountDeck 2 ⌘Q.

How it behaves (`MenuBar`, on its own thread `ad2-menubar`): it re-reads the snapshot every 5 s, redraws
only what changed, delivers new alerts through the notification sink with `notify.Notifier`'s policy, and
runs menu commands. A quick switch asks the engine for a plan, shows the plan's facts in an alert and asks
for consent before it calls `engine.action("switch", …)`, as the dashboard's switch modal does: the menu
bar never switches without that confirmation. "Update to X…" likewise shows what the update does and starts it
only on Update (`start-update`); a new version's one notification goes out through the same sink. Opening the menu
asks for an update check in the background (`engine.looked`). Every AppKit call goes through the `bar` it is given
(statusbar_mac.AppKitStatusBar, or a fake in tests), so nothing here needs the main thread.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import queue
import re
import struct
import threading
import time
import zlib

from .. import diagnostics, notify, pool, updates
from ..agy_accounts import limit_text
from ..alerts import PROVIDER_NAMES
from ..rules import (HIDDEN_STATES, LOCKED_TEXT, REFILL_KEEP, STRIP_MIN_USED, age_text, countdown_text, js_round, locked,
                     phrase)
from ..core import DeckError

APP_NAME = "AccountDeck 2"
POLL = 5.0                  # seconds between snapshot reads
WAKE_GAP = 60.0             # a wall-clock jump this much past a poll means the Mac slept
OPEN_DEBOUNCE = 1.0         # a double click opens the dashboard once
LOGIN_RECHECK = 60.0        # seconds between start-at-login reads (each one runs launchctl)
ROUNDS = 3                  # consent rounds when the sessions keep changing under the user
IMMINENT = 900              # a reset this close puts the dot on the ring (model.js)
STALE_AFTER = 3600          # a reading older than this dims the ring (model.js ageTier)
STEPS = 24                  # the ring's resolution; redrawn only when this bucket changes
ICON_POINTS = 16            # the ring's size in the menu bar; drawn at 1× and 2× for every display
ICON_SCALES = (1, 2)
TRACK_ALPHA = 0.30
MUTED_ALPHA = 0.45          # a template ring with nothing reliable to show is drawn dimmed
TONES = {"plenty": "plenty", "watch": "watch", "low": "alarm", "critical": "alarm", "out": "alarm", "unknown": "muted"}
# Colours of the attention states (the dashboard's tokens), for light and dark menu bars.
PALETTES = {
    "light": {"watch": (0xA8, 0x73, 0x0A), "alarm": (0xD2, 0x41, 0x5C)},
    "dark": {"watch": (0xF0, 0xB4, 0x29), "alarm": (0xFF, 0x6B, 0x81)},
}
NOTIFICATION_SETTINGS_URL = "x-apple.systempreferences:com.apple.Notifications-Settings.extension"

# Copy (web/js/format.js).
STOP_COPY = {
    "managed": "Claude will close and reopen on its own. If it does not close within 10 seconds it will be "
               "closed by force.",
    "graceful": "Claude will close by itself and reopen.",
}
SESSION_STATUS = {"busy": "working", "idle": "idle", "unknown": "status unknown"}
WINDOW_STATE_COPY = {"resetting": "resetting…", "expected_refill": "expected refill · unconfirmed",
                     "not_started": "not started"}
SAVES_OUTGOING = "Your current account ({label}) isn't saved yet — it will be saved so you can switch back."
RECONFIRM = "{name}'s sessions changed — please confirm again."
EXPIRED = "That confirmation expired — please confirm again."
UNSETTLED = "The sessions kept changing, so nothing was switched. Try again from the dashboard."
NOTICE_IN_DASHBOARD = "Open the dashboard and read the note about Google's terms for Antigravity first."
FINISH_FIRST = "Finish or cancel the current action first."
NOT_READY = "AccountDeck 2 is still starting. Try again in a moment."
LOGIN_ITEMS = ("macOS has AccountDeck 2 switched off in System Settings › General › Login Items. Switch it on "
               "there to open AccountDeck 2 when you log in.")
TEST_TITLE, TEST_MESSAGE = APP_NAME, "Notifications are on. Alerts will look like this."
NOTIFICATIONS_OFF = {
    "denied": "Turn On Notifications…",
    "rejected": "Notifications Unavailable — Reinstall AccountDeck 2",
}

# Menu commands; switch targets are numbered from SWITCH_FIRST.
OPEN, REFRESH, AUTOSTART, QUIT, NOTIFICATIONS, OPEN_IN_BROWSER, UPDATE, CHECK_UPDATES = 1, 2, 3, 4, 5, 6, 7, 8
SWITCH_FIRST = 100


# ---- what the menu bar shows ----

@dataclass(frozen=True)
class Ring:
    """What the icon shows; it is redrawn only when this changes."""
    tone: str       # plenty | watch | alarm | muted
    steps: int      # filled part of the ring, in STEPS
    dot: bool       # a reset within 15 min

    @property
    def template(self) -> bool:
        """Drawn by macOS in the menu bar's colour: everything except the attention states."""
        return self.tone in ("plenty", "muted")


@dataclass(frozen=True)
class MenuItem:
    text: str = ""
    command: int = 0
    enabled: bool = True
    checked: bool = False
    separator: bool = False
    key: str = ""               # key equivalent with ⌘ (shown and active while the menu is open)
    alternate: bool = False     # shown in place of the item above while ⌥ is held (its key gets ⌥⌘)
    submenu: tuple = ()


SEPARATOR = MenuItem(separator=True)


@dataclass(frozen=True)
class Prompt:
    """One alert. `buttons` are (answer, title) in AppKit's order (the first is the rightmost); the answer of
    the pressed button is the result, None meaning "do nothing". Return presses `default`; Escape presses the
    button whose answer is None."""
    title: str
    text: str
    buttons: tuple = ((None, "OK"),)
    default: int = 0
    style: str = "informational"     # informational | warning


def active_account(state):
    """The account the menu bar reports on: the terminals' account first, as the tab icon does (model.js
    activeAccount), else any active one."""
    accounts = state.get("accounts") or ()
    return (next((a for a in accounts if a.get("active") and a.get("provider") == "claude_cli"), None)
            or next((a for a in accounts if a.get("active")), None))


def _stale(account, now) -> bool:
    return account.get("last_good_at") is not None and now - account["last_good_at"] > STALE_AFTER


def _state_at(window, account, now) -> str:
    """The window's state at `now`, which may be later than the snapshot (model.js deriveWindow):
    resets pass, refills expire, readings age."""
    reset, at = window.get("reset"), account.get("last_good_at")
    passed = reset is not None and reset <= now and (at is None or at < reset)
    refilled = window.get("refilled")
    if not window.get("started", True):
        return "not_started"
    if passed:
        return "expected_refill" if _stale(account, now) else "resetting"
    if window.get("severity") == "out":
        return "out"
    if refilled and now - refilled["confirmed"] <= REFILL_KEEP:
        return "refilled"
    return "last_known" if _stale(account, now) else "live"


def _locked(account) -> bool:
    """A window Claude reports locked holds the whole account, whatever its percentage (rules.locks)."""
    return any(locked(w) for w in (*(account.get("windows") or ()), *(account.get("other_windows") or ())))


def _shown(window, account, now) -> bool:
    """Whether the window's percentage still stands at `now`."""
    return window.get("used") is not None and _state_at(window, account, now) not in HIDDEN_STATES


def _target(account, now):
    """The account's binding window, else its fullest shown window, else None (model.js faviconSvg)."""
    shown = [w for w in account.get("windows") or () if _shown(w, account, now)]
    return next((w for w in shown if w.get("binding")), None) or max(shown, key=lambda w: w["used"], default=None)


def _imminent(state, now) -> bool:
    """A reset within 15 min worth announcing, as model.js `soonest` finds it; none of a locked or on-hold
    account (neither has a known end, so no reset refills it: rules.held)."""
    for account in state.get("accounts") or ():
        if _locked(account) or account.get("health") == "on_hold":
            continue
        derived = [(w, _state_at(w, account, now)) for w in account.get("windows") or ()]
        out = any(state_now == "out" for _, state_now in derived)
        for window, state_now in derived:
            reset = window.get("reset")
            if reset is None or not 0 < reset - now <= IMMINENT or state_now in ("not_started", "refilled"):
                continue
            if out or (window.get("used") is not None and window["used"] >= STRIP_MIN_USED):
                return True
    return False


def ring_for(state, now) -> Ring:
    """The arc is the share left of the active account's binding window. An account that is out fills the
    whole ring in the alarm colour; with nothing known (or a failing or old reading) the ring is dimmed."""
    account, tone, steps = active_account(state), "muted", STEPS
    if account is not None and account.get("last_good_at") is not None:
        target = _target(account, now)
        if target is not None:
            tone = TONES.get(target.get("severity"), "muted")
            left = 0.0 if target.get("severity") == "out" else min(1.0, max(0.0, (100 - target["used"]) / 100))
            steps = STEPS if left in (0.0, 1.0) else min(STEPS - 1, max(1, round(left * STEPS)))
        if _locked(account):
            tone, steps = "alarm", STEPS        # out whatever its percentages say (model.js faviconSvg)
        if account.get("error") or _stale(account, now):
            tone = "muted"
    return Ring(tone, steps, _imminent(state, now))


def title_runs(state, now) -> tuple[tuple[str, str], ...]:
    """The title as (text, role) runs, role "label" (secondary colour) or "value": "5h 4.2 · wk 5.6" ("wk 5.6"
    when no counted account reports a 5-hour window), "wk —" when terminal accounts exist but none can be
    counted, and nothing when there are none at all."""
    totals = pool.summarize(state, now)
    if not (totals.capacity or totals.uncounted):
        return ()
    weekly = pool.amount(totals.weekly, totals.weekly_approx) if totals.capacity else "—"
    five = (("5h ", "label"), (pool.amount(totals.five, totals.five_approx), "value"), (" · ", "label")) if totals.has_five else ()
    return five + (("wk ", "label"), (weekly, "value"))


def title_text(state, now) -> str:
    return "".join(text for text, _role in title_runs(state, now))


def _number(window, display) -> str:
    """"38% used" or "62% left", as model.js formatNumber writes it."""
    used = window["used"]
    if display == "left":
        return f"{max(0, 100 - js_round(used))}% left"
    return "100%+ used" if used > 100 else f"{js_round(used)}% used"


def _account_line(account, now, display) -> str:
    label = account.get("label") or ""
    if account.get("last_good_at") is None:
        return f"{label} · waiting for the first reading"
    target = _target(account, now)
    if _locked(account):
        line = f"{label} · {LOCKED_TEXT}"       # no limit used up, so no time to wait for (rules.headline)
    elif target is None:
        windows = account.get("windows") or ()
        lead = next((w for w in windows if w.get("binding")), None) or next(iter(windows), None)
        if lead is None:
            return f"{label} · usage not reported"
        state = WINDOW_STATE_COPY.get(_state_at(lead, account, now), "not reported")
        return f"{label} {lead.get('short') or lead.get('label')} {state}"
    else:
        secs = None if target.get("reset") is None else target["reset"] - now
        name = f"{label} {target.get('short') or target.get('label')}"
        ahead = countdown_text(secs) if secs and secs > 0 else None
        if target.get("severity") == "out":
            line = f"{name} out" + (f" · back in {ahead}" if ahead else "")
        else:
            line = f"{name} {_number(target, display)}" + (f" · resets in {ahead}" if ahead else "")
    if account.get("health") == "on_hold":
        line += " · on hold"                # its error is the hold, not a failed check
    elif _stale(account, now):
        line += f" · as of {age_text(now - account['last_good_at'])} ago"
    elif account.get("error"):
        line += " · last check failed"
    return line


def _active_line(state, now) -> str | None:
    account = active_account(state)
    if account is not None:
        return _account_line(account, now, (state.get("settings") or {}).get("usage_display"))
    if state.get("accounts"):
        return "No account is signed in"
    return "No accounts yet" if state else None


def tooltip(state, now) -> str:
    """AccountDeck 2, the active account in words, work in progress, then every terminal account's room."""
    lines = [APP_NAME]
    active = _active_line(state, now)
    if active:
        lines.append(active)
    pending = state.get("pending") or {}
    if pending.get("message"):
        lines.append(pending["message"])
    totals = pool.summarize(state, now)
    if totals.capacity or totals.uncounted:
        lines += [""] + pool.tooltip_lines(totals, now)
    return "\n".join(lines)


def _summary(account, now, display) -> str:
    """The menu line beside an account: its primary windows, or why it cannot be switched to."""
    if account.get("state") == "needs_signin":
        return "sign-in needed"
    if account.get("health") == "on_hold":
        return "on hold"                    # Claude put it on hold: never switched to, no sign-in helps
    if account.get("last_good_at") is None:
        return "no reading yet"
    if _locked(account):
        return "locked by Claude"
    windows = account.get("windows") or ()
    out = next((w for w in windows if _state_at(w, account, now) == "out"), None)
    if out is not None:
        secs = None if out.get("reset") is None else out["reset"] - now
        return f"out · back in {countdown_text(secs)}" if secs and secs > 0 else "out"
    shown = [w for w in windows
             if _shown(w, account, now) and (w.get("kind") in ("session", "weekly") or w.get("binding"))]
    parts = [f"{w.get('short') or w.get('label')} {_number(w, display)}" for w in shown[:3]]
    return " · ".join(parts) or "usage not reported"


def _status_lines(state, now) -> list[str]:
    """The disabled lines at the top of the menu: the active account, the next reset, the best account now,
    the terminal totals and any work in progress."""
    band, lines = state.get("now_band") or {}, []
    active = _active_line(state, now)
    if active:
        lines.append(active)
    accounts = {a.get("id"): a for a in state.get("accounts") or ()}
    hero = band.get("next_reset") or {}
    if hero.get("kind") == "next_reset" and hero.get("reset") is not None and hero["reset"] > now:
        window = next((w for w in (accounts.get(hero.get("account")) or {}).get("windows") or ()
                       if w.get("id") == hero.get("window")), None)
        named = phrase(window) if window else (hero.get("window_label") or "")
        lines.append(f"Next reset: {hero.get('label')} {named} in {countdown_text(hero['reset'] - now)}")
    elif hero.get("kind") == "refilled" and hero.get("message"):
        lines.append(hero["message"])
    providers = {a.get("provider") for a in accounts.values()}
    for provider, best in (band.get("best") or {}).items():
        if best.get("state") == "ok" and best.get("label") and not best.get("current_is_best"):
            suffix = f" — {PROVIDER_NAMES.get(provider, provider)}" if len(providers) > 1 else ""
            lines.append(f"Best now: {best['label']}{suffix}")
    terminals = _terminals_line(pool.summarize(state, now))
    if terminals:
        lines.append(terminals)
    agy = _antigravity_line(state, now)
    if agy:
        lines.append(agy)
    pending = state.get("pending") or {}
    if pending.get("message"):
        lines.append(pending["message"])
    return lines


def _terminals_line(totals) -> str | None:
    """"Terminals: 5-hour 1.2 of 2 · weekly 5.6 of 6 accounts", each total over the accounts it covers (no 5-hour
    part when no counted account reports a 5-hour window), then who is not counted ("· 1 on hold (Bob)"), or None
    without terminal accounts."""
    named = "".join(f" · {line}" for line in pool.uncounted_lines(totals))
    if not totals.capacity:
        return f"Terminals: none can be counted{named}" if totals.uncounted else None
    accounts = f"{totals.capacity} account" + ("" if totals.capacity == 1 else "s")
    five = f"5-hour {pool.amount(totals.five, totals.five_approx)} of {totals.five_capacity} · " if totals.has_five else ""
    return f"Terminals: {five}weekly {pool.amount(totals.weekly, totals.weekly_approx)} of {accounts}{named}"


def _agy_summary(account, now) -> str:
    """The menu words beside an Antigravity account: its tightest limit (agy_accounts.limit_text), or why there is
    none."""
    if (account.get("error") or {}).get("code") == "reauth_required" or (account.get("saved") and not account.get("switchable")):
        return "sign-in needed"
    return limit_text(account.get("usage"), now) or "no reading yet"


def _antigravity_line(state, now) -> str | None:
    """"Antigravity: Personal — Gemini Models 5-hour 45% left": the account agy is signed in to and its tightest
    limit, once agy is installed or an account is saved; None otherwise."""
    block = (state.get("providers") or {}).get("antigravity") or {}
    accounts = [a for a in block.get("accounts") or () if isinstance(a, dict)]
    if not (block.get("installed") or any(a.get("saved") for a in accounts)):
        return None
    name = PROVIDER_NAMES["antigravity"]
    active = next((a for a in accounts if a.get("active")), None)
    if active is not None:
        return f"{name}: {active.get('label') or ''} — {_agy_summary(active, now)}"
    store = (block.get("store") or {}).get("state")
    return f"{name}: nobody signed in" if store == "absent" else f"{name}: agy's sign-in cannot be read"


def _switch_menus(state, now, busy) -> tuple[list[MenuItem], dict[int, str]]:
    display = (state.get("settings") or {}).get("usage_display")
    items, targets = [], {}
    for provider in ("claude", "codex", "claude_cli"):
        accounts = [a for a in state.get("accounts") or () if a.get("provider") == provider]
        if not accounts:
            continue
        entries = []
        for account in accounts:
            text = f"{account.get('label') or ''} — {_summary(account, now, display)}"
            if account.get("active"):
                entries.insert(0, MenuItem(text, enabled=False, checked=True))
            elif (account.get("switchable") and account.get("saved", True) and not busy
                  and account.get("state") != "needs_signin"):
                command = SWITCH_FIRST + len(targets)
                targets[command] = account["id"]
                entries.append(MenuItem(text, command))
            else:
                entries.append(MenuItem(text, enabled=False))
        if all(account.get("active") for account in accounts):
            entries.append(MenuItem("No other account yet", enabled=False))
        items.append(MenuItem(f"Switch {PROVIDER_NAMES[provider]} To", submenu=tuple(entries)))
    agy = _antigravity_menu(state, now, busy, targets)
    if agy is not None:
        items.append(agy)
    return items, targets


def _antigravity_menu(state, now, busy, targets) -> MenuItem | None:
    """"Switch Antigravity To", from `providers.antigravity` (its accounts are not `state.accounts` rows), once agy is
    installed or an account is saved; None otherwise. A saved account that can be put back is a switch target, as
    the other apps' are: the switch still goes through its plan and the user's confirmation (quick_switch)."""
    block = (state.get("providers") or {}).get("antigravity") or {}
    accounts = [a for a in block.get("accounts") or () if isinstance(a, dict)]
    if not (block.get("installed") or any(a.get("saved") for a in accounts)):
        return None
    # A switch left for Recover, or an Add under way, comes first (the plan would refuse with it anyway).
    busy = busy or bool(block.get("recovery")) or (block.get("enrollment") or {}).get("phase") == "signing_in"
    entries = []
    for account in accounts:
        text = f"{account.get('label') or account.get('email') or ''} — {_agy_summary(account, now)}"
        if account.get("active"):
            entries.insert(0, MenuItem(text, enabled=False, checked=True))
        elif (account.get("switchable") and account.get("saved") and not busy
              and (account.get("error") or {}).get("code") != "reauth_required"):
            command = SWITCH_FIRST + len(targets)
            targets[command] = account["id"]
            entries.append(MenuItem(text, command))
        else:
            entries.append(MenuItem(text, enabled=False))
    if not entries:
        entries.append(MenuItem("No account yet", enabled=False))
    elif all(account.get("active") for account in accounts):
        entries.append(MenuItem("No other account yet", enabled=False))
    return MenuItem(f"Switch {PROVIDER_NAMES['antigravity']} To", submenu=tuple(entries))


def menu_model(state, now, *, start_on_login: bool | None, notifications: str | None = None):
    """(items, switch targets by command), built from the cached snapshot only (opening the menu never
    starts a refresh). Switch items are disabled while any action is in progress. `start_on_login` None
    leaves Start at Login out (no installed app to start); `notifications` is the sink's state, and an
    item to fix it appears when macOS has AccountDeck's notifications off while its alerts are on."""
    busy = bool(state.get("pending"))
    items = [MenuItem(line, enabled=False) for line in _status_lines(state, now)]
    if items:
        items.append(SEPARATOR)
    items += [MenuItem("Open Dashboard", OPEN, key="o"),
              MenuItem("Open Dashboard in Browser", OPEN_IN_BROWSER, key="o", alternate=True), SEPARATOR]
    switches, targets = _switch_menus(state, now, busy)
    if switches:
        items += switches + [SEPARATOR]
    items.append(MenuItem("Refresh Now", REFRESH, key="r"))
    settings = state.get("settings") or {}
    if notifications in NOTIFICATIONS_OFF and settings.get("notifications", True) and settings.get(
            "alerts_enabled", True):
        items.append(MenuItem(NOTIFICATIONS_OFF[notifications], NOTIFICATIONS))
    if start_on_login is not None:
        items.append(MenuItem("Start at Login", AUTOSTART, checked=start_on_login))
    items += _update_items(state.get("update"))
    items += [SEPARATOR, MenuItem(f"Quit {APP_NAME}", QUIT, key="q")]
    return tuple(items), targets


def _update_items(update) -> list[MenuItem]:
    """"Update to X…" while a newer release is offered, and Check for Updates… where this copy checks at all
    (disabled while checking is off or under way)."""
    if not isinstance(update, dict) or update.get("status") in (None, "unavailable"):
        return []
    items = [SEPARATOR]
    if update.get("available") and update.get("latest"):
        items.append(MenuItem(f"Update to {update['latest']}…", UPDATE))
    items.append(MenuItem("Check for Updates…", CHECK_UPDATES,
                          enabled=bool(update.get("enabled")) and not update.get("checking")))
    return items


# ---- consent prompts (the dashboard's switch modal, PLAN §7.8) ----

def _working(plan) -> tuple[list, int]:
    """(the Code sessions that are working or of unknown status, how many there are)."""
    running = plan.get("running") or {}
    sessions = [s for s in running.get("sessions") or () if s.get("status") != "idle"]
    return sessions, max(len(sessions), (running.get("busy") or 0) + (running.get("unknown") or 0))


def _sections(*sections) -> str:
    return "\n\n".join("\n".join(section) for section in sections if section)


def _sessions_line(sessions) -> str:
    """format.js modal.sessions: '2 Code sessions are open: "A" (working) · "B" (idle). Switching will end them.'"""
    names = " · ".join(f'"{s.get("name")}" ({SESSION_STATUS.get(s.get("status"), "status unknown")})'
                       for s in sessions[:4])
    names += f" and {len(sessions) - 4} more" if len(sessions) > 4 else ""
    if len(sessions) == 1:
        return f"1 Code session is open: {names}. Switching will end it."
    return f"{len(sessions)} Code sessions are open: {names}. Switching will end them."


def switch_prompt(plan, note: str | None = None) -> Prompt:
    """The consent alert for a switch plan: the facts of the dashboard's modal and one button per choice.
    Claude: Switch When Idle, Switch Now, Cancel. Codex: wait for its sessions to finish (or switch now when
    none are open), Cancel; ending a Codex session stays in the dashboard, which asks about each one. Antigravity:
    switch when agy closes (or now when none runs), Cancel; agy is never closed for the user."""
    provider, running = plan["provider"], plan.get("running") or {}
    name, target = PROVIDER_NAMES.get(provider, provider), plan["target"]["label"]
    current = (plan.get("current") or {}).get("label")
    running_now = []
    if provider == "claude" and plan.get("stop_tier") in STOP_COPY:
        running_now.append(STOP_COPY[plan["stop_tier"]])
    if provider == "claude" and running.get("sessions"):
        running_now.append(_sessions_line(running["sessions"]))
    if plan.get("invisible"):
        running_now.append(plan["invisible"])
    headroom = plan.get("headroom") or {}
    usage = [f"Now: {current} — {headroom['now']}"] if headroom.get("now") and current else []
    usage += [f"After: {target} — {headroom['after']}"] if headroom.get("after") else []
    effects = []
    if plan.get("will"):
        effects.append("What will happen: " + " · ".join(f"{n} {step}" for n, step in enumerate(plan["will"], 1)))
    if plan.get("account_bound"):
        effects.append("Tied to each account: " + " · ".join(plan["account_bound"]))
    if plan.get("saves_outgoing") and current:
        effects.append(SAVES_OUTGOING.format(label=current))
    # The busy warning repeats the session list above.
    warnings = [w["message"] for w in plan.get("warnings") or () if w.get("code") != "busy" and w.get("message")]
    notes = ["Heads-up:"] + [f"• {message}" for message in warnings] if warnings else []
    notes += [f"Tip: {plan['tip']}"] if plan.get("tip") else []
    title, lead = f"Switch {name} to {target}?", [note] if note else []
    if provider == "claude":
        buttons = (("idle", "Switch When Idle"), ("now", "Switch Now"), (None, "Cancel"))
        default = 1 if plan.get("default_button") == "now" else 0
        return Prompt(title, _sections(lead, running_now, usage, effects, notes), buttons, default)
    if provider == "antigravity":         # agy is never closed for the user: the switch waits until it is
        consumers = running.get("agy_consumers") or []
        buttons = ((("idle", "Switch When agy Closes") if consumers else ("now", "Switch Now")), (None, "Cancel"))
        return Prompt(title, _sections(lead, running_now, usage, effects, notes), buttons)
    consumers = running.get("codex_consumers") or []
    extra = ["To end a Codex session now, use the dashboard."] if consumers else []
    buttons = ((("idle", "Switch When They Finish") if consumers else ("now", "Switch Now")), (None, "Cancel"))
    return Prompt(title, _sections(lead, running_now, usage, effects, notes, extra), buttons)


def interrupt_prompt(plan) -> Prompt:
    """The second confirmation before "Switch Now" ends working sessions (§7.8); Cancel is the default."""
    sessions, count = _working(plan)
    names = [f'"{s.get("name")}"' for s in sessions[:3]]
    if names:
        what = ", ".join(names) + (f" and {count - len(names)} more" if count > len(names) else "")
    else:
        what = f"{count} working Code session" + ("" if count == 1 else "s")
    return Prompt(f"Switch {PROVIDER_NAMES['claude']} now and interrupt {what}?",
                  "Their current work stops when Claude closes.",
                  (("now", "Switch Now Anyway"), (None, "Cancel")), default=1, style="warning")


def blocked_prompt(plan) -> Prompt:
    """Blockers replace the choice (§7.8): their text and one button."""
    blockers = [NOTICE_IN_DASHBOARD if b.get("code") == "notice_required" else b["message"]
                for b in plan.get("blockers") or () if b.get("message")]
    title = f"Switch {PROVIDER_NAMES.get(plan['provider'], plan['provider'])} to {plan['target']['label']}?"
    return Prompt(title, _sections(["This cannot start yet."], blockers))


def message_prompt(text: str) -> Prompt:
    return Prompt(APP_NAME, text)


def update_prompt(update) -> Prompt:
    """The confirmation before an update (the dashboard's Update… shows the same facts): Update, What's New (the
    release page in the browser), Skip This Version or Cancel."""
    version = update["latest"]
    return Prompt(f"Update AccountDeck to {version}?", updates.confirm_text(version, update.get("current")),
                  (("update", "Update"), (None, "Cancel"), ("notes", "What's New"), ("skip", "Skip This Version")))


# ---- the icon ----

def ring_pixels(size: int, ring: Ring, colour: tuple, *, alpha: float = 1.0) -> bytes:
    """The ring as `size`×`size` RGBA pixels (straight alpha, top row first), anti-aliased with 4×4 samples:
    a faint track, the arc clockwise from 12 o'clock and, for an imminent reset, a dot at the top right with
    a clear margin around it. One colour throughout (a template image is drawn black)."""
    samples = 4
    centre = size / 2
    outer = size / 2 - 0.5
    inner = outer - max(1.75, size * 0.16)
    sweep = 2 * math.pi * ring.steps / STEPS
    dot_x, dot_y, dot_r = size * 0.80, size * 0.20, size * 0.17
    margin = dot_r + max(1.0, size * 0.08)
    red, green, blue = colour
    pixels = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            cover = 0.0
            for sy in range(samples):
                for sx in range(samples):
                    px, py = x + (sx + 0.5) / samples, y + (sy + 0.5) / samples
                    if ring.dot and math.hypot(px - dot_x, py - dot_y) <= margin:
                        cover += 1.0 if math.hypot(px - dot_x, py - dot_y) <= dot_r else 0.0
                    elif inner <= math.hypot(px - centre, py - centre) <= outer:
                        angle = math.atan2(px - centre, centre - py) % (2 * math.pi)
                        cover += 1.0 if angle <= sweep else TRACK_ALPHA
            if cover:
                offset = (y * size + x) * 4
                pixels[offset:offset + 4] = bytes((red, green, blue, round(255 * alpha * cover / samples ** 2)))
    return bytes(pixels)


def png(width: int, height: int, rgba: bytes) -> bytes:
    """A PNG (8-bit RGBA, no filter) from straight-alpha RGBA rows, top row first."""
    stride = width * 4
    raw = b"".join(b"\x00" + rgba[row * stride:(row + 1) * stride] for row in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ring_images(ring: Ring, dark: bool) -> tuple[tuple[bytes, int], ...]:
    """((PNG, scale), …) of the ring for every display scale."""
    if ring.template:
        colour, alpha = (0, 0, 0), (MUTED_ALPHA if ring.tone == "muted" else 1.0)
    else:
        colour, alpha = PALETTES["dark" if dark else "light"][ring.tone], 1.0
    images = []
    for scale in ICON_SCALES:
        size = ICON_POINTS * scale
        images.append((png(size, size, ring_pixels(size, ring, colour, alpha=alpha)), scale))
    return tuple(images)


def dashboard_url(data_dir) -> str | None:
    """The dashboard address from the runtime.json run.py writes before it starts the menu bar."""
    try:
        runtime = json.loads((Path(data_dir) / "runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(runtime, dict) or runtime.get("app") != "accountdeck2" or runtime.get("host") != "127.0.0.1":
        return None
    port, token = runtime.get("port"), runtime.get("token")
    if type(port) is not int or not 0 < port < 65536 or not isinstance(token, str) \
            or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return None
    return f"http://127.0.0.1:{port}/#token={token}"


# ---- the behaviour (thread ad2-menubar) ----

class MenuBar:
    """The menu bar's state and behaviour. Everything runs on the `ad2-menubar` thread except `request`,
    `choose`, `request_stop` and `remove_icon`, which any thread may call.

    `bar` is the StatusBar seam: set_title(runs), set_tooltip(text), set_image(images, points, template),
    set_menu(items, choose), alert(prompt) -> index of the pressed button, dark() -> bool, remove(); its
    `on_open`, called as the menu opens, is set to request("look") (statusbar_mac.start). `sink` (or None)
    delivers notifications: state() -> str, accepting() -> bool, show(title, message, key, level).
    `autostart_api` (or None) is the start-at-login backend; `open_url(url)` opens the dashboard (in the app's
    window) or a settings page, `open_browser(url)` the dashboard in the browser; `quit()` ends the app."""

    def __init__(self, engine, bar, *, sink, autostart_api, open_url, quit, open_browser=None):
        self.engine, self.bar, self.sink = engine, bar, sink
        self.autostart, self.open_url, self.quit = autostart_api, open_url, quit
        self.open_browser = open_browser or open_url
        self.clock = getattr(engine, "clock", time.time)
        self.notifier = notify.Notifier("desktop", started=self.clock())
        self.inbox: queue.Queue = queue.Queue()
        self.state = self.rev = None
        self.drawn = self.title = self.tip = self.menu = None
        self.stopping = self.quitting = False
        self.done_at = 0.0          # when the last menu command finished (clicks made before it are dropped)
        self.polled_at = None
        self.last_open = None
        self.login = None           # (start at login or None, when it was read)
        self.logged = set()

    # -- lifecycle --

    def run(self, ready: threading.Event) -> None:
        self.refresh()
        ready.set()
        diagnostics.log("menubar.started", notifications=self._notification_state())
        try:
            self._loop()
        finally:
            self.remove_icon()
            diagnostics.log("menubar.stopped")

    def _loop(self) -> None:
        due = time.monotonic() + POLL
        while not self.stopping:
            try:
                item = self.inbox.get(timeout=max(0.0, due - time.monotonic()))
            except queue.Empty:
                item = None
            if item is not None and not self.stopping:
                try:
                    self._serve(item)
                except Exception as exc:    # one failing command never ends the menu bar
                    self._log_once("menubar.command_failed", exc)
            if not self.stopping and time.monotonic() >= due:
                self.refresh()
                due = time.monotonic() + POLL

    def request(self, what: str) -> None:
        """"test" (a test notification), "open" (the app was opened again), "refresh" or "look" (the menu opened)."""
        self.inbox.put((what,))

    def choose(self, command: int, targets: dict) -> None:
        """A menu click (main thread): handled on the worker with the targets of the menu clicked."""
        self.inbox.put(("choose", command, targets, time.monotonic()))

    def request_stop(self) -> None:
        self.stopping = True
        self.inbox.put(("stop",))

    def remove_icon(self) -> None:
        self.bar.remove()

    def _serve(self, item) -> None:
        kind = item[0]
        if kind == "test":
            self._notify(TEST_TITLE, TEST_MESSAGE, "test", "info")
        elif kind == "open":
            self.open_dashboard()
        elif kind == "refresh":
            self.refresh()
        elif kind == "look":            # the user looks: an update check in the background when the last is old
            looked = getattr(self.engine, "looked", None)
            if callable(looked):
                looked()
        elif kind == "choose":
            _, command, targets, at = item
            if at < self.done_at:       # clicked while the last command (or its dialog) was running
                return
            try:
                self._command(command, targets)
            finally:
                self.done_at = time.monotonic()

    def _command(self, command: int, targets: dict) -> None:
        if command == OPEN:
            self.open_dashboard()
        elif command == OPEN_IN_BROWSER:
            self.open_dashboard(browser=True)
        elif command == REFRESH:
            self._act("refresh", {})
        elif command == AUTOSTART:
            self.toggle_autostart()
        elif command == NOTIFICATIONS:
            self.open_url(NOTIFICATION_SETTINGS_URL)
        elif command == QUIT:
            self.request_quit()
        elif command == UPDATE:
            self.update_now()
        elif command == CHECK_UPDATES:
            self.check_for_updates()
        elif command in targets:
            self.quick_switch(targets[command])

    def _log_once(self, event: str, exc: Exception) -> None:
        if (event, type(exc).__name__) not in self.logged:
            self.logged.add((event, type(exc).__name__))
            diagnostics.log(event, "warning", error_type=type(exc).__name__)

    # -- what is shown --

    def refresh(self) -> None:
        try:
            self._refresh()
        except Exception as exc:        # the next poll tries again
            self._log_once("menubar.refresh_failed", exc)

    def _refresh(self) -> None:
        now = self.clock()
        if self.polled_at is not None and now - self.polled_at > POLL + WAKE_GAP:
            self.notifier.resumed(now)          # the Mac slept: hold notifications as after a start
        self.polled_at = now
        self._read_state()
        state = self.state or {}
        ring = ring_for(state, now)
        dark = bool(self.bar.dark()) and not ring.template      # a template ring follows the menu bar by itself
        if (ring, dark) != self.drawn:
            self.bar.set_image(ring_images(ring, dark), ICON_POINTS, ring.template)
            self.drawn = (ring, dark)
        title = title_runs(state, now)
        if title != self.title:
            self.bar.set_title(title)
            self.title = title
        tip = tooltip(state, now)
        if tip != self.tip:
            self.bar.set_tooltip(tip)
            self.tip = tip
        items, targets = menu_model(state, now, start_on_login=self._starts_at_login(),
                                    notifications=self._notification_state())
        if items != self.menu:
            self.bar.set_menu(items, lambda command, targets=targets: self.choose(command, targets))
            self.menu = items
        if self.sink is not None and state.get("alerts"):
            delivery = self.notifier.poll(state["alerts"], state.get("settings"), now,
                                          accepting=self.sink.accepting())
            if delivery:
                self._notify(delivery.title, delivery.message, delivery.key, delivery.level)
        checker = getattr(self.engine, "updates", None)
        if self.sink is not None and checker is not None and (state.get("update") or {}).get("available"):
            checker.announce(lambda title, text, key: self._notify(title, text, key, "info"),
                             accepting=self.sink.accepting)

    def _read_state(self) -> None:
        try:
            state = self.engine.snapshot(self.rev)
        except Exception as exc:        # a stopping engine or a bad snapshot: keep the last good state
            self._log_once("menubar.snapshot_failed", exc)
            return
        if state is not None:
            self.state, self.rev = state, state.get("rev")

    def _notification_state(self) -> str:
        if self.sink is None:
            return "unavailable"
        try:
            return self.sink.state()
        except Exception:
            return "unavailable"

    def _notify(self, title: str, message: str, key: str, level: str) -> None:
        if self.sink is None:
            return
        try:
            self.sink.show(title, message, key, level)
        except Exception as exc:        # notifications fail quietly; the dashboard lists every alert
            self._log_once("menubar.notify_failed", exc)

    # -- commands --

    def open_dashboard(self, *, browser: bool = False) -> None:
        now = time.monotonic()
        if self.last_open is not None and now - self.last_open < OPEN_DEBOUNCE:
            return
        self.last_open = now
        looked = getattr(self.engine, "looked", None)
        if callable(looked):            # a dashboard window already open only comes forward: no page load says so
            looked()
        url = dashboard_url(self.engine.paths.data_dir)
        if url is None:
            self.tell(NOT_READY)
            return
        (self.open_browser if browser else self.open_url)(url)

    def ask(self, prompt: Prompt):
        """Shows `prompt` and returns the pressed button's answer; None (cancel) once the app is stopping."""
        index = self.bar.alert(prompt)
        if self.stopping or not isinstance(index, int) or not 0 <= index < len(prompt.buttons):
            return None
        return prompt.buttons[index][0]

    def tell(self, text: str) -> None:
        self.ask(message_prompt(text))

    def _act(self, name: str, body: dict) -> None:
        try:
            self.engine.action(name, body)
        except DeckError as exc:
            self.tell(str(exc))

    def _plan(self, account_id: str):
        try:
            return self.engine.action("switch-plan", {"id": account_id})["plan"]
        except DeckError as exc:
            self.tell(str(exc))
            return None

    def quick_switch(self, account_id: str) -> None:
        """switch-plan → consent alert (→ a second confirmation before interrupting working sessions) →
        switch. A changed or expired plan is asked about again with the fresh plan."""
        plan, note = self._plan(account_id), None
        for _ in range(ROUNDS):
            if plan is None or self.stopping:
                return
            if plan.get("blockers"):
                self.ask(blocked_prompt(plan))
                return
            consent = self.ask(switch_prompt(plan, note))
            if consent is None:
                return
            if consent == "now" and plan["provider"] == "claude" and _working(plan)[1] \
                    and self.ask(interrupt_prompt(plan)) is None:
                return
            try:
                self.engine.action("switch", {"id": account_id, "plan_id": plan["plan_id"], "consent": consent,
                                              "remember_idle": False})
            except DeckError as exc:
                if exc.code in ("needs_reconfirm", "confirm_required") and getattr(exc, "plan", None):
                    plan = exc.plan
                    note = RECONFIRM.format(name=PROVIDER_NAMES.get(plan["provider"], plan["provider"]))
                elif exc.code == "stale_plan":
                    plan, note = self._plan(account_id), EXPIRED
                else:
                    self.tell(str(exc))
                    return
            else:
                diagnostics.log("menubar.switch", provider=plan["provider"], consent=consent)
                return
        self.tell(UNSETTLED)

    def update_now(self, update=None) -> None:
        """"Update to X…": the confirmation, then start-update (a Terminal window runs the installer), What's New or
        Skip This Version. A refusal (an action running, the demo) is told, as the dashboard tells it."""
        update = update or (self.state or {}).get("update") or {}
        version = update.get("latest")
        if not update.get("available") or not version:
            self.tell(updates.check_result(update))
            return
        if update.get("blocked"):
            self.tell(update["blocked"])
            return
        answer = self.ask(update_prompt(update))
        if answer == "update":
            self._act("start-update", {"version": version})
        elif answer == "skip":
            self._act("skip-update", {"version": version})
        elif answer == "notes" and update.get("url"):
            self.open_browser(update["url"])
        self.refresh()

    def check_for_updates(self) -> None:
        """Check for Updates…: a check now (up to a few seconds), then what it found; a newer release goes straight
        to its confirmation."""
        try:
            answer = self.engine.action("check-update", {})
        except DeckError as exc:
            self.tell(str(exc))
            return
        update = answer.get("update") or {}
        self.refresh()
        if update.get("available"):
            self.update_now(update)
        else:
            self.tell(updates.check_result(update))

    def request_quit(self) -> None:
        """Quit AccountDeck 2 from the menu: refused while an action runs, like /api/stop."""
        if self.engine.pending:
            self.tell(FINISH_FIRST)
        elif not self.quitting:
            self.quitting = True
            self.quit()

    def _starts_at_login(self, *, fresh: bool = False) -> bool | None:
        """Whether macOS will open AccountDeck 2 at login (read at most every LOGIN_RECHECK s unless `fresh`);
        None when there is no installed app to open."""
        if self.autostart is None:
            return None
        if not fresh and self.login is not None and time.monotonic() - self.login[1] < LOGIN_RECHECK:
            return self.login[0]
        try:
            value = self.autostart.is_enabled()
        except DeckError as exc:
            value = None if exc.code == "unsupported" else bool(
                ((self.state or {}).get("settings") or {}).get("start_on_login"))
        self.login = (value, time.monotonic())
        return value

    def toggle_autostart(self) -> None:
        if self.autostart is None:
            return
        wanted = not self._starts_at_login(fresh=True)
        try:
            status = self.autostart.set_enabled(wanted)
            self.engine.update_settings({"start_on_login": wanted})
        except DeckError as exc:
            self.tell(str(exc))
            return
        finally:
            self.login = None
        if wanted and status.approved is False:
            self.tell(LOGIN_ITEMS)
        self.refresh()


class MenuBarHandle:
    """What run.py and the engine (`attach_tray`) hold on to."""

    def __init__(self, menubar: MenuBar, thread: threading.Thread):
        self._menubar, self._thread = menubar, thread

    @property
    def running(self) -> bool:
        return self._thread.is_alive() and not self._menubar.stopping

    def stop(self, timeout: float = 5.0) -> None:
        self._menubar.request_stop()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout)
        if self._thread.is_alive():
            self._menubar.remove_icon()

    def open_dashboard(self) -> None:
        self._menubar.request("open")

    def notification_state(self) -> str:
        """"authorized", "provisional", "not_determined", "pending", "denied", "rejected" (macOS refused this
        copy's identity: reinstall) or "unavailable" (not running inside the app)."""
        return self._menubar._notification_state()

    def test_notification(self) -> None:
        if not self.running:
            raise DeckError("The menu bar item is not running.", "unsupported")
        state = self.notification_state()
        if state == "denied":
            raise DeckError("Notifications for AccountDeck 2 are off in System Settings › Notifications. "
                            "Turn them on there, then try again.", "unsupported")
        if state in ("rejected", "unavailable"):
            raise DeckError("macOS does not accept notifications from this copy of AccountDeck 2. Run install.sh "
                            "again to repair the app.", "unsupported")
        self._menubar.request("test")
