#!/usr/bin/env node
/**
 * `i18n-usage.mjs`: every `t()` key exists in the base catalogue, every
 * catalogue key is used, every member of a typed dynamic key has a catalogue
 * entry, and every `{{placeholder}}` of a key receives a value at each call.
 * Written for i18next-style `t(key, options)` calls.
 *
 * Static keys are read straight off `t("literal.key")` call sites. Dynamic
 * keys go through a real `ts.Program` + type checker (`lib/program.mjs`): the
 * interpolated expression's type is expanded to its finite string-literal
 * values when possible (e.g. `'HIGH' | 'MEDIUM' | 'LOW'`), and each
 * `prefix + literal` is required to exist as a key. When the type cannot be
 * reduced to a finite literal set (plain `string`, a runtime-only value), the
 * call is skipped for that check -- never invented -- but its whole prefix
 * still counts the matching catalogue keys as "used", so a legitimately
 * dynamic key is never flagged merely because this checker could not
 * enumerate it.
 *
 * A plural key is written once and resolved by i18next from the `count` it is
 * given: the catalogue holds `<key>_one`, `<key>_other` and the other CLDR
 * categories, and the call names `<key>`. A call whose key has no entry of its
 * own but has plural forms is therefore a use of those forms, not a missing key.
 *
 * The "every key is used" side additionally treats a key as used when its
 * exact text appears as a string-literal value anywhere in the source (e.g.
 * `labelKey: 'menu.home'` passed through a prop and only later handed to
 * `t(item.labelKey)`), so a key routed through one level of indirection is
 * not a false "unused" positive. A key never written anywhere as a literal
 * cannot be used by any code path either, so this cannot under-count.
 *
 * Every `{{name}}` placeholder of a key's catalogue value must receive a value
 * at each call, otherwise i18next renders the raw `{{name}}`. The names a call
 * passes are read from its options object literal, or from the checker's type
 * of the options when they are spread or passed by reference; options whose
 * names cannot be known (an index signature, `any`) are not judged.
 *
 * usage: node i18n-usage.mjs [--root <dir>] [--src <dir>] [--locales <dir>] [--base-locale <code>]
 */

import { existsSync } from 'node:fs';
import path from 'node:path';

import { finding } from './lib/report.mjs';
import { runScript, isMainModule, stringOption } from './lib/main.mjs';
import { relPath } from './lib/files.mjs';
import {
  DEFAULT_LOCALES_DIR,
  baseLocaleOf,
  localeDirs,
  localesRootOf,
  loadLocaleWithLocations,
  locateKey,
} from './lib/i18n.mjs';
import { position, ts } from './lib/ts.mjs';
import { loadProjectSourceFiles } from './lib/program.mjs';
import { collectTranslationCalls, resolvedDynamicKeys } from './lib/i18n-calls.mjs';

/** The CLDR plural categories i18next appends to a key (`<key>_one`, `<key>_other`, ...). */
const PLURAL_SUFFIXES = ['zero', 'one', 'two', 'few', 'many', 'other'];

/** The catalogue keys that are the plural forms of `key`, when it has any. */
function pluralForms(key, catalogueKeys) {
  return PLURAL_SUFFIXES.map((suffix) => `${key}_${suffix}`).filter((form) =>
    catalogueKeys.has(form),
  );
}

/** The interpolation names a catalogue value requires: `{{name}}`, `{{- name}}`, `{{name, fmt}}`. */
function placeholderNames(value) {
  const names = new Set();
  for (const match of String(value).matchAll(/\{\{-?\s*([A-Za-z_$][\w$]*)[^}]*\}\}/g)) {
    names.add(match[1]);
  }
  return names;
}

/** The value names a call's options pass, or null when they cannot be known statically. */
function passedNames(options, checker) {
  if (options === null) return new Set();
  if (
    ts.isObjectLiteralExpression(options) &&
    options.properties.every(
      (p) => ts.isPropertyAssignment(p) || ts.isShorthandPropertyAssignment(p),
    )
  ) {
    return new Set(
      options.properties.map((p) =>
        ts.isIdentifier(p.name) || ts.isStringLiteral(p.name) ? p.name.text : p.name.getText(),
      ),
    );
  }
  if (checker === null) return null;
  const type = checker.getTypeAtLocation(options);
  if (type.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown)) return null;
  if (checker.getIndexInfosOfType(type).length > 0) return null;
  return new Set(type.getProperties().map((symbol) => symbol.getName()));
}

