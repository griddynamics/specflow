# SQL validation rules

Every rule the SQL kit enforces, grouped by the `sqlcheck` step that reports it. Each
finding's `doc:` points at its step's heading here. Inputs come from `sqlcheck.toml`
(see `sqlcheck.example.toml`) and the declared schema `sql-schema.json` (see
`sql-schema.example.json`).

## Conventions

The conventions the steps below enforce, each with the rule that carries it:

- Sessions come from a dependency; nothing but the database module builds an engine or
  session factory (`kit.sql.self-opened-engine-outside-database-module`).
- The service layer owns one transaction per unit of work; nothing else commits, rolls
  back or begins one, and no transaction stays open across a network call
  (`kit.sql.commit-outside-service`, `kit.sql.no-network-call-in-open-session`).
- Alembic migrations are reversible and the history has one head
  (`alembic-lint-noop-downgrade`, `alembic-lint-not-single-head`, `migrations-*`).
- Rows of a table outside the hard-delete allow-list are deactivated, never deleted
  (`sql-rules-delete-not-allowlisted`, `kit.sql.delete-outside-allowlist`).
- Every statement lives in a query file loaded at runtime, never a string built in Python
  (`inline-queries-sql`, `inline-queries-missing-file`, `inline-queries-orphan-file`).
- Name the columns, never `SELECT *` into a model (SQLFluff `AM04`, reported as
  `sqlfluff-AM04`).
- Filters, ordering and limits belong in the statement, not in Python over the rows it
  returned (`kit.store_work.*`).
- A read of every row of a table must be registered on purpose
  (`orm-lint-unregistered-scan`).

| Step | Kind | Needs the project's environment | Inputs |
| --- | --- | --- | --- |
| `sqlfluff` | static, `.sql` | no | `.sqlfluff`, `[paths].sql` |
| `sql-rules` | static, `.sql` | no | `[paths].sql`, `[paths].schema`, `[paths].migration_sql_dirs` |
| `alembic-lint` | static, migrations | no | `[paths].alembic_versions` |
| `orm-lint` | static, Python | no | `[paths].python`, `[orm_lint]`, `[paths].schema` |
| `inline-queries` | static, Python | no | `[paths].python`, `[inline_queries]` |
| `semgrep` | static, Python | no | `semgrep/`, `[semgrep]`, `[paths].schema` |
| `schema-parity` | dynamic | yes (imports the models) | `[schema_parity]`, `[paths].schema` |
| `migrations` | dynamic | yes (runs `env.py`) | `[migrations]` |
| `sqlite-pragmas` | dynamic | yes (calls the engine factory) | `[sqlite_pragmas]` |

A step whose inputs are not configured reports `skipped: <reason>`; a step that runs with
part of its input missing reports `degraded: <reason>`. Nothing is skipped silently.

## sqlfluff

SQLFluff lints every `.sql` file under `[paths].sql` with every core rule (aliasing,
ambiguity, capitalisation, conventions, layout, references, structure). The kit's
`.sqlfluff` sets `templater = raw` and `disable_noqa = True`, so an inline `-- noqa`
suppresses nothing. Findings are `sqlfluff-<CODE>` (for example `sqlfluff-LT01`).

## sql-rules

Policy rules over SQLFluff's parse tree of every `.sql` file.

