"""Whether Claude Desktop came back signed in as the account a job restored (PLAN §5.5), and the
secret-free facts the switch manifest and report read from Claude's folder (main.log lines, config.json
key names, token-cache shape, MCP server names).

`Verifier.check()` weighs the signals: Claude's main process alive and stable, config.json naming the
target with a healthy token cache, main.log's login lines, a renewed token cache and, once, the
provider's own answer for the token. A job runs `Verifier.run()` for the hard window; an unconfirmed
switch is handed to `BackgroundVerifier`, which the engine's tick keeps checking.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import time

from . import diagnostics
from .core import DeckError, FileTransaction, read_json, recover_transactions
from .platform import facts_mac
from .providers.errors import ProviderError
from .sessions import valid_uuid

STABLE_SECONDS = 20.0            # how long Claude's main process must stay up before the sign-in counts (V1)
READY_POLL = 0.25
MAX_LOG_READ = 1 << 20
SETTLED_FILES = ("remote-control-state.json", "bridge-state.json", "buddy-tokens.json", "ant-device-registry.json")
OPEN_IT = "Open it from Applications or Spotlight"
NOT_OPENED = f"Claude didn't open by itself. {OPEN_IT}; AccountDeck will verify in the background."
_CACHE_KEY = re.compile(r"^acct:(?P<id>[0-9a-fA-F-]{36})\|(?P<client>[^:|]+):(?P<org>[0-9a-fA-F-]{36}):https://[^:]+:(?P<scopes>.*)$")
_TRANSITION = re.compile(r"Login-state transition \(.*uuid: (?P<old>[^\s)]+) -> (?P<new>[^\s)]+)\)")
_INITIALIZED = re.compile(r"Initialization succeeded - accountId=(?P<id>[0-9a-fA-F-]{36})")
_TOKEN_ENTRIES = re.compile(r"loaded token cache from disk with (?P<n>\d+) entries")
_SERVER_COUNT = re.compile(r"(?:serverCount|total servers|sdk servers)\D{0,6}(?P<n>\d+)", re.IGNORECASE)


# ---- what the manifest and the report read from Claude's folder ----

def log_size(paths) -> int:
    try:
        return Path(paths.claude_main_log).stat().st_size
    except OSError:
        return 0


def log_text(paths, offset) -> str:
    """main.log from `offset` (from the start when the log was rotated meanwhile), at most 1 MB."""
    try:
        with open(paths.claude_main_log, "rb") as handle:
            handle.seek(0, 2)
            handle.seek(offset if handle.tell() >= offset else 0)
            return handle.read(MAX_LOG_READ).decode("utf-8", "replace")
    except OSError:
        return ""


def config_keys(claude) -> list:
    """The names (never values) of config.json's keys."""
    try:
        config = read_json(claude.base / "config.json")
    except (OSError, ValueError):
        return []
    return sorted(str(key) for key in config) if isinstance(config, dict) else []


def files_present(paths) -> dict:
    base = facts_mac.claude_base(paths)[0]
    return {name: (base / name).exists() for name in SETTLED_FILES}


def token_cache_summary(claude, profile, now):
    """{"entries", "scope_names", "client_prefixes", "expires_in"} of a profile's token cache, or None."""
    try:
        cache = claude._cache(profile)
    except (DeckError, AttributeError, KeyError, TypeError):
        return None
    scopes, clients, expiry = set(), set(), None
    for key, entry in cache.items():
        found = _CACHE_KEY.match(key)
        if not found or not isinstance(entry, dict):
            continue
        scopes.update(found["scopes"].split())
        clients.add(found["client"][:8])
        value = entry.get("expiresAt")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            expiry = max(expiry or 0, value / 1000)
    return {"entries": len(cache), "scope_names": sorted(scopes), "client_prefixes": sorted(clients)[:8],
            "expires_in": None if expiry is None else round(expiry - now)}


def cache_org(claude, profile):
    """The organization named by a profile's token-cache keys, or "" when it cannot be read."""
    try:
        cache = claude._cache(profile)
    except (DeckError, AttributeError, KeyError, TypeError):
        return ""
    for key in cache:
        found = _CACHE_KEY.match(key)
        if found and found["id"] == profile.get("identity"):
            return found["org"]
    return ""


def generation_digest(vault, key):
    try:
        found = vault.generations(key)
    except OSError:
        return None
    return found[0]["digest"][:12] if found else None


def mcp_names(paths, claude):
    """The local MCP server names of Claude Chat's and Claude Code's configuration, by file (names only)."""
    found = {}
    for label, path, key in (("chat", claude.base / "claude_desktop_config.json", "mcpServers"),
                             ("code", paths.claude_user_config, "mcpServers")):
        try:
            servers = read_json(path).get(key)
        except (OSError, ValueError, AttributeError):
            servers = None
        found[label] = sorted(str(name)[:80] for name in servers)[:50] if isinstance(servers, dict) else []
    return found


