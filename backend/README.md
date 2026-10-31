## Development

Create or update the repository-local virtual environment:

```shell
make setup
```

Run the offline unit tests:

```shell
make unit-test
```

## The service (`src/`)

`src/` holds the deployable service (the top-level packages below): one image,
three roles. It also holds the libraries the service reuses (`llm`, `file_parsers`,
`logger`; `file_parsers` for PDF parsing, `logger` for logging).

| Module | What it holds |
|---|---|
| `settings.py` | Typed settings per role, `load_settings(role)`, all startup guards, `ConfigurationError` |
| `cli.py` | The `specflow` entry point |
| `api/` | FastAPI app (`/api/v1`, `/healthz`, `/readyz`), streaming upload, workflow starts, error envelope |
| `auth/` | Token claims, OIDC verifier with a throttled JWKS cache, dev-token verifier |
| `db/` | Models, repositories, async engine, `agent_scope` (RLS session), `migrate` |
| `storage/` | `ObjectStore` protocol, GCS adapter (create-only resumable writes), in-memory store (tests only) |
| `graph/` | The graph library (in progress, `docs/graph/`); the host uses `graph.core` for the Neo4j driver and readiness |
| `temporal/` | Client, worker runner, queues, ids, registry; `documents/` (parse workflow) and `agents/` (summarize workflow, tools, activities) |
| `agent_runtime/` | Summarizer agent, LLM gateway model provider, Temporal plugin, Langfuse tracing |
| `health.py` | The checks behind `/readyz` and `worker --check` (database, temporal, storage, neo4j) |

The PDF entry point of the service, `file_parsers.pdf.to_markdown`
(`pdf_file_to_markdown`, `PdfParseError`, no LLM), lives in the `file_parsers`
library.

### Roles (CLI)

```shell
uv run --env-file ../.env.local specflow api                  # uvicorn on 0.0.0.0:$API_PORT
uv run --env-file ../.env.local specflow worker               # polls WORKER_QUEUES (default parse,agents)
uv run --env-file ../.env.local specflow worker --queue parse # one queue only (repeatable)
uv run --env-file ../.env.local specflow migrate              # alembic upgrade head; a second run is a no-op
uv run --env-file ../.env.local specflow mcp                  # MCP endpoint on 0.0.0.0:$MCP_PORT (default 8002)
uv run --env-file ../.env.local specflow mcp --check-config   # settings and guards only: exit 0, or 2
uv run --env-file ../.env.local specflow api --check-config   # settings and guards only, no network: exit 0, or 2
uv run --env-file ../.env.local specflow worker --check [--queue agents]   # live probes: exit 0 or 1
```

`make api` and `make worker` do the first two. A bad configuration exits with
code 2 after one ERROR log line `config.validation.failed` that names the
variables, never their values.

### Settings and guards

Each role requires only its own variables (`api`, `worker`, `migrate`, `mcp`); a worker
that polls `agents` also needs Neo4j, the LLM gateway and the agent database password.
`mcp` needs `MCP_RESOURCE_URL` (https outside local) plus the database, Neo4j and auth
variables, never Temporal, storage or the LLM gateway.
Outside `APP_ENV=local` startup is refused for: `AUTH_MODE=dev`, a dev signing
key, a non-https issuer, a JWKS URL on another host, a `STORAGE_PREFIX` that does
not end in `/` or starts with `dev/local/`, an empty secret, or `specflow-smoke`
in `AUTH_API_CLIENT_IDS`. Always: the worker refuses `DATABASE_AGENT_USER` equal to
`DATABASE_USER`; `AUTH_MODE=dev` needs a signing key of 32 bytes or more and is
refused when `KUBERNETES_SERVICE_HOST` is set; `AUTH_MODE=oidc` with a dev key is
refused. Variable names and meaning: `.env.example`; the full table with roles and
secret flags: the deployment environment documentation.

### Tests

```shell
make check                      # root or backend/: ruff + the offline suite (tier 1)
uv run pytest tests/test_specflow_cli.py -q    # one file
```

Tier 1 is offline and always on. Live tests (tier 2) are skipped unless a gate
variable is `1`, and need the stack and `--env-file ../.env.local`:

