// Config of the validation kit's lint track for this package (`make validate-typescript-lint-ui`). Paths are globs relative to
// this folder. The package is React source with no router, no API client and no storage: only the application preset applies.
import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineKitConfig } from "../../validation/typescript/lint/eslint.config.mjs";

const kit = defineKitConfig({
  root: path.dirname(fileURLToPath(import.meta.url)),
  node: { files: ["**/*.{js,mjs}"], cjs: ["**/*.cjs"] },
  app: {
    src: "src",
    testSupport: ["tests/**/*.{ts,tsx}"],
    apiDir: "src/api",
    apiClient: "src/api/client.ts",
    storage: "src/storage.ts",
    entryPoints: ["src/index.ts"],
    router: "src/index.ts",
    components: "src/components",
    pages: "src/components",
    localeModules: [],
    reactVersion: "19.2.8",
    layers: [
      { type: "components", pattern: "src/components/**", allow: ["components", "shared"] },
      { type: "shared", pattern: "src/*.ts", allow: ["shared"] },
    ],
  },
  e2e: false,
});

// project: unit tests run on node:test (`npm test`), not Vitest, and a top-level `test()` returns a promise nobody awaits;
// src/index.ts is the package's one public entry, a barrel by purpose (consumers import "@graphico/ui").
export default kit.map((config) => {
  if (config.rules === undefined) return config;
  const rules = { ...config.rules };
  const isTests = (config.files ?? []).some((glob) => typeof glob === "string" && glob.startsWith("tests/"));
  if (isTests) {
    if ("vitest/no-import-node-test" in rules) rules["vitest/no-import-node-test"] = "off";
    if ("@typescript-eslint/no-floating-promises" in rules) rules["@typescript-eslint/no-floating-promises"] = "off";
  }
  if ("barrel-files/avoid-barrel-files" in rules) {
    return { ...config, rules, ignores: [...(config.ignores ?? []), "src/index.ts"] };
  }
  return { ...config, rules };
});
