"""How much terminal capacity is left, in "accounts' worth" (the menu bar totals). Pure: state + now in.

Only Claude Code (terminal) accounts count; Desktop "claude" rows are the same people. An account
that needs sign-in, that Claude put on hold, that the owner left out, or whose weekly reading (or the 5-hour
reading it has) is unknown is not counted, only named. Each counted window's share left at `now` (which may be
after the snapshot): locked → 0, also past its reset (a lock has no known end); not started → 100; a reset that
passed since the reading → 100, assumed (approximate); blocked or out → 0; else 100 − used.
An account whose weekly limit, or a meter that holds terminals (rules.meters_out), is out has no usable 5-hour
or Fable 5.1 room either; one with a lock that holds it has none at all (rules.locks), and none of its resets is a
refill. Each total covers its own accounts: weekly every counted one; 5-hour only those with a 5-hour window (no
window is no 5-hour limit, cli_policy.considered), each adding no more than its weekly room, as 5-hour use also
spends the weekly limit; Fable 5.1 those reporting it. Nothing about 5-hour shows when no account has a window.
The totals are those shares summed over 100: 4.2 means four accounts and a fifth of one.

`web/js/overview.js:totals` applies the same rule for the dashboard (there in percent); keep the two in step.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from .rules import countdown_text, locked
from .core import is_frozen

STALE = 3600            # a reading older than this makes its totals approximate
OUT_LEFT = 0.5          # ≤ this much left is out (rules.band)
MIN_GAIN = 0.05         # a refill must add at least this much to be the "next refill"
FABLE = "Fable"         # the Fable 5.1 weekly window's model, by prefix ("Fable 5.1" too), like the policy
# Face thresholds, in accounts' worth: plenty from 2.0, watch from 0.75, alarm below; muted when nothing counts.
PLENTY, WATCH = 2.0, 0.75


@dataclass(frozen=True)
class Row:
    """One counted account. Shares are percent left (0–100); `five` is None without a 5-hour window, `fable`
    when not reported."""
    id: str
    label: str
    active: bool
    five: float | None
    weekly: float
    fable: float | None
    weekly_out: bool
    five_approx: bool
    weekly_approx: bool
    fable_approx: bool


@dataclass(frozen=True)
class Refill:
    label: str
    window: str         # "5-hour" | "weekly"
    gain: float         # accounts' worth added to that total
    at: float


@dataclass(frozen=True)
class Pool:
    rows: tuple = ()
    signin: tuple = ()      # labels of accounts that need sign-in
    on_hold: tuple = ()     # labels of accounts Claude put on hold (signing in again does not help)
    left_out: tuple = ()
    unread: tuple = ()
    refill: Refill | None = None

    @property
    def capacity(self) -> int:
        return len(self.rows)

    @property
    def uncounted(self) -> tuple:
        """The labels of every terminal account that is named rather than counted."""
        return self.signin + self.on_hold + self.left_out + self.unread

    @property
    def five(self) -> float:
        # 5-hour use also spends the weekly limit, so no account adds more 5-hour room than weekly room.
        return sum(min(r.five, r.weekly) for r in self.rows if r.five is not None) / 100

    @property
    def five_capacity(self) -> int:
        """The accounts the 5-hour total covers: those with a 5-hour window."""
        return sum(r.five is not None for r in self.rows)

    @property
    def has_five(self) -> bool:
        """Whether any counted account reports a 5-hour window; without one the 5-hour total is not shown."""
        return self.five_capacity > 0

    @property
    def weekly(self) -> float:
        return sum(r.weekly for r in self.rows) / 100

    @property
    def fable(self) -> float:
        return sum(r.fable for r in self.rows if r.fable is not None) / 100

    @property
    def fable_capacity(self) -> int:
        return sum(r.fable is not None for r in self.rows)

    @property
    def five_approx(self) -> bool:
        return any(r.five_approx for r in self.rows if r.five is not None)

    @property
    def weekly_approx(self) -> bool:
        return any(r.weekly_approx for r in self.rows)

    @property
    def fable_approx(self) -> bool:
        return any(r.fable_approx for r in self.rows if r.fable is not None)


def _num(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def left_at(window, at, now):
    """(percent left at `now`, assumed) for one window read at `at`, or None when unknown."""
    if not isinstance(window, dict):
        return None
    if locked(window):
        return 0.0, False                   # a lock has no known end: a reset that passed does not lift it
    if window.get("started") is False:
        return 100.0, False
    reset = _num(window.get("reset"))
    if reset is not None and reset <= now and (at is None or at < reset):
        return 100.0, True                  # resetting: the refill is assumed until a reading confirms it
    if window.get("blocked") or window.get("severity") == "out":
        return 0.0, False
    used = _num(window.get("used"))
    if used is None:
        return None
    return min(100.0, max(0.0, 100.0 - used)), False


def _windows(account):
    windows = [w for w in account.get("windows") or () if isinstance(w, dict)]
    five = next((w for w in windows if w.get("kind") == "session"), None)
    weekly = next((w for w in windows if w.get("kind") == "weekly"), None)
    fable = next((w for w in windows if w.get("kind") == "model" and str(w.get("model") or "").casefold()
                  .startswith(FABLE.casefold())), None)
    return five, weekly, fable


def _locked(account) -> bool:
    """A window Claude reports locked holds the whole account (rules.locks)."""
    return any(locked(w) for w in (*(account.get("windows") or ()), *(account.get("other_windows") or ())))


def _meter_out(account, at, now) -> bool:
    """A meter that holds terminals is used up at `now` (rules.meters_out)."""
    lefts = [left_at(w, at, now) for w in account.get("other_windows") or () if w.get("gates")]
    return any(left is not None and left[0] <= OUT_LEFT for left in lefts)


def _signin(account) -> bool:
    return account.get("health") == "needs_signin" or (account.get("credential") or {}).get("state") == "reauth_required"


def _row(account, now):
    """A Row, or None when the account's weekly share, or the 5-hour share it has, is unknown."""
    at = _num(account.get("last_good_at"))
    if at is None:
        return None
    five_w, weekly_w, fable_w = _windows(account)
    weekly, fable = left_at(weekly_w, at, now), left_at(fable_w, at, now)
    five = None if five_w is None else left_at(five_w, at, now)
    if weekly is None or (five_w is not None and five is None):
        return None
    if _locked(account):                    # a lock holds the whole account (rules.locks): no room anywhere
        five = None if five is None else (0.0, False)
        weekly = (0.0, False)
        fable = None if fable is None else (0.0, False)
    stale = now - at > STALE
    out = weekly[0] <= OUT_LEFT or bool(weekly_w.get("blocked")) or _meter_out(account, at, now)
    label = str(account.get("label") or account.get("id") or "")
    return Row(str(account.get("id") or label), label, bool(account.get("active")),
               None if five is None else (0.0 if out else five[0]), weekly[0],
               None if fable is None else (0.0 if out else fable[0]), out,
               five is not None and (stale or five[1]), stale or weekly[1], fable is not None and (stale or fable[1]))


