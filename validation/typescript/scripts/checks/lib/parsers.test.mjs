import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import {
  parseContractFindings,
  parseEslintJson,
  parseKnipJson,
  parseNodeFailures,
  parsePrettier,
  parseTsc,
  renderFinding,
} from './parsers.mjs';

describe('renderFinding', () => {
  test('includes fix and doc when present', () => {
    const line = renderFinding({
      path: 'a.ts',
      line: 1,
      col: 2,
      rule: 'r',
      message: 'm',
      fix: 'do X',
      doc: 'https://example.com/rule',
    });
    assert.equal(line, 'a.ts:1:2: r: m (fix: do X; doc: https://example.com/rule)');
  });

  test('omits the parenthetical when both are absent', () => {
    const line = renderFinding({ path: 'a.ts', line: 1, col: 2, rule: 'r', message: 'm' });
    assert.equal(line, 'a.ts:1:2: r: m');
  });
});

describe('parseContractFindings', () => {
  test('parses a well-formed contract line', () => {
    const findings = parseContractFindings('a.ts:3:4: no-explicit-any: avoid any\n');
    assert.deepEqual(findings, [
      { path: 'a.ts', line: 3, col: 4, rule: 'no-explicit-any', message: 'avoid any' },
    ]);
  });

  test('round-trips renderFinding output', () => {
    const f = { path: 'src/x.ts', line: 7, col: 9, rule: 'some-rule', message: 'text' };
    assert.deepEqual(parseContractFindings(renderFinding(f)), [f]);
  });

  test('ignores lines that do not match', () => {
    assert.deepEqual(parseContractFindings('not a finding line\n'), []);
  });
});

describe('parsePrettier', () => {
  test('extracts each unformatted file', () => {
    const findings = parsePrettier(
      'Checking formatting...\n[warn] src/a.ts\n[warn] src/b.ts\n',
      '',
    );
    assert.equal(findings.length, 2);
    assert.equal(findings[0].path, 'src/a.ts');
    assert.equal(findings[0].rule, 'prettier');
  });

  test('skips the trailing summary line', () => {
    const stdout =
      '[warn] src/a.ts\n[warn] Code style issues found in the above file. Run Prettier with --write to fix.\n';
    const findings = parsePrettier(stdout, '');
    assert.equal(findings.length, 1);
    assert.equal(findings[0].path, 'src/a.ts');
  });

  test('a stdout without a trailing newline never swallows the first stderr warning', () => {
    const findings = parsePrettier('Checking formatting...', '[warn] src/a.ts\n');
    assert.deepEqual(
      findings.map((f) => f.path),
      ['src/a.ts'],
    );
  });

  test('no output means no findings', () => {
    assert.deepEqual(parsePrettier('All matched files use Prettier code style!\n', ''), []);
  });
});

describe('parseEslintJson', () => {
  const ESLINT_JSON_OUTPUT = JSON.stringify([
    {
      filePath: '/repo/src/a.tsx',
      messages: [
        {
          ruleId: 'no-var',
          severity: 2,
          message: 'Unexpected var, use let or const instead.',
          line: 1,
          column: 1,
        },
        {
          ruleId: '@typescript-eslint/no-explicit-any',
          severity: 2,
          message: 'Unexpected any. Specify a different type.',
          line: 12,
          column: 5,
        },
      ],
      suppressedMessages: [],
      errorCount: 2,
    },
    {
      filePath: '/repo/src/clean.tsx',
      messages: [],
      suppressedMessages: [],
      errorCount: 0,
    },
  ]);

  test('parses every message, relative to cwd, and excludes clean file results', () => {
    const findings = parseEslintJson(ESLINT_JSON_OUTPUT, '/repo');
    assert.equal(findings.length, 2);
    assert.equal(findings[0].path, 'src/a.tsx');
    assert.equal(findings[0].rule, 'no-var');
    assert.equal(findings[1].rule, '@typescript-eslint/no-explicit-any');
    assert.equal(findings[1].line, 12);
    assert.equal(findings[1].col, 5);
    assert.ok(!findings.some((f) => f.path.includes('clean.tsx')));
  });

  test('a fatal parse error (ruleId: null) falls back to a synthetic rule id', () => {
    const output = JSON.stringify([
      {
        filePath: '/repo/src/bad.tsx',
        messages: [
          {
            ruleId: null,
            fatal: true,
            severity: 2,
            message: 'Parsing error: Unexpected token',
            line: 1,
            column: 11,
          },
        ],
      },
    ]);
    const findings = parseEslintJson(output, '/repo');
    assert.equal(findings.length, 1);
    assert.equal(findings[0].rule, 'eslint-parse-error');
  });

  test('clean output (empty array) is no findings', () => {
    assert.deepEqual(parseEslintJson('[]'), []);
  });

  test('unparseable output does not throw', () => {
    assert.deepEqual(parseEslintJson('not json'), []);
  });
});

