/**
 * Render sinks: does an expression reach the screen as text?
 *
 * Shared by `local/no-unformatted-number` and `local/no-display-list-join`. It decides from
 * program structure, types and data flow, never from the name of a variable or a component:
 *
 * - a JSX expression child;
 * - a JSX attribute that is a text prop: on a component, one whose declared type (through the
 *   checker) admits a string or a `ReactNode`; on an HTML element, one of the intrinsic attributes
 *   the browser shows or announces (`aria-label`, `title`, `alt`, `placeholder`);
 * - an option value of a `t()` call;
 * - what a render callback returns: a function whose contextual type returns a `ReactNode`, that
 *   is a type admitting both text and an element (`cell: (row) => row.codes.join(', ')` passed to a
 *   prop typed to return `ReactNode`);
 * - a template literal, a `+` concatenation, a logical or conditional expression, `String(x)` and
 *   `x.toString()` that flow into one of those, and a variable that is assigned once (`const`, or
 *   a `let` with a single assignment), the value a `useMemo` factory returns and what a function
 *   declared in the same file returns.
 *
 * `renderedValues` walks that flow backwards from a sink to the expressions whose value is shown.
 *
 * Limit: a component prop typed `string | number` (a size, a width, a value) is a quantity for the
 * component to use, not text, so a bare number passed to it is not flagged; the same number turned
 * into text by a template or `String()` is. The rule does not follow the prop into the component
 * to see whether it renders it.
 */
import ts from 'typescript';

/** HTML attributes whose value the browser shows or announces. */
const INTRINSIC_TEXT_ATTRIBUTES = new Set(['aria-label', 'title', 'alt', 'placeholder']);

/**
 * Attributes that carry structure, not text, whatever their declared type: React's own, the
 * attributes that name or locate an element, and the `data-*` / `aria-*` families but for the
 * accessible-name attributes that are text. A reviewed list of attributes, not of components.
 */
const STRUCTURAL_ATTRIBUTES = new Set([
  'key',
  'ref',
  'id',
  'idPrefix',
  'className',
  'name',
  'htmlFor',
  'href',
  'to',
  'type',
  'role',
  'testId',
  'style',
]);
const TEXT_ARIA_ATTRIBUTES = new Set(['aria-label', 'aria-description', 'aria-valuetext']);

/** The default pattern of the module whose exports are the client's formatters. */
export const DEFAULT_FORMAT_MODULE = '(?:^|/)utils/format\\.ts$';

/** The format-module facts a rule takes from its options (all optional). */
export function formatModuleFacts(options) {
  const given = options ?? {};
  return {
    pattern: new RegExp(given.formatModule ?? DEFAULT_FORMAT_MODULE, 'u'),
    moduleName: given.moduleName ?? 'the format module',
    formatters: {
      number: given.formatters?.number ?? 'formatNumber',
      list: given.formatters?.list ?? 'formatList',
      inline: given.formatters?.inline ?? 'formatInline',
    },
  };
}

/** The `schema` entry of the rules that read the format-module facts. */
export const FORMAT_MODULE_SCHEMA = {
  type: 'object',
  properties: {
    formatModule: { type: 'string' },
    moduleName: { type: 'string' },
    formatters: {
      type: 'object',
      properties: {
        number: { type: 'string' },
        list: { type: 'string' },
        inline: { type: 'string' },
      },
      additionalProperties: false,
    },
  },
  additionalProperties: false,
};

/** Number methods that turn a number into text without the reader's locale. */
const NUMBER_STRINGIFIERS = new Set(['toString', 'toFixed', 'toLocaleString', 'toPrecision']);

function unionParts(type) {
  return type.isUnion() ? type.types : [type];
}

/** Whether a (possibly union) type admits a string. */
function admitsString(type) {
  return unionParts(type).some((part) => (part.flags & ts.TypeFlags.StringLike) !== 0);
}

const PRIMITIVE_FLAGS =
  ts.TypeFlags.StringLike |
  ts.TypeFlags.NumberLike |
  ts.TypeFlags.BooleanLike |
  ts.TypeFlags.Null |
  ts.TypeFlags.Undefined |
  ts.TypeFlags.Void;

/**
 * Whether a type is a number type: `number`, `number | undefined`, `string | number`. A type that
 * also admits an object, such as `ReactNode`, is a renderable, not a number.
 */
