import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, test } from 'node:test';

import {
  CHECKS,
  DEFAULT_CHECKS,
  aggregateExit,
  cmdStep,
  optionFlags,
  plan,
  resolveBin,
  statusFor,
  TOOLS,
} from './run.mjs';
import { parseTsc } from './checks/lib/parsers.mjs';

const KIT = path.dirname(fileURLToPath(import.meta.url));
const RUNNER = path.join(KIT, 'run.mjs');
const FIXTURES = path.join(KIT, 'checks', 'fixtures');

function runCli(args) {
  return spawnSync(process.execPath, [RUNNER, ...args], { encoding: 'utf8' });
}

describe('statusFor and aggregateExit', () => {
  test('one status vocabulary', () => {
    assert.equal(statusFor({ exit: 0, detail: '' }), 'ok');
    assert.equal(statusFor({ exit: 1, detail: '' }), 'findings');
    assert.equal(statusFor({ exit: 2, detail: 'tool-error: x' }), 'tool-error');
    assert.equal(statusFor({ exit: 0, detail: 'skipped: no targets' }), 'skipped');
  });

  test('worst of wins; an empty run is clean', () => {
    assert.equal(aggregateExit([{ exit: 0 }, { exit: 1 }]), 1);
    assert.equal(aggregateExit([{ exit: 0 }, { exit: 1 }, { exit: 2 }]), 2);
    assert.equal(aggregateExit([]), 0);
  });
});

describe('cmdStep: tool exit statuses', () => {
  const diagLine = 'src/a.ts(10,3): error TS4111: Property x comes from an index signature.';
  // An absolute argv[0] is never found under node_modules/.bin, so cmdStep runs
  // this exact fake process instead of an installed tool.
  function fake(exitCode, { withDiagnostic = true } = {}) {
    const script = withDiagnostic
      ? `console.log(${JSON.stringify(diagLine)}); process.exit(${exitCode});`
      : `process.exit(${exitCode});`;
    return [process.execPath, '-e', script];
  }
  const root = os.tmpdir();

  test('exit 2 with parsed diagnostics is findings when the step opts in', () => {
    const result = cmdStep('tsc', fake(2), {
      root,
      parser: parseTsc,
      diagnosticStatuses: [0, 1, 2],
    });
    assert.equal(result.exit, 1);
    assert.equal(result.findings[0].rule, 'TS4111');
  });

  test('exit 2 is a tool error for a step that does not opt in', () => {
    const result = cmdStep('tool', fake(2), { root, parser: parseTsc });
    assert.equal(result.exit, 2);
    assert.match(result.detail, /^tool-error:/);
  });

  test('a non-zero exit with nothing parsed is a tool error, never a silent pass', () => {
    const result = cmdStep('tool', fake(1, { withDiagnostic: false }), { root, parser: parseTsc });
    assert.equal(result.exit, 2);
    assert.match(result.detail, /no parsed findings/);
  });

  test('an unparseable structured output (parser null) is a tool error', () => {
    const result = cmdStep('tool', fake(0), { root, parser: () => null });
    assert.equal(result.exit, 2);
  });

  test('exit 0 with no diagnostics is clean', () => {
    const result = cmdStep('tsc', fake(0, { withDiagnostic: false }), { root, parser: parseTsc });
    assert.deepEqual([result.exit, result.findings], [0, []]);
  });

  test('a missing tool is reported as tool-missing', () => {
    const result = cmdStep('x', ['kit-tool-that-does-not-exist'], { root, parser: parseTsc });
    assert.equal(result.exit, 2);
    assert.match(result.detail, /^tool-missing:/);
  });
});

