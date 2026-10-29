# Python lint kit

A drop-in, project-agnostic set of strict Python checks: ruff, mypy (with the pydantic plugin), import-linter contracts, deptry, check-jsonschema, pytest and coverage settings, and a semgrep rule pack with fixtures. Rules whose logic is generic but whose input is project knowledge (module names, vendor SDK, entity fields, banned terms) ship as configurable rules with neutral examples. Every semgrep rule id starts with `kit.`. The kit tests itself.

The catalogue of every rule, script and setting is in [RULES.md](RULES.md).

## Purpose

- Fail on anything a strict reviewer would reject, without per-project tuning.
- Keep a finding a finding: nothing is hidden by a comment (`# noqa`, `# type: ignore`, `# nosemgrep`, skipped tests). The runner ignores `noqa` and `nosemgrep` on purpose.
- Be ready to use. The adopter sets paths, the root package name and the few configurable inputs, then runs one command.
- Be isolated. Nothing in the kit imports or resolves a file outside its own folder; every tool is declared in the kit's `pyproject.toml`.

## Layout

| Path | Role |
|---|---|
| `run.sh` | Single entry point: runs every check over a project, fail-slow. |
| `selftest.sh` | Validates the kit itself. |
| `pyproject.toml` | The kit's manifest: pinned tool versions in dependency groups and the pytest settings of its self-tests. |
| `ruff.toml` | Strict ruff lint and format configuration. |
| `mypy.ini` | Strict mypy configuration with the pydantic plugin. |
| `importlinter.toml` | Four generic import contracts for a layered package. |
| `pyproject.fragment.toml` | pytest, coverage and deptry tables to copy into the target's `pyproject.toml`. |
| `jsonschema-checks.example.txt` | Example input of the check-jsonschema step. |
| `.gitignore` | Keeps the kit's `.venv/` and tool caches out of version control. |
| `.semgrepignore` | Exclusion list to copy to the scanned project's root. |
| `semgrep/*.yml` | Generic semgrep rule files, grouped by topic. |
| `semgrep/fixtures/` | One fixture per rule file (`# ruleid:` / `# ok:` markers) plus path-scoped fixtures under `src/` and `outside_src/`. |
| `configurable/semgrep/*.yml` | Semgrep rules whose names, paths or patterns the adopter edits (see below), with fixtures in `configurable/semgrep/fixtures/`. |
| `configurable/importlinter.confinement.example.toml` | Example purity and confinement import contracts to copy and edit. |
| `kit_tools/importlinter_source_coverage.py` | Checker that hand-written `source_modules` lists name every top-level package. |
| `examples/clean_project/` | A neutral sample project that every check, and the shipped configurable examples, accept; used by the end-to-end self-test. |
| `tests/` | Self-tests (pytest). |

## Requirements

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). Versions are pinned exactly in `pyproject.toml`:

| Group | Tools | Installed by |
|---|---|---|
| `lint` | ruff 0.16.7, mypy 2.3.1, pydantic 2.13.5 (the `pydantic.mypy` plugin), semgrep 1.177.0, import-linter 2.15, grimp 3.17, deptry 0.25.1, check-jsonschema 0.38.0 | default |
| `test` | pytest 9.1.1, pytest-randomly 5.0.0, pytest-timeout 2.4.0, pyyaml 6.0.3, types-pyyaml 6.0.12.20260906 | default |
| `target-test` | pytest 9.1.1, pytest-randomly, pytest-timeout, pytest-asyncio 1.4.0, pytest-cov 7.1.0, pytest-socket 0.8.1, pytest-xdist 3.8.0, coverage 7.16.0 | `uv sync --group target-test` |

`target-test` lists the plugins the target project's own test suite needs for the settings in `pyproject.fragment.toml`; add them to the target's test environment. The kit itself does not need them.

```
uv sync
```

## Adopt in a new project

