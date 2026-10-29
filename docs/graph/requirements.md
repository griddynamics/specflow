---
type: Requirements
title: "Graph library requirements"
description: "Requirement units of the graph library (compact form), functional and non-functional"
status: stable
tags: [graph]
---
# requirements.md: requirement units of the graph library (compact form), functional and non-functional

Each row is one testable requirement in EARS form with one acceptance criterion. Status of every unit:
Approved (the product team, 2026-10-22). Verification is by test unless the row says otherwise. Details live in the
linked file of each area.

## Core ([architecture.md](architecture.md), [data-model.md](data-model.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-CORE-01 | Every query the library runs shall bind and filter on `project_id`, except schema setup. | Given two projects with equal content, when project A is read or written, then no node or relationship of project B is returned or changed. |
| FR-CORE-02 | `ensure_schema` shall create every constraint and index of the data model and be idempotent. | Given an empty database, when it runs twice, then the second run changes nothing. |
| FR-CORE-03 | If Neo4j is older than 5.26, then `ensure_schema` shall raise `ConfigurationError`. | Given Neo4j 5.25, when it runs, then `ConfigurationError` is raised and nothing is created. |
| FR-CORE-04 | When an `OwnedWriter` is asked to write a type its owner does not own, it shall raise `OwnershipError` and write nothing. | Given a `gap` writer, when it upserts a `Module`, then `OwnershipError` is raised. |
| FR-CORE-05 | A write shall change only properties its owner owns. | Given a spec node with a foreign property, when build updates the node, then the foreign property is unchanged. |
| FR-CORE-06 | A `spec_id` shall never change and never be given to a different node of the project. | Given a node deleted in build 2, when build 3 creates a node with another identity key and the same name, then it gets a different `spec_id`. |
| FR-CORE-07 | The library shall never import a host package or read environment variables. | Inspection: the structure test passes. |

## Ontology ([ontology.md](ontology.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-ONT-01 | `load_ontology()` shall load and validate the shipped ontology. | When called without a path, then it returns an ontology with 9 extracted node types and 5 extracted relations. |
| FR-ONT-02 | If an ontology file has problems, then loading shall raise one `OntologyError` listing every problem. | Given a file with an unknown key and an unresolved pair, then the error lists both. |
| FR-ONT-03 | The library shall build a graph from any valid ontology without code changes. | Given the shipped ontology and a fully renamed copy, when one corpus is built with each, then the graphs map onto each other. |
| FR-ONT-04 | `load_ontology` shall merge the ontology files of the installed submodules into one validated ontology. | Given a test submodule file declaring a type owned by build, then `OntologyError` names it; given one with a relation from its type to `Fragment`, then it loads. |

## Read and search ([api.md](api.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-READ-01 | `node` shall resolve a `spec_id` or any of its aliases to the node. | Given A merged into B, when `node(A)` is called, then B is returned. |
| FR-READ-02 | `find_nodes` shall rank nodes by fused vector and fulltext results. | Given a node matched by both indexes and one matched by one, then the first ranks higher. |
| FR-READ-03 | Where no gateway is available, `find_nodes` shall search fulltext only. | Given a reader without a gateway, then results come from fulltext and say so. |
| FR-READ-04 | `nodes_for_fragment` shall return nodes whose evidence lines overlap the given range. | Given a node grounded on lines 10-12, when lines 12-20 are asked, then it is returned; lines 13-20 do not return it. |
| FR-READ-05 | `impact` shall return nodes reachable within `depth` (1-3) hops, nearest first, at most 500, and flag truncation. | Given 600 reachable nodes, then 500 are returned and `truncated` is true. |
| FR-READ-06 | Read methods shall never return documents, fragments or foreign nodes as spec nodes. | Given a foreign node linked to a spec node, then `nodes` does not include it. |
| FR-READ-07 | `scope` shall return the nodes that reach the given node by a declared spine path, or by a contiguous part of a sequence path, walking symmetric relations either way and never passing through another spine root. | Given a module with a feature holding 2 requirements and a capability it realises grouping 1 requirement, then `scope(module)` returns 5 nodes: the feature, the capability and the 3 requirements. |
| FR-READ-08 | `nodes` and `find_nodes` shall filter by attribute values, and an unknown attribute name shall raise `ConfigurationError`. | Given `where={"is_assumption": "yes"}`, then only assumptions are returned. |
| FR-READ-09 | `find_fragments` shall return `active` fragments whose text matches, from the fulltext index. | Given a retired fragment and an active one containing "SAML", then only the active one is returned. |

