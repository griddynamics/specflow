/**
 * Locates a CLI binary installed by npm: `node_modules/.bin/<name>` of the
 * first directory at or above each search root (npm hoists binaries to the
 * nearest workspace root). The kit's own folder is a search root too, so a
 * tool the kit declares in its `package.json` is found even when the target
 * project does not install it.
 */

import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/** The kit folder (two levels above `checks/lib/`). */
export const KIT_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

/** `node_modules/.bin/<name>` at or above `root`, else `null`. */
export function resolveBin(name, root) {
  for (let dir = path.resolve(root); ; dir = path.dirname(dir)) {
    const candidate = path.join(dir, 'node_modules', '.bin', name);
    if (existsSync(candidate)) return candidate;
    if (path.dirname(dir) === dir) return null;
  }
}

/** The first hit of `resolveBin` over `roots`, in order, else `null`. */
export function resolveBinIn(name, roots) {
  for (const root of roots) {
    const found = resolveBin(name, root);
    if (found) return found;
  }
  return null;
}
