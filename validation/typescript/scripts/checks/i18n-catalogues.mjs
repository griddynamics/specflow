#!/usr/bin/env node
/**
 * `i18n-catalogues.mjs`: every locale's catalogue files carry the same key
 * set and the same `{{placeholders}}` as the base locale; values are flat,
 * non-empty strings; a field placeholder hint ends with the single ellipsis
 * character.
 *
 * Reads every `<locales>/<locale>/**\/*.json` catalogue file (`--locales`,
 * default `src/locales`). Locale directories are discovered from the
 * filesystem, so a third locale is compared the same way; the base is
 * `--base-locale` (default `en`) when present, otherwise the alphabetically
 * first locale.
 *
 * Rules:
 * - `i18n-catalogue-invalid-json`, `i18n-catalogue-non-string-value`,
 *   `i18n-catalogue-empty-value`;
 * - `i18n-catalogue-missing-file`, `i18n-catalogue-missing-key`,
 *   `i18n-catalogue-extra-key`, `i18n-catalogue-placeholder-mismatch`;
 * - `i18n-catalogue-placeholder-ellipsis`: a key whose last segment ends in
 *   `placeholder` (an input's hint text) ends with U+2026, never "..."
 *   (on by default; `--placeholder-ellipsis false` disables it).
 * - `i18n-catalogue-missing-required-string` (only with `--required-strings
 *   <file>`): a string the adopter's specification fixes verbatim (an empty
 *   state, a legal notice) is the value of no base-locale key. The file is a
 *   JSON array of strings, or `{ "strings": [ { "value": "...", "label":
 *   "..." } ] }` (see `examples/required-strings.example.json`).
 *
 * The ellipsis rule is a typography convention and is on by default;
 * `--placeholder-ellipsis false` turns it off without losing key and
 * placeholder parity.
 *
 * usage: node i18n-catalogues.mjs [--root <dir>] [--locales <dir>] [--base-locale <code>]
 *        [--required-strings <file>] [--placeholder-ellipsis <true|false>]
 */

import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

import { finding } from './lib/report.mjs';
import { runScript, isMainModule, stringOption } from './lib/main.mjs';
import { relPath } from './lib/files.mjs';
import {
  DEFAULT_LOCALES_DIR,
  baseLocaleOf,
  catalogueFiles,
  flatten,
  localeDirs,
  localesRootOf,
  locateKey,
} from './lib/i18n.mjs';
import { readJsonInput, requireArray } from './lib/spec-input.mjs';

// A field's placeholder key: the last key segment ends in `placeholder`. A key
// with `placeholder` in a middle segment (`chat.placeholder.mode`) is not one.
const FIELD_PLACEHOLDER_KEY_RE = /(^|[._])placeholder$/;
const PLACEHOLDER_RE = /\{\{\s*([\w.]+)\s*\}\}/g;

function placeholders(value) {
  if (typeof value !== 'string') return new Set();
  return new Set([...value.matchAll(PLACEHOLDER_RE)].map((m) => m[1]));
}

function localesOf(root, options) {
  return localesRootOf(root, stringOption(options, 'locales', DEFAULT_LOCALES_DIR));
}

