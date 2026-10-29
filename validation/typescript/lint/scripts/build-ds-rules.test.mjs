import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  RULE_FILES,
  buildDesignSystemRules,
  renderDesignSystemRules,
  renderTemplate,
} from './build-ds-rules.mjs';

const read = (relative) => readFileSync(new URL(relative, import.meta.url), 'utf8');
const example = JSON.parse(read('../design-system.example.json'));
const template = (name) => read(`../templates/${name}.yml.tmpl`);

test('the shipped semgrep-ds/ds-*.yml files are the build of design-system.example.json', () => {
  const rendered = renderDesignSystemRules(example, template);
  for (const name of RULE_FILES) {
    assert.equal(rendered[name], read(`../semgrep-ds/${name}.yml`), name);
  }
});

test('the default semgrep folder carries no design-system rule file', () => {
  const folder = fileURLToPath(new URL('../semgrep', import.meta.url));
  assert.deepEqual(
    readdirSync(folder).filter((name) => name.startsWith('ds-')),
    [],
  );
});

test('facts move the paths and the name lists of the rules', () => {
  const facts = {
    ...example,
    primitivesDir: 'web/ui',
    breadcrumbFile: 'web/nav/trail.tsx',
    routeHelperFiles: ['web/router.tsx'],
    primitiveNames: ['Card', 'Chip'],
    modalNames: ['Sheet'],
    headerComponentNames: ['Heading', 'Banner'],
    pillComponentNames: ['Tag'],
    typeRoleFields: ['caption', 'lead'],
    geometryModules: ['web/geometry.ts'],
    dismissalHook: { name: 'useClose', file: 'web/ui/use-close.ts' },
    components: { ...example.components, emptyState: 'Blank' },
  };
  const rendered = renderDesignSystemRules(facts, template);
  assert.match(rendered['ds-components'], /- 'web\/ui\/\*\*'/u);
  assert.match(rendered['ds-components'], /regex: '\^\(Card\|Chip\)\$'/u);
  assert.match(rendered['ds-components'], /\(Tag\)\$'/u);
  assert.match(rendered['ds-components'], /<Blank \.\.\. \/>/u);
  assert.match(rendered['ds-navigation'], /- 'web\/router\.tsx'/u);
  assert.match(rendered['ds-overlays'], /useClose\\\(\|<\(Sheet\)/u);
  assert.match(rendered['ds-typography'], /\(Heading\|Banner\)\$'/u);
  assert.match(rendered['ds-typography'], /regex: '\^\(caption\|lead\)\$'/u);
  assert.match(rendered['ds-styling'], /- 'web\/geometry\.ts'/u);
  assert.doesNotMatch(rendered['ds-overlays'], /useDismissal|use-dismissal/u);
  assert.doesNotMatch(rendered['ds-navigation'], /components\/navigation/u);
  assert.doesNotMatch(rendered['ds-typography'], /PageHeader\|SectionHeader/u);
  assert.doesNotMatch(rendered['ds-styling'], /utils\/geometry/u);
});

test('an empty geometryModules list leaves only the test globs in the exclude', () => {
  const rendered = renderDesignSystemRules({ ...example, geometryModules: [] }, template);
  assert.doesNotMatch(rendered['ds-styling'], /@@|geometry\.ts/u);
});

test('a missing fact, a quote in a path and an unresolved placeholder are refused', () => {
  assert.throws(
    () => renderTemplate("- '@@searchModule@@'", { ...example, searchModule: undefined }),
    /"searchModule" is missing/u,
  );
  assert.throws(
    () => renderTemplate("- '@@searchModule@@'", { searchModule: "a'b" }),
    /without a quote/u,
  );
  assert.throws(() => renderTemplate('@@ nope @@', example), /unresolved placeholder/u);
});

test('buildDesignSystemRules writes every rule file into the folder it is given', () => {
  const out = mkdtempSync(path.join(os.tmpdir(), 'ts-lint-kit-ds-'));
  try {
    const factsFile = fileURLToPath(new URL('../design-system.example.json', import.meta.url));
    const written = buildDesignSystemRules(factsFile, path.join(out, 'nested'));
    assert.deepEqual(
      written.map((file) => path.basename(file)),
      RULE_FILES.map((name) => `${name}.yml`),
    );
    for (const file of written) {
      assert.equal(readFileSync(file, 'utf8'), read(`../semgrep-ds/${path.basename(file)}`));
    }
  } finally {
    rmSync(out, { recursive: true, force: true });
  }
});
