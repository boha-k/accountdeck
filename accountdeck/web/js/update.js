/* AccountDeck 2 — the update prompt (state.update, accountdeck/updates.py): the banner's controls, the confirmation
 * before an update, what the page says once the update runs, and the status Settings › About shows.
 *
 * Only an installed copy asks GitHub, every 5 minutes and when the user opens AccountDeck; the service keeps nothing
 * of GitHub's answer but a version, which it checks, and that version's release page. The page checks both again:
 * a version is dotted numbers, the release page is https://github.com/<owner>/<repo>/releases/tag/v<that version>
 * and nothing else, and no other text from the network ever reaches the page. The prompt shows only while the
 * status is "available"; every other status (up to date, skipped, never checked, off, failed, a copy that does
 * not check) shows nothing outside Settings.
 *
 * Update… asks first, in the words of updates.CONFIRM, then sends start-update with the version it showed. The
 * service opens a Terminal window running the installed get.sh, and the installer stops and restarts AccountDeck
 * by itself, so this page loses its connection (the app's own window closes with the process). */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;
  const U = STRINGS.update;
  const VERSION = /^[0-9]+(?:\.[0-9]+){1,3}$/;
  const RELEASE = /^\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/releases\/tag\/v([0-9]+(?:\.[0-9]+){1,3})$/;
  const el = (...args) => AD.dom.el(...args);

  let asking = false;              // a Check Now from this page is waiting for its answer

  function valid(version) {
    return typeof version === 'string' && version.length <= 32 && VERSION.test(version);
  }

  /** The update block when it offers a version this page can name, else null. */
  function offered(update) {
    return update && typeof update === 'object' && update.status === 'available' && update.available === true
      && valid(update.latest) && valid(update.current) ? update : null;
  }

  /** The offered version's release page on GitHub, or null for anything else. */
  function releasePage(update) {
    let parsed;
    try {
      parsed = new URL(update.url);
    } catch (err) {
      return null;
    }
    const tag = RELEASE.exec(parsed.pathname);
    if (parsed.protocol !== 'https:' || parsed.hostname !== 'github.com' || parsed.port || parsed.username
      || parsed.password || parsed.search || parsed.hash || !tag || tag[1] !== update.latest) return null;
    return parsed.href;
  }

  // ---- The banner's controls (banners.js draws the banner) -------------------------------------

  function notesLink(update) {
    const href = releasePage(update);
    if (!href) return null;
    return el('a', { class: ['button', 'button-small'], href, target: '_blank', rel: 'noopener noreferrer',
      title: U.notesTip, 'data-keep': 'update-notes' }, U.notes, AD.dom.svgIcon('external'));
  }

  /** Skip This Version, What's New, Update… (the default action last, as on a Mac). `describedBy` names the line
   * that says why Update… is disabled. */
  function bannerActions(update, describedBy) {
    const blocked = typeof update.blocked === 'string' && update.blocked ? update.blocked : null;
    return [
      AD.dom.button(U.skip, () => skip(update), { kind: 'ghost', small: true, attrs: { 'data-keep': 'update-skip' } }),
      notesLink(update),
      AD.dom.button(U.update, () => confirm(update), { small: true, disabled: !!blocked, title: blocked || undefined,
        attrs: { 'data-keep': 'update-start', 'aria-describedby': blocked && describedBy ? describedBy : undefined } }),
    ];
  }

  // ---- The actions ------------------------------------------------------------------------------

  async function skip(update) {
    try {
      await AD.api.action('skip-update', { version: update.latest });
    } catch (err) {
      AD.dom.toast(err.message, { level: err.code === 'invalid' ? 'info' : 'critical' });
      return;
    }
    AD.dom.toast(fill(U.skipped, { version: update.latest }), { level: 'info' });
  }

  /** The confirmation (an alert, like the menu bar's): what the update does, then Cancel or Update. `error` is why
   * the last try was refused. */
  function confirm(update, error) {
    if (!offered(update)) return;
    AD.settings.close();
    const version = update.latest;
    AD.dom.openModal({
      title: fill(U.confirmTitle, { version }), view: 'update-confirm', size: 'alert',
      body: [el('p', { text: fill(U.confirm, { version, current: update.current }) }),
        error ? el('p', { class: 'modal-error', role: 'alert', text: error }) : null],
      actions: [
        AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal(), { dataset: { choice: 'cancel' } }),
        AD.dom.button(U.confirmButton, () => start(update), { kind: 'primary', dataset: { choice: 'update' },
          attrs: { 'data-autofocus': '' } }),
      ],
    });
  }

  /** start-update with the version the confirmation showed; the service refuses anything else. */
  async function start(update) {
    let result;
    try {
      result = await AD.api.action('start-update', { version: update.latest });
    } catch (err) {
      confirm(update, err.message);
      return;
    }
    started(valid(result && result.started) ? result.started : update.latest);
  }

  /** The update runs in Terminal: say so, and what happens to this window. */
  function started(version) {
    AD.dom.openModal({
      title: fill(U.startedTitle, { version }), view: 'update-started', size: 'alert',
      body: el('p', { text: AD.format.appWindow() ? U.startedWindow : U.startedPage }),
      actions: [AD.dom.button(STRINGS.button.done, () => AD.dom.closeModal(),
        { kind: 'primary', attrs: { 'data-autofocus': '' } })],
    });
  }

  /** Check Now: one check (the service waits up to a few seconds for GitHub), then the status says what it found. */
  async function checkNow(redraw) {
    if (asking) return;
    asking = true;
    redraw();
    let result = null;
    try {
      result = await AD.api.action('check-update', {});
    } catch (err) {
      AD.dom.toast(err.message, { level: 'critical' });
    } finally {
      asking = false;
    }
    redraw();
    if (result && result.update) AD.dom.announce(said(result.update));
  }

  // ---- What Settings › About says ----------------------------------------------------------------

  /** {label, hint, ago, agoText, checking, off}: the status in words. `ago` (epoch s) is the time `agoText` names
   * ("Checked {age} ago."), said after the hint. `off`: Check Now cannot run (the service would refuse it). */
  function status(update) {
    const S = U.status;
    const u = update && typeof update === 'object' ? update : {};
    const checked = typeof u.checked_at === 'number' ? u.checked_at : null;
    if (asking || u.checking) return { label: S.checking, hint: null, ago: null, checking: true };
    const latest = valid(u.latest) ? u.latest : null;
    const current = typeof u.current === 'string' ? u.current : '';      // this build's own version
    const found = (label, hint) => ({ label, hint, ago: checked, agoText: U.checked });
    // The service sends only checked versions (contract._check_update); one that is not is never shown.
    if ((u.status === 'available' || u.status === 'skipped') && !latest) return { label: S.unavailable, hint: null, ago: null };
    switch (u.status) {
      case 'available':
        return found(fill(S.available, { version: latest }), fill(S.availableHint, { current }));
      case 'skipped':
        return found(fill(S.skipped, { version: latest }), S.skippedHint);
      case 'current':
        return found(fill(S.current, { current }), null);
      case 'never':
        return { label: S.never, hint: S.neverHint, ago: null };
      case 'failed':
        return { label: S.failed, hint: S.failedHint, ago: typeof u.failed_at === 'number' ? u.failed_at : null,
          agoText: S.tried };
      case 'off':
        return { label: S.off, hint: S.offHint, ago: null, off: S.offButton };
      default:
        return { label: S.unavailable, hint: typeof u.reason === 'string' ? u.reason : null, ago: null,
          off: typeof u.reason === 'string' ? u.reason : S.unavailable };
    }
  }

  /** The status as one sentence for the live region (a check asked for here has finished). */
  function said(update) {
    const words = status(update);
    const age = words.ago !== null && words.agoText
      ? fill(words.agoText, { age: AD.format.ago(words.ago, AD.app.clockNow()) }) : null;
    return [`${words.label}.`, words.hint, age].filter(Boolean).join(' ');
  }

  AD.update = { valid, offered, releasePage, bannerActions, confirm, started, checkNow, status, asking: () => asking };
})();