def _refill(rows, accounts, now):
    """The soonest scheduled reset that adds room to the 5-hour or weekly total; none of a locked account's."""
    best = None
    for row in rows:
        if _locked(accounts[row.id]):
            continue                        # a lock has no known end: no reset refills the account
        five_w, weekly_w, _ = _windows(accounts[row.id])
        # A 5-hour refill adds nothing while the weekly limit is out.
        for name, window, left in (("5-hour", None if row.weekly_out else five_w, row.five),
                                   ("weekly", weekly_w, row.weekly)):
            reset = _num((window or {}).get("reset"))
            if reset is None or reset <= now:
                continue
            gain = (row.weekly - min(left, row.weekly) if name == "5-hour" else 100.0 - left) / 100
            if gain < MIN_GAIN:
                continue
            if best is None or reset < best.at:
                best = Refill(row.label, name, gain, reset)
    return best


def summarize(state, now) -> Pool:
    rows, signin, on_hold, left_out, unread, by_id = [], [], [], [], [], {}
    for account in (state or {}).get("accounts") or ():
        if not isinstance(account, dict) or account.get("provider") != "claude_cli":
            continue
        label = str(account.get("label") or account.get("id") or "")
        if is_frozen(account):     # another computer's sign-in: never counted here, like a left-out account
            left_out.append(label)
        elif _signin(account):
            signin.append(label)
        elif account.get("health") == "on_hold":
            on_hold.append(label)
        elif account.get("excluded"):
            left_out.append(label)
        else:
            row = _row(account, now)
            if row is None:
                unread.append(label)
            else:
                rows.append(row)
                by_id[row.id] = account
    return Pool(tuple(rows), tuple(signin), tuple(on_hold), tuple(left_out), tuple(unread), _refill(rows, by_id, now))


