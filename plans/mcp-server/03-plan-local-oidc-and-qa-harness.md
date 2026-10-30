# 03 — Local OIDC provider and QA harness

Depends on: none. Parallel with 01, 02, 04.

## Do

1. Compose service `keycloak` in a new profile `mcp`, as specified in `docs/mcp-server/deployment.md` (section "Local
   stack"): pinned Keycloak 26.x image from `quay.io`, `start-dev`, realm import, healthcheck, `127.0.0.1:8180`,
   `KC_HOSTNAME=http://localhost:8180`. The `mcp` service itself is session 06.
2. Realm file `config/keycloak/specflow-realm.json` with the clients, scope and audience mapper of deployment.md; no
   users, no secrets, `offline_access` allowed for `specflow-mcp`.
3. `Makefile`: `WITH` accepts `mcp`; `env-local` also generates `KEYCLOAK_ADMIN_PASSWORD`, `KEYCLOAK_QA_PASSWORD`,
   `KEYCLOAK_WEB_CLIENT_SECRET`, appending only names missing from an existing file.
4. QA library `backend/examples/mcp_qa_lib.py`, used by the tests of 06 and by a manual entry:
   - Keycloak admin: ensure the QA users of `docs/mcp-server/testing.md` (Fixtures), set the web client secret
   - login helper: authorization URL to loopback redirect URL over plain HTTP (testing.md "Login helper")
   - token helper: authorization code + PKCE for `specflow-mcp` (and for `specflow-web`, for the cross-audience case)
   - seed: Postgres users, projects and memberships through `db.repositories`; Neo4j fixtures through `OwnedWriter`; idempotent
   - membership toggle for scenario E8
   - Claude driver plumbing: pseudo-terminal runner for `claude mcp login --no-browser`, OSC 8 URL extraction,
     stream-json tool-event extraction, add/remove/logout of a uniquely named server
5. Manual entry `backend/examples/mcp_qa.py` with `seed` and `token <user>` (token printed only on request).
6. Offline unit tests `backend/tests/test_mcp_qa_lib.py` for the parsing and plumbing parts.
7. `.gitignore`: the QA report folder; `[tool.deptry]` first-party list gains `mcp_qa_lib`.

## Subagents

- keycloak-operator (short-term): brings the profile up and proves token shape and idempotent seeding.

## Rules

- No password, client secret or token in any committed file; the realm file passes the secret scan.
- Keycloak sets its session cookies `Secure` even on `http://localhost`: the login helper forwards them itself.
- Realm import runs only when the realm is absent: users and secrets are ensured through the admin API at seed time.
- Tokens carry `iss=http://localhost:8180/realms/specflow` only because of `KC_HOSTNAME`; do not remove it.
- `claude mcp login` needs a pseudo-terminal; its URL sits inside an OSC 8 escape; tokens land in the real `~/.claude`:
  always remove and log out the QA server in a `finally`.
- The MCP Python SDK client part of the library waits for 04 (dependency declared there); this session imports no `mcp` module.
- Seed calls `ensure_schema` before writing graph fixtures; graph ids are `str(project uuid)`.
- Docker Hub rate limits: `mirror.gcr.io` for Docker Hub images; start `dockerd` if needed.

## Done when

- `docker compose --profile mcp up -d keycloak` is healthy from a fresh `.env.local`, and an existing `.env.local`
  gains only the three new names.
- `uv run --env-file ../.env.local python examples/mcp_qa.py seed` run twice leaves the same users, projects,
  memberships and node counts.
- The token helper returns an alice token whose decoded claims (names and non-secret values) show
  `aud=[http://localhost:8002/mcp, account]`, `azp=specflow-mcp`, `specflow:read` in `scope`, verified email.

## Checklist

- [ ] Keycloak image pinned (tag and digest), profile `mcp`, healthcheck, loopback port only
- [ ] Realm: `specflow-mcp` public, standard flow only, PKCE S256, both loopback redirect URIs
- [ ] Realm: `specflow-web` confidential, audience `specflow-api`, web redirect URI of the local frontend
- [ ] Realm: client scope `specflow:read` optional on `specflow-mcp`; audience mapper `http://localhost:8002/mcp` (access token only)
- [ ] Realm file contains no `credentials`, `secret` or user entries
- [ ] `WITH=mcp` accepted; error message lists `mcp`
- [ ] `env-local` append logic: never rewrites existing lines, never prints values
- [ ] Admin helper idempotent (existing users and secret left consistent)
- [ ] Login helper: form discovery, credential post, cookie forwarding, redirect capture (`code`, `state`, `iss`)
- [ ] Token helper: PKCE S256, exact redirect URI, token request; no token logged
- [ ] Seed: users keyed by Keycloak user ids; `P-Alpha`, `P-Beta`, `P-Empty`; memberships per testing.md
- [ ] Seed: graph fixtures through `OwnedWriter` (7 features, 2 modules, 3 requirements in `P-Alpha`; 3 features in `P-Beta`)
- [ ] Seed idempotence verified by counts
- [ ] Membership toggle for E8
- [ ] Claude plumbing: pty runner with timeout, OSC 8 extraction, stream-json tool events, unique server name, teardown in `finally`
- [ ] Port 8765 free-check before login
- [ ] Unit tests for every parser and the teardown path
- [ ] `.gitignore` entry for QA reports
- [ ] deptry first-party entry; `make validate-python-lint` green
- [ ] `make check` green; secret scan clean
- [ ] Docstring of `examples/mcp_qa.py` explains each command and its environment
- [ ] `03-handoff.md` with the evidence (claim names, counts); `HANDOFF.md` row updated
