---
type: DataModel
title: "Graph data model"
description: "Neo4j graph model, identity, provenance, constraints and invariants"
status: stable
tags: [graph]
---
# data-model.md: Neo4j graph model, identity, provenance, constraints and invariants

## Partition

All projects share one Neo4j database. Every node and relationship carries `group_id = project_id`,
and every query filters on it. `project_id` matches `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`.

## Spec node

Label `SpecNode` plus exactly one domain label from the ontology (for example `Module`,
`FunctionalRequirement`).

| Property | Meaning |
|---|---|
| `uuid` | Storage key; fixed at creation |
| `spec_id` | External id; fixed at creation, unique in the project, never reused |
| `spec_id_aliases` | `spec_id`s of nodes merged into this one |
| `name` | Natural-language name; display only, may change |
| `identity_key`, `identity_aliases` | Current identity key; earlier keys that still match this node |
| attributes | The node type's ontology attributes, such as `stmt`, `quote`, `acceptance_criteria` |
| `statements` | JSON list: what each supporting fragment says (see Statements) |
| `statement_text` | Statement texts joined by newlines; searchable |
| `label_disagreement` | Label resolution would give now, when it differs from the node's label |
| `embedding` | Search vector of `"<Label>: <name>. <stmt>"` (`"<Label>: <name>"` when the node has no `stmt`); used by search, analysis and orphan candidates |
| `name_embedding` | Vector of `"<Label>: <name>"`; used only to find duplicate nodes (hub consolidation in Resolve, requirement dedup in Conform) |
| `embedding_model` | Model of both vectors |
| `created_at`, `updated_at`, `created_build_id`, `last_build_id` | Write stamps |

## Document and Fragment

`:Document`: one per lineage. `document_id` (lineage id), `spec_id` (`DOC::<document_id>`),
`revision_id` and `version` of the built revision, `path`, `title`, `status` (`active` or `removed`),
`last_build_id`. A removed document stays as history; if its lineage returns it becomes `active` again.

