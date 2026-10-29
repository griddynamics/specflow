#!/usr/bin/env node
/**
 * Kit runner: runs the static-validation checks of this folder against one
 * target root and, on request, the target's own `tsc`, `eslint`, `knip`
 * (also in `--production` mode as `knip-production`), `prettier`, `stylelint`
 * and the duplicates scan. Prints every finding as `path:line:col: rule:
 * message` on stdout, one status line per step on stderr, and exits with the worst step status:
 * 0 clean, 1 findings, 2 tool or configuration error.
 *
 * usage: node run.mjs [--root <dir>] [--config <file.json>] [--only <step,...>]
 *
 * Config file (all keys optional; see validation.config.example.json):
 *   {
 *     "root": "../app",                         // relative to the config file
 *     "checks": { "<check>": { "<option>": "value" } | false },
 *     "tools": ["tsc", "eslint", "knip", "knip-production", "prettier", "stylelint", "duplicates"],
 *     "binDirs": ["../lint"]                     // relative to the config file
 *   }
 * A tool binary is looked up in `node_modules/.bin` at or above the root,
 * then at or above each `binDirs` entry, then in this kit's own folder.
 * Without a config file the default checks run with their default options.
 * A check listed in `checks` runs (with its options); `false` disables it.
 * `--only` restricts the run to the named steps (checks or tools).
 */

import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { resolveBin, resolveBinIn } from './checks/lib/bin.mjs';
import { parseArgs } from './checks/lib/files.mjs';
import {
  parseContractFindings,
  parseEslintJson,
  parseKnipJson,
  parsePrettier,
  parseTsc,
  renderFinding,
} from './checks/lib/parsers.mjs';

const KIT = path.dirname(fileURLToPath(import.meta.url));
const MAX_BUFFER = 64 * 1024 * 1024;

/** Every check script of `checks/`, in run order: `bundle-size` comes before the
 * checks that read `dist/`, because it can run the build. */
export const CHECKS = [
  'html-scripts',
  'i18n-catalogues',
  'i18n-usage',
  'i18n-protected',
  'tokens',
  'contrast',
  'css-usage',
  'bundle-size',
  'dist-policy',
  'api-types-only',
  'api-contract-map',
  'nav-tree',
  'next-routes',
  'next-boundaries',
  'semgrep',
];
/** The checks that run without a config: each one skips, rather than fails,
 * when its input is absent. The others need a build, a generator run, a rules
 * directory or an adopter-supplied spec file and are opted into through the
 * config or `--only`. */
export const DEFAULT_CHECKS = [
  'html-scripts',
  'i18n-catalogues',
  'i18n-usage',
  'i18n-protected',
  'tokens',
  'contrast',
  'css-usage',
];
export const TOOLS = [
  'tsc',
  'eslint',
  'knip',
  'knip-production',
  'prettier',
  'stylelint',
  'duplicates',
];

/** @typedef {{path:string,line:number,col:number,rule:string,message:string,fix?:string,doc?:string}} Finding */
/** @typedef {{step:string,exit:number,findings:Finding[],detail:string}} StepResult */

/** `ok | findings | skipped | tool-error`. */
export function statusFor(result) {
  if (result.detail && result.detail.startsWith('skipped:')) return 'skipped';
  if (result.exit === 2) return 'tool-error';
  return result.exit === 1 ? 'findings' : 'ok';
}

export function aggregateExit(results) {
  return results.reduce((worst, r) => Math.max(worst, r.exit), 0);
}

export { resolveBin };

/** Directories searched for tool binaries after the target root: the
 * config's `binDirs` (for example the folder of the lint kit, where ESLint,
 * knip, Prettier and stylelint are installed), then this kit's own folder. */
let binSearchDirs = [KIT];

export function setBinDirs(dirs) {
  binSearchDirs = [...dirs, KIT];
}

function spawn(argv, cwd) {
  const proc = spawnSync(argv[0], argv.slice(1), { cwd, encoding: 'utf8', maxBuffer: MAX_BUFFER });
  if (proc.error && proc.error.code === 'ENOENT') return null;
  return proc;
}

function firstLine(text) {
  return (
    text
      .split('\n')
      .map((line) => line.trim())
      .find(Boolean) ?? ''
  );
}

