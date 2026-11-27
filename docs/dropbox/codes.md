---
type: API
title: "Dropbox codes"
description: "Every dropbox_* error code of the API with its HTTP status and meaning, the inside-only codes of the client, and the closed failure reasons a Dropbox import uses"
status: draft
tags: [dropbox, codes]
---
# codes.md: Dropbox codes

`DropboxCode` (`dropbox.codes`) is the closed set; `str(DropboxError)` is the code. `api.dropbox.dropbox_error` turns it into
an `ApiError` with a fixed sentence. No Dropbox text, token, code, `state`, link, path, name or e-mail address is ever inside
an error or a log line. A refused call is a `DropboxApiError` that also carries the tags of Dropbox's `error_summary`
(`("path", "not_found")`): identifiers only, never a path or a message.

## API codes

| Code | HTTP | When |
|---|---|---|
| `dropbox_unavailable` | 503 | the Dropbox app is not configured on this server (`DROPBOX_OAUTH_CLIENT_ID` unset) |
| `dropbox_not_connected` | 409 | the caller (`PUT` integration) or the project (check, add) has no Dropbox connection |
| `dropbox_reconnect_required` | 409 | the connection expired (`invalid_grant`, a rotated `DROPBOX_TOKEN_KEY`): connect Dropbox again |
| `dropbox_state_invalid` | 400 | the callback `state` is forged, expired, of another user or purpose |
| `dropbox_code_invalid` | 400 | Dropbox did not accept the authorization code (or answered no refresh token) |
| `dropbox_unreachable` | 502 | Dropbox is down, slow or throttled past the wait budget, or the network failed |
| `dropbox_auth_failed` | 502 | Dropbox refused the app's own credentials or a fresh token (a setup problem); also any inside-only code that has no sentence of its own |
| `dropbox_link_invalid` | 422 | not a Dropbox shared link or `/home` path on `www.dropbox.com`; a password-protected link; the Dropbox root; another host |
| `dropbox_no_access` | 409 | the bound member's Dropbox cannot read the folder or file, or it is not in their Dropbox (no `path_lower`): add it first |

The source routes also answer the shared codes of [integrations-api.md](../mission-control/integrations-api.md):
`404 project_not_found`, `404 source_not_found`, `409 integration_workspace_mismatch` (the bound member reconnected another
Dropbox account, D2), `422 validation_failed` (the body is closed; `link` at most 2048 characters; `exclude` at most 1000 ids
`id:...`). Order of checks: project member, Dropbox bound and usable, link, Dropbox.

## Inside only

| Code | Raised by | Becomes |
|---|---|---|
| `dropbox_grant_invalid` | `/oauth2/token` `400 invalid_grant` | `dropbox_reconnect_required` (refresh) or `dropbox_code_invalid` (sign-in) |
| `dropbox_refused` | any other refusal (`400`, an unknown `409`, a `422` that is not `invalid_root`), or an answer this code cannot read | sign-in: `dropbox_code_invalid`; refresh: `dropbox_auth_failed`; target: `dropbox_no_access`, `dropbox_link_invalid` (`malformed_path`, `shared_link_malformed`) or `dropbox_unreachable` (`409 reset`, an expired listing cursor) |
| `dropbox_token_refused` | `401` `expired_access_token` or `invalid_access_token` (recorded body `{"error":{".tag":"expired_access_token"}}`) | a refresh and one more try; if it repeats, `dropbox_auth_failed` |
| `dropbox_invalid_root` | `422 invalid_root` for the Path-Root header | read the root namespace once more and repeat; a second one is `dropbox_auth_failed` |
| `dropbox_not_found` | `409` with `not_found` or `shared_link_not_found` | target: `dropbox_no_access`; item: `no_access`/`read` |
| `dropbox_restricted` | `409` with `restricted_content`, `no_permission`, `shared_link_access_denied` (a password link), `403` | target: `dropbox_no_access` or (`shared_link_access_denied`) `dropbox_link_invalid`; item: `no_access`/`read` |
| `dropbox_unsupported` | `409` with `unsupported_file`, `non_exportable`, `invalid_export_format`, `unsupported_link_type`, `unsupported_extension` | target: `dropbox_link_invalid` (`unsupported_link_type`) or `dropbox_no_access`; item: a file Dropbox cannot download is `skipped`, a Paper export is `convert_failed`/`convert` (a Paper doc without a Markdown export option is `skipped` by the listing, without a call) |

A `401` with another tag (`missing_scope`, `user_suspended`) is `dropbox_auth_failed` without a refresh.

## Failure reasons of an import

Dropbox adds none (AC24). Source level (`connections.failure_reason`): `no_access`, `unknown_error`. Item level
(`connection_items.error_reason`, `error_step`): `no_access`/`read`, `unknown_error`/`download`, `convert_failed`/`download`,
`convert_failed`/`convert`. Skipped items are the state `skipped`, not an error. The mapping:
[source.md](source.md#failure-mapping-closed-vocabulary-ac24).
