import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { check, parseTokenSpec } from './tokens.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures', 'tokens');

test('a var(--x) reference to a property :root does not declare is reported at the referencing line', () => {
  const findings = check({ root: path.join(FIXTURES, 'negative') });
  assert.equal(findings.length, 1);
  assert.equal(findings[0].rule, 'tokens-undeclared-reference');
  assert.equal(findings[0].path, 'src/components/card.tsx');
  assert.equal(findings[0].line, 4);
  assert.match(findings[0].message, /--success-muted/);
});

test('declared references, spaced references and fallbacks are not reported', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'positive') }), []);
});

test('generated build output (.next) is not scanned for references', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'tokens-next-'));
  try {
    mkdirSync(path.join(root, 'src', 'styles'), { recursive: true });
    mkdirSync(path.join(root, 'src', '.next', 'static'), { recursive: true });
    writeFileSync(path.join(root, 'src', 'styles', 'tokens.css'), ':root {\n  --accent: red;\n}\n');
    writeFileSync(path.join(root, 'src', 'app.css'), '.a { color: var(--accent); }\n');
    writeFileSync(
      path.join(root, 'src', '.next', 'static', 'chunk.css'),
      '.b { color: var(--xy-edge); }\n',
    );
    assert.deepEqual(check({ root }), []);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('a var(--x) inside a CSS comment is not a reference; one after the comment still is', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'tokens-comment-'));
  try {
    mkdirSync(path.join(root, 'src', 'styles'), { recursive: true });
    writeFileSync(path.join(root, 'src', 'styles', 'tokens.css'), ':root {\n  --accent: red;\n}\n');
    writeFileSync(
      path.join(root, 'src', 'app.css'),
      [
        '/* Values come from tokens.css (var(--token)). */',
        '.a { background: url("a/*b.png"); color: var(--accent); }',
        '/* multi-line',
        '   var(--placeholder) */ .b { color: var(--missing); }',
        '',
      ].join('\n'),
    );
    const findings = check({ root });
    assert.deepEqual(
      findings.map((f) => [f.rule, f.path, f.line, f.col]),
      [['tokens-undeclared-reference', 'src/app.css', 4, 38]],
    );
    assert.match(findings[0].message, /--missing/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('no theme stylesheet: no findings', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'does-not-exist') }), []);
  assert.deepEqual(
    check({ root: path.join(FIXTURES, 'negative'), options: { 'tokens-css': 'theme.css' } }),
    [],
  );
});

test('--tokens-spec: a listed token that :root lacks or declares with another value is reported', () => {
  const findings = check({
    root: path.join(FIXTURES, 'positive'),
    options: { 'tokens-spec': '../tokens-spec.json' },
  });
  assert.deepEqual(
    findings.map((f) => [f.rule, f.path, f.line]),
    [
      ['tokens-value-mismatch', 'src/styles/tokens.css', 4],
      ['tokens-missing', 'src/styles/tokens.css', 1],
    ],
  );
  assert.match(findings[0].message, /"--muted"/);
  assert.match(findings[1].message, /"--accent"/);
});

test('--tokens-spec: a malformed or missing token list is a configuration error', () => {
  assert.throws(() => parseTokenSpec({ tokens: [{ name: 'accent' }] }), /starting with "--"/);
  assert.throws(() => parseTokenSpec({}), /must be a JSON array/);
  assert.throws(
    () => check({ root: path.join(FIXTURES, 'positive'), options: { 'tokens-spec': 'none.json' } }),
    /--tokens-spec does not exist/,
  );
});
