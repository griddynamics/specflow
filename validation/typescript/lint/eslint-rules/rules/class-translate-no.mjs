/**
 * `local/class-translate-no` (type-aware): an element that shows a code value (an identifier, a
 * part number, a key) carries a class token, and a browser translator must not rewrite it. Every
 * JSX element whose `className` carries that token states `translate="no"`.
 *
 * The token is found in a literal, a template, a conditional, an array, the arguments of a call
 * (`clsx('code', x)`), the keys of an object argument (`clsx({ code: on })`) and a `styles.code`
 * member. An element whose props type (from the checker) has no `translate` prop cannot state it,
 * so it is skipped; the primitives that own the class state it once for their callers.
 *
 * Options: `{ className: 'mono' }` (the class token; required).
 */
import { ESLintUtils } from '@typescript-eslint/utils';
import ts from 'typescript';

const escapeRegExp = (text) => text.replaceAll(/[.*+?^${}()|[\]\\]/gu, '\\$&');

function keyIs(key, name, computed = false) {
  if (key.type === 'Identifier') return !computed && key.name === name;
  return key.type === 'Literal' && key.value === name;
}

/** Whether a `className` expression can yield a string holding the token. */
function mentions(node, token, pattern) {
  if (node == null) return false;
  switch (node.type) {
    case 'Literal':
      return typeof node.value === 'string' && pattern.test(node.value);
    case 'TemplateLiteral':
      return node.quasis.some((quasi) => pattern.test(quasi.value.cooked ?? ''));
    case 'ConditionalExpression':
      return mentions(node.consequent, token, pattern) || mentions(node.alternate, token, pattern);
    case 'LogicalExpression':
      return mentions(node.left, token, pattern) || mentions(node.right, token, pattern);
    case 'ArrayExpression':
      return node.elements.some((element) => mentions(element, token, pattern));
    case 'CallExpression':
      return node.arguments.some((argument) => mentions(argument, token, pattern));
    case 'ObjectExpression':
      return node.properties.some(
        (property) =>
          property.type === 'Property' && !property.computed && keyIs(property.key, token),
      );
    case 'MemberExpression':
      return keyIs(node.property, token, node.computed);
    case 'JSXExpressionContainer':
      return mentions(node.expression, token, pattern);
    default:
      return false;
  }
}

export const rule = {
  meta: {
    type: 'problem',
    fixable: 'code',
    docs: {
      description: 'An element whose class marks a code value carries translate="no".',
    },
    schema: [
      {
        type: 'object',
        properties: { className: { type: 'string' } },
        required: ['className'],
        additionalProperties: false,
      },
    ],
    messages: {
      missing:
        'An element with the `{{className}}` class shows a code value that must not be machine-translated: add translate="no".',
    },
  },
  create(context) {
    const token = context.options[0]?.className;
    if (token === undefined) return {};
    const pattern = new RegExp(`(^|\\s)${escapeRegExp(token)}(\\s|$)`, 'u');
    const services = ESLintUtils.getParserServices(context);
    const checker = services.program.getTypeChecker();

    /** False when the element's props type is known and declares no `translate` prop. */
    function acceptsTranslate(node) {
      let type = checker.getTypeAtLocation(services.esTreeNodeToTSNodeMap.get(node.name));
      const signature = type.getCallSignatures()[0] ?? type.getConstructSignatures()[0];
      if (signature) {
        const parameter = signature.parameters[0];
        if (!parameter?.valueDeclaration) return false;
        type = checker.getTypeOfSymbolAtLocation(parameter, parameter.valueDeclaration);
      }
      if ((type.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown)) !== 0) return true;
      return checker.getPropertyOfType(checker.getNonNullableType(type), 'translate') !== undefined;
    }

    return {
      JSXOpeningElement(node) {
        const attributes = node.attributes;
        const className = attributes.find(
          (attribute) => attribute.type === 'JSXAttribute' && attribute.name.name === 'className',
        );
        if (className === undefined || !mentions(className.value, token, pattern)) return;
        const stated = attributes.some(
          (attribute) =>
            attribute.type === 'JSXSpreadAttribute' ||
            (attribute.type === 'JSXAttribute' && attribute.name.name === 'translate'),
        );
        if (stated || !acceptsTranslate(node)) return;
        context.report({
          node: className,
          messageId: 'missing',
          data: { className: token },
          fix: (fixer) => fixer.insertTextAfter(className, ' translate="no"'),
        });
      },
    };
  },
};
