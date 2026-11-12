---
type: Design
title: "Graph plan task lifecycle"
description: "Task transitions, ready tasks, task and story claims, story progress, task and story briefs, the agent contract and plan checks"
status: stable
tags: [graph, plan]
---
# lifecycle.md: task lifecycle, ready tasks, task and story claims, story progress, task and story briefs, the agent contract and plan checks

The plan stores execution progress on its `Task` nodes. Coding agents move tasks through their states
with the lifecycle calls below, through the host's MCP tools ([api.md](api.md) "MCP tools (host)");
plan runs move them only as [plan.md](plan.md) "Diff" says. Every lifecycle call is one transaction that
first takes a write lock on the task node, then checks its state, then writes, so two agents never both
claim or finish one task (a plain conditional update is not enough under read-committed isolation). On
Neo4j and ArcadeDB the lock is a property write on the task; on PostgreSQL it is `SELECT ... FOR UPDATE`
on the task row. The rules hold for every client: the plan enforces them, not the MCP server.

## Transitions

| From | To | Call | Rule |
|---|---|---|---|
| `pending` | `in_progress` | `claim_task`, `claim_next` | The task is ready ("Ready tasks"); sets `claim_token`, `claimed_by`, `claimed_at`, `attempts + 1` |
| `in_progress` | `completed` | `complete_task` | Claim token matches; commit is 40 or 64 hex; `completed_by` (the claimant) and `completed_at` set; claim fields cleared |
| `in_progress` | `failed` | `fail_task` | Claim token matches; claim fields cleared |
| `in_progress` | `blocked` | `block_task` | Claim token matches; `block_reason` set; claim fields cleared |
| `in_progress` | `pending` | `release_task` | Claim token matches, or `force=True` for crash recovery (the host decided the claim is stale); claim fields cleared; `attempts` unchanged |
| `failed` | `pending` | `retry_task` | The host decides to try again |
| `blocked` | `pending` | `unblock_task` | The host decides the task can proceed (the spec was clarified, or the block was wrong); `block_reason` cleared |
| `pending`, `failed`, `blocked`, `completed`, `retired` | per [plan.md](plan.md) "Diff" | plan run | Reset, supersede, retire, revive |

`fail_task`, `block_task` and `release_task` take an optional `note`, stored in `note` (omitting it clears the previous note). A call that does
not apply raises `TaskStateError` with a code and changes nothing:

| Code | When | Carries |
|---|---|---|
| `not_ready` | `claim_task` on a `pending` task that is not ready | `blockers` ("Claims") |
| `nothing_ready` | `claim_next` finds no ready task in the story | `blockers` ("Claims") |
| `plan_applying` | A claim, or a story call that locks stories and tasks ("Story claims"), while a plan persist is in progress | - |
| `wrong_state` | The task is not in the call's From state, or is closed | the current status |
| `story_claimed` | A task call targets a task of a story held by a story claim; end the claim with `release_story` or `complete_story` | as in "Story claims" |
| `unknown_task`, `unknown_story` | The id resolves to no task or story of the project | - |
| `claim_mismatch` | The claim token does not match (the task was released and claimed again), or `release_task` without a token and without `force` | - |

There are no forced claims: a claim never ignores prerequisites outside what it claims. `claim_task`
claims one task, so every prerequisite counts; a story claim takes the prerequisites inside its unit
along ("Story claims"). A person who wants to proceed anyway
completes or retires the blocking work explicitly. A plan run never touches an `in_progress` task, so a
running agent always finds its claim intact.

Block reason codes: `spec_ambiguous`, `spec_contradiction`, `spec_missing`, `environment`, `other`.

## Ready tasks

A task is ready when:
- it is `pending`;
- its area is `active` and its story is `active`;
- every task it `WAITS_FOR` is `completed`;
- no plan persist is in progress.

`ready_tasks(project_id, *, story_id=None, area_id=None, limit=None)` returns them ordered by `task_id`, optionally
only those of one story or area. On a fresh plan only the setup root is ready: every area's root tasks
wait for the setup sinks. The plan does not limit parallelism; any number of agents may claim ready
tasks. A blocking finding blocks the tasks of a claimed story ([../gap.md](../gap.md) "Blocking link");
holding tasks back on other open findings stays later ([later.md](later.md)).

