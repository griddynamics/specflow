/**
 * `no-view-state-in-route`: the tab or filter a screen shows is the address's, not a
 * `useState` of a component on that screen. A value
 * held in component state is lost on reload, cannot be linked to and is not restored by Back; the
 * screen keeps it in a validated route search parameter (read with
 * `useSearch`, written with `navigate`).
 *
 * Everything is decided by structure, types and data flow, never by a component name:
 *
 * - Scope. Route components (`lib/route-components.mjs`: the `component` of each route in the
 *   router module, resolved through the checker) and the components they render unconditionally,
 *   transitively and across files. A dialog or a disclosure body mounts behind a condition, so its
 *   state is out of scope: it ends with the dialog and the address does not name it.
 * - What is flagged. A `useState` value that both feeds a controlled prop of a child (the setter,
 *   or a closure calling it, is passed as a function prop of the same element that receives the
 *   value) and selects rendered content (it is read by the test of a conditional, an `&&`, an `if`
 *   or a `switch` whose result is JSX, or by the index of a lookup that yields JSX or the
 *   component rendered next, or it is passed as a prop to a child component that chooses its
 *   content by that prop: `<Tabs value={tab} onChange={setTab} /><Body tab={tab} />`, where `Body`
 *   is resolved through the checker, `lib/prop-selection.mjs`, and followed through further props
 *   to a bounded depth). A form draft feeds a controlled field but selects no content, so it
 *   stays.
 * - Boolean state is state like any other: a boolean that chooses between two renderings
 *   (`grid ? <GridView /> : <ListView />`) is view state. The one exemption is structural: state
 *   that mounts a single optional element (`open && <Panel />`), a disclosure, selects no view.
 *   The limit of the child analysis is its bounded depth and that it follows plain props, not
 *   spread props or context.
 * - Data flow. State variables and their setters are the `Variable`s ESLint's scope manager
 *   resolves; a `const` derived from one in its initialiser reads it too, but only by the value it
 *   carries on (`const shown = chosen ?? 'a'`, `const grid = mode === 'grid'`; not `draft !== baseline`, which
 *   says whether two values differ). A lookup of data by
 *   the state (`cases.find((row) => row.id === selected)`) makes the state a selected item, which
 *   chooses what is shown but not which view, so it is not followed.
 *
 * Custom code because the decision needs variable resolution through derivations and closures,
 * which no pattern matcher offers.
 */
import { ESLintUtils } from '@typescript-eslint/utils';
import ts from 'typescript';
import { choosesContentByProp } from './lib/prop-selection.mjs';
import { resolveComponent } from './lib/repetition.mjs';
import { routeComponents } from './lib/route-components.mjs';

/** How many `const b = f(a)` hops are followed from a read back to a state variable. */
const MAX_HOPS = 3;

const COMPARISONS = new Set(['===', '!==', '==', '!=']);

const DEFAULT_ROUTER = '/src/router.tsx';

const FUNCTIONS = new Set(['FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression']);

function isJsx(node) {
  return node.type === 'JSXElement' || node.type === 'JSXFragment';
}

