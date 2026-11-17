"""Synthetic providers and machine for tests and demo mode (§11.4).

Nothing here reads or writes a real profile or Keychain item: build_fake_home refuses any Paths that is
not synthetic and every target outside its test root, the fake adapters never make a request, and
FakeMacCrypto stands in for the "Claude Safe Storage" Keychain password with a fixed synthetic one.
"""
from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid

from ..config import SafetyViolation
from ..core import DeckError, atomic_write, json_bytes, read_json
from .antigravity import STORE_ACCOUNT, STORE_SERVICE, Antigravity, KeychainStore, StoreRead
from .chromium_crypto import ChromiumCryptoMac, derive_mac_key, seal_cbc
from .claude import CHROMIUM_EPOCH, DEFAULT_SCOPE, Claude, claim_reset
from .codex import AUTH_CLAIMS, Codex, jwt_claims
from .errors import ProviderError
from .status import StatusSource

CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_SCOPES = "user:inference user:profile user:sessions:claude_code"
SAFE_STORAGE_PASSWORD = "synthetic-safe-storage-password"     # what FakeMacCrypto "reads from the Keychain"
REGISTRY_VERSION = "2.1.284"
REGISTRY_KEYS = ("pid", "status", "kind", "entrypoint", "name", "cwd", "updatedAt", "statusUpdatedAt", "procStart",
                 "version")
ROTATED_LIFETIME = 10 * 86400
COOKIE_SCHEMA = """
CREATE TABLE meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR);
INSERT INTO meta VALUES('version', '24');
CREATE TABLE cookies(creation_utc INTEGER NOT NULL, host_key TEXT NOT NULL, top_frame_site_key TEXT NOT NULL,
    name TEXT NOT NULL, value TEXT NOT NULL, encrypted_value BLOB NOT NULL, path TEXT NOT NULL,
    expires_utc INTEGER NOT NULL, is_secure INTEGER NOT NULL, is_httponly INTEGER NOT NULL,
    last_access_utc INTEGER NOT NULL, has_expires INTEGER NOT NULL, is_persistent INTEGER NOT NULL,
    priority INTEGER NOT NULL, samesite INTEGER NOT NULL, source_scheme INTEGER NOT NULL,
    source_port INTEGER NOT NULL, last_update_utc INTEGER NOT NULL);
"""


def test_cipher(data, decrypt=False):
    """A reversible stand-in for the vault cipher in tests and demos; it protects nothing."""
    if decrypt:
        if not data.startswith(b"TEST-ENCRYPTED:"):
            raise ValueError("not a test-cipher blob")
        return base64.b64decode(data[15:])
    return b"TEST-ENCRYPTED:" + base64.b64encode(data)


def unsigned_jwt(claims):
    """A JWT with alg "none", as Codex's auth.json holds them for synthetic accounts."""
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return part({"alg": "none", "typ": "JWT"}) + "." + part(claims) + "."


class FakeMacCrypto(ChromiumCryptoMac):
    """Claude Desktop's macOS encryption with a fixed password instead of the Keychain item."""

    def __init__(self, password=SAFE_STORAGE_PASSWORD):
        super().__init__()
        self._fixed = password

    def _password(self):
        return self._fixed


class Timeline:
    """A script [(t_offset, payload_or_exception)] against an injectable clock. current() returns
    (a copy of) the payload of the last entry whose offset has passed since construction, None
    before the first one, and raises an entry that is an exception."""

    def __init__(self, script, now=time.time):
        self.script = sorted(script or (), key=lambda entry: entry[0])
        self.now = now
        self.start = now()

    def current(self):
        elapsed = self.now() - self.start
        payload = None
        for offset, value in self.script:
            if offset > elapsed:
                break
            payload = value
        if isinstance(payload, BaseException) or (isinstance(payload, type) and issubclass(payload, BaseException)):
            raise payload
        return copy.deepcopy(payload)


