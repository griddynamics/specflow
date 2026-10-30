---
type: Questions
title: "Open questions of the MCP server"
description: "Open questions of the MCP server specification, each with the default that applies until it is answered"
status: draft
tags: [mcp]
---
# questions.md: open questions of the MCP server specification

The specification follows each default below until its question is answered. An answered question leaves this list,
and its answer is written into the document the row names. None of them blocks implementation.

| # | Question | Default until answered |
|---|---|---|
| Q4 | Does the production identity provider accept a public client with PKCE and the loopback redirects `http://localhost:8765/callback` and `http://127.0.0.1:8765/callback`? Asked to the provider's operators, together with the audience, refresh-token and claim-name details the claim profile needs, and whether its access tokens carry the email. | Yes: one pre-registered public client ([auth.md](auth.md), client registration), with the email in the access token. If loopback redirects are refused, the provider is brokered through Keycloak, which accepts them. If the access token has no email, the domain allowlist and the user row need a userinfo lookup or another matching rule. Either way the design is revisited when the answer arrives |
| Q22 | How is the server tested as a user would use it? | Python only: a QA library and manual entry in `backend/examples`; an end-to-end tier with the MCP Python SDK (scenarios E1 to E13) and an agent tier driving the Claude Code CLI, both gated by environment variables and out of CI ([testing.md](testing.md)) |
| Q23 | How is access audited? | The `mcp.tool.completed` and `mcp.auth.rejected` log events; no audit table ([architecture.md](architecture.md), logging and audit) |
| Q24 | Where are rate limits? | At the edge (suggested 20 requests per second, burst 40); none in the role ([deployment.md](deployment.md), P6) |
| Q25 | What is the public URL of the endpoint? | `MCP_RESOURCE_URL` with the path exactly `/mcp`, on a dedicated host such as `mcp.specflow.example.com` ([architecture.md](architecture.md), settings; [deployment.md](deployment.md), P1 and P2) |
