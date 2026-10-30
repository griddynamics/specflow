# Discovery: MCP server for the SpecFlow backend

Rosetta `discovery` output. Facts only, cited at the commit this plan was written on (`01a02b1`, "SpecFlow 2.0.0").
Line numbers drift; the symbol names are the stable anchor. Durable conclusions live in
[`docs/mcp-server/`](../../docs/mcp-server/INDEX.md); this file is a planning artifact and may be deleted after delivery.

## What the request turns on

1. Who the caller is: an OIDC access token from a developer's Claude Code, mapped onto the same user row the web app uses.
2. What the caller may see: only projects they are a member of, and only the graph of those projects.
3. Where the graph read lives: the graph library confines Neo4j and Cypher to `graph/`.
4. How the endpoint is hosted: the API process is internal-only and needs Temporal to start.
5. How it is proven: a real client (Claude Code, the MCP Python SDK) against a real OIDC provider.

## Authentication today

| Fact | Evidence |
|---|---|
| One claim-validation path for every verifier; 401 `invalid_token`, 403 for email policy | `backend/src/auth/claims.py:89-122` |
| Keycloak-shaped policy: `typ` claim must be `Bearer`, client from `azp`, `email_verified` must be true in the access token, extra audience `account` tolerated | `auth/claims.py:31,97,110-114,145-153` |
| `aud` must be exactly one non-ignored audience equal to the policy audience | `auth/claims.py:96-98` |
| `Principal` carries subject, email, name, issuer, client id; no scopes | `auth/claims.py:34-43` |
| OIDC verifier: RS256/ES256 via JWKS, `kid` required; dev verifier: HS256, local only | `auth/verifiers.py:21-23,53-92` |
| JWKS cache: 600 s key lifetime, negative cache, throttled single-flight refetch, issuer-host check on discovery | `auth/jwks.py:1-20,220-250` |
| Bearer parsing with size cap and charset check; the module imports FastAPI | `auth/dependencies.py:12,25-34` |
| Check order today: iss, aud, exp, iat, nbf, sub, azp, typ; then email_verified, email validity, domain | `auth/claims.py:94-121,145-153` |
| Every env name read in code must have a `docs/configuration.md` row; `.env.example` pinned to 22 required + 10 optional keys | `backend/tests/test_specflow_settings_env.py:130-152` |
| Settings: `AUTH_MODE` (oidc default), `OIDC_ISSUER`, `AUTH_JWKS_URL`, `AUTH_API_AUDIENCE`=`specflow-api`, `AUTH_API_CLIENT_IDS`=`specflow-web`, `AUTH_ALLOWED_EMAIL_DOMAINS` | `backend/src/settings.py:263-285`, `docs/configuration.md:34-70` |
| Guards: dev mode refused outside local and in Kubernetes; https issuer outside local; JWKS on issuer origin outside local; an `http` issuer is accepted in local | `settings.py:691-721` |
| Web: Auth.js v5, PKCE+state+nonce, scopes `openid email profile`, forwards the raw access token to the API through a server-side proxy; browser never calls the API | `frontend/auth.ts:22-34,96-105`, `frontend/app/api/backend/[...path]/route.ts:59-64,122-140` |
| `aud=specflow-api` comes from IdP configuration (no audience/resource parameter sent by the web) | searched `frontend/` for `audience`, `resource=`: absent |

## Users, projects, membership

| Fact | Evidence |
|---|---|
| User row created or refreshed on every authenticated request, keyed by `users.auth_subject` (unique, issuer not stored) | `backend/src/api/deps.py:24-28`, `db/repositories.py:162-196`, `db/models.py` (`ix_users_auth_subject`) |
| Project roles: `("owner",)` only; creator becomes owner | `db/models.py:34`, `db/repositories.py:199-208` |
| Member projects: join on `project_members`; keyset paging on `(created_at desc, id desc)`, opaque base64url cursor, limit+1 probe | `db/repositories.py:101-158,211-228` |
| Non-member and missing project answer the same 404 `project_not_found` | `api/routers/projects.py:63-69`, `api/routers/documents.py:62-65` |
| Row-level-security role exists only for agent tool activities, scoped to one project by `app.project_id` | `db/agent_scope.py:1-30`, `migrations/versions/*` (`agent_project_scope`) |

## Graph access

