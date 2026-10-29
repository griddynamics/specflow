---
type: Design
title: "Graph build"
description: "How graph.build turns a corpus into the graph and keeps it current"
status: stable
tags: [graph]
---
# build.md: how graph.build turns a corpus into the graph and keeps it current

## Modes

| Mode | When | What is extracted |
|---|---|---|
| `first` | The project has no persisted graph | Everything |
| `update` | Every later build (a delta build) | New, changed and retried fragments, and fragments whose stored result no longer matches the extractor fingerprint; stored results are reused for the rest |
| `rebuild` | Only when the host asks | Everything again; no stored extraction result and no persisted label is reused; only embedding vectors and merge decisions are, so identities stay stable. Matched nodes keep their `uuid` and `spec_id` |

The library picks `first` or `update` from the persisted graph; the host asks for a `rebuild` with a
flag in the build context. An `update` needs the record of the last `first` or `rebuild` build in the
`BuildStore`; when the project has a persisted graph but no such record, it raises `ConfigurationError`
and a `rebuild` is needed.

`BuildOptions` are frozen by the last `first` or `rebuild` build; an `update` build with different
options raises `ConfigurationError`. Changing options, adding or renaming ontology types or relations,
or changing the embedding model needs a `rebuild`; an `update` build in those cases raises
`ConfigurationError`.

## Steps

A build runs these steps in order. Each step stores its output in the `BuildStore` (artifacts) so a
host can run steps separately and resume after an interruption; `run_build` runs them all in one call
([api.md](api.md)).

| # | Step | Output |
|---|---|---|
| 1 | Plan | Change set, fragments, dirty fragments, units |
| 2 | Extract nodes | Raw nodes per fragment |
| 3 | Resolve | Canonical nodes with identity keys, names, labels, embeddings |
| 4 | Extract edges | Raw edges and attribute values per unit |
| 5 | Conform | Target graph: grounded nodes, legal edges, statements |
| 6 | Diff | Changes from the current graph to the target graph, with `spec_id`s and spec changes |
| 7 | Persist | The diff written to Neo4j |
| 8 | Infer | Inferred edges ([analysis.md](analysis.md)) |
| 9 | Verify | Verification run with repairs ([analysis.md](analysis.md)) |

### Plan

1. Read the corpus snapshot. Parser comment lines (page markers, machine annotations) are masked: they
   keep their line numbers but are never part of text, hashes, prompts or quotes.
2. Change set: compare the snapshot with the persisted `Document` nodes. A lineage without an `active`
   document is new; a different `revision_id` is a new revision; an active document missing from the snapshot is
   removed. The host may add lineages to retry: their `skipped` fragments, and their `active`
   fragments that were never extracted, are extracted again. A fragment with an unknown status is
   read as never extracted.
3. Segment each new or revised revision into fragments along its headings, within a token range
   (`fragment_tokens`). A long section is cut at points chosen from its content, so an edit changes
   only the fragment holding it and at most its neighbour. The fragment id is
   `<document_id>#<heading path slug>#<8 hex chars from the first body line>`, with `~2`, `~3` on
   collision; an unchanged section keeps its id across revisions.
4. Match the fragments of a revised document to its persisted fragments: same content is unchanged;
   same heading path with mostly the same lines is changed; the rest are new. Persisted fragments
   without a match, and every fragment of a removed document, become `retired`.
5. Dirty fragments are new, changed and retried ones. Fragments are grouped into units: a unit is
   consecutive fragments of one document, never of several. A document that fits `unit_tokens` is one
   unit; a longer one is cut into balanced parts at fragment boundaries, preferring top-level headings.
   Units and their windows are working data only; they are never stored in the graph.

### Extract nodes

For each unit with dirty fragments, Graphiti's `extract_nodes` is called with the ontology's extracted
types, their descriptions and the unit's fragments, one Graphiti episode per fragment, marked as
untrusted. An episode's text is the fragment text after a breadcrumb of the document title and heading
path; the breadcrumb is prompt-only. The prompt also lists known terms, so the same thing is named the
same way in every document. A known term is reused when the text refers to the same thing by the same
or a nearly identical name; when the text clearly uses a different name (a rename), the text's name is
used, and Diff carries the node over (same `spec_id`, new name, the old key kept as an identity alias).

Known terms are names of hub nodes: types whose ontology role is `anchor` or `group` (in the shipped
ontology: modules, features, capabilities). They come from:

