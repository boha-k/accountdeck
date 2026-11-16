/* AccountDeck 2 — DOM helpers: element builders, icons, keyed patching, the one modal (sheet or
 * alert), toasts, the live region, keyboard shortcuts and Hide emails.
 * Everything builds DOM with textContent and the CSSOM only: account labels are untrusted, and the
 * CSP forbids inline style attributes (element.style.setProperty is allowed). */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};

  // The SVG namespace identifier: a name, never fetched.
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const PROPERTIES = new Set(['value', 'checked', 'disabled', 'hidden', 'selected', 'indeterminate']);
  const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), '
    + 'textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
  const TOAST_MS = 6500;
  const MAX_TOASTS = 3;
  const TOAST_ICON = { info: 'info', good: 'check-circle', warning: 'warning', critical: 'warning' };
  const HIDE_EMAILS_KEY = 'ad2.hideEmails';
  const EMAIL = /([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9])[A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*\.([A-Za-z]{2,})/g;

  /* Icons in the spirit of SF Symbols: a 16 × 16 box, 1.5 px round strokes in currentColor. An object
   * entry may set `fill` (a filled glyph), `box` (another view box) or `sw` (stroke width). The
   * provider marks are the Claude and OpenAI logos from Simple Icons (CC0) on a 24 × 24 box. */
  const CIRCLE = 'M8 1.75a6.25 6.25 0 1 0 0 12.5 6.25 6.25 0 0 0 0-12.5z';
  const ICONS = {
    check: 'M3.5 8.5l3 3 6-7',
    'check-circle': `${CIRCLE}M5.25 8.25l1.9 1.9 3.6-4.1`,
    warning: 'M8 2.25l6.25 11.25H1.75zM8 6.5v3M8 11.75v.01',
    info: `${CIRCLE}M8 7.25V11M8 5v.01`,
    lock: 'M4.5 7h7a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1h-7a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1zM5.5 7V5a2.5 2.5 0 0 1 5 0v2',
    stop: `${CIRCLE}M6 6h4v4H6z`,
    refresh: 'M2.5 8a5.5 5.5 0 0 1 9.4-3.9l1.6 1.4M13.5 2.5v3h-3M13.5 8a5.5 5.5 0 0 1-9.4 3.9L2.5 10.5M2.5 13.5v-3h3',
    clock: `${CIRCLE}M8 4.5V8l2.5 1.5`,
    card: 'M2.75 3.75h10.5a1.25 1.25 0 0 1 1.25 1.25v6a1.25 1.25 0 0 1-1.25 1.25H2.75A1.25 1.25 0 0 1 1.5 11V5a1.25 1.25 0 0 1 1.25-1.25zM1.5 6.75h13M4 9.5h2.5',
    pulse: 'M1.5 8.5h3l1.5-4 3 7 1.5-3h4',
    hourglass: 'M4.5 2h7M4.5 14h7M5.5 2v1.5c0 1.6 1.2 2.9 2.5 4.5-1.3 1.6-2.5 2.9-2.5 4.5V14M10.5 2v1.5c0 1.6-1.2 2.9-2.5 4.5 1.3 1.6 2.5 2.9 2.5 4.5V14',
    bolt: 'M9 1.5L3.5 9H8l-1 5.5L12.5 7H8z',
    spark: 'M7 2.5l1.2 3.3 3.3 1.2-3.3 1.2L7 11.5 5.8 8.2 2.5 7l3.3-1.2zM12.5 10.5l.5 1.5 1.5.5-1.5.5-.5 1.5-.5-1.5-1.5-.5 1.5-.5z',
    x: 'M4 4l8 8M12 4l-8 8',
    gear: 'M6.71 3.17L6.97 1.48A6.6 6.6 0 0 1 9.03 1.48L9.29 3.17A5 5 0 0 1 10.5 3.67L11.88 2.66A6.6 6.6 0 0 1 13.34 4.12'
      + 'L12.33 5.5A5 5 0 0 1 12.83 6.71L14.52 6.97A6.6 6.6 0 0 1 14.52 9.03L12.83 9.29A5 5 0 0 1 12.33 10.5L13.34 11.88'
      + 'A6.6 6.6 0 0 1 11.88 13.34L10.5 12.33A5 5 0 0 1 9.29 12.83L9.03 14.52A6.6 6.6 0 0 1 6.97 14.52L6.71 12.83'
      + 'A5 5 0 0 1 5.5 12.33L4.12 13.34A6.6 6.6 0 0 1 2.66 11.88L3.67 10.5A5 5 0 0 1 3.17 9.29L1.48 9.03'
      + 'A6.6 6.6 0 0 1 1.48 6.97L3.17 6.71A5 5 0 0 1 3.67 5.5L2.66 4.12A6.6 6.6 0 0 1 4.12 2.66L5.5 3.67'
      + 'A5 5 0 0 1 6.71 3.17ZM10 8a2 2 0 1 1-4 0 2 2 0 0 1 4 0z',
    sun: 'M8 5.25a2.75 2.75 0 1 0 0 5.5 2.75 2.75 0 0 0 0-5.5zM8 1.5V3M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1.06 1.06'
      + 'M11.54 11.54l1.06 1.06M3.4 12.6l1.06-1.06M11.54 4.46l1.06-1.06',
    moon: 'M13.5 9.6A5.75 5.75 0 1 1 6.4 2.5a4.5 4.5 0 0 0 7.1 7.1z',
    'circle-half': `${CIRCLE}M8 1.75v12.5`,
    keyboard: 'M2.25 4h11.5a.75.75 0 0 1 .75.75v6.5a.75.75 0 0 1-.75.75H2.25a.75.75 0 0 1-.75-.75v-6.5A.75.75 0 0 1 2.25 4z'
      + 'M4.25 6.5h.01M6.75 6.5h.01M9.25 6.5h.01M11.75 6.5h.01M5 9.5h6',
    ellipsis: { d: 'M3.75 8h.01M8 8h.01M12.25 8h.01', sw: 2.25 },
    chevron: 'M4 6l4 4 4-4',
    'chevron-up': 'M4 10l4-4 4 4',
    chevrons: 'M4.75 6.25L8 3l3.25 3.25M4.75 9.75L8 13l3.25-3.25',
    external: 'M9.5 2.5h4v4M13.5 2.5L8 8M11.5 9.5V13a.5.5 0 0 1-.5.5H3a.5.5 0 0 1-.5-.5V5a.5.5 0 0 1 .5-.5h3.5',
    copy: 'M6.5 5.5h6a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-6a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1zM3.5 10.5h-.25a.75.75 0 0 1-.75-.75v-6.5a.75.75 0 0 1 .75-.75h6.5a.75.75 0 0 1 .75.75v.25',
    folder: 'M2 4.5a1 1 0 0 1 1-1h3.2l1.5 1.5H13a1 1 0 0 1 1 1v6.5a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1z',
    play: 'M5 3.5v9l7-4.5z',
    pause: 'M5.5 3.5v9M10.5 3.5v9',
    plus: 'M8 3v10M3 8h10',
    'plus-circle': `${CIRCLE}M8 5.25v5.5M5.25 8h5.5`,
    'minus-circle': `${CIRCLE}M5.25 8h5.5`,
    pencil: 'M10.6 2.4l3 3L6 13H3v-3zM9.1 3.9l3 3',
    trash: 'M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.4a1 1 0 0 0 1 .9h3.8a1 1 0 0 0 1-.9l.6-8.4M6.75 7v4.5M9.25 7v4.5',
    crown: 'M3 12.5h10M3 12.5L2 5.25l3.6 2.9L8 3.5l2.4 4.65L14 5.25l-1 7.25',
    terminal: 'M2.5 3.5h11a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1zM4.5 6l2 2-2 2M8 10h3.5',
    bell: 'M4 11V7.25a4 4 0 0 1 8 0V11l1.25 1.5H2.75zM6.5 14.25h3',
    menubar: 'M3 2.5h10a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1zM2 5.5h12M10 4h2',
    sliders: 'M2.5 4.5h6.5M12.5 4.5h1M2.5 11.5h1.5M7.5 11.5h6M10.75 3v3M5.75 10v3',
    import: 'M8 2.5v7M5 6.5l3 3 3-3M2.5 10.5v2a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-2',
    'arrow-down-circle': `${CIRCLE}M8 4.75v6.25M5.5 8.75L8 11.25l2.5-2.5`,
    // Antigravity's mark (our own glyph, not Google's logo): a ball floating over its shadow.
    antigravity: 'M8 2.25a3.25 3.25 0 1 0 0 6.5 3.25 3.25 0 0 0 0-6.5zM4.25 13.5h7.5M6.25 11.25h3.5',
    power: 'M8 2v5.5M4.6 4.2a5 5 0 1 0 6.8 0',
    doc: 'M4 1.75h5l3 3v9a.5.5 0 0 1-.5.5h-7a.5.5 0 0 1-.5-.5V2.25a.5.5 0 0 1 .5-.5zM9 1.75v3h3M5.75 8h4.5M5.75 10.5h4.5',
    undo: 'M5.5 3L2.5 6l3 3M2.5 6H10a3.5 3.5 0 0 1 0 7H7',
    // A limit reset: a circle that comes round again (arrow.counterclockwise).
    reset: 'M2.75 8a5.25 5.25 0 1 0 1.54-3.71M2.75 2.5v2.75H5.5',
    claude: { fill: true, box: 24, d: 'm4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z' },
    codex: { fill: true, box: 24, d: 'M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z' },
  };

  // ---- Building elements ------------------------------------------------------------------

  function append(parent, child) {
    if (child === null || child === undefined || child === false || child === true) return;
    if (Array.isArray(child)) {
      for (const item of child) append(parent, item);
      return;
    }
    parent.appendChild(typeof child === 'object' && child.nodeType ? child : document.createTextNode(String(child)));
  }

  function kebab(name) {
    return name.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
  }

  function applyProps(node, props) {
    if (!props) return;
    if (typeof props === 'string') {
      node.setAttribute('class', props);
      return;
    }
    for (const [name, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (name === 'class') {
        const names = Array.isArray(value) ? value.filter(Boolean).join(' ') : String(value);
        if (names) node.setAttribute('class', names);
      } else if (name === 'text') {
        node.textContent = String(value);
      } else if (name === 'dataset') {
        for (const [key, item] of Object.entries(value)) {
          if (item !== undefined && item !== null && item !== false) node.setAttribute(`data-${kebab(key)}`, String(item));
        }
      } else if (name === 'style') {
        for (const [key, item] of Object.entries(value)) {
          if (item !== undefined && item !== null) node.style.setProperty(key, String(item));
        }
      } else if (name === 'on') {
        for (const [type, listener] of Object.entries(value)) node.addEventListener(type, listener);
      } else if (PROPERTIES.has(name)) {
        node[name] = value;
      } else {
        node.setAttribute(name, value === true ? '' : String(value));
      }
    }
  }

  /** el('p', {class: 'muted', dataset: {id}}, 'text', node, [more]) — props may also be a class string.
   * Props: class (string | array), text, dataset, style (CSSOM custom properties), on (listeners),
   * value/checked/disabled/hidden/selected (properties), anything else as an attribute (true → ""). */
  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    applyProps(node, props);
    append(node, children);
    return node;
  }

  /** A push button. options: kind ('primary' | 'ghost' | 'danger' | 'icon' | 'link'), icon, small,
   * large, label (aria-label; required for icon-only buttons), title, disabled, className, dataset,
   * attrs. A handler that returns a promise keeps the button busy until it settles, so one click
   * never sends the same action twice. */
  function button(text, onClick, options = {}) {
    const kind = options.kind;
    let classes;
    if (kind === 'icon') classes = ['icon-button'];
    else if (kind === 'link') classes = ['link-button'];
    else classes = ['button', kind && `button-${kind}`, options.small && 'button-small', options.large && 'button-large'];
    classes.push(options.className);
    const node = el('button', Object.assign({
      type: 'button', class: classes, title: options.title, 'aria-label': options.label,
      disabled: options.disabled, dataset: options.dataset,
    }, options.attrs));
    if (options.icon) node.appendChild(svgIcon(options.icon));
    if (text) node.appendChild(document.createTextNode(text));
    if (onClick) node.addEventListener('click', (event) => run(node, onClick, event));
    return node;
  }

  function run(node, handler, event) {
    if (node.getAttribute('aria-busy') === 'true') return;
    const result = handler(event);
    if (!result || typeof result.then !== 'function') return;
    node.setAttribute('aria-busy', 'true');
    node.classList.add('is-busy');
    const done = () => {
      node.removeAttribute('aria-busy');
      node.classList.remove('is-busy');
    };
    result.then(done, (error) => {
      done();
      toast(error && error.message ? error.message : AD.format.STRINGS.connection.failed, { level: 'critical' });
    });
  }

  /** An inline SVG icon; unknown names give an empty icon. options: size (16 | 20), className,
   * label (makes it a labelled image instead of decoration). */
  function svgIcon(name, options = {}) {
    const def = ICONS[name === 'claude_cli' ? 'claude' : name];
    const svg = document.createElementNS(SVG_NS, 'svg');
    const box = def && def.box ? def.box : 16;
    svg.setAttribute('viewBox', `0 0 ${box} ${box}`);
    svg.setAttribute('class', ['icon', options.size === 20 && 'icon-20', options.className].filter(Boolean).join(' '));
    svg.setAttribute('data-icon', name);
    if (options.label) {
      svg.setAttribute('role', 'img');
      svg.setAttribute('aria-label', options.label);
    } else {
      svg.setAttribute('aria-hidden', 'true');
    }
    svg.setAttribute('focusable', 'false');
    if (!def) return svg;
    const spec = typeof def === 'string' ? { d: def } : def;
    if (spec.fill) {
      svg.setAttribute('fill', 'currentColor');
    } else {
      svg.setAttribute('fill', 'none');
      svg.setAttribute('stroke', 'currentColor');
      svg.setAttribute('stroke-width', String(spec.sw || 1.5));
      svg.setAttribute('stroke-linecap', 'round');
      svg.setAttribute('stroke-linejoin', 'round');
    }
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', spec.d);
    svg.appendChild(path);
    return svg;
  }

  /** A monogram avatar for an account (decorative: the name always sits beside it).
   * options: size ('small' | 'large'), active, muted. */
  function avatar(label, options = {}) {
    const first = Array.from(String(label || '').trim())[0] || '?';
    return el('span', { class: ['avatar', options.size && `avatar-${options.size}`, options.active && 'is-active',
      options.muted && 'is-muted'], 'aria-hidden': 'true', text: first.toUpperCase() });
  }

  // ---- Keyed patching -----------------------------------------------------------------------

  function keepKey(node) {
    return node.getAttribute && node.getAttribute('data-keep');
  }

  function findKept(root, key) {
    if (keepKey(root) === key) return root;
    for (const node of root.querySelectorAll('[data-keep]')) {
      if (node.getAttribute('data-keep') === key) return node;
    }
    return null;
  }

  /** Replace `old` with `next` as if it had been patched: elements that carry the same `data-keep`
   * value in both trees are the same control, so the focus and a <details> element's open state carry
   * over. Without a matching key, focus inside `old` moves to `next` when it is focusable. */
  function swap(old, next) {
    if (old === next) return next;
    const active = document.activeElement;
    const focusKey = active && old.contains(active) ? (keepKey(active) || '') : null;
    for (const details of old.querySelectorAll('details[data-keep]')) {
      const twin = findKept(next, details.getAttribute('data-keep'));
      if (twin && twin.tagName === 'DETAILS') twin.open = details.open;
    }
    old.replaceWith(next);
    if (focusKey !== null) {
      const target = (focusKey && findKept(next, focusKey)) || (next.tabIndex >= 0 ? next : null);
      if (target) target.focus({ preventScroll: true });
    }
    return next;
  }

  /** Reconcile container's children with items, keyed by `key` (a property name or item → key).
   * New keys get render(item, index); existing ones get update(node, item, index), which may return a
   * replacement node (swapped in with swap(), so focus and open <details> survive). Nodes move only
   * when their position changed, focus survives a move, and any unkeyed child is removed. */
  function patchList(container, items, key, render, update) {
    const focused = document.activeElement;
    const existing = new Map();
    for (const child of Array.from(container.children)) {
      const id = child.getAttribute('data-key');
      if (id === null || existing.has(id)) child.remove();
      else existing.set(id, child);
    }
    const kept = new Set();
    let previous = null;
    items.forEach((item, index) => {
      const id = String(typeof key === 'function' ? key(item, index) : item[key]);
      let node = existing.get(id);
      if (node) {
        const replacement = update ? update(node, item, index) : null;
        if (replacement && replacement !== node) node = swap(node, replacement);
      } else {
        node = render(item, index);
      }
      node.setAttribute('data-key', id);
      kept.add(id);
      const expected = previous ? previous.nextSibling : container.firstChild;
      if (node !== expected) container.insertBefore(node, expected);
      previous = node;
    });
    for (const [id, node] of existing) {
      if (!kept.has(id)) node.remove();
    }
    if (focused && focused !== document.body && focused.isConnected && document.activeElement !== focused) {
      focused.focus({ preventScroll: true });
    }
    return container;
  }

  /** Patch `container` with items {key, sig, build}: a node is rebuilt only when its signature changed. */
  const sigs = new WeakMap();
  function patchSigned(container, items) {
    const make = (item) => {
      const node = item.build();
      sigs.set(node, item.sig);
      return node;
    };
    return patchList(container, items, 'key', make, (node, item) => (sigs.get(node) === item.sig ? null : make(item)));
  }

  // ---- Modal: a sheet (default), a wide sheet or an alert --------------------------------------

  let modal = null;   // {view, closable, onClose, trigger, release} while open

  function part(id) {
    return document.getElementById(id);
  }

  function nodes(content) {
    if (content === null || content === undefined) return [];
    return Array.isArray(content) ? content.flat(Infinity).filter((item) => item !== null && item !== undefined && item !== false)
      : [content];
  }

  function focusables(root) {
    return Array.from(root.querySelectorAll(FOCUSABLE)).filter((node) => !node.closest('[hidden]') && !node.closest('[inert]'));
  }

  function focusInitial(dialog) {
    const target = dialog.querySelector('[data-autofocus]')
      || focusables(part('modal-body'))[0]
      || focusables(part('modal-actions'))[0]
      || dialog;
    target.focus();
  }

  /** Open the one modal (or replace what an open modal shows; the replaced view's onClose is not called).
   * `view` names what is shown (modalView() reports it); `size` is 'alert', 'wide' or a sheet by
   * default. closable: false hides ✕ and ignores Esc, ⌘. and backdrop clicks. Focus goes to
   * [data-autofocus] or the first control, and back to the trigger on close. */
  function openModal({ title, body, actions, closable = true, onClose, view = 'dialog', size } = {}) {
    const dialog = part('modal');
    part('modal-title').textContent = title || '';
    part('modal-body').replaceChildren(...nodes(body));
    const bar = part('modal-actions');
    bar.replaceChildren(...nodes(actions));
    bar.hidden = !bar.childElementCount;
    // An alert has no close button (its CSS hides it): hidden here too, so Tab never lands on a button nobody sees.
    part('modal-close').hidden = !closable || size === 'alert';
    dialog.setAttribute('data-view', view);
    if (size) dialog.setAttribute('data-size', size);
    else dialog.removeAttribute('data-size');
    if (modal && dialog.open) {
      Object.assign(modal, { view, closable, onClose });
    } else {
      const trigger = document.activeElement;
      modal = { view, closable, onClose, trigger, release: null };
      dialog.showModal();
      modal.release = focusTrap(dialog);
    }
    focusInitial(dialog);
    document.dispatchEvent(new CustomEvent('ad:modal', { detail: { open: true, view } }));
    return dialog;
  }

  function finishModal() {
    const closing = modal;
    if (!closing) return;
    modal = null;
    const dialog = part('modal');
    dialog.removeAttribute('data-view');
    if (closing.release) closing.release();
    part('modal-body').replaceChildren();
    part('modal-actions').replaceChildren();
    if (closing.trigger && closing.trigger.isConnected && typeof closing.trigger.focus === 'function') closing.trigger.focus();
    if (closing.onClose) closing.onClose();
    document.dispatchEvent(new CustomEvent('ad:modal', { detail: { open: false, view: closing.view } }));
  }

  /** Close the modal; its onClose runs once. Returns false when nothing was open. */
  function closeModal() {
    const dialog = part('modal');
    if (!dialog || !dialog.open) return false;
    dialog.close();
    finishModal();
    return true;
  }

  /** Replace only the body of the open modal (progress updates); focus moves only if it was lost. */
  function setModalBody(content) {
    const body = part('modal-body');
    const hadFocus = body.contains(document.activeElement);
    body.replaceChildren(...nodes(content));
    if (hadFocus && !body.contains(document.activeElement)) focusInitial(part('modal'));
  }

  /** Replace only the action bar of the open modal (a progress view whose Cancel comes and goes). */
  function setModalActions(actions) {
    const bar = part('modal-actions');
    const hadFocus = bar.contains(document.activeElement);
    bar.replaceChildren(...nodes(actions));
    bar.hidden = !bar.childElementCount;
    if (hadFocus && !bar.contains(document.activeElement)) focusInitial(part('modal'));
  }

  /** What the modal shows (the `view` given to openModal), or null when it is closed. */
  function modalView() {
    const dialog = part('modal');
    return dialog && dialog.open && modal ? modal.view : null;
  }

  /** Close a dialog the way a click outside it would, if the press both started and ended outside. */
  function dismissOnBackdrop(dialog, close) {
    let pressedOutside = false;
    const outside = (event) => {
      const box = dialog.getBoundingClientRect();
      return event.target === dialog && (event.clientX < box.left || event.clientX > box.right
        || event.clientY < box.top || event.clientY > box.bottom);
    };
    dialog.addEventListener('mousedown', (event) => { pressedOutside = outside(event); });
    dialog.addEventListener('click', (event) => {
      if (pressedOutside && outside(event)) close();
      pressedOutside = false;
    });
  }

  function bindModal() {
    const dialog = part('modal');
    if (!dialog || dialog.getAttribute('data-bound')) return;
    dialog.setAttribute('data-bound', 'true');
    dialog.addEventListener('cancel', (event) => {
      if (modal && !modal.closable) event.preventDefault();
    });
    // Esc (after the cancel check) and other native closes; a close event that arrives after a new
    // modal was opened belongs to the old one and is ignored.
    dialog.addEventListener('close', () => {
      if (!dialog.open) finishModal();
    });
    // ⌘. cancels, as in every Mac dialog.
    dialog.addEventListener('keydown', (event) => {
      if (event.metaKey && event.key === '.' && modal && modal.closable) {
        event.preventDefault();
        closeModal();
      }
    });
    dismissOnBackdrop(dialog, () => { if (modal && modal.closable) closeModal(); });
    part('modal-close').addEventListener('click', () => closeModal());
  }

  /** Keep Tab and Shift+Tab inside `root`; returns the function that releases the trap. */
  function focusTrap(root) {
    const onKeydown = (event) => {
      if (event.key !== 'Tab') return;
      const items = focusables(root);
      if (!items.length) {
        event.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !root.contains(active) || active === root)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !root.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    root.addEventListener('keydown', onKeydown);
    return () => root.removeEventListener('keydown', onKeydown);
  }

  // ---- Toasts and announcements --------------------------------------------------------------

  /** A notification banner at the top right: role="status", gone after 6.5 s (paused while hovered or
   * focused). level 'critical' is an error unless sticky is false: role="alert", stays until
   * dismissed. options: level (info | good | warning | critical), sticky, title. */
  function toast(message, { level = 'info', sticky, title } = {}) {
    const region = part('toasts');
    if (!region) return null;
    const keep = sticky === undefined ? level === 'critical' : !!sticky;
    const error = level === 'critical' && keep;
    const node = el('div', { class: ['toast', `toast-${level}`], role: error ? 'alert' : 'status', dataset: { level } },
      svgIcon(TOAST_ICON[level] || 'info', { className: 'toast-icon' }),
      el('div', 'toast-text', title ? el('strong', { class: 'toast-title', text: title }) : null, el('span', { text: message })));
    let timer = null;
    const dismiss = () => {
      window.clearTimeout(timer);
      node.remove();
    };
    const arm = () => {
      window.clearTimeout(timer);
      if (!keep) timer = window.setTimeout(dismiss, TOAST_MS);
    };
    node.appendChild(button('', dismiss, { kind: 'icon', icon: 'x', label: 'Dismiss', className: 'toast-close' }));
    node.addEventListener('mouseenter', () => window.clearTimeout(timer));
    node.addEventListener('mouseleave', arm);
    node.addEventListener('focusin', () => window.clearTimeout(timer));
    node.addEventListener('focusout', arm);
    region.appendChild(node);
    while (region.children.length > MAX_TOASTS) region.firstElementChild.remove();
    arm();
    return node;
  }

  /** Say `text` through the one polite live region (#announce). Each sentence is a new child, so a
   * repeated sentence is announced again; only the last three are kept. */
  function announce(text) {
    const region = part('announce');
    if (!region || !text) return;
    region.appendChild(el('p', { text }));
    while (region.children.length > 3) region.firstElementChild.remove();
  }

  // ---- Keyboard shortcuts ---------------------------------------------------------------------

  function typing(target) {
    return !!target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
  }

  /** Bind document keys. Map keys are event.key values (letters lower-cased), with a "Meta+" prefix
   * for ⌘ combinations: "r", "?", "1", "Meta+r", "Meta+,". Plain keys are ignored while typing in a
   * field or while a dialog is open; ⌘ combinations always reach their handler (which decides), so
   * the browser never acts on a key the page owns. Control combinations are never taken. A handler
   * that returns false leaves the key alone. Returns the function that unbinds the map. */
  function bindShortcuts(map) {
    const onKeydown = (event) => {
      if (event.defaultPrevented || event.ctrlKey || event.altKey || event.isComposing) return;
      const key = event.key && event.key.length === 1 ? event.key.toLowerCase() : event.key;
      const combo = (event.metaKey ? 'Meta+' : '') + key;
      const handler = map[combo];
      if (!handler) return;
      if (!event.metaKey && (typing(event.target) || document.querySelector('dialog[open]'))) return;
      if (handler(event) !== false) event.preventDefault();
    };
    document.addEventListener('keydown', onKeydown);
    return () => document.removeEventListener('keydown', onKeydown);
  }

  // ---- Hide emails ----------------------------------------------------------------------------

  function storedHideEmails() {
    try {
      return window.localStorage.getItem(HIDE_EMAILS_KEY) === '1';
    } catch (err) {
      return false;
    }
  }

  let hidingEmails = storedHideEmails();

  /** Whether Hide emails is on (client-side only, localStorage["ad2.hideEmails"]). */
  function hideEmails() {
    return hidingEmails;
  }

  /** Turn Hide emails on or off for this browser; fires `ad:hide-emails` so the page re-renders. */
  function setHideEmails(on) {
    hidingEmails = !!on;
    try {
      window.localStorage.setItem(HIDE_EMAILS_KEY, hidingEmails ? '1' : '0');
    } catch (err) {
      // Storage can be blocked; the choice then lasts for this page only.
    }
    document.dispatchEvent(new CustomEvent('ad:hide-emails', { detail: { on: hidingEmails } }));
  }

  /** `text` with every email masked ("alpha@example.test" → "a•••@e•••.test") while Hide emails is on,
   * or always when `force` is true; otherwise unchanged. null/undefined give "". */
  function maskEmail(text, force) {
    if (text === null || text === undefined) return '';
    const value = String(text);
    if (!force && !hidingEmails) return value;
    return value.replace(EMAIL, (match, first, domain, tld) => `${first}•••@${domain}•••.${tld}`);
  }

  window.addEventListener('storage', (event) => {
    if (event.key !== HIDE_EMAILS_KEY) return;
    const next = storedHideEmails();
    if (next === hidingEmails) return;
    hidingEmails = next;
    document.dispatchEvent(new CustomEvent('ad:hide-emails', { detail: { on: hidingEmails } }));
  });

  AD.dom = {
    el, button, svgIcon, avatar, patchList, patchSigned, swap, openModal, closeModal, setModalBody, setModalActions,
    modalView, dismissOnBackdrop, focusTrap, toast, announce, bindShortcuts, maskEmail, hideEmails, setHideEmails,
  };
  bindModal();
})();
