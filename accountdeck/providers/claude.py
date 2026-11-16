"""Claude Desktop adapter (§5.7-5.10, A1): profile capture and restore, health, credential tiers and usage reads.

A profile is {"version": 1, "identity", "auth": {AUTH_KEYS}, "key": the crypto's key_ref()
(chromium_crypto.MAC_KEY_REF), "cookies": base64 SQLite snapshot or None, "captured", "journal":
{"present", "consistent", "used"}, "cookie_heal": {"hot_journal", "healed"} (full captures), "files":
{rel: base64 or None} (only with ProfileScope.extra_files)}; the engine adds "org" at enrolment. The
token caches and cookie values are encrypted with Claude's "Claude Safe Storage" key behind
`self.crypto` (chromium_crypto.ChromiumCryptoMac). Nothing here stops or checks processes: the engine
calls restore() and clear_for_login() only once Claude is closed.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid

from cryptography.exceptions import InvalidTag

from .. import diagnostics
from ..usage import claude_plan, claude_usage, has_weekly
from ..config import SafetyViolation
from ..core import DeckError, json_bytes, read_json
from . import http as http_module
from .base import (AUTH_KEYS, CLAUDE_HOSTS, COOKIE_FILES, OAUTH_PROFILE_URL, OAUTH_USAGE_URL, RESET_CLAIM_TIMEOUT,
                   RESET_CLAIM_URL, RESET_PROGRAM, RESETS_QUERY, SESSION_COOKIES, WEB_COOKIES, WEB_ORGS_URL, WEB_ORIGIN,
                   WEB_PLATFORM, CredentialState, Reading)
from .chromium_crypto import ChromiumCryptoMac, ChromiumKeyNotReady
from .errors import ProviderError, no_weekly, reauth_escalation

FRESH_MARGIN = 180             # a saved token counts as fresh while more than 3 minutes remain
RESET_TOKEN_MARGIN = 120       # a limit resets read or a claim needs the token this long: it is never renewed for one
RESETS_TIMEOUT = 10            # a limit resets read waits this long (Claude Code: 5 s a try)
COOKIE_ROUTE_INTERVAL = 1800   # at most one cookie-route attempt per account per 30 minutes
CHROMIUM_EPOCH = 11644473600   # seconds from 1601-01-01 (Chromium cookie times) to 1970-01-01
SQLITE_MAGIC = b"SQLite format 3\x00"
JOURNAL = "config.json.journal"
_FALLBACK_KINDS = ("token_refused", "token_expired", "scope_missing")


@dataclass(frozen=True)
class ProfileScope:
    """What a Claude profile swap covers (§5.9); DEFAULT_SCOPE is v1's swap set (A14)."""
    auth_keys: tuple[str, ...] = AUTH_KEYS
    cookie_files: tuple[str, ...] = COOKIE_FILES
    extra_files: tuple[str, ...] = ()        # relative paths swapped as opaque bytes
    delete_on_switch: tuple[str, ...] = ()   # relative paths deleted inside the transaction


DEFAULT_SCOPE = ProfileScope()


