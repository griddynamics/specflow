---
type: API
title: "Project integrations API"
description: "The project integration routes (who each app is connected by, connect, remove), member roles on integrations, the importer seam, plan destinations, the Jira answer channel, the GitHub Projects destination and the Notion routes: shapes, codes and rules"
status: draft
tags: [mission-control, integrations, api]
---
# integrations-api.md: project integrations, member roles, importer seam, plan destinations

What Mission Control V2 asks of the API (decisions D1, D3, D4, D6 of `DECISIONS.md` in this folder). Under `/api/v1`, for
the signed-in web user. An **integration** is the project's connection to one app: it binds one member's per-user
connection to the project, and every use of the app in the project (a Jira target, a repository, a Slack channel)
acts as that member. The card says "by *member*".

The screens that call these routes: [ui.md](ui.md). The connector docs: [../sources/importers.md](../sources/importers.md) (Confluence, Jira epic),
[../slack/gap-answering.md](../slack/gap-answering.md) (Slack channel), [../jira-sync/gaps.md](../jira-sync/gaps.md) (Jira gap channel),
[../jira-sync/INDEX.md](../jira-sync/INDEX.md) (Jira plan destination), [../github/INDEX.md](../github/INDEX.md) (repositories, GitHub Projects), [../notion/INDEX.md](../notion/INDEX.md) (Notion connection, page source, plan destination).

## Routes

| Method, path | Who | Success | Errors |
|---|---|---|---|
| `GET /projects/{pid}/integrations` | member | 200 `ProjectIntegrationsOut` | 404 `project_not_found` |
| `PUT /projects/{pid}/integrations/{app}` (`app` is `atlassian`, `github` or `notion`; no body) | member | 200 `ProjectIntegrationOut` | 404 `project_not_found`; 409 `atlassian_not_connected` / `github_not_connected` / `notion_not_connected`; 409 `integration_workspace_mismatch`; 503 `atlassian_oauth_unavailable` / `github_unavailable` / `notion_unavailable`; 422 `validation_failed` (`app` is `slack`, or unknown) |
| `DELETE /projects/{pid}/integrations/{app}` (`app` is `atlassian`, `github`, `slack` or `notion`) | the member who connected it, or the owner | 204 | 404 `project_not_found`; 404 `integration_not_found`; 403 `integration_forbidden`; 502 `slack_unreachable` (Slack channel could not be unbound); 422 `validation_failed` (unknown `app`) |

**Slack in the UI:** `DELETE /projects/{pid}/slack` (the route of the Settings tab) and `/specflow disconnect` follow the same rule as
`DELETE /projects/{pid}/integrations/slack`: the member who connected the channel, or the owner (`403 integration_forbidden` for any
other member). A channel bound before the integration row existed has no connector, so any member may remove it. `PUT` of an app
another member still holds with an active connection is `409 integration_already_connected` (it is taken over only after that member
left, lost the connection or removed the integration).

Slack is bound by its own command in the channel (`/specflow connect <project id>`, [../slack/gap-answering.md](../slack/gap-answering.md)),
which records the caller as the project's Slack member; there is no `PUT` for it. The caller connects Atlassian,
GitHub and Notion to their own account first, with the per-user routes (`/integrations/atlassian/*`,
`/integrations/github/*`, `/integrations/notion/*`); `PUT` then binds that connection to the project.

### Shapes

`ProjectIntegrationsOut` is `{integrations: ProjectIntegrationOut[]}`: always four entries, in the order `atlassian`,
`github`, `slack`, `notion`.

| Model | Fields |
|---|---|
| `ProjectIntegrationOut` | `app`, `available` (the server runs the app: Atlassian `oauth` mode, GitHub on, Slack on, Notion on: `NOTION_OAUTH_CLIENT_ID` set), `connected_by` (`{user_id, name}` or null), `workspace` (`{id, name}` or null), `connected_at` (or null), `health` |
| `health` | `active`: the bound member's connection works (Slack: the channel is bound); `reconnect_required`: their Atlassian, GitHub or Notion connection expired; `none`: nothing acts (no row, the member left, or their connection is gone) |

