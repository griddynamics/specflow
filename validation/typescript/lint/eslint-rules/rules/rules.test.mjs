import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { RuleTester } from '../rule-tester-setup.mjs';
import { rule as noQueryDataInState } from './no-query-data-in-state.mjs';
import { rule as noUnsafeModelAssertion } from './no-unsafe-model-assertion.mjs';
import { rule as noDataHookInPresentational } from './no-data-hook-in-presentational.mjs';
import { rule as noDisplayListJoin } from './no-display-list-join.mjs';
import { rule as noStraightQuoteTemplate } from './no-straight-quote-template.mjs';
import { rule as noUnformattedNumber } from './no-unformatted-number.mjs';
import { ariaNameNeedsRole, noImgRoleWithText, imgRequiresDimensions } from './a11y-semantics.mjs';
import { rule as noViewStateInRoute } from './no-view-state-in-route.mjs';
import { rule as noPendingLabelSwap } from './no-pending-label-swap.mjs';

const FIXTURES_DIR = fileURLToPath(new URL('./fixtures/', import.meta.url));
const QUANTITY_FIXTURE = `${FIXTURES_DIR}typed-fixture.tsx`;

// -- no-query-data-in-state (scope analysis, untyped) -----------------------

const jsx = new RuleTester({
  languageOptions: {
    ecmaVersion: 2022,
    sourceType: 'module',
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
});

jsx.run('local/no-query-data-in-state', noQueryDataInState, {
  valid: [
    'const { data } = useQuery(opts);\nfunction View() { return <div>{data?.length}</div>; }',
    'const { data } = useQuery(opts);\nfunction View() { return data ? <div>{data.name}</div> : null; }',
    'const [count, setCount] = useState(0);',
    // Two components in one file, each destructuring a
    // same-named `data` binding from a DIFFERENT source -- only the first is
    // a TanStack Query hook. Real scope/variable tracking (not a file-wide
    // name Set) must not confuse the second component's unrelated `data`
    // with the first's query result.
    'function A() { const { data } = useQuery(opts); return <div>{data?.length}</div>; }\n' +
      'function B() { const { data } = useLocalHelper(); const [state, setState] = useState(data); return state; }',
  ],
  invalid: [
    {
      code: 'const { data } = useQuery(opts);\nconst [state, setState] = useState(data);',
      errors: [{ messageId: 'useState' }],
    },
    {
      code: 'const q = useQuery(opts);\nconst { data } = q;\nconst [state, setState] = useState(data);',
      errors: [{ messageId: 'useState' }],
    },
    {
      code:
        'const { data } = useQuery(opts);\n' +
        'function View() {\n' +
        '  const [state, setState] = useState(null);\n' +
        '  useEffect(() => { setState(data); }, [data]);\n' +
        '  return null;\n' +
        '}',
      errors: [{ messageId: 'setter' }],
    },
    {
      code:
        'const { data } = useQuery(opts);\n' +
        'function View() { return <MyContext.Provider value={data}><Child /></MyContext.Provider>; }',
      errors: [{ messageId: 'contextValue' }],
    },
  ],
});

// -- no-data-hook-in-presentational (scope + JSX shape, untyped) -------------

jsx.run('local/no-data-hook-in-presentational', noDataHookInPresentational, {
  valid: [
    // A pure view: props and markup, no data hook.
    'function ItemView({ item, onPick }) { return <div><p>{item.title}</p></div>; }',
    // A thin container: props and a data hook, but it renders composition only.
    "import { useSaveItem } from '../../api/items';\n" +
      'function ItemCard({ item, listId, onSaved }) {\n' +
      '  const save = useSaveItem(listId);\n' +
      '  return <ItemView item={item} onPick={save.mutate} />;\n' +
      '}',
    // A thin container returning a fragment of components.
    "import { useSaveItem } from '../../api/items';\n" +
      'function ItemCard({ item, listId }) {\n' +
      '  const save = useSaveItem(listId);\n' +
      '  return <><ItemView item={item} /><Toast state={save.status} /></>;\n' +
      '}',
    // A route-level container: no props, so the router gives it its identity.
    "import { useOrders } from '../../api/orders';\n" +
      'function OrdersPage() { const { data } = useOrders(); return <div>{data?.id}</div>; }',
    // The data layer itself: a custom hook may call a data hook.
    "import { useMutation } from '@tanstack/react-query';\n" +
      'export function useSaveItem(listId) { return useMutation({ mutationKey: [listId] }); }',
    // A non-data hook in a leaf is untouched.
    'function ItemView({ item }) { const { t } = useTranslation(); return <p>{t(item.title)}</p>; }',
  ],
  invalid: [
    // The class: a leaf with props, markup and a mutation hook from src/api.
    {
      code:
        "import { useSaveItem } from '../../api/items';\n" +
        'function OptionGrid({ item, listId, onSaved }) {\n' +
        '  const save = useSaveItem(listId);\n' +
        '  return <div><Button onClick={() => save.mutate(item.id)}>Yes</Button></div>;\n' +
        '}',
      errors: [{ messageId: 'presentational' }],
    },
    // Same class by naming convention, without the src/api import.
    {
      code:
        'const ResultPanel = ({ orderId }) => {\n' +
        '  const record = useRecordPaymentMutation(orderId);\n' +
        '  return <section onClick={record.mutate}>done</section>;\n' +
        '};',
      errors: [{ messageId: 'presentational' }],
    },
    // A TanStack hook called straight inside a leaf.
    {
      code:
        "import { useQuery } from '@tanstack/react-query';\n" +
        'function NextItemPanel({ listId }) {\n' +
        '  const { data } = useQuery({ queryKey: [listId] });\n' +
        '  return <aside>{data?.prompt}</aside>;\n' +
        '}',
      errors: [{ messageId: 'presentational' }],
    },
    // A nested leaf inside a module whose top-level component is a container.
    {
      code:
        "import { useMergeEntity } from '../../api/users';\n" +
        'function EntityPicker({ entityId }) {\n' +
        '  const merge = useMergeEntity(entityId);\n' +
        '  return <ul><li onClick={merge.mutate}>merge</li></ul>;\n' +
        '}\n' +
        'function EntityMergeDialog(props) { return <EntityPicker entityId={props.id} />; }',
      errors: [{ messageId: 'presentational' }],
    },
  ],
});

// -- untyped TypeScript tester ---------------------------------------------

const ts = new RuleTester({
  languageOptions: {
    parser: (await import('typescript-eslint')).parser,
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
});

// -- type-aware tester ------------------------------------------------------

const typed = new RuleTester({
  languageOptions: {
    parser: (await import('typescript-eslint')).parser,
    parserOptions: {
      ecmaFeatures: { jsx: true },
      project: './tsconfig.json',
      tsconfigRootDir: FIXTURES_DIR,
      // RuleTester replaces the virtual file's source for every case. In CI,
      // typescript-eslint otherwise infers a one-shot CLI run and reuses the
      // first Program for later cases with the same fixture filename.
      disallowAutomaticSingleRunInference: true,
    },
  },
});

// -- no-unsafe-model-assertion (double assertions / unvalidated model fabrication) --

ts.run('local/no-unsafe-model-assertion', noUnsafeModelAssertion, {
  valid: [
    'const count = 42;',
    'const mode = "FORM" as const;',
    'const id = String(rawId);',
    'const order: OrderInfo = item;',
    'function isOrderInfo(item: unknown): item is OrderInfo { return typeof item === "object" && item !== null; }',
    'const n = x as number;',
    { code: 'const n = <number>x;', filename: 'assertions.ts' },
  ],
  invalid: [
    {
      code: 'const item = <Model><unknown>raw;',
      filename: 'assertions.ts',
      errors: [{ messageId: 'unsafeDoubleCast' }],
    },
    {
      code: 'const item = (<unknown>raw) as Model;',
      filename: 'assertions.ts',
      errors: [{ messageId: 'unsafeDoubleCast' }],
    },
    {
      code: 'const item = <Model>(raw as unknown);',
      filename: 'assertions.ts',
      errors: [{ messageId: 'unsafeDoubleCast' }],
    },
    {
      code: 'const orders = raw as unknown as OrderInfo[];',
      errors: [
        {
          messageId: 'unsafeDoubleCast',
          data: {
            inner: 'unknown',
            target: 'OrderInfo[]',
            baseTarget: 'OrderInfo',
          },
        },
      ],
    },
    {
      code: 'const invoice = raw as unknown as Invoice;',
      errors: [
        {
          messageId: 'unsafeDoubleCast',
          data: {
            inner: 'unknown',
            target: 'Invoice',
            baseTarget: 'Invoice',
          },
        },
      ],
    },
    {
      code: 'const archived = (result.order as unknown as ArchivedOrder) ?? null;',
      errors: [
        {
          messageId: 'unsafeDoubleCast',
          data: {
            inner: 'unknown',
            target: 'ArchivedOrder',
            baseTarget: 'ArchivedOrder',
          },
        },
      ],
    },
    {
      code: 'const item = raw as any as Model;',
      errors: [
        {
          messageId: 'unsafeDoubleCast',
          data: {
            inner: 'any',
            target: 'Model',
            baseTarget: 'Model',
          },
        },
      ],
    },
  ],
});

// -- no-display-list-join (type-aware) ---------------------------------------

const FORMAT_PRELUDE =
  "import { useMemo } from 'react';\n" +
  "import { formatInline, formatList, LIST_INPUT_SEPARATOR } from './src/utils/format';\n" +
  'declare const t: (key: string, options?: object) => string;\n' +
  'declare const navigate: (to: string) => void;\n' +
  'declare const codes: string[];\n' +
  'declare const scopes: readonly string[] | undefined;\n' +
  'declare const ids: number[];\n' +
  'declare const className: string | undefined;\n' +
  'declare function Field(props: { label: string; value: string; hint?: string }): null;\n' +
  'type Node = string | number | null | undefined | { readonly props: object };\n' +
  'declare function Table(props: { columns: { header: string; cell: (row: { codes: string[] }) => Node }[]; render?: (row: { codes: string[] }) => Node }): null;\n' +
  'declare function formatNumber(value: number, language: string): string;\n';

typed.run('local/no-display-list-join', noDisplayListJoin, {
  valid: [
    {
      code: FORMAT_PRELUDE + "const el = <p>{formatList(codes, 'en')}</p>;",
      filename: QUANTITY_FIXTURE,
    },
    {
      code: FORMAT_PRELUDE + 'const el = <p>{formatInline([codes[0] ?? "", codes[1] ?? ""])}</p>;',
      filename: QUANTITY_FIXTURE,
    },
    {
      // An edit field serialises with the separator of its parser.
      code:
        FORMAT_PRELUDE +
        'const el = <Field label="Aliases" value={codes.join(LIST_INPUT_SEPARATOR)} />;',
      filename: QUANTITY_FIXTURE,
    },
    {
      // A route, a key or a class list is not text on the screen.
      code:
        FORMAT_PRELUDE +
        "navigate(['a', 'b'].join('/'));\n" +
        "const key = codes.sort().join(',');\n" +
        "const el = <div id={codes.join('-')} className={[className, 'row'].join(' ')} />;",
      filename: QUANTITY_FIXTURE,
    },
    {
      // A number list is formatted first, then listed.
      code:
        FORMAT_PRELUDE +
        "const el = <p>{formatList(ids.map((id) => formatNumber(id, 'en')), 'en')}</p>;",
      filename: QUANTITY_FIXTURE,
    },
    {
      // A callback that does not return something shown.
      code:
        FORMAT_PRELUDE +
        "const keys = [{ codes }].map((row) => row.codes.join(','));\nnavigate(keys[0] ?? '');",
      filename: QUANTITY_FIXTURE,
    },
    {
      // A `let` assigned twice is not followed.
      code:
        FORMAT_PRELUDE +
        "let line = 'x';\nline = codes.join(', ');\nline = 'y';\nconst el = <p>{line}</p>;",
      filename: QUANTITY_FIXTURE,
    },
    {
      // A join that never reaches a sink.
      code: FORMAT_PRELUDE + "const text = codes.join(', ');\nnavigate(text);",
      filename: QUANTITY_FIXTURE,
    },
  ],
  invalid: [
    {
      // Any element type: the numbers are formatted first.
      code: FORMAT_PRELUDE + "const el = <p aria-label={t('ids', { list: ids.join(', ') })} />;",
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'join' }],
    },
    {
      // Through a useMemo factory.
      code:
        FORMAT_PRELUDE +
        "export function A() { const line = useMemo(() => codes.join(', '), [codes]); return <p>{line}</p>; }",
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'join' }],
    },
    {
      // Through a helper of the same file, declared and as a const arrow.
      code:
        FORMAT_PRELUDE +
        "function describe(values: string[]) { return values.join(' / '); }\n" +
        "const summarise = (values: string[]) => { return values.join(', '); };\n" +
        'const el = <p>{describe(codes)}<b>{summarise(codes)}</b></p>;',
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'join' }, { messageId: 'join' }],
    },
    {
      // A `let` assigned once.
      code:
        FORMAT_PRELUDE + "let line: string;\nline = codes.join(', ');\nconst el = <p>{line}</p>;",
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'join' }],
    },
    {
      // A render callback whose type returns a ReactNode, in a column and as a prop.
      code:
        FORMAT_PRELUDE +
        "const el = <Table columns={[{ header: 'Codes', cell: (row) => row.codes.join(', ') }]} render={(row) => { return row.codes.join(' · '); }} />;",
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'join' }, { messageId: 'join' }],
    },
    {
      code: FORMAT_PRELUDE + "const el = <span>{codes.join(', ')}</span>;",
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'join' }],
    },
    {
      // Whatever the separator.
      code: FORMAT_PRELUDE + "const el = <span>{codes.join(' · ')}</span>;",
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'join' }],
    },
    {
      code: FORMAT_PRELUDE + "const el = <span>{(scopes ?? []).join(' → ') || t('none')}</span>;",
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'join' }],
    },
    {
      code: FORMAT_PRELUDE + "const text = t('adds', { list: codes.join(' · ') });",
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'join' }],
    },
    {
      code: FORMAT_PRELUDE + "const el = <button aria-label={codes.join(' ')} />;",
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'join' }],
    },
    {
      // A text prop of a component, decided by its props type.
      code: FORMAT_PRELUDE + 'const el = <Field label={codes.join(\', \')} value="x" />;',
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'join' }],
    },
    {
      // Through a template and a const.
      code:
        FORMAT_PRELUDE +
        "const joined = codes.map((code) => code.trim()).join(', ');\n" +
        'const line = `Codes: ${joined}`;\n' +
        'const el = <p>{line}</p>;',
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'join' }],
    },
  ],
});

