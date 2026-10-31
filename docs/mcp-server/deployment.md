---
type: Design
title: "MCP server deployment"
description: "Running the mcp role, the public endpoint, identity provider setup, developer setup in Claude Code, and the local stack with Keycloak"
status: draft
tags: [mcp, deployment]
---
# deployment.md: role and ingress, IdP setup, Claude Code setup and the local stack with Keycloak

## Role and process

| Item | Value |
|---|---|
| Container | its own (compose service `mcp`, its own deployment), built from the same image as `api`; part of every SpecFlow deployment, like `api` and the workers |
| Command | `specflow mcp` (overrides the image's default `migrate` then `api` command) |
| Port | `MCP_PORT`, default `8002` |
| Replicas | stateless; any number behind a load balancer, no affinity |
| Depends on | Postgres, Neo4j, the IdP's JWKS endpoint, and a migrated schema (locally: `api` healthy, since the `api` container runs `specflow migrate` first) |
| Probes | liveness `GET /healthz`; readiness `GET /readyz` (database and Neo4j) |
| Database migrations | never; the `api` container runs `specflow migrate` before `specflow api`, so `mcp` starts after `api` on a fresh schema |
| Settings | [architecture.md](architecture.md), [auth.md](auth.md), and the shared database, Neo4j and auth variables |

## Public endpoint

| # | Requirement |
|---|---|
| P1 | The MCP endpoint shares the web app's public host (`PUBLIC_URL`): no new DNS name or certificate. On that host the ingress sends `/mcp` (exact path) and `/.well-known/oauth-protected-resource` with everything below it to the `mcp` service, and every other path to the web app. The web app serves none of these paths. Nothing else of the role is public; the probes stay internal. |
| P2 | `MCP_RESOURCE_URL` is exactly `https://<web app host>/mcp` as clients see it, for example `https://specflow.example.com/mcp`. The host-wide `/.well-known/oauth-protected-resource` path belongs to MCP. |
| P3 | No sign-in proxy (forward-auth, SSO redirect) in front of these paths: the client must receive the `401` and the metadata. |
| P4 | Pass the `Host` header through unchanged; add any internal host names the role is reached by to `MCP_ALLOWED_HOSTS`. |
| P5 | TLS at the edge; request body limit at least 1 MiB; request timeout at least 30 s; no response buffering requirement (JSON responses). |
| P6 | Rate limit per client address at the edge (suggested: 20 requests per second, burst 40, raised where many developers share one office address); the role has no rate limiter of its own. |
| P7 | The REST API (`api` service) never gets a public route; only the `mcp` service is published. The web app keeps reaching the API on the internal network. |

## Identity provider setup

Keycloak (reference). In the realm the web app uses:

1. Client `specflow-mcp`: OpenID Connect, client authentication off, standard flow on; implicit, direct access grants,
   device and service accounts off; PKCE method `S256`; redirect URIs `http://localhost:8765/callback` and
   `http://127.0.0.1:8765/callback`; no web origins.
2. Audience mapper on `specflow-mcp`'s dedicated scope: included custom audience = the value of `MCP_AUDIENCE` (by
   default `MCP_RESOURCE_URL`), added to the access token only.
3. The `email` scope stays a default scope (the access token carries `email` and `email_verified`).
4. `offline_access` stays on the client. The realm's Offline Session Max Limited is on (its maximum at least 30 days),
   and `specflow-mcp` sets Client Offline Session Max to 30 days, so other clients keep their own limits; see
   [auth.md](auth.md).
5. Access token lifespan 5 to 10 minutes.
6. `specflow-mcp` gets no `specflow-api` audience, and the web client gets no MCP audience.

Other providers: the recipes in [auth.md](auth.md), with the matching `AUTH_*` claim profile on both roles.

## Developer setup in Claude Code

Once per machine (user scope), or once per repository (a committed `.mcp.json`):