`connected_by.name` is the member's display name (their email when they have no name). `connected_by` is null and
`workspace` stays when the member left: another member connects it again (D2). The Atlassian workspace name is the host
of the configured site; the GitHub one is the account login; the Slack one the workspace name; the Notion one the name of the Notion workspace
the member connected.

### Rules

- **Connect** (`PUT`): the caller's own connection must be `active` (else `409 <app>_not_connected`). The project has
  one workspace per app (D3): the Atlassian site, the GitHub account (login), the Notion workspace or the Slack workspace of that connection.
  A different workspace is `409 integration_workspace_mismatch` (for GitHub the workspace is the login of the connecting
  account, so a second member who connected GitHub under another login gets it until the binding is removed), unless
  nobody acts on the earlier one (its member left), which any member then replaces. The same workspace is taken over by the caller: the row names them, and
  `jira_targets.connected_user_id` (Atlassian) or every `project_repositories.added_by` (GitHub) is set to them in the
  same transaction, so the runners that read those columns are unchanged.
- **Acting account** (D1): `PUT /projects/{pid}/jira` sets `connected_user_id` from the project's `atlassian` row
  (`409 jira_not_connected` when there is none), not from whoever saves. `POST /projects/{pid}/repositories` reads the
  `github` row's member's repository list and stores that member as `added_by` (`409 github_not_connected` when there
  is none). The Jira access check runs as the same member (`409 jira_not_connected` when there is none), so it answers what the
  sync will be able to do. In `token` mode nothing reads `connected_user_id`.
- **Remove** (`DELETE`): the member who connected the app, or the owner (AC6). The check and the delete are one
  statement (the row goes only if that member still holds it). The row is deleted and the Jira target's
  `connected_user_id` or the repositories' `added_by` becomes null in the same transaction, so everything that used the
  account stops and no other member's account takes over (D2). The existence and holder checks are the same for every app, Slack included (also when Slack is off on the server). For Slack the binding is read first (a failed read is `502 slack_unreachable` too) and the channel is unbound before the row goes, only once that worked; a failed unbind is `502 slack_unreachable` (the row stays), and a binding taken over by another member meanwhile is `403 integration_forbidden`. A
  member who is removed from the project or leaves it stops being the member of every row of the project in the
  same transaction.
- No transaction is open across a call to an app; events `api.integrations.connected`, `.removed`, `.refused` carry
  ids and the app name only.

## Slack test message

`POST /projects/{pid}/slack/test` (no body; member, D6) posts `Welcome to the workspace of project <project name>.` in
the project's bound channel (text in [../slack/gap-answering.md](../slack/gap-answering.md)) and answers 200
`{posted: true, channel: {id, name, private}}`. Nothing is stored; the text and Slack's own error text are never logged.

| Code | HTTP | When |
|---|---|---|
| `slack_not_connected` | 409 | the project has no bound channel (the channel is bound by `/specflow connect`) |
| `slack_not_in_channel` | 409 | the app is not in the channel any more, or it is archived |
| `slack_unreachable` | 502 | Slack did not answer, or refused for another reason |
| `slack_unavailable` | 503 | the Slack app is off on this server |
| `project_not_found` | 404 | not a member |

The canvas form polls `GET /projects/{pid}/slack` until `channel` is set, then enables **Test connection**.

## Roles (D6)

Owner only: add and remove members, cancel invites, rename the project. Every other action of the project is a
member's, owner or collaborator:

| Route | Before | Now |
|---|---|---|
| `PUT /projects/{pid}/jira`, `POST /projects/{pid}/jira/access-check` | owner | member |
| `POST /projects/{pid}/repositories`, `DELETE .../{rid}`, `POST .../{rid}/clone` | owner | member |
| `DELETE /projects/{pid}/slack`; `/specflow connect`, `/specflow disconnect` in Slack | owner | member (disconnect: connector or owner) |
| `editable` of `GET /projects/{pid}/jira` and of `GET /projects/{pid}/slack`; `can_manage` of `GET /projects/{pid}/repositories` | owner only | true for every member |
| `PUT` / `DELETE /projects/{pid}/integrations/{app}` | new | member (`DELETE`: connector or owner) |