export const rule = {
  meta: {
    type: 'problem',
    docs: {
      description: 'The tab or filter a screen shows belongs in the route search, not in useState.',
    },
    schema: [
      {
        type: 'object',
        properties: { router: { type: 'string' } },
        additionalProperties: false,
      },
    ],
    messages: {
      stateBound:
        'This {{element}} shows a value held in useState ({{name}}) that also chooses what the screen renders. Keep a tab or filter in a validated route search parameter so Back, reload and a shared link restore it.',
    },
  },
  defaultOptions: [{ router: DEFAULT_ROUTER }],
  create(context) {
    const services = ESLintUtils.getParserServices(context);
    const { program } = services;
    const checker = program.getTypeChecker();
    const { sourceCode } = context;
    const router = context.options[0]?.router ?? DEFAULT_ROUTER;

    /** `{ variable, setter }` per `const [x, setX] = useState(...)` on a route's screen. */
    const states = [];
    /** Variables declared `const x = <expression>`, with that expression. */
    const derivations = new Map();
    /** Expressions that choose what is rendered, with the result they choose between. */
    const selectors = [];
    const elements = [];

    function declaredVariable(declarator, identifier) {
      return sourceCode
        .getDeclaredVariables(declarator)
        .find((variable) => variable.defs.some((definition) => definition.name === identifier));
    }

    function resolved(identifier) {
      let scope = sourceCode.getScope(identifier);
      while (scope) {
        const reference = scope.references.find((candidate) => candidate.identifier === identifier);
        if (reference) return reference.resolved ?? undefined;
        scope = scope.upper;
      }
      return undefined;
    }

    function identifiersIn(node, found = []) {
      if (node == null || typeof node.type !== 'string') return found;
      if (node.type === 'Identifier') {
        found.push(node);
        return found;
      }
      // A property name or a non-computed member is not a read of a variable.
      for (const key of sourceCode.visitorKeys[node.type] ?? []) {
        if (node.type === 'MemberExpression' && key === 'property' && !node.computed) continue;
        if (node.type === 'Property' && key === 'key' && !node.computed) continue;
        const child = node[key];
        if (Array.isArray(child)) for (const item of child) identifiersIn(item, found);
        else identifiersIn(child, found);
      }
      return found;
    }

    /** Does `useState(...)` here call React's hook? Decided by where the callee is declared. */
    function isReactHook(call) {
      const callee = call.callee.type === 'MemberExpression' ? call.callee.property : call.callee;
      if (callee.type !== 'Identifier') return false;
      let symbol = checker.getSymbolAtLocation(services.esTreeNodeToTSNodeMap.get(callee));
      if (symbol && (symbol.flags & ts.SymbolFlags.Alias) !== 0) {
        symbol = checker.getAliasedSymbol(symbol);
      }
      return (
        symbol?.name === 'useState' &&
        (symbol.declarations ?? []).some((declaration) => {
          const file = declaration.getSourceFile();
          return file.isDeclarationFile || program.isSourceFileFromExternalLibrary(file);
        })
      );
    }

    function enclosingFunction(node) {
      for (let current = node.parent; current; current = current.parent) {
        if (FUNCTIONS.has(current.type)) return current;
      }
      return undefined;
    }

    /**
     * The identifiers whose value the expression carries on: a variable, an operand of `??`/`||`/
     * `&&`, a branch of a conditional (not its test), a comparison or negation of such a value.
     * A call, a member read or a lookup is not: `cases.find((row) => row.id === selected)` picks
     * data by the state, which makes the state a selected item and not a view.
     */
    function valueIdentifiers(expression, found = []) {
      switch (expression?.type) {
        case 'Identifier':
          found.push(expression);
          break;
        case 'ConditionalExpression':
          valueIdentifiers(expression.consequent, found);
          valueIdentifiers(expression.alternate, found);
          break;
        case 'LogicalExpression':
          valueIdentifiers(expression.left, found);
          valueIdentifiers(expression.right, found);
          break;
        case 'BinaryExpression':
          // A comparison against a constant picks (`mode === 'grid'`); a comparison of two
          // variables (`draft !== baseline`) says whether they differ, which is not a choice.
          if (
            COMPARISONS.has(expression.operator) &&
            (expression.left.type !== 'Identifier' || expression.right.type !== 'Identifier')
          ) {
            valueIdentifiers(expression.left, found);
            valueIdentifiers(expression.right, found);
          }
          break;
        case 'UnaryExpression':
          valueIdentifiers(expression.argument, found);
          break;
        case 'TSAsExpression':
        case 'TSNonNullExpression':
        case 'TSSatisfiesExpression':
        case 'ChainExpression':
          valueIdentifiers(expression.expression, found);
          break;
        default:
      }
      return found;
    }

    /**
     * The first of `variables` the expression reads: any identifier of the expression itself, then
     * through the `const`s it reads by the value they carry on. With `carried`, only by the value
     * the expression carries on (the prop of a child receives the state, not text computed from it).
     */
    function readOf(expression, variables, hops = 0, carried = hops > 0) {
      const identifiers = carried ? valueIdentifiers(expression) : identifiersIn(expression);
      for (const identifier of identifiers) {
        const variable = resolved(identifier);
        if (variable == null) continue;
        if (variables.has(variable)) return variable;
        const derived = derivations.get(variable);
        if (derived != null && hops < MAX_HOPS) {
          const through = readOf(derived, variables, hops + 1, true);
          if (through != null) return through;
        }
      }
      return undefined;
    }

    /** Does the expression call the setter: it is the setter, or a closure whose body calls it? */
    function callsSetter(expression, setter, hops = 0) {
      if (expression == null) return false;
      if (expression.type === 'Identifier') {
        const variable = resolved(expression);
        if (variable === setter) return true;
        const derived = variable == null ? undefined : derivations.get(variable);
        return derived != null && hops < MAX_HOPS && callsSetter(derived, setter, hops + 1);
      }
      if (
        expression.type === 'ArrowFunctionExpression' ||
        expression.type === 'FunctionExpression'
      ) {
        return identifiersIn(expression.body).some((identifier) =>
          callsSetter(identifier, setter, hops + 1),
        );
      }
      if (expression.type === 'CallExpression') {
        return expression.arguments.some((argument) => callsSetter(argument, setter, hops));
      }
      return false;
    }

    /** Does the node yield JSX or a component: contains JSX, or has the JSX element type? */
    function yieldsContent(node) {
      if (node == null) return false;
      if (isJsx(node)) return true;
      if (node.type === 'JSXExpressionContainer' || node.type === 'JSXText') return false;
      for (const key of sourceCode.visitorKeys[node.type] ?? []) {
        if (FUNCTIONS.has(node.type) && key !== 'body') continue;
        const child = node[key];
        if (Array.isArray(child) ? child.some(yieldsContent) : yieldsContent(child)) return true;
      }
      return false;
    }

    function yieldsElementType(node) {
      const type = checker.getNonNullableType(
        checker.getTypeAtLocation(services.esTreeNodeToTSNodeMap.get(node)),
      );
      const parts = type.isUnion() ? type.types : [type];
      return parts.some((part) => part.symbol?.name === 'Element');
    }

    const overlaps = (a, b) => a.range[0] < b.range[1] && b.range[0] < a.range[1];

    function selectsFrom(test, results, certain = false) {
      selectors.push({ test, results, certain });
    }

    return {
      VariableDeclarator(node) {
        const { id, init } = node;
        if (init?.type === 'CallExpression' && id.type === 'ArrayPattern') {
          const [value, setter] = id.elements;
          if (value?.type === 'Identifier' && setter?.type === 'Identifier' && isReactHook(init)) {
            const fn = enclosingFunction(node);
            if (
              fn &&
              routeComponents(program, router).has(services.esTreeNodeToTSNodeMap.get(fn))
            ) {
              states.push({
                variable: declaredVariable(node, value),
                setter: declaredVariable(node, setter),
              });
            }
          }
          return;
        }
        if (id.type === 'Identifier' && init != null) {
          const variable = declaredVariable(node, id);
          if (variable) derivations.set(variable, init);
        }
      },
      ConditionalExpression(node) {
        selectsFrom(node.test, [node.consequent, node.alternate]);
      },
      LogicalExpression(node) {
        selectsFrom(node.left, [node.right]);
      },
      IfStatement(node) {
        selectsFrom(node.test, [node.consequent, node.alternate]);
      },
      SwitchStatement(node) {
        selectsFrom(
          node.discriminant,
          node.cases.map((entry) => ({
            type: 'BlockStatement',
            body: entry.consequent,
            range: entry.range,
          })),
        );
      },
      MemberExpression(node) {
        // A lookup: the index chooses a rendered element or the component rendered next.
        if (node.computed && yieldsElementType(node)) selectsFrom(node.property, [node], true);
      },
      JSXOpeningElement(node) {
        elements.push(node);
        if (node.name.type === 'JSXIdentifier') {
          // `<Panel />` where `Panel` is looked up: the lookup's index chooses the content.
          const variable = resolved(node.name);
          const init = variable == null ? undefined : derivations.get(variable);
          if (init?.type === 'MemberExpression' && init.computed) {
            selectsFrom(init.property, [node], true);
          }
        }
      },
      'Program:exit'() {
        for (const { variable, setter } of states) {
          if (variable == null || setter == null) continue;
          const own = new Set([variable]);
          const choosers = selectors.filter(
            ({ test, results, certain }) =>
              readOf(test, own) != null && (certain || results.some(yieldsContent)),
          );
          const managed = [];
          const managedOpenings = new Set();
          const controlled = [];
          for (const opening of elements) {
            const attributes = opening.attributes.filter((a) => a.type === 'JSXAttribute');
            if (!attributes.some((a) => callsSetter(a.value?.expression, setter))) continue;
            managed.push(opening.parent);
            managedOpenings.add(opening);
            const fed = attributes.find(
              (a) =>
                a.value?.type === 'JSXExpressionContainer' &&
                !callsSetter(a.value.expression, setter) &&
                readOf(a.value.expression, own) != null,
            );
            if (fed) controlled.push({ opening, fed });
          }
          // A child the value is passed to that chooses its content by that prop.
          for (const opening of elements) {
            if (managedOpenings.has(opening)) continue;
            const component = resolveComponent(
              checker,
              services.esTreeNodeToTSNodeMap.get(opening.name),
            );
            if (component === undefined) continue;
            for (const attribute of opening.attributes) {
              if (
                attribute.type === 'JSXAttribute' &&
                attribute.name.type === 'JSXIdentifier' &&
                attribute.value?.type === 'JSXExpressionContainer' &&
                readOf(attribute.value.expression, own, 0, true) != null &&
                choosesContentByProp(checker, component, attribute.name.name)
              ) {
                choosers.push({ test: attribute, results: [opening], certain: true });
              }
            }
          }
          if (choosers.length === 0 || controlled.length === 0) continue;
          // An element the setter drives (a dialog, an editor, a clear button) or a part of one
          // belongs to the state's own control. What is left is content the state chooses: one
          // lookup, or at least two renderings it chooses between. A single optional element is a
          // disclosure, not a view.
          const alternatives = choosers.flatMap(({ results, certain }) =>
            certain
              ? results
              : results.filter(
                  (result) =>
                    yieldsContent(result) && managed.every((element) => !overlaps(result, element)),
                ),
          );
          if (alternatives.length < 2 && !choosers.some(({ certain }) => certain)) continue;
          for (const { opening, fed } of controlled) {
            context.report({
              node: fed,
              messageId: 'stateBound',
              data: {
                element: opening.name.type === 'JSXIdentifier' ? opening.name.name : 'component',
                name: variable.name,
              },
            });
          }
        }
      },
    };
  },
};