class Claude:
    provider = "claude"

    def __init__(self, paths, http=None, *, crypto=None, scope=DEFAULT_SCOPE, clock=time.time):
        """`crypto` is the ChromiumCrypto to use: by default this Mac's (the login keychain's "Claude Safe
        Storage" key); tests and the demo pass one with a synthetic password."""
        self.paths = paths
        self.http = http or http_module.request
        self.crypto = crypto or ChromiumCryptoMac()
        self.scope = scope
        self.clock = clock
        self._cookie_attempts = {}
        self._lock = threading.Lock()

    @property
    def base(self):
        return Path(self.paths.claude_profile)

    # ---- reading the signed-in profile ----

    def journal_state(self, path=None):
        """§5.8: {"present", "consistent", "body"}. The journal body (a dict) is trusted only when
        its digest matches the header and the header's base digest matches the current
        config.json, or config.json already holds that body. Anything else, including a header
        that does not parse, means config.json may predate Claude's last write."""
        path = Path(path) if path is not None else self.base / JOURNAL
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {"present": False, "consistent": True, "body": None}
        stale = {"present": True, "consistent": False, "body": None}
        head, newline, body = raw.partition(b"\n")
        fields = head.decode("ascii", "replace").split()
        if not newline or len(fields) != 3:
            return stale
        body_digest, base_digest = fields[0], fields[1]
        if body_digest not in _digest_forms(body):
            return stale
        try:
            current = path.with_name(path.name.removesuffix(".journal")).read_bytes()
        except FileNotFoundError:
            current = None
        if current is None and base_digest != "-":
            return stale
        if current is not None and base_digest not in _digest_forms(current) and body_digest not in _digest_forms(current):
            return stale
        try:
            parsed = json.loads(body.decode("utf-8-sig"))
        except ValueError:
            return stale
        return {"present": True, "consistent": True, "body": parsed} if isinstance(parsed, dict) else stale

    def capture(self, config_only=False, heal_copy_dir=None):
        """Reads the signed-in profile without changing any Claude file. `config_only` skips the
        cookie database (safe while Claude runs). With `heal_copy_dir`, a cookie database left with
        a hot journal is read from a rolled-back copy instead of failing with busy."""
        config, journal = self._config()
        identity = config.get("lastKnownAccountUuid")
        profile = {"version": 1, "identity": identity,
                   "auth": {key: config[key] for key in self.scope.auth_keys if key in config},
                   "key": self.crypto.key_ref(), "cookies": None, "captured": self.clock(),
                   "journal": {"present": journal["present"], "consistent": journal["consistent"],
                               "used": journal["body"] is not None}}
        if self.scope.extra_files:
            profile["files"] = {rel: self._read_extra(rel) for rel in self.scope.extra_files}
        if not config_only:
            profile["cookies"], profile["cookie_heal"] = self._capture_cookies(heal_copy_dir)
            if self._config()[0].get("lastKnownAccountUuid") != identity:
                raise DeckError("The Claude account changed while saving. Try again.", "identity")
        return profile

    def current(self, cookies=True):
        """capture() of a signed-in Desktop; login_needed when no account is signed in."""
        profile = self.capture(config_only=not cookies)
        if _uuid(profile["identity"]) is None:
            raise DeckError("Sign in to Claude Desktop first.", "login_needed")
        return profile

    def active_identity(self):
        """The account Claude Desktop is signed in to (journal-aware), or None."""
        try:
            config, _ = self._config()
        except DeckError:
            return None
        return _uuid(config.get("lastKnownAccountUuid"))

    def heal_cookie_copy(self, src_dir, work_dir):
        """A snapshot of a cookie database whose rollback journal is hot (Claude stopped mid-write).
        The database and its sidecars are copied into a fresh folder under `work_dir` and opened
        read-write there, so SQLite rolls the copy back; the originals are only read."""
        src_dir, work_dir = Path(src_dir).resolve(), Path(work_dir).resolve()
        if work_dir.is_relative_to(src_dir) or work_dir.is_relative_to(self.base.resolve()):
            raise DeckError("The cookie repair folder must be outside Claude's data folder.", "safety")
        work_dir.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="cookies-", dir=work_dir))
        try:
            for name in self.scope.cookie_files:
                if (src_dir / name).is_file():
                    shutil.copyfile(src_dir / name, scratch / name)
            return _snapshot(scratch / self.scope.cookie_files[0], read_only=False)
        except (OSError, sqlite3.Error):
            raise DeckError("Claude's cookie database could not be read. Quit Claude, then save the account again.",
                            "busy") from None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    # ---- health and credentials ----

    def diagnose(self, profile):
        """(health, reason) of a captured profile; see health()."""
        journal = profile.get("journal") or {}
        if journal.get("present") and not journal.get("consistent"):
            return "stale_after_kill", "Claude's settings journal did not match; this capture may predate its last write."
        auth = profile.get("auth") or {}
        identity = _uuid(auth.get("lastKnownAccountUuid"))
        if identity is None:
            return "logged_out", "No Claude account is signed in."
        if profile.get("identity") != auth.get("lastKnownAccountUuid"):
            return "identity_mismatch", "The profile's account does not match its sign-in data."
        if not auth.get("oauth:tokenCacheV2"):
            return "half_login", "Claude has not stored this account's tokens yet."
        try:
            entries = _entries(self._cache(profile), auth["lastKnownAccountUuid"])
        except ChromiumKeyNotReady as exc:      # macOS: the Keychain key is not available yet
            return "unreadable", str(exc)
        except DeckError:
            return "unreadable", "The saved tokens could not be decrypted."
        if not entries:
            return "half_login", "Claude has not stored tokens for this account yet."
        if all(_is_sentinel(entry) for _, entry in entries):
            return "sentinel", "Claude stored an empty placeholder instead of tokens."
        if profile.get("cookies"):
            try:
                signed_in = _has_session_cookie(profile["cookies"])
            except (ValueError, sqlite3.Error):
                return "unreadable", "The saved cookie database could not be read."
            if not signed_in:
                return "half_login", "Claude's web sign-in cookie is missing."
        return "ok", None

    def health(self, profile):
        """"ok" | "half_login" | "logged_out" | "identity_mismatch" | "unreadable" | "stale_after_kill" | "sentinel"."""
        return self.diagnose(profile)[0]

    def token_entry(self, profile):
        """The saved OAuth entry v1 reads usage with (a user:profile token of this account with the
        latest expiry), or None. DeckError when the token cache cannot be decrypted."""
        if not (profile.get("auth") or {}).get("oauth:tokenCacheV2"):
            return None
        usable = [entry for key, entry in _entries(self._cache(profile), profile.get("identity"))
                  if "user:profile" in key and entry.get("token")]
        return max(usable, key=_expires_at) if usable else None

    def token(self, profile, allow_expired=False):
        entry = self.token_entry(profile)
        if entry is None:
            raise DeckError("Claude has no usage token yet. Open the account in Desktop and try again.", "login_needed")
        if not allow_expired and _expires_at(entry) < self.clock():
            raise DeckError("Open this Claude account once to renew its usage access.", "login_needed")
        return entry["token"]

    def credential_state(self, profile, *, active, now=None, outcomes=None, incident=False):
        """(CredentialState, {"expires_in", "reason"}) of a profile (§5.10). `outcomes` is the
        account's OutcomeLog; `expires_in` is the saved token's remaining lifetime in seconds."""
        now = self.clock() if now is None else now
        info = {"expires_in": None, "reason": None}
        try:
            entry = self.token_entry(profile)
        except DeckError:
            entry, info["reason"] = None, "unreadable"
        if entry is not None:
            info["expires_in"] = round(_expires_at(entry) - now)
        if active:
            return CredentialState.LIVE, info
        if outcomes is not None and reauth_escalation(outcomes, now, incident=incident):
            return CredentialState.REAUTH_REQUIRED, {**info, "reason": "token_refused"}
        if entry is not None:
            return (CredentialState.FRESH if info["expires_in"] > FRESH_MARGIN else CredentialState.EXPIRED), info
        if info["reason"] is None and _has_session_cookie_quietly(profile.get("cookies")):
            return CredentialState.WEB_ONLY, {**info, "reason": "no_token"}
        return CredentialState.UNKNOWN, {**info, "reason": info["reason"] or "no_token"}

    def sessionkey_expiry(self, profile):
        """Unix time when the saved claude.ai session cookie expires (dormancy hint), or None."""
        if not profile.get("cookies"):
            return None
        try:
            db = _cookie_db(profile["cookies"])
            try:
                rows = db.execute(f"SELECT host_key, expires_utc, has_expires FROM cookies WHERE name IN "
                                  f"({','.join('?' * len(SESSION_COOKIES))})", SESSION_COOKIES).fetchall()
            finally:
                db.close()
        except (ValueError, sqlite3.Error):
            return None
        times = [expires / 1e6 - CHROMIUM_EPOCH for host, expires, has_expires in rows
                 if str(host).lstrip(".") in CLAUDE_HOSTS and has_expires and isinstance(expires, int) and expires > 0]
        return max(times) if times else None

    # ---- usage ----

    def read_usage(self, profile, state, *, now=None, last_good=None, cookie_route=True, incident=False,
                   account=None):
        """One usage read in the tiered order of §5.10. Provider failures come back in the Reading.

        live/fresh: the saved token on /api/oauth/usage, unless it has already expired.
        expired/web_only, an expired live token, or an OAuth refusal: the cookie route when
        `cookie_route` allows it, at most once per account per 30 minutes. reauth_required and
        unknown: no request. A hollow answer never replaces `last_good` while its reset is ahead, and
        an answer without the weekly limit is a failed read (S3a)."""
        now = self.clock() if now is None else now
        state = CredentialState(state)
        meta, outcomes = {"tier": state.value}, []
        usage = source = error = None
        try:
            entry = self.token_entry(profile)
        except DeckError as exc:
            entry, entry_error = None, exc
        else:
            entry_error = None
        if entry is not None:
            meta["expires_in"] = round(_expires_at(entry) - now)
        try_cookie = state in (CredentialState.EXPIRED, CredentialState.WEB_ONLY)
        if state in (CredentialState.LIVE, CredentialState.FRESH):
            if entry is not None and meta["expires_in"] > 0:
                try:
                    usage = _reading(self._read_oauth(profile, entry["token"], incident=incident), now, "oauth")
                    source = "oauth"
                    outcomes.append(("oauth", "ok", "2xx"))
                except ProviderError as exc:
                    error = exc
                    outcomes.append(("oauth", exc.kind, exc.http_class))
                    try_cookie = exc.kind in _FALLBACK_KINDS
            elif entry_error is not None:
                error = entry_error
            else:
                meta["reason"] = "token_expired" if entry is not None else "no_token"
                try_cookie = True
        if usage is None and try_cookie:
            skip = self._cookie_skip(profile, cookie_route, now)
            if skip:
                meta["cookie"] = skip
            else:
                try:
                    usage, source = _reading(self._read_cookie(profile, incident=incident), now, "cookie"), "cookie"
                    outcomes.append(("cookie", "ok", "2xx"))
                except ProviderError as exc:
                    error = error or exc
                    outcomes.append(("cookie", exc.kind, exc.http_class))
                except SafetyViolation:
                    raise
                except DeckError as exc:
                    error = error or exc
        if usage is not None and keeps_last_good(usage, last_good, now):
            usage, meta["hollow"] = None, True
        failure = None if usage is not None or meta.get("hollow") else error
        diagnostics.log("claude.read", account=diagnostics.account_ref(account or profile.get("identity")),
                        tier=state.value, expires_in=meta.get("expires_in"), source=source,
                        outcomes=[f"{route}:{kind}" for route, kind, _ in outcomes], hollow=meta.get("hollow"),
                        detail=getattr(failure, "detail", None))
        return Reading(usage=usage, source=source, error=failure, outcomes=tuple(outcomes), checked=now, meta=meta)

    def identity(self, profile):
        """The account behind a saved profile, confirmed by the provider: {"identity", "email", "plan", "org"}."""
        data, _ = self.http("GET", OAUTH_PROFILE_URL, token=self.token(profile))
        data = data if isinstance(data, dict) else {}
        account, org = data.get("account") or {}, data.get("organization") or {}
        if account.get("uuid") != profile.get("identity"):
            raise DeckError("Claude returned a different account. Switch cancelled.", "identity")
        plan = claude_plan(org.get("plan_display_name"), org.get("rate_limit_tier"), org.get("organization_type"))
        if not plan:
            plan = "max" if account.get("has_claude_max") else "pro" if account.get("has_claude_pro") else "unknown"
        return {"identity": profile["identity"], "email": account.get("email", ""), "plan": plan,
                "org": org.get("uuid", "")}

    def cookie_header(self, profile):
        """The Cookie header for the claude.ai route: the WEB_COOKIES set for claude.ai, decrypted."""
        if not profile.get("cookies"):
            raise DeckError("Quit Claude once to save its complete sign-in.", "login_needed")
        key = self._key(profile)
        jar = {}
        try:
            for host, name, plain, encrypted in _claude_rows(profile["cookies"], WEB_COOKIES):
                value = plain
                if encrypted:
                    blob = bytes(encrypted)
                    if blob[:3] not in key.prefixes:        # Chromium's "v10" framing
                        continue
                    raw = key.decrypt(blob)
                    host_hash = hashlib.sha256(host.encode()).digest()
                    if raw.startswith(host_hash):
                        raw = raw[32:]
                    value = raw.decode("utf-8")
                if value and not any(ch in value for ch in "\r\n;"):
                    jar[name] = value
        except (ValueError, TypeError, sqlite3.Error, InvalidTag):
            raise DeckError("Saved Claude cookies could not be read. Reconnect this account.", "login_needed") from None
        if not any(jar.get(name) for name in SESSION_COOKIES):
            raise DeckError("Claude's saved web sign-in has expired. Reconnect this account.", "login_needed")
        return "; ".join(f"{name}={value}" for name, value in jar.items())

    def _read_oauth(self, profile, token, *, incident):
        data, meta = self.http("GET", OAUTH_USAGE_URL, token=token, incident=incident)
        return _json_object(data, meta)

    def _read_cookie(self, profile, *, incident):
        org = profile.get("org")
        cookie = self.cookie_header(profile)
        headers = {"Referer": WEB_ORIGIN, "Origin": WEB_ORIGIN, "anthropic-client-platform": WEB_PLATFORM}
        orgs, _ = self.http("GET", WEB_ORGS_URL, headers=headers, cookie=cookie, incident=incident)
        # A saved cookie can outlive the short-lived OAuth token (ai-multi-instance); read only the
        # exact enrolled organization, never another org's first result.
        if not isinstance(orgs, list) or not any(isinstance(item, dict) and item.get("uuid", item.get("id")) == org
                                                 for item in orgs):
            raise DeckError("The saved Claude web login does not match this account's organization.", "identity")
        data, meta = self.http("GET", f"{WEB_ORGS_URL}/{org}/usage", headers=headers, cookie=cookie,
                               incident=incident)
        return _json_object(data, meta)

    # ---- limit resets (resets.py) ----
    def read_resets(self, profile, route):
        """The answer of one limit resets read on `route`, the one the account's ordinary read just used: "oauth"
        (the saved token, never renewed for this) or "cookie" (the enrolled organisation's usage on claude.ai, with the
        ordinary read's headers). Raises ProviderError, or DeckError when the route cannot be used now."""
        if route == "oauth":
            token = self.reset_token(profile)
            data, meta = self.http("GET", OAUTH_USAGE_URL + RESETS_QUERY, token=token, timeout=RESETS_TIMEOUT)
        elif route == "cookie":
            org = _uuid(profile.get("org"))
            if org is None:
                raise DeckError("This account's organization is not known.", "identity")
            headers = {"Referer": WEB_ORIGIN, "Origin": WEB_ORIGIN, "anthropic-client-platform": WEB_PLATFORM}
            data, meta = self.http("GET", f"{WEB_ORGS_URL}/{org}/usage{RESETS_QUERY}", headers=headers,
                                   cookie=self.cookie_header(profile), timeout=RESETS_TIMEOUT)
        else:
            raise ValueError(f"unknown route: {route}")
        return _resets_answer(data, meta)

    def reset_token(self, profile):
        """The saved OAuth token a limit resets read or a claim uses: valid for RESET_TOKEN_MARGIN more at least."""
        entry = self.token_entry(profile)
        if entry is None:
            raise DeckError("No saved sign-in token of this account can use a limit reset here. Use it on claude.ai.",
                            "login_needed")
        if _expires_at(entry) <= self.clock() + RESET_TOKEN_MARGIN:
            raise DeckError("Its saved sign-in token has expired. Open the account in Claude once, then try again.",
                            "login_needed")
        return entry["token"]

    def claim_reset(self, profile, org, grant_id, request_id):
        """Uses one limit reset of the account (`claim_reset` below), only through the OAuth route."""
        return claim_reset(self.http, self.reset_token(profile), org, grant_id, request_id)

    def _cookie_skip(self, profile, cookie_route, now):
        """Why the cookie route is not tried now, or None after reserving this attempt."""
        if not cookie_route:
            return "disabled"
        if not profile.get("cookies"):
            return "no_cookies"
        if _uuid(profile.get("org")) is None:
            return "no_org"
        with self._lock:
            last = self._cookie_attempts.get(profile.get("identity"))
            if last is not None and now - last < COOKIE_ROUTE_INTERVAL:
                return "wait"
            self._cookie_attempts[profile.get("identity")] = now
        return None

    # ---- writing a profile ----

    def restore(self, profile, tx, scope=None):
        """Writes a saved profile into Claude's data folder through `tx`. Only the scope's sign-in
        data changes; settings, MCP servers and preferences in config.json stay as they are."""
        scope = scope or self.scope
        if self.crypto.key_ref() != profile.get("key"):
            raise DeckError("Claude changed its encryption key. Reconnect this saved account.", "unsupported")
        if self.crypto.verifies_restore and (profile.get("auth") or {}).get("oauth:tokenCacheV2"):
            # macOS: the key ref is only a marker, so prove this Mac's key opens the saved tokens (a
            # ChromiumKeyNotReady or DeckError here means nothing was written).
            self._cache(profile)
        if not profile.get("cookies"):
            raise DeckError("This account needs its Desktop login saved again.", "login_needed")
        auth = profile.get("auth") or {}
        if profile.get("identity") != auth.get("lastKnownAccountUuid"):
            raise DeckError("Saved Claude profile identities disagree. Reconnect this account.", "identity")
        cookies = _checked_cookies(profile["cookies"])
        files = self._extra_writes(profile, scope)
        removals = [self._inside(rel) for rel in scope.delete_on_switch]
        config, journal = self._config()
        for key in scope.auth_keys:
            config.pop(key, None)
        config.update({key: value for key, value in auth.items() if key in scope.auth_keys})
        network = self.cookie_dir
        for name in scope.cookie_files[1:]:
            tx.write(network / name, None)
        tx.write(network / scope.cookie_files[0], cookies)
        for path, data in files:
            tx.write(path, data)
        for path in removals:
            tx.write(path, None)
        tx.write(self.base / "config.json", json_bytes(config))
        if journal["present"]:
            tx.write(self.base / JOURNAL, None)

    def clear_for_login(self, tx, scope=None):
        """Signs Desktop out for an add-account login by removing only the scope's sign-in data."""
        scope = scope or self.scope
        removals = [self._inside(rel) for rel in scope.delete_on_switch]
        config, journal = self._config()
        for key in scope.auth_keys:
            config.pop(key, None)
        for name in scope.cookie_files:
            tx.write(self.cookie_dir / name, None)
        for path in removals:
            tx.write(path, None)
        tx.write(self.base / "config.json", json_bytes(config))
        if journal["present"]:
            tx.write(self.base / JOURNAL, None)

    # ---- helpers ----

    def _config(self):
        """config.json as Claude last meant to write it (a consistent journal wins), and the journal state."""
        journal = self.journal_state()
        if journal["body"] is not None:
            return dict(journal["body"]), journal
        try:
            config = read_json(self.base / "config.json")
        except FileNotFoundError:
            raise DeckError("Open Claude Desktop and sign in once, then try again.", "not_installed") from None
        except (OSError, ValueError):
            raise DeckError("Claude is writing its settings. Try again in a moment.", "busy") from None
        if not isinstance(config, dict):
            raise DeckError("Claude's settings file is not recognized. No files were changed.", "unsupported")
        return config, journal

    def _key(self, profile):
        """The unlocked Chromium key the profile was saved with: this Mac's Keychain key, for a profile
        carrying the Mac marker (anything else is refused as not converted for this Mac)."""
        return self.crypto.key_for(profile.get("key") or "")

    def _cache(self, profile):
        """The decrypted oauth:tokenCacheV2 of a profile (DeckError when it cannot be read)."""
        key = self._key(profile)
        try:
            encrypted = base64.b64decode(profile["auth"]["oauth:tokenCacheV2"])
            if not encrypted.startswith(b"v10"):
                raise ValueError("unknown token cache format")
            cache = json.loads(key.decrypt(encrypted))
        except (KeyError, TypeError, ValueError, InvalidTag):
            cache = None
        if not isinstance(cache, dict):
            raise DeckError("Claude's saved credentials could not be read. Reconnect this account.", "unsupported")
        return cache

    @property
    def cookie_dir(self):
        """Where Claude keeps its cookie database: the profile folder itself on macOS, unless a Network
        folder already holds one (newer Chromium builds move it there)."""
        network = self.base / "Network"
        return network if (network / self.scope.cookie_files[0]).exists() else self.base

    def _capture_cookies(self, heal_copy_dir):
        network = self.cookie_dir
        database = network / self.scope.cookie_files[0]
        if not database.is_file():
            raise DeckError("Claude's sign-in cookies are missing. Open Desktop and sign in.", "login_needed")
        try:
            hot = (network / (self.scope.cookie_files[0] + "-journal")).stat().st_size > 0
        except FileNotFoundError:
            hot = False
        except OSError:
            hot = True
        snapshot = None
        if not hot:
            try:
                snapshot = _snapshot(database)
            except (sqlite3.Error, OSError):
                snapshot = None
        healed = snapshot is None
        if healed:
            if heal_copy_dir is None:
                raise DeckError("Claude is holding its sign-in files. Quit Claude, then save the account again.", "busy")
            snapshot = self.heal_cookie_copy(network, heal_copy_dir)
        return base64.b64encode(snapshot).decode(), {"hot_journal": hot, "healed": healed}

    def _read_extra(self, rel):
        try:
            return base64.b64encode(self._inside(rel).read_bytes()).decode()
        except FileNotFoundError:
            return None
        except OSError:
            raise DeckError("Claude is holding its sign-in files. Quit Claude, then save the account again.",
                            "busy") from None

    def _extra_writes(self, profile, scope):
        saved = profile.get("files") or {}
        writes = []
        for rel in scope.extra_files:
            if rel not in saved:
                continue
            try:
                data = None if saved[rel] is None else base64.b64decode(saved[rel], validate=True)
            except (TypeError, ValueError):
                raise DeckError("A saved profile file is damaged. Reconnect this account.", "vault") from None
            writes.append((self._inside(rel), data))
        return writes

    def _inside(self, rel):
        base = self.base.resolve()
        path = (base / rel).resolve()
        if path == base or not path.is_relative_to(base):
            raise DeckError("A profile file is outside Claude's data folder.", "safety")
        return path


