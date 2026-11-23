---
type: Design
title: "Atlassian OAuth connections"
description: "Per-user Atlassian OAuth 2.0 (3LO) connections for the Jira sync: modes, settings, the connect flow, token storage and rotation, the acting account of a target, routes, codes and the Jira tab"
status: draft
tags: [jira, auth]
---
# oauth.md: per-user Atlassian connections (OAuth 2.0 3LO) for the Jira sync

The Jira sync started with one Atlassian account per deployment (D1). This adds per-user connections: each person
connects their own Atlassian account once, and Specflow then calls Jira as that person, with exactly their Jira
permissions. No shared technical account is added to Jira projects.

## Modes

| Mode | When | Credentials |
|---|---|---|
| `oauth` | `ATLASSIAN_OAUTH_CLIENT_ID` is set | per user, from `atlassian_connections` |
| `token` | the OAuth variables are not set and `ATLASSIAN_EMAIL` + `ATLASSIAN_API_TOKEN` are | the one account of the environment (unchanged) |
| off | neither | the Jira routes answer 503 `jira_unavailable` (unchanged) |

`ATLASSIAN_SITE_URL` is required in both modes. When both sets are present, `oauth` wins and the api logs
`api.jira.mode` with `mode=oauth`; the token is not used.

## Settings (api role)

| Variable | Rule |
|---|---|
| `ATLASSIAN_SITE_URL` | as today: https origin, no path |
| `ATLASSIAN_OAUTH_CLIENT_ID` | the app's client ID from the Atlassian developer console; turns `oauth` on |
| `ATLASSIAN_OAUTH_CLIENT_SECRET` | required with the client ID; a `SecretStr`, never logged |
| `ATLASSIAN_TOKEN_KEY` | required with the client ID; a Fernet key (`cryptography.fernet.Fernet.generate_key()`), encrypts refresh tokens and signs `state` |
| `PUBLIC_URL` | required with the client ID; the callback is `{PUBLIC_URL}/api/integrations/atlassian/callback` |

A partial OAuth set (client ID without secret, key or `PUBLIC_URL`) is refused at startup like any bad variable.
One client ID serves every deployment: each callback URL is registered on the same app (local
`http://localhost:3001/...`, Evergreen `https://specflowgr-frontend-prod.evergreen.gcp.griddynamics.net/...`).

Scopes requested (fixed, the set the site admin approved; changing them makes every user consent again):
`read:jira-work write:jira-work read:jira-user read:board-scope:jira-software read:sprint:jira-software
write:sprint:jira-software write:board-scope:jira-software read:page:confluence read:hierarchical-content:confluence
read:folder:confluence read:space:confluence read:attachment:confluence offline_access`.

## Connect flow

```mermaid
sequenceDiagram
  participant U as Browser
  participant FE as Next.js (BFF)
  participant API as Specflow API
  participant AT as auth.atlassian.com
  U->>FE: Connect Atlassian (Jira tab)
  FE->>API: POST /integrations/atlassian/authorize {return_to}
  API-->>FE: {url} (state signed: user id, nonce, return_to, expiry 10 min)
  FE-->>U: navigate to url
  U->>AT: consent
  AT-->>U: 302 {PUBLIC_URL}/api/integrations/atlassian/callback?code&state
  U->>FE: GET callback (signed-in session)
  FE->>API: POST /integrations/atlassian/callback {code, state}
  API->>AT: POST /oauth/token (authorization_code)
  API->>AT: GET accessible-resources; GET /myself on the site's cloud
  API-->>FE: connection + return_to
  FE-->>U: 302 return_to?atlassian=connected (or ?atlassian=<code>)
```

Rules:
- `state` is `nonce.payload.signature` signed with an HMAC key derived from `ATLASSIAN_TOKEN_KEY`; the callback
  refuses a bad signature, an expired state or a state of another user (`atlassian_state_invalid`).
- `return_to` is a same-origin path: it starts with `/`, not `//`, and has no scheme; else 422.
- The connection is kept only when the granted resources include the cloud whose `url` equals `ATLASSIAN_SITE_URL`
  (`atlassian_site_not_granted` otherwise). The cloud id, the account id and the display name (`/myself`) are stored.
- Atlassian's own errors on the callback (`error=access_denied`, the site admin block) reach the Jira tab as
  `?atlassian=atlassian_consent_denied`; no Atlassian text is shown or logged.
- Connecting again replaces the row (new grant, new refresh token).

## Tokens

