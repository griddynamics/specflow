/**
 * `decorative-glyph-aria-hidden` (type-aware): a decorative glyph in a control's
 * text is read aloud unless it sits in an `aria-hidden` subtree. A control is whatever
 * `lib/interactive.mjs` detects; a glyph is a character of the Unicode arrow, guillemet or
 * bullet classes, or a middle dot standing alone as a decoration (not between words). Text is a
 * JSX text child or an expression child that yields string literals: a literal, a template, a
 * concatenation, a ternary or a logical operand.
 */
import { ESLintUtils } from '@typescript-eslint/utils';
import { createInteractive, isAriaHidden } from './lib/interactive.mjs';

const DOC = 'WCAG 2.2 / WAI-ARIA 1.2';

const ARROWS = '\\u2190-\\u21FF\\u27F0-\\u27FF\\u2900-\\u297F\\u2B00-\\u2BFF';
const GUILLEMETS = '\\u00AB\\u00BB\\u2039\\u203A';
const BULLETS = '\\u2022\\u2023\\u2043\\u2219\\u25AA\\u25E6\\u25CF';
const MIDDLE_DOT = '\u00B7';
const GLYPH = new RegExp(`[${ARROWS}${GUILLEMETS}${BULLETS}]`, 'u');

export const rule = {
  meta: {
    type: 'problem',
    docs: { description: `Decorative glyphs in a control's text are aria-hidden (${DOC}).` },
    schema: [],
    messages: {
      glyph: `Decorative glyph in a control's text is read aloud: wrap it in <span aria-hidden="true"> (${DOC}).`,
    },
  },
  create(context) {
    const interactive = createInteractive(ESLintUtils.getParserServices(context));

    /**
     * The string pieces an expression yields. `side` says whether other content comes `before` or
     * `after` the piece in the control's text: a dot with content on both sides separates words, a
     * dot with none on one side decorates.
     */
    function pieces(e, side) {
      if (!e) return [];
      switch (e.type) {
        case 'Literal':
          return typeof e.value === 'string' ? [{ text: e.value, ...side }] : [];
        case 'TemplateLiteral':
          return e.quasis.map((q, i) => ({
            text: q.value.cooked ?? '',
            before: side.before || i > 0,
            after: side.after || i < e.quasis.length - 1,
          }));
        case 'ConditionalExpression':
          return [...pieces(e.consequent, side), ...pieces(e.alternate, side)];
        case 'LogicalExpression':
          return [...pieces(e.left, side), ...pieces(e.right, side)];
        case 'BinaryExpression': {
          if (e.operator !== '+') return [];
          const operands = concatenated(e);
          return operands.flatMap((operand, i) =>
            pieces(operand, {
              before: side.before || i > 0,
              after: side.after || i < operands.length - 1,
            }),
          );
        }
        default:
          return [];
      }
    }

    /** The operands of a `a + b + c` chain, in order. */
    function concatenated(e) {
      return e.type === 'BinaryExpression' && e.operator === '+'
        ? [...concatenated(e.left), ...concatenated(e.right)]
        : [e];
    }

    function isGlyph({ text, before, after }) {
      return GLYPH.test(text) || (!(before && after) && text.trim() === MIDDLE_DOT);
    }

    /** A spacing child: whitespace-only text or a `{' '}` literal. */
    function isSpacing(child) {
      if (child.type === 'JSXText') return child.value.trim() === '';
      return (
        child.type === 'JSXExpressionContainer' &&
        child.expression.type === 'Literal' &&
        typeof child.expression.value === 'string' &&
        child.expression.value.trim() === ''
      );
    }

    /** Whether content children come before and after this child in the control's text. */
    function sidesOf(node) {
      const siblings = node.parent.children;
      const index = siblings.indexOf(node);
      return {
        before: !siblings.slice(0, index).every(isSpacing),
        after: !siblings.slice(index + 1).every(isSpacing),
      };
    }

    function glyphText(node) {
      const side = sidesOf(node);
      if (node.type === 'JSXText') return isGlyph({ text: node.value, ...side });
      return pieces(node.expression, side).some(isGlyph);
    }

    /** True when the nearest control ancestor is reached without crossing an aria-hidden element. */
    function exposedInControl(node) {
      const ancestors = context.sourceCode.getAncestors(node);
      for (let i = ancestors.length - 1; i >= 0; i -= 1) {
        const a = ancestors[i];
        if (a.type !== 'JSXElement') continue;
        if (isAriaHidden(a.openingElement)) return false;
        if (interactive.isControl(a)) return true;
      }
      return false;
    }

    function check(node) {
      if (glyphText(node) && exposedInControl(node)) context.report({ node, messageId: 'glyph' });
    }

    return {
      JSXText: check,
      // Expression children only: an attribute's value is not text on screen.
      'JSXElement > JSXExpressionContainer': check,
      'JSXFragment > JSXExpressionContainer': check,
    };
  },
};