| Build | Source |
|---|---|
| `first`, `rebuild` | The hub catalog: a small-model pass over an outline of the corpus (each document's title, heading paths and first fragment), in batches that fit the small model, returning hub names with their types. If it fails, the build continues without known terms. Turned off with `hub_catalog`. |
| `update` | The persisted hub nodes of the project |

Each unit gets at most `known_terms_limit` terms, chosen by relevance to the unit: names that occur in
its text first, then the closest by cosine between the name's vector and the unit's fragment vectors.
Fragments are embedded for this ranking only; their vectors are reused by content and never written
to the graph. Ties are broken by name, so the same inputs give the same terms. A hub also matches
by its `surface` identity aliases (names folded in by consolidation or a rename), but is shown by its
current name. Known terms are computed only when some unit is extracted.

A call extracts only from its window: a run of the unit's fragments, while the whole unit stays
visible as context. A unit too large for one call is split into several windows (at most
`max_node_windows`). In an `update` build the windows cover only the dirty fragments; the other
fragments reuse their stored nodes. Graphiti returns, for each node, the episodes it came from; that
attribution ties the node to its fragments, and a node without attribution, or attributed only outside
its window, is tied to every fragment of its window. A truncated answer halves its window and tries again.

### Resolve

1. Each raw node gets an identity key ([data-model.md](data-model.md)): its explicit id, else its
   normalised name; a requirement without an id gets a provisional key until Conform finds its
   defining line. The name of a hub node (role `anchor` or `group`) is normalised first: `-` and `_`
   read as spaces, a leading article (the, a, an) is dropped, and plurals fold to the singular (light
   rules: acronyms and words of up to 3 letters stay as written).
2. Exact merges: raw nodes with the same key in the same identity scope merge; so do nodes with the
   same explicit id and hub nodes with the same normalised name. Then hub nodes of the same
   normalised name under different hub labels become one node; its label is the one most raw
   extractions chose (ties: `anchor` role over `group`, then ontology order).
3. Hub consolidation replaces pairwise similarity merging. All hub nodes are ordered by a
   nearest-neighbour chain over their `name_embedding` and cut into blocks of at most 400 nodes
   (neighbouring blocks overlap by 20). The judge model receives each block (id, label, name, up to 2
   evidence lines, all inside untrusted blocks) and returns groups of nodes that are the same part of
   the system or the same grouping, with the label the merged node keeps (any hub label). Vetoes:
   nodes with different explicit ids never merge, and a group beyond 12 nodes is refused.
   - Decisions, merged and not merged, for pairs with `name_embedding` cosine at least 0.85 are
     stored in the `ExtractionStore` (kind `decisions`, keyed by project and the two identity keys).
     A stored decision overrides the model in later builds, so hub identities stay stable. A block
     whose pairs are all decided and that holds no new node is not sent.
4. Resolve never merges requirements by similarity, with or without an explicit id: wording that looks
   alike may state different commitments ("within 5 s", "within 10 s"). Conform deduplicates
   near-identical restatements of those without an id under guards (Conform 2).
5. Each canonical node gets a name (its clearest member's surface), a label (explicit id family first,
   then the extracted label; existing nodes keep theirs, except in a `rebuild`) and a `name_embedding`.

### Extract edges

For each unit, Graphiti's `extract_edges` is called with the unit text and a catalog of the nodes the
unit may link, drawn from the whole project after Resolve:

1. every node grounded in the unit;
2. hub nodes from elsewhere, at most `catalog_limit`: those mentioned in the unit first, then for each
   fragment the 20 closest by cosine between the hub's `name_embedding` and the fragment vector, ties
   broken by `spec_id` or identity key. A hub joins by nearness only when its name vector is close
   enough to a fragment of the unit (a similarity floor; values in tech-specs 5.4), so unrelated hubs
   never enter. In an `update` build, a unit with no changed fragment keeps the hubs it took by
   nearness last time (while they exist) and takes new hubs only when its text names them; nearness
   for new hubs applies to changed units and to `first` and `rebuild` builds.

So a requirement can link to a module, capability or feature that another document defines, and a
unit's catalog changes only when something relevant to it changes. A unit whose content and catalog
are unchanged reuses its stored edges. A small-model pass fills the
attributes of new and changed nodes in batches. A hub node is shown, and its stored attributes keyed
by, only the fragments that name it (as `MENTIONED_IN` counts), or all of them if none does.

### Conform

1. Snap every node's quote and every edge's fact to the source lines that hold them, searching first
   in the fragments the node was attributed to; these become the evidence lines (`REFERENCED_BY`,
   `source_fragment_ids`). A node whose quote matches no line is still grounded in its attributed
   fragments, with each fragment's full line range, and keeps no `quote`. A hub node is grounded in
   every fragment it was attributed to: on the lines where its quote snaps, else the first line naming
   it, else the whole fragment. An edge whose fact matches no line keeps the fragments Graphiti
   attributed it to as evidence and no `fact`. A requirement without an explicit id gets its final
   identity key here from its span value: the sentence(s) of its defining lines that contain its
   located quote ([data-model.md](data-model.md)). Nodes with equal final keys merge; on a span key only
   nodes of the same label with alike names merge, and the others get name-qualified keys.
2. Deduplicate: merge near-identical restatements of requirements without an explicit id, of the same
   label and identity scope, across documents.
   - Candidates: each node's 10 nearest by `name_embedding` cosine, at least 0.85.
   - Guards refuse a merge when: both sides state a number with a unit of the same kind and the values
     differ; a negation is present on one side only; one side says must or shall and the other should
     or may; they reference different explicit ids; the cluster would grow beyond 12 nodes.
   - A pair at cosine ≥ 0.95 and name-token overlap ≥ 0.7 merges directly; any other candidate merges
     only when the small model judges it the same commitment, possibly with more detail, and not
     conflicting.
   - The lowest identity key survives; the other keys become its identity aliases; evidence and
     statements are joined.
3. Add `implied` edges from `ref` attributes the ontology `derive`s, and from explicit ids named in a
   node's quote or `stmt` for relations with `id_references`.
4. Legalise each edge: keep a legal relation; flip a reversed one; use the pair's default relation;
   honour `cue_required`; drop the rest and count them. A `symmetric` edge is put in canonical order.
   Edges with the same edge key then merge: their evidence fragments are joined and the strongest
   basis wins (`stated`, then `implied`).
5. Keep a cross-unit edge only when both endpoints are grounded (`locality: grounded`), or only edges
   inside one unit (`locality: strict`).
6. Build statements; a hub node's come only from evidence fragments that name it (as `MENTIONED_IN`
   counts), its quote lines there, else the first line naming it. Set `MENTIONED_IN` where a hub
   node's name occurs in a fragment it is not grounded in (counted on word tokens, longest name
   first), and count `exclusive` pairs with more than one target. Compute each node's search `embedding` from its label, name and `stmt`.
