# Python static-validation scripts

`pystatic` is a set of project-agnostic static checks for Python repositories and their
surrounding files: documentation, deployment files and JSON data. Each check is an AST,
pylint, vulture, `check-jsonschema` or jscpd pass over a target tree given on the command
line. None of them imports or runs the code it checks. The one exception is `openapi-snapshot`,
which must import the app.

Project facts are never written into the kit. Enum values, configuration keys, registry
enums, schemas, documentation conventions and project lists all come from one adopter file,
`pystatic.toml`. `pystatic.example.toml` documents every table with a neutral example.

Each check that needs a fact the documentation should own reads it as plain input. A
project that wants to derive those inputs from its documentation tables can follow the
specification in `schema-specific/docs-oracle.md`.

The level-2 headings below are the `doc:` anchors that findings cite, for example
`doc: /README.md#enum-literals`. `pystatic/harness/test_docref.py` checks that each one
resolves, so renaming a heading fails that test.

## Layout

| Path | Contents |
| --- | --- |
| `pystatic/harness/` | The report contract (`Finding`, `StepResult`, `.reports/<unit>/<step>.json`, `summary.json`), git helpers, doc-anchor resolution, and the pytest doc-citation helper `collected_docs` |
| `pystatic/files.py`, `pystatic/cli.py`, `pystatic/config.py` | The scan target (`--root`, `--scan`) and the adopter configuration (`--config`) shared by every check |
| `pystatic/scripts/` | The checks, each with a colocated `test_<name>.py` and, where needed, `fixtures/<name>/` |
| `pystatic/docs/` | The documentation-bundle checker and index generator |
| `pystatic/style/` | The pylint and vulture adapters and the clone-detection input filter. None of them depends on the harness |
| (SQL, migration and ORM checks) | Ship in `validation/sql`, not here |
| `pystatic/openapi_snapshot.py` | Exports or verifies an OpenAPI snapshot |
| `pystatic/run.py` | One runner for every harness check |
| `pystatic.example.toml` | The adopter configuration, with every table documented |
| `reviewed-suppressions.example.toml` | Template for the `config-scoping` reviewed table |
| `reviewed-suppressions.toml` | The kit's own reviewed table, used when the kit checks itself |
| `schema-specific/docs-oracle.md` | Specification for deriving check inputs from documentation tables |

## Installation

The kit is a standalone uv project. From this directory:

```sh
uv sync --all-extras          # runtime + `style` + `pytest` extras + dev tools
```

To use the kit from another project, install it into that project's environment, or run it
from the kit's own environment with `--root` pointing at the project:

```sh
uv pip install "pystatic[style,pytest,pages] @ file:///path/to/validation/python/scripts"  # into the project's venv
uv run --project /path/to/validation/python/scripts python -m pystatic.run --root .
```

## Running

Every check prints each finding on one line:

```
path:line:col: rule: message (fix: ...; doc: ...)
```

Each check exits 0 when clean, 1 when it has findings, and 2 on a tool or configuration
error. A check whose configuration table is absent either runs on its defaults or reports
itself `skipped` in its status line. It never passes silently.

```sh
# every harness check against one tree; `--level all` adds package-data (builds a wheel)
python -m pystatic.run --root /path/to/project [--config pystatic.toml] \
    [--level quick|all] [--reviewed reviewed-suppressions.toml]

# one check
python -m pystatic.scripts.<check_module> --root /path/to/project [--scan src] [--config FILE]

# the documentation bundle
python -m pystatic.docs.check --root /path/to/project
python -m pystatic.docs.indexes --root /path/to/project [--check]

# style adapters: the positional argument is the project directory that holds pyproject.toml
python -m pystatic.style.length_limits /path/to/project [--repository /path/to/repo]
python -m pystatic.style.dead_code /path/to/project

# the OpenAPI snapshot, run with the project's own interpreter
python -m pystatic.openapi_snapshot --app app.main:create_app --out contracts/openapi.json --check
```

Options and environment variables:

- `--root` defaults to the working directory.
- Each `--scan SUBDIR` (repeatable) replaces the check's default scan directories, except
  where the check's table supplies its own `scan` (the `config-literals` numeric rule and
  `runtime-defaults`), which ignore `--scan`.
