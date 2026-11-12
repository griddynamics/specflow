---
type: API
title: "Graph plan API"
description: "Public API of graph.plan: setup, plan runs, reads, task lifecycle, plan service and story calls, errors, ports, MCP tool mapping"
status: stable
tags: [graph, plan]
---
# api.md: public API of graph.plan: setup, plan runs, reads, task lifecycle, plan service and story calls, errors, ports, host endpoints, MCP tool mapping

All functions are `async` unless noted. Every function takes `project_id` explicitly (in a context or as
an argument), and an invalid `project_id` raises `ConfigurationError` before any query. Core setup
(`load_ontology`, `graph.stores.build_store`, `ensure_schema`, `Gateway`) is in [../api.md](../api.md);
`load_ontology()` picks up the plan ontology file once `graph.plan` is installed. The functions, classes
and result models below are exported from the package root `graph.plan`, except `PlanInfo`, which
`PlanReader.plan` returns.

## Setup

| Name | Contract |
|---|---|
| `load_plan_rules(path=None, *, ontology) -> PlanRules` (sync) | Shipped plan rules by default; validated against the merged ontology; raises `PlanRulesError` listing every problem ([ontology.md](ontology.md)) |
| `ensure_plan_schema(source, ontology, llm_config, database=None) -> tuple[str, ...]` | The core `ensure_schema` under the plan's name: creates the missing graph objects, the plan's included, and returns the names it created; idempotent (a second run returns `()`). `source` is a `GraphStore` (a Neo4j driver with `database` is deprecated, as in core); `ontology` must hold the plan ontology file. The core `ensure_schema` alone does the same, because it discovers the plan DDL shipped at `graph/plan/ddl/` |
| `open_plan_store(root: str \| Path) -> FilePlanStore` (sync) | File implementation of the `PlanStore` port under a host-given directory |

## Plan runs

| Name | Contract |
|---|---|
| `PlanContext` | `project_id`, `ontology`, `rules` (`PlanRules`), `driver` (the `GraphStore`; a Neo4j driver is deprecated), `gateway`, `store` (the `PlanStore`), `build_id` (the project's last build, which the graph reflects; becomes `Plan.last_build_id`), `changes` (a tuple, below; default `()`), `options` (default `PlanOptions()`), `replan` (default `False`), `run_id` (none: a new run; set: resume that run from its first unfinished step), `database` (none: the store's default), `query_executor` (none: the store's executor; tests may pass one) |
| `run_plan(context) -> PlanResult` | Runs every step of [plan.md](plan.md); on a finished run returns its stored result |
| Step functions | `graph.plan.steps.<name>`: `snapshot`, `partition`, `describe`, `project`, `tasks`, `stitch`, `review`, `diff`, `persist`, `check`. Each reads its inputs from and writes its output to the `PlanStore`. `snapshot(context)` starts a run when `run_id` is None and returns the run id; run the other steps with `context.with_run_id(id)`, then call `run_plan(context.with_run_id(id))` to store the result |
| `estimate_plan(context) -> PlanEstimate` | Calls and input tokens per step of the plan run `context` would start, without a model call: reads the spec graph, the plan and the `PlanStore`, chooses the mode and the changed areas and parts as Snapshot and Partition would, and counts reused answers as no call. Writes nothing |
| `check_plan(context, stale_after=None) -> PlanCheckReport` | The plan checks of [lifecycle.md](lifecycle.md), read-only; `stale_after` is a `timedelta` for the `stale_claims` metric |

`PlanResult` holds the fields of [plan.md](plan.md) "Plan result". `PlanEstimate` holds `mode`, `unchanged`
(true when an `update` would plan nothing; then no steps) and `steps`: per model step (`describe`,
`project`, `tasks`, `review`) a `PlanStepEstimate` of `calls` and `input_tokens` (estimated from prompt
sizes, like `estimate_build`).

**When the host runs a plan.** After a build of the project has returned (with its follow-up builds),
never during one, and not when the build's settlement lists `documents_incomplete`. The plan itself
checks nothing about settlement.

### Change reports

`PlanContext.changes` is a tuple of `BuildChangeReport`, the build change reports of every build after
`Plan.last_build_id` up to `build_id`, in build order. It is a documented contract with the host:
`graph.plan` never imports `graph.build`, so the host converts each build's change report into this
plan-side model.

| Field | Content |
|---|---|
| `build_id`, `previous_build_id` | The build, and the project's build before it (none for the project's first build); the plan checks the chain with them |
| `nodes` | `{spec_id, classes, previous_spec_ids}` per changed node; class names as in the build change report: `added`, `removed`, `merged`, `carried_over`, `rekeyed`, `changed_meaning`, `changed_links`, `changed_evidence` |
| `edges` | `{source, relation, target, change, basis}` per changed domain edge, ends by `spec_id`; `change` is `added`, `removed` or `changed`; inferred edges included |