class ResetLedger:
    """Limit resets on the providers' side, for tests and the demo, in memory: per account (by identity) Claude's
    `cedar_ember` block (`claude`) or Codex's `rateLimitResetCredits` (`codex`). A use spends one reset, as the
    provider would: once per request key (a repeat of a key it applied answers `already_used` / `alreadyRedeemed`),
    and the limits it refills read as unused afterwards (`claude_payload`, `codex_payload`).

    `scripts[identity]` lists what the next uses of that account do: "timeout" (the reset is used, but no answer
    comes back), "lost" (nothing is used and no answer comes back), "error" (HTTP 500 / JSON-RPC -32603 after
    using it), and for Claude "unavailable" (the reset is used and the answer says `unavailable`), "cooldown" (the
    reset is used, as by an earlier request that arrived late, and this one is answered `cooldown`) and
    "unreachable" (the request never leaves this Mac). `hidden` holds the Codex accounts whose answers carry no
    `rateLimitResetCredits` for now. `uses` lists (identity, target, key) of every use request received, `reads`
    the identities whose resets were read; tests compare them, nothing prints them."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.claude, self.codex, self.scripts, self.hidden = {}, {}, {}, set()
        self.applied, self.cleared, self.uses, self.reads = {}, {}, [], []

    def _step(self, identity):
        script = self.scripts.get(identity) or []
        return script.pop(0) if script else None

    # -- Claude --
    def claude_answer(self, identity):
        with self.lock:
            self.reads.append(identity)
            block = self.claude.get(identity)
        if block is None:
            raise ProviderError("network", detail="no_script")
        return {"five_hour": None, "seven_day": None, "cedar_ember": copy.deepcopy(block)}

    def claude_claim(self, identity, body):
        with self.lock:
            grant_id, key = body.get("grant_id"), body.get("request_id")
            step = self._step(identity)
            if step == "unreachable":
                raise ProviderError("network", detail="network", meta={"class": "network", "unsent": True})
            self.uses.append((identity, grant_id, key))
            if key in self.applied:
                return {"result": "already_used", "reason": "already_used"}, {"status": 200, "class": "2xx"}
            if step == "lost":
                raise ProviderError("network", detail="timeout", meta={"class": "timeout", "unsent": False})
            grant = next((g for g in (self.claude.get(identity) or {}).get("grants") or () if g.get("id") == grant_id), None)
            if grant is None or grant.get("resets_left", 0) < 1:
                return {"result": "ineligible", "reason": "unknown_grant"}, {"status": 200, "class": "2xx"}
            grant["resets_left"] -= 1
            self.applied[key] = identity
            self.cleared.setdefault(identity, set()).update(grant.get("clears") or ("five_hour", "seven_day"))
        if step == "timeout":
            raise ProviderError("network", detail="timeout", meta={"class": "timeout", "unsent": False})
        if step == "error":
            raise ProviderError("network", http=500, meta={"class": "5xx", "status": 500})
        if step in ("unavailable", "cooldown"):
            return {"result": step, "reason": step}, {"status": 200, "class": "2xx"}
        return {"result": "reset", "resets_left": grant["resets_left"], "cleared": list(grant.get("clears") or ())}, \
            {"status": 200, "class": "2xx"}

    def claude_payload(self, identity, payload):
        """A usage answer as it reads after a use refilled limits: those limits unused."""
        cleared = self.cleared.get(identity)
        if not cleared or not isinstance(payload, dict):
            return payload
        for key in cleared:
            if isinstance(payload.get(key), dict):
                payload[key] = {**payload[key], "utilization": 0.0}
        kinds = {"session"} if "five_hour" in cleared else set()
        kinds |= {"weekly_all"} if "seven_day" in cleared else set()
        fable = "seven_day_overage_included" in cleared          # Claude's name of Fable's weekly limit
        if fable and isinstance(payload.get("seven_day_fable"), dict):
            payload["seven_day_fable"] = {**payload["seven_day_fable"], "utilization": 0.0}
        for row in payload.get("limits") or ():
            if not isinstance(row, dict):
                continue
            model = (row.get("scope") or {}).get("model") if isinstance(row.get("scope"), dict) else None
            name = model.get("display_name") if isinstance(model, dict) else model
            if row.get("kind") in kinds or (fable and row.get("kind") == "weekly_scoped"
                                            and isinstance(name, str) and name.startswith("Fable")):
                row["percent"] = 0.0
        return payload

    # -- Codex --
    def codex_consume(self, identity, params):
        with self.lock:
            key, credit_id = params.get("idempotencyKey"), params.get("creditId")
            self.uses.append((identity, credit_id, key))
            step = self._step(identity)
            if key in self.applied:
                return {"outcome": "alreadyRedeemed"}
            if step == "lost":
                raise ProviderError("network", detail="timeout", meta={"class": "timeout"})
            resets = self.codex.get(identity) or {}
            credit = next((c for c in resets.get("credits") or () if c.get("id") == credit_id), None)
            if credit is None or credit.get("status") not in (None, "available"):
                return {"outcome": "noCredit"}
            credit["status"] = "redeemed"
            resets["availableCount"] = max(0, int(resets.get("availableCount") or 0) - 1)
            self.applied[key] = identity
            self.cleared.setdefault(identity, set()).add("codex")
        if step in ("timeout", "error"):
            raise ProviderError("codex", detail="-32603", meta={"class": "other", "rpc_code": -32603})
        return {"outcome": "reset"}

    def codex_payload(self, identity, payload):
        """An `account/rateLimits/read` answer with this account's credits, and its limits unused after a use."""
        if identity in self.codex and identity not in self.hidden and isinstance(payload, dict):
            with self.lock:
                payload["rateLimitResetCredits"] = copy.deepcopy(self.codex[identity])
        if "codex" in self.cleared.get(identity, ()) and isinstance(payload, dict):
            bucket = payload.get("rateLimits") or {}
            for slot in ("primary", "secondary"):
                if isinstance(bucket.get(slot), dict):
                    bucket[slot] = {**bucket[slot], "usedPercent": 0.0}
            bucket.pop("rateLimitReachedType", None)
            payload.pop("ordinaryUsageAllowed", None)
        return payload


