---
type: Architecture
title: "Graph library architecture"
description: "Package layout, components, import rules, write ownership, ports, settings and errors"
status: stable
tags: [graph]
---
# architecture.md: package layout, components, import rules, write ownership, ports, settings and errors

## Package layout

```
graph/
  core/    shared by every submodule
  build/   builds and updates the graph
  gap/     reserved, empty
  plan/    reserved, empty
```

Each submodule exports its public names from its package root. `build`, `gap` and `plan` import
`graph.core` and never each other; `graph.core` imports none of them.

## Components

| Package | Component | Responsibility | Spec |
|---|---|---|---|
| core | Database | Neo4j driver factory, readiness check, project-scoped query executor, schema setup (constraints, indexes) | [data-model.md](data-model.md) |
| core | Ontology | Load and validate an ontology file; the shipped ontology | [ontology.md](ontology.md) |
| core | Identity | `spec_id` and uuid rules, `SpecIdRegistry` port | [data-model.md](data-model.md) |
| core | Owned writes | `OwnedWriter`: idempotent, ownership-checked node and relationship writes | [api.md](api.md) |
| core | Read and search | `GraphReader` | [api.md](api.md) |
| core | LLM gateway | Chat, structured output and embedding calls with one shared limiter; judge runner | [api.md](api.md) |
| build | Corpus and changes | Read the corpus, segment it into fragments, work out what changed | [build.md](build.md) |
| build | Extraction | Node and edge extraction through Graphiti, resolution of duplicates | [build.md](build.md) |
| build | Conform, diff, persist | Ground and legalise the result, match it to the current graph, write the difference | [build.md](build.md) |
| build | Analysis | Light inference; verification and repair | [analysis.md](analysis.md) |
| build | Run | `run_build`, phase functions, build options, build result | [build.md](build.md), [api.md](api.md) |

## Import rules

| # | Rule |
|---|---|
| I1 | Allowed imports: the standard library, `graph` modules per the layout rule, the repository `logger` utility, `pydantic`, `pyyaml`, `numpy`, `neo4j`, `openai`, `graphiti_core`. Nothing else. |
| I2 | `openai` only in the core gateway; `graphiti_core` only in the build's extraction adapter. |
| I3 | Cypher only in query modules: one in the core, one per writing submodule. Every query runs through the core executor and binds `$project_id`, except schema setup. |
| I4 | No ontology label, relation, id prefix or vocabulary value as a literal in code. Allowed built-in names: `SpecNode`, `Document`, `Fragment`, `CONTAINS`, `REFERENCED_BY`, `MENTIONED_IN`, and the edge `basis` values of the data model (`stated`, `implied`, `inferred`), even where they equal an ontology vocabulary value. |
| I5 | No environment reads, no printing, no disk writes outside file port implementations. |

Structure tests check I1-I5; a renamed-ontology test checks I4 ([ontology.md](ontology.md)).

## Write ownership

Each node type and relation in the ontology has an `origin`, and the origin names its owner:

| Origin | Owner |
|---|---|
| `extracted` (spec nodes, domain edges) | `graph.build` |
| `analysis`, `human` | `graph.gap` (declared when gap is designed) |
| `planner` | `graph.plan` (declared when plan is designed) |

`graph.build` also owns the built-in names, which no ontology declares: labels `SpecNode`, `Document`,
`Fragment` and relationships `CONTAINS`, `REFERENCED_BY`, `MENTIONED_IN` ([data-model.md](data-model.md)).

| # | Rule |
|---|---|
| W1 | A submodule writes only the node and relationship types it owns, through an `OwnedWriter` or its own query module. `OwnedWriter` refuses other types with `OwnershipError`. |
| W2 | A write sets only its owner's properties; other owners' properties stay untouched. |
| W3 | When build merges two nodes, or hands a node's identity to a new node, every relationship build does not own moves to the surviving node, except that a relationship of another owner running between the two merged nodes is dropped (it would become a self-loop). |
| W4 | When build deletes a spec node that lost all support, its relationships go with it, and the node is listed as removed in the build's spec changes. |
| W5 | Everything written carries `group_id = project_id`. |

## Ports

State outside Neo4j goes through async ports the library defines. The library ships a file
implementation of each, rooted at a directory the host passes; a host may supply its own. One contract
test suite runs against every implementation. Methods: [api.md](api.md).

| Port | Package | Holds |
|---|---|---|
| `SpecIdRegistry` | core | Every `spec_id` issued in a project and its state (`active`, `alias`, `retired`) |
| `CorpusSource` | build | The corpus snapshot of a build |
| `ExtractionStore` | build | Extraction results reusable by later builds |
| `BuildStore` | build | Build records (mode, options, progress, status) and per-build artifacts |

## Settings

Immutable typed objects with defaults, built by the host; invalid values raise `ConfigurationError`.
Secrets use a redacting type and are never logged or stored.

| Object | Holds |
|---|---|
| `Neo4jConfig` (core) | URI, user, password, database |
| `LLMConfig` (core) | Endpoint, API key, main model, small model, judge model (default: the main model), embedding model and dimension, concurrency, timeouts |
| `BuildOptions` (build) | Per-project options that shape extraction; frozen by the last `first` or `rebuild` ([build.md](build.md)) |
| `BuildSettings` (build) | Inference on or off |

## Errors

Every expected failure is a `GraphError`. Error fields hold ids, counts and codes, never document text.

| Error | Raised when | Host action |
|---|---|---|
| `ConfigurationError` | Invalid settings or options; invalid read arguments (`invalid_limit`, `invalid_depth`, `invalid_line_range`, `query_too_long`, `invalid_vector_floor`); unsupported Neo4j; Neo4j rejects the credentials | Fix configuration |
| `OntologyError` (a `ConfigurationError`) | Invalid ontology; lists every problem | Fix the ontology |
| `CorpusError` | Corpus breaks its contract (missing lineage id, version or markdown) | Fix the corpus |
| `OwnershipError` | A write targets a type its owner does not own | Bug |
| `LLMError` | Model call failed: malformed or refused output, overflow (a chat or embedding input too long), truncation, exhausted retries | Handled inside a build (except `LLMFatalError`): a skipped fragment in extraction, a failed judgement in inference; raised elsewhere |
| `LLMFatalError` (an `LLMError`) | Authentication, unknown model or endpoint, or another non-retryable 4xx that is not overflow or a content filter (a configuration problem such as an unsupported schema) | Fix configuration |
| `InfrastructureUnavailableError` | Gateway, Neo4j or a port backend unreachable | Retry later |
| `GraphWriteError` | A Neo4j write failed | Retry; persist resumes where it stopped |
| `ConformanceError` | An internal consistency check failed | Bug |

## Logging and concurrency

The library logs through the repository's `logger` utility; event names start with `graph.`; logs carry
ids, counts and codes, never document or prompt text. The API is `async`, except the calls [api.md](api.md) marks sync. Builds of different projects
may run in one process and share the gateway limiter. The host runs at most one write operation per
project at a time; the library takes no lock.