| Gate | Needs |
|---|---|
| `SPECFLOW_TEST_POSTGRES=1` | Postgres up, agent role created (scratch databases, a per-run test role) |
| `SPECFLOW_TEST_GCS=1` | bucket and ADC; objects only under `dev/local/pytest/<run-id>/`, deleted in teardown |
| `SPECFLOW_TEST_NEO4J=1` | Neo4j up |
| `SPECFLOW_TEST_TEMPORAL=1` | the external Temporal stack, namespace `specflow-local` |
| `SPECFLOW_TEST_LLM_GATEWAY=1` | spends real model calls (a few per run) |
| `SPECFLOW_TEST_IMAGES=1` | built images (`make images` from the root runs these) |
| `SPECFLOW_IMAGE_RSS=1` | opt-in memory test, run inside the `test` image |

Run one live test, for example the project-isolation canary:

```shell
SPECFLOW_TEST_POSTGRES=1 uv run --env-file ../.env.local pytest tests/test_specflow_canary.py -q
```

The `agents` live tests that call the model are deliberately few; do not loop
them. Live tests never use Temporal's test servers (`start_local`,
`start_time_skipping`): a test scans for them. Replay fixtures for the workflows
are in `tests/fixtures/temporal/`.

### Rules for contributors

- Log only through `logger.logger` (`get_logger`, `log_event`, events named
  `area.object.verb`); no `import logging`, no `print`, no field names such as
  `token`, `secret`, `password`, `api_key`.
- Agent tools take `project_id` and `document_id` from the run context, never from
  the model; agent database sessions are built only through `agent_scope`.
- Temporal workflows schedule activities by name and import project modules only
  inside `workflow.unsafe.imports_passed_through()`. Activity errors carry fixed
  codes, never DB or exception text.
- New environment variables go into `.env.example`, `docker-compose.yml` (the services
  that read them) and the deployment environment documentation.
- Use package-qualified absolute imports (`from db.session import ...`).

## Settings and permissions

`AgentSettings` exposes stable SDK controls while keeping SDK option
construction internal. Tool grants are explicit and unconstrained `Read`,
`Write`, `Edit`, and `Bash` grants fail unless the caller deliberately opts in:

```python
from llm import AgentPermissions, AgentSettings

settings = AgentSettings(
    permissions=AgentPermissions(
        allowed_tools=(
            "Read(//workspace/**)",
            "Edit(//workspace/**)",
            "Bash(git status:*)",
        ),
        permission_mode="acceptEdits",
    ),
    max_turns=20,
    setting_sources=("project",),
)
```

Settings also support fallback models, buffer and budget limits, subagents,
MCP servers, hooks, sandbox configuration, extra environment values, stderr
callbacks, additional directories, and streamed event controls. Subagents
inherit the parent model by default; model mismatches and unconstrained
subagent tools require their own explicit opt-ins.

## MCP server

The `mcp` role serves the read-only project tools to coding agents such as Claude Code over MCP
(streamable HTTP, path `/mcp`). Design, auth and operations: [MCP server](../docs/mcp-server/INDEX.md).
Its settings are in [configuration](../docs/configuration.md).

Local run, from the repository root:

```shell
make up                  # mcp in dev mode on http://localhost:8002/mcp, with the dev-login users
make up WITH=keycloak    # also the local Keycloak; only mcp switches to oidc against it
```

Probes: `GET /healthz` (liveness) and `GET /readyz` (database and Neo4j) on port 8002. Clients use
`localhost`, not `127.0.0.1`: on `/mcp` another `Host` answers `421`.

