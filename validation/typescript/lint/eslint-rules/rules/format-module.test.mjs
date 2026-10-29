// RuleTester cases of the layout options of `no-unformatted-number`, `no-display-list-join` (the
// formatters live in `src/lib/text-format.ts`, not at the default path) and
// `no-data-hook-in-presentational` (the data layer lives in `data-access/*`).
import { fileURLToPath } from 'node:url';

import { RuleTester } from '../rule-tester-setup.mjs';
import { rule as noDataHookInPresentational } from './no-data-hook-in-presentational.mjs';
import { rule as noDisplayListJoin } from './no-display-list-join.mjs';
import { rule as noUnformattedNumber } from './no-unformatted-number.mjs';

const FIXTURES_DIR = fileURLToPath(new URL('./fixtures/', import.meta.url));
const FILE = `${FIXTURES_DIR}typed-fixture.tsx`;

const jsx = new RuleTester({
  languageOptions: {
    ecmaVersion: 2022,
    sourceType: 'module',
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
});

const DATA_LAYER = [{ apiModulePattern: '(^|/)data-access/[A-Za-z0-9-]+$' }];
const LEAF =
  'function ItemLeaf({ item, listId }) {\n' +
  '  const save = useSaveItem(listId);\n' +
  '  return <div onClick={save.mutate}>{item.id}</div>;\n' +
  '}';

jsx.run(
  'local/no-data-hook-in-presentational with a configured data layer',
  noDataHookInPresentational,
  {
    valid: [
      // The default `api/*` path is not the data layer once the pattern is set.
      {
        code: "import { useSaveItem } from '../../api/items';\n" + LEAF,
        options: DATA_LAYER,
      },
    ],
    invalid: [
      {
        code: "import { useSaveItem } from '../../data-access/items';\n" + LEAF,
        options: DATA_LAYER,
        errors: [{ messageId: 'presentational' }],
      },
    ],
  },
);

const typed = new RuleTester({
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

const OPTIONS = [
  {
    formatModule: '(?:^|/)lib/text-format\\.ts$',
    moduleName: 'lib/text-format.ts',
    formatters: { number: 'renderNumber', list: 'renderList', inline: 'renderList' },
  },
];

const PRELUDE =
  "import { renderNumber, renderList, LIST_SEPARATOR } from './src/lib/text-format';\n" +
  'declare const item: { count: number };\n' +
  'declare const codes: string[];\n';

typed.run('local/no-unformatted-number with a configured format module', noUnformattedNumber, {
  valid: [
    {
      code: PRELUDE + "const el = <li>{renderNumber(item.count, 'en')}</li>;",
      options: OPTIONS,
      filename: FILE,
    },
  ],
  invalid: [
    {
      code: PRELUDE + 'const el = <li>{item.count}</li>;',
      options: OPTIONS,
      filename: FILE,
      errors: [
        { messageId: 'raw', data: { formatter: 'renderNumber', module: 'lib/text-format.ts' } },
      ],
    },
    {
      // Without options the message names the default formatter and module.
      code: PRELUDE + 'const el = <li>{item.count}</li>;',
      filename: FILE,
      errors: [
        { messageId: 'raw', data: { formatter: 'formatNumber', module: 'the format module' } },
      ],
    },
  ],
});

typed.run('local/no-display-list-join with a configured format module', noDisplayListJoin, {
  valid: [
    {
      code: PRELUDE + "const el = <p>{renderList(codes, 'en')}</p>;",
      options: OPTIONS,
      filename: FILE,
    },
    {
      code: PRELUDE + 'const el = <p title={codes.join(LIST_SEPARATOR)} />;',
      options: OPTIONS,
      filename: FILE,
    },
  ],
  invalid: [
    {
      code: PRELUDE + "const el = <p>{codes.join(', ')}</p>;",
      options: OPTIONS,
      filename: FILE,
      errors: [
        {
          messageId: 'join',
          data: { list: 'renderList', inline: 'renderList', module: 'lib/text-format.ts' },
        },
      ],
    },
    {
      // The default module path does not match, so the separator is display text.
      code: PRELUDE + 'const el = <p title={codes.join(LIST_SEPARATOR)} />;',
      filename: FILE,
      errors: [{ messageId: 'join' }],
    },
  ],
});
