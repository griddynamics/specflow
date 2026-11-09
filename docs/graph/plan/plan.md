---
type: Design
title: "Graph plan runs"
description: "How graph.plan turns the spec graph into a plan and keeps it current"
status: stable
tags: [graph, plan]
---
# plan.md: how graph.plan turns the spec graph into a plan and keeps it current

## Modes

| Mode | When | What is planned by a model |
|---|---|---|
| `first` | The project has no `Plan` node | Everything |
| `update` | Every later run | Only areas, planning parts and the project plan whose reuse key changed ("Reuse"): a member with a meaning change in the change reports, a changed edge, or a changed membership. Stored planner answers are reused for the rest |
| `replan` | Only when the host asks | Everything again; no stored answer of an earlier run is reused. Current tasks are shown to the planners as in `update`, so task ids, finished work and claims are kept the same way |

The library picks `first` or `update` from the persisted plan; the host asks for `replan` with a flag in
the plan context (after a prompt, rules, plan ontology or option change, or to refresh a plan that
drifted). A changed rules, plan ontology or options version alone re-plans nothing in `update`. Every
mode diffs against the plan nodes that exist, so a `first` run after an interrupted persist does not
duplicate them.

**Change reports decide.** The host passes `PlanContext.changes`: the build change reports of every
build since the plan's last consumed build, in build order, as the plan-side model `BuildChangeReport`
([api.md](api.md)). Each report holds `build_id`, `previous_build_id` (the project's build before it); `nodes`: `{spec_id, classes, previous_spec_ids}`;
`edges`: `{source, relation, target, change: added | removed | changed, basis}` (source and target are
`spec_id`s). The class names are those of the build change report: `added`, `removed`, `merged`,
`carried_over`, `rekeyed`, `changed_meaning`, `changed_links`, `changed_evidence`. This is a documented
contract with the host; `graph.plan` never imports `graph.build`. Snapshot merges the reports into one
change set:

| Change | From |
|---|---|
| Meaning change | A node classed `added`, `removed`, `merged`, `carried_over`, `rekeyed` or `changed_meaning` |
| Link change | A node classed `changed_links`, and both ends of every added, removed or changed edge, inferred edges included |
| Ignored | `changed_evidence`: a reworded statement, a moved line or a new vector changes no plan |

A `spec_id` merged away or rekeyed in one report and changed in a later one resolves through the
registry aliases (`GraphNode.spec_id_aliases`, read by `GraphReader`) and `previous_spec_ids`. Each node's **meaning revision** is the build id of its last
meaning change, kept in the `PlanStore`. The consumed build of a run is `PlanContext.build_id`: the
project's last build, which the graph reflects. `Plan.last_build_id` records it when the run persists,
and the `PlanStore` keeps the chain of consumed builds. Build ids are ordered only by that chain, never
by value: "a build after `Plan.last_build_id`" means a build that is not in the consumed chain.

Plan update correctness follows the build's change-report classification: a node wrongly classed
`changed_meaning` supersedes finished work that did not change, a real change classed `changed_evidence`
leaves finished work in place. Every superseded task is reported with the class that caused it ("Plan
result"), so the host can check it.

**Missing reports.** A gap in the chain raises `ConfigurationError("change_reports_missing")` naming the
missing builds: in `update`, the first report's `previous_build_id` is not `Plan.last_build_id`, a
report's `previous_build_id` is not the `build_id` of the report before it, or the last report's
`build_id` is not `PlanContext.build_id`. In `first`, `changes` must be empty (passed reports raise `change_reports_missing`); in `update` at
least one report is required. The host passes the missing reports or runs `replan`.

**`replan` without reports.** Every area is re-planned. A completed task is superseded only when an
implemented node no longer resolves. A completed task kept although an implemented node has a
`last_build_id` outside the consumed chain is listed in `unverified_completed`, for the host to
review: a real meaning change may have gone unnoticed. Each node whose `last_build_id` is outside the consumed
chain gets `PlanContext.build_id` as its meaning revision.

**No change.** An `update` run whose change set has no meaning or link change, and whose previous run
left nothing open (no fallback, no `conflicts`, no `in_progress_changed`), ends after Snapshot: no model
call, and persists only `Plan.last_build_id`, `consumed_build_ids`, `graph_fingerprint` and the meaning revisions.
When something is open, all steps run with every answer reused, so only areas planned by the fallback
call a model again.

**When the host runs a plan.** After a build returned (with its follow-up builds), unless the build's
settlement lists `documents_incomplete`, and never during a build of the same project
([architecture.md](architecture.md)). The plan itself checks nothing about settlement. A new run while
the project's previous run is unfinished raises `ConfigurationError("unfinished_run", run_id=...)`; the
host resumes that run first (a resume after a spec change ends it `failed`, which frees the project for
a new run).

## Steps

A plan run executes these steps in order. Each step stores its output in the `PlanStore` (artifacts) so
a host can run steps one by one and resume; `run_plan` runs them all ([api.md](api.md)).

| # | Step | Output |
|---|---|---|
| 1 | Snapshot | Plannable and context nodes, edges with basis and confidence, the current plan, the change set and meaning revisions, the graph fingerprint, mode |
| 2 | Partition | Work areas, the area of every plannable node, stories and the home story of every plannable node, planning parts |
| 3 | Describe | Title, description and interfaces of each area; summary of each story |
| 4 | Project | Project profile with conventions, setup tasks, closing tasks |
| 5 | Tasks | Tasks of each area with their story and their waits inside the area; `not_buildable` entries |
| 6 | Stitch | Waits across areas and from setup and closing work; one acyclic task graph |
| 7 | Review | Advisory review issues |
| 8 | Diff | Changes from the current plan to the target plan |
| 9 | Persist | The diff written to the graph store |
| 10 | Check | Plan check report ([lifecycle.md](lifecycle.md) "Plan checks") |

**Resume.** Before Persist starts, a run resumes at its first unfinished step only while the graph
fingerprint still equals the one its Snapshot recorded; otherwise the run ends `failed` (`graph_changed`)
and the host starts a new run. Once Persist has started, a resume finishes it without the fingerprint
guard; the next check then reports the plan `outdated`. Answers a run stored are reused when that same run resumes, in every mode, so no planner call
is repeated.

### Snapshot

1. Read through `GraphReader`:
   - the plannable nodes: the types of the plan rules, minus excluded nodes. Excluded are nodes whose
     attributes match a type's `exclude` (external modules) and nodes that match `exclude_all`
     (`in_scope: no`) themselves or through their owner (the target of their containment edge);
   - the context nodes: external modules and nodes of role `context` (open questions). They are read
     for placement, briefs and planner input, never placed or planned;
   - every domain edge that touches a plannable node, inferred edges included, with its `basis` and
     `confidence`.

   A project without a plannable node raises `ConfigurationError("no_spec_graph")`.
2. Read the current plan: `Plan` (with `last_build_id`), areas, stories, tasks and the plan
   relationships with their stored keys.
3. Check the report chain and build the change set ("Modes"); compute the run's meaning revisions.
4. Compute the graph fingerprint ([data-model.md](data-model.md) "Fingerprints"). It is a guard only: a
   resume needs it unchanged and the `outdated` check compares it. It never decides what is re-planned.
5. Choose the mode; in `update`, stop here when nothing changed ("Modes").

### Partition

Every plannable node gets exactly one area. The rules are deterministic and follow the ontology spine;
the type and relation roles come from the plan rules ([ontology.md](ontology.md)). A module is internal
when it is not excluded. The hop limit is the `max` of the
ontology spine path whose `via` is the containment relation (3 in the shipped ontology). A symmetric
relation counts in either direction. Placement uses every edge, inferred ones included.

1. Each internal module (role `area_anchor`) anchors a module area, deployment and datastore modules
   included.
2. The project area always exists.
3. An interface goes to the area of its provider (provision role) when the provider is internal; else
   to the area of the single internal module that consumes it (consumption role) or integrates with its
   provider (integration role); else to the project area (counted `cross_cutting`).
4. A data entity goes to the area of the single internal module it is stored in (storage role); else to
   the project area (counted `cross_cutting`).
5. A capability (role `shared`) realised by exactly one internal module goes to that module's area;
   realised by several, to the project area (counted `cross_cutting`); by none, to the project area
   (counted `unplaced`).
6. Every other plannable node takes the first of:
   1. **Owner walk.** Follow outgoing containment edges, through other plannable nodes, within the hop
      limit, to the nearest anchor. An interface or data entity owner gives that hub's area (rules 3
      and 4); a module owner gives its area; an external-module owner gives the area of the single
      internal module that consumes its interfaces or integrates with it, else the project area (counted
      `unplaced`). At
      equal distance the anchor with the lowest `spec_id` wins and the node is counted
      `ambiguous_membership`. The walk stops at the first anchor, so a sub-module's requirements stay
      in the sub-module's area;
   2. the area of a capability it is grouped under by its own grouping edge (lowest capability
      `spec_id` first), as rule 5 placed it;
   3. when its reference edges reach exactly one internal module, that module's area; several, the
      project area (counted `cross_cutting`);
   4. the project area (counted `unplaced`).
7. Area ids follow [data-model.md](data-model.md) "Identity", assigned in this order: the project area,
   then module areas by anchor `spec_id`. A persisted area keeps its id while its anchor exists. An
   area whose anchor is gone, or now resolves (through a `spec_id` alias after a build merge) to the
   anchor of another area, becomes `retired`; its anchor's return makes it `active` again under the
   same id.

A node that moved area since the last run (an orphan that inference attached, for example) takes its
open task along: Diff matches tasks by their `implements` set. A move alone supersedes no completed
task.

**Stories.** Partition creates the stories of every area, with ids per [data-model.md](data-model.md)
"Identity" (`STORY::<area slug>::<SLUG>`):

| Story | Kind | Exists for |
|---|---|---|
| `<anchor spec_id without prefix>` | `feature`, `interface`, `entity` | Every plannable node of a type whose rules set `story: <kind>` (`feature`, `interface` or `entity`), in its own area; nested features are their own stories |
| `CORE` | `core` | A module area with members whose home is no feature, interface or entity story |
| `CH_<document slug>_<chapter slug>`, slugged like any story id (stop words dropped, at most 5 words including `CH`) | `chapter` | Project area: each source document chapter that holds a member whose home is no feature, interface or entity story |
| `SETUP`, `CLOSING` | `setup`, `closing` | Project area, always: setup and closing tasks |

Every plannable node except an area anchor has exactly one **home story** in its own area: a story anchor's own story; else
the story of the nearest story anchor it reaches by containment within the hop limit, inside its area;
else `CORE` in a module area, or the chapter story of its first source (lowest document title, then
position) in the project area; a project-area node without a source chapter is homed to `SETUP`
and is covered by the Project planner. A slug already held in the area gets `_2`, `_3`. A story's title is its
anchor's name (chapter: the chapter heading); its `release` is its anchor's. A story whose anchor is
gone, or which no longer holds a member, becomes `retired`; it becomes `active` again under its id when
it returns.

**Planning parts.** Parts are built from whole stories, in story id order: a story with more than
`planner_members` members is split into chunks in `spec_id` order; smaller stories are packed together
up to `planner_members`. An area with at most `planner_members` members is one part. In the project
area the planned stories are the chapter stories and the stories of interfaces, entities and features
placed there; `SETUP` and `CLOSING` are planned by Project. Parts are numbered from 1 in that order.

**Fallback ordinals.** Partition numbers the active stories of each area from 1 in story id order; the
fallback uses this ordinal ("Validation of planner answers").

### Describe

One call per area whose Describe key changed ("Reuse"); the project area too. Input: the anchor and
members (`spec_id`, label, name, `stmt`, `acceptance_criteria`, assumption flag, `quote`, `release`,
home story), the area's stories (id, kind, title, anchor), the spec interfaces the area provides, and
the edges that touch the members (other end's `spec_id`, name and area; `basis` and `confidence`). An
area with more than three times `planner_members` members shows the anchor, its story anchors and the
first members in `spec_id` order up to that size (counted `describe_truncated`). Output:

- `title`, `description` (responsibility and what the area does not own);
- `interfaces`: one entry per spec Interface the area provides, with `name`, `kind` and `spec_id` set
  by code from the node and a `summary` written by the describer; the describer may add interfaces the
  text implies but no Interface node names (`name`, `kind`, `summary`, `spec_ids` of members), marked
  `planned_by: model`;
- `stories`: a `summary` per story of the area (`SETUP` and `CLOSING` are summarised by Project).

### Project

One call when the Project key changed. Input: internal modules (`type`), deployment modules and the
deployment edges to them; external modules with the interfaces they provide or consume and the
integrations with them; members of the types the rules mark `project_input`; the members homed to
`SETUP`, which the answer must cover; every area's id, title and interfaces. Output:

- `profile`: languages, frameworks, package layout, test command, test paths, the `spec_id`s they rest
  on, and `conventions`: `[{text, spec_ids}]`, conventions and policies every task follows; the planner
  may cite requirements of its input there instead of planning tasks for them;
- `setup_tasks` (story `SETUP`): workspace and layout, toolchain, configuration, shared libraries, test
  harness, CI; exactly one has no wait (the setup root) and every other waits for it directly or
  indirectly;
- `closing_tasks` (story `CLOSING`): end-to-end acceptance across areas, packaging, cross-cutting
  quality checks; they name no waits, Stitch adds them;
- a `summary` for `SETUP` and for `CLOSING`.

Setup and closing tasks belong to the project area and have the task fields of step 5 except `story`,
which code sets.

### Tasks

One call per part of every area whose Tasks key changed, the project area included (setup and closing
tasks are shown as existing work and are not re-planned here). Input:

- the part's members as in Describe, each with its home story, and the part's stories;
- open questions related to a member by a reference edge (text and `spec_id`), as context;
- the area's title, description and interfaces;
- the interfaces the area consumes, with their provider area and contract task ids, and the consumer
  areas of the interfaces it provides; the areas it integrates with, in both directions;
- the project profile with its conventions, and the setup task ids;
- tasks already planned for earlier parts of the area;
- the part's current tasks (id, action, phase, status, story, `implements`): those of its stories or
  implementing one of its members, never a setup or closing task and never one whose action an earlier
  part of the area already holds; each marked whether its spec changed, with the instruction to keep the
  action of work whose spec did not change (given the whole area's tasks, a part copied the tasks of
  other parts and waited on their actions);
- the phase guidance and `task_size`.

Output:

- per task: `story` (one of the part's stories), `action`, `phase`, `title`, `description`,
  `implements` (`spec_id`s), `waits_for` (actions in the area), `done_when`, `complexity`.
  `done_when` is a list of `{text, source}`: `spec` items restate an implemented node's
  `acceptance_criteria`, `planner` items are written by the planner from the statement and quote;
- `not_buildable`: `[{spec_id, reason, note}]`, members that are not code work (a process, governance or
  release-gate rule, a policy with nothing to build), with `reason` from the `not_buildable_reason`
  vocabulary.

Rules for the planner: every interface the area provides gets a `contract` task that implements it;
every data entity gets a `data` task; unit tests are part of every task; an `acceptance` task only for
acceptance criteria that span several tasks of the area; a node flagged as an assumption is still
implemented, and the task description says the behaviour is assumed, not committed.

Code then sets on every task (planned or fallback, setup and closing included):

| Field | Value |
|---|---|
| `release` | The distinct `release` values of the implemented nodes, a node's own value, else its feature's (by containment); empty when none |
| `has_open_questions` | An open question relates to an implemented node by a reference edge |

No release filters or orders tasks.

### Validation of planner answers

The gateway checks the JSON schema. Before the checks, code repairs what has one right reading
(`graph.plan.normalise`), so neither a second call nor a fallback is spent on it:

| Repair | Applies to |
|---|---|
| a `phase` or `complexity` that is a vocabulary value in another letter case becomes that value | tasks, setup and closing tasks |
| a `spec` `done_when` item of a task none of whose implemented nodes has `acceptance_criteria` becomes `planner` | tasks, setup and closing tasks |
| a wait for a setup task (by action or task key) is dropped: Stitch rule 1 adds it | tasks |
| every wait of a closing task is dropped: Stitch rule 8 adds them | project |

Then the plan checks:

| Check | Applies to |
|---|---|
| `action` matches `^[A-Z][A-Z0-9_]{1,39}$`, does not end in `_R` and digits, unique in its area | tasks, setup and closing tasks |
| `phase`, `complexity`, interface `kind`, `done_when` `source`, `not_buildable` `reason` are vocabulary values; a setup task has phase `setup`, a task of a Tasks answer never | all |
| a provided interface has a `contract` task and a data entity a `data` task (`missing_phase`) | tasks |
| a closing task has no `waits_for` (`closing_waits`) | project |
| every required member homed to `SETUP` is covered | project |
| a title is not empty (`empty_title`); story summaries name the area's stories (`unknown_story`); an interface `spec_id` is one the area provides | describe |
| `story` is one of the part's stories | tasks |
| every `implements`, `not_buildable` and interface `spec_id` is a member of the area or part (setup, closing and conventions: any plannable node of the input) | all |
| `done_when` is not empty; a `spec` item only when an implemented node has `acceptance_criteria` | tasks, setup and closing tasks |
| a `not_buildable` member is implemented by no task of the answer | tasks |
| every member of a `required` type is implemented by a task of the part, listed `not_buildable`, or cited by a convention of the project profile | tasks |
| `waits_for` names known actions; waits inside the area are acyclic | tasks, setup tasks |
| exactly one setup root | project |
| more than `task_size` entries in `implements` | warning `oversized_task`, not an error |

A failed check sends the answer back once with the list of problems, each as its code and ids followed by
one sentence on what it means and how to fix it (`PROBLEM_HINTS` in `graph.plan.prompts`; `max_attempts`
calls in all). The logs `graph.plan.planner_invalid` and `graph.plan.planner_fallback` count the problem
codes (`codes`). When
the answer still fails, or the call fails with a non-fatal `LLMError`, the planner falls back:

| Planner | Area or project has current plan content | No current content (`first`, new area) |
|---|---|---|
| Describe | Keep the current title, description, interfaces and story summaries; add spec interfaces without summary | Title = the anchor's name; spec interfaces without summary; no story summaries |
| Project | Keep the current profile, setup and closing tasks | One setup task `WORKSPACE` in `SETUP`, no closing task, empty profile |
| Tasks | Keep the area's current tasks and `not_buildable` entries; add rules tasks only for required members no current task implements and no entry or convention covers | Rules tasks for the part |

**Rules tasks.** For each story of the part, the story's members grouped by the default phase of their
type, and each group cut in spec id order into tasks of at most `task_size` members, in that story.
Title: `<Phase>: <member names>` (cut to 120 characters); description: one line per member with its
name and statement, an assumption marked "assumed, not committed" (cut to 3,000 characters).
Action: `<PHASE>_<n>`, with `<PHASE>` the phase in upper case and `n` the story's fallback ordinal
(Partition); when that action is already held in the area by a task of another story, another answer
or an earlier cut of the same group, `<PHASE>_<n>_<m>` with the lowest free `m` from 2. The action is unique in the area and the same
for the same stories; an ordinal that shifts between runs only renames the task, which Diff matches by
its `implements` set. Waits inside the area follow the `after` and `after_mode` of each phase over the
area's rules tasks; complexity by phase; `done_when` one `planner` item per implemented node, restating
its statement, plus a `spec` item per acceptance criterion. The run records `planned_by = rules` for
what the fallback wrote, and a fallback answer is never stored for reuse. `LLMFatalError` and
`InfrastructureUnavailableError` are not fallbacks: the step raises, the step is `failed` and the run
stays unfinished, to be resumed by its run id (no stored planner answer is asked again).

### Stitch

Python adds waits (`WAITS_FOR`, `source = stitch`), in this order; planners never write waits across
areas. A root task is a task with no wait inside its area; setup and closing tasks are never roots of an
area. A sink is a task no other task of its group waits for. Rules 2 to 5 use only `stated` and
`implied` edges; inferred edges place nodes but never order work.

1. **Setup.** Every root task of every area, the project area included, waits for every setup sink.
2. **Interfaces.** For every interface provided in area P and consumed (consumption role) by an
   internal module of area A ≠ P: A's `integration` tasks, else its `logic` tasks, wait for P's
   `contract` tasks that implement the interface.
3. **Integration without an interface.** For every integration edge (integration role) from a member of
   area A to a member of area B ≠ A, where no interface of B is consumed by A: A's `integration` tasks, else its
   `logic` tasks, wait for B's `contract` tasks, else B's sinks that are not `acceptance` tasks.
4. **Spec dependencies.** For every edge of the dependency role (the spec `DEPENDS_ON`;
   requirement → requirement, feature → feature):
   the tasks implementing the source wait for the tasks implementing the target; a task never waits for
   itself.
5. **Data.** For every data entity E in area D: tasks of another area S that implement a node owned by
   E (containment) or linked to E by a reference edge wait for D's `data` tasks that implement E.
6. **Deployment.** A deployment area is an area whose anchor is the target of a deployment edge from an
   internal module. Its root tasks wait for the setup sinks (rule 1); closing tasks of phase
   `acceptance` wait for the sinks of every deployment area.
7. **Acceptance.** An `acceptance` task never waits for another area's `acceptance` task; a rule that
   would add such a wait adds nothing.
8. **Closing.** Every closing task waits for the sinks of the areas whose members it implements, or of
   every area when it implements none.

Then the whole graph is checked for cycles. A cycle is broken by dropping the stitch edge in it that was
added last, in the order rule, then `area_id`, then `task_id`; each drop is counted in
`dropped_dependencies`. Waits inside an area are acyclic by validation.

### Review

Off when `review` is false. One call on the `judge` call class of the gateway the run is given, with
`PlanOptions.review_model` as its model override (`None` means the gateway's judge model). Input: the
stitched plan (areas with title and interfaces; stories with id and title; tasks with
id, story, phase, title, `implements`, waits, `done_when` with sources), the `not_buildable` entries,
the profile conventions, and the members of each area. Criteria: every member is built by a task that
can build it, or is truly not code work; contracts and data come before the logic that uses them; no
task is too large for one agent run; `planner` items of `done_when` agree with the quote; setup covers
what the areas assume; closing work covers cross-area acceptance. Output: issues `{area_id, task_id |
null, problem, severity: blocking | advisory}`. Issues are stored as a run artifact and counted in the
result; they change nothing ([later.md](later.md)). A failed review call is counted as `review_failed`.

### Diff

**Spec changed.** A task's spec changed when an implemented node's meaning revision is newer than the
task's `spec_build_id`, or an implemented node no longer resolves. The cause is the class of the latest
meaning change among the implemented nodes (`unresolved` for a node that no longer resolves in a
`replan` without reports). Node fingerprints are not used.

Target tasks are matched to current tasks in two passes:

1. By task key without revision (`TASK::<area slug>::<action>`); the current task of a key is its
   latest revision.
2. Unmatched current tasks against unmatched target tasks with the same `implements` set, in the same
   area or across areas (a node moved area, the planner renamed an action, or a fallback ordinal
   shifted); the target takes the current task's id.

| Current task | Condition | Result |
|---|---|---|
| `completed` | Spec not changed, no cascade | Kept: id, status, commit unchanged; title, description, `done_when`, story and `release` updated |
| `completed` | Spec changed, or cascade | Becomes `superseded`; a new revision `<key>_R<n>` is created `pending` and `SUPERSEDES` it; the commit is listed as stale; the cause is reported |
| `pending`, `failed`, `blocked` | Spec changed | Updated in place and set to `pending`; a block is cleared |
| `pending`, `failed`, `blocked` | Spec not changed | Content updated; status unchanged |
| `in_progress` | Any | Not touched, relationships included; listed in `in_progress_changed` when its spec changed or a prerequisite is superseded or retired |
| `retired` without commit | Key returns | Updated in place and set to `pending` |
| `retired` with commit | Key returns | A new revision is created `pending` and `SUPERSEDES` it |
| none | - | Created `pending` |

A current `pending`, `failed`, `blocked` or `completed` task with no target becomes `retired`; a commit
it had is listed as stale. In a `replan` without reports, "spec changed" for a completed task means only
an unresolved implemented node ("Modes"). Diff sets `spec_build_id` to the consumed build on every task
it writes; `in_progress` tasks and tasks left as conflicts keep theirs.

**Cascade** ([questions.md](questions.md) Q4): a `completed` task with a `WAITS_FOR` (source `planner`,
inside its area) to a task that is `superseded` or `retired` is superseded too (cause `cascade`), even
when its own spec did not change, and so on inside the area. The cascade starts only from tasks
superseded or retired because of meaning changes or lost work, never from link changes or moves. A
completed task whose cross-area (`stitch`) prerequisite was superseded is kept and listed in
`affected_dependants`. A prerequisite that is only added supersedes nothing.

**Stories** are matched by id. A target story without a current one is created `active`; a current
active story with no target becomes `retired`; a retired story that is a target again becomes `active`
under its id. Title, summary, kind and `release` are updated. A story that still holds an `in_progress`
task stays `active` until that task leaves `in_progress`. Story progress is never stored: it is derived
at read time from the story's tasks ([lifecycle.md](lifecycle.md)), so a superseded task reopens its
story by itself.

**Relationships.**
- `WAITS_FOR` is rewritten for every `pending`, `failed` and `blocked` task and for new tasks; a wait on
  a superseded task points to its successor. `in_progress` and closed tasks keep theirs; a kept
  `completed` task's wait on a superseded task points to the successor, and its wait on a retired task is
  removed.
- `IN_STORY` is set to the target story for every task Diff writes; `in_progress` and closed tasks keep
  theirs.
- `IN_AREA`, `IMPLEMENTS` (task and story) and `IN_STORY` are matched on (type, source node, target
  node), whatever key they carry: build may have moved them to a merge survivor with their old key.
  Missing ones are created with keys from current ids; extra ones (a duplicate after a merge, a node
  that left the area, a task that changed story) are deleted by their stored key.
- Closed tasks and retired stories keep their relationships as history.

The diff and the plan changes are stored before anything is written. The target plan is checked
against PD1-PD5 and PD7 first; a violation raises `ConformanceError`.

### Persist

The first batch sets `Plan.applying` to the run id; while it is set, `ready_tasks` returns nothing and
claims raise `TaskStateError("plan_applying")`. Then the diff is applied in batches, each its own
idempotent transaction: areas, stories, tasks and their properties, supersessions and retirements,
relationships; the last batch writes the `Plan` node (graph fingerprint, `last_build_id`, versions,
profile) and clears `applying`. A cursor records the last applied batch, so an interrupted persist
resumes there. The run's meaning revisions become the project's revisions in the `PlanStore` when the
last batch is written.

A task change is applied only while the task's status is still the one the snapshot read; a task
claimed or reported in the meantime is left as it is and counted in `conflicts`, and the next run
reconciles it.

### Check

The plan checks of [lifecycle.md](lifecycle.md) run read-only on the persisted plan, the `stories` check
(PD7: every open task is `IN_STORY` exactly one active story of its own area) and the story metrics
included; their report is stored and summarised in the result. A check problem never changes the run
status.

## Model calls

Every planner call goes through the core gateway as one structured call: `extraction` call class (main
model) for Describe, Project and Tasks, and the `judge` call class for Review with `PlanOptions.review_model` as its model override ("Review"). Prompts are the files `graph/plan/prompts/<name>.md`; each has a revision (`PROMPT_REVISIONS` in
`graph/plan/prompts.py`), raised with every change of its text, and the revision used is recorded on what
it produced (`prompt_ref`, `<name>.<revision>`). Spec text (names, statements, quotes, open questions) is
placed in untrusted blocks and the prompts say it is data, not instructions. Calls of one step run in
parallel under the gateway limiter; parts of one area run in order, because later parts see earlier
tasks. Usage per step is read from the shared gateway, so it is approximate when other work uses the
gateway at the same time. `estimate_plan` gives the calls and input tokens of a run without making
model calls ([api.md](api.md)).

## Reuse

| Answer | Reused when | Stored in |
|---|---|---|
| Area description | Same Describe key: the area's member `spec_id`s with their meaning revisions, the domain edges that touch a member (relation, other end's `spec_id` and area, basis), the area's story ids, the `spec_id`s of the interfaces it provides | `PlanStore`, kind `describe` |
| Project plan | Same Project key: the `spec_id`s of its input nodes with their meaning revisions, the deployment and external integration edges, area ids with their anchors and provided interface `spec_id`s | `PlanStore`, kind `project` |
| Tasks of a part | Same Tasks key: the part's member `spec_id`s with their meaning revisions and the domain edges that touch them (as for Describe), the part's story ids, the ids of the areas the area provides to, consumes from or integrates with, setup task ids, the `spec_id`s cited by profile conventions, `task_size` and `planner_members` | `PlanStore`, kind `tasks` |

Keys hold ids and meaning revisions only, never text (model-written or spec) and never node
fingerprints, so a reworded statement re-plans nothing and re-planning one area does not change another
area's key. Every key includes the project id. A `replan` reuses no answer of an earlier run. Prompt
revisions are not part of the keys.

## Failures

| Failure | Effect |
|---|---|
| A change report is missing from the chain | `ConfigurationError("change_reports_missing")` before any step; the host passes the reports or runs `replan` |
| A planner answer stays invalid, or a call fails with a non-fatal `LLMError` | Fallback for that area, part or the project; the run ends `partially_succeeded` |
| The endpoint rejects credentials or the model; the graph store or the port is unreachable | The step raises `LLMFatalError` or `InfrastructureUnavailableError`; the step is `failed`, the run stays unfinished and resumes by run id; nothing is written before Persist |
| A write fails | `GraphWriteError`; persist resumes from its cursor |
| The spec graph changed before a resume | The run ends `failed` with `graph_changed` |
| The target plan breaks an invariant before Persist | `ConformanceError`; the run is `failed` |

## Plan result

| Field | Meaning |
|---|---|
| `status` | `failed` on a configuration or conformance error, or `graph_changed`; `partially_succeeded` when a fallback was used; else `succeeded`. Review issues, `review_failed`, `conflicts` and dropped dependencies never change it |
| `mode`, `unchanged`, `build_id` | The mode; true when the run ended after Snapshot; the consumed build |
| `changes` | Task ids `added`, `updated` (content only), `reopened` (set back to `pending`, or revived from `retired`), `superseded` (with successor and cause: a change class, `cascade` or `unresolved`), `retired`, `kept`; area ids and story ids `added`, `retired`, `reactivated` |
| `stale_commits` | Commits of tasks superseded or retired in this run, by task id |
| `unverified_completed` | Completed tasks kept by a `replan` without reports although an implemented node was written by a build after `Plan.last_build_id` |
| `in_progress_changed`, `affected_dependants`, `conflicts` | Task ids left alone ("Diff", "Persist") |
| `planning` | Per planner: calls, reused answers, fallbacks, invalid answers; review issues by severity, `review_failed`; `dropped_dependencies` |
| `checks` | `ok` and problems per check ([lifecycle.md](lifecycle.md)) |
| `metrics` | Areas; stories by kind and by progress; tasks by status and phase; `not_buildable` (by reason); `open_questions_on_tasks`; `oversized_task`, `ambiguous_membership`, `cross_cutting`, `unplaced`, `describe_truncated`; calls and tokens per step, duration per step |

The result names tasks, stories, areas and spec nodes by their ids, never by text.

## Plan options

| Option | Default | Meaning |
|---|---|---|
| `review` | on | Run the Review step |
| `review_model` | `None` | Review model override on the shared gateway; `None` means the gateway's judge model |
| `task_size` | 5 | `implements` entries per task above which a task is reported as oversized |
| `planner_members` | 40 | Members per Tasks call; stories are packed or split into planning parts of at most this size |
| `max_attempts` | 2 | Calls per planner answer, the first included |

Only the options that shape a planner are part of its reuse key.
