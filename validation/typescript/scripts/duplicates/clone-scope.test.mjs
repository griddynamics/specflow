import assert from 'node:assert/strict';
import { test } from 'node:test';
import { projectArgument, projectFiles } from './clone-scope.mjs';

test('the default global inventory retains cross-owner files', () => {
  const files = [
    'packages/server/source.py',
    'packages/client/source.ts',
    'packages/server-extra/source.py',
  ];
  assert.equal(projectArgument([]), null);
  assert.deepEqual(projectFiles(files, null), files);
  assert.deepEqual(projectFiles(files, 'packages/server'), ['packages/server/source.py']);
});

test('normalized project paths use directory boundaries', () => {
  const files = ['lib/fixtures/build.py', 'lib/fixtures-extra/build.py', 'tools/builders/setup.py'];
  assert.equal(projectArgument(['--project', './lib/fixtures/']), 'lib/fixtures');
  assert.deepEqual(projectFiles(files, 'lib/fixtures'), ['lib/fixtures/build.py']);
  assert.deepEqual(projectFiles(files, '.'), files);
});

test('invalid or escaping project selectors fail closed', () => {
  for (const argv of [
    ['--unknown', 'packages/server'],
    ['--project'],
    ['--project', '../packages/server'],
    ['--project', '/packages/server'],
    ['--project', 'packages/../../server'],
    ['--project', ''],
  ])
    assert.throws(() => projectArgument(argv), TypeError);
});
