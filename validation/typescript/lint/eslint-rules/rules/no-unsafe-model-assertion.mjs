/**
 * `no-unsafe-model-assertion`:
 * Prohibits double type assertions (`x as unknown as T`, `x as any as T`)
 * and fabricating model types without validation.
 *
 * Directs developers to the 3-step resolution path:
 * 1. Type the source: give the producer of the value a concrete type.
 * 2. Boundary validation: use Zod schema or type predicate guard.
 * 3. Safe narrowing: use control-flow analysis (in, typeof, switch).
 */

function formatTypeAnnotation(typeAnnotation) {
  if (!typeAnnotation) return 'unknown';
  switch (typeAnnotation.type) {
    case 'TSUnknownKeyword':
      return 'unknown';
    case 'TSAnyKeyword':
      return 'any';
    case 'TSNeverKeyword':
      return 'never';
    case 'TSStringKeyword':
      return 'string';
    case 'TSNumberKeyword':
      return 'number';
    case 'TSBooleanKeyword':
      return 'boolean';
    case 'TSTypeReference':
      return formatEntityName(typeAnnotation.typeName);
    case 'TSArrayType':
      return `${formatTypeAnnotation(typeAnnotation.elementType)}[]`;
    case 'TSUnionType':
      return typeAnnotation.types.map(formatTypeAnnotation).join(' | ');
    case 'TSIntersectionType':
      return typeAnnotation.types.map(formatTypeAnnotation).join(' & ');
    case 'TSNullKeyword':
      return 'null';
    case 'TSUndefinedKeyword':
      return 'undefined';
    default:
      return 'type';
  }
}

function formatEntityName(node) {
  if (!node) return 'unknown';
  if (node.type === 'Identifier') return node.name;
  if (node.type === 'TSQualifiedName') {
    return `${formatEntityName(node.left)}.${node.right.name}`;
  }
  return 'unknown';
}

function getBaseIdentifier(typeAnnotation) {
  if (!typeAnnotation) return 'Model';
  if (typeAnnotation.type === 'TSTypeReference' && typeAnnotation.typeName.type === 'Identifier') {
    return typeAnnotation.typeName.name;
  }
  if (typeAnnotation.type === 'TSArrayType') {
    return getBaseIdentifier(typeAnnotation.elementType);
  }
  if (typeAnnotation.type === 'TSUnionType') {
    for (const t of typeAnnotation.types) {
      const base = getBaseIdentifier(t);
      if (base !== 'Model' && base !== 'null' && base !== 'undefined') {
        return base;
      }
    }
  }
  return 'Model';
}

export const rule = {
  meta: {
    type: 'problem',
    docs: {
      description:
        'Prohibit unsafe double type assertions ("as unknown as T") and unvalidated model type fabrication.',
    },
    schema: [],
    messages: {
      unsafeDoubleCast:
        "Unsafe double type assertion ('as {{inner}} as {{target}}') is forbidden. It bypasses type safety and masks contract gaps. Resolution path: (1) Type the source: give the producer of the value (an API schema, a parser) a concrete type; (2) Runtime validation: parse with a Zod schema or type predicate guard (is{{baseTarget}}(x): x is {{baseTarget}}); (3) Control-flow narrowing: use 'in', 'typeof', or discriminated union switch instead of type fabrication.",
    },
  },
  create(context) {
    return {
      'TSAsExpression, TSTypeAssertion'(node) {
        const inner = node.expression;
        if (!inner) return;

        if (inner.type === 'TSAsExpression' || inner.type === 'TSTypeAssertion') {
          const innerAnnotation = inner.typeAnnotation;
          const outerAnnotation = node.typeAnnotation;
          const innerStr = formatTypeAnnotation(innerAnnotation);
          const targetStr = formatTypeAnnotation(outerAnnotation);
          const baseTarget = getBaseIdentifier(outerAnnotation);

          context.report({
            node,
            messageId: 'unsafeDoubleCast',
            data: {
              inner: innerStr,
              target: targetStr,
              baseTarget,
            },
          });
        }
      },
    };
  },
};
