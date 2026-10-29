import path from 'node:path';

/** The `--project <dir>` selector of the duplicates scan: `null` for the
 * whole inventory, else a normalised directory inside the scanned root. */
export function projectArgument(argv) {
  if (argv.length === 0) return null;
  if (argv.length !== 2 || argv[0] !== '--project')
    throw new TypeError('usage: duplicates.mjs [--project <root-relative directory>]');
  const project = argv[1];
  if (!project || path.isAbsolute(project) || project.split(/[\\/]/u).includes('..'))
    throw new TypeError('project must be a directory inside the scanned root');
  return path.posix.normalize(project).replace(/\/$/u, '');
}

/** The files of `files` inside `project` (directory boundaries, not prefixes). */
export function projectFiles(files, project) {
  if (project === null || project === '.') return files;
  return files.filter((file) => file.startsWith(`${project}/`));
}
