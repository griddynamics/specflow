/**
 * The asset lists the payload budgets measure, read from the build output: the JavaScript the
 * entry document loads (its module scripts and module preloads) and every JavaScript file the
 * build emits.
 */
import { readdirSync } from 'node:fs';
import path from 'node:path';

/** The local module scripts and module preloads of an `index.html`, as paths under `dist`. */
export function entryAssetPaths(html, dist) {
  const references = [
    ...html.matchAll(/<script\b[^>]*\btype=["']module["'][^>]*\bsrc=["']([^"']+)["']/gu),
    ...html.matchAll(/<script\b[^>]*\bsrc=["']([^"']+)["'][^>]*\btype=["']module["']/gu),
    ...html.matchAll(/<link\b[^>]*\brel=["']modulepreload["'][^>]*\bhref=["']([^"']+)["']/gu),
  ].map((match) => match[1]);
  const local = references.filter((reference) => !/^[a-z]+:\/\//iu.test(reference));
  return [...new Set(local)].map((reference) => path.join(dist, reference.replace(/^\//u, '')));
}

/** Every `.js` file under the build output. */
export function totalJavaScriptPaths(dist) {
  return readdirSync(dist, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith('.js'))
    .map((entry) => path.join(entry.parentPath, entry.name));
}

/** A budget in bytes from the environment, with a default; refuses anything but whole bytes. */
export function budget(environment, name, fallback) {
  const raw = environment[name] ?? String(fallback);
  if (!/^\d+$/u.test(raw)) throw new Error(`${name} must be a whole number of bytes, got "${raw}"`);
  return `${raw} B`;
}