Unchanged (members could already): sources, uploads, Drive, source removal, graph build, gaps, plan, `sync_to_jira`.

## Importer seam

For sources the API reads itself (Confluence pages, Jira epics, Notion pages; each source session adds its importer). Nothing in
the graph build changes: an importer writes `parsed` documents the build already reads.

- Source kinds: `connections.kind` is `gdrive`, `upload`, `confluence`, `jira_epic` or `notion` (`SourceKindName` too).
  `external_id` of an imported source is `str(uuid)`, as a Local files batch has; `connected_by` is the importing member.
- `api.imports.ImportRunner` (`Runtime.imports`) holds the imports of the process: one asyncio task per source, at most
  two at once (`MAX_CONCURRENT_IMPORTS`). `add_source` creates the source (`listing`); `start(job, importer)` runs it:
  `importing`, the importer, then `done`, or `failed` with a closed `connections.failure_reason` (`ImportFailedError`
  names one, any other error is `unknown_error`). No Temporal workflow is used (AC5).
- An importer is `async def (job: ImportJob, writer: ImportWriter)`. `ImportWriter.declare(external_id, name)` lists an
  item (`connection_items`, `pending`); `add(external_id, name=, original=, markdown=)` stores the original under
  `projects/{pid}/documents/{did}/original.json` (or the extension of another media type), inserts the `documents` row
  (`origin=connection`, `status=parsed`, `parser=<kind>-convert`, held by the source) and finishes the item
  `processed` (a duplicate of content the project holds is `duplicate`, linking the first document); `fail(...)` ends
  one item `error`. A removed source stops the importer at its next write (`SourceGoneError`).
- A restart ends the tasks: at startup every source of those kinds still `listing` or `importing` and not touched since
  before this process started is `failed` (`unknown_error`) with its unfinished items `error` (`import_failed`); the member adds it again (D7: import once). The start instant is read once from the database clock (`SELECT now()`) and compared with `connections.updated_at`, so no process clock is involved; a source added after it is never ended. If the clock read fails (the database is still coming up) it is tried again after 0.5, 1 and 2 seconds; after the last failure a warning (`api.imports.recovery_failed`, the error class only) is logged and no source is ended. One API replica is assumed, as for the Slack bindings.
- Removing such a source (`DELETE /projects/{pid}/sources/{sid}`) behaves as removing an upload.
- Lineage: an imported document is `doc:<document id>`; a re-imported changed page is a new graph document.
- The importers that exist, their routes, caps and failure reasons: [../sources/importers.md](../sources/importers.md), "Confluence source" and "Jira epic source" below.

## Confluence source

What the canvas form (check, then add) calls. Any member (D6); the import reads as the member the project's Atlassian is
connected by (R1), runs in the API process and is the source `kind` `confluence`. What it reads, the caps and how a changed
page is added again: [../sources/importers.md](../sources/importers.md).

| Method, path | Body | Success | Errors |
|---|---|---|---|
| `POST /projects/{pid}/sources/confluence/check` | `{link, follow}` | 200 `ConfluenceCheckOut` | 404 `project_not_found`; the codes below |
| `POST /projects/{pid}/sources/confluence` | `{link, follow}` | 201 `BatchSourceOut` (`{source: SourceSummaryOut}`, the card of the new source, `kind` `confluence`, `listing_finished` false) | as the check; 404 `source_not_found` (removed at once) |

