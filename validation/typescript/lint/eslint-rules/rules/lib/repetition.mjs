/**
 * Is this JSX rendered once per item of a collection? The answer comes from program structure, TypeScript types and data flow, never
 * from a component, variable or method name:
 *
 * - An iteration context is a callback handed to a call that the checker resolves to a declaration
 *   in the default library, on a receiver (or first argument) that is iterable or array-like, or a
 *   loop body.
 * - A function component instantiated (`<X>`) inside an iteration context is repeated, and so is
 *   every component it renders, except one mounted behind a condition that reads component state
 *   (a `useState` value: a dialog, a disclosure body), which shows for one item at a time. A
 *   condition on props, permissions, query data or loading does not exempt. The set is computed once per program over every project source
 *   file (cross-file, through the checker) as a fixpoint and cached per program.
 * - A row-varying value is a `useId()` result, a parameter of an iteration callback, a prop of a repeated component, a
 *   loop variable, or any variable whose initialiser reads one of them (scope analysis).
 */
import ts from 'typescript';

const LOOP_KINDS = new Set([
  ts.SyntaxKind.ForStatement,
  ts.SyntaxKind.ForOfStatement,
  ts.SyntaxKind.ForInStatement,
]);

export function isFunctionNode(node) {
  return (
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node) ||
    ts.isMethodDeclaration(node)
  );
}

function isIterableType(checker, type) {
  const nonNull = checker.getNonNullableType(type);
  if (nonNull.isUnion()) return nonNull.types.some((t) => isIterableType(checker, t));
  if ((nonNull.flags & ts.TypeFlags.StringLike) !== 0) return false;
  const apparent = checker.getApparentType(nonNull);
  return apparent.getProperties().some((p) => String(p.escapedName).startsWith('__@iterator'));
}

function isArrayLikeType(checker, type) {
  if (isIterableType(checker, type)) return true;
  const nonNull = checker.getNonNullableType(type);
  if (nonNull.isUnion()) return nonNull.types.some((t) => isArrayLikeType(checker, t));
  if ((nonNull.flags & ts.TypeFlags.StringLike) !== 0) return false;
  // A function has a `length` too: `promise.then(ok, (e) => ...)` iterates nothing.
  if (nonNull.getCallSignatures().length > 0) return false;
  const apparent = checker.getApparentType(nonNull);
  const length = apparent.getProperty('length');
  return length !== undefined;
}

function stripParens(node) {
  let current = node;
  while (current.parent && ts.isParenthesizedExpression(current.parent)) current = current.parent;
  return current;
}

/**
 * True when the function literal is an argument of a call resolved to a default-library
 * declaration whose receiver, or first argument, is an iterable or array-like value.
 */
function isIterationCallback(program, checker, fn) {
  const arg = stripParens(fn);
  const call = arg.parent;
  if (!call || !ts.isCallExpression(call)) return false;
  const index = call.arguments.indexOf(arg);
  if (index < 0) return false;
  const declaration = checker.getResolvedSignature(call)?.declaration;
  if (!declaration || !program.isSourceFileDefaultLibrary(declaration.getSourceFile())) {
    return false;
  }
  const callee = call.expression;
  if (ts.isPropertyAccessExpression(callee)) {
    const receiver = checker.getTypeAtLocation(callee.expression);
    if (isIterableType(checker, receiver)) return true;
  }
  const first = call.arguments[0];
  return (
    index > 0 && first !== undefined && isArrayLikeType(checker, checker.getTypeAtLocation(first))
  );
}

function insideRepeatedSyntax(program, checker, node) {
  for (let current = node.parent; current; current = current.parent) {
    if (LOOP_KINDS.has(current.kind)) return true;
    if (isFunctionNode(current) && isIterationCallback(program, checker, current)) return true;
  }
  return false;
}

export function functionOfDeclaration(declaration) {
  if (!declaration) return undefined;
  if (isFunctionNode(declaration)) return declaration;
  if (ts.isVariableDeclaration(declaration) && declaration.initializer) {
    let init = declaration.initializer;
    // memo(fn), forwardRef(fn): the component is the function argument.
    while (ts.isCallExpression(init) || ts.isParenthesizedExpression(init)) {
      if (ts.isParenthesizedExpression(init)) {
        init = init.expression;
        continue;
      }
      const inner = init.arguments.find((a) => isFunctionNode(a) || ts.isCallExpression(a));
      if (!inner) return undefined;
      init = inner;
    }
    return isFunctionNode(init) ? init : undefined;
  }
  return undefined;
}

