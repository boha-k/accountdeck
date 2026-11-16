"""Normalise provider usage payloads without guessing missing limits (PLAN §6.1).

Unknown stays ``None`` (never 0), every numeric entry survives in ``raw_windows`` for the
"Raw usage" view, and credits are reported, never consumed.
"""
from __future__ import annotations

import copy
import datetime as dt
import math
import re
import threading
import time

SESSION_MINUTES = 300
WEEKLY_MINUTES = 10080
PRIMARY_KINDS = frozenset({"session", "weekly", "model"})
KIND_ORDER = {"session": 0, "weekly": 1, "model": 2, "other": 3}
NON_WINDOW_KEYS = frozenset({"extra_usage", "spend", "limits", "cedar_ember"})
LOCKED = "locked"          # `blocked` of a Claude window its answer reports locked; the text is `blocked_reason`
REASON_CHARS = 200
LOCK_WITHOUT_WORDS = "Claude reports this limit locked."    # a `locked_reason` that is set but no text
LAST_TIME = 7258118400.0   # 2200-01-01: a "time" after this, or before 1970, is garbage no clock copy could print
LABEL_CHARS = 60           # the server's label of a meter is shown as it is, this long at most
PLAN_TIERS = {"default_claude_max_20x": "Ultra", "default_claude_max_5x": "Max"}
# Claude Code's words for a kept reset of each kind (`kept_resets`, `claude_reset_block`).
KEPT_TITLES = {"session": "5-hour reset", "weekly": "Weekly reset", "full": "Full reset", "fable": "Fable reset"}
RESET_TITLE_CHARS = 80      # a provider's name of a limit reset is shown this long at most (the state contract)
# What Claude Code accepts as a grant's id and as a limit key, and the reasons it knows (RS1 §B2).
_GRANT_ID = re.compile(r"[a-z0-9_-]{1,40}")
_RESET_WORD = re.compile(r"[a-z0-9_]{1,40}")
# Claude Code's own check of a plan's display name (letters and digits, inner " .+-", at most 40 characters).
_PLAN_NAME = re.compile(r"[^\W_](?:(?:[^\W_]|[ .+-]){0,38}(?:[^\W_]|\+))?")
_PLAN_CODE = re.compile(r"[a-z0-9_]{1,64}")     # a rate-limit tier or organization type as the profile names it
_TERMINAL_SURFACE = re.compile(r"(?<![a-z])(code|terminal|cli)(?![a-z])")
_LEGACY_MODEL = re.compile(r"seven_day_(opus|sonnet|haiku|fable)")
_OTHER_KEYS = {
    "seven_day_oauth_apps": ("Connected apps weekly", "Apps"),
    "seven_day_cowork": ("Cowork weekly", "Cowork"),
    "cowork": ("Cowork weekly", "Cowork"),
    "seven_day_routines": ("Routines weekly", "Routines"),
}


def number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:       # an integer too large for a float: JSON has no limit
        return None


def timestamp(value):
    """Unix seconds as a float from seconds, milliseconds or an ISO-8601 string (UTC unless it
    names an offset), within 1970 to LAST_TIME; ``None`` otherwise."""
    seconds = None
    if number(value) is not None:
        seconds = value / 1000 if value > 100000000000 else float(value)
    elif isinstance(value, str):
        try:
            moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            # Provider times are UTC: a string without an offset is read as UTC, never as local time.
            seconds = (moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)).timestamp()
        except (ValueError, OverflowError):
            return None
    return seconds if seconds is not None and 0 < seconds <= LAST_TIME else None


def title(value):
    return str(value).replace("_", " ").replace("-", " ").strip().capitalize()


def scalars(value, depth=0):
    if depth > 4:
        return None
    if isinstance(value, dict):
        return {str(k): scalars(v, depth + 1) for k, v in value.items()
                if isinstance(v, (dict, list, int, float, str, bool)) or v is None}
    if isinstance(value, list):
        return [scalars(x, depth + 1) for x in value[:30]]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def classify_key(key, value):
    """``(kind, label, short, model, primary)`` for a top-level Claude usage key, or ``None``
    when the entry carries no numeric percentage (null codenames, credit blocks, lists)."""
    if key in NON_WINDOW_KEYS or not isinstance(value, dict) or _percent(value) is None:
        return None
    kind, label, short, model, _minutes = _describe(key)
    return kind, label, short, model, kind in PRIMARY_KINDS