7. Check the target graph against the invariants; a violation raises `ConformanceError`.

### Diff

Match target nodes to current nodes by identity key and identity aliases:

| Case | Result |
|---|---|
| One target, one current node | Update in place |
| Several current nodes, one target | Merge: the oldest node survives; the others' `spec_id`s become aliases; their foreign relationships move to it |
| One current node, several targets | Split: the node continues as its best match; the others are new |
| Current node without a target, new target in the same scope with a matching name and overlapping evidence lines | Carry-over: the new target takes the node's `uuid`, `spec_id` and relationships; the node's name updates to the target's, the old key stays as an identity alias |
| Current node without a target | Delete |
| Target without a current node | Create, with a new `spec_id` |

Edges match by edge key. Inferred edges are outside the diff: they change only when inference replaces
them, when an endpoint is deleted, or when extraction now states the same edge (it becomes `stated` in
place). The diff and the build's spec changes (`added`, `removed`, `modified`,
`rekeyed`, `carried_over`) are stored before anything is written.

A continuing node keeps its persisted label; its vectors are computed from its texts under that label
(embedded by Persist when they differ from the target's), and a created node that Conform relabeled
carries `label_disagreement`, so the next build sees no change.

### Persist

The diff is applied in batches, each its own idempotent transaction, in this order: removed edges,
merges, updates and carry-overs, node deletes, creates, edges and structural relationships, then
documents and fragments. A cursor records the last applied batch, so an interrupted persist resumes
there. While persist runs, readers may see a partly applied diff.

## Reuse in update builds

| Result | Reused when | Stored in |
|---|---|---|
| Nodes of a fragment (by Graphiti's attribution) | Same lineage, same fragment content, same heading path (the breadcrumb the prompt shows), same extractor fingerprint. A raw node on several fragments is reused once, on its reused fragments | `ExtractionStore` |
| Edges of a unit | Same unit content, same selected catalog, same extractor fingerprint. A unit with no changed fragment keeps its earlier nearness hubs, so a new unrelated hub does not change its catalog | `ExtractionStore` |
| Attributes of a node | Same identity key and label, same evidence fragment contents (for a hub, those naming it), same extractor fingerprint; a node supported only by the last extracted version of a skipped fragment is never asked again (its stored values are kept) | `ExtractionStore` |
| Embeddings (node and fragment vectors) | Same text and embedding model | `ExtractionStore`, once per distinct text: kind `vectors`, key `<project_id>\|<embedding model>\|<sha256 of the text>` |
| Hub consolidation decisions (merged and not merged pairs) | Same project and the same two identity keys | `ExtractionStore`: kind `decisions`, key `<project_id>\|<identity key>\|<identity key>` |

Every reuse key includes the project id. Build artifacts hold texts or hashes, never vectors.

The extractor fingerprint covers what shapes an extraction call: the ontology parts that reach the
prompt, the build options, the prompt version and the model. A `rebuild` extracts everything again and reuses no stored extraction result and no persisted label;
it reuses only embedding vectors and merge decisions (hub consolidation decisions, dedup verdicts), so
identities stay stable. An update
build's graph may differ slightly from a rebuild of the same corpus; that is accepted.

## Failures

| Failure | Effect |
|---|---|
| An extraction call fails after its retries (malformed output, refusal, overflow) | Its dirty fragments become `skipped` with an error code; a changed fragment keeps its last extracted version as support; the build ends `partially_succeeded` |
| The LLM endpoint or Neo4j is unreachable, or the endpoint rejects the credentials, the model or the request ([architecture.md](architecture.md) "Errors") | The step raises `InfrastructureUnavailableError` or `LLMFatalError`; nothing is marked skipped; the build is `failed` |
| A write fails | `GraphWriteError`; persist resumes from its cursor |
| A judge call fails, a verdict is invalid, or a non-fatal model error stops an inference rule | Reported in the inference report; the build still succeeds. Any other inference error fails the build, and the previous inferred edges stay ([analysis.md](analysis.md)) |

Skipped fragments are extracted again when their content changes or the host asks for a retry.

## Build result

| Field | Meaning |
|---|---|
| `status` | `failed` when a step raised; `partially_succeeded` when fragments were skipped; else `succeeded`. Verification does not change it. |
| `settled` | Persist completed and verification requested no follow-up build |
| `spec_changes` | `added`, `removed`, `modified`, `rekeyed`, `carried_over` spec_ids; `inferred_added` and `inferred_removed` edges as `<source spec_id>\|<RELATION>\|<target spec_id>`, never as edge keys (NFR-SEC-01). Nodes and inferred edges deleted by verification repairs are listed as removed |
| `verification` | `ok`, problems found and left per check, repairs, repair rounds applied and blocked by the valve, follow-up lineages and the verification metrics ([analysis.md](analysis.md)) |
| `metrics` | Fragments by status, units extracted and reused, LLM calls and tokens per step, nodes and edges by type, dropped edges, `exclusive_conflicts`, `spine_unreachable`, duration per step |

Metrics are reported, never used as gates.

## Cost estimate

`estimate_build` runs the Plan step and the reuse lookups without any model call or write, and returns:
the mode, the change set, fragments by change and by status, dirty fragments, units to extract and
units reused, and the expected calls and input tokens of seven steps: hub catalog, node extraction, hub
consolidation, edge extraction, attributes, requirement dedup and embeddings.

| Aspect | Rule |
|---|---|
| Tokens | Prompts are rendered by the build's own code and counted at about 3.6 characters per token |
| Expected nodes | A fragment with a persisted version gives back the persisted nodes grounded in it; any other gives 20 nodes per 1,000 tokens (10% hubs) |
| Reuse | As the build reuses: stored vectors and merge decisions, and in an `update` build stored edges and attributes |
| Dedup | 0.06 judged pairs per new id-less requirement |
| Not estimated | Inference, verification, output tokens, truncation splits, catalog-driven edge calls |

The estimate is of a new build: a context with `build_id` set raises
`ConfigurationError("estimate_build_id_set")`.

## Build options

| Option | Default | Meaning |
|---|---|---|
| `fragment_tokens` | 300-3,000, target 2,000 | Fragment size range |
| `hub_catalog` | on | Run the hub catalog pass in `first` and `rebuild` builds |
| `known_terms_limit` | 120 | Known terms shown per unit |
| `catalog_limit` | 150 | Hub nodes from elsewhere in an edge catalog per unit |
| `locality` | `grounded` | Cross-unit edge rule |
| `unit_tokens` | 24,000 | Largest unit; a longer document is cut into parts |
| `max_node_windows` | 8 | Node extraction calls per unit at most |
