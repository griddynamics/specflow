---
type: Design
title: "GitHub Projects as a plan destination"
description: "How a project's plan is exported to an organization project of GitHub: target, mapping, rules, passes, permissions, routes, codes, limits"
status: draft
tags: [github, plan, mission-control]
---
# projects.md: the GitHub Projects plan destination

A project can send its plan to one repository and one organization project of GitHub as issues. It mirrors the Jira plan sync
([../jira-sync/sync.md](../jira-sync/sync.md)): the same ownership rules, the same "never delete", the same one pass per
project. It runs on the lifted export code (`api.exports`: runner, digest, rank planner, run status), reads the plan the way the
Jira sync does (`api.jira.desired.build`, a pure function) and changes neither the plan runner nor any Jira table. Routes and
shapes for the canvas form: [../mission-control/integrations-api.md](../mission-control/integrations-api.md) "GitHub Projects".

## Scope

Organization projects of the project's one GitHub organization only (the organization that owns the repository). A GitHub App
cannot reach a user's Projects v2, so a repository owned by a user is refused (`github_not_an_organization`). The export acts as the
member the project's GitHub is connected by (`PUT /projects/{pid}/integrations/github`, rule R1): their user access token,
refreshed through `GitHubConnections.call`. Anyone on the project may save the destination (D6).

## Permissions (the owner approves them once per installation)

The App needs two more permissions than the clone check:

| Permission | Level | For |
|---|---|---|
| Repository: Issues | Read and write | create and edit issues, comments, labels, sub-issues |
| Organization: Projects | Read and write | read and create organization Projects v2, add items, move items |

GitHub documents that a user-to-server token acts with the permissions of the App on what both the user and the installation can
reach, and that GitHub App tokens can use the Projects v2 GraphQL API ("Using the API to manage Projects"). Changing the App's
permissions makes **every installation approve them again**; until an installation does, a call is refused with "Resource not
accessible" and Specflow answers `github_permissions_missing`. The check below shows it for Projects at once; for Issues it shows on
the first pass (the status carries the code). The feature was verified against the documentation only, not with a live
installation: the first live use is an owner follow-up.

## Target

One row per project in `github_project_targets` (migration `0010_github_projects`): the repository (`project_repositories.id`, the
row is deleted with it), the project's GraphQL node id with its title and address, `enabled`, the run columns (`last_*`, as
`jira_targets`) and `connected_user_id`, the member the export acts as. Removing the repository deletes the target; saving a new
target for another repository or project deletes the map first.

## Map and mapping

`github_project_items` has one row per plan node: `kind` (`story` or `subtask`), the issue (`issue_number`, REST `issue_id`,
GraphQL `issue_node_id`), `item_id` (the item on the project; stories only), the applied `content` and its `content_hash`,
`position`, `removed_at`, `error_code`, `synced_at`. A row without `issue_number` is a create about to be sent.

| Plan | GitHub |
|---|---|
| story | an issue in the repository, and an item on the project |
| task | a sub-issue of its story's issue (not an item of the project) |
| work area | a label `area-<slug>` (and `specflow` on everything the export owns) |
| story order | the order of the items on the project (`updateProjectV2ItemPosition`) |
| done when, release, phase, implements | Markdown in the issue body, with the plan item id |

The desired set is `api.jira.desired.build` with a placeholder epic: stories, their live tasks, labels, order, removed and revived
nodes (a task or story a person deleted on GitHub is never made again). Issue dependencies ("blocked by") are not exported.

## Rules of a pass

`push_once` (`api.github_projects.sync`): skipped with `no_plan` or `plan_applying`, `github_projects_not_configured` without an enabled
target, `target_changed` when the destination changed during the pass. Then, in order:

1. **Create** each missing story and task (a story, then its tasks): issue, then the item on the project (story) or the sub-issue link
   (task). A story that has an issue but no item (an earlier pass stopped between the two calls) gets its item first.
2. **Update** a changed item: read its state (open or closed, Status field of the item on this project). An **open issue whose Status
   is empty, `Todo`, `To do` or `Backlog`** takes the plan's title, body and labels. Any other issue gets **one comment** listing the
   changed fields and is otherwise left alone; the stored content moves on, so the comment is not repeated.
3. **Remove**: an item that leaves the plan gets the label `graphico-removed` and a comment, and `removed_at`; it is never deleted or
   closed. One that returns loses the label and gets a comment.
4. **Order**: the minimal moves (`api.exports.rank`) put the story items in plan order; new items stay at the end of the project.

A refused issue (GitHub answered 422) fails that item only (`github_item_invalid`, status `partial`); a 404 or 410 on an issue marks it
gone (`github_issue_gone`). Every other refusal ends the pass `failed` with one closed code and the next pass continues from the rows.
A crash between an issue's create and its row can create that issue twice after the restart (the same window as the Jira sync).
Task order inside a story and a task that moves to another story are not exported; the sub-issue link is made once.

## Runs

The same as the Jira sync ([../jira-sync/sync.md](../jira-sync/sync.md#triggers-and-runs)): triggers `plan_run` (the plan fan-out calls
`after_plan`), `settings` (saving a destination that was turned on or changed) and `recovery` (a start finds a stored `running`);
starts are merged, one pass runs per process, `running` is stored before the slot. Logs `api.github_projects.sync.*` carry ids, codes and
counts only.

## Codes

| Code | Where | Meaning |
|---|---|---|
| `github_not_connected`, `github_reconnect_required` | check, save, pass | no GitHub binding on the project, or the member's connection expired |
| `github_unreachable` | check, save, pass | GitHub down, a rate limit or a secondary limit (try again later) |
| `github_auth_failed` | check, save, pass | GitHub refused the App's credentials or an unknown refusal |
| `github_repo_not_accessible` | check, save, pass | the account cannot read the repository through the App |
| `github_repo_read_only` | check, save | the account cannot push to the repository, so it cannot create issues |
| `github_not_an_organization` | check, save | the repository belongs to a user |
| `github_permissions_missing` | check, save, pass | the installation has not approved Issues or Organization projects |
| `github_project_not_found` | check, save, pass | no such project in the repository's organization, or the account cannot see it |
| `github_item_invalid`, `github_issue_gone` | item | GitHub refused one issue, or a person deleted or moved it |

## Limits

- A 403 that is not a rate limit is reported as `github_permissions_missing`, so a locked or archived repository reads the same as
  a missing approval.
- If saving the destination fails after a new project was created on GitHub, an empty project may remain there (nothing deletes it).
- On an issue that is not started the label list is owned by the export: labels a person added are replaced with the plan's.
- A task that moves to another story is not re-linked: its `item_id` keeps the old parent (the plan does not move tasks between stories).
- A task's `item_id` holds the id of the issue it is linked under (the marker that the sub-issue call was made); a story's holds its
  item on the project.

The project list shows the 50 most recently updated open projects. The pass has no progress count. Rate limits end a pass
(`github_unreachable`); the next trigger repeats it from the rows.