| Rule | Reports | Why |
| --- | --- | --- |
| `sql-rules-no-where` | an `UPDATE` or `DELETE` with no `WHERE` of its own (a `WHERE` inside a subquery does not count) | An unbounded write touches every row. |
| `sql-rules-delete-not-allowlisted` | a `DELETE` on a table not in `hard_delete_allowlist` | Rows are deactivated, not deleted, unless the schema says otherwise. Without a schema file every `DELETE` is reported. |
| `sql-rules-append-only-table` | an `UPDATE` or `DELETE` on a table in `append_only_tables` | Audit and event logs are insert-only. |
| `sql-rules-drop-outside-migration` | a `DROP TABLE/INDEX/TRIGGER/VIEW/...` in a file not directly inside one of `migration_sql_dirs` | Schema changes are migrations. |
| `sql-rules-unknown-table` | a table the declared schema does not have (CTE names are known) | SQL and the schema stay in step. |
| `sql-rules-unknown-column` | an alias- or table-qualified column the declared table does not have | Same. Unqualified columns are not judged: which table they belong to is ambiguous. |
| `sql-rules-unknown-qualifier` | a qualifier that is no alias, CTE or declared table | Same. |
| `sql-rules-noqa-comment` | any comment containing `noqa` | Suppressions are not allowed; the comment itself is the finding. |
| `sql-rules-parse-error` | text SQLFluff cannot parse | The other rules cannot see inside it. |

The reference rules (`unknown-*`) run only when `[paths].schema` points at a schema file.

## alembic-lint

Static checks over `[paths].alembic_versions`, by AST only (no migration is imported).

| Rule | Reports |
| --- | --- |
| `alembic-lint-noop-downgrade` | a `downgrade()` that is empty, `pass` or a bare docstring. Raise with a reason when no downgrade is possible. |
| `alembic-lint-not-single-head` | a revision graph with no head or several (merge revisions with a tuple `down_revision` count every parent). |
| `alembic-lint-undocumented-destructive-op` | a `drop_table`, `drop_column`, `drop_index`, `drop_constraint`, or an `op.execute` whose SQL parses as a `DROP` or `DELETE`, whose object the revision docstring does not name. A `downgrade()` drop of what the same revision's `upgrade()` created is exempt, and so is a call whose object name cannot be read at all. |

## orm-lint

SQLAlchemy rules over the Python files under `[paths].python`, by AST only.

| Rule | Reports |
| --- | --- |
| `orm-lint-missing-lazy` | a `relationship()` with no `lazy=`. |
| `orm-lint-disallowed-lazy` | a `lazy=` other than `"raise"`, `"raise_on_sql"` or `"selectin"`, or a non-literal one. Every other loading strategy can issue a hidden query on attribute access. |
| `orm-lint-unregistered-scan` | a `select()` over a model (a name imported from `[orm_lint].models_package`) that is executed with no `limit`, no key the request supplies (a function parameter, or a value derived from one, inside `where`/`filter`/`filter_by`/`having`/`join`/`select_from`) and no equality on a column the declared schema calls unique, and that the scan register does not admit. Aggregates, `exists`, subqueries and statements returned or handed to a helper are not judged. |
| `orm-lint-stale-scan-entry` | a `[orm_lint.scan_register]` `methods` or `classes` entry that admits no read of every row. |
| `orm-lint-stale-complete-list-entry` | a `complete_lists` entry that admits no read of every row. |
| `orm-lint-doubly-registered-read` | a method listed in both `methods` and `complete_lists`. |

Register entries are dotted paths below `[orm_lint].module_root`
(`app.repositories.orders.OrderRepository.export_all`), or bare class names whose every
method may read every row. A column declared `"unique": {"where": "<col>"}` (or
`<col> = 1`, `<col> IS TRUE`) is a key only for the value true; any other partial
predicate is no key. Without `models_package` only the `lazy=` rules run.

## inline-queries

Python files hold no SQL text: statements live in query files and are loaded at runtime.
By AST only. Docstrings are exempt, and test files (`tests/` directories, `test_*.py`,
`*_test.py`) are skipped, plus any `[inline_queries].exclude` glob.

