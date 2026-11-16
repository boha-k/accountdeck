"""Whether a newer AccountDeck release exists (PLAN §6b): a short request to GitHub every 5 minutes from an installed
copy and when the user looks at AccountDeck, the prompt it leads to, and the Terminal window that runs the update.

The request (`github_reply`) is one HEAD of github.com/<REPO>/releases/latest: no redirect followed, no body either
way, and nothing sent but this build's version in the User-Agent. GitHub answers it with a redirect to the latest
release's tag page, and the version is read from that address (`read_reply`). When the answer cannot be read (GitHub
changed it), the API is asked instead, at most once a day (`read_release`), so the one channel that delivers fixes
cannot die quietly.

The check runs on a thread of its own (`ad2-update`), never on the engine's tick: it cannot fail a tick phase, add
an alert row, delay a usage read or a switch, or raise into anything. It holds no lock while GitHub answers. Only an
installed copy (`install.json` with `manage` true) checks; safe mode, the demo and a source checkout never make the
request. The first check runs FIRST_CHECK after a start, then one every EVERY. A check that fails waits longer each
time (`pause`: 5, 10, 20, 40, then 60 minutes; a Retry-After on a 403 or 429 is honoured up to WAIT_MAX) until one
succeeds. The thread looks at the wall clock every minute, so a check that fell due while the Mac slept runs within a
minute of the wake. Opening the menu bar's menu or loading the dashboard (`look`) asks for a check when the last one
is over MANUAL_GAP old: in the background, so neither waits for GitHub; the state changes when the answer is in.

GitHub's answer is untrusted. Of it only the version is kept: the redirect must lead to exactly this repository's tag
page of "v" plus dotted numbers; the API's `tag_name` must be such a tag, the release neither a draft nor a
prerelease, and `html_url` that tag's release page. No text from GitHub is ever shown, and the version reaches a
command line only through `update_command`, which checks it again.

What survives a restart is one internal setting, `update_memory` (MEMORY_KEY): when the last check ran and succeeded
and whether it failed (and how many times in a row), the latest version seen, the version already notified, the
version skipped, when the API was last asked and until when GitHub asked to wait. Older builds ignore it. It lives in
memory and reaches the database only when something a user or a restart needs has changed, and otherwise at most once
an hour (`worth_saving`): a check that found nothing new writes nothing and logs nothing.

The state's `update` block (`view`) and what each status means:

    unavailable  this copy does not check (safe mode, the demo, a checkout): `reason` says why, the rest is empty
    off          the user switched `update_check` off: no request is ever made
    never        no check has finished yet
    current      the latest release is not newer than this version
    available    `latest` is newer, not skipped: the prompt shows (`blocked` says why Update… cannot start now,
                 also while an update this service started still runs in its Terminal window)
    skipped      `latest` is newer, but the user skipped that version; a later one is offered again
    failed       the last check could not tell (offline, GitHub refused, no release, a malformed answer)
"""
from __future__ import annotations

from collections import namedtuple
import math
from pathlib import Path
import re
import threading
import time

from . import __version__, config, diagnostics
from .core import DeckError

REPO = "boha-k/accountdeck"           # = REPO= in get.sh (tests/test_updates.py); a fork changes both
LATEST_URL = f"https://github.com/{REPO}/releases/latest"          # the check: a HEAD that GitHub redirects
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"   # the fallback, at most once a day
RELEASES_URL = f"https://github.com/{REPO}/releases/"
SETTING = "update_check"              # the user's switch (config.SETTINGS_SCHEMA)
MEMORY_KEY = "update_memory"          # the internal setting this module keeps

