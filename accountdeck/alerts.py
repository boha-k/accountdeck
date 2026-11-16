"""Alert engine (PLAN §6.10): turns successive states into alerts, once per episode.

`AlertEngine.evaluate(prev_state, state, latches, now)` is pure apart from the start/resume hold it
keeps itself: the first observation after start only primes the latches (nothing is raised from
startup, errors or accounts whose reading is ambiguous), and nothing is raised for two minutes after
start or after a resume from sleep. A terminal login's end is the exception: it waits out that hold
instead of being primed by it, as an end that came closer while the Mac slept is still news. So do the
terminal pool's run-out, usage credits being spent and a changed usage report (`runway`, `spent`, `on_credits`,
`usage_report`). Latches
are plain JSON, persisted by the engine in the internal `alert_latches` setting so a restart does not
repeat an alert. Alert text names accounts and windows only, never a token, an identity or a raw error.
"""
from __future__ import annotations

import datetime as dt
import math

from . import rules
from .cli_policy import considered, same_window
from .core import is_frozen
from .notify import in_quiet_hours
from .rules import _WEEKDAYS, STRIP_MIN_USED, _is_num, clock_text, countdown_text
from .usage import title

HOLD = 120.0                  # quiet period after start and after a resume
REARM = 5                     # the low latch re-arms this many points above the threshold
SOON_USED = 80.0              # reset_soon: windows at least this full
USE_IT_LEFT = 30.0            # use_it: weekly windows with at least this much left ...
USE_IT_WITHIN = 6 * 3600      # ... that reset within this time
KEEP = 8 * 86400              # latches of past resets and cycles are forgotten after this long
LOGIN_AHEAD = 3 * 86400       # a terminal login's end is announced this long before it, like Claude Code does
LOGIN_LAST = 86400            # ... and again this long before it, on its last day
LOGIN_SOON = 7 * 86400        # `signin_due` lists a login from this long before its end, and `together` speaks up
# Terminal logins ending within TOGETHER of each other: from TOGETHER_LOGINS of them, with at most TOGETHER_OTHERS
# others left to take over, one heads-up, not more than once per TOGETHER_AGAIN.
TOGETHER, TOGETHER_LOGINS, TOGETHER_OTHERS, TOGETHER_AGAIN = 72 * 3600, 3, 2, 14 * 86400
SAME_LOGIN = 86400            # ends this close belong to one login (each renewal may move it by seconds)
# A confident forecast of the terminal pool running dry this soon, with this long to wait for a refill,
# raises the runway alert. Each pair's second figure is how far the forecast must recover before it is
# raised again, so it does not flap around the first.
RUNWAY_SOON, RUNWAY_CLEAR = 24 * 3600, 36 * 3600
RUNWAY_CRITICAL, RUNWAY_REARM = 3 * 3600, 6 * 3600
RUNWAY_GAP, RUNWAY_GAP_CLEAR = 3600, 1800
CREDITS_EVERY = 3600          # at most one "spent" alert per account this often while the amount keeps rising
CREDITS_CRITICAL = 50.0       # ... critical from this share of its limit
# These are limit_reached alerts (the kind notifications carry), told apart by their `window`.
RUNWAY_WINDOW, SPENT_WINDOW, OUT_OF_PLAN_WINDOW = "runway", "credits", "credits_only"
CLAUDE_CODE_WINDOW = "claude_code"
USAGE_REPORT_WINDOW = "usage_report"    # the shape watch's alerts (`usage_report`)
CURRENCIES = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "BRL": "R$", "CAD": "CA$", "AUD": "A$", "NZD": "NZ$",
              "SGD": "S$"}     # as Claude Code shows them; any other currency by its code
PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex", "claude_cli": "Claude Code (terminal)", "antigravity": "Antigravity"}
LEVELS = {"low": "warning", "limit_reached": "critical", "reset_confirmed": "good", "reset_soon": "info",
          "suggest_switch": "info", "credential_expired": "warning", "switch_result": "good", "use_it": "info",
          "service": "warning"}
