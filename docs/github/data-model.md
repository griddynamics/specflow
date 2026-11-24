---
type: DataModel
title: "GitHub data model"
description: "The tables github_connections and project_repositories, the migration 0007_github and row-level security"
status: draft
tags: [github]
---
# data-model.md: github_connections, project_repositories, migration 0007, row-level security

## github_connections

One row per Specflow user who connected GitHub (migration `0007_github`).

| Column | Type | Rule |
|---|---|---|
| `user_id` | uuid | primary key; foreign key `users.id` on delete cascade |
| `github_user_id` | bigint | not null |
| `login` | text | not null; the GitHub login, shown to the user only |
| `refresh_token` | bytea | not null; Fernet ciphertext |
| `refresh_expires_at` | timestamptz | null when GitHub did not say |
| `status` | text | `active` or `expired` (check constraint) |
| `connected_at`, `last_used_at` | timestamptz | not null; `last_used_at` again on each refresh |
| `expired_at` | timestamptz | null unless `expired` |

Row-level security is on with no policy and no grant: the agent role reads nothing of it.

## project_repositories

One row per GitHub repository a project owner added.

| Column | Type | Rule |
|---|---|---|
| `id` | uuid | primary key |
| `project_id` | uuid | foreign key `projects.id`; unique with `github_repo_id` |
| `github_repo_id` | bigint | GitHub's repository id (stable across renames) |
| `full_name`, `html_url` | text | not null; as GitHub answered when the owner added it |
| `private` | boolean | not null |
| `added_by` | uuid | foreign key `users.id` on delete set null; the user whose connection every clone check uses |
| `clone_state` | text | `pending`, `cloning`, `cloned`, `failed` (check constraint); default `pending` |
| `failure_code` | text | a clone failure code ([oauth.md](oauth.md#codes)); set exactly when `clone_state` is `failed` |
| `default_branch`, `head_sha` | text | of the last good snapshot |
| `file_count`, `size_bytes` | int, bigint | of the last good snapshot's working tree |
| `snapshot_key`, `snapshot_bytes` | text, bigint | the object key and size of the last good snapshot ([clone.md](clone.md)) |
| `checked_at` | timestamptz | the end of the last check, good or failed |
| `cloned_at` | timestamptz | the end of the last good check; kept after a later failure |
| `created_at`, `updated_at` | timestamptz | not null |

A failed check keeps the last good snapshot and its fields. A member who is removed from the project, or leaves it,
stops being `added_by` of its repositories in the same transaction (`db.members`); the next check then fails with
`github_not_connected` until an owner adds the repository again. Row-level security is on with the policy
`agent_project_scope` of the agent role and no grant, as on every project table.

## Migration

`0007_github` on top of `0006_atlassian_connections` creates both tables and their row-level security; no existing
table changes. The downgrade drops both tables: every connection and project repository is lost (their snapshots stay
in object storage until removed by hand).
