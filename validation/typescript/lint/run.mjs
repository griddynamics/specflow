#!/usr/bin/env node
/**
 * Runs the whole TypeScript lint track against a project: Prettier, ESLint, stylelint, tsc, knip,
 * semgrep and (opt-in) Size Limit. Every tool is the kit's own install (`npm install` in this
 * folder; semgrep through `uv` with this folder's pyproject.toml).
 *
 * Usage: node run.mjs [--project <dir>] [--only <step,...>] [--skip <step,...>] [--size-limit]
 *                     [--design-system <facts.json>]
 *   steps: prettier, eslint, stylelint, tsc, knip, semgrep, size-limit
 *
 * Config resolution, per tool: the project's own config file when it has one (it usually extends
 * the kit's, see kit.config.example.mjs), else the kit's default:
 *   eslint.config.mjs, stylelint.config.mjs, knip.json, .prettierrc, size-limit.config.mjs,
 *   tsconfig.json (tsc runs only when the project has one).
 * Prettier ignores: the project's `.prettierignore` when it has one, else its `.gitignore` plus
 * the kit's `prettierignore.project` (build output, dependencies, reports).
 * semgrep runs every rule file in ./semgrep (and the project's own `semgrep/` folder when present).
 * The design-system rules (templates/ds-*.yml.tmpl) are off until the project supplies its facts:
 * `--design-system <facts.json>` builds them into a temporary folder for the run, or the project
 * keeps the built `ds-*.yml` files in its own `semgrep/` folder
 * (`node scripts/build-ds-rules.mjs <facts.json> <project>/semgrep`).
 *
 * The kit may live inside the project (for example `validation/typescript/lint/`): its own folder
 * is left out of the Prettier, ESLint, stylelint and semgrep scans. Add it to the `ignore` list of
 * the project's knip config as well.
 *
 * Size Limit runs from the kit folder (it finds its plugins from the package.json above its
 * working directory) with `SIZE_LIMIT_DIST` resolved against the project, `dist` by default.
 *
 * Exit status: 0 when every step passes, 1 when a step reports findings, 2 on a usage error.
 */
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { buildDesignSystemRules } from './scripts/build-ds-rules.mjs';

const KIT = path.dirname(fileURLToPath(import.meta.url));
const BIN = path.join(KIT, 'node_modules', '.bin');
export const STEPS = ['prettier', 'eslint', 'stylelint', 'tsc', 'knip', 'semgrep', 'size-limit'];

export function parseArguments(argv) {
  const options = {
    project: process.cwd(),
    only: null,
    skip: new Set(),
    sizeLimit: false,
    designSystem: null,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    const value = () => {
      index += 1;
      if (argv[index] === undefined) throw new Error(`${argument} needs a value`);
      return argv[index];
    };
    if (argument === '--project') options.project = path.resolve(value());
    else if (argument === '--only') options.only = new Set(value().split(','));
    else if (argument === '--skip') options.skip = new Set(value().split(','));
    else if (argument === '--size-limit') options.sizeLimit = true;
    else if (argument === '--design-system') options.designSystem = path.resolve(value());
    else throw new Error(`unknown argument ${argument}`);
  }
  for (const step of [...(options.only ?? []), ...options.skip]) {
    if (!STEPS.includes(step)) throw new Error(`unknown step ${step}; steps: ${STEPS.join(', ')}`);
  }
  return options;
}

/** The project's own file when present, else the kit's. */
function configFor(project, ...names) {
  for (const name of names) {
    const own = path.join(project, name);
    if (existsSync(own)) return own;
  }
  return path.join(KIT, names[0]);
}

/** The kit's folder relative to the project, with forward slashes; null when it is not inside. */
export function kitPathIn(project) {
  const relative = path.relative(project, KIT);
  if (relative === '' || relative.startsWith('..') || path.isAbsolute(relative)) return null;
  return relative.split(path.sep).join('/');
}

function prettierIgnoreArguments(project) {
  const own = path.join(project, '.prettierignore');
  if (existsSync(own)) return ['--ignore-path', own];
  const gitignore = path.join(project, '.gitignore');
  return [
    ...(existsSync(gitignore) ? ['--ignore-path', gitignore] : []),
    '--ignore-path',
    path.join(KIT, 'prettierignore.project'),
  ];
}

/**
 * Each step as `{ command, cwd, env }` (null when the step does not apply).
 *
 * @param {string} project
 * @param {{sizeLimit: boolean, designSystem?: string | null}} options
 */