FIRST_CHECK = 30.0        # after a start the first check waits this long
EVERY = 300.0             # then one check every 5 minutes
BACKOFF_MAX = 3600.0      # checks that fail in a row wait 5, 10, 20, 40 minutes, then this long (`pause`)
WAIT_MAX = 6 * 3600.0     # the longest Retry-After honoured (GitHub's 403 or 429)
FALLBACK_EVERY = 86400.0  # the API is asked at most once a day, and only when the redirect cannot be read
SAVE_EVERY = 3600.0       # a check that changed nothing a user or a restart needs is saved at most once an hour
MANUAL_GAP = 60.0         # a check asked for (Check Now, a look) within a minute of the last one gets that one's answer
TIMEOUT = 10.0            # the request's socket timeout (providers.http)
CHECK_WAIT = 8.0          # check-update waits this long for the answer; the state then says `checking`
WAKE_EVERY = 60.0         # the thread looks at the wall clock this often: a sleeping Mac stops its timer
HOLD = 120.0              # no notification in the first two minutes after a start (as notify.HOLD)
UPDATE_LIMIT = 900.0      # an update window counts as running at most this long, if its end cannot be seen
MAX_VERSION = 32          # characters: a version is short, and int() refuses very long digit strings
MAX_LOCATION = 200        # characters of a redirect address read: a tag page's is short
MAX_FAILURES = 99         # failed checks in a row are counted up to this (only the first five lengthen the pause)
REDIRECTS = (301, 302, 303, 307, 308)
TAG_PATH = "releases/tag/v"           # after https://github.com/<REPO>/: where the redirect names the version
BASH = "/bin/bash"
STATUSES = ("unavailable", "off", "never", "current", "available", "skipped", "failed")
SIGNING_IN = ("signing_in", "waiting_exit")      # the terminal sign-in's phases while its window is open
# What a restart or a user needs: a change in one of these is saved at once, anything else at most hourly.
SAVE_AT_ONCE = ("latest", "skipped", "notified", "failed", "fallback_at", "wait_until")

_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}", re.ASCII)

# Copy. The dashboard repeats CONFIRM in its own words file (web/js/format.js).
DEMO = "The demo doesn't check for updates."
SAFE = "Safe mode is on, so AccountDeck doesn't check for updates."
NOT_INSTALLED = ("This copy runs from a source folder, so it doesn't check for updates. Update it with git and run "
                 "install.sh.")
UNREADABLE = "AccountDeck couldn't read its record of update checks."
OFF = "Checking for updates is off. Turn on Check for updates automatically in Settings first."
DEMO_UPDATE = "The demo can't update AccountDeck."
SAFE_UPDATE = "Safe mode is on, so AccountDeck won't update itself."
NOT_OFFERED = "That update is no longer offered. Check for updates again."
FINISH_FIRST = "Finish or cancel the current action first."
FINISH_SIGNIN = "Finish or cancel the sign-in first."
FINISH_ADD = "Wait until the Antigravity Add has ended."      # no e-mail: the page shows this text as it is
UPDATE_RUNNING = "An update is already running in a Terminal window."
NO_UPDATER = (f"This copy has no get.sh to update with. Install the update with the command at "
              f"https://github.com/{REPO}.")
CONFIRM = ("AccountDeck downloads {version} from GitHub, checks its SHA-256 and installs it in a Terminal window. "
           "Every saved account is kept, and only AccountDeck restarts: open Claude, Codex and agy sessions are "
           "not touched. If the new version doesn't start, the installer puts {current} back.")
STARTED = "Updating AccountDeck to {version} in a Terminal window. AccountDeck restarts by itself when it's done."
NOTIFY_TITLE = "AccountDeck {version} is available"
NOTIFY_TEXT = "You have {current}. Choose Update to {version}… in the menu bar, or click to open AccountDeck."


class BadAnswer(ValueError):
    """GitHub's answer is not a release AccountDeck offers: it changes nothing and offers nothing. From the check's
    request it means an answer this build cannot read, and the API is asked instead (`http` is its status)."""

    def __init__(self, what, http=None):
        super().__init__(what)
        self.http = http


class NoRelease(Exception):
    """GitHub shows no published release: none yet (a redirect to the release list), or the repository is private or
    missing (404). A failed check, as the API's 404 is; the API is not asked."""

    def __init__(self, http):
        super().__init__(f"no release (HTTP {http})")
        self.http = http


class Refused(Exception):
    """GitHub refused (403, 429) or failed (5xx): a failed check. `wait` is the Retry-After to honour, in seconds
    (at most WAIT_MAX), or None."""

    def __init__(self, http, wait=None):
        super().__init__(f"refused (HTTP {http})")
        self.http, self.wait = http, wait


# GitHub's answer to the check's HEAD: its status, its Location header and its Retry-After in seconds (or None).
Reply = namedtuple("Reply", "status location retry_after")


# ---- versions and GitHub's answer (pure) ----

def valid_version(text) -> bool:
    """Dotted numbers, two to four parts, ASCII digits only: 2.2.0, 2.10, 3.0.0.1."""
    return type(text) is str and len(text) <= MAX_VERSION and _VERSION.fullmatch(text) is not None


def parse_version(text) -> tuple[int, ...]:
    if not valid_version(text):
        raise ValueError("not a version")
    return tuple(int(part) for part in text.split("."))