def is_hollow(usage):
    """An all-zero answer without any reset time (§5.10); a locked window is news, never hollow."""
    return all(window.get("used") in (0, None) and window.get("reset") is None and not window.get("blocked")
               for window in (usage or {}).get("windows") or [])


def keeps_last_good(usage, last_good, now):
    """The hollow-response guard: True when `usage` must not replace `last_good`, i.e. it is hollow
    while a reset of the last good reading is still in the future."""
    if not last_good or not is_hollow(usage):
        return False
    return any(isinstance(window.get("reset"), (int, float)) and window["reset"] > now
               for window in last_good.get("windows") or [])


def _json_object(data, meta):
    """The object a usage answer must be. Any other is a failed read, which the shape watcher counts too."""
    if not isinstance(data, dict):
        claude_usage(data)
        raise ProviderError("network", http=meta.get("status"), detail="not_json", meta=meta,
                            message="The provider's answer was not understood. Your last reading is kept.")
    return data


def _resets_answer(data, meta):
    if not isinstance(data, dict):
        raise ProviderError("network", http=meta.get("status"), detail="not_json", meta=meta,
                            message="The provider's answer was not understood.")
    return data


def claim_reset(http, token, org, grant_id, request_id):
    """The one request that uses a Claude limit reset, as Claude Code sends it (RS1 §C1): POST
    /api/organizations/<org>/reset_rate_limits with the bearer, the OAuth beta flag (http.request_headers) and
    {"program": "cedar_ember", "grant_id", "request_id"}; AccountDeck's own User-Agent. Returns (answer or None,
    meta); raises ProviderError like any request (its `meta["unsent"]`: certainly never left this Mac)."""
    org = _uuid(org)
    if org is None or not isinstance(grant_id, str) or not isinstance(request_id, str):
        raise DeckError("This limit reset cannot be named in a request.", "invalid")
    return http("POST", RESET_CLAIM_URL.format(org=org), token=token, timeout=RESET_CLAIM_TIMEOUT,
                json_body={"program": RESET_PROGRAM, "grant_id": grant_id, "request_id": request_id})


