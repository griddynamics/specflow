/**
 * `row-action-names-row` (type-aware): a control rendered once per item
 * of a collection names the item it acts on. "Rendered once per item" is `lib/repetition.mjs`
 * (iteration callbacks by type, repeated components across files, row-varying values by data
 * flow); "control" and its name sources are `lib/interactive.mjs`.
 */
import { ESLintUtils } from '@typescript-eslint/utils';
import { createInteractive } from './lib/interactive.mjs';
import { createRepetition } from './lib/repetition.mjs';

const DOC = 'WCAG 2.2 SC 2.4.6';

export const rule = {
  meta: {
    type: 'problem',
    docs: {
      description: `A control repeated per item builds its accessible name from the item (${DOC}).`,
    },
    schema: [],
    messages: {
      sameName: `Every row renders this '{{tag}}' with the same accessible name: build its label (or aria-label / aria-describedby) from the row, e.g. t('...', { name }) with the visible label inside it (${DOC}).`,
    },
  },
  create(context) {
    const services = ESLintUtils.getParserServices(context);
    const sourceCode = context.sourceCode;
    const repetition = createRepetition(services, sourceCode);
    const interactive = createInteractive(services);

    /**
     * Exemption kept on purpose: a control inside a `<form>` of the row (an inline edit or assign
     * form) is a part of that form, which the row names; its own label ("Save") is the action, and
     * the form is the context the screen reader announces. Only the element named `form` counts.
     */
    function insideRowForm(node, boundary) {
      for (const a of sourceCode.getAncestors(node).reverse()) {
        if (a === boundary) return false;
        if (
          a.type === 'JSXElement' &&
          a.openingElement.name.type === 'JSXIdentifier' &&
          a.openingElement.name.name === 'form'
        ) {
          return true;
        }
      }
      return false;
    }

    return {
      JSXElement(node) {
        const control = interactive.describe(node);
        if (!control) return;
        const row = repetition.rowContext(node);
        if (!row || insideRowForm(node, row.boundary)) return;
        if (control.nameSources.some((source) => repetition.isRowVarying(source, row))) return;
        context.report({
          node: node.openingElement,
          messageId: 'sameName',
          data: { tag: control.tag },
        });
      },
    };
  },
};
