---
type: Questions
title: "Graph plan decisions"
description: "Decision record of the graph.plan specification (Q1-Q21, PT1-PT23)"
status: stable
tags: [graph, plan]
---
# questions.md: decisions of the graph.plan specification (Q1-Q21, PT1-PT23)

The record of the design decisions behind this specification, decided 2026-11-01. Part 1 keeps the
questions of the first draft (Q1-Q21) with their options and the decision taken. Part 2 records the
topics raised when the draft was checked against build ontology v2.0 and against agent use over MCP
(PT1-PT23). Every spec file is written as decided; this file explains why.

# Part 1. Questions of the first draft

## Q1. Where does the executor live?

The executor claims a task, runs coding agents (tests, implementation, validation) in a git workspace and
reports the commit. The agent runtime is the repository's `llm` package (`CodingAgent`, validated agent
DAG, git progress detection). Import rule I1 of the library allows no such dependency in `graph.*`.

| Option | Effect |
|---|---|
| A. Host package (for example `backend/src/plan_executor/`), outside `graph`, using `llm`; `graph.plan` defines only the lifecycle API and the executor contract | `graph` stays a pure library; the executor gets its own short spec; the API service or a Temporal worker can run it |
| B. `graph.plan.execute` inside the library, with an I1 exception for `llm` | One place for planning and execution; breaks the library's dependency rule and ties `graph` to the agent runtime |
| C. No executor in this release; the lifecycle API is used by tests and by hand | Smallest; nothing runs a plan end to end |

**Decision (2026-11-01): superseded by PT23.** No executor is in scope. Users connect their own coding
agents to the app over MCP; the MCP server (host code) exposes the plan's reads and lifecycle calls as
tools. The draft's "Executor contract" becomes the "Agent contract" ([lifecycle.md](lifecycle.md)).

## Q2. How are work areas found?

| Option | Effect |
|---|---|
| A. Spine-based assignment: one area per internal module and a project area; nodes follow containment to the nearest module, then grouping, then references; cross-cutting and unplaced nodes go to the project area | Deterministic, explainable, no dependency; uses the strong spine of the ontology (`PART_OF` is exclusive). A huge module is one area, planned in parts |
| B. Community detection by modularity over the spec graph (`networkx`), with post-passes for capabilities, deployments and singletons | Finds clusters the spine does not state; needs a new dependency (I1), name stabilisation across runs, and still needs the spine to name areas |
| C. A, plus a split of a large module into sub-areas by top-level feature | Smaller areas and more parallel work; area ids depend on features, which change more often than modules |

**Decision (2026-11-01): A, with the v2.0 placement of PT4-PT6.** Interfaces and data entities are placed
first (provider, single consumer or integrator, single storing module), members follow their owner.

## Q3. One living plan, or a new plan per run?

| Option | Effect |
|---|---|
| A. One `Plan` per project, kept current by plan runs, like the spec graph is kept current by builds; history in run records and task revisions | Stable task ids; simple reads ("the plan"); no shared nodes between plan versions |
| B. A new `Plan` node per run (named by fingerprint) that supersedes the previous one; tasks carried over are members of both | Explicit plan versions; areas and tasks shared across versions need extra rules to keep their identity stable |

**Decision (2026-11-01): A.**

## Q4. How far does a spec change cascade to finished work?

When the spec behind a completed task changes, the task gets a new revision. The question is what
happens to completed tasks that wait for it. Waits come in two kinds: inside an area (written by the
planner) and across areas or from setup and closing work (added by Stitch). Every area's first tasks
wait for the setup tasks, so a cascade along stitch edges from a changed setup task would redo the whole
project.

| Option | Effect |
|---|---|
| A. Full: every completed dependant of a superseded or retired task is superseded too, transitively, along both kinds of edges | Safe; one changed setup or contract task can redo most of the project |
| B. Inside the area: the cascade follows planner edges only; completed dependants in other areas are kept and listed in `affected_dependants` for the host | Rework stays in the changed area; cross-area effects are visible but not automatic |
| C. Interface-based: dependants are superseded only when the interfaces of the changed task's area changed | Less rework; relies on interfaces written by a model, which may change wording without meaning |
| D. None: only tasks whose own spec changed get revisions | Least rework; dependants may silently build on outdated code |

