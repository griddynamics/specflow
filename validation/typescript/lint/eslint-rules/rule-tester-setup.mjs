/**
 * Wires ESLint's `RuleTester` to Node's built-in test runner: by default `RuleTester` looks for global Mocha-style `describe`/`it`;
 * ESLint's documented way to use a different framework is to assign
 * `RuleTester.describe`/`.it`/`.itOnly` before calling `.run()`. Every
 * `*.test.mjs` under `eslint-rules/` imports `RuleTester` from here (not
 * straight from `eslint`) so `node --test` collects and reports every
 * rule assertion as its own test, instead of RuleTester silently no-op'ing
 * when it can't find a test framework.
 */
import { describe, it } from 'node:test';
import { RuleTester } from 'eslint';

RuleTester.describe = describe;
RuleTester.it = it;
RuleTester.itOnly = it.only;

export { RuleTester };
