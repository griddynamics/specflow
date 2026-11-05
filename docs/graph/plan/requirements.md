---
type: Requirements
title: "Graph plan requirements"
description: "Requirement units of graph.plan, functional and non-functional"
status: stable
tags: [graph, plan]
---
# requirements.md: requirement units of graph.plan (compact form), functional and non-functional

Each row is one testable requirement in EARS form with one acceptance criterion. Status of every unit:
Draft (2026-11-01, decisions applied), pending implementation. Verification is by test unless the row
says otherwise. Details live in the linked file of each area; the reasons are in
[questions.md](questions.md).

"Shop graph" is the synthetic fixture `tests/fixtures/graph/plan/shop.yaml`. End-to-end update scenarios
use the shop-v2 corpus with build ontology v2.0. Its
modules are "Orders service", "Payments service", "Web shop", the datastore "Orders database" and the
external "Stripe". The Interface "Orders API" is `PROVIDED_BY` "Orders service" and `CONSUMED_BY` "Web
shop"; the DataEntity "Order" is `STORED_IN` "Orders database"; the Interface "Stripe API" is provided by
"Stripe" and consumed by "Payments service". Area ids drop the module's spec id prefix
(`AREA::ORDERS_SERVICE`), story ids drop the anchor's (`STORY::ORDERS_SERVICE::ORDERS_API`).

Dependencies on `graph.build` (not units of this spec): `check_frozen` compares only build-owned labels
and relations (PT2); the host passes the build change reports and reads the build settlement (PT10,
PT11). Plan update correctness follows the precision of the build's change-report classification (PT9).

## Ontology, rules, setup and ownership ([ontology.md](ontology.md), [architecture.md](architecture.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PONT-01 | `load_ontology()` shall merge the shipped plan ontology file with the build file when `graph.plan` is installed. | When called without arguments with build ontology v2.0, then the ontology loads, has `Plan`, `WorkArea`, `Story`, `Task` owned by `plan`, the relations `IN_AREA`, `IN_STORY`, `IMPLEMENTS`, `WAITS_FOR`, `SUPERSEDES` owned by `plan`, and the spec relation `DEPENDS_ON` owned by the build. |
| FR-PONT-02 | `load_plan_rules` shall validate the rules against the merged ontology and list every problem in one `ConfigurationError`. | Given rules naming an unknown type and a type that is not a legal `IN_AREA` source, then the error names both. |
| FR-PONT-03 | `graph.plan` shall plan a spec graph of any valid build ontology without code changes, given rules that name its types and relations. | Given the shop corpus built with the shipped ontology and with a fully renamed copy, and rules adjusted to the renamed copy, then the two plans map onto each other. |
| FR-PONT-04 | `ensure_plan_schema` shall create every `graph_plan_*` constraint and index and be idempotent. | Given a database where the core `ensure_schema` has not created the plan objects, when it runs twice, then the second run returns `()`. |
| FR-PONT-05 | `load_plan_rules` shall accept the roles `area_anchor`, `shared`, `member` and `context`, the relation roles of the rules file, and `story: <kind>` (`feature`, `interface` or `entity`) only on `member` types that are legal `Story IMPLEMENTS` targets. | Given the shipped rules, then `module` is `area_anchor`, `interface` and `data_entity` are required members with `story: true`, `open_question` is `context`; given `story: true` on `functional_requirement`, then a `ConfigurationError` names it. |
| FR-PONT-06 | The plan's own names (labels, relations, vocabulary values of the plan ontology file) may be literals in `graph.plan`; spec-side names shall be reached only through the plan rules. | Inspection: the structure test finds no spec type or relation name as a literal in `graph.plan`. |
| FR-POWN-01 | `graph.plan` shall write only the types its ontology file declares and never set a property of a spec node, document or fragment. | Given a full plan run and lifecycle calls on the shop graph, then every spec node, document and fragment property is unchanged. |
| FR-POWN-02 | Every plan query shall bind and filter on `project_id`. | Given two projects with the same spec graph, when project A is planned and its tasks claimed, then nothing of project B is read or changed. |
| FR-POWN-03 | The `PlanStore` file implementation shall pass the port contract suite. | The contract suite runs against `open_plan_store`. |
| FR-POWN-04 | `graph.plan` shall never import `graph.build`; build change reports shall reach it only as the plan-side `BuildChangeReport` model. | Inspection: the structure test finds no import of `graph.build` under `graph.plan`. |

