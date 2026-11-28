---
type: API
title: "GitHub routes"
description: "The caller's GitHub connection routes and the project repository routes: methods, bodies, shapes and error codes"
status: draft
tags: [github, api]
---
# api.md: GitHub connection routes and project repository routes

Under `/api/v1`, for the signed-in web user; request bodies forbid extra fields. With the integration off every route
below answers 503 `github_unavailable`, except `GET /integrations/github` (`enabled` false) and
`GET /projects/{pid}/repositories` (`enabled` false, an empty list).

## Connection routes

| Method, path | Body | Success | Errors |
|---|---|---|---|
| `GET /integrations/github` | - | 200 `GitHubConnectionOut` | - |
| `POST /integrations/github/authorize` | `{return_to}` | 200 `{url}` | 422 `validation_failed` |
| `POST /integrations/github/callback` | `{code, state}` (1 to 4096 characters) | 200 `{connection, return_to}` | 400 `github_state_invalid`; 400 `github_code_invalid`; 502 `github_unreachable` |
| `POST /integrations/github/install` | `{return_to}` | 200 `{url}` | 422 `validation_failed` |
| `POST /integrations/github/setup` | `{state}` | 200 `{return_to}` | 400 `github_state_invalid` |
| `GET /integrations/github/repositories` | - | 200 `{repositories: GitHubRepositoryOut[], truncated}` (at most 1000) | 409 `github_not_connected`; 409 `github_reconnect_required`; 502 `github_unreachable`; 502 `github_auth_failed` |

The per-user `DELETE /integrations/github` went with the V1 screens; removing the app from a project is
`DELETE /projects/{pid}/integrations/github` ([integrations-api.md](../mission-control/integrations-api.md)).

`GitHubConnectionOut`: `enabled`, `connected`, `status` (`active`, `expired` or null), `login`, `connected_at`,
`last_used_at`, `manage_url` (`https://github.com/settings/installations` when enabled).

`GitHubRepositoryOut`: `github_repo_id`, `full_name`, `html_url`, `private`, `default_branch`, `size_kb`, `too_large`
(`size_kb` over 1 GiB).

## Project repository routes

Any member reads and writes (D6; a collaborator no longer gets 403 `owner_required`). A non-member and a missing project are 404
`project_not_found`; a malformed or unknown repository id is 404 `repository_not_found`.

| Method, path | Body | Success | Errors |
|---|---|---|---|
| `GET /projects/{pid}/repositories` | - | 200 `{enabled, can_manage, repositories: ProjectRepositoryOut[]}` | - |
| `POST /projects/{pid}/repositories` | `{github_repo_id}` (integer, 1 or more) | 201 `ProjectRepositoryOut`; the clone check starts | 409 `github_not_connected` (the project has no GitHub connection bound to a member); 409 `github_reconnect_required`; 404 `github_repo_not_accessible` (not in the list of the bound member); 422 `github_repo_too_large`; 409 `repository_exists`; 502 `github_unreachable` / `github_auth_failed` |
| `DELETE /projects/{pid}/repositories/{rid}` | - | 204; the snapshots under its prefix are deleted (a failure there is only logged) | 404 `repository_not_found` |

Any member reads, adds and removes (D6; the owner-only rule of 0007 is gone). The add route checks the
repository against the list of the member the project's GitHub connection is bound to (the `github` row of
`project_integrations`; `GET /user/installations` and the repositories of each installation, with that member's
token), so only a repository the bound member reads through the app can be added, and the row's `added_by` is that
member, not the caller. `can_manage` is true for every member. A workflow start that fails marks the row `failed` with
`github_clone_failed` (log `api.github.snapshot.start_failed`); the re-check route went with the V1 screens, and a member
removes the repository and adds it again (the add starts the clone check).

`ProjectRepositoryOut`: `id`, `github_repo_id`, `full_name`, `html_url`, `private`, `added_by_name` (the Specflow display
name, null once that user is gone), `added_by_me`, `clone_state`, `failure_code`, `default_branch`, `head_sha`,
`file_count`, `size_bytes`, `snapshot_bytes`, `checked_at`, `cloned_at`, `created_at`.

## Log events

`api.github.add_refused` (`project_id`, `code`), `api.github.repository_added` (`project_id`, `repository_id`),
`api.github.repository_removed` (`repository_id`, `objects`), `api.github.snapshot.started` /
`api.github.snapshot.start_failed` / `api.github.snapshot.delete_failed` (`repository_id`, `error` type). Ids and codes
only.