def is_newer(candidate, current) -> bool:
    """Whether `candidate` is a later release than `current`, as dotted numbers (2.10.0 > 2.9.0; 2.2 = 2.2.0).
    False when either is not a version."""
    try:
        a, b = parse_version(candidate), parse_version(current)
    except ValueError:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def release_page(version) -> str:
    """The release page of `version` (its tag is "v" + version, as get.sh downloads it)."""
    return f"{RELEASES_URL}tag/v{version}"


def read_reply(reply) -> str:
    """The version GitHub's answer to the check names. Only a redirect to exactly this repository's tag page of a plain
    version names one: https://github.com/<REPO>/releases/tag/v<version>, owner and name in any case (GitHub matches
    them so), ASCII only, nothing after the version. The address itself is never kept (release_page is). Raises
    NoRelease for the release list (none published yet) or a 404, Refused for a 403, 429 or 5xx, and BadAnswer for
    anything else: an answer this build cannot read."""
    status, location = reply.status, reply.location
    if status == 404:
        raise NoRelease(404)
    if status in (403, 429):
        wait = reply.retry_after
        usable = type(wait) in (int, float) and math.isfinite(wait) and wait > 0
        raise Refused(status, min(float(wait), WAIT_MAX) if usable else None)
    if type(status) is int and 500 <= status <= 599:
        raise Refused(status)
    if status in REDIRECTS and type(location) is str and len(location) <= MAX_LOCATION and location.isascii():
        home = f"https://github.com/{REPO}/"
        if location[:len(home)].lower() == home.lower():
            rest = location[len(home):]
            if rest.startswith(TAG_PATH) and valid_version(rest[len(TAG_PATH):]):
                return rest[len(TAG_PATH):]
            if rest in ("releases", "releases/"):
                raise NoRelease(status)
    raise BadAnswer("not a redirect to a release", http=status if type(status) is int else None)


def read_release(data) -> str:
    """The version the API's /releases/latest answer offers, or BadAnswer. Nothing else is taken from it."""
    if not isinstance(data, dict):
        raise BadAnswer("not an object")
    if data.get("draft") is not False or data.get("prerelease") is not False:
        raise BadAnswer("a draft or a prerelease")
    tag = data.get("tag_name")
    if type(tag) is not str or not tag.startswith("v") or not valid_version(tag[1:]):
        raise BadAnswer("tag_name")
    page = data.get("html_url")
    # GitHub matches owner and repository names in any case: compared so, never kept (release_page is).
    if type(page) is not str or len(page) > 200 or page.lower() != release_page(tag[1:]).lower():
        raise BadAnswer("html_url")
    return tag[1:]


def sample_answer(version) -> dict:
    """What the API answers for a published release `version`, cut to the fields read here (tests)."""
    return {"tag_name": f"v{version}", "html_url": release_page(version), "draft": False, "prerelease": False,
            "name": f"AccountDeck {version}"}


def stand_in(version):
    """A stand-in for GitHub where `version` is the latest release (the demo, tests): `fetch(url)` as Updater takes
    it, answering the check with the redirect and the API with its answer. It never reaches the network."""
    def fetch(url):
        if url == LATEST_URL:
            return Reply(302, release_page(version), None)
        if url == API_URL:
            return sample_answer(version)
        raise ValueError("The update check asks only LATEST_URL and API_URL.")
    return fetch


def github_reply(http=None) -> Reply:
    """GitHub's answer to the check: one HEAD of LATEST_URL, no redirect followed, no body either way, nothing sent
    but this build's user agent. Raises what providers.http.head raises (SafetyViolation in safe mode before any
    socket opens; a ProviderError when nothing answered: no network, a timeout)."""
    from .providers.errors import header, retry_after_seconds    # on use: the providers package is heavy
    if http is None:
        from .providers import http as http_module
        http = http_module.head
    status, headers = http(LATEST_URL, timeout=TIMEOUT)
    return Reply(status, header(headers, "location"), retry_after_seconds(header(headers, "retry-after")))


