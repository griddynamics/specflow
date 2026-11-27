---
type: Design
title: "Notion connections"
description: "Per-user Notion connections through one public connection: why OAuth, the settings, the connect flow, tokens and their rotation, the request budget, log events"
status: draft
tags: [notion, auth]
---
# oauth.md: per-user Notion connections (public connection, OAuth 2.0)

Each user connects their own Notion account once. Specflow then calls Notion as that user: a public connection's access
token reaches only the pages and databases the user ticked in Notion's page picker when they authorised it. Decisions: N1
(OAuth only, per user), N10 (one request budget per bot).

## Access

| Option | Access | Chosen |
|---|---|---|
| Public connection (OAuth 2.0) | the pages the user ticked, with the capabilities of the connection; one `bot_id` per authorisation | yes |
| Internal connection | one static token per workspace, added to pages by a workspace admin | no in the product; `NOTION_API_TOKEN` is used by the live tests only |
| Personal access token | a secret pasted into a form | no |

## Settings

| Name | Rule |
|---|---|
| `NOTION_OAUTH_CLIENT_ID`, `NOTION_OAUTH_CLIENT_SECRET` | the OAuth client of the public connection; the client ID turns the integration on |
| `NOTION_TOKEN_KEY` | a Fernet key: encrypts the refresh tokens and signs the OAuth `state`; its own key |
| `NOTION_REQUESTS_PER_MINUTE` | default 150, 1 to 600; see "Request budget" |
| `PUBLIC_URL` | the redirect URI is `{PUBLIC_URL}/api/integrations/notion/callback` |