1. Copy the whole kit folder into the project (or keep it in a shared location and pass absolute paths). Run `uv sync` in it once.
2. Copy `.semgrepignore` to the root of the scanned project. Semgrep reads it only from its project root. Without it, semgrep's built-in list skips every `tests` folder, which would silently disable the `kit.tests.*` rules.
3. Merge the tables of `pyproject.fragment.toml` into the target's `pyproject.toml`. Set `testpaths`, `[tool.coverage.run] source`, `[tool.deptry] known_first_party`, optionally `fail_under`, and register every custom pytest marker under `markers` (the settings use `--strict-markers`). Install the `target-test` plugins in the target's test environment. Enable `asyncio_mode` only together with pytest-asyncio (`--strict-config` rejects the key otherwise).
4. Import contracts: `importlinter.toml` assumes the root package `src` and feature packages `src.<feature>` with optional `routes`, `service` and `repositories` modules. Change `root_package` and every `src.` prefix to your root package. Contracts over layers you do not have are kept, not errors. Pass another file with `IMPORTLINTER_CONFIG`.
5. Path-scoped semgrep rules default to these locations; edit `paths.include` / `paths.exclude` in the rule files to match your layout:
   - `**/src/core/**`: purity rules (`determinism.yml`) and an exclusion of the environment-read rule.
   - `**/src/**`: application-code rules.
   - `**/src/errors.py`, `**/src/main.py`, `**/src/config.py`: the designated owners that the matching rules exclude.
   - `**/src/schemas/**` and `**/src/routes/**`: pydantic schema and route rules.
   - `**/tests/**`: test rules.
