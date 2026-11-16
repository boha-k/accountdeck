/* AccountDeck 2 — the overview: the answers that matter at a glance, for the Claude Code
 * (terminal) accounts only.
 *   Total       — the usage left, 100 % per account each limit covers (seven accounts make 700 %), per limit,
 *                 how long it lasts at the recent pace (the runway), and the limit resets the accounts hold;
 *   Sign-ins    — the logins that need a sign-in or end within the week, most urgent first (only then);
 *   In use      — the account every terminal uses, how much is left, when its limits reset and its limit resets;
 *   Auto-switch — on or off (a switch right there), who takes over next and when, why the others
 *                 wait, and how fast it last answered a limit error;
 *   Next reset  — the soonest refill across the accounts;
 *   Fable 5.1   — which account has the most Fable 5.1 weekly room.
 * render() runs on every state and clock tick; a card is rebuilt only when its signature changed.
 * Countdowns are [data-reset] text the shell's clock patches in place. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;
  const A = AD.accounts;
  const O = STRINGS.overview;

  const QUEUE_SHOWN = 4;
  const DUE_SHOWN = 5;                        // the rest wait in the table, each with its own Sign In Again
  const OUT_LEFT = 0.5;                       // ≤ this much left is out (rules.band)
  const BOARD_SHOWN = 3;                      // the full ranking is the table's "Most Fable 5.1 room" order
  // The runway's urgency, as alerts.runway judges it: a warning within a day, critical within 3 h, and calm
  // when a refill comes within the hour after the run-out.
  const RUNWAY_SOON = 86400;
  const RUNWAY_CRITICAL = 3 * 3600;
  const RUNWAY_GAP = 3600;
  const RUNWAY_ICONS = { quiet: 'clock', calm: 'check-circle', warning: 'warning', critical: 'warning' };
  const HEADLINE_TONES = { ready: 'good', watch: 'accent', low: 'warning', critical: 'critical', out: 'critical', signin: 'warning', on_hold: 'critical' };
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);

  /** An account's name; a click brings its row into view. */
  function nameLink(label, id) {
    if (!id) return el('span', { text: mask(label) });
    return AD.dom.button(mask(label), () => AD.rows.spotlight(id), { kind: 'link', className: 'name-link',
      title: fill(O.show, { label: mask(label) }) });
  }

  function card(key, heading, parts, extra = {}) {
    const id = `ov-${key}-title`;
    return el('section', Object.assign({ class: ['card', `ov-${key}`], 'aria-labelledby': id }, extra),
      el('div', 'card-head', el('h2', { class: 'card-heading', id }, heading.title), heading.aside || null), parts);
  }

  function countdown(reset, secs, template) {
    return el('time', { dataset: { reset, resetTemplate: template, resetDone: STRINGS.window.resetting },
      text: fill(template, { countdown: AD.model.countdownText(secs) }) });
  }

  // ---- Total -------------------------------------------------------------------------------------

  /** The limits the total adds up, top to bottom; labels come from the accounts table's columns. */
  const TOTAL_LIMITS = [
    { key: 'session', find: (a) => (a.windows || []).find((w) => w.kind === 'session' && !w.model) },
    { key: 'weekly', find: (a) => (a.windows || []).find((w) => w.kind === 'weekly' && !w.model) },
    { key: 'fable', find: (a) => A.fableWindow(a), fable: true },
  ];

  /** Percent left of one window at nowMs (pool.left_at), null when unknown: locked → 0, also past its reset;
   * not started → 100; a reset that passed since the reading → 100; blocked or out → 0; else 100 − used. */
  function leftAt(win, account, nowMs) {
    if (win.blocked === 'locked') return 0;
    if (win.started === false) return 100;
    const read = account.last_good_at;
    if (isNum(win.reset) && win.reset <= nowMs / 1000 && (!isNum(read) || read < win.reset)) return 100;
    if (win.blocked || win.severity === 'out') return 0;
    return isNum(win.used) ? Math.min(100, Math.max(0, 100 - win.used)) : null;
  }

  /** What is left across the accounts at nowMs, the same rule as the menu bar's (pool.py). An account that
   * needs sign-in, that Claude put on hold, that the owner left out or whose reading is unknown is not counted,
   * only named (`named`, with a count of each). Each limit covers its own accounts (`capacity`, 100 per account):
   * weekly every counted one; 5-hour only those with a 5-hour window, each adding no more than its weekly room;
   * Fable 5.1 those reporting it. An account whose weekly limit, or a meter that holds terminals, is out has no
   * 5-hour or Fable 5.1 room either, and one locked by Claude has none at all. `reset` is the soonest refill of
   * something used, never a locked account's (a lock has no known end). */
  function totals(state, nowMs) {
    const named = { signin: [], held: [], left_out: [], unread: [] };
    const sum = { count: 0, out: 0, named,
      limits: TOTAL_LIMITS.map((limit) => ({ key: limit.key, left: 0, capacity: 0, reset: null })) };
    for (const account of A.terminals(state)) {
      if (A.frozen(account)) { named.left_out.push(account.label); continue; }
      if (A.needsSignin(account)) { named.signin.push(account.label); continue; }
      if (A.onHold(account)) { named.held.push(account.label); continue; }
      if (account.excluded) { named.left_out.push(account.label); continue; }
      const windows = TOTAL_LIMITS.map((limit) => limit.find(account) || null);
      const [five, weekly, fable] = windows.map((win) => (win && isNum(account.last_good_at) ? leftAt(win, account, nowMs) : null));
      if (weekly === null || (windows[0] && five === null)) { named.unread.push(account.label); continue; }
      const locked = AD.model.locked(account);
      const meterOut = (account.other_windows || []).some((win) => win.gates && leftAt(win, account, nowMs) !== null
        && leftAt(win, account, nowMs) <= OUT_LEFT);
      const out = locked || weekly <= OUT_LEFT || !!windows[1].blocked || meterOut;
      const room = locked ? 0 : weekly;
      sum.count += 1;
      if (A.blocked(account) || out) sum.out += 1;
      [out ? 0 : Math.min(five, room), room, out ? 0 : fable].forEach((left, index) => {
        if (!windows[index] || (index === 2 && fable === null)) return;
        const row = sum.limits[index];
        row.left += left;
        row.capacity += 100;
        const d = AD.model.deriveWindow(windows[index], account, nowMs);
        if (!locked && isNum(d.secs) && d.secs > 0 && (d.state === 'out' || d.used > 0) && (row.reset === null || d.reset < row.reset)) {
          row.reset = d.reset;
        }
      });
    }
    Object.assign(sum, { signin: named.signin.length, held: named.held.length, left_out: named.left_out.length,
      unread: named.unread.length });
    for (const row of sum.limits) row.left = Math.round(row.left);
    return sum;
  }

  function totalLimit(limit, row, label, nowMs) {
    let note = null;
    const capacity = row.capacity;
    if (row.reset !== null) {
      note = el('span', { class: 'limit-note', title: fill(STRINGS.window.resetsAt, { clock: AD.model.clock(row.reset, nowMs) }) },
        countdown(row.reset, row.reset - nowMs / 1000, O.totalRefill));
    }
    const share = capacity ? Math.min(100, (row.left / capacity) * 100) : 0;
    return el('div', { class: 'limit is-compact', dataset: { fable: limit.fable ? 'true' : null } },
      el('div', 'limit-head',
        el('span', 'limit-lead', el('span', { class: 'limit-label' },
          limit.fable ? el('span', { class: 'fable-mark', 'aria-hidden': 'true' }) : null, label), note),
        el('span', { class: 'limit-value', text: fill(O.left, { n: row.left }) })),
      el('div', { class: 'meter', role: 'meter', 'aria-valuemin': 0, 'aria-valuemax': capacity, 'aria-valuenow': row.left,
        'aria-valuetext': `${label}: ${fill(O.totalValue, { left: row.left, total: capacity })}` },
      el('span', { class: 'meter-fill', style: { '--fill': `${share}%` } })));
  }

  function totalCard(state, sum, runway, resets, ctx) {
    const labels = A.columns(A.terminals(state));
    const named = (text, who) => `${text} (${who.map(mask).join(', ')})`;
    const notes = [sum.count === 1 ? O.totalAccountsOne : fill(O.totalAccounts, { n: sum.count })];
    if (sum.out) notes.push(fill(O.totalOut, { n: sum.out }));
    if (sum.signin) notes.push(named(sum.signin === 1 ? O.totalSigninOne : fill(O.totalSignin, { n: sum.signin }), sum.named.signin));
    if (sum.held) notes.push(named(fill(O.totalHeld, { n: sum.held }), sum.named.held));
    if (sum.left_out) notes.push(named(fill(O.totalLeftOut, { n: sum.left_out }), sum.named.left_out));
    if (sum.unread) notes.push(named(fill(O.totalUnread, { n: sum.unread }), sum.named.unread));
    if (resets) notes.push(AD.resets.totalText(resets));          // the limit resets the terminal accounts hold
    // A limit no account has is left out, so a 5-hour limit none reports appears nowhere; weekly always shows.
    const shown = TOTAL_LIMITS.map((limit, index) => ({ limit, row: sum.limits[index], label: labels[index] }))
      .filter(({ limit, row }) => limit.key === 'weekly' || row.capacity > 0);
    // The figure, like the menu bar's title, is the tighter of 5-hour and weekly, each over the accounts it covers.
    const share = (row) => (row.capacity ? row.left / row.capacity : 0);
    const tightest = shown.filter(({ limit }) => !limit.fable).reduce((low, item) => (share(item.row) < share(low.row) ? item : low));
    const parts = [
      el('div', 'who', el('span', { class: 'avatar avatar-large', 'aria-hidden': 'true', text: String(sum.count) }),
        el('div', 'who-text', el('p', { class: 'who-name', text: O.totalWho }),
          el('p', { class: 'who-meta', text: notes.join(' · ') }))),
      el('div', 'figure-row',
        el('p', 'figure-line', el('span', { class: 'figure', text: `${tightest.row.left}%` }), el('span', { class: 'figure-unit', text: 'left' })),
        el('p', { class: 'figure-caption', text: fill(O.totalCaption, { window: tightest.label, n: tightest.row.capacity }) })),
      el('div', 'limit-stack', shown.map(({ limit, row, label }) => totalLimit(limit, row, label, ctx.nowMs))),
      runway ? el('p', { class: 'ov-runway', dataset: { tone: runway.tone }, title: runway.tip },
        AD.dom.svgIcon(RUNWAY_ICONS[runway.tone]), el('span', { text: runway.text })) : null,
    ];
    return card('total', { title: O.total }, parts);
  }

  /** The pool's forecast (providers.claude_cli.runway) as one line at nowMs: {text, tone, tip}, or null before
   * the first forecast and without an account to forecast. tone: quiet (too little recent use to say), calm,
   * warning or critical (RUNWAY_*). */
  function runwayLine(state, nowMs) {
    const runway = (A.provider(state) || {}).runway;
    if (!runway || typeof runway !== 'object' || !runway.accounts) return null;
    const R = STRINGS.runway;
    if (runway.confidence !== 'ok') return { text: R.low, tone: 'quiet', tip: null };
    const tip = fill(R.tip, { pace: Math.round(runway.pace_per_hour * 10) / 10, room: Math.round(runway.room),
      span: AD.model.countdownText(runway.basis_s), accounts: runway.accounts === 1 ? O.totalAccountsOne : fill(O.totalAccounts, { n: runway.accounts }) });
    if (!isNum(runway.runs_out_at)) return { text: isNum(runway.next_refill_at) ? R.lasts : R.lastsWeek, tone: 'calm', tip };
    const when = AD.model.around(runway.runs_out_at, 60, nowMs);
    const text = isNum(runway.refill_after_out_at)
      ? fill(R.out, { when, clock: AD.model.clock(runway.refill_after_out_at, nowMs), label: mask(runway.refill_label || '') })
      : fill(R.outNoRefill, { when });
    const away = runway.runs_out_at - nowMs / 1000;
    let tone = away < RUNWAY_CRITICAL ? 'critical' : 'warning';
    if (away >= RUNWAY_SOON || (isNum(runway.gap_s) && runway.gap_s <= RUNWAY_GAP)) tone = 'calm';
    return { text, tone, tip };
  }

  function totalSig(state, sum, runway, resets) {
    return JSON.stringify([sum, A.columns(A.terminals(state)), runway, resets, AD.dom.hideEmails()]);
  }

  // ---- Sign-ins due ------------------------------------------------------------------------------

  /** The rows of the sign-ins card at nowMs: the service's list (most urgent first), a "soon" login that has
   * ended since then reading as ended. [{id, label, email, account, state, text, urgent}] */
  function dueRows(state, nowMs) {
    const L = STRINGS.logins;
    return A.signinDue(state).map((row) => {
      const account = A.terminals(state).find((a) => a.id === row.id) || { id: row.id, provider: 'claude_cli', label: row.label };
      const left = isNum(row.ends_at) ? row.ends_at - nowMs / 1000 : null;
      const stage = row.state === 'soon' && left !== null && left <= 0 ? 'ended' : row.state;
      let text = L.needs;
      if (stage === 'ended') text = L.ended;
      else if (stage === 'soon') text = fill(L.ends, { clock: AD.model.clock(row.ends_at, nowMs) });
      return { id: row.id, label: row.label, email: account.email || null, account, state: stage, text,
        urgent: stage !== 'soon' || (left !== null && left <= 86400) };
    });
  }

  function dueCard(state, rows, readonly) {
    const busy = !!state.pending;
    const shown = rows.slice(0, DUE_SHOWN);
    const parts = [el('ul', { class: 'due-list', 'aria-labelledby': 'ov-due-title' }, shown.map((row) => el('li',
      { class: 'due-row', dataset: { state: row.state, urgent: row.urgent ? 'true' : null } },
      AD.dom.avatar(row.label, { size: 'small', muted: row.state !== 'soon' }),
      el('div', 'due-text', el('span', 'due-name', nameLink(row.label, row.id)), el('span', { class: 'due-when', text: row.text }),
        row.email ? el('span', { class: 'due-email', text: mask(row.email), title: mask(row.email) }) : null),
      AD.dom.button(STRINGS.accounts.signIn, () => AD.terminal.add(row.account), { small: true, disabled: !!readonly || busy,
        title: readonly || undefined, label: `${STRINGS.accounts.signIn}: ${mask(row.label)}`,
        attrs: { 'data-keep': `ov-due-${row.id}` } }))))];
    if (rows.length > shown.length) parts.push(el('p', { class: 'ov-line', text: fill(O.dueMore, { n: rows.length - shown.length }) }));
    return card('due', { title: O.due }, parts, { dataset: { urgent: rows.some((r) => r.urgent) ? 'true' : null } });
  }

  // ---- In use ------------------------------------------------------------------------------------

  function headlineBadge(account) {
    // On hold has no headline of its own (the service reads such an account as ready): Claude's message is its title.
    const headline = A.onHold(account) ? { kind: 'on_hold', text: STRINGS.accounts.onHold, title: (account.error || {}).message }
      : account.headline || {};
    if (!headline.text) return null;
    const tone = HEADLINE_TONES[headline.kind] || 'neutral';
    return AD.meter.badge(tone, mask(headline.text), { icon: tone === 'good' ? null : undefined, dot: tone === 'good',
      title: headline.title });
  }

  /** Usage credits of the account in use: "Credits on", and the money spent this month when there is any. */
  function creditLine(account) {
    const credit = A.usageCredit(account);
    if (!credit) return null;
    const C = STRINGS.credits;
    const spent = isNum(credit.used) && credit.used > 0 ? fill(C.spent, { amount: AD.format.money(credit.used, credit) }) : null;
    const words = [credit.enabled === true ? C.on : null, spent].filter(Boolean);
    if (!words.length) return null;
    // Half the monthly limit spent reads like the row's line (rows.js moneyLine).
    const share = isNum(credit.limit) && credit.limit > 0 && spent ? (credit.used / credit.limit) * 100 : credit.percent;
    return el('p', { class: 'who-money', title: C.onTip, dataset: { tone: spent && isNum(share) && share >= 50 ? 'high' : null } },
      AD.dom.svgIcon('card'), el('span', { text: words.join(' · ') }));
  }

  function figure(account, ctx) {
    const bind = A.binding(account, ctx.nowMs);
    if (!bind) return el('p', { class: 'figure-caption', text: O.waiting });
    const { win, d } = bind;
    const value = ctx.display === 'left' ? Math.max(0, Math.round(d.left)) : (d.used > 100 ? '100+' : Math.round(d.used));
    const timed = isNum(d.secs) && d.secs > 0 && !AD.model.held(account);       // no reset brings it back
    return el('div', 'figure-row',
      el('p', 'figure-line',
        el('span', { class: 'figure', dataset: { severity: d.severity }, text: `${value}%` }),
        el('span', { class: 'figure-unit', text: ctx.display === 'left' ? 'left' : 'used' })),
      el('p', 'figure-caption', fill(O.limitCaption, { window: win.label }),
        timed ? [' · ', countdown(d.reset, d.secs, d.state === 'out' ? 'back in {countdown}' : 'resets in {countdown}')] : null));
  }

  function inUseCard(state, ctx) {
    const account = A.active(state);
    if (!account) {
      return card('inuse', { title: O.inUse },
        el('div', 'figure-block', el('p', { class: 'ov-title', text: O.nobodyInUse }), el('p', { class: 'ov-line', text: O.nobodyInUseLine })));
    }
    const meta = [AD.format.capitalised(account.plan), account.email ? mask(account.email) : null].filter(Boolean).join(' · ');
    const parts = [
      el('div', 'who', AD.dom.avatar(account.label, { size: 'large', active: true }),
        el('div', 'who-text', el('p', 'who-name', nameLink(account.label, account.id)),
          meta ? el('p', { class: 'who-meta', text: meta }) : null, creditLine(account), AD.resets.cardLine(account, ctx.nowMs))),
      figure(account, ctx),
      el('div', 'limit-stack', A.limits(account).map((win) => AD.meter.limit(win, account, Object.assign({ compact: true }, ctx)))),
    ];
    return card('inuse', { title: O.inUse, aside: headlineBadge(account) }, parts);
  }

  function inUseSig(state, ctx) {
    const account = A.active(state);
    if (!account) return 'none';
    const derived = (account.windows || []).map((w) => AD.model.deriveWindow(w, account, ctx.nowMs).state);
    return JSON.stringify([A.signature(account), derived, ctx.display, ctx.threshold, ctx.hide]);
  }

  // ---- Auto-switch ---------------------------------------------------------------------------------

  function autoToggle(state) {
    const auto = A.autoswitch(state);
    const provider = A.provider(state);
    const input = el('input', { type: 'checkbox', class: 'switch', role: 'switch', checked: !!auto.enabled,
      'aria-label': O.autoToggle, title: O.autoToggle, disabled: !!(provider && provider.readonly_reason),
      'data-keep': 'ov-auto-toggle' });
    input.addEventListener('change', () => {
      const on = input.checked;
      input.disabled = true;
      AD.api.settings({ cli_autoswitch: on }).catch((err) => {
        input.checked = !on;
        AD.dom.toast(err.message, { level: 'critical' });
      }).finally(() => { input.disabled = false; });
    });
    return input;
  }

  function queueRow(row) {
    const why = row.skip ? mask(row.skip) : (isNum(row.room) ? fill(O.room, { n: Math.round(row.room) }) : '');
    return el('li', { class: 'queue-row', dataset: { account: row.id, skipped: row.skip ? 'true' : null } },
      nameLink(row.label, row.id), el('span', { class: 'queue-why', text: why, title: why }));
  }

  /** The last limit error auto-switch answered (providers.claude_cli.watch.last, counted and with its reaction
   * time): "Moved 2.2 s after Delta hit its weekly limit · 5:13 PM", or null. */
  function reactionLine(state, nowMs) {
    const watch = (A.provider(state) || {}).watch;
    const last = watch && watch.last;
    const limit = last && STRINGS.watch.limits[last.kind];
    if (!last || !last.counted || !isNum(last.reaction_s) || !limit) return null;
    const account = A.terminals(state).find((a) => a.id === last.account);
    const seconds = String(Math.round(last.reaction_s * 10) / 10);
    return { text: fill(O.reaction, { seconds, limit, label: account ? mask(account.label) : O.reactionSomeone,
      clock: AD.model.clock(last.at, nowMs) }), tip: fill(O.reactionTip, { seconds }) };
  }

  function autoCard(state, ctx) {
    const auto = A.autoswitch(state);
    const active = A.active(state);
    const parts = [];
    if (!auto.enabled) {
      parts.push(el('div', 'figure-block', el('p', { class: 'ov-title', text: O.autoOff }),
        el('p', { class: 'ov-line', text: active ? fill(O.autoOffLine, { label: mask(active.label) }) : O.autoOffNoActive })));
      const best = A.bestPick(state);
      if (best) {
        parts.push(el('p', 'ov-line', `${O.best} `, el('strong', null, nameLink(best.account.label, best.account.id)),
          isNum(best.headroom) ? ` · ${fill(O.left, { n: Math.round(best.headroom) })}` : ''));
      }
    } else {
      // The account in use is never a target, so the queue leaves it out.
      const queue = (auto.next_up || []).filter((r) => !active || r.id !== active.id);
      const pick = queue.find((r) => !r.skip) || null;
      const rest = queue.filter((r) => r !== pick).slice(0, QUEUE_SHOWN);
      if (pick) {
        parts.push(el('div', 'ov-next', AD.dom.avatar(pick.label),
          el('p', 'who-name', nameLink(pick.label, pick.id)),
          isNum(pick.room) ? AD.meter.badge('accent', fill(O.room, { n: Math.round(pick.room) }),
            { title: fill(O.roomTip, { n: Math.round(pick.room), t: auto.threshold }) }) : null));
        parts.push(el('p', { class: 'ov-line', text: active
          ? fill(O.takesOver, { label: mask(active.label), n: auto.threshold })
          : fill(O.takesOverPlain, { n: auto.threshold }) }));
      } else {
        parts.push(el('div', 'figure-block', el('p', { class: 'ov-title', text: O.nobody }),
          el('p', { class: 'ov-line', text: rest.length ? O.nobodyLine : O.nobodyAlone })));
      }
      if (auto.hold) parts.push(el('p', 'ov-hold', AD.dom.svgIcon('pause'), el('span', { text: fill(O.hold, { reason: mask(auto.hold) }) })));
      if (rest.length) {
        parts.push(el('div', null, el('p', { class: 'queue-head', id: 'ov-queue-head', text: pick ? O.then : O.waitingList }),
          el('ol', { class: 'queue', 'aria-labelledby': 'ov-queue-head' }, rest.map(queueRow))));
      }
      const reaction = ctx.reaction;
      if (reaction) parts.push(el('p', { class: 'ov-reaction', title: reaction.tip }, AD.dom.svgIcon('bolt'), el('span', { text: reaction.text })));
    }
    const settings = AD.dom.button('', () => AD.settings.open(AD.app.state, 'autoswitch'),
      { kind: 'icon', icon: 'gear', label: O.settingsLink, title: O.settingsLink, attrs: { 'data-keep': 'ov-auto-settings' } });
    return card('auto', { title: O.auto, aside: el('div', 'card-tools', settings, autoToggle(state)) }, parts);
  }

  function autoSig(state, ctx) {
    const auto = A.autoswitch(state);
    const active = A.active(state);
    const provider = A.provider(state);
    return JSON.stringify([auto.enabled, auto.threshold, auto.next_up, auto.hold, active && [active.id, active.label],
      state.now_band && state.now_band.best && state.now_band.best.claude_cli, provider && provider.readonly_reason, ctx.hide,
      ctx.reaction]);
  }

  // ---- Next reset ----------------------------------------------------------------------------------

  /** What the card shows, from the terminal rows alone: a refill from the last hour first, else the
   * soonest reset ahead, else all clear; before any reading, "unknown". */
  function resetPick(state, nowMs) {
    const accounts = A.terminals(state);
    if (state.ready === false || !accounts.some((a) => isNum(a.last_good_at))) return { kind: 'unknown', row: null };
    const rows = AD.model.stripItems(state, nowMs).filter((r) => r.provider === 'claude_cli');
    const refilled = rows.find((r) => r.chip === 'refilled');
    if (refilled) return { kind: 'refilled', row: refilled };
    const next = rows.find((r) => r.chip !== 'refilled' && r.secs > 0);
    return next ? { kind: 'next_reset', row: next } : { kind: 'all_clear', row: null };
  }

  function resetCard(state, pick, ctx) {
    const { kind, row } = pick;
    const parts = [];
    let tier = null;
    if (kind === 'next_reset') {
      const account = A.terminals(state).find((a) => a.id === row.account);
      const win = account && (account.windows || []).find((w) => w.id === row.window);
      tier = AD.model.tier(row.secs);
      const usage = isNum(row.left) ? AD.model.formatNumber({ used: 100 - row.left }, ctx.display) : STRINGS.window.notReported;
      parts.push(el('div', 'figure-block',
        el('div', 'figure-row',
          el('p', 'figure-line', el('span', { class: 'figure', role: 'timer', 'aria-live': 'off', dataset: { reset: row.reset },
            text: AD.model.countdownText(row.secs) })),
          el('p', { class: 'figure-caption', text: fill(O.refillsAt, { clock: AD.model.clock(row.reset, ctx.nowMs) }) })),
        el('p', 'ov-line', el('strong', null, nameLink(row.label, row.account)), ` · ${fill(O.resetWindow, { window: row.window_label })} · ${usage}`)));
      if (win) parts.push(AD.meter.meter(win, { account, nowMs: ctx.nowMs, display: ctx.display, size: 'large' }));
    } else if (kind === 'refilled') {
      parts.push(el('div', 'figure-block', el('p', 'ov-title ov-title-icon', AD.dom.svgIcon('check-circle'), O.refilled),
        el('p', { class: 'ov-line', text: fill(O.refilledLine, { label: mask(row.label), window: String(row.window_label || '').toLowerCase() }) })));
    } else if (kind === 'all_clear') {
      parts.push(el('div', 'figure-block', el('p', { class: 'ov-title', text: O.allClear }), el('p', { class: 'ov-line', text: O.allClearLine })));
    } else {
      parts.push(el('div', 'figure-block', el('p', { class: 'ov-title', text: O.unknownTitle }), el('p', { class: 'ov-line', text: O.unknownLine })));
    }
    // data-tier lets the countdown take the accent once the reset is near; the clock keeps it current.
    return card('reset', { title: O.nextReset }, parts, { dataset: { kind, tier } });
  }

  function resetSig(pick, ctx) {
    const row = pick.row;
    return JSON.stringify([pick.kind, row && [row.account, row.label, row.window, row.reset, row.left], ctx.display, ctx.hide]);
  }

  // ---- Fable 5.1 room ----------------------------------------------------------------------------------

  function fableLabel(state, board) {
    if (board.length) return board[0].win.label;
    for (const account of A.terminals(state)) {
      const win = A.fableWindow(account);
      if (win && win.label) return win.label;
    }
    return O.fableFallback;
  }

  function boardRow(row, index, leader, nowMs) {
    const lead = row.id === leader;
    const value = row.d.left === null ? O.notReported : fill(O.left, { n: Math.round(row.d.left) });
    return el('li', { class: 'board-row', dataset: { account: row.id, leader: lead ? 'true' : null } },
      el('span', { class: 'board-rank' }, lead ? AD.dom.svgIcon('crown', { label: O.fableTag }) : String(index + 1)),
      el('span', 'board-name', nameLink(row.label, row.id)),
      el('span', { class: 'board-value', text: value }),
      AD.meter.meter(row.win, { account: row.account, nowMs, display: 'left' }));
  }

  function fableCard(state, ctx) {
    const board = A.fableBoard(A.terminals(state), ctx.nowMs);
    const label = fableLabel(state, board);
    const heading = { title: [el('span', { class: 'fable-mark', 'aria-hidden': 'true' }), fill(O.fableRoom, { label })] };
    if (!board.length) {
      return card('fable', heading, el('p', 'ov-line', el('strong', { text: O.noFable }), ` — ${O.noFableLine}`), { dataset: { board: 'empty' } });
    }
    const leader = A.fableLeader(board);
    const top = board[0];
    const parts = [];
    if (leader) {
      parts.push(el('div', 'figure-block', el('p', 'ov-title', nameLink(top.label, top.id)),
        el('p', { class: 'ov-line', text: top.d.reset
          ? fill(O.leaderLine, { n: Math.round(top.d.left), clock: AD.model.clock(top.d.reset, ctx.nowMs) })
          : fill(O.left, { n: Math.round(top.d.left) }) })));
    } else {
      parts.push(el('p', { class: 'ov-title', text: O.tie }));
    }
    parts.push(el('ol', { class: 'board', 'aria-label': fill(O.fableRoom, { label }) },
      board.slice(0, BOARD_SHOWN).map((row, index) => boardRow(row, index, leader, ctx.nowMs))));
    return card('fable', heading, parts, { dataset: { board: 'ready' } });
  }

  function fableSig(state, ctx) {
    const board = A.fableBoard(A.terminals(state), ctx.nowMs);
    return JSON.stringify([board.map((r) => [r.id, r.label, r.win.label, r.win.reset, r.d.state, r.d.used]),
      A.fableLeader(board), ctx.hide]);
  }

  // ---- Render ----------------------------------------------------------------------------------------

  /** Render the overview at nowMs; cheap when nothing but the time changed. Returns whether it shows. */
  function render(state, nowMs) {
    const host = document.getElementById('overview');
    if (!host || !state) return false;
    if (!A.provider(state) || !A.terminals(state).length) {
      host.replaceChildren();
      return false;
    }
    const auto = A.autoswitch(state);
    const ctx = { nowMs, display: A.display(state), threshold: auto.enabled && isNum(auto.threshold) ? auto.threshold : null,
      hide: AD.dom.hideEmails(), reaction: reactionLine(state, nowMs) };
    const pick = resetPick(state, nowMs);
    const sum = totals(state, nowMs);
    const runway = runwayLine(state, nowMs);
    const resets = AD.resets.total(A.terminals(state));
    const due = dueRows(state, nowMs);
    const provider = A.provider(state);
    const readonly = provider.readonly_reason || (state.read_only === true ? STRINGS.accounts.readOnlyOther : null);
    AD.dom.patchSigned(host, [
      sum.count || sum.held || sum.signin || sum.unread || sum.left_out
        ? { key: 'total', sig: totalSig(state, sum, runway, resets), build: () => totalCard(state, sum, runway, resets, ctx) } : null,
      due.length ? { key: 'due', sig: JSON.stringify([due.map((r) => [r.id, r.label, r.email, r.state, r.text, r.urgent]), readonly,
        !!state.pending, ctx.hide]), build: () => dueCard(state, due, readonly) } : null,
      { key: 'inuse', sig: inUseSig(state, ctx), build: () => inUseCard(state, ctx) },
      { key: 'auto', sig: autoSig(state, ctx), build: () => autoCard(state, ctx) },
      { key: 'reset', sig: resetSig(pick, ctx), build: () => resetCard(state, pick, ctx) },
      { key: 'fable', sig: fableSig(state, ctx), build: () => fableCard(state, ctx) },
    ].filter(Boolean));
    return true;
  }

  AD.overview = { render, resetPick, totals };
})();
