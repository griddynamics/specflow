---
type: Design
title: "Atlassian OAuth connections"
description: "Per-user Atlassian OAuth 2.0 (3LO) connections for the Jira sync: modes, settings, the connect flow, token storage and rotation, the acting account of a target, routes, codes and the Jira tab"
status: stable
tags: [jira, auth]
---
# oauth.md: per-user Atlassian connections (OAuth 2.0 3LO) for the Jira sync

The Jira sync started with one Atlassian account per deployment (D1). The `oauth` mode adds per-user connections: each
person connects their own Atlassian account once, and Specflow then calls Jira as that person, with exactly their Jira
permissions. No shared technical account is added to Jira projects. Decisions D40 to D54 of
[questions.md](questions.md#decisions) belong to this page.

## Modes

| Mode | When | Credentials |
|---|---|---|
| `oauth` | `ATLASSIAN_OAUTH_CLIENT_ID` is not blank | per user, from `atlassian_connections` |
| `token` | the client ID is blank or unset, and `ATLASSIAN_EMAIL` + `ATLASSIAN_API_TOKEN` are set | the one account of the environment (unchanged) |
| off | neither | the Jira routes answer 503 `jira_unavailable` (unchanged) |

`ATLASSIAN_SITE_URL` is required in both modes. Only the client ID turns `oauth` on; the token variables are then not
read at all, so a partial token set next to a client ID is not refused (D47). A secret or a key without the client ID is
ignored. The api logs `api.jira.mode` with `mode` `oauth` or `token` at every start.

## Settings

All are read by the api role only.

| Variable | Rule |
|---|---|
| `ATLASSIAN_SITE_URL` | as in `token` mode: an https origin, no path |
| `ATLASSIAN_OAUTH_CLIENT_ID` | the app's client ID from the Atlassian developer console; turns `oauth` on |
| `ATLASSIAN_OAUTH_CLIENT_SECRET` | required with the client ID, not blank; a secret setting, never logged |
| `ATLASSIAN_TOKEN_KEY` | required with the client ID; a Fernet key (32 bytes in url-safe base64, what `Fernet.generate_key()` returns); encrypts the refresh tokens and derives the `state` signing key |
| `PUBLIC_URL` | required with the client ID; the callback is `{PUBLIC_URL}/api/integrations/atlassian/callback` (a trailing `/` is trimmed) |

A partial OAuth set (a client ID without the site, the secret, the key or `PUBLIC_URL`), or a key that is not a Fernet
key, stops the api at start and names the variable, like any bad variable. How to create the key and register the app:
[../configuration.md](../configuration.md#jira-sync).

One client ID serves every deployment: each deployment's callback URL is registered on the same app (locally
`http://localhost:3001/api/integrations/atlassian/callback`).

Scopes requested (fixed in the code, the set the site admin approved; changing them makes every user consent again):
`read:jira-work write:jira-work read:jira-user read:board-scope:jira-software read:sprint:jira-software
write:sprint:jira-software write:board-scope:jira-software read:page:confluence read:hierarchical-content:confluence
read:folder:confluence read:space:confluence read:attachment:confluence offline_access`. The Confluence scopes are
granted now for a later Confluence source (D45); nothing reads Confluence yet.

## Connect flow

```mermaid
sequenceDiagram
  participant U as Browser
  participant FE as Next.js (BFF)
  participant API as Specflow API
  participant AT as auth.atlassian.com
  U->>FE: Connect Atlassian (Jira tab)
  FE->>API: POST /integrations/atlassian/authorize {return_to}
  API-->>FE: {url} (state signed: user id, return_to, expiry 10 min)
  FE-->>U: navigate to url
  U->>AT: consent
  AT-->>U: 302 {PUBLIC_URL}/api/integrations/atlassian/callback?code&state
  U->>FE: GET callback (signed-in session)
  FE->>API: POST /integrations/atlassian/callback {code, state}
  API->>AT: POST /oauth/token (authorization_code)
  API->>AT: GET accessible-resources; GET /myself on the site's cloud
  API-->>FE: {connection, return_to}
  FE-->>U: 302 return_to?atlassian=connected (or ?atlassian=<code>)
```

Rules:
- The consent URL asks for the fixed scopes with `audience=api.atlassian.com` and `prompt=consent`.
- `state` is `nonce.payload.signature`: the payload is base64url JSON with the user id, `return_to` and the expiry (10
  minutes); the signature is an HMAC-SHA256 with a key derived from `ATLASSIAN_TOKEN_KEY`. No table holds it (D42). The
  callback refuses a bad signature, an expired state or a state of another user (`atlassian_state_invalid`).
- `return_to` is a path of this site: it starts with `/`, not `//`, and has no backslash, whitespace, control or
  non-ASCII character; at most 2048 characters; else 422 `validation_failed` (D52).
- The code exchange: a used, expired or unknown code, or any other refusal of the token endpoint, is
  `atlassian_code_invalid`; `429`, `5xx`, a timeout or a transport error is `atlassian_unreachable`.
- The connection is kept only when the granted resources include the cloud whose `url` equals `ATLASSIAN_SITE_URL`
  (`atlassian_site_not_granted` otherwise). A refused `accessible-resources` call is `atlassian_code_invalid`; a refused
  `/myself` is `atlassian_site_not_granted` (D49). The cloud id, the account id and the display name (`/myself`) are
  stored.
- Atlassian's own `?error=` on the callback (consent denied, the site admin block) never reaches the API: the web app's
  callback route maps it to `?atlassian=atlassian_consent_denied` itself (D49). No Atlassian text is shown or logged.
- Connecting again replaces the row (new grant, new refresh token, `active`).

## Tokens

| Rule | Why |
|---|---|
| The refresh token is stored encrypted (Fernet) in `atlassian_connections`; the access token lives in the api process's memory only, per user, until 60 s before `expires_in` | no plaintext token at rest |
| A refresh runs under a per-user `asyncio.Lock` and a `FOR UPDATE` lock on the row; the rotated refresh token (and `last_used_at`) is committed before the new access token is used. Callers that waited for the lock get the token the first one fetched | Atlassian disables the old refresh token on every rotation; losing the new one logs the user out |
| A Jira `401` refreshes once and sends the call once more; a second `401` is `jira_auth_failed` | an access token can be revoked early |
| `invalid_grant` from the token endpoint (unused for 90 days, password changed, consent revoked) sets `status` `expired` and is `jira_reconnect_required`; an expired connection is never refreshed again until the user reconnects | no retry storm, a clear action for the user |
| A refresh token that no longer decrypts (a rotated `ATLASSIAN_TOKEN_KEY`) counts as `invalid_grant`: `expired`, reconnect (D48) | a key rotation expires every connection |
| Any other refresh failure keeps the connection `active`: `429`, `5xx`, a timeout or a transport error is `jira_unreachable`; any other refusal (a wrong client secret) is `jira_auth_failed` (D48) | a passing outage or a bad deployment setting must not log every user out |
| No background keep-alive refreshes; each refresh resets Atlassian's 90-day inactivity window | a token stays alive only while it is used |
| Access tokens are cached per process: several api replicas each refresh on their own, and the row's `FOR UPDATE` serializes the rotation (D53) | no shared cache to run |
| Disconnect deletes the row and drops the cached access token; a gateway a running pass holds then fails its next call with `jira_not_connected` (D53). Atlassian has no revoke endpoint for 3LO; the Jira tab links to `https://id.atlassian.com/manage-profile/apps` | the user can also remove the grant on Atlassian's side |

## Acting account of a target

In `oauth` mode every Jira call of a project's sync uses the connection of the target's `connected_user_id`:
- Every `PUT /projects/{pid}/jira` (the Sync switch included) sets `connected_user_id` to the saving owner, in both
  modes (D46). In `oauth` mode the owner must have an `active` connection: none, or an `expired` one, is 409
  `jira_not_connected` (D51). Any owner can take a target over by connecting and saving (D40).
- The access check uses the caller's own connection: `reason` `jira_not_connected` or `jira_reconnect_required`, `field`
  null, and no Jira call.
- A pass whose acting connection is missing (or a target no owner saved since `oauth` was turned on) ends `failed` with
  `jira_not_connected`; an expired one with `jira_reconnect_required`; no Jira call is made. Every trigger (plan run,
  MCP, settings, recovery) uses the target's account: a member who runs `sync_to_jira` acts through the owner the
  target names.
- A refresh that fails during a pass ends the pass with the refresh's code ([Tokens](#tokens)).
- In `token` mode `connected_user_id` is stored but ignored, and the deployment account is used (unchanged).

## Data

`atlassian_connections`, one row per Specflow user (migration `0006_atlassian_connections`):

| Column | Type | Rule |
|---|---|---|
| `user_id` | uuid | primary key; foreign key `users.id` on delete cascade |
| `cloud_id` | text | not null |
| `account_id` | text | not null; the Atlassian account id |
| `account_name` | text | not null; display name from `/myself` |
| `scopes` | text | not null; the granted scope string |
| `refresh_token` | bytea | not null; Fernet ciphertext |
| `status` | text | `active` or `expired` (check constraint) |
| `connected_at`, `last_used_at` | timestamptz | not null; both set on connect, `last_used_at` again on each refresh |
| `expired_at` | timestamptz | null unless `expired` |

`jira_targets` gains `connected_user_id uuid null` (foreign key `users.id` on delete set null). Row-level security is
enabled on `atlassian_connections` with no policy and no grant to the agent role. Details:
[data-model.md](data-model.md#migration).

## Routes

Under `/api/v1`, for the signed-in web user; every route acts on the caller's own connection.

| Method, path | Body | Success | Errors |
|---|---|---|---|
| `GET /integrations/atlassian` | - | 200 `AtlassianConnectionOut` | - |
| `POST /integrations/atlassian/authorize` | `{return_to}` | 200 `{url}` | 422 `validation_failed`; 503 `atlassian_oauth_unavailable` (not `oauth` mode) |
| `POST /integrations/atlassian/callback` | `{code, state}` | 200 `{connection: AtlassianConnectionOut, return_to}` | 400 `atlassian_state_invalid`; 400 `atlassian_code_invalid`; 403 `atlassian_site_not_granted`; 502 `atlassian_unreachable`; 422 `validation_failed`; 503 `atlassian_oauth_unavailable` |
| `DELETE /integrations/atlassian` | - | 204 (also when there was no connection) | 503 `atlassian_oauth_unavailable` |

Bodies forbid extra fields; `code` and `state` are 1 to 4096 characters.

`AtlassianConnectionOut`: `mode` (`oauth`, `token`, or null when Jira is off), `connected` (bool), `status` (`active`,
`expired`, or null), `account_name`, `connected_at`, `last_used_at` (null when not connected). `GET` answers in every
mode: in `token` mode and off, `connected` is false and the other fields are null (D50).

`JiraSettingsOut` gains `connection`: null outside `oauth` mode and when the project has no target, else
`{connected_by_name, connected_by_me, status}` for the target's acting account. `status` is `active`, `expired` or
`missing` (no acting user, or the row is gone); `connected_by_name` is the Atlassian display name of the acting
connection, null when `missing`; `connected_by_me` is true when the caller is the acting user (D50).

New codes: the sync and access-check codes `jira_not_connected` and `jira_reconnect_required`
([api.md](api.md#sync-codes)); the route codes above. `sync_to_jira` and `jira_sync_status` carry the sync codes in
`error_code` like the other sync codes; their MCP descriptions tell the agent that the target's owner must connect or
reconnect Atlassian in Settings, tab Jira ([../mcp-server/tools.md](../mcp-server/tools.md#sync_to_jira)).

## Jira tab

The web app's part; the full layout is in [ui.md](ui.md#atlassian-connection).

- A route handler `frontend/app/api/integrations/atlassian/callback/route.ts` (signed-in session required; no session
  goes to sign-in) forwards `code` and `state` to the API and redirects to `return_to` with `?atlassian=connected` or
  `?atlassian=<code>`; Atlassian's `?error=` becomes `?atlassian=atlassian_consent_denied`.
- In `oauth` mode the tab shows, for owners, a connection row above the fields: "Connected to Atlassian as *name*"
  with **Disconnect**, or **Connect Atlassian**, or for an expired one "Your Atlassian connection expired" with
  **Reconnect**. Below the target: "Syncs to Jira as *name*" (or "as you"), and a warning when the acting connection
  is expired or missing.
- Check access and Save are disabled until the owner has an active connection.
- `?atlassian=` shows one toast and is then removed from the URL.
- In `token` mode the tab is unchanged.

## Log events

| Event | Fields |
|---|---|
| `api.jira.mode` | `mode` (`oauth` or `token`), at every start with Jira on |
| `api.atlassian.connected` | `user_id` |
| `api.atlassian.disconnected` | `user_id`, `deleted` (whether there was a row) |
| `api.atlassian.refreshed` | `user_id` |
| `api.atlassian.expired` | `user_id`, `code` (`jira_reconnect_required`) |
| `api.atlassian.callback.refused` | `user_id`, `code` (a route code above) |

They carry the user id and codes only: never a token, a code, `state`, an Atlassian account id or name.