def plan_text(value, *, name=False):
    """`value` when it is plan data as Claude Code keeps it in `oauthAccount`: a display name its own pattern
    accepts (`name`) or a short code ("default_claude_max_20x", "claude_max"); None otherwise, so nothing else is
    saved with a login or written into Claude Code's config."""
    return value if isinstance(value, str) and (_PLAN_NAME if name else _PLAN_CODE).fullmatch(value) else None


def claude_plan(display_name, tier, organization_type):
    """The plan of a Claude account from what its login or profile says, in this order: the rate-limit tier, which
    stays when a plan is renamed (a login saved before the Oct 30 rename may still call Ultra "Max"), Claude's
    display name ("Ultra"), the organization's type ("claude_max" is "max"). None when none says."""
    if isinstance(tier, str) and tier in PLAN_TIERS:
        return PLAN_TIERS[tier]
    name = plan_text(display_name, name=True)
    if name:
        return name
    kind = _text(organization_type)
    return kind.removeprefix("claude_") or None if kind else None


def has_weekly(usage):
    """True when a normalised Claude reading reports the account-wide weekly limit: its percentage, or
    that it is locked. Every switch decision needs it, so the providers treat an answer without it as a
    failed read, never as a reading."""
    return any(w["kind"] == "weekly" and (w["used"] is not None or w["blocked"]) for w in usage.get("windows") or ())


def claude_usage(data, *, now=None):
    """The reading of one usage answer. It never raises: what cannot be read is left out, so a malformed
    answer reads as one without the weekly limit, which the providers treat as a failed read (`has_weekly`)."""
    data = data if isinstance(data, dict) else {}
    windows, raw, structured = [], [], {}
    # limits[] is the server's list of meters ("Classify a row on this, never on a label").
    rows = data.get("limits") if isinstance(data.get("limits"), list) else []
    meters = [(index, limit, _limit_shape(index, limit)) for index, limit in enumerate(rows) if isinstance(limit, dict)]
    listed = {shape[0] for _index, _limit, shape in meters}
    # A window object's non-empty `locked_reason` blocks that window whatever its percentage says. The
    # locks are gathered by window id first, so one survives the limits[] entry that replaces its alias.
    locks = {key: reason for key, value in data.items() if key not in NON_WINDOW_KEYS and isinstance(value, dict)
             and (reason := _reason(value.get("locked_reason")))}
    for key, value in data.items():
        described = classify_key(key, value)
        if described is None and key in locks:          # the lock is the reading, even without a percentage
            kind, label, short, model, _minutes = _describe(key)
            described = kind, label, short, model, kind in PRIMARY_KINDS
        if described is None:
            continue
        kind, label, short, model, _primary = described
        used, reset = _percent(value), timestamp(value.get("resets_at"))
        raw.append({"key": key, "used": used, "reset": reset})
        if meters and key not in listed and key not in locks and _no_meter(key, used, reset):
            continue
        windows.append(_window(key, kind, label, short, model, _describe(key)[4], used, reset,
                               scope=model if kind == "model" else key if kind == "other" else None))
    for index, limit, (wid, raw_key, (kind, label, short, model, minutes), gates, scope) in meters:
        used, reset, reason = _percent(limit, "percent"), timestamp(limit.get("resets_at")), _reason(limit.get("locked_reason"))
        raw.append({"key": raw_key, "used": used, "reset": reset})
        if used is None and (not reason or any(w["id"] == wid for w in windows)):
            # No percentage: a lock holds on the alias that has one; without a lock the row says nothing, and an
            # unknown meter must not make a whole reading unknown (what stopped every switch on 2026-11-01).
            if reason:
                locks.setdefault(wid, reason)
            continue
        if wid in structured:
            if (used, reset) in structured[wid]:
                continue
            structured[wid].append((used, reset))
            wid = _unique(wid, windows)
        else:
            # The structured entry replaces its legacy alias, even when the asynchronously
            # updated percentages differ slightly.
            windows = [w for w in windows if w["id"] != wid]
            structured[wid] = [(used, reset)]
        severity = limit.get("severity")
        active = limit.get("is_active")
        window = _window(wid, kind, label, short, model, minutes, used, reset,
                         group=_text(limit.get("group")), scope=scope,
                         severity_hint=severity if isinstance(severity, str) else None,
                         is_active=active if isinstance(active, bool) else None, gates=gates)
        _lock(window, reason)
        windows.append(window)
    for window in windows:
        _lock(window, locks.get(window["id"]))
    usage = {"checked": time.time() if now is None else now, "windows": _shown(windows), "raw_windows": raw,
             "credits": _claude_credits(data), "reset_credits": kept_resets(data.get("cedar_ember")), "tokens": None}
    _answer.shape = _shape(data, rows, usage)
    return usage


