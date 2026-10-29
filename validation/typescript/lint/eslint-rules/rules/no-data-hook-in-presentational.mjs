/**
 * `no-data-hook-in-presentational`: a presentational component never reaches for the network
 * itself.
 *
 * Every screen reads its data through a data-access layer "so the data source can change without
 * changing the component". The failure this rule bans is the other half of that sentence: the
 * *component* changing because the data source is wired into it. A leaf that calls
 * `useSaveItem(listId)` inside itself cannot be rendered in a story, in a simulation or in a test
 * without a live backend -- the coupling, not the hook, is the defect.
 *
 * The container/leaf split, expressed in the only terms a linter can see:
 *
 *   A component that (1) takes props -- so its parent parameterises it -- and
 *   (2) renders its own markup -- so it is a view, not a composition -- is a
 *   presentational leaf, and a presentational leaf may not call a data-access
 *   hook.
 *
 * Both halves matter. A route-level container takes no props (the router gives
 * it its identity), so it may fetch. A thin container takes props and renders
 * only components (`<ItemView ... />`, or a fragment of components),
 * so it may fetch -- which also means a container that wants a wrapper `<div>`
 * has layout in it that belongs in the view. A custom hook (`use*`) is the data
 * layer itself and is never a component, so it may fetch. What is left -- props
 * *and* markup *and* a data hook -- is exactly the leaf that reached for the
 * network.
 *
 * A data-access hook is a TanStack Query hook, any `use*Query` / `use*Mutation`,
 * or any `use*` imported from an `api/*` module (the data-access layer; set the
 * `apiModulePattern` option to the layer's real import path).
 *
 * Custom code rather than configuration because the decision needs three things
 * no pattern matcher has: the module an identifier was imported from, the
 * nearest enclosing component of a call, and whether the JSX under that
 * component is intrinsic markup or composition. `eslint-plugin-boundaries`
 * cannot express it -- it classifies folders, and `src/components/*` is one
 * element type, so "leaf" and "container" live in the same folder by design.
 */

const TANSTACK_HOOKS = new Set([
  'useQuery',
  'useQueries',
  'useSuspenseQuery',
  'useSuspenseQueries',
  'useInfiniteQuery',
  'useSuspenseInfiniteQuery',
  'useMutation',
  'useMutationState',
  'useQueryClient',
  'useIsFetching',
  'useIsMutating',
]);

/** `use*Query` / `use*Mutation`: the naming convention of a data hook. */
const DATA_HOOK_NAME = /^use[A-Z][A-Za-z0-9]*(Query|Mutation)$/u;

/**
 * The data-access layer: an `api/*` module, however it is reached. The `apiModulePattern` option
 * (a regular expression source matched against the import specifier without its extension)
 * replaces this default.
 */
const DEFAULT_API_MODULE = '(^|/)api/[A-Za-z0-9-]+$';

const COMPONENT_NAME = /^[A-Z][A-Za-z0-9]*$/u;
const HOOK_NAME = /^use[A-Z]/u;

const FUNCTION_TYPES = new Set([
  'FunctionDeclaration',
  'FunctionExpression',
  'ArrowFunctionExpression',
]);

/** The name a function is known by: its own, or the binding it is assigned to
 * (including through `memo(...)` / `forwardRef(...)` wrappers). */
function functionName(node) {
  if (node.id?.type === 'Identifier') return node.id.name;
  let current = node;
  let parent = current.parent;
  while (parent) {
    if (parent.type === 'VariableDeclarator' && parent.init === current) {
      return parent.id.type === 'Identifier' ? parent.id.name : undefined;
    }
    if (parent.type === 'CallExpression' && parent.arguments.includes(current)) {
      current = parent;
      parent = current.parent;
      continue;
    }
    if (parent.type === 'TSAsExpression' || parent.type === 'TSSatisfiesExpression') {
      current = parent;
      parent = current.parent;
      continue;
    }
    return undefined;
  }
  return undefined;
}

/** The nearest enclosing function that is a React component, or undefined when
 * the nearest enclosing function is a custom hook or an anonymous callback. */
function enclosingComponent(node) {
  let current = node.parent;
  while (current) {
    if (FUNCTION_TYPES.has(current.type)) {
      const name = functionName(current);
      if (name === undefined) {
        // An anonymous callback: attribute it to whatever encloses it.
        current = current.parent;
        continue;
      }
      if (HOOK_NAME.test(name)) return undefined; // a custom hook is the data layer
      return COMPONENT_NAME.test(name) ? { node: current, name } : undefined;
    }
    current = current.parent;
  }
  return undefined;
}

function isIntrinsic(openingElement) {
  const name = openingElement.name;
  return name.type === 'JSXIdentifier' && /^[a-z]/u.test(name.name);
}

export const rule = {
  meta: {
    type: 'problem',
    docs: {
      description:
        'A presentational component (one that takes props and renders its own markup) must not call a data-access hook; lift the hook into a thin container.',
    },
    schema: [
      {
        type: 'object',
        properties: { apiModulePattern: { type: 'string' } },
        additionalProperties: false,
      },
    ],
    messages: {
      presentational:
        '`{{hook}}` is a data-access hook and `{{component}}` is a presentational component: it takes props and renders its own markup, so the network is wired into a leaf. Data reaches a screen through the data-access layer so the data source can change without changing the component -- a leaf that fetches cannot be rendered in a story, in a simulation or in a test. Split it: one pure component that takes everything it renders as props, and thin containers that call the hook and pass the result down.',
    },
  },
  create(context) {
    const apiModule = new RegExp(context.options[0]?.apiModulePattern ?? DEFAULT_API_MODULE, 'u');
    const apiHookLocals = new Set();
    const candidates = [];
    const rendersMarkup = new Set();

    return {
      ImportDeclaration(node) {
        const source = node.source.value;
        if (typeof source !== 'string') return;
        const isApi = apiModule.test(source.replace(/\.[jt]sx?$/u, ''));
        const isTanstack = source === '@tanstack/react-query';
        if (!isApi && !isTanstack) return;
        for (const specifier of node.specifiers) {
          if (specifier.local.type === 'Identifier' && HOOK_NAME.test(specifier.local.name)) {
            apiHookLocals.add(specifier.local.name);
          }
        }
      },

      JSXOpeningElement(node) {
        if (!isIntrinsic(node)) return;
        const component = enclosingComponent(node);
        if (component) rendersMarkup.add(component.node);
      },

      CallExpression(node) {
        if (node.callee.type !== 'Identifier') return;
        const hook = node.callee.name;
        if (!TANSTACK_HOOKS.has(hook) && !DATA_HOOK_NAME.test(hook) && !apiHookLocals.has(hook)) {
          return;
        }
        const component = enclosingComponent(node);
        if (!component) return;
        if (component.node.params.length === 0) return; // route-level container
        candidates.push({ node, hook, component });
      },

      'Program:exit'() {
        for (const candidate of candidates) {
          if (!rendersMarkup.has(candidate.component.node)) continue; // thin container
          context.report({
            node: candidate.node,
            messageId: 'presentational',
            data: { hook: candidate.hook, component: candidate.component.name },
          });
        }
      },
    };
  },
};
