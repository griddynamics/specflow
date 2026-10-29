import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { check, semgrepBin } from './semgrep.mjs';

/** A target root with a rules directory, a source directory and a fake
 * scanner that prints `body` and exits `status`; the scanner is selected
 * through `SEMGREP_BIN` for the duration of `fn`. */
function withScanner(script, fn) {
  const root = mkdtempSync(path.join(os.tmpdir(), 'kit-semgrep-'));
  const previous = process.env.SEMGREP_BIN;
  try {
    mkdirSync(path.join(root, '.semgrep'));
    mkdirSync(path.join(root, 'src'));
    const binary = path.join(root, 'scanner');
    writeFileSync(binary, `#!/bin/sh\n${script}\n`, { mode: 0o755 });
    process.env.SEMGREP_BIN = binary;
    return fn(root);
  } finally {
    if (previous === undefined) delete process.env.SEMGREP_BIN;
    else process.env.SEMGREP_BIN = previous;
    rmSync(root, { recursive: true, force: true });
  }
}

test('an explicit semgrep binary overrides PATH lookup', () => {
  const previous = process.env.SEMGREP_BIN;
  try {
    delete process.env.SEMGREP_BIN;
    const trackBin = fileURLToPath(new URL('../.venv/bin/semgrep', import.meta.url));
    assert.ok(['semgrep', trackBin].includes(semgrepBin()));
    process.env.SEMGREP_BIN = '/opt/validation/semgrep';
    assert.equal(semgrepBin(), '/opt/validation/semgrep');
  } finally {
    if (previous === undefined) delete process.env.SEMGREP_BIN;
    else process.env.SEMGREP_BIN = previous;
  }
});

test('results become findings named by the last segment of the rule id', () => {
  const body = JSON.stringify({
    results: [
      {
        check_id: 'rules.no-inner-html',
        path: 'src/a.tsx',
        start: { line: 3, col: 5 },
        extra: { message: 'avoid innerHTML\nsecond line' },
      },
    ],
    errors: [],
    paths: { scanned: ['src/a.tsx'] },
  });
  withScanner(`printf '%s' '${body}'`, (root) => {
    assert.deepEqual(check({ root }), [
      { path: 'src/a.tsx', line: 3, col: 5, rule: 'no-inner-html', message: 'avoid innerHTML' },
    ]);
  });
});

test('a crashed scanner without JSON never certifies a clean tree', () => {
  withScanner('echo "Permission denied opening scanner log" >&2\nexit 1', (root) => {
    assert.throws(() => check({ root }), /did not return JSON/);
  });
});

test('a JSON response without scanned targets never certifies a clean tree', () => {
  withScanner(`printf '{"results":[],"paths":{"scanned":[]}}'`, (root) => {
    assert.throws(() => check({ root }), /no scanned targets/);
  });
});

test('a rule that fails to load is a configuration error, not zero findings', () => {
  const body = JSON.stringify({
    results: [],
    errors: [{ level: 'error', message: 'invalid rule schema' }],
    paths: { scanned: ['src/a.tsx'] },
  });
  withScanner(`printf '%s' '${body}'`, (root) => {
    assert.throws(() => check({ root }), /configuration error\(s\).*invalid rule schema/);
  });
});

test('a partially scanned target never certifies the tree', () => {
  const body = JSON.stringify({
    results: [],
    errors: [{ level: 'warn', type: 'Timeout', message: 'timeout on src/a.tsx' }],
    paths: { scanned: ['src/a.tsx'] },
  });
  withScanner(`printf '%s' '${body}'`, (root) => {
    assert.throws(() => check({ root }), /did not scan 1 target/);
  });
});

test('an exit status above 1 is a tool error', () => {
  withScanner('echo "bad config" >&2\nexit 7', (root) => {
    assert.throws(() => check({ root }), /semgrep exited 7: bad config/);
  });
});

test('a default rules or target directory that is absent: no findings', () => {
  withScanner('exit 9', (root) => {
    rmSync(path.join(root, '.semgrep'), { recursive: true });
    assert.deepEqual(check({ root }), []);
    rmSync(path.join(root, 'src'), { recursive: true });
    assert.deepEqual(check({ root }), []);
  });
});

test('a named config or target that does not exist is a tool error', () => {
  withScanner('exit 0', (root) => {
    assert.throws(() => check({ root, options: { config: 'nope' } }), /nope does not exist/);
    assert.throws(() => check({ root, options: { target: 'nope' } }), /nope does not exist/);
  });
});
