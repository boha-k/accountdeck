"""AccountDeck 2 - a local dashboard for Claude and Codex account usage and switching."""
import datetime as dt
import json
from pathlib import Path

# Set by hand. install.sh reads this line.
__version__ = "2.2.0"


def _read_build():
    """What install.sh stamped in `build.json` beside this package: {"commit", "dirty", "installed_at"}, or None
    in a checkout. Read once: after a later install it still names the build this process started from."""
    try:
        value = json.loads((Path(__file__).resolve().parent.parent / "build.json").read_text(encoding="utf-8"))
        commit, dirty, at = value.get("commit"), value.get("dirty"), value.get("installed_at")
    except (OSError, ValueError, AttributeError):
        return None
    return {"commit": commit if isinstance(commit, str) else None, "dirty": dirty if isinstance(dirty, bool) else None,
            "installed_at": at if type(at) in (int, float) else None}


BUILD = _read_build()


def build_text(build):
    """A build in words: "c58fa73 with uncommitted changes, installed 2026-11-02 18:40"."""
    if not isinstance(build, dict):
        return "no build stamp"
    words = build.get("commit") or "unknown commit"
    if build.get("dirty"):
        words += " with uncommitted changes"
    if type(build.get("installed_at")) in (int, float):
        words += f", installed {dt.datetime.fromtimestamp(build['installed_at']):%Y-%m-%d %H:%M}"
    return words
