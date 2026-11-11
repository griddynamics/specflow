---
type: DataModel
title: "Graph plan data model"
description: "Plan nodes and relationships, identity, statuses, fingerprints, constraints, per-store mapping and invariants"
status: stable
tags: [graph, plan]
---
# data-model.md: plan nodes and relationships, identity, statuses, fingerprints, meaning revisions and invariants

The plan lives in the project's partition of the shared database, next to the spec graph
([../data-model.md](../data-model.md) "Partition"). Plan nodes carry only their own label, never
`SpecNode`, so spec readers, search and build verification never see them.

## Plan

`:Plan`, one per project.

| Property | Meaning |
|---|---|
| `uuid`, `plan_id` | Storage key; external id `PLAN::MAIN` |
| `profile` | Project profile as JSON text: languages, frameworks, package layout, test command, test paths, `conventions` (a list of `{text, spec_ids}`: conventions and policies every task follows, with the requirements they cover) ([plan.md](plan.md) "Project") |
| `last_build_id` | The last build whose change report the plan consumed ("Fingerprints") |
| `consumed_build_ids` | The build ids of the consumed change-report chain; a node whose `last_build_id` is not in it changed after the plan (task briefs, `spec_changed`) |
| `graph_fingerprint` | Fingerprint of the spec graph the plan was last brought in line with; a guard only |
| `ontology_version`, `plan_ontology_version`, `rules_version` | Versions of the build ontology file, the plan ontology file and the plan rules used |
| `applying` | Run id while a persist is in progress, else absent ([plan.md](plan.md) "Persist") |
| `created_at`, `updated_at`, `last_run_id` | Write stamps |

## Work area

`:WorkArea`, one per part of the system that is built together ([plan.md](plan.md) "Partition").

| Property | Meaning |
|---|---|
| `uuid`, `area_id` | Storage key; external id `AREA::<SLUG>` |
| `kind` | `module` (an internal module and the nodes placed with it) or `project` (setup, closing, cross-cutting and unplaced work) |
| `anchor` | `spec_id` of the module the area is named after; none for the project area |
| `title`, `description` | Written by the area describer: what the area is responsible for and what it does not own |
| `interfaces` | JSON list of `{name, kind, spec_id, spec_ids, summary, planned_by}`: what the area offers other areas. Entries with `planned_by: rules` are the spec Interfaces the area provides (`name`, `kind` and `spec_id` copied from the Interface node); the describer writes each `summary`. Entries with `planned_by: model` are interfaces the text implies but names none: no `spec_id`, `kind` from the `interface_kind` vocabulary, `spec_ids` they rest on |
| `not_buildable` | JSON list of `{spec_id, reason, note}`: members the Tasks planner returned as not buildable; `reason` from the `not_buildable_reason` vocabulary, `note` at most 300 characters. Read by coverage (PD3) and briefs; also kept in the run result |
| `planned_by` | `model` or `rules` |
| `prompt_ref` | The describer prompt revision (`<name>.<revision>`) when written by a model |
| `status` | `active`, or `retired` when its anchor is gone or merged into another area's anchor; a retired area keeps its stories and tasks as history |
| `created_at`, `updated_at`, `last_run_id` | Write stamps |

## Story

`:Story`, a user-facing slice of one area: what a user picks work by ([plan.md](plan.md) "Partition").
Partition creates stories from the spec; no model invents them.

