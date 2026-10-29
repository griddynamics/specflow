/**
 * Token clone detection over the native jscpd engine (`@jscpd/core` +
 * `@jscpd/tokenizer`), with a Cypher grammar registered beside the built-in ones.
 *
 * `implementationSource` blanks (keeping line numbers) what is not
 * implementation before tokenising: import declarations, independent assertion
 * oracles in test files (`expect(...)`/`assert(...)` statements whose arguments
 * only observe, and `waitFor` callbacks made only of such assertions), the
 * configuration boilerplate of a native typed ESLint flat config. Only
 * JS/TS files are blanked; other formats (Python, SQL, ...) are compared in
 * full. Rule implementations, diagnostic parsers, fixtures and actions
 * evaluated inside an assertion stay compared.
 */
import ts from 'typescript';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { Detector, MemoryStore, getModeByName } = require('@jscpd/core');
const { Tokenizer, FORMATS } = require('@jscpd/tokenizer');
const Prism = require('prismjs');
require('prismjs/components/prism-cypher.js');
const reprism = require('reprism').default;
FORMATS.cypher = { exts: ['cypher'] };
reprism.languages.cypher = {
  ...Prism.languages.cypher,
  identifier: /\b\w+\b/u,
  new_line: /\n/u,
  empty: /\s+/u,
};

