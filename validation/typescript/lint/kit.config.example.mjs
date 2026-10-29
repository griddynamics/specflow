// Example project ESLint config: copy it to the project root as `eslint.config.mjs`, fix the
// import path, and replace the example facts (an orders app with an `@acme/api-types` package)
// with the project's own. Every option is listed; delete the ones the project does not need.
// Paths are globs relative to `root`, which must be the project directory: it is taken from this
// file's location (the project root once copied there), not from the working directory, so the
// config applies whatever cwd ESLint is started from.
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { defineKitConfig } from './eslint.config.mjs';

export default defineKitConfig({
  root: path.dirname(fileURLToPath(import.meta.url)),
  ignores: ['**/generated/**'],

  // Plain Node tooling scripts and configs.
  node: { files: ['**/*.{js,mjs}'], cjs: ['**/*.cjs'] },

  // The React + TanStack application.
  app: {
    src: 'src',
    testSupport: ['tests/**/*.{ts,tsx}'],
    apiDir: 'src/api',
    apiClient: 'src/api/client.ts',
    storage: 'src/utils/storage.ts',
    entryPoints: ['src/main.tsx'],
    router: 'src/router.tsx',
    components: 'src/components',
    pages: 'src/pages',
    localeModules: ['src/utils/**/*.{ts,tsx}', 'src/i18n*/**/*.{ts,tsx}'],
    // Off unless true. Needs the formatter module named by designSystem.formatModule below.
    localisation: true,
    literalStrings: false,
    fastRefresh: false,
    reactVersion: '18.3.1',
    layers: [
      {
        type: 'pages',
        pattern: 'src/pages/*',
        allow: ['pages', 'components', 'hooks', 'api', 'shared'],
      },
      {
        type: 'components',
        pattern: 'src/components/*',
        allow: ['components', 'hooks', 'api', 'shared'],
      },
      { type: 'hooks', pattern: 'src/hooks', allow: ['hooks', 'api', 'shared'] },
      { type: 'api', pattern: 'src/api', allow: ['api', 'shared'] },
      { type: 'shared', pattern: 'src/{utils,styles,locales,context}', allow: ['shared'] },
    ],

    // Generated API types: imported with `import type` only, never re-exported, never redeclared.
    typeOnlyPackages: ['@acme/api-types'],
    reexportPackages: ['@acme/api-types'],
    wireTypes: {
      openapi: 'api/openapi.json',
      generatedDir: 'api-types/src/generated',
      importFrom: '@acme/api-types',
    },

    // Every useMutation declares meta.feedback: { success, failure }.
    mutationFeedback: { metaKey: 'feedback', keys: ['success', 'failure'] },
    // A raw rating number is shown through its badge, never as text.
    rawMetric: { namePattern: 'rating', use: 'RatingBadge' },
    // No client threshold over a field when the type serves the verdict as a boolean.
    servedVerdict: {
      fieldPattern: 'score|rank|confidence',
      allowEqualityPairs: [['fromRank', 'toRank']],
    },

    designSystem: {
      primitives: 'src/components/primitives/**/*.{ts,tsx}',
      styleModules: ['src/styles/**/*.{ts,tsx}', 'src/theme/**/*.{ts,tsx}'],
      breakpoints: { widths: [640, 1024, 1280], hook: 'src/hooks/use-breakpoint.ts' },
      lazyPages: { importPattern: '^\\.\\/pages\\/', eager: ['error-page', 'not-found-page'] },
      formGridClass: 'form-grid',
      variantComponents: ['Button', 'IconButton'],
      requiredMarker: true,
      singleScroller: { exempt: ['src/components/primitives/modal.tsx'] },
      pageButtonClass: true,
      pageMargins: [8, 12, 16, 20, 24, 32],
      formatModule: 'src/utils/format.ts',
      wrappedElements: [{ element: 'details', use: 'Disclosure' }],
      wrappedImports: [
        { source: 'acme-ui', names: ['Badge', 'Alert'], use: 'StatusPill / Notice' },
      ],
      capsLabelClass: 'label-caps',
      containmentClass: 'long-list',
      translateNoClass: 'mono',
    },
  },

  // A TypeScript package without React (here: the generated API types package).
  library: {
    files: ['api-types/src/**/*.ts'],
    publicEntry: ['api-types/src/index.ts'],
  },

  // Playwright specs.
  e2e: {
    files: ['e2e/**/*.ts'],
    fixtures: 'e2e/fixtures.ts',
    appSourceImports: ['**/src/**'],
    // Words naming a search/filter/sort control in the UI languages (default: English only).
    searchWords: 'search|filter|find|sort|recherch|trier',
  },
});