export function resolveComponent(checker, tagName) {
  const target = ts.isPropertyAccessExpression(tagName) ? tagName.name : tagName;
  let symbol = checker.getSymbolAtLocation(target);
  if (!symbol) return undefined;
  if ((symbol.flags & ts.SymbolFlags.Alias) !== 0) symbol = checker.getAliasedSymbol(symbol);
  return functionOfDeclaration(symbol.valueDeclaration);
}

/** `child` is evaluated only when a condition of `node` holds: a branch, or the right of `&&`. */
function isConditionalPart(node, child) {
  if (ts.isConditionalExpression(node)) return child === node.whenTrue || child === node.whenFalse;
  if (ts.isIfStatement(node)) return child === node.thenStatement || child === node.elseStatement;
  return (
    ts.isBinaryExpression(node) &&
    child === node.right &&
    [
      ts.SyntaxKind.AmpersandAmpersandToken,
      ts.SyntaxKind.BarBarToken,
      ts.SyntaxKind.QuestionQuestionToken,
    ].includes(node.operatorToken.kind)
  );
}

const STATE_HOOKS = new Set(['useState', 'useReducer']);

function calleeIsStateHook(checker, call) {
  const callee = ts.isPropertyAccessExpression(call.expression)
    ? call.expression.name
    : call.expression;
  let symbol = checker.getSymbolAtLocation(callee);
  if (symbol && (symbol.flags & ts.SymbolFlags.Alias) !== 0)
    symbol = checker.getAliasedSymbol(symbol);
  return (
    symbol !== undefined &&
    STATE_HOOKS.has(symbol.name) &&
    (symbol.declarations ?? []).some((d) => d.getSourceFile().isDeclarationFile)
  );
}

/**
 * True when the expression reads component state: a value from React's `useState` / `useReducer`,
 * directly or through a `const` derived from one (`const open = selected !== null`).
 */
function readsComponentState(checker, expression, seen = new Set()) {
  let found = false;
  function visit(node) {
    if (found) return;
    if (ts.isIdentifier(node)) {
      const symbol = checker.getSymbolAtLocation(node);
      const declaration = symbol?.valueDeclaration;
      if (!declaration || seen.has(declaration)) return;
      seen.add(declaration);
      if (ts.isBindingElement(declaration)) {
        const list = declaration.parent.parent;
        if (
          ts.isVariableDeclaration(list) &&
          list.initializer &&
          ts.isCallExpression(list.initializer)
        ) {
          // Only the value of the pair is state; the setter is a function.
          found = calleeIsStateHook(checker, list.initializer) || found;
        }
      } else if (ts.isVariableDeclaration(declaration) && declaration.initializer) {
        found = readsComponentState(checker, declaration.initializer, seen);
      }
      return;
    }
    ts.forEachChild(node, visit);
  }
  visit(expression);
  return found;
}

/** The test that decides whether `child` of `node` is evaluated. */
function conditionOf(node) {
  if (ts.isConditionalExpression(node)) return node.condition;
  if (ts.isIfStatement(node)) return node.expression;
  return node.left;
}

/**
 * `child` is a conditional part of `node` whose condition reads component state: the dialog
 * shape. A condition on props, permissions, query data or loading is not one.
 */
export function isStateConditionalPart(checker, node, child) {
  return isConditionalPart(node, child) && readsComponentState(checker, conditionOf(node));
}

const programCache = new WeakMap();

/** The function nodes (TS AST) of every component rendered once per item, program-wide. */
function repeatedComponents(program) {
  const cached = programCache.get(program);
  if (cached) return cached;
  const checker = program.getTypeChecker();
  const seeds = new Set();
  const renders = new Map();

  // `enclosing` lists the functions around a node, each with whether the path from it to the node
  // crosses a state-driven condition. A component mounted behind one (a dialog, a disclosure body)
  // shows for one item at a time, so it does not inherit the repetition of the component that
  // mounts it. A condition on props, permissions, query data or loading does not exempt.
  function visit(node, enclosing) {
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      const component = resolveComponent(checker, node.tagName);
      if (component) {
        if (insideRepeatedSyntax(program, checker, node)) seeds.add(component);
        for (const { fn, conditional } of enclosing) {
          if (conditional) continue;
          const set = renders.get(fn) ?? new Set();
          set.add(component);
          renders.set(fn, set);
        }
      }
    }
    ts.forEachChild(node, (child) => {
      let next = enclosing;
      if (isStateConditionalPart(checker, node, child))
        next = enclosing.map((e) => ({ ...e, conditional: true }));
      if (isFunctionNode(child)) next = [...next, { fn: child, conditional: false }];
      visit(child, next);
    });
  }

  for (const file of program.getSourceFiles()) {
    if (file.isDeclarationFile || program.isSourceFileFromExternalLibrary(file)) continue;
    visit(file, []);
  }

  const repeated = new Set(seeds);
  const queue = [...seeds];
  for (let next = queue.pop(); next !== undefined; next = queue.pop()) {
    for (const rendered of renders.get(next) ?? []) {
      if (!repeated.has(rendered)) {
        repeated.add(rendered);
        queue.push(rendered);
      }
    }
  }
  programCache.set(program, repeated);
  return repeated;
}

