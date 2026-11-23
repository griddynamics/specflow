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
| `DELETE /integrations/github` | - | 204, also without a connection | - |
| `GET /integrations/github/repositories` | - | 200 `{repositories: GitHubRepositoryOut[], truncated}` (at most 1000) | 409 `github_not_connected`; 409 `github_reconnect_required`; 502 `github_unreachable`; 502 `github_auth_failed` |

`GitHubConnectionOut`: `enabled`, `connected`, `status` (`active`, `expired` or null), `login`, `connected_at`,
`last_used_at`, `manage_url` (`https://github.com/settings/installations` when enabled).

`GitHubRepositoryOut`: `github_repo_id`, `full_name`, `html_url`, `private`, `default_branch`, `size_kb`, `too_large`
(`size_kb` over 1 GiB).

## Project repository routes

A member reads; only the owner (403 `owner_required` otherwise) writes. A non-member and a missing project are 404
`project_not_found`; a malformed or unknown repository id is 404 `repository_not_found`.

| Method, path | Body | Success | Errors |
|---|---|---|---|
| `GET /projects/{pid}/repositories` | - | 200 `{enabled, can_manage, repositories: ProjectRepositoryOut[]}` | - |
| `POST /projects/{pid}/repositories` | `{github_repo_id}` (integer, 1 or more) | 201 `ProjectRepositoryOut`; the clone check starts | 409 `github_not_connected`; 409 `github_reconnect_required`; 404 `github_repo_not_accessible` (not in the caller's list); 422 `github_repo_too_large`; 409 `repository_exists`; 502 `github_unreachable` / `github_auth_failed` |
| `DELETE /projects/{pid}/repositories/{rid}` | - | 204; the snapshots under its prefix are deleted (a failure there is only logged) | 404 `repository_not_found` |
| `POST /projects/{pid}/repositories/{rid}/clone` | - | 202 `ProjectRepositoryOut` (`pending`, or `cloning` when a check runs) | 404 `repository_not_found` |

The add route checks the repository against the caller's own list (`GET /user/installations` and the repositories of
each installation, with the caller's token), so an owner can add only a repository they read through the app. A
workflow start that fails marks the row `failed` with `github_clone_failed` (log `api.github.snapshot.start_failed`);
the owner asks again with `POST …/clone`.

`ProjectRepositoryOut`: `id`, `github_repo_id`, `full_name`, `html_url`, `private`, `added_by_name` (the Specflow display
name, null once that user is gone), `added_by_me`, `clone_state`, `failure_code`, `default_branch`, `head_sha`,
`file_count`, `size_bytes`, `snapshot_bytes`, `checked_at`, `cloned_at`, `created_at`.

## Log events

`api.github.add_refused` (`project_id`, `code`), `api.github.repository_added` (`project_id`, `repository_id`),
`api.github.repository_removed` (`repository_id`, `objects`), `api.github.snapshot.started` /
`api.github.snapshot.start_failed` / `api.github.snapshot.delete_failed` (`repository_id`, `error` type). Ids and codes
only.
