/**
 * Self-test of the kit's configs. ESLint: the effective config per file scope (severity and the
 * resolved option arrays of the `restrict/*` groups), the presets and the options that switch
 * project-fact groups on, and real findings on a scratch project. tsconfig.base.json and
 * tsconfig.library.json: `tsc` refuses what a strict flag forbids. knip.json and .prettierrc:
 * valid and complete.
 */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { ESLint, Linter } from 'eslint';
import tseslint from 'typescript-eslint';

import { optionsOf, selectorsOf, severityOf } from './eslint-rules/config-helpers.mjs';
import {
  RESTRICT_GROUPS,
  SEL_COMPONENTS,
  SEL_DISPLAY_FORMAT,
  SEL_E2E_FIXTURE_IMPORT,
  SEL_INPUT_PRESERVATION,
  SEL_LOCALE_PARSE,
  SEL_QUERY_KEY,
  SEL_REQUIRED_MARKER,
  SEL_SINGLE_SCROLLER,
  SEL_TESTS,
  SEL_TEST_PROBES,
  defineKitConfig,
  selFormGrid,
  selLazyPages,
  selTypeOnlyImports,
  selVariantReskin,
  selViewport,
  selWrapped,
} from './eslint.config.mjs';
import { defineRestrictPlugin } from './eslint-rules/restrict.mjs';

const KIT = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

let root;
let eslint;
let designed;

const FILES = {
  'tsconfig.json': JSON.stringify({
    compilerOptions: {
      strict: true,
      target: 'ESNext',
      module: 'ESNext',
      moduleResolution: 'bundler',
      jsx: 'react-jsx',
      lib: ['ESNext', 'DOM'],
      noEmit: true,
      skipLibCheck: true,
    },
    include: ['src', 'e2e', 'lib'],
  }),
  'package.json': JSON.stringify({ name: 'sample', private: true, type: 'module' }),
  'src/components/widget.tsx': [
    'export enum Size {',
    '  Small,',
    '}',
    '// eslint-disable-next-line no-console',
    'export async function load(raw: unknown): Promise<number[]> {',
    "  const response = await fetch('/items');",
    '  const items = (await response.json()) as unknown as number[];',
    '  return raw === null ? [] : items;',
    '}',
    '',
  ].join('\n'),
  'src/components/primitives/field.tsx': 'export const Field = (): null => null;\n',
  'src/pages/orders-page.tsx': 'export const OrdersPage = (): null => null;\n',
  'src/utils/numbers.ts': [
    'export function parse(text: string): number {',
    '  return parseFloat(text);',
    '}',
    '',
  ].join('\n'),
  'src/utils/format.ts': 'export const join = (a: string, b: string): string => `${a} · ${b}`;\n',
  'src/hooks/use-breakpoint.ts': 'export const useBreakpoint = (): boolean => false;\n',
  'src/api/client.ts': 'export const get = (url: string): Promise<Response> => fetch(url);\n',
  'src/utils/storage.ts':
    'export const read = (key: string): string | null => localStorage.getItem(key);\n',
  'src/items-query-keys.ts': "export const itemKeys = { all: ['items'] as const };\n",
  'src/router.tsx': 'export const routes = [];\n',
  'src/components/widget.test.tsx': "it.only('works', () => undefined);\n",
  'e2e/fixtures.ts': "export { test, expect } from '@playwright/test';\n",
  'e2e/items.spec.ts': "import { test } from '@playwright/test';\ntest('x', () => undefined);\n",
  'lib/src/index.ts': "export type { Order } from './order';\n",
  'lib/src/order.ts': 'export interface Order {\n  id: string;\n}\n',
  'tools/build.mjs': "console.log('build');\n",
};

const DESIGN_SYSTEM = {
  breakpoints: { widths: [640, 1024], hook: 'src/hooks/use-breakpoint.ts' },
  lazyPages: { importPattern: '^\\.\\/pages\\/', eager: ['error-page'] },
  formGridClass: 'form-grid',
  variantComponents: ['Button'],
  requiredMarker: true,
  singleScroller: { exempt: ['src/components/primitives/modal.tsx'] },
  pageButtonClass: true,
  pageMargins: [16, 24],
  formatModule: 'src/utils/format.ts',
  wrappedElements: [{ element: 'details', use: 'Disclosure' }],
  wrappedImports: [{ source: 'acme-ui', names: ['Badge'], use: 'StatusPill' }],
  capsLabelClass: 'label-caps',
  containmentClass: 'long-list',
  translateNoClass: 'code',
};

