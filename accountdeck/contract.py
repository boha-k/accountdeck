"""The /api/state contract (PLAN §3.3-3.7) as code: its enums and the validator the engine runs on every
state it builds (an assertion in safe mode and the demo, one log line in live mode).

The rules the validator checks against (`accountdeck.rules`) are the ones the engine's intel applies, so a
snapshot that follows the rules validates by construction. Example payloads live in `accountdeck.fixtures`.
"""
from __future__ import annotations

import copy
import math
import re

from . import rules, updates
from .updates import STATUSES as UPDATE_STATUSES
# Re-exported for older importers (platform/menubar.py); new code imports accountdeck.rules.
from .rules import (HIDDEN_STATES, PRIMARY_KINDS, REFILL_KEEP, STRIP_MIN_USED, age_text, clock_text,  # noqa: F401
                    countdown_text, js_round as _round, phrase as _phrase)
from .rules import ATTENTION_KINDS, DORMANT_DAYS  # noqa: F401  (enum and threshold the dashboard shares)

# ---- frozen enums (PLAN §3) ----
MODES = ("live", "demo")
# Every app AccountDeck knows: the keys of `providers`, and what `pending`, `report`, `banners` and plans name.
PROVIDERS = ("claude", "codex", "claude_cli", "antigravity")
# The apps whose accounts are rows of `accounts`, which the scheduler reads, the recommender ranks (`now_band.best`)
# and alerts watch. Antigravity is not one: its accounts live in `providers.antigravity` and nothing automatic
# touches them (no polling, no automatic switch, no alert, no forecast).
ACCOUNT_PROVIDERS = ("claude", "codex", "claude_cli")
STATUS_LEVELS = ("ok", "degraded", "outage", "unknown")
STATUS_SOURCES = ("statuspage", "observed", "none")
ACCOUNT_STATES = ("ok", "limited", "stale", "needs_signin", "error", "unknown")
HEADLINE_KINDS = ("ready", "watch", "low", "critical", "out", "signin", "stale", "checking", "unknown", "outage")
# access_expired: Codex access token lapsed while the client runs (PLAN §5.11); not an error.
CREDENTIAL_STATES = ("live", "fresh", "expired", "web_only", "reauth_required", "access_expired", "unknown")
AGE_TIERS = ("fresh", "aging", "stale")
OWNERS = ("v2", "v2-pending", "v1", "moved", "on_pc")      # core.OWNERS; only "v2" and "on_pc" occur on a Mac
WINDOW_KINDS = ("session", "weekly", "model", "other")
WINDOW_STATES = ("not_started", "live", "last_known", "resetting", "refilled", "expected_refill", "out")
SEVERITIES = ("plenty", "watch", "low", "critical", "out", "unknown")
RESET_STATES = ("scheduled", "passed", "none")
PACE_STAGES = ("on_pace", "slightly_ahead", "ahead", "far_ahead", "slightly_under", "under", "far_under", "too_early")
NEXT_RESET_KINDS = ("next_reset", "refilled", "all_clear", "unknown", "empty")
BEST_STATES = ("ok", "unknown", "all_exhausted", "paused", "none")
POLICIES = ("availability", "use_soon")
CONFIDENCES = ("high", "low")
CHIPS = ("back_in", "use_it", "plain", "refilled")
BUCKETS = ("refilled", "minute", "hour", "six_hours", "day")
PHASES = ("queued", "planning", "waiting_idle", "stopping", "settling", "saving", "restoring", "launching",
          "verifying", "done", "unconfirmed", "failed", "cancelled", "needs_reconfirm")
STEP_STATUSES = ("pending", "active", "done", "skipped", "warning", "failed")
OUTCOMES = ("success", "failed", "cancelled")
VERIFICATIONS = ("verified", "unconfirmed", "failed")
STOP_TIERS = ("not_running", "graceful", "managed", "manual")
WATCH_KINDS = ("session", "weekly", "model", "auth")
SIGNIN_STATES = ("needs_signin", "ended", "soon")
ALERT_KINDS = ("low", "limit_reached", "reset_confirmed", "reset_soon", "suggest_switch", "credential_expired",
               "switch_result", "use_it", "service")
WATCHDOG_STATES = ("off", "watching", "braked", "stopping")
DB_CHECKS = ("ok", "damaged", "error")
BANNER_KINDS =("outage", "degraded", "read_only", "recovery", "demo", "safe_mode", "paused", "self_protect",
                "move_import")
HEALTH_LEVELS = ("ok", "warn", "fail")      # a doctor check (`state.health`)
PURPOSES = ("switch", "quit", "add", "finish-login", "cancel-login", "recover", "capture")
CONSENTS = ("idle", "now")
DEFAULT_BUTTONS = ("idle", "now")
LEVELS = ("info", "good", "warning", "critical")
SESSION_STATUSES = ("busy", "idle", "unknown")
# Codex's consumers (`providers.codex.running`, a plan's `codex_consumers`) and Antigravity's ("agy": any process of
# the agy command-line tool, `providers.antigravity.running`, a plan's `agy_consumers`).
CODEX_CONSUMER_KINDS = ("tui", "app_server_child", "desktop")
AGY_CONSUMER_KINDS = ("agy",)
CONSUMER_KINDS = CODEX_CONSUMER_KINDS + AGY_CONSUMER_KINDS
# "system": macOS refused something (a launch, a folder, disk space). "antigravity": agy answered something
# AccountDeck cannot read; "notice_required": an Antigravity Save, Add or Switch before the notice was accepted.
ERROR_CODES = ("invalid", "not_found", "busy", "login_needed", "reauth_required", "blocked", "identity", "unsupported",
               "network", "rate_limited", "timeout", "vault", "recovery", "not_installed", "system", "codex",
               "safety", "cancelled", "confirm_required", "needs_reconfirm", "stale_plan", "outage", "read_only",
               "antigravity", "notice_required")
PROVIDER_FAILURES = ("token_refused", "blocked", "scope_missing", "web_session_stale", "rate_limited", "outage", "network")
ACTIONS = ("refresh", "rename", "reorder", "switch-plan", "switch", "quit-claude", "cancel", "dismiss", "dismiss-alert",
           "snooze-alert", "dismiss-report", "capture", "add", "finish-login", "cancel-login", "remove", "open",
           "stop-consumer", "recover", "verify", "open-folder", "test-notification", "cli-exclude", "cli-undo",
           "demo-clock", "import-bundle-preview", "import-bundle", "import-bundle-dismiss",
           "check-update", "skip-update", "start-update", "agy-accept-notice", "reset-plan", "use-reset")
INTERNAL_SETTINGS = ("recommender_state", "alert_latches", "update_memory", "agy_notice_seen")  # = config's internal keys
UNSAVED_IDS = {"claude": "unsaved-claude", "codex": "unsaved-codex", "antigravity": "unsaved-antigravity"}
# Claude switch step ids in order (PLAN §5.4).
CLAUDE_SWITCH_STEPS = ("prepare", "wait", "stop", "settle", "capture", "restore", "sessions", "launch", "verify", "report")
MOVE_IMPORT_OUTCOMES = ("new", "updated", "filled", "kept", "on_pc", "deferred", "failed")

_SETTING_RULES = {
    "cli_autoswitch": ("bool",), "cli_threshold": ("int", 50, 99),
    "cli_order": ("choice", ("soonest_reset", "most_room", "manual")), "cli_manual_order": ("ids",),
    "cli_early_switch": ("bool",),
    "usage_display": ("choice", ("used", "left")), "confirm_when_idle": ("bool",),
    "sort": ("choice", ("stable", "best", "resets", "attention", "name", "manual")),
    "density": ("choice", ("auto", "cards", "rows")), "pinned_model": ("text",), "use_soon": ("bool",),
    "show_other_limits": ("bool",), "cookie_route": ("bool",), "alert_low_left": ("int", 5, 25),
    "alerts_enabled": ("bool",), "quiet_hours": ("hours",), "notify_reset_minutes": ("int", 1, 180),
    "notifications": ("bool",), "tray": ("bool",), "start_on_login": ("bool",), "cwd": ("text",),
    "log_level": ("choice", ("debug", "info", "warning")), "refresh_paused": ("bool",), "update_check": ("bool",),
    "agy_usage_on_open": ("bool",), "claude_resets": ("bool",), "auto_reset": ("bool",),
}
# Keys that would leak identities or secrets to the browser (PLAN §3.3: uuids never leave the server).
FORBIDDEN_KEYS = frozenset({"identity", "org", "org_uuid", "organization_uuid", "account_uuid", "uuid", "token",
                            "access_token", "refresh_token", "id_token", "cookie", "cookies", "sessionkey",
                            "session_key", "password", "secret", "accesstoken", "refreshtoken", "claudeaioauth",
                            "trusteddevicetoken", "oauthaccount", "credentials"})

# ---- limit resets (`accounts[].reset_credits`, `accounts[].resets`, the `reset-plan` answer; resets.py) ----
RESET_PROVIDERS = ("claude", "claude_cli", "codex")       # the apps whose accounts may hold limit resets
RESET_TITLE_MAX = 80
RESET_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")
RESET_PLAN_ID = re.compile(r"rp_[0-9a-f]{32}")
_RESET_ROW_KEYS = ("count", "expires", "title", "key", "restores", "usable", "reason")
_RESETS_KEYS = ("checked", "available", "can_use", "blocked", "note", "doubt")
_RESET_PLAN_KEYS = ("plan_id", "expires", "provider", "account", "choices", "default", "effects", "warnings", "blockers")

# ---- Antigravity (`providers.antigravity`; the shapes are written out in its validator, `_check_agy`) ----
AGY_STORE_STATES = ("absent", "ok", "unknown")
# Why agy's sign-in store reads as unknown, which blocks Save, Add and Switch: the keychain item cannot be read
# (locked, refused, timed out), its value does not decode, the record lacks a refresh token or an e-mail, it is
# not a personal Google sign-in (auth_method "consumer"), agy keeps its sign-in in a file on this Mac instead,
# or nothing has been checked yet.
AGY_STORE_REASONS = ("unreadable", "undecodable", "incomplete", "not_consumer", "file_store", "not_checked")
# An Add waiting for a sign-in ("signing_in"), ending without one and waiting to put the earlier sign-in back
# ("restoring": until no agy runs and the keychain answers), or ended ("saved", "cancelled", "timed_out", "failed").
AGY_ENROLLMENT_PHASES = ("signing_in", "saved", "cancelled", "timed_out", "failed", "restoring")
# What Recover may put in agy's keychain item after a switch that could not finish or undo itself: the record
# from before the switch ("before"; none: the item is deleted), the target's ("after"), or what is there now ("keep").
AGY_RECOVERY_CHOICES = ("before", "after", "keep")
AGY_TEXT_MAX = 200              # every text of a usage reading is cut to this (providers.antigravity.parse_usage)
AGY_GROUPS_MAX = AGY_BUCKETS_MAX = 8
_AGY_KEYS = ("installed", "store", "signed_in", "accounts", "refresh", "enrollment", "recovery", "notice_seen",
             "t3_code")
_AGY_ACCOUNT_KEYS = ("id", "label", "email", "plan", "saved", "active", "switchable", "usage", "error")
_AGY_BUCKET_KEYS = ("id", "name", "window", "remaining", "amount", "resets_at", "disabled")
# Key names that would mean a part of agy's sign-in record reached the browser: the record's own fields, and any
# key naming a token, secret, password, cookie or credential (`_check_agy_secrets`).
AGY_RECORD_KEYS = frozenset({"record", "records", "stores", "keychain", "access_token", "token_type", "refresh_token",
                             "expiry", "auth_method", "id_token_email", "user_tier", "tier_display_name", "project_id",
                             "writer_id", "wif_provider", "saved_wif_provider", "aicode_scope_sign_out_attempted_at"})