`:Fragment`: one per piece of a revision. `fragment_id`, `document_id`, `spec_id` (`FRG::<fragment_id>`),
`text`, `heading_path`, `ordinal`, `line_start`, `line_end` (lines of the revision's markdown),
`content_hash`, `status`, `extracted_hash` (content hash of the last successfully extracted version),
`error_code` (last extraction failure), `last_build_id`.

| Fragment status | Meaning |
|---|---|
| `active` | Current content; supports nodes and edges |
| `skipped` | Extraction failed; a changed fragment keeps its last extracted version as support until a retry or the next change succeeds |
| `retired` | Gone from the corpus (revision dropped it, or its document was removed); history only, supports nothing |

## Edges

**Domain edges** are typed relationships between spec nodes, named by the ontology relation, on a pair
the ontology allows. At most one domain edge per edge key, so a symmetric relation has at most one edge per node pair.

| Property | Meaning |
|---|---|
| `uuid` | Storage key; fixed at creation |
| `edge_key` | `<source identity key>\|<RELATION>\|<target identity key>`; matches the edge across builds. For a `symmetric` relation the endpoint with the lower identity key comes first, and the edge is stored in that direction |
| `basis` | `stated` (extracted from text), `implied` (derived by a rule from an attribute), `inferred` (light inference); an edge without `basis` is read as `stated` |
| `source_fragment_ids` | Fragments holding the evidence; inferred edges carry it too (the fragments their evidence cites) |
| `fact` | The evidence line, verbatim, at most 300 characters |
| `inference_rule`, `confidence`, `evidence`, `judge_model`, `inferred_at` | Inferred edges only; `evidence` is a JSON list of `{fragment_id, quote}`; `inferred_at` is the write time |
| `created_at`, `updated_at`, `last_build_id` | Write stamps |

When extraction later finds an edge that already exists as inferred, the edge becomes `stated` in place
and keeps its `uuid`.

Build results, inference reports and verification problems and repairs name an edge as
`<source spec_id>|<RELATION>|<target spec_id>` in its stored direction, never by its edge key, whose
identity keys may hold document text (NFR-SEC-01).

**Structural relationships** (owned by build):

| Type | From → To | Properties |
|---|---|---|
| `CONTAINS` | Document → Fragment | - |
| `REFERENCED_BY` | SpecNode → Fragment | `line_start`, `line_end` of the node's evidence lines |
| `MENTIONED_IN` | SpecNode → Fragment | `count`; hub nodes only (roles `anchor`, `group`), where the node's name occurs but the node is not grounded there. Counted on word tokens, longest name first, so tokens matched by a longer name do not count for a shorter name inside it |

## Identity

| Value | Rule |
|---|---|
| Identity key | `<scope>\|<kind>\|<value>`. Kind `id`: the node's own explicit id, normalised. Kind `span`: the defining sentence of a requirement without an id (span value below). Kind `surface`: the normalised name (modules, features, capabilities). A hub name (role `anchor` or `group`) is normalised first: `-` and `_` as spaces, a leading article (the, a, an) dropped, plurals folded to the singular (light rules; acronyms and words of up to 3 letters kept). Scope is the type's group when the ontology says so (all requirement types share one scope), else the label. May change between builds. |
| Explicit id | Family letters as written, upper case (an ontology id family), a separator (`-`, `_` or one space), optional upper-case letter segments, then digits: `FR-ADM-007`, `NFR_12`, `BR 3`. A node's own id is one that leads its name (after list or emphasis markup); ids elsewhere in a name or quote are references and feed `id_references`. |
| Span value | The normalised sentence(s) of the node's defining lines that contain its located quote; no such sentence: the whole lines; no located quote: the provisional key stays. A value over 500 characters is bounded: its first 500 characters, `#`, then 12 hex chars of its sha256. Nodes with equal span keys merge only when they have the same label and alike names (name-token overlap ≥ 0.6 or `name_embedding` cosine ≥ 0.9); otherwise each node's value becomes `<sentence>\|<normalised name>`, then gets `\|<label>`, then `\|2`, `\|3` on further clashes. |
| `spec_id` | `<prefix from the ontology id format>::<SLUG>`; SLUG is the upper-case name, non-alphanumerics as `_`, stop words dropped, first 5 words, at most 80 characters; an explicit id leads the slug. A taken value gets `_2`, `_3`, and so on. Assigned once, never changed, never given to a node with a different identity key (a deleted node's `spec_id` returns only for the same key). `Document` and `Fragment` use `DOC::<document_id>` and `FRG::<fragment_id>`. |
| uuid | `uuid5(NAMESPACE, "<project_id>\|node\|<identity key at creation>")`; edges use `"<project_id>\|edge\|<edge_key at creation>"`. `NAMESPACE = 36012a50-eff4-4a22-8410-fad8c8a2474a`. Never changes. |
| Label | Chosen at creation: the type whose id families match the node's explicit id, else the extracted label. Labels never change afterwards; a different computed label is stored in `label_disagreement`. |

Merges keep the older node's `uuid` and `spec_id`; the other `spec_id`s become aliases. Readers key on
`spec_id` (aliases resolve), never on `name`. Extraction prompts never show `spec_id`, uuid,
`project_id` or lineage ids.

## Statements

`statements` holds one entry per supporting fragment (for a hub node, only those naming it by name or
alias, matched as for `MENTIONED_IN`; a hub named nowhere has none): `{fragment_id, document_id,
revision_id, version, text, at}`, where `text` is the node's evidence in that fragment (at most 300 characters). At most 50 entries; the first and the latest entry of each document
are always kept. Statements are facts: the library does not judge whether they agree.

## Provenance

Every spec node has a `REFERENCED_BY` relationship to each fragment holding its evidence. Every
`stated` or `implied` edge lists its evidence fragments. Every quoted text (`quote`, `fact`, statement
`text`) is verbatim source text.

## Constraints and indexes

Names start with `graph_`. Setup is idempotent and runs before the first build. The objects are
defined in static DDL files, `src/graph/core/ddl/*.cypher` (one `CREATE ... IF NOT EXISTS` statement per
object, the vector dimension as the parameter `$dimension`); setup runs the statements of the objects
the database lacks. The DDL must cover the ontology setup runs with (the objects below, an edge-key
index per relation and a constraint per foreign label), else setup stops with `schema_ddl_incomplete`
before anything is created.

| Object | On |
|---|---|
| Unique | `SpecNode (group_id, spec_id)`, `SpecNode (group_id, uuid)`, `Document (group_id, document_id)`, `Fragment (group_id, fragment_id)`; per foreign label `(group_id, uuid)`, named `graph_foreign_uuid_<label in lower case>` |
| Range index | `SpecNode (group_id, identity_key)`; per domain relation `(group_id, edge_key)` |
| Vector index | `SpecNode.embedding` and `SpecNode.name_embedding`, cosine, dimension from `LLMConfig` |
| Fulltext index | `SpecNode` `name`, `statement_text`; `Fragment` `text` |

A foreign label is a label a submodule ontology file of another owner than build declares (for
example a `gap` finding). A foreign node carries that label only (never `SpecNode`) and is keyed by
`uuid`; owned writes MERGE it on `(group_id, uuid)`, the key its constraint holds, as they MERGE a
`Document` on `document_id` and a `Fragment` on `fragment_id`. Setup creates the constraints of the
foreign labels of the ontology it runs with, so it must run with the submodule files loaded and with
DDL that holds their statements (`ensure_schema(..., ddl=...)`; the shipped ontology has no foreign
label).

Creating a uniqueness constraint fails when nodes that would break it already exist. A database whose
foreign nodes were written before their constraint existed may hold duplicates of one `(group_id,
uuid)`: setup then stops with `schema_setup_failed` naming the constraint's create query, as for any
create the server refuses (the objects created before it stay). Merge or delete the duplicates, then
run setup again.

Vector and fulltext searches span the whole database, so search filters index hits by `group_id`,
fetches more while too few remain, and cuts to the requested limit; a project with at most 2000
candidate vectors is scored by exact cosine instead ([api.md](api.md)).

## Invariants

| # | Invariant |
|---|---|
| D1 | Every spec node has `SpecNode` and exactly one domain label. |
| D2 | Every domain edge has a declared type on a legal pair. |
| D3 | `spec_id` and uuid unique per project; one spec node per (identity scope, identity key), ensured by the build. |
| D4 | Every spec node is grounded in at least one `active` or `skipped` fragment; every `stated` or `implied` edge has evidence fragments. |
| D5 | Build never creates or changes nodes, relationships or properties it does not own, except as W3 and W4 allow ([architecture.md](architecture.md)). |

Verification checks D1-D4 after every build ([analysis.md](analysis.md)).
