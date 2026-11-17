---
type: Design
title: "Graph gap detection"
description: "Gap kinds, run modes and scope, the relevance filter, the detector catalogue, seeds and corroboration, sites, stated open questions, limits, the rules file and run metrics"
status: draft
tags: [graph, gap]
---
# detection.md: gap kinds, run modes and scope, relevance filter, detector catalogue, seeds, sites and limits

Detection is the first half of a gap run: the steps `scope`, `relevance`, `detect` and `sites` of
`run_gap` ([architecture.md](architecture.md) "Run steps"). It uses no model except the batched relevance
call for uncertain sections. Its output is a ranked list of **sites**, each the entry point of one
investigation ([investigation.md](investigation.md)).

## Gap kinds

A gap is anything in the spec that would force a builder to guess. Every gap has exactly one kind, a value
of the `gap_kind` vocabulary ([gap-ontology.yaml](gap-ontology.yaml)). The kind decides which detectors
find it, what a good answer does to the text, and whether AI may answer it ([answers.md](answers.md)
"Routing").

| Kind | Definition | Example | Typical detectors | A good answer | May AI answer |
|---|---|---|---|---|---|
| `missing` | The spec needs a fact no text gives: an owner, a failure behaviour, a data store, a quality target, the meaning of a term | "Payments are captured through the provider", and nothing says what happens when the capture fails | MS-PROVIDER, MS-STORE, MS-CONTRACT, MS-OWNER, MS-CRITERIA, MS-COVERAGE, MS-REF | Inserts one or more statements where the topic lives | Yes, for minor gaps when the answer follows from stated text |
| `ambiguous` | A statement allows more than one reading, or cannot be tested: vague terms, no measure, unclear reference, text that only implies | "The admin panel should be fast." | AM-VAGUE, AM-MEASURE, AM-IMPLIED | Replaces the statement with a precise one | Only when another stated line gives the measure |
| `contradiction` | Two statements about the same thing cannot both hold: different values, a negation, must against may | orders-spec: "Only orders over 1,000 EUR are held for manual review"; payments-spec: "Every order over 500 EUR is held for manual review" | CT-VALUE, CT-CROSS, CT-RULE | One side wins: the losing line is replaced, often in another document | Never |
| `inconsistency` | Parts that should fit do not, although no single pair of lines is false: one thing under two names, a scope mismatch, a dependency cycle, an interface described with different data | "Customer" in Orders and "Client" in Billing, both with an email and an address | IN-TERM, IN-SCOPE, IN-CYCLE, IN-PAYLOAD | Aligns the parts: renames, merges or separates, often in several documents | Only naming alignment when the meaning is clearly the same |
| `open_question` | The text states a question or a placeholder and leaves it open | "Can a customer cancel a paid order?" | OQ-STATED, OQ-TBD | Replaces the question with its answer, so the next build has no open question | No; a question the text already answers gets a pointer ("Stated open questions") |
| `assumption` | The text marks something as an assumption nobody confirmed | "We assume every customer has an email address." | AS-FLAG | Confirm: becomes a plain statement; reject: replaced | Never |

A gap can touch many parts of the graph: a contradiction has at least two sides, often in different
documents, and a missing contract sits between two modules. A gap is linked with `ABOUT` to every node the
problem is in and with `IMPACTS` to the nodes an answer would also change. Its reach (stories, tasks, other
modules) is not stored; it is computed with trace when it is read.

## Run modes and scope

| Mode | When | Detectors | Investigation |
|---|---|---|---|
| `update` | The host runs it after every build that returns, before the plan run | Over the whole graph | Only new sites and sites whose input hash changed, up to `limits.sites_update_run` |
| `full` | On demand (a person, or after a rules, prompt or ontology change) | Over the whole graph | Every site whose verdict is not cached, up to `limits.sites_full_run` |
| `site` | At once, for one development gap or one gap a person raised ([lifecycle.md](lifecycle.md) "Development gaps") | None | Only the sites named in `site_ids` |