// -- no-straight-quote-template ----------------------------------------------

jsx.run('local/no-straight-quote-template', noStraightQuoteTemplate, {
  valid: [
    "const text = t('users.preview.translation_text', { locale, text });",
    // A CSS attribute selector is a selector, not a quotation.
    'const node = root.querySelector(`[id="${target}"]`);',
    'const node = root.querySelector(`[data-builder-view="${step}"] [data-builder-heading]`);',
    'const text = `${locale} ${text}`;',
    'const text = `He said "hi"`;',
  ],
  invalid: [
    { code: 'const text = `${row.locale} "${row.text}"`;', errors: [{ messageId: 'quote' }] },
    { code: 'const text = `"${value}"`;', errors: [{ messageId: 'quote' }] },
  ],
});

// -- no-unformatted-number (type-aware) --------------------------------------

const PRELUDE =
  "import { formatNumber } from './src/utils/format';\n" +
  'type Renderable = string | number | null | undefined | { readonly props: object };\n' +
  'declare const t: (key: string, options?: object) => string;\n' +
  'declare const rows: string[];\n' +
  'declare const item: { count: number; label: string; version: number; position: number | null };\n' +
  'declare const index: number;\n' +
  'declare function Cell(props: { value: Renderable; size?: number | string; id?: string }): null;\n' +
  'declare function Row(props: { children?: Renderable; label: string }): null;\n' +
  'declare function List(props: { cell: (n: number) => Renderable; size: (n: number) => number }): null;\n';

