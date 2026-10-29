import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { loadProjectSourceFiles, ts } from './program.mjs';

function projectFixture(t, config) {
  const root = mkdtempSync(path.join(tmpdir(), 'i18n-program-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const srcRoot = path.join(root, 'src');
  mkdirSync(srcRoot);
  writeFileSync(path.join(root, 'tsconfig.json'), JSON.stringify(config));
  writeFileSync(path.join(root, 'outside.ts'), "export const outside = 'outside';");
  writeFileSync(path.join(srcRoot, 'data.json'), '{"key":"json"}');
  writeFileSync(path.join(srcRoot, 'widget.tsx'), "export const label = 'first';");
  writeFileSync(path.join(srcRoot, 'helper.ts'), "export const helper = 'helper';");
  writeFileSync(path.join(srcRoot, 'widget.test.tsx'), "export const label = 'test';");
  writeFileSync(path.join(srcRoot, 'helper.test.ts'), "export const helper = 'test';");
  return { root, srcRoot };
}

test('project loading excludes tests, JSON and external files and refreshes changed source', (t) => {
  const { root, srcRoot } = projectFixture(t, {
    compilerOptions: { noLib: true, types: [], resolveJsonModule: true },
    include: ['src/**/*', 'outside.ts'],
  });
  const first = loadProjectSourceFiles(root, srcRoot);
  assert.ok(first.checker);
  assert.deepEqual(first.sourceFiles.map((file) => path.basename(file.fileName)).sort(), [
    'helper.ts',
    'widget.tsx',
  ]);
  assert.match(
    first.sourceFiles.find((file) => file.fileName.endsWith('widget.tsx')).text,
    /first/,
  );

  writeFileSync(path.join(srcRoot, 'widget.tsx'), "export const label = 'changed';");
  const second = loadProjectSourceFiles(root, srcRoot);
  assert.notEqual(second.checker, first.checker);
  assert.match(
    second.sourceFiles.find((file) => file.fileName.endsWith('widget.tsx')).text,
    /changed/,
  );
});

test('failed program construction retains the same filtered syntax-only fallback', (t) => {
  const { root, srcRoot } = projectFixture(t, {});
  t.mock.method(ts.sys, 'readFile', () => {
    throw new Error('compiler file read failed');
  });
  const { sourceFiles, checker } = loadProjectSourceFiles(root, srcRoot);
  assert.equal(checker, null);
  assert.deepEqual(
    sourceFiles.map((file) => path.basename(file.fileName)),
    ['helper.ts', 'widget.tsx'],
  );
  assert.match(sourceFiles[1].text, /first/);
});