# ---- verification ----

@dataclass(frozen=True)
class Verdict:
    state: str          # "verified" | "pending" | "failed"
    code: str           # a short, secret-free reason for the manifest
    message: str = ""   # what the report shows


class Verifier:
    """Checks that Claude came back signed in as the target. `check()` is one evaluation of the signals;
    `run()` repeats it for the hard window and `BackgroundVerifier` keeps going afterwards."""

    def __init__(self, ctx, *, identity, label, restored_cipher, log_offset, manifest=None):
        self.ctx, self.identity, self.label = ctx, valid_uuid(identity), label
        self.restored_cipher, self.offset, self.manifest = restored_cipher, log_offset, manifest
        self.stable = min(STABLE_SECONDS, ctx.timings.verify_hard / 3)
        self.cipher_changed = self.marker_seen = self.identity_ok = False
        self._main, self._asked, self._answer, self._seen = None, 0.0, None, set()

    def signal(self, name, value):
        """Records each distinct signal once, with its time; values are words, never identifiers."""
        if (name, value) in self._seen:
            return
        self._seen.add((name, value))
        diagnostics.log("verify.signal", signal=name, value=value)
        if self.manifest is not None:
            self.manifest.append("verification.signals", {"signal": name, "at": time.time(), "value": value})

    def wait_ready(self, op):
        """Waits up to `timings.ready` for Claude's main process and (advisory) its sign-in log lines.
        Returns (opened, token cache entries Claude loaded or None); missing log lines never fail."""
        deadline = time.monotonic() + self.ctx.timings.ready
        while True:
            main = self.ctx.procs.claude_desktop_main()
            text = log_text(self.ctx.paths, self.offset)
            loaded = _TOKEN_ENTRIES.search(text)
            if main is not None and loaded and "Account details received via IPC" in text:
                return True, int(loaded["n"])
            if time.monotonic() >= deadline:
                return main is not None, int(loaded["n"]) if loaded else None
            op.sleep(READY_POLL)

    def run(self, op) -> Verdict:
        """The hard window: evaluates every few seconds until verified, failed or `verify_hard` has passed."""
        timings = self.ctx.timings
        deadline = time.monotonic() + timings.verify_hard
        poll = min(2.0, timings.verify_hard / 6)
        while True:
            verdict = self.check()
            if verdict.state != "pending" or time.monotonic() >= deadline:
                return verdict
            op.sleep(min(poll, max(0.0, deadline - time.monotonic())))

    def check(self) -> Verdict:
        ctx = self.ctx
        main = ctx.procs.claude_desktop_main()
        stable = self._track(main)
        facts = self._read_log()
        live = self._live()
        found = valid_uuid(live.get("identity")) if live else None
        signed_in = found is not None and found == self.identity
        healthy = signed_in and ctx.claude.diagnose(live)[0] == "ok"
        if healthy:
            self.signal("config", "target")
        if signed_in and live["auth"].get("oauth:tokenCacheV2") not in (None, self.restored_cipher):
            self.cipher_changed = True
            self.signal("token_cache", "renewed")
        if facts["success"]:
            self.marker_seen = True
            self.signal("main_log", "initialized")
        hard = facts["hard"]
        if hard is None and found is not None and not signed_in and main is not None:
            hard = "other_account"
        if hard is None and healthy and not self.identity_ok:
            hard = self._ask_profile(live)
        if hard is not None:
            self.signal("failure", hard)
            return Verdict("failed", hard, self._failure_text(hard))
        if healthy and stable and (self.identity_ok or self.marker_seen or self.cipher_changed):
            return Verdict("verified", "verified")
        return self._pending(main, healthy)

    def _track(self, main):
        if main is None:
            self._main = None
            return False
        key = (main.pid, main.ctime)
        if self._main is None or self._main[0] != key:
            self._main = (key, time.monotonic())
            self.signal("main", "alive")
        stable = time.monotonic() - self._main[1] >= self.stable
        if stable:
            self.signal("main", "stable")
        return stable

    def _live(self):
        try:
            return self.ctx.claude.capture(config_only=True)
        except DeckError:
            return None

    def _read_log(self):
        facts = {"hard": None, "success": False}
        for line in log_text(self.ctx.paths, self.offset).splitlines():
            moved = _TRANSITION.search(line)
            if moved and valid_uuid(moved["new"]) not in (None, self.identity):
                facts["hard"] = "transition"
            if "oauth: sessionKey names a different account than the flow; not minting" in line:
                facts["hard"] = "other_account"
            if "[account] Navigated to /logout" in line:
                facts["hard"] = "logout"
            started = _INITIALIZED.search(line)
            if started and valid_uuid(started["id"]) == self.identity:
                facts["success"] = True
            elif started:
                facts["hard"] = "other_account"
        return facts

    def _ask_profile(self, live):
        """V4: the provider names the account behind Claude's token. Returns a hard failure code or None."""
        now = time.monotonic()
        if now < self._asked:
            return None
        self._asked = now + max(self.ctx.timings.verify_hard / 3, 1.0)
        if self.ctx.engine.status("claude")["level"] in ("degraded", "outage"):
            self._answer = "incident"
            return None
        try:
            self.ctx.claude.identity(live)
        except ProviderError as exc:
            if exc.kind == "token_refused":
                return "refused"
            self._answer = "unavailable"
        except DeckError as exc:
            if exc.code == "identity":
                return "other_account"
            self._answer = "expired"
        except (OSError, ValueError):
            self._answer = "unavailable"
        else:
            self.identity_ok, self._answer = True, "ok"
            self.signal("profile", "target")
        return None

    def _failure_text(self, code):
        if code == "logout":
            return f"Claude signed {self.label} out right after opening it."
        if code == "refused":
            return f"Claude refused {self.label}'s saved sign-in."
        return f"Claude opened a different account than {self.label}."

    def _pending(self, main, healthy) -> Verdict:
        if main is None:
            return Verdict("pending", "not_open", NOT_OPENED)
        if not healthy:
            return Verdict("pending", "signing_in", "Claude hasn't finished signing in yet. AccountDeck will keep checking.")
        if self._answer == "expired":
            return Verdict("pending", "token_expired",
                           f"{self.label}'s saved token has expired; Claude will renew it. Still checking.")
        if self._answer == "incident":
            return Verdict("pending", "incident",
                           "Claude is reporting problems, so the sign-in can't be confirmed yet. Still checking.")
        return Verdict("pending", "unconfirmed",
                       "Claude opened, but the sign-in couldn't be confirmed yet. Still checking.")