class FakeClaude(Claude):
    """Claude with scripted usage instead of HTTP. `scripts` maps an account uuid to its script and
    `script` serves every other account; payloads are what /api/oauth/usage returns (the cookie
    route reads the same timeline). Credential tiers, health and restore are the real code, on
    macOS-format profiles under FakeMacCrypto unless `crypto` is given. Limit resets come from `ledger` (a
    ResetLedger); `reset_reads` lists (identity, route) of each resets read and `claims` each claim's request
    (method, url, the JSON body and whether the beta flag goes with it), never a token."""

    def __init__(self, paths, script=(), *, scripts=None, identities=None, now=time.time, scope=DEFAULT_SCOPE,
                 crypto=None, ledger=None):
        super().__init__(paths, crypto=crypto or FakeMacCrypto(), scope=scope, clock=now)
        self.default = Timeline(script, now)
        self.timelines = {key: Timeline(value, now) for key, value in (scripts or {}).items()}
        self.identities = identities or {}
        self.ledger = ledger if ledger is not None else ResetLedger(now)
        self.reset_reads, self.claims = [], []

    def read_resets(self, profile, route):
        if route == "oauth":
            self.reset_token(profile)              # the real check: a saved token valid long enough, never renewed
        else:
            self.cookie_header(profile)
        self.reset_reads.append((profile.get("identity"), route))
        return self.ledger.claude_answer(profile.get("identity"))

    def claim_reset(self, profile, org, grant_id, request_id):
        identity = profile.get("identity")

        def answer(method, url, *, token=None, timeout=None, json_body=None, **_):
            from .http import request_headers
            self.claims.append({"method": method, "url": url, "body": copy.deepcopy(json_body), "timeout": timeout,
                                "beta": request_headers(url, token=token).get("anthropic-beta"), "token": bool(token)})
            return self.ledger.claude_claim(identity, json_body)
        return claim_reset(answer, self.reset_token(profile), org, grant_id, request_id)

    def identity(self, profile):
        ident = profile.get("identity")
        known = self.identities.get(ident, {})
        return {"identity": ident, "email": known.get("email", f"claude-{str(ident)[:8]}@example.test"),
                "plan": known.get("plan", "max"), "org": known.get("org", profile.get("org", ""))}

    def _read_oauth(self, profile, token, *, incident):
        return self._payload(profile)

    def _read_cookie(self, profile, *, incident):
        self.cookie_header(profile)
        return self._payload(profile)

    def _payload(self, profile):
        payload = self.timelines.get(profile.get("identity"), self.default).current()
        if payload is None:
            raise ProviderError("network", detail="no_script")
        return self.ledger.claude_payload(profile.get("identity"), payload)


class FakeCodex(Codex):
    """Codex with scripted probes instead of the app-server. Payloads are what
    account/rateLimits/read returns; a refresh-mode probe rotates the probe's auth.json like the
    real helper, so persistence and fingerprints run the real code."""

    def __init__(self, paths, script=(), *, scripts=None, now=time.time, timings=None, sleep=time.sleep, ledger=None):
        super().__init__(paths, None, timings=timings, clock=now, sleep=sleep)
        self.default = Timeline(script, now)
        self.timelines = {key: Timeline(value, now) for key, value in (scripts or {}).items()}
        self.ledger = ledger if ledger is not None else ResetLedger(now)
        self.reset_sessions = []     # per reset session: whose, whether its refresh token was blank, the calls made

    def _rates(self, identity):
        payload = self.timelines.get(identity, self.default).current()
        if payload is None:
            raise ProviderError("codex", detail="no_script")
        return self.ledger.codex_payload(identity, payload)

    def _probe(self, work, identity, refresh):
        if refresh:
            _rotate_codex_auth(Path(work) / "auth.json", self.clock())
        return self._rates(identity), None

    def _reset_rpc(self, work):
        auth = read_json(Path(work) / "auth.json")
        return _FakeResetRPC(self, Codex.identity(auth)["identity"], blank=(auth.get("tokens") or {}).get("refresh_token") == "")


class _FakeResetRPC:
    """The app-server of a FakeCodex reset session: account/read, rateLimits/read and the consume, on the ledger."""

    def __init__(self, codex, identity, *, blank):
        self.codex, self.identity = codex, identity
        self.log = {"identity": identity, "refresh_blank": blank, "calls": []}
        codex.reset_sessions.append(self.log)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def call(self, method, params=None, timeout=25):
        self.log["calls"].append((method, copy.deepcopy(params or {})))
        if method == "account/read":
            return {"account": {"type": "chatgpt", "email": "codex@example.test", "planType": "plus"}}
        if method == "account/rateLimits/read":
            return self.codex._rates(self.identity)
        if method == "account/rateLimitResetCredit/consume":
            return self.codex.ledger.codex_consume(self.identity, params or {})
        raise ProviderError("codex", detail="-32601", meta={"class": "other", "rpc_code": -32601})


# ---- Antigravity (agy) ----

