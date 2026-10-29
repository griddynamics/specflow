// Config of the validation kit's lint track (validation/typescript/lint), run by
// `make validate-frontend`. `npm run lint` keeps using eslint.config.mjs (eslint-config-next);
// the kit's run.mjs prefers this file. Paths are globs relative to this folder.
import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineKitConfig } from "../validation/typescript/lint/eslint.config.mjs";

const kit = defineKitConfig({
  root: path.dirname(fileURLToPath(import.meta.url)),
  ignores: [
    ".next/**",
    "next-env.d.ts",
    "playwright-report/**",
    "test-results/**",
    "logs/**",
    "resources/**",
    "public/**",
    // Copy of the GD Liquid Glass design system: read-only, re-copied from docs/design-system.
    "styles/ds/**",
  ],

  // Node scripts, .mjs libs and tests.
  node: { files: ["**/*.{js,mjs}"], cjs: ["**/*.cjs"] },

  // Next.js App Router application: no `src/`, code sits in app/, components/, lib/.
  app: {
    src: ".",
    testSupport: ["tests/**/*.{ts,tsx}"],
    apiDir: "lib/data",
    apiClient: "lib/api.ts",
    // project: the one module that writes cookies and web storage (the kit's default is utils/storage.ts, a folder this app has not).
    storage: "lib/storage.ts",
    entryPoints: ["app/layout.tsx"],
    router: "app/layout.tsx",
    components: "components",
    pages: "app",
    localeModules: [],
    reactVersion: "19.2.8",
    layers: [
      { type: "app", pattern: "app/**", allow: ["app", "components", "lib", "types"] },
      { type: "components", pattern: "components/**", allow: ["components", "lib", "types"] },
      { type: "lib", pattern: "lib/**", allow: ["lib", "types"] },
      { type: "types", pattern: "types/**", allow: ["types"] },
    ],

    // GD Liquid Glass primitives live in components/ui; tokens in styles/ds/tokens.css.
    designSystem: {
      primitives: "components/ui/**/*.{ts,tsx}",
      styleModules: ["lib/theme.ts"],
    },
  },

  // Playwright specs (browser suites: e2e, e2e-production).
  e2e: {
    files: ["e2e/**/*.ts", "e2e-production/**/*.ts"],
    fixtures: "e2e/support.ts",
    appSourceImports: ["**/app/**", "**/components/**", "**/lib/**"],
  },
});

// Project facts the kit cannot know, applied by editing the kit's own config objects (a rule can only be
// configured in an object that also declares its plugin).
const FILENAME_CASE = [
  "error",
  // Next.js conventions: PascalCase component files (design-system ports) and [param] / (group) route folders.
  { cases: { kebabCase: true, pascalCase: true }, ignore: ["^\\[.+\\]$", "^\\(.+\\)$"] },
];

// The kit scopes these rules to "the browser application", but classifies the Playwright suites and the
// Playwright configs as application source. They are Node-side test-runner code, so they are exempt from:
// process.env probes (the runner is configured through the environment), fetch (a 200 MiB streamed upload that the
// Playwright request API cannot stream), web storage (page.evaluate seeds and reads the page's storage) and
// devDependencies (@playwright/test is a test-runner package by design).
const RUNNER_FILES = ["e2e/**", "e2e-production/**", "playwright*.config.ts"];
const RUNNER_EXEMPT_RULES = [
  "restrict/syntax-test-probes",
  "restrict/globals-network",
  "restrict/globals-storage",
  "restrict/properties-storage",
  "import-x/no-extraneous-dependencies",
];

// project: Next.js runs part of this source on the server only, where the kit's "browser application" rules do not apply.
// Each exemption names the one server module that owns the concern, as lib/api.ts owns the browser's network:
// - instrumentation.ts is Next's server start-up hook: it branches on NEXT_RUNTIME / NEXT_PHASE, build constants Next
//   inlines, and validates the server environment (AC-6.4);
// - lib/config.ts is the one module that reads the server environment (the proxy, Auth.js, the BFF and the server
//   layouts read it through serverEnv() / appConfig());
// - lib/oidc-refresh.ts is the server's OIDC back channel (token refresh and Auth.js's customFetch): it calls the issuer,
//   which the browser's same-origin BFF client cannot do.
const SERVER_EXEMPT_FILES = {
  "restrict/syntax-test-probes": ["instrumentation.ts", "lib/config.ts"],
  "restrict/globals-network": ["lib/oidc-refresh.ts"],
};

// project: `playwright/no-raw-locators` only reports a string literal argument, so a CSS selector held in a constant,
// a template string or a helper's parameter (`page.locator(SELECTOR)`, `scope.locator(\`[data-id="${id}"]\`)`) passed it.
// This rule reports the raw-selector APIs themselves, whatever the argument expression is: an element is found by
// its role, label, text or test id (`getByRole`, `getByLabel`, `getByText`, `getByTestId`, ...).
// - `locator`, `frameLocator`, `$`, `$$`, `$eval`, `$$eval`, `waitForSelector`: they only exist to take a selector;
// - the Page / Frame shortcuts whose first argument is a selector (`page.click(sel)`, `page.textContent(sel)`, ...):
//   reported when the receiver is a Playwright Page or Frame (from the types where the file is type-checked, else
//   from the receiver's name), because the same method on a Locator takes no selector (`locator.click()`).
const SELECTOR_ONLY_METHODS = new Set(["locator", "frameLocator", "$", "$$", "$eval", "$$eval", "waitForSelector"]);
const PAGE_SELECTOR_METHODS = new Set([
  "check",
  "click",
  "dblclick",
  "dispatchEvent",
  "dragAndDrop",
  "fill",
  "focus",
  "getAttribute",
  "hover",
  "innerHTML",
  "innerText",
  "inputValue",
  "isChecked",
  "isDisabled",
  "isEditable",
  "isEnabled",
  "isHidden",
  "isVisible",
  "press",
  "selectOption",
  "setChecked",
  "setInputFiles",
  "tap",
  "textContent",
  "type",
  "uncheck",
]);
const PAGE_TYPES = new Set(["Page", "Frame"]);
const PAGE_NAMES = /^(?:page|frame)$|(?:Page|Frame)$/u;