# Every latch maps a key to the time it was set, except login. low and limit are keyed "account|window"
# and last as long as the episode, cred by account; the others carry the reset or cycle they belong to.
# login is keyed "account|soon" / "account|last" / "account|ended" and maps to the login end it was raised for;
# together has one key, "told", the time of the last heads-up. runway is
# keyed "warning" / "critical" and maps to the refill the alert named (0 for none), credits by account to
# the amount last announced (a baseline: `spent`), credits_told to when; on_credits by account, the episode.
# report holds "down" for as long as Claude's answers do not tell the limits, and "kind|<kind>" for good.
GROUPS = ("low", "limit", "refill", "soon", "suggest", "use_it", "cred", "login", "runway", "credits", "credits_told",
          "on_credits", "report", "together")
CYCLE_GROUPS = ("refill", "soon", "suggest", "use_it")


def _alert(kind, account, window, title, message, now, level=None):
    return {"kind": kind, "level": level or LEVELS[kind], "account": account, "window": window, "title": title,
            "message": message, "at": now}


def _accounts(state):
    """The saved accounts of a state by id: the unsaved signed-in row is not an account to alert about."""
    return {a["id"]: a for a in (state or {}).get("accounts") or () if a.get("saved", True)}


def _latches(stored):
    stored = stored if isinstance(stored, dict) else {}
    return {group: {str(key): at for key, at in stored[group].items()
                    if isinstance(at, (int, float)) and not isinstance(at, bool)}
            if isinstance(stored.get(group), dict) else {} for group in GROUPS}


def _float(text):
    """The reset a cycle latch's key ends with (`_Run.cycle`), or None."""
    try:
        return float(text)
    except ValueError:
        return None


def money(minor, credit, fit=False):
    """An amount of a Usage credits row, which is in minor units of its currency (usage._claude_credits), as
    Claude Code shows it: "$3.20", and with `fit` "$100" when it is whole."""
    currency = str(credit.get("currency") or "USD").upper()
    exponent = credit.get("exponent")
    exponent = exponent if type(exponent) is int and 0 <= exponent <= 6 else 2
    value = minor / 10 ** exponent
    return f"{CURRENCIES.get(currency, currency + ' ')}{value:,.{0 if fit and value == int(value) else exponent}f}"


def _credit(account):
    """A Claude account's Usage credits row, or None."""
    if account["provider"] not in ("claude", "claude_cli"):
        return None
    return next((c for c in account.get("credits") or () if c.get("label") == "Usage credits"), None)


def _spends(credit):
    """True when a Usage credits row can still pay: turned on, not stopped by the answer (`spend_limit_reached`,
    a `disabled_reason`, should usage._claude_credits carry them) and under its monthly limit (its percentage
    under 100 without one). Once the limit is spent, nothing more is: the terminals are simply out."""
    if not credit or not credit.get("enabled") or credit.get("spend_limit_reached") or credit.get("disabled_reason"):
        return False
    used, limit, percent = credit.get("used"), credit.get("limit"), credit.get("percent")
    if _is_num(used) and _is_num(limit):
        return used < limit
    return not (_is_num(percent) and percent >= 100)


def _payers(accounts):
    """(account, its Usage credits row) per person: a terminal and a Desktop row of one login report the same
    money, read at different times, so one alert speaks for both: the terminal row's name and id (they
    stay the same whichever row was read last) with the higher amount, the newer one within a month. A row
    read over CREDITS_EVERY before the other has no say: a login no longer read would hold last month's
    amount over this month's."""
    people = {}
    for account in accounts:
        credit = _credit(account)
        if credit is not None and _is_num(credit.get("used")):
            people.setdefault((account.get("email") or account["id"]).casefold(), []).append((account, credit))
    for rows in people.values():
        newest = max(account["last_good_at"] or 0 for account, _ in rows)
        yield (min(rows, key=lambda row: row[0]["provider"] != "claude_cli")[0],
               max((credit for account, credit in rows if newest - (account["last_good_at"] or 0) <= CREDITS_EVERY),
                   key=lambda credit: credit["used"]))


def _around(ts, minutes, tz, now):
    """"Tue 3 PM", "Wed 5:30 PM": a forecast's time to the nearest `minutes`, always with its day, as an alert
    may be read long after it was raised; with the date from 6 days after `now` on ("Sun Nov 8, 11 AM"), where
    the day alone could be read as this week's."""
    rounded = round(ts / (minutes * 60)) * minutes * 60
    moment = dt.datetime.fromtimestamp(rounded, tz) if tz is not None else dt.datetime.fromtimestamp(rounded).astimezone()
    clock = f"{moment.hour % 12 or 12}{f':{moment.minute:02d}' if moment.minute else ''} {'AM' if moment.hour < 12 else 'PM'}"
    day = _WEEKDAYS[moment.weekday()]
    return f"{day} {moment:%b} {moment.day}, {clock}" if rounded - now >= 6 * 86400 else f"{day} {clock}"


