---
type: Architecture
title: "Graph gap architecture"
description: "Package layout, components, run steps, import rules, write ownership, ports, settings, errors and the changes outside graph.gap"
status: draft
tags: [graph, gap]
---
# architecture.md: package layout, components, run steps, import rules, write ownership, ports, settings, errors and changes outside graph.gap

## Package layout

```
graph/gap/
  ontology.yaml        gap ontology file (owner: gap), found by load_ontology
  gap-rules.yaml       detectors, relevance, checklist, routing, limits
  rules.py             load_gap_rules: read and validate the rules against the merged ontology
  ports.py             GapRecords, Amendments, Investigator, StoryHolds, BuildRequests, PlanView
  models.py            GapContext, GapSettings, GapChangeReport, GapRunResult, GapView, AnswerInput, NewGap, rows
  errors.py            GapNotFoundError, GapStateError, AnswerInvalidError, StaleGapError
  run.py               run_gap, estimate_gap: the run steps, resume from the run store
  run_store.py         GapRunStore, GapRunRecord: the run artifact store (file and in-memory), for resume
  gaps.py              the gap read (READ_GAPS) as StoredGap, and the keys of gap nodes and relations
  steps/
    scope.py           change reports: aliases, touched nodes, the investigation scope
    close.py           close check of applying gaps, obsolete gaps, stale witnesses, follow-ups
    reconciled.py      close check of answers reconcile changed: absorbed, conflict and orphaned follow-ups
    relevance.py       relevance filter: rules, cache, batched model call
    detect.py          detectors from the rules file -> seeds
    sites.py           corroboration, grouping, ranking, known-site skip, limits
    snapshot.py        the graph detection reads once: nodes, edges, documents, active fragments, sections
    pack.py            the context pack of a site (no model call)
    reads.py           GraphSiteReads: the read menu bound to the project, and the ids it showed
    investigate.py     the investigate step and the default structured loop (LoopInvestigator)
    check.py           report validation, duplicates, the second check
    route.py           severity, gates, ask or ai
    record.py          Gap and Clarification nodes and relations, relational rows, holds
  answers.py           answers, edits, placement, apply, revert, AI answers
  corpus.py            amendments applied to a revision, effective revision id, the clarifications document
  reconcile.py         reconcile_document, reconcile_corpus: amendments against a new source revision
  follow_ups.py        what answers to follow-up gaps replace (predecessors), options that settle at once
  service.py           GapService: inbox reads, answers, dismissals, raise, the plan's findings port
  prompts.py           prompt revisions and gateway prompt names (gap.<name>.<revision>)
  services/
    vector_similarity.py  cosine of vectors no store holds (question embeddings of the duplicate check)
  prompts/             relevance, investigate, judge, edits and reconcile prompts <name>.md (revisions in prompts.py)
  ddl/                 gap_schema.cypher: the graph_gap_* constraints and indexes
  queries/             one canonical .cypher file per gap query
```

`graph.gap` exports its public names from its package root ([api.md](api.md)).

## Components

| Component | Responsibility | Spec |
|---|---|---|
| Rules | Load and validate detectors, relevance, checklist, routing, limits | [ontology.md](ontology.md) |
| Scope | Merge change reports, map aliases, list touched nodes and fragments, choose the investigation scope | [detection.md](detection.md) |
| Close check | Ground clarifications, re-check answered gaps, resolve or follow up, move relations after rekeys, mark obsolete gaps | [lifecycle.md](lifecycle.md) |
| Relevance filter | Classify sections as spec, tracking or mixed; noise report | [detection.md](detection.md) |
| Detectors | Seeds with witness and strength | [detection.md](detection.md) |
| Sites | Corroborate weak seeds, group seeds, rank, skip known sites, apply limits | [detection.md](detection.md) |
| Investigator | Read around a site and return a report | [investigation.md](investigation.md) |
| Check | Validate reports, find duplicates, run the second check | [investigation.md](investigation.md) |
| Route | Decide severity and who answers | [answers.md](answers.md) |
| Record | Write gaps, clarifications, relations, option rows, events; hold tasks | [data-model.md](data-model.md) |
| Answers | Turn an option or own text into a clarification and edits; apply, withdraw, revert | [answers.md](answers.md) |
| Reconcile | Decide what happens to each amendment when its document changes | [reconcile.md](reconcile.md) |
| Service | Inbox reads, answers, dismissals, raising gaps, noise rules, the plan's findings port | [api.md](api.md) |

## Run steps

A gap run executes these steps in order. Each step writes its output to the run store before the next
starts; a run interrupted by a shutdown resumes at the first unfinished step with the same run id.

| Step | Reads | Writes | Model |
|---|---|---|---|
| `scope` | `ctx.changes`, gap nodes, last run | Touched ids, alias map | No |
| `close` | Applying and reconciled gaps, change reports, fragments | Status, `GROUNDED_IN`, follow-ups, moved relations, released holds | Contradiction re-check only (cached) |
| `relevance` | Sections of touched documents (all in `full`), relevance cache, noise rules | Relevance rows, noise report | `gap_relevance`, uncertain sections only |
| `detect` | Graph snapshot, fragments, rules, build facts, section relevance | Nothing (seeds and info notes go to `sites`) | No |
| `sites` | Seeds, suppressions, plan view, trace, verdict cache | Seed rows (append-only, with their site), site rows of `skipped` and `deferred` sites, info notes | No |
| `investigate` | Sites, graph reads | Reports | `gap_investigate` |
| `check` | Reports, gaps, fragments, suppressions | Checked reports, duplicates | `gap_investigate` repair (once per report), `gap_judge` for pointers and AI candidates, embeddings for similar questions |
| `route` | Checked reports, rules | Route and severity | No |
| `record` | Routed reports | Gap and Clarification nodes, relations, option rows, notes, events, holds | No |

