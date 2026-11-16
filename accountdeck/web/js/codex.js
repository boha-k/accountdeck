/* Codex account management uses the service's existing isolated login and consent-bound switch.
 * No Claude action is sent from this section. It never switches an account merely by saving it. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;
  const C = STRINGS.codex;
  const el = (...args) => AD.dom.el(...args);
  let parts = null;
  let headSig = null;

  const add = () => AD.switch.openPurpose('codex', 'add');
  const capture = () => AD.switch.openPurpose('codex', 'capture');
  const action = (name, body) => AD.api.action(name, Object.assign({ provider: 'codex' }, body))
    .catch((error) => { AD.dom.toast(error.message, { level: 'critical' }); return null; });

  function setup() {
    AD.dom.openModal({ title: C.setupTitle, view: 'codex-setup',
      body: [el('p', { class: 'plan-lead', text: C.isolation }),
        el('ol', 'plan-steps', C.steps.map((text) => el('li', { text }))),
        el('p', { class: 'modal-hint', text: C.storage }),
        el('a', { href: 'https://learn.chatgpt.com/docs/auth#credential-storage', target: '_blank',
          rel: 'noopener noreferrer', text: C.docs })],
      actions: [AD.dom.button(STRINGS.button.done, () => AD.dom.closeModal(), { kind: 'primary' })] });
  }

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
    AD.dom.openModal({ title: R.title, view: 'codex-rename', size: 'alert', body: input,
      actions: [AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal()),
        AD.dom.button(R.save, save, { kind: 'primary' })] });
    input.select();
  }

  function remove(account) {
    const R = STRINGS.accounts.remove;
    AD.dom.openModal({ title: fill(R.title, { label: AD.dom.maskEmail(account.label) }),
      view: 'codex-remove', size: 'alert', body: el('p', { text: C.removeBody }),
      actions: [AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal(), { attrs: { 'data-autofocus': '' } }),
        AD.dom.button(R.button, async () => {
          if (await action('remove', { id: account.id })) AD.dom.closeModal();
        }, { kind: 'danger' })] });
  }

  function render(state, nowMs) {
    const host = document.getElementById('codex-accounts');
    if (!host) return;
    host.hidden = !state.providers || !state.providers.codex;
    if (host.hidden) return;
    if (!parts || parts.body.parentElement !== host) {
      parts = { head: el('header'), body: el('div') };
      host.replaceChildren(parts.head, parts.body);
      headSig = null;
    }
    const accounts = (state.accounts || []).filter((a) => a.provider === 'codex');
    const current = accounts.find((a) => a.active);
    const readonly = state.read_only ? STRINGS.accounts.readOnlyOther : null;
    const blocked = !!readonly || !!state.pending || !!state.login || !!(state.recovery && state.recovery.pending);
    const sig = JSON.stringify([accounts.length, current && current.saved, blocked, readonly]);
    if (sig !== headSig) {
      headSig = sig;
      const count = accounts.length === 1 ? STRINGS.accounts.countOne : fill(STRINGS.accounts.count, { n: accounts.length });
      const head = el('header', 'section-head',
        el('div', null, el('h2', { id: 'codex-title', class: 'section-title', text: C.title }),
          el('p', { class: 'section-sub', text: accounts.length ? `${count} · ${C.sub}` : C.sub })),
        el('div', 'section-actions',
          AD.dom.button(C.setup, setup, { attrs: { 'data-keep': 'codex-setup' } }),
          current && current.saved === false ? AD.dom.button(C.capture, capture,
            { disabled: blocked, attrs: { 'data-keep': 'codex-capture' } }) : null,
          AD.dom.button(C.add, add, { icon: 'plus', disabled: blocked, title: readonly || undefined,
            attrs: { 'data-keep': 'codex-add' } })));
      AD.dom.swap(parts.head, head);
      parts.head = head;
    }
    if (!accounts.length) {
      if (!parts.body.querySelector('.empty-state')) {
        parts.body.replaceChildren(el('div', 'card empty-state',
          el('span', 'empty-icon', AD.dom.svgIcon('codex')),
          el('h3', { class: 'empty-title', text: C.emptyTitle }),
          el('p', { class: 'empty-text', text: C.emptyText })));
      }
      return;
    }
    let list = parts.body.querySelector('.rows');
    if (!list) {
      list = el('ol', { class: 'rows', 'aria-labelledby': 'codex-title' });
      AD.rows.bindKeys(list);
      parts.body.replaceChildren(el('div', 'table',
        el('div', { class: 'table-head', 'aria-hidden': 'true' },
          el('span', { text: STRINGS.accounts.colAccount }), el('span', { text: C.usage }), el('span')), list));
    }
    AD.rows.patchRows(list, AD.accounts.sorted(accounts, 'room', nowMs), state, {
      nowMs, display: AD.accounts.display(state), threshold: null, readonly: readonly || (state.recovery && state.recovery.pending ? STRINGS.button.recover : null),
      fableLeader: null, nextId: null, bestId: null,
    });
  }

  AD.codex = { render, add, capture, setup, rename, remove };
})();