| Property | Meaning |
|---|---|
| `uuid`, `story_id` | Storage key; external id `STORY::<area slug>::<SLUG>` ("Identity") |
| `area_id` | Home area |
| `kind` | `feature`, `interface`, `entity` (anchored on a node of a `story` type), `core` (requirements the area's module owns directly), `chapter` (project-area requirements of one document chapter), `setup`, `closing` (project area only) |
| `anchor` | `spec_id` of the anchoring feature, interface or data entity; mirrors the story's `IMPLEMENTS`. None for the other kinds |
| `chapter` | For `chapter` stories: JSON `{document, heading}`, the source document and chapter heading |
| `title` | The anchor's name; for other kinds a fixed title (core, setup, closing) or the chapter heading |
| `summary` | Written by the area describer: what the story delivers |
| `prompt_ref` | The describer prompt revision (`<name>.<revision>`) when written by a model |
| `release` | The anchor's release (rules `release`, a null value taken from the anchor's owner); none for other kinds |
| `status` | `active` or `retired` ("Story status") |
| `claimed_by`, `claimed_at`, `agent_label` | The story claim ([lifecycle.md](lifecycle.md) "Story claims"): the holder's user id, when, and the label the agent gave (1-80 printable characters, display only). Set by `claim_story`, cleared when the claim ends. The story is held exactly while `claimed_by` is set |
| `claim_token` | Token of the last story claim; kept after the claim ends (release, completion, blocking finding) until the next claim, so a repeated call is recognised. Never returned by reads |
| `branch`, `note` | Last branch name (1-200 printable characters) and note (at most 500 characters) given by a claim or release; kept when the claim ends; `note` never logged |
| `pr_url` | Pull request URL given by the last `complete_story` |
| `created_at`, `updated_at`, `last_run_id` | Write stamps |

Plan runs never write the claim fields (`claimed_by` to `pr_url` above).

Every plannable node has one home story: the story of the feature, interface or data entity it is or
belongs to (nested features are stories of their own), else the area's `CORE` story, else, in the
project area, the story of its document chapter. Setup and closing tasks are in `SETUP` and `CLOSING`.
A task may implement nodes of several stories; it is `IN_STORY` the one the Tasks planner names.

## Task

`:Task`, one unit of work for one coding agent.

| Property | Meaning |
|---|---|
| `uuid`, `task_id` | Storage key; external id `TASK::<area slug>::<action>`, with `_R2`, `_R3` for later revisions ("Identity") |
| `area_id`, `action`, `revision` | Home area, the action segment of the id, revision number (1 first) |
| `story_id` | Home story; mirrors the task's `IN_STORY`, as `area_id` names its area |
| `phase` | `setup`, `contract`, `data`, `logic`, `integration`, `quality`, `acceptance` |
| `title`, `description` | What to build, which interface it serves, what is out of scope |
| `done_when` | JSON list of `{text, source}`: checkable completion statements; `source` `spec` for items taken from the acceptance criteria of an implemented node (rules `acceptance_criteria`), `planner` for items the planner wrote |
| `complexity` | `low`, `medium`, `high`; the host or agent may map it to a model, the plan never names models |
| `implements` | `spec_id`s of the spec nodes the task implements, as planned; mirrors the `IMPLEMENTS` relationships so a lost target is noticed |
| `release` | List of the distinct releases of the implemented nodes (rules `release`; a null value taken from the owner); empty when none. Shown, never used for order |
| `has_open_questions` | True when a node of a `context` type (an open question) is linked by the `reference` relation to an implemented node |
| `planned_by`, `prompt_ref` | `model` or `rules`; the planner prompt revision (`<name>.<revision>`) when planned by a model |
| `spec_build_id` | The plan's consumed build (`Plan.last_build_id` of the run) when the task's spec was last planned ("Fingerprints") |
| `status` | See "Task status" |
| `claim_token`, `claimed_by`, `claimed_at` | Set by a claim, cleared when the task leaves `in_progress`. A story claim sets the story's token and the holder's user id on every task it claims |
| `progress` | `done`, `skipped` or `todo`: what the holder of a story claim reported (`report_progress`); only while `in_progress`, cleared when the task leaves it |
| `attempts` | Number of claims so far |
| `commit` | Full git commit hash that finished the task (40 or 64 hex, lower case) |
| `completed_by`, `completed_at` | Who completed the task (the claimant, a user id for story claims) and when; kept after completion, also when the task is later superseded |
| `block_reason` | Block reason code while `blocked` ([lifecycle.md](lifecycle.md)) |
| `blocked_by` | `finding_id` of the blocking finding that blocked the task ([../gap.md](../gap.md) "Blocking link"); only while `blocked`, cleared with the block |
| `note` | Last short note from the agent (at most 500 characters); never logged |
| `created_at`, `updated_at`, `created_run_id`, `last_run_id` | Write stamps |
| `_lock` | Technical CAS token used and cleared inside a single lifecycle or persist statement; never returned by reads |

