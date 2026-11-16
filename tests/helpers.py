"""Shared test scaffolding: a synthetic Mac home under a temporary folder and an engine on fake providers.

Importing this module turns safe mode on before any accountdeck module loads, so nothing a test does can
reach a real process, file, Keychain item, port or provider. Every path lives under a registered
temporary test root (config.Paths.synthetic).
"""
from __future__ import annotations

import copy
import os

os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"
for _name in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):   # would make terminal mode read-only
    os.environ.pop(_name, None)

from pathlib import Path  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
import uuid  # noqa: E402

from accountdeck.config import Paths, Timings  # noqa: E402
from accountdeck.core import Vault, atomic_write, json_bytes  # noqa: E402
from accountdeck.engine import Engine  # noqa: E402
from accountdeck.procs import FakeProcessController  # noqa: E402
from accountdeck.providers.claude_cli import ClaudeCLI, profile as cli_profile  # noqa: E402
from accountdeck.providers.codex import AUTH_CLAIMS  # noqa: E402
from accountdeck.providers.claude import claim_reset  # noqa: E402
from accountdeck.providers.fake import (FakeAntigravity, FakeClaude, FakeCodex, ResetLedger, build_fake_home,  # noqa: E402
                                        test_cipher, unsigned_jwt)
from accountdeck.usage import claude_usage  # noqa: E402

# Short waits so a switch job runs in well under a second.
FAST = Timings(idle_hold=0.05, idle_poll=0.01, graceful_wait=2, graceful_progress=0.05, terminate_wait=1,
               helper_self_exit=0.2, settle_file=0.5, settle_grace=0.01, ready=0.3, verify_hard=0.6,
               verify_soft_every=0.2, verify_soft_count=2, lock_wait=2, launch_retry=(0.01,))
CLAUDE_USAGE = {"five_hour": {"utilization": 12.0, "resets_at": None},
                "seven_day": {"utilization": 30.0, "resets_at": "2099-01-01T00:00:00+00:00"}}
CODEX_RATES = {"rateLimits": {"limitId": "codex",
                              "primary": {"usedPercent": 10.0, "windowDurationMins": 300, "resetsAt": None},
                              "secondary": {"usedPercent": 20.0, "windowDurationMins": 10080, "resetsAt": None}}}


def new_uuid():
    return str(uuid.uuid4())


def codex_auth(identity, *, exp, refresh="synthetic-refresh-token", email="codex@example.test"):
    """A Codex auth.json dict with unsigned JWTs for `identity`, its access token expiring at `exp`."""
    claims = {AUTH_CLAIMS: {"chatgpt_account_id": identity, "chatgpt_plan_type": "plus"}}
    return {"OPENAI_API_KEY": None, "last_refresh": "2026-10-01T00:00:00Z",
            "tokens": {"id_token": unsigned_jwt({**claims, "email": email, "exp": int(exp)}),
                       "access_token": unsigned_jwt({**claims, "exp": int(exp)}),
                       "refresh_token": refresh, "account_id": identity}}


def terminal_login(identity, email, *, now=None, token="t"):
    """(credentials, oauthAccount) of a synthetic Claude Code login; `token` makes its tokens distinct."""
    now = time.time() if now is None else now
    credentials = {"claudeAiOauth": {"accessToken": f"access-{token}", "refreshToken": f"refresh-{token}",
                                     "expiresAt": int((now + 8 * 3600) * 1000), "subscriptionType": "max",
                                     "scopes": ["user:profile", "user:inference"]}}
    account = {"accountUuid": identity, "emailAddress": email, "organizationUuid": new_uuid()}
    return credentials, account


class FakeTerminal(ClaudeCLI):
    """Claude Code's adapter with the provider answers scripted: whose token it is, usage, renewal."""

    def __init__(self, paths, owners=None, *, ledger=None, **kwargs):
        super().__init__(paths, **kwargs)
        self.owners = owners if owners is not None else {}      # refresh token -> accountUuid
        self.ledger = ledger if ledger is not None else ResetLedger(self.clock)
        self.claims = []

    def _owner(self, credentials):
        return self.owners.get(credentials["claudeAiOauth"]["refreshToken"])

    def read_resets(self, credentials):
        self.reset_token(credentials)               # the real check: valid long enough, never renewed
        return self.ledger.claude_answer(self._owner(credentials))

    def claim_reset(self, credentials, org, grant_id, request_id):
        identity = self._owner(credentials)

        def answer(method, url, *, json_body=None, **_):
            self.claims.append({"method": method, "url": url, "body": copy.deepcopy(json_body)})
            return self.ledger.claude_claim(identity, json_body)
        return claim_reset(answer, self.reset_token(credentials), org, grant_id, request_id)

    def resolve_account(self, credentials, *, timeout=None):
        return {"accountUuid": self.owners.get(credentials["claudeAiOauth"]["refreshToken"]),
                "emailAddress": "", "organizationUuid": ""}

    def usage(self, credentials):
        return claude_usage(CLAUDE_USAGE, now=self.clock())

    def refresh(self, credentials, *, timeout=None):
        raise AssertionError("a test login must never be renewed")


class SyntheticTestCase(unittest.TestCase):
    """A fresh synthetic Mac home per test (`self.paths`)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ad2-test-")
        self.addCleanup(temp.cleanup)
        self.paths = Paths.synthetic(Path(temp.name) / "root")

    def make_engine(self, *, claude=None, codex=None, procs=None, owners=None, vault=None, **kwargs):
        """An engine on fake providers that does not tick or read by itself (`engine.tick()` does both). agy is a
        FakeAntigravity (not installed, nobody signed in) unless `antigravity=` names another."""
        kwargs.setdefault("antigravity", FakeAntigravity(self.paths))
        self.terminal = FakeTerminal(self.paths, owners)
        providers = {"claude": FakeClaude(self.paths, [(0, CLAUDE_USAGE)]),
                     "codex": FakeCodex(self.paths, [(0, CODEX_RATES)]), "claude_cli": self.terminal}
        providers.update({name: value for name, value in (("claude", claude), ("codex", codex)) if value})
        engine = Engine(self.paths, providers=providers, procs=procs or FakeProcessController(self.paths, timings=FAST),
                        vault=vault or Vault(self.paths.data_dir / "vault", protect=test_cipher), status_sources={},
                        timings=FAST, start=False, inline=True, **kwargs)
        self.addCleanup(engine.stop)
        return engine

    def fake_home(self, **kwargs):
        """Signs Claude Desktop and Codex in to fresh synthetic accounts (see providers.fake.build_fake_home)."""
        kwargs.setdefault("claude_identity", new_uuid())
        kwargs.setdefault("claude_org", new_uuid())
        kwargs.setdefault("codex_identity", "acct-" + uuid.uuid4().hex[:12])
        return build_fake_home(self.paths, **kwargs)

    def sign_in_terminal(self, credentials, account, *, shared=None):
        """Writes Claude Code's live login (credentials file + ~/.claude.json oauthAccount)."""
        atomic_write(self.paths.claude_cli_credentials, json_bytes({**credentials, **(shared or {})}))
        config = {"projects": {"/Users/me/work": {"allowedTools": []}}, "oauthAccount": copy.deepcopy(account)}
        atomic_write(self.paths.claude_user_config, json_bytes(config))

    def save_terminal(self, engine, credentials, account):
        """Saves a terminal login as an account whose ownership the provider confirmed; returns its id."""
        record = cli_profile(credentials, account, engine.clock())
        record["verified"] = record["fingerprint"]
        return engine.cli.save(record, source="test")
