/**
 * `local/no-reexport-package`: the exports of a shared package (generated API types, a contracts
 * package) are imported from that package by every consumer, never re-exported from a local
 * module. A local re-export is a second import path that hides where a type comes from and
 * outlives the package's own renames.
 *
 * Reports `export { X } from 'pkg'`, `export * from 'pkg'`, and `export { X }` of a binding
 * imported from the package. The package's own sources are exempt through `ownPaths`.
 *
 * Options: `{ packages: ['@acme/api-types'], ownPaths: ['/packages/api-types/'] }`.
 */

function normalized(path) {
  return (path ?? '').replaceAll('\\', '/');
}

export const rule = {
  meta: {
    type: 'problem',
    docs: { description: 'Do not re-export what a shared package exports.' },
    schema: [
      {
        type: 'object',
        properties: {
          packages: { type: 'array', items: { type: 'string' } },
          ownPaths: { type: 'array', items: { type: 'string' } },
        },
        required: ['packages'],
        additionalProperties: false,
      },
    ],
    messages: {
      reexport:
        "Do not re-export '{{name}}' from {{source}}. Consumers import it from {{source}} directly.",
    },
  },
  create(context) {
    const packages = new Set(context.options[0]?.packages ?? []);
    const ownPaths = context.options[0]?.ownPaths ?? [];
    const filename = normalized(context.filename);
    if (packages.size === 0 || ownPaths.some((part) => filename.includes(part))) return {};

    const imported = new Map();
    const report = (node, name, source) =>
      context.report({ node, messageId: 'reexport', data: { name, source } });

    return {
      ImportDeclaration(node) {
        if (!packages.has(node.source.value)) return;
        for (const specifier of node.specifiers) {
          if (specifier.type === 'ImportSpecifier') {
            imported.set(specifier.local.name, node.source.value);
          }
        }
      },
      ExportNamedDeclaration(node) {
        if (node.source != null && packages.has(node.source.value)) {
          for (const specifier of node.specifiers) {
            report(
              specifier,
              specifier.exported.name ?? specifier.exported.value,
              node.source.value,
            );
          }
          return;
        }
        if (node.source != null) return;
        for (const specifier of node.specifiers) {
          const local = specifier.local.name ?? specifier.local.value;
          const source = imported.get(local);
          if (source !== undefined) {
            report(specifier, specifier.exported.name ?? specifier.exported.value, source);
          }
        }
      },
      ExportAllDeclaration(node) {
        if (packages.has(node.source.value)) report(node, '*', node.source.value);
      },
    };
  },
};
