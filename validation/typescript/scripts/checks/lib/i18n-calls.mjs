/**
 * Walks every `t(...)` call site in a set of source files once, shared by
 * `i18n-usage` and `i18n-protected`. The same walk collects literal evidence
 * for indirect catalogue-key usage. A dynamic call's interpolated expression
 * is resolved to a finite string-literal union when `checker` can (a real
 * `ts.Program`'s type checker, `./program.mjs`); when it cannot, the call is
 * still reported with `members: null` so a caller knows it exists (e.g. to
 * count its prefix as "used") without inventing member values.
 */

import { calleeName, staticStringValue, walk, ts } from './ts.mjs';
import { stringLiteralUnionValues } from './program.mjs';

const PLACEHOLDER = ' ';

function staticPrefixOf(templateNode) {
  if (ts.isNoSubstitutionTemplateLiteral(templateNode)) return templateNode.text;
  return templateNode.head.text;
}

function shapeOf(templateNode) {
  if (ts.isNoSubstitutionTemplateLiteral(templateNode)) return templateNode.text;
  let out = templateNode.head.text;
  for (const span of templateNode.templateSpans) out += PLACEHOLDER + span.literal.text;
  return out;
}

/**
 * @param {import('typescript').SourceFile[]} sourceFiles
 * @param {import('typescript').TypeChecker | null} checker
 * @returns {{
 *   staticCalls: {key: string, sourceFile: any, node: any, options: any}[],
 *   dynamicCalls: {prefix: string, shape: string, members: string[] | null, sourceFile: any, node: any, options: any}[],
 *   literalValues: Set<string>,
 * }}
 */
export function collectTranslationCalls(sourceFiles, checker) {
  const staticCalls = [];
  const dynamicCalls = [];
  const literalValues = new Set();
  for (const sourceFile of sourceFiles) {
    for (const node of walk(sourceFile)) {
      const value = staticStringValue(node);
      if (value !== null) literalValues.add(value);
      if (!ts.isCallExpression(node) || calleeName(node) !== 't' || node.arguments.length === 0)
        continue;
      const arg0 = node.arguments[0];

      const literal = staticStringValue(arg0);
      if (literal !== null) {
        staticCalls.push({
          key: literal,
          sourceFile,
          node: arg0,
          options: node.arguments[1] ?? null,
        });
        continue;
      }
      if (!ts.isTemplateExpression(arg0)) continue;

      const prefix = staticPrefixOf(arg0);
      const shape = shapeOf(arg0);
      let members = null;
      if (arg0.templateSpans.length === 1 && checker) {
        const type = checker.getTypeAtLocation(arg0.templateSpans[0].expression);
        members = stringLiteralUnionValues(type);
      }
      dynamicCalls.push({
        prefix,
        shape,
        members,
        sourceFile,
        node: arg0,
        options: node.arguments[1] ?? null,
      });
    }
  }
  return { staticCalls, dynamicCalls, literalValues };
}

/** Every `prefix + member` full key a resolved dynamic call can produce,
 * flattened across all dynamic calls, as `{key, member, sourceFile, node, shape, options}[]`. */
export function resolvedDynamicKeys(dynamicCalls) {
  const out = [];
  for (const call of dynamicCalls) {
    if (call.members === null) continue;
    for (const member of call.members) {
      out.push({
        key: call.shape.replace(PLACEHOLDER, member),
        member,
        sourceFile: call.sourceFile,
        node: call.node,
        shape: call.shape,
        options: call.options,
      });
    }
  }
  return out;
}