_AGY_SECRET_PARTS = ("token", "secret", "password", "cookie", "credential")
_AGY_SECRET_MARK = "go-keyring-base64:"          # how agy's keychain value starts
# The actions the dashboard sends for Antigravity (POST /api/action), with the keys of their bodies ("?": optional).
# "provider": "antigravity" routes one that names no Antigravity account id. Answers and refusals as for the other
# apps: {} or {"op": id} (an operation, shown as `pending`), {"plan": …} for switch-plan, a DeckError otherwise.
AGY_ACTIONS = {
    "refresh": ("provider",),                                  # run agy's usage command for the signed-in account
    "capture": ("provider",),                                  # Save: the signed-in account becomes a saved one
    "switch-plan": ("provider", "purpose", "id?"),             # purpose "switch" (with id) or "add" (id: Sign In Again)
    "switch": ("provider", "id", "plan_id", "consent"),        # consent "idle": wait until no agy runs
    "add": ("provider", "plan_id", "consent", "id?"),          # Add another account (id: Sign In Again for it)
    "cancel-login": ("provider",),                             # end an Add; the earlier sign-in goes back
    "recover": ("provider", "keep"),                           # keep: one of AGY_RECOVERY_CHOICES
    "rename": ("id", "label"),
    "reorder": ("provider", "ids"),
    "remove": ("id",),
    "agy-accept-notice": (),                                   # the notice about Google's terms was accepted
}


# ---- the platform block (docs/mac/PLAN.md D9) ----
# The words the dashboard uses for this Mac's places. Kept as a block of the state so the page's copy
# comes from one place; the feature flags say what the Mac offers.
PLATFORM_FEATURES = ("tray", "menu_bar", "taskbar_widget", "aumid", "start_on_login")
PLATFORM_LABELS = ("system", "section", "status_icon", "status_icon_hint", "start_on_login", "quit_hint",
                   "open_claude", "reopen_hint", "open_hint", "closed_hint", "system_error", "cwd_example")
# macOS notification permission as the menu bar item sees it ("rejected": macOS refused this copy's code
# identity, a reinstall repairs it; "unavailable": no menu bar item is running).
NOTIFICATION_STATES = ("authorized", "provisional", "pending", "not_determined", "denied", "rejected", "unavailable")
NOTIFICATION_SETTINGS_URL = "x-apple.systempreferences:com.apple.Notifications-Settings.extension"
_PLATFORM = {
    "os": "mac",
    "features": {"tray": False, "menu_bar": True, "taskbar_widget": False, "aumid": False, "start_on_login": True},
    "labels": {
        "system": "macOS", "section": "Menu bar & startup", "status_icon": "Show AccountDeck 2 in the menu bar",
        "status_icon_hint": "The menu bar shows the terminal room left. Click it for every account.",
        "start_on_login": "Start at login", "quit_hint": "Quit Claude from the menu bar and try again.",
        "open_claude": "Open it from Applications or Spotlight.",
        "reopen_hint": "Reopen AccountDeck 2 from Applications.",
        "open_hint": "Open AccountDeck 2 from Applications.",
        "closed_hint": "Open it again from Applications to see your accounts.",
        "system_error": "macOS did not allow that. Try again.", "cwd_example": "/Users/me",
    },
    "notifications": {"state": "unavailable", "settings_url": NOTIFICATION_SETTINGS_URL},
}


def platform_block(notifications="unavailable"):
    """The `platform` block of /api/state, with the notification permission `notifications` (one of
    NOTIFICATION_STATES); a fresh copy, callers may edit it."""
    block = copy.deepcopy(_PLATFORM)
    block["notifications"]["state"] = notifications
    return block


# ---- validation ----

def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


class _Report:
    def __init__(self):
        self.items = []

    def add(self, path, message):
        self.items.append(f"{path}: {message}")

    def obj(self, value, required, path):
        if not isinstance(value, dict):
            self.add(path, "must be an object")
            return False
        missing = [key for key in required if key not in value]
        for key in missing:
            self.add(path, f"missing {key!r}")
        return not missing

    def enum(self, value, allowed, path, nullable=False):
        if (value is None and nullable) or (isinstance(value, str) and value in allowed):
            return True
        self.add(path, f"{value!r} is not one of {', '.join(allowed)}")
        return False

    def num(self, value, path, nullable=True, low=None, high=None):
        if value is None and nullable:
            return True
        if not _is_num(value) or (low is not None and value < low) or (high is not None and value > high):
            bounds = "" if low is None and high is None else f" in [{low}, {high}]"
            self.add(path, f"must be a number{bounds}, got {value!r}")
            return False
        return True

    def int(self, value, path, nullable=False, low=None):
        if (value is None and nullable) or (_is_int(value) and (low is None or value >= low)):
            return True
        self.add(path, f"must be an integer{'' if low is None else f' ≥ {low}'}, got {value!r}")
        return False

    def text(self, value, path, nullable=False, empty=False):
        if (value is None and nullable) or (isinstance(value, str) and (empty or value)):
            return True
        self.add(path, f"must be a{'' if empty else ' non-empty'} string, got {value!r}")
        return False

    def flag(self, value, path):
        if isinstance(value, bool):
            return True
        self.add(path, f"must be true or false, got {value!r}")
        return False

    def seq(self, value, path):
        if isinstance(value, list):
            return True
        self.add(path, "must be a list")
        return False


_STATE_KEYS = ("app", "version", "rev", "now", "mode", "safe_mode", "ready", "read_only", "settings", "providers",
               "accounts", "now_band", "resetting_soon", "pending", "report", "login", "recovery", "notices", "alerts",
               "banners", "setup", "sessions", "cwd", "events", "last_manifest", "platform")
_ACCOUNT_KEYS = ("id", "provider", "label", "email", "plan", "active", "switchable", "saved", "imported_from", "owner",
                 "state", "headline", "credential", "checked", "last_good_at", "stale", "age_tier", "refreshing", "error",
                 "next_check", "paused_until", "score", "recommended", "rank", "sort_order", "windows", "other_windows",
                 "raw_windows", "credits", "tokens", "trend", "temporary_limit")
_WINDOW_KEYS = ("id", "kind", "label", "short", "model", "minutes", "used", "left", "known", "started", "reset",
                "reset_state", "state", "severity", "binding", "pace", "blocked", "primary", "refilled")
_CREDENTIAL_KEYS = ("state", "expires_in", "last_outcome", "dormant_days", "session_expires_in_days", "revoked_hint")
_BEST_KEYS = ("account", "label", "headroom", "policy", "reason", "confidence", "suggest_switch", "current_is_best",
              "alternatives", "state")
_HERO_KEYS = ("kind", "account", "label", "provider", "window", "window_label", "reset", "left", "severity", "message",
              "more_within_24h")
_STRIP_KEYS = ("account", "label", "provider", "window", "window_label", "reset", "left", "severity", "chip", "bucket")
_PENDING_KEYS = ("id", "kind", "provider", "account", "label", "phase", "message", "cancellable", "started",
                 "eta_seconds", "consent", "waiting_on", "steps")
_REPORT_KEYS = ("op", "kind", "provider", "account", "label", "finished", "outcome", "verified", "verified_at",
                "verify_reason", "stop_tier", "stop_ms", "helpers_self_exited", "stragglers", "summary", "carried",
                "account_bound", "manifest", "error")
_RUNWAY_KEYS = ("pace_per_hour", "room", "accounts", "runs_out_at", "next_refill_at", "refill_after_out_at", "refill_label",
                "gap_s", "confidence", "basis_s")
RUNWAY_CONFIDENCES = ("low", "ok")
_PLAN_KEYS = ("plan_id", "expires", "provider", "purpose", "target", "current", "headroom", "running", "process_set",
              "signature", "blockers", "warnings", "tip", "will", "preserved", "account_bound", "invisible", "stop_tier",
              "confirmation_required", "idle", "remember_available", "default_button", "saves_outgoing")


def validate_state(payload):
    """Every contract violation in an /api/state payload as "path: problem" strings (empty = valid)."""
    report = _Report()
    try:
        _check_state(payload, report)
    except Exception as exc:     # a malformed payload must produce a violation, never crash the engine
        report.add("$", f"could not be validated ({type(exc).__name__}: {exc})")
    return report.items


def validate_reset_plan(plan):
    """Every contract violation in the plan `reset-plan` answers with, as "path: problem" strings."""
    report = _Report()
    try:
        _check_reset_plan(plan, "plan", report)
    except Exception as exc:
        report.add("plan", f"could not be validated ({type(exc).__name__}: {exc})")
    return report.items


def validate_plan(plan):
    """Every contract violation in a SwitchPlan (PLAN §3.4) as "path: problem" strings."""
    report = _Report()
    try:
        _check_plan(plan, "plan", report)
    except Exception as exc:
        report.add("plan", f"could not be validated ({type(exc).__name__}: {exc})")
    return report.items


def _check_state(state, r):
    if not r.obj(state, _STATE_KEYS, "$"):
        return
    if state["app"] != "accountdeck2":
        r.add("app", "must be 'accountdeck2'")
    r.text(state["version"], "version")
    r.int(state["rev"], "rev", low=0)
    if not r.num(state["now"], "now", nullable=False):
        return
    now = state["now"]
    r.enum(state["mode"], MODES, "mode")
    for key in ("safe_mode", "ready", "read_only"):
        r.flag(state[key], key)
    if state["mode"] == "demo" and state["safe_mode"] is not True:
        r.add("safe_mode", "demo mode always runs in safe mode")
    pinned = _check_settings(state["settings"], r)
    accounts = state["accounts"] if r.seq(state["accounts"], "accounts") else []
    good = [a for i, a in enumerate(accounts) if _check_account(a, f"accounts[{i}]", now, pinned, r)]
    ids = [a["id"] for a in good]
    for aid in {x for x in ids if ids.count(x) > 1}:
        r.add("accounts", f"duplicate id {aid!r}")
    complete = len(good) == len(accounts)
    _check_providers(state["providers"], good, r)
    _check_now_band(state["now_band"], state, good, complete, now, r)
    _check_strip(state["resetting_soon"], good, complete and state["ready"] is True, now, r)
    _check_pending(state["pending"], r)
    _check_report(state["report"], r)
    if state["login"] is not None and r.obj(state["login"], ("provider",), "login"):
        r.enum(state["login"]["provider"], ACCOUNT_PROVIDERS, "login.provider")     # Antigravity's Add: its own block
    if r.obj(state["recovery"], ("pending", "count"), "recovery"):
        r.flag(state["recovery"]["pending"], "recovery.pending")
        r.int(state["recovery"]["count"], "recovery.count", low=0)
    _check_messages(state, r)
    if not isinstance(state["setup"], dict):
        r.add("setup", "must be an object")
    r.seq(state["sessions"], "sessions")
    r.text(state["cwd"], "cwd", empty=True)
    r.text(state["last_manifest"], "last_manifest", nullable=True)
    _check_platform(state["platform"], r)
    if "move_import" in state:      # optional: the Mac side of the move (docs/mac/PLAN.md §2.3)
        _check_move_import(state["move_import"], r)
    if "service" in state:          # optional: the service looking after itself (engine.service_state)
        _check_service(state["service"], r)
    if state.get("health") is not None:      # null until the doctor's first slow cycle
        _check_health(state["health"], "health", r)
    if state.get("build") is not None:       # null in a checkout: install.sh stamps an installed copy
        _check_build(state["build"], r)
    if "update" in state:           # the update check (accountdeck/updates.py); every state this build makes has it
        _check_update(state["update"], state["version"], r)


_UPDATE_KEYS = ("enabled", "current", "latest", "available", "url", "checked_at", "skipped", "status", "checking",
                "failed_at", "reason", "blocked")


