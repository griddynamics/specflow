/**
 * Loader for the JSON input files that carry an adopter's own schema or
 * catalogue (an endpoint list, a token list, a list of required strings). The
 * checks that need one read it through here, so a missing or malformed file is
 * always the same configuration error (exit 2), never a clean pass.
 */

import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

/**
 * Reads and parses a JSON input file, resolved against `root`.
 * @param {string} root
 * @param {string} file path relative to `root` (or absolute)
 * @param {string} label the option name, used in error messages
 */
export function readJsonInput(root, file, label) {
  const abs = path.resolve(root, file);
  if (!existsSync(abs)) throw new Error(`${label} does not exist: ${file}`);
  try {
    return JSON.parse(readFileSync(abs, 'utf8'));
  } catch (err) {
    throw new Error(`${label} is not valid JSON: ${file}: ${err.message}`, { cause: err });
  }
}

/** `value` when it is an array, else a configuration error naming `label`. */
export function requireArray(value, label) {
  if (!Array.isArray(value)) throw new TypeError(`${label} must be a JSON array`);
  return value;
}

/** `value` when it is an array of strings, else a configuration error. */
export function requireStringArray(value, label) {
  requireArray(value, label);
  if (!value.every((item) => typeof item === 'string'))
    throw new TypeError(`${label} must contain only strings`);
  return value;
}
