import { test } from 'node:test';
import assert from 'node:assert/strict';

import stylelint from 'stylelint';

import { defineStylelintConfig } from '../stylelint.config.mjs';

/**
 * Self-test of the opt-in stylelint options: the single vertical scroller (with its exempt
 * stylesheet), the one containment class and the one palette. Each is off by default.
 */

const STRICT = defineStylelintConfig({
  singleScroller: { exempt: ['**/styles/scroll-regions.css'] },
  containmentClass: '.long-list',
  secondPaletteSelectors: ['/\\.dark\\b/'],
});

async function warnings(code, config, codeFilename = 'src/components/panel.css') {
  const { results } = await stylelint.lint({ code, codeFilename, config });
  return results[0].warnings.map((warning) => warning.rule);
}

const LIST = 'declaration-property-value-disallowed-list';

test('a vertical scroller is reported only when the single-scroller option is set', async () => {
  const css = '.panel { overflow-y: auto; }';
  assert.ok((await warnings(css, STRICT)).includes(LIST));
  assert.ok(!(await warnings(css, defineStylelintConfig())).includes(LIST));
});

test('the two-value shorthand is a vertical scroller only when y scrolls', async () => {
  assert.ok((await warnings('.a { overflow: hidden auto; }', STRICT)).includes(LIST));
  assert.ok(!(await warnings('.a { overflow: auto hidden; }', STRICT)).includes(LIST));
});

test('max-height is a clipped region outside the exempt stylesheet', async () => {
  assert.ok((await warnings('.a { max-height: 20rem; }', STRICT)).includes(LIST));
  assert.ok(
    !(
      await warnings('.a { max-height: 20rem; }', STRICT, 'src/styles/scroll-regions.css')
    ).includes(LIST),
  );
});

test('the exempt stylesheet keeps the motion and focus bans', async () => {
  assert.ok(
    (await warnings('.a { outline: none; }', STRICT, 'src/styles/scroll-regions.css')).includes(
      LIST,
    ),
  );
});

test('only the containment class declares content-visibility', async () => {
  const rule = 'rule-selector-property-disallowed-list';
  assert.ok((await warnings('.card { content-visibility: auto; }', STRICT)).includes(rule));
  assert.ok(!(await warnings('.long-list { content-visibility: auto; }', STRICT)).includes(rule));
});

test('a second palette selector is reported', async () => {
  const rule = 'selector-disallowed-list';
  assert.ok((await warnings('.dark .a { color: var(--fg); }', STRICT)).includes(rule));
  assert.ok(!(await warnings('.darker { color: var(--fg); }', STRICT)).includes(rule));
});
