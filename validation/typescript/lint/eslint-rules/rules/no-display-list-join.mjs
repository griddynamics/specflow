/**
 * `no-display-list-join` (type-aware): a list shown to the reader is built by a list formatter of
 * the format module (`Intl.ListFormat` with the reader's language), and a dot-separated metadata
 * line by an inline formatter, so a separator lives in one place. A `.join(sep)` on a string array
 * whose result reaches a render sink (`lib/render-sinks.mjs`) is flagged whatever the separator.
 * An edit field serialises with a separator constant declared in the format module, the other half
 * of its parser's contract: a join by a separator declared there is not display text.
 *
 * Options (all optional): `formatModule` (regular expression source matched against the declaring
 * file, default `(?:^|/)utils/format\.ts$`), `moduleName` (the module as the message names it),
 * `formatters.list` and `formatters.inline` (the formatters the message recommends).
 */
import { ESLintUtils } from '@typescript-eslint/utils';
import {
  FORMAT_MODULE_SCHEMA,
  formatModuleFacts,
  isDeclaredInFormatModule,
  renderedValues,
  sinkExpressions,
} from './lib/render-sinks.mjs';

export const rule = {
  meta: {
    type: 'problem',
    docs: {
      description:
        'A displayed list is built with a list or inline formatter of the format module, not .join(sep).',
    },
    schema: [FORMAT_MODULE_SCHEMA],
    messages: {
      join: 'Build a displayed list with {{list}}(values, i18n.language) or a metadata line with {{inline}}(values) ({{module}}), not .join(separator); an edit field uses a separator declared in {{module}}.',
    },
  },
  create(context) {
    const services = ESLintUtils.getParserServices(context);
    const checker = services.program.getTypeChecker();
    const facts = formatModuleFacts(context.options[0]);
    const data = {
      list: facts.formatters.list,
      inline: facts.formatters.inline,
      module: facts.moduleName,
    };
    const reported = new Set();

    const isArray = (type) => checker.isArrayType(type) || checker.isTupleType(type);

    /** `values.join(separator)` on an array, by a separator that is display text. */
    function isDisplayJoin(value) {
      if (value.type !== 'CallExpression') return false;
      const { callee } = value;
      if (
        callee.type !== 'MemberExpression' ||
        callee.computed ||
        callee.property.type !== 'Identifier' ||
        callee.property.name !== 'join'
      ) {
        return false;
      }
      const [separator] = value.arguments;
      if (separator !== undefined && isDeclaredInFormatModule(services, separator, facts.pattern)) {
        return false;
      }
      const array = checker.getNonNullableType(
        checker.getTypeAtLocation(services.esTreeNodeToTSNodeMap.get(callee.object)),
      );
      return (array.isUnion() ? array.types : [array]).some(isArray);
    }

    function visit(node) {
      for (const expression of sinkExpressions(services, context.sourceCode, node)) {
        for (const [value] of renderedValues(
          services,
          context.sourceCode,
          expression,
          facts.pattern,
        )) {
          if (reported.has(value) || !isDisplayJoin(value)) continue;
          reported.add(value);
          context.report({ node: value, messageId: 'join', data });
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
