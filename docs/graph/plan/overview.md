---
type: Overview
title: "Graph plan overview"
description: "Purpose, scope, goals, non-goals, actors, boundaries and key terms of graph.plan"
status: stable
tags: [graph, plan]
---
# overview.md: purpose, scope, goals, non-goals, actors, boundaries and key terms of graph.plan

## Purpose

`graph.plan` turns the spec graph that `graph.build` keeps current (build ontology v2.0) into an
implementation plan that coding agents can follow, and keeps that plan current as the spec changes. The
plan has three levels:
- **work areas**: the parts of the system built together, one per internal module plus one project
  area;
- **stories**: what a user picks work by, one per feature, interface and data entity, plus a few for
  work without such an anchor;
- **tasks**: units of work one coding agent can finish, joined by `WAITS_FOR` into one acyclic task
  graph.

The plan is stored in the same graph store (Neo4j, ArcadeDB or PostgreSQL), next to the spec nodes it
implements. It also records
how far the work has come: which tasks are claimed, and which are done by which git commit. Users
connect their own coding agents to the app over MCP; the host's MCP tools wrap the plan's read and
lifecycle API, and the plan enforces the claim rules for every client.

| Submodule | Role | Specification |
|---|---|---|
| `graph.core` | Store port, ontology, identity, reads, owned writes, LLM gateway | [../overview.md](../overview.md) |
| `graph.build` | Builds and updates the spec graph | [../build.md](../build.md) |
| `graph.gap` | Gaps, contradictions, findings | reserved |
| `graph.plan` | Implementation plans and task progress | this folder |

`graph.plan` reuses `graph.core` and never imports `graph.build` or `graph.gap`.

## Goals

| # | Goal |
|---|---|
| PG1 | Group every plannable spec node of a project into exactly one work area, along the v2.0 spine: interfaces by their provider, data entities by the module that stores them, other nodes by their owner. |
| PG2 | Cut each work area into right-sized tasks: each with a description, checkable done-when statements marked as from the spec or from the planner, and the spec nodes it implements; every required node implemented by a task, recorded as not buildable, or cited by a project convention. |
| PG3 | One acyclic task graph for the project, from explicit spec edges: setup before component work, an interface's contract before its consumers, data before the logic that uses it, stated dependencies kept, deployment after setup, closing work last. |
| PG4 | Hand a coding agent only what its task needs: the task and its story, the spec nodes it implements with their source lines, the open questions on them, how certain the edges behind the task are, the contracts of finished prerequisites and the project profile. |
| PG5 | Track progress on the tasks themselves: status, claims, the git commit that finished each task; only ready tasks can be claimed, and a refused claim names what blocks it. |
| PG6 | Keep the plan current: re-plan only what the build change reports mark as changed in meaning or links, ignore rewording, keep finished work whose spec did not change, and keep task and story ids stable. |
| PG7 | No model call when nothing changed; a plan run never fails because of a non-fatal model failure (a bad or failed answer). |
| PG8 | Give users stories to pick work from, with progress derived from their tasks, and give the plan as a DAG (tasks in order, grouped by story) at any time. |

Quality is shown by tests, plan checks and the metrics each plan run reports; no benchmark gates.

## Non-goals

1. Running coding agents, git operations or test commands. There is no executor in scope: users'
   agents work through MCP tools, and the MCP server is host code ([api.md](api.md) "MCP tools (host)",
   [lifecycle.md](lifecycle.md) "Agent contract").
2. Changing the spec graph: no spec node, domain edge, document or fragment is written or deleted.
3. Findings, clarifications, gap detection (`graph.gap`). Open questions are shown, not resolved.
4. Scheduling, queues, HTTP endpoints, user interfaces, a command-line tool, the MCP server itself.
5. Estimating effort of the work in time or money (`estimate_plan` estimates only a plan run's calls and
   tokens).
6. Release filters or release ordering: `release` is carried on tasks and stories, nothing more.
7. Forced claims that ignore prerequisites.
8. The features in [later.md](later.md).

## Actors

| Actor | Responsibility |
|---|---|
| Host | Creates settings, the graph store, the gateway (Review uses its `judge` class; `PlanOptions.review_model` may override the model) and port implementations; runs a plan after a build has returned unless its settlement lists `documents_incomplete`; passes the build change reports; runs the MCP server; guarantees one plan writer per project at a time. |
| MCP server | Host code that exposes the plan's reads and lifecycle calls as tools and gives agents the agent contract. |
| Coding agent | An MCP client that picks a story or task, claims ready tasks, reads briefs and reports each result with its commit, or blocks, fails or releases. |
| User | Picks stories and tells agents what to work on; reviews stale commits and unverified work the plan reports. |
| `graph.build` | Keeps the spec graph current; owns every spec node and domain edge `graph.plan` reads; produces the change report of every build. |
| LLM endpoint | The core gateway's endpoint; planning calls go through it. |
| Reader | Code that reads the plan (host UI, reports, a later `graph.gap`). |

## Boundaries

