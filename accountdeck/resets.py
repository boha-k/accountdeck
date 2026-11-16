"""Limit resets: how many each Claude Code, Claude Desktop and Codex account holds, and "Use a limit reset…" for one
account and one reset the user confirmed (the `reset_credits` rows and the `resets` block of the state, the actions
`reset-plan` and `use-reset`).

A limit reset is used in two ways only, and both run the one operation here (`ResetOperation`, `Resets.run`).
`use-reset` uses one for the account and the row of a plan the user confirmed within its five minutes. With the
setting `auto_reset` on (off by default), reset_auto.py uses one Claude Code reset when every terminal account
auto-switch could use is out (`Resets.use_automatically`): one at a time, and never another before a reading showed
how the last went. Nothing else sends a claim or a consume: the scheduler, the auto-switch, the limit-error watch,
the runway, the alerts and the recommender never reach it, and a Codex or Claude Desktop reset is never used
automatically. Never two resets for one click or one decision: the request key (Codex's `idempotencyKey`, Claude's
`request_id`) is saved before the request is sent, the same key is sent again for the same reset while its outcome is
not known (Claude: 10 minutes, Codex: 24 hours, as their own clients do), and a new one is made only once a reading
has settled it.

What reaches the state, the log, an error or a process argument never names a provider's id of a reset, an
organisation, a token or a request key: a row's `key` is AccountDeck's own hash. The ids live only in the service's
memory (`MEMORY`, one internal setting: no table, no schema change, no vault record; older builds never read it).

Codex's count comes with every reading (`account/rateLimits/read`); the use goes through Codex's own app-server only
(`Codex.reset_session`). Claude's needs a separate read (`?cedar_ember=1&skip_spend=1`), made only straight after a
successful ordinary read of that account, on the route that read used, at most hourly (every 15 minutes when the user
asked), never with a token that would need renewing, and after a 401, 403 or 429 or three failures in a row not for
6 hours; the setting `claude_resets` turns it off. Its use is one POST through the OAuth route only. The read on the
OAuth route and the claim go out with Claude Code's User-Agent (providers.base.is_reset_request): Anthropic lists an
account's grants to no other client there.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import re
import secrets
import threading
import uuid

from . import diagnostics, intel
from .core import DeckError, is_frozen
from .operations import Operation
from .providers.claude_cli import fingerprint
from .providers.codex import ACCESS_EXPIRY_MARGIN, identity_key, token_state
from .providers.errors import ProviderError
from .usage import (KEPT_TITLES, RESET_TITLE_CHARS, available_credit, claude_reset_block, codex_usage, reset_group,
                    take_codex_resets)

MEMORY = "limit_resets"           # the internal setting that holds what this module keeps (see the docstring)
CLAUDE = ("claude", "claude_cli")
PROVIDERS = CLAUDE + ("codex",)
PLAN_TTL = 300.0                  # a plan can be confirmed for this long
READ_EVERY = 3600.0               # Claude's resets read at most this often ...
READ_ASKED = 900.0                # ... or this often when the user asked for the read
ASK_WINDOW = 900.0                # a read this soon after a Refresh or after AccountDeck was opened counts as asked
READ_WAIT = 6 * 3600.0            # after a 401, 403 or 429, or READ_FAILURES failures in a row
READ_FAILURES = 3
KEY_TTL = {"claude": 600.0, "claude_cli": 600.0, "codex": 86400.0}   # a request key is sent again this long
STALE_READING = 3600.0            # a reading older than this is too old to plan a use from
STALE_BLOCK = 6 * 3600.0          # Claude's resets read older than this is too
CLAUDE_TOKEN_MARGIN = 120.0       # = providers.claude.RESET_TOKEN_MARGIN
CODEX_TOKEN_MARGIN = ACCESS_EXPIRY_MARGIN    # the helper cannot renew an access token closer to its expiry
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# ---- words ----
OTHER_OWNER = "Another AccountDeck 2 is managing your sign-ins. Quit it to make changes here."   # = engine.OTHER_OWNER
BUSY = "Finish or cancel the current action first."                                            # = switching.BUSY_MESSAGE
SIGNING_IN = "Finish or cancel the sign-in first."
FROZEN = "This account's sign-in belongs to another computer, so its limit resets are used there."
SETTING_OFF = "Turn on “Read Claude limit resets” in Settings first."
NEEDS_SIGNIN = "This account needs a new sign-in first."
STALE = "Its last reading is over an hour old. Refresh it first."
STALE_RESETS = "Its limit resets were last read too long ago. Refresh it first."
NO_ORG = "AccountDeck does not know this account's organization, so it cannot use a limit reset here. Use it on claude.ai."
WEB_ONLY = "No saved sign-in token of this account can use a limit reset here. Use it on claude.ai."
CLAUDE_EXPIRED = "Its saved sign-in token has expired. Open the account in Claude once, then try again."
CLI_EXPIRED = "Its sign-in has expired. Refresh the account first, then try again."
CODEX_EXPIRING = ("Its Codex sign-in is about to expire, and the helper AccountDeck uses cannot renew it. Refresh the "
                  "account or use it in Codex once, then try again.")
WAIT_REFUSED = "Anthropic refused the last read of this account's limit resets. AccountDeck reads them again later."
WAIT_FAILED = "The last reads of this account's limit resets failed. AccountDeck reads them again later."
SAVE_FIRST = "Save this account first to use its limit resets."
NOT_READ = "AccountDeck does not read this account's limit resets."
NO_RESETS = "Antigravity has no limit resets."
NONE_USABLE = "None of its limit resets can be used right now."
NONE_HELD = "This account holds no limit resets right now."
DOUBT_OTHER = "The outcome of the last use is not known yet. AccountDeck is reading the account again."
NOT_KNOWN = "It is not known whether the limit reset was used. AccountDeck is reading the account again."
SAID = ("No limit reset was used", "It is not known whether")     # how a report's summary starts (the dashboard reads it)
CHANGED = "this limit reset changed since you reviewed it. Review the account again."
GONE = "this limit reset is no longer there. Review the account again."
AUTO_STALE = "this limit reset is not as AccountDeck last read it. It has been read again."    # nobody reviewed it
STALE_PLAN = "This plan has expired. Review it again."                                         # = switching

CODEX_BELOW_80 = "No limit is below 80% left, so there is nothing to reset right now."
CODEX_UNLISTED = "Codex did not list this limit reset just now, so it cannot be chosen here. Refresh the account and try again."
CODEX_80_WARNING = "Codex applies a reset only when a limit it covers has less than 80% left."
CODEX_RESTORES = {"plus": "the 5-hour and weekly limits", "business": "the 5-hour and weekly limits",
                  "team": "the 5-hour and weekly limits", "pro": "the weekly limit", "go": "the monthly limit",
                  None: "the limits this plan's reset covers"}
CODEX_EFFECTS = {"pro": "Its weekly limit goes back to full and its period starts again.",
                 "go": "Its monthly limit goes back to full and its period starts again.",
                 None: "The limits its plan's reset covers go back to full and their periods start again."}
CODEX_EFFECT_BOTH = "Its 5-hour and weekly limits go back to full and their periods start again."

CLAUDE_RESTORES = {"session": "the 5-hour limit", "weekly": "the weekly limits", "full": "the 5-hour and weekly limits",
                   "fable": "the weekly allowance spent on Fable, plus its listed session limit if present"}
CLAUDE_EFFECT = ("The selected reset restores the allowance described above. A Fable reset returns only the weekly "
                 "allowance spent on Fable. The day the weekly limits reset stays the same.")
CLAUDE_EXTRA_WARNING = "A limit reset does not refund extra usage already billed."
# Said on every Claude plan: who sends the request, and where Anthropic's own button is.
CLAUDE_SENDER_WARNING = ("AccountDeck sends this request itself, as Claude Code, with this account's sign-in. "
                         "Anthropic's own button for it is on claude.ai, under Settings > Usage.")
CLAUDE_NEEDS_LIMIT_WARNING = "Some limit resets work only once a limit they refill is reached."
DOUBT_WARNING = ("The outcome of an earlier use is not known yet. Using it again sends the same request, so it cannot "
                 "be used twice.")
INELIGIBLE = {
    "surface": "Anthropic offers this account's limit resets on claude.ai only.",
    "cli_version": "Anthropic offers this account's limit resets only in a newer Claude Code or on claude.ai.",
    "tier": "This account's plan does not get limit resets.",
    "seat": "This seat of its Team or Enterprise plan does not get limit resets.",
    "mobile": "Anthropic does not offer limit resets to this account here (the reason it gives: mobile).",
    "config_off": "Anthropic has limit resets turned off for now.",
    "no_grant": "Anthropic has not given this account any limit resets.",
    "tenure": "This account is too new for limit resets.",
    "other_experiment": "This account takes part in another Anthropic offer, so it does not get limit resets.",
    "control": "Anthropic does not offer limit resets to this account.",
    "not_enrolled": "Anthropic does not offer limit resets to this account.",
    "plan_changed": "This account's plan changed, so its limit resets are gone.",
}
INELIGIBLE_OTHER = "Anthropic does not offer limit resets to this account here."
PAUSED = "Anthropic has paused this limit reset for now."
COOLDOWN = "A limit reset was just used on this account. Wait a minute, then refresh it."
NEEDS_LIMIT = "It can be used only once a limit it refills is reached."
NOT_NEXT = "Anthropic offers another of this account's limit resets first."
UNAVAILABLE = {
    "cooldown": COOLDOWN,
    "blocked_five_hour": "It cannot be used while the 5-hour limit is spent.",
    "blocked_seven_day": "It cannot be used while the weekly limit is spent.",
    "blocked_opus": "It cannot be used while a model's weekly limit is spent.",
    "blocked_sonnet": "It cannot be used while a model's weekly limit is spent.",
    "blocked_other": "It cannot be used while another limit is spent.",
    "cannot_clear": "None of the limits it refills needs refilling now.",
    "needs_limit": NEEDS_LIMIT,
}
UNAVAILABLE_OTHER = "Anthropic says this limit reset cannot be used right now."
# Why a claim used nothing (`_claim_verdict`), as the end of "No limit reset was used: …".
NOT_NEEDED = "the terminal accounts are no longer all out."      # an automatic use, asked again before sending
CLAIM_UNUSED = {
    "already_used": "this limit reset was already used.",
    "not_limited": "it works only at a usage limit, and the account is not at one now.",
    "cooldown": "another reset was just started on this account. Wait a minute, then refresh it.",
    "ineligible": "this limit reset is not available any more.",
}
# `unavailable` is never in that list: Claude Code reads it as "couldn't confirm the reset went through" (RS1 §C2,
# §C3) and sends the same request again, so here it is "not known" and the key stays.

_offer = threading.local()        # the resets read the ordinary read on this thread may be followed by (`offer`)


def offer(fetch):
    """Called by an ordinary Claude read that just succeeded: `fetch()` would read this account's limit resets on the
    route that read used. The engine decides after the read whether it runs (`Resets.after_read`)."""
    _offer.fetch = fetch


def take_offer():
    fetch, _offer.fetch = getattr(_offer, "fetch", None), None
    return fetch


def row_key(prefix, *parts):
    """AccountDeck's own name for a row: stable while the row is the same, never a provider's id."""
    return prefix + "-" + hashlib.sha256("|".join("" if part is None else str(part) for part in parts).encode()).hexdigest()[:12]


def codex_plan(plan):
    """The word of a Codex plan that decides what its reset covers (`CODEX_RESTORES`), or None."""
    words = set(re.findall(r"[a-z]+", str(plan or "").lower()))
    return next((word for word in ("go", "pro", "plus", "business", "team") if word in words), None)


def codex_needs_reset(usage, word):
    """Whether, as far as the reading tells, a limit the plan's reset covers has less than 80% left (or is spent);
    True when no covered limit has a known percentage: Codex decides then, and refuses with nothing used."""
    kinds = ("weekly",) if word == "pro" else ("session", "weekly")
    windows = [w for w in (usage or {}).get("windows") or () if isinstance(w, dict) and w.get("kind") in kinds]
    if any(w.get("blocked") for w in windows):
        return True
    known = [w["used"] for w in windows if isinstance(w.get("used"), (int, float)) and not isinstance(w.get("used"), bool)]
    return not known or any(used > 20 for used in known)


def codex_rows(account_id, usage, plan):
    """The contract's rows of a Codex reading (`usage["reset_credits"]`), each with the (expires, title) it groups
    credits by (`_group`, removed before the state)."""
    word = codex_plan(plan)
    needed = codex_needs_reset(usage, word)
    rows, seen = [], {}
    for row in (usage or {}).get("reset_credits") or ():
        if not isinstance(row, dict) or type(row.get("count")) is not int or row["count"] < 1:
            continue
        group = (row.get("expires"), row.get("title"))
        seen[group] = seen.get(group, 0) + 1
        listed = not (row.get("expires") is None and row.get("title") is None)
        usable, reason = needed and listed, None if needed and listed else CODEX_UNLISTED if needed else CODEX_BELOW_80
        title = (row.get("title") or "")[:RESET_TITLE_CHARS] or None
        rows.append({"count": row["count"], "expires": row.get("expires"), "title": title,
                     "key": row_key("c", account_id, *group, seen[group]), "restores": CODEX_RESTORES[word],
                     "usable": usable, "reason": reason, "_group": group})
    return rows


def codex_present(usage):
    """Whether a Codex reading carried `rateLimitResetCredits` at all (also with none in it)."""
    return bool((usage or {}).get("reset_credits")) or any(
        isinstance(c, dict) and c.get("label") == "Limit reset credits" for c in (usage or {}).get("credits") or ())


def ineligible_words(reason):
    return INELIGIBLE.get(reason, INELIGIBLE_OTHER)


def _limit_reached(parsed, grant, usage):
    """Whether a limit the grant refills is reached: as the resets read said, or as the ordinary reading says now."""
    clears = set(grant["clears"])
    if grant["kind"] == "fable":
        clears.add("seven_day")     # returns Fable's contribution, without clearing the whole weekly limit
    if parsed.get("at_limit") and not clears:
        return True
    if clears & set(parsed.get("exhausted") or ()):
        return True
    for window in (usage or {}).get("windows") or ():
        if not isinstance(window, dict) or (clears and window.get("id") not in clears):
            continue
        used = window.get("used")
        if window.get("blocked") or (isinstance(used, (int, float)) and not isinstance(used, bool) and used >= 100):
            return True
    return False


def claude_rows(account_id, parsed, usage, now):
    """The contract's rows of Claude's parsed block (`usage.claude_reset_block`), each with its grant (`_grant`)."""
    rows, offered = [], set()
    for grant in parsed["grants"]:
        if grant["left"] < 1 or (grant["ends"] is not None and grant["ends"] <= now):
            continue
        reason = None
        if not parsed["eligible"]:
            reason = ineligible_words(parsed["reason"])
        elif grant["paused"]:
            reason = PAUSED
        elif parsed.get("cooldown_until") and parsed["cooldown_until"] > now:
            reason = COOLDOWN
        elif grant["needs_limit"] and not _limit_reached(parsed, grant, usage):
            reason = NEEDS_LIMIT
        elif grant["usable_now"] is not True:
            reason = UNAVAILABLE.get(grant["unavailable"], UNAVAILABLE_OTHER)
        elif grant["kind"] in offered:
            reason = NOT_NEXT
        if reason is None:
            offered.add(grant["kind"])
        rows.append({"count": grant["left"], "expires": grant["ends"], "title": grant["label"] or KEPT_TITLES[grant["kind"]],
                     "key": row_key("g", account_id, grant["id"]), "restores": CLAUDE_RESTORES[grant["kind"]],
                     "usable": reason is None, "reason": reason, "_grant": grant})
    return sorted(rows, key=lambda row: (row["expires"] is None, row["expires"] or 0))


def assemble(rows, *, checked, note, pending, running, blockers):
    """The `resets` block of an account's rows (changed in place: while a use's outcome is not known, no other row is
    usable). `pending`: its open request ({"row", "since"}) or None; `running`: a use of it runs now (no doubt is
    shown meanwhile); `blockers`: what must change first (`Resets.blockers`)."""
    doubt = None
    if pending is not None:
        doubt = None if running else {"since": pending.get("since") or pending.get("made")}
        for item in rows:
            if item["usable"] and item["key"] != pending.get("row"):
                item.update(usable=False, reason=DOUBT_OTHER)
    available = sum(item["count"] for item in rows)
    can_use = not blockers and any(item["usable"] for item in rows)
    blocked = None
    if not can_use and available:
        blocked = blockers[0]["message"] if blockers else next((item["reason"] for item in rows if item["reason"]),
                                                                 NONE_USABLE)
    return {"checked": checked, "available": available, "can_use": can_use, "blocked": blocked, "note": note,
            "doubt": doubt}


def public(rows):
    return [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _maybe(value):
    return value is None or _number(value)


def _words(value):
    return isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value)


def block_ok(block) -> bool:
    """Whether a kept block has the shape `usage.claude_reset_block` gives (what the rows are built from)."""
    if not isinstance(block, dict) or not isinstance(block.get("eligible"), bool) or not isinstance(block.get("grants"), list):
        return False
    if not (block.get("reason") is None or isinstance(block["reason"], str)) or not _maybe(block.get("cooldown_until")):
        return False
    if not (block.get("next") is None or isinstance(block["next"], str)) or not _words(block.get("exhausted", ())):
        return False
    for grant in block["grants"]:
        if not isinstance(grant, dict) or not isinstance(grant.get("id"), str) or not grant["id"]:
            return False
        if type(grant.get("left")) is not int or grant["left"] < 0 or grant.get("kind") not in CLAUDE_RESTORES:
            return False
        if not _maybe(grant.get("ends")) or not (grant.get("label") is None or isinstance(grant["label"], str)):
            return False
        if not isinstance(grant.get("paused"), bool) or not isinstance(grant.get("needs_limit"), bool):
            return False
        if grant.get("usable_now") not in (None, True, False) or not _words(grant.get("clears", ())):
            return False
        if not (grant.get("unavailable") is None or isinstance(grant["unavailable"], str)):
            return False
    return True


def pending_ok(pending) -> bool:
    """Whether a kept request has the shape `_keep_key` writes."""
    return (isinstance(pending, dict) and isinstance(pending.get("key"), str) and bool(_UUID.fullmatch(pending["key"]))
            and pending.get("provider") in PROVIDERS and isinstance(pending.get("row"), str)
            and isinstance(pending.get("target"), str) and bool(pending["target"])
            and _number(pending.get("made")) and _number(pending.get("until")) and _number(pending.get("since"))
            and type(pending.get("before")) is int and pending.get("sent") is True and _maybe(pending.get("ends")))


def auto_ok(auto) -> dict:
    """What reset_auto.py keeps (`Resets.set_auto`: `last`, `failed`, `stopped`), read back. A part that is not in the
    shape it writes is left out, and automatic resets then count as stopped: what held the next one back is gone."""
    if auto is None:
        return {}
    damaged = not isinstance(auto, dict)
    auto, clean = auto if isinstance(auto, dict) else {}, {}
    last, failed, stopped = auto.get("last"), auto.get("failed"), auto.get("stopped")
    if last is not None:
        if (isinstance(last, dict) and isinstance(last.get("account"), str) and last["account"] and _number(last.get("at"))
                and _words(last.get("limits")) and isinstance(last.get("open"), bool)
                and isinstance(last.get("unsure", False), bool)):
            clean["last"] = {"account": last["account"], "at": last["at"], "limits": list(last["limits"]),
                             "open": last["open"], "unsure": last.get("unsure", False)}
        else:
            damaged = True
    if failed is not None:
        if (isinstance(failed, dict) and _number(failed.get("at")) and type(failed.get("count")) is int
                and failed["count"] >= 1 and isinstance(failed.get("account"), str)):
            clean["failed"] = {"at": failed["at"], "count": failed["count"], "account": failed["account"]}
        else:
            damaged = True
    if stopped is not None:
        if isinstance(stopped, dict) and _number(stopped.get("at")):
            clean["stopped"] = {"at": stopped["at"]}
        else:
            damaged = True
    if damaged:
        clean.setdefault("stopped", {"at": 0})
        diagnostics.log("resets.memory_dropped", "warning", what="auto")
    return clean


def clean_memory(stored) -> dict:
    """What this module kept (`MEMORY`), with everything that is not in the shape it writes left out: a damaged or
    hand-edited value never stops the service. A request key that cannot be read is logged, since its doubt ends."""
    accounts = stored.get("accounts") if isinstance(stored, dict) else None
    clean = {}
    for key, entry in accounts.items() if isinstance(accounts, dict) else ():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        kept = {}
        claude = entry.get("claude")
        if isinstance(claude, dict):
            times = {name: claude[name] for name in ("tried", "checked", "wait_until") if _number(claude.get(name))}
            if type(claude.get("failures")) is int and claude["failures"] >= 0:
                times["failures"] = claude["failures"]
            if claude.get("wait_reason") in ("refused", "failed"):
                times["wait_reason"] = claude["wait_reason"]
            if block_ok(claude.get("block")) and "checked" in times:
                times["block"] = claude["block"]
            else:
                times.pop("checked", None)
            if times:
                kept["claude"] = times
        if "pending" in entry:
            if pending_ok(entry["pending"]):
                kept["pending"] = entry["pending"]
            else:
                diagnostics.log("resets.memory_dropped", "warning", acct=diagnostics.account_ref(key), what="pending")
        if kept:
            clean[key] = kept
    auto = auto_ok(stored.get("auto") if isinstance(stored, dict) else None)
    return {"accounts": clean, **({"auto": auto} if auto else {})}


class Resets:
    """The engine's limit resets: the memory, Claude's resets read, the view, the plans and the use."""

    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()
        self.plans = {}
        self.forced = set()           # accounts whose next ordinary read is followed by a resets read in any case
        self.asked = {}               # account id -> when the user asked for its read (Refresh)
        self.looked_at = None         # when the user last opened AccountDeck
        self.memory = clean_memory(engine.store.setting(MEMORY))
        self._committed = copy.deepcopy(self.memory)

    # ---- memory ----
    def _entry(self, key):
        return self.memory["accounts"].setdefault(key, {})

    def _save(self):
        """Caller holds the lock. Written through: a request key must be on disk before its request is sent."""
        self.memory["accounts"] = {key: entry for key, entry in self.memory["accounts"].items() if entry}
        try:
            self.engine.store.set_setting(MEMORY, self.memory)
        except Exception:
            self.memory = copy.deepcopy(self._committed)
            raise
        self._committed = copy.deepcopy(self.memory)

    def _members(self, row):
        """Claude's two app rows can refer to one account. Never link by email or an unknown organization."""
        if row["provider"] not in CLAUDE or not _UUID.fullmatch(str(row["org"] or "")):
            return [row]
        return [other for other in self.engine.store.accounts()
                if other["provider"] in CLAUDE and other["identity"] == row["identity"] and other["org"] == row["org"]]

    def _pending(self, row):
        with self.lock:
            return [(other["id"], copy.deepcopy(pending)) for other in self._members(row)
                    if (pending := self.memory["accounts"].get(other["id"], {}).get("pending")) and pending.get("sent")]

    def check_remove(self, row):
        if self._pending(row):
            raise DeckError("A limit reset's outcome is still unknown. Refresh this account before removing it.", "busy")

    def entry(self, key):
        with self.lock:
            return copy.deepcopy(self.memory["accounts"].get(key) or {})

    # ---- automatic use (reset_auto.py decides; what it remembers is kept here, with the request keys) ----
    def auto(self):
        """What reset_auto remembers: {"last", "failed", "stopped"}, each only while there is one."""
        with self.lock:
            return copy.deepcopy(self.memory.get("auto") or {})

    def set_auto(self, *, update=None, **parts):
        """Keeps what reset_auto remembers, written through: each of `parts` ("last", "failed", "stopped") replaces
        that part (None removes it), and `update` changes fields of the `last` that is there."""
        with self.lock:
            auto = self.memory.setdefault("auto", {})
            for name, value in parts.items():
                if value is None:
                    auto.pop(name, None)
                else:
                    auto[name] = copy.deepcopy(value)
            if update and isinstance(auto.get("last"), dict):
                auto["last"].update(update)
            if not auto:
                self.memory.pop("auto", None)
            self._save()

    def _auto_verdict(self, key, verdict, now):
        """Caller holds the lock and saves. The request of an automatic use that was not known to have gone through is
        settled: used counts from now (a reading has yet to show the refill), not used is one more try that used
        nothing."""
        auto = self.memory.get("auto") or {}
        last = auto.get("last") or {}
        if not (last.get("open") and last.get("unsure") and last.get("account") == key):
            return
        if verdict == "used":
            last.update(at=now, unsure=False)
        elif verdict == "unused":
            failed = auto.get("failed") or {}
            auto["failed"] = {"at": now, "count": int(failed.get("count") or 0) + 1, "account": key}
            auto.pop("last", None)

    def any_open_request(self):
        """Whether any account's request key is open: a use whose outcome is not known yet."""
        with self.lock:
            return any((entry.get("pending") or {}).get("sent") for entry in self.memory["accounts"].values())

    def open_request(self, row):
        return bool(self._pending(row))

    def usable_rows(self, row, now):
        """The rows `reset-plan` would offer for this account right now, each with its grant (`_grant`): none while
        anything blocks a use (`blockers`, a running action and an open sign-in among them). Never raises."""
        try:
            rows, block = self._view(row, now, transient=True)
        except Exception as exc:
            diagnostics.log_exception("resets.view_failed", exc, acct=diagnostics.account_ref(row["id"]))
            return []
        if block is None or block["blockers"]:
            return []
        return [item for item in rows if item["usable"]]

    def use_automatically(self, row, chosen, automatic):
        """reset_auto's one way in: the operation a confirmed plan starts, for a Claude Code row `usable_rows` just
        gave. `automatic["confirm"]()` is asked right before the request is sent (`_use_claude`), and
        `automatic["why"]` goes into its report."""
        if row["provider"] != "claude_cli" or not chosen.get("usable"):
            raise DeckError(NONE_USABLE, "blocked")
        return self.engine.submit(ResetOperation(self, row, chosen, automatic=automatic))

    def forget(self, key):
        """The account was removed: what was kept about its resets goes too."""
        with self.lock:
            if self.memory["accounts"].pop(key, None) is not None:
                self._save()
            self.plans = {pid: plan for pid, plan in self.plans.items() if plan["account"] != key}

    # ---- the user asked ----
    def note_asked(self, ids, now):
        with self.lock:
            for key in ids:
                self.asked[key] = now

    def note_looked(self, now):
        with self.lock:
            self.looked_at = now

    def _was_asked(self, key, now):
        with self.lock:
            times = [at for at in (self.asked.get(key), self.looked_at) if at is not None]
        return any(0 <= now - at <= ASK_WINDOW for at in times)

    # ---- after an ordinary read (engine._refresh, on the read's worker) ----
    def begin_read(self):
        take_offer()
        take_codex_resets()

    def after_read(self, key, provider, result):
        """Straight after an ordinary read of `key`: Claude's resets read when it is due, and a doubt settled by what
        Codex's answer showed. Never raises and never changes the ordinary read's outcome."""
        fetch, credits = take_offer(), take_codex_resets()
        if result != "ok" or provider not in PROVIDERS:
            return
        try:
            row = self.engine.store.account(key)
            if row is None or is_frozen(row):
                return
            if provider == "codex":
                if credits is not None:
                    self._settle_codex(row, credits, self.engine.clock())
            elif fetch is not None:
                self._claude_read(row, fetch)
        except Exception as exc:
            diagnostics.log_exception("resets.after_read_failed", exc, acct=diagnostics.account_ref(key))

    def _claude_read(self, row, fetch):
        engine, key = self.engine, row["id"]
        if not engine.prefs.get("claude_resets"):
            return
        now = engine.clock()
        with self.lock:
            entry = copy.deepcopy(self._entry(key).get("claude") or {})
            forced = key in self.forced
        if (entry.get("wait_until") or 0) > now:
            return
        gap = 0.0 if forced else READ_ASKED if self._was_asked(key, now) else READ_EVERY
        if entry.get("tried") is not None and now - entry["tried"] < gap:
            return
        governor, gkey = engine.scheduler.governor, (row["provider"], row["identity"])
        if governor.delay(gkey, now, signal=True) > 0:
            return                    # this hour's requests for the account are spent: the next read may try
        governor.record(gkey, now)
        with self.lock:
            self.forced.discard(key)
        outcome, parsed = self._fetch(fetch)
        self._record_read(row, outcome, parsed, now)

    @staticmethod
    def _fetch(fetch):
        """("ok", parsed block) or (failure word, None) of one resets read."""
        try:
            data = fetch()
        except ProviderError as exc:
            return ("refused" if exc.http in (401, 403, 429) else "failed"), None
        except Exception:             # DeckError (no token, no org, safe mode) and anything unexpected
            return "failed", None
        parsed = claude_reset_block(data.get("cedar_ember") if isinstance(data, dict) else None)
        if parsed is None or (not parsed["eligible"] and parsed["reason"] == "unavailable"):
            return "failed", None     # `unavailable`: the server could not answer (Claude Code: a failed read)
        return "ok", parsed

    def _record_read(self, row, outcome, parsed, now):
        key = row["id"]
        with self.lock:
            entry = self._entry(key)
            claude = entry.setdefault("claude", {})
            claude["tried"] = now
            if outcome == "ok":
                claude.update(checked=now, block=parsed, failures=0, wait_until=None, wait_reason=None)
            else:
                claude["failures"] = int(claude.get("failures") or 0) + 1
                if outcome == "refused" or claude["failures"] >= READ_FAILURES:
                    claude.update(wait_until=now + READ_WAIT, wait_reason=outcome, failures=0)
            settled = self._settle_claude(row, parsed, now) if outcome == "ok" else None
            self._save()
        diagnostics.log("resets.read", acct=diagnostics.account_ref(key), provider=row["provider"], outcome=outcome,
                        eligible=None if parsed is None else parsed["eligible"],
                        grants=None if parsed is None else len(parsed["grants"]))
        self._settled_event(row, settled)
        self.engine.bump()
        return settled

    # ---- settling a doubt ----
    def _settle_claude(self, row, parsed, now):
        """Caller holds the lock. "used" / "unused" when this reading settles the account's open request, else None."""
        verdicts = []
        for key, pending in self._pending(row):
            grant = next((g for g in parsed["grants"] if g["id"] == pending["target"]), None)
            if grant is None:
                continue              # missing grant/count says nothing, even after the retry window
            if grant["left"] < pending["before"]:
                verdict = "used"
            elif now < pending["until"]:
                continue
            else:
                verdict = "unused"
            self._entry(key).pop("pending", None)
            self._auto_verdict(key, verdict, now)
            verdicts.append(verdict)
        return "used" if "used" in verdicts else "unused" if verdicts else None

    def _settle_codex(self, row, credits, now):
        with self.lock:
            entry = self._entry(row["id"])
            pending = entry.get("pending")
            if not pending or not pending.get("sent") or pending.get("provider") != "codex":
                return
            verdict = codex_verdict(pending, credits, now)
            if verdict is None:
                return
            entry.pop("pending", None)
            self._save()
        self._settled_event(row, verdict)
        self.engine.bump()

    def _settled_event(self, row, verdict):
        if verdict is None:
            return
        label = row["label"] or row["email"] or "an account"
        self.engine.store.event("use-reset", {
            "used": f"The earlier limit reset on {label} went through.",
            "unused": f"The earlier limit reset on {label} was not used."}.get(
                verdict, f"It stayed unknown whether the earlier limit reset on {label} was used. Check its limits."))
        diagnostics.log("resets.settled", acct=diagnostics.account_ref(row["id"]), verdict=verdict)

    # ---- the view (engine._build) ----
    def view(self, row, now):
        """{"rows": the contract's `reset_credits` rows, "block": the `resets` block or None} of a saved account.
        Never raises: the state is built with it, and one account's resets may not take the page down."""
        try:
            rows, block = self._view(row, now, transient=False)
            return {"rows": public(rows), "block": block}
        except Exception as exc:
            diagnostics.log_exception("resets.view_failed", exc, acct=diagnostics.account_ref(row["id"]))
            return {"rows": [], "block": None}

    def view_unsaved(self, provider, unsaved):
        """An unsaved signed-in Codex account shows its count; using one needs it saved."""
        try:
            if (provider != "codex" or not unsaved or unsaved.get("last_good_at") is None
                    or not codex_present(unsaved.get("usage"))):
                return {"rows": [], "block": None}
            rows = [{**row, "usable": False, "reason": SAVE_FIRST}
                    for row in public(codex_rows(contract_unsaved(provider), unsaved["usage"], unsaved.get("plan")))]
            available = sum(row["count"] for row in rows)
            return {"rows": rows, "block": {"checked": unsaved["last_good_at"], "available": available, "can_use": False,
                                            "blocked": SAVE_FIRST if available else None, "note": None, "doubt": None}}
        except Exception as exc:
            diagnostics.log_exception("resets.view_failed", exc, acct=provider)
            return {"rows": [], "block": None}

    def _view(self, row, now, *, transient):
        """(rows with their private fields, block or None) of a saved account; the block's `blockers` list too when
        `transient` (a plan's, which also names a running action or an open sign-in)."""
        provider, key = row["provider"], row["id"]
        if provider not in PROVIDERS or (provider in CLAUDE and not self.engine.prefs.get("claude_resets")):
            return [], None
        entry = self.entry(key)
        usage, at = row["last_good_usage"], row["last_good_at"]
        note = None
        if provider == "codex":
            if at is None or not codex_present(usage):
                return [], None
            rows, checked = codex_rows(key, usage, row["plan"]), at
        else:
            claude = entry.get("claude") or {}
            parsed = claude.get("block")
            if not isinstance(parsed, dict):
                rows, checked = [], None
                note = (WAIT_REFUSED if claude.get("wait_reason") == "refused" else WAIT_FAILED
                        if claude.get("tried") is not None else
                        "Limit resets have not been read yet. Refresh this account to check after its usage read.")
            else:
                rows, checked = claude_rows(key, parsed, usage, now), claude.get("checked")
                if not parsed["eligible"]:
                    note = ineligible_words(parsed["reason"])
            wait = (claude.get("wait_until") or 0) - now
            if wait > 0:
                note = (note + " " if note else "") + f"The next check is allowed in about {int(wait / 60) + 1} minutes."
        pending = entry.get("pending") if (entry.get("pending") or {}).get("sent") else None
        linked = self._pending(row)
        if pending is None and linked:
            pending = linked[0][1]
        blockers = self.blockers(row, entry, now, transient=transient)
        block = assemble(rows, checked=checked, note=note, pending=pending,
                         running=pending is not None and self._running(key), blockers=blockers)
        if transient:
            block["blockers"] = blockers
        return rows, block

    def _running(self, key):
        op = self.engine.runner.current
        return op is not None and op.kind == "use-reset" and op.account == key and not op.done_event.is_set()

    def blockers(self, row, entry, now, *, transient):
        """What must change before a limit reset of this account can be used: [{"code", "message"}], the first
        decides. `transient` adds what ends by itself (another action, an open sign-in)."""
        engine, provider = self.engine, row["provider"]
        found = []

        def add(code, message):
            found.append({"code": code, "message": message})

        if not engine.owns_login:
            add("read_only", OTHER_OWNER)
        if is_frozen(row):
            add("read_only", FROZEN)
        if provider in CLAUDE and not engine.prefs.get("claude_resets"):
            add("unsupported", SETTING_OFF)
        if transient:
            if engine.runner.busy:
                add("busy", BUSY)
            if self._signing_in():
                add("busy", SIGNING_IN)
        active = row["identity"] == engine.active_identity.get(provider)
        state = row["credential_state"]
        expires_in = None if row["expires_at"] is None else row["expires_at"] - now
        health = row["flags"].get("health", "ok")
        if state == "reauth_required" or row["error_code"] == "reauth_required" or health == "needs_signin":
            add("reauth_required", NEEDS_SIGNIN)
        elif provider == "codex":
            if state == "access_expired" or expires_in is None or expires_in < CODEX_TOKEN_MARGIN:
                add("login_needed", CODEX_EXPIRING)
        elif provider == "claude":
            if not active and state == "web_only":
                add("login_needed", WEB_ONLY)
            elif (not active and state not in ("fresh", "live")) or (expires_in is not None and expires_in < CLAUDE_TOKEN_MARGIN):
                add("login_needed", CLAUDE_EXPIRED)
        elif health == "on_hold":
            add("reauth_required", NEEDS_SIGNIN)
        elif state in ("expired", "access_expired") or (expires_in is not None and expires_in < CLAUDE_TOKEN_MARGIN):
            add("login_needed", CLI_EXPIRED)
        if provider in CLAUDE and not _UUID.fullmatch(str(row["org"] or "")):
            add("unsupported", NO_ORG)
        if provider in CLAUDE:
            if any(key != row["id"] for key, _ in self._pending(row)):
                add("busy", "This Claude account has an unresolved limit reset in its other app entry. "
                            "Refresh it, or retry from the entry where you used the reset.")
            claude = entry.get("claude") or {}
            if (claude.get("wait_until") or 0) > now:
                add("rate_limited", WAIT_REFUSED if claude.get("wait_reason") == "refused" else WAIT_FAILED)
            elif claude.get("checked") is None or now - claude["checked"] > STALE_BLOCK:
                add("blocked", STALE_RESETS)
        if row["last_good_at"] is None or now - row["last_good_at"] > STALE_READING:
            add("blocked", STALE)
        return found

    def _signing_in(self):
        engine = self.engine
        if engine.login is not None:
            return True
        cli = getattr(engine.cli, "enrollment", None)
        if cli:
            return True
        agy = getattr(engine.agy, "enrollment", None)
        phase = agy.get("phase") if isinstance(agy, dict) else getattr(agy, "phase", None)
        return phase in ("signing_in", "restoring")

    # ---- reset-plan ----
    def plan(self, key):
        engine = self.engine
        now = engine.clock()
        row = self._account(key)
        rows, block = self._view(row, now, transient=True)
        if block is None:
            raise DeckError(NOT_READ, "unsupported")
        blockers = list(block["blockers"])
        if not any(item["usable"] for item in rows):
            blockers.append({"code": "blocked", "message": next((item["reason"] for item in rows if item["reason"]),
                                                                 block["note"] or (NONE_USABLE if rows else NONE_HELD))})
        usable = sorted((item for item in rows if item["usable"]),
                        key=lambda item: (item["expires"] is None, item["expires"] or 0))
        pending = self.entry(key).get("pending") or {}
        default = next((item["key"] for item in usable if item["key"] == pending.get("row")),
                       usable[0]["key"] if usable else None)
        available = block["available"]
        count = ("Its only limit reset is used. This cannot be undone." if available == 1 else
                 f"One of its {available} limit resets is used. This cannot be undone.") if available else None
        if row["provider"] == "codex":
            word = codex_plan(row["plan"])
            effects = [CODEX_EFFECT_BOTH if word in ("plus", "business", "team") else CODEX_EFFECTS[word]]
            warnings = [CODEX_80_WARNING]
        else:
            effects = [CLAUDE_EFFECT]
            warnings = [CLAUDE_SENDER_WARNING, CLAUDE_EXTRA_WARNING]
            if any(item["_grant"]["needs_limit"] for item in rows):
                warnings.append(CLAUDE_NEEDS_LIMIT_WARNING)
        effects = effects + [count] if count else []
        if pending.get("sent"):
            warnings.append(DOUBT_WARNING)
        plan = {"plan_id": "rp_" + secrets.token_hex(16), "expires": now + PLAN_TTL, "provider": row["provider"],
                "account": {"id": key, "label": row["label"] or row["email"] or key, "email": row["email"] or None,
                            "plan": row["plan"] or None},
                "choices": public(rows), "default": default, "effects": effects, "warnings": warnings,
                "blockers": blockers}
        with self.lock:
            self.plans = {pid: entry for pid, entry in self.plans.items() if entry["expires"] > now}
            self.plans[plan["plan_id"]] = {"account": key, "expires": plan["expires"],
                                           "rows": {item["key"]: _shown(item) for item in rows}}
        return plan

    def _account(self, key):
        engine = self.engine
        if not isinstance(key, str) or not key:
            raise DeckError("Choose an account.", "invalid")
        if engine.agy.owns(key):
            raise DeckError(NO_RESETS, "unsupported")
        if key in contract_unsaved_ids():
            raise DeckError(SAVE_FIRST, "invalid")
        row = engine.store.account(key)
        if row is None:
            raise DeckError("That account no longer exists.", "not_found")
        if row["provider"] not in PROVIDERS:
            raise DeckError(NOT_READ, "unsupported")
        return row

    # ---- use-reset ----
    def use(self, key, plan_id, choice):
        """Starts the use of the row `choice` the plan `plan_id` showed for `key`. Refused before anything starts when
        the plan is not this account's or has expired (`stale_plan`), when something blocks it now (its code), or
        when the row is gone or changed (`stale_plan`)."""
        engine = self.engine
        if not isinstance(plan_id, str) or not isinstance(choice, str) or not choice:
            raise DeckError("Choose a limit reset to use.", "invalid")
        if not engine.owns_login:
            raise DeckError(OTHER_OWNER, "read_only")
        row = self._account(key)
        now = engine.clock()
        with self.lock:
            entry = self.plans.get(plan_id)
            if entry is None or entry["account"] != key or entry["expires"] <= now:
                raise DeckError(STALE_PLAN, "stale_plan")
            shown = entry["rows"].get(choice)
            if shown is None:
                raise DeckError(STALE_PLAN, "stale_plan")
        rows, block = self._view(row, now, transient=True)
        if block is None:
            raise DeckError(NOT_READ, "unsupported")
        if block["blockers"]:
            first = block["blockers"][0]
            raise DeckError(first["message"], first["code"])
        chosen = next((item for item in rows if item["key"] == choice), None)
        if chosen is None or _shown(chosen) != shown:
            raise DeckError("This limit reset changed. Review it again.", "stale_plan")
        if not chosen["usable"]:
            raise DeckError(chosen["reason"] or NONE_USABLE, "blocked")
        with self.lock:
            if self.plans.pop(plan_id, None) is None:      # one use per confirmed plan
                raise DeckError(STALE_PLAN, "stale_plan")
        return engine.submit(ResetOperation(self, row, chosen))

    def run(self, op, row, chosen):
        """The operation (ResetOperation.run): the last look, the key saved, one request, the outcome."""
        engine, key = self.engine, row["id"]
        title = chosen["title"] or "limit reset"
        try:
            if row["provider"] == "codex":
                self._use_codex(op, row, chosen)
            else:
                self._use_claude(op, row, chosen)
        except DeckError as exc:
            if exc.code in ("cancelled", "needs_reconfirm") or str(exc).startswith(SAID):
                raise
            if op.sent:                    # after the request: whatever failed, the reset may be used
                raise _unknown(op, row, title) from exc
            raise _unused(op, row, title, _lead(exc), exc.code) from exc
        except Exception as exc:           # a failure nobody named, before or after the request
            diagnostics.log_exception("resets.use_failed", exc, acct=diagnostics.account_ref(key), sent=op.sent)
            if op.sent:
                raise _unknown(op, row, title) from exc
            raise _unused(op, row, title, "AccountDeck could not prepare the request.", "invalid") from exc
        finally:
            now = engine.clock()
            keys = [member["id"] for member in self._members(row)]
            with self.lock:
                self.forced.update(keys)
            # After every outcome its usage and its resets are read again: at the next tick, past the spacing of the
            # requests just made (the hour's budget still holds), as after a limit error.
            for member in keys:
                if not engine.scheduler.request_signal(member, now):
                    engine.scheduler.request(member, now)
            engine.bump()

    # -- Codex --
    def _use_codex(self, op, row, chosen):
        engine, key = self.engine, row["id"]
        codex = engine.providers["codex"]
        title = chosen["title"] or "limit reset"
        with engine.scheduler.account_lock(key), contextlib.ExitStack() as stack:
            with op.step("look", phase="verifying", message="Checking the limit reset again…"):
                profile = self._codex_login(row)
                now = engine.clock()
                remaining = token_state(profile, now).remaining
                if remaining is None or remaining < CODEX_TOKEN_MARGIN:
                    raise _unused(op, row, title, _lead(CODEX_EXPIRING), "login_needed")
                try:
                    session = stack.enter_context(codex.reset_session(profile, account=key))
                    rates = session.rates()
                except ProviderError as exc:
                    raise _unused(op, row, title, _read_failed(exc), _code(exc)) from None
                with self.lock:
                    pending = copy.deepcopy(self._entry(key).get("pending"))
                look = self._codex_look(op, row, chosen, rates, pending, engine.clock())
                if look == "settled":
                    self._store_codex(row, rates)
                    return _used(op, row, title, earlier=True)
                credit_id, before, pending = look
            with op.step("send", phase="saving", message="Using the limit reset…", cancellable=False):
                op.checkpoint()               # a Cancel that came while the step began: nothing is sent
                request = self._keep_key(row, chosen, credit_id, before, pending)
                op.sent = True
                verdict, detail = session.consume(request, credit_id)
                try:
                    after = session.rates()
                except ProviderError:
                    after = None
            # Still inside the account's lock: no reading of this account settles the request before its own answer has.
            credits = self._store_codex(row, after) if after is not None else None
            diagnostics.log("resets.use", acct=diagnostics.account_ref(key), provider="codex", verdict=verdict,
                            detail=detail)
            kept = self.entry(key).get("pending") or {}
            seen = codex_verdict(kept, credits, engine.clock()) if credits is not None and kept else None
            refusal = "unsupported" if detail != "noCredit" else "stale_plan"
            if verdict == "used":
                self._drop_key(key)
                return _used(op, row, title)
            if verdict == "unused" and pending is None:
                self._drop_key(key)
                raise _unused(op, row, title, _codex_unused(detail), refusal)
            if seen == "used":                    # the reading taken right after shows it gone
                self._drop_key(key)
                return _used(op, row, title, earlier=verdict == "unused")
            if verdict == "unused" and credits is not None and codex_still_there(kept, credits):
                # A request sent again with its open key: Codex's "nothing" speaks of this try, the reading of the reset.
                self._drop_key(key)
                raise _unused(op, row, title, _codex_unused(detail), refusal)
            raise _unknown(op, row, title)

    def _codex_login(self, row):
        """The login a use gets: Codex's own while it holds this account (its token is the newest), else the saved
        one with any interrupted renewal settled first (AccountService.codex_profile)."""
        engine = self.engine
        codex = engine.providers["codex"]
        try:
            found = codex.active_identity()
        except DeckError:
            found = None
        if found is not None and found.status == "ok" and found.identity == row["identity"]:
            try:
                live = codex.current()
            except DeckError:
                live = None
            if live is not None and identity_key(live) == row["identity"]:
                return live
        return engine.accounts.codex_profile(row)

    def _codex_look(self, op, row, chosen, rates, pending, now):
        """The last look at Codex's own answer, in the session that may use it: "settled" when the open request of
        this reset turns out to have gone through, else (credit id, available count, the open request to send again
        or None for a new one). An open request whose outcome is still not known is sent again with its own key and
        credit, or not at all. Raises DeckError (nothing used) when the chosen row has no available credit any more,
        Codex would have nothing to reset, or the look settled another reset's request."""
        resets = rates.get("rateLimitResetCredits") if isinstance(rates.get("rateLimitResetCredits"), dict) else {}
        listed = [c for c in resets.get("credits") or () if isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"]]
        count = resets.get("availableCount") if type(resets.get("availableCount")) is int else None
        available = count if count is not None else len([c for c in listed if available_credit(c)])
        title = chosen["title"] or "limit reset"
        if pending and pending.get("sent") and pending.get("provider") == "codex":
            shown = {"available": count, "credits": [{"id": c["id"], "status": c.get("status")} for c in listed]
                     if resets.get("credits") is not None else None}
            verdict = codex_verdict(pending, shown, now)
            if verdict == "used":
                self._drop_key(row["id"])
                if pending.get("row") == chosen["key"]:
                    return "settled"
                raise _unused(op, row, title, CHANGED, "stale_plan")      # another reset's: review the account again
            if verdict is None:           # still open: only this reset, only with its own key
                same = next((c for c in listed if c["id"] == pending["target"]), None)
                if pending.get("row") != chosen["key"] or same is None or same.get("status") not in (
                        None, "available", "redeeming"):
                    raise _unused(op, row, title, _lead(DOUBT_OTHER), "busy")
                return same["id"], pending["before"], pending
            self._drop_key(row["id"])     # the key's time is over: a new request gets a new key
        needed = codex_needs_reset(codex_usage(rates, now=now), codex_plan(row["plan"]))
        take_codex_resets()
        if not needed:
            raise _unused(op, row, title, _lead(CODEX_BELOW_80), "unsupported")
        if resets.get("credits") is None:
            raise _unused(op, row, title, _lead(CODEX_UNLISTED), "unsupported")
        group = chosen["_group"]
        matches = [c for c in listed if available_credit(c) and reset_group(c) == group]
        if not matches:
            raise _unused(op, row, title, GONE, "stale_plan")
        first = min(matches, key=lambda c: (reset_group(c)[0] is None, reset_group(c)[0] or 0, c["id"]))
        return first["id"], available, None

    def _store_codex(self, row, rates):
        """Codex's answer read in the use's own session, stored as the account's new reading; its credits, or None."""
        engine, key, now = self.engine, row["id"], self.engine.clock()
        try:
            current = engine.store.account(key) or row
            reading = codex_usage(rates, now=now)
            credits = take_codex_resets()
            old = current["last_good_usage"] or {}
            if reading.get("tokens") is None and isinstance(old.get("tokens"), dict):
                reading["tokens"] = old["tokens"]        # the session reads no token summary: keep the last one
            for refill in intel.detect_refill(current["last_good_usage"], reading):
                engine.store.record_refill(key, refill["window"], reset_at=refill["reset_at"],
                                           confirmed_at=refill["confirmed_at"], previous_used=refill["previous_used"])
            engine.store.observe(key, reading, at=now)
            return credits
        except Exception as exc:
            diagnostics.log_exception("resets.store_failed", exc, acct=diagnostics.account_ref(key))
            return None

    # -- Claude --
    def _use_claude(self, op, row, chosen):
        engine, key = self.engine, row["id"]
        title = chosen["title"] or "limit reset"
        grant_id = chosen["_grant"]["id"]
        # The account's lock, as a Codex use holds it: no ordinary read of this account runs under the request (it
        # could settle the doubt in the middle of it, or renew the saved token).
        with contextlib.ExitStack() as locks:
            for member in sorted(self._members(row), key=lambda item: item["id"]):
                locks.enter_context(engine.scheduler.account_lock(member["id"]))
            with op.step("look", phase="verifying", message="Checking the limit reset again…"):
                reader, claimer = self._claude_login(op, row, title)
                now = engine.clock()
                governor, gkey = engine.scheduler.governor, (row["provider"], row["identity"])
                if governor.delay(gkey, now, signal=True) > 0:
                    raise _unused(op, row, title, "AccountDeck has read this account often in the last hour. Try again "
                                                  "in a few minutes.", "rate_limited")
                governor.record(gkey, now)
                with self.lock:
                    open_before = copy.deepcopy(self._entry(key).get("pending"))
                outcome, parsed = self._fetch(reader)
                settled = self._record_read(row, outcome, parsed, now)
                if outcome != "ok":
                    raise _unused(op, row, title, "AccountDeck could not read this account's limit resets just now. "
                                                  "Try again later.", "rate_limited" if outcome == "refused" else "network")
                if settled == "used":
                    # This look shows that the earlier request went through. That was the reset the user means when it
                    # is the same one; either way nothing more is sent now: a second reset needs a new review.
                    if (open_before or {}).get("target") == grant_id:
                        return _used(op, row, title, earlier=True)
                    raise _unused(op, row, title, _stale(op, CHANGED), "stale_plan")
                with self.lock:
                    pending = copy.deepcopy(self._entry(key).get("pending"))
                if pending and pending.get("provider") in CLAUDE and pending.get("sent"):
                    if pending["target"] != grant_id or now >= pending["until"]:
                        raise _unused(op, row, title, _lead(DOUBT_OTHER), "busy")
                fresh = engine.store.account(key) or row
                rows = claude_rows(key, parsed, fresh["last_good_usage"], now)
                found = next((item for item in rows if item["_grant"]["id"] == grant_id), None)
                grant = found["_grant"] if found else None
                if not parsed["eligible"] or grant is None or grant["paused"] or grant["left"] < 1 or (
                        grant["ends"] is not None and grant["ends"] <= now):
                    raise _unused(op, row, title, _stale(op, GONE), "stale_plan")
                if not found["usable"] or _shown(found) != _shown(chosen):
                    # Also a count that is not the one confirmed.
                    raise _unused(op, row, title, _stale(op, CHANGED), "stale_plan")
            with op.step("send", phase="saving", message="Using the limit reset…", cancellable=False):
                op.checkpoint()               # a Cancel that came while the step began: nothing is sent
                # An automatic use: is it still called for, now that the look is done? (reset_auto.tick)
                if op.automatic is not None and not op.automatic["confirm"]():
                    raise _unused(op, row, title, NOT_NEEDED, "stale_plan")
                request = self._keep_key(row, chosen, grant_id, grant["left"], pending)
                governor.record(gkey, engine.clock())
                op.sent = True
                verdict, words, code = _claim_verdict(claimer, row["org"], grant_id, request, reused=pending is not None)
            # Still inside the account's lock: no reading of this account settles the request before its own answer has.
            diagnostics.log("resets.use", acct=diagnostics.account_ref(key), provider=row["provider"], verdict=verdict)
            if verdict == "used":
                self._spent(row, grant_id, grant["left"])
                return _used(op, row, title, earlier=words == "earlier")
            if verdict == "unused" and pending is None:
                self._drop_key(key)
                raise _unused(op, row, title, words, code)
            # Not known. Also every "no" to a request sent again with its open key: the sign-in, the network, a 4xx, and
            # Anthropic's `cooldown`, `not_limited` or `ineligible`, which may be the earlier request with that key
            # having gone through just now. Only `reset` and `already_used` speak of the key itself; the key stays
            # until one of them or a reading settles it.
            raise _unknown(op, row, title)

    def _claude_login(self, op, row, title):
        """(read the resets, claim one) with this account's own sign-in token: Claude Code's or Claude's live one while
        the account is in use, else the saved one. The organisation must be the account's own (a UUID it was saved
        with, and the one its sign-in names)."""
        engine, key = self.engine, row["id"]
        org = str(row["org"] or "")
        if not _UUID.fullmatch(org):
            raise _unused(op, row, title, _lead(NO_ORG), "unsupported")
        active = row["identity"] == engine.active_identity.get(row["provider"])
        if row["provider"] == "claude_cli":
            adapter = engine.cli.adapter
            record = None
            if active:
                try:                          # a live token saved under another account is not used: a sign-in
                    live = engine.cli.crosscheck(adapter.current(), drop=False)     # under way (no request is made)
                except DeckError:
                    live = None
                record = live if live is not None and live.get("identity") == row["identity"] else None
            record = record or engine.vault.get(key)
            if record.get("organizationUuid") and record["organizationUuid"] != org:
                raise _unused(op, row, title, _lead(NO_ORG), "identity")
            credentials = record.get("credentials") or {}
            try:
                adapter.reset_token(credentials)
            except DeckError as exc:
                raise _unused(op, row, title, _lead(exc), exc.code or "login_needed") from None
            fp = fingerprint(credentials)
            verified = (record.get("identity") == row["identity"] and fp
                        and record.get("fingerprint") == fp and record.get("verified") == fp)
            if not verified and engine.cli.owner(credentials) != row["identity"]:
                raise _unused(op, row, title, "AccountDeck could not confirm who owns this sign-in. "
                                              "Refresh the account before trying again.", "identity")
            return (lambda: adapter.read_resets(credentials)), (
                lambda org, grant, request: adapter.claim_reset(credentials, org, grant, request))
        claude = engine.providers["claude"]
        profile = engine.vault.get(key)
        if active:
            profile = engine.accounts._live_claude(profile, row["identity"])
        if profile.get("org") and profile["org"] != org:
            raise _unused(op, row, title, _lead(NO_ORG), "identity")
        try:
            claude.reset_token(profile)
        except DeckError as exc:
            raise _unused(op, row, title, _lead(exc), exc.code or "login_needed") from None
        return (lambda: claude.read_resets(profile, "oauth")), (
            lambda org, grant, request: claude.claim_reset(profile, org, grant, request))

    def _spent(self, row, grant_id, before):
        """Commit the known count and completed request together, including the other app's cached count."""
        with self.lock:
            for member in self._members(row):
                block = (self._entry(member["id"]).get("claude") or {}).get("block")
                for grant in (block or {}).get("grants") or ():
                    if grant["id"] == grant_id:
                        grant["left"] = min(grant["left"], before - 1)
            self._entry(row["id"]).pop("pending", None)
            self._auto_verdict(row["id"], "used", self.engine.clock())
            self._save()

    # -- the request key --
    def _keep_key(self, row, chosen, target, before, pending):
        """The request key of this use, on disk before it is sent: the open one of this reset while its outcome is
        not known, else a new UUID 4."""
        now = self.engine.clock()
        with self.lock:
            entry = self._entry(row["id"])
            if pending and pending.get("target") == target and pending.get("key"):
                kept = {**pending, "sent": True}          # `since` stays: when its outcome first was not known
            else:
                kept = {"key": str(uuid.uuid4()), "provider": row["provider"], "row": chosen["key"], "target": target,
                        "made": now, "until": now + KEY_TTL[row["provider"]], "before": before, "sent": True,
                        "since": now, "ends": chosen.get("expires")}
            earlier = entry.get("pending")
            entry["pending"] = kept
            try:
                self._save()
            except Exception:                 # not on disk, so not sent: no doubt may show for it
                if earlier is None:
                    entry.pop("pending", None)
                else:
                    entry["pending"] = earlier
                raise
            return kept["key"]

    def _drop_key(self, key):
        with self.lock:
            entry = self.memory["accounts"].get(key)
            if entry and entry.pop("pending", None) is not None:
                self._save()


class ResetOperation(Operation):
    """`use-reset`: one limit reset of one account (Resets.run). Not cancellable once the request is being sent.
    `automatic`: None for the user's own use, else what reset_auto.py gave (`Resets.use_automatically`)."""

    not_cancellable_message = "The limit reset is being sent and cannot be stopped now."

    def __init__(self, resets, row, chosen, automatic=None):
        super().__init__("use-reset", provider=row["provider"], account=row["id"], label=row["label"] or row["email"],
                         steps=(("look", "Checking the limit reset again"), ("send", "Using the limit reset")))
        self.resets, self.row, self.chosen, self.automatic = resets, row, chosen, automatic
        self.event_text = None
        self.sent = False                 # the request went out (or is about to): a failure after this is "not known"
        self.unsure = False               # it ended without knowing whether the reset was used (`_unknown`)

    def run(self):
        self.resets.run(self, self.row, self.chosen)


def codex_verdict(pending, credits, now):
    """What a Codex answer's credits (`usage.codex_reset_credits`) say of an open request: "used" (its credit is
    redeemed, or no longer there with the count down), None while the key's time runs, then "unused" (its credit is
    still available, or the count is not down) or "unknown" (the answer does not say). A count Codex did not give
    is never taken for zero, and a credit that ran out meanwhile proves nothing."""
    listed, available, before = credits.get("credits"), credits.get("available"), pending.get("before", 0)
    credit = None if listed is None else next((c for c in listed if c.get("id") == pending.get("target")), None)
    down = type(available) is int and available < before
    lapsed = pending.get("ends") is not None and now >= pending["ends"]
    if credit is not None and credit.get("status") == "redeemed":
        return "used"
    if credit is None and down and not lapsed:
        return "used"
    if now < pending.get("until", 0):
        return None
    if codex_still_there(pending, credits):
        return "unused"
    return "unknown"


def codex_still_there(pending, credits):
    """Whether Codex's answer shows the open request's credit unused: listed and available, or not listed with the
    count not down."""
    listed, available = credits.get("credits"), credits.get("available")
    credit = None if listed is None else next((c for c in listed if c.get("id") == pending.get("target")), None)
    if credit is not None:
        return credit.get("status") in (None, "available")
    return type(available) is int and available >= pending.get("before", 0)


def _claim_verdict(claimer, org, grant_id, request, *, reused):
    """("used" | "unused" | "unknown", words, code) of the one claim. Only `result: "reset"` is used (or `already_used`
    answering a key sent before, which says the earlier try went through: words "earlier"). "unused" speaks of this
    request only: for a key sent again the caller keeps the key (`_use_claude`)."""
    try:
        data, _meta = claimer(org, grant_id, request)
    except ProviderError as exc:
        status = exc.http
        if exc.meta.get("unsent"):
            return "unused", "AccountDeck could not reach Anthropic. Try again later.", "network"
        if status in (401, 403):
            return "unused", "Anthropic did not accept this account's sign-in for it. Sign in again, then try again.", \
                "reauth_required"
        if status == 429:
            return "unused", "Anthropic asked AccountDeck to wait. Try again in a moment.", "rate_limited"
        if isinstance(status, int) and 400 <= status < 500:
            return "unused", "Anthropic refused the request.", "unsupported"
        return "unknown", None, "timeout"      # a timeout, a lost answer, a 5xx: it may have been used
    except DeckError as exc:                    # refused here before anything was sent
        return "unused", _lead(str(exc) or "it could not be sent."), exc.code or "invalid"
    result = data.get("result") if isinstance(data, dict) else None
    if result == "reset":
        return "used", None, None
    if result == "already_used" and reused:
        return "used", "earlier", None
    if result == "ineligible" and isinstance(data, dict) and data.get("reason") in INELIGIBLE:
        return "unused", _lead(INELIGIBLE[data["reason"]]), "unsupported"     # e.g. "claude.ai only"
    if result in CLAIM_UNUSED:
        return "unused", CLAIM_UNUSED[result], "rate_limited" if result == "cooldown" else "unsupported"
    return "unknown", None, "timeout"   # `unavailable`, and anything this build does not know


_NAMES = ("AccountDeck", "Anthropic", "Codex", "Claude", "OpenAI")


def _lead(text):
    """`text` as the end of "No limit reset was used: …": its first letter in lower case, a name's kept."""
    text = str(text or "")
    return text if not text or text.startswith(_NAMES) else text[0].lower() + text[1:]


def _stale(op, words):
    """CHANGED or GONE as an automatic use says it: nobody reviewed anything."""
    return words if op.automatic is None else AUTO_STALE


def _used(op, row, title, *, earlier=False):
    label = op.label or "the account"
    if earlier:
        op.report["summary"] = f"The earlier {title} on {label} went through. Its limits are being read again."
        op.event_text = f"The earlier {title} on {label} went through."
    elif op.automatic is not None:
        op.event_text = f"AccountDeck used one {title} on {label} by itself: {op.automatic['why']}."
        op.report["summary"] = f"{op.event_text} Its limits are being read again."
    else:
        op.report["summary"] = f"Used one {title} on {label}. Its limits are being read again."
        op.event_text = f"Used one {title} on {label}."


def _unused(op, row, title, words, code):
    label = (op.label if op is not None else None) or row["label"] or row["email"] or "the account"
    if op is not None:
        op.event_text = f"No limit reset was used on {label}: {words}"
    return DeckError(f"No limit reset was used: {words}", code or "invalid")


def _unknown(op, row, title):
    label = op.label or "the account"
    op.unsure = True
    op.event_text = (f"AccountDeck tried one {title} on {label} by itself. It is not known yet whether it was used."
                     if op.automatic is not None else f"Not known whether the {title} on {label} was used.")
    return DeckError(NOT_KNOWN, "timeout")


def _codex_unused(detail):
    return {"nothingToReset": "Codex had nothing to reset: no limit it covers is below 80% left.",
            "noCredit": "Codex found no limit reset to use. Review the account again."}.get(
        detail, "Codex refused the request.")


def _read_failed(exc):
    if exc.kind in ("unauthorized", "needs_attention", "refresh_rejected"):
        return "Codex did not accept this account's sign-in. Sign in to Codex again, then try again."
    if exc.kind == "identity":
        return "Codex answered for a different account."
    return "AccountDeck could not read the account in Codex just now. Try again in a minute."


def _code(exc):
    return {"unauthorized": "reauth_required", "needs_attention": "reauth_required", "refresh_rejected": "reauth_required",
            "identity": "identity", "rate_limited": "rate_limited", "network": "network"}.get(exc.kind, "codex")


def _shown(row):
    """What the user saw of a row, to tell later whether it changed."""
    return {key: row.get(key) for key in ("count", "expires", "title", "restores")}


def contract_unsaved_ids():
    from . import contract
    return tuple(contract.UNSAVED_IDS.values())


def contract_unsaved(provider):
    from . import contract
    return contract.UNSAVED_IDS[provider]
