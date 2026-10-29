/* @ds-bundle: {"format":4,"namespace":"GDGlass","components":[{"name":"GlassSurface"},{"name":"Button"},{"name":"Card"},{"name":"Sidebar"},{"name":"TopBar"},{"name":"NavBar"},{"name":"TabBar"},{"name":"Menu"},{"name":"Modal"},{"name":"SegmentedControl"},{"name":"Select"},{"name":"Switch"},{"name":"TextField"},{"name":"SearchField"},{"name":"PeoplePicker"},{"name":"LinkField"},{"name":"OptionCards"},{"name":"Dropzone"},{"name":"FileRow"},{"name":"ProgressBar"},{"name":"Tag"},{"name":"SeverityBadge"},{"name":"Avatar"},{"name":"StatCard"},{"name":"EmptyState"},{"name":"Skeleton"},{"name":"Tooltip"},{"name":"Toast"},{"name":"Sheet"},{"name":"Icon"},{"name":"Prose"},{"name":"FileTree"},{"name":"Breadcrumbs"},{"name":"SelectionToolbar"},{"name":"CommentThread"},{"name":"DecisionList"},{"name":"QuestionBlock"},{"name":"DocLinks"},{"name":"ChatMessage"},{"name":"ChatComposer"},{"name":"ConnectorCard"},{"name":"DiagramNode"},{"name":"ArchitectureDiagram"},{"name":"ProcessFlow"},{"name":"FlowCanvas"}]} */
(function () {
  var React = window.React, h = React.createElement, useState = React.useState, useRef = React.useRef, useLayoutEffect = React.useLayoutEffect;
  function cx() { return Array.prototype.slice.call(arguments).filter(Boolean).join(' '); }
  function omit(o, keys) { var r = {}; for (var k in o) if (keys.indexOf(k) < 0) r[k] = o[k]; return r; }
  var ASSETS = (window.GDGlassAssets || '../assets/'); var LOGO = ASSETS + 'Logos/Full%20color,%20black/SVG/full-color-black-horizontal.svg'; var LOGO_DARK = ASSETS + 'Logos/Full%20color,%20white/SVG/full-color-white-horizontal.svg';

  var GlassSurface = React.forwardRef(function GlassSurface(p, ref) {
    var v = p.variant || 'regular', as = p.as || 'div';
    return h(as, Object.assign(omit(p, ['variant', 'as', 'ribbed', 'floating', 'className', 'children']), {
      ref: ref, className: cx('gdg-glass', v !== 'regular' && 'gdg-glass--' + v, p.ribbed && 'gdg-glass--ribbed', p.floating && 'gdg-glass--float', p.className)
    }), p.children);
  });

  function Arrow() {
    return h('svg', { className: 'gdg-arrow', viewBox: '0 0 16 16', fill: 'none', 'aria-hidden': true },
      h('path', { d: 'M2 8h11M9 4l4 4-4 4', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round' }));
  }
  function Button(p) {
    var v = p.variant || 'primary', s = p.size || 'md';
    return h('button', Object.assign({ type: 'button' }, omit(p, ['variant', 'size', 'arrow', 'className', 'children']), {
      className: cx('gdg-btn', 'gdg-btn--' + v, s !== 'md' && 'gdg-btn--' + s, p.className)
    }), p.children, p.arrow ? h(Arrow) : null);
  }

  function Card(p) {
    return h(GlassSurface, { variant: p.variant || 'regular', className: cx('gdg-card', p.className), style: p.style },
      p.eyebrow ? h('p', { className: 'gdg-card__eyebrow' }, p.eyebrow) : null,
      p.title ? h('h3', { className: 'gdg-card__title' }, p.title) : null,
      p.children ? h('div', { className: 'gdg-card__body' }, p.children) : null,
      p.footer ? h('div', { className: 'gdg-card__footer' }, p.footer) : null);
  }

  function NavBar(p) {
    var links = p.links || [];
    return h(GlassSurface, { as: 'nav', className: 'gdg-nav', floating: true, 'aria-label': 'Main' },
      h('img', { className: 'gdg-nav__logo gdg-logo-light', src: p.logoSrc || LOGO, alt: 'Grid Dynamics' }),
      h('img', { className: 'gdg-nav__logo gdg-logo-dark', src: p.logoDarkSrc || LOGO_DARK, alt: 'Grid Dynamics' }),
      h('ul', { className: 'gdg-nav__links' }, links.map(function (l, i) {
        return h('li', { key: i }, h('a', { href: l.href || '#', 'aria-current': l.current ? 'page' : undefined }, l.label));
      })),
      p.action || null);
  }

  function useControlled(value, def, onChange) {
    var st = useState(def), inner = st[0], setInner = st[1];
    var cur = value !== undefined ? value : inner;
    return [cur, function (n) { if (value === undefined) setInner(n); if (onChange) onChange(n); }];
  }

  function SegmentedControl(p) {
    var opts = p.options || [];
    var s = useControlled(p.value, p.defaultValue !== undefined ? p.defaultValue : (opts[0] && opts[0].value), p.onChange), cur = s[0], set = s[1];
    var refs = useRef({}), lens = useState({ left: 4, width: 0 }), box = lens[0], setBox = lens[1];
    useLayoutEffect(function () {
      var el = refs.current[cur]; if (el) setBox({ left: el.offsetLeft, width: el.offsetWidth });
    }, [cur, opts.length]);
    return h(GlassSurface, { className: 'gdg-seg', role: 'group', 'aria-label': p.label },
      h('span', { className: 'gdg-seg__lens', style: { left: box.left + 'px', width: box.width + 'px' }, 'aria-hidden': true }),
      opts.map(function (o) {
        return h('button', { key: o.value, type: 'button', className: 'gdg-seg-btn', 'aria-pressed': cur === o.value,
          ref: function (el) { refs.current[o.value] = el; }, onClick: function () { set(o.value); } }, o.label);
      }));
  }

  function TabBar(p) {
    var tabs = p.tabs || [];
    var s = useControlled(p.value, p.defaultValue !== undefined ? p.defaultValue : (tabs[0] && tabs[0].value), p.onChange), cur = s[0], set = s[1];
    return h(GlassSurface, { className: 'gdg-tabbar', floating: true, role: 'tablist', 'aria-label': p.label || 'Sections' },
      tabs.map(function (t) {
        return h('button', { key: t.value, type: 'button', role: 'tab', className: 'gdg-tab', 'aria-selected': cur === t.value,
          onClick: function () { set(t.value); } }, t.label);
      }));
  }

  function Switch(p) {
    var s = useControlled(p.checked, !!p.defaultChecked, p.onChange), on = s[0], set = s[1];
    return h('label', { className: 'gdg-switch-row' },
      h('button', { type: 'button', role: 'switch', className: 'gdg-switch', 'aria-checked': on, disabled: p.disabled,
        onClick: function () { set(!on); } }),
      p.label ? h('span', null, p.label) : null);
  }

  var fid = 0;
  function TextField(p) {
    var idRef = useRef(p.id || ('gdg-f' + (++fid)));
    var id = idRef.current;
    return h('div', { className: cx('gdg-field', p.error && 'gdg-field--error', p.className) },
      h('label', { className: 'gdg-field__label', htmlFor: id }, p.label),
      h(p.multiline ? 'textarea' : 'input', Object.assign(omit(p, ['label', 'help', 'error', 'className', 'id', 'multiline']), { id: id, rows: p.multiline ? (p.rows || 3) : undefined, 'aria-invalid': p.error ? true : undefined,
        'aria-describedby': (p.error || p.help) ? id + '-h' : undefined })),
      (p.error || p.help) ? h('span', { className: 'gdg-field__help', id: id + '-h' }, p.error || p.help) : null);
  }

  function Tag(p) {
    var t = p.tone || 'neutral', status = t === 'info' || t === 'success' || t === 'danger' || t === 'warning';
    return h('span', { className: cx('gdg-tag', t !== 'neutral' && 'gdg-tag--' + t, p.className) },
      status ? h('span', { className: 'gdg-tag__dot', 'aria-hidden': true }) : t === 'pending' ? h('span', { className: 'gdg-tag__spin', 'aria-hidden': true }) : null, p.children);
  }

  function StatCard(p) {
    return h(GlassSurface, { className: 'gdg-stat', variant: p.variant || 'regular' },
      h('div', { className: 'gdg-stat__bar', 'aria-hidden': true }),
      h('div', { className: 'gdg-stat__value' }, p.value, p.unit ? h('span', { className: 'gdg-stat__unit' }, p.unit) : null),
      h('div', { className: 'gdg-stat__label' }, p.label));
  }

  function Sheet(p) {
    if (p.open === false) return null;
    return h('div', { className: 'gdg-sheet-wrap' },
      h('div', { className: 'gdg-sheet-scrim', onClick: p.onClose }),
      h(GlassSurface, { variant: 'strong', floating: true, className: 'gdg-sheet', role: 'dialog', 'aria-modal': true, 'aria-label': p.title },
        h('div', { className: 'gdg-sheet__grabber', 'aria-hidden': true }),
        p.title ? h('h2', { className: 'gdg-sheet__title' }, p.title) : null,
        h('div', { className: 'gdg-sheet__body' }, p.children),
        p.actions ? h('div', { className: 'gdg-sheet__actions' }, p.actions) : null));
  }

  window.GDGlass = Object.assign(window.GDGlass || {}, {
    GlassSurface: GlassSurface, Button: Button, Card: Card, NavBar: NavBar, TabBar: TabBar,
    SegmentedControl: SegmentedControl, Switch: Switch, TextField: TextField, Tag: Tag, StatCard: StatCard, Sheet: Sheet
  });
})();
/* ---- part 2: navigation + diagrams ---- */
(function () {
  var React = window.React, h = React.createElement, useState = React.useState;
  var G = window.GDGlass, GlassSurface = G.GlassSurface;
  function cx() { return Array.prototype.slice.call(arguments).filter(Boolean).join(' '); }
  var ICONS = {"house":["M219.31,108.68l-80-80a16,16,0,0,0-22.62,0l-80,80A15.87,15.87,0,0,0,32,120v96a8,8,0,0,0,8,8h64a8,8,0,0,0,8-8V160h32v56a8,8,0,0,0,8,8h64a8,8,0,0,0,8-8V120A15.87,15.87,0,0,0,219.31,108.68ZM208,208H160V152a8,8,0,0,0-8-8H104a8,8,0,0,0-8,8v56H48V120l80-80,80,80Z"],"squares-four":["M104,40H56A16,16,0,0,0,40,56v48a16,16,0,0,0,16,16h48a16,16,0,0,0,16-16V56A16,16,0,0,0,104,40Zm0,64H56V56h48v48Zm96-64H152a16,16,0,0,0-16,16v48a16,16,0,0,0,16,16h48a16,16,0,0,0,16-16V56A16,16,0,0,0,200,40Zm0,64H152V56h48v48Zm-96,32H56a16,16,0,0,0-16,16v48a16,16,0,0,0,16,16h48a16,16,0,0,0,16-16V152A16,16,0,0,0,104,136Zm0,64H56V152h48v48Zm96-64H152a16,16,0,0,0-16,16v48a16,16,0,0,0,16,16h48a16,16,0,0,0,16-16V152A16,16,0,0,0,200,136Zm0,64H152V152h48v48Z"],"tree-structure":["M160,112h48a16,16,0,0,0,16-16V48a16,16,0,0,0-16-16H160a16,16,0,0,0-16,16V64H128a24,24,0,0,0-24,24v32H72v-8A16,16,0,0,0,56,96H24A16,16,0,0,0,8,112v32a16,16,0,0,0,16,16H56a16,16,0,0,0,16-16v-8h32v32a24,24,0,0,0,24,24h16v16a16,16,0,0,0,16,16h48a16,16,0,0,0,16-16V160a16,16,0,0,0-16-16H160a16,16,0,0,0-16,16v16H128a8,8,0,0,1-8-8V88a8,8,0,0,1,8-8h16V96A16,16,0,0,0,160,112ZM56,144H24V112H56v32Zm104,16h48v48H160Zm0-112h48V96H160Z"],"flow-arrow":["M245.66,74.34l-32-32a8,8,0,0,0-11.32,11.32L220.69,72H208c-49.33,0-61.05,28.12-71.38,52.92-9.38,22.51-16.92,40.59-49.48,42.84a40,40,0,1,0,.1,16c43.26-2.65,54.34-29.15,64.14-52.69C161.41,107,169.33,88,208,88h12.69l-18.35,18.34a8,8,0,0,0,11.32,11.32l32-32A8,8,0,0,0,245.66,74.34ZM48,200a24,24,0,1,1,24-24A24,24,0,0,1,48,200Z"],"stack":["M230.91,172A8,8,0,0,1,228,182.91l-96,56a8,8,0,0,1-8.06,0l-96-56A8,8,0,0,1,36,169.09l92,53.65,92-53.65A8,8,0,0,1,230.91,172ZM220,121.09l-92,53.65L36,121.09A8,8,0,0,0,28,134.91l96,56a8,8,0,0,0,8.06,0l96-56A8,8,0,1,0,220,121.09ZM24,80a8,8,0,0,1,4-6.91l96-56a8,8,0,0,1,8.06,0l96,56a8,8,0,0,1,0,13.82l-96,56a8,8,0,0,1-8.06,0l-96-56A8,8,0,0,1,24,80Zm23.88,0L128,126.74,208.12,80,128,33.26Z"],"folder-simple":["M216,72H130.67L102.93,51.2a16.12,16.12,0,0,0-9.6-3.2H40A16,16,0,0,0,24,64V200a16,16,0,0,0,16,16H216.89A15.13,15.13,0,0,0,232,200.89V88A16,16,0,0,0,216,72Zm0,128H40V64H93.33L123.2,86.4A8,8,0,0,0,128,88h88Z"],"users":["M117.25,157.92a60,60,0,1,0-66.5,0A95.83,95.83,0,0,0,3.53,195.63a8,8,0,1,0,13.4,8.74,80,80,0,0,1,134.14,0,8,8,0,0,0,13.4-8.74A95.83,95.83,0,0,0,117.25,157.92ZM40,108a44,44,0,1,1,44,44A44.05,44.05,0,0,1,40,108Zm210.14,98.7a8,8,0,0,1-11.07-2.33A79.83,79.83,0,0,0,172,168a8,8,0,0,1,0-16,44,44,0,1,0-16.34-84.87,8,8,0,1,1-5.94-14.85,60,60,0,0,1,55.53,105.64,95.83,95.83,0,0,1,47.22,37.71A8,8,0,0,1,250.14,206.7Z"],"chart-bar":["M224,200h-8V40a8,8,0,0,0-8-8H152a8,8,0,0,0-8,8V80H96a8,8,0,0,0-8,8v40H48a8,8,0,0,0-8,8v64H32a8,8,0,0,0,0,16H224a8,8,0,0,0,0-16ZM160,48h40V200H160ZM104,96h40V200H104ZM56,144H88v56H56Z"],"gear-six":["M128,80a48,48,0,1,0,48,48A48.05,48.05,0,0,0,128,80Zm0,80a32,32,0,1,1,32-32A32,32,0,0,1,128,160Zm109.94-52.79a8,8,0,0,0-3.89-5.4l-29.83-17-.12-33.62a8,8,0,0,0-2.83-6.08,111.91,111.91,0,0,0-36.72-20.67,8,8,0,0,0-6.46.59L128,41.85,97.88,25a8,8,0,0,0-6.47-.6A112.1,112.1,0,0,0,54.73,45.15a8,8,0,0,0-2.83,6.07l-.15,33.65-29.83,17a8,8,0,0,0-3.89,5.4,106.47,106.47,0,0,0,0,41.56,8,8,0,0,0,3.89,5.4l29.83,17,.12,33.62a8,8,0,0,0,2.83,6.08,111.91,111.91,0,0,0,36.72,20.67,8,8,0,0,0,6.46-.59L128,214.15,158.12,231a7.91,7.91,0,0,0,3.9,1,8.09,8.09,0,0,0,2.57-.42,112.1,112.1,0,0,0,36.68-20.73,8,8,0,0,0,2.83-6.07l.15-33.65,29.83-17a8,8,0,0,0,3.89-5.4A106.47,106.47,0,0,0,237.94,107.21Zm-15,34.91-28.57,16.25a8,8,0,0,0-3,3c-.58,1-1.19,2.06-1.81,3.06a7.94,7.94,0,0,0-1.22,4.21l-.15,32.25a95.89,95.89,0,0,1-25.37,14.3L134,199.13a8,8,0,0,0-3.91-1h-.19c-1.21,0-2.43,0-3.64,0a8.08,8.08,0,0,0-4.1,1l-28.84,16.1A96,96,0,0,1,67.88,201l-.11-32.2a8,8,0,0,0-1.22-4.22c-.62-1-1.23-2-1.8-3.06a8.09,8.09,0,0,0-3-3.06l-28.6-16.29a90.49,90.49,0,0,1,0-28.26L61.67,97.63a8,8,0,0,0,3-3c.58-1,1.19-2.06,1.81-3.06a7.94,7.94,0,0,0,1.22-4.21l.15-32.25a95.89,95.89,0,0,1,25.37-14.3L122,56.87a8,8,0,0,0,4.1,1c1.21,0,2.43,0,3.64,0a8.08,8.08,0,0,0,4.1-1l28.84-16.1A96,96,0,0,1,188.12,55l.11,32.2a8,8,0,0,0,1.22,4.22c.62,1,1.23,2,1.8,3.06a8.09,8.09,0,0,0,3,3.06l28.6,16.29A90.49,90.49,0,0,1,222.9,142.12Z"],"question":["M140,180a12,12,0,1,1-12-12A12,12,0,0,1,140,180ZM128,72c-22.06,0-40,16.15-40,36v4a8,8,0,0,0,16,0v-4c0-11,10.77-20,24-20s24,9,24,20-10.77,20-24,20a8,8,0,0,0-8,8v8a8,8,0,0,0,16,0v-.72c18.24-3.35,32-17.9,32-35.28C168,88.15,150.06,72,128,72Zm104,56A104,104,0,1,1,128,24,104.11,104.11,0,0,1,232,128Zm-16,0a88,88,0,1,0-88,88A88.1,88.1,0,0,0,216,128Z"],"sign-out":["M120,216a8,8,0,0,1-8,8H48a8,8,0,0,1-8-8V40a8,8,0,0,1,8-8h64a8,8,0,0,1,0,16H56V208h56A8,8,0,0,1,120,216Zm109.66-93.66-40-40a8,8,0,0,0-11.32,11.32L204.69,120H112a8,8,0,0,0,0,16h92.69l-26.35,26.34a8,8,0,0,0,11.32,11.32l40-40A8,8,0,0,0,229.66,122.34Z"],"caret-double-left":["M205.66,202.34a8,8,0,0,1-11.32,11.32l-80-80a8,8,0,0,1,0-11.32l80-80a8,8,0,0,1,11.32,11.32L131.31,128ZM51.31,128l74.35-74.34a8,8,0,0,0-11.32-11.32l-80,80a8,8,0,0,0,0,11.32l80,80a8,8,0,0,0,11.32-11.32Z"],"caret-double-right":["M141.66,133.66l-80,80a8,8,0,0,1-11.32-11.32L124.69,128,50.34,53.66A8,8,0,0,1,61.66,42.34l80,80A8,8,0,0,1,141.66,133.66Zm80-11.32-80-80a8,8,0,0,0-11.32,11.32L204.69,128l-74.35,74.34a8,8,0,0,0,11.32,11.32l80-80A8,8,0,0,0,221.66,122.34Z"],"database":["M128,24C74.17,24,32,48.6,32,80v96c0,31.4,42.17,56,96,56s96-24.6,96-56V80C224,48.6,181.83,24,128,24Zm80,104c0,9.62-7.88,19.43-21.61,26.92C170.93,163.35,150.19,168,128,168s-42.93-4.65-58.39-13.08C55.88,147.43,48,137.62,48,128V111.36c17.06,15,46.23,24.64,80,24.64s62.94-9.68,80-24.64ZM69.61,53.08C85.07,44.65,105.81,40,128,40s42.93,4.65,58.39,13.08C200.12,60.57,208,70.38,208,80s-7.88,19.43-21.61,26.92C170.93,115.35,150.19,120,128,120s-42.93-4.65-58.39-13.08C55.88,99.43,48,89.62,48,80S55.88,60.57,69.61,53.08ZM186.39,202.92C170.93,211.35,150.19,216,128,216s-42.93-4.65-58.39-13.08C55.88,195.43,48,185.62,48,176V159.36c17.06,15,46.23,24.64,80,24.64s62.94-9.68,80-24.64V176C208,185.62,200.12,195.43,186.39,202.92Z"],"cloud":["M160,40A88.09,88.09,0,0,0,81.29,88.67,64,64,0,1,0,72,216h88a88,88,0,0,0,0-176Zm0,160H72a48,48,0,0,1,0-96c1.1,0,2.2,0,3.29.11A88,88,0,0,0,72,128a8,8,0,0,0,16,0,72,72,0,1,1,72,72Z"],"cube":["M223.68,66.15,135.68,18h0a15.88,15.88,0,0,0-15.36,0l-88,48.17a16,16,0,0,0-8.32,14v95.64a16,16,0,0,0,8.32,14l88,48.17a15.88,15.88,0,0,0,15.36,0l88-48.17a16,16,0,0,0,8.32-14V80.18A16,16,0,0,0,223.68,66.15ZM128,32h0l80.34,44L128,120,47.66,76ZM40,90l80,43.78v85.79L40,175.82Zm96,129.57V133.82L216,90v85.78Z"],"hard-drives":["M208,136H48a16,16,0,0,0-16,16v48a16,16,0,0,0,16,16H208a16,16,0,0,0,16-16V152A16,16,0,0,0,208,136Zm0,64H48V152H208v48Zm0-160H48A16,16,0,0,0,32,56v48a16,16,0,0,0,16,16H208a16,16,0,0,0,16-16V56A16,16,0,0,0,208,40Zm0,64H48V56H208v48ZM192,80a12,12,0,1,1-12-12A12,12,0,0,1,192,80Zm0,96a12,12,0,1,1-12-12A12,12,0,0,1,192,176Z"],"queue":["M32,64a8,8,0,0,1,8-8H216a8,8,0,0,1,0,16H40A8,8,0,0,1,32,64Zm104,56H40a8,8,0,0,0,0,16h96a8,8,0,0,0,0-16Zm0,64H40a8,8,0,0,0,0,16h96a8,8,0,0,0,0-16Zm112-24a8,8,0,0,1-3.76,6.78l-64,40A8,8,0,0,1,168,200V120a8,8,0,0,1,12.24-6.78l64,40A8,8,0,0,1,248,160Zm-23.09,0L184,134.43v51.14Z"],"user":["M230.92,212c-15.23-26.33-38.7-45.21-66.09-54.16a72,72,0,1,0-73.66,0C63.78,166.78,40.31,185.66,25.08,212a8,8,0,1,0,13.85,8c18.84-32.56,52.14-52,89.07-52s70.23,19.44,89.07,52a8,8,0,1,0,13.85-8ZM72,96a56,56,0,1,1,56,56A56.06,56.06,0,0,1,72,96Z"],"globe":["M128,24h0A104,104,0,1,0,232,128,104.12,104.12,0,0,0,128,24Zm88,104a87.61,87.61,0,0,1-3.33,24H174.16a157.44,157.44,0,0,0,0-48h38.51A87.61,87.61,0,0,1,216,128ZM102,168H154a115.11,115.11,0,0,1-26,45A115.27,115.27,0,0,1,102,168Zm-3.9-16a140.84,140.84,0,0,1,0-48h59.88a140.84,140.84,0,0,1,0,48ZM40,128a87.61,87.61,0,0,1,3.33-24H81.84a157.44,157.44,0,0,0,0,48H43.33A87.61,87.61,0,0,1,40,128ZM154,88H102a115.11,115.11,0,0,1,26-45A115.27,115.27,0,0,1,154,88Zm52.33,0H170.71a135.28,135.28,0,0,0-22.3-45.6A88.29,88.29,0,0,1,206.37,88ZM107.59,42.4A135.28,135.28,0,0,0,85.29,88H49.63A88.29,88.29,0,0,1,107.59,42.4ZM49.63,168H85.29a135.28,135.28,0,0,0,22.3,45.6A88.29,88.29,0,0,1,49.63,168Zm98.78,45.6a135.28,135.28,0,0,0,22.3-45.6h35.66A88.29,88.29,0,0,1,148.41,213.6Z"],"lock-simple":["M208,80H176V56a48,48,0,0,0-96,0V80H48A16,16,0,0,0,32,96V208a16,16,0,0,0,16,16H208a16,16,0,0,0,16-16V96A16,16,0,0,0,208,80ZM96,56a32,32,0,0,1,64,0V80H96ZM208,208H48V96H208V208Z"],"lightning":["M215.79,118.17a8,8,0,0,0-5-5.66L153.18,90.9l14.66-73.33a8,8,0,0,0-13.69-7l-112,120a8,8,0,0,0,3,13l57.63,21.61L88.16,238.43a8,8,0,0,0,13.69,7l112-120A8,8,0,0,0,215.79,118.17ZM109.37,214l10.47-52.38a8,8,0,0,0-5-9.06L62,132.71l84.62-90.66L136.16,94.43a8,8,0,0,0,5,9.06l52.8,19.8Z"],"plug":["M237.66,66.34a8,8,0,0,0-11.32,0L192,100.69,155.31,64l34.35-34.34a8,8,0,1,0-11.32-11.32L144,52.69,117.66,26.34a8,8,0,0,0-11.32,11.32L112.69,44l-53,53a40,40,0,0,0,0,56.57l15.71,15.71L26.34,218.34a8,8,0,0,0,11.32,11.32l49.09-49.09,15.71,15.71a40,40,0,0,0,56.57,0l53-53,6.34,6.35a8,8,0,0,0,11.32-11.32L203.31,112l34.35-34.34A8,8,0,0,0,237.66,66.34ZM147.72,185a24,24,0,0,1-33.95,0L71,142.23a24,24,0,0,1,0-33.95l53-53L200.69,132Z"],"cpu":["M152,96H104a8,8,0,0,0-8,8v48a8,8,0,0,0,8,8h48a8,8,0,0,0,8-8V104A8,8,0,0,0,152,96Zm-8,48H112V112h32Zm88,0H216V112h16a8,8,0,0,0,0-16H216V56a16,16,0,0,0-16-16H160V24a8,8,0,0,0-16,0V40H112V24a8,8,0,0,0-16,0V40H56A16,16,0,0,0,40,56V96H24a8,8,0,0,0,0,16H40v32H24a8,8,0,0,0,0,16H40v40a16,16,0,0,0,16,16H96v16a8,8,0,0,0,16,0V216h32v16a8,8,0,0,0,16,0V216h40a16,16,0,0,0,16-16V160h16a8,8,0,0,0,0-16Zm-32,56H56V56H200v95.87s0,.09,0,.13,0,.09,0,.13V200Z"],"browser":["M216,40H40A16,16,0,0,0,24,56V200a16,16,0,0,0,16,16H216a16,16,0,0,0,16-16V56A16,16,0,0,0,216,40Zm0,16V88H40V56Zm0,144H40V104H216v96Z"],"device-mobile":["M176,16H80A24,24,0,0,0,56,40V216a24,24,0,0,0,24,24h96a24,24,0,0,0,24-24V40A24,24,0,0,0,176,16ZM72,64H184V192H72Zm8-32h96a8,8,0,0,1,8,8v8H72V40A8,8,0,0,1,80,32Zm96,192H80a8,8,0,0,1-8-8v-8H184v8A8,8,0,0,1,176,224Z"],"robot":["M200,48H136V16a8,8,0,0,0-16,0V48H56A32,32,0,0,0,24,80V192a32,32,0,0,0,32,32H200a32,32,0,0,0,32-32V80A32,32,0,0,0,200,48Zm16,144a16,16,0,0,1-16,16H56a16,16,0,0,1-16-16V80A16,16,0,0,1,56,64H200a16,16,0,0,1,16,16Zm-52-56H92a28,28,0,0,0,0,56h72a28,28,0,0,0,0-56Zm-24,16v24H116V152ZM80,164a12,12,0,0,1,12-12h8v24H92A12,12,0,0,1,80,164Zm84,12h-8V152h8a12,12,0,0,1,0,24ZM72,108a12,12,0,1,1,12,12A12,12,0,0,1,72,108Zm88,0a12,12,0,1,1,12,12A12,12,0,0,1,160,108Z"],"sparkle":["M197.58,129.06,146,110l-19-51.62a15.92,15.92,0,0,0-29.88,0L78,110l-51.62,19a15.92,15.92,0,0,0,0,29.88L78,178l19,51.62a15.92,15.92,0,0,0,29.88,0L146,178l51.62-19a15.92,15.92,0,0,0,0-29.88ZM137,164.22a8,8,0,0,0-4.74,4.74L112,223.85,91.78,169A8,8,0,0,0,87,164.22L32.15,144,87,123.78A8,8,0,0,0,91.78,119L112,64.15,132.22,119a8,8,0,0,0,4.74,4.74L191.85,144ZM144,40a8,8,0,0,1,8-8h16V16a8,8,0,0,1,16,0V32h16a8,8,0,0,1,0,16H184V64a8,8,0,0,1-16,0V48H152A8,8,0,0,1,144,40ZM248,88a8,8,0,0,1-8,8h-8v8a8,8,0,0,1-16,0V96h-8a8,8,0,0,1,0-16h8V72a8,8,0,0,1,16,0v8h8A8,8,0,0,1,248,88Z"],"shield-check":["M208,40H48A16,16,0,0,0,32,56v56c0,52.72,25.52,84.67,46.93,102.19,23.06,18.86,46,25.26,47,25.53a8,8,0,0,0,4.2,0c1-.27,23.91-6.67,47-25.53C198.48,196.67,224,164.72,224,112V56A16,16,0,0,0,208,40Zm0,72c0,37.07-13.66,67.16-40.6,89.42A129.3,129.3,0,0,1,128,223.62a128.25,128.25,0,0,1-38.92-21.81C61.82,179.51,48,149.3,48,112l0-56,160,0ZM82.34,141.66a8,8,0,0,1,11.32-11.32L112,148.69l50.34-50.35a8,8,0,0,1,11.32,11.32l-56,56a8,8,0,0,1-11.32,0Z"],"git-branch":["M232,64a32,32,0,1,0-40,31v17a8,8,0,0,1-8,8H96a23.84,23.84,0,0,0-8,1.38V95a32,32,0,1,0-16,0v66a32,32,0,1,0,16,0V144a8,8,0,0,1,8-8h88a24,24,0,0,0,24-24V95A32.06,32.06,0,0,0,232,64ZM64,64A16,16,0,1,1,80,80,16,16,0,0,1,64,64ZM96,192a16,16,0,1,1-16-16A16,16,0,0,1,96,192ZM200,80a16,16,0,1,1,16-16A16,16,0,0,1,200,80Z"],"arrows-split":["M229.66,189.66l-32,32a8,8,0,0,1-11.32,0l-32-32a8,8,0,0,1,11.32-11.32L184,196.69V139.31l-56-56-56,56v57.38l18.34-18.35a8,8,0,0,1,11.32,11.32l-32,32a8,8,0,0,1-11.32,0l-32-32a8,8,0,0,1,11.32-11.32L56,196.69V136a8,8,0,0,1,2.34-5.66L120,68.69V24a8,8,0,0,1,16,0V68.69l61.66,61.65A8,8,0,0,1,200,136v60.69l18.34-18.35a8,8,0,0,1,11.32,11.32Z"],"cloud-arrow-up":["M178.34,165.66,160,147.31V208a8,8,0,0,1-16,0V147.31l-18.34,18.35a8,8,0,0,1-11.32-11.32l32-32a8,8,0,0,1,11.32,0l32,32a8,8,0,0,1-11.32,11.32ZM160,40A88.08,88.08,0,0,0,81.29,88.68,64,64,0,1,0,72,216h40a8,8,0,0,0,0-16H72a48,48,0,0,1,0-96c1.1,0,2.2,0,3.29.12A88,88,0,0,0,72,128a8,8,0,0,0,16,0,72,72,0,1,1,100.8,66,8,8,0,0,0,3.2,15.34,7.9,7.9,0,0,0,3.2-.68A88,88,0,0,0,160,40Z"],"magnifying-glass":["M229.66,218.34l-50.07-50.06a88.11,88.11,0,1,0-11.31,11.31l50.06,50.07a8,8,0,0,0,11.32-11.32ZM40,112a72,72,0,1,1,72,72A72.08,72.08,0,0,1,40,112Z"],"bell":["M221.8,175.94C216.25,166.38,208,139.33,208,104a80,80,0,1,0-160,0c0,35.34-8.26,62.38-13.81,71.94A16,16,0,0,0,48,200H88.81a40,40,0,0,0,78.38,0H208a16,16,0,0,0,13.8-24.06ZM128,216a24,24,0,0,1-22.62-16h45.24A24,24,0,0,1,128,216ZM48,184c7.7-13.24,16-43.92,16-80a64,64,0,1,1,128,0c0,36.05,8.28,66.73,16,80Z"],"plus":["M224,128a8,8,0,0,1-8,8H136v80a8,8,0,0,1-16,0V136H40a8,8,0,0,1,0-16h80V40a8,8,0,0,1,16,0v80h80A8,8,0,0,1,224,128Z"],"list-checks":["M224,128a8,8,0,0,1-8,8H128a8,8,0,0,1,0-16h88A8,8,0,0,1,224,128ZM128,72h88a8,8,0,0,0,0-16H128a8,8,0,0,0,0,16Zm88,112H128a8,8,0,0,0,0,16h88a8,8,0,0,0,0-16ZM82.34,42.34,56,68.69,45.66,58.34A8,8,0,0,0,34.34,69.66l16,16a8,8,0,0,0,11.32,0l32-32A8,8,0,0,0,82.34,42.34Zm0,64L56,132.69,45.66,122.34a8,8,0,0,0-11.32,11.32l16,16a8,8,0,0,0,11.32,0l32-32a8,8,0,0,0-11.32-11.32Zm0,64L56,196.69,45.66,186.34a8,8,0,0,0-11.32,11.32l16,16a8,8,0,0,0,11.32,0l32-32a8,8,0,0,0-11.32-11.32Z"],"flag-checkered":["M227.32,48.75A8,8,0,0,0,218.76,50c-28,24.22-51.72,12.48-79.21-1.13C111.07,34.76,78.78,18.79,42.76,50A8,8,0,0,0,40,56V224a8,8,0,0,0,16,0V179.77c26.79-21.16,49.87-9.75,76.45,3.41,28.49,14.09,60.77,30.06,96.79-1.13a8,8,0,0,0,2.76-6V56A8,8,0,0,0,227.32,48.75ZM216,71.6v40.65c-14,11.06-27,13.22-40,10.88V79.34A60.05,60.05,0,0,0,216,71.6Zm-56,3.76v43c-6.66-2.67-13.43-6-20.45-9.48-8.82-4.37-18-8.91-27.55-12.18v-43c6.66,2.66,13.43,6,20.45,9.48C141.27,67.55,150.46,72.09,160,75.36ZM96,48.91V92.69a60.06,60.06,0,0,0-40,7.75V59.78C70,48.72,83,46.57,96,48.91ZM86.58,152A60.06,60.06,0,0,0,56,160.43V119.78c14-11.06,27-13.22,40-10.88v43.8A65.61,65.61,0,0,0,86.58,152ZM112,156.67v-43c6.66,2.66,13.43,6,20.45,9.48,8.82,4.37,18,8.9,27.55,12.17v43c-6.66-2.67-13.43-6-20.45-9.48C130.73,164.47,121.54,159.94,112,156.67Zm64,26.45v-43.8a65.61,65.61,0,0,0,9.42.72A60.11,60.11,0,0,0,216,131.57v40.68C202,183.31,189,185.46,176,183.12Z"],"play-circle":["M128,24A104,104,0,1,0,232,128,104.11,104.11,0,0,0,128,24Zm0,192a88,88,0,1,1,88-88A88.1,88.1,0,0,1,128,216Zm48.24-94.78-64-40A8,8,0,0,0,100,88v80a8,8,0,0,0,12.24,6.78l64-40a8,8,0,0,0,0-13.56ZM116,153.57V102.43L156.91,128Z"],"caret-down":["M213.66,101.66l-80,80a8,8,0,0,1-11.32,0l-80-80A8,8,0,0,1,53.66,90.34L128,164.69l74.34-74.35a8,8,0,0,1,11.32,11.32Z"],"caret-right":["M181.66,133.66l-80,80a8,8,0,0,1-11.32-11.32L164.69,128,90.34,53.66a8,8,0,0,1,11.32-11.32l80,80A8,8,0,0,1,181.66,133.66Z"],"caret-up":["M213.66,165.66a8,8,0,0,1-11.32,0L128,91.31,53.66,165.66a8,8,0,0,1-11.32-11.32l80-80a8,8,0,0,1,11.32,0l80,80A8,8,0,0,1,213.66,165.66Z"],"x":["M205.66,194.34a8,8,0,0,1-11.32,11.32L128,139.31,61.66,205.66a8,8,0,0,1-11.32-11.32L116.69,128,50.34,61.66A8,8,0,0,1,61.66,50.34L128,116.69l66.34-66.35a8,8,0,0,1,11.32,11.32L139.31,128Z"],"check":["M229.66,77.66l-128,128a8,8,0,0,1-11.32,0l-56-56a8,8,0,0,1,11.32-11.32L96,188.69,218.34,66.34a8,8,0,0,1,11.32,11.32Z"],"check-circle":["M173.66,98.34a8,8,0,0,1,0,11.32l-56,56a8,8,0,0,1-11.32,0l-24-24a8,8,0,0,1,11.32-11.32L112,148.69l50.34-50.35A8,8,0,0,1,173.66,98.34ZM232,128A104,104,0,1,1,128,24,104.11,104.11,0,0,1,232,128Zm-16,0a88,88,0,1,0-88,88A88.1,88.1,0,0,0,216,128Z"],"x-circle":["M165.66,101.66,139.31,128l26.35,26.34a8,8,0,0,1-11.32,11.32L128,139.31l-26.34,26.35a8,8,0,0,1-11.32-11.32L116.69,128,90.34,101.66a8,8,0,0,1,11.32-11.32L128,116.69l26.34-26.35a8,8,0,0,1,11.32,11.32ZM232,128A104,104,0,1,1,128,24,104.11,104.11,0,0,1,232,128Zm-16,0a88,88,0,1,0-88,88A88.1,88.1,0,0,0,216,128Z"],"warning-circle":["M128,24A104,104,0,1,0,232,128,104.11,104.11,0,0,0,128,24Zm0,192a88,88,0,1,1,88-88A88.1,88.1,0,0,1,128,216Zm-8-80V80a8,8,0,0,1,16,0v56a8,8,0,0,1-16,0Zm20,36a12,12,0,1,1-12-12A12,12,0,0,1,140,172Z"],"paper-plane-right":["M231.87,114l-168-95.89A16,16,0,0,0,40.92,37.34L71.55,128,40.92,218.67A16,16,0,0,0,56,240a16.15,16.15,0,0,0,7.93-2.1l167.92-96.05a16,16,0,0,0,.05-27.89ZM56,224a.56.56,0,0,0,0-.12L85.74,136H144a8,8,0,0,0,0-16H85.74L56.06,32.16A.46.46,0,0,0,56,32l168,95.83Z"],"file-text":["M213.66,82.34l-56-56A8,8,0,0,0,152,24H56A16,16,0,0,0,40,40V216a16,16,0,0,0,16,16H200a16,16,0,0,0,16-16V88A8,8,0,0,0,213.66,82.34ZM160,51.31,188.69,80H160ZM200,216H56V40h88V88a8,8,0,0,0,8,8h48V216Zm-32-80a8,8,0,0,1-8,8H96a8,8,0,0,1,0-16h64A8,8,0,0,1,168,136Zm0,32a8,8,0,0,1-8,8H96a8,8,0,0,1,0-16h64A8,8,0,0,1,168,168Z"],"file-md":["M213.66,82.34l-56-56A8,8,0,0,0,152,24H56A16,16,0,0,0,40,40v72a8,8,0,0,0,16,0V40h88V88a8,8,0,0,0,8,8h48V224a8,8,0,0,0,16,0V88A8,8,0,0,0,213.66,82.34ZM160,51.31,188.69,80H160ZM144,144H128a8,8,0,0,0-8,8v56a8,8,0,0,0,8,8h16a36,36,0,0,0,0-72Zm0,56h-8V160h8a20,20,0,0,1,0,40Zm-40-48v56a8,8,0,0,1-16,0V177.38L74.55,196.59a8,8,0,0,1-13.1,0L48,177.38V208a8,8,0,0,1-16,0V152a8,8,0,0,1,14.55-4.59L68,178.05l21.45-30.64A8,8,0,0,1,104,152Z"],"folder-open":["M245,110.64A16,16,0,0,0,232,104H216V88a16,16,0,0,0-16-16H130.67L102.94,51.2a16.14,16.14,0,0,0-9.6-3.2H40A16,16,0,0,0,24,64V208h0a8,8,0,0,0,8,8H211.1a8,8,0,0,0,7.59-5.47l28.49-85.47A16.05,16.05,0,0,0,245,110.64ZM93.34,64,123.2,86.4A8,8,0,0,0,128,88h72v16H69.77a16,16,0,0,0-15.18,10.94L40,158.7V64Zm112,136H43.1l26.67-80H232Z"],"upload-simple":["M224,144v64a8,8,0,0,1-8,8H40a8,8,0,0,1-8-8V144a8,8,0,0,1,16,0v56H208V144a8,8,0,0,1,16,0ZM93.66,77.66,120,51.31V144a8,8,0,0,0,16,0V51.31l26.34,26.35a8,8,0,0,0,11.32-11.32l-40-40a8,8,0,0,0-11.32,0l-40,40A8,8,0,0,0,93.66,77.66Z"],"chat-circle-text":["M168,112a8,8,0,0,1-8,8H96a8,8,0,0,1,0-16h64A8,8,0,0,1,168,112Zm-8,24H96a8,8,0,0,0,0,16h64a8,8,0,0,0,0-16Zm72-8A104,104,0,0,1,79.12,219.82L45.07,231.17a16,16,0,0,1-20.24-20.24l11.35-34.05A104,104,0,1,1,232,128Zm-16,0A88,88,0,1,0,51.81,172.06a8,8,0,0,1,.66,6.54L40,216,77.4,203.53a7.85,7.85,0,0,1,2.53-.42,8,8,0,0,1,4,1.08A88,88,0,0,0,216,128Z"],"pencil-simple":["M227.31,73.37,182.63,28.68a16,16,0,0,0-22.63,0L36.69,152A15.86,15.86,0,0,0,32,163.31V208a16,16,0,0,0,16,16H92.69A15.86,15.86,0,0,0,104,219.31L227.31,96a16,16,0,0,0,0-22.63ZM92.69,208H48V163.31l88-88L180.69,120ZM192,108.68,147.31,64l24-24L216,84.68Z"],"at":["M128,24a104,104,0,0,0,0,208c21.51,0,44.1-6.48,60.43-17.33a8,8,0,0,0-8.86-13.33C166,210.38,146.21,216,128,216a88,88,0,1,1,88-88c0,26.45-10.88,32-20,32s-20-5.55-20-32V88a8,8,0,0,0-16,0v4.26a48,48,0,1,0,5.93,65.1c6,12,16.35,18.64,30.07,18.64,22.54,0,36-17.94,36-48A104.11,104.11,0,0,0,128,24Zm0,136a32,32,0,1,1,32-32A32,32,0,0,1,128,160Z"],"link-simple":["M165.66,90.34a8,8,0,0,1,0,11.32l-64,64a8,8,0,0,1-11.32-11.32l64-64A8,8,0,0,1,165.66,90.34ZM215.6,40.4a56,56,0,0,0-79.2,0L106.34,70.45a8,8,0,0,0,11.32,11.32l30.06-30a40,40,0,0,1,56.57,56.56l-30.07,30.06a8,8,0,0,0,11.31,11.32L215.6,119.6a56,56,0,0,0,0-79.2ZM138.34,174.22l-30.06,30.06a40,40,0,1,1-56.56-56.57l30.05-30.05a8,8,0,0,0-11.32-11.32L40.4,136.4a56,56,0,0,0,79.2,79.2l30.06-30.07a8,8,0,0,0-11.32-11.31Z"],"arrow-counter-clockwise":["M224,128a96,96,0,0,1-94.71,96H128A95.38,95.38,0,0,1,62.1,197.8a8,8,0,0,1,11-11.63A80,80,0,1,0,71.43,71.39a3.07,3.07,0,0,1-.26.25L44.59,96H72a8,8,0,0,1,0,16H24a8,8,0,0,1-8-8V56a8,8,0,0,1,16,0V85.8L60.25,60A96,96,0,0,1,224,128Z"],"dots-three":["M140,128a12,12,0,1,1-12-12A12,12,0,0,1,140,128Zm56-12a12,12,0,1,0,12,12A12,12,0,0,0,196,116ZM60,116a12,12,0,1,0,12,12A12,12,0,0,0,60,116Z"],"paperclip":["M209.66,122.34a8,8,0,0,1,0,11.32l-82.05,82a56,56,0,0,1-79.2-79.21L147.67,35.73a40,40,0,1,1,56.61,56.55L105,193A24,24,0,1,1,71,159L154.3,74.38A8,8,0,1,1,165.7,85.6L82.39,170.31a8,8,0,1,0,11.27,11.36L192.93,81A24,24,0,1,0,159,47L59.76,147.68a40,40,0,1,0,56.53,56.62l82.06-82A8,8,0,0,1,209.66,122.34Z"],"arrow-square-out":["M224,104a8,8,0,0,1-16,0V59.32l-66.33,66.34a8,8,0,0,1-11.32-11.32L196.68,48H152a8,8,0,0,1,0-16h64a8,8,0,0,1,8,8Zm-40,24a8,8,0,0,0-8,8v72H48V80h72a8,8,0,0,0,0-16H48A16,16,0,0,0,32,80V208a16,16,0,0,0,16,16H176a16,16,0,0,0,16-16V136A8,8,0,0,0,184,128Z"],"funnel-simple":["M200,136a8,8,0,0,1-8,8H64a8,8,0,0,1,0-16H192A8,8,0,0,1,200,136Zm32-56H24a8,8,0,0,0,0,16H232a8,8,0,0,0,0-16Zm-80,96H104a8,8,0,0,0,0,16h48a8,8,0,0,0,0-16Z"],"minus":["M224,128a8,8,0,0,1-8,8H40a8,8,0,0,1,0-16H216A8,8,0,0,1,224,128Z"],"corners-out":["M216,48V88a8,8,0,0,1-16,0V56H168a8,8,0,0,1,0-16h40A8,8,0,0,1,216,48ZM88,200H56V168a8,8,0,0,0-16,0v40a8,8,0,0,0,8,8H88a8,8,0,0,0,0-16Zm120-40a8,8,0,0,0-8,8v32H168a8,8,0,0,0,0,16h40a8,8,0,0,0,8-8V168A8,8,0,0,0,208,160ZM88,40H48a8,8,0,0,0-8,8V88a8,8,0,0,0,16,0V56H88a8,8,0,0,0,0-16Z"],"kanban":["M216,48H40a8,8,0,0,0-8,8V208a16,16,0,0,0,16,16H88a16,16,0,0,0,16-16V160h48v16a16,16,0,0,0,16,16h40a16,16,0,0,0,16-16V56A8,8,0,0,0,216,48ZM88,208H48V128H88Zm0-96H48V64H88Zm64,32H104V64h48Zm56,32H168V128h40Zm0-64H168V64h40Z"],"clock":["M128,24A104,104,0,1,0,232,128,104.11,104.11,0,0,0,128,24Zm0,192a88,88,0,1,1,88-88A88.1,88.1,0,0,1,128,216Zm64-88a8,8,0,0,1-8,8H128a8,8,0,0,1-8-8V72a8,8,0,0,1,16,0v48h48A8,8,0,0,1,192,128Z"]};
  var ASSETS2 = (window.GDGlassAssets || '../assets/'); var MARK = ASSETS2 + 'Logos/Full%20color,%20black/SVG/full-color-black-mark.svg';
  var WORDMARK = ASSETS2 + 'Logos/Full%20color,%20black/SVG/full-color-black-horizontal.svg';
  var WORDMARK_DARK = ASSETS2 + 'Logos/Full%20color,%20white/SVG/full-color-white-horizontal.svg';

  /* ---------- Icon (Phosphor Regular, MIT, flagged substitute) ---------- */
  function Icon(p) {
    var paths = ICONS[p.name] || ICONS['cube'], s = p.size || 20;
    return h('svg', { className: cx('gdg-icon', p.className), width: s, height: s, viewBox: '0 0 256 256', fill: 'currentColor',
      'aria-hidden': p.label ? undefined : true, role: p.label ? 'img' : undefined, 'aria-label': p.label },
      paths.map(function (d, i) { return h('path', { key: i, d: d }); }));
  }
  Icon.names = Object.keys(ICONS);

  /* ---------- Sidebar ---------- */
  function Sidebar(p) {
    var st = useState(!!p.defaultCollapsed), inner = st[0], setInner = st[1];
    var collapsed = p.collapsed !== undefined ? p.collapsed : inner;
    var av = useState(p.defaultValue), curInner = av[0], setCur = av[1];
    var cur = p.value !== undefined ? p.value : curInner;
    function toggle() { if (p.collapsed === undefined) setInner(!collapsed); if (p.onToggle) p.onToggle(!collapsed); }
    function pick(v) { if (p.value === undefined) setCur(v); if (p.onChange) p.onChange(v); }
    var items = p.items || [];
    return h(GlassSurface, { as: 'aside', className: cx('gdg-side', collapsed && 'gdg-side--collapsed', p.className), floating: true,
      'aria-label': p.label || 'Primary', style: p.style },
      h('div', { className: 'gdg-side__head' },
        collapsed ? h('img', { className: 'gdg-side__mark', src: p.markSrc || MARK, alt: 'Grid Dynamics' })
                  : [h('img', { key: 'l', className: 'gdg-side__logo gdg-logo-light', src: p.logoSrc || WORDMARK, alt: 'Grid Dynamics' }),
                     h('img', { key: 'd', className: 'gdg-side__logo gdg-logo-dark', src: p.logoDarkSrc || WORDMARK_DARK, alt: 'Grid Dynamics' })]),
      h('nav', { className: 'gdg-side__nav' },
        items.map(function (it, i) {
          if (it.section) return collapsed ? h('div', { key: 's' + i, className: 'gdg-side__rule', role: 'separator' })
            : h('div', { key: 's' + i, className: 'gdg-side__section' }, it.section);
          var on = cur === it.value;
          return h('button', { key: it.value, type: 'button', className: 'gdg-side__item', 'aria-current': on ? 'page' : undefined,
            'aria-label': collapsed ? it.label : undefined, 'data-tip': collapsed ? it.label : undefined, onClick: function () { pick(it.value); } },
            h(Icon, { name: it.icon, size: 20 }),
            collapsed ? null : h('span', { className: 'gdg-side__label' }, it.label),
            it.badge != null ? h('span', { className: 'gdg-side__badge' }, it.badge) : null);
        })),
      h('div', { className: 'gdg-side__foot' },
        p.user ? h('div', { className: 'gdg-side__user', 'data-tip': collapsed ? p.user.name : undefined },
          h('span', { className: 'gdg-side__avatar', 'aria-hidden': true }, (p.user.name || '?').split(' ').map(function (w) { return w[0]; }).join('').slice(0, 2)),
          collapsed ? null : h('span', { className: 'gdg-side__who' }, h('b', null, p.user.name), h('small', null, p.user.role))) : null,
        h('button', { type: 'button', className: 'gdg-side__item gdg-side__toggle', onClick: toggle,
          'aria-label': collapsed ? 'Expand sidebar' : 'Collapse sidebar', 'aria-expanded': !collapsed, 'data-tip': collapsed ? 'Expand' : undefined },
          h(Icon, { name: collapsed ? 'caret-double-right' : 'caret-double-left', size: 18 }),
          collapsed ? null : h('span', { className: 'gdg-side__label' }, 'Collapse'))));
  }

  /* ---------- Diagram engine ---------- */
  var KIND_ICON = { service: 'cube', app: 'browser', mobile: 'device-mobile', gateway: 'arrows-split', datastore: 'database', storage: 'hard-drives',
    queue: 'queue', ai: 'sparkle', external: 'globe', actor: 'user', cloud: 'cloud', security: 'shield-check', compute: 'cpu', integration: 'plug' };
  var KIND_TINT = { datastore: 'data', storage: 'data', queue: 'queue', ai: 'ai' };
  var KIND_LABEL = { service: 'Service', app: 'Web app', mobile: 'Mobile app', gateway: 'Gateway', datastore: 'Database', storage: 'Storage', queue: 'Queue',
    ai: 'AI model', external: 'External', actor: 'Actor', cloud: 'Cloud', security: 'Security', compute: 'Compute', integration: 'Integration' };

  function DiagramNode(p) {
    var kind = p.kind || 'service';
    var style = p.x != null ? { position: 'absolute', left: p.x, top: p.y, width: p.w || 200, height: p.h || 64 } : { width: p.w || 200, height: p.h || 64 };
    return h('div', { className: cx('gdg-dnode', 'gdg-dnode--' + kind, p.selected && 'is-selected', p.dim && 'is-dim', p.onClick && 'is-clickable'),
      style: style, onClick: p.onClick, role: p.onClick ? 'button' : undefined, tabIndex: p.onClick ? 0 : undefined,
      onKeyDown: p.onClick ? function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); p.onClick(); } } : undefined,
      'aria-pressed': p.onClick ? !!p.selected : undefined },
      h('span', { className: 'gdg-dnode__tile gdg-dnode__tile--' + (KIND_TINT[kind] || 'service') }, h(Icon, { name: p.icon || KIND_ICON[kind] || 'cube', size: 20 })),
      h('span', { className: 'gdg-dnode__text' },
        h('span', { className: 'gdg-dnode__kind' }, p.kindLabel || KIND_LABEL[kind] || kind),
        h('span', { className: 'gdg-dnode__title' }, p.title),
        p.subtitle ? h('span', { className: 'gdg-dnode__sub' }, p.subtitle) : null),
      p.badge ? h('span', { className: 'gdg-dnode__badge' }, p.badge) : null);
  }

  function DiagramGroup(p) {
    return h('div', { className: cx('gdg-dgroup', p.tone === 'plain' && 'gdg-dgroup--plain'),
      style: { left: p.x, top: p.y, width: p.w, height: p.h } },
      h('span', { className: 'gdg-dgroup__label' }, p.icon ? h(Icon, { name: p.icon, size: 14 }) : null, p.label));
  }

  function anchor(r, side, off) {
    off = off || 0;
    if (side === 'right') return [r.x + r.w, r.y + r.h / 2 + off];
    if (side === 'left') return [r.x, r.y + r.h / 2 + off];
    if (side === 'top') return [r.x + r.w / 2 + off, r.y];
    return [r.x + r.w / 2 + off, r.y + r.h];
  }
  function pickSides(a, b) {
    if (b.x >= a.x + a.w - 2) return ['right', 'left'];
    if (b.x + b.w <= a.x + 2) return ['left', 'right'];
    if (b.y >= a.y + a.h) return ['bottom', 'top'];
    return ['top', 'bottom'];
  }
  /** Orthogonal route with rounded corners between two rects. Returns {d, mid}. */
  function routeEdge(a, b, opt) {
    opt = opt || {};
    var sides = pickSides(a, b), fs = opt.fromSide || sides[0], ts = opt.toSide || sides[1];
    var p1 = anchor(a, fs, opt.fromOffset), p2 = anchor(b, ts, opt.toOffset), pts;
    var hz = function (s) { return s === 'left' || s === 'right'; };
    if (hz(fs) && hz(ts)) { var mx = opt.bend != null ? opt.bend : (p1[0] + p2[0]) / 2; pts = [p1, [mx, p1[1]], [mx, p2[1]], p2]; }
    else if (!hz(fs) && !hz(ts)) { var my = opt.bend != null ? opt.bend : (p1[1] + p2[1]) / 2; pts = [p1, [p1[0], my], [p2[0], my], p2]; }
    else if (hz(fs)) pts = [p1, [p2[0], p1[1]], p2];
    else pts = [p1, [p1[0], p2[1]], p2];
    pts = pts.filter(function (pt, i) { return i === 0 || pt[0] !== pts[i - 1][0] || pt[1] !== pts[i - 1][1]; });
    var R = 12, d = 'M' + pts[0][0] + ' ' + pts[0][1];
    for (var i = 1; i < pts.length - 1; i++) {
      var pr = pts[i - 1], c = pts[i], nx = pts[i + 1];
      var l1 = Math.hypot(c[0] - pr[0], c[1] - pr[1]), l2 = Math.hypot(nx[0] - c[0], nx[1] - c[1]), r = Math.min(R, l1 / 2, l2 / 2);
      var ax = c[0] - (c[0] - pr[0]) / (l1 || 1) * r, ay = c[1] - (c[1] - pr[1]) / (l1 || 1) * r;
      var bx = c[0] + (nx[0] - c[0]) / (l2 || 1) * r, by = c[1] + (nx[1] - c[1]) / (l2 || 1) * r;
      d += ' L' + ax + ' ' + ay + ' Q' + c[0] + ' ' + c[1] + ' ' + bx + ' ' + by;
    }
    d += ' L' + pts[pts.length - 1][0] + ' ' + pts[pts.length - 1][1];
    var k = Math.floor((pts.length - 1) / 2), m1 = pts[k], m2 = pts[k + 1];
    return { d: d, mid: [(m1[0] + m2[0]) / 2, (m1[1] + m2[1]) / 2] };
  }

  var EDGE_KINDS = { sync: 'Synchronous call', async: 'Async / event', data: 'Data flow', flow: 'Process flow' };
  function edgeClass(e, hi) { return cx('gdg-dedge', 'gdg-dedge--' + (e.kind || 'sync'), hi && 'is-hi'); }
  function markerFor(e, hi, id) { return 'url(#' + id + '-' + (hi ? 'hi' : (e.kind === 'data' ? 'data' : 'base')) + ')'; }

  var uid = 0;
  function DiagramCanvas(p) {
    var idRef = React.useRef('gdgd' + (++uid)), id = idRef.current;
    var rects = p.rects || {}, edges = p.edges || [], hiSet = p.highlight || {};
    var labels = [];
    var paths = edges.map(function (e, i) {
      var a = rects[e.from], b = rects[e.to]; if (!a || !b) return null;
      var r = routeEdge(a, b, e), hi = !!(e.highlight || hiSet[i]);
      if (e.label) labels.push(h('span', { key: 'l' + i, className: cx('gdg-dlabel', hi && 'is-hi'), style: { left: r.mid[0], top: r.mid[1] } }, e.label));
      return h('g', { key: i },
        hi ? h('path', { className: 'gdg-dedge-halo', d: r.d }) : null,
        h('path', { className: edgeClass(e, hi), d: r.d, markerEnd: e.arrow === false ? undefined : markerFor(e, hi, id),
          markerStart: e.bidirectional ? markerFor(e, hi, id) : undefined }));
    });
    function marker(name, cls) {
      return h('marker', { id: id + '-' + name, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse', markerUnits: 'userSpaceOnUse' },
        h('path', { d: 'M1 1 L9 5 L1 9 Z', className: cls }));
    }
    return h('div', { className: cx('gdg-dscroll', p.className) },
      h('div', { className: cx('gdg-dcanvas', p.grid === false && 'gdg-dcanvas--plain'), style: { width: p.width, height: p.height }, role: 'group', 'aria-label': p.label },
        p.groups,
        h('svg', { className: 'gdg-dedges', width: p.width, height: p.height, 'aria-hidden': true },
          h('defs', null, marker('base', 'gdg-dmark'), marker('data', 'gdg-dmark gdg-dmark--data'), marker('hi', 'gdg-dmark gdg-dmark--hi')),
          paths),
        p.children,
        labels,
        p.legend ? h('div', { className: 'gdg-dlegend-wrap gdg-dlegend-wrap--' + (p.legendPosition || 'bottom-right') }, p.legend) : null));
  }

  function DiagramLegend(p) {
    var kinds = p.kinds || ['sync', 'async', 'data'];
    return h(GlassSurface, { variant: 'strong', className: 'gdg-dlegend' },
      kinds.map(function (k) {
        return h('span', { key: k, className: 'gdg-dlegend__row' },
          h('svg', { width: 32, height: 10, 'aria-hidden': true }, h('path', { d: 'M1 5 H31', className: 'gdg-dedge gdg-dedge--' + k })),
          EDGE_KINDS[k] || k);
      }),
      p.highlight ? h('span', { className: 'gdg-dlegend__row' },
        h('svg', { width: 32, height: 12, 'aria-hidden': true }, h('path', { d: 'M4 6 H28', className: 'gdg-dedge-halo' }), h('path', { d: 'M4 6 H28', className: 'gdg-dedge is-hi' })),
        p.highlight) : null);
  }

  function useSelection(p) {
    var s = useState(p.defaultSelected || null); return s;
  }

  function ArchitectureDiagram(p) {
    var nodes = p.nodes || [], edges = p.edges || [], groups = p.groups || [];
    var sel = useSelection(p), selected = sel[0], setSel = sel[1];
    var rects = {}; nodes.forEach(function (n) { rects[n.id] = { x: n.x, y: n.y, w: n.w || 200, h: n.h || 64 }; });
    var hi = {}, linked = {};
    if (selected) edges.forEach(function (e, i) { if (e.from === selected || e.to === selected) { hi[i] = true; linked[e.from] = linked[e.to] = true; } });
    return h(DiagramCanvas, { width: p.width, height: p.height, rects: rects, edges: edges, highlight: hi, label: p.label, grid: p.grid,
      groups: groups.map(function (g, i) { return h(DiagramGroup, Object.assign({ key: 'g' + i }, g)); }),
      legend: p.legend === false ? null : h(DiagramLegend, { kinds: p.legendKinds, highlight: 'Selected path' }), legendPosition: p.legendPosition },
      nodes.map(function (n) {
        return h(DiagramNode, Object.assign({}, n, { key: n.id, w: rects[n.id].w, h: rects[n.id].h, selected: selected === n.id,
          dim: selected && !linked[n.id] && selected !== n.id,
          onClick: p.selectable === false ? undefined : function () { setSel(selected === n.id ? null : n.id); } }));
      }));
  }

  /* ---------- Process flow ---------- */
  var STEP_SIZE = { start: [140, 48], end: [140, 48], step: [156, 64], subprocess: [156, 64], decision: [128, 92] };
  function ProcessNode(p) {
    var t = p.type || 'step';
    var cls = cx('gdg-pnode', 'gdg-pnode--' + t, p.selected && 'is-selected', p.dim && 'is-dim');
    var style = { position: 'absolute', left: p.x, top: p.y, width: p.w, height: p.h };
    var inner = [];
    if (t === 'decision') {
      inner.push(h('svg', { key: 'd', className: 'gdg-pnode__diamond', width: p.w, height: p.h, viewBox: '0 0 ' + p.w + ' ' + p.h, 'aria-hidden': true },
        h('polygon', { points: (p.w / 2) + ',1 ' + (p.w - 1) + ',' + (p.h / 2) + ' ' + (p.w / 2) + ',' + (p.h - 1) + ' 1,' + (p.h / 2) })));
    } else if (t === 'start' || t === 'end') {
      inner.push(h(Icon, { key: 'i', name: t === 'start' ? 'play-circle' : 'flag-checkered', size: 18 }));
    } else if (p.number != null) {
      inner.push(h('span', { key: 'n', className: 'gdg-pnode__num' }, p.number));
    }
    inner.push(h('span', { key: 't', className: 'gdg-pnode__text' }, h('span', { className: 'gdg-pnode__title' }, p.title),
      p.owner && t !== 'decision' ? h('span', { className: 'gdg-pnode__sub' }, p.owner) : null));
    return h('div', { className: cls, style: style, onClick: p.onClick, role: p.onClick ? 'button' : undefined, tabIndex: p.onClick ? 0 : undefined,
      'aria-pressed': p.onClick ? !!p.selected : undefined,
      onKeyDown: p.onClick ? function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); p.onClick(); } } : undefined }, inner);
  }

  function ProcessFlow(p) {
    var lanes = p.lanes || [], steps = p.steps || [], edges = (p.edges || []).map(function (e) { return Object.assign({ kind: 'flow' }, e); });
    var laneH = p.laneHeight || 132, colW = p.colWidth || 176, labelW = lanes.length ? 132 : 0, pad = 24;
    var cols = steps.reduce(function (m, s) { return Math.max(m, (s.col || 0) + 1); }, 0);
    var width = p.width || (labelW + cols * colW + pad), height = p.height || ((lanes.length || 1) * laneH);
    var laneIndex = {}; lanes.forEach(function (l, i) { laneIndex[l.id] = i; });
    var rects = {}, sel = useSelection(p), selected = sel[0], setSel = sel[1];
    steps.forEach(function (s) {
      var sz = STEP_SIZE[s.type || 'step'], li = laneIndex[s.lane] || 0;
      rects[s.id] = { x: labelW + (s.col || 0) * colW + (colW - sz[0]) / 2 + pad / 2, y: li * laneH + (laneH - sz[1]) / 2, w: sz[0], h: sz[1] };
    });
    var hi = {}, linked = {};
    if (selected) edges.forEach(function (e, i) { if (e.from === selected || e.to === selected) { hi[i] = true; linked[e.from] = linked[e.to] = true; } });
    var laneEls = lanes.map(function (l, i) {
      return h('div', { key: l.id, className: cx('gdg-plane', i % 2 && 'gdg-plane--alt'), style: { top: i * laneH, height: laneH, width: width } },
        h('span', { className: 'gdg-plane__label', style: { width: labelW } }, l.icon ? h(Icon, { name: l.icon, size: 16 }) : null, l.label));
    });
    var num = 0;
    return h(DiagramCanvas, { width: width, height: height, rects: rects, edges: edges, highlight: hi, label: p.label, grid: lanes.length ? false : p.grid,
      groups: laneEls, legend: null },
      steps.map(function (s) {
        var r = rects[s.id], n = (s.type || 'step') === 'step' || s.type === 'subprocess' ? ++num : null;
        return h(ProcessNode, Object.assign({}, s, { key: s.id, x: r.x, y: r.y, w: r.w, h: r.h, number: p.numbered === false ? null : n,
          selected: selected === s.id, dim: selected && !linked[s.id] && selected !== s.id,
          onClick: p.selectable === false ? undefined : function () { setSel(selected === s.id ? null : s.id); } }));
      }));
  }

  Object.assign(window.GDGlass, { Icon: Icon, Sidebar: Sidebar, DiagramCanvas: DiagramCanvas, DiagramNode: DiagramNode, DiagramGroup: DiagramGroup,
    DiagramLegend: DiagramLegend, ArchitectureDiagram: ArchitectureDiagram, ProcessFlow: ProcessFlow, ProcessNode: ProcessNode, routeEdge: routeEdge });
})();
/* ---- part 3: app components + React Flow adapters ---- */
(function () {
  var React = window.React, h = React.createElement, useState = React.useState, useRef = React.useRef, useEffect = React.useEffect,
      useLayoutEffect = React.useLayoutEffect, useContext = React.useContext, useCallback = React.useCallback;
  var G = window.GDGlass, GlassSurface = G.GlassSurface, Icon = G.Icon, Button = G.Button, Tag = G.Tag;
  function cx() { return Array.prototype.slice.call(arguments).filter(Boolean).join(' '); }
  function omit(o, keys) { var r = {}; for (var k in o) if (keys.indexOf(k) < 0) r[k] = o[k]; return r; }
  function useControlled(value, def, onChange) {
    var st = useState(def), inner = st[0], setInner = st[1];
    var cur = value !== undefined ? value : inner;
    return [cur, function (n) { if (value === undefined) setInner(n); if (onChange) onChange(n); }];
  }
  function useOutside(ref, open, close) {
    useEffect(function () {
      if (!open) return;
      function onDown(e) { if (ref.current && !ref.current.contains(e.target)) close(); }
      function onKey(e) { if (e.key === 'Escape') close(); }
      document.addEventListener('mousedown', onDown); document.addEventListener('keydown', onKey);
      return function () { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey); };
    }, [open]);
  }
  var sid = 0; function useId(prefix) { var r = useRef(null); if (!r.current) r.current = (prefix || 'gdg') + '-' + (++sid); return r.current; }
  function initials(name) { return (name || '?').replace(/^@/, '').split(/\s+/).map(function (w) { return w[0]; }).join('').slice(0, 2).toUpperCase(); }
  var TONES = ['yellow', 'data', 'queue', 'neutral'];
  function toneOf(name) { var s = 0; for (var i = 0; i < (name || '').length; i++) s = (s + name.charCodeAt(i)) % 997; return TONES[s % TONES.length]; }

  /* ---------- Avatar ---------- */
  function Avatar(p) {
    var size = p.size || 'md', bot = p.kind === 'bot';
    return h('span', { className: cx('gdg-avatar', 'gdg-avatar--' + size, 'gdg-avatar--' + (bot ? 'bot' : (p.tone || toneOf(p.name))), p.className),
      title: p.title === false ? undefined : (p.title || p.name), role: 'img', 'aria-label': p.name },
      p.src ? h('img', { src: p.src, alt: '' }) : bot ? h(Icon, { name: 'sparkle', size: size === 'sm' ? 13 : size === 'lg' ? 20 : 16 }) : initials(p.name));
  }
  function AvatarGroup(p) {
    var max = p.max || 3, people = p.people || [], shown = people.slice(0, max), rest = people.length - shown.length;
    return h('span', { className: 'gdg-avgroup', role: 'group', 'aria-label': people.map(function (x) { return x.name; }).join(', ') },
      shown.map(function (x, i) { return h(Avatar, Object.assign({ key: i, size: p.size || 'sm' }, x)); }),
      rest > 0 ? h('span', { className: cx('gdg-avatar', 'gdg-avatar--' + (p.size || 'sm'), 'gdg-avatar--more') }, '+' + rest) : null);
  }

  /* ---------- Tooltip ---------- */
  function Tooltip(p) {
    return h('span', { className: cx('gdg-tip', 'gdg-tip--' + (p.side || 'top')), 'data-tip': p.label }, p.children);
  }

  /* ---------- Spinner / ProgressBar / Skeleton ---------- */
  function Spinner(p) { var s = p.size || 18; return h('span', { className: 'gdg-spinner', style: { width: s, height: s }, role: 'status', 'aria-label': p.label || 'Loading' }); }
  function ProgressBar(p) {
    var v = Math.max(0, Math.min(100, p.value || 0));
    return h('div', { className: cx('gdg-progress', p.indeterminate && 'is-indeterminate', p.tone === 'danger' && 'is-error') },
      p.label ? h('div', { className: 'gdg-progress__head' }, h('span', null, p.label), p.indeterminate ? null : h('span', null, Math.round(v) + '%')) : null,
      h('div', { className: 'gdg-progress__track', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': p.indeterminate ? undefined : Math.round(v), 'aria-label': p.label || 'Progress' },
        h('div', { className: 'gdg-progress__fill', style: p.indeterminate ? undefined : { width: v + '%' } })));
  }
  function Skeleton(p) {
    var v = p.variant || 'text';
    if (v === 'text') {
      var n = p.lines || 3, out = [];
      for (var i = 0; i < n; i++) out.push(h('span', { key: i, className: 'gdg-skel gdg-skel--line', style: { width: i === n - 1 ? '62%' : '100%' } }));
      return h('span', { className: 'gdg-skel-group', 'aria-hidden': true }, out);
    }
    return h('span', { className: cx('gdg-skel', 'gdg-skel--' + v), style: { width: p.width, height: p.height }, 'aria-hidden': true });
  }

  /* ---------- EmptyState ---------- */
  function EmptyState(p) {
    return h(GlassSurface, { variant: p.variant || 'strong', className: 'gdg-empty' },
      p.icon ? h('span', { className: 'gdg-empty__icon' }, h(Icon, { name: p.icon, size: 28 })) : p.media || null,
      h('h3', { className: 'gdg-empty__title' }, p.title),
      p.children ? h('p', { className: 'gdg-empty__body' }, p.children) : null,
      p.actions ? h('div', { className: 'gdg-empty__actions' }, p.actions) : null);
  }

  /* ---------- Menu ---------- */
  function Menu(p) {
    var st = useState(!!p.defaultOpen), open = st[0], setOpen = st[1], ref = useRef(null), listRef = useRef(null), id = useId('menu');
    useOutside(ref, open, function () { setOpen(false); });
    useEffect(function () { if (open && listRef.current && !p.defaultOpen) { var f = listRef.current.querySelector('[role=menuitem],[role=menuitemradio]'); if (f) f.focus(); } }, [open]);
    function onKey(e) {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return; e.preventDefault();
      var els = Array.prototype.slice.call(listRef.current.querySelectorAll('[role=menuitem],[role=menuitemradio]')), i = els.indexOf(document.activeElement);
      var n = e.key === 'ArrowDown' ? (i + 1) % els.length : (i - 1 + els.length) % els.length; els[n].focus();
    }
    var trigger = typeof p.trigger === 'function' ? p.trigger({ open: open, toggle: function () { setOpen(!open); }, id: id })
      : h('button', { type: 'button', className: cx('gdg-menu__trigger', p.triggerClassName), 'aria-haspopup': 'menu', 'aria-expanded': open, 'aria-controls': id,
          onClick: function () { setOpen(!open); } }, p.trigger, h(Icon, { name: 'caret-down', size: 14, className: 'gdg-menu__caret' }));
    return h('div', { className: cx('gdg-menu', 'gdg-menu--' + (p.align || 'start'), p.className), ref: ref },
      trigger,
      open ? h(GlassSurface, { variant: 'strong', floating: true, className: 'gdg-menu__list', role: 'menu', id: id, 'aria-label': p.label, onKeyDown: onKey },
        h('div', { ref: listRef, style: { display: 'contents' } },
          p.header || null,
          (p.items || []).map(function (it, i) {
            if (it.divider) return h('div', { key: i, className: 'gdg-menu__divider', role: 'separator' });
            if (it.section) return h('div', { key: i, className: 'gdg-menu__section' }, it.section);
            var radio = it.checked !== undefined;
            return h('button', { key: i, type: 'button', role: radio ? 'menuitemradio' : 'menuitem', 'aria-checked': radio ? !!it.checked : undefined, tabIndex: -1,
              className: cx('gdg-menu__item', it.tone === 'danger' && 'is-danger'), disabled: it.disabled,
              onClick: function () { if (it.onSelect) it.onSelect(it); if (p.onSelect) p.onSelect(it); setOpen(false); } },
              it.icon ? h(Icon, { name: it.icon, size: 18 }) : it.avatar ? h(Avatar, Object.assign({ size: 'sm' }, it.avatar)) : null,
              h('span', { className: 'gdg-menu__text' }, h('span', null, it.label), it.description ? h('small', null, it.description) : null),
              radio && it.checked ? h(Icon, { name: 'check', size: 16, className: 'gdg-menu__check' }) : null,
              it.shortcut ? h('kbd', null, it.shortcut) : null);
          }))) : null);
  }

  /* ---------- TopBar ---------- */
  function TopBar(p) {
    var projects = p.projects || [], cur = projects.filter(function (x) { return x.id === p.projectId; })[0] || projects[0] || { name: 'No project' };
    var user = p.user || { name: '?' };
    return h('header', { className: cx('gdg-topbar', p.className) },
      h('div', { className: 'gdg-topbar__lead' }, p.children),
      h(GlassSurface, { className: 'gdg-topbar__cluster', floating: true },
        h(Menu, { label: 'Switch project', align: 'end', defaultOpen: p.defaultProjectMenuOpen, triggerClassName: 'gdg-topbar__project',
          trigger: h(React.Fragment, null, h(Icon, { name: 'folder-simple', size: 18 }), h('span', { className: 'gdg-topbar__pname' }, cur.name)),
          header: h('div', { className: 'gdg-menu__section' }, 'Projects'),
          items: projects.map(function (x) { return { label: x.name, description: x.meta, checked: x.id === cur.id, value: x.id, onSelect: function () { if (p.onProjectChange) p.onProjectChange(x.id); } }; }) }),
        h('span', { className: 'gdg-topbar__sep', 'aria-hidden': true }),
        h(Button, { variant: 'primary', size: 'sm', onClick: p.onNew, className: 'gdg-topbar__new' }, h(Icon, { name: 'plus', size: 14 }), p.newLabel || 'New'),
        h(Menu, { label: 'Account', align: 'end', defaultOpen: p.defaultUserMenuOpen,
          trigger: function (t) { return h('button', { type: 'button', className: 'gdg-topbar__user', 'aria-haspopup': 'menu', 'aria-expanded': t.open, 'aria-label': 'Account: ' + user.name, onClick: t.toggle }, h(Avatar, { name: user.name, size: 'md', title: false })); },
          header: h('div', { className: 'gdg-menu__who' }, h(Avatar, { name: user.name, size: 'lg' }), h('span', null, h('b', null, user.name), h('small', null, user.email))),
          items: p.userItems || [{ divider: true }, { label: 'Settings', icon: 'gear-six' }, { label: 'Reset demo', icon: 'arrow-counter-clockwise' }, { divider: true }, { label: 'Sign out', icon: 'sign-out' }] })));
  }

  /* ---------- Modal ---------- */
  function Modal(p) {
    var ref = useRef(null), tid = useId('dlg');
    useEffect(function () {
      if (p.open === false) return;
      var prev = document.activeElement, el = ref.current;
      if (el && !p.noAutoFocus) { var f = el.querySelector('input,textarea,select,button:not(.gdg-modal__close)'); (f || el).focus(); }
      function onKey(e) {
        if (e.key === 'Escape' && p.onClose) p.onClose();
        if (e.key === 'Tab' && el) {
          var els = el.querySelectorAll('button,[href],input,textarea,select,[tabindex]:not([tabindex="-1"])'); if (!els.length) return;
          var first = els[0], last = els[els.length - 1];
          if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
          else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
      }
      document.addEventListener('keydown', onKey);
      return function () { document.removeEventListener('keydown', onKey); if (prev && prev.focus && !p.noAutoFocus) prev.focus(); };
    }, [p.open]);
    if (p.open === false) return null;
    return h('div', { className: cx('gdg-modal-wrap', p.contained && 'is-contained') },
      h('div', { className: 'gdg-modal-scrim', onClick: p.onClose }),
      h(GlassSurface, { variant: 'strong', floating: true, className: cx('gdg-modal', 'gdg-modal--' + (p.size || 'md')), role: 'dialog', 'aria-modal': true, 'aria-labelledby': tid, tabIndex: -1, ref: ref },
        h('div', { className: 'gdg-modal__head' },
          h('div', null, p.eyebrow ? h('p', { className: 'gdg-modal__eyebrow' }, p.eyebrow) : null, h('h2', { className: 'gdg-modal__title', id: tid }, p.title),
            p.description ? h('p', { className: 'gdg-modal__desc' }, p.description) : null),
          h('button', { type: 'button', className: 'gdg-modal__close', 'aria-label': 'Close', onClick: p.onClose }, h(Icon, { name: 'x', size: 18 }))),
        h('div', { className: 'gdg-modal__body' }, p.children),
        p.footer ? h('div', { className: 'gdg-modal__foot' }, p.footer) : null));
  }

  /* ---------- Select ---------- */
  function Select(p) {
    var opts = p.options || [];
    var s = useControlled(p.value, p.defaultValue !== undefined ? p.defaultValue : (opts[0] && opts[0].value), p.onChange), cur = s[0], set = s[1];
    var curOpt = opts.filter(function (o) { return o.value === cur; })[0];
    return h('div', { className: cx('gdg-select', p.className) },
      p.label ? h('span', { className: 'gdg-select__label' }, p.label) : null,
      h(Menu, { label: p.label || 'Options', defaultOpen: p.defaultOpen, triggerClassName: 'gdg-select__trigger',
        trigger: h(React.Fragment, null, p.icon ? h(Icon, { name: p.icon, size: 16 }) : null, h('span', { className: 'gdg-select__value' }, curOpt ? curOpt.label : (p.placeholder || 'Select'))),
        items: opts.map(function (o) { return { label: o.label, description: o.description, checked: o.value === cur, onSelect: function () { set(o.value); } }; }) }));
  }

  /* ---------- SearchField ---------- */
  function SearchField(p) {
    var s = useControlled(p.value, p.defaultValue || '', p.onChange), v = s[0], set = s[1];
    return h('label', { className: cx('gdg-search', p.className) },
      h(Icon, { name: 'magnifying-glass', size: 18 }),
      h('input', { type: 'search', value: v, placeholder: p.placeholder || 'Search', 'aria-label': p.label || p.placeholder || 'Search', onChange: function (e) { set(e.target.value); } }),
      v ? h('button', { type: 'button', className: 'gdg-search__clear', 'aria-label': 'Clear search', onClick: function () { set(''); } }, h(Icon, { name: 'x', size: 14 })) : null);
  }

  /* ---------- PeoplePicker ---------- */
  function PeoplePicker(p) {
    var people = p.people || [], id = useId('pp');
    var s = useControlled(p.value, p.defaultValue || [], p.onChange), sel = s[0], set = s[1];
    var q = useState(''), query = q[0], setQuery = q[1], o = useState(!!p.defaultOpen), open = o[0], setOpen = o[1], ref = useRef(null), act = useState(0), active = act[0], setActive = act[1];
    useOutside(ref, open, function () { setOpen(false); });
    var ql = query.toLowerCase();
    var sugg = people.filter(function (x) { return sel.indexOf(x.id) < 0 && (!ql || (x.name + ' ' + (x.email || '') + ' ' + (x.role || '')).toLowerCase().indexOf(ql) >= 0); });
    function add(x) { set(sel.concat([x.id])); setQuery(''); setActive(0); }
    function onKey(e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setOpen(true); setActive(Math.min(active + 1, sugg.length - 1)); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(Math.max(active - 1, 0)); }
      else if (e.key === 'Enter' && open && sugg[active]) { e.preventDefault(); add(sugg[active]); }
      else if (e.key === 'Backspace' && !query && sel.length) set(sel.slice(0, -1));
    }
    return h('div', { className: 'gdg-field gdg-pp', ref: ref },
      p.label ? h('label', { className: 'gdg-field__label', htmlFor: id }, p.label) : null,
      h('div', { className: 'gdg-pp__box', onClick: function () { var i = document.getElementById(id); if (i) i.focus(); } },
        sel.map(function (pid) {
          var x = people.filter(function (y) { return y.id === pid; })[0]; if (!x) return null;
          return h('span', { key: pid, className: 'gdg-chip' }, h(Avatar, { name: x.name, size: 'sm', title: false }), x.name,
            h('button', { type: 'button', 'aria-label': 'Remove ' + x.name, onClick: function (e) { e.stopPropagation(); set(sel.filter(function (y) { return y !== pid; })); } }, h(Icon, { name: 'x', size: 12 })));
        }),
        h('input', { id: id, value: query, placeholder: sel.length ? '' : (p.placeholder || 'Add people by name or email'), role: 'combobox', 'aria-expanded': open, 'aria-controls': id + '-l', 'aria-autocomplete': 'list',
          onChange: function (e) { setQuery(e.target.value); setOpen(true); setActive(0); }, onFocus: function () { setOpen(true); }, onKeyDown: onKey })),
      p.help ? h('span', { className: 'gdg-field__help' }, p.help) : null,
      open && sugg.length ? h(GlassSurface, { variant: 'strong', floating: true, className: 'gdg-pp__list', role: 'listbox', id: id + '-l' },
        sugg.map(function (x, i) {
          return h('div', { key: x.id, role: 'option', 'aria-selected': i === active, className: cx('gdg-pp__opt', i === active && 'is-active'),
            onMouseDown: function (e) { e.preventDefault(); add(x); }, onMouseEnter: function () { setActive(i); } },
            h(Avatar, { name: x.name, size: 'md', title: false }), h('span', null, h('b', null, x.name), h('small', null, [x.role, x.email].filter(Boolean).join(' · '))));
        })) : null);
  }

  /* ---------- LinkField ---------- */
  function simulateAccess(url) { return new Promise(function (res) { setTimeout(function () { res(!/private/i.test(url) && /^https?:\/\/\S+\.\S+/.test(url)); }, 1400); }); }
  function LinkField(p) {
    var id = useId('lf'), v = useState(p.defaultValue || ''), val = v[0], setVal = v[1];
    var st = useState(p.defaultStatus || 'idle'), status = st[0], setStatus = st[1];
    function test() {
      if (!val) return; setStatus('checking');
      (p.onTest ? Promise.resolve(p.onTest(val)) : simulateAccess(val)).then(function (ok) { setStatus(ok ? 'ok' : 'fail'); if (p.onStatus) p.onStatus(ok ? 'ok' : 'fail'); });
    }
    var msg = { checking: ['pending', 'Checking access…'], ok: ['success', p.okText || 'Access confirmed'], fail: ['danger', p.failText || 'No access. Check the link or share it with the GD service account.'] }[status];
    return h('div', { className: cx('gdg-field', 'gdg-lf', status === 'fail' && 'gdg-field--error') },
      h('label', { className: 'gdg-field__label', htmlFor: id }, p.icon ? h(Icon, { name: p.icon, size: 16 }) : null, p.label),
      h('div', { className: 'gdg-lf__row' },
        h('input', { id: id, type: 'url', value: val, placeholder: p.placeholder, onChange: function (e) { setVal(e.target.value); setStatus('idle'); },
          'aria-describedby': id + '-s', onKeyDown: function (e) { if (e.key === 'Enter') { e.preventDefault(); test(); } } }),
        h(Button, { variant: 'glass', size: 'sm', onClick: test, disabled: !val || status === 'checking' }, p.testLabel || 'Test access'),
        p.onRemove ? h('button', { type: 'button', className: 'gdg-iconbtn', 'aria-label': 'Remove link', onClick: p.onRemove }, h(Icon, { name: 'x', size: 16 })) : null),
      h('span', { id: id + '-s', className: 'gdg-lf__status', 'aria-live': 'polite' }, msg ? h(Tag, { tone: msg[0] }, msg[1]) : (p.help ? h('span', { className: 'gdg-field__help' }, p.help) : null)));
  }

  /* ---------- OptionCards ---------- */
  var LETTERS = 'ABCDEFGH';
  function OptionCards(p) {
    var type = p.type || 'choice', opts = type === 'yesno' ? [{ value: 'yes', label: p.yesLabel || 'Yes' }, { value: 'no', label: p.noLabel || 'No' }] : (p.options || []);
    if (type === 'choice' && p.allowOther) opts = opts.concat([{ value: '__other', label: p.otherLabel || 'Other', other: true }]);
    var s = useControlled(p.value, p.defaultValue, p.onChange), cur = s[0], set = s[1];
    var o = useState(p.defaultOther || ''), other = o[0], setOther = o[1];
    var gid = useId('oc');
    function onKey(e, i) {
      if (['ArrowDown', 'ArrowRight', 'ArrowUp', 'ArrowLeft'].indexOf(e.key) < 0) return; e.preventDefault();
      var n = (e.key === 'ArrowDown' || e.key === 'ArrowRight') ? (i + 1) % opts.length : (i - 1 + opts.length) % opts.length;
      set(opts[n].value); var els = document.querySelectorAll('[data-oc="' + gid + '"]'); if (els[n]) els[n].focus();
    }
    return h('div', { className: cx('gdg-oc', 'gdg-oc--' + type), role: 'radiogroup', 'aria-label': p.label },
      opts.map(function (op, i) {
        var on = cur === op.value;
        return h('div', { key: op.value, className: cx('gdg-oc__card', on && 'is-on') },
          h('button', { type: 'button', role: 'radio', 'aria-checked': on, tabIndex: on || (cur == null && i === 0) ? 0 : -1, 'data-oc': gid, className: 'gdg-oc__btn',
            onClick: function () { set(op.value); }, onKeyDown: function (e) { onKey(e, i); } },
            type === 'yesno' ? h(Icon, { name: op.value === 'yes' ? 'check' : 'x', size: 18 }) : h('span', { className: 'gdg-oc__letter', 'aria-hidden': true }, LETTERS[i]),
            h('span', { className: 'gdg-oc__text' }, h('span', { className: 'gdg-oc__label' }, op.label), op.description ? h('span', { className: 'gdg-oc__desc' }, op.description) : null)),
          op.other && on ? h('textarea', { className: 'gdg-oc__other', rows: 2, value: other, placeholder: p.otherPlaceholder || 'Describe your answer', 'aria-label': 'Other answer',
            onChange: function (e) { setOther(e.target.value); if (p.onOtherChange) p.onOtherChange(e.target.value); } }) : null);
      }));
  }

  /* ---------- Dropzone ---------- */
  function Dropzone(p) {
    var d = useState(false), over = d[0], setOver = d[1], inp = useRef(null);
    function files(list) { if (p.onFiles) p.onFiles(Array.prototype.slice.call(list || [])); }
    return h('div', { className: cx('gdg-drop', over && 'is-over', p.compact && 'gdg-drop--compact', p.className),
      onDragOver: function (e) { e.preventDefault(); setOver(true); }, onDragLeave: function () { setOver(false); },
      onDrop: function (e) { e.preventDefault(); setOver(false); files(e.dataTransfer.files); } },
      h('span', { className: 'gdg-drop__icon' }, h(Icon, { name: p.icon || 'upload-simple', size: p.compact ? 20 : 28 })),
      h('div', { className: 'gdg-drop__text' }, h('b', null, p.title || 'Drop files here'), h('span', null, p.hint || 'Markdown, PDF or Word, up to 20 MB each')),
      h(Button, { variant: p.primary ? 'primary' : 'glass', size: 'sm', onClick: function () { if (inp.current) inp.current.click(); } }, p.buttonLabel || 'Choose files'),
      h('input', { ref: inp, type: 'file', multiple: p.multiple !== false, accept: p.accept, hidden: true, onChange: function (e) { files(e.target.files); e.target.value = ''; } }));
  }

  /* ---------- FileRow ---------- */
  function fmtSize(b) { if (b == null) return ''; if (typeof b === 'string') return b; return b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB'; }
  function FileRow(p) {
    var status = p.status || 'ready';
    var meta = status === 'uploading' ? null : status === 'done' ? h(Tag, { tone: 'success' }, p.doneText || 'Uploaded') : status === 'error' ? h(Tag, { tone: 'danger' }, p.errorText || 'Failed') : status === 'processing' ? h(Tag, { tone: 'pending' }, 'Processing') : null;
    return h('div', { className: cx('gdg-filerow', 'is-' + status) },
      h('span', { className: 'gdg-filerow__icon' }, h(Icon, { name: p.icon || (/\.md$/i.test(p.name) ? 'file-md' : 'file-text'), size: 20 })),
      h('div', { className: 'gdg-filerow__main' },
        h('div', { className: 'gdg-filerow__top' }, h('span', { className: 'gdg-filerow__name' }, p.name), h('span', { className: 'gdg-filerow__size' }, fmtSize(p.size))),
        status === 'uploading' ? h(ProgressBar, { value: p.progress, label: null }) : null),
      meta,
      p.onRemove && (status === 'ready' || status === 'error') ? h('button', { type: 'button', className: 'gdg-iconbtn', 'aria-label': 'Remove ' + p.name, onClick: p.onRemove }, h(Icon, { name: 'x', size: 16 })) : null);
  }

  /* ---------- Toast ---------- */
  var ToastCtx = React.createContext(null);
  var TONE_ICON = { success: 'check-circle', danger: 'x-circle', info: 'warning-circle', neutral: 'check' };
  function Toast(p) {
    var t = p.tone || 'neutral';
    return h(GlassSurface, { variant: 'strong', floating: true, className: cx('gdg-toast', 'gdg-toast--' + t), role: t === 'danger' ? 'alert' : 'status' },
      h('span', { className: 'gdg-toast__icon' }, h(Icon, { name: TONE_ICON[t] || 'check', size: 20 })),
      h('div', { className: 'gdg-toast__text' }, h('b', null, p.title), p.message ? h('span', null, p.message) : null),
      p.action || null,
      p.onClose ? h('button', { type: 'button', className: 'gdg-iconbtn', 'aria-label': 'Dismiss', onClick: p.onClose }, h(Icon, { name: 'x', size: 16 })) : null);
  }
  function ToastProvider(p) {
    var s = useState([]), list = s[0], setList = s[1], seq = useRef(0);
    var push = useCallback(function (t) {
      var id = ++seq.current; setList(function (l) { return l.concat([Object.assign({ id: id }, t)]); });
      setTimeout(function () { setList(function (l) { return l.filter(function (x) { return x.id !== id; }); }); }, t.duration || 4000);
    }, []);
    return h(ToastCtx.Provider, { value: push }, p.children,
      h('div', { className: 'gdg-toasts', 'aria-live': 'polite' }, list.map(function (t) {
        return h(Toast, Object.assign({ key: t.id }, t, { onClose: function () { setList(function (l) { return l.filter(function (x) { return x.id !== t.id; }); }); } }));
      })));
  }
  function useToast() { return useContext(ToastCtx) || function (t) { if (window.console) console.warn('GDGlass.useToast needs <ToastProvider>', t); }; }

  /* ---------- Breadcrumbs ---------- */
  function Breadcrumbs(p) {
    var items = p.items || [];
    return h('nav', { className: 'gdg-crumbs', 'aria-label': p.label || 'Breadcrumb' }, h('ol', null, items.map(function (it, i) {
      var last = i === items.length - 1;
      return h('li', { key: i },
        last ? h('span', { 'aria-current': 'page' }, it.label) : h('button', { type: 'button', onClick: it.onClick }, it.label),
        last ? null : h(Icon, { name: 'caret-right', size: 12 }));
    })));
  }

  /* ---------- FileTree ---------- */
  function FileTree(p) {
    var ex = useState(function () { var m = {}; (p.defaultExpanded || []).forEach(function (k) { m[k] = true; }); return m; }), expanded = ex[0], setExp = ex[1];
    var s = useControlled(p.selected, p.defaultSelected, p.onSelect), sel = s[0], setSel = s[1];
    function row(n, depth) {
      var folder = n.type === 'folder' || n.children, open = !!expanded[n.id];
      var btn = h('button', { type: 'button', className: cx('gdg-tree__row', sel === n.id && 'is-on'), style: { paddingLeft: 10 + depth * 18 },
        onClick: function () { if (folder) { var m = Object.assign({}, expanded); m[n.id] = !open; setExp(m); } else setSel(n.id); } },
        folder ? h(Icon, { name: open ? 'caret-down' : 'caret-right', size: 12, className: 'gdg-tree__caret' }) : h('span', { className: 'gdg-tree__caret' }),
        h(Icon, { name: folder ? (open ? 'folder-open' : 'folder-simple') : (/\.md$/i.test(n.name) ? 'file-md' : 'file-text'), size: 18 }),
        h('span', { className: 'gdg-tree__name' }, n.name),
        n.badge != null ? h('span', { className: 'gdg-tree__badge' }, n.badge) : null);
      return h('li', { key: n.id, role: 'treeitem', 'aria-expanded': folder ? open : undefined, 'aria-selected': !folder ? sel === n.id : undefined },
        btn, folder && open ? h('ul', { role: 'group' }, (n.children || []).map(function (c) { return row(c, depth + 1); })) : null);
    }
    return h('div', { className: cx('gdg-tree', p.className) },
      p.title || p.actions ? h('div', { className: 'gdg-tree__head' }, h('span', null, p.title), p.actions) : null,
      h('ul', { role: 'tree', 'aria-label': p.label || 'Files' }, (p.nodes || []).map(function (n) { return row(n, 0); })));
  }

  /* ---------- Prose ---------- */
  function Prose(p) {
    return h('article', { className: cx('gdg-prose', p.paper && 'gdg-paper', p.className), dangerouslySetInnerHTML: p.html ? { __html: p.html } : undefined, ref: p.innerRef }, p.html ? undefined : p.children);
  }

  /* ---------- SelectionToolbar ---------- */
  function useTextSelection(containerRef) {
    var s = useState(null), sel = s[0], setSel = s[1];
    useEffect(function () {
      function update() {
        var c = containerRef.current, ws = window.getSelection(); if (!c || !ws || ws.rangeCount === 0 || ws.isCollapsed) { setSel(null); return; }
        var r = ws.getRangeAt(0); if (!c.contains(r.commonAncestorContainer)) { setSel(null); return; }
        var rr = r.getBoundingClientRect(), cr = c.getBoundingClientRect();
        setSel({ text: ws.toString(), range: r.cloneRange(), x: rr.left - cr.left + rr.width / 2 + c.scrollLeft, y: rr.top - cr.top + c.scrollTop, bottom: rr.bottom - cr.top + c.scrollTop });
      }
      function onUp() { setTimeout(update, 0); }
      document.addEventListener('mouseup', onUp); document.addEventListener('keyup', onUp);
      return function () { document.removeEventListener('mouseup', onUp); document.removeEventListener('keyup', onUp); };
    }, []);
    return [sel, function () { setSel(null); var ws = window.getSelection(); if (ws) ws.removeAllRanges(); }];
  }
  function SelectionToolbar(p) {
    var actions = p.actions || [{ id: 'edit', label: 'Edit', icon: 'pencil-simple' }, { id: 'comment', label: 'Comment', icon: 'chat-circle-text' }, { id: 'ask', label: 'Ask SpecBot', icon: 'sparkle', accent: true }];
    return h(GlassSurface, { variant: 'strong', floating: true, className: 'gdg-seltool', role: 'toolbar', 'aria-label': 'Selection actions',
      style: p.x != null ? { position: 'absolute', left: p.x, top: p.y, transform: 'translate(-50%, calc(-100% - 10px))' } : undefined,
      onMouseDown: function (e) { e.preventDefault(); } },
      actions.map(function (a) {
        return h('button', { key: a.id, type: 'button', className: cx('gdg-seltool__btn', a.accent && 'is-accent'), onClick: function () { if (p.onAction) p.onAction(a.id); } },
          h(Icon, { name: a.icon, size: 16 }), a.label);
      }));
  }

  /* ---------- Mentions + CommentThread ---------- */
  function renderMentions(text, people) {
    var names = (people || []).map(function (x) { return x.handle || x.name; }).sort(function (a, b) { return b.length - a.length; });
    var out = [], rest = text, k = 0;
    while (rest.length) {
      var at = rest.indexOf('@'); if (at < 0) { out.push(rest); break; }
      out.push(rest.slice(0, at)); var after = rest.slice(at + 1), hit = null;
      for (var i = 0; i < names.length; i++) if (after.indexOf(names[i]) === 0) { hit = names[i]; break; }
      if (!hit) { var m = after.match(/^[\w.-]+/); hit = m ? m[0] : null; }
      if (hit) { out.push(h('span', { key: 'm' + (k++), className: 'gdg-mention' }, '@' + hit)); rest = after.slice(hit.length); } else { out.push('@'); rest = after; }
    }
    return out;
  }
  function MentionInput(p) {
    var people = p.people || [], ref = useRef(null);
    var s = useControlled(p.value, p.defaultValue || '', p.onChange), val = s[0], set = s[1];
    var q = useState(null), query = q[0], setQuery = q[1], a = useState(0), active = a[0], setActive = a[1];
    var sugg = query == null ? [] : people.filter(function (x) { return (x.handle || x.name).toLowerCase().indexOf(query.toLowerCase()) === 0 || x.name.toLowerCase().indexOf(query.toLowerCase()) >= 0; }).slice(0, 6);
    function onChange(e) {
      var v = e.target.value, pos = e.target.selectionStart, before = v.slice(0, pos), m = before.match(/(^|\s)@([\w .-]{0,24})$/);
      set(v); setQuery(m ? m[2] : null); setActive(0);
    }
    function pick(x) {
      var el = ref.current, pos = el.selectionStart, before = val.slice(0, pos).replace(/@([\w .-]{0,24})$/, '@' + (x.handle || x.name) + ' ');
      var nv = before + val.slice(pos); set(nv); setQuery(null);
      setTimeout(function () { el.focus(); el.setSelectionRange(before.length, before.length); }, 0);
    }
    function onKey(e) {
      if (sugg.length) {
        if (e.key === 'ArrowDown') { e.preventDefault(); setActive((active + 1) % sugg.length); return; }
        if (e.key === 'ArrowUp') { e.preventDefault(); setActive((active - 1 + sugg.length) % sugg.length); return; }
        if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); pick(sugg[active]); return; }
        if (e.key === 'Escape') { setQuery(null); return; }
      }
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && p.onSubmit) { e.preventDefault(); p.onSubmit(val); }
    }
    return h('div', { className: 'gdg-mention-input' },
      h('textarea', { ref: ref, rows: p.rows || 2, value: val, placeholder: p.placeholder || 'Reply… type @ to mention', 'aria-label': p.label || 'Comment', onChange: onChange, onKeyDown: onKey }),
      sugg.length ? h(GlassSurface, { variant: 'strong', floating: true, className: 'gdg-pp__list gdg-mention__list', role: 'listbox' },
        sugg.map(function (x, i) {
          return h('div', { key: x.id || x.name, role: 'option', 'aria-selected': i === active, className: cx('gdg-pp__opt', i === active && 'is-active'), onMouseDown: function (e) { e.preventDefault(); pick(x); } },
            h(Avatar, { name: x.name, kind: x.kind, size: 'sm', title: false }), h('span', null, h('b', null, x.handle ? '@' + x.handle : x.name), h('small', null, x.role || x.email || '')));
        })) : null);
  }
  function CommentThread(p) {
    var people = p.people || [], d = useState(''), draft = d[0], setDraft = d[1];
    return h(GlassSurface, { variant: 'strong', className: cx('gdg-thread', p.resolved && 'is-resolved') },
      h('div', { className: 'gdg-thread__head' },
        p.quote ? h('blockquote', { className: 'gdg-thread__quote' }, p.quote) : h('span', null),
        p.onResolve ? h('button', { type: 'button', className: 'gdg-iconbtn', 'aria-label': 'Resolve thread', title: 'Resolve', onClick: p.onResolve }, h(Icon, { name: 'check', size: 16 })) : null),
      h('ol', { className: 'gdg-thread__list' }, (p.comments || []).map(function (c, i) {
        return h('li', { key: i, className: cx('gdg-comment', c.kind === 'bot' && 'is-bot') },
          h(Avatar, { name: c.author, kind: c.kind, size: 'sm' }),
          h('div', { className: 'gdg-comment__main' },
            h('div', { className: 'gdg-comment__meta' }, h('b', null, c.author), h('span', null, c.time)),
            c.typing ? h(TypingIndicator, null) : h('p', { className: 'gdg-comment__text' }, renderMentions(c.text || '', people))));
      })),
      p.onSubmit ? h('div', { className: 'gdg-thread__compose' },
        h(MentionInput, { people: people, value: draft, onChange: setDraft, onSubmit: function (v) { if (v.trim()) { p.onSubmit(v); setDraft(''); } } }),
        h('div', { className: 'gdg-thread__actions' }, h('small', null, '@ to mention · ⌘↵ to send'),
          h(Button, { variant: 'primary', size: 'sm', disabled: !draft.trim(), onClick: function () { p.onSubmit(draft); setDraft(''); } }, 'Comment'))) : null);
  }

  /* ---------- Severity + DecisionRow ---------- */
  var SEV = { critical: [4, 'Critical'], high: [3, 'High'], medium: [2, 'Medium'], low: [1, 'Low'] };
  function SeverityBadge(p) {
    var lv = SEV[p.level] || SEV.low, bars = [];
    for (var i = 1; i <= 4; i++) bars.push(h('i', { key: i, className: i <= lv[0] ? 'on' : '' }));
    return h('span', { className: cx('gdg-sev', 'gdg-sev--' + (p.level || 'low')) }, h('span', { className: 'gdg-sev__bars', 'aria-hidden': true }, bars), p.label || lv[1]);
  }
  function DecisionList(p) {
    var cols = p.columns || ['Decision', 'Domain', 'Severity', 'Assignee'];
    var n = React.Children.count(p.children);
    return h(GlassSurface, { variant: 'strong', className: 'gdg-dlist' },
      h('div', { className: 'gdg-dlist__head', 'aria-hidden': true }, cols.map(function (c, i) { return h('span', { key: i }, c); }), h('span')),
      n ? h('ul', { className: 'gdg-dlist__rows', 'aria-label': p.label || 'Decisions' }, p.children) : h('div', { className: 'gdg-dlist__empty' }, p.empty || 'Nothing matches these filters.'),
      p.footer ? h('div', { className: 'gdg-dlist__foot' }, p.footer) : null);
  }
  function DecisionRow(p) {
    return h('li', { className: cx('gdg-drow', p.status === 'answered' && 'is-answered') },
      h('button', { type: 'button', className: 'gdg-drow__btn', onClick: p.onClick },
        h('span', { className: 'gdg-drow__title' }, h('b', null, p.title), p.subtitle ? h('small', null, p.subtitle) : null),
        h('span', { className: 'gdg-drow__cell' }, h(Tag, null, p.domain)),
        h('span', { className: 'gdg-drow__cell' }, h(SeverityBadge, { level: p.severity })),
        h('span', { className: 'gdg-drow__who' }, p.assignee ? h(React.Fragment, null, h(Avatar, { name: p.assignee, size: 'sm', title: false }), h('span', null, p.assignee)) : h('span', { className: 'gdg-muted' }, 'Unassigned')),
        h(Icon, { name: 'caret-right', size: 16, className: 'gdg-drow__go' })));
  }

  /* ---------- Decision detail ---------- */
  function DocLinks(p) {
    return h('ul', { className: 'gdg-doclinks' }, (p.links || []).map(function (l, i) {
      return h('li', { key: i }, h('button', { type: 'button', onClick: l.onClick || function () { if (p.onOpen) p.onOpen(l); } },
        h(Icon, { name: 'file-md', size: 18 }), h('span', null, h('b', null, l.title), h('small', null, l.path + (l.section ? ' · ' + l.section : ''))), h(Icon, { name: 'arrow-square-out', size: 14 })));
    }));
  }
  function QuestionBlock(p) {
    var c = useState(!!p.defaultCommentOpen), showC = c[0], setShowC = c[1];
    var input = p.type === 'upload' ? h(Dropzone, { compact: true, title: p.uploadTitle || 'Attach a supporting document', hint: 'Demo only: files are not uploaded', onFiles: p.onFiles })
      : h(OptionCards, { type: p.type === 'yesno' ? 'yesno' : 'choice', options: p.options, allowOther: p.allowOther, label: p.question, defaultValue: p.defaultValue, onChange: p.onChange });
    return h('section', { className: 'gdg-q' },
      h('div', { className: 'gdg-q__head' }, h('span', { className: 'gdg-q__num' }, p.number || 'Q'), h('div', null, h('h3', { className: 'gdg-q__title' }, p.question), p.help ? h('p', { className: 'gdg-q__help' }, p.help) : null)),
      input,
      showC ? G.TextField ? h(G.TextField, { label: 'Comment (optional)', multiline: true, rows: 3, placeholder: 'Add context for the reviewer' }) : null
        : h(Button, { variant: 'plain', size: 'sm', onClick: function () { setShowC(true); } }, 'Add comment'));
  }

  /* ---------- Chat ---------- */
  function TypingIndicator() { return h('span', { className: 'gdg-typing', role: 'status', 'aria-label': 'Typing' }, h('i'), h('i'), h('i')); }
  function ToolSteps(p) {
    return h('ol', { className: 'gdg-steps' }, (p.steps || []).map(function (s, i) {
      return h('li', { key: i, className: 'is-' + (s.status || 'done') },
        s.status === 'running' ? h(Spinner, { size: 14 }) : h(Icon, { name: s.icon || 'check', size: 14 }),
        h('span', null, s.label), s.detail ? h('code', null, s.detail) : null);
    }));
  }
  function Citation(p) {
    return h('button', { type: 'button', className: 'gdg-cite', onClick: p.onClick },
      p.index != null ? h('span', { className: 'gdg-cite__n' }, p.index) : null, h(Icon, { name: 'file-md', size: 14 }), h('span', null, p.label));
  }
  function ChatMessage(p) {
    var user = p.role === 'user';
    return h('div', { className: cx('gdg-msg', user ? 'gdg-msg--user' : 'gdg-msg--bot') },
      user ? null : h(Avatar, { name: p.name || 'SpecBot', kind: 'bot', size: 'md' }),
      h('div', { className: 'gdg-msg__body' },
        user ? null : h('div', { className: 'gdg-msg__name' }, p.name || 'SpecBot'),
        p.steps ? h(ToolSteps, { steps: p.steps }) : null,
        p.typing ? h(TypingIndicator, null) : h('div', { className: user ? 'gdg-msg__bubble' : 'gdg-msg__text' }, p.children),
        p.citations && p.citations.length ? h('div', { className: 'gdg-msg__cites' }, p.citations.map(function (c, i) { return h(Citation, Object.assign({ key: i, index: i + 1 }, c)); })) : null));
  }
  function ChatComposer(p) {
    var s = useControlled(p.value, p.defaultValue || '', p.onChange), v = s[0], set = s[1];
    function send() { if (v.trim() && p.onSend) { p.onSend(v.trim()); set(''); } }
    return h('div', { className: 'gdg-composer-wrap' },
      p.suggestions && p.suggestions.length ? h('div', { className: 'gdg-suggest' }, p.suggestions.map(function (s2, i) {
        return h('button', { key: i, type: 'button', className: 'gdg-suggest__chip', onClick: function () { if (p.onSuggestion) p.onSuggestion(s2); else if (p.onSend) p.onSend(s2); } }, h(Icon, { name: 'sparkle', size: 14 }), s2);
      })) : null,
      h(GlassSurface, { variant: 'strong', floating: true, className: 'gdg-composer' },
        h('textarea', { rows: 1, value: v, placeholder: p.placeholder || 'Ask me', 'aria-label': p.label || 'Message', disabled: p.disabled,
          onChange: function (e) { set(e.target.value); }, onKeyDown: function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } } }),
        h('button', { type: 'button', className: 'gdg-composer__send', 'aria-label': 'Send', disabled: !v.trim() || p.disabled, onClick: send }, h(Icon, { name: 'paper-plane-right', size: 18 }))));
  }

  /* ---------- ConnectorCard ---------- */
  var CONN = { connected: ['success', 'Connected'], attention: ['warning', 'Needs attention'], off: ['neutral', 'Not connected'], checking: ['pending', 'Checking'] };
  function ConnectorCard(p) {
    var st = CONN[p.status || 'off'];
    return h(GlassSurface, { variant: p.variant || 'strong', className: cx('gdg-conn', 'is-' + (p.status || 'off')) },
      h('div', { className: 'gdg-conn__top' },
        h('span', { className: 'gdg-conn__logo' }, p.logoSrc ? h('img', { src: p.logoSrc, alt: '' }) : h('span', { className: 'gdg-conn__mono', title: 'Placeholder: use the vendor logo' }, initials(p.name))),
        h(Tag, { tone: st[0] }, st[1])),
      h('h3', { className: 'gdg-conn__name' }, p.name),
      h('p', { className: 'gdg-conn__cat' }, p.category),
      p.description ? h('p', { className: 'gdg-conn__desc' }, p.description) : null,
      h('div', { className: 'gdg-conn__foot' },
        p.meta ? h('span', { className: 'gdg-conn__meta' }, p.meta) : h('span'),
        h(Button, { variant: p.status === 'off' || !p.status ? 'glass' : 'plain', size: 'sm', onClick: p.onAction }, p.actionLabel || (p.status === 'off' || !p.status ? 'Connect' : p.status === 'attention' ? 'Fix' : 'Manage'))));
  }

  /* ---------- React Flow adapters ---------- */
  function RF() { return window.ReactFlow || null; }
  var SIDES = ['top', 'right', 'bottom', 'left'];
  function Handles() {
    var X = RF(); if (!X) return null;
    var P = X.Position, map = { top: P.Top, right: P.Right, bottom: P.Bottom, left: P.Left };
    return SIDES.reduce(function (a, s) {
      a.push(h(X.Handle, { key: 't-' + s, id: s, type: 'target', position: map[s], className: 'gdg-rf-handle' }));
      a.push(h(X.Handle, { key: 's-' + s, id: s, type: 'source', position: map[s], className: 'gdg-rf-handle' }));
      return a;
    }, []);
  }
  function FlowNode(p) {
    var d = p.data || {};
    return h('div', { className: 'gdg-rf-node' }, h(Handles),
      h(G.DiagramNode, { kind: d.kind, title: d.title, subtitle: d.subtitle, icon: d.icon, badge: d.badge, kindLabel: d.kindLabel, w: d.w || p.width || 200, h: d.h || p.height || 64,
        selected: p.selected || d.selected, dim: d.dim }));
  }
  function FlowGroupNode(p) {
    var d = p.data || {};
    return h('div', { className: cx('gdg-dgroup gdg-rf-group', d.tone === 'plain' && 'gdg-dgroup--plain', d.collapsed && 'is-collapsed') },
      h('span', { className: 'gdg-dgroup__label' }, d.icon ? h(Icon, { name: d.icon, size: 14 }) : null, d.label,
        d.count != null ? h('span', { className: 'gdg-rf-group__count' }, d.count) : null,
        d.onToggle ? h('button', { type: 'button', className: 'gdg-rf-group__toggle nodrag', 'aria-label': d.collapsed ? 'Expand ' + d.label : 'Collapse ' + d.label, onClick: d.onToggle }, h(Icon, { name: d.collapsed ? 'corners-out' : 'minus', size: 12 })) : null));
  }
  function FlowLaneNode(p) {
    var d = p.data || {};
    return h('div', { className: cx('gdg-plane gdg-rf-lane', d.alt && 'gdg-plane--alt') },
      h('span', { className: 'gdg-plane__label', style: { width: d.labelWidth || 132 } }, d.icon ? h(Icon, { name: d.icon, size: 16 }) : null, d.label));
  }
  function FlowStepNode(p) {
    var d = p.data || {}, t = d.type || 'step';
    return h('div', { className: 'gdg-rf-node' }, h(Handles),
      h('div', { style: { position: 'relative', width: d.w || p.width || 156, height: d.h || p.height || 64 } },
        h(G.ProcessNode, { type: t, title: d.title, owner: d.owner, number: d.number, x: 0, y: 0, w: d.w || p.width || 156, h: d.h || p.height || 64, selected: p.selected || d.selected, dim: d.dim })));
  }
  var flowNodeTypes = { gdNode: FlowNode, gdGroup: FlowGroupNode, gdLane: FlowLaneNode, gdStep: FlowStepNode };
  function flowEdge(kind, extra) {
    kind = kind || 'sync';
    var hi = extra && extra.highlight;
    return Object.assign({ type: 'smoothstep', pathOptions: { borderRadius: 12, offset: 24 },
      className: cx('gdg-rf-edge', 'gdg-rf-edge--' + kind, hi && 'is-hi'),
      markerEnd: hi ? 'gd-arrow-hi' : kind === 'data' ? 'gd-arrow-data' : 'gd-arrow-base',
      labelBgPadding: [8, 3], labelBgBorderRadius: 999 }, omit(extra || {}, ['highlight']));
  }
  function FlowMarkers() {
    function m(id, cls) { return h('marker', { id: id, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse', markerUnits: 'userSpaceOnUse' }, h('path', { d: 'M1 1 L9 5 L1 9 Z', className: cls })); }
    return h('svg', { className: 'gdg-rf-markers', width: 0, height: 0, 'aria-hidden': true },
      h('defs', null, m('gd-arrow-base', 'gdg-dmark'), m('gd-arrow-data', 'gdg-dmark gdg-dmark--data'), m('gd-arrow-hi', 'gdg-dmark gdg-dmark--hi')));
  }
  /** Highlight edges touching a node id and dim the rest: returns new [nodes, edges]. */
  function traceSelection(nodes, edges, id) {
    if (!id) return [nodes.map(function (n) { return n.type === 'gdNode' || n.type === 'gdStep' ? Object.assign({}, n, { data: Object.assign({}, n.data, { dim: false, selected: false }) }) : n; }),
      edges.map(function (e) { return Object.assign({}, e, flowEdge(e.data && e.data.kind, { label: e.label })); })];
    var linked = {}; linked[id] = true;
    var es = edges.map(function (e) { var on = e.source === id || e.target === id; if (on) { linked[e.source] = linked[e.target] = true; } return Object.assign({}, e, flowEdge(e.data && e.data.kind, { highlight: on, label: e.label, zIndex: on ? 5 : 0 })); });
    var ns = nodes.map(function (n) { if (n.type !== 'gdNode' && n.type !== 'gdStep') return n; return Object.assign({}, n, { data: Object.assign({}, n.data, { dim: !linked[n.id], selected: n.id === id }) }); });
    return [ns, es];
  }
  function FlowCanvas(p) {
    var X = RF(), st = useState(null), sel = st[0], setSel = st[1];
    if (!X) return h('div', { className: 'gdg-dcanvas', style: { width: '100%', height: p.height || 480, display: 'grid', placeItems: 'center' } }, h('p', { className: 'gdg-muted' }, 'Load @xyflow/react (window.ReactFlow) to render this diagram.'));
    var traced = traceSelection(p.nodes || [], (p.edges || []).map(function (e) { return Object.assign({}, e, { data: Object.assign({ kind: e.kind }, e.data) }); }), sel);
    return h('div', { className: cx('gdg-rf-wrap', p.className), style: { height: p.height || 480 } },
      h(X.ReactFlow, { className: 'gdg-rf', nodes: traced[0], edges: traced[1], nodeTypes: p.nodeTypes || flowNodeTypes, fitView: true, fitViewOptions: { padding: 0.12 },
        proOptions: { hideAttribution: !!p.hideAttribution }, nodesDraggable: p.draggable !== false, minZoom: 0.3, maxZoom: 2,
        onNodeClick: function (e, n) { if (n.type === 'gdNode' || n.type === 'gdStep') setSel(sel === n.id ? null : n.id); }, onPaneClick: function () { setSel(null); } },
        h(FlowMarkers),
        h(X.Background, { variant: X.BackgroundVariant ? X.BackgroundVariant.Dots : 'dots', gap: 24, size: 1.5 }),
        p.controls === false ? null : h(X.Controls, { showInteractive: false, position: 'bottom-right' }),
        p.minimap ? h(X.MiniMap, { pannable: true, zoomable: true, position: 'top-right', nodeBorderRadius: 8 }) : null,
        p.legend === false ? null : h(X.Panel, { position: 'bottom-left' }, h(G.DiagramLegend, { kinds: p.legendKinds, highlight: 'Selected path' }))));
  }

  Object.assign(window.GDGlass, {
    Avatar: Avatar, AvatarGroup: AvatarGroup, Tooltip: Tooltip, Spinner: Spinner, ProgressBar: ProgressBar, Skeleton: Skeleton, EmptyState: EmptyState,
    Menu: Menu, TopBar: TopBar, Modal: Modal, Select: Select, SearchField: SearchField, PeoplePicker: PeoplePicker, LinkField: LinkField, simulateAccess: simulateAccess,
    OptionCards: OptionCards, Dropzone: Dropzone, FileRow: FileRow, Toast: Toast, ToastProvider: ToastProvider, useToast: useToast,
    Breadcrumbs: Breadcrumbs, FileTree: FileTree, Prose: Prose, SelectionToolbar: SelectionToolbar, useTextSelection: useTextSelection,
    MentionInput: MentionInput, CommentThread: CommentThread, renderMentions: renderMentions,
    SeverityBadge: SeverityBadge, DecisionList: DecisionList, DecisionRow: DecisionRow, DocLinks: DocLinks, QuestionBlock: QuestionBlock,
    TypingIndicator: TypingIndicator, ToolSteps: ToolSteps, Citation: Citation, ChatMessage: ChatMessage, ChatComposer: ChatComposer,
    ConnectorCard: ConnectorCard,
    FlowNode: FlowNode, FlowGroupNode: FlowGroupNode, FlowLaneNode: FlowLaneNode, FlowStepNode: FlowStepNode, flowNodeTypes: flowNodeTypes,
    flowEdge: flowEdge, FlowMarkers: FlowMarkers, traceSelection: traceSelection, FlowCanvas: FlowCanvas
  });
})();