export function implementationSource(file, source) {
  const test = /\.(?:test|spec)\.[cm]?[jt]sx?$/u.test(file);
  if (!/\.[cm]?[jt]sx?$/u.test(file)) return source;
  const tree = ts.createSourceFile(
    file,
    source,
    ts.ScriptTarget.Latest,
    true,
    file.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.JS,
  );
  const spans = [];
  function nativeEslintBase(node) {
    if (!file.endsWith('/eslint.config.mjs') || !ts.isObjectLiteralExpression(node)) return false;
    const text = node.getText(tree);
    return (
      node.properties.some(
        (property) =>
          property.name?.getText(tree) === 'extends' &&
          ts.isPropertyAssignment(property) &&
          property.initializer.getText(tree).replace(/\s/gu, '') ===
            '[...tseslint.configs.strictTypeChecked,...tseslint.configs.stylisticTypeChecked]',
      ) ||
      (text.includes('noUnsanitized.configs.recommended') && text.includes('regexpPlugin'))
    );
  }
  function nativeRule(property) {
    if (ts.isSpreadAssignment(property))
      return /^(?:js|sonarjs|noUnsanitized|regexpPlugin)\.configs\./u.test(
        property.expression.getText(tree),
      );
    if (!ts.isPropertyAssignment(property)) return false;
    const name = property.name.text;
    if (name?.startsWith('no-restricted-')) return false;
    if (name?.includes('/') && !/^(?:@typescript-eslint|sonarjs|regexp|unicorn)\//u.test(name))
      return false;
    return configurationValue(property.initializer);
  }
  function configurationValue(node) {
    if (ts.isArrayLiteralExpression(node)) return node.elements.every(configurationValue);
    if (ts.isObjectLiteralExpression(node))
      return node.properties.every(
        (property) => ts.isPropertyAssignment(property) && configurationValue(property.initializer),
      );
    return (
      ts.isStringLiteral(node) ||
      ts.isNumericLiteral(node) ||
      [ts.SyntaxKind.TrueKeyword, ts.SyntaxKind.FalseKeyword, ts.SyntaxKind.NullKeyword].includes(
        node.kind,
      ) ||
      (ts.isIdentifier(node) && node.text === 'Infinity')
    );
  }
  function assertionExpression(node) {
    if (ts.isAwaitExpression(node)) return assertionExpression(node.expression);
    if (!ts.isCallExpression(node)) return false;
    let receiver = node.expression;
    while (ts.isPropertyAccessExpression(receiver) || ts.isCallExpression(receiver))
      receiver = receiver.expression;
    return (
      ts.isIdentifier(receiver) &&
      ['expect', 'assert'].includes(receiver.text) &&
      observationalArguments(node)
    );
  }
  function observationalArguments(node) {
    if (ts.isAwaitExpression(node) || ts.isNewExpression(node)) return false;
    if (ts.isCallExpression(node)) {
      let receiver = node.expression;
      while (ts.isPropertyAccessExpression(receiver) || ts.isCallExpression(receiver))
        receiver = receiver.expression;
      const name = ts.isIdentifier(node.expression)
        ? node.expression.text
        : ts.isPropertyAccessExpression(node.expression)
          ? node.expression.name.text
          : '';
      if (
        !(ts.isIdentifier(receiver) && ['expect', 'assert'].includes(receiver.text)) &&
        !/^(?:(?:get|find|query)(?:All)?By[A-Z]|get$|json$|rest$|keys$|values$|entries$|mocked$)/u.test(
          name,
        )
      )
        return false;
    }
    let observation = true;
    ts.forEachChild(node, (child) => {
      if (!observationalArguments(child)) observation = false;
    });
    return observation;
  }
  function executableConfiguration(node) {
    if (ts.isFunctionExpression(node) || ts.isArrowFunction(node) || ts.isMethodDeclaration(node))
      return true;
    let executable = false;
    ts.forEachChild(node, (child) => {
      if (executableConfiguration(child)) executable = true;
    });
    return executable;
  }
  function assertionOnlyWait(node) {
    if (!ts.isExpressionStatement(node)) return false;
    const expression = ts.isAwaitExpression(node.expression)
      ? node.expression.expression
      : node.expression;
    if (
      !ts.isCallExpression(expression) ||
      !ts.isIdentifier(expression.expression) ||
      expression.expression.text !== 'waitFor'
    )
      return false;
    const callback = expression.arguments[0];
    if (!callback || !(ts.isArrowFunction(callback) || ts.isFunctionExpression(callback)))
      return false;
    return ts.isBlock(callback.body)
      ? callback.body.statements.length > 0 &&
          callback.body.statements.every(
            (statement) =>
              ts.isExpressionStatement(statement) && assertionExpression(statement.expression),
          )
      : assertionExpression(callback.body);
  }
  function walk(node) {
    if (nativeEslintBase(node)) {
      for (const property of node.properties) {
        if (
          property.name?.getText(tree) === 'rules' &&
          ts.isObjectLiteralExpression(property.initializer)
        ) {
          for (const rule of property.initializer.properties)
            if (nativeRule(rule)) spans.push([rule.getStart(tree), rule.end]);
        } else if (
          property.name?.getText(tree) === 'plugins' &&
          ts.isObjectLiteralExpression(property.initializer)
        ) {
          for (const plugin of property.initializer.properties)
            if (
              ['sonarjs', "'no-unsanitized'", 'regexp', 'unicorn'].includes(
                plugin.name?.getText(tree),
              )
            )
              spans.push([plugin.getStart(tree), plugin.end]);
        } else if (
          ['basePath', 'files', 'extends', 'languageOptions'].includes(
            property.name?.getText(tree),
          ) &&
          !executableConfiguration(property)
        ) {
          spans.push([property.getStart(tree), property.end]);
        }
      }
      return;
    }
    if (
      ts.isImportDeclaration(node) ||
      (test &&
        ts.isExpressionStatement(node) &&
        (assertionExpression(node.expression) || assertionOnlyWait(node)))
    ) {
      spans.push([node.getStart(tree), node.end]);
      return;
    }
    ts.forEachChild(node, walk);
  }
  walk(tree);
  for (const [start, end] of spans.sort((a, b) => b[0] - a[0]))
    source =
      source.slice(0, start) + source.slice(start, end).replace(/[^\n]/gu, ' ') + source.slice(end);
  return source;
}

export function createDetector(config = { minLines: 10, minTokens: 100, mode: 'weak' }) {
  return new Detector(new Tokenizer(), new MemoryStore(), [], {
    minLines: config.minLines,
    minTokens: config.minTokens,
    mode: getModeByName(config.mode),
    skipLocal: false,
  });
}
