# Third-party notices

AccountDeck is released under the MIT License (see the `LICENSE` file beside this one). This file names the work
it builds on: code adapted from one other project, the two logos the dashboard draws, the parts the installer
downloads, and the projects whose ideas shaped the design. Everything else is AccountDeck's own work. The
dashboard loads no outside script, style or font, and the menu bar item uses AccountDeck's own ctypes bridge to
AppKit; no other library is bundled.

AccountDeck is an independent project. It is not made, endorsed or supported by Anthropic, OpenAI or Google.

## 1. Code adapted from ccswap

[ccswap](https://github.com/errhythm/ccswap), MIT License, at commit
`14df53cf3eba7c86a10d03698b958f7ba809b1c4`, is the only project AccountDeck adapts code from. The adapted
portions are in two files:

- `accountdeck/cli_locks.py`: Claude Code's lock protocol as ccswap's `src/claude_swap/claude_locks.py`
  implements it. Each lock is a folder created with `mkdir`. The OAuth refresh lock is taken before the legacy
  `~/.claude.lock`. A credential lock counts as stale after 60 seconds and the config lock after 10; a holder
  touches its lock every 3 seconds; a caller waits at most 9 seconds.
- `accountdeck/providers/claude_cli.py`: from ccswap's `src/claude_swap/credentials.py`, the list of credential
  keys every account shares (MCP and plugin sign-ins) and the rule that a switch takes those keys from the live
  login and everything else from the saved one; from `src/claude_swap/oauth.py`, the shape of the token renewal
  request and the SHA-256 fingerprint of a refresh token.

No ccswap file is included whole, and AccountDeck never runs ccswap. The following license applies to the adapted
portions:

```text
MIT License

Copyright (c) 2026 Onur Cetinkol

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 2. Logos from Simple Icons

The dashboard marks Claude accounts with the Claude logo and Codex accounts with the OpenAI logo. Both drawings
come from [Simple Icons](https://simpleicons.org), whose icons are released under CC0 1.0 (a public domain
dedication). They are the `claude` and `codex` entries of `ICONS` in `accountdeck/web/js/dom.js`.

The logos are trademarks of Anthropic and OpenAI. AccountDeck shows them only to tell which app an account
belongs to; showing them implies no affiliation or endorsement. Antigravity accounts are marked with a glyph
of AccountDeck's own (the `antigravity` entry), not with Google's logo.

## 3. Downloaded by the installer, not included

`install.sh` downloads three components from their publishers, checks each against the SHA-256 pinned in
`install.sh`, and installs them in `~/Library/Application Support/AccountDeck2/runtime`. They are not part of
this package or of the repository. Each keeps its own license files where it is installed. The versions pinned
for this release:

| Component | Version | Source | License |
|---|---|---|---|
| CPython, built by python-build-standalone | 3.14.8, build 20261003, Apple silicon, `install_only` | [astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone) | Python Software Foundation License Version 2 (installed as `runtime/python/lib/python3.14/LICENSE.txt`). The build also contains libraries such as OpenSSL and SQLite, each under its own terms. |
| cffi | 2.1.1 | [pypi.org/project/cffi](https://pypi.org/project/cffi/) | MIT-0 |
| cryptography | 50.0.1 | [pypi.org/project/cryptography](https://pypi.org/project/cryptography/), source at [pyca/cryptography](https://github.com/pyca/cryptography) | Apache-2.0 or BSD-3-Clause |

## 4. Ideas, not code (not redistributed)

These projects were studied while AccountDeck was designed. None of their code is in AccountDeck, and none of
them is bundled, installed or run. They are listed as credit. The licenses are the ones their repositories carried
at the revision studied.

| Project | Revision studied | License | What it informed |
|---|---|---|---|
| [AI Account Hub](https://github.com/AlexC1991/AI_Account_Hub) | `4ed637e670a65be44dbcad44ca8c7699615b50ed` | MIT | One account workflow across apps, launching the official tools, reading usage through the Codex app-server, enrolling Desktop and terminal accounts separately, guarded synchronization |
| [Claude Account Switcher](https://github.com/SnlperStripes/claude-account-switcher) | `d64fd68da061090b6b8280cfa93bc348978d0805` | Apache-2.0 | Which Claude Desktop sign-in fields to exchange, the Chromium encryption format, the cookie hand-off, letting processes settle |
| [ccswap](https://github.com/errhythm/ccswap) | `14df53cf3eba7c86a10d03698b958f7ba809b1c4` | MIT | Beyond the code in section 1: model-scoped Claude usage, refresh-token ownership, Codex switching by the sign-in file alone, refusing a Codex setup that does not keep its login in a file, fail-closed polling, the consume gate, the passive mirror and reset-aware switching |
| [claude-code-sessions](https://github.com/craigstoller/claude-code-sessions) | `c98888fa45d5a99c72f1c33a8cc90ff007284356` | MIT | Session identity, not moving account-specific connectors or permissions, conflict-aware rollback |
| [ai-multi-instance](https://github.com/Zoltak-Dev/ai-multi-instance) | `4a977e5c2232f719fde98e023ba71f262ed7c7aa` | MIT | Reading Claude usage through the sign-in cookie as a fallback, capturing it once while Claude Desktop is closed |
| [Codex Account Switcher](https://github.com/korsun009/codex-account-switcher) | `709ca1c91fdb0e885b7c0cc5337769149f2a20ca` | MIT | An encrypted local vault; configuration and history shared by every account |
| [Codex Account Switcher](https://github.com/liuzhao1225/codex-account-switcher) | `7312cfac3184b77e756a704748a9dab04b443ef8` | MIT | Checking an account's identity before and after handing it over |

The design research also credits these projects, by name and license only:

- claudepot (ISC): preflight checks, finding an app's process family, classifying status.
- CodexBar (MIT): reset boundaries, compact rows, pace.
- claude-swap (MIT): retry timings.
- ai-usagebar (MIT): telling which account is active.
- 10Lollipop (MIT): stopping a switch when something changes under it.
- [openai/codex](https://github.com/openai/codex) (Apache-2.0): how the app-server and its login interface behave.
