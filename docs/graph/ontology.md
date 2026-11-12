---
type: Ontology
title: "Graph ontology"
description: "Ontology file format, loading and validation, and the shipped ontology"
status: stable
tags: [graph]
---
# ontology.md: ontology file format, loading and validation, and the shipped ontology

The ontology is a YAML file that tells the library which node types and relations exist, how to
extract them, who owns them and which rules the graph keeps. Library code knows the format, never the
content: a new ontology needs no code change. Every rule a flag can express is a flag in the file;
code interprets flags only ([architecture.md](architecture.md) I4). The few rules no flag can carry
live in prompt text (Rules carried by prompt text).

## Format

Top-level keys: `format_version` (2), `name`, `version`, `owner` (`build`, `gap` or `plan`; see
Ontology files of submodules), `corpus` (`framing`: one line describing the
documents; `exclude`: things never to extract; both go into extraction prompts), `vocabularies`
(named value sets, as `value: description` or a plain list), `node_groups`, `node_types`, `edge_types`,
`spine`, `conform`, `inference`. A file of another format version is rejected.

### Node types

| Field | Meaning |
|---|---|
| `label` | Node label (a type name in ArcadeDB, `kind` and `labels` in PostgreSQL: [data-model.md](data-model.md#per-store-mapping)) |
| `origin` | Who writes it: `extracted` (build), `analysis`, `human` or `agent` (gap), `planner` (plan) |
| `tier` | `context`, `spec`, `governance` or `plan`; readers use it to tell layers apart |
| `role` | Required on extracted types, not allowed on others. `anchor` (a part of the system), `group` (groups other nodes), `atomic` (one commitment). Sets identity and merge defaults; `anchor` and `group` types are hub nodes, used for known terms and edge catalogs ([build.md](build.md)). |
| `group` | Optional node group whose fields the type inherits (`origin`, `tier`, `role`, `identity`, `description`, `guidance`, `attributes`; the type's own value wins); never a label. `origin` and `tier` are required on the type or its group |
| `id.format` | `spec_id` prefix and placeholder, for example `REQ::FR::{SLUG}`; required on extracted types |
| `id_families` | Explicit-id families that name this type, for example `FR`, `UC`. An explicit id is the family letters, a separator, optional letter segments and digits; only an id leading a node's name is its own, others are references ([data-model.md](data-model.md)) |
| `identity` | `key` (default `id` for atomic types, `surface` for others) and `scope` (default `type`). `key: id` uses an explicit id from the text and falls back to `span` (the defining line) for atomic types and to `surface` (the normalised name) for others; `key: surface` always uses the name. `scope: group` makes all types of the group one identity scope. |
| `merge_across_labels` | Default true. False: a node of this type is never merged with a node of another label, by the consolidation judge or by a stored decision |
| `supported_by` | List of `{relation, direction: in or out}`. After normalisation a node of this type is dropped (counted) unless it has at least one such edge |
| `description`, `guidance` | Prompt text: definition, naming rule, good and bad examples |
| `attributes` | Values extracted from text: `text` (with `max_chars`), `enum` (`values`: a list, a `value: description` mapping or a vocabulary name, plus `aliases`) or `ref` (`target`: the type or group key it names; `store: false` means it only feeds a `derive` rule). Each kind takes only its own fields: `default` only on `enum`, `inherit` on `text` and `enum`. Optional `default`: a declared value Conform stores when the value is null, so read filters work. Optional `inherit: {via: <relation>}`: when the value is null, readers fill it from the node's owner one `via` hop away; never stored |

### Relations

| Field | Meaning |
|---|---|
| `origin`, `tier`, `description` | As for node types |
| `pairs` | Legal `{from, to}` pairs with their flags (table below); a group expands to its members |
| `symmetric` | The relation has no direction: A to B equals B to A. Every pair must also hold reversed; the edge is stored once, with its endpoints in canonical order (lower identity key first), and legalisation never flips it |
| `acyclic` | The relation's edges form no directed cycle (invariant D7) |
| `stated_only` | Only `stated` edges: no `derive`, `derive_via`, pair `default`, `id_references`, inference rule writing it, or `otherwise` and fallback conversion into it. Shown in the edge prompt. One exception: a `cross_document` rule may write it as `also_writes` when the relation declares `cues` (check 12) |
| `id_references` | When a source node's quote or `stmt` contains the explicit id of a node it may link to on a legal pair, Conform adds this relation with `basis = implied`, without a model call, only between nodes with different owners |
| `cues` | Regular expressions, matched ignoring case; used by `cue_required` pairs, and on a `stated_only` relation by `cross_document` rules: such an edge is written only when its source-side quote matches a cue. Legalisation of a `cue_required` pair searches each cue anywhere in an evidence line, as a substring (v2.0 behaviour, unchanged); the `cross_document` gate (`Ontology.cue_matches`) matches a cue only as whole words, ignoring case, so `requires` does not match `requirement` but matches `Requires`. A cue must also compile inside that whole-word pattern, so it takes no inline flag such as `(?i)` |
| `derive` | `<type or group>.<ref attribute>` entries: each resolved reference yields an edge with `basis = implied` |
| `derive_via` | `{consumer: <relation>, provider: <relation>}`: an implied edge from each module a hub is `consumer` of to the module it is `provider` of. It merges with a stated edge of the same key; stated wins |
| `impact` | Optional role, one of `uses`, `used_by`, `coupled`, `related`, `contains`, `none`: how an edge of the relation carries a change. It is the default of the relation's pairs; a relation without it is `none`. Read-only: `GraphReader.trace` reads it, extraction, merging and fingerprints never do ([trace.md](trace.md) "Impact roles in the ontology") |

A node's **owner** is its target on an `owner` pair. The owner pairs of a source are all its pairs
flagged `owner`, of any relation; together they allow at most one target (invariant D6).

| Pair flag | Meaning |
|---|---|
| `default` | Chosen when the extracted relation does not fit the pair. Several defaults of one label pair are allowed when their `where` conditions are disjoint; the ends' attributes choose between them |
| `default_from` | A list of relations, on a `default` pair only: the pair is the default only for an edge extracted as one of them; any other relation that does not fit stays illegal. Without it a default pair takes any relation. Normalisation's owner-conflict default never uses such a pair |
| `cue_required` | Only chosen when a cue matches the evidence |
| `owner` | An ownership pair: over all owner pairs of a source, at most one target |
| `owner_rank` | Default 1. In an owner conflict the target on the lower rank wins first |
| `exclusive` | At most one target of this pair per source; extra targets are resolved by normalisation (D6) |
| `source_where`, `target_where` | `{attribute: [values]}`: the pair holds only when the end's value (or its `default` when null) is listed; a null value without a default holds |
| `where_reason` | Quarantine reason when a `where` fails and no `otherwise` applies; default `condition_failed` ([build.md](build.md) Quarantine) |
| `otherwise` | When only a `where` fails: the edge becomes this relation if it is legal for the ends, else it is quarantined with `where_reason` |
| `max_depth` | Longest containment chain along the relation; deeper edges are quarantined (`depth_cut`) |
| `not_across` | Attributes the ends must share; an edge whose ends differ is quarantined with `where_reason` |
| `distinct_owner` | The ends must have different owners; an edge between nodes of one owner is dropped as redundant (normalising, never a hard check) |
| `redundant_via_owner` | Dropped as redundant when the source's owner has the same relation to the same target |
| `impact` | Optional role overriding the relation's `impact` for this pair (same six values). A pair without it takes the relation's role. `Ontology.impact_role(relation, source, target)` answers the pair's role, else the relation's for a pair the file does not declare |

### Spine, conform and inference

`spine` names the `sources` that should reach a `root` (requirements → modules) and the `paths` that
count: `{via: [relations], min, max}` (`min` default 1, `max` default `min`) or `{sequence: [relations]}`
(no `min` or `max`). Optional `exclude:
{attribute: [values]}`: a source is skipped by spine checks and metrics when it or its owner matches.
A source that reaches no root is counted as `spine_unreachable`.

`conform` holds the owner-conflict judge of normalisation: `judge_prompt`, `cap` (fresh judge calls per
build), `floor` (lowest accepted confidence) and `fallback_relation`, the relation that conflict losers,
broken cycle edges and hints become when it is legal for their ends ([build.md](build.md) Conform).
Optional `answered_elsewhere` turns on the answered-elsewhere check ([build.md](build.md) Answered
elsewhere): `label` (the type checked, an extracted atomic type, by its type key or its label), `prompt` (judge prompt), `candidates`
(fragments shown per node), `min_cosine` (lowest node-to-fragment cosine of a candidate), `floor`
(lowest confidence that drops a node) and `cap` (fresh calls per build).

`inference` holds `cap` (fresh subjects judged per rule per build, in every mode), `first_cap` (fresh
subjects per rule at most in a `first` or `rebuild` build), `first_budget` (estimated input tokens of a
`first` or `rebuild` build's inference calls, beyond which no rule goes past its `cap`; [analysis.md](analysis.md)
Limits and backlog), `floor` (lowest accepted judge confidence)
and `rules`, run in file order. A rule has an `id`, a `template`, a judge `prompt`, the relation it
`writes`, an optional `cap` and `first_cap` (override `inference.cap` and `inference.first_cap` for this rule), an optional `floor` (overrides `inference.floor` for this rule: the lowest confidence of an accepted verdict; for `cross_document` it applies to link items only, owner items keep `inference.floor`), an optional `reads` (ids of earlier rules whose inferred edges of this build the rule sees; check 16) and the template's parameters. A
`cross_document` rule may also name `also_writes`, further relations it writes. Parameters that name
node types (`subject`, one key or, for `cross_document`, a list; `members`, `target`, `targets`,
`container`) take type or group keys, never labels; only `conform.answered_elsewhere.label` also
accepts a label. A judge prompt
`<name>` (snake_case, no version) is the file `judge_prompts/<name>.md` next to the ontology file that
references it. Stored judgements are keyed under `<name>@<first 12 hex of the text's SHA-256>`
(`Ontology.judge_prompt_version`), so editing a prompt file expires them without renaming it. The four templates are code ([analysis.md](analysis.md)):

| Template | Parameters | Subject | Candidates |
|---|---|---|---|
| `endpoints` | `subject`, `via` (a list of relations), `mentions` (optional, true or false), `target`, `min_targets`, `max_targets`, `exclude_owned_by` (optional, a list of type or group keys) | A `subject` node with between `min_targets` and `max_targets` `target` nodes: those it reaches by one edge of a `via` relation (a symmetric one in either direction) and, with `mentions`, those mentioned (`MENTIONED_IN`) in its evidence fragments; a subject whose owner has a label in `exclude_owned_by` is skipped | Pairs of those targets not yet linked by the written relation in either direction |
| `realization` | `subject`, `member_relation`, `container_paths` (spine-style paths), `container` | A `subject` node | `container` nodes reached from its members (`member_relation`) by one of the `container_paths`; a `via` path's relations are all directed or all symmetric |
| `attach_orphan` | `subject`, `targets`, `max_candidates`, `exclude_where` (optional, `{attribute: [values]}`), `target_exclude_where` (optional, `{attribute: [values]}` over the targets), `batch` (optional: subjects judged together per call, [analysis.md](analysis.md)), `batch_prompt` (optional: the judge prompt of batched calls; required with `batch`) | A `subject` node with no target on any of its owner pairs, unless it or its owner matches `exclude_where` | The `max_candidates` most similar nodes of the project by exact cosine over `embedding`, among the `targets` labels that are legal owner targets of the subject, leaving out those that match `target_exclude_where` and those whose owner pair from the subject has a `source_where` or `target_where` they do not meet (an external module for a requirement whose module pair needs `external: no`) |
| `cross_document` | `subject` (a list of type or group keys), `members` (a type or group key), `via` (a list of relations), `mentions` (optional), `min_documents`, `max_pairs`, `pair_floor`, `max_candidates`, `exclude_where` (optional), `also_writes` (optional, a list of relations) | A `subject` hub whose members come from at least `min_documents` documents, with at least one pair of members sharing no evidence document. Members: `members` nodes linked to it by one `via` edge in either direction and, with `mentions`, those grounded in a fragment the hub is `MENTIONED_IN`, or grounded in itself when their own name, quote or statement there names the hub; a member that, or whose owner, matches `exclude_where` is left out | Member pairs sharing no evidence document, not linked by any relation the rule writes, not of one owner: the `max_pairs` best by exact cosine over `embedding`, at least `pair_floor`. Each orphan member (no owner of any basis: inferred owner edges of earlier builds count) also gets at most `max_candidates` owner candidates: the owners of the members it is paired with, then the hub when it is a legal owner target |

## Loading and validation

`load_ontology(path=None)` loads the shipped ontology, or another file, and returns a validated,
immutable `Ontology`. Any problem raises one `OntologyError` that lists every problem found. Checks:

1. Known keys only, and no key written twice in one mapping; required fields present; values of the right type (`inference.cap`, `first_cap` and `first_budget`, and an inference rule's `cap` and `first_cap`, positive integers); `format_version` is 2.
2. Labels, relation names and attribute names match safe patterns, so they can be used in Cypher; type, group, vocabulary and attribute keys are snake_case; an attribute never takes a property name the data model owns (`uuid`, `spec_id`, `name`, `embedding` and the like); each label belongs to one type, a type key is no group key, and a relation name is no label.
3. Every reference (group, pair endpoint, `ref` target, `derive`, `derive_via`, `supported_by`, spine, inference, `conform`) resolves; every relation named by `default_from` and every attribute and value named by `default`, `where`, `not_across`, spine `exclude`, `exclude_where` and `target_exclude_where` (on a target or its owner) is declared.
4. Each relation pair appears once; several `default` pairs of one label pair have disjoint `where` conditions (when a null value has no default, several may hold and the first declared one wins); `default_from` only on a `default` pair; `owner_rank` only on an `owner` pair; a relation with a `cue_required` pair has cues, each a valid regular expression.
5. `id.format` prefixes are unique and no prefix is a prefix of another or of a built-in prefix (`DOC::`, `FRG::`); an id family names one type only.
6. Each spine source reaches a root through some declared path.
7. Rule ids are unique. Inference rules name an existing template with all its required parameters and no others, a shipped judge prompt, and a relation of the file's own owner legal for the template's endpoints (for `endpoints`: between every two targets, either way round; for `realization`: from the subject to every container; for `attach_orphan`: an owner pair for at least one subject → target pair; candidates are limited to legal targets). Only `cross_document` takes a list of `subject` keys. For `endpoints`, each `via` relation is named once and is legal from the subject to a target, `min_targets` is not above `max_targets`, and `mentions` needs a hub target; for `realization`, `member_relation` has a pair ending at the subject and some container path reaches a container from a member. A rule `cap` is a positive integer and a rule `floor` lies in 0..1 (on a `cross_document` rule it binds link items only). `also_writes` appears only on a `cross_document` rule, names each relation once and never its `writes`; for `cross_document`, `subject` names hub types, each `via` relation is named once and is legal between a member and a subject in either direction, `writes` and every `also_writes` relation is legal for at least one pair of ends: two members, or a member and an owner candidate, where the owner candidates are the `subject` labels plus the targets of the members' owner pairs; `min_documents` is at least 2, and `pair_floor` lies in 0..1.
8. No declared name equals a built-in name (`SpecNode`, `Document`, `Fragment`, `CONTAINS`, `REFERENCED_BY`, `MENTIONED_IN`).
9. Every declared type and relation has an origin its file's `owner` owns ([architecture.md](architecture.md)).
10. A `symmetric` relation's pairs hold in both directions; none is `exclusive` or `owner`, and none has `source_where` or `target_where`.
11. `otherwise` and `fallback_relation` name a declared relation that is not `stated_only`; `otherwise` is legal for the ends of its own pair, `fallback_relation` for the ends of at least one owner pair; `where_reason` is a quarantine reason.
12. A `stated_only` relation has no `derive`, `derive_via`, `default` pair or `id_references`, and no inference rule writes it, with one exception: a `cross_document` rule may name it in `also_writes` when the relation declares `cues`. This is an exception to the stated-only rule (T7): inference writes such an edge only when the judge's source-side quote matches a cue as whole words, ignoring case ([analysis.md](analysis.md)), and the shared validator accepts it as `inferred`.
13. `inherit.via` is a relation of an owner pair of the type, and an attribute has `inherit` or `default`, not both; `derive` and `derive_via` rules yield legal pairs.
14. Relations normalisation adds late are never checked again: a `derive_via` relation is not `acyclic` and has no `owner` or `exclusive` pair, and the `fallback_relation` has no `source_where` or `target_where` on a pair it can produce (the ends of an owner pair or of an `acyclic` relation's pair, either way round).
15. `conform.answered_elsewhere` names a declared extracted atomic type and a shipped judge prompt; `candidates` and `cap` are positive integers; `min_cosine` and `floor` lie in 0..1.
16. `reads` names each rule once, and only rules declared before it in the file (never itself or a later one), and appears only on a `realization` rule (a narrowing of the concept's `reads`): the other templates read only `stated` and `implied` edges, and a `cross_document` rule reads earlier rules' owner edges implicitly ([analysis.md](analysis.md)). `batch` and `batch_prompt` appear only on an `attach_orphan` rule; `batch` is an integer of at least 2 and needs a `batch_prompt`; a `batch_prompt` names a shipped judge prompt (in the file's `judge_prompts/`) and may be named without `batch` (batching off), so a replay can turn batching on (shipped: `INF-CONT-01` and `INF-CONT-02` name `judge_attach_orphan_batch`, no `batch`).
17. `impact` on a relation or a pair is one of the six roles; any other value is rejected as a format error before the checks run. A relation or pair without `impact` is `none`, so an ontology written before the field loads unchanged.
18. A `symmetric` relation has no `contains` role on any pair, and the roles of a pair and its reverse are inverses: `uses` with `used_by`, every other role with itself ([trace.md](trace.md) "Impact roles in the ontology").
19. `contains` (on a relation or a pair) needs the relation to have at least one `owner` pair or to be named by a spine path.

## Ontology files of submodules

Each submodule ships its own ontology file as package data: `graph.core` ships the build file
([spec-ontology.yaml](spec-ontology.yaml), `owner: build`), `graph.plan` ships `graph/plan/ontology.yaml` (`owner: plan`,
[plan/ontology.md](plan/ontology.md)), and `graph.gap` ships `graph/gap/ontology.yaml` (`owner: gap`, origin `agent` for its
findings, [gap.md](gap.md) "Ontology file"). Beside its ontology file a submodule ships its own queries and DDL, and
every query is registered by name in each store's dialect: `graph.gap` ships `graph/gap/ddl/gap_schema.cypher` and
`graph/gap/queries/*.cypher`, registered as `gap.<name>` in the Cypher dialect (`stores/_bolt/cypher.py`) with SQL ports in
`stores/postgres/queries/gap/` (`stores/postgres/dialect.py`); `graph.plan` does the same under `graph/plan/ddl/` and
`graph/plan/queries/`. `load_ontology` merges the shipped files of the installed
submodules into one `Ontology` and validates the union, so names stay unique across files and pairs may
cross owners (for example a gap relation from a finding to a `FunctionalRequirement`). Vocabularies and
node groups are shared by name across files; one declared in two files must be declared the same. The
build file has `owner: build` and a submodule file another owner; only the build file declares `corpus`,
`spine`, `conform` and `inference`.

A relation of another owner may also end at the built-in labels `Fragment` and `Document`, so that gap and
plan can cite evidence. Build still owns those nodes; a relation that ends there belongs to its declaring
owner, and when build retires a fragment such relations stay.

The parts of the ontology that reach extraction prompts form its fingerprint (the `stated_only` flag
too, since the edge prompt shows it); when the fingerprint changes, stored extraction results stop
matching and are re-extracted ([build.md](build.md)). Submodule files declare no extracted type or
relation, so adding or changing one never changes the fingerprint. Adding or renaming build labels or
relations needs a migration ([build.md](build.md) Migration).

A renamed-ontology test builds one corpus with the shipped ontology and with a copy whose declared
labels, relations, id prefixes and enum values are all renamed, including the names held by `where`,
`otherwise`, `inherit` and `conform`; the two graphs must map onto each other.

## Shipped ontology

File: [spec-ontology.yaml](spec-ontology.yaml) is the **initial template**. It declares only what
`graph.build` owns. When development starts it is copied into `graph.core` as package data; from then
on the package file is the shipped ontology, and this template is kept in step with it in structure
only.

What the template fixes and what implementation may improve:

| Part | Status |
|---|---|
| Structure: labels, groups, roles, `origin`, `tier`, `id` formats, `id_families`, `identity`, `merge_across_labels`, `supported_by`, attribute names, types, enum values, `default` and `inherit`, relations, `pairs` and their flags, relation flags (`symmetric`, `acyclic`, `stated_only`, `id_references`), `derive`, `derive_via`, `spine`, `conform`, inference rules | Fixed by this spec. A change is a spec change, made here first. |
| Prompt text: `corpus.framing`, `corpus.exclude`, type and relation `description` and `guidance` (definitions, naming rules, GOOD and BAD examples), attribute `description`s, enum value descriptions and `aliases`, `cues` | A starting point. Developers and coding agents may rewrite it to improve extraction, and judge prompt files are written the same way, except that the rules of Rules carried by prompt text must stay. |

The MCP server finds features by the type key `feature` and returns their stored attributes; renaming that key breaks
`list_features` ([../mcp-server/tools.md](../mcp-server/tools.md#list_features)).

Rules for prompt-text changes:

1. Keep the shape: each type keeps a definition, a naming rule, and GOOD and BAD examples that name
   the type a BAD example belongs to; each relation keeps its direction rule and GOOD and BAD examples.
2. Keep examples generic. They come from a made-up shop domain, never from a real project's
   documents.
3. Show the effect: a change is accepted when a run over the test corpus is no worse on the build
   metrics (`spine_unreachable`, `dropped_edges`, `exclusive_conflicts`, `label_disagreement`,
   `fragments_without_nodes`, the quarantine counts and the metrics of Rules carried by prompt text)
   and on a reviewed sample of nodes and edges.
4. Raise the minor `version` (2.1 to 2.2, say). Prompt text is part of the fingerprint, so the next
   `update` build re-extracts what it shaped.

A structure change that alters what a build extracts raises the major `version` (1.3 to 2.0 is one). It
changes labels or relations, so `update` builds refuse the project (`ontology_changed`) and the project
is migrated by dropping its graph and building it with `first` ([build.md](build.md) Migration). A new
optional field that only readers use, with no effect on extraction, merging or any fingerprint, raises
the minor `version` and needs no migration: `impact` did so (2.0 to 2.1, A44). The plan fingerprint
includes the ontology version, so every project's plan shows `outdated` once after the deploy; the
next plan run clears it and reuses everything.

| Label | Role | Group | `spec_id` prefix | Id families | Owner |
|---|---|---|---|---|---|
| `Module` | anchor | - | `MOD::` | - | none; at most one parent module |
| `Feature` | group | - | `FEAT::` | - | one module, not external, not deployment |
| `Capability` | group | - | `CAP::` | - | none (a tag) |
| `Interface` | anchor | - | `IFC::` | - | none; at most one providing module |
| `DataEntity` | group | - | `ENT::` | - | none; dropped unless a data requirement, `STORED_IN` or `CARRIES` supports it |
| `FunctionalRequirement` | atomic | requirement | `REQ::FR::` | FR, UC, US | one feature or module |
| `QualityRequirement` | atomic | requirement | `REQ::NFR::` | NFR, QR, PERF, SEC | one feature or module |
| `Constraint` | atomic | requirement | `REQ::CON::` | CON | one feature or module, external allowed |
| `BusinessRule` | atomic | requirement | `REQ::BR::` | BR | one feature or module |
| `IntegrationRequirement` | atomic | requirement | `REQ::INT::` | INT, IR | one interface, else the calling module |
| `DataRequirement` | atomic | requirement | `REQ::DATA::` | DATA, DR | one data entity |
| `OpenQuestion` | atomic | - | `QST::` | - | none; own identity scope, not on the spine |

The six requirement types share one identity scope, so the same requirement extracted under two
labels is still one node. `Interface` and `DataEntity` never merge with another label. An
`OpenQuestion` is a question or TBD the text states.

| Attribute | On | Rule |
|---|---|---|
| `stmt`, `quote` | every type | the node as one sentence; verbatim supporting text. `quote` is a format-level name: the node call gives it and Conform grounds it, the attributes pass never asks for it |
| `acceptance_criteria` | requirements | the criteria the text states, else null; never invented |
| `source_status`, `is_assumption` | requirements | `stated` or `implied` (vocabulary `source_status`); `yes` or `no`: the text presents it as an assumption |
| `release` | requirements, features | the verbatim release label ("MVP", "Phase 2") or null; a requirement's null is filled from its owner when read (`inherit: {via: PART_OF}`) |
| `in_scope` | requirements, features, modules, interfaces | `yes` or `no`, default `yes` (stored); `no` only from an explicit scope statement |
| `kind` | interfaces | `api`, `event`, `queue`, `file`, `other` |
| `external`, `type` | modules | `yes` or `no`; `frontend`, `backend`, `datastore`, `deployment`, `other` |
| `module` (ref) | FR, NFR, CON, BR, INT, features, capabilities | derives `PART_OF` (`REALIZED_BY` for capabilities) |
| `interface`, `entity` (refs) | INT, DATA | derive `PART_OF` |
| `provider` (ref, `store: false`) | interfaces | derives `PROVIDED_BY`: the module the text names as providing or owning the contract (a Provider, Owner or Service column counts); a fallback only, dropped when the interface has a stated `PROVIDED_BY` ([build.md](build.md) Conform 4) |

| Relation | Pairs and flags | Notes |
|---|---|---|
| `PART_OF` | FR, NFR, CON, BR → feature (owner, rank 1, default from `GROUPED_BY`); FR, NFR, BR → module (owner, rank 2, target not external, otherwise `RELATES_TO`, reason `external_owner`); CON → module (owner, rank 2); INT → interface (owner, rank 1); INT → module (owner, rank 2, as FR); DATA → data entity (owner, rank 1); feature → module (owner, target not external and not deployment, reason `external_owner`); module → module (exclusive, cue required, depth 1, not across `external`, target not deployment, otherwise `DEPLOYED_TO`) | One owner per requirement and feature. A requirement about the whole system or several modules is `PART_OF` none of them. An integration requirement without a named interface is owned by the calling module. A requirement extracted as `GROUPED_BY` a feature is owned by it; any other relation to a feature (a mixed-level `DEPENDS_ON`) stays illegal |
| `GROUPED_BY` | feature → capability; requirement → capability (redundant via owner) | grouping, not containment |
| `REALIZED_BY` | capability → module | a capability is usually realised by several modules |
| `INTEGRATES_WITH` | module → module (default, both ends not deployment; otherwise `DEPLOYED_TO`) | stored directed from the module that calls to the module called; derived from interfaces (`derive_via`: consumer → provider). In extraction a two-way integration is two edges; inference writes one direction per pair. The direction is a hint: analysis reads A to B and B to A as one integration |
| `DEPLOYED_TO` | module → module (default, source not deployment, target deployment) | stated, from the modules the text names |
| `PROVIDED_BY` | interface → module (exclusive) | the owner of the contract (for an event: who defines the message); external allowed; also derived from `interface.provider` when no edge states it |
| `CONSUMED_BY` | interface → module | external allowed |
| `STORED_IN` | data entity → module | usually a datastore |
| `CARRIES` | interface → data entity | the payload |
| `DEPENDS_ON` | feature → feature; requirement → requirement (distinct owner) | acyclic, stated only, with dependency `cues` ("requires", "depends on", "only after"); never mixed levels. Only exception to stated only: `INF-XDOC-01` writes it across documents when its source-side quote matches a cue |
| `RELATES_TO` | requirement ↔ requirement (distinct owner); requirement ↔ module, interface, data entity (requirement → data entity: default from `PART_OF`); open question ↔ requirement, feature, module, capability, interface, data entity | symmetric, `id_references`; only links the text makes. An open question is the only node that relates to a feature. A non-data requirement extracted as `PART_OF` a data entity relates to it instead |

Module rules: an external module owns only constraints; another requirement `PART_OF` it becomes
`RELATES_TO` it, and a feature `PART_OF` it is quarantined. A deployment module (`type: deployment`)
is no container and owns no feature; modules reach it by `DEPLOYED_TO`.

Spine: requirements reach a module by 1-3 `PART_OF` edges; by `PART_OF` then `PROVIDED_BY`,
`CONSUMED_BY` or `STORED_IN` (through an interface or data entity); by `GROUPED_BY` then
`REALIZED_BY`; by `PART_OF`, `GROUPED_BY`, `REALIZED_BY`; or by one `RELATES_TO` edge. A symmetric
relation on a spine path counts in either direction, because it is stored in canonical order. A
requirement with `in_scope: no`, or whose owner has it, is excluded.

Conform: judge prompt `judge_owner_conflict`, cap 200 calls per build, floor 0.7, fallback relation
`RELATES_TO`. Answered elsewhere: `OpenQuestion`, judge prompt `judge_answered_elsewhere`, 4
candidate fragments, `min_cosine` 0.35 (set at TEST.4 from offline cosines, [build.md](build.md)), floor 0.8, cap 60 calls per build.

| Inference rule (in order) | Template | Writes |
|---|---|---|
| `INF-CONT-01` | `attach_orphan`: a requirement with no owner, not `in_scope: no`, 3 candidate features or modules among its legal owners, none `in_scope: no` (`target_exclude_where`); prompt `judge_attach_orphan`; floor 0.8 | `PART_OF` |
| `INF-CONT-02` | `attach_orphan`: a feature with no module, not `in_scope: no`, 3 candidate modules, none `in_scope: no`, external or a deployment target (its owner pair's `target_where`); prompt `judge_attach_orphan` | `PART_OF` |
| `INF-INT-01` | `endpoints`: an integration requirement not owned by an interface, with 2-4 modules by `PART_OF`, `RELATES_TO` or mentions (`via: [PART_OF, RELATES_TO]`, `mentions: true`, `exclude_owned_by: [interface]`); prompt `judge_endpoints` | `INTEGRATES_WITH` between those modules |
| `INF-CAP-01` | `realization`: a capability and the modules its grouped members reach by 1-3 `PART_OF`, one `RELATES_TO`, or `PART_OF` then `PROVIDED_BY` or `CONSUMED_BY`; `reads: [INF-CONT-01, INF-CONT-02]`, so the `PART_OF` edges those rules infer in the same build count on the paths; prompt `judge_realization` | `REALIZED_BY` |
| `INF-XDOC-01` | `cross_document` (last): a module, feature, interface or data entity whose requirement members (`via: [PART_OF, RELATES_TO]`, `mentions: true`, not `in_scope: no`) come from 2 or more documents; at most 8 member pairs from different documents (`pair_floor` 0.55, calibrated at TEST.4 R0); 3 owner candidates per member with no owner of any basis (inferred owners of earlier builds count); prompt `judge_cross_document` | `RELATES_TO`; also `DEPENDS_ON` (only with a dependency cue) and `PART_OF` (an orphan member's owner) |

Cap 100 subjects per rule per build in every mode (was 50; P8.1). In `first` and `rebuild` builds a rule
goes on past it up to `first_cap` 2000 subjects while the build's inference input stays within
`first_budget` 11,000,000 estimated tokens (TEST.5 R0: 1.25 × the larger test corpus's measured full-drain
input of 8.49M, rounded up to 1M). A `first_cap` of
2000 (the concept: "e.g. 1000") with a build token budget and a `cap` every rule keeps is decided. A
rule's effective `first_cap` is never below its `cap`. Floor 0.7; `INF-CONT-01` has its own floor 0.8 (TEST.5 decision 5.16 under the frozen rule: in R1 its [0.7, 0.8) band
was .65 precise on the larger corpus and .43 on the smaller, below .70, while the band at 0.8 or more was .85 and .93). `INF-XDOC-01`
has its own cap, 36, in updates too (decided; the concept's update cap is 100) (TEST.4 R0: about 16k
input per call, so about 10% of the larger test corpus's build input, where 150 took 29%; the smaller
corpus is re-measured in TEST.4 R1), and its own floor for link items, 0.85; owner items keep 0.7 (TEST.4b r2-fix: every wrong link of the
smaller corpus's two replays was below 0.85 or 0.90; at 0.85 its link precision rises from .71 to .86 and
the larger corpus keeps 216 of 221 links).

### Rules carried by prompt text

No flag can express these rules. They live in the prompt text named here, must survive prompt-text
changes, and are watched by the metric or sample named.

| Rule | Home | Measured by |
|---|---|---|
| A deployment module owns only infrastructure requirements; "all services run in X" is one requirement owned by X, not a `DEPLOYED_TO` edge from every module | module `type` and `DEPLOYED_TO` descriptions | sample of requirements owned by deployment modules; `deployed_to_fan_out` |
| A requirement `RELATES_TO` only the modules the text names | `PART_OF`, `RELATES_TO` and requirement group descriptions | `relates_fan_out` |
| A requirement `RELATES_TO` a data entity only when it explicitly creates, changes or reads it | `RELATES_TO` description | `entity_relates_fan_out` |
| Open questions are stated only: no rhetorical question, no answered (FAQ) question | `OpenQuestion` description and the extraction prompt's corpus rules | sample precision |
| `acceptance_criteria` only from the text | attribute description and the attributes prompt | sample of non-null criteria with a quote |
| `release` and `in_scope` come from the text or its heading path, never guessed | attribute descriptions and the attributes prompt | sample of `in_scope: no` |
| A reference attribute names the part the text names as responsible, hosting or owning; a heading counts only when it is that part's own name, never a topic, chapter or document heading | `ref` attribute descriptions and the attributes prompt | `spine_unreachable`; references equal to a topic heading |
| An integration requirement is owned by the caller | `PART_OF` description | sample of integration requirements owned by an external or called module |
| A requirement is `PART_OF` a feature or module only when the fact is in that part's section or names it | edge system message | `PART_OF` sample precision; `owners_by_source` |
| Each interface a window names gets `PROVIDED_BY` when the text, its section heading or a Provider, Owner or Service column names the module | edge system message; `interface.provider` description | `interfaces_without_provider`; `PROVIDED_BY` sample by basis |
| A shared topic is not a cross-document link: both quotes must constrain the same attribute, value or behaviour (not only name the same entity, workflow, agent or parameter), the same parameter on two surfaces is no link, a decision-log row links only to the rule it decides, and each quote states its own member's fact; members of one owner are not linked; a dependency needs "requires", "only after" or the like; an owner only when the owner-side quote shows the member belongs to that part (not a part it only acts on, not when its own section is another part's, never the whole system, a list of tools that "may provide" something, a scope bullet or a separate success metric) | `judge_cross_document` | sample of accepted `INF-XDOC-01` items |
| A requirement the window's text refers to by section, chapter or id is linked as the referring sentence says: `RELATES_TO` by default, `DEPENDS_ON` only with a dependency cue, never `PART_OF` from a reference alone | edge system message (reference line; relations from the `id_references`, cue-gated and owner flags) | "§" reference recall sheet |
| A question is answered elsewhere only when a candidate text answers it, quoted; a partial answer keeps it | `judge_answered_elsewhere` | sample of dropped questions; `oq_partly_kept` |
| An inferred integration links only the two modules the integration requirement says exchange data or call each other; when that pair is already linked, no other pair is linked in its place | `judge_endpoints` | sample of accepted `INF-INT-01` items |
| A capability is realised only by the part the text says does its work; a part that only stores, holds or receives something the capability deals with (the holder of a rotated key, the store a report reads) or that the capability acts on does not realise it, nor does a part only listed in a member's row | `judge_realization` | sample of accepted `INF-CAP-01` items |
