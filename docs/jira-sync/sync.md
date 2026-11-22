---
type: Design
title: "Jira sync rules"
description: "How plan items map to Jira issues: triggers, owned fields, labels, story points, description layout, comment format, Blocks links, rank, remote link, status rules, removed items, a target change during a pass, the pass order, failure handling and the Jira calls"
status: stable
tags: [jira]
---
# sync.md: triggers, mapping, owned fields, ADF layout, comments, links, rank, status rules, pass order, failures, Jira calls

Values defined here are the only definition; [requirements.md](requirements.md) links to them. Decisions are numbered as in
[questions.md](questions.md#decisions).

## Triggers and runs

| Trigger | When | Condition |
|---|---|---|
| `plan_run` | a plan run returns with status `succeeded` or `partially_succeeded` (an unchanged run too) | the project's target exists and is enabled |
| `mcp` | `sync_to_jira` | target exists and is enabled, else `409 jira_not_configured` |
| `settings` | a `PUT` of the settings leaves the target enabled and either turned it on or changed `jira_project` or `epic_key` | - |
| `recovery` | API process start | the stored last status is `running` (the process stopped mid-run) and the target is enabled |

A start on a project with no target or a disabled one queues nothing and answers `skipped` with `jira_not_configured`
(the `mcp` route answers 409 from it; the other triggers drop it).

A failed plan run starts nothing. Starting is never an error for the plan run: a failure to start is logged and dropped.

Runs (D12, D18):
- One sync pass per project at a time. A start while the project's pass runs sets a flag and answers `running` with
  `queued: true`; when the pass ends, exactly one more pass runs. Starts are merged, never dropped.
- One pass at a time in the process (all projects share one Atlassian account); other projects wait.
- A pass records `running`, the trigger and the time it was queued as soon as it is queued, before it waits for the
  process-wide slot, so a restart recovers a waiting pass too. At its end it records the status, counts, error code and
  finish time.
- While a pass of the project is queued or runs in this process, the status reads `running`, whatever the stored row
  says. A stored `running` with no live pass reads as `failed` with `interrupted` until recovery restarts it; when the
  target is disabled it reads `skipped` with `jira_not_configured` (recovery never starts a disabled target).
- A pass that finds the target disabled or gone ends `skipped` with `jira_not_configured`; a project without a plan ends
  `skipped` with `no_plan`; a plan being written ends `skipped` with `plan_applying` (the plan run's own trigger follows).
- Startup recovery runs in the background; a failure of it is logged (`api.jira.sync.start_failed`) and never stops the
  API.

| Status | Meaning |
|---|---|
| `idle` | the target never ran |
| `running` | a pass is queued or runs (`queued` tells whether another follows) |
| `succeeded` | every item applied or unchanged |
| `partial` | the pass ended, one or more items failed ([Failure handling](#failure-handling)) |
| `failed` | a run error stopped the pass |
| `skipped` | nothing was read from or written to Jira, for the code's reason; with `target_changed` the pass stopped early ([Target change during a pass](#target-change-during-a-pass)) |

Counts of a pass: `created`, `updated` (To Do issues overwritten, a link-only change included), `commented` (every
comment posted, removal and revival included), `removed` (newly marked removed, issues deleted in Jira included),
`links_added`, `links_removed`, `ranked` (issues moved), `unchanged` (wanted items with no create, change or revival),
`failed` (items).

### Target change during a pass

A settings `PUT` that changes `jira_project` or `epic_key` never waits for a running pass. Instead:

- The `PUT` locks the target row for update, then (on a `jira_project` change) deletes the project's map rows and
  changes the target, all in one transaction.
- Every map-row write of a pass runs in its own short transaction that first locks the target row for share and checks
  that `jira_project` and `epic_key` still equal the values the pass read in step 1 of the [pass order](#pass-order).
  On a mismatch the pass writes nothing more, makes no further Jira call and ends `skipped` with `target_changed`.
- So a map row is either written before the change (and deleted with the old map) or not at all. Only the Jira calls of
  the one item in flight can still reach the old Jira project; that issue stays there untouched, like the other old
  issues.
- The `PUT`'s own `settings` start ([Triggers and runs](#triggers-and-runs)) merges into the stopped pass, so exactly
  one more pass runs with the new target when the target stays enabled.

## Mapping

| Plan item | Jira issue | Map node id | Parent | Created when |
|---|---|---|---|---|
| the project | **Epic**: the configured `epic_key`, or one Specflow creates (summary = project name) | `EPIC` (created only) | - | `epic_key` is blank and no `EPIC` row has a key |
| story with status `active` | **Story** | story id (`STORY::<area>::<slug>`) | the Epic | first seen |
| live task (D17) | **Sub-task** (the project's sub-task issue type) | base task id (`_R<n>` stripped) | its story's Story | first seen |
| work area | label `area-<slug>` on its stories and sub-tasks (D3) | - | - | - |
| WAITS_FOR | `Blocks` links ([Links](#links)) | - | - | - |

Live task: the representative (the highest revision of a base among the plan's tasks) has a status outside `superseded`
and `retired`; `completed` tasks stay live. A live task whose story is not an active story of the plan (missing or
`retired`) is not wanted: its Sub-task is handled as removed. The plan is read with the plan library's single read of
the current plan, which keeps closed tasks.

Issue types come from the target project's issue type list: `Epic` and `Story` by name, the sub-task type as the
first type flagged as a sub-task. The [access check](api.md#access-check) refuses a project that lacks one.

Create fields: `project`, `issuetype`, `summary`, `parent` (Stories and Sub-tasks) and the other owned fields. No Epic
Name or other extra field is sent: on Jira Cloud an Epic, like a Story, requires only project, issue type and summary,
and a Sub-task also its parent. A project whose create screen requires more answers 400, and the item ends
`jira_invalid_field` ([Failure handling](#failure-handling)).

A configured `epic_key` belongs to the owner: Specflow only parents Stories to it and never edits it. An Epic Specflow
created is owned like any other issue (summary and labels). When `epic_key` is set later, the created Epic is no longer
wanted and is handled as a removed item; Stories move to the configured Epic through their `parent` field. When
`epic_key` is cleared again, the created Epic is revived ([Removed and revived items](#removed-and-revived-items)) and
the Stories move back to it.

## Owned fields

Specflow writes only these fields. Status, assignee, reporter, priority, sprint, comments it did not post and links it
did not create are never touched; no issue is ever deleted or moved through a workflow transition.

| Field | Epic (created) | Story | Sub-task |
|---|---|---|---|
| Summary | project name | story title | task title |
| Description (ADF) | - | [layout](#description-layout) | [layout](#description-layout) |
| Story points | - | sum of its live sub-tasks' points (none when 0; a sub-task deleted in Jira still counts) | `low` 1, `medium` 3, `high` 5 |
| Labels (owned ones only) | `specflow` | `specflow`, `area-<slug>` | `specflow`, `area-<slug>` |
| Parent | - | the Epic's key | its Story's key |
| `Blocks` links | - | derived story links | task links |
| Rank | - | among the Epic's Stories | among the Story's Sub-tasks |
| Remote link | on create | on create | on create |

Field limits (Jira refuses the field otherwise; the stored content holds the values as sent):
- Summary: every run of whitespace, line breaks included, becomes one space, with no leading or trailing space; a
  summary longer than 255 characters is cut to 254 and ends with `…`.
- Description: the text part (row 1 of the [layout](#description-layout)) is cut to 30,000 characters ending with `…`,
  so the description stays under Jira's 32,767-character limit.
- Area label: a slug longer than 200 characters is cut to 200 (and trailing `-` removed); an empty slug gives no area
  label.

Labels: `area-<slug>` takes the area id without `AREA::`, lower case, every run of characters outside `a-z0-9`
replaced by one `-`, no leading or trailing `-`. Labels are changed with add and remove operations on the owned labels
(`specflow`, `area-*`, `specflow-removed`) only; labels people add stay.

Story points: the custom field named exactly `Story Points` in the site's field list, found once per process. When the
site has no such field, points are not sent (they stay in the stored content).

## Description layout

Atlassian Document Format, version 1. Blocks in this order; an empty part is left out.

| # | Story | Sub-task |
|---|---|---|
| 1 | paragraphs of the story summary (split on blank lines) | paragraphs of the task description |
| 2 | paragraph `Release: <release>` | heading (level 3) `Done when` and a task list, one `TODO` item per `done_when` text |
| 3 | heading `Implements` and a bullet list `<spec name> (<spec id>)`, from the story's IMPLEMENTS links | paragraph `Phase: <phase>` |
| 4 | paragraph `Specflow: <story id>` (the id in code style) | heading `Implements` and a bullet list from the task's `implements`, aliases resolved to the current spec node |
| 5 | - | paragraph `Specflow: <task id>` (the representative's id, so a redo shows) |

A spec id with no node keeps the id as its name.

## Update rules

For each mapped issue whose stored content differs from the new content (or has none stored), Specflow reads the
issue's status category (D5, D25):

| Status category | Fields | Links | Comment |
|---|---|---|---|
| `new` (To Do, Backlog) | overwrite the owned fields that differ | add wanted, delete unwanted links Specflow created | none, unless removed or revived |
| any other | unchanged | unchanged | one comment listing every change ([Comment format](#comment-format)) |

Then the new content is stored. Content that differs only in layout (the same field values, for example a new
`MAPPING_VERSION`) posts no comment and only refreshes the stored content; in To Do it rewrites the description.

The description is sent only when one of its inputs differs (text, release, phase, done-when items, implemented specs,
task id, or the mapping version) or no content was stored; a change of only links, points or labels sends no
description, and a change of links alone sends no field edit at all.

Unchanged content means no Jira call for that item. A pass where nothing changed makes no Jira call at all, so a
re-run never adds a duplicate comment.

## Comment format

One ADF comment per issue and pass, in English:

1. Paragraph: `Specflow plan update for <node id>. This issue is in status "<status name>", so Specflow left its fields as they are.`
   For a removal or revival the paragraph says `This item was removed from the Specflow plan.` or
   `This item is back in the Specflow plan.` instead of the second sentence.
2. Bullet list, one item per change, in this order (an unchanged field is left out):

| Change | Item text |
|---|---|
| summary | `Summary: "<old>" → "<new>"` |
| description text | `Description changed. New text:` and the new text as a quote (at most 2000 characters, cut with `…`; `none` when the new text is empty) |
| done-when items | `Done when: added "<text>"` / `Done when: removed "<text>"` |
| release, phase | `Release: <old> → <new>`, `Phase: <old> → <new>` (`none` for an empty value) |
| implemented specs | `Implements: added <name> (<id>)` / `Implements: removed <name> (<id>)` |
| task id | `Task id: <old> → <new>` (a redone task) |
| story points | `Story points: <old> → <new>` |
| labels | `Label: added <label>` / `Label: removed <label>` |
| parent | `Parent: <old key> → <new key>` |
| blocked by | `Blocked by: added <key>` / `Blocked by: removed <key>` (the node id when the key is not known) |

The list is computed from the stored content and the new content, never from Jira's current fields (people may have
edited them). Comments are at-least-once: a crash between posting and storing repeats that one comment on the next
pass.

## Links

The waiter owns its links: a WAITS_FOR edge `(waiter, prerequisite)` becomes "prerequisite **blocks** waiter", stored in
the waiter's content as `blocked_by` (node ids).

- Task level: for each live task, its waits mapped to base ids, kept when that base is live, same-base pairs dropped.
- Story level: for each task edge whose two tasks are in different stories, `(story of waiter, story of prerequisite)`;
  deduplicated, no self links, cycles kept. Built from all live tasks, completed ones included, so links do not change as
  work finishes.
- A To Do waiter: missing links are added; links Specflow created (in the stored `blocked_by`) and no longer wanted are
  deleted by their link id, found in the waiter's links. Links of other types, and links that were never in the stored
`blocked_by`, are never deleted.
- A `Blocks` link a person made before Specflow wanted the same wait is stored in `blocked_by` like one Specflow
  created, and is deleted when the plan drops the wait. A link already in Jira is not added twice.
- Any other waiter: link changes are listed in its comment only.
- New issues get their links after every issue of the pass exists: a new issue is first stored with an empty
  `blocked_by` and its full content is stored after its links, so a crash in between leaves a difference the next pass
  closes. A link add that answers 404 reads both issues: a missing one is deleted in Jira; if both exist the item ends
  `jira_unexpected`.

## Rank

Scopes: the Stories under the Epic, and the Sub-tasks under each Story. Order = prerequisites first (topological order
of the scope's links, lowest node id first among the ready ones; members of a cycle appended sorted by id). Each map row
stores its position in its scope (D20).

- Previous order of a scope = its rows by stored position, then the issues created in this pass in creation order.
- Items on the longest increasing subsequence of the previous order (as positions in the new order) stay; every other
  item is moved. Moved items form maximal runs in the new order; runs are processed right to left, each with one
  rank call of at most 50 issues placed before the next item, or after the previous item for a run at the end.
- Only issues in To Do are moved. The moves are planned before the status read of the [pass order](#pass-order) (step
  4), so the status of every issue a move would touch is read in that same batched search. After that read the scope is
  planned again from the previous and the new order with the issues outside To Do removed from both; only these moves
  are sent. An issue not in To Do keeps its place and gets no comment for rank, and the To Do issues end in plan order
  among themselves.
- The stored position is the index in the order Jira holds after the pass (the previous order with the sent moves
  applied), not in the new order: an issue that stayed out of place because it is past To Do keeps its real place. While
  it stays past To Do the scope still differs from the plan, so each pass reads its status again (no rank call); once it
  is back in To Do, a later pass moves it.
- A rank call that Jira refuses with 400 or 404 marks the first issue of the call (`jira_invalid_field`, or
  `jira_unexpected` for 404), keeps the scope's stored positions so the next pass retries, and the pass ends `partial`.
  A pass that found an issue deleted in Jira makes no rank call.
- An unchanged order makes no rank call; issues created in plan order make none either.

## Remote link

When the api reads `PUBLIC_URL`, each issue Specflow creates gets one remote link, upserted by its global id
`specflow:<project id>:<node id>`, title `Specflow plan`, URL `<PUBLIC_URL>/projects/<project id>/plan`. The api reads
`PUBLIC_URL` only when `MCP_RESOURCE_URL` is set, the same rule as the web links of the gap cards
([../configuration.md](../configuration.md)). Otherwise no remote link is made.

## Removed and revived items

A mapped node is removed when its representative is missing or closed, or its story is not an active story (base
task), the story is missing or `retired` (story), or a created Epic is no longer wanted. A superseded revision with a
live successor is not removed (D17).

| Event | To Do | Any other status |
|---|---|---|
| removed (first pass that sees it) | add label `specflow-removed`, post the removal comment | post the removal comment |
| revived (removed, now live again) | remove `specflow-removed`, overwrite the fields, post the revival comment | post the revival comment with the change list |

Removed issues are never deleted, closed or transitioned (D6). A removed item is not ranked and its links stay.

Deleted in Jira (D32): a mapped Story or Sub-task that Jira no longer finds (404, or a 404 in the one-by-one status read
of [Pass order](#pass-order) step 8) was deleted by a person. Its row gets `removed_at` and `error_code`
`jira_not_found`, it counts as `removed`, the pass does not become `partial`, and Specflow makes no further call for
that node until a `jira_project` change clears the map. A create of a Sub-task that answers 404 means its Story was deleted: the Story is marked so, and its new Sub-tasks are
not created. A created Epic whose row is marked deleted while no Epic key is configured ends the pass `failed` with
`jira_not_found` before any call. A
created Epic that Jira no longer finds is a run error (`jira_not_found`): the owner sets an Epic key.

## Pass order

1. (`running` was recorded when the pass was queued.) Read the target, the project name and the map rows in one short
   transaction; keep `jira_project` and `epic_key` for the [target check](#target-change-during-a-pass).
2. Read the plan (one read); `no_plan` or `plan_applying` ends the pass `skipped`.
3. Read the names of the implemented spec nodes (one read for all ids).
4. Build the desired issues, contents, links and orders; diff them with the map rows; plan the rank moves of the
   mapped issues ([Rank](#rank)).
5. Nothing to do: record `succeeded` with `unchanged` = the number of desired items. No Jira call.
6. Recover pending rows ([Failure handling](#failure-handling)).
7. Create missing issues, parents first: the Epic, then Stories, then Sub-tasks; each through a pending row. Before
   the first Story of a pass is created, and before a To Do Story gets a new parent, the Epic is read once; a missing
   Epic ends the pass `failed` with `jira_not_found` (not needed for an Epic created in the same pass).
8. Read the status and links of every changed, removed or revived mapped issue and of every issue a planned rank move
   touches, in batched searches (100 keys a page). If Jira refuses a page with 400 (it does when a key in the query no
   longer exists), read that page's issues one by one; a 404 there means the issue was deleted in Jira
   ([Removed and revived items](#removed-and-revived-items)). Apply the [update rules](#update-rules) per issue; store
   the content after each issue.
9. Add and delete links of To Do waiters.
10. Rank moves of To Do issues; store positions.
11. Record the status, counts, error code and finish time.

No database transaction is open while a Jira call runs; each item's write is its own short transaction, which first
checks the target ([Target change during a pass](#target-change-during-a-pass)).

## Failure handling

Creates (D23):
1. Commit a pending row: node id, kind, no key, `requested_at` now.
2. Create the issue with the entity property `specflow` = `{project_id, node_id}` set in the same request.
3. Commit the key; upsert the remote link; commit the content and position. A row with a key and no content is
   re-applied in full on the next pass.
4. A pass that finds pending rows first searches the target's Jira project for issues labelled `specflow` created since
   the oldest pending `requested_at` minus 5 minutes (relative minutes in JQL), with the `specflow` property. An issue
   is adopted only when its property's `project_id` is this Specflow project's id and its `node_id` is a pending row's
   node id; issues of other Specflow projects syncing to the same Jira project are ignored. No match creates again.
   Jira's search lists a new issue only some seconds after the create, so the search waits until the youngest pending
   `requested_at` is 60 seconds old (no database transaction open); a duplicate is worse than a delay.
   A pending row whose last create Jira refused with 400 (`jira_invalid_field`, no key) is not searched for and does not
   make the pass wait: a 400 proves Jira created nothing. The next create of that row clears the error.

| Error | Scope | Effect |
|---|---|---|
| `400` on one issue | item | row gets `jira_invalid_field`, its content is not stored (retried next pass); the pass goes on; status `partial` |
| `404` on one mapped Story or Sub-task | item | deleted in Jira ([Removed and revived items](#removed-and-revived-items)): row removed with `jira_not_found`, no further call |
| `401` | run | stop, `failed`, `jira_auth_failed` |
| `403` | run | stop, `failed`, `jira_forbidden` |
| `404` on the project, the configured Epic or the created Epic | run | stop, `failed`, `jira_not_found` |
| `429` | call | wait `Retry-After`, at most 5 tries; a `Retry-After` over 60 s, or a sixth `429`, ends the run at once with `jira_unreachable` |
| `5xx`, timeout (30 s), transport error | call | 3 retries (4 requests) with back-off 1, 2, 4 s, then run error `jira_unreachable`; a create, a comment or a link add is never sent again (Jira may have done it): run error `jira_unreachable` at once, and the next pass recovers it |
| any other Jira status | run | stop, `failed`, `jira_unexpected` |
| a bug in Specflow | run | stop, `failed`, `unexpected_error`; logged with the class name only |

A successful item clears its row's error code. A pending row without a key whose node is no longer wanted is ignored
and never searched for.

## Jira calls

All calls use basic authentication with the deployment's email and API token over HTTPS, JSON bodies, REST v3 unless
marked agile.

| Purpose | Call |
|---|---|
| Who am I (access check) | `GET /rest/api/3/myself` |
| Project and issue types | `GET /rest/api/3/project/{key}` |
| Permissions (access check) | `GET /rest/api/3/mypermissions?projectKey={key}&permissions=BROWSE_PROJECTS,CREATE_ISSUES,EDIT_ISSUES,ADD_COMMENTS,LINK_ISSUES,SCHEDULE_ISSUES` |
| Configured Epic (access check) | `GET /rest/api/3/issue/{key}?fields=summary,issuetype,project` |
| Story points field | `GET /rest/api/3/field` |
| Create | `POST /rest/api/3/issue` with `fields` and `properties` |
| Overwrite fields, label operations, parent | `PUT /rest/api/3/issue/{key}` with `fields` and `update.labels` |
| Status and links | `POST /rest/api/3/search/jql` (`key in (...)`, `fields: status, issuelinks`) |
| Epic read (once per pass, before a Story is created or a To Do Story gets another parent; none for an Epic made in the pass) | `POST /rest/api/3/search/jql` (`key in (<epic>)`, `fields: status, issuelinks`), the same call as Status and links |
| Status and links of one issue (a refused search page) | `GET /rest/api/3/issue/{key}?fields=status,issuelinks` |
| Pending recovery | `POST /rest/api/3/search/jql` (`project = P AND labels = specflow AND created >= -<n>m`, `properties: specflow`) |
| Comment | `POST /rest/api/3/issue/{key}/comment` |
| Add link | `POST /rest/api/3/issueLink` (type `Blocks`) |
| Delete a Specflow link | `DELETE /rest/api/3/issueLink/{id}` |
| Rank | `PUT /rest/agile/1.0/issue/rank` (agile) |
| Remote link | `POST /rest/api/3/issue/{key}/remotelink` with `globalId` |

No other method or path is used: no issue `DELETE`, no `/transitions`.
