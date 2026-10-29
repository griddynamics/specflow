// The clone scan behind `duplicates.mjs`. Scans every tracked and untracked
// (not ignored) file of the git work tree under the current directory, or of
// one `--project <dir>` inside it, with the native jscpd engine
// (`./clone-detector.mjs`), and prints one finding per clone:
//
//   <file>:<line>:1: jscpd-clone: duplicates <other file>:<line>
//
// Configuration (`minLines`, `minTokens`, `mode`, `formats`, `ignore` globs) is
// read from `.jscpd.json` in the current directory, else from this kit's
// `jscpd.json`. `VALIDATION_REPORTS_DIR`, when set, also receives a JSON report.
//
// Exit: 0 no clone, 1 clones, 2 no supported source scanned or a bad argument.
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { createDetector, implementationSource } from './clone-detector.mjs';
import { projectArgument, projectFiles } from './clone-scope.mjs';

const require = createRequire(import.meta.url);
const { getFormatByFile } = require('@jscpd/tokenizer');

const root = process.cwd();
let project;
try {
  project = projectArgument(process.argv.slice(2));
  if (
    project !== null &&
    (!existsSync(path.join(root, project)) || !statSync(path.join(root, project)).isDirectory())
  )
    throw new TypeError(`project directory does not exist: ${project}`);
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exit(2);
}

const localConfig = path.join(root, '.jscpd.json');
const configPath = existsSync(localConfig)
  ? localConfig
  : fileURLToPath(new URL('../jscpd.json', import.meta.url));
const config = JSON.parse(readFileSync(configPath, 'utf8'));

// `git ls-files` run from `root` lists paths relative to `root`.
const files = execFileSync(
  'git',
  ['ls-files', '--cached', '--others', '--exclude-standard', '-z'],
  { cwd: root, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 },
)
  .split('\0')
  .filter(Boolean);
const inputs = projectFiles(files, project).filter(
  (file) =>
    existsSync(path.join(root, file)) &&
    !(config.ignore ?? []).some((pattern) => path.matchesGlob(file, pattern)),
);

const detector = createDetector(config);
const clones = [];
let scanned = 0;
for (const file of [...new Set(inputs)].sort()) {
  const format = getFormatByFile(file);
  if (!format || !config.formats.includes(format)) continue;
  const source = implementationSource(file, readFileSync(path.join(root, file), 'utf8'));
  clones.push(...(await detector.detect(file, source, format)));
  scanned += 1;
}

if (process.env.VALIDATION_REPORTS_DIR) {
  const directory = path.resolve(process.env.VALIDATION_REPORTS_DIR);
  mkdirSync(directory, { recursive: true });
  writeFileSync(
    path.join(
      directory,
      project === null ? 'duplicates.json' : `duplicates-${project.replaceAll('/', '-')}.json`,
    ),
    JSON.stringify({ project, scanned, clones }, null, 2),
  );
}
for (const clone of clones) {
  process.stdout.write(
    `${clone.duplicationA.sourceId}:${clone.duplicationA.start.line}:1: jscpd-clone: duplicates ${clone.duplicationB.sourceId}:${clone.duplicationB.start.line}\n`,
  );
}
process.stdout.write(`jscpd: ${scanned} files, ${clones.length} clones\n`);
if (scanned === 0) process.stderr.write(`no supported sources found in project: ${project}\n`);
process.exitCode = scanned === 0 ? 2 : Number(clones.length > 0);
