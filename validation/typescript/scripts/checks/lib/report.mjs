/**
 * The finding/report contract every check script shares.
 *
 * Exit codes: 0 clean, 1 findings, 2 tool or configuration error. A check
 * whose target set is empty is never silent: it prints `skipped: <reason>` on
 * stderr and exits 0.
 *
 * `process.exitCode`, never `process.exit()`: `process.exit()` terminates
 * before Node has flushed a *pipe* (stdout to a pipe is asynchronous), so a
 * script whose output is piped would lose an arbitrary tail of its own
 * findings. Every exit here sets the code and lets Node drain and exit.
 */
import { renderFinding } from './parsers.mjs';

export { renderFinding };

/** @typedef {{path:string, line:number, col:number, rule:string, message:string, fix?:string, doc?:string}} Finding */

/**
 * @param {string} path
 * @param {number} line
 * @param {number} col
 * @param {string} rule
 * @param {string} message
 * @param {{fix?: string, doc?: string}} [extra]
 * @returns {Finding}
 */
export function finding(path, line, col, rule, message, extra = {}) {
  /** @type {Finding} */
  const f = { path, line, col, rule, message };
  if (extra.fix) f.fix = extra.fix;
  if (extra.doc) f.doc = extra.doc;
  return f;
}

/** Prints every finding to stdout and sets exit 1 if there is one, else 0.
 * @param {Finding[]} findings */
export function report(findings) {
  for (const f of findings) console.log(renderFinding(f));
  process.exitCode = findings.length ? 1 : 0;
}

/** An empty target set passes and says so on stderr, never silently. */
export function skip(reason) {
  console.error(`skipped: ${reason}`);
  process.exitCode = 0;
}

/** A tool or config error (a missing input the check cannot run without, an
 * unexpected exception) is exit 2, never a finding. */
export function toolError(message) {
  console.error(`tool-error: ${message}`);
  process.exitCode = 2;
}