`run_gap(ctx, *, mode, site_ids=())` takes the mode from the host; the library never picks it. A run with
mode `update` and no persisted gap run behaves as `full`.

**Scope** (step `scope`) reads `GapContext.changes`: the build change reports of every build since the last
gap run, in build order, as the gap-side model `GapChangeReport`. Their classes decide what is new for the
investigation, never what the detectors look at:

| Class | Effect on detection |
|---|---|
| `added` | Its nodes are new; any site containing them is new |
| `removed` | `ABOUT` and `IMPACTS` relations to it went away with the node; the close check decides about the gaps ([lifecycle.md](lifecycle.md) "Close check") |
| `merged`, `rekeyed` | `ABOUT` and `IMPACTS` move to the surviving key before detection; the signature keeps the old ids |
| `carried_over` | Unchanged; cached verdicts stay valid |
| `changed_meaning`, `changed_links` | Its sites get a new input hash and are investigated again |
| `changed_evidence` | Only the witness quotes are checked again; the input hash includes evidence, so the site is investigated again only if a witness quote no longer snaps |

Detectors are cheap queries, so they always run over the whole graph: a gap may appear in an unchanged part
because a node elsewhere changed (a provider removed, a new reference that does not resolve). Agents are
expensive, so they only see new or changed sites.

## Relevance filter