def _check_update(block, version, r):
    """`update`: the latest release seen and whether it is offered (updates.view). Versions are dotted numbers, the
    link is that version's release page and only ours, `available` agrees with `status`, and a copy that does not
    check says why and shows nothing else."""
    if not r.obj(block, _UPDATE_KEYS, "update"):
        return
    for key in ("enabled", "available", "checking"):
        r.flag(block[key], f"update.{key}")
    r.enum(block["status"], UPDATE_STATUSES, "update.status")
    if block["current"] != version:
        r.add("update.current", f"must be the state's version {version!r}, got {block['current']!r}")
    for key in ("latest", "skipped"):
        if block[key] is not None and not updates.valid_version(block[key]):
            r.add(f"update.{key}", f"must be null or a version like 2.3.0, got {block[key]!r}")
    if block["url"] != (updates.release_page(block["latest"]) if block["latest"] is not None else None):
        r.add("update.url", "must be the release page of update.latest (null without one)")
    for key in ("checked_at", "failed_at"):
        r.num(block[key], f"update.{key}")
    for key in ("reason", "blocked"):
        r.text(block[key], f"update.{key}", nullable=True)
    if block["available"] is not (block["status"] == "available"):
        r.add("update.available", "must be true exactly when status is available")
    if block["available"] and not updates.is_newer(block["latest"], block["current"]):
        r.add("update.latest", "an available update must be newer than update.current")
    if (block["status"] == "unavailable") is not (block["reason"] is not None):
        r.add("update.reason", "must say why exactly when status is unavailable")
    if block["status"] == "unavailable" and any(block[key] is not None for key in ("latest", "checked_at", "skipped")):
        r.add("update", "a copy that does not check shows no release")
    if block["blocked"] is not None and not block["available"]:
        r.add("update.blocked", "only an available update can be blocked")


def validate_health(block):
    """Every contract violation in a doctor report (`state.health`, `run.py --doctor --json`) as "path: problem"."""
    report = _Report()
    try:
        _check_health(block, "health", report)
    except Exception as exc:
        report.add("health", f"could not be validated ({type(exc).__name__}: {exc})")
    return report.items


def _check_health(block, path, r):
    """`health`: {"ok": no check failed, "checks": [{"id", "level", "text", "action"}], "at"} (accountdeck/doctor.py)."""
    if not r.obj(block, ("ok", "checks", "at"), path):
        return
    r.flag(block["ok"], f"{path}.ok")
    r.num(block["at"], f"{path}.at", nullable=False)
    if not r.seq(block["checks"], f"{path}.checks"):
        return
    failed = False
    for index, check in enumerate(block["checks"]):
        where = f"{path}.checks[{index}]"
        if r.obj(check, ("id", "level", "text", "action"), where):
            r.text(check["id"], f"{where}.id")
            r.enum(check["level"], HEALTH_LEVELS, f"{where}.level")
            r.text(check["text"], f"{where}.text")
            r.text(check["action"], f"{where}.action", nullable=True)
            failed |= check["level"] == "fail"
    if block["ok"] is failed:
        r.add(f"{path}.ok", "must be true exactly when no check failed")


def _check_build(block, r):
    """`build`: install.sh's stamp (`accountdeck.BUILD`); a package install knows no commit."""
    if r.obj(block, ("commit", "dirty", "installed_at"), "build"):
        r.text(block["commit"], "build.commit", nullable=True)
        if block["dirty"] is not None:
            r.flag(block["dirty"], "build.dirty")
        r.num(block["installed_at"], "build.installed_at")


def _check_platform(block, r):
    """The platform block: this Mac, every feature flag, every label as text and the notification state."""
    if not r.obj(block, ("os", "features", "labels", "notifications"), "platform"):
        return
    if r.obj(block["notifications"], ("state", "settings_url"), "platform.notifications"):
        r.enum(block["notifications"]["state"], NOTIFICATION_STATES, "platform.notifications.state")
        r.text(block["notifications"]["settings_url"], "platform.notifications.settings_url")
    r.enum(block["os"], ("mac",), "platform.os")
    if r.obj(block["features"], PLATFORM_FEATURES, "platform.features"):
        for key in PLATFORM_FEATURES:
            r.flag(block["features"][key], f"platform.features.{key}")
    if r.obj(block["labels"], PLATFORM_LABELS, "platform.labels"):
        for key in PLATFORM_LABELS:
            r.text(block["labels"][key], f"platform.labels.{key}")


def _is_move_code(value):
    """XXXX-XXXX in base32 letters and digits 2-7 (move_bundle.confirm_code)."""
    alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    return (isinstance(value, str) and len(value) == 9 and value[4] == "-"
            and set(value[:4] + value[5:]) <= alphabet)


def _check_service(block, r):
    """The `service` block, also what /_health says: the watchdog (`last`: its last restart or brake) and the
    database's care (`restored_from`: the backup this run went back to, when it did)."""
    if not r.obj(block, ("watchdog", "db"), "service"):
        return
    watchdog, db = block["watchdog"], block["db"]
    path = "service.watchdog"
    if r.obj(watchdog, ("state", "tick_age_s", "restarts_last_hour", "reads_stuck", "last"), path):
        r.enum(watchdog["state"], WATCHDOG_STATES, f"{path}.state")
        r.num(watchdog["tick_age_s"], f"{path}.tick_age_s", low=0)
        r.int(watchdog["restarts_last_hour"], f"{path}.restarts_last_hour", low=0)
        r.int(watchdog["reads_stuck"], f"{path}.reads_stuck", low=0)
        if watchdog["last"] is not None and r.obj(watchdog["last"], ("at", "reason"), f"{path}.last"):
            r.num(watchdog["last"]["at"], f"{path}.last.at", nullable=False)
            r.text(watchdog["last"]["reason"], f"{path}.last.reason")
    path = "service.db"
    if r.obj(db, ("quick_check", "last_backup", "backups", "restored_from"), path):
        checked, backup = db["quick_check"], db["last_backup"]
        if checked is not None and r.obj(checked, ("result", "at", "ms"), f"{path}.quick_check"):
            r.enum(checked["result"], DB_CHECKS, f"{path}.quick_check.result")
            r.num(checked["at"], f"{path}.quick_check.at", nullable=False)
            r.num(checked["ms"], f"{path}.quick_check.ms", nullable=False, low=0)
        if backup is not None and r.obj(backup, ("at", "bytes"), f"{path}.last_backup"):
            r.num(backup["at"], f"{path}.last_backup.at", nullable=False)
            r.int(backup["bytes"], f"{path}.last_backup.bytes", low=0)
        r.int(db["backups"], f"{path}.backups", low=0)
        r.num(db["restored_from"], f"{path}.restored_from")


def _check_move_import(block, r):
    """The `move_import` block (accountdeck/move_import.py state()): undismissed imports, each with a code
    exactly when it is complete; the Claude Desktop accounts waiting for conversion; the "on_pc" count."""
    if not r.obj(block, ("imports", "deferred", "on_pc"), "move_import"):
        return
    r.int(block["on_pc"], "move_import.on_pc", low=0)
    deferred = block["deferred"]
    if r.obj(deferred, ("count", "message", "reason", "problem"), "move_import.deferred"):
        if r.int(deferred["count"], "move_import.deferred.count", low=0):
            r.text(deferred["message"], "move_import.deferred.message", nullable=deferred["count"] == 0)
        for key in ("reason", "problem"):
            r.text(deferred[key], f"move_import.deferred.{key}", nullable=True)
    if not r.seq(block["imports"], "move_import.imports"):
        return
    keys = ("bundle_id", "kind", "at", "complete", "code", "counts", "failures", "message")
    for index, item in enumerate(block["imports"]):
        path = f"move_import.imports[{index}]"
        if not r.obj(item, keys, path):
            continue
        r.text(item["bundle_id"], f"{path}.bundle_id")
        r.enum(item["kind"], ("main", "handover"), f"{path}.kind")
        r.num(item["at"], f"{path}.at", nullable=False)
        r.text(item["message"], f"{path}.message")
        if r.flag(item["complete"], f"{path}.complete"):
            if item["complete"] != _is_move_code(item["code"]):
                r.add(f"{path}.code", "a complete import has a XXXX-XXXX code; any other has none")
            if item["complete"] and item["failures"]:
                r.add(f"{path}.failures", "a complete import has no failures")
        if r.obj(item["counts"], MOVE_IMPORT_OUTCOMES, f"{path}.counts"):
            for key in MOVE_IMPORT_OUTCOMES:
                r.int(item["counts"][key], f"{path}.counts.{key}", low=0)
        if r.seq(item["failures"], f"{path}.failures"):
            for number, failure in enumerate(item["failures"]):
                where = f"{path}.failures[{number}]"
                if r.obj(failure, ("label", "provider", "reason"), where):
                    r.text(failure["label"], f"{where}.label")
                    r.enum(failure["provider"], ACCOUNT_PROVIDERS, f"{where}.provider")     # the PC had no Antigravity
                    r.text(failure["reason"], f"{where}.reason")


def _check_settings(settings, r):
    if not isinstance(settings, dict):
        r.add("settings", "must be an object")
        return ""
    for key, value in settings.items():
        path = f"settings.{key}"
        if key in INTERNAL_SETTINGS:
            r.add(path, "internal setting must not be sent to the browser")
            continue
        rule = _SETTING_RULES.get(key)
        if rule is None:
            continue
        kind = rule[0]
        if kind == "choice":
            r.enum(value, rule[1], path)
        elif kind == "bool":
            r.flag(value, path)
        elif kind == "int":
            if r.int(value, path) and not rule[1] <= value <= rule[2]:
                r.add(path, f"must be between {rule[1]} and {rule[2]}")
        elif kind == "text":
            r.text(value, path, empty=True)
        elif kind == "ids":
            if r.seq(value, path):
                for item in value:
                    r.text(item, path)
        elif kind == "time":
            r.num(value, path)
        elif kind == "hours" and value is not None and not (
                isinstance(value, list) and len(value) == 2 and all(_is_int(h) and 0 <= h <= 23 for h in value)):
            r.add(path, "must be null or [start_hour, end_hour]")
    pinned = settings.get("pinned_model", "")
    return pinned if isinstance(pinned, str) else ""


def _forbidden(value, path, r):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                r.add(f"{path}.{key}", "identities, org ids and secrets never leave the server")
            _forbidden(item, f"{path}.{key}", r)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _forbidden(item, f"{path}[{index}]", r)