def _days(first, last, tz):
    """"Nov 25-28", "Nov 30-Dec 2": the days of a span of time."""
    start, end = (dt.datetime.fromtimestamp(ts, tz) if tz is not None else dt.datetime.fromtimestamp(ts).astimezone()
                  for ts in (first, last))
    text = f"{start:%b} {start.day}"
    if start.date() == end.date():
        return text
    return f"{text}-{end.day}" if start.month == end.month else f"{text}-{end:%b} {end.day}"


SIGNIN_ORDER = {"needs_signin": 0, "ended": 1, "soon": 2}


def signin_due(accounts, now):
    """`providers.claude_cli.signin_due`: the terminal logins a sign-in would help, most urgent first: "needs_signin"
    (no working login, `ends_at` as far as known), "ended" and "soon" (within LOGIN_SOON). Not an account left out,
    on hold (signing in again does not help) or owned by another computer. Terminals that lost a login while a
    newer one is saved for it show "ok" (Engine._raw_account): that one is on its way in, no sign-in is due."""
    due = []
    for account in accounts:
        if (account["provider"] != "claude_cli" or not account.get("saved", True) or account.get("excluded")
                or account.get("health") == "on_hold" or is_frozen(account)):
            continue
        ends = account.get("login_ends_at")
        ends = ends if _is_num(ends) else None
        if account.get("health") == "needs_signin" or account["credential"]["state"] == "reauth_required":
            state = "needs_signin"
        elif ends is None or ends - now > LOGIN_SOON:
            continue
        else:
            state = "ended" if ends <= now else "soon"
        due.append({"id": account["id"], "label": account["label"], "ends_at": ends, "state": state})
    due.sort(key=lambda row: (SIGNIN_ORDER[row["state"]], row["ends_at"] is None, row["ends_at"] or 0, row["label"]))
    return due


