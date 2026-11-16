/* AccountDeck 2 — the shell. Loaded last.
 * It signs the page in, polls the state, renders every module in turn (one module's failure never
 * blanks the others), runs the adaptive clock and owns the toolbar, the footer and the shortcuts.
 * The clock is one setTimeout: every second while a displayed reset is under 5 minutes away, every
 * 30 s under an hour, otherwise on the minute (always on the minute while the tab is hidden). Each
 * tick patches [data-reset] and [data-ago] text in place; time-driven modules skip unchanged work. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;

  const STATUS_LEVELS = ['ok', 'degraded', 'outage', 'unknown'];
  const SECOND_TICKS_UNDER = 300;
  const HALF_MINUTE_TICKS_UNDER = 3600;
  const STILL_CHECKING_MS = 10000;
  const REFRESH_WATCH_MS = 120000;
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

  let current = null;
  let connection = 'ok';
  let booted = false;
  let tickTimer = null;
  // Demo mode runs the engine on a virtual clock; the page follows it. The engine's `now` can lag
  // behind by up to a poll, so the largest offset seen is the best estimate. Live mode shares this
  // machine's clock, so no offset applies there.
  let clockOffset = 0;
  let clockMode = null;
  let demoOffset = 0;
  let refresh = null;                          // {asked, checked} while a user refresh is being watched
  let tiers = null;                            // "account|window" → tier at the last tick
  let refilled = null;                         // keys shown as refilled at the last tick
  let pillSig = null;

  function clockNow() {
    return Date.now() + clockOffset;
  }

  function observeClock(state) {
    if (state.mode !== clockMode) {
      clockMode = state.mode;
      clockOffset = 0;
      if (state.mode === 'demo' && isNum(state.now)) clockOffset = state.now * 1000 - Date.now();
      return;
    }
    if (state.mode === 'demo' && isNum(state.now)) clockOffset = Math.max(clockOffset, state.now * 1000 - Date.now());
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function step(name, work) {
    try {
      return work();
    } catch (err) {
      console.error(`AccountDeck 2: ${name} could not render.`, err);
      return undefined;
    }
  }

  // ---- Rendering --------------------------------------------------------------------------------

  /** Render a new state: every module, then the shell, then the clock. */
  function render(state) {
    if (!state || typeof state !== 'object') return;
    observeClock(state);
    current = state;
    AD.format.setPlatform(state.platform);
    AD.api.setFast(!!(state.pending || state.login) || !!(AD.antigravity && AD.antigravity.waiting(state)));
    const nowMs = clockNow();
    step('banners', () => AD.banners.render(state));
    renderTimed(state, nowMs);
    step('pending', () => AD.switch.renderPending(state));
    step('report', () => AD.switch.renderReport(state));
    step('resets', () => AD.resets.render(state));
    step('login', () => AD.switch.renderLogin(state));
    step('settings', () => AD.settings.render(state));
    step('details', () => AD.details.render(state));
    step('alerts', () => AD.alerts.render(state, nowMs));
    step('shell', () => renderShell(state));
    watchRefresh(state);
    settle(nowMs);
  }

  /** The parts that follow the clock: the overview and the accounts. */
  function renderTimed(state, nowMs) {
    const shown = step('overview', () => AD.overview.render(state, nowMs));
    byId('layout').classList.toggle('is-single', !shown);
    step('accounts', () => AD.terminal.render(state, nowMs));
    step('desktop', () => AD.desktop.render(state, nowMs));
    step('codex', () => AD.codex.render(state, nowMs));
    step('antigravity', () => AD.antigravity.render(state, nowMs));
  }

  function renderShell(state) {
    renderPills(state);
    const refreshing = (state.accounts || []).some((account) => account.refreshing);
    byId('main').classList.toggle('is-refreshing', refreshing);
    byId('refresh').classList.toggle('is-spinning', refreshing || !!refresh);
    if (state.version) {
      const text = [fill(STRINGS.shell.version, { version: state.version }), buildText(state.build)].filter(Boolean).join(' · ');
      if (byId('version').textContent !== text) byId('version').textContent = text;
      const at = state.build && state.build.installed_at;
      // The clock read at its own moment names no weekday: "Installed Nov 1 at 6:40 PM".
      const tip = isNum(at) ? fill(STRINGS.shell.buildTip, { date: AD.format.dateText(at), clock: AD.model.clock(at, at * 1000) }) : null;
      if (tip) byId('version').title = tip;
      else byId('version').removeAttribute('title');
    }
  }

  /** "c58fa73 · installed Nov 1" from install.sh's stamp (`state.build`, null in a checkout); a package install
   * knows no commit. */
  function buildText(build) {
    if (!build || typeof build !== 'object' || !isNum(build.installed_at)) return null;
    const date = AD.format.dateText(build.installed_at);
    const commit = typeof build.commit === 'string' && build.commit ? build.commit.slice(0, 7) : null;
    if (!commit) return fill(STRINGS.shell.buildPackage, { date });
    return fill(build.dirty ? STRINGS.shell.buildDirty : STRINGS.shell.build, { commit, date });
  }

  /** Only problems show: a healthy provider needs no pill. The terminal provider reports the shared
   * Claude status; the Desktop entry stands in when it is absent. */
  function renderPills(state) {
    const providers = state.providers || {};
    const candidates = ['claude_cli', 'claude'].map((id) => providers[id] && providers[id].status).filter(Boolean);
    const claude = candidates.find((s) => s.level === 'outage') || candidates.find((s) => s.level === 'degraded') || null;
    const statuses = [['Claude', claude], ['Codex', providers.codex && providers.codex.status]]
      .filter(([, status]) => status && ['outage', 'degraded'].includes(status.level));
    const sig = JSON.stringify(statuses);
    if (sig === pillSig) return;
    pillSig = sig;
    const host = byId('status-pills');
    host.replaceChildren(...statuses.map(([name, status]) => {
      const level = STATUS_LEVELS.includes(status.level) ? status.level : 'unknown';
      const words = [AD.dom.el('span', { class: 'status-dot', 'aria-hidden': 'true' }),
        AD.dom.el('span', { text: name }), AD.dom.el('span', { class: 'status-word', text: ` · ${STRINGS.status[level]}` })];
      let link = null;
      try {
        const url = status.url ? new URL(status.url) : null;
        if (url && url.protocol === 'https:') {
          link = AD.dom.el('a', { href: url.href, target: '_blank', rel: 'noopener noreferrer', title: status.summary || STRINGS.outage.link }, words);
        }
      } catch (err) { /* no link */ }
      return AD.dom.el('li', { class: 'status-pill', dataset: { level }, title: link ? null : status.summary }, link || words);
    }));
  }

  // ---- The clock --------------------------------------------------------------------------------

  /** One clock tick: time-driven modules re-render (they skip unchanged work), text is patched in place. */
  function tick() {
    const nowMs = clockNow();
    if (current) {
      renderTimed(current, nowMs);
      step('alerts', () => AD.alerts.tick(current, nowMs));
    }
    settle(nowMs);
  }

  function settle(nowMs) {
    const soonest = patchTimes(nowMs);
    if (current) announceChanges(current, nowMs);
    renderClockline(nowMs);
    schedule(nowMs, soonest);
  }

  /** Patch every [data-reset] (epoch s) and [data-ago] (epoch s) at nowMs; returns the seconds to the
   * soonest reset still ahead. data-reset-template / data-ago-template wrap the value ("Resets in
   * {countdown}", "Checked {age} ago"); once a templated reset has passed it reads data-reset-done
   * (default "Resetting…"). data-reset-attr names an attribute to write instead of the text (a
   * meter's aria-valuetext). The tier goes to the nearest ancestor that carries data-tier. */
  function patchTimes(nowMs) {
    const now = nowMs / 1000;
    let soonest = Infinity;
    for (const node of document.querySelectorAll('[data-reset]')) {
      const reset = Number(node.getAttribute('data-reset'));
      if (!Number.isFinite(reset)) continue;
      const secs = reset - now;
      const template = node.getAttribute('data-reset-template');
      let text = AD.model.countdownText(secs);
      if (template) {
        const done = node.getAttribute('data-reset-done');
        text = secs <= 0 ? (done !== null ? done : STRINGS.window.resetting) : fill(template, { countdown: text });
      }
      const attr = node.getAttribute('data-reset-attr');
      if (attr) {
        if (node.getAttribute(attr) !== text) node.setAttribute(attr, text);
      } else if (node.textContent !== text) {
        node.textContent = text;
      }
      const host = node.closest('[data-tier]');
      const tier = AD.model.tier(secs);
      if (host && host.getAttribute('data-tier') !== tier) host.setAttribute('data-tier', tier);
      if (secs > 0 && secs < soonest) soonest = secs;
    }
    for (const node of document.querySelectorAll('[data-ago]')) {
      const at = Number(node.getAttribute('data-ago'));
      if (!Number.isFinite(at)) continue;
      const template = node.getAttribute('data-ago-template');
      const age = AD.format.ageText(now - at);
      const text = template ? fill(template, { age }) : age;
      if (node.textContent !== text) node.textContent = text;
    }
    return soonest;
  }

  function schedule(nowMs, soonest) {
    window.clearTimeout(tickTimer);
    const toMinute = 60000 - (nowMs % 60000) + 25;
    let wait;
    if (document.hidden) wait = toMinute;
    else if (soonest < SECOND_TICKS_UNDER) wait = 1000;
    else if (soonest < HALF_MINUTE_TICKS_UNDER) wait = 30000;
    else wait = toMinute;
    if (refresh && !document.hidden) {
      const due = refresh.asked + STILL_CHECKING_MS - nowMs;
      if (due > 0) wait = Math.min(wait, due + 25);
    }
    tickTimer = window.setTimeout(tick, wait);
  }

  function renderClockline(nowMs) {
    const line = byId('clockline');
    const text = clockText(nowMs);
    if (line.textContent !== text) line.textContent = text;
  }

  /** "Updated 2m ago", "Checking now…" or "Usage checks paused": whether the numbers are current. */
  function clockText(nowMs) {
    if (!current) return '';
    const accounts = current.accounts || [];
    const refreshing = accounts.some((account) => account.refreshing);
    const last = Math.max(...accounts.map((a) => a.last_good_at).filter(isNum));
    if (refresh && refreshing && nowMs - refresh.asked >= STILL_CHECKING_MS && Number.isFinite(last)) {
      return fill(STRINGS.shell.stillChecking, { age: AD.format.ago(last, nowMs) });
    }
    if (current.settings && current.settings.refresh_paused) return STRINGS.shell.paused;
    if (refreshing) return STRINGS.shell.checking;
    if (!Number.isFinite(last)) return '';
    if (nowMs / 1000 - last < 60) return STRINGS.shell.updatedNow;
    return fill(STRINGS.shell.updated, { age: AD.format.ago(last, nowMs) });
  }

  /** Say a reset entering its last 15 minutes, a reset passing and a refill, once each. The first
   * tick after boot only records what is on screen. */
  function announceChanges(state, nowMs) {
    const nextTiers = new Map();
    const nextRefilled = new Set();
    for (const row of AD.model.stripItems(state, nowMs)) {
      if (row.provider !== 'claude_cli') continue;          // only the accounts on this page are announced
      const key = `${row.account}|${row.window}`;
      const label = AD.dom.maskEmail(row.label);
      const windowName = String(row.window_label || '').toLowerCase();
      if (row.chip === 'refilled') {
        nextRefilled.add(key);
        if (refilled && !refilled.has(key)) AD.dom.announce(fill(STRINGS.alerts.ready, { label }));
        continue;
      }
      const tier = AD.model.tier(row.secs);
      nextTiers.set(key, tier);
      const before = tiers && tiers.get(key);
      if (!before || before === tier) continue;
      if (tier === 'imminent') {
        AD.dom.announce(fill(STRINGS.alerts.imminent, { label, window: windowName, countdown: AD.model.countdownText(row.secs) }));
      } else if (tier === 'resetting') {
        AD.dom.announce(fill(STRINGS.alerts.resetting, { label, window: windowName }));
      }
    }
    tiers = nextTiers;
    refilled = nextRefilled;
  }

  // ---- Refresh ----------------------------------------------------------------------------------

  function latestCheck(state) {
    return Math.max(0, ...((state && state.accounts) || []).map((a) => a.checked).filter(isNum));
  }

  function refreshAll() {
    if (refresh || !current) return;
    refresh = { asked: clockNow(), checked: latestCheck(current) };
    byId('refresh').classList.add('is-spinning');
    settle(clockNow());
    AD.api.action('refresh', {}).catch((err) => {
      refresh = null;
      byId('refresh').classList.remove('is-spinning');
      if (err.status !== 0) AD.dom.toast(err.message, { level: err.code === 'rate_limited' ? 'info' : 'critical' });
    });
  }

  /** Announce the end of a refresh the user asked for, once the readings are newer than the request. */
  function watchRefresh(state) {
    if (!refresh) return;
    const refreshing = (state.accounts || []).some((account) => account.refreshing);
    if (!refreshing && latestCheck(state) > refresh.checked) {
      refresh = null;
      AD.dom.announce(STRINGS.shell.refreshed);
    } else if (clockNow() - refresh.asked > REFRESH_WATCH_MS) {
      refresh = null;
    }
    byId('refresh').classList.toggle('is-spinning', refreshing || !!refresh);
  }

  // ---- Connection -------------------------------------------------------------------------------

  function setConnection(status) {
    connection = status;
    step('banners', () => AD.banners.render(current));
  }

  function readToken() {
    const match = /^#token=([^&]*)/.exec(window.location.hash || '');
    if (!match) return null;
    // The sign-in token must not stay in the address bar or the history.
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
    try {
      return decodeURIComponent(match[1]);
    } catch (err) {
      return match[1];
    }
  }

  // ---- Demo clock ------------------------------------------------------------------------------

  /** Advance the synthetic engine too, so demo usage and auto-switch follow the scrubber. */
  async function setDemoShift(ms) {
    if (!current || current.mode !== 'demo') return;
    demoOffset = isNum(ms) ? ms : 0;
    try {
      const reply = await AD.api.action('demo-clock', { seconds: demoOffset / 1000 });
      if (isNum(reply.now)) clockOffset = reply.now * 1000 - Date.now();
    } catch (error) {
      AD.dom.toast(error.message, { level: 'warning' });
    }
    tiers = null;
    refilled = null;
    if (current) render(current);
    else settle(clockNow());
  }

  // ---- Shell controls ------------------------------------------------------------------------------

  function hydrateIcons(root) {
    for (const slot of root.querySelectorAll('span[data-icon]')) {
      slot.replaceWith(AD.dom.svgIcon(slot.getAttribute('data-icon'), { className: slot.className || undefined }));
    }
  }

  function confirmQuit() {
    AD.settings.close();
    const cancel = AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal());
    const quit = AD.dom.button(STRINGS.button.quit, async () => {
      try {
        await AD.api.post('/api/stop', {});
      } catch (err) {
        AD.dom.toast(err.message, { level: 'critical' });
        return;
      }
      AD.api.stopPolling();
      window.clearTimeout(tickTimer);
      AD.dom.closeModal();
      setConnection('closed');
    }, { kind: 'primary', attrs: { 'data-autofocus': '' } });
    AD.dom.openModal({ title: STRINGS.shell.quitTitle, body: AD.dom.el('p', { text: STRINGS.shell.quitBody }),
      actions: [cancel, quit], view: 'quit', size: 'alert' });
  }

  function showShortcuts() {
    AD.settings.close();
    const list = AD.dom.el('dl', 'shortcut-list', STRINGS.shortcuts.map(([keys, what]) => AD.dom.el('div', 'shortcut',
      AD.dom.el('dt', { text: what }),
      AD.dom.el('dd', null, keys.map((key) => (key === '–' ? AD.dom.el('span', { 'aria-hidden': 'true', text: '–' })
        : AD.dom.el('kbd', { text: key })))))));
    AD.dom.openModal({ title: STRINGS.shell.shortcuts, body: list, view: 'shortcuts',
      actions: [AD.dom.button(STRINGS.button.done, () => AD.dom.closeModal(), { kind: 'primary', attrs: { 'data-autofocus': '' } })] });
  }

  /** The toolbar gains its material once content scrolls beneath it (the macOS scroll-edge effect). */
  function watchScrollEdge() {
    const sentinel = byId('scroll-sentinel');
    if (!sentinel || !('IntersectionObserver' in window)) {
      byId('toolbar').classList.add('is-scrolled');
      return;
    }
    new IntersectionObserver(([entry]) => {
      byId('toolbar').classList.toggle('is-scrolled', !entry.isIntersecting);
    }).observe(sentinel);
  }

  function bindShell() {
    byId('refresh').addEventListener('click', refreshAll);
    byId('open-settings').addEventListener('click', () => AD.settings.open(current));
    byId('open-help').addEventListener('click', showShortcuts);
    byId('quit').addEventListener('click', confirmQuit);
    document.addEventListener('ad:theme', () => {
      AD.dom.announce(fill(STRINGS.shell.themeChanged, { mode: STRINGS.theme[AD.theme.get()] }));
    });
    document.addEventListener('ad:hide-emails', () => {
      if (current) render(current);
    });
    document.addEventListener('visibilitychange', () => tick());
  }

  function bindKeys() {
    const openSettings = () => {
      if (AD.dom.modalView()) return;          // a sheet is up: ⌘, waits (the browser still never gets it)
      AD.settings.toggle(current);
    };
    const keys = {
      'Meta+r': () => refreshAll(),
      r: () => refreshAll(),
      'Meta+,': openSettings,
      ',': () => AD.settings.open(current),
      '?': () => showShortcuts(),
      t: () => AD.theme.cycle(),
    };
    for (let n = 1; n <= 9; n += 1) keys[String(n)] = () => AD.rows.focusIndex(n - 1);
    AD.dom.bindShortcuts(keys);
  }

  /** Start the page once: icons, controls, sign-in from the #token fragment, polling and the clock. */
  async function boot() {
    if (booted) return;
    booted = true;
    AD.theme.init();
    hydrateIcons(document);
    bindShell();
    bindKeys();
    watchScrollEdge();
    tick();
    const token = readToken();
    if (token !== null) {
      try {
        await AD.api.post('/api/session', { token });
      } catch (err) {
        setConnection(err.status === 0 ? 'offline' : 'signin');
      }
    }
    await AD.api.startPolling(render, setConnection);
  }

  AD.app = {
    get state() { return current; },
    get connection() { return connection; },
    render, tick, boot, clockNow, setDemoShift,
    demoShift: () => demoOffset,
    quit: confirmQuit,
    shortcuts: showShortcuts,
    // For the app window's own menu (⌘R, ⌘,): the native shell can call these.
    refresh: refreshAll,
    toggleSettings: () => AD.settings.toggle(current),
  };

  if (document.getElementById('main')) {
    boot().catch((err) => console.error('AccountDeck 2 could not start.', err));
  }
})();