def _check_account(a, path, now, pinned, r):
    """Checks one account; returns True when it is well-formed enough for cross-checks."""
    if not r.obj(a, _ACCOUNT_KEYS, path):
        return False
    before = len(r.items)
    _forbidden(a, path, r)
    r.text(a["id"], f"{path}.id")
    r.enum(a["provider"], ACCOUNT_PROVIDERS, f"{path}.provider")      # Antigravity's are in providers.antigravity
    if a["provider"] == "claude_cli" and r.obj(a, ("health", "excluded", "live_expired", "login_ends_at"), path):
        r.enum(a["health"], ("ok", "needs_signin", "on_hold"), path + ".health")
        r.flag(a["excluded"], path + ".excluded")
        r.flag(a["live_expired"], path + ".live_expired")
        r.num(a["login_ends_at"], path + ".login_ends_at")
    r.text(a["label"], f"{path}.label")
    r.text(a["email"], f"{path}.email", nullable=True)
    r.text(a["plan"], f"{path}.plan", nullable=True)
    for key in ("active", "switchable", "saved", "stale", "refreshing", "recommended"):
        r.flag(a[key], f"{path}.{key}")
    if a["imported_from"] not in (None, "v1", "pc"):
        r.add(f"{path}.imported_from", "must be null, 'v1' or 'pc'")
    r.enum(a["owner"], OWNERS, f"{path}.owner")
    if a["owner"] in ("moved", "on_pc") and a["switchable"] is not False:
        r.add(f"{path}.switchable", "must be false for an account another computer owns")
    r.enum(a["state"], ACCOUNT_STATES, f"{path}.state")
    for key in ("checked", "last_good_at", "next_check", "paused_until"):
        r.num(a[key], f"{path}.{key}")
    r.enum(a["age_tier"], AGE_TIERS, f"{path}.age_tier", nullable=True)
    r.num(a["score"], f"{path}.score", low=0, high=1)
    r.int(a["rank"], f"{path}.rank", nullable=True, low=1)
    r.int(a["sort_order"], f"{path}.sort_order")
    if a["tokens"] is not None and not isinstance(a["tokens"], dict):
        r.add(f"{path}.tokens", "must be null or an object")
    if r.seq(a["credits"], f"{path}.credits"):
        for index, credit in enumerate(a["credits"]):
            if r.obj(credit, ("label",), f"{path}.credits[{index}]"):
                r.text(credit["label"], f"{path}.credits[{index}].label")
    if ("reset_credits" in a or "resets" in a) and a["provider"] not in RESET_PROVIDERS:
        r.add(path, "only Claude and Codex accounts hold limit resets")
    if "reset_credits" in a and r.seq(a["reset_credits"], f"{path}.reset_credits"):
        if not a["reset_credits"]:
            r.add(f"{path}.reset_credits", "is present only when it has rows")
        _check_reset_rows(a["reset_credits"], f"{path}.reset_credits", r)
    if "resets" in a:
        _check_resets(a["resets"], a.get("reset_credits") or [], f"{path}.resets", r)
    _check_trend(a["trend"], f"{path}.trend", now, r)
    if r.seq(a["raw_windows"], f"{path}.raw_windows"):
        for index, raw in enumerate(a["raw_windows"]):
            if r.obj(raw, ("key", "used", "reset"), f"{path}.raw_windows[{index}]"):
                r.text(raw["key"], f"{path}.raw_windows[{index}].key")
                r.num(raw["used"], f"{path}.raw_windows[{index}].used")
                r.num(raw["reset"], f"{path}.raw_windows[{index}].reset")
    if r.obj(a["credential"], _CREDENTIAL_KEYS, f"{path}.credential"):
        c = a["credential"]
        r.enum(c["state"], CREDENTIAL_STATES, f"{path}.credential.state")
        r.num(c["expires_in"], f"{path}.credential.expires_in")
        r.text(c["last_outcome"], f"{path}.credential.last_outcome", nullable=True)
        r.int(c["dormant_days"], f"{path}.credential.dormant_days", nullable=True, low=0)
        r.num(c["session_expires_in_days"], f"{path}.credential.session_expires_in_days")
        r.flag(c["revoked_hint"], f"{path}.credential.revoked_hint")
    if a["error"] is not None and r.obj(a["error"], ("code", "message", "since", "outage"), f"{path}.error"):
        r.enum(a["error"]["code"], ERROR_CODES + PROVIDER_FAILURES, f"{path}.error.code")
        r.text(a["error"]["message"], f"{path}.error.message")
        r.num(a["error"]["since"], f"{path}.error.since")
        r.flag(a["error"]["outage"], f"{path}.error.outage")
    if not (r.seq(a["windows"], f"{path}.windows") and r.seq(a["other_windows"], f"{path}.other_windows")):
        return False
    if len(r.items) > before:
        return False
    if a["saved"] is False or a["id"] in UNSAVED_IDS.values():
        if a["id"] != UNSAVED_IDS.get(a["provider"]) or a["saved"] or not a["active"] or a["switchable"]:
            r.add(path, "only 'unsaved-claude' / 'unsaved-codex' may be unsaved, and they are active and not switchable")
    at = a["last_good_at"]
    tier = None if at is None else rules.age_tier(now - at, a["active"])
    if a["age_tier"] != tier:
        r.add(f"{path}.age_tier", f"must be {tier!r} for a reading {'never taken' if at is None else f'{now - at:.0f}s old'}")
    if a["stale"] != (tier == "stale"):
        r.add(f"{path}.stale", "must be true exactly when age_tier is 'stale'")
    if at is None and (a["windows"] or a["other_windows"]):
        r.add(f"{path}.windows", "an account without a reading has no windows")
    stale = tier == "stale"
    for group, primary in (("windows", True), ("other_windows", False)):
        for index, w in enumerate(a[group]):
            _check_window(w, f"{path}.{group}[{index}]", now, at, stale, primary, r)
    if len(r.items) > before:
        return False
    windows, others = a["windows"], a["other_windows"]
    _check_order(windows, f"{path}.windows", r)
    _check_order(others, f"{path}.other_windows", r)
    wids = [w["id"] for w in windows + others]
    for wid in {x for x in wids if wids.count(x) > 1}:
        r.add(path, f"duplicate window id {wid!r}")
    for index, w in enumerate(others):
        if not ((w["used"] or 0) > 0 or w["reset"] is not None or w["blocked"]):
            r.add(f"{path}.other_windows[{index}]", "other windows appear only when used > 0, a reset exists or locked")
    marked = [w for w in windows + others if w["binding"]]
    expected = rules.pick_binding(windows, pinned)
    if expected is None:
        if marked:
            r.add(path, "no gating window, so no window may be binding")
    elif len(marked) != 1 or not rules.is_gating(marked[0], pinned):
        r.add(path, "exactly one gating window must be binding")
    elif marked[0]["severity"] != "out" and (expected["severity"] == "out" or marked[0]["left"] > expected["left"]):
        r.add(path, f"binding must be {expected['id']!r} (out beats everything, then the lowest left)")
    binding = marked[0] if len(marked) == 1 else None
    if a["temporary_limit"] != rules.temporary_limit(a):
        r.add(f"{path}.temporary_limit", "must be true exactly for an Ultra account with a 5-hour window")
    state = rules.account_state(a)
    if a["state"] != state:
        r.add(f"{path}.state", f"must be {state!r}")
    if r.obj(a["headline"], ("kind", "text", "window"), f"{path}.headline"):
        h = a["headline"]
        if r.enum(h["kind"], HEADLINE_KINDS, f"{path}.headline.kind"):
            kind = rules.headline_kind({**a, "state": state}, binding)
            if h["kind"] != kind:
                r.add(f"{path}.headline.kind", f"must be {kind!r}")
            wanted = binding["id"] if kind in ("out", "critical", "low", "watch") and binding else None
            if h["window"] != wanted:
                r.add(f"{path}.headline.window", f"must be {wanted!r}")
        r.text(h["text"], f"{path}.headline.text")
    if a["recommended"] and a["rank"] != 1:
        r.add(f"{path}.rank", "the recommended account has rank 1")
    return len(r.items) == before


def _check_trend(trend, path, now, r):
    if not r.seq(trend, path):
        return
    if len(trend) > 32:
        r.add(path, "at most 32 points")
    stamps = []
    for index, point in enumerate(trend):
        if not (isinstance(point, list) and len(point) == 2 and all(_is_num(v) for v in point)):
            r.add(f"{path}[{index}]", "must be [timestamp, used]")
            return
        stamps.append(point[0])
    if stamps != sorted(stamps) or (stamps and stamps[-1] > now):
        r.add(path, "points must be in time order and not in the future")


def _check_window(w, path, now, at, stale, primary, r):
    if not r.obj(w, _WINDOW_KEYS, path):
        return
    before = len(r.items)
    r.text(w["id"], f"{path}.id")
    r.enum(w["kind"], WINDOW_KINDS, f"{path}.kind")
    r.text(w["label"], f"{path}.label")
    r.text(w["short"], f"{path}.short")
    r.text(w["model"], f"{path}.model", nullable=w["kind"] != "model")
    r.num(w["minutes"], f"{path}.minutes", low=1)
    r.num(w["used"], f"{path}.used", low=0)
    r.num(w["left"], f"{path}.left", low=0, high=100)
    r.num(w["reset"], f"{path}.reset")
    for key in ("known", "started", "binding", "primary"):
        r.flag(w[key], f"{path}.{key}")
    r.enum(w["reset_state"], RESET_STATES, f"{path}.reset_state")
    r.enum(w["state"], WINDOW_STATES, f"{path}.state")
    r.enum(w["severity"], SEVERITIES, f"{path}.severity")
    r.text(w["blocked"], f"{path}.blocked", nullable=True)
    if "gates" in w and w["gates"] is not True:      # only on a meter of kind "other" that holds terminals
        r.add(f"{path}.gates", "is true where present")
    if w["refilled"] is not None and r.obj(w["refilled"], ("at", "confirmed", "from"), f"{path}.refilled"):
        for key in ("at", "confirmed", "from"):
            r.num(w["refilled"][key], f"{path}.refilled.{key}", nullable=key == "from")
    if w["pace"] is not None and r.obj(w["pace"], ("expected", "delta", "stage", "ready", "eta_full", "samples"),
                                       f"{path}.pace"):
        p = w["pace"]
        r.num(p["expected"], f"{path}.pace.expected", nullable=False, low=0, high=100)
        r.num(p["delta"], f"{path}.pace.delta", nullable=False)
        r.enum(p["stage"], PACE_STAGES, f"{path}.pace.stage")
        r.flag(p["ready"], f"{path}.pace.ready")
        r.num(p["eta_full"], f"{path}.pace.eta_full")
        r.int(p["samples"], f"{path}.pace.samples", low=0)
    if len(r.items) > before:
        return
    kind_is_primary = w["kind"] in PRIMARY_KINDS
    if w["primary"] != kind_is_primary:
        r.add(f"{path}.primary", "must be true exactly for session, weekly and model windows")
    if kind_is_primary != primary:
        r.add(f"{path}.kind", "windows holds session/weekly/model windows; other_windows holds kind 'other'")
    used, left = w["used"], w["left"]
    if w["known"] != (used is not None):
        r.add(f"{path}.known", "known must be true exactly when used is a number (unknown is never 0)")
    if (used is None) != (left is None):
        r.add(f"{path}.left", "left must be null exactly when used is null")
    elif used is not None and abs(left - max(0.0, 100.0 - used)) > 1e-6:
        r.add(f"{path}.left", "left must equal 100 − used, clamped at 0")
    if not w["started"] and w["reset"] is not None:
        r.add(f"{path}.started", "a window with a reset time has started")
    reset_state = rules.reset_state(w["reset"], now, at)
    if w["reset_state"] != reset_state:
        r.add(f"{path}.reset_state", f"must be {reset_state!r}")
    if w["state"] in HIDDEN_STATES and used is not None:
        r.add(f"{path}.used", f"the percentage of a {w['state']} window is hidden (null)")
    if used is None and w["severity"] != "unknown":
        r.add(f"{path}.severity", "must be 'unknown' when used is null")
    pace = w["pace"]
    if pace is not None:
        if used is None or w["severity"] == "out":
            r.add(f"{path}.pace", "no pace for hidden, unknown or out windows")
        else:
            if pace["stage"] != rules.pace_stage(pace["delta"], pace["ready"]):
                r.add(f"{path}.pace.stage", f"must be {rules.pace_stage(pace['delta'], pace['ready'])!r}")
            if abs(pace["delta"] - (used - pace["expected"])) > 0.11:
                r.add(f"{path}.pace.delta", "must equal used − expected")
    if used is not None:
        severity = rules.severity(used, w["blocked"], pace)
        if w["severity"] != severity:
            r.add(f"{path}.severity", f"must be {severity!r} for {max(0.0, 100.0 - used):g}% left")
    refilled = w["refilled"]
    if refilled is not None and not (refilled["at"] <= refilled["confirmed"] <= now + 1
                                     and now - refilled["confirmed"] <= REFILL_KEEP):
        r.add(f"{path}.refilled", "is kept only for 60 min after a confirmed refill")
    state = rules.window_state(w["started"], reset_state, w["severity"], refilled, stale, now)
    if w["state"] != state:
        r.add(f"{path}.state", f"must be {state!r}")
    if w["binding"] and not w["primary"]:
        r.add(f"{path}.binding", "only primary windows can be binding")


def _check_order(windows, path, r):
    if [w["id"] for w in windows] != [w["id"] for w in sorted(windows, key=rules.window_order)]:
        r.add(path, "must be sorted session, weekly, then models by label")