# What `agy -p "/usage" --output-format json` printed once on a real Mac (agy 1.5.5, 2026-11-10): no account, no
# secret in it. `agy_usage_output` gives the same shape with times and fractions of a test's choosing.
AGY_USAGE_SAMPLE = (
    '{"conversation_id":"","status":"SUCCESS","response":"Gemini Models\\tWeekly Limit Remaining\\t99%\\t'
    '2026-11-17T20:29:37Z\\nGemini Models\\tFive Hour Limit Remaining\\t97%\\t2026-11-11T01:29:37Z\\nClaude and GPT '
    'models\\tWeekly Limit Remaining\\t100%\\t2026-11-17T22:57:34Z\\nClaude and GPT models\\tFive Hour Limit Remaining'
    '\\t100%\\t2026-11-11T03:57:34Z\\n","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,'
    '"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0},"command":{"name":"usage","data":{"description":'
    '"Within each group, models share a weekly limit and a 5-hour limit. Quota is consumed proportionally to the cost '
    'of the tokens. Thus, limits will last longer with shorter tasks or using more cost-effective models. The 5-hour '
    'limit smooths out aggregate demand to fairly distribute global capacity across all users, while your weekly '
    'limit is tied directly to your individual tier.","groups":[{"name":"Gemini Models","description":"Models within '
    'this group: Gemini Flash, Gemini Pro","buckets":[{"id":"gemini-weekly","name":"Weekly Limit Remaining",'
    '"description":"You have used some of your weekly limit, it will fully refresh in 6 days, 21 hours.","window":'
    '"weekly","remaining_fraction":0.991513192653656,"reset_time":"2026-11-17T20:29:37Z"},{"id":"gemini-5h","name":'
    '"Five Hour Limit Remaining","description":"You have used some of your 5-hour limit, it will fully refresh in 2 '
    'hours, 32 minutes.","window":"5h","remaining_fraction":0.9660527110099792,"reset_time":"2026-11-11T01:29:37Z"}]},'
    '{"name":"Claude and GPT models","description":"Models within this group: Claude Opus, Claude Sonnet, GPT-OSS",'
    '"buckets":[{"id":"3p-weekly","name":"Weekly Limit Remaining","window":"weekly","remaining_fraction":1,'
    '"reset_time":"2026-11-17T22:57:34Z"},{"id":"3p-5h","name":"Five Hour Limit Remaining","window":"5h",'
    '"remaining_fraction":1,"reset_time":"2026-11-11T03:57:34Z"}]}]}}}')
# The sample's buckets: (seconds from when it was taken to the bucket's reset, its remaining fraction).
AGY_SAMPLE_BUCKETS = {"gemini-weekly": (595920, 0.991513192653656), "gemini-5h": (9120, 0.9660527110099792),
                      "3p-weekly": (604797, 1), "3p-5h": (17997, 1)}
# Why the fake store reads as unknown (`FakeAntigravity.fault`), in the words the dashboard would show.
AGY_FAULTS = {
    "unreadable": "AccountDeck could not read Antigravity's sign-in from the keychain.",
    "undecodable": "Antigravity's sign-in is in a form AccountDeck does not know.",
    "incomplete": "Antigravity's sign-in lacks a part AccountDeck needs.",
    "not_consumer": "Antigravity is signed in with a work account, which AccountDeck does not handle.",
    "file_store": "agy keeps its sign-in in a file on this Mac, which AccountDeck does not handle.",
}


def _rfc3339(when):
    return dt.datetime.fromtimestamp(when, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def agy_record(email, *, plan="Google AI Pro", tier="g1-pro-tier", refresh=None, auth_method="consumer", now=None):
    """A made-up agy sign-in record with the real record's field names (PLAN §2). Nothing in it works anywhere; the
    tokens only tell records apart (`refresh` gives the refresh token a value of the test's choosing)."""
    tag = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]
    now = time.time() if now is None else now
    return {"token": {"access_token": f"synthetic-agy-access-{tag}-{int(now)}", "token_type": "Bearer",
                      "refresh_token": refresh or f"synthetic-agy-refresh-{tag}", "expiry": _rfc3339(now + 3600),
                      "expires_in": 3599},
            "auth_method": auth_method, "id_token_email": email, "user_tier": tier, "tier_display_name": plan,
            "project_id": "", "region": "", "writer_id": f"synthetic-writer-{tag[:8]}", "wif_provider": "",
            "saved_wif_provider": "", "aicode_scope_sign_out_attempted_at": ""}


def agy_usage_output(now, buckets=None):
    """What agy's usage command prints, shaped as `AGY_USAGE_SAMPLE`, taken at `now`: each bucket's reset as far
    from `now` as it was in the sample, and its remaining fraction from `buckets` ({bucket id: fraction, or
    (fraction, seconds to its reset)}) or the sample's."""
    data = json.loads(AGY_USAGE_SAMPLE)
    lines = []
    for group in data["command"]["data"]["groups"]:
        for bucket in group["buckets"]:
            offset, fraction = AGY_SAMPLE_BUCKETS[bucket["id"]]
            wanted = (buckets or {}).get(bucket["id"], fraction)
            fraction, offset = wanted if isinstance(wanted, tuple) else (wanted, offset)
            bucket.pop("description", None)          # it names the time to the reset, which moved
            bucket.update(remaining_fraction=fraction, reset_time=_rfc3339(now + offset))
            lines.append(f"{group['name']}\t{bucket['name']}\t{round(fraction * 100)}%\t{bucket['reset_time']}")
    data["response"] = "\n".join(lines) + "\n"
    return json.dumps(data, separators=(",", ":"))


