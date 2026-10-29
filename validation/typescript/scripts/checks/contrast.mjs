#!/usr/bin/env node
/**
 * `contrast.mjs`: every foreground/background token pair of the theme
 * palette meets the WCAG 2.1 AA contrast ratio for normal text (4.5:1, or
 * `--min-ratio`).
 *
 * Pairs are derived structurally from the names the `:root` block of the theme
 * stylesheet (`--tokens-css`, default `src/styles/tokens.css`) declares: the
 * base pair (`--foreground` on `--background`), and every `--x-foreground`
 * paired with its base `--x` when `--x` is itself declared (`--primary` /
 * `--primary-foreground`, `--muted` / `--muted-foreground`, ...). Values must
 * be `oklch()`; any other value is skipped, never guessed.
 *
 * Other naming conventions: `--pairs <file>` is a JSON file
 * `{ "pairs": [["--surface", "--text"], ...] }` (background first, then
 * foreground; see `examples/contrast-pairs.example.json`) and replaces the
 * derived pairs. When no pair can be formed the check skips with `no targets`;
 * it never reports a clean pass for something it could not check. A pair that
 * names an undeclared token is a configuration error.
 *
 * usage: node contrast.mjs [--root <dir>] [--tokens-css <file>] [--min-ratio <n>]
 *        [--pairs <file>]
 */

import { existsSync } from 'node:fs';
import path from 'node:path';

import { finding } from './lib/report.mjs';
import { runScript, isMainModule, stringOption } from './lib/main.mjs';
import { relPath } from './lib/files.mjs';
import { readTokensCss } from './lib/tokens-css.mjs';
import { contrastRatio } from './lib/oklch.mjs';
import { readJsonInput, requireArray } from './lib/spec-input.mjs';

const DEFAULT_TOKENS_CSS = 'src/styles/tokens.css';
const AA_NORMAL_TEXT = 4.5;

/** Structural foreground/background pairs derivable from a token name list. */
export function derivePairs(tokenNames) {
  const names = new Set(tokenNames);
  const pairs = [];
  if (names.has('--background') && names.has('--foreground'))
    pairs.push(['--background', '--foreground']);
  for (const name of names) {
    if (name === '--foreground' || !name.endsWith('-foreground')) continue;
    const base = name.slice(0, -'-foreground'.length);
    if (names.has(base)) pairs.push([base, name]);
  }
  return pairs;
}

/** Validates the `--pairs` input: `{ "pairs": [[background, foreground], ...] }`
 * (a bare array of pairs is accepted too). */
export function parsePairs(data, label = '--pairs') {
  const items = requireArray(Array.isArray(data) ? data : data?.pairs, `${label} "pairs"`);
  return items.map((pair, index) => {
    if (!Array.isArray(pair) || pair.length !== 2 || !pair.every((n) => typeof n === 'string'))
      throw new TypeError(`${label} pairs[${String(index)}] must be [background, foreground]`);
    return pair;
  });
}

/** The pairs to evaluate for `palette`: from `--pairs` when given, else derived. */
function pairsOf(root, options, palette) {
  const file = stringOption(options, 'pairs', '');
  if (!file) return derivePairs(Object.keys(palette));
  const pairs = parsePairs(readJsonInput(root, file, '--pairs'), file);
  for (const name of pairs.flat())
    if (!(name in palette))
      throw new Error(`--pairs names ${name}, which the stylesheet does not declare`);
  return pairs;
}

function cssPathOf(root, options) {
  return path.resolve(root, stringOption(options, 'tokens-css', DEFAULT_TOKENS_CSS));
}

export function check({ root, options = {} }) {
  const cssPath = cssPathOf(root, options);
  if (!existsSync(cssPath)) return [];
  const minRatio = Number(stringOption(options, 'min-ratio', String(AA_NORMAL_TEXT)));
  if (!Number.isFinite(minRatio) || minRatio < 1)
    throw new RangeError(`--min-ratio must be a number >= 1, got ${options['min-ratio']}`);

  const palette = readTokensCss(cssPath);
  const findings = [];
  const relFile = relPath(root, cssPath);

  for (const [bg, fg] of pairsOf(root, options, palette)) {
    const ratio = contrastRatio(palette[bg].value, palette[fg].value);
    if (ratio === null) continue; // not an oklch() value this checker can evaluate
    if (ratio < minRatio) {
      findings.push(
        finding(
          relFile,
          palette[fg].line,
          1,
          'contrast-below-aa',
          `${fg} on ${bg} has a contrast ratio of ${ratio.toFixed(2)}:1, below the required ${minRatio}:1 for normal text`,
          { fix: `darken ${fg} or lighten ${bg} (or the reverse)` },
        ),
      );
    }
  }

  return findings;
}

function skipIf({ root, options }) {
  const cssPath = cssPathOf(root, options);
  if (!existsSync(cssPath)) return `no targets: ${relPath(root, cssPath)} does not exist`;
  if (pairsOf(root, options, readTokensCss(cssPath)).length === 0)
    return `no targets: no foreground/background token pairs in ${relPath(root, cssPath)}`;
  return null;
}

if (isMainModule(import.meta.url)) runScript(check, { skipIf });
