# Rule catalogue

Every semgrep rule has severity ERROR and an id of the form `kit.<group>.<name>`. Scope below is the `paths:` setting of the rule; where none is shown the rule scans every Python file. "Excluded" lists files the rule skips because they are the designated owner of the behaviour.

Fixtures for `semgrep --test` sit in `semgrep/fixtures/<rule file>.py` and `configurable/semgrep/fixtures/<rule file>.py` (annotated with `# ruleid:` and `# ok:`). Path-scoped rules are also checked by real scans over `semgrep/fixtures/src/`, `semgrep/fixtures/outside_src/` and `configurable/semgrep/fixtures/src/`.

## 1. ruff (`ruff.toml`)

| Choice | Value | Why |
|---|---|---|
| `line-length`, `target-version` | 100, `py312` | One width and one language level for formatter and linter. |
| `extend-exclude` | `**/semgrep/fixtures/**` | The kit's fixtures are deliberate violations, never lint targets. |
| `select` | A, ARG, ASYNC, B, BLE, C4, C90, DTZ, E, EM, ERA, EXE, F, FAST, FBT, FLY, FURB, G, I, ICN, INP, ISC, LOG, N, PD, PERF, PGH, PIE, PL, PT, PTH, RET, RSE, RUF, S, SIM, SLF, T10, T20, TID, TRY, UP, W, YTT | The strictest generic family list: bugs, security (S), naming, complexity, timezone-aware datetimes, no commented-out code (ERA), no prints (T20), pathlib, pytest style. `PL` includes `PLC0415`, so imports stay at module top. |
| `ignore` | `COM812`, `ISC001` | Conflict with `ruff format`, which owns trailing commas and implicit string concatenation. |
| `ignore` | `FAST003` | Misfires on `Annotated` path-parameter aliases. |
| `isort.required-imports` | `from __future__ import annotations` | Uniform lazy annotations in every module. |
| `flake8-tidy-imports.ban-relative-imports` | `all` | Absolute imports only, so a module's name is its location. |
| `mccabe.max-complexity` | 10 | Bounds branching per function. |
| `pylint` | max-args 8, max-positional-args 5, max-branches 12, max-statements 50, max-returns 6 | Bounds function size and signature width. |

Banned APIs (`flake8-tidy-imports.banned-api`):

| API | Message | Rationale |
|---|---|---|
| `requests` | Use httpx | One HTTP client with sync and async support. |
| `os.path` | Use pathlib | Path objects instead of string handling. |
| `logging.getLogger` | Use structlog.get_logger | One structured logging entry point. |
| `time.sleep` | Wait for the condition, not a duration | Sleeps are guesses about timing. |
| `asyncio.get_event_loop` | Use asyncio.to_thread / get_running_loop | The call is deprecated and ambiguous. |
| `datetime.datetime.utcnow` | Use datetime.now(UTC) | Returns a naive datetime. |
| `pydantic.v1` | Pydantic v2 only | No legacy API surface. |
| `typing.TYPE_CHECKING` | Forbidden: imports stay concrete and visible at runtime | Runtime tools (pydantic, FastAPI, import-linter, deptry) must see every import. |
| `sqlalchemy.orm.declarative_base` | SQLAlchemy 2: subclass DeclarativeBase | Legacy 1.x declarative API; inert in projects without SQLAlchemy. |

Project-specific bans are added as further `banned-api` rows (commented examples in `ruff.toml`): a whole SDK the project has dropped, or a superseded in-house class or function, so the old mechanism cannot return. Two further commented rows: `sqlalchemy.text` (opt-in, SQL lives in `.sql` files) and the application package itself (`"app"`, "a tool never imports the application package"). The latter belongs only to the tooling subtree: put it in a `tools/ruff.toml` with `extend = "<kit>/ruff.toml"` plus that `banned-api` row, never in the config that lints the application.