class _Run:
    """One evaluation: the latches it updates, the alerts it raises and the thresholds it applies."""

    def __init__(self, latches, now, quiet, tz, low_left, reset_minutes, logins=None, stranded=False, settings=None):
        self.latches, self.now, self.quiet, self.tz = latches, now, quiet, tz
        self.low_left, self.reset_minutes = low_left, reset_minutes
        self.logins = logins or {}
        self.stranded, self.settings = stranded, settings or {}
        self.alerts = []

    def arm(self, group, key):
        """Latches `key` and says whether it was new. Latches are set while quiet too: that is what
        keeps a condition that already holds at start from being announced later."""
        if key in self.latches[group]:
            return False
        self.latches[group][key] = self.now
        return True

    def cycle(self, group, account, window, reset):
        """`arm` for one cycle of a window, keyed "account|window|reset" with the reset in whole minutes. Claude's
        reset times drift between readings (every answer, Claude Code's cached ones too, carries new fractions of a
        second), so a latch of the same window whose reset is within cli_policy's SAME_RESET of this one is this
        cycle, also one stored with its reset unrounded: else each reading would raise the alert again."""
        head = f"{account['id']}|{'' if window is None else window['id']}|"
        for key in self.latches[group]:
            if key.startswith(head) and same_window(_float(key[len(head):]), reset):
                return False
        return self.arm(group, head + ("" if reset is None else str(round(reset / 60) * 60)))

    def raise_alert(self, kind, account, window, title, message, level=None):
        if not self.quiet:
            self.alerts.append(_alert(kind, account["id"], None if window is None else window["id"], title, message,
                                      self.now, level))

    def quiet_hours(self):
        """True inside the quiet hours (the state's settings, local time), which drop all but critical notifications
        (notify.Notifier): an alert that asks for a sign-in waits for their end instead, unlatched, so it is still
        delivered."""
        hours = self.settings.get("quiet_hours")
        return bool(hours) and in_quiet_hours(hours, dt.datetime.fromtimestamp(self.now, self.tz).hour)

    def account(self, account, old, best):
        self.credential(account)
        self.login(account)
        self.on_credits(account)           # before `limit`, which it speaks for
        reading_ok = account["last_good_at"] is not None and account["error"] is None
        for window in account["windows"] + (account.get("other_windows") or []):
            self.refilled(account, window)
            if not (reading_ok and window["primary"]):
                continue
            if window["used"] is None:
                if account["active"] and rules.locked(window):
                    self.limit(account, window, best)      # a lock is news without a percentage too
                continue
            self.soon(account, window)
            self.use_it(account, window)
            if account["active"]:
                self.low(account, window, old)
                self.limit(account, window, best)
        if account["active"]:
            self.suggest(account, best)

    def credential(self, account):
        """credential_expired once per failure episode; only a fresh successful reading re-arms it, so a
        network error, a rate limit, a block or an outage neither raises nor clears it. One found in the quiet hours
        waits for their end unlatched, like a login's end (`login`), which it often follows; its "<id>|wait" mark
        makes it wait out a quiet period too (the Mac asleep at the end of the quiet hours, or a restart), which
        would otherwise latch it unsaid."""
        latch, wait = self.latches["cred"], f"{account['id']}|wait"
        if account["credential"]["state"] == "reauth_required":
            if (self.quiet_hours() and not self.quiet) or (self.quiet and wait in latch):
                latch.setdefault(wait, self.now)
                return
            latch.pop(wait, None)
            # Not again for a login whose end was just announced: that alert asked for the sign-in.
            if self.arm("cred", account["id"]) and not self.told(account["id"], "ended"):
                self.raise_alert("credential_expired", account, None, "Sign-in needs renewing",
                                 f"{account['label']}'s sign-in needs renewing.")
        else:
            latch.pop(wait, None)
            if account["id"] in latch and (account["last_good_at"] or 0) > latch[account["id"]]:
                del latch[account["id"]]

    def login(self, account):
        """A terminal login's end (`logins`, from Claude Code's refreshTokenExpiresAt), as a
        credential_expired alert: once LOGIN_AHEAD before it, once more on its last day and once when it has
        passed, per login. These wait out the quiet period and the quiet hours rather than being latched by them;
        a login that already needs sign-in has its own alert. Nothing is said for an account left out, nor for one
        on hold (signing in again does not help)."""
        ends = self.logins.get(account["id"])
        if (not _is_num(ends) or self.quiet or account["credential"]["state"] == "reauth_required"
                or account.get("excluded") or account.get("health") == "on_hold" or self.quiet_hours()):
            return
        left = ends - self.now
        stage = "ended" if left <= 0 else "last" if left <= LOGIN_LAST else "soon"
        if left > LOGIN_AHEAD or self.told(account["id"], stage) or (stage == "ended" and account["id"] in self.latches["cred"]):
            return
        self.latches["login"][f"{account['id']}|{stage}"] = ends
        label = account["label"]
        if stage == "ended":
            self.raise_alert("credential_expired", account, None, f"{label}'s login has ended",
                             f"Use Sign In Again on {label} in AccountDeck to keep using it.")
        else:
            days = math.ceil(left / 86400)
            # Days in words, as the heads-up and the earlier stage say them; hours and minutes under a day, short of
            # its last minute (countdown_text rounds minutes up: it would say "1d").
            when = countdown_text(left) if left <= 86400 - 60 else "1 day" if days == 1 else f"{days} days"
            self.raise_alert("credential_expired", account, None, f"{label}'s login ends in {when}",
                             f"This is its last day. Use Sign In Again on {label} in AccountDeck." if stage == "last"
                             else f"Use Sign In Again on {label} in AccountDeck before then.")

    def together(self, accounts):
        """Terminal logins that end together (`logins`) with few others to take over: ending one by one, they would
        leave auto-switch nowhere to go. One heads-up from LOGIN_SOON before the first of them ends, naming them. It
        waits out the quiet period and the quiet hours; a login that needs sign-in, or left out or on hold, counts
        neither way."""
        usable = [account for account in accounts if account["provider"] == "claude_cli" and not account.get("excluded")
                  and account.get("health") == "ok" and account["credential"]["state"] != "reauth_required"
                  and not is_frozen(account)]
        ends = sorted((end, account["label"]) for account in usable
                      if _is_num(end := self.logins.get(account["id"])) and end > self.now)
        soon = [end for end, _ in ends if end - self.now <= LOGIN_SOON]        # a prefix of `ends`
        group = max(([row for row in ends[index:] if row[0] - first <= TOGETHER] for index, first in enumerate(soon)),
                    key=len, default=())
        if (self.quiet or self.quiet_hours() or len(group) < TOGETHER_LOGINS
                or self.now - self.latches["together"].get("told", -math.inf) < TOGETHER_AGAIN):
            return
        start, last = group[0][0], group[-1][0]
        others = [account for account in usable if not _is_num(end := self.logins.get(account["id"])) or end > last]
        if len(others) <= TOGETHER_OTHERS:
            self.latches["together"]["told"] = self.now
            when = f"{len(group)} logins end {_days(start, last, self.tz)}"
            names = ", ".join(sorted((label for _, label in group), key=str.casefold))
            self.alerts.append(_alert("credential_expired", None, None, when,
                                      f"{when} ({names}): sign in again in the next week.", self.now))

    def told(self, key, stage):
        """True when the `stage` alert of the account's current login was raised already."""
        ends, at = self.logins.get(key), self.latches["login"].get(f"{key}|{stage}")
        return _is_num(ends) and at is not None and abs(at - ends) < SAME_LOGIN

    def refilled(self, account, window):
        refill = window["refilled"]
        if window["state"] != "refilled" or not refill or rules.held(account):      # no refill brings it back
            return
        if self.cycle("refill", account, window, refill["at"]):
            before = refill["from"]
            if before is None or before >= STRIP_MIN_USED:       # a window that was all but empty is no news
                self.raise_alert("reset_confirmed", account, window,
                                 f"{account['label']}'s {rules.phrase(window)} limit refilled",
                                 f"{account['label']} is ready again.")

    def soon(self, account, window):
        if window["reset_state"] != "scheduled" or window["used"] < SOON_USED or rules.held(account):
            return
        secs = window["reset"] - self.now
        if secs <= self.reset_minutes * 60 and self.cycle("soon", account, window, window["reset"]):
            self.raise_alert("reset_soon", account, window, f"{account['label']}'s {rules.phrase(window)} limit resets soon",
                             f"{account['label']}'s {rules.phrase(window)} limit resets in {countdown_text(secs)}.")

    def use_it(self, account, window):
        """A weekly quota about to expire unused on an account you are not on (a stale reading only
        bounds `left` from above, so it never counts)."""
        if (window["kind"] != "weekly" or account["active"] or not account["switchable"]
                or account["age_tier"] == "stale" or rules.account_out(account) or window["left"] < USE_IT_LEFT
                or window["reset_state"] != "scheduled"):
            return
        secs = window["reset"] - self.now
        if secs <= USE_IT_WITHIN and self.cycle("use_it", account, window, window["reset"]):
            self.raise_alert("use_it", account, window, f"Use {account['label']}'s weekly quota",
                             f"{account['label']}'s weekly limit resets in {countdown_text(secs)} with "
                             f"{rules.pct_left(window['left'])}% unused.")

    def low(self, account, window, old):
        """The active account's window crossed the threshold downward; it re-arms 5 points above it."""
        key = f"{account['id']}|{window['id']}"
        left = window["left"]
        if left >= self.low_left + REARM:
            self.latches["low"].pop(key, None)
            return
        if left >= self.low_left or window["severity"] == "out":
            return
        if not self.arm("low", key):
            return
        previous = next((w for w in (old or {}).get("windows") or () if w["id"] == window["id"]), None)
        if previous is None or previous["left"] is None or previous["left"] < self.low_left:
            return      # no crossing was observed: a first reading, or the window was low already
        tail = "" if window["reset"] is None or window["reset"] <= self.now \
            else f", resets in {countdown_text(window['reset'] - self.now)}"
        self.raise_alert("low", account, window, f"{account['label']}'s {rules.phrase(window)} limit is running low",
                         f"{account['label']} is running low: {rules.pct_left(left)}% left{tail}.")

    def limit(self, account, window, best):
        """limit_reached once per episode: it ends when the window is no longer out. A window Claude locked
        says so (it is no quota used up, so it names no time to wait for) and lasts until a reading clears it."""
        key = f"{account['id']}|{window['id']}"
        locked = rules.locked(window)
        if window["state"] != "out" and not locked:
            self.latches["limit"].pop(key, None)
            return
        if not self.arm("limit", key) or account["id"] in self.latches["on_credits"]:
            return
        other = best.get("label") if best.get("state") == "ok" and best.get("account") != account["id"] else None
        if locked:
            title = f"{account['label']}'s {rules.phrase(window)} limit is locked by Claude"
            message = f"{title}."
        else:
            until = "" if window["reset"] is None or window["reset"] <= self.now \
                else f" until {clock_text(window['reset'], self.now, self.tz)}"
            title = f"{account['label']} is out of {rules.phrase(window)} quota"
            message = f"{title}{until}."
        if other:
            message += f" Best alternative: {other}."
        self.raise_alert("limit_reached", account, window, title, message)

    def suggest(self, account, best):
        """suggest_switch once per (active account, binding window cycle). Snoozing the alert only
        hides it (Store.update_alert); the cycle latch keeps it from being raised again. None for the terminals
        while auto-switch is on (as cli_policy reads the setting): it moves them itself."""
        if (not best.get("suggest_switch") or not best.get("account")
                or (account["provider"] == "claude_cli" and self.settings.get("cli_autoswitch", True))):
            return
        limiting = next((w for w in account["windows"] if w["binding"]), None)
        if self.cycle("suggest", account, limiting, limiting["reset"] if limiting else None):
            self.raise_alert("suggest_switch", account, limiting, f"{best['label']} has more room",
                             f"{best['label']} has {rules.pct_left(best['headroom'])}% left; "
                             f"{account['label']} is running low.")

    def on_credits(self, account):
        """The terminals' account is past a limit, its usage credits can still pay (`_spends`) and no other account
        can take over (`stranded`): real money from here on. One critical alert per episode, which ends when the
        account is back under the limit, no longer in use or its credits can pay no more (a pool that changes its
        mind does not end it). It says what `limit` would, so `limit` stays silent for this account meanwhile."""
        latch, credit = self.latches["on_credits"], _credit(account)
        out = account["active"] and account["provider"] == "claude_cli" and any(
            used is not None and used >= 100 for _, used, _ in considered(account, self.settings, self.now))
        if not (out and _spends(credit)):
            if latch.pop(account["id"], None) is not None and out:
                # Its credits can pay no more: the terminals stop, and `limit` says so after all.
                for key in [key for key in self.latches["limit"] if key.split("|")[0] == account["id"]]:
                    del self.latches["limit"][key]
        elif self.stranded and not self.quiet and account["id"] not in latch:
            latch[account["id"]] = self.now
            label = account["label"]
            self.alerts.append(_alert("limit_reached", account["id"], OUT_OF_PLAN_WINDOW, f"{label} now spends usage credits",
                                      f"{label} is out of plan usage and now spends usage credits; no other account "
                                      "can take over.", self.now))

    def spent(self, accounts):
        """Usage credits are real money at API prices: the amount rising is news at once, also when it rose
        while the service was stopped or the Mac slept (it waits out the quiet period), at most every
        CREDITS_EVERY while it keeps rising. The last amount announced stays the baseline until the next
        alert, so a rise that came too soon for one is still announced. A first reading is a baseline and a
        lower amount a new month: neither is an event. Credit fields never gate a switch (cli_policy)."""
        latch, told = self.latches["credits"], self.latches["credits_told"]
        for account, credit in _payers(accounts):
            key, used, limit = account["id"], credit["used"], credit.get("limit")
            base = latch.get(key)
            if base is None or used < base:
                latch[key] = used
            elif (not self.quiet and money(used, credit) != money(base, credit)
                  and self.now - told.get(key, -math.inf) >= CREDITS_EVERY):
                latch[key], told[key] = used, self.now
                share = used / limit * 100 if _is_num(limit) and limit > 0 else credit.get("percent")
                level = "critical" if _is_num(share) and share >= CREDITS_CRITICAL else "warning"
                amount = money(used, credit)
                tail = f" ({amount} of {money(limit, credit, fit=True)})" if _is_num(limit) else ""
                self.alerts.append(_alert("limit_reached", key, SPENT_WINDOW, f"{account['label']} is spending usage credits",
                                          f"{account['label']} spent {amount} of usage credits this month{tail}.",
                                          self.now, level))

    def runway(self, forecast):
        """The terminal pool's run-out (`runway.forecast`), once per event and once more as critical when it comes
        close. An event lasts until the forecast recovers or the refill its alert named has come; an idle
        night (confidence low) says nothing either way, and the morning's forecast is still the same event.
        Once the pool has run dry (`stranded`) auto-switch's "All terminal accounts are low" says so."""
        latch = self.latches["runway"]
        for key, refill in list(latch.items()):
            if 0 < refill <= self.now:
                del latch[key]
        if not isinstance(forecast, dict) or forecast.get("confidence") != "ok":
            return
        out, gap, refill = forecast.get("runs_out_at"), forecast.get("gap_s"), forecast.get("refill_after_out_at")
        away = math.inf if out is None else out - self.now
        if away > RUNWAY_CLEAR or (gap is not None and gap < RUNWAY_GAP_CLEAR):
            latch.clear()
            return
        if away > RUNWAY_REARM:
            latch.pop("critical", None)
        level = "critical" if away < RUNWAY_CRITICAL else "warning"
        if (level in latch or self.quiet or self.stranded or away >= RUNWAY_SOON
                or (gap is not None and gap <= RUNWAY_GAP)):
            return
        latch[level] = latch["warning"] = refill or 0
        when = _around(out, 60, self.tz, self.now)
        next_refill = "No refill is known after that." if refill is None else (
            f"The next refill is {_around(refill, 5, self.tz, self.now)} ({forecast.get('refill_label')}).")
        self.alerts.append(_alert("limit_reached", None, RUNWAY_WINDOW, f"Terminal accounts run out around {when}",
                                  f"At this pace your terminal accounts run out around {when}. {next_refill}",
                                  self.now, level))

    def usage_report(self, report):
        """The next change to Claude's usage report must not pass in silence (`usage.report_status`): one critical
        alert while its answers do not tell the limits, which the engine dismisses when readings return, and one
        warning per new limits[] kind. Both wait out the quiet period: a change during a sleep is still news. The
        critical one names no cause: a changed report, an outage answering with no JSON and lapsed plans look alike."""
        latch, report = self.latches["report"], report or {}
        if not report.get("down"):
            latch.pop("down", None)
        elif "down" not in latch and not self.quiet:
            latch["down"] = self.now
            count = report.get("accounts") if _is_num(report.get("accounts")) and report["accounts"] >= 1 else None
            whose = "" if count is None else f" for {count} account" + ("" if count == 1 else "s")
            self.alerts.append(_alert("limit_reached", None, USAGE_REPORT_WINDOW, "AccountDeck can't read Claude's limits",
                                      f"Claude's usage answers{whose} have no weekly limit AccountDeck can read, so it can't "
                                      "switch ahead of a limit. A terminal that hits one still moves to another account, and "
                                      "sign-ins and manual switches still work. The last readings are kept.", self.now))
        for kind in report.get("kinds") or ():
            if f"kind|{kind}" not in latch and not self.quiet:
                latch[f"kind|{kind}"] = self.now
                self.alerts.append(_alert("limit_reached", None, USAGE_REPORT_WINDOW, "Claude reports a new kind of limit",
                                          f"Claude's usage report now has a limit called \"{title(kind)}\". AccountDeck counts "
                                          "it for your terminals unless it belongs to another app or model.", self.now, "warning"))

    def forget(self, accounts):
        """Drops the latches of removed accounts and of cycles that ended long ago."""
        self.latches = {group: {key: at for key, at in latch.items() if (group in ("runway", "report", "together") or key.split("|")[0] in accounts)
                                and (group not in CYCLE_GROUPS or self.now - at <= KEEP)}
                        for group, latch in self.latches.items()}


