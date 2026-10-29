import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { parseContractFindings } from './lib/parsers.mjs';

const checker = fileURLToPath(new URL('./css-usage.mjs', import.meta.url));

function writeTree(root, sources) {
  for (const [relative, content] of Object.entries(sources)) {
    const file = path.join(root, relative);
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, content);
  }
}

test('classes used only by tests are reported; production and template classes are retained', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'css-usage-'));
  try {
    writeTree(root, {
      'index.html': '<body class="template-class"></body>',
      'src/main.tsx': '<div className="production-class" />',
      'src/components/widgets/panel.tsx': '<div className="panel-class" />',
      'src/components/forms/form-panel.tsx': '<div className="form-class" />',
      'src/main.test.tsx': '<div className="unit-only" />',
      'src/main.spec.ts': 'const example = "spec-only";',
      'tests/support/wrapper.tsx': '<div className="support-only" />',
      'src/styles/global.css':
        '.template-class, .production-class, .panel-class, .form-class { color: red; }\n' +
        '.unit-only, .spec-only, .support-only { color: blue; }\n' +
        'article:focus, :focus-visible { outline: 1px solid; }',
    });

    // Run from another directory: the result must not depend on the cwd.
    const result = spawnSync(process.execPath, [checker, '--root', root], {
      cwd: os.tmpdir(),
      encoding: 'utf8',
    });

    assert.equal(result.status, 1, result.stderr);
    const findings = parseContractFindings(result.stdout);
    assert.deepEqual(findings.map((finding) => finding.message).sort(), [
      'unused class selector .spec-only',
      'unused class selector .support-only',
      'unused class selector .unit-only',
    ]);
    assert.ok(findings.every((finding) => finding.rule === 'unused-css-class'));
    assert.ok(findings.every((finding) => finding.path === 'src/styles/global.css'));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('a target with no stylesheet or no source directory is skipped, not passed', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'css-usage-skip-'));
  try {
    writeTree(root, { 'index.html': '<body></body>', 'src/main.tsx': '<div />' });
    const noCss = spawnSync(process.execPath, [checker, `--root=${root}`], { encoding: 'utf8' });
    assert.equal(noCss.status, 0, noCss.stderr);
    assert.match(noCss.stderr, /skipped: no targets: no stylesheet matches/u);

    const noSrc = spawnSync(process.execPath, [checker, `--root=${root}`, '--src=missing'], {
      encoding: 'utf8',
    });
    assert.equal(noSrc.status, 0, noSrc.stderr);
    assert.match(noSrc.stderr, /skipped: no targets: missing does not exist/u);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('a tree whose every class is used is clean', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'css-usage-clean-'));
  try {
    writeTree(root, {
      'index.html': '<body></body>',
      'src/main.tsx': '<div className="used" />',
      'src/styles/global.css': '.used { color: red; }\nbody { margin: 0; }',
    });
    const result = spawnSync(process.execPath, [checker, `--root=${root}`], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, '');
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
