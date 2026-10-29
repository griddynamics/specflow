import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { detectSources, parseRequest } from './detect-sources.mjs';

const entry = fileURLToPath(new URL('./detect-sources.mjs', import.meta.url));
const body = Array.from(
  { length: 14 },
  (_, index) => `    total = total + value_${String(index)}`,
).join('\n');
const python = `def calculate(value_0, value_1):\n    total = 0\n${body}\n    return total\n`;
const options = { minLines: 5, minTokens: 40, mode: 'weak' };

function request(sources) {
  return JSON.stringify({ ...options, sources });
}

test('a copied Python function is one clone at the later copy', async () => {
  const result = await detectSources(
    parseRequest(
      request([
        { id: 'src/a.py', format: 'python', source: python },
        { id: 'src/b.py', format: 'python', source: python },
      ]),
    ),
  );
  assert.equal(result.scanned, 2);
  assert.equal(result.clones.length, 1);
  const [clone] = result.clones;
  assert.deepEqual([clone.a.id, clone.b.id, clone.a.start.line], ['src/b.py', 'src/a.py', 1]);
  assert.ok(clone.tokens >= options.minTokens);
});

test('distinct sources have no clone', async () => {
  const other = python.replaceAll('+', '*').replaceAll('value_', 'factor_');
  const result = await detectSources(
    parseRequest(
      request([
        { id: 'a.py', format: 'python', source: python },
        { id: 'b.py', format: 'python', source: other },
      ]),
    ),
  );
  assert.deepEqual(result, { scanned: 2, clones: [] });
});

test('a malformed request is refused', () => {
  assert.throws(() => parseRequest('[]'), /JSON object/u);
  assert.throws(() => parseRequest(JSON.stringify({ ...options, minLines: 0 })), /minLines/u);
  assert.throws(() => parseRequest(JSON.stringify({ ...options, mode: 'loose' })), /mode/u);
  assert.throws(() => parseRequest(JSON.stringify({ ...options, sources: {} })), /sources/u);
  assert.throws(() => parseRequest(request([{ id: 'a.py', source: python }])), /format/u);
});

test('the command reads stdin, prints JSON and exits 2 on bad input', () => {
  const ok = spawnSync(process.execPath, [entry], {
    input: request([{ id: 'a.py', format: 'python', source: python }]),
    encoding: 'utf8',
  });
  assert.equal(ok.status, 0, ok.stderr);
  assert.deepEqual(JSON.parse(ok.stdout), { scanned: 1, clones: [] });
  const bad = spawnSync(process.execPath, [entry], { input: '{', encoding: 'utf8' });
  assert.equal(bad.status, 2);
  assert.match(bad.stderr, /^tool-error: clone engine: /u);
});