All-or-nothing, as GitHub: with the client ID unset the integration is off and its routes answer `503 notion_unavailable`; a
partial set stops the api at start naming the missing variables. Only the api role reads them (`NotionSettings`). Details
and the registration of the connection: [../configuration.md](../configuration.md#notion-integration).

Register the public connection in Notion's developer portal: redirect URI `{PUBLIC_URL}/api/integrations/notion/callback`;
capabilities **read content**, **update content**, **insert content**, **read comments**, **insert comments**, user
information **without e-mail**. (Comments are for the later gap channel; asking for them now spares every user a second
consent.)

## Connect flow

```mermaid
sequenceDiagram
  participant U as Browser
  participant FE as Next.js (BFF)
  participant API as Specflow API
  participant N as notion.com
  U->>FE: Connect Notion (Integrations page)
  FE->>API: POST /integrations/notion/authorize {return_to}
  API-->>FE: {url} (state signed: user id, return_to, purpose authorize, expiry 10 min)
  FE-->>U: navigate to url
  U->>N: Allow, tick pages in the page picker
  N-->>U: 302 {PUBLIC_URL}/api/integrations/notion/callback?code&state
  U->>FE: GET callback (signed-in session)
  FE->>API: POST /integrations/notion/callback {code, state}
  API->>N: POST /v1/oauth/token (code; Basic client id and secret)
  API-->>FE: {connection, return_to}
  FE-->>U: 302 return_to?notion=connected (or ?notion=<code>)
```

- `state` is the shared signed state of `integrations.state` with its own context. A state of another user, another
  purpose or older than 10 minutes is `notion_state_invalid`; Notion is not called.
- `return_to` follows the Atlassian rule (a path of this site; `integrations.return_to`).
- The token response carries `workspace_id`, `workspace_name` and `bot_id`; they are stored with the Fernet refresh
  token. A refused code is `notion_code_invalid`; `429`, `5xx`, a timeout or a transport error is `notion_unreachable`.
  Connecting again replaces the row (new grant, `active`).
- The return route (a page of the web app, not the API) adds two codes of its own to `?notion=<code>`: `notion_consent_denied`
  (Notion came back with `?error=`, the API is not called) and `notion_unexpected` (any other answer of the API).
- Notion has no install step: the page picker of the consent page is how a user shares pages. To share more later the user
  connects again.

## Tokens

The same rules as GitHub ([../github/oauth.md](../github/oauth.md#tokens)), on `notion_connections`:

- the refresh token is stored encrypted; the access token lives in the process's memory, and is also stored encrypted
  (Fernet, column `access_token`) only for a grant with no refresh token, so it survives a restart or a second replica
  (`integrations.tokens`). A token answer without `expires_in` never expires: it is cached and replaced only after a `401`.
  An answer without a refresh token still connects, as a connection that cannot be refreshed: a `401` on a token that was
  used sets `expired` (`notion_reconnect_required`) and Notion's token endpoint is not called. A cache miss never expires
  a connection, and a refresh answer without a refresh token keeps the stored one;
- a refresh runs under a per-user lock and `FOR UPDATE` on the row, and the rotated refresh token is committed before the
  new access token is used;
- a `401` refreshes once and asks once more: the REST client never retries a `401` (`notion_auth_failed`); the refreshing
  wrapper that `NotionConnections.gateway` returns refreshes with the token it handed out as `used` and repeats the call
  once (a `401` means the call never ran, so a repeat is safe for writes); `invalid_grant` or a refresh token that no longer decrypts sets `expired`
  (`notion_reconnect_required`) and the connection is never refreshed again until the user reconnects; any other refusal
  keeps it `active` (`notion_unreachable`, or `notion_auth_failed` for a wrong client secret);
- every use reads the row first (`notion_not_connected` without one), so another replica's disconnect or reconnect is seen;
- disconnect deletes the row, drops the cached token and calls `POST /v1/oauth/revoke` with an access token (best effort):
  the cached one while it is good, else the stored one (a grant with no refresh token), else one refresh under the row lock
  whose fresh access token is revoked. A failed refresh or revoke never blocks the delete; an expired connection is not
  revoked. A refresh token is never sent to revoke.
- a project binding to a workspace other than the one of the member's connection is `notion_reconnect_required` everywhere
  (see [codes.md](codes.md)).

## Request budget

Notion limits each connection to 180 requests a minute on a Free or Plus workspace (600 on Business and Enterprise), in a
fixed 60-second window, and answers `429` or `529` with `Retry-After` (at most 60 s).

- One rate limiter per `bot_id` lives in the api process and import and export share it (N10): a long import cannot starve
  a plan pass of the same member, and the reverse. Its rate is `NOTION_REQUESTS_PER_MINUTE` (default 150, a margin under
  180).
- `Retry-After` is honoured. A pass or an import waits at most 60 s in all for the limit; a wait that does not fit ends it
  with `notion_rate_limited` (a plan pass) or `unknown_error` (an import, N6).
- Retries: `429` and `529` always, `5xx` for reads only (`GET` and read-only `POST`s) and `409` `conflict_error` on any method, five attempts with exponential backoff and jitter.
- The request limit is per process: one api replica is assumed (as for the importers and the Slack bindings).

## Log events

| Event | Fields |
|---|---|
| `api.notion.mode` | `mode` (`on`), at every api start with the integration on |
| `notion.connected` | `user_id` |
| `notion.disconnected` | `user_id`, `deleted`, `revoked` |
| `notion.refreshed` | `user_id` |
| `notion.expired` | `user_id`, `code` |
| `api.notion.refused` | `user_id`, `step` (`callback`), `code` |

They carry the user id and codes only: never a token, a code, `state`, a workspace name, a page title or a page id.

## Routes

`GET /integrations/notion` (the caller's connection: `enabled`, `connected`, `status`, `workspace_name`, `connected_at`,
`last_used_at`), `POST /integrations/notion/authorize`, `POST /integrations/notion/callback`, `DELETE /integrations/notion`.
The project binding is `PUT` and `DELETE /projects/{pid}/integrations/notion` ([integrations-api.md](../mission-control/integrations-api.md)):
the workspace of the project is the caller's `workspace_id`, one workspace per project.

## Data

`notion_connections` (migration `0014_notion`): `user_id` (primary key, cascade with the user), `workspace_id`,
`workspace_name`, `bot_id`, `refresh_token` (Fernet ciphertext), `access_token` (Fernet ciphertext, nullable: only for a grant with no refresh token), `refresh_expires_at`, `status` (`active`, `expired`),
`connected_at`, `last_used_at`, `expired_at`. Row-level security is on with no policy and no grant: the agent role reads
nothing of it. The migration also widens the check constraints of `project_integrations.app` and `connections.kind` with
`notion`.

## Not verified live

The OAuth consent and the page picker need a browser click on Notion's consent page; the first live run is left to the
owner. Business and Enterprise limits (600 a minute) are Notion's documentation, not measured.
