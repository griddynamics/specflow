#!/usr/bin/env node
/**
 * `semgrep.mjs`: runs a Semgrep rule directory over a target directory and
 * reports every result in the shared finding contract.
 *
 * It refuses to certify a tree it did not fully scan: a rule that cannot be
 * loaded (it would silence its file-mates too), a target scanned only
 * partially (timeout, too big, syntax error), output that is not JSON, or a
 * run with no scanned targets is a tool error (exit 2), never "no findings".
 *
 * The binary is `$SEMGREP_BIN` when set, else this track's own `.venv/bin/semgrep`
 * (`uv sync` in the track folder), else `semgrep` on `PATH`.
 *
 * A default path (`.semgrep`, `src`) that is absent skips the check; a path named
 * with `--config` or `--target` that is absent is a tool error (exit 2).
 *
 * usage: node semgrep.mjs [--root <dir>] [--config <rules dir or file>] [--target <dir>]
 *   --config  default `.semgrep` under the root
 *   --target  default `src` under the root
 */

import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

import { finding } from './lib/report.mjs';
import { runScript, isMainModule, stringOption } from './lib/main.mjs';
import { relPath } from './lib/files.mjs';

const TRACK_SEMGREP = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  '.venv',
  'bin',
  'semgrep',
);

export function semgrepBin() {
  if (process.env.SEMGREP_BIN) return process.env.SEMGREP_BIN;
  return existsSync(TRACK_SEMGREP) ? TRACK_SEMGREP : 'semgrep';
}

function pathsOf(root, options) {
  return {
    configPath: path.resolve(root, stringOption(options, 'config', '.semgrep')),
    targetPath: path.resolve(root, stringOption(options, 'target', 'src')),
  };
}

/** The first missing input, as `{ text, explicit }`; `explicit` is true when
 * the path was named with `--config` or `--target` rather than defaulted. */
function missingInput({ root, options }) {
  const { configPath, targetPath } = pathsOf(root, options);
  if (!existsSync(configPath)) {
    return {
      text: `${relPath(root, configPath)} does not exist`,
      explicit: options.config !== undefined,
    };
  }
  if (!existsSync(targetPath)) {
    return {
      text: `${relPath(root, targetPath)} does not exist`,
      explicit: options.target !== undefined,
    };
  }
  return null;
}

export function check({ root, options = {} }) {
  const { configPath, targetPath } = pathsOf(root, options);
  const missing = missingInput({ root, options });
  if (missing?.explicit)
    throw new Error(`semgrep input named on the command line: ${missing.text}`);
  if (missing) return [];
  const binary = semgrepBin();
  const environment = path.isAbsolute(binary)
    ? {
        ...process.env,
        PATH: `${path.dirname(binary)}${path.delimiter}${process.env.PATH ?? ''}`,
      }
    : process.env;
  const proc = spawnSync(
    binary,
    [
      '--metrics=off',
      '--disable-version-check',
      '--config',
      configPath,
      '--timeout',
      '0',
      '--json',
      relPath(root, targetPath) || '.',
    ],
    {
      cwd: root,
      env: environment,
      encoding: 'utf8',
      maxBuffer: 64 * 1024 * 1024,
    },
  );

  if (proc.error) {
    throw new Error(`semgrep could not be run: ${proc.error.message}`);
  }
  // semgrep: 0 clean, 1 findings (only with `--error`), >=2 tool/config error.
  if (proc.status !== 0 && proc.status !== 1) {
    throw new Error(`semgrep exited ${proc.status}: ${(proc.stderr || '').trim()}`);
  }

  let data;
  try {
    data = JSON.parse(proc.stdout);
  } catch (err) {
    throw new Error(`semgrep did not return JSON: ${err.message}`);
  }

  // A rule that cannot be loaded or parsed reports nothing for ever, and it
  // silences its file-mates too. That is a config error, not zero findings.
  const fatal = (data.errors || []).filter((e) => e.level === 'error');
  if (fatal.length > 0) {
    throw new Error(
      `semgrep reported ${fatal.length} configuration error(s); the rules in ` +
        `${relPath(root, configPath)} did not all run: ${fatal.map((e) => e.message).join(' | ')}`,
    );
  }

  const degraded = (data.errors || []).filter(
    (e) => e.level !== 'error' && /timeout|too big|partial|syntax error/i.test(JSON.stringify(e)),
  );
  if (degraded.length > 0) {
    throw new Error(
      `semgrep did not scan ${degraded.length} target(s) completely, so this run cannot ` +
        `certify the tree: ${degraded.map((e) => e.message || e.type).join(' | ')}`,
    );
  }

  if (!Array.isArray(data.paths?.scanned) || data.paths.scanned.length === 0) {
    throw new Error('semgrep returned no scanned targets; this run cannot certify the tree');
  }

  return (data.results || []).map((r) =>
    finding(
      r.path,
      r.start?.line || 1,
      r.start?.col || 1,
      r.check_id.split('.').pop() || r.check_id,
      r.extra?.message?.trim().split('\n')[0] || r.check_id,
    ),
  );
}

function skipIf({ root, options }) {
  const missing = missingInput({ root, options });
  return missing && !missing.explicit ? `no targets: ${missing.text}` : null;
}

if (isMainModule(import.meta.url)) runScript(check, { skipIf });