| Rule | Why |
|---|---|
| The refresh token is stored encrypted (Fernet) in `atlassian_connections`; the access token lives in memory only, per user, until 60 s before `expires_in` | no plaintext token at rest |
| A refresh runs under a per-user `asyncio.Lock` and a `FOR UPDATE` lock on the row; the rotated refresh token is committed before the new access token is used | Atlassian disables the old refresh token on every rotation; losing the new one logs the user out |
| A Jira `401` with a fresh access token refreshes once and retries the call once; a second `401` is `jira_auth_failed` | an access token can be revoked early |
| `invalid_grant` from the token endpoint (expired after 90 days unused, password changed, consent revoked) sets `status` `expired` and is `jira_reconnect_required`; an expired connection is never refreshed again until the user reconnects | no retry storm, a clear action for the user |
| No background keep-alive refreshes; each use refreshes and resets Atlassian's 90-day inactivity window | a token stays alive only while it is used |
| Disconnect deletes the row and drops the cached access token. Atlassian has no revoke endpoint for 3LO; the Jira tab links to `https://id.atlassian.com/manage-profile/apps` | the user can also remove the grant on Atlassian's side |

## Acting account of a target

In `oauth` mode every Jira call of a project's sync uses the connection of the target's `connected_user_id`:
- Saving the target (`PUT /projects/{pid}/jira`) sets `connected_user_id` to the saving owner. The owner must have an
  `active` connection, else 409 `jira_not_connected`. Any owner can take a target over by connecting and saving.
- The access check uses the caller's own connection (`jira_not_connected` / `jira_reconnect_required` as `reason`,
  `field` null).
- A pass whose acting connection is missing ends `failed` with `jira_not_connected`; an expired one with
  `jira_reconnect_required`. Plan-run and MCP starts both use the target's account: a member who runs `sync_to_jira`
  acts through the owner the target names.
- In `token` mode `connected_user_id` is ignored and the deployment account is used (unchanged).

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
| `connected_at`, `last_used_at` | timestamptz | not null; `last_used_at` is set on each refresh |
| `expired_at` | timestamptz | null unless `expired` |

`jira_targets` gains `connected_user_id uuid null` (foreign key `users.id` on delete set null). Row-level security is
enabled on `atlassian_connections` with no policy and no grant to the agent role.

## Routes (under `/api/v1`, the web user)

| Method, path | Body | Success | Errors |
|---|---|---|---|
| `GET /integrations/atlassian` | - | 200 `AtlassianConnectionOut` | - |
| `POST /integrations/atlassian/authorize` | `{return_to}` | 200 `{url}` | 503 `atlassian_oauth_unavailable` (not `oauth` mode); 422 |
| `POST /integrations/atlassian/callback` | `{code, state}` | 200 `{connection: AtlassianConnectionOut, return_to}` | 400 `atlassian_state_invalid`; 400 `atlassian_code_invalid`; 403 `atlassian_site_not_granted`; 502 `atlassian_unreachable`; 503 `atlassian_oauth_unavailable` |
| `DELETE /integrations/atlassian` | - | 204 | 503 `atlassian_oauth_unavailable` |

`AtlassianConnectionOut`: `mode` (`oauth` / `token` / null), `connected` (bool), `status` (`active` / `expired` /
null), `account_name`, `connected_at`, `last_used_at` (null when not connected).

`JiraSettingsOut` gains `connection`: null outside `oauth` mode, else `{connected_by_name, connected_by_me, status}`
for the target's acting account (`status` `missing` when the row is gone).

New codes: sync and access-check reasons `jira_not_connected`, `jira_reconnect_required`; route codes above.
`sync_to_jira` and `jira_sync_status` carry them in `last_error_code` like the other sync codes; the MCP description
tells the agent that the target's owner must reconnect Atlassian in Settings → Jira.

## Jira tab (frontend)

- A new route handler `frontend/app/api/integrations/atlassian/callback/route.ts` (signed-in session required; no
  session → sign-in) forwards `code` and `state` to the API and redirects to `return_to` with `?atlassian=connected`
  or `?atlassian=<code>`.
- In `oauth` mode the tab shows, for owners, a connection row above the fields: "Connected to Atlassian as *name*"
  with **Disconnect**, or **Connect Atlassian**, or for an expired one "Your Atlassian connection expired" with
  **Reconnect**. Below the target: "Syncs to Jira as *name*" (or "as you"), and a warning when the acting connection
  is expired or missing.
- Check access and Save are disabled until the owner has an active connection.
- `?atlassian=` shows one toast and is then removed from the URL.
- In `token` mode the tab is unchanged.

## Log events

`api.atlassian.connected`, `api.atlassian.disconnected`, `api.atlassian.refreshed`, `api.atlassian.expired`,
`api.atlassian.callback.refused` (`code`). They carry the user id and codes only: never a token, a code, `state`, an
Atlassian account id or name.
