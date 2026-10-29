# SQL validation kit

A project-agnostic, ready-to-use set of SQL validations for a Python project that uses
SQL files, SQLAlchemy and Alembic. One entry point, `sqlcheck`, runs every step and
reports findings in text or JSON. It is isolated: it depends on nothing outside this
folder, and every tool it drives is pinned in its own `pyproject.toml`.

Every rule is documented in [RULES.md](RULES.md).

## What it checks

| Step | Kind | What it enforces |
| --- | --- | --- |
| `sqlfluff` | static | every SQLFluff core rule over `.sql` files, with no inline suppression |
| `sql-rules` | static | policy over the SQL parse tree: bounded writes, hard-delete allow-list, append-only tables, DROP only in migrations, tables and columns exist in the declared schema |
| `alembic-lint` | static | real downgrades, a single head, documented destructive operations |
| `orm-lint` | static | explicit safe `lazy=` on relationships, no unregistered read of every row of a table |
| `inline-queries` | static | no SQL text in Python files, loader paths that resolve, no orphan query files |
| `semgrep` | static | transaction ownership, engine construction, no network call in an open session, "the store does the data work", schema-bound delete and append-only rules |
| `schema-parity` | dynamic | the declared schema equals the ORM `MetaData` |
| `migrations` | dynamic | one head, clean upgrade, no drift, per-revision round trip |
| `sqlite-pragmas` | dynamic | WAL, foreign keys and busy timeout on a real connection |

A step whose inputs are not configured is reported `skipped`; a step with partial input is
reported `degraded` in its detail (the status stays `findings` when findings exist). Exit status: 0 clean, 1 findings, 2 tool or configuration error.

## Layout

```
.sqlfluff                  drop-in SQLFluff configuration
sqlcheck.example.toml      drop-in kit configuration (neutral example layout)
sql-schema.example.json    example of the declared schema input
pyproject.toml             the kit's package, pinned tools, test/ruff/mypy/coverage settings
run.sh                     run every configured step over a project with the kit's environment
selftest.sh                validate the kit itself
sqlcheck/                  the package (cli, config, one module per step, build-schema, render-semgrep)
semgrep/sql.yml            transaction and engine rules
semgrep/store_work.yml     "the store does the data work" rules
semgrep/fixtures/          ruleid/ok fixtures for `semgrep --test`
tests/                     self-tests and neutral fixtures (sample app with models and Alembic)
```

## Requirements

Python 3.12 or newer and [`uv`](https://docs.astral.sh/uv/). Pinned in `pyproject.toml`:

- runtime: sqlfluff 4.3.0, sqlalchemy[asyncio] 2.0.52, alembic 1.20.0, pyyaml 6.0.3;
- `lint` group: semgrep 1.177.0, ruff 0.16.7, mypy 2.3.1, types-pyyaml;
- `test` group: pytest 9.1.1, pytest-randomly, pytest-timeout, pytest-cov, coverage 7.16.0,
  aiosqlite 0.22.1, greenlet 3.5.5.

The kit's own pytest strictness (strict markers and config, warnings are errors, strict
xfail, random order, timeout), branch coverage (`fail_under = 85`), ruff and strict mypy
settings live in `pyproject.toml`.

## Adoption

1. Copy this folder into the other project (for example `validation/sql`) and run `uv sync` in it.
2. Copy the configuration into the project root and edit it:
   `cp sqlcheck.example.toml <PROJECT>/sqlcheck.toml && cp .sqlfluff <PROJECT>/.sqlfluff`.
   Keys to set:
   - `[run].steps`: the static steps by default; add the dynamic ones only in the project's environment;
   - `[paths]`: `root`, `sql` (directories with `.sql` files), `python`, `schema`,
     `alembic_versions`, `migration_sql_dirs` (the only places a DROP may appear);
   - `[sqlfluff].dialect` (also set `dialect` in `.sqlfluff`; keep them equal);
   - `[orm_lint]`: `models_package`, `not_models`, `models_dir`, `module_root`, and the
     `[orm_lint.scan_register]` lists of intended reads of every row;
   - `[semgrep]`: `generated_dir` (omit it to render into a temporary directory), `paths_include`; optional `rules` to use another rules folder;
   - `[schema_parity]`: `metadata`, `ignore_tables`, `implicit_columns`,
     `conditional_unique_dialects`, `[schema_parity.type_families]`;
   - `[migrations]`: `alembic_ini`, `url_template`, `url_env`;
   - `[sqlite_pragmas]`: `factory`, `url_template`, `factory_kwargs`, `expected`;
   - `[inline_queries]`: `query_suffixes`, `migration_dirs`, `orphan_roots`, `exclude`, and
     `[inline_queries.loaders]` (loader function name = directory its path argument is relative to).
3. Adjust the `paths:` globs in `semgrep/sql.yml` and `semgrep/store_work.yml` (they assume
   a `src/` tree with `services/` and `repositories/` directories and a `database.py` module
   or `database/` package at any depth, as in the example layout `src/app/...`)
   and the repository module path in `store_work.yml` to the project's layout.
4. Build the declared schema (see below), then run.

## Building the inputs

The configurable rules read the project's facts from `sql-schema.json` (example:
`sql-schema.example.json`):