`not_buildable` is not a task property: a member the planner returns as not buildable gets no task; it
is recorded on its area's `not_buildable` list and in the run result (kept by `PlanStore`).

## Relationships

| Type | From → To | Properties | Meaning |
|---|---|---|---|
| `IN_AREA` | plannable spec node → `WorkArea` | `edge_key`, `rule` (partition rule), `via` (edge keys that caused it), `story` (the member's home story id) | Membership: every plannable node in exactly one area; `context` nodes have none |
| `IN_STORY` | `Task` → `Story` | `edge_key` | The task's home story |
| `IMPLEMENTS` | `Task` → spec node | `edge_key` | The task builds this spec node |
| `IMPLEMENTS` | `Story` → feature, interface or data entity | `edge_key` | The story's anchor |
| `WAITS_FOR` | `Task` → `Task` | `edge_key`, `source` (`planner` inside an area, `stitch` across areas or from setup and closing work) | The later task needs the earlier one `completed` first |
| `SUPERSEDES` | `Task` → `Task` | `edge_key` | The new revision replaces a task whose spec changed after it was finished |

The area of a story or task is its `area_id` property, not a relationship. An `edge_key` is
`<source id>|<RELATION>|<target id>` with `task_id`, `story_id`, `area_id` or `spec_id` at creation,
so a write is idempotent. Writes address spec nodes by their `uuid` (`GraphNode.uuid`), as
`OwnedWriter` requires.

When build merges two spec nodes, their plan relationships move to the survivor with their old keys;
when build deletes a spec node, they go with it ([../architecture.md](../architecture.md) W3, W4). Plan
runs therefore match plan relationships by their endpoints, never by key ([plan.md](plan.md) "Diff"),
and readers key on `spec_id` with aliases resolved. Closed tasks and retired stories keep their
relationships as history.

## Identity

| Value | Rule |
|---|---|
| `area_id` | `AREA::` + the anchor's `spec_id` without its prefix (`MOD::ORDERS_SERVICE` → `AREA::ORDERS_SERVICE`); the project area is `AREA::PROJECT`, assigned first; a value already held by another anchor gets `_2`, `_3`. Fixed at creation and kept for the anchor, so a renamed module keeps its area and a returning anchor gets its old area back. |
| `story_id` | `STORY::<area slug>::<SLUG>`. `SLUG`: for a feature, interface or data entity story the anchor's `spec_id` without its prefix; `CORE` for the requirements a module owns directly; `SETUP` and `CLOSING` in the project area only; for a chapter story (project area only) `CH_<slug of document title>_<slug of chapter heading>`, cut to at most 60 characters. A value already held by another anchor or chapter gets `_2`, `_3`. Fixed at creation and kept for the anchor or chapter; an anchor placed in another area gets a story there and its old story is retired. |
| `task_id` | `TASK::<area slug>::<action>`; `action` matches `^[A-Z][A-Z0-9_]{1,39}$`, never ends in `_R` and digits, and is unique in its area. The planner chooses actions and is asked to keep the actions of unchanged work. |
| Chapter slug | `heading_path[1]` of the member's first source fragment, else `heading_path[0]`; tuple, string or `None` are accepted and normalised to the same chapter value |
| Revision | A task that has a `commit` is never reset. When it must be redone, it becomes `superseded` and a new task `<task_id>_R<n>` (next free `n`) `SUPERSEDES` it. |
| Reuse of an id | An id belongs to one node. A task without a commit is updated in place under its id; a `retired` task without a commit comes back under its id when the same work returns. A `retired` story becomes `active` again under its id when its anchor or chapter returns. |
| `uuid` | `uuid5(NAMESPACE, "<project_id>\|plan\|<external id>")` with the core `NAMESPACE`. Never changes. |

## Task status

| Status | Meaning |
|---|---|
| `pending` | Planned; ready when every prerequisite is `completed` |
| `in_progress` | Claimed by an agent |
| `completed` | Done; `commit` is set |
| `failed` | The agent gave up; may be retried with `retry_task` |
| `blocked` | The agent reported that the spec does not let it proceed; waits for a spec change or an explicit unblock |
| `superseded` | Replaced by a newer revision; its commit is stale |
| `retired` | Its work left the plan (its spec nodes were removed or the planner dropped it); history only |

Open tasks are `pending`, `in_progress`, `failed` and `blocked`. `superseded` and `retired` tasks are
closed: never ready, never claimed. Transitions: [lifecycle.md](lifecycle.md).

## Story status and progress

| Status (stored) | Meaning |
|---|---|
| `active` | In the plan |
| `retired` | Its anchor left the spec graph, moved to another area, or (chapter, core) it has no members left; history only. Its tasks are retired or superseded with it |

Progress is derived from the story's tasks at read time and never stored; closed tasks are ignored.

| Progress | When |
|---|---|
| `completed` | Every task `completed`, and at least one |
| `blocked` | Any task `blocked` |
| `in_progress` | Any task `in_progress` or `completed` |
| `pending` | Otherwise (no open or completed task, or none started) |

The first matching row wins.

## Fingerprints

The build's change reports decide what changed; fingerprints only guard. The host passes the change
reports of every build since `Plan.last_build_id`, in build order ([api.md](api.md) `PlanContext.changes`).
Build ids are ordered by that chain: a build id is *newer* than build B when it is neither B nor a build
consumed up to B.

**Meaning revision.** For every plannable and context node, `PlanStore` keeps its meaning revision: the
build id of its last meaning change (classes `added`, `removed`, `merged`, `carried_over`, `rekeyed`,
`changed_meaning`). A node without a stored revision gets the build the run consumes; a `replan`
without reports sets the revision of every node whose `last_build_id` is newer to the build the run
consumes ([plan.md](plan.md) "Modes"). `changed_links` and `changed_evidence` do not change it; link changes reach the plan
through the edges in the reuse keys. A `spec_id` merged or rekeyed away resolves through the registry
aliases, and its survivor's revision is the build of the merge. The run's revisions are stored when the
run persists.

**Task spec.** A task's spec changed when the meaning revision of an implemented node is newer than its
`spec_build_id`, or an implemented node no longer resolves. This decides supersede and reset in Diff ([plan.md](plan.md) "Diff"). A `replan`
without reports supersedes a completed task only when an implemented node no longer resolves.

**Reuse keys** hold `spec_id`s with their meaning revisions and ids, never text or node fingerprints,
and never text a model wrote.

| Key | Covers |
|---|---|
| Describe | The area's member `spec_id`s with meaning revisions, the domain edges that touch a member (relation, other end's `spec_id` and area, basis), the area's story ids, the `spec_id`s of the interfaces it provides |
| Project | The `spec_id`s of the project planner's input nodes with meaning revisions, the deployment and external integration edges, area ids with their anchors and provided interface `spec_id`s |
| Tasks (one part) | The part's member `spec_id`s with meaning revisions and the domain edges that touch them (as for Describe), the part's story ids, the ids of the areas the area provides to, consumes from or integrates with, setup task ids, the `spec_id`s cited by profile conventions, `task_size` and `planner_members` |

