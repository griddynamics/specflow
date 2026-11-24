---
type: API
title: "Jira sync API"
description: "Web routes for the Jira settings and the access check, the agent routes behind sync_to_jira and jira_sync_status, shapes, error codes, sync codes, log events and threats"
status: stable
tags: [jira, api, mcp]
---
# api.md: Jira settings and access-check routes, agent sync routes, shapes, errors, sync codes, log events, threats

All routes are under `/api/v1` and answer the error envelope `{"error": {code, message, details}}`; messages are fixed
sentences and never echo a value or Jira text.

## Access

| Check | Answer |
|---|---|
| Caller is not a member, the project does not exist, or the id is malformed | 404 `project_not_found` (the rule of every project route) |
| Owner route and the caller is a collaborator | 403 `owner_required` (no Jira route is owner-only since V2, D6; the members, rename and invite routes are) |
| Jira is off: neither the OAuth app nor the one account is set ([oauth.md](oauth.md#modes)) | 503 `jira_unavailable` on every route below except `GET /projects/{pid}/jira` |

Any member may start a sync and read its status, on the agent routes (D9, D14), and since Mission Control V2 (D6) any
member saves the settings and runs the access check too.

Order of the checks: membership (404), the body (422), availability (503), then on `PUT` in `oauth` mode the project's
Atlassian connection (409). A malformed body on a
server without `ATLASSIAN_*` is 422, not 503.

## Web routes

| Method, path | Who | Body | Success |
|---|---|---|---|
| `GET /projects/{pid}/jira` | member | - | 200 `JiraSettingsOut` |
| `PUT /projects/{pid}/jira` | member | `JiraSettingsIn` | 200 `JiraSettingsOut` |
| `POST /projects/{pid}/jira/access-check` | member | `JiraAccessCheckIn` | 200 `JiraAccessOk` or `JiraAccessFailed` |

There is no web route that starts a sync (D24): the web app only configures. Bodies forbid extra fields.

`PUT` rules:
- `jira_project` must match `^[A-Z][A-Z0-9_]{1,9}$`; `epic_key`, when not null, `^[A-Z][A-Z0-9_]{1,9}-[1-9][0-9]*$` with the
  prefix `jira_project`; else 422 `validation_failed` with `details.errors[{loc, type}]`. Values are trimmed and upper-cased
  before the check; a blank `epic_key` is null.
- `PUT` does not call Jira; the web app runs the access check before it ([ui.md](ui.md#jira-tab)).
- `PUT` makes the caller the target's acting account (`connected_user_id`). In `oauth` mode the caller needs an `active`
  Atlassian connection of their own, else 409 `jira_not_connected` ([oauth.md](oauth.md#acting-account-of-a-target)).
- A changed `jira_project` deletes the project's map rows in the same transaction ([data-model.md](data-model.md#issues)).
- A change of `jira_project` or `epic_key` while a pass runs never waits for it: that pass stops before its next write
  ([sync.md](sync.md#target-change-during-a-pass)).
- The answer is the state after the change; a sync starts when [sync.md](sync.md#triggers-and-runs) says so (`settings`).

`GET` while `available` is false: `sync` is built from the stored target alone, as [sync.md](sync.md#triggers-and-runs)
reads a stored row with no live pass (`queued` false); `sync` is null without a target.

## Access check

`POST /projects/{pid}/jira/access-check` with the typed values, before they are saved. The API calls Jira in this
order and answers at the first failure; `field` says which input to fix:

| Step | Failure `reason` | `field` |
|---|---|---|
| `oauth` mode: the caller has an Atlassian connection (no Jira call without one) | `jira_not_connected`; `jira_reconnect_required` when it expired | null |
| authenticate | `jira_auth_failed` | null |
| read the project | `jira_not_found` | `jira_project` |
| the project has `Epic`, `Story` and a sub-task type | `jira_issue_types_missing` | `jira_project` |
| the account may browse, create, edit, comment, link and rank issues there | `jira_forbidden` | `jira_project` |
| read the Epic (when given) | `jira_not_found`; `jira_forbidden` when Jira refuses the read (403) | `epic_key` |
| it is an `Epic` of that project | `jira_epic_invalid` | `epic_key` |
| any call: `429` or `5xx` after retries, timeout | `jira_unreachable` | null |
| any other Jira answer, a `400` included | `jira_unexpected` | null |

`JiraAccessOk` carries the Jira project name and the Epic summary (null when Specflow will create the Epic). The
names are Jira data shown to the owner; they are never logged.

## Agent routes

Prefix `/api/v1/agent`, the MCP policy, `project_id` in the query, the membership check first, as every agent route
([../mcp-server/architecture.md](../mcp-server/architecture.md#api-agent-routes)).

| Tool | Method, path | Body | 200 body | Route errors (code → status) |
|---|---|---|---|---|
| `sync_to_jira` | `POST /jira-sync` | `{}` | `JiraSyncStatus` (`status` `running`; `queued` true when merged) | `project_not_found` → 404; `jira_not_configured` → 409; `jira_unavailable` → 503 |
| `jira_sync_status` | `GET /jira-sync-status` | - (query `limit` 1 to 100, default 50; `cursor`) | `JiraSyncReport` | `project_not_found` → 404; `invalid_cursor` → 400; `jira_not_configured` (no target) → 409; `jira_unavailable` → 503 |

`POST /jira-sync` needs no body: an empty body and `{}` are the same (the tool sends `{}`). `sync_to_jira` answers `jira_not_configured` when the project has no target or the target is disabled (D15); the runner queues nothing then and answers `skipped` with `jira_not_configured`, which the route maps to 409; `jira_sync_status` answers it only when there is no target, and shows a disabled target with `enabled: false`.

## Shapes

| Model | Fields |
|---|---|
| `JiraSettingsIn` | `jira_project` (string), `epic_key` (string or null), `enabled` (bool) |
| `JiraAccessCheckIn` | `jira_project`, `epic_key` (string or null) |
| `JiraSettingsOut` | `available` (Jira is on, in `oauth` or `token` mode), `editable` (true for every member), `site_url` (null unless available), `jira_project`, `epic_key`, `created_epic_key` (the Epic Specflow created, read-only), `enabled` (false without a target), `sync` (`JiraSyncStatus` or null without a target), `connection` (`JiraConnectionOut`; null outside `oauth` mode and without a target) |
| `JiraConnectionOut` | `connected_by_name` (the acting account's Atlassian display name, null when `missing`), `connected_by_me` (bool), `status` (`active`, `expired`, `missing`) ([oauth.md](oauth.md#routes)) |
| `JiraAccessOk` | `result: "ok"`, `project_name`, `epic_summary` (string or null) |
| `JiraAccessFailed` | `result: "failed"`, `reason` ([Access check](#access-check)), `field` (`jira_project`, `epic_key` or null) |
| `JiraSyncStatus` | `project_id`, `enabled`, `status` (`idle`, `running`, `succeeded`, `partial`, `failed`, `skipped`), `queued`, `trigger` (or null), `started_at` (when the pass was queued), `finished_at`, `error_code` (or null), `counts` (`JiraSyncCounts` or null), `jira_project`, `epic_key` (configured, else created, else null), `progress` (`JiraSyncProgress` or null, [Progress](sync.md#progress)) |
| `JiraSyncProgress` | `step` (`issues`, `links`, `order`), `done` (integer, 0 or more), `total` (integer, 0 or more) |
| `JiraSyncReport` | the fields of `JiraSyncStatus`, `items` (one page of `JiraSyncItem`), `next_cursor` (null on the last page) |
| `JiraSyncCounts` | `created`, `updated`, `commented`, `removed`, `links_added`, `links_removed`, `ranked`, `unchanged`, `failed` (integers) |
| `JiraSyncItem` (in `items`) | `node_id`, `kind` (`epic`, `story`, `subtask`), `jira_key` (null while pending), `url` (`<site>/browse/<key>` or null), `removed` (bool), `error_code` (or null) |

While a pass is queued or runs, `trigger` and `started_at` are those of that pass (`started_at` is when it was queued), even before the pass has stored its start; a start merged into it changes neither.

`status` is `running` while a pass of the project is queued or runs ([sync.md](sync.md#triggers-and-runs)). `items` is
a list (no map), ordered by kind (`epic`, `story`, `subtask`) then node id, and paged as every list tool pages
([../mcp-server/tools.md](../mcp-server/tools.md#conventions)). `JiraSyncStatus` and `JiraSyncReport` are models of the
agent contracts; `JiraSyncStatus` is used by `sync_to_jira` and by `JiraSettingsOut.sync`.

## Errors

| Status, code | Routes | When | MCP message (tools.md) |
|---|---|---|---|
| 503 `jira_unavailable` | all but `GET /projects/{pid}/jira` | Jira is off (no OAuth app and no account) | Jira sync is not set up on this Specflow server. Ask an administrator to connect an Atlassian account. |
| 409 `jira_not_configured` | agent routes | no target, or (sync) a disabled target | This project does not sync to Jira. Its owner sets the Jira target in the project Settings, tab Jira. |
| 409 `jira_not_connected` | `PUT` | `oauth` mode and the caller has no `active` Atlassian connection | - |
| 403 `owner_required` | `PUT`, access check | the caller is a collaborator | - |
| 422 `validation_failed` | `PUT`, access check | body rule | - |

API messages: `jira_unavailable` "Jira sync is not configured on this server." (`sync_to_jira` while the api shuts down:
"Jira sync is shutting down."); `jira_not_configured` "The project has
no enabled Jira target."; `jira_not_connected` "Connect your Atlassian account before you save the Jira target.".
`jira_unavailable` and `jira_not_configured` join the agent error codes, the error-code registry of the API and the
[tools.md](../mcp-server/tools.md#error-codes) table with the MCP message above. The routes of the Atlassian connection
itself and their codes are in [oauth.md](oauth.md#routes).

## Sync codes

The values of `JiraSyncStatus.error_code`, `JiraSyncItem.error_code` and `JiraAccessFailed.reason`; never an HTTP
error code of these routes except where the table above says so.

| Code | Kind | Meaning |
|---|---|---|
| `jira_auth_failed` | run, check | Jira refused the account (401; in `oauth` mode a second 401 after one refresh, or a refresh Atlassian refused other than `invalid_grant`) |
| `jira_forbidden` | run, check | the account lacks a permission (403, or a missing permission in the check) |
| `jira_not_found` | run, item, check | the project or an Epic (run, check) is gone, or one mapped issue was deleted in Jira (item, 404) |
| `jira_invalid_field` | item | Jira refused a field of one issue (400), or the rank call that moved it (the first moved issue of the call gets the code) |
| `jira_unreachable` | run, check | `429` or `5xx` after retries, a timeout or a transport error |
| `jira_unexpected` | run, item, check | any other Jira status; on a run also a project that lacks the issue type of a create; on an item a link add that answers 404 while both issues exist, and a rank call that answers 404 |
| `jira_issue_types_missing` | check | the project lacks `Epic`, `Story` or a sub-task type |
| `jira_epic_invalid` | check | the Epic key is not an `Epic` of the project |
| `jira_not_connected` | run, check | `oauth` mode: the acting account (run) or the caller (check) has no Atlassian connection, or the project has no Atlassian connection bound to a member (`PUT /projects/{pid}/integrations/atlassian`, [integrations-api.md](../mission-control/integrations-api.md)) |
| `jira_reconnect_required` | run, check | `oauth` mode: that connection expired (Atlassian refused its refresh token); its member reconnects |
| `jira_not_configured` | skipped | the target was disabled or removed before the pass |
| `no_plan` | skipped | the project has no plan yet |
| `plan_applying` | skipped | a plan run was writing the plan |
| `target_changed` | skipped | the owner changed the Jira project or Epic during the pass; one more pass follows when the target stays on |
| `interrupted` | run | the process stopped during the pass, or the pass ended and its end could not be stored |
| `unexpected_error` | run | a bug in Specflow |

## Log events

Ids, codes and counts only: never a summary, description, Jira body, Jira key text other than the key, URL query, email
or a token. The events of the Atlassian connections are in [oauth.md](oauth.md#log-events).

| Event | Fields |
|---|---|
| `api.jira.mode` | `mode` (`oauth` or `token`), at start |
| `api.jira.settings.saved` | `user_id`, `project_id`, `enabled`, `project_changed` |
| `api.jira.access.checked` | `user_id`, `project_id`, `result`, `reason` |
| `api.jira.sync.queued` / `api.jira.sync.merged` | `project_id`, `trigger`, `user_id` (agent route only) |
| `api.jira.sync.started` | `project_id`, `trigger` |
| `api.jira.sync.finished` | `project_id`, `status`, `error_code`, `counts` (one object with the count names) |
| `api.jira.sync.failed` | `project_id`, `error_type` (class name); `error_code` `unexpected_error` when the runner itself failed (storage), not the engine |
| `api.jira.sync.item_failed` | `project_id`, `node_id`, `jira_key`, `error_code` |
| `api.jira.sync.recovered` | `project_id`, `node_id`, `jira_key` (a pending create adopted) |
| `api.jira.sync.start_failed` | `project_id` (null for recovery), `trigger`, `error_type` (the plan hook or startup recovery could not start a pass) |

## Threats

| STRIDE | Example | Mitigation |
|---|---|---|
| Spoofing | a non-member starts a sync | membership check before anything on every route |
| Tampering | a collaborator points the project at another Jira project | accepted since V2 (D6): any member sets the target; the sync acts as the member the project's Atlassian connection is bound to, whoever saves |
| Tampering | a sync overwrites work people do in Jira | only To Do issues are overwritten, only owned fields; others get a comment ([sync.md](sync.md#update-rules)) |
| Tampering | a sync deletes or closes issues | the gateway has no issue delete and no transition call; removed items get a label and a comment |
| Repudiation | who changed the target or started a sync | `api.jira.settings.saved`, `api.jira.sync.queued` with ids |
| Information disclosure | the Atlassian token leaks | `token` mode: env only; a secret setting type; never in the database, a response, a log or an exception text. `oauth` mode: refresh tokens Fernet-encrypted at rest, access tokens in memory only, no agent-role access to the table |
| Spoofing | a callback binds another user's Atlassian grant | `state` is signed, expires in 10 minutes and names the user who asked ([oauth.md](oauth.md#connect-flow)) |
| Information disclosure | plan text reaches logs | events carry ids and codes only |
| Information disclosure | an owner reads the names of Jira projects and Epics through the access check | accepted: any member runs the check (D6), and it answers with the project name and the Epic summary of any key the account can see; the account's own Jira permissions bound it |
| Information disclosure | a member writes into any Jira project the account can reach | accepted (D9, D6): members choose the target; the account's own Jira permissions bound it (in `oauth` mode those of the member the project's Atlassian connection is bound to) |
| Denial of service | many triggers flood Jira | one pass per project, merged starts, one pass per process, `Retry-After` honoured |
| Elevation of privilege | an agent changes settings | no tool changes settings; the agent routes only start a sync and read status |
