"""Machine paths, environment flags, timings and the settings schema (PLAN §2.3-2.5, §3.6, §4.10).

Besides the standard library this module imports only ``core.DeckError`` at import time (core
imports nothing from the package but ``platform.locks``, and resolves ``SafetyViolation`` from here
lazily; ``procs`` is imported inside ``Paths.synthetic``), so every other module can import it
without cycles. It is the only package module that reads the environment or the user's home folder.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import threading

from .core import DeckError

APP = "accountdeck2"
DEFAULT_PORT = 53157
# AccountDeck 1's port: never registered as a test server, so a test can never talk to a v1 left running.
V1_PORT = 53147
SYNTHETIC_CLAUDE_FAMILY = "com.anthropic.claudefordesktop.test"
SYNTHETIC_CODEX_FAMILIES = ("com.openai.codex.test",)
OWNER_LOCK_NAME = "AccountDeck2-Owner.lock"
# Claude Desktop's bundle id (docs/mac/PROVIDERS-MAC.md §1), used only when the installed app's
# Info.plist cannot be read; the app's own CFBundleIdentifier wins.
MAC_CLAUDE_BUNDLE_ID = "com.anthropic.claudefordesktop"
MAC_APPLICATIONS = Path("/Applications")
MAX_INFO_PLIST_BYTES = 1 << 20
REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_LEVELS = ("debug", "info", "warning")


class SafetyViolation(DeckError):
    """Raised before any side effect when safe mode or the consent rules forbid an action (PLAN §4.1)."""

    def __init__(self, message="This action is not allowed in safe mode."):
        super().__init__(message, "safety")


# ---- environment flags (PLAN §2.4); read on every call, never cached ----

def demo_mode() -> bool:
    return os.environ.get("ACCOUNTDECK_DEMO") == "1"


def safe_mode() -> bool:
    """True for tests, demos and development runs; nothing may then reach a real process, file or port."""
    return (os.environ.get("ACCOUNTDECK_SAFE_MODE") == "1" or demo_mode()
            or "unittest" in sys.modules or "pytest" in sys.modules)


def env_data_dir() -> Path | None:
    value = os.environ.get("ACCOUNTDECK2_DATA", "").strip()
    return Path(value) if value else None


def env_port() -> int | None:
    value = os.environ.get("ACCOUNTDECK2_PORT", "").strip()
    if not value.isdigit():
        return None
    port = int(value)
    return port if port <= 65535 else None


def env_log_level() -> str | None:
    value = os.environ.get("ACCOUNTDECK2_LOG_LEVEL", "").strip().lower()
    return value if value in LOG_LEVELS else None


def cli_custom_home() -> bool:
    return bool(os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR"))


def child_env(remove=(), **extra) -> dict[str, str]:
    """This process's environment for a child process, without the `remove` names, plus `extra`."""
    dropped = {name.upper() for name in remove}
    env = {name: value for name, value in os.environ.items() if name.upper() not in dropped}
    env.update(extra)
    return env


# ---- machine paths (PLAN §2.5, frozen contract) ----

