/* AccountDeck 2 — banners and notices. Three mount points:
 *   #system  — the connection state, stalled checks and AccountDeck's own health (only when something
 *              fails) as banners; demo and safe mode as one slim strip (with the demo time scrubber);
 *   #banners — the import from PC, the pending operation, the last switch's report, a sign-in in
 *              progress, sessions a limit stopped, the service's banners in order, then a newer
 *              AccountDeck release (update.js) last: it is the one that can always wait;
 *   #notices — sticky notices above the accounts (non-sticky ones become toasts).
 * Server titles and messages are shown as sent. Every list is keyed and patched, so a control
 * inside a banner keeps focus across polls. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;

  const DEV_KINDS = new Set(['demo', 'safe_mode']);
  const LOOK = {
    outage: ['warning', 'warning'], degraded: ['warning', 'warning'], recovery: ['warning', 'warning'],
    paused: ['pause', 'info'], self_protect: ['stop', 'warning'], read_only: ['lock', 'warning'],
    move_import: ['import', 'info'],
  };
  const NOTICE_ICONS = { info: 'info', good: 'check-circle', warning: 'warning', critical: 'warning' };
  const DEMO_STEP_MS = 15 * 60000;
  const DEMO_STEPS = 96;                       // ±24 h in 15-minute steps
  const HEALTH_MS = 30000;
  const STOPPED_KEY = 'ad2.stoppedHidden';     // sessions the owner hid from the stopped-sessions banner (this browser)

  let last = null;
  let health = null;                           // the engine's liveness from /_health (null until known)
  let known = null;                            // banner keys already on screen (announce the new ones)
  let noticesSeen = null;                      // notice ids already handled
  let stepSeen;                                // the pending step last announced (undefined before the first state)
  let hiddenStops = loadHiddenStops();         // "session|at" of stopped sessions hidden with Hide
  let stopsSeen = null;                        // stopped sessions listed at the last render (null before the first)
  let updateSeen;                              // the version offered at the last render (undefined before the first)

  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);
  const keyOf = (b) => `${b.kind}:${b.provider || ''}`;

  // ---- Building blocks --------------------------------------------------------------------------

  function banner({ kind, icon, level, title, message, actions, link, extra, compact }) {
    return el('div', { class: ['banner', compact && 'is-compact'], dataset: { kind, level }, role: level === 'error' ? 'alert' : null },
      icon ? el('span', 'banner-icon', AD.dom.svgIcon(icon)) : null,
      el('div', 'banner-text',
        title ? el('p', { class: 'banner-title', text: title }) : null,
        message ? el('p', { class: 'banner-message', text: message }) : null,
        extra || null),
      (actions && actions.length) || link ? el('div', 'banner-actions', actions || [], link || null) : null);
  }

  function statusLink(url) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (err) {
      return null;
    }
    if (parsed.protocol !== 'https:') return null;
    return el('a', { class: 'banner-link', href: parsed.href, target: '_blank', rel: 'noopener noreferrer',
      title: STRINGS.outage.link }, parsed.host, AD.dom.svgIcon('external'));
  }

  // ---- #system: connection, stalled checks, demo and safe mode ----------------------------------

  function connectionItem(status) {
    const copy = {
      offline: [STRINGS.connection.offlineTitle, fill(STRINGS.connection.offline, { reopen: AD.format.label('reopen_hint') })],
      signin: [STRINGS.connection.signinTitle, AD.format.label('open_hint')],
      closed: [STRINGS.connection.closedTitle, AD.format.label('closed_hint')],
    }[status];
    if (!copy) return null;
    return {
      key: `connection:${status}`, sig: `${status}|${copy[1]}`,
      build: () => banner({ kind: 'connection', icon: status === 'closed' ? 'power' : 'warning',
        level: status === 'closed' ? 'info' : 'error', title: copy[0], message: copy[1],
        actions: status === 'offline' ? [AD.dom.button(STRINGS.button.retry, () => AD.api.poll(), { small: true })] : [] }),
    };
  }

  /** Background checks stopped or terminal readings went stale (the engine's own judgement). */
  function stalledItem() {
    if (!health || !health.stalled) return null;
    const engine = health.engine || {};
    const reasons = engine.stall_reasons || [];
    const tick = reasons.includes('tick');
    const lastOk = engine.reads && engine.reads.claude_cli && engine.reads.claude_cli.last_ok_at;
    const title = tick ? 'Background checks stopped' : "Terminal usage hasn't updated for 20 minutes";
    const when = typeof lastOk === 'number' ? AD.model.clock(lastOk, AD.app.clockNow()) : 'unknown';
    const message = tick ? 'Quit and reopen AccountDeck 2 to resume usage checks and auto-switching.'
      : `Last good reading at ${when}. Auto-switch is working from old numbers.`;
    return { key: 'stalled', sig: `${title}|${message}`,
      build: () => banner({ kind: 'stalled', icon: 'warning', level: 'warning', title, message }) };
  }

  /** AccountDeck's own health, only when something fails (AD.details.problems): the first problem and what to
   * do about it; the full list waits in Activity & troubleshooting. */
  function healthItem(state) {
    const problems = AD.details ? AD.details.problems(state) : [];
    if (!problems.length) return null;
    const H = STRINGS.health;
    const [first] = problems;
    const more = problems.length > 1 ? ` (${fill(H.more, { n: problems.length - 1 })})` : '';
    return {
      key: 'health', sig: JSON.stringify(problems),
      build: () => banner({ kind: 'health', icon: 'pulse', level: 'error', title: `${H.bad}${more}`, message: first.text,
        extra: first.action ? el('p', { class: 'banner-action', text: first.action }) : null,
        actions: [AD.dom.button(H.show, () => AD.details.showHealth(), { small: true, attrs: { 'data-keep': 'health-show' } })] }),
    };
  }

  function offsetText(ms) {
    if (!ms) return STRINGS.shell.demoNow;
    const minutes = Math.round(Math.abs(ms) / 60000);
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    const offset = h && m ? `${h}h ${m}m` : (h ? `${h}h` : `${m}m`);
    return fill(STRINGS.shell.demoOffset, { sign: ms > 0 ? '+' : '−', offset });
  }

  /** The demo time scrubber: moves the synthetic engine in 15-minute steps up to ±24 h. */
  function scrubber() {
    const shiftMs = AD.app.demoShift();
    const input = el('input', { type: 'range', id: 'demo-clock', class: 'demo-range', min: -DEMO_STEPS, max: DEMO_STEPS,
      step: 1, value: String(Math.round(shiftMs / DEMO_STEP_MS)), 'aria-valuetext': offsetText(shiftMs), 'data-keep': 'demo-clock' });
    const output = el('output', { class: 'demo-offset', for: 'demo-clock', text: offsetText(shiftMs) });
    const apply = (ms) => {
      output.textContent = offsetText(ms);
      input.setAttribute('aria-valuetext', offsetText(ms));
      AD.app.setDemoShift(ms);
    };
    input.addEventListener('input', () => apply(Number(input.value) * DEMO_STEP_MS));
    const reset = AD.dom.button(STRINGS.button.reset, () => {
      input.value = '0';
      apply(0);
    }, { kind: 'ghost', small: true, attrs: { 'data-keep': 'demo-reset' } });
    return el('div', 'demo-scrubber', el('label', { class: 'demo-label', for: 'demo-clock', text: STRINGS.shell.demo }),
      input, output, reset);
  }

  /** Demo and safe mode: development states, kept to one quiet line. */
  function devItem(list) {
    if (!list.length) return null;
    const demo = list.find((b) => b.kind === 'demo');
    return {
      key: 'dev', sig: JSON.stringify(list.map((b) => [b.kind, b.title, b.message])),
      build: () => el('div', { class: 'dev-strip', role: 'note' },
        el('p', 'dev-strip-text', list.map((b) => el('span', { class: 'dev-item', title: b.message },
          AD.meter.badge(b.kind === 'demo' ? 'accent' : 'neutral', b.title, { icon: b.kind === 'demo' ? 'play' : 'lock' }),
          el('span', { class: 'sr-only', text: ` ${b.message}` })))),
        demo ? scrubber() : null),
    };
  }

  function renderSystem(state) {
    const items = [];
    const status = AD.app.connection;
    if (status && status !== 'ok') {
      const item = connectionItem(status);
      if (item) items.push(item);
    }
    const stalled = stalledItem();
    if (stalled) items.push(stalled);
    const unwell = state ? healthItem(state) : null;
    if (unwell) items.push(unwell);
    const dev = devItem(((state && state.banners) || []).filter((b) => DEV_KINDS.has(b.kind)));
    if (dev) items.push(dev);
    AD.dom.patchSigned(document.getElementById('system'), items);
  }

  // ---- #banners: pending, report, sign-in, server banners ----------------------------------------

  function sessionNames(list) {
    return list.map((s) => `“${s.name}” (${STRINGS.session[s.status] || STRINGS.session.unknown})`).join(', ');
  }

  function pendingItem(state) {
    const pending = state.pending;
    const label = mask(pending.label || '');
    const active = (pending.steps || []).find((s) => s.status === 'active');
    const switching = pending.kind === 'switch' && pending.label;
    let title = switching ? fill(STRINGS.progress.title, { label }) : (pending.message || STRINGS.modal.progressTitle);
    if (pending.kind === 'use-reset') title = pending.label ? fill(STRINGS.resets.progressOn, { label }) : STRINGS.resets.progress;
    let message = active ? active.label : (switching || pending.kind === 'use-reset' ? pending.message : '');
    if (pending.waiting_on && pending.waiting_on.length) {
      message = fill(STRINGS.progress.waitingOn, { names: sessionNames(pending.waiting_on) });
    }
    const reconfirm = pending.phase === 'needs_reconfirm';
    if (reconfirm) message = pending.message || STRINGS.modal.reconfirm;
    const actions = [];
    if (pending.cancellable) {
      actions.push(AD.dom.button(STRINGS.button.cancel, () => AD.api.action('cancel', { op: pending.id }),
        { small: true, attrs: { 'data-keep': 'pending-cancel' } }));
    }
    actions.push(AD.dom.button(reconfirm ? STRINGS.button.review : STRINGS.button.progress,
      () => AD.switch.renderPending(last, { open: true }), { small: true, kind: reconfirm ? 'primary' : undefined,
        attrs: { 'data-keep': 'pending-view' } }));
    return {
      key: `pending:${pending.id}`, sig: JSON.stringify([pending, AD.dom.hideEmails()]),
      build: () => banner({ kind: 'pending', icon: 'hourglass', level: 'info', title, message: mask(message), actions }),
    };
  }

  function reportItem(state) {
    const report = state.report;
    const label = mask(report.label || '');
    let title = report.summary;
    let icon = 'check-circle';
    let level = 'good';
    if (report.outcome === 'failed') {
      title = STRINGS.report.failedTitle;
      icon = 'warning';
      level = 'warning';
    } else if (report.outcome === 'cancelled') {
      title = STRINGS.report.cancelledTitle;
      icon = 'info';
      level = 'info';
    } else if (report.kind === 'switch' && report.label) {
      title = fill(STRINGS.report.switched, { label });
      if (report.verified !== 'verified') icon = 'hourglass';
    }
    // Using a limit reset: a failure that used nothing, or whose outcome is not known yet, is information, not damage.
    if (report.kind === 'use-reset') ({ title, icon, level } = AD.resets.reportLook(report));
    const message = title === report.summary ? '' : mask(report.summary);
    const actions = [
      AD.dom.button(STRINGS.button.viewReport, () => AD.switch.renderReport(last, { open: true }),
        { small: true, attrs: { 'data-keep': 'report-view' } }),
      AD.dom.button(STRINGS.button.done, () => AD.api.action('dismiss-report', {}),
        { small: true, attrs: { 'data-keep': 'report-done' } }),
    ];
    return {
      key: `report:${report.op}`, sig: JSON.stringify([report, AD.dom.hideEmails()]),
      build: () => banner({ kind: 'report', icon, level, title: mask(title), message, actions }),
    };
  }

  function loginItem(state) {
    const login = state.login;
    const codex = login.provider === 'codex';
    return {
      key: `login:${login.provider}`, sig: JSON.stringify(login),
      build: () => banner({ kind: 'login', icon: 'info', level: 'info',
        title: codex ? STRINGS.login.codexTitle : STRINGS.login.claudeTitle,
        message: codex ? STRINGS.login.codex : STRINGS.login.claude,
        actions: AD.switch.loginActions(login, { small: true }) }),
    };
  }

  // ---- Sessions a limit stopped (providers.claude_cli.watch.stopped) ------------------------------

  function loadHiddenStops() {
    try {
      const stored = JSON.parse(window.localStorage.getItem(STOPPED_KEY) || '[]');
      return new Set(Array.isArray(stored) ? stored.filter((key) => typeof key === 'string') : []);
    } catch (err) {
      return new Set();            // unreadable or blocked storage: nothing is hidden
    }
  }

  const stopKey = (row) => `${row.session}|${row.at}`;

  function stoppedRows(state) {
    const watch = ((state.providers || {}).claude_cli || {}).watch;
    return (watch && Array.isArray(watch.stopped) ? watch.stopped : []).filter((row) => row && row.session);
  }

  /** Hide the sessions listed now (a session stopped later shows again). Only keys still listed are kept. */
  function hideStops(rows) {
    hiddenStops = new Set(rows.map(stopKey));
    try {
      window.localStorage.setItem(STOPPED_KEY, JSON.stringify(Array.from(hiddenStops)));
    } catch (err) { /* kept for this page only */ }
    if (last) renderBanners(last);
  }

  /** Sessions that stopped at a limit and wait for a "continue" from the owner: AccountDeck never types into a
   * session. The service forgets one once it goes on, or after a day. */
  function stoppedItem(state) {
    const rows = stoppedRows(state);
    const shown = rows.filter((row) => !hiddenStops.has(stopKey(row)));
    if (!shown.length) return null;
    const W = STRINGS.watch;
    const active = AD.accounts.active(state);
    const nowMs = AD.app.clockNow();
    const lines = shown.map((row) => ({
      key: stopKey(row), name: row.name ? mask(row.name) : null,
      detail: fill(W.row, { where: row.project ? `${mask(row.project)} · ` : '', limit: W.limits[row.kind] || W.limits.session,
        clock: AD.model.clock(row.at, nowMs) }),
    }));
    const title = shown.length === 1 ? W.stoppedOne : fill(W.stopped, { n: shown.length });
    // "Now use X" only when the terminals did move to an account with room: if the account in use is the one the
    // limit hit (unless a reading since shows it has room again), or is itself out, a "continue" now would only hit
    // a limit again.
    const last = (((state.providers || {}).claude_cli || {}).watch || {}).last;
    const hit = !!active && !!last && last.account === active.id
      && !(typeof active.last_good_at === 'number' && active.last_good_at > last.at);
    const moved = active && !AD.accounts.blocked(active) && !hit;
    const message = `${moved ? fill(W.message, { label: mask(active.label) }) : W.messageWait} ${W.never}`;
    return {
      key: 'stopped', sig: JSON.stringify([lines, title, message]), stops: lines.map((line) => line.key), title,
      build: () => banner({ kind: 'stopped', icon: 'pause', level: 'warning', title, message,
        extra: el('ul', { class: 'stopped-list', 'aria-label': title }, lines.map((line) => el('li', 'stopped-row',
          el('span', { class: ['stopped-name', !line.name && 'is-quiet'], text: line.name || W.unnamed }),
          el('span', { class: 'stopped-detail', text: line.detail })))),
        actions: [AD.dom.button(STRINGS.button.hide, () => hideStops(rows), { small: true, attrs: { 'data-keep': 'stopped-hide' } })] }),
    };
  }

  // ---- A newer AccountDeck release (state.update) --------------------------------------------------

  /** Only while a newer version is offered: never while it is skipped, up to date, unchecked, off, failed or
   * unavailable. One calm banner; the versions are checked (update.js), the rest of the words are the page's. */
  function updateItem(state) {
    const update = AD.update && AD.update.offered(state.update);
    if (!update) return null;
    const U = STRINGS.update;
    const blocked = typeof update.blocked === 'string' && update.blocked ? update.blocked : null;
    const title = fill(U.title, { version: update.latest });
    return {
      key: 'update', sig: JSON.stringify([update.latest, update.current, update.url, blocked]), version: update.latest, title,
      // Why Update… cannot start now (a switch is running, the demo) follows on the same line.
      build: () => banner({ kind: 'update', icon: 'arrow-down-circle', level: 'info', title,
        extra: el('p', 'banner-message', fill(U.current, { current: update.current }),
          blocked ? ' ' : null, blocked ? el('span', { id: 'update-blocked', text: blocked }) : null),
        actions: AD.update.bannerActions(update, 'update-blocked') }),
    };
  }

  /** A newly offered version is said once (the first render only records it). Not while Settings is open: there a
   * Check Now says what it found itself. */
  function announceUpdate(item) {
    const version = item ? item.version : null;
    if (updateSeen !== undefined && version && version !== updateSeen && !AD.settings.isOpen()) AD.dom.announce(item.title);
    updateSeen = version;
  }

  function actionButton(name, b) {
    // Antigravity has its own Recover sheet and its own Refresh (antigravity.js): never another app's.
    const agy = b.provider === 'antigravity' && AD.antigravity;
    const run = {
      recover: agy ? () => AD.antigravity.recover() : () => AD.switch.openPurpose(b.provider || 'claude', 'recover'),
      refresh: agy ? () => AD.antigravity.refresh() : () => AD.api.action('refresh', {}),
    }[name];
    if (!run) return null;
    const text = { recover: STRINGS.button.recover, refresh: STRINGS.button.refresh }[name];
    return AD.dom.button(text, run, { small: true, attrs: { 'data-keep': `banner-${name}` } });
  }

  function serverItem(b) {
    const [icon, level] = LOOK[b.kind] || ['info', 'info'];
    return {
      key: keyOf(b), sig: JSON.stringify(b),
      build: () => {
        const actions = [];
        for (const name of b.actions || []) {
          const node = actionButton(name, b);
          if (node && !actions.some((other) => other.textContent === node.textContent)) actions.push(node);
        }
        if (b.kind === 'paused') {
          actions.push(AD.dom.button(STRINGS.button.resume, () => AD.api.settings({ refresh_paused: false }),
            { small: true, attrs: { 'data-keep': 'banner-resume' } }));
        }
        return banner({ kind: b.kind, icon, level, title: b.title, message: b.message, actions,
          link: b.link ? statusLink(b.link) : null });
      },
    };
  }

  /** The service's banners for this page: not the dev strip, and one incident
   * banner when several providers report the same incident. */
  function pageBanners(state) {
    const seen = new Set();
    const list = [];
    for (const b of (state && state.banners) || []) {
      if (DEV_KINDS.has(b.kind)) continue;
      if (b.kind === 'outage' || b.kind === 'degraded') {
        const same = `${b.kind}|${b.message}|${b.link || ''}`;
        if (seen.has(same)) continue;
        seen.add(same);
      }
      list.push(b);
    }
    return list;
  }

  function renderBanners(state) {
    const items = [];
    if (state) {
      if (AD.move) items.push(...AD.move.bannerItems(state));
      const view = AD.dom.modalView();
      if (state.pending && view !== 'progress') items.push(pendingItem(state));
      if (state.report && !state.pending && view !== 'report' && view !== 'progress') items.push(reportItem(state));
      if (state.login && view !== 'login') items.push(loginItem(state));
      const stopped = stoppedItem(state);
      if (stopped) items.push(stopped);
      for (const b of pageBanners(state)) items.push(serverItem(b));
      const update = updateItem(state);
      if (update) items.push(update);
      announceStops(stopped);
      announceUpdate(update);
    }
    const host = document.getElementById('banners');
    const focused = host.contains(document.activeElement);
    AD.dom.patchSigned(host, items);
    // A banner that went away with the focus in it (Skip This Version, Done) leaves the focus on the page, not lost.
    if (focused && !host.contains(document.activeElement)) document.getElementById('main').focus({ preventScroll: true });
    announceNew(state);
    announceStep(state);
  }

  /** A session newly stopped at a limit is said once (the first render only records what is listed). */
  function announceStops(item) {
    const keys = new Set(item ? item.stops : []);
    if (stopsSeen && item && item.stops.some((key) => !stopsSeen.has(key))) AD.dom.announce(item.title);
    stopsSeen = keys;
  }

  /** A pending operation's step changes are announced while its progress view is closed (the view
   * announces them itself when open). */
  function announceStep(state) {
    if (!state) return;
    const pending = state.pending;
    const active = pending && (pending.steps || []).find((s) => s.status === 'active');
    const key = pending ? `${pending.id}|${active ? active.id : pending.phase}` : null;
    if (stepSeen !== undefined && key && key !== stepSeen && AD.dom.modalView() !== 'progress') {
      AD.dom.announce(active ? active.label : pending.message);
    }
    stepSeen = key;
  }

  function announceNew(state) {
    const titles = new Map(pageBanners(state).map((b) => [keyOf(b), b.title]));
    if (known) {
      for (const [key, title] of titles) {
        if (!known.has(key)) AD.dom.announce(title);
      }
    }
    if (state) known = new Set(titles.keys());
  }

  // ---- #notices -----------------------------------------------------------------------------------

  function renderNotices(state) {
    const notices = (state && state.notices) || [];
    const sticky = notices.filter((n) => n.sticky).map((n) => ({
      key: `notice:${n.id}`, sig: JSON.stringify([n, AD.dom.hideEmails()]),
      build: () => el('div', { class: 'notice', dataset: { level: n.level } },
        el('span', 'notice-icon', AD.dom.svgIcon(NOTICE_ICONS[n.level] || 'info')),
        el('p', { class: 'notice-text', text: mask(n.message) }),
        AD.dom.button(STRINGS.button.dismiss, () => AD.api.action('dismiss', { notice: n.id }),
          { kind: 'ghost', small: true, attrs: { 'data-keep': `notice-${n.id}` } })),
    }));
    AD.dom.patchSigned(document.getElementById('notices'), sticky);
    if (!state) return;
    if (noticesSeen) {
      for (const n of notices) {
        if (!n.sticky && !noticesSeen.has(n.id)) AD.dom.toast(mask(n.message), { level: n.level, sticky: false });
      }
    }
    noticesSeen = new Set(notices.map((n) => n.id));
  }

  // ---- Engine health ------------------------------------------------------------------------------

  async function pollHealth() {
    if (document.hidden) return;
    let next = null;
    try {
      const response = await window.fetch('/_health', { cache: 'no-store' });
      if (response.ok) next = await response.json();
    } catch (err) { /* the connection banner covers an unreachable service */ }
    const changed = JSON.stringify(next && [next.stalled, (next.engine || {}).stall_reasons])
      !== JSON.stringify(health && [health.stalled, (health.engine || {}).stall_reasons]);
    health = next;
    if (changed && last) renderSystem(last);
  }

  /** Render every banner slot from the state (null before the first state arrives). */
  function render(state) {
    last = state;
    renderSystem(state);
    renderBanners(state);
    renderNotices(state);
  }

  // The pending and report banners stand in for the modal views while those are closed.
  document.addEventListener('ad:modal', () => {
    if (last) renderBanners(last);
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) pollHealth();
  });
  pollHealth();
  window.setInterval(pollHealth, HEALTH_MS);

  AD.banners = { render };
})();
