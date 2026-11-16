"""Antigravity's agy on this Mac (providers/antigravity.py and the agy parts of procs and procs_mac): its usage answer,
the keychain item it keeps its sign-in in, AccountDeck's own runs of it, and which processes count as agy.

Safe mode is on before anything loads. No `security` command runs: FakeSecurity stands in for the process
procs.run_security starts, so keychain.py and the store run as they do live, against a keychain kept in memory. No
real agy runs: a shell script in the test's own folder stands in for it, and the sign-in window is never opened.
Every test also checks that LEAK, planted in each secret of the test records and answers, never reached a log line.
"""
from __future__ import annotations

import os

os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"

import base64  # noqa: E402
import calendar  # noqa: E402
import copy  # noqa: E402
import json  # noqa: E402
import shlex  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from accountdeck import contract, diagnostics, procs  # noqa: E402
from accountdeck.config import Paths, SafetyViolation  # noqa: E402
from accountdeck.core import DeckError  # noqa: E402
from accountdeck.platform import keychain, libproc, procs_mac  # noqa: E402
from accountdeck.providers import antigravity as agy  # noqa: E402

NOW = 1794353936.0
LEAK = "LEAKMARK7f3a"          # in every secret below: it must never show up in a log, an argument or a message
PREFIX = b"go-keyring-base64:"
# What `agy -p "/usage" --output-format json` printed on the maintainer's Mac (agy 1.5.5, 2026-11-10), byte for byte.
SAMPLE = (
    '{"conversation_id":"","status":"SUCCESS","response":"Gemini Models\\tWeekly Limit Remaining\\t99%\\t2026-11'
    '-17T20:29:37Z\\nGemini Models\\tFive Hour Limit Remaining\\t97%\\t2026-11-11T01:29:37Z\\nClaude and GPT model'
    's\\tWeekly Limit Remaining\\t100%\\t2026-11-17T22:57:34Z\\nClaude and GPT models\\tFive Hour Limit Remaining'
    '\\t100%\\t2026-11-11T03:57:34Z\\n","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tok'
    'ens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0},"command":{"name":"usage","data":{"de'
    'scription":"Within each group, models share a weekly limit and a 5-hour limit. Quota is consumed proport'
    'ionally to the cost of the tokens. Thus, limits will last longer with shorter tasks or using more cost-e'
    'ffective models. The 5-hour limit smooths out aggregate demand to fairly distribute global capacity acro'
    'ss all users, while your weekly limit is tied directly to your individual tier.","groups":[{"name":"Gemi'
    'ni Models","description":"Models within this group: Gemini Flash, Gemini Pro","buckets":[{"id":"gemini-w'
    'eekly","name":"Weekly Limit Remaining","description":"You have used some of your weekly limit, it will f'
    'ully refresh in 6 days, 21 hours.","window":"weekly","remaining_fraction":0.991513192653656,"reset_time"'
    ':"2026-11-17T20:29:37Z"},{"id":"gemini-5h","name":"Five Hour Limit Remaining","description":"You have us'
    'ed some of your 5-hour limit, it will fully refresh in 2 hours, 32 minutes.","window":"5h","remaining_fr'
    'action":0.9660527110099792,"reset_time":"2026-11-11T01:29:37Z"}]},{"name":"Claude and GPT models","descr'
    'iption":"Models within this group: Claude Opus, Claude Sonnet, GPT-OSS","buckets":[{"id":"3p-weekly","na'
    'me":"Weekly Limit Remaining","window":"weekly","remaining_fraction":1,"reset_time":"2026-11-17T22:57:34Z'
    '"},{"id":"3p-5h","name":"Five Hour Limit Remaining","window":"5h","remaining_fraction":1,"reset_time":"2'
    '026-11-11T03:57:34Z"}]}]}}}'
)


def agy_record(email="ada@example.test", **changes):
    """A made-up agy sign-in record with the real record's field names (PLAN §2); its secrets carry LEAK."""
    record = {"token": {"access_token": f"ya29.{LEAK}-access", "token_type": "Bearer",
                        "refresh_token": f"1//{LEAK}-refresh", "expiry": "2026-11-10T14:27:37.123456+01:00",
                        "expires_in": 3599},
              "auth_method": "consumer", "id_token_email": email, "user_tier": "g1-pro-tier",
              "tier_display_name": "Google AI Pro", "project_id": f"{LEAK}-project", "region": "",
              "writer_id": f"{LEAK}-writer", "wif_provider": "", "saved_wif_provider": "",
              "aicode_scope_sign_out_attempted_at": "0001-01-01T00:00:00Z"}
    record.update(changes)
    return record


def as_agy_writes(record) -> bytes:
    """The value as agy's own write stores it (go-keyring): the prefix and the standard base64 of Go's compact JSON."""
    return PREFIX + base64.standard_b64encode(json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode())


