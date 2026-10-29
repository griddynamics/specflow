/**
 * Accessibility-semantics rules (WCAG 2.2, WAI-ARIA 1.2). `eslint-plugin-jsx-a11y` was
 * checked first and
 * covers none of these shapes: `role-supports-aria-props` accepts `aria-label` on a
 * role-less `<div>` (a global attribute), there is no rule on `role="img"` children,
 * or on `<img>` dimensions. Each rule below is one AST decision.
 */
const DOC = 'WCAG 2.2 / WAI-ARIA 1.2';

function jsxName(opening) {
  const n = opening.name;
  return n.type === 'JSXIdentifier' ? n.name : undefined;
}

function findAttr(opening, name) {
  return opening.attributes.find(
    (a) => a.type === 'JSXAttribute' && a.name.type === 'JSXIdentifier' && a.name.name === name,
  );
}

function hasSpread(opening) {
  return opening.attributes.some((a) => a.type === 'JSXSpreadAttribute');
}

function stringValue(attr) {
  const v = attr?.value;
  if (v?.type === 'Literal' && typeof v.value === 'string') return v.value;
  if (
    v?.type === 'JSXExpressionContainer' &&
    v.expression.type === 'Literal' &&
    typeof v.expression.value === 'string'
  ) {
    return v.expression.value;
  }
  return undefined;
}

/** `aria-label` / `aria-labelledby` on a generic `<div>`/`<span>` names nothing. */
export const ariaNameNeedsRole = {
  meta: {
    type: 'problem',
    docs: { description: `aria-label on a role-less div/span is ignored (${DOC}).` },
    schema: [],
    messages: {
      needsRole: `'{{attr}}' on a role-less <{{tag}}> is not announced: give it role="group" (or use a section or fieldset element) (${DOC}).`,
    },
  },
  create(context) {
    return {
      JSXOpeningElement(node) {
        const tag = jsxName(node);
        if (tag !== 'div' && tag !== 'span') return;
        if (findAttr(node, 'role') || hasSpread(node)) return;
        for (const attr of ['aria-label', 'aria-labelledby']) {
          const found = findAttr(node, attr);
          if (found) context.report({ node: found, messageId: 'needsRole', data: { attr, tag } });
        }
      },
    };
  },
};

/** `role="img"` makes its descendants presentational: visible text would be hidden. */
export const noImgRoleWithText = {
  meta: {
    type: 'problem',
    docs: { description: `role="img" must not wrap visible text (${DOC}).` },
    schema: [],
    messages: {
      hidesText: `role="img" hides its children from assistive technology, so this visible text is lost: use role="group" with the label, or drop the role (${DOC}).`,
    },
  },
  create(context) {
    return {
      JSXElement(node) {
        if (stringValue(findAttr(node.openingElement, 'role')) !== 'img') return;
        const hasText = node.children.some(
          (c) =>
            (c.type === 'JSXText' && /[\p{L}\p{N}]/u.test(c.value)) ||
            (c.type === 'JSXExpressionContainer' && c.expression.type !== 'JSXEmptyExpression'),
        );
        if (hasText) context.report({ node: node.openingElement, messageId: 'hidesText' });
      },
    };
  },
};

/** Every `<img>` reserves its box. */
export const imgRequiresDimensions = {
  meta: {
    type: 'problem',
    docs: { description: '<img> needs width and height to avoid layout shift.' },
    schema: [],
    messages: {
      dimensions: `<img> without both width and height shifts the layout when it loads: set both (${DOC}).`,
    },
  },
  create(context) {
    return {
      JSXOpeningElement(node) {
        if (jsxName(node) !== 'img' || hasSpread(node)) return;
        if (!findAttr(node, 'width') || !findAttr(node, 'height')) {
          context.report({ node, messageId: 'dimensions' });
        }
      },
    };
  },
};