| Neighbour | `graph.plan` | Neighbour |
|---|---|---|
| Host | Offers plan runs, `estimate_plan`, the task lifecycle, task briefs, reads, typed results and errors; never decides when to plan and checks nothing about settlement | Triggers, settlement decision, change reports, retries across processes, MCP server, UI |
| `graph.build` | Reads spec nodes, domain edges (with `basis` and `confidence`), statements and provenance through `GraphReader`; keys everything on `spec_id`; takes the build change reports from the host as plan-side `BuildChangeReport`, which decide what is re-planned; its correctness follows their classification | Keeps `spec_id`s stable; moves or deletes plan relationships on merge and delete (W3, W4 of [../architecture.md](../architecture.md)); `check_frozen` records and compares only build-owned labels and relations, so installing or changing the plan ontology does not block updates |
| Coding agents (over MCP) | Stores claims, statuses and commits; checks state transitions and readiness | Own the workspace, git, builds and test runs |
| `graph.gap` (later) | Will read findings to hold tasks back ([later.md](later.md)) | Owns findings and clarifications |

## Constraints

The constraints of the library ([../overview.md](../overview.md) C1-C7) apply unchanged. In addition:

| # | Constraint |
|---|---|
| PC1 | `graph.plan` writes only the node and relationship types its ontology file declares (owner `plan`) ([architecture.md](architecture.md)). |
| PC2 | No spec-side label, relation or vocabulary value is hard-coded in code; they come from the merged ontology through the plan rules file. The plan's own names are fixed by this spec ([ontology.md](ontology.md) "Names"). |
| PC3 | Planning output from a model is validated before anything is written; text from documents and from models never becomes query text. |

## Key terms

| Term | Meaning |
|---|---|
| Plan | The living implementation plan of one project: one `:Plan` node (`PLAN::MAIN`), its work areas, stories and tasks. |
| Plan run | One run that brings the plan in line with the spec graph: mode `first`, `update` or `replan`. |
| Plannable node | A spec node of a type the plan rules include: internal modules, capabilities, interfaces, data entities, features and the six requirement types, minus nodes `in_scope: no` themselves or through their owner. External modules and open questions are context only. |
| Interface | A v2.0 spec node: a named contract PROVIDED_BY at most one module and CONSUMED_BY others. Planned in the provider's area as a `contract` task; anchors a story. |
| Data entity | A v2.0 spec node (`DataEntity`): a business object STORED_IN a module, owning data requirements. Planned as a `data` task; anchors a story. |
| Open question | A v2.0 spec node (`OpenQuestion`) RELATES_TO the nodes it concerns. Never planned; shown in the briefs of tasks implementing those nodes. |
| Work area | A part of the system built together: an internal module (deployment and datastore modules included) with what reaches it on the spine, or the project area. Label `WorkArea`. |
| Project area | The work area `AREA::PROJECT`: setup and closing tasks, cross-cutting nodes (reaching several modules) and unplaced nodes (reaching none). |
| Story | What a user picks work by: one per feature, interface and data entity of an area, a `CORE` story for what a module owns directly, and in the project area chapter stories, `SETUP` and `CLOSING`. Label `Story`; `STORY::<area slug>::<SLUG>`. Every open task is in exactly one active story of its own area. |
| Story progress | `pending`, `in_progress`, `blocked` or `completed`, derived from the story's tasks at read time, never stored. |
| Task | One unit of work for one coding agent, in one work area, one story and one phase. Label `Task`. |
| Phase | The kind of work a task does: `setup`, `contract`, `data`, `logic`, `integration`, `quality`, `acceptance`. |
| Task key | `TASK::<area slug>::<action>`; stable across plan runs while the work it names exists; `_R<n>` for revisions. |
| Prerequisite | A task another task `WAITS_FOR`; it must be `completed` before the waiting task is ready. |
| Ready task | A `pending` task in an active area and story whose prerequisites are all `completed`, while no plan persist runs. Only ready tasks can be claimed. |
| Plan DAG | The tasks and their `WAITS_FOR` edges: always acyclic. Story order is a projection of it, may contain cycles, and is never enforced. |
| Task brief | What a coding agent reads for one task, computed on request from the plan and the spec graph. |
| Project profile | Stack, layout, test command and conventions every task follows, chosen by the project planner; a convention may cite the requirements it covers. |
| Not buildable | A required member a planner recorded as no code work, with a reason (`process`, `governance`, `release_gate`, `policy_only`). |
| Change report | What one build changed, by `spec_id` and class; the host passes those since the plan's last consumed build as `BuildChangeReport`. |
| Meaning change, link change | Nodes classed `added`, `removed`, `merged`, `carried_over`, `rekeyed` or `changed_meaning`; nodes classed `changed_links` and the ends of changed edges. `changed_evidence` (rewording) changes no plan. |
| Settlement | The build's statement whether its graph is complete; the host does not plan while it lists `documents_incomplete`. |
| Fingerprint | A hash of the plannable and context nodes with their builds, the domain edges that touch them, the ontology, plan ontology and rules versions and the shaping options, used only as a guard (resume, the `outdated` metric), never to decide what is re-planned. |
| Planner | A planning model call: area describer, project planner, task planner or reviewer. |
| Plan rules | The shipped `plan-rules.yaml`: which spec types are plannable and in which role, their default phase, which anchor stories, the relation roles; planner guidance and the fallback when a planner fails. |
