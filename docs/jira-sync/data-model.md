---
type: DataModel
title: "Jira sync data model"
description: "The tables jira_targets, jira_issues and atlassian_connections, the canonical content Specflow stores per issue, the migrations 0005_jira_sync and 0006_atlassian_connections and row-level security"
status: stable
tags: [jira]
---
# data-model.md: jira_targets, jira_issues, atlassian_connections, canonical content, migrations 0005 and 0006, row-level security

In `token` mode no table holds a credential: the one Atlassian account lives in the environment only
([../configuration.md](../configuration.md#jira-sync)). In `oauth` mode `atlassian_connections` holds each user's refresh
token, Fernet-encrypted with `ATLASSIAN_TOKEN_KEY`; access tokens are never stored ([oauth.md](oauth.md#data)).

## Targets

`jira_targets`: one row per Specflow project that has Jira settings.

| Column | Type | Rule |
|---|---|---|
| `project_id` | uuid | primary key; foreign key to `projects.id` |
| `jira_project` | text | not null; the Jira project key, `^[A-Z][A-Z0-9_]{1,9}$` |
| `epic_key` | text | null = Specflow creates the Epic; else `^[A-Z][A-Z0-9_]{1,9}-[1-9][0-9]*$` and its prefix equals `jira_project` |
| `enabled` | boolean | not null |
| `updated_at` | timestamptz | not null, set on every change of the three columns above |
| `last_status` | text | null = never ran; else one of `running`, `succeeded`, `partial`, `failed`, `skipped` (check constraint) |
| `last_trigger` | text | null or one of `plan_run`, `mcp`, `settings`, `recovery` (check constraint) |
| `last_started_at`, `last_finished_at` | timestamptz | null until a pass is queued or ends (`last_started_at` is set when the pass is queued) |
| `last_error_code` | text | a code of [api.md](api.md#sync-codes), else null |
| `last_counts` | jsonb | null until a pass ends; the counts object of [sync.md](sync.md#triggers-and-runs) |
| `connected_user_id` | uuid | null; foreign key to `users.id` on delete set null; the owner who last saved the target, whose Atlassian connection the sync acts as in `oauth` mode (ignored in `token` mode); set to null when that user is removed from the project or leaves it; added by 0006 |

The key formats are checked by the API ([api.md](api.md#web-routes)); the table keeps them as text.

## Issues

`jira_issues`: the map of plan nodes to Jira issues, one row per node Specflow created an issue for (or is creating).

| Column | Type | Rule |
|---|---|---|
| `project_id` | uuid | part of the primary key; foreign key to `projects.id` |
| `node_id` | text | part of the primary key; base task id, story id or `EPIC` ([sync.md](sync.md#mapping)) |
| `kind` | text | `epic`, `story` or `subtask` (check constraint) |
| `jira_key` | text | null while pending ([sync.md](sync.md#failure-handling)); unique per project when set |
| `requested_at` | timestamptz | not null; when the create was requested |
| `content` | jsonb | null until applied; the [canonical content](#canonical-content) last applied (a new issue is stored with an empty `blocked_by` first, [sync.md](sync.md#links)) |
| `content_hash` | text | null with `content`; sha256 hex of the canonical content |
| `position` | integer | null; the item's index in the new order of its rank scope at the last pass, also for an issue that rank left in place ([sync.md](sync.md#rank)) |
| `removed_at` | timestamptz | null; set when the item was first seen removed or deleted in Jira, cleared on revival |
| `error_code` | text | null; the item error of the last pass that touched it; `jira_not_found` with `removed_at` = deleted in Jira, never called again ([sync.md](sync.md#removed-and-revived-items)) |
| `synced_at` | timestamptz | null; the last time Specflow applied or confirmed the row |

Indexes: primary key (`project_id`, `node_id`); unique (`project_id`, `jira_key`) where `jira_key` is not null.

Rows are deleted in one case only: the owner changes `jira_project`; then every row of the project goes in one
statement, in the same transaction as the settings change, after that transaction locked the target row (the old
issues stay in Jira, untouched). Every map-row write of a pass locks the target row for share first, so a pass never
writes a row of the old Jira project after the change ([sync.md](sync.md#target-change-during-a-pass)).

## Canonical content

The JSON stored in `content`, written with sorted keys and no spaces; `content_hash` is the sha256 of exactly those
bytes. The constant `MAPPING_VERSION` (starting at 1) is part of it, so a layout change re-applies every To Do issue
and posts no comment ([sync.md](sync.md#update-rules)).

| Key | Epic (created) | Story | Sub-task |
|---|---|---|---|
| `v` | `MAPPING_VERSION` | same | same |
| `summary` | project name | story title | task title (each as sent: [field limits](sync.md#owned-fields)) |
| `text` | null | story summary or null | task description or null (as sent) |
| `release` | null | story release or null | null |
| `phase` | null | null | task phase or null |
| `done_when` | `[]` | `[]` | the `done_when` texts in plan order |
| `specs` | `[]` | `[{id, name}]` sorted by id | same |
| `task_id` | null | null | the representative's task id |
| `points` | null | sum or null | 1, 3, 5 or null |
| `labels` | `["specflow"]` | sorted owned labels | sorted owned labels |
| `parent` | null | the Epic's Jira key | the Story's Jira key |
| `blocked_by` | `[]` | sorted story node ids | sorted base task ids |

`specflow-removed` is state (`removed_at`), not content. Status, assignee and rank are not content.

## Migration

Revision `0005_jira_sync`, `down_revision` `0004_gap_assignment`, hand-written like 0004:

| # | Step |
|---|---|
| M1 | `SET LOCAL lock_timeout = '10s'` first (the statement file of 0003) |
| M2 | create `jira_targets` with its check constraints |
| M3 | create `jira_issues` with its check constraint, the primary key and the partial unique index on `jira_key` |
| M4 | row-level security and the policy `agent_project_scope` of the agent role on both tables, reusing the 0003 statement files with the table name replaced; no grant to that role |

`downgrade()` drops both tables (data loss: every Jira target and the whole map; the issues in Jira stay). No existing
table changes.

Revision `0006_atlassian_connections`, `down_revision` `0005_jira_sync`:

| # | Step |
|---|---|
| M5 | `SET LOCAL lock_timeout = '10s'` first |
| M6 | create `atlassian_connections` ([oauth.md](oauth.md#data)): primary key `user_id` (foreign key `users.id` on delete cascade), the check constraint on `status` |
| M7 | row-level security on `atlassian_connections` with no policy (the 0003 statement file); no grant to the agent role |
| M8 | add `jira_targets.connected_user_id` with its foreign key to `users.id` on delete set null |

`downgrade()` drops the column and the table: every connection is lost (each user connects again) and so is the acting
account of every target (the owners save their targets again).

## Row-level security

As every project table since 0003: `jira_targets` and `jira_issues` enable row-level security with the
`agent_project_scope` policy for the agent role, and the agent role gets no grant. `atlassian_connections` enables
row-level security with no policy at all, so the agent role reads none of it. The API reads and writes all three with
the application role only.