6. Drop rule files you do not need (for example `fastapi.yml`). Rules whose scope matches no file are inert.
7. Adjust `ruff.toml` where your layout differs: `extend-exclude`, the banned APIs and the per-file ignores for tests. Project-specific bans (a dropped SDK, a superseded class) go in as rows of `banned-api`; commented examples are in the file.
8. Exclude the vendored kit from the target's own checks. `run.sh` already excludes a kit folder located inside the project from ruff, mypy, semgrep and deptry (for deptry it adds the folder to the project's `[tool.deptry] extend_exclude` entries, which it re-passes, instead of replacing them). For tools you call directly, add the kit folder to `[tool.deptry] extend_exclude` (otherwise deptry reports the kit's `importlinter` import as DEP001), to ruff `extend-exclude`, to mypy `exclude` and to the copied `.semgrepignore`. Its `tests/` and `examples/` hold a second `src` package that must not be linted as project code.
9. Install the target's dependencies in the interpreter given by `KIT_TARGET_PYTHON` (see the environment table) so mypy can resolve third-party imports.
10. Configure the optional parts you want (next section).

### Configurable rules

Each is a generic check that needs your names as input. Copy the file, edit it, and run it with `KIT_SEMGREP_CONFIGURABLE` (absolute path to the file or folder). Mirror every edited name in the matching file under `configurable/semgrep/fixtures/` and run the self-test so the rule stays proven.

| File | What to set | Example config |
|---|---|---|
| `configurable/semgrep/gateway.yml` | The outbound-client package in `paths:` and the vendor SDK import spelling(s) replacing `vendor_sdk`. | [gateway.yml](configurable/semgrep/gateway.yml) |
| `configurable/semgrep/llm.yml` | The model package scope, model-id prefixes, the client constructor and settings object, and the file-log names (written for pydantic-ai `Agent`). | [llm.yml](configurable/semgrep/llm.yml) |
| `configurable/semgrep/pipeline.yml` | The pipeline package scope and the name pattern of immutable files. | [pipeline.yml](configurable/semgrep/pipeline.yml) |
| `configurable/semgrep/schema_fields.yml` | The entity name, its state field and its typed field, plus the owner module excluded. | [schema_fields.yml](configurable/semgrep/schema_fields.yml) |
| `configurable/semgrep/banned_terms.yml` | The regex alternation of terms production code must not contain. | [banned_terms.yml](configurable/semgrep/banned_terms.yml) |
| `configurable/semgrep/traceability.yml` | The pytest marker name that tests must carry (and register it). | [traceability.yml](configurable/semgrep/traceability.yml) |
| `configurable/importlinter.confinement.example.toml` | Module names of the core, the vendor SDK package, the numeric library and module, and approved importers. Copy the contracts you need into the file given by `IMPORTLINTER_CONFIG`. | [example](configurable/importlinter.confinement.example.toml) |
| `jsonschema-checks.example.txt` | One `<schema> <files>` line per JSON Schema validation. Copy to the project root as `jsonschema-checks.txt` or set `JSONSCHEMA_CHECKS`. | [example](jsonschema-checks.example.txt) |

Building the inputs:

- Semgrep rules: they read nothing external; the "input" is the edit of the rule file itself (paths, names, the regex).
- Import contracts: list real modules only. A `forbidden` contract aborts the run when a `source_modules` entry is missing from the import graph, and a forbidden module that is absent is silently ignored. For hand-written source lists, enable the coverage checker below so a new top-level package cannot escape a contract.
- JSON Schema checks: write the `jsonschema-checks.txt` lines yourself; `vendor.<name>` selects a check-jsonschema built-in schema.

## Run against a project

```
uv sync
uv run bash run.sh /path/to/project
```

`PROJECT_DIR` defaults to the current directory. From that directory the script runs the steps below. Names in the first column are the ones `KIT_SKIP` takes.

| Step | Command | When |
|---|---|---|
| `ruff-format` | `ruff format --check` | always |
| `ruff` | `ruff check --ignore-noqa` | always |
| `mypy` | `mypy` with `mypy.ini` | always |
| `semgrep` | `semgrep scan --config semgrep ... --error --disable-nosem` | always |
| `import-linter` | `lint-imports` with the contracts file | always |
| `import-linter-coverage` | `kit_tools/importlinter_source_coverage.py` | when `IMPORTLINTER_COVERAGE_ARGS` is set |
| `deptry` | `deptry .` | when the project has a `pyproject.toml` |
| `jsonschema` | `check-jsonschema`, one call per line | when the checks file exists |

Every step runs even after a failure. The exit status is 0 clean, 1 if any step failed, 2 if the project directory is missing.

Environment variables:

| Variable | Meaning |
|---|---|
| `IMPORTLINTER_CONFIG` | Import contracts file (default: the kit's `importlinter.toml`). Relative paths resolve from the project directory. |
| `IMPORTLINTER_COVERAGE_ARGS` | Arguments of the coverage checker; unset skips the step. |
| `KIT_SEMGREP_CONFIGURABLE` | Semgrep file or folder added to the shipped rules, normally your edited copy of `configurable/semgrep`. Absolute path. |
| `JSONSCHEMA_CHECKS` | File of `<schema> <files>` lines (default `<project>/jsonschema-checks.txt`; absent: step skipped). |
| `KIT_TARGET_PYTHON` | Interpreter of the target project's environment, passed to mypy as `--python-executable` (default `<project>/.venv/bin/python` when it exists). |
| `KIT_SKIP` | Space-separated step names to skip. |

The kit's own virtual environment holds only the lint tools. The target's dependencies (web framework, ORM, HTTP client, ...) must be installed in the interpreter named by `KIT_TARGET_PYTHON`, otherwise strict mypy reports `import-not-found` for every third-party import. deptry maps distributions to module names most reliably when it runs inside the target environment (`cd /path/to/project && <target env>/bin/deptry .`, or skip the `deptry` step and run it that way).

Commands:

```
uv sync
uv run bash run.sh /path/to/project
IMPORTLINTER_CONFIG=importlinter.toml uv run bash run.sh /path/to/project   # relative paths resolve from the project dir
KIT_SEMGREP_CONFIGURABLE="$PWD/configurable/semgrep" uv run bash run.sh /path/to/project   # after editing the configurable rules; absolute path
# vendor-sdk-confined exists only in configurable/importlinter.confinement.example.toml: copy that contract into your contracts file first
IMPORTLINTER_COVERAGE_ARGS="--contracts importlinter.toml --source-root src --contract vendor-sdk-confined --exempt gateway" uv run bash run.sh /path/to/project
JSONSCHEMA_CHECKS=/path/to/jsonschema-checks.txt uv run bash run.sh /path/to/project
KIT_SKIP="import-linter deptry" uv run bash run.sh /path/to/project
uv run python kit_tools/importlinter_source_coverage.py --contracts /path/to/importlinter.toml --source-root /path/to/src --contract <id> [--exempt <package>]
cd /path/to/project && uv run --project /path/to/kit deptry .
```

The last line runs deptry directly against the project's own dependency tables.

## Wire into CI and pre-commit

CI: install uv, then run the kit as one job step; a non-zero exit fails the job.

```
uv sync --project path/to/kit
uv run --project path/to/kit bash path/to/kit/run.sh .
```

Run the target's own tests in a separate step with the `target-test` plugins and the pytest/coverage tables from `pyproject.fragment.toml`. Run `uv run bash selftest.sh` in the kit folder only when you change the kit or edit a configurable rule.

Pre-commit: add a local hook that calls the same command with `pass_filenames: false`, and use `KIT_SKIP` to keep the hook fast (for example skip `mypy` and `deptry` locally and keep them in CI).

## Self-tests

```
uv sync
uv run bash selftest.sh
```

`selftest.sh` runs, in order, the equivalents of:

```
uv run semgrep --test --metrics=off --config semgrep semgrep/fixtures
uv run semgrep --test --metrics=off --config configurable/semgrep configurable/semgrep/fixtures
uv run pytest -c pyproject.toml -q -p no:cacheprovider tests
```

followed by ruff format, ruff check (`--ignore-noqa`) and mypy over the kit's own Python. It sets `PYTHONDONTWRITEBYTECODE=1` so no caches land in the kit.

| File | Checks |
|---|---|
| `tests/test_semgrep_rules.py` | `semgrep --test` passes for the shipped and the configurable rules with every rule file covered by fixtures; every rule id is `kit.`-namespaced and unique; every rule is severity ERROR with a message; path scoping and exclusions on real scans (tunable literals, HTTP exceptions, request ids, deterministic core, application layer, SDK confinement); `# nosemgrep` is reported by a real scan. |
| `tests/semgrep_support.py` | Helper: resolves `semgrep` from `PATH`, runs it, and a `real_scan` plus `assert_scoping` pair for `paths:` tests. |
| `tests/test_compat_shims.py` | Import-only modules are flagged; implementation modules, type definitions, constants, `__init__.py` and empty modules are not. |
| `tests/test_module_probes.py` | The module-feature-probe rule finishes within a timeout on a file with 200 imports and 1200 ordinary getters, reports no errors and finds the one real probe. |
| `tests/test_importlinter_contracts.py` | Each contract in `importlinter.toml` has a pair of tests on an in-memory import graph (kept without the forbidden edge, broken with it); every contract survives a graph with no matching module; no contract lacks a pair. |
| `tests/test_importlinter_source_coverage.py` | The coverage checker: listed packages and modules inside them count, a missing package is reported, exempt packages are not required, a name prefix is not coverage, exit statuses. |
| `tests/test_run_sh.py` | End to end on a copy of `examples/clean_project`: a clean project passes every step, an added violation fails the run, a missing directory exits 2, a kit vendored inside the project keeps the project's `[tool.deptry] extend_exclude` (a colocated `test_*.py` importing pytest raises no deptry finding), and the shipped `configurable/semgrep` examples (passed with `KIT_SEMGREP_CONFIGURABLE`) accept the sample project. |
