---
type: DataModel
title: "Graph gap data model"
description: "Gap and Clarification nodes and relations, identity, signatures, statuses, the relational store, per-store mapping and invariants"
status: stable
tags: [graph, gap]
---
# data-model.md: gap nodes and relations, identity, signatures, statuses, the relational store and invariants

## Where data lives

A fact goes into the graph only when graph reads need it: traversal, trace, node views, briefs, the plan or
an investigating agent. Everything else is a row in the host's relational store (PostgreSQL), next to the
documents the corpus reader already reads from there.

| Fact | Store | Why there |
|---|---|---|
| A gap and what it is about (`Gap`, `ABOUT`, `IMPACTS`, `CITES`) | Graph | Agents, trace, briefs and the plan see gaps next to the nodes; relations follow rekeyed and merged nodes |
| An answer and where its text went (`Clarification`, `ANSWERS`, `PLACED_IN`, `GROUNDED_IN`) | Graph | "Why does this requirement exist?" is one walk: requirement, fragment, clarification, gap |
| History between gaps and answers (`FOLLOWS_UP`, `REPLACES`) | Graph | Read with the gap |
| Options offered for a gap | Relational | Only the inbox reads them; the chosen one is copied onto the Clarification |
| Amendments | Relational | Read by the corpus reader beside the documents |
| Runs, seeds, sites, verdict cache, section relevance cache | Relational | Operational data nobody traverses |
| Suppressions and noise rules | Relational | Checked by detectors by key |
| Gap event log | Relational | Audit of every transition, including gaps that left the graph |
| Who resolves a gap (its assignee) | Relational | Only the inbox and the agent's list read it; assigning does not change the gap, so an answer form that is open does not go stale |
| The applied answer as spec content | Graph, through the build | Ordinary spec text, extracted into ordinary spec nodes with evidence |

## Graph model

The canonical model is the Neo4j model of the library ([../data-model.md](../data-model.md)); the per-store
mapping of `graph.core` applies unchanged. Every gap node carries `group_id` (the project id) like every
other node.

### Nodes

| Label | Key | Properties |
|---|---|---|
| `Gap` | `gap_id` (`GAP::<12 hex>`) | `kind`, `severity`, `status`, `origin`, `route`, `resolution` (when resolved), `title`, `question`, `why`, `detector` (code, or `development`, `person`), `signature`, `seed_signatures` (the signatures of the detector seeds of the gap's detector whose sites recorded or re-checked it; what a dismissal suppresses besides `signature`), `blocking` (`yes` when a development gap holds the reporting agent's claimed story), `story_id` (the story a development gap was reported from), `aliases` (other ids the gap answers to: the `FND::` id of a migrated first-slice finding; `get_gap` and the release of holds resolve them), `run_id`, `created_by`, `created_at`, `updated_at`, `resolved_at` |
| `Clarification` | `clr_id` (`CLR::<12 hex>`) | `mode`, `placement`, `status`, `option_label`, `text`, `answered_by` (a user id, or `ai`), `answered_at`, `apply_requested_at` (when its answers were sent to an update build), `applied_build_id`, `reconcile_state` (hash of its amendments' ids, statuses, anchor hashes and revisions as the close step last saw them), `basis` (JSON: quoted lines behind an AI or pointer answer), `confidence` (AI and pointer answers: the second check's), `reverted_at`, `reverted_by` (set by `revert_clarification`) |

Vocabularies are those of [gap-ontology.yaml](gap-ontology.yaml). `severity` `info` is not a graph value:
an info finding is a run note in the relational store. A gap written while runs held new questions back may still
carry `held_back`; nothing reads it (every new question shows, FR-GRTE-06).

### Relations

