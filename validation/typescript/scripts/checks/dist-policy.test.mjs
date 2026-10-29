import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test, describe } from 'node:test';

import { check, checkAssetsDir, resolveBanned } from './dist-policy.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures', 'dist-policy');

describe('checkAssetsDir', () => {
  test('a clean dist has no findings', () => {
    assert.deepEqual(checkAssetsDir(path.join(FIXTURES, 'clean-dist', 'assets')), []);
  });

  test('router devtools and msw markers are both reported', () => {
    const findings = checkAssetsDir(path.join(FIXTURES, 'dirty-dist', 'assets'));
    assert.deepEqual(findings.map((f) => f.rule).sort(), [
      'dist-policy-msw',
      'dist-policy-router-devtools',
    ]);
    for (const f of findings) assert.equal(f.path, 'dist/assets/index-abc.js');
  });

  test('a missing dist directory is reported as null, not a false clean pass', () => {
    assert.equal(checkAssetsDir(path.join(FIXTURES, 'does-not-exist', 'assets')), null);
  });
});

describe('check', () => {
  test('--assets points at the built assets and labels findings relative to the root', () => {
    const findings = check({ root: FIXTURES, options: { assets: 'dirty-dist/assets' } });
    assert.equal(findings.length, 2);
    assert.ok(findings.every((f) => f.path === 'dirty-dist/assets/index-abc.js'));
  });

  test('a missing assets directory is a tool error (exit 2) on the CLI', () => {
    const proc = spawnSync(
      process.execPath,
      [path.join(__dirname, 'dist-policy.mjs'), '--root', FIXTURES],
      { encoding: 'utf8' },
    );
    assert.equal(proc.status, 2);
    assert.match(proc.stderr, /^tool-error: .*dist\/assets does not exist/);
  });
});

describe('--banned', () => {
  const custom = {
    root: FIXTURES,
    options: { assets: 'custom-dist/assets', banned: 'banned.json' },
  };

  test('a custom marker is reported alongside the built-in defaults', () => {
    const findings = check(custom);
    assert.deepEqual(
      findings.map((f) => f.rule),
      ['dist-policy-query-devtools'],
    );
    const dirty = check({ ...custom, options: { ...custom.options, assets: 'dirty-dist/assets' } });
    assert.deepEqual(dirty.map((f) => f.rule).sort(), [
      'dist-policy-msw',
      'dist-policy-router-devtools',
    ]);
  });

  test('--banned-replace drops the defaults', () => {
    const dirty = check({
      root: FIXTURES,
      options: { assets: 'dirty-dist/assets', banned: 'banned.json', 'banned-replace': true },
    });
    assert.deepEqual(dirty, []);
  });

  test('a malformed list or --banned-replace alone is a configuration error', () => {
    assert.throws(() => resolveBanned({}), /JSON array/);
    assert.throws(
      () => resolveBanned([{ rule: 'x', label: 'x', markers: [] }]),
      /must not be empty/,
    );
    assert.throws(
      () => check({ root: FIXTURES, options: { 'banned-replace': true } }),
      /needs --banned/,
    );
    assert.throws(
      () => check({ root: FIXTURES, options: { banned: 'missing.json' } }),
      /does not exist/,
    );
  });
});