Per-file ignores (scoping, not suppression): `**/tests/**` and `**/test_*.py` skip `S101` (assert), `ARG001` (fixture arguments), `PLR2004` (literal expectations), `SLF001` (white-box access) and `S603` (subprocess with argv). `**/conftest.py` skips `ARG001`.

Run with `--ignore-noqa` so a `# noqa` hides nothing.

## 2. mypy (`mypy.ini`)

| Option | Effect |
|---|---|
| `python_version = 3.12`, `strict = True` | Full strict mode. |
| `warn_unreachable`, `warn_unused_configs`, `warn_unused_ignores` | Dead code, dead config and stale ignores are findings. |
| `show_error_codes`, `show_column_numbers`, `pretty = False` | Stable one-line output. |
| `local_partial_types` | Partial types cannot leak across scopes. |
| `explicit_package_bases` | Module names come from the directory structure. |
| `enable_error_code` | `ignore-without-code`, `redundant-expr`, `redundant-self`, `truthy-bool`, `truthy-iterable`, `possibly-undefined`, `unused-awaitable`, `explicit-override`, `mutable-override`, `exhaustive-match`, `deprecated`. |
| `exclude` | `(^|/)(semgrep/fixtures|examples/clean_project)/`: the kit's own fixtures and sample project. |
| `plugins` | `pydantic.mypy`, with `init_forbid_extra` and `init_typed` enabled. |

Bad: `x = foo()  # type: ignore`. A code-less ignore is a finding; so is an unused one.

## 3. Import contracts (`importlinter.toml`)

Settings: `root_package = "src"`, `include_external_packages = true`. Wildcards resolve only to modules that exist.

| Id | Type | Enforces |
|---|---|---|
| `feature-layers` | layers, containers `src.*`, layers `(routes)`, `(service)`, `(repositories)`, not exhaustive | Within a feature package a lower layer never imports a higher one. Parenthesised layers are optional. |
| `routes-thin` | forbidden, indirect allowed, source `src.*.routes`, forbidden `src.*.repositories`, `sqlalchemy` | Routes call services and never reach the data layer directly. |
| `no-framework-below-routes` | forbidden, indirect allowed, sources `src.*.service`, `src.*.repositories`, forbidden `fastapi`, `starlette` | The web framework belongs to the routes layer alone. |
| `no-test-imports` | forbidden, source `src`, forbidden `tests`, `vcr`, `pytest`, `_pytest` | Production reaches no test module or test-only library, directly or indirectly. |

Contracts over modules the project does not have are kept, not errors; the confinement contracts that need the project's module names are in [section 8](#8-configurable-rules).

## 4. pytest, coverage, deptry (`pyproject.fragment.toml`)

| Setting | Value | Why |
|---|---|---|
| `addopts` | `--strict-markers --strict-config -ra --import-mode=importlib -p randomly` | Unknown markers and config keys fail; summary of every non-pass; no `sys.path` tricks; random test order exposes hidden coupling. |
| `xfail_strict` | true | An unexpected pass fails. |
| `filterwarnings` | `error` | Warnings are failures. |
| `timeout` | 60 | Each test is bounded (pytest-timeout). The kit's own manifest uses 120. |
| `markers` | commented example | Register every custom marker; `--strict-markers` rejects others. |
| `asyncio_mode`, `asyncio_default_fixture_loop_scope` | commented | Enable with pytest-asyncio; `--strict-config` rejects them without the plugin. |
| command-line switches | documented | `--disable-socket --allow-unix-socket` (pytest-socket, no network), `-n auto --dist loadfile` (pytest-xdist), `--cov` (pytest-cov). Not in `addopts`, since they depend on the suite. |
| `python_files`, `testpaths` | `test_*.py`, `tests` | Set `testpaths` for the target. |
| `[tool.coverage.run]` | `branch = true`, `source = ["src"]` | Branch coverage of the production package. |
| `[tool.coverage.report]` | `show_missing`, `skip_covered`, commented `fail_under` | Report unexecuted lines; set the floor to the project's value. A floor for one package only: `coverage report --include="src/core/*" --fail-under=95`. |
| `[tool.deptry] extend_exclude` | test modules and `conftest.py` | Tests use the validation environment, not runtime dependencies. |
| `[tool.deptry] known_first_party` | `["src"]` | Set to the project's root package. |
| `pep621_dev_dependency_groups`, `package_module_name_map` | commented examples | Dev groups whose imports are never DEP004; map a distribution to an import name that differs (`python-multipart = ["multipart"]`). |

