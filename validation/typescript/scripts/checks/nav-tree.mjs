#!/usr/bin/env node
/**
 * `nav-tree.mjs`: the application's navigation model in code equals the
 * declared navigation tree, entry by entry and level by level, and every nav
 * entry carries exactly the access restriction its route has.
 *
 * The declared tree is the adopter's own input (`--nav-spec`, a JSON file, see
 * `examples/nav-spec.example.json`):
 *
 *   {
 *     "variable": "NAV_ITEMS",                      // optional, the default
 *     "fields": { "key": "labelKey", "path": "to", "icon": "icon",
 *                 "exact": "exact", "children": "children",
 *                 "restricted": "adminOnly" },      // optional, these defaults
 *     "items": [
 *       { "label": "Orders", "path": "/orders", "icon": "ShoppingCart", "key": "nav.orders" },
 *       { "label": "Catalog", "group": true, "icon": "BookOpen", "children": [ ... ] }
 *     ],
 *     "restrictedRoutes": { "/admin/users": true, "/orders": false }   // optional
 *   }
 *
 * The code side is the first top-level `const <variable> = [ ... ]` under
 * `--src` whose elements are all object literals carrying the `key` field (TS
 * compiler API, not a regex). Each code entry is read for its path, icon
 * (string or identifier), `exact` flag, key, children and restriction flag.
 *
 * Rules:
 * - `nav-tree-missing`: no navigation array was found under the source root.
 * - `nav-tree-missing-entry`: a declared entry has no code entry at its position.
 * - `nav-tree-unlisted-entry`: a code entry has no declared entry at its position.
 * - `nav-tree-entry-mismatch`: path, `exact`, icon, key or group-ness differ
 *   (the key is compared only when the declared entry gives one).
 * - `nav-tree-role-restriction-missing` / `nav-tree-role-restriction-excess`:
 *   for every code entry whose path is listed in `restrictedRoutes`, the
 *   entry's effective restriction (its own flag, or an ancestor group's) must
 *   equal the listed value. Without `restrictedRoutes` this half is off.
 *
 * usage: node nav-tree.mjs [--root <dir>] [--src <dir>] [--nav-spec <file>]
 */

import { existsSync } from 'node:fs';
import path from 'node:path';

import { finding } from './lib/report.mjs';
import { runScript, isMainModule, stringOption } from './lib/main.mjs';
import { listFiles, relPath } from './lib/files.mjs';
import { parseFile, position, staticStringValue, ts } from './lib/ts.mjs';
import { readJsonInput, requireArray } from './lib/spec-input.mjs';

export const DEFAULT_NAV_SPEC = 'nav-spec.json';
export const DEFAULT_FIELDS = {
  key: 'labelKey',
  path: 'to',
  icon: 'icon',
  exact: 'exact',
  children: 'children',
  restricted: 'adminOnly',
};

function validateItems(items, where) {
  requireArray(items, where);
  for (const [index, item] of items.entries()) {
    const at = `${where}[${String(index)}]`;
    if (item === null || typeof item !== 'object' || Array.isArray(item))
      throw new TypeError(`${at} must be an object`);
    if (typeof item.label !== 'string' || item.label === '')
      throw new TypeError(`${at} needs a string "label"`);
    if (item.children !== undefined) validateItems(item.children, `${at}.children`);
  }
}

/** Validates a parsed navigation spec and returns it with defaults applied. */
export function parseNavSpec(data, label = '--nav-spec') {
  if (data === null || typeof data !== 'object' || Array.isArray(data))
    throw new TypeError(`${label} must be a JSON object with an "items" array`);
  validateItems(data.items, `${label} "items"`);
  const restricted = data.restrictedRoutes ?? {};
  if (restricted === null || typeof restricted !== 'object' || Array.isArray(restricted))
    throw new TypeError(`${label} "restrictedRoutes" must be an object of route path to boolean`);
  for (const [route, value] of Object.entries(restricted))
    if (typeof value !== 'boolean')
      throw new TypeError(`${label} "restrictedRoutes"["${route}"] must be true or false`);
  return {
    variable: typeof data.variable === 'string' ? data.variable : 'NAV_ITEMS',
    fields: { ...DEFAULT_FIELDS, ...(data.fields ?? {}) },
    items: data.items,
    restrictedRoutes:
      data.restrictedRoutes === undefined ? null : new Map(Object.entries(restricted)),
  };
}

