---
type: Design
title: "Light inference and verification"
description: "Light inference and verification with repair, run after every persist"
status: stable
tags: [graph]
---
# analysis.md: light inference and verification with repair, run after every persist

## Shared validator

One validator checks the ontology's hard rules on a graph view: Conform's invariant check, inference
proposals and verification use it. Checks: `illegal_pair` (a pair the ontology does not declare),
`where` (a failed `where` condition), `owner` (more than one target over a source's `owner` pairs),
`exclusive` (more than one target of an `exclusive` pair), `depth` (a chain past `max_depth`),
`across` (ends differing in a `not_across` attribute), `acyclic` (a cycle of an `acyclic` relation)
and `stated_only` (a non-`stated` edge of a `stated_only` relation, except an `inferred` edge whose
`inference_rule` is a `cross_document` rule naming the relation in `also_writes`; [ontology.md](ontology.md)
check 12). Each violation names the check,
relation, ends and basis. Rules that normalisation enforces by dropping edges (`distinct_owner`,
`redundant_via_owner`) are never violations.

## Light inference

Adds domain edges between existing spec nodes when the source texts state or directly imply them.
It never creates, merges or relabels nodes.

| Rule | Behaviour |
|---|---|
| When | Once per build, after persist; off when `BuildSettings.inference` is false |
| Input view | Only `stated` and `implied` edges (an edge without `basis` is read as `stated`), so earlier inferred edges never feed new candidates; two exceptions: one owner per node: inferred owner edges of earlier builds count as owners (an `attach_orphan` node another rule's inferred owner edge attaches is no subject; `cross_document` below); and a rule's `reads` (Ordered rules with `reads`) |
| Order | Rules run in the ontology file's order (shipped: `INF-CONT-01`, `INF-CONT-02`, `INF-INT-01`, `INF-CAP-01`, `INF-XDOC-01` last). Each rule selects its subjects and candidates when it runs, after the rules before it have judged; the current edges the validator checks against are read once, up front. A rule sees the edges another rule adds in the same build only through `reads`, and a `cross_document` rule counts the owner edges accepted by earlier rules of this build as owners (an implicit read) |
| Subjects and candidates | From the ontology's inference rules and templates ([ontology.md](ontology.md)); a subject has at least one candidate |
| Judge | One structured call per subject through the core judge runner, with the subject's and candidates' names, statements and evidence fragment texts. An `attach_orphan` payload also gives the subject and each candidate its `sections`: per evidence fragment shown (at most 3), the fragment's heading path extended by the headings of its text still open at the node's first evidence line there, so the judge can prefer the candidate whose own section holds the subject (TEST.5: the nearest heading is the strongest owner evidence). A `realization` payload shows at most 12 members and 12 candidates; a cut subject is counted as `truncated`. A `cross_document` payload is described below (Cross-document links) |
| Judge memory | An outcome is stored per rule, subject and input hash (the payload less each node's statements and sections, every shown node by identity key, label, name and quote, the fragments by id and content hash, the prompt ref and the judge model; an `attach_orphan` subject's candidates as a set (a reworded statement or `stmt` of an unchanged node, or a new order of the same candidates, keeps the hash; a new quote, name, fragment or candidate changes it; the earlier hash, over the whole payload, made every stored judgement of a project expire once at this change — accepted, only test projects are stored)) in the `ExtractionStore` (kind `decisions`). A subject with unchanged inputs reuses it without a call and counts against no limit. A failed judgement is not stored. Fresh judgements are written every 100 completed calls, before the swap (Limits and backlog); a batched `attach_orphan` judgement is stored under its subject's single-call input hash |
| Acceptance | `accepted`: the verdict names at least one candidate, its confidence is at least the rule's `floor`, else the ontology's (0.7; `INF-CONT-01`: 0.8; `INF-XDOC-01`: 0.85 for link items, owner items 0.7) and every cited quote is found in the cited fragment (an `attach_orphan` quote its cited fragment does not hold counts when a shown fragment of the subject, else of the chosen candidate, holds it, and the edge cites that fragment: TEST.5b, 13 of 15 such drops had a garbled fragment id or the quote of another shown fragment of the two ends). `declined`: a valid "no link" answer. `invalid`: it names an id or pair it was not given, or its edge fails the shared validator against the current graph plus the proposals accepted before it and every inferred edge the swap keeps (the other rules' planned edges, a rule not judged yet keeping its current ones, and the rule's own edges of subjects it does not judge: deferred, sampled out, unreadable or failed; TEST.5b: an `INF-XDOC-01` owner beside a kept owner of a deferred hub) (such an edge is quarantined `invalid_proposal`). Else `rejected`. An unsure judge writes nothing. A subject that is judged again and gets no accepted link loses its earlier inferred edges |
| Bound | The limits by mode of Limits and backlog below: a rule's `cap` in every build, in `first` and `rebuild` up to `first_cap` within the build's `first_budget`. Subjects a limit leaves are queued for later builds and keep their current inferred edges; every build drains the queues, a build whose diff changed nothing included |
| Write | The new set of inferred edges replaces the old set in one transaction, over every relation a rule writes (`writes` and `also_writes`); deferred (queued) subjects and subjects whose judgement failed keep theirs. An edge already present as `stated` or `implied` is not written. |
| Edge | `basis = inferred`, `inference_rule`, `confidence`, `evidence`, `source_fragment_ids`, `judge_model`, `inferred_at` (the write time) ([data-model.md](data-model.md)) |
| Failure | Only judge failures and invalid verdicts are tolerated: a failed judgement is counted and its subject keeps its edges; a non-fatal model error that stops a rule is reported in that rule's `error_code` and leaves its edges unchanged. Credential, model, infrastructure and conformance errors fail the build, and the previous inferred edges stay (one swap transaction) |

Shipped rules, in order: `INF-CONT-01` attaches an orphan requirement to one of its 3 most similar
project features or modules (exact cosine over `embedding`), or to its section hub, with `PART_OF`.
The section hub is a fourth candidate when it is not among the 3: the headings of the orphan's
section (its first evidence fragment's heading path and open headings, without the document title)
are read nearest first, and the first heading that names a legal candidate (by whole name without
the section number or a `…:` prefix, no alias; else by a name of two or more words the heading
contains) gives it when it names exactly one (TEST.5b: in 15 of 26 sampled missed owners the
chapter's own hub was not offered; this offers it for 10 of them). An orphan has no target on
any owner pair; a node that has `in_scope: no`, or whose owner has it, is not a subject. Candidates
come only from the labels that are legal owners of the subject, so a data requirement (owned only by a
data entity) is never a subject and an integration requirement gets modules only; the judge may
answer "none" and never attaches a system-wide statement to one module. `INF-CONT-02` does the same
for a feature with no module, with 3 candidate modules; an external or deployment candidate fails the
validator. `INF-INT-01` links the modules of an integration requirement that is not owned by an
interface with `INTEGRATES_WITH`. Its modules are those it reaches by `PART_OF` or `RELATES_TO`, plus
the modules mentioned in its evidence fragments. The judge gives one direction per pair, from the
module that calls to the module called; a verdict linking a pair both ways is invalid for that pair,
and a pair already linked in either direction is skipped. `INF-CAP-01` adds `REALIZED_BY` from a
capability to the modules its grouped members reach by 1-3 `PART_OF` edges, one `RELATES_TO` edge, or
`PART_OF` then `PROVIDED_BY` or `CONSUMED_BY` (through an interface); it reads `INF-CONT-01` and
`INF-CONT-02`, so members those rules attach in the same build count. `INF-XDOC-01` (last) links
requirements of different documents that share a hub (Cross-document links).

### Limits and backlog

Every rule judges its fresh subjects (those without a stored judgement for their current inputs) up to
limits that depend on the mode, and queues the rest for later builds.

| Part | Rule |
|---|---|
| Order | (1) subjects judged before whose inputs changed; (2) the rule's persisted queue, in its order (oldest first); (3) never-judged subjects not queued. (1) and (3) in `spec_id` order (`cross_document`: the order subjects chose their pairs, most members first). In an `update`, when (1) holds more than half of the `cap` and the queue is not empty, ⌊`cap` / 2⌋ places are kept for (2); places the queue does not fill go back to (1) |
| Limits | Every mode: the rule's `cap`, else `inference.cap` (shipped 100; `INF-XDOC-01` 36). `first` and `rebuild`: past its `cap` the rule goes on, in order, up to its `first_cap`, else `inference.first_cap` (2000), fresh subjects while the build's inference input stays within `first_budget` (11M): a subject past the `cap` is judged only when the counted input of every fresh inference call of this build so far, plus its own, is within the budget; the first that does not fit stops the rule. Every rule keeps its `cap` even when the budget is spent (its calls count toward it). Input is counted before each call from the rendered judge messages, as the estimate counts it (`request_tokens`, about 3.6 characters per token); reused judgements cost nothing |
| Update scope | In an `update` whose diff changed something, a `cross_document` rule judges first the subjects whose members changed (created, modified or rekeyed this build), joined or left since the last build (a never-judged subject only when a member joined: no record of its members), then its queue (decided; before, the queue waited), under its `cap` and the reservation above; it judges no other subject. Every build records each subject's member `spec_id`s in judge memory |
| Drain | Every build drains the queues: an `update` whose diff changed nothing judges (2), then (3), under the `cap`; a subject whose inputs changed all the same (a read rule's edges moved as its queue drained, such as `INF-CAP-01` when `INF-CONT-01` drains) is not judged in it but queued after the rest, so the next build judges it from the queue (`RuleReport.changed_queued` counts them); other `update`s after (1). Follow-up builds are `update`s and drain too ([build.md](build.md) Follow-up builds and settlement) |
| Queue | Subjects a limit left, in the order the next build would take them, and subjects whose judgement failed, at the head: `InferenceReport.backlog` maps a rule id to a `BacklogQueue` (`spec_ids` in order; `since_build`, the build since which the queue has not been empty; `age_builds`, the builds since then that ended with it not empty, this one included). A build reads the queues from the infer artifact of the project's latest finished build. A queued subject that is no subject any more, or has a stored judgement for its current inputs, drops out. A rule that fails (`error_code`) keeps its previous queue. Subjects a build does not take at all (a `cross_document` subject outside the update scope) are not queued. The queue is a recomputable cache, in no other store: a lost queue loses only its order (judge memory still tells judged from never-judged subjects) |
| Calls and chunks | Admission is decided before any call: the rule's calls are admitted in judging order (subjects read and counted 100 at a time) until a limit stops it. The admitted calls then run in a sliding window of twice the gateway's concurrency (a call waiting out a retry backoff frees its gateway slot), so a slow call holds one place, never a whole chunk. Verdicts are evaluated in judging order whatever order the calls finish in (each as soon as every earlier subject has its verdict), so the shared validator and the queue see a fixed order. After every 100 completed calls the judgements and `seen` keys evaluated so far are written to judge memory, with the counted input of the build's completed calls (key `<project_id>\|budget\|<build id>`), so a resumed infer step reuses them without calls, charges its budget from that input (a crash and resume never spend the budget twice) and its swap writes their edges as judged (an edge an earlier judgement wrote, left by the failed swap, is refreshed). A `cross_document` subject's `members` key is written only after the swap commits (written before it, a resumed `update` could miss changed members). The swap stays one transaction (FR-INF-04); a dry run writes no memory. Writing memory before the swap is decided |
| Report | Per rule (`RuleReport`): `deferred` (fresh subjects not judged), `limit` (`cap`, `first_cap` or `budget`: the limit that stopped the rule; none when no fresh subject was left), `budget_tokens` (the counted input of its fresh calls), `backlog` (its queue's length after the build) and, in a build whose diff changed nothing, `changed_queued` (subjects with changed inputs it queued without a call) |

An `attach_orphan` rule with `batch: N` judges up to N orphans written in the same section of the same
document (the heading path of each one's first grounding fragment; that fragment when it has no section)
in one call (its `batch_prompt`; shipped `judge_attach_orphan_batch.3`). Each subject is listed with its
own candidates and fragments, those its single call would show; a candidate or fragment several
subjects share is shown once. One verdict, quotes and checks per subject: its target only from its own
candidates (another subject's candidate makes its answer `invalid`), its quotes only from the fragments
its own call would show. Judge memory per subject under the input hash of its own single call (the
single prompt's key, whether it is judged in a batch or alone), so turning `batch` on or off,
regrouping or a new batch prompt version judges nothing again; only a change of the single prompt or of
the subject's inputs does. A failed batch, or one whose answers are malformed (a subject id it does not
show, or a subject answered twice), is judged again one subject per call; a subject a batch leaves
without an answer is judged again on its own. Groups form over the whole judging order, within one part
of it ((1) up to the queue's reservation, (2), the rest of (1), (3)) and one side of the `cap`; calls
run in the order of their first subject, so a batch never pulls a later subject ahead of an earlier
part, such as the places an `update` keeps for the queue; a call counts toward `cap` and `first_cap`
with its last subject and toward the budget with its own counted input, and a group a limit cuts is
judged up to the limit (the calls of a failed batch are charged, never stopped). The shipped ontology
sets no `batch`; it is set only if the TEST.5 replays (R0b, R0b2) show no quality loss.

### Ordered rules with `reads`

A rule may name earlier rules in `reads` (only a `realization` rule, decided;
[ontology.md](ontology.md) check 16). Its input view is the `stated` and `implied` edges plus the read
rules' inferred edges as this build plans them: the edges the swap will leave for those rules (kept,
reused and newly accepted, less those it removes), computed by the function that builds the swap rows,
so a dry run sees the same view. `realization` container paths are walked in code over the spine graph
(an inferred edge carries its `inference_rule`); with no read edges the walk gives the containers of the
graph query it replaces. A reading subject's judgement input includes the read edges behind each
candidate, so a change in them judges it again; a candidate that read edges alone also reach keeps none
(the overlay neither makes nor unmakes it, so its moving edges do not change the input). The shipped `INF-CAP-01` reads `INF-CONT-01` and
`INF-CONT-02`; `INF-INT-01` does not read ([later.md](later.md)).

### Cross-document links

The `cross_document` template ([ontology.md](ontology.md)) links members of one hub that different
documents state. Shared hubs are the meeting point; no code compares documents otherwise.

| Part | Rule |
|---|---|
| Subjects | Hubs of the `subject` labels (shipped: module, feature, interface, data entity) whose members (shipped: requirements by `PART_OF` or `RELATES_TO` in either direction, or grounded in a fragment where the hub is mentioned (`MENTIONED_IN`), or in one that also grounds the hub when the requirement's own name, quote or statement there names the hub (its name or a `surface` alias, in hub form as `MENTIONED_IN` counts; TEST.4b: a long Q&A fragment grounds many hubs, so sharing it is no tie on its own); not `in_scope: no`) come from at least 2 documents, with at least one member pair sharing no evidence document |
| Owners | A member's owners are its `stated` and `implied` owner edges, the `PART_OF` edges earlier rules accepted in this build and the inferred owner edges of earlier builds (any rule). For one subject, an inferred owner edge of this rule from an earlier build that the subject could give again (to the hub, or to the owner of another of its members) is left out, so the subject judges that owner again and its judgement input stays the same; any other owner keeps the member owned. This counts for the pair exclusion, the payload's `owner`, the distinct-owner check and the owner candidates (TEST.4: R2 added a second owner next to R1's inferred one, which Verify repaired) |
| Pairs | Members sharing no evidence document, not of one owner, not linked by a `stated` or `implied` edge of a relation the rule writes (either direction): the `max_pairs` (8) best by exact cosine over `embedding`, at least `pair_floor` (0.55, calibrated at TEST.4 R0: link precision .97, and it does not bind), ties by `spec_id`. The cosine is that of Neo4j's `vector.similarity.cosine`, normalised to (1 + cos) / 2, and `pair_floor` is on that scale; it is computed in Python (float64, equal to Neo4j's float32 result within 1e-6) from the members' embeddings, read once for every hub. Subjects choose in order, most members first, then by `spec_id`, and a member pair goes only to the first subject that chooses it: a later subject is shown only the pairs left (`RuleReport.duplicate_pairs` counts those left out; a hub left without a pair is no subject), so one pair is never judged under two hubs in a run |
| Owner candidates | Each orphan member (no owner of any basis, as above): at most `max_candidates` (3), the owners of the members it is paired with (pair order), then the hub, each only when it is a legal owner target of the member over a relation the rule writes |
| Payload | The hub (name, label, statements); the members (local ids `M1`, ... in `spec_id` order, name, label, `document`, owner name or null, statements, and the ids of its shown `fragments`); the pairs; the relations legal between the ends of a shown pair; the owner candidates (ids `O1`, ..., each with its shown `fragments`) per orphan member; at most 20 evidence fragments: each member's first, each owner candidate's first, then the members' other fragments in pair order; each owner candidate's first is reserved, so the members' fragments fill only the room left. `document` joins the member's document titles with `; ` (a title, else the document's path, else its id) |
| Answer | `links`: `{a, b, relation, source, confidence, evidence}` per pair it links (`relation` one the rule writes, `source` the member the relation starts from); `owners`: `{member, owner, confidence, evidence}`; `evidence` a list of `{fragment_id, quote}` |

Each item is evaluated on its own, owners first, then links (`RuleReport.items_by_outcome`, listed in `judged_items` by `spec_id`):

| # | Check, in order | Outcome when it fails |
|---|---|---|
| 1 | The item names a pair or owner candidate it was given | `invalid` |
| 2 | Every quote is found in its cited fragment, and the quotes include one from each end's evidence fragments (for an owner, the member's and the owner's) | `quote_missing` |
| 3 | Confidence at least the floor: for a link the rule's `floor` (shipped 0.85, TEST.4b: the smaller corpus's wrong links sat below it), else the ontology's; for an owner always the ontology's `floor` (0.7; owners were .95 precise there) | `below_floor` |
| 4 | A relation of `also_writes` that is `stated_only` (shipped: `DEPENDS_ON`) needs a quote from the `source` member's fragments matching one of the relation's `cues`; without one the item is written as the rule's `writes` relation (`RELATES_TO`) | `downgraded` (accepted) |
| 5 | The edge passes the shared validator (owner and exclusive, acyclic, distinct owner) against the current graph plus the items accepted before it | `invalid`, quarantined `invalid_proposal` |

Else `accepted`. A subject's accepted items are written as `inferred` edges with both sides' quotes as
`evidence` and their fragments as `source_fragment_ids`. The subject's outcome is `accepted` when any
item is written, `declined` when the answer has no items, else `invalid` when any item is `invalid`,
else `rejected`. Judge memory, bound and swap are those of every rule, with the rule's own `cap` of
subjects (shipped 36, set at TEST.4 R0: about 16k input per call, so about 10% of the larger test
corpus's build input, where 150 took 29%; the smaller corpus is re-measured in TEST.4 R1; kept in updates); subjects past the limits are queued in the order they chose their pairs. A reused judgement's items are
checked again by the shared validator each build, each edge on its own (a refused one is quarantined,
the others written). A deferred subject keeps only its inferred edges between two of its members or
from one of its members to one of that member's owner candidates (another subject's edge sharing one
end is not its), and of those not the stale ones: ends now linked by a `stated` or `implied` edge of a
relation the rule writes, or an owner edge whose member now has another owner (stated, implied,
accepted earlier in this build, or another rule's inferred one). A dry run (`infer(..., write=False)`) counts the edges the swap would add, remove and update, and its judged items keep their quotes (`JudgedItem.evidence`, `(fragment_id, quote)` pairs, source side first) so a replay can be labelled from its report; a build's report never holds them.

The report lists, per rule: subjects, judged, accepted, declined, rejected, invalid, failed,
deferred, reused, truncated, edges added, removed and updated, items by outcome and duplicate pairs
(`cross_document` rules), `limit`, `budget_tokens` and `backlog` (Limits and backlog), `read_edges`
(rules with `reads`: the read rules' planned edges behind its shown candidates), and `error_code` when
the rule failed; the queues (`backlog`); the judged items of every rule; integration requirements with
1 or more than 4 modules; orphan requirements with no candidate; `spine_unreachable_before` and
`spine_unreachable_after`. Edges are named `<source spec_id>|<RELATION>|<target spec_id>`, never by
edge key. Later submodules may read these lists.

Judged items (`JudgedItem`, with `confidence` and outcome) cover every template (decided;
before: `cross_document` rules only): an `attach_orphan` verdict gives one `owner` item, an
`endpoints` or `realization` verdict one `link` item per edge it proposes, a `cross_document` verdict
its items; outcomes `accepted`, `below_floor`, `quote_missing` and `invalid` (`downgraded` for
`cross_document`); a verdict that proposes no edge gives one `declined` item without a target, one that names an id it was not given one `invalid` item without a target; a verdict failing both its quotes and the floor is `quote_missing` (the order of the `cross_document` checks), and every edge of a verdict the shared validator refuses is `invalid`. An item of a rule with `reads` names the read rules' edges this build plans that the container path to its target uses (`JudgedItem.read_edges`, `edge_ref` form; empty when stated and implied edges alone reach it). A replay can lower every floor as a dry run
(`examples/graph_replay.py --floor 0.6`: rule floors and `inference.floor`; the `INF-XDOC-01` link floor
stays; `--first` takes a `first` build's limits, `--sample N --seed S` judges a seeded subset of a rule's subjects and `--batch N` turns batching on), so items can be labelled per rule and confidence band; TEST.5 sets the floors from it.
`spine_unreachable_after` counts spine sources reaching no root over the graph inference read plus every
rule's planned inferred edges, in a dry run too, so a replay predicts it; verification measures it on
the persisted graph.

## Verification

Checks the persisted graph's structure after inference, repairs what it can, and records the run.
Checks read only what `graph.build` owns; foreign nodes, relationships and properties are never a
problem and never touched.

| Check (all blocking) | Problem | Repair |
|---|---|---|
| `node_label` | A spec node without exactly one domain label | Set the label its `spec_id` prefix names; delete the node if none |
| `spec_id` | Missing or malformed `spec_id` | Assign a new one |
| `illegal_edge` | A domain edge on a pair the ontology does not allow | Delete it |
| `ungrounded_node` | A spec node with no `REFERENCED_BY` to an `active` or `skipped` fragment | Restore its evidence from the build's target graph, else delete it |
| `ungrounded_edge` | A `stated` or `implied` edge without valid evidence fragments | Restore its evidence from the target graph, else delete it |
| `inferred_edge` | An inferred edge missing its rule, confidence or evidence | Delete it; the next inference re-adds it if still valid |
| `fragment_state` | A fragment status outside the allowed set, or an `active` fragment whose content was never extracted | Request a follow-up build for its document; the problem stays until that build |
| `v2_rule` | An edge the shared validator rejects (code: the validator check), other than an undeclared pair (`illegal_edge`) | An `inferred` edge is deleted (inference judges it again later); a `stated` or `implied` edge is reported and kept: Conform guarantees these rules, so it is a bug. Edges between two nodes of one owner that appear after inference are only counted (`redundant_after_inference`), never deleted: Diff would write them again at the next update |

The metrics are one consolidated set, defined once and used by the build result, verification and
the test-corpus reports. A metric whose data an older build does not hold is n/a. Spine metrics skip
sources the spine `exclude` matches, themselves or through their owner.

| Metric (reported, never repaired) | Counts |
|---|---|
| `nodes_by_label`, `edges_by_relation_basis` | Spec nodes per label; domain edges per relation and basis |
| `inferred_added_by_relation`, `edges_by_relation_persisted` | Inferred edges the build added, per relation; the build result's edges per relation (n/a without a result) |
| `metric_sources` | Where each metric with a verified and an artifact value came from (`verify`, `spine view`, `artifacts`, `build metrics`, the conform artifact before inference, `n/a`) |
| `judge_calls`, `judge_failed_calls` | Judge calls and failed judge calls per judging step, from the build result ([build.md](build.md)); n/a without them |
| `spine_unreachable` | Requirements that reach no module by a spine path, before and after inference (`spine_unreachable_before`), also per label (`spine_unreachable_by_label`, `spine_unreachable_before_by_label`) |
| `exclusive_conflicts` | Every target past the first in an owner or `exclusive` group, as Conform counts them before normalisation (a build from before normalisation: counted on the graph) |
| `label_disagreement` | Nodes whose computed label differs from their label |
| `dropped_edges` | Edges dropped by legalisation and locality (unknown relation, illegal pair, missing cue, self-loop, locality), per relation; normalisation drops show in `quarantine_by_reason` and the counts below |
| `fragments_without_nodes` | Active fragments no spec node is grounded in |
| `quarantine_by_reason`, `quarantine_by_pair` | Quarantined edges of Conform and inference (`invalid_proposal`) per reason and per `<RELATION>\|<source label>\|<target label>` |
| `owner_conflicts_judged`, `cycles_broken` | Conflict groups sent to the conform judge; cycle edges cut |
| `ownerless_requirements` | Requirements with no target on any owner pair |
| `redundant_after_inference` | Links of a `distinct_owner` pair whose ends got one owner only through inferred edges |
| `interfaces_without_provider` | Interfaces with no `PROVIDED_BY` target |
| `features_named_like_statements` | Features whose name has a modal verb or ends with a period |
| `unsupported_entities_dropped` | Nodes dropped by `supported_by` |
| `relates_fan_out`, `entity_relates_fan_out`, `deployed_to_fan_out` | Sources per number of targets: `RELATES_TO` modules of a requirement, `RELATES_TO` data entities of a requirement, `DEPLOYED_TO` targets of a module |
| `in_scope_disagreements` | Merged nodes whose members gave different `in_scope` values |
| `in_scope_dirty_wins` | Of those, the nodes that took the value of a member grounded in a new or revised document ([build.md](build.md) Conform 2) |
| `unsnapped_quote_nodes` | Canonical nodes per label without a snapped quote span (evidence from id or name lines, or whole fragments) |
| `unsnapped_source_quotes` | Source quotes of canonical nodes that snapped nowhere; n/a for a build from before per-source quotes |
| `provisional_keys_left` | Canonical nodes that kept a provisional identity key |
| `attributed_fragments_dropped` | Attributed fragments, summed over canonical nodes, that hold none of the node's evidence |
| `nodes_missing_source_documents` | Canonical nodes attributed to a document that holds none of their evidence |
| `label_collapses` | Names a node call gave two or more labels (extract-nodes counters); n/a for a build from before the count |
| `derive_fallback_matches` | `derive` references Conform resolved by the whole-word fallback; n/a for a build from before the count |
| `derive_fallback_refused` | `derive` references the whole-word fallback found a hub for but refused, because no evidence fragment of the source names that hub; n/a for a build from before the count |
| `hub_surfaces_per_hub`, `hub_raw_names_per_hub` | Per canonical hub node: distinct hub surfaces / distinct case-folded names of its raw nodes, as `hubs`, `mean`, `max` and `share_over_1`, over every hub (`all`) and per hub label |
| `hub_consolidation_merges` | Merges of the hub consolidation pass (Resolve) |
| `catalog_hubs`, `catalog_aliases` | Hubs of the hub catalog merged from the hub pass, and their aliases ([build.md](build.md) Extract nodes); n/a when no hub pass ran or the build is older |
| `catalog_links_refused`, `catalog_aliases_dropped` | Alias links the catalog merge refused (hub over 4 groups); aliases it dropped (weak, another label's or another hub's name, or claimed by two or more hubs) |
| `catalog_ambiguous` | Catalog hubs of an `update` build matching two or more persisted hubs, left out of the known terms |
| `catalog_batches`, `catalog_calls`, `catalog_failed`, `catalog_reused`, `catalog_truncation_splits` | Counts of a small-model catalog call, which the build no longer makes (the hub pass is the catalog; its calls are the extract-nodes counter `hub_calls`): always n/a |
| `known_terms_mentioned`, `known_terms_nearby` | Per unit, known terms shown to the statement pass because its fragments name them / by nearness, as `units`, `mean` and `max`; n/a when the build stored no known terms |
| `nearby_term_uses_hubs` | Raw hub nodes (not reused) named by a nearby known term of their unit's hub pass; n/a when the hub pass was shown no known terms (every first build) |
| `nearby_term_uses_statements` | Raw statement nodes (not reused) whose name contains a nearby known term of their unit's statement pass, by its name or another name of it; n/a without stored known terms |
| `nearby_term_uses` | Both together; n/a when neither is measured |
| `nearby_term_uses_unsupported`, `nearby_term_uses_hubs_unsupported`, `nearby_term_uses_statements_unsupported` | Of those, the ones whose fragments name neither the term nor another name of it (catalog alias, persisted alias, a name Resolve folded in); a statement with several nearby terms counts once |
| `alias_name_uses` | Raw hub nodes named by another name (catalog or persisted alias) of a known term shown to their unit's hub pass instead of the term; n/a when the hub pass was shown none |
| `hub_merges_by_source` | Hub consolidation merges per source: `block`, `pair` (pair pass), `stored` (a stored `same` decision) ([build.md](build.md) Resolve 3) |
| `pair_candidates_vector`, `pair_candidates_alias` | Hub candidate pairs found by candidate-vector cosine / by a catalog alias (a pair found both ways counts in both) |
| `pair_batches`, `pair_calls`, `pair_over_cap` | Pair-pass batches, their judge calls, and candidate pairs left past the per-build call cap (judged in a later build) |
| `pair_merges`, `alias_merges` | Merges of the pair pass; of those, the ones of alias pairs |
| `unsure_groups` | Hub groups the judge gave `low` confidence: not merged, not stored |
| `decisions_expired` | Stored hub decisions skipped because their evidence hash no longer matches (judged again) |
| `dedup_candidates_rich_only` | Requirement dedup candidates found only by the candidate vectors ([build.md](build.md) Conform 2) |
| `dedup_unsure` | Dedup verdicts of `low` confidence: no merge, not stored |
| `dedup_verdicts_expired` | Stored dedup verdicts skipped because their evidence hash no longer matches (asked again) |
| `dedup_refused_by_guard` | Guard-refused dedup pairs (the build result's `dedup_refused`) per guard |
| `edges_per_requirement`, `requirements_without_edge` | Mean domain edges per requirement; share of requirements with no domain edge. Requirements in these and the next three rows: nodes of owned spine-source labels that the spine `exclude` does not match (`in_scope: no` left out), as for `ownerless_requirements` |
| `cross_document_edges`, `cross_document_share` | Domain edges whose ends share no evidence document, per relation and basis; their share of the edges with documents at both ends; n/a when no edge has documents at both ends |
| `owners_by_source` | Requirements by the basis of their owner edge: `stated`, `implied`, `inferred`, `none` |
| `orphans_best_elsewhere` | Ownerless requirements whose most similar legal owner (exact cosine over `embedding`) is grounded only in other documents |
| `derive_suppressed_by_edge` | `derive` edges Conform dropped because a stated edge of the relation already owns the node ([build.md](build.md) Conform 4) |
| `foreign_edges` | Raw edges with an end from another unit's catalog, per entry path (`mentioned`, `heading`, `near`, `referenced`) and outcome (`kept`, `owner_conflict`, `quarantined:<reason>`, `dropped`) ([build.md](build.md) Conform, Foreign edges) |
| `windows_called`, `windows_reused`, `windows_fallback`, `warm_ups` | Edge windows called, reused from the store, and kept on stored edges after a failed call; edge warm-up calls ([build.md](build.md) Extract edges) |
| `windows_called_by_cause` | Called edge windows per cause (`unrecorded` without one); n/a when the artifact records no windows |
| `items_by_outcome` | Judged items per inference rule and outcome |
| `modified_causes` | Modified nodes per diff cause (`DiffArtifact.modified_causes`, [build.md](build.md)) |
| `oq_answer_judged`, `oq_answered_dropped`, `oq_partly_kept` | Nodes the answered-elsewhere check judged (fresh or stored), dropped as answered, kept as partly answered ([build.md](build.md) Answered elsewhere) |
| `inference_backlog` | Per rule, queued subjects after the build and the queue's age in builds (`age_builds`; Limits and backlog) |
| `inference_limit_hits`, `inference_budget_tokens` | Per rule, the limit that stopped it (`cap`, `first_cap`, `budget`); its counted input tokens |
| `inference_read_edges` | Per rule with `reads`, the read rules' planned edges behind its shown candidates |
| `inference_changed_queued` | Per rule, in a build whose diff changed nothing: subjects with changed inputs queued without a call (`RuleReport.changed_queued`) |
| `inference_items_by_band` | Judged items per rule, confidence band ([0,60), [60,70), [70,80), [80,100] in percent; `none` without a confidence) and outcome (`declined` included) |
| `spine_unreachable_planned` | `spine_unreachable_after` of the inference report, next to the verified `spine_unreachable` |
| `inference_input_share` | Per rule, its `budget_tokens` over the build's input tokens; `all`, the infer step's input over the same total |
| `settlement` | The build result's settlement: settled or not, documents, follow-up builds, queued subjects, reasons ([build.md](build.md) Follow-up builds and settlement) |
| `change_report_nodes`, `change_report_edges` | Change-report nodes per class; edges per change ([build.md](build.md) Change report) |

The hub-name and known-term metrics (`hub_surfaces_per_hub` to `alias_name_uses`) are measured on the
stored extract-nodes, resolve and plan artifacts, so an older build gives its "before" values; those it
did not store are n/a.

The identity metrics (`hub_merges_by_source` to `dedup_refused_by_guard`) come from the stored resolve
and conform artifacts (`dedup_refused_by_guard` from the build result when it has it); a build whose
artifacts lack them gives n/a.

The edge metrics (`edges_per_requirement` to `oq_partly_kept`) come from the persisted graph
(`cross_document_edges` and `owners_by_source` after inference) and the stored extract-edges, conform,
diff and inference artifacts; a build whose artifacts lack them gives n/a. Without the verification
values, the artifacts give `cross_document_edges` and `owners_by_source` after inference only when the
build carried no edge over from the graph before it (a `first` build, or a diff keeping no earlier
inferred edge); otherwise they are measured before inference and labelled so. The test-corpus report also
writes samples: nearness edges, `INF-XDOC-01` items, answered-elsewhere drops, `PART_OF` and
`PROVIDED_BY` edges by basis, judged items per rule and confidence band, and change-report nodes per
class.

The inference and operation metrics (`inference_backlog` to `change_report_edges`) come from the
stored infer artifact, the build result and the gateway usage per prompt; a build whose artifacts lack
them gives n/a.

The grounding metrics (`unsnapped_quote_nodes` to `nodes_missing_source_documents`) count each
canonical node grounded on its own, before Conform merges any. They come from the conform artifact's counters; for an older build they are measured on
its stored plan, resolve and extract-edges artifacts, reading its `quote` attribute as the one source
quote of all the node's fragments.

Loop: verify, repair, verify again; at most 2 repair rounds. A round is not applied, and the loop
ends, when it would delete more than max(1, 5%) of the project's spec nodes, or more than max(1, 5%)
of its domain edges (a sign of a bug, not of bad data). Edges deleted with their node count toward the
edge limit. The run is `ok` when no blocking problem is left; a `fragment_state` problem stays until
its follow-up build, so a run that requests one is not `ok` and the build result is not `settled`
(`run_build` starts that build itself, [build.md](build.md) Follow-up builds and settlement). The
verification run (checks, metrics, repairs, `ok`) is stored as a build artifact and summarised in the
build result. Problems and repairs name an edge as `<source spec_id>|<RELATION>|<target spec_id>`,
never by edge key; nodes and inferred edges that repairs delete are listed as removed in the build's
spec changes ([build.md](build.md)).

`verify_project` ([api.md](api.md)) runs the checks read-only, without repairs, so a host or a later submodule
can check a graph at any time. It lists problems and metrics only, never follow-ups.