class AlertEngine:
    def __init__(self, *, started):
        self.hold_until = started + HOLD
        self.primed = False

    def resumed(self, now):
        """A resume from sleep: hold again, and prime again on the next observation."""
        self.hold_until = max(self.hold_until, now + HOLD)
        self.primed = False

    def evaluate(self, prev_state, state, latches, now, *, low_left=15, reset_minutes=15, tz=None, logins=None,
                 stranded=False, report=None):
        """(new alerts, latches). Alerts are dicts for Store.add_alert; `latches` is the stored
        `alert_latches` value (not modified) and the returned one replaces it. `prev_state` is the
        state of the previous evaluation, which tells a downward crossing from a window that was
        low already. `logins` maps terminal account ids to the end of their saved login (unix
        seconds, None when unknown). `stranded` says no terminal account can take over from the active
        one (cli_policy's `exhausted`). `report` is `usage.report_status`."""
        quiet = not self.primed or now < self.hold_until
        self.primed = True
        run = _Run(_latches(latches), now, quiet, tz, low_left, reset_minutes, logins, stranded, (state or {}).get("settings"))
        previous = _accounts(prev_state)
        best = ((state or {}).get("now_band") or {}).get("best") or {}
        current = _accounts(state)
        for account in current.values():
            run.account(account, previous.get(account["id"]), best.get(account["provider"]) or {})
        run.together(current.values())
        run.runway((((state or {}).get("providers") or {}).get("claude_cli") or {}).get("runway"))
        run.usage_report(report)
        run.spent(current.values())     # last: merged notifications lead with the newest alert, and this is money
        run.forget(current)
        return run.alerts, run.latches


