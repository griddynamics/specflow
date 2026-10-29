#!/usr/bin/env node
/**
 * `api-types-only.mjs`: the directory `openapi-typescript` generates into
 * contains only what `openapi-typescript` itself produces.
 *
 * `openapi-typescript openapi.json -o <dir>/api.d.ts` has exactly five
 * possible top-level exports (`paths`, `webhooks`, `components`,
 * `operations`, `$defs`), however large the schema. This check is the
 * regression guard on that: it fails if the generated directory
 * (`--generated-dir`, default `src/generated`) ever contains anything else --
 * an extra file, or an export name outside that fixed set (a hand-added or
 * mis-generated type sharing the generated directory).
 *
 * Tool first: exported top-level declaration names are read with the
 * TypeScript compiler API, the same parser `tsc` itself uses.
 *
 * Exits 2 (tool error) when the generated directory does not exist: generate
 * first; a missing output is not a clean pass.
 *
 * usage: node api-types-only.mjs [--root <dir>] [--generated-dir <dir>] [--expected-file <name>]
 */
import { readdirSync } from 'node:fs';
import path from 'node:path';

import ts from 'typescript';

import { finding } from './lib/report.mjs';
import { runScript, isMainModule, stringOption } from './lib/main.mjs';
import { relPath } from './lib/files.mjs';

// openapi-typescript's own top-level export names.
export const ALLOWED_EXPORTS = new Set(['paths', 'webhooks', 'components', 'operations', '$defs']);

/** Every top-level exported declaration in a `.ts`/`.d.ts` source text:
 * `{ name, line, col }` (1-based, at the declaration's own name). Handles
 * `export interface X`, `export type X`, `export class X`, `export const X`
 * and a separate `export { X }` list. */
export function topLevelExports(sourceText, fileName = 'generated.d.ts') {
  const sourceFile = ts.createSourceFile(fileName, sourceText, ts.ScriptTarget.Latest, true);
  const exportsFound = [];
  const push = (nameNode) => {
    const { line, character } = sourceFile.getLineAndCharacterOfPosition(
      nameNode.getStart(sourceFile),
    );
    exportsFound.push({ name: nameNode.text, line: line + 1, col: character + 1 });
  };
  for (const stmt of sourceFile.statements) {
    const hasExportModifier = (ts.canHaveModifiers(stmt) ? ts.getModifiers(stmt) : undefined)?.some(
      (m) => m.kind === ts.SyntaxKind.ExportKeyword,
    );
    if (ts.isExportDeclaration(stmt) && stmt.exportClause && ts.isNamedExports(stmt.exportClause)) {
      for (const el of stmt.exportClause.elements) push(el.name);
      continue;
    }
    if (!hasExportModifier) continue;
    if (
      ts.isInterfaceDeclaration(stmt) ||
      ts.isTypeAliasDeclaration(stmt) ||
      ts.isClassDeclaration(stmt)
    ) {
      if (stmt.name) push(stmt.name);
    } else if (ts.isVariableStatement(stmt)) {
      for (const decl of stmt.declarationList.declarations) {
        if (ts.isIdentifier(decl.name)) push(decl.name);
      }
    }
  }
  return exportsFound;
}

/** Names only, for callers that do not need a location. */
export function topLevelExportNames(sourceText, fileName = 'generated.d.ts') {
  return topLevelExports(sourceText, fileName).map((e) => e.name);
}

function tsFilesIn(dir) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return null;
  }
  const out = [];
  for (const entry of entries) {
    const abs = path.join(dir, entry.name);
    // `.d.ts` already ends with `.ts`, so one suffix check covers both.
    if (entry.isDirectory()) out.push(...(tsFilesIn(abs) ?? []));
    else if (entry.name.endsWith('.ts')) out.push(abs);
  }
  return out;
}

/** `null` if `generatedDir` does not exist. Otherwise the findings (empty
 * when clean): one `api-types-only-extra-file` per file other than
 * `expectedFileName`, and one `api-types-only-unexpected-export` per exported
 * name outside `ALLOWED_EXPORTS`; each path labelled `<pathPrefix>/<file>`. */
export function checkGeneratedDir(generatedDir, expectedFileName = 'api.d.ts', pathPrefix = '.') {
  const files = tsFilesIn(generatedDir);
  if (files === null) return null;
  const label = (rel) => (pathPrefix === '.' ? rel : `${pathPrefix}/${rel}`);
  const findings = [];
  for (const abs of files) {
    const rel = path.relative(generatedDir, abs).split(path.sep).join('/');
    if (rel !== expectedFileName) {
      findings.push(
        finding(
          label(rel),
          1,
          1,
          'api-types-only-extra-file',
          `unexpected file in the generated directory (only ${expectedFileName} comes from openapi-typescript)`,
          { fix: 'remove the file, or generate it from the OpenAPI document only' },
        ),
      );
      continue;
    }
    const text = ts.sys.readFile(abs) ?? '';
    for (const { name, line, col } of topLevelExports(text, rel)) {
      if (ALLOWED_EXPORTS.has(name)) continue;
      findings.push(
        finding(
          label(rel),
          line,
          col,
          'api-types-only-unexpected-export',
          `exports "${name}", which is not one of openapi-typescript's own top-level exports (${[...ALLOWED_EXPORTS].join(', ')})`,
          {
            fix: 'never hand-edit or hand-append to a generated file; hand-written types belong outside the generated directory',
          },
        ),
      );
    }
  }
  return findings;
}

export function check({ root, options = {} }) {
  const generatedDir = path.resolve(root, stringOption(options, 'generated-dir', 'src/generated'));
  const expected = stringOption(options, 'expected-file', 'api.d.ts');
  const findings = checkGeneratedDir(generatedDir, expected, relPath(root, generatedDir));
  if (findings === null)
    throw new Error(`${relPath(root, generatedDir)} does not exist (run the generator first)`);
  return findings;
}

if (isMainModule(import.meta.url)) runScript(check);
