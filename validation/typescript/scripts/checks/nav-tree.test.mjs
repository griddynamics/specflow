import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { check, parseNavSpec } from './nav-tree.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures', 'nav-tree');
const SCRIPT = path.join(__dirname, 'nav-tree.mjs');
const ROLE_SPEC = { 'nav-spec': '../role-spec.json' };

test('positive fixture: the nav model equals the declared tree (spec found at its default path)', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'positive') }), []);
});

test('negative fixture: reports a wrong icon and a missing child entry', () => {
  const findings = check({
    root: path.join(FIXTURES, 'negative'),
    options: { 'nav-spec': '../positive/nav-spec.json' },
  });
  assert.deepEqual(findings.map((f) => f.rule).sort(), [
    'nav-tree-entry-mismatch',
    'nav-tree-missing-entry',
  ]);
  const mismatch = findings.find((f) => f.rule === 'nav-tree-entry-mismatch');
  assert.match(mismatch.message, /icon: declared "ShoppingCart", code "Package"/);
  assert.equal(mismatch.path, 'src/navigation-tree.tsx');
});

test('restriction cross-check positive: own and inherited restrictions match restrictedRoutes', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'role-positive'), options: ROLE_SPEC }), []);
});

test('restriction cross-check negative: missing restriction, excess own, excess inherited', () => {
  const findings = check({ root: path.join(FIXTURES, 'role-negative'), options: ROLE_SPEC });
  const byKey = new Map(findings.map((f) => [/"([^"]+)"/.exec(f.message)[1], f]));
  assert.deepEqual([...byKey.keys()].sort(), [
    'nav.activity_log',
    'nav.catalog_suppliers',
    'nav.orders',
  ]);

  const missing = byKey.get('nav.catalog_suppliers');
  assert.equal(missing.rule, 'nav-tree-role-restriction-missing');
  assert.equal(missing.fix, 'add `adminOnly: true` to this nav entry');

  const excessOwn = byKey.get('nav.orders');
  assert.equal(excessOwn.rule, 'nav-tree-role-restriction-excess');
  assert.match(excessOwn.message, /its own `adminOnly: true`/);

  const excessInherited = byKey.get('nav.activity_log');
  assert.equal(excessInherited.rule, 'nav-tree-role-restriction-excess');
  assert.match(excessInherited.message, /an ancestor group's `adminOnly: true`/);
});

test('without restrictedRoutes only the tree is compared', () => {
  const findings = check({
    root: path.join(FIXTURES, 'role-negative'),
    options: { 'nav-spec': '../tree-only-spec.json' },
  });
  assert.deepEqual(findings, []);
  const spec = parseNavSpec({ items: [{ label: 'Orders' }] });
  assert.equal(spec.restrictedRoutes, null);
  assert.equal(spec.variable, 'NAV_ITEMS');
  assert.equal(spec.fields.key, 'labelKey');
});

test('a source tree without the configured navigation array is reported', () => {
  const findings = check({
    root: path.join(FIXTURES, 'positive'),
    options: { 'nav-spec': '../menu-spec.json' },
  });
  assert.deepEqual(
    findings.map((f) => [f.rule, f.path]),
    [['nav-tree-missing', 'src']],
  );
  assert.match(findings[0].message, /"MENU"/);
});

test('a malformed spec is a configuration error, never a pass', () => {
  assert.throws(() => parseNavSpec({ items: [{}] }), /needs a string "label"/);
  assert.throws(() => parseNavSpec({ items: 'x' }), /must be a JSON array/);
  assert.throws(
    () => parseNavSpec({ items: [], restrictedRoutes: { '/a': 'yes' } }),
    /must be true or false/,
  );
  assert.throws(
    () => check({ root: path.join(FIXTURES, 'negative') }),
    /--nav-spec does not exist/,
  );
});

test('CLI: findings exit 1, a missing spec exits 2, a missing source root skips', () => {
  const run = (args) => spawnSync(process.execPath, [SCRIPT, ...args], { encoding: 'utf8' });
  const negative = run([
    `--root=${path.join(FIXTURES, 'negative')}`,
    '--nav-spec=../positive/nav-spec.json',
  ]);
  assert.equal(negative.status, 1, negative.stderr);
  assert.match(negative.stdout, /nav-tree-missing-entry/);
  assert.equal(run([`--root=${path.join(FIXTURES, 'negative')}`]).status, 2);
  const skipped = run([`--root=${path.join(FIXTURES, 'does-not-exist')}`]);
  assert.equal(skipped.status, 0);
  assert.match(skipped.stderr, /^skipped: /);
});