## Plan runs ([plan.md](plan.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PRUN-01 | When a project has no `Plan` node, the library shall choose mode `first` and plan every area. | Given the shop graph without a plan, then every area is described and planned and the plan is persisted. |
| FR-PRUN-02 | When the passed change reports hold no meaning or link change of any node and the previous run left nothing open, an `update` run shall end after Snapshot with no model call and no plan change. | Given a second run after a build whose report classes every node `changed_evidence`, then the gateway records no call and `unchanged` is true. |
| FR-PRUN-03 | In an `update` run, Describe and Tasks shall call a model only for areas and planning parts that hold a node with a meaning or link change, or whose Tasks key changed. | Given a report with one requirement of the Orders service classed `changed_meaning`, then Describe and Tasks call the model for `AREA::ORDERS_SERVICE` only. |
| FR-PRUN-04 | Each step shall store its output so that an interrupted run resumes at its first unfinished step without repeating a planner call. | Given a failure after Tasks, when the run resumes, then no planner call is repeated and the result equals an uninterrupted run. |
| FR-PRUN-05 | If the graph fingerprint changed since the run's Snapshot and Persist has not started, then a resume shall end the run `failed` with `graph_changed`. | Given a build between the failure and the resume, then the run is `failed` and nothing more is written. |
| FR-PRUN-06 | When persist is interrupted, re-running it shall continue from its cursor without duplicating writes. | Given a failure after batch 2, then the persisted plan equals an uninterrupted run. |
| FR-PRUN-07 | While a persist is in progress, `ready_tasks` shall return nothing and `claim_task` and `claim_next` shall raise `TaskStateError("plan_applying")`. | Given a persist stopped after its first batch, then no task is ready and a claim fails; after it finishes, ready tasks return. |
| FR-PRUN-08 | A `replan` run shall plan every area again without answers of earlier runs, keeping task ids and completed work as an `update` does. | Given a `replan` with complete change reports of an unchanged graph whose planner returns the same actions, then every completed task is kept with its commit. |
| FR-PRUN-09 | When a run is unfinished, starting a new run shall raise `ConfigurationError("unfinished_run")`. | Given a run stopped after Describe, then a new run is refused and names it. |
| FR-PRUN-10 | If the endpoint rejects credentials or the model, then the step shall fail, the run stay unfinished (resumable) and nothing be written to the plan. | Given a 401 in Tasks, then `LLMFatalError` is raised and the plan is unchanged. |
| FR-PRUN-11 | The plan result shall report status, mode, changes (with the change class behind each superseded task), stale commits, `unverified_completed`, planning counts (including `not_buildable`), checks and metrics, naming only ids. | Given any finished run, then every field is present and contains no planner or document text. |
| FR-PRUN-12 | `estimate_plan(context)` shall return the calls and input tokens per step of the run `run_plan` would make, without a model call and without writing. | Given the shop graph without a plan, then the gateway records no call, the plan is unchanged, and the estimated Tasks calls equal the number of planning parts. |

