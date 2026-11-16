/* AccountDeck 2 — derived model.
 * Pure functions over the /api/state payload: no DOM, no timers and no clock reads; callers pass
 * nowMs (AD.app.clockNow()). The countdown, tier, clock, window and strip rules mirror
 * accountdeck/contract.py, so the page and the service never disagree about a reading. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};

  const HIDDEN = new Set(['not_started', 'resetting', 'expected_refill']);
  const REFILL_KEEP = 3600;          // a confirmed refill is shown for 60 min
  const HORIZON = 86400;             // the strip looks 24 h ahead
  const MIN_USED = 5;                // strip rows under 5 % used are dropped unless the account is out
  const IMMINENT = 900;
  const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const APP = 'AccountDeck 2';

  const buckets = Object.freeze([
    Object.freeze({ id: 'refilled', label: 'Refilled', max: null }),
    Object.freeze({ id: 'minute', label: 'Any minute', max: 900 }),
    Object.freeze({ id: 'hour', label: 'Within the hour', max: 3600 }),
    Object.freeze({ id: 'six_hours', label: 'Next 6 hours', max: 21600 }),
    Object.freeze({ id: 'day', label: 'Next 24 hours', max: 86400 }),
  ]);

  // Favicon colours: the meter fills and labels of tokens.css (accent, orange, red, label-3, label).
  const FAVICON = {
    light: { plenty: '#0071e3', watch: '#c96400', alarm: '#e5321f', muted: '#8e8e93', text: '#1d1d1f' },
    dark: { plenty: '#2a93ff', watch: '#ff9f0a', alarm: '#ff5c52', muted: '#8e8e93', text: '#f5f5f7' },
  };
  const TONE = { plenty: 'plenty', watch: 'watch', low: 'alarm', critical: 'alarm', out: 'alarm', unknown: 'muted' };
  // `blocked` of a window Claude reports locked (rules.locked): no limit used up, so it never reads "back in …".
  // A lock on a limit that does not hold the terminals (another model's, another app's) has its own code
  // (rules.LOCKED_ELSEWHERE): it reads locked too, but the account is not out.
  const LOCKED = 'locked';
  const LOCKED_ELSEWHERE = 'locked_elsewhere';
  const LOCKED_TEXT = 'Locked by Claude';

  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const isLocked = (win) => !!win && win.blocked === LOCKED;
  const isElsewhere = (win) => !!win && win.blocked === LOCKED_ELSEWHERE;

  /** True when any window of the account is locked: it is out whatever the percentages say, also without
   * one (rules.locks; a window without a percentage keeps severity "unknown", as the service sends it). */
  function locked(account) {
    return [...((account && account.windows) || []), ...((account && account.other_windows) || [])].some(isLocked);
  }

  /** True while no reset brings the account back: a limit of it is locked, or Claude put it on hold (rules.held). */
  function held(account) {
    return locked(account) || (!!account && account.health === 'on_hold');
  }

  /** "2d 4h", "1h 12m", "45m", "4m 59s", "20s", "now": ceil minutes, seconds only under 5 min. */
  function countdownText(secs) {
    if (!isNum(secs)) return '—';
    if (secs <= 0) return 'now';
    if (secs < 300) {
      const total = Math.ceil(secs);
      const m = Math.floor(total / 60);
      const s = total % 60;
      return m ? `${m}m ${String(s).padStart(2, '0')}s` : `${s}s`;
    }
    const minutes = Math.ceil(secs / 60);
    const d = Math.floor(minutes / 1440);
    const h = Math.floor((minutes % 1440) / 60);
    const m = minutes % 60;
    if (d) return h ? `${d}d ${h}h` : `${d}d`;
    if (h) return m ? `${h}h ${m}m` : `${h}h`;
    return `${m}m`;
  }

  /** Countdown urgency tier: resetting ≤ 0 < imminent ≤ 15 min < soon ≤ 1 h < today ≤ 6 h < later. */
  function tier(secs) {
    if (!isNum(secs)) return null;
    if (secs <= 0) return 'resetting';
    if (secs <= IMMINENT) return 'imminent';
    if (secs <= 3600) return 'soon';
    if (secs <= 21600) return 'today';
    return 'later';
  }

  /** "3:40 PM" within 24 h of now, else "Thu 9:00 AM" (local time). */
  function clock(ts, nowMs) {
    if (!isNum(ts)) return '—';
    const date = new Date(ts * 1000);
    const hours = date.getHours();
    const text = `${hours % 12 || 12}:${String(date.getMinutes()).padStart(2, '0')} ${hours < 12 ? 'AM' : 'PM'}`;
    return Math.abs(ts * 1000 - nowMs) < 86400000 ? text : `${WEEKDAYS[date.getDay()]} ${text}`;
  }

  /** A forecast's time to the nearest `minutes`, as clock() words it: "1 AM", "Sat 5:30 PM". */
  function around(ts, minutes, nowMs) {
    if (!isNum(ts)) return '—';
    const step = minutes * 60;
    const rounded = Math.round(ts / step) * step;
    const date = new Date(rounded * 1000);
    const hours = date.getHours();
    const mins = date.getMinutes();
    const text = `${hours % 12 || 12}${mins ? `:${String(mins).padStart(2, '0')}` : ''} ${hours < 12 ? 'AM' : 'PM'}`;
    return Math.abs(rounded * 1000 - nowMs) < 86400000 ? text : `${WEEKDAYS[date.getDay()]} ${text}`;
  }

  function ageTier(account, now) {
    if (!isNum(account.last_good_at)) return null;
    const age = now - account.last_good_at;
    if (age <= (account.active ? 360 : 1200)) return 'fresh';
    return age <= 3600 ? 'aging' : 'stale';
  }

  /** A state window as it stands at nowMs: resets pass, refills expire, readings age. */
  function deriveWindow(win, account, nowMs) {
    const now = nowMs / 1000;
    const reset = isNum(win.reset) ? win.reset : null;
    const secs = reset === null ? null : reset - now;
    const at = account.last_good_at;
    let resetState = 'none';
    if (reset !== null) {
      if (secs > 0) resetState = 'scheduled';
      else if (!isNum(at) || at < reset) resetState = 'passed';   // no reading since the reset
    }
    const stale = ageTier(account, now) === 'stale';
    const refill = win.refilled && now - win.refilled.confirmed <= REFILL_KEEP ? win.refilled : null;
    let state;
    if (!win.started) state = 'not_started';
    else if (resetState === 'passed') state = stale ? 'expected_refill' : 'resetting';
    else if (win.severity === 'out') state = 'out';
    else if (refill) state = 'refilled';
    else state = stale ? 'last_known' : 'live';
    const hidden = HIDDEN.has(state);
    const used = hidden || !isNum(win.used) ? null : win.used;
    return {
      id: win.id, kind: win.kind, label: win.label, short: win.short, binding: !!win.binding,
      state, hidden, resetState, locked: isLocked(win), elsewhere: isElsewhere(win),
      used, left: used === null ? null : Math.max(0, 100 - used), known: used !== null,
      severity: used === null ? 'unknown' : win.severity,
      reset, secs, tier: tier(secs),
      countdown: secs === null ? null : countdownText(secs),
      clock: reset === null ? null : clock(reset, nowMs),
      refilledAt: state === 'refilled' ? refill.at : null,
    };
  }

  function bucketFor(secs) {
    for (const bucket of buckets) {
      if (bucket.max !== null && secs <= bucket.max) return bucket.id;
    }
    return null;
  }

  function chipFor(kind, left, severity, secs, accountOut) {
    if (severity === 'out' || (left !== null && left < 20)) return 'back_in';
    // "Use it" is meaningless while the account is out: its quota cannot be spent.
    if (!accountOut && left !== null) {
      if ((kind === 'weekly' || kind === 'model') && left >= 30 && secs <= HORIZON) return 'use_it';
      if (kind === 'session' && left >= 50 && secs <= 1800) return 'use_it';
    }
    return 'plain';
  }

  /** Resetting-soon rows at nowMs: refilled first, then by reset; `secs` added. None of a locked or on-hold
   * account: neither has a known end, so no reset refills it (rules.strip). */
  function stripItems(state, nowMs) {
    const now = nowMs / 1000;
    const rows = [];
    for (const account of (state && state.accounts) || []) {
      if (held(account)) continue;
      const derived = (account.windows || []).map((win) => [win, deriveWindow(win, account, nowMs)]);
      const out = derived.some(([, d]) => d.state === 'out');
      for (const [win, d] of derived) {
        const row = { account: account.id, label: account.label, provider: account.provider, window: win.id,
          window_label: win.label, reset: win.reset, left: d.left, severity: d.severity };
        if (d.state === 'refilled') {
          rows.push({ ...row, reset: win.refilled.at, chip: 'refilled', bucket: 'refilled',
            confirmed: win.refilled.confirmed });
        } else if (d.state === 'resetting') {
          rows.push({ ...row, chip: 'plain', bucket: 'minute' });
        } else if (d.resetState === 'scheduled' && d.state !== 'not_started' && d.secs <= HORIZON
            && (out || (d.used !== null && d.used >= MIN_USED))) {
          rows.push({ ...row, chip: chipFor(win.kind, d.left, d.severity, d.secs, out), bucket: bucketFor(d.secs) });
        }
      }
    }
    rows.sort((a, b) => (a.chip !== 'refilled') - (b.chip !== 'refilled') || a.reset - b.reset);
    return rows.map((row) => ({ ...row, secs: row.reset - now }));
  }

  function soonest(state, nowMs) {
    let best = null;
    for (const row of stripItems(state, nowMs)) {
      if (row.chip !== 'refilled' && row.secs > 0 && row.secs <= IMMINENT && (!best || row.secs < best.secs)) best = row;
    }
    return best;
  }

  function outCount(state, nowMs) {
    return ((state && state.accounts) || []).filter((account) => locked(account)
      || (account.windows || []).some((win) => deriveWindow(win, account, nowMs).state === 'out')).length;
  }

  function incident(state) {
    const providers = (state && state.providers) || {};
    return Object.values(providers).some((p) => p && p.status && (p.status.level === 'outage' || p.status.level === 'degraded'));
  }

  /** Browser-tab title: the soonest reset within 15 min, else accounts out, else an incident. */
  function titleSignal(state, nowMs) {
    const row = soonest(state, nowMs);
    if (row) {
      return { kind: 'reset', title: `${countdownText(row.secs)} · ${APP}`, secs: row.secs, account: row.account,
        window: row.window, count: 0 };
    }
    const count = outCount(state, nowMs);
    if (count) return { kind: 'out', title: `${count} out · ${APP}`, secs: null, account: null, window: null, count };
    if (incident(state)) return { kind: 'incident', title: `Incident · ${APP}`, secs: null, account: null, window: null, count: 0 };
    return { kind: 'none', title: APP, secs: null, account: null, window: null, count: 0 };
  }

  /** The account the favicon describes: the terminals' account first (this page's subject). */
  function activeAccount(state) {
    const accounts = (state && state.accounts) || [];
    return accounts.find((a) => a.active && a.provider === 'claude_cli') || accounts.find((a) => a.active) || null;
  }

  /** 32 × 32 SVG favicon: ring in the active account's binding severity colour, the % left
   * in bold digits, an accent dot when a reset is ≤ 15 min away, grey when errored or stale. */
  function faviconSvg(state, nowMs) {
    const account = activeAccount(state);
    let tone = 'muted';
    let figure = '';
    if (account && isNum(account.last_good_at)) {
      const shown = (account.windows || []).map((win) => deriveWindow(win, account, nowMs)).filter((d) => d.used !== null);
      const target = shown.find((d) => d.binding) || shown.reduce((low, d) => (!low || d.left < low.left ? d : low), null);
      if (target) {
        figure = String(Math.max(0, 100 - Math.round(target.used)));
        tone = TONE[target.severity] || 'muted';
      }
      if (locked(account)) tone = 'alarm';
      if (account.error || ageTier(account, nowMs / 1000) === 'stale') tone = 'muted';
    }
    const dot = soonest(state, nowMs) !== null;
    const css = (p) => `.ring{stroke:${p[tone]}}.fig{fill:${p.text}}.dot{fill:${p.plenty}}`;
    return '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">'
      + `<style>${css(FAVICON.light)}@media (prefers-color-scheme:dark){${css(FAVICON.dark)}}</style>`
      + '<circle class="ring" cx="16" cy="16" r="13" fill="none" stroke-width="4"/>'
      + (figure ? `<text class="fig" x="16" y="16" dominant-baseline="central" text-anchor="middle" font-family="-apple-system,system-ui,sans-serif" font-size="${figure.length > 2 ? 10 : 13}" font-weight="700">${figure}</text>` : '')
      + (dot ? '<circle class="dot" cx="26" cy="6" r="5"/>' : '')
      + '</svg>';
  }

  /** The leading number of a meter or tile: "63% used" or "37% left"; "—" when not reported; "Locked by
   * Claude" for a locked window, whatever its percentage, also one that does not hold the terminals. */
  function formatNumber(win, display) {
    if (isLocked(win) || isElsewhere(win)) return LOCKED_TEXT;
    const used = win && isNum(win.used) ? win.used : null;
    if (used === null) return '—';
    if (display === 'left') return `${Math.max(0, 100 - Math.round(used))}% left`;
    return used > 100 ? '100%+ used' : `${Math.round(used)}% used`;
  }

  AD.model = { deriveWindow, tier, countdownText, clock, around, stripItems, titleSignal, faviconSvg, formatNumber, locked, held };
})();
