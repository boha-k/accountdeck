/* AccountDeck 2 — limit resets: what a Claude Code, Claude Desktop or Codex account holds (accounts[].resets and its
 * reset_credits rows), and Use a Limit Reset….
 *   The count    — a chip on the account's row and on the In use card ("2 limit resets"), quiet while none can be
 *                  used now; while a use is in doubt (resets.doubt), a sentence stands in its place.
 *   The details  — the account's Limit Resets sheet: one line per row, a row's reason when it can't be used, the
 *                  account's note, when they were last read, and Use a Limit Reset…. The dashboard has no other view
 *                  of one account, so the chip and the row's More menu open this sheet.
 *   The action   — reset-plan (nothing reaches a provider), its sheet, then use-reset, sent only from that sheet's Use
 *                  Limit Reset button for the one account and row chosen there. A refusal stays inside the sheet; a
 *                  stale plan is asked for again and shown again, once, and never sent again by itself. The progress
 *                  and the report are switch.js's views (kind "use-reset"); this file words the report.
 * Every text from the service is shown as text, through the e-mail mask. Antigravity has no resets: its accounts never
 * reach this file (they are not in state.accounts, and block() refuses any other app). */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;
  const R = STRINGS.resets;

  const PROVIDERS = new Set(['claude', 'claude_cli', 'codex']);
  const KEY = /^[A-Za-z0-9_-]{1,64}$/;
  // The opening words of a use-reset report's summary (CONTRACT.md): nothing was used, or nobody knows yet.
  const NOTHING_USED = /^No limit reset was used\b/i;
  const NOT_KNOWN = /^It is not known whether\b/i;
  // A plan sheet can be drawn under the pointer: by the second click of a double click on the button that opened it,
  // or on Use Limit Reset of a plan that had gone stale. A click this soon after it was drawn is not a yes to it.
  const ARM_MS = 700;
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);
  const spacer = () => el('span', { class: 'spacer', 'aria-hidden': 'true' });

  let shown = null;          // {id, sig}: the account whose Limit Resets sheet is open, and what it was built from

  // ---- What the state says -----------------------------------------------------------------------

  /** The account's resets block, or null: AccountDeck does not read this account's resets (the setting is off, the
   * app has none, no read has worked yet), or the account is not one of the three apps that have them. */
  function block(account) {
    if (!account || !PROVIDERS.has(account.provider) || AD.accounts.frozen(account)) return null;
    const b = account.resets;
    return b && typeof b === 'object' && !Array.isArray(b) ? b : null;
  }

  /** The account's rows (reset_credits) that hold at least one reset. */
  function rows(account) {
    return (account && Array.isArray(account.reset_credits) ? account.reset_credits : [])
      .filter((row) => row && typeof row === 'object' && Number.isInteger(row.count) && row.count >= 1);
  }

  function available(b) {
    return b && Number.isInteger(b.available) && b.available > 0 ? b.available : 0;
  }

  function doubt(b) {
    return b && b.doubt && typeof b.doubt === 'object' && isNum(b.doubt.since) ? b.doubt : null;
  }

  function usable(row) {
    return !!row && row.usable === true;
  }

  function countText(n) {
    return n === 1 ? R.one : fill(R.many, { n });
  }

  /** "2 limit resets · first use by Nov 28": what the chip says to a tooltip and to assistive technology. */
  function summary(account, b) {
    const n = available(b);
    const count = countText(n);
    const list = rows(account);
    if (!list.length) return count;
    const dates = list.map((row) => row.expires).filter(isNum);
    if (dates.length) return fill(R.tipBy, { count, date: AD.format.dateText(Math.min(...dates)) });
    return fill(n === 1 ? R.tipNeverOne : R.tipNever, { count });
  }

  function doubtText(d, nowMs) {
    return fill(R.doubt, { clock: AD.model.clock(d.since, nowMs) });
  }

  /** Why Use a Limit Reset… is off now, or null: the page is read-only, or the service says no (`can_use` false,
   * `blocked` its reason). Without `blocked` it borrows the doubt's sentence, a row's own reason when no row can be
   * used, or a plain sentence. */
  function useOff(account, b, readonly, nowMs) {
    if (readonly) return readonly;
    if (b.can_use === true) return null;
    if (typeof b.blocked === 'string' && b.blocked) return mask(b.blocked);
    const d = doubt(b);
    if (d) return doubtText(d, nowMs);
    const list = rows(account);
    const why = list.some(usable) ? null : list.map((row) => row.reason).find((text) => typeof text === 'string' && text);
    return why ? mask(why) : R.unavailable;
  }

  /** The words of the service's own refusal (or the read-only page's), which the Limit Resets sheet says under the
   * rows; a reason borrowed from a row or the doubt is already on the sheet. */
  function ownRefusal(b, readonly) {
    if (readonly) return readonly;
    return b.can_use !== true && typeof b.blocked === 'string' && b.blocked ? mask(b.blocked) : null;
  }

  function readonlyOf(state) {
    return state && state.read_only ? STRINGS.accounts.readOnlyOther : null;
  }

  function accountOf(state, id) {
    return ((state && state.accounts) || []).find((a) => a && a.id === id && PROVIDERS.has(a.provider)) || null;
  }

  // ---- The count ---------------------------------------------------------------------------------

  /** The chip of an account that holds limit resets, a button that opens its Limit Resets sheet; `where` keeps the
   * row's and the card's apart. Accented while one can be used now (a usable row, and the service offers the use),
   * quiet otherwise. Nothing without resets, with none, or while a use is in doubt (doubtLine stands there instead). */
  function chip(account, where = 'row') {
    const b = block(account);
    const n = available(b);
    if (!n || doubt(b)) return null;
    const words = summary(account, b);
    const node = el('button', {
      type: 'button', class: ['badge', 'reset-chip', b.can_use === true && rows(account).some(usable) && 'is-ready'],
      title: words, 'aria-label': `${words}. ${fill(R.chipTip, { label: mask(account.label) })}`, 'aria-haspopup': 'dialog',
      'data-keep': `resets-${where}-${account.id}`,
    }, AD.dom.svgIcon('reset'), el('span', { text: countText(n) }));
    node.addEventListener('click', () => details(account.id));
    return node;
  }

  /** While a use is in doubt: "A limit reset may have been used at 2:02 PM. AccountDeck is reading this account
   * again." in the count's place. */
  function doubtLine(account, nowMs, className = 'row-status') {
    const d = doubt(block(account));
    if (!d) return null;
    return el('p', { class: [className, 'reset-doubt'] }, AD.dom.svgIcon('hourglass'), el('span', { text: doubtText(d, nowMs) }));
  }

  /** The In use card's line: the chip, or the doubt's sentence. */
  function cardLine(account, nowMs) {
    const node = chip(account, 'card');
    if (node) return el('p', 'who-resets', node);
    return doubtLine(account, nowMs, 'who-resets');
  }

  /** The row's More menu: Limit Resets… while AccountDeck reads this account's resets (also with none: the sheet says
   * so, and shows the account's note), and Use a Limit Reset… while it holds any, off with the service's reason
   * (said again under it: a menu skips an item that is off). */
  function menuItems(account, readonly, nowMs) {
    const b = block(account);
    if (!b) return [];
    const items = [{ label: R.menu, icon: 'info', run: () => details(account.id) }];
    if (available(b)) {
      const off = useOff(account, b, readonly, nowMs);
      items.push({ label: R.use, icon: 'reset', disabled: !!off, title: off || undefined, run: () => openPlan(account.id) });
      if (off) items.push({ note: off });
    }
    return items;
  }

  // ---- A row's words ---------------------------------------------------------------------------

  /** "Weekly reset ×2" (the provider's own name, or ours). */
  function rowTitle(row) {
    const title = typeof row.title === 'string' && row.title ? mask(row.title) : R.unnamed;
    return row.count > 1 ? fill(R.times, { title, count: row.count }) : title;
  }

  /** ["refills the weekly limits", "use by Nov 28"]: what a row refills (when known) and its use-by. */
  function rowFacts(row) {
    return [typeof row.restores === 'string' && row.restores ? fill(R.refills, { what: mask(row.restores) }) : null,
      isNum(row.expires) ? fill(R.useBy, { date: AD.format.dateText(row.expires) }) : R.never].filter(Boolean);
  }

  function sentence(parts) {
    const text = parts.join(' · ');
    return text ? text[0].toUpperCase() + text.slice(1) : text;
  }

  /** The account as both sheets show it: its mark, its name, and app · plan · e-mail. */
  function accountLine(provider, account) {
    const meta = [STRINGS.apps[provider], AD.format.capitalised(account.plan), account.email ? mask(account.email) : null];
    const mark = provider === 'claude_cli' ? AD.dom.avatar(account.label, { size: 'large' }) : AD.dom.svgIcon(provider, { size: 20 });
    return el('div', 'plan-target', mark,
      el('div', 'plan-target-text', el('p', { class: 'plan-target-name', text: mask(account.label) }),
        el('p', { class: 'plan-target-meta', text: meta.filter(Boolean).join(' · ') })));
  }

  function note(icon, text, className = 'reset-note') {
    return el('p', className, AD.dom.svgIcon(icon), el('span', { text }));
  }

  // ---- The account's Limit Resets sheet ------------------------------------------------------------

  function resetLine(row) {
    const ok = usable(row);
    return el('li', { class: 'reset-line', dataset: { usable: ok ? 'true' : 'false' } }, AD.dom.svgIcon('reset'),
      el('div', 'reset-line-text',
        el('p', null, el('strong', { text: rowTitle(row) }), el('span', { text: ` · ${rowFacts(row).join(' · ')}` })),
        !ok && typeof row.reason === 'string' && row.reason ? el('p', { class: 'reset-reason', text: mask(row.reason) }) : null));
  }

  function detailsBody(account, b, readonly, nowMs) {
    const n = available(b);
    const list = rows(account);
    const d = doubt(b);
    const part = [];
    if (!n) part.push(el('p', { class: 'reset-none', text: isNum(b.checked) ? R.none : R.unknown }));
    else if (list.length) part.push(el('ul', 'reset-lines', list.map(resetLine)));
    else part.push(el('p', { text: countText(n) }));
    if (typeof b.note === 'string' && b.note) part.push(note('info', mask(b.note)));
    const off = n ? ownRefusal(b, readonly) : null;
    const said = list.some((row) => !usable(row) && typeof row.reason === 'string' && mask(row.reason) === off);
    if (off && !said) part.push(note('info', off));
    part.push(isNum(b.checked)
      ? el('p', { class: 'reset-checked', dataset: { ago: b.checked, agoTemplate: R.checked },
        text: fill(R.checked, { age: AD.format.ago(b.checked, nowMs) }) })
      : el('p', { class: 'reset-checked', text: R.notChecked }));
    return el('div', 'plan',
      accountLine(account.provider, account),
      d ? note('hourglass', doubtText(d, nowMs), 'plan-reconfirm reset-doubt') : null,
      el('section', { class: 'plan-section', 'aria-labelledby': 'resets-heading' },
        el('h3', { class: 'plan-heading', id: 'resets-heading', text: R.heading }), part));
  }

  function detailsActions(account, b, readonly, nowMs) {
    const done = AD.dom.button(STRINGS.button.done, () => AD.dom.closeModal(),
      { kind: 'primary', dataset: { choice: 'done' }, attrs: { 'data-autofocus': '', 'data-keep': 'resets-done' } });
    if (!available(b)) return [done];
    const off = useOff(account, b, readonly, nowMs);
    return [AD.dom.button(R.use, () => openPlan(account.id), { disabled: !!off, title: off || undefined,
      dataset: { choice: 'use' }, attrs: { 'data-keep': 'resets-use' } }), spacer(), done];
  }

  function detailsSig(state, account) {
    return JSON.stringify([account.label, account.email, account.plan, account.resets, account.reset_credits,
      readonlyOf(state), AD.dom.hideEmails()]);
  }

  /** Open the Limit Resets sheet of account `id` (the chip, the row's menu). */
  function details(id) {
    const state = AD.app.state;
    const account = accountOf(state, id);
    const b = block(account);
    if (!b) return;
    const nowMs = AD.app.clockNow();
    const readonly = readonlyOf(state);
    shown = { id, sig: detailsSig(state, account) };
    AD.dom.openModal({ title: fill(R.sheetTitle, { label: mask(account.label) }), view: 'resets',
      body: detailsBody(account, b, readonly, nowMs), actions: detailsActions(account, b, readonly, nowMs) });
  }

  /** A new state: the open Limit Resets sheet follows its account, and closes once AccountDeck no longer reads that
   * account's resets (or the account is gone). Rebuilt only when what it shows changed; focus stays on its control. */
  function render(state) {
    if (!shown || AD.dom.modalView() !== 'resets') {
      if (AD.dom.modalView() !== 'resets') shown = null;
      return;
    }
    const account = accountOf(state, shown.id);
    const b = block(account);
    if (!b) {
      shown = null;
      AD.dom.closeModal();
      return;
    }
    const sig = detailsSig(state, account);
    if (sig === shown.sig) return;
    shown.sig = sig;
    const nowMs = AD.app.clockNow();
    const readonly = readonlyOf(state);
    const active = document.activeElement;
    const key = active && active.closest && active.closest('#modal') ? active.getAttribute('data-keep') : null;
    document.getElementById('modal-title').textContent = fill(R.sheetTitle, { label: mask(account.label) });
    AD.dom.setModalBody(detailsBody(account, b, readonly, nowMs));
    AD.dom.setModalActions(detailsActions(account, b, readonly, nowMs));
    const again = key ? document.querySelector(`#modal [data-keep="${key}"]`) : null;
    if (again && !again.disabled && document.activeElement !== again) again.focus({ preventScroll: true });
  }

  // ---- Use a Limit Reset… ----------------------------------------------------------------------------

  function texts(list) {
    return (Array.isArray(list) ? list : []).map((entry) => (entry && typeof entry === 'object' ? entry.message : entry))
      .filter((text) => typeof text === 'string' && text);
  }

  function item(icon, text) {
    return el('li', 'plan-item', AD.dom.svgIcon(icon), el('span', { text: mask(text) }));
  }

  function choiceText(choice) {
    const off = !usable(choice);
    return el('span', 'reset-choice-text',
      el('span', 'reset-choice-title', rowTitle(choice)),
      el('span', { class: 'reset-choice-hint', text: sentence(rowFacts(choice)) }),
      off && typeof choice.reason === 'string' && choice.reason
        ? el('span', { class: 'reset-choice-reason', text: mask(choice.reason) }) : null);
  }

  /** The row selected at first: the one chosen before a stale plan (still there and usable), else the plan's default
   * (when usable), else none. A row that can't be used is never selected. */
  function firstChoice(plan, choices, prefer) {
    const ok = (key) => key && choices.some((c) => c.key === key && usable(c));
    if (ok(prefer)) return prefer;
    if (ok(plan.default)) return plan.default;
    return null;
  }

  /** The sheet of a reset plan. options: error (why the last try failed), changed (this is the plan asked for again
   * after a stale one), prefer (the row chosen then), retried (a stale refusal now is said, not asked again). */
  function showPlan(plan, options = {}) {
    const choices = (Array.isArray(plan.choices) ? plan.choices : [])
      .filter((c) => c && typeof c.key === 'string' && KEY.test(c.key) && Number.isInteger(c.count) && c.count >= 1);
    const first = choices.length === 1 ? (usable(choices[0]) ? choices[0].key : null) : firstChoice(plan, choices, options.prefer);
    const error = el('p', { class: 'modal-error', role: 'alert', hidden: !options.error, text: options.error ? mask(options.error) : '' });
    const parts = [];
    if (options.changed) {
      parts.push(el('p', { class: 'plan-reconfirm', role: 'status' }, AD.dom.svgIcon('info'), el('span', { text: R.changed })));
    }
    parts.push(error, accountLine(plan.provider, plan.account));
    let group = null;
    if (choices.length > 1) {
      group = el('div', { class: 'reset-choices', role: 'radiogroup', 'aria-label': R.choose },
        choices.map((c) => el('label', { class: 'reset-choice', dataset: { usable: usable(c) ? 'true' : 'false' } },
          el('input', { type: 'radio', name: 'reset-choice', value: c.key, checked: c.key === first, disabled: !usable(c) }),
          choiceText(c))));
      parts.push(group);
    } else if (choices.length === 1) {
      parts.push(el('div', 'reset-choices', el('div', { class: 'reset-choice is-single', dataset: { usable: usable(choices[0]) ? 'true' : 'false' } },
        AD.dom.svgIcon('reset'), choiceText(choices[0]))));
    }
    const effects = texts(plan.effects).map((text) => item('info', text));
    if (effects.length) parts.push(el('section', 'plan-section', el('h3', { class: 'plan-heading', text: STRINGS.modal.will }),
      el('ul', 'plan-list', effects)));
    const warnings = texts(plan.warnings).map((text) => item('warning', text));
    if (warnings.length) parts.push(el('section', 'plan-section', el('h3', { class: 'plan-heading', text: STRINGS.modal.headsUp }),
      el('ul', 'plan-list', warnings)));
    const blockers = texts(plan.blockers);
    if (blockers.length) {
      parts.push(el('div', { class: 'plan-blockers', role: 'alert' }, el('p', { class: 'plan-blocked', text: STRINGS.modal.blocked }),
        blockers.map((text) => el('p', { text: mask(text) }))));
    }
    // Keep It is the default button and where focus starts: a reset that is used cannot be had back.
    const keep = AD.dom.button(R.keep, () => AD.dom.closeModal(),
      { kind: 'primary', dataset: { choice: 'keep' }, attrs: { 'data-autofocus': '' } });
    const actions = [keep];
    if (!blockers.length && choices.some(usable)) {
      const chosen = () => {
        if (!group) return first;
        const picked = group.querySelector('input:checked');
        return picked ? picked.value : null;
      };
      const drawn = performance.now();
      const use = AD.dom.button(R.confirm, (event) => {
        // Not the second click of a double click, and not a click on a sheet that has only just appeared.
        if ((event && event.detail > 1) || performance.now() - drawn < ARM_MS || use.dataset.spent === 'true') return undefined;
        return send(plan, chosen(), error, options, use);
      }, { disabled: !first, dataset: { choice: 'use' } });
      // `send` turns the button off for good (no answer, a plan stale twice): choosing another row leaves it off.
      if (group) group.addEventListener('change', () => { use.disabled = use.dataset.spent === 'true' || !chosen(); });
      actions.unshift(use);
    }
    AD.dom.openModal({ title: R.planTitle, view: 'reset-plan', body: el('div', 'plan', parts), actions });
  }

  /** reset-plan refused (or answered without a plan): the sheet says so, with the account when the page knows it. */
  function showRefusal(account, message) {
    AD.dom.openModal({ title: R.planTitle, view: 'reset-plan',
      body: el('div', 'plan', el('p', { class: 'modal-error', role: 'alert', text: mask(message) }),
        account ? accountLine(account.provider, account) : null),
      actions: [AD.dom.button(STRINGS.button.close, () => AD.dom.closeModal(),
        { kind: 'primary', dataset: { choice: 'close' }, attrs: { 'data-autofocus': '' } })] });
  }

  /** Use a Limit Reset… for account `id`: ask the service for a plan (it reads nothing new and sends nothing to a
   * provider) and show it. options as showPlan's. */
  async function openPlan(id, options = {}) {
    const account = accountOf(AD.app.state, id);
    let result;
    try {
      result = await AD.api.action('reset-plan', { id });
    } catch (err) {
      showRefusal(account, err.message);
      return;
    }
    const plan = result && result.plan;
    if (!plan || typeof plan !== 'object' || !plan.account || typeof plan.plan_id !== 'string') {
      showRefusal(account, STRINGS.connection.failed);
      return;
    }
    showPlan(plan, options);
  }

  /** Use Limit Reset: send use-reset once, for the plan and the row chosen. A stale plan is asked for again and shown
   * again once (the user says yes again), and only while this sheet is still open; any other refusal stays in the
   * sheet's error line. When AccountDeck gave no answer (the use may have started), or the plan went stale a second
   * time, this sheet sends nothing more: `use` is turned off, and the user looks at the account first. */
  async function send(plan, choice, error, options, use) {
    if (!choice) return;
    error.hidden = true;
    let result;
    try {
      result = await AD.api.action('use-reset', { id: plan.account.id, plan_id: plan.plan_id, choice });
    } catch (err) {
      const open = AD.dom.modalView() === 'reset-plan' && error.isConnected;
      if (err.code === 'stale_plan' && !options.retried) {
        if (open) await openPlan(plan.account.id, { changed: true, prefer: choice, retried: true });
        return;
      }
      const unanswered = !err.status;
      error.textContent = unanswered ? R.noAnswer : mask(err.message);
      error.hidden = false;
      if (use && (unanswered || err.code === 'stale_plan')) {
        use.dataset.spent = 'true';
        use.disabled = true;
      }
      if (open) error.scrollIntoView({ block: 'nearest' });
      return;
    }
    AD.switch.follow(result);
  }

  // ---- The report (kind "use-reset") -------------------------------------------------------------------

  /** What a use-reset report says happened: success, nothing (nothing was used: information, not damage), unknown
   * (sent, no answer: the account's doubt is open), failed (anything else), cancelled. */
  function outcome(report) {
    if (report.outcome === 'success') return 'success';
    if (report.outcome === 'cancelled') return 'cancelled';
    const said = String(report.summary || '');
    if (NOTHING_USED.test(said)) return 'nothing';
    if (NOT_KNOWN.test(said)) return 'unknown';
    const account = accountOf(AD.app.state, report.account);
    if (account && doubt(block(account))) return 'unknown';
    return 'failed';
  }

  const LOOKS = {
    success: { icon: 'check-circle', level: 'good' },
    nothing: { icon: 'info', level: 'info' },
    unknown: { icon: 'hourglass', level: 'info' },
    failed: { icon: 'warning', level: 'warning' },
    cancelled: { icon: 'info', level: 'info' },
  };

  /** The report's title, icon and banner level. */
  function reportLook(report) {
    const kind = outcome(report);
    return Object.assign({ kind, title: R.report[kind] }, LOOKS[kind]);
  }

  /** The report sheet's body: the service's summary first, as it is. Its error is a red alert only when something
   * failed; when nothing was used, or the outcome is not known yet, it is a quiet line. */
  function reportBody(report) {
    const kind = outcome(report);
    const parts = [];
    if (report.summary) parts.push(el('p', { class: 'plan-lead', text: mask(report.summary) }));
    const said = report.error && report.error.message ? AD.format.errorText(report.error.code, report.error.message) : null;
    if (said && said !== report.summary) {
      parts.push(kind === 'failed' ? el('p', { class: 'modal-error', role: 'alert', text: mask(said) })
        : el('p', { class: 'plan-muted', text: mask(said) }));
    }
    return el('div', 'report', parts);
  }

  // ---- The pool --------------------------------------------------------------------------------------

  /** The limit resets the terminal accounts hold, for the overview's total. */
  function total(accounts) {
    return (accounts || []).reduce((sum, account) => sum + available(block(account)), 0);
  }

  function totalText(n) {
    return n === 1 ? R.totalOne : fill(R.total, { n });
  }

  AD.resets = { chip, doubtLine, cardLine, menuItems, details, openPlan, render, reportLook, reportBody, total, totalText };
})();