# ---- the shape of Claude's usage answers (the lesson of 2026-11-01: a change must never pass in silence) ----

_answer = threading.local()         # the shape of the last Claude answer this thread parsed (`take_shape`)
SHAPE_NAME_CHARS, SHAPE_NAMES = 40, 200
SHAPE_PRIME = 3600.0                # a provider's first hour of answers only teaches what its accounts look like
SHAPE_TOUCH = 600.0                 # last-seen times are kept this coarse: the setting need not change at every read
SHAPE_GAP = 3600.0                  # answers this far apart: the Mac slept, and nothing lacked a name meanwhile
SHAPE_GONE = 6 * 3600.0             # a name no answer carried for this long of reading has left the report
SHAPE_SHOWN = 7 * 86400.0           # how long the state names the last change
WEEKLESS_KEEP = 86400.0             # an answer with or without a weekly limit this long ago no longer tells anything
KNOWN_KINDS = frozenset({"session", "weekly_all", "weekly_scoped"})     # any other limits[] kind is news, once
# The top-level keys AccountDeck knows the meaning of; any other non-null one is logged by name, once a run.
_KNOWN_KEYS = frozenset({"five_hour", "seven_day", "limits", "extra_usage", "spend", "pace", "seven_day_breakdown",
                         "weekly_scoped_shares", "member_dashboard_available", "cedar_ember", *_OTHER_KEYS})


def _shape(data, rows, usage):
    """What one answer is made of, names only and never a value: its non-null top-level keys and its limits[]
    kinds with their scope's names; with whether the reading has a 5-hour window and the weekly limit."""
    names, kinds = {str(key)[:SHAPE_NAME_CHARS] for key, value in data.items() if value is not None}, set()
    for row in rows:
        if isinstance(row, dict):
            scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
            kind = _label(row.get("kind")) or _label(row.get("type")) or "unknown"
            kinds.add(kind)
            parts = [f"limits:{kind}", *(f"{part}:{name}" for part in ("model", "surface") if (name := _scope_name(scope, part)))]
            names.add(f"limits:{kind}"[:SHAPE_NAME_CHARS])
            names.add("/".join(parts)[:SHAPE_NAME_CHARS])
    return {"names": sorted(names)[:SHAPE_NAMES], "kinds": sorted(kinds)[:SHAPE_NAMES], "weekly": has_weekly(usage),
            "session": any(w["kind"] == "session" for w in usage["windows"])}


def take_shape():
    """The shape of the last Claude answer this thread parsed, once; None when it parsed none since."""
    shape, _answer.shape = getattr(_answer, "shape", None), None
    return shape


def watch_shape(memory, provider, key, seen, now):
    """(`memory` after one more answer, what is news). `memory` is the `usage_shapes` setting, per provider: the names
    of its good answers with when each was last seen, the limits[] kinds it ever saw, the last change, and per
    account what its answers showed. News: `added` / `removed` names (a provider's first hour only teaches;
    a name leaves after SHAPE_GONE of reading without it, as most keys are null most of the time), `session`
    True / False when an account gained or lost its 5-hour window (two answers in a row, so a flap says nothing;
    an account's first answer only teaches) and `unknown` keys. An answer without the weekly limit teaches nothing:
    it only marks its account (`report_status`); `seen` is when the account's answers last had one, coarsely."""
    memory = copy.deepcopy(memory) if isinstance(memory, dict) else {}
    watch = memory[provider] = memory[provider] if isinstance(memory.get(provider), dict) else {}
    accounts = watch["accounts"] = watch.get("accounts") if isinstance(watch.get("accounts"), dict) else {}
    account = accounts[key] = accounts[key] if isinstance(accounts.get(key), dict) else {}
    names = seen["names"]
    news = {"added": [], "removed": [], "session": None,
            "unknown": [name for name in names if name not in _KNOWN_KEYS and ":" not in name and not _LEGACY_MODEL.fullmatch(name)]}
    watch["kinds"] = sorted({*(watch.get("kinds") or ()), *(kind for kind in seen["kinds"] if kind not in KNOWN_KINDS)})
    if not seen["weekly"]:
        # "Gone": the names themselves left, where this account's own answers had them.
        account.update(weekless=now, gone="seen" in account and "seven_day" not in names and "limits:weekly_all" not in names)
        return memory, news
    if now - (number(account.get("seen")) or -math.inf) >= SHAPE_TOUCH:
        account["seen"] = now
    account.pop("weekless", None)
    account.pop("gone", None)
    known = watch["known"] = watch.get("known") if isinstance(watch.get("known"), dict) else {}
    since, last = watch.setdefault("since", now), watch.get("last", now)
    if now - last > SHAPE_GAP:
        known = watch["known"] = dict.fromkeys(known, now)
    added = sorted(name for name in names if name not in known)
    known.update((name, now) for name in names if now - known.get(name, -math.inf) >= SHAPE_TOUCH)
    removed = sorted(name for name, at in known.items() if name not in names and now - at >= SHAPE_GONE)
    for name in removed:
        del known[name]
    if now - last >= SHAPE_TOUCH or "last" not in watch:
        watch["last"] = now
    if (added or removed) and now - since >= SHAPE_PRIME:
        news.update(added=added, removed=removed)
        watch["changed"] = {"changed_at": now, "added": added, "removed": removed}
    if "session" not in account or seen["session"] == account["session"]:
        account["session"] = seen["session"]
        account.pop("flip", None)
    elif account.get("flip"):
        account["session"], news["session"] = seen["session"], seen["session"]
        account.pop("flip", None)
    else:
        account["flip"] = True
    return memory, news