- Directories named `fixtures`, `.git`, `.venv`, `node_modules` or `__pycache__` are always
  skipped.
- `--config` defaults to `<root>/pystatic.toml`. A missing default file means "no tables". A
  missing file named explicitly is a configuration error.
- The runner writes its reports under `PYSTATIC_REPORTS_ROOT` (default `<cwd>/.reports`) and
  the summary schema under `<cwd>/build/`.
- `PYSTATIC_REPO_ROOT` overrides the working directory as the tree that git helpers and
  reports are relative to.
- `PYSTATIC_DOCS_ROOT` points `doc:` anchors at a project's own documentation.

## Self-tests

From this directory:

```sh
uv sync --all-extras
uv run pytest                                     # every self-test, fixtures included
uv run pytest --cov --cov-report=term-missing     # coverage, fail-under 80
uv run ruff check . && uv run ruff format --check .
uv run mypy pystatic
uv run deptry .
uv run python -m pystatic.scripts.config_scoping --root .   # the kit's own suppressions are reviewed
uv run python -m pystatic.style.length_limits .
uv run python -m pystatic.style.dead_code .
```

## Requirements

Declared in `pyproject.toml`; Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

| Group | Packages |
| --- | --- |
| Runtime | `pydantic==2.13.5`, `markdown-it-py[linkify]==4.2.0`, `pyyaml==6.0.3`, `check-jsonschema==0.38.0` |
| Extra `style` | `pylint==4.1.3`, `vulture==2.16` (length limits, dead code) |
| Extra `pytest` | `pytest==9.1.1` (the `collected_docs` helper) |
| Node.js | Node.js 24 and `npm ci` in `validation/typescript/scripts` (pinned `@jscpd/core` and `@jscpd/tokenizer` 4.0.4): the engine of [clones](#clones) |
| Dev group | `pytest`, `pytest-cov==7.1.0`, `hatchling==1.32.4` (`package-data` builds with it), `ruff==0.16.7`, `mypy==2.3.1`, `deptry==0.25.1`, `types-pyyaml` |

`pyproject.toml` also holds the kit's own tool settings, which an adopter can copy for the
project's tooling: strict mypy with the `pydantic.mypy` plugin, `warn_unreachable` and extra
error codes; strict pytest (`--strict-markers`, `--strict-config`, `xfail_strict`,
`filterwarnings = ["error"]`); coverage with branch measurement and `fail_under = 80`; ruff
`E F W I UP B SIM RUF`; deptry.

## Adoption

1. Copy this directory into the target repository (or keep it beside it) and run
   `uv sync --all-extras`.
2. Copy `pystatic.example.toml` to `<project>/pystatic.toml`. Keep only the tables for the
   checks you want and replace the neutral orders and bookshop facts with the project's own.
   Every path is relative to `--root`.
3. If the project suppresses lint rules in `pyproject.toml`, copy
   `reviewed-suppressions.example.toml` to `<project>/reviewed-suppressions.toml` and give
   each suppression a reason (see [config-scoping](#config-scoping)).
4. Declare dead-code scopes in the project's `pyproject.toml` (see [dead-code](#dead-code)) and
   source-ownership externals if needed.
5. Run the runner once and fix or configure every finding.

```sh
uv sync --all-extras
uv run python -m pystatic.run --root /path/to/project \
    --config /path/to/project/pystatic.toml --level all \
    --reviewed /path/to/project/reviewed-suppressions.toml
uv run python -m pystatic.scripts.enum_literals --root /path/to/project
uv run python -m pystatic.docs.check --root /path/to/project
uv run python -m pystatic.docs.indexes --root /path/to/project --check
uv run python -m pystatic.style.length_limits /path/to/project
uv run python -m pystatic.style.dead_code /path/to/project
echo '["tests/test_orders.py"]' | uv run python -m pystatic.style.clone_inputs --root /path/to/project
```

## Wiring into CI and pre-commit

CI: run the kit as one job step, using the runner's exit code (0 clean, 1 findings, 2 tool or
configuration error). The reports under `.reports/python/<step>.json` and
`.reports/summary.json` can be uploaded as build artifacts.

```yaml
- run: uv sync --all-extras
  working-directory: validation/python/scripts
- run: >
    uv run python -m pystatic.run --root "$GITHUB_WORKSPACE" --level all
    --config "$GITHUB_WORKSPACE/pystatic.toml"
    --reviewed "$GITHUB_WORKSPACE/reviewed-suppressions.toml"
  working-directory: validation/python/scripts
```

Use `--level quick` for pre-commit and local loops: it skips `package-data`, which builds a
wheel. A local pre-commit hook can call the same command with `pass_filenames: false`. Run
`python -m pystatic.docs.indexes --check` and `python -m pystatic.openapi_snapshot --check` as
separate steps; they are not in the runner.

## Configurable rules

Every check below reads project facts from `pystatic.toml`. The shipped
`pystatic.example.toml` has a neutral example for each table, and `pystatic/test_config.py`
checks that the example parses for every check.

| Check | Table | Input you supply |
| --- | --- | --- |
| [blocking-in-async](#blocking-in-async) | `[blocking-in-async]` | blocking call names to replace or add |
| [no-dict-schemas](#no-dict-schemas) | `[no-dict-schemas]` | schema directories, open fields, class suffixes |
| [enum-literals](#enum-literals) | `[enum-literals]` | enum values, or `values-from-enums` to harvest them from code |
| [config-literals](#config-literals) | `[config-literals]` | scan directories, configuration keys, config classes |
| [registry-files](#registry-files) | `[[registry-files]]` | enum `path:Class`, directory, file pattern |
| [json-schemas](#json-schemas) | `[[json-schemas]]` | schema file, document globs, optional name field |
| [deployment-files](#deployment-files) | `[deployment-files]` | compose files, forbidden trees, word lists, allowed keys |
| [file-size](#file-size) | `[file-size]` | paths, byte ceiling, suffixes, page budget |
| [source-ownership](#source-ownership) | `[source-ownership]` | project directories, extensions |
| [runtime-defaults](#runtime-defaults) | `[runtime-defaults]` | a `SETTING_NAME: number` JSON file, scan directories |
| [package-data](#package-data) | `[package-data]` | package directory, data suffixes |
| [clones](#clones) | `[clones]` | source pathspecs, test-file patterns, clone size, reporters |
| [docs-bundle](#docs-bundle) | `[docs-bundle]` | the whole documentation convention |
| [config-scoping](#config-scoping) | `reviewed-suppressions.toml` | one reasoned row per suppression |

Building the inputs:

- `enum-literals.values`, `config-literals.keys` and the `runtime-defaults` JSON can be typed
  by hand, harvested from code (`values-from-enums`, or a small script that dumps a settings
  class to JSON), or derived from documentation tables by the extractor specified in
  [`schema-specific/docs-oracle.md`](schema-specific/docs-oracle.md): input registry schema,
  build algorithm, output, consumers and an example.
- The schema for `json-schemas` is an ordinary JSON Schema file you author or export from the
  data model.

## blocking-in-async

`pystatic.scripts.blocking_in_async` flags a known-blocking library call made directly
inside an `async def` body. It knows these blocking calls:

- pandas and openpyxl readers
- synchronous cloud-SDK client constructors and their I/O methods (`Client`, `blob`,
  `get_bucket`, `download_as_bytes`, `upload_from_*`, ...)

Configure the call set in `[blocking-in-async]`: `calls` replaces the default set,
`extra-calls` extends it (for example `["fetch_object", "execute_query"]`). Both are lists of
the last dotted component of the call.

Ruff's `ASYNC` rules cover the standard-library cases.

- Calls passed through `asyncio.to_thread`, `anyio.to_thread.run_sync` or
  `run_in_threadpool` are allowed.
- The bodies of nested `def`s and lambdas are not checked.
- Default scan: `src`, `tests`.

## no-dict-schemas

`pystatic.scripts.no_dict_schemas` flags fields typed `dict[str, Any]`,
`dict[str, object]` or `dict[str, str]` in a response schema. Declare a concrete Pydantic
model instead.

A response schema is a class whose name ends in one of these suffixes: `Out`, `Response`,
`Result`, `Row`, `DTO`, `View` (extend with the `suffixes` key).

The `[no-dict-schemas]` table sets three keys:

- `scan`: the directories to check. Default `src/schemas`. Only the `.py` files directly
  inside each one are checked.
- `suffixes`: replaces the suffix list.
- `open-fields`: names of fields that the contract declares as open maps.

## enum-literals

`pystatic.scripts.enum_literals` flags an enum value written as a string literal in any of
these places:

- a comparison with an attribute or name
- an assignment to an attribute or name
- a keyword argument

The attribute or name must be one of `status`, `kind`, `role`, `stage`, `outcome`, `mode`,
`action` or `tier`.

- Rule: `enum-literals-hand-typed`.
- Values come from the `[enum-literals]` table:
  - `values` lists them.
  - `values-from-enums = true` adds the string value of every member of every `Enum`
    subclass in the scanned code.
- `names` replaces the eight names.
- A keyword is accepted when the callee declares that field as a `Literal[...]` that
  contains the value. Callees are resolved through imports, with module names computed under
  `import-roots`, default `src` and `.`.
- Assignments inside an `Enum` class body are not checked.
- Default scan: `src`, `tests`.
- Skipped without the table.

## config-literals

`pystatic.scripts.config_literals` keeps magic numbers out of code that must compute on
configuration. The `[config-literals]` table drives two rules.

`config-literals-numeric` applies to the production modules under `scan`, not to `test_*`
files. Each `scan` entry is a directory (every `.py` file under it) or one `.py` file, so a
settings module at an import root can be judged without its sibling packages. An entry that
names neither is a configuration error. Only the numbers in `allowed` (default `0`, `1`, `-1`)
may appear as numeric literals.
These are also allowed:

- the value of an `UPPER_SNAKE` constant whose lower-case name is listed in `keys`
- subscript positions
- Pydantic `Field(ge=, gt=, le=, lt=)` bounds

`config-literals-class-default` and `config-literals-class-no-config` apply to each class in
`config-classes`. Such a class is built only from configuration, so it declares no field
default and is never constructed with no arguments.

Skipped without the table.

## registry-files

`pystatic.scripts.registry_files` checks that every member of a registry enum owns exactly
one file in its directory, and that nothing else is there. That rules out versioned copies
and files for unknown members. Each `[[registry-files]]` entry sets these keys:

- `enum`: `path.py:Class`. The class is read by AST, never imported.
- `directory`: the directory that holds the files.
- `pattern`: the file name for a member. Default `{value}.md`. Use `{name}` for the member
  name.
- `exclude`: members that own no file.
- `ignore`: directory entries that are not registry files.

The rules are `registry-files-missing` and `registry-files-stray`. Skipped without an entry.

## config-scoping

`pystatic.scripts.config_scoping` enforces "no suppressions" at the configuration level.
It reads these entries from `<root>/pyproject.toml`:

- ruff global ignores, per-file ignores and excludes
- mypy `ignore_errors`, `disable_error_code`, `ignore_missing_imports` and `exclude`
- pytest `filterwarnings` entries that start with `ignore:`

Each entry must appear, with a reason, in the reviewed table. That table defaults to
`<root>/reviewed-suppressions.toml`, and `--reviewed` sets another path.

- A reviewed entry that the config no longer holds is reported as
  `no-suppressions-stale-review`.
- A missing table reviews nothing.
- A missing `pyproject.toml` is a tool error.

`reviewed-suppressions.example.toml` shows the table format.

## json-schemas

`pystatic.scripts.json_schemas` validates committed JSON documents with `check-jsonschema`,
which is a kit dependency. Each `[[json-schemas]]` entry sets these keys:

- `schema`: the schema file.
- `files`: globs relative to the root.
- `name-field`: optional. Each file must then be named
  `<lower-cased value of that field>.json`.

| Rule | Reported when |
| --- | --- |
| `json-schemas-invalid` | A document violates its schema |
| `json-schemas-unparseable` | A file is not valid JSON |
| `json-schemas-duplicate-key` | An object repeats a key, which `json.loads` would otherwise hide |
| `json-schemas-file-name` | A file breaks the `name-field` rule |
| `json-schemas-missing-schema` | Documents exist but their schema file does not |

Skipped without an entry. Tool error when `check-jsonschema` is missing.

## deployment-files

`pystatic.scripts.deployment_files` checks deployment and CI files.

`deployment-test-input`: a production Compose service sets an environment key that names a
test or seed input. A key names one when either of these holds:

- one of its `_`-separated words is in `input-words`
- it has a word in `replay-words` and a word in `locator-words`

Keys listed in `allowed-keys` are exempt.

`deployment-test-mount`: a production Compose file takes something from a `forbidden-trees`
directory (default `tests`) through any of these:

- a volume
- an `env_file`
- a secret file
- a config file

`ci-full-history-blobs`: an `actions/checkout` step uses `fetch-depth: 0` without
`filter: blob:none`.

Every key of `[deployment-files]` is optional:

- `compose` defaults to whichever of `compose.yaml`, `compose.yml`, `docker-compose.yaml`
  and `docker-compose.yml` exist.
- `workflows` defaults to `.github/workflows`.

Compose's `!reset` and `!override` tags are read as plain values.

## file-size

`pystatic.scripts.file_size` reports each tracked or untracked-but-not-ignored file
(`git ls-files --cached --others --exclude-standard`) under `paths` (default `tests/fixtures`)
that is larger than `max-bytes` (default 10 MiB). Rule: `file-size-over-ceiling`. Without the
table, the check runs on these defaults.

- `suffixes` limits the judged files to those extensions (default: every file).
- `page-paths` lists paths whose PDFs and PPTX decks hold at most `max-pages` pages or slides
  (default 2). Rules: `file-size-over-page-budget`, and `file-size-pages-not-countable` for a
  file of another format there. It needs the `pages` extra (`pypdf`, `python-pptx`).

## source-ownership

`pystatic.scripts.source_ownership` reports each tracked or untracked-but-not-ignored source file
(`git ls-files --cached --others --exclude-standard`) that lies outside every
project in `projects`, as `source-without-validation-owner`.

- `extensions` sets which files count as sources. The default covers Python, TypeScript,
  JavaScript, CSS, SQL, Cypher and shell.
- A project can claim files outside its directory in its own `pyproject.toml`:

  ```toml
  [tool.source-ownership]
  external = ["../scripts/report_*.py"]
  ```

- The configuration can declare files external too, one `[[source-ownership.external]]` entry
  per group. `paths` are globs under `--root` (`dir/**/*` for a whole tree). `reason` is
  required: the written justification. `owner`, optional, names the configured project whose
  checks or tests run over the files; leave it out only for files no project validates, such
  as vendored third-party sources, and say so in `reason`. An entry with an unknown key, no
  `reason`, an `owner` that is not a configured project, or a pattern that matches no file is a
  configuration error, so a declaration cannot outlive the files it justifies.

  ```toml
  [[source-ownership.external]]
  paths = ["tools/release.sh"]
  owner = "services/orders"
  reason = "services/orders/tests/test_release.py runs it"

  [[source-ownership.external]]
  paths = ["vendor/charts/**/*"]
  reason = "vendored read-only third-party bundle; validated by nobody here"
  ```

- A configured project directory that does not exist is a configuration error.
- Skipped without the table.

## runtime-defaults

`pystatic.scripts.runtime_defaults` keeps documented runtime defaults out of test code.
Tests should read them from one generated file instead. The `[runtime-defaults]` table sets
`defaults`, a JSON object of `SETTING_NAME: number`. You can generate it from the
documentation (see `schema-specific/docs-oracle.md`) or from the settings class.

A numeric literal in the `scan` directories (default `tests`, with suffixes `.py` and `.ts`)
is reported as `runtime-defaults-hard-coded` when both of these hold:

- it equals a setting's default
- the setting's name appears within five lines, as `SETTING_NAME`, `setting_name` or
  `settingName`

These are not reported:

- the value assigned to the quoted setting name, as in an override mapping
- comments and docstrings
- `0` and `1`

Skipped without the table.

## docs-bundle

`pystatic.docs.check` validates a Markdown documentation bundle. The bundle lives under
`root` (default `docs`), and its conventions come from `[docs-bundle]`.

| Rule | Checks |
| --- | --- |
| `docs-frontmatter` | The `fields`, `optional-fields`, `types`, `statuses` and per-folder `types`. Tags must be documents of `tag-folder` or `extra-tags`. A document linked from a tag document's `reading-order-heading` section must have tags. An `index.md` below the root has no frontmatter |
| `docs-numbered-record` | Records in a `numbered-records` folder: the file name matches `filename`. The first heading matches `heading`, and the slug of its joined groups equals the file stem |
| `docs-heading` | No numbered, non-ASCII or `forbidden-heading-chars` headings, and no duplicate heading slugs in one file |
| `docs-duplicate-id` | An identifier matching `id-pattern` in a table's first cell is declared only once across the bundle |
| `docs-id-placement` | Rows matching an `id-placement` pattern stay out of its `forbidden-folders`, except `allowed-files` |
| `docs-ephemeral` | No work-in-progress markers outside code fences, reported once per pattern per file. The default patterns cover status glyphs, revised markers, code citations (`path.py:12`), section-number references, `[NOT IMPLEMENTED` and revision notes. Configured `[[docs-bundle.ephemeral]]` entries replace them. `ephemeral-exempt` frees the text after a heading in one file |
| `docs-link` | Every absolute internal link (`/path.md`, `/path.md#anchor`, `#anchor`) resolves to a file and a GitHub-style heading slug |
| `docs-index-stale` | Every generated `index.md` matches `pystatic.docs.indexes` |

`pystatic.docs.indexes` writes the indexes:

- The root index holds `root-preamble`, then `# root-title`, then one `# <folder title>`
  list per configured folder. A folder with a `summary` gets that one line instead of a list.
- Each folder gets its own index, which also links its sub-folders.
- Within a folder, documents named in `order` come first, then the rest alphabetically.

`--check` writes nothing and exits 1 when an index is stale. The runner's `docs-bundle` step
is skipped when the bundle directory is absent.

## package-data

`pystatic.scripts.package_data` builds the target's wheel with
`uv build --wheel --no-build-isolation`. The build backend must already be installed in the
environment. A data file matches a wheel member either by its path from the repository root
(`src/app/q.sql`) or by its path from `package-dir` (`app/q.sql`, the standard src layout).

| Rule | Reported when |
| --- | --- |
| `package-data-missing-from-wheel` | A file under `package-dir` (default `src`) with one of the `data-suffixes` (default `.sql`, `.cypher`) is not in the wheel |
| `package-data-test-support` | A module or package under `package-dir` is test support, named `test_*`, `conftest`, `fixtures` or `fixture_*` |
| `package-data-build-failed` | The build fails |

## openapi-snapshot

`pystatic.openapi_snapshot` writes the OpenAPI document that an app serves to a snapshot
file, as deterministic JSON: sorted keys, two-space indent, trailing newline.

- `--app MODULE:ATTRIBUTE` names either of these:
  - an object with an `.openapi()` method, such as a FastAPI app
  - a factory that takes no arguments and returns one
- `--check` writes nothing. It exits 1 with `openapi-snapshot-stale` when the committed
  snapshot differs, so contract drift fails review.
- `--secret-env NAME` sets a throwaway random value for a settings guard that refuses to
  build the app without that secret.

This tool imports the target, so run it with the project's interpreter, with the kit
installed there.

## length-limits

`pystatic.style.length_limits` reports three things:

- a production module longer than 3000 lines, using pylint `too-many-lines`
- any pylint pragma that would suppress that check
- a comment block or docstring longer than 5 lines

It also flags PEP 695 generic syntax (`def f[T]`, `class C[T]`, `type X = ...`), because
that syntax stops semgrep from matching `async def` patterns in the same file. Use
`TypeVar` or `TypeAliasType` instead.

- Test modules and files under `tests/` or `fixtures/` are exempt from the module-length
  limit only.
- Files outside the project directory are included when the project's
  `[tool.source-ownership] external` lists them as globs.

## dead-code

`pystatic.style.dead_code` runs vulture over the scopes that the project's
`pyproject.toml` declares. Each finding prints as `ABSOLUTE_PATH:LINE:1: vulture-dead-code:
MESSAGE`, with no fix or doc tail; the exit code is 1 on findings and 2 on a tool error.

```toml
[[tool.dead-code.scopes]]
sources = ["src/**/*.py"]           # owned: findings are reported here
consumers = ["tests/**/*.py"]       # supply usage, own no findings
exclude = ["src/generated/**"]
```

Code that a framework calls is not reported as unused:

- functions with registration decorators: routes, validators, pytest fixtures and hooks,
  `override`
- `NodeVisitor.visit_*` methods and `TestCase` set-up methods
- fields of data classes (Pydantic, enums, protocols, typed dicts, dataclasses)
- Alembic revision identifiers

## clone-inputs

`pystatic.style.clone_inputs` prepares Python test files for a clone detector such as
jscpd. It reads a JSON list of paths on stdin. It writes a JSON object that maps each path
to the line numbers to leave out of the comparison:

- imports
- assertion oracles: a native `assert` built only from names, attributes and intrinsic calls
  (`len`, `str`, `set`, `sorted`, `isinstance`, `all`, `any`), and `assert_*(...)` helper
  calls built the same way

Assertions that perform actions (calls, `await`, walrus) stay in the comparison. So does a
shadowed intrinsic. Independent oracles therefore never count as duplicated code, while
duplicated setup still does.

```sh
echo '["tests/test_orders.py"]' | python -m pystatic.style.clone_inputs --root /path/to/project
```

## clones

`pystatic.scripts.clones` reports Python code copied between or within files: jscpd token
clones of at least `min-lines` lines and `min-tokens` tokens. The kit is zero-baseline: every
clone is a finding, `clones-duplicate`, at the later copy, naming the earlier one.

What is compared:

- every tracked or untracked-but-not-ignored `.py` file (`git ls-files --cached --others
  --exclude-standard`) under the `paths` pathspecs; nothing else filters the list
- each file with its imports blanked, and each test file (a `tests` pattern) as
  [clone-inputs](#clone-inputs) prepares it: imports and independent assertion oracles
  blanked, so shared expectations never count while duplicated setup still does. Line
  numbers are kept

The `[clones]` table:

| Key | Default | Meaning |
| --- | --- | --- |
| `paths` | required | git pathspecs under `--root` (`"."` for the whole tree) |
| `tests` | `test_*.py`, `*_test.py`, `conftest.py`, `tests/*` (also below any directory) | root-relative fnmatch patterns of test files; `*` crosses `/` |
| `min-lines`, `min-tokens` | `10`, `100` | the smallest clone reported |
| `mode` | `weak` | jscpd token mode: `strict`, `mild` or `weak` (comments and blank lines left out) |
| `format` | `python` | the jscpd tokenizer language |
| `threshold` | `0` | the duplicated-line percentage allowed; only `0` is accepted |
| `reporters` | `["json"]` | `json` writes `<reports>/clones-jscpd.json` (statistics and every clone), `sarif` adds `<unit>/clones.sarif` |

The status line carries the totals: `416 files, 4 clones, 0.15% duplicated lines`.

The engine is the TypeScript kit's `duplicates/detect-sources.mjs`, the native jscpd detector
of its duplicates check, run with `node`. It needs Node.js 24 and `npm ci` in
`validation/typescript/scripts`. `PYSTATIC_NODE` and `PYSTATIC_JSCPD_ENGINE` point at another
`node` or engine script. A missing `node` or engine is a tool error (`tool-missing`), as are
a file Python cannot parse and an engine failure. A `paths` list that matches no Python file
is a configuration error, so the check cannot pass on an empty scan. Skipped without the
table.

```sh
python -m pystatic.scripts.clones --root /path/to/project --config pystatic.toml
```

## collected-docs

`pystatic.harness.collected_docs` is a helper for a project's own pytest session. Install it
with the `pytest` extra. It enforces "every test module cites the documentation it checks".

- `declared_anchors(module)` reads a module's `ANCHOR` or `DOC_ANCHORS`.
- `citation_problems(request, owner)` returns two lists for every module collected beside
  the owner file:
  - the modules that declare no anchor
  - the anchors that resolve to no heading under `PYSTATIC_DOCS_ROOT`

A one-test owner module in the project looks like this:

```python
def test_every_module_cites_a_doc(request):
    undeclared, dead = citation_problems(request, Path(__file__).resolve())
    assert (undeclared, dead) == ([], [])
```

## Catalogue

[`RULES.md`](RULES.md) lists every rule id, what it enforces, why, and a bad and good example.
