/**
 * `no-query-data-in-state`:
 * server state from TanStack Query never gets mirrored into component state
 * or a context value ("Server state through
 * TanStack Query with typed keys; never mirror it into useState.").
 *
 * Real scope analysis (tool first: ESLint's own scope
 * manager already resolves every identifier to the `Variable` it binds --
 * the reason it exists: a naive pass that tracked "query data"
 * by identifier *name* in a file-wide Set, which false-positived on two
 * unrelated components in the same file each destructuring a `data`
 * binding from a different source (only one of them a TanStack Query
 * hook) -- the second component's own, unrelated `data` was flagged
 * because the NAME matched, not the binding. This version binds every
 * tracked value to the actual `Variable` object `context.sourceCode`
 * resolves (`getDeclaredVariables` for a declaration, the matching
 * `Reference.resolved` for a read of one), so two same-named bindings in
 * different scopes are never confused.
 *
 * A variable is treated as "query data" when it is bound to the `data`
 * property of a TanStack Query hook result -- either destructured directly
 * (`const { data } = useQuery(...)`) or read off a named result
 * (`const q = useQuery(...); const x = q.data`). The rule then reports
 * that variable reaching:
 *   1. a `useState(...)` initial-value argument;
 *   2. the argument of a state setter call (`set*`) anywhere inside a
 *      `useEffect(...)` callback;
 *   3. the `value` prop of a `*.Provider`/`Provider` JSX element.
 */

const QUERY_HOOKS = new Set([
  'useQuery',
  'useSuspenseQuery',
  'useInfiniteQuery',
  'useSuspenseInfiniteQuery',
  'useQueries',
  'useMutation',
]);

function calleeName(node) {
  return node.type === 'CallExpression' && node.callee.type === 'Identifier'
    ? node.callee.name
    : undefined;
}

function isInsideUseEffectCallback(node) {
  let current = node;
  while (current) {
    const parent = current.parent;
    if (
      parent?.type === 'CallExpression' &&
      calleeName(parent) === 'useEffect' &&
      parent.arguments[0] === current
    ) {
      return true;
    }
    current = parent;
  }
  return false;
}

function providerName(jsxName) {
  if (jsxName.type === 'JSXIdentifier') return jsxName.name;
  if (jsxName.type === 'JSXMemberExpression' && jsxName.property.type === 'JSXIdentifier') {
    return jsxName.property.name;
  }
  return undefined;
}

/** The real `Variable` a declaration Identifier binds (not a name lookup):
 * walk this identifier's scope chain for the `Variable` whose `defs` name
 * it. Every scope in the file is already fully resolved before any rule
 * visitor runs (ESLint builds the whole scope tree once, up front), so
 * this is safe to call regardless of visit order. */
function declaredVariableOf(context, identifierNode) {
  let scope = context.sourceCode.getScope(identifierNode);
  while (scope) {
    const variable = scope.variables.find((v) => v.defs.some((d) => d.name === identifierNode));
    if (variable) return variable;
    scope = scope.upper;
  }
  return undefined;
}

/** The real `Variable` a READ of this identifier resolves to (its
 * `Reference.resolved`), e.g. the `q` in `const { data } = q`. */
function resolvedVariableOf(context, identifierNode) {
  let scope = context.sourceCode.getScope(identifierNode);
  while (scope) {
    const reference = scope.references.find((r) => r.identifier === identifierNode);
    if (reference) return reference.resolved ?? undefined;
    scope = scope.upper;
  }
  return undefined;
}

/** For an ObjectPattern like `{ data }` (or `{ data: renamed }`), the real
 * `Variable` bound to the property literally named `data`, if any. */
function dataPropertyVariable(context, pattern) {
  if (pattern.type !== 'ObjectPattern') return undefined;
  for (const property of pattern.properties) {
    if (
      property.type === 'Property' &&
      !property.computed &&
      property.key.type === 'Identifier' &&
      property.key.name === 'data' &&
      property.value.type === 'Identifier'
    ) {
      return declaredVariableOf(context, property.value);
    }
  }
  return undefined;
}

export const rule = {
  meta: {
    type: 'problem',
    docs: {
      description:
        'Server state from a TanStack Query hook must not be mirrored into useState, an effect-driven setter, or a context value.',
    },
    schema: [],
    messages: {
      useState: 'Query data reached useState; read it from the query result instead.',
      setter:
        'Query data reached a state setter inside useEffect; read it from the query result instead.',
      contextValue:
        'Query data reached a context Provider value; read it from the query result instead.',
    },
  },
  create(context) {
    const queryResultVariables = new Set(); // Variable objects, e.g. `q` in `const q = useQuery(...)`
    const queryDataVariables = new Set(); // Variable objects bound to a query result's `.data`

    function reportIfQueryData(identifierNode, messageId) {
      const variable = resolvedVariableOf(context, identifierNode);
      if (variable && queryDataVariables.has(variable)) {
        context.report({ node: identifierNode, messageId });
      }
    }

    return {
      VariableDeclarator(node) {
        const init = node.init;

        // `const x = q.data`, `q` a previously recorded query-result variable.
        if (
          init?.type === 'MemberExpression' &&
          !init.computed &&
          init.object.type === 'Identifier' &&
          init.property.type === 'Identifier' &&
          init.property.name === 'data' &&
          node.id.type === 'Identifier'
        ) {
          const objectVariable = resolvedVariableOf(context, init.object);
          if (objectVariable && queryResultVariables.has(objectVariable)) {
            const bound = declaredVariableOf(context, node.id);
            if (bound) queryDataVariables.add(bound);
          }
          return;
        }

        // `const { data } = q`, `q` a previously recorded query-result variable.
        if (init?.type === 'Identifier') {
          const objectVariable = resolvedVariableOf(context, init);
          if (objectVariable && queryResultVariables.has(objectVariable)) {
            const dataVar = dataPropertyVariable(context, node.id);
            if (dataVar) queryDataVariables.add(dataVar);
          }
          return;
        }

        if (init?.type !== 'CallExpression' || !QUERY_HOOKS.has(calleeName(init) ?? '')) return;

        // `const q = useQuery(opts)`.
        if (node.id.type === 'Identifier') {
          const bound = declaredVariableOf(context, node.id);
          if (bound) queryResultVariables.add(bound);
          return;
        }
        // `const { data } = useQuery(opts)`.
        const dataVar = dataPropertyVariable(context, node.id);
        if (dataVar) queryDataVariables.add(dataVar);
      },

      'CallExpression[callee.name="useState"]'(node) {
        const arg = node.arguments[0];
        if (arg?.type === 'Identifier') reportIfQueryData(arg, 'useState');
      },

      'CallExpression[callee.name=/^set[A-Z]/]'(node) {
        const arg = node.arguments[0];
        if (arg?.type === 'Identifier' && isInsideUseEffectCallback(node)) {
          reportIfQueryData(arg, 'setter');
        }
      },

      JSXAttribute(node) {
        if (node.name.name !== 'value') return;
        const opening = node.parent;
        if (opening?.type !== 'JSXOpeningElement') return;
        if (providerName(opening.name) !== 'Provider') return;
        const expr =
          node.value?.type === 'JSXExpressionContainer' ? node.value.expression : undefined;
        if (expr?.type === 'Identifier') reportIfQueryData(expr, 'contextValue');
      },
    };
  },
};