def shape_change(memory, provider, now):
    """The last change in `provider`'s answers for the state: {"changed_at", "added", "removed"}, or None when
    there was none in the last SHAPE_SHOWN."""
    watch = memory.get(provider) if isinstance(memory, dict) else None
    changed = watch.get("changed") if isinstance(watch, dict) else None
    return dict(changed) if isinstance(changed, dict) and now - changed.get("changed_at", -math.inf) <= SHAPE_SHOWN else None


def report_status(memory, now):
    """What the alert engine needs of `usage_shapes`. `down`: Claude's answers no longer tell the limits, per
    provider (one account's Desktop and terminal rows are not two accounts): two or more accounts answering without
    a weekly limit, or one whose answer lost both `seven_day` and the `weekly_all` row while no other account's
    answer still has them (one account alone is that account's plan, as a lapsed one's answer may look; its
    row says so). `accounts`: how many answer so where it is down. `kinds`: every limits[] kind AccountDeck has
    no rule for."""
    down, count, kinds = False, 0, set()
    for watch in memory.values() if isinstance(memory, dict) else ():
        accounts = watch.get("accounts") if isinstance(watch, dict) else None
        kinds.update((watch.get("kinds") or ()) if isinstance(watch, dict) else ())
        weekless, gone, answered = 0, False, False
        for account in accounts.values() if isinstance(accounts, dict) else ():
            account = account if isinstance(account, dict) else {}
            if now - (number(account.get("weekless")) or -math.inf) <= WEEKLESS_KEEP:
                weekless += 1
                gone = gone or bool(account.get("gone"))
            elif now - (number(account.get("seen")) or -math.inf) <= WEEKLESS_KEEP:
                answered = True
        if weekless >= 2 or (gone and not answered):
            down, count = True, count + weekless
    return {"down": down, "accounts": count, "kinds": sorted(kinds)}


