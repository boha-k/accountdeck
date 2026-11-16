/* AccountDeck 2 — "Activity & troubleshooting", one disclosure at the bottom of the accounts: the
 * last switch explained (once asked for), AccountDeck's own health, recent activity, the shared setup
 * and Diagnostics. Rebuilt only when its data changed; its open state survives through data-keep. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;
  const D = STRINGS.details;
  const H = STRINGS.health;

  const EVENTS_SHOWN = 20;
  const TIME_KEYS = new Set(['at', 'started', 'finished']);
  const LEVEL_ICONS = { fail: 'warning', warn: 'info', ok: 'check-circle' };
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);

  let last = null;
  let drawn = null;
  let manifest = null;                // {op, data, error} once showManifest ran

  function words(key) {
    const text = String(key).replace(/[_-]+/g, ' ');
    return text ? text[0].toUpperCase() + text.slice(1) : text;
  }

  function value(key, item, nowMs) {
    if (item === null || item === undefined) return el('span', { class: 'is-quiet', text: D.notRecorded });
    if (typeof item === 'boolean') return D[item ? 'yes' : 'no'];
    if (typeof item === 'number' && TIME_KEYS.has(key)) return AD.model.clock(item, nowMs);
    if (Array.isArray(item)) {
      if (!item.length) return el('span', { class: 'is-quiet', text: '—' });
      if (item.every((entry) => entry === null || typeof entry !== 'object')) return item.map((entry) => mask(entry)).join(', ');
      return el('ol', 'manifest-list', item.map((entry) => el('li', null, value(key, entry, nowMs))));
    }
    if (typeof item === 'object') return fields(item, nowMs);
    return mask(item);
  }

  function fields(object, nowMs) {
    return el('dl', 'manifest', Object.keys(object).map((key) => el('div', 'manifest-row',
      el('dt', { text: words(key) }), el('dd', null, value(key, object[key], nowMs)))));
  }

  function activity(state, nowMs) {
    const events = (state.events || []).slice(0, EVENTS_SHOWN);
    if (!events.length) return el('p', { class: 'is-quiet', text: D.noActivity });
    return el('ol', 'events', events.map((event) => el('li', 'event',
      el('time', { text: AD.model.clock(event.at, nowMs) }), el('span', { text: mask(event.message) }))));
  }

  function setup(state) {
    const info = state.setup || {};
    const groups = (info.groups || []).map((group) => {
      let status = D.notConfigured;
      if (group.error) status = D.review;
      else if ((group.servers || []).length) status = group.servers.join(', ');
      else if (group.present) status = D.preserved;
      return el('div', 'manifest-row', el('dt', { text: group.label }), el('dd', { text: status }));
    });
    return [info.message ? el('p', { text: info.message }) : null, groups.length ? el('dl', 'manifest', groups) : null,
      info.note ? el('p', { class: 'is-quiet', text: info.note }) : null];
  }

  // ---- Health ---------------------------------------------------------------------------------------

  /** What keeps AccountDeck from doing its job, [{id, text, action}]: its doctor's failing checks (state.health),
   * then what the service reports of itself (state.service) when no check says it already: a watchdog that
   * stopped restarting it, a database that fails its check. [] when all is well, or nothing is known yet. */
  function problems(state) {
    const list = [];
    const checks = state && state.health && Array.isArray(state.health.checks) ? state.health.checks : [];
    for (const check of checks) {
      if (check && check.level === 'fail') list.push({ id: String(check.id), text: check.text, action: check.action || null });
    }
    const service = (state && state.service) || {};
    if (service.watchdog && service.watchdog.state === 'braked') list.push({ id: 'watchdog', text: H.braked, action: H.brakedAction });
    const checked = service.db && service.db.quick_check;
    if (checked && checked.result === 'damaged' && !list.some((p) => p.id === 'db')) {
      list.push({ id: 'db', text: H.damaged, action: H.damagedAction });
    }
    return list;
  }

  function healthItem(level, text, action) {
    return el('li', { class: 'health-item', dataset: { level } }, AD.dom.svgIcon(LEVEL_ICONS[level] || 'info'),
      el('div', 'health-words', el('p', { text: mask(text) }), action ? el('p', { class: 'is-quiet', text: mask(action) }) : null));
  }

  /** The service itself: the watchdog, the database's last check and its backups, a restore at this start. */
  function serviceLine(state) {
    const service = state.service;
    if (!service || !service.watchdog) return null;
    const watchdog = service.watchdog;
    const db = service.db || {};
    const words = [fill(H.watchdog, { state: H.watchdogStates[watchdog.state] || watchdog.state })];
    if (watchdog.restarts_last_hour) {
      words.push(watchdog.restarts_last_hour === 1 ? H.restartsOne : fill(H.restarts, { n: watchdog.restarts_last_hour }));
    }
    const parts = [words.join(' · ')];
    if (db.quick_check && isNum(db.quick_check.at)) {
      parts.push(' · ', el('span', { dataset: { ago: db.quick_check.at, agoTemplate: H.dbChecked },
        text: fill(H.dbChecked, { age: AD.format.ago(db.quick_check.at, AD.app.clockNow()) }) }));
    }
    if (isNum(db.backups)) parts.push(` · ${db.backups ? (db.backups === 1 ? H.backupsOne : fill(H.backups, { n: db.backups })) : H.noBackup}`);
    return el('p', { class: 'is-quiet health-service' }, parts);
  }

  /** The health part: one quiet line when all is well; otherwise what fails and what is worth a look, each with
   * what to do. Every check stays one click away; a limit watch that is not running is said here only. */
  function healthPart(state, nowMs) {
    const block = state.health;
    const failing = problems(state);
    const checks = block && Array.isArray(block.checks) ? block.checks.filter(Boolean) : [];
    const notes = checks.filter((check) => check.level === 'warn');
    let tone = 'ok';
    let status = H.ok;
    if (!block) {
      tone = 'quiet';
      status = H.waiting;
    } else if (failing.length) {
      tone = 'fail';
      status = H.bad;
    } else if (notes.length) {
      tone = 'warn';
      status = notes.length === 1 ? H.okNote : fill(H.okNotes, { n: notes.length });
    }
    const line = el('p', { class: 'health-line', dataset: { tone } }, AD.dom.svgIcon(LEVEL_ICONS[tone] || 'clock'), el('span', { text: status }),
      block && isNum(block.at) ? el('span', { class: 'is-quiet', dataset: { ago: block.at, agoTemplate: ` · ${H.checked}` },
        text: ` · ${fill(H.checked, { age: AD.format.ago(block.at, nowMs) })}` }) : null);
    const items = failing.map((p) => healthItem('fail', p.text, p.action)).concat(notes.map((c) => healthItem('warn', c.text, c.action)));
    const restored = state.service && state.service.db && state.service.db.restored_from;
    const watch = ((state.providers || {}).claude_cli || {}).watch;
    return el('section', { class: 'details-part', id: 'details-health', tabindex: -1 },
      el('h3', { class: 'details-heading', text: H.title }), line,
      items.length ? el('ul', 'health-list', items) : null,
      isNum(restored) ? el('p', { class: 'is-quiet', text: fill(H.restored, { clock: AD.model.clock(restored, nowMs) }) }) : null,
      watch && watch.running === false ? el('p', { class: 'is-quiet', text: STRINGS.watch.off }) : null,
      serviceLine(state),
      checks.length ? el('details', { class: 'health-all', 'data-keep': 'health-all' }, el('summary', { text: fill(H.all, { n: checks.length }) }),
        el('ul', 'health-list', checks.map((check) => healthItem(check.level, check.text, check.level === 'ok' ? null : check.action)))) : null);
  }

  function explain(nowMs) {
    if (!manifest) return null;
    let body;
    if (manifest.error) body = el('p', { class: 'modal-error', role: 'alert', text: manifest.error });
    else if (!manifest.data) body = el('p', { class: 'is-quiet', text: D.loading });
    else body = fields(manifest.data, nowMs);
    return el('section', { class: 'details-part', id: 'details-explain', tabindex: -1 },
      el('h3', { class: 'details-heading', text: D.explain }), body);
  }

  /** Show the diagnostics log in a wide sheet. */
  async function diagnostics() {
    try {
      const data = await AD.api.get('/api/diagnostics');
      AD.dom.openModal({ title: D.diagnostics, view: 'diagnostics', size: 'wide',
        body: [el('p', { class: 'modal-hint', text: D.footer }), el('pre', { tabindex: 0, text: (data.lines || []).join('\n') })],
        actions: [AD.dom.button(STRINGS.button.done, () => AD.dom.closeModal(), { kind: 'primary', attrs: { 'data-autofocus': '' } })] });
    } catch (err) {
      AD.dom.toast(err.message, { level: 'critical' });
    }
  }

  function build(state, nowMs) {
    return el('details', { class: 'details-panel', 'data-keep': 'details-panel' },
      el('summary', { text: D.title }),
      el('div', 'details-body',
        explain(nowMs),
        healthPart(state, nowMs),
        el('section', 'details-part', el('h3', { class: 'details-heading', text: D.activity }), activity(state, nowMs)),
        el('section', 'details-part', el('h3', { class: 'details-heading', text: D.setup }), setup(state)),
        el('div', 'details-actions', AD.dom.button(D.diagnosticsButton, diagnostics, { small: true }))));
  }

  function draw(force) {
    const root = document.getElementById('details');
    if (!root || !last) return;
    const watch = ((last.providers || {}).claude_cli || {}).watch;
    const service = last.service || {};
    const watchdog = service.watchdog || {};
    const db = service.db || {};
    // Not the tick's age, which changes at every poll: what the part shows of the service.
    const sig = JSON.stringify([last.events, last.setup, manifest, AD.dom.hideEmails(), last.health, watch && watch.running,
      [watchdog.state, watchdog.restarts_last_hour, db.quick_check, db.backups, db.restored_from]]);
    if (!force && sig === drawn) return;
    drawn = sig;
    const next = build(last, AD.app.clockNow());
    if (root.firstElementChild) AD.dom.swap(root.firstElementChild, next);
    else root.appendChild(next);
  }

  /** A new state: activity and setup follow it. */
  function render(state) {
    if (!state) return;
    last = state;
    draw(false);
  }

  /** Explain a switch: load its manifest, open the panel and bring the explanation into view. */
  async function showManifest(op) {
    manifest = { op, data: null, error: null };
    draw(true);
    const reveal = () => {
      const panel = document.querySelector('#details .details-panel');
      if (panel) panel.open = true;
      const part = document.getElementById('details-explain');
      if (part) {
        part.scrollIntoView({ block: 'start', behavior: AD.theme.reducedMotion() ? 'auto' : 'smooth' });
        part.focus({ preventScroll: true });
      }
    };
    reveal();
    try {
      // Without an operation id the service answers with the latest manifest.
      const data = await AD.api.get(op ? `/api/manifest?op=${encodeURIComponent(op)}` : '/api/manifest');
      manifest = { op, data: data || {}, error: null };
    } catch (err) {
      manifest = { op, data: null, error: err.message };
    }
    draw(true);
    reveal();
  }

  /** Open the panel at AccountDeck's health (from the health banner). */
  function showHealth() {
    draw(true);
    const panel = document.querySelector('#details .details-panel');
    if (panel) panel.open = true;
    const part = document.getElementById('details-health');
    if (part) {
      part.scrollIntoView({ block: 'start', behavior: AD.theme.reducedMotion() ? 'auto' : 'smooth' });
      part.focus({ preventScroll: true });
    }
  }

  AD.details = { render, showManifest, showHealth, diagnostics, problems };
})();