export function admitsNumber(type) {
  const parts = unionParts(type);
  return (
    parts.some((part) => (part.flags & ts.TypeFlags.NumberLike) !== 0) &&
    parts.every((part) => (part.flags & PRIMITIVE_FLAGS) !== 0)
  );
}

/**
 * Whether a bare number reaching this sink is displayed. A child and a `t()` option are; a
 * component prop is when its type admits an object as well as a string (`ReactNode`), whereas a
 * `string | number` prop (a size, a width, a value) is a quantity for the component to use. A
 * number turned into text by a template or `String()` is displayed whatever the prop.
 */
export function rendersBareNumber(services, node) {
  if (node.type !== 'JSXExpressionContainer' || node.parent.type !== 'JSXAttribute') return true;
  const checker = services.program.getTypeChecker();
  const contextual = checker.getContextualType(services.esTreeNodeToTSNodeMap.get(node.expression));
  return (
    contextual !== undefined &&
    unionParts(contextual).some((p) => (p.flags & PRIMITIVE_FLAGS) === 0)
  );
}

function typeOf(services, node) {
  const checker = services.program.getTypeChecker();
  return checker.getTypeAtLocation(services.esTreeNodeToTSNodeMap.get(node));
}

/** Whether a node's declaration lives in the format module (`formatModule`, a RegExp). */
export function isDeclaredInFormatModule(services, node, formatModule) {
  const checker = services.program.getTypeChecker();
  let symbol = checker.getSymbolAtLocation(services.esTreeNodeToTSNodeMap.get(node));
  if (symbol === undefined) return false;
  if ((symbol.flags & ts.SymbolFlags.Alias) !== 0) symbol = checker.getAliasedSymbol(symbol);
  return (symbol.declarations ?? []).some((declaration) =>
    formatModule.test(declaration.getSourceFile().fileName.replaceAll('\\', '/')),
  );
}

/** `formatNumber(…)` and the like, or the `format` of an `Intl.NumberFormat`. */
function isFormatterCall(services, call, formatModule) {
  if (call.type !== 'CallExpression') return false;
  if (isDeclaredInFormatModule(services, call.callee, formatModule)) return true;
  const { callee } = call;
  if (
    callee.type === 'MemberExpression' &&
    !callee.computed &&
    callee.property.type === 'Identifier' &&
    callee.property.name === 'format'
  ) {
    return typeOf(services, callee.object).symbol?.name === 'NumberFormat';
  }
  return false;
}

/** The i18next `t('key', { … })` call an options object belongs to, else `null`. */
function translateCall(property) {
  const options = property.parent;
  const call = options?.parent;
  if (
    options?.type !== 'ObjectExpression' ||
    call?.type !== 'CallExpression' ||
    call.arguments[1] !== options
  ) {
    return null;
  }
  const { callee } = call;
  const isT =
    (callee.type === 'Identifier' && callee.name === 't') ||
    (callee.type === 'MemberExpression' &&
      !callee.computed &&
      callee.property.type === 'Identifier' &&
      callee.property.name === 't');
  return isT ? call : null;
}

function isIntrinsicElement(opening) {
  return opening.name.type === 'JSXIdentifier' && /^[a-z]/u.test(opening.name.name);
}

/** Whether the value of this attribute is shown or announced as text. */
function isTextAttribute(services, attribute) {
  if (attribute.name.type !== 'JSXIdentifier') return false;
  const { name } = attribute.name;
  if (isIntrinsicElement(attribute.parent)) return INTRINSIC_TEXT_ATTRIBUTES.has(name);
  if (STRUCTURAL_ATTRIBUTES.has(name) || name.startsWith('data-')) return false;
  if (name.startsWith('aria-')) return TEXT_ARIA_ATTRIBUTES.has(name);
  const checker = services.program.getTypeChecker();
  const expression = attribute.value?.expression;
  if (expression === undefined || expression.type === 'JSXEmptyExpression') return false;
  const contextual = checker.getContextualType(services.esTreeNodeToTSNodeMap.get(expression));
  return contextual !== undefined && admitsString(contextual);
}

/**
 * The expression a sink node displays, else `null`. `node` is a `JSXExpressionContainer` (a child
 * or a text attribute) or a `Property` (an option value of a `t()` call, `count` excepted: i18next
 * reads it to choose the plural form).
 */
