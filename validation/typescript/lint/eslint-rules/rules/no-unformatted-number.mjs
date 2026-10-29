/**
 * `no-unformatted-number` (type-aware): a number shown to the reader goes through a
 * formatter of the format module (`formatNumber(value, i18n.language)`, an `Intl.NumberFormat`), so
 * a locale such as de-DE reads "1.234" and "0,5". Any `number`-typed expression (`number |
 * undefined` included) that reaches a render sink (`lib/render-sinks.mjs`: a JSX child, a text
 * prop, an option value of `t()`, text built from those) is flagged, whatever it counts and
 * whatever it is called: a count, a version, a position, an id. The `count` option of `t()` is
 * exempt, because i18next reads it to choose the plural form.
 *
 * Options (all optional): `formatModule` (regular expression source matched against the declaring
 * file of a formatter, default `(?:^|/)utils/format\.ts$`), `moduleName` (the module as the message
 * names it) and `formatters.number` (the formatter the message recommends).
 *
 * Limits. A component prop typed `string | number` (a size, a width, a value) is a quantity for
 * the component to use, so a bare number passed to it is not flagged; the same number turned into
 * text by a template or `String()` is. The rule does not follow the prop into the component to see
 * whether it renders it. A function whose contextual type returns a `ReactNode` is a render
 * callback: what it returns is checked as a child would be.
 */
import { ESLintUtils } from '@typescript-eslint/utils';
import {
  FORMAT_MODULE_SCHEMA,
  admitsNumber,
  formatModuleFacts,
  renderedValues,
  rendersBareNumber,
  sinkExpressions,
} from './lib/render-sinks.mjs';

export const rule = {
  meta: {
    type: 'problem',
    docs: {
      description:
        'A number that reaches the screen goes through a formatter of the format module.',
    },
    schema: [FORMAT_MODULE_SCHEMA],
    messages: {
      raw: 'Format a displayed number with {{formatter}}(value, i18n.language) ({{module}}), not as a raw number.',
    },
  },
  create(context) {
    const services = ESLintUtils.getParserServices(context);
    const checker = services.program.getTypeChecker();
    const facts = formatModuleFacts(context.options[0]);
    const data = { formatter: facts.formatters.number, module: facts.moduleName };
    const reported = new Set();

    function visit(node) {
      const bareRendered = rendersBareNumber(services, node);
      for (const expression of sinkExpressions(services, context.sourceCode, node)) {
        for (const [value, wrapped] of renderedValues(
          services,
          context.sourceCode,
          expression,
          facts.pattern,
        )) {
          if (value.type === 'Literal' || reported.has(value) || (!wrapped && !bareRendered))
            continue;
          const type = checker.getTypeAtLocation(services.esTreeNodeToTSNodeMap.get(value));
          if (!admitsNumber(type)) continue;
          reported.add(value);
          context.report({ node: value, messageId: 'raw', data });
        }
      }
    }

    return {
      JSXExpressionContainer: visit,
      Property: visit,
      ArrowFunctionExpression: visit,
      FunctionExpression: visit,
    };
  },
};
