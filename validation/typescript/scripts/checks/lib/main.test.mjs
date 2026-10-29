import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { listOption, stringOption } from './main.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.join(__dirname, '..', 'html-scripts.mjs');

/** Every script wires `runScript(check, { skipIf })`; this spawns one of them
 * (`html-scripts.mjs`, cheapest to point at an empty root) to prove the shared
 * CLI contract end to end: a `skipIf` reason prints `skipped: <reason>` on
 * stderr and exits 0, never producing a finding. */
test("runScript: a skipIf reason prints 'skipped: ...' on stderr and exits 0, with no findings on stdout", () => {
  const proc = spawnSync(process.execPath, [SCRIPT, '--root=/nonexistent-root-for-skip-test'], {
    encoding: 'utf8',
  });
  assert.equal(proc.status, 0);
  assert.match(proc.stderr, /^skipped: no targets:/);
  assert.equal(proc.stdout, '');
});

test('runScript: a stray positional argument is a tool error (exit 2), not a clean run', () => {
  const proc = spawnSync(process.execPath, [SCRIPT, 'stray'], { encoding: 'utf8' });
  assert.equal(proc.status, 2);
  assert.match(proc.stderr, /^tool-error: unexpected positional arguments: stray/);
});

test('string and list options fall back on absent or bare flags', () => {
  assert.equal(
    stringOption({ html: 'public/index.html' }, 'html', 'index.html'),
    'public/index.html',
  );
  assert.equal(stringOption({ html: true }, 'html', 'index.html'), 'index.html');
  assert.equal(stringOption(undefined, 'html', 'index.html'), 'index.html');
  assert.deepEqual(listOption({ families: 'a., b.' }, 'families'), ['a.', 'b.']);
  assert.deepEqual(listOption({ families: ['x.'] }, 'families'), ['x.']);
  assert.deepEqual(listOption({}, 'families'), []);
});
