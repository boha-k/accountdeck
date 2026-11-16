/* AccountDeck 2 — what the page knows about Claude Code (terminal) accounts, without any DOM:
 * which ones are listed, which is in use, which are blocked or frozen, sort orders, the limits a row
 * shows, the limit that binds, logins about to end, usage credits, the Fable 5.1 board and auto-switch's
 * next pick. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};

  const FABLE = /^Fable/i;
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const order = (a) => (isNum(a.sort_order) ? a.sort_order : 0);
  const signatures = new WeakMap();

  /** The terminal accounts of a state. */
  function terminals(state) {
    return ((state && state.accounts) || []).filter((a) => a.provider === 'claude_cli');
  }

  /** The terminal provider block, or null when this build has none. */
  function provider(state) {
    return (state && state.providers && state.providers.claude_cli) || null;
  }

  /** Auto-switch's block ({enabled, threshold, next_up, hold, last, previews, …}); {} when absent. */
  function autoswitch(state) {
    const block = provider(state);
    return (block && block.autoswitch) || {};
  }

  /** "used" or "left": what the numbers and bars show. */
  function display(state) {
    return state && state.settings && state.settings.usage_display === 'left' ? 'left' : 'used';
  }

  function active(state) {
    return terminals(state).find((a) => a.active) || null;
  }

  /** Accounts the Mac lists but cannot use: moved away, or still signed in on the PC. */
  function frozen(account) {
    return account.owner === 'moved' || account.owner === 'on_pc';
  }

  function ownerLabel(account) {
    return account.owner === 'moved' ? 'Moved to Mac' : 'Still on the PC';
  }

  function needsSignin(account) {
    const kind = (account.headline || {}).kind;
    return account.state === 'needs_signin' || account.health === 'needs_signin' || kind === 'signin';
  }

  /** Claude put it on hold: never switched to, and signing in again does not help. */
  function onHold(account) {
    return account.health === 'on_hold';
  }

  /** Out of quota, signed out, on hold or frozen: nothing to do with it until it is back. */
  function blocked(account) {
    const kind = (account.headline || {}).kind;
    return frozen(account) || needsSignin(account) || onHold(account) || account.state === 'limited' || kind === 'out';
  }

  // ---- Fable 5.1 ----------------------------------------------------------------------------------

  /** The Fable 5.1 weekly limit: a model window whose model starts with "Fable" (never matched by label). */
  function isFable(win) {
    return !!win && win.kind === 'model' && FABLE.test(String(win.model || ''));
  }

  function fableWindow(account) {
    return ((account && account.windows) || []).find(isFable) || null;
  }

  /** The Fable 5.1 room of every usable terminal account at nowMs: [{id, label, account, win, d}],
   * most room first (unknown readings last, saved order on ties). */
  function fableBoard(accounts, nowMs) {
    const rows = [];
    for (const account of accounts || []) {
      if (account.provider !== 'claude_cli' || blocked(account)) continue;
      const win = fableWindow(account);
      if (!win) continue;
      rows.push({ id: account.id, label: account.label, account, win, d: AD.model.deriveWindow(win, account, nowMs) });
    }
    const room = (r) => (r.d.left === null ? -1 : r.d.left);
    rows.sort((a, b) => room(b) - room(a) || order(a.account) - order(b.account));
    return rows;
  }

  /** The leader's id, or null on a tie or without readings. */
  function fableLeader(rows) {
    const [first, second] = rows;
    if (!first || first.d.left === null) return null;
    if (second && second.d.left !== null && Math.round(second.d.left) === Math.round(first.d.left)) return null;
    return first.id;
  }

  // ---- Limits -------------------------------------------------------------------------------------

  /** The 5-hour window, or null: Claude has reported none since 2026-10-31. */
  function sessionWindow(account) {
    return ((account && account.windows) || []).find((w) => w.kind === 'session' && !w.model) || null;
  }

  /** Whether the accounts table shows a 5-hour column: some account whose row shows limits (not frozen,
   * not signed out) reports a 5-hour window. */
  function showsSession(accounts) {
    return (accounts || []).some((a) => !frozen(a) && !needsSignin(a) && !!sessionWindow(a));
  }

  /** The limits a row shows: 5-hour (when reported), weekly, Fable 5.1 weekly (a placeholder when the
   * account has no Fable reading, so the columns line up), then any other model limit that binds and any
   * other meter that holds the terminals (`gates`: a new kind of limit, under the server's own label), both
   * marked extra. With `session` (the table shows a 5-hour column), a missing 5-hour window gets a placeholder
   * too, which says the account has none. */
  function limits(account, session) {
    const windows = account.windows || [];
    if (account.provider !== 'claude_cli') return windows;          // Codex and Claude Desktop: as the service gives them
    const main = windows.filter((w) => !w.model);
    if (session && !sessionWindow(account)) {
      main.unshift({ id: 'session-missing', kind: 'session', label: '5-hour', placeholder: true });
    }
    const fable = fableWindow(account)
      || { id: 'fable-missing', kind: 'model', model: 'Fable', label: AD.format.STRINGS.overview.fableFallback, placeholder: true };
    const others = windows.filter((w) => w.model && w.binding && !isFable(w))
      .concat((account.other_windows || []).filter((w) => w.gates)).map((w) => Object.assign({ extra: true }, w));
    return main.concat([fable], others);
  }

  // ---- Logins and money -------------------------------------------------------------------------

  const LOGIN_SOON = 7 * 86400;        // alerts.LOGIN_SOON: a login's end is shown from a week before it
  const LOGIN_LAST = 86400;            // ... and stands out on its last day

  /** A terminal login whose end matters at nowMs, by alerts.signin_due's rule: {state: 'soon' | 'ended', ends,
   * last} (last: under a day to go), else null. Not for an account left out, on hold or owned elsewhere (the
   * user's choice, or signing in again does not help), nor one that needs sign-in (it says so already). */
  function loginDue(account, nowMs) {
    if (!account || account.provider !== 'claude_cli' || account.saved === false || account.excluded || onHold(account)
        || frozen(account) || needsSignin(account) || !isNum(account.login_ends_at)) return null;
    const left = account.login_ends_at - nowMs / 1000;
    if (left > LOGIN_SOON) return null;
    return { state: left <= 0 ? 'ended' : 'soon', ends: account.login_ends_at, last: left <= LOGIN_LAST };
  }

  /** The service's sign-ins due ([{id, label, ends_at, state}], most urgent first); [] when none. */
  function signinDue(state) {
    const block = provider(state);
    return (block && Array.isArray(block.signin_due) ? block.signin_due : []).filter((row) => row && row.id);
  }

  /** A Claude account's "Usage credits" row (usage._claude_credits), or null. */
  function usageCredit(account) {
    if (!account || (account.provider !== 'claude_cli' && account.provider !== 'claude')) return null;
    return (account.credits || []).find((c) => c && c.label === 'Usage credits') || null;
  }

  /** The column labels of the accounts table, from the accounts that have each limit: always 5-hour, weekly
   * and Fable 5.1 weekly (the overview's total relies on that order); the table drops the first unless
   * showsSession(). */
  function columns(accounts) {
    const labels = ['5-hour', 'Weekly', AD.format.STRINGS.overview.fableFallback];
    const kinds = ['session', 'weekly'];
    for (const account of accounts) {
      for (const [index, kind] of kinds.entries()) {
        const win = (account.windows || []).find((w) => w.kind === kind && !w.model);
        if (win && win.label) labels[index] = win.label;
      }
      const fable = fableWindow(account);
      if (fable && fable.label) labels[2] = fable.label;
    }
    return labels;
  }

  /** The limit that holds the account back at nowMs: the one the service marks binding, else the one
   * with the least left. {win, d} or null without a reading. */
  function binding(account, nowMs) {
    const shown = (account.windows || []).map((win) => ({ win, d: AD.model.deriveWindow(win, account, nowMs) }))
      .filter((item) => item.d.used !== null);
    if (!shown.length) return null;
    return shown.find((item) => item.d.binding) || shown.reduce((low, item) => (item.d.left < low.d.left ? item : low));
  }

  // ---- Order --------------------------------------------------------------------------------------

  /** Percent left in one window at nowMs: 100 for a window not started yet, null when unknown. */
  function leftOf(win, account, nowMs) {
    if (!win) return null;
    const d = AD.model.deriveWindow(win, account, nowMs);
    return d.state === 'not_started' ? 100 : d.left;
  }

  const SORTS = ['room', 'fable', 'weekly', 'session', 'reset', 'manual'];

  /** Terminal rows in `mode` (one of SORTS): usable accounts first, unknown readings after known
   * ones, saved order on ties. "Most room left" ranks by the tightest of 5-hour, weekly and Fable 5.1. */
  function sorted(list, mode, nowMs) {
    const win = (a, kind) => (a.windows || []).find((w) => w.kind === kind);
    const key = {
      room: (a) => {
        const values = [win(a, 'session'), win(a, 'weekly'), fableWindow(a)].map((w) => leftOf(w, a, nowMs)).filter((v) => v !== null);
        return values.length ? Math.min(...values) : null;
      },
      fable: (a) => leftOf(fableWindow(a), a, nowMs),
      weekly: (a) => leftOf(win(a, 'weekly'), a, nowMs),
      session: (a) => leftOf(win(a, 'session'), a, nowMs),
      reset: (a) => { const w = win(a, 'weekly'); return w && isNum(w.reset) ? -w.reset : null; },
    }[mode];
    if (!key) return list.slice().sort((a, b) => blocked(a) - blocked(b) || order(a) - order(b));
    const rank = (a) => { const v = key(a); return v === null ? -Infinity : v; };
    return list.slice().sort((a, b) => blocked(a) - blocked(b) || rank(b) - rank(a) || order(a) - order(b));
  }

  // ---- Auto-switch --------------------------------------------------------------------------------

  /** The id of the account auto-switch would move the terminals to next, or null. */
  function nextUp(state) {
    const auto = autoswitch(state);
    if (!auto.enabled) return null;
    const row = (auto.next_up || []).find((r) => !r.skip);
    return row ? row.id : null;
  }

  /** The service's "most room right now" terminal pick (auto-switch off), or null. */
  function bestPick(state) {
    const best = state && state.now_band && state.now_band.best && state.now_band.best.claude_cli;
    if (!best || !best.account || best.state !== 'ok') return null;
    const account = terminals(state).find((a) => a.id === best.account);
    if (!account || account.active || account.excluded || blocked(account)) return null;
    return { account, headroom: isNum(best.headroom) ? best.headroom : null };
  }

  /** A stable text signature of an account object (cached: a state's objects never change). */
  function signature(account) {
    let sig = signatures.get(account);
    if (sig === undefined) {
      sig = JSON.stringify(account);
      signatures.set(account, sig);
    }
    return sig;
  }

  AD.accounts = {
    terminals, provider, autoswitch, display, active, frozen, ownerLabel, needsSignin, onHold, blocked,
    isFable, fableWindow, fableBoard, fableLeader, sessionWindow, showsSession, limits, loginDue, signinDue, usageCredit,
    columns, binding, SORTS, sorted, nextUp, bestPick, signature,
  };
})();
