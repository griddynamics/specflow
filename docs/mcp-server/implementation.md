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
| MCP-M2 | Auth claim profile for any OIDC provider | planned |
| MCP-M3 | Local Keycloak and QA harness | planned |
| MCP-M4 | MCP role, transport and auth guard | planned |
| MCP-M5 | Tools and access control | planned |
| MCP-M6 | Local stack wiring and setup docs | planned |
| MCP-M7 | Real-client QA with the end-to-end and agent tiers | planned |
| MCP-M8 | Bundle verification, security review and release check | planned |

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
