"""Facts about this Mac besides processes (docs/mac/PLAN.md §1.3, §1.4, §1.9): the data folder's permissions,
Claude Desktop's data folder and launch id, the Codex CLI, Antigravity's agy and the file-release probe.

Claude Desktop keeps one data folder (~/Library/Application Support/Claude), opens by the bundle id read
from the installed app at runtime, and macOS has no mandatory file locks, so "released" means: no Claude
Desktop process is alive and the controller's per-file probe (libproc's open-file scan plus `lsof`) answers
"free" for every sign-in file. Doubt is always "not released". Machine locations come from config; process
control lives only in procs.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat

from .. import config
from ..core import DeckError

# On macOS Chromium keeps Cookies in the profile folder itself; Network/ is checked too in case it appears.
DATA_FOLDER_FILES = (("Cookies",), ("Cookies-journal",), ("Network", "Cookies"), ("Network", "Cookies-journal"),
                     ("config.json",), ("Local State",))
DATA_FOLDER_KIND = "Application Support"
CLAUDE_APP = "Claude.app"
CODEX_APP = "ChatGPT.app"                                   # the Codex app on macOS (PLAN §8)
CODEX_IN_APP = ("Contents", "Resources", "codex-cli", "bin", "codex")
CODEX_PACKAGE = (".local", "share", "openai-codex", "current", "bin", "codex")
CODEX_LAUNCHER = (".local", "bin", "codex")
MAX_LAUNCHER_BYTES = 64 << 10
# Mach-O and universal-binary magic numbers as they appear on disk.
NATIVE_MAGIC = frozenset({b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
                          b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"})
LAUNCHER_PATH = re.compile(r"""(?:\$\{HOME\}|\$HOME|~)?/[^\s"'`;|&<>()$]*/codex(?=["'\s;)]|$)""")
VAULT_MESSAGE = "Could not restrict access to the local account vault."
NOT_INSTALLED_MESSAGE = "Install Codex CLI and sign in once, then try again."


# ---- the data folder and helper work folders ----

def secure_directory(path) -> None:
    """Creates `path` private to this user: a real folder (not a symlink) owned by this user with mode 0700.
    A folder copied from elsewhere keeps its old mode, so the mode is set on every call and read back."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(path)
        os.chmod(path, 0o700)
        if stat.S_IMODE(os.stat(path).st_mode) != 0o700:
            raise PermissionError(path)
    except OSError:
        raise DeckError(VAULT_MESSAGE, "vault") from None


def remove_work_directory(path, root) -> None:
    """Removes an isolated helper directory under `root`, including read-only cache files."""
    path, root = Path(path), Path(root).resolve()
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root) or path.is_symlink():
        raise DeckError("Temporary-folder cleanup stopped at its safety boundary.", "recovery")

    def retry(function, target, error):
        target = Path(target)
        if not target.resolve().is_relative_to(root) or not isinstance(error, PermissionError):
            raise error
        target.chmod(stat.S_IWRITE | stat.S_IREAD)
        function(target)

    if path.exists():
        shutil.rmtree(path, onexc=retry)


# ---- Claude Desktop's identity, data folder and launch id (PLAN §1.2, §1.4, §1.9) ----

def claude_launch_id(paths, *, applications=None) -> str | None:
    """How macOS opens Claude Desktop: the CFBundleIdentifier of the installed Claude.app, read now from its
    Info.plist (never hard-coded; expect com.anthropic.claudefordesktop). None when no installed app has a
    readable one. Synthetic paths answer `paths.claude_family`, the demo's stand-in id."""
    if paths.mode != "live":
        return paths.claude_family
    folders = config.mac_applications(paths.home) if applications is None else tuple(applications)
    for folder in folders:
        found = config.bundle_id(Path(folder) / CLAUDE_APP)
        if found:
            return found
    return None


def claude_base(paths) -> tuple[Path, str, str]:
    """(folder, kind, reason): Claude Desktop keeps one data folder, ~/Library/Application Support/Claude
    (reason "single"; "synthetic" for a test or demo home)."""
    return Path(paths.claude_profile), DATA_FOLDER_KIND, "single" if paths.mode == "live" else "synthetic"


def data_folder_files(paths) -> list[Path]:
    """The files Claude must have released before its sign-in state is touched (§4.6 settle)."""
    base = claude_base(paths)[0]
    return [base.joinpath(*parts) for parts in DATA_FOLDER_FILES]


# ---- Codex (PLAN §1.8, §8) ----

def codex_exe(paths) -> Path:
    """The Codex CLI binary. `paths.codex_exe` when it exists (synthetic paths); live, in this order: `codex`
    on PATH, ~/.local/bin/codex (launchd's PATH does not include it), the standalone package's current
    binary, then the CLI bundled in ChatGPT.app. A shell-script launcher (the standalone installer's
    ~/.local/bin/codex `exec`s the package binary) is resolved to the binary it starts, so the path matches
    the running Codex processes. Only a native (Mach-O) binary is ever returned."""
    if paths.codex_exe is not None and Path(paths.codex_exe).is_file():
        return Path(paths.codex_exe)
    if paths.mode == "live":
        home = Path(paths.home)
        candidates = [shutil.which("codex"), home.joinpath(*CODEX_LAUNCHER), home.joinpath(*CODEX_PACKAGE)]
        candidates += [Path(folder) / CODEX_APP / Path(*CODEX_IN_APP) for folder in config.mac_applications(home)]
        for candidate in candidates:
            found = _codex_binary(Path(candidate), home) if candidate else None
            if found is not None:
                return found
    raise DeckError(NOT_INSTALLED_MESSAGE, "not_installed")


def _codex_binary(path: Path, home: Path) -> Path | None:
    """`path` itself when it is a native binary, the binary it starts when it is a launcher script, else None."""
    if _is_native(path):
        return path.resolve()
    for target in _launcher_targets(path, home):
        if _is_native(target):
            return target.resolve()
    return None


def _is_native(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        with open(path, "rb") as handle:
            return handle.read(4) in NATIVE_MAGIC
    except OSError:
        return False


def _launcher_targets(script: Path, home: Path) -> list[Path]:
    """Absolute `…/codex` paths a `#!` launcher names ($HOME and ~ expanded; anything else with a variable
    is skipped), then the standalone package's current binary."""
    try:
        if not script.is_file() or script.stat().st_size > MAX_LAUNCHER_BYTES:
            return []
        text = script.read_bytes().decode("utf-8", "replace")
    except OSError:
        return []
    if not text.startswith("#!"):
        return []
    found = []
    for match in LAUNCHER_PATH.finditer(text):
        value = match.group()
        for prefix in ("${HOME}", "$HOME", "~"):
            if value.startswith(prefix + "/"):
                value = str(home) + value[len(prefix):]
                break
        target = Path(value)
        if target.is_absolute() and target != script:
            found.append(target)
    return found + [home.joinpath(*CODEX_PACKAGE)]


# ---- Antigravity's command-line tool, agy (providers/antigravity.py) ----

AGY_LAUNCHER = (".local", "bin", "agy")          # where agy's own installer puts it
AGY_HOMEBREW = (Path("/opt/homebrew/bin/agy"), Path("/usr/local/bin/agy"))


def agy_exe(paths) -> Path | None:
    """agy's executable, or None when it is not installed: `paths.agy_exe` in a synthetic home; live, in this
    order, ~/.local/bin/agy (a LaunchAgent's PATH lacks it), the Homebrew folders, then `agy` on PATH. Only an
    executable file counts. Cheap: file checks only, nothing is started (sensing asks on every tick)."""
    if paths.mode != "live":
        candidates = [paths.agy_exe]
    else:
        candidates = [Path(paths.home).joinpath(*AGY_LAUNCHER), *AGY_HOMEBREW, shutil.which("agy")]
    for candidate in candidates:
        if candidate and _is_program(Path(candidate)):
            return Path(candidate)
    return None


def file_identity(path) -> tuple | None:
    """What tells one installed file from the next (its resolved path, size and modification time), so a fact read
    from it (agy's version) is remembered until it is replaced. None when it cannot be read."""
    try:
        status = os.stat(path)
        return (os.path.realpath(path), status.st_size, status.st_mtime_ns, status.st_ino)
    except OSError:
        return None


def _is_program(path: Path) -> bool:
    try:
        return path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


# ---- files (§1.3 settle) and Claude's own preference ----

def file_is_free(path) -> bool:
    """Without asking about processes this module can only say "free" for a file that does not exist, or for
    a synthetic file of a test or the demo (no real process can hold one; the fake controller models holds
    itself). Anything else is "cannot tell", which counts as held: the real probe is the live controller's
    `file_is_free`."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    from ..procs import is_test_owned_path        # procs imports this module
    return config.safe_mode() and is_test_owned_path(path)


def files_released(paths, procs) -> bool:
    """The settle check (PLAN §1.3): no Claude desktop process is alive, and the controller's probe answers
    exactly True for every sign-in file. A failing or doubtful probe means "not released"."""
    try:
        if procs.claude_desktop():
            return False
        return all(procs.file_is_free(path) is True for path in data_folder_files(paths))
    except (DeckError, OSError):
        return False
