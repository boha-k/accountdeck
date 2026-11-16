/* AccountDeck 2 — meters and badges.
 * A meter is a capsule bar like the ones in System Settings: its fill follows the "Show usage as"
 * setting (the part used, or the part left), its colour follows the limit's severity, and a notch
 * marks where auto-switch moves the terminals on. Unknown usage draws no fill and reads "not
 * reported", never 0. The countdown inside aria-valuetext is kept current by the shell's clock. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;

  const BADGE_ICONS = { good: 'check', warning: 'warning', critical: 'warning', accent: null, neutral: null, fable: null };
  // Limits auto-switch watches: the 5-hour, the weekly and model (Fable 5.1 / pinned model) limits.
  const GATING = new Set(['session', 'weekly', 'model']);
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

  /** The share a meter shows, 0–100: used, or left when the page shows what is left. */
  function shown(d, display) {
    if (d.used === null) return null;
    const used = Math.min(100, Math.max(0, d.used));
    return display === 'left' ? 100 - used : used;
  }

  /** A usage meter for a state window. opts: account and nowMs (to derive the window at that time),
   * display ('used' | 'left'), threshold (auto-switch's percentage, to draw its notch), size ('large'). */
  function meter(win, opts = {}) {
    const d = AD.model.deriveWindow(win, opts.account || {}, isNum(opts.nowMs) ? opts.nowMs : AD.app.clockNow());
    const display = opts.display === 'left' ? 'left' : 'used';
    const classes = ['meter', opts.size === 'large' && 'meter-large', `sev-${d.severity}`];
    if (d.state === 'out' || d.locked) classes.push('is-out');
    if (d.state === 'resetting' || d.state === 'expected_refill') classes.push('is-resetting');
    const lead = d.used === null && !d.locked
      ? `${d.label}: ${STRINGS.window.notReported.toLowerCase()}`
      : `${d.label}: ${AD.model.formatNumber(win, display)}`;
    const timed = d.used !== null && !d.locked && !AD.model.held(opts.account) && isNum(d.secs) && d.secs > 0;    // no reset brings it back
    const template = timed ? `${lead}, ${d.state === 'out' ? 'back' : 'resets'} in {countdown}, at ${d.clock}` : null;
    const node = AD.dom.el('div', {
      class: classes, role: 'meter', 'aria-valuemin': 0, 'aria-valuemax': 100,
      'aria-valuetext': timed ? fill(template, { countdown: d.countdown }) : lead,
      dataset: timed ? { reset: d.reset, resetTemplate: template, resetAttr: 'aria-valuetext', resetDone: lead } : null,
    });
    const share = shown(d, display);
    if (share !== null) {
      node.setAttribute('aria-valuenow', String(Math.round(share)));
      node.appendChild(AD.dom.el('span', { class: 'meter-fill', style: { '--fill': `${d.state === 'out' ? (display === 'left' ? 0 : 100) : share}%` } }));
    }
    if (isNum(opts.threshold) && GATING.has(win.kind) && share !== null) {
      const at = display === 'left' ? 100 - opts.threshold : opts.threshold;
      node.appendChild(AD.dom.el('span', { class: 'meter-mark', 'aria-hidden': 'true', style: { '--at': `${at}%` } }));
    }
    return node;
  }

  // ---- A limit: label, value, meter and when it resets ----------------------------------------

  /** The value of a limit: "30% used" (or "70% left"), "Not started", "Locked by Claude" (with or without
   * a percentage), or "—" when there is none. */
  function limitValue(win, d, display) {
    if (d.locked) return AD.model.formatNumber(win, display);
    if (d.state === 'not_started') return STRINGS.window.notStarted;
    if (d.hidden || d.used === null) return '—';
    return AD.model.formatNumber(win, display);
  }

  /** The line under a limit's bar (or, compact, beside its label): when it resets (a live
   * countdown), or why there is no time. */
  function limitNote(win, d, nowMs, tag = 'p') {
    const note = (props, ...children) => AD.dom.el(tag, Object.assign({ class: 'limit-note' }, props), ...children);
    if (d.state === 'not_started') return note({ text: STRINGS.window.startsNext });
    if (d.state === 'expected_refill') return note({ text: STRINGS.window.expected });
    if (d.state === 'resetting') {
      return note({ dataset: { tier: 'resetting' }, title: fill(STRINGS.window.resettingTip, { clock: AD.model.clock(win.reset, nowMs) }) },
        AD.dom.svgIcon('refresh', { className: 'is-spinning' }), STRINGS.window.resetting);
    }
    if (d.state === 'refilled') {
      return note({ class: 'limit-note is-good' }, AD.dom.svgIcon('spark'),
        fill(STRINGS.window.refilled, { clock: AD.model.clock(d.refilledAt, nowMs) }));
    }
    if (isNum(win.reset) && isNum(d.secs) && d.secs > 0) {
      const out = d.state === 'out';
      const template = out ? STRINGS.window.backIn : STRINGS.window.resetsIn;
      return note({ class: ['limit-note', out && 'is-out'], dataset: { tier: out ? null : AD.model.tier(d.secs) },
        title: fill(STRINGS.window.resetsAt, { clock: AD.model.clock(win.reset, nowMs) }) },
      AD.dom.el('time', { dataset: { reset: win.reset, resetTemplate: template, resetDone: STRINGS.window.resetting },
        text: fill(template, { countdown: AD.model.countdownText(d.secs) }) }));
    }
    return null;
  }

  /** A second line for a limit that needs words: a temporary 5-hour limit (an Ultra account Claude has not
   * verified yet, `temporary_limit`), or a lock on a limit the terminals do not depend on. */
  function limitAside(win, d, account) {
    const W = STRINGS.window;
    if (d.elsewhere) return AD.dom.el('p', { class: 'limit-aside', title: W.elsewhereTip }, AD.dom.svgIcon('lock'), W.elsewhere);
    if (win.kind === 'session' && account && account.temporary_limit) {
      return AD.dom.el('p', { class: 'limit-aside', title: W.temporaryTip }, AD.dom.svgIcon('info'), W.temporary);
    }
    return null;
  }

  /** One limit of an account: label, value, meter and note. ctx: {nowMs, display, threshold, compact}.
   * Compact (the overview) puts the note beside the label: one line of text, then the bar.
   * A placeholder window (an account without a Fable 5.1 reading, or without a 5-hour one while the
   * table shows that column) keeps its slot and says so quietly. */
  function limit(win, account, ctx) {
    const fable = AD.accounts.isFable(win);
    const label = AD.dom.el('span', { class: 'limit-label' },
      fable ? AD.dom.el('span', { class: 'fable-mark', 'aria-hidden': 'true' }) : null, win.label);
    if (win.placeholder) {
      const none = win.kind === 'session';           // the account has no 5-hour limit; others still do
      return AD.dom.el('div', { class: ['limit', 'is-placeholder', ctx.compact && 'is-compact'], dataset: { state: 'missing', severity: 'unknown', fable: fable ? 'true' : null } },
        AD.dom.el('div', 'limit-head', label, AD.dom.el('span', { class: 'limit-note', title: none ? STRINGS.window.noSessionTip : null,
          text: none ? STRINGS.window.noSession : STRINGS.window.notReported })));
    }
    const d = AD.model.deriveWindow(win, account, ctx.nowMs);
    const value = AD.dom.el('span', { class: 'limit-value', text: limitValue(win, d, ctx.display) });
    // No reset brings a locked or on-hold account back, so no limit of it names a time or a refill (rules.held).
    const held = AD.model.held(account);
    const bar = meter(win, { account, nowMs: ctx.nowMs, display: ctx.display, threshold: ctx.threshold });
    const data = { state: d.state, severity: d.severity, binding: d.binding ? 'true' : null, fable: fable ? 'true' : null,
      extra: win.extra ? 'true' : null, elsewhere: d.elsewhere ? 'true' : null };
    const aside = limitAside(win, d, account);
    if (ctx.compact) {
      return AD.dom.el('div', { class: 'limit is-compact', dataset: data },
        AD.dom.el('div', 'limit-head', AD.dom.el('span', 'limit-lead', label, held ? null : limitNote(win, d, ctx.nowMs, 'span')), value), bar, aside);
    }
    return AD.dom.el('div', { class: 'limit', dataset: data }, AD.dom.el('div', 'limit-head', label, value), bar,
      held ? null : limitNote(win, d, ctx.nowMs), aside);
  }

  /** A small capsule badge: an optional icon and words, never colour alone.
   * kind: good | warning | critical | accent | fable | neutral. opts: icon, title, dot. */
  function badge(kind, text, opts = {}) {
    const icon = opts.icon === undefined ? BADGE_ICONS[kind] : opts.icon;
    return AD.dom.el('span', { class: ['badge', kind !== 'neutral' && `badge-${kind}`, opts.dot && 'badge-dot'], title: opts.title },
      icon ? AD.dom.svgIcon(icon) : null, AD.dom.el('span', { text }));
  }

  AD.meter = { meter, limit, badge, shown };
})();