- In `first` mode `changes` must be empty; the plan records `build_id`.
- In `update` mode a chain that does not start at `Plan.last_build_id` or does not end at `build_id`
  raises `ConfigurationError("change_reports_missing")` naming the gap. The host passes the missing
  reports from its build store, or runs a `replan`.
- A `replan` may pass no reports. It then re-plans every area and lists completed tasks it could not
  verify in `unverified_completed` ([plan.md](plan.md)).

Plan update correctness follows the build's change-report classification: a node wrongly classed
`changed_meaning` supersedes finished work, a real change classed `changed_evidence` leaves it in
place.

Host conversion derives `previous_build_id` from the project's build order, skips failed builds, and
runs a `replan` when a needed report is missing.

### Gateway

Describe, Project and Tasks call the `extraction` call class; Review calls `judge` on the same gateway
the run is given, with `PlanOptions.review_model` as its model override (`None` means the gateway's
judge model). Plan calls share the gateway limit (`GRAPH_LLM_CONCURRENCY`).

## Reads

`PlanReader(store_or_executor, ontology, rules, *, database=None)`. Returns plan data only; spec nodes
are named by `spec_id` (aliases resolve to the current node).

| Method | Contract |
|---|---|
| `plan(project_id) -> PlanInfo \| None` | The `Plan` node: profile, graph fingerprint, `last_build_id`, versions, last run id, whether a persist is in progress |
| `areas(project_id, status=None) -> list[PlanArea]` | Areas ordered by `area_id`, with anchor, interfaces, member `spec_id`s, task counts by status and story counts by progress |
| `stories(project_id, *, area_id=None, status=None, release=None) -> list[PlanStory]` | Stories ordered by `story_id`: id, area, kind, title, summary, anchor, `release`, stored status, derived progress, task counts by status. `status` takes a `story_status` value or a `story_progress` value; none returns the active stories. `release` matches the story's release |
| `story(project_id, story_id) -> StoryDetail \| None` | The story with its anchor (`spec_id`, label, name, attributes), its members (every plannable node whose home story it is, each with the open or completed task ids that implement it; uncovered and `not_buildable` members included), its tasks in topological order, its derived progress, and the stories it waits for and that wait for it (the projection, [lifecycle.md](lifecycle.md) "Story progress") |
| `story_work(project_id, story_id) -> StoryWork \| None` | The story's open tasks plus their unfinished transitive prerequisites, in topological order, each with status, story, area, ready flag, waits and claim holder ([lifecycle.md](lifecycle.md) "Claims") |
| `plan_dag(project_id, output="json") -> PlanDag \| str` | The plan as a DAG: open and completed tasks with their `WAITS_FOR` edges, in topological order, grouped by story, plus the story projection with cycles collapsed into story groups. `output="json"` returns a `PlanDag`; `output="mermaid"` returns Mermaid flowchart text (one subgraph per story) |
| `tasks(project_id, *, status=None, area_id=None, story_id=None, phase=None) -> list[PlanTask]` | Tasks ordered by `task_id` |
| `task(project_id, task_id) -> PlanTask \| None` | One task with its prerequisites, dependants, predecessor and successor revision |
| `tasks_for(project_id, spec_ids, *, include_closed=False) -> list[PlanTask]` | Tasks that implement any of the given spec nodes; with `GraphReader.impact` this answers "which tasks does a spec change touch" |

`PlanTask`: the task properties of [data-model.md](data-model.md) except `claim_token`, `uuid`, `_lock` and
the story-claim fields `progress`, `completed_by`, `completed_at` and `blocked_by`, plus `waits_for`,
`waited_by`, `predecessor` and `successor` (the revision links). Reads that show the story-claim fields
return a `StoryTask` ("Plan service"), never a `PlanTask`; this keeps the shape of every task read that
exists today.

## Task lifecycle

