# Review of the first draft plan

The draft supplied with the request ("MCP server for Claude Code: list-projects and list-features") checked against the
code at `01a02b1`, the MCP SDK 2.3.0 source, and a working spike (Claude Code and the MCP Python SDK signing in at
Keycloak and calling an SDK 2.3.0 resource server). Verdicts: **keep**, **change**, **drop**, **add** (missing).
The result is the design in [`docs/mcp-server/`](../../docs/mcp-server/INDEX.md).

## Decisions table

| Draft | Verdict | Why |
|---|---|---|
| Add `list-projects` next to `list-features` | keep | Discovery is needed before a project id is known. Names become `list_projects`, `list_features` (snake_case) |
| MCP owns its own Postgres and Neo4j queries; `graph.core` and `docs/graph` untouched | change | Postgres: `db.repositories.list_projects` and `get_member_project` already are the member-projects query with keyset paging; a second copy is a second access decision that can drift. Neo4j: Cypher outside `graph/*/queries` escapes the cypher validation track and breaks the graph library's one-home-for-Cypher rule. The paged read becomes `GraphReader.count_nodes` and `nodes_page` |
| OIDC only, no dev-token path | keep | |
| Real Claude Code smoke test waits for the production route and IdP client | change | A local Keycloak profile plus a scripted `claude mcp login --no-browser` makes it testable now; proven in the spike |
| Graph reads only; no Rosetta, workflow tools or writes | keep | |

## Step 0 (folder and ticketing)

| Draft | Verdict | Why |
|---|---|---|
| `.specs/mcp-server/planning/` with plan, decisions, state, infra request | change | Durable design in `docs/mcp-server/` (bundle like `docs/graph`), execution plan in `plans/mcp-server/` |
| Create tracker story and sub-tasks, write keys back | drop | Internal tracker data does not belong in a public repository; progress lives in `HANDOFF.md` |

## Step 1 (dependency, settings, runtime)

| Draft | Verdict | Why |
|---|---|---|
| `mcp==2.3.0` | change | `mcp` 2.1.1 is already locked through `claude-agent-sdk`, `openai-agents`, `temporalio-openai-agents`; declaring `>=2.3,<3` bumps it for them too, so the agent suites must pass after the lock update |
| `MCP_ENABLED` switch on the API | drop | A separate role is on when it runs |
| `MCP_RESOURCE_URL`, `MCP_CLIENT_IDS`, `MCP_REQUIRED_SCOPES`, `MCP_ALLOWED_HOSTS`, `MCP_ALLOWED_ORIGINS` | keep | Plus `MCP_AUDIENCE` and `MCP_SCOPES_SUPPORTED` for IdPs whose audience or scope strings differ (Entra ID) |
| `MCP_MAX_BODY_BYTES`, `MCP_PAGE_LIMIT`, `MCP_PAGE_LIMIT_MAX` settings | change | Constants: the SDK caps bodies (`max_request_body_size`); REST hardcodes its page bounds too |
| Guards: no dev mode, https resource outside local, client list, allowed hosts | keep | Plus: resource path exactly `/mcp`, audience not equal to a client id |
| `Runtime.mcp` inside the API runtime, lazy ontology | change | Own slim runtime in the `mcp` role (DB, Neo4j, ontology at startup) |
| Spike to confirm SDK signatures, mounting, allowlist names | done | `MCPServer`, `streamable_http_app(stateless_http, json_response, max_request_body_size, transport_security)`, `TransportSecuritySettings(allowed_hosts, allowed_origins)`; the returned app runs its session manager in its own lifespan |

## Step 2 (host MCP inside FastAPI)

| Draft | Verdict | Why |
|---|---|---|
| Mount the SDK app at `/` after the REST routers | drop | The API is internal by design and would have to be exposed; its start fails without Temporal (`build_runtime`), so MCP would fail with Temporal; a catch-all mount changes the API's unknown-path answer that archkit checks; a mounted app's lifespan never runs. Replaced by the `mcp` role |
| `tool_holder` mutable object | change | Tools read the runtime and `Principal` from the request context; no module globals |
| Custom ASGI body-size wrapper | drop | SDK `max_request_body_size` |
| Host and Origin allowlists, no cookies, no wildcard CORS | keep | |

## Step 3 (authentication and isolation)

| Draft | Verdict | Why |
|---|---|---|
| `Principal.scopes`, `ClaimPolicy.required_scopes`, 403 `insufficient_scope` | keep | |
| `McpTokenVerifier` as the SDK `TokenVerifier`, SDK `AuthSettings` publishing metadata | change | A verifier can only return a token or nothing: every policy failure becomes 401, so a domain-policy failure sends the client into a sign-in loop; tools get no `Principal`. Own guard on the `auth` package; own PRM route |
| `validate_token_resource=True` as an advisory mitigation | change | Our audience rule (exactly one non-ignored audience) is stricter than resource equality; the advisory id in the draft was not verified and is not cited |
| Separate MCP policy: REST tokens fail on MCP and the reverse, both tested | keep | |
| MCP-owned `access.py` with its own keyset pagination | drop | Reuse `db.repositories` |
| Membership on every call, before Neo4j, identical not-found, no graph query on denial | keep | Plus a structural test over the tool registry |
| Claim rules are Keycloak-specific (`typ` claim, `azp`, `email_verified` in the access token) | add | "Any OIDC provider" needs a claim profile; shared with REST because the subject must match the web app's user row |
| Users keyed by subject without issuer | add | Forces one issuer for web and MCP; recorded as a constraint |

## Step 4 (tools)

| Draft | Verdict | Why |
|---|---|---|
| `list-projects` returns `role` | drop | Only `owner` exists |
| `list-features` result shape (`total_count`, `items`, `next_cursor`) | keep | `label` dropped (fixed by the tool) |
| Count and page queries, keyset on `spec_id`, `limit + 1` | keep | In `graph.core` |
| Label from the ontology, attributes from stored specs, no vectors or statements | keep | Type key `feature` |
| Codes `project_not_found`, `invalid_limit`, `invalid_cursor`, `graph_unavailable` | change | `invalid_limit` is the input schema's job; add `database_unavailable`, `internal_error` |
| Annotations read-only, non-destructive, idempotent | keep | Plus `openWorldHint: false` |

## Step 5 (tests)

| Draft | Verdict | Why |
|---|---|---|
| Offline support module, HTTP-boundary tests over `ASGITransport` | keep | |
| Metadata must not advertise `offline_access` | change | Claude Code adds `offline_access` itself (observed); the control is the IdP client's scopes. Assert `scopes_supported` equals configuration instead |
| MCP-off app has no `/mcp` and imports no SDK | change | Becomes: the REST app never imports `api.mcp` or the SDK (import contract) |
| Live Neo4j and Postgres tiers | keep | |
| Real-client tests | add | End-to-end tier with the Python SDK client and an agent tier with the Claude Code CLI against local Keycloak |

## Step 6 (docs and infra)

| Draft | Verdict | Why |
|---|---|---|
| README section, `.env.example` entries | keep | Plus `docs/configuration.md` |
| Infra request: public route without forward-auth, public PKCE client, audience mapper, scope | keep | Client named `specflow-mcp` (not client-specific); both loopback hosts; both well-known paths; written as public operator requirements in `deployment.md` without internal host names |
| Keycloak 26.6+ for client metadata documents | change | Not verified; moved to `later.md` |

## Missing from the draft

Revocation latency as a number, the `offline_access` policy, untrusted graph text reaching the agent, the no-passthrough
rule, audit events, readiness for the endpoint, edge rate limits, a local OIDC provider, the docs validator's rules,
and the environment quirks found in the spike.
