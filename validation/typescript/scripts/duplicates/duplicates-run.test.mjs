import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const entry = fileURLToPath(new URL('./duplicates.mjs', import.meta.url));

const body = Array.from(
  { length: 18 },
  (_, index) => `  total = total + value${String(index)};`,
).join('\n');
const source = `export function sum() {\n  let total = 0;\n${body}\n  return total;\n}\n`;

/** A git work tree with `files`; the scan runs with it as the current directory. */
function scan(files, argv = []) {
  const root = mkdtempSync(path.join(tmpdir(), 'kit-duplicates-'));
  try {
    assert.equal(spawnSync('git', ['init', '-q', root]).status, 0);
    for (const [relative, content] of Object.entries(files)) {
      mkdirSync(path.dirname(path.join(root, relative)), { recursive: true });
      writeFileSync(path.join(root, relative), content);
    }
    return spawnSync(process.execPath, [entry, ...argv], { cwd: root, encoding: 'utf8' });
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

test('a copied implementation is a clone finding (exit 1)', () => {
  const result = scan({ 'src/a.ts': source, 'src/b.ts': source });
  assert.equal(result.status, 1, result.stderr);
  assert.match(result.stdout, /^src\/b\.ts:\d+:1: jscpd-clone: duplicates src\/a\.ts:\d+$/m);
  assert.match(result.stdout, /jscpd: 2 files, 1 clones/);
});

test('distinct sources are clean (exit 0) and ignored globs are not scanned', () => {
  const result = scan({ 'src/a.ts': source, 'src/fixtures/b.ts': source });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stdout, /jscpd: 1 files, 0 clones/);
});

test('--project limits the scan to one directory', () => {
  const result = scan(
    { 'one/a.ts': source, 'two/b.ts': source, 'two/c.ts': 'export const c = 1;\n' },
    ['--project', 'two'],
  );
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stdout, /jscpd: 2 files, 0 clones/);
});

test('no supported source and a missing project are tool errors (exit 2)', () => {
  assert.equal(scan({ 'README.md': '# x\n' }).status, 2);
  assert.equal(scan({ 'src/a.ts': source }, ['--project', 'missing']).status, 2);
});