## Claims

**Topological order.** Where this file says "topological order", it means the order of a fixed
topological sort of the `WAITS_FOR` graph: among the tasks whose prerequisites in the set come first,
the lowest `task_id` comes next. The same set always gives the same order.

| Call | Behaviour |
|---|---|
| `claim_task(task_id)` | Claims that task when it is ready. A `pending` task that is not ready raises `not_ready`; `blockers` lists the ids of its unfinished transitive prerequisites, in topological order, so the agent can take those first |
| `claim_next(story_id)` | Claims the first ready task of the story in topological order. When none is ready it raises `nothing_ready`; `blockers` lists the unfinished transitive prerequisites of the story's `pending` tasks that are not themselves `pending` tasks of the story, in topological order (empty when the story has no `pending` task). A retired story raises `wrong_state` |

Exactly one of concurrent claims of a task succeeds; a `claim_next` that loses a race on its first ready
task tries the next ready one in the same order, within the same call.

**Story work.** `story_work(project_id, story_id)` returns the story's open tasks plus their unfinished
transitive prerequisites (of any story or area), in topological order. Each entry has the task id,
story id, area id, status, whether it is ready, the ids it waits for, and `claimed_by` and `claimed_at`
while it is `in_progress`. It is the small DAG an agent or a person works through to finish one story.

`claim_task` and `claim_next` refuse a task of a held story with `story_claimed` ("Story claims").

## Story claims

Agents claim stories; task claims stay for people and hotfixes. A story claim is one token for every
task it claims. It lasts until the holder releases or completes the story, a blocking finding ends it, or
a person force-releases it: no lease, no heartbeat, no expiry. A story is held exactly while its
`claimed_by` is set ([data-model.md](data-model.md) "Story"). The calls are `PlanService` methods
([api.md](api.md) "Story calls"). Each is one transaction that locks the Story nodes of the claim, then
their tasks in `task_id` order, then checks and writes, as the task calls do.
A `claim_task` racing a `claim_story` on the same task waits on the task lock; when the story claim wins, the
task claim answers `wrong_state` (on Postgres, READ COMMITTED re-reads the locked task, now `in_progress`).

**Claim unit.** The story and every story it is in a cycle with in the story order ("Story progress"),
taken over open tasks; usually the story alone. Claiming any story of a cycle group claims the group:
each of its Story nodes gets the same token and claim fields, and a release, completion or block through
any of them acts on all.

**Claimable.** The story is `active`, no plan persist is in progress, its unit is not held, no task of the
unit is `in_progress` or `blocked`, at least one task of the unit is `pending` or `failed`, and every task
those tasks wait for outside the unit is `completed`. Prerequisites inside the unit do not count.

The claimed tasks of a claim are the tasks that carry its token while `in_progress`. A task a plan run
adds to a held story stays `pending`, outside the claim.

| Call | Rule | Effect |
|---|---|---|
| `claim_story(story_id, claimed_by, agent_label=None, branch=None)` | Claimable | Every `pending` and `failed` task of the unit → `in_progress` (task claim fields with the new token and `claimed_by`, `attempts + 1`); story claim fields set on the unit's stories. Answers the token, the claimed story ids and the claimed tasks in topological order |
| `release_story(story_id, claimed_by=None, claim_token=None, note=None, branch=None, force=False)` | `claimed_by` holds the story and the token matches, or `force=True` (host only: a person frees a forgotten claim; no caller or token) | Claimed tasks → `pending`, `attempts` unchanged; the claim ends; `note` and `branch` stored when given, else kept |
| `report_progress(task_id, claimed_by, claim_token, progress, note=None)` | The task is a claimed task of a story `claimed_by` holds and the token matches | `progress` set (`done`, `skipped`, `todo`), `note` stored when given; status unchanged |
| `complete_story(story_id, claim_token, completed_by, merge_commit, pr_url=None, done=None)` | `completed_by` holds the story and the token matches; `merge_commit` 40 or 64 hex; `done` only claimed task ids | Tasks in `done` (default: every claimed task) → `completed` in topological order with `commit` = `merge_commit` (lower case), `completed_by`, `completed_at`; the other claimed tasks → `pending`; `pr_url` stored; the claim ends |
| `block_story(story_id, claim_token, reason, finding_id)` | Token matches; `reason` is `spec_ambiguous`, `spec_contradiction` or `spec_missing` | Every claimed task → `blocked` with `block_reason` and `blocked_by` = `finding_id`; the claim ends |
| `unblock_finding(finding_id)` | Host only, after a person resolved or dismissed the finding | Tasks still `blocked` with that `blocked_by` → `pending`, block cleared; others untouched |

