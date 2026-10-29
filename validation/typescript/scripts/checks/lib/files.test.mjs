import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { listFiles, relPath } from './files.mjs';

test('listFiles skips dependency and build output folders, including the Next.js .next folder', (t) => {
  const root = mkdtempSync(path.join(tmpdir(), 'list-files-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const file of [
    'app/page.tsx',
    'node_modules/pkg/index.ts',
    'dist/out.ts',
    '.next/static/chunk.css',
  ]) {
    mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    writeFileSync(path.join(root, file), '');
  }
  const listed = listFiles(root, { extensions: ['.ts', '.tsx', '.css'] }).map((file) =>
    relPath(root, file),
  );
  assert.deepEqual(listed, ['app/page.tsx']);
});
