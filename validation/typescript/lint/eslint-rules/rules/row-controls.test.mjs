// row-action-names-row and decorative-glyph-aria-hidden (type-aware): both are built on
// lib/repetition.mjs and lib/interactive.mjs, so the cases read real component shapes.
import { fileURLToPath } from 'node:url';
import { RuleTester } from '../rule-tester-setup.mjs';
import { rule as rowActionNamesRow } from './row-action-names-row.mjs';
import { rule as decorativeGlyphAriaHidden } from './decorative-glyph-aria-hidden.mjs';

const FIXTURES_DIR = fileURLToPath(new URL('./fixtures/', import.meta.url));

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

const ROW_FIXTURE = `${FIXTURES_DIR}repeat-row.tsx`;
const SAME_FILE_FIXTURE = `${FIXTURES_DIR}typed-fixture.tsx`;
const PRELUDE =
  "import { useState } from 'react';\n" +
  "import type { LibraryButtonProps, LibraryLinkProps } from './library-props';\n" +
  'declare const t: (key: string, options?: object) => string;\n' +
  'declare const rows: { id: string; name: string }[];\n' +
  'declare const tags: Set<{ name: string }>;\n' +
  'declare const open: () => void;\n' +
  'declare function pick(row: unknown): void;\n' +
  // Like a design-system Button: `onClick` is declared by a library base props type, not inline.
  'declare const Button: (props: LibraryButtonProps) => null;\n' +
  'declare const LinkButton: (props: LibraryLinkProps) => null;\n' +
  'declare const canEdit: boolean;\n' +
  'declare const loading: boolean;\n' +
  'declare const query: { data?: { id: string } };\n' +
  'declare const Plain: (props: { children?: unknown }) => null;\n' +
  'declare function run(callback: () => unknown): unknown;\n';

const source = (code, filename = SAME_FILE_FIXTURE) => ({ code: PRELUDE + code, filename });
const sameName = (code, filename) => ({
  ...source(code, filename),
  errors: [{ messageId: 'sameName' }],
});
const glyph = (code) => ({ ...source(code), errors: [{ messageId: 'glyph' }] });

typed.run('local/row-action-names-row', rowActionNamesRow, {
  valid: [
    // Cross-file: `PairRow` is rendered per pair by the on-disk repeat-list.tsx, and names its pair.
    source(
      "export const PairRow = ({ pair }: { pair: { label: string } }) => <Button onClick={open}>{t('x', { pair: pair.label })}</Button>;",
      ROW_FIXTURE,
    ),
    // Not repeated: one instance on the screen.
    source("const el = <Button onClick={open}>{t('save')}</Button>;"),
    // A callback that is not an iteration of a collection.
    source("run(() => <Button onClick={open}>{t('save')}</Button>);"),
    source(
      "rows.map((row) => <Button ariaLabel={t('edit_named', { name: row.name })} onClick={open}>{t('edit')}</Button>);",
    ),
    source(
      "rows.map((row) => <Button onClick={open}>{t('edit_named', { name: row.name })}</Button>);",
    ),
    source("rows.map((row) => <Button aria-describedby={row.id}>{t('edit')}</Button>);"),
    // `t('x', { row: item.name })` carries the row.
    source(
      "const out: unknown[] = [];\nfor (const item of rows) { out.push(<Button onClick={open}>{t('edit', { row: item.name })}</Button>); }",
    ),
    // Derived from the row through a local variable.
    source(
      "rows.map((row) => { const label = t('edit_named', { name: row.name }); return <Button onClick={open}>{label}</Button>; });",
    ),
    source(
      "Array.from(rows, (row) => <LinkButton to={`/x/${row.id}`} ariaLabel={t('open', { row: row.name })}>{t('open_short')}</LinkButton>);",
    ),
    source("rows.map((row) => <form><Button onClick={open}>{t('save')}</Button></form>);"),
    // Not a control: nothing to name.
    source("rows.map((row) => <Plain>{t('static')}</Plain>);"),
    source('rows.map((row) => <li key={row.id}>{row.name}</li>);'),
    // A bare while loop declares no per-item value: nothing is repeated.
    source(
      "const out: unknown[] = [];\nwhile (out.length < 3) { out.push(<Button onClick={open}>{t('edit')}</Button>); }",
    ),
    // A function argument of a call that is not an iteration (`then`'s rejection handler).
    source(
      "declare const job: Promise<string>;\njob.then(open, (e) => <Button onClick={open}>{t('retry')}</Button>);",
    ),
    // A mount behind component state is the dialog shape: one item at a time.
    source(
      "const Dialog = () => <Button onClick={open}>{t('close')}</Button>;\nconst Cell = () => { const [shown] = useState(false); return <>{shown && <Dialog />}</>; };\nrows.map((row) => <Cell key={row.id} />);",
    ),
    // A checkbox named from the row.
    source(
      'rows.map((r) => <input type="checkbox" aria-label={t(\'select_named\', { name: r.name })} onChange={open} />);',
    ),
    // A label reference computed from the row names the control.
    source("rows.map((row) => <Button aria-labelledby={`name-${row.id}`}>{t('edit')}</Button>);"),
    // aria-hidden=\"false\" keeps the control in the tree, and its name is the row's.
    source(
      'rows.map((row) => <Button aria-hidden="false">{t(\'edit_named\', { name: row.name })}</Button>);',
    ),
  ],
  invalid: [
    // Cross-file: the same component, rendered per pair by repeat-list.tsx.
    sameName(
      "export const PairRow = ({ pair }: { pair: { label: string } }) => <Button onClick={open}>{t('x')}</Button>;",
      ROW_FIXTURE,
    ),
    // Same file: a component instantiated inside an iteration, and what it renders in turn.
    sameName(
      "const Inner = () => <Button onClick={open}>{t('x')}</Button>;\nconst Item = () => <Inner />;\nconst el = rows.map((r) => <Item key={r.id} />);",
    ),
    sameName(
      "rows.map((row) => <li key={row.id}><Button onClick={() => pick(row)}>{t('assign')}</Button></li>);",
    ),
    // Iterations that are not `.map`.
    sameName(
      "const out: unknown[] = [];\nfor (const item of rows) { out.push(<Button onClick={() => pick(item)}>{t('edit')}</Button>); }",
    ),
    sameName("Array.from({ length: 3 }, () => <Button onClick={open}>{t('edit')}</Button>);"),
    sameName("rows.flatMap((row) => [<Button onClick={() => pick(row)}>{t('edit')}</Button>]);"),
    sameName("tags.forEach((tag) => { run(() => <Button onClick={open}>{t('edit')}</Button>); });"),
    // Any function-valued on[A-Z] prop is activation, whatever the element.
    sameName("rows.map((row) => <span onDragStart={() => pick(row)}>{t('move')}</span>);"),
    sameName("rows.map((row) => <Plain onSelectRow={() => open()}>{t('pick')}</Plain>);"),
    // A checkbox toggled by onChange is a control, with a constant name.
    sameName(
      'rows.map((r) => <input type="checkbox" aria-label={t(\'select\')} onChange={open} />);',
    ),
    sameName(
      'rows.map((r) => <span role="switch" aria-checked={true} aria-label={t(\'enable\')} />);',
    ),
    // A constant id repeated per row names nothing; aria-hidden=\"false\" does not exempt.
    sameName('rows.map((row) => <Button aria-labelledby="title">{t(\'edit\')}</Button>);'),
    sameName('rows.map((row) => <Button aria-describedby="hint">{t(\'edit\')}</Button>);'),
    sameName('rows.map((row) => <Button aria-hidden="false">{t(\'edit\')}</Button>);'),
    // A condition on props, permissions, query data or loading does not exempt a mounted component.
    sameName(
      "const EditAction = (props: { row: { id: string } }) => <Button onClick={open}>{t('edit')}</Button>;\nrows.map((row) => <li key={row.id}>{canEdit && <EditAction row={row} />}</li>);",
    ),
    sameName(
      "const ActionCell = () => <Button onClick={open}>{t('edit')}</Button>;\nrows.map((row) => <li key={row.id}>{loading ? <span /> : <ActionCell />}</li>);",
    ),
    sameName(
      "const Detail = () => <Button onClick={open}>{t('edit')}</Button>;\nrows.map((row) => <li key={row.id}>{query.data && <Detail />}</li>);",
    ),
    // A control recognised only by the event prop it passes.
    sameName("rows.map((row) => <span onPointerDown={() => pick(row)}>{t('assign')}</span>);"),
    // A control recognised only by its props type declaring onClick.
    sameName("rows.map((row) => <Button>{t('edit')}</Button>);"),
    // A static t() names nothing: only its arguments can carry the row.
    sameName(
      "rows.map((row) => <Button onClick={open} ariaLabel={t('edit')}>{t('edit')}</Button>);",
    ),
    sameName(
      "rows.map((row) => <LinkButton to=\"/x\" ariaLabel={t('edit')}>{t('edit')}</LinkButton>);",
    ),
    sameName('rows.map((row) => <a href="/x">{t(\'open\')}</a>);'),
  ],
});

