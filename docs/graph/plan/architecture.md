---
type: Architecture
title: "Graph plan architecture"
description: "Package layout, boundaries, components, import rules, write ownership, ports, settings and errors of graph.plan"
status: stable
tags: [graph, plan]
---
# architecture.md: package layout, boundaries, components, import rules, write ownership, ports, settings and errors of graph.plan

## Package layout

```
graph/plan/
  ontology.yaml        plan ontology file (owner: plan), found by load_ontology
  plan-rules.yaml      plan rules: plannable types, default phases, phase order
  prompts/             planner prompts <name>.md (their revisions in prompts.py)
  ddl/                 plan_schema.cypher: the graph_plan_* constraints and indexes
  queries/             one canonical .cypher file per plan query
  ...                  modules: snapshot, partition, planners, stitch, review, diff, persist,
                       lifecycle, reader, checks, run
```

`graph.plan` exports its public names from its package root. It imports `graph.core`, never
`graph.build` or `graph.gap`; neither of those imports it.

## Boundaries

| Neighbour | Boundary |
|---|---|
| `graph.build` | Writes the spec graph `graph.plan` reads. Its change reports reach the plan only through the host, converted to the plan-side model `BuildChangeReport` (`PlanContext.changes`, [api.md](api.md)); `graph.plan` never imports `graph.build`. The host runs a plan after a build returned, unless the build's settlement lists `documents_incomplete`; the plan itself enforces nothing about settlement |
| Host | Owns the process: settings, gateway, ports, scheduling of plan runs, and the MCP server |
| MCP server (host code) | Exposes the plan's reads and lifecycle calls as tools for coding agents ([lifecycle.md](lifecycle.md) "Agent contract"). There is no executor in `graph.plan`: agents claim, build and report through these tools. Checking that a reported commit exists is host-side and optional; the plan cannot see git |

## Components

| Component | Responsibility | Spec |
|---|---|---|
| Snapshot | Read the spec graph, the current plan and the change reports; merge the change set and meaning revisions; compute the graph fingerprint; choose the mode | [plan.md](plan.md) |
| Partition | Assign every plannable node to exactly one work area and one home story; create stories; cut planning parts from whole stories | [plan.md](plan.md) |
| Planners | Describe areas and stories, plan the project, plan the tasks of each part, review the plan, through the core gateway; validate every answer; fall back to the rules | [plan.md](plan.md) |
| Stitch | Add cross-area waits (`WAITS_FOR`); keep the task graph acyclic | [plan.md](plan.md) |
| Diff, persist | Match the new plan to the current one, keep finished work, write the difference | [plan.md](plan.md) |
| Lifecycle | Ready tasks, claims (`claim_task`, `claim_next`), results, recovery, story work and progress, task briefs, the plan DAG | [lifecycle.md](lifecycle.md) |
| Checks | Plan checks after every run and on request | [lifecycle.md](lifecycle.md) |
| Run | `run_plan`, `estimate_plan`, step functions, options, plan result | [plan.md](plan.md), [api.md](api.md) |

## Import rules

The library rules I1-I5 ([../architecture.md](../architecture.md)) apply to `graph.plan` unchanged:

| # | What it means here |
|---|---|
| I1 | No new dependency: work areas, cycle checks and topological order are written with the standard library. In particular no agent runtime (`llm`), no `networkx`. |
| I2 | Model calls only through the core `Gateway`; `graph.plan` never imports `openai` or `graphiti_core`. |
| I3 | Query text only in the plan query module: canonical `.cypher` files under `graph/plan/queries/`, named by the dialect-neutral templates `plan.<name>`. The Cypher stores render them (ArcadeDB runs them unchanged); the PostgreSQL adapter holds one SQL port of each under `stores/postgres/queries/plan/`. Every query runs through the store's executor and binds the project id. |
| I4 | No spec-side label, relation, attribute or vocabulary value as a literal: they come from the merged ontology through the plan rules. The plan's own names (its labels, relations and vocabulary values) are fixed by this spec and may be literals in `graph.plan` only ([ontology.md](ontology.md) "Names") |
| I5 | No environment reads, no printing, no disk writes outside file port implementations. |

## Write ownership

The plan ontology file declares every type `graph.plan` writes, all with origin `planner`
([ontology.md](ontology.md)). `graph.core`'s `OwnedWriter` refuses any other type for owner `plan`
(W1 of [../architecture.md](../architecture.md)).