- `tables`: each a list of column names (name check only) or an object with `columns`
  (`type`, `fk`, `enum`, `unique` as `true` or `{"where": "<predicate>"}`) and `indexes`;
- `hard_delete_allowlist`: tables whose rows may be deleted; empty means no delete is allowed;
- `append_only_tables`: insert-only tables;
- `orm_models`: optional table to model-class name map (default: CamelCase of the table).

Bootstrap it from the ORM metadata or a live database, then edit the policy keys by hand;
the build keeps them:

```
uv run sqlcheck build-schema --config <PROJECT>/sqlcheck.toml --metadata app.models:Base.metadata
uv run sqlcheck build-schema --config <PROJECT>/sqlcheck.toml --url sqlite:///app.db
```

## Commands

```
uv sync
cp sqlcheck.example.toml <PROJECT>/sqlcheck.toml && cp .sqlfluff <PROJECT>/.sqlfluff   # then edit [run].steps, [paths], [orm_lint], [schema_parity], [migrations], [sqlite_pragmas], [inline_queries]
bash run.sh <PROJECT>   # every configured step with the kit's own environment (reads <PROJECT>/sqlcheck.toml)
uv run sqlcheck run --config <PROJECT>/sqlcheck.toml --step sqlfluff --step sql-rules --step alembic-lint --step orm-lint --step semgrep
uv run sqlcheck run --config <PROJECT>/sqlcheck.toml --format json
uv run sqlcheck render-semgrep --config <PROJECT>/sqlcheck.toml   # writes the schema-bound semgrep rules to --out, else [semgrep].generated_dir, else <PROJECT>/build/sqlcheck
# dynamic steps import the project's code, so run them from the project's environment: (cd <PROJECT> && uv add --dev --editable ./validation/sql && uv run sqlcheck build-schema --metadata app.models:Base.metadata && uv run sqlcheck run --step schema-parity --step migrations --step sqlite-pragmas)
```

The `semgrep` step renders the schema-bound rules itself before scanning; `render-semgrep`
only writes them for inspection or for use with another semgrep invocation.

## CI and pre-commit

CI: install `uv`, then run the static steps from the kit environment and the dynamic steps
from the project's environment (two jobs, or one job running both commands in order). Use
`--format json` to feed a reporting tool; any non-zero exit fails the job.

```
bash validation/sql/run.sh .            # static steps (list only these in [run].steps)
uv run sqlcheck run --step schema-parity --step migrations --step sqlite-pragmas
```

Pre-commit: a local hook running `bash validation/sql/run.sh .` with `pass_filenames: false`
and `files: '\.(sql|py)$'`. Run the dynamic steps in CI only, since they build databases.

## Self-tests

```
uv sync
uv run semgrep --test --metrics=off --config semgrep semgrep/fixtures
uv run pytest -p no:cacheprovider --cov=sqlcheck --cov-branch --cov-report=term-missing
uv run ruff format --no-cache --check .
uv run ruff check --no-cache .
uv run mypy --cache-dir=/dev/null
uv run sqlcheck run --config tests/fixtures/example_project/sqlcheck.toml
bash selftest.sh
```

`selftest.sh` runs the semgrep fixtures, the static steps over the neutral example project
(`tests/fixtures/example_project/`, laid out as `sqlcheck.example.toml` describes; every
step must report ok, none skipped), pytest with branch coverage, ruff format and check, and
mypy, from this folder only.

The example project is static-only: it has no `alembic/env.py`, and its revision drops a
table without a downgrade, so the dynamic steps (`schema-parity`, `migrations`,
`sqlite-pragmas`) do not run on it. Their working fixture is `tests/fixtures/sample_app/`
(models, engine factory and migrations); `app.models:Base.metadata` in the example
resolves because `app/models/__init__.py` re-exports the models.

## Configurable rules

Every rule that needs project facts is configurable through a shipped input; none needed a
separate specification, so there is no `schema-specific/` folder.

| Rule | Input | Example |
| --- | --- | --- |
| `sql-rules-*` (tables, columns, allow-list, append-only, migration dirs) | `sql-schema.json`, `[paths]` | `sql-schema.example.json` |
| `kit.sql.delete-outside-allowlist`, `kit.sql.no-mutation-of-append-only-table` | `hard_delete_allowlist`, `append_only_tables`, `orm_models`, rendered by `sqlcheck render-semgrep` | `sql-schema.example.json` |
| `orm-lint-unregistered-scan` and the stale/double register findings | `[orm_lint]`, `[orm_lint.scan_register]`, unique columns of the schema | `sqlcheck.example.toml` |
| `inline-queries-*` (loader names and their base directories, migration dirs, orphan roots) | `[inline_queries]`, `[inline_queries.loaders]` | `sqlcheck.example.toml` |
| `schema-parity-*` | `[schema_parity]`, `sql-schema.json` | `sqlcheck.example.toml` |
| `migrations-*` | `[migrations]` | `sqlcheck.example.toml` |
| `sqlite-pragmas-mismatch` | `[sqlite_pragmas]` | `sqlcheck.example.toml` |
| `kit.sql.*`, `kit.store_work.*` path scoping | `paths:` globs in `semgrep/*.yml` | in the rule files |