export function plan(project, options) {
  const projectSemgrep = path.join(project, 'semgrep');
  const kit = kitPathIn(project);
  const inProject = (command) => ({ command, cwd: project, env: process.env });
  return {
    prettier: () =>
      inProject([
        path.join(BIN, 'prettier'),
        '--check',
        '--config',
        configFor(project, '.prettierrc'),
        ...prettierIgnoreArguments(project),
        '.',
        ...(kit === null ? [] : [`!${kit}/**`]),
      ]),
    eslint: () =>
      inProject([
        path.join(BIN, 'eslint'),
        '--config',
        // project: frontend keeps eslint.config.mjs for `npm run lint` (eslint-config-next); the kit config is eslint.kit.config.mjs.
        configFor(project, 'eslint.kit.config.mjs', 'eslint.config.mjs'),
        '--no-inline-config',
        '--report-unused-disable-directives-severity',
        'error',
        '--max-warnings',
        '0',
        ...(kit === null ? [] : ['--ignore-pattern', `${kit}/**`]),
        '.',
      ]),
    stylelint: () =>
      inProject([
        path.join(BIN, 'stylelint'),
        '--config',
        configFor(project, 'stylelint.config.mjs'),
        '--allow-empty-input',
        '--ignore-pattern',
        '**/node_modules/**',
        '--ignore-pattern',
        '**/.venv/**',
        ...(kit === null ? [] : ['--ignore-pattern', `${kit}/**`]),
        '**/*.css',
      ]),
    tsc: () =>
      existsSync(path.join(project, 'tsconfig.json'))
        ? inProject([path.join(BIN, 'tsc'), '--noEmit', '-p', path.join(project, 'tsconfig.json')])
        : null,
    knip: () =>
      inProject([
        path.join(BIN, 'knip'),
        '--directory',
        project,
        '--config',
        configFor(project, 'knip.json'),
        '--no-progress',
      ]),
    semgrep: () => {
      const facts = options.designSystem ?? null;
      const designSystemRules =
        facts === null ? null : mkdtempSync(path.join(os.tmpdir(), 'ts-lint-kit-ds-'));
      if (designSystemRules !== null) buildDesignSystemRules(facts, designSystemRules);
      return {
        ...inProject([
          'uv',
          'run',
          '--project',
          KIT,
          'semgrep',
          'scan',
          '--error',
          '--metrics=off',
          '--disable-version-check',
          '--config',
          path.join(KIT, 'semgrep'),
          ...(existsSync(projectSemgrep) ? ['--config', projectSemgrep] : []),
          ...(designSystemRules === null ? [] : ['--config', designSystemRules]),
          '--exclude',
          'semgrep/fixtures',
          ...(kit === null ? [] : ['--exclude', kit]),
          project,
        ]),
        cleanup: () => {
          if (designSystemRules === null) return;
          rmSync(designSystemRules, { recursive: true, force: true });
        },
      };
    },
    'size-limit': () =>
      options.sizeLimit
        ? {
            command: [
              path.join(BIN, 'size-limit'),
              '--config',
              configFor(project, 'size-limit.config.mjs'),
            ],
            cwd: KIT,
            env: {
              ...process.env,
              SIZE_LIMIT_DIST: path.resolve(project, process.env['SIZE_LIMIT_DIST'] ?? 'dist'),
            },
          }
        : null,
  };
}

function main() {
  let options;
  try {
    options = parseArguments(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    return 2;
  }
  const commands = plan(options.project, options);
  const results = [];
  for (const step of STEPS) {
    if (options.only !== null && !options.only.has(step)) continue;
    if (options.skip.has(step)) continue;
    const invocation = commands[step]();
    if (invocation === null) {
      results.push([step, 'skipped']);
      continue;
    }
    const { command, cwd, env, cleanup } = invocation;
    process.stdout.write(`\n== ${step}: ${command.join(' ')}\n`);
    const run = spawnSync(command[0], command.slice(1), { cwd, env, stdio: 'inherit' });
    cleanup?.();
    if (run.error !== undefined) {
      results.push([step, `not run (${run.error.message})`]);
    } else {
      results.push([step, run.status === 0 ? 'pass' : `fail (exit ${String(run.status)})`]);
    }
  }
  process.stdout.write('\n== summary\n');
  for (const [step, outcome] of results) process.stdout.write(`${step.padEnd(11)} ${outcome}\n`);
  return results.every(([, outcome]) => outcome === 'pass' || outcome === 'skipped') ? 0 : 1;
}

if (
  process.argv[1] !== undefined &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  process.exitCode = main();
}