## Change reports ([plan.md](plan.md) "Snapshot", "Diff")

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PCHG-01 | Snapshot shall build the change set from `PlanContext.changes` in build order: meaning changes from `added`, `removed`, `merged`, `carried_over`, `rekeyed`, `changed_meaning`; link changes from `changed_links` and both ends of every changed edge, inferred edges included; `changed_evidence` ignored. | Given two reports, the first adding an inferred `PART_OF` to an orphan requirement and the second rewording "Order" (`changed_evidence`), then the change set holds a link change of the requirement and its new owner and nothing for "Order". |
| FR-PCHG-02 | When a build only rewords text (every changed node `changed_evidence`), the plan run shall make no model call and supersede no task. | Given a completed task implementing a requirement whose `stmt` the build reworded, then the task keeps its id, status and commit and the gateway records no call. |
| FR-PCHG-03 | When a report classes a node implemented by a completed task `changed_meaning`, the run shall supersede the task and name the class in the result. | Given "Order" classed `changed_meaning` and its completed data task, then the task is `superseded`, a `pending` revision exists and the result names `changed_meaning` for it. |
| FR-PCHG-04 | A link change alone shall re-plan the affected area and never supersede a completed task. | Given a report whose only change is a `changed_links` on a requirement of a completed task, then the Orders service area is planned again and the task keeps its commit. |
| FR-PCHG-05 | Each node's meaning revision shall be the build id of its last meaning change, reuse keys shall hold member `spec_id`s with meaning revisions, and a task's `spec_build_id` shall record the consumed build when its spec was last planned. | Given a meaning change in build 3 after a plan that consumed build 2, then the node's meaning revision is 3, the Tasks key of its part changed, and its new revision has `spec_build_id` 3. |
| FR-PCHG-06 | A spec id merged or rekeyed in one report and changed in a later one shall resolve to the surviving node through the registry aliases. | Given requirement A merged into B in build 2 and B classed `changed_meaning` in build 3, then the task that implemented A is matched to B and superseded once. |
| FR-PCHG-07 | After a persisted run, `Plan.last_build_id` shall be the build id of the last report consumed. | Given reports of builds 4 and 5, then after the run `Plan.last_build_id` is 5. |
| FR-PCHG-08 | If the passed reports do not cover every build since `Plan.last_build_id`, then an `update` run shall raise `ConfigurationError("change_reports_missing")` naming the gap, before any write. | Given a plan at build 2 and reports of builds 4 and 5 only, then the error names build 3 and the plan is unchanged. |
| FR-PCHG-09 | A `replan` without reports shall re-plan every area, supersede a completed task only when an implemented node no longer resolves, and list in `unverified_completed` every completed task with an implemented node whose `last_build_id` is newer than `Plan.last_build_id`. | Given a plan at build 2, two builds without reports, one completed task whose requirement was rewritten in build 3 and one whose requirement was deleted, then the first is kept and listed in `unverified_completed` and the second is retired with its commit stale. |

## Partition ([plan.md](plan.md) "Partition")

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PPRT-01 | The partition shall put every plannable node in exactly one work area. | Given the shop graph, then each plannable node has one `IN_AREA`. |
| FR-PPRT-02 | Each internal module shall anchor its own area, deployment and datastore modules included; external modules shall be in no area. | Given the shop graph, then `AREA::ORDERS_SERVICE` and `AREA::ORDERS_DATABASE` exist and "Stripe" has no `IN_AREA`. |
| FR-PPRT-03 | An Interface shall go to the area of its internal provider; else to the single internal module that consumes it or integrates with its provider; else to the project area. | Given the shop graph, then "Orders API" is in `AREA::ORDERS_SERVICE` and "Stripe API" is in `AREA::PAYMENTS_SERVICE`; given an interface without a provider consumed by two internal modules, then it is in `AREA::PROJECT`. |
| FR-PPRT-04 | A DataEntity shall go to the area of the single internal module that stores it; else to the project area. | Given the shop graph, then "Order" is in `AREA::ORDERS_DATABASE`; given an entity stored in two internal modules, then it is in `AREA::PROJECT`. |
| FR-PPRT-05 | A capability realised by exactly one internal module shall join that module's area; otherwise it shall join the project area. | Given "Authentication" realised by "Orders service" and "Web shop", then it is in `AREA::PROJECT` and counted `cross_cutting`. |
| FR-PPRT-06 | Any other node shall take the area of the nearest anchor its containment edges reach within 3 hops, an interface or entity owner giving that hub's area, ties to the lowest `spec_id` counted `ambiguous_membership`. | Given a requirement `PART_OF` a feature `PART_OF` "Orders service", then it is in `AREA::ORDERS_SERVICE`; given an integration requirement `PART_OF` "Orders API", then it is in `AREA::ORDERS_SERVICE`; given a data requirement `PART_OF` "Order", then it is in `AREA::ORDERS_DATABASE`. |
| FR-PPRT-07 | A node owned by an external module shall take the area of the single internal module that consumes that module's interfaces or integrates with it; else the project area. | Given the constraint "Stripe limits charges to …" `PART_OF` "Stripe", then it is in `AREA::PAYMENTS_SERVICE`. |
| FR-PPRT-08 | A node no containment places shall take its grouping capability's area, else the area of the single internal module its reference edges reach, else the project area. | Given "All services run in the EU region" related to three modules, then it is in `AREA::PROJECT` (`cross_cutting`); given a requirement reaching no module, then it is there too and counted `unplaced`. |
| FR-PPRT-09 | A node with `in_scope: no`, or whose owner has `in_scope: no`, shall not be plannable. | Given a feature "Gift cards" with `in_scope: no` and its requirements without the attribute, then neither the feature nor its requirements have an `IN_AREA` or a task. |
| FR-PPRT-10 | Placement shall use every domain edge, inferred included; nodes of a `context` type shall never be placed. | Given an orphan requirement with an inferred `PART_OF` to "Web shop", then it is in `AREA::WEB_SHOP`; given an OpenQuestion, then it has no `IN_AREA`. |
| FR-PPRT-11 | The partition shall be deterministic and an area id shall stay fixed for its anchor. | Given the same graph twice, then the partitions are equal; given the anchor module renamed by build (same `spec_id`), then the area id is unchanged. |
| FR-PPRT-12 | When an area's anchor is gone or merged into another anchor, the area shall become `retired`, and active again under its id when the anchor returns. | Given build merges two modules, then the absorbed module's area is retired and its members move to the survivor's area. |
| FR-PPRT-13 | After build merges two plannable nodes, the next plan run shall leave the survivor with exactly one `IN_AREA` and no duplicate `IMPLEMENTS`. | Given two requirements merged by build, each with an `IN_AREA` and an `IMPLEMENTS`, then after the run the survivor has one of each per task. |