In every option a newly added prerequisite supersedes nothing.

**Decision (2026-11-01): B.** The cascade starts only from tasks superseded by a meaning change in the
build change reports (PT9), so rewording never starts one.

## Q5. Who decides that the spec graph is ready to plan?

`graph.plan` must not import `graph.build`, so it cannot read a build result or run `verify_project`
itself.

| Option | Effect |
|---|---|
| A. The host: it runs a plan only after a build that is settled and verified; the plan refuses only an empty graph | Simple; matches the build's rule that the host decides when to build |
| B. `PlanContext` takes the build result (or its `settled` and `verification.ok`), and the run refuses when they are false | The library enforces the gate; couples the plan API to the build result shape |
| C. The plan runs its own read-only structure checks before planning | Duplicates build verification |

**Decision (2026-11-01): A.** The host decides by the build's settlement (PT10) and passes the build
change reports (PT11). The plan enforces nothing about settlement.

## Q6. Who creates the uniqueness constraints of plan nodes?

The core `ensure_schema` creates indexes for every declared relation (plan relations included), but
uniqueness constraints only for spec nodes, documents and fragments.

| Option | Effect |
|---|---|
| A. `graph.plan` ships `ensure_plan_schema` with its own `graph_plan_*` constraints | No change to `graph.core` or its docs; the host calls two setup functions |
| B. Extend the core `ensure_schema` to create `(group_id, uuid)` and external-id uniqueness for every declared type of any owner | One setup call; a core change (spec and code), which this work was told not to make |

**Decision (2026-11-01): A.** Revisit as B when `graph.gap` needs the same.

**Implementation note (D1, 2026-11-01; please check).** Q6 is refined: `graph.plan` ships its DDL at
`graph/plan/ddl/`, and the core `ensure_schema` discovers submodule DDL by default and therefore
creates the plan objects. `ensure_plan_schema` remains a convenience that creates only the plan
objects; no host needs two setup calls merely because the plan module is installed.

## Q7. Which call classes do the planners use?

The core gateway has four fixed call classes: `extraction`, `small`, `judge`, `embedding`.

| Option | Effect |
|---|---|
| A. Planners (describe, project, tasks) on `extraction` (main model); reviewer on `judge` (judge model, so it can differ from the planner) | No core change; usage reports name the build's class for plan calls |
| B. Add a `planning` call class to the core | Clear usage reports and its own budget; a core change |

**Decision (2026-11-01): A, with a plan-only review model (PT18).** Review runs on the `judge` class of
the plan's own gateway; the host builds that gateway with a plan-only review model setting as its
`judge_model`, so the build's judge steps are not affected.

**Implementation note (D5/D6, 2026-11-01; please check).** Q7 and PT18 are refined: the host builds one
shared gateway for the plan run, and Review calls it on the `judge` class with
`PlanOptions.review_model` as its model override. `None` means the gateway's judge model; there is no
host setting or separate plan gateway.

## Q8. Where do cross-cutting requirements go?

