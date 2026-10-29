/**
 * Small TypeScript-compiler-API helpers shared by the scripts that read
 * `.ts`/`.tsx` source. Every script uses the real parser
 * (`ts.createSourceFile`) instead of a regex over source text.
 */

import { readFileSync } from 'node:fs';
import ts from 'typescript';

/** Parses one file into a `ts.SourceFile` (syntax only, no type checker). */
export function parseFile(filePath) {
  const text = readFileSync(filePath, 'utf8');
  const kind = filePath.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  return ts.createSourceFile(filePath, text, ts.ScriptTarget.Latest, true, kind);
}

/** Depth-first walk of every descendant node (including `root` itself). */
export function* walk(root) {
  yield root;
  for (const child of root.getChildren()) yield* walk(child);
}

/** 1-based `{line, col}` of a node's start, the finding format. */
export function position(sourceFile, node) {
  const { line, character } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
  return { line: line + 1, col: character + 1 };
}

/** String value of a string-literal-like node, or `null`. Handles plain
 * string literals and no-substitution template literals (`` `/x/y` ``). */
export function staticStringValue(node) {
  if (ts.isStringLiteralLike(node) && !ts.isTemplateExpression(node)) return node.text;
  return null;
}

/**
 * A template literal's static "shape": every interpolation replaced with
 * `placeholder` (default `{}`), e.g. `` `/orders/${id}/lines` `` ->
 * `/orders/{}/lines`. Returns `null` for a plain (non-template) node.
 */
function templateShape(node, placeholder = '{}') {
  if (ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  if (!ts.isTemplateExpression(node)) return null;
  let out = node.head.text;
  for (const span of node.templateSpans) {
    out += placeholder + span.literal.text;
  }
  return out;
}

/** Either a static string or a template literal's static shape; `null` for
 * anything else (an identifier, a computed expression, ...). */
export function pathLikeValue(node) {
  const s = staticStringValue(node);
  if (s !== null) return s;
  return templateShape(node);
}

/** Text of the callee name of a call expression (`foo(...)` -> `"foo"`,
 * `a.b(...)` -> `"b"`), or `null` for anything more complex. */
export function calleeName(callExpr) {
  const expr = callExpr.expression;
  if (ts.isIdentifier(expr)) return expr.text;
  if (ts.isPropertyAccessExpression(expr) && ts.isIdentifier(expr.name)) return expr.name.text;
  return null;
}

export { ts };
