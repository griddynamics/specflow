# 05 — Tools and access control

Depends on: 01, 04. Runs alone in its phase.

## Do

1. The single membership function and user provisioning of `docs/mcp-server/access-control.md` (rules AC1 to AC7),
   built on `db.repositories.upsert_user` and `get_member_project`.
2. `list_projects` per `docs/mcp-server/tools.md`, on `db.repositories.list_projects`.
3. `list_features` per tools.md, on `GraphReader.count_nodes` and `nodes_page` (session 01); label from the ontology
   node type key `feature`; attributes from the ontology's stored attribute specs.
4. Input and output schemas, annotations and model-facing descriptions as written in tools.md.
5. Cursor codecs (projects: the repository cursor; features: versioned opaque cursor) and the error mapper with the
   codes and messages of tools.md.
6. `mcp.tool.completed` audit event per call (architecture.md, Logging and audit).
7. Tests: unit (recording executor, fake sessions), HTTP-boundary through an SDK client, the structural membership
   test, live tiers (`SPECFLOW_TEST_POSTGRES=1`, `SPECFLOW_TEST_NEO4J=1`), renamed ontology.

## Subagents

- access-auditor (short-term): tries to read another member's project through every tool and argument combination;
  reports any path that reaches the graph before the membership check.

## Rules

- The graph reader gets `str(project.id)` from the membership result, never the raw argument.
- A malformed `project_id` is `project_not_found`, not a schema error (tools.md); parse it inside the tool.
- Startup fails when the ontology has no `feature` node type; check in the runtime built by 04.
- `module` is `store: false` in the ontology: it is not an attribute.
- Map `graph.core.InfrastructureUnavailableError` to `graph_unavailable`. Classify database errors with the API's
  `is_database_unreachable`: move it unchanged from `api/runtime.py` to `db/session.py` and import it from both (no
  copy, and `api.mcp` never imports `api.runtime`). A `ConfigurationError` from the reader is a bug: `internal_error`.
- No SQL and no Cypher in `api.mcp` (contracts from 04).
- Messages are the fixed strings of tools.md; nothing from exceptions reaches a result.

## Done when

- FR-MCP-ACC-01 to 06, FR-MCP-TOOL-01 to 10 and FR-MCP-OPS-07 have passing tests (TOOL-10 is session 01's).
- `tools/list` shows exactly the two tools with schemas, annotations and descriptions of tools.md.
- `make check` and the validation tracks are green.

## Checklist

- [ ] Membership function; every project-scoped tool calls it first
- [ ] Structural test over the registered tools
- [ ] `upsert_user` once per call; one user row per subject under concurrency
- [ ] `list_projects`: newest first, `limit` bounds, cursor passthrough, `invalid_cursor`
- [ ] `list_features`: `total_count`, ordering, `limit + 1`, `next_cursor`, versioned cursor, `invalid_cursor`
- [ ] Only `feature` nodes; modules and requirements never listed
- [ ] Attributes from ontology stored specs; absent values omitted; no embeddings, statements, provenance
- [ ] Malformed, unknown and foreign ids give byte-identical results
- [ ] Denied membership: zero graph queries (recording executor)
- [ ] Membership removed between calls: second call `project_not_found`
- [ ] Empty graph: `total_count` 0
- [ ] Neo4j failure: `graph_unavailable`; Postgres failure: `database_unavailable`; other: `internal_error`
- [ ] Results carry structured content and the same JSON as text; errors carry `isError`
- [ ] Annotations and descriptions exactly as tools.md
- [ ] Audit event fields; no feature text, cursor or email in logs
- [ ] Live Postgres tier: two users, isolation
- [ ] Live Neo4j tier: real counts and pages; renamed ontology
- [ ] HTTP-boundary: both tools through an SDK client with stub tokens
- [ ] `make check`; `make validate-python-lint`, `-arch`, `-scripts`
- [ ] `05-handoff.md` (evidence, deviations); `HANDOFF.md` row updated
