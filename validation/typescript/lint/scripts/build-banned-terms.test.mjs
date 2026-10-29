import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { bannedTermsRegex, renderBannedTerms } from './build-banned-terms.mjs';

const read = (relative) => readFileSync(new URL(relative, import.meta.url), 'utf8');

test('the shipped semgrep-ds/banned-terms.yml is the build of banned-terms.example.json', () => {
  const example = JSON.parse(read('../banned-terms.example.json'));
  assert.equal(renderBannedTerms(example), read('../semgrep-ds/banned-terms.yml'));
});

test('terms are escaped literals on word boundaries; patterns stay raw', () => {
  const regex = bannedTermsRegex({ terms: ['A.B (x)'], patterns: ['\\d{3}'] });
  assert.equal(regex, '(?i)(?:\\b(?:a\\.b \\(x\\))\\b|\\d{3})');
});

test('a terms file without terms or patterns is refused', () => {
  assert.throws(() => bannedTermsRegex({}), /no term or pattern/);
});