def _check_providers(providers, accounts, r):
    if not r.obj(providers, PROVIDERS, "providers"):
        return
    for p in PROVIDERS:
        path = f"providers.{p}"
        keys = ("status", "active", "running", "switching", "paused") + (("data_folder", "folder_warning") if p == "claude" else ())
        keys += _AGY_KEYS if p == "antigravity" else ()
        if not r.obj(providers[p], keys, path):
            continue
        entry = providers[p]
        status = entry["status"]
        if r.obj(status, ("level", "summary", "source", "checked", "since", "url", "incident"), f"{path}.status"):
            r.enum(status["level"], STATUS_LEVELS, f"{path}.status.level")
            r.enum(status["source"], STATUS_SOURCES, f"{path}.status.source")
            r.text(status["summary"], f"{path}.status.summary")
            r.num(status["checked"], f"{path}.status.checked")
            r.num(status["since"], f"{path}.status.since")
            r.text(status["url"], f"{path}.status.url", nullable=True)
            if status["incident"] is not None and r.obj(status["incident"], ("title", "impact", "status", "started"),
                                                        f"{path}.status.incident"):
                r.text(status["incident"]["title"], f"{path}.status.incident.title")
                r.num(status["incident"]["started"], f"{path}.status.incident.started")
        if p == "antigravity":          # its accounts are the block's own (`_check_agy`)
            mine = entry["accounts"] if isinstance(entry["accounts"], list) else []
            actives = [a.get("id") for a in mine if isinstance(a, dict) and a.get("active") is True]
        else:
            actives = [a["id"] for a in accounts if a["provider"] == p and a["active"]]
        if len(actives) > 1:
            r.add(path, f"more than one active account: {actives}")
        if entry["active"] != (actives[0] if actives else None):
            r.add(f"{path}.active", f"must name the active {p} account ({actives[0] if actives else None!r})")
        r.flag(entry["switching"], f"{path}.switching")
        r.flag(entry["paused"], f"{path}.paused")
        if p == "claude":
            r.text(entry["data_folder"], f"{path}.data_folder", nullable=True)
            r.text(entry["folder_warning"], f"{path}.folder_warning", nullable=True)
            _check_claude_running(entry["running"], f"{path}.running", r)
        elif p == "codex":
            _check_codex_running(entry["running"], f"{path}.running", r)
        elif p == "antigravity":
            _check_agy(entry, path, r)
        else:
            _check_terminal(entry, path, accounts, r)


def _check_runway(block, path, r):
    """The terminal pool's forecast (accountdeck/runway.py): null until the first one is worked out."""
    if block is None or not r.obj(block, _RUNWAY_KEYS, path):
        return
    r.num(block["pace_per_hour"], f"{path}.pace_per_hour", nullable=False, low=0)
    r.num(block["room"], f"{path}.room", nullable=False, low=0)
    r.int(block["accounts"], f"{path}.accounts", low=0)
    for key in ("runs_out_at", "next_refill_at", "refill_after_out_at"):
        r.num(block[key], f"{path}.{key}")
    r.text(block["refill_label"], f"{path}.refill_label", nullable=True)
    r.int(block["gap_s"], f"{path}.gap_s", nullable=True, low=0)
    r.enum(block["confidence"], RUNWAY_CONFIDENCES, f"{path}.confidence")
    r.int(block["basis_s"], f"{path}.basis_s", low=0)
    if (block["gap_s"] is None) != (block["refill_after_out_at"] is None) or (block["refill_label"] is None) != (block["gap_s"] is None):
        r.add(path, "gap_s, refill_after_out_at and refill_label come together")
    if block["runs_out_at"] is None and block["gap_s"] is not None:
        r.add(f"{path}.gap_s", "a refill after the run-out needs a run-out")


def _check_watch(watch, path, r):
    """Limit errors in Claude Code's transcripts (accountdeck/cli_watch.py). `last` is the latest signal, counted or
    not; `reaction_s` the seconds from its error line to the switch it drove; `stopped` the sessions that need a
    "continue" from the owner."""
    if not r.obj(watch, ("running", "last", "stopped"), path):
        return
    r.flag(watch["running"], f"{path}.running")
    last = watch["last"]
    if last is not None and r.obj(last, ("at", "kind", "account", "counted", "reaction_s"), f"{path}.last"):
        r.num(last["at"], f"{path}.last.at", nullable=False)
        r.enum(last["kind"], WATCH_KINDS, f"{path}.last.kind")
        r.text(last["account"], f"{path}.last.account", nullable=True)
        r.flag(last["counted"], f"{path}.last.counted")
        r.num(last["reaction_s"], f"{path}.last.reaction_s", low=0)
    if r.seq(watch["stopped"], f"{path}.stopped"):
        for row in watch["stopped"]:
            if r.obj(row, ("session", "name", "project", "kind", "at"), f"{path}.stopped[]"):
                r.text(row["session"], f"{path}.stopped[].session")
                r.text(row["name"], f"{path}.stopped[].name", nullable=True)
                r.text(row["project"], f"{path}.stopped[].project", nullable=True)
                r.enum(row["kind"], WATCH_KINDS[:-1], f"{path}.stopped[].kind")
                r.num(row["at"], f"{path}.stopped[].at", nullable=False)


def _check_usage_shape(block, path, r):
    """The last change in the shape of Claude's usage answers, names only (usage.watch_shape); null when none lately."""
    if block is None or not r.obj(block, ("changed_at", "added", "removed"), path):
        return
    r.num(block["changed_at"], f"{path}.changed_at", nullable=False)
    for key in ("added", "removed"):
        if r.seq(block[key], f"{path}.{key}"):
            for name in block[key]:
                r.text(name, f"{path}.{key}[]")


def _check_terminal(entry, path, accounts, r):
    if not r.obj(entry, ("autoswitch", "enrollment", "readonly_reason", "signin_due"), path):
        return
    r.text(entry["readonly_reason"], path + ".readonly_reason", nullable=True)
    if r.seq(entry["signin_due"], path + ".signin_due"):
        for row in entry["signin_due"]:
            if r.obj(row, ("id", "label", "ends_at", "state"), path + ".signin_due[]"):
                if not any(a["id"] == row["id"] and a["provider"] == "claude_cli" for a in accounts):
                    r.add(path + ".signin_due[].id", "must be a terminal account")
                r.text(row["label"], path + ".signin_due[].label")
                r.num(row["ends_at"], path + ".signin_due[].ends_at")
                r.enum(row["state"], SIGNIN_STATES, path + ".signin_due[].state")
    _check_usage_shape(entry.get("usage_shape"), path + ".usage_shape", r)
    _check_watch(entry.get("watch"), path + ".watch", r)
    _check_runway(entry.get("runway"), path + ".runway", r)
    # Automatic limit resets (reset_auto.state): on or off, why they stopped, and the last one. Absent in older states.
    resetting = entry.get("auto_reset")
    if resetting is not None and r.obj(resetting, ("enabled", "stopped", "last"), path + ".auto_reset"):
        r.flag(resetting["enabled"], path + ".auto_reset.enabled")
        r.text(resetting["stopped"], path + ".auto_reset.stopped", nullable=True)
        used = resetting["last"]
        if used is not None and r.obj(used, ("account", "label", "at"), path + ".auto_reset.last"):
            if not any(a["id"] == used["account"] and a["provider"] == "claude_cli" for a in accounts):
                r.add(path + ".auto_reset.last.account", "must be a terminal account")
            r.text(used["label"], path + ".auto_reset.last.label")
            r.num(used["at"], path + ".auto_reset.last.at", nullable=False)
    auto = entry["autoswitch"]
    if r.obj(auto, ("enabled", "threshold", "order_mode", "early_switch", "next_up", "last", "hold"), path + ".autoswitch"):
        r.flag(auto["enabled"], path + ".autoswitch.enabled")
        r.flag(auto["early_switch"], path + ".autoswitch.early_switch")
        r.enum(auto["order_mode"], ("soonest_reset", "most_room", "manual"), path + ".autoswitch.order_mode")
        r.num(auto["threshold"], path + ".autoswitch.threshold", nullable=False, low=50, high=99)
        r.text(auto["hold"], path + ".autoswitch.hold", nullable=True)
        if r.seq(auto["next_up"], path + ".autoswitch.next_up"):
            expected = {a["id"] for a in accounts if a["provider"] == "claude_cli"}
            ids = []
            for row in auto["next_up"]:
                if r.obj(row, ("id", "label", "skip", "room", "weekly_reset"), path + ".autoswitch.next_up[]"):
                    ids.append(row["id"])
                    r.text(row["skip"], path + ".autoswitch.next_up[].skip", nullable=True)
                    r.num(row["room"], path + ".autoswitch.next_up[].room")
            if set(ids) != expected or len(ids) != len(expected):
                r.add(path + ".autoswitch.next_up", "must list every terminal account once")
        last = auto["last"]
        if last is not None and r.obj(last, ("from", "to", "at", "kind", "reason", "undo_until"), path + ".autoswitch.last"):
            for key in ("from", "to", "reason"):
                r.text(last[key], path + ".autoswitch.last." + key)
            r.enum(last["kind"], ("threshold", "out", "login", "early", "refilled"), path + ".autoswitch.last.kind")
            for key in ("at", "undo_until"):
                r.num(last[key], path + ".autoswitch.last." + key, nullable=False)
    enrollment = entry["enrollment"]
    if r.obj(enrollment, ("phase", "message"), path + ".enrollment"):
        if "email" in enrollment:
            r.text(enrollment["email"], path + ".enrollment.email", empty=True)
        r.enum(enrollment["phase"], ("idle", "signing_in", "waiting_exit", "saved", "cancelled", "failed"), path + ".enrollment.phase")
        r.text(enrollment["message"], path + ".enrollment.message", empty=True)


def _check_sessions(sessions, path, r):
    if not r.seq(sessions, path):
        return None
    counts = {"busy": 0, "unknown": 0}
    for index, s in enumerate(sessions):
        if r.obj(s, ("pid", "name", "status", "since"), f"{path}[{index}]"):
            r.int(s["pid"], f"{path}[{index}].pid", low=1)
            r.text(s["name"], f"{path}[{index}].name", nullable=True)
            if r.enum(s["status"], SESSION_STATUSES, f"{path}[{index}].status") and s["status"] in counts:
                counts[s["status"]] += 1
            r.num(s["since"], f"{path}[{index}].since")
    return counts


def _check_counts(running, counts, path, r):
    for key in ("busy", "unknown", "foreign_claude"):
        r.int(running[key], f"{path}.{key}", low=0)
    if counts is not None:
        for key in ("busy", "unknown"):
            if running[key] != counts[key]:
                r.add(f"{path}.{key}", f"must count the {key} sessions ({counts[key]})")


def _check_claude_running(running, path, r):
    keys = ("desktop", "desktop_pids", "checked", "sessions", "busy", "unknown", "foreign_claude", "registry_version")
    if not r.obj(running, keys, path):
        return
    if running["desktop"] is not None:
        r.flag(running["desktop"], f"{path}.desktop")
    r.int(running["desktop_pids"], f"{path}.desktop_pids", nullable=True, low=0)
    r.num(running["checked"], f"{path}.checked")
    r.text(running["registry_version"], f"{path}.registry_version", nullable=True)
    _check_counts(running, _check_sessions(running["sessions"], f"{path}.sessions", r), path, r)


def _check_consumers(consumers, path, r, kinds=CODEX_CONSUMER_KINDS):
    if r.seq(consumers, path):
        for index, c in enumerate(consumers):
            if r.obj(c, ("pid", "kind", "started", "parent"), f"{path}[{index}]"):
                r.int(c["pid"], f"{path}[{index}].pid", low=1)
                r.enum(c["kind"], kinds, f"{path}[{index}].kind")
                r.num(c["started"], f"{path}[{index}].started")
                r.text(c["parent"], f"{path}[{index}].parent", nullable=True)


