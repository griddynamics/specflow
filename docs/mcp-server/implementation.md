---
type: Roadmap
title: "MCP server implementation log"
description: "Short, high-level log of how the MCP server was built and changed: milestone status and one quick summary per finished milestone or later change"
status: draft
tags: [mcp]
---
# implementation.md: implementation log of the MCP server

## How to use this file

- This is a short, high-level log for future readers. It is the only change log of the MCP server.
- The detailed plan (session files, tracking, evidence) is the coding flow's working plan; it is kept outside the
  repository. This file keeps only what lasts: which milestones exist, their status, and what each one delivered.
- When the coding flow finishes a milestone, it sets the milestone's status to `done` below and adds a quick summary
  ([template](#log)). A summary is a few lines, not a report.
- Later changes to the MCP server, in this pull request or after it, update the design documents in place and add a
  quick summary here.

## Milestones

| Id | Milestone | Status |
|---|---|---|
| MCP-M1 | Graph paged read (`count_nodes`, `nodes_page`) | done |
| MCP-M2 | Auth claim profile for any OIDC provider | done |
| MCP-M3 | Local Keycloak and QA harness | done |
| MCP-M4 | MCP role, transport and auth guard | done |
| MCP-M5 | Tools and access control | done |
| MCP-M6 | Local stack wiring and setup docs | done |
| MCP-M7 | Real-client QA with the end-to-end and agent tiers | done |
| MCP-M8 | Bundle verification, security review and release check | done |

## Log

Each entry has a unique `###` heading: `### MCP-Mn: <title>` for a milestone, a short title for a later change.

Template:

```markdown
### MCP-Mn: <title>

- Date and commits: YYYY-MM-DD, <short SHAs>
- Summary: <what was delivered, two to four lines>
- Requirements proven: <ids>, or none
- Design sections updated in place: <file and section>, or none
- Follow-ups: <items>, or none
```

### Plan: design fixes made while planning

- Date and commits: 2026-10-31, the commit that adds this file
- Summary: the implementation plan was made from this bundle (eight milestones above). Planning found three gaps
  and fixed each in place with the smallest edit:
  - [requirements.md](requirements.md): the acceptance of NFR-MCP-05 named a "secret scan" that this repository does
    not have; it now names the check (no tracked file holds a secret value of `.env.local`; the realm file has no
    `credentials` or `secret` value).
  - [deployment.md](deployment.md#local-stack), realm file: the confidential `specflow-web` client had no redirect URI,
    which the token helper needs for scenario E9; it is now `http://localhost:3001/api/auth/callback/oidc`.
  - [deployment.md](deployment.md#local-stack), QA users: the row listed alice and bob, while
    [testing.md](testing.md#fixtures) and scenario E10 also need `carol@other.example`; the row now lists all three.
- Requirements proven: none
- Design sections updated in place: requirements.md (Non-functional), deployment.md (Local stack)
- Follow-ups: Q4 of [questions.md](questions.md) stays open; the plan follows its default

### MCP-M1: Graph paged read

- Date and commits: 2026-10-31, c5cfeb6
- Summary: `GraphReader.count_nodes` and `GraphReader.nodes_page` (model `NodePage`) count and page spec nodes of
  given labels by `spec_id`; the page query reads `limit + 1` rows after an exclusive cursor and keeps the projection
  and provenance of `nodes`. A test helper derives a renamed-feature ontology from the shipped one for FR-MCP-TOOL-06.
- Requirements proven: FR-MCP-TOOL-10
- Design sections updated in place: none; [../graph/api.md](../graph/api.md#read-and-search) lists both methods
- Follow-ups: none

### MCP-M2: Auth claim profile

- Date and commits: 2026-10-31, eaf5cb1
- Summary: six `AUTH_*` settings (subject, client, email and verified-email claims, token type rule, ignored
  audiences) drive token validation with provider-neutral defaults; the check order is unchanged and every rule fails
  closed. The REST API refuses MCP-audience tokens, the `api` role refuses an audience equal to a client id or listed as
  ignored, and the bearer parser is FastAPI-free.
- Requirements proven: FR-MCP-AUTH-07, FR-MCP-AUTH-08, FR-MCP-AUTH-12
- Design sections updated in place: none; [../configuration.md](../configuration.md) has the six rows and the
  corrected `OIDC_ISSUER` row
- Follow-ups: the local stack forwards the claim-profile variables in MCP-M6

### MCP-M3: Local Keycloak and QA harness

- Date and commits: 2026-10-31, 10ea78e
- Summary: an optional, digest-pinned Keycloak service (`WITH=keycloak`) imports a realm with no users or secrets;
  `make env-local` appends the Keycloak secrets to an existing `.env.local`. `examples/mcp_qa.py` and its library set
  up the QA users and the web client secret through the admin API, log in without a browser, mint tokens and dev
  tokens, seed the fixture projects idempotently, and drive Claude Code with teardown of its MCP registration.
- Requirements proven: NFR-MCP-05
- Design sections updated in place: none
- Follow-ups: `KEYCLOAK_*` rows in the configuration reference come with MCP-M6; the seed is idempotent per Keycloak
  container lifetime

### MCP-M4: MCP role, transport and auth guard

- Date and commits: 2026-10-31, 32d3539
- Summary: `specflow mcp` serves stateless Streamable HTTP with SDK 2.3 on port 8002 and starts with database, Neo4j,
  auth and MCP settings only. A guard in front of `/mcp` checks the token (401 with the PRM challenge, 403 without
  one), the PRM is served at both well-known paths, and Host, Origin, body-size and method checks, readiness probes
  and import contracts complete the role. An independent security review found no high finding; its fixes are in.
- Requirements proven: FR-MCP-AUTH-01 to -06, FR-MCP-AUTH-09, -10, -11 (subject and 401 halves), FR-MCP-OPS-01, -02,
  -04, -05, -06, NFR-MCP-02, NFR-MCP-03
- Design sections updated in place: [architecture.md](architecture.md): HTTP surface (`GET`, `DELETE` on `/mcp`
  answer 405), Errors (Host, Origin and body checks come after the guard), Logging (`mcp.auth.rejected` fields)
- Follow-ups: the REST role likely exits 143 on SIGTERM and loses its shutdown log (the mcp role has a local fix);
  the SDK matches Host case-sensitively and logs a refused Host or Origin at warning level

### MCP-M5: Tools and access control

- Date and commits: 2026-10-31, 7c38002
- Summary: `list_projects` and `list_features` are registered through one wrapper (audit, error mapping, user
  provisioning, membership, handler) with read-only annotations and fixed error codes. Membership is read on every
  call, and malformed, unknown and foreign project ids give the same `project_not_found` without a graph query. An
  independent access audit found no path to another member's project.
- Requirements proven: FR-MCP-ACC-01 to -06, FR-MCP-TOOL-01 to -09, FR-MCP-OPS-03, FR-MCP-OPS-07
- Design sections updated in place: [architecture.md](architecture.md): package layout (`access.py`), tool registry
  (handlers take the caller), Logging (`mcp.tool.completed` fields); [tools.md](tools.md): Paging (JSON-text cursors
  are refused by the input schema)
- Follow-ups: an out-of-range timestamp in a `list_projects` cursor is now `invalid_cursor` (the shared cursor
  decoder, so the REST API benefits too); stored feature attributes are assumed to be text

### MCP-M6: Local stack wiring and setup docs

- Date and commits: 2026-10-31, 2690e7b
- Summary: `make up` starts the `mcp` service in dev mode; `make up WITH=keycloak` switches only that service to
  `oidc` against the local realm, while the web app and REST API keep their dev login. The configuration reference,
  backend README, quickstart and deployment guide describe the role and how to connect Claude Code locally.
- Requirements proven: FR-MCP-OPS-08 (both modes healthy and exercised; in the build environment the images were built
  outside compose and the stack ran with the bundled Temporal)
- Design sections updated in place: [deployment.md](deployment.md#local-endpoint) (local Claude Code recipe)
- Follow-ups: the REST API still needs `AGENT_MODEL` and the LLM gateway names in `.env.local` before `make up`

### MCP-M7: Real-client QA with the end-to-end and agent tiers

- Date and commits: 2026-10-31, 398e6f9
- Summary: the end-to-end tier passes E1 to E12 and E14 in `oidc` mode and E13 in dev mode with the MCP Python SDK
  and plain HTTP; E14 measured p95 37.5 ms over 600 calls with no errors. The agent tier passes the three persona
  prompts in a signed-in Claude Code and leaves no `specflow-qa-` server behind.
- Requirements proven: NFR-MCP-01, FR-MCP-AUTH-11 (E13 half); scenarios E1 to E14
- Design sections updated in place: [testing.md](testing.md#client-notes) (the SDK client narrows the requested
  scope to `openid`, because the PRM advertises no scopes)
- Follow-ups: generic MCP clients against an IdP that refuses unknown scopes must request only assigned scopes; E13
  leaves one project in the local database per run

### MCP-M8: Bundle verification, security review and release check

- Date and commits: 2026-10-31, 6db8353
- Summary: the eight design documents were checked against the code, corrected in place and marked `stable`. An
  independent security review found four low findings, all fixed: a request id per HTTP request, a Keycloak admin
  password check in `make up`, and two wording fixes. The release check passes (`make check`, `make validate`,
  `make frontend-build`, `make e2e-production`), and no tracked file holds a secret of `.env.local`.
- Requirements proven: NFR-MCP-04; NFR-MCP-05 checked again
- Design sections updated in place: [architecture.md](architecture.md) (HTTP surface, package layout, errors,
  logging and audit), [deployment.md](deployment.md) (identity provider setup, local endpoint, local stack),
  [auth.md](auth.md) (local mode, S6), [testing.md](testing.md) (tiers, agent QA flags), [requirements.md](requirements.md)
  (status)
- Follow-ups: the REST role likely exits 143 on SIGTERM; Q4 of [questions.md](questions.md) stays open; a link in
  `docs/graph/api.md` to `implementation/tech-specs.md` was already broken before this work

### Client-audience tokens with a token-type claim

- Date and commits: 2026-10-31, the commit that adds this entry
- Summary: `AUTH_TOKEN_TYPE=claim` accepts access tokens only when the claim named by `AUTH_TOKEN_TYPE_CLAIM` holds
  exactly `AUTH_TOKEN_TYPE_VALUE`, and fails closed. Under a token-type check, `MCP_AUDIENCE` and `AUTH_API_AUDIENCE` may
  equal a client id, so a provider whose code flow issues tokens for the client works; under `none` the refusals stay.
  Startup keeps the two surfaces on separate clients and refuses a registered or identity claim as the marker.
- Requirements proven: FR-MCP-AUTH-07, FR-MCP-AUTH-09, FR-MCP-AUTH-12
- Design sections updated in place: [auth.md](auth.md) (client registration, audience refusals, claim profile,
  provider recipes, token lifetimes, decision AA8), [requirements.md](requirements.md) (FR-MCP-AUTH-07, -09, -12),
  [architecture.md](architecture.md#settings) (startup refusals), [later.md](later.md) (OAuth proxy mode),
  [questions.md](questions.md) (Q4)
- Follow-ups: the open items of Q4 in [questions.md](questions.md), verified in the production rollout

### Requirements link to their defining sections

- Date and commits: 2026-10-31, the commit that adds this entry
- Summary: each requirement row now states the behavior and links the section that defines it, so contract values live
  in one place and the acceptance column keeps the test values. The quality numbers are defined in
  [requirements.md](requirements.md#non-functional) and linked from the other files; the SDK range is defined in
  [architecture.md](architecture.md#transport). Copies of values in the other files became links.
- Requirements proven: none
- Design sections updated in place: [requirements.md](requirements.md) (all tables, introduction),
  [overview.md](overview.md) (constraints, assumed numbers), [architecture.md](architecture.md) (process, settings,
  constants, errors, logging, decision AD2), [auth.md](auth.md) (client registration, provider recipes, security
  rules), [tools.md](tools.md) (argument tables, error codes, graph read), [deployment.md](deployment.md) (role,
  public endpoint, identity provider setup, local endpoint, local stack), [access-control.md](access-control.md) (data
  returned, audit), [testing.md](testing.md) (scenario E14), [questions.md](questions.md) (Q4),
  [../configuration.md](../configuration.md) (MCP rows)
- Follow-ups: none
