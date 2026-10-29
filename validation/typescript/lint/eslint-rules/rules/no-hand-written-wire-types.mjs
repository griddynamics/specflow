/**
 * `local/no-hand-written-wire-types`: API types are generated from the API description; a local
 * `interface` or `type` alias whose name is a schema the API publishes is a hand-written mirror of
 * a server model, and it drifts from the generated one.
 *
 * The schema names come from the project's own API description (`./lib/wire-type-names.mjs`):
 * the generated openapi-typescript output, else the OpenAPI document. They are read once per
 * distinct option set, not per linted file. The generated directory itself is never reported.
 *
 * Options: `{ root, generatedDir: 'src/generated', openapi: 'openapi.json', importFrom: '@acme/api-types' }`
 * (paths relative to `root`; at least one of generatedDir / openapi).
 */
import path from 'node:path';

import { loadWireTypeNames } from './lib/wire-type-names.mjs';

const cache = new Map();

function namesFor(options) {
  const key = JSON.stringify([options.root, options.generatedDir, options.openapi]);
  let names = cache.get(key);
  if (names === undefined) {
    names = loadWireTypeNames(options);
    cache.set(key, names);
  }
  return names;
}

export const rule = {
  meta: {
    type: 'problem',
    docs: { description: 'Do not hand-write a type whose name is a generated API schema.' },
    schema: [
      {
        type: 'object',
        properties: {
          root: { type: 'string' },
          generatedDir: { type: 'string' },
          openapi: { type: 'string' },
          importFrom: { type: 'string' },
        },
        additionalProperties: false,
      },
    ],
    messages: {
      duplicate:
        "'{{name}}' is a schema the API publishes; import the generated type{{from}} instead of redeclaring it.",
    },
  },
  create(context) {
    const options = { root: context.cwd, ...context.options[0] };
    if (options.generatedDir !== undefined) {
      const generated = path.resolve(options.root, options.generatedDir) + path.sep;
      if (path.resolve(context.filename).startsWith(generated)) return {};
    }
    const names = namesFor(options);
    const from = options.importFrom === undefined ? '' : ` from '${options.importFrom}'`;
    return {
      'TSInterfaceDeclaration, TSTypeAliasDeclaration'(node) {
        if (names.has(node.id.name)) {
          context.report({
            node: node.id,
            messageId: 'duplicate',
            data: { name: node.id.name, from },
          });
        }
      },
    };
  },
};
