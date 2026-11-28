---
type: API
title: "Linear routes"
description: "The caller's Linear connection routes, the source, gap channel and plan destination routes, the public webhook and the MCP tools: methods, bodies, shapes and error codes"
status: draft
tags: [linear, api]
---
# api.md: Linear routes

Under `/api/v1`, for the signed-in web user; request bodies forbid extra fields. With the integration off
(`LINEAR_OAUTH_CLIENT_ID` unset) every route below answers 503 `linear_unavailable`, except the `GET` routes that carry
an `available` field (`available` false). The webhook is public. Error codes are listed in
`backend/archkit.toml`; the factories are in `backend/src/api/linear/errors.py`. Shapes are the pydantic models named in
each section.

## Connection routes

`backend/src/api/routers/linear.py`; models in `backend/src/api/linear/schemas.py`. Any signed-in user, for their own
connection.

| Method, path | Body | Success | Errors |
|---|---|---|---|
| `GET /integrations/linear` | - | 200 `LinearConnectionOut` | - |
| `POST /integrations/linear/authorize` | `{return_to}` | 200 `{url}` | 422 `validation_failed`; 503 `linear_unavailable` |
| `POST /integrations/linear/callback` | `{code, state}` (1 to 4096 characters) | 200 `{connection, return_to}` | 400 `linear_state_invalid`; 400 `linear_code_invalid`; 502 `linear_unreachable`; 502 `linear_auth_failed`; 503 `linear_unavailable` |

The per-user `DELETE /integrations/linear` went with the V1 screens; removing the app from a project is
`DELETE /projects/{pid}/integrations/linear` ("Integration health" below).

