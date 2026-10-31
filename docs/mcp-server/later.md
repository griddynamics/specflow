---
type: Roadmap
title: "Deferred MCP server features"
description: "Features deferred beyond version 1 of the MCP server"
status: draft
tags: [mcp]
---
# later.md: features deferred beyond version 1 of the MCP server

| Feature | What it adds |
|---|---|
| `get_feature` | The first tool to add when an agent needs more than the feature list: one feature with its module, the requirements in its scope (`GraphReader.scope`, [../graph/api.md](../graph/api.md#read-and-search)) and provenance (document, lines, quote) |
| Search | `GraphReader.find_nodes` hybrid search; the role then needs the LLM gateway for query embeddings ([../graph/api.md](../graph/api.md#llm-gateway)) |
| Impact | `GraphReader.impact` for "what else does this change touch" ([../graph/api.md](../graph/api.md#read-and-search)) |
| Other node types | Modules, capabilities, requirements through the same paged read |
| Plan tasks | Next-task and task detail tools once `graph.plan` exists ([../graph/later.md](../graph/later.md)); the web app's Develop tab flow |
| Write tools | Claiming a task, reporting status; open to every project member like the read tools, with audit records and client confirmation |
| Advertised scopes | Providers that choose the token's audience from the requested scope (Entra ID's `api://<app>/.default`) need the PRM to advertise that scope; a setting added with such a provider, never checked on the token |
| Long-running tools | MCP tasks (the `io.modelcontextprotocol/tasks` extension, built on the SDK's extension mechanism): the tool call answers with a task, each task is a Temporal workflow, and `tasks/get` reports the workflow's status. Claude Code polls tasks from version 2.1.304. Needs a decision on how the public `mcp` process reaches Temporal: its own Temporal credentials, or a call through the internal API |
| Streamed responses | Progress updates and elicitation (questions to the user) during a tool call: the SDK's streamed responses instead of JSON, and ingress that does not buffer `text/event-stream`. Built when a tool needs it |
| FastMCP | Re-evaluated once FastMCP supports `mcp` 2.x: transforms, versioning and per-session visibility for a large tool library; the registry keeps tools independent of the framework |
| OAuth proxy mode | The service acts as authorization server for MCP clients (registration, consent, own tokens) over an upstream IdP; covers IdPs without JWT access tokens or audience control (Google, GitHub, Cognito) and clients that only do dynamic registration |
| A client id per MCP client type | Separate pre-registered clients (for example one for Claude Code, one for another MCP client), so each type can be revoked and audited on its own; added when a second MCP client is rolled out |
| Dynamic registration and client metadata documents | Accept clients registered at the IdP (RFC 7591) or identified by a metadata URL; replaces the fixed client allowlist with a registration policy |
| Token introspection | RFC 7662 for IdPs that issue opaque access tokens |
| Several IdPs | Issuer stored with the subject in the user key; a data migration |
| End-to-end tier in CI | CI starts Postgres, Neo4j, Keycloak and the role and runs the end-to-end scenarios on every pull request; considered once the stack is stable, weighing CI time and image pulls |
| Local Keycloak everywhere | The bundled Keycloak becomes the local OIDC provider for every service and test suite, and dev login goes away; local MCP then uses the real sign-in flow too, and the MCP SDK's built-in token verification becomes possible (decision AA3 of [auth.md](auth.md) is re-evaluated) |
| Service-side rate limits | Per-user limits in the role instead of only at the edge |
| Develop tab | The web app shows the deployment's real `claude mcp add` command and `.mcp.json` |
| Web app on other IdPs | The frontend requests the API audience or scope, so the claim profile serves the web app on Auth0, Okta or Entra ID too |
| Audience from `resource` | Where the IdP sets `aud` from the RFC 8707 `resource` parameter, the per-client audience mapper goes away |
