/**
 * `no-pending-label-swap` (type-aware): a control keeps its label while its
 * request is in flight; it shows a `loading` state, never a different caption.
 * Swapping "Save" for "Saving" resizes the control mid-request, reflows
 * the row around it and takes the name of the action away from the person who pressed it.
 *
 * Flagged: a conditional (a ternary, however deeply nested in a template, `&&`, `??` or a `const`
 * the label is read from) at
 *
 * - a control's accessible-name source (`lib/interactive.mjs`: its text children, `aria-label`,
 *   `title`, `label` and the like), or
 * - a label-typed prop (`lib/render-sinks.mjs`: a prop that is shown or announced as text),
 *
 * whose condition reads pending state and whose branches are different texts.
 *
 * Pending state is found by where it comes from, never by a verb or a variable name:
 *
 * - a pending flag of a `@tanstack/react-query` result type, read as a member or from a
 *   destructuring of the result. The flags are library API facts, declared on `QueryObserverBaseResult`,
 *   `InfiniteQueryObserverBaseResult` and `MutationObserverBaseResult`: `isPending`, `isLoading`,
 *   `isInitialLoading`, `isFetching`, `isRefetching`, `isFetchingNextPage` and
 *   `isFetchingPreviousPage`. The outcome flags (`isError`, `isSuccess`, `isStale`, ...) are not
 *   pending state: a label that names the outcome is a different thing;
 * - a comparison of the `status` or `fetchStatus` property of those result types with the pending
 *   value of the library's `QueryStatus` / `MutationStatus` / `FetchStatus` unions
 *   (`m.status === 'pending'`, `q.fetchStatus !== 'idle'` is not: only `'pending'` and
 *   `'fetching'`);
 * - the count of in-flight requests the library's `useIsFetching` / `useIsMutating` hooks return,
 *   compared or used as a condition;
 * - a value the component itself passes to a `loading` prop (a component prop, or a flag held in
 *   state), which declares it as the pending flag of the control;
 * - a `const` derived from any of these.
 *
 * Custom code because it needs the checker for the origin of a property and the control analysis.
 */
import { ESLintUtils } from '@typescript-eslint/utils';
import ts from 'typescript';
import { createInteractive } from './lib/interactive.mjs';
import { sinkExpression } from './lib/render-sinks.mjs';

const DOC = 'WCAG 2.2 SC 3.2.4';
const QUERY_LIBRARY = /[\\/]@tanstack[\\/](react-)?query/u;

/** Boolean properties of the library's result types that say a request is in flight. */
const PENDING_FLAGS = new Set([
  'isPending',
  'isLoading',
  'isInitialLoading',
  'isFetching',
  'isRefetching',
  'isFetchingNextPage',
  'isFetchingPreviousPage',
]);
/** Properties of the result types that hold the request's state, and the in-flight values. */
const STATE_PROPERTIES = new Set(['status', 'fetchStatus']);
const PENDING_VALUES = new Set(['pending', 'fetching']);
/** Hooks of the library that return how many requests are in flight. */
const IN_FLIGHT_COUNTS = new Set(['useIsFetching', 'useIsMutating']);
const COMPARISONS = new Set(['===', '!==', '==', '!=']);
const MAX_HOPS = 3;