class _FakeAgyStore(KeychainStore):
    """agy's keychain item, in the FakeAntigravity's memory."""

    def __init__(self, agy):
        super().__init__("cli", STORE_SERVICE, STORE_ACCOUNT)
        self.agy = agy

    def read(self):
        return self.agy._read_store()

    def write(self, record):
        self.agy._write_store(record)

    def delete(self):
        return self.agy._delete_store()

    def probe(self):
        return self.agy._probe_store()


class FakeAntigravity(Antigravity):
    """agy for tests and the demo, in memory: not installed and nobody signed in unless a test says so. Nothing here
    reads a keychain item, a file or a process.

    The store: `sign_in(record)` puts a decoded agy record in it (as the user signing in to agy would), `renew()`
    rewrites it as agy's hourly renewal does (a new access token, the same refresh token), `sign_out()` empties it.
    Each change moves the change marker `probe` gives, as agy's rewrites move the keychain item's. It reads as the
    real store would: absent, ok, or unknown when the record lacks its refresh token or e-mail ("incomplete") or is
    not a personal sign-in ("not_consumer"). Scripted:

    fault          the item exists but reads as unknown for this reason ("unreadable", "undecodable")
    file           agy keeps its sign-in in a file instead (`file_store`; nothing is written there)
    probe_fails    the keychain does not answer `probe`
    script_writes  what the next writes do: "ok", "fail" (refused, nothing changed), "garble" (the item no longer
                   decodes, then refused), or a record (it lands instead, then refused: the read-back e-mail differs)
    script_deletes what the next deletes do: "ok" or "fail"
    sign_in_at     a sign-in that lands at a given time on `clock` if agy has none then and `unless()` is not true
                   (the demo's Terminal window, where nobody signs in after a Cancel)

    `writes` lists the e-mail of every record written, in order; `version_runs` counts the `agy --version` runs (the
    first `version()` for the installed agy; later ones answer from memory, as the real adapter's do)."""

    def __init__(self, paths, procs=None, *, installed=False, record=None, clock=time.time):
        super().__init__(paths, procs)
        self.installed, self.record, self.changes, self.clock = installed, None, 0, clock
        self.fault, self.file, self.probe_fails = None, False, False
        self.writes, self._write_script, self._delete_script, self._pending = [], [], [], None
        self.version_runs, self._version_known = 0, False
        self.lock = threading.RLock()
        self.stores = {"cli": _FakeAgyStore(self)}
        if record is not None:
            self.sign_in(record)

    # ---- what a test or the demo does to the store ----
    def sign_in(self, record):
        with self.lock:
            self.record, self.fault, self.changes = copy.deepcopy(record), None, self.changes + 1

    def sign_in_at(self, record, at, unless=None):
        with self.lock:
            self._pending = (at, copy.deepcopy(record), unless)

    def renew(self):
        with self.lock:
            self.record["token"].update(access_token=f"{self.record['token']['access_token']}-renewed",
                                        expiry=_rfc3339(self.clock() + 3600))
            self.changes += 1

    def sign_out(self):
        with self.lock:
            self.record, self.fault, self.changes = None, None, self.changes + 1

    def update_binary(self):
        """agy is updated in place: its version is not known until `version()` asks again."""
        with self.lock:
            self._version_known = False

    def script_writes(self, *outcomes):
        with self.lock:
            self._write_script.extend(outcomes)

    def script_deletes(self, *outcomes):
        with self.lock:
            self._delete_script.extend(outcomes)

    # ---- the adapter ----
    def executable(self):
        return Path(self.paths.agy_exe) if self.installed and self.paths.agy_exe is not None else None

    def version(self):
        with self.lock:
            if not self.installed:
                return None
            if not self._version_known:
                self.version_runs, self._version_known = self.version_runs + 1, True
        return "1.5.5"

    def known_version(self):
        with self.lock:
            return (True, "1.5.5") if self.installed and self._version_known else (False, None)

    def file_store(self):
        return Path(self.paths.agy_file_stores[0]) if self.file else None

    def read(self, name="cli"):
        if self.file_store() is not None:
            return StoreRead.unknown("file_store", AGY_FAULTS["file_store"])
        return self.store(name).read()

    def probe(self, name="cli"):
        return self.store(name).probe()

    # ---- the store's side ----
    def _settle(self):
        if self._pending is not None and self.clock() >= self._pending[0]:
            # A sign-in lands only while agy has none (its sign-in screen shows only then): after a cancel put the
            # earlier one back, it never does.
            at, record, unless = self._pending
            self._pending = None
            if self.record is None and self.fault is None and not (unless is not None and unless()):
                self.record, self.fault, self.changes = record, None, self.changes + 1

    def _read_store(self):
        with self.lock:
            self._settle()
            if self.fault is not None:
                return StoreRead.unknown(self.fault, AGY_FAULTS[self.fault])
            if self.record is None:
                return StoreRead.absent()
            record = copy.deepcopy(self.record)
        email = record.get("id_token_email")
        if not (isinstance(record.get("token"), dict) and record["token"].get("refresh_token")
                and isinstance(email, str) and "@" in email):
            return StoreRead.unknown("incomplete", AGY_FAULTS["incomplete"])
        if record.get("auth_method") != "consumer":
            return StoreRead.unknown("not_consumer", AGY_FAULTS["not_consumer"])
        return StoreRead.ok(record, email, plan=record.get("tier_display_name") or None,
                            tier=record.get("user_tier") or None)

    def _write_store(self, record):
        with self.lock:
            self._settle()
            outcome = self._write_script.pop(0) if self._write_script else "ok"
            if outcome == "fail":
                raise DeckError("The keychain did not take Antigravity's sign-in.", "antigravity")
            self.changes += 1
            if outcome == "garble":
                self.record, self.fault = None, "undecodable"
                raise DeckError("Antigravity's sign-in could not be read back.", "antigravity")
            if isinstance(outcome, dict):
                self.record, self.fault = copy.deepcopy(outcome), None
                raise DeckError("Antigravity's sign-in read back as another account.", "antigravity")
            self.record, self.fault = copy.deepcopy(record), None
            self.writes.append(record.get("id_token_email"))

    def _delete_store(self):
        with self.lock:
            self._settle()
            outcome = self._delete_script.pop(0) if self._delete_script else "ok"
            if outcome == "fail":
                raise DeckError("The keychain did not remove Antigravity's sign-in.", "antigravity")
            existed = self.record is not None or self.fault is not None
            self.record, self.fault, self.changes = None, None, self.changes + 1
            return existed

    def _probe_store(self):
        with self.lock:
            self._settle()
            if self.probe_fails:
                raise DeckError("The keychain did not answer.", "timeout")
            return None if self.record is None and self.fault is None else f"fake-{self.changes}"


