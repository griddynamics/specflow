#!/usr/bin/env node
/**
 * `tokens.mjs`: every `var(--x)` reference under the source directory names a
 * custom property that the theme stylesheet's `:root` block declares.
 *
 * A reference to an undeclared name is not a syntax error and not a type
 * error -- CSS resolves it to nothing and the declaration silently renders
 * transparent. The declared set is the `:root` block of the theme stylesheet
 * (`--tokens-css`, default `src/styles/tokens.css`), not "every `--name:`
 * anywhere", which would let a component mint a private theme token beside
 * the palette. A reference with a fallback (`var(--columns, 1)`) is not the
 * defect this looks for: the declaration renders the fallback, visibly and on
 * purpose. A `var(--x)` inside a CSS comment renders nothing either and is not
 * a reference.
 *
 * Rule: `tokens-undeclared-reference`, reported at the referencing file and
 * line, because that is the line that renders nothing.
 *
 * Optional parity half, enabled by `--tokens-spec <file>`: the adopter's own
 * token list, a JSON file `{ "tokens": [ { "name": "--background", "value":
 * "oklch(0.97 0 0)" }, { "name": "--accent" } ] }` (see
 * `examples/tokens.spec.example.json`). Every listed token must be declared in
 * `:root`, and a listed `value` must equal the declared one (whitespace
 * collapsed).
 * - `tokens-missing`: a listed token is not declared in `:root`.
 * - `tokens-value-mismatch`: the declared value differs from the listed one.
 *
 * usage: node tokens.mjs [--root <dir>] [--src <dir>] [--tokens-css <file>]
 *        [--tokens-spec <file>]
 */

import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

import { finding } from './lib/report.mjs';
import { runScript, isMainModule, stringOption } from './lib/main.mjs';
import { listFiles, relPath } from './lib/files.mjs';
import { readJsonInput, requireArray } from './lib/spec-input.mjs';
import { readTokensCss } from './lib/tokens-css.mjs';

export const DEFAULT_TOKENS_CSS = 'src/styles/tokens.css';
// The capture group keeps the separator so a fallback (`,`) can be told apart.
const VAR_REFERENCE_RE = /var\(\s*(--[a-zA-Z0-9-]+)\s*([,)]?)/g;
const REFERENCE_EXTENSIONS = ['.ts', '.tsx', '.css'];

/**
 * `cssText` with every `/* ... *\/` comment blanked to spaces (newlines kept),
 * so offsets, lines and columns stay those of the original text. A comment
 * renders nothing, so a `var(--x)` inside one (a placeholder in prose such as
 * "values come from var(--token)") is not a reference. Quoted strings are
 * skipped whole: a `/*` inside `url("a/*b")` does not open a comment.
 */
export function blankCssComments(cssText) {
  const out = cssText.split('');
  let i = 0;
  while (i < cssText.length) {
    const ch = cssText[i];
    if (ch === '"' || ch === "'") {
      i++;
      while (i < cssText.length && cssText[i] !== ch && cssText[i] !== '\n') {
        i += cssText[i] === '\\' ? 2 : 1;
      }
      i++;
    } else if (ch === '/' && cssText[i + 1] === '*') {
      const close = cssText.indexOf('*/', i + 2);
      const end = close === -1 ? cssText.length : close + 2;
      for (let j = i; j < end; j++) if (out[j] !== '\n') out[j] = ' ';
      i = end;
    } else {
      i++;
    }
  }
  return out.join('');
}

/** 1-based line and column of `index` in `text`. */
function lineColOf(text, index) {
  const before = text.slice(0, index);
  const line = before.split('\n').length;
  const lastNewline = before.lastIndexOf('\n');
  return { line, col: index - lastNewline };
}

function pathsOf(root, options) {
  return {
    cssPath: path.resolve(root, stringOption(options, 'tokens-css', DEFAULT_TOKENS_CSS)),
    srcRoot: path.resolve(root, stringOption(options, 'src', 'src')),
  };
}

function normalizeCss(value) {
  return value.replace(/\s+/g, ' ').trim();
}

/** Validates the parsed token list and returns `[{ name, value? }]`. */
export function parseTokenSpec(data, label = '--tokens-spec') {
  const tokens = requireArray(data?.tokens, `${label} "tokens"`);
  for (const [index, token] of tokens.entries()) {
    const where = `${label} tokens[${String(index)}]`;
    if (typeof token?.name !== 'string' || !token.name.startsWith('--'))
      throw new TypeError(`${where} needs a "name" starting with "--"`);
    if (token.value !== undefined && typeof token.value !== 'string')
      throw new TypeError(`${where} "value" must be a string`);
  }
  return tokens;
}

/** One finding per listed token that is undeclared or has a different value. */
function specFindings(root, cssPath, declared, specFile) {
  const relFile = relPath(root, cssPath);
  const findings = [];
  for (const token of parseTokenSpec(readJsonInput(root, specFile, '--tokens-spec'), specFile)) {
    const entry = declared[token.name];
    const differs =
      entry !== undefined &&
      token.value !== undefined &&
      normalizeCss(entry.value) !== normalizeCss(token.value);
    if (!entry) {
      const message = `listed token "${token.name}" is not declared in :root`;
      findings.push(finding(relFile, 1, 1, 'tokens-missing', message));
    } else if (differs) {
      findings.push(
        finding(
          relFile,
          entry.line,
          1,
          'tokens-value-mismatch',
          `token "${token.name}" is "${entry.value}" but the token list gives "${token.value}"`,
          { fix: `set :root { ${token.name}: ${token.value}; }` },
        ),
      );
    }
  }
  return findings;
}

export function check({ root, options = {} }) {
  const { cssPath, srcRoot } = pathsOf(root, options);
  if (!existsSync(cssPath)) return [];
  const declaredEntries = readTokensCss(cssPath);
  const declared = new Set(Object.keys(declaredEntries));
  const specFile = stringOption(options, 'tokens-spec', '');
  const findings = specFile ? specFindings(root, cssPath, declaredEntries, specFile) : [];
  for (const file of listFiles(srcRoot, { extensions: REFERENCE_EXTENSIONS })) {
    const raw = readFileSync(file, 'utf8');
    const text = file.endsWith('.css') ? blankCssComments(raw) : raw;
    for (const match of text.matchAll(VAR_REFERENCE_RE)) {
      const name = match[1];
      if (declared.has(name) || match[2] === ',') continue;
      const { line, col } = lineColOf(text, match.index);
      findings.push(
        finding(
          relPath(root, file),
          line,
          col,
          'tokens-undeclared-reference',
          `var(${name}) references a custom property that :root does not declare; it resolves to nothing`,
          { fix: `use a declared token, or declare ${name} in :root of ${relPath(root, cssPath)}` },
        ),
      );
    }
  }
  return findings;
}

function skipIf({ root, options }) {
  const { cssPath } = pathsOf(root, options);
  if (!existsSync(cssPath)) return `no targets: ${relPath(root, cssPath)} does not exist`;
  return null;
}

if (isMainModule(import.meta.url)) runScript(check, { skipIf });
