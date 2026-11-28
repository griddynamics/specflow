---
type: Questions
title: "Graph gap decisions"
description: "Decision record of the graph.gap specification: GD1-GD3 and the topics decided with the design"
status: stable
tags: [graph, gap]
---
# questions.md: decisions of the graph.gap specification

The record of the design decisions behind this specification. Part 1 keeps the three questions that needed
a decision before building, with their options and the decision taken. Part 2 records the topics decided
with the design, each with its reason. Every spec file is written as decided; this file explains why.

# Part 1. Questions decided before building

## GD1. Where does an answer's text go?

| Option | Effect |
|---|---|
| A. An amendment that Specflow keeps on the source section by default, plus one project clarifications document for answers with no single home | Precise: the answer lands where its topic lives, a contradiction replaces the losing line, and Drive files or uploads are never written. Costs an amendment table and anchor matching in the corpus reader, and needs reconcile on re-upload |
| B. The clarifications document only | Simplest. A contradiction cannot replace the older line, so the graph keeps both statements; answers sit away from their topic, which may lower extraction and linking quality |
| C. Write the answer back into the source files | One source of truth, but needs write access to customers' documents and an edit review flow |

**Decision: A.** Specified in [answers.md](answers.md) "Placement rules" and [reconcile.md](reconcile.md).

## GD2. Do open blocker gaps hold plan tasks back?

| Option | Effect |
|---|---|
| A. Yes, blockers only: a task that implements a node an open blocker gap is `ABOUT` cannot be claimed, and the refusal names the gap | Agents never build on a decision nobody took; major and minor gaps show in briefs only, so early runs do not freeze the plan |
| B. No holds: gaps show in briefs and on stories; agents decide | Simplest; agents may build on a guess |
| C. Any open gap holds its tasks | Safest, but early runs could freeze much of the plan |

**Decision: A.** Specified in [lifecycle.md](lifecycle.md) "Plan link".

## GD3. A new upload contradicts an earlier answer: which text does the build read until someone decides?

| Option | Effect |
|---|---|
| A. The upload; the amendment is suspended (clarification status `conflict`) and a follow-up contradiction gap asks: keep the upload, restore the earlier answer, or write a new one | The document owner's newest text always counts, and nobody's newer text is overwritten without a question |
| B. The earlier answer; the amendment keeps replacing the line and the gap asks the owner to confirm | The spec stays stable, but the owner's file and the graph disagree until someone acts |
| C. Neither; the section stays as it was at the last build until a person decides | Safest for the plan, but blocks every other change in that section |

**Decision: A.** Specified in [reconcile.md](reconcile.md).

# Part 2. Topics decided with the design

| Topic | Decision | Reason |
|---|---|---|
| Ontologies | Build and plan ontologies unchanged; gap owns its own file with `Gap`, `Clarification` and eight relations | The gap module adds its own layer, as the plan module does |
| Graph or relational store | `Gap` and `Clarification` with real relations in the graph; options, amendments, runs, seeds, sites, verdicts, suppressions, noise rules and the event log in the relational store | History and reach are walks in the graph; options as nodes add nothing a graph read needs |
| Relations of a gap | Two relation types, `ABOUT` (the problem is in this node) and `IMPACTS` (an answer will change it), rather than one relation with a role property | Every arrow is a real relation type, and readers filter by type |
| Resolved gaps | Stay in the graph with their clarification; dismissed and obsolete gaps leave it | "Why does this requirement exist?" stays answerable; noise does not accumulate |
| Gap kinds | missing, ambiguous, contradiction, inconsistency, open_question, assumption | Each maps to its detectors, its answer shape and its AI rule |
| Stated open questions | `OpenQuestion` nodes are strong seeds; a stated default becomes the recommended option | They are the most direct signal the spec gives |
| Noise | A relevance filter in gap (spec, tracking, mixed); weak seeds need corroboration; a noise report and noise rules; the build is unchanged | Many isolated or ownerless nodes are release names or task tracking, not spec |
| Gap never edits the spec graph | Answers reach the spec only as corpus text through a normal update build | Provenance, dedup and change reports keep working, and the plan re-plans only what changed |
| One answer pipeline | People and AI create the same Clarification and take the same path | Simple gaps and severe gaps differ only in who answers |
| AI answers | Applied without approval, only for minor gaps of allowed kinds when every gate holds; marked, listed for review, revertible; a per-run valve | Fast for simple gaps, safe for the rest |
| Severity | blocker, major, minor; info is a run note | Maps to who answers and to the plan effect |
| Who answers | Any project member; the answerer is recorded | Membership is the access rule of the app |
| Agent runtime | An `Investigator` port; the default adapter is a bounded loop of structured calls through the core gateway; an optional host adapter uses the existing OpenAI Agents SDK runtime with graph reads as function tools | The loop runs in process with exact cost and caching; the tool agent reuses existing code where freer exploration pays off |
| When gap runs | Automatically after every build that returns, before the plan run, incrementally; a full run on demand | Detectors are cheap; agents see only new or changed sites |
| Dismissed gaps | Quiet until their signature changes | No nagging; a real change asks again |
| Findings from coding agents | A finding is a gap with origin `development`, investigated at once; when it blocks a task, its answer is applied without waiting | One list and one answer path; the agent reads the updated spec |
| Change-report wiring of the plan | The host passes build change reports to the plan run; ships first, as its own change | The plan library supports partial re-planning, but the host passes no reports, so every newer build re-plans everything |
| First-slice findings | Replaced by `Gap`; existing findings are migrated | One gap type, one inbox |