## 5. Semgrep ignore (`.semgrepignore`)

Excludes `.venv/`, `.git/`, `__pycache__/`, the tool caches (`.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `.import_linter_cache/`), `node_modules/`, `dist/`, `build/`, `htmlcov/`, `coverage/` and `**/semgrep/fixtures/`. It deliberately has no tests pattern (see the README).

## 6. Semgrep rules

### python.yml: security basics

| Id | Enforces | Why |
|---|---|---|
| `kit.python.no-hardcoded-secrets` | No hardcoded API keys, tokens or credentials. | Secrets in source leak through the repository. |
| `kit.python.no-dynamic-code-execution` | No executing dynamically built code. | Code injection. |
| `kit.python.no-subprocess-shell` | No subprocess through a shell; pass an argv sequence. | Shell injection. |
| `kit.python.no-disabled-tls-validation` | TLS verification is never disabled. | Removes protection against interception. |

### files.yml

| Id | Enforces | Why |
|---|---|---|
| `kit.files.mime-from-extension-guess` | No `mimetypes.guess_type(...)`. | The type of an uploaded or received file is detected from its content; a name is chosen by the sender and proves nothing. |

### fastapi.yml

| Id | Enforces |
|---|---|
| `kit.fastapi.no-debug-mode` | `debug=True` is never set on the app. |
| `kit.fastapi.no-credentialed-wildcard-cors` | Credentialed CORS uses an explicit origin allowlist, never `*`. |

### imports.yml: no compatibility code

The premise: the lockfile fixes the environment, so code never adapts to what happens to be importable.

| Id | Scope | Enforces |
|---|---|---|
| `kit.python.import-fallback` | all | No `try: import ... except ImportError/ModuleNotFoundError` fallback. |
| `kit.python.sys-path-mutation` | all | No runtime change of `sys.path`. |
| `kit.python.package-dunder-probe` | all | No branching on `__package__` to support two invocation forms. |
| `kit.python.dynamic-import` | all | No `importlib.import_module`, `importlib.util` or `__import__` for a module a plain import could load. |
| `kit.python.module-feature-probe` | all | No `hasattr`/`getattr` guard or fallback on an imported module's attribute. Ordinary use on a non-module value is fine. |
| `kit.python.version-branch` | all | No control flow on `sys.version_info`; `requires-python` pins one interpreter. |
| `kit.python.no-type-checking` | `**/*.py` | No `TYPE_CHECKING` constant or guarded imports. |
| `kit.python.module-binding-alias` | all | A module does not give an imported binding, class or function a second name. `TypeAliasType` aliases and upper-case constants are allowed. |
| `kit.python.explicit-compatibility-comment` | `**/src/**` | A comment declaring retained compatibility code is a finding. |

Bad: `try: import ujson as json\nexcept ImportError: import json`. Good: `import json`.

### compat_shims.yml

| Id | Scope | Enforces |
|---|---|---|
| `kit.python.reexport-only-shim` | all except `**/__init__.py` | A module that only imports and re-exports (optionally with `__all__`) is a shim; consumers import the defining module. Modules with any definition, constant or other statement are not matched. |

### suppressions.yml

| Id | Enforces |
|---|---|
| `kit.suppressions.python-comment-suppression` | No `# noqa`, `# type: ignore`, `# nosec`, `# nosemgrep`, `# pragma: no cover`, `# pylint:` or `# fmt: off` comment. Exceptions belong in checker config. |
| `kit.suppressions.pytest-skip-decorator` | No `skip`, `skipif` or `xfail` marks. |
| `kit.suppressions.pytest-skip-call` | No runtime `pytest.skip(...)` or `pytest.xfail(...)`. |

### tests.yml and test_inputs.yml (scope `**/tests/**`)

| Id | Enforces | Why |
|---|---|---|
| `kit.tests.no-sleep-literal` | No sleeping for a duration in a test. | Wait for the condition or hold the clock. |
| `kit.tests.selection-by-response-position` | No `.json()[N]` row selection. | Position depends on ordering the test does not own; select by a value the test arranged. |
| `kit.tests.selection-by-sorted-id` | No ordering or picking rows by generated `id`. | Ids change on every run. |

### production.yml: production does not know about tests

| Id | Scope | Enforces |
|---|---|---|
| `kit.production.test-justified-comment` | `**/src/**` | A comment must state the product rule, not justify code by a test, recording or replay. |
| `kit.production.test-model-import` | `**/src/**` | No import of `pydantic_ai.models.test` or `pydantic_ai.models.function`. |
| `kit.production.test-runner-branch` | `**/src/**` | No branching on a test runner or test environment. |
| `kit.production.fixture-setting` | `**/src/config.py` | No setting that names a fixture, recording, replay or seed input. |

### pydantic.yml

| Id | Scope | Enforces | Why |
|---|---|---|---|
| `kit.pydantic.no-nested-config-class` | all | No nested `class Config`; use `model_config = ConfigDict(...)`. | v1 idiom. |
| `kit.pydantic.no-loose-dict-field` | `**/src/schemas/**` | No `dict[str, Any]`, `dict[str, str]` and similar field types. | Produces open-ended OpenAPI and index-signature client types; use a submodel. |
| `kit.pydantic.no-any-field` | `**/src/schemas/**` | No `Any` field. | Produces untyped schemas. |
| `kit.pydantic.no-untyped-route-response` | `**/src/routes/**` | No bare dict or `Any` return annotation on a route handler. | Routes declare a typed response model. |

### errors.yml: one error edge

| Id | Scope (excluded) | Enforces |
|---|---|---|
| `kit.errors.http-exception-outside-handlers` | `**/src/**` (`**/src/errors.py`) | `HTTPException` is built only in the error handlers module. |
| `kit.errors.envelope-outside-handlers` | same | No error-status `JSONResponse` or hand-built `{"error": ...}` body outside the handlers. |
| `kit.errors.no-builtin-raise` | same | No raising `ValueError`, `RuntimeError`, `Exception` or `KeyError` in application code; raise a typed application exception. |
| `kit.errors.lifespan-swallowed-failure` | `**/src/main.py` | A startup step in the lifespan must not catch and swallow its failure. |

### logging.yml

| Id | Scope | Enforces |
|---|---|---|
| `kit.logging.no-secret-kwarg-in-log-call` | `**/src/**` | No `password`, `token`, `secret`, `authorization` or `cookie` keyword on a log or `bind` call. |
| `kit.logging.exception-text-exposed` | `**/src/errors.py` | No `str(exc)` in an error handler body; responses carry no stack trace or provider message. |

### security.yml (scope `**/src/**`)

| Id | Excluded | Enforces |
|---|---|---|
| `kit.security.identity-from-header` | none | No identity or role read from an `X-User-*` header. |
| `kit.security.cookie-missing-safe-flags` | none | `set_cookie` carries `httponly`, `secure`, `samesite` and `max_age`. |
| `kit.security.delete-cookie-missing-matching-flags` | none | `delete_cookie` carries `httponly`, `secure` and `samesite` matching the set. |
| `kit.security.runtime-tunable-literal` | `**/src/config.py` | Timeouts, limits, thresholds, retries, batch sizes and `max_*`/`min_*`/`*_dim` bounds are not passed as literals; they come from settings. |
| `kit.security.request-id-outside-middleware` | `**/src/main.py` | A `request_id` is minted (`uuid4()`) only in the middleware. |
| `kit.security.request-id-read-outside-middleware` | `**/src/main.py` | The `X-Request-Id` header is read only in the middleware. |
| `kit.security.environment-read-outside-settings` | `**/src/config.py`, `**/src/core/**` | `os.environ` and `os.getenv` are read only in the settings module. |

### determinism.yml (scope `**/src/core/**`)

For a package that must be a pure function of its inputs. Change `paths.include` to the package that needs it.

| Id | Enforces |
|---|---|
| `kit.determinism.no-nondeterministic-source` | No `time.time/monotonic/perf_counter`, `datetime.now/utcnow`, `date.today`, `random.*`, `secrets.*`, `uuid.uuid1/uuid4`; inject them as arguments. |
| `kit.determinism.no-environment-or-io` | No network, database, file system or environment access. `print` is left to ruff `T20`. |
| `kit.determinism.no-async-def` | No `async def`, nested included. |
| `kit.determinism.no-await-expression` | No `await`. |

## 7. Scripts

| Script | Purpose | Options |
|---|---|---|
| `run.sh [PROJECT_DIR]` | Single fail-slow entry point. Steps: `ruff-format`, `ruff`, `mypy`, `semgrep` (with `--error --disable-nosem`), `import-linter`, `import-linter-coverage`, `deptry`, `jsonschema`. Exit 0 clean, 1 a step failed, 2 no such project. | Argument: directory (default `.`). Env: `IMPORTLINTER_CONFIG`, `IMPORTLINTER_COVERAGE_ARGS`, `KIT_SEMGREP_CONFIGURABLE`, `JSONSCHEMA_CHECKS`, `KIT_TARGET_PYTHON`, `KIT_SKIP`. |
| `selftest.sh` | Validates the kit: semgrep fixtures (shipped and configurable), pytest self-tests (which run `run.sh` on `examples/clean_project`), ruff and mypy over the kit's own Python. | None. |
| `kit_tools/importlinter_source_coverage.py` | Fails when a top-level package (a directory with `__init__.py` under the source root) is named by no `source_modules` entry of a listed contract, as the package or as a module inside it. A "forbidden" contract that exempts one package cannot use a wildcard, so its list is hand-written and a new package would otherwise escape silently. A name prefix is not coverage (`gateway_x` does not cover `gateway`). | `--contracts FILE`, `--source-root DIR`, `--contract ID` (repeatable, required), `--exempt PACKAGE` (repeatable). Exit 0 complete, 1 a package is missing, 2 a named contract does not exist. |

### check-jsonschema (`jsonschema` step)

Validates data files against JSON Schemas. The input is a text file of `<schema> <file or glob>...` lines, one schema per line (empty lines and `#` comments ignored); a schema named `vendor.<name>` is a check-jsonschema built-in, anything else is a schema file path. One `check-jsonschema` call runs per line. Default location `<project>/jsonschema-checks.txt`; example: `jsonschema-checks.example.txt`.

## 8. Configurable rules

Generic checks whose input is the adopter's own names. The shipped values are neutral examples (orders, `vendor_sdk`, `acme`); edit the rule file, mirror the change in its fixture, and run it with `KIT_SEMGREP_CONFIGURABLE`. All have severity ERROR.

### gateway.yml

| Id | Enforces | Set |
|---|---|---|
| `kit.configurable.no-silent-fallback-in-gateway` | A `try`/`except` in the outbound-client package must not return a value; it raises a typed error and the caller decides what unavailability means. Scope `**/src/gateway/**`. | `paths:` |
| `kit.configurable.sdk-import-outside-gateway` | The vendor SDK is imported only by the package that wraps it, so retries, timeouts, credentials and error mapping live in one place. Excludes the gateway and `**/tests/**`. | Replace `vendor_sdk` with the SDK import spellings; `paths.exclude` |

A whole SDK that must never be imported anywhere is a ruff `banned-api` row instead.

### llm.yml (written for pydantic-ai `Agent`; adapt call shapes to another client)

| Id | Enforces | Set |
|---|---|---|
| `kit.configurable.llm-inline-prompt-string` | No prompt or instructions literal (20+ characters) passed inline; prompts live in versioned files. | scope `**/src/llm/**` |
| `kit.configurable.llm-model-id-literal-outside-settings` | No model-id literal outside the settings module; the model is a setting. | id prefixes `gpt-`, `text-embedding-`, `models/`; settings path |
| `kit.configurable.llm-run-missing-timeout` | `agent.run(...)` carries `timeout=`; calls are bounded by the application, not the provider default. | scope |
| `kit.configurable.llm-temperature-literal` | `temperature=` is never a numeric literal; it is a setting. | scope; settings path |
| `kit.configurable.llm-client-not-from-settings` | The provider client takes its credential from settings, not a literal. | constructor `llm_sdk.Client`, settings object `settings` |
| `kit.configurable.llm-no-file-log` | No file-based log of model traffic: no `FileTrafficLogger(...)` constructor and no `traffic_log_path` attribute; traffic goes through the application's structured logging or tracing. | scope; the constructor and attribute names (pair with a ruff `banned-api` row for the import) |

SQLAlchemy session and transaction rules live in the kit's SQL track (`validation/sql`), not here.

### pipeline.yml

| Id | Enforces | Set |
|---|---|---|
| `kit.configurable.no-random-id-in-pipeline-output` | No `uuid4()` for identifiers a pipeline produces; derive them from inputs (checksum, position, `uuid5`) so a re-run yields the same ids. | scope `**/src/pipeline/**` |
| `kit.configurable.no-delete-of-immutable-file` | No `os.remove`, `os.unlink`, `.unlink()` or `shutil.rmtree` on a path whose name matches the immutable-file pattern; only derived caches are cleared. | name pattern `(?i).*original.*` |

### schema_fields.yml

| Id | Enforces | Set |
|---|---|---|
| `kit.configurable.state-field-written-outside-owner` | The `status` field of an entity is assigned only in the owner module, so every transition is auditable in one place. Excludes `**/src/repositories/**`. | entity regex, field name, owner path |
| `kit.configurable.typed-field-assigned-dict-literal` | The structured field (`metadata`) is assigned a typed model, never a raw dict literal. | entity regex, field name |

### banned_terms.yml

| Id | Enforces | Set |
|---|---|---|
| `kit.configurable.no-banned-term` | Production code (`**/src/**`, not fixtures) contains none of the project's customer, brand, supplier or catalogue-code strings; such values are data. Regex rule, case-insensitive, word-bounded. | The alternation (shipped `acme`, `globex`, `sku-NNNN`) |

### traceability.yml

| Id | Enforces | Set |
|---|---|---|
| `kit.configurable.test-must-carry-marker` | Every `test_*` function (sync or async) under `**/tests/**` carries `@pytest.mark.requirement(...)`. | marker name (also register it under pytest `markers`); exempt folders |

### Import contracts: `configurable/importlinter.confinement.example.toml`

Not run by default. Copy the contracts you need into the contracts file you pass with `IMPORTLINTER_CONFIG`, replacing the example modules.

| Id | Type | Enforces |
|---|---|---|
| `core-pure` | forbidden | The deterministic core imports no outer layer and no side-effecting library. |
| `vendor-sdk-confined` | forbidden, indirect allowed | The inner layers never import the vendor SDK; only the wrapping package does. Lives only in `configurable/importlinter.confinement.example.toml`; copy it into your contracts file before naming it in `IMPORTLINTER_COVERAGE_ARGS`. |
| `numeric-library-confined` | protected | A library is imported by one module only (`allowed_importers`), so a new caller is a finding. |
| `numeric-module-confined` | protected | A module is imported by approved callers only. |

Notes: `source_modules` of a forbidden contract must all exist in the graph or the run aborts; external forbidden modules need `include_external_packages = true`, and a forbidden module absent from the graph is silently ignored. Pair a hand-written `source_modules` list with the `import-linter-coverage` step.