before(() => {
  root = mkdtempSync(path.join(os.tmpdir(), 'ts-lint-kit-'));
  for (const [file, text] of Object.entries(FILES)) {
    mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    writeFileSync(path.join(root, file), text);
  }
  eslint = new ESLint({
    cwd: root,
    overrideConfigFile: true,
    overrideConfig: defineKitConfig({ root }),
    allowInlineConfig: false,
  });
  designed = new ESLint({
    cwd: root,
    overrideConfigFile: true,
    overrideConfig: defineKitConfig({
      root,
      app: {
        typeOnlyPackages: ['@acme/api-types'],
        mutationFeedback: {},
        rawMetric: { namePattern: 'score' },
        servedVerdict: {},
        designSystem: DESIGN_SYSTEM,
      },
      library: { files: ['lib/src/**/*.ts'], publicEntry: ['lib/src/index.ts'] },
    }),
    allowInlineConfig: false,
  });
});

after(() => {
  rmSync(root, { recursive: true, force: true });
});

const rulesFor = async (file, linter = eslint) =>
  (await linter.calculateConfigForFile(path.join(root, file))).rules;
const has = (selectors, group) => group.every((entry) => selectors.includes(entry.selector));
const group = (rules, id) => selectorsOf(rules, `restrict/${id}`);

test('every restrict group is a core rule registered under its own name', () => {
  const plugin = defineRestrictPlugin(RESTRICT_GROUPS);
  assert.equal(Object.keys(plugin.rules).length, RESTRICT_GROUPS.length);
  assert.throws(() => defineRestrictPlugin(['unknown-x']), /known kind/);
});

test('core, typed, React, accessibility and local rules are error on a component', async () => {
  const rules = await rulesFor('src/components/widget.tsx');
  for (const id of [
    '@typescript-eslint/strict-boolean-expressions',
    '@typescript-eslint/ban-ts-comment',
    '@typescript-eslint/naming-convention',
    'react-hooks/rules-of-hooks',
    'jsx-a11y/aria-props',
    '@eslint-react/no-nested-component-definitions',
    '@tanstack/query/exhaustive-deps',
    'import-x/no-cycle',
    'boundaries/dependencies',
    'no-unsanitized/property',
    'unicorn/filename-case',
    'local/no-data-hook-in-presentational',
    'local/row-action-names-row',
    'local/no-unsafe-model-assertion',
    'local/no-module-binding-alias',
    'no-warning-comments',
  ]) {
    assert.equal(severityOf(rules, id), 'error', id);
  }
  assert.ok(optionsOf(rules, 'no-warning-comments')[0].terms.includes('eslint-disable'));
});

test('selector groups: a component carries the component, query-key and production groups', async () => {
  const rules = await rulesFor('src/components/widget.tsx');
  assert.ok(has(group(rules, 'syntax-components'), SEL_COMPONENTS));
  assert.ok(has(group(rules, 'syntax-query-key'), SEL_QUERY_KEY));
  assert.ok(has(group(rules, 'syntax-test-probes'), SEL_TEST_PROBES));
  assert.ok(has(group(rules, 'syntax-locale-parse'), SEL_LOCALE_PARSE));
  assert.ok(has(group(rules, 'syntax-input-preservation'), SEL_INPUT_PRESERVATION));
  assert.equal(severityOf(rules, 'restrict/syntax-tests'), undefined);
});

test('selector groups: each exemption lifts its own group only', async () => {
  const keys = await rulesFor('src/items-query-keys.ts');
  assert.equal(severityOf(keys, 'restrict/syntax-query-key'), undefined);
  assert.ok(has(group(keys, 'syntax-test-probes'), SEL_TEST_PROBES));
  const locale = await rulesFor('src/utils/numbers.ts');
  assert.equal(severityOf(locale, 'restrict/syntax-locale-parse'), undefined);
  assert.ok(has(group(locale, 'syntax-query-key'), SEL_QUERY_KEY));
});

