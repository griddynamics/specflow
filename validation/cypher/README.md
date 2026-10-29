# cypher-checks

Static and live validation of Cypher query files against a declared graph schema, plus the
Python-side checks that keep query text out of application code. Project-agnostic: every
project fact (labels, relationships, constraints, allowances) comes from a schema file and a run
configuration that you supply. The rule catalogue with rationale and examples is in
[RULES.md](RULES.md); every finding ends in `doc: RULES.md#<heading>`.

## Layout

| Path | Content |
|---|---|
| `cypher_checks/` | The checks: `lint`, `parity`, `cyver_check`, `explain`, `no_inline_queries`, `param_names`, `derive_schema`, `run_all`, plus `schema`, `query_files`, `fixture_graph`, `container`, `neo4j_container`, `findings`. |
| `semgrep/neo4j.yml` | Semgrep rules for Neo4j driver use in Python; `semgrep/neo4j.py` holds the paired test cases. |
| `examples/` | A neutral Person/Movie/Genre example: `cypher-checks.toml`, `schema.yaml`, `queries/`, `schema/constraints.cypher`, `app/`. |
| `schema-specific/` | Specs of checks that need your schema as input and are not shipped as code. |
| `tests/` | Self-tests and fixtures (CyVer, EXPLAIN, inline-query fixtures). |
| `pyproject.toml` | Tool manifest and configuration: ruff, mypy (strict), pytest (strict), coverage. |
| `RULES.md` | The rules, why they exist, and every finding id. |

## Requirements

- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).
- Docker daemon, only for the live steps (`cyver`, `explain`) and the integration tests. They
  start a throwaway Neo4j container (default image `neo4j:2026.08.1-community`).
- Runtime dependencies (pinned in `pyproject.toml`): `cyver 2.0.2`, `docker 7.2.0`, `neo4j 6.3.0`,
  `pyyaml 6.0.3`, `semgrep 1.177.0`, `testcontainers 4.15.0`.
- Dev tools (pinned): `ruff 0.16.7`, `mypy 2.3.1` (+ `types-pyyaml`), `pytest 9.1.1`,
  `pytest-timeout 2.4.0`, `pytest-cov 7.1.0`, `coverage 7.16.0`; build
  backend `hatchling 1.32.4`.

## Adoption

1. Copy the whole `cypher/` folder into your repository (or keep it as a tool folder) and run
   `uv sync` inside it.
2. Create your inputs from the examples, then edit both:
   - `cypher-checks.toml`: the run configuration (paths, live settings, Python scan, semgrep).
   - `graph-schema.yaml`: the graph schema and `policy` registers.
3. Build the schema input from a development database instead of typing it, then add
   `common_label`, `enums` and `policy` by hand (see below).
4. Run `all --static-only` first; add the live steps once Docker is available.
5. Adapt `semgrep/neo4j.yml` to your layout: the `**/graph/**` exclude globs name the one module
   allowed to build drivers and sessions, and the loader regex `^load_\w*(?:query|cypher)\w*$`
   names your loader functions. Point `[semgrep] rules` at the edited copy. Without `rules`, `all` uses the kit's own file: the
   wheel ships it as `cypher_checks/semgrep/neo4j.yml`, a source checkout reads `semgrep/neo4j.yml`.

### Inputs to set

