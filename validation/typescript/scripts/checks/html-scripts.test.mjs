import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { check } from './html-scripts.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures', 'html-scripts');

test('positive fixture: a module script and a deferred script yield no findings', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'positive') }), []);
});

test('negative fixture: a plain blocking script is reported', () => {
  const findings = check({ root: path.join(FIXTURES, 'negative') });
  assert.equal(findings.length, 1);
  assert.equal(findings[0].rule, 'html-scripts-not-deferred');
  assert.equal(findings[0].path, 'index.html');
  assert.equal(findings[0].line, 8);
});

test('third-party fixture: each tag loading from another origin is reported, a comment is not', () => {
  const findings = check({ root: path.join(FIXTURES, 'third-party') });
  assert.deepEqual(
    findings.map((entry) => [entry.rule, entry.line]),
    [
      ['html-scripts-third-party-origin', 4],
      ['html-scripts-third-party-origin', 12],
    ],
  );
  assert.ok(findings[0].message.includes('https://fonts.googleapis.com'));
  assert.ok(findings[1].message.includes('//cdn.example.com/analytics.js'));
});

test('the --html option points the check at another entry document', () => {
  const root = path.join(FIXTURES, 'negative');
  assert.equal(check({ root, options: { html: 'index.html' } }).length, 1);
  assert.deepEqual(check({ root, options: { html: 'public/missing.html' } }), []);
});

test('no index.html: no findings', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'does-not-exist') }), []);
});
