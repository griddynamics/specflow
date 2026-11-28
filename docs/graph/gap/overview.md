---
type: Overview
title: "Graph gap overview"
description: "Purpose, scope, goals, non-goals, actors, boundaries, constraints and key terms of graph.gap"
status: stable
tags: [graph, gap]
---
# overview.md: purpose, scope, goals, non-goals, actors, boundaries, constraints and key terms of graph.gap

## Purpose

`graph.gap` finds the places where a project's spec would force a builder to guess, asks the right person
the right question with a recommended answer, and brings the answer back into the spec so that only the
affected part of the graph and of the plan changes.

A **gap** is one of six kinds: missing information, an ambiguity, a contradiction, an inconsistency, a
stated open question, or an unconfirmed assumption ([detection.md](detection.md) "Gap kinds"). A gap is
found in three steps:
1. **Detection**: cheap checks with no model call (detectors) read the persisted graph and the text and
   return entry points (seeds) with a witness. A relevance filter keeps project-tracking noise (release
   names, sprint items, ticket references) out of the structural checks.
2. **Investigation**: one agent per entry point reads the graph around it, decides whether the gap is real,
   names every node it is about, and proposes two to four options with one recommended.
3. **Routing**: code decides who answers. A person answers severe gaps — in the project's chat channel or
   through MCP; AI answers
   simple ones with the recommended option (**advanced inference**). Both take the same path.

An answer becomes a **Clarification**. Its text reaches the spec only as corpus text: an amendment that
Specflow keeps on the source section, or a section of the project clarifications document. A normal update
build reads it, and the build's change report tells the plan what to re-plan. When the owner uploads a new
version of an amended document, a reconcile step decides whether the answer is applied again, adopted, in
conflict, or lost ([reconcile.md](reconcile.md)).

| Submodule | Role | Specification |
|---|---|---|
| `graph.core` | Store port, ontology, identity, reads, owned writes, LLM gateway | [../overview.md](../overview.md) |
| `graph.build` | Builds and updates the spec graph | [../build.md](../build.md) |
| `graph.gap` | Gaps, investigation, answers, reconcile | this folder |
| `graph.plan` | Implementation plans and task progress | [../plan/INDEX.md](../plan/INDEX.md) |

`graph.gap` reuses `graph.core` and never imports `graph.build` or `graph.plan`; neither imports it.

This folder replaces the first-slice specification of agent findings in [../gap.md](../gap.md). A finding a
coding agent reports is a gap with origin `development` ([lifecycle.md](lifecycle.md) "Development gaps").

## Goals

| # | Goal |
|---|---|
| GG1 | Find gaps of all six kinds in a built spec graph, each with a witness that code can check again: a quoted line found in its fragment, or a graph fact a detector re-evaluates. |
| GG2 | Keep noise out: project-tracking content (release names, sprint scopes, ticket references, roadmap items) does not become structural questions, and a dismissed gap stays quiet until its inputs change. |
| GG3 | Show the full reach of a gap: every node it is about, every node an answer would change, and, through trace and the plan, every story and task behind them. |
| GG4 | Ask well: one clear question, the reason, the quoted lines, two to four options and exactly one recommendation; the person can always write their own answer. |
| GG5 | One answer pipeline for people and AI: AI answers only minor gaps of allowed kinds when every gate holds, marks its answers and lets a person revert them. |
| GG6 | Bring every answer back as corpus text through a normal update build, so the build and plan ontologies stay unchanged, provenance and dedup keep working, and only the affected part is re-extracted and re-planned. |
| GG7 | Survive re-uploads: an answer is applied again, adopted, or reported as a conflict or a lost place; a person's newer text is never overwritten without a question. |
| GG8 | Serve coding agents: a finding reported during development is investigated at once, answered through the same path, and the story it holds is released when the answer is in the spec. |
| GG9 | Cost little when little changed: detectors are cheap and run over the whole graph; agents run only on new or changed sites; verdicts are cached by input hash. |

## Non-goals

1. Changing the spec graph directly. `graph.gap` never writes, deletes or edits a spec node, a spec
   relation, a document or a fragment. Answers reach the spec only through the corpus and a build.