def claude_code_mismatch(version, passed, problems, now):
    """The compatibility watch's one alert for a Claude Code version (doctor.watch): a limit_reached alert like the
    runway's (the kind notifications carry), told apart by its `window`."""
    since = f" It last matched {passed}." if passed else ""
    return _alert("limit_reached", None, CLAUDE_CODE_WINDOW, f"Claude Code {version} no longer matches AccountDeck",
                  " ".join(problems[:3]) + since + " Terminal switching may not work until AccountDeck is updated.", now)


def switch_result(label, provider, verified, now):
    """The switch_result alert for a finished switch (`verified` as in the SwitchReport)."""
    name = PROVIDER_NAMES.get(provider, "The app")
    if verified == "verified":
        return _alert("switch_result", None, None, f"Switched {name} to {label}.", f"{name} is signed in as {label}.", now)
    if verified == "unconfirmed":
        return _alert("switch_result", None, None, f"Switched {name} to {label}.",
                      f"{name} opened — still confirming the sign-in in the background.", now, "info")
    return _alert("switch_result", None, None, f"Couldn't switch {name} to {label}.",
                  f"{name} opened a different account. Recovery is available." if provider == "claude"
                  else f"{name} did not sign in as {label}.", now, "critical")


# ---- the service itself (engine.Watchdog, Engine.announce): kind "service", never about one account ----