class BackgroundVerifier:
    """Keeps checking an unconfirmed switch (every `verify_soft_every` seconds, `verify_soft_count` times).
    The engine ticks it through `engine.background_verifiers`; `tick` returns True once it is done."""

    def __init__(self, ctx, verifier, *, op_id, account, label, summary, tx_key, manifest, started):
        self.ctx, self.verifier, self.op_id, self.account, self.label = ctx, verifier, op_id, account, label
        self.summary, self.tx_key, self.manifest = summary, tx_key, manifest
        every, count = ctx.timings.verify_soft_every, ctx.timings.verify_soft_count
        self.every, self.next, self.deadline = every, started + every, started + every * count

    def request(self):
        """The `verify` action: check again at the next tick."""
        self.next = 0.0

    def tick(self, now) -> bool:
        engine = self.ctx.engine
        if engine.runner.busy:            # another job is changing the files this verifier watches
            return True
        if now < self.next:
            return False
        self.next = now + self.every
        verdict = self.verifier.check()
        if verdict.state == "verified":
            self._finish("verified", verdict)
            engine.runner.update_report(self.op_id, verified="verified", verified_at=now, verify_reason=None,
                                        summary=self.summary)
            engine.notice(f"Claude is signed in as {self.label}.", "good", sticky=False)
            return True
        if verdict.state == "failed":
            if engine.runner.busy:
                return True
            self._finish("failed", verdict)
            engine.runner.update_report(
                self.op_id, verified="failed", verify_reason=verdict.message,
                summary=verdict.message + " Recovery is available.", outcome="failed",
                error={"code": "identity", "message": verdict.message, "step": "verify"})
            self._arm_recovery()
            engine.notice(verdict.message + " Recovery is available.", "critical")
            return True
        engine.runner.update_report(self.op_id, verify_reason=verdict.message)
        if now >= self.deadline:
            self._finish("unconfirmed", verdict)
            engine.notice("Couldn't confirm the sign-in yet. Open Claude once and refresh.", "warning")
            return True
        return False

    def _finish(self, result, verdict):
        self.manifest.set("verification.result", result)
        self.manifest.set("verification.reason", verdict.code)
        self.manifest.finish()

    def _arm_recovery(self):
        """A switch that opened the wrong account stays undoable: its transaction is pending again."""
        engine = self.ctx.engine
        try:
            tx = FileTransaction.load(self.ctx.vault, engine.roots(), self.tx_key)
            tx.doc["status"] = "verifying"
            tx.save()
            engine.recovery = recover_transactions(self.ctx.vault)
        except DeckError:
            engine.recovery = ["unreadable"]
        engine.bump()