| Fact | Evidence |
|---|---|
| `GraphReader.nodes(project_id, labels, where)` returns every match, ordered by `spec_id`; no limit, offset, count | `graph/core/reader.py:283-300`, `graph/core/queries/read_nodes.cypher` |
| Executor refuses a scoped query without `$project_id` and injects it | `graph/core/queries.py:1-12`, `graph/core/db.py:182-210` |
| Cypher lives only in `.cypher` files under `graph/core/queries` and `graph/build/queries`; the cypher track scans only those | `backend/cypher-checks.toml`, `docs/graph/architecture.md` (I3) |
| The Cypher track's Python scan excludes `api/**`: inline Cypher planted in `api/mcp` is missed today and caught once the exclusion goes; today's API code passes the scan | `cypher-checks no-inline-queries` on a scratch copy of `backend/src` |
| The SQL track scans all of `backend/src` (only `src/graph/*` excluded): inline SQL planted in `api/mcp` is caught | `backend/sqlcheck.toml`; `sqlcheck run --step inline-queries` on a scratch copy |
| `neo4j` and `graphiti_core` imports are forbidden outside `graph` | `backend/importlinter.toml` (`graph-sdk-confined`) |
| `fastapi`/`starlette` only in `api` and `auth`; ORM only in `api`, `db`, `temporal` | `importlinter.toml` (`web-framework-confined`, `orm-confined`) |
| Ontology: node type key `feature` → label `Feature`, id `FEAT::{SLUG}`, attributes `stmt`, `quote`, `module` (`store: false`) | `graph/core/ontology/spec-ontology.yaml:148-182` |
| `Ontology.node_types` keyed by type key; `node_type(label)` by label | `graph/core/ontology/compiled.py:60-90,280-335` |
| Graph group id is `str(project uuid)` | `api/routers/graph_build.py:53-60` |

## Hosting

| Fact | Evidence |
|---|---|
| Roles: `api`, `worker`, `migrate`; one CLI entry per role | `backend/src/cli.py:1-60,147-149`, `settings.py:140` |
| API start fails when Temporal is unreachable; builds storage, Temporal, Neo4j, DB | `api/runtime.py:183-232` |
| API is internal: no CORS, browser talks only to the BFF | `api/app.py:108-112` |
| Image default command runs `migrate` then `api` | `backend/api.Dockerfile:44` |
| Readiness probes reusable: `database_probe`, `neo4j_probe`, `Readiness` | `backend/src/health.py:104-160` |
| Host ports in use: 3000, 3001, 5433, 7233, 7474, 7687, 8000, 8233, 19000; 8001 is the worker health port | `docker-compose.yml`, `docs/configuration.md:61` |

## MCP SDK and clients (verified in this environment)

| Fact | Evidence |
|---|---|
| `mcp` 2.1.1 is already locked as a transitive dependency of `claude-agent-sdk`, `openai-agents`, `temporalio-openai-agents`; no direct import | `backend/uv.lock`; `grep -rn "import mcp" backend/src`: absent |
| `mcp==2.3.0` resolves with the backend lock (only `mcp`, `mcp-types` change) | `uv add mcp==2.3.0` in a temp copy |
| 2.3.0: `MCPServer`, `streamable_http_app(stateless_http, json_response, max_request_body_size=4 MiB, session_idle_timeout, max_sessions, transport_security)`, `TransportSecuritySettings(allowed_hosts, allowed_origins)`, `AuthSettings.validate_token_resource`; the returned Starlette app runs the session manager in its own lifespan | `mcp/server/mcpserver/server.py`, `mcp/server/lowlevel/server.py`, `mcp/server/auth/settings.py`, `mcp/server/transport_security.py` |
| 2.1.1 lacks `validate_token_resource` | `backend/.venv/.../mcp/server/auth/settings.py` |
| Claude Code CLI: `claude mcp add --transport http --client-id --callback-port`; writes `oauth.clientId`/`oauth.callbackPort` into `.mcp.json`; `claude mcp login <name> --no-browser` prints the URL and reads the redirect URL | `claude mcp add --help`, `claude mcp login --help`, spike |
| Claude Code authorize request: PKCE S256, `scope=<PRM scopes> offline_access`, `prompt=consent`, `resource=<MCP URL>`, `redirect_uri=http://localhost:<callbackPort>/callback` | spike capture |
| Keycloak 26.4 token for a public client with an audience mapper: `aud=[<MCP URL>, account]`, `azp=specflow-mcp`, `scope` includes `offline_access profile email specflow:read` | spike server log |
| Spike end to end: Claude Code login + `claude -p` tool call, and SDK `OAuthClientProvider` client, both succeeded against an SDK 2.3.0 resource server | spike (scratchpad, not committed) |

## Prior attempts

