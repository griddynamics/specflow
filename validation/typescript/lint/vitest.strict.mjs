/**
 * Strict Vitest options: no left-behind `.only`, a suite with zero tests is a config mistake not a
 * pass, mocks, globals and env stubs never leak between tests, run order is shuffled (order-
 * dependent tests fail), no hidden retries, and a console error or warning fails the test
 * (`vitest.setup.mjs`, vitest-fail-on-console). Coverage uses v8; thresholds are opt-in.
 *
 * Usage in the project's vite.config.ts / vitest.config.ts:
 *
 *   import { strictTestOptions } from './validation/typescript/lint/vitest.strict.mjs';
 *   export default defineConfig({
 *     test: strictTestOptions({
 *       include: ['src/**\/*.test.{ts,tsx}'],
 *       setupFiles: ['tests/setup.ts'],
 *       coverageThresholds: { lines: 80, branches: 80, functions: 80, statements: 80 },
 *     }),
 *   });
 */
import { fileURLToPath } from 'node:url';

export const FAIL_ON_CONSOLE_SETUP = fileURLToPath(new URL('./vitest.setup.mjs', import.meta.url));

/**
 * @param {object} [options]
 * @param {string[]} [options.include] test globs.
 * @param {string[]} [options.exclude] extra excluded globs.
 * @param {string[]} [options.setupFiles] the project's own setup files (run after fail-on-console).
 * @param {string} [options.environment] test environment (default `jsdom`).
 * @param {{lines?: number, branches?: number, functions?: number, statements?: number}} [options.coverageThresholds]
 * @param {string[]} [options.coverageExclude] extra globs left out of coverage.
 */
export function strictTestOptions(options = {}) {
  return {
    include: options.include ?? [
      'src/**/*.{test,spec}.{ts,tsx}',
      'tests/**/*.{test,spec}.{ts,tsx}',
    ],
    exclude: ['node_modules/**', 'dist/**', ...(options.exclude ?? [])],
    environment: options.environment ?? 'jsdom',
    setupFiles: [FAIL_ON_CONSOLE_SETUP, ...(options.setupFiles ?? [])],
    allowOnly: false,
    passWithNoTests: false,
    restoreMocks: true,
    unstubGlobals: true,
    unstubEnvs: true,
    sequence: { shuffle: true },
    retry: 0,
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html', 'json-summary'],
      exclude: ['node_modules/**', 'tests/**', '**/*.d.ts', ...(options.coverageExclude ?? [])],
      ...(options.coverageThresholds === undefined
        ? {}
        : { thresholds: options.coverageThresholds }),
    },
  };
}