test('selector groups: a unit test gets the test group, not the production groups', async () => {
  const rules = await rulesFor('src/components/widget.test.tsx');
  assert.ok(has(group(rules, 'syntax-tests'), SEL_TESTS));
  assert.equal(severityOf(rules, 'restrict/syntax-test-probes'), undefined);
  assert.equal(severityOf(rules, 'vitest/no-focused-tests'), 'error');
  assert.equal(severityOf(rules, 'testing-library/no-node-access'), 'error');
  assert.equal(severityOf(rules, 'jest-dom/prefer-checked'), 'error');
  assert.notEqual(severityOf(rules, 'local/no-query-data-in-state'), 'error');
});

test('e2e preset: Playwright rules apply; only the fixtures module may import test/expect', async () => {
  const spec = await rulesFor('e2e/items.spec.ts');
  assert.equal(severityOf(spec, 'playwright/no-wait-for-timeout'), 'error');
  assert.equal(severityOf(spec, 'local/settled-before-action'), 'error');
  assert.equal(severityOf(spec, 'restrict/imports-e2e-black-box'), 'error');
  assert.ok(has(group(spec, 'syntax-e2e-fixture-import'), SEL_E2E_FIXTURE_IMPORT));
  const fixtures = await rulesFor('e2e/fixtures.ts');
  assert.equal(severityOf(fixtures, 'restrict/syntax-e2e-fixture-import'), undefined);
  assert.equal(severityOf(fixtures, 'restrict/syntax-e2e'), 'error');
});

test('node preset: tooling scripts get Node globals and the JS rules, no typed rules', async () => {
  const rules = await rulesFor('tools/build.mjs');
  assert.equal(severityOf(rules, 'no-undef'), 'error');
  assert.equal(severityOf(rules, '@typescript-eslint/strict-boolean-expressions'), undefined);
});

test('library preset: typed hygiene, no extraneous dependency, the public entry may be a barrel', async () => {
  const order = await rulesFor('lib/src/order.ts', designed);
  assert.equal(severityOf(order, '@typescript-eslint/strict-boolean-expressions'), 'error');
  assert.equal(severityOf(order, 'import-x/no-extraneous-dependencies'), 'error');
  assert.equal(severityOf(order, 'barrel-files/avoid-barrel-files'), 'error');
  assert.equal(severityOf(order, 'restrict/syntax-type-only-imports'), undefined);
  const entry = await rulesFor('lib/src/index.ts', designed);
  assert.equal(severityOf(entry, 'barrel-files/avoid-barrel-files'), 'off');
});

test('browser boundaries: the client owns the network, the storage module owns storage', async () => {
  const names = async (file, id) =>
    optionsOf(await rulesFor(file), `restrict/${id}`).map((entry) => entry.name);
  assert.deepEqual(await names('src/api/client.ts', 'globals-network'), []);
  assert.ok((await names('src/api/client.ts', 'globals-storage')).includes('localStorage'));
  assert.deepEqual(await names('src/utils/storage.ts', 'globals-storage'), []);
  assert.ok((await names('src/utils/storage.ts', 'globals-network')).includes('fetch'));
  assert.ok((await names('src/components/widget.tsx', 'globals-network')).includes('fetch'));
});

test('the router module may throw a redirect', async () => {
  assert.equal(
    severityOf(await rulesFor('src/router.tsx'), '@typescript-eslint/only-throw-error'),
    'off',
  );
});

test('project-fact groups are off until their option is set', async () => {
  const rules = await rulesFor('src/pages/orders-page.tsx');
  for (const id of [
    'restrict/syntax-viewport',
    'restrict/syntax-required-marker',
    'restrict/syntax-display-format',
    'restrict/syntax-type-only-imports',
    'local/mutation-declares-feedback',
    'local/no-raw-metric-render',
    'local/no-threshold-over-served-verdict',
    'local/class-translate-no',
    'local/no-unformatted-number',
    'local/no-display-list-join',
    'local/no-straight-quote-template',
  ]) {
    assert.equal(severityOf(rules, id), undefined, id);
  }
});