| # | Rule |
|---|---|
| PW1 | `graph.plan` writes only `Plan`, `WorkArea`, `Story`, `Task` nodes and `IN_AREA`, `IN_STORY`, `IMPLEMENTS`, `WAITS_FOR`, `SUPERSEDES` relationships, through `OwnedWriter` or its own query module (lifecycle compare-and-set writes, plan reads). |
| PW2 | It never sets a property on a spec node, document or fragment; a relationship from or to a spec node is the only thing it adds there. |
| PW3 | It accepts that build moves its relationships to the surviving node on a merge (with their old keys) and deletes them with a deleted spec node (W3, W4); every plan run matches plan relationships by their endpoints and reads coverage again instead of trusting earlier writes. |
| PW4 | Everything written carries `group_id = project_id`. |

## Ports

| Port | Holds |
|---|---|
| `PlanStore` | Plan run records (mode, options, progress, status, result, including `not_buildable` and `unverified_completed`), per-run artifacts, the meaning revision of every plannable and context node and the chain of consumed build ids ([data-model.md](data-model.md) "Fingerprints"), and stored planner answers keyed by their reuse keys |

The library ships a file implementation (`open_plan_store(root)`), with the same file rules as the build
stores: per-project paths, gzip JSON, atomic writes. One contract test suite runs against every
implementation. Task state lives on `Task` nodes in the graph store, not in the port, so any process that reaches
the database sees it.

## Settings

| Object | Holds |
|---|---|
| `LLMConfig` (core) | Endpoint and models. Planners use the `extraction` call class (main model), the reviewer the `judge` class of the gateway the run is given ([plan.md](plan.md) "Model calls") |
| `PlanOptions.review_model` | Optional Review model override on the gateway the run is given; `None` means the gateway's judge model |
| `PlanOptions` | Options that shape planning ([plan.md](plan.md) "Plan options"); each planner's reuse key holds the options that shape it |
| `PlanRules` | The parsed and validated `plan-rules.yaml`; shipped by default, a host may pass another file |

## Errors

`graph.plan` raises the core errors ([../architecture.md](../architecture.md) "Errors") and one of its own.

| Error | Raised when | Host action |
|---|---|---|
| `ConfigurationError` | Invalid options; a plan run without a spec graph (`no_spec_graph`); a new run while one is unfinished (`unfinished_run`); a gap in the change reports since `Plan.last_build_id` (`change_reports_missing`); run and step calls on a missing or unbound run (`unknown_run`, `unbound_run`, `missing_artifact`); the file store (`invalid_run_id`, `run_not_found`, `run_exists`, `invalid_plan_result`); invalid lifecycle arguments (`invalid_commit`, `invalid_reason`, `invalid_note`, `invalid_claimant`) | Fix configuration or the call; build first; resume the run; pass the missing change reports or run `replan` |
| `PlanRulesError` (a `ConfigurationError`) | Invalid plan rules, including a type the ontology lacks and illegal `IN_AREA` or story targets; carries every problem in `.problems` | Fix the rules file and reload |
| `TaskStateError` (a `GraphError`) | A lifecycle call on a task or story in the wrong state, an unknown task or story, a stale claim token, a claim while a plan persist runs (`plan_applying`), a claim of a task that is not ready (`not_ready`, with the ids of its unfinished prerequisites), or `claim_next` with no ready task in the story (`nothing_ready`, with the blockers) | Re-read the task and retry later; take the listed prerequisites first; usually a lost race or a re-planned task |
| `LLMFatalError` | The endpoint rejects credentials or the model | Fix configuration |
| `InfrastructureUnavailableError` | The graph store, the endpoint or the port backend is unreachable | Retry later |
| `GraphWriteError` | A graph store write failed | Retry; persist resumes where it stopped |
| `ConformanceError` | A plan the run is about to write breaks an invariant (a bug) | Bug |

A non-fatal `LLMError` never reaches the host from a plan run: the planner falls back
([plan.md](plan.md) "Failures"). A spec graph that changed before a resume, while Persist has not started, ends the
run `failed` with `graph_changed` instead of raising; once Persist has started, the run is applied to the end.

## Logging and concurrency

Logs follow the library rules: event names start with `graph.plan.`, and carry ids, counts, codes and
durations, never document, prompt, planner or task text. The API is `async`, except calls
[api.md](api.md) marks sync. The host runs at most one plan run per project at a time, and never a plan
run during a build of the same project; lifecycle calls may run at any time and in parallel, because
each one is a single compare-and-set transaction, and claims pause while a plan persist runs.