/**
 * Runs an external tool and parses its structured output. `diagnosticStatuses`
 * names the exit codes meaning "the tool ran and may have printed
 * diagnostics"; any other status is a tool error, and output is never mined
 * for findings then. A non-zero status whose output parses to no finding is a
 * tool error too: never a fabricated finding, never a silent pass.
 */
export function cmdStep(name, argv, { root, parser, diagnosticStatuses = [0, 1] }) {
  const bin = resolveBinIn(argv[0], [root, ...binSearchDirs]);
  const proc = spawn(bin ? [bin, ...argv.slice(1)] : argv, root);
  if (proc === null)
    return { step: name, exit: 2, findings: [], detail: `tool-missing: ${argv[0]}` };
  const stdout = proc.stdout ?? '';
  const stderr = proc.stderr ?? '';
  if (!diagnosticStatuses.includes(proc.status)) {
    const detail = firstLine(stdout + stderr) || `${argv[0]} exited ${proc.status ?? 'null'}`;
    return { step: name, exit: 2, findings: [], detail: `tool-error: ${detail}` };
  }
  const findings = parser(stdout, stderr);
  if (findings === null)
    return {
      step: name,
      exit: 2,
      findings: [],
      detail: `tool-error: unparseable ${argv[0]} output`,
    };
  if (proc.status !== 0 && findings.length === 0) {
    return {
      step: name,
      exit: 2,
      findings: [],
      detail: `tool-error: exit ${proc.status} with no parsed findings`,
    };
  }
  return { step: name, exit: findings.length ? 1 : 0, findings, detail: '' };
}

/** The command line flags of a check's options object. */
export function optionFlags(options = {}) {
  return Object.entries(options).map(([key, value]) =>
    Array.isArray(value) ? `--${key}=${value.join(',')}` : `--${key}=${value}`,
  );
}

/** Runs one `checks/<name>.mjs` script, which prints the contract itself. */
export function checkStep(name, root, options = {}) {
  const script = path.join(KIT, 'checks', `${name}.mjs`);
  const proc = spawn([process.execPath, script, `--root=${root}`, ...optionFlags(options)], root);
  if (proc === null) return { step: name, exit: 2, findings: [], detail: 'tool-missing: node' };
  const stdout = proc.stdout ?? '';
  const stderr = proc.stderr ?? '';
  const exit = [0, 1, 2].includes(proc.status) ? proc.status : 2;
  const detail =
    exit === 2 || stderr.startsWith('skipped:')
      ? firstLine(stderr) || `${name} exited ${proc.status ?? 'null'}`
      : '';
  return { step: name, exit, findings: parseContractFindings(stdout), detail };
}

function stylelintFindings(stdout, stderr) {
  let rows;
  try {
    rows = JSON.parse(stdout.trim() || stderr.trim() || '[]');
  } catch {
    return null;
  }
  if (!Array.isArray(rows)) return null;
  return rows.flatMap((row) =>
    (row.warnings ?? []).map((warning) => ({
      path: path.isAbsolute(row.source ?? '')
        ? path.relative(process.cwd(), row.source)
        : row.source,
      line: warning.line,
      col: warning.column,
      rule: warning.rule,
      message: warning.text,
    })),
  );
}

const TOOL_STEPS = {
  // `tsc -b` exits 2 on a normal "diagnostics present, outputs generated" run.
  tsc: (root) =>
    cmdStep('tsc', ['tsc', '-b'], { root, parser: parseTsc, diagnosticStatuses: [0, 1, 2] }),
  eslint: (root) =>
    cmdStep(
      'eslint',
      [
        'eslint',
        '.',
        '--no-inline-config',
        '--report-unused-disable-directives-severity',
        'error',
        '--max-warnings',
        '0',
        '--no-warn-ignored',
        '--format',
        'json',
      ],
      { root, parser: (stdout) => parseEslintJson(stdout, root) },
    ),
  knip: (root) => cmdStep('knip', ['knip', '--reporter', 'json'], { root, parser: parseKnipJson }),
  // Production mode: code and dependencies that only tests or dev tooling use.
  'knip-production': (root) =>
    cmdStep('knip-production', ['knip', '--reporter', 'json', '--production'], {
      root,
      parser: parseKnipJson,
    }),
  prettier: (root) =>
    cmdStep('prettier', ['prettier', '--check', '.'], { root, parser: parsePrettier }),
  // stylelint exits 2 when it reports problems.
  stylelint: (root) =>
    cmdStep('stylelint', ['stylelint', '**/*.css', '--formatter', 'json', '--allow-empty-input'], {
      root,
      parser: stylelintFindings,
      diagnosticStatuses: [0, 2],
    }),
  duplicates: (root) =>
    cmdStep('duplicates', [process.execPath, path.join(KIT, 'duplicates', 'duplicates.mjs')], {
      root,
      parser: (stdout) => parseContractFindings(stdout),
    }),
};