test('design-system options switch their groups on with their exemptions', async () => {
  const page = await rulesFor('src/pages/orders-page.tsx', designed);
  for (const id of [
    'syntax-viewport',
    'syntax-form-grid',
    'syntax-variant-reskin',
    'syntax-required-marker',
    'syntax-single-scroller',
    'syntax-wheel',
    'syntax-page-button-class',
    'syntax-page-margin',
    'syntax-display-format',
    'syntax-wrapped',
    'syntax-caps-label',
    'syntax-containment',
    'syntax-type-only-imports',
    'globals-viewport',
    'properties-viewport',
  ]) {
    assert.equal(severityOf(page, `restrict/${id}`), 'error', id);
  }
  for (const id of [
    'local/mutation-declares-feedback',
    'local/no-raw-metric-render',
    'local/no-threshold-over-served-verdict',
    'local/class-translate-no',
  ]) {
    assert.equal(severityOf(page, id), 'error', id);
  }
  assert.ok(has(group(page, 'syntax-required-marker'), SEL_REQUIRED_MARKER));
  assert.ok(has(group(page, 'syntax-single-scroller'), SEL_SINGLE_SCROLLER));

  const primitive = await rulesFor('src/components/primitives/field.tsx', designed);
  assert.equal(severityOf(primitive, 'restrict/syntax-required-marker'), undefined);
  assert.equal(severityOf(primitive, 'restrict/syntax-wrapped'), undefined);
  assert.equal(severityOf(primitive, 'restrict/syntax-page-margin'), undefined);

  const format = await rulesFor('src/utils/format.ts', designed);
  assert.equal(severityOf(format, 'restrict/syntax-display-format'), undefined);
  assert.ok(has(group(page, 'syntax-display-format'), SEL_DISPLAY_FORMAT));

  const hook = await rulesFor('src/hooks/use-breakpoint.ts', designed);
  assert.equal(severityOf(hook, 'restrict/syntax-viewport'), undefined);
  const hookProperties = optionsOf(hook, 'restrict/properties-viewport').map((e) => e.property);
  assert.ok(!hookProperties.includes('matchMedia'));
  assert.ok(hookProperties.includes('innerWidth'));

  const router = await rulesFor('src/router.tsx', designed);
  assert.equal(severityOf(router, 'restrict/syntax-router-lazy'), 'error');
});

test('localisation: true switches the display-formatting rules on with the configured module', async () => {
  const localised = new ESLint({
    cwd: root,
    overrideConfigFile: true,
    overrideConfig: defineKitConfig({
      root,
      app: { localisation: true, designSystem: { formatModule: 'src/lib/**/text-format.ts' } },
    }),
  });
  const rules = await rulesFor('src/components/widget.tsx', localised);
  for (const id of [
    'local/no-unformatted-number',
    'local/no-display-list-join',
    'local/no-straight-quote-template',
    'local/row-action-names-row',
  ]) {
    assert.equal(severityOf(rules, id), 'error', id);
  }
  const options = optionsOf(rules, 'local/no-unformatted-number')[0];
  assert.equal(options.moduleName, 'src/lib/**/text-format.ts');
  const pattern = new RegExp(options.formatModule, 'u');
  assert.ok(pattern.test('/repo/src/lib/deep/text-format.ts'));
  assert.ok(pattern.test('/repo/src/lib/text-format.ts'));
  assert.ok(!pattern.test('/repo/src/utils/format.ts'));
});

test('the data-access layer pattern follows apiDir; the e2e search words are an option', async () => {
  const custom = new ESLint({
    cwd: root,
    overrideConfigFile: true,
    overrideConfig: defineKitConfig({
      root,
      app: { apiDir: 'src/data-access' },
      e2e: { searchWords: 'recherch' },
    }),
  });
  const component = await rulesFor('src/components/widget.tsx', custom);
  const { apiModulePattern } = optionsOf(component, 'local/no-data-hook-in-presentational')[0];
  assert.ok(new RegExp(apiModulePattern, 'u').test('../../data-access/items'));
  assert.ok(!new RegExp(apiModulePattern, 'u').test('../../api/items'));
  const spec = await rulesFor('e2e/items.spec.ts', custom);
  assert.equal(optionsOf(spec, 'local/settled-before-action')[0].searchWords, 'recherch');
});