The claim ends: `claimed_by`, `claimed_at` and `agent_label` are cleared on the unit's stories,
`claim_token` and `token_holder` stay until the next claim; every task leaving `in_progress` loses its claim fields and
`progress`. A `skipped` task completes with its note. `complete_story` also answers `released` (task ids
back to `pending`), `now_available` (stories this completion made claimable) and `spec_changed` (spec ids,
not a boolean: the `spec_id`s the completed tasks implement whose node a build wrote after the plan's
consumed builds, by the rule of the brief's `spec_changed` in "Task brief"; empty on a repeat).

**Repeats.** Calls are idempotent by state; there is no request id. A repeat with the same arguments
changes nothing:

| Call | Repeat when | Answer |
|---|---|---|
| `claim_story` | `claimed_by` already holds the unit | The same token and tasks; `repeat` true |
| `release_story` | Not held, the token equals `claim_token` and the caller is `token_holder` (the user the token was issued to) | `released` empty |
| `report_progress` | The same `progress` is set | The same answer |
| `complete_story` | Not held, the token equals `claim_token`, and the story's tasks with `commit` = `merge_commit` have `completed_by` = the caller | `completed` = those ids, `released` empty, `now_available` as now, `spec_changed` empty |

Any other call with a token that does not match, or by a caller who does not hold the story (another member
who learned the token), raises `claim_mismatch`; the holder is checked under the story lock with the token.

**Refusals** (`TaskStateError`, nothing changes):

| Code | When | Carries |
|---|---|---|
| `story_claimed` | Another user holds the unit, or a task call targets a task of a held story | `claimed_by`, `claimed_at`, `alternatives`: up to 3 claimable stories of the same area, in list order |
| `partly_claimed` | A task of the unit is `in_progress` under a task claim; also a repeated `claim_story` after a plan run grew the held unit with a story nobody holds (`claimed_by` is the caller, `task_id` may be none; release or complete first) | `task_id`, `claimed_by` |
| `not_available` | Not claimable for another reason | `blockers`: the unfinished prerequisites outside the unit, in topological order (empty when nothing is left to claim or a task is `blocked`) |
| `plan_applying`, `unknown_story`, `unknown_task`, `wrong_state` (retired story), `claim_mismatch` | As for the task calls | - |

**Story state.** The people's views derive one state per active story; the first matching row wins.

| State | When |
|---|---|
| `completed` | Progress `completed` |
| `in_progress` | Held, or any task `in_progress` |
| `blocked` | Any task `blocked` |
| `ready` | Claimable |
| `waiting` | Otherwise: unfinished work outside the unit |

**Plan runs.** A run never writes `in_progress` tasks, so claimed tasks keep their token and
`progress`; it never writes story claim fields. A reset of a `blocked` task clears `blocked_by`; the
finding stays open ([plan.md](plan.md) "Diff"). Claims refuse while a persist is in progress, and so do
the story calls that lock stories and tasks (`report_progress`, `complete_story`, `block_story`, `unblock_finding`,
`release_story` with a token): a persist locks a task before its story, those calls the other way round. A forced
`release_story` does not check it.

## Story progress

A story's lifecycle (`active`, `retired`) is stored; its progress is derived from its tasks at read
time and never stored, so a superseded task reopens its story without a write. Closed tasks
(`superseded`, `retired`) are ignored.

| Progress | When |
|---|---|
| `completed` | At least one task, and every task `completed` |
| `blocked` | Any task `blocked` |
| `in_progress` | Otherwise, any task `in_progress` or `completed` |
| `pending` | Otherwise (no task yet, or every task `pending` or `failed`) |

**Story order** is a projection of the task graph, for people: story A waits for story B when a task of
A waits for a task of B. It may contain cycles (A's contract before B's logic, B's contract before A's
logic). Cycles are collapsed into story groups for display, and nothing enforces story order; the task
graph decides what is ready ([api.md](api.md) `plan_dag`).

## Task brief

`task_brief(project_id, task_id)` assembles what one coding agent needs, from the plan and the current
spec graph, at the time it is asked (not at planning time, so it always shows finished prerequisites).
It makes no model call.

| Part | Content |
|---|---|
| Task | id, title, description, phase, `complexity`, status, `attempts`, `release` (the distinct releases of the implemented nodes), `has_open_questions` |
| Done when | Each item with its `source`: `spec` (from the implemented nodes' `acceptance_criteria`; the spec guarantees it) or `planner` (written from the statement; the planner's reading) |
| Story | id, kind, title, summary, anchor `spec_id`, `release`, derived progress |
| Area | id, title, description, interfaces: the spec Interfaces the area provides (name, kind, `spec_id`, summary), and interfaces the describer added, marked `planned_by: model` |
| Project | The project profile, with its conventions (`text`, `spec_ids`) every task follows |
| Implements | Each implemented spec node: `spec_id`, label, name, ontology attributes, statements, provenance (document path, lines, quote); nodes the rules flag as assumptions are marked |
| Contracts | Each completed direct prerequisite: task id, title, commit, `implements`, and the interfaces of its area that cite those spec nodes |
| Open questions | Each open question RELATES_TO an implemented node: `spec_id`, statement, provenance, the node it concerns |
| Neighbours | Spec nodes one domain edge away from the implemented nodes (`spec_id`, label, name, relation, direction, `basis`, `confidence`), at most `neighbours` (an argument, default 30), in relation and `spec_id` order |
| Placement | For each implemented node, the partition rule that placed it in the area and the edges it used, with `basis` and `confidence` (an inferred owner is less certain than a stated one) |
| Spec state | `spec_changed`: an implemented node was written by a build after the plan's last consumed build (node `last_build_id` newer than `Plan.last_build_id`); `missing`: implemented `spec_id`s that no longer resolve |

`spec_changed` is a conservative hint: it is also true for a node a later build only reworded. The next
plan run decides from the build change reports whether the task is redone.

A brief never contains other tasks' briefs, prompts or the whole graph.

## Story brief

`story_brief(project_id, story_id, *, neighbours=30)` merges the task briefs of the story's open and
completed tasks, from one read, without a model call: the story with its anchor, the area and project
parts once, every task with its `done_when` items and sources in topological order, the implemented
nodes, open questions and neighbours (at most `neighbours` in all) merged by `spec_id`, contracts of the
completed prerequisites outside the story with their commits, and `spec_changed` (any task's) and
`missing` merged (`spec_changed` here stays the brief's boolean, true when any implemented node changed).
A retired or unknown story raises `PlanReadError("story_not_found")`. The brief also carries `findings`:
the open findings about the anchor and the implemented nodes (`finding_id`, `kind`, `text`, `blocking`,
`about_spec_ids`, `story_id`, `created_by`, `created_at`, `orphaned`), blocking first, then newest first, at
most 200. `graph.plan` never imports `graph.gap`: `PlanService` reads them through a structural
`FindingsReads` port that the host fills with `graph.gap.Findings` ([../gap.md](../gap.md)). A service built
without the port has an empty `findings` list.

## Agent contract

`graph.plan` runs no agents, git or tests, and there is no executor in its scope. Users connect their
own coding agents to the app over MCP; the MCP server (host code) gives every agent these
instructions. The plan does not check how the work was done.

1. Pick work by story or task. Claim a ready task with `claim_task`, or the story's next ready task with
   `claim_next`. On `not_ready` or `nothing_ready`, take the listed blockers first or pick other work.
   Keep the returned claim token with the workspace state.
2. Read the brief. When it shows `spec_changed` or `missing`, release the task and ask for a plan run
   instead of building on an outdated task.
3. Start from a workspace that contains the commits of every completed prerequisite (brief
   "Contracts"), and never from a commit the plan lists as stale.
4. Build to `done_when`. Items with source `spec` are binding; items with source `planner` are the
   planner's reading of the statement and may be questioned. Follow the profile's conventions.
5. When an open question or the spec itself does not allow a decision the code needs, report
   `block_task` with a reason code (`spec_ambiguous`, `spec_contradiction`, `spec_missing`) instead of
   guessing. `environment` and `other` cover the rest.
6. Report `complete_task` only with the commit that holds the integrated work. An intermediate commit
   (tests only, work in progress) is never reported. Report `fail_task` when giving up for any other
   reason, and `release_task` when stopping without a result.
7. Log and store no document text from briefs outside the workspace.

Checking that a reported commit exists in the repository is host-side and optional; the plan cannot
see git. A client may map `complexity` to models; the plan stores no model name.

**Agents over MCP** work by story ([../../mcp-server/tools.md](../../mcp-server/tools.md)): claim a
story, read its story brief, report each task's progress, add a finding when the spec does not allow a
decision (a blocking one replaces `block_task` and ends the claim), and report `complete_story` with
the merge commit only after the merge; release the story when stopping without a result. Points 2 to 4
and 7 hold as written.

**Recovery.** `stale_claims(project_id, *, older_than)` lists `in_progress` tasks claimed longer ago than
the host's timeout. The host completes each one whose integrated commit exists (with the claim token,
or after a forced release and a new claim) and releases the rest with `force=True`. Forced releases are
a host operation, not an agent tool. The tasks of a forgotten story claim show there too; a person
frees the story with `release_story(force=True)` from the web app's claims board.

## Plan checks

Read-only checks of the persisted plan, after every plan run and on request
(`check_plan(context, stale_after=None)`). They read the plannable spec nodes and the plan; foreign data
of other owners is never a problem.

| Check | Problem |
|---|---|
| `membership` | A plannable spec node without exactly one `IN_AREA` to an active area, or an `IN_AREA` from a node that is not plannable (PD1) |
| `task_area` | An open task whose `area_id` is not an active area (PD2) |
| `coverage` | A node of a `required` type that no open or completed task implements, that is not recorded `not_buildable`, and that no profile convention cites (PD3) |
| `dependencies` | A `WAITS_FOR` cycle among open (other than `in_progress`, whose waits are frozen) and completed tasks, or a wait of a task other than `in_progress` on a closed task (PD4) |
| `task_state` | Claim, `progress`, `blocked_by` or commit fields that do not match the status (PD5), or story claim fields that do not match their tasks (PD8) |
| `stories` | An open task not `IN_STORY` exactly one active story of its own area, or an active `feature`, `interface` or `entity` story that does not `IMPLEMENTS` exactly its anchor, except one kept for claimed work after its anchor was deleted (PD7) |
| `lost_target` | An open task whose `implements` names a `spec_id` that no longer resolves (build deleted the node) |

| Metric | Counts |
|---|---|
| `outdated` | The graph fingerprint differs from the plan's: a plan run is due |
| `ready`, `blocked`, `failed` | Tasks in those states |
| `stale_claims` | Claims older than `stale_after` (only when given) |
| `stale_commits` | Superseded and retired tasks with a commit |
| `not_buildable` | Required nodes recorded `not_buildable`, by reason |
| `open_questions_on_tasks` | Open tasks with `has_open_questions` |
| `stories` | Active stories by derived progress |

The report is `ok` when no check has a problem. Problems name ids only. Plan checks repair nothing: the
next plan run rebuilds membership, stories, coverage and waits from the spec graph, and the state
fields are owned by the lifecycle calls.
