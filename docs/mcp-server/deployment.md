---
type: Design
title: "MCP server deployment"
description: "Running the mcp role, the public endpoint, identity provider setup, developer setup in Claude Code, the Guides pages of the web app, and the local stack with Keycloak"
status: stable
tags: [mcp, deployment]
---
# deployment.md: role and ingress, IdP setup, Claude Code setup, web app guides and the local stack with Keycloak

## Role and process

| Item | Value |
|---|---|
| Container | its own (compose service `mcp`, its own deployment), built from the same image as `api`; part of every SpecFlow deployment, like `api` and the workers |
| Command | `specflow mcp` (overrides the image's default `migrate` then `api` command) |
| Port | `MCP_PORT` ([architecture.md](architecture.md#settings)) |
| Replicas | stateless; any number behind a load balancer, no affinity |
| Depends on | Postgres, Neo4j, the IdP's JWKS endpoint, and a migrated schema (locally: `api` healthy, since the `api` container runs `specflow migrate` first) |
| Probes | the liveness and readiness probes of [architecture.md](architecture.md#http-surface-of-the-role) |
| Database migrations | never; the `api` container runs `specflow migrate` before `specflow api`, so `mcp` starts after `api` on a fresh schema |
| Settings | [architecture.md](architecture.md), [auth.md](auth.md), and the shared database, Neo4j and auth variables |

## Public endpoint

| # | Requirement |
|---|---|
| P1 | The MCP endpoint shares the web app's public host (`PUBLIC_URL`): no new DNS name or certificate. On that host the ingress sends `/mcp` (exact path) and `/.well-known/oauth-protected-resource` with everything below it to the `mcp` service, and every other path to the web app. The web app serves none of these paths. Nothing else of the role is public; the probes stay internal. |
| P2 | `MCP_RESOURCE_URL` is exactly `https://<web app host>/mcp` as clients see it, for example `https://specflow.example.com/mcp`. The host-wide `/.well-known/oauth-protected-resource` path belongs to MCP. |
| P3 | No sign-in proxy (forward-auth, SSO redirect) in front of these paths: the client must receive the `401` and the metadata. |
| P4 | Pass the `Host` header through unchanged; add any internal host names the role is reached by to `MCP_ALLOWED_HOSTS`. |
| P5 | TLS at the edge; request body limit at least the body cap of [Transport](architecture.md#transport); request timeout at least 30 s; no response buffering requirement (JSON responses). |
| P6 | Rate limit per client address at the edge (suggested: 20 requests per second, burst 40, raised where many developers share one office address); the role has no rate limiter of its own. |
| P7 | The REST API (`api` service) never gets a public route; only the `mcp` service is published. The web app keeps reaching the API on the internal network. |

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
binding file is `http://localhost:8002/mcp`. Switching modes needs `claude mcp remove specflow-local --scope local` and a new add.

Dev mode (`make up`), from the repository root. The token expires ([auth.md](auth.md#local-and-development-mode)); add it again after that:

```bash
claude mcp add --transport http --scope local specflow-local http://localhost:8002/mcp --header "Authorization: Bearer $(cd backend && uv run --quiet --env-file ../.env.local python examples/mcp_qa.py dev-token dev@example.com)"
```

Run the `dev-token` command alone first and check that it prints exactly one line: an empty token would register a
server with an empty header. The token is stored in the local Claude Code config. Keep the name and URL before
`--header`, which takes every argument after it. `dev@example.com` is the default `LOCAL_TEST_USER_EMAIL`, the web
app's dev-login user; use your value if you changed it, so Claude Code and the web app see the same projects. For a token
that refreshes, register a `headersHelper` entry at local scope with `claude mcp add-json --scope local specflow-local '<the JSON below>'`
(run from the repository root, to whose local config the entry belongs; Claude Code runs the helper only after you accepted
the workspace trust dialog once; the helper's working directory was not verified, so give the path of the repository
explicitly). The `.mcp.json` at the repository root is tracked: never put a static token in it, and keep
this entry out of it:

```json
{"type": "http", "url": "http://localhost:8002/mcp",
 "headersHelper": "cd <repository>/backend && uv run --quiet --env-file ../.env.local python examples/mcp_qa.py dev-token dev@example.com --headers"}
```

Keycloak (`make up WITH=keycloak`): seed the QA users once, and again after any Keycloak recreate, then add the server
and sign in as `alice@example.com` with `KEYCLOAK_QA_PASSWORD` from `.env.local`:

```bash
cd backend && uv run --env-file ../.env.local python examples/mcp_qa.py seed
claude mcp add --transport http --scope local --client-id specflow-mcp --callback-port 8765 specflow-local http://localhost:8002/mcp
claude mcp login specflow-local
```

### Guides in the web app

The web app shows this developer setup with the deployment's own values, so a developer copies commands instead of
editing placeholders.

| Page | What it shows |
|---|---|
| Guides › MCP server (`/guides/mcp-server`) | The steps of this section for Claude Code, with the address and client id inside the commands and the team file: add the server, sign in, bind a repository, check it. Each command has a copy button. A tools table lists the tools and the prompt ([architecture.md](architecture.md#tool-registry)) |
| Plan › Develop | Per project: `/mcp__specflow__select_project <project id>` with the id filled in, the binding file as JSON ([tools.md](tools.md#the-binding-file)), and a link to the guide |

Only Claude Code has setup steps; Codex and Cursor show "Setup is in progress" until each is tested end to end
([later.md](later.md)). `/guides/mcp-server?agent=claude-code`, `codex` or `cursor` selects the agent.

The page shows one of two variants, chosen by the web app's own `AUTH_MODE`:

| Web app `AUTH_MODE` | Variant |
|---|---|
| `oidc` | The production steps of this section: server `specflow`, client id from `MCP_CLIENT_ID`, sign-in through the IdP, and the team `.mcp.json` |
| `dev` | The local steps of [Local endpoint](#local-endpoint): server `specflow-local`, the `headersHelper` entry with the dev user's e-mail (`LOCAL_TEST_USER_EMAIL`), no sign-in step and no `.mcp.json` |

Limit: with `make up WITH=keycloak` only the `mcp` role switches to `oidc`; the web app keeps dev login
([Local stack](#local-stack)), so the guide shows the local variant there. That mode is for QA; use the commands of
[Local endpoint](#local-endpoint) for it.

### Web app settings for the guides

Operators set two settings on the web app, from the same values as the `mcp` role. Both are read on the server only:
no `NEXT_PUBLIC_` variable exists, and the pages pass the values to the browser as props.

| Web app setting | Same value as the `mcp` role | Default | Rule |
|---|---|---|---|
| `MCP_PUBLIC_URL` | `MCP_RESOURCE_URL` | `PUBLIC_URL` + `/mcp` | An `http` or `https` URL; `https` unless `APP_ENV=local` |
| `MCP_CLIENT_ID` | the first entry of `MCP_CLIENT_IDS` | `specflow-mcp` | Not blank |

With the ingress of [Public endpoint](#public-endpoint) (P1, P2) the default already equals `MCP_RESOURCE_URL`; set the
setting when the two differ. The web app refuses a value that breaks the rule, with an error that names the setting
([../configuration.md](../configuration.md)).

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
| Dev token for Claude Code | from the QA helper command, through `headersHelper` (local scope) or `--header`; lifetime in [auth.md](auth.md#local-and-development-mode) |
| `mcp` with Keycloak | `AUTH_MODE=oidc`, `OIDC_ISSUER=http://localhost:8180/realms/specflow`, `AUTH_JWKS_URL` on the compose network name, and `AUTH_DEV_SIGNING_KEY` empty: `.env.local` always holds a dev key for the web app, and the role refuses any dev key under `oidc` |
| Web app guide values | compose gives the web app `MCP_PUBLIC_URL=http://localhost:8002/mcp` (the web app's `PUBLIC_URL` is `http://localhost:3001`, so the default would be wrong here) and leaves `MCP_CLIENT_ID` at its default. Guides › MCP server at `http://localhost:3001/guides/mcp-server` then shows the local variant ([Guides in the web app](#guides-in-the-web-app)) |
| Web app and API | dev login in both modes. With Keycloak, the MCP users are Keycloak users, so the QA seed provides their data ([testing.md](testing.md)) |

`make up WITH=keycloak` refuses to start (`check-keycloak-env`) when the env file has no non-empty `KEYCLOAK_ADMIN_PASSWORD`; run `make env-local`. Compose keeps an empty default so other profiles are unaffected.

Generated local secrets (Keycloak admin password, QA user password, `specflow-web` client secret) live in `.env.local`
next to the other generated local secrets and are never committed.
