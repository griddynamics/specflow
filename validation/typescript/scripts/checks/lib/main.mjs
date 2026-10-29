/**
 * Common CLI bootstrap for every `checks/*.mjs` script: resolves the target
 * root (`--root=<dir>`, default the current directory), collects every other
 * `--name=value` flag as a check option, runs the script's `check()` function
 * (sync or async) and prints its findings in the shared contract
 * (`./report.mjs`).
 *
 * One copy of the CLI contract (exit codes, `--root`, `skipIf` -> exit 0 with
 * `skipped:` on stderr, an exception -> exit 2) for every script.
 */

import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { parseArgs } from './files.mjs';
import { report, skip, toolError } from './report.mjs';

/**
 * @param {(ctx: { root: string, options: Record<string, string | boolean> }) => any} checkFn
 * @param {{ skipIf?: (ctx: { root: string, options: Record<string, string | boolean> }) => string | null }} [opts]
 */
export async function runScript(checkFn, opts = {}) {
  const { _: positional, root: rootArg, ...options } = parseArgs(process.argv.slice(2));
  if (positional.length) {
    toolError(`unexpected positional arguments: ${positional.join(' ')}`);
    return;
  }
  const root = path.resolve(String(rootArg ?? process.cwd()));
  const ctx = { root, options };
  if (opts.skipIf) {
    const reason = opts.skipIf(ctx);
    if (reason) {
      skip(reason);
      return;
    }
  }

  let findings;
  try {
    findings = await checkFn(ctx);
  } catch (err) {
    toolError(`${err?.name ?? 'Error'}: ${err?.message ?? String(err)}`);
    return;
  }
  report(findings);
}

/** A string option, or `fallback` when it is absent or a bare flag. */
export function stringOption(options, name, fallback) {
  const value = options?.[name];
  return typeof value === 'string' && value !== '' ? value : fallback;
}

/** A comma-separated list option (or an array, when called from code). */
export function listOption(options, name, fallback = []) {
  const value = options?.[name];
  if (Array.isArray(value)) return value;
  if (typeof value !== 'string' || value === '') return fallback;
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

/** True when this module is being run directly (`node script.mjs`) rather
 * than imported by a test. */
export function isMainModule(importMetaUrl) {
  return fileURLToPath(importMetaUrl) === path.resolve(process.argv[1] ?? '');
}
