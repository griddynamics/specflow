#!/usr/bin/env node
/**
 * `css-usage.mjs`: every class selector of the target's stylesheets has usage
 * evidence in the production sources -- the entry document and the
 * `.ts`/`.tsx` component graph -- excluding unit tests and their support
 * modules. A class referenced only by a test does not establish production
 * usage.
 *
 * Tool first: PurgeCSS (`rejected: true`) computes the unused selectors
 * without rewriting styles; `postcss-selector-parser` keeps only selectors
 * that carry a class. Type and pseudo selectors implement global
 * HTML/accessibility behaviour and are never reported.
 *
 * Rule: `unused-css-class`.
 *
 * usage: node css-usage.mjs [--root <dir>] [--src <dir>] [--html <file>] [--css <glob>]
 */
import { existsSync, globSync } from 'node:fs';
import path from 'node:path';

import { PurgeCSS } from 'purgecss';
import selectorParser from 'postcss-selector-parser';

import { finding } from './lib/report.mjs';
import { runScript, isMainModule, stringOption } from './lib/main.mjs';
import { relPath } from './lib/files.mjs';

/** The PurgeCSS options for a target root: absolute globs, so the result does
 * not depend on the process working directory. */
export function purgeOptions(root, options = {}) {
  const src = path.resolve(root, stringOption(options, 'src', 'src'));
  const glob = (p) => p.split(path.sep).join('/');
  return {
    content: [
      glob(path.resolve(root, stringOption(options, 'html', 'index.html'))),
      glob(path.join(src, '**/*.{ts,tsx}')),
    ],
    skippedContentGlobs: [glob(path.join(src, '**/*.{test,spec}.{ts,tsx}'))],
    css: [glob(path.resolve(root, stringOption(options, 'css', 'src/**/*.css')))],
    rejected: true,
  };
}

/** Findings for every rejected selector that carries a class. */
export function unusedClasses(results, root) {
  return results.flatMap((result) =>
    (result.rejected ?? [])
      .filter((selector) => {
        let hasClass = false;
        selectorParser((selectors) =>
          selectors.walkClasses(() => {
            hasClass = true;
          }),
        ).processSync(selector);
        return hasClass;
      })
      .map((selector) =>
        finding(
          result.file ? relPath(root, result.file) : 'css',
          1,
          1,
          'unused-css-class',
          `unused class selector ${selector.trim()}`,
        ),
      ),
  );
}

export async function check({ root, options = {} }) {
  return unusedClasses(await new PurgeCSS().purge(purgeOptions(root, options)), root);
}

/** Skip reason when there is nothing to check: no `--src` directory or no
 * stylesheet matching `--css`; otherwise null. */
export function skipIf({ root, options = {} }) {
  const src = path.resolve(root, stringOption(options, 'src', 'src'));
  if (!existsSync(src)) return `no targets: ${relPath(root, src) || '.'} does not exist`;
  const css = stringOption(options, 'css', 'src/**/*.css');
  const matches = globSync(css, { cwd: root });
  return matches.length ? null : `no targets: no stylesheet matches ${css}`;
}

if (isMainModule(import.meta.url)) runScript(check, { skipIf });
