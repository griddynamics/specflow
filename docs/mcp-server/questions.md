---
type: Questions
title: "MCP server decisions"
description: "Every design question of the MCP server with its locked decision, so implementation sessions run without review gates"
status: draft
tags: [mcp]
---
# questions.md: design questions of the MCP server and their locked decisions

All questions are closed. A session that finds a decision wrong records it in its handoff and stops at that point;
it does not reopen the decision on its own.

| # | Question | Decision (locked) | Basis |
|---|---|---|---|
| Q-MCP-01 | Where does the server run? | Its own role `specflow mcp`, same backend image, port 8002 | [architecture.md](architecture.md) AD1 |
| Q-MCP-02 | Which SDK? | Official `mcp`, declared `>=2.3,<3`, locked at 2.3.0 | AD2; 2.1.1 lacks the resource-validation setting |
| Q-MCP-03 | Which transport mode? | Streamable HTTP at `/mcp`, stateless, JSON responses | AD3 |
| Q-MCP-04 | Who issues tokens? | The IdP; the service is a resource server only | [auth.md](auth.md) AA1 |
| Q-MCP-05 | How do clients register? | One pre-registered public client `specflow-mcp`, PKCE S256, redirects `http://localhost:8765/callback` and `http://127.0.0.1:8765/callback` | AA2 |
| Q-MCP-06 | Which issuer? | `OIDC_ISSUER`, the web app's; one IdP per deployment | users are keyed by subject only |
| Q-MCP-07 | Which audience? | `MCP_AUDIENCE`, default `MCP_RESOURCE_URL`; exactly one non-ignored audience; never `specflow-api` | AA4 |
| Q-MCP-08 | Which scope? | `specflow:read` required and advertised; both configurable | auth.md |
| Q-MCP-09 | Dev tokens on MCP? | Refused; local work uses the bundled Keycloak | AA5 |
| Q-MCP-10 | `offline_access`? | Decided at the IdP; local realm allows it; production removes it or limits offline sessions to 30 days | auth.md revocation table |
| Q-MCP-11 | How is any OIDC provider supported? | Shared `AUTH_*` claim profile with today's behavior as defaults; recipes for Keycloak (run), Auth0, Okta, Entra ID (documented); Cognito, Google, GitHub not in version 1 | AA6 |
| Q-MCP-12 | Which code validates tokens? | A guard on top of the `auth` package; not the SDK's verifier | AA3 |
| Q-MCP-13 | Which tools? | `list_projects`, `list_features`; snake_case; read-only annotations | [tools.md](tools.md) |
| Q-MCP-14 | What does `list_projects` return? | `project_id`, `name`, `created_at`, newest first; no role (only `owner` exists) | tools.md |
| Q-MCP-15 | What does `list_features` return? | `spec_id`, `name`, stored ontology `attributes`, `total_count`; no embeddings, statements history or provenance; label from the ontology type `feature` | tools.md |
| Q-MCP-16 | How does paging work? | `limit` 1 to 100, default 50 (constants); opaque cursors; features keyed on `spec_id`; projects reuse the REST cursor | tools.md |
| Q-MCP-17 | Where does the paged graph read live? | `GraphReader.count_nodes` and `nodes_page` in `graph.core`, `.cypher` files, `docs/graph` updated | AD4 |
| Q-MCP-18 | How is Postgres read? | Through `db.repositories` only; no new SQL | AD5 |
| Q-MCP-19 | What is the error model? | Tool errors with fixed codes and `isError`; HTTP 401 and 403 per auth.md | tools.md, auth.md |
| Q-MCP-20 | Where does the code live? | `backend/src/api/mcp/` | AD6 |
| Q-MCP-21 | What does the local stack add? | Compose profile `mcp` with Keycloak and the role; realm file without users or secrets; generated secrets in `.env.local` | [deployment.md](deployment.md) |
| Q-MCP-22 | How is it tested as a user would? | Python harness under `backend/tests`: end-to-end tier with the MCP Python SDK and agent tier with the Claude Code CLI, both gated by environment variables and out of CI | [testing.md](testing.md) |
| Q-MCP-23 | How is access audited? | `mcp.tool.completed` and `mcp.auth.rejected` log events; no audit table | architecture.md |
| Q-MCP-24 | Where are rate limits? | At the edge for version 1 | deployment.md |
| Q-MCP-25 | What is the resource URL's path? | Exactly `/mcp`; a dedicated host is recommended | deployment.md |