2. Changing the build or the plan ontology. `graph.gap` owns only the types of its own file
   ([ontology.md](ontology.md)).
3. Writing to the customer's sources (Drive files or uploaded originals). Specflow keeps amendments beside
   them, and owners copy answers into their files ([reconcile.md](reconcile.md) "Answers moved into the sources").
4. Fixing extraction quality. The relevance filter and the noise report describe noise; they do not change
   what the build extracts.
5. Running coding agents, scheduling builds, HTTP endpoints, the MCP server or the user interface. These are
   host code; this spec states what the host provides ([architecture.md](architecture.md), [api.md](api.md)).

## Actors

| Actor | What it does |
|---|---|
| Host | Runs a gap run after each build, before the plan run; owns the relational store, the corpus reader, the runner, web routes and MCP tools |
| Person (project member) | Answers gaps in the project's chat channel or through MCP; reads the run's noise report |
| Coding agent (over MCP) | Reports a finding while building a story; reads open gaps and answers in briefs |
| Investigator agent | Reads the graph around a site through the core LLM gateway and returns a report |
| `graph.build` | Reads amended text and the clarifications document like any other corpus text; reports what changed |
| `graph.plan` | Shows open gaps in briefs and on stories, holds tasks behind blocker gaps, re-plans from change reports |

## Boundaries

| Neighbour | Boundary |
|---|---|
| `graph.build` | Writes the spec graph `graph.gap` reads. Its change reports reach gap only through the host, as the gap-side model `GapChangeReport`. The build is unchanged: it never knows that a text came from an answer |
| `graph.plan` | Reads open gaps through its existing structural port for findings, which the gap service satisfies. Re-plans from the build change reports the host passes (a prerequisite, [lifecycle.md](lifecycle.md) "Plan prerequisite") |
| Corpus reader (host) | Applies amendments and adds the clarifications document when it reads a project's documents; runs reconcile when a new revision arrives |
| Relational store (host, PostgreSQL) | Holds options, amendments, runs, seeds, sites, verdicts, suppressions, noise rules and the gap event log ([data-model.md](data-model.md) "Relational store") |
| LLM gateway (`graph.core`) | Every model call of `graph.gap` goes through it |

## Constraints

| # | Constraint |
|---|---|
| GC1 | Every model call goes through the core LLM gateway or through a host adapter of the `Investigator` port that uses the host's LLM gateway configuration; no other model access. |
| GC2 | Document text is untrusted: it is passed to a model only inside marked data blocks, and no model output is written before code validates it. |
| GC3 | `graph.gap` names spec types, relations and attributes only through its rules file; its own labels and vocabulary values may be literals. |
| GC4 | Every gap query binds and filters on the project id. |
| GC5 | A gap without a witness is never written. A model may propose a gap; only a checked report creates one. |
| GC6 | Builds stay one at a time per project; gap requests an update build through the host and never runs one. |

## Key terms

| Term | Meaning |
|---|---|
| Gap | A `Gap` node: a place where the spec would force a guess, with kind, severity, status and its relations |
| Seed | A detector hit: spec ids, a witness and a strength; stored in the relational store |
| Site | Seeds merged around shared nodes: the entry point one investigation reads |
| Witness | The evidence of a gap: a quoted line found in a fragment, or a graph fact a detector can re-check |
| Relevance | Whether a section is spec, tracking or mixed; tracking sections give no structural seeds |
| Report | The investigator's structured answer: verdict, kind, severity, question, why, ABOUT and IMPACTS ids, witness, options |
| Option | An offered answer with its edits; a relational row, never a node |
| Clarification | A `Clarification` node: the answer to one gap, its mode, placement and status |
| Edit | Where an answer's text goes: target section, operation (insert or replace) and text |
| Amendment | A stored edit applied by the corpus reader to a source document; a relational row |
| Clarifications document | One generated document per project holding answers that have no single home |
| Reconcile | The check of every active amendment against a new revision of its document |
| Advanced inference | An answer AI gives to a minor gap, through the same pipeline as a person's |
| Close check | The step of a gap run after a build that decides whether an answered gap is fixed |
| Noise report | Per gap run: the nodes and sections judged tracking, with the signals behind each |