@dataclass(frozen=True)
class Paths:
    """Every machine path AccountDeck 2 touches. Build it with `Paths.live()` or `Paths.synthetic(root)`."""
    mode: str                          # "live" | "synthetic"
    home: Path
    data_dir: Path
    claude_profile: Path               # ~/Library/Application Support/Claude
    claude_main_log: Path
    claude_sessions: Path
    claude_projects: Path
    claude_user_config: Path
    codex_home: Path
    codex_exe: Path | None             # None live: found at run time (PATH, ~/.local/bin, ChatGPT.app)
    # Antigravity (`agy`): its sign-in is a login-keychain item, not a file; AccountDeck reads this folder only
    # to see whether agy fell back to a token file (`agy_file_stores`) and never writes under it.
    gemini_home: Path                  # ~/.gemini
    agy_exe: Path | None               # None live: found at run time (~/.local/bin, Homebrew, PATH)
    python_exe: Path
    repo_root: Path
    temp_root: Path | None
    claude_family: str                 # Claude Desktop's bundle id (docs/mac/PLAN.md §1.2)
    codex_families: tuple[str, ...]    # Codex app bundle ids the process table attributes (none live yet)

    @property
    def codex_managed_configs(self) -> tuple[Path, Path]:
        """Codex's system policy files; synthetic homes never consult the machine's /etc."""
        if self.mode == "synthetic":
            base = self.temp_root / "etc" / "codex"
        else:
            if safe_mode():
                raise SafetyViolation("Codex managed settings must be synthetic in safe mode.")
            base = Path("/etc/codex")
        return base / "config.toml", base / "requirements.toml"

    @property
    def claude_cli_home(self):
        return self.home / ".claude"

    @property
    def claude_cli_credentials(self):
        return self.claude_cli_home / ".credentials.json"

    @property
    def claude_cli_refresh_lock(self):
        return self.claude_cli_home / ".oauth_refresh.lock"

    @property
    def claude_cli_legacy_lock(self):
        return self.home / ".claude.lock"

    @property
    def claude_cli_config_lock(self):
        return self.home / ".claude.json.lock"

    @property
    def cli_enroll(self):
        return self.data_dir / "cli-enroll"

    @property
    def agy_home(self):
        """agy's own folder (~/.gemini/antigravity-cli). Read only, never written."""
        return self.gemini_home / "antigravity-cli"

    @property
    def agy_file_stores(self) -> tuple[Path, Path]:
        """Where agy keeps its sign-in when the keychain is unavailable to it. While either exists AccountDeck
        saves and switches no Antigravity account (that store is not handled in this version)."""
        return (self.agy_home / "antigravity-oauth-token", self.gemini_home / "jetski-standalone-oauth-token")

    @property
    def t3_antigravity(self):
        """Where T3 Code keeps an Antigravity sign-in of its own (~/.t3/userdata/providers/antigravity). Only whether
        it is a folder is looked at (the state's `t3_code`); nothing in it is read or written."""
        return self.home / ".t3" / "userdata" / "providers" / "antigravity"

    @property
    def agy_work(self):
        """The fixed, empty working folder AccountDeck's own usage run of agy starts in (inside its data folder)."""
        return self.data_dir / "agy-work"

    @property
    def agy_usage_log(self):
        """Where AccountDeck's own usage run of agy writes its log (`--log-file`), beside AccountDeck's logs."""
        return self.data_dir / "logs" / "agy-usage.log"

    def require_cli_paths(self):
        """Check every terminal path before any read, directory creation or write."""
        from .procs import is_test_owned_path
        protected = (self.claude_cli_home, self.claude_cli_credentials, self.claude_user_config,
                     self.claude_cli_refresh_lock, self.claude_cli_legacy_lock,
                     self.claude_cli_config_lock, self.cli_enroll, self.data_dir)
        if safe_mode() and (self.mode != "synthetic" or not all(is_test_owned_path(p) for p in protected)):
            raise SafetyViolation("Terminal login paths must be synthetic in safe mode.")

    @staticmethod
    def live(data_dir: Path | None = None) -> "Paths":
        """This Mac. Raises SafetyViolation in safe mode, so tests and demos cannot reach it."""
        if safe_mode():
            raise SafetyViolation("The real machine paths are not available in safe mode.")
        return _mac_paths(_mac_home(), data_dir)

    @staticmethod
    def synthetic(root: Path, *, codex_stub: bool = True) -> "Paths":
        """This Mac's layout under `root`: creates `root` (never its parents) and the skeleton inside it,
        and registers `root` with procs as test-owned (PLAN §2.5 rule 4). `codex_stub` names
        `root/bin/codex` as the Codex CLI (providers.fake writes a synthetic app-server there). `root/bin/agy`
        is where a synthetic agy goes; none is written, and no ~/.gemini is made: Antigravity starts absent."""
        root = Path(root).resolve()
        home = root / "home"
        paths = replace(_mac_paths(home, root / "data", applications=()), mode="synthetic",
                        codex_exe=root / "bin" / "codex" if codex_stub else None, agy_exe=root / "bin" / "agy",
                        temp_root=root, claude_family=SYNTHETIC_CLAUDE_FAMILY, codex_families=SYNTHETIC_CODEX_FAMILIES)
        # Registered first: procs refuses a root outside the temp folder (or outside safe mode)
        # before anything is created there.
        _register_test_root(root)
        root.mkdir(exist_ok=True)
        for directory in (paths.data_dir, paths.claude_profile, paths.claude_main_log.parent,
                          paths.claude_sessions, paths.claude_projects, paths.codex_home, root / "bin"):
            directory.mkdir(parents=True, exist_ok=True)
        return paths

    def user_path(self, value: str) -> Path:
        """Resolve a user-selected path against this machine's (possibly synthetic) home."""
        if value == "~":
            return self.home
        if value.startswith("~/"):
            return self.home / value[2:]
        return Path(value)


