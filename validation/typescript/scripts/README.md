# TypeScript static-validation scripts

Standalone Node scripts that check a TypeScript/React front end for defects that
`tsc`, ESLint and Stylelint do not see: translation catalogues out of step, a CSS
variable that resolves to nothing, a contrast ratio below WCAG AA, a blocking
`<script>`, a dev tool leaking into the production bundle, a bundle over its
byte budget, a hand-written type in a generated directory, a client call with no
matching API contract, a navigation tree that drifted from its declared shape,
a Next.js App Router tree that breaks the file conventions or a link to a page
that does not exist, a Server/Client Component boundary crossed the wrong way,
an unused CSS class, and copied code. Every script runs alone
against a target root given on the command line, and every one ships with its
self-tests and fixtures. The rule catalogue is in [RULES.md](RULES.md).

Some checks are generic logic that needs facts about your project as input
(a route list, a navigation tree, a token list, API contracts, required
strings). Those take a small JSON file that you write or build; neutral
examples are in `examples/`. Two further checks are specified but not shipped
as code, in `schema-specific/`.

## Layout

| Path                             | Contents                                                                                                                                                         |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run.mjs`                        | Runner. Runs the checks (and optionally tools) and returns the worst status                                                                                      |
| `run.test.mjs`                   | Self-test of the runner                                                                                                                                          |
| `validation.config.example.json` | Example runner config: every check with example options, and the tool list                                                                                       |
| `checks/*.mjs`                   | One check per file. Each has a `check()` function and a CLI. `*.test.mjs` beside it is its self-test                                                             |
| `checks/lib/`                    | Shared helpers: CLI contract, finding output, output parsers, tool lookup, spec loader, TypeScript AST helpers, OKLCH maths                                      |
| `checks/fixtures/<check>/`       | Positive and negative target trees (and spec files) for each check's self-test                                                                                   |
| `tools/openapi-to-contracts.mjs` | Build step: derives the API contracts input from an OpenAPI JSON document                                                                                        |
| `examples/*.json`                | Neutral example inputs: API contracts, navigation spec, tokens spec, required strings, dist-policy banned list, contrast pairs                                   |
| `schema-specific/*.md`           | Specifications of two checks that are not shipped as code ([`primitives`](schema-specific/primitives.md), [`state-surfaces`](schema-specific/state-surfaces.md)) |
| `duplicates/`                    | Token clone detection (jscpd engine) and its tests                                                                                                               |
| `jscpd.json`                     | Default clone-detection configuration                                                                                                                            |
| `pyproject.toml`, `uv.lock`      | Pinned Semgrep (installed by `uv sync` into the local `.venv`)                                                                                                   |
| `package.json`                   | Dependencies, Node engine and the `check`, `duplicates`, `openapi-to-contracts`, `test`, `format:check` and `selftest` scripts                                   |
| `package-lock.json`              | Lockfile pinning every transitive dependency; installed with `npm ci --workspaces=false`                                                                         |
| `.prettierrc.json`               | Formatting used by the kit's own sources                                                                                                                         |

## Requirements

- Node 24 or newer (`engines` in `package.json`).
- Packages, pinned in `package.json`: `typescript` 5.9.3, `purgecss` 7.0.2,
  `postcss-selector-parser` 7.1.4, `@jscpd/core` 4.0.4, `@jscpd/tokenizer`
  4.0.4, `prismjs` 1.30.0, `reprism` 0.0.11, `size-limit` 13.1.1 and
  `@size-limit/file` 13.1.1 (dependencies); `prettier` 3.9.6 (development).
  Install them with `npm ci --workspaces=false --no-audit --no-fund` in this folder (the shipped `package-lock.json` pins the transitive packages).
- Semgrep 1.177.0, only for the `semgrep` check. It is a Python tool pinned in
  this folder's `pyproject.toml` (lockfile `uv.lock`); `uv sync` in this folder
  installs it into the local `.venv`. The check uses `$SEMGREP_BIN`, else
  `.venv/bin/semgrep`, else `semgrep` on `PATH`; for an absolute binary the
  check puts that binary's folder first on `PATH` so the `semgrep` wrapper finds
  `pysemgrep`.
- `git`, only for the duplicates scan, which lists files with `git ls-files`.
- The runner's optional `tools` step calls `tsc`, `eslint`, `knip`, `knip-production`,
  `prettier` and `stylelint`. `tsc`, `prettier` and `size-limit` are installed with the kit.
  `eslint`, `knip` and `stylelint` are not kit dependencies: they come from the
  target project, or from a folder you list in `binDirs` (for example the folder
  of the sibling lint kit that installs them).

## Adopt in a new project

1. Copy this folder into the project (for example to `tools/static-checks/`)
   and run `npm ci --workspaces=false --no-audit --no-fund` there (it installs the
   versions pinned in `package-lock.json`).
2. Copy `validation.config.example.json` to a name of your choice, for example
   `validation.config.json`, and set `root` (relative to the config file) to the
   front-end package.
3. Set each check's options to the project's real paths: `src`, `locales`,
   `base-locale`, `tokens-css`, `html`, `css`, `assets`, `dist`,
   `generated-dir`, `expected-file`, and `config`/`target` for Semgrep. The kit's
   TypeScript Semgrep rules are the lint kit's `semgrep/` folder
   (`typescript/lint/semgrep`); copy only its rule files (`typescript/lint/semgrep/*.yml`) to `.semgrep` in the
   target (the example config's value), never its `fixtures/` folder, which holds
   deliberately bad code; or point `config` at the lint kit's folder with a path
   relative to `--root`. Add `.semgrep` to the target's prettier and knip ignore
   lists. Delete
   the checks that do not apply, or set them to `false`.
4. Provide the JSON inputs for the configurable checks you want (see the next
   section). A check that names an input file which does not exist exits `2`.
5. Set `localised-code-families` if some dynamic key families hold localised
   labels in every locale, and `namespaces` if the catalogues must provide
   certain key namespaces (see `i18n-protected` in RULES.md).
6. List the tools the runner should call in `tools`, and set `binDirs` if
   `eslint`, `knip` or `stylelint` are installed outside the target.
7. To tune clone detection, put a `.jscpd.json` in the directory the scan
   runs from; it replaces the kit's `jscpd.json`.
8. Add the run command to CI and, if wanted, to a pre-commit hook (below). Run
   the build and the code generator before the `dist-policy`, `bundle-size` (when
   it does not build itself) and `api-types-only` checks, which refuse to pass
   without their input.

### Build the inputs of the configurable checks

| Check                                  | Option             | Input                                                                                               | How to get it                                                                                                            |
| -------------------------------------- | ------------------ | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `i18n-catalogues` (required strings)   | `required-strings` | `{ "strings": [ { "value", "label" } ] }`; `examples/required-strings.example.json`                 | Write it by hand: the user-facing strings that must exist verbatim in the base catalogue (empty-state or error messages) |
| `tokens` (token parity)                | `tokens-spec`      | `{ "tokens": [ { "name": "--x", "value"? } ] }`; `examples/tokens.spec.example.json`                | Write it by hand or export it from your design-token source                                                              |
| `api-contract-map`                     | `contracts`        | `{ "callees", "endpoints": [ { "id", "method", "path" } ] }`; `examples/api-contracts.example.json` | Build it from OpenAPI (below)                                                                                            |
| `nav-tree`                             | `nav-spec`         | declared navigation tree; `examples/nav-spec.example.json`                                          | Write it by hand from your navigation design; it is the reference the code is compared to                                |
| `dist-policy` (extra banned libraries) | `banned`           | `[ { rule, label, markers } ]`; `examples/dist-policy.example.json`                                 | Write it by hand                                                                                                         |
| `contrast` (other token naming)        | `pairs`            | `{ "pairs": [[bg, fg]] }`; `examples/contrast-pairs.example.json`                                   | Write it by hand                                                                                                         |
| `next-routes` (route table, nav)       | `route-table`      | a `const` object of route templates in your source (`<file>#<Name>`), optional                      | Already in your code: the module that builds your paths; `nav-spec` is the `nav-tree` input                              |

Build the API contracts from an OpenAPI 3 document in JSON:

```sh
node tools/openapi-to-contracts.mjs ../my-app/openapi.json --out ../my-app/validation/api-contracts.json --base-path /api
```

Each `paths.<path>.<method>` becomes one endpoint; its id is the `operationId`,
else `<METHOD> <path>`. `--base-path` is the prefix your client source code
writes before the OpenAPI paths (for example `/api` when calls read
`httpGet('/api/orders')`); omit it when an HTTP wrapper adds the prefix at
runtime. `--callees name=METHOD,...` names the client functions that make a call
(default `httpGet`, `httpPost`, `httpPut`, `httpPatch`, `httpDelete`). Without
`--out` the result prints on stdout. Re-run it whenever the OpenAPI document
changes.

## Run against a project

In the commands below, `<validation>` is the root of the whole validation kit and
`<scripts-kit>` is this folder.

```sh
npm ci --workspaces=false --no-audit --no-fund
uv sync   # semgrep, only for the semgrep check
node run.mjs --root ../my-app                      # default checks
node run.mjs --config validation.config.json       # checks and tools from a config
node run.mjs --config validation.config.json --only tokens,contrast,next-routes
node checks/i18n-catalogues.mjs --root ../my-app --locales src/locales --required-strings validation/required-strings.json
node checks/tokens.mjs --root ../my-app --tokens-css src/styles/tokens.css --tokens-spec validation/tokens.spec.json
node tools/openapi-to-contracts.mjs ../my-app/openapi.json --out ../my-app/validation/api-contracts.json --base-path /api
node checks/api-contract-map.mjs --root ../my-app --contracts validation/api-contracts.json
node checks/nav-tree.mjs --root ../my-app --nav-spec validation/nav-spec.json
node checks/next-routes.mjs --root ../my-app --route-table lib/routes.ts#ROUTES --nav-spec validation/nav-spec.json
node checks/next-boundaries.mjs --root ../my-app
node checks/bundle-size.mjs --root ../my-app --build "npm run build" --entry-budget 160000 --total-budget 250000
node checks/semgrep.mjs --root ../my-app --config <validation>/typescript/lint/semgrep --target src
cd ../my-app && node <scripts-kit>/duplicates/duplicates.mjs
```

Every script and the runner print one finding per line on stdout:

```
path:line:col: rule-id: message (fix: ...)
```

Exit codes: `0` clean, `1` findings, `2` tool or configuration error. When a
check has nothing to check (its input file or directory is absent), it prints
`skipped: <reason>` on stderr and exits `0`. A skip is always reported. A check
whose input must exist, such as a build output, generated code or a named spec
file, exits `2` when that input is missing. It never reports a clean pass for
something it could not read.

All paths are relative to `--root` (default: the current directory). Every
option is passed as `--name=value` or `--name value`. A stray positional
argument exits `2`.

### Runner

```sh
node run.mjs [--root <dir>] [--config <file.json>] [--only <step,...>]
```

- With no config, the runner runs the checks that skip cleanly when their input
  is absent: `html-scripts`, `i18n-catalogues`, `i18n-usage`, `i18n-protected`,
  `tokens`, `contrast` and `css-usage`. The other checks run only when named in
  a config or in `--only`, because each needs a build, a generator run, a rules
  directory or an input file you supply: `dist-policy`, `bundle-size`,
  `api-types-only`, `api-contract-map`, `nav-tree`, `next-routes`,
  `next-boundaries` and `semgrep`.
- In a config file, `checks.<name>` holds a check's options. A check named
  there is turned on, and the value `false` turns it off. `root` is resolved
  relative to the config file; `--root` overrides it. An unknown check or tool
  name exits `2`.
- The config's `tools` list adds tools: `tsc` (`tsc -b`), `eslint` (JSON
  formatter, with `--max-warnings 0`, no inline config, and unused disable
  directives reported as errors), `knip` (JSON reporter), `knip-production` (`knip --production`: code and
  dependencies only tests or dev tooling use), `prettier --check`,
  `stylelint` (JSON formatter) and `duplicates`. A status that the tool does
  not use for diagnostics counts as a tool error. So does a non-zero status
  whose output parses to no finding, and so does a tool that is not installed.
- Tool binaries are looked up in `node_modules/.bin` at or above the root, then
  at or above each directory of the config's `binDirs` (paths relative to the
  config file; an array of strings, anything else exits `2`), then in the kit's
  own folder.
- `--only` runs only the named checks and tools.
- Each step prints one status line on stderr (`ok`, `findings`, `skipped` or
  `tool-error`); the runner exits with the worst step status.

### Duplicates

```sh
cd <git work tree> && node <scripts-kit>/duplicates/duplicates.mjs [--project <dir>]
```

Scans the tracked and untracked (not ignored) files of the current directory
with the native jscpd token engine, or only those under `--project`. Before
comparing, the scan blanks content that is not implementation, keeping line
numbers intact: import declarations; assertions in test files that only check
a value (and `waitFor` callbacks made up of only such assertions); and the
boilerplate of a typed ESLint flat config. Actions inside an assertion are
still compared, and so are custom rule configurations. Assertion blanking
applies to JS/TS test files only; Python files are compared in full when
`python` is added to `formats`. Details are in RULES.md.

`duplicates/detect-sources.mjs` is the same engine as a stdin-to-stdout filter
for a caller that prepares its own sources: a JSON request (`minLines`,
`minTokens`, `mode`, `sources` of `id`, `format`, `source`) in, the clones as
JSON out, exit 2 on a bad request. The Python kit's `clones` check
(`validation/python/scripts`, README `clones`) runs it on Python sources with
imports and test oracles already blanked.

## Wire into CI and pre-commit

CI: install once, then run the runner with the config; its exit code fails the
job on findings (`1`) and on configuration or tool errors (`2`).

```sh
(cd tools/static-checks && npm ci --workspaces=false --no-audit --no-fund)
npm run build                                   # before dist-policy / api-types-only
node tools/static-checks/run.mjs --config tools/static-checks/validation.config.json
```

Pre-commit: run only the fast checks that need no build, for example

```sh
node tools/static-checks/run.mjs --config tools/static-checks/validation.config.json \
  --only i18n-catalogues,i18n-usage,tokens,contrast,next-routes,next-boundaries,nav-tree
```

in a Git `pre-commit` hook or a hook manager's local-command entry. Keep
`bundle-size`, `dist-policy`, `semgrep` and `duplicates` for CI, where a build
and the extra tools exist. Set `VALIDATION_REPORTS_DIR` to collect the
duplicates JSON report as a CI artifact.

## Self-tests

```sh
npm ci --workspaces=false --no-audit --no-fund
npm test
npm run format:check
npm run selftest      # runs both of the above
```

`npm test` runs `node --test` over `checks/**/*.test.mjs`, `duplicates/*.test.mjs`,
`tools/*.test.mjs` and `run.test.mjs`. The self-tests need no network and no
Semgrep: the Semgrep tests use a stub scanner selected through `SEMGREP_BIN`,
and the bundle-size tests parse recorded size-limit output, plus one end-to-end test that uses the kit-installed size-limit. The duplicates tests run
`git init` in a temporary directory. Each check has a positive fixture (must
report nothing) and one or more negative fixtures (must report the named
rules), so a rule that stops firing fails its test.
