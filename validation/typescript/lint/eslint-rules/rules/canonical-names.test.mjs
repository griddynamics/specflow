import { fileURLToPath } from 'node:url';
import { RuleTester } from '../rule-tester-setup.mjs';
import { rule as noModuleBindingAlias } from './no-module-binding-alias.mjs';
import { rule as noRenamedExport } from './no-renamed-export.mjs';
import barrelFiles from 'eslint-plugin-barrel-files';
import tseslint from 'typescript-eslint';
import { builtinRules } from 'eslint/use-at-your-own-risk';

const javascript = new RuleTester({
  languageOptions: { ecmaVersion: 'latest', sourceType: 'module' },
});
javascript.run('local/no-module-binding-alias', noModuleBindingAlias, {
  valid: [
    'const LIMIT = 5; export const SEARCH_LIMIT = LIMIT;',
    'const record = { id: 1 }; const selected = record;',
    'function parse() {} function use() { const local = parse; return local(); }',
    'class Record {} function construct() { const local = Record; return new local(); }',
    'export const parse = () => 1;',
  ],
  invalid: [
    { code: 'class Record {} const OldRecord = Record;', errors: [{ messageId: 'alias' }] },
    { code: 'function parse() {} const oldParse = parse;', errors: [{ messageId: 'alias' }] },
    {
      code: 'const parse = () => 1; export const oldParse = parse;',
      errors: [{ messageId: 'alias' }],
    },
    {
      code: 'const Record = class {}; const OldRecord = Record;',
      errors: [{ messageId: 'alias' }],
    },
  ],
});
const typed = new RuleTester({
  languageOptions: {
    parser: tseslint.parser,
    parserOptions: {
      project: './tsconfig.json',
      tsconfigRootDir: fileURLToPath(new URL('./fixtures/', import.meta.url)),
      disallowAutomaticSingleRunInference: true,
    },
  },
});
typed.run('local/no-module-binding-alias: imported runtime functions', noModuleBindingAlias, {
  valid: [
    {
      code: 'import { useState } from "react"; export function useRecord() { return useState(0); }',
      filename: fileURLToPath(new URL('./fixtures/typed-fixture.tsx', import.meta.url)),
    },
    {
      code: 'type RecordId = string; const LIMIT = 5; const SEARCH_LIMIT = LIMIT;',
      filename: fileURLToPath(new URL('./fixtures/typed-fixture.tsx', import.meta.url)),
    },
  ],
  invalid: [
    {
      code: 'import { useState } from "react"; export const useOldState = useState;',
      filename: fileURLToPath(new URL('./fixtures/typed-fixture.tsx', import.meta.url)),
      errors: [{ messageId: 'alias' }],
    },
  ],
});
javascript.run('local/no-renamed-export', noRenamedExport, {
  valid: ['export { Record } from "./record";', 'const record = {}; export { record };'],
  invalid: [
    { code: 'export { Record as OldRecord } from "./record";', errors: [{ messageId: 'renamed' }] },
    {
      code: 'const record = {}; export { record as oldRecord };',
      errors: [{ messageId: 'renamed' }],
    },
    { code: 'export { default as Record } from "./record";', errors: [{ messageId: 'renamed' }] },
  ],
});
const typescript = new RuleTester({ languageOptions: { parser: tseslint.parser } });
typescript.run('barrel-files/avoid-re-export-all', barrelFiles.rules['avoid-re-export-all'], {
  valid: ['export type { Record } from "./record";', 'export interface Record { id: string; }'],
  invalid: [{ code: 'export * from "./record";', errors: 1 }],
});
typescript.run(
  'no-restricted-syntax: type-star barrels',
  builtinRules.get('no-restricted-syntax'),
  {
    valid: [
      {
        code: 'export type { Record } from "./record";',
        options: ["ExportAllDeclaration[exportKind='type']"],
      },
    ],
    invalid: [
      {
        code: 'export type * from "./record";',
        options: ["ExportAllDeclaration[exportKind='type']"],
        errors: 1,
      },
    ],
  },
);
typescript.run(
  'no-warning-comments: compatibility declarations',
  builtinRules.get('no-warning-comments'),
  {
    valid: [
      '// Legacy Office file formats are parsed and validated.',
      '// The failed command is kept for its retry notice.',
      '// A deprecated SDK method must never be called.',
    ],
    invalid: [
      {
        code: '// Backward compatibility for old callers.',
        options: [{ terms: ['backward compatibility'], location: 'anywhere' }],
        errors: 1,
      },
      {
        code: '// A legacy alias remains.',
        options: [{ terms: ['legacy alias'], location: 'anywhere' }],
        errors: 1,
      },
      {
        code: '// Kept for compatibility with old consumers.',
        options: [{ terms: ['kept for compatibility'], location: 'anywhere' }],
        errors: 1,
      },
    ],
  },
);
