# Provider facts for the Mac port and the PC-to-Mac transfer

> Design notes of the Mac port as of October 2026; the code and [docs/GUIDE.md](../GUIDE.md) are current
> where they differ. The app is now Mac-only; the Windows sections explain data that came over
> from the PC. Section numbers stay as they are: code comments cite them.

`terminal-mode.md` and the design research cited below (`research 00`–`04`,
`01-codex-live-switch-internals.md`) were working papers and are not published.

Research date 2026-10-07.

The one-time transfer: the owner exports every saved account on this PC and imports it on a Mac
without signing in again, then the PC stops using those accounts. These are the provider facts that
work depends on. Getting a format wrong could lock the owner out, so each fact carries a source and a
confidence label.

Confidence labels:

- **[code]** seen in a program's own files or source (file and identifier or message named). The strongest evidence here.
- **[mac]** verified on the test Mac (macOS 26.6.2, LibreSSL 3.3.6, python3 3.9.6, temporary keychain; no Claude/Codex installed)
- **[doc]** documented (link)
- **[inferred]** reasoned from the above, not directly observed

Sources (no credential store was read):

- Claude Desktop 2.19675.0 for Windows, whose Electron code is the same on both systems: its observed behaviour and the formats of the files it keeps. The `index.chunk-…js` and `package.json` names below are files of that build.
- Claude Code CLI 2.x (the native build): its observed behaviour, identifiers and messages. Its `~/.claude` contents were never read.
- Codex: its published source, `codex-rs/` in [openai/codex](https://github.com/openai/codex), on `main` as of early October 2026 and at version 0.159.0.
- AccountDeck's own Windows adapters: `accountdeck/providers/{base,claude,claude_cli,codex}.py`, `accountdeck/switch_*.py`, `accountdeck/cli_locks.py`, `accountdeck/config.py`.

---

## 1. Claude Desktop on macOS

### Identity and locations

| Fact | Value | Source |
|---|---|---|
| macOS bundle id | `com.anthropic.claudefordesktop` | [code] `index.chunk-CN7MfJQH.js`, `index.pre.js` (appears 5× as the primary id); updater bundle `com.anthropic.claudefordesktop.ShipIt` (Squirrel.Mac) |
| Alt/legacy ids seen | `com.anthropic.Claude.desktop` (`package.json`), `com.anthropic.claude` | [code] |
| Electron product/app name | `Claude` (`package.json` `productName:"Claude"`, internal `name:"@ant/desktop"`) | [code] package.json |
| App bundle install path | `/Applications/Claude.app` (standard; the DMG installs there) | [inferred][doc] |
| Profile folder (userData) | `~/Library/Application Support/Claude/` | [code] `app.getPath("userData")`; string `Application Support/Claude` in `index.chunk-CN7MfJQH.js`. `app.getPath("userData")` on macOS = `~/Library/Application Support/<productName>` [doc Electron] |
| Log folder + main log | `~/Library/Logs/Claude/main.log` | [code] app uses `getPath("logs")` + `main.log`; `app.getPath("logs")` on macOS = `~/Library/Logs/<productName>` [doc Electron] |

The Windows profile is `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude`
(MSIX) — see `accountdeck/config.py:150-153`, `CLAUDE_FAMILY = "Claude_pzs8sxrjxfjjc"`. The Mac profile
is a plain directory (no MSIX redirect / split-brain), so the newest-mtime folder-resolution logic in
`windows.claude_base()` is not needed on Mac: the path is fixed. [inferred]

### Files inside the profile (what Windows swaps → Mac equivalents)

AccountDeck's Windows swap set is small and selective (`providers/base.py`, `providers/claude.py`):

- `config.json` — holds the auth keys `AUTH_KEYS = ("oauth:tokenCache", "oauth:tokenCacheV2", "lastKnownAccountUuid")` [code base.py:7]. On Mac it is `~/Library/Application Support/Claude/config.json` [code `getPath("userData")+"config.json"`]. The swap removes the scope's auth keys and writes the saved ones, leaving all other settings/MCP/preferences intact (`claude.py restore()`:399-401).
- `Local State` — JSON with `os_crypt.encrypted_key` (the wrapped master key) [code claude.py:448 `_local_state_key`]. Path on Mac: `~/Library/Application Support/Claude/Local State`. **On macOS there is NO `Local State` master key for the app's own data** — Chromium/Electron on macOS derives the key from the Keychain, not from `Local State` (see §2). A `Local State` file may still exist for other Chromium state but `os_crypt.encrypted_key` is a Windows/Linux concept. [inferred][doc]
- `Network/Cookies` (+ `-journal`, `-wal`, `-shm`) — `COOKIE_FILES = ("Cookies","Cookies-journal","Cookies-wal","Cookies-shm")` inside `<profile>\Network` [code base.py:8]. Mac path: `~/Library/Application Support/Claude/Network/Cookies`. Same relative layout as Windows. [code/inferred]
- `claude_desktop_config.json` — Claude Chat's local MCP servers (`switch_claude.py mcp_names`). Shared; not account-bound. Mac: same folder.
- `SETTLED_FILES = ("remote-control-state.json","bridge-state.json","buddy-tokens.json","ant-device-registry.json")` [code switch_claude.py:47] — presence is recorded for diagnostics; AccountDeck does **not** swap or delete them. Same names on Mac. Note `bridge-state.json` (remote-control bridge) and `ant-device-registry.json` (Claude-in-Chrome device map, account-UUID → device registration) are per-account/per-device state; the design research (research 01) recommends deleting a stale `bridge-state.json` and re-pairing devices rather than transferring them.
- The **"cowork partition"**: `Partitions\launch-preview-cowork-shared\Network\Cookies` (a second cookie jar for the Cowork webview) [doc research 00-SYNTHESIS §Profile]. Mac: `~/Library/Application Support/Claude/Partitions/launch-preview-cowork-shared/Network/Cookies`. The Cowork VM / local-agent sessions are bound to the account UUID in their on-disk path and are **not** portable. `cowork-svc.exe` on Windows (`procs.py:55`) → on Mac the Cowork helper is `cowork-svc` inside the app bundle (no `.exe`). [inferred]
- Renderer identity stores `Local Storage/`, `Session Storage/`, `IndexedDB/` — AccountDeck does **not** swap these (minimal set). The maintained macOS switchers (mobius, claude-acc, ai-usagebar; research 01 §3) all include them. On Mac they live in the same `~/Library/Application Support/Claude/` subfolders. Whether they must travel for a clean transfer is unproven (see Unknowns). [inferred]

### Token cache format (`oauth:tokenCacheV2`)

Verified in app code (`index.chunk-CN7MfJQH.js`): the OAuth cache is persisted into the
electron-store named **`config`** (= `config.json`) under key `oauth:tokenCacheV2`:

- persist: `store.set("oauth:tokenCacheV2", safeStorage.encryptString(JSON.stringify(cache)).toString("base64"))`
- load: `safeStorage.decryptString(Buffer.from(store.get(key),"base64"))`, then `JSON.parse`
- cache-key shape: `acct:<accountUuid>|<clientId>:<orgId>:https://api.anthropic.com:<scopes>` (prefix `acct:`; function `kO`). Older builds used `<userUuid>:<orgId>:host:scopes` without the `acct:` prefix. [code]
- log line AccountDeck parses: `"[oauth] loaded token cache from disk with %d entries"` [code] — matches `switch_claude.py:69 _TOKEN_ENTRIES`. Also `"Account details received via IPC"`, `"Initialization succeeded - accountId=…"` and `"[oauth-v2] …"` lines (switch_claude.py:67-70) are present in this build.

So `config.json["oauth:tokenCacheV2"]` is `base64( <platform-specific safeStorage blob> )` on **both**
OSes; only the blob's inner encryption differs (§2). This matches `claude.py:472-484 _cache()` which
base64-decodes, checks the `v10` prefix, and AES-GCM-decrypts on Windows.

### Processes, helpers, Code-tab sessions

| Windows (procs.py) | macOS equivalent | Source |
|---|---|---|
| main `Claude.exe` (family `Claude_pzs8sxrjxfjjc`) | `Claude` (main Electron process) | [inferred] |
| helpers (3×) | `Claude Helper`, `Claude Helper (GPU)`, `Claude Helper (Renderer)`, `Claude Helper (Plugin)` | [inferred][doc] standard Electron helper naming |
| `cowork-svc.exe` (protected) | `cowork-svc` (in bundle) | [code] name; [inferred] mac form |
| `chrome-native-host.exe` | `chrome-native-host` | [code]/[inferred] |
| Code-tab session: `…\Claude\claude-code\<ver>\claude.exe` (`procs.py:59 CODE_CHILD_PATH`, `is_code_child_path`) | `~/Library/Application Support/Claude/claude-code/<ver>/claude` | [code] Windows path under userData; [inferred] mac path = userData/claude-code/<ver>/claude |

The Code-tab `claude` children get their token from Desktop via the environment, so a terminal CLI
switch does not disturb Desktop (terminal-mode.md §2). [doc]

### Quit gracefully and relaunch (Mac)

- Graceful quit: send the app an Apple Event quit — `osascript -e 'quit app "Claude"'` (or `tell application "Claude" to quit`). This runs the app's `before-quit`/`will-quit` handlers (present: `index.chunk-CN7MfJQH.js` has `before-quit`, `will-quit`, `app.quit`, `requestSingleInstanceLock`), which flush `config.json` and the cookie DB. This is the Mac analogue of the Windows "quit from tray"; do **not** `kill -9` (leaves a hot cookie journal). [code presence of handlers; inferred for the Apple-Event method]
- The app takes a single-instance lock (`requestSingleInstanceLock`), so relaunch while it is quitting can fail — wait for exit first. [code]
- Relaunch: `open -b com.anthropic.claudefordesktop` (or `open -a Claude`). This is the Mac analogue of the Windows AUMID `shell:AppsFolder` activation (`procs.py launch_claude`). [inferred][doc]
- Mac has no AUMID; the `claude_aumid` setting and Explorer activation (`procs.py:1299`) are Windows-only. [code]

---

## 2. Encryption formats, Windows vs Mac

Electron `safeStorage` (used for `oauth:tokenCacheV2` and other auth keys) and Chromium `os_crypt`
(used for cookie `encrypted_value`) share the same key and the same `v10`/`v11` framing on each OS.

### Windows (what AccountDeck already implements)

- Master key: `Local State` → `os_crypt.encrypted_key`, base64. Strip the 5-byte `"DPAPI"` prefix, then `CryptUnprotectData` (DPAPI, current user) → a 32-byte AES key. [code claude.py:446-470 `_aes_key`, `core.dpapi`]
- Encryption: `v10` + 12-byte GCM nonce + ciphertext + 16-byte GCM tag; **AES-256-GCM**, no AAD. [code claude.py:336,479 `AESGCM(key).decrypt(blob[3:15], blob[15:], None)`]
- `oauth:tokenCacheV2` in `config.json` = `base64("v10" + nonce + ct + tag)`. [code]

### macOS (what the port must add)

- Master key: **the login Keychain**, not `Local State`. Electron `safeStorage` on macOS reads a generic-password item whose service is `"<app name> Safe Storage"` and account is `"<app name>"`. For this app, app name = `Claude` ⇒ **service `"Claude Safe Storage"`, account `"Claude"`**. The stored secret is a random password string. [doc Electron/Chromium `KeychainPassword`; the app's own Chrome-import module uses the identical scheme and error class `"Chrome Safe Storage read failed"`]. Confidence: the algorithm is [code]/[mac]-verified; the exact item names `"Claude Safe Storage"`/`"Claude"` are [inferred] from Electron's app-name rule and need a one-line check on a real Mac (`security find-generic-password -s "Claude Safe Storage"`).
- Key derivation from that password: **PBKDF2-HMAC-SHA1**, salt `b"saltysalt"`, **1003** iterations, **16-byte** key (AES-128). [doc Chromium `os_crypt_mac`; [mac] the derivation reproduces byte-for-byte, below]
- Encryption: `v10` + ciphertext; **AES-128-CBC**, IV = **sixteen `0x20` (space) bytes**, PKCS#7 padding. No per-record nonce, no GCM tag. [code] the app's cookie decryptor (`index.chunk-ChwwJtOA.js`): `createDecipheriv("aes-128-cbc", key, oe)` with `oe = Buffer.alloc(16, 0x20)`, after stripping a 3-byte version prefix (`M=3`, `a.subarray(M)`).
- `oauth:tokenCacheV2` in `config.json` = `base64("v10" + AES-128-CBC(pkcs7(JSON)))`. Same outer base64, same `v10` tag, different inner cipher. [inferred from the shared safeStorage path + the CBC branch above]

### Cookie `encrypted_value` plaintext layout and the host-key prefix

From the app's own cookie decryptor (`index.chunk-ChwwJtOA.js`) [code]:

```
plaintext = AES-decrypt(encrypted_value[3:])          // 3 = len("v10"/"v11")
if cookie_db_meta_version >= 24:
    expect plaintext[:32] == SHA-256(host_key)        // timing-safe compared, then stripped
    value = plaintext[32:]
else:
    value = plaintext
```

- The 32-byte `SHA-256(host_key)` prefix is added **iff the cookie DB `meta` table `version` is ≥ 24**. [code `r>=24` where `r` is the meta version; matches `claude.py:337-339` which strips 32 bytes when `raw.startswith(sha256(host))`].
- `host_key` is the cookie's `host_key` column value (e.g. `claude.ai` or `.claude.ai`). AccountDeck computes `hashlib.sha256(host.encode()).digest()` [code claude.py:337].
- Windows builds at meta ≥ 24 also carry this prefix (the layout is Chromium-wide, not OS-specific); only the cipher around it differs.

### Is a Windows cookie DB schema-compatible with Mac Claude?

- **Schema: yes.** The `cookies` table and `meta` version are Chromium-wide and identical for the same
  Chromium/Electron version on both OSes. The SQLite file itself opens fine on Mac. [inferred][doc]
- **Ciphertext: no.** Every `encrypted_value` is sealed with the **Windows** key (DPAPI-wrapped,
  AES-256-GCM). Mac Claude will try to decrypt it with its **Keychain-derived AES-128-CBC** key and
  fail. So a raw copy of the Windows `Network/Cookies` is useless on the Mac.
- **Recommendation: rewrite the rows.** Keep the Windows DB as the schema donor (or build a fresh DB
  with the Mac Chromium's own `cookies`/`meta` schema), and for each row: decrypt `encrypted_value`
  with the Windows key → recover the plaintext value (strip the 32-byte host hash if meta ≥ 24) →
  re-encrypt with the Mac key (prepend `SHA-256(host_key)` again if the **target** DB's meta ≥ 24, use
  `v10` + AES-128-CBC) → store back into `encrypted_value`, leaving the plaintext `value` column empty
  (Chromium's convention). The `meta.version` of the DB you write must match what the installed Mac
  Claude expects; if unsure, let Mac Claude create its own empty `Network/Cookies` once, read its
  `meta.version`, and rewrite rows into **that** DB. [inferred, strongly supported by code]

### Decrypt (Windows) → re-encrypt (Mac) recipe

```
# WINDOWS side (per blob: a cookie encrypted_value, or the base64-decoded oauth:tokenCacheV2)
assert blob[:3] == b"v10"
win_key = DPAPI_unprotect( base64decode(LocalState.os_crypt.encrypted_key)[5:] )   # 32 bytes
plaintext = AES_256_GCM(win_key).decrypt(nonce=blob[3:15], data=blob[15:])         # data = ct||tag
# For a cookie at meta>=24: value = plaintext[32:]  (plaintext[:32] == sha256(host_key))
# For tokenCacheV2:         value = plaintext        (JSON text, no host prefix)

# MAC side
mac_pw  = Keychain.get(service="Claude Safe Storage", account="Claude")            # random string
mac_key = PBKDF2_HMAC_SHA1(mac_pw, salt=b"saltysalt", iterations=1003, dklen=16)
IV = b"\x20"*16
# For a cookie into a meta>=24 DB: pt = sha256(host_key) + value   else pt = value
# For tokenCacheV2:               pt = JSON text
new_blob = b"v10" + AES_128_CBC(mac_key, IV).encrypt(pkcs7(pt))
# cookie: store new_blob in encrypted_value; tokenCacheV2: store base64(new_blob) in config.json
```

### Known-answer test vectors (derived here; round-trip and Mac-tool verified)

These use **fixed, public** inputs (no real secrets) so implementers can unit-test both directions.
Computed with Node 24 `crypto`; the Mac key derivation and CBC ciphertext were reproduced
byte-for-byte on the test Mac with python3 `hashlib.pbkdf2_hmac` and LibreSSL `openssl enc` [mac].

Common plaintexts:
- cookie: `host_key="claude.ai"`, value `sk-sessionkey-EXAMPLE-VALUE`; `SHA-256("claude.ai") = 743e483ae01f1fa262d2b2142bdd118d5289bf940191b1f5fcdf820ed3124eba`
- token cache JSON (compact): `{"acct:demo":{"token":"T","expiresAt":111}}`

**MAC** — PBKDF2-HMAC-SHA1(password=`peanuts`, salt=`saltysalt`, iters=1003, len=16); AES-128-CBC; IV=`20`×16; `v10`:
- `mac_key = d9a09d499b4e1b7461f28e67972c6dbd`   *(verified identical on the test Mac)*
- cookie `encrypted_value` (hex) = `763130a38158b3716edd6caaea9421ad64bfc0295e72ba8b71e586d2b218f113cc99404a7df8239c9d95d6a374206a527df5b91808cbbdfbb9950b7ef65f122551e634`   *(verified identical on the test Mac via `openssl enc -aes-128-cbc`)*; decrypt then drop first 32 bytes ⇒ `sk-sessionkey-EXAMPLE-VALUE`
- tokenCacheV2 blob (hex) = `7631305cda85e21bbc562810d990553833ec9e01f32bff868c67c93c64a93ca2933a06137809d03590302169618dda2aba895c`
- tokenCacheV2 as stored in config.json (base64) = `djEwXNqF4hu8VigQ2ZBVODPsngHzK/+GjGfJPGSpPKKTOgYTeAnQNZAwIWlhjdoquolc`

(`peanuts` is Chromium's well-known Linux fallback password; on a real Mac the password is the random
`"Claude Safe Storage"` Keychain secret. Swap it in and the recipe is unchanged.)

**WINDOWS** — AES-256-GCM, demo key = bytes `00..1f`, nonce = `000102030405060708090a0b`; `v10`+nonce+ct+tag:
- `win_key = 000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f` (real key = DPAPI-unwrap of `Local State`)
- tokenCacheV2 blob (hex) = `763130000102030405060708090a0b3c20b778a691f87fe82cf8a98b925a19ecbde25ad2417d281a4bc7e0651969c06463ef888dfb23a945d902894539d269d70e3978252e06ee5f1beb`
- tokenCacheV2 config.json (base64) = `djEwAAECAwQFBgcICQoLPCC3eKaR+H/oLPipi5JaGey94lrSQX0oGkvH4GUZacBkY++IjfsjqUXZAolFOdJp1w45eCUuBu5fG+s=`
- cookie `encrypted_value` (hex) = `763130000102030405060708090a0b333c9e2125faddb9ef93259f9a3469e0d15f38a0f1eaee89c4b8678bce7b4e08727b838fcab261f11bca1488f1aa6d60af1430c11ffbf59b73c26f997d4a4ba8ccde9818303a1046443ede`

The script is kept out of the repo (session scratchpad). Re-derivable with Node:
`pbkdf2Sync('peanuts','saltysalt',1003,16,'sha1')`, `createCipheriv('aes-128-cbc',key,Buffer.alloc(16,0x20))`,
`createCipheriv('aes-256-gcm',key,nonce)`.

---

## 3. Claude Code CLI on macOS

Observed in Claude Code 2.x as installed (`~/.local/bin/claude.exe` on Windows) [code]. `~/.claude` was
not read.

### Where OAuth credentials live

- **Primary store: the login Keychain**, with a **plaintext file fallback**. Strings:
  `keychain-with-plaintext-fallback`, `"your keychain (or in the credentials file in your Claude
  configuration folder)"`, `"the macOS Keychain or the credentials file"`. On Windows the primary is
  **Windows Credential Manager** (string `windows-credman`, `credential-manager`), same file fallback. [code]
- **Keychain item**: the code uses two sibling services under base **`Claude Code`**:
  `"Claude Code-device-keys"` (seen verbatim) and a credentials service formed with the `"-credentials"`
  suffix ⇒ **`"Claude Code-credentials"`**. [code `Claude Code-device-keys` verbatim + `-credentials`
  suffix string; the full `"Claude Code-credentials"` is [inferred] from the sibling pattern + prior
  community observation]. Account = the macOS login user; value = a JSON string (see shape). The
  internal module is `secureStorage`. [code]
- **Stored encoding**: the OAuth credentials are stored as a **plaintext JSON string** (the Keychain
  itself encrypts at rest), not hex. The JSON is the same `{"claudeAiOauth": {...}}` object used in the
  file fallback. [code string `claudeAiOauth`; [inferred] it is the value written to the item]
- Separately, a **device key** is stored via `security add-generic-password -U -a "<user>" -s "Claude
  Code-device-keys" -X "<hexdoc>"` and read with `find-generic-password`. The `-X` flag means the value
  is **hex**-encoded. This is a per-machine device-binding document (`deviceKeyStore`, `deviceRegistry`,
  cowork `remote_devices`), **not** the OAuth login. [code]

### `security` CLI arguments (verified behaviour on the test Mac)

Reproduced on a temporary keychain [mac]:
- write/update: `security add-generic-password -U -a <account> -s <service> -X <hexvalue> <keychain>` (hex), or `-w <string>` (plaintext). `-U` updates in place if the item exists.
- read value: `security find-generic-password -s <service> -w <keychain>` (prints only the password); `-g` prints attributes (`svce`, `acct`) and the password to stderr.
- delete: `security delete-generic-password -s <service> <keychain>`.

### File fallback and `CLAUDE_CONFIG_DIR`

- Fallback file: `~/.claude/.credentials.json` (0600). Used when the Keychain is unavailable
  (locked, headless/no-GUI session, or `errSecInteractionNotAllowed`). [code strings
  `keychain_locked_skip_fallback`, `plaintext_fallback_used`, `plaintext_fallback_primary_not_cleared`;
  terminal-mode.md §2 for the Windows/Linux path]
- With `CLAUDE_CONFIG_DIR` set, the credentials file lives in that folder instead
  (`<CLAUDE_CONFIG_DIR>/.credentials.json`). [doc code.claude.com/authentication; terminal-mode.md §2]
- The keychain **service suffix for a custom `CLAUDE_CONFIG_DIR`**: not proven. Claude Code
  treats `CLAUDE_CONFIG_DIR` as the settings/home root; a non-default dir almost certainly scopes the
  keychain item too (Codex does this explicitly — §4). **Unknown exact form; confirm on a real Mac.**
  AccountDeck's own Windows provider already refuses a custom `CLAUDE_CONFIG_DIR`
  (`claude_cli.py:readonly_reason`, `CUSTOM_MESSAGE`), so the default-dir case is what matters.

### Credential shape (same on all OSes)

`{"claudeAiOauth": {accessToken, refreshToken, expiresAt(ms), refreshTokenExpiresAt, scopes,
subscriptionType, rateLimitTier, ...}}` plus siblings. [code strings `claudeAiOauth`, `accessToken`,
`refreshToken`, `expiresAt`, `refreshTokenExpiresAt`, `subscriptionType`] — matches
`providers/claude_cli.py oauth()` and terminal-mode.md §2. Account identity (`oauthAccount`:
`accountUuid`, `emailAddress`, `organizationUuid`) lives in `~/.claude.json`, written often. [code]

### Locks and cross-process pickup (how a running session adopts a changed credential)

- **Write lock** around credential/device-key writes: an internal `secureStorage` / `deviceKeyStore`
  `withWriteLock` with **readback verification** (strings `withWriteLock`, `[secureStorage] write lock
  compromised`, `readback_mismatch`, `item_write_failed`, `read_failed_skip_write`). This is the
  macOS/keychain analogue of the Windows/Linux proper-lockfile protocol. [code]
- **Proper-lockfile directory locks** (`.oauth_refresh.lock`, legacy `.claude.lock`, `.claude.json.lock`)
  that `accountdeck/cli_locks.py` and terminal-mode.md rely on still exist for the config/credential
  **file** path. On macOS, when creds are in the Keychain the authoritative serialization is the
  keychain write lock, not the file lock — a swap that only rewrites `.credentials.json` may be ignored
  while the Keychain holds the live item. **Implication for the port:** to change a Claude Code account
  on Mac, write the **Keychain** item (not just the file), under the keychain write lock. [code + inferred]
- **Pickup by running sessions** (no restart): the auth worker re-reads the Keychain on a schedule —
  strings: `"auth: no token found, will re-check keychain every 30s"`, `"auth: re-check reads the
  keychain again"`, `"auth: token found via keychain re-check"`, `"auth: token refreshed via keychain
  re-check retry"`, plus proactive refresh (`"auth: scheduling proactive refresh in …"`, `"auth: token
  still valid (cross-process refresh or not yet due)"` — it explicitly tolerates a token refreshed by
  **another** process). So a session picks up an externally changed credential within ~30 s / at the
  next refresh check, matching the Windows "re-reads on change" assumption (terminal-mode.md §2). A
  daemon writes `daemon-auth-status.json` with a `v5 cooldown`. [code]

Net: the Mac story is the Windows story with the store swapped from `.credentials.json`+file-locks to
Keychain(`Claude Code-credentials`)+keychain-write-lock, with `.credentials.json` as fallback only.

---

## 4. Codex on macOS

From pinned Codex Rust source [code] (`codex-main/codex-rs/`):

- **Default store = `file`.** `AuthCredentialsStoreMode` has `#[default] File` (`config/src/types.rs:116-126`),
  comment `"Persist credentials in CODEX_HOME/auth.json"`. **There is no macOS-specific override** — the
  research confirms "no Windows-specific default" and the enum default is `File` on every OS
  (`01-codex-live-switch-internals.md §3`). So on macOS Codex reads/writes **`~/.codex/auth.json`**
  (plaintext JSON) unless `cli_auth_credentials_store` is set otherwise. [code]
- `cli_auth_credentials_store` values: `file` (default), `keyring` (fail if unavailable), `auto`
  (keyring, fall back to file), `ephemeral` (process memory). [code types.rs]
- **Keyring mode on macOS** uses `AuthKeyringBackendKind::Direct` (non-Windows default;
  `types.rs:155-162` `if cfg!(windows) { Secrets } else { Direct }`). Direct = the serialized `auth.json`
  JSON stored **directly** in the OS keychain. Service = **`"Codex Auth"`** (`login/src/auth/storage.rs:243`
  `KEYRING_SERVICE`). Account/key = **`cli|<first-16-hex-of-SHA-256(canonical CODEX_HOME path)>`**
  (`compute_store_key`, storage.rs:245-256). Value = `serde_json::to_string(AuthDotJson)`. On save in
  keyring mode the `auth.json` file is deleted (storage.rs cleanup); `auto` falls back to the file only
  on keyring error. [code]
  - Windows keyring mode differs: `Secrets` backend ⇒ an age-encrypted `CODEX_HOME\secrets\codex_auth.age`
    with the key in Windows Credential Manager. [code/research]
- **MCP credentials** are separate: `OAuthCredentialsStoreMode` default `Auto` (keyring preferred, file
  fallback `CODEX_HOME/.credentials.json`). [code types.rs:131-143]
- `security` round-trip for the `"Codex Auth"` item verified on the test Mac (add `-U`/`-w`/`-X`, find `-w`,
  delete). [mac]

`auth.json` shape (`AuthDotJson`): `{ "OPENAI_API_KEY": <null|str>, "tokens": { "id_token",
"access_token", "refresh_token", "account_id" }, "last_refresh" }`. AccountDeck's `providers/codex.py`
reads `token_state` from the access-token JWT `exp` and `last_refresh`, and forces file mode in its
probe helper (`PROBE_CONFIG = b'cli_auth_credentials_store = "file"\n…'`). [code codex.py:38]

**AccountDeck already refuses non-file Codex modes** (`validate_store`, research 04 §2.1) and swaps
`auth.json` whole through its transaction. On Mac that is unchanged: the transfer target is
`~/.codex/auth.json`. If the owner set keyring mode, the port must write the `"Codex Auth"` keychain
item with the `cli|<hash>` account, or (simpler) set `cli_auth_credentials_store = "file"` and write
`auth.json`. [code + inferred]

### How the app-server reports a sign-in it cannot renew (observed 2026-10-08, Codex 0.161.0)

Checked offline against the installed Codex 0.161.0 app-server, with a synthetic `CODEX_HOME`, fake
unsigned JWTs and `CODEX_REFRESH_TOKEN_URL_OVERRIDE` pointing at a local stub (no provider contacted). [mac]

- At start-up the app-server renews an **expired** access token by itself, also when the probe's refresh
  token is blank (read-only probes send `refresh_token: ""`, which can rotate nothing).
- A **permanent** refusal (`refresh_token_expired`, `_reused`, `_invalidated`) makes Codex drop the sign-in:
  `account/read` then answers `{"account": null, "requiresOpenaiAuth": true}` (no JSON-RPC error), and
  `account/rateLimits/read` fails with a 401. The baseline build read `account: null` as "not a
  subscription login" and reported `code=identity`, also for an account whose refresh token had been
  used up. It is now `unauthorized` → `refresh_rejected` → credential `reauth_required` ("sign in again").
- A **transient** failure (HTTP 500) keeps the sign-in; with the access token expired, `account/read`
  then fails with `-32603 "workspace routing discovery unauthorized (401)"` (→ `access_expired` read-only).

### Codex desktop app

- A running Codex (TUI/daemon) does **not** watch `auth.json`; the guarded reload refuses to adopt a
  different account mid-session (`01-codex-live-switch-internals.md §1, §2`). [code/research]
- The Codex Desktop app has its own bundled app-server that reads the same `CODEX_HOME` store; it is
  closed-source. [doc/inferred research 04]
- Windows process names: `chatgpt.exe`, `chatgpt (beta).exe` (`procs.py:58 CODEX_DESKTOP_NAMES`),
  plus `codex.exe` for the CLI (TUI `codex_tui`, `app-server` daemon). [code]
- **macOS process names: not confirmed.** Likely `ChatGPT` / `Codex` for the desktop app and `codex`
  for the CLI (no `.exe`). The CLI's `app-server` daemon socket is `CODEX_HOME/app-server-control/
  app-server-control.sock` on Unix. **Confirm desktop process name on a real Mac.** [inferred]

---

## 5. Token rotation and moving accounts

### What rotates / what detects reuse

| Provider / store | Rotation & reuse behaviour | Source |
|---|---|---|
| Claude Code OAuth | Refresh tokens are **single-use and rotate**: a second holder's copy dies with `invalid_grant` (400/401), and CC then writes a blank stub. Refresh tokens expire after ~**4 weeks** unused. Refresh: `POST https://platform.claude.com/v1/oauth/token`, `client_id 9d1c250a-…`. | [doc] terminal-mode.md §2 / research 03; [code] claude_cli.py `refresh()`, `TOKEN_URL`, `CLIENT_ID` |
| Claude Desktop token cache | Access token ~8 h; the cache is re-minted via a **fresh `/authorize`** using the durable **`sessionKey` cookie**, which is the real root credential. Desktop refuses to mint if the `sessionKey` names a different account than the flow (`"oauth: sessionKey names a different account than the flow; not minting"`). Legacy `oauth:tokenCache` is emptied to an encrypted `{}` after first sign-in on a new build. | [doc] research 01 §5; [code] the app's `[oauth-v2]` logic |
| Codex / OpenAI | Refresh-token **reuse is detected**: the OSS routers warn `refresh_token_reused` and "never copy `auth.json` into a pool". Refusal kinds AccountDeck handles: `refresh_token_expired`, `refresh_token_reused`, `refresh_token_invalidated`, `invalid_grant` (`codex.py:REFRESH_REJECTED`). One live refresh chain per account. | [code] codex.py; research 02 |

### Is the claude.ai `sessionKey` cookie device- or IP-bound?

- The `sessionKey`/`sessionKeyV3`/`sessionKeyLC` cookies are **server-side session bearers keyed to the
  account, not the IP**: AccountDeck's own cookie route reuses them to read usage from a different
  process, and the research shows a saved cookie outliving the OAuth token (ai-multi-instance). So they
  are expected to work from the Mac's IP. [code claude.py `_read_cookie`; research 01] Confidence [inferred].
- `cf_clearance` **is** bound to UA + IP (Cloudflare) and will differ on the Mac; it is regenerated
  automatically by Cloudflare and is not needed for a valid session. `__cf_bm` is short-lived. [doc research 01]
- `anthropic-device-id` is a device-identifier cookie carried in the cookie DB; it transfers with the
  jar. Separate from it, **Claude Desktop and Claude Code now register a per-machine device key**
  (`ant-device-registry.json`; Claude Code `Claude Code-device-keys` + `deviceRegistry`/cowork
  `remote_devices`). Device keys are **per-machine and must NOT be transferred** — the Mac registers its
  own; a stale device entry for the old PC is harmless or re-paired. [code/research]

### What the PC must stop doing / delete so a moved account works on the Mac indefinitely

General rule: after export and Mac import, on the PC **remove the local credential stores for those
accounts and stop every process that could refresh/rotate them — but NEVER invoke any provider's own
"Log out" / `/logout` / `account/logout`, which revokes the server-side session or token** (AGENTS.md;
`01` notes `/logout` revokes). Local removal preserves the server session for the Mac.

- **Claude Desktop (PC's currently signed-in account):** quit Desktop gracefully, then remove the
  account's sign-in from `config.json` (`oauth:tokenCacheV2`, `oauth:tokenCache`,
  `lastKnownAccountUuid`) and its `claude.ai` cookies from `Network/Cookies` — exactly
  `Claude.clear_for_login()` does (`claude.py:414`). Do **not** click Desktop's "Log out". Because the
  `sessionKey` is a shared server session (not single-use), the Mac keeps working; the risk is the PC's
  Desktop re-minting/rotating the OAuth chain, so the PC must not run that account again. [code/inferred]
- **Claude Code (PC):** delete the PC's credential for that account — the Keychain item on Mac, or on
  Windows the Credential Manager entry / `~/.claude/.credentials.json` — so only the Mac holds the
  (latest) refresh token. Since refresh tokens are single-use, if both machines keep a copy the first to
  refresh invalidates the other. Do not `/logout`. The PC's device key stays local and is irrelevant to
  the Mac. [code/doc]
- **Codex (PC):** delete `~/.codex/auth.json` (or the keyring item) for the moved account so the PC
  stops refreshing it; OpenAI's reuse detection will otherwise kill whichever copy refreshes second. Do
  not `account/logout`. [code/research]
- In all three cases, make sure no background process keeps the old tokens alive: the PC's Claude
  Desktop, any Claude Code sessions/daemon, and Codex TUIs/daemon for those accounts must be closed and
  not relaunched on them. AccountDeck's existing "the PC stops using the account" bookkeeping (owner
  flags, excluded accounts) should mark them retired after export. [inferred]

---

## 6. Unknowns — need a real Mac with Claude / Claude Code / Codex installed

These could not be settled on the test Mac (no Claude/Codex there) or from Windows program code alone:

1. **Exact Electron safeStorage Keychain item names for Claude Desktop.** Predicted service
   `"Claude Safe Storage"`, account `"Claude"` (Electron app-name rule). Confirm with
   `security find-generic-password -s "Claude Safe Storage" -g`. The PBKDF2/AES-128-CBC/IV/v10 algorithm
   around it is already [mac]-verified; only the item names are unconfirmed.
2. **Exact Claude Code OAuth keychain service string.** Predicted `"Claude Code-credentials"`, account =
   login user, value = `{"claudeAiOauth":…}` JSON. The `"Claude Code-device-keys"` sibling and the
   `-credentials` suffix are [code]-confirmed; the full credentials service string and the account value
   need one `security` read on a signed-in Mac.
3. **`CLAUDE_CONFIG_DIR` keychain suffix** for Claude Code (if the owner ever uses a non-default dir).
4. **Mac cookie DB `meta.version`** of the installed Claude Desktop build (to know whether to prepend the
   32-byte `SHA-256(host_key)` prefix when rewriting rows). Read `PRAGMA user_version`/`SELECT value FROM
   meta WHERE key='version'` from a freshly-created Mac `Network/Cookies`.
5. **Whether `Local Storage`/`Session Storage`/`IndexedDB` must travel** for a clean Desktop transfer, or
   whether the cookie jar + token cache + `config.json` auth keys suffice (the minimal AccountDeck set).
   The maintained Mac switchers include the LevelDB stores; AccountDeck does not.
6. **Codex Desktop macOS process name(s)** and whether it reads `~/.codex/auth.json` or its own store.
7. **Whether importing credentials without launching the app once is accepted** — e.g. does Mac Claude
   Desktop accept a hand-written `oauth:tokenCacheV2` and cookie DB on first launch, or does it require a
   `sessionKey`-driven fresh `/authorize` to repopulate the cache? (Same open question the research flags
   for Windows.)
8. **Login-keychain availability in the owner's Mac session** (GUI login vs SSH/headless): writing the
   Claude Code / Codex keychain items needs an unlocked login keychain; the fallback files are the
   headless path.