| Input | Keys |
|---|---|
| `cypher-checks.toml` | `schema`, `root`, `queries`, `ddl`; `[live] enabled`, `image`; `[python] root`, `loaders`, `query_dirs`, `orphan_exempt`, `exclude`, `migrations`; `[semgrep] enabled`, `paths`, `rules`. A step whose input is missing prints `skipped: <reason>`. |
| schema `nodes`, `relationships` | Labels with typed properties; relationship types with `from`, `to`, `properties`. Feed unknown-type, unknown-property, vector and fixture-graph logic. |
| schema `common_label`, `common_properties` | A label and properties every node carries. |
| schema `constraints.unique`, `constraints.indexes` | `Label.property` lists: `MERGE` keys, anchor rule, total order, DDL parity. |
| schema `policy.ddl_globs` | DDL files: many statements, `CREATE` allowed. |
| schema `policy.write_globs`, `write_allowed_globs` | Where write files may exist; empty disables the rule. |
| schema `policy.scan_shaped_queries`, `banned_scan_labels` | Intentional scans, and labels whose scan is a plan finding. |
| schema `policy.capped_traversals` | Per variable-length query: `query` (file stem), `hard_cap`, `key`. |
| schema `policy.create_allowed_globs`, `node_removals` | Files allowed to re-point edges, and files with the labels/types they may `DELETE`. |
| schema `policy.merge_scope_keys` | Properties every uniqueness constraint is composite with (a tenant key): a node `MERGE` may name them beside its one key, each a `$parameter`. |
| schema `policy.batch_merge_queries` | Batch writes whose `MERGE` key may be a row property of any earlier `UNWIND $rows AS row` (or of a list property of such a row), never rebound. |
| schema `policy.placeholder_labels` | Placeholder labels of templates mapped to the concrete labels they stand for: DDL parity expands a declared `Placeholder.property` to each concrete label (an empty list asks for no DDL). |
| schema `policy.vector_read_queries` | Files allowed to return vector properties. |
| schema `policy.read_globs`, `read_owners` | Optional read register, cross-checked in both directions. |

A register entry is a query file stem; case is ignored and `_`, `-` and spaces are equivalent.

### Building the inputs

- Config and schema: copy `examples/cypher-checks.toml` and `examples/schema.yaml`.
- Schema from a live database (labels, properties, relationship endpoints, unique, range and
  vector indexes): the `derive-schema` command below. It drafts only; add `common_label`,
  enums and `policy` yourself.
- DDL: one `CREATE CONSTRAINT ... IF NOT EXISTS` / `CREATE INDEX ... IF NOT EXISTS` per
  statement in files matching `policy.ddl_globs`; `parity` keeps them equal to the schema file.

## Commands

Exit codes: 0 clean, 1 findings, 2 tool or configuration error.

```sh
uv sync
cp examples/cypher-checks.toml <your-project>/cypher-checks.toml && cp examples/schema.yaml <your-project>/graph-schema.yaml   # then edit both
NEO4J_PASSWORD=<pw> uv run cypher-checks derive-schema --uri bolt://localhost:7687 [--user neo4j] [--database <db>] > <your-project>/graph-schema.yaml   # build step: draft the schema from a dev database, then add common_label/enums/policy by hand

uv run cypher-checks all --config <your-project>/cypher-checks.toml   # every step; cyver/explain need Docker
uv run cypher-checks all --config <your-project>/cypher-checks.toml --static-only   # no Docker

uv run cypher-checks lint --schema <schema.yaml> --root <your-project> <queries-dir-or-files>...
uv run cypher-checks parity --schema <schema.yaml> --root <your-project> <ddl-dir-or-files>...
uv run cypher-checks cyver --schema <schema.yaml> [--image neo4j:<tag>] <queries-dir>...
uv run cypher-checks explain --schema <schema.yaml> [--image neo4j:<tag>] <queries-dir>...
uv run cypher-checks no-inline-queries <src-root> --loader load_query=<queries-dir-under-root> [--query-dir <dir>] [--orphan-exempt <glob>] [--exclude <glob>] [--migrations <glob>]
uv run cypher-checks param-names <src-root> --loader load_query=<queries-dir-under-root> [--exclude <glob>]
uv run semgrep scan --config semgrep/neo4j.yml --error --metrics off <src-root>

CYPHER_CHECKS_NEO4J_IMAGE=neo4j:<tag> uv run cypher-checks all --config <your-project>/cypher-checks.toml   # pin the server image per run
```

The server image resolves from `--image`, then `CYPHER_CHECKS_NEO4J_IMAGE`, then `[live] image`,
then the pinned default; only `neo4j:` images are accepted.

## CI and pre-commit

- CI: run `uv sync` once, then `uv run cypher-checks all --config cypher-checks.toml` on a runner
  with Docker. Without Docker use `--static-only`; the job fails on any non-zero exit.
- Pre-commit: a local hook running `uv run cypher-checks all --config cypher-checks.toml
  --static-only` with `pass_filenames: false`, triggered on `\.(cypher|py|ya?ml|toml)$`. The live
  steps are slow, so keep them in CI.
- Pin the server version with `CYPHER_CHECKS_NEO4J_IMAGE` to the one you deploy.

## Self-tests