class FakeStatusSource(StatusSource):
    """A status page following a script. A payload is a level ("ok", "degraded", "outage"), a
    partial status dict, or an exception (a failed fetch); None before the first entry means ok."""
    SUMMARIES = {"ok": "All Systems Operational", "degraded": "Partially Degraded Service",
                 "outage": "Major Service Outage", "unknown": "Status unknown"}

    def __init__(self, script=(), *, now=time.time, url="https://status.example.test/api/v2/summary.json"):
        super().__init__(url, clock=now)
        self.timeline = Timeline(script, now)

    def _load(self):
        payload = self.timeline.current()
        payload = {"level": payload or "ok"} if payload is None or isinstance(payload, str) else payload
        level = payload.get("level", "ok")
        return {**self._status(level, self.SUMMARIES.get(level, level)), **payload}


def build_fake_home(paths, *, claude_identity, claude_org, codex_identity, cookies=True, menu_bar_enabled=True,
                    sessions=(), codex_exp_offset=9 * 86400, claude_exp_offset=8 * 3600, now=None,
                    password=SAFE_STORAGE_PASSWORD, meta_version=24):
    """Writes a complete synthetic Claude Desktop and Codex installation into a Paths.synthetic tree
    (§11.4) and returns {"paths", "claude", "codex", "codex_exe", "sessions"}.

    Claude (macOS format, docs/mac/PLAN.md §1.6): config.json with the token caches as base64("v10" +
    AES-128-CBC) under PBKDF2(`password`), a Chromium cookie database in the profile folder at
    `meta_version` (unless `cookies` is false), a Code session with its transcript,
    claude_desktop_config.json with two MCP servers, registry rows for `sessions` (dicts with at least
    "pid") and main.log. Codex: auth.json with unsigned JWTs, config.toml, session_index.jsonl, daemon
    settings and `paths.codex_exe` as a synthetic app-server script. The same `password` again keeps
    profiles captured in between restorable."""
    if getattr(paths, "mode", None) != "synthetic" or getattr(paths, "temp_root", None) is None:
        raise SafetyViolation("build_fake_home writes only into Paths.synthetic trees.")
    root = Path(paths.temp_root).resolve()
    targets = [paths.claude_profile, paths.claude_sessions, paths.claude_projects, paths.claude_main_log,
               paths.codex_home, paths.home] + ([paths.codex_exe] if paths.codex_exe else [])
    if not all(Path(target).resolve().is_relative_to(root) for target in targets):
        raise SafetyViolation("A synthetic path points outside its test root.")
    for value in (claude_identity, claude_org):
        uuid.UUID(value)
    now = time.time() if now is None else now
    ms = int(now * 1000)
    profile_dir = Path(paths.claude_profile)
    project = Path(paths.home) / "projects" / "synthetic"

    key = derive_mac_key(password)
    expires_at = now + claude_exp_offset
    cache = {f"acct:{claude_identity}|{CLAUDE_CLIENT_ID}:{claude_org}:https://api.anthropic.com:{CLAUDE_SCOPES}": {
        "token": "synthetic-access-" + secrets.token_hex(12), "refreshToken": "synthetic-refresh-" + secrets.token_hex(12),
        "expiresAt": int(expires_at * 1000)}}
    atomic_write(profile_dir / "config.json", json_bytes({
        "locale": "en-US", "userThemeMode": "system", "lastKnownAccountUuid": claude_identity,
        "oauth:tokenCache": encrypt_v10(key, b"{}"), "oauth:tokenCacheV2": encrypt_v10(key, json_bytes(cache))}))
    for name in DEFAULT_SCOPE.cookie_files:
        (profile_dir / name).unlink(missing_ok=True)
    if cookies:
        write_cookies(profile_dir / DEFAULT_SCOPE.cookie_files[0], key, now, [
            (".claude.ai", "sessionKey", "synthetic-session-" + secrets.token_hex(12)),
            (".claude.ai", "lastActiveOrg", claude_org),
            (".claude.ai", "anthropic-device-id", str(uuid.uuid4())),
            (".claude.ai", "cf_clearance", "synthetic-clearance-" + secrets.token_hex(6)),
            (".example.test", "sessionKey", "synthetic-foreign-cookie")], meta_version=meta_version)

    local, cli = uuid.uuid4(), uuid.uuid4()
    atomic_write(profile_dir / "claude-code-sessions" / claude_identity / claude_org / f"local_{local}.json", json_bytes({
        "sessionId": f"local_{local}", "cliSessionId": str(cli), "cwd": str(project), "originCwd": str(project),
        "createdAt": ms, "lastActivityAt": ms, "model": "claude-sonnet-4-5", "isArchived": False,
        "title": "Synthetic session", "permissionMode": "default"}))
    atomic_write(Path(paths.claude_projects) / "synthetic-project" / f"{cli}.jsonl",
                 json_bytes({"type": "summary", "summary": "Synthetic session"}) + b"\n")
    atomic_write(profile_dir / "claude_desktop_config.json", json_bytes({
        "mcpServers": {"filesystem": {"command": "synthetic-mcp-filesystem", "args": [str(project)]},
                       "github": {"command": "synthetic-mcp-github", "args": [], "env": {"GITHUB_TOKEN": "synthetic"}}},
        "preferences": {"menuBarEnabled": bool(menu_bar_enabled)}}))
    rows = []
    for session in sessions:
        pid = int(session["pid"])
        row = {"status": "idle", "kind": "interactive", "entrypoint": "claude-desktop", "name": f"Session {pid}",
               "cwd": str(project), "updatedAt": ms, "statusUpdatedAt": ms, "procStart": "0",
               "version": REGISTRY_VERSION, **session, "pid": pid}
        target = Path(paths.claude_sessions) / f"{pid}.json"
        atomic_write(target, json_bytes({name: row[name] for name in REGISTRY_KEYS}))
        rows.append(target)
    log = Path(paths.claude_main_log)
    if not log.exists():
        atomic_write(log, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)).encode()
                     + b" [info] Synthetic Claude Desktop log\n")

    codex_home = Path(paths.codex_home)
    codex_email = f"codex-{codex_identity[:8]}@example.test"
    exp = int(now + codex_exp_offset)
    claims = {AUTH_CLAIMS: {"chatgpt_account_id": codex_identity, "chatgpt_plan_type": "plus"}}
    atomic_write(codex_home / "auth.json", json_bytes({
        "OPENAI_API_KEY": None,
        "tokens": {"id_token": unsigned_jwt({**claims, "email": codex_email, "exp": exp}),
                   "access_token": unsigned_jwt({**claims, "exp": exp}),
                   "refresh_token": "synthetic-refresh-" + secrets.token_hex(12), "account_id": codex_identity},
        "last_refresh": _iso(now)}))
    atomic_write(codex_home / "config.toml", b'cli_auth_credentials_store = "file"\n')
    atomic_write(codex_home / "session_index.jsonl", json_bytes(
        {"id": str(uuid.uuid4()), "thread_name": "Synthetic thread", "updated_at": _iso(now)}) + b"\n")
    atomic_write(codex_home / "app-server-daemon" / "settings.json", json_bytes({"shutdownGraceSeconds": 60}))
    codex_exe = _write_codex_stub(Path(paths.codex_exe), paths.python_exe) if paths.codex_exe else None

    return {"paths": paths, "codex_exe": codex_exe, "sessions": rows,
            "claude": {"identity": claude_identity, "org": claude_org, "email": f"claude-{claude_identity[:8]}@example.test",
                       "plan": "max", "profile_dir": profile_dir, "password": password, "key": key,
                       "expires_at": expires_at, "local_session": f"local_{local}", "cli_session": str(cli)},
            "codex": {"identity": codex_identity, "email": codex_email, "plan": "plus", "home": codex_home, "exp": exp}}


