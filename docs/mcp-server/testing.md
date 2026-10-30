---
type: Design
title: "MCP server testing"
description: "Test tiers, QA fixtures, the end-to-end scenario matrix, and scripted QA with Claude Code and the MCP Python SDK against a real OIDC provider"
status: draft
tags: [mcp, testing]
---
# testing.md: test tiers, fixtures, scenario matrix and end-to-end QA with a real client

## Tiers

| Tier | Gate | Covers |
|---|---|---|
| Unit | always (`make check`) | settings and guards of the role; claim profile branches; cursor codecs; PRM document; error mapping and sanitizing; result shaping; the membership structural test; import contracts |
| HTTP boundary | always (`make check`) | the role's ASGI app over `httpx.ASGITransport` with the loopback JWKS stub of `specflow_auth_support` and a fake runtime: the full auth matrix, PRM, Host and Origin rules, body cap, 404 body, every tool through an MCP client session over HTTP, REST rejecting MCP tokens |
| Live data | `SPECFLOW_TEST_POSTGRES=1`, `SPECFLOW_TEST_NEO4J=1` | repository reuse against Postgres; `count_nodes` and `nodes_page` against Neo4j, including a renamed ontology |
| End to end | `SPECFLOW_TEST_MCP_E2E=1`, local stack with `WITH=mcp` | the real role, real Keycloak, the MCP Python SDK client running the OAuth flow; the scenario matrix below |
| Agent QA | `SPECFLOW_TEST_MCP_CLAUDE=1` and a signed-in `claude` CLI | Claude Code signs in and answers developer prompts with the tools; assertions on tool calls and results |

The end-to-end and agent tiers are not part of CI: they need the local stack and, for the agent tier, model calls.

The harness is Python only. The QA library and its manual entry live in `backend/examples` (`mcp_qa_lib.py`,
`mcp_qa.py`, run with `uv run --env-file ../.env.local python examples/mcp_qa.py`); the end-to-end and agent tiers are
tests in `backend/tests`.

## Fixtures

One idempotent QA seed prepares all three stores. It writes graph data through `graph.core.OwnedWriter` as the build
owner (the path the graph live tests use), never with ad hoc Cypher.

| Store | Data |
|---|---|
| Keycloak | `alice@example.com`, `bob@example.com` (verified); `carol@other.example` (verified, domain not allowed); one password from `.env.local` |
| Postgres | users whose `auth_subject` is the Keycloak user id; `P-Alpha` and `P-Empty` owned by alice; `P-Beta` owned by bob |
| Neo4j | `P-Alpha`: 7 features, 2 modules, 3 functional requirements; `P-Beta`: 3 features; `P-Empty`: nothing |

## End-to-end scenarios

| # | Caller | Action | Expected |
|---|---|---|---|
| E1 | no token | `POST /mcp` | 401 with `resource_metadata`; the PRM names the resource URL and the issuer |
| E2 | alice | `list_projects` | `P-Alpha`, `P-Empty`; never `P-Beta` |
| E3 | alice | `list_features(P-Alpha, limit=5)`, then the next page | `total_count` 7; 5 then 2 items; only `FEAT::` ids; second `next_cursor` null |
| E4 | alice | `list_features(P-Beta)` | `project_not_found` |
| E5 | alice | `list_features(<random uuid>)` | `project_not_found`, same message as E4 |
| E6 | alice | `list_features(P-Empty)` | `total_count` 0, no items |
| E7 | bob | `list_projects` | `P-Beta` only |
| E8 | alice, removed from `P-Alpha` between two calls | `list_features(P-Alpha)` | second call `project_not_found` |
| E9 | a `specflow-web` token (REST audience) | `POST /mcp` | 401 |
| E10 | carol | `POST /mcp` | 403 `domain_not_allowed`, no challenge |
| E11 | alice | `list_features` with a forged cursor | `invalid_cursor` |
| E12 | alice | `POST /mcp` with a foreign `Host` | refused by transport security |

The reverse audience case (an MCP token on `/api/v1`) is an HTTP-boundary test of the API, because the local API runs
with dev login.

## Agent QA with Claude Code

The driver acts as a developer at a terminal:

1. Registers a uniquely named server (`specflow-qa-<run id>`) in a temporary project folder with
   `claude mcp add --transport http --scope local --client-id specflow-mcp --callback-port 8765`.
2. Runs `claude mcp login <name> --no-browser` in a pseudo-terminal, reads the authorization URL (printed inside an
   OSC 8 hyperlink escape), completes the Keycloak sign-in with the HTTP login helper, and writes the redirect URL back.
3. Runs persona prompts with `claude -p --output-format stream-json --verbose --allowedTools <the two tools>`:

| Prompt (as a developer would type it) | Assert on tool events |
|---|---|
| Which SpecFlow projects can I work on? | `list_projects` called; results hold `P-Alpha` and `P-Empty`, not `P-Beta` |
| List every feature of P-Alpha. | `list_features` called for `P-Alpha` until `next_cursor` is null, or with a limit covering all; the union holds the 7 feature ids and no `REQ::` id |
| Show me the features of project `<P-Beta id>`. | `list_features` result is `project_not_found`; no `P-Beta` feature name appears anywhere in the run |

4. Tears down: `claude mcp logout <name>` and `claude mcp remove <name> --scope local` (the CLI stores MCP tokens in
   the user's real Claude configuration).

Assertions read tool inputs and results from the stream; the model's prose is recorded, never asserted.

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

Run reports (scenario, outcome, duration; never tokens) go to a git-ignored folder; screenshots are never taken or
committed.