describe('plan', () => {
  test('without a config the default checks run against the current directory', () => {
    const { root, steps } = plan([], '/tmp/x');
    assert.equal(root, '/tmp/x');
    assert.deepEqual(
      steps.map((s) => s.name),
      DEFAULT_CHECKS,
    );
  });

  test('a config enables, disables and configures checks and tools; root is config-relative', () => {
    const dir = mkdtempSync(path.join(os.tmpdir(), 'kit-config-'));
    try {
      const configPath = path.join(dir, 'kit.json');
      writeFileSync(
        configPath,
        JSON.stringify({
          root: 'app',
          checks: { 'dist-policy': { assets: 'build/assets' }, contrast: false },
          tools: ['tsc'],
        }),
      );
      const { root, steps } = plan(['--config', configPath]);
      assert.equal(root, path.join(dir, 'app'));
      const names = steps.map((s) => s.name);
      assert.ok(names.includes('dist-policy'));
      assert.ok(!names.includes('contrast'));
      assert.equal(names.at(-1), 'tsc');
      assert.deepEqual(steps.find((s) => s.name === 'dist-policy').options, {
        assets: 'build/assets',
      });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test('binDirs resolve relative to the config file; a malformed value is refused', () => {
    const dir = mkdtempSync(path.join(os.tmpdir(), 'kit-config-'));
    try {
      const configPath = path.join(dir, 'kit.json');
      writeFileSync(configPath, JSON.stringify({ binDirs: ['tools/lint'] }));
      assert.deepEqual(plan(['--config', configPath]).binDirs, [path.join(dir, 'tools', 'lint')]);
      writeFileSync(configPath, JSON.stringify({ binDirs: 'tools/lint' }));
      assert.throws(() => plan(['--config', configPath]), /binDirs/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test('the spec-driven checks are opt-in, never default', () => {
    for (const name of [
      'api-contract-map',
      'nav-tree',
      'next-routes',
      'next-boundaries',
      'bundle-size',
    ]) {
      assert.ok(CHECKS.includes(name), name);
      assert.ok(!DEFAULT_CHECKS.includes(name), name);
    }
  });

  test('--only selects steps; unknown names and arguments are refused', () => {
    assert.deepEqual(
      plan(['--only', 'tokens,knip']).steps.map((s) => [s.name, s.kind]),
      [
        ['tokens', 'check'],
        ['knip', 'tool'],
      ],
    );
    assert.throws(() => plan(['--only', 'nope']), /unknown steps: nope/);
    assert.throws(() => plan(['--level', 'quick']), /unknown arguments: --level/);
    assert.throws(() => plan(['stray']), /unknown arguments: stray/);
  });

  test('knip-production is a selectable tool step', () => {
    assert.ok(TOOLS.includes('knip-production'));
    assert.deepEqual(
      plan(['--only', 'knip-production']).steps.map((s) => [s.name, s.kind]),
      [['knip-production', 'tool']],
    );
  });

  test('every check has a script and option flags render as --key=value', () => {
    for (const name of CHECKS) assert.ok(existsSync(path.join(KIT, 'checks', `${name}.mjs`)), name);
    assert.deepEqual(optionFlags({ a: 'x', b: ['p.', 'q.'] }), ['--a=x', '--b=p.,q.']);
  });
});

test('resolveBin walks up to the nearest node_modules/.bin', () => {
  assert.equal(resolveBin('kit-tool-that-does-not-exist', KIT), null);
});

describe('runner CLI end to end', () => {
  test('a finding in the target makes exit 1 and is printed in the contract', () => {
    const result = runCli([
      '--root',
      path.join(FIXTURES, 'html-scripts', 'negative'),
      '--only',
      'html-scripts',
    ]);
    assert.equal(result.status, 1, result.stderr);
    assert.match(result.stdout, /^index\.html:8:1: html-scripts-not-deferred: /m);
    assert.match(result.stderr, /\[html-scripts\] findings/);
  });

  test('a target with no inputs skips every default check and exits 0', () => {
    const empty = mkdtempSync(path.join(os.tmpdir(), 'kit-empty-'));
    try {
      const result = runCli(['--root', empty, '--only', 'html-scripts,i18n-usage,tokens,contrast']);
      assert.equal(result.status, 0, result.stderr);
      assert.match(result.stderr, /\[tokens\] skipped -- skipped: no targets/);
    } finally {
      rmSync(empty, { recursive: true, force: true });
    }
  });

  test('a check that cannot run is a tool error (exit 2)', () => {
    const result = runCli(['--root', path.join(FIXTURES, 'dist-policy'), '--only', 'dist-policy']);
    assert.equal(result.status, 2);
    assert.match(result.stderr, /\[dist-policy\] tool-error/);
  });

  test('bad arguments and a missing root exit 2 without running anything', () => {
    assert.equal(runCli(['--bogus']).status, 2);
    assert.equal(runCli(['--root', '/nonexistent-kit-root']).status, 2);
  });
});
