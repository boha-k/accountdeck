"""Shared provider vocabulary: credential sets, endpoints, credential states and readings (PLAN §5.9-5.11)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re

from .. import __version__

AUTH_KEYS = ("oauth:tokenCache", "oauth:tokenCacheV2", "lastKnownAccountUuid")
COOKIE_FILES = ("Cookies", "Cookies-journal", "Cookies-wal", "Cookies-shm")   # inside <profile>\Network
SESSION_COOKIES = ("sessionKey", "sessionKeyV3", "sessionKeyLC", "sessionKeyV3LC")
WEB_COOKIES = SESSION_COOKIES + ("lastActiveOrg", "cf_clearance", "__cf_bm", "anthropic-device-id")
CLAUDE_HOSTS = ("claude.ai", "www.claude.ai")

OAUTH_HOST = "api.anthropic.com"
OAUTH_PATH = "/api/oauth/"
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
OAUTH_BETA = "oauth-2025-04-20"
# Claude's separate read of an account's limit resets: the usage route with the query Claude Code and claude.ai send
# (`cedar_ember=1&skip_spend=1`, RS1 §B1), and the path of the one request that uses one (only through the OAuth route).
RESETS_QUERY = "?cedar_ember=1&skip_spend=1"
RESET_CLAIM_PATH = re.compile(r"/api/organizations/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                              r"/reset_rate_limits")
RESET_CLAIM_URL = "https://api.anthropic.com/api/organizations/{org}/reset_rate_limits"
RESET_CLAIM_TIMEOUT = 25
RESET_PROGRAM = "cedar_ember"       # never `juniper_tide`, Anthropic's other program at the same path
WEB_ORIGIN = "https://claude.ai"
WEB_ORGS_URL = "https://claude.ai/api/organizations"
WEB_PLATFORM = "web_claude_ai"
USER_AGENT = f"AccountDeck2/{__version__} (+local)"
# Claude's resets read and claim go out under Claude Code's name: Anthropic answers any other client of the OAuth
# route `ineligible_reason: "surface"` and lists no grants (tested 2026-11-16: the same token and read, seconds
# apart, got "surface" with USER_AGENT and the account's grants with this one). Every other request keeps USER_AGENT.
# The version is the installed Claude Code's (~/.local/bin/claude links to …/versions/<version>), else this one.
CLAUDE_CODE_VERSION = "2.1.325"
CLAUDE_CODE_AGENT = "claude-cli/{version} (external, cli)"


def is_reset_request(host, path, query):
    """Whether a request is Claude's resets read (the usage route with RESETS_QUERY) or its claim."""
    return host == OAUTH_HOST and ((path == "/api/oauth/usage" and "?" + query == RESETS_QUERY)
                                   or RESET_CLAIM_PATH.fullmatch(path) is not None)


class CredentialState(str, Enum):
    """How an account's usage can be read (§5.10); the value is what the state contract shows."""
    LIVE = "live"                        # active in the official app, which owns the token
    FRESH = "fresh"                      # saved token valid for more than 3 minutes
    EXPIRED = "expired"                  # saved token expired: last-good reading plus the optional cookie route
    WEB_ONLY = "web_only"                # no OAuth entry, but a web sign-in cookie
    REAUTH_REQUIRED = "reauth_required"  # only through the A1 escalation (or a refused Codex refresh)
    ACCESS_EXPIRED = "access_expired"    # Codex: the access token lapsed; the client renews it when it runs (§5.11)
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Reading:
    """One usage read. `usage` None means nothing new: keep the last good reading.

    `outcomes` lists (route, kind, http_class) per request made ("ok" for success), for the
    re-auth escalation and the observed-outage rule. `credential` is a state the adapter asks the
    engine to record (None = unchanged). `meta` holds secret-free diagnostics only."""
    usage: dict | None
    source: str | None = None            # "oauth" | "cookie" | "helper"
    error: Exception | None = None       # a DeckError (usually ProviderError) when nothing was read
    outcomes: tuple = ()
    credential: str | None = None
    checked: float = 0.0
    meta: dict = field(default_factory=dict)
