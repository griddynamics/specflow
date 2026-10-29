---
type: Design
title: "Light inference and verification"
description: "Light inference and verification with repair, run after every persist"
status: stable
tags: [graph]
---
# analysis.md: light inference and verification with repair, run after every persist

## Light inference

Adds domain edges between existing spec nodes when the source texts state or directly imply them.
It never creates, merges or relabels nodes.

| Rule | Behaviour |
|---|---|
| When | Once per build, after persist; off when `BuildSettings.inference` is false |
| Input view | Only `stated` and `implied` edges (an edge without `basis` is read as `stated`), so earlier inferred edges never feed new candidates |
| Subjects and candidates | From the ontology's inference rules and templates ([ontology.md](ontology.md)); a subject has at least one candidate |
| Judge | One structured call per subject through the core judge runner, with the subject's and candidates' names, statements and evidence fragment texts. A `realization` payload shows at most 12 members and 12 candidates; a cut subject is counted as `truncated` |
| Judge memory | An outcome is stored per rule, subject and input hash (payload, prompt ref, judge model) in the `ExtractionStore` (kind `decisions`). A subject with unchanged inputs reuses it without a call and does not count against the cap. A failed judgement is not stored |
| Acceptance | `accepted`: the verdict names at least one candidate, its confidence is at least the ontology `floor` (0.7) and every cited quote is found in the cited fragment. `declined`: a valid "no link" answer. `invalid`: it names an id or pair it was not given. Else `rejected`. An unsure judge writes nothing. A subject that is judged again and gets no accepted link loses its earlier inferred edges |
| Bound | At most the ontology `cap` (50) subjects judged per rule per build, from those without a stored outcome: subjects whose inputs changed first, then never-judged ones, each in `spec_id` order; the rest keep their current inferred edges and are reported as deferred. A build whose diff changed nothing judges no subject: remembered judgements are reused and the rest stay deferred |
| Write | The new set of inferred edges replaces the old set in one transaction; deferred subjects and subjects whose judgement failed keep theirs. An edge already present as `stated` or `implied` is not written. |
| Edge | `basis = inferred`, `inference_rule`, `confidence`, `evidence`, `source_fragment_ids`, `judge_model`, `inferred_at` (the write time) ([data-model.md](data-model.md)) |
| Failure | Only judge failures and invalid verdicts are tolerated: a failed judgement is counted and its subject keeps its edges; a non-fatal model error that stops a rule is reported in that rule's `error_code` and leaves its edges unchanged. Credential, model, infrastructure and conformance errors fail the build, and the previous inferred edges stay (one swap transaction) |

Shipped rules: `INF-INT-01` links the modules of an integration requirement with `INTEGRATES_WITH`.
Its modules are those it reaches by `PART_OF` or `RELATES_TO`, plus the modules mentioned in its
evidence fragments. The judge gives one direction per pair, from the module that calls to the module
called; a verdict linking a pair both ways is invalid for that pair, and a pair already linked in
either direction is skipped. `INF-CAP-01` adds `REALIZED_BY` from a capability to the modules its
grouped features sit in. `INF-CONT-01` attaches a requirement that reaches no module to one of its 3
most similar project features or modules (exact cosine over `embedding`) with `PART_OF`; a requirement
that already has a target on an `exclusive` pair of `PART_OF` is not a subject.

The report lists, per rule: subjects, judged, accepted, declined, rejected, invalid, failed,
deferred, reused, truncated, edges added, removed and updated, and `error_code` when the rule failed;
integration requirements with 1 or more than 4 modules; orphan requirements with no candidate. Edges
are named `<source spec_id>|<RELATION>|<target spec_id>`, never by edge key. Later submodules may
read these lists.

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

| Metric (reported, never repaired) | Counts |
|---|---|
| `spine_unreachable` | Requirements that reach no module by a spine path, before and after inference |
| `exclusive_conflicts` | Every extra target on an `exclusive` pair (per source, relation and target label, each target past the first), as Conform counts them |
| `label_disagreement` | Nodes whose computed label differs from their label |
| `dropped_edges` | Edges dropped by legalisation, per relation |
| `fragments_without_nodes` | Active fragments no spec node is grounded in |

Loop: verify, repair, verify again; at most 2 repair rounds. A round is not applied, and the loop
ends, when it would delete more than max(1, 5%) of the project's spec nodes, or more than max(1, 5%)
of its domain edges (a sign of a bug, not of bad data). Edges deleted with their node count toward the
edge limit. The run is `ok` when no blocking problem is left; a `fragment_state` problem stays until
its follow-up build, so a run that requests one is not `ok` and the build result is not `settled`. The
verification run (checks, metrics, repairs, `ok`) is stored as a build artifact and summarised in the
build result. Problems and repairs name an edge as `<source spec_id>|<RELATION>|<target spec_id>`,
never by edge key; nodes and inferred edges that repairs delete are listed as removed in the build's
spec changes ([build.md](build.md)).

`verify_project` ([api.md](api.md)) runs the checks read-only, without repairs, so a host or a later submodule
can check a graph at any time. It lists problems and metrics only, never follow-ups.