## Stories ([plan.md](plan.md) "Partition", [data-model.md](data-model.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PSTY-01 | Partition shall create, without a model call, one story per feature (nested features included), per interface and per data entity of the area, a `CORE` story for requirements a module owns directly, and in the project area one story per source chapter of its other work plus `SETUP` and `CLOSING`. | Given the shop graph, then `STORY::ORDERS_SERVICE::ORDERS_API`, `STORY::ORDERS_DATABASE::ORDER`, one story per Web shop feature, `STORY::ORDERS_SERVICE::CORE`, `STORY::PROJECT::SETUP` and `STORY::PROJECT::CLOSING` exist. |
| FR-PSTY-02 | Every plannable node except an area anchor shall have exactly one home story: its feature, interface or entity story, else `CORE`, else a chapter story in the project area, else `SETUP`. | Given a requirement `PART_OF` a nested feature `PART_OF` a feature, then its home story is the nested feature's story; given "All services run in the EU region", then its home story is a `CH_` story of `AREA::PROJECT`. |
| FR-PSTY-03 | A chapter story id shall be `CH_<document title slug>_<chapter heading slug>`, slugged like any story id (at most 5 words), and stay fixed while the document title and heading are unchanged. | Given a chapter whose slugs exceed 60 characters, then the story id is cut to its slug and equal across two runs. |
| FR-PSTY-04 | Every open task shall be `IN_STORY` exactly one active story of its own area (PD7). | Given the plan of the shop graph, then each open task has one `IN_STORY` to an active story whose area equals the task's area; setup and closing tasks are in `SETUP` and `CLOSING`. |
| FR-PSTY-05 | A feature, interface or entity story shall `IMPLEMENTS` its anchor; `CORE`, chapter, `SETUP` and `CLOSING` stories shall implement nothing. | Given the shop graph, then `STORY::ORDERS_SERVICE::ORDERS_API` `IMPLEMENTS` "Orders API" and `STORY::ORDERS_SERVICE::CORE` has no `IMPLEMENTS`. |
| FR-PSTY-06 | When a story's anchor is gone, the story shall become `retired`, and active again under its id when the anchor returns. | Given a feature removed and restored, then its story is `retired` after the first run and `active` with the same id after the second. |
| FR-PSTY-07 | Story progress shall be derived at read time from its open and completed tasks, never stored: `completed` when every task is completed and there is at least one, else `blocked` when any is blocked, else `in_progress` when any is in progress or completed, else `pending`. | Given a story with one completed and one pending task, then its progress is `in_progress`; when a plan run supersedes the completed task, then it is `pending` with no story write. |
| FR-PSTY-08 | A story shall carry its anchor's `release`, and a task the distinct releases of its implemented nodes, inherited via containment when null. | Given a feature with `release` "MVP" and a requirement without one, then the feature's story and the requirement's task carry "MVP". |
| FR-PSTY-09 | Planning parts shall be built from whole stories, packing small stories up to `planner_members`; a story larger than `planner_members` shall be split in `spec_id` order. | Given an area with stories of 10, 15, 30 and 90 members and `planner_members` 40, then no story except the 90-member one is split across parts, and that one spans 3 parts. |
| FR-PSTY-10 | Describe shall add a `summary` to every story of a described area; the story title shall be its anchor's name. | Given a first run, then every feature story has the feature's name as title and a non-empty summary. |