| Relation | From, to | Properties | Meaning |
|---|---|---|---|
| `ABOUT` | `Gap`, spec node | `added_by_run` | The gap is in this node |
| `IMPACTS` | `Gap`, spec node | `added_by_run` | An answer will change this node |
| `CITES` | `Gap`, `Fragment` | `quote`, `line_start`, `line_end`, `quote_hash` | The witness line |
| `FOLLOWS_UP` | `Gap`, `Gap` | `reason` (`not_fixed`, `orphaned`, `conflict`, `reverted`) | The source gap continues the target |
| `ANSWERS` | `Clarification`, `Gap` | | The clarification answers the gap |
| `PLACED_IN` | `Clarification`, `Document` | | The document the text goes into |
| `GROUNDED_IN` | `Clarification`, `Fragment` | `build_id` | The fragment that holds the text after a build, or the answering fragment of a pointer |
| `REPLACES` | `Clarification`, `Clarification` | | A newer answer of the same gap |

A spec node never gets a gap property, and no spec relation is written. Graph reads of other submodules
reach gaps only by walking these relations.

### What a gap looks like in the graph

```
(:Document {orders-spec.md})-[:CONTAINS]->(:Fragment {Place order})
(:OpenQuestion QST::CANCEL_PAID_ORDER)-[:REFERENCED_BY]->(:Fragment {Place order})
(:OpenQuestion QST::CANCEL_PAID_ORDER)-[:RELATES_TO]-(:FunctionalRequirement REQ::FR::PLACE_ORDER)
(:Gap GAP::7C2A91E04B3D {kind: open_question, severity: major, status: open})
   -[:ABOUT]->(:OpenQuestion QST::CANCEL_PAID_ORDER)
   -[:ABOUT]->(:FunctionalRequirement REQ::FR::PLACE_ORDER)
   -[:IMPACTS]->(:Interface IFC::PAYMENTS_REFUND)
   -[:CITES {quote: "Can a customer cancel a paid order?"}]->(:Fragment {Place order})
(:Task TASK::ORDERS::LOGIC_PLACE_ORDER)-[:IMPLEMENTS]->(:FunctionalRequirement REQ::FR::PLACE_ORDER)
```

After a person answers and the update build ran:

```
(:Clarification CLR::4E19A0C2B771 {mode: option, status: applied})
   -[:ANSWERS]->(:Gap GAP::7C2A91E04B3D {status: resolved, resolution: answered})
   -[:PLACED_IN]->(:Document {orders-spec.md})
   -[:GROUNDED_IN]->(:Fragment {Place order, amended})
(:FunctionalRequirement REQ::FR::CANCEL_PAID_ORDER)-[:REFERENCED_BY]->(:Fragment {Place order, amended})
```

The question node is gone (the build removed it), and with it the `ABOUT` relation to it. The `CITES`
relation is removed by the close check when its fragment no longer holds the quote; the quote stays in the
gap event log.

## Identity

| Item | Rule |
|---|---|
| Signature | SHA-256 of the canonical JSON of: detector code, sorted `ABOUT` spec ids, sorted witness entries (fragment id and normalised quote, or the graph fact as text). For a development gap: `development`, the reporting task id, sorted spec ids and the normalised question. For a gap a person raised: `person`, sorted spec ids and the normalised question |
| `gap_id` | `GAP::` and the first 12 hex digits of the signature. A collision with a different signature appends the next 4 hex digits |
| `clr_id` | `CLR::` and the first 12 hex digits of SHA-256 of the gap id, the answer text and the answered-at instant |
| Normalisation | A quote (and a graph fact) is normalised by making quote marks plain, collapsing whitespace to one space and trimming it; a development gap's question is also case-folded. Canonical JSON has sorted keys and no spaces |
| Same gap again | A report whose signature equals an existing gap's is that gap; a report whose question is similar (embedding cosine at least 0.9) on an overlapping `ABOUT` set is a duplicate ([investigation.md](investigation.md) "Checks") |

When a build rekeys or merges a spec node, the next gap run moves `ABOUT` and `IMPACTS` to the new key and
recomputes nothing else; the signature keeps the old ids, and the alias table of the build maps them.

