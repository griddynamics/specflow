/**
 * `no-straight-quote-template`: a quotation shown to the reader is an i18n string with the
 * locale's own quotation marks, not a template literal that wraps an interpolation in a straight
 * `"`. Only template literals in `.tsx` render
 * code are checked; a CSS attribute selector such as `[id="${id}"]` is a selector, and is told
 * apart by the `=` before the quote.
 */

export const rule = {
  meta: {
    type: 'problem',
    docs: {
      description:
        'A quoted value is an i18n string with curly quotes, not a template literal with straight quotes.',
    },
    schema: [],
    messages: {
      quote:
        'Do not wrap an interpolation in straight quotes in a template literal; use an i18n string with the locale quotation marks.',
    },
  },
  create(context) {
    return {
      TemplateLiteral(node) {
        const { quasis } = node;
        for (let index = 0; index < node.expressions.length; index += 1) {
          const before = quasis[index].value.raw;
          const after = quasis[index + 1].value.raw;
          if (before.endsWith('"') && !before.endsWith('="') && after.startsWith('"')) {
            context.report({ node, messageId: 'quote' });
            return;
          }
        }
      },
    };
  },
};