## LLM gateway and judges ([api.md](api.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-LLM-01 | Every model and embedding call shall go through the process's one gateway and its limiter. | Given concurrency 4, when 10 calls are made, then at most 4 run at once. |
| FR-LLM-02 | When a call hits a rate limit, timeout or server error, the gateway shall retry within the call class budget. | Given two 429 answers then success, then the call succeeds after 3 attempts. |
| FR-LLM-03 | If the endpoint rejects authentication or the model, then the gateway shall raise `LLMFatalError` without retrying. | Given a 401, then one request is sent and `LLMFatalError` is raised. |
| FR-LLM-04 | When an answer does not match the output schema, the gateway shall retry once with a repair note, then raise `LLMError`. | Given two malformed answers, then `LLMError` is raised after 2 requests. |
| FR-LLM-05 | Prompts shall place document text in marked blocks and state that it is evidence, not instructions. | Inspection of every prompt template. |
| FR-LLM-06 | A judge verdict shall be accepted only when its confidence meets the floor and every cited quote occurs in the cited fragment. | Given a verdict quoting text absent from its fragment, then it is rejected. |
| FR-LLM-07 | Graphiti's extraction calls shall go through the gateway, using Chat Completions with JSON-schema output. | Given concurrency 2 and 5 parallel `extract_nodes` calls, then at most 2 requests are in flight, every request is a Chat Completions request, and usage counts all 5. |

## Build ([build.md](build.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-BLD-01 | When a project has no persisted graph, the library shall choose mode `first` and extract every fragment. | Given an empty project and 3 documents, then all fragments are extracted and persisted. |
| FR-BLD-02 | The build shall derive the change set from the corpus snapshot and the persisted documents. | Given one new, one revised and one missing document, then the change set holds exactly those three. |
| FR-BLD-03 | In an `update` build, only new, changed and retried fragments shall be extracted. | Given one edited section in one of 10 documents, then node extraction runs only on a window over that section's fragments, and every other fragment keeps its stored nodes. |
| FR-BLD-04 | An unchanged section shall keep its fragment id across revisions. | Given a revision that inserts a paragraph in section 2, then sections 1 and 3 keep their ids. |
| FR-BLD-05 | When a revision drops a fragment or a document is removed, its fragments shall become `retired` and nodes supported only by them shall be deleted. | Given a requirement stated only in a removed section, then after the build the node is gone and listed as removed. |
| FR-BLD-06 | Unchanged nodes shall keep their `spec_id` across `update` and `rebuild` builds. | Given a rebuild of an unchanged corpus, then every `spec_id` is the same. |
| FR-BLD-07 | Raw nodes with the same identity key in one identity scope, and hub nodes whose names normalise alike, shall become one node. | Given `FR-ADM-007` extracted as a functional and as a quality requirement, then one node exists; given modules "Orders service" and "Orders Service", then one node exists. |
| FR-BLD-08 | Every persisted spec node and every `stated` or `implied` edge shall be grounded as invariant D4 requires. | Inspection of a built test graph: verification reports no ungrounded node or edge. |
| FR-BLD-09 | An edge whose relation is not legal for its pair shall be legalised or dropped and counted. | Given an extracted `Module PART_OF Requirement`, then it is flipped or dropped and `dropped_edges` counts drops. |
| FR-BLD-10 | When merging nodes, build shall move every relationship it does not own to the surviving node. | Given a foreign relationship on the losing node, then after the merge it is on the survivor. |
| FR-BLD-11 | If an extraction call fails after its retries, then its dirty fragments shall become `skipped` and the build shall end `partially_succeeded`. | Given a unit whose calls all return malformed output, then its fragments are skipped with an error code. |
| FR-BLD-12 | A changed fragment that is skipped shall keep its last extracted version as support. | Given an edit whose extraction fails, then the nodes of the old text remain. |
| FR-BLD-13 | When persist is interrupted, re-running it shall continue from its cursor without duplicating writes. | Given a failure after batch 3, when persist re-runs, then batches 4+ are applied and the graph equals an uninterrupted run. |
| FR-BLD-14 | An `update` build whose options differ from the frozen options shall raise `ConfigurationError`. | Given a changed `fragment_tokens`, then the update build refuses to start. |
| FR-BLD-15 | The build result shall report status, settled flag, spec changes, verification summary and metrics. | Given any finished build, then every field is present. |
| FR-BLD-16 | When the host asks for an estimate, the library shall report the units to extract and the expected calls and input tokens per estimated step without any model call or write. | Given 3 new documents, when the estimate runs, then the gateway records no call, the graph is unchanged, and calls and tokens are reported per step. |
| FR-BLD-17 | Hub nodes (roles `anchor`, `group`) that the documents use for one part of the system or one grouping shall be merged by a block-wise consolidation pass, never across different explicit ids. | Given hubs "Backend" and "Backend service" that the documents use for one part, then one node exists; given two hubs with different explicit ids, they never merge. |
| FR-BLD-18 | Resolve shall never merge requirements without an explicit id by similarity, and Conform shall merge them only when no deduplication guard refuses. | Given "Export completes within 5 s" and "Export completes within 10 s", then two nodes exist. |
| FR-BLD-19 | Edges with the same edge key shall merge, a symmetric edge being keyed in canonical order. | Given `RELATES_TO` extracted as A to B and as B to A, then one edge exists with the evidence of both. |
| FR-BLD-20 | When a requirement's quote or `stmt` names another requirement's explicit id, build shall add `RELATES_TO` with basis `implied`. | Given "Bulk export reuses FR-012", then the two requirements are related with basis `implied`. |
| FR-BLD-21 | Each unit's edge catalog shall include relevant hub nodes from the whole project, at most `catalog_limit`. | Given a feature defined in document A and its requirement stated in document B without naming it, then the requirement is `PART_OF` the feature. |
| FR-BLD-22 | An `update` build shall reuse a unit's stored edges when its content and selected catalog are unchanged. | Given a new module in document C unrelated to document B, then B's edges are reused and no edge extraction call runs for B. |
| FR-BLD-23 | In `first` and `rebuild` builds the hub catalog shall seed known terms; in `update` builds known terms shall come from the persisted hub nodes. | Given a first build of 3 documents naming "Orders service", then every node extraction prompt that names it lists it as a known term, and a failing hub catalog call does not fail the build. |
| FR-BLD-24 | When requirements without an explicit id, of the same label and identity scope, restate one commitment in near-identical words, build shall merge them into one node. | Given the same id-less requirement stated in two documents in slightly different words, then one node exists with a statement from each document. |
| FR-BLD-25 | A consolidation decision stored for a pair of hubs shall be reused by later builds. | Given a first build that merged hubs A and B, when an `update` build runs on a corpus where both still exist, then they stay one node and no consolidation call is made for a block whose pairs are all decided. |

