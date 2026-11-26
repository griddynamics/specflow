---
type: Design
title: "Linear connections"
description: "Per-member Linear connections through an OAuth app (actor user): the settings, the registration of the app, the connect flow, the encrypted token pair and its rotation, disconnect, the member gateway, codes and log events"
status: draft
tags: [linear, auth]
---
# oauth.md: per-member Linear connections (Linear OAuth app, actor user)

Each member connects their own Linear account once. Graphico then calls Linear as that member with an OAuth 2.0 access
token (`actor=user`, the Linear default). A token reaches only what the member can reach in their workspace, so a
write made by Graphico shows the member as its author in Linear. Graphico never acts as an application user.

Code: `backend/src/linear/oauth.py` (the token endpoint client), `backend/src/linear/connections.py` (the service),
`backend/src/db/linear.py` (the rows), `backend/src/api/routers/linear.py` (the routes),
`frontend/app/api/integrations/linear/callback/route.ts` (the return of the browser).

## Settings

Read by the api only. The table and the registration steps: [../configuration.md](../configuration.md#linear-integration).
No worker role gets `LINEAR_*` (plan export, gap channel, importer and webhook all run in the api; AC6).

| Variable | Rule |
|---|---|
| `LINEAR_OAUTH_CLIENT_ID` | turns the integration on; blank or unset = off |
| `LINEAR_OAUTH_CLIENT_SECRET` | required when on; a secret, never logged; not blank |
| `LINEAR_TOKEN_KEY` | required when on; a Fernet key; encrypts both tokens, signs the `state` and derives the PKCE verifier |
| `LINEAR_WEBHOOK_SECRET` | optional; the signing secret of the app's webhook; unset: the webhook route answers 503 `linear_webhook_unavailable` |
| `PUBLIC_URL` | required when on; the callback is `{PUBLIC_URL}/api/integrations/linear/callback` |

A partial set, or a key that is not a Fernet key, stops the api at start and names the variable
(`LinearSettings` in `backend/src/settings.py`). The api logs `api.linear.mode` (`mode` `on`) at start when the
integration is on. With the integration off every Linear route answers 503 `linear_unavailable`, except
`GET /integrations/linear` (`available` false) and the `GET` of the role targets (`available` false).

## Register the Linear OAuth app

Only an **admin** of the Linear workspace that owns the app can create it. On linear.app: Settings, API, OAuth
applications, New.

| Field | Value |
|---|---|
| Application name | for example `Graphico (local)` |
| Callback URL | `{PUBLIC_URL}/api/integrations/linear/callback` |
| Public | on (the app can be installed by other workspaces; a private app is installable by the owning workspace only) |
| Scopes | `read` and `write` (Graphico asks for `read,write` and no `admin`) |
| Webhook URL | `{api public URL}/api/v1/integrations/linear/webhook` (a tunnel to the api port 8000 on a local stack) |
| Webhook events | Comments and OAuth app authorization events (revocations). Issues is not needed: Graphico does not read it. |

Rules:

- **One app per environment** (local, each shared environment). A Linear app has one webhook URL, so two
  environments cannot share one app.
- The webhook URL is the public HTTPS address of the **api** plus `/api/v1/integrations/linear/webhook`. A local stack
  needs a tunnel to the api port 8000. The web app's address (`PUBLIC_URL`) is not it: the web app proxies the api only
  under `/api/backend/*` and with a session.
- After you create the app: copy the client ID to `LINEAR_OAUTH_CLIENT_ID`, the client secret to
  `LINEAR_OAUTH_CLIENT_SECRET` and the webhook signing secret to `LINEAR_WEBHOOK_SECRET`.
- A workspace can require admin approval of third-party apps. The member then sees "request approval" at the consent
  page and cannot connect until an admin approves. Graphico has no code for this case: **unknown**, the browser
  returns with `?linear=linear_consent_denied` or no `code`.
- The api reaches `linear.app` and `api.linear.app` directly: the OAuth client ignores the proxy variables and `.netrc`
  (`trust_env=False`).

Linear facts the code relies on (`linear/oauth.py`): the access token lives about 24 hours; the refresh token is
rotated on every refresh; PKCE `S256`; revoke is `POST https://api.linear.app/oauth/revoke`.

## Connect flow

```mermaid
sequenceDiagram
  participant U as Browser
  participant FE as Next.js (BFF)
  participant API as Graphico API
  participant LN as linear.app
  U->>FE: Connect Linear (Integrations page)
  FE->>API: POST /integrations/linear/authorize {return_to}
  API-->>FE: {url} (state signed: user id, return_to, expiry 10 min; PKCE challenge of the state)
  FE-->>U: navigate to url
  U->>LN: sign in, approve the app
  LN-->>U: 302 {PUBLIC_URL}/api/integrations/linear/callback?code&state
  U->>FE: GET callback (signed-in session)
  FE->>API: POST /integrations/linear/callback {code, state}
  API->>LN: POST /oauth/token (code, code_verifier)
  API->>LN: POST /graphql viewer { id name organization { id name urlKey } }
  API-->>FE: {connection, return_to}
  FE-->>U: 302 return_to?linear=connected (or ?linear=<code>)
```

- `state` is the shared signed state of `integrations.state` with its own context. A state of another user, or older
  than 10 minutes, is `linear_state_invalid`; Linear is not called.
- **PKCE without storage (AC-S1-1).** The code verifier is an HMAC-SHA256 of the `state`, keyed from
  `LINEAR_TOKEN_KEY`. The authorize URL carries its SHA-256 challenge; the callback computes the same verifier from the
  `state` it gets back. Nothing is stored between the consent and the callback, so any api replica can finish the flow.
- The authorize URL has `response_type=code`, `scope=read,write`, `code_challenge_method=S256` and `actor=user`.
- `return_to` is a path of this site (`same_site_path`); the form body forbids extra fields.
- A refused code that Linear answers with `invalid_grant` or any other 4xx is `linear_code_invalid`. A 429, a 5xx, a
  timeout or a network error is `linear_unreachable`. A refused app credential (`invalid_client`,
  `unauthorized_client`, 401, 403) is `linear_auth_failed`.
- A grant without a refresh token, or without a positive `expires_in`, is refused as `linear_code_invalid`.
- The browser lands on `return_to?linear=connected`. A denied consent (`?error=` from Linear) becomes
  `?linear=linear_consent_denied`; an unknown API answer becomes `?linear=linear_unexpected`
  (`frontend/lib/linear-callback.ts`).
- The Linear user id and name and the workspace id, name and URL key are stored. Connecting again replaces the row
  (new grant, `active`).

## Tokens

| Rule | Why |
|---|---|
| Both tokens are stored encrypted (Fernet) in `linear_connections` (AC7): the access token and the rotated refresh token, with `expires_at` | a process that finds a still-valid access token in the row can use it without a refresh (AC-S1-2) |
| A process also keeps the access token in memory, per member, until 60 s before it expires (`integrations.tokens`) | fewer row reads and refreshes |
| A refresh runs under the shared per-member lock and `FOR UPDATE` on the row; the rotated pair is committed **before** the new access token is used, even when the caller is cancelled | Linear kills the old refresh token on every refresh |
| When the row's access token has more than 60 s left and is not the one Linear just refused, it is served and nothing is rotated | another replica already refreshed it |
| A `401` from the GraphQL API refreshes once and asks once more (the gateway, below) | an access token can be revoked early |
| `invalid_grant` on a refresh sets `status` `expired` and `expired_at`; the row is never refreshed again until the member reconnects (AC-S1-3) | no retry storm, a clear action |
| A pair that no longer decrypts (a rotated `LINEAR_TOKEN_KEY`) sets `expired` the same way | every member reconnects |
| Any other refresh failure keeps the connection `active`: unreachable, or a refused app credential, is `LinearUnavailableError` (`linear_unreachable`) | an outage or a bad deployment setting must not log every member out |
| Every `access_token` call reads the row first. No row, or a row not `active`, is `LinearUnauthorizedError`; a row of another grant (another Linear user id or `connected_at`) makes the process drop its cached token | another process never sees this process's disconnect or reconnect |

The service has no `linear_reconnect_required` of its own: "not connected" and "expired" both surface as the API
code `linear_reconnect_required` in the role routes (AC-S1-5), and as `linear_not_connected` where the project has no
binding at all (see [api.md](api.md#codes)).

Statuses of a row: `active`, `expired`, `revoked` (check constraint, [data-model.md](data-model.md#linear_connections)).
`revoked` is set only by the webhook (`OAuthApp` `revoked`, [gaps.md](gaps.md#webhook)). A `revoked` or `expired` row
is not used for any call; the member connects again, which writes `active`. `GET /integrations/linear` shows `revoked`
as `revoked`; the web app treats it like `expired` (Reconnect Linear), and the project's integration health is
`reconnect_required` for both.

Known limits:

- A refresh can be lost: if Linear rotated the refresh token but the commit fails, the stored refresh token is dead.
  The next refresh gets `invalid_grant` and the member must reconnect. The OAuth client comment says Linear accepts a
  retry with the same refresh token for 30 minutes after a lost answer; the service does not use this.
- A refresh token that is not used for 30 days expires (Linear). An idle connection then needs a reconnect (shown as
  `expired`).
- A token revoked on Linear's side only is found at the next call (`401`), not before.

## Disconnect

`DELETE /integrations/linear` locks the row, deletes it, drops the cached token, then revokes the refresh token and the
access token at Linear (best effort; AC-S1-4). A token that no longer decrypts, or that Linear refuses or
cannot be reached for, is skipped. The route answers 204 also without a connection. Project bindings
(`project_integrations`) of the member are not deleted here; a binding without an active connection reads as
`reconnect_required` or `none` ([api.md](api.md#integration-health)).

## The member gateway

Every Linear call of every role is made through one contract (AC-ORCH-1):

```python
async with runtime.linear.gateway(user_id) as gateway:   # a linear.gateway.LinearGateway
    ...
```

`LinearConnections.gateway(user_id)` builds an `HttpLinearGateway` whose token provider is the member's
`access_token` (read on every call) and whose `refresh` callback is `LinearConnections.refresh(user_id, used=<refused
token>)`. On a `401` the gateway asks for a new token once, retries once, and raises `LinearUnauthorizedError` if
Linear refuses again. The context closes the HTTP client on exit. The gateway does not retry on a 429: the caller owns
rate-limit handling (AC-S2-1, `LinearRateLimitedError.retry_after`).

The member is **never the caller**. Each role reads the member from the project's `linear` row in
`project_integrations` (rule R1) at the start of each use: the plan pass, the gap post, the source check and add. The
caller of the source add is only `connections.connected_by`.

## Codes

Closed `LinearCode` values inside the process (`linear/errors.py`): `linear_unauthorized`, `linear_forbidden`,
`linear_not_found`, `linear_rate_limited`, `linear_invalid`, `linear_already_exists`, `linear_unreachable`. They map to
API codes in `api/linear/errors.py`:

| Inside | API |
|---|---|
| `linear_unauthorized` | 409 `linear_reconnect_required` |
| `linear_forbidden` | 409 `linear_forbidden` |
| `linear_not_found` | 409 `linear_not_found` |
| `linear_rate_limited` | 429 `linear_rate_limited` (with `Retry-After`) |
| `linear_invalid` | 422 `linear_invalid` (with `details.field` when known) |
| `linear_already_exists` | 422 `linear_invalid` (it has no own answer) |
| `linear_unreachable` | 502 `linear_unreachable` |

Connect codes: `linear_state_invalid`, `linear_code_invalid`, `linear_unreachable`, `linear_auth_failed`. All codes
with their statuses: [api.md](api.md#codes). No Linear text is kept or shown: an error carries its code and an HTTP
status only.

## Log events

| Event | Fields |
|---|---|
| `api.linear.mode` | `mode` (`on`), at every api start with the integration on |
| `linear.connected` | `user_id` |
| `linear.disconnected` | `user_id`, `deleted`, `revoked` (how many tokens Linear confirmed) |
| `linear.refreshed` | `user_id` |
| `linear.refresh_failed` | `user_id`, `code` (the connection stays `active`) |
| `linear.expired` | `user_id` |
| `api.linear.callback.refused` | `user_id`, `code` |

They carry the user id and codes only: never a token, a code, `state`, a Linear user id or a name.
