#!/usr/bin/env node
/**
 * Build step for `checks/api-contract-map.mjs`: derives the contracts file from
 * an OpenAPI 3 document in JSON.
 *
 * usage: node tools/openapi-to-contracts.mjs <openapi.json> [--out <file>]
 *        [--base-path <prefix>] [--callees <name=METHOD,...>]
 *
 * Every `paths.<path>.<method>` becomes one endpoint. Its id is the operation's
 * `operationId`, else `<METHOD> <path>`. `--base-path` is prepended to every
 * path (the prefix the client adds, for example `/api`). `--callees` writes the
 * `callees` map (default: the kit's `httpGet`, `httpPost`, ... names). Without
 * `--out` the result is printed on stdout.
 */

import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';

import { parseArgs } from '../checks/lib/files.mjs';
import { isMainModule } from '../checks/lib/main.mjs';
import { DEFAULT_CALLEES } from '../checks/lib/api-calls.mjs';

const METHODS = ['get', 'put', 'post', 'delete', 'patch'];

/**
 * @param {{ paths?: Record<string, Record<string, { operationId?: string }>> }} document
 * @param {{ basePath?: string, callees?: Record<string, string> }} [options]
 */
export function contractsFromOpenApi(document, { basePath = '', callees = DEFAULT_CALLEES } = {}) {
  if (document === null || typeof document !== 'object' || typeof document.paths !== 'object')
    throw new TypeError('not an OpenAPI document: no "paths" object');
  const endpoints = [];
  for (const [route, item] of Object.entries(document.paths ?? {})) {
    for (const method of METHODS) {
      const operation = item?.[method];
      if (!operation) continue;
      const upper = method.toUpperCase();
      const full = `${basePath}${route}`;
      endpoints.push({
        id: operation.operationId ?? `${upper} ${full}`,
        method: upper,
        path: full,
      });
    }
  }
  endpoints.sort((a, b) => a.path.localeCompare(b.path) || a.method.localeCompare(b.method));
  return { callees, endpoints };
}

function parseCallees(text) {
  const out = {};
  for (const pair of text.split(',').filter(Boolean)) {
    const [name, method] = pair.split('=');
    if (!name || !method) throw new TypeError(`--callees entries are name=METHOD, got "${pair}"`);
    out[name.trim()] = method.trim().toUpperCase();
  }
  return out;
}

function main(argv) {
  const args = parseArgs(argv);
  if (args._.length !== 1) {
    console.error('usage: node tools/openapi-to-contracts.mjs <openapi.json> [--out <file>]');
    return 2;
  }
  try {
    const document = JSON.parse(readFileSync(path.resolve(args._[0]), 'utf8'));
    const contracts = contractsFromOpenApi(document, {
      basePath: typeof args['base-path'] === 'string' ? args['base-path'] : '',
      callees: typeof args.callees === 'string' ? parseCallees(args.callees) : DEFAULT_CALLEES,
    });
    const text = `${JSON.stringify(contracts, null, 2)}\n`;
    if (typeof args.out === 'string') writeFileSync(path.resolve(args.out), text);
    else process.stdout.write(text);
    return 0;
  } catch (err) {
    console.error(`tool-error: ${err.message}`);
    return 2;
  }
}

if (isMainModule(import.meta.url)) process.exitCode = main(process.argv.slice(2));