Structural signals alone are noisy. Real corpora hold many nodes that are isolated or have no owner because
they are not spec at all: release names, sprint scopes, ticket references, roadmap items. The step
`relevance` classifies every **section** (a fragment's heading path within its document lineage) as one of:

| Relevance | Meaning | Effect |
|---|---|---|
| `spec` | Describes the system | Every detector reads it |
| `tracking` | Describes the project: releases, sprints, tickets, status, meetings | Structural detectors skip its nodes; text detectors still read it |
| `mixed` | Tracking text that also states obligations | Structural detectors read only its nodes whose text has obligation wording; their seeds are weak |

Classification, in order, stops at the first answer:
1. **Noise rules.** A `gap_noise_rule` that matches the document, the section or a pattern decides
   `tracking` (by `person`). A `document` rule's value is the lineage or the document path; a `section`
   rule's value is the lineage and the heading path joined by ` › ` (`upload:roadmap › Shop roadmap › Release
   plan`) and also covers the sections below it; a `pattern` rule is a regular expression searched in the
   document path, the document title and the heading path. A noise-rule decision is not cached, so removing
   the rule ends it.
2. **Cache.** A `gap_section_relevance` row with the same lineage, heading path and text hash is reused.
3. **Rules.** From `relevance` in the rules file: a title word of `tracking_titles` in the heading path or the
   document title; hits of `tracking_patterns` (ticket key, version, release name, quarter, date) in the
   node names of the section; the share of isolated nodes (no spec relation except `PART_OF` to a document
   chapter) at least `isolated_share`; and hits of `obligation_cues`. Tracking signals without obligation
   cues decide `tracking`; no tracking signal decides `spec`; tracking signals with obligation cues are
   uncertain.
4. **Model.** When `classify_with_model` is true, every uncertain section of the run goes into one batched
   structured call (gateway role `gap_relevance`, at most 40 sections per call), each section as an untrusted
   data block with its heading path and at most 1,500 characters. The answer is one relevance per section
   with the signal it relied on. A failed call (or a section the answer skips) leaves the sections `mixed`
   without caching them, so the next run asks again. When `classify_with_model` is false, uncertain sections
   are `mixed` by `rules` and cached.

The result is written to `gap_section_relevance` with `by` = `rules`, `model` or `person`.

**Text detectors ignore the filter.** Contradictions, stated questions, placeholders, vague terms and
dangling references are read in every section, because a real rule written inside a release plan is still
a rule. Only the structural detectors (family `structure`) skip `tracking` nodes.

**Noise report.** Every run writes, per section judged `tracking` or `mixed`: lineage, heading path,
relevance, `by`, the signals, and the number of structural seeds skipped. It is returned in
`GapRunResult.noise` and served by the host ([api.md](api.md)). A person can mark a section or a document
"not spec" from the report, which writes a `gap_noise_rule`, or mark it "spec", which writes a
`gap_section_relevance` row with `by` = `person` that rules and models never override.

**Feedback from investigation.** A report with verdict `not_spec` ([investigation.md](investigation.md)
"Report") marks the site's section `mixed` (when it was `spec`) in the cache and adds its node to the noise
report; it never writes a noise rule by itself.

The filter never changes the build or the graph.

## Detector catalogue

A detector is a deterministic check with no model call. It reads the persisted graph through the core
reader and the fragments' text, and returns seeds. Every spec-side name a detector uses (types, relations,
attributes, values) comes from its entry in the rules file ([gap-rules.yaml](gap-rules.yaml)); the codes are
`graph.gap`'s own literals. Slice 1 detectors ship first; slice 2 detectors ship after their precision is
measured on the test corpus.

| Code | Kind | Strength | Rule | Witness | Slice |
|---|---|---|---|---|---|
| MS-OWNER | `missing` | weak | `no_spine_owner`: a node of the `types` groups that reaches no internal module along the spine after inference, with `in_scope` not `no` (spine roots matching `exclude`, `external` `yes`, are not internal) | Graph fact and the node's evidence line | 1 |
| MS-PROVIDER | `missing` | strong | `missing_relation`: a node of `types` with no outgoing `relation` (`PROVIDED_BY`) | Graph fact | 1 |
| MS-STORE | `missing` | strong | `missing_relation`: a node of `types` with no outgoing `relation` (`STORED_IN`) | Graph fact | 1 |
| MS-CONTRACT | `missing` | strong | `integration_without_interface`: two internal modules (of `types`, not matching `exclude`) joined by one of `relations` (`INTEGRATES_WITH`, or a requirement of `members` owned by one, along spine paths without those relations, that `RELATES_TO` the other) with no node of `interfaces` that `provided_by` one and `consumed_by` the other | Graph fact and the naming line | 1 |
| MS-CRITERIA | `missing` | weak | `empty_attribute`: a node of `types` whose `attribute` (`acceptance_criteria`) is empty | Graph fact | 1 |
| MS-COVERAGE | `missing` | strong | `checklist`: a topic of `topics` (security, performance, availability, observability, data protection, accessibility; cues from `checklist`) with no node of `types` outside `tracking` sections whose name or text contains one of its cues; one seed per missing topic, about the project | Absence over the declared checklist | 1 |
| MS-REF | `missing` | strong | `dangling_reference`: a fragment line citing an explicit spec id, a numbered section or a document name that resolves to no node, heading or document of the project | The line | 1 |
| MS-CONSUMER | `missing` | weak | `missing_relation`: a node of `types` with no outgoing `relation` (`CONSUMED_BY`) and no integration requirement `RELATES_TO` it | Graph fact | 2 |
| MS-ERROR | `missing` | weak | `checklist_topic`: a node of `types` with no related node matching the cues of `topic` (`failure`) | Absence | 2 |
| MS-LIFECYCLE | `missing` | weak | `checklist_topic`: a node of `types` with no related node matching the cues of `topic` (`lifecycle`) | Absence | 2 |
| AM-VAGUE | `ambiguous` | strong | `vague_terms`: a node of `types` whose text holds a term of `vague_terms` and no number with a unit in the same sentence | The line | 1 |
| AM-MEASURE | `ambiguous` | strong | `no_number`: a node of `types` whose text holds no number | The line | 1 |
| AM-IMPLIED | `ambiguous` | weak | `attribute_value`: a node of `types` whose `attribute` (`source_status`) is `value` (`implied`) | The line | 1 |
| CT-VALUE | `contradiction` | strong | `refused_merge`: a pair of nodes of `types` the build kept apart although near-duplicate, because a number, a negation or the modality differs (the build's dedup refusals) | Both lines | 1 |
| CT-CROSS | `contradiction` | weak | `similar_across_documents`: pairs of `types` from different documents with similarity at least 0.85 and no relation between them | Both lines | 2 |
| CT-RULE | `contradiction` | weak | `overlapping_conditions`: business rules about the same data entity or feature whose conditions overlap with different outcomes | Both lines | 2 |
| IN-SCOPE | `inconsistency` | strong | `in_scope_uses_out_of_scope`: a node of `types` whose `attribute` (`in_scope`) is not `value` (`no`) that uses (one edge of impact role `uses`, or `used_by` read backwards) a node whose `attribute` is `value` | Graph fact | 1 |
| IN-CYCLE | `inconsistency` | strong | `cycle`: a cycle of `relation` (`DEPENDS_ON`) among nodes of `types`, including cycles the build cut | Graph fact and the relations' evidence lines | 1 |
| IN-TERM | `inconsistency` | weak | `near_names`: two nodes of `types` with near-identical names (normalised edit distance at most 0.2, or embedding similarity at least 0.9) and different labels or owners | Both lines | 2 |
| IN-PAYLOAD | `inconsistency` | weak | `carries_differ`: one node of `types` whose `relation` (`CARRIES`) targets differ between the documents that describe it | Both lines | 2 |
| OQ-STATED | `open_question` | strong | `present`: every node of `types` (`open_question`) still in the graph | The question line | 1 |
| OQ-TBD | `open_question` | strong | `placeholder_markers`: a fragment line holding a marker of `placeholder_markers` whose evidence range grounds no node of `types` (`open_question`) | The line | 1 |
| AS-FLAG | `assumption` | strong | `attribute_value`: a node of `types` whose `attribute` (`is_assumption`) is `value` (`yes`) | The line | 1 |
| DV-AGENT | any | strong | Not a detector query: a coding agent's report through MCP becomes a seed of its own site ([lifecycle.md](lifecycle.md) "Development gaps") | The agent's quoted lines and spec ids | 5 |
| DV-PLAN | any | weak | A plan review issue that names a requirement no task can build | Plan artifact | later |
| AG-FLOW | `missing` | weak | Agent hypothesis: a flow across modules misses a step; the agent must return a re-checked path and quotes | Path and quotes | later |

Rule parameters not named above are fixed by the rule: `no_spine_owner` uses the spine of the merged
ontology (its roots, paths and `exclude`); `refused_merge` reads the refusals the build persisted with the
build result, and `cycle` also reads the edges the build cut out of a cycle (quarantined `cycle_broken`): the
host converts both from the build result into `GapContext.build` (`BuildFacts`), so `graph.gap` never imports
`graph.build`; `dangling_reference` resolves explicit ids (the id families of the merged ontology), numbered
sections (the word section or chapter, or the section sign, before a dotted number, against headings that
start with that number) and document names (against document paths and titles).

Detectors read one in-memory snapshot of the persisted graph (spec nodes, domain edges with inferred ones,
documents, active fragments), read once per run through the core reader, so detection time grows with the
graph and not with the number of detector questions (NFR-GPERF-01). Detector slices: the run executes the
detectors of slice 1; a slice 2 entry enabled by an override whose rule has no implementation yet is reported
in `detector_failures` as `detector_not_shipped`.

A detector returns, per hit, a **seed**: detector code, kind, strength, the spec ids it is about (the
`ABOUT` candidates), optional context ids (`IMPACTS` candidates), and a witness. A witness is either a quote
(fragment id, line range, verbatim text) or a graph fact (a short statement that the same detector proves
again when it runs). A detector that fails (a query error) is skipped, counted in the run metrics, and never
fails the run.

## Stated open questions

When a spec states a question or a placeholder, the build already extracts it as an `OpenQuestion` node
(`QST::`) and links it with `RELATES_TO` to the node it asks about. The build's answered-elsewhere check drops
a question another fragment answers, so every `OpenQuestion` still in the graph is open as far as the text
shows. These are the most direct seeds the gap module gets:
1. OQ-STATED fires on every `OpenQuestion` node, with the question line as witness and the nodes it
   `RELATES_TO` as `ABOUT` candidates (with the question itself).
2. The seed is strong: no corroboration, and the relevance filter does not apply.
3. Questions that relate to the same node merge into one site; two documents asking the same question
   (equal words after normalisation) become one site and one gap `ABOUT` both question nodes. Questions
   worded differently (question similarity at least 0.9) are found by the duplicate check of the check
   step ([investigation.md](investigation.md) "Checks").
4. Investigation reads the question's stated default and owner, if any, and searches for an answer the
   build missed ([investigation.md](investigation.md) "Pointer answers").
5. The answer replaces the question line, so the next build removes the question node, and the close check
   resolves the gap when OQ-STATED no longer fires on it.

| Case | What gap does |
|---|---|
| The text answers the question elsewhere, and the build saw it | Nothing: no node, no seed |
| The text answers it elsewhere, but the build missed it | Investigation finds the answering line; AI resolves the gap with a `pointer` clarification citing it; nothing is added, and the next build's check drops the question |
| The question names a default or an owner ("TBD, default 30 days, owner: Finance") | The default becomes the recommended option; the owner is shown on the card |
| Two documents ask the same question | One gap `ABOUT` both question nodes; the answer replaces both lines |
| The question is in a tracking section (meeting notes, release plan) | Still a seed; if it is about an out-of-scope node only, it becomes an info note |
| A question already decided by a person in Specflow | Matched to the earlier gap by signature or question similarity; the earlier clarification is offered as the recommended option |

## Seeds and corroboration

| Strength | Meaning | Becomes a site |
|---|---|---|
| strong | A certain graph fact or a quoted line that shows the problem by itself | Always (unless suppressed) |
| weak | A signal that is often noise on its own (an isolated node, no owner, an empty attribute, a similarity) | Only with at least one corroboration signal of `seeds.corroboration` |

Corroboration signals:

| Signal | Holds when |
|---|---|
| `obligation_wording` | The node's text holds a cue of `relevance.obligation_cues` |
| `names_system_part` | The node's text names another node of `seeds.system_parts` (module, interface, data entity), a plural `s` allowed |
| `planned_dependency` | A plan task implements the node, or a node it is related to (`PlanView`) |

A weak seed from a `mixed` section needs `obligation_wording` and one more signal. Uncorroborated weak seeds
are counted, kept in `gap_seed` with `corroborated` false, and not investigated.

**Suppression.** Before grouping, every seed's signature (data-model.md "Identity", computed from the seed's
detector, `ABOUT` candidates and witness) is looked up in `gap_suppression`. A suppressed seed is dropped and
counted. A dismissal suppresses the dismissed gap's signature and its `seed_signatures`, so the seeds that formed it
are dropped here when their nodes and witness did not change (FR-GDET-07). A seed whose signature matches an existing gap in the graph is attached to that gap's site, so the
gap is re-checked, never duplicated.

**Info notes.** A seed whose detector and nodes can only ever be info (for example a stated question about an
out-of-scope node only) is written to `gap_note` and not investigated.

## Sites

The step `sites` groups corroborated seeds into sites, the entry points of investigations:
1. Seeds that share an `ABOUT` candidate merge.
2. Seeds whose `ABOUT` candidates have the same owning internal module and the same kind merge when their
   witnesses are within one section.
3. A site holds at most 12 seeds; larger groups split by section.

| Field | Value |
|---|---|
| `site_id` | `SITE::` and the first 12 hex digits of SHA-256 of the sorted seed signatures |
| `input_hash` | SHA-256 of: the seeds, the meaning revisions and evidence hashes of every `ABOUT` and `IMPACTS` candidate and their trace neighbours to depth 2, the gaps on them (id and status) other than gaps of origin `analysis` in the state their run left them (`open`, or `answered` by the run's AI route), the rules version and the prompt versions. Such a gap is left out because it is what an earlier run recorded from the same seeds, which the hash already holds: a run after a run on an unchanged graph finds every verdict cached |
| `rank_score` | strength weight (strong 2, weak 1) x `routing.severity_prior` of the kind x reach |
| Reach | 1 + the number of nodes reached by trace from the candidates along impact roles to depth 2 + 2 x the number of plan tasks implementing them (from `PlanView`, 0 without a plan) |

A site whose `input_hash` equals that of a site with a cached verdict (`gap_verdict`) reuses the verdict and is
not investigated (`outcome` `skipped`). Sites are investigated in `rank_score` order up to the mode's limit;
the rest get `outcome` `deferred` and are first in the next run.

## Limits

From `limits` in the rules file; all are starting values for the test corpus.

| Limit | Value | Applies to |
|---|---|---|
| `sites_update_run` | 30 | Sites investigated in an `update` run |
| `sites_full_run` | 120 | Sites investigated in a `full` run |
| `rounds_per_site` | 4 | Read rounds of one investigation |
| `reads_per_round` | 4 | Reads one round may request |
| `context_tokens` | 24,000 | Context of one investigation call |
| `pack_tokens` | 3,000 | Nearby nodes in the context pack |
| `new_questions_per_run` | 25 | New gaps with route `ask` shown in the inbox per run; the rest are written with `held_back` `yes` and shown from the next run on |
| `ai_answers_per_run` | 20 | AI answers per run |
| `ai_answers_max_node_share` | 0.05 | Share of spec nodes AI answers of one run may touch |
| `options_min`, `options_max` | 2, 4 | Options per gap |
| `apply_quiet_seconds` | 120 | Quiet period before answers are applied ([answers.md](answers.md) "Applying answers") |

## Rules file

`load_gap_rules(path=None, *, ontology)` reads [gap-rules.yaml](gap-rules.yaml) (or the project override in
`GapSettings.rules_path`) and validates it against the merged ontology, listing every problem in one
`ConfigurationError` ([ontology.md](ontology.md) "Rules file"). A detector whose entry names a type,
relation or attribute the ontology lacks is an error, never a silent skip. `GapContext.rules` holds the
loaded rules; the rules version is recorded on every run and is part of every site's input hash.

## Run metrics

Every run records in `gap_run.counts` and returns in `GapRunResult.counts`:

| Metric | Meaning |
|---|---|
| `sections_by_relevance` | Sections per relevance and per `by` |
| `seeds_by_detector` | Seeds per detector, with strong, weak, corroborated, suppressed and skipped-as-tracking counts |
| `detector_failures` | Detectors that failed, with the error code |
| `sites` | Sites formed, investigated, skipped (cache), deferred |
| `outcomes` | Sites per outcome: `gap`, `not_a_gap`, `not_spec`, `duplicate` |
| `gaps_new`, `gaps_updated` | Gaps written or re-checked, per kind and severity |
| `routes` | New gaps per route: `ask`, `ai` (pointers and AI answers) |
| `pointers`, `ai_answered`, `held_back`, `rejected` | New gaps resolved by a pointer, new gaps answered by AI (the host schedules their apply), new questions held back from the inbox, reports the checks rejected |
| `notes` | Info notes written |
| `model_calls`, `tokens` | Per gateway role |
| `noise_nodes` | Nodes listed in the noise report |
| `close` | Gaps the close check resolved, followed up, left waiting and took out as obsolete, and stale `CITES` removed (`update` and `full` runs) |

`estimate_gap(ctx, *, mode)` runs the steps up to `sites` without writing and returns the expected number of
investigations and model calls.