/** The called method's name: `a.b()` and `a["b"]()`. */
function calledName(callee) {
  if (callee.type !== "MemberExpression") return undefined;
  if (!callee.computed && callee.property.type === "Identifier") return callee.property.name;
  if (callee.computed && callee.property.type === "Literal" && typeof callee.property.value === "string") {
    return callee.property.value;
  }
  return undefined;
}

/** Whether `receiver` is a Playwright Page or Frame: from the type checker when the file has one, else by name. */
function isPageLike(context, receiver) {
  const services = context.sourceCode.parserServices;
  if (services?.program && services.esTreeNodeToTSNodeMap) {
    const checker = services.program.getTypeChecker();
    const type = checker.getTypeAtLocation(services.esTreeNodeToTSNodeMap.get(receiver));
    const name = (type.aliasSymbol ?? type.getSymbol())?.getName();
    return name !== undefined && PAGE_TYPES.has(name);
  }
  if (receiver.type === "Identifier") return PAGE_NAMES.test(receiver.name);
  return receiver.type === "MemberExpression" && calledName(receiver) !== undefined && PAGE_NAMES.test(calledName(receiver));
}

const noRawSelector = {
  meta: {
    type: "problem",
    docs: { description: "Find elements by role, label, text or test id, never by a raw selector." },
    schema: [],
    messages: {
      raw: "`{{name}}()` finds elements by a raw selector, whatever expression holds it. Use getByRole, getByLabel, getByText or getByTestId (add an accessible name or a data-testid to the component when it lacks one).",
    },
  },
  create(context) {
    return {
      CallExpression(node) {
        const name = calledName(node.callee);
        if (name === undefined) return;
        const raw =
          SELECTOR_ONLY_METHODS.has(name) ||
          (PAGE_SELECTOR_METHODS.has(name) && node.arguments.length > 0 && isPageLike(context, node.callee.object));
        if (raw) context.report({ node: node.callee.property, messageId: "raw", data: { name } });
      },
    };
  },
};

const RAW_SELECTOR_CONFIG = {
  basePath: path.dirname(fileURLToPath(import.meta.url)),
  // Every source of the app, its tests, the browser suites, their support helpers and the configs.
  files: ["**/*.{ts,tsx,mts,cts,js,jsx,mjs,cjs}"],
  plugins: { project: { rules: { "no-raw-selector": noRawSelector } } },
  rules: { "project/no-raw-selector": "error" },
};

const configs = kit.flatMap((config) => {
  // project: a config without rules is returned untouched. Adding `rules: {}` to the kit's global-ignores object
  // (only basePath + ignores) turns it into a non-global ignore, and ESLint then lints .next/ and the other
  // build/report folders the kit and this file exclude.
  if (config.rules === undefined) return [config];
  const rules = config.rules;
  const touched = { ...rules };
  if (RUNNER_EXEMPT_RULES.some((rule) => rule in rules)) {
    const server = Object.entries(SERVER_EXEMPT_FILES).flatMap(([rule, files]) => (rule in rules ? files : []));
    return [{ ...config, rules: touched, ignores: [...(config.ignores ?? []), ...RUNNER_FILES, ...server] }];
  }
  // project: a design-system component's own `role` prop (ChatMessage's "user" | "assistant", bundle.js's API) is not an
  // ARIA role; TypeScript types the props that do reach the DOM (React's AriaRole), so the check stays on DOM elements.
  if ("jsx-a11y/aria-role" in rules) touched["jsx-a11y/aria-role"] = ["error", { ignoreNonDOM: true }];
  // project: a scrollable region (a wide table, the document pane) takes the focus so a keyboard user can scroll it
  // (WCAG 2.1.1; axe `scrollable-region-focusable`); it is a labelled landmark, not a control.
  if ("jsx-a11y/no-noninteractive-tabindex" in rules) {
    touched["jsx-a11y/no-noninteractive-tabindex"] = ["error", { roles: ["tabpanel", "region"] }];
  }
  if ("unicorn/filename-case" in rules) touched["unicorn/filename-case"] = FILENAME_CASE;
  const isTests = (config.files ?? []).some((glob) => typeof glob === "string" && glob.startsWith("tests/"));
  // Unit tests run on node:test (`npm test`), not Vitest; a top-level `test()` returns a promise nobody awaits.
  if (isTests) {
    if ("vitest/no-import-node-test" in rules) touched["vitest/no-import-node-test"] = "off";
    if ("@typescript-eslint/no-floating-promises" in rules) touched["@typescript-eslint/no-floating-promises"] = "off";
  }
  return [{ ...config, rules: touched }];
});

export default [...configs, RAW_SELECTOR_CONFIG];
