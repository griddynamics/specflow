/**
 * Small filesystem helpers shared by the check scripts: recursive file
 * listing that skips build output and dependencies (Node stdlib only), and
 * the CLI argument parser.
 */

import { existsSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

// `.next` is the Next.js build output (generated, gitignored CSS/JS chunks that
// inline third-party custom properties); like `dist` it is not source to check.
const DEFAULT_SKIP = new Set(['node_modules', 'dist', '.next', '.git', 'coverage', '.reports']);

/**
 * Every file under `root` (recursive), skipping `DEFAULT_SKIP` directory
 * names, as absolute paths.
 * @param {string} root
 * @param {{ extensions?: string[] }} [opts] filter by extension (with dot), e.g. ['.ts', '.tsx']
 */
export function listFiles(root, opts = {}) {
  if (!existsSync(root)) return [];
  const out = [];
  const stack = [root];
  while (stack.length) {
    const dir = stack.pop();
    for (const name of readdirSync(dir)) {
      if (DEFAULT_SKIP.has(name)) continue;
      const full = path.join(dir, name);
      const st = statSync(full);
      if (st.isDirectory()) {
        stack.push(full);
      } else if (!opts.extensions || opts.extensions.some((ext) => name.endsWith(ext))) {
        out.push(full);
      }
    }
  }
  return out.sort();
}

/** Parses `--flag=value` / `--flag value` style CLI args into an object;
 * bare positional args go under `_`. */
export function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg.startsWith('--')) {
      const eq = arg.indexOf('=');
      if (eq !== -1) {
        out[arg.slice(2, eq)] = arg.slice(eq + 1);
      } else if (i + 1 < argv.length && !argv[i + 1].startsWith('--')) {
        out[arg.slice(2)] = argv[++i];
      } else {
        out[arg.slice(2)] = true;
      }
    } else {
      out._.push(arg);
    }
  }
  return out;
}

/** `root`-relative POSIX path of `abs`. */
export function relPath(root, abs) {
  return path.relative(root, abs).split(path.sep).join('/');
}
