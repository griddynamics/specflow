# archkit: architecture, security and parity checks

A configurable pytest suite that checks a Python web service (FastAPI) against its own
documented HTTP contract: the error-code catalogue, the error envelope, the request-id
header, the route contract (typed responses, creates, authentication) and the committed
OpenAPI snapshot.

The kit holds no project facts. Every module path, operation and catalogue comes from the
adopter's `archkit.toml`. Every check section is required, and a section or key the kit
does not read is a configuration error (`archkit: ...`, exit 2), so no check is ever
silently skipped. The kit tests itself against a neutral example service under
`examples/shop`.

The per-check catalogue (what each enforces, why, options, bad/good examples) is in
[RULES.md](RULES.md).

## Layout

| Path | Role |
|---|---|
| `archkit/checks/test_*.py` | The checks, one module per configuration section. |
| `archkit/checks/pytest.ini` | The strict pytest settings `archkit run` always uses. |
| `archkit/plugin.py` | Loads the configuration and serves the `archkit_*` fixtures. |
| `archkit/config.py` | Configuration loading and validation (known sections and keys, required checks), `pkg.mod:attr` references, catalogue files. |
| `archkit/support.py` | Shared helpers: section lookup, OpenAPI walk, path filling, envelope reads. |
| `archkit/analysis.py` | Pure analysis the checks share: handler coverage, snapshot diff. |
| `archkit/markdown.py` | Markdown tables (the catalogue build step). |
| `archkit/cli.py` | The `archkit` command. |
| `examples/shop/` | The neutral example service (users/orders) and its complete `archkit.toml`. |
| `tests/` | The kit's self-tests, plus OpenAPI snapshot-diff fixtures. |
| `pyproject.toml` | The kit's manifest: runtime dependencies, example app and tool pins, ruff, mypy, pytest. |

## Requirements

Python 3.12 or newer, and `uv` (or pip).

Runtime dependencies (minimums, from `pyproject.toml`): pytest 9.1.1, pytest-asyncio 1.4.0,
httpx 0.28.1, pyyaml 6.0.3, markdown-it-py 4.2.0.

The `dev` group pins the example service and the kit's own tooling exactly: fastapi 0.141.1,
starlette 1.6.0, pydantic-settings 2.15.0, anyio 4.15.1, ruff 0.16.7, mypy 2.3.1 (with the
`pydantic.mypy` plugin), types-pyyaml. The build backend is hatchling 1.32.4.

The checks import the adopter's app, so the kit must be installed in the environment that
runs the project's code.

## Adopt in a project

1. Copy this folder into the project (for example to `validation/python/arch`).
2. Install it into the environment that runs the project's code:
   `uv add --dev --editable validation/python/arch` (or `pip install -e validation/python/arch`).
3. Copy `examples/shop/archkit.toml` to the project root as `archkit.toml` and replace every
   value. Every check section is required; a section or key the kit does not read is an
   error. Keys to set first:
   - `[archkit]`: `project_root`, `python_path` (import roots), `app_factory`
     (`pkg.mod:create_app`, zero arguments, returns a FastAPI app), `filterwarnings`.
   - `[archkit.hermetic]`: `settings_class`, `cache_clear` (cached settings getters), `env`
     (variables a check needs to build the app). Checks never read a developer `.env`.
   - `[http]`: `path_params`, `default_path_param`, `boom_path`, `envelope_code`,
     `envelope_message`, `probe_headers`.
   - One table per check (see [RULES.md](RULES.md)). Every key is set in
     `examples/shop/archkit.toml`, and each check module's docstring documents its keys.
4. Build the error-code catalogue from the project's own document (next section) and point
   `[error_codes] codes_file` at it, or list the codes inline.
5. Run `uv run archkit run --config <project-root>/archkit.toml`. Extra arguments go to
   pytest.

### Build the configurable inputs

A check that compares code with documented facts reads them from the configuration, inline
or from a `<key>_file` (JSON, YAML or TOML). Build the file from the project's own
document in the same commit as the document changes:

```sh
uv run archkit build-catalogue --doc <project-root>/docs/api.md --heading "Error codes" \
  --column code=Code --column http=HTTP --int http --out <project-root>/build/error-codes.json
```

Then set `codes_file = "build/error-codes.json"` under `[error_codes]`. `--int KEY` converts
a column to an integer, `--list KEY` splits it on commas.

Write the OpenAPI snapshot once, then commit it:

```sh
uv run archkit export-openapi --config <project-root>/archkit.toml
```

Probes (`[error_codes] probes`) name the project's own exception classes as `pkg.mod:attr`;
list one per handled exception.

## Commands

| Command | What it does |
|---|---|
| `uv sync` | Installs the kit and its pinned tooling from this folder. |
| `uv run archkit run --config <project-root>/archkit.toml` | Runs every check with the kit's own pytest settings; exits 2 on a configuration error. |
| `uv run archkit run --config <project-root>/archkit.toml -k request_id -x` | Same, with pytest arguments (select by keyword, stop at first failure). |
| `uv run archkit export-openapi --config <project-root>/archkit.toml` | Writes `[openapi_snapshot] path` from the live app. |
| `uv run archkit build-catalogue --doc <project-root>/docs/api.md --heading "Error codes" --column code=Code --column http=HTTP --int http --out <project-root>/build/error-codes.json` | Turns a Markdown table into a JSON catalogue. |

The configuration path also comes from `$ARCHKIT_CONFIG`. `archkit run` passes its own
`pytest.ini` (`--strict-markers --strict-config`, `xfail_strict`, `filterwarnings = error`,
`asyncio_mode = auto`), so the adopter's pytest configuration never changes how the checks
behave. Add warning filters for third-party noise with `[archkit] filterwarnings`.

## Wire it into CI and pre-commit

CI, after the project's dependencies and this kit are installed:

```sh
uv run archkit run --config archkit.toml
```

A non-zero exit fails the job. Regenerate the snapshot and catalogue first in the same job
(or commit them) so the checks compare current files.

Pre-commit, as a local hook that runs when code or documents change:

```yaml
- repo: local
  hooks:
    - id: archkit
      name: archkit
      entry: uv run archkit run --config archkit.toml
      language: system
      pass_filenames: false
      files: ^(src/|docs/|archkit\.toml)
```

## Self-test

From this folder:

```sh
uv sync
uv run pytest
uv run archkit export-openapi --config examples/shop/archkit.toml
uv run archkit run --config examples/shop/archkit.toml
```

`tests/test_example.py` exports the example's OpenAPI snapshot into a temporary folder, runs
every check against `examples/shop` (all must pass, none may skip), breaks three settings
and requires the matching checks to fail, and requires a retired section, an unknown
section or a missing required key to stop the run with exit 2. The other self-tests cover
configuration loading and validation, Markdown tables, and the analysis functions (handler
coverage, snapshot diff).

The kit's own code is linted and type-checked with `uv run ruff check archkit tests` and
`uv run mypy` (ruff rule families E, F, W, I, UP, B, SIM, RUF, PT, S; mypy strict).