export function sinkExpression(services, node) {
  if (node.type === 'JSXExpressionContainer') {
    const { expression, parent } = node;
    if (expression.type === 'JSXEmptyExpression') return null;
    if (parent.type === 'JSXElement' || parent.type === 'JSXFragment') return expression;
    if (parent.type === 'JSXAttribute' && isTextAttribute(services, parent)) return expression;
    return null;
  }
  if (node.type === 'Property' && !node.computed && translateCall(node) !== null) {
    const key = node.key.type === 'Identifier' ? node.key.name : node.key.value;
    return key === 'count' ? null : node.value;
  }
  return null;
}

/** Whether a function type returns text and elements both, which is what `ReactNode` is. */
function returnsRenderable(checker, type) {
  if (type === undefined) return false;
  return unionParts(type).some((part) =>
    part.getCallSignatures().some((signature) => {
      const parts = unionParts(checker.getReturnTypeOfSignature(signature));
      return (
        parts.some((p) => (p.flags & ts.TypeFlags.StringLike) !== 0) &&
        parts.some((p) => (p.flags & PRIMITIVE_FLAGS) === 0)
      );
    }),
  );
}

const FUNCTIONS = new Set(['ArrowFunctionExpression', 'FunctionExpression']);

/** The expressions a function returns, not those of the functions nested in it. */
function* returnedExpressions(sourceCode, fn) {
  if (fn.body.type !== 'BlockStatement') {
    yield fn.body;
    return;
  }
  function* walk(node) {
    if (node == null || typeof node.type !== 'string') return;
    if (node.type === 'ReturnStatement') {
      if (node.argument != null) yield node.argument;
      return;
    }
    if (/Function/u.test(node.type)) return;
    for (const key of sourceCode.visitorKeys[node.type] ?? []) {
      const child = node[key];
      if (Array.isArray(child)) for (const item of child) yield* walk(item);
      else yield* walk(child);
    }
  }
  yield* walk(fn.body);
}

/**
 * Every expression a sink node displays: `sinkExpression`'s, or, for a render callback (a function
 * whose contextual type returns a `ReactNode`), what the function returns.
 */
export function* sinkExpressions(services, sourceCode, node) {
  if (FUNCTIONS.has(node.type)) {
    const checker = services.program.getTypeChecker();
    const contextual = checker.getContextualType(services.esTreeNodeToTSNodeMap.get(node));
    if (returnsRenderable(checker, contextual)) yield* returnedExpressions(sourceCode, node);
    return;
  }
  const expression = sinkExpression(services, node);
  if (expression !== null) yield expression;
}

function isStringTyped(services, node) {
  return (
    (services.program.getTypeChecker().getBaseTypeOfLiteralType(typeOf(services, node)).flags &
      ts.TypeFlags.StringLike) !==
    0
  );
}

/** The number a `String(n)` / `n.toString()` / `n.toFixed()` call turns into text, else `null`. */
function stringifiedOperand(call) {
  const { callee } = call;
  if (callee.type === 'Identifier' && callee.name === 'String' && call.arguments.length === 1) {
    return call.arguments[0];
  }
  if (
    callee.type === 'MemberExpression' &&
    !callee.computed &&
    callee.property.type === 'Identifier' &&
    NUMBER_STRINGIFIERS.has(callee.property.name)
  ) {
    return callee.object;
  }
  return null;
}

function variableOf(sourceCode, identifier) {
  for (let scope = sourceCode.getScope(identifier); scope !== null; scope = scope.upper) {
    const variable = scope.set.get(identifier.name);
    if (variable !== undefined) return variable;
  }
  return undefined;
}

/** The expression a variable is assigned once (`const`, or a `let` with one assignment), else `null`. */
function assignedExpression(sourceCode, identifier) {
  const variable = variableOf(sourceCode, identifier);
  if (variable === undefined || variable.defs.length !== 1) return null;
  const [definition] = variable.defs;
  if (definition.type !== 'Variable' || definition.node.id.type !== 'Identifier') return null;
  const writes = variable.references.filter((reference) => reference.isWrite());
  if (writes.length !== 1) return null;
  const [{ identifier: target, writeExpr }] = writes;
  const { parent } = target;
  const plain =
    (parent.type === 'AssignmentExpression' && parent.operator === '=') ||
    (parent.type === 'VariableDeclarator' && parent.init === writeExpr);
  return plain ? writeExpr : null;
}

