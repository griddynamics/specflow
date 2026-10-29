/**
 * Shared locale-catalogue loading for the i18n scripts (`i18n-catalogues`,
 * `i18n-usage`, `i18n-protected`): one reader, not three.
 *
 * Layout: `<locales>/<locale>/**\/*.json`, where `<locales>` defaults to
 * `src/locales` under the target root and is overridable with `--locales`.
 */

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

export const DEFAULT_LOCALES_DIR = 'src/locales';

/** `{a: {b: "x"}}` -> `{"a.b": "x"}`; flat (dot-joined) catalogues pass
 * through unchanged, and a nested file is supported too. */
export function flatten(obj, prefix = '') {
  const out = {};
  for (const [key, value] of Object.entries(obj)) {
    const full = prefix ? `${prefix}.${key}` : key;
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      Object.assign(out, flatten(value, full));
    } else {
      out[full] = value;
    }
  }
  return out;
}

/** 1-based `{line, col}` of `"key"`'s first appearance as a JSON object key. */
export function locateKey(text, key) {
  const needle = `"${key.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
  const lines = text.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const col = lines[i].indexOf(needle);
    if (col !== -1) return { line: i + 1, col: col + 1 };
  }
  return { line: 1, col: 1 };
}

/** Absolute locales directory of a target root. */
export function localesRootOf(root, localesDir = DEFAULT_LOCALES_DIR) {
  return path.resolve(root, localesDir);
}

export function localeDirs(localesRoot) {
  if (!existsSync(localesRoot)) return [];
  return readdirSync(localesRoot)
    .filter((name) => statSync(path.join(localesRoot, name)).isDirectory())
    .sort();
}

/** The base locale: `preferred` (default `en`) when present, else the
 * alphabetically first locale directory. */
export function baseLocaleOf(locales, preferred = 'en') {
  return locales.includes(preferred) ? preferred : locales[0];
}

/** Catalogue files relative to a locale dir, e.g. `["common.json"]`. */
export function catalogueFiles(localeDir) {
  const out = [];
  const stack = [''];
  while (stack.length) {
    const rel = stack.pop();
    const abs = path.join(localeDir, rel);
    for (const name of readdirSync(abs)) {
      const relChild = rel ? path.join(rel, name) : name;
      const absChild = path.join(abs, name);
      if (statSync(absChild).isDirectory()) stack.push(relChild);
      else if (name.endsWith('.json')) out.push(relChild);
    }
  }
  return out.sort();
}

/**
 * Loads and flattens every catalogue file of one locale into a single
 * `{key: value}` map, plus `keyLocations`: `key -> {abs, text}` of the
 * catalogue file each flattened key came from, so a consumer can report a
 * precise `path:line:col` for any key. Files that fail to parse are skipped
 * (`i18n-catalogues` is the one that reports invalid JSON).
 */
export function loadLocaleWithLocations(localesRoot, locale) {
  const localeDir = path.join(localesRoot, locale);
  const flat = {};
  const keyLocations = new Map();
  for (const rel of catalogueFiles(localeDir)) {
    const abs = path.join(localeDir, rel);
    const text = readFileSync(abs, 'utf8');
    let parsed;
    try {
      parsed = flatten(JSON.parse(text));
    } catch {
      continue;
    }
    for (const [key, value] of Object.entries(parsed)) {
      flat[key] = value;
      keyLocations.set(key, { abs, text });
    }
  }
  return { flat, keyLocations };
}