export function check({ root, options = {} }) {
  const localesRoot = localesOf(root, options);
  if (!existsSync(localesRoot)) return [];
  const locales = localeDirs(localesRoot);
  if (locales.length < 2) return [];

  const base = baseLocaleOf(locales, stringOption(options, 'base-locale', 'en'));
  const others = locales.filter((l) => l !== base);

  // relFile -> locale -> { text, flat|null, parseError?, abs }
  const filesByRel = new Map();
  for (const locale of locales) {
    for (const rel of catalogueFiles(path.join(localesRoot, locale))) {
      const abs = path.join(localesRoot, locale, rel);
      const text = readFileSync(abs, 'utf8');
      if (!filesByRel.has(rel)) filesByRel.set(rel, new Map());
      try {
        filesByRel.get(rel).set(locale, { text, flat: flatten(JSON.parse(text)), abs });
      } catch (err) {
        filesByRel.get(rel).set(locale, { text, flat: null, parseError: err.message, abs });
      }
    }
  }

  const ellipsisRule = stringOption(options, 'placeholder-ellipsis', 'true');
  if (!['true', 'false'].includes(ellipsisRule))
    throw new RangeError(`--placeholder-ellipsis must be true or false, got ${ellipsisRule}`);
  const checkEllipsis = ellipsisRule === 'true';

  const findings = [];
  for (const [rel, byLocale] of [...filesByRel.entries()].sort()) {
    for (const [locale, entry] of byLocale) {
      if (entry.parseError) {
        findings.push(
          finding(
            relPath(root, entry.abs),
            1,
            1,
            'i18n-catalogue-invalid-json',
            `${rel} (${locale}) is not valid JSON: ${entry.parseError}`,
          ),
        );
      }
    }
    const baseEntry = byLocale.get(base);
    if (!baseEntry || baseEntry.flat === null) continue;

    // Value shape: every value in every locale must be a non-empty string.
    for (const [locale, entry] of byLocale) {
      if (entry.flat === null) continue;
      for (const [key, value] of Object.entries(entry.flat)) {
        const { line, col } = locateKey(entry.text, key);
        if (typeof value !== 'string') {
          findings.push(
            finding(
              relPath(root, entry.abs),
              line,
              col,
              'i18n-catalogue-non-string-value',
              `key "${key}" (${locale}/${rel}) is not a flat string value`,
            ),
          );
        } else if (checkEllipsis && FIELD_PLACEHOLDER_KEY_RE.test(key) && !value.endsWith('…')) {
          findings.push(
            finding(
              relPath(root, entry.abs),
              line,
              col,
              'i18n-catalogue-placeholder-ellipsis',
              `placeholder "${key}" (${locale}/${rel}) must end with the single character "…", not "${value.slice(-3)}"`,
              {
                fix: 'end the hint with "…" (U+2026), after an example such as "e.g. ID01…" if it names a format',
              },
            ),
          );
        } else if (value.trim() === '') {
          findings.push(
            finding(
              relPath(root, entry.abs),
              line,
              col,
              'i18n-catalogue-empty-value',
              `key "${key}" (${locale}/${rel}) has an empty value`,
            ),
          );
        }
      }
    }

    const baseKeys = new Set(Object.keys(baseEntry.flat));
    for (const locale of others) {
      const entry = byLocale.get(locale);
      if (!entry || entry.flat === null) {
        findings.push(
          finding(
            relPath(root, path.join(localesRoot, locale, rel)),
            1,
            1,
            'i18n-catalogue-missing-file',
            `${rel} has no ${locale} catalogue`,
            { fix: `add ${relPath(root, path.join(localesRoot, locale, rel))}` },
          ),
        );
        continue;
      }
      const otherKeys = new Set(Object.keys(entry.flat));
      for (const key of baseKeys) {
        if (!otherKeys.has(key)) {
          const { line, col } = locateKey(baseEntry.text, key);
          findings.push(
            finding(
              relPath(root, baseEntry.abs),
              line,
              col,
              'i18n-catalogue-missing-key',
              `key "${key}" is missing from ${locale}/${rel}`,
            ),
          );
        } else {
          const baseP = placeholders(baseEntry.flat[key]);
          const otherP = placeholders(entry.flat[key]);
          const missing = [...baseP].filter((p) => !otherP.has(p));
          const extra = [...otherP].filter((p) => !baseP.has(p));
          if (missing.length || extra.length) {
            const { line, col } = locateKey(entry.text, key);
            const parts = [];
            if (missing.length) parts.push(`missing {{${missing.join('}}, {{')}}}`);
            if (extra.length) parts.push(`unexpected {{${extra.join('}}, {{')}}}`);
            findings.push(
              finding(
                relPath(root, entry.abs),
                line,
                col,
                'i18n-catalogue-placeholder-mismatch',
                `key "${key}" (${locale}/${rel}) placeholders differ from ${base}: ${parts.join('; ')}`,
              ),
            );
          }
        }
      }
      for (const key of otherKeys) {
        if (!baseKeys.has(key)) {
          const { line, col } = locateKey(entry.text, key);
          findings.push(
            finding(
              relPath(root, entry.abs),
              line,
              col,
              'i18n-catalogue-extra-key',
              `key "${key}" (${locale}/${rel}) does not exist in ${base}`,
            ),
          );
        }
      }
    }
  }

  const requiredFile = stringOption(options, 'required-strings', '');
  if (requiredFile) {
    const required = parseRequiredStrings(
      readJsonInput(root, requiredFile, '--required-strings'),
      requiredFile,
    );
    const baseValues = new Set();
    for (const byLocale of filesByRel.values()) {
      const entry = byLocale.get(base);
      if (!entry || entry.flat === null) continue;
      for (const value of Object.values(entry.flat))
        if (typeof value === 'string') baseValues.add(value.trim());
    }
    for (const { value, label } of required) {
      if (baseValues.has(value.trim())) continue;
      const what = label ? ` (${label})` : '';
      findings.push(
        finding(
          relPath(root, path.join(localesRoot, base)),
          1,
          1,
          'i18n-catalogue-missing-required-string',
          `no ${base} catalogue value carries the required string${what}: "${value}"`,
          {
            fix: `add the string verbatim to the ${base} catalogue, and its translation to every other locale`,
          },
        ),
      );
    }
  }

  return findings;
}

/** Validates the required-strings input: an array of strings, or of
 * `{ "value": "...", "label": "..." }` objects (the label names the screen or
 * state the string belongs to and only appears in messages). */
export function parseRequiredStrings(data, label = '--required-strings') {
  const items = requireArray(Array.isArray(data) ? data : data?.strings, `${label} "strings"`);
  return items.map((item, index) => {
    if (typeof item === 'string' && item.trim()) return { value: item, label: '' };
    if (typeof item?.value === 'string' && item.value.trim())
      return { value: item.value, label: typeof item.label === 'string' ? item.label : '' };
    throw new TypeError(
      `${label} strings[${String(index)}] must be a non-empty string or { value }`,
    );
  });
}

function skipIf({ root, options }) {
  const localesRoot = localesOf(root, options);
  if (!existsSync(localesRoot)) return `no targets: ${relPath(root, localesRoot)} does not exist`;
  if (localeDirs(localesRoot).length < 2)
    return `no targets: fewer than two locale directories under ${relPath(root, localesRoot)}`;
  return null;
}

if (isMainModule(import.meta.url)) runScript(check, { skipIf });