export const rule = {
  meta: {
    type: 'problem',
    docs: {
      description: `A control keeps its label while its request is pending (${DOC}).`,
    },
    schema: [],
    messages: {
      swap: `This label changes while the request is pending, so the control resizes mid-request and loses the name of its action. Keep the label and pass a 'loading' flag to the control instead (${DOC}).`,
    },
  },
  create(context) {
    const services = ESLintUtils.getParserServices(context);
    const checker = services.program.getTypeChecker();
    const { sourceCode } = context;
    const interactive = createInteractive(services);

    /** Variables and property symbols this file passes to a `loading` prop. */
    const loadingVariables = new Set();
    const loadingProperties = new Set();
    const containers = [];

    const tsNode = (node) => services.esTreeNodeToTSNodeMap.get(node);

    function resolved(identifier) {
      for (let scope = sourceCode.getScope(identifier); scope; scope = scope.upper) {
        const reference = scope.references.find((candidate) => candidate.identifier === identifier);
        if (reference) return reference.resolved ?? undefined;
      }
      return undefined;
    }

    /** Every identifier read and (outermost) non-computed member in the expression. */
    function* reads(node) {
      if (node == null || typeof node.type !== 'string') return;
      if (node.type === 'Identifier') {
        yield node;
        return;
      }
      if (node.type === 'MemberExpression' && !node.computed) {
        yield node;
        return;
      }
      if (isStatusComparison(node)) {
        yield node;
        return;
      }
      for (const key of sourceCode.visitorKeys[node.type] ?? []) {
        if (node.type === 'Property' && key === 'key' && !node.computed) continue;
        const child = node[key];
        if (Array.isArray(child)) for (const item of child) yield* reads(item);
        else yield* reads(child);
      }
    }

    const fromQueryLibrary = (symbol) =>
      (symbol?.declarations ?? []).some((declaration) =>
        QUERY_LIBRARY.test(declaration.getSourceFile().fileName),
      );

    function isPendingProperty(symbol, atNode) {
      if (!symbol || !PENDING_FLAGS.has(symbol.name) || !fromQueryLibrary(symbol)) return false;
      const type = checker.getTypeOfSymbolAtLocation(symbol, atNode);
      return (type.flags & ts.TypeFlags.BooleanLike) !== 0;
    }

    /** The library property symbol a member or destructured read stands for, else `undefined`. */
    function libraryProperty(node) {
      if (node.type === 'MemberExpression' && !node.computed) {
        return checker.getSymbolAtLocation(tsNode(node.property));
      }
      if (node.type !== 'Identifier') return undefined;
      const [definition] = resolved(node)?.defs ?? [];
      return definition?.type === 'Variable' && definition.node.id.type === 'ObjectPattern'
        ? destructuredProperty(definition.name)
        : undefined;
    }

    /** `result.status === 'pending'`: the state property of a library result against a pending value. */
    function isStatusComparison(node) {
      if (node.type !== 'BinaryExpression' || !COMPARISONS.has(node.operator)) return false;
      const [literal, operand] =
        node.right.type === 'Literal' ? [node.right, node.left] : [node.left, node.right];
      if (literal.type !== 'Literal' || !PENDING_VALUES.has(literal.value)) return false;
      const symbol = libraryProperty(operand);
      return symbol != null && STATE_PROPERTIES.has(symbol.name) && fromQueryLibrary(symbol);
    }

    /** `useIsMutating` / `useIsFetching` of the library, by the declaration the callee resolves to. */
    function isInFlightCount(identifier) {
      let symbol = checker.getSymbolAtLocation(tsNode(identifier));
      if (symbol && (symbol.flags & ts.SymbolFlags.Alias) !== 0) {
        symbol = checker.getAliasedSymbol(symbol);
      }
      return symbol != null && IN_FLIGHT_COUNTS.has(symbol.name) && fromQueryLibrary(symbol);
    }

    /** The property symbol a destructured binding takes its value from. */
    function destructuredProperty(identifier) {
      const binding = tsNode(identifier)?.parent;
      if (!binding || !ts.isBindingElement(binding) || !ts.isObjectBindingPattern(binding.parent)) {
        return undefined;
      }
      const owner = binding.parent.parent;
      const source =
        ts.isVariableDeclaration(owner) && owner.initializer ? owner.initializer : owner;
      const name = binding.propertyName ?? binding.name;
      if (!ts.isIdentifier(name)) return undefined;
      return checker.getPropertyOfType(checker.getTypeAtLocation(source), name.text);
    }

    /** Is this one read pending state: a property of a request result, or a `loading` flag? */
    function isPendingRead(node, hops) {
      if (node.type === 'BinaryExpression') return true;
      if (node.type === 'MemberExpression') {
        const symbol = checker.getSymbolAtLocation(tsNode(node.property));
        return loadingProperties.has(symbol) || isPendingProperty(symbol, tsNode(node));
      }
      const variable = resolved(node);
      if (variable == null) return false;
      if (loadingVariables.has(variable)) return true;
      const [definition] = variable.defs;
      if (definition?.type === 'ImportBinding') return isInFlightCount(node);
      if (definition?.type !== 'Variable') return false;
      if (definition.node.id.type === 'ObjectPattern') {
        return isPendingProperty(destructuredProperty(definition.name), tsNode(definition.name));
      }
      // A `const` is pending state only when it is made of nothing else
      // (`const busy = save.isPending || remove.isPending`): a flag that merely depends on a
      // request, such as "the list is loaded and empty", is not.
      return (
        definition.parent.kind === 'const' &&
        definition.node.init != null &&
        hops < MAX_HOPS &&
        onlyPending(definition.node.init, hops + 1)
      );
    }

    function onlyPending(expression, hops) {
      const parts = [...reads(expression)];
      return parts.length > 0 && parts.every((node) => isPendingRead(node, hops));
    }

    /** The condition reads pending state somewhere in it. */
    const readsPending = (expression) =>
      [...reads(expression)].some((node) => isPendingRead(node, 0));

    function isText(node) {
      const type = checker.getBaseTypeOfLiteralType(checker.getTypeAtLocation(tsNode(node)));
      return (type.flags & ts.TypeFlags.StringLike) !== 0;
    }

    const isEmptyLiteral = (node) => node.type === 'Literal' && node.value === '';

    /** The conditionals a label expression can show, through wrappers, templates and a `const`. */
    function* conditionalsOf(expression, seen = new Set()) {
      if (seen.has(expression)) return;
      seen.add(expression);
      switch (expression.type) {
        case 'ConditionalExpression':
          yield expression;
          yield* conditionalsOf(expression.consequent, seen);
          yield* conditionalsOf(expression.alternate, seen);
          return;
        case 'LogicalExpression':
        case 'BinaryExpression':
          yield* conditionalsOf(expression.left, seen);
          yield* conditionalsOf(expression.right, seen);
          return;
        case 'TemplateLiteral':
          for (const part of expression.expressions) yield* conditionalsOf(part, seen);
          return;
        case 'TSAsExpression':
        case 'TSNonNullExpression':
        case 'TSSatisfiesExpression':
        case 'ChainExpression':
          yield* conditionalsOf(expression.expression, seen);
          return;
        case 'Identifier': {
          const variable = resolved(expression);
          const [definition] = variable?.defs ?? [];
          if (
            variable?.defs.length === 1 &&
            definition.type === 'Variable' &&
            definition.parent.kind === 'const' &&
            definition.node.id.type === 'Identifier' &&
            definition.node.init != null
          ) {
            yield* conditionalsOf(definition.node.init, seen);
          }
          return;
        }
        default:
      }
    }

    function isSwap(conditional) {
      const { consequent, alternate } = conditional;
      return (
        !isEmptyLiteral(consequent) &&
        !isEmptyLiteral(alternate) &&
        isText(consequent) &&
        isText(alternate) &&
        sourceCode.getText(consequent) !== sourceCode.getText(alternate) &&
        readsPending(conditional.test)
      );
    }

    /** Is the container a control's name source, or a label-typed prop? */
    function isLabelPosition(container) {
      if (sinkExpression(services, container) !== null) {
        if (container.parent.type === 'JSXAttribute') return true;
        // Text inside a control names it, however many elements deep.
        for (let current = container.parent; current; current = current.parent) {
          if (current.type === 'JSXElement' && interactive.describe(current) !== null) return true;
          if (/Function/u.test(current.type)) return false;
        }
        return false;
      }
      const { parent } = container;
      if (parent.type !== 'JSXAttribute') return false;
      const control = interactive.describe(parent.parent.parent);
      return control !== null && control.nameSources.includes(parent.value);
    }

    return {
      JSXAttribute(node) {
        if (node.name.type !== 'JSXIdentifier' || node.name.name !== 'loading') return;
        const expression =
          node.value?.type === 'JSXExpressionContainer' ? node.value.expression : null;
        for (const read of reads(expression)) {
          if (read.type === 'MemberExpression') {
            const symbol = checker.getSymbolAtLocation(tsNode(read.property));
            if (symbol) loadingProperties.add(symbol);
          } else {
            const variable = resolved(read);
            if (variable) loadingVariables.add(variable);
          }
        }
      },
      JSXExpressionContainer(node) {
        if (node.expression.type !== 'JSXEmptyExpression') containers.push(node);
      },
      'Program:exit'() {
        for (const container of containers) {
          const swaps = [...conditionalsOf(container.expression)].filter(isSwap);
          if (swaps.length === 0 || !isLabelPosition(container)) continue;
          for (const conditional of swaps) context.report({ node: conditional, messageId: 'swap' });
        }
      },
    };
  },
};
