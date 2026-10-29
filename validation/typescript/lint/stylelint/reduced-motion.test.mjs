import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

import stylelint from 'stylelint';

/**
 * Self-test of the kit's stylelint config:
 * - `a11y/reduced-motion` (./reduced-motion.mjs): a transition or animation is switched off for
 *   `prefers-reduced-motion: reduce` in its own file;
 * - `declaration-property-value-disallowed-list`: no `transition: all`, no removed outline;
 * - the duplicate-declaration and duplicate-selector rules.
 */

const CONFIG = fileURLToPath(new URL('../stylelint.config.mjs', import.meta.url));

async function lint(code, rule) {
  const { results } = await stylelint.lint({
    code,
    codeFilename: 'src/components/panel.css',
    configFile: CONFIG,
  });
  return results[0].warnings.filter((w) => w.rule === rule);
}

const MOTION = 'a11y/reduced-motion';
const LIST = 'declaration-property-value-disallowed-list';

for (const decl of [
  'transition: opacity 150ms ease-out',
  'animation: spin 1s linear infinite',
  'transition-property: transform',
]) {
  test(`negative: "${decl}" with no reduced-motion cover is a finding`, async () => {
    const warnings = await lint(`.lock:disabled { ${decl}; }`, MOTION);
    assert.equal(warnings.length, 1);
    assert.ok(warnings[0].text.includes('prefers-reduced-motion: reduce'));
  });
}

test('negative: a reduced-motion block that names another selector does not cover', async () => {
  const warnings = await lint(
    `.a { transition: opacity 1s; } @media (prefers-reduced-motion: reduce) { .b { transition: none; } }`,
    MOTION,
  );
  assert.equal(warnings.length, 1);
});

test('positive: the selector is switched off in a reduced-motion block', async () => {
  const warnings = await lint(
    `.a { transition: opacity 1s; } .a, .c { animation: x 1s; }
     @media (prefers-reduced-motion: reduce) { .a, .c { transition: none; animation: none; } }`,
    MOTION,
  );
  assert.deepEqual(warnings, []);
});

test('positive: a * rule in the reduced-motion block covers the whole file', async () => {
  const warnings = await lint(
    `.a { transition: opacity 1s; } .b { animation: x 1s; }
     @media (prefers-reduced-motion: reduce) { * { transition: none; animation: none; } }`,
    MOTION,
  );
  assert.deepEqual(warnings, []);
});

test('positive: a delay starts no motion, and an opt-in block needs no cover', async () => {
  const warnings = await lint(
    `.a { animation-delay: 90ms; }
     @media (prefers-reduced-motion: no-preference) { .b { transition: opacity 1s; } }`,
    MOTION,
  );
  assert.deepEqual(warnings, []);
});

for (const decl of [
  'transition: all 0.2s',
  'transition: opacity 1s, all 2s',
  'transition-property: all',
]) {
  test(`negative: "${decl}" is a finding`, async () => {
    const warnings = await lint(`.a { ${decl}; }`, LIST);
    assert.equal(warnings.length, 1);
    assert.ok(warnings[0].text.includes('name the ones that change'));
  });
}

for (const decl of [
  'outline: none',
  'outline: 0',
  'outline: 0px',
  'outline-style: none',
  'outline-width: 0',
  'outline-width: 0px',
]) {
  test(`negative: "${decl}" removes the focus ring and is a finding`, async () => {
    assert.equal((await lint(`.a { ${decl}; }`, LIST)).length, 1);
  });
}

test('positive: named transition properties and a styled outline pass', async () => {
  const warnings = await lint(
    `.a { transition: opacity 1s, transform 1s; transition-property: opacity; outline: 2px solid var(--ring); }`,
    LIST,
  );
  assert.deepEqual(warnings, []);
});

test('negative: a duplicate property and a duplicate selector are findings', async () => {
  assert.equal(
    (await lint('.a { color: red; color: blue; }', 'declaration-block-no-duplicate-properties'))
      .length,
    1,
  );
  assert.equal(
    (await lint('.a { color: red; } .a { margin: 0; }', 'no-duplicate-selectors')).length,
    1,
  );
});