def _check_codex_running(running, path, r):
    if not r.obj(running, ("consumers", "daemon", "desktop", "checked"), path):
        return
    _check_consumers(running["consumers"], f"{path}.consumers", r)
    for key in ("daemon", "desktop"):
        if running[key] is not None:
            r.flag(running[key], f"{path}.{key}")
    r.num(running["checked"], f"{path}.checked")


def _check_agy(entry, path, r):
    """`providers.antigravity` beyond the keys every app has (`status`: no status page is read for it; `active`: the
    id of its row with active true, or null; `switching`; `paused`: its usage runs are paused):

    running      {"consumers": [{"pid", "kind": "agy", "started", "parent"}], "checked": when the processes were
                 read, null when they could not be (then nothing counts as idle)}
    installed    agy was found on this Mac
    store        {"state": AGY_STORE_STATES, "reason": AGY_STORE_REASONS, "message": text}: agy's sign-in store as
                 last sensed; reason and message are set exactly when the state is "unknown" (else null)
    signed_in    the e-mail the store holds, exactly when its state is "ok" (else null)
    accounts     the rows the section shows, in order: each saved account, then the signed-in one when it is not
                 saved (id "unsaved-antigravity", saved false, active, not switchable). A row is {"id" (32 hex when
                 saved), "label", "email", "plan" (text or null), "saved", "active" (agy is signed in to it),
                 "switchable" (it has a saved sign-in AccountDeck can put back), "usage" (its last reading or null:
                 `_check_agy_reading`), "error" (null or {"code": ERROR_CODES, "message", "since": time or null})}
    refresh      {"busy": a usage run is under way, "last_try": when the last one started (or null),
                  "next_allowed": the earliest time Refresh is taken again (null: now)}
    enrollment   null, or an Add under way or just ended: {"phase": AGY_ENROLLMENT_PHASES, "account": the saved
                 account being signed in again (null: a new one), "started", "expires": when it gives up (or null;
                 null while "restoring"), "email": who signed in (set once saved), "message": text, may be empty;
                 while "restoring" it says what the Add waits for}
    recovery     null, or a switch left for Recover (AGY_RECOVERY_CHOICES): {"started", "account": the target's id
                 (or null), "before": the e-mail of the record before it (null: there was none), "after": the
                 target's e-mail (or null)}
    notice_seen  the notice about Google's terms was accepted (the `agy_notice_seen` setting)
    t3_code      T3 Code's own Antigravity folder exists on this Mac (it keeps a sign-in a switch here leaves alone)

    No token, record or secret: a key named like one is refused anywhere in the block, and so is agy's keychain
    value."""
    _check_agy_secrets(entry, path, r)
    running = entry["running"]
    if r.obj(running, ("consumers", "checked"), f"{path}.running"):
        _check_consumers(running["consumers"], f"{path}.running.consumers", r, AGY_CONSUMER_KINDS)
        r.num(running["checked"], f"{path}.running.checked")
    r.flag(entry["installed"], f"{path}.installed")
    r.flag(entry["notice_seen"], f"{path}.notice_seen")
    r.flag(entry["t3_code"], f"{path}.t3_code")
    store, state = entry["store"], None
    if r.obj(store, ("state", "reason", "message"), f"{path}.store") and r.enum(store["state"], AGY_STORE_STATES,
                                                                                 f"{path}.store.state"):
        state = store["state"]
        unknown = state == "unknown"
        r.enum(store["reason"], AGY_STORE_REASONS, f"{path}.store.reason", nullable=not unknown)
        r.text(store["message"], f"{path}.store.message", nullable=not unknown)
        if not unknown and (store["reason"] is not None or store["message"] is not None):
            r.add(f"{path}.store", "has a reason and a message only when its state is unknown")
    signed_in = entry["signed_in"]
    r.text(signed_in, f"{path}.signed_in", nullable=True)
    if state is not None and (signed_in is not None) != (state == "ok"):
        r.add(f"{path}.signed_in", "is the store's e-mail exactly when its state is ok")
    if r.seq(entry["accounts"], f"{path}.accounts"):
        rows = [a for index, a in enumerate(entry["accounts"]) if _check_agy_account(a, f"{path}.accounts[{index}]", r)]
        ids = [a["id"] for a in rows]
        emails = [a["email"].lower() for a in rows]
        for duplicate in {x for x in ids if ids.count(x) > 1} | {x for x in emails if emails.count(x) > 1}:
            r.add(f"{path}.accounts", f"{duplicate!r} appears more than once")
        active = [a for a in rows if a["active"]]
        if isinstance(signed_in, str) and len(rows) == len(entry["accounts"]) and (
                len(active) != 1 or active[0]["email"].lower() != signed_in.lower()):
            r.add(f"{path}.accounts", "the signed-in account is the one active row")
    refresh = entry["refresh"]
    if r.obj(refresh, ("busy", "last_try", "next_allowed"), f"{path}.refresh"):
        r.flag(refresh["busy"], f"{path}.refresh.busy")
        r.num(refresh["last_try"], f"{path}.refresh.last_try")
        r.num(refresh["next_allowed"], f"{path}.refresh.next_allowed")
    enrollment = entry["enrollment"]
    keys = ("phase", "account", "started", "expires", "email", "message")
    if enrollment is not None and r.obj(enrollment, keys, f"{path}.enrollment"):
        r.enum(enrollment["phase"], AGY_ENROLLMENT_PHASES, f"{path}.enrollment.phase")
        r.text(enrollment["account"], f"{path}.enrollment.account", nullable=True)
        r.num(enrollment["started"], f"{path}.enrollment.started", nullable=False)
        r.num(enrollment["expires"], f"{path}.enrollment.expires")
        r.text(enrollment["email"], f"{path}.enrollment.email", nullable=enrollment["phase"] != "saved")
        r.text(enrollment["message"], f"{path}.enrollment.message", empty=True)
    recovery = entry["recovery"]
    if recovery is not None and r.obj(recovery, ("started", "account", "before", "after"), f"{path}.recovery"):
        r.num(recovery["started"], f"{path}.recovery.started", nullable=False)
        for key in ("account", "before", "after"):
            r.text(recovery[key], f"{path}.recovery.{key}", nullable=True)


def _check_agy_account(a, path, r):
    """One row of `providers.antigravity.accounts`; True when it is well-formed enough for cross-checks."""
    if not r.obj(a, _AGY_ACCOUNT_KEYS, path):
        return False
    before = len(r.items)
    for key in ("id", "label", "email"):
        r.text(a[key], f"{path}.{key}")
    r.text(a["plan"], f"{path}.plan", nullable=True)
    for key in ("saved", "active", "switchable"):
        r.flag(a[key], f"{path}.{key}")
    if a["usage"] is not None:
        _check_agy_reading(a["usage"], f"{path}.usage", r)
    if a["error"] is not None and r.obj(a["error"], ("code", "message", "since"), f"{path}.error"):
        r.enum(a["error"]["code"], ERROR_CODES, f"{path}.error.code")
        r.text(a["error"]["message"], f"{path}.error.message")
        r.num(a["error"]["since"], f"{path}.error.since")
    if len(r.items) > before:
        return False
    unsaved = UNSAVED_IDS["antigravity"]
    if a["saved"] and not (len(a["id"]) == 32 and set(a["id"]) <= set("0123456789abcdef")):
        r.add(f"{path}.id", "a saved Antigravity account's id is 32 lower-case hex digits")
    if not a["saved"] and (a["id"] != unsaved or not a["active"] or a["switchable"]):
        r.add(path, f"only {unsaved!r} may be unsaved, and it is active and not switchable")
    return len(r.items) == before


def _check_agy_reading(reading, path, r):
    """A normalised Antigravity usage reading (`providers.antigravity.parse_usage`): {"at": when it was taken,
    "groups": [{"name", "description" (text or null), "buckets": [{"id", "name", "window" ("weekly", "5h" or what
    agy named), "remaining": 0..1 or null, "amount": a number ≥ 0 or null, "resets_at": time or null,
    "disabled"}]}]}. At most AGY_GROUPS_MAX groups of AGY_BUCKETS_MAX buckets, every text at most AGY_TEXT_MAX
    characters, bucket ids unique within the reading."""
    if not r.obj(reading, ("at", "groups"), path):
        return
    r.num(reading["at"], f"{path}.at", nullable=False)
    if not r.seq(reading["groups"], f"{path}.groups"):
        return
    if len(reading["groups"]) > AGY_GROUPS_MAX:
        r.add(f"{path}.groups", f"at most {AGY_GROUPS_MAX} groups")
    ids = []
    for index, group in enumerate(reading["groups"]):
        where = f"{path}.groups[{index}]"
        if not r.obj(group, ("name", "description", "buckets"), where):
            continue
        _agy_text(group["name"], f"{where}.name", r)
        _agy_text(group["description"], f"{where}.description", r, nullable=True)
        if not r.seq(group["buckets"], f"{where}.buckets"):
            continue
        if len(group["buckets"]) > AGY_BUCKETS_MAX:
            r.add(f"{where}.buckets", f"at most {AGY_BUCKETS_MAX} buckets")
        for number, bucket in enumerate(group["buckets"]):
            spot = f"{where}.buckets[{number}]"
            if not r.obj(bucket, _AGY_BUCKET_KEYS, spot):
                continue
            for key in ("id", "name", "window"):
                _agy_text(bucket[key], f"{spot}.{key}", r)
            r.num(bucket["remaining"], f"{spot}.remaining", low=0, high=1)
            r.num(bucket["amount"], f"{spot}.amount", low=0)
            r.num(bucket["resets_at"], f"{spot}.resets_at")
            r.flag(bucket["disabled"], f"{spot}.disabled")
            ids.append(bucket["id"])
    for duplicate in {x for x in ids if ids.count(x) > 1}:
        r.add(f"{path}.groups", f"bucket id {duplicate!r} appears more than once")


def _agy_text(value, path, r, nullable=False):
    if r.text(value, path, nullable=nullable) and isinstance(value, str) and len(value) > AGY_TEXT_MAX:
        r.add(path, f"at most {AGY_TEXT_MAX} characters")


def _check_agy_secrets(value, path, r):
    """Refuses every key in the Antigravity block named like a part of a sign-in record or a secret, and agy's
    keychain value anywhere in it."""
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).lower()
            if name in FORBIDDEN_KEYS or name in AGY_RECORD_KEYS or any(part in name for part in _AGY_SECRET_PARTS):
                r.add(f"{path}.{key}", "no part of a sign-in record and no secret leaves the server")
            _check_agy_secrets(item, f"{path}.{key}", r)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_agy_secrets(item, f"{path}[{index}]", r)
    elif isinstance(value, str) and _AGY_SECRET_MARK in value:
        r.add(path, "agy's keychain value never leaves the server")