`LinearConnectionOut`: `available`, `status` (`none`, `active`, `expired`, `revoked`), `user_name`, `workspace`
(`{id, name, url_key}` or null). `revoked` (the workspace withdrew the app, set by the webhook) is shown as `revoked`;
any other status that is not `active` is shown as `expired`. The web app treats both as "Reconnect Linear". A token is
never in an answer. The flow:
[oauth.md](oauth.md#connect-flow).

## Integration health

The project binding (`PUT /projects/{pid}/integrations/linear`, `GET`/`DELETE` of the same path) is the shared
integrations router (`api/routers/integrations.py`, [integrations-api.md](../mission-control/integrations-api.md)). For
`linear` it checks the caller has an `active` connection (409 `linear_not_connected` otherwise; 503 `linear_unavailable`
when off) and stores the workspace id and name. Its health reads `none` (no row or no connection), `active`, or
`reconnect_required` (any status that is not `active`). The binding names the member every role acts as
([oauth.md](oauth.md#the-member-gateway)).

## Source routes

`backend/src/api/routers/source_linear.py`. Any member (D6). Behaviour: [source.md](source.md).

| Method, path | Body | Success | Errors |
|---|---|---|---|
| `POST /projects/{pid}/sources/linear/check` | `{link, exclude?}` | 200 `LinearCheckOut` | see below |
| `POST /projects/{pid}/sources/linear` | `{link, exclude?}` | 201 `BatchSourceOut` (the import starts) | see below |

Body `LinearSourceIn`: `link` (a project link, an issue link or an issue identifier such as `ENG-12`; at most `LINK_MAX`
characters), `exclude` (a list of issue identifiers like `ENG-12`, at most 200; the check ignores it).
`LinearCheckOut`: `kind` (`project` or `issue`), `name`, `issues` (the count, at most 200), `items`
(`{identifier, title, state, parent}`), `capped` (more issues exist than the cap) and `documents` (project documents).

Errors of both: 404 `project_not_found` (non-member); 503 `linear_unavailable`; 409 `linear_not_connected` (no
binding, or no connection of the bound member); 409 `linear_reconnect_required` (the connection is not `active`);
422 `linear_link_invalid` (the value is not a Linear link or identifier); 409 `linear_not_found` (the object or its
workspace is not visible to the member); 409 `linear_forbidden`; 429 `linear_rate_limited`; 502 `linear_unreachable`;
422 `linear_invalid` (any other refusal of Linear).

## Gap channel routes

`backend/src/api/routers/linear_gaps.py`; models in `backend/src/api/linear_gaps/models.py`. Any member (D6).
Behaviour: [gaps.md](gaps.md).

| Method, path | Body | Success | Errors |
|---|---|---|---|
| `GET /projects/{pid}/linear-gaps` | - | 200 `LinearGapsOut` | - |
| `PUT /projects/{pid}/linear-gaps` | `{team_key, linear_project?, grouping, enabled}` | 200 `LinearGapsOut` | see below |
| `POST /projects/{pid}/linear-gaps/access-check` | `{team_key, linear_project?}` | 200 `{result: "ok", team_name, linear_project_name}` or `{result: "failed", reason, field}` | 503 `linear_unavailable`; 503 `gap_unavailable` |

`team_key` is trimmed and upper-cased and must match `^[A-Z][A-Z0-9]{0,14}$`; `linear_project` is a name or a link (at
most 300 characters), a blank value is null; `grouping` is `one` or `area`; `enabled` is a strict boolean.
`LinearGapsOut`: `available`, `enabled`, `team_id`, `team_key`, `linear_project_id`, `grouping`, `parent_identifier`,
`parent_url`, `posted`, `answered`, `error_code`. `GET` answers `available: false` without the integration or without gap
runs.

`PUT` errors: 404 `linear_team_not_found`; 404 `linear_project_not_found`; 409 `linear_not_connected`; 409
`linear_reconnect_required`; 503 `linear_unavailable`; 503 `gap_unavailable`; and the Linear codes of
[the table](#codes). Turning an existing channel off (`enabled` false) makes no Linear call. `PUT` posts the open
questions in the background after the commit. The access check never answers an error for a refused destination: a
failed check is `{result: "failed"}`; `field` is `team_key` or `linear_project`.

## Plan destination routes

`backend/src/api/routers/linear_plan.py`; models in `backend/src/api/linear_plan/models.py`. Any member (D6). The
routes act as the member the project's Linear is bound to, not as the caller. Behaviour: [plan.md](plan.md).

| Method, path | Body or query | Success | Errors |
|---|---|---|---|
| `GET /projects/{pid}/linear-plan` | - | 200 `LinearPlanOut` | - |
| `GET /projects/{pid}/linear-plan/teams` | - | 200 `{workspace, teams: [{id, key, name}]}` | see below |
| `GET /projects/{pid}/linear-plan/projects?team_id=` | `team_id` required | 200 `{projects: [{id, name, url}]}` (not archived) | see below |
| `PUT /projects/{pid}/linear-plan` | `{team_id, project_id?, enabled}` | 200 `LinearPlanOut`; a pass starts when the target was turned on or its team or project changed | see below |
| `POST /projects/{pid}/linear-plan/access-check` | `{team_id, project_id?}` | 200 `{result: "ok", team_key, project_name}` or `{result: "failed", reason, field}` | see below |

`team_id` and `project_id` are 1 to 100 characters; a blank `project_id` is null. With no `project_id` the `PUT` makes
the Linear project itself (named after the Graphico project, id `UUIDv5(project, "project", "-")`, so a retry adopts it)
and records `created_by_graphico` true. There is no `DELETE` route: to stop, save with `enabled` false.

`LinearPlanOut`: `available`, `team_id`, `team_key`, `project_id`, `project_name`, `project_url`,
`created_by_graphico`, `enabled`, `sync` (`LinearSyncStatus` or null), `workspace` (the workspace name of the member
who acts now), `connected_by_me`. Without a target all fields are null or false.

Access-check reasons (`reason`): `linear_not_connected`, `linear_reconnect_required`, `linear_unreachable`,
`linear_forbidden`, `linear_rate_limited`, `linear_team_not_found` (field `team`), `linear_project_not_found` (field
`project`; also when the project does not belong to the team), `linear_invalid`. The check cannot try a write: a missing
write scope shows on the first pass (`linear_forbidden`).

Errors of the other four routes: 409 `linear_not_connected` (no binding); 409 `linear_reconnect_required`; 404
`linear_team_not_found`; 404 `linear_project_not_found` (`PUT`); 503 `linear_unavailable`; and the Linear codes of
[the table](#codes). `PUT` never waits for a pass. No Linear call runs inside a transaction.

## Webhook

`POST /integrations/linear/webhook` (full path `/api/v1/integrations/linear/webhook`). A **public** route: no sign-in
(`public_operations` in `backend/archkit.toml`); the signature of the raw body is the check. Code:
`backend/src/api/routers/linear_webhook.py`, `backend/src/api/linear_gaps/webhook.py`.

| Case | Answer |
|---|---|
| `LINEAR_WEBHOOK_SECRET` not set | 503 `linear_webhook_unavailable` |
| `Linear-Signature` missing, wrong, or `webhookTimestamp` more than 60 s from now, or the body is not a JSON object | 401 `linear_webhook_signature_invalid` |
| Verified | 200 `{"ok": true}` at once; the event is handled in the background |

Events: `Comment` `create` goes to the gap answer path ([gaps.md](gaps.md#answers-one-path-for-the-webhook-and-the-catch-up));
`OAuthApp` `revoked` marks the workspace's (or the acting user's) connections `revoked`; every other event is
acknowledged and ignored. The route reads no Issues events; subscribe the app to Comments and OAuth app authorization events only. Logs:
`api.linear_webhook.received` (`event_type`, `action`) and `api.linear_webhook.rejected` (`reason`); the body is never
logged.

## MCP tools

`backend/src/api/routers/agent_linear.py`, the Linear twins of the Jira pair. The tools, their descriptions and
annotations: [../mcp-server/tools.md](../mcp-server/tools.md). Registered in `backend/src/mcp_server/registry.py` and
`backend/src/agent_contracts/routes.py`. Any member may call both; the call acts as the member the project's Linear is
bound to, not as the token's user (`user_id` goes only to the log).

| Tool | Route | Body or query | Success | Errors |
|---|---|---|---|---|
| `sync_to_linear` | `POST /agent/linear-sync` | project only | 200 `LinearSyncStatus` (`status` `running`, `queued` true when merged into a running pass) | 409 `linear_not_configured` (no target, or the target is off); 503 `linear_unavailable` (off, or the runner is closing) |
| `linear_sync_status` | `GET /agent/linear-sync-status` | `limit`, `cursor` | 200 `LinearSyncReport`: the status plus one page of `items` and `next_cursor` | 409 `linear_not_configured` (no target; a disabled target is shown); `invalid_cursor` |

`LinearSyncStatus` (`backend/src/agent_contracts/linear_models.py`): `project_id`, `enabled`, `status` (`idle`,
`running`, `succeeded`, `partial`, `failed`, `skipped`), `queued`, `trigger`, `started_at`, `finished_at`, `error_code`,
`counts` (`created`, `updated`, `commented`, `removed`, `relations_added`, `estimate_refused`, `unchanged`, `failed`),
`team_key`, `linear_project`, `linear_project_url`, `progress` (`{step: issues|relations, done, total}`, only while a pass
runs). `LinearSyncItem`: `node_id`, `kind` (`story`, `subtask`), `identifier`, `url`, `removed`, `error_code`; items are
ordered by kind then node id, and the cursor holds the sort key of the last item.

## Codes

All closed codes of the Linear routes, with `backend/archkit.toml`:

| Code | HTTP | Meaning |
|---|---|---|
| `linear_unavailable` | 503 | the Linear OAuth app is not configured on this server |
| `linear_not_connected` | 409 | the member, or the project's binding, has no Linear connection |
| `linear_reconnect_required` | 409 | the connection is `expired` or `revoked`, or Linear refused the token twice |
| `linear_state_invalid` | 400 | the sign-in `state` is of another user or too old |
| `linear_code_invalid` | 400 | Linear did not accept the authorization code |
| `linear_unreachable` | 502 | Linear is down, timed out, or limited the token endpoint |
| `linear_auth_failed` | 502 | Linear refused the server's app credentials |
| `linear_forbidden` | 409 | the member's role or the app's scopes do not allow the call |
| `linear_not_found` | 409 | the Linear object does not exist or is not visible to the member |
| `linear_rate_limited` | 429 | Linear limits requests; carries `Retry-After` (seconds, at least 1) when Linear sent one |
| `linear_invalid` | 422 | Linear refused the input; `details.field` names it when known |
| `linear_team_not_found` | 404 | no such team for the member |
| `linear_project_not_found` | 404 | no such Linear project for the member (or not of the team) |
| `linear_link_invalid` | 422 | the value is not a Linear project link, issue link or identifier |
| `linear_not_configured` | 409 | the project has no enabled Linear target (MCP routes) |
| `linear_webhook_signature_invalid` | 401 | the webhook signature is missing, wrong or too old |
| `linear_webhook_unavailable` | 503 | `LINEAR_WEBHOOK_SECRET` is not set |

Codes that only appear in a pass status or in a gap target (not as an HTTP answer): see [plan.md](plan.md#codes) and
[gaps.md](gaps.md).

## Log events

`api.linear.callback.refused` (`user_id`, `code`), `api.linear_plan.saved` (`user_id`, `project_id`, `enabled`,
`destination_changed`), `api.linear_plan.access_checked` (`user_id`, `project_id`, `result`, `reason`),
`api.linear_gaps.saved`, `api.linear_gaps.access_checked`, `api.imports.added` (`kind` `linear_project`, `excluded`),
`api.linear_webhook.received`, `api.linear_webhook.rejected`. Ids, counts and codes only.
