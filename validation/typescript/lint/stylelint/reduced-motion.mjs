import stylelint from 'stylelint';

/**
 * Stylelint plugin `a11y/reduced-motion`: every `transition` or `animation` a stylesheet declares is
 * switched off by a `prefers-reduced-motion: reduce` block of the same file (WCAG 2.2 SC 2.3.3).
 *
 * A motion rule is covered when each of its selectors is named, verbatim, by a rule inside a
 * `@media (prefers-reduced-motion: reduce)` block that sets `transition` or `animation` to `none`
 * (or a zero duration); a `*` rule there covers the whole file. A motion rule inside
 * `(prefers-reduced-motion: no-preference)` is already opt-in and needs no cover. Only the
 * shorthands and `*-name` / `transition-property` carry motion; `animation-delay` and the like do
 * not start one.
 *
 * Tool first: stylelint-a11y's `media-prefers-reduced-motion` only checks that motion sits inside a
 * reduced-motion query, not that each rule is switched off, and it is not installed; this is the
 * smallest custom rule that checks the cover itself.
 */

export const ruleName = 'a11y/reduced-motion';
const DOC = 'WCAG 2.2 SC 2.3.3';
const messages = stylelint.utils.ruleMessages(ruleName, {
  uncovered: (selector, property) =>
    `"${property}" on "${selector}" has no \`@media (prefers-reduced-motion: reduce)\` rule in this file that sets it to \`none\`: add the selector to that block (doc: ${DOC})`,
});

const MOTION = /^(transition|transition-property|animation|animation-name)$/i;
const OFF_DECL =
  /^(transition|transition-property|animation|animation-name|transition-duration|animation-duration)$/i;
const OFF_VALUE = /^(none|0s|0ms|0\.01ms)$/i;

const isReduce = (node) =>
  node.type === 'atrule' &&
  node.name === 'media' &&
  /prefers-reduced-motion\s*:\s*reduce/i.test(node.params);
const isOptIn = (node) =>
  node.type === 'atrule' &&
  node.name === 'media' &&
  /prefers-reduced-motion\s*:\s*no-preference/i.test(node.params);
const inside = (node, test) => {
  for (let p = node.parent; p; p = p.parent) if (test(p)) return true;
  return false;
};
const normal = (selector) => selector.replace(/\s+/g, ' ').trim();
const isOff = (decl) => OFF_DECL.test(decl.prop) && OFF_VALUE.test(decl.value.trim());
const startsMotion = (decl) => MOTION.test(decl.prop) && !/^none$/i.test(decl.value.trim());

const rule = (primary) => (root, result) => {
  if (!primary) return;
  const covered = new Set();
  root.walkDecls((decl) => {
    if (isOff(decl) && inside(decl, isReduce)) {
      for (const selector of decl.parent.selectors ?? []) covered.add(normal(selector));
    }
  });
  root.walkDecls((decl) => {
    if (!startsMotion(decl) || inside(decl, isReduce) || inside(decl, isOptIn)) return;
    for (const selector of decl.parent.selectors ?? []) {
      if (covered.has('*') || covered.has(normal(selector))) continue;
      stylelint.utils.report({
        ruleName,
        result,
        node: decl,
        message: messages.uncovered(normal(selector), decl.prop),
      });
    }
  });
};
rule.ruleName = ruleName;
rule.messages = messages;

export default stylelint.createPlugin(ruleName, rule);