def _check_now_band(band, state, accounts, complete, now, r):
    if not r.obj(band, ("next_reset", "best", "attention"), "now_band"):
        return
    by_id = {a["id"]: a for a in accounts}
    # Accounts that failed their own checks are already reported; references to them are not "unknown".
    listed = {a.get("id") for a in state["accounts"] if isinstance(a, dict)} if isinstance(state["accounts"], list) else set()
    hero = band["next_reset"]
    if r.obj(hero, _HERO_KEYS, "now_band.next_reset") and r.enum(hero["kind"], NEXT_RESET_KINDS, "now_band.next_reset.kind"):
        r.text(hero["message"], "now_band.next_reset.message")
        r.int(hero["more_within_24h"], "now_band.next_reset.more_within_24h", low=0)
        r.num(hero["reset"], "now_band.next_reset.reset")
        r.num(hero["left"], "now_band.next_reset.left")
        r.enum(hero["severity"], SEVERITIES, "now_band.next_reset.severity", nullable=True)
        if complete and state["ready"] is True:
            expected = rules.next_reset(accounts, rules.strip(accounts, now), now)
            if hero["kind"] != expected["kind"]:
                r.add("now_band.next_reset.kind", f"must be {expected['kind']!r}")
            elif hero["kind"] in ("next_reset", "refilled") and (
                    (hero["reset"], hero["left"]) != (expected["reset"], expected["left"])
                    or hero["account"] not in by_id):
                r.add("now_band.next_reset", f"must be {expected['label']}'s {expected['window_label']} window")
            elif hero["more_within_24h"] != expected["more_within_24h"]:
                r.add("now_band.next_reset.more_within_24h", f"must be {expected['more_within_24h']}")
    best = band["best"]
    if r.obj(best, ACCOUNT_PROVIDERS, "now_band.best"):
        for extra in set(best) - set(ACCOUNT_PROVIDERS):
            r.add(f"now_band.best.{extra}", "best is per provider (claude, codex, claude_cli; never antigravity)")
        for p in ACCOUNT_PROVIDERS:
            _check_best(best[p], p, state, accounts, listed, r)
    if r.seq(band["attention"], "now_band.attention"):
        _check_attention(band["attention"], by_id, listed, now, r)


def _check_best(b, provider, state, accounts, listed, r):
    path = f"now_band.best.{provider}"
    if not r.obj(b, _BEST_KEYS, path):
        return
    ok = all((r.enum(b["state"], BEST_STATES, f"{path}.state"), r.enum(b["policy"], POLICIES, f"{path}.policy"),
              r.enum(b["confidence"], CONFIDENCES, f"{path}.confidence"),
              r.flag(b["suggest_switch"], f"{path}.suggest_switch"), r.flag(b["current_is_best"], f"{path}.current_is_best"),
              r.num(b["headroom"], f"{path}.headroom", low=0, high=100), r.text(b["reason"], f"{path}.reason", nullable=True),
              r.seq(b["alternatives"], f"{path}.alternatives")))
    if not ok:
        return
    mine = {a["id"]: a for a in accounts if a["provider"] == provider}
    others = {a["id"] for a in accounts if a["provider"] != provider}
    active = next((a["id"] for a in mine.values() if a["active"]), None)
    for aid in [b["account"]] + list(b["alternatives"]):
        if aid in others:
            r.add(path, f"{aid!r} is not a {provider} account (best is never cross-provider)")
        elif aid is not None and aid not in mine and aid not in listed:
            r.add(path, f"unknown account {aid!r}")
    if b["account"] in b["alternatives"]:
        r.add(f"{path}.alternatives", "must not repeat the best account")
    switchable = sum(a["switchable"] for a in mine.values())
    if (b["state"] == "none") != (switchable < 2):
        r.add(f"{path}.state", "is 'none' exactly when fewer than 2 accounts are switchable")
    if b["state"] == "paused":
        r.add(f"{path}.state", "is never 'paused': an incident does not pause the recommendation")
    if b["state"] in ("all_exhausted", "paused", "none") and (b["account"] is not None or b["suggest_switch"]):
        r.add(path, f"a {b['state']!r} recommendation names no account and suggests nothing")
    if (b["account"] is None) != (b["headroom"] is None):
        r.add(f"{path}.headroom", "is set exactly when an account is named")
    if b["suggest_switch"] and (b["state"] != "ok" or b["account"] is None or b["account"] == active or b["current_is_best"]):
        r.add(f"{path}.suggest_switch", "only an 'ok' recommendation of another account can suggest a switch")
    if b["current_is_best"] and b["account"] != active:
        r.add(f"{path}.current_is_best", "requires the best account to be the active one")
    named = b["account"] if b["state"] in ("ok", "unknown") else None
    for aid, account in mine.items():
        if account["recommended"] != (aid == named):
            r.add(path, f"account {aid!r} recommended flag must be {aid == named}")


def _check_attention(items, by_id, listed, now, r):
    order = {kind: index for index, kind in enumerate(ATTENTION_KINDS)}
    if len(items) > 6:
        r.add("now_band.attention", "at most 6 entries")
    ranks = []
    for index, item in enumerate(items):
        path = f"now_band.attention[{index}]"
        if not (r.obj(item, ("kind", "account", "label", "message"), path)
                and r.enum(item["kind"], ATTENTION_KINDS, f"{path}.kind")):
            return
        r.text(item["message"], f"{path}.message")
        ranks.append(order[item["kind"]])
        account = by_id.get(item["account"])
        if account is None:
            if item["account"] not in listed:
                r.add(f"{path}.account", f"unknown account {item['account']!r}")
            continue
        kind = item["kind"]
        if kind == "out" and not rules.account_out(account):
            r.add(path, "an 'out' entry needs an out window")
        elif kind == "signin" and account["credential"]["state"] != "reauth_required":
            r.add(path, "a 'signin' entry needs credential state reauth_required")
        elif kind == "stale" and account["age_tier"] != "stale":
            r.add(path, "a 'stale' entry needs a reading older than 60 min")
        elif kind == "rate_limited" and not (account["paused_until"] or 0) > now:
            r.add(path, "a 'rate_limited' entry needs paused_until in the future")
    if ranks != sorted(ranks):
        r.add("now_band.attention", f"must be sorted {' → '.join(ATTENTION_KINDS)}")


def _check_strip(rows, accounts, complete, now, r):
    if not r.seq(rows, "resetting_soon"):
        return
    for index, row in enumerate(rows):
        path = f"resetting_soon[{index}]"
        if not r.obj(row, _STRIP_KEYS, path):
            return
        if not all((r.enum(row["chip"], CHIPS, f"{path}.chip"), r.enum(row["bucket"], BUCKETS, f"{path}.bucket"),
                    r.num(row["reset"], f"{path}.reset", nullable=False), r.num(row["left"], f"{path}.left"),
                    r.enum(row["severity"], SEVERITIES, f"{path}.severity"))):
            return
    if not complete:
        return
    expected = rules.strip(accounts, now)
    fields = _STRIP_KEYS + ("confirmed",)
    for index, (got, want) in enumerate(zip(rows, expected, strict=False)):
        if any(got.get(f) != want.get(f) for f in fields):
            diff = ", ".join(f"{f}={got.get(f)!r} (want {want.get(f)!r})" for f in fields if got.get(f) != want.get(f))
            r.add(f"resetting_soon[{index}]", f"does not follow the strip rules: {diff}")
            return
    if len(rows) != len(expected):
        missing = [f"{w['label']} {w['window_label']}" for w in expected[len(rows):]]
        extra = [f"{g['label']} {g['window_label']}" for g in rows[len(expected):]]
        r.add("resetting_soon", f"has {len(rows)} entries, the rules give {len(expected)}"
                                f"{f'; missing {missing}' if missing else ''}{f'; unexpected {extra}' if extra else ''}")


def _check_steps(steps, path, r):
    if not r.seq(steps, path):
        return
    ids = []
    for index, step in enumerate(steps):
        if r.obj(step, ("id", "label", "status"), f"{path}[{index}]"):
            r.text(step["id"], f"{path}[{index}].id")
            r.text(step["label"], f"{path}[{index}].label")
            r.enum(step["status"], STEP_STATUSES, f"{path}[{index}].status")
            for key in ("started", "ended"):
                r.num(step.get(key), f"{path}[{index}].{key}")
            ids.append(step["id"])
    if len(ids) != len(set(ids)):
        r.add(path, "step ids must be unique")


def _check_pending(pending, r):
    if pending is None or not r.obj(pending, _PENDING_KEYS, "pending"):
        return
    r.text(pending["id"], "pending.id")
    r.text(pending["kind"], "pending.kind")
    # Operations spanning every provider (the move import) carry no provider.
    r.enum(pending["provider"], PROVIDERS, "pending.provider", nullable=pending["kind"] != "switch")
    r.enum(pending["phase"], PHASES, "pending.phase")
    r.text(pending["message"], "pending.message")
    r.flag(pending["cancellable"], "pending.cancellable")
    r.num(pending["started"], "pending.started", nullable=False)
    r.num(pending["eta_seconds"], "pending.eta_seconds", low=0)
    r.enum(pending["consent"], CONSENTS, "pending.consent", nullable=True)
    if r.seq(pending["waiting_on"], "pending.waiting_on"):
        for index, item in enumerate(pending["waiting_on"]):
            if r.obj(item, ("pid", "name", "status"), f"pending.waiting_on[{index}]"):
                r.int(item["pid"], f"pending.waiting_on[{index}].pid", low=1)
                r.enum(item["status"], SESSION_STATUSES, f"pending.waiting_on[{index}].status")
    _check_steps(pending["steps"], "pending.steps", r)
    if pending["kind"] == "switch" and pending["provider"] == "claude" and isinstance(pending["steps"], list):
        ids = [s.get("id") for s in pending["steps"] if isinstance(s, dict)]
        if ids != [sid for sid in CLAUDE_SWITCH_STEPS if sid in ids]:
            r.add("pending.steps", f"a Claude switch uses the steps {', '.join(CLAUDE_SWITCH_STEPS)} in that order")
    if pending["phase"] == "needs_reconfirm" and "plan" not in pending:
        r.add("pending.plan", "needs_reconfirm carries the fresh plan")
    if pending.get("plan") is not None:
        _check_plan(pending["plan"], "pending.plan", r)


def _check_report(report, r):
    if report is None or not r.obj(report, _REPORT_KEYS, "report"):
        return
    r.text(report["kind"], "report.kind")
    r.enum(report["provider"], PROVIDERS, "report.provider", nullable=report["kind"] != "switch")
    r.enum(report["outcome"], OUTCOMES, "report.outcome")
    r.enum(report["verified"], VERIFICATIONS, "report.verified", nullable=True)
    r.enum(report["stop_tier"], STOP_TIERS, "report.stop_tier", nullable=True)
    r.num(report["finished"], "report.finished", nullable=False)
    r.num(report["verified_at"], "report.verified_at")
    r.int(report["stop_ms"], "report.stop_ms", nullable=True, low=0)
    r.int(report["stragglers"], "report.stragglers", nullable=True, low=0)
    r.text(report["summary"], "report.summary")
    for key, required in (("carried", ("label", "status", "detail")), ("account_bound", ("label", "detail"))):
        if r.seq(report[key], f"report.{key}"):
            for index, item in enumerate(report[key]):
                if r.obj(item, required, f"report.{key}[{index}]"):
                    r.text(item["label"], f"report.{key}[{index}].label")
    if report["error"] is not None and r.obj(report["error"], ("code", "message", "step"), "report.error"):
        r.enum(report["error"]["code"], ERROR_CODES, "report.error.code")


def _check_messages(state, r):
    if r.seq(state["notices"], "notices"):
        for index, n in enumerate(state["notices"]):
            path = f"notices[{index}]"
            if r.obj(n, ("id", "level", "message", "at", "sticky"), path):
                r.text(n["id"], f"{path}.id")
                r.enum(n["level"], LEVELS, f"{path}.level")
                r.text(n["message"], f"{path}.message")
                r.num(n["at"], f"{path}.at", nullable=False)
                r.flag(n["sticky"], f"{path}.sticky")
    alert_keys = ("id", "kind", "level", "account", "window", "title", "message", "at", "snoozed_until", "dismissed")
    if r.seq(state["alerts"], "alerts"):
        for index, a in enumerate(state["alerts"]):
            path = f"alerts[{index}]"
            if r.obj(a, alert_keys, path):
                r.text(a["id"], f"{path}.id")
                r.enum(a["kind"], ALERT_KINDS, f"{path}.kind")
                r.enum(a["level"], LEVELS, f"{path}.level")
                r.text(a["title"], f"{path}.title")
                r.text(a["message"], f"{path}.message")
                r.num(a["at"], f"{path}.at", nullable=False)
                r.num(a["snoozed_until"], f"{path}.snoozed_until")
                r.flag(a["dismissed"], f"{path}.dismissed")
    kinds = set()
    if r.seq(state["banners"], "banners"):
        for index, b in enumerate(state["banners"]):
            path = f"banners[{index}]"
            if r.obj(b, ("kind", "title", "message", "actions", "provider", "link"), path):
                if r.enum(b["kind"], BANNER_KINDS, f"{path}.kind"):
                    kinds.add(b["kind"])
                r.text(b["title"], f"{path}.title")
                r.text(b["message"], f"{path}.message")
                r.enum(b["provider"], PROVIDERS, f"{path}.provider", nullable=True)
                r.text(b["link"], f"{path}.link", nullable=True)
                if r.seq(b["actions"], f"{path}.actions"):
                    for position, action in enumerate(b["actions"]):
                        r.enum(action, ACTIONS, f"{path}.actions[{position}]")
    if state["mode"] == "demo" and "demo" not in kinds:
        r.add("banners", "demo mode shows the demo banner")
    if state["read_only"] is True and "read_only" not in kinds:
        r.add("banners", "read-only mode shows a read_only banner")
    if r.seq(state["events"], "events"):
        for index, e in enumerate(state["events"]):
            path = f"events[{index}]"
            if r.obj(e, ("id", "at", "kind", "message"), path):
                r.int(e["id"], f"{path}.id")
                r.num(e["at"], f"{path}.at", nullable=False)
                r.text(e["kind"], f"{path}.kind")
                r.text(e["message"], f"{path}.message")


