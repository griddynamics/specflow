# TypeScript lint kit

A drop-in, project-agnostic set of static validations for TypeScript and React code bases: ESLint
(flat config, 20+ plugins, 22 local rules, grouped selector bans), stylelint, semgrep, knip,
Prettier, a strict `tsc` base, Size Limit payload budgets and strict Vitest options. Every tool is
declared in this folder, so the adopter sets configuration and runs one command.

The rule-by-rule catalogue is in [RULES.md](RULES.md).

## Purpose

- Make the compiler, the linter and a handful of structural scans refuse the defects that review
  keeps finding: type escapes, inline suppressions, unlabelled or nested controls, hand-rolled
  design-system pieces, unformatted numbers, leaked test code, layering violations, dead code and
  oversized bundles.
- Keep policy in code. A rule with no project facts is on by default. A rule that needs a fact
  falls in one of two groups: path facts (layers, `apiDir`, `apiClient`, `storage`,
  `entryPoints`, `router`, `localeModules`, `components`, `pages`) default to the conventional
  layout and the rule is on; data and design facts (type-only packages, wire types, mutation
  feedback, raw metrics, served verdicts, localisation, `designSystem.*`, the banned-terms rule, the semgrep
  design-system facts file) are off until the adopter supplies them. The `Default` column of the
  "Configurable rules" table in RULES.md says which is which.
- Be isolated: nothing here imports or resolves a file outside this folder. Tools are installed
  from this folder's `package.json` and `pyproject.toml`.

## Layout