/** The placeholders of `key` and of its plural forms, which a call naming `key` resolves to. */
function requiredNames(key, catalogue, catalogueKeys) {
  const names = new Set();
  for (const form of [key, ...pluralForms(key, catalogueKeys)]) {
    if (catalogueKeys.has(form))
      for (const name of placeholderNames(catalogue[form])) names.add(name);
  }
  return names;
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
  if (locales.length === 0) return [];
  const base = baseLocaleOf(locales, stringOption(options, 'base-locale', 'en'));
  const { flat: catalogue, keyLocations } = loadLocaleWithLocations(localesRoot, base);
  const catalogueKeys = new Set(Object.keys(catalogue));
  if (catalogueKeys.size === 0) return [];

  const { sourceFiles, checker = null } = loadProjectSourceFiles(root, srcRoot);
  if (sourceFiles.length === 0) return [];

  const { staticCalls, dynamicCalls, literalValues } = collectTranslationCalls(
    sourceFiles,
    checker,
  );
  const findings = [];
  const used = new Set();

  for (const call of staticCalls) {
    used.add(call.key);
    const forms = pluralForms(call.key, catalogueKeys);
    for (const form of forms) used.add(form);
    if (!catalogueKeys.has(call.key) && forms.length === 0) {
      const { line, col } = position(call.sourceFile, call.node);
      findings.push(
        finding(
          relPath(root, call.sourceFile.fileName),
          line,
          col,
          'i18n-usage-missing-key',
          `t("${call.key}") has no catalogue key in ${base}`,
        ),
      );
    }
  }

  const interpolated = [
    ...staticCalls,
    ...resolvedDynamicKeys(dynamicCalls).map((resolved) => ({ ...resolved })),
  ];
  for (const call of interpolated) {
    const required = requiredNames(call.key, catalogue, catalogueKeys);
    if (required.size === 0) continue;
    const passed = passedNames(call.options, checker);
    if (passed === null) continue;
    const missing = [...required].filter((name) => !passed.has(name));
    if (missing.length === 0) continue;
    const { line, col } = position(call.sourceFile, call.node);
    findings.push(
      finding(
        relPath(root, call.sourceFile.fileName),
        line,
        col,
        'i18n-usage-missing-interpolation',
        `t("${call.key}") passes no value for ${missing.map((name) => `{{${name}}}`).join(', ')}, so the reader sees the raw placeholder`,
        { fix: `pass ${missing.join(', ')} in the options of t("${call.key}")` },
      ),
    );
  }

  const usedPrefixes = dynamicCalls.map((c) => c.prefix);
  for (const resolved of resolvedDynamicKeys(dynamicCalls)) {
    used.add(resolved.key);
    const forms = pluralForms(resolved.key, catalogueKeys);
    for (const form of forms) used.add(form);
    if (!catalogueKeys.has(resolved.key) && forms.length === 0) {
      const { line, col } = position(resolved.sourceFile, resolved.node);
      findings.push(
        finding(
          relPath(root, resolved.sourceFile.fileName),
          line,
          col,
          'i18n-usage-dynamic-key-missing-member',
          `t(\`${resolved.shape.replace(' ', '${…}')}\`) has no catalogue key "${resolved.key}" for member "${resolved.member}"`,
          { fix: `add "${resolved.key}" to every locale catalogue` },
        ),
      );
    }
  }

  // Any string literal anywhere counts as "used" evidence for the
  // unused-key check (see module doc comment): catches a key that reaches
  // `t()` only through a variable, prop or config object.
  for (const text of literalValues) if (catalogueKeys.has(text)) used.add(text);

  for (const key of catalogueKeys) {
    if (used.has(key)) continue;
    if (usedPrefixes.some((p) => key.startsWith(p))) continue;
    const loc = keyLocations.get(key);
    const { line, col } = loc ? locateKey(loc.text, key) : { line: 1, col: 1 };
    findings.push(
      finding(
        loc ? relPath(root, loc.abs) : relPath(root, path.join(localesRoot, base)),
        line,
        col,
        'i18n-usage-unused-key',
        `key "${key}" is not referenced by any t() call`,
      ),
    );
  }
  return findings;
}

function skipIf({ root, options }) {
  const { localesRoot, srcRoot } = dirsOf(root, options);
  if (!existsSync(localesRoot) || !existsSync(srcRoot))
    return `no targets: ${relPath(root, srcRoot)} or ${relPath(root, localesRoot)} does not exist`;
  if (localeDirs(localesRoot).length === 0)
    return `no targets: no locale directories under ${relPath(root, localesRoot)}`;
  return null;
}

if (isMainModule(import.meta.url)) runScript(check, { skipIf });
