---
type: Requirements
title: "MCP server requirements"
description: "Requirement units of the MCP server (compact form): authentication, access control, tools, operations and non-functional"
status: draft
tags: [mcp]
---
# requirements.md: requirement units of the MCP server (compact form), functional and non-functional

Each row is one testable requirement in EARS form with one acceptance criterion. Status of every unit: Proposed.
Verification is by test unless the row says otherwise.

## Authentication ([auth.md](auth.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-AUTH-01 | In `oidc` mode, when a request to `/mcp` carries no bearer token, the service shall answer 401 with `WWW-Authenticate: Bearer` naming the PRM URL. | Given no `Authorization` header, then 401 and `resource_metadata="<origin of MCP_RESOURCE_URL>/.well-known/oauth-protected-resource/mcp"`. |
| FR-MCP-AUTH-02 | The service shall serve the same PRM document at the path-inserted and the root well-known locations, built only from configuration. | Given a request with `Host: other.example` in the allowlist, then both documents name `MCP_RESOURCE_URL` and `[OIDC_ISSUER]`. |
| FR-MCP-AUTH-03 | In `oidc` mode, the service shall accept only tokens signed with RS256 or ES256 by a key of the issuer's JWKS. | Given an HS256 token or one signed by an unknown key, then 401. |
| FR-MCP-AUTH-04 | The service shall accept only tokens whose issuer equals `OIDC_ISSUER` and whose audience, without the ignored audiences, is exactly `MCP_AUDIENCE`. | Given `aud=[MCP URL, account]`, then accepted; given `aud=specflow-api` or `aud=[MCP URL, specflow-api]`, then 401. |
| FR-MCP-AUTH-05 | The service shall accept only tokens whose client claim is in `MCP_CLIENT_IDS`. | Given `azp=specflow-web`, then 401. |
| FR-MCP-AUTH-06 | If a valid token's email is unverified, invalid or outside `AUTH_ALLOWED_EMAIL_DOMAINS`, then the service shall answer 403 without a challenge. | Given `carol@other.example`, then 403 `domain_not_allowed` and no `WWW-Authenticate` challenge. |
| FR-MCP-AUTH-07 | The token checks shall read subject, client, email, email-verified, token type and ignored audiences from the `AUTH_*` claim profile, whose defaults are provider-neutral. | Given `AUTH_TOKEN_TYPE=keycloak`, then every existing auth test passes unchanged; given the defaults, a token without `typ` and with only `client_id` is accepted while an ID token is still refused by the audience rule; given `AUTH_SUBJECT_CLAIM=oid`, then the `oid` value is the `Principal` subject. |
| FR-MCP-AUTH-08 | The REST API shall reject tokens issued for the MCP audience. | Given a token with `aud=<MCP URL>`, when it calls `GET /api/v1/me`, then 401. |
| FR-MCP-AUTH-09 | The `mcp` role shall refuse to start with `AUTH_MODE=dev` outside local or inside Kubernetes, a dev signing key or no issuer under `oidc`, a resource URL whose path is not `/mcp` or that is not `https` outside local, no client id, an `MCP_AUDIENCE` equal to a client id or to `AUTH_API_AUDIENCE`, or ignored audiences that hold either surface's audience. | Given each case, then startup fails naming the variable. |
| FR-MCP-AUTH-10 | The service shall never write a token, an `Authorization` value or an email to logs or error bodies. | Given the full auth matrix, then captured logs and bodies contain none of them. |
| FR-MCP-AUTH-11 | In local dev mode, the role shall accept dev tokens minted for the MCP audience and client, whose subject is the web dev login's subject for the same email. | Given alice creates a project through the REST API with her web dev token, when an MCP client calls `list_projects` with her MCP dev token, then the project is listed; her web dev token on `/mcp` gets 401. |
| FR-MCP-AUTH-12 | The `api` role shall refuse to start when `AUTH_API_AUDIENCE` equals one of `AUTH_API_CLIENT_IDS` or is listed in `AUTH_IGNORED_AUDIENCES`. | Given `AUTH_API_AUDIENCE=specflow-web` with the default client ids, then `specflow api --check-config` fails naming `AUTH_API_AUDIENCE`. |

## Access control ([access-control.md](access-control.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-ACC-01 | `list_projects` shall return only projects the caller is a member of. | Given alice owns two projects and bob one, when alice lists, then bob's project is absent. |
| FR-MCP-ACC-02 | A tool with a `project_id` shall check membership before any other read and, when denied, shall run no graph query. | Given a non-member, then a recording executor records zero queries. |
| FR-MCP-ACC-03 | A malformed, unknown or foreign project id shall give the same `project_not_found` result, and none of them shall reach the graph. | Given bob's existing project id with alice's token, a random UUID and `not-a-uuid`, then the three results are byte-identical and a recording executor records zero graph queries. |
| FR-MCP-ACC-04 | Membership shall be read on every call. | Given the test deletes alice's `project_members` row in Postgres between two calls, then the second call answers `project_not_found`. |
| FR-MCP-ACC-05 | The caller's user row shall be provisioned or refreshed through `upsert_user` with the configured subject claim. | Given a new subject, when it calls twice, then exactly one user row exists. |
| FR-MCP-ACC-06 | Every registered tool with a `project_id` argument shall pass through the one membership function. | Inspection: the structural test enumerates registered tools and passes. |

