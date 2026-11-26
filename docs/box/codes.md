---
type: API
title: "Box codes"
description: "Every box_* error code of the API with its HTTP status and meaning, the inside-only codes, and the closed failure reasons a Box import uses"
status: draft
tags: [box, codes]
---
# codes.md: Box codes

`BoxCode` (`box.codes`) is the closed set; `str(BoxError)` is the code. `api.box.box_error` turns it into an `ApiError` with
a fixed sentence. No Box text, token, code, `state`, link, name or e-mail address is ever inside an error or a log line.

## API codes

| Code | HTTP | When |
|---|---|---|
| `box_unavailable` | 503 | the Box app is not configured on this server (`BOX_OAUTH_CLIENT_ID` unset) |
| `box_not_connected` | 409 | the caller (`PUT` integration) or the project (check, add) has no active Box connection |
| `box_reconnect_required` | 409 | the connection expired (`invalid_grant`, a rotated `BOX_TOKEN_KEY`): connect Box again |
| `box_state_invalid` | 400 | the callback `state` is forged, expired, of another user or purpose |
| `box_code_invalid` | 400 | Box did not accept the authorization code |
| `box_unreachable` | 502 | Box is down, slow or throttled past the wait budget, or the network failed |
| `box_auth_failed` | 502 | Box refused the app's own credentials or a fresh token (a setup problem); also any inside-only code that has no sentence of its own |
| `box_link_invalid` | 422 | not a folder, file or shared link on `app.box.com`; All Files; a web link |
| `box_link_password` | 422 | a shared link with a password |
| `box_no_access` | 409 | the bound member's Box account cannot read the folder or file (not found or forbidden) |

The source routes also answer the shared codes of [integrations-api.md](../mission-control/integrations-api.md):
`404 project_not_found`, `404 source_not_found`, `422 validation_failed` (the body is closed; `link` at most 2048 characters).
Order of checks: project member, Box bound and usable (`box_unavailable`, `box_not_connected`, `box_reconnect_required`;
`409 integration_workspace_mismatch` when the bound member reconnected another Box account, D2), link, Box. Adding a source
reads the one folder or file the link names; only the check and the import walk the tree. Binding with an expired Box
connection is `box_reconnect_required`.

## Inside only

| Code | Becomes |
|---|---|
| `box_grant_invalid` | `box_reconnect_required` (refresh) or `box_code_invalid` (sign-in) |
| `box_refused` | `box_code_invalid` (sign-in) or `box_auth_failed` (elsewhere) |
| `box_token_refused` (a `401` on a read) | a refresh and one more try; if it repeats, `box_auth_failed` |
| `box_not_ready` (a `202` that never became a file) | `box_unreachable` |

## Failure reasons of an import

Box adds none (AC24). Source level (`connections.failure_reason`): `no_access`, `unknown_error`. Item level
(`connection_items.error_reason`, `error_step`): `no_access`/`read`, `unknown_error`/`read`, `unknown_error`/`download`,
`convert_failed`/`download`. Skipped items are the state `skipped`, not an error. The mapping: [source.md](source.md#failure-mapping-closed-vocabulary-ac24).
