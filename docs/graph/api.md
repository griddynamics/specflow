---
type: API
title: "Graph library API"
description: "Public library API: core setup, owned writes, read and search, LLM gateway, judges, build entry points, ports"
status: stable
tags: [graph]
---
# api.md: public library API: core setup, owned writes, read and search, LLM gateway, judges, build entry points, ports

All functions are `async` unless noted. Every graph function takes `project_id` explicitly. Other
exports (identity helpers, result and record models, `File*` port implementations): [tech-specs.md](implementation/tech-specs.md).

## Core setup

| Name | Contract |
|---|---|
| `load_ontology(path=None, *, submodule_files=None) -> Ontology` (sync) | Shipped ontology by default; `submodule_files` None merges the installed submodules' `ontology.yaml` files, a list replaces discovery; raises `OntologyError` ([ontology.md](ontology.md)) |
| `connect(Neo4jConfig) -> driver` (sync) | Creates the Neo4j driver; the host owns and closes it |
| `ensure_schema(driver, ontology, llm_config, database=None, *, ddl=None) -> tuple[str, ...]` | Creates missing constraints and indexes and returns the names it created; checks the Neo4j version; idempotent (a second run returns `()`). The objects come from the static DDL files (`ddl` None: the shipped `graph/core/ddl/*.cypher`; a sequence of statements replaces them), which must cover the ontology (else `ConfigurationError("schema_ddl_incomplete")`) |
| `check(driver, database=None)` | Raises `InfrastructureUnavailableError` when Neo4j is unreachable |

`database` None uses the driver's default database. Also exported: `require_version`, `server_version`,
`Executor` (project-scoped query runner).

## Owned writes

`OwnedWriter(driver_or_executor, ontology, owner, project_id, *, database=None)` with `owner` in `build`,
`gap`, `plan`. Every operation checks that the type belongs to the owner (else `OwnershipError`), stamps
`group_id`, and is idempotent. Operations may be grouped into one transaction with
`async with writer.transaction() as w:` (commits at the end, rolls back on any error).

| Operation | Contract |
|---|---|
| `upsert_node(label, key, properties)` | Create or update by key; sets only the given properties |
| `upsert_relationship(rel_type, source, target, key, properties)` | Create or update between existing nodes; the pair must be legal for declared types |
| `delete_relationship(rel_type, key) -> bool` | Absent is a no-op (False) |
| `delete_node(label, key) -> bool` | Deletes the node and its relationships (False when absent); a non-build owner may not delete a node that has relationships of other owners |
| `move_foreign_relationships(from_node, to_node) -> int` | Build only: moves every relationship build does not own (W3); returns the number moved |

## Read and search

`GraphReader(driver_or_executor, ontology, gateway=None, *, database=None, vector_floor=0.60)`. `vector_floor`: lowest vector
score `(1+cos)/2`, 0..1, for a vector-only hit; set for the embedding model (default tuned for
text-embedding-3-large). Returns spec nodes only (never documents, fragments or
foreign nodes) unless a method says otherwise. Aliases of `spec_id` resolve. Read results never carry
vectors. Invalid arguments raise `ConfigurationError`: `limit` outside 1-500 (`invalid_limit`), `impact`
depth outside 1-3 (`invalid_depth`), a line range not 1 ≤ `line_start` ≤ `line_end` (`invalid_line_range`),
`vector_floor` outside 0..1 (`invalid_vector_floor`).

