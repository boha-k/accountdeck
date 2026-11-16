"""Open at login on macOS (docs/mac/PLAN.md §1.9, D3; docs/mac/PLATFORM-MAC.md §4): a user LaunchAgent.

`~/Library/LaunchAgents/local.accountdeck2.plist` runs the app bundle's launcher
(`AccountDeck 2.app/Contents/MacOS/AccountDeck2 --app --no-browser`), never bare Python, so the process
carries the bundle identity. It is loaded with `launchctl bootstrap gui/<uid>` and unloaded with
`launchctl bootout`. `accountdeck.autostart` re-exports the public functions (`Status`, `status`,
`is_enabled`, `set_enabled`, `repair`) for the engine, the menu bar and `run.py --set-autostart`.

Where things live comes from `install.json`, which install.sh writes into the installed app folder
(the folder that holds run.py): the launcher, the LaunchAgents folder, the log folder and whether
launchctl may be used. A `--prefix` install (tests) writes `"manage": false`, so its agent file lands
in the prefix and launchctl is never run for it. Running from a checkout without install.json, start
at login is unavailable: there is no app bundle to start.

Status mapping (PLATFORM-MAC.md §4): `registered` = our plist exists with our label and arguments;
`command` = its arguments, shell-quoted; `approved` = False when `launchctl print-disabled` lists the
label as disabled or `bootstrap` fails with error 5 (switched off under System Settings › Login
Items), otherwise None.

Two launchd rules shape set_enabled. A `bootout` of a job whose process runs stops that process, and
that process can be this very service (started at login). So enabling never boots out a running job
(the rewritten file takes effect at the next login), and disabling removes the file and boots the job
out only when nothing of it runs; a running one stays loaded until logout, where it is dropped.

Seams for tests: `Layout.sandbox(root)` puts every file under a test folder, and `launchctl=` takes
a fake. In safe mode the installed layout and the real `Launchctl` are refused before anything is
read or run, so tests never load a real agent. The real `Launchctl` starts /bin/launchctl through
procs (the only module that starts processes).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import tempfile

from .. import config
from ..config import SafetyViolation
from ..core import DeckError

LABEL = "local.accountdeck2"
BUNDLE_ID = "local.accountdeck2"        # PLAN D3: Info.plist CFBundleIdentifier and the agent's association
ARGS = ("--app", "--no-browser")
LAUNCHER_TAIL = ("AccountDeck 2.app", "Contents", "MacOS", "AccountDeck2")
LAUNCHCTL = "/bin/launchctl"
MANIFEST = "install.json"
MANIFEST_VERSION = 1
TIMEOUT = 20.0                          # seconds for one launchctl call
THROTTLE = 30                           # launchd waits this long before restarting after a crash
RC_BLOCKED = 5                          # "Bootstrap failed: 5: Input/output error": switched off in Login Items
LOG_NAME = "launchd.log"
_DISABLED = re.compile(r'"' + re.escape(LABEL) + r'"\s*=>\s*(\w+)')
_PID = re.compile(r"^\s*pid\s*=\s*(\d+)\s*$", re.MULTILINE)

NOT_INSTALLED = ("Open at login needs the installed app. Run install.sh from the AccountDeck 2 package, "
                 "then try again.")
NOT_ACCEPTED = "macOS did not accept the change to Open at login."
NO_ANSWER = "macOS did not answer in time while changing Open at login."


@dataclass(frozen=True)
class Status:
    registered: bool            # our LaunchAgent file exists
    command: str | None         # what it runs, shell-quoted
    approved: bool | None       # False: switched off in Login Items (or launchctl disabled); None: no record

    @property
    def enabled(self) -> bool:
        """Whether macOS will start AccountDeck 2 at the next login."""
        return self.registered and self.approved is not False


@dataclass(frozen=True)
class Layout:
    """Where the agent file points and lives. `manage` False: write the file only, never run launchctl."""
    launcher: Path              # …/AccountDeck 2.app/Contents/MacOS/AccountDeck2
    launch_agents: Path         # …/Library/LaunchAgents
    logs: Path                  # …/Library/Logs/AccountDeck2 (launchd's stdout and stderr)
    manage: bool

    @property
    def plist(self) -> Path:
        return self.launch_agents / f"{LABEL}.plist"

    @staticmethod
    def installed(root=None) -> "Layout":
        """The layout install.sh recorded in `<root>/install.json` (root: the folder holding run.py).
        In safe mode only a root inside a test folder is read, and every path in it must be there too."""
        if config.safe_mode() and root is None:
            raise SafetyViolation("Open at login is not read or changed in safe mode.")
        root = Path(root) if root is not None else config.REPO_ROOT
        if config.safe_mode():
            _require_test_owned(root)
        try:
            with open(root / MANIFEST, "rb") as handle:
                manifest = json.load(handle)
        except FileNotFoundError:
            raise DeckError(NOT_INSTALLED, "unsupported") from None
        except (OSError, ValueError):
            raise DeckError("The install record of AccountDeck 2 cannot be read. Run install.sh again.",
                            "unsupported") from None
        layout = _from_manifest(manifest)
        if config.safe_mode():
            for path in (layout.launcher, layout.launch_agents, layout.logs):
                _require_test_owned(path)
        return layout

    @staticmethod
    def sandbox(root, *, manage: bool = False) -> "Layout":
        """Everything below `root`, laid out like a home folder (tests). In safe mode `root` must be a
        registered test folder."""
        root = Path(root)
        if config.safe_mode():
            _require_test_owned(root)
        return Layout(launcher=root.joinpath("Applications", *LAUNCHER_TAIL),
                      launch_agents=root / "Library" / "LaunchAgents",
                      logs=root / "Library" / "Logs" / "AccountDeck2", manage=manage)


class Launchctl:
    """/bin/launchctl, started through procs. Refused in safe mode: tests pass a fake."""

    def domain(self) -> str:
        _refuse_in_safe_mode()
        return f"gui/{os.getuid()}"

    def run(self, *args: str) -> tuple[int, str]:
        """(exit code, stdout + stderr). Never carries a secret: the arguments are a label and paths."""
        _refuse_in_safe_mode()
        from .. import procs     # procs imports this package's siblings; imported on use
        controller = procs.get_controller(config.Paths.live(), demo=False)
        process = controller.spawn([LAUNCHCTL, *args], stdin=procs.DEVNULL, stdout=procs.PIPE,
                                   stderr=procs.PIPE)
        try:
            out, err = process.communicate(timeout=TIMEOUT)
        except Exception:
            process.kill()
            process.communicate()
            raise DeckError(NO_ANSWER, "timeout") from None
        return process.returncode, (out or b"").decode("utf-8", "replace") + (err or b"").decode("utf-8", "replace")


def agent_plist(layout: Layout) -> dict:
    """The LaunchAgent (PLATFORM-MAC.md §4) with absolute paths only: launchd expands neither ~ nor $HOME."""
    log = str(layout.logs / LOG_NAME)
    return {
        "Label": LABEL,
        "ProgramArguments": [str(layout.launcher), *ARGS],
        "AssociatedBundleIdentifiers": [BUNDLE_ID],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},      # restart after a crash, not after Quit (exit 0)
        "ThrottleInterval": THROTTLE,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",            # graphical logins only, never an SSH session
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def status(*, layout: Layout | None = None, launchctl=None) -> Status:
    layout = layout or Layout.installed()
    arguments = _registered_arguments(layout)
    approved = None
    if arguments is not None and layout.manage:
        launchctl = launchctl or Launchctl()
        if _disabled(launchctl):
            approved = False
    return Status(registered=arguments is not None,
                  command=shlex.join(arguments) if arguments is not None else None, approved=approved)


def is_enabled(*, layout: Layout | None = None, launchctl=None) -> bool:
    return status(layout=layout, launchctl=launchctl).enabled


def set_enabled(enabled: bool, *, layout: Layout | None = None, launchctl=None) -> Status:
    """Writes (or removes) the agent file and loads (or unloads) it, then returns the resulting state.
    A login item switched off in System Settings stays off: the result then says approved=False."""
    layout = layout or Layout.installed()
    _check_paths(layout)
    if layout.manage:
        launchctl = launchctl or Launchctl()
    blocked = False
    if enabled:
        _write_plist(layout)
        if layout.manage:
            blocked = _load(launchctl, layout)
    else:
        if layout.manage:
            _unload_if_idle(launchctl)
        try:
            layout.plist.unlink(missing_ok=True)
        except OSError:
            raise DeckError(NOT_ACCEPTED, "blocked") from None
    result = status(layout=layout, launchctl=launchctl)
    return replace(result, approved=False) if blocked and result.registered else result


def repair(*, layout: Layout | None = None, launchctl=None) -> bool:
    """Rewrites an agent whose launcher is not this install's; True when it did. An agent that starts
    another copy of AccountDeck 2 which still exists is left alone."""
    layout = layout or Layout.installed()
    current = _registered_arguments(layout)
    wanted = agent_plist(layout)["ProgramArguments"]
    if current is None or current == wanted:
        return False
    theirs = current[0] if current else None
    if theirs and theirs != wanted[0] and Path(theirs).is_file():
        return False
    set_enabled(True, layout=layout, launchctl=launchctl)
    return True


# ---- internals ----

def _from_manifest(manifest) -> Layout:
    bad = DeckError("The install record of AccountDeck 2 is not valid. Run install.sh again.", "unsupported")
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise bad
    paths = {}
    for key in ("launcher", "launch_agents", "logs"):
        value = manifest.get(key)
        if not isinstance(value, str) or "\x00" in value or "\n" in value or not Path(value).is_absolute():
            raise bad
        paths[key] = Path(value)
    if tuple(paths["launcher"].parts[-len(LAUNCHER_TAIL):]) != LAUNCHER_TAIL:
        raise bad
    if not isinstance(manifest.get("manage"), bool):
        raise bad
    return Layout(manage=manifest["manage"], **paths)


def _refuse_in_safe_mode() -> None:
    if config.safe_mode():
        raise SafetyViolation("launchctl is never run in safe mode.")


def _require_test_owned(path) -> None:
    from ..procs import is_test_owned_path      # procs imports config; imported on use
    if not is_test_owned_path(path):
        raise SafetyViolation("Safe mode changes Open at login only inside a test folder.")


def _check_paths(layout: Layout) -> None:
    """Defence in depth: in safe mode every file this module writes is inside a test folder."""
    if config.safe_mode():
        for path in (layout.plist, layout.logs, layout.launcher):
            _require_test_owned(path)


def _registered_arguments(layout: Layout) -> list[str] | None:
    """The ProgramArguments of our agent file, or None when there is none (or it is not ours)."""
    try:
        with open(layout.plist, "rb") as handle:
            data = plistlib.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    if not isinstance(data, dict) or data.get("Label") != LABEL:
        return None
    arguments = data.get("ProgramArguments")
    if not isinstance(arguments, list) or not arguments or not all(isinstance(a, str) for a in arguments):
        return None
    return arguments


def _write_plist(layout: Layout) -> None:
    """Atomically, mode 0644 (launchd ignores an agent file that others may write)."""
    data = plistlib.dumps(agent_plist(layout), fmt=plistlib.FMT_XML, sort_keys=False)
    try:
        layout.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        layout.launch_agents.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".write-", suffix=".plist", dir=layout.launch_agents)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, 0o644)
            os.replace(name, layout.plist)
        finally:
            Path(name).unlink(missing_ok=True)
    except OSError:
        raise DeckError(NOT_ACCEPTED, "blocked") from None


def _job(launchctl) -> tuple[bool, int | None]:
    """(loaded, pid of its running process or None)."""
    rc, out = launchctl.run("print", f"{launchctl.domain()}/{LABEL}")
    if rc != 0:
        return False, None
    match = _PID.search(out)
    return True, int(match.group(1)) if match else None


def _disabled(launchctl) -> bool:
    """True when launchd's override list says our label is disabled ("disabled", or "true" on older macOS)."""
    rc, out = launchctl.run("print-disabled", launchctl.domain())
    if rc != 0:
        return False
    match = _DISABLED.search(out)
    return bool(match) and match.group(1).lower() in ("disabled", "true")


def _load(launchctl, layout: Layout) -> bool:
    """Loads the agent now (RunAtLoad starts it; a running service makes the new copy exit). True when
    macOS refused because the item is switched off in Login Items. Other failures (no graphical login,
    as over SSH) leave the file for the next login."""
    loaded, pid = _job(launchctl)
    if loaded and pid is not None:
        return False                    # running: never boot out; the new file applies at the next login
    domain = launchctl.domain()
    if loaded:
        launchctl.run("bootout", f"{domain}/{LABEL}")          # an idle older copy
    launchctl.run("enable", f"{domain}/{LABEL}")               # clears an earlier `launchctl disable`
    rc, _out = launchctl.run("bootstrap", domain, str(layout.plist))
    if rc == 0:
        return False
    _log("autostart.bootstrap_failed", rc=rc)
    return rc == RC_BLOCKED


def _unload_if_idle(launchctl) -> None:
    loaded, pid = _job(launchctl)
    if loaded and pid is None:
        launchctl.run("bootout", f"{launchctl.domain()}/{LABEL}")


def _log(ev: str, **fields) -> None:
    try:
        from .. import diagnostics
        diagnostics.log(ev, **fields)
    except Exception:
        pass
