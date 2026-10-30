---
type: Design
title: "MCP server access control"
description: "Project membership as the single access decision, tenant isolation in Postgres and Neo4j, untrusted content and audit"
status: draft
tags: [mcp, auth]
---
# access-control.md: project membership, tenant isolation, untrusted content and audit

## Rule

A caller may read a project when, and only when, a `project_members` row links the caller's user row to that project.
Role does not matter in version 1 (`owner` is the only role). The rule is the one the REST API applies; the MCP server
applies it through the same repository functions.

## Decision point

| # | Rule |
|---|---|
| AC1 | Every tool resolves the caller first: `upsert_user(principal)` provisions or refreshes the user row (same function as REST). |
| AC2 | Every tool that takes a `project_id` calls one membership function before any other read. The function wraps `db.repositories.get_member_project`; there is no second membership query. |
| AC3 | A project that does not exist, a malformed id and a project the caller is not a member of give the same `project_not_found` result, with the same message and no timing-relevant extra work. |
| AC4 | When membership is denied, no Neo4j query runs. |
| AC5 | `list_projects` lists only member projects (`db.repositories.list_projects`). |
| AC6 | Membership is read on every call; nothing caches it. Removing a member takes effect on the next call. |
| AC7 | A tool never accepts Cypher, labels, attribute filters, sort keys or another user's id from the caller. The node type of `list_features` is fixed by the tool. |

A structural test lists every registered tool and checks that each tool with a `project_id` argument goes through the
membership function before reaching the graph reader.

## Tenant isolation

| Store | Isolation | Enforced by |
|---|---|---|
| Postgres: users, projects, members | membership predicate in the repository query | `db.repositories` (shared with REST) |
| Neo4j: project graph | `group_id = $project_id` in every scoped query; the executor refuses a scoped query without `$project_id` and injects the value itself | `graph.core` executor |
| Postgres: project-scoped tables (documents, sources), when a later tool reads them | the row-level-security agent role, scoped to the project after the membership check | `db.agent_scope` |

The project id given to the graph reader is the id the membership check returned, never the raw argument.

## Identity mapping

The `Principal` subject is looked up in `users.auth_subject`. The web app and the MCP server must therefore use the
same issuer and the same subject claim ([auth.md](auth.md)); a developer who has only ever used MCP gets a user row and
no projects until someone adds them to a project.

## Untrusted content

Feature names, statements and quotes come from documents that project members uploaded. They reach the developer's
agent as tool output and may contain text that reads like instructions. Mitigations:

- Every tool is read-only; no tool output can make the server write anything.
- Content is returned in named JSON fields, never mixed into server-authored instructions or tool descriptions.
- Tool descriptions tell the model that field values are project data, not instructions.
- What the agent does next is governed by the client's own permission model (Claude Code asks before running tools
  and commands).

## Data returned

| Data | Returned | Note |
|---|---|---|
| Project id, name, creation time | yes | member projects only |
| Other members, emails, user ids | no | |
| Feature `spec_id`, name, stored attributes (`stmt`, `quote`) | yes | `quote` is an excerpt of at most 300 characters of a project document |
| Embeddings, statements history, provenance, document paths | no | not needed for listing; a later detail tool may add provenance |

## Audit

`mcp.tool.completed` records user id, tool, project id and outcome for every call; `mcp.auth.rejected` records every
refused request ([architecture.md](architecture.md)). Together they answer who read which project, and when.