def github_answer(http=None):
    """The API's answer about the latest release (parsed JSON), the fallback: one anonymous GET with this build's user
    agent and nothing else about the Mac or its user. Raises what providers.http.request raises (SafetyViolation in
    safe mode before any socket opens; a classified ProviderError for 404, 403, 429, 5xx, a timeout or no network)."""
    if http is None:
        from .providers import http as http_module      # on use: the providers package is heavy
        http = http_module.request
    data, _meta = http("GET", API_URL, timeout=TIMEOUT,
                       headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    return data


def update_command(script, version) -> list[str]:
    """The update window's command: bash running the installed get.sh for `version`. The version is checked again
    here, so nothing but dotted numbers ever reaches the command line."""
    if not valid_version(version):
        raise DeckError("That is not a version AccountDeck can update to.", "invalid")
    script = Path(script)
    if not script.is_absolute() or script.name != "get.sh":
        raise DeckError("That is not AccountDeck's updater.", "invalid")
    return [BASH, str(script), "--version", version]


# ---- memory and the rules on it (pure) ----

def load_memory(value) -> dict:
    """The persisted memory with anything malformed dropped: the database is not trusted with a command line. 2.0.0
    and 2.1.0 wrote none; a memory without `failures`, `wait_until` and `fallback_at` (an earlier build) loads with
    one failure in a row if its last check failed, and no wait or fallback."""
    value = value if isinstance(value, dict) else {}

    def when(key):
        found = value.get(key)
        return float(found) if type(found) in (int, float) and math.isfinite(found) and found > 0 else None

    def version(key):
        found = value.get(key)
        return found if valid_version(found) else None
    failed = value.get("failed") is True
    failures = value.get("failures")
    if type(failures) is not int or not 0 <= failures <= MAX_FAILURES:
        failures = 1 if failed else 0
    return {"checked_at": when("checked_at"), "tried_at": when("tried_at"), "failed": failed,
            "latest": version("latest"), "notified": version("notified"), "skipped": version("skipped"),
            "failures": failures, "wait_until": when("wait_until"), "fallback_at": when("fallback_at")}


def pause(failures) -> float:
    """The wait after a check, by the number of checks in a row that found no version: EVERY while they find one,
    then 5, 10, 20 and 40 minutes, then BACKOFF_MAX."""
    if failures <= 0:
        return EVERY
    return min(EVERY * 2 ** min(failures - 1, 8), BACKOFF_MAX)


def asked_to_wait(memory, now) -> float | None:
    """Until when GitHub asked this copy to wait (a Retry-After), or None once that has passed. A wait ending more
    than WAIT_MAX from now (the clock went back) counts as none."""
    until = memory["wait_until"]
    return until if until is not None and now < until <= now + WAIT_MAX else None


def next_due(memory, started, now) -> float:
    """When the next automatic check is due (wall clock): FIRST_CHECK after the start, `pause` after the last attempt,
    and not before GitHub's Retry-After has passed. An attempt dated in the future (the clock went back) counts as
    none."""
    due = started + FIRST_CHECK
    tried = memory["tried_at"]
    if tried is not None and tried <= now:
        due = max(due, tried + pause(memory["failures"]))
    return max(due, asked_to_wait(memory, now) or due)


def may_ask(memory, now) -> bool:
    """Whether a check asked for (Check Now, a look) goes to GitHub now: the last one is over MANUAL_GAP old (or dated
    in the future) and GitHub has not asked to wait. A failed check's pause does not hold it back."""
    tried = memory["tried_at"]
    return (tried is None or not 0 <= now - tried < MANUAL_GAP) and asked_to_wait(memory, now) is None


def fallback_due(memory, now) -> bool:
    """Whether the API may be asked: not in the FALLBACK_EVERY before `now` (one dated in the future counts as none)."""
    asked = memory["fallback_at"]
    return asked is None or not 0 <= now - asked < FALLBACK_EVERY


def worth_saving(saved, memory, now) -> bool:
    """Whether `memory` is written over `saved` (what the database holds): at once when something a user or a restart
    needs has changed (SAVE_AT_ONCE: a new latest version, a skip, a notification, a change between working and
    failing, the API's time, GitHub's wait), else when the last saved check is SAVE_EVERY old. So a check that found
    nothing new is kept in memory only."""
    if memory == saved:
        return False
    if any(memory[key] != saved[key] for key in SAVE_AT_ONCE):
        return True
    last = saved["tried_at"]
    return last is None or not 0 <= now - last < SAVE_EVERY


def view(memory, *, current, enabled, reason, checking=False, blocked=None) -> dict:
    """The state's `update` block (the module docstring lists the statuses). `reason`: why this copy does not check,
    or None; `blocked`: why Update… cannot start now (kept only while an update is available)."""
    if reason is not None:
        return {"enabled": enabled, "current": current, "latest": None, "available": False, "url": None,
                "checked_at": None, "skipped": None, "status": "unavailable", "checking": False, "failed_at": None,
                "reason": reason, "blocked": None}
    latest, skipped = memory["latest"], memory["skipped"]
    newer = latest is not None and is_newer(latest, current)
    available = enabled and newer and latest != skipped
    if not enabled:
        status = "off"
    elif available:
        status = "available"
    elif newer:
        status = "skipped"
    elif memory["failed"]:
        status = "failed"
    elif memory["checked_at"] is None:
        status = "never"
    else:
        status = "current"
    return {"enabled": enabled, "current": current, "latest": latest, "available": available,
            "url": release_page(latest) if latest else None, "checked_at": memory["checked_at"], "skipped": skipped,
            "status": status, "checking": bool(checking), "failed_at": memory["tried_at"] if memory["failed"] else None,
            "reason": None, "blocked": blocked if available else None}


def announcement(memory, block, settings, hour):
    """(version, show) of the one notification an offered version gets, or None. Never twice for one version and
    never for a skipped one (`block` is not then available). During quiet hours it waits for their end. With
    notifications off it only counts as told (show False): the dashboard and the menu still offer it."""
    from .notify import in_quiet_hours       # on use: notify imports contract, which imports this module
    if not block["available"] or memory["notified"] == block["latest"]:
        return None
    settings = settings or {}
    if not (settings.get("alerts_enabled", True) and settings.get("notifications", True)):
        return block["latest"], False
    if in_quiet_hours(settings.get("quiet_hours"), hour):
        return None
    return block["latest"], True


def start_refusal(*, mode, safe, installed, block, version, busy, script_ok):
    """(message, code) of why start-update cannot go ahead, or None."""
    if mode != "live":
        return DEMO_UPDATE, "unsupported"
    if safe:
        return SAFE_UPDATE, "safety"
    if not installed:
        return NOT_INSTALLED, "unsupported"
    if not block["available"] or version != block["latest"]:
        return NOT_OFFERED, "invalid"
    if busy:
        return busy, "busy"
    if not script_ok:
        return NO_UPDATER, "not_installed"
    return None


def installed(root) -> bool:
    """Whether this copy, run from `root` (the folder holding run.py), is an installed one: never in safe mode."""
    return not config.safe_mode() and manages(root)


def manages(root) -> bool:
    """Whether `root` holds the record install.sh writes for a real install: its install.json reads and says
    `manage` true. A checkout has none; a `--prefix` test install says false and never updates itself."""
    from .platform import autostart_mac       # on use: the platform package is macOS-only
    try:
        return autostart_mac.Layout.installed(Path(root)).manage is True
    except DeckError:
        return False


def unavailable(*, mode, safe, installed, stand_in) -> str | None:
    """Why this copy does not check for updates, or None. A stand-in for GitHub (tests, the demo) never reaches the
    network, so it may check anywhere; GitHub itself is asked only by an installed copy outside safe mode."""
    if stand_in:
        return None
    if mode != "live":
        return DEMO
    if safe:
        return SAFE
    return None if installed else NOT_INSTALLED


def confirm_text(version, current) -> str:
    return CONFIRM.format(version=version, current=current)


def check_result(block) -> str:
    """What a manual check found, in words (the menu bar's answer to Check for Updates…)."""
    status = block.get("status")
    if block.get("checking"):
        return "AccountDeck is still checking for updates. Look at the menu again in a minute."
    if status == "available":
        return f"AccountDeck {block['latest']} is available. You have {block['current']}."
    if status == "failed":
        return "AccountDeck couldn't check for updates. Check the internet connection and try again later."
    if status == "skipped":
        return f"You skipped AccountDeck {block['latest']}. A later version will be offered when it's out."
    if status in ("unavailable", "off"):
        return block.get("reason") or OFF
    return f"AccountDeck {block['current']} is the newest version."


# ---- the check, its thread and the actions ----

class Updater:
    """The update check of one engine. `fetch(url)` stands in for GitHub (tests, the demo; `stand_in` makes one):
    for LATEST_URL it returns a Reply, for API_URL the API's parsed answer. Without it the check asks GitHub, and
    only from an installed copy outside safe mode and the demo. `app_root` is the folder holding run.py and, in an
    installed copy, get.sh."""

    def __init__(self, engine, *, fetch=None, app_root=None):
        self.engine, self.fetch = engine, fetch
        self.clock = engine.clock
        self.app_root = Path(app_root) if app_root is not None else config.REPO_ROOT
        self.current = __version__
        self.installed = installed(self.app_root)
        self.reason = unavailable(mode=engine.mode, safe=config.safe_mode(), installed=self.installed,
                                  stand_in=fetch is not None)
        # The memory as it stands, replaced whole and never changed in place, so any thread reads it without a lock;
        # `_saved` is what the database holds. A failing save leaves the memory ahead, and the next save catches up.
        self._memory = self._saved = load_memory(engine.prefs.get(MEMORY_KEY))
        self._memory_lock = threading.Lock()     # the memory's read-modify-write; never held while GitHub answers
        self._flags = threading.Lock()           # the fields below
        self._checking = self._asked = False
        self._done = threading.Event()
        self._done.set()
        self._wake, self._stopping = threading.Event(), threading.Event()
        self._thread = None
        self._started, self._started_real = self.clock(), time.monotonic()
        self._starting = threading.Lock()        # one start-update at a time: its refusal and its window are one step
        self._window = None                      # (record, when) of the update window this service opened (_flags)
        self._logged = set()
        self._failures_logged = set()            # the kinds of failure logged since the last check that succeeded

    # -- lifecycle (Engine.start / stop / update_settings) --
    def start(self) -> None:
        """Starts `ad2-update` (once), only where this copy checks."""
        if self.reason is None and self._thread is None and not self._stopping.is_set():
            self._thread = threading.Thread(target=self._run, name="ad2-update", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Ends the thread at its next look. A request under way finishes by its own timeout and saves nothing."""
        self._stopping.set()
        self._wake.set()
        self._done.set()

    def wake(self) -> None:
        """The thread looks again now (the setting changed)."""
        self._wake.set()

    def _run(self) -> None:
        while True:
            self._wake.wait(self._sleep())
            self._wake.clear()
            if self._stopping.is_set():
                return
            try:
                self.run_due()
            except Exception as exc:      # the thread lives on: the next look tries again
                self._log_once("update.loop_failed", exc)

    def _sleep(self) -> float:
        try:
            if not self.engine.prefs.get(SETTING):
                return WAKE_EVERY
            now = self.clock()
            return max(1.0, min(WAKE_EVERY, next_due(self._memory, self._started, now) - now))
        except Exception:
            return WAKE_EVERY

    def run_due(self) -> None:
        """Checks when a check is due or was asked for (the thread; tests call it directly)."""
        with self._flags:
            asked, self._asked = self._asked, False
        if self.reason is None and self.engine.prefs.get(SETTING) and not self._stopping.is_set():
            memory, now = self._memory, self.clock()
            if (asked and asked_to_wait(memory, now) is None) or now >= next_due(memory, self._started, now):
                self._check()
                return
        if asked:
            self._done.set()

    def look(self) -> None:
        """The user looked at AccountDeck (the menu bar's menu opened, a dashboard page loaded): the thread checks now
        when the last check is over MANUAL_GAP old and GitHub has not asked to wait. It returns at once and never
        raises; the state changes when the answer is in."""
        try:
            if self.reason is not None or self._thread is None or self._stopping.is_set():
                return
            if not self.engine.prefs.get(SETTING) or not may_ask(self._memory, self.clock()):
                return
            with self._flags:
                if self._checking:
                    return
                self._asked = True
            self._wake.set()
        except Exception as exc:
            self._log_once("update.look_failed", exc)

    def _check(self) -> None:
        """One check and what it found, kept. Never raises."""
        with self._flags:
            if self._checking:
                return
            self._checking, self._asked = True, False       # this check answers whoever asked
            self._done.clear()
        self._bump()
        try:
            version, error, api = self._ask(self._memory)
            if not self._stopping.is_set():
                self._finish(version, error, api)
        except Exception as exc:
            self._log_once("update.save_failed", exc)
        finally:
            with self._flags:
                self._checking = False
            self._done.set()
            self._bump()

    def _ask(self, memory):
        """(version, error, api) of one check: the version GitHub's redirect names, or why there is none. The API is
        asked (api True) only when the redirect cannot be read and the fallback is due; an unreadable redirect while
        it is not due ends with a BadAnswer and api False, which tells nothing either way."""
        try:
            return read_reply(self._github(LATEST_URL)), None, False
        except BadAnswer as exc:          # GitHub answered in a way this build cannot read: worth one line a run
            if ("update.unreadable", exc.http) not in self._logged:
                self._logged.add(("update.unreadable", exc.http))
                diagnostics.log("update.unreadable", "warning", http=exc.http)
            if not fallback_due(memory, self.clock()):
                return None, exc, False
        except Exception as exc:          # nothing answered, no release, refused, a broken stand-in
            return None, exc, False
        try:
            return read_release(self._github(API_URL)), None, True
        except Exception as exc:
            return None, exc, True

    def _github(self, url):
        if self.fetch is not None:
            return self.fetch(url)
        return github_reply() if url == LATEST_URL else github_answer()

    def _finish(self, version, error, api) -> None:
        """Keeps what one check found. The last good answer stays whatever went wrong; a failure lengthens the pause
        before the next check (and GitHub's Retry-After sets its earliest time), a success resets both."""
        now = self.clock()
        with self._memory_lock:
            before = self._memory
            memory = {**before, "tried_at": now}
            if api:
                memory["fallback_at"] = now
            if version is not None:
                memory.update(checked_at=now, failed=False, failures=0, wait_until=None, latest=version)
            else:
                told_nothing = isinstance(error, BadAnswer) and not api
                wait = error.wait if isinstance(error, Refused) else None
                memory.update(failures=min(before["failures"] + 1, MAX_FAILURES),
                              failed=before["failed"] if told_nothing else True,
                              wait_until=now + wait if wait else None)
            self._store(memory)
        if version is not None:
            if version != before["latest"] or before["failed"]:
                diagnostics.log("update.checked", latest=version, newer=is_newer(version, self.current), api=api)
            self._failures_logged.clear()
            return
        kind = (type(error).__name__, getattr(error, "kind", None), getattr(error, "http", None), api)
        if kind not in self._failures_logged:          # one line per kind of failure until a check succeeds
            self._failures_logged.add(kind)
            diagnostics.log("update.check_failed", "info", error_type=kind[0], kind=kind[1], http=kind[2], api=api)

    # -- memory --
    def memory(self) -> dict:
        """The memory as it stands (a copy): what the database holds, with what this run found since."""
        return dict(self._memory)

    def _store(self, memory) -> None:
        """Makes `memory` the memory, and saves it when worth_saving says so. Called with _memory_lock held."""
        self._memory = memory
        if worth_saving(self._saved, memory, self.clock()):
            self.engine.prefs.set_internal(MEMORY_KEY, memory)
            self._saved = memory

    # -- the state --
    def view(self) -> dict:
        """The state's `update` block. Never raises: a snapshot must not fail because of it."""
        try:
            with self._flags:
                checking = self._checking
            block = view(self._memory, current=self.current, enabled=bool(self.engine.prefs.get(SETTING)),
                         reason=self.reason, checking=checking)
            if block["available"]:
                refusal = self._refusal(block, block["latest"])
                block["blocked"] = refusal[0] if refusal else None
            return block
        except Exception as exc:
            self._log_once("update.view_failed", exc)
            return view(load_memory(None), current=self.current, enabled=False, reason=UNREADABLE)

    def _busy(self):
        if self._updating():
            return UPDATE_RUNNING
        engine = self.engine
        if engine.pending:
            return FINISH_FIRST
        phase = (getattr(getattr(engine, "cli", None), "phase", None) or {}).get("phase")
        if engine.login or phase in SIGNING_IN:
            return FINISH_SIGNIN
        agy = getattr(engine, "agy", None)
        if agy is not None and agy.enrolling:       # an Antigravity Add: agy's sign-in window, or the Add's end
            return FINISH_SIGNIN if agy.restoring_refusal() is None else FINISH_ADD
        return None

    def _updating(self) -> bool:
        """Whether the update window this service opened still runs its command. Its record is what the window
        opener returned (procs_mac.open_terminal_command); whether that command has ended is asked the way the
        sign-in window's is. Until that can be seen, the window counts as running, for UPDATE_LIMIT at most: then a
        lost or unreadable record can no longer hold Update… back. An update that succeeds restarts this service,
        and a new run starts with no window."""
        with self._flags:
            window = self._window
        if window is None:
            return False
        record, opened = window
        elapsed = self.clock() - opened
        try:
            ended = not 0 <= elapsed < UPDATE_LIMIT or bool(self.engine.procs.cli_enrollment_exited(record))
        except Exception as exc:          # cannot tell: it runs until UPDATE_LIMIT
            self._log_once("update.window_unknown", exc)
            ended = False
        if ended:
            with self._flags:
                if self._window is window:
                    self._window = None
            diagnostics.log("update.window_ended", after_s=round(max(0.0, elapsed)))
        return not ended

    def _script(self) -> Path:
        return self.app_root / "get.sh"

    def _refusal(self, block, version):
        script = self._script()
        return start_refusal(mode=self.engine.mode, safe=config.safe_mode(), installed=self.installed, block=block,
                             version=version, busy=self._busy(),
                             script_ok=script.is_file() and not script.is_symlink())

    # -- the actions (Engine._do_check_update, _do_skip_update, _do_start_update) --
    def check_now(self, wait=None) -> dict:
        """check-update: a check now (one within the last minute answers instead, and so does the last one while
        GitHub asked to wait), up to `wait` seconds for its answer. Asking also takes back a skip: whoever asks wants
        to hear about every newer version."""
        if self.reason is not None:
            raise DeckError(self.reason, "unsupported")
        if not self.engine.prefs.get(SETTING):
            raise DeckError(OFF, "invalid")
        with self._memory_lock:
            if self._memory["skipped"] is not None:
                self._store({**self._memory, "skipped": None})
        threaded = self._thread is not None and self._thread.is_alive()
        with self._flags:                 # one decision: a check that starts after it answers this ask too
            running = self._checking
            asked = running or may_ask(self._memory, self.clock())
            if asked and not running and threaded:
                self._asked = True
                self._done.clear()
        if asked and not running:
            if threaded:
                self._wake.set()
            else:
                self._check()             # no thread (tests, an engine never started): here and now
        if asked:
            self._done.wait(CHECK_WAIT if wait is None else wait)
        return {"checked": asked, "update": self.view()}

    def skip(self, version) -> dict:
        """skip-update: the offered `version` is not offered again; a later one is."""
        if self.reason is not None:
            raise DeckError(self.reason, "unsupported")
        with self._memory_lock:
            memory = self.memory()
            if not valid_version(version) or version != memory["latest"] or not is_newer(version, self.current):
                raise DeckError(NOT_OFFERED, "invalid")
            memory["skipped"] = version
            self._store(memory)
        diagnostics.log("update.skipped", version=version)
        return {"update": self.view()}

    def start_update(self, version) -> dict:
        """start-update: a Terminal window runs the installed get.sh for the offered `version`, which the user
        confirmed. It returns once the window is asked for; the installer then stops and restarts this service by
        itself (install.sh), refusing while a switch runs, and puts the previous version back when the new one fails
        its health check. Nothing is left half-done here: a window that could not open leaves no script behind.
        While that window's command runs, a second start-update is refused (UPDATE_RUNNING), so a second click in
        the dashboard or the menu opens no second window."""
        with self._starting:
            block = view(self._memory, current=self.current, enabled=bool(self.engine.prefs.get(SETTING)),
                         reason=self.reason)
            refusal = self._refusal(block, version)
            if refusal is not None:
                raise DeckError(*refusal)
            opener = getattr(self.engine.procs, "open_update_terminal", None)
            if opener is None:
                raise DeckError(SAFE_UPDATE, "unsupported")
            record = opener(self._script(), block["latest"])
            with self._flags:
                self._window = (record, self.clock())
        diagnostics.log("update.started", version=block["latest"], current=self.current)
        self.engine.notice(STARTED.format(version=block["latest"]), "info", sticky=False)
        return {"started": block["latest"], "update": self.view()}

    # -- the one notification (the menu bar's poll) --
    def announce(self, show, *, accepting=lambda: True) -> None:
        """Shows the one notification a newly offered version gets through `show(title, text, key)` and remembers
        it. Called by the menu bar, which owns the notification sink, on every poll. Never raises."""
        try:
            if self.reason is not None or time.monotonic() - self._started_real < HOLD:
                return
            block = self.view()
            if not block["available"]:
                return
            due = announcement(self._memory, block, self.engine.prefs.public(),
                               time.localtime(self.clock()).tm_hour)
            if due is None:
                return
            version, shown = due
            if shown:
                if not accepting():
                    return                # macOS has not allowed notifications yet: the next poll tries again
                show(NOTIFY_TITLE.format(version=version), NOTIFY_TEXT.format(version=version, current=self.current),
                     f"update|{version}")
            with self._memory_lock:
                self._store({**self._memory, "notified": version})
            diagnostics.log("update.notified", version=version, shown=shown)
        except Exception as exc:
            self._log_once("update.announce_failed", exc)

    # -- helpers --
    def _bump(self) -> None:
        try:
            self.engine.bump()
        except Exception as exc:
            self._log_once("update.bump_failed", exc)

    def _log_once(self, event, exc) -> None:
        if (event, type(exc).__name__) not in self._logged:
            self._logged.add((event, type(exc).__name__))
            diagnostics.log(event, "warning", error_type=type(exc).__name__)
