---
type: Design
title: "Box discovery"
description: "What Box documents for a server-side OAuth connection and a folder importer: scopes, tokens, admin gates, rate limits, folders, files, shared links, Box Notes, representations, metadata, comments, change tracking; each fact with its source, and what is unverified"
status: draft
tags: [box, discovery]
---
# discovery.md: facts about Box, 2026-11-25

Researched on 2026-11-25 from the official Box developer documentation (`developer.box.com`) and the Box support site
(`support.box.com`). A line marked **UNVERIFIED** was not confirmed on an official page. The decisions that follow from these
facts are in [oauth.md](oauth.md) and [source.md](source.md). The base URL of the REST API is `https://api.box.com/2.0`.

## App type and who can create one

- A **Platform App** of type **User** (OAuth 2.0) sends the user to Box to sign in. The type cannot be changed later.
  Create it in the Developer Console (`https://app.box.com/developers/console`): New App, User, Create, then the
  Configuration tab: OAuth 2.0 Redirect URIs, Application Scopes.
  Source: https://developer.box.com/guides/authentication/oauth2/oauth2-setup
- Anyone with Developer Console access in their Box enterprise, or a free developer account
  (https://account.box.com/signup/developer), can create one. The free developer plan allows 10,000 API calls a month.
  Source: https://developer.box.com/platform/free-developer-plan
- The client secret is visible only to an account with multi-factor authentication on.
  Source: https://developer.box.com/guides/authentication/select
- **App access level** (App Access Only, App + Enterprise Access) applies only to JWT and CCG apps. For OAuth 2.0 the token is
  the signing-in user and the content it reaches is what that user can reach, cut by the app scopes.
  Source: https://developer.box.com/guides/security
- Redirect URI rules: exact match; HTTPS, or HTTP for localhost and loopback (any port); several URIs are allowed, and then
  the authorize URL must send `redirect_uri`. A mismatch shows the user `redirect_uri_mismatch`.
  Source: https://developer.box.com/guides/authentication/oauth2/oauth2-setup

## Scopes

Source: https://developer.box.com/guides/api-calls/permissions-and-errors/scopes

| Console label | Scope | Meaning |
|---|---|---|
| Read all files and folders | `root_readonly` | read files and folders of the user |
| Read and write all files and folders | `root_readwrite` | adds uploads, collaborations, comments, tasks. "Necessary to download files and folders" |
| Manage webhooks | `manage_webhook` | webhooks, 1000 per app and user |
| Manage enterprise properties | `manage_enterprise_properties` | enterprise event stream, attributes, reports |
| Manage AI | `ai.readwrite` | Box AI API |

- **Download needs `root_readwrite`.** A token with `root_readonly` gets `403 access_denied_insufficient_permissions` on
  `GET /files/{id}/content`; Box staff call this expected
  (https://support.box.com/hc/en-us/community/posts/360052262573). The console label says "Read and write" and Box shows that
  label to the user on the consent page. Graphico asks for `root_readwrite` and never writes ([oauth.md](oauth.md)).
- The authorize URL accepts `scope` (space separated); without it the user gets all scopes configured on the app. `scope` can
  only narrow what the app has. Source: https://developer.box.com/reference/get-authorize
- No page maps a scope to a read endpoint other than download (metadata, comments, tasks, versions, representations).
  **UNVERIFIED** per endpoint; `root_readonly` is expected to cover them.

## Admin gates

- An OAuth 2.0 app needs no admin authorization. It needs a Box Admin or Co-Admin to **enable** it only when the enterprise
  turned on "Disable unpublished platform apps by default". Admin Console, Platform Apps (older articles: Integrations,
  Platform Apps Manager): add the app by Client ID and enable it. A blocked user sees "Disabled by administrator" with the
  Client ID. Sources: https://developer.box.com/guides/security ,
  https://developer.box.com/guides/authorization/platform-app-approval ,
  https://support.box.com/hc/en-us/articles/360044195053 ,
  https://support.box.com/hc/en-us/articles/49348283011731
- An enterprise can also disable all integrations ("Enable Integrations by default" off). Source:
  https://support.box.com/hc/en-us/articles/360044195293
- Another customer's enterprise applies the same rule through its own admin. No official page walks through that case.
  **UNVERIFIED** (it follows from the rule above). A personal account has no Admin Console and no gate (**UNVERIFIED**).
- Publishing in Box Integrations is optional, goes through Box review and is not needed. Source:
  https://developer.box.com/guides/applications/integrations/index

## Tokens

Sources: https://developer.box.com/guides/authentication/tokens ,
https://developer.box.com/guides/authentication/tokens/refresh , https://developer.box.com/reference/post-oauth2-token

- Authorization code: valid 30 seconds. Access token: 60 minutes. Refresh token: 60 days **or one use**.
- Every refresh returns a new access token **and a new refresh token**; the old refresh token is dead. A grace period is
  not documented (community reports conflict). A reused old token answers `400 invalid_grant`. **UNVERIFIED** grace.
- Code exchange and refresh: `POST /oauth2/token`, form encoded, `grant_type`, `client_id`, `client_secret`, and `code` or
  `refresh_token`. The documented body has no `redirect_uri`. The answer has `access_token`, `expires_in`, `token_type`,
  `refresh_token`, `restricted_to`, and no user id and no scope. A failure is `400` with `{error, error_description}`.
- PKCE is not documented (no `code_challenge` on authorize, no `code_verifier` on token). **UNVERIFIED** in practice.
- Revoke: `POST /oauth2/revoke` with `client_id`, `client_secret`, `token`; 200 with an empty body. Whether revoking one
  token ends its pair is not stated. Source: https://developer.box.com/guides/authentication/tokens/revoke
- Identity: `GET /users/me` is the user who authorized the app. `fields=id,name,login,enterprise` adds the enterprise
  (`{id, type, name}`). `login` is the e-mail address. **UNVERIFIED**: `enterprise` is null for a personal account.
  Source: https://developer.box.com/reference/get-users-me

## Errors

Source: https://developer.box.com/guides/api-calls/permissions-and-errors/common-errors

`400 invalid_grant` (code expired, refresh token invalid or used), `400 invalid_request` (grant type unauthorized for the
client), `401 invalid_token` (access token expired or revoked: refresh), `403 insufficient_scope`,
`403 access_denied_insufficient_permissions` (scope or user permission), `403 forbidden_by_policy` (Shield),
`404 not_found`.

## Rate limits and plans

Sources: https://developer.box.com/guides/api-calls/permissions-and-errors/rate-limits , https://www.box.com/pricing

- Per user: about 1000 API calls a minute; uploads 240 a minute; search 6 a second and 60 a minute. No download limit is
  documented. A throttled call is `429` with `Retry-After` (seconds) and a `rate_limit_exceeded` body.
- A monthly allowance counts per enterprise, including authentication calls from custom apps: Business and Business Plus
  50,000, Enterprise and Enterprise Plus 100,000, Enterprise Advanced 200,000 (one card says 100,000), free developer 10,000;
  more can be bought. What happens at the limit and whose allowance an external user's calls use: **UNVERIFIED**.
- Box AI calls use AI Units, counted apart from API calls.

## Folders, files and shared links

- List items: `GET /folders/{id}/items` (root is `0`). `limit` at most 1000; offset or marker paging (`usemarker=true`,
  `next_marker`); folders come first, then files, then web links; `fields` limits the answer to the mini form plus the named
  fields. No recursive list exists: walk the tree. Source: https://developer.box.com/reference/get-folders-id-items/
- File info: `GET /files/{id}`, with `size`, `extension`, `sha1`, `item_status` (`active`, `trashed`, `deleted`).
  Source: https://developer.box.com/reference/get-files-id/
- Download: `GET /files/{id}/content` answers `302` to a short-lived `dl.boxcloud.com` URL (not to be stored), or `202` with
  `Retry-After` when the file is not ready. A `range` header and `version` are supported. Source:
  https://developer.box.com/reference/get-files-id-content/
- Shared link: `GET /shared_items` with the header `boxapi: shared_link=<url>[&shared_link_password=<pw>]` answers the
  folder, file or web link; the same header on the normal endpoints reads inside a shared folder. Sources:
  https://developer.box.com/reference/get-shared-items/ , https://developer.box.com/reference/get-shared-items--folders/
- A web link is a bookmark (`GET /web_links/{id}`, field `url`); it holds no file content.
- Versions, trash: `item_status != active` is a deleted item. Plan limits on kept versions differ (1 to unlimited).

## Box Notes

A `.boxnote` is a proprietary JSON (ProseMirror) file. The Notes API only creates notes from Markdown; it has no read or
export call. The download returns the raw JSON with an undocumented structure. The **`[extracted_text]` representation
supports `.boxnote`** (and `.boxcanvas`); the PDF, thumbnail and Markdown representations do not. Source:
https://developer.box.com/guides/representations/supported-file-types/ , https://developer.box.com/guides/box-notes

## Representations

`GET /files/{id}?fields=representations` with header `x-rep-hints: [<type>]` lists the representation with `info.url`,
`content.url_template` (opaque, contains `{+asset_path}`) and `status.state` (`success`, `pending`, `none`, `viewable`).
`[extracted_text]`: `asset_path` is empty, no images, not generated over 500 MB. `[markdown]` exists for DOCX, PPTX, XLS(X),
PDF and Google files (not `.boxnote`, `.doc`, `.ppt`, `.txt`). Source: https://developer.box.com/guides/representations/

## Metadata, comments, tasks

- Metadata: `GET /files/{id}/metadata` (not paged, up to 100 templates per item); `fields=metadata.<scope>.<template>` inline;
  templates by `GET /metadata_templates/enterprise`. Source: https://developer.box.com/guides/metadata/
- Comments: `GET /files/{id}/comments` (`limit` up to 1000); tasks: `GET /files/{id}/tasks` (not paged). Author fields hold
  `login`, the e-mail address. Sources: https://developer.box.com/reference/get-files-id-comments/ ,
  https://developer.box.com/reference/get-files-id-tasks/

## Change tracking

User event stream (`GET /events`, retention two weeks to two months, duplicates possible) and webhooks v2 (scope
`manage_webhook`, public HTTPS endpoint, signature headers, no root folder, auto-deleted after 30 days without a delivery).
The enterprise stream needs an admin and `manage_enterprise_properties`. Not used: import once (D7).
Sources: https://developer.box.com/guides/events , https://developer.box.com/guides/webhooks/v2

## Box AI and the Box MCP server

Box AI (`/ai/ask`, `/ai/extract_structured`, scope `ai.readwrite`) spends the customer's AI Units; it is not source text. The
Box MCP server (`https://mcp.box.com`) needs admin-created integration credentials in each customer enterprise and returns
text; a Platform App token is not documented as usable there. Neither is used by the importer.
Sources: https://developer.box.com/guides/box-ai , https://developer.box.com/guides/box-mcp

## Box SDK

`boxsdk>=10` (package `box_sdk_gen`) is synchronous (`requests`), refreshes only after a `401`, and cannot share one rotating
refresh token across workers safely. Not used (decision B1): the integration has its own async `httpx` client like GitHub's.
Source: https://pypi.org/project/boxsdk/

## Open points

These cannot be settled without the owner's Box app keys; each has a default in the code and is the first thing to check in
the live test.

1. How Box answers `GET /shared_items` for a password-protected link given without the password. The default maps `403` and
   `404` to `box_no_access`; `box_link_password` is used only when the answer says the link needs a password.
2. Whether `root_readonly` is enough for the reads other than download. The app asks for `root_readwrite` anyway.
3. Whether `GET /users/me` answers `enterprise: null` for a personal account (the code treats a missing enterprise as personal).
4. Whether a rotated refresh token has a grace period. The code assumes none (the safe case).
5. Whose monthly API allowance an external user's calls use.
