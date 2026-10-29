/**
 * Parsers that turn a tool's own structured output into findings of the one
 * finding shape every check and the runner share:
 * `{ path, line, col, rule, message, fix?, doc? }`.
 *
 * Each parser reads the tool's documented machine format (`knip --reporter
 * json`, `eslint --format json`, `tsc`'s `path(line,col)` lines, `node --test`
 * failure blocks, `prettier --check` warnings) and never fabricates a finding
 * out of output it cannot read.
 */
import path from 'node:path';

const TSC_RE = /^(.+?)\((\d+),(\d+)\): (error|warning) (TS\d+): (.+)$/;

/** Findings from `knip --reporter json`, or `null` when the output is not knip JSON. */
export function parseKnipJson(stdout) {
  let data;
  try {
    data = JSON.parse(stdout.trim());
  } catch {
    return null; // unparseable: never fabricate a finding out of this
  }
  if (!data || !Array.isArray(data.issues)) return null;
  const findings = [];
  const SKIP_KEYS = new Set(['file', 'owners']);
  for (const row of data.issues) {
    const file = row.file ?? 'knip';
    for (const [key, items] of Object.entries(row)) {
      if (SKIP_KEYS.has(key) || !Array.isArray(items)) continue;
      for (const item of items) {
        if (Array.isArray(item)) {
          const names = item
            .map((s) => (typeof s === 'string' ? s : (s?.symbol ?? '')))
            .filter(Boolean);
          findings.push({
            path: file,
            line: 1,
            col: 1,
            rule: `knip-${key}`,
            message: names.join(' -> ') || key,
          });
          continue;
        }
        const name = item?.name ?? item?.symbol ?? '';
        findings.push({
          path: file,
          line: item?.line ?? 1,
          col: item?.col ?? 1,
          rule: `knip-${key}`,
          message: name ? `unused ${key}: ${name}` : `unused ${key}`,
        });
      }
    }
  }
  return findings;
}

/** Findings from `eslint --format json`; paths are made relative to `cwd`. */
export function parseEslintJson(stdout, cwd = process.cwd()) {
  let results;
  try {
    results = JSON.parse(stdout);
  } catch {
    return [];
  }
  if (!Array.isArray(results)) return [];
  const findings = [];
  for (const fileResult of results) {
    const filePath = fileResult.filePath ? path.relative(cwd, fileResult.filePath) : 'eslint';
    for (const msg of fileResult.messages ?? []) {
      findings.push({
        path: filePath,
        line: msg.line ?? 1,
        col: msg.column ?? 1,
        rule: msg.ruleId || (msg.fatal ? 'eslint-parse-error' : 'eslint-error'),
        message: msg.message,
      });
    }
  }
  return findings;
}

/** Findings from `tsc`'s `path(line,col): error TSxxxx: message` lines. */
export function parseTsc(stdout, stderr) {
  const findings = [];
  for (const line of (stdout + stderr).split('\n')) {
    const m = TSC_RE.exec(line.trim());
    if (m)
      findings.push({
        path: m[1],
        line: Number(m[2]),
        col: Number(m[3]),
        rule: m[5],
        message: m[6],
      });
  }
  return findings;
}

const TEST_LOCATION_RE = /^test at (.+):(\d+):(\d+)$/;
const FAILURE_TITLE_RE = /^✖ (.+?) \([\d.]+m?s\)$/;

/** Findings from the `node --test` spec reporter's failing-test blocks:
 *   test at <path>:<line>:<col>
 *   ✖ <title> (<duration>)
 *     <error message> */
export function parseNodeFailures(output, { repoRoot, cwd = repoRoot }) {
  const lines = output.split('\n');
  const findings = [];
  for (let i = 0; i < lines.length - 1; i++) {
    const loc = TEST_LOCATION_RE.exec(lines[i].trim());
    if (!loc) continue;
    const title = FAILURE_TITLE_RE.exec(lines[i + 1].trim());
    if (!title) continue;
    const messageLines = [];
    for (let j = i + 2; j < lines.length; j++) {
      const trimmed = lines[j].trim();
      if (TEST_LOCATION_RE.test(trimmed)) break;
      if (!trimmed && messageLines.length) break;
      if (trimmed) messageLines.push(trimmed);
    }
    findings.push({
      path: path.relative(repoRoot, path.resolve(cwd, loc[1])).split(path.sep).join('/'),
      line: Number(loc[2]),
      col: Number(loc[3]),
      rule: 'self-tests-failed',
      message: messageLines[0] ? `${title[1]}: ${messageLines[0]}` : title[1],
    });
  }
  return findings;
}

/** One finding as one line: `path:line:col: rule: message (fix: ...; doc: ...)`. */
export function renderFinding(f) {
  const tail = [f.fix ? `fix: ${f.fix}` : '', f.doc ? `doc: ${f.doc}` : ''].filter(Boolean);
  const suffix = tail.length ? ` (${tail.join('; ')})` : '';
  return `${f.path}:${f.line}:${f.col}: ${f.rule}: ${f.message}${suffix}`;
}

const FINDING_RE = /^(.+?):(\d+):(\d+): (\S+): (.+)$/;

/** Parses `renderFinding` lines back into findings; other lines are ignored. */
export function parseContractFindings(text) {
  const findings = [];
  for (const line of text.split('\n')) {
    const match = FINDING_RE.exec(line);
    if (match)
      findings.push({
        path: match[1],
        line: Number(match[2]),
        col: Number(match[3]),
        rule: match[4],
        message: match[5],
      });
  }
  return findings;
}

const ANSI_ESCAPE = new RegExp(`${String.fromCodePoint(27)}\\[[0-?]*[ -/]*[@-~]`, 'gu');

/** Findings from `prettier --check`: one per `[warn] <path>` line. */
export function parsePrettier(stdout, stderr) {
  const findings = [];
  const re = /^\[warn]\s+(.+)$/;
  // stdout and stderr are independent streams and neither is required to end
  // in a newline. Keep an explicit boundary so `Checking formatting...` can
  // never swallow stderr's first `[warn] <path>` line.
  for (const line of `${stdout}\n${stderr}`.split('\n')) {
    const trimmed = line.replace(ANSI_ESCAPE, '').trim();
    if (trimmed.includes('Code style issues found')) continue;
    const m = re.exec(trimmed);
    if (m)
      findings.push({
        path: m[1],
        line: 1,
        col: 1,
        rule: 'prettier',
        message: 'not formatted',
        fix: 'prettier --write',
      });
  }
  return findings;
}