def owner_lock_path(paths: Paths) -> Path:
    """The per-user owner lock (PLAN §9.1): beside, not inside, the default data folder, so a second
    service on another data folder still cannot manage the same sign-ins."""
    if paths.mode == "synthetic":
        return paths.temp_root / OWNER_LOCK_NAME
    if safe_mode():
        raise SafetyViolation("The real owner lock is not available in safe mode.")
    return paths.home / "Library" / "Application Support" / OWNER_LOCK_NAME


def _mac_home() -> Path:
    """The user's home from the user database: launchd sets $HOME oddly (docs/mac/PLATFORM-MAC.md §7)."""
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _mac_paths(home: Path, data_dir: Path | None = None, *, applications=None) -> Paths:
    """The macOS machine layout (docs/mac/PLAN.md §1.9; PROVIDERS-MAC.md §1) for `home`. Claude Desktop
    keeps one data folder and its family is its bundle id, read from the installed app in
    `applications`. Codex is found at run time (facts_mac.codex_exe), and Codex Desktop's identity is
    still unknown (PROVIDERS-MAC.md §6.6), so no family names it."""
    support = home / "Library" / "Application Support"
    folders = mac_applications(home) if applications is None else tuple(applications)
    family = next(filter(None, (_bundle_id(folder / "Claude.app") for folder in folders)), MAC_CLAUDE_BUNDLE_ID)
    return Paths(
        mode="live", home=home, data_dir=Path(data_dir or env_data_dir() or support / "AccountDeck2"),
        claude_profile=support / "Claude", claude_main_log=home / "Library" / "Logs" / "Claude" / "main.log",
        claude_sessions=home / ".claude" / "sessions", claude_projects=home / ".claude" / "projects",
        claude_user_config=home / ".claude.json", codex_home=home / ".codex", codex_exe=None,
        gemini_home=home / ".gemini", agy_exe=None,
        python_exe=Path(sys.executable), repo_root=REPO_ROOT, temp_root=None,
        claude_family=family, codex_families=())


def mac_applications(home) -> tuple[Path, Path]:
    """The folders macOS apps are installed in, system-wide first (/Applications, then ~/Applications)."""
    return MAC_APPLICATIONS, Path(home) / "Applications"


def _bundle_id(app: Path) -> str | None:
    """An app bundle's CFBundleIdentifier, or None when its Info.plist cannot be read."""
    import plistlib
    try:
        if (Path(app) / "Contents" / "Info.plist").stat().st_size > MAX_INFO_PLIST_BYTES:
            return None
        with open(Path(app) / "Contents" / "Info.plist", "rb") as handle:
            value = plistlib.load(handle).get("CFBundleIdentifier")
    except Exception:            # an unreadable or malformed plist (plistlib, expat and struct errors)
        return None
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9.-]{1,255}", value) else None


bundle_id = _bundle_id   # read at runtime by platform.facts_mac (Claude's launch id, PLAN §1.4)


def _register_test_root(root: Path) -> None:
    from . import procs          # imported here: procs imports config at module level
    procs.register_test_root(root)


# ---- timings (PLAN §4.10); seconds unless noted; tests and demos pass overrides ----

