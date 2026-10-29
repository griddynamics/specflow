import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { readJsonInput, requireArray, requireStringArray } from './spec-input.mjs';

test('reads a JSON input relative to the root', (t) => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'spec-input-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  writeFileSync(path.join(root, 'input.json'), '["a","b"]');
  assert.deepEqual(readJsonInput(root, 'input.json', '--input'), ['a', 'b']);
});

test('a missing or malformed input is an error naming the option', (t) => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'spec-input-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  writeFileSync(path.join(root, 'broken.json'), '{ nope');
  assert.throws(() => readJsonInput(root, 'absent.json', '--input'), /--input does not exist/);
  assert.throws(() => readJsonInput(root, 'broken.json', '--input'), /--input is not valid JSON/);
});

test('shape guards accept arrays and refuse anything else', () => {
  assert.deepEqual(requireArray([1], '--x'), [1]);
  assert.throws(() => requireArray({}, '--x'), /--x must be a JSON array/);
  assert.deepEqual(requireStringArray(['a'], '--x'), ['a']);
  assert.throws(() => requireStringArray(['a', 1], '--x'), /--x must contain only strings/);
});