`link` is at most 2048 characters, `follow` defaults to false (the body is closed: other keys are 422 `validation_failed`).
`ConfluenceCheckOut`: `kind` (`page` or `space`), `title` (the page title or the space name; shown, not stored), `pages`
(how many pages an import would take, at most 100; the first page plus its descendants, or the space's pages), `capped`
(there are more than the cap), `linked_issues` (with `follow`: the Jira issues the first page links to; else 0, and 0 for a
space). Pages that "follow links" would add are not counted. The check reads Confluence (up to a few calls); nothing is
stored. Adding does not repeat a source for the same link: every add is a new source (D7).

**Progress and status.** The new source is a card of `GET /projects/{pid}/sources?batches=1` like any other: `kind`
`confluence`, `jira_epic` or `notion`, `name` (the title), `link` (as pasted), `added_by` (the caller), `counts` per file state,
`total`, `listing_finished` (false while `listing`), `open` false and, when the import failed, `failure_reason` (`no_access`
or `unknown_error`). Items appear as pages are declared and finish as they are stored, so `total` grows while the import
runs; a page the bound member cannot see is a file in a problem state (`access_lost`). `GET /projects/{pid}/sources`
without `batches` lists Drive sources only (as before) and counts the imported ones in its `progress` totals. Poll the
`?batches=1` status; a card with `listing_finished` true and `counts.waiting + counts.processing = 0` is done.

Codes (any 4xx here carries only a fixed sentence, never Confluence's text):

| Code | HTTP | When |
|---|---|---|
| `atlassian_oauth_unavailable` | 503 | the server is not in Atlassian `oauth` mode |
| `atlassian_not_connected` | 409 | the project has no Atlassian connection bound to a member (the canvas says "Set up first") |
| `atlassian_reconnect_required` | 409 | the bound member's connection expired or Atlassian refused its token again after a refresh |
| `atlassian_site_mismatch` | 422 | the link is on another host than `ATLASSIAN_SITE_URL` |
| `confluence_link_invalid` | 422 | not an https page or space link of the site (including the old `/wiki/display/` form) |
| `confluence_short_link_unsupported` | 422 | a `/wiki/x/<id>` short link: paste the full page address |
| `confluence_not_found` | 409 | the page or space does not exist or is not visible to the bound member |
| `confluence_forbidden` | 409 | Confluence refused the bound member (403) |
| `atlassian_unreachable` | 502 | Atlassian could not be reached, or answered something unusable |

Order of checks: project member, Atlassian bound and usable, link, Confluence.

## Jira epic source

What the canvas form (check, then add) calls. Any member (D6); the import reads as the member the project's Atlassian is
connected by (R1), runs in the API process and is the source `kind` `jira_epic`. What it reads and its caps:
[../sources/importers.md](../sources/importers.md).

| Method, path | Body | Success | Errors |
|---|---|---|---|
| `POST /projects/{pid}/sources/jira-epic/check` | `{key_or_link}` | 200 `JiraEpicCheckOut` | 404 `project_not_found`; the codes below |
| `POST /projects/{pid}/sources/jira-epic` | `{key_or_link, exclude?}` | 201 `BatchSourceOut` (the card of the new source, `kind` `jira_epic`, `name` `KEY: summary`, `link` `https://<site>/browse/KEY`) | as the check; 404 `source_not_found` (removed at once) |

`key_or_link` is at most 2048 characters (the body is closed: other keys are 422 `validation_failed`).
`JiraEpicCheckOut`: `key` (upper case), `summary` (shown, not stored), `issues` (how many issues the epic has for the
bound member; an import takes at most 200), `children` (`[{key, summary, type, status}]`: the issues under the epic in
key order, at most 200, read with a search of those three fields; names are shown, not stored) and `capped` (the epic has
more issues than `children` lists). The add takes an optional `exclude`: issue keys such as `ABC-12`, default `[]`, at
most 200, each matching `^[A-Z][A-Z0-9_]*-\d+$` (else 422 `validation_failed`). The epic is always imported; a child
whose key is listed is neither declared nor imported, and a key that is not a child is ignored. The log carries the count
only. Progress and status are the card of `GET /projects/{pid}/sources?batches=1`, as
for the Confluence source above; one source per add, every add is a new source (D7).

Codes: the shared `atlassian_oauth_unavailable`, `atlassian_not_connected`, `atlassian_reconnect_required`,
`atlassian_site_mismatch`, `atlassian_unreachable` of the Confluence source, and

| Code | HTTP | When |
|---|---|---|
| `jira_epic_link_invalid` | 422 | not an issue key, nor an https `/browse/KEY` or `selectedIssue=KEY` link |
| `jira_epic_not_an_epic` | 422 | the issue exists but is not an epic |
| `jira_epic_not_found` | 409 | no such issue, or not visible to the bound member |
| `jira_epic_forbidden` | 409 | Jira refused the bound member (403) |

Order of checks: project member, Atlassian bound and usable, key or link, Jira.

## Google Drive source

What the Drive dialog (test, then add) calls. Any member; the import reads the folder with the Drive account of the
deployment and is the source `kind` `gdrive`.

| Method, path | Body | Success | Errors |
|---|---|---|---|
| `POST /projects/{pid}/sources/gdrive/test` | `{link}` | 200 `DriveTestOk` or `DriveTestFailed` (`result` `failed` with a `reason`) | 404 `project_not_found` |
| `POST /projects/{pid}/sources/gdrive` | `{link, exclude?}` | 201 `SourceCreated` | 422 `validation_failed`; the Drive reason codes (`invalid_link`, `no_access`, `already_connected`, ...) |

`DriveTestOk`: `folder` (`{id, name}`), `folder_count` (includes the shared root), `file_count`, `categories`
(`[{category, processing_class, count}]`) and, to pick files before adding:

- `files`: `[{id, name, path, category}]`, one per listed file, ordered by `path` then `name` (both case-insensitive).
  `path` is the folder names from the shared root (the root itself is not in it) down to the file's parent, joined with
  `/`; a file directly in the root has `""`. `category` is the value of `categories[].category`. `id` is the Drive file id.
- `files_capped`: more files than listed. At most 1000 are listed (`DRIVE_FILES_LIST_MAX`); `file_count` and
  `categories` always count every file. Names and paths are shown, never logged.

The add takes an optional `exclude`: Drive file ids from `files[].id`, default `[]`, at most 1000, each matching
`^[A-Za-z0-9_-]{1,128}$` (else 422 `validation_failed`). The test body is unchanged and refuses `exclude`. An id that is
not in the folder is ignored. Excluded files are neither declared (no `connection_items` row), downloaded nor counted
in the progress totals. The ids travel as input of the `DriveImportWorkflow` (Temporal keeps the history), which lists
the folder again in the worker and drops them before storing the listing; so continue-as-new and activity retries keep
them, and nothing in the database holds them. The log carries the count only. A file added to the folder after the
check is imported (it was not unchecked). Files beyond the 1000 of a capped check cannot be unchecked and are imported.

## Plan destinations

`GraphPlanRunner` has one post-plan hook and is unchanged. `Runtime.plan_destinations` (`api.plan_destinations.
PlanDestinations`) is that hook: its `after_plan(project_id)` calls each registered destination in order (Jira first,
when it is configured, then GitHub Projects), and a destination that raises is logged by class (`api.plan.destination_failed`) and never
reaches the plan run or the next destination. A destination only enqueues its work. A new destination registers itself
where the runtime is built (`api.runtime._add_runners`).

## Gap answers by Jira

`AnswerInput.via` (the gap library) accepts `jira` besides `web`, `elicitation` and `slack`, so a Jira answer is
recorded in the answer history under its own name. No column stores it, no consumer switches over the literal.

## Jira gap channel

What the canvas form (space key and grouping, Check access, Add channel) and the channel node call. Any member (D6).
Design, posting, polling and limits: [../jira-sync/gaps.md](../jira-sync/gaps.md). The channel acts as the member the
project's Atlassian is connected by (R1); it needs Atlassian OAuth and gap runs.

| Method, path | Body | Success | Errors |
|---|---|---|---|
| `GET /projects/{pid}/jira-gaps` | | 200 `JiraGapsOut` | 404 `project_not_found` |
| `PUT /projects/{pid}/jira-gaps` | `{jira_project, grouping, enabled}` | 200 `JiraGapsOut` | 404; 409 `jira_not_connected` (no Atlassian bound to a member); 422 `validation_failed`; 503 `atlassian_oauth_unavailable`, `gap_unavailable` |
| `POST /projects/{pid}/jira-gaps/access-check` | `{jira_project}` | 200 `JiraAccessResult` (`ok` with `project_name`, `epic_summary` null; or `failed` with `reason` and `field`) | 404; 503 as above |

- `jira_project` is trimmed and upper-cased (`^[A-Z][A-Z0-9_]{1,9}$`); `grouping` is `one` or `area`; the body is closed.
- `JiraGapsOut`: `available` (false when the server has no Atlassian OAuth or gap runs are off: then the other fields are
  empty), `jira_project`, `grouping`, `enabled`, `story_key`, `story_url` (`<site>/browse/<key>`), `posted` and `answered`
  (questions posted and answered, for the node), `last_polled_at`, `error_code` (the closed code of the last refused post or
  poll), `poll_seconds` (60: the form says answers are read about every 60 seconds).
- The access check failed reasons: `jira_not_connected`, `jira_reconnect_required` (no field), `jira_auth_failed`,
  `jira_unreachable`, `jira_unexpected` (no field), `jira_not_found`, `jira_forbidden`, `jira_issue_types_missing` (field
  `jira_project`).
- `PUT` never waits for Jira: turning the channel on (or another Jira project) posts the open questions in the background;
  read `GET` for `story_key` and the counts.

## GitHub Projects

What the canvas form (repository, project picked or created, Check access, Add destination) and the destination node call. Any member
(D6). Design, mapping, rules of a pass, permissions and limits: [../github/projects.md](../github/projects.md). The export acts as the
member the project's GitHub is connected by (R1); it needs the GitHub integration and an installation that approved Issues and
Organization projects (until then: `github_permissions_missing`).

| Method, path | Body | Success | Errors |
|---|---|---|---|
| `GET /projects/{pid}/github-projects` | | 200 `GitHubProjectsOut` | 404 `project_not_found` |
| `GET /projects/{pid}/github-projects/projects?repository_id=` | | 200 `GitHubProjectsListOut` (`organization`, `projects[{node_id, number, title, url}]`) | 404 `repository_not_found`; 409 `github_not_connected`, `github_reconnect_required`, `github_permissions_missing`; 502 `github_unreachable`, `github_auth_failed`; 503 `github_unavailable` |
| `PUT /projects/{pid}/github-projects` | `{repository_id, project_node_id` or `new_project_title, enabled}` | 200 `GitHubProjectsOut` | 404 `repository_not_found`, `github_project_not_found`; 409 `github_not_connected`, `github_reconnect_required`, `github_permissions_missing`; 422 `validation_failed`, `github_not_an_organization`, `github_repo_read_only`; 502; 503 `github_unavailable` |
| `POST /projects/{pid}/github-projects/access-check` | `{repository_id, project_node_id` or `new_project_title}` | 200 `GitHubProjectsAccessResult` (`ok` with `organization`, `project_title` or null; or `failed` with `reason` and `field`) | 404 `repository_not_found`; 409 `github_not_connected`; 503 `github_unavailable` |

- `repository_id` is the id of a repository of the project (`GET /projects/{pid}/repositories`). Name exactly one of `project_node_id`
  (a project picked from the list) and `new_project_title` (1 to 100 characters, trimmed; the project is created in the repository's
  organization when the form is saved); the body is closed.
- `GitHubProjectsOut`: `available` (false when GitHub is off on this server: the other fields are empty), `repository_id`,
  `repository` (`owner/name`), `project_node_id`, `project_title`, `project_url`, `enabled`, `connected_by_name` (the GitHub login the
  export acts as) and `connected_by_me`, `sync` (`project_id, enabled, status` `idle|running|succeeded|partial|failed|skipped`, `queued`,
  `trigger`, `started_at`, `finished_at`, `error_code`, `counts{created, updated, commented, removed, ranked, unchanged, failed}`,
  `repository`, `project_title`; null without a target).
- The access check failed reasons: `github_not_connected`, `github_reconnect_required`, `github_unreachable`, `github_auth_failed`,
  `github_repo_not_accessible` (field `repository`), `github_not_an_organization` (field `repository`), `github_repo_read_only` (field
  `repository`), `github_permissions_missing`, `github_project_not_found` (field `project`). The check reads the repository, its
  owner and write access, then the project (or lists the organization's projects); it cannot try an issue, so a missing Issues
  approval shows on the first pass (`sync.error_code`).
- `PUT` never waits for a pass: turning the destination on (or another repository or project) starts one in the background; read
  `GET` for `sync`. The plan fan-out also starts one after every plan run.
- Sync `error_code` values: `github_projects_not_configured`, `no_plan`, `plan_applying`, `target_changed` (skipped), and the closed
  GitHub codes above plus `github_refused`, `github_item_invalid`, `github_issue_gone`.

## Notion

What the Integrations card, the canvas forms and the nodes call. Any member (D6). Everything about Notion (the public connection,
the page source, the plan database, rules and limits) is in [../notion/INDEX.md](../notion/INDEX.md); the codes are closed in
[../notion/codes.md](../notion/codes.md). Every read and write acts as the member the project's Notion is connected by (R1); a Notion
connection has no login, so its workspace name is the Notion workspace.

| Method, path | Body | Success | Errors |
|---|---|---|---|
| `GET /integrations/notion` | | 200 the caller's connection (`enabled`, `connected`, `status`, `workspace_name`, `connected_at`, `last_used_at`) | |
| `POST /integrations/notion/authorize` | `{return_to}` | 200 `{url}` (Notion's consent page) | 422 `validation_failed`; 503 `notion_unavailable` |
| `POST /integrations/notion/callback` | `{code, state}` | 200 `{connection, return_to}` | 400 `notion_state_invalid`, `notion_code_invalid`; 502 `notion_unreachable`, `notion_auth_failed`; 503 `notion_unavailable` |
| `DELETE /integrations/notion` | | 204 | 503 `notion_unavailable` |
| `POST /projects/{pid}/sources/notion/check` | `{link}` | 200 `{kind, title, pages, capped}` | 404 `project_not_found`; 409 `notion_not_connected`, `notion_reconnect_required`, `notion_not_shared`; 422 `notion_link_invalid`; 502; 503 |
| `POST /projects/{pid}/sources/notion` | `{link}` | 201 the source card | as the check |
| `GET /projects/{pid}/notion-plan` | | 200 `NotionPlanOut` | 404 `project_not_found` |
| `PUT /projects/{pid}/notion-plan` | `{link, enabled}` | 200 `NotionPlanOut` | 409 `notion_not_connected`, `notion_reconnect_required`, `notion_not_shared`, `notion_permissions_missing`; 422 `validation_failed`, `notion_link_invalid`, `notion_parent_not_a_page`; 502; 503 `notion_unavailable` |
| `POST /projects/{pid}/notion-plan/access-check` | `{link}` | 200 `{result: ok, parent_page_title}` or `{result: failed, reason}` (a closed `notion_*` reason; an unbound project or an expired connection is a `failed` result, not an error) | 404; 503 |

- `NotionPlanOut`: `available` (false when Notion is off on this server: the other fields are empty), `parent_page_id`,
  `parent_page_title`, `parent_page_url`, `database_url` (set once the first pass made the database), `enabled`,
  `connected_by_name` (the Notion workspace name), `connected_by_me`, `sync` (`project_id`, `enabled`, `status` `idle|running|succeeded|partial|failed|skipped`,
  `queued`, `trigger`, `started_at`, `finished_at`, `error_code`, `counts{created, updated, commented, removed, unchanged, failed,
  deferred}`, `parent_page_title`; null without a target).
- A source's `kind` is `notion`; an import never shows a `notion_*` code (closed failure reasons of sources, N6); the check does.
- `PUT` never waits for a pass; the plan fan-out (`Runtime.plan_destinations`) also starts one after every plan run.

## Codes

| Code | HTTP | When |
|---|---|---|
| `atlassian_not_connected` | 409 | `PUT` Atlassian: the caller has no active Atlassian connection |
| `notion_not_connected`, `notion_reconnect_required` | 409 | `PUT` Notion: the caller has no active Notion connection; the Notion check, add and save: the project has no Notion binding, or the bound member's connection expired |
| `notion_state_invalid`, `notion_code_invalid` | 400 | the Notion callback: the sign-in `state` is not valid, or Notion refused the code |
| `notion_link_invalid`, `notion_not_shared`, `notion_parent_not_a_page`, `notion_permissions_missing` | 422, 409, 422, 409 | the Notion check, add and save: not a link to a page or database; nothing shared at the link; a database where a page is needed; the connection may not write |
| `notion_unreachable`, `notion_auth_failed` | 502 | Notion did not answer, or refused this server's credentials |
| `notion_rate_limited` | 503 | the wait for Notion's request limit did not fit its budget |
| `github_not_connected` | 409 | `PUT` GitHub: the caller has no active GitHub connection; adding a repository: the project has no GitHub connection bound to a member |
| `integration_already_connected` | 409 | `PUT`: another member's connection to the app is still active, so the binding is not taken over (they or the owner remove it first) |
| `integration_workspace_mismatch` | 409 | `PUT`: the project is connected to another Atlassian site, GitHub account, Notion workspace or Slack workspace |
| `integration_not_found` | 404 | `DELETE`: the project has no connection to that app |
| `integration_forbidden` | 403 | `DELETE`: the caller neither connected the app nor owns the project |
| `atlassian_oauth_unavailable`, `github_unavailable`, `slack_unavailable`, `notion_unavailable` | 503 | the app is not configured on this server |
| `slack_link_invalid` | 400 | `POST /integrations/slack/link`: the nonce expired or was used |
| `jira_not_connected` | 409 | `PUT /projects/{pid}/jira` and `PUT /projects/{pid}/jira-gaps` in `oauth` mode: the project has no Atlassian connection bound to a member |
| `gap_unavailable` | 503 | `PUT` and the check of `jira-gaps`: gap runs are off on this server |
| `github_permissions_missing` | 409 | `github-projects`: the installation has not approved Issues or Organization projects |
| `github_project_not_found` | 404 | `PUT github-projects`: no such project in the repository's organization, or the account cannot see it |
| `github_not_an_organization`, `github_repo_read_only` | 422 | `PUT github-projects`: the repository belongs to a user, or the account cannot push to it |

## Data

`project_integrations` (migration `0008_project_integrations`): primary key (`project_id`, `app`), `app` one of
`atlassian`, `github`, `slack`, `notion` (a later integration widens the check), `user_id` (null once the member left; foreign
key `users.id` on delete set null), `workspace_id`, `workspace_name`, `connected_at`. Row-level security on, policy
`agent_project_scope`, no grant. The migration fills an `atlassian` row from every Jira target whose acting account
has a connection, and a `github` row from the first adder of every project's repositories whose adder has a GitHub
connection; Slack bindings live in a file of the graph state folder, so the api fills the `slack` rows from it at
its start (the member who ran the connect, while they are a member).

The Notion tables (migration `0011_notion`; columns in [../notion/oauth.md](../notion/oauth.md#data) and [../notion/plan.md](../notion/plan.md)):
`notion_connections` (one row per user: workspace, bot, encrypted refresh token, `status`), `notion_plan_targets` (one per project: the parent
page, the database and its data source once made, `enabled`, the last pass) and `notion_plan_items` (the map of plan nodes to rows,
`kind` `story|task`). All three have row-level security on and no grant; the two plan tables carry the policy `agent_project_scope`, `notion_connections` none (not project-scoped). The migration also widens the checks of
`project_integrations.app` and `connections.kind` with `notion`.
