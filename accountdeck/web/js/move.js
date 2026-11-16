/* AccountDeck 2 — Import from PC: the Mac's side of the one-time PC-to-Mac move.
 * Open an encrypted .adeck bundle, preview it, import it, then copy the eight-character
 * confirmation code back to the PC. Secrets (the passphrase) live only in the open form and the
 * request body, never in browser storage. Banners: a first-run invitation while there are no
 * accounts, each undismissed import result (with its code), and deferred Claude Desktop conversions. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const el = (...args) => AD.dom.el(...args);
  const VIEW = 'move';
  let waitingImport = null;
  const p = (text, cls = 'plan-muted') => el('p', { class: cls, text });
  const close = () => AD.dom.button(AD.format.STRINGS.button.cancel, () => AD.dom.closeModal());
  const done = () => AD.dom.button(AD.format.STRINGS.button.done, () => AD.dom.closeModal(), { kind: 'primary', attrs: { 'data-autofocus': '' } });

  function show(title, body, actions) {
    AD.settings.close();
    AD.dom.openModal({ title, body: el('div', 'move-form', body), actions: actions || [close()], view: VIEW });
  }

  function field(label, id, value = '', type = 'text', extra = {}) {
    const input = el('input', Object.assign({ id, type, value, class: 'text-input', autocomplete: 'off', spellcheck: 'false' }, extra));
    return { input, node: el('div', 'move-field', el('label', { for: id, text: label }), input) };
  }

  function error(message) {
    const form = AD.dom.modalView() === VIEW && document.querySelector('#modal .move-form');
    if (!form) {
      AD.dom.toast(message, { level: 'critical' });
      return;
    }
    const old = form.querySelector('.modal-error');
    if (old) old.remove();
    form.append(el('p', { class: 'modal-error', role: 'alert', text: message }));
  }

  function submit(label, work, options = {}) {
    const node = AD.dom.button(label, async () => {
      if (node.disabled) return;
      node.disabled = true;
      try { await work(); } catch (err) { error(err.message); } finally { node.disabled = false; }
    }, Object.assign({ kind: 'primary' }, options));
    return node;
  }

  async function copy(value) {
    try {
      await navigator.clipboard.writeText(value);
      AD.dom.toast('Copied.', { level: 'good' });
    } catch (_) {
      AD.dom.toast('Select the text and copy it with ⌘C.');
    }
  }

  function rows(accounts) {
    if (!(accounts || []).length) return null;
    return el('ul', 'import-list', accounts.map((a) => el('li', 'import-row',
      el('div', 'import-text', el('strong', { text: AD.dom.maskEmail(a.label || a.provider) }),
        p(a.action === 'on_pc' ? 'Still in use on the PC — listed here, not usable yet'
          : ({ new: 'New on this Mac', update: 'Updates the copy on this Mac', fill: 'Fills in a missing sign-in', refused: 'Cannot be imported' }[a.action] || a.action || a.status || ''),
        'import-meta'),
        a.reason ? p(AD.dom.maskEmail(a.reason), 'import-note') : null))));
  }

  function grouped(code) {
    const raw = String(code || '').replace(/-/g, '').toUpperCase();
    return /^[A-Z2-7]{8}$/.test(raw) ? `${raw.slice(0, 4)}-${raw.slice(4)}` : null;
  }

  function resultBody(result) {
    const code = result.complete && !(result.failures || []).length && grouped(result.code);
    if (code) {
      return [p('Every account is stored on this Mac. Enter this code on your PC to finish the move:', 'plan-lead'),
        el('output', { class: 'move-code', 'aria-label': `Confirmation code ${code.split('').join(' ')}`, text: code }),
        el('div', null, AD.dom.button('Copy Code', () => copy(code), { small: true, icon: 'copy' }))];
    }
    return [p('No confirmation code: some accounts could not be imported.', 'modal-error'),
      el('ul', 'import-list', (result.failures || []).map((f) => el('li', 'import-row',
        el('span', { text: AD.dom.maskEmail(`${f.label || f.id || 'Account'}: ${f.reason || f.message || 'Import failed'}`) })))),
      p('Use Cancel Move on the PC and export again.')];
  }

  function importResult(result) {
    show(result.complete && !(result.failures || []).length ? 'Import Complete' : 'Import Incomplete', resultBody(result), [done()]);
  }

  function openImport() {
    const path = field('Bundle file', 'move-path', '', 'text', { placeholder: '~/Downloads/accounts.adeck', class: 'text-input is-mono' });
    const phrase = field('Passphrase', 'move-passphrase', '', 'password');
    show('Import Accounts from Your PC', [
      p('Open the encrypted bundle AccountDeck 2 exported on your PC, using the passphrase you kept with it.', 'plan-lead'),
      path.node, phrase.node,
      p('Tip: in Finder, select the .adeck file and press ⌥⌘C to copy its path.'),
      p('If macOS asks to access Claude Safe Storage, choose Allow to finish the Desktop import. Always Allow is not needed.'),
    ], [close(), submit('Preview Import', async () => {
      if (!path.input.value.trim() || !phrase.input.value) { error('Enter the bundle path and its passphrase.'); return; }
      const body = { path: path.input.value.trim(), passphrase: phrase.input.value };
      const answer = await AD.api.action('import-bundle-preview', body);
      if (AD.dom.modalView() !== VIEW) return;
      const preview = answer.preview || {};
      phrase.input.value = '';
      show('Review Import', [p(preview.message || 'Review the accounts before importing.', 'plan-lead'), rows(preview.accounts)],
        preview.refused ? [close()] : [close(), submit('Import', async () => {
          const result = await AD.api.action('import-bundle', body);
          body.passphrase = '';
          if (AD.dom.modalView() !== VIEW) return;
          if (result.result) importResult(result.result);
          else {
            waitingImport = preview.bundle_id;
            show('Importing…', p('Your code appears here once every account is stored. It also appears on the dashboard.'), [close()]);
          }
        })]);
    })]);
    path.input.setAttribute('data-autofocus', '');
    path.input.focus();
  }

  /** The import banners for the dashboard (see the header). */
  function bannerItems(state) {
    const items = [];
    const add = (key, level, icon, title, body, actions = []) => items.push({
      key, sig: JSON.stringify([title, body && typeof body === 'string' ? body : key, state.move_import, AD.dom.hideEmails()]),
      build: () => el('div', { class: 'banner', dataset: { kind: key, level } },
        el('span', 'banner-icon', AD.dom.svgIcon(icon)),
        el('div', 'banner-text', p(title, 'banner-title'), typeof body === 'string' ? p(body, 'banner-message') : body()),
        actions.length ? el('div', 'banner-actions', actions.map(([text, run, kind]) => AD.dom.button(text, run, { small: true, kind }))) : null),
    });
    const imported = state.move_import || {};
    if (!(state.accounts || []).length && !(imported.imports || []).length) {
      add('move-first', 'info', 'import', 'Bring your accounts from your PC', 'Import the encrypted bundle AccountDeck 2 exported on Windows.',
        [['Import from PC…', openImport, 'primary']]);
    }
    for (const result of imported.imports || []) {
      add(`move-import-${result.bundle_id}`, result.complete ? 'good' : 'warning', result.complete ? 'check-circle' : 'warning',
        result.complete ? 'Accounts imported from your PC' : 'Import incomplete', () => el('div', 'move-result', resultBody(result)),
        [[AD.format.STRINGS.button.dismiss, () => AD.api.action('import-bundle-dismiss', { bundle_id: result.bundle_id })]]);
      if (waitingImport === result.bundle_id && AD.dom.modalView() === VIEW) {
        waitingImport = null;
        importResult(result);
      }
    }
    if (imported.deferred && imported.deferred.count && !(state.banners || []).some((b) => b.kind === 'move_import')) {
      add('move-deferred', 'info', 'info', 'Finish importing Claude Desktop accounts',
        `Open Claude Desktop once on this Mac to finish importing ${imported.deferred.count} ${imported.deferred.count === 1 ? 'account' : 'accounts'}.`);
    }
    return items;
  }

  /** Remove a frozen row (an account still on the PC) and its history from this Mac. */
  function remove(account) {
    AD.dom.openModal({ title: `Remove “${AD.dom.maskEmail(account.label)}”?`, view: VIEW, size: 'alert',
      body: p('This removes the saved row and its history from AccountDeck on this Mac.'),
      actions: [AD.dom.button(AD.format.STRINGS.button.cancel, () => AD.dom.closeModal(), { attrs: { 'data-autofocus': '' } }),
        submit('Remove', async () => { await AD.api.action('remove', { id: account.id }); AD.dom.closeModal(); }, { kind: 'danger' })] });
  }

  AD.move = { open: () => openImport(), bannerItems, remove };
})();
