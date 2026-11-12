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
| Develop tab flow | The web app's Plan › Develop tab built on the story tools; the story and spec tools ([tools.md](tools.md#story-and-spec-tools)) closed the earlier rows `get_feature` (now `get_node`), Search (`search_spec`, embedded by the API), Impact (`trace`), Other node types (`get_node`, `search_spec`), the next-task tool (`claim_story`) and Write tools |
| Claim leases | A claim lasts until it is completed or released. Leases, heartbeats and expiry come only if forgotten claims become a burden; today a lead frees them in the web app (stale claims, forced release) |
| Merge from CI | A CI step reports the merge of a story's pull request instead of the agent's `complete_story` |
| Story size | Measure story size against pull request size on a real plan before the planner changes how it cuts stories |
| Change feed | Tell an agent when the spec or the plan changes under its claim, instead of `spec_changed` at completion |
| Review state | A review step between the merge and `completed` |
| Agent-added tasks | Agents propose tasks or edit `done_when`; today they record findings and people change the spec |
| MCP Apps tables | The people tools' rows as an MCP Apps view, for clients that render one (the text block is plain JSON) |
| Advertised scopes | Providers that choose the token's audience from the requested scope (Entra ID's `api://<app>/.default`) need the PRM to advertise that scope; a setting added with such a provider, never checked on the token |
| Long-running tools | MCP tasks (the `io.modelcontextprotocol/tasks` extension, built on the SDK's extension mechanism): the tool call answers with a task, each task is a Temporal workflow, and `tasks/get` reports the workflow's status. Claude Code polls tasks from version 2.1.304. Needs a decision on how the public `mcp` process reaches Temporal: its own Temporal credentials, or a call through the internal API |
| Streamed responses | Progress updates and elicitation (questions to the user) during a tool call: the SDK's streamed responses instead of JSON, and ingress that does not buffer `text/event-stream`. Built when a tool needs it |
| FastMCP | Re-evaluated once FastMCP supports `mcp` 2.x: transforms, versioning and per-session visibility for a large tool library; the registry keeps tools independent of the framework |
| OAuth proxy mode | The service acts as authorization server for MCP clients (registration, consent, own tokens) over an upstream IdP; covers IdPs without JWT access tokens or audience control (Google, GitHub, Cognito), providers that issue resource-server tokens only through token exchange, and clients that only do dynamic registration |
| A client id per MCP client type | Separate pre-registered clients (for example one for Claude Code, one for another MCP client), so each type can be revoked and audited on its own; added when a second MCP client is rolled out |
| Dynamic registration and client metadata documents | Accept clients registered at the IdP (RFC 7591) or identified by a metadata URL; replaces the fixed client allowlist with a registration policy |
| Token introspection | RFC 7662 for IdPs that issue opaque access tokens |
| Several IdPs | Issuer stored with the subject in the user key; a data migration |
| End-to-end tier in CI | CI starts Postgres, Neo4j, Keycloak and the role and runs the end-to-end scenarios on every pull request; considered once the stack is stable, weighing CI time and image pulls |
| Service-side rate limits | Per-user limits in the role instead of only at the edge |
| Header binding | The fallback if agents ignore the server instructions: a request header set in the repository's `.mcp.json` carries the project id, and `project_id` becomes optional in the project tools. It changes the tool contract and repeats the binding in each agent's own config (AD7 of [architecture.md](architecture.md#decisions)) |
| `init-specflow` command | One command that adds the server to the agent, signs in and selects a project, so a developer needs no separate setup steps ([deployment.md](deployment.md#developer-setup-in-claude-code)) |
| Guides for Codex and Cursor | Once each is tested end to end with SpecFlow, the Guides page shows its setup ([deployment.md](deployment.md#guides-in-the-web-app)) |
| Codex compatibility check | Verify that Codex loads the server instructions and can use the binding file; version 1 covers Codex by the instructions only, and no test runs it |
| Several projects per workspace | A binding file that holds more than one project, and a rule for choosing among them; needs a new format version |
| Web app on other IdPs | The frontend requests the API audience or scope, so the claim profile serves the web app on Auth0, Okta or Entra ID too |
| Audience from `resource` | Where the IdP sets `aud` from the RFC 8707 `resource` parameter, the per-client audience mapper goes away |