```sh
uv sync
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run semgrep --test semgrep/
uv run pytest -m "not integration"
uv run cypher-checks all --config examples/cypher-checks.toml --static-only
uv run pytest -m integration   # needs a running Docker daemon
uv run pytest --cov   # full suite with Docker; coverage gate fail_under=85
```

Pytest runs strict (strict markers and config, `xfail_strict`, warnings are errors, 600 s
timeout). Mypy runs in strict mode and Ruff with a broad rule selection; both are configured in
`pyproject.toml` and apply to the kit's own code.

## Catalogue

Full text, rationale and examples for each id: [RULES.md](RULES.md).

### `lint` (static, no database)

| Group | Ids |
|---|---|
| File shape | `cypher-lint-missing-header-comment`, `cypher-lint-multiple-statements`, `cypher-lint-templating-marker` |
| Bounded paths | `cypher-lint-unbounded-path`, `cypher-lint-missing-length-filter`, `cypher-lint-hard-cap-exceeded`, `cypher-lint-wrong-config-key`, `cypher-lint-unregistered-capped-query` |
| Anchoring and ordering | `cypher-lint-not-anchored`, `cypher-lint-limit-without-order`, `cypher-lint-order-not-total` |
| Writes | `cypher-lint-merge-no-key`, `cypher-lint-merge-multiple-keys`, `cypher-lint-merge-key-not-parameter`, `cypher-lint-merge-key-label-unconstrained`, `cypher-lint-create-not-allowed`, `cypher-lint-delete-not-allowed`, `cypher-lint-write-outside-write-folder`, `cypher-lint-write-outside-capability`, `cypher-lint-shared-set-in-both-branches`, `cypher-lint-set-unknown-property` |
| Vocabulary and vectors | `cypher-lint-unknown-relationship-type`, `cypher-lint-unregistered-vector-read` |
| Ownership | `cypher-lint-read-without-owner`, `cypher-lint-owner-row-without-read` |
| Formatting | `cypher-lint-keyword-not-uppercase`, `cypher-lint-multiple-match-per-line`, `cypher-lint-qpp-uncommented` |

### Other commands

| Command | Findings |
|---|---|
| `parity` | `parity-unique-missing-in-ddl`, `parity-unique-undeclared`, `parity-index-missing-in-ddl`, `parity-index-undeclared`, `parity-ddl-not-idempotent`, `parity-no-ddl-files` |
| `no-inline-queries` | `no-inline-queries-cypher`, `no-inline-queries-sql`, `no-inline-queries-missing-file`, `no-inline-queries-orphan-file` |
| `cyver` | `cyver-schema-score`, `cyver-properties-score`, CyVer metadata codes (syntax, schema, properties) |
| `explain` | server notifications (GQLSTATUS), `AllNodesScan`, `NodeByLabelScan`, `cap-mismatch`, planning errors |
| `param-names` | `cypher-lint-param-mismatch` |
| `derive-schema` | Build step, no findings: drafts the schema file from a live database. |
| `all` | Runs lint, parity, no-inline-queries, param-names, semgrep, cyver and explain; exits with the worst code. |

### Semgrep (`semgrep/neo4j.yml`)

| Id | Enforces |
|---|---|
| `neo4j.autocommit-session-run` | No auto-commit `session.run(...)`; use managed transactions. |
| `neo4j.loaded-query-mutated` | A loaded query is never concatenated, formatted, replaced or put in an f-string. Adapt the loader regex. |
| `neo4j.driver-constructed-outside-graph-module` | One shared driver, built in the graph module. Adapt the exclude glob. |
| `neo4j.ad-hoc-session-outside-graph-module` | Sessions are opened only in the graph module. Adapt the exclude glob. |

### Configuration files

| File | Purpose |
|---|---|
| `pyproject.toml` | Dependencies, `cypher-checks` console script, ruff, mypy, pytest, coverage. |
| `examples/cypher-checks.toml` | Run configuration template for `all`. |
| `examples/schema.yaml` | Graph schema and policy template. |
| `.gitignore` | Tool caches and the virtual environment. |

## Configurable rules

Rules that read a schema `policy` register or the declared schema are listed in
[RULES.md](RULES.md#configurable-rules). Their input is `examples/schema.yaml` (neutral
Person/Movie example). One check is specified for adopters to implement:
[schema-specific/register-entries-bind-to-files.md](schema-specific/register-entries-bind-to-files.md).
