import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { budgetsOf, check } from './bundle-size.mjs';
import { KIT_DIR, resolveBin } from './lib/bin.mjs';
import {
  entryAssetPaths,
  interpretSizeLimitReport,
  parseBudget,
  sizeLimitConfig,
  totalJavaScriptPaths,
} from './lib/payload-budget.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE = path.join(__dirname, 'fixtures', 'bundle-size');
const BUILD = path.join(FIXTURE, 'web-build');
const SCRIPT = path.join(__dirname, 'bundle-size.mjs');

// Recorded `size-limit --json` output shapes.
const PASSING = JSON.stringify([
  { name: 'entry JavaScript', passed: true, size: 113808, sizeLimit: 160000 },
  { name: 'total JavaScript', passed: true, size: 191468, sizeLimit: 250000 },
]);
const FAILING = JSON.stringify([
  { name: 'entry JavaScript', passed: false, size: 300000, sizeLimit: 160000 },
  { name: 'total JavaScript', passed: true, size: 191468, sizeLimit: 250000 },
]);
const ERROR = JSON.stringify({
  error: 'SizeLimitError: Unknown option *comment* in config.\n    at checkChecks (get-config.js)',
});

describe('interpretSizeLimitReport', () => {
  test('every check passing means no findings', () => {
    assert.deepEqual(interpretSizeLimitReport(PASSING), { findings: [] });
  });

  test('a failing check is one size-limit-exceeded finding; passing checks are silent', () => {
    const { toolError, findings } = interpretSizeLimitReport(FAILING, 'web-build');
    assert.equal(toolError, undefined);
    assert.equal(findings.length, 1);
    assert.equal(findings[0].rule, 'size-limit-exceeded');
    assert.equal(findings[0].path, 'web-build');
    assert.match(findings[0].message, /300000 B exceeds the 160000 B budget/);
  });

  test("size-limit's own error shape and non-JSON output are tool errors, never findings", () => {
    assert.match(interpretSizeLimitReport(ERROR).toolError, /Unknown option \*comment\*/);
    assert.equal(interpretSizeLimitReport('not json').toolError, 'unparseable size-limit output');
    assert.equal(interpretSizeLimitReport('{}').toolError, 'unparseable size-limit output');
  });
});

describe('payload paths and budgets', () => {
  test('entry assets are the scripts and module preloads of index.html, third-party URLs excluded', () => {
    const html = '<script src="/assets/index-abc.js"></script>';
    assert.deepEqual(entryAssetPaths(html, BUILD), [path.join(BUILD, 'assets', 'index-abc.js')]);
    const fromFile = entryAssetPaths(
      '<script type="module" src="/assets/index-abc.js?v=1"></script>' +
        '<link rel="modulepreload" href="/assets/vendor-123.js" />' +
        '<script src="https://cdn.example.com/analytics.js"></script>',
      BUILD,
    );
    assert.deepEqual(fromFile, [
      path.join(BUILD, 'assets', 'index-abc.js'),
      path.join(BUILD, 'assets', 'vendor-123.js'),
    ]);
  });

  test('entry assets must be absolute, local, inside the build directory and present', () => {
    assert.throws(() => entryAssetPaths('<script src="assets/a.js"></script>', BUILD), /absolute/);
    assert.throws(() => entryAssetPaths('<script src="/../a.js"></script>', BUILD), /escapes/);
    assert.throws(() => entryAssetPaths('<p>no scripts</p>', BUILD), /no JavaScript entry/);
  });

  test('the total payload is every .js file under the build directory', () => {
    assert.deepEqual(
      totalJavaScriptPaths(BUILD).map((p) => path.basename(p)),
      ['index-abc.js', 'lazy-page-def.js', 'vendor-123.js'],
    );
  });

  test('budgets are positive integers; only the given budgets become checks', () => {
    assert.equal(parseBudget('1024', '--entry-budget'), 1024);
    assert.throws(() => parseBudget('10kB', '--entry-budget'), /positive integer byte count/);
    assert.deepEqual(budgetsOf({}), { entryBudget: undefined, totalBudget: undefined });
    assert.deepEqual(budgetsOf({ 'total-budget': '5000' }), {
      entryBudget: undefined,
      totalBudget: 5000,
    });
    const config = sizeLimitConfig({ entryPaths: ['a.js'], totalPaths: [], entryBudget: 10 });
    assert.deepEqual(config, [{ name: 'entry JavaScript', path: ['a.js'], limit: '10 B' }]);
  });

  test('a missing build directory is a configuration error, never a pass', () => {
    assert.throws(
      () => check({ root: FIXTURE, options: { dist: 'missing', 'entry-budget': '10' } }),
      /build directory does not exist/,
    );
  });

  test('a failing build command is a build-failed finding', () => {
    const findings = check({
      root: FIXTURE,
      options: { dist: 'web-build', build: `"${process.execPath}" -e "process.exit(3)"` },
    });
    assert.deepEqual(
      findings.map((f) => f.rule),
      ['build-failed'],
    );
  });
});

describe('end to end with the kit-installed size-limit', () => {
  const installed = resolveBin('size-limit', KIT_DIR) !== null;
  const run = (args) => spawnSync(process.execPath, [SCRIPT, ...args], { encoding: 'utf8' });

  const skip = installed ? false : 'size-limit is not installed in the kit (npm install)';

  test('generous budgets pass, a tiny budget is a finding', { skip }, () => {
    const base = [`--root=${FIXTURE}`, '--dist=web-build'];
    const ok = run([...base, '--entry-budget=100000', '--total-budget=100000']);
    assert.equal(ok.status, 0, ok.stderr);
    const over = run([...base, '--entry-budget=1']);
    assert.equal(over.status, 1, over.stderr);
    assert.match(over.stdout, /^web-build:1:1: size-limit-exceeded: entry JavaScript: /m);
  });
});