`TaskLifecycle(store_or_executor, ontology, rules, *, database=None)`. Each state change is one
transaction ([lifecycle.md](lifecycle.md) "Transitions"); a call that does not apply raises
`TaskStateError` and changes nothing. A `note` is at most 500 characters (else
`ConfigurationError("invalid_note")`), stored on the task, never logged.
`claimed_by` is 1-200 printable characters (else `ConfigurationError("invalid_claimant")`).

| Method | Contract |
|---|---|
| `ready_tasks(project_id, *, story_id=None, area_id=None, limit=None) -> list[PlanTask]` | Ready tasks ordered by `task_id`, optionally of one story or area; empty while a plan persist is in progress |
| `claim_task(project_id, task_id, *, claimed_by) -> Claim` | `pending` and ready → `in_progress`; `Claim` holds the task and a new `claim_token`. Not ready: `TaskStateError("not_ready")` with `blockers` |
| `claim_next(project_id, story_id, *, claimed_by) -> Claim` | Claims the story's first ready task in topological then `task_id` order; none ready: `TaskStateError("nothing_ready")` with `blockers` |
| `complete_task(project_id, task_id, *, claim_token, commit) -> PlanTask` | → `completed`; `commit` must match `^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$` (else `ConfigurationError("invalid_commit")`), stored lower case |
| `fail_task(project_id, task_id, *, claim_token, note="") -> PlanTask` | → `failed` |
| `block_task(project_id, task_id, *, claim_token, reason, note="") -> PlanTask` | → `blocked`; `reason` is a block reason code (else `ConfigurationError("invalid_reason")`) |
| `release_task(project_id, task_id, *, claim_token=None, force=False, note="") -> PlanTask` | → `pending`; needs the claim token, or `force=True` for crash recovery |
| `retry_task(project_id, task_id) -> PlanTask` | `failed` → `pending` |
| `unblock_task(project_id, task_id) -> PlanTask` | `blocked` → `pending` |
| `stale_claims(project_id, *, older_than) -> list[PlanTask]` | `in_progress` tasks claimed longer ago than `older_than` (a `timedelta`) |
| `task_brief(project_id, task_id, *, neighbours=30) -> TaskBrief` | The brief of [lifecycle.md](lifecycle.md) "Task brief", for any open or completed task; reads the spec graph through a `GraphReader` without a gateway and makes no model call |

## Plan service

`PlanService(store_or_executor, ontology, rules, *, graph=None, load=None, clock=None, findings=None)` answers the task questions of the plan's callers (the MCP plan
tools; the REST app can use it too) from one read of the persisted plan per call, so a host checks access, pages and
renders and composes nothing. Superseded and retired tasks are history: never listed, never on a path, never briefed.
A project without a plan raises `PlanReadError("no_plan")`.

| Method | Contract |
|---|---|
| `list_tasks(project_id, *, ready_only=True, status=None, area_id=None, story_id=None) -> tuple[TaskItem, ...]` | Open and completed tasks after the filters, ordered by `task_id`; `ready_only` keeps the ready ones |
| `feature_tasks(project_id, feature_id) -> FeatureTasks` | The tasks of the active stories the feature anchors and the tasks that implement it or a node of its `GraphReader.scope`, ordered by `task_id`; a node that is not of the `feature` type: `PlanReadError("feature_not_found")` |
| `task_path(project_id, task_id) -> TaskPath` | Every transitive prerequisite in build order (lowest `task_id` first among equals), then the task; `completed` counts finished prerequisites |
| `task_brief(project_id, task_id, *, neighbours=30) -> TaskBrief` | `TaskLifecycle.task_brief` from the same read; an unknown, superseded or retired task: `PlanReadError("task_not_found")` |
| `story_brief(project_id, story_id, *, neighbours=30) -> StoryBrief` | The story brief of [lifecycle.md](lifecycle.md) "Story brief" from one graph read and one plan load, with `findings`: the open findings about the anchor and implemented nodes (`OpenFinding`, blocking first, at most 200; empty without the port). A retired or unknown story: `PlanReadError("story_not_found")` |
| `trace(project_id, *, story_id=None, spec_ids=None, **options) -> PlanTrace` | The plan overlay of [../trace.md](../trace.md) "Plan overlay". `options` is a `TraceOptions`: `direction` (required), `depth` (2), `basis` (`trusted`), `relations` (none), `include_owner` (true), `limit` (100). Exactly one of `story_id` and `spec_ids`, else `ConfigurationError("invalid_arguments")`; the graph arguments are checked by `GraphReader.trace` ([../trace.md](../trace.md) "Library") |
| `check_block(project_id, story_id, *, claimed_by, claim_token, spec_ids) -> None` | The guard of a blocking finding, a host call: `claimed_by` holds the story under `claim_token` and a node of `spec_ids` is implemented by an open or completed task of the story; else `TaskStateError("claim_mismatch")`, the same answer for an unknown story, a story nobody holds and a wrong token ([../gap.md](../gap.md) "Blocking link") |

