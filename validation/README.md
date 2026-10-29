# Validation kit

A standalone, project-agnostic, ready-to-use set of static (and some dynamic) validations for Python, TypeScript/React, Cypher and SQL code bases. It bundles linters, semgrep rule packs, static-check scripts and architecture checks together with the tools that run them, each configured and documented, so an adopter only sets configuration inputs and runs it.

- **Project-agnostic.** No rule names a product, feature, module, table or document of any particular project. Where a check needs project facts (layers, enums, schemas, routes, tokens, catalogues, banned terms), it reads them from a small input file you supply or build with a shipped step. Neutral examples (users/orders, Person/Movie, `app.api`/`app.domain`) ship alongside.
- **Design-system-library-free.** No rule or configuration is tied to a specific component library.
- **Isolated.** Nothing in this folder imports or resolves a file outside it. Every tool is declared in the track's own manifest (`pyproject.toml` or `package.json`) with pinned versions.
- **Self-testing.** Each track carries its own self-tests and fixtures, so a copy can be proven working before it is pointed at your code.

## Layout

| Track | Folder | What it provides |
|---|---|---|
| Python lint | [python/lint](python/lint/README.md) | ruff, mypy (pydantic plugin), import-linter and grimp, deptry, check-jsonschema, pytest and coverage settings, the semgrep rule pack (`kit.*` ids) and configurable semgrep rules |
| Python scripts | [python/scripts](python/scripts/README.md) | `pystatic`: AST, pylint, vulture and schema checks, documentation-bundle checker and index generator, OpenAPI snapshot, clone-input filter, jscpd clone check (`clones`, Node.js engine from typescript/scripts) |
| Python architecture | [python/arch](python/arch/README.md) | `archkit`: a configurable pytest suite for the HTTP contract: error-code catalogue, error envelope, request ids, route contract and the OpenAPI snapshot |
| TypeScript lint | [typescript/lint](typescript/lint/README.md) | ESLint flat config with its plugins and local rules, stylelint, semgrep, knip, Prettier, strict `tsc` base, Size Limit, strict Vitest options |
| TypeScript scripts | [typescript/scripts](typescript/scripts/README.md) | Node checks: i18n catalogues, tokens, contrast, routes, navigation tree, API contract map, bundle size, unused CSS, clone detection, semgrep wrapper |
| Cypher | [cypher](cypher/README.md) | `cypher-checks`: schema lint, DDL parity, CyVer, EXPLAIN (Docker), inline-query and parameter-name checks, Neo4j semgrep rules |
| CI report | [ci](ci/README.md) | `ci_report.py`: stdlib-only wrapper that records every check or test step (exit code, SARIF, JUnit) and turns the jobs' reports into one GitHub Actions run summary with annotations |
| SQL | [sql](sql/README.md) | `sqlcheck`: SQLFluff, SQL policy rules, Alembic and ORM lint, inline-query check, semgrep rules, schema parity, migration and SQLite pragma checks |

Each track has a `RULES.md` cataloguing every rule. Tracks with a check that depends on your schema and is specified rather than shipped as code keep that spec in `schema-specific/` (python/scripts, typescript/scripts, cypher).

## Prerequisites