function propValue(objectLiteral, name) {
  if (!ts.isObjectLiteralExpression(objectLiteral)) return null;
  for (const prop of objectLiteral.properties) {
    if (ts.isPropertyAssignment(prop) && ts.isIdentifier(prop.name) && prop.name.text === name)
      return prop.initializer;
  }
  return null;
}

function isTrue(objectLiteral, name) {
  return propValue(objectLiteral, name)?.kind === ts.SyntaxKind.TrueKeyword;
}

function iconOf(node) {
  if (!node) return null;
  if (ts.isIdentifier(node)) return node.text;
  return staticStringValue(node);
}

/** One code nav entry: `{ path, exact, icon, key, group, restricted, children, node }`. */
function readNavItem(objectLiteral, fields) {
  const childrenNode = propValue(objectLiteral, fields.children);
  const children = [];
  if (childrenNode && ts.isArrayLiteralExpression(childrenNode)) {
    for (const el of childrenNode.elements)
      if (ts.isObjectLiteralExpression(el)) children.push(readNavItem(el, fields));
  }
  const pathNode = propValue(objectLiteral, fields.path);
  const keyNode = propValue(objectLiteral, fields.key);
  return {
    path: pathNode ? staticStringValue(pathNode) : null,
    exact: isTrue(objectLiteral, fields.exact),
    icon: iconOf(propValue(objectLiteral, fields.icon)),
    key: keyNode ? staticStringValue(keyNode) : null,
    group: childrenNode !== null,
    restricted: isTrue(objectLiteral, fields.restricted),
    children,
    node: objectLiteral,
  };
}

/** The `const <variable> = [ {<key>: ...}, ... ]` array of a source file, or `null`. */
function findNavArray(sourceFile, { variable, fields }) {
  for (const stmt of sourceFile.statements) {
    if (!ts.isVariableStatement(stmt)) continue;
    for (const decl of stmt.declarationList.declarations) {
      if (!ts.isIdentifier(decl.name) || decl.name.text !== variable) continue;
      const init = decl.initializer;
      if (!init || !ts.isArrayLiteralExpression(init)) continue;
      const elements = init.elements;
      if (
        elements.length > 0 &&
        elements.every(
          (el) => ts.isObjectLiteralExpression(el) && propValue(el, fields.key) !== null,
        )
      )
        return init;
    }
  }
  return null;
}

function compareLevel(declared, code, ctx) {
  const { sourceFile, relFile, findings } = ctx;
  const len = Math.max(declared.length, code.length);
  for (let i = 0; i < len; i++) {
    const want = declared[i];
    const have = code[i];
    if (!want) {
      const { line, col } = position(sourceFile, have.node);
      findings.push(
        finding(
          relFile,
          line,
          col,
          'nav-tree-unlisted-entry',
          `nav entry at position ${i + 1} is not in the declared navigation tree`,
        ),
      );
      continue;
    }
    if (!have) {
      findings.push(
        finding(
          relFile,
          1,
          1,
          'nav-tree-missing-entry',
          `declared nav entry "${want.label}" (position ${i + 1}) is missing from the nav model`,
        ),
      );
      continue;
    }
    const mismatches = [];
    if ((want.path ?? null) !== have.path)
      mismatches.push(`path: declared "${want.path}", code "${have.path}"`);
    if (Boolean(want.exact) !== have.exact)
      mismatches.push(`exact: declared ${Boolean(want.exact)}, code ${have.exact}`);
    if ((want.icon ?? null) !== have.icon)
      mismatches.push(`icon: declared "${want.icon}", code "${have.icon}"`);
    if (want.key !== undefined && want.key !== null && want.key !== have.key)
      mismatches.push(`key: declared "${want.key}", code "${have.key}"`);
    if (Boolean(want.group) !== have.group)
      mismatches.push(`group: declared ${Boolean(want.group)}, code ${have.group}`);
    if (mismatches.length) {
      const { line, col } = position(sourceFile, have.node);
      findings.push(
        finding(
          relFile,
          line,
          col,
          'nav-tree-entry-mismatch',
          `nav entry "${want.label}" (position ${i + 1}) differs from the declared tree: ${mismatches.join('; ')}`,
        ),
      );
    }
    compareLevel(want.children ?? [], have.children, ctx);
  }
}

