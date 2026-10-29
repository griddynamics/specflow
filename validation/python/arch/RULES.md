# archkit rule catalogue

Every check ships in one `archkit/checks/test_<section>.py` module and reads its own
`archkit.toml` section. Every check section is required: `archkit run` stops with exit 2
when one is missing, when a required key is not set, or when the file holds a section or
key the kit does not read, so no check is ever skipped. All inputs come from the
configuration; `examples/shop/archkit.toml` sets every key. See [README.md](README.md) for
adoption and commands.

| Section | Module | Tests |
|---|---|---|
| `error_codes` | `test_error_codes.py` | 2 |
| `error_exposure` | `test_error_exposure.py` | 3 |
| `request_id` | `test_request_id.py` | 5 |
| `route_contract` | `test_route_contract.py` | 6 |
| `openapi_snapshot` | `test_openapi_snapshot.py` | 1 |

Common to the HTTP checks: they read the app's own OpenAPI document and send real ASGI
requests through `httpx` (no server, no lifespan). Probe requests carry `[http]
probe_headers` (none by default).

## HTTP contract

### error_codes
Enforces: every handled error answers a documented `(code, status)` pair; every documented pair
is reachable; every registered exception handler has a probe.
Why: clients branch on codes, so code and catalogue must not drift; the handled classes come
from `app.exception_handlers`, so a new handler without a probe fails instead of escaping.
Options: `codes` or `codes_file`, `probes` (`{class, args, kwargs, name}`), `ignore_handlers`.
Checks: `test_every_handled_error_answers_a_documented_code`, `test_every_handled_exception_class_has_a_probe`.

### error_exposure
Enforces: an unknown route, a wrong method and an unhandled exception answer the error
envelope with no traceback, file path or raw exception message.
Why: internals in an error body help an attacker. Only the envelope's shape is asserted (codes belong to `error_codes`).
Options: `unknown_path`, `method_not_allowed` (`"METHOD /path"`, required), `leak_markers`; `[http] envelope_code`, `envelope_message`.

### request_id
Enforces: the request-id middleware is outermost; the header is present and non-empty on a 2xx,
404, 405, 401 and an unhandled-exception 500.
Why: a header missing from the 500 means the middleware sits inside error handling, so the hardest case cannot be traced.
Options: `header` (default `X-Request-Id`), `ok_path` (required), `unknown_path`, `method_not_allowed` (required), `unauthenticated_path` (required).

### route_contract
Enforces: typed JSON (or another declared media type) on every 2xx; no body on a 204; 201 on
every create; every public operation exists; no protected operation succeeds without
credentials; no public operation demands them.
Options: `public_operations` (all others are protected), `create_operation_prefix` (default `create_`), `request_headers`.
Checks: six tests, one per property.

### openapi_snapshot
Enforces: the committed OpenAPI snapshot equals the running app's document (parsed JSON compared with `==`; a mismatch prints a unified diff).
Why: an API change must be a reviewed diff in the same commit.
Options: `path`, `regenerate` (command shown on failure). Write it with `archkit export-openapi`.

## Shared machinery

| Item | Role |
|---|---|
| `archkit_config`, `archkit_app_factory`, `archkit_app`, `archkit_openapi`, `archkit_client`, `archkit_boom` | Fixtures from `archkit/plugin.py`; the boom route is added outside the OpenAPI document at `[http] boom_path`. |
| `_archkit_hermetic` (autouse) | Settings get no `env_file`, `[archkit.hermetic] env` is exported, cached getters are cleared around each check. |
| `iter_operations`, `fill_path` | OpenAPI walk and path-template filling (`[http] path_params`, `default_path_param`). |
| `SHARED_SECTIONS`, `CHECK_SECTIONS`, `REQUIRED_KEYS` | The tables and keys `archkit/config.py` accepts and requires. |
| `archkit/checks/pytest.ini` | Strict pytest settings: strict markers and config, `xfail_strict`, warnings as errors, asyncio auto. |
