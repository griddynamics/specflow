import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const nodeGlobals = Object.fromEntries(
  [
    "process",
    "console",
    "Buffer",
    "URL",
    "URLSearchParams",
    "fetch",
    "Headers",
    "Request",
    "Response",
    "AbortController",
    "setTimeout",
    "clearTimeout",
    "setInterval",
    "clearInterval",
    "TextEncoder",
    "TextDecoder",
    "ReadableStream",
    "performance",
    "structuredClone",
  ].map((name) => [name, "readonly"]),
);

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Node scripts and tests (including the S13 e2e suite) are linted too (AC-12.5).
  {
    files: [
      "scripts/**/*.mjs",
      "lib/**/*.mjs",
      "tests/**/*.{mjs,ts}",
      "e2e/**/*.{mjs,ts}",
      "e2e-production/**/*.ts",
      "playwright.production.config.ts",
    ],
    languageOptions: { globals: nodeGlobals },
  },
  // Playwright fixtures take a `use` callback, which the React hooks rule mistakes for a hook.
  { files: ["e2e-production/**/*.ts"], rules: { "react-hooks/rules-of-hooks": "off" } },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Reports and the owner's untracked assets.
    "playwright-report/**",
    "logs/**",
    "test-results/**",
    "resources/**",
  ]),
]);

export default eslintConfig;