## Planners ([plan.md](plan.md) "Describe" to "Review")

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PPLN-01 | Every planner answer shall be validated before use, and an invalid answer shall be sent back once with its problems. | Given a task answer that omits a required member, then a second call carries the problem list. |
| FR-PPLN-02 | If an answer is still invalid, or the call fails with a non-fatal `LLMError`, for an area with no current tasks, then the run shall use rules tasks for it and end `partially_succeeded`. | Given a first run whose Tasks planner always answers malformed JSON for one area, then that area gets rules tasks with `planned_by = rules`, each in a story of the area, and the other areas keep model tasks. |
| FR-PPLN-03 | If a planner fails for an area that has current tasks, then the run shall keep those tasks and add rules tasks only for uncovered required members. | Given an update run whose Tasks call fails for the Orders service area after one requirement was added, then the area's completed tasks are kept and one rules task implements the new requirement. |
| FR-PPLN-04 | A fallback answer shall never be stored for reuse. | Given a fallback in run 1 and an unchanged graph, then run 2 calls the model for that area again. |
| FR-PPLN-05 | The project planner shall return the project profile with `conventions` (each with the `spec_id`s it rests on), setup tasks with exactly one setup root, and closing tasks. | Given an answer with two setup tasks without waits, then it is invalid. |
| FR-PPLN-06 | The project area shall be planned by Tasks like any other area, in parts made of its chapter stories. | Given "All services run in the EU region" in the project area, then a project-area task implements it, it is recorded `not_buildable`, or a profile convention cites it. |
| FR-PPLN-07 | The Tasks planner shall name for each task one `story` of the part's stories; an answer naming another story shall be invalid. | Given an answer that puts a task in a story of another part, then the answer is sent back with that problem. |
| FR-PPLN-08 | The Tasks planner may return a member as `not_buildable` with a reason from `not_buildable_reason` and a note; the run shall record it and count it. | Given a release-gate rule returned `not_buildable` with reason `release_gate`, then no task implements it, coverage accepts it and the result counts 1 `not_buildable`; given reason `unknown`, then the answer is invalid. |
| FR-PPLN-09 | Each `done_when` item shall carry `source` `spec` when taken from the node's `acceptance_criteria`, else `planner`. | Given a requirement with two acceptance criteria and one without, then the first task's `done_when` has two `spec` items and the second's only `planner` items. |
| FR-PPLN-10 | Area interfaces shall be the spec Interfaces the area provides (name, kind, `spec_id`); the describer shall only add summaries and interfaces the text implies but names none, marked `planned_by: model`. | Given the Orders service area, then its interfaces list "Orders API" with its `spec_id` whatever the describer answers. |
| FR-PPLN-11 | Every interface an area provides shall be implemented by a `contract` task and every data entity by a `data` task. | Given an answer for the Orders service area without a task implementing "Orders API", then it is invalid. |
| FR-PPLN-12 | Review issues shall be stored and counted and change nothing; a failed review call shall be counted as `review_failed`. | Given a reviewer that reports a blocking issue, then the plan equals the plan without review and the result counts the issue. |
| FR-PPLN-13 | Planners shall call the `extraction` class and Review the `judge` class of the gateway the plan run is given, with `PlanOptions.review_model` as its model override. | Given `PlanOptions.review_model` set, the review call is recorded on that model and every planner call on the main model. |
| FR-PPLN-14 | Planner prompts shall place spec text in untrusted blocks, and every model-planned task, story summary and area shall record its `prompt_ref`. | Inspection of every planner prompt; given a model-planned task, then `prompt_ref` names the prompt revision. |

