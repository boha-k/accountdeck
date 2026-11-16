/* AccountDeck 2 — appearance (Auto, Light or Dark).
 * Loaded first in <head> without defer so data-theme is set before the stylesheets paint. The choice
 * (system | light | dark) lives only in localStorage["ad2.theme"]; "system" (shown as Auto, like macOS)
 * removes the attribute and the tokens follow prefers-color-scheme. Any element with [data-theme-value] is a radio of the theme control;
 * clicks and arrow keys on it are handled here by delegation, so controls rendered later need no wiring. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};

  const KEY = 'ad2.theme';
  const MODES = ['system', 'light', 'dark'];
  let mode = 'system';
  let bound = false;

  function stored() {
    try {
      const value = window.localStorage.getItem(KEY);
      return MODES.includes(value) ? value : 'system';
    } catch (err) {
      return 'system';
    }
  }

  function apply() {
    const root = document.documentElement;
    if (mode === 'system') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', mode);
    for (const radio of document.querySelectorAll('[data-theme-value]')) {
      const on = radio.getAttribute('data-theme-value') === mode;
      radio.setAttribute('aria-checked', String(on));
      radio.tabIndex = on ? 0 : -1;
    }
  }

  function set(next) {
    if (!MODES.includes(next)) return mode;
    const changed = next !== mode;
    mode = next;
    try {
      window.localStorage.setItem(KEY, next);
    } catch (err) {
      // Storage can be blocked; the choice then lasts for this page only.
    }
    apply();
    if (changed) document.dispatchEvent(new CustomEvent('ad:theme', { detail: { mode } }));
    return mode;
  }

  function cycle() {
    return set(MODES[(MODES.indexOf(mode) + 1) % MODES.length]);
  }

  function get() {
    return mode;
  }

  function reducedMotion() {
    try {
      return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    } catch (err) {
      return false;
    }
  }

  function onClick(event) {
    const radio = event.target.closest && event.target.closest('[data-theme-value]');
    if (radio) set(radio.getAttribute('data-theme-value'));
  }

  // Radio-group keys: arrows move the choice within the control, Home/End jump to the ends.
  function onKeydown(event) {
    const radio = event.target.closest && event.target.closest('[data-theme-value]');
    if (!radio) return;
    const group = radio.closest('[role="radiogroup"]') || radio.parentElement;
    const radios = Array.from(group.querySelectorAll('[data-theme-value]'));
    const index = radios.indexOf(radio);
    let next = null;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = radios[(index + 1) % radios.length];
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = radios[(index - 1 + radios.length) % radios.length];
    else if (event.key === 'Home') next = radios[0];
    else if (event.key === 'End') next = radios[radios.length - 1];
    if (!next) return;
    event.preventDefault();
    set(next.getAttribute('data-theme-value'));
    next.focus();
  }

  // Another tab changed the theme.
  function onStorage(event) {
    if (event.key !== KEY) return;
    const next = stored();
    if (next === mode) return;
    mode = next;
    apply();
    document.dispatchEvent(new CustomEvent('ad:theme', { detail: { mode } }));
  }

  /** Read the stored choice and apply it; idempotent, so it also re-syncs controls rendered later. */
  function init() {
    mode = stored();
    apply();
    if (!bound) {
      bound = true;
      document.addEventListener('click', onClick);
      document.addEventListener('keydown', onKeydown);
      window.addEventListener('storage', onStorage);
    }
    return mode;
  }

  AD.theme = { init, get, set, cycle, reducedMotion };
  init();
})();
