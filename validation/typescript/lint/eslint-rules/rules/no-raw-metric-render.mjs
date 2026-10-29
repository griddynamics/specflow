/**
 * `local/no-raw-metric-render` (type-aware): a raw model number (a score, a probability, a
 * confidence) never reaches the UI as text or as a hand-made percentage; it is shown through the
 * one component or formatter that turns it into what a person reads (a band, a label, a formatted
 * percentage).
 *
 * Reports a `number`-typed expression whose name matches `namePattern` when it is rendered as JSX
 * text (`{item.score}`), concatenated with a `%` literal (`{item.score + '%'}`) or interpolated
 * before a `%` (`` `${item.score}%` ``). Type-aware: only the checker can tell a number from a
 * string of the same name.
 *
 * Options: `{ namePattern: 'score', use: 'the band component' }` (namePattern is a case-insensitive
 * regular expression source; `use` names the remedy in the message).
 */
import { ESLintUtils } from '@typescript-eslint/utils';
import ts from 'typescript';

function metricName(expression, pattern) {
  let name;
  if (expression.type === 'Identifier') name = expression.name;
  else if (
    expression.type === 'MemberExpression' &&
    !expression.computed &&
    expression.property.type === 'Identifier'
  ) {
    name = expression.property.name;
  }
  return name !== undefined && pattern.test(name) ? name : undefined;
}

export const rule = {
  meta: {
    type: 'problem',
    docs: {
      description: 'A raw numeric metric is not rendered as text or a hand-made percentage.',
    },
    schema: [
      {
        type: 'object',
        properties: { namePattern: { type: 'string' }, use: { type: 'string' } },
        additionalProperties: false,
      },
    ],
    messages: {
      raw: "'{{name}}' is a raw numeric metric; render it through {{use}}, not as the number.",
    },
  },
  create(context) {
    const pattern = new RegExp(context.options[0]?.namePattern ?? 'score', 'iu');
    const use = context.options[0]?.use ?? 'the component that formats it';
    const services = ESLintUtils.getParserServices(context);
    const checker = services.program.getTypeChecker();

    function isNumber(expression) {
      const type = checker.getTypeAtLocation(services.esTreeNodeToTSNodeMap.get(expression));
      return (checker.getBaseTypeOfLiteralType(type).flags & ts.TypeFlags.NumberLike) !== 0;
    }

    function reportIfRaw(expression, reportNode) {
      const name = metricName(expression, pattern);
      if (name === undefined || !isNumber(expression)) return;
      context.report({ node: reportNode ?? expression, messageId: 'raw', data: { name, use } });
    }

    return {
      JSXExpressionContainer(node) {
        if (node.parent?.type !== 'JSXElement' && node.parent?.type !== 'JSXFragment') return;
        const expression = node.expression;
        if (expression.type === 'BinaryExpression' && expression.operator === '+') {
          const percent = [expression.left, expression.right].find(
            (side) =>
              side.type === 'Literal' && typeof side.value === 'string' && side.value.includes('%'),
          );
          if (percent === undefined) return;
          reportIfRaw(expression.left === percent ? expression.right : expression.left, node);
          return;
        }
        reportIfRaw(expression, node);
      },
      TemplateLiteral(node) {
        node.expressions.forEach((expression, index) => {
          if (node.quasis[index + 1]?.value.raw.startsWith('%')) reportIfRaw(expression, node);
        });
      },
    };
  },
};
