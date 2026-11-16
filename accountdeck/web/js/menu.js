/* AccountDeck 2 — the one popover menu (an NSMenu look-alike) shared by every "More" button.
 * It lives outside the rows, so a row re-rendered by a poll never closes it. role="menu" with
 * menuitems; ↑ ↓ Home End move, a letter jumps to the item it starts, Return or Space chooses, Esc
 * or a click elsewhere closes, and focus goes back to the button that opened it (or its re-rendered
 * twin, found by data-keep). Uses the popover top layer where available. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};

  const GAP = 4;
  const EDGE = 8;
  let menu = null;
  let anchor = null;
  let anchorKey = null;
  const native = typeof HTMLElement !== 'undefined' && typeof HTMLElement.prototype.showPopover === 'function';

  function node() {
    if (!menu) {
      menu = document.getElementById('menu');
      if (!menu) return null;
      if (!native) menu.hidden = true;
      menu.addEventListener('keydown', onKeydown);
      if (native) menu.addEventListener('toggle', (event) => { if (event.newState === 'closed') finish(); });
    }
    return menu;
  }

  function items() {
    return Array.from(menu.querySelectorAll('[role="menuitem"]:not([aria-disabled="true"])'));
  }

  function isOpen() {
    return !!menu && (native ? menu.matches(':popover-open') : !menu.hidden);
  }

  function place() {
    const box = anchor.getBoundingClientRect();
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    let left = box.right - width;
    if (left < EDGE) left = Math.min(box.left, window.innerWidth - width - EDGE);
    let top = box.bottom + GAP;
    if (top + height > window.innerHeight - EDGE && box.top - GAP - height > EDGE) top = box.top - GAP - height;
    menu.style.setProperty('left', `${Math.max(EDGE, left)}px`);
    menu.style.setProperty('top', `${Math.max(EDGE, top)}px`);
  }

  function itemNode(item) {
    if (item === 'separator') return AD.dom.el('div', { class: 'menu-sep', role: 'separator' });
    if (item.note) return AD.dom.el('p', { class: 'menu-note', text: item.note });
    const disabled = !!item.disabled;
    const button = AD.dom.el('button', {
      type: 'button', role: 'menuitem', tabindex: -1, class: ['menu-item', item.danger && 'is-danger'],
      'aria-disabled': disabled ? 'true' : null, title: item.title,
    }, item.icon ? AD.dom.svgIcon(item.icon) : null, AD.dom.el('span', { text: item.label }));
    button.addEventListener('click', () => {
      if (disabled) return;
      // Focus goes back to the trigger first, so a sheet the item opens returns focus there too.
      close(true);
      item.run();
    });
    return button;
  }

  /** Open the menu under `trigger` with items [{label, icon, run, disabled, danger, title} |
   * 'separator' | {note}]. options.label names the menu for assistive technology. */
  function open(trigger, list, options = {}) {
    if (!node()) return;
    if (isOpen()) {
      const same = anchor === trigger;
      close(false);
      if (same) return;
    }
    anchor = trigger;
    anchorKey = trigger.getAttribute('data-keep');
    menu.replaceChildren(...list.map(itemNode));
    menu.setAttribute('aria-label', options.label || 'Actions');
    trigger.setAttribute('aria-expanded', 'true');
    if (native) menu.showPopover();
    else {
      menu.hidden = false;
      menu.classList.add('is-open');
      window.setTimeout(() => document.addEventListener('mousedown', outside), 0);
    }
    place();
    const first = items()[0];
    if (first) first.focus();
    else menu.focus();
  }

  function outside(event) {
    if (menu && !menu.contains(event.target)) close(false);
  }

  /** Close the menu; `refocus` (default true) returns focus to the button that opened it. */
  function close(refocus = true) {
    if (!isOpen()) return;
    if (native) menu.hidePopover();
    else {
      menu.hidden = true;
      menu.classList.remove('is-open');
      document.removeEventListener('mousedown', outside);
      finish();
    }
    if (refocus) restoreFocus();
  }

  function trigger() {
    if (anchor && anchor.isConnected) return anchor;
    return anchorKey ? document.querySelector(`[data-keep="${CSS.escape(anchorKey)}"]`) : null;
  }

  function finish() {
    const owner = trigger();
    if (owner) owner.setAttribute('aria-expanded', 'false');
    // Light dismiss (a click elsewhere) keeps the focus where the click put it; Esc returns it.
    if (menu && (menu.contains(document.activeElement) || document.activeElement === document.body)) restoreFocus();
  }

  function restoreFocus() {
    const owner = trigger();
    if (owner) owner.focus({ preventScroll: true });
  }

  function onKeydown(event) {
    const list = items();
    const index = list.indexOf(document.activeElement);
    let next = null;
    if (event.key === 'ArrowDown') next = list[(index + 1) % list.length];
    else if (event.key === 'ArrowUp') next = list[(index - 1 + list.length) % list.length];
    else if (event.key === 'Home') next = list[0];
    else if (event.key === 'End') next = list[list.length - 1];
    else if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
      return;
    } else if (event.key === 'Tab') {
      event.preventDefault();
      close(true);
      return;
    } else if (event.key.length === 1 && /\S/.test(event.key)) {
      const letter = event.key.toLowerCase();
      const order = list.slice(index + 1).concat(list.slice(0, index + 1));
      next = order.find((item) => item.textContent.trim().toLowerCase().startsWith(letter)) || null;
    }
    if (next) {
      event.preventDefault();
      next.focus();
    }
  }

  window.addEventListener('resize', () => close(false));

  AD.menu = { open, close, isOpen };
})();