def _reading(data, now, route):
    """The reading of one usage answer. One that reports no weekly limit is a failed read (S3a): a
    reading without it would leave every switch decision blind."""
    usage = claude_usage(data, now=now)
    if not has_weekly(usage):
        raise no_weekly(data, route=route)
    return usage


def _uuid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _expires_at(entry):
    value = entry.get("expiresAt")
    return value / 1000 if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _entries(cache, identity):
    prefix = f"acct:{identity}|"
    return [(key, value) for key, value in cache.items() if key.startswith(prefix) and isinstance(value, dict)]


def _is_sentinel(entry):
    return not entry.get("token") and not entry.get("refreshToken") and not entry.get("expiresAt")


def _digest_forms(data):
    """Encodings a journal header may use for sha256(data)."""
    raw = hashlib.sha256(data).digest()
    encoded = (base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode())
    return {raw.hex(), raw.hex().upper(), *encoded, *(value.rstrip("=") for value in encoded)}


def _snapshot(path, *, read_only=True):
    """The bytes of a consistent copy of a SQLite database, taken with the backup API."""
    source = sqlite3.connect(Path(path).resolve().as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
                             uri=True, timeout=3)
    target = sqlite3.connect(":memory:")
    try:
        source.backup(target)
        data = bytearray(target.serialize())
    finally:
        source.close()
        target.close()
    # backup() materialized the whole database; the detached copy must not expect a WAL sidecar.
    data[18:20] = b"\x01\x01"
    return bytes(data)


