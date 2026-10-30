# 01 — Graph paged read

Depends on: none. Parallel with 02, 03.

## Do

1. Add `GraphReader.count_nodes(project_id, labels)` and `GraphReader.nodes_page(project_id, labels, *, limit=50, after=None) -> NodePage`
   with the contract in `docs/mcp-server/tools.md` (section "Graph read used by list_features"). Export `NodePage` from `graph.core`.
2. Two `.cypher` files in `backend/src/graph/core/queries/` and their `Query` constants in `graph/core/queries.py`; same
   filter and node projection as `read_nodes.cypher` plus `spec_id > $after`, `ORDER BY n.spec_id`, `LIMIT $limit`.
3. Update `docs/graph/api.md` (Read and search table) and `docs/graph/requirements.md` (new `FR-READ-*` rows) for both methods.
4. Unit tests (recording or offline executor) and live tests (`SPECFLOW_TEST_NEO4J=1`), including the renamed-ontology suite.

## Subagents

- cypher-reviewer (short-term): checks the two queries against `validation/cypher/RULES.md` and `backend/cypher-checks.toml`.

## Rules

- `nodes()` and every existing read keep their behavior and signatures.
- Labels pass the same validation as `nodes` (spec labels only; else `ConfigurationError`). No label literal in code (I4).
- `limit` 1 to 500, else `ConfigurationError("invalid_limit")`, like the other reads; MCP caps at 100 on its side.
- The page reads `limit + 1` rows to compute `next_after`; never fetch all and slice.
- `after` is exclusive and compared as a string; `(group_id, spec_id)` uniqueness makes the order total.
- `make validate-cypher` live checks start a Neo4j container; when Docker is unavailable run `--static-only` and say so.

## Done when

- `count_nodes` and `nodes_page` exist, are exported, documented in `docs/graph/api.md` and covered by `FR-READ-*` rows.
- Live: 120 nodes of one label page as 50, 50, 20 with `next_after` null at the end; count is 120; another project's
  nodes never appear; other labels never appear; the renamed ontology pages its renamed label.
- `make check` and the cypher and python validation tracks pass.

## Checklist

- [ ] `count_nodes` implemented; binds `$project_id`; label filter validated
- [ ] `nodes_page` implemented; `NodePage(items, next_after)` frozen model; exported from `graph.core`
- [ ] Two `.cypher` files, loaded through `load_core_query`; no inline Cypher
- [ ] Node projection strips `embedding` and `name_embedding` as `read_nodes.cypher` does
- [ ] `invalid_limit` for 0, 501, non-int, bool
- [ ] Unknown or non-spec label raises `ConfigurationError`
- [ ] Empty project: count 0, empty page, `next_after` None
- [ ] Exactly `limit` nodes left: `next_after` None (probe row logic)
- [ ] `after` beyond the last id: empty page
- [ ] Unit tests: parameters (`limit + 1`, `after`, labels), `$project_id` binding
- [ ] Live tests: pagination, count, isolation across projects, label isolation
- [ ] Renamed-ontology live test covers the new methods
- [ ] Graph structure tests (I1 to I5) still pass
- [ ] `docs/graph/api.md` rows added; `docs/graph/requirements.md` rows added with acceptance criteria
- [ ] Docs bundle check passes (`python -m pystatic.docs.check`)
- [ ] `make validate-cypher` (or `--static-only` with the reason recorded)
- [ ] `make validate-python-lint`, `make validate-python-arch`, `make validate-python-scripts`
- [ ] `make check` green
- [ ] Logging: no new events, or `graph.*` names via the central logger
- [ ] No other module changed outside the owned files
- [ ] `01-handoff.md` written; `HANDOFF.md` row updated
