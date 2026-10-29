/**
 * `settled-before-action`: act on a settled screen.
 *
 * "A step that changes what a list or region shows -- a search, a filter, a sort, a tab or scope
 * change -- is followed by an assertion of the settled state ... before the next action targets
 * that list or region; a click that lands while the list re-renders acts on a row that is about
 * to move. The lint refuses an action on a locator directly after a fill or
 * selection that changes its list, with no web-first assertion between them."
 *
 * No native rule expresses an order between two statements, so this one reads each statement
 * list of a function body in order. Only a step that re-renders a list arms it:
 *
 * - a `fill`, `press` or `selectOption` on a search, filter or sort control -- a `searchbox`, or a
 *   locator whose accessible name, label, placeholder or test id says search, filter, find or
 *   sort (the `searchWords` option, a regular expression source; English by default, set it for
  other UI languages);
 * - a `fill` or `press` on a `combobox`, whose typed text filters the options it lists;
 * - any `fill` or `press` before an action on an `option`: a picker's options are the results of
 *   the text typed into it, whatever its field is named.
 *
 * The next statement of that list that performs a `click`, `check` or `fill` on a member of a
 * list -- a locator taken by position (`first`, `last`, `nth`), by `filter`, or by a list-member
 * role (`option`, `row`, `cell`, `gridcell`, `listitem`, `radio`, `treeitem`, `menuitem`) -- is
 * refused unless a statement between them makes an `expect(...)` assertion (`expect`,
 * `expect.soft` or `expect.poll`). A plain form field, filled or selected, re-renders no list, so
 * a form is filled field by field with no assertion between the fields, and its radios, rows or
 * positional fields are acted on directly. A locator held in a `const` is read from its
 * declaration. A statement list is one block, so a nested block is read on its own.
 */

const CHANGES = new Set(['fill', 'selectOption', 'press']);
const TYPING = new Set(['fill', 'press']);
const ACTIONS = new Set(['click', 'check', 'fill']);
const MEMBER_PICKS = new Set(['first', 'last', 'nth', 'filter']);
const MEMBER_ROLES = new Set([
  'option',
  'row',
  'cell',
  'gridcell',
  'listitem',
  'radio',
  'treeitem',
  'menuitem',
]);
const NAMED_BY = new Set(['getByLabel', 'getByPlaceholder', 'getByTestId', 'getByTitle']);
/** English by default; the `searchWords` option (a regular expression source) replaces it. */
const DEFAULT_SEARCH_WORDS = 'search|filter|find|sort';
const SKIPPED_KEYS = new Set(['parent', 'loc', 'range']);
const MAX_ALIAS_DEPTH = 4;

/** The call a statement performs as its whole effect: `await x.y(...)` or `x.y(...)`. */
function statementCall(statement) {
  let expression = null;
  if (statement.type === 'ExpressionStatement') expression = statement.expression;
  if (statement.type === 'VariableDeclaration' && statement.declarations.length === 1) {
    expression = statement.declarations[0].init;
  }
  const call = expression?.type === 'AwaitExpression' ? expression.argument : expression;
  if (call?.type !== 'CallExpression' || call.callee.type !== 'MemberExpression') return null;
  const { property } = call.callee;
  if (call.callee.computed || property.type !== 'Identifier') return null;
  return { method: property.name, target: call.callee.object };
}

/** Whether a call is `expect(...)` or one of `expect.soft(...)` / `expect.poll(...)`. */
function isExpectCall(node) {
  if (node.type !== 'CallExpression') return false;
  let callee = node.callee;
  if (callee.type === 'MemberExpression' && !callee.computed) callee = callee.object;
  return callee.type === 'Identifier' && callee.name === 'expect';
}

/** Whether a node contains an `expect` assertion anywhere inside it. */
function assertsInside(node) {
  if (node === null || typeof node !== 'object') return false;
  if (Array.isArray(node)) return node.some(assertsInside);
  if (typeof node.type === 'string' && isExpectCall(node)) return true;
  return Object.entries(node).some(
    ([key, value]) => !SKIPPED_KEYS.has(key) && assertsInside(value),
  );
}

/** The `const` initializer an identifier names in its scope, or null. */
function constInit(identifier, scope) {
  for (let current = scope; current !== null; current = current.upper) {
    const variable = current.set.get(identifier.name);
    if (variable === undefined) continue;
    const definition = variable.defs[0];
    const declarator = definition?.node;
    const constant = definition?.parent?.kind === 'const';
    return constant && declarator?.type === 'VariableDeclarator' ? declarator.init : null;
  }
  return null;
}