## Stitch ([plan.md](plan.md) "Stitch")

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PDAG-01 | Every root task of every area shall wait for every setup sink, and setup and closing tasks shall never count as roots. | Given setup tasks `WORKSPACE` and `TEST_HARNESS`, where `TEST_HARNESS` waits for `WORKSPACE`, then every area root waits for `TEST_HARNESS` and the setup root waits for nothing. |
| FR-PDAG-02 | A consumer area's integration tasks (else its logic tasks) shall wait for the provider area's contract tasks that implement the consumed interface, and never the reverse. | Given "Orders API" provided by "Orders service" and consumed by "Web shop", then the Web shop integration task waits for the Orders service contract task implementing "Orders API" and no Orders service task waits for a Web shop task. |
| FR-PDAG-03 | For an integration edge between two areas with no interface, the caller's integration tasks (else logic tasks) shall wait for the callee's contract tasks, else its non-acceptance sinks. | Given "Web shop" `INTEGRATES_WITH` "Payments service" and no interface between them, then the Web shop integration task waits for the Payments service contract tasks and not the reverse. |
| FR-PDAG-04 | For a `stated` or `implied` spec dependency edge (requirement → requirement or feature → feature), the tasks implementing the source shall wait for the tasks implementing the target; an `inferred` dependency edge shall create no wait. | Given requirement A of "Web shop" depending on requirement B of "Orders service" (stated), then A's task waits for B's task; given the same edge with basis `inferred`, then no wait exists and the brief shows the edge with its basis. |
| FR-PDAG-05 | Tasks in a service area implementing requirements of an entity stored in another area shall wait for that area's data tasks implementing the entity. | Given an Orders service requirement with a domain edge to "Order", then its task waits for the `AREA::ORDERS_DATABASE` data task implementing "Order". |
| FR-PDAG-06 | A deployment area's root tasks shall wait for the setup sinks, and closing end-to-end tasks shall wait for the deployment area's sinks. | Given a deployment module "Production cluster", then its root task waits for the setup sink and the closing end-to-end task waits for its sink. |
| FR-PDAG-07 | An `acceptance` task shall never wait for another area's `acceptance` task. | Given two areas with acceptance tasks that integrate, then no `WAITS_FOR` joins the two acceptance tasks. |
| FR-PDAG-08 | Every closing task shall wait for the sinks of the areas it implements, or of every area when it implements none. | Given a closing task implementing a Web shop requirement, then it waits for the Web shop's sinks only. |
| FR-PDAG-09 | The task graph shall be acyclic; a cycle shall be broken by dropping its last-added stitch edge, counted in `dropped_dependencies`. | Given two areas with stated dependency edges in both directions between their requirements, then one stitch edge is dropped and counted. |

