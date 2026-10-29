#!/usr/bin/env node
/**
 * `bundle-size.mjs`: the built JavaScript stays within its byte budgets,
 * measured by size-limit (Brotli, the `@size-limit/file` default).
 *
 * Two modes:
 * - Budgets given (`--entry-budget` and/or `--total-budget`, bytes): the kit
 *   measures the entry payload (the scripts and module preloads the built
 *   `index.html` loads up front) and/or every `.js`/`.mjs` file under the build
 *   directory, with a size-limit configuration it writes itself. Nothing is
 *   needed in the target beyond its build output.
 * - No budget given: size-limit runs in the target root with the target's own
 *   configuration (`.size-limit.*` or the `size-limit` key of package.json).
 *
 * `--build <command>` runs a build first (through the shell, in the root, for
 * example `"npm run build"`); a failing build is a `build-failed` finding.
 * Without it the build output must already exist.
 *
 * Rules: `size-limit-exceeded`, `build-failed`. A missing build directory, a
 * missing size-limit, or size-limit's own error output is a tool error (exit
 * 2), never a finding and never a pass.
 *
 * usage: node bundle-size.mjs [--root <dir>] [--dist <dir>] [--html <file>]
 *        [--entry-budget <bytes>] [--total-budget <bytes>] [--build <command>]
 */

import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { finding } from './lib/report.mjs';
import { runScript, isMainModule, stringOption } from './lib/main.mjs';
import { relPath } from './lib/files.mjs';
import { KIT_DIR, resolveBinIn } from './lib/bin.mjs';
import {
  entryAssetPaths,
  interpretSizeLimitReport,
  parseBudget,
  sizeLimitConfig,
  totalJavaScriptPaths,
} from './lib/payload-budget.mjs';

const MAX_BUFFER = 64 * 1024 * 1024;

function firstLine(text) {
  return (
    String(text ?? '')
      .split('\n')
      .map((line) => line.trim())
      .find(Boolean) ?? ''
  );
}

/** The budgets of the options, `undefined` where absent. */
export function budgetsOf(options) {
  const entry = stringOption(options, 'entry-budget', '');
  const total = stringOption(options, 'total-budget', '');
  return {
    entryBudget: entry ? parseBudget(entry, '--entry-budget') : undefined,
    totalBudget: total ? parseBudget(total, '--total-budget') : undefined,
  };
}

/** Writes the generated size-limit config into a temporary directory. */
export function writeGeneratedConfig({ dist, html, entryBudget, totalBudget }) {
  const entryPaths =
    entryBudget === undefined ? [] : entryAssetPaths(readFileSync(html, 'utf8'), dist);
  const totalPaths = totalBudget === undefined ? [] : totalJavaScriptPaths(dist);
  const config = sizeLimitConfig({ entryPaths, totalPaths, entryBudget, totalBudget });
  const dir = mkdtempSync(path.join(os.tmpdir(), 'bundle-size-'));
  const file = path.join(dir, '.size-limit.json');
  writeFileSync(file, `${JSON.stringify(config, null, 2)}\n`);
  return { dir, file };
}

function runBuild(command, root) {
  const proc = spawnSync(command, {
    cwd: root,
    shell: true,
    encoding: 'utf8',
    maxBuffer: MAX_BUFFER,
  });
  if (proc.status === 0) return null;
  return firstLine(proc.stderr) || firstLine(proc.stdout) || `build exited ${proc.status}`;
}

export function check({ root, options = {} }) {
  const dist = path.resolve(root, stringOption(options, 'dist', 'dist'));
  const html = path.resolve(dist, stringOption(options, 'html', 'index.html'));
  const { entryBudget, totalBudget } = budgetsOf(options);
  const build = stringOption(options, 'build', '');

  if (build) {
    const failure = runBuild(build, root);
    if (failure) return [finding(relPath(root, root) || '.', 1, 1, 'build-failed', failure)];
  }
  if (!existsSync(dist))
    throw new Error(`build directory does not exist: ${relPath(root, dist)} (build first)`);

  const bin = resolveBinIn('size-limit', [root, KIT_DIR]);
  if (!bin) throw new Error('tool-missing: size-limit (run npm install in the kit folder)');

  const generated = entryBudget !== undefined || totalBudget !== undefined;
  let tmp = null;
  try {
    let argv = ['--json'];
    let cwd = root;
    if (generated) {
      tmp = writeGeneratedConfig({ dist, html, entryBudget, totalBudget });
      argv = ['--config', tmp.file, '--json'];
      // size-limit loads its plugins from the package.json above its cwd: the
      // kit's own package.json declares @size-limit/file.
      cwd = KIT_DIR;
    }
    const proc = spawnSync(bin, argv, { cwd, encoding: 'utf8', maxBuffer: MAX_BUFFER });
    if (proc.error) throw proc.error;
    const { toolError, findings } = interpretSizeLimitReport(proc.stdout, relPath(root, dist));
    if (toolError) throw new Error(`size-limit: ${toolError || firstLine(proc.stderr)}`);
    return findings;
  } finally {
    if (tmp) rmSync(tmp.dir, { recursive: true, force: true });
  }
}

if (isMainModule(import.meta.url)) runScript(check);