@dataclass(frozen=True)
class Timings:
    plan_ttl: float = 120
    consent_ttl: float = 1800
    idle_hold: float = 20
    idle_poll: float = 2
    graceful_wait: float = 100
    graceful_progress: float = 10
    terminate_wait: float = 10
    helper_self_exit: float = 10
    settle_file: float = 15
    settle_grace: float = 0.7
    ready: float = 30
    verify_hard: float = 90
    verify_soft_every: float = 30
    verify_soft_count: int = 20
    lock_wait: float = 30
    daemon_grace_default: float = 60
    helper_refresh_grace: float = 30
    helper_readonly_grace: float = 3
    launch_retry: tuple[float, ...] = (2, 4, 8)


# ---- settings (PLAN §3.6) ----

@dataclass(frozen=True)
class Setting:
    """One server-side setting. `internal` keys are refused by /api/settings; the engine sets them
    through `Settings.set_internal`."""
    kind: str              # bool | int | choice | text | ids | path | hours | object
    default: object
    choices: tuple = ()
    low: int | None = None
    high: int | None = None
    internal: bool = False


SETTINGS_SCHEMA = {
    "cli_autoswitch": Setting("bool", True),
    "cli_threshold": Setting("int", 95, low=50, high=99),
    "cli_order": Setting("choice", "soonest_reset", ("soonest_reset", "most_room", "manual")),
    "cli_manual_order": Setting("ids", []),
    "cli_early_switch": Setting("bool", True),
    "usage_display": Setting("choice", "used", ("used", "left")),
    "confirm_when_idle": Setting("bool", True),
    "sort": Setting("choice", "stable", ("stable", "best", "resets", "attention", "name", "manual")),
    "density": Setting("choice", "auto", ("auto", "cards", "rows")),
    "pinned_model": Setting("text", "", high=40),
    "use_soon": Setting("bool", False),
    "show_other_limits": Setting("bool", False),
    "cookie_route": Setting("bool", True),
    "alert_low_left": Setting("int", 15, low=5, high=25),
    "alerts_enabled": Setting("bool", True),
    "quiet_hours": Setting("hours", [22, 8]),
    "notify_reset_minutes": Setting("int", 15, low=1, high=180),
    "notifications": Setting("bool", True),
    # The menu bar item (platform/statusbar_mac.py). The app always shows it at start (it holds Quit);
    # turning this off hides it for the rest of the run. The key predates the Mac port ("tray").
    "tray": Setting("bool", True),
    "start_on_login": Setting("bool", False),
    "cwd": Setting("path", ""),                      # "" = the user's home folder
    "log_level": Setting("choice", "info", LOG_LEVELS),
    "refresh_paused": Setting("bool", False),
    # Check for updates automatically: every 5 minutes, and when the user opens AccountDeck, an installed copy asks
    # GitHub whether a newer release exists (accountdeck/updates.py).
    "update_check": Setting("bool", True),
    # Antigravity's usage runs `agy` only when the user asks: Refresh, and, with this on, opening AccountDeck when
    # the last reading is over 15 minutes old (agy_accounts.AgyService.looked). Never on a timer.
    "agy_usage_on_open": Setting("bool", True),
    # "Read Claude limit resets": after a successful usage read of a Claude account, at most hourly (every 15 minutes
    # when the user asked), one more read of its limit resets (accountdeck/resets.py). Codex's come with every reading.
    "claude_resets": Setting("bool", True),
    "recommender_state": Setting("object", {}, internal=True),
    "alert_latches": Setting("object", {}, internal=True),
    "update_memory": Setting("object", {}, internal=True),       # updates.load_memory: what the check remembers
    "agy_notice_seen": Setting("bool", False, internal=True),    # the notice about Google's terms was accepted
}
# Stored settings of earlier builds (Windows and AccountDeck 1 era: taskbar_widget, taskbar_widget_gap,
# claude_aumid, v1_retired_at, first_run_done) stay in the database untouched; Settings ignores any key
# the schema does not know, so an old data folder loads unchanged and a downgrade still finds them.


