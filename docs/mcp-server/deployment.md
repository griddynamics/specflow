---
type: Design
title: "MCP server deployment"
description: "Running the mcp role, the public endpoint, identity provider setup, developer setup in Claude Code, the Guides pages of the web app, the local stack with Keycloak, and troubleshooting"
status: stable
tags: [mcp, deployment]
---
# deployment.md: role and ingress, IdP setup, Claude Code setup, web app guides, the local stack with Keycloak and troubleshooting

## Role and process

| Item | Value |
|---|---|
| Container | its own (compose service `mcp`, its own deployment) from its own image `specflow-mcp` (`backend/docker/mcp/Dockerfile`, only the `mcp` dependency extra; Docker Hub `griddynamics/specflow-mcp`); part of every SpecFlow deployment, like `api` and the workers |
| Command | `specflow mcp` (the image's default command) |
| Port | `MCP_PORT` ([architecture.md](architecture.md#settings)) |
| Replicas | stateless; any number behind a load balancer, no affinity |
| Depends on | the API at `API_INTERNAL_URL` and the IdP's JWKS endpoint (locally: `api` healthy) |
| Probes | the liveness and readiness probes of [architecture.md](architecture.md#http-surface-of-the-role); readiness follows the API's `database` and `graph` checks |
| Database migrations | never; the role has no database access |
| Settings | [architecture.md](architecture.md#settings), [auth.md](auth.md), the shared auth variables and `API_INTERNAL_URL`; no database or graph store variables |
| Availability | the API's: with the API down every tool answers `internal_error` and `/readyz` is not ready; agent reads share the one API replica with the web app (accepted, [architecture.md](architecture.md#operations)) |
| Network | plain HTTP from `mcp` to the API inside the cluster, as for the web app; a NetworkPolicy lets only the frontend and mcp pods reach the API. The cluster's CNI must enforce NetworkPolicy; encryption of the internal hop is decided later for both backends-for-frontends |

The `api` role serves the [agent routes](architecture.md#api-agent-routes) only when it has the MCP settings:

| `api` setting | Value |
|---|---|
| `MCP_RESOURCE_URL` | the `mcp` role's value; unset turns the agent routes off (`503 agent_unavailable`) |
| `MCP_AUDIENCE`, `MCP_CLIENT_IDS` | the `mcp` role's values |
| `OIDC_ISSUER`, the `AUTH_*` claim profile (`AUTH_TOKEN_TYPE`, the claim names, `AUTH_ALLOWED_EMAIL_DOMAINS`) | identical to the `mcp` role's |
| `AUTH_JWKS_URL` | optional; identical to the `mcp` role's when set |

A difference in any of these lets the MCP guard accept a token that the API refuses ([Troubleshooting](#troubleshooting)).

## Public endpoint

| # | Requirement |
|---|---|
| P1 | The MCP endpoint shares the web app's public host (`PUBLIC_URL`): no new DNS name or certificate. On that host the ingress sends `/mcp` (exact path) and `/.well-known/oauth-protected-resource` with everything below it to the `mcp` service, and every other path to the web app. The web app serves none of these paths. Nothing else of the role is public; the probes stay internal. |
| P2 | `MCP_RESOURCE_URL` is exactly `https://<web app host>/mcp` as clients see it, for example `https://specflow.example.com/mcp`. The host-wide `/.well-known/oauth-protected-resource` path belongs to MCP. |
| P3 | No sign-in proxy (forward-auth, SSO redirect) in front of these paths: the client must receive the `401` and the metadata. |
| P4 | Pass the `Host` header through unchanged; add any internal host names the role is reached by to `MCP_ALLOWED_HOSTS`. |
| P5 | TLS at the edge; request body limit at least the body cap of [Transport](architecture.md#transport); request timeout at least 30 s; no response buffering requirement (JSON responses). |
| P6 | Rate limit per client address at the edge (suggested: 20 requests per second, burst 40, raised where many developers share one office address); the role has no rate limiter of its own. |
| P7 | The REST API (`api` service) never gets a public route; only the `mcp` service is published. The web app and the `mcp` service reach the API on the internal network. |

## Identity provider setup

Keycloak (reference). In the realm the web app uses:

1. Client `specflow-mcp`, registered as [auth.md](auth.md#client-registration) defines: OpenID Connect, client
   authentication off, standard flow on; implicit, direct access grants, device and service accounts off; no web origins.
2. Audience mapper on `specflow-mcp`'s dedicated scope: included custom audience = the value of `MCP_AUDIENCE` ([architecture.md](architecture.md#settings)), added to the access token only.
3. The `email` scope stays a default scope (the access token carries `email` and `email_verified`).
4. `offline_access` stays on the client. The realm's Offline Session Max Limited is on (its maximum at least 30 days),
   and `specflow-mcp` sets Client Offline Session Max to 30 days, so other clients keep their own limits; see
   [auth.md](auth.md#token-lifetimes-and-revocation).
5. Access token lifespan as [auth.md](auth.md#token-lifetimes-and-revocation) recommends.
6. `specflow-mcp` gets no `specflow-api` audience, and the web client gets no MCP audience.

Other providers: the recipes in [auth.md](auth.md), with the matching `AUTH_*` claim profile on both roles.

MCP clients other than Claude Code: the protected resource metadata advertises no scopes, and a client that then
requests every scope of the IdP's metadata (the MCP Python SDK does) is refused with `invalid_scope` by an IdP that
rejects scopes not assigned to the client, as Keycloak does. Configure such a client to request only scopes assigned to
`specflow-mcp`, for example `openid` ([testing.md](testing.md#client-notes)).

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
| The agent asks which project to use in every session | the repository has no `.specflow/config.json` (run `select_project`), the file names another server (its `server` differs from this one's `MCP_RESOURCE_URL`), or the client is old and ignores server instructions |
| `project_not_found` for the bound project | the project was deleted, you are no longer a member, or the file came from a branch or a teammate that names a project you cannot read; run `select_project` again |

### Bind a repository to a project

Once per repository, run the prompt of the server in Claude Code. For the server named `specflow` the command is:

```text
/mcp__specflow__select_project
```

The agent lists your projects, asks you to choose one and writes `.specflow/config.json` in the working directory
([tools.md](tools.md#workspace-binding)). Claude Code splits the arguments at spaces and passes only the first word, so
add a project id or a one-word name, for example `/mcp__specflow__select_project <project id>`; for a name with spaces,
run it without an argument.

- Commit `.specflow/config.json` (recommended): the team shares one binding, and a branch can carry its own.
  The agent never stages or commits it.
- Later sessions read the file and pass its `project_id`; they do not list projects.
- To switch to another project, select again: the whole file is replaced.

### Local endpoint

Use `localhost`, never `127.0.0.1` (refused by the Host rule of [architecture.md](architecture.md#transport)). The server is named `specflow-local`, so it does not clash with a
production `specflow` entry; its prompt is `/mcp__specflow-local__select_project`, and the `server` value in the
binding file is `http://localhost:8002/mcp`. The local stack gives the web app `MCP_SERVER_NAME=specflow-local`, so the
Guides page shows this name ([Guides in the web app](#guides-in-the-web-app)).

The local endpoint signs in through the bundled Keycloak, like production through its IdP. After `make env-local` and
`make up` ([Local stack](#local-stack)), add the server once:

```bash
claude mcp add --transport http --scope user --client-id specflow-mcp --callback-port 8765 specflow-local http://localhost:8002/mcp
```

Sign in with `/mcp` in Claude Code (select `specflow-local`, then Authenticate) or `claude mcp login specflow-local`.
The browser opens the local Keycloak. Use your own account: the email you gave `make env-local` and
`LOCAL_TEST_USER_PASSWORD` from `.env.local`. It is the web app's user, so Claude Code and the web app see the same
projects. Port 8765 must be free during sign-in. The QA users (`alice@example.com` and the others, with
`KEYCLOAK_QA_PASSWORD`) are for the tests ([testing.md](testing.md#fixtures)).

Keycloak keeps no data between container creations ([Local stack](#local-stack)): after `make down` and `make up`,
authenticate again. A static token or a `headersHelper` entry no longer exists; do not put a token in the tracked
`.mcp.json`.

### Guides in the web app

The web app shows this developer setup with the deployment's own values, so a developer copies commands instead of
editing placeholders.

| Page | What it shows |
|---|---|
| Guides › MCP server (`/guides/mcp-server`) | The steps of this section for Claude Code, with the address and client id inside the commands and the team file: add the server, sign in, bind a repository, check it. Each command has a copy button. A tools table lists the tools and the prompt ([architecture.md](architecture.md#tool-registry)) |
| Plan › Develop | Per project: `/mcp__specflow__select_project <project id>` with the id filled in, the binding file as JSON ([tools.md](tools.md#the-binding-file)), and a link to the guide |

Only Claude Code has setup steps; Codex and Cursor show "Setup is in progress" until each is tested end to end
([later.md](later.md)). `/guides/mcp-server?agent=claude-code`, `codex` or `cursor` selects the agent.

The page has one set of steps in every deployment: add the server, sign in through the IdP, bind a repository, check it,
and the team `.mcp.json`. Only the values come from configuration: the server name from `MCP_SERVER_NAME` (default
`specflow`; the local stack sets `specflow-local`), the address from `MCP_PUBLIC_URL` and the client id from
`MCP_CLIENT_ID`. The server name is used in the add command, the team `.mcp.json` and the prompt
`/mcp__<name>__select_project`.

### Web app settings for the guides

Operators set up to three settings on the web app, two from the same values as the `mcp` role. All are read on the server only:
no `NEXT_PUBLIC_` variable exists, and the pages pass the values to the browser as props.

| Web app setting | Same value as the `mcp` role | Default | Rule |
|---|---|---|---|
| `MCP_PUBLIC_URL` | `MCP_RESOURCE_URL` | `PUBLIC_URL` + `/mcp` | An `http` or `https` URL; `https` unless `APP_ENV=local` |
| `MCP_CLIENT_ID` | the first entry of `MCP_CLIENT_IDS` | `specflow-mcp` | Not blank |
| `MCP_SERVER_NAME` | none (a label of the developer's machine) | `specflow` | Letters, digits, `_` and `-`, 1 to 64 characters; it ends up in a slash command |

With the ingress of [Public endpoint](#public-endpoint) (P1, P2) the default already equals `MCP_RESOURCE_URL`; set the
setting when the two differ. The web app refuses a value that breaks the rule, with an error that names the setting
([../configuration.md](../configuration.md)).

## Local stack

`make up` always starts the bundled Keycloak and the `mcp` role. Keycloak is the identity provider of every local
service: the web app, the REST API and `mcp` sign in and validate tokens against it, so one local user is the same
person everywhere. There is no dev login and no `AUTH_MODE`. `make up WITH=keycloak` is refused (`WITH` accepts `temporal` and
`langfuse`).

| Service | Compose profile | Image | Host port | Role |
|---|---|---|---|---|
| `mcp` | none (always on) | `specflow-mcp:local`, `specflow mcp` | `127.0.0.1:8002` | the MCP role |
| `keycloak` | none (always on) | `quay.io/keycloak/keycloak`, an exact 26.4 patch tag pinned like the other services, `start-dev` | `127.0.0.1:8180` | local IdP, realm `specflow` imported at container creation; api, mcp and frontend wait for it to be healthy |

| Item | Local value |
|---|---|
| Realm file | `config/keycloak/specflow-realm.json`, a template: every secret, password, id and address is a `${NAME}` placeholder that Keycloak fills from the container environment at import. Compose passes the values from `.env.local`. Clients: `specflow-mcp` (public, audience mapper `http://localhost:8002/mcp`) and `specflow-web` (confidential, secret `KEYCLOAK_WEB_CLIENT_SECRET`, audience `specflow-api`, redirect URI `<PUBLIC_URL>/api/auth/callback/oidc`, post-logout redirect `<PUBLIC_URL>/*`) |
| `PUBLIC_URL` | No trailing slash: the realm joins it with paths to build the redirect URIs. Compose takes it from `PUBLIC_URL` or `http://localhost:3001` |
| Your user | email `LOCAL_TEST_USER_EMAIL`, password `LOCAL_TEST_USER_PASSWORD`, Keycloak id `LOCAL_TEST_USER_ID` (so the token `sub` stays the same), all written by `make env-local` |
| QA users | `alice@example.com`, `bob@example.com` and `carol@other.example` (a domain that is not allowed), with password `KEYCLOAK_QA_PASSWORD`. Their ids are fixed in the realm file, the same constants as in `backend/examples/mcp_qa_lib.py`, so the QA seed needs no Keycloak call ([testing.md](testing.md#fixtures)) |
| Issuer | `http://localhost:8180/realms/specflow` for every token: Keycloak runs with `KC_HOSTNAME=http://localhost:8180` |
| Keys from containers | `AUTH_JWKS_URL` and the web app's `OIDC_INTERNAL_URL` on the compose network name `specflow-keycloak:8080` (accepted in local only) |
| `mcp` | `APP_ENV=local`, `OIDC_ISSUER` and `AUTH_JWKS_URL` as above, `MCP_RESOURCE_URL=http://localhost:8002/mcp`, `API_INTERNAL_URL=http://specflow-api:8000`, `AUTH_ALLOWED_EMAIL_DOMAINS` from `.env.local` (your domain and `example.com`); no database or graph store variables |
| `api` | the same issuer, JWKS URL, claim profile, email domains and `MCP_RESOURCE_URL` as `mcp`, so the agent routes are on |
| Web app guide values | compose gives the web app `MCP_PUBLIC_URL=http://localhost:8002/mcp` (the web app's `PUBLIC_URL` is `http://localhost:3001`, so the default would be wrong here), `MCP_SERVER_NAME=specflow-local` and leaves `MCP_CLIENT_ID` at its default. Guides › MCP server at `http://localhost:3001/guides/mcp-server` shows the same steps as production with these values ([Guides in the web app](#guides-in-the-web-app)) |

`make up` refuses to start (`check-keycloak-env`) when the env file lacks one of `KEYCLOAK_ADMIN_PASSWORD`,
`KEYCLOAK_QA_PASSWORD`, `KEYCLOAK_WEB_CLIENT_SECRET`, `LOCAL_TEST_USER_EMAIL`, `LOCAL_TEST_USER_PASSWORD` or
`LOCAL_TEST_USER_ID`, or when the email is not usable. It names the missing variables and says to run `make env-local`.
Compose keeps empty defaults so other commands (`make ps`, `make logs`, `make down`) still work.

Generated local secrets (Keycloak admin password, QA user password, `specflow-web` client secret, your password and id)
live in `.env.local` next to the other generated local secrets and are never committed.

Keycloak has no data volume, by design. Recreating the container imports the realm again from the current `.env.local`:

| Event | Effect |
|---|---|
| `make down`, then `make up` | New Keycloak container: all Keycloak sessions end. Sign in again in the browser and in Claude Code. Users and ids are the same, so the `sub` and the projects stay |
| A value Keycloak reads changes in `.env.local` (password, email) | `make up` recreates the container; the new value takes effect |
| `config/keycloak/specflow-realm.json` changes | The bind mount alone does not recreate the container: run `make down` and `make up` |
| `make down-volumes` | Deletes the application data (Postgres, Neo4j, RustFS, Temporal, Langfuse); Keycloak has none. Run it once after upgrading from the dev login, whose `dev:<email>` data is not migrated ([QUICKSTART.md](../../QUICKSTART.md#upgrading-from-the-dev-login)) |

The web app signs out through the IdP (RP-initiated logout) when the IdP advertises an end-session endpoint, as Keycloak does.

## Troubleshooting

Tool failures between `mcp` and the API show as `internal_error` to the agent; the `error_type` of the
`mcp.tool.completed` event in the `mcp` log names the cause ([architecture.md](architecture.md#errors)), and its
`request_id` finds the matching API log lines.

| Symptom | Likely cause |
|---|---|
| Sign-in works, every tool answers `internal_error`, `error_type` `ApiAuthRejected` | the MCP audience differs between api and mcp: `MCP_AUDIENCE` or `MCP_CLIENT_IDS` missing or different in the `api` role; set the `mcp` role's values |
| The same, with the audience settings equal | the claim profile differs between api and mcp: `OIDC_ISSUER`, `AUTH_TOKEN_TYPE` (and its claim and value), the claim names or `AUTH_ALLOWED_EMAIL_DOMAINS`; make them identical. A single failed call after a long pause is a token that expired between the guard and the API; the next call refreshes it |
| Every tool answers `internal_error`, `error_type` `ApiAgentUnavailable` | the `api` role has no `MCP_RESOURCE_URL` |
| Every tool answers `internal_error`, `error_type` `ApiUnreachable`; `/readyz` answers `503` with detail `api_unreachable` | the API is down, `API_INTERNAL_URL` is wrong, or the NetworkPolicy does not admit the mcp pods |
| Some calls answer `internal_error`, `error_type` `ApiTimeout` | the API took longer than 25 s |
| Every tool answers `internal_error`, `error_type` `ApiProtocolError` | the API answered outside the agent contract, for example api and mcp images of different releases |
