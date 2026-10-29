import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { check, parseContracts } from './api-contract-map.mjs';
import { pathsMatch } from './lib/api-calls.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures', 'api-contract-map');
const SCRIPT = path.join(__dirname, 'api-contract-map.mjs');

test('positive fixture: calls matching one contract yield no findings; a computed path is skipped, not guessed', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'positive') }), []);
});

test('negative fixture: reports a call with no matching contract and a call matching two distinct contracts', () => {
  const findings = check({ root: path.join(FIXTURES, 'negative') });
  assert.deepEqual(findings.map((f) => f.rule).sort(), [
    'api-contract-map-ambiguous',
    'api-contract-map-no-match',
  ]);
  const ambiguous = findings.find((f) => f.rule === 'api-contract-map-ambiguous');
  assert.match(ambiguous.message, /orders\.get, orders\.read/);
  assert.equal(ambiguous.path, 'src/orders.ts');
  assert.equal(ambiguous.line, 4);
});

test('query strings, including dynamically assembled ones, do not affect route matching; callees come from the file', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'query-strings') }), []);
});

test('--contracts and --src point the check at other inputs', () => {
  const root = FIXTURES;
  assert.deepEqual(
    check({
      root,
      options: { src: 'positive/src', contracts: 'positive/api-contracts.json' },
    }),
    [],
  );
  assert.equal(
    check({
      root,
      options: { src: 'negative/src', contracts: 'negative/api-contracts.json' },
    }).length,
    2,
  );
});

test('pathsMatch compares method + pathname, never the query', () => {
  // A whole interpolated suffix, a conditional suffix on a route declared
  // without a query, and a literal `?` + interpolation.
  assert.equal(pathsMatch('/orders{}', '/orders?status=&page='), true);
  assert.equal(pathsMatch('/orders/{}/cancel{}', '/orders/{id}/cancel'), true);
  assert.equal(pathsMatch('/search?{}', '/search?q=&scope='), true);

  // Path parameters still match structurally, by shape and not by name.
  assert.equal(pathsMatch('/orders/{}', '/orders/{id}'), true);

  // A trailing `{}` that is its own segment is a path parameter: it must not
  // collapse into the collection route, and distinct routes stay distinct.
  assert.equal(pathsMatch('/orders/{}', '/orders?status='), false);
  assert.equal(pathsMatch('/nope', '/orders/{id}'), false);
  assert.equal(pathsMatch(null, '/orders?status='), false);
});

test('parseContracts validates the shape and upper-cases methods', () => {
  const parsed = parseContracts({ endpoints: [{ id: 'a', method: 'get', path: '/a' }] });
  assert.deepEqual(parsed.endpoints, [{ id: 'a', method: 'GET', path: '/a' }]);
  assert.equal(parsed.callees.httpGet, 'GET');
  assert.throws(() => parseContracts([]), /must be a JSON object/);
  assert.throws(() => parseContracts({}), /"endpoints" must be a JSON array/);
  assert.throws(
    () => parseContracts({ endpoints: [{ id: 'a', method: 'GET' }] }),
    /needs a string/,
  );
  assert.throws(
    () => parseContracts({ endpoints: [], callees: [] }),
    /"callees" must be an object/,
  );
});

test('no src directory: no findings; a missing contracts file is a configuration error', () => {
  assert.deepEqual(check({ root: path.join(FIXTURES, 'does-not-exist') }), []);
  assert.throws(
    () => check({ root: path.join(FIXTURES, 'positive'), options: { contracts: 'absent.json' } }),
    /--contracts does not exist/,
  );
});

test('CLI: findings exit 1 and a missing contracts file exits 2', () => {
  const run = (args) => spawnSync(process.execPath, [SCRIPT, ...args], { encoding: 'utf8' });
  const found = run([`--root=${path.join(FIXTURES, 'negative')}`]);
  assert.equal(found.status, 1);
  assert.match(found.stdout, /^src\/orders\.ts:3:\d+: api-contract-map-no-match: /m);
  const broken = run([`--root=${path.join(FIXTURES, 'positive')}`, '--contracts=absent.json']);
  assert.equal(broken.status, 2);
  assert.match(broken.stderr, /^tool-error: Error: --contracts does not exist/);
});