| Rule | Reports |
| --- | --- |
| `inline-queries-sql` | a string constant or f-string of at least 20 characters shaped like a statement: `SELECT ... FROM`, `INSERT [OR ...] INTO`, `UPDATE ... SET`, `DELETE FROM`, `CREATE/ALTER/DROP` of a table, index, view, trigger or type, `TRUNCATE TABLE`, `VACUUM`, `ATTACH DATABASE`. In a `migration_dirs` module only text passed to `execute(...)` or `text(...)` counts; Core constructs such as `create_table` are not raw SQL. A `PRAGMA` string is a connection setting, not a query: it is not reported, and `sqlite-pragmas` checks its effect. |
| `inline-queries-missing-file` | a call to a `[inline_queries.loaders]` function whose path argument (a literal, a module-level constant, a loop variable over one, or an f-string over one with `.lower()`/`.upper()`) resolves to no file below the loader's directory. |
| `inline-queries-orphan-file` | a query file below `orphan_roots` (default `[paths].sql`) that no loader call reaches. Runs only when `loaders` is non-empty; otherwise the step reports `degraded`. |

## semgrep

Semgrep rules over the Python files under `[paths].python`, run with `--disable-nosem`.
Adjust the `paths:` globs and the `src.repositories` module path in the rule files to the
project's layout.

`semgrep/sql.yml`:

| Rule | Reports |
| --- | --- |
| `kit.sql.commit-outside-service` | `commit()`, `rollback()` or `begin()` on a session-, `db`-, connection- or `uow`-named target outside `**/src/services/**` and the database module. The service layer owns the unit of work. |
| `kit.sql.self-opened-engine-outside-database-module` | `create_engine(...)`, `create_async_engine(...)`, `sessionmaker(...)`, `async_sessionmaker(...)`, `scoped_session(...)` or `Session(engine)` outside the database module. Sessions come from a dependency. |
| `kit.sql.no-network-call-in-open-session` | an `httpx` or `requests` call inside `async with session.begin():` or `with session.begin():` (the synchronous form is matched for both libraries). No transaction is held across a network call. |

Rendered from the declared schema at every `semgrep` step (or by `sqlcheck render-semgrep`):

| Rule | Reports |
| --- | --- |
| `kit.sql.delete-outside-allowlist` | `session.delete(obj)` where the variable name names no `hard_delete_allowlist` table (with or without underscores). The rule sees names, not types. |
| `kit.sql.no-mutation-of-append-only-table` | an ORM `update(Model)` or `delete(Model)` on the model of an `append_only_tables` table (`orm_models`, else the CamelCase of the table name). |

`semgrep/store_work.yml` ("the store does the data work"):

| Rule | Reports |
| --- | --- |
| `kit.store_work.repository-result-shaped-in-python` | a repository that filters, orders, searches or takes the extreme of the rows a statement returned, instead of a `WHERE`, `ORDER BY` or `LIMIT`. |
| `kit.store_work.repository-record-projection-filtered` | a repository that builds rows from records and leaves some out. |
| `kit.store_work.repository-result-sliced` | a repository that slices a statement's rows to a count. |
| `kit.store_work.caller-shapes-repository-read` | a caller that filters, orders or searches the rows a repository read returned. A predicate over two stores is admitted once each read took the other's keys. |
| `kit.store_work.caller-slices-repository-read` | a caller that slices a repository read's rows. |
| `kit.store_work.hand-rolled-vector-maths` | dot products, norms and similarity written by hand (or with numpy) outside the one module set aside for vectors no store holds. |
| `kit.store_work.stdlib-text-similarity` | `difflib` text similarity. |

## schema-parity

The declared schema equals the ORM's `MetaData` (`[schema_parity].metadata`). Findings
point at the schema file.

| Rule | Reports |
| --- | --- |
| `schema-parity-tables` | a table on one side only (`ignore_tables` excluded). |
| `schema-parity-columns` | a column on one side only (`implicit_columns` are added to the declared side). |
| `schema-parity-type` | a declared type whose family (longest prefix in `[schema_parity.type_families]`) does not hold the ORM type; a `TypeDecorator` is judged by its `impl`. |
| `schema-parity-unmapped-type` | a declared type that matches no family. |
| `schema-parity-enum` | a declared `enum` that differs from the ORM enum's values or their order. |
| `schema-parity-foreign-key` | a declared `fk` missing on the ORM column, or an ORM foreign key that is not declared. |
| `schema-parity-unique` | declared single-column uniqueness that differs from the ORM's; `{"where": ...}` needs a unique index on that column whose predicate compiles to that text for each of `conditional_unique_dialects` (compared case- and whitespace-insensitively, table qualifiers dropped). |
| `schema-parity-index` | a declared `indexes` column with no ORM index, `index=True`, single-column unique constraint or primary key. |