Every key includes the project id; [plan.md](plan.md) "Reuse" says when each is used.

**Graph fingerprint** (sha256 hex over a canonical, sorted text): build ontology, plan ontology and
rules versions, the `task_size` and `planner_members` options, every plannable and context node's
`spec_id` with its `last_build_id`, and every domain edge that touches a plannable node (relation,
source and target `spec_id`, basis).
Used only as a guard: a run resumes only while it is unchanged, and the plan is `outdated` when it
differs from `Plan.graph_fingerprint` ([lifecycle.md](lifecycle.md)). It decides nothing about tasks.

Prompt revisions are not part of these keys: a new prompt applies to areas planned again for another
reason, or in a `replan` run ([plan.md](plan.md) "Modes").

**Dependency on the build.** The correctness of a plan update follows the build's change-report
classification. A node wrongly classed `changed_meaning` supersedes finished work that is not stale; a
real change wrongly classed `changed_evidence` leaves finished work in place. The result names, per
superseded task, the change class that caused it, so the host can check.

## Constraints and indexes

Names start with `graph_plan_`, except the `(group_id, uuid)` constraints, which are the core's foreign
label constraints (`graph_foreign_uuid_<label>`), and the relationship indexes, named like the core's
(`graph_edge_key_<relation>`). Every plan relationship carries `group_id`. The DDL ships with the module at
`graph/plan/ddl/plan_schema.cypher`; the core `ensure_schema` discovers it with the submodule DDL and
creates it by default, with the plan ontology loaded. `ensure_plan_schema` is the same call under the
plan's name ([api.md](api.md)).

