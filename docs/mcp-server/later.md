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
| `get_feature` | One feature with its module, the requirements in its scope (`GraphReader.scope`) and provenance (document, lines, quote) |
| Search | `GraphReader.find_nodes` hybrid search; the role then needs the LLM gateway for query embeddings |
| Impact | `GraphReader.impact` for "what else does this change touch" |
| Other node types | Modules, capabilities, requirements through the same paged read |
| Plan tasks | Next-task and task detail tools once `graph.plan` exists; the web app's Develop tab flow |
| Write tools | Claiming a task, reporting status; declared with `specflow:write` in the tool registry, with audit records and client confirmation |
| Long-running tools | MCP tasks (the `io.modelcontextprotocol/tasks` extension, built on the SDK's extension mechanism): the tool call answers with a task, each task is a Temporal workflow, and `tasks/get` reports the workflow's status. Claude Code polls tasks from version 2.1.304. Needs a decision on how the public `mcp` process reaches Temporal: its own Temporal credentials, or a call through the internal API |
| FastMCP | Re-evaluated once FastMCP supports `mcp` 2.x: transforms, versioning and per-session visibility for a large tool library; the registry keeps tools independent of the framework |
| OAuth proxy mode | The service acts as authorization server for MCP clients (registration, consent, own tokens) over an upstream IdP; covers IdPs without JWT access tokens or audience control (Google, GitHub, Cognito) and clients that only do dynamic registration |
| Dynamic registration and client metadata documents | Accept clients registered at the IdP (RFC 7591) or identified by a metadata URL; replaces the fixed client allowlist with a registration policy |
| Token introspection | RFC 7662 for IdPs that issue opaque access tokens |
| Several IdPs | Issuer stored with the subject in the user key; a data migration |
| Service-side rate limits | Per-user limits in the role instead of only at the edge |
| Develop tab | The web app shows the deployment's real `claude mcp add` command and `.mcp.json` |
| Web app on other IdPs | The frontend requests the API audience or scope, so the claim profile serves the web app on Auth0, Okta or Entra ID too |
| Audience from `resource` | Where the IdP sets `aud` from the RFC 8707 `resource` parameter, the per-client audience mapper goes away |
