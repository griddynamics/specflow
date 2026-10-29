// Shared harness of the lib/ unit tests: a typed RuleTester over the fixtures project, and the
// declarations the cases build on.
import { fileURLToPath } from 'node:url';
import { RuleTester } from '../../rule-tester-setup.mjs';

const FIXTURES_DIR = fileURLToPath(new URL('../fixtures/', import.meta.url));
export const SAME_FILE = `${FIXTURES_DIR}typed-fixture.tsx`;
export const CROSS_FILE = `${FIXTURES_DIR}repeat-row.tsx`;

export const typed = new RuleTester({
  languageOptions: {
    parser: (await import('typescript-eslint')).parser,
    parserOptions: {
      ecmaFeatures: { jsx: true },
      project: './tsconfig.json',
      tsconfigRootDir: FIXTURES_DIR,
      disallowAutomaticSingleRunInference: true,
    },
  },
});

const PRELUDE =
  'declare const rows: { id: string; name: string }[];\n' +
  'declare const tags: Set<{ name: string }>;\n' +
  'declare const fixed: string;\n' +
  'declare const Button: (props: { onClick?: () => void; children?: unknown }) => null;\n' +
  'declare const Plain: (props: { children?: unknown }) => null;\n';

export const withPrelude = (code, filename = SAME_FILE) => ({ code: PRELUDE + code, filename });