typed.run('local/no-unformatted-number', noUnformattedNumber, {
  valid: [
    {
      code: PRELUDE + "const el = <li>{formatNumber(item.count, 'en')}</li>;",
      filename: QUANTITY_FIXTURE,
    },
    {
      code: PRELUDE + "const el = <li>{new Intl.NumberFormat('en').format(item.count)}</li>;",
      filename: QUANTITY_FIXTURE,
    },
    {
      // Text is not a number, whatever it is called.
      code: PRELUDE + 'const el = <li>{item.label}</li>;',
      filename: QUANTITY_FIXTURE,
    },
    {
      // `count` is the plural selector of t(): it stays a number.
      code: PRELUDE + "const text = t('rows', { count: rows.length });",
      filename: QUANTITY_FIXTURE,
    },
    {
      code: PRELUDE + "const text = t('rows', { total: formatNumber(rows.length, 'en') });",
      filename: QUANTITY_FIXTURE,
    },
    {
      // A formatted number inside a template.
      code: PRELUDE + "const el = <li aria-label={`${formatNumber(index + 1, 'en')} rows`} />;",
      filename: QUANTITY_FIXTURE,
    },
    {
      // A key, a size and an identifier are not text, by the props type or the attribute.
      code:
        PRELUDE +
        'const el = <Cell key={index} value="x" size={index} id={`cell-${index}`} />;\n' +
        'const div = <div data-index={index} tabIndex={index} style={{ width: index }} />;',
      filename: QUANTITY_FIXTURE,
    },
    {
      // A number that is only computed, never shown.
      code:
        PRELUDE + 'const next = item.version + 1;\nconst ok = item.count > 0 && rows.length > 1;',
      filename: QUANTITY_FIXTURE,
    },
    {
      // A string-only prop receives a string.
      code:
        PRELUDE + "const el = <Row label={`${rows[0]}`}>{rows.length > 0 ? 'many' : 'none'}</Row>;",
      filename: QUANTITY_FIXTURE,
    },
  ],
  invalid: [
    {
      code: PRELUDE + 'const el = <li>{item.count}</li>;',
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'raw' }],
    },
    {
      // Any number: a version, a position, an ordinal.
      code: PRELUDE + 'const el = <li>v{item.version} #{index + 1}</li>;',
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'raw' }, { messageId: 'raw' }],
    },
    {
      code: PRELUDE + "const el = <li>{item.position ?? '—'}</li>;",
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'raw' }],
    },
    {
      code: PRELUDE + 'const el = <li>{rows.length ?? 0}</li>;',
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'raw' }],
    },
    {
      code: PRELUDE + "const text = t('paging', { total: rows.length, version: item.version });",
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'raw' }, { messageId: 'raw' }],
    },
    {
      code: PRELUDE + "const el = <p>{t('label') + ' ' + item.count.toString()}</p>;",
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'raw' }],
    },
    {
      code: PRELUDE + "const el = <li title={t('label') + ' ' + String(item.count)} />;",
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'raw' }],
    },
    {
      // A locale-less formatter is not the formatter.
      code: PRELUDE + 'const el = <li>{item.count.toLocaleString()}</li>;',
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'raw' }],
    },
    {
      // A text attribute of an element and a renderable prop of a component.
      code:
        PRELUDE +
        'const el = <input placeholder={`${item.count}`} />;\n' +
        'const cell = <Cell value={item.count} />;',
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'raw' }, { messageId: 'raw' }],
    },
    {
      // A render callback returns a number to the screen; a callback that returns a quantity does not.
      code: PRELUDE + 'const el = <List cell={(n) => n + 1} size={(n) => n * 2} />;',
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'raw' }],
    },
    {
      // A number reaching a text prop through a template and a const.
      code:
        PRELUDE +
        'const line = `${item.count} rows`;\n' +
        'const el = <Row label={line}>{line}</Row>;',
      filename: QUANTITY_FIXTURE,
      errors: [{ messageId: 'raw' }],
    },
  ],
});

