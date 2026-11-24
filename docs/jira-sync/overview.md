---
type: Overview
title: "Jira sync overview"
description: "Purpose, scope, actors, non-goals, boundaries, the sync flow and key terms of the automatic export of a project's implementation plan to Jira"
status: stable
tags: [jira, mcp]
---
# overview.md: purpose, scope, actors, non-goals, boundaries, flow and key terms of the Jira sync

## Purpose

Teams track delivery in Jira while Specflow holds the implementation plan. The Jira sync keeps the two aligned without
manual export: once a project owner points a project at a Jira project, every plan run that does not fail pushes the plan
into Jira. The web app is used only to configure the target; agents (and later Slack) run the sync and read its status
over MCP.

In short: one Specflow project is one Epic, each plan story is a Story under it, each task is a Sub-task under its
Story, each work area is a label. Issues nobody has started are kept equal to the plan; issues people work on are never
overwritten, they get one comment per change instead.

## Scope

| In scope | Where |
|---|---|
| Per-project target: Jira project key, optional Epic key, on/off switch, access check | [api.md](api.md#web-routes), [ui.md](ui.md) |
| Automatic sync after each plan run that does not fail | [sync.md](sync.md#triggers-and-runs) |
| Manual sync and status over MCP (`sync_to_jira`, `jira_sync_status`) | [api.md](api.md#agent-routes), [../mcp-server/tools.md](../mcp-server/tools.md#sync_to_jira) |
| Mapping Epic, Story, Sub-task, labels, story points, description, `Blocks` links, rank, remote link | [sync.md](sync.md#mapping) |
| Overwrite in To Do, comment otherwise; removed items labelled, never deleted | [sync.md](sync.md#update-rules) |
| Map of plan node to Jira key with the last applied content | [data-model.md](data-model.md) |
| Crash safety: no duplicate issue after a restart | [sync.md](sync.md#failure-handling) |
| Per-user Atlassian connections (OAuth 2.0 3LO), or one account for the deployment | [oauth.md](oauth.md) |

## Actors

| Actor | Who | Can |
|---|---|---|
| Owner | the Specflow project's owner | set, check and change the target; turn the sync on and off; in `oauth` mode connect their own Atlassian account, which the target's sync then acts as ([oauth.md](oauth.md#acting-account-of-a-target)); everything a member can |
| Member | owner or collaborator | see the Jira tab read-only; run `sync_to_jira` and `jira_sync_status` from a coding agent |
| Plan runner | Specflow itself | start a sync after each plan run that does not fail |
| Deployment admin | whoever runs Specflow | set `ATLASSIAN_SITE_URL` and either the OAuth app (`ATLASSIAN_OAUTH_CLIENT_ID`, `ATLASSIAN_OAUTH_CLIENT_SECRET`, `ATLASSIAN_TOKEN_KEY`) or the one account (`ATLASSIAN_EMAIL`, `ATLASSIAN_API_TOKEN`) ([../configuration.md](../configuration.md#jira-sync)) |
| Jira users | people working in the Jira project | change status, assignee and anything else; Specflow never undoes it |

## Non-goals

- Reading Jira back into Specflow (status, comments, estimates).
- Moving issues through workflow transitions, for example when a story is claimed or completed.
- Sprints, fix versions, Jira Plans dates, components.
- Deleting Jira issues, comments or links Specflow did not create.
- An allow-list of Jira projects.
- A web "Sync now" button or a preview of the changes.
- Confluence as a source (a separate feature; it will reuse the same Atlassian connection).

## Boundaries

| Boundary | Rule |
|---|---|
| Credentials | `token` mode: one Atlassian account per deployment, from the environment only. `oauth` mode: one connection per user, its refresh token encrypted in the database, the access token in memory only ([oauth.md](oauth.md#tokens)). No token reaches a response or a log |
| Process | the sync runs in the API process, like the plan and gap runs; one API replica (the same constraint as those runs) |
| Plan reads | the persisted plan through the plan library's one read (`load_plan`), closed tasks included |
| Jira API | REST v3 and the agile rank endpoint, through one gateway package; no Jira text reaches a log or an API error |
| Transactions | no database transaction is open while a Jira call runs |

## Flow

```mermaid
%%{init: {"theme": "base", "themeVariables": {"background": "#ffffff", "primaryColor": "#e8f1fb", "primaryTextColor": "#0b1f33", "primaryBorderColor": "#1f5f99", "lineColor": "#1f5f99", "actorBkg": "#e8f1fb", "actorBorder": "#1f5f99", "actorTextColor": "#0b1f33", "signalColor": "#0b1f33", "signalTextColor": "#0b1f33", "noteBkgColor": "#fff4cc", "noteTextColor": "#0b1f33", "noteBorderColor": "#b38600", "activationBkgColor": "#d6e6f7"}}}%%
sequenceDiagram
  participant PR as Plan runner
  participant AG as Coding agent (MCP)
  participant RN as Jira sync runner
  participant DB as Postgres
  participant GS as Graph store
  participant JI as Jira Cloud
  PR->>RN: after_plan(project) when the run did not fail
  AG->>RN: sync_to_jira(project)
  RN->>DB: read target, map rows (short transaction)
  RN->>GS: load_plan(project)
  RN->>RN: desired issues, links, order; diff with stored content
  alt nothing changed
    RN->>DB: record succeeded, unchanged = n
  else changes
    RN->>JI: create, update or comment per item (no transaction open)
    RN->>DB: commit key, content, position per item
    RN->>JI: link and rank changes
    RN->>DB: record status and counts
  end
  AG->>RN: jira_sync_status(project)
```

## Key terms

| Term | Meaning |
|---|---|
| Target | the per-project row `jira_targets`: Jira project key, Epic key, enabled, last run |
| Map row | one `jira_issues` row: a plan node, its Jira key and the content last applied |
| Node id | the map key: base task id (the task id without its `_R<n>` revision suffix), story id, or `EPIC` for an Epic Specflow created |
| Owned fields | the fields Specflow sets ([sync.md](sync.md#owned-fields)); everything else belongs to people |
| To Do | an issue whose status category key is `new` (Backlog and To Do statuses) |
| Removed | a mapped node with no live representative in the plan ([sync.md](sync.md#removed-and-revived-items)) |
| Representative | the highest revision of a base task among the plan's tasks |
