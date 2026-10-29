import assert from 'node:assert/strict';
import test from 'node:test';

import { collectTranslationCalls, resolvedDynamicKeys } from './i18n-calls.mjs';
import { ts, walk } from './ts.mjs';

test('translation analysis collects indirect literal evidence in the same AST walk', () => {
  const source = ts.createSourceFile(
    'fixture.tsx',
    [
      "const config = { labelKey: 'nav.indirect' };",
      'const copy = `common.template`;',
      'const view = <div title="aria.label">literal text</div>;',
      "t('direct.key');",
      't(`dynamic.${value}`);',
      't(`multi.${first}.${second}`);',
      "const duplicate = 'nav.indirect';",
      "other('other.literal');",
    ].join('\n'),
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const nodes = [...walk(source)];
  const visits = new Map();
  for (const node of nodes) {
    const getChildren = node.getChildren.bind(node);
    node.getChildren = (...args) => {
      visits.set(node, (visits.get(node) ?? 0) + 1);
      return getChildren(...args);
    };
  }

  const { staticCalls, dynamicCalls, literalValues } = collectTranslationCalls([source], null);

  assert.deepEqual(
    [...literalValues],
    ['nav.indirect', 'common.template', 'aria.label', 'direct.key', 'other.literal'],
  );
  assert.deepEqual(
    staticCalls.map((call) => call.key),
    ['direct.key'],
  );
  assert.deepEqual(
    dynamicCalls.map(({ prefix, shape, members }) => ({ prefix, shape, members })),
    [
      { prefix: 'dynamic.', shape: 'dynamic. ', members: null },
      { prefix: 'multi.', shape: 'multi. . ', members: null },
    ],
  );
  assert.deepEqual(resolvedDynamicKeys(dynamicCalls), []);
  assert.equal(visits.size, nodes.length);
  assert.ok([...visits.values()].every((count) => count === 1));
});
