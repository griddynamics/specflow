import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { check, parseRequiredStrings } from './i18n-catalogues.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures', 'i18n-catalogues');

test('positive fixture: matching catalogues yield no findings', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'positive') }), []);
});

test('negative fixture: reports missing key, extra key, placeholder mismatch, non-string and empty values', () => {
  const findings = check({ root: path.join(FIXTURES, 'negative') });
  const rules = findings.map((f) => f.rule).sort();
  assert.deepEqual(rules, [
    'i18n-catalogue-empty-value',
    'i18n-catalogue-extra-key',
    'i18n-catalogue-missing-key',
    'i18n-catalogue-non-string-value',
    'i18n-catalogue-placeholder-mismatch',
  ]);
  for (const f of findings) assert.ok(f.line >= 1 && f.col >= 1);
});

test('no locales directory: no findings (nothing to check)', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'does-not-exist') }), []);
});

test('the --locales option points the check at another catalogue directory', () => {
  const root = path.join(FIXTURES, 'negative');
  assert.equal(check({ root, options: { locales: 'src/locales' } }).length, 5);
  assert.deepEqual(check({ root, options: { locales: 'public/locales' } }), []);
});

test('the --base-locale option chooses the locale the others are compared against', () => {
  const findings = check({
    root: path.join(FIXTURES, 'negative'),
    options: { 'base-locale': 'pt-BR' },
  });
  const extra = findings.find((f) => f.rule === 'i18n-catalogue-extra-key');
  assert.match(extra.message, /does not exist in pt-BR/);
});

test('--required-strings: a fixed string that no base-locale value carries is reported', () => {
  const findings = check({
    root: path.join(FIXTURES, 'positive'),
    options: { 'required-strings': '../required-strings.json' },
  });
  assert.deepEqual(
    findings.map((f) => [f.rule, f.path]),
    [['i18n-catalogue-missing-required-string', 'src/locales/en']],
  );
  assert.match(findings[0].message, /\(supplier list, empty\): "No supplier has been added yet\."/);
});

test('--required-strings: a malformed or missing list is a configuration error', () => {
  assert.deepEqual(parseRequiredStrings(['A']), [{ value: 'A', label: '' }]);
  assert.throws(() => parseRequiredStrings({ strings: [{ label: 'x' }] }), /non-empty string/);
  assert.throws(() => parseRequiredStrings({}), /must be a JSON array/);
  assert.throws(
    () =>
      check({ root: path.join(FIXTURES, 'positive'), options: { 'required-strings': 'no.json' } }),
    /--required-strings does not exist/,
  );
});

test('a field placeholder must end with the single ellipsis character in every locale', () => {
  const findings = check({ root: path.join(FIXTURES, 'placeholder-negative') });
  assert.deepEqual(findings.map((f) => `${f.rule}:${f.path}`).sort(), [
    'i18n-catalogue-placeholder-ellipsis:src/locales/en/common.json',
    'i18n-catalogue-placeholder-ellipsis:src/locales/en/common.json',
    'i18n-catalogue-placeholder-ellipsis:src/locales/pt-BR/common.json',
  ]);
});

test('--placeholder-ellipsis false turns the ellipsis rule off and keeps the rest', () => {
  const root = path.join(FIXTURES, 'placeholder-negative');
  assert.deepEqual(check({ root, options: { 'placeholder-ellipsis': 'false' } }), []);
  assert.equal(check({ root, options: { 'placeholder-ellipsis': 'true' } }).length, 3);
  assert.throws(() => check({ root, options: { 'placeholder-ellipsis': 'no' } }), RangeError);
});
