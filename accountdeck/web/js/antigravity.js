/* AccountDeck 2 — the Antigravity accounts section (providers.antigravity: Google's agy command-line tool).
 * Built like the Codex section: a header (Refresh Usage, Save Current Account, Add Antigravity Account), notes, the
 * table and a quiet footer. Rows are drawn here with rows.js's look and keyboard (an Antigravity account has its own
 * shape: a usage reading of model groups, each with a 5-hour and a weekly limit, instead of `windows`).
 *
 * Nothing here runs by itself: usage is read only when the user clicks Refresh Usage (the service also reads it when
 * AccountDeck is opened, if the setting allows), and Save, Add and Switch start only from a click, through the plan
 * and consent of switch.js and, once, the notice about Google's terms. AccountDeck never stops an agy: a switch waits
 * for it and an Add cannot start while one runs. The state never holds a token, and nothing here asks for one. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;
  const G = STRINGS.antigravity;

  const PROVIDER = 'antigravity';
  const UNSAVED = 'unsaved-antigravity';
  const WINDOWS = ['5h', 'weekly'];                  // the limits each group lines up in columns, in this order
  const ENDED_KEY = 'ad2.agyEnrollmentSeen';         // the end of an Add already shown (this browser)
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);

  let parts = null;                                  // {head, notes, body, foot}: the section's slots
  const sigs = { head: null, notes: null, body: null, foot: null };
  let endedSeen = loadEnded();
  // An Add cancelled here: {started, op, summary}. Its report (operation `op`, `summary` once it has come) is on
  // screen, so its end is said again only when that report did not say it (sayEnded).
  let quiet = null;
  let recoverShown = null;                           // what the open Recover sheet was built from (recoverSig)
  let recovering = false;                            // Recover was sent and its answer has not come yet
  let wasReading = null;                             // refresh.busy at the last render (announce the end of a read)

  // ---- What the state says -----------------------------------------------------------------------

  function block(state) {
    const b = state && state.providers && state.providers.antigravity;
    return b && typeof b === 'object' ? b : null;
  }

  function accountsOf(b) {
    return (Array.isArray(b.accounts) ? b.accounts : []).map((a) => Object.assign({ provider: PROVIDER }, a));
  }

  function consumers(b) {
    return b.running && Array.isArray(b.running.consumers) ? b.running.consumers : [];
  }

  function signingIn(b) {
    return !!(b.enrollment && b.enrollment.phase === 'signing_in');
  }

  /** An Add that is ending: it waits until no agy runs before the earlier sign-in goes back (never under a
   * running agy, whose own sign-in could have just finished). */
  function restoring(b) {
    return !!(b.enrollment && b.enrollment.phase === 'restoring');
  }

  /** An Add is open: someone is signing in, or it is ending. Nothing else for Antigravity starts meanwhile. */
  function addOpen(b) {
    return signingIn(b) || restoring(b);
  }

  function storeState(b) {
    return (b.store && b.store.state) || 'unknown';
  }

  /** The section shows while agy is installed or an account is saved (or an Add or a recovery is open). */
  function shown(state) {
    const b = block(state);
    return !!b && (!!b.installed || accountsOf(b).length > 0 || !!b.recovery || addOpen(b));
  }

  /** Something Antigravity is under way that the next state will change: the page polls faster meanwhile. */
  function waiting(state) {
    const b = block(state);
    return !!b && (!!(b.refresh && b.refresh.busy) || !!b.switching || addOpen(b));
  }

  /** Why Save, Switch and Sign In Again can't start now (the buttons' title), or null. */
  function blockedReason(state, b) {
    if (state.read_only) return STRINGS.accounts.readOnlyOther;
    if (b.recovery) return G.recoverFirst;
    if (state.recovery && state.recovery.pending) return STRINGS.desktop.recoverFirst;
    if (restoring(b)) return G.addEnding;
    if (storeState(b) === 'unknown') return (b.store && b.store.message) || G.storeText;
    if (signingIn(b)) return G.addOpen;
    return null;
  }

  /** Why Add (and Sign In Again, which is an Add) can't start: the above, agy missing, or agy running (it would be
   * signed out, and AccountDeck never stops it). */
  function addReason(state, b) {
    return blockedReason(state, b) || (b.installed ? null : G.notFound) || (consumers(b).length ? G.addRunning : null);
  }

  /** Why Refresh Usage can't read now, or null. */
  function refreshReason(state, b) {
    if (!b.installed) return G.notFound;
    if (restoring(b)) return G.addEnding;
    if (storeState(b) === 'unknown') return (b.store && b.store.message) || G.storeText;
    if (storeState(b) !== 'ok') return G.signedOut;
    if (b.paused) return G.paused;
    if (b.recovery) return G.recoverFirst;
    if (signingIn(b)) return G.addOpen;
    if (b.switching) return G.switching;
    return null;
  }

  // ---- Actions ----------------------------------------------------------------------------------------

  /** Run `next` now, or after the notice about Google's terms while it has not been accepted yet. */
  function gate(next) {
    const b = block(AD.app.state);
    if (b && b.notice_seen === false) {
      notice(next);
      return undefined;
    }
    return next();
  }

  const capture = () => gate(() => AD.switch.openPurpose(PROVIDER, 'capture'));
  /** Add another account, or (a saved `account`) sign in to it again: the same Add, for that account. */
  const add = (account) => {
    const again = account && account.provider === PROVIDER && account.saved !== false && account.id !== UNSAVED;
    return gate(() => AD.switch.openPurpose(PROVIDER, 'add', again ? { id: account.id } : undefined));
  };
  const switchTo = (account) => gate(() => AD.switch.open(account.id, { provider: PROVIDER }));

  /** Send an Antigravity action; resolves to the answer, or null after a refusal (shown as the notice or the
   * plan the refusal asks for, else in `into`, the open dialog's own error line, else as a toast). */
  async function act(name, body, into) {
    if (into) into.hidden = true;
    try {
      return await AD.api.action(name, Object.assign({ provider: PROVIDER }, body));
    } catch (err) {
      if (err.code === 'notice_required') {
        notice(() => act(name, body));
      } else if ((err.code === 'confirm_required' || err.code === 'needs_reconfirm') && err.plan) {
        AD.switch.showPlan(err.plan);
      } else if (into) {
        // A dialog is open: a toast would sit behind it, where neither the eye nor a screen reader finds it.
        into.textContent = mask(err.message);
        into.hidden = false;
      } else {
        AD.dom.toast(mask(err.message), { level: err.code === 'rate_limited' || err.code === 'invalid' ? 'info' : 'critical' });
      }
      return null;
    }
  }

  const errorLine = () => el('p', { class: 'modal-error', role: 'alert', hidden: true });

  /** Refresh Usage: agy's own usage command, for the signed-in account (the service allows it once a minute). */
  function refresh() {
    const state = AD.app.state;
    const b = block(state);
    if (!b || (b.refresh && b.refresh.busy) || refreshReason(state, b) || cooling(b, AD.app.clockNow())) return undefined;
    return act('refresh', {});
  }

  /** Cancel an open Add. Its progress and report follow here, so the end of that Add is not said again (sayEnded). */
  async function cancelAdd() {
    const b = block(AD.app.state);
    const started = b && b.enrollment ? b.enrollment.started : null;
    const result = await act('cancel-login', {});
    if (!result || !result.op) return;
    quiet = { started, op: result.op, summary: null };
    AD.switch.follow(result);
  }

  function rename(account) {
    const R = STRINGS.accounts.rename;
    const input = el('input', { class: 'text-input', value: account.label, maxlength: 80,
      'aria-label': R.label, 'data-autofocus': '', autocomplete: 'off', spellcheck: 'false' });
    const error = errorLine();
    const save = async () => {
      const label = input.value.trim();
      if (!label || label === account.label) { AD.dom.closeModal(); return; }
      if (await act('rename', { id: account.id, label }, error)) AD.dom.closeModal();
    };
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); save(); }
    });
    AD.dom.openModal({ title: R.title, view: 'agy-rename', size: 'alert', body: [input, error],
      actions: [AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal()),
        AD.dom.button(R.save, save, { kind: 'primary' })] });
    input.select();
  }

  function remove(account) {
    const R = STRINGS.accounts.remove;
    const error = errorLine();
    AD.dom.openModal({ title: fill(R.title, { label: mask(account.label) }),
      view: 'agy-remove', size: 'alert', body: [el('p', { text: G.removeBody }), error],
      actions: [AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal(), { attrs: { 'data-autofocus': '' } }),
        AD.dom.button(R.button, async () => {
          if (await act('remove', { id: account.id }, error)) AD.dom.closeModal();
        }, { kind: 'danger' })] });
  }

  // ---- The notice about Google's terms (PLAN §7) -----------------------------------------------------

  /** Shown before the first Save, Add or Switch, and whenever the service asks for it (`notice_required`).
   * Continue records that it was accepted (`agy-accept-notice`), then goes on with `next`. Cancel is the default. */
  function notice(next) {
    const error = el('p', { class: 'modal-error', role: 'alert', hidden: true });
    const [first, second, risk] = G.notice;
    const body = el('div', 'agy-notice',
      el('p', { class: 'plan-lead', text: first }),
      el('p', { class: 'plan-lead', text: second }),
      el('p', 'agy-notice-risk', AD.dom.svgIcon('warning'), el('span', { text: risk })));
    const go = async () => {
      try {
        await AD.api.action('agy-accept-notice', {});
      } catch (err) {
        error.textContent = mask(err.message);
        error.hidden = false;
        return;
      }
      if (typeof next === 'function') await next();
      // `next` normally replaces this view with its plan; if it showed nothing (a refusal became a toast), close.
      if (AD.dom.modalView() === 'agy-notice') AD.dom.closeModal();
    };
    AD.dom.openModal({ title: G.noticeTitle, view: 'agy-notice', body: [error, body],
      actions: [AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal(),
        { dataset: { choice: 'cancel' }, attrs: { 'data-autofocus': '' } }),
      AD.dom.button(G.continue, go, { kind: 'primary', dataset: { choice: 'continue' } })] });
  }

  // ---- Recover: a switch that could neither finish nor undo itself ------------------------------------

  function labelOf(b, email) {
    const account = accountsOf(b).find((a) => a.email === email);
    return mask(account ? account.label : email);
  }

  function recoveryText(b) {
    const r = b.recovery;
    if (r.before && r.after) return fill(G.recoverBoth, { before: mask(r.before), after: mask(r.after) });
    if (r.after) return fill(G.recoverFromNone, { after: mask(r.after) });
    return fill(G.recoverAway, { before: mask(r.before || '') });
  }

  /** What the Recover sheet shows of the state: when it changes under the open sheet, the sheet is built again. */
  function recoverSig(state, b) {
    return JSON.stringify([b.recovery, b.signed_in, storeState(b), consumers(b).length > 0, !!state.read_only,
      accountsOf(b).map((a) => [a.email, a.label]), AD.dom.hideEmails()]);
  }

  /** The Recover sheet: what agy has now, and the three things Recover can leave in agy's sign-in. `from`: the
   * state to build it from (render, with the state it is drawing); else the page's current one. */
  function recover(from) {
    const state = from && from.providers ? from : AD.app.state;
    const b = block(state);
    if (!b || !b.recovery) return;
    recoverShown = recoverSig(state, b);
    const r = b.recovery;
    const C = G.choices;
    const choices = [
      r.before ? ['before', fill(C.before[0], { label: labelOf(b, r.before) }), C.before[1]] : ['before', C.beforeNone[0], C.beforeNone[1]],
      r.after ? ['after', fill(C.after[0], { label: labelOf(b, r.after) }), C.after[1]] : null,
      ['keep', C.keep[0], C.keep[1]],
    ].filter(Boolean);
    const store = storeState(b);
    const now = store === 'ok' && b.signed_in ? fill(G.nowSigned, { email: mask(b.signed_in) })
      : (store === 'absent' ? G.nowSignedOut : G.nowUnknown);
    const error = el('p', { class: 'modal-error', role: 'alert', hidden: true });
    // A sign-in can't be put back while agy runs (it would move to it); leaving it as it is works at any time.
    const running = consumers(b).length > 0;
    const first = running ? 'keep' : choices[0][0];
    const list = el('div', { class: 'agy-choices', role: 'radiogroup', 'aria-label': G.recoverSheet },
      choices.map(([value, title, hint]) => el('label', 'agy-choice',
        el('input', { type: 'radio', name: 'agy-keep', value, checked: value === first,
          disabled: running && value !== 'keep', 'data-autofocus': value === first ? '' : null }),
        el('span', 'agy-choice-text', el('span', { class: 'agy-choice-title', text: title }),
          el('span', { class: 'agy-choice-hint', text: hint })))));
    const waits = running ? el('p', { class: 'plan-muted', text: G.recoverRunning }) : null;
    const go = async () => {
      const picked = list.querySelector('input:checked');
      let result;
      recovering = true;
      try {
        result = await AD.api.action('recover', { provider: PROVIDER, keep: picked ? picked.value : first });
      } catch (err) {
        error.textContent = mask(err.message);
        error.hidden = false;
        return;
      } finally {
        recovering = false;
      }
      if (result && result.op) AD.switch.follow(result);
      else AD.dom.closeModal();
    };
    AD.dom.openModal({ title: G.recoverSheet, view: 'agy-recover',
      body: [error, el('p', { class: 'plan-lead', text: recoveryText(b) }), el('p', { class: 'plan-muted', text: now }), waits, list],
      actions: [AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal(), { dataset: { choice: 'cancel' } }),
        AD.dom.button(G.recoverButton, go, { kind: 'primary', dataset: { choice: 'recover' } })] });
  }

  // ---- A usage reading -------------------------------------------------------------------------------

  /** rules.band on what is left (percent): how a limit is coloured. */
  function band(left) {
    if (left <= 0.5) return 'out';
    if (left < 5) return 'critical';
    if (left < 20) return 'low';
    if (left < 50) return 'watch';
    return 'plenty';
  }

  function windowLabel(bucket) {
    return G.windows[bucket.window] || bucket.name || AD.format.capitalised(String(bucket.window || '')) || '—';
  }

  /** A bucket's reset has passed since the reading: the limit is full again (as far as this reading can say). */
  function resetPassed(bucket, reading, nowMs) {
    return isNum(bucket.resets_at) && bucket.resets_at <= nowMs / 1000 && isNum(reading.at) && reading.at < bucket.resets_at;
  }

  function label(text) {
    return el('span', { class: 'limit-label' }, text);
  }

  function resetNote(reset, nowMs) {
    const secs = isNum(reset) ? reset - nowMs / 1000 : null;
    if (secs === null || secs <= 0) return null;
    const W = STRINGS.window;
    return el('p', { class: 'limit-note', dataset: { tier: AD.model.tier(secs) }, title: fill(W.resetsAt, { clock: AD.model.clock(reset, nowMs) }) },
      el('time', { dataset: { reset, resetTemplate: W.resetsIn, resetDone: W.resetting }, text: fill(W.resetsIn, { countdown: AD.model.countdownText(secs) }) }));
  }

  function passedNote(reset, nowMs) {
    return el('p', { class: 'limit-note is-good', title: fill(G.resetSinceTip, { clock: AD.model.clock(reset, nowMs) }) },
      AD.dom.svgIcon('spark'), G.resetSince);
  }

  /** A limit without a share to draw (an amount, a disabled limit, nothing reported): its words, and an empty slot
   * where the bar would be so the cells line up. */
  function plainLimit(text, value, note, state) {
    return el('div', { class: 'limit', dataset: { state, severity: state === 'amount' ? null : 'unknown' } },
      el('div', 'limit-head', label(text), el('span', { class: 'limit-value', text: value })),
      el('span', { class: 'agy-no-meter', 'aria-hidden': 'true' }), note);
  }

  /** One limit of a reading: what is left (or used, as the page shows usage), its bar and when it resets. */
  function limitNode(bucket, reading, ctx) {
    const text = windowLabel(bucket);
    const passed = resetPassed(bucket, reading, ctx.nowMs);
    if (bucket.disabled) return plainLimit(text, G.notAvailable, null, 'disabled');
    let left = isNum(bucket.remaining) ? Math.min(100, Math.max(0, bucket.remaining * 100)) : null;
    if (left === null && isNum(bucket.amount)) {
      if (passed) return plainLimit(text, '—', passedNote(bucket.resets_at, ctx.nowMs), 'missing');
      return plainLimit(text, fill(G.amountLeft, { amount: bucket.amount.toLocaleString('en-US') }), resetNote(bucket.resets_at, ctx.nowMs), 'amount');
    }
    if (left === null && !passed) return plainLimit(text, '—', resetNote(bucket.resets_at, ctx.nowMs), 'missing');
    if (passed) left = 100;
    const shown = Math.round(left);
    const win = { id: bucket.id, kind: bucket.window === '5h' ? 'session' : (bucket.window === 'weekly' ? 'weekly' : 'other'),
      label: text, used: 100 - shown, severity: band(left), reset: passed ? null : bucket.resets_at, started: true };
    const node = AD.meter.limit(win, { last_good_at: reading.at }, { nowMs: ctx.nowMs, display: ctx.display, threshold: null });
    if (passed) node.appendChild(passedNote(bucket.resets_at, ctx.nowMs));
    return node;
  }

  /** A column another group fills but this one has no limit for. */
  function missingLimit(window) {
    return el('div', { class: ['limit', 'is-placeholder'], dataset: { state: 'missing', severity: 'unknown' } },
      el('div', 'limit-head', label(G.windows[window] || window), el('span', { class: 'limit-note', text: G.notReported })));
  }

  /** A model group: its name (agy's own words; its description as the tooltip), then its 5-hour and weekly limits
   * side by side, then any other limit it has. */
  function groupNode(group, reading, columns, ctx) {
    const buckets = Array.isArray(group.buckets) ? group.buckets : [];
    const used = new Set();
    const cells = columns.map((window) => {
      const bucket = buckets.find((b) => b.window === window);
      if (!bucket) return missingLimit(window);
      used.add(bucket);
      return limitNode(bucket, reading, ctx);
    });
    const others = buckets.filter((b) => !used.has(b)).map((b) => limitNode(b, reading, ctx));
    return el('div', { class: 'agy-group', role: 'group', 'aria-label': group.name },
      el('p', { class: 'agy-group-name', title: group.description || null, text: group.name }),
      el('div', 'agy-limits', cells, others));
  }

  function usageCell(account, ctx) {
    const reading = account.usage;
    const groups = reading && Array.isArray(reading.groups) ? reading.groups : [];
    if (!groups.length) {
      if (account.active && ctx.reading) return el('p', { class: 'row-note', text: G.reading });
      return el('p', { class: 'row-note', text: account.active ? G.noReading : G.noReadingOther });
    }
    const columns = WINDOWS.filter((w) => groups.some((g) => (g.buckets || []).some((b) => b.window === w)));
    return el('div', 'agy-usage', groups.map((group) => groupNode(group, reading, columns, ctx)));
  }

  // ---- A row ------------------------------------------------------------------------------------------------

  /** The row's one button: Save Account (not saved yet), Sign In Again (its sign-in stopped working, or its saved
   * sign-in is missing), nothing for the account agy uses, else Switch. */
  function primaryAction(account, state, ctx) {
    if (account.saved === false) return { label: G.save, run: () => capture(), disabled: !!ctx.reason || ctx.busy, reason: ctx.reason };
    const reauth = (account.error || {}).code === 'reauth_required';
    if (reauth || (!account.switchable && !account.active)) {
      return { label: STRINGS.accounts.signIn, run: () => add(account), disabled: !!ctx.addReason || ctx.busy, reason: ctx.addReason };
    }
    if (account.active) return null;
    const target = state.pending && state.pending.account === account.id;
    return { label: target ? STRINGS.shell.switching : STRINGS.accounts.switch, run: () => switchTo(account),
      disabled: !!ctx.reason || ctx.busy, reason: ctx.reason || (ctx.running ? G.switchWaits : null) };
  }

  /** `held`: why neither can start now (an Add is ending: the service refuses both until it has), or null. */
  function menuItems(account, held) {
    const M = STRINGS.accounts.menu;
    return [
      { label: M.rename, icon: 'pencil', disabled: !!held, title: held || undefined, run: () => rename(account) },
      'separator',
      { label: M.remove, icon: 'trash', danger: true, disabled: !!held, title: held || undefined, run: () => remove(account) },
    ];
  }

  function badges(account) {
    const list = [];
    if (account.active) list.push(AD.meter.badge('good', STRINGS.chip.current, { icon: null, dot: true }));
    if (account.saved === false) list.push(AD.meter.badge('neutral', STRINGS.chip.notSaved));
    return list;
  }

  /** Under the name: a sign-in that stopped working or a usage read that failed, plan and e-mail, why a saved
   * account can't be switched to, and how old the reading is (always: readings are not live). */
  function detailLines(account, ctx) {
    const lines = [];
    const error = account.error;
    if (error && error.code === 'reauth_required') {
      lines.push(el('p', { class: 'row-status', dataset: { kind: 'signin' }, title: mask(error.message) || null },
        AD.dom.svgIcon('warning'), el('span', { text: G.needsSignin })));
    }
    const meta = [account.plan, account.email ? mask(account.email) : null].filter(Boolean).join(' · ');
    if (meta) lines.push(el('p', { class: 'row-sub', text: meta, title: meta }));
    if (error && error.code !== 'reauth_required' && error.message) {
      const since = isNum(error.since) ? fill(G.since, { clock: AD.model.clock(error.since, ctx.nowMs) }) : null;
      lines.push(el('p', { class: 'row-status', dataset: { kind: 'agy-error' }, title: since },
        AD.dom.svgIcon('warning'), el('span', { text: fill(G.readError, { message: mask(error.message) }) })));
    }
    if (account.saved !== false && !account.switchable && !account.active && !(error && error.code === 'reauth_required')) {
      lines.push(el('p', { class: 'row-note', text: G.missing }));
    }
    const at = account.usage && account.usage.at;
    if (isNum(at)) {
      lines.push(el('p', { class: 'row-note', dataset: { ago: at, agoTemplate: G.read }, text: fill(G.read, { age: AD.format.ago(at, ctx.nowMs) }) }));
    }
    return lines;
  }

  function row(account, state, ctx) {
    const nameId = `row-agy-${account.id}-name`;
    const action = primaryAction(account, state, ctx);
    const actions = [];
    if (action) {
      actions.push(AD.dom.button(action.label, action.run, { small: true, disabled: action.disabled,
        title: action.reason || undefined, className: 'row-action',
        attrs: { 'data-primary': '', 'data-keep': `primary-agy-${account.id}`, 'aria-describedby': nameId } }));
    }
    if (account.saved !== false) actions.push(AD.rows.more(account, menuItems(account, ctx.ending)));
    return el('li', {
      class: ['row', account.active && 'is-current', account.active && ctx.reading && 'is-refreshing'],
      id: `row-agy-${account.id}`, tabindex: -1, 'aria-labelledby': nameId, dataset: { id: account.id },
    },
    el('div', 'row-id',
      AD.dom.avatar(account.label, { active: account.active }),
      el('div', 'row-text',
        el('div', 'row-title', el('h3', { class: 'row-name', id: nameId, text: mask(account.label) }), badges(account)),
        detailLines(account, ctx))),
    el('div', 'row-limits', usageCell(account, ctx)),
    el('div', 'row-actions', actions));
  }

  /** What a row shows depends on: the account, which resets have passed, and the section's conditions. */
  function rowSig(account, state, ctx) {
    const groups = (account.usage && account.usage.groups) || [];
    const passed = groups.flatMap((g) => (g.buckets || []).filter((b) => resetPassed(b, account.usage, ctx.nowMs)).map((b) => b.id));
    return JSON.stringify([AD.accounts.signature(account), passed, ctx.display, AD.dom.hideEmails(), ctx.reason, ctx.addReason,
      ctx.ending, ctx.busy, ctx.running, account.active && ctx.reading, state.pending ? state.pending.account || true : null]);
  }

  // ---- Header, notes, footer --------------------------------------------------------------------------

  function cooling(b, nowMs) {
    const next = b.refresh && b.refresh.next_allowed;
    return !(b.refresh && b.refresh.busy) && isNum(next) && next > nowMs / 1000;
  }

  /** Refresh Usage, and while it can't be used yet, "Again in 42s" beside it. It stays focusable (aria-disabled)
   * while it reads or waits, so the focus never drops out of the section. */
  function refreshControls(state, b, nowMs) {
    const busy = !!(b.refresh && b.refresh.busy);
    const wait = cooling(b, nowMs);
    const reason = refreshReason(state, b);
    const next = wait ? b.refresh.next_allowed : null;
    const title = reason || (wait ? fill(G.againTip, { clock: AD.model.clock(next, nowMs) }) : G.refreshTip);
    const button = AD.dom.button(busy ? G.refreshing : G.refresh, refresh, {
      icon: 'refresh', title, className: ['agy-refresh', busy && 'is-busy'].filter(Boolean).join(' '),
      attrs: { 'data-keep': 'agy-refresh', 'aria-disabled': busy || wait || reason ? 'true' : null, 'aria-busy': busy ? 'true' : null,
        'aria-describedby': wait ? 'agy-again' : null },
    });
    const again = wait ? el('span', { class: 'agy-again', id: 'agy-again',
      dataset: { reset: next, resetTemplate: G.again, resetDone: '' },
      text: fill(G.again, { countdown: AD.model.countdownText(next - nowMs / 1000) }) }) : null;
    return [again, button];
  }

  function head(state, b, accounts, nowMs) {
    const count = accounts.length === 1 ? STRINGS.accounts.countOne : fill(STRINGS.accounts.count, { n: accounts.length });
    const current = accounts.find((a) => a.active);
    const reason = blockedReason(state, b);
    const busy = !!state.pending || !!state.login;
    const toAdd = addReason(state, b);
    return el('header', 'section-head',
      el('div', null, el('h2', { id: 'antigravity-title', class: 'section-title', text: G.title }),
        el('p', { class: 'section-sub', text: accounts.length ? `${count} · ${G.sub}` : G.sub })),
      el('div', 'section-actions',
        refreshControls(state, b, nowMs),
        current && current.saved === false ? AD.dom.button(G.capture, () => capture(),
          { disabled: !!reason || busy, title: reason || undefined, attrs: { 'data-keep': 'agy-capture' } }) : null,
        AD.dom.button(G.add, () => add(), { icon: 'plus', disabled: !!toAdd || busy, title: toAdd || undefined,
          attrs: { 'data-keep': 'agy-add' } })));
  }

  function note({ kind, icon, level, title, message, actions, role }) {
    return el('div', { class: 'banner is-compact', role: role || null, dataset: { kind, level } },
      el('span', 'banner-icon', AD.dom.svgIcon(icon)),
      el('div', 'banner-text', title ? el('p', { class: 'banner-title', text: title }) : null,
        message ? el('p', 'banner-message', message) : null),
      actions && actions.length ? el('div', 'banner-actions', actions) : null);
  }

  /** The notes above the table: a switch left unfinished, an Add under way, a store AccountDeck can't use, agy
   * missing, agy running. */
  function notes(state, b, accounts, nowMs) {
    const items = [];
    if (b.recovery) {
      items.push(note({ kind: 'agy-recovery', icon: 'warning', level: 'warning', role: 'status', title: G.recoverTitle,
        message: recoveryText(b),
        actions: [AD.dom.button(STRINGS.button.recover, () => recover(), { small: true, attrs: { 'data-keep': 'agy-recover' } })] }));
    }
    if (signingIn(b)) {
      const e = b.enrollment;
      const again = e.account ? accounts.find((a) => a.id === e.account) : null;
      const left = isNum(e.expires) && e.expires > nowMs / 1000
        ? el('time', { class: 'agy-left', dataset: { reset: e.expires, resetTemplate: G.timeLeft, resetDone: '' },
          text: fill(G.timeLeft, { countdown: AD.model.countdownText(e.expires - nowMs / 1000) }) }) : null;
      items.push(note({ kind: 'agy-enrollment', icon: 'hourglass', level: 'info', role: 'status',
        title: again ? fill(G.addingAgain, { label: mask(again.label) }) : G.adding,
        message: [mask(e.message || G.addingDefault), left ? ' ' : null, left],
        actions: [AD.dom.button(STRINGS.button.cancel, cancelAdd, { small: true, attrs: { 'data-keep': 'agy-cancel-add' } })] }));
    }
    if (restoring(b)) {
      // No button: the service finishes by itself once agy has closed, and says here what it waits for.
      items.push(note({ kind: 'agy-restoring', icon: 'hourglass', level: 'info', role: 'status',
        title: G.endingTitle, message: mask(b.enrollment.message || G.endingDefault) }));
    }
    if (storeState(b) === 'unknown') {
      items.push(note({ kind: 'agy-store', icon: 'lock', level: 'info', title: G.storeTitle,
        message: (b.store && b.store.message) || G.storeText }));
    }
    if (!b.installed && accounts.length) {
      items.push(note({ kind: 'agy-missing', icon: 'info', level: 'info', title: G.notFoundTitle, message: G.notFoundText }));
    }
    const running = consumers(b).length;
    if (running) {
      items.push(note({ kind: 'agy-running', icon: 'terminal', level: 'info',
        title: running === 1 ? G.runningOne : fill(G.running, { n: running }),
        message: running === 1 ? G.runningText : G.runningTextMany }));
    }
    return items;
  }

  /** Quiet lines below the table: how usage is read, and, on a Mac where T3 Code has its own Antigravity folder
   * (`t3_code`), that T3 Code keeps a sign-in of its own that a switch here leaves alone. */
  function foot(state, b) {
    const onOpen = !(state.settings && state.settings.agy_usage_on_open === false);
    return el('div', 'agy-foot',
      el('p', 'agy-foot-line', AD.dom.svgIcon('refresh'), el('span', { text: onOpen ? G.howRead : G.howReadOff })),
      b.t3_code === true ? el('p', 'agy-foot-line', AD.dom.svgIcon('info'), el('span', { text: G.t3 })) : null);
  }

  // ---- The end of an Add, said once ------------------------------------------------------------------------

  function loadEnded() {
    try {
      return window.localStorage.getItem(ENDED_KEY);
    } catch (err) {
      return null;
    }
  }

  /** An Add that ended (saved, cancelled, timed out, failed) is said once, as a toast; this browser remembers it. */
  function sayEnded(state, b) {
    const e = b && b.enrollment;
    if (!e || !G.ended[e.phase]) return;
    const key = `${e.started}|${e.phase}`;
    if (key === endedSeen) return;
    let told = false;
    if (quiet && quiet.started === e.started) {
      // Cancelled here, and the cancel's report is on screen. An Add that ended at once: the report said so, in
      // these words. One that had to wait for agy to close: the report only said what it waited for, and this is
      // the first word of the end. While the cancel still runs its report has not come: decided on a later state.
      if (state.pending && state.pending.id === quiet.op) return;
      told = quiet.summary !== null && quiet.summary === e.message;
    }
    endedSeen = key;
    try { window.localStorage.setItem(ENDED_KEY, key); } catch (err) { /* said once on this page */ }
    if (told) return;
    const [title, fallback, level] = G.ended[e.phase];
    AD.dom.toast(mask(e.message || fill(fallback, { email: e.email || '' })), { title, level, sticky: level === 'critical' ? undefined : false });
  }

  /** The end of a read the page saw start is announced (the reading itself is on screen). */
  function sayRead(b) {
    const reading = !!(b && b.refresh && b.refresh.busy);
    if (wasReading && !reading && b) {
      const active = accountsOf(b).find((a) => a.active);
      if (active && !active.error) AD.dom.announce(fill(G.read, { age: AD.format.ago(active.usage && active.usage.at, AD.app.clockNow()) }));
    }
    wasReading = reading;
  }

  // ---- The section ------------------------------------------------------------------------------------------

  function frame(host) {
    if (!parts || parts.body.parentElement !== host) {
      parts = { head: el('header'), notes: el('div', 'group-notes'), body: el('div'), foot: el('div') };
      host.replaceChildren(parts.head, parts.notes, parts.body, parts.foot);
      sigs.head = sigs.notes = sigs.body = sigs.foot = null;
    }
  }

  function replaceSlot(name, next) {
    AD.dom.swap(parts[name], next);
    parts[name] = next;
  }

  function render(state, nowMs) {
    const host = document.getElementById('antigravity-accounts');
    if (!host) return;
    const b = block(state);
    if (quiet && state.report && state.report.op === quiet.op) quiet.summary = state.report.summary || '';
    sayEnded(state, b);
    sayRead(b);
    // The Recover sheet is built from one state: when what it shows changes under it (agy's sign-in, an agy that
    // started, the recovery itself), it is built again from this one, or closed once nothing is left to recover.
    if (AD.dom.modalView() === 'agy-recover' && !recovering) {
      if (!b || !b.recovery) AD.dom.closeModal();
      else if (recoverSig(state, b) !== recoverShown) recover(state);
    }
    host.hidden = !shown(state);
    if (host.hidden) {
      if (parts) host.replaceChildren();
      parts = null;
      return;
    }
    frame(host);
    const accounts = accountsOf(b);
    const consumerCount = consumers(b).length;
    const wait = cooling(b, nowMs);

    const headSig = JSON.stringify([accounts.length, accounts.map((a) => [a.active, a.saved]), blockedReason(state, b),
      addReason(state, b), refreshReason(state, b), b.refresh, wait, !!state.pending, !!state.login]);
    if (headSig !== sigs.head) {
      sigs.head = headSig;
      replaceSlot('head', head(state, b, accounts, nowMs));
    }
    const notesSig = JSON.stringify([b.recovery, addOpen(b) ? b.enrollment : null,
      b.store, b.installed, accounts.length > 0, consumerCount, accounts.map((a) => [a.id, a.label]), AD.dom.hideEmails()]);
    if (notesSig !== sigs.notes) {
      sigs.notes = notesSig;
      replaceSlot('notes', el('div', 'group-notes', notes(state, b, accounts, nowMs)));
    }
    const footSig = JSON.stringify([state.settings && state.settings.agy_usage_on_open, b.t3_code === true]);
    if (footSig !== sigs.foot) {
      sigs.foot = footSig;
      replaceSlot('foot', foot(state, b));
    }

    if (!accounts.length) {
      // Nobody saved or signed in: the empty state, unless the store can't be read (its note says why).
      const bodySig = `empty|${storeState(b) === 'unknown'}`;
      if (bodySig !== sigs.body) {
        sigs.body = bodySig;
        parts.body.replaceChildren(storeState(b) === 'unknown' ? [] : el('div', 'card empty-state',
          el('span', 'empty-icon', AD.dom.svgIcon('antigravity')),
          el('h3', { class: 'empty-title', text: G.emptyTitle }),
          el('p', { class: 'empty-text', text: G.emptyText })));
      }
      return;
    }
    let list = parts.body.querySelector('.rows');
    if (!list) {
      list = el('ol', { class: 'rows', 'aria-labelledby': 'antigravity-title' });
      AD.rows.bindKeys(list);
      parts.body.replaceChildren(el('div', 'table',
        el('div', { class: 'table-head', 'aria-hidden': 'true' },
          el('span', { text: STRINGS.accounts.colAccount }), el('span', { text: G.usage }), el('span')), list));
      sigs.body = 'table';
    }
    // The service's order (the saved order, then the signed-in account when it is not saved): rows never jump
    // when a reading arrives.
    AD.rows.patchRows(list, accounts, state, {
      nowMs, display: AD.accounts.display(state), reason: blockedReason(state, b), addReason: addReason(state, b),
      ending: restoring(b) ? G.addEnding : null,
      busy: !!state.pending || !!state.login, running: consumerCount, reading: !!(b.refresh && b.refresh.busy),
      build: row, signature: rowSig,
    });
  }

  AD.antigravity = { render, shown, waiting, add, capture, rename, remove, refresh, recover, notice };
})();