| Object | On |
|---|---|
| Unique | `Plan (group_id, plan_id)` `graph_plan_id`, `WorkArea (group_id, area_id)` `graph_plan_area_id`, `Story (group_id, story_id)` `graph_plan_story_id`, `Task (group_id, task_id)` `graph_plan_task_id`; each label also `(group_id, uuid)`, `graph_foreign_uuid_<label in lower case>` |
| Range index | `Task (group_id, status)` `graph_plan_task_status`, `Task (group_id, story_id)` `graph_plan_task_story`, `Story (group_id, area_id)` `graph_plan_story_area` |
| Relationship index | `(group_id, edge_key)` on `IN_AREA`, `IN_STORY`, `IMPLEMENTS`, `WAITS_FOR`, `SUPERSEDES`, `graph_edge_key_<relation in lower case>` |

## Per-store mapping

Plan nodes are foreign nodes of the core model ([../data-model.md](../data-model.md) "Constraints and
indexes", "Per-store mapping"): they carry their plan label only, never `SpecNode`, and are keyed by
`uuid`, which is derived from the project id and the plan id (`plan_id`, `area_id`, `story_id`,
`task_id`), so the `uuid` key and the plan id key hold the same uniqueness. The plan queries are the same
templates on every store; property names, ids and the invariants below do not differ.

| Model | Neo4j | ArcadeDB 26.10 | PostgreSQL 15 + pgvector 0.8 |
|---|---|---|---|
| Plan node | Node with one plan label | Vertex of the type named by its plan label | Row of `graph.node` with `kind = 'foreign'`, `labels[1]` the plan label, the plan properties in `props` |
| Plan relationship | Typed relationship | Edge of a type per plan relation | Row of `graph.edge` with `rel_type` the plan relation |
| Keys | The constraints above | Unique indexes mapped from the same DDL | The core's partial unique index on foreign nodes `(group_id, labels[1], uuid)`; the plan ids are unique through their derived `uuid` |
| Range indexes | The indexes above | Indexes mapped from the same DDL | None of their own: plan reads filter `props` within one project's foreign rows |
| Queries | Canonical `.cypher` files of `graph/plan/queries/` | The same files, unchanged | One SQL port per query in `stores/postgres/queries/plan/` |
| Lifecycle lock | A property write on the task in the same statement | As Neo4j | `SELECT ... FOR UPDATE` on the task row in the same statement |

## Invariants

| # | Invariant |
|---|---|
| PD1 | Every plannable spec node has exactly one `IN_AREA` to an `active` area; a `context` or excluded node has none. |
| PD2 | Every open task has an `area_id` of an `active` area. |
| PD3 | Every node of a type the rules mark `required` is covered: implemented by at least one open or `completed` task, or recorded in its area's `not_buildable`, or named in the `spec_ids` of a profile convention. |
| PD4 | `WAITS_FOR` among open and `completed` tasks is acyclic; except from `in_progress` tasks, it never ends at a closed task. |
| PD5 | A `completed` task has a `commit`; an `in_progress` task has a claim; no other task has a claim. `progress` is set only on `in_progress` tasks, `blocked_by` only on `blocked` tasks, `completed_by` only on tasks with a `commit`. |
| PD6 | No plan node carries `SpecNode`; no plan write sets a property of a spec node, document or fragment. |
| PD7 | Every open task is `IN_STORY` exactly one `active` story of its own area, and its `story_id` names that story. Every `active` story of kind `feature`, `interface` or `entity` `IMPLEMENTS` exactly its anchor. |
| PD8 | A held story has `claim_token` and `claimed_at`; every task carrying a held story's token is an `in_progress` task of a story held with that token, and a task of a held story that is `in_progress` carries its token. |

Plan checks test PD1-PD5, PD7 and PD8 after every plan run and on request ([lifecycle.md](lifecycle.md)
"Plan checks").
