/**
 * `local/no-threshold-over-served-verdict` (type-aware): the client does not decide a verdict from
 * a numeric threshold when the server already serves that verdict as a boolean. A comparison such
 * as `item.rank <= 0` beside a served `item.active` is a second, silent implementation of a server
 * rule, and it drifts from the server without anyone noticing.
 *
 * Reports a comparison whose operands are both numeric, where one side is a field whose name
 * matches `fieldPattern` (read as a member access, or as a destructured binding of one) and the
 * type that field sits on declares at least one boolean field. A presence check (`x.rank != null`)
 * is not numeric on both sides and is not reported. `allowEqualityPairs` lists field pairs whose
 * `===`/`!==` comparison on one object compares two recorded values rather than deriving a verdict
 * (for example `['fromPosition', 'toPosition']`).
 *
 * Options: `{ fieldPattern: 'score|rank|confidence|similarity|weight|threshold', allowEqualityPairs: [] }`.
 */
import { ESLintUtils } from '@typescript-eslint/utils';
import ts from 'typescript';

const COMPARISON_OPERATORS = new Set(['<', '<=', '>', '>=', '===', '!==', '==', '!=']);
const DEFAULT_FIELDS = 'score|rank|confidence|similarity|weight|threshold';

function isAllowedPair(node, pairs) {
  if (node.operator !== '===' && node.operator !== '!==') return false;
  const { left, right } = node;
  if (left.type !== 'MemberExpression' || right.type !== 'MemberExpression') return false;
  if (left.computed || right.computed) return false;
  if (left.object.type !== 'Identifier' || right.object.type !== 'Identifier') return false;
  if (left.object.name !== right.object.name) return false;
  if (left.property.type !== 'Identifier' || right.property.type !== 'Identifier') return false;
  const names = [left.property.name, right.property.name];
  return pairs.some(
    ([first, second]) =>
      (names[0] === first && names[1] === second) || (names[0] === second && names[1] === first),
  );
}

const constituents = (type) => (type.isUnion() ? type.types : [type]);
const isNullish = (type) => (type.flags & (ts.TypeFlags.Undefined | ts.TypeFlags.Null)) !== 0;

/** Every non-nullish constituent has `flag` (number-like or boolean-like), and one exists. */
function isOnly(checker, type, flag) {
  let seen = false;
  for (const part of constituents(type)) {
    if (isNullish(part)) continue;
    if ((checker.getBaseTypeOfLiteralType(part).flags & flag) === 0) return false;
    seen = true;
  }
  return seen;
}

function booleanFieldsOf(checker, type, atNode) {
  const found = [];
  for (const part of constituents(type)) {
    if (isNullish(part)) continue;
    for (const symbol of checker.getPropertiesOfType(part)) {
      if (found.includes(symbol.name)) continue;
      const fieldType = checker.getTypeOfSymbolAtLocation(symbol, atNode);
      if (isOnly(checker, fieldType, ts.TypeFlags.BooleanLike)) found.push(symbol.name);
    }
  }
  return found;
}

export const rule = {
  meta: {
    type: 'problem',
    docs: {
      description:
        'Do not derive a verdict from a numeric threshold when the type already serves it as a boolean.',
    },
    schema: [
      {
        type: 'object',
        properties: {
          fieldPattern: { type: 'string' },
          allowEqualityPairs: {
            type: 'array',
            items: { type: 'array', items: { type: 'string' }, minItems: 2, maxItems: 2 },
          },
        },
        additionalProperties: false,
      },
    ],
    messages: {
      threshold:
        "'{{field}}' is a number the server serves; comparing it against a threshold here re-implements a server rule in the view, and it drifts from the server silently. '{{owner}}' already carries the verdict as {{booleans}}: read that field.",
    },
  },
  create(context) {
    const fieldPattern = new RegExp(context.options[0]?.fieldPattern ?? DEFAULT_FIELDS, 'iu');
    const pairs = context.options[0]?.allowEqualityPairs ?? [];
    const services = ESLintUtils.getParserServices(context);
    const checker = services.program.getTypeChecker();
    const typeOf = (node) => checker.getTypeAtLocation(services.esTreeNodeToTSNodeMap.get(node));

    function fieldOperand(node) {
      if (
        node.type === 'MemberExpression' &&
        !node.computed &&
        node.property.type === 'Identifier' &&
        fieldPattern.test(node.property.name)
      ) {
        return { field: node.property.name, ownerType: typeOf(node.object), atNode: node };
      }
      if (node.type === 'Identifier' && fieldPattern.test(node.name)) {
        const declaration = checker
          .getSymbolAtLocation(services.esTreeNodeToTSNodeMap.get(node))
          ?.declarations?.at(0);
        if (declaration && ts.isBindingElement(declaration)) {
          const bindingPattern = declaration.parent;
          const source = ts.isVariableDeclaration(bindingPattern.parent)
            ? bindingPattern.parent.initializer
            : bindingPattern.parent;
          if (source) {
            return { field: node.name, ownerType: checker.getTypeAtLocation(source), atNode: node };
          }
        }
      }
      return undefined;
    }

    return {
      BinaryExpression(node) {
        if (!COMPARISON_OPERATORS.has(node.operator)) return;
        if (isAllowedPair(node, pairs)) return;
        if (node.left.type === 'PrivateIdentifier') return;
        if (!isOnly(checker, typeOf(node.left), ts.TypeFlags.NumberLike)) return;
        if (!isOnly(checker, typeOf(node.right), ts.TypeFlags.NumberLike)) return;
        for (const side of [node.left, node.right]) {
          const operand = fieldOperand(side);
          if (!operand) continue;
          const booleans = booleanFieldsOf(checker, operand.ownerType, operand.atNode);
          if (booleans.length === 0) continue;
          context.report({
            node,
            messageId: 'threshold',
            data: {
              field: operand.field,
              owner: checker.typeToString(operand.ownerType),
              booleans: booleans.map((name) => `'${name}'`).join(', '),
            },
          });
          return;
        }
      },
    };
  },
};