def codex_usage(data, tokens=None, *, now=None):
    windows, raw, credits = [], [], []
    buckets = data.get("rateLimitsByLimitId") if isinstance(data.get("rateLimitsByLimitId"), dict) else {}
    default = data.get("rateLimits") if isinstance(data.get("rateLimits"), dict) else None
    if not buckets and default is not None:
        buckets = {default.get("limitId") or "codex": default}
    main = default.get("limitId") if default is not None else None
    if main not in buckets:
        main = "codex" if "codex" in buckets else None
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        group = _text(bucket.get("limitName")) or key
        name = _text(bucket.get("limitName")) or ("Codex" if key == "codex" else title(key))
        own = []
        for slot in ("primary", "secondary"):
            value = bucket.get(slot)
            if not isinstance(value, dict):
                continue
            used, reset = _float(value.get("usedPercent")), timestamp(value.get("resetsAt"))
            minutes = number(value.get("windowDurationMins"))
            minutes = minutes if minutes is not None and minutes > 0 else None
            wid = f"{key}:{slot}"      # v1 ids, so history and alert latches carry over
            raw.append({"key": wid, "used": used, "reset": reset})
            span, short = _span_label(minutes, slot), _span_short(minutes, slot)
            if key == main:
                # The default bucket always gates: primary → session, secondary → weekly,
                # unless the reported duration says otherwise.
                weekly = minutes == WEEKLY_MINUTES or (minutes != SESSION_MINUTES and slot == "secondary")
                kind, label, model = ("weekly" if weekly else "session"), span, None
            else:
                kind = "model" if used is not None else "other"
                label, short = f"{name} {span[0].lower()}{span[1:]}", f"{name} {short}"
                model = name if kind == "model" else None
            own.append(_window(wid, kind, label, short, model, minutes, used, reset,
                               group=group, scope=None if key == main else group))
        reached = _text(bucket.get("rateLimitReachedType"))
        # ordinaryUsageAllowed=false is authoritative for the default bucket even without a reached type.
        blocked = reached or ("not_allowed" if key == main and data.get("ordinaryUsageAllowed") is False else None)
        if blocked:
            full = [w for w in own if w["used"] is not None and w["used"] >= 100]
            for window in full or own:
                window["blocked"] = blocked
        windows.extend(own)
        credit = bucket.get("credits")
        if isinstance(credit, dict):
            credits.append({"label": f"{name} credits", "balance": credit.get("balance"),
                            "unlimited": credit.get("unlimited"), "hasCredits": credit.get("hasCredits")})
        individual = bucket.get("individualLimit")
        if isinstance(individual, dict):
            # Extra allowances are preserved as details, never flattened into a false percentage.
            credits.append({"label": f"{name} allowance", "details": scalars(individual)})
    resets = data.get("rateLimitResetCredits")
    if resets is not None:
        credits.append({"label": "Limit reset credits", "details": _without_ids(scalars(resets)) if isinstance(resets, dict)
                        else resets})
    # What a use needs of each credit (its id above all) goes to the service on the side (`take_codex_resets`), never
    # into the reading: a reading reaches the state, the history and the database's snapshots.
    _answer.codex_resets = codex_reset_credits(resets)
    allowed = data.get("ordinaryUsageAllowed")
    return {"checked": time.time() if now is None else now, "windows": _shown(windows), "raw_windows": raw,
            "credits": credits, "reset_credits": reset_credits(resets),
            "ordinaryUsageAllowed": allowed if isinstance(allowed, bool) else None,
            "tokens": scalars((tokens or {}).get("summary") or {}) or None}


def reset_credits(resets):
    """Available Codex limit-reset credits grouped by expiry: ``[{"count","expires","title"}]``.
    Credits counted by ``availableCount`` but not listed become one row without an expiry."""
    if not isinstance(resets, dict):
        return []
    listed = [c for c in resets.get("credits") or [] if available_credit(c)]
    groups = {}
    for credit in listed:
        key = reset_group(credit)
        groups[key] = groups.get(key, 0) + 1
    rows = [{"count": count, "expires": expires, "title": label} for (expires, label), count in groups.items()]
    rows.sort(key=lambda row: (row["expires"] is None, row["expires"] or 0))
    available = number(resets.get("availableCount"))
    if available is not None and int(available) > len(listed):
        rows.append({"count": int(available) - len(listed), "expires": None, "title": None})
    return rows


def available_credit(credit):
    """Whether one of Codex's listed reset credits counts as available (`reset_credits`)."""
    return isinstance(credit, dict) and credit.get("status") in (None, "available")


def reset_group(credit):
    """The row of `reset_credits` a listed Codex credit belongs to: (expires, title)."""
    return timestamp(credit.get("expiresAt")), _text(credit.get("title"))


def codex_reset_credits(resets):
    """What a use needs of Codex's `rateLimitResetCredits`: {"available": its availableCount (int or None),
    "credits": [{"id", "status", "expires", "granted", "title", "description", "type"}] or None when Codex listed
    none (only the count is known then)}; None when the answer carried no such object. Inside the service only."""
    if not isinstance(resets, dict):
        return None
    available = number(resets.get("availableCount"))
    listed = resets.get("credits")
    credits = None
    if isinstance(listed, list):
        credits = []
        for credit in listed[:50]:
            if not isinstance(credit, dict) or not isinstance(credit.get("id"), str) or not credit["id"].strip():
                continue
            status = credit.get("status")
            credits.append({"id": credit["id"].strip()[:200], "status": status if isinstance(status, str) else None,
                            "expires": timestamp(credit.get("expiresAt")), "granted": timestamp(credit.get("grantedAt")),
                            "title": _reset_title(credit.get("title")), "description": _reset_title(credit.get("description")),
                            "type": _text(credit.get("resetType"))})
    return {"available": int(available) if available is not None and available >= 0 else None, "credits": credits}


def take_codex_resets():
    """The reset credits of the last Codex answer this thread parsed (`codex_reset_credits`), once; None when it
    parsed none since, or that answer carried none."""
    found, _answer.codex_resets = getattr(_answer, "codex_resets", None), None
    return found