STUB_APP_SERVER = r'''"""Synthetic Codex app-server for AccountDeck tests and demos: JSON-RPC over stdio, no network."""
import base64
import json
import os
import secrets
import sys
import time
from pathlib import Path

AUTH = "https://api.openai.com/auth"


def encode(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def claims(token):
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def account_read(params):
    auth = json.loads(Path("auth.json").read_text(encoding="utf-8"))
    tokens = auth["tokens"]
    if params.get("refreshToken") and tokens.get("refresh_token"):
        access = claims(tokens["access_token"])
        access["exp"] = int(time.time()) + 10 * 86400
        tokens["access_token"] = encode({"alg": "none", "typ": "JWT"}) + "." + encode(access) + "."
        tokens["refresh_token"] = "synthetic-refresh-" + secrets.token_hex(8)
        auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        Path("auth.json.tmp").write_text(json.dumps(auth), encoding="utf-8")
        os.replace("auth.json.tmp", "auth.json")
    info = claims(tokens.get("id_token") or tokens["access_token"])
    plan = (info.get(AUTH) or {}).get("chatgpt_plan_type", "plus")
    return {"account": {"type": "chatgpt", "email": info.get("email", ""), "planType": plan},
            "requiresOpenaiAuth": True}


def rate_limits(params):
    now = int(time.time())
    return {"rateLimits": {"limitId": "codex",
                           "primary": {"usedPercent": 12.0, "windowDurationMins": 300, "resetsAt": now + 3600},
                           "secondary": {"usedPercent": 40.0, "windowDurationMins": 10080, "resetsAt": now + 259200}}}


METHODS = {"initialize": lambda params: {"userAgent": "codex-stub/0.0.0"}, "account/read": account_read,
           "account/rateLimits/read": rate_limits,
           "account/usage/read": lambda params: {"summary": {"totalTokens": 1234}}}


def serve():
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue
        handler = METHODS.get(message.get("method"))
        if handler is None:
            reply = {"id": message["id"], "error": {"code": -32601, "message": "Method not found"}}
        else:
            try:
                reply = {"id": message["id"], "result": handler(message.get("params") or {})}
            except Exception as exc:
                reply = {"id": message["id"], "error": {"code": -32603, "message": type(exc).__name__}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    return 0


def main(argv):
    if argv[:2] == ["app-server", "daemon"]:
        return 0
    if argv[:1] == ["app-server"]:
        return serve()
    sys.stderr.write("codex stub: synthetic test binary\n")
    return 2


sys.exit(main(sys.argv[1:]))
'''


