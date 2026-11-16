"""Provider adapters (§1.3): Claude Desktop, Claude Code (terminal) and Codex, their HTTP and RPC
transports, failure classification and status sources. The engine builds the adapters with make_providers()."""
from __future__ import annotations

from .base import AUTH_KEYS, COOKIE_FILES, CredentialState, Reading
from .claude import DEFAULT_SCOPE, Claude, ProfileScope
from .claude_cli import ClaudeCLI
from .codex import RPC, ActiveIdentity, Codex, TokenState, daemon_settings, refresh_due, token_state
from .errors import OutcomeLog, ProviderError, classify_http, reauth_escalation

__all__ = ["AUTH_KEYS", "COOKIE_FILES", "CredentialState", "Reading", "DEFAULT_SCOPE", "Claude", "ProfileScope",
           "RPC", "ActiveIdentity", "Codex", "TokenState", "daemon_settings", "refresh_due", "token_state",
           "OutcomeLog", "ProviderError", "classify_http", "reauth_escalation", "make_providers"]


def make_providers(paths, http=None, procs=None):
    """{"claude", "codex", "claude_cli"} adapters for one machine layout."""
    return {"claude": Claude(paths, http), "codex": Codex(paths, procs), "claude_cli": ClaudeCLI(paths, http)}
