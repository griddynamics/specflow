import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test, describe } from 'node:test';

import {
  ALLOWED_EXPORTS,
  check,
  checkGeneratedDir,
  topLevelExportNames,
} from './api-types-only.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures', 'api-types-only');

describe('topLevelExportNames', () => {
  test('finds every openapi-typescript top-level export shape', () => {
    const names = topLevelExportNames(
      `export interface paths {}\nexport type webhooks = never;\nexport const $defs = {};\n`,
    );
    assert.deepEqual(names.sort(), ['$defs', 'paths', 'webhooks']);
  });

  test('a separate export list is read too', () => {
    assert.deepEqual(topLevelExportNames('interface a {}\nexport { a };\n'), ['a']);
  });

  test('a non-exported declaration is not reported', () => {
    assert.deepEqual(topLevelExportNames('interface internal {}\n'), []);
  });
});

describe('checkGeneratedDir', () => {
  test("openapi-typescript's own output shape is clean", () => {
    assert.deepEqual(checkGeneratedDir(path.join(FIXTURES, 'clean')), []);
    for (const name of topLevelExportNames(
      readFileSync(path.join(FIXTURES, 'clean', 'api.d.ts'), 'utf8'),
    )) {
      assert.ok(ALLOWED_EXPORTS.has(name));
    }
  });

  test("an export beyond openapi-typescript's own set is reported at its declaration", () => {
    const findings = checkGeneratedDir(path.join(FIXTURES, 'extra-export'));
    assert.equal(findings.length, 1);
    assert.equal(findings[0].rule, 'api-types-only-unexpected-export');
    assert.match(findings[0].message, /ExtraHeaderProps/);
    assert.equal(findings[0].path, 'api.d.ts');
    assert.ok(findings[0].line > 1);
  });

  test('a stray file beside api.d.ts is reported and its own exports are not inspected', () => {
    const findings = checkGeneratedDir(path.join(FIXTURES, 'extra-file'));
    assert.equal(findings.length, 1);
    assert.equal(findings[0].rule, 'api-types-only-extra-file');
    assert.match(findings[0].path, /hand-written\.ts$/);
  });

  test('a missing generated directory is null, not a false clean pass', () => {
    assert.equal(checkGeneratedDir(path.join(FIXTURES, 'does-not-exist')), null);
  });
});

describe('check', () => {
  test('--generated-dir is resolved against the root and labels findings relative to it', () => {
    const findings = check({ root: FIXTURES, options: { 'generated-dir': 'extra-file' } });
    assert.deepEqual(
      findings.map((f) => f.path),
      ['extra-file/hand-written.ts'],
    );
  });

  test('a missing generated directory throws (exit 2 on the CLI)', () => {
    assert.throws(() => check({ root: FIXTURES }), /src\/generated does not exist/);
  });
});