def _write_codex_stub(target, python):
    """`paths.codex_exe`: an executable running the synthetic app-server with `python` (isolated). The
    second line is both a shell command and a harmless Python string expression, so the interpreter's
    path may contain spaces (a #! line cannot)."""
    launcher = f'#!/bin/sh\n"exec" "{Path(python)}" "-I" "$0" "$@"\n'
    atomic_write(target, launcher.encode() + STUB_APP_SERVER.encode())
    os.chmod(target, 0o755)
    return target


def encrypt_v10(key, plaintext):
    """macOS Chromium's "v10" AES-128-CBC value as base64 text (how config.json holds the token caches);
    `key` is the 16-byte derive_mac_key(password)."""
    return base64.b64encode(seal_cbc(key, plaintext)).decode()


def write_cookies(path, key, now, cookies, *, meta_version=24):
    """A Chromium cookie database of (host, name, value) rows at `path`; values are macOS "v10"
    AES-128-CBC under the 16-byte `key`, with the sha256(host) prefix from `meta_version` 24, and expire
    30 days after `now`."""
    atomic_write(path, _cookie_database(now, cookies, lambda plaintext: seal_cbc(key, plaintext), meta_version))


def empty_cookie_db(meta_version=24):
    """The bytes of an empty Chromium cookie database at `meta_version`, standing in for the one Claude
    Desktop creates on its first launch (the schema donor of claude_portable.from_portable)."""
    return _cookie_database(0, (), None, meta_version)


def _cookie_database(now, cookies, seal, meta_version):
    stamp = int((now + CHROMIUM_EPOCH) * 1_000_000)
    expires = int((now + 30 * 86400 + CHROMIUM_EPOCH) * 1_000_000)
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(COOKIE_SCHEMA)
        if meta_version != 24:
            db.execute("UPDATE meta SET value = ? WHERE key = 'version'", (str(int(meta_version)),))
        for host, name, value in cookies:
            plaintext = value.encode()
            if meta_version >= 24:
                plaintext = hashlib.sha256(host.encode()).digest() + plaintext
            db.execute("INSERT INTO cookies VALUES (?, ?, '', ?, '', ?, '/', ?, 1, 1, ?, 1, 1, 1, 0, 2, 443, ?)",
                       (stamp, host, name, seal(plaintext), expires, stamp, stamp))
        db.commit()
        data = db.serialize()
    finally:
        db.close()
    return data


def _rotate_codex_auth(path, now):
    auth = json.loads(Path(path).read_bytes())
    tokens = auth["tokens"]
    tokens["access_token"] = unsigned_jwt({**jwt_claims(tokens["access_token"]), "exp": int(now) + ROTATED_LIFETIME})
    tokens["refresh_token"] = "synthetic-refresh-" + secrets.token_hex(8)
    auth["last_refresh"] = _iso(now)
    atomic_write(path, json_bytes(auth))


def _iso(when):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))
