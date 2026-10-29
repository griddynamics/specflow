---
type: Ontology
title: "Graph ontology"
description: "Ontology file format, loading and validation, and the shipped ontology"
status: stable
tags: [graph]
---
# ontology.md: ontology file format, loading and validation, and the shipped ontology

The ontology is a YAML file that tells the library which node types and relations exist, how to
extract them and who owns them. Library code knows the format, never the content: a new ontology needs
no code change.

## Format

Top-level keys: `format_version` (1), `name`, `version`, `owner` (`build`, `gap` or `plan`; see
Ontology files of submodules), `corpus` (`framing`: one line describing the
documents; `exclude`: things never to extract; both go into extraction prompts), `vocabularies`
(named value sets, as `value: description` or a plain list), `node_groups`, `node_types`, `edge_types`,
`spine`, `inference`.

### Node types

| Field | Meaning |
|---|---|
| `label` | Neo4j label |
| `origin` | Who writes it: `extracted` (build), `analysis` or `human` (gap), `planner` (plan) |
| `tier` | `context`, `spec`, `governance` or `plan`; readers use it to tell layers apart |
| `role` | Extracted types only. `anchor` (a part of the system), `group` (groups other nodes), `atomic` (one commitment). Sets identity and merge defaults; `anchor` and `group` types are hub nodes, used for known terms and edge catalogs ([build.md](build.md)). |
| `group` | Optional node group whose fields the type inherits; never a label |
| `id.format` | `spec_id` prefix and placeholder, for example `REQ::FR::{SLUG}` |
| `id_families` | Explicit-id families that name this type, for example `FR`, `UC`. An explicit id is the family letters, a separator, optional letter segments and digits; only an id leading a node's name is its own, others are references ([data-model.md](data-model.md)) |
| `identity` | `key` and `scope` (default `type`). `key: id` uses an explicit id from the text and falls back to `span` (the defining line) for atomic types and to `surface` (the normalised name) for others; `key: surface` always uses the name. `scope: group` makes all types of the group one identity scope. |
| `description`, `guidance` | Prompt text: definition, naming rule, good and bad examples |
| `attributes` | Values extracted from text: `text` (with `max_chars`), `enum` (values or a vocabulary, plus aliases) or `ref` (names another node; `store: false` means it only feeds a `derive` rule) |

### Relations

| Field | Meaning |
|---|---|
| `origin`, `tier`, `description` | As for node types |
| `pairs` | Legal `{from, to}` pairs; a group expands to its members. Flags: `default` (chosen when the extracted relation does not fit the pair), `cue_required` (only chosen when a cue matches the evidence), `exclusive` (a source should have one target per target label; extra targets are counted, not removed) |
| `symmetric` | The relation has no direction: A to B equals B to A. Every pair must also hold reversed; the edge is stored once, with its endpoints in canonical order (lower identity key first), and legalisation never flips it |
| `id_references` | When a source node's quote or `stmt` contains the explicit id of a node it may link to on a legal pair, Conform adds this relation with `basis = implied`, without a model call |
| `cues` | Regular expressions searched in evidence lines; used by `cue_required` pairs |
| `derive` | `<type or group>.<ref attribute>` entries: each resolved reference yields an edge with `basis = implied` |

### Spine and inference

`spine` names the `sources` that should reach a `root` (requirements → modules) and the `paths` that
count: `{via: [relations], min, max}` or `{sequence: [relations]}`. A source that reaches no root is
counted as `spine_unreachable`.

`inference` holds `cap` (subjects judged per rule per build), `floor` (lowest accepted judge confidence)
and `rules`. A rule has an `id`, a `template`, a judge `prompt`, the relation it `writes` and the
template's parameters. A judge prompt `<name>.<revision>` is the file `judge_prompts/<name>.<revision>.md`
next to the ontology file that references it. The three templates are code ([analysis.md](analysis.md)):

| Template | Parameters | Subject | Candidates |
|---|---|---|---|
| `endpoints` | `subject`, `via` (a list of relations), `mentions` (optional, true or false), `target`, `min_targets`, `max_targets` | A `subject` node with between `min_targets` and `max_targets` `target` nodes: those it reaches by one edge of a `via` relation (a symmetric one in either direction) and, with `mentions`, those mentioned (`MENTIONED_IN`) in its evidence fragments | Pairs of those targets not yet linked by the written relation in either direction |
| `realization` | `subject`, `member_relation`, `container_relation`, `container`, `max_hops` | A `subject` node | `container` nodes reached from its members (`member_relation`) by up to `max_hops` `container_relation` edges |
| `attach_orphan` | `subject`, `targets`, `max_candidates` | A `subject` node that reaches no spine root and has no target yet on an `exclusive` pair of the written relation | The `max_candidates` most similar `targets` of the project by exact cosine over `embedding` |

## Loading and validation

`load_ontology(path=None)` loads the shipped ontology, or another file, and returns a validated,
immutable `Ontology`. Any problem raises one `OntologyError` that lists every problem found. Checks:

1. Known keys only; required fields present; values of the right type.
2. Labels, relation names and attribute names match safe patterns, so they can be used in Cypher.
3. Every reference (group, pair endpoint, `ref` target, `derive`, spine, inference) resolves.
4. Each relation pair appears once; at most one `default` per pair; a relation with a `cue_required` pair has cues.
5. `id.format` prefixes are unique and no prefix is a prefix of another.
6. Each spine source reaches a root through some declared path.
7. Inference rules name an existing template, a shipped judge prompt, and a relation legal for the template's endpoints; each `via` relation is named once and is legal from the subject to a target, and `mentions` needs a hub target.
8. No declared name equals a built-in name (`SpecNode`, `Document`, `Fragment`, `CONTAINS`, `REFERENCED_BY`, `MENTIONED_IN`).
9. Every declared type and relation has an origin its file's `owner` owns ([architecture.md](architecture.md)).
10. A `symmetric` relation's pairs hold in both directions and none is `exclusive`.

