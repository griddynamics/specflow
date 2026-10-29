// RuleTester cases of the rules that take project facts as options: each case passes the neutral
// example facts (an `@acme/api-types` package, a `meta.feedback` declaration, a `code` class).
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { RuleTester } from '../rule-tester-setup.mjs';
import { rule as mutationDeclaresFeedback } from './mutation-declares-feedback.mjs';
import { rule as classTranslateNo } from './class-translate-no.mjs';
import { rule as noReexportPackage } from './no-reexport-package.mjs';
import { rule as noHandWrittenWireTypes } from './no-hand-written-wire-types.mjs';
import { rule as noRawMetricRender } from './no-raw-metric-render.mjs';
import { rule as noThresholdOverServedVerdict } from './no-threshold-over-served-verdict.mjs';
import { loadWireTypeNames } from './lib/wire-type-names.mjs';

const FIXTURES_DIR = fileURLToPath(new URL('./fixtures/', import.meta.url));
const TYPED_FIXTURE = `${FIXTURES_DIR}typed-fixture.tsx`;
const WIRE_TYPES = `${FIXTURES_DIR}wire-types/`;

const ts = new RuleTester({
  languageOptions: {
    parser: (await import('typescript-eslint')).parser,
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
});

const typed = new RuleTester({
  languageOptions: {
    parser: (await import('typescript-eslint')).parser,
    parserOptions: {
      ecmaFeatures: { jsx: true },
      project: './tsconfig.json',
      tsconfigRootDir: FIXTURES_DIR,
      disallowAutomaticSingleRunInference: true,
    },
  },
});

// -- no-reexport-package --------------------------------------------------------

const PACKAGE = [{ packages: ['@acme/api-types'] }];
const reexport = (code, names) => ({
  code,
  options: PACKAGE,
  errors: names.map((name) => ({
    messageId: 'reexport',
    data: { name, source: '@acme/api-types' },
  })),
});

ts.run('local/no-reexport-package', noReexportPackage, {
  valid: [
    {
      code: "import type { Order } from '@acme/api-types';\nconst order: Order = {} as Order;",
      options: PACKAGE,
    },
    { code: 'export type LocalType = { id: string };', options: PACKAGE },
    {
      code: "import { useQuery } from '@tanstack/react-query';\nexport { useQuery };",
      options: PACKAGE,
    },
    {
      code: "export * from '@acme/api-types';",
      options: [{ packages: ['@acme/api-types'], ownPaths: ['/api-types/'] }],
      filename: '/repo/api-types/src/index.ts',
    },
  ],
  invalid: [
    reexport("import type { Order } from '@acme/api-types';\nexport type { Order };", ['Order']),
    reexport("import { UserRole } from '@acme/api-types';\nexport { UserRole };", ['UserRole']),
    reexport("export type { Order } from '@acme/api-types';", ['Order']),
    reexport("export { Order, OrderLine } from '@acme/api-types';", ['Order', 'OrderLine']),
    reexport("export * from '@acme/api-types';", ['*']),
  ],
});

// -- no-hand-written-wire-types -------------------------------------------------

test('wire-type names come from the generated directory when it is present', () => {
  const names = loadWireTypeNames({
    root: WIRE_TYPES,
    generatedDir: 'generated',
    openapi: 'openapi.json',
  });
  assert.deepEqual([...names], ['User']);
});

test('wire-type names fall back to the OpenAPI document', () => {
  const names = loadWireTypeNames({
    root: WIRE_TYPES,
    generatedDir: 'absent',
    openapi: 'openapi.json',
  });
  assert.deepEqual([...names].sort(), ['Order', 'User']);
});

test('no source of wire-type names is a hard failure, never an empty set', () => {
  assert.throws(
    () => loadWireTypeNames({ root: WIRE_TYPES, generatedDir: 'absent', openapi: 'absent.json' }),
    /no schema names found/,
  );
});

const WIRE = [{ root: WIRE_TYPES, openapi: 'openapi.json', importFrom: '@acme/api-types' }];
ts.run('local/no-hand-written-wire-types', noHandWrittenWireTypes, {
  valid: [
    { code: 'interface LocalViewModel { id: string; }', options: WIRE },
    { code: 'type SortDirection = "asc" | "desc";', options: WIRE },
  ],
  invalid: [
    {
      code: 'interface Order { id: string; }',
      options: WIRE,
      errors: [
        { messageId: 'duplicate', data: { name: 'Order', from: " from '@acme/api-types'" } },
      ],
    },
    {
      code: 'type User = { id: string };',
      options: WIRE,
      errors: [{ messageId: 'duplicate', data: { name: 'User', from: " from '@acme/api-types'" } }],
    },
  ],
});

// -- no-raw-metric-render -------------------------------------------------------

const METRIC = [{ namePattern: 'score', use: 'RatingBadge' }];
const metric = (code) => ({ code, options: METRIC, filename: TYPED_FIXTURE });

typed.run('local/no-raw-metric-render', noRawMetricRender, {
  valid: [
    metric(
      'declare const item: { score: number; band: "HIGH" | "LOW" };\nconst el = <span>{item.band}</span>;',
    ),
    // Named like a metric but not a number: not reported (type-aware).
    metric('declare const item: { score: "HIGH" | "LOW" };\nconst el = <span>{item.score}</span>;'),
  ],
  invalid: [
    {
      ...metric('declare const item: { score: number };\nconst el = <span>{item.score}</span>;'),
      errors: [{ messageId: 'raw' }],
    },
    {
      ...metric(
        "declare const item: { score: number };\nconst el = <span>{item.score + '%'}</span>;",
      ),
      errors: [{ messageId: 'raw' }],
    },
    {
      ...metric(
        'declare const item: { score: number };\nconst el = <span>{`${item.score}%`}</span>;',
      ),
      errors: [{ messageId: 'raw' }],
    },
  ],
});

// -- no-threshold-over-served-verdict -------------------------------------------

const VERDICT = [{ allowEqualityPairs: [['fromRank', 'toRank']] }];
const verdict = (code) => ({ code, options: VERDICT, filename: TYPED_FIXTURE });
const threshold = [{ messageId: 'threshold' }];

typed.run('local/no-threshold-over-served-verdict', noThresholdOverServedVerdict, {
  valid: [
    // Two recorded positions compared for equality: an allowed pair.
    verdict(
      'declare const move: { fromRank: number; toRank: number; active: boolean };\nconst moved = move.fromRank !== move.toRank;',
    ),
    // The served verdict, read as served.
    verdict('declare const item: { score: number; active: boolean };\nconst off = !item.active;'),
    // A field outside the pattern.
    verdict(
      'declare const item: { depth: number; active: boolean };\nconst nested = item.depth > 0;',
    ),
    // A presence check is not a threshold.
    verdict(
      'declare const item: { rank?: number; active: boolean };\nconst ranked = item.rank != null;',
    ),
    // No boolean on the type: nothing is served to read instead.
    verdict('declare const axis: { score: number; label: string };\nconst wide = axis.score > 0;'),
    // Collection size.
    verdict(
      'declare const items: { score: number; active: boolean }[];\nconst any = items.length > 0;',
    ),
    // Named like a score but not a number.
    verdict(
      'declare const item: { score: "HIGH" | "LOW"; active: boolean };\nconst high = item.score === "HIGH";',
    ),
  ],
  invalid: [
    {
      ...verdict(
        'declare const item: { rank: number; active: boolean };\nconst off = item.rank <= 0;',
      ),
      errors: threshold,
    },
    {
      ...verdict(
        'declare const items: { rank: number; active: boolean }[];\nconst on = items.filter((i) => i.rank > 0);',
      ),
      errors: threshold,
    },
    {
      ...verdict(
        'declare const move: { fromRank: number; toRank: number; active: boolean };\nconst first = move.toRank === 1;',
      ),
      errors: threshold,
    },
    {
      ...verdict(
        'declare const item: { score: number; confirmed: boolean };\nconst strong = item.score >= 60;',
      ),
      errors: threshold,
    },
    {
      // Destructuring is not an escape.
      ...verdict(
        'declare const item: { score: number; active: boolean };\nconst { score } = item;\nconst strong = score > 40;',
      ),
      errors: threshold,
    },
    {
      ...verdict(
        'declare const unit: { confidence: number; validated: boolean };\nconst trusted = unit.confidence > 0.8;',
      ),
      errors: threshold,
    },
  ],
});

// -- class-translate-no -----------------------------------------------------------

const CODE_PRELUDE =
  'declare const item: { code: string };\n' +
  'declare const on: boolean;\n' +
  'declare const extra: string;\n' +
  'declare const id: string;\n' +
  'declare const value: string;\n' +
  'declare const note: string;\n' +
  'declare const rest: { title?: string };\n' +
  'declare const children: unknown;\n' +
  'declare const label: string;\n' +
  'declare const setValue: (next: string) => void;\n' +
  'declare const styles: { code: string; codeNote: string };\n' +
  'declare const clsx: (...parts: unknown[]) => string;\n' +
  'declare const className: string;\n' +
  'declare const Cell: (props: { className?: string; translate?: "yes" | "no"; children?: unknown }) => null;\n' +
  'declare const TextField: (props: { label: string; className?: string; value: string; onChange: (next: string) => void }) => null;\n';
const CODE_CLASS = [{ className: 'code' }];
const codeCase = (code) => ({
  code: `${CODE_PRELUDE}const el = ${code};`,
  options: CODE_CLASS,
  filename: TYPED_FIXTURE,
});
const codeMissing = (code, output) => ({
  ...codeCase(code),
  output: `${CODE_PRELUDE}const el = ${output};`,
  errors: [{ messageId: 'missing' }],
});

typed.run('local/class-translate-no', classTranslateNo, {
  valid: [
    codeCase('<span className="code" translate="no">{item.code}</span>'),
    codeCase('<span className="muted small">{note}</span>'),
    // A class that only starts with the letters is not the token.
    codeCase('<span className="code-note">{note}</span>'),
    codeCase(
      "<span className={on ? 'code' : undefined} translate={on ? 'no' : undefined}>{value}</span>",
    ),
    // A spread may carry `translate`.
    codeCase(
      "<Cell translate=\"no\" className={['code', className].join(' ')} {...rest}>{children}</Cell>",
    ),
    // A component whose props type has no `translate` prop cannot state it.
    codeCase('<TextField label={label} className="code" value={value} onChange={setValue} />'),
    codeCase('<span className={styles.codeNote}>{value}</span>'),
  ],
  invalid: [
    codeMissing(
      '<span className="code">{item.code}</span>',
      '<span className="code" translate="no">{item.code}</span>',
    ),
    codeMissing(
      '<Cell className="code bold">{item.code}</Cell>',
      '<Cell className="code bold" translate="no">{item.code}</Cell>',
    ),
    codeMissing(
      "<span className={on ? 'code' : undefined}>{value}</span>",
      '<span className={on ? \'code\' : undefined} translate="no">{value}</span>',
    ),
    codeMissing(
      '<li className={`code ${extra}`}>{id}</li>',
      '<li className={`code ${extra}`} translate="no">{id}</li>',
    ),
    codeMissing(
      "<span className={clsx('code', extra)}>{id}</span>",
      '<span className={clsx(\'code\', extra)} translate="no">{id}</span>',
    ),
    codeMissing(
      '<span className={clsx({ code: true })}>{id}</span>',
      '<span className={clsx({ code: true })} translate="no">{id}</span>',
    ),
    codeMissing(
      '<span className={styles.code}>{id}</span>',
      '<span className={styles.code} translate="no">{id}</span>',
    ),
  ],
});

// -- mutation-declares-feedback -------------------------------------------------

const FEEDBACK_PRELUDE =
  "import { useMutation, useMutation as useWrite, useQuery } from '@tanstack/react-query';\n" +
  'declare const save: () => Promise<void>;\n' +
  'declare const feedback: { success: string; failure: "inline" | "toast" };\n' +
  'declare function useMutation2(options: { mutationFn: () => Promise<void> }): null;\n';
const feedbackCase = (code) => ({ code: FEEDBACK_PRELUDE + code, filename: TYPED_FIXTURE });
const missing = [{ messageId: 'missing' }];
const incomplete = [{ messageId: 'incomplete' }];

typed.run('local/mutation-declares-feedback', mutationDeclaresFeedback, {
  valid: [
    feedbackCase(
      "export const a = () => useMutation({ meta: { feedback: { success: 'inline', failure: 'inline' } }, mutationFn: save });",
    ),
    feedbackCase('export const a = () => useMutation({ meta: { feedback }, mutationFn: save });'),
    feedbackCase(
      "export const a = () => useWrite({ meta: { feedback: { success: 'none', failure: 'toast' } }, mutationFn: save });",
    ),
    feedbackCase('export const a = () => useMutation2({ mutationFn: save });'),
    feedbackCase('export const a = () => useQuery({ queryKey: ["k"], queryFn: save });'),
    {
      ...feedbackCase(
        "export const a = () => useMutation({ meta: { notify: { done: 'inline' } }, mutationFn: save });",
      ),
      options: [{ metaKey: 'notify', keys: ['done'] }],
    },
  ],
  invalid: [
    {
      ...feedbackCase('export const a = () => useMutation({ mutationFn: save });'),
      errors: missing,
    },
    {
      ...feedbackCase('export const a = () => useMutation({ meta: {}, mutationFn: save });'),
      errors: missing,
    },
    { ...feedbackCase('export const a = () => useWrite({ mutationFn: save });'), errors: missing },
    {
      ...feedbackCase(
        "export const a = () => useMutation({ meta: { feedback: { success: 'inline' } }, mutationFn: save });",
      ),
      errors: incomplete,
    },
    {
      ...feedbackCase(
        'declare const options: { mutationFn: () => Promise<void> };\nexport const a = () => useMutation(options);',
      ),
      errors: missing,
    },
  ],
});