| Attempt | Verdict |
|---|---|
| v1 `mcp_server/` package: local stdio FastMCP server, 7 tools over the v1 generation pipeline, optional `X-API-Key`, Textual TUI | Absent now: deleted in `01a02b1`. Different product surface, no OIDC, nothing reusable for authenticated graph reads |
| Frontend Plan "Develop" tab | Mock only: "No MCP server exists yet"; offers `.mcp.json` snippets (`frontend/components/plan/develop/DevelopTab.tsx`) |
| Draft plan supplied with the request (MCP mounted in FastAPI, MCP-owned Cypher) | Present as text only; reviewed in [draft-review.md](draft-review.md) |

## Rosetta (griddynamics/rosetta) MCP hosting

| Fact | Evidence |
|---|---|
| `mcp>=1.26,<2` + `fastmcp>=3.4,<4`; HTTP transport with OAuth required | `src/rosetta-mcp-server/pyproject.toml:29-30`, `rosetta_mcp/auth/oauth.py:41-60` |
| OAuth proxy pattern: server is the AS for MCP clients (DCR + CIMD, consent screen), one confidential upstream client, own JWTs, upstream tokens encrypted (Fernet) in Redis; modes `oauth` (introspection), `oidc`, `github` | `rosetta_mcp/auth/oauth.py:1-149`, `rosetta_mcp/server.py:220-290` |
| Workarounds: Keycloak `refresh_expires_in=0` with `offline_access` dropped refresh tokens; loopback redirect URIs with ephemeral ports | `rosetta_mcp/auth/offline_refresh_fix.py`, `rosetta_mcp/auth/loopback_redirect_fix.py` |
| Hosting advice: rate-limit `/register`, `/token`, `/authorize` | `SECURITY.md:95-98` |
| Multi-tenancy or per-project authorization | absent (searched `tenant`, `project_member`, `membership`, `is_member` in `rosetta_mcp/`) |

## Validation rules that bind the deliverables

| Fact | Evidence |
|---|---|
| Docs bundle check: frontmatter `type,title,description,status,tags`; types `Overview, Architecture, DataModel, Ontology, Design, API, Requirements, Roadmap, Questions, Changelog, Explainer`; unique plain-ASCII unnumbered headings; unique ids `[A-Z]{1,6}-...`; ephemeral markers flagged (`file.py:NN`, `§N`, status glyphs, `rev N`) | `backend/pystatic.toml` `[docs-bundle]`, `validation/python/scripts/pystatic/docs/settings.py:18-30` |
| Every Python/TS/JS source must sit in a project folder listed in root `pystatic.toml` | `pystatic.toml`, `validation/python/scripts/pystatic/scripts/source_ownership.py` |
| archkit error-code registry for the API app | `backend/archkit.toml` `[error_codes]` |

## Environment (this cloud container)

| Check | Result |
|---|---|
| Docker | CLI present, daemon not running; `dockerd` started by hand works |
| Docker Hub | anonymous pulls rate-limited (429); `mirror.gcr.io/<image>` works and digests match the pins |
| Postgres 17 + Neo4j 5.26 (compose) | healthy; `make db-upgrade` ok |
| `make check` | 3879 passed, 431 skipped |
| Live tiers `SPECFLOW_TEST_NEO4J=1 SPECFLOW_TEST_POSTGRES=1` (reader, core, sources, migrations) | 119 passed |
| `make api` on host (dev auth, Temporal + RustFS) | starts with a placeholder `AGENT_MODEL`; database, Temporal, Neo4j ready; storage readiness fails (S3 `ClientError`, not investigated, not on the MCP path) |
| Keycloak 26.4 (`quay.io`) | runs; realm, public PKCE client, audience mapper, scope, user created with `kcadm.sh` |
| Keycloak cookies | marked `Secure` even over http: a non-browser login must forward them by hand; headless Chromium works |
| Playwright | `page.route` does not see redirect hops; the `request` event does |
| `claude` CLI | headless `claude -p` works; its MCP OAuth tokens land in the real `~/.claude` store |

## Not examined

- Production ingress and IdP configuration (not in this repository).
- Helm or Kubernetes manifests (absent: searched `helm`, `kind: Deployment`).
- IdPs other than Keycloak (no access here); their recipes in the design are documented, not tested.

## Confidence

High for backend facts and the Keycloak + Claude Code path (executed). Medium for other IdPs (documentation-based). Weakest link: how
each non-Keycloak IdP treats the RFC 8707 `resource` parameter that Claude Code sends.