def leak_forms() -> list[str]:
    """LEAK as it could show up: plain, in hex (`security -w` prints a value with odd bytes that way), inside base64
    at each of the three byte alignments (the keychain value is base64), and the value's own prefix."""
    forms = [LEAK, LEAK.encode().hex(), PREFIX.decode()]
    for shift in range(3):
        encoded = base64.b64encode(b"\0" * shift + LEAK.encode()).decode()
        forms.append(encoded[4 * -(-shift // 3):4 * ((shift + len(LEAK)) // 3)])   # the groups made of LEAK alone
    return forms


LEAKS = leak_forms()


def texts_of(error) -> str:
    """Everything an exception, and the ones behind it, would show: messages, arguments and attributes."""
    seen, found = set(), []
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        found += [str(error), repr(error), repr(error.args), repr(getattr(error, "__dict__", {}))]
        error = error.__cause__ or error.__context__
    return "\n".join(found)


class FakeSecurity:
    """/usr/bin/security's three generic-password commands on a keychain in memory. It replaces the process
    procs.run_security starts (procs._popen), so everything up to that point runs as it does live; it never starts a
    process, and any other program asked for fails the test. `answer`: a return code every command gets (36: locked)
    or "hang" (the command never ends); `on_add(key, value)` may change what an add stores."""

    VALUED = {"find-generic-password": {"-a", "-s"}, "delete-generic-password": {"-a", "-s"},
              "add-generic-password": {"-a", "-s", "-w", "-X"}}

    def __init__(self):
        self.items, self.changed, self.clock, self.calls = {}, {}, 0, []
        self.answer = self.on_add = None

    def popen(self, argv, *, keychain=None, **options):
        if argv[0] != keychain_tool():
            raise AssertionError(f"only security may start here, not {argv[0]}")
        return _Command(self, list(argv))

    def put(self, keychain_file, value: bytes):
        self.run(["add-generic-password", "-U", "-a", agy.STORE_ACCOUNT, "-s", agy.STORE_SERVICE,
                  "-w", "-", str(keychain_file)], None, value=value)
        self.calls.clear()

    def value(self, keychain_file):
        return self.items.get((str(keychain_file), agy.STORE_SERVICE, agy.STORE_ACCOUNT))

    def run(self, args, stdin, *, value=None):
        if args == ["-i"]:
            line = stdin.decode("ascii")
            if not line.endswith("\n") or line.count("\n") != 1:
                raise AssertionError("security -i takes one line")
            args = _split(line[:-1])
        command, options, keychain_file = args[0], {}, None
        rest = args[1:]
        while rest:
            token = rest.pop(0)
            if token in self.VALUED[command]:
                options[token] = rest.pop(0)
            elif token.startswith("-"):
                options[token] = True
            elif keychain_file is None:
                keychain_file = token
            else:
                raise AssertionError("one keychain file at most")
        key = (keychain_file, options["-s"], options["-a"])
        missing = (44, b"", b"security: SecKeychainSearchCopyNext: The specified item could not be found.\n")
        if command == "find-generic-password":
            if key not in self.items:
                return missing
            if "-w" in options:
                stored = self.items[key]
                shown = stored if all(32 <= byte < 127 for byte in stored) else stored.hex().encode()
                return 0, shown + b"\n", b""
            if "-g" in options:
                raise AssertionError("-g prints the secret")
            stamp = f"202611101{self.changed[key]:05d}Z"          # the modification date, moved by every write
            return 0, (f'keychain: "{keychain_file}"\nversion: 512\nclass: "genp"\nattributes:\n'
                       f'    "acct"<blob>="{key[2]}"\n    "mdat"<timedate>=0x{stamp.encode().hex().upper()}00  '
                       f'"{stamp}\\000"\n    "svce"<blob>="{key[1]}"\n').encode(), b""
        if command == "delete-generic-password":
            return (0, b"", b"") if self.items.pop(key, None) is not None else missing
        if key in self.items and "-U" not in options:
            return 45, b"", b"security: The specified item already exists in the keychain.\n"
        stored = value if value is not None else (
            options["-w"].encode("ascii") if "-w" in options else bytes.fromhex(options["-X"]))
        if self.on_add is not None:
            stored = self.on_add(key, stored)
        self.clock += 1
        self.items[key], self.changed[key] = stored, self.clock
        return 0, b"", b""


class _Command:
    def __init__(self, security, argv):
        self.security, self.argv, self.done, self.returncode = security, argv, False, None

    def communicate(self, stdin=None, timeout=None):
        if self.done:
            return b"", b""
        self.done = True
        self.security.calls.append((self.argv, stdin))
        if self.security.answer == "hang":
            raise subprocess.TimeoutExpired(self.argv, timeout)
        if isinstance(self.security.answer, int):
            self.returncode = self.security.answer
            return b"", b"security: User interaction is not allowed.\n"
        self.returncode, out, err = self.security.run(self.argv[1:], stdin)
        return out, err

    def kill(self):
        self.returncode = -9


def keychain_tool():
    return procs.SECURITY_TOOL


def _split(line):
    """The tokens of one `security -i` line: bare words, or double-quoted ones with backslash escapes."""
    tokens, index = [], 0
    while index < len(line):
        if line[index] == " ":
            index += 1
        elif line[index] == '"':
            index, chars = index + 1, []
            while line[index] != '"':
                if line[index] == "\\":
                    index += 1
                chars.append(line[index])
                index += 1
            tokens.append("".join(chars))
            index += 1
        else:
            end = line.find(" ", index)
            end = len(line) if end < 0 else end
            tokens.append(line[index:end])
            index = end
    return tokens


class LeakWatch(unittest.TestCase):
    """Collects every log line the code under test writes, and checks at the end that no form of LEAK, and no
    e-mail of the test records, is in any of them."""

    def setUp(self):
        self.logged = []
        patcher = mock.patch.object(diagnostics, "_emit", lambda ev, lvl, fields: self.logged.append((ev, fields)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.assertEqual([form for form in [*LEAKS, "example.test"]
                                                  if form in repr(self.logged)], [], "a secret reached a log line"))
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.paths = Paths.synthetic(Path(self.folder.name) / "root")


class UsageAnswer(LeakWatch):
    def test_the_real_sample_and_the_shapes_agy_may_print(self):
        reading = agy.parse_usage(SAMPLE, now=NOW)
        report = contract._Report()
        contract._check_agy_reading(reading, "usage", report)
        self.assertEqual(report.items, [])
        self.assertEqual(reading["at"], NOW)
        self.assertEqual([group["name"] for group in reading["groups"]], ["Gemini Models", "Claude and GPT models"])
        buckets = {bucket["id"]: bucket for group in reading["groups"] for bucket in group["buckets"]}
        self.assertEqual(list(buckets), ["gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h"])
        self.assertEqual(buckets["gemini-5h"], {"id": "gemini-5h", "name": "Five Hour Limit Remaining", "window": "5h",
                                                "remaining": 0.9660527110099792, "amount": None,
                                                "resets_at": calendar.timegm((2026, 11, 11, 1, 29, 37)),
                                                "disabled": False})
        self.assertEqual((buckets["3p-weekly"]["window"], buckets["3p-weekly"]["remaining"],
                          buckets["3p-weekly"]["resets_at"]),
                         ("weekly", 1.0, calendar.timegm((2026, 11, 17, 22, 57, 34))))

        # What agy may also print: an amount instead of a fraction, a disabled bucket, a window it did not have
        # before, Go's zero time, another time zone, a hostile name, a group without buckets, fields AccountDeck
        # does not know.
        answer = json.loads(SAMPLE)
        groups = answer["command"]["data"]["groups"]
        first, second = groups[0]["buckets"]
        del first["remaining_fraction"]
        first.update(remaining_amount=120, disabled=True, window="daily", more={"nested": [1, 2]})
        second.update(reset_time="0001-01-01T00:00:00Z", name="Five\tHour‮ Limit\n\x1b[31m" + "x" * 500)
        groups[1]["buckets"][0]["reset_time"] = "2026-11-17T23:57:34+01:00"
        del groups[1]["description"]
        groups[1]["name"] = "\x1b "                    # nothing is left of it once cleaned
        del groups[1]["buckets"][1]["name"], groups[1]["buckets"][1]["window"]
        groups.append({"name": "Later models", "buckets": None})
        answer["something_new"] = [1, 2, 3]
        reading = agy.parse_usage(json.dumps(answer, ensure_ascii=False), now=NOW)
        report = contract._Report()
        contract._check_agy_reading(reading, "usage", report)
        self.assertEqual(report.items, [])
        first, second = reading["groups"][0]["buckets"]
        self.assertEqual((first["remaining"], first["amount"], first["disabled"], first["window"]),
                         (None, 120, True, "daily"))
        self.assertIsNone(second["resets_at"])
        self.assertEqual(len(second["name"]), contract.AGY_TEXT_MAX)
        self.assertTrue(second["name"].startswith("Five Hour Limit [31mxxx"))
        self.assertTrue(second["name"].isprintable())
        self.assertEqual(reading["groups"][1]["buckets"][0]["resets_at"], calendar.timegm((2026, 11, 17, 22, 57, 34)))
        self.assertIsNone(reading["groups"][1]["description"])
        self.assertEqual((reading["groups"][1]["name"], reading["groups"][1]["buckets"][1]["name"],
                          reading["groups"][1]["buckets"][1]["window"]), ("Models", "3p-5h", "other"))
        self.assertEqual(reading["groups"][2], {"name": "Later models", "description": None, "buckets": []})

    def test_hostile_or_broken_answers_fail_cleanly(self):
        good = json.loads(SAMPLE)

        def changed(edit):
            answer = copy.deepcopy(good)
            edit(answer)
            return json.dumps(answer)

        def bucket(answer):
            return answer["command"]["data"]["groups"][0]["buckets"][0]

        def groups(answer):
            return answer["command"]["data"]["groups"]

        cases = {
            "empty": "",
            "not JSON": f"{LEAK} is not an answer",
            "two answers": SAMPLE + SAMPLE,
            "a list": f'["{LEAK}"]',
            "a failure": changed(lambda a: a.update(status="ERROR", error=LEAK)),
            "no turn count": changed(lambda a: a.pop("num_turns")),
            "a turn": changed(lambda a: a.update(num_turns=1)),
            "a turn count that is a bool": changed(lambda a: a.update(num_turns=False)),
            "another command": changed(lambda a: a["command"].update(name="credits")),
            "a fraction above one": changed(lambda a: bucket(a).update(remaining_fraction=1.5)),
            "a negative fraction": changed(lambda a: bucket(a).update(remaining_fraction=-0.1)),
            "a fraction that is a bool": changed(lambda a: bucket(a).update(remaining_fraction=True)),
            "a fraction that is text": changed(lambda a: bucket(a).update(remaining_fraction="0.5")),
            "NaN": SAMPLE.replace("0.991513192653656", "NaN"),
            "infinity": SAMPLE.replace("0.991513192653656", "Infinity"),
            "a number too large": SAMPLE.replace("0.991513192653656", "1e999"),
            "a negative amount": changed(lambda a: bucket(a).update(remaining_amount=-1)),
            "a time that is no time": changed(lambda a: bucket(a).update(reset_time=LEAK)),
            "a time in other digits": changed(lambda a: bucket(a).update(reset_time="２０２６-11-11T01:29:37Z")),
            "a bucket without a value": changed(lambda a: [bucket(a).pop(key, None)
                                                           for key in ("remaining_fraction", "remaining_amount")]),
            "month 13": changed(lambda a: bucket(a).update(reset_time="2026-13-01T00:00:00Z")),
            "a leap second": changed(lambda a: bucket(a).update(reset_time="2026-12-31T23:59:60Z")),
            "nine groups": changed(lambda a: groups(a).extend({"name": f"g{n}", "buckets": []} for n in range(7))),
            "nine buckets": changed(lambda a: groups(a)[0]["buckets"].extend(
                {"id": f"b{n}", "name": "n", "window": "5h"} for n in range(7))),
            "a bucket id twice": changed(lambda a: groups(a)[1]["buckets"][0].update(id="gemini-weekly")),
            "a bucket id that is not plain": changed(lambda a: bucket(a).update(id=f"<b>{LEAK}</b>")),
            "a bucket id too long": changed(lambda a: bucket(a).update(id="x" * 65)),
            "a key twice": SAMPLE.replace('"status":"SUCCESS"', '"status":"SUCCESS","status":"SUCCESS"'),
            "nesting too deep": "[" * 100_000 + "]" * 100_000,
            "too long": SAMPLE[:-1] + ',"pad":"' + LEAK * 30_000 + '"}',
            "a number with 5000 digits": SAMPLE.replace('"num_turns":0', '"num_turns":' + "9" * 5000),
            "a name that is no text": changed(lambda a: bucket(a).update(name=7)),
            "a group that is no object": changed(lambda a: groups(a).append(LEAK)),
            "a byte order mark": "﻿" + SAMPLE,
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(DeckError) as caught:
                agy.parse_usage(text, now=NOW)
            self.assertEqual((str(caught.exception), caught.exception.code), (agy.USAGE_UNREADABLE, "antigravity"))
            self.assertNotIn(LEAK, texts_of(caught.exception))


class Store(LeakWatch):
    """KeychainStore and Antigravity's store against FakeSecurity."""

    def setUp(self):
        super().setUp()
        self.keychain = self.paths.temp_root / "agy-test.keychain-db"     # never made: FakeSecurity keeps the items
        self.security = FakeSecurity()
        patcher = mock.patch.object(procs, "_popen", self.security.popen)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.agy = agy.Antigravity(self.paths)
        self.agy.stores["cli"] = self.store = agy.KeychainStore("cli", agy.STORE_SERVICE, agy.STORE_ACCOUNT,
                                                                keychain=self.keychain)

    def stored(self):
        value = self.security.value(self.keychain)
        return json.loads(base64.b64decode(value[len(PREFIX):], validate=True))

    def test_a_sign_in_is_read_and_put_back_whole_through_agys_own_command(self):
        ada = agy_record("Ada@Example.test", tier_display_name="Google AI Pro – Überall")
        self.security.put(self.keychain, as_agy_writes(ada))
        found = self.agy.read()
        self.assertEqual((found.state, found.email, found.plan, found.tier),
                         ("ok", "ada@example.test", "Google AI Pro – Überall", "g1-pro-tier"))
        self.assertEqual(found.record, ada)
        self.assertNotIn(LEAK, repr(found))
        marker = self.agy.probe()
        self.assertTrue(marker)

        bob = agy_record("bob@example.test", token={**ada["token"], "refresh_token": f"1//{LEAK}-bob"})
        self.store.write(bob)
        self.assertEqual(self.stored(), bob)                      # whole: every field as it was saved
        adds = [(argv, stdin) for argv, stdin in self.security.calls if stdin is not None]
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0][0], [procs.SECURITY_TOOL, "-i"])  # agy's own command, on stdin
        self.assertRegex(adds[0][1].decode("ascii"), r'^add-generic-password -U -a "antigravity" -s "gemini" -w '
                                                     r'"go-keyring-base64:[A-Za-z0-9+/]+={0,2}" "[^"]+"\n$')
        for argv, _stdin in self.security.calls:
            self.assertNotIn("-X", argv)
            self.assertFalse(any("go-keyring" in arg or LEAK in arg for arg in argv), "a value in a process argument")
        self.assertEqual(self.agy.read().email, "bob@example.test")
        self.assertNotEqual(self.agy.probe(), marker)             # each write moves the marker...
        self.store.write(found.record)                             # ...and agy's own record goes back as it was
        self.assertEqual(self.stored(), ada)
        looks = [argv for argv, _stdin in self.security.calls if argv[1] == "find-generic-password"]
        self.assertTrue(any("-w" not in argv for argv in looks))   # the marker comes from the attributes alone
        self.assertFalse(any("-g" in argv for argv in looks))

        self.security.on_add = lambda key, value: as_agy_writes(agy_record("carol@example.test"))
        with self.assertRaises(DeckError) as caught:               # read back as another account: refused
            self.store.write(bob)
        self.assertEqual((str(caught.exception), caught.exception.code), (agy.NOT_WRITTEN, "antigravity"))
        self.security.on_add = None

        self.assertTrue(self.store.delete())
        self.assertEqual(self.agy.read().state, "absent")
        self.assertIsNone(self.agy.probe())
        self.assertFalse(self.store.delete())

    def test_every_store_state_reads_without_raising(self):
        def b64(data):
            return PREFIX + base64.standard_b64encode(data if isinstance(data, bytes) else data.encode())

        no_refresh = agy_record(token={"access_token": f"ya29.{LEAK}", "token_type": "Bearer"})
        no_method = agy_record()
        del no_method["auth_method"]
        cases = [
            ("nobody signed in", None, None, "absent", None),
            ("locked", as_agy_writes(agy_record()), 36, "unknown", "unreadable"),
            ("no answer", as_agy_writes(agy_record()), "hang", "unknown", "unreadable"),
            ("no prefix", json.dumps(agy_record()).encode(), None, "unknown", "undecodable"),
            ("not base64", PREFIX + b"!!" + LEAK.encode() + b"!!", None, "unknown", "undecodable"),
            ("not JSON", b64(f"{LEAK} not json"), None, "unknown", "undecodable"),
            ("a list", b64(f'["{LEAK}"]'), None, "unknown", "undecodable"),
            ("not UTF-8", b64(b"\xff\xfe{}"), None, "unknown", "undecodable"),
            ("a key twice", b64('{"auth_method":"consumer","auth_method":"consumer"}'), None, "unknown",
             "undecodable"),
            ("binary", b"\x00\x01" + LEAK.encode(), None, "unknown", "undecodable"),
            ("a work sign-in", as_agy_writes(agy_record(auth_method="gcp")), None, "unknown", "not_consumer"),
            ("no sign-in method", as_agy_writes(no_method), None, "unknown", "not_consumer"),
            ("no refresh token", as_agy_writes(no_refresh), None, "unknown", "incomplete"),
            ("no e-mail", as_agy_writes(agy_record("")), None, "unknown", "incomplete"),
            ("not an e-mail", as_agy_writes(agy_record("ada at example")), None, "unknown", "incomplete"),
        ]
        for name, value, answer, state, reason in cases:
            with self.subTest(name):
                self.security.items.clear()
                if value is not None:
                    self.security.put(self.keychain, value)
                self.security.answer = answer
                found = self.agy.read()
                self.assertEqual((found.state, found.reason, found.record if state != "ok" else None),
                                 (state, reason, None))
                self.assertNotIn(LEAK, repr(found) + str(found.message))
                if reason:
                    self.assertTrue(found.message)
                if answer is not None:
                    with self.assertRaises(DeckError) as caught:      # the marker says so too, it never guesses
                        self.agy.probe()
                    self.assertEqual(caught.exception.code, "busy")
                self.security.answer = None

        # agy keeps a file instead (any of its two, a link too): unknown, whatever the keychain holds.
        self.security.items.clear()
        self.security.put(self.keychain, as_agy_writes(agy_record()))
        self.assertEqual(self.agy.read().state, "ok")
        quiet = self.agy.probe()
        for file_store in self.paths.agy_file_stores:
            with self.subTest(file_store.name):
                file_store.parent.mkdir(parents=True, exist_ok=True)
                file_store.symlink_to(file_store.parent / "gone")
                found = self.agy.read()
                self.assertEqual((found.state, found.reason, found.message), ("unknown", "file_store", agy.FILE_STORE))
                self.assertTrue(self.agy.probe().startswith("file:"))
                file_store.unlink()
        self.assertEqual((self.agy.read().state, self.agy.probe()), ("ok", quiet))

    def test_too_long_for_agys_own_write_is_refused_and_never_sent_another_way(self):
        def record(size):
            return agy_record(padding="p" * size)

        def longest():
            low, high = 0, 8000
            while low < high:
                middle = (low + high + 1) // 2
                low, high = (middle, high) if self.store.fits(record(middle)) else (low, middle - 1)
            return low

        self.security.put(self.keychain, as_agy_writes(agy_record()))
        for line_max in (keychain.MAX_LINE, 1 << 20):            # AccountDeck's line, then agy's own limit alone
            with self.subTest(line_max=line_max), mock.patch.object(keychain, "MAX_LINE", line_max):
                size = longest()
                fitting = agy.encode_value(record(size))
                self.assertLessEqual(agy.agy_command_length(agy.STORE_SERVICE, agy.STORE_ACCOUNT, fitting),
                                     agy.AGY_COMMAND_MAX)
                if line_max != keychain.MAX_LINE:                 # agy's line is what stops it here
                    self.assertGreater(agy.agy_command_length(agy.STORE_SERVICE, agy.STORE_ACCOUNT,
                                                              agy.encode_value(record(size + 1))), agy.AGY_COMMAND_MAX)
                self.store.write(record(size))
                self.assertEqual(self.stored(), record(size))
                before = list(self.security.calls)
                with self.assertRaises(DeckError) as caught:
                    self.store.write(record(size + 1))
                self.assertEqual((str(caught.exception), caught.exception.code), (agy.TOO_LARGE, "antigravity"))
                self.assertEqual(self.security.calls, before)     # nothing ran: not on stdin, not as -X in argv
                self.assertEqual(self.stored(), record(size))      # and the item kept what it held
                self.assertFalse(self.store.fits(record(size + 1)))

        # keychain.set's stdin-only switch refuses before anything runs; without it a long value still goes the
        # way Claude Code's long logins always went (-X in argv).
        before = list(self.security.calls)
        with self.assertRaises(keychain.KeychainError) as caught:
            keychain.set("AD2 test", "long", "x" * 6000, keychain=self.keychain, stdin_only=True)
        self.assertEqual(caught.exception.reason, "unsupported")
        self.assertEqual(self.security.calls, before)
        keychain.set("AD2 test", "long", "x" * 6000, keychain=self.keychain)
        self.assertIn("-X", self.security.calls[-1][0])

    def test_no_part_of_a_sign_in_reaches_a_log_an_argument_or_a_message(self):
        secrets = [*LEAKS, as_agy_writes(agy_record()).decode(), agy.encode_value(agy_record())]
        shown = []

        def attempt(call):
            try:
                result = call()
                shown.append(repr(result))
                if isinstance(result, agy.StoreRead):
                    shown.append(str(result.message))
            except Exception as error:          # every way out is collected and checked below
                shown.append(texts_of(error))

        records = [agy_record(), agy_record("bob@example.test"), agy_record(padding=LEAK * 400),
                   agy_record(auth_method=f"{LEAK}-method"), {"token": LEAK}]
        for answer in (None, 36, "hang"):
            for value in (as_agy_writes(agy_record()), json.dumps(agy_record()).encode(), b"\xff" + LEAK.encode()):
                self.security.items.clear()
                self.security.put(self.keychain, value)
                self.security.answer = answer
                for call in (self.agy.read, self.agy.probe, self.store.delete, self.agy.read):
                    attempt(call)
                for record in records:
                    attempt(lambda record=record: self.store.write(record))
                self.security.on_add = lambda key, stored: as_agy_writes(agy_record(f"{LEAK}@example.test"))
                attempt(lambda: self.store.write(agy_record()))
                self.security.on_add = None
        for bad in (PREFIX + base64.b64encode(f"{LEAK} not json".encode()),
                    PREFIX + base64.b64encode(b"\xff" + LEAK.encode()), b"\xff" + LEAK.encode()):
            attempt(lambda bad=bad: agy.decode_value(bad))      # the errors decoding raises hold what they read
        run = procs.AgyRun(f"{LEAK} {SAMPLE}", f'oauth2: "invalid_grant" {LEAK}', 1, False)
        shown.append(repr(run))
        attempt(lambda: agy.reading_from_run(run, now=NOW))
        attempt(lambda: agy.parse_usage(SAMPLE.replace("SUCCESS", LEAK), now=NOW))
        arguments = [" ".join(argv) for argv, _stdin in self.security.calls]
        self.assertTrue(any(stdin and b"go-keyring-base64:" in stdin for _argv, stdin in self.security.calls))
        for secret in secrets:
            for where, texts in (("a message", shown), ("a process argument", arguments),
                                 ("a log line", [repr(self.logged)])):
                self.assertFalse(any(secret in text for text in texts), f"a secret reached {where}")


class Runs(LeakWatch):
    """AccountDeck's own runs of agy, with a shell script standing in for agy in the test's folder."""

    def setUp(self):
        super().setUp()
        self.controller = procs.get_controller(self.paths, demo=False, allow_readonly=True)
        self.exe, self.root = Path(self.paths.agy_exe), Path(self.paths.temp_root)
        (self.exe.parent / "answer.json").write_text(SAMPLE + "\n", encoding="utf-8")
        script = f"""#!/bin/sh
# Stands in for agy in tests/test_antigravity_store.py: records how it was started, then does what "mode" says.
here=$(dirname "$0"); out="$here/../run"
mode=$(cat "$here/mode")
echo $$ > "$out.pid"
if [ "$mode" = version ]; then
    count=$(cat "$out.count" 2>/dev/null || echo 0); echo $((count + 1)) > "$out.count"
    echo "agy version 1.5.5 (build {LEAK})"; exit 0
fi
pwd -P > "$out.cwd"
for arg in "$@"; do printf '%s\\n' "$arg"; done > "$out.argv"
/usr/bin/env > "$out.env"
cat > "$out.stdin"
/usr/bin/stat -f '%Lp %z' "$6" > "$out.log"
echo "applyAuthResult: email=ada@example.test" >> "$6"
case "$mode" in
answer) cat "$here/answer.json" ;;
hold) touch "$out.ready"; while [ ! -e "$out.go" ]; do sleep 0.05; done; cat "$here/answer.json" ;;
refused) echo 'Error: oauth2: "invalid_grant" "Token has been expired or revoked." {LEAK}' >&2; exit 1 ;;
stall) /bin/sleep 30 & echo $! > "$out.grandchild"; exec /bin/sleep 30 ;;
flood) exec /usr/bin/yes "{LEAK}" ;;
esac
"""
        self.exe.write_text(script, encoding="utf-8")
        self.exe.chmod(0o700)

    def mode(self, mode):
        (self.exe.parent / "mode").write_text(mode, encoding="utf-8")

    def end_grandchild(self):
        """Ends the `sleep` the stalled run started, when it is this test's own: started in the last two minutes."""
        try:
            pid = int(self.recorded("grandchild"))
        except (OSError, ValueError):
            return
        born = libproc.creation_time(pid)
        if born and born > procs.filetime(time.time() - 120):
            os.kill(pid, signal.SIGKILL)

    def recorded(self, name):
        return (self.root / f"run.{name}").read_text(encoding="utf-8")

    def run_usage(self, timeout=20.0):
        return self.controller.run_agy_usage(self.exe, cwd=self.paths.agy_work, log_file=self.paths.agy_usage_log,
                                             timeout=timeout)

    def typed(self, text, call):
        """`call()` while this process's standard input holds `text`, as a terminal would: a child that inherited
        it would read it."""
        read_end, write_end = os.pipe()
        os.write(write_end, text.encode())
        os.close(write_end)
        saved = os.dup(0)
        try:
            os.dup2(read_end, 0)
            return call()
        finally:
            os.dup2(saved, 0)
            os.close(saved)
            os.close(read_end)

    def test_the_usage_command_its_environment_folder_and_log(self):
        self.mode("answer")
        log = Path(self.paths.agy_usage_log)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"a log naming someone {LEAK}\n", encoding="utf-8")
        inherited = {"SSH_TTY": "/dev/ttys042", "SSH_CONNECTION": "10.0.0.1 50000 10.0.0.2 22",
                     "GEMINI_API_KEY": f"{LEAK}-key", "JETSKI_OAUTH_TOKEN": f"{LEAK}-token", "AGY_ADC_AUTH": "true",
                     "HTTPS_PROXY": "http://proxy.example.test:3128"}
        with mock.patch.dict(os.environ, inherited):
            run = self.typed(f"typed {LEAK}\n", self.run_usage)
        self.assertEqual((run.status, run.timed_out), (0, False))
        self.assertEqual(agy.reading_from_run(run, now=NOW), agy.parse_usage(SAMPLE, now=NOW))
        self.assertEqual(self.recorded("argv").splitlines(),
                         ["-p", "/usage", "--output-format", "json", "--log-file", str(log)])
        env = dict(line.split("=", 1) for line in self.recorded("env").splitlines() if "=" in line)
        self.assertEqual((env["AGY_CLI_DISABLE_AUTO_UPDATE"], env["HOME"], env["PATH"]),
                         ("true", str(self.paths.home), procs.AGY_PATH))
        self.assertEqual(env.get("HTTPS_PROXY"), "http://proxy.example.test:3128")
        allowed = {"HOME", "PATH", "USER", "LOGNAME", *procs.AGY_ENV, *procs.AGY_PASSED, "PWD", "SHLVL", "_", "OLDPWD"}
        self.assertLessEqual(set(env), allowed)                    # an SSH, token or key variable never reaches agy
        self.assertEqual(self.recorded("stdin"), "")               # stdin closed: read to its end at once
        work = Path(self.paths.agy_work)
        self.assertEqual(self.recorded("cwd").strip(), os.path.realpath(work))
        self.assertEqual(work.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.recorded("log").split(), ["600", "0"])   # a new private log for each run...
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(LEAK, log.read_text(encoding="utf-8"))      # ...the last one's lines gone

        # Never a real agy, folder or log in safe mode: refused before anything is made.
        outside = Path("/tmp") / f"accountdeck-not-a-test-folder-{os.getpid()}"
        for exe, cwd, log_file in (("/usr/local/bin/agy-not-a-test-file", work, log), (self.exe, outside, log),
                                   (self.exe, work, outside / "agy.log")):
            with self.subTest(exe=str(exe), cwd=str(cwd)), self.assertRaises(SafetyViolation):
                self.controller.run_agy_usage(exe, cwd=cwd, log_file=log_file, timeout=5)
        self.assertFalse(outside.exists())

    def test_a_run_is_its_own_while_it_runs_and_ends_at_its_limits(self):
        self.mode("hold")
        finished = {}
        worker = threading.Thread(target=lambda: finished.update(run=self.run_usage()))
        worker.start()
        self.addCleanup(lambda: ((self.root / "run.go").touch(), worker.join(30)))
        deadline = time.monotonic() + 20
        while not (self.root / "run.ready").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        pid = int(self.recorded("pid"))
        ctime = libproc.creation_time(pid)
        self.assertTrue(ctime)
        self.assertEqual(self.controller.owned.get(pid), ctime)       # AccountDeck's own from its start...
        ours = procs.ProcessInfo(pid, os.getpid(), "agy", str(self.exe), ctime, None, kind="agy")
        theirs = procs.ProcessInfo(pid + 1, 1, "agy", "/Users/someone/.local/bin/agy", ctime + 1, None, kind="agy")
        self.assertEqual(self.controller.agy_consumers(rows=[ours, theirs]), [theirs])   # ...so never a consumer
        (self.root / "run.go").touch()
        worker.join(30)
        self.assertEqual((finished["run"].status, agy.run_outcome(finished["run"])), (0, "ok"))

        # Past the time limit the run, and only the run, is ended: a process it started is left alone.
        self.mode("stall")
        started = time.monotonic()
        run = self.run_usage(timeout=1.0)
        elapsed = time.monotonic() - started
        self.addCleanup(self.end_grandchild)            # before any assertion: a failing one leaves no `sleep` behind
        self.assertLess(elapsed, 1.0 + procs.AGY_STOP_GRACE)
        self.assertEqual((run.status, run.timed_out, agy.run_outcome(run)), (None, True, "timed_out"))
        self.assertIsNone(libproc.creation_time(int(self.recorded("pid"))))
        self.assertIsNotNone(libproc.creation_time(int(self.recorded("grandchild"))),
                             "a process the run started was ended too")

        # An answer longer than any usage answer ends the run as well; nothing of it is kept beyond the limit.
        self.mode("flood")
        run = self.run_usage()
        self.assertFalse(run.timed_out)
        self.assertLess(run.status, 0)
        self.assertLessEqual(len(run.output.encode()), procs.AGY_OUTPUT_MAX)
        self.assertEqual(agy.run_outcome(run), "unreadable")

    def test_failures_are_told_apart_without_guessing(self):
        Run, absent = procs.AgyRun, agy.StoreRead.absent()
        signed_in = agy.StoreRead.ok(agy_record(), "ada@example.test")
        refused = 'Error: oauth2: "invalid_grant" "Token has been expired or revoked."'
        cases = [
            (Run(SAMPLE, "", 0, False), None, "ok"),
            (Run(SAMPLE, refused, 0, False), signed_in, "ok"),           # a good answer never asks for a sign-in
            (Run("", "", None, True), None, "timed_out"),
            (Run("", "", None, False), None, "not_started"),
            (Run("", refused, 1, False), signed_in, "reauth"),
            (Run('{"status":"ERROR","error":"invalid_grant"}', "", 1, False), absent, "reauth"),
            (Run("", "Error: not signed in", 1, False), absent, "signed_out"),
            (Run(SAMPLE.replace("SUCCESS", "FAILED"), "", 0, False), absent, "signed_out"),
            (Run("", "401 Unauthorized: UNAUTHENTICATED, please sign in again", 1, False), signed_in, "unreadable"),
            (Run("", "invalid_grants", 1, False), signed_in, "unreadable"),
            (Run(SAMPLE, "", 3, False), signed_in, "unreadable"),         # an answer with a failed exit is not taken
            (Run("not JSON", "", 0, False), None, "unreadable"),
        ]
        for run, after, outcome in cases:
            with self.subTest(outcome=outcome, errors=run.errors[:30], status=run.status):
                self.assertEqual(agy.run_outcome(run, after=after), outcome)
                if outcome == "ok":
                    self.assertEqual(agy.reading_from_run(run, now=NOW, after=after), agy.parse_usage(SAMPLE, now=NOW))
                    continue
                with self.assertRaises(DeckError) as caught:
                    agy.reading_from_run(run, now=NOW, after=after)
                self.assertEqual((caught.exception.outcome, str(caught.exception), caught.exception.code),
                                 (outcome, *agy.RUN_ERRORS[outcome]))

        # A real run that Google refused: "sign in again" for that account.
        self.mode("refused")
        with self.assertRaises(DeckError) as caught:
            agy.reading_from_run(self.run_usage(), now=NOW, after=signed_in)
        self.assertEqual(caught.exception.code, "reauth_required")
        self.assertNotIn(LEAK, texts_of(caught.exception))

    def test_agy_and_only_agy_counts_and_its_sign_in_window_and_version(self):
        home = str(self.paths.home)
        app = "/Applications/Antigravity.app/Contents"
        t3 = f"{home}/.t3/tools/antigravity-acp/darwin-arm64/versions/abc"
        rows = [procs.ProcessInfo(30001, 1, "agy", f"{home}/.local/bin/agy", 10, None),
                procs.ProcessInfo(30002, 1, "agy", "/opt/homebrew/Cellar/agy/1.5.5/bin/agy", 11, None),
                procs.ProcessInfo(30003, 1, "agy", None, 0, "?"),            # identity unreadable: it counts
                procs.ProcessInfo(30004, 1, "agy_acp_server.p", None, 0, "?"),   # T3 Code's agent by its short name
                procs.ProcessInfo(30005, 1, "agy_acp_server.par", f"{t3}/agy_acp_server.par", 12, None),
                procs.ProcessInfo(30006, 30005, "localharness_external", f"{t3}/localharness_external", 13, None),
                procs.ProcessInfo(30007, 1, "Antigravity", f"{app}/MacOS/Antigravity", 14, "com.google.antigravity"),
                procs.ProcessInfo(30008, 30007, "language_server_macos_arm",
                                  f"{app}/Resources/bin/language_server_macos_arm", 15, "com.google.antigravity")]
        rows = procs.with_kinds(rows, self.paths)
        self.assertEqual([row.pid for row in rows if row.kind == "agy"], [30001, 30002, 30003])
        self.controller.owned.update({30001: 10, 30003: 0})               # its own run; an unreadable one never is
        self.assertEqual([row.pid for row in self.controller.agy_consumers(rows=rows)], [30002, 30003])
        for row in rows:
            self.assertEqual(self.controller._relevant(row.name, row.path), row.name == "agy", row.name)
        # agy behind a link: its installed path resolves to a file of another name, which runs under that name.
        target = f"{home}/Applications/agy-cli/bin/agy-cli-1.5.5"
        linked = [procs.ProcessInfo(30009, 1, "agy-cli-1.5.5", target, 16, None),
                  procs.ProcessInfo(30010, 1, "agy-cli-1.5.5", None, 0, "?")]    # identity unreadable: it counts
        self.assertEqual([row.kind == "agy" for row in procs.with_kinds(linked, self.paths, agy_path=target)],
                         [True, True])
        self.controller.agy_path = target
        self.assertTrue(self.controller._relevant("agy-cli-1.5.5", target))
        self.controller.agy_path = None

        # The sign-in window: agy in Terminal, not updating itself, with nothing that moves its sign-in elsewhere.
        # Terminal is never opened here: `open` is replaced, and safe mode refuses it anyway.
        opened = []

        class Opened:
            def wait(self, timeout=None):
                return 0

        def spawn(argv, **options):
            opened.append([str(arg) for arg in argv])
            return Opened()

        with mock.patch.object(procs_mac, "_check_spawn_target"), mock.patch.object(self.controller, "spawn", spawn):
            record = self.controller.open_agy_signin(self.exe, cwd=self.paths.home)
        self.assertEqual(opened[0][:3], [procs_mac.OPEN_TOOL, "-b", procs_mac.TERMINAL_BUNDLE_ID])
        script = Path(opened[0][3]).read_text(encoding="utf-8")
        self.assertIn("export AGY_CLI_DISABLE_AUTO_UPDATE=true", script)
        for name in ("SSH_TTY", "SSH_CONNECTION", "AGY_ADC_AUTH", "JETSKI_OAUTH_TOKEN", "GEMINI_API_KEY"):
            self.assertIn(f"unset {name}\n", script)
        self.assertTrue(script.rstrip().endswith("exec " + shlex.quote(str(self.exe))))
        self.assertEqual(set(record), {"pid", "ctime", "pidfile", "opened"})
        with self.assertRaises(SafetyViolation):
            self.controller.open_agy_signin(self.exe, cwd=self.paths.home)

        # agy is found where its installer puts it, and its version read once for each binary.
        adapter = agy.Antigravity(self.paths, procs=self.controller)
        self.mode("version")
        self.assertEqual((adapter.executable(), adapter.version(), adapter.version()), (self.exe, "1.5.5", "1.5.5"))
        self.assertEqual(self.recorded("count").strip(), "1")
        status = self.exe.stat()
        os.utime(self.exe, ns=(status.st_atime_ns, status.st_mtime_ns + 5_000_000_000))   # another binary there
        self.assertEqual(adapter.version(), "1.5.5")
        self.assertEqual(self.recorded("count").strip(), "2")
        self.exe.chmod(0o600)
        self.assertIsNone(adapter.executable())


if __name__ == "__main__":
    unittest.main()
