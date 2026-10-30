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
| Q1 | Does production issue offline refresh tokens to `specflow-mcp`? | Yes, with offline sessions limited to 30 days ([auth.md](auth.md), token lifetimes and revocation) |
| Q2 | Which IdPs besides Keycloak must work at launch? | Keycloak only; the other recipes of [auth.md](auth.md) are checked with the QA harness when a deployment needs them |
| Q3 | Which public host serves the MCP endpoint? | A dedicated host such as `mcp.specflow.example.com` ([deployment.md](deployment.md), P2) |
