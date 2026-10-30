---
type: Questions
title: "Open questions of the MCP server"
description: "Open questions of the MCP server specification, each with the default that applies until it is answered"
status: draft
tags: [mcp]
---
# questions.md: open questions of the MCP server specification

None of these blocks implementation: the specification and the plan follow each default until the question is answered.

| # | Question | Default until answered |
|---|---|---|
| Q2 | Which IdPs besides Keycloak must work at launch? | Keycloak only; the other recipes of [auth.md](auth.md) are checked with the QA harness when a deployment needs them |
| Q3 | Which public host serves the MCP endpoint? | A dedicated host such as `mcp.specflow.example.com` ([deployment.md](deployment.md), P2) |
| Q4 | Does the production identity provider accept a public client with PKCE and the loopback redirects `http://localhost:8765/callback` and `http://127.0.0.1:8765/callback`? Asked to the provider's operators, together with the audience, refresh-token and claim-name details the claim profile needs. | Yes: one pre-registered public client ([auth.md](auth.md), client registration). If not, the provider is brokered through Keycloak, which accepts the loopback redirects, and the design is revisited when the answer arrives |