Type, enum, key and index checks apply to tables in the object form; a table given as a
plain list of column names is checked by name only. `sqlcheck build-schema` writes the
object form from the models (or a live database) and keeps the policy keys.

## migrations

Runs the project's Alembic migrations against a throwaway database (`url_template`, with
`{path}` a temporary file).

| Rule | Reports |
| --- | --- |
| `migrations-not-single-head` | a history with more than one head. |
| `migrations-upgrade-failed` | `upgrade head` failing on an empty database. |
| `migrations-drift` | `alembic check` finding a difference between the migrated database and the models after `upgrade head`. |
| `migrations-round-trip-failed` | a revision that does not upgrade, downgrade to its parent and upgrade again, each from an empty database. |

## sqlite-pragmas

Calls the project's engine factory (`[sqlite_pragmas].factory`, sync or async) on a
temporary database file and reads each pragma in `[sqlite_pragmas.expected]` on a real
connection.

| Rule | Reports |
| --- | --- |
| `sqlite-pragmas-mismatch` | a pragma whose value differs from the expected one (text compares case-insensitively). Typical expectations: `journal_mode = "wal"`, `foreign_keys = 1`, `busy_timeout` above zero. |

## Configurable rules

Rules that need the adopter's facts read them from an input the adopter supplies. All of
them ship a neutral example (users and orders style tables, `app.models`, `app.database`)
and none required a separate specification.

| Rule family | Input | How to build it |
| --- | --- | --- |
| `sql-rules-*` references, allow-list, append-only | `sql-schema.json` (`tables`, `hard_delete_allowlist`, `append_only_tables`) | `sqlcheck build-schema --metadata MOD:ATTR` or `--url URL`, then edit the policy keys; see `sql-schema.example.json` |
| `kit.sql.delete-outside-allowlist`, `kit.sql.no-mutation-of-append-only-table` | the same schema (`orm_models` optional) | `sqlcheck render-semgrep`; an empty allow-list flags every `session.delete` |
| `orm-lint-unregistered-scan` and register findings | `[orm_lint]`, `[orm_lint.scan_register]` | write the dotted names of intended full-table reads; see `sqlcheck.example.toml` |
| `schema-parity-*` | `[schema_parity]` and the schema | `metadata = "package.module:Base.metadata"`, extend `type_families` |
| `migrations-*` | `[migrations]` | point `alembic_ini` at the config, pick `url_template` matching the `env.py` driver |
| `sqlite-pragmas-mismatch` | `[sqlite_pragmas]` | name the engine factory (sync or async) and the expected pragmas |

Path-scoped Semgrep rules (`kit.sql.commit-outside-service`,
`kit.sql.self-opened-engine-outside-database-module`, `kit.sql.no-network-call-in-open-session`,
every `kit.store_work.*`) assume a `src/` tree with `services/`, `repositories/` and
`database/` (or `database.py`) at any depth, as in `src/app/database.py`; edit the `paths:` globs and, in
`store_work.yml`, the repository module path and the `graph_*`/`*_graph` module regexes.
`kit.store_work.hand-rolled-vector-maths` exempts one path glob for vectors no store holds;
set it to the adopter's module or leave it matching nothing. `kit.store_work.caller-shapes-repository-read`
treats method names matching `^[a-z_]+$` on a repository handle as repository reads (the `$READ`
metavariable-regex in the two repository-read sources of that rule and in `caller-slices-repository-read`); narrow all three together when callers use other helpers.