test('a preset set to false is not applied', async () => {
  const noE2e = new ESLint({
    cwd: root,
    overrideConfigFile: true,
    overrideConfig: defineKitConfig({ root, e2e: false }),
  });
  const rules = await rulesFor('e2e/items.spec.ts', noE2e);
  assert.equal(severityOf(rules, 'playwright/no-wait-for-timeout'), undefined);
});

test('a real typed lint run reports the expected findings', async () => {
  const [result] = await eslint.lintFiles([path.join(root, 'src/components/widget.tsx')]);
  const ids = new Set(result.messages.map((message) => message.ruleId));
  assert.equal(result.fatalErrorCount, 0, JSON.stringify(result.messages));
  for (const id of [
    'restrict/syntax-base',
    'restrict/globals-network',
    'no-warning-comments',
    'local/no-unsafe-model-assertion',
  ]) {
    assert.ok(ids.has(id), `${id} in ${[...ids].join(', ')}`);
  }
});

test('selector groups fire on their target shapes', () => {
  const linter = new Linter({ configType: 'flat' });
  const verify = (code, list, jsx = false) =>
    linter.verify(code, {
      languageOptions: {
        ecmaVersion: 'latest',
        sourceType: 'module',
        parser: tseslint.parser,
        parserOptions: { ecmaFeatures: { jsx } },
      },
      rules: { 'no-restricted-syntax': ['error', ...list] },
    });
  assert.equal(
    verify("function f() { m.mutate(text); setText(''); }", SEL_INPUT_PRESERVATION).length,
    1,
  );
  assert.equal(
    verify("async function f() { await m.mutateAsync(text); setText(''); }", SEL_INPUT_PRESERVATION)
      .length,
    0,
  );
  assert.equal(verify("if (import.meta.env.MODE === 'test') {}", SEL_TEST_PROBES).length, 1);
  assert.equal(verify("if (typeof window !== 'undefined') {}", SEL_TEST_PROBES).length, 1);
  assert.equal(verify('expect(tree).toMatchSnapshot();', SEL_TESTS).length, 1);
  assert.equal(
    verify("import { Order } from '@acme/api-types';", selTypeOnlyImports(['@acme/api-types']))
      .length,
    1,
  );
  assert.equal(
    verify("import type { Order } from '@acme/api-types';", selTypeOnlyImports(['@acme/api-types']))
      .length,
    0,
  );
  const lazy = selLazyPages({ importPattern: '^\\.\\/pages\\/', eager: ['error-page'] });
  assert.equal(verify("import { OrdersPage } from './pages/orders-page';", lazy).length, 1);
  assert.equal(verify("import { ErrorPage } from './pages/error-page';", lazy).length, 0);
  const viewport = selViewport({ widths: [640] });
  assert.equal(verify('const wide = width >= 640;', viewport).length, 1);
  assert.equal(verify('const w = document.documentElement.clientWidth;', viewport).length, 1);
  assert.equal(
    verify("const s = { gridTemplateColumns: '1fr 1fr' };", selFormGrid('form-grid')).length,
    1,
  );
  assert.equal(
    verify(
      "const s = { gridTemplateColumns: 'repeat(auto-fit, minmax(10rem, 1fr))' };",
      selFormGrid('form-grid'),
    ).length,
    0,
  );
  assert.equal(
    verify("const b = <Button style={{ color: 'red' }} />;", selVariantReskin(['Button']), true)
      .length,
    1,
  );
  assert.equal(
    verify("const b = <Button style={{ width: '100%' }} />;", selVariantReskin(['Button']), true)
      .length,
    0,
  );
  assert.equal(
    verify('const r = <input aria-required="true" />;', SEL_REQUIRED_MARKER, true).length,
    1,
  );
  assert.equal(
    verify("const p = <div style={{ maxHeight: '10rem' }} />;", SEL_SINGLE_SCROLLER, true).length,
    1,
  );
  const wrapped = selWrapped(
    [{ element: 'details', use: 'Disclosure' }],
    [{ source: 'acme-ui', names: ['Badge'], use: 'StatusPill' }],
  );
  assert.equal(verify('const d = <details />;', wrapped, true).length, 1);
  assert.equal(verify("import { Badge } from 'acme-ui';", wrapped).length, 1);
  assert.equal(verify('const line = `${a} · ${b}`;', SEL_DISPLAY_FORMAT).length, 1);
});