Claude Code, with a dev token (dev mode) or the Keycloak sign-in: see
[Local endpoint](../docs/mcp-server/deployment.md#local-endpoint).

QA commands, from `backend/` with the stack up (a token is printed alone on its line, nothing else is):

```shell
uv run --env-file ../.env.local python examples/mcp_qa.py seed [--mode keycloak|dev]
uv run --env-file ../.env.local python examples/mcp_qa.py token alice [--client mcp|web]
uv run --env-file ../.env.local python examples/mcp_qa.py dev-token alice@example.com [--client mcp|web] [--headers]
```

The live tiers `SPECFLOW_TEST_MCP_E2E` and `SPECFLOW_TEST_MCP_CLAUDE` are described in
[testing](../docs/mcp-server/testing.md#tiers).

## Native OS sandbox

Every `Agent` and `CodingAgent` uses the Claude SDK's native Bash sandbox by
default. The package preflights Apple Seatbelt through `/usr/bin/sandbox-exec`
on macOS, or `bwrap` plus `socat` on Linux, before opening an SDK session.
Missing dependencies and unsupported platforms raise `SandboxUnavailableError`
instead of silently running unconfined.

The default `NativeSandboxPolicy` denies unsandboxed-command escape, excludes
only Docker, and limits sandboxed network access to common package registries
and GitHub hosts. Callers can replace the allowlist or grant explicit additional
write roots:

```python
from pathlib import Path

from llm import AgentSettings, NativeSandboxPolicy

settings = AgentSettings(
    sandbox=NativeSandboxPolicy(
        allowed_domains=("pypi.org", "files.pythonhosted.org"),
        additional_write_roots=(Path("/opt/shared-build-cache"),),
    )
)
```

Custom SDK sandbox settings remain supported, but `enabled=False` and
`allowUnsandboxedCommands=True` are rejected. `AgentSettings(sandbox=False)` is
the only unconfined opt-out and should be used only when another trusted
isolation boundary exists. On Debian or Ubuntu install Linux prerequisites with
`sudo apt-get install bubblewrap socat`; Ubuntu 24.04+ may also need an AppArmor
profile allowing unprivileged user namespaces for `bwrap`.

## Langfuse tracing

Langfuse is a core package dependency. Configure the client through
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_BASE_URL`.
`LangfuseMessageObserver` attaches through `Agent.message_observer` and isolates
all tracing failures from agent execution. `LangfuseTraceRegistry` creates named
observers for one application run. Pass `session_id` so Langfuse Sessions group
every observer in that run; Claude's SDK session id is kept as
`sdk_session_id` metadata. Each `Agent.run` closes its observer; call
`close_and_flush()` once after every observer exists. `evidence()` snapshots
observer health without flushing so later steps can judge the run so far.
`require_authenticated_langfuse_client()` is the fail-closed counterpart to
`LangfuseMessageObserver.from_default_client()` when tracing must be present:

```python
from llm import Agent
from llm.tracing import (
    LangfuseTraceRegistry,
    require_authenticated_langfuse_client,
)

traces = LangfuseTraceRegistry(
    require_authenticated_langfuse_client(),
    model="...",
    session_id="...",
    metadata={"run_id": "..."},
)
agent = Agent(
    model="...",
    message_observer=traces.observer("app.agent"),
)
result = await agent.run("Describe the requested change.")
traces.close_and_flush()
```

The tracing package is layered: `TraceSession` and `TraceSink` define the
provider-neutral lifecycle, `ClaudeTraceObserver` translates Claude Agent SDK
messages, and `LangfuseSink` handles only Langfuse observations.

By default only lifecycle metadata, session ID, tool names, result status,
timing, cost, and token usage are sent. Prompt and system-prompt bodies,
assistant output, reasoning, SDK system-message data, and tool inputs or outputs
each require an explicit `LangfuseCapture` opt-in. Credentials are never
collected automatically. Caller-provided `metadata` is sent as supplied and
must already be safe. A result message closes the trace automatically; call
`Agent` also closes observers that expose the optional lifecycle method after
transport failures, timeouts, cancellation, and incomplete streams.
`LangfuseMessageObserver.from_default_client()` stays the fail-open helper when
credentials are missing.

## Logging

Every process bootstraps one central logging pipeline (`src/logger/`, see
the `logger.logger` module docstrings for the API and adoption rules). Records go to stdout only, in every environment. Each record
is one JSON object on one line. The logger writes no files. The platform
(Cloud Logging, Grafana) collects stdout and owns retention. Each record has a
`severity` key. Full LLM payloads are not logged; they live only in Langfuse.

| Env var | Default | Meaning |
|---|---|---|
| `LOGS_LEVEL` | `INFO` | Level for the top-level name of any non-third-party logger obtained through `get_logger` |
| `LOGS_STDOUT_LEVEL` | `INFO` | Threshold for the stdout handler |
| `LOGS_RAISE_EXCEPTIONS` | off | Development switch (`1`/`true`/`yes`/`on`): a failing handler or malformed record dumps loudly to stderr instead of the silent, rate-limited deployed behaviour. The test suite turns it on |

The old log-directory, retention and full-payload variables are removed. The
logger ignores any other `LOGS_*` variable if it is set.

To read the logs as text, pipe them through `jq`. The first recipe prints one
line per record and passes every non-JSON line through unchanged. The second
recipe selects one run.

```shell
uv run … | jq -R -r '. as $l | (fromjson? | objects | "\(.asctime) \(.severity) \(.name) \(.message)") // $l'
uv run … | jq -R 'fromjson? | objects | select(.run_id=="<id>")'
docker compose -p specflow logs -f --no-log-prefix <service> | jq -R -r '. as $l | (fromjson? | objects | "\(.asctime) \(.severity) \(.name) \(.message)") // $l'
```

`docker compose logs` adds a `<service> | ` prefix to each line. Use
`--no-log-prefix` so `jq` can parse the JSON.

These lines stay non-JSON by design:

| Source | Stream |
|---|---|
| `[central-logger] …` self-diagnostics | stderr |
| Temporal Rust core logs (for example `WARN temporalio_sdk_core::…`) | process console |
| Uncaught exceptions, `warnings.warn`, faulthandler output | stderr |
| stdlib last-resort output after `close()` | stderr |

Follow-up F1 (not done yet): forward the Temporal core logs into the central
logger so they become JSON too.

A `trace.start` record's `trace_url` field is built from `run_id` (which
doubles as the Langfuse trace id) whenever `LANGFUSE_BASE_URL`/`LANGFUSE_HOST`
is set — it is absent from other trace events, and since it's constructed
rather than confirmed against Langfuse, it can point to a trace that was
never actually created (e.g. no Langfuse client attached). `make check` runs
the full lint + test gate, including the logging adoption tests.

## Structured judging

`llm.judge.run_structured_judge` sends caller-owned criteria and untrusted
evidence to an existing `Agent` and returns native structured output. It does
not introduce a second agent type. Independent judges typically pass
`AgentPermissions()` so the session cannot mutate the workspace under review.
`llm.evidence.run_command` and `snapshot_text_files` collect the command output
and bounded file snapshots that payload can include.

## Postgres

The service (`src/db/`) owns the schema: models `User`,
`Project`, `ProjectMember`, `Document` (`models.py`), an async asyncpg engine
and session factory (`session.py`), repositories (`repositories.py`) and the
RLS-scoped agent session (`agent_scope.py`). Alembic (`alembic.ini`,
`migrations/`) has one hand-written revision, `0001_initial`: every table in its
final shape (users, projects, project_members, connections, documents,
connection_items), row-level security for the agent role on every project-scoped
table and its column-level grant on `documents`. `DatabaseSettings` (in
`src/settings.py`) reads `DATABASE_*`.

While the application is in development there are no incremental migrations and no
backfills: on a schema change, edit the models and `migrations/versions/0001_initial.py`
together, then recreate the database from scratch with `make down-volumes` and
`make up` (details in `migrations/README`). A database stamped with an older revision
(such as `0005`) fails `alembic upgrade` and is recreated the same way.
The `postgres` service of the root `docker-compose.yml` runs `postgres:17.11`
with `backend/postgres-init/` mounted as its init folder. On its first start
(empty volume) the script `10-specflow-roles.sh` creates the app role `specflow` (owner of the database, not a
superuser), the database `specflow` and the agent role. The `api` container runs
`specflow migrate` every time it starts.

**Set the passwords before the first `make up`.** Run `make env-local` (repository
root): it writes `.env.local` with random `POSTGRES_ADMIN_PASSWORD`, `DATABASE_PASSWORD` and
`DATABASE_AGENT_PASSWORD`. There is no default: a blank password stops the stack,
and the backend rejects it too. `make up` starts no Temporal and no Langfuse; it uses
your own (`TEMPORAL_ADDRESS`), or add them with `make up WITH=temporal,langfuse`
(see `docs/configuration.md`). The data volume keeps the credentials it was first
initialised with, so changing `.env.local` later does not change the database roles.

```shell
make up                        # repository root; postgres on 127.0.0.1:5433 (DATABASE_PORT)
make db-upgrade                # alembic upgrade head from the host (root or backend/)
cd backend && uv run --env-file ../.env.local alembic current   # 0001_initial (head)
cd backend && uv run --env-file ../.env.local alembic check     # the models match the revision
make down-volumes && make up   # after editing 0001_initial: recreate the database from scratch
```

`make db-upgrade` prints nothing on success, because Alembic's log records
only reach stderr when they are warnings or errors. Check the result with
`alembic current` (it writes to stdout). Revision `0001_initial` refuses to run when the
agent role (`DATABASE_AGENT_USER`) does not exist, or has SUPERUSER or BYPASSRLS
(offline, `--sql`, the check is skipped with a comment). `make db-revision` is not used
while the schema is one revision.

`DATABASE_PORT` is the host port (5433 in `.env.example`); the container
listens on 5432 and is published on `127.0.0.1` only. Alembic reads the same
`DATABASE_*` variables through `DatabaseSettings`, so `alembic.ini` holds no URL
or password.

```python
from db.session import build_engine, build_session_factory
from settings import DatabaseSettings


async def main() -> None:
    engine = build_engine(DatabaseSettings.from_environment())  # lazy
    sessions = build_session_factory(engine)
    try:
        async with sessions() as session:
            ...
    finally:
        await engine.dispose()  # the caller owns the engine
```