/** The resolved run plan: `{ root, steps: [{ name, kind, options }] }`. */
export function plan(argv, cwd = process.cwd()) {
  const args = parseArgs(argv);
  const unknown = Object.keys(args).filter((key) => !['_', 'root', 'config', 'only'].includes(key));
  if (unknown.length || args._.length)
    throw new TypeError(
      `unknown arguments: ${[...unknown.map((k) => `--${k}`), ...args._].join(' ')}`,
    );

  let config = {};
  let configDir = cwd;
  if (typeof args.config === 'string') {
    const configPath = path.resolve(cwd, args.config);
    config = JSON.parse(readFileSync(configPath, 'utf8'));
    configDir = path.dirname(configPath);
  }
  const root =
    typeof args.root === 'string'
      ? path.resolve(cwd, args.root)
      : path.resolve(configDir, config.root ?? '.');

  const configured = config.checks ?? {};
  for (const name of Object.keys(configured))
    if (!CHECKS.includes(name)) throw new TypeError(`unknown check in config: ${name}`);
  const tools = config.tools ?? [];
  for (const name of tools)
    if (!TOOLS.includes(name)) throw new TypeError(`unknown tool in config: ${name}`);
  const binDirs = config.binDirs ?? [];
  if (!Array.isArray(binDirs) || !binDirs.every((dir) => typeof dir === 'string'))
    throw new TypeError('binDirs in config must be an array of directory paths');

  const enabled = new Set(DEFAULT_CHECKS);
  for (const [name, value] of Object.entries(configured)) {
    if (value === false) enabled.delete(name);
    else enabled.add(name);
  }
  let steps = [
    ...CHECKS.filter((name) => enabled.has(name)).map((name) => ({
      name,
      kind: 'check',
      options: configured[name] || {},
    })),
    ...TOOLS.filter((name) => tools.includes(name)).map((name) => ({ name, kind: 'tool' })),
  ];
  if (typeof args.only === 'string') {
    const only = args.only.split(',').filter(Boolean);
    const bad = only.filter((name) => !CHECKS.includes(name) && !TOOLS.includes(name));
    if (bad.length) throw new TypeError(`unknown steps: ${bad.join(', ')}`);
    steps = only.map((name) =>
      CHECKS.includes(name)
        ? { name, kind: 'check', options: configured[name] || {} }
        : { name, kind: 'tool' },
    );
  }
  return { root, steps, binDirs: binDirs.map((dir) => path.resolve(configDir, dir)) };
}

export function runPlan({ root, steps, binDirs = [] }) {
  setBinDirs(binDirs);
  const results = [];
  for (const step of steps) {
    const result =
      step.kind === 'check'
        ? checkStep(step.name, root, step.options)
        : TOOL_STEPS[step.name](root);
    for (const f of result.findings) console.log(renderFinding(f));
    const suffix = result.detail ? ` -- ${result.detail}` : '';
    console.error(`[${result.step}] ${statusFor(result)}${suffix}`);
    results.push(result);
  }
  return results;
}

function main(argv) {
  let resolved;
  try {
    resolved = plan(argv);
  } catch (err) {
    console.error(`run.mjs: ${err.message}`);
    console.error('usage: node run.mjs [--root <dir>] [--config <file.json>] [--only <step,...>]');
    return 2;
  }
  if (!existsSync(resolved.root)) {
    console.error(`run.mjs: root does not exist: ${resolved.root}`);
    return 2;
  }
  const results = runPlan(resolved);
  const exit = aggregateExit(results);
  const total = results.reduce((n, r) => n + r.findings.length, 0);
  console.error(`${results.length} steps, ${total} findings, exit ${exit}`);
  return exit;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = main(process.argv.slice(2));
}
