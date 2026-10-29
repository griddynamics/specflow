// The clone engine of the duplicates check as a filter, for a caller that prepares its own
// sources (the Python kit's `clones` check blanks imports and test oracles before comparing).
//
// stdin, one JSON object:
//   { "minLines": 10, "minTokens": 100, "mode": "weak",
//     "sources": [{ "id": "src/a.py", "format": "python", "source": "..." }, ...] }
// stdout, one JSON object, the sources compared in the order given (each against itself and
// every earlier one):
//   { "scanned": 2, "clones": [{ "format": "python", "tokens": 120,
//       "a": { "id": "src/b.py", "start": { "line": 3, "column": 1 }, "end": { ... } },
//       "b": { "id": "src/a.py", "start": { ... }, "end": { ... } } }] }
//
// Exit: 0 the comparison ran (clones or not: the caller judges them), 2 bad input or an
// engine error, with the reason on stderr.
import { realpathSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { createDetector } from './clone-detector.mjs';

const MODES = ['strict', 'mild', 'weak'];

function positiveInteger(value, key) {
  if (!Number.isInteger(value) || value <= 0)
    throw new TypeError(`\`${key}\` must be a positive integer`);
  return value;
}

/** The validated request: detector options and the sources to compare. */
export function parseRequest(text) {
  const request = JSON.parse(text);
  if (request === null || typeof request !== 'object' || Array.isArray(request))
    throw new TypeError('the request must be a JSON object');
  const minLines = positiveInteger(request.minLines, 'minLines');
  const minTokens = positiveInteger(request.minTokens, 'minTokens');
  if (!MODES.includes(request.mode))
    throw new TypeError(`\`mode\` must be one of ${MODES.join(', ')}`);
  if (!Array.isArray(request.sources)) throw new TypeError('`sources` must be a list');
  for (const entry of request.sources) {
    if (
      entry === null ||
      typeof entry !== 'object' ||
      typeof entry.id !== 'string' ||
      typeof entry.format !== 'string' ||
      typeof entry.source !== 'string'
    )
      throw new TypeError('each source needs a string `id`, `format` and `source`');
  }
  return { minLines, minTokens, mode: request.mode, sources: request.sources };
}

function location(duplication) {
  return {
    id: duplication.sourceId,
    start: { line: duplication.start.line, column: duplication.start.column ?? 1 },
    end: { line: duplication.end.line, column: duplication.end.column ?? 1 },
  };
}

/** Every clone among `request.sources`, in the order the engine finds them. */
export async function detectSources(request) {
  const detector = createDetector(request);
  const clones = [];
  for (const entry of request.sources) {
    for (const clone of await detector.detect(entry.id, entry.source, entry.format)) {
      clones.push({
        format: clone.format,
        tokens: clone.duplicationA.end.position - clone.duplicationA.start.position,
        a: location(clone.duplicationA),
        b: location(clone.duplicationB),
      });
    }
  }
  return { scanned: request.sources.length, clones };
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf8');
}

const entry = process.argv[1] ? realpathSync(process.argv[1]) : '';
if (entry === realpathSync(fileURLToPath(import.meta.url))) {
  try {
    const result = await detectSources(parseRequest(await readStdin()));
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } catch (error) {
    process.stderr.write(`tool-error: clone engine: ${error?.message ?? error}\n`);
    process.exitCode = 2;
  }
}