`PlanService` takes its graph reads (`graph`, default a `GraphReader` over the same executor), its plan loader
(`load`) and its clock (`clock`) as arguments so tests can replace them. `findings` is the optional
`FindingsReads` port ("Ports") that the host fills with `graph.gap.Findings`; without it every findings field
below is empty and every count 0.

A `TaskItem` names its area and story (`AreaRef`, `StoryRef`; the story title only while the story is active), its
readiness, `waits_for` and `planned_by`. It carries no claim fields. `StoryTask(TaskItem)`, returned by the story
calls (`claim_story`, `report_progress`), adds `progress` (while `in_progress` under the claim) and, once
completed, `commit`, `completed_by` and `completed_at`; the claim token is never part of it.

### Story reads for people

Rows name holders by user id; the host turns them into names. `state` is the story state of
[lifecycle.md](lifecycle.md) "Story claims". A project without a plan raises `PlanReadError("no_plan")`.

| Method | Contract |
|---|---|
| `area_summary(project_id) -> tuple[AreaSummary, ...]` | Per active area, project area last: `area_id`, `title`, `kind`, stories per state, the holders of its held stories, `open_tasks` |
| `story_list(project_id, *, view="open", caller, area_id=None, finding_ids=()) -> tuple[StoryRow, ...]` | Active stories as rows: `rank`, `story_id`, `title`, `kind`, `state`, holder (`claimed_by`, `claimed_at`, `agent_label`), `waits_on` (up to 3 stories with unfinished work the unit waits for), `tasks` (open count), `unblocks` (stories that wait for it), `blocked_by` (finding ids). `open`: every state but `completed`, `ready` first (by `release`, then `unblocks` descending, then `story_id`), then `in_progress`, `waiting`, `blocked`. `mine`: held by `caller`, plus stories with a task blocked by one of `finding_ids` (the host passes the caller's open findings). `done`: `completed` stories newest first, with `pr_url`, `commit`, `completed_by` and `completed_at` of their last completed task. Unknown `area_id`: `PlanReadError("area_not_found")` |
| `story_preview(project_id, story_id) -> StoryPreview` | The story, its state, claim (holder, `claimed_at`, `agent_label`, `branch`, `note`), the unit's story ids, its open tasks in topological order (`task_id`, `title`, `status`, `ready`, `complexity`, `progress`), the blockers outside the unit (story, state, holder) and `unblocks`. `findings`: the open findings about the nodes its open tasks implement (as in the story brief). Never the claim token. Unknown or retired: `PlanReadError("story_not_found")` |

### Story calls

The transitions of [lifecycle.md](lifecycle.md) "Story claims", one transaction each. Argument checks
run before any write and raise `ConfigurationError`: `claimed_by` and `completed_by` as for task claims
(`invalid_claimant`), `note` (`invalid_note`), `merge_commit` (`invalid_commit`), `reason`
(`invalid_reason`), `agent_label` (1-80 printable), `branch` (1-200 printable), `pr_url` (an `http` or
`https` URL without user info, at most 2000 characters) and `progress` (`invalid_field`), `done` naming a
task outside the claim (`invalid_done`). State refusals raise `TaskStateError`.

| Method | Contract |
|---|---|
| `claim_story(project_id, story_id, *, claimed_by, agent_label=None, branch=None) -> StoryClaim` | `claim_token`, `story_ids` (the unit, the asked story first), `tasks` (`StoryTask`s in topological order), `claimed_at`, `repeat` |
| `release_story(project_id, story_id, *, claim_token=None, force=False, note=None, branch=None) -> StoryRelease` | `released` (task ids), `repeat`; `force=True` is a host operation |
| `report_progress(project_id, task_id, *, claim_token, progress, note=None) -> StoryTask` | The task after the report. A task that is not `in_progress` or not under the held token: `claim_mismatch`; an unknown task: `unknown_task` |
| `complete_story(project_id, story_id, *, claim_token, completed_by, merge_commit, pr_url=None, done=None) -> StoryCompletion` | `completed`, `released`, `now_available` (story ids), `spec_changed` (spec ids: the implemented nodes a build wrote after the plan's consumed builds), `repeat` |
| `block_story(project_id, story_id, *, claim_token, reason, finding_id) -> StoryRelease` | `released` holds the blocked task ids; called by the host for a blocking finding ([../gap.md](../gap.md) "Blocking link") |
| `unblock_finding(project_id, finding_id) -> tuple[str, ...]` | The task ids moved back to `pending`; a host operation |

## Errors

| Error | Codes | Raised by |
|---|---|---|
| `ConfigurationError` | `no_spec_graph`, `change_reports_missing` | plan runs, `estimate_plan` |
| `ConfigurationError` | `unfinished_run`, `unknown_run`, `unbound_run`, `missing_artifact` | plan runs and step functions |
| `ConfigurationError` | `invalid_options` | `PlanOptions` |
| `ConfigurationError` | `invalid_run_id`, `run_not_found`, `run_exists`, `invalid_plan_result` | the file `PlanStore` |
| `ConfigurationError` | `invalid_commit`, `invalid_reason`, `invalid_note`, `invalid_claimant`, `invalid_field`, `invalid_done` | lifecycle and story calls, before any write |
| `PlanRulesError` | `invalid_plan_rules` (with every problem) | `load_plan_rules` |
| `PlanReadError` | `no_plan`, `feature_not_found`, `task_not_found`, `story_not_found`, `area_not_found` | `PlanService` |
| `TaskStateError` | `not_ready`, `nothing_ready`, `not_available` (each with `blockers: tuple[str, ...]`), `plan_applying`, `wrong_state`, `unknown_task`, `unknown_story`, `claim_mismatch`, `story_claimed` (with `claimed_by`, `claimed_at`, `alternatives`), `partly_claimed` (with `task_id`, `claimed_by`) | lifecycle and story calls ([lifecycle.md](lifecycle.md) "Transitions", "Story claims") |
| `LLMFatalError`, `InfrastructureUnavailableError`, `GraphWriteError`, `ConformanceError` | - | plan runs ([plan.md](plan.md) "Failures") |

A non-fatal `LLMError` never reaches the host from a plan run: the planner falls back.

## Ports

Port methods are `async`.

| Port | Methods |
|---|---|
| `PlanStore` | `create_run(record)`, `get_run(project_id, run_id)`, `latest_run(project_id, *, finished_only=False, modes=None)`, `put_progress(project_id, run_id, step, state)`, `finish_run(result)`, `get_result(project_id, run_id)`; `put_artifact(project_id, run_id, step, payload)`, `get_artifact(project_id, run_id, step)`; `get_answer(project_id, kind, key, reuse_key, *, run_id=None)`, `put_answer(project_id, kind, key, reuse_key, run_id, *, payload)` with kinds `describe`, `project`, `tasks`; `get_revisions(project_id)`, `put_revisions(project_id, revisions)` exchanging `MeaningRevisions` (`nodes`: `spec_id` → build id of its meaning revision; `consumed`: the consumed build chain) |
| `FindingsReads` | `open_counts(project_id, spec_ids) -> Mapping[str, int]`, `findings(project_id, *, spec_ids=None, limit=50) -> page with .findings`: structural, satisfied by `graph.gap.Findings` as it is; `graph.plan` never imports `graph.gap`. Optional: `PlanService(..., findings=)` |

`open_plan_store(root)` stores per-project paths, gzip-compressed JSON, atomic writes (write then
rename), like the build file stores. One contract test suite runs against every implementation.

## HTTP endpoints (host)

The API serves plan runs and plan reads to the web app (`backend/src/api/routers/plan.py`,
`backend/src/api/plan/runner.py`). They mirror `/graph_build`: the run is an in-process background task, one
per project, and the contract can move to a Temporal workflow unchanged. The routes exist when the graph build
is configured (`GRAPH_LLM_MODEL`, `GRAPH_SMALL_MODEL`, `GRAPH_EMBEDDING_MODEL`); otherwise each answers 503
`plan_unavailable`. A non-member gets 404 `project_not_found`. Bodies hold ids, step names, counts, codes and
the plan's own text, never document text.

| Endpoint | What it does |
|---|---|
| `POST /api/v1/projects/{pid}/plan` | Starts a plan run and answers 202 with the state at once; a second call while it runs answers the same run. Body (optional): `{"replan": bool}`. 409 `no_graph_build` without a finished build, 409 `graph_build_running` while the graph builds |
| `GET /api/v1/projects/{pid}/plan` | The state: `status` (`idle`, `running`, `succeeded`, `partially_succeeded`, `failed`), `has_plan`, `run_id`, `mode`, `replan`, `step` (the step that runs or last ran), `steps_done`, `steps_total`, `step_items_done` and `step_items_total` (while the Tasks step runs: its planning parts done and in all, reported through `PlanContext.on_progress`; null otherwise), `started_at`, `finished_at`, `unchanged`, `error_code` (`interrupted` for a run a stopped process left) |
| `GET /api/v1/projects/{pid}/plan/tasks` | The persisted plan: `plan_id`, `last_build_id`, `updated_at`, `areas`, `stories` (with `progress`), `tasks` in phase order, then by area, story and id. Each area has `planned_by` and `not_buildable` (spec id, name, reason code, note); each task has its phase, status, complexity, `done_when`, `waits_for`, `implements` (spec id, label and name), `planned_by` (`model`, or `rules` when a fallback wrote it) and `ready` (pending with every wait completed). From the run that last wrote the plan: `quality` (`run_id`, `status`, `fallbacks` per planner, `tasks_planned`, `review` counts by severity, `review_failed`, `unplaced`, `oversized`; null when that run's result is gone) and `review`, its findings (`area_id`, `task_id`, `problem`, `severity`). 404 `no_plan` without a plan |
| `GET /api/v1/projects/{pid}/plan/preview` | What a run would start from, without a model call: `features` (the anchors of feature stories in the rules), `mode`, `unchanged`, and the run estimate summed over the steps, `calls` and `input_tokens` (null without a finished build or when the estimate fails) |

The runner plans from the project's latest finished build. Without a plan the run is `first`. With a plan whose
`last_build_id` is that build, it is an `update` with no change reports (the result is `unchanged`). With a plan
from an earlier build, or with `replan`, it is a `replan` without change reports: the build store keeps no build
chain yet, so the host takes the documented path for missing reports ("Change reports"). An unfinished run is
resumed by the next start. Plan state lives in the file `PlanStore` under `GRAPH_STATE_DIR`, next to the build
stores. The web app's Plan page reads `GET /plan` and `/plan/preview`, polls `GET /plan` every 2 s while a run
goes, and reads `/plan/tasks` once a plan exists; "Recreate Plan" posts `replan`.

## MCP tools (host)

Users connect coding agents to the app over MCP. The MCP server is host code, not part of `graph.plan`:
it calls the API, which calls `PlanService`; tool contracts are in
[../../mcp-server/tools.md](../../mcp-server/tools.md). Every rule (ready tasks only, one claim per task
or story, no claims while a plan persists, the claim token) is enforced by the plan, so it holds for
every client.

| Tool | Call |
|---|---|
| `list_tasks`, `list_feature_tasks`, `get_task_path`, `get_task_brief` | `PlanService.list_tasks`, `feature_tasks`, `task_path`, `task_brief` |
| `list_areas`, `list_stories`, `preview_story` | `PlanService.area_summary`, `story_list`, `story_preview` |
| `claim_story`, `release_story`, `report_task`, `complete_story` | `PlanService.claim_story`, `release_story` (without `force`), `report_progress`, `complete_story` |
| `get_story_brief`, `trace` | `PlanService.story_brief`, `trace` |
| `add_finding` with `blocking` | `PlanService.check_block` before the finding is written, `block_story` after it ([../gap.md](../gap.md) "Blocking link"): composed by `graph.gap.FindingFlow` with `PlanService` as its `StoryBlocks` port |

The API maps library error codes to the tool's by one table, which also covers the renames (`TaskStateError`
`unknown_task` becomes `task_not_found`, `unknown_story` and a retired story's `wrong_state` become
`story_not_found`): [../../mcp-server/tools.md](../../mcp-server/tools.md#library-calls-used-by-the-story-and-spec-tools).

Task claims (`claim_task`, `claim_next` and the other task calls), `retry_task`, `unblock_task`,
`unblock_finding`, `stale_claims`, forced releases and plan runs stay host operations. Checking that a
reported commit exists in the repository is host-side and optional.