## Diff ([plan.md](plan.md) "Diff")

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PDIF-01 | A completed task none of whose implemented nodes has a meaning revision newer than its `spec_build_id`, and that is not reached by a cascade, shall keep its id, status and commit. | Given a meaning change of a requirement of another area, then every completed task of this area is kept. |
| FR-PDIF-02 | When an implemented node of a completed task has a meaning change, the library shall mark the task `superseded`, create a `pending` revision `_R<n>` that `SUPERSEDES` it, and list its commit as stale. | Given a completed `TASK::ORDERS_SERVICE::LOGIC_EXPORT` whose requirement is classed `changed_meaning`, then `..._R2` is `pending` and the old task is `superseded`. |
| FR-PDIF-03 | A superseded or retired prerequisite inside an area shall supersede its completed dependants inside the area; completed dependants in other areas shall be listed in `affected_dependants`. | Given contract → logic → acceptance completed in the Orders service and a Web shop task waiting for the contract, when "Orders API" is classed `changed_meaning`, then the three Orders service tasks get revisions and the Web shop task is kept and listed. |
| FR-PDIF-04 | A plan run shall never change an `in_progress` task or its relationships. | Given a claimed task whose requirement changed, then after the run the task, its claim, token, story and waits are unchanged and it is listed in `in_progress_changed`. |
| FR-PDIF-05 | When a task's work leaves the plan, the task shall become `retired`, and a commit it had shall be listed as stale. | Given a removed document whose requirement had a completed task, then the task is `retired` and its commit is in `stale_commits`. |
| FR-PDIF-06 | A current task shall match a target task with the same `implements` set when their keys differ. | Given a planner that renames the action of an unchanged completed task, then the task keeps its id and commit. |
| FR-PDIF-07 | When the key of a retired task returns, the task shall be revived under its id when it has no commit, and get a new revision when it has one. | Given a requirement removed and restored, then its task without commit is `pending` again under the same id. |
| FR-PDIF-08 | A task change shall be persisted only while the task's status equals the one the snapshot read, and skipped changes shall be counted in `conflicts`. | Given a task claimed during the run, then the run does not change it and `conflicts` lists it. |
| FR-PDIF-09 | A node that moved area shall take its open task along, and a move alone shall never supersede a completed task. | Given an orphan requirement with a pending task in the project area that inference attaches to "Web shop", then the task is in a Web shop story after the run; a completed one keeps its commit. |

## Task lifecycle ([lifecycle.md](lifecycle.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PLCY-01 | `ready_tasks` shall return `pending` tasks of active areas whose prerequisites are all `completed`, filtered by story or area when given. | Given a fresh plan, then only the setup root is ready; given a pending task of a retired area, then it is never ready. |
| FR-PLCY-02 | `claim_task` shall succeed for exactly one of concurrent claims of the same task. | Given 10 concurrent claims on a live store (each of Neo4j, ArcadeDB, PostgreSQL), then one returns a claim and 9 raise `TaskStateError`. |
| FR-PLCY-03 | A completion, failure, block or release shall require the current claim token; a release shall also accept `force=True`. | Given a task released and claimed again, when the first agent completes it with its old token, then `TaskStateError` is raised and nothing changes; a forced release succeeds. |
| FR-PLCY-04 | `complete_task` shall accept only a 40 or 64 hex commit and store it lower case. | Given a 39 hex commit, then `ConfigurationError("invalid_commit")` is raised and the task stays `in_progress`. |
| FR-PLCY-05 | Every transition not in the transition table shall raise `TaskStateError` and change nothing. | Given a `pending` task, when `complete_task` is called, then it raises and the task is unchanged. |
| FR-PLCY-06 | `stale_claims` shall return `in_progress` tasks claimed longer ago than the given age. | Given claims 2 h and 5 min old and an age of 1 h, then only the first is returned. |
| FR-PLCY-07 | `task_brief` shall return the task with its story and area, the project profile with conventions, the implemented spec nodes with provenance, the completed prerequisites with commits, and at most `neighbours` neighbour nodes with each edge's basis and confidence, without a model call. | Given a logic task whose data prerequisite is completed, then the brief names its story, lists the data task's commit, the requirement's source lines and the basis of each neighbour edge, and the gateway records no call. |
| FR-PLCY-08 | `task_brief` shall report `spec_changed` when an implemented node's `last_build_id` is newer than `Plan.last_build_id`, and `missing` for implemented `spec_id`s that no longer resolve. | Given a requirement rewritten by a build after the plan's last consumed build, then the brief of its task has `spec_changed` true. |
| FR-PLCY-09 | `task_brief` shall list the open questions that relate to an implemented node, and the task shall carry `has_open_questions`. | Given an OpenQuestion `RELATES_TO` "Order", then the brief of the "Order" data task lists it and the task has `has_open_questions` true. |
| FR-PLCY-10 | `task_brief` shall show `done_when` items with their source and the task's releases. | Given a task with one `spec` and one `planner` item, then the brief shows both sources. |