A gap's signature is computed from its checked report (the investigator may name more or fewer `ABOUT` nodes than
the detector did), while the sites step compares seed signatures (computed from each seed's detector, `ABOUT`
candidates and witness). So that a dismissed gap stays quiet, the gap keeps the signatures of the seeds of its
detector that formed or re-checked it (`seed_signatures`) and a dismissal suppresses them too. A follow-up gap's
signature is SHA-256 of the canonical JSON of the old gap's id, its signature and the follow-up reason.

## Statuses

### Gap

| Status | In graph | Meaning | Next |
|---|---|---|---|
| `open` | yes | Waiting for an answer | `answered`, dismissed, obsolete |
| `answered` | yes | A pending Clarification exists | `applying`, back to `open` when the answer is withdrawn |
| `applying` | yes | The text is in the corpus; an update build is queued or running | `resolved`, or `resolved` with a follow-up gap |
| `resolved` | yes | The close check passed, or it closed in favour of a follow-up | final |
| dismissed | no | A person said it is not a gap; the node and its relations are deleted, the signature becomes a suppression | final |
| obsolete | no | Everything it was about left the graph, or its detector stopped firing, before an answer; deleted | final |

### Clarification

| Status | Meaning |
|---|---|
| `pending` | Recorded; not yet in the corpus |
| `applied` | In the corpus and grounded by a build (`GROUNDED_IN` set) |
| `absorbed` | The owner adopted the text into the source; the amendment retired ([reconcile.md](reconcile.md)) |
| `conflict` | A newer upload says something else; the amendment is suspended; a follow-up gap asks |
| `orphaned` | The anchor and its section are gone; a follow-up gap asks where the answer goes |
| `reverted` | A person withdrew it; the next build removes its text; the gap reopens as a follow-up |
| `superseded` | A newer clarification of the same gap replaced it |

Transitions and who triggers them are in [lifecycle.md](lifecycle.md).

## Relational store

The tables belong to the host; `graph.gap` reaches them through the ports `GapRecords` and `Amendments`
([api.md](api.md) "Ports"). Every table has `project_id`, and every query filters on it.

| Table | Columns | Notes |
|---|---|---|
| `gap_option` | `gap_id`, `rank`, `label`, `detail`, `recommended`, `edits` (JSON), `basis` (JSON), `created_run` | Exactly one `recommended` per gap; 2 to 4 rows |
| `corpus_amendment` | `amendment_id`, `clr_id`, `lineage`, `heading_path` (JSON), `anchor_text`, `anchor_hash`, `operation` (`insert_after`, `replace`, `append_section`), `text`, `status` (`pending`, `active`, `retired`, `suspended`, `orphaned`, `reverted`, `undecided`), `last_revision_id`, `created_at`, `updated_at` | One row per edit; one clarification may have several |
| `gap_run` | `run_id`, `build_id`, `mode` (`update`, `full`, `site`), `status`, `started_at`, `finished_at`, `counts` (JSON), `cost` (JSON), `rules_version`, `prompt_versions` (JSON) | One row per run |
| `gap_seed` | `run_id`, `detector`, `spec_ids` (JSON), `witness` (JSON), `strength`, `relevance`, `corroborated`, `site_id` | Every detector hit |
| `gap_site` | `site_id`, `run_id`, `input_hash`, `rank_score`, `outcome` (`gap`, `not_a_gap`, `not_spec`, `duplicate`, `skipped`, `deferred`), `gap_id` | `input_hash` keys the verdict cache |
| `gap_verdict` | `input_hash`, `report` (JSON), `model`, `prompt_version`, `created_at` | Reused when a site's inputs did not change |
| `gap_section_relevance` | `lineage`, `heading_path`, `text_hash`, `relevance` (`spec`, `tracking`, `mixed`), `signals` (JSON), `by` (`rules`, `model`, `person`) | Cache of the relevance filter |
| `gap_noise_rule` | `rule_id`, `scope` (`document`, `section`, `pattern`), `value`, `reason`, `created_by`, `created_at` | Sections or patterns a person marked "not spec" |
| `gap_suppression` | `signature`, `gap_id`, `question`, `reason`, `dismissed_by`, `dismissed_at` | A dismissed signature stays quiet until it changes; one dismissal writes one row for the gap's signature and one for each of its `seed_signatures` |
| `gap_note` | `run_id`, `detector`, `spec_ids` (JSON), `text` | Info findings: nothing to decide; also a coding agent's `note` finding (`run_id` null, detector `development`), a development gap a run closed as not spec (its question as text) and a migrated first-slice note |
| `gap_event` | `gap_id`, `clr_id`, `at`, `actor`, `event`, `detail` (JSON) | Every transition, quote and reconcile outcome (event `reconciled`, actor `reconcile`), and every change of assignee (`assigned`, `unassigned`) |
| `gap_assignment` | `gap_id`, `user_id`, `assigned_by`, `assigned_at` | The member who resolves a gap: at most one row per gap ([answers.md](answers.md) "Who answers"); deleted when the gap is dismissed or obsolete, and with the membership when the member leaves or is removed |

