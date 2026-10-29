#!/usr/bin/env node
/**
 * `i18n-protected.mjs`: a code token whose base-locale label *is* the code
 * stays verbatim in every other locale.
 *
 * "Protected" keys are found precisely, not by guessing which words look
 * code-like: a key is protected exactly when it is a resolved
 * `t(\`prefix.${v}\`)` dynamic call site (`lib/i18n-calls.mjs`, the same
 * typed union resolution `i18n-usage.mjs` uses) *and* its base catalogue
 * value already equals that literal member verbatim -- the base label already
 * is the code (`status.NEW` -> "NEW"), as opposed to a dynamic key whose base
 * value is a human label of the member (`status.band_HIGH` -> "High",
 * correctly translated to "Alta"). Scoping to the exact key's own resolved
 * code, rather than any code value anywhere, avoids false positives on
 * correctly translated labels that happen to share a word with some enum.
 *
 * `--localised-code-families` (comma-separated key prefixes, default none)
 * names the families whose keys keep the code as suffix but whose values are
 * localised labels in every locale; their keys are never protected.
 *
 * `--namespaces` (comma-separated, default none) names the key namespaces the
 * catalogues must provide: each needs at least one `<namespace>.*` key in the
 * base locale (`i18n-protected-namespace-missing`).
 *
 * usage: node i18n-protected.mjs [--root <dir>] [--src <dir>] [--locales <dir>]
 *        [--base-locale <code>] [--localised-code-families <prefix,...>]
 *        [--namespaces <ns,...>]
 */

import { existsSync } from 'node:fs';
import path from 'node:path';

import { finding } from './lib/report.mjs';
import { runScript, isMainModule, listOption, stringOption } from './lib/main.mjs';
import { relPath } from './lib/files.mjs';
import {
  DEFAULT_LOCALES_DIR,
  baseLocaleOf,
  localeDirs,
  localesRootOf,
  loadLocaleWithLocations,
  locateKey,
} from './lib/i18n.mjs';
import { loadProjectSourceFiles } from './lib/program.mjs';
import { collectTranslationCalls, resolvedDynamicKeys } from './lib/i18n-calls.mjs';

/** `--namespaces`: every listed key namespace has at least one base-locale key. */
function namespaceFindings(root, localesRoot, base, baseCatalogue, options) {
  const keys = Object.keys(baseCatalogue);
  return listOption(options, 'namespaces')
    .filter((ns) => !keys.some((key) => key.startsWith(`${ns}.`)))
    .map((ns) =>
      finding(
        relPath(root, path.join(localesRoot, base)),
        1,
        1,
        'i18n-protected-namespace-missing',
        `key namespace "${ns}" has no ${base} catalogue key`,
        { fix: `add at least one "${ns}.*" key, or drop "${ns}" from --namespaces` },
      ),
    );
}

function dirsOf(root, options) {
  return {
    localesRoot: localesRootOf(root, stringOption(options, 'locales', DEFAULT_LOCALES_DIR)),
    srcRoot: path.resolve(root, stringOption(options, 'src', 'src')),
  };
}

export function check({ root, options = {} }) {
  const { localesRoot, srcRoot } = dirsOf(root, options);
  if (!existsSync(localesRoot) || !existsSync(srcRoot)) return [];
  const locales = localeDirs(localesRoot);
  if (locales.length < 2) return [];
  const base = baseLocaleOf(locales, stringOption(options, 'base-locale', 'en'));
  const others = locales.filter((l) => l !== base);
  const { flat: baseCatalogue } = loadLocaleWithLocations(localesRoot, base);
  if (Object.keys(baseCatalogue).length === 0) return [];
  const localisedFamilies = listOption(options, 'localised-code-families');
  const isLocalisedFamily = (key) => localisedFamilies.some((prefix) => key.startsWith(prefix));

  const findings = namespaceFindings(root, localesRoot, base, baseCatalogue, options);
  const { sourceFiles, checker } = loadProjectSourceFiles(root, srcRoot);
  if (sourceFiles.length === 0) return findings;

  // For every dynamic call whose base value already equals the resolved
  // literal member, every other locale's value for that key must equal the
  // base value exactly.
  const { dynamicCalls } = collectTranslationCalls(sourceFiles, checker);
  const protectedKeys = new Map(); // key -> code
  for (const resolved of resolvedDynamicKeys(dynamicCalls)) {
    if (baseCatalogue[resolved.key] === resolved.member && !isLocalisedFamily(resolved.key)) {
      protectedKeys.set(resolved.key, resolved.member);
    }
  }

  for (const locale of others) {
    const { flat: otherCatalogue, keyLocations } = loadLocaleWithLocations(localesRoot, locale);
    for (const [key, code] of protectedKeys) {
      const value = otherCatalogue[key];
      if (value === undefined || value === code) continue;
      const loc = keyLocations.get(key);
      const { line, col } = loc ? locateKey(loc.text, key) : { line: 1, col: 1 };
      findings.push(
        finding(
          loc ? relPath(root, loc.abs) : relPath(root, path.join(localesRoot, locale)),
          line,
          col,
          'i18n-protected-code-not-verbatim',
          `key "${key}" (${locale}) is "${value}" but the code "${code}" must stay verbatim, as it does in ${base}`,
          { fix: `set "${key}" to "${code}" in the ${locale} catalogue` },
        ),
      );
    }
  }
  return findings;
}

function skipIf({ root, options }) {
  const { localesRoot, srcRoot } = dirsOf(root, options);
  if (!existsSync(localesRoot) || !existsSync(srcRoot))
    return `no targets: ${relPath(root, srcRoot)} or ${relPath(root, localesRoot)} does not exist`;
  if (localeDirs(localesRoot).length < 2)
    return `no targets: fewer than two locale directories under ${relPath(root, localesRoot)}`;
  return null;
}

if (isMainModule(import.meta.url)) runScript(check, { skipIf });
