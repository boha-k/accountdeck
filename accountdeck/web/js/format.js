/* AccountDeck 2 — the copy catalogue and text helpers.
 * Mac wording throughout: buttons and menu items in title case ("Switch Now", "Rename…"), labels and
 * descriptions in sentence case. Countdown and clock rules live in AD.model so the page and
 * contract.py never disagree. Templates use {name} placeholders; fill() substitutes them. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};

  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

  function freeze(object) {
    for (const value of Object.values(object)) {
      if (value && typeof value === 'object') freeze(value);
    }
    return Object.freeze(object);
  }

  const STRINGS = freeze({
    app: 'AccountDeck 2',
    apps: { claude: 'Claude Desktop', codex: 'Codex', claude_cli: 'Claude Code', antigravity: 'Antigravity' },
    provider: { claude: 'Claude', codex: 'Codex', claude_cli: 'Claude Code', antigravity: 'Antigravity' },

    shell: {
      version: 'AccountDeck {version}', updated: 'Updated {age} ago', updatedNow: 'Updated just now',
      checking: 'Checking now…', paused: 'Usage checks paused', stillChecking: 'Still checking… last update {age} ago',
      refreshed: 'Accounts refreshed.', themeChanged: 'Appearance: {mode}',
      quitTitle: 'Quit AccountDeck 2?',
      quitBody: 'Usage checks, auto-switch and alerts stop until you open it again. Claude and Codex keep running as they are.',
      shortcuts: 'Keyboard Shortcuts', demo: 'Demo clock', demoOffset: '{sign}{offset} from the demo time',
      demoNow: 'At the demo time', switching: 'Switching…',
      build: '{commit} · installed {date}', buildDirty: '{commit} with uncommitted changes · installed {date}',
      buildPackage: 'installed {date}', buildTip: 'Installed {date} at {clock}',
    },
    connection: {
      offlineTitle: "Can't reach AccountDeck 2", offline: "The app isn't responding. {reopen}",
      signinTitle: 'This page is signed out', closedTitle: 'AccountDeck 2 has quit',
      network: "Can't reach AccountDeck 2.", failed: "That didn't go through. Try again.",
    },
    status: { ok: 'Operational', degraded: 'Degraded', outage: 'Incident', unknown: 'Status unknown' },

    window: {
      resetsIn: 'Resets in {countdown}', resetsAt: 'Resets at {clock}', backIn: 'Back in {countdown}',
      resetting: 'Resetting…', resettingTip: 'Should have reset at {clock} — confirming',
      expected: 'Refill expected · unconfirmed', notStarted: 'Not started', startsNext: 'Starts with your next message',
      refilled: 'Refilled at {clock}', notReported: 'Not reported', noSession: 'No 5-hour limit',
      noSessionTip: 'Claude no longer gives this account a 5-hour limit: only its weekly limits apply.',
      temporary: 'Temporary until verified',
      temporaryTip: 'Claude gives a new Ultra account this 5-hour limit until it verifies the account. Verification is automatic and Claude emails you when it is done; the limit then goes away.',
      elsewhere: 'Terminals unaffected',
      elsewhereTip: 'Claude locked this limit, but your terminals do not depend on it, so auto-switch keeps using this account.',
    },

    overview: {
      inUse: 'In use by your terminals', nobodyInUse: 'No account in use',
      nobodyInUseLine: 'Switch to an account below and every terminal follows it.',
      limitCaption: '{window} limit', waiting: 'Waiting for the first reading',
      auto: 'Auto-switch', autoToggle: 'Switch accounts automatically',
      autoOff: 'Auto-switch is off', autoOffLine: 'Terminals stay on {label} until you switch.',
      autoOffNoActive: 'Pick an account below when you need one.',
      takesOver: 'Takes over when {label} reaches {n}% (the notch on each bar).',
      takesOverPlain: 'Takes over when the account in use reaches {n}% (the notch on each bar).',
      room: '{n}% room', roomTip: '{n} points below the {t}% switch point',
      nobody: 'No account can take over', nobodyLine: 'Every other account is in use, out of room or left out.',
      nobodyAlone: 'Add another terminal account so auto-switch has somewhere to go.',
      then: 'Then', waitingList: 'Waiting', hold: 'On hold: {reason}', best: 'Most room right now:',
      settingsLink: 'Auto-Switch Settings…',
      nextReset: 'Next reset', refilled: 'Refilled', refilledLine: "{label}'s {window} limit is full again.",
      allClear: 'All clear', allClearLine: 'Nothing with much used resets in the next 24 hours.',
      unknownTitle: 'Waiting for readings', unknownLine: 'Usage appears after your next message in Claude Code.',
      resetWindow: '{window} limit', refillsAt: 'Refills at {clock}',
      fableRoom: '{label} room', fableFallback: 'Fable 5.1 weekly', noFable: 'No readings yet',
      noFableLine: 'They appear after a Fable 5.1 message in Claude Code.',
      leaderLine: '{n}% left · resets {clock}', tie: 'Tied for the most room', fableTag: 'Most Fable room',
      left: '{n}% left', notReported: 'not reported', show: 'Show {label} in the list', rank: '{n}',
      total: 'Claude usage left', totalWho: 'All accounts', totalAccounts: '{n} accounts', totalAccountsOne: '1 account',
      totalOut: '{n} out of room', totalSignin: '{n} need sign-in', totalSigninOne: '1 needs sign-in',
      totalHeld: '{n} on hold', totalLeftOut: '{n} left out',
      totalUnread: '{n} not read yet', totalCaption: '{window} limit · of {n}%', totalValue: '{left}% left of {total}%',
      totalRefill: 'Next refill in {countdown}',
      due: 'Sign-ins due', dueMore: '{n} more in the list below',
      reaction: 'Moved {seconds} s after {label} hit its {limit} · {clock}', reactionSomeone: 'an account',
      reactionTip: 'A session hit the limit and your terminals moved to another account {seconds} seconds later.',
    },

    // The terminal pool's forecast (providers.claude_cli.runway), one line in the total.
    runway: {
      lasts: 'At this pace: lasts past the next refill', lastsWeek: 'At this pace: lasts all week',
      out: 'At this pace: runs out around {when} · next refill {clock} ({label})',
      outNoRefill: 'At this pace: runs out around {when} · no refill known after that',
      low: 'Not enough recent use to forecast',
      tip: 'Your terminals used about {pace}% of a weekly limit an hour over the last {span}; {room}% is left across {accounts}.',
    },

    // Terminal logins end about 30 days after sign-in (accounts[].login_ends_at, providers.claude_cli.signin_due).
    logins: {
      ends: 'Login ends {clock}', ended: 'Login ended', needs: 'Needs sign-in',
      endsTip: 'Claude Code logins end about 30 days after you sign in. Sign in again before then to keep using this account.',
      endedTip: 'This login has ended. Sign in again to use this account.',
    },

    // Sessions a limit stopped (providers.claude_cli.watch).
    watch: {
      stoppedOne: '1 session stopped at a limit', stopped: '{n} sessions stopped at a limit',
      message: 'Your terminals now use {label}. Type “continue” in each one to pick up where it left off.',
      messageWait: 'No account has room for them yet. Type “continue” in each one once a limit refills.',
      never: 'AccountDeck never types into your sessions.',
      unnamed: 'Unnamed session', row: '{where}{limit} · {clock}',
      limits: { session: '5-hour limit', weekly: 'weekly limit', model: 'model limit' },
      off: "AccountDeck isn't watching your sessions for limit errors right now, so it switches on usage readings alone.",
    },

    // Usage credits: real money at API prices once a limit is used up (accounts[].credits, "Usage credits").
    credits: {
      on: 'Credits on', onTip: 'Usage credits are on: past a limit, Claude keeps working at API prices and charges this account.',
      spent: '{amount} spent this month', spentTip: 'Usage credits spent this month: {amount}{limit}, at API prices.',
      limit: ' of the {amount} monthly limit',
    },

    // Limit resets (accounts[].resets and reset_credits; resets.js): what an account holds, its Limit Resets sheet,
    // and Use a Limit Reset…, which asks for a plan (reset-plan) and the user's yes before use-reset. "limit reset" is
    // lower case inside a sentence; buttons and menu items are title case, as everywhere on the page.
    resets: {
      one: '1 limit reset', many: '{n} limit resets',
      tipBy: '{count} · first use by {date}', tipNeverOne: '{count} · does not expire', tipNever: '{count} · they do not expire',
      chipTip: 'Show the limit resets of {label}',
      doubt: 'A limit reset may have been used at {clock}. AccountDeck is reading this account again.',
      heading: 'Limit resets', sheetTitle: 'Limit Resets for {label}', unnamed: 'Limit reset',
      times: '{title} ×{count}', refills: 'refills {what}', useBy: 'use by {date}', never: 'does not expire',
      none: 'No limit resets.', unknown: 'The number of limit resets is not known yet.', checked: 'Checked {age} ago', notChecked: 'Not read yet.',
      menu: 'Limit Resets…', use: 'Use a Limit Reset…', unavailable: "A limit reset can't be used right now.",
      planTitle: 'Use a limit reset?', choose: 'Which limit reset to use', confirm: 'Use Limit Reset', keep: 'Keep It',
      changed: "This account's limit resets changed. Please review again.",
      noAnswer: "AccountDeck didn't answer, so it isn't known whether this was sent. Close this sheet and look at the account before you try again.",
      progress: 'Using a limit reset', progressOn: 'Using a limit reset on {label}',
      // The report's title by what happened (its summary, the service's words, is the first line under it).
      report: {
        success: 'Limit reset used', nothing: 'No limit reset was used', unknown: 'Not known yet whether a limit reset was used',
        failed: "The limit reset didn't finish", cancelled: 'Limit reset cancelled',
      },
      total: '{n} limit resets available', totalOne: '1 limit reset available',
      setting: 'Read Claude limit resets',
      settingHint: 'On by default. One more read after usage, hourly or every 15 minutes after Refresh. Refused reads wait longer. Off: Claude limit resets are hidden and cannot be used, by you or automatically.',
    },

    // AccountDeck's own health (state.health from its doctor, state.service from its watchdog).
    health: {
      title: 'Health', ok: 'AccountDeck is working normally', okNotes: 'AccountDeck is working · {n} things to look at',
      okNote: 'AccountDeck is working · 1 thing to look at', bad: 'AccountDeck needs attention',
      checked: 'checked {age} ago', waiting: 'The first health check runs a minute after AccountDeck starts.',
      all: 'All checks ({n})', more: '+{n} more', show: 'Show Details',
      braked: 'AccountDeck keeps getting stuck',
      brakedAction: 'Usage checks and auto-switch may not run until you quit and reopen AccountDeck.',
      damaged: 'AccountDeck found damage in its database',
      damagedAction: 'Quit and reopen AccountDeck: at its start it goes back to its newest good backup.',
      restored: 'At its start it went back to the database backup of {clock}.',
      watchdog: 'Watchdog {state}', watchdogStates: { off: 'off', watching: 'on', braked: 'stopped restarting', stopping: 'restarting' },
      restarts: '{n} restarts in the last hour', restartsOne: '1 restart in the last hour',
      dbChecked: 'database checked {age} ago', backups: '{n} backups', backupsOne: '1 backup', noBackup: 'no backup yet',
    },

    accounts: {
      title: 'Claude Code accounts', count: '{n} accounts', countOne: '1 account',
      sub: 'Every open terminal follows the account in use.', add: 'Add Account', sortLabel: 'Sort accounts',
      sorts: {
        room: 'Most room left', fable: 'Most Fable 5.1 room', weekly: 'Most weekly room',
        session: 'Most 5-hour room', reset: 'Soonest weekly reset', manual: 'My order',
      },
      emptyTitle: 'No terminal accounts yet',
      emptyText: 'Add a Claude Code account and every terminal on this Mac can share it. When one runs low, auto-switch moves your terminals to the next.',
      unavailableTitle: "Claude Code accounts aren't available", unavailableText: 'This build has no Claude Code (terminal) support.',
      colAccount: 'Account', switch: 'Switch', signIn: 'Sign In Again', more: 'More actions for {label}',
      blockedSignin: 'Usage is unavailable until you sign in again.',
      onHold: 'On hold',
      liveExpired: 'Usage updates after your next message in Claude Code.',
      checked: 'Checked {age} ago', paused: 'Checks paused until {clock}',
      tip: 'Switch accounts here, not with /login or /logout in Claude Code. Open terminals follow on their next message.',
      readOnlyOther: 'Another AccountDeck 2 is managing your sign-ins. Quit that copy to make changes here.',
      undo: 'Undo', undoLeft: 'Undo available for {countdown}', cancelAdd: 'Cancel',
      menu: {
        leaveOut: 'Leave Out of Auto-Switch', include: 'Let Auto-Switch Use It', rename: 'Rename…', remove: 'Remove…',
        removeActive: 'Switch to another account before removing this one.', switchTo: 'Switch…', signIn: 'Sign In Again…',
      },
      addSheet: {
        title: 'Add a Terminal Account',
        body: 'A Terminal window opens. Sign in there with the account you want to add, then close that window to finish.',
        hint: 'If the browser signs in to the wrong account, use a private window.', open: 'Open Terminal',
        againTitle: 'Sign In to {label} Again',
        againBody: 'A Terminal window opens. Sign in there with {who}, then close that window to finish. AccountDeck keeps the new login for {label}.',
      },
      rename: { title: 'Rename Account', label: 'Account name', save: 'Rename' },
      remove: {
        title: 'Remove “{label}”?', button: 'Remove',
        body: "AccountDeck forgets this terminal login. The account itself isn't affected, and you can add it again later.",
      },
    },

    codex: {
      title: 'Codex accounts', add: 'Add Codex Account', save: 'Save Account', capture: 'Save Current Account',
      sub: 'ChatGPT sign-ins for Codex. Switch after Codex sessions finish.',
      setup: 'Set Up Codex…', setupTitle: 'Set Up Codex Accounts',
      emptyTitle: 'Add your first Codex account',
      emptyText: 'Sign in with ChatGPT to save a Codex account. Your current sign-ins stay in place.',
      waiting: 'Waiting for a Codex usage reading.', usage: 'Usage and resets',
      isolation: 'Claude logins, connections and running sessions stay unchanged.',
      steps: [
        'If you are already signed in to Codex, choose Save Current Account. An account marked In use without a Not saved badge is already saved.',
        'Choose Add Codex Account, continue to the sign-in page, and sign in with the ChatGPT account you want to add. Use a private browser window if the browser picks another account. Then choose Save Sign-In.',
        'Choose Switch on a saved account when you are ready. Finish and close your Codex sessions first, or choose Wait for Them to Finish. Adding an account does not switch to it.',
      ],
      storage: 'AccountDeck supports Codex file storage. If the sign-in plan reports a Keychain or other storage setting, review the Codex storage instructions before changing it.',
      docs: 'Codex credential-storage instructions',
      removeBody: 'AccountDeck forgets this saved Codex login. Your ChatGPT account and Claude connections are unaffected.',
      purpose: { add: 'Add a Codex account', capture: 'Save the current Codex account?',
        'finish-login': 'Save the new Codex account?', 'cancel-login': 'Close the Codex sign-in?',
        recover: 'Recover the previous Codex sign-in?' },
      will: {
        add: ['Open the official Codex sign-in flow in a separate folder.', 'Keep the current Codex and Claude sign-ins in place.'],
        capture: ['Save a copy of the current Codex sign-in in the encrypted vault.', 'Keep all running sessions signed in as they are.'],
        'finish-login': ['Save the new Codex sign-in in the encrypted vault.', 'Keep the current Codex and Claude sign-ins in place.'],
        'cancel-login': ['Close the separate sign-in helper. If sign-in already completed, keep that account saved.', 'Keep the current Codex and Claude sign-ins in place.'],
        recover: ['Restore the previous Codex sign-in from encrypted recovery state.'],
      },
      reconfirm: 'The Codex sessions changed — please review again.',
    },

    // Claude Desktop (provider "claude"). Saving, adding and switching all close Claude, so each goes through a plan;
    // the plan and the sign-in views use modal.purpose and login.
    desktop: {
      title: 'Claude Desktop accounts', add: 'Add Desktop Account', capture: 'Save Current Account', save: 'Save Account',
      sub: 'Claude closes and reopens to switch. Finish Chat and Cowork work first.',
      emptyTitle: 'Add your first Claude Desktop account',
      emptyText: 'Sign in to Claude Desktop and its account appears here, ready to save. Or choose Add Desktop Account and Claude opens its sign-in screen.',
      absent: 'When Claude Desktop is signed in on this Mac, its account appears here.',
      waiting: 'Waiting for a usage reading.', usage: 'Usage and resets',
      importing: 'Open Claude Desktop once on this Mac to finish importing this account.',
      incomplete: 'Its saved sign-in is incomplete. Sign in again to switch to it.',
      recoverFirst: 'Finish the pending recovery first.',
      removeBody: "AccountDeck forgets this saved Claude Desktop sign-in. The account itself isn't affected, and you can add it again later.",
    },

    // Antigravity (providers.antigravity: Google's agy command-line tool). Usage is read only when the user asks (Refresh
    // Usage) or opens AccountDeck; Save, Add and Switch go through a plan, and through the notice once (`notice`, PLAN §7
    // word for word, one paragraph per item).
    antigravity: {
      title: 'Antigravity accounts', add: 'Add Antigravity Account', capture: 'Save Current Account', save: 'Save Account',
      sub: 'Google sign-ins for agy. Switch after agy sessions finish.',
      emptyTitle: 'Add your first Antigravity account',
      emptyText: "Sign in to agy in a terminal and its account appears here, ready to save. Or choose Add Antigravity Account and AccountDeck opens agy's sign-in in a Terminal window.",
      usage: 'Usage and resets',
      refresh: 'Refresh Usage', refreshing: 'Reading Usage…',
      refreshTip: "Reads the signed-in account's usage with Antigravity's own command.",
      again: 'Again in {countdown}', againTip: 'Usage can be read once a minute. Refresh Usage works again at {clock}.',
      read: 'Usage read {age} ago',
      reading: 'Reading usage…',
      noReading: 'No usage reading yet. Choose Refresh Usage to read it.',
      noReadingOther: 'No usage reading yet. Switch to this account, then choose Refresh Usage.',
      windows: { '5h': '5-hour', weekly: 'Weekly' },
      resetSince: 'Reset since this reading', resetSinceTip: 'It reset at {clock}, after this reading. Refresh Usage reads it again.',
      notAvailable: 'Not available', notReported: 'Not reported', amountLeft: '{amount} left',
      needsSignin: 'Sign-in needed', readError: "Couldn't read usage: {message}", since: 'Since {clock}',
      missing: 'Its saved sign-in is missing. Sign in again to switch to it.',
      // Why a button can't be used now (its title).
      notFound: "agy wasn't found on this Mac.", recoverFirst: 'Finish the unfinished Antigravity switch first.',
      addOpen: 'Finish or cancel the Antigravity sign-in first.',
      addEnding: 'Wait until the Add has ended.',
      addRunning: 'Quit agy first: adding an account signs agy out, and AccountDeck never stops it.',
      switchWaits: 'agy is running. The switch waits until it ends.',
      signedOut: 'Sign in to agy first: usage is read for the account it is signed in to.',
      switching: 'Wait for the switch to finish.',
      paused: 'Usage checks are paused. Turn them back on in Settings.',
      // The section's notes.
      runningOne: 'agy is running', running: '{n} agy sessions are running',
      runningText: "Switching waits until it ends, and Add can't start until then. AccountDeck never stops agy.",
      runningTextMany: "Switching waits until they end, and Add can't start until then. AccountDeck never stops agy.",
      storeTitle: "Saving and switching aren't available right now",
      storeText: "AccountDeck can't read the sign-in agy keeps on this Mac.",
      notFoundTitle: "agy wasn't found on this Mac",
      notFoundText: "Your saved accounts stay here. Switching and reading usage need agy, Antigravity's command-line tool.",
      adding: 'Adding an Antigravity account', addingAgain: 'Signing in to {label} again',
      timeLeft: '{countdown} left to sign in', addingDefault: 'Sign in with Google in the Terminal window AccountDeck opened.',
      // An Add that is ending (phase restoring): the service's message says what it waits for.
      endingTitle: 'Ending the Add',
      endingDefault: 'AccountDeck puts the earlier sign-in back as soon as agy has closed.',
      ended: {
        saved: ['Antigravity account added', 'Saved {email}.', 'good'],
        cancelled: ['Adding the account was cancelled', 'The earlier sign-in is back.', 'info'],
        timed_out: ['Adding the account timed out', 'No sign-in arrived in time. The earlier sign-in is back.', 'warning'],
        failed: ["Adding the account didn't finish", 'The earlier sign-in is back.', 'critical'],
      },
      recoverTitle: "An Antigravity switch didn't finish",
      recoverBoth: 'AccountDeck was switching agy from {before} to {after}.',
      recoverFromNone: 'AccountDeck was switching agy to {after}. agy was signed out before.',
      recoverAway: 'AccountDeck was switching agy away from {before}.',
      // The Recover sheet.
      recoverSheet: 'Which sign-in should agy keep?',
      recoverRunning: 'agy is running. Close every agy session to put a sign-in back; leaving it as it is works now.',
      nowSigned: 'agy is signed in to {email} now.', nowSignedOut: 'agy is signed out now.',
      nowUnknown: "AccountDeck can't read agy's sign-in right now.",
      choices: {
        before: ['Put back {label}', 'The account agy was signed in to before the switch.'],
        beforeNone: ['Leave agy signed out', 'agy was signed out before the switch.'],
        after: ['Use {label}', 'The account the switch was moving to.'],
        keep: ['Keep what agy has now', "Nothing in agy's sign-in changes."],
      },
      recoverButton: 'Recover',
      // The footer: how usage is read, and T3 Code (PLAN §12).
      howRead: "Usage is read with Antigravity's own command when you click Refresh Usage or open AccountDeck, never in the background.",
      howReadOff: "Usage is read with Antigravity's own command only when you click Refresh Usage, never in the background.",
      t3: "T3 Code keeps its own Antigravity sign-in. Switching here does not change it; to use another account there, add another Antigravity instance in T3 Code's settings.",
      removeBody: "AccountDeck forgets this saved Antigravity sign-in. Your Google account isn't affected, and agy stays signed in as it is.",
      // The notice before the first Save, Add or Switch (agy_notice_seen).
      noticeTitle: "Antigravity accounts and Google's terms",
      notice: [
        "Google's terms for Antigravity say that using third-party tools to access it is not allowed, and that automating account rotation is not supported.",
        'AccountDeck never contacts Google with your sign-in and never switches Antigravity accounts by itself: it stores the sign-in Antigravity keeps on this Mac and puts it back when you click Switch.',
        "Google could still object to the use of several accounts and suspend an account's access to Antigravity. Continue only if you accept that.",
      ],
      continue: 'Continue',
      // The plan sheet (switch.js).
      switchTitle: 'Switch Antigravity to {label}?', againTitle: 'Sign in to {label} again?',
      purpose: { add: 'Add an Antigravity account', capture: 'Save the account agy is signed in to?',
        'cancel-login': 'Stop adding the account?', recover: 'Recover the Antigravity sign-in?' },
      isolation: 'Claude Code, Claude Desktop and Codex stay as they are.',
      reconfirm: "agy's sessions or its sign-in changed. Please review again.",
      consumersOne: '1 agy session is running. The switch waits until it ends; AccountDeck never stops it.',
      consumers: '{n} agy sessions are running. The switch waits until they end; AccountDeck never stops them.',
      consumersAddOne: '1 agy session is running. Quit it first: adding an account signs agy out.',
      consumersAdd: '{n} agy sessions are running. Quit them first: adding an account signs agy out.',
      consumerKind: 'An agy session', wait: 'Wait for agy to Finish',
      will: {
        switch: ['Wait until no agy runs.', "Save the current sign-in in AccountDeck's encrypted vault.",
          "Put the account's saved sign-in back where agy keeps it.", 'Check that agy is signed in to it.'],
        capture: ['Read the sign-in agy keeps on this Mac.', "Save a copy in AccountDeck's encrypted vault. agy stays signed in."],
        add: ['Save the current sign-in.', 'Sign agy out on this Mac. Nothing is revoked at Google.',
          'Open agy in a Terminal window for the Google sign-in.', 'Save the new account when it appears. It stays signed in.'],
      },
      settings: 'Antigravity', usageOnOpen: 'Read Antigravity usage when I open AccountDeck',
      usageOnOpenHint: "Reads the signed-in account's usage when you open the menu or this page and the last reading is over 15 minutes old. When this is off, only Refresh Usage reads it.",
    },

    // The update prompt (state.update, accountdeck/updates.py): the banner, the confirmation and Settings › About.
    // `confirm` repeats updates.CONFIRM word for word, as the menu bar's confirmation shows it.
    update: {
      title: 'AccountDeck {version} is available', current: 'You have {current}.',
      update: 'Update…', notes: "What's New", notesTip: 'The release notes on GitHub', skip: 'Skip This Version',
      skipped: "AccountDeck {version} won't be offered again. A later version will be.",
      confirmTitle: 'Update AccountDeck to {version}?', confirmButton: 'Update',
      confirm: "AccountDeck downloads {version} from GitHub, checks its SHA-256 and installs it in a Terminal window. Every saved account is kept, and only AccountDeck restarts: open Claude, Codex and agy sessions are not touched. If the new version doesn't start, the installer puts {current} back.",
      startedTitle: 'Updating AccountDeck to {version}',
      startedWindow: 'The update goes on in the Terminal window. This window closes while AccountDeck restarts. When AccountDeck is back in the menu bar, choose Open Dashboard.',
      startedPage: 'The update goes on in the Terminal window. This page loses its connection while AccountDeck restarts. When AccountDeck is back in the menu bar, choose Open Dashboard.',
      group: 'Updates', setting: 'Check for updates automatically',
      settingHint: 'Every 5 minutes, and when you open AccountDeck, it asks GitHub whether a newer release is out. It sends only its version number.',
      checkNow: 'Check Now', checked: 'Checked {age} ago.',
      status: {
        checking: 'Checking for updates…',
        current: 'AccountDeck {current} is up to date',
        available: 'AccountDeck {version} is available', availableHint: 'You have {current}.',
        skipped: 'You skipped AccountDeck {version}', skippedHint: 'Check Now offers it again.',
        never: 'Not checked yet', neverHint: 'The first check runs 30 seconds after AccountDeck starts.',
        failed: "Couldn't check for updates", failedHint: 'Check the internet connection, or try again later.',
        tried: 'Last tried {age} ago.',
        off: 'Update checks are off', offHint: 'AccountDeck never asks GitHub.', offButton: 'Turn on Check for updates automatically first.',
        unavailable: 'Update checks are unavailable',
      },
    },

    chip: {
      current: 'In use', next: 'Next up', best: 'Most room', leftOut: 'Left out', notSaved: 'Not saved',
      incident: 'Paused · incident', cloudflare: 'Check refused by Cloudflare', error: "Couldn't refresh",
    },

    button: {
      switchIdle: 'Switch When Claude Is Idle', switchNow: 'Switch Now', switchAnyway: 'Switch Now Anyway',
      continueIdle: 'Continue When Claude Is Idle', continueNow: 'Continue Now', continueAnyway: 'Continue Anyway',
      cancel: 'Cancel', done: 'Done', close: 'Close', hide: 'Hide', progress: 'Show Progress', review: 'Review…',
      retry: 'Try Again', viewReport: 'Show Report', explain: 'Explain Last Switch', recover: 'Recover…',
      refresh: 'Refresh', resume: 'Resume Checks', dismiss: 'Dismiss', reset: 'Reset', quit: 'Quit',
      endCodex: 'End Session', waitCodex: 'Wait for Them to Finish', importPC: 'Import from PC…',
    },
    modal: {
      switchTerminals: 'Switch your terminals to {label}?',
      switchClaude: 'Switch Claude to {label}?', switchCodex: 'Switch Codex to {label}?',
      managed: 'Claude will close and reopen on its own. If it does not close within 10 seconds it will be closed by force.',
      graceful: 'Claude will close by itself and reopen.',
      interrupt: 'Switch now and interrupt {names}?', interruptOther: 'Continue now and interrupt {names}?',
      reconfirm: "Claude's sessions changed — please confirm again.",
      savesOutgoing: "Your current account ({label}) isn't saved yet — it will be saved so you can switch back.",
      sessions: '{n} Code sessions are open: {names}. Switching will end them.',
      sessionsOne: '1 Code session is open: {names}. Switching will end it.',
      running: 'Running now', will: 'What will happen', stays: 'What stays with you', bound: 'Tied to each account',
      headsUp: 'Heads-up', rowNow: 'Now', rowAfter: 'After', remember: "Don't ask again when Claude is idle",
      blocked: 'This cannot start yet.', unsavedHint: 'You can switch to it after the next switch away from it.',
      consumers: '{n} Codex sessions are running. Switch when they finish, or end them one by one.',
      consumersOne: '1 Codex session is running. Switch when it finishes, or end it below.',
      progressTitle: 'Working…',
      purpose: {
        add: 'Add a Claude account', capture: 'Save the account Claude is signed in to?',
        recover: 'Recover the previous sign-in?', 'finish-login': 'Save the new account?',
        'cancel-login': 'Stop adding the account?', quit: 'Close Claude?',
      },
      endTitle: 'End this Codex session?', endBody: 'It stops right away; anything it was doing is interrupted.',
      consumerFrom: 'from {parent}', consumerStarted: 'started {clock}',
      consumerKind: { tui: 'Codex in a terminal', exec: 'A Codex command', app_server_child: 'Codex in an editor', other: 'A Codex session' },
    },
    progress: {
      title: 'Switching to {label}', waitingOn: 'Waiting for {names}',
      step: { pending: 'waiting', active: 'in progress', done: 'done', skipped: 'skipped', warning: 'needs attention', failed: 'failed' },
    },
    session: { busy: 'working', idle: 'idle', unknown: 'status unknown' },
    report: {
      switched: 'Switched to {label}', verified: 'Claude is signed in as {label}',
      unconfirmed: 'Claude opened — still confirming the sign-in in the background',
      failed: 'Claude opened a different account. Recovery is available.',
      title: 'Last switch', carried: 'Carried over', failedTitle: "The switch didn't finish", cancelledTitle: 'Switch cancelled',
    },
    login: {
      claudeTitle: 'Sign in to the new account in Claude',
      claude: 'When you have signed in, choose Save Account. Claude closes briefly to save the sign-in.',
      codexTitle: 'Finish signing in with ChatGPT', codex: 'Open the sign-in page and sign in with the ChatGPT account you want to add. Then choose Save Sign-In. Your current Codex and Claude sign-ins stay in place.',
      open: 'Open Sign-In Page', save: 'Save Account', saveCodex: 'Save Sign-In', cancel: 'Cancel Sign-In',
      deviceCode: 'Device code: {code}',
      hint: 'Sign in with the account you intend to add; a browser already signed in to another Claude account can approve the wrong one.',
    },
    outage: { link: 'Open the status page' },
    alerts: {
      more: '{message} (+{n} more)', imminent: "{label}'s {window} limit resets in {countdown}",
      resetting: "{label}'s {window} limit is resetting", ready: '{label} is ready again.',
      blocked: 'Allow notifications for this page in your browser settings to get alerts.',
      unsupported: "This browser can't show notifications.", enabled: 'Browser notifications are on.',
    },

    settings: {
      title: 'Settings',
      tabs: { general: 'General', autoswitch: 'Auto-Switch', notifications: 'Notifications', menubar: 'Menu Bar & Login', advanced: 'Advanced', about: 'About' },
      appearance: 'Appearance', numbers: 'Show usage as', used: 'Used', left: 'Left',
      privacy: 'Privacy and confirmation',
      hideEmails: 'Hide email addresses', hideEmailsHint: 'Masks addresses on this screen only.',
      confirmIdle: 'Ask before switching when Claude is idle',
      confirmIdleHint: 'Shows the confirmation even when no Claude session would be interrupted.',
      auto: 'Switch accounts automatically', autoHint: 'Before an account runs out, every terminal moves to the next one.',
      threshold: 'Switch when an account reaches', thresholdHint: 'Of its 5-hour, weekly or Fable 5.1 weekly limit.',
      thresholdHintWeekly: 'Of its weekly or Fable 5.1 weekly limit.',
      thresholdUp: 'Raise the switch point', thresholdDown: 'Lower the switch point',
      order: 'Next account', orderBy: 'Order', aboutVersion: 'Version {version}', orders: { soonest_reset: 'Soonest reset first', most_room: 'Most room left first', manual: 'My own order' },
      orderNow: 'Order right now', orderManual: 'Your order (top goes first)', orderEmpty: 'Add terminal accounts to see the order.',
      orderReset: 'Reset to Automatic Order', moveUp: 'Move {label} up', moveDown: 'Move {label} down',
      early: 'Use weekly allowance before it resets', earlyHint: 'Switches early to an account whose unused weekly limit is about to reset.',
      // Automatic limit resets (the auto_reset setting; providers.claude_cli.auto_reset says why they stopped).
      autoReset: 'Use a limit reset when every terminal account is out',
      autoResetHint: 'Off unless you turn it on. When every terminal account auto-switch could use is at 97% or more of its weekly or Fable 5.1 weekly limit (the account in use: at your switch point, if that is higher), AccountDeck uses one Claude limit reset by itself: on the account in use when it holds one that fits, else on another account. One at a time, never for a limit that refills within 10 minutes, and it tells you each time. A limit reset cannot be taken back.',
      autoResetNeeds: 'It works only with “Switch accounts automatically” and “Read Claude limit resets” on.',
      alertsOn: 'Show alerts in AccountDeck', alertsOnHint: 'Low limits, refills and switches appear on this page.',
      browser: 'Browser notifications', browserOn: 'On in this browser.', browserOff: 'Off. The browser asks for your permission once.',
      browserBlocked: "Blocked in this browser's site settings.", browserUnsupported: "This browser can't show notifications.",
      enable: 'Turn On…', turnOff: 'Turn Off',
      test: 'Test notification', testHint: 'If nothing appears, choose Turn On Notifications… in the AccountDeck 2 menu bar item.',
      testButton: 'Send Test', testSent: 'Test notification sent.',
      mac: 'Mac notifications are off',
      macOff: 'macOS is blocking notifications from AccountDeck 2. Choose Turn On Notifications… in its menu bar item, or allow them in System Settings › Notifications.',
      menuBar: 'Show in the menu bar', menuBarHint: 'The menu bar item shows the terminal room left. Click it for every account.',
      login: 'Open at login', loginHint: 'Listed in System Settings › General › Login Items, where you can also turn it off.',
      pause: 'Pause usage checks', pauseHint: 'Background usage readings stop until you turn this back on.',
      importPC: 'Import accounts from your PC', importPCHint: 'Open an encrypted .adeck bundle exported by AccountDeck 2 on Windows.',
      shortcuts: 'Keyboard shortcuts', shortcutsButton: 'Show…', diagnostics: 'Diagnostics', diagnosticsButton: 'Show…',
      diagnosticsHint: 'A recent log for troubleshooting. It never contains tokens, cookies, keys or emails.',
      quit: 'Quit AccountDeck 2…',
    },

    details: {
      title: 'Activity & troubleshooting', activity: 'Recent activity', setup: 'Shared setup', explain: 'Last switch explained',
      diagnostics: 'Diagnostics', diagnosticsButton: 'Show Diagnostics…',
      noActivity: 'Your account changes will appear here.', loading: 'Loading the explanation…', notRecorded: 'not recorded',
      review: 'Needs review', preserved: 'Configuration preserved', notConfigured: 'Not configured', yes: 'Yes', no: 'No',
      footer: 'Logs never contain tokens, cookies, keys or emails.',
    },
    theme: { label: 'Appearance', system: 'Auto', light: 'Light', dark: 'Dark' },
    help: {
      logout: "Use Switch or Add Account here; never use Claude's own Log Out to change accounts — it can end that sign-in on Anthropic's side.",
    },
    shortcuts: [
      [['⌘', 'R'], 'Refresh all accounts'],
      [['⌘', ','], 'Settings'],
      [['↑', '↓'], 'Move between accounts'],
      [['Return'], 'Switch to the focused account'],
      [['1', '–', '9'], 'Go to account 1 to 9'],
      [['T'], 'Change the appearance'],
      [['Esc'], 'Close a sheet, menu or Settings'],
      [['?'], 'Show these shortcuts'],
    ],
  });

  /** Substitute {name} placeholders; unknown names stay as written so a missing value is visible. */
  function fill(template, values) {
    return String(template).replace(/\{(\w+)\}/g, (match, name) =>
      (values && values[name] !== undefined && values[name] !== null ? String(values[name]) : match));
  }

  /** Age of a reading, as in contract.age_text: "42m" (at least 1m), "3h" under 48 h, else "2d". */
  function ageText(secs) {
    if (!isNum(secs)) return '—';
    const minutes = Math.floor(Math.max(0, secs) / 60);
    if (minutes < 60) return `${Math.max(minutes, 1)}m`;
    if (minutes < 48 * 60) return `${Math.floor(minutes / 60)}h`;
    return `${Math.floor(minutes / 1440)}d`;
  }

  /** Age of `ts` (epoch s) at nowMs; callers add their own words ("Checked {age} ago"). */
  function ago(ts, nowMs) {
    return isNum(ts) ? ageText(nowMs / 1000 - ts) : '—';
  }

  /** "Max" from "max": a plan name for display. */
  function capitalised(text) {
    return typeof text === 'string' && text ? text[0].toUpperCase() + text.slice(1) : null;
  }

  // As Claude Code shows a currency (alerts.CURRENCIES); any other by its code.
  const CURRENCIES = { USD: '$', EUR: '€', GBP: '£', JPY: '¥', BRL: 'R$', CAD: 'CA$', AUD: 'A$', NZD: 'NZ$', SGD: 'S$' };
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  /** An amount of a Usage credits row, in minor units of its currency (usage._claude_credits), as alerts.money
   * writes it: "$3.20", and with `fit` "$100" when it is whole. */
  function money(minor, credit, fit = false) {
    if (!isNum(minor)) return null;
    const currency = String((credit && credit.currency) || 'USD').toUpperCase();
    const raw = credit && credit.exponent;
    const exponent = Number.isInteger(raw) && raw >= 0 && raw <= 6 ? raw : 2;
    const value = minor / 10 ** exponent;
    const digits = fit && Number.isInteger(value) ? 0 : exponent;
    const text = value.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
    return `${CURRENCIES[currency] || `${currency} `}${text}`;
  }

  /** "Nov 1" (local date of epoch seconds). */
  function dateText(ts) {
    if (!isNum(ts)) return '—';
    const date = new Date(ts * 1000);
    return `${MONTHS[date.getMonth()]} ${date.getDate()}`;
  }

  // ---- The platform block ------------------------------------------------------------------------
  // /api/state may carry `platform: {os, features, labels}`. This build is Mac-only: the page has Mac
  // copy built in and reads the block, when present, only to let the service adjust a few hints or
  // hide a feature it reports as unavailable. A missing or malformed block changes nothing.
  const MAC_LABELS = freeze({
    section: 'Menu Bar & Login',
    status_icon: 'Show in the menu bar',
    status_icon_hint: 'The menu bar item shows the terminal room left. Click it for every account.',
    start_on_login: 'Open at login',
    quit_hint: 'Quit Claude from its menu and try again.',
    reopen_hint: 'Reopen AccountDeck 2 from Applications.',
    open_hint: 'Open AccountDeck 2 from Applications.',
    closed_hint: 'Open it again from Applications to see your accounts.',
    system_error: "macOS didn't allow that. Try again.",
  });
  // Server labels that are allowed to replace the built-in copy (the rest stay the page's own).
  const SERVER_LABELS = new Set(['quit_hint', 'reopen_hint', 'open_hint', 'closed_hint', 'system_error', 'status_icon_hint']);
  let platform = { features: {}, labels: {}, notifications: null };
  // The app's own window (a WKWebView) names itself in the user agent. It has no browser
  // notifications; Mac notifications come from the menu bar item instead.
  const APP_WINDOW = /AccountDeck2App\//.test((window.navigator && window.navigator.userAgent) || '');

  /** Follow the platform block of a new state (or its absence). */
  function setPlatform(block) {
    const ok = block && typeof block === 'object';
    platform = {
      features: ok && block.features && typeof block.features === 'object' ? block.features : {},
      labels: ok && block.labels && typeof block.labels === 'object' ? block.labels : {},
      notifications: ok ? block.notifications : null,
    };
  }

  /** True inside AccountDeck 2's own window rather than a browser. */
  function appWindow() {
    return APP_WINDOW;
  }

  /** macOS's notification permission for AccountDeck 2 as the service reports it
   * (platform.notifications.state: "authorized", "denied", "not_determined", …), or null. */
  function notificationState() {
    const value = platform.notifications;
    const state = value && typeof value === 'object' ? value.state : null;
    return typeof state === 'string' && state ? state : null;
  }

  /** False only when the service explicitly reports a feature (menu_bar, start_on_login) as unavailable. */
  function feature(name) {
    return platform.features[name] !== false;
  }

  /** The Mac copy for `name`; a few hints may come from the service. */
  function label(name) {
    const server = platform.labels[name];
    if (SERVER_LABELS.has(name) && typeof server === 'string' && server) return server;
    return MAC_LABELS[name] || '';
  }

  /** An error's message for the page: the service's own words; code "system" (macOS refused)
   * without a message gets the Mac wording. */
  function errorText(code, message) {
    if (code === 'system' && !message) return label('system_error');
    return message;
  }

  AD.format = { STRINGS, fill, ageText, ago, capitalised, money, dateText, setPlatform, feature, label, errorText, appWindow,
    notificationState };
})();