```bash
claude mcp add --transport http --scope user --client-id specflow-mcp --callback-port 8765 specflow https://specflow.example.com/mcp
```

```json
{
  "mcpServers": {
    "specflow": {
      "type": "http",
      "url": "https://specflow.example.com/mcp",
      "oauth": {"clientId": "specflow-mcp", "callbackPort": 8765}
    }
  }
}
```

Sign in with `/mcp` in Claude Code (select `specflow`, then Authenticate) or `claude mcp login specflow`; over SSH,
`claude mcp login specflow --no-browser` prints the URL and asks for the redirect URL. Port 8765 must be free during
sign-in.

| Symptom | Likely cause |
|---|---|
| Sign-in succeeds, every call answers 401 | the token lacks the MCP audience (IdP audience mapper) or the client id is not in `MCP_CLIENT_IDS` |
| `invalid redirect_uri` at the IdP | callback port or host differs from the registered redirect URIs |
| 403 after sign-in | email not verified, or its domain is not in `AUTH_ALLOWED_EMAIL_DOMAINS` |
| `list_projects` is empty | signed in with another IdP account, or not a member of any project |
| Tools disappear after a while | the session ended at the IdP; sign in again |

## Local stack

`make up` always starts the `mcp` role, in dev mode like the web app: the same dev-login users and the same projects.
`make up WITH=keycloak` also starts the bundled Keycloak and switches the role to `oidc`, for the real sign-in flow and
the QA tiers.

| Service | Compose profile | Image | Host port | Role |
|---|---|---|---|---|
| `mcp` | none (always on) | the backend image, `specflow mcp` | `127.0.0.1:8002` | the MCP role |
| `keycloak` | `keycloak` | `quay.io/keycloak/keycloak`, an exact 26.4 patch tag pinned like the other services, `start-dev` | `127.0.0.1:8180` | local IdP for the real sign-in flow, realm `specflow` imported at start |

| Item | Local value |
|---|---|
| Realm file | `config/keycloak/specflow-realm.json`: clients `specflow-mcp` (public) and `specflow-web` (confidential, audience `specflow-api`, redirect URI `http://localhost:3001/api/auth/callback/oidc`, the web app's sign-in callback, which the QA token helper uses), audience mapper `http://localhost:8002/mcp`. No users and no literal secret in the file: the QA seed sets the `specflow-web` secret from `.env.local` through the Keycloak admin API |
| QA users | `alice@example.com`, `bob@example.com` and `carol@other.example` (a domain that is not allowed), created by the QA seed with a password from `.env.local` ([testing.md](testing.md#fixtures)) |
| Issuer | `http://localhost:8180/realms/specflow` for every token: Keycloak runs with `KC_HOSTNAME=http://localhost:8180` |
| Keys from containers | `AUTH_JWKS_URL` on the compose network name (accepted in local only) |
| `mcp` in dev mode | `APP_ENV=local`, `AUTH_MODE=dev`, `AUTH_DEV_SIGNING_KEY` from `.env.local`, `MCP_RESOURCE_URL=http://localhost:8002/mcp`, `AUTH_ALLOWED_EMAIL_DOMAINS=example.com` |
| Dev token for Claude Code | from the QA helper command, through `headersHelper` in `.mcp.json` or `--header`; 8 hours ([auth.md](auth.md)) |
| `mcp` with Keycloak | `AUTH_MODE=oidc`, `OIDC_ISSUER=http://localhost:8180/realms/specflow`, `AUTH_JWKS_URL` on the compose network name, and `AUTH_DEV_SIGNING_KEY` empty: `.env.local` always holds a dev key for the web app, and the role refuses any dev key under `oidc` |
| Web app and API | dev login in both modes. With Keycloak, the MCP users are Keycloak users, so the QA seed provides their data ([testing.md](testing.md)) |

Generated local secrets (Keycloak admin password, QA user password, `specflow-web` client secret) live in `.env.local`
next to the other generated local secrets and are never committed.
