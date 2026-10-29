/**
 * Builds a real `ts.Program` (TypeScript compiler API, with a type checker)
 * over a target's `src/`, for the scripts that need type information rather
 * than only syntax: the dynamic translation-key check needs the
 * union-of-literals type of an interpolated expression, which no regex or
 * plain AST walk can answer.
 *
 * Uses the target's own `tsconfig.json` when present (so path mapping, `jsx`,
 * lib and package resolution all match what `tsc` itself would see); falls
 * back to a minimal, self-contained compiler-options set for a directory that
 * has no `tsconfig.json` of its own.
 */

import { existsSync } from 'node:fs';
import path from 'node:path';
import ts from 'typescript';

import { listFiles } from './files.mjs';
import { parseFile } from './ts.mjs';

const FALLBACK_OPTIONS = {
  target: ts.ScriptTarget.ES2022,
  module: ts.ModuleKind.ESNext,
  moduleResolution: ts.ModuleResolutionKind.Bundler,
  jsx: ts.JsxEmit.ReactJSX,
  strict: true,
  esModuleInterop: true,
  skipLibCheck: true,
  allowJs: false,
  noEmit: true,
};

/**
 * @param {string} root directory containing `src/` and, usually, `tsconfig.json`
 * @param {string} srcRoot absolute source directory
 * @returns {{ program: ts.Program, checker: ts.TypeChecker }}
 */
function createProgram(root, srcRoot) {
  const tsconfigPath = path.join(root, 'tsconfig.json');
  let rootNames, options;
  if (existsSync(tsconfigPath)) {
    const configFile = ts.readConfigFile(tsconfigPath, ts.sys.readFile);
    const parsed = ts.parseJsonConfigFileContent(configFile.config, ts.sys, root);
    rootNames = parsed.fileNames;
    options = parsed.options;
  } else {
    rootNames = listFiles(srcRoot, { extensions: ['.ts', '.tsx'] });
    options = { ...FALLBACK_OPTIONS };
  }
  const program = ts.createProgram({ rootNames, options });
  return { program, checker: program.getTypeChecker() };
}

function isProjectFile(fileName, srcRoot) {
  const rel = path.relative(srcRoot, fileName);
  return (
    !rel.startsWith('..') &&
    !rel.endsWith('.json') &&
    !rel.endsWith('.test.ts') &&
    !rel.endsWith('.test.tsx')
  );
}

/** Fresh typed source files for translation analysis, or syntax-only
 * files when program construction fails. Dynamic members then remain
 * unresolvable, while static-key checks still run.
 * @param {string} root
 * @param {string} srcRoot
 * @returns {{ sourceFiles: ts.SourceFile[], checker: ts.TypeChecker | null }}
 */
export function loadProjectSourceFiles(root, srcRoot) {
  try {
    const { program, checker } = createProgram(root, srcRoot);
    return {
      sourceFiles: program.getSourceFiles().filter((sf) => isProjectFile(sf.fileName, srcRoot)),
      checker,
    };
  } catch {
    const files = listFiles(srcRoot, { extensions: ['.ts', '.tsx'] }).filter((f) =>
      isProjectFile(f, srcRoot),
    );
    return { sourceFiles: files.map((f) => parseFile(f)), checker: null };
  }
}

/**
 * Recursively expands a type into its finite set of string-literal values,
 * or `null` if any branch is not a string literal (plain `string`, `number`,
 * a non-literal type): the caller then knows the union cannot be statically
 * enumerated and must skip the stricter check rather than invent an answer.
 * @param {ts.Type} type
 * @returns {string[] | null}
 */
export function stringLiteralUnionValues(type) {
  if (type.isUnion()) {
    const out = [];
    for (const sub of type.types) {
      const vals = stringLiteralUnionValues(sub);
      if (vals === null) return null;
      out.push(...vals);
    }
    return out;
  }
  if (type.isStringLiteral()) return [type.value];
  return null;
}

export { ts };
