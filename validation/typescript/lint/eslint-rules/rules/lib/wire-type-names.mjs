/**
 * The schema names an API publishes, read from the project's own API description: the
 * openapi-typescript output in `generatedDir` when it is present, else the OpenAPI document at
 * `openapi` (JSON). Each source yields the keys of `components.schemas`.
 *
 * openapi-typescript declares one `interface components { schemas: { <Name>: ... } }`, not one
 * top-level declaration per schema, so the names are read from those schema keys with the
 * TypeScript compiler API. When neither source yields a name the loader throws: an empty set would
 * make the rule a permanent no-op instead of surfacing a missing or broken API description.
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import ts from 'typescript';

/** Every property key of `interface components { schemas: {...} }` in one output file. */
function schemaNames(sourceFile) {
  const names = new Set();
  const visit = (node) => {
    if (ts.isInterfaceDeclaration(node) && node.name.text === 'components') {
      for (const member of node.members) {
        if (
          ts.isPropertySignature(member) &&
          member.name.getText(sourceFile) === 'schemas' &&
          member.type &&
          ts.isTypeLiteralNode(member.type)
        ) {
          for (const schemaMember of member.type.members) {
            if (ts.isPropertySignature(schemaMember)) {
              const key = schemaMember.name;
              if (ts.isIdentifier(key) || ts.isStringLiteral(key)) names.add(key.text);
            }
          }
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return names;
}

function namesFromGeneratedDir(directory) {
  if (!existsSync(directory)) return undefined;
  const files = readdirSync(directory).filter((file) => file.endsWith('.ts'));
  const names = new Set();
  for (const file of files) {
    const absolute = path.join(directory, file);
    const text = readFileSync(absolute, 'utf8');
    const source = ts.createSourceFile(absolute, text, ts.ScriptTarget.Latest, true);
    for (const name of schemaNames(source)) names.add(name);
  }
  return names.size > 0 ? names : undefined;
}

function namesFromOpenApi(file) {
  if (!existsSync(file)) return undefined;
  const document = JSON.parse(readFileSync(file, 'utf8'));
  const schemas = document?.components?.schemas;
  const names = schemas && typeof schemas === 'object' ? new Set(Object.keys(schemas)) : new Set();
  return names.size > 0 ? names : undefined;
}

/**
 * @param {{ root?: string, generatedDir?: string, openapi?: string }} source paths relative to root.
 * @returns {Set<string>}
 */
export function loadWireTypeNames({ root = process.cwd(), generatedDir, openapi }) {
  const fromGenerated =
    generatedDir === undefined
      ? undefined
      : namesFromGeneratedDir(path.resolve(root, generatedDir));
  if (fromGenerated) return fromGenerated;
  const fromDocument =
    openapi === undefined ? undefined : namesFromOpenApi(path.resolve(root, openapi));
  if (fromDocument) return fromDocument;
  throw new Error(
    `no-hand-written-wire-types: no schema names found -- neither "${generatedDir ?? '(no generatedDir)'}" nor "${openapi ?? '(no openapi)'}" yielded a "components.schemas" entry.`,
  );
}
