---
type: Overview
title: "Graph library overview"
description: "Purpose, scope, goals, non-goals, actors, boundaries and key terms of the graph library"
status: stable
tags: [graph]
---
# overview.md: purpose, scope, goals, non-goals, actors, boundaries and key terms of the graph library

## Purpose

The graph library (Python package `graph`) turns the parsed documents of a project into a typed
knowledge graph in Neo4j and keeps it current as documents are added, revised and removed. The graph
shows what a software specification commits to: modules, features, capabilities and requirements, how
they relate, and which lines of which documents state them.

| Submodule | Role | In this specification |
|---|---|---|
| `graph.core` | What every submodule needs: Neo4j access, ontology, identity, read and search, owned writes, LLM gateway | specified |
| `graph.build` | Builds and updates the graph from documents; light inference; verification and repair | specified |
| `graph.gap` | Gaps, contradictions, findings | reserved, designed later |
| `graph.plan` | Implementation plans | reserved, designed later |

`graph.gap` and `graph.plan` reuse `graph.core`; no submodule copies core code.

## Goals

| # | Goal |
|---|---|
| G1 | Build a graph that conforms to the ontology from a project's documents. |
| G2 | Work with any ontology written in the ontology format, without code changes. |
| G3 | One node per real-world thing: no duplicates, no false merges. |
| G4 | One edge per stated relation, with the right type and direction. |
| G5 | A thing named in several documents is one node grounded in each of them. |
| G6 | Keep the graph current: a new, revised or removed document changes only what it affects, and unchanged nodes keep their `spec_id`. |
| G7 | One shared core for build, gap and plan. |

Quality is shown by tests, verification checks and the metrics each build reports; no benchmark gates.

## Non-goals

1. Relations from world knowledge: inference only links existing nodes when the texts support it.
2. Contradiction or gap detection, findings (`graph.gap`); planning (`graph.plan`).
3. Ingestion: lineages, versions, file parsing.
4. Scheduling, queues, HTTP endpoints, user interfaces, a "ready" flag for users.
5. A command-line tool.
6. Extraction from source code.
7. The features in [later.md](later.md).

## Actors

| Actor | Responsibility |
|---|---|
| Host | Program that runs the library (service, test, script): creates settings, Neo4j driver and port implementations, passes the ontology, calls builds, keeps build results, guarantees one writer per project at a time. |
| Corpus provider | Implements the corpus port: one snapshot of the current revision of every active document, with lineage id, version and immutable markdown. |
| LLM endpoint | One OpenAI-compatible endpoint for chat and embeddings. |
| Neo4j | One database for all projects, partitioned by project. |
| Reader | Code that reads the graph through the core read API, including later `gap` and `plan`. |

## Boundaries

| Neighbour | Library | Neighbour |
|---|---|---|
| Host | Offers build functions, read API, typed results and errors; never decides when to build | Orchestration, retries across processes, storage of results, UI |
| Ingestion | Requires lineage id, version and markdown that never changes after parsing | Decides lineages and versions, parses files |
| `graph.gap`, `graph.plan` | Keeps statements, provenance and stable `spec_id`s; preserves their nodes and relationships | Own their graph parts |
| Graphiti | Uses only its node and edge extraction functions, with model calls routed through the core gateway | Stores and searches nothing |

## Constraints

| # | Constraint |
|---|---|
| C1 | Every model call goes through the core gateway. |
| C2 | No ontology label, relation or id prefix is hard-coded in library code, except the built-in structural names ([architecture.md](architecture.md)). |
| C3 | Every query binds `project_id`; one project never reads or writes another's data. |
| C4 | No build deletes a project's graph wholesale; a build deletes only what it owns. |
| C5 | Document text and model output never become Cypher or instructions: values are query parameters, names come from the validated ontology, prompts mark document text as untrusted. |
| C6 | The library reads no environment variables and writes to disk only through file port implementations. |
| C7 | Neo4j 5.26 or later; Community edition is enough. |

## Key terms

| Term | Meaning |
|---|---|
| Project | Tenant of the graph; `project_id` is stored as `group_id` on every node and relationship. |
| Lineage, revision | All versions of one document; one version with immutable markdown. |
| Corpus | The snapshot of current revisions one build reads. |
| Fragment | A piece of a revision cut by segmentation; the unit of evidence (`:Fragment`). |
| Unit | Consecutive fragments of one document sent to one extraction call; never stored. |
| Hub node | A node of an `anchor` or `group` type (modules, features, capabilities): the names other nodes link to across documents. |
| Spec node | A node extracted from text: label `SpecNode` plus one domain label. |
| `spec_id` | Stable external id of a spec node, such as `REQ::FR::USERS_EXPORT_CSV`; never changes, never reused. |
| Identity key | Internal key that decides whether two extracted nodes are the same thing. |
| Domain edge | Typed relationship between spec nodes on a pair the ontology allows; `basis` is `stated`, `implied` or `inferred`. |
| Statement | What one fragment says about a node, kept with its source. |
| Build | One run that brings the graph in line with the corpus: mode `first`, `update` or `rebuild`. |
| Delta build | An `update` build: re-extracts only changed content and reuses stored results for the rest. |
| Owner | The submodule that may write a node or relationship type, given by the ontology `origin`. |
| Spine | Ontology paths by which a requirement should reach a module. |
| Host, port | The program running the library; an async interface the library defines for state outside Neo4j. |