## Ontology files of submodules

Each submodule ships its own ontology file as package data: `graph.core` ships the build file
([spec-ontology.yaml](spec-ontology.yaml), `owner: build`), and `graph.gap` and `graph.plan` will each
ship one with their own types and relations. `load_ontology` merges the shipped files of the installed
submodules into one `Ontology` and validates the union, so names stay unique across files and pairs may
cross owners (for example a gap relation from a finding to a `FunctionalRequirement`). Vocabularies and
node groups are shared by name across files.

A relation of another owner may also end at the built-in labels `Fragment` and `Document`, so that gap and
plan can cite evidence. Build still owns those nodes; a relation that ends there belongs to its declaring
owner, and when build retires a fragment such relations stay.

The parts of the ontology that reach extraction prompts form its fingerprint; when the fingerprint
changes, stored extraction results stop matching and are re-extracted ([build.md](build.md)). Adding or
renaming labels or relations needs a `rebuild`.

A renamed-ontology test builds one corpus with the shipped ontology and with a copy whose declared
labels, relations and id prefixes are all renamed; the two graphs must map onto each other.

## Shipped ontology

File: [spec-ontology.yaml](spec-ontology.yaml) is the **initial template**. It declares only what
`graph.build` owns. When development starts it is copied into `graph.core` as package data; from then
on the package file is the shipped ontology, and this template is not kept in step with it.

What the template fixes and what implementation may improve:

| Part | Status |
|---|---|
| Structure: labels, groups, roles, `origin`, `tier`, `id` formats, `id_families`, `identity`, attribute names, types and enum values, relations, `pairs` and their flags, `symmetric`, `id_references`, `derive`, `spine`, inference rules | Fixed by this spec. A change is a spec change, made here first. |
| Prompt text: `corpus.framing`, `corpus.exclude`, type and relation `description` and `guidance` (definitions, naming rules, GOOD and BAD examples), attribute `description`s, enum value descriptions and `aliases`, `cues` | A starting point. Developers and coding agents may rewrite it to improve extraction, and judge prompt files are written the same way. |

Rules for prompt-text changes:

1. Keep the shape: each type keeps a definition, a naming rule, and GOOD and BAD examples that name
   the type a BAD example belongs to; each relation keeps its direction rule and GOOD and BAD examples.
2. Keep examples generic. They come from a made-up shop domain, never from a real project's
   documents.
3. Show the effect: a change is accepted when a run over the test corpus is no worse on the build
   metrics (`spine_unreachable`, `dropped_edges`, `exclusive_conflicts`, `label_disagreement`,
   `fragments_without_nodes`) and on a reviewed sample of nodes and edges.
4. Raise the minor `version` (1.0 to 1.1). Prompt text is part of the fingerprint, so the next
   `update` build re-extracts what it shaped.

| Label | Role | Group | `spec_id` prefix | Id families |
|---|---|---|---|---|
| `Module` | anchor | - | `MOD::` | - |
| `Feature` | group | - | `FEAT::` | - |
| `Capability` | group | - | `CAP::` | - |
| `FunctionalRequirement` | atomic | requirement | `REQ::FR::` | FR, UC, US |
| `QualityRequirement` | atomic | requirement | `REQ::NFR::` | NFR, QR, PERF, SEC |
| `Constraint` | atomic | requirement | `REQ::CON::` | CON |
| `IntegrationRequirement` | atomic | requirement | `REQ::INT::` | INT, IR |
| `DataRequirement` | atomic | requirement | `REQ::DATA::` | DATA, DR |
| `BusinessRule` | atomic | requirement | `REQ::BR::` | BR |

The six requirement types share one identity scope, so the same requirement extracted under two
labels is still one node.

| Relation | Pairs | Notes |
|---|---|---|
| `PART_OF` | requirement → feature, requirement → module, feature → module, feature → feature, module → module | exclusive except integration requirement → module; module → module needs a containment cue; a requirement that only applies to a module is never `PART_OF` it (`RELATES_TO`) |
| `GROUPED_BY` | feature → capability, requirement → capability | grouping, not containment |
| `REALIZED_BY` | capability → module | a capability is usually realised by several modules |
| `INTEGRATES_WITH` | module → module | default for module links; stored directed from the module that calls to the module called. In extraction a two-way integration is two edges; inference writes one direction per pair. The direction is a hint: analysis reads A to B and B to A as one integration |
| `RELATES_TO` | requirement ↔ requirement, requirement ↔ module | symmetric; only links the text makes (an explicit reference, or two requirements stated as dependent), never mere similarity; `id_references` adds it when a requirement names another's explicit id. Requirement ↔ module: a requirement that applies to a module without being part of it, such as a system-wide or cross-module statement (deployment, security, operations) |

Spine: requirements reach a module by 1-3 `PART_OF` edges, by `GROUPED_BY` then `REALIZED_BY`, or by one `RELATES_TO` edge. A symmetric relation on a spine path counts in either direction, because it is stored in canonical order.

| Inference rule | Template | Writes |
|---|---|---|
| `INF-INT-01` | `endpoints`: an integration requirement with 2-4 modules by `PART_OF`, `RELATES_TO` or mentions (`via: [PART_OF, RELATES_TO]`, `mentions: true`) | `INTEGRATES_WITH` between those modules |
| `INF-CAP-01` | `realization`: a capability and the modules of its grouped features | `REALIZED_BY` |
| `INF-CONT-01` | `attach_orphan`: a requirement that reaches no module, 3 candidates | `PART_OF` |

Cap 50 subjects per rule per build; floor 0.7.
