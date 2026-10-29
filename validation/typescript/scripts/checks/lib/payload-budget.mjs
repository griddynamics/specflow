/**
 * Helpers for `checks/bundle-size.mjs`: which built JavaScript files make up
 * the entry payload (the `<script src>` and `<link rel="modulepreload">`
 * assets the built `index.html` loads up front) and the total payload (every
 * `.js`/`.mjs` under the build directory), the size-limit configuration that
 * measures both against byte budgets, and the interpretation of
 * `size-limit --json` output. Node stdlib only.
 */

import { readdirSync } from 'node:fs';
import path from 'node:path';

const SCRIPT_SRC_RE = /<script\b[^>]*\bsrc\s*=\s*["']([^"']+)["']/gi;
const MODULEPRELOAD_RE =
  /<link\b(?=[^>]*\brel\s*=\s*["']modulepreload["'])[^>]*\bhref\s*=\s*["']([^"']+)["']/gi;

function isJavaScript(file) {
  return file.endsWith('.js') || file.endsWith('.mjs');
}

/** A byte budget option: a positive integer, else a configuration error. */
export function parseBudget(value, label) {
  const text = String(value ?? '').trim();
  if (!/^[1-9]\d*$/.test(text))
    throw new TypeError(
      `${label} must be a positive integer byte count, got ${JSON.stringify(value)}`,
    );
  return Number(text);
}

/**
 * Absolute paths of the JavaScript assets the built `index.html` loads up
 * front. Asset URLs must be absolute (`/assets/...`) and resolve inside `dist`.
 */
export function entryAssetPaths(indexHtml, dist) {
  const root = path.resolve(dist);
  const references = [
    ...indexHtml.matchAll(SCRIPT_SRC_RE),
    ...indexHtml.matchAll(MODULEPRELOAD_RE),
  ].map((match) => match[1]);
  const assets = new Set();
  for (const reference of references) {
    if (/^[a-z][a-z0-9+.-]*:/i.test(reference) || reference.startsWith('//')) continue;
    const pathname = reference.split(/[?#]/, 1)[0];
    if (!pathname.startsWith('/'))
      throw new Error(`index.html must reference an absolute local asset URL, got ${reference}`);
    const resolved = path.resolve(root, `.${pathname}`);
    if (!resolved.startsWith(`${root}${path.sep}`))
      throw new Error(`index.html asset escapes the build directory: ${reference}`);
    if (isJavaScript(resolved)) assets.add(resolved);
  }
  if (assets.size === 0) throw new Error('the built index.html loads no JavaScript entry asset');
  return [...assets].sort();
}

/** Absolute paths of every `.js`/`.mjs` file under `dist`, sorted. */
export function totalJavaScriptPaths(dist) {
  const out = [];
  const stack = [path.resolve(dist)];
  while (stack.length) {
    const dir = stack.pop();
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) stack.push(full);
      else if (entry.isFile() && isJavaScript(entry.name)) out.push(full);
    }
  }
  return out.sort();
}

/** The size-limit configuration for the entry and/or total budget. */
export function sizeLimitConfig({ entryPaths, totalPaths, entryBudget, totalBudget }) {
  const checks = [];
  if (entryBudget !== undefined)
    checks.push({ name: 'entry JavaScript', path: entryPaths, limit: `${entryBudget} B` });
  if (totalBudget !== undefined)
    checks.push({ name: 'total JavaScript', path: totalPaths, limit: `${totalBudget} B` });
  return checks;
}

/**
 * Interprets `size-limit --json` stdout. Returns `{ toolError }` for a
 * configuration or tool problem (size-limit's own `{"error": "..."}` shape, or
 * output that is not JSON) -- never a fabricated finding -- or `{ findings }`
 * (empty when every check passed).
 */
export function interpretSizeLimitReport(stdout, findingPath = '.') {
  let report;
  try {
    report = JSON.parse((stdout ?? '').trim());
  } catch {
    return { toolError: 'unparseable size-limit output' };
  }
  if (report && !Array.isArray(report) && typeof report.error === 'string')
    return { toolError: report.error.split('\n')[0] };
  if (!Array.isArray(report)) return { toolError: 'unparseable size-limit output' };
  const findings = report
    .filter((check) => check.passed === false)
    .map((check) => ({
      path: findingPath,
      line: 1,
      col: 1,
      rule: 'size-limit-exceeded',
      message: `${check.name}: ${check.size} B exceeds the ${check.sizeLimit} B budget`,
      fix: 'reduce what is shipped (split, lazy-load or drop code); a budget is not raised to make a build pass',
    }));
  return { findings };
}