/** Whether a literal string or regular expression says search, filter, find or sort. */
function saysSearch(node, words) {
  if (node?.type !== 'Literal') return false;
  const text = node.regex === undefined ? node.value : node.regex.pattern;
  return typeof text === 'string' && words.test(text);
}

/** The `name` option of a `getByRole(role, { name })` call, or null. */
function roleName(call) {
  const options = call.arguments[1];
  if (options?.type !== 'ObjectExpression') return null;
  const name = options.properties.find(
    (property) =>
      property.type === 'Property' &&
      !property.computed &&
      property.key.type === 'Identifier' &&
      property.key.name === 'name',
  );
  return name?.value ?? null;
}

/**
 * Every link of a locator chain, outermost first: each `x.y(...)` call it makes, followed
 * through `await`, `as` and a `const` alias.
 */
function chainCalls(node, scope, depth = 0) {
  const calls = [];
  let current = node;
  while (current) {
    if (current.type === 'AwaitExpression' || current.type === 'TSAsExpression') {
      current = current.type === 'AwaitExpression' ? current.argument : current.expression;
    } else if (current.type === 'CallExpression' && current.callee.type === 'MemberExpression') {
      if (!current.callee.computed) calls.push(current);
      current = current.callee.object;
    } else if (current.type === 'MemberExpression') {
      current = current.object;
    } else if (current.type === 'Identifier' && depth < MAX_ALIAS_DEPTH) {
      const init = constInit(current, scope);
      return init === null ? calls : calls.concat(chainCalls(init, scope, depth + 1));
    } else {
      break;
    }
  }
  return calls;
}

/** The literal role of a `getByRole(role, ...)` call, or null for any other call. */
function roleOf(call) {
  const role = call.arguments[0];
  return call.callee.property.name === 'getByRole' && role?.type === 'Literal' ? role.value : null;
}

/** Whether a call names a search, filter or sort control (see the module comment). */
function namesSearchControl(call, words) {
  const method = call.callee.property.name;
  if (NAMED_BY.has(method)) return saysSearch(call.arguments[0], words);
  if (method !== 'getByRole') return false;
  return roleOf(call) === 'searchbox' || saysSearch(roleName(call), words);
}

/** Whether `method` on the locator `target` re-renders a list (see the module comment). */
function changesList(method, target, scope, words) {
  const calls = chainCalls(target, scope);
  if (calls.some((call) => namesSearchControl(call, words))) return true;
  return TYPING.has(method) && calls.some((call) => roleOf(call) === 'combobox');
}

/** `option`, `member` or null: what list member, if any, a locator picks. */
function memberPicked(target, scope) {
  const calls = chainCalls(target, scope);
  if (calls.some((call) => roleOf(call) === 'option')) return 'option';
  const member = calls.some(
    (call) => MEMBER_PICKS.has(call.callee.property.name) || MEMBER_ROLES.has(roleOf(call)),
  );
  return member ? 'member' : null;
}

/** @type {import('eslint').Rule.RuleModule} */
export const rule = {
  meta: {
    type: 'problem',
    docs: {
      description:
        'Assert the settled state of a list after a search, filter or sort step re-renders it before acting on one of its members.',
    },
    schema: [
      {
        type: 'object',
        properties: { searchWords: { type: 'string' } },
        additionalProperties: false,
      },
    ],
    messages: {
      unsettled:
        '`{{action}}` on a list member directly follows a `{{change}}` that re-renders the list, with no web-first `expect(...)` between them; assert the settled list first.',
    },
  },
  create(context) {
    const words = new RegExp(context.options[0]?.searchWords ?? DEFAULT_SEARCH_WORDS, 'i');
    function readStatements(statements) {
      // The last unsettled change of this list: its method and whether it re-renders a list.
      let pending = null;
      for (const statement of statements) {
        const scope = context.sourceCode.getScope(statement);
        const call = statementCall(statement);
        const picked =
          pending !== null && call !== null && ACTIONS.has(call.method)
            ? memberPicked(call.target, scope)
            : null;
        const refused =
          picked !== null &&
          (pending.rendersList || (picked === 'option' && TYPING.has(pending.method)));
        if (refused) {
          context.report({
            node: statement,
            messageId: 'unsettled',
            data: { action: call.method, change: pending.method },
          });
        }
        if (assertsInside(statement)) pending = null;
        if (call !== null && CHANGES.has(call.method)) {
          const rendersList = changesList(call.method, call.target, scope, words);
          // A plain field filled after a search keeps the search unsettled.
          if (rendersList || pending === null || !pending.rendersList) {
            pending = { method: call.method, rendersList };
          }
        }
      }
    }
    return {
      BlockStatement(node) {
        readStatements(node.body);
      },
      StaticBlock(node) {
        readStatements(node.body);
      },
    };
  },
};
