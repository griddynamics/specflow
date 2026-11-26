---
type: Design
title: "Dropbox discovery"
description: "What Dropbox documents for a server-side OAuth connection and a folder importer: app console, OAuth, scopes, endpoints, shared links, team spaces, Paper, rate limits, webhooks, branding; each fact with its source, the live exploration of 2026-11-25, and what is unverified"
status: draft
tags: [dropbox, discovery]
---
# discovery.md: facts about Dropbox, 2026-11-25

Researched on 2026-11-25 from the official Dropbox documentation, which moved to `docs.dropboxapi.com` on 2026-09-21 (old
`developers.dropbox.com` and `www.dropbox.com/developers/reference` URLs redirect there). A line marked **UNVERIFIED** was not
confirmed on an official page or live. The decisions that follow are in [oauth.md](oauth.md) and [source.md](source.md); the
raw research is in `plans/DROPBOX/research/` (`dropbox-docs.md`, `explore-findings.md`, `explore.py`).

## App console

- Scoped access is the only choice for a new app; legacy permission types keep working for old apps
  ([oauth](https://docs.dropboxapi.com/dropbox-api/docs/oauth)).
- **App folder** acts only in `/Apps/<name>`; **Full Dropbox** on all the account's data. Changing it needs a new app
  ([app console](https://docs.dropboxapi.com/dropbox-api/docs/get-started/tutorial/app-console)).
- Development status: the creator links first, more users under "Enable additional users", up to 500; after 50 linked users,
  two weeks to get production approval or linking freezes. "Apply for production" needs a branding icon; the name cannot change
  after approval ([developer guide](https://docs.dropboxapi.com/dropbox-api/docs/developer-resources/developer-guide)).
- "Allow public clients" covers PKCE and the implicit grant; off is safer when neither is used.
- Redirect URIs must match exactly and be https, except `localhost` ([authorization](https://docs.dropboxapi.com/dropbox-api/docs/get-started/authorization)).
- Branding: the app name must not include "Dropbox", start with "drop" or use "DBX", "DB", "DfB"
  ([branding guide](https://docs.dropboxapi.com/dropbox-api/docs/developer-resources/branding-guide)).

## OAuth

- `https://www.dropbox.com/oauth2/authorize`: `client_id`, `response_type=code`, `redirect_uri`, `state` (up to 2000 bytes),
  `token_access_type=offline` (a refresh token; `online` is the default), `scope` (space separated, a subset of the console's),
  `include_granted_scopes`, PKCE parameters, `force_reapprove` ([authorization](https://docs.dropboxapi.com/dropbox-api/docs/get-started/authorization)).
- `POST https://api.dropboxapi.com/oauth2/token`: code exchange (`code`, `grant_type=authorization_code`, `redirect_uri`,
  credentials) and refresh (`grant_type=refresh_token`). Tokens are opaque and "capable of exceeding 1KB". `expires_in` is
  returned (every sample shows 14400 s, 4 hours; read it, do not assume it).
- **Refresh tokens do not rotate** (a refresh answers no new one) and do not expire unless `refresh_token_expiration_seconds`
  was set; valid while the user's approval stands.
- `POST https://api.dropboxapi.com/2/auth/token/revoke` disables the access token, its refresh token and every other access
  token of it ([revoke](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/auth/token-revoke)).
- A user who unlinks the app: calls answer 401 ([error handling](https://docs.dropboxapi.com/dropbox-api/docs/error-handling)).
  **UNVERIFIED**: the exact `/oauth2/token` answer after an unlink (expected `invalid_grant`).

## Scopes

| Endpoint | Scope |
|---|---|
| `files/list_folder`, `/continue`, `files/get_metadata` | `files.metadata.read` |
| `files/download`, `files/export` | `files.content.read` |
| `sharing/get_shared_link_metadata` | `sharing.read` |
| `users/get_current_account` | `account_info.read` (must be enabled for user-linked apps) |

Sources: [list_folder](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/files/list-folder),
[get_metadata](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/files/get-metadata),
[download](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/files/download),
[export](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/files/export),
[get_shared_link_metadata](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/sharing/get-shared-link-metadata),
[get_current_account](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/users/get-current-account).
Scopes added in the console later do not affect issued tokens ([scopes blog](https://dropbox.tech/developers/customizing-scopes-in-oauth-flow)).

## Endpoints and formats

- RPC endpoints take a JSON body on `api.dropboxapi.com`; content-download endpoints take the argument in the `Dropbox-API-Arg`
  header and answer the metadata in `Dropbox-API-Result`, on `content.dropboxapi.com`. Every call is a `POST`. In
  `Dropbox-API-Arg`, non-ASCII characters and 0x7F must be `\uXXXX`-escaped
  ([formats](https://docs.dropboxapi.com/dropbox-api/docs/technical-reference/request-response-formats)).
- Errors: `400` bad input (text), `401` auth (`{"error": {".tag": "expired_access_token"}}`), `403` no access to the endpoint,
  `409` endpoint-specific (`error_summary` like `path/not_found/..`), `429` rate limit, `5xx`
  ([error handling](https://docs.dropboxapi.com/dropbox-api/docs/error-handling)).
- `files/list_folder`: `path` (`""` = root), `recursive`, `limit` (approximate), `include_non_downloadable_files` (default
  true), `shared_link` (non-recursive only). `recursive` "may lead to performance issues or errors" on big trees. Keep calling
  `continue` while `has_more`; cursors may expire (`409 reset`) ([detecting changes](https://docs.dropboxapi.com/dropbox-api/docs/detecting-changes)).
- File metadata: `id` (stable, case sensitive), `rev`, `server_modified`, `size`, `content_hash`, `is_downloadable`; when it is
  false, `export_info` (`export_as`, `export_options`). Paths are case-insensitive.
- `content_hash`: SHA-256 of the concatenated SHA-256 digests of 4 MiB blocks
  ([content hash](https://docs.dropboxapi.com/dropbox-api/docs/technical-reference/content-hash)); not the plain SHA-256 the
  project stores.
- `files/export` works only for files with `export_as`; `export_format` must be one of `export_options`. `files/download` on
  such a file answers `unsupported_file`. Non-downloadable: Google Docs/Sheets/Slides, Paper, cloud-edited Office
  ([file access](https://docs.dropboxapi.com/dropbox-api/docs/file-access)). **UNVERIFIED**: the format strings of a `.paper`
  file (a 2021 blog says Markdown, HTML or plain text, [paper endpoints](https://dropbox.tech/developers/new-paper-endpoints-released-in-preview)).
- Paper docs of new users since 2019 are `.paper` files read with `/files/*`; the legacy `/paper/*` API stays until the
  migration ends ([paper migration](https://docs.dropboxapi.com/dropbox-api/docs/paper-migration)).
- Download size limit: not documented.

## Shared links

- `sharing/get_shared_link_metadata` takes `url`, optional `link_password` and `path`; App or User auth. Shared links are
  public by default ([sharing](https://docs.dropboxapi.com/dropbox-api/docs/sharing)). Errors: `shared_link_not_found`,
  `shared_link_access_denied`, `unsupported_link_type`.
- `path_lower` is present only when the caller has the item in their Dropbox (live: present for the owner's link; per the docs
  absent for someone else's).
- `files/list_folder` with `shared_link` is non-recursive only (live: recursive answers 400).

## Team spaces

- Calls are rooted in the member's home folder by default; without `Dropbox-API-Path-Root` the app cannot reach the team space
  ([team files](https://docs.dropboxapi.com/dropbox-api/docs/team-files)).
- `{".tag": "root", "root": "<root_namespace_id>"}` targets the user's root; a changed root answers `invalid_root` with the new
  root info ([path root modes](https://docs.dropboxapi.com/dropbox-api/docs/technical-reference/path-root-header-modes)).
- `users/get_current_account` `root_info` has `.tag` `user` or `team`, `root_namespace_id`, `home_namespace_id`; updated team
  spaces also report `user`, so the header is always sent ([team space changes](https://dropbox.tech/developers/api-updates-to-better-support-team-spaces)).
- Admins can block apps; blocking does not disconnect connected members ([admin help](https://help.dropbox.com/teams-admins/admin/app-integrations)).

## Rate limits

- Per authorization (per linked user); numbers not published ([performance](https://docs.dropboxapi.com/dropbox-api/docs/performance)).
- `429` `too_many_requests` or `too_many_write_operations`. The performance guide says a 429 "always" has `Retry-After`; the
  error guide says it "may", with exponential backoff otherwise. Graphico handles both. Retry `5xx` with backoff.
- Parallel `list_folder` calls of the same tree can be throttled.

## Webhooks (not used)

A public URI with a `challenge` echo; notifications name accounts only; `X-Dropbox-Signature` is HMAC-SHA256 with the app secret
([webhooks](https://docs.dropboxapi.com/dropbox-api/docs/webhooks)). Sync is a follow-up.

## Live exploration (2026-11-25)

Dev app "Graphico Local" (scoped, Full Dropbox, the four read scopes), the owner's `basic` account, sample folder
"Graphico Lab" (1 folder, an HTML of 89 KB, a PDF of 592 KB, a PNG of 332 KB):

- Account: `root_info.tag=user`, home = root namespace, no team; feature `paper_as_files` on.
- `sharing/get_shared_link_metadata` of the owner's `/scl/fo/` link: 200, folder, `id`, `path_lower`.
- Recursive `files/list_folder` by path with `include_non_downloadable_files`: every file has `id`, `rev`, `content_hash`,
  `server_modified`, `is_downloadable=true`; cursor 238 characters.
- `files/download` by `id`: 200; `Content-Type` always `application/octet-stream`; the block hash of the bytes equals
  `content_hash`, the plain SHA-256 does not.
- A `401` body was `{"error": {".tag": "expired_access_token"}, ...}`. No 429 in 21 calls; no `Retry-After` header on any answer.
- Not covered (no such file; the app cannot write): DOCX/PPTX/XLSX/MD/CSV, a Paper export, duplicates, a nested subfolder,
  more than 50 MiB, a team space, a link of another account. Task L0 records them after the owner adds them.

## Open points

1. **Paper export shape** (format list, headers, first bytes): recorded by L0; without it Paper ships `skipped` (DB10).
2. **Team space** (Path-Root on a Business account): documented shapes only; live check listed for W9 if a Business account is
   available.
3. **Password-protected link**: expected `409 shared_link_access_denied` (mapped to `dropbox_link_invalid`); unverified live.
4. **Refresh after an unlink**: expected `400 invalid_grant` (mapped to `expired`); unverified live.