test('tsconfig.base.json: tsc refuses an unchecked index access and accepts a checked one', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'ts-lint-kit-tsc-'));
  try {
    writeFileSync(
      path.join(dir, 'tsconfig.json'),
      JSON.stringify({ extends: path.join(KIT, 'tsconfig.base.json'), include: ['*.ts'] }),
    );
    const tsc = require.resolve('typescript/bin/tsc');
    writeFileSync(
      path.join(dir, 'a.ts'),
      'const xs: number[] = [];\nexport const n: number = xs[0];\n',
    );
    const bad = spawnSync(process.execPath, [tsc, '-p', dir], { encoding: 'utf8' });
    assert.notEqual(bad.status, 0);
    assert.match(bad.stdout, /TS2322/);
    writeFileSync(
      path.join(dir, 'a.ts'),
      'const xs: number[] = [];\nexport const n: number = xs[0] ?? 0;\n',
    );
    const good = spawnSync(process.execPath, [tsc, '-p', dir], { encoding: 'utf8' });
    assert.equal(good.status, 0, good.stdout);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('tsconfig.library.json: an exported function needs an explicit return type', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'ts-lint-kit-lib-'));
  try {
    writeFileSync(
      path.join(dir, 'tsconfig.json'),
      JSON.stringify({ extends: path.join(KIT, 'tsconfig.library.json'), include: ['*.ts'] }),
    );
    const tsc = require.resolve('typescript/bin/tsc');
    writeFileSync(
      path.join(dir, 'a.ts'),
      'export function twice(n: number) {\n  return n * 2;\n}\n',
    );
    const bad = spawnSync(process.execPath, [tsc, '-p', dir], { encoding: 'utf8' });
    assert.notEqual(bad.status, 0);
    assert.match(bad.stdout, /TS9007/);
    writeFileSync(
      path.join(dir, 'a.ts'),
      'export function twice(n: number): number {\n  return n * 2;\n}\n',
    );
    const good = spawnSync(process.execPath, [tsc, '-p', dir], { encoding: 'utf8' });
    assert.equal(good.status, 0, good.stdout);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('knip.json sets every issue type to error; .prettierrc parses', () => {
  const knip = JSON.parse(readFileSync(path.join(KIT, 'knip.json'), 'utf8'));
  assert.ok(Object.values(knip.rules).every((level) => level === 'error'));
  assert.ok(Object.keys(knip.rules).length >= 12);
  const prettier = JSON.parse(readFileSync(path.join(KIT, '.prettierrc'), 'utf8'));
  assert.equal(prettier.printWidth, 100);
});

test('knip.json: knip reports an unreferenced source file and fails', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'ts-lint-kit-knip-'));
  try {
    mkdirSync(path.join(dir, 'src'));
    writeFileSync(
      path.join(dir, 'package.json'),
      JSON.stringify({ name: 'sample', private: true, type: 'module' }),
    );
    writeFileSync(
      path.join(dir, 'tsconfig.json'),
      JSON.stringify({ compilerOptions: { strict: true } }),
    );
    writeFileSync(path.join(dir, 'src/main.tsx'), 'export const used = 1;\n');
    writeFileSync(path.join(dir, 'src/orphan.ts'), 'export const orphan = 2;\n');
    const knip = path.resolve(path.dirname(require.resolve('knip')), '..', 'bin', 'knip.js');
    const run = spawnSync(
      process.execPath,
      [knip, '--directory', dir, '--config', path.join(KIT, 'knip.json'), '--no-progress'],
      { encoding: 'utf8' },
    );
    assert.notEqual(run.status, 0);
    assert.match(run.stdout, /src\/orphan\.ts/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('kit.config.example.mjs takes its root from its own location, not from the cwd', () => {
  const source = readFileSync(new URL('./kit.config.example.mjs', import.meta.url), 'utf8');
  assert.ok(!source.includes('process.cwd()'));
  assert.ok(source.includes('import.meta.url'));
});
