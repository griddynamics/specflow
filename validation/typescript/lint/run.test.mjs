/**
 * Self-test of the orchestrator: the kit's own folder is left out of the scans when it lives in
 * the project, Prettier falls back to the project's ignores, and the Size Limit step finds its
 * plugin and the project's build output from a project that declares no Size Limit dependency.
 * The design-system semgrep rules are loaded only when the project supplies its facts.
 */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { kitPathIn, parseArguments, plan } from './run.mjs';

const KIT = path.dirname(fileURLToPath(import.meta.url));
const RUN = path.join(KIT, 'run.mjs');

let scratch;

before(() => {
  scratch = mkdtempSync(path.join(os.tmpdir(), 'ts-lint-kit-run-'));
  mkdirSync(path.join(scratch, 'dist', 'assets'), { recursive: true });
  writeFileSync(
    path.join(scratch, 'dist', 'index.html'),
    '<script type="module" src="/assets/app.js"></script>\n',
  );
  writeFileSync(path.join(scratch, 'dist', 'assets', 'app.js'), 'export const answer = 42;\n');
});

after(() => {
  rmSync(scratch, { recursive: true, force: true });
});

test('a kit folder inside the project is excluded from the scans', () => {
  const project = path.dirname(path.dirname(path.dirname(KIT)));
  const inside = kitPathIn(project);
  assert.equal(inside, path.relative(project, KIT).split(path.sep).join('/'));
  const steps = plan(project, { sizeLimit: false });
  assert.ok(steps.eslint().command.includes(`${inside}/**`));
  assert.ok(steps.stylelint().command.includes(`${inside}/**`));
  assert.ok(steps.prettier().command.includes(`!${inside}/**`));
  assert.ok(steps.semgrep().command.includes(inside));
});

test('a kit folder outside the project adds no exclusion', () => {
  assert.equal(kitPathIn(scratch), null);
  const steps = plan(scratch, { sizeLimit: false });
  assert.ok(!steps.eslint().command.includes('--ignore-pattern'));
  assert.ok(steps.prettier().command.some((part) => part.endsWith('prettierignore.project')));
  assert.equal(steps['size-limit'](), null);
  assert.equal(steps.tsc(), null);
});

test('the project own .prettierignore replaces the kit template', () => {
  writeFileSync(path.join(scratch, '.prettierignore'), 'out/\n');
  try {
    const command = plan(scratch, { sizeLimit: false }).prettier().command;
    assert.ok(command.includes(path.join(scratch, '.prettierignore')));
    assert.ok(!command.some((part) => part.endsWith('prettierignore.project')));
  } finally {
    rmSync(path.join(scratch, '.prettierignore'));
  }
});

test('every stylelint ignore glob follows its own flag and the css glob is the only positional', () => {
  const { command } = plan(scratch, { sizeLimit: false }).stylelint();
  const args = command.slice(command.indexOf('--allow-empty-input') + 1);
  const positional = args.filter(
    (part, index) => part !== '--ignore-pattern' && args[index - 1] !== '--ignore-pattern',
  );
  assert.deepEqual(positional, ['**/*.css']);
  assert.ok(args.includes('**/.venv/**'));
  assert.equal(args.filter((part) => part === '--ignore-pattern').length, 2);
});

test('arguments: unknown steps and flags are refused', () => {
  assert.throws(() => parseArguments(['--only', 'nope']), /unknown step/u);
  assert.throws(() => parseArguments(['--bogus']), /unknown argument/u);
  assert.equal(parseArguments(['--size-limit']).sizeLimit, true);
});

test('the design-system rules are off by default and built on request', () => {
  const configs = (command) =>
    command.flatMap((part, index) => (command[index - 1] === '--config' ? [part] : []));
  const withoutFacts = plan(scratch, { sizeLimit: false }).semgrep();
  assert.deepEqual(configs(withoutFacts.command), [path.join(KIT, 'semgrep')]);
  const shipped = readdirSync(path.join(KIT, 'semgrep'));
  assert.ok(!shipped.some((name) => name.startsWith('ds-')));
  assert.ok(!shipped.includes('banned-terms.yml'));

  const factsFile = path.join(KIT, 'design-system.example.json');
  assert.equal(parseArguments(['--design-system', factsFile]).designSystem, factsFile);
  assert.throws(() => parseArguments(['--design-system']), /needs a value/u);
  const withFacts = plan(scratch, { sizeLimit: false, designSystem: factsFile }).semgrep();
  try {
    const built = configs(withFacts.command).filter((dir) => dir !== path.join(KIT, 'semgrep'));
    assert.equal(built.length, 1);
    assert.ok(readdirSync(built[0]).includes('ds-components.yml'));
  } finally {
    withFacts.cleanup();
  }
});

test('the size-limit step runs from the kit against the project build output', () => {
  const run = (environment) =>
    spawnSync(
      process.execPath,
      [RUN, '--project', scratch, '--only', 'size-limit', '--size-limit'],
      {
        cwd: scratch,
        encoding: 'utf8',
        env: { ...process.env, ...environment },
      },
    );
  const within = run({});
  assert.equal(within.status, 0, within.stdout + within.stderr);
  const over = run({ SIZE_LIMIT_ENTRY_JS_B: '1' });
  assert.equal(over.status, 1, over.stdout + over.stderr);
});
