/* AccountDeck 2 — the Claude Code (terminal) accounts section. Every open terminal follows the
 * account in use; auto-switch moves them all to the next account before a limit runs out.
 * This file owns the section's header (sort and Add Account), its notes (Undo after an automatic
 * switch, read-only mode, an account being added), the table frame and the account actions
 * (add, rename, remove, leave out). rows.js draws the rows. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;
  const A = AD.accounts;
  const T = STRINGS.accounts;

  const SORT_KEY = 'ad2.terminalSort';
  const REFILL_MS = 2600;
  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);
  const sigs = { head: null, notes: null, columns: null };
  let parts = null;                              // {head, notes, body}: the section's three slots
  let picked = null;                             // the order picked on this page (outlives blocked storage)
  let refilledKeys = null;                       // "account|window" shown as refilled at the last render

  // ---- Actions -------------------------------------------------------------------------------------

  /** Run a terminal action; failures become a toast and resolve to null. */
  const action = (name, body = {}) => AD.api.action(name, Object.assign({ provider: 'claude_cli' }, body))
    .catch((error) => {
      AD.dom.toast(error.message, { level: 'critical' });
      return null;
    });

  /** Add an account, or sign `account` in again: a Terminal window opens for the sign-in, and the service
   * keeps a login for an account it already has as that account's new login. */
  function add(account) {
    const S = T.addSheet;
    const again = account && account.provider === 'claude_cli' ? account : null;
    const label = again ? mask(again.label) : '';
    const title = again ? fill(S.againTitle, { label }) : S.title;
    const body = again ? fill(S.againBody, { label, who: again.email ? mask(again.email) : label }) : S.body;
    AD.dom.openModal({ title, view: 'cli-add', size: 'alert',
      body: [el('p', { text: body }), el('p', { class: 'modal-hint', text: S.hint })],
      actions: [AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal()), AD.dom.button(S.open, async () => {
        const result = await action('add');
        if (result) AD.dom.closeModal();
      }, { kind: 'primary', attrs: { 'data-autofocus': '' } })] });
  }

  function rename(account) {
    const R = T.rename;
    const input = el('input', { class: 'text-input', value: account.label, maxlength: 80, 'aria-label': R.label,
      'data-autofocus': '', autocomplete: 'off', spellcheck: 'false' });
    const save = async () => {
      const label = input.value.trim();
      if (!label || label === account.label) {
        AD.dom.closeModal();
        return;
      }
      const result = await action('rename', { id: account.id, label });
      if (result) AD.dom.closeModal();
    };
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        save();
      }
    });
    AD.dom.openModal({ title: R.title, body: input, view: 'cli-rename', size: 'alert',
      actions: [AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal()), AD.dom.button(R.save, save, { kind: 'primary' })] });
    input.select();
  }

  function remove(account) {
    const R = T.remove;
    AD.dom.openModal({ title: fill(R.title, { label: mask(account.label) }), view: 'cli-remove', size: 'alert',
      body: el('p', { text: R.body }),
      actions: [AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal(), { attrs: { 'data-autofocus': '' } }),
        AD.dom.button(R.button, async () => {
          const result = await action('remove', { id: account.id });
          if (result) AD.dom.closeModal();
        }, { kind: 'danger' })] });
  }

  async function exclude(account) {
    const result = await action('cli-exclude', { id: account.id, excluded: !account.excluded });
    if (result) {
      AD.dom.announce(account.excluded ? `${mask(account.label)} can be used by auto-switch again.`
        : `${mask(account.label)} is left out of auto-switch.`);
    }
  }

  // ---- Header ----------------------------------------------------------------------------------------

  /** The table's order, kept per browser (a viewer convenience; storage may be unavailable). "Most 5-hour
   * room" only while the table shows that column (`session`). */
  function sortMode(session) {
    let mode = picked;
    if (!mode) {
      try { mode = window.localStorage.getItem(SORT_KEY); } catch (err) { /* private window or blocked storage */ }
    }
    return A.SORTS.includes(mode) && (session || mode !== 'session') ? mode : 'room';
  }

  function sortControl(mode, session) {
    const select = el('select', { id: 'accounts-sort', 'data-keep': 'cli-sort' },
      A.SORTS.filter((value) => session || value !== 'session')
        .map((value) => el('option', { value, text: T.sorts[value], selected: value === mode })));
    select.addEventListener('change', () => {
      try { window.localStorage.setItem(SORT_KEY, select.value); } catch (err) { /* kept for this page only */ }
      picked = select.value;
      if (AD.app.state) render(AD.app.state, AD.app.clockNow());
    });
    return el('div', 'section-actions',
      el('label', { class: 'popup-label', for: 'accounts-sort', text: 'Sort by' }),
      el('span', 'popup', select, AD.dom.svgIcon('chevrons', { className: 'popup-chevrons' })));
  }

  function head(accounts, mode, readonly, session) {
    const count = accounts.length === 1 ? T.countOne : fill(T.count, { n: accounts.length });
    return el('header', 'section-head',
      el('div', null, el('h2', { class: 'section-title', id: 'accounts-title', text: T.title }),
        el('p', { class: 'section-sub', text: accounts.length ? `${count} · ${T.sub}` : T.sub })),
      el('div', 'section-actions',
        accounts.length > 1 ? sortControl(mode, session) : null,
        AD.dom.button(T.add, add, { icon: 'plus', disabled: !!readonly, title: readonly || undefined, attrs: { 'data-keep': 'cli-add' } })));
  }

  // ---- Notes -------------------------------------------------------------------------------------------

  function notes(provider, auto, undo) {
    const items = [];
    if (undo) {
      items.push(el('div', { class: 'banner is-compact', dataset: { kind: 'cli-last', level: 'info' } },
        el('span', 'banner-icon', AD.dom.svgIcon('bolt')),
        el('div', 'banner-text', el('p', { class: 'banner-title', text: mask(auto.last.reason) }),
          el('p', 'banner-message', el('time', { dataset: { reset: auto.last.undo_until, resetTemplate: T.undoLeft, resetDone: '' },
            text: fill(T.undoLeft, { countdown: AD.model.countdownText(auto.last.undo_until - AD.app.clockNow() / 1000) }) }))),
        el('div', 'banner-actions', AD.dom.button(T.undo, async () => {
          const result = await action('cli-undo');
          if (result && result.plan) AD.switch.showPlan(result.plan);
        }, { small: true, icon: 'undo', attrs: { 'data-cli-undo': '', 'data-keep': 'cli-undo' } }))));
    }
    if (provider.readonly_reason) {
      items.push(el('div', { class: 'banner is-compact', dataset: { kind: 'cli-readonly', level: 'info' } },
        el('span', 'banner-icon', AD.dom.svgIcon('lock')), el('p', { class: 'banner-text', text: mask(provider.readonly_reason) })));
    }
    const enrollment = provider.enrollment;
    if (enrollment && enrollment.phase && enrollment.phase !== 'idle') {
      const waiting = ['signing_in', 'waiting_exit'].includes(enrollment.phase);
      items.push(el('div', { class: 'banner is-compact', role: 'status', dataset: { kind: 'cli-enrollment', level: 'info', phase: enrollment.phase } },
        el('span', 'banner-icon', AD.dom.svgIcon(waiting ? 'hourglass' : 'info')),
        el('p', { class: 'banner-text', text: mask(enrollment.message) }),
        waiting ? el('div', 'banner-actions', AD.dom.button(T.cancelAdd, () => action('cancel-login'),
          { small: true, attrs: { 'data-keep': 'cli-cancel-add' } })) : null));
    }
    return items;
  }

  // ---- Frame -------------------------------------------------------------------------------------------

  function emptyState(icon, title, text, withButton, readonly) {
    return el('div', 'card empty-state',
      el('span', 'empty-icon', AD.dom.svgIcon(icon)),
      el('h3', { class: 'empty-title', text: title }),
      el('p', { class: 'empty-text', text }),
      withButton ? AD.dom.button(T.add, add, { kind: 'primary', icon: 'plus', large: true, disabled: !!readonly,
        attrs: { 'data-keep': 'cli-empty-add' } }) : null);
  }

  /** The column labels; the 5-hour one only while an account reports a 5-hour window (`session`). */
  function columnHead(labels, session) {
    return el('div', { class: 'table-head', 'aria-hidden': 'true' },
      el('span', { text: T.colAccount }),
      el('div', 'table-head-limits', labels.map((text, index) => (index === 0 && !session ? null : el('span', null,
        index === 2 ? el('span', { class: 'fable-mark' }) : null, text)))),
      el('span'));
  }

  /** The section's slots, built once: head, notes and body (the table or an empty state). */
  function frame(host) {
    if (!parts || parts.body.parentElement !== host) {
      parts = { head: el('header', 'section-head'), notes: el('div', 'group-notes'), body: el('div') };
      host.replaceChildren(parts.head, parts.notes, parts.body);
      sigs.head = sigs.notes = sigs.columns = null;
    }
    return parts;
  }

  /** Swap a slot's node for a new one, keeping focus (data-keep) across the swap. */
  function replaceSlot(name, next) {
    AD.dom.swap(parts[name], next);
    parts[name] = next;
  }

  function table(body) {
    let list = body.querySelector(':scope > .table > .rows');
    if (!list) {
      list = el('ol', { class: 'rows', 'aria-labelledby': 'accounts-title' });
      AD.rows.bindKeys(list);
      body.replaceChildren(el('div', 'table', el('div', { class: 'table-head', 'aria-hidden': 'true' }), list));
      sigs.columns = null;
    }
    return list;
  }

  /** A gentle green wash on a row whose limit just refilled (never on the first render). */
  function washRefills(accounts, nowMs) {
    const now = new Set();
    for (const account of accounts) {
      for (const win of account.windows || []) {
        if (AD.model.deriveWindow(win, account, nowMs).state === 'refilled') now.add(`${account.id}|${win.id}`);
      }
    }
    if (refilledKeys) {
      for (const key of now) {
        if (refilledKeys.has(key)) continue;
        const node = document.getElementById(`row-${key.split('|')[0]}`);
        if (!node) continue;
        node.classList.add('is-refilled');
        window.setTimeout(() => node.classList.remove('is-refilled'), REFILL_MS);
      }
    }
    refilledKeys = now;
  }

  /** Render the section at nowMs. */
  function render(state, nowMs) {
    const host = document.getElementById('accounts');
    if (!host || !state) return;
    const provider = A.provider(state);
    if (!provider) {
      parts = null;
      if (!host.querySelector(':scope > .empty-state')) {
        host.replaceChildren(emptyState('terminal', T.unavailableTitle, T.unavailableText, false));
      }
      return;
    }
    frame(host);
    const accounts = A.terminals(state);
    const auto = A.autoswitch(state);
    // Read-only: the terminal store is managed elsewhere (a custom config folder), or another
    // AccountDeck 2 holds the sign-ins (its banner says so at the top).
    const readonly = provider.readonly_reason || (state.read_only === true ? T.readOnlyOther : null);
    // Claude has reported no 5-hour limit since 2026-10-31: its column shows only while an account reports one.
    const session = A.showsSession(accounts);
    const mode = sortMode(session);

    const headSig = JSON.stringify([accounts.length, mode, readonly, session]);
    if (headSig !== sigs.head) {
      sigs.head = headSig;
      replaceSlot('head', head(accounts, mode, readonly, session));
    }
    const undo = !!(auto.last && auto.last.undo_until > nowMs / 1000);
    const notesSig = JSON.stringify([undo && auto.last, readonly, provider.enrollment, AD.dom.hideEmails()]);
    if (notesSig !== sigs.notes) {
      sigs.notes = notesSig;
      replaceSlot('notes', el('div', 'group-notes', notes(provider, auto, undo)));
    }

    if (!accounts.length) {
      if (!parts.body.querySelector(':scope > .empty-state') || sigs.columns !== `empty|${readonly}`) {
        parts.body.replaceChildren(emptyState('terminal', T.emptyTitle, T.emptyText, true, readonly));
        sigs.columns = `empty|${readonly}`;
      }
      return;
    }
    const list = table(parts.body);
    const labels = A.columns(accounts);
    if (JSON.stringify([labels, session]) !== sigs.columns) {
      sigs.columns = JSON.stringify([labels, session]);
      parts.body.querySelector('.table-head').replaceWith(columnHead(labels, session));
      list.parentElement.style.setProperty('--limit-columns', session ? '3' : '2');
    }
    const best = auto.enabled ? null : A.bestPick(state);
    const ctx = {
      nowMs, display: A.display(state), threshold: auto.enabled && Number.isFinite(auto.threshold) ? auto.threshold : null,
      fableLeader: A.fableLeader(A.fableBoard(accounts, nowMs)), nextId: A.nextUp(state),
      bestId: best ? best.account.id : null, readonly, session,
    };
    AD.rows.patchRows(list, A.sorted(accounts, mode, nowMs), state, ctx);
    washRefills(accounts, nowMs);
  }

  AD.terminal = { render, add, rename, remove, exclude };
})();