A constraint or quality requirement that applies to several modules ("all services run in the EU
region") reaches several modules by `RELATES_TO` and no module by `PART_OF`.

| Option | Effect |
|---|---|
| A. The project area, which is planned by Tasks like any area (plus the setup and closing tasks of the project planner) | One owner; the work is done once, across modules |
| B. Copy coverage into each related module area (each area plans a task that implements it), while membership stays in the project area | Every module honours it in its own code; the same requirement has several tasks; validation must allow implementing related non-members |

**Decision (2026-11-01): A, refined by PT15.** The project area may return such members as
`not_buildable`, cite them in the project profile's conventions, and is planned in chapter stories.
B stays in [later.md](later.md).

## Q9. Does the plan read inferred edges?

Light inference adds edges a judge accepted (for example attaching an orphan requirement to a module).

| Option | Effect |
|---|---|
| A. Read all domain edges, inferred included | Better placement of requirements the text does not attach; a node may move area when inference changes its mind, and its open task follows it (Diff matches tasks by their `implements` set) |
| B. Read `stated` and `implied` edges only | More stable areas; more `unplaced` requirements in the project area |

**Decision (2026-11-01): as PT8.** All domain edges, inferred included, for placement and briefs; only
`stated` and `implied` edges create stitch waits. Briefs show each edge's basis and confidence.

## Q10. Module inside a module

The ontology allows `module PART_OF module` (a sub-service).

| Option | Effect |
|---|---|
| A. Every internal module anchors its own area; containment walks stop at the nearest module; no extra dependency | Simple; matches `scope`, which stops at another spine root |
| B. A sub-module joins its parent's area | Fewer, larger areas |
| C. Own area, and the parent's root tasks wait for the sub-module's contract tasks | Encodes "the parent needs its parts"; one more stitch rule |

**Decision (2026-11-01): A.**

## Q11. Task size

| Option | Effect |
|---|---|
| A. `task_size` 5 as a warning (`oversized_task`), `planner_members` 40 per call | The planner decides; the metric shows outliers |
| B. `task_size` as a validation error, forcing the planner to split | Smaller tasks; more invalid answers and fallbacks |

**Decision (2026-11-01): A.** Defaults are checked on one measured specflow `first` plan with
`estimate_plan` beside it (PT17).

## Q12. `complexity` or model tiers on tasks?

| Option | Effect |
|---|---|
| A. `complexity` `low`, `medium`, `high`: a property of the work; the agent's client maps it to models | The plan never names models or tiers; a client can map differently |
| B. `TIER1`, `TIER2`, `TIER3` | Closer to how models are configured; mixes the client's concern into the plan |

**Decision (2026-11-01): A.**

## Q13. Is Review on by default?

One extra call per run that changed something; its issues are only reported (Q19).

**Decision (2026-11-01): on.** Turned off in tests and offline runs.

## Q14. Work done for removed requirements

When a requirement is removed after its task was completed, the task is retired and its commit listed
as stale; the code stays.

| Option | Effect |
|---|---|
| A. Report only (`stale_commits`); a human decides | Simple; nothing removes code automatically |
| B. Add a cleanup task to the area that removes or adapts the code | Keeps code and spec in step; the planner must reason about code it cannot see |

**Decision (2026-11-01): A.** B stays in [later.md](later.md).

## Q15. HTTP endpoint and runtime for plan runs

The graph build has `POST/GET /api/v1/projects/{pid}/graph_build` with an in-process runner.

| Option | Effect |
|---|---|
| A. Out of this spec: a short host spec after the library, mirroring `graph_build` (`plan_run` endpoint, runner, file store under the state directory) | This spec stays a library spec, like `docs/graph/` |
| B. Specify the endpoint here | One document set; mixes host and library concerns |

**Decision (2026-11-01): A** ([later.md](later.md)). The MCP tools of PT23 are host code too.

## Q16. Names

`WorkArea` (a part of the system built together) was chosen over `Community` (a graph-clustering term)
and `Component` (often a code-level term). Draft labels: `Plan`, `WorkArea`, `Task`; relations:
`IN_AREA`, `IMPLEMENTS`, `DEPENDS_ON`, `SUPERSEDES`; phases `setup`, `contract`, `data`, `logic`,
`integration`, `quality`, `acceptance`.

**Decision (2026-11-01): keep the names, except `DEPENDS_ON` → `WAITS_FOR` (PT1); add the label
`Story` and the relation `IN_STORY` (PT19).**

## Q17. Installing graph.plan forces a rebuild of every project

An `update` build refuses to run when the merged ontology's labels or relations differ from those its
last full build recorded (`check_frozen`). Shipping the plan ontology file adds the plan labels and
relations to the merged ontology, so every existing project's next `update` build raises
`ConfigurationError("ontology_changed")`.

| Option | Effect |
|---|---|
| A. Accept it: a one-time full build of every project when `graph.plan` is installed, written in the release notes and in `.agents/docs/graph.md` | No change to `graph.build`; every project pays one full extraction |
| B. Change the build to freeze only build-owned labels and relations (a small change in `check_frozen` and build.md "Modes") | No full build; plan and later gap files can change without touching builds. A build spec and code change |

**Decision (2026-11-01): B (PT2).** In build ontology v2.0 the remedy for `ontology_changed` is a drop
and a `first` build, so A would cost a full re-extraction of every project on every plan or gap
ontology change.

## Q18. Which names may `graph.plan` code hold as literals?

The library rule I4 forbids ontology names as code literals, so a renamed ontology needs no code
change. But plan code must name its own labels, relations, task statuses and phases (the lifecycle and
Stitch work on them), and the ontology format gives non-extracted types no role through which code could
find them.

| Option | Effect |
|---|---|
| A. The plan's own names (labels, relations, vocabulary values of the plan ontology file) are fixed by this spec and may be literals in `graph.plan`, like the build's built-in names; I4 and the renamed-ontology test apply to the spec side, reached through the plan rules | Simple; the plan's own model changes only by a spec change |
| B. Add a rules section that maps roles to plan names (statuses, phase roles such as `provides_contract`, `closing`) | Plan names renamable too; more format, checks and indirection for little gain |

**Decision (2026-11-01): A** ([ontology.md](ontology.md) "Names").

## Q19. Should Review change the plan?

| Option | Effect |
|---|---|
| A. Advisory: issues are stored and counted, nothing changes | Simple; no extra resume, cache or part-selection rules |
| B. One revision: an area with a blocking issue is planned once more with the issues, then stitched again | Plans can improve within a run; needs rules for which part is re-planned, how the revised answer is cached and what happens to issues on the project area |

**Decision (2026-11-01): A.** B stays in [later.md](later.md).

## Q20. Areas for shared capabilities

A capability realised by several modules ("Authentication" realised by the Orders service and the Web
shop) could get its own area, so its shared contract is built once and the module areas wait for it.

| Option | Effect |
|---|---|
| A. No capability areas: such a capability, and nodes grouped only under it, go to the project area as cross-cutting work | Simple; `PART_OF` wins over `GROUPED_BY`, so a capability area would usually hold little more than the capability node |
| B. Capability areas (`AREA::CAP_<slug>`) with a stitch rule: module areas that realise the capability wait for its contract tasks | Shared work is explicit and ordered; one more area kind, id form and stitch rule |

**Decision (2026-11-01): A.** In v2.0 capabilities are tags and `REALIZED_BY` is often inferred, which
supports A. B stays in [later.md](later.md).

## Q21. Do deployment and datastore modules anchor work areas?

`Module.type` separates `frontend`, `backend`, `datastore`, `deployment`. A deployment target
("Production cluster") or a datastore ("Orders database") may have few requirements of its own.

| Option | Effect |
|---|---|
| A. Every internal module anchors an area, whatever its type | Uniform; small areas give small, clear tasks (schema and migrations, deployment config) |
| B. Deployment modules join the project area; datastore modules join the area of the module that integrates with them most | Fewer areas; placement depends on integration edges, which are hints |

**Decision (2026-11-01): A, with PT5 and PT4.** Deployment-area tasks wait for the setup sinks and
closing end-to-end tasks wait for the deployment sinks (PT5); data entities go to the area of their
single storing module (PT4).

# Part 2. Decisions after build ontology v2.0

The first draft was written against build ontology v1.3. The topics below record what changed when it
was checked against the v2.0 graph (Interface, DataEntity, OpenQuestion, spec `DEPENDS_ON`, edge
`basis`, settlement, change reports) and against agent use over MCP. Every topic follows the first
recommendation except PT9 and PT11.

## PT1. The plan's `DEPENDS_ON` clashes with the spec `DEPENDS_ON`

**Problem.** v2.0 declares `DEPENDS_ON` (feature → feature, requirement → requirement) in the build
ontology file. The core loader rejects a relation declared in two files, so the draft's task relation
of the same name makes the merged ontology fail to load.

**Decision.** The plan relation Task → Task is `WAITS_FOR` (a task waits for its prerequisite), with the
property `source`: `planner` or `stitch`. No core change.

**Affects:** plan-ontology.yaml, ontology.md, data-model.md, plan.md, lifecycle.md, api.md.

## PT2. Installing the plan ontology blocks every project's update build

**Problem.** `check_frozen` compares every label and relation of the merged ontology, so adding the plan
file makes every project's next `update` build raise `ontology_changed`. In v2.0 the remedy is a drop and
a `first` build, a full re-extraction (Q17).

**Decision.** `graph.build` `check_frozen` compares only build-owned labels and relations (a build
change with its test). Until it lands, installing the plan ontology makes every project's next update
raise `ontology_changed`, whose remedy is drop and `first`.

**Affects:** overview.md (boundaries), ontology.md; outside this spec: build `check_frozen` and its doc.

## PT3. Which v2.0 types are plannable, and in which phase

**Problem.** The draft rules know module, capability, feature and the requirement types. v2.0 adds
Interface, DataEntity and OpenQuestion, and IntegrationRequirements are now owned by interfaces.

**Decision.** Interface: member, phase `contract`, required, anchors a story. DataEntity: member, phase
`data`, required, anchors a story. IntegrationRequirement: phase `integration` (the Interface is the
contract, the requirement is the wiring). OpenQuestion: role `context`, read for briefs, never placed or
planned.

**Affects:** plan-rules.yaml, ontology.md, plan.md.

## PT4. Partition follows the v2.0 spine, not only `PART_OF`

**Problem.** In v2.0 an IntegrationRequirement is `PART_OF` an Interface (`PROVIDED_BY` a module) and a
DataRequirement is `PART_OF` a DataEntity (`STORED_IN` a module). A walk on `PART_OF` alone stops at
the hub and leaves these requirements unplaced.

**Decision.** Hubs are placed first: an Interface goes to its internal provider's area, else to the
single internal module that consumes it or integrates with the provider, else the project area; a
DataEntity goes to its single internal storing module's area, else the project area. Other nodes follow
their owner via containment (an interface or entity owner gives that hub's area). The rules carry the
relation roles `provision`, `consumption`, `storage`, so code holds no spec names.

**Affects:** plan-rules.yaml, plan.md "Partition", requirements.md FR-PPRT.

## PT5. Deployment and datastore modules, `DEPLOYED_TO`

**Problem.** v2.0 marks deployment modules (modules are `DEPLOYED_TO` them) and datastore modules
(entities are `STORED_IN` them). The draft gives every internal module an area (Q21) but has no rule for
ordering deployment work.

**Decision.** Every internal module anchors an area, deployment and datastore modules included.
Deployment-area tasks wait for the setup sinks; closing end-to-end tasks wait for the deployment sinks.

**Affects:** plan.md "Partition" and "Stitch", plan-rules.yaml (role `deployment`).

## PT6. External modules own constraints and provide interfaces

**Problem.** In v2.0 a Constraint may be `PART_OF` an external module ("Stripe limits charges to …") and
an Interface may be `PROVIDED_BY` one. External modules are excluded from planning, so these walks
dead-end.

**Decision.** Work owned by an external module, and an interface it provides, goes to the area of the
single internal module that consumes its interfaces or integrates with it; with none or several, the
project area.

**Affects:** plan.md "Partition", requirements.md FR-PPRT.

## PT7. Directed stitch from explicit v2.0 edges

**Problem.** The draft stitch treats every integration between two areas symmetrically. v2.0 states
direction (`PROVIDED_BY`, `CONSUMED_BY`, directed `INTEGRATES_WITH`) and states dependencies (spec
`DEPENDS_ON`).

**Decision.** Directed stitch, in order: root tasks wait for setup sinks; a consumer area's integration
tasks (else logic) wait for the provider area's contract tasks implementing the interface; an
integration edge without an interface makes the caller's integration (else logic) tasks wait for the
callee's contract tasks, else its non-acceptance sinks; tasks implementing the source of a spec
dependency edge wait for tasks implementing its target; data users in a service area wait for the
datastore area's data task of the entity; deployment rules (PT5); no acceptance-to-acceptance wait
across areas; closing tasks wait for the sinks of the areas they implement, or of every area. A cycle
drops the last-added stitch edge, counted `dropped_dependencies`.

**Affects:** plan.md "Stitch", requirements.md FR-PDAG.

## PT8. Inferred edges: placement yes, ordering no

**Problem.** v2.0 inference adds many edges (about 677 inferred owners on specflow at about .93-.97
precision, plus cross-document references and cue-gated dependencies), each with `basis: inferred` and a
`confidence`. A wrong inferred dependency would add a wrong wait that silently blocks parallel work.

**Decision.** All domain edges, inferred included, are used for placement and briefs; only `stated` and
`implied` edges create stitch waits. Briefs show each edge's basis and confidence. (Q9.)

**Affects:** plan.md "Snapshot", "Partition", "Stitch"; lifecycle.md "Task brief".

## PT9. What decides that finished work must be redone

**Problem.** The draft's node fingerprint covers every stored attribute. In v2.0 re-extraction rewords
the `stmt` of unchanged lines, so every completed task on such a node would be superseded and its
commit listed stale although nobody changed the text.

**Decision: B (the build change reports), not the first recommendation A (a plan-side fingerprint
without `stmt` and name).** A completed task is superseded only when the change reports since the plan's
last consumed build class one of its implemented nodes as a meaning change (`added`, `removed`,
`merged`, `carried_over`, `rekeyed`, `changed_meaning`). `changed_evidence` never supersedes. Each node's
meaning revision is the build id of its last meaning change; task property `spec_build_id` records the
consumed build when the task's spec was last planned. Node fingerprints are no longer used for
decisions; the graph fingerprint stays only as a guard (resume, `outdated`).

Consequences of choosing B (shared with PT11):
- **Dependency on build precision.** Plan update correctness follows the build's change-report
  classification. A node wrongly classed `changed_meaning` supersedes finished work that did not
  change; a real change classed `changed_evidence` leaves finished work in place. On TEST.5c updates the
  classification agreed .84 (specflow) and .55 (stitch) with labelled nodes. The build's work on
  rewording during re-extraction is a prerequisite for trusting plan updates; until then the result
  names, per superseded task, the change class that caused it, so the host can check it.
- **`change_reports_missing`.** The plan needs the reports of every build since its last consumed
  build; a gap raises `ConfigurationError("change_reports_missing")`.
- **Replan without reports.** A `replan` without reports re-plans every area, supersedes a completed
  task only when an implemented node no longer resolves, and lists in `unverified_completed` the
  completed tasks whose implemented node was written by a build after the plan's last consumed build.

**Affects:** data-model.md (`spec_build_id`, `Plan.last_build_id`, fingerprints), plan.md (Snapshot,
Diff, Reuse, result), api.md, lifecycle.md (`spec_changed`), requirements.md FR-PRUN, FR-PDIF, FR-PCHG.

## PT10. When is the graph ready to plan?

**Problem.** v2.0 reports `settlement`: settled, or not settled with reasons `documents_incomplete`,
`inference_backlog` or `inference_rule_failed`. After an update an inference backlog is common and drains
over the next builds.

**Decision.** The host runs a plan after a build has returned, unless its settlement lists
`documents_incomplete` (missing documents give missing tasks, which is worse than late placement). The
plan itself enforces nothing about settlement. (Q5.)

**Affects:** overview.md, plan.md "Modes".

## PT11. Does the plan read the build's change report?

**Problem.** The draft needs no build result: its own fingerprints show what changed. v2.0 produces a
classified change report per build (added, removed, merged, carried_over, rekeyed, changed_meaning,
changed_links, changed_evidence).

**Decision: C (the change reports decide what is re-planned), not the first recommendation A
(fingerprints only, reports later as explanation).** The host passes `PlanContext.changes`: the change
reports of every build since the plan's last consumed build, in build order, converted to the plan-side
model `SpecChanges` {build_id, nodes: [{spec_id, classes, previous_spec_ids}], edges: [{source, relation,
target, change: added|removed|changed, basis}]}. `graph.plan` never imports `graph.build`; the model is a
documented contract with the host. Meaning changes are as in PT9; link changes are `changed_links` and
both ends of every changed edge touching a plannable node, inferred included; `changed_evidence` is
ignored. Reuse keys hold member `spec_id`s with their meaning revisions, never text or node
fingerprints. An `update` stops after Snapshot when there is no meaning or link change and nothing open.
`Plan.last_build_id` records the last consumed build. A brief's `spec_changed` is a conservative hint:
an implemented node's `last_build_id` is newer than `Plan.last_build_id`.

Consequences of choosing C (with PT9): the same dependency on the build's classification precision; a
gap in the chain raises `change_reports_missing` and the host passes the missing reports or runs
`replan`; a `replan` without reports reports `unverified_completed`. The build API exposes no change
report yet, so the host plan runner reads them from the build store or the build status response gains
them (an additive build change).

**Affects:** api.md (`PlanContext.changes`, `SpecChanges`, errors), plan.md, data-model.md,
lifecycle.md, overview.md, requirements.md FR-PRUN, FR-PCHG.

**Implementation note (D6, 2026-11-01):** the plan-side model is named `BuildChangeReport`, with
`ChangedSpecNode` and `ChangedSpecEdge` items.

## PT12. Out-of-scope nodes and releases

**Problem.** v2.0 has `in_scope: no`, which also excludes a requirement through its owner, and `release`
("MVP", "Phase 2"), inherited from the feature when null. The draft excludes by a node's own values only
and has no notion of release.

**Decision.** Nodes with `in_scope: no`, and nodes whose owner (target of the containment edge) has it,
are excluded. `release` is carried on tasks (the distinct releases of the implemented nodes, inherited
via containment when null) and on stories (the anchor's), and shown in briefs and story lists. No release
filter and no release ordering (both in [later.md](later.md)): release labels are verbatim, not ranked.

**Affects:** plan-rules.yaml, data-model.md, plan.md, lifecycle.md, api.md.

## PT13. Open questions

**Problem.** v2.0 extracts stated open questions (`RELATES_TO` the nodes they concern). A task can
implement a requirement an open question puts in doubt.

**Decision.** Open questions are context: the brief lists those that relate to an implemented node; the
task flag `has_open_questions` and the metric `open_questions_on_tasks` show them. Tasks are not held
back (that comes with `graph.gap`).

**Affects:** plan-rules.yaml, data-model.md, lifecycle.md, plan.md "Check".

## PT14. `done_when` when the spec has no acceptance criteria

**Problem.** v2.0 keeps `acceptance_criteria` only when the text states them; generating criteria is
plan-owned. Most requirements have none.

**Decision.** `done_when` is a list of {text, source}; `source` is `spec` (from `acceptance_criteria`)
or `planner`. Briefs show the difference.

**Affects:** plan-ontology.yaml (`done_when_source`), data-model.md, plan.md "Tasks", lifecycle.md.

## PT15. A large project area, and requirements that are not code work

**Problem.** On specflow 34% of requirements (711) have no owner; most of them are correctly unowned
governance, release-gate, policy and convention rules. In the draft each would need a project-area task,
many of which are not code.

**Decision.** The Tasks planner may return a member as `not_buildable` {spec_id, reason, note} with a
reason from `not_buildable_reason` (`process`, `governance`, `release_gate`, `policy_only`). The
project profile gains `conventions: [{text, spec_ids}]`. Coverage (PD3) accepts a required node that is
implemented by a task, recorded `not_buildable`, or cited by a profile convention; the result counts
`not_buildable`. The project area is planned in parts made of chapter stories.

**Affects:** plan-ontology.yaml, data-model.md, plan.md, lifecycle.md "Plan checks".

## PT16. Area interfaces vs. spec Interface nodes

**Problem.** The draft's area interfaces are written by the describer model. v2.0 extracts Interface
nodes with providers and consumers.

**Decision.** Area interfaces are the spec Interfaces the area provides (name, kind, `spec_id`,
deterministic). The describer writes a summary per interface and may add interfaces the text implies
but names none, marked `planned_by: model`.

**Affects:** data-model.md, plan.md "Describe".

## PT17. Scale, cost and time

**Problem.** The draft is sized for the shop example. Specflow v2.0 has 2103 requirements; with
`planner_members` 40 a `first` plan needs about 60-80 Tasks calls, each with large output.

**Decision.** `estimate_plan(context)` (calls and input tokens of a plan run, without model calls) is
part of the API now. Defaults stay (`task_size` 5, `planner_members` 40) until one measured specflow
`first` plan.

**Affects:** api.md, plan.md "Model calls", later.md.

## PT18. Review is not a second opinion by default

**Problem.** Review runs on the `judge` call class, but the host sets no separate judge model, and the
build's judge steps were calibrated on the main model.

**Decision.** Planners run on `extraction`, Review on `judge` of the plan's own gateway; the host builds
that gateway with a plan-only review model setting (for example `GRAPH_PLAN_REVIEW_MODEL`) as
`judge_model`. (Q7.)

**Affects:** architecture.md (settings), plan.md "Review".

**Implementation note (D5, 2026-11-01; please check).** PT18 is refined: the host builds one shared
gateway for the plan run, and Review calls it on the `judge` class with `PlanOptions.review_model` as
its model override. `None` means the gateway's judge model; there is no `GRAPH_PLAN_REVIEW_MODEL`
host setting.

## PT19. A story level between areas and tasks

**Problem.** Users pick work by feature. An area of a large module holds dozens of tasks, too coarse to
pick from and too fine to track.

**Decision.** An explicit `Story` node, created deterministically by Partition: one per feature (nested
features too), per interface and per data entity of the area, `CORE` for requirements a module owns
directly, and in the project area chapter stories, `SETUP` and `CLOSING`. Every plannable node has one
home story. Ids follow the anchor (`STORY::<area slug>::<SLUG>`); a story whose anchor is gone is
`retired` and returns active under its id. Title = anchor name; Describe adds a summary. The Tasks
planner names each task's story; planning parts are built from whole stories. New invariant PD7: every
open task is `IN_STORY` exactly one active story of its own area. Story progress (`pending`,
`in_progress`, `blocked`, `completed`) is derived from its tasks at read time, never stored.

**Affects:** plan-ontology.yaml, plan-rules.yaml (`story: true`), ontology.md, data-model.md, plan.md,
api.md, lifecycle.md, requirements.md FR-PSTY.

## PT20. Story and area links for context retrieval

**Problem.** Agents and users need context at area, story and task level; only tasks linked to spec
nodes.

**Decision.** `Story IMPLEMENTS` its anchor (feature, interface or data entity). Context: area → module
and members; story → anchor and all its members, including uncovered ones; task → the brief.

**Affects:** plan-ontology.yaml, data-model.md, api.md.

## PT21. The plan as a DAG, and story order

**Problem.** Users want the plan as a DAG and stories in order, but stories aggregate tasks of several
phases, so story order can cycle.

**Decision.** The task graph (`WAITS_FOR`) is the DAG (PD4). Story order is a projection (story A waits
for B when a task of A waits for a task of B); cycles are allowed, collapsed for display and never
enforced. `plan_dag(project_id, format)` returns `json` or `mermaid`: tasks in topological order,
grouped by story.

**Affects:** api.md, lifecycle.md.

## PT22. Claim rules for MCP agent pools

**Problem.** Many agents claim work over MCP, and a user may tell an agent "implement story X" before
X's prerequisites are done.

**Decision.** Only ready tasks can be claimed. A refused claim raises `TaskStateError("not_ready")`
carrying the ids of the unfinished prerequisites. `story_work(project_id, story_id)` returns the story's
open tasks plus their unfinished transitive prerequisites in topological order, with claim holders.
`claim_next(project_id, story_id, *, claimed_by)` claims the story's first ready task in topological then
`task_id` order, or raises `TaskStateError("nothing_ready")` with the blockers. No forced claims
(option C, rejected); `release_task(force=True)` for crash recovery stays.

**Affects:** api.md, lifecycle.md, requirements.md FR-PMCP.

## PT23. Agents over MCP instead of an executor

**Problem.** The draft assumed a host executor (Q1). Users instead connect their own agents over MCP;
the plan should offer what MCP tools need, nothing more.

**Decision.** No executor in scope. The MCP server (host code) exposes the plan's calls as tools:
reading `list_areas`, `list_stories(area?, status?, release?)`, `story`, `story_work`,
`ready_tasks(story?, area?)`, `task_brief`, `plan_dag`; working `claim_task`, `claim_next`,
`complete_task(commit)`, `block_task(reason)`, `fail_task`, `release_task`. The "Agent contract" states
what MCP agents are told: report only the commit that holds the work; block on unclear spec; release on
`spec_changed` or `missing`; never start from a stale commit. Commit existence checks are host-side and
optional.

**Affects:** overview.md, how-it-works.md, lifecycle.md "Agent contract", api.md, later.md.
