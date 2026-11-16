# Security policy

AccountDeck keeps Claude, Codex and Antigravity sign-ins on your Mac and switches the apps that use them. A
flaw in it can expose a sign-in or put the wrong account in front of someone's work, so security reports are
welcome.

## Supported versions

Only the latest release receives security fixes. Before you report, check whether you have it: the
dashboard's update prompt and **Check Now** in Settings → About say whether a newer release exists, and so does
`bash "$HOME/Library/Application Support/AccountDeck2/app/get.sh" --check`.

## Reporting a vulnerability

Report it privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**
([direct link](https://github.com/boha-k/accountdeck/security/advisories/new)). Never report it in a public
issue, pull request or discussion. If the **Report a vulnerability** button is missing, open an issue (the
bug report form will do) that says only that you have a security report, with no details, and wait to be
contacted.

Please include:

- the AccountDeck version (`run.py --version`) and the macOS version;
- which app is involved: Claude Code, Claude Desktop, Codex or Antigravity (`agy`);
- what you did, what you expected and what happened, as steps someone else can follow;
- the output of `run.py --doctor`, when it helps.

The [guide](docs/GUIDE.md#command-line) shows how to run `run.py` from an installed copy.

### Never include secrets

Never put any of these in a report, an issue or an attachment: tokens, cookies, API keys, the dashboard URL
(it carries a session token after `#token=`), the `vault/` folder, `accounts.sqlite3`, `runtime.json`, an app's
credential files (such as `~/.codex/auth.json`, `~/.claude/.credentials.json` or a sign-in file of `agy`'s
under `~/.gemini/`), Keychain values, `logs/agy-usage.log` (`agy`'s own log, which can name your email
address), or a log you have not read through. If the problem involves a secret, describe its shape (its
length, which field it sits in) instead of its value.

Antigravity sign-ins are Google sign-ins with a refresh token. AccountDeck keeps them as it keeps the others,
encrypted in its vault. It moves one whole, only between `agy`'s Keychain item and the vault, writes one to
the item on the standard input of `security`, never in a process argument, and never uses one itself or
sends it anywhere.
`logs/agy-usage.log`, the log `agy` writes during AccountDeck's usage read, is made new for each read and
only you can read it.

AccountDeck checks whether T3 Code's own Antigravity folder exists, to decide whether to show the note
about its separate sign-in. It reads and writes nothing in that folder.

`run.py --doctor` output is safe to share: it holds versions, check results and the names of keys and files,
never a token, a cookie or an email address. It does name your accounts by their labels in AccountDeck, with
any email address in a label masked; look the labels over first if they say more than you want to share.

## Scope

In scope, for example:

- anything that exposes a saved or live sign-in: to a log, a crash file, the dashboard, another app or the
  network;
- anything that lets another local user, another app or a web page read from or drive the AccountDeck service,
  which should answer only its own dashboard on 127.0.0.1;
- anything that makes AccountDeck switch the wrong account, put back an older or already used login, or lose a
  saved sign-in;
- anything that makes AccountDeck itself send a request to Google or use an Antigravity sign-in;
- the installer or `get.sh` running code that is not the published release;
- the update check: anything that makes its request carry more than AccountDeck's version, or lets GitHub's
  answer, or whoever answers in its place, show text in the dashboard, reach a command line, or start an update
  you did not confirm.

Out of scope:

- flaws in Claude, Claude Code, Claude Desktop, Codex, Antigravity or the services behind them; report those to
  Anthropic, OpenAI or Google;
- a Mac that an attacker already controls as your user or as root;
- reports that need a modified copy of AccountDeck.

## What happens next

You get an answer in the advisory. Once a fix is released, the advisory is published with credit to you, unless
you ask not to be named.
