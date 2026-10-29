// Generic stylelint config: duplicate hygiene, motion that honours `prefers-reduced-motion`, and a
// focus indicator that is styled rather than removed (WCAG 2.2 SC 2.3.3, 2.4.7). Native rules
// only, plus the one custom plugin in ./stylelint/reduced-motion.mjs.
//
// The default export is the generic config. A project whose layout or theme policy is stricter
// writes its own `stylelint.config.mjs`:
//
//   import { defineStylelintConfig } from './validation/typescript/lint/stylelint.config.mjs';
//   export default defineStylelintConfig({
//     singleScroller: { exempt: ['**/styles/scroll-regions.css'] },
//     containmentClass: '.long-list',
//     secondPaletteSelectors: ['/\\.dark\\b/'],
//   });
//
// Each option is off until it is set.
import { fileURLToPath } from 'node:url';

const A11Y = 'WCAG 2.2 SC 2.3.3, 2.4.7';

// The `/regex/` keys match the property; the arrays hold the banned values.
const BANNED_MOTION_FOCUS = {
  // `all` transitions properties nobody chose (layout included) and bypasses the reduced-motion
  // cover; name the properties.
  transition: ['/(^|[\\s,])all($|[\\s,])/'],
  'transition-property': ['all'],
  // The `:focus-visible` ring is the baseline that covers every `:hover` rule.
  outline: ['/^(none|0(px)?)$/'],
  'outline-style': ['none'],
  'outline-width': ['/^0(px)?$/'],
};

// The page content region is the only vertical scroller (`overflow-x` is not listed: a horizontal
// scroller is legal, and the semgrep `css-*` rules check that it states `overflow-y: hidden`).
const BANNED_SCROLL = {
  'overflow-y': ['/^(auto|scroll)$/'],
  // Shorthand `overflow: <x> <y>`: only a vertical scroller (single value, or y = last value).
  overflow: ['/^(auto|scroll)$/', '/\\s(auto|scroll)$/'],
  'max-height': ['/.*/'],
};

const message = (property, value) =>
  property.startsWith('transition')
    ? `"${property}: ${value}" transitions every property; name the ones that change (${A11Y})`
    : property.startsWith('outline')
      ? `"${property}: ${value}" removes the focus indicator; style \`:focus-visible\` instead (${A11Y})`
      : `"${property}: ${value}" makes a vertical scroller or clips what it holds; the page content region is the only one. Move a sanctioned pinned, floating or modal region to the exempt stylesheet with its reason`;

const escapeRegExp = (text) => text.replaceAll(/[.*+?^${}()|[\]\\]/gu, '\\$&');

/**
 * @param {object} [options]
 * @param {{ exempt?: string[] }} [options.singleScroller] ban vertical scrollers outside the
 *   exempt stylesheets (globs).
 * @param {string} [options.containmentClass] the one selector allowed to declare
 *   `content-visibility` / `contain-intrinsic-*`.
 * @param {string[]} [options.secondPaletteSelectors] selector patterns of a second palette
 *   (e.g. `/\\.dark\\b/`) in a project that has one.
 */
export function defineStylelintConfig(options = {}) {
  const scroll = options.singleScroller;
  const rules = {
    // Every transition or animation is switched off for `prefers-reduced-motion: reduce` in its file.
    'a11y/reduced-motion': true,
    'no-duplicate-selectors': true,
    'declaration-block-no-duplicate-properties': true,
    'declaration-block-no-duplicate-custom-properties': true,
    'declaration-property-value-disallowed-list': [
      scroll == null ? BANNED_MOTION_FOCUS : { ...BANNED_SCROLL, ...BANNED_MOTION_FOCUS },
      { message },
    ],
  };
  if (options.containmentClass != null) {
    const allowed = escapeRegExp(options.containmentClass);
    rules['rule-selector-property-disallowed-list'] = [
      { [`/^(?!${allowed}$)/`]: ['content-visibility', '/^contain-intrinsic-/'] },
      {
        message: (selector, property) =>
          `"${selector}" declares "${property}"; only \`${options.containmentClass}\` is a containment unit`,
      },
    ];
  }
  if ((options.secondPaletteSelectors ?? []).length > 0) {
    rules['selector-disallowed-list'] = [
      options.secondPaletteSelectors,
      {
        message: (selector) =>
          `"${selector}" selects a second palette; the application has one, declared on :root`,
      },
    ];
  }
  return {
    plugins: [fileURLToPath(new URL('./stylelint/reduced-motion.mjs', import.meta.url))],
    rules,
    overrides:
      scroll == null || (scroll.exempt ?? []).length === 0
        ? []
        : [
            {
              // The sanctioned stylesheets: every rule in them is a pinned, floating, drawer or
              // panel-body scroll region with a written reason. Only the scroll bans lift.
              files: scroll.exempt,
              rules: {
                'declaration-property-value-disallowed-list': [BANNED_MOTION_FOCUS, { message }],
              },
            },
          ],
  };
}

export default defineStylelintConfig();