def _without_ids(details):
    """Codex's reset credits as the "Limit reset credits" details row shows them: without the credits' ids."""
    if isinstance(details, dict) and isinstance(details.get("credits"), list):
        details["credits"] = [{key: value for key, value in credit.items() if key != "id"} if isinstance(credit, dict)
                              else credit for credit in details["credits"]]
    return details


def claude_reset_block(block):
    """Claude's `cedar_ember` block as the service keeps it, or None when it is not one (no object, no `eligible`):
    {"eligible", "reason" (`ineligible_reason`, a plain word or None), "at_limit", "exhausted" (limit keys),
    "cooldown_until", "next" (`next_grant_id` when it names a listed grant), "grants": [{"id", "kind" (session,
    weekly, full or fable), "label", "left", "total", "starts", "ends", "paused", "usable_now" (None when not said),
    "needs_limit", "clears", "blocking", "unavailable"}]}. Every grant with a valid id and a `resets_left` is kept,
    spent and expired ones too: a doubt is settled by comparing counts (resets.py). Claude Code's own rules (RS1
    §B2, §0b): a grant without a valid id is dropped, `use_requires_limit` is true unless it says false, and a
    missing `type` comes from `clears`."""
    if not isinstance(block, dict) or not isinstance(block.get("eligible"), bool):
        return None
    grants = []
    for grant in block.get("grants") if isinstance(block.get("grants"), list) else ():
        if not isinstance(grant, dict) or not isinstance(grant.get("id"), str) or not _GRANT_ID.fullmatch(grant["id"]):
            continue
        left = grant.get("resets_left")
        if type(left) is not int or left < 0:
            continue
        total = grant.get("resets_total")
        clears = _reset_words(grant.get("clears"))
        kind = grant.get("type") if grant.get("type") in ("session", "weekly", "full") else None
        if grant.get("gives_back_weekly") is True:
            kind = "fable"
        elif kind is None:
            kind = "full" if "five_hour" in clears and len(clears) > 1 else "session" if "five_hour" in clears else "weekly"
        usable = grant.get("usable_now")
        reason = grant.get("unavailable_reason")
        grants.append({"id": grant["id"], "kind": kind, "label": _reset_title(grant.get("label")), "left": left,
                       "total": total if type(total) is int and total >= 0 else None,
                       "starts": timestamp(grant.get("starts_at")), "ends": timestamp(grant.get("ends_at")),
                       "paused": grant.get("paused") is True, "usable_now": usable if isinstance(usable, bool) else None,
                       "needs_limit": grant.get("use_requires_limit") is not False, "clears": clears,
                       "blocking": _reset_words(grant.get("blocking")),
                       "unavailable": reason if isinstance(reason, str) and _RESET_WORD.fullmatch(reason) else None})
    reason = block.get("ineligible_reason")
    listed = {grant["id"] for grant in grants}
    return {"eligible": block["eligible"], "reason": reason if isinstance(reason, str) and _RESET_WORD.fullmatch(reason) else None,
            "at_limit": block.get("at_limit") is True, "exhausted": _reset_words(block.get("exhausted")),
            "cooldown_until": timestamp(block.get("cooldown_until")),
            "next": block.get("next_grant_id") if block.get("next_grant_id") in listed else None, "grants": grants}


def reset_rows(parsed, now):
    """The grants of a parsed block (`claude_reset_block`) that are shown as rows: not expired and with at least one
    reset left (paused ones too: they show why they cannot be used), the one that expires first first."""
    grants = [grant for grant in (parsed or {}).get("grants") or ()
              if grant["left"] >= 1 and (grant["ends"] is None or grant["ends"] > now)]
    return sorted(grants, key=lambda grant: (grant["ends"] is None, grant["ends"] or 0, grant["id"]))


def kept_resets(block, now=None):
    """The limit resets a Claude account keeps ("Resets kept" in Claude Code), per grant and as `reset_credits`
    has them: ``[{"count","expires","title"}]``. Only Claude's separate resets read (`?cedar_ember=1&skip_spend=1`,
    resets.py) gets the block: the ordinary usage answer has `"cedar_ember": null`, and Claude Code never keeps the
    grants on disk (its cache holds the plain answer)."""
    now = time.time() if now is None else now
    return [{"count": grant["left"], "expires": grant["ends"], "title": grant["label"] or KEPT_TITLES[grant["kind"]]}
            for grant in reset_rows(claude_reset_block(block), now)]


def _reset_words(value):
    return [word for word in value[:20] if isinstance(word, str) and _RESET_WORD.fullmatch(word)] if isinstance(value, list) else []


def _reset_title(value):
    return (_plain(value) or "")[:RESET_TITLE_CHARS] or None


