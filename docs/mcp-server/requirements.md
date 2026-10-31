---
type: Requirements
title: "MCP server requirements"
description: "Requirement units of the MCP server (compact form): authentication, access control, tools, workspace binding, operations and non-functional"
status: stable
tags: [mcp]
---
# requirements.md: requirement units of the MCP server (compact form), functional and non-functional

Each row is one testable requirement in EARS form with one acceptance criterion. Status of every unit: Implemented.
Verification is by test unless the row says otherwise.

Each row states the behavior and links the section that defines it; values are defined only there. Acceptance criteria hold concrete test values.

## Authentication ([auth.md](auth.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-AUTH-01 | In `oidc` mode, when a request to `/mcp` carries no bearer token, the service shall answer as [auth.md](auth.md#failure-responses) defines. | Given no `Authorization` header, then 401 and `resource_metadata="<origin of MCP_RESOURCE_URL>/.well-known/oauth-protected-resource/mcp"`. |
| FR-MCP-AUTH-02 | The service shall serve the protected resource metadata at the locations and from the sources [auth.md](auth.md#protected-resource-metadata) defines. | Given a request with `Host: other.example` in the allowlist, then both documents name `MCP_RESOURCE_URL` and `[OIDC_ISSUER]`. |
| FR-MCP-AUTH-03 | In `oidc` mode, the service shall verify the token signature as checks 2 and 3 of [auth.md](auth.md#token-validation) define. | Given an HS256 token or one signed by an unknown key, then 401. |
| FR-MCP-AUTH-04 | The service shall check issuer and audience as checks 4 and 5 of [auth.md](auth.md#token-validation) define. | Given `aud=[MCP URL, account]`, then accepted; given `aud=specflow-api` or `aud=[MCP URL, specflow-api]`, then 401. |
| FR-MCP-AUTH-05 | The service shall check the client as check 8 of [auth.md](auth.md#token-validation) defines. | Given `azp=specflow-web`, then 401. |
| FR-MCP-AUTH-06 | If a valid token's email fails the email policy, then the service shall answer as [auth.md](auth.md#failure-responses) defines. | Given `carol@other.example`, then 403 `domain_not_allowed` and no `WWW-Authenticate` challenge. |
| FR-MCP-AUTH-07 | The token checks shall read the identity claims from the claim profile that [auth.md](auth.md#claim-profile-for-any-oidc-provider) defines, with its provider-neutral defaults. | Given `AUTH_TOKEN_TYPE=keycloak`, then every existing auth test passes unchanged; given the defaults, a token without `typ` and with only `client_id` is accepted while an ID token is still refused by the audience rule; given `AUTH_SUBJECT_CLAIM=oid`, then the `oid` value is the `Principal` subject; given `AUTH_TOKEN_TYPE=claim` with `AUTH_TOKEN_TYPE_CLAIM=x_token_kind` and `AUTH_TOKEN_TYPE_VALUE=access`, a token with `x_token_kind=access` is accepted while a token without the claim, with a non-string value or with another value gets 401. |
| FR-MCP-AUTH-08 | The REST API shall reject tokens issued for the MCP audience (S5 of [auth.md](auth.md#security-rules)). | Given a token with `aud=<MCP URL>`, when it calls `GET /api/v1/me`, then 401. |
| FR-MCP-AUTH-09 | The `mcp` role shall refuse to start on the settings that [architecture.md](architecture.md#settings) lists, including the audience mix-ups of [auth.md](auth.md#token-validation). | Given each refusal of both lists, then startup fails naming the variable; given `AUTH_TOKEN_TYPE=claim` and `MCP_AUDIENCE` equal to the client id, then startup succeeds. |
| FR-MCP-AUTH-10 | The service shall keep secrets and identity data out of logs and error bodies, as S2 of [auth.md](auth.md#security-rules) defines. | Given the full auth matrix, then captured logs and bodies contain no token, no `Authorization` value and no email. |
| FR-MCP-AUTH-11 | In local dev mode, the role shall accept the dev tokens that [auth.md](auth.md#local-and-development-mode) defines and treat their holder as the web dev login's user for the same email. | Given alice creates a project through the REST API with her web dev token, when an MCP client calls `list_projects` with her MCP dev token, then the project is listed; her web dev token on `/mcp` gets 401. |
| FR-MCP-AUTH-12 | The `api` role shall refuse to start on the audience mix-ups that [auth.md](auth.md#token-validation) lists for it. | Given `AUTH_API_AUDIENCE=specflow-web` with the default client ids, then `specflow api --check-config` fails naming `AUTH_API_AUDIENCE`; given the same value under `AUTH_TOKEN_TYPE=claim`, the check passes. |

## Access control ([access-control.md](access-control.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-ACC-01 | `list_projects` shall list only member projects (AC5, [access-control.md](access-control.md#decision-point)). | Given alice owns two projects and bob one, when alice lists, then bob's project is absent. |
| FR-MCP-ACC-02 | A tool with a `project_id` shall check membership first and run no graph query when it is denied (AC2 and AC4, [access-control.md](access-control.md#decision-point)). | Given a non-member, then a recording executor records zero queries. |
| FR-MCP-ACC-03 | A malformed, unknown or foreign project id shall be answered as AC3 of [access-control.md](access-control.md#decision-point) defines. | Given bob's existing project id with alice's token, a random UUID and `not-a-uuid`, then the three results are byte-identical and a recording executor records zero graph queries. |
| FR-MCP-ACC-04 | Membership shall be read on every call (AC6, [access-control.md](access-control.md#decision-point)). | Given the test deletes alice's `project_members` row in Postgres between two calls, then the second call answers `project_not_found`. |
| FR-MCP-ACC-05 | The caller's user row shall be provisioned or refreshed as AC1 of [access-control.md](access-control.md#decision-point) defines, keyed by the subject claim of the [claim profile](auth.md#claim-profile-for-any-oidc-provider). | Given a new subject, when it calls twice, then exactly one user row exists. |
| FR-MCP-ACC-06 | Every tool with a `project_id` shall pass through the one membership function (AC2, [access-control.md](access-control.md#decision-point)). | Inspection: the structural test enumerates registered tools and passes. |

## Tools ([tools.md](tools.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-TOOL-01 | `list_projects` shall page as [tools.md](tools.md#list_projects) defines. | Given 3 projects and `limit=2`, then pages of 2 and 1, the last with `next_cursor` null. |
| FR-MCP-TOOL-02 | `list_features` shall return the nodes of the type that [tools.md](tools.md#list_features) defines. | Given a project with features, modules and requirements, then only `FEAT::` ids are returned. |
| FR-MCP-TOOL-03 | `list_features` shall order and count as [tools.md](tools.md#list_features) defines. | Given 7 features and `limit=5`, then pages of 5 and 2, both with `total_count` 7. |
| FR-MCP-TOOL-04 | `list_features` shall read a page as [tools.md](tools.md#graph-read-used-by-list_features) defines. | Inspection: the page query's limit parameter is `limit + 1`; `GraphReader.nodes` is never called. |
| FR-MCP-TOOL-05 | Feature items shall hold the fields that [tools.md](tools.md#list_features) defines. | Given a feature with an embedding and provenance, then neither appears in the result. |
| FR-MCP-TOOL-06 | The feature label and id prefix shall come from the loaded ontology as [tools.md](tools.md#list_features) defines. | Given a copy of the shipped ontology whose `feature` type keeps its key but has another label and id prefix (for example `Funktion`, `FUNK::`), then `list_features` returns exactly that label's nodes; given an ontology without a `feature` type, then startup fails. |
| FR-MCP-TOOL-07 | If a cursor is invalid, then the tool shall answer with the error that [tools.md](tools.md#error-codes) defines for it (cursor rules in [tools.md](tools.md#conventions)). | Given a forged cursor and a 513-character cursor, then `invalid_cursor` for both tools. |
| FR-MCP-TOOL-08 | If Postgres or Neo4j fails, then the tool shall answer with the fixed error that [tools.md](tools.md#error-codes) defines. | Given Neo4j stopped, then `graph_unavailable` and no driver text. |
| FR-MCP-TOOL-09 | Every version 1 tool shall declare the annotations that [tools.md](tools.md#conventions) defines. | Given `tools/list`, then `readOnlyHint` true, `destructiveHint` false, `idempotentHint` true and `openWorldHint` false on both tools. |
| FR-MCP-TOOL-10 | `GraphReader.count_nodes` and `nodes_page` shall count and page as [tools.md](tools.md#graph-read-used-by-list_features) defines (contract owner: [graph/api.md](../graph/api.md#read-and-search)). | Given 120 nodes, when paged by 50, then 50, 50, 20, `next_after` `None` at the end, count 120; `limit=0` raises `invalid_limit`. |

## Workspace binding ([tools.md](tools.md#workspace-binding))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-BIND-01 | The `initialize` result shall carry the instructions that [tools.md](tools.md#server-instructions) defines. | Given `initialize`, then the instructions contain `MCP_RESOURCE_URL` and `.specflow/config.json`, are under 2,048 characters and contain no project id, name or feature text. |
| FR-MCP-BIND-02 | The server shall offer the prompt that [tools.md](tools.md#the-select_project-prompt) defines, behind the auth guard. | Given a valid token, then `prompts/list` lists `select_project` with an optional `project` argument, and `prompts/get` returns the steps and the exact version 1 JSON, with `project` `Checkout` kept as quoted data; given no token, then 401. |
| FR-MCP-BIND-03 | After `select_project`, the folder shall hold a binding file that [tools.md](tools.md#the-binding-file) defines and that names the chosen project. | Agent tier step 1: the file validates against the contract schema and names P-Alpha. |
| FR-MCP-BIND-04 | A new session in a bound folder shall apply rule 1 of [tools.md](tools.md#rules). | Agent tier step 2: `list_features` is called with P-Alpha's id and `list_projects` is never called. |
| FR-MCP-BIND-05 | Selecting again shall replace the whole file, as [tools.md](tools.md#rules) defines, and the next session shall use the new project. | E17 and agent tier step 4: the file names P-Empty and the next session lists features of P-Empty (`total_count` 0). |
| FR-MCP-BIND-06 | A binding to a project the caller is not a member of shall be answered as rule 4 of [tools.md](tools.md#rules) defines. | E17 and agent tier step 3: a file bound to P-Beta answers `project_not_found`, no P-Beta feature name appears, and no other project is used. |
| FR-MCP-BIND-07 | Project tools shall keep a required `project_id`, and the binding shall add no server state ([tools.md](tools.md#workspace-binding)). | `tools/list` input schemas are unchanged except descriptions; no new table or setting. |

## Operations ([architecture.md](architecture.md), [deployment.md](deployment.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-OPS-01 | The `mcp` role shall start with only the settings that [architecture.md](architecture.md#settings) names for it. | Given no Temporal, storage or LLM variable, then `specflow mcp --check-config` succeeds. |
| FR-MCP-OPS-02 | `/readyz` shall report readiness as [architecture.md](architecture.md#components) (Health) defines. | Given Neo4j stopped, then 503 with `neo4j` not ok. |
| FR-MCP-OPS-03 | The service shall not need session affinity ([architecture.md](architecture.md#transport), Sessions). | Given two app instances, when `initialize` goes to one and `tools/call` to the other, then the call succeeds. |
| FR-MCP-OPS-04 | The service shall refuse a `Host` and an `Origin` as [architecture.md](architecture.md#transport) defines. | Given `Host: evil.example` or `Origin: https://evil.example`, then refused. |
| FR-MCP-OPS-05 | The service shall refuse oversized request bodies as [architecture.md](architecture.md#transport) defines. | Given a 2 MiB body, then 413. |
| FR-MCP-OPS-06 | An unknown path shall be answered as [architecture.md](architecture.md#http-surface-of-the-role) defines. | Given `GET /nope`, then 404 `not_found`. |
| FR-MCP-OPS-07 | Every tool call that passes the input schema shall be audited as [architecture.md](architecture.md#logging-and-audit) defines, whatever the outcome. | Given one call of each tool and one `list_features` call for a foreign project, then three events with user id, tool, project id, outcome, item count and duration (the last with outcome `project_not_found`) and no feature text. |
| FR-MCP-OPS-08 | `make up` shall start the `mcp` role as [deployment.md](deployment.md#local-stack) defines, with and without `WITH=keycloak`. | Given a fresh `.env.local`, then in dev mode the role accepts a dev token, and with Keycloak both services are healthy and the PRM names the local issuer. |

## Non-functional

The targets below are the only place the quality numbers are defined; other files link here.

| ID | Requirement | Acceptance |
|---|---|---|
| NFR-MCP-01 | Tool calls shall answer within 500 ms at the 95th percentile at 10 calls per second on the local stack against a project with 300 features. | Measured by end-to-end scenario E14 of [testing.md](testing.md#end-to-end-scenarios) (`P-Large`, 60 s) and recorded in its report. |
| NFR-MCP-02 | The `mcp` SDK shall be a declared dependency in the range that [architecture.md](architecture.md#transport) defines, and the agent suites shall pass with the updated lock. | `make check` passes after the lock update. |
| NFR-MCP-03 | The import rules of [architecture.md](architecture.md#import-rules) shall be import-linter contracts on direct imports, and the Cypher and SQL validation tracks shall flag query text in `api.mcp`. | `make validate-python-lint`, `make validate-cypher` and `make validate-sql` pass, and each fails on a planted violation in `api.mcp`. |
| NFR-MCP-04 | The repository's static validation shall pass. | `make validate` passes. |
| NFR-MCP-05 | No secret shall be committed; the local stack keeps secrets as [deployment.md](deployment.md#local-stack) defines. | No tracked file holds a secret value of `.env.local` (checked by a scan that prints no values); the realm file has no `credentials` or `secret` value. |
