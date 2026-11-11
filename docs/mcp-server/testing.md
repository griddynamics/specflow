---
type: Design
title: "MCP server testing"
description: "Test tiers, QA fixtures including the temp workspace, the end-to-end scenario matrix, the stack gate, and scripted QA with Claude Code and the MCP Python SDK against a real OIDC provider"
status: stable
tags: [mcp, testing]
---
# testing.md: test tiers, fixtures, scenario matrix, stack gate and end-to-end QA with a real client

## Tiers

| Tier | Gate | Covers |
|---|---|---|
| Unit | always (`make check`) | settings and guards of both roles; claim profile branches; cursor codecs; PRM document; error mapping and sanitizing; result shaping; the membership structural test; the binding file schema, with its negative cases; the server instructions (resource URL, file name, under 2,048 characters, no project data) and the `select_project` text with and without the argument, the argument kept as quoted data |
| Settings | always (`make check`) | role loading (the api role loads the MCP settings only with `MCP_RESOURCE_URL`), the `API_INTERNAL_URL` forms and refusals, env parity between `.env.example`, compose and the settings (`test_specflow_settings_env.py`) |
| Golden gate | always (`make check`) | the agent-visible surface stays identical across the split: `tools/list`, prompts, instructions, PRM, probes, error results, success payloads and fixture cursors issued before the split; the golden files are frozen |
| API agent routes | always (`make check`, fakes); live tier with `SPECFLOW_TEST_POSTGRES=1`, `SPECFLOW_TEST_NEO4J=1` | every route of [architecture.md](architecture.md#api-agent-routes), every route and common error, the dependency chain, `X-SpecFlow-User-Id` on success and on error, `X-Request-ID` adoption, and every behaviour ported from the former tool handlers |
| Audience matrix | always (`make check`) | the matrix of [auth.md](auth.md#token-validation) in both audience modes (FR-MCP-AUTH-13) |
| MCP router | always (`make check`) | the role over an `httpx.MockTransport` fake of the API: tool to route and query mapping, `Authorization` and `X-Request-ID` forwarding, the `from_api` table of [architecture.md](architecture.md#errors), the 25 s timeout, the audit fields, and S2 with `httpx` and `httpcore` logging at DEBUG |
| HTTP boundary | always (`make check`) | the role's ASGI app over `httpx.ASGITransport` (the SDK client session over `httpx2.ASGITransport`) with the loopback JWKS stub of `specflow_auth_support` and a fake API: the full auth matrix, PRM, Host and Origin rules, body cap, 404 body, every tool through an MCP client session over HTTP, REST rejecting MCP tokens; `initialize` instructions; `prompts/list` and `prompts/get`, and 401 for both without a token; tool input schemas unchanged except descriptions |
| Live data | `SPECFLOW_TEST_POSTGRES=1`, `SPECFLOW_TEST_NEO4J=1` | repository reuse against Postgres; `count_nodes` and `nodes_page` against Neo4j, including an ontology whose `feature` label and prefix are renamed (the graph library's renamed-ontology fixture also renames type keys, so it is not used unchanged: [../graph/ontology.md](../graph/ontology.md#ontology-files-of-submodules)) |
| Role imports | always (`make check`, no Docker) | each role's entrypoint imports with only its own dependency extras installed ([architecture.md](architecture.md#import-rules), M2) |
| Images and chart | Docker-gated image tests; `helm lint`, `helm template`, chart unit tests | per image (api, worker-agents, worker-parse, mcp): a smoke start and the absence of the other roles' packages; the chart renders the four images |
| Stack gate | the local stack, at the end of every change to the split | [Stack gate](#stack-gate) |
| End to end | `SPECFLOW_TEST_MCP_E2E=1`, local stack (`make up`; Keycloak is always part of it), then the QA seed | the real role, real Keycloak, the MCP Python SDK client running the OAuth flow; the scenario matrix below; every project-scoped scenario reads its project id from a binding file ([Temp workspace](#temp-workspace)) |
| Agent QA | `SPECFLOW_TEST_MCP_CLAUDE=1` and a signed-in `claude` CLI | Claude Code signs in, binds a temp repository to a project and answers developer prompts with the tools; assertions on tool events and the binding file; gate: 3 runs, all passing |

The end-to-end and agent tiers are not part of CI: they need the local stack and, for the agent tier, model calls.
Import contracts, query placement, the dependency check, the archkit rules, the OpenAPI snapshot and the licence gate
are checked by `make validate`, not by the test tiers.

Test data of the agent-route and router tiers, beyond the scenarios below:

| Kind | Cases |
|---|---|
| Happy | one call per tool for alice on `P-Alpha`; `list_tasks` with its defaults; `get_task_brief` with `neighbours` 30 |
| Edge | `limit` 1 and 100; cursors of 512 and 513 characters; non-canonical cursors (padding, key order, an extra key, `v` 2); `P-Empty`; `neighbours` 0 and 100; a cursor naming a vanished task (the list answers, the path answers `invalid_cursor`) |
| Error | `project_id` `x/../y` and `""`; a foreign project (bob on `P-Alpha`); no plan; an unknown task or feature; Neo4j down; Postgres down (also during provisioning: `database_unavailable`); the API down; a `2xx` without the user-id header |
| Security | the audience matrix; two `Authorization` headers on `/mcp`; a caller's `X-Request-ID` not adopted by MCP; a caller's `X-SpecFlow-User-Id` not forwarded; no token, claim or malformed id in the logs with `httpx` and `httpcore` at DEBUG |
| Performance | a 10 s run of E14 (`mcp_qa.py check --latency-seconds 10`) on `P-Large`: p95 at most 50 ms above the p95 measured before the split |

The harness is Python only. The QA library and its manual entry live in `backend/examples` (`mcp_qa_lib.py`,
`mcp_qa.py`, run with `uv run --env-file ../.env.local python examples/mcp_qa.py`); the end-to-end and agent tiers are
tests in `backend/tests`.

## Fixtures

One idempotent QA seed prepares the SpecFlow side (Postgres and Neo4j); Keycloak needs no seed, because the realm import creates its users ([Local stack](deployment.md#local-stack)). The seed has no mode and makes no Keycloak call. It writes graph data through `graph.core.OwnedWriter` as the build
owner (the path the graph live tests use, [../graph/api.md](../graph/api.md#owned-writes)), never with ad hoc Cypher.
`P-Alpha`, `P-Beta`, `P-Empty` and `P-Large` are project names; the seed writes each project's graph under its
Postgres id (`group_id` = `str(project.id)`), and the scenarios pass that id.

| Store | Data |
|---|---|
| Keycloak (from the realm import, not the seed) | `alice@example.com`, `bob@example.com` (verified); `carol@other.example` (verified, domain not allowed); fixed user ids; `KEYCLOAK_QA_PASSWORD` from `.env.local` as the password; the `specflow-web` client secret `KEYCLOAK_WEB_CLIENT_SECRET` from `.env.local` |
| Postgres | users whose `auth_subject` is the fixed Keycloak user id of the QA user (the same constants as in the realm file); `P-Alpha`, `P-Empty` and `P-Large` owned by alice; `P-Beta` owned by bob |
| Neo4j | `P-Alpha`: 7 features, 2 modules, 3 functional requirements; `P-Beta`: 3 features; `P-Empty`: nothing; `P-Large`: 300 features |

## Temp workspace

The end-to-end and agent tiers use a temp workspace: a fresh folder under the system temp directory (a `git init`
repository in the agent tier), deleted at teardown. The QA library's workspace helper creates it, finds the nearest
`.specflow/config.json` from a working directory up, writes or replaces the whole file, reads it, and validates it
against the [schema](../../backend/contracts/schemas/specflow-workspace-config.schema.json). The MCP Python SDK client
does not read files by itself, so the harness plays the agent's part: it writes the binding file and passes the
`project_id` it reads back.

Rule: every project-scoped end-to-end scenario reads its project id from a binding file the harness wrote in a temp
workspace, never from a constant. The file names the seeded project's Postgres id and the `server` of the stack under
test.

## End-to-end scenarios


| # | Caller | Action | Expected |
|---|---|---|---|
| E1 | no token | `POST /mcp` | 401 with `resource_metadata`; the PRM names the resource URL and the issuer |
| E2 | alice | `list_projects` | `P-Alpha`, `P-Empty`, `P-Large`; never `P-Beta` |
| E3 | alice | `list_features(P-Alpha, limit=5)`, then the next page | `total_count` 7; 5 then 2 items; only `FEAT::` ids; second `next_cursor` null |
| E4 | alice | `list_features(P-Beta)`: a valid id of an existing project alice is not a member of | `project_not_found`; no graph query |
| E5 | alice | `list_features(<random uuid>)` | `project_not_found`, same message as E4 |
| E6 | alice | `list_features(P-Empty)` | `total_count` 0, no items |
| E7 | bob | `list_projects` | `P-Beta` only |
| E8 | alice; between two calls the test deletes her `project_members` row of `P-Alpha` directly in Postgres (no API removes members) | `list_features(P-Alpha)` | second call `project_not_found` |
| E9 | a `specflow-web` token (REST audience) | `POST /mcp` | 401 |
| E10 | carol | `POST /mcp` | 403 `domain_not_allowed`, no challenge |
| E11 | alice | `list_features` with a forged cursor | `invalid_cursor` |
| E12 | alice | `POST /mcp` with a foreign `Host` | refused by transport security |
| E13 | alice | signs in at Keycloak as the `specflow-web` client (the web token) and as `specflow-mcp` (the MCP token); the two tokens carry the same `sub`; creates a project through the REST API with the web token, then calls `list_projects` with the MCP token | the `sub` is alice's fixed Keycloak id in both tokens; the project is listed; the web token on `/mcp` gets 401 |
| E14 | alice | `list_features(P-Large)` at the load NFR-MCP-01 names, for 60 s | p95 within the target of NFR-MCP-01 ([requirements.md](requirements.md#non-functional)), recorded in the run report |
| E15 | alice | `initialize` | the result carries instructions that hold the resource URL and `.specflow/config.json`, are under 2,048 characters and hold no project data |
| E16 | alice; then no token | `prompts/list`, then `prompts/get` of `select_project` with and without `project` | the prompt is listed with an optional argument; the text holds the steps and the exact version 1 JSON; without a token, 401 for both |
| E17 | alice | bind `P-Alpha` in a temp workspace and call `list_features` with the id from the file; replace the file with `P-Empty` and call again; then write a file bound to `P-Beta` and call again | first call: 7 features; second call: `total_count` 0, and the file holds only the `P-Empty` binding; third call: `project_not_found` |

The reverse audience cases (an MCP token on the REST routes, a web token on the agent routes) are tests of the
audience-matrix tier, and checks of the [Stack gate](#stack-gate), not end-to-end scenarios.

## Stack gate

Every change to the MCP split ends green on the local stack, run from the main checkout (a worktree's stack collides
with the main one: ask first). Evidence holds commands, exit codes, counts and timings, never tokens, emails or
secrets; token-carrying calls use `curl -s -o /dev/null -w '%{http_code}'` (`-D -` only to read response headers),
never `-v`, and never echo the token.

| # | Check | Pass |
|---|---|---|
| SG-1 | `make env-local` | exits 0; no value overwritten or printed |
| SG-2 | `make up` | every service healthy within the wait timeout |
| SG-3 | `cd backend && uv run --env-file ../.env.local python examples/mcp_qa.py seed` | exits 0 |
| SG-4 | `mcp_qa.py check --user alice` and `--user bob` | exit 0; tools and counts as before the split |
| SG-5 | a web token (`mcp_qa.py token alice --client web`) on `GET 127.0.0.1:8000/api/v1/projects` | 200 (REST unaffected) |
| SG-6 | the checks of the change below | all pass |

Checks that hold once the split is done:

- An MCP token on `GET /api/v1/agent/projects` answers 200 with `X-SpecFlow-User-Id`; a web token on any agent route
  and an MCP token on `/api/v1/projects` answer 401 or 403; bob on alice's project answers `404 project_not_found`.
- The environment of the `specflow-mcp` container holds no `DATABASE_` or `GRAPH_STORE_` name.
- With the `api` container stopped, `mcp_qa.py check` fails, the `mcp` log shows `mcp.tool.completed` with outcome
  `internal_error`, error type `ApiUnreachable` and a `request_id`, and `127.0.0.1:8002/readyz` answers 503; after the
  API starts again, SG-4 passes.
- The `specflow-mcp` container runs image `specflow-mcp:local` with command `specflow mcp`, user 10001 and no
  `/app/migrations`; every service is healthy on its own image; in each image a package of another role fails to
  import (for example `temporalio` in `specflow-mcp:local`).
- E14 against the run before the split, as the performance row above says.

## Agent QA with Claude Code

The driver acts as a developer at a terminal, in a fresh temp git repository per run (the workspace of
[Temp workspace](#temp-workspace)), outside this repository:

1. Registers a uniquely named server (`specflow-qa-<run id>`) in that folder with
   `claude mcp add --transport http --scope local --client-id specflow-mcp --callback-port 8765 specflow-qa-<run id> http://localhost:8002/mcp`.
2. Runs `claude mcp login <name> --no-browser` in a pseudo-terminal, reads the authorization URL (printed inside an
   OSC 8 hyperlink escape), completes the Keycloak sign-in with the HTTP login helper, and writes the redirect URL back.
3. Runs a workspace flow of four steps, each a fresh session started with `claude -p --output-format stream-json --verbose`, a model and spend guards (`--model`, `--max-turns`, `--max-budget-usd`), no prompts and no saved session (`--permission-mode dontAsk`, `--no-session-persistence`):

| Step | Prompt (as a developer would type it) | Assert on tool events and the file |
|---|---|---|
| 1 Bind | `/mcp__specflow-qa-<run id>__select_project P-Alpha` | the file exists, validates against the schema and names `P-Alpha` (id and name); it is written by a run that called `list_projects`, and is the only write target |
| 2 Reuse | List every feature of this repository's SpecFlow project. | `list_features` called with `P-Alpha`'s id until `next_cursor` is null, or with a limit covering all; the union holds the 7 feature ids and no `REQ::` id; `list_projects` never called |
| 3 Foreign | the harness writes a `P-Beta` binding, then the same prompt as step 2 | `list_features` result is `project_not_found`; no `P-Beta` feature name appears anywhere in the run; no other project's tool call |
| 4 Switch | `/mcp__specflow-qa-<run id>__select_project P-Empty`, then a fresh session with the prompt of step 2 | the whole file is replaced and names `P-Empty` (id and name), written by the `select_project` run, which is the only write; `list_features` called with `P-Empty`'s id, `total_count` 0 |

   The slash form is the Claude Code form of the prompt ([tools.md](tools.md#the-select_project-prompt)). Claude Code
   2.1.316 does not echo the expanded prompt in the stream, so the outcome is proved by the file and the tool events,
   and `slash` means that the typed command is a registered slash command of the session (`init.slash_commands`). If
   the hyphenated server name is not registered, steps 1 and 4 use the plain request "Bind this folder to the SpecFlow
   project P-Alpha" (or `P-Empty`), and the run report records `slash` or `plain`.

   Permissions: writes are allowed only by `Edit(./.specflow/**)` (Claude Code applies Edit rules to the Write tool,
   and scoped `Write(...)` allow rules are denied); `Bash` and the other built-in tools stay disallowed
   (`--disallowedTools`; `Read`, `Write` and `Edit` are not in that list). `--allowedTools` lists the two SpecFlow
   tools and that Edit rule, with no `Read` rule: under `dontAsk` the agent reads files inside the working folder
   without one, and the CLI denies reads elsewhere. The nearest-file lookup walks up from the working directory
   ([tools.md](tools.md#the-binding-file)), so a `Read` of `<an ancestor of the workspace>/.specflow/config.json` is the
   designed lookup: the run check always allows it (the file or its `.specflow` folder), executed or denied.
   The run check is a second layer: it fails on any Read the CLI executed outside the workspace (paths are normalized,
   `..` resolved and symlinks not followed, before they are classified) and on any Write, Edit, MultiEdit or
   NotebookEdit call whose target is not `<workspace>/.specflow/config.json`, denied or not. A Read the CLI denied is
   recorded in the step report as `denied=Read(<kind>: <path>)` (workspace as `<ws>`, home as `~`), not failed, because
   nothing was read. It also fails on any other built-in tool call, on a denied call of the SpecFlow tools or of a write tool (the flow is broken), and
   on a file created or changed anywhere else in the workspace or any symlink in it. A runner or parser error is a
   failed step with its reason, never a crash; the stream's `permission_denied` events (`message` is a string there)
   are recorded, never fatal.
4. Tears down: `claude mcp logout <name>`, `claude mcp remove <name> --scope local` (the CLI stores MCP tokens in
   the user's real Claude configuration) and deletes the workspace.

Assertions read tool inputs and results from the stream and the binding file; the model's prose is recorded, never
asserted. The gate is 3 runs, all passing.

Running the gate: `uv run --env-file ../.env.local python examples/mcp_qa.py claude --runs 3` (exit status 1 when a
step fails), or `SPECFLOW_QA_RUNS=3 SPECFLOW_TEST_MCP_CLAUDE=1 uv run --env-file ../.env.local pytest tests/test_mcp_claude_qa.py -q`
(one test per run; the default is 1 run). Every run registers its own server and workspace. A flow is five prompts
(step 4 is two), or seven when both slash steps fall back to the plain request; each prompt has `--model sonnet`,
`--max-turns 10` and `--max-budget-usd 0.50`, so a run costs at most 7 x 0.50 = 3.50 USD. The plain request is used
only when the init message's `slash_commands` does not list `mcp__<server>__select_project`. `--runs N` stops after a
run whose teardown was not clean. The report records, per step, the outcome, our tool calls, duration, cost and
`slash` or `plain`.

## Login helper

The QA login helper finishes an authorization URL at Keycloak over plain HTTP: it loads the login form, posts the
credentials, and returns the loopback redirect URL (with `code`, `state`, `iss`). Keycloak marks its session cookies
`Secure` even on `http://localhost`, so the helper forwards them itself; a browser is not needed. The same helper
serves the MCP Python SDK client (as its redirect and callback handlers) and the Claude Code driver.

## Client notes

| Note | Handling |
|---|---|
| Claude Code asks for `offline_access` and sends `prompt=consent` | the QA realm allows both; no consent screen is configured |
| A port held by an earlier sign-in | the driver checks that 8765 is free before it starts |
| The MCP Python SDK finds no `scopes_supported` in the PRM and requests every scope of the provider's metadata; Keycloak answers `invalid_scope` for scopes not assigned to `specflow-mcp` | the SDK client's redirect handler narrows the request to `openid` before the login helper runs; Claude Code needs no change |

Run reports (scenario, outcome, duration; never tokens) go to a git-ignored folder; screenshots are never taken or
committed.
