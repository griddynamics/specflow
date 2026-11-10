---
type: API
title: "Graph library API"
description: "Public library API: core setup, owned writes, read and search, LLM gateway, judges, build entry points, ports"
status: stable
tags: [graph]
---
# api.md: public library API: core setup, owned writes, read and search, LLM gateway, judges, build entry points, ports

All functions are `async` unless noted. Every graph function takes `project_id` explicitly. Other
exports, not described here: identity helpers (`NAMESPACE`, `node_uuid`, `edge_uuid`, `identity_key`,
`normalise_id`, `normalise_surface`, `slug`, `allocate_spec_id`, `validate_project_id`, `SpecIdRow`),
configuration (`Neo4jConfig`, `LLMConfig`, `CallBudget`, `CallClass`, `CALL_CLASSES`), `Query`,
`ScopedTransaction`, the errors (`GraphError` and its subclasses), result and record models, step
constants (`STEPS`, `STEP_FUNCTIONS`, `StepFunctions`, `MAX_FOLLOW_UPS`, `EXTRACTION_KINDS`) and the
`File*` port implementations.

## Core setup

| Name | Contract |
|---|---|
| `load_ontology(path=None, *, submodule_files=None) -> Ontology` (sync) | Shipped ontology by default; `submodule_files` None merges the installed submodules' `ontology.yaml` files, a list replaces discovery; raises `OntologyError` ([ontology.md](ontology.md)) |
| `graph.stores.build_store(StoreConfig) -> GraphStore` (sync) | The store of `config.backend` (`neo4j`, `arcadedb`, `postgres`, `tigergraph`); imports only that adapter and its SDK; no connection is opened until first use. A backend without an adapter raises `ConfigurationError("store_not_available", store=...)`, one whose SDK is missing `ConfigurationError("store_sdk_missing", store=..., install_extra=...)`. The host owns the store and calls `close()` |
| `StoreConfig(backend, uri, user, password, name=None, admin_uri=None, pool_size=None)` | Immutable settings (`password` is a secret); `name` is the backend's database, schema or graph (None: the backend's default); the host builds it from its settings (`GRAPH_STORE_*`, [configuration.md](../configuration.md#graph-store)), the library reads no environment; `Neo4jConfig` still works and converts with `StoreConfig.from_neo4j` |
| `GraphStore` | `info` (`StoreInfo`: backend name, server version once read, `capabilities.multi_statement_tx`, `capabilities.shared_vector_index`), `executor()`, `await check()`, `await ensure_schema(ontology, embedding_dim, *, ddl=None) -> tuple[str, ...]`, `await close()` |
| `ensure_schema(store, ontology, llm_config, database=None, *, ddl=None) -> tuple[str, ...]` | Creates missing constraints and indexes and returns the names it created; checks the server version; idempotent (a second run returns `()`); refuses a vector index of another dimension or similarity. The objects come from the static DDL files (`ddl` None: the shipped `graph/core/ddl/*.cypher`; a sequence of statements replaces them), which must cover the ontology (else `ConfigurationError("schema_ddl_incomplete")`) |
| `check(store, database=None)` | Raises `InfrastructureUnavailableError` when the store is unreachable, `ConfigurationError` on rejected credentials, denied access or an unknown database |
| `connect(Neo4jConfig) -> driver` (sync) | Deprecated: creates the Neo4j driver; the host owns and closes it |

Deprecated for one release: passing a Neo4j driver where a store is expected (`ensure_schema`, `check`,
`GraphReader`, `OwnedWriter`, `BuildContext.driver`, with `database` naming its database; None uses the
driver's default). The driver is wrapped in the Neo4j store. `Executor` and `ScopedTransaction` are protocols
(project-scoped query runner and its transaction); `graph.stores.neo4j.Neo4jStore(driver, database).executor()`
builds one from a driver. Also exported: `require_version`, `server_version` (Neo4j).

## Store errors

Every store reports failures with the same codes and the field `store=<backend name>`, whatever the
backend. Fields never hold server messages, queries' text or secrets.

| Code | Error class | Meaning |
|---|---|---|
| `store_unreachable` | `InfrastructureUnavailableError` | The store cannot be reached |
| `store_auth_rejected` | `ConfigurationError` | The credentials are rejected |
| `store_access_denied` | `ConfigurationError` | The credentials may not do this (field `query`) |
| `store_database_not_found` | `ConfigurationError` | The configured database, schema or graph does not exist |
| `store_version_unsupported` | `ConfigurationError` | The server is older than the adapter's minimum (fields `found`, `minimum`) |
| `store_version_unknown` | `ConfigurationError` | The server version could not be read |
| `store_write_failed` | `GraphWriteError` | A write failed after retries (fields `query`, `error_type`, `backend_code`, `attempts`) |
| `store_query_rejected` | `ConformanceError` | The server rejected a read query: a bug |
| `store_read_failed` | `InfrastructureUnavailableError` | A read failed for another reason |
| `query_too_complex` | `ConformanceError` | The backend refuses a search text as too complex; the reader turns it into `ConfigurationError("query_too_long")` |
| `store_not_available`, `store_sdk_missing` | `ConfigurationError` | Routing: no adapter for the backend, or its SDK is not installed |
| `store_transactions_unsupported` | `ConfigurationError` | `OwnedWriter.transaction()` on a store without multi-statement transactions |
| `store_uri_invalid` | `ConfigurationError` | The URI (or admin URI) is not valid for the backend (ArcadeDB, PostgreSQL) |
| `store_name_invalid`, `store_database_name_invalid` | `ConfigurationError` | The schema name (PostgreSQL) or database name (ArcadeDB) is not a safe identifier |
| `store_extension_missing` | `ConfigurationError` | PostgreSQL: the `vector` extension is not installed in the database |
| `store_schema_outdated` | `ConfigurationError` | PostgreSQL: the schema carries no or another schema version; it is not upgraded in place |
| `vector_index_mismatch` | `ConfigurationError` | The vector index or column has another dimension or similarity than the settings |
| `schema_setup_failed` | `ConfigurationError` | The server refused a schema statement (names the statement, never the server text) |
| `arcadedb_edge_types_missing`, `arcadedb_unrenderable_edge_type` | `ConformanceError` | ArcadeDB: a merge, move or label repair was planned without the relationship types, or would drop an edge of a type the dialect cannot render: a bug |

`backend_code` carries the server's own error code (for Neo4j and ArcadeDB `Neo.ClientError...`, for PostgreSQL the SQLSTATE). Transient failures
are retried inside the executor (delays 0.1, 0.3 and 1.0 s) before they surface.

## Owned writes

`OwnedWriter(store_or_executor, ontology, owner, project_id, *, database=None)` with `owner` in `build`,
`gap`, `plan`. Every operation checks that the type belongs to the owner (else `OwnershipError`), stamps
`group_id`, and is idempotent. Operations may be grouped into one transaction with
`async with writer.transaction() as w:` (commits at the end, rolls back on any error); a store whose
`info.capabilities.multi_statement_tx` is false refuses it with `ConfigurationError("store_transactions_unsupported")`.

| Operation | Contract |
|---|---|
| `upsert_node(label, key, properties)` | Create or update by key; sets only the given properties |
| `upsert_relationship(rel_type, source, target, key, properties)` | Create or update between existing nodes; the pair must be legal for declared types |
| `delete_relationship(rel_type, key) -> bool` | Absent is a no-op (False) |
| `delete_node(label, key) -> bool` | Deletes the node and its relationships (False when absent); a non-build owner may not delete a node that has relationships of other owners |
| `move_foreign_relationships(from_node, to_node) -> int` | Build only: moves every relationship build does not own (W3); returns the number moved |

## Read and search

`GraphReader(store_or_executor, ontology, gateway=None, *, database=None, vector_floor=0.60)`. `vector_floor`: lowest vector
score `(1+cos)/2`, 0..1, for a vector-only hit; set for the embedding model (default tuned for
text-embedding-3-large). Returns spec nodes only (never documents, fragments or
foreign nodes) unless a method says otherwise. Aliases of `spec_id` resolve. Read results never carry
vectors. Invalid arguments raise `ConfigurationError`: `limit` outside 1-500 (`invalid_limit`), `impact`
depth outside 1-3 (`invalid_depth`), a line range not 1 ≤ `line_start` ≤ `line_end` (`invalid_line_range`),
`vector_floor` outside 0..1 (`invalid_vector_floor`).

| Method | Contract |
|---|---|
| `node(project_id, spec_id) -> GraphNode \| None` | One node |
| `nodes(project_id, labels=None, where=None) -> list[GraphNode]` | All spec nodes, ordered by `spec_id`; `where` maps ontology attribute names to required values (equality on stored values; enum values as declared; a null value of an attribute with a `default` matches that default; `None` means the property is absent and the attribute has no default; inherited values such as `release` are filled in after filtering and are not filterable) |
| `nodes_by_spec_ids(project_id, spec_ids) -> list[GraphNode]` | The spec nodes holding any of `spec_ids` as `spec_id` or alias, as `nodes` gives them, without reading the others |
| `spec_aliases(project_id) -> dict[str, str]` | Each `spec_id` alias mapped to the `spec_id` of the node holding it (what plan reads resolve merged ids with), without reading the nodes |
| `count_nodes(project_id, labels) -> int` | Number of spec nodes with any of `labels`, filtered as `nodes` does (spec labels only; others raise `ConfigurationError`) ([list_features](../mcp-server/tools.md#graph-read-used-by-list_features)) |
| `nodes_page(project_id, labels, *, limit=50, after=None) -> NodePage` | One page of the nodes `nodes` returns for `labels`, in `spec_id` order: those with `spec_id > after` (exclusive, string compare), at most `limit` (1 to 500, else `ConfigurationError` `invalid_limit`). Reads `limit + 1` rows to know whether more exist; never reads every node and slices ([list_features](../mcp-server/tools.md#graph-read-used-by-list_features)) |
| `scope(project_id, spec_id, labels=None) -> list[GraphNode]` | The nodes under a node: those that reach it by a declared spine path, or by a contiguous part of a `sequence` path, each edge in its direction (a symmetric relation either way), never passing through another spine-root node (for a module: its features, the capabilities that realise it, the interfaces it provides or consumes, the data entities stored in it, and the requirements under any of them); ordered by `spec_id` |
| `neighbours(project_id, spec_id, relations=None) -> list[GraphEdge]` | Domain edges of a node |
| `edges(project_id, *, include_inferred=True) -> list[GraphEdge]` | All domain edges |
| `find_nodes(project_id, text, labels=None, where=None, limit=20) -> list[NodeHit]` | Hybrid search: weighted rank fusion (vector 2, fulltext 1, k = 10) over a vector list and a fulltext list, each `limit` project hits deep, ties by `spec_id`; a hit only the vector list found is dropped below `vector_floor`; English stop words left out of the fulltext query. Vector: at most 2000 candidate vectors are scored by exact cosine; otherwise the index fetch doubles while hits < `limit`, then falls back to exact cosine; on a store whose one vector index holds every project (`capabilities.shared_vector_index`: Neo4j, ArcadeDB) the first fetch is 8 times as deep. Fulltext is paged in the index's score order. Only vectors made by the reader gateway's embedding model are compared; fulltext only when no gateway is given, no vector matches that model, or embedding fails with a non-fatal error (`LLMFatalError` propagates). A node the text names by explicit id or `spec_id` (aliases resolve) ranks first, in text order, `matched="id"`; filters and `limit` apply |
| `nodes_for_fragment(project_id, document_id, line_start, line_end) -> list[GraphNode]` | Nodes whose evidence lines (of the built revision) overlap the range; retired fragments are left out |
| `impact(project_id, spec_ids, depth=2, relations=None) -> ImpactResult` | Nodes reachable over domain edges in both directions, up to `depth` (1-3), nearest first, at most 500; the start nodes are not included, unknown ids are ignored |
| `find_fragments(project_id, text, document_id=None, limit=20) -> list[FragmentHit]` | Fulltext search over `active` fragment text (English stop words left out of the query); each hit has the fragment, its document and score |
| `documents(project_id)`, `fragments(project_id, document_id=None)` | Documents and fragments with their status |

Search text (`find_nodes`, `find_fragments`) is split into terms the way Lucene's standard analyser
does, at most 256 terms and 1000 characters. Operators such as `+`, `-`, `*`, `~` and `AND`/`OR`/`NOT`
are plain terms. Text the backend still rejects as too complex (`query_too_complex`) raises `ConfigurationError("query_too_long")`. The embedding
gets the same capped text.

`GraphNode`: `spec_id`, `uuid`, `label`, `name`, `spec_id_aliases`, attributes (a null attribute with `inherit` filled from the owner, [data-model.md](data-model.md)), `statements`, provenance (one
record per evidence fragment: document, path, fragment, lines, quote). `GraphEdge`: relation, source and
target `spec_id`, `basis`, `fact`, evidence fragments, and for inferred edges the rule and confidence.
`NodePage`: `items` (list of `GraphNode`), `next_after` (the `spec_id` of the last node of the page when
more exist, else `None`).
`NodeHit`: `node`, `score` (the fused score; 1 for an `id` hit), `matched` (`vector`, `fulltext`, `both` or `id`). Vector search
uses the search `embedding`. `ImpactResult`: `sources` (the resolved start `spec_id`s), `nodes`,
`distance` (hops per returned `spec_id`), `truncated`. Readers that analyse `INTEGRATES_WITH` treat the
two directions as one integration ([ontology.md](ontology.md)); `GraphEdge` keeps the stored direction.

## LLM gateway

`Gateway(LLMConfig)`: one per process; all model calls of all builds and submodules go through it.
`usage() -> UsageSnapshot` (sync); `aclose()` closes its client. Keywords `http_client`, `sleep`, `jitter`
are test hooks.

| Aspect | Contract |
|---|---|
| Calls | `structured(call_class, model, messages, output_type, *, prompt)` returns a validated object (`model` none: the call class's model; `prompt`: the prompt name for usage); `embed(texts)` returns vectors of the configured dimension; `warm_up(call_class, model, messages, output_type, *, prompt)` returns nothing |
| Prompt-cache warm-up | `warm_up` sends `messages` exactly as `structured` would (same model, untrusted notice and `output_type` schema) with at most 16 output tokens, and discards the answer: a cut, refused, empty or malformed answer is no error. Retries, HTTP errors and usage (under `prompt`) are as for `structured`. It fills a provider cache that reuses a prefix only up to where an earlier request ended, so later calls that start with `messages` and differ after them read them from the cache |
| Call classes | `extraction`, `small` (attributes, dedup adjudication, answered-elsewhere check), `judge`, `embedding`; each has a timeout and attempt count in `LLMConfig` |
| Limiter | One process-wide limit on concurrent calls (`LLMConfig.concurrency`), served in class priority order: `extraction`, `judge`, `small`, `embedding` |
| Retries | Rate limits, timeouts and server errors are retried with backoff within the class budget (exponential from 1 s, at most 30 s; a rate limit without `Retry-After` from 10 s, at most 60 s; a `Retry-After` is honoured, at most 120 s); authentication and unknown-model errors, and any other non-retryable 4xx except overflow and content filter (a configuration problem such as an unsupported schema), raise `LLMFatalError` at once |
| Output | JSON-schema structured output; a malformed answer gets one repair retry, then `LLMError` |
| Untrusted text | Document text is placed in marked blocks and the instructions say it is evidence, never instructions. Document-derived names shown outside those blocks (catalog and node names) have marker runs defused and are cut to 200 characters |
| Embedding batches | `embed` sends batches of at most 256 texts and 100k estimated tokens; a text over 8,000 estimated tokens is cut (counted, never logged); an input the endpoint still rejects as too long raises `LLMError(overflow)`, not `LLMFatalError` |
| Usage | Calls, failed calls, requests, input and output tokens, cached input tokens and duration are counted per call class and prompt name for the build report. Cached input tokens are the part of the input tokens the provider served from its prompt cache: `usage.prompt_tokens_details.cached_tokens`, else `cache_read_input_tokens` or `prompt_cache_hit_tokens`, else 0 |

The gateway holds the process's one `AsyncOpenAI` client, created with `base_url = LLMConfig.endpoint`,
the API key and `max_retries=0` (the gateway retries). It uses the Chat Completions API with JSON-schema
output, never the Responses API, so any OpenAI-compatible endpoint or proxy works.

The build's node and edge extraction calls are `structured` calls on the `extraction` class with the
build's own messages and answer schemas (prompts `nodes.hubs.14` for the hub pass, `nodes.statements.15`
for the statement pass, so usage shows each pass apart, and `edges.8`, one call per edge window; [build.md](build.md) steps 2
and 4); no extraction library sits between the build and the gateway. Before a unit's calls the
build may send a `warm_up` of their shared prefix (prompts `nodes.warmup.1`, `edges.warmup.4`).

## Judges

`JudgeRunner(gateway, ontology)`: `judge(prompt, payload, verdict_type) -> verdict | failure`. One
structured call per judgement on the `judge` class. Prompts are the ontology's judge prompts, versioned
files (`judge_prompts/<name>.md`, [ontology.md](ontology.md)); the prompt name is recorded on what a
verdict produces, and the judge memories key on `Ontology.judge_prompt_version(name)` (the name and a hash
of the prompt text), so editing a prompt drops its stored verdicts. Helpers (sync) `quotes_valid(verdict, fragments)` checks that every quote a
verdict cites occurs in the cited fragment, `meets_floor(verdict, floor)` that confidence meets a floor.
Conform's owner-conflict judge and the inference rules use it. Later submodules add their own prompts
and verdict types without changing the core.

## Build

| Name | Contract |
|---|---|
| `BuildContext` | `project_id`, `rebuild` flag, `BuildOptions`, `BuildSettings`, ontology, `driver` (the `GraphStore`; a Neo4j driver is deprecated), gateway, ports, lineages to retry, `build_id` (none: a new build; set: resume that build from its first unfinished step), `database` (for a driver only; none: the driver's default), `executor` (none: the store's executor; tests may pass one) |
| `run_build(context) -> BuildResult` | Runs every step of [build.md](build.md) and the analysis of [analysis.md](analysis.md), then at most 2 follow-up `update` builds for lineages left to retry (retryable codes only; none after a follow-up that recovered nothing), and returns the last build's result ([build.md](build.md) Follow-up builds and settlement); a follow-up that fails raises as any build |
| Step functions | `graph.build.steps.<name>` (module names would clash with direct exports): `plan`, `extract_nodes`, `resolve`, `extract_edges`, `conform`, `diff`, `persist`, `infer`, `verify`; each reads its inputs from and writes its output to the `BuildStore`, so a host can run and resume steps one by one. `plan(context)` starts a build when `build_id` is None and returns the build id (also `graph.build.plan`); run the other steps with `context.with_build_id(id)`. Steps never finish the build: after the last step call `run_build(context.with_build_id(id))`, which finds no open step, stores the `BuildResult` and starts any follow-up builds. A resumed `infer` reuses the judgements it wrote before it stopped ([analysis.md](analysis.md) Limits and backlog) |
| `estimate_build(context) -> BuildEstimate` | Cost estimate without model calls or writes, inference included ([build.md](build.md)) |
| `project_status(builds, project_id) -> ProjectBuildStatus` | `graph.build.status`, also exported from `graph.build`: whether the project is building, the running build's id, mode, step, start and follow-up attempt, and the latest finished build's `settlement`; reads the `BuildStore` only ([build.md](build.md) Building status) |
| `verify_project(context) -> VerificationReport` | Read-only checks: problems and metrics, never repairs or follow-ups ([analysis.md](analysis.md)) |

`BuildResult` holds the fields of [build.md](build.md) "Build result", `settlement` and `change_report` included. `BuildResult`, its change report and `VerificationReport` name
an edge as `<source spec_id>|<RELATION>|<target spec_id>`, never by edge key.

## Ports

Port methods are `async`.

| Port | Methods |
|---|---|
| `SpecIdRegistry` | `taken(project_id)`, `record(project_id, build_id, rows)`; rows are `spec_id`, identity key, uuid, state (`active`, `alias`, `retired`) |
| `CorpusSource` | `snapshot(project_id, *, dry_run=False)`: every active lineage's current revision (`lineage_id`, `revision_id`, `version`, `path`, `title`, `markdown`) in document order; a dry run never writes; raises `CorpusError` on a broken contract |
| `ExtractionStore` | `get(kind, key, fingerprint)`, `put(kind, key, fingerprint, payload)`; optionally batch `get_many(kind, keys, fingerprint) -> dict`, `put_many(kind, fingerprint, entries)`; kinds include `vectors` and `decisions` |
| `BuildStore` | `create_build`, `get_build`, `latest_build(project_id, *, finished_only=False, modes=None)`, `put_progress`, `finish_build(result)`, `get_result`; `put_artifact`, `get_artifact`. A follow-up build's `BuildRecord` holds `follow_up_of` (the chain's first build) and `follow_up_attempt` (1, 2; 0 for any other build) |

`open_file_stores(root, corpus_folder=None) -> FileStores` (sync) returns file implementations of every
port under a host-given directory, for tests and local runs; `FileStores.corpus` is a `FileCorpusSource`
over `corpus_folder`, None without a folder. Artifacts and results are gzip-compressed JSON; writes are atomic (write then
rename).