Keys (each starts with `project_id`, the project's id in the application database, a foreign key to
`projects`): `gap_option` (`gap_id`, `rank`), with a unique index keeping one `recommended` row per gap and `rank`
1 to 4; `corpus_amendment` (`amendment_id`); `gap_run` (`run_id`); `gap_site` (`run_id`, `site_id`); `gap_verdict`
(`input_hash`); `gap_section_relevance` (`lineage`, `heading_path`, `text_hash`); `gap_noise_rule` (`rule_id`);
`gap_suppression` (`signature`); `gap_assignment` (`gap_id`), with a foreign key (`project_id`, `user_id`) to
`project_members` that deletes the row with the membership. `gap_seed`, `gap_note` and `gap_event` are append-only and keyed by a generated
`id`. JSON columns are `jsonb`; enumerated columns are text with a check. Every table has the row-level security
policy `agent_project_scope` and no grant to the agent role: only the application role reads and writes them.

## Constraints and indexes

| Object | Definition |
|---|---|
| `graph_gap_id` | Unique (`group_id`, `gap_id`) on `Gap` |
| `graph_clarification_id` | Unique (`group_id`, `clr_id`) on `Clarification` |
| `graph_gap_status` | Index on `Gap` (`group_id`, `status`) |
| `graph_gap_signature` | Index on `Gap` (`group_id`, `signature`) |
| `graph_foreign_uuid_gap`, `graph_foreign_uuid_clarification` | Unique (`group_id`, `uuid`) on `Gap` and `Clarification`: the key every foreign node of the core owned writer needs |
| `graph_edge_key_<relation>` | Index on (`group_id`, `edge_key`) of each of the eight gap relations: the key of an owned relationship write |

`ensure_gap_schema` creates them and is idempotent ([api.md](api.md) "Setup").

## Invariants

| # | Invariant |
|---|---|
| GI1 | Every `Gap` in the graph has at least one `ABOUT` relation or one `CITES` relation, and its status is one of `open`, `answered`, `applying`, `resolved`. |
| GI2 | Every `Gap` with status `answered` or `applying` has exactly one `Clarification` with status `pending` or `applied` that `ANSWERS` it. |
| GI3 | Every `Clarification` `ANSWERS` exactly one `Gap`. |
| GI4 | A `Clarification` with status `applied` or `absorbed` has a `GROUNDED_IN` relation; one with placement `amend` or `document` has a `PLACED_IN` relation. |
| GI5 | Every gap with route `ask` has 2 to 4 option rows with exactly one recommended; a development gap may have none until its investigation finishes. |
| GI6 | No gap relation ends at a node of another project, and no spec node, spec relation, document or fragment property is written by `graph.gap`. |
| GI7 | Every active or pending amendment belongs to a clarification with status `pending` or `applied`; a `pending` amendment's clarification is `pending`. |
