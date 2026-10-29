import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { check } from './i18n-protected.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures', 'i18n-protected');

test('positive fixture: verbatim code stays verbatim, human labels translate freely', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'positive') }), []);
});

test('negative fixture: every code-equal label that is translated is reported', () => {
  const findings = check({ root: path.join(FIXTURES, 'negative') });
  assert.deepEqual(
    findings.map((f) => f.rule),
    ['i18n-protected-code-not-verbatim', 'i18n-protected-code-not-verbatim'],
  );
  assert.ok(findings.some((f) => /priority\.HIGH/.test(f.message) && /ALTA/.test(f.message)));
  assert.ok(findings.some((f) => /orderState\.NEW/.test(f.message) && /NOVO/.test(f.message)));
  for (const f of findings) assert.equal(f.path, 'src/locales/pt-BR/common.json');
});

test('a human label (en value differs from the code) is never protected', () => {
  const findings = check({ root: path.join(FIXTURES, 'negative') });
  assert.ok(findings.every((f) => !/status\.band_/.test(f.message)));
});

test('a configured localised-code family is translated freely, though its en label equals the code', () => {
  const findings = check({
    root: path.join(FIXTURES, 'negative'),
    options: { 'localised-code-families': 'orderState.' },
  });
  assert.equal(findings.length, 1);
  assert.match(findings[0].message, /priority\.HIGH/);
});

test('--namespaces: a listed namespace with no base-locale key is reported', () => {
  const findings = check({
    root: path.join(FIXTURES, 'positive'),
    options: { namespaces: 'orderState,status,orders' },
  });
  assert.deepEqual(
    findings.map((f) => [f.rule, f.path]),
    [['i18n-protected-namespace-missing', 'src/locales/en']],
  );
  assert.match(findings[0].message, /"orders"/);
});

test('no locales directory: no findings', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'does-not-exist') }), []);
});
