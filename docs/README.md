# AccountDeck documentation

Start with the project's [README](../README.md): what AccountDeck is, how to install it and the first
run.

## Using AccountDeck

| Document | What it covers |
|---|---|
| [GUIDE.md](GUIDE.md) | How AccountDeck behaves: the menu bar, switching and auto-switch for Claude Code in terminals, Claude Desktop and Codex, Antigravity and Google's terms for it, usage and alerts, reliability, updates, the command line |
| [UPGRADING.md](UPGRADING.md) | The update prompt, how an update runs and what it keeps, the sign-in snapshot, rollback, older versions, what the update check sends, `get.sh --check` |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | The health report, the logs, and what to do about Keychain prompts, notifications, Codex settings, Antigravity, sign-ins, the update prompt and failed updates |

## Working on AccountDeck

| Document | What it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The pieces, how a usage read and a switch flow through them, how Antigravity sits apart, the rules nothing may break |
| [VALIDATION.md](VALIDATION.md) | What the automated tests cover and the checks that need a real Mac with real apps |
| [mac/](mac/) | The research and build plan of the Windows-to-Mac port (October 2026): a historical record, cited by code comments |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Setting up, the tests, the demo and how to send a change |
| [../SECURITY.md](../SECURITY.md) | How to report a security problem |

The design notes in `mac/`:

- [mac/PLAN.md](mac/PLAN.md): the build plan of the Mac port and the one-time move from the PC.
- [mac/PLATFORM-MAC.md](mac/PLATFORM-MAC.md): macOS facts: processes, menu bar, notifications, start at
  login, the app bundle, the pinned runtime, paths and the Keychain.
- [mac/PROVIDERS-MAC.md](mac/PROVIDERS-MAC.md): Claude Desktop, Claude Code and Codex on macOS: where
  each keeps its sign-in, encryption formats and token rotation.
- [mac/CONTRACT-ADDITIONS.md](mac/CONTRACT-ADDITIONS.md): the Mac's additions to the dashboard's state
  contract and the import actions.

[images/](images/) holds the screenshots used by these pages. They come from demo mode
(`run.py --demo`), which shows synthetic accounts only.
