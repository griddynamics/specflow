import { ESLintUtils } from '@typescript-eslint/utils';
import {
  DEFAULT_FORMAT_MODULE,
  admitsNumber,
  isDeclaredInFormatModule,
  renderedValues,
  sinkExpression,
} from './render-sinks.mjs';
import { typed, SAME_FILE } from './probe-setup.mjs';

/**
 * Reports, for each sink the file has, `<source of the sink expression> => <leaf>[!], …`: the
 * values `renderedValues` reaches, a `!` marking one a template or `String()` turned into text.
 */
const sinkProbe = {
  meta: { type: 'problem', schema: [], messages: {} },
  create(context) {
    const services = ESLintUtils.getParserServices(context);
    const { sourceCode } = context;
    const visit = (node) => {
      const expression = sinkExpression(services, node);
      if (expression === null) return;
      const leaves = [...renderedValues(services, sourceCode, expression)].map(
        ([value, wrapped]) => sourceCode.getText(value) + (wrapped ? '!' : ''),
      );
      context.report({
        node,
        message: `${sourceCode.getText(expression)} => ${leaves.join(', ')}`,
      });
    };
    return { JSXExpressionContainer: visit, Property: visit };
  },
};

const PRELUDE =
  "import { formatNumber } from './src/utils/format';\n" +
  'type Renderable = string | number | null | undefined | { readonly props: object };\n' +
  'declare const t: (key: string, options?: object) => string;\n' +
  'declare const n: number;\n' +
  'declare const maybe: number | undefined;\n' +
  'declare const s: string;\n' +
  'declare const flag: boolean;\n' +
  'declare function Cell(props: { value: Renderable; label: string; size?: number | string; id?: string }): null;\n';

const reports = (code, ...messages) => ({
  code: PRELUDE + code,
  filename: SAME_FILE,
  errors: messages.map((message) => ({ message })),
});

typed.run('lib/render-sinks', sinkProbe, {
  valid: [
    // Nothing reaches the screen.
    { code: PRELUDE + 'const next = n + 1;\nconst key = `row-${n}`;', filename: SAME_FILE },
    // A structural attribute and a non-text intrinsic attribute are not sinks.
    {
      code:
        PRELUDE + 'const el = <div key={n} id={`a-${n}`} data-n={n} tabIndex={n} className={s} />;',
      filename: SAME_FILE,
    },
    // The plural `count` of t() is read by i18next, not shown.
    { code: PRELUDE + "const text = t('rows', { count: n });", filename: SAME_FILE },
  ],
  invalid: [
    reports('const el = <p>{n}</p>;', 'n => n'),
    reports("const el = <p>{flag ? n : 'none'}</p>;", "flag ? n : 'none' => n, 'none'"),
    reports('const el = <p>{maybe ?? s}</p>;', 'maybe ?? s => maybe, s'),
    reports('const el = <p>{`${n} of ${s}`}</p>;', '`${n} of ${s}` => n!, s!'),
    reports("const el = <p>{'rows: ' + String(n)}</p>;", "'rows: ' + String(n) => 'rows: '!, n!"),
    reports('const el = <p>{n.toFixed(2)}</p>;', 'n.toFixed(2) => n!'),
    // A formatter's result is already text, so nothing is rendered from it.
    reports("const el = <p>{formatNumber(n, 'en')}</p>;", "formatNumber(n, 'en') => "),
    // The option values of t(), `count` excepted.
    reports("const text = t('k', { count: n, version: maybe });", 'maybe => maybe'),
    // A text attribute: intrinsic by a short list, a component's by its props type.
    reports('const a = <i aria-label={`${n}`} title={s} data-x={s} />;', '`${n}` => n!', 's => s'),
    reports(
      'const c = <Cell value={n} label={s} size={n} id={s} />;',
      'n => n',
      's => s',
      'n => n',
    ),
    // A text const is followed to what built it; a number is read where it is used.
    reports('const line = `${n} rows`;\nconst el = <p>{line}</p>;', 'line => line, n!'),
  ],
});

/** Reports `<name>:<admitsNumber>` for each declared constant. */
const numberProbe = {
  meta: { type: 'problem', schema: [], messages: {} },
  create(context) {
    const services = ESLintUtils.getParserServices(context);
    const checker = services.program.getTypeChecker();
    return {
      VariableDeclarator(node) {
        if (node.id.type !== 'Identifier') return;
        const type = checker.getTypeAtLocation(services.esTreeNodeToTSNodeMap.get(node.id));
        context.report({ node, message: `${node.id.name}:${String(admitsNumber(type))}` });
      },
    };
  },
};

typed.run('lib/render-sinks admitsNumber', numberProbe, {
  valid: [],
  invalid: [
    {
      code:
        'declare const a: number;\ndeclare const b: number | undefined;\n' +
        'declare const c: string | number;\ndeclare const d: string;\n' +
        'declare const e: string | number | { readonly props: object };',
      filename: SAME_FILE,
      errors: [
        { message: 'a:true' },
        { message: 'b:true' },
        { message: 'c:true' },
        { message: 'd:false' },
        { message: 'e:false' },
      ],
    },
  ],
});

/** Reports whether each call's callee is declared in the default format module. */
const declarationProbe = {
  meta: { type: 'problem', schema: [], messages: {} },
  create(context) {
    const services = ESLintUtils.getParserServices(context);
    return {
      CallExpression(node) {
        context.report({
          node,
          message: String(
            isDeclaredInFormatModule(services, node.callee, new RegExp(DEFAULT_FORMAT_MODULE, 'u')),
          ),
        });
      },
    };
  },
};

typed.run('lib/render-sinks isDeclaredInFormatModule', declarationProbe, {
  valid: [],
  invalid: [
    {
      code:
        "import { formatNumber } from './src/utils/format';\n" +
        'declare function local(n: number): string;\n' +
        "formatNumber(1, 'en');\nlocal(1);",
      filename: SAME_FILE,
      errors: [{ message: 'true' }, { message: 'false' }],
    },
  ],
});
