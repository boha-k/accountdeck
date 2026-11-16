/* AccountDeck 2 — the Settings window, laid out like System Settings: a sidebar of panes and, for
 * each pane, grouped rows with the label on the left and the control (a switch, a segmented
 * control, a pop-up button, a stepper) on the right.
 * Panes: General, Auto-Switch, Notifications, Menu Bar & Login, Advanced (usage checks, Claude limit resets), About
 * (with the update check: the update_check switch, what the last check found and Check Now; update.js has the words).
 * Service settings go through /api/settings; the appearance, Hide emails and browser notifications belong to this
 * browser. The open pane is rebuilt only when what it shows changed, and data-keep carries focus.
 * Keyboard: ↑ ↓ (or ← →) move between panes in the sidebar, Esc or ⌘. closes, ⌘, toggles. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;
  const A = AD.accounts;
  const S = STRINGS.settings;

  const THEME_ICONS = { system: 'circle-half', light: 'sun', dark: 'moon' };
  const ALIASES = { 'cli-settings': 'autoswitch' };
  const STEP_DELAY_MS = 450;
  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);

  let current = null;
  let pane = 'general';
  let drawn = null;
  let tabsDrawn = null;
  let trigger = null;
  let stepTimer = null;

  const PANES = [
    { id: 'general', icon: 'gear', tone: 'graphite', build: general },
    { id: 'autoswitch', icon: 'bolt', tone: 'orange', build: autoswitch, available: (state) => !!A.provider(state) },
    { id: 'notifications', icon: 'bell', tone: 'red', build: notifications },
    { id: 'menubar', icon: 'menubar', tone: 'blue', build: menubar,
      available: (state) => (AD.format.feature('menu_bar') && 'tray' in ((state && state.settings) || {}))
        || AD.format.feature('start_on_login') },
    { id: 'advanced', icon: 'sliders', tone: 'graphite', build: advanced },
    { id: 'about', icon: 'info', tone: 'graphite', build: about },
  ];

  function dialog() {
    return document.getElementById('settings');
  }

  function panes() {
    return PANES.filter((p) => !p.available || p.available(current));
  }

  function title(id) {
    return id === 'menubar' ? AD.format.label('section') : S.tabs[id];
  }

  // ---- Form building blocks ---------------------------------------------------------------------

  function patch(values) {
    return AD.api.settings(values).catch((err) => {
      AD.dom.toast(err.message, { level: 'critical' });
      draw(true);
    });
  }

  function group(heading, rows, footnote) {
    return el('section', 'form-group',
      heading ? el('h4', { class: 'form-group-title', text: heading }) : null,
      el('div', 'form-box', rows),
      footnote ? el('p', { class: 'form-footnote', text: footnote }) : null);
  }

  /** A row: the label (a <label> when `forId` names the control) and hint on the left, the control on the right. */
  function row(label, hint, control, options = {}) {
    const hintId = hint && options.forId ? `${options.forId}-hint` : null;
    if (hintId && control && control.querySelector) {
      const target = control.id === options.forId ? control : control.querySelector(`#${options.forId}`);
      if (target) target.setAttribute('aria-describedby', hintId);
    }
    return el('div', { class: ['form-row', options.stacked && 'is-stacked'] },
      el('div', 'form-row-text',
        options.forId ? el('label', { class: 'form-label', for: options.forId, text: label })
          : el('p', { class: 'form-label', id: options.labelId, text: label }),
        hint ? el('p', { class: 'form-hint', id: hintId, text: hint }) : null),
      control ? el('div', 'form-control', control) : null);
  }

  function switchRow(id, label, hint, checked, onChange, options = {}) {
    const input = el('input', { type: 'checkbox', class: 'switch', role: 'switch', id, checked: !!checked,
      disabled: options.disabled, 'data-keep': id });
    input.addEventListener('change', () => onChange(input.checked));
    return row(label, hint, input, { forId: id });
  }

  function serverSwitch(settings, key, label, hint) {
    return switchRow(`setting-${key}`, label, hint, settings[key] === true, (on) => patch({ [key]: on }));
  }

  function buttonRow(label, hint, text, run, key) {
    return row(label, hint, AD.dom.button(text, run, { attrs: { 'data-keep': key } }));
  }

  // ---- General ------------------------------------------------------------------------------------

  function appearance() {
    const mode = AD.theme.get();
    return el('div', { class: 'segmented', role: 'radiogroup', 'aria-labelledby': 'setting-theme-label' },
      ['system', 'light', 'dark'].map((value) => el('button', { type: 'button', role: 'radio', 'data-theme-value': value,
        'data-keep': `setting-theme-${value}`, 'aria-checked': String(mode === value), tabindex: mode === value ? 0 : -1 },
      AD.dom.svgIcon(THEME_ICONS[value]), STRINGS.theme[value])));
  }

  function usageDisplay(settings) {
    const display = settings.usage_display === 'left' ? 'left' : 'used';
    const option = (value) => el('button', { type: 'button', 'aria-pressed': String(display === value),
      'data-keep': `setting-display-${value}`, on: { click: () => { if (display !== value) patch({ usage_display: value }); } } },
    S[value]);
    return el('div', { class: 'segmented', role: 'group', 'aria-labelledby': 'setting-display-label' }, option('used'), option('left'));
  }

  /** Whether General shows the Antigravity group: while the dashboard shows its section and the service has the setting. */
  function antigravityShown(state) {
    return !!AD.antigravity && AD.antigravity.shown(state) && 'agy_usage_on_open' in ((state && state.settings) || {});
  }

  function general(state) {
    const settings = state.settings || {};
    const G = STRINGS.antigravity;
    return [
      group(null, [
        row(S.appearance, null, appearance(), { labelId: 'setting-theme-label' }),
        row(S.numbers, null, usageDisplay(settings), { labelId: 'setting-display-label' }),
      ]),
      group(S.privacy, [
        switchRow('setting-hide-emails', S.hideEmails, S.hideEmailsHint, AD.dom.hideEmails(), (on) => AD.dom.setHideEmails(on)),
        serverSwitch(settings, 'confirm_when_idle', S.confirmIdle, S.confirmIdleHint),
      ]),
      antigravityShown(state) ? group(G.settings, [serverSwitch(settings, 'agy_usage_on_open', G.usageOnOpen, G.usageOnOpenHint)]) : null,
    ];
  }

  // ---- Auto-Switch --------------------------------------------------------------------------------

  function orderList(state, mode) {
    const prefs = state.settings || {};
    const auto = A.autoswitch(state);
    const previews = auto.previews || {};
    const rows = new Map();
    for (const list of Object.values(previews)) {
      for (const item of list || []) rows.set(item.id, item);
    }
    const automatic = (previews.soonest_reset || []).map((r) => r.id);
    const saved = prefs.cli_manual_order || [];
    const manual = mode === 'manual';
    const ids = manual ? [...saved.filter((id) => automatic.includes(id)), ...automatic.filter((id) => !saved.includes(id))]
      : (previews[mode] || []).map((r) => r.id);
    const label = (id) => {
      const account = (state.accounts || []).find((a) => a.id === id);
      return mask((account && account.label) || (rows.get(id) || {}).label || id);
    };
    if (!ids.length) return el('p', { class: 'form-hint', dataset: { orderPreview: mode }, text: S.orderEmpty });
    const move = (index, delta) => {
      const next = ids.slice();
      [next[index], next[index + delta]] = [next[index + delta], next[index]];
      patch({ cli_manual_order: next, cli_order: 'manual' });
    };
    return el('ol', { class: ['order-list', manual && 'cli-manual-order'], dataset: { orderPreview: mode } }, ids.map((id, index) => {
      const skip = (rows.get(id) || {}).skip;
      return el('li', { dataset: { account: id } }, el('span', { class: 'order-name', text: label(id) }),
        skip ? el('span', { class: 'order-skip', text: mask(skip), title: mask(skip) }) : el('span', { class: 'order-skip' }),
        manual ? el('span', 'order-moves',
          AD.dom.button('', () => move(index, -1), { kind: 'icon', icon: 'chevron-up', disabled: index === 0,
            label: fill(S.moveUp, { label: label(id) }), attrs: { 'data-keep': `order-up-${id}` } }),
          AD.dom.button('', () => move(index, 1), { kind: 'icon', icon: 'chevron', disabled: index === ids.length - 1,
            label: fill(S.moveDown, { label: label(id) }), attrs: { 'data-keep': `order-down-${id}` } }))
          : null);
    }));
  }

  function thresholdStepper(value) {
    const input = el('input', { id: 'setting-cli_threshold', type: 'number', min: 50, max: 99, step: 1, value,
      inputmode: 'numeric', 'data-keep': 'setting-cli-threshold' });
    const send = () => {
      const next = Math.round(Number(input.value));
      if (!Number.isFinite(next)) {
        input.value = String(value);
        return;
      }
      const clamped = Math.min(99, Math.max(50, next));
      input.value = String(clamped);
      if (clamped !== value) patch({ cli_threshold: clamped });
    };
    const nudge = (delta) => {
      input.value = String(Math.min(99, Math.max(50, Math.round(Number(input.value) || value) + delta)));
      window.clearTimeout(stepTimer);
      stepTimer = window.setTimeout(send, STEP_DELAY_MS);
    };
    input.addEventListener('change', send);
    return el('span', 'stepper', input,
      el('span', 'stepper-buttons',
        el('button', { type: 'button', tabindex: -1, 'aria-label': S.thresholdUp, on: { click: () => nudge(1) } }, AD.dom.svgIcon('chevron-up')),
        el('button', { type: 'button', tabindex: -1, 'aria-label': S.thresholdDown, on: { click: () => nudge(-1) } }, AD.dom.svgIcon('chevron'))),
      el('span', { class: 'stepper-unit', 'aria-hidden': 'true', text: '%' }));
  }

  function autoswitch(state) {
    const prefs = state.settings || {};
    const auto = A.autoswitch(state);
    const mode = S.orders[prefs.cli_order] ? prefs.cli_order : 'soonest_reset';
    const automatic = ((auto.previews || {}).soonest_reset || []).map((r) => r.id);
    const select = el('select', { id: 'setting-cli_order', 'data-keep': 'setting-cli-order' },
      Object.entries(S.orders).map(([value, text]) => el('option', { value, text, selected: value === mode })));
    select.addEventListener('change', () => patch({ cli_order: select.value,
      ...(select.value === 'manual' && !(prefs.cli_manual_order || []).length ? { cli_manual_order: automatic } : {}) }));
    const threshold = Number.isFinite(prefs.cli_threshold) ? prefs.cli_threshold : 95;
    // The 5-hour limit only while an account reports one (Claude has reported none since 2026-10-31).
    const hint = A.showsSession(A.terminals(state)) ? S.thresholdHint : S.thresholdHintWeekly;
    return [
      group(null, [
        serverSwitch(prefs, 'cli_autoswitch', S.auto, S.autoHint),
        row(S.threshold, hint, thresholdStepper(threshold), { forId: 'setting-cli_threshold' }),
        serverSwitch(prefs, 'cli_early_switch', S.early, S.earlyHint),
      ]),
      group(S.order, [
        row(S.orderBy, null, el('span', 'popup', select, AD.dom.svgIcon('chevrons', { className: 'popup-chevrons' })), { forId: 'setting-cli_order' }),
        el('div', { class: 'form-row is-stacked' },
          el('p', { class: 'form-hint', text: mode === 'manual' ? S.orderManual : S.orderNow }),
          orderList(state, mode),
          mode === 'manual' ? el('div', null, AD.dom.button(S.orderReset, () => patch({ cli_manual_order: automatic }),
            { small: true, attrs: { 'data-keep': 'setting-order-reset' } })) : null),
      ], STRINGS.accounts.tip),
    ];
  }

  // ---- Notifications ------------------------------------------------------------------------------

  function browserNotifications() {
    const prefs = AD.alerts.preferences();
    let status;
    let control = null;
    if (prefs.permission === 'unsupported') {
      status = S.browserUnsupported;
    } else if (prefs.permission === 'denied') {
      status = S.browserBlocked;
    } else if (prefs.enabled && prefs.permission === 'granted') {
      status = S.browserOn;
      control = AD.dom.button(S.turnOff, () => {
        AD.alerts.setPreferences({ enabled: false });
        draw(true);
      }, { attrs: { 'data-keep': 'setting-notify' } });
    } else {
      status = S.browserOff;
      // The permission prompt must start inside this click.
      control = AD.dom.button(S.enable, async () => {
        await AD.alerts.requestPermission();
        draw(true);
      }, { attrs: { 'data-keep': 'setting-notify' } });
    }
    return row(S.browser, status, control);
  }

  async function testNotification() {
    try {
      await AD.api.action('test-notification', {});
      AD.dom.toast(S.testSent, { level: 'good' });
    } catch (err) {
      AD.dom.toast(err.message, { level: 'critical' });
    }
  }

  /** A calm hint, only when macOS blocks AccountDeck 2's notifications. Turning them on happens in the
   * menu bar item (it asks macOS) or System Settings, so the row only says where. */
  function macNotifications() {
    const state = AD.format.notificationState();
    return state === 'denied' || state === 'rejected' ? row(S.mac, S.macOff, null) : null;
  }

  function notifications(state) {
    const settings = state.settings || {};
    return [
      group(null, [
        serverSwitch(settings, 'alerts_enabled', S.alertsOn, S.alertsOnHint),
        // The app's own window has no browser notifications: Mac notifications come from the menu bar item.
        AD.format.appWindow() ? null : browserNotifications(),
      ]),
      group(null, [macNotifications(), buttonRow(S.test, S.testHint, S.testButton, testNotification, 'setting-test-notification')]),
    ];
  }

  // ---- Menu bar & login ---------------------------------------------------------------------------

  function menubar(state) {
    const settings = state.settings || {};
    const rows = [];
    if (AD.format.feature('menu_bar') && 'tray' in settings) {
      rows.push(serverSwitch(settings, 'tray', AD.format.label('status_icon'), AD.format.label('status_icon_hint')));
    }
    if (AD.format.feature('start_on_login')) rows.push(serverSwitch(settings, 'start_on_login', AD.format.label('start_on_login'), S.loginHint));
    return [group(null, rows)];
  }

  // ---- Advanced and About -------------------------------------------------------------------------

  function advanced(state) {
    const settings = state.settings || {};
    return [
      group(null, [
        serverSwitch(settings, 'refresh_paused', S.pause, S.pauseHint),
        // Only while the service has the setting: an older one would ignore it.
        'claude_resets' in settings ? serverSwitch(settings, 'claude_resets', STRINGS.resets.setting, STRINGS.resets.settingHint) : null,
      ]),
      group(null, [
        buttonRow(S.importPC, S.importPCHint, STRINGS.button.importPC, () => AD.move.open(current), 'setting-import'),
        buttonRow(S.shortcuts, null, S.shortcutsButton, () => {
          close();
          AD.app.shortcuts();
        }, 'setting-shortcuts'),
        buttonRow(S.diagnostics, S.diagnosticsHint, S.diagnosticsButton, () => {
          close();
          AD.details.diagnostics();
        }, 'setting-diagnostics'),
      ]),
    ];
  }

  /** One line of what the update check found, with Check Now (and Update… while a newer version is offered). */
  function updateStatus(state) {
    const U = STRINGS.update;
    const words = AD.update.status(state.update);
    const id = 'setting-update-status';
    // "Checked 3h ago." keeps itself current: app.js patches every [data-ago] on its clock.
    const age = words.ago !== null && words.agoText ? el('span', { 'data-ago': words.ago, 'data-ago-template': words.agoText,
      text: fill(words.agoText, { age: AD.format.ago(words.ago, AD.app.clockNow()) }) }) : null;
    const hint = words.hint || age
      ? el('p', { class: 'form-hint', id: `${id}-hint` }, words.hint, words.hint && age ? ' ' : null, age) : null;
    const offered = AD.update.offered(state.update);
    const blocked = offered && typeof offered.blocked === 'string' && offered.blocked ? offered.blocked : null;
    const controls = [
      offered ? AD.dom.button(U.update, () => AD.update.confirm(offered), { disabled: !!blocked, title: blocked || undefined,
        attrs: { 'data-keep': 'setting-update-start' } }) : null,
      AD.dom.button(U.checkNow, () => AD.update.checkNow(() => draw(true)), {
        disabled: !!words.off, title: words.off || undefined, className: words.checking ? 'is-busy' : undefined,
        attrs: { 'data-keep': 'setting-update-check-now', 'aria-busy': words.checking ? 'true' : undefined,
          'aria-describedby': `${id}-label${hint ? ` ${id}-hint` : ''}` } }),
    ];
    return el('div', { class: 'form-row', dataset: { update: (state.update && state.update.status) || 'none' } },
      el('div', 'form-row-text', el('p', { class: 'form-label', id: `${id}-label`, text: words.label }), hint),
      el('div', 'form-control', controls));
  }

  function about(state) {
    const version = state.version ? fill(S.aboutVersion, { version: state.version }) : '';
    const U = STRINGS.update;
    return [
      el('div', 'about', el('img', { src: '/icon.svg', alt: '', width: 72, height: 72 }),
        el('p', { class: 'about-name', text: STRINGS.app }), el('p', { class: 'about-version', text: version })),
      group(U.group, [
        switchRow('setting-update_check', U.setting, U.settingHint, (state.settings || {}).update_check === true,
          (on) => patch({ update_check: on })),
        updateStatus(state),
      ]),
      group(null, [row(STRINGS.help.logout, null, null)]),
      el('div', null, AD.dom.button(S.quit, () => {
        close();
        AD.app.quit();
      }, { kind: 'danger', attrs: { 'data-keep': 'setting-quit' } })),
    ];
  }

  // ---- Drawing ------------------------------------------------------------------------------------

  function drawTabs() {
    const list = panes();
    const sig = JSON.stringify([list.map((p) => p.id), pane, title('menubar')]);
    if (sig === tabsDrawn) return;
    tabsDrawn = sig;
    const host = document.getElementById('settings-tabs');
    const focused = host.contains(document.activeElement);
    host.replaceChildren(...list.map((p) => el('button', {
      type: 'button', role: 'tab', class: 'settings-tab', id: `settings-tab-${p.id}`, 'aria-selected': String(p.id === pane),
      'aria-controls': 'settings-body', tabindex: p.id === pane ? 0 : -1, dataset: { pane: p.id },
    }, el('span', { class: 'tab-icon', dataset: { tone: p.tone } }, AD.dom.svgIcon(p.icon)), el('span', { text: title(p.id) }))));
    if (focused) document.getElementById(`settings-tab-${pane}`).focus();
  }

  function signature() {
    const state = current || {};
    return JSON.stringify([pane, state.settings, A.provider(state), state.version, state.platform, AD.dom.hideEmails(),
      AD.alerts.preferences(), A.terminals(state).map((a) => [a.id, a.label]), A.showsSession(A.terminals(state)),
      state.update, AD.update.asking(), antigravityShown(state)]);
  }

  function draw(force) {
    const body = document.getElementById('settings-body');
    if (!body || !current) return;
    if (!panes().some((p) => p.id === pane)) pane = 'general';
    drawTabs();
    const sig = signature();
    if (!force && sig === drawn) return;
    drawn = sig;
    const spec = PANES.find((p) => p.id === pane);
    document.getElementById('settings-pane-title').textContent = title(pane);
    body.setAttribute('aria-labelledby', `settings-tab-${pane}`);
    const next = el('div', { class: 'settings-pane', dataset: { pane } }, spec.build(current));
    if (body.firstElementChild && body.firstElementChild.getAttribute('data-pane') === pane) AD.dom.swap(body.firstElementChild, next);
    else {
      body.replaceChildren(next);
      body.scrollTop = 0;
    }
  }

  function select(id, focusTab) {
    if (id === pane) return;
    pane = id;
    draw(true);
    if (focusTab) document.getElementById(`settings-tab-${id}`).focus();
  }

  function onTabKeys(event) {
    const tab = event.target.closest('[role="tab"]');
    if (!tab) return;
    const tabs = Array.from(document.querySelectorAll('#settings-tabs [role="tab"]'));
    const index = tabs.indexOf(tab);
    let next = null;
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length];
    else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') next = tabs[(index - 1 + tabs.length) % tabs.length];
    else if (event.key === 'Home') next = tabs[0];
    else if (event.key === 'End') next = tabs[tabs.length - 1];
    if (!next) return;
    event.preventDefault();
    select(next.getAttribute('data-pane'), true);
  }

  function bind(node) {
    if (node.getAttribute('data-bound')) return;
    node.setAttribute('data-bound', 'true');
    document.getElementById('settings-close').addEventListener('click', () => close());
    const tabs = document.getElementById('settings-tabs');
    tabs.addEventListener('click', (event) => {
      const tab = event.target.closest('[role="tab"]');
      if (tab) select(tab.getAttribute('data-pane'), true);
    });
    tabs.addEventListener('keydown', onTabKeys);
    node.addEventListener('keydown', (event) => {
      if (event.metaKey && event.key === '.') {
        event.preventDefault();
        close();
      }
    });
    node.addEventListener('close', () => {
      window.clearTimeout(stepTimer);
      if (trigger && trigger.isConnected && typeof trigger.focus === 'function') trigger.focus();
      trigger = null;
    });
    AD.dom.dismissOnBackdrop(node, () => close());
  }

  /** Open Settings (the toolbar button, ⌘, or ","); `paneId` opens one pane and focuses its first control. */
  function open(state, paneId) {
    const node = dialog();
    if (!node) return;
    if (state) current = state;
    const wanted = ALIASES[paneId] || paneId;
    if (wanted && PANES.some((p) => p.id === wanted)) pane = wanted;
    if (!node.open) {
      bind(node);
      trigger = document.activeElement;
      tabsDrawn = null;
      draw(true);
      node.showModal();
    } else {
      draw(true);
    }
    const first = wanted && document.querySelector('#settings-body .switch, #settings-body button, #settings-body input, #settings-body select');
    (first || document.getElementById(`settings-tab-${pane}`)).focus();
  }

  function close() {
    const node = dialog();
    if (node && node.open) node.close();
  }

  function isOpen() {
    const node = dialog();
    return !!(node && node.open);
  }

  /** ⌘, — open Settings, or close it when it is already open. */
  function toggle(state) {
    if (isOpen()) close();
    else open(state);
  }

  /** A new state: the open window follows the service's settings. */
  function render(state) {
    current = state;
    if (isOpen()) draw(false);
  }

  AD.settings = { open, close, toggle, isOpen, render };
})();