| Path                                                    | What it is                                                                                                                                                                                         |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run.mjs`                                               | Orchestrator: runs prettier, eslint, stylelint, tsc, knip, semgrep and (opt-in) size-limit against a project                                                                                       |
| `eslint.config.mjs`                                     | Flat config; exports `defineKitConfig`, `appPreset`, `libraryPreset`, `e2ePreset`, `nodePreset`, the `SEL_*` selector groups and the selector builders; default export lints the current directory |
| `kit.config.example.mjs`                                | Example project config listing every option with neutral facts                                                                                                                                     |
| `eslint-rules/index.mjs`                                | The `local` plugin: registers every custom rule                                                                                                                                                    |
| `eslint-rules/restrict.mjs`                             | The `restrict` plugin: ESLint core `no-restricted-*` rules registered once per selector group                                                                                                      |
| `eslint-rules/config-helpers.mjs`                       | `severityOf`, `optionsOf`, `selectorsOf` helpers for reading a rule entry out of a built config                                                                                                    |
| `eslint-rules/rules/*.mjs`, `rules/lib/*.mjs`           | Custom rule implementations and their shared analysis helpers (interactive controls, repetition, render sinks, route components, wire type names)                                                  |
| `eslint-rules/rules/fixtures/`                          | Fixture sources (neutral names) the type-aware rule tests lint                                                                                                                                     |
| `eslint-rules/rule-tester-setup.mjs`                    | Wires ESLint `RuleTester` to `node:test`                                                                                                                                                           |
| `stylelint.config.mjs`, `stylelint/`                    | Stylelint config factory `defineStylelintConfig` and the `a11y/reduced-motion` plugin with tests                                                                                                   |
| `semgrep/*.yml`, `semgrep/fixtures/`                    | Semgrep rule files that run by default, and annotated test targets (`ruleid:` / `ok:`); the banned-terms rule is generated and lives in your own `semgrep/` folder                                 |
| `semgrep-ds/*.yml`, `semgrep-ds/fixtures/`              | The design-system rules built from `design-system.example.json` and the banned-terms rule built from `banned-terms.example.json`. Self-test target only: `run.mjs` never loads this folder         |
| `scripts/build-banned-terms.mjs`                        | Builds the `banned-terms.yml` rule from a terms file                                                                                                                                               |
| `banned-terms.example.json`                             | Neutral example terms file                                                                                                                                                                         |
| `scripts/build-ds-rules.mjs`, `templates/ds-*.yml.tmpl` | Builds the six design-system rule files (`ds-components`, `ds-forms`, `ds-navigation`, `ds-overlays`, `ds-typography`, `ds-styling`) from the templates and a design-system facts file             |
| `design-system.example.json`                            | Neutral example design-system facts (primitives folder, component and field names, geometry modules, tokens)                                                                                       |
| `prettierignore.project`                                | Prettier ignores `run.mjs` uses when the project has no `.prettierignore`                                                                                                                          |
| `run.test.mjs`                                          | Orchestrator self-test (kit exclusion, ignore fallback, Size Limit step)                                                                                                                           |
| `knip.json`                                             | Dead code and dependency hygiene template, every issue type an error                                                                                                                               |
| `tsconfig.base.json`, `tsconfig.library.json`           | Strict compiler base and library variant (`isolatedDeclarations`)                                                                                                                                  |
| `.prettierrc`, `.prettierignore`                        | Formatting options                                                                                                                                                                                 |
| `size-limit.config.mjs`, `size-limit/assets.mjs`        | Payload budgets read from the build output                                                                                                                                                         |
| `vitest.strict.mjs`, `vitest.setup.mjs`                 | `strictTestOptions()` and the fail-on-console setup                                                                                                                                                |
| `config.test.mjs`                                       | Config self-test: presets, restrict groups and option-gated groups on a scratch project                                                                                                            |
| `package.json`, `pyproject.toml`                        | Pinned Node tools and the pinned semgrep                                                                                                                                                           |

## Requirements

All versions come from the kit's own manifests.

- Node >= 24 and npm.
- `uv` and Python >= 3.12 (for semgrep).
- Node tools (`package.json`, exact versions): eslint 9.39.5, typescript 5.9.3, typescript-eslint
  8.70.0, @eslint/js, globals, @eslint-react/eslint-plugin, eslint-plugin-react, react-hooks (v7),
  react-refresh, jsx-a11y, import-x, boundaries, barrel-files, sonarjs, unicorn, regexp,
  no-unsanitized, i18next, @tanstack/eslint-plugin-query and -router, @vitest/eslint-plugin,
  testing-library, jest-dom, playwright, eslint-config-prettier, @typescript-eslint/utils, stylelint
  16, knip 6, prettier 3, size-limit with @size-limit/file, vitest 5 with @vitest/coverage-v8,
  vitest-fail-on-console, jsdom, react, @tanstack/react-query, @types/node, @types/react.
- Python tool (`pyproject.toml`): semgrep 1.177.0.

## Adoption steps

1. Copy this folder into the target repository (for example `validation/typescript/lint/`). Do not
   split it: the config, the plugins, the fixtures and the tests resolve each other by relative path.
2. Install the tools from inside the folder:
   `npm ci --workspaces=false` and `uv sync --locked`.
3. Tell the kit about the project. Either run with the generic defaults (`app` at `src`, `e2e` at
   `e2e`, `node` for `.js`/`.mjs`), or write the project's own `eslint.config.mjs` from
   `kit.config.example.mjs`: set `root`, then each path and fact the project has. Paths are globs
   relative to `root`. `run.mjs` prefers the project's own `eslint.config.mjs`,
   `stylelint.config.mjs`, `knip.json`, `.prettierrc`, `size-limit.config.mjs` when they exist at
   the project root, else the kit's file. For Prettier ignores it uses the project's
   `.prettierignore`, else the project's `.gitignore` plus the kit's `prettierignore.project`.
   When the kit sits inside the project, `run.mjs` leaves its folder out of the Prettier, ESLint,
   stylelint and semgrep scans; add the folder to the `ignore` list of the project's `knip.json`.
4. Point the compiler at the strict base. In the project `tsconfig.json`:
   `{ "extends": "<kit>/tsconfig.base.json", "compilerOptions": { "lib": [...], "jsx": "react-jsx" }, "include": [...] }`.
   A package whose declarations other packages consume extends `tsconfig.library.json`.
5. Set the knip entries in the project's `knip.json` (copy and edit `entry` and `project`).
6. Build the optional inputs described under "Configurable rules" in RULES.md: the banned-terms
   rule (build it into `<project>/semgrep/banned-terms.yml`; the rule is off until then), and the OpenAPI document or generated types directory for the wire-type rule.
7. Add the semgrep rules the project wants to own: put them in a `semgrep/` folder at the project
   root and `run.mjs` loads it next to the kit's rules. The design-system rules (the `ds-*` ids
   and `styling-layout-measurement`) are OFF until the project supplies its facts: copy
   `design-system.example.json`, set the project's primitives folder, component and field names,
   geometry modules, tokens and classes, then either pass it on every run
   (`node run.mjs --design-system <facts.json>`, which builds the rules into a temporary folder),
   or build the files into the project's own `semgrep/` folder
   (`node scripts/build-ds-rules.mjs <facts.json> <project>/semgrep`). Never edit the generated
   `ds-*.yml` files: the next rebuild replaces them. A project without a primitives layer skips
   this step and gets none of those rules.
8. In Vitest config, use `strictTestOptions()` from `vitest.strict.mjs`.
9. Run the kit, fix findings, then wire it into CI.

## Commands

Install:

```
npm ci --workspaces=false
uv sync --locked
```

Run everything against a project (exit 0 pass, 1 findings, 2 usage error):

```
node run.mjs --project /path/to/project
node run.mjs --project /path/to/project --only eslint,stylelint,semgrep
node run.mjs --project /path/to/project --skip knip --size-limit
node run.mjs --project /path/to/project --only semgrep --design-system /path/to/design-system.json
```

Steps: `prettier`, `eslint`, `stylelint`, `tsc`, `knip`, `semgrep`, `size-limit`. `tsc` runs only
when the project has a `tsconfig.json`; `size-limit` runs only with `--size-limit` (it needs a
build).

Each tool on its own:

```
npx eslint --config /path/to/project/eslint.config.mjs --no-inline-config --report-unused-disable-directives-severity error --max-warnings 0 /path/to/project
npx stylelint --config stylelint.config.mjs --allow-empty-input '/path/to/project/**/*.css'
uv run semgrep scan --error --metrics=off --config semgrep/ /path/to/project
npx knip --directory /path/to/project --config "$PWD/knip.json"
npx tsc --noEmit -p /path/to/project/tsconfig.json
SIZE_LIMIT_DIST=/path/to/project/dist npx size-limit --config size-limit.config.mjs
node scripts/build-banned-terms.mjs /path/to/project/banned-terms.json /path/to/project/semgrep/banned-terms.yml
node scripts/build-ds-rules.mjs /path/to/project/design-system.json /path/to/project/semgrep
```

`--no-inline-config` makes inline `eslint-disable` comments inert, and the `no-warning-comments`
term `eslint-disable` turns every such comment, live or dead, into a finding, so suppression is not
available as an escape hatch. `--report-unused-disable-directives-severity error` reports nothing
under `--no-inline-config` (ESLint collects no directives then); it stays only for runs without
that flag.
Always run ESLint with these flags.

The Size Limit command above runs from the kit folder; `run.mjs --size-limit` does that for you
(Size Limit finds its plugin from the package.json above its working directory, so the project
does not need to declare `@size-limit/file`) and resolves `SIZE_LIMIT_DIST` against the project.

Size Limit settings (environment): `SIZE_LIMIT_DIST` (default `dist`), `SIZE_LIMIT_ENTRY_JS_B`
(default 180000), `SIZE_LIMIT_TOTAL_JS_B` (default 450000). Budgets are Brotli bytes; the entry
assets are read from `dist/index.html` (module scripts and modulepreloads).

## Self-tests

Run these inside the kit folder to prove the kit itself is intact:

```
npm ci --workspaces=false
uv sync --locked
npm run selftest
npm run selftest:semgrep-validate
npm run selftest:semgrep
npm run selflint
npm run format:check
```

- `selftest`: `node --test` over the custom rules (RuleTester wired to `node:test`), the stylelint
  plugin and options, the banned-terms and design-system rule builders (the shipped
  `semgrep-ds/ds-*.yml` must equal the build of `design-system.example.json`, and `semgrep/` must
  hold no `ds-*.yml`; `semgrep-ds/banned-terms.yml` must equal the build of
  `banned-terms.example.json`), the Size Limit asset
  reader, `config.test.mjs` and `run.test.mjs` (the orchestrator, including a Size Limit run on a
  scratch build).
- `selftest:semgrep-validate`: every rule file in `semgrep/` and `semgrep-ds/` parses and validates.
- `selftest:semgrep`: every rule fires on its `ruleid:` lines and stays quiet on its `ok:` lines,
  in `semgrep/fixtures/` and `semgrep-ds/fixtures/`.
- `selflint`: the kit lints itself with its own config and zero warnings.
- Prettier: `format:check` verifies the shipped files without rewriting them. Run
  `npx prettier --write --ignore-path .prettierignore .` after editing a kit file. Both semgrep
  fixture folders, `templates/` and the generated `semgrep-ds/*.yml` (design-system and
  banned-terms example builds) are ignored by design (annotations must stay on the line before their
  target; the generated files are compared byte for byte).

## CI and pre-commit

CI (one job, after install and, for size-limit, after the build):

```
npm ci --workspaces=false --prefix <kit> && uv sync --locked --project <kit>
node <kit>/run.mjs --project "$PWD"
node <kit>/run.mjs --project "$PWD" --only size-limit --size-limit   # after the build step
```

Also run `npm run selftest`, `selftest:semgrep-validate` and `selftest:semgrep` in the kit folder
whenever a kit file changes.

Pre-commit (fast subset, staged files are covered by the whole-project run because several rules
are type-aware or cross-file):

```
node <kit>/run.mjs --project "$PWD" --only prettier,eslint,stylelint,semgrep
```

Reserve `tsc`, `knip` and `size-limit` for CI or a pre-push hook. Run Prettier with `--write`
locally and `--check` in CI. Do not commit during a run that edits files the kit is reading.

## Zero-baseline policy

Every rule is an error, warnings fail the run (`--max-warnings 0`), and there is no suppression
mechanism: an inline disable is inert and itself a finding. A rule the project cannot meet is
changed in the config (a scoped override or an option), never silenced at the call site.

## Configurable rules

Rules that need project facts are listed in RULES.md under "Configurable rules", each with the
option that turns it on, the input it reads and how to build that input. The shipped example
inputs are `kit.config.example.mjs`, `banned-terms.example.json` and `design-system.example.json`.

## Related tracks

Token-level clone detection (`jscpd`) and the unused-CSS check (`purgecss`) are not part of this
kit; they live in the sibling track `../scripts` (`duplicates/` with `jscpd.json`, and
`checks/css-usage.mjs`).