typed.run('local/decorative-glyph-aria-hidden', decorativeGlyphAriaHidden, {
  valid: [
    source(
      'const el = <LinkButton to="/x"><span aria-hidden="true">→</span> {t("sources")}</LinkButton>;',
    ),
    source('const el = <LinkButton to="/x">{t("sources")}</LinkButton>;'),
    source('const el = <p>a → b</p>;'),
    source('const el = <Plain>• item</Plain>;'),
    source('const el = <Button>“quoted”</Button>;'),
    source('const el = <a href="/x"><span aria-hidden="true">{"»"}</span>{t("next")}</a>;'),
    // A middle dot between words is a separator, not a decoration.
    source('const el = <Button>Name · value</Button>;'),
    source('const el = <Button>{`${t("a")} · ${t("b")}`}</Button>;'),
    // aria-hidden="false" is not hidden, but a hidden ancestor still is.
    source('const el = <Button aria-hidden>→</Button>;'),
  ],
  invalid: [
    glyph('const el = <LinkButton to="/x"><span>→ {t("sources")}</span></LinkButton>;'),
    glyph('const el = <LinkButton to="/x">{t("a")} ›{\' \'}{t("b")}</LinkButton>;'),
    glyph('const el = <Button>{"»"}</Button>;'),
    glyph('const el = <a href="/x">• {t("a")}</a>;'),
    glyph('const el = <span role="button" tabIndex={0}>{`${t("a")} ⟶`}</span>;'),
    glyph('const el = <Plain onClick={open}>← {t("back")}</Plain>;'),
    // Expression children yielding string literals: concatenation, ternary, template.
    glyph('const el = <Button>{t("a") + " →"}</Button>;'),
    glyph('const el = <Button>{open ? "▪" : t("b")}</Button>;'),
    glyph('const el = <Button>{`${t("a")} ▪`}</Button>;'),
    // A middle dot standing alone as a decoration.
    glyph('const el = <Button>· {t("a")}</Button>;'),
    glyph('const el = <Button>{t("a")} {"·"}</Button>;'),
    glyph('const el = <Button aria-hidden="false">▪ {t("a")}</Button>;'),
  ],
});
