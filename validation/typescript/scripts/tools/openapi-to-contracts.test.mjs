import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import { contractsFromOpenApi } from './openapi-to-contracts.mjs';

const SCRIPT = path.join(path.dirname(fileURLToPath(import.meta.url)), 'openapi-to-contracts.mjs');

const DOCUMENT = {
  openapi: '3.1.0',
  paths: {
    '/orders': {
      get: { operationId: 'listOrders' },
      post: {},
      parameters: [],
    },
    '/orders/{id}': { get: { operationId: 'getOrder' } },
  },
};

test('every operation becomes one endpoint, sorted, with the base path prepended', () => {
  const { endpoints } = contractsFromOpenApi(DOCUMENT, { basePath: '/api' });
  assert.deepEqual(endpoints, [
    { id: 'listOrders', method: 'GET', path: '/api/orders' },
    { id: 'POST /api/orders', method: 'POST', path: '/api/orders' },
    { id: 'getOrder', method: 'GET', path: '/api/orders/{id}' },
  ]);
});

test('a document without paths is refused', () => {
  assert.throws(() => contractsFromOpenApi({}), /no "paths" object/);
});

test('CLI writes the contracts file the api-contract-map check reads', (t) => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'openapi-contracts-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const input = path.join(dir, 'openapi.json');
  const output = path.join(dir, 'api-contracts.json');
  writeFileSync(input, JSON.stringify(DOCUMENT));
  const result = spawnSync(
    process.execPath,
    [SCRIPT, input, '--out', output, '--callees', 'fetchJson=get'],
    { encoding: 'utf8' },
  );
  assert.equal(result.status, 0, result.stderr);
  const written = JSON.parse(readFileSync(output, 'utf8'));
  assert.deepEqual(written.callees, { fetchJson: 'GET' });
  assert.equal(written.endpoints.length, 3);
  assert.equal(spawnSync(process.execPath, [SCRIPT], { encoding: 'utf8' }).status, 2);
});