def _window(wid, kind, label, short, model, minutes, used, reset, *, group=None, scope=None,
            severity_hint=None, is_active=None, gates=False):
    return {"id": wid, "kind": kind, "label": label, "short": short, "model": model,
            "used": used, "known": used is not None, "reset": reset, "minutes": minutes,
            # A window at 0 % without a reset time has not started (it starts with the next message).
            "started": not (used == 0 and reset is None), "blocked": None, "blocked_reason": None,
            "group": group, "scope": scope, "primary": kind in PRIMARY_KINDS,
            "severity_hint": severity_hint, "is_active": is_active,
            "gates": gates}     # a meter of kind "other" that holds terminals (`_limit_shape`, rules.holds_terminals)


def _lock(window, reason):
    """Marks a Claude window locked (S3b): `blocked` gets the stable LOCKED code, the provider's words
    stay in `blocked_reason`. extra_usage.spend_limit_reached is not a lock: it is about paid usage
    credits, not the plan's limits."""
    if reason and window["blocked"] is None:
        window["blocked"], window["blocked_reason"] = LOCKED, reason


def _shown(windows):
    """Primary windows always; others only when used > 0, a reset exists (D2) or a lock says something."""
    kept = [w for w in windows if w["primary"] or (w["used"] or 0) > 0 or w["reset"] is not None or w["blocked"]]
    return sorted(kept, key=lambda w: (KIND_ORDER[w["kind"]], w["label"].casefold()))


def _no_meter(key, used, reset):
    """Whether a top-level window that a non-empty limits[] does not list is no limit of the account: a
    codename, whatever its percentage (`iguana_necktie` today; `raw_windows` keeps it), or a 5-hour window at
    0 % without a reset, which says nothing and would read as a temporary limit (rules.temporary_limit)."""
    if key == "five_hour":
        return used == 0 and reset is None
    return _describe(key)[0] == "other" and key not in _OTHER_KEYS


def model_label(model):
    """How a per-model limit is named: the bare "Fable" limit is Fable 5.1's."""
    return "Fable 5.1" if model == "Fable" else model


def _describe(key):
    if key == "five_hour":
        return "session", "5-hour", "5h", None, SESSION_MINUTES
    if key == "seven_day":
        return "weekly", "Weekly", "7d", None, WEEKLY_MINUTES
    legacy = _LEGACY_MODEL.fullmatch(key)
    if legacy:
        model = legacy.group(1).capitalize()
        return "model", f"{model_label(model)} weekly", model_label(model), model, WEEKLY_MINUTES
    if key in _OTHER_KEYS:
        label, short = _OTHER_KEYS[key]
        return "other", label, short, None, WEEKLY_MINUTES
    return "other", _label(title(key)) or "Other", "Other", None, None


def _scope_name(scope, key):
    """The server's label of a limits[] row's model or surface scope, or None when it has none."""
    part = scope.get(key)
    return _label(part.get("display_name")) or _label(part.get("id")) if isinstance(part, dict) else _label(part)


def _limit_shape(index, limit):
    """(window id, raw key, (kind, label, short, model, minutes), gates, scope) for one ``limits[]`` entry.
    `gates`: a meter of no known kind, or scoped to the terminals' own surface, keeps the server's label and
    holds terminals, so a new meter needs no release; one scoped to another surface does not, and one scoped
    to a model (its name in `scope`) only while that model is pinned (rules.holds_terminals)."""
    kind = _label(limit.get("kind")) or _label(limit.get("type"))
    scope = limit.get("scope") if isinstance(limit.get("scope"), dict) else {}
    model_name, surface = _scope_name(scope, "model"), _scope_name(scope, "surface")
    base = f"limits[{index}].{kind or 'unknown'}"
    if kind == "session" and not (model_name or surface):
        return "five_hour", base, _describe("five_hour"), False, None
    if kind == "weekly_all" and not (model_name or surface):
        return "seven_day", base, _describe("seven_day"), False, None
    here = surface is None or _TERMINAL_SURFACE.search(surface.casefold()) is not None
    if kind == "weekly_scoped" and model_name and surface is None:
        slug = _slug(model_name)
        label = model_label(model_name)
        return (f"seven_day_{slug}", f"{base}.{slug}", ("model", f"{label} weekly", label, model_name, WEEKLY_MINUTES),
                False, model_name)
    if kind == "weekly_scoped" and surface and not model_name:
        key = f"seven_day_{_slug(surface)}"
        shape = _describe(key) if key in _OTHER_KEYS else ("other", f"{surface} weekly", surface, None, WEEKLY_MINUTES)
        return key, f"{base}.{_slug(surface)}", shape, here and key not in _OTHER_KEYS, None
    named = " ".join(filter(None, (model_name, surface)))
    group = _label(limit.get("group"))
    label = (_label(limit.get("display_name")) or _label(limit.get("name"))
             or (f"{named} {group}" if named and group else named) or title(kind or f"limit {index + 1}"))
    wid = _slug("_".join(filter(None, (_text(limit.get("id")) or kind or f"limit_{index + 1}", named))))
    return wid, base, ("other", label, "Other", None, None), here, model_name


