import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

import { budget, entryAssetPaths, totalJavaScriptPaths } from './assets.mjs';
import { strictTestOptions } from '../vitest.strict.mjs';

test('the entry assets are the local module scripts and module preloads of index.html', () => {
  const html = [
    '<script type="module" crossorigin src="/assets/index-a1.js"></script>',
    '<link rel="modulepreload" href="/assets/vendor-b2.js">',
    '<script src="https://cdn.example.com/x.js" type="module"></script>',
    '<link rel="stylesheet" href="/assets/index.css">',
  ].join('\n');
  assert.deepEqual(entryAssetPaths(html, '/dist'), [
    path.join('/dist', 'assets/index-a1.js'),
    path.join('/dist', 'assets/vendor-b2.js'),
  ]);
});

test('the total is every .js file under the build output', () => {
  const dist = mkdtempSync(path.join(os.tmpdir(), 'size-limit-kit-'));
  try {
    mkdirSync(path.join(dist, 'assets'));
    writeFileSync(path.join(dist, 'assets', 'a.js'), '');
    writeFileSync(path.join(dist, 'assets', 'a.css'), '');
    writeFileSync(path.join(dist, 'b.js'), '');
    assert.deepEqual(totalJavaScriptPaths(dist).sort(), [
      path.join(dist, 'assets', 'a.js'),
      path.join(dist, 'b.js'),
    ]);
  } finally {
    rmSync(dist, { recursive: true, force: true });
  }
});

test('a budget is whole bytes from the environment, else the default', () => {
  assert.equal(budget({}, 'X', 100), '100 B');
  assert.equal(budget({ X: '250' }, 'X', 100), '250 B');
  assert.throws(() => budget({ X: '25kB' }, 'X', 100), /whole number of bytes/);
});

test('strict Vitest options: no .only, no empty suite, no leaks, shuffled, fail on console', () => {
  const options = strictTestOptions({ coverageThresholds: { lines: 80 } });
  assert.equal(options.allowOnly, false);
  assert.equal(options.passWithNoTests, false);
  assert.equal(options.restoreMocks, true);
  assert.equal(options.unstubGlobals, true);
  assert.equal(options.sequence.shuffle, true);
  assert.equal(options.retry, 0);
  assert.match(options.setupFiles[0], /vitest\.setup\.mjs$/);
  assert.deepEqual(options.coverage.thresholds, { lines: 80 });
  assert.equal(strictTestOptions().coverage.thresholds, undefined);
});