// -- a11y-semantics (JSX structure, untyped) ----------------------------------

const a11y = new RuleTester({
  languageOptions: {
    ecmaVersion: 2022,
    sourceType: 'module',
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
});

a11y.run('local/aria-name-needs-role', ariaNameNeedsRole, {
  valid: [
    '<div role="group" aria-label={t("users.scope.bar_label")}>x</div>',
    '<section aria-label="Cited by">x</section>',
    '<div className="plain">x</div>',
    '<div {...rest} aria-label="x">x</div>',
  ],
  invalid: [
    {
      code: '<div className="edit-form" aria-label={t("users.edit.form_label")}>x</div>',
      errors: [{ messageId: 'needsRole' }],
    },
    { code: '<span aria-labelledby="h">x</span>', errors: [{ messageId: 'needsRole' }] },
  ],
});

a11y.run('local/no-img-role-with-text', noImgRoleWithText, {
  valid: [
    '<div role="group" aria-label="Zones"><span>Low</span></div>',
    '<span role="img" aria-label="warning">⚠</span>',
    '<div role="img" aria-label="chart" />',
  ],
  invalid: [
    {
      code: '<div role="img" aria-label="Zones">Normal</div>',
      errors: [{ messageId: 'hidesText' }],
    },
    {
      code: '<div role="img" aria-label="Zones">{t("zone_low")}</div>',
      errors: [{ messageId: 'hidesText' }],
    },
  ],
});

a11y.run('local/img-requires-dimensions', imgRequiresDimensions, {
  valid: [
    '<img src={src} alt="" width={w} height={h} />',
    '<img {...props} />',
    '<Image src={s} />',
  ],
  invalid: [
    { code: '<img src={src} alt={alt} className="x" />', errors: [{ messageId: 'dimensions' }] },
    { code: '<img src={src} width={10} />', errors: [{ messageId: 'dimensions' }] },
  ],
});

// -- no-view-state-in-route (type-aware, route tree + data flow) -------------

const ROUTER = '/fixtures/view-routes.tsx';
const viewPage = `${FIXTURES_DIR}view-page.tsx`;
const viewChild = `${FIXTURES_DIR}view-child.tsx`;
const viewOverlay = `${FIXTURES_DIR}view-overlay.tsx`;
const viewLazy = `${FIXTURES_DIR}view-lazy.tsx`;
const viewOrphan = `${FIXTURES_DIR}view-orphan.tsx`;
const VIEW_PRELUDE =
  "import { useState } from 'react';\n" +
  'declare const Switcher: (props: { current: string; onChange: (value: string) => void }) => null;\n' +
  'declare const Narrow: (props: { chosen: string; onNarrow: (value: string) => void }) => null;\n' +
  'declare const Field: (props: { text: string; onText: (value: string) => void }) => null;\n' +
  'declare const Reveal: (props: { shown: boolean; onToggle: () => void }) => null;\n' +
  'declare const PaneA: () => null;\n' +
  'declare const PaneB: () => null;\n' +
  'declare const PANES: Record<string, () => null>;\n' +
  'declare const ROWS: readonly string[];\n' +
  'declare const search: { view?: string };\n';
// The typed project keeps the last source a case gave a file, so a case about a component that a
// route reaches through view-page.tsx first puts the page back as written on disk.
const PAGE_AS_WRITTEN = readFileSync(viewPage, 'utf8');
const view = (code, filename = viewPage) => ({
  code: VIEW_PRELUDE + code,
  filename,
  options: [{ router: ROUTER }],
});
// A route page whose Overlay mounts behind a permission: a prop, not component state.
const PERMISSION_PAGE =
  "import { Child } from './view-child';\nimport { Overlay } from './view-overlay';\n" +
  'export function Page({ canEdit }: { canEdit: boolean }) { return <div><Child />{canEdit && <Overlay />}</div>; }';
const OVERLAY_WITH_VIEW =
  'export function Overlay() { const [mode, setMode] = useState("a"); return <><Switcher current={mode} onChange={setMode} />{mode === "a" ? <PaneA /> : <PaneB />}</>; }';
const stateBound = (element, name) => ({ messageId: 'stateBound', data: { element, name } });

typed.run('local/no-view-state-in-route', noViewStateInRoute, {
  valid: [
    // The address owns the view: the value is read from the route search, nothing is held.
    view(
      'export function Page() { return <><Switcher current={search.view ?? "a"} onChange={() => undefined} />{search.view === "a" ? <PaneA /> : <PaneB />}</>; }',
    ),
    // A form draft feeds a controlled field but selects no content.
    view(
      'export function Page() { const [text, setText] = useState(""); return <Field text={text} onText={setText} />; }',
    ),
    // A draft whose value only feeds a handler, not a rendered choice.
    view(
      'export function Page() { const [text, setText] = useState(""); return <><Field text={text} onText={(v) => { setText(v); }} /><PaneA /></>; }',
    ),
    { code: PAGE_AS_WRITTEN, filename: viewPage, options: [{ router: ROUTER }] },
    // State held by a dialog: mounted behind a condition on the route's screen, so out of scope.
    view(
      'export function Overlay() { const [mode, setMode] = useState("a"); return <><Switcher current={mode} onChange={setMode} />{mode === "a" ? <PaneA /> : <PaneB />}</>; }',
      viewOverlay,
    ),
    // A component on no route is out of scope.
    view(
      'export function Orphan() { const [mode, setMode] = useState("a"); return <><Switcher current={mode} onChange={setMode} />{mode === "a" ? <PaneA /> : <PaneB />}</>; }',
      viewOrphan,
    ),
    // A disclosure: a boolean that mounts one optional element does not choose a view.
    view(
      'export function Page() { const [shown, setShown] = useState(false); return <><Reveal shown={shown} onToggle={() => { setShown(!shown); }} />{shown && <PaneA />}</>; }',
    ),
    // A selected item: the state picks data by id (`find`), which is not a choice of view.
    view(
      'export function Page() { const [id, setId] = useState<string | null>(null); const item = ROWS.find((row) => row === id); return <><Narrow chosen={id ?? ""} onNarrow={setId} />{item ? <PaneA /> : <PaneB />}</>; }',
    ),
    // A draft compared with its baseline says whether it differs; it does not choose a view.
    view(
      'export function Page() { const [draft, setDraft] = useState(""); const baseline = search.view; const dirty = draft !== baseline; return <><Field text={draft} onText={setDraft} />{dirty ? <PaneA /> : <PaneB />}</>; }',
    ),
    // Text computed from the state is not the state handed to a child.
    view(
      'function Body(props: { tab: string }) { return props.tab === "a" ? <PaneA /> : <PaneB />; }\nexport function Page() { const [tab, setTab] = useState("a"); return <><Switcher current={tab} onChange={setTab} /><Body tab={tab === "a" ? "x" : "y"} /></>; }',
    ),
    // The value goes to a child, but the child does not choose content by it.
    view(
      'function Body(props: { tab: string }) { return <p>{props.tab}</p>; }\nexport function Page() { const [tab, setTab] = useState("a"); return <><Switcher current={tab} onChange={setTab} /><Body tab={tab} /></>; }',
    ),
    // One optional element chosen by the state is a disclosure, not a choice between renderings.
    view(
      'export function Page() { const [chosen, setChosen] = useState<string | null>(null); return <><Narrow chosen={chosen ?? ""} onNarrow={setChosen} />{chosen !== null && <PaneA />}</>; }',
    ),
    // The element the state mounts is the one it feeds: an overlay or an editor of its own.
    view(
      'export function Page() { const [item, setItem] = useState<string | null>(null); return <>{item === "a" && <Narrow chosen={item} onNarrow={setItem} />}{item === "b" && <Switcher current={item} onChange={setItem} />}</>; }',
    ),
    // State that selects content, with no control of it fed the value.
    view(
      'export function Page() { const [mode] = useState("a"); return mode === "a" ? <PaneA /> : <PaneB />; }',
    ),
    // The next cases put Overlay behind a condition that is not component state.
    view(PERMISSION_PAGE),
  ],
  invalid: [
    {
      // Mounted behind a permission (a prop), not component state: the overlay is on the screen.
      ...view(OVERLAY_WITH_VIEW, viewOverlay),
      errors: [stateBound('Switcher', 'mode')],
    },
    {
      // A boolean that chooses between two renderings is view state, as any other.
      ...view(
        'export function Page() { const [grid, setGrid] = useState(true); return <><Reveal shown={grid} onToggle={() => { setGrid(!grid); }} />{grid ? <PaneA /> : <PaneB />}</>; }',
      ),
      errors: [stateBound('Reveal', 'grid')],
    },
    {
      // A comparison of the state against a constant, held in a const, is the discriminant.
      ...view(
        'export function Page() { const [mode, setMode] = useState("a"); const grid = mode === "grid"; return <><Switcher current={mode} onChange={setMode} />{grid ? <PaneA /> : <PaneB />}</>; }',
      ),
      errors: [stateBound('Switcher', 'mode')],
    },
    {
      // The value follows into a child's prop, and the child chooses its content by it.
      ...view(
        'function Body(props: { tab: string }) { return props.tab === "a" ? <PaneA /> : <PaneB />; }\nexport function Page() { const [tab, setTab] = useState("a"); return <><Switcher current={tab} onChange={setTab} /><Body tab={tab} /></>; }',
      ),
      errors: [stateBound('Switcher', 'tab')],
    },
    {
      // A destructured prop and a lookup in the child.
      ...view(
        'function Body({ which }: { which: string }) { const Pane = PANES[which]; return <Pane />; }\nexport function Page() { const [tab, setTab] = useState("a"); return <><Switcher current={tab} onChange={setTab} /><Body which={tab} /></>; }',
      ),
      errors: [stateBound('Switcher', 'tab')],
    },
    {
      // Transitively: the child hands the prop on to a component that chooses by it.
      ...view(
        'function Inner({ mode }: { mode: string }) { switch (mode) { case "a": return <PaneA />; default: return <PaneB />; } }\nfunction Body({ tab }: { tab: string }) { return <Inner mode={tab} />; }\nexport function Page() { const [tab, setTab] = useState("a"); return <><Switcher current={tab} onChange={setTab} /><Body tab={tab} /></>; }',
      ),
      errors: [stateBound('Switcher', 'tab')],
    },
    {
      // A tab-like control with arbitrary names: the setter and the value reach one element.
      ...view(
        'export function Page() { const [mode, setMode] = useState("a"); return <><Switcher current={mode} onChange={setMode} />{mode === "a" ? <PaneA /> : <PaneB />}</>; }',
      ),
      errors: [stateBound('Switcher', 'mode')],
    },
    {
      // A filter-like control: a closure calls the setter; two `&&` renderings are chosen.
      ...view(
        'export function Page() { const [kind, setKind] = useState("all"); return <><Narrow chosen={kind} onNarrow={(v) => { setKind(v); }} />{kind === "all" && <PaneA />}{kind === "some" && <PaneB />}</>; }',
      ),
      errors: [stateBound('Narrow', 'kind')],
    },
    {
      // The shown value is derived from the state; a switch chooses the content.
      ...view(
        'export function Page() {\n  const [picked, setPicked] = useState<string | null>(null);\n  const shown = picked ?? "a";\n  const pick = (v: string) => { setPicked(v); };\n  let body = <PaneA />;\n  switch (shown) {\n    case "a":\n      body = <PaneA />;\n      break;\n    default:\n      body = <PaneB />;\n  }\n  return <><Switcher current={shown} onChange={pick} />{body}</>;\n}',
      ),
      errors: [stateBound('Switcher', 'picked')],
    },
    {
      // A lookup yields the component rendered next.
      ...view(
        'export function Page() { const [view, setView] = useState("a"); const Pane = PANES[view]; return <><Switcher current={view} onChange={setView} /><Pane /></>; }',
      ),
      errors: [stateBound('Switcher', 'view')],
    },
    {
      // The page as written, now with a view of its own; it renders `Child` below.
      code:
        VIEW_PRELUDE +
        PAGE_AS_WRITTEN.replace("import { useState } from 'react';\n", '')
          .replace('const [open] = useState(false);', 'const [mode, setMode] = useState("a");')
          .replace('{open && <Overlay />}', '{mode === "a" ? <PaneA /> : <PaneB />}')
          .replace('<Child />', '<Child /><Switcher current={mode} onChange={setMode} />'),
      filename: viewPage,
      options: [{ router: ROUTER }],
      errors: [stateBound('Switcher', 'mode')],
    },
    {
      // A component the route component renders unconditionally, in another file.
      ...view(
        'export function Child() { const [mode, setMode] = useState("a"); return <><Switcher current={mode} onChange={setMode} />{mode === "a" ? <PaneA /> : <PaneB />}</>; }',
        viewChild,
      ),
      errors: [stateBound('Switcher', 'mode')],
    },
    {
      // A route component loaded with lazy(() => import(...)).
      ...view(
        'export function LazyPage() { const [mode, setMode] = useState("a"); return <><Switcher current={mode} onChange={setMode} />{mode === "a" ? <PaneA /> : <PaneB />}</>; }',
        viewLazy,
      ),
      errors: [stateBound('Switcher', 'mode')],
    },
  ],
});

// -- no-pending-label-swap (type-aware, type origin of pending state) -------

const PENDING_PRELUDE =
  "import { useIsMutating, useMutation, useQuery } from '@tanstack/react-query';\n" +
  'declare const Button: (props: { onClick?: () => void; loading?: boolean; children?: unknown }) => null;\n' +
  'declare const Footer: (props: { confirmLabel: string; loading?: boolean }) => null;\n' +
  'declare const Hint: (props: { label: string }) => null;\n' +
  'declare const t: (key: string) => string;\n' +
  'declare const save: () => Promise<void>;\n' +
  'declare const editing: boolean;\n' +
  'declare const unrelated: { isPending: boolean };\n';
const pending = (code) => ({
  code: PENDING_PRELUDE + code,
  filename: QUANTITY_FIXTURE,
});
const swap = { messageId: 'swap' };

typed.run('local/no-pending-label-swap', noPendingLabelSwap, {
  valid: [
    // The fixed shape: the label stays and the control takes `loading`.
    pending(
      'export function A() { const m = useMutation({ mutationFn: save }); return <Button loading={m.isPending}>{t("a.save")}</Button>; }',
    ),
    pending(
      'export function A() { const m = useMutation({ mutationFn: save }); return <Footer confirmLabel={t("a.save")} loading={m.isPending} />; }',
    ),
    // A ternary on something that is not request state.
    pending(
      'export function A() { return <Button>{editing ? t("a.save") : t("a.edit")}</Button>; }',
    ),
    // The outcome flags and statuses of a result are not pending state.
    pending(
      'export function A() { const q = useQuery({ queryKey: ["k"], queryFn: save }); return <Button>{q.isError ? t("a.retry") : t("a.go")}</Button>; }',
    ),
    pending(
      'export function A() { const m = useMutation({ mutationFn: save }); return <Button>{m.isSuccess ? t("a.again") : t("a.go")}</Button>; }',
    ),
    pending(
      'export function A() { const q = useQuery({ queryKey: ["k"], queryFn: save }); return <Button>{q.isStale ? t("a.refresh") : t("a.go")}</Button>; }',
    ),
    pending(
      'export function A() { const m = useMutation({ mutationFn: save }); return <Button>{m.status === "error" ? t("a.retry") : t("a.go")}</Button>; }',
    ),
    // `isPending` that is not a react-query result property is no pending state.
    pending(
      'export function A() { return <Button>{unrelated.isPending ? t("a.save") : t("a.edit")}</Button>; }',
    ),
    // Pending state choosing something that is not a label.
    pending(
      'export function A() { const m = useMutation({ mutationFn: save }); return <span data-state={m.isPending ? "busy" : "idle"} />; }',
    ),
    // Same text in both branches, or a branch that is not text.
    pending(
      'export function A() { const m = useMutation({ mutationFn: save }); return <Button>{m.isPending ? t("a.save") : t("a.save")}</Button>; }',
    ),
    pending(
      'export function A() { const m = useMutation({ mutationFn: save }); return <Button>{m.isPending ? "" : t("a.save")}</Button>; }',
    ),
    // A flag that depends on a request but is not itself pending state.
    pending(
      'export function A() { const q = useQuery({ queryKey: ["k"], queryFn: save }); const empty = !q.isLoading && q.data === undefined; return <Button>{empty ? t("a.add") : t("a.edit")}</Button>; }',
    ),
    // Plain text that is not a control's name nor a label prop.
    pending(
      'export function A() { const m = useMutation({ mutationFn: save }); return <p>{m.isPending ? t("a.saving") : t("a.saved")}</p>; }',
    ),
  ],
  invalid: [
    {
      // A react-query mutation result read as a member, in a control's children.
      ...pending(
        'export function A() { const m = useMutation({ mutationFn: save }); return <Button>{m.isPending ? "Working" : "Go"}</Button>; }',
      ),
      errors: [swap],
    },
    {
      // Destructured from a query result, in a label-typed prop of a component.
      ...pending(
        'export function A() { const { isFetching } = useQuery({ queryKey: ["k"], queryFn: save }); return <Footer confirmLabel={isFetching ? t("a.busy") : t("a.go")} />; }',
      ),
      errors: [swap],
    },
    {
      // A const made of pending state only; the label is built in a template.
      ...pending(
        'export function A() { const a = useMutation({ mutationFn: save }); const b = useMutation({ mutationFn: save }); const busy = a.isPending || b.isPending; return <Button>{`${busy ? t("a.wait") : t("a.go")}`}</Button>; }',
      ),
      errors: [swap],
    },
    {
      // A label held in a const and read as the control's name; nested in an element of the control.
      ...pending(
        'export function A() { const m = useMutation({ mutationFn: save }); const label = m.isPending ? t("a.wait") : t("a.go"); return <Button onClick={() => undefined}><b>{label}</b></Button>; }',
      ),
      errors: [swap],
    },
    {
      // The status of a result compared with the pending value.
      ...pending(
        'export function A() { const m = useMutation({ mutationFn: save }); return <Button>{m.status === "pending" ? t("a.wait") : t("a.go")}</Button>; }',
      ),
      errors: [swap],
    },
    {
      // fetchStatus, from a destructured result, negated.
      ...pending(
        'export function A() { const { fetchStatus } = useQuery({ queryKey: ["k"], queryFn: save }); return <Footer confirmLabel={fetchStatus !== "fetching" ? t("a.go") : t("a.wait")} />; }',
      ),
      errors: [swap],
    },
    {
      // The pending state of a query, held in a const.
      ...pending(
        'export function A() { const q = useQuery({ queryKey: ["k"], queryFn: save }); const busy = q.status === "pending"; return <Button>{busy ? t("a.wait") : t("a.go")}</Button>; }',
      ),
      errors: [swap],
    },
    {
      // The count of in-flight mutations of the library.
      ...pending(
        'export function A() { const n = useIsMutating(); return <Button>{n > 0 ? t("a.wait") : t("a.go")}</Button>; }',
      ),
      errors: [swap],
    },
    {
      // A component prop that the component passes to `loading` is its pending flag.
      ...pending(
        'export function A(props: { working: boolean }) { return <Button loading={props.working}>{props.working ? t("a.wait") : t("a.go")}</Button>; }',
      ),
      errors: [swap],
    },
    {
      // The same, destructured; the swap sits in a label-typed prop.
      ...pending(
        'export function A({ working }: { working: boolean }) { return <><Button loading={working}>{t("a.go")}</Button><Hint label={working ? "Wait" : "Go"} /></>; }',
      ),
      errors: [swap],
    },
    {
      // A control's accessible-name attribute.
      ...pending(
        'export function A() { const m = useMutation({ mutationFn: save }); return <Button aria-label={m.isPending ? t("a.wait") : t("a.go")} onClick={() => undefined} />; }',
      ),
      errors: [swap],
    },
  ],
});
