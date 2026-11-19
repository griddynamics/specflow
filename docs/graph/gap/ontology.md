---
type: Ontology
title: "Graph gap ontology"
description: "The gap ontology file, the gap rules file, how they load and what may change in them"
status: stable
tags: [graph, gap]
---
# ontology.md: the gap ontology file, the gap rules file, how they load and what may change in them

## Ontology file

`graph.gap` ships `graph/gap/ontology.yaml` (`owner: gap`); [gap-ontology.yaml](gap-ontology.yaml) is a copy.
It uses the ontology format of [../ontology.md](../ontology.md) and declares only:

| Kind | Names |
|---|---|
| Node types | `Gap` (origin `analysis`), `Clarification` (origin `human`), both tier `governance` |
| Relations | `ABOUT`, `IMPACTS`, `CITES`, `FOLLOWS_UP` (origin `analysis`); `ANSWERS`, `PLACED_IN`, `GROUNDED_IN`, `REPLACES` (origin `human`) |
| Vocabularies | `gap_kind`, `gap_severity`, `gap_status`, `gap_origin`, `gap_route`, `gap_resolution`, `clarification_mode`, `clarification_placement`, `clarification_status` |

`load_ontology` merges it with the build file (and the plan file when installed). Pairs end at build types,
at `Fragment` and at `Document`. No type has a `role`: nothing here is extracted, so the file never changes
the extraction fingerprint. The build and plan files are not changed.

The file replaces the first-slice file (`Finding`, `ExternalRef`, `ABOUT` to findings, `REFERS_TO`) described
in [../gap.md](../gap.md). Migration of existing findings is in [lifecycle.md](lifecycle.md) "Migration of
first-slice findings". Until that migration ships, the file also declares the first-slice types for the
transition, marked as such: `Finding` (origin `agent`), `ExternalRef`, `REFERS_TO`, their vocabularies, and the
finding pairs of `ABOUT` (to the same spec types and to `Fragment`). `ABOUT` is declared once, with origin
`analysis`, holding both the gap and the finding pairs; the migration removes the transition entries.

## Rules file

`graph.gap` ships `graph/gap/gap-rules.yaml`; [gap-rules.yaml](gap-rules.yaml) is a copy. `load_gap_rules`
reads it and validates it against the merged ontology:

| Section | Holds | Validated |
|---|---|---|
| `relevance` | Tracking titles, tracking patterns, the isolated share, obligation cues, whether a model classifies uncertain sections | Patterns compile; share in 0 to 1 |
| `seeds` | Corroboration signals a weak seed needs; `system_parts`, the types whose names count for `names_system_part` | Known signal names; the types resolve to spec types |
| `detectors` | One entry per detector: kind, family, strength, types, rule, rule parameters, optional slice | Kind is a `gap_kind` value; the code is one of the catalogue with its kind, family, strength and rule; every type, group, relation and attribute resolves in the merged ontology (an attribute on every type of the entry; a `cycle` relation with both ends among the types; `members`, `interfaces` resolve to spec types, `provided_by`, `consumed_by` to spec relations, `topics` to checklist topics, an `exclude` attribute is declared on every end of the rule with values of its vocabulary); the rule is known and has exactly its parameters |
| `placeholder_markers`, `vague_terms` | Term lists of OQ-TBD and AM-VAGUE | Non-empty |
| `checklist` | Topics and their cue words for MS-COVERAGE, MS-ERROR, MS-LIFECYCLE | Every topic a detector names exists |
| `routing` | Kinds AI may answer, gate thresholds, severity priors | Kinds are `gap_kind` values and AI kinds a subset of the shipped list; every kind has a prior; thresholds in range |
| `limits` | Run, investigation and AI limits | Positive; `options_min` at least 2, `options_max` at most 4 |

Every problem is listed in one `ConfigurationError`. A project may override the file with its own copy
(`GapSettings.rules_path`); the override is validated the same way.

## What may change

| Part | Rule |
|---|---|
| Structure of the ontology file: labels, origins, tiers, id formats, relations, pairs, vocabulary values | Fixed by this spec; a change is a spec change, made here first |
| Descriptions in the ontology file | May be improved |
| Detector entries | Adding a detector is a spec change ([detection.md](detection.md) "Detector catalogue"); a detector's parameters, cue words and slice may be tuned |
| Relevance titles and patterns, vague terms, placeholder markers, checklist cues | May be tuned on the test corpus |
| Routing thresholds and limits | May be tuned; the AI kinds list may only shrink without a spec change |