function compareRestrictions(entries, inherited, ctx) {
  const { sourceFile, relFile, findings, restrictedRoutes, fields } = ctx;
  for (const entry of entries) {
    const effective = inherited || entry.restricted;
    const expected = entry.path === null ? undefined : restrictedRoutes.get(entry.path);
    if (expected !== undefined && expected !== effective) {
      const { line, col } = position(sourceFile, entry.node);
      const name = entry.key ?? entry.path;
      const flag = `\`${fields.restricted}: true\``;
      findings.push(
        expected
          ? finding(
              relFile,
              line,
              col,
              'nav-tree-role-restriction-missing',
              `nav entry "${name}" links to "${entry.path}", a restricted route, but carries no restriction: it is shown to users who cannot open the screen`,
              { fix: `add ${flag} to this nav entry` },
            )
          : finding(
              relFile,
              line,
              col,
              'nav-tree-role-restriction-excess',
              `nav entry "${name}" links to "${entry.path}", an unrestricted route, but is restricted (${entry.restricted ? `its own ${flag}` : `an ancestor group's ${flag}`}): users entitled to the screen cannot reach it`,
              {
                fix: entry.restricted
                  ? `remove ${flag} from this nav entry`
                  : 'move this entry out of the restricted group, or restrict the group per child instead',
              },
            ),
      );
    }
    compareRestrictions(entry.children, effective, ctx);
  }
}

function inputsOf(root, options) {
  return {
    srcRoot: path.resolve(root, stringOption(options, 'src', 'src')),
    specFile: stringOption(options, 'nav-spec', DEFAULT_NAV_SPEC),
  };
}

export function check({ root, options = {} }) {
  const { srcRoot, specFile } = inputsOf(root, options);
  if (!existsSync(srcRoot)) return [];
  const spec = parseNavSpec(readJsonInput(root, specFile, '--nav-spec'), specFile);
  if (spec.items.length === 0) return [];

  for (const file of listFiles(srcRoot, { extensions: ['.ts', '.tsx'] })) {
    const sourceFile = parseFile(file);
    const array = findNavArray(sourceFile, spec);
    if (!array) continue;
    const code = array.elements
      .filter(ts.isObjectLiteralExpression)
      .map((el) => readNavItem(el, spec.fields));
    const ctx = {
      sourceFile,
      relFile: relPath(root, file),
      findings: [],
      restrictedRoutes: spec.restrictedRoutes,
      fields: spec.fields,
    };
    compareLevel(spec.items, code, ctx);
    if (spec.restrictedRoutes) compareRestrictions(code, false, ctx);
    return ctx.findings; // one navigation model per application: the first wins
  }

  return [
    finding(
      relPath(root, srcRoot),
      1,
      1,
      'nav-tree-missing',
      `no navigation array "${spec.variable}" (object literals carrying "${spec.fields.key}") was found`,
      {
        fix: `declare the nav model as const ${spec.variable} = [{ ${spec.fields.key}, ${spec.fields.path} | ${spec.fields.children}, ${spec.fields.icon} }]`,
      },
    ),
  ];
}

function skipIf({ root, options }) {
  const { srcRoot } = inputsOf(root, options);
  if (!existsSync(srcRoot)) return `no targets: ${relPath(root, srcRoot)} does not exist`;
  return null;
}

if (isMainModule(import.meta.url)) runScript(check, { skipIf });
