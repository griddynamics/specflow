#!/usr/bin/env node
/**
 * `api-contract-map.mjs`: every client call (method + normalised path) maps to
 * exactly one contract of the API's contract list.
 *
 * The contract list is the adopter's own input (`--contracts`, a JSON file, see
 * `examples/api-contracts.example.json`; `tools/openapi-to-contracts.mjs`
 * builds one from an OpenAPI document):
 *
 *   {
 *     "callees": { "httpGet": "GET", "httpPost": "POST" },   // optional
 *     "endpoints": [ { "id": "orders.list", "method": "GET", "path": "/orders" } ]
 *   }
 *
 * Reads every call to a configured callee under `--src` (TS compiler API) and
 * matches it structurally against the endpoints (a `{param}` segment matches any
 * single path segment regardless of its name; a query string never takes part).
 * A call whose path is not a static string or template literal (a fully
 * computed path) cannot be matched and is skipped, never guessed.
 *
 * Rules: `api-contract-map-no-match`, `api-contract-map-ambiguous` (the call
 * matches endpoints with more than one distinct id).
 *
 * usage: node api-contract-map.mjs [--root <dir>] [--src <dir>] [--contracts <file>]
 */

import { existsSync } from 'node:fs';
import path from 'node:path';

import { finding } from './lib/report.mjs';
import { runScript, isMainModule, stringOption } from './lib/main.mjs';
import { listFiles, relPath } from './lib/files.mjs';
import { parseFile, position } from './lib/ts.mjs';
import { DEFAULT_CALLEES, findApiCalls, pathsMatch } from './lib/api-calls.mjs';
import { readJsonInput, requireArray } from './lib/spec-input.mjs';

export const DEFAULT_CONTRACTS = 'api-contracts.json';

/** Validates a parsed contracts file and returns `{ callees, endpoints }`. */
export function parseContracts(data, label = '--contracts') {
  if (data === null || typeof data !== 'object' || Array.isArray(data))
    throw new TypeError(`${label} must be a JSON object with an "endpoints" array`);
  const endpoints = requireArray(data.endpoints, `${label} "endpoints"`);
  for (const [index, endpoint] of endpoints.entries()) {
    const where = `${label} endpoints[${String(index)}]`;
    if (typeof endpoint?.id !== 'string' || endpoint.id === '')
      throw new TypeError(`${where} needs a string "id"`);
    if (typeof endpoint.method !== 'string' || endpoint.method === '')
      throw new TypeError(`${where} needs a string "method"`);
    if (typeof endpoint.path !== 'string' || endpoint.path === '')
      throw new TypeError(`${where} needs a string "path"`);
  }
  const callees = data.callees ?? DEFAULT_CALLEES;
  if (callees === null || typeof callees !== 'object' || Array.isArray(callees))
    throw new TypeError(`${label} "callees" must be an object of callee name to HTTP method`);
  return {
    callees,
    endpoints: endpoints.map((e) => ({ id: e.id, method: e.method.toUpperCase(), path: e.path })),
  };
}

function inputsOf(root, options) {
  return {
    srcRoot: path.resolve(root, stringOption(options, 'src', 'src')),
    contractsFile: stringOption(options, 'contracts', DEFAULT_CONTRACTS),
  };
}

export function check({ root, options = {} }) {
  const { srcRoot, contractsFile } = inputsOf(root, options);
  if (!existsSync(srcRoot)) return [];
  const { callees, endpoints } = parseContracts(
    readJsonInput(root, contractsFile, '--contracts'),
    contractsFile,
  );
  if (endpoints.length === 0) return [];

  const files = listFiles(srcRoot, { extensions: ['.ts', '.tsx'] }).filter(
    (f) => !f.endsWith('.test.ts') && !f.endsWith('.test.tsx'),
  );
  const findings = [];

  for (const file of files) {
    const sourceFile = parseFile(file);
    for (const call of findApiCalls(sourceFile, callees)) {
      if (call.path === null) continue; // computed path: cannot be matched, not guessed
      const matches = endpoints.filter(
        (e) => e.method === call.method && pathsMatch(call.path, e.path),
      );
      const { line, col } = position(sourceFile, call.node);
      const relFile = relPath(root, file);
      if (matches.length === 0) {
        findings.push(
          finding(
            relFile,
            line,
            col,
            'api-contract-map-no-match',
            `${call.method} "${call.path}" matches no contract`,
          ),
        );
      } else {
        const ids = [...new Set(matches.map((m) => m.id))];
        if (ids.length > 1) {
          findings.push(
            finding(
              relFile,
              line,
              col,
              'api-contract-map-ambiguous',
              `${call.method} "${call.path}" matches more than one contract: ${ids.join(', ')}`,
            ),
          );
        }
      }
    }
  }

  return findings;
}

function skipIf({ root, options }) {
  const { srcRoot } = inputsOf(root, options);
  if (!existsSync(srcRoot)) return `no targets: ${relPath(root, srcRoot)} does not exist`;
  try {
    const { contractsFile } = inputsOf(root, options);
    const { endpoints } = parseContracts(readJsonInput(root, contractsFile, '--contracts'));
    if (endpoints.length === 0) return 'no targets: the contracts file lists no endpoints';
  } catch {
    return null; // check() reports the unreadable input as a configuration error
  }
  return null;
}

if (isMainModule(import.meta.url)) runScript(check, { skipIf });