const FUNCTION_TYPES = new Set([
  'FunctionDeclaration',
  'FunctionExpression',
  'ArrowFunctionExpression',
]);
const LOOP_TYPES = new Set(['ForStatement', 'ForOfStatement', 'ForInStatement']);

function parameterVariables(scopeManager, fn) {
  return scopeManager
    .getDeclaredVariables(fn)
    .filter((v) => v.defs.some((d) => d.type === 'Parameter'));
}

function loopVariables(scopeManager, loop) {
  const head = loop.type === 'ForStatement' ? loop.init : loop.left;
  return head?.type === 'VariableDeclaration' ? scopeManager.getDeclaredVariables(head) : [];
}

/**
 * Binds the repetition analysis to one linted file. `services` is typescript-eslint's parser
 * services; `sourceCode` is the rule's source code.
 */
export function createRepetition(services, sourceCode) {
  const { program } = services;
  const checker = program.getTypeChecker();
  const repeated = repeatedComponents(program);
  const scopeManager = sourceCode.scopeManager;

  /** Identifier node -> the variable it resolves to. */
  const resolved = new Map();
  for (const scope of scopeManager.scopes) {
    for (const reference of scope.references)
      resolved.set(reference.identifier, reference.resolved);
  }

  /** True when some identifier under `node` resolves to a variable for which `predicate` holds. */
  function reads(node, predicate) {
    if (!node || typeof node.type !== 'string') return false;
    if (node.type === 'Identifier' || node.type === 'JSXIdentifier') {
      const variable = resolved.get(node);
      return variable ? predicate(variable) : false;
    }
    return (sourceCode.visitorKeys[node.type] ?? []).some((key) => {
      const child = node[key];
      return Array.isArray(child)
        ? child.some((c) => reads(c, predicate))
        : reads(child, predicate);
    });
  }

  /**
   * The repetition context of a node, or `null` when it renders once. `seeds` are the variables
   * that differ per item; `boundary` is the innermost function or loop that repeats it.
   */
  function rowContext(node) {
    const seeds = new Set();
    let boundary = null;
    const ancestors = sourceCode.getAncestors(node);
    for (let i = ancestors.length - 1; i >= 0; i -= 1) {
      const a = ancestors[i];
      let found = [];
      let hit = false;
      if (FUNCTION_TYPES.has(a.type)) {
        const tsNode = services.esTreeNodeToTSNodeMap.get(a);
        if (repeated.has(tsNode) || isIterationCallback(program, checker, tsNode)) {
          hit = true;
          found = parameterVariables(scopeManager, a);
        }
      } else if (LOOP_TYPES.has(a.type)) {
        hit = true;
        found = loopVariables(scopeManager, a);
      }
      // A bare `while` / `do` loop declares no per-item value: no seed, not repeated.
      if (hit) {
        boundary ??= a;
        for (const v of found) seeds.add(v);
      }
    }
    return boundary ? { seeds, boundary, memo: new Map() } : null;
  }

  /** `useId()`: React's per-instance id, which differs for every row of a repeated component. */
  function isInstanceId(init) {
    if (init?.type !== 'CallExpression') return false;
    const callee = init.callee.type === 'MemberExpression' ? init.callee.property : init.callee;
    let symbol = checker.getSymbolAtLocation(services.esTreeNodeToTSNodeMap.get(callee));
    if (symbol && (symbol.flags & ts.SymbolFlags.Alias) !== 0) {
      symbol = checker.getAliasedSymbol(symbol);
    }
    return (
      symbol?.name === 'useId' &&
      (symbol.declarations ?? []).some(
        (declaration) => declaration.getSourceFile().isDeclarationFile,
      )
    );
  }

  function varies(variable, context) {
    if (context.seeds.has(variable)) return true;
    const known = context.memo.get(variable);
    if (known !== undefined) return known;
    context.memo.set(variable, false);
    const result = variable.defs.some((def) => {
      if (def.type === 'Variable') {
        return isInstanceId(def.node.init) || reads(def.node.init, (v) => varies(v, context));
      }
      if (def.type === 'FunctionName') return reads(def.node.body, (v) => varies(v, context));
      return false;
    });
    context.memo.set(variable, result);
    return result;
  }

  /** True when the expression reads a value that differs per item of the context. */
  function isRowVarying(expression, context) {
    return reads(expression, (v) => varies(v, context));
  }

  return { rowContext, isRowVarying };
}