## Tools ([tools.md](tools.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-TOOL-01 | `list_projects` shall page newest first with `limit` 1 to 100 (default 50, bounds in the input schema) and an opaque cursor. | Given 3 projects and `limit=2`, then pages of 2 and 1, the last with `next_cursor` null. |
| FR-MCP-TOOL-02 | `list_features` shall return only nodes of the ontology node type `feature`. | Given a project with features, modules and requirements, then only `FEAT::` ids are returned. |
| FR-MCP-TOOL-03 | `list_features` shall order by `spec_id` and report a `total_count` that does not depend on the page. | Given 7 features and `limit=5`, then pages of 5 and 2, both with `total_count` 7. |
| FR-MCP-TOOL-04 | `list_features` shall read at most `limit + 1` nodes per page. | Inspection: the page query's limit parameter is `limit + 1`; `GraphReader.nodes` is never called. |
| FR-MCP-TOOL-05 | Feature items shall hold `spec_id`, `name` and the stored ontology attributes only. | Given a feature with an embedding and provenance, then neither appears in the result. |
| FR-MCP-TOOL-06 | The feature label and id prefix shall come from the loaded ontology's type with key `feature`. | Given a copy of the shipped ontology whose `feature` type keeps its key but has another label and id prefix (for example `Funktion`, `FUNK::`), then `list_features` returns exactly that label's nodes; given an ontology without a `feature` type, then startup fails. |
| FR-MCP-TOOL-07 | If a cursor is over 512 characters or does not decode, or a `list_features` cursor has the wrong format version or shape, then the tool shall answer `invalid_cursor`. | Given a forged cursor and a 513-character cursor, then `invalid_cursor` for both tools. |
| FR-MCP-TOOL-08 | If Postgres or Neo4j fails, then the tool shall answer `database_unavailable` or `graph_unavailable` with a fixed message. | Given Neo4j stopped, then `graph_unavailable` and no driver text. |
| FR-MCP-TOOL-09 | Every version 1 tool shall declare `readOnlyHint` true, `destructiveHint` false, `idempotentHint` true, `openWorldHint` false. | Given `tools/list`, then the four annotations are present on both tools. |
| FR-MCP-TOOL-10 | `GraphReader.count_nodes` and `nodes_page` shall count and page spec nodes of given labels by `spec_id`. | Given 120 nodes, when paged by 50, then 50, 50, 20, `next_after` `None` at the end, count 120; `limit=0` raises `invalid_limit`. |

## Operations ([architecture.md](architecture.md), [deployment.md](deployment.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-OPS-01 | The `mcp` role shall start with database, Neo4j, auth and MCP settings only. | Given no Temporal, storage or LLM variable, then `specflow mcp --check-config` succeeds. |
| FR-MCP-OPS-02 | `/readyz` shall report database and Neo4j readiness. | Given Neo4j stopped, then 503 with `neo4j` not ok. |
| FR-MCP-OPS-03 | The service shall not need session affinity. | Given two app instances, when `initialize` goes to one and `tools/call` to the other, then the call succeeds. |
| FR-MCP-OPS-04 | The service shall refuse a `Host` outside the allowlist and any `Origin` outside `MCP_ALLOWED_ORIGINS`. | Given `Host: evil.example` or `Origin: https://evil.example`, then refused. |
| FR-MCP-OPS-05 | The service shall refuse request bodies over 1 MiB. | Given a 2 MiB body, then 413. |
| FR-MCP-OPS-06 | An unknown path shall answer 404 with the JSON error body. | Given `GET /nope`, then 404 `not_found`. |
| FR-MCP-OPS-07 | Every tool call that passes the input schema shall log `mcp.tool.completed` with user id, tool, project id, outcome, item count and duration, and no content, whatever the outcome. | Given one call of each tool and one `list_features` call for a foreign project, then three events with those fields (the last with outcome `project_not_found`) and no feature text. |
| FR-MCP-OPS-08 | `make up` shall always start the `mcp` role, in dev mode; `make up WITH=keycloak` shall also start Keycloak with the imported realm and run the role in `oidc` mode. | Given a fresh `.env.local`, then in dev mode the role accepts a dev token, and with Keycloak both services are healthy and the PRM names the local issuer. |

## Non-functional

| ID | Requirement | Acceptance |
|---|---|---|
| NFR-MCP-01 | Tool calls shall answer within 500 ms at the 95th percentile at 10 calls per second on the local stack against a project with 300 features. | Measured by end-to-end scenario E14 (`P-Large`, 60 s) and recorded in its report. |
| NFR-MCP-02 | The `mcp` SDK shall be a declared dependency `>=2.3,<3`, and the agent suites shall pass with the updated lock. | `make check` passes after the lock update. |
| NFR-MCP-03 | Import rules M2 to M4 shall be import-linter contracts on direct imports, and the Cypher and SQL validation tracks shall flag query text in `api.mcp` (rules M3, M4). | `make validate-python-lint`, `make validate-cypher` and `make validate-sql` pass, and each fails on a planted violation in `api.mcp`. |
| NFR-MCP-04 | The repository's static validation shall pass. | `make validate` passes. |
| NFR-MCP-05 | No secret shall be committed: the realm file holds no credential, local secrets are generated into `.env.local`, and the QA seed sets the `specflow-web` client secret through the Keycloak admin API. | No tracked file holds a value of `.env.local` (checked by a scan that prints no values); the realm file has no `credentials` or `secret` value. |
