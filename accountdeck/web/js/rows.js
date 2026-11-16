/* AccountDeck 2 — the rows of the accounts table. A row shows the account (monogram, name, badges,
 * a detail or status line, a login about to end, usage credits spent), its limits — 5-hour when reported,
 * weekly and Fable 5.1 weekly, plus any other model limit that binds and any other meter that holds the
 * terminals — and its action and More menu. Rows are keyed by account id and rebuilt
 * only when their data changed; countdowns are [data-reset] text the shell's clock patches.
 *
 * Keyboard: the list is one Tab stop (a roving tabindex); ↑ ↓ Home End move between rows, Return
 * runs the focused row's action, and Tab continues into the row's own buttons. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;
  const A = AD.accounts;
  const T = STRINGS.accounts;

  const SPOTLIGHT_MS = 1400;
  const STATUS_KINDS = new Set(['signin', 'out', 'outage', 'on_hold']);
  const STATUS_ICONS = { signin: 'warning', out: 'lock', outage: 'pause', on_hold: 'stop' };
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);
  const sigs = new WeakMap();
  let lastFocused = null;                       // the row id that holds the list's Tab stop

  // ---- The row's action and menu ----------------------------------------------------------------

  /** The switch a row offers: its button, or (`switch` false) none. */
  function switchAction(account, state, ctx, busy) {
    const target = state.pending && state.pending.account === account.id;
    const held = A.onHold(account) ? (account.error || {}).message || T.onHold : null;     // a switch would be refused
    return { label: target ? STRINGS.shell.switching : T.switch, run: () => AD.switch.open(account.id),
      disabled: !!ctx.readonly || busy || !!held, reason: ctx.readonly || held, prominent: ctx.nextId === account.id || ctx.bestId === account.id };
  }

  /** Sign In Again leads a row that is signed out, or whose terminal login has ended or ends within a day; the
   * account in use, which offers no switch, shows it as soon as its login ends within the week. */
  function signinFirst(account, nowMs) {
    const due = A.loginDue(account, nowMs);
    return A.needsSignin(account) || (!!due && (account.active || due.state === 'ended' || due.last));
  }

  /** The section that carries out a row's add, capture, rename and remove. */
  function actionsFor(account) {
    return { codex: AD.codex, claude: AD.desktop, antigravity: AD.antigravity }[account.provider] || AD.terminal;
  }

  /** The words of an app's own section (Codex, Claude Desktop or Antigravity; the terminal's are T). */
  function wordsFor(account) {
    return { codex: STRINGS.codex, antigravity: STRINGS.antigravity }[account.provider] || STRINGS.desktop;
  }

  /** A Claude Desktop account the PC import is still converting (desktop.js): read-only until it is done. */
  function importing(account, ctx) {
    return !!(ctx.importing && ctx.importing.has(account.id));
  }

  /** The row's one button: Switch, Sign In Again (signinFirst; Switch… then waits in the More menu), or nothing
   * for the account in use. */
  function primaryAction(account, state, ctx) {
    if (A.frozen(account)) return { label: 'Remove…', run: () => AD.move.remove(account), disabled: false };
    const reason = ctx.readonly;
    const busy = !!state.pending || (account.provider !== 'claude_cli' && !!state.login);
    const actions = actionsFor(account);
    if (account.saved === false && account.provider !== 'claude_cli') {
      const save = wordsFor(account).save;
      return { label: save, run: () => actions.capture(), disabled: !!reason || busy, reason };
    }
    // A Claude Desktop sign-in saved without its cookies cannot be switched to: signing in again saves it whole.
    // The one in use needs nothing (switching away saves it whole); one the import still converts waits for that.
    if (account.provider === 'claude' && !account.switchable) {
      if (account.active || importing(account, ctx)) return null;
      return { label: T.signIn, run: () => actions.add(account), disabled: !!reason || busy, reason };
    }
    if (signinFirst(account, ctx.nowMs)) return { label: T.signIn, run: () => actions.add(account), disabled: !!reason || busy, reason };
    if (account.active) return null;
    return switchAction(account, state, ctx, busy);
  }

  /** The More menu; `lead` is the action the row's button gave way to (Switch… behind Sign In Again, or Sign In
   * Again… behind Switch while a login ends later in the week), first in the menu. The account's limit resets
   * (resets.js) have a group of their own above Remove. */
  function menuItems(account, lead, ctx = {}) {
    const M = T.menu;
    const terminal = account.provider === 'claude_cli';
    const actions = actionsFor(account);
    const resets = AD.resets.menuItems(account, ctx.readonly, ctx.nowMs);
    return [
      lead ? { label: lead.menu, icon: lead.icon, disabled: lead.disabled, title: lead.reason || undefined, run: lead.run } : null,
      lead ? 'separator' : null,
      terminal ? { label: account.excluded ? M.include : M.leaveOut, icon: account.excluded ? 'plus-circle' : 'minus-circle',
        run: () => AD.terminal.exclude(account) } : null,
      { label: M.rename, icon: 'pencil', run: () => actions.rename(account) },
      resets.length ? 'separator' : null,
      ...resets,
      'separator',
      { label: M.remove, icon: 'trash', danger: true, disabled: account.active, title: account.active ? M.removeActive : undefined,
        run: () => actions.remove(account) },
      account.active ? { note: M.removeActive } : null,
    ].filter(Boolean);
  }

  /** The row's More button; `items` (menu items) replaces the menu rows.js would build. */
  function menuButton(account, lead, items, ctx) {
    const key = `menu-${account.id}`;
    const node = AD.dom.button('', () => AD.menu.open(node, items || menuItems(account, lead, ctx), { label: fill(T.more, { label: mask(account.label) }) }), {
      kind: 'icon', icon: 'ellipsis', label: fill(T.more, { label: mask(account.label) }), title: 'More',
      attrs: { 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'data-keep': key },
    });
    return node;
  }

  // ---- The row's text ------------------------------------------------------------------------------

  function badges(account, ctx) {
    if (A.frozen(account)) return [AD.meter.badge('neutral', A.ownerLabel(account))];
    const list = [];
    if (account.active) list.push(AD.meter.badge('good', STRINGS.chip.current, { icon: null, dot: true }));
    if (ctx.nextId === account.id) list.push(AD.meter.badge('accent', STRINGS.chip.next, { icon: 'bolt' }));
    else if (ctx.bestId === account.id) list.push(AD.meter.badge('accent', STRINGS.chip.best));
    if (ctx.fableLeader === account.id) list.push(AD.meter.badge('fable', STRINGS.overview.fableTag, { icon: 'crown' }));
    if (account.excluded) list.push(AD.meter.badge('neutral', STRINGS.chip.leftOut));
    if (account.saved === false) list.push(AD.meter.badge('neutral', STRINGS.chip.notSaved));
    const credit = A.usageCredit(account);
    if (credit && credit.enabled === true) list.push(AD.meter.badge('neutral', STRINGS.credits.on, { title: STRINGS.credits.onTip }));
    list.push(AD.resets.chip(account, 'row'));         // the limit resets it holds; null without any
    const error = account.error;
    if (error && !A.blocked(account)) {
      // An incident the status line already names (detailLines) is said once.
      if (error.outage) {
        if ((account.headline || {}).kind !== 'outage') list.push(AD.meter.badge('neutral', STRINGS.chip.incident, { icon: 'pause' }));
      }
      else if (error.code === 'blocked') list.push(AD.meter.badge('neutral', STRINGS.chip.cloudflare, { title: error.message }));
      else if (error.code !== 'rate_limited') {
        list.push(AD.meter.badge('critical', STRINGS.chip.error, { title: AD.format.errorText(error.code, error.message) }));
      }
    }
    return list.filter(Boolean);
  }

  /** "Checked 42m ago" once a reading is stale; paused checks say until when. */
  function freshness(account, nowMs) {
    if (isNum(account.paused_until) && account.paused_until > nowMs / 1000) {
      return el('p', { class: 'row-note', text: fill(T.paused, { clock: AD.model.clock(account.paused_until, nowMs) }) });
    }
    if (!isNum(account.last_good_at) || account.age_tier !== 'stale' || (account.headline || {}).kind === 'stale') return null;
    return el('p', { class: 'row-note is-stale', dataset: { ago: account.last_good_at, agoTemplate: T.checked },
      text: fill(T.checked, { age: AD.format.ago(account.last_good_at, nowMs) }) });
  }

  /** "Login ends Thu 6:16 AM" or "Login ended", in the status style; it stands out on its last day. */
  function loginLine(account, nowMs) {
    const due = A.loginDue(account, nowMs);
    if (!due) return null;
    const L = STRINGS.logins;
    const ended = due.state === 'ended';
    return el('p', { class: 'row-status', dataset: { kind: ended || due.last ? 'login-urgent' : 'login' }, title: ended ? L.endedTip : L.endsTip },
      AD.dom.svgIcon('clock'), el('span', { text: ended ? L.ended : fill(L.ends, { clock: AD.model.clock(due.ends, nowMs) }) }));
  }

  /** Usage credits spent this month, plainly: real money at API prices. */
  function moneyLine(account) {
    const credit = A.usageCredit(account);
    if (!credit || !isNum(credit.used) || credit.used <= 0) return null;
    const C = STRINGS.credits;
    const amount = AD.format.money(credit.used, credit);
    const limit = isNum(credit.limit) && credit.limit > 0 ? fill(C.limit, { amount: AD.format.money(credit.limit, credit, true) }) : '';
    const share = limit ? (credit.used / credit.limit) * 100 : credit.percent;
    return el('p', { class: 'row-money', dataset: { tone: isNum(share) && share >= 50 ? 'high' : null },
      title: fill(C.spentTip, { amount, limit }) }, AD.dom.svgIcon('card'), el('span', { text: fill(C.spent, { amount }) }));
  }

  /** Under the name: the status when something needs a look, then plan and email (also under a status: the email
   * says which login to sign in with); a login about to end and money spent; then any notes. */
  function detailLines(account, ctx) {
    const nowMs = ctx.nowMs;
    if (A.frozen(account)) return [];
    const lines = [];
    // On hold: a blocked row shows no error badge, so the status carries Claude's message as its title.
    const headline = A.onHold(account) ? { kind: 'on_hold', text: T.onHold, title: (account.error || {}).message }
      : account.headline || {};
    if (headline.text && STATUS_KINDS.has(headline.kind)) {
      lines.push(el('p', { class: 'row-status', dataset: { kind: headline.kind }, title: headline.title },
        AD.dom.svgIcon(STATUS_ICONS[headline.kind] || 'info'), el('span', { text: mask(headline.text) })));
    }
    const meta = [AD.format.capitalised(account.plan), account.email ? mask(account.email) : null].filter(Boolean).join(' · ');
    if (meta) lines.push(el('p', { class: 'row-sub', text: meta, title: meta }));
    lines.push(loginLine(account, nowMs), moneyLine(account), AD.resets.doubtLine(account, nowMs));
    if (account.live_expired) lines.push(el('p', { class: 'row-note', text: T.liveExpired }));
    // Why a saved Claude Desktop account offers Sign In Again instead of Switch (primaryAction). The one in use
    // needs no word (switching away saves it whole); one the import still converts says so in its limits.
    if (account.provider === 'claude' && account.saved !== false && !account.switchable && !account.active
        && !importing(account, ctx) && !A.needsSignin(account)) {
      lines.push(el('p', { class: 'row-note', text: STRINGS.desktop.incomplete }));
    }
    const fresh = freshness(account, nowMs);
    if (fresh) lines.push(fresh);
    return lines.filter(Boolean);
  }

  function limitCells(account, ctx) {
    if (A.frozen(account)) return null;
    if (A.needsSignin(account)) return el('p', { text: T.blockedSignin });
    // Never read until the import finishes it, so it says what finishes it rather than wait for a reading.
    if (importing(account, ctx)) return el('p', { class: 'row-note', text: STRINGS.desktop.importing });
    const limits = A.limits(account, ctx.session);
    if (!limits.length && account.provider !== 'claude_cli') {
      return el('p', { class: 'row-note', text: wordsFor(account).waiting || STRINGS.desktop.waiting });
    }
    return limits.map((win) => AD.meter.limit(win, account, ctx));
  }

  // ---- A row ------------------------------------------------------------------------------------

  /** One account row. ctx: {nowMs, display, threshold, fableLeader, nextId, bestId, readonly, session, importing}
   * (session: the table shows a 5-hour column; importing: the ids of Claude Desktop accounts the PC import still
   * converts). */
  function row(account, state, ctx) {
    const action = primaryAction(account, state, ctx);
    const nameId = `row-${account.id}-name`;
    const actions = [];
    if (action) {
      actions.push(AD.dom.button(action.label, action.run, {
        kind: action.prominent && !action.disabled ? 'primary' : undefined, small: true, disabled: action.disabled,
        title: action.reason || undefined, className: 'row-action',
        attrs: { 'data-primary': '', 'data-keep': `primary-${account.id}`, 'aria-describedby': nameId } }));
    }
    // A login that ends this week keeps both actions one click away: the one not on the button leads the menu.
    // A login that has ended offers no switch: the terminals could not use it.
    let lead = null;
    const due = A.loginDue(account, ctx.nowMs);
    if (due) {
      const busy = !!state.pending;
      if (signinFirst(account, ctx.nowMs)) {
        lead = account.active || due.state === 'ended' ? null
          : Object.assign(switchAction(account, state, ctx, busy), { menu: T.menu.switchTo, icon: 'bolt' });
      } else {
        lead = { menu: T.menu.signIn, icon: 'refresh', run: () => AD.terminal.add(account), disabled: !!ctx.readonly || busy, reason: ctx.readonly };
      }
    }
    // No menu for an unsaved account (nothing to rename or remove yet) or one the import still converts.
    if (!A.frozen(account) && (account.provider === 'claude_cli' || account.saved !== false) && !importing(account, ctx)) {
      actions.push(menuButton(account, lead, null, ctx));
    }
    return el('li', {
      class: ['row', A.blocked(account) && 'is-blocked', account.active && 'is-current', ctx.nextId === account.id && 'is-next',
        account.stale && 'is-stale', account.refreshing && 'is-refreshing'],
      id: `row-${account.id}`, tabindex: -1, 'aria-labelledby': nameId, dataset: { id: account.id },
    },
    el('div', 'row-id',
      AD.dom.avatar(account.label, { active: account.active, muted: A.blocked(account) }),
      el('div', 'row-text',
        el('div', 'row-title', el('h3', { class: 'row-name', id: nameId, text: mask(account.label) }), badges(account, ctx)),
        detailLines(account, ctx))),
    el('div', { class: ['row-limits', A.needsSignin(account) && 'is-message'] }, limitCells(account, ctx)),
    el('div', 'row-actions', actions));
  }

  function rowSig(account, state, ctx) {
    const derived = (account.windows || []).map((w) => AD.model.deriveWindow(w, account, ctx.nowMs).state);
    const due = A.loginDue(account, ctx.nowMs);
    return JSON.stringify([A.signature(account), derived, due && [due.state, due.last], state.pending ? state.pending.account || true : null,
      ctx.display, ctx.threshold, AD.dom.hideEmails(), ctx.fableLeader === account.id, ctx.nextId === account.id,
      ctx.bestId === account.id, ctx.readonly, account.provider !== 'claude_cli' && !!state.login, !!ctx.session, importing(account, ctx)]);
  }

  /** Keep `list` (an <ol>) showing `accounts` as rows: unchanged rows keep their node and focus. A section whose
   * accounts have their own shape (Antigravity) passes ctx.build(account, state, ctx) and ctx.signature(account,
   * state, ctx) for its rows; the list, its keyboard and its Tab stop stay these. */
  function patchRows(list, accounts, state, ctx) {
    const build = ctx.build || row;
    const signature = ctx.signature || rowSig;
    const make = (account) => {
      const node = build(account, state, ctx);
      sigs.set(node, signature(account, state, ctx));
      return node;
    };
    AD.dom.patchList(list, accounts, 'id', make,
      (node, account) => (sigs.get(node) === signature(account, state, ctx) ? null : make(account)));
    syncTabStop(list);
  }

  // ---- Keyboard ---------------------------------------------------------------------------------

  /** Exactly one row is in the Tab order: the one last focused, else the first. */
  function syncTabStop(list) {
    const rows = Array.from(list.children);
    const keep = rows.find((node) => node.getAttribute('data-id') === lastFocused) || rows[0];
    for (const node of rows) node.tabIndex = node === keep ? 0 : -1;
  }

  function focusRow(node, scroll = true) {
    if (!node) return false;
    const list = node.parentElement;
    for (const other of list.children) other.tabIndex = other === node ? 0 : -1;
    lastFocused = node.getAttribute('data-id');
    node.focus({ preventScroll: true });
    if (scroll) node.scrollIntoView({ block: 'nearest' });
    return true;
  }

  /** Arrow keys and Return on the rows of `list` (bound once). */
  function bindKeys(list) {
    list.addEventListener('focusin', (event) => {
      const node = event.target.closest('.row');
      if (node && node.parentElement === list) {
        lastFocused = node.getAttribute('data-id');
        for (const other of list.children) other.tabIndex = other === node ? 0 : -1;
      }
    });
    list.addEventListener('keydown', (event) => {
      if (!event.target.matches('.row') || event.metaKey || event.ctrlKey || event.altKey) return;
      const rows = Array.from(list.children);
      const index = rows.indexOf(event.target);
      let next = null;
      if (event.key === 'ArrowDown') next = rows[Math.min(rows.length - 1, index + 1)];
      else if (event.key === 'ArrowUp') next = rows[Math.max(0, index - 1)];
      else if (event.key === 'Home') next = rows[0];
      else if (event.key === 'End') next = rows[rows.length - 1];
      else if (event.key === 'Enter' || event.key === ' ') {
        const primary = event.target.querySelector('[data-primary]');
        if (primary && !primary.disabled) {
          event.preventDefault();
          primary.click();
        }
        return;
      }
      if (next) {
        event.preventDefault();
        focusRow(next);
      }
    });
  }

  /** Focus the row at `index` (the 1–9 keys); false when there is none. */
  function focusIndex(index) {
    return focusRow(document.querySelectorAll('#accounts .row')[index] || null);
  }

  /** Scroll to a row and flash it (from the overview's names). */
  function spotlight(id) {
    const node = document.getElementById(`row-${id}`);
    if (!node) return;
    focusRow(node, false);
    node.scrollIntoView({ block: 'center', behavior: AD.theme.reducedMotion() ? 'auto' : 'smooth' });
    node.classList.remove('is-spotlit');
    void node.offsetWidth;                      // restart the animation when spotlit twice
    node.classList.add('is-spotlit');
    window.setTimeout(() => node.classList.remove('is-spotlit'), SPOTLIGHT_MS);
  }

  AD.rows = { patchRows, bindKeys, focusIndex, spotlight, more: (account, items) => menuButton(account, null, items) };
})();
