import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, copyFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const here = path.dirname(fileURLToPath(import.meta.url));

test('a crash while loading the scan is a tool error, not a finding', () => {
  const directory = mkdtempSync(path.join(tmpdir(), 'duplicates-crash-'));
  try {
    copyFileSync(path.join(here, 'duplicates.mjs'), path.join(directory, 'duplicates.mjs'));
    writeFileSync(
      path.join(directory, 'duplicates-run.mjs'),
      "import 'package-that-does-not-exist-for-duplicates-test';\n",
    );
    const result = spawnSync(process.execPath, [path.join(directory, 'duplicates.mjs')], {
      encoding: 'utf8',
    });
    assert.equal(result.status, 2);
    assert.match(result.stderr, /tool-error/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
