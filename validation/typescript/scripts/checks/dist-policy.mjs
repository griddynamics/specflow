#!/usr/bin/env node
/**
 * `dist-policy.mjs`: development-only libraries are absent from the
 * production bundle.
 *
 * Router devtools and mock service workers are dev-only tools, normally gated
 * behind `import.meta.env.DEV`; a production build statically replaces that
 * with `false` and the bundler dead-code-eliminates the branch and its import.
 * This is a regression guard on that fact: it scans the already-built
 * JavaScript under the assets directory (`--assets`, default `dist/assets`)
 * for each library's own literal strings (unminifiable, unlike an identifier
 * name).
 *
 * Exits 2 (tool error) when the assets directory does not exist: this script
 * never builds, and a missing build is not a clean pass.
 *
 * The two built-in entries (router devtools, msw) are defaults. `--banned
 * <file>` supplies the adopter's own list, a JSON array of `{ rule, label,
 * markers: [..] }` (see `examples/dist-policy.example.json`); entries extend the
 * defaults (an entry with a built-in `rule` replaces it), and `--banned-replace`
 * drops the defaults so only the file's entries apply.
 *
 * usage: node dist-policy.mjs [--root <dir>] [--assets <dir>] [--banned <file>]
 *        [--banned-replace]
 */
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';

import { finding } from './lib/report.mjs';
import { runScript, isMainModule, stringOption } from './lib/main.mjs';
import { relPath } from './lib/files.mjs';
import { readJsonInput, requireArray, requireStringArray } from './lib/spec-input.mjs';

export const BANNED = [
  {
    rule: 'dist-policy-router-devtools',
    markers: ['router-devtools', 'TanStackRouterDevtools'],
    label: 'TanStack Router Devtools',
  },
  {
    rule: 'dist-policy-msw',
    markers: ['Mock Service Worker', 'mockServiceWorker.js'],
    label: 'msw',
  },
];

function jsFilesIn(dir) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return null;
  }
  const out = [];
  for (const entry of entries) {
    const abs = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(jsFilesIn(abs) ?? []));
    else if (entry.name.endsWith('.js')) out.push(abs);
  }
  return out;
}

/** `null` if `assetsDir` does not exist (not built yet); otherwise the
 * findings (empty when clean), each path labelled `<pathPrefix>/<file>`. */
export function checkAssetsDir(assetsDir, pathPrefix = 'dist/assets', banned = BANNED) {
  const files = jsFilesIn(assetsDir);
  if (files === null) return null;
  const findings = [];
  for (const abs of files) {
    const rel = path.relative(assetsDir, abs).split(path.sep).join('/');
    const text = readFileSync(abs, 'utf8');
    for (const { rule, markers, label } of banned) {
      if (markers.some((m) => text.includes(m))) {
        findings.push(
          finding(
            `${pathPrefix}/${rel}`,
            1,
            1,
            rule,
            `${label} leaked into the production bundle`,
            {
              fix: 'check the import.meta.env.DEV gate is still unconditional',
            },
          ),
        );
      }
    }
  }
  return findings;
}

/** Validates the `--banned` input and merges it with (or replaces) `BANNED`. */
export function resolveBanned(data, { replace = false, label = '--banned' } = {}) {
  const items = requireArray(data, label).map((item, index) => {
    const at = `${label}[${String(index)}]`;
    if (typeof item?.rule !== 'string' || !item.rule) throw new TypeError(`${at} needs a "rule"`);
    if (typeof item.label !== 'string' || !item.label) throw new TypeError(`${at} needs a "label"`);
    requireStringArray(item.markers, `${at} "markers"`);
    if (item.markers.length === 0) throw new TypeError(`${at} "markers" must not be empty`);
    return { rule: item.rule, label: item.label, markers: item.markers };
  });
  if (replace) return items;
  const custom = new Set(items.map((i) => i.rule));
  return [...BANNED.filter((b) => !custom.has(b.rule)), ...items];
}

export function check({ root, options = {} }) {
  const bannedFile = stringOption(options, 'banned', '');
  const replace = options['banned-replace'] === true || options['banned-replace'] === 'true';
  if (replace && !bannedFile) throw new Error('--banned-replace needs --banned <file>');
  const banned = bannedFile
    ? resolveBanned(readJsonInput(root, bannedFile, '--banned'), { replace })
    : BANNED;
  const assetsDir = path.resolve(root, stringOption(options, 'assets', 'dist/assets'));
  const findings = checkAssetsDir(assetsDir, relPath(root, assetsDir), banned);
  if (findings === null)
    throw new Error(`${relPath(root, assetsDir)} does not exist (build the bundle first)`);
  return findings;
}

if (isMainModule(import.meta.url)) runScript(check);