| Need | For |
|---|---|
| Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) | every Python track, cypher, sql, and the semgrep install used by the TypeScript tracks (`uv sync`) |
| Node 24 or newer and npm | TypeScript tracks |
| semgrep (pinned in each track's `pyproject.toml`; installed by `uv sync`) | semgrep steps |
| Docker daemon | only the live Cypher steps (`cyver`, `explain`) and the Cypher integration tests; use `--static-only` otherwise |
| Git | only where a check lists changed files |

## Adoption steps

1. **Copy** this folder into your repository (for example as `validation/`), or keep it in a shared location and pass absolute paths. Copy only the tracks you need; they do not depend on each other.
2. **Install** each track's tools from inside its folder: `uv sync` (Python, cypher, sql), `npm install --workspaces=false` and `uv sync` (TypeScript lint and TypeScript scripts; `uv sync` installs the pinned semgrep). `run-all.sh --install` does this for you.
3. **Set the configuration** the track documents: root package name and path scopes (python/lint), `pystatic.toml` (python/scripts), `archkit.toml` (python/arch), `kit.config.mjs` and `validation.config.json` (TypeScript), `cypher-checks.toml` and `graph-schema.yaml` (cypher), `sqlcheck.toml` and `.sqlfluff` (sql). Each track ships an example to copy and edit: an `*.example.*` file (python/lint, python/scripts, TypeScript, sql), or its `examples/` folder for cypher (`examples/cypher-checks.toml`, and `examples/schema.yaml` copied to `graph-schema.yaml`) and python/arch (`examples/shop/archkit.toml`).
4. **Build schema inputs** for the configurable rules. Typical build steps: `cypher-checks derive-schema` (graph schema from a development database), `sqlcheck build-schema` (declared schema from ORM metadata), `archkit build-catalogue` and `archkit export-openapi` (error-code catalogue and OpenAPI snapshot), `openapi-to-contracts.mjs` (API contracts from an OpenAPI document), `build-banned-terms.mjs` (banned-terms semgrep rule), `pystatic.openapi_snapshot` (OpenAPI snapshot).
5. **Prove the copy** with `sh run-all.sh --selftest` (or a single track's self-test).
6. **Run** against your project (see below) and fix findings. The kits are zero-baseline: a finding is a defect, and no inline suppression comment hides it.
7. **Wire into CI and pre-commit.** Call `run-all.sh` (or one track's entry point) as one CI job per track; each track README has a "CI and pre-commit" or "Wiring" section with details. Copy `.semgrepignore` (python/lint) and `prettierignore.project` (typescript/lint) to the project root where the track says so.

## Quick start per language

Replace `/path/to/project` with the project root.

**Python**

```
cd python/lint    && uv sync && uv run bash run.sh /path/to/project
cd python/scripts && uv sync --all-extras && uv run python -m pystatic.run --root /path/to/project --config /path/to/project/pystatic.toml --level all
# python/arch runs in the project's own environment (install it there first, see python/arch/README.md):
cd /path/to/project && uv add --dev --editable /path/to/validation/python/arch && uv run archkit run --config archkit.toml
```

**TypeScript**

```
cd typescript/lint    && npm install --workspaces=false && uv sync && node run.mjs --project /path/to/project
cd typescript/scripts && npm install --workspaces=false && uv sync && node run.mjs --root /path/to/project
```

**Cypher**

```
cd cypher && uv sync
uv run cypher-checks all --config /path/to/project/cypher-checks.toml --static-only   # no Docker
uv run cypher-checks all --config /path/to/project/cypher-checks.toml                 # adds cyver and explain
```

**SQL**

```
cd sql && uv sync && bash run.sh /path/to/project
```

`python/arch` imports your app, so it must run from your project's environment with archkit installed there (`uv add --dev --editable validation/python/arch`); `run-all.sh` uses `KIT_TARGET_PYTHON`, else `<project>/.venv/bin/python`, for it. Its `--selftest` still runs in the kit's own venv.

Dynamic SQL steps (`schema-parity`, `migrations`, `sqlite-pragmas`) import your application code, so run them from your project's environment; see [sql/README.md](sql/README.md).

## Run everything

`run-all.sh` is a POSIX sh script that runs each track's entry point from its own folder and stops at the first failure.

```
sh run-all.sh --project /path/to/project                       # all tracks
sh run-all.sh --project /path/to/project --tracks python-lint,sql
sh run-all.sh --project /path/to/project --skip cypher
sh run-all.sh --project /path/to/project --install --static-only
sh run-all.sh --selftest                                       # the kit's own tests
sh run-all.sh --list
```

| Flag | Meaning |
|---|---|
| `--project DIR` | Project to validate (required unless `--selftest`) |
| `--tracks LIST` | Comma-separated subset: `python-lint`, `python-scripts`, `python-arch`, `typescript-lint`, `typescript-scripts`, `cypher`, `sql` |
| `--skip LIST` | Tracks to leave out |
| `--install` | Install each track's tools first |
| `--static-only` | Cypher without Docker |
| `--selftest` | Run each track's self-test instead of checking a project |

A track whose required input file is missing in the project (`archkit.toml`, `cypher-checks.toml`, `sqlcheck.toml`) is skipped with a notice, not failed. Track-specific switches (for example `KIT_SKIP`, `KIT_SEMGREP_CONFIGURABLE`, `IMPORTLINTER_CONFIG`, `JSONSCHEMA_CHECKS`) are read from the environment; see each track README.

## Notes

- Some small helpers (finding output, config loading, semgrep invocation) exist in more than one track. Each track is kept self-contained on purpose so it can be copied alone; the copies are not shared across tracks.
- Python tracks and the TypeScript tracks each have their own lockfile or manifest. Versions are pinned per track and may be updated independently.
- Run commands from the folders documented here; the kit does not need, and must not be given, access to files outside the folder it checks.