def _check_reset_rows(rows, path, r):
    keys = []
    for index, row in enumerate(rows):
        where = f"{path}[{index}]"
        if not r.obj(row, _RESET_ROW_KEYS, where):
            continue
        r.int(row["count"], f"{where}.count", low=1)
        r.num(row["expires"], f"{where}.expires")
        if r.text(row["title"], f"{where}.title", nullable=True) and row["title"] is not None and len(row["title"]) > RESET_TITLE_MAX:
            r.add(f"{where}.title", f"must be at most {RESET_TITLE_MAX} characters")
        if not isinstance(row["key"], str) or not RESET_KEY.fullmatch(row["key"]):
            r.add(f"{where}.key", "must match ^[A-Za-z0-9_-]{1,64}$")
        keys.append(row["key"])
        r.text(row["restores"], f"{where}.restores", nullable=True)
        if r.flag(row["usable"], f"{where}.usable") and r.text(row["reason"], f"{where}.reason", nullable=True):
            if row["usable"] is False and row["reason"] is None:
                r.add(f"{where}.reason", "must say why a row is not usable")
            if row["usable"] is True and row["reason"] is not None:
                r.add(f"{where}.reason", "must be null for a usable row")
    if len(set(keys)) != len(keys):
        r.add(path, "row keys must be unique")


def _check_resets(block, rows, path, r):
    if not r.obj(block, _RESETS_KEYS, path):
        return
    r.num(block["checked"], f"{path}.checked")
    if r.int(block["available"], f"{path}.available", low=0) and block["available"] != sum(
            row.get("count", 0) for row in rows if isinstance(row, dict) and _is_int(row.get("count"))):
        r.add(f"{path}.available", "must be the sum of the rows' counts")
    if r.flag(block["can_use"], f"{path}.can_use") and block["can_use"] and not any(
            isinstance(row, dict) and row.get("usable") is True for row in rows):
        r.add(f"{path}.can_use", "must be false without a usable row")
    r.text(block["blocked"], f"{path}.blocked", nullable=True)
    if block["can_use"] is True and block["blocked"] is not None:
        r.add(f"{path}.blocked", "must be null while a reset can be used")
    if block["can_use"] is False and _is_int(block["available"]) and block["available"] > 0 and block["blocked"] is None:
        r.add(f"{path}.blocked", "must say why none of the resets can be used")
    r.text(block["note"], f"{path}.note", nullable=True)
    doubt = block["doubt"]
    if doubt is not None and r.obj(doubt, ("since",), f"{path}.doubt"):
        r.num(doubt["since"], f"{path}.doubt.since", nullable=False)


def _check_reset_plan(plan, path, r):
    if not r.obj(plan, _RESET_PLAN_KEYS, path):
        return
    _forbidden(plan, path, r)
    if not isinstance(plan["plan_id"], str) or not RESET_PLAN_ID.fullmatch(plan["plan_id"]):
        r.add(f"{path}.plan_id", "must be rp_ and 32 hex digits")
    r.num(plan["expires"], f"{path}.expires", nullable=False)
    r.enum(plan["provider"], RESET_PROVIDERS, f"{path}.provider")
    account = plan["account"]
    if r.obj(account, ("id", "label", "email", "plan"), f"{path}.account"):
        r.text(account["id"], f"{path}.account.id")
        r.text(account["label"], f"{path}.account.label")
        r.text(account["email"], f"{path}.account.email", nullable=True)
        r.text(account["plan"], f"{path}.account.plan", nullable=True)
    if r.seq(plan["choices"], f"{path}.choices"):
        _check_reset_rows(plan["choices"], f"{path}.choices", r)
    if plan["default"] is not None and not any(isinstance(row, dict) and row.get("key") == plan["default"]
                                               and row.get("usable") is True for row in plan["choices"] or ()):
        r.add(f"{path}.default", "must be null or the key of a usable choice")
    for key in ("effects", "warnings"):
        if r.seq(plan[key], f"{path}.{key}"):
            for index, text in enumerate(plan[key]):
                r.text(text, f"{path}.{key}[{index}]")
    if r.seq(plan["blockers"], f"{path}.blockers"):
        for index, blocker in enumerate(plan["blockers"]):
            if r.obj(blocker, ("code", "message"), f"{path}.blockers[{index}]"):
                r.enum(blocker["code"], ERROR_CODES, f"{path}.blockers[{index}].code")
                r.text(blocker["message"], f"{path}.blockers[{index}].message")
    if plan["default"] is None and not plan["blockers"]:
        r.add(f"{path}.blockers", "must say why when no choice can be used")


def _check_plan_windows(windows, path, r):
    if r.seq(windows, path):
        for index, w in enumerate(windows):
            if r.obj(w, _WINDOW_KEYS, f"{path}[{index}]"):
                r.enum(w["state"], WINDOW_STATES, f"{path}[{index}].state")
                if w["known"] != (w["used"] is not None):
                    r.add(f"{path}[{index}].known", "known must be true exactly when used is a number")


def _check_plan(plan, path, r):
    if not r.obj(plan, _PLAN_KEYS, path):
        return
    if not (isinstance(plan["plan_id"], str) and plan["plan_id"].startswith("pl_") and len(plan["plan_id"]) > 3):
        r.add(f"{path}.plan_id", "must be 'pl_' + an opaque id")
    r.num(plan["expires"], f"{path}.expires", nullable=False)
    r.enum(plan["provider"], PROVIDERS, f"{path}.provider")
    r.enum(plan["purpose"], PURPOSES, f"{path}.purpose")
    target = plan["target"]
    if target is None:
        if plan["purpose"] == "switch":
            r.add(f"{path}.target", "a switch plan names its target")
    elif r.obj(target, ("id", "label", "email", "plan", "windows", "checked", "age_tier", "dormant_days"), f"{path}.target"):
        r.text(target["id"], f"{path}.target.id")
        r.text(target["label"], f"{path}.target.label")
        r.enum(target["age_tier"], AGE_TIERS, f"{path}.target.age_tier", nullable=True)
        r.int(target["dormant_days"], f"{path}.target.dormant_days", nullable=True, low=0)
        _check_plan_windows(target["windows"], f"{path}.target.windows", r)
    if plan["current"] is not None and r.obj(plan["current"], ("id", "label", "windows"), f"{path}.current"):
        _check_plan_windows(plan["current"]["windows"], f"{path}.current.windows", r)
        # Switching away from the unsaved signed-in account saves it first (PLAN §9.4).
        if plan["purpose"] == "switch" and plan["saves_outgoing"] is not (plan["current"]["id"] in UNSAVED_IDS.values()):
            r.add(f"{path}.saves_outgoing", "is true exactly when the current account is the unsaved signed-in one")
    if plan["headroom"] is not None and r.obj(plan["headroom"], ("now", "after"), f"{path}.headroom"):
        r.text(plan["headroom"]["now"], f"{path}.headroom.now", nullable=True)
        r.text(plan["headroom"]["after"], f"{path}.headroom.after", nullable=True)
    running = plan["running"]
    keys = ("desktop", "sessions", "busy", "unknown", "foreign_claude", "codex_consumers")
    idle = None
    if r.obj(running, keys, f"{path}.running"):
        counts = _check_sessions(running["sessions"], f"{path}.running.sessions", r)
        _check_counts(running, counts, f"{path}.running", r)
        _check_consumers(running["codex_consumers"], f"{path}.running.codex_consumers", r)
        # `agy_consumers`: the agy processes an Antigravity plan waits for; only its plans carry it (and must).
        agy = running.get("agy_consumers", [])
        if "agy_consumers" in running:
            _check_consumers(agy, f"{path}.running.agy_consumers", r, AGY_CONSUMER_KINDS)
        elif plan["provider"] == "antigravity":
            r.add(f"{path}.running", "missing 'agy_consumers'")
        if (_is_int(running["busy"]) and _is_int(running["unknown"]) and isinstance(running["codex_consumers"], list)
                and isinstance(agy, list)):
            # Idle: no Code session busy or unknown and no Codex or agy consumer left ("Switch now" needs none, §7.8).
            idle = running["busy"] == 0 and running["unknown"] == 0 and not running["codex_consumers"] and not agy
    pairs = plan["process_set"]
    if not (isinstance(pairs, list) and all(isinstance(p, list) and len(p) == 2 and all(_is_int(v) for v in p) for p in pairs)):
        r.add(f"{path}.process_set", "must be a list of [pid, creation_time] pairs")
    signature = plan["signature"]
    if not (isinstance(signature, str) and len(signature) == 71 and signature.startswith("sha256:")
            and all(ch in "0123456789abcdef" for ch in signature[7:])):
        r.add(f"{path}.signature", "must be 'sha256:' + 64 hex digits")
    for key, codes in (("blockers", ERROR_CODES), ("warnings", None)):
        if r.seq(plan[key], f"{path}.{key}"):
            for index, item in enumerate(plan[key]):
                if r.obj(item, ("code", "message"), f"{path}.{key}[{index}]"):
                    # A blocker's code becomes the refusal's error code (PLAN §3.4).
                    if codes is not None:
                        r.enum(item["code"], codes, f"{path}.{key}[{index}].code")
                    else:
                        r.text(item["code"], f"{path}.{key}[{index}].code")
                    r.text(item["message"], f"{path}.{key}[{index}].message")
    r.text(plan["tip"], f"{path}.tip", nullable=True)
    r.text(plan["invisible"], f"{path}.invisible", nullable=True)
    for key in ("will", "preserved", "account_bound"):
        if r.seq(plan[key], f"{path}.{key}"):
            for index, line in enumerate(plan[key]):
                r.text(line, f"{path}.{key}[{index}]")
    r.enum(plan["stop_tier"], STOP_TIERS, f"{path}.stop_tier")
    flags_ok = all([r.flag(plan[key], f"{path}.{key}") for key in
                    ("confirmation_required", "idle", "remember_available", "saves_outgoing")])
    if r.enum(plan["default_button"], DEFAULT_BUTTONS, f"{path}.default_button") and flags_ok and idle is not None:
        if plan["idle"] != idle:
            r.add(f"{path}.idle", "is true exactly when no session is busy or unknown and no Codex or agy consumer runs")
        if plan["default_button"] != ("now" if idle else "idle"):
            r.add(f"{path}.default_button", "is 'idle' until the plan is idle, else 'now'")
        if plan["remember_available"] and not idle:
            r.add(f"{path}.remember_available", "only when the plan is idle")
        if not plan["confirmation_required"] and not idle:
            r.add(f"{path}.confirmation_required", "can be skipped only when the plan is idle")