def _cookie_db(encoded):
    """An in-memory connection over a cookie snapshot; ValueError when it is not a SQLite database."""
    raw = base64.b64decode(encoded, validate=True)
    if not raw.startswith(SQLITE_MAGIC):
        raise ValueError("not a SQLite database")
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(raw)
    except sqlite3.Error:
        db.close()
        raise
    return db


def _claude_rows(encoded, names):
    """(host, name, value, encrypted_value) of the named cookies set for claude.ai."""
    db = _cookie_db(encoded)
    try:
        rows = db.execute(f"SELECT host_key, name, value, encrypted_value FROM cookies WHERE name IN "
                          f"({','.join('?' * len(names))})", names).fetchall()
    finally:
        db.close()
    return [row for row in rows if str(row[0]).lstrip(".") in CLAUDE_HOSTS]


def _has_session_cookie(encoded):
    return any(value or encrypted for _host, _name, value, encrypted in _claude_rows(encoded, SESSION_COOKIES))


def _has_session_cookie_quietly(encoded):
    if not encoded:
        return False
    try:
        return _has_session_cookie(encoded)
    except (ValueError, sqlite3.Error):
        return False


def _checked_cookies(encoded):
    """The raw bytes of a saved cookie snapshot that SQLite accepts (v1's checks)."""
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError):
        raw = b""
    if not raw.startswith(SQLITE_MAGIC):
        raise DeckError("Saved cookie database is invalid.", "vault")
    check = sqlite3.connect(":memory:")
    try:
        check.deserialize(raw)
        if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("quick_check failed")
        check.execute("SELECT name FROM cookies LIMIT 1")
    except sqlite3.Error:
        raise DeckError("Saved cookie database is incomplete. Reconnect this account.", "vault") from None
    finally:
        check.close()
    return raw
