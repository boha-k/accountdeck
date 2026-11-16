"""The update check (accountdeck/updates.py) can never harm the service that manages the logins, and it hears of a
release within minutes at almost no cost: GitHub's answer is untrusted and names a version only when its redirect
leads to this repository's tag page of a plain version (the API, asked only when that redirect cannot be read, at
most once a day, only for a published release); checks that fail wait longer each time and one success resets that;
a check that found nothing new writes and logs nothing; a failing or hanging check never touches the tick, and a look
at the app never waits for it; safe mode and a source checkout never reach the network; one version is announced
once; and the update window's command is built only from a version checked twice. GitHub is never contacted here:
a stand-in answers.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import threading
import time
from unittest import mock

import tests.helpers as helpers  # (safe mode before any accountdeck module loads)

from accountdeck import __version__, procs, updates
from accountdeck.config import SafetyViolation
from accountdeck.core import DeckError
from accountdeck.providers.errors import ProviderError

REPO_ROOT = Path(__file__).resolve().parent.parent
NEXT = f"{updates.parse_version(__version__)[0]}.{updates.parse_version(__version__)[1] + 1}.0"
TAG = updates.release_page(NEXT)                     # where GitHub's redirect leads while NEXT is the latest release
HOME = f"https://github.com/{updates.REPO}/"
MINUTE = 60.0


def redirect(location, status=302, retry_after=None):
    return updates.Reply(status, location, retry_after)


class GitHub:
    """A stand-in for GitHub: `replies` answer the check (a HEAD of LATEST_URL) and `answers` the API, each in turn,
    the last one repeating; an exception is raised. It counts the requests of each kind."""

    def __init__(self, replies=(), answers=()):
        self.replies, self.answers = list(replies), list(answers)
        self.heads = self.gets = 0

    def __call__(self, url):
        if url == updates.LATEST_URL:
            self.heads += 1
            answer = self.replies[min(self.heads, len(self.replies)) - 1]
        else:
            assert url == updates.API_URL, url
            self.gets += 1
            answer = self.answers[min(self.gets, len(self.answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


class UpdateCheckTests(helpers.SyntheticTestCase):
    def engine(self, fetch):
        """An engine whose update check starts from an empty memory, past its first wait, on a clock the test moves."""
        engine = self.make_engine(update_fetch=fetch)
        engine.prefs.set_internal(updates.MEMORY_KEY, {})  # a second engine in one test shares the data folder
        engine.updates = checker = updates.Updater(engine, fetch=fetch)
        checker.clock = self.clock = Clock()
        checker._started = self.clock.now - updates.FIRST_CHECK
        checker._started_real -= updates.HOLD              # as if the service had run for a while
        return engine

    def checked(self, engine):
        """The next scheduled check: the clock moves on to when it is due, and it runs as the `ad2-update` thread runs
        it."""
        checker = engine.updates
        self.clock.now = max(self.clock.now, updates.next_due(checker.memory(), checker._started, self.clock.now))
        checker.run_due()
        return engine.snapshot()["update"]

    def test_versions_compare_as_dotted_numbers(self):
        self.assertTrue(updates.is_newer("2.10.0", "2.9.0"))
        self.assertTrue(updates.is_newer("3.0", "2.99.99"))
        for older_or_same in ("2.1.9", "2.2.0", "2.2", "1.99.99.99"):
            self.assertFalse(updates.is_newer(older_or_same, "2.2.0"), older_or_same)
        for bad in ("v2.3.0", "2.3.0\n", "2.3.0 ", "2", "2..3", ".2.3", "2.3.", "2.3.0.1.5", "٢.٣.٠", "2.3.0;id",
                    "9" * 40 + ".0", None, 2.3, b"2.3.0"):
            self.assertFalse(updates.valid_version(bad), repr(bad))
            self.assertFalse(updates.is_newer(bad, "1.0.0"), repr(bad))

    def test_only_a_redirect_to_this_repositorys_tag_page_names_a_version(self):
        self.assertEqual(updates.read_reply(redirect(TAG)), NEXT)
        for status in (301, 303, 307, 308):
            self.assertEqual(updates.read_reply(redirect(TAG, status)), NEXT)
        self.assertEqual(updates.read_reply(redirect(TAG.replace(updates.REPO, updates.REPO.upper()))), NEXT)
        unreadable = [
            TAG.replace(updates.REPO, "someone/else"), TAG.replace(updates.REPO, updates.REPO + "-fork"),
            TAG.replace("https://github.com", "https://evil.example"),
            TAG.replace("https://github.com", "https://github.com.evil.example"), TAG.replace("https:", "http:"),
            TAG.replace("https://github.com", ""),                                   # an address without its host
            TAG + "/assets", TAG + "?x=1", TAG + "#x", TAG + " ", TAG + "\n",        # a path with extra parts
            HOME + "releases/tag/" + NEXT, HOME + "releases/tag/v" + NEXT + ".1.5",
            HOME + "releases/tag/v" + "9" * 40 + ".0", HOME + "releases/tag/v" + "9" * 400,   # over-long versions
            HOME + "releases/tag/vlatest", HOME + "releases/tag/v$(id)", HOME + "releases/tag/v٢.٣.٠",
            HOME + "releases/tag/v" + NEXT.replace(".", "%2E"), HOME + "releases/download/v" + NEXT, HOME + "tags",
            HOME, "", None, 42,
        ]
        kelvin = TAG.replace("k", "K")          # lowercases to "k": GitHub's addresses are plain ASCII
        unreadable += [kelvin] if kelvin != TAG else []
        for location in unreadable:
            with self.assertRaises(updates.BadAnswer, msg=repr(location)):
                updates.read_reply(redirect(location))
        for status in (200, 204, 300, 304, 400, 401, 410, 451, None, "302", True):
            with self.assertRaises(updates.BadAnswer, msg=repr(status)):
                updates.read_reply(redirect(TAG, status))
        for no_release in (redirect(HOME + "releases"), redirect(HOME + "releases/"), redirect(None, 404)):
            with self.assertRaises(updates.NoRelease, msg=repr(no_release)):       # none yet; private or missing
                updates.read_reply(no_release)
        for reply, wait in ((redirect(None, 429, 120.0), 120.0), (redirect(None, 403, 1e9), updates.WAIT_MAX),
                            (redirect(None, 429), None), (redirect(None, 403, -5.0), None), (redirect(TAG, 503), None)):
            with self.assertRaises(updates.Refused, msg=repr(reply)) as refused:
                updates.read_reply(reply)
            self.assertEqual(refused.exception.wait, wait)
        # The API's answer, the fallback's: only a published release with a plain version and its own page.
        good = updates.sample_answer(NEXT)
        self.assertEqual(updates.read_release(good), NEXT)
        for answer in (None, [], "v9.0.0", {}, {**good, "draft": True}, {**good, "prerelease": True},
                       {**good, "draft": None},
                       {**good, "tag_name": "9.0.0", "html_url": updates.RELEASES_URL + "tag/9.0.0"},
                       {**good, "tag_name": f"v{NEXT}\n"}, {**good, "tag_name": f"v{NEXT};touch /tmp/x"},
                       {**good, "tag_name": "v$(id)"}, {**good, "tag_name": "v" + "9" * 5000 + ".0"},
                       {**good, "tag_name": ["v9.0.0"]}, {**good, "html_url": TAG.replace("github.com", "evil.example")},
                       {**good, "html_url": TAG.replace(updates.REPO, "someone/else")},
                       {**good, "html_url": TAG + "?x=1"}, {**good, "html_url": updates.release_page("99.0.0")},
                       {**good, "html_url": None}):
            with self.assertRaises(updates.BadAnswer, msg=repr(answer)[:80]):
                updates.read_release(answer)

    def test_an_untrusted_answer_offers_nothing_and_the_api_is_asked_at_most_once_a_day(self):
        failing = [redirect(HOME + "releases"), redirect(None, 404), redirect(None, 403), redirect(None, 429),
                   redirect(None, 503), ProviderError("network", detail="timeout"), RuntimeError("anything at all")]
        github = GitHub(failing)
        engine = self.engine(github)
        for reply in failing:             # a check that fails: nothing offered, and no reason to ask the API
            update = self.checked(engine)
            self.assertEqual((update["status"], update["latest"], update["available"]), ("failed", None, False),
                             repr(reply))
        self.assertEqual((github.heads, github.gets), (len(failing), 0))
        # A redirect this build cannot read (GitHub changed it): the API is asked instead, at most once a day.
        good = updates.sample_answer(NEXT)
        hostile = [None, {}, {**good, "prerelease": True}, {**good, "tag_name": f"v{NEXT};id"},
                   {**good, "html_url": updates.release_page("99.0.0")}, ProviderError("network", detail="oversize"),
                   ProviderError("network", http=404)]
        github.replies, github.answers = [redirect(TAG.replace(updates.REPO, "someone/else"))], [*hostile, good]
        for answer in hostile:
            for _ in range(3):            # the first asks the API; the rest of that day does not
                update = self.checked(engine)
                self.assertEqual((update["status"], update["latest"]), ("failed", None), repr(answer)[:80])
            self.clock.now += updates.FALLBACK_EVERY
        self.assertEqual(github.gets, len(hostile))
        self.assertEqual(self.checked(engine)["url"], TAG)
        update = self.checked(engine)     # the redirect still unreadable, the API not due: the last answer stands
        self.assertEqual((update["status"], update["latest"], github.gets), ("available", NEXT, len(hostile) + 1))
        github.replies = failing          # and nothing that fails replaces it
        for reply in failing:
            update = self.checked(engine)
            self.assertEqual((update["latest"], update["available"]), (NEXT, True), repr(reply))
            self.assertIsNotNone(update["failed_at"])
        self.assertEqual(github.gets, len(hostile) + 1)
        self.assertEqual(set(engine.prefs.get(updates.MEMORY_KEY)), {"checked_at", "tried_at", "failed", "latest",
                         "notified", "skipped", "failures", "wait_until", "fallback_at"})
        # A check whose answer cannot be saved is still not due again at once: one request, not one a second.
        github.replies, heads = [redirect(TAG)], github.heads
        self.clock.now = updates.next_due(engine.updates.memory(), engine.updates._started, self.clock.now)
        with mock.patch.object(engine.prefs, "set_internal", side_effect=OSError("disk full")):
            for _ in range(5):
                engine.updates.run_due()
        self.assertEqual(github.heads, heads + 1)

    def test_failed_checks_wait_longer_each_time_and_one_success_resets_the_wait(self):
        github = GitHub([redirect(TAG), *[ProviderError("network", detail="timeout")] * 6, redirect(TAG)])
        engine = self.engine(github)
        checker = engine.updates

        def minutes_to_next_check():
            self.checked(engine)
            return (updates.next_due(checker.memory(), checker._started, self.clock.now) - self.clock.now) / MINUTE
        fresh = updates.load_memory(None)                                      # the first check: 30 s after a start
        self.assertEqual(updates.next_due(fresh, 1000.0, 1000.0), 1030.0)
        self.assertEqual(minutes_to_next_check(), 5)                           # every 5 minutes while all is well
        self.assertEqual([minutes_to_next_check() for _ in range(6)], [5, 10, 20, 40, 60, 60])
        self.assertEqual(minutes_to_next_check(), 5)                           # one success resets it
        self.assertEqual(github.heads, 8)
        # The thread looks at the clock every minute: a check that fell due while the Mac slept runs within a minute.
        self.assertEqual(checker._sleep(), updates.WAKE_EVERY)
        self.assertEqual(updates.WAKE_EVERY, MINUTE)
        # GitHub's Retry-After on a 403 or 429 is honoured, up to 6 hours: nothing asks before it, Check Now neither.
        github.replies, github.heads = [redirect(None, 429, 2 * 3600.0)], 0
        self.assertEqual(minutes_to_next_check(), 120)
        self.clock.now += 61 * MINUTE
        self.assertFalse(engine.action("check-update", {})["checked"])
        self.assertEqual(github.heads, 1)
        github.replies = [redirect(None, 403, 1e9)]
        self.assertEqual(minutes_to_next_check(), 6 * 60)
        self.assertEqual(github.heads, 2)
        # A wait that ends further away than that (the clock went back) counts as none.
        self.assertIsNone(updates.asked_to_wait({"wait_until": self.clock.now + 7 * 3600}, self.clock.now))
        github.replies = [redirect(TAG)]
        self.clock.now += 6 * 3600
        self.assertTrue(engine.action("check-update", {})["checked"])
        self.assertEqual((github.heads, checker.memory()["failures"], checker.memory()["wait_until"]), (3, 0, None))

    def test_a_check_that_found_nothing_new_writes_and_logs_nothing(self):
        github = GitHub([redirect(TAG)])
        engine = self.engine(github)
        saves, save = [], engine.prefs.set_internal

        def saving(key, value):
            if key == updates.MEMORY_KEY:
                saves.append(value)
            save(key, value)
        with mock.patch.object(engine.prefs, "set_internal", side_effect=saving), \
                mock.patch.object(updates.diagnostics, "log") as log:
            def lines():
                return [call.args[0] for call in log.call_args_list if call.args[0].startswith("update.")]
            self.checked(engine)                       # a new latest version: saved and logged
            self.assertEqual((len(saves), lines()), (1, ["update.checked"]))
            for _ in range(11):                        # 55 minutes of checks that find the same release
                self.checked(engine)
            self.assertEqual((len(saves), len(lines())), (1, 1))
            self.checked(engine)                       # an hour after the last save: saved again, without a word
            self.assertEqual((len(saves), len(lines())), (2, 1))
            github.replies = [ProviderError("network", detail="timeout")]
            for _ in range(4):                         # from working to failing: one save and one line
                self.checked(engine)
            self.assertEqual((len(saves), lines()[1:]), (3, ["update.check_failed"]))
            github.replies = [redirect(TAG)]
            self.checked(engine)                       # and back: one save and one line
            self.assertEqual((len(saves), lines()[2:]), (4, ["update.checked"]))
        self.assertEqual(engine.store.setting(updates.MEMORY_KEY), saves[-1])
        self.assertEqual(updates.Updater(engine, fetch=github).memory(), saves[-1])     # what a restart finds
        # What earlier builds wrote loads: 2.0.0 and 2.1.0 none, the first 2.2.0 builds six keys.
        earlier = {"checked_at": 1790000000.0, "tried_at": 1790000600.0, "failed": True, "latest": "9.9.9",
                   "notified": "9.9.9", "skipped": None}
        self.assertEqual(updates.load_memory(earlier),
                         {**earlier, "failures": 1, "wait_until": None, "fallback_at": None})
        for nothing in (None, {}, [], "junk", {"failures": -1, "wait_until": "soon", "fallback_at": float("nan")}):
            self.assertEqual(updates.load_memory(nothing), updates.load_memory(None), repr(nothing))

    def test_a_failing_or_hanging_check_never_fails_a_tick_and_a_look_never_waits_for_it(self):
        engine = self.engine(GitHub([RuntimeError("broken")]))
        self.checked(engine)
        engine.tick()
        self.assertEqual(engine.health()["failing_phases"], {})
        self.assertEqual(engine.store.alerts(), [])
        self.assertEqual(engine.snapshot()["update"]["status"], "failed")

        release, asked = threading.Event(), []

        def hangs(url):
            asked.append(url)
            release.wait(10)
            raise TimeoutError("GitHub never answered")

        def until(condition):
            deadline = time.monotonic() + 5
            while not condition() and time.monotonic() < deadline:
                time.sleep(0.01)
            return condition()
        engine = self.engine(hangs)
        engine.updates._started = self.clock.now            # no check of its own during the test: a look asks
        engine.updates.start()
        self.addCleanup(release.set)
        self.addCleanup(engine.updates.stop)
        started = time.monotonic()
        engine.looked()                   # the menu opened, a page loaded: back at once, the check runs on its thread
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(until(lambda: len(asked) == 1))
        engine.looked()                   # while it runs, nothing more
        with mock.patch.object(updates, "CHECK_WAIT", 0.3):
            answer = engine.action("check-update", {})
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(answer["checked"] and answer["update"]["checking"])
        started = time.monotonic()
        engine.tick()                     # while GitHub hangs
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(engine.health()["failing_phases"], {})
        release.set()
        self.assertTrue(until(lambda: not engine.snapshot()["update"]["checking"]))
        self.assertEqual(engine.snapshot()["update"]["status"], "failed")
        self.assertEqual(engine.store.alerts(), [])
        engine.looked()                   # within a minute of that answer: it stands
        time.sleep(0.2)
        self.assertEqual(len(asked), 1)
        self.clock.now += updates.MANUAL_GAP + 1
        engine.looked()                   # a minute later: one more check
        self.assertTrue(until(lambda: len(asked) == 2))

    def test_safe_mode_and_a_checkout_never_reach_the_network(self):
        with mock.patch("accountdeck.providers.http.request", side_effect=AssertionError("network")) as network, \
                mock.patch("accountdeck.providers.http.head", side_effect=AssertionError("network")) as short:
            engine = self.make_engine()          # no stand-in: GitHub itself would be asked
            checker = engine.updates
            self.assertEqual(checker.reason, updates.SAFE)
            checker.start()
            self.assertIsNone(checker._thread)
            checker._started -= updates.FIRST_CHECK
            checker.run_due()
            engine.looked()
            with self.assertRaises(DeckError) as refused:
                engine.action("check-update", {})
            self.assertEqual(refused.exception.code, "unsupported")
            self.assertEqual(engine.snapshot()["update"]["status"], "unavailable")
            network.assert_not_called()
            short.assert_not_called()
        for ask in (updates.github_reply, updates.github_answer):     # and the transport itself refuses in safe mode
            with self.assertRaises(SafetyViolation):
                ask()
        # A checkout: live, outside safe mode, but without install.sh's record, says why and never checks.
        self.assertEqual(updates.unavailable(mode="live", safe=False, installed=False, stand_in=False),
                         updates.NOT_INSTALLED)
        self.assertIsNone(updates.unavailable(mode="live", safe=False, installed=True, stand_in=False))
        self.assertEqual(updates.unavailable(mode="demo", safe=True, installed=True, stand_in=False), updates.DEMO)
        app = self.paths.temp_root / "app"
        app.mkdir()
        self.assertFalse(updates.manages(app))                             # a checkout: no install.json
        record = {"version": 1, "launcher": str(self.paths.temp_root.joinpath("AccountDeck 2.app", "Contents", "MacOS",
                                                                               "AccountDeck2")),
                  "launch_agents": str(self.paths.temp_root / "LaunchAgents"), "logs": str(self.paths.temp_root / "logs")}
        for manage, expected in ((False, False), (True, True)):            # a --prefix test install never updates itself
            (app / "install.json").write_text(json.dumps({**record, "manage": manage}), encoding="utf-8")
            self.assertIs(updates.manages(app), expected)
            self.assertFalse(updates.installed(app))                       # never in safe mode

    def test_a_version_is_announced_once_and_a_skipped_one_never(self):
        engine = self.engine(GitHub([redirect(TAG)]))
        engine.update_settings({"quiet_hours": None})
        shown = []

        def show(title, text, key):
            shown.append((title, key))
        engine.updates.announce(show)
        self.assertEqual(shown, [])           # nothing offered yet
        self.assertEqual(self.checked(engine)["status"], "available")
        noon = time.mktime(time.localtime()[:3] + (12, 0, 0, 0, 0, -1))
        engine.updates.clock = lambda: noon   # the hour it reads is noon, whatever the wall clock does meanwhile
        engine.update_settings({"quiet_hours": [12, 13]})
        engine.updates.announce(show)
        self.assertEqual(shown, [])           # waits for the end of quiet hours...
        engine.update_settings({"quiet_hours": None})
        engine.updates.announce(show, accepting=lambda: False)
        self.assertEqual(shown, [])           # ... and for macOS to allow notifications
        engine.updates.announce(show)
        engine.updates.announce(show)
        self.assertEqual(shown, [(f"AccountDeck {NEXT} is available", f"update|{NEXT}")])

        engine = self.engine(GitHub([redirect(TAG)]))
        engine.update_settings({"quiet_hours": None})
        self.checked(engine)
        engine.action("skip-update", {"version": NEXT})
        engine.updates.announce(show)
        update = engine.snapshot()["update"]
        self.assertEqual((update["status"], update["available"], update["skipped"]), ("skipped", False, NEXT))
        self.assertEqual(len(shown), 1)

    def test_start_update_runs_only_the_validated_version_and_refuses_where_it_must(self):
        script = Path("/Users/someone/Library/Application Support/AccountDeck2/app/get.sh")
        self.assertEqual(updates.update_command(script, NEXT), ["/bin/bash", str(script), "--version", NEXT])
        for bad in (f"{NEXT}; rm -rf ~", f"{NEXT}\n", "$(id)", "--prefix=/tmp", "", None, f" {NEXT}", "v" + NEXT):
            with self.assertRaises(DeckError, msg=repr(bad)):
                updates.update_command(script, bad)
        for bad_script in (Path("get.sh"), script.with_name("install.sh")):
            with self.assertRaises(DeckError):
                updates.update_command(bad_script, NEXT)

        offered = updates.view(updates.load_memory({"checked_at": 1.0, "tried_at": 1.0, "latest": NEXT}),
                               current=__version__, enabled=True, reason=None)
        ready = {"mode": "live", "safe": False, "installed": True, "block": offered, "version": NEXT, "busy": None,
                 "script_ok": True}
        self.assertIsNone(updates.start_refusal(**ready))
        for change, message in (({"mode": "demo"}, updates.DEMO_UPDATE), ({"safe": True}, updates.SAFE_UPDATE),
                                ({"installed": False}, updates.NOT_INSTALLED), ({"version": "99.0.0"}, updates.NOT_OFFERED),
                                ({"busy": updates.FINISH_FIRST}, updates.FINISH_FIRST),
                                ({"script_ok": False}, updates.NO_UPDATER)):
            self.assertEqual(updates.start_refusal(**{**ready, **change})[0], message, change)

        engine = self.engine(GitHub([redirect(TAG)]))
        self.checked(engine)
        with self.assertRaises(DeckError) as refused:
            engine.action("start-update", {"version": NEXT})
        self.assertEqual(refused.exception.code, "safety")
        self.assertFalse([call for call in engine.procs.calls if "terminal" in call[0]])
        # The window's own gate: the live controller refuses in safe mode before it writes or starts anything.
        controller = procs.get_controller(self.paths, demo=False, allow_readonly=True)
        with self.assertRaises(SafetyViolation):
            controller.open_update_terminal(self.paths.temp_root / "get.sh", NEXT)
        self.assertFalse((self.paths.data_dir / "terminal").exists())

        # One update window at a time: until the command of the window it opened has ended, a second start-update
        # is refused and the state says why. (Outside safe mode only, which here only the updates module is told.)
        (self.paths.temp_root / "get.sh").write_text("", encoding="utf-8")
        window = {"exited": False}            # the fake controller's record: cli_enrollment_exited reads "exited"
        outside = mock.Mock(safe_mode=lambda: False, REPO_ROOT=updates.config.REPO_ROOT)
        with mock.patch.object(updates, "config", outside), \
                mock.patch.object(engine.procs, "open_update_terminal", create=True, return_value=window) as opener:
            engine.updates.installed, engine.updates.app_root = True, self.paths.temp_root
            self.assertEqual(engine.action("start-update", {"version": NEXT})["started"], NEXT)
            with self.assertRaises(DeckError) as again:
                engine.action("start-update", {"version": NEXT})
            self.assertEqual((again.exception.code, opener.call_count), ("busy", 1))
            self.assertEqual(engine.snapshot()["update"]["blocked"], updates.UPDATE_RUNNING)
            window["exited"] = True
            engine.bump()                     # the state is built again at least once a minute (engine.HEARTBEAT)
            self.assertIsNone(engine.snapshot()["update"]["blocked"])

    def test_the_repository_is_the_one_get_sh_installs_from(self):
        text = (REPO_ROOT / "get.sh").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"^REPO=(\S+)$", text, re.M), [updates.REPO])
        self.assertEqual(updates.LATEST_URL, f"https://github.com/{updates.REPO}/releases/latest")
        self.assertEqual(updates.API_URL, f"https://api.github.com/repos/{updates.REPO}/releases/latest")
