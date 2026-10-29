import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { check } from './i18n-usage.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures', 'i18n-usage');

test('positive fixture: every static and dynamic key is used and exists', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'positive') }), []);
});

test('negative fixture: reports a missing static key, an unresolved dynamic member and an unused key', () => {
  const findings = check({ root: path.join(FIXTURES, 'negative') });
  const rules = findings.map((f) => f.rule).sort();
  assert.deepEqual(rules, [
    'i18n-usage-dynamic-key-missing-member',
    'i18n-usage-missing-key',
    'i18n-usage-unused-key',
  ]);
  const unused = findings.find((f) => f.rule === 'i18n-usage-unused-key');
  assert.match(unused.message, /app\.unused/);
  assert.equal(unused.path, 'src/locales/en/common.json');
  const dynamic = findings.find((f) => f.rule === 'i18n-usage-dynamic-key-missing-member');
  assert.match(dynamic.message, /status\.band_LOW/);
});

test('a plural key written once uses its plural forms and is not missing', () => {
  const findings = check({ root: path.join(FIXTURES, 'positive') });
  assert.deepEqual(
    findings.filter((f) => /app\.items/.test(f.message)),
    [],
  );
});

test('no locales directory: no findings', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'does-not-exist') }), []);
});

test('the --src and --locales options point the check at other directories', () => {
  const root = path.join(FIXTURES, 'negative');
  assert.deepEqual(check({ root, options: { src: 'lib' } }), []);
  assert.deepEqual(check({ root, options: { locales: 'public/locales' } }), []);
});

test('a call that leaves a {{placeholder}} without a value is a finding; a typed spread or reference is read by type', () => {
  const findings = check({ root: path.join(FIXTURES, 'interpolation') });
  const missing = findings.filter((f) => f.rule === 'i18n-usage-missing-interpolation');
  assert.deepEqual(
    missing.map((f) => f.message.match(/t\("([^"]+)"\)/)[1]),
    ['merge.confirm', 'merge.items', 'merge.unescaped'],
  );
  assert.match(missing[0].message, /\{\{label\}\}/);
  assert.match(missing[1].message, /\{\{count\}\}/);
});
