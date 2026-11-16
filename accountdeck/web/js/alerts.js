/* AccountDeck 2 — tab title, favicon and alert delivery in the page.
 * Title and favicon come from AD.model (titleSignal, faviconSvg) at every render and clock tick,
 * computed over the terminal accounts this page shows (the incident signal still covers every
 * provider).
 * Alerts are the state's `alerts` list; this page delivers each new one at most once, with the same
 * policy as notify.py's page sink: alerts already listed when the page opened are not news, nothing
 * older than 5 minutes is delivered, `alerts_enabled: false` drops them and quiet hours all but critical
 * ones, toasts come at most once a minute (alerts arriving meanwhile are merged: "… (+2 more)") and wait
 * while the tab is hidden. A browser Notification additionally needs the user's opt-in click, the permission, an
 * enabled kind and a page the user is not looking at. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;

  const KEY = 'ad2.notify';
  // Kinds a browser notification carries unless the user changes them.
  // `service` is AccountDeck about itself (a restart, a restored database), never about one account.
  const DEFAULT_KINDS = Object.freeze({
    reset_confirmed: true, limit_reached: true, credential_expired: true, switch_result: true, service: true,
    low: false, use_it: false, reset_soon: false, suggest_switch: false,
  });
  const TOAST_GAP_MS = 60000;
  const NOTIFY_GAP_MS = 20000;
  const EXPIRE_S = 300;
  const LEVELS = ['info', 'good', 'warning', 'critical'];

  let prefs = loadPrefs();
  let favicon = null;
  const views = new WeakMap();                 // state → the same state with only terminal accounts
  let seen = null;                             // alert ids already handled (null until the first state)
  const queue = { toast: [], notify: [] };
  const lastAt = { toast: -Infinity, notify: -Infinity };

  // ---- Preferences (client-side only, localStorage["ad2.notify"]) --------------------------------

  function loadPrefs() {
    const result = { enabled: false, kinds: Object.assign({}, DEFAULT_KINDS) };
    try {
      const stored = JSON.parse(window.localStorage.getItem(KEY) || 'null');
      if (stored && typeof stored === 'object') {
        result.enabled = stored.enabled === true;
        for (const kind of Object.keys(DEFAULT_KINDS)) {
          if (stored.kinds && typeof stored.kinds[kind] === 'boolean') result.kinds[kind] = stored.kinds[kind];
        }
      }
    } catch (err) {
      // Unreadable or blocked storage: the defaults apply.
    }
    return result;
  }

  function permission() {
    return 'Notification' in window ? window.Notification.permission : 'unsupported';
  }

  /** {enabled, kinds: {kind: bool}, permission: 'default' | 'granted' | 'denied' | 'unsupported'}. */
  function preferences() {
    return { enabled: prefs.enabled, kinds: Object.assign({}, prefs.kinds), permission: permission() };
  }

  /** Change the browser-notification opt-in or kinds ({enabled?, kinds?: {kind: bool}}) for this browser. */
  function setPreferences(patch) {
    if (patch && typeof patch.enabled === 'boolean') prefs.enabled = patch.enabled;
    if (patch && patch.kinds) {
      for (const kind of Object.keys(DEFAULT_KINDS)) {
        if (typeof patch.kinds[kind] === 'boolean') prefs.kinds[kind] = patch.kinds[kind];
      }
    }
    try {
      window.localStorage.setItem(KEY, JSON.stringify(prefs));
    } catch (err) {
      // Storage can be blocked; the choice then lasts for this page only.
    }
    return preferences();
  }

  /** Ask for notification permission. Must run inside the user's click (browsers ignore it otherwise). */
  async function requestPermission() {
    if (!('Notification' in window)) {
      AD.dom.toast(STRINGS.alerts.unsupported, { level: 'warning', sticky: false });
      return 'unsupported';
    }
    let result = window.Notification.permission;
    if (result === 'default') {
      try {
        result = await window.Notification.requestPermission();
      } catch (err) {
        result = window.Notification.permission;
      }
    }
    if (result === 'granted') {
      setPreferences({ enabled: true });
      AD.dom.toast(STRINGS.alerts.enabled, { level: 'good' });
    } else {
      setPreferences({ enabled: false });
      AD.dom.toast(STRINGS.alerts.blocked, { level: 'warning', sticky: false });
    }
    return result;
  }

  // ---- Title and favicon ------------------------------------------------------------------------

  function terminalView(state) {
    let view = views.get(state);
    if (!view) {
      view = Object.assign({}, state, { accounts: AD.accounts.terminals(state) });
      views.set(state, view);
    }
    return view;
  }

  function updateTitle(state, nowMs) {
    const signal = AD.model.titleSignal(terminalView(state), nowMs);
    let title = signal.title;
    // A hidden tab ticks once a minute, so its title never promises seconds.
    if (signal.kind === 'reset' && document.hidden && signal.secs < 300) {
      title = `${Math.max(1, Math.ceil(signal.secs / 60))}m · ${STRINGS.app}`;
    }
    if (document.title !== title) document.title = title;
  }

  function updateFavicon(state, nowMs) {
    const svg = AD.model.faviconSvg(terminalView(state), nowMs);
    if (svg === favicon) return;
    favicon = svg;
    const link = document.getElementById('favicon');
    if (link) link.setAttribute('href', `data:image/svg+xml,${encodeURIComponent(svg)}`);
  }

  // ---- Delivery -------------------------------------------------------------------------------------

  function inQuietHours(hours, hour) {
    if (!Array.isArray(hours) || hours.length !== 2) return false;
    const [start, end] = hours;
    if (start === end) return false;
    return start < end ? hour >= start && hour < end : hour >= start || hour < end;
  }

  function current(alert, byId, now) {
    const live = byId.get(alert.id);
    return !!live && !live.dismissed && !(typeof live.snoozed_until === 'number' && live.snoozed_until > now)
      && now - live.at <= EXPIRE_S;
  }

  function collect(state) {
    const alerts = Array.isArray(state.alerts) ? state.alerts : [];
    if (!seen) {
      seen = new Set(alerts.map((a) => a.id));
      return;
    }
    const present = new Set();
    for (const alert of alerts) {
      present.add(alert.id);
      if (seen.has(alert.id)) continue;
      seen.add(alert.id);
      queue.toast.push(alert);
      if (prefs.kinds[alert.kind]) queue.notify.push(alert);
    }
    for (const id of Array.from(seen)) {
      if (!present.has(id)) seen.delete(id);
    }
  }

  /** One message for everything waiting: the newest alert's text, "(+n more)" for the rest. */
  function merge(alerts) {
    const sorted = alerts.slice().sort((a, b) => b.at - a.at);
    const newest = sorted[0];
    const message = sorted.length > 1 ? fill(STRINGS.alerts.more, { message: newest.message, n: sorted.length - 1 }) : newest.message;
    const level = sorted.reduce((top, a) => (LEVELS.indexOf(a.level) > LEVELS.indexOf(top) ? a.level : top), 'info');
    return { title: newest.title, message, level, tag: [newest.kind, newest.account, newest.window].join('|') };
  }

  function notify(item) {
    try {
      const note = new window.Notification(AD.dom.maskEmail(item.title), {
        body: AD.dom.maskEmail(item.message), tag: item.tag, icon: '/icon.svg',
      });
      note.onclick = () => {
        window.focus();
        note.close();
      };
    } catch (err) {
      console.error('AccountDeck 2: the notification could not be shown.', err);
    }
  }

  function deliver(state, nowMs) {
    const now = nowMs / 1000;
    const settings = state.settings || {};
    const byId = new Map((Array.isArray(state.alerts) ? state.alerts : []).map((a) => [a.id, a]));
    queue.toast = queue.toast.filter((a) => current(a, byId, now));
    queue.notify = queue.notify.filter((a) => current(a, byId, now));
    if (settings.alerts_enabled === false) {
      queue.toast = [];
      queue.notify = [];
      return;
    }
    if (inQuietHours(settings.quiet_hours, new Date(nowMs).getHours())) {
      // A critical alert is let through, as notify.py does: money being spent, a stuck service, the terminals stopped.
      queue.toast = queue.toast.filter((a) => a.level === 'critical');
      queue.notify = queue.notify.filter((a) => a.level === 'critical');
    }
    const wall = Date.now();
    const looking = !document.hidden && document.hasFocus();
    if (queue.notify.length) {
      if (looking || !prefs.enabled || permission() !== 'granted') {
        queue.notify = [];
      } else if (wall - lastAt.notify >= NOTIFY_GAP_MS) {
        notify(merge(queue.notify));
        queue.notify = [];
        lastAt.notify = wall;
      }
    }
    if (queue.toast.length && !document.hidden && wall - lastAt.toast >= TOAST_GAP_MS) {
      const item = merge(queue.toast);
      AD.dom.toast(AD.dom.maskEmail(item.message), { level: item.level, sticky: false, title: AD.dom.maskEmail(item.title) });
      queue.toast = [];
      lastAt.toast = wall;
    }
  }

  /** A new state: title, favicon, then any alerts it brings. */
  function render(state, nowMs) {
    if (!state) return;
    updateTitle(state, nowMs);
    updateFavicon(state, nowMs);
    collect(state);
    deliver(state, nowMs);
  }

  /** A clock tick: title and favicon follow the time; waiting alerts go out once their gap has passed. */
  function tick(state, nowMs) {
    if (!state) return;
    updateTitle(state, nowMs);
    updateFavicon(state, nowMs);
    deliver(state, nowMs);
  }

  AD.alerts = { render, requestPermission, tick, preferences, setPreferences };
})();
