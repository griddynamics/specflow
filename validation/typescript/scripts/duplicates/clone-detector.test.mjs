import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createDetector, implementationSource } from './clone-detector.mjs';

const body = Array.from(
  { length: 18 },
  (_, index) => `  accumulator = accumulator + value${String(index)};`,
).join('\n');

test('cross-file and within-file copies fail at the configured clone size', async () => {
  const detector = createDetector();
  const source = `function calculate() {\n${body}\nreturn accumulator;\n}\n`;
  assert.equal((await detector.detect('first.js', source, 'javascript')).length, 0);
  assert.ok((await detector.detect('second.js', source, 'javascript')).length > 0);
  const local = createDetector();
  assert.ok((await local.detect('local.js', source + '\n' + source, 'javascript')).length > 0);
});

test('Cypher uses its own grammar and reports copies', async () => {
  const detector = createDetector();
  const query =
    Array.from(
      { length: 18 },
      (_, index) => `OPTIONAL MATCH (n)-[:REL${String(index)}]->(value${String(index)})`,
    ).join('\n') + '\nRETURN n';
  await detector.detect('first.cypher', query, 'cypher');
  assert.ok((await detector.detect('second.cypher', query, 'cypher')).length > 0);
});

test('independent assertions are omitted but fixture implementations remain compared', () => {
  const source = 'const fixture = { value: 3 };\nexpect(fixture.value).toBe(3);\n';
  const result = implementationSource('example.test.ts', source);
  assert.ok(result.includes('const fixture'));
  assert.ok(!result.includes('toBe'));
});

test('assertion-only waitFor callbacks are omitted while actions remain compared', async () => {
  const oracle = `await waitFor(() => {\nexpect(screen.getByRole('button')).toBeVisible();\nexpect(result).toEqual(expected);\n});`;
  assert.ok(!implementationSource('example.test.ts', oracle).includes('waitFor'));
  const action = `await waitFor(() => {\n${body}\nexpect(result).toEqual(expected);\n});`;
  const detector = createDetector();
  await detector.detect(
    'first.test.ts',
    implementationSource('first.test.ts', action),
    'typescript',
  );
  assert.ok(
    (
      await detector.detect(
        'second.test.ts',
        implementationSource('second.test.ts', action),
        'typescript',
      )
    ).length > 0,
  );
});

test('native ESLint boilerplate does not exempt custom rule configurations', async () => {
  const source = `export default [{\nextends: [...tseslint.configs.strictTypeChecked, ...tseslint.configs.stylisticTypeChecked],\nrules: { 'project/check': function enforce() {\n${body}\nreturn accumulator;\n} },\n}];`;
  const first = implementationSource('first/eslint.config.mjs', source);
  const second = implementationSource('second/eslint.config.mjs', source);
  assert.ok(first.includes('project/check'));
  assert.ok(!first.includes('strictTypeChecked'));
  const detector = createDetector();
  await detector.detect('first/eslint.config.mjs', first, 'javascript');
  assert.ok((await detector.detect('second/eslint.config.mjs', second, 'javascript')).length > 0);
});

test('custom ESLint presets and actions wrapped in assertions are preserved', () => {
  const config = `export default [{ extends: customPreset, rules: { 'custom/check': ${JSON.stringify(body)} } }];`;
  assert.equal(implementationSource('example/eslint.config.mjs', config), config);
  const action = `await waitFor(() => { expect(client.post('/records')).toBeDefined(); });`;
  assert.ok(implementationSource('example.test.ts', action).includes('client.post'));
});
