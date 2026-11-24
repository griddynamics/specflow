---
type: API
title: "Project integrations API"
description: "The project integration routes (who each app is connected by, connect, remove), member roles on integrations, the importer seam, plan destinations and the Jira answer channel: shapes, codes and rules"
status: draft
tags: [mission-control, integrations, api]
---
# integrations-api.md: project integrations, member roles, importer seam, plan destinations

What Mission Control V2 asks of the API (decisions D1, D3, D4, D6 of `DECISIONS.md` in this folder). Under `/api/v1`, for
the signed-in web user. An **integration** is the project's connection to one app: it binds one member's per-user
connection to the project, and every use of the app in the project (a Jira target, a repository, a Slack channel)
acts as that member. The card says "by *member*".

## Routes

| Method, path | Who | Success | Errors |
|---|---|---|---|
| `GET /projects/{pid}/integrations` | member | 200 `ProjectIntegrationsOut` | 404 `project_not_found` |
| `PUT /projects/{pid}/integrations/{app}` (`app` is `atlassian` or `github`; no body) | member | 200 `ProjectIntegrationOut` | 404 `project_not_found`; 409 `atlassian_not_connected` / `github_not_connected`; 409 `integration_workspace_mismatch`; 503 `atlassian_oauth_unavailable` / `github_unavailable`; 422 `validation_failed` (`app` is `slack`, or unknown) |
| `DELETE /projects/{pid}/integrations/{app}` (`app` is `atlassian`, `github` or `slack`) | the member who connected it, or the owner | 204 | 404 `project_not_found`; 404 `integration_not_found`; 403 `integration_forbidden`; 422 `validation_failed` (unknown `app`) |

**Slack in the UI:** any member can also remove the channel with `DELETE /projects/{pid}/slack` (D6, the route of the
Settings tab), so a screen must not tell a member that only the connector or the owner can disconnect Slack; only
`DELETE /projects/{pid}/integrations/slack` is connector-or-owner.

Slack is bound by its own command in the channel (`/specflow connect <project id>`, [../slack/gap-answering.md](../slack/gap-answering.md)),
which records the caller as the project's Slack member; there is no `PUT` for it. The caller connects Atlassian and
GitHub to their own account first, with the per-user routes that exist today (`/integrations/atlassian/*`,
`/integrations/github/*`); `PUT` then binds that connection to the project.

### Shapes

`ProjectIntegrationsOut` is `{integrations: ProjectIntegrationOut[]}`: always three entries, in the order `atlassian`,
`github`, `slack`.

| Model | Fields |
|---|---|
| `ProjectIntegrationOut` | `app`, `available` (the server runs the app: Atlassian `oauth` mode, GitHub on, Slack on), `connected_by` (`{user_id, name}` or null), `workspace` (`{id, name}` or null), `connected_at` (or null), `health` |
| `health` | `active`: the bound member's connection works (Slack: the channel is bound); `reconnect_required`: their Atlassian or GitHub connection expired; `none`: nothing acts (no row, the member left, or their connection is gone) |

`connected_by.name` is the member's display name (their email when they have no name). `connected_by` is null and
`workspace` stays when the member left: another member connects it again (D2). The Atlassian workspace name is the host
of the configured site; the GitHub one is the account login; the Slack one the workspace name.

### Rules

- **Connect** (`PUT`): the caller's own connection must be `active` (else `409 <app>_not_connected`). The project has
  one workspace per app (D3): the Atlassian site, the GitHub account (login) or the Slack workspace of that connection.
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
  account stops and no other member's account takes over (D2). For Slack the channel is unbound first and the row goes only once that worked. A
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
| `DELETE /projects/{pid}/slack`; `/specflow connect`, `/specflow disconnect` in Slack | owner | member |
| `editable` of `GET /projects/{pid}/jira` and of `GET /projects/{pid}/slack`; `can_manage` of `GET /projects/{pid}/repositories` | owner only | true for every member |
| `PUT` / `DELETE /projects/{pid}/integrations/{app}` | new | member (`DELETE`: connector or owner) |

Unchanged (members could already): sources, uploads, Drive, source removal, graph build, gaps, plan, `sync_to_jira`.

## Importer seam

For sources the API reads itself (Confluence pages, Jira epics; each source session adds its importer). Nothing in
the graph build changes: an importer writes `parsed` documents the build already reads.

- Source kinds: `connections.kind` is `gdrive`, `upload`, `confluence` or `jira_epic` (`SourceKindName` too).
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
  before this process started (less a minute) is `failed` (`unknown_error`) with its unfinished items `error` (`import_failed`); the member adds it again (D7: import once). One API replica is assumed, as for the Slack bindings.
- Removing such a source (`DELETE /projects/{pid}/sources/{sid}`) behaves as removing an upload.
- Lineage: an imported document is `doc:<document id>`; a re-imported changed page is a new graph document.

## Plan destinations

`GraphPlanRunner` has one post-plan hook and is unchanged. `Runtime.plan_destinations` (`api.plan_destinations.
PlanDestinations`) is that hook: its `after_plan(project_id)` calls each registered destination in order (Jira first,
when it is configured), and a destination that raises is logged by class (`api.plan.destination_failed`) and never
reaches the plan run or the next destination. A destination only enqueues its work. A new destination registers itself
where the runtime is built (`api.runtime._add_runners`).

## Gap answers by Jira

`AnswerInput.via` (the gap library) accepts `jira` besides `web`, `elicitation` and `slack`, so a Jira answer is
recorded in the answer history under its own name. No column stores it, no consumer switches over the literal.

## Codes

| Code | HTTP | When |
|---|---|---|
| `atlassian_not_connected` | 409 | `PUT` Atlassian: the caller has no active Atlassian connection |
| `github_not_connected` | 409 | `PUT` GitHub: the caller has no active GitHub connection; adding a repository: the project has no GitHub connection bound to a member |
| `integration_workspace_mismatch` | 409 | `PUT`: the project is connected to another Atlassian site, GitHub account or Slack workspace |
| `integration_not_found` | 404 | `DELETE`: the project has no connection to that app |
| `integration_forbidden` | 403 | `DELETE`: the caller neither connected the app nor owns the project |
| `atlassian_oauth_unavailable`, `github_unavailable`, `slack_unavailable` | 503 | the app is not configured on this server |
| `slack_link_invalid` | 400 | `POST /integrations/slack/link`: the nonce expired or was used |
| `jira_not_connected` | 409 | `PUT /projects/{pid}/jira` in `oauth` mode: the project has no Atlassian connection bound to a member |

## Data

`project_integrations` (migration `0008_project_integrations`): primary key (`project_id`, `app`), `app` one of
`atlassian`, `github`, `slack` (a later integration widens the check), `user_id` (null once the member left; foreign
key `users.id` on delete set null), `workspace_id`, `workspace_name`, `connected_at`. Row-level security on, policy
`agent_project_scope`, no grant. The migration fills an `atlassian` row from every Jira target whose acting account
has a connection, and a `github` row from the first adder of every project's repositories whose adder has a GitHub
connection; Slack bindings live in a file of the graph state folder, so the api fills the `slack` rows from it at
its start (the member who ran the connect, while they are a member).