# ---- words ----

def amount(value: float, approx: bool = False) -> str:
    """"4.2", or "~4.2" when approximate; one decimal rounded half up like model.js."""
    return ("~" if approx else "") + f"{math.floor(value * 10 + 0.5) / 10:.1f}"


def tone(total: float, capacity: int) -> str:
    """The totals' colour: plenty | watch | alarm, muted when nothing is counted."""
    if capacity <= 0:
        return "muted"
    return "plenty" if total >= PLENTY else "watch" if total >= WATCH else "alarm"


def _accounts(n: int) -> str:
    return f"{n} account" + ("" if n == 1 else "s")


def _total_line(name, total, capacity, approx) -> str:
    share = round(100 * total / capacity) if capacity else 0
    return f"{name} {amount(total, approx)} of {_accounts(capacity)} ({share}%)"


def _share(value, approx) -> str:
    return f"{'~' if approx else ''}{math.floor(value + 0.5)}%"


def _account_line(row: Row) -> str:
    parts = ([] if row.five is None else [f"5-hour {_share(row.five, row.five_approx)}" + (" (weekly out)" if row.weekly_out else "")])
    parts.append(f"weekly {_share(row.weekly, row.weekly_approx)}")
    if row.fable is not None:
        parts.append(f"Fable 5.1 {_share(row.fable, row.fable_approx)}")
    return f"{row.label}{' (in use)' if row.active else ''}: " + " · ".join(parts) + " left"


def _named(count_text: str, labels) -> list[str]:
    return [f"{len(labels)} {count_text} ({', '.join(labels)})"] if labels else []


def uncounted_lines(pool: Pool) -> list[str]:
    """Who the totals leave out and why ("1 on hold (Bob)", "2 left out (Cut1, Cut4)"), one entry each."""
    return (_named("needs sign-in", pool.signin) + _named("on hold", pool.on_hold) + _named("left out", pool.left_out)
            + _named("not read yet", pool.unread))


def tooltip_lines(pool: Pool, now) -> list[str]:
    """The totals' detail text: totals, the next refill, each counted account, then who is not counted."""
    lines = ["Claude Code terminals: room left in accounts' worth"]
    if pool.capacity:
        if pool.has_five:
            lines.append(_total_line("5-hour", pool.five, pool.five_capacity, pool.five_approx))
        lines.append(_total_line("Weekly", pool.weekly, pool.capacity, pool.weekly_approx))
        if pool.fable_capacity:
            lines.append(_total_line("Fable 5.1", pool.fable, pool.fable_capacity, pool.fable_approx))
        if pool.refill is not None:
            refill = pool.refill
            lines.append(f"Next refill: {refill.label} +{amount(refill.gain)} ({refill.window}) in "
                         f"{countdown_text(refill.at - now)}")
        if pool.five_approx or pool.weekly_approx or pool.fable_approx:
            lines.append("~ an old reading, or a reset assumed to have refilled")
        lines.append("")
        lines += [_account_line(row) for row in pool.rows]
    else:
        lines.append("No terminal account can be counted right now.")
    extra = uncounted_lines(pool)
    if extra:
        lines += ([""] if pool.capacity else []) + ["Not counted:"] + extra
    return lines


def tray_line(pool: Pool) -> str | None:
    """A one-line summary ("Terminals 5h 1.2 / 2 · wk 5.6 / 6", without "5h" when no account reports a 5-hour
    window), or None when there are no terminal accounts."""
    if not pool.capacity:
        return "Terminals: none counted" if pool.uncounted else None
    five = f"5h {amount(pool.five, pool.five_approx)} / {pool.five_capacity} · " if pool.has_five else ""
    return f"Terminals {five}wk {amount(pool.weekly, pool.weekly_approx)} / {pool.capacity}"