def validate_settings(patch, *, internal: bool = False) -> dict:
    """Checks a settings patch; returns the normalised values or raises DeckError(code="invalid")."""
    if not isinstance(patch, dict):
        raise DeckError("Settings must be sent as an object.", "invalid")
    clean = {}
    for key, value in patch.items():
        spec = SETTINGS_SCHEMA.get(key) if isinstance(key, str) else None
        if spec is None:
            raise DeckError(f"Unknown setting: {str(key)[:40]}.", "invalid")
        if spec.internal and not internal:
            raise DeckError(f"The {key} setting is managed by AccountDeck and cannot be changed here.", "invalid")
        clean[key] = _checked(key, spec, value)
    return clean


def _checked(key: str, spec: Setting, value):
    kind = spec.kind
    if kind == "ids" and isinstance(value, list) and len(value) <= 500 and all(
            isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", v) for v in value) and len(set(value)) == len(value):
        return list(value)
    if kind == "bool" and type(value) is bool:
        return value
    if kind == "int" and type(value) is int and spec.low <= value <= spec.high:
        return value
    if kind == "choice" and value in spec.choices:
        return value
    if kind == "text" and isinstance(value, str) and len(value.strip()) <= spec.high and value.isprintable():
        return value.strip()
    if kind == "path" and isinstance(value, str) and len(value) <= 1024 and value.isprintable() and (
            value == "" or PurePosixPath(value).is_absolute()):
        return value
    if kind == "hours" and (value is None or (
            isinstance(value, (list, tuple)) and len(value) == 2 and value[0] != value[1]
            and all(type(hour) is int and 0 <= hour <= 23 for hour in value))):
        return None if value is None else list(value)
    if kind == "object" and isinstance(value, dict) and _json_safe(value):
        return copy.deepcopy(value)
    raise DeckError(_EXPECTED[kind].format(key=key, choices=", ".join(spec.choices),
                                           low=spec.low, high=spec.high), "invalid")


_EXPECTED = {
    "ids": "{key} must be a list of unique account ids.",
    "bool": "{key} must be true or false.",
    "int": "{key} must be a whole number from {low} to {high}.",
    "choice": "{key} must be one of: {choices}.",
    "text": "{key} must be plain text of at most {high} characters.",
    "path": "{key} must be a full folder path such as /Users/you/project, or empty for your home folder.",
    "hours": "{key} must be null or [start_hour, end_hour] with two different hours from 0 to 23.",
    "object": "{key} must be a JSON object.",
}


def _json_safe(value) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


class Settings:
    """Current settings over the schema defaults. `save(key, value)` persists each change
    (e.g. `Store.set_setting`); stored values that no longer validate fall back to their default."""

    def __init__(self, stored: dict | None = None, *, save=None, home: Path | None = None):
        self._lock = threading.Lock()
        self._save = save
        self._home = str(home) if home else ""
        self._values = {}
        for key, value in (stored or {}).items():
            spec = SETTINGS_SCHEMA.get(key)
            if spec is None:
                continue
            try:
                self._values[key] = _checked(key, spec, value)
            except DeckError:
                continue

    def get(self, key: str):
        with self._lock:
            value = self._values.get(key, SETTINGS_SCHEMA[key].default)
        if key == "cwd" and not value:
            return self._home
        return copy.deepcopy(value)

    def public(self) -> dict:
        """Every user-facing setting (what /api/state and /api/settings return)."""
        return {key: self.get(key) for key, spec in SETTINGS_SCHEMA.items() if not spec.internal}

    def update(self, patch) -> dict:
        """Applies a user patch from /api/settings; returns the keys that changed with their new values."""
        return self._apply(validate_settings(patch))

    def set_internal(self, key: str, value) -> None:
        self._apply(validate_settings({key: value}, internal=True))

    def _apply(self, clean: dict) -> dict:
        with self._lock:
            changed = {key: value for key, value in clean.items()
                       if self._values.get(key, SETTINGS_SCHEMA[key].default) != value}
            for key, value in changed.items():
                self._values[key] = value
                if self._save:
                    self._save(key, copy.deepcopy(value))
        return copy.deepcopy(changed)
