/**
 * Which components are on screen at a route's address? The answer comes from the route tree and the checker, never from a component name:
 *
 * - A route component is the `component` of a route declared in the router module, resolved to its
 *   function declaration through the checker, across files. A `lazy(() => import(...))` binding is
 *   followed to the export it loads.
 * - A component rendered unconditionally by a route component (not behind a condition that reads
 *   component state) is on the route's screen too, transitively. A dialog or a disclosure body
 *   mounts behind a `useState` value, so it is outside the set; a component behind a condition on
 *   props, permissions, query data or loading is on the screen and inside it.
 *
 * The set (TS function nodes) is computed once per program and router module and cached.
 */
import ts from 'typescript';
import {
  functionOfDeclaration,
  isStateConditionalPart,
  isFunctionNode,
  resolveComponent,
} from './repetition.mjs';

function findDynamicImport(node) {
  if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword)
    return node;
  return ts.forEachChild(node, findDynamicImport);
}

function findDefaultProperty(node) {
  if (
    ts.isPropertyAssignment(node) &&
    ts.isIdentifier(node.name) &&
    node.name.text === 'default' &&
    (ts.isPropertyAccessExpression(node.initializer) || ts.isIdentifier(node.initializer))
  ) {
    return node.initializer;
  }
  return ts.forEachChild(node, findDefaultProperty);
}

function resolvedSymbol(checker, node) {
  const symbol = checker.getSymbolAtLocation(node);
  if (symbol && (symbol.flags & ts.SymbolFlags.Alias) !== 0)
    return checker.getAliasedSymbol(symbol);
  return symbol;
}

/** The function a `lazy(() => import(...))` initialiser loads, else `undefined`. */
function lazyTarget(checker, initializer) {
  const load = findDynamicImport(initializer);
  if (!load) return undefined;
  const picked = findDefaultProperty(initializer);
  if (picked) {
    const target = ts.isPropertyAccessExpression(picked) ? picked.name : picked;
    return functionOfDeclaration(resolvedSymbol(checker, target)?.valueDeclaration);
  }
  // `lazy(() => import('./page'))`: the module's default export.
  const module = resolvedSymbol(checker, load.arguments[0]);
  const exported = module && checker.tryGetMemberInModuleExports('default', module);
  const symbol =
    exported && (exported.flags & ts.SymbolFlags.Alias) !== 0
      ? checker.getAliasedSymbol(exported)
      : exported;
  return functionOfDeclaration(symbol?.valueDeclaration);
}

function routeComponentOf(checker, expression) {
  if (isFunctionNode(expression)) return expression;
  if (!ts.isIdentifier(expression)) return undefined;
  const declaration = resolvedSymbol(checker, expression)?.valueDeclaration;
  if (declaration && ts.isVariableDeclaration(declaration) && declaration.initializer) {
    const lazy = lazyTarget(checker, declaration.initializer);
    if (lazy) return lazy;
  }
  return resolveComponent(checker, expression);
}

/** The components `fn` renders on every render: JSX not behind a state-driven condition. */
function unconditionalComponents(checker, fn) {
  const rendered = new Set();
  function visit(node, conditional) {
    if (!conditional && (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))) {
      const component = resolveComponent(checker, node.tagName);
      if (component) rendered.add(component);
    }
    ts.forEachChild(node, (child) => {
      visit(child, conditional || isStateConditionalPart(checker, node, child));
    });
  }
  visit(fn.body ?? fn, false);
  return rendered;
}

const programCache = new WeakMap();

/** The function nodes of the route components and everything they render unconditionally. */
export function routeComponents(program, routerSuffix) {
  const perRouter = programCache.get(program) ?? new Map();
  programCache.set(program, perRouter);
  const cached = perRouter.get(routerSuffix);
  if (cached) return cached;

  const checker = program.getTypeChecker();
  const reached = new Set();
  perRouter.set(routerSuffix, reached);
  const router = program
    .getSourceFiles()
    .find((file) => !file.isDeclarationFile && file.fileName.endsWith(routerSuffix));
  if (!router) return reached;

  const queue = [];
  function collect(node) {
    if (
      ts.isPropertyAssignment(node) &&
      ts.isIdentifier(node.name) &&
      node.name.text === 'component' &&
      ts.isObjectLiteralExpression(node.parent) &&
      ts.isCallExpression(node.parent.parent)
    ) {
      const component = routeComponentOf(checker, node.initializer);
      if (component && !reached.has(component)) {
        reached.add(component);
        queue.push(component);
      }
    }
    ts.forEachChild(node, collect);
  }
  collect(router);

  for (let next = queue.pop(); next !== undefined; next = queue.pop()) {
    for (const component of unconditionalComponents(checker, next)) {
      if (!reached.has(component)) {
        reached.add(component);
        queue.push(component);
      }
    }
  }
  return reached;
}
