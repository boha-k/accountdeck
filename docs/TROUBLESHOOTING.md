# Troubleshooting

Start with the health report: it checks everything AccountDeck depends on and says what to do. Then look
for your problem below. Each entry says what you see and what to do.

## Contents

- [The health report](#the-health-report)
- [Logs](#logs)
- [Installing](#installing)
- [macOS asks for Keychain access](#macos-asks-for-keychain-access)
- [No notifications](#no-notifications)
- [Codex: "Codex keeps its sign-in in the macOS Keychain"](#codex-codex-keeps-its-sign-in-in-the-macos-keychain)
- [Claude Code: a custom `CLAUDE_CONFIG_DIR`](#claude-code-a-custom-claude_config_dir)
- [Antigravity](#antigravity)
- [Antigravity: "agy keeps its sign-in in a file on this Mac"](#antigravity-agy-keeps-its-sign-in-in-a-file-on-this-mac)
- [Limit resets](#limit-resets)
- [Port 53157 is busy](#port-53157-is-busy)
- ["Sign In Again"](#sign-in-again)
- ["Another AccountDeck 2 is managing your sign-ins"](#another-accountdeck-2-is-managing-your-sign-ins)
- [AccountDeck found damage in its database](#accountdeck-found-damage-in-its-database)
- [Open at login does not start it](#open-at-login-does-not-start-it)
- [No update prompt](#no-update-prompt)
- [Update… is disabled](#update-is-disabled)
- [An update was rolled back](#an-update-was-rolled-back)
- [Reporting a problem](#reporting-a-problem)

## The health report

In Terminal:

```sh
"$HOME/Library/Application Support/AccountDeck2/runtime/python/bin/python3.14" -E -s \
  "$HOME/Library/Application Support/AccountDeck2/app/run.py" --doctor
```

It prints one line per check (`OK`, `WARN` or `FAIL`) and, under a line that needs it, what to do. It
covers the service, the runtime, the database, Claude Code's files and Keychain item, each terminal
account, auto-switch, Antigravity (`agy`, its Keychain item by its attributes only, the saved accounts),
notifications, disk space and the last hour's log warnings. It only reads: it changes nothing, shows no
secret and no email address, and works whether AccountDeck is running or not.
It exits 0 when healthy and 1 when something fails. Add `--json` for the same report as JSON.

The dashboard runs the checks every 5 minutes and shows them under **Health**. For Antigravity, it uses
the remembered sign-in state and `agy` version; until the version is checked it says so. Opening
AccountDeck or acting on Antigravity checks `agy --version` once per installed file during a service run.
The report run by hand shows no `agy` version and does not run `agy`.

## Logs

| Log | What is in it |
|---|---|
| `~/Library/Application Support/AccountDeck2/logs/accountdeck2.log` | AccountDeck's own log (1 MB, 5 older files kept) |
| `~/Library/Logs/AccountDeck2/launchd.log` | what the process printed when it was started at login |
| `~/Library/Application Support/AccountDeck2/startup-error.txt` | present only after a start that failed: the kind of error |
| `~/Library/Application Support/AccountDeck2/logs/agy-usage.log` | `agy`'s own log of AccountDeck's last Antigravity usage read, made new for each read |

AccountDeck's logs never contain tokens, cookies, keys or email addresses. `agy-usage.log` is `agy`'s own
log, not AccountDeck's: it can name the account's email address, so only you can read it. **Settings →
Advanced → Diagnostics** shows the recent log in the dashboard.

## Installing

The installer stops with a message and changes nothing in these cases:

| It says | What to do |
|---|---|
| run it as yourself, not with sudo | Run the same command without `sudo`. |
| … runs only on a Mac with Apple silicon (M1 or later) / needs a Mac with Apple silicon | Intel Macs are not supported. |
| AccountDeck 2 needs macOS 14 (Sonoma) or newer | Update macOS in System Settings › General › Software Update. |
| could not download … Check the internet connection | Check the connection (and any proxy) and run it again. |
| … did not match its pinned SHA-256 | A download was damaged or altered. Run it again; if it keeps failing, report it. |
| AccountDeck 2 is in the middle of a switch | Let the switch finish, then run the installer again. |
| another install or update of AccountDeck 2 is running. Let it finish, then run install.sh again. Nothing was changed. | An install or update is already running, perhaps in another Terminal window. Let it finish, then run yours again if you still need it. If none is running, restart the Mac and try again. |
| the install lock … could not be made (…). Nothing was changed. | The installer could not create or lock its file in `~/Library/Application Support/AccountDeck2`; check disk space and folder permissions. A directory named `.install.lock` or `.install.lock.guard` also blocks it. The ordinary `.install.lock.guard` file stays after installation and should not be removed: macOS releases its lock automatically. |
| AccountDeck 2 is running. Choose Quit in its menu bar menu … | You used `--no-launch`: quit AccountDeck first, or run without it. |
| the test suite failed on the new code | Only from a git checkout: your checkout's code does not pass its tests. Use a release, or fix the code. |
| Stopped: … Nothing was changed. | `get.sh` could not fetch or check the release. The message says why. |
| the installed AccountDeck 2 is version …, newer than this one | You asked for an older version. See [UPGRADING.md](UPGRADING.md#installing-an-older-version). |
| the saved accounts could not be counted before the update | The database could not be read. Run the [health report](#the-health-report) and report it. |

## macOS asks for Keychain access

AccountDeck keeps its own vault key in your login keychain as `AccountDeck2 vault`. It creates and reads
that item through Apple's `security` tool, so macOS does not ask about it.

The logins of other apps are different. To switch and read them, AccountDeck reads Claude Code's
`Claude Code-credentials` item, for Claude Desktop the `Claude Safe Storage` key and for Antigravity the
`gemini` item `agy` keeps, also through `security`. macOS may then show "security wants to use your
confidential information stored in … in your keychain".

- Choose **Allow**. **Always Allow** is not needed, and it would let any program that uses `security`
  read that item without asking.
- If macOS asks for your login keychain password, that is the password you log in to your Mac with.
- If you chose **Deny** or did not answer in time, AccountDeck says "Claude Code's login in the keychain
  could not be read. Unlock the keychain, or allow AccountDeck when macOS asks, and try again. Nothing
  was changed." or "The keychain did not answer in time. If macOS is asking for permission, answer it and
  try again." Try the action again and choose Allow.
- "Open Claude Desktop on this Mac once, then try again" means Claude Desktop has not created its key
  yet. "macOS did not let AccountDeck read Claude Desktop's key" means the Keychain refused: unlock it or
  choose Allow when macOS asks.
- For Antigravity the section says "The keychain is locked, or macOS did not let AccountDeck read
  Antigravity's sign-in. Unlock it, or choose Allow, and try again." or "The keychain did not answer in time
  when AccountDeck read Antigravity's sign-in. If macOS is asking for permission, answer it and try again."

Do not delete `AccountDeck2 vault` in Keychain Access while you have saved accounts: without it the vault
cannot be opened. AccountDeck then says "This saved sign-in could not be read. Reconnect this account."
and you have to sign in to each account again. It never makes a new key over old records.

## No notifications

macOS asks once, the first time AccountDeck starts, whether AccountDeck 2 may send notifications.

- **You chose Don't Allow.** The menu bar menu shows **Turn On Notifications…**, which opens System
  Settings › Notifications. Allow AccountDeck 2 there. Until you do, **Settings → Notifications** in the
  dashboard says "Mac notifications are off".
- **The menu shows "Notifications Unavailable — Reinstall AccountDeck 2".** macOS refused this copy's
  identity. Run the installer again; it rebuilds and re-signs the app.
- **Nothing arrives.** **Settings → Notifications → Send Test** sends one. Quiet hours hold back all but
  critical alerts, and an alert is raised once per limit cycle.

## Codex: "Codex keeps its sign-in in the macOS Keychain"

AccountDeck switches Codex by exchanging `~/.codex/auth.json`, so Codex must keep its sign-in in that
file. That is Codex's default. If `~/.codex/config.toml` sets `cli_auth_credentials_store` to `keyring`
or `auto`, switching there would not take effect, so AccountDeck refuses and names the file.

Set it to `"file"` (or remove the line) in `~/.codex/config.toml`:

```toml
cli_auth_credentials_store = "file"
```

Codex may then ask you to sign in once more, since its sign-in was in the Keychain. Or keep the Keychain
and switch Codex accounts in Codex yourself.

"Install Codex CLI and sign in once, then try again." means AccountDeck found no Codex CLI. It looks on
your `PATH`, at `~/.local/bin`, and inside ChatGPT.app.

## Claude Code: a custom `CLAUDE_CONFIG_DIR`

AccountDeck manages Claude Code's default login: the `~/.claude` folder and the `Claude Code-credentials`
Keychain item. A terminal started with `CLAUDE_CONFIG_DIR` or `CLAUDE_SECURESTORAGE_CONFIG_DIR` set keeps
its own login in another Keychain item, which AccountDeck does not switch.

When AccountDeck itself runs with either variable set, terminal management is read-only and the
dashboard says "A custom CLAUDE_CONFIG_DIR isn't supported yet". The health report warns about it too
when you run it from a terminal that has the variable. Unset both variables for AccountDeck (and for the
terminals you want it to manage).

## Antigravity

The **Antigravity accounts** section says what is wrong in a note above its accounts, on a row, or as the
reason a button can't be used (hold the pointer over it). Each row below gives the words and what to do.
In the messages, `{email}` is the earlier account's address (or "the earlier sign-in"), `{label}` is its
saved name, and `{message}` is the explanation the Add shows while it waits.

| It says | What to do |
|---|---|
| agy wasn't found on this Mac | AccountDeck looks for `agy` at `~/.local/bin/agy`, `/opt/homebrew/bin/agy` and `/usr/local/bin/agy`, then on your `PATH`. Install `agy` again; your saved accounts stay meanwhile. |
| Saving and switching aren't available right now | The line under it says why. The keychain messages are under [macOS asks for Keychain access](#macos-asks-for-keychain-access); the file store has [its own entry](#antigravity-agy-keeps-its-sign-in-in-a-file-on-this-mac). Until it is cleared, AccountDeck saves and switches no Antigravity account and leaves `agy`'s sign-in as it is. |
| Antigravity is signed in with a work or Google Cloud sign-in. AccountDeck handles personal Google sign-ins only. | Work, Gemini Enterprise and Google Cloud sign-ins are not supported. AccountDeck changes nothing. |
| Antigravity's sign-in in the keychain is in a form AccountDeck does not know, so it leaves it alone. / Antigravity's sign-in in the keychain lacks a part AccountDeck needs (its refresh token or its e-mail). | `agy` may still work with it; AccountDeck can't save it or put another in its place. A newer `agy` may keep its sign-in in a new form: [report it](#reporting-a-problem). |
| The keychain did not answer in time when AccountDeck looked at Antigravity's sign-in. AccountDeck looks again in a minute. | The attributes check did not answer within 5 seconds. Antigravity's sign-in shows as unreadable for a minute before AccountDeck checks again. If macOS is asking for permission, choose **Allow**. |
| agy is running / 2 agy sessions are running | Switching waits until every `agy` has ended, and Add can't start. AccountDeck never stops `agy`: end the sessions yourself (quit them in their terminals). An `agy` that another user of this Mac runs counts too. A waiting switch goes on by itself once they have ended. |
| agy was still running when this switch's confirmation expired. Nothing was changed; review the switch again. | A switch waits at most 30 minutes. Quit `agy`, then choose **Switch** again. |
| agy started again. Review the switch again. | Another `agy` started while the switch waited. Nothing was changed; the switch shows again with the sessions now running, for you to confirm or cancel. |
| agy started again. Nothing was changed. | An `agy` started just before the switch would have changed its sign-in. Quit it and choose **Switch** again. |
| agy's sign-in changed. Review the switch again. | `agy` was signed in to another account after you confirmed. Nothing was changed; choose **Switch** again. |
| agy's sign-in did not read back as the selected account. The earlier sign-in is back. | The keychain item did not hold the selected account after the switch wrote it, so AccountDeck put the earlier sign-in back. Try again; if it keeps happening, [report it](#reporting-a-problem). |
| Quit agy first: adding an account signs agy out, and AccountDeck never stops it. | Add takes `agy`'s sign-in out while you sign in, so a running session would be signed out. Quit every `agy`, then choose **Add Antigravity Account** again. |
| Close every agy session first: while you add an account agy has no sign-in on this Mac, so a running session would be signed out. / Close every agy session first: a running session would move to the account put back. | An `agy` session is running: it was there when you chose the action, or it started just before AccountDeck would have written. Nothing was changed. Quit every `agy` yourself, then choose the action again. |
| agy's sign-in changed just now, so AccountDeck changed nothing. Try again. | `agy`'s sign-in changed in the moment between AccountDeck's look at it and its write for a Switch, an Add or a Recover. Nothing was changed; choose the action again. |
| Antigravity's usage is being checked. Try again in a moment. | Let AccountDeck's usage read finish, then choose **Add Antigravity Account** again. |
| Close agy's sign-in window. AccountDeck puts {email} back as soon as agy has closed. | The Add is ending. Close its Terminal sign-in window; the earlier sign-in comes back only once no `agy` runs. A Google sign-in that finishes meanwhile is saved and kept, even after **Cancel**. |
| Close every agy session. AccountDeck puts {email} back as soon as none runs. | The sign-in window has closed, but another `agy` still runs. End those sessions to let the Add finish putting the earlier sign-in back. |
| The keychain is locked or did not answer, so AccountDeck cannot put {email} back yet. Unlock it, or choose Allow when macOS asks: AccountDeck tries again in a moment. | The Add stays open while AccountDeck waits for the keychain. Unlock it, or choose **Allow** when asked; AccountDeck tries again by itself. |
| AccountDeck could not put {email} back yet. It tries again in a moment. | The earlier sign-in could not be written or did not read back as expected. AccountDeck keeps the Add open and tries again. |
| Wait until the Add has ended. {message} | Another Antigravity action must wait. Follow the explanation after the first sentence; cancelling the Add does not end this wait while `agy` still runs. |
| Nobody signed in within 15 minutes, so the Add ended. The earlier sign-in is back. | Nothing is lost. Choose **Add Antigravity Account** again and finish the Google sign-in in the Terminal window within 15 minutes. |
| agy's sign-in window closed before anyone signed in, so the Add ended. The earlier sign-in is back. | Closing the window ends the Add without waiting for 15 minutes. The earlier sign-in has been restored after every `agy` closed. |
| Cancelled. The earlier sign-in is back. | The Add has ended after **Cancel**, and `agy` has its earlier sign-in again. |
| The sign-in had finished, so it stays and the earlier one was not put back. | This follows the saved sign-in's message when a Google sign-in finishes before the cancelled Add ends. The new sign-in is saved and kept; the earlier one stays saved too. |
| AccountDeck cannot save a work or school sign-in, so it left the one in agy alone. | The Add has ended and that sign-in stays in `agy`. AccountDeck handles personal Google sign-ins only. If an earlier account was saved, the message below says how to return to it. |
| {label} is saved: to go back to it, sign out of the other account in agy first, then choose Switch. | This follows an Add that ends with a sign-in AccountDeck cannot save. To return to the saved account, sign out of that unsupported sign-in in `agy`, close every `agy`, then choose **Switch** in AccountDeck. |
| You signed in as …, not …. | During **Sign In Again** the browser signed in with another Google account. That sign-in is saved as an account of its own; choose **Sign In Again** once more and use the right account (a private browser window helps). |
| An Antigravity switch didn't finish | The switch could neither finish nor put the earlier sign-in back. Choose **Recover…** and pick the sign-in `agy` keeps: the one from before, the one the switch was moving to, or what it has now. Putting one back needs every `agy` closed. Recover leaves the same account alone, saves another account first, and writes nothing if the sign-in cannot be read. |
| Recover changes nothing until AccountDeck can read agy's sign-in. | The message before this sentence says why the current sign-in cannot be read. Resolve that first, then choose **Recover…** again. Keeping what `agy` has needs no write. |
| Sign-in needed | Google no longer accepts the account's sign-in ("Google no longer accepts this account's Antigravity sign-in. Sign in again."), or its saved copy is missing. Choose **Sign In Again**. |
| Open the dashboard and read the note about Google's terms for Antigravity first. | The menu bar says this when you switch before you have read [the note](GUIDE.md#googles-terms). Choose **Switch** in the dashboard once; it shows the note first. |
| Again in 42s | Usage can be read once a minute; **Refresh Usage** works again when the countdown ends. |
| Usage checks are paused. Turn them back on in Settings. | Paused usage checks pause Antigravity's too. |
| AccountDeck could not start checking Antigravity's usage. Try again in a moment. | The usage read did not start. Wait a moment, then choose **Refresh Usage** again. |
| Couldn't read usage: agy did not answer in time. | `agy` took over 60 seconds and was stopped. The last reading stays; try again later. |
| Couldn't read usage: AccountDeck could not read agy's answer. | `agy` gave no usage answer AccountDeck could accept: the command failed, or its answer is in a form AccountDeck does not know (perhaps after an update of `agy`). The last reading stays. Try `/usage` in `agy` itself; [report it](#reporting-a-problem) if it keeps happening, without posting `agy-usage.log`. |
| Couldn't read usage: agy could not be started. Check that it is installed. | Install `agy` again, or check that it runs in a terminal. |
| Usage read 3h ago / No usage reading yet | Only the account `agy` is signed in to can be read: on **Refresh Usage**, and when you open AccountDeck over 15 minutes after its last reading and the last try (unless **Read Antigravity usage when I open AccountDeck** is off in **Settings → General**). A saved account that is not signed in shows its last reading; switch to it to read it again. |

Change Antigravity accounts in AccountDeck, never with `/logout` in `agy`: it deletes the sign-in `agy`
keeps. The exception is an Add whose message asks you to sign out of an unsupported sign-in before
switching back.

## Antigravity: "agy keeps its sign-in in a file on this Mac"

The full message is "agy keeps its sign-in in a file on this Mac instead of the keychain. AccountDeck does
not handle that yet, so it saves and switches nothing." `agy` keeps its sign-in in a file instead of the
login keychain when it can't use the keychain, for example when it signed in over SSH. Those files are
`~/.gemini/antigravity-cli/antigravity-oauth-token` and `~/.gemini/jetski-standalone-oauth-token`.

AccountDeck handles only the keychain item in this version. It only looks whether one of these files
exists, and never opens, changes or removes it. While one exists, AccountDeck saves and switches no
Antigravity account, and the accounts saved before stay as they are.

## Limit resets

What the **Limit Resets** list, the **Use a Limit Reset…** sheet or its report can say
([the guide](GUIDE.md#limit-resets) explains the feature):

| It says | What to do |
|---|---|
| No chip and no **Limit Resets…** in a Claude account's menu | Check **Read Claude limit resets** in Settings → Advanced. It is on by default; frozen imported accounts are also excluded. |
| The number of limit resets is not known yet. | No reset read has succeeded. The list explains the last refusal or waiting time. Reads follow ordinary usage, at most hourly or every 15 minutes after Refresh; a refusal pauses them for six hours. |
| Anthropic offers this account's limit resets on claude.ai only. | Anthropic's answer for this account. Nothing can be used from AccountDeck: open claude.ai → Settings → Usage, signed in to that account. Other sentences in this place (the plan gets none, the seat gets none, turned off for now) are Anthropic's reasons too. |
| It can be used only once a limit it refills is reached. / Anthropic offers another of this account's limit resets first. / Anthropic has paused this limit reset for now. | That reset cannot be used now. The first two change by themselves; a paused one is Anthropic's to release. |
| No limit is below 80% left, so there is nothing to reset right now. | Codex applies a reset only when a limit it covers has less than 80% left. Nothing would be used, so AccountDeck does not ask. |
| Codex did not list this limit reset just now, so it cannot be chosen here. | Codex gave the count without the details. **Refresh** the account and try again. |
| Its last reading is over an hour old. Refresh it first. / Its limit resets were last read too long ago. Refresh it first. | A use starts from a recent reading. **Refresh** the account, wait for the reading, then choose **Use a Limit Reset…** again. |
| Its Codex sign-in is about to expire… / Its saved sign-in token has expired… / This account needs a new sign-in first. | AccountDeck never renews a sign-in to use a reset. Do what the sentence says (refresh the account, open it in its app once, or **Sign In Again**), then try again. |
| Anthropic refused the last read of this account's limit resets. AccountDeck reads them again later. | After a refusal (or three failed reads) AccountDeck waits 6 hours for that account. The usage readings are not affected. |
| This plan has expired. Review it again. / This limit reset changed. Review it again. | The sheet was open over 5 minutes, or the account's resets changed meanwhile. The sheet is drawn again; nothing was sent. |
| No limit reset was used: … | Nothing was spent. The reason follows: Codex had nothing to reset, the reset is no longer there, Anthropic refused the request or did not accept the sign-in, AccountDeck could not reach the provider. |
| It is not known whether the limit reset was used. AccountDeck is reading the account again. | The request went out and no answer came back. Wait for the next readings: the count going down means it was used. Choosing the same reset again repeats the same request, which cannot use a second one. |
| A limit reset may have been used at … | The warning stays until a complete reading settles it. Claude's ten-minute retry window is not a deadline for resolving the outcome: a missing count keeps the warning. The same Claude account's other app entry is blocked too. |
| Automatic limit resets stopped | With **Use a limit reset when every terminal account is out** on, a reading taken 10 minutes or more after a reset AccountDeck used by itself still showed that limit out (or what AccountDeck remembered of its last automatic reset could not be read). No further reset is used automatically. Look at the account's usage on claude.ai, then turn the setting off and on in Settings → Auto-Switch to start again. |
| A limit reset could not be used automatically | Every terminal account was out and the try used nothing; the reason follows, as in "No limit reset was used: …". AccountDeck tries again by itself after 10 minutes, then later each time (an hour at most), and takes another account's reset first after two tries. |
| Limit reset on …: outcome not known | An automatic use got no answer. As with your own use, the account is read again; until a reading settles it, AccountDeck uses no other limit reset by itself. |
| Every terminal account is out and no limit reset is used by itself | Check, in this order: the setting, **Switch accounts automatically** and **Read Claude limit resets** are on; every account auto-switch could use shows 97 % or more of its weekly or Fable weekly limit (the account in use: your switch point, if that is higher) in a reading under 15 minutes old (one with room, or one that cannot be read, holds it back, and so does a reading from before a limit reset used on that account or from the 10 minutes after it); no such limit refills by itself within 10 minutes; an account that is out holds a reset its list would let you use now and that refills the limit that is out (a reset that needs a limit reached first shows as usable only after the next hourly read; accounts left out of auto-switch are never reset); the last automatic reset was over an hour ago and a reading has shown its refill; no other action is running. The health report says whether automatic limit resets are on or stopped. |

## Port 53157 is busy

AccountDeck listens on `127.0.0.1:53157`. If another program holds that port when AccountDeck starts, it
waits up to 20 seconds (it may be an AccountDeck that is just quitting), then uses another free port and
says so. The menu bar item and the dashboard find it on whatever port it got; nothing else needs to
change. To see what holds the port:

```sh
lsof -nP -iTCP:53157 -sTCP:LISTEN
```

## "Sign In Again"

An account shows **Sign In Again** (or "Needs sign-in") when its saved login can no longer be used:

- a Claude Code login ended (they end about 30 days after you sign in; the **Sign-ins due** card lists
  the ones ending soon);
- Claude or OpenAI refused the saved login when AccountDeck tried to renew it;
- a renewal got no clear answer: AccountDeck never sends the same refresh token twice, so it asks you to
  sign in instead;
- Google refused an Antigravity account's sign-in when its usage was read, or its saved copy is missing.

For a Claude Code account, choose **Sign In Again**: a Terminal window opens, you sign in with that
account and close the window. For a Codex account, add it again with **Add Codex Account**. For an
Antigravity account, quit every `agy`, choose **Sign In Again** and sign in with Google from the Terminal
window that opens. Use a private browser window if the sign-in picks the wrong account.

An account Claude has put **On hold** is different: signing in again does not help, and AccountDeck does
not ask you to.

## "Another AccountDeck 2 is managing your sign-ins"

Another copy of AccountDeck 2 for your user (for example one started with another data folder) holds
the sign-ins, so this one is read-only. Quit the other copy from its menu bar item.

## AccountDeck found damage in its database

AccountDeck checks its database at start and daily, and keeps 7 daily backups in
`~/Library/Application Support/AccountDeck2/backups/`. When the dashboard says "AccountDeck found damage
in its database", choose **Quit AccountDeck 2** in the menu bar menu and open it again: at its start it
moves the damaged file aside and goes back to the newest good backup. Readings since that backup are
lost; sign-ins are not, as they live in the vault. With no good backup it stops at start and changes
nothing; report it.

## Open at login does not start it

If the menu bar says "macOS has AccountDeck 2 switched off in System Settings › General › Login Items",
switch it on there. **Open at login** in **Settings → Menu Bar & Login** (or **Start at Login** in the
menu bar menu) adds or removes the LaunchAgent itself.

## No update prompt

The prompt appears only when a release newer than yours has been published on GitHub, and only in an
installed copy. **Settings → About** says what the last check found:

- **"Update checks are off".** Turn on **Check for updates automatically**. While it is off AccountDeck never
  asks GitHub.
- **"Update checks are unavailable"** with "This copy runs from a source folder…": AccountDeck runs from a
  git checkout, which updates with `git pull` and `bash install.sh`. Safe mode and the demo never check
  either.
- **"Couldn't check for updates".** GitHub could not be reached, or did not answer as expected (offline, a
  proxy or firewall that blocks `github.com`, GitHub asking AccountDeck to wait). AccountDeck tries again
  by itself, waiting longer after each check that fails (up to an hour, or as long as GitHub asked, up to 6
  hours); **Check Now** tries at once unless GitHub asked to wait. `get.sh --check` asks github.com from
  Terminal and is worth a try.
- **"You skipped AccountDeck X".** You chose **Skip This Version**. **Check Now** offers it again; a later
  version is offered by itself.
- **"Not checked yet".** The first check runs 30 seconds after AccountDeck starts, or as soon as you open
  the menu or the dashboard.
- **"AccountDeck X is up to date".** There is no newer release. Commits on `main` between releases are not
  offered.

The notification comes once per version, waits for the end of quiet hours and is not sent while
notifications are off; the banner and the menu bar item still show the update.

## Update… is disabled

The banner says why, after "You have Y." (and so does the button's tooltip):

- "Finish or cancel the current action first." or "Finish or cancel the sign-in first.": a switch, another
  action or a sign-in is in progress, and restarting AccountDeck now would cut it short. Let it finish or
  cancel it, and the button comes back. An Antigravity Add that is ending says "Wait until the Antigravity
  Add has ended."; the Antigravity section says what it waits for ([Antigravity](#antigravity)).
- "An update is already running in a Terminal window.": an update you started from AccountDeck is still
  running. Follow it in that window and let it finish.
- "This copy has no get.sh to update with. …": the installed app folder lacks the updater. Install the
  update with the one-line command in the [README](../README.md#install).

If the Terminal window opens and the installer stops with a message, the table under
[Installing](#installing) says what it means.

## An update was rolled back

The installer printed "AccountDeck 2 … failed its health check" or "AccountDeck 2 … lost saved accounts",
put the previous version back and exited with status 1. AccountDeck keeps running the version you had, with your
data as it was. After a failed health check the message names the logs; when accounts were missing it names
the snapshot folder in `update-backups/` instead. [UPGRADING.md](UPGRADING.md#rollback) explains what happened
and how a snapshot can be put back by hand.

## Reporting a problem

Open an [issue](https://github.com/boha-k/accountdeck/issues) with what you did, what you saw, the
output of `run.py --version` and the health report. Both are free of secrets; still read them before you
paste, and never paste a token, a cookie or a Keychain value. Security problems go privately, as
[SECURITY.md](../SECURITY.md) explains.
