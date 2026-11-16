/* AccountDeck 2 — the Claude Desktop accounts section. Saving the signed-in account, adding one and switching all
 * close Claude, so each asks the service for a plan and the user's consent (switch.js); nothing here starts one
 * directly. Renaming and removing change only AccountDeck's own list. rows.js draws the rows.
 *
 * The state says nothing about whether Claude Desktop is installed, only whether it runs and who is signed in. With
 * no account listed and Claude closed, the section stays a single line rather than pushing an empty state, with
 * its buttons, at a Mac that may not have Claude Desktop at all. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;
  const D = STRINGS.desktop;
  const el = (...args) => AD.dom.el(...args);
  let parts = null;
  let headSig = null;
  let bodySig = null;

  const add = () => AD.switch.openPurpose('claude', 'add');
  const capture = () => AD.switch.openPurpose('claude', 'capture');
  const action = (name, body) => AD.api.action(name, Object.assign({ provider: 'claude' }, body))
    .catch((error) => { AD.dom.toast(error.message, { level: 'critical' }); return null; });

  function rename(account) {
    const R = STRINGS.accounts.rename;
    const input = el('input', { class: 'text-input', value: account.label, maxlength: 80,
      'aria-label': R.label, 'data-autofocus': '', autocomplete: 'off', spellcheck: 'false' });
    const save = async () => {
      const label = input.value.trim();
      if (!label || label === account.label) { AD.dom.closeModal(); return; }
      if (await action('rename', { id: account.id, label })) AD.dom.closeModal();
    };
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); save(); }
    });
    AD.dom.openModal({ title: R.title, view: 'desktop-rename', size: 'alert', body: input,
      actions: [AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal()),
        AD.dom.button(R.save, save, { kind: 'primary' })] });
    input.select();
  }

  function remove(account) {
    const R = STRINGS.accounts.remove;
    AD.dom.openModal({ title: fill(R.title, { label: AD.dom.maskEmail(account.label) }),
      view: 'desktop-remove', size: 'alert', body: el('p', { text: D.removeBody }),
      actions: [AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal(), { attrs: { 'data-autofocus': '' } }),
        AD.dom.button(R.button, async () => {
          if (await action('remove', { id: account.id })) AD.dom.closeModal();
        }, { kind: 'danger' })] });
  }

  /** The ids of saved accounts the PC import is still converting for this Mac (move_import.deferred): the service
   * refuses their reads and switches until Claude Desktop has run here once, and their rows stay read-only. The
   * state has no per-account flag; such a row is the PC's, not switchable (no usable profile yet) and not frozen. */
  function importing(state, accounts) {
    const deferred = ((state.move_import || {}).deferred || {}).count;
    if (!(deferred > 0)) return new Set();
    return new Set(accounts.filter((a) => a.imported_from === 'pc' && a.saved !== false && !a.switchable
      && !AD.accounts.frozen(a)).map((a) => a.id));
  }

  function head(accounts, current, compact, blocked, reason) {
    const count = accounts.length === 1 ? STRINGS.accounts.countOne : fill(STRINGS.accounts.count, { n: accounts.length });
    return el('header', 'section-head',
      el('div', null, el('h2', { id: 'desktop-title', class: 'section-title', text: D.title }),
        compact ? null : el('p', { class: 'section-sub', text: accounts.length ? `${count} · ${D.sub}` : D.sub })),
      compact ? null : el('div', 'section-actions',
        current && current.saved === false ? AD.dom.button(D.capture, capture,
          { disabled: blocked, title: reason || undefined, attrs: { 'data-keep': 'desktop-capture' } }) : null,
        AD.dom.button(D.add, add, { icon: 'plus', disabled: blocked, title: reason || undefined,
          attrs: { 'data-keep': 'desktop-add' } })));
  }

  function render(state, nowMs) {
    const host = document.getElementById('desktop-accounts');
    if (!host) return;
    const provider = state.providers && state.providers.claude;
    host.hidden = !provider;
    if (host.hidden) return;
    if (!parts || parts.body.parentElement !== host) {
      parts = { head: el('header'), body: el('div') };
      host.replaceChildren(parts.head, parts.body);
      headSig = bodySig = null;
    }
    const accounts = (state.accounts || []).filter((a) => a.provider === 'claude');
    const current = accounts.find((a) => a.active);
    // Nothing listed, Claude closed and no Claude sign-in waiting: one quiet line.
    const compact = !accounts.length && !(provider.running && provider.running.desktop)
      && !(state.login && state.login.provider === 'claude');
    const recovering = !!(state.recovery && state.recovery.pending);
    const reason = state.read_only ? STRINGS.accounts.readOnlyOther : (recovering ? D.recoverFirst : null);
    const blocked = !!reason || !!state.pending || !!state.login;
    const sig = JSON.stringify([accounts.length, current && current.saved, compact, blocked, reason]);
    if (sig !== headSig) {
      headSig = sig;
      const next = head(accounts, current, compact, blocked, reason);
      AD.dom.swap(parts.head, next);
      parts.head = next;
    }
    if (!accounts.length) {
      if (bodySig !== `empty|${compact}`) {
        bodySig = `empty|${compact}`;
        parts.body.replaceChildren(compact
          ? el('div', 'card empty-inline', el('span', 'empty-icon', AD.dom.svgIcon('claude')),
            el('p', { class: 'empty-text', text: D.absent }))
          : el('div', 'card empty-state', el('span', 'empty-icon', AD.dom.svgIcon('claude')),
            el('h3', { class: 'empty-title', text: D.emptyTitle }),
            el('p', { class: 'empty-text', text: D.emptyText })));
      }
      return;
    }
    let list = parts.body.querySelector('.rows');
    if (!list) {
      list = el('ol', { class: 'rows', 'aria-labelledby': 'desktop-title' });
      AD.rows.bindKeys(list);
      parts.body.replaceChildren(el('div', 'table',
        el('div', { class: 'table-head', 'aria-hidden': 'true' },
          el('span', { text: STRINGS.accounts.colAccount }), el('span', { text: D.usage }), el('span')), list));
      bodySig = 'table';
    }
    AD.rows.patchRows(list, AD.accounts.sorted(accounts, 'room', nowMs), state, {
      nowMs, display: AD.accounts.display(state), threshold: null, readonly: reason,
      fableLeader: null, nextId: null, bestId: null, importing: importing(state, accounts),
    });
  }

  AD.desktop = { render, add, capture, rename, remove };
})();