describe('parseTsc', () => {
  test('parses the paren-style location', () => {
    const line = 'src/a.ts(10,3): error TS2345: Argument of type X is not assignable.\n';
    assert.deepEqual(parseTsc(line, '')[0], {
      path: 'src/a.ts',
      line: 10,
      col: 3,
      rule: 'TS2345',
      message: 'Argument of type X is not assignable.',
    });
  });

  test('non-matching lines are ignored', () => {
    assert.deepEqual(parseTsc('Found 1 error.\n', ''), []);
  });
});

describe('parseNodeFailures', () => {
  test('parses a spec-reporter failure block into a source finding', () => {
    const output = [
      'test at src/example.test.mjs:12:3',
      '✖ reports the mismatch (1.5ms)',
      '  Expected values to be strictly equal',
      '',
    ].join('\n');
    assert.deepEqual(parseNodeFailures(output, { repoRoot: '/repo', cwd: '/repo' }), [
      {
        path: 'src/example.test.mjs',
        line: 12,
        col: 3,
        rule: 'self-tests-failed',
        message: 'reports the mismatch: Expected values to be strictly equal',
      },
    ]);
  });

  test('passing output yields no findings', () => {
    assert.deepEqual(parseNodeFailures('✔ ok (1ms)\n', { repoRoot: '/repo' }), []);
  });
});

describe("parseKnipJson: knip's own --reporter json", () => {
  const KNIP_JSON_OUTPUT = JSON.stringify({
    issues: [
      { file: 'src/pages/stub-page.tsx', files: [{}] },
      { file: 'src/api/orders.ts', exports: [{ name: 'orderQueryKeys', line: 12, col: 14 }] },
      { file: 'package.json', dependencies: [{ name: 'left-pad' }] },
      { file: 'src/a.ts', cycles: [['src/a.ts', 'src/b.ts']] },
    ],
  });

  test('every issue type across every file becomes one finding', () => {
    const findings = parseKnipJson(KNIP_JSON_OUTPUT);
    assert.equal(findings.length, 4);
    assert.ok(
      findings.some((f) => f.rule === 'knip-files' && f.path === 'src/pages/stub-page.tsx'),
    );
    assert.ok(
      findings.some(
        (f) =>
          f.rule === 'knip-exports' &&
          f.line === 12 &&
          f.col === 14 &&
          f.message.includes('orderQueryKeys'),
      ),
    );
    assert.ok(
      findings.some((f) => f.rule === 'knip-dependencies' && f.message.includes('left-pad')),
    );
    assert.ok(
      findings.some((f) => f.rule === 'knip-cycles' && f.message === 'src/a.ts -> src/b.ts'),
    );
  });

  test('clean output (no issues) is no findings', () => {
    assert.deepEqual(parseKnipJson(JSON.stringify({ issues: [] })), []);
  });

  test('unparseable output is null, never a fabricated finding', () => {
    assert.equal(parseKnipJson('not json'), null);
    assert.equal(parseKnipJson(JSON.stringify({ notIssues: [] })), null);
  });
});
