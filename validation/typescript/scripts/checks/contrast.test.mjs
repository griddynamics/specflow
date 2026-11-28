import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { spawnSync } from 'node:child_process';

import { check, derivePairs, parsePairs } from './contrast.mjs';
import { contrastRatio, relativeLuminance } from './lib/oklch.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures', 'contrast');

test('oklch math matches the known achromatic reference points', () => {
  assert.equal(relativeLuminance('oklch(1 0 0)'), 1);
  assert.equal(relativeLuminance('oklch(0 0 0)'), 0);
  assert.equal(contrastRatio('oklch(1 0 0)', 'oklch(0 0 0)'), 21);
  assert.equal(contrastRatio('#fff', 'oklch(0 0 0)'), null);
});

test('derivePairs finds the base pair and every -foreground pair with a matching base', () => {
  const pairs = derivePairs([
    '--background',
    '--foreground',
    '--primary',
    '--primary-foreground',
    '--accent-foreground',
  ]);
  assert.deepEqual(pairs.sort(), [
    ['--background', '--foreground'],
    ['--primary', '--primary-foreground'],
  ]);
});

test('positive fixture: every declared pair meets WCAG AA', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'positive') }), []);
});

test('negative fixture: reports the --primary-foreground/--primary pair below AA', () => {
  const findings = check({ root: path.join(FIXTURES, 'negative') });
  assert.equal(findings.length, 1);
  assert.equal(findings[0].rule, 'contrast-below-aa');
  assert.equal(findings[0].path, 'src/styles/tokens.css');
  assert.equal(findings[0].line, 5);
  assert.match(findings[0].message, /--primary-foreground on --primary has a contrast ratio/);
});

test('--min-ratio raises or lowers the bar', () => {
  const positive = path.join(FIXTURES, 'positive');
  assert.ok(check({ root: positive, options: { 'min-ratio': '21' } }).length > 0);
  assert.deepEqual(
    check({ root: path.join(FIXTURES, 'negative'), options: { 'min-ratio': '1' } }),
    [],
  );
  assert.throws(() => check({ root: positive, options: { 'min-ratio': 'abc' } }), RangeError);
});

test('no theme stylesheet: no findings', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'does-not-exist') }), []);
});

test('--pairs replaces the derived pairs for another naming convention', () => {
  const root = path.join(FIXTURES, 'custom-names');
  assert.deepEqual(derivePairs(['--color-surface', '--color-text']), []);
  const findings = check({ root, options: { pairs: 'pairs.json' } });
  assert.equal(findings.length, 1);
  assert.match(findings[0].message, /--color-text-muted on --color-surface/);
  assert.throws(
    () => check({ root, options: { pairs: 'pairs-undeclared.json' } }),
    /does not declare/,
  );
  assert.throws(() => parsePairs({ pairs: [['--a']] }), /\[background, foreground\]/);
});

test('no derivable pair skips instead of passing clean; --pairs avoids the skip', () => {
  const root = path.join(FIXTURES, 'custom-names');
  const run = (...args) =>
    spawnSync(process.execPath, [path.join(__dirname, 'contrast.mjs'), '--root', root, ...args], {
      encoding: 'utf8',
    });
  const skipped = run();
  assert.equal(skipped.status, 0);
  assert.match(skipped.stderr, /^skipped: no targets: no foreground\/background token pairs in /);
  assert.doesNotMatch(run('--pairs', 'pairs.json').stderr, /^skipped/);
});

test('--tokens-selector reads a class-rooted palette (the .gx pattern)', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'contrast-selector-'));
  try {
    writeFileSync(
      path.join(root, 'tokens.css'),
      '.gx {\n  --paper: oklch(1 0 0);\n  --ink: oklch(0.3 0 0);\n}\n',
    );
    writeFileSync(path.join(root, 'pairs.json'), '{"pairs": [["--paper", "--ink"]]}');
    const options = { 'tokens-css': 'tokens.css', 'tokens-selector': '.gx', pairs: 'pairs.json' };
    assert.deepEqual(check({ root, options }), []); // the pair is ~12.7:1, above AA
    const findings = check({ root, options: { ...options, 'min-ratio': '21' } });
    assert.equal(findings.length, 1);
    assert.equal(findings[0].rule, 'contrast-below-aa');
    assert.equal(findings[0].path, 'tokens.css');
    assert.equal(findings[0].line, 3);
    assert.match(findings[0].message, /--ink on --paper/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
