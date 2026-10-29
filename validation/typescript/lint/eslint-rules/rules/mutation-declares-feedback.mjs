/**
 * `local/mutation-declares-feedback` (type-aware): every TanStack Query `useMutation` declares
 * what the person is told of its outcome, as `meta.<metaKey>` on its options.
 *
 * One `MutationCache` handler reads the declaration (for example: a success key shows a notice,
 * `failure: 'toast'` shows the refusal, `'inline'` says the screen renders the result itself).
 * Declaring is a decision made where the verb is defined; an omitted declaration is a mutation
 * whose failure nobody was asked about, so the opt-out is an explicit value, never silence.
 *
 * A call is `useMutation` by its declaration, resolved through the checker to
 * `@tanstack/(react-)query` (an import under another name, a re-export), not by its name. Its
 * first argument must be an object literal whose `meta` object holds `<metaKey>`. A declaration
 * written as an object literal carries every key of `keys`; any other expression (a parameter, a
 * shared constant) is left to the type registered for the mutation meta.
 *
 * Options: `{ metaKey: 'feedback', keys: ['success', 'failure'] }` (the defaults).
 */
import { ESLintUtils } from '@typescript-eslint/utils';
import ts from 'typescript';

const QUERY_LIBRARY = /[\\/]@tanstack[\\/](react-)?query/u;

function propertyNamed(object, name) {
  return object.properties.find(
    (property) =>
      property.type === 'Property' &&
      !property.computed &&
      ((property.key.type === 'Identifier' && property.key.name === name) ||
        (property.key.type === 'Literal' && property.key.value === name)),
  );
}

export const rule = {
  meta: {
    type: 'problem',
    docs: { description: 'A useMutation declares the feedback its outcome produces in its meta.' },
    schema: [
      {
        type: 'object',
        properties: {
          metaKey: { type: 'string' },
          keys: { type: 'array', items: { type: 'string' } },
        },
        additionalProperties: false,
      },
    ],
    messages: {
      missing:
        'This useMutation declares no `meta.{{metaKey}}`, so nobody decided what the person is told when it succeeds or fails. Add `meta: { {{metaKey}}: { {{keys}} } }`.',
      incomplete: '`meta.{{metaKey}}` declares every one of: {{keys}}.',
    },
  },
  create(context) {
    const metaKey = context.options[0]?.metaKey ?? 'feedback';
    const keys = context.options[0]?.keys ?? ['success', 'failure'];
    const data = { metaKey, keys: keys.join(', ') };
    const services = ESLintUtils.getParserServices(context);
    const checker = services.program.getTypeChecker();

    function isLibraryUseMutation(callee) {
      const name = callee.type === 'MemberExpression' ? callee.property : callee;
      if (name.type !== 'Identifier') return false;
      let symbol = checker.getSymbolAtLocation(services.esTreeNodeToTSNodeMap.get(name));
      if (symbol && (symbol.flags & ts.SymbolFlags.Alias) !== 0) {
        symbol = checker.getAliasedSymbol(symbol);
      }
      return (
        symbol?.name === 'useMutation' &&
        (symbol.declarations ?? []).some((declaration) =>
          QUERY_LIBRARY.test(declaration.getSourceFile().fileName),
        )
      );
    }

    return {
      CallExpression(node) {
        if (!isLibraryUseMutation(node.callee)) return;
        const options = node.arguments[0];
        if (options?.type !== 'ObjectExpression') {
          context.report({ node, messageId: 'missing', data });
          return;
        }
        const meta = propertyNamed(options, 'meta');
        const metaValue = meta?.value;
        const declaration =
          metaValue?.type === 'ObjectExpression' ? propertyNamed(metaValue, metaKey) : undefined;
        if (declaration == null) {
          context.report({ node: meta ?? node, messageId: 'missing', data });
          return;
        }
        const declared = declaration.value;
        if (
          declared.type === 'ObjectExpression' &&
          !declared.properties.some((property) => property.type === 'SpreadElement') &&
          keys.some((key) => propertyNamed(declared, key) == null)
        ) {
          context.report({ node: declared, messageId: 'incomplete', data });
        }
      },
    };
  },
};