## Agent-facing reads and claims ([api.md](api.md), [lifecycle.md](lifecycle.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PMCP-01 | If a task is not ready, then `claim_task` shall raise `TaskStateError("not_ready")` carrying the ids of its unfinished prerequisites, and change nothing. | Given a fresh plan, when a Web shop logic task is claimed, then the error lists the unfinished prerequisites it waits for, among them the setup sink. |
| FR-PMCP-02 | `story_work(project_id, story_id)` shall return the story's open tasks plus their unfinished transitive prerequisites, in topological order, with their claim holders. | Given `STORY::WEB_SHOP::CHECKOUT` on a fresh plan, then the result holds the setup tasks, the Orders service contract task for "Orders API" and the checkout tasks, each prerequisite before its dependants. |
| FR-PMCP-03 | `claim_next(project_id, story_id, *, claimed_by)` shall claim the story's first ready task in topological, then `task_id`, order. | Given a story with two ready tasks and one waiting, then `claim_next` claims the ready task first in topological order, and a second call the other. |
| FR-PMCP-04 | If no task of the story is ready, then `claim_next` shall raise `TaskStateError("nothing_ready")` carrying the blocking tasks, and change nothing. | Given `STORY::WEB_SHOP::CHECKOUT` on a fresh plan, then the error names the unfinished prerequisites outside the story. |
| FR-PMCP-05 | The library shall offer no claim that ignores prerequisites; only `release_task(force=True)` may bypass the claim token. | Inspection: no lifecycle call takes a parameter that skips the ready rule. |
| FR-PMCP-06 | `plan_dag(project_id, format)` shall return, for `json` and `mermaid`, the open and completed tasks in topological order grouped by story, with their waits, and the story order projection with cycles collapsed into groups. | Given two stories whose tasks wait for each other in different phases, then the task order is acyclic and the two stories appear as one group in the story order. |
| FR-PMCP-07 | The story reads shall list stories filtered by area, status and release, and return one story with its anchor, all its members including uncovered ones, its tasks with status, and its derived progress. | Given `STORY::ORDERS_SERVICE::ORDERS_API` with one integration requirement no task implements yet, then the story read lists that requirement as uncovered. |

## Plan checks ([lifecycle.md](lifecycle.md) "Plan checks")

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PCHK-01 | After every plan run, the library shall run the plan checks and store their report in the result. | Given a finished run on the shop graph, then `checks.ok` is true and every required node is covered. |
| FR-PCHK-02 | `check_plan` shall report problems without changing the graph. | Given a task whose implemented node build deleted, then `lost_target` names the task and the graph is unchanged. |
| FR-PCHK-03 | Plan checks shall report `outdated` when the spec graph changed since the last plan run. | Given a build that changed a requirement, then `outdated` is true until the next plan run. |
| FR-PCHK-04 | Coverage (PD3) shall accept a required node that an open or completed task implements, that is recorded `not_buildable`, or that a profile convention cites, and report any other. | Given a constraint cited by a profile convention and no task, then coverage reports nothing for it; given a required requirement with neither, then coverage names it. |
| FR-PCHK-05 | Plan checks shall report every open task that is not `IN_STORY` exactly one active story of its own area (PD7). | Given a task whose story was retired by hand in a test fixture, then the check names the task. |
| FR-PCHK-06 | Plan check metrics shall include `not_buildable` and `open_questions_on_tasks`. | Given one `not_buildable` member and two open tasks with `has_open_questions`, then the metrics are 1 and 2. |

## Non-functional

| ID | Requirement | Acceptance |
|---|---|---|
| NFR-PSEC-01 | Logs, errors and results shall never contain document text, planner text, notes or secrets. | Log-capture test over a full plan run and lifecycle calls. |
| NFR-PSEC-02 | Plan labels and relations shall reach query text only as declared names; all values as parameters. | Inspection: the structure test passes for `graph.plan`. |
| NFR-PCOST-01 | An `update` run after a one-requirement meaning change should call the model only for the changed planning part's area, the project planner when its input changed, and one review. | Analysis: measured on the shop corpus and reported; not a release gate. |
| NFR-PCOST-02 | One specflow `first` plan should be measured (calls, tokens, time per step) beside its `estimate_plan` before the defaults are tuned. | Analysis: measured and reported; not a release gate. |
| NFR-PPORT-01 | `graph.plan` shall add no third-party dependency beyond those of the library. | Inspection: the structure test passes. |