RESTART_REASONS = {"tick": "Its background loop had stopped", "reads": "All its usage checks were stuck",
                   "db_damaged": "It found damage in its database"}


def restarted_itself(reason, now, *, interrupted=None):
    why = RESTART_REASONS.get(reason, "Something inside it had stopped")
    tail = " An action under way was cut short: if AccountDeck asks you to Recover, do that first." if interrupted else ""
    return _alert("service", None, None, "AccountDeck restarted itself",
                  f"{why}, so it restarted, and your accounts are being watched again.{tail}", now)


def restarts_braked(reason, count, now):
    why = RESTART_REASONS.get(reason, "Something inside it had stopped")
    return _alert("service", None, None, "AccountDeck keeps getting stuck",
                  f"{why} again. It already restarted itself {count} times in the last hour, so it stays as it is for "
                  "now: usage checks and auto-switch may not run until you quit and reopen it.", now, "critical")


def port_moved(wanted, port, now):
    return _alert("service", None, None, "AccountDeck moved to another port",
                  f"Port {wanted} is taken by another program, so the dashboard runs on port {port} for now. The menu "
                  "bar and the app window follow it; a browser bookmark of the old address does not.", now)


def database_restored(backup_at, now, *, why="damaged", back=0, lost=0, checked=True, tz=None):
    """A restored database: what was lost (everything recorded since `backup_at`) and how the logins fared."""
    parts = [f"Its database was {why}, so it went back to the backup of {clock_text(backup_at, now, tz)}. Usage readings "
             "and changes to settings since then are gone."]
    if not checked:
        parts.append("Your saved logins are in the encrypted vault, which a backup does not touch, but the keychain did "
                     "not answer, so they could not be checked yet.")
    elif lost:
        parts.append(f"{lost} saved login(s) could not be read and need signing in again (an account removed since the "
                     "backup shows up this way: remove it again). The others are safe in the encrypted vault, which a "
                     "backup does not touch.")
    else:
        parts.append("Your saved logins are safe: they live in the encrypted vault, which a backup does not touch.")
    if back:
        parts.append(f"{back} account(s) added since the backup came back from the vault.")
    parts.append("The old file is kept in the backups folder.")
    return _alert("service", None, None, "AccountDeck restored its database", " ".join(parts), now, "critical")


def database_damaged(has_backup, now):
    tail = ("It restarts as soon as it can and restores its newest backup." if has_backup else
            "It has no backup yet, so it keeps running on the file as it is. Your saved logins are safe in the "
            "encrypted vault.")
    return _alert("service", None, None, "AccountDeck found damage in its database", tail, now, "critical")