## Inference ([analysis.md](analysis.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-INF-01 | Where inference is on, the build shall run each ontology inference rule once after persist. | Given 3 rules, then each is run once per build. |
| FR-INF-02 | Inference shall read only `stated` and `implied` edges when choosing subjects and candidates. | Given an orphan attached by inference earlier, then it is still a subject. |
| FR-INF-03 | Inference shall write an edge only for a verdict at or above the floor with valid quotes. | Given a verdict with confidence 0.6, then no edge is written. |
| FR-INF-04 | The new inferred set shall replace the old one in one transaction. | Given a crash during the swap, then the previous inferred edges are intact. |
| FR-INF-05 | If a judge call fails or a verdict is invalid, then the build shall still succeed; if inference fails for any other reason, then the build shall fail and keep the previous inferred edges. | Given a judge that always fails, then the build status is unaffected; given an endpoint that rejects the credentials during inference, then the build is `failed` and the inferred edges are unchanged. |

## Verification ([analysis.md](analysis.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-VER-01 | After inference, the build shall run every check and repair blocking problems, at most 2 rounds. | Given an illegal edge written by a fault, then it is deleted and the run is `ok`. |
| FR-VER-02 | If a repair round would delete more than max(1, 5%) of the spec nodes, or more than max(1, 5%) of the domain edges, then it shall not be applied. | Given a fault that makes half the nodes look ungrounded, then nothing is deleted and the run is not `ok`; given one ungrounded node without edges in a project of 10 nodes, then it is deleted. |
| FR-VER-03 | Verification shall never report or change foreign nodes, relationships or properties. | Given a foreign relationship on a spec node, then no check reports it. |
| FR-VER-04 | `verify_project` shall run the checks without changing the graph. | Given a graph with a problem, then the problem is reported and the graph is unchanged. |

## Non-functional

| ID | Requirement | Acceptance |
|---|---|---|
| NFR-SEC-01 | Logs, errors and reports shall never contain document text, prompt text or secrets. | Inspection and a log-capture test over a full build. |
| NFR-SEC-02 | Labels and relation names shall reach Cypher only after ontology validation; all values as parameters. | Inspection: the structure test passes. |
| NFR-PERF-01 | An `update` build after a one-section edit should make at most 10% of the LLM calls of the `first` build of the same corpus. | Analysis: measured on the test corpus and reported; not a release gate. |
| NFR-PORT-01 | The library shall run against Neo4j 5.26 Community and any OpenAI-compatible endpoint. | Integration test on Neo4j 5.26 Community. |