| Method | Contract |
|---|---|
| `node(project_id, spec_id) -> GraphNode \| None` | One node |
| `nodes(project_id, labels=None, where=None) -> list[GraphNode]` | All spec nodes, ordered by `spec_id`; `where` maps ontology attribute names to required values (equality; enum values as declared; `None` means the property is absent) |
| `count_nodes(project_id, labels) -> int` | Number of spec nodes with any of `labels`, filtered as `nodes` does (spec labels only; others raise `ConfigurationError`) ([list_features](../mcp-server/tools.md#graph-read-used-by-list_features)) |
| `nodes_page(project_id, labels, *, limit=50, after=None) -> NodePage` | One page of the nodes `nodes` returns for `labels`, in `spec_id` order: those with `spec_id > after` (exclusive, string compare), at most `limit` (1 to 500, else `ConfigurationError` `invalid_limit`). Reads `limit + 1` rows to know whether more exist; never reads every node and slices ([list_features](../mcp-server/tools.md#graph-read-used-by-list_features)) |
| `scope(project_id, spec_id, labels=None) -> list[GraphNode]` | The nodes under a node: those that reach it by a declared spine path, or by a contiguous part of a `sequence` path, each edge in its direction (a symmetric relation either way), never passing through another spine-root node (for a module: its features, the capabilities that realise it, and the requirements under either); ordered by `spec_id` |
| `neighbours(project_id, spec_id, relations=None) -> list[GraphEdge]` | Domain edges of a node |
| `edges(project_id, include_inferred=True) -> list[GraphEdge]` | All domain edges |
| `find_nodes(project_id, text, labels=None, where=None, limit=20) -> list[NodeHit]` | Hybrid search: weighted rank fusion (vector 2, fulltext 1, k = 10) over a vector list and a fulltext list, each `limit` project hits deep, ties by `spec_id`; a hit only the vector list found is dropped below `vector_floor`; English stop words left out of the fulltext query. Vector: at most 2000 candidate vectors are scored by exact cosine; otherwise the index fetch doubles while hits < `limit`, then falls back to exact cosine. Fulltext is paged in the index's score order. Only vectors made by the reader gateway's embedding model are compared; fulltext only when no gateway is given, no vector matches that model, or embedding fails with a non-fatal error (`LLMFatalError` propagates). A node the text names by explicit id or `spec_id` (aliases resolve) ranks first, in text order, `matched="id"`; filters and `limit` apply |
| `nodes_for_fragment(project_id, document_id, line_start, line_end) -> list[GraphNode]` | Nodes whose evidence lines (of the built revision) overlap the range; retired fragments are left out |
| `impact(project_id, spec_ids, depth=2, relations=None) -> ImpactResult` | Nodes reachable over domain edges in both directions, up to `depth` (1-3), nearest first, at most 500; the start nodes are not included, unknown ids are ignored |
| `find_fragments(project_id, text, document_id=None, limit=20) -> list[FragmentHit]` | Fulltext search over `active` fragment text (English stop words left out of the query); each hit has the fragment, its document and score |
| `documents(project_id)`, `fragments(project_id, document_id=None)` | Documents and fragments with their status |

Search text (`find_nodes`, `find_fragments`) is split into terms the way Lucene's standard analyser
does, at most 256 terms and 1000 characters. Operators such as `+`, `-`, `*`, `~` and `AND`/`OR`/`NOT`
are plain terms. Text Lucene still rejects raises `ConfigurationError("query_too_long")`. The embedding
gets the same capped text.

`GraphNode`: `spec_id`, `label`, `name`, `spec_id_aliases`, attributes, `statements`, provenance (one
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
| Calls | `structured(call_class, model, messages, output_type, *, prompt)` returns a validated object (`model` none: the call class's model; `prompt`: the prompt name for usage); `embed(texts)` returns vectors of the configured dimension |
| Call classes | `extraction`, `small` (attributes, adjudication, hub catalog), `judge`, `embedding`; each has a timeout and attempt count in `LLMConfig` |
| Limiter | One process-wide limit on concurrent calls (`LLMConfig.concurrency`), served in class priority order: `extraction`, `judge`, `small`, `embedding` |
| Retries | Rate limits, timeouts and server errors are retried with backoff within the class budget; authentication and unknown-model errors, and any other non-retryable 4xx except overflow and content filter (a configuration problem such as an unsupported schema), raise `LLMFatalError` at once |
| Output | JSON-schema structured output; a malformed answer gets one repair retry, then `LLMError` |
| Untrusted text | Document text is placed in marked blocks and the instructions say it is evidence, never instructions. Document-derived names shown outside those blocks (catalog and node names) have marker runs defused and are cut to 200 characters |
| Embedding batches | `embed` sends batches of at most 256 texts and 100k estimated tokens; a text over 8,000 estimated tokens is cut (counted, never logged); an input the endpoint still rejects as too long raises `LLMError(overflow)`, not `LLMFatalError` |
| Usage | Calls, tokens and duration are counted per call class and prompt name for the build report |

The gateway holds the process's one `AsyncOpenAI` client, created with `base_url = LLMConfig.endpoint`,
the API key and `max_retries=0` (the gateway retries). It uses the Chat Completions API with JSON-schema
output, never the Responses API, so any OpenAI-compatible endpoint or proxy works.

Graphiti is given adapters, never its own OpenAI client (in the build's Graphiti adapter module):

| Graphiti client | Adapter |
|---|---|
| LLM client | Subclasses Graphiti's `OpenAIGenericClient` and overrides its request method, so every request goes through the gateway: limiter, retries, error classes, usage, untrusted-text marking |
| Embedder | Implements Graphiti's `EmbedderClient` on `Gateway.embed` |
| Graph driver, cross-encoder | Stubs that fail if called: the library calls only `extract_nodes` and `extract_edges`, and Graphiti never touches Neo4j |

## Judges

`JudgeRunner(gateway, ontology)`: `judge(prompt, payload, verdict_type) -> verdict | failure`. One
structured call per judgement on the `judge` class. Prompts are the ontology's judge prompts, versioned
files (`judge_prompts/<name>.<revision>.md`, [ontology.md](ontology.md)); the prompt version is recorded
on what a verdict produces. Helpers (sync) `quotes_valid(verdict, fragments)` checks that every quote a
verdict cites occurs in the cited fragment, `meets_floor(verdict, floor)` that confidence meets a floor.
Later submodules add their own prompts and verdict types without changing the core.

## Build

| Name | Contract |
|---|---|
| `BuildContext` | `project_id`, `rebuild` flag, `BuildOptions`, `BuildSettings`, ontology, driver, gateway, ports, lineages to retry, `build_id` (none: a new build; set: resume that build from its first unfinished step), `database` (none: the driver's default), `executor` (none: `Executor(driver, database)`; tests may pass one) |
| `run_build(context) -> BuildResult` | Runs every step of [build.md](build.md) and the analysis of [analysis.md](analysis.md) |
| Step functions | `graph.build.steps.<name>` (module names would clash with direct exports): `plan`, `extract_nodes`, `resolve`, `extract_edges`, `conform`, `diff`, `persist`, `infer`, `verify`; each reads its inputs from and writes its output to the `BuildStore`, so a host can run and resume steps one by one. `plan(context)` starts a build when `build_id` is None and returns the build id (also `graph.build.plan`); run the other steps with `context.with_build_id(id)`. Steps never finish the build: after the last step call `run_build(context.with_build_id(id))`, which finds no open step and stores the `BuildResult` |
| `estimate_build(context) -> BuildEstimate` | Cost estimate without model calls or writes ([build.md](build.md)) |
| `verify_project(context) -> VerificationReport` | Read-only checks: problems and metrics, never repairs or follow-ups ([analysis.md](analysis.md)) |

`BuildResult` holds the fields of [build.md](build.md) "Build result". `BuildResult` and `VerificationReport` name
an edge as `<source spec_id>|<RELATION>|<target spec_id>`, never by edge key.

## Ports

Port methods are `async`.

| Port | Methods |
|---|---|
| `SpecIdRegistry` | `taken(project_id)`, `record(project_id, build_id, rows)`; rows are `spec_id`, identity key, uuid, state (`active`, `alias`, `retired`) |
| `CorpusSource` | `snapshot(project_id, *, dry_run=False)`: every active lineage's current revision (`lineage_id`, `revision_id`, `version`, `path`, `title`, `markdown`) in document order; a dry run never writes; raises `CorpusError` on a broken contract |
| `ExtractionStore` | `get(kind, key, fingerprint)`, `put(kind, key, fingerprint, payload)`; optionally batch `get_many(kind, keys, fingerprint) -> dict`, `put_many(kind, fingerprint, entries)`; kinds include `vectors` and `decisions` |
| `BuildStore` | `create_build`, `get_build`, `latest_build(project_id, *, finished_only=False, modes=None)`, `put_progress`, `finish_build(result)`, `get_result`; `put_artifact`, `get_artifact` |

`open_file_stores(root, corpus_folder=None) -> FileStores` (sync) returns file implementations of every
port under a host-given directory, for tests and local runs; `FileStores.corpus` is a `FileCorpusSource`
over `corpus_folder`, None without a folder. Artifacts and results are gzip-compressed JSON; writes are atomic (write then
rename).
