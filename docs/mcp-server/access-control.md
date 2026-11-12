---
type: Design
title: "MCP server access control"
description: "Project membership as the single access decision, tenant isolation in Postgres and Neo4j, the rules of the write tools, untrusted content and audit"
status: stable
tags: [mcp, auth]
---
# access-control.md: project membership, tenant isolation, write tools, untrusted content and audit

## Rule

A caller may read a project when, and only when, a `project_members` row links the caller's user row to that project.
Every member has the same access to every tool: no role or scope distinguishes members (`owner` is the only role).
The rule is the one the REST API applies; the API applies it on the agent routes that serve the tools
([architecture.md](architecture.md#api-agent-routes)), through the same repository functions.

## Decision point

| # | Rule |
|---|---|
| AC1 | Every agent route resolves the caller first: `upsert_user(principal)` provisions or refreshes the user row (same function as REST). |
| AC2 | Every agent route that takes a `project_id` calls one membership function before any other read. The function wraps `db.repositories.get_member_project`; there is no second membership query. |
| AC3 | A project that does not exist, a malformed id and a project the caller is not a member of give the same `project_not_found` result, with the same message and no timing-relevant extra work. This includes an existing project whose valid id the caller obtained some other way: the id alone grants nothing. |
| AC4 | When membership is denied, no Neo4j query runs. |
| AC5 | `list_projects` lists only member projects (`db.repositories.list_projects`). |
| AC6 | Membership is read on every call; nothing caches it. Removing a member takes effect on the next call. |
| AC7 | A tool never accepts Cypher, labels, attribute filters, sort keys or another user's id from the caller. The node type of `list_features` is fixed by the tool. |
| AC8 | The workspace binding grants nothing. The `project_id` that an agent reads from a binding file is an ordinary argument: membership is checked on every call (AC2, AC6). |

The agent routes apply the membership function, in their dependency chain, to every tool declared as project-scoped
([architecture.md](architecture.md#api-agent-routes)). A structural test reads the declarations and the agent routes and
checks that every tool with a `project_id` argument is declared project-scoped and maps to a route that runs the check,
so none reaches the graph reader without it.

## Tenant isolation

| Store | Isolation | Enforced by |
|---|---|---|
| Postgres: users, projects, members | membership predicate in the repository query | `db.repositories` (shared with REST) |
| Neo4j: project graph | `group_id = $project_id` in every scoped query ([../graph/data-model.md](../graph/data-model.md#partition)); the executor refuses a scoped query without `$project_id` and injects the value itself | `graph.core` executor ([../graph/architecture.md](../graph/architecture.md#import-rules), rule I3) |
| Postgres: project-scoped tables (documents, sources), when a later tool reads them | the row-level-security agent role, scoped to the project after the membership check | `db.agent_scope` |

Neo4j has no per-user or per-project permissions: every graph read uses the service's own credentials, and the
database would return any project's nodes to any query. The membership check in code (AC2) is therefore the only guard
in front of the graph, and it runs before every project-scoped read; the executor's `group_id` predicate then keeps
the read inside that one project. The project id given to the graph reader is the id the membership check returned
(`str(project.id)`, the canonical lower-case UUID that is the graph's `group_id`), never the raw argument: the graph's
project id check accepts other spellings of the same UUID, which would pass membership and read an empty graph.

## Identity mapping

The `Principal` subject is looked up in `users.auth_subject`. The web app and the MCP server must therefore use the
same issuer and the same subject claim ([auth.md](auth.md)); a developer who has only ever used MCP gets a user row and
no projects until they create one in the web app (only a project's creator is its member today).

## Write tools

`claim_story`, `release_story`, `add_finding`, `report_task` and `complete_story` ([tools.md](tools.md#story-and-spec-tools))
write as the caller. Membership stays the only access decision: every member may claim, report, complete and add
findings in a member project, as every member may read it.

| # | Rule |
|---|---|
| AC9 | The writer is the token's user. No write tool takes a user argument; `claimed_by`, `completed_by` and `created_by` hold that user's id, never an email or a name. |
| AC10 | A claim is held by one user. Releasing, reporting progress, completing and a blocking finding need the claim token that `claim_story` returned to that user; another member's token, or a stale one, answers `claim_mismatch` and writes nothing, except the repeats of [Repeats](tools.md#repeats). |
| AC11 | A blocking finding blocks only the caller's own claimed story, and only through a node a task of that story implements. |
| AC12 | Only people resolve or dismiss findings and force a release, in the web app; no tool does either. The web routes apply the membership every web route applies: any member of the project may, and there is no role ([../graph/gap.md](../graph/gap.md#web-routes)). |
| AC13 | No write tool changes a spec node or edge, and nothing a finding says is read by a build or the planner. |
| AC14 | A refused write changes nothing: the membership check, the token check and the argument checks run before the write, in one locked transaction per story. |

| Threat | Example | Mitigation |
|---|---|---|
| Spoofing | an agent acts as another member | AC9: identity from the token only |
| Tampering | a stale or copied token releases or completes another member's story | AC10; the story keeps its last token only to answer the holder's own repeat |
| Tampering | a finding blocks other people's work | AC11 |
| Repudiation | who claimed, completed or wrote what | user ids on the graph nodes, `api.agent.written` and `mcp.tool.completed` joined by one request id |
| Information disclosure | names, emails, tokens reach other agents | holders carry a display name or null, never an id or email; the token goes to its holder only; finding text, notes, labels and names are never logged |
| Information disclosure | a `refers_to` link points at a private host | references are stored as given and never fetched; URLs without user information |
| Denial of service | an agent floods findings | at most 50 open findings per caller and story (`finding_limit`); the length limits of [tools.md](tools.md#add_finding) |
| Elevation of privilege | an agent changes the spec through a finding | AC13 |

## Untrusted content

Feature names, statements and quotes come from documents that project members uploaded. They reach the developer's
agent as tool output and may contain text that reads like instructions. Mitigations:

- The read tools are read-only. The write tools change plan state and findings only, never a spec node or edge
  ([Write tools](#write-tools)); a write needs the agent's own tool call with the caller's token, so no tool output
  can make the server write anything.
- Content is returned in named JSON fields, never mixed into server-authored instructions or tool descriptions.
- Tool descriptions tell the model that field values are project data, not instructions.
- The binding file is data from the user's repository, and a committed file can come from another person. The file and
  its `project_name` are untrusted data, never instructions. A committed file that names a project the reader is not a
  member of answers `project_not_found`, like any foreign id (AC3); the agent does not fall back to another project
  ([tools.md](tools.md#rules)).
- The server instructions are server-authored text built from `MCP_RESOURCE_URL`. They hold no project data and no
  user data ([tools.md](tools.md#server-instructions)).
- What the agent does next is governed by the client's own permission model (Claude Code asks before running tools
  and commands).

## Data returned

| Data | Returned | Note |
|---|---|---|
| Project id, name, creation time | yes | member projects only |
| Other members, emails, user ids | no | |
| Feature `spec_id`, name, stored attributes (`stmt`, `quote`) | yes | `quote` is an excerpt of a project document ([../graph/data-model.md](../graph/data-model.md#spec-node)) |
| Embeddings, statements history, provenance, document paths | no | not needed for listing; a later detail tool may add provenance ([../graph/data-model.md](../graph/data-model.md#spec-node)) |
| Story and spec tools: plan state, spec nodes with statements and provenance, source lines, findings | yes | member projects only; never vectors |
| Holder names | yes | the display name of a member who holds, completed or wrote something; null when it is the email or the user left the project |
| Claim token | to its holder only | never shown in tables, error details or other users' answers |

## Audit

`mcp.tool.completed` records every call and `mcp.auth.rejected` every refused request; the fields are those of
[architecture.md](architecture.md#logging-and-audit); the user id comes from the API, and the API's log lines of the call
carry the same request id. Together they answer who read which project, and when. Writes add `api.agent.written`
in the API, and the graph keeps who claimed, completed or wrote what ([architecture.md](architecture.md#logging-and-audit)).
