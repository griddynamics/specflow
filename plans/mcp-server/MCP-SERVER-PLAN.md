# MCP-SERVER-PLAN — session index and tracker

Goal: the `mcp` backend role with `list_projects` and `list_features`, OAuth resource server for any OIDC provider,
project-member isolation, proven with Claude Code and the MCP Python SDK against Keycloak.
Design authority: [`docs/mcp-server/`](../../docs/mcp-server/INDEX.md), which describes the target system. Its open
questions ([`questions.md`](../../docs/mcp-server/questions.md)) carry defaults that sessions apply, so sessions run
without review gates.

## Read first (every session)

1. `docs/mcp-server/INDEX.md`, then `overview.md`, `architecture.md`, `auth.md`, `access-control.md`, `tools.md`, `testing.md`, `questions.md`
2. `plans/mcp-server/discovery.md`: cited facts, environment notes; `plans/mcp-server/spike.md`: working client code
   (sessions 03, 04, 06); `plans/mcp-server/draft-review.md`: what changed from the first draft and why
3. `plans/mcp-server/HANDOFF.md` and the handoffs of the sessions this one depends on
4. `AGENTS.md`, `backend/AGENTS.md`
5. Graph sessions (01, 05): `docs/graph/architecture.md`, `docs/graph/api.md`

## Governing rules

| Rule | Authority |
|---|---|
| The design is binding and open questions use their defaults; a session that finds the design wrong records it under Deviations in its handoff and stops at that point | `docs/mcp-server/` |
| Absolute package imports; central logger (`get_logger`, `log_event`), no `print`, no stdlib logging setup | `backend/AGENTS.md` |
| Cypher only in `.cypher` files of `graph/*/queries`; no ontology label literals in graph code | `docs/graph/architecture.md` (I3, I4) |
| `neo4j` only in `graph`; FastAPI/Starlette only in `api`/`auth`; ORM only in `api`/`db`/`temporal` | `backend/importlinter.toml` |
| Docs bundle: frontmatter, unique ASCII headings, no `file.py:NN` citations, no `§`, no status glyphs | `backend/pystatic.toml` `[docs-bundle]` |
| Every setting documented in the configuration reference | `docs/configuration.md` |
| Before a PR: `make check`, `make validate`, `make frontend-build`, `make e2e-production` | `AGENTS.md` |
| Never commit secrets, env files with values, screenshots or QA images | `AGENTS.md` |
| Public repository: no internal host names, tracker keys, people or customer names in code, docs or commits | request |

## Records

| What | Where |
|---|---|
| Session status, evidence, blockers | `plans/mcp-server/<NN>-handoff.md` (created by the session) and its row in `HANDOFF.md` |
| Requirement coverage | each handoff: a table of the requirement ids the session owns and the tests that prove them |
| Design deviations | the session handoff, section Deviations; design docs change only in session 06 |
| Cross-session issues and fixes | `HANDOFF.md`, section Common issues |
| QA run reports | git-ignored report folder (session 03 adds it) |

## File ownership

| Path | Owner | Others |
|---|---|---|
| `backend/src/graph/**`, `docs/graph/**`, `backend/tests/test_graph_reader*.py`, `backend/tests/test_graph_renamed_ontology_live.py` | 01 | read only |
| `backend/src/auth/**`, `backend/tests/specflow_auth_support.py`, `backend/tests/test_specflow_auth*.py`, `backend/archkit.toml` | 02 | read only |
| `backend/src/settings.py` | 02 (`AuthSettings`, auth guards), then 04 (role `mcp`) | sequential |
| `docs/configuration.md` | 02 (auth rows), then 04 (`MCP_*` rows), then 06 (local stack rows) | sequential; `test_specflow_settings_env.py` fails while a setting read in code has no row |
| `backend/tests/test_specflow_settings_env.py` (`.env.example` key sets) | 06 | |
| `config/keycloak/**`, `backend/examples/mcp_qa*.py`, `backend/tests/test_mcp_qa_lib.py`, `.gitignore` | 03 | 06 extends `mcp_qa_lib.py` |
| `docker-compose.yml`, `Makefile` | 03 (Keycloak, `WITH=keycloak`, `env-local`), then 06 (always-on `mcp` service, variable forwarding) | sequential |
| `backend/pyproject.toml` | 03 (`[tool.deptry]` only), 04 (`[project]` dependencies) | parallel-safe: different tables |
| `backend/uv.lock`, `backend/importlinter.toml`, `backend/cypher-checks.toml`, `backend/src/cli.py` | 04 | |
| `backend/src/api/mcp/**` | 04 (app, runtime, guard, PRM), then 05 (tools, access, errors) | sequential |
| `backend/src/db/session.py`, `backend/src/api/runtime.py` (moving `is_database_unreachable` only) | 05 | |
| `backend/tests/mcp_support.py`, `backend/tests/test_mcp_*.py` (not `qa_lib`, `e2e`, `claude_qa`) | 04, then 05 | sequential |
| `backend/tests/test_mcp_e2e.py`, `backend/tests/test_mcp_claude_qa.py` | 06 | |
| `docs/mcp-server/**`, `backend/README.md`, `.env.example`, `QUICKSTART.md` | 06 | |
| `plans/mcp-server/HANDOFF.md` | every session, its own row only | |

## Sessions

### Phase A: foundations

| # | Session | Depends on | Parallel with |
|---|---|---|---|
| 01 | [Graph paged read](01-plan-graph-paged-read.md) | none | 02, 03 |
| 02 | [Auth claim profile](02-plan-auth-claim-profile.md) | none | 01, 03 |
| 03 | [Local OIDC provider and QA harness](03-plan-local-oidc-and-qa-harness.md) | none | 01, 02, 04 |

### Phase B: the role

| # | Session | Depends on | Parallel with |
|---|---|---|---|
| 04 | [MCP role, transport and auth guard](04-plan-mcp-role.md) | 02 | 01, 03 |

### Phase C: tools

| # | Session | Depends on | Parallel with |
|---|---|---|---|
| 05 | [Tools and access control](05-plan-mcp-tools.md) | 01, 04 | none |

### Phase D: end to end (runs alone)

| # | Session | Depends on | Parallel with |
|---|---|---|---|
| 06 | [Stack wiring, real-client QA, docs, release check](06-plan-e2e-qa-and-release.md) | 03, 05 | none; uses ports 8000, 8002, 8180, 8765 and the whole stack |

Pairs not listed as parallel are sequential. Parallelism is valid only because those sessions share no files, except
`backend/pyproject.toml`, where 03 and 04 own different tables. Session 06 runs alone.

## Environment notes (cloud container)

- Start `dockerd` if `docker info` cannot reach the daemon.
- Docker Hub may answer 429: pull `mirror.gcr.io/<image>` and tag it; pinned digests match. Keycloak comes from `quay.io`.
- `make env-local` writes `.env.local`; the API on the host needs a placeholder `AGENT_MODEL` without an LLM gateway.
- Keycloak cookies are `Secure` on http; `claude mcp login --no-browser` prints its URL inside an OSC 8 escape and
  stores MCP tokens in the real `~/.claude` (always log out and remove QA servers).