A model failure in `relevance` leaves the section `mixed`; in `investigate` or `check` it defers the site to
the next run. Neither fails the run. Mode `site` skips `scope` and `close` and runs the rest for the named
sites only.

## Import rules

| Rule | Check |
|---|---|
| `graph.gap` imports `graph.core` and the standard library, pydantic and its own modules only | import-linter contract |
| `graph.build` and `graph.plan` never import `graph.gap` | import-linter contract |
| The host (`api`, `mcp_server`, `temporal`) may import all three | |
| An `Investigator` adapter that uses an agent runtime lives in the host, never in `graph.gap` | import-linter contract |

## Write ownership

`graph.gap` is the owner `gap` of the ontology: it writes only `Gap`, `Clarification` and the relations of
its ontology file through the core owned writer, and deletes only them. It never writes or deletes a spec
node, a spec relation, a document, a fragment, or a plan node; plan task status changes only through the
plan's own calls behind `StoryHolds`. Relational rows are written only through `GapRecords` and
`Amendments`.

## Ports and who fills them

| Port | Filled by |
|---|---|
| `GapRecords` | Host: PostgreSQL tables of [data-model.md](data-model.md) "Relational store" |
| `Amendments` | Host: the `corpus_amendment` table, also read by the corpus reader |
| `Investigator` | `graph.gap`: the structured loop over the core gateway (`investigator = "loop"`); host: an adapter on the Temporal agent runtime with function tools (`"agents"`, [investigation.md](investigation.md)) |
| `StoryHolds` | Host: the plan service (guard, block, unblock, the blocker hold) |
| `BuildRequests` | Host: the build runner's queue |
| `PlanView` | Host: the plan reader |

The plan's `FindingsReads` port is filled by `GapService`.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `GAP_ENABLED` | `true` | Runs gap runs and serves the Gaps screen |
| `GAP_AUTO_RUN_AFTER_BUILD` | `true` | Runs an update gap run after each build, before the plan run |
| `GAP_INVESTIGATOR` | `loop` | `loop` or `agents` |
| `GAP_AI_ANSWERS` | `true` | Allows the `ai` route; when off, every gap is asked |
| `GAP_RULES_PATH` | empty | A project-wide rules file replacing the shipped one |
| `GAP_APPLY_QUIET_SECONDS` | `120` | Quiet period before pending answers are applied |
| Gateway roles `gap_relevance`, `gap_investigate`, `gap_judge`, `gap_edits` | the gateway defaults | Model, token limits and timeouts per role |

The host maps the settings to `GapSettings` ([api.md](api.md) "Models") when the graph build is configured
(`RoleSettings.gap`, with `gap_settings_from_env`); an invalid value stops the start naming the variable. Limits come
from the rules file.

## Errors

`graph.gap` raises `ConfigurationError` (core) for invalid rules, settings or missing ports, and its own
`GapNotFoundError`, `GapStateError`, `AnswerInvalidError` and `StaleGapError` ([api.md](api.md) "Errors"). Model failures
are never raised from a run. Logs carry ids, counts, step names and error codes only, never document text,
questions or answers (`graph.gap.*` events).

## Changes outside graph.gap

The build and plan ontologies do not change. These changes are needed elsewhere:

| Where | Change |
|---|---|
| Build runner (host) | Store each build's change report with the build; call the gap runner (`after_build`) after a build, before the plan run; stop calling the first-slice findings refresh |
| Plan runner (host) | Pass the change reports since the plan's last consumed build to the plan run (the plan prerequisite, its own pull request first, [lifecycle.md](lifecycle.md)); wait until the project has no gap run queued or running before it plans (`wait_idle`) |
| Plan service (host wiring) | Fill the findings port with `GapService`; provide `StoryHolds`; a claim is refused when a task of the story is held by a blocker gap |
| Corpus reader (host) | Apply amendments, add the clarifications document, call reconcile, report the effective revision id ([api.md](api.md) "Host runner") |
| Relational store (host) | One migration adding the tables of [data-model.md](data-model.md) |
| Gap runner (host, new) | `api/gap/runner.py`, the routes and their models |
| MCP server (host) | `add_finding` mapped to `raise_gap`; new read `list_gaps` (agent route `api/routers/agent_gaps.py`); briefs and `get_node` show gaps through the plan's findings port, filled by `api/gap/findings.py` (`OpenGaps`) |
| Frontend | The Gaps screen from the decision components, with a recommended mark on option cards; plan screens and the document view link to it |
| First slice of `graph.gap` | `Finding`, `ExternalRef`, `FindingFlow` and their routes are replaced; existing data is migrated ([lifecycle.md](lifecycle.md) "Migration of first-slice findings") |
