# 04 — MCP role, transport and auth guard

Depends on: 02. Parallel with 01, 03.

## Do

1. Declare `mcp>=2.3,<3` in `backend/pyproject.toml`; update `uv.lock` (moves the transitive 2.1.1 to 2.3.x).
2. Role `mcp` in `settings.py` with the settings and startup refusals of `docs/mcp-server/architecture.md` (Settings)
   and `auth.md` (Token validation, the audience guard from 02); no Temporal, storage, LLM or agent-database binding.
3. `specflow mcp [--check-config]` in `cli.py`.
4. Package `backend/src/api/mcp/`: app factory with the routes of architecture.md (HTTP surface of the role); SDK
   `MCPServer` without SDK auth; Streamable HTTP, stateless, JSON responses, 1 MiB body cap, Host and Origin rules.
5. Slim runtime: DB engine and session factory, Neo4j driver, ontology loaded at startup, one `GraphReader` without a
   gateway, readiness (database, Neo4j); lifespan also runs the SDK session manager.
6. Auth guard and PRM per auth.md (Protected resource metadata, Token validation, Failure responses); the `Principal`
   is available to tools through the request.
7. Import-linter contracts for rules M2 to M4 of architecture.md.
8. Log events of architecture.md (startup, shutdown, `mcp.auth.rejected`).
9. HTTP-boundary tests (`backend/tests/mcp_support.py`, `test_mcp_*.py`) with the loopback JWKS stub; the REST app
   test that an MCP-audience token gets 401.

## Subagents

- sdk-scout (short-term): confirms SDK 2.3.x names used (`MCPServer`, `streamable_http_app` options,
  `TransportSecuritySettings`, session manager) against the installed package before code is written.
- security-reviewer (short-term): reviews the guard and PRM against auth.md security rules S1 to S6.

## Rules

- A Starlette app returned by the SDK runs its session manager in its own lifespan; when composed, run
  `session_manager.run()` in the role's lifespan.
- Do not pass `auth=`/`token_verifier=` to the SDK; the guard owns 401 and 403 (auth.md AA3).
- PRM values come from settings only; never from `Host` or `X-Forwarded-*`.
- Transport security compares the `Host` header with its port; derive the default allowlist from `MCP_RESOURCE_URL`.
- Bumping `mcp` reaches `claude-agent-sdk`, `openai-agents`, `temporalio-openai-agents`: the full suite, including
  agent and Temporal tests, must pass.
- The image's default command migrates then starts `api`; the role never migrates.
- `api.app` must not import `api.mcp`; `api.mcp` must not import FastAPI or `api.routers`/`api.deps`.
- No tools are registered yet (05); tests reach `initialize` and `tools/list` through an SDK client over HTTP.

## Done when

- `specflow mcp --check-config` passes with only `APP_ENV`, database, Neo4j, `OIDC_ISSUER`, `AUTH_ALLOWED_EMAIL_DOMAINS`, `MCP_RESOURCE_URL`.
- Requirements FR-MCP-AUTH-01 to 07, 09 to 11 and FR-MCP-OPS-01 to 06 have passing tests.
- `make check` and `make validate-python-*` green.

## Checklist

- [ ] Dependency declared; lock updated; agent and Temporal tests green
- [ ] Role `mcp` in `ROLES` and CLI metavar; `--check-config`
- [ ] Settings: resource URL path exactly `/mcp`; https outside local; audience default; CSV lists; port default 8002
- [ ] Refusals: dev mode, dev key, no issuer, empty clients or scopes, audience equals a client id; all bad names reported together
- [ ] Runtime opens DB and Neo4j lazily; ontology loaded at startup; closes everything on shutdown
- [ ] `/healthz`, `/readyz` (database, Neo4j) reuse `health`
- [ ] Stateless, JSON responses; body cap 1 MiB; Host allowlist; Origin refusal
- [ ] Unknown path: 404 JSON `not_found`
- [ ] PRM at both locations; identical; values from settings
- [ ] 401 without token: `resource_metadata` and `scope` in the challenge
- [ ] 401 invalid token: `error="invalid_token"`
- [ ] 403 `insufficient_scope` with challenge; 403 email policy without challenge
- [ ] Principal reachable from a tool via the request (test-only tool or context probe)
- [ ] REST rejects MCP-audience token; MCP rejects REST-audience token
- [ ] No token, header or email in logs (log capture test)
- [ ] `mcp.auth.rejected`, startup and shutdown events via the central logger
- [ ] Two app instances: `initialize` on one, `tools/list` on the other succeeds (no session affinity)
- [ ] Import-linter contracts M2 to M4; planted violation fails locally
- [ ] deptry clean with the new dependency
- [ ] `make check`; `make validate-python-lint`, `-arch`, `-scripts`
- [ ] `04-handoff.md` (SDK facts confirmed, deviations); `HANDOFF.md` row updated