def _span_label(minutes, slot):
    if minutes is None:
        return "5-hour" if slot == "primary" else "Weekly"
    if minutes == SESSION_MINUTES:
        return "5-hour"
    if minutes == WEEKLY_MINUTES:
        return "Weekly"
    if minutes == 1440:
        return "Daily"
    if minutes % 1440 == 0:
        return f"{minutes // 1440:g}-day"
    if minutes % 60 == 0:
        return f"{minutes // 60:g}-hour"
    return f"{minutes:g}-minute"


def _span_short(minutes, slot):
    if minutes is None:
        return "5h" if slot == "primary" else "7d"
    if minutes == WEEKLY_MINUTES:
        return "7d"
    if minutes % 1440 == 0:
        return f"{minutes // 1440:g}d"
    if minutes % 60 == 0:
        return f"{minutes // 60:g}h"
    return f"{minutes:g}m"


def _claude_credits(data):
    credits = []
    spend, extra = data.get("spend"), data.get("extra_usage")
    if isinstance(spend, dict):
        amounts = [spend.get(k) if isinstance(spend.get(k), dict) else None for k in ("used", "limit", "balance")]
        money = next((a for a in amounts if a is not None), {})
        credits.append({"label": "Usage credits", "enabled": spend.get("enabled"),
                        "used": _minor(amounts[0]), "limit": _minor(amounts[1]), "balance": _minor(amounts[2]),
                        "unit": "minor currency units", "exponent": _exponent(money.get("exponent")),
                        "currency": _text(money.get("currency")) or "USD", "percent": number(spend.get("percent"))})
    elif isinstance(extra, dict):
        credits.append({"label": "Usage credits", "enabled": extra.get("is_enabled"),
                        "used": number(extra.get("used_credits")), "limit": number(extra.get("monthly_limit")),
                        "balance": number(extra.get("balance")), "unit": "minor currency units",
                        "exponent": _exponent(extra.get("decimal_places")),
                        "currency": _text(extra.get("currency")) or "USD", "percent": number(extra.get("utilization")),
                        "reset": timestamp(extra.get("resets_at"))})
    if isinstance(spend, dict):
        for key, label in (("cap", "Usage credit cap"), ("auto_reload", "Automatic credit reload")):
            if spend.get(key) is not None:
                credits.append({"label": label, "details": scalars(spend[key])})
    if isinstance(extra, dict):
        for key, label in (("daily", "Daily credit allowance"), ("weekly", "Weekly credit allowance")):
            if extra.get(key) is not None:
                credits.append({"label": label, "details": scalars(extra[key])})
    return credits


def _percent(value, first="utilization"):
    """The percentage of a window object, or of a limits[] row (`first` "percent", the name rows carry), under
    either name."""
    used = _float(value.get(first))
    return _float(value.get("percent" if first == "utilization" else "utilization")) if used is None else used


def _float(value):
    value = number(value)
    return None if value is None else float(value)


def _minor(amount):
    return number(amount.get("amount_minor")) if isinstance(amount, dict) else None


def _exponent(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6 else 2


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _plain(value):
    """Provider text as plain text (printable, single-spaced); None unless a non-empty string."""
    if not isinstance(value, str):
        return None
    return " ".join("".join(ch if ch.isprintable() else " " for ch in value).split()) or None


def _label(value):
    return (_plain(value) or "")[:LABEL_CHARS] or None


def _reason(value):
    """A lock reason, at most REASON_CHARS; None when no lock is reported. A reason that is set but no text (an
    object, a flag) still reports a lock: a lock wrongly taken for none would send the terminals to a locked account."""
    if not isinstance(value, str):
        return LOCK_WITHOUT_WORDS if value else None
    return (_plain(value) or "")[:REASON_CHARS] or None


def _slug(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_") or "unknown"


def _unique(wid, windows):
    taken = {w["id"] for w in windows}
    suffix = 2
    while f"{wid}_{suffix}" in taken:
        suffix += 1
    return f"{wid}_{suffix}"
