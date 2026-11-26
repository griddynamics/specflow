---
type: DataModel
title: "Linear data model"
description: "The tables linear_connections, linear_targets, linear_issues, linear_gap_targets and linear_gap_issues, the migration 0011_linear, row-level security and the widened app and source kinds"
status: draft
tags: [linear]
---
# data-model.md: the five Linear tables, migration 0011, row-level security

Migration `backend/migrations/versions/0011_linear.py` (on top of `0010_github_projects`); models in
`backend/src/db/models.py`. The plan says to renumber the migration at rebase if the Notion branch merges first.

## linear_connections

One row per Graphico user who connected Linear. Not project scoped.

| Column | Type | Rule |
|---|---|---|
| `user_id` | uuid | primary key; foreign key `users.id` on delete cascade |
| `linear_user_id` | text | not null; the Linear user id |
| `linear_user_name` | text | not null; shown to the user only |
| `organization_id`, `organization_name`, `organization_url_key` | text | not null; the Linear workspace (`url_key` is the first part of an issue link) |
| `scopes` | text | not null; as Linear granted them (`read,write`) |
| `access_token` | bytea | not null; Fernet ciphertext |
| `refresh_token` | bytea | not null; Fernet ciphertext; rotated on every refresh |
| `expires_at` | timestamptz | not null; when the access token expires |
| `status` | text | `active`, `expired` or `revoked` (check `ck_linear_connections_status`) |
| `connected_at`, `last_used_at` | timestamptz | not null; `last_used_at` again on each refresh |
| `expired_at` | timestamptz | null unless the connection expired |

Index `ix_linear_connections_linear_user_id` on `(organization_id, linear_user_id)`: a webhook comment is mapped to a
member by the Linear user id of the workspace ([gaps.md](gaps.md#answers-one-path-for-the-webhook-and-the-catch-up)).
The index is not unique: two Graphico users can connect the same Linear account. The webhook lookup is limited to the
members of the project and takes the first match by user id, skipping `revoked` rows (`db/linear_gaps.py` `member_of`);
an `expired` row still matches. Row-level security is on with **no policy and no
grant**: the agent role reads nothing of it.

## linear_targets

The Linear plan destination of a project and its last pass. One row per project.

| Column | Type | Rule |
|---|---|---|
| `project_id` | uuid | primary key; foreign key `projects.id` |
| `team_id`, `team_key` | text | not null; the Linear team |
| `linear_project_id`, `linear_project_name` | text | not null; the Linear project the issues go to |
| `linear_project_url` | text | null when Linear gave none |
| `created_by_graphico` | boolean | not null; true when Graphico made the Linear project |
| `enabled` | boolean | not null |
| `updated_at` | timestamptz | not null |
| `last_status` | text | `running`, `succeeded`, `partial`, `failed`, `skipped` (check) |
| `last_trigger` | text | `plan_run`, `mcp`, `settings`, `recovery` (check) |
| `last_started_at`, `last_finished_at` | timestamptz | the last pass |
| `last_error_code` | text | a closed code of the last pass ([plan.md](plan.md#codes)) |
| `last_counts` | jsonb | the counts of the last pass |

The member the export acts as is **not** stored here: it is read from `project_integrations` at the start of every
pass. Holds no credential.

## linear_issues

The map of plan nodes to Linear issues. One row per plan node. A story is an issue; a task is a sub-issue.

| Column | Type | Rule |
|---|---|---|
| `project_id`, `node_id` | uuid, text | primary key (together); `project_id` foreign key `projects.id` |
| `kind` | text | `story` or `subtask` (check) |
| `issue_id` | uuid | null on a pending row: a create about to be sent |
| `identifier` | text | the Linear identifier (`ENG-12`); unique with `project_id` |
| `requested_at` | timestamptz | not null; when the pending row was made |
| `content`, `content_hash` | jsonb, text | the content applied last; the hash decides "changed"; `content.blocked_by` lists the blockers already linked |
| `position` | integer | **not written by the pass** (see [plan.md](plan.md#rules-of-a-pass)) |
| `mirrored_state` | text | the name of the last workflow state the task status mirror set ([plan.md](plan.md#task-status-mirror)) |
| `removed_at` | timestamptz | set when the item left the plan or a person deleted the issue |
| `error_code` | text | `linear_issue_gone`, `linear_estimate_refused`, or the code of a failed item |
| `synced_at` | timestamptz | the last write to the issue |

## linear_gap_targets

The Linear gap channel of a project. One row per project.

| Column | Type | Rule |
|---|---|---|
| `project_id` | uuid | primary key; foreign key `projects.id` |
| `team_id`, `team_key` | text | not null |
| `linear_project_id` | text | null = the team only |
| `grouping` | text | `one` or `area` (check) |
| `parent_issue_id`, `parent_issue_identifier` | uuid, text | the "Open questions" issue, set when it is created |
| `enabled` | boolean | not null |
| `updated_at` | timestamptz | not null |
| `last_error_code` | text | the closed code of the last refused post or read |

## linear_gap_issues

The map of gaps to the issues that ask them. One row per posted gap; the row outlives its answer, so a gap is posted
once. Holds no Linear text.

| Column | Type | Rule |
|---|---|---|
| `project_id`, `gap_id` | uuid, text | primary key (together); `project_id` foreign key `projects.id` |
| `issue_id` | uuid | not null; the issue that asks the gap |
| `identifier` | text | not null |
| `area` | text | not null; the work area the question sits under ("General" when none) |
| `number` | integer | not null; the question's number in its issue |
| `posted_at` | timestamptz | not null |
| `answered_at` | timestamptz | null until the question is answered |

Index `ix_linear_gap_issues_issue_id` on `issue_id`: a webhook delivery names the issue only.

## Row-level security

On `linear_connections`: on, no policy, no grant. On the four project tables (`linear_targets`, `linear_issues`,
`linear_gap_targets`, `linear_gap_issues`): on, with the policy `agent_project_scope` of the agent role and no grant, as
on every project table (the statements of 0003 are reused).

## Widened checks

| Check | Before | After |
|---|---|---|
| `ck_project_integrations_app` (`INTEGRATION_APPS`) | `atlassian`, `github`, `slack` | adds `linear` |
| `ck_connections_kind` (`CONNECTION_KINDS`) | `gdrive`, `upload`, `confluence`, `jira_epic` | adds `linear_project` |

The same lists are in `db/models.py` (`INTEGRATION_APPS`, `CONNECTION_KINDS`) and in
`frontend/lib/domain/integrations.ts`.

## Migration

`0011_linear` creates the five tables, their indexes and row-level security, and widens both checks. Its first
statement sets `lock_timeout` to 10 s; if a lock on `projects`, `users`, `project_integrations` or `connections` is not
granted in time, the whole transaction rolls back and the next api start retries it.

The downgrade returns to the `0010_github_projects` schema: it deletes the `linear_project` sources with their items and
documents, deletes the `linear` integrations, narrows both checks and drops the five tables. Every Linear connection,
target and map is lost; the issues in Linear stay.
