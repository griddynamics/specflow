#!/usr/bin/env node
/**
 * Builds the design-system semgrep rule files (`ds-*.yml`) from the templates in `templates/` and
 * a facts file that names the project's primitives: where they live, what they are called, which
 * tokens and classes they own. The rules say "a shared visual concept has one implementation";
 * the facts say which module and which names that is.
 *
 * These rules are opt-in: `run.mjs` loads none of them unless the project passes
 * `--design-system <facts.json>` (built into a temporary folder) or keeps the built files in its
 * own `semgrep/` folder.
 *
 * Usage: node scripts/build-ds-rules.mjs [facts.json] [outDir]
 *   facts.json  default: design-system.example.json next to this kit
 *   outDir      default: semgrep-ds (next to this kit; the example build the self-test checks)
 *
 * Template placeholders (a placeholder that does not resolve is an error):
 *   @@key@@          a string fact (dotted keys reach into objects: @@tokens.overlay@@)
 *   @@alt:key@@      a list of identifiers joined into a regular expression alternation (a|b|c)
 *   @@list:key@@     alone on a line: one YAML list item per entry of a list of strings at the
 *                    line's indentation; an empty list removes the line
 *
 * Facts file: see design-system.example.json. Globs are semgrep `paths` globs.
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const KIT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

export const RULE_FILES = [
  'ds-components',
  'ds-forms',
  'ds-navigation',
  'ds-overlays',
  'ds-typography',
  'ds-styling',
];

const IDENTIFIER = /^[\w$-]+$/u;

function fact(facts, key) {
  let value = facts;
  for (const part of key.split('.')) value = value?.[part];
  if (value === undefined) throw new Error(`design-system facts: "${key}" is missing`);
  return value;
}

function textFact(facts, key) {
  const value = fact(facts, key);
  if (typeof value !== 'string' || value === '' || value.includes("'")) {
    throw new Error(`design-system facts: "${key}" must be a non-empty string without a quote`);
  }
  return value;
}

function listFact(facts, key) {
  const value = fact(facts, key);
  const valid =
    Array.isArray(value) &&
    value.every((item) => typeof item === 'string' && item !== '' && !item.includes("'"));
  if (!valid) throw new Error(`design-system facts: "${key}" must be a list of strings`);
  return value;
}

function nameListFact(facts, key) {
  const value = listFact(facts, key);
  if (value.length === 0 || !value.every((item) => IDENTIFIER.test(item))) {
    throw new Error(`design-system facts: "${key}" must be a non-empty list of identifiers`);
  }
  return value;
}

/** A template with every placeholder replaced by its fact. */
export function renderTemplate(template, facts) {
  const lines = template.split('\n').flatMap((line) => {
    const item = /^(\s*)@@list:([\w.]+)@@$/u.exec(line);
    if (item !== null) {
      return listFact(facts, item[2]).map((entry) => `${item[1]}- '${entry}'`);
    }
    return [
      line.replaceAll(/@@(alt:)?([\w.]+)@@/gu, (_match, alternation, key) =>
        alternation === undefined
          ? textFact(facts, key)
          : nameListFact(facts, key)
              .map((name) => name.replaceAll('$', String.raw`\$`))
              .join('|'),
      ),
    ];
  });
  const output = lines.join('\n');
  const unresolved = /@@[^@\n]*@@/u.exec(output);
  if (unresolved !== null) throw new Error(`unresolved placeholder ${unresolved[0]}`);
  return output;
}

/** `{ 'ds-components': '<yaml>', ... }`: every rule file, rendered. */
export function renderDesignSystemRules(facts, readTemplate) {
  return Object.fromEntries(
    RULE_FILES.map((name) => [name, renderTemplate(readTemplate(name), facts)]),
  );
}

/**
 * Renders every rule file from the facts file and writes them into `outDir` (created when
 * missing). Returns the written paths.
 *
 * @param {string} factsFile
 * @param {string} outDir
 */
export function buildDesignSystemRules(factsFile, outDir) {
  const facts = JSON.parse(readFileSync(factsFile, 'utf8'));
  const rendered = renderDesignSystemRules(facts, (name) =>
    readFileSync(path.join(KIT, 'templates', `${name}.yml.tmpl`), 'utf8'),
  );
  mkdirSync(outDir, { recursive: true });
  return Object.entries(rendered).map(([name, text]) => {
    const file = path.join(outDir, `${name}.yml`);
    writeFileSync(file, text);
    return file;
  });
}

function main(argv) {
  const source = path.resolve(argv[0] ?? path.join(KIT, 'design-system.example.json'));
  const target = path.resolve(argv[1] ?? path.join(KIT, 'semgrep-ds'));
  for (const file of buildDesignSystemRules(source, target)) {
    process.stdout.write(`wrote ${file}\n`);
  }
}

if (
  process.argv[1] !== undefined &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  main(process.argv.slice(2));
}