/** `useMemo(factory, deps)` of React, decided by where the callee is declared. */
function isUseMemo(services, call) {
  const checker = services.program.getTypeChecker();
  const callee = call.callee.type === 'MemberExpression' ? call.callee.property : call.callee;
  if (callee.type !== 'Identifier') return false;
  let symbol = checker.getSymbolAtLocation(services.esTreeNodeToTSNodeMap.get(callee));
  if (symbol !== undefined && (symbol.flags & ts.SymbolFlags.Alias) !== 0) {
    symbol = checker.getAliasedSymbol(symbol);
  }
  return (
    symbol?.name === 'useMemo' &&
    (symbol.declarations ?? []).some((declaration) => declaration.getSourceFile().isDeclarationFile)
  );
}

/** The function a call to a function declared in this file runs, else `null`. */
function localCallee(sourceCode, call) {
  if (call.callee.type !== 'Identifier') return null;
  const variable = variableOf(sourceCode, call.callee);
  if (variable === undefined || variable.defs.length !== 1) return null;
  const [definition] = variable.defs;
  if (definition.type === 'FunctionName') return definition.node.body ? definition.node : null;
  if (definition.type !== 'Variable' || definition.node.id.type !== 'Identifier') return null;
  const init = assignedExpression(sourceCode, call.callee);
  return init !== null && FUNCTIONS.has(init.type) ? init : null;
}

/**
 * The expressions whose value `expression` displays once it reaches a sink: it descends through
 * templates, `+` on text, logical and conditional expressions, assertions, stringifying calls and
 * `const` initialisers of text, and yields every other expression as `[expression, wrapped]`,
 * `wrapped` telling that a template, a concatenation or a stringifying call turned it into text on
 * the way. A call to a formatter (a function declared in `formatModule`, a RegExp) yields nothing:
 * its result is already the reader's text.
 */
export function* renderedValues(
  services,
  sourceCode,
  expression,
  formatModule = new RegExp(DEFAULT_FORMAT_MODULE, 'u'),
  wrapped = false,
  seen = new Set(),
) {
  if (seen.has(expression)) return;
  seen.add(expression);
  const next = (child) => renderedValues(services, sourceCode, child, formatModule, wrapped, seen);
  const nextWrapped = (child) =>
    renderedValues(services, sourceCode, child, formatModule, true, seen);
  switch (expression.type) {
    case 'TemplateLiteral':
      for (const part of expression.expressions) yield* nextWrapped(part);
      return;
    case 'BinaryExpression':
      if (expression.operator === '+' && isStringTyped(services, expression)) {
        yield* nextWrapped(expression.left);
        yield* nextWrapped(expression.right);
      } else {
        yield [expression, wrapped];
      }
      return;
    case 'LogicalExpression':
      yield* next(expression.left);
      yield* next(expression.right);
      return;
    case 'ConditionalExpression':
      yield* next(expression.consequent);
      yield* next(expression.alternate);
      return;
    case 'TSNonNullExpression':
    case 'TSAsExpression':
    case 'TSSatisfiesExpression':
    case 'TSTypeAssertion':
    case 'ChainExpression':
      yield* next(expression.expression);
      return;
    case 'CallExpression': {
      if (isFormatterCall(services, expression, formatModule)) return;
      const operand = stringifiedOperand(expression);
      if (operand !== null) {
        yield* nextWrapped(operand);
        return;
      }
      yield [expression, wrapped];
      // Text built by a `useMemo` factory or a function of this file is followed to what it returns.
      if (isStringTyped(services, expression)) {
        const factory = isUseMemo(services, expression) ? expression.arguments[0] : undefined;
        const fn =
          factory !== undefined && FUNCTIONS.has(factory.type)
            ? factory
            : localCallee(sourceCode, expression);
        if (fn != null) {
          for (const returned of returnedExpressions(sourceCode, fn)) yield* next(returned);
        }
      }
      return;
    }
    case 'Identifier': {
      yield [expression, wrapped];
      // A number is checked where it is read; a text variable is followed to what built it.
      if (isStringTyped(services, expression)) {
        const initialiser = assignedExpression(sourceCode, expression);
        if (initialiser !== null) yield* next(initialiser);
      }
      return;
    }
    default:
      yield [expression, wrapped];
  }
}
