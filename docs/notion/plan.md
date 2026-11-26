---
type: Design
title: "Notion as a plan destination"
description: "A project's plan exported to a Notion database: target, schema, mapping, rules of a pass, row cap, runs, routes, codes, limits"
status: draft
tags: [notion, plan, mission-control]
---
# plan.md: the Notion plan destination

A project can send its plan to a Notion database as rows: one per story and one per live task. It mirrors the GitHub
Projects destination ([../github/projects.md](../github/projects.md)): the same ownership rules, the same "never delete", the
same one pass per project, on the lifted export code (`api.exports`: runner, digest, run status) and the same desired set
(`api.jira.desired.build`, a pure function). It changes neither the plan runner nor any other destination's table.
Decisions: N7 to N11.

## Scope

One database per project, created by Graphico (N11) under a **parent page** the member names. The export acts as the member
the project's Notion is connected by (`PUT /projects/{pid}/integrations/notion`, rule R1), read from `project_integrations`
at the start of every pass: a removed member, a removed integration or a take-over takes effect on the next pass (no binding
is `notion_not_connected`, an expired connection `notion_reconnect_required`). The parent page must be shared with the
connection (ticked in the page picker). Anyone on the project may save the destination (D6).

## Target

One row per project in `notion_plan_targets` (migration `0011_notion`): the parent page (`parent_page_id`,
`parent_page_title`, `parent_page_url`), the database once the first pass made it (`database_id`, `data_source_id`,
`database_url`; null before), `enabled`, the run columns (`last_*`, as `github_project_targets`). Saving a different parent
page clears the database columns and deletes the map first; the next pass makes a new database under the new page (the old
database stays on Notion).

## Database schema (N7)

The first pass creates the database "Graphico plan · <project name>" with one data source and these properties:

| Property | Type | From |
|---|---|---|
| `Name` | title | story or task title |
| `Type` | select: Story, Task | node kind |
| `Status` | status | Notion's default groups (To-do, In progress, Complete); a new row starts in the first To-do option |
| `Points` | number | story points |
| `Work area` | multi-select | work areas of the node |
| `Graphico ID` | rich text | the plan node id; the key of idempotence |
| `Parent` / `Children` | relation to the same data source, two-way | a task's story |
| `Blocked by` / `Blocks` | relation to the same data source, two-way | dependencies between nodes |
| `Removed` | checkbox | the node left the plan |

The two-way self-relations need the data source's own id, so they are added right after the create
(`update_data_source`). The task brief (description, done-when, release, phase, implements) is the row's body, as Markdown.
The order by `api.exports.rank` is not exported: Notion views sort themselves.

## Map

`notion_plan_items` has one row per plan node: `kind` (`story` or `task`), the row's `page_id` (null before the create), the
applied `content` and its `content_hash`, `removed_at`, `error_code`, `synced_at`. A row without a `page_id` is a create
about to be sent.

## Rules of a pass (N8, N9)

`push_once` (`api.notion_plan.sync`): skipped with `no_plan` or `plan_applying`, `notion_plan_not_configured` without an
enabled target, `target_changed` when the destination changed during the pass. Then, in order:

1. **Database**: when the target has none, create it and its relations and store the ids.
2. **Create** each missing story and task (stories first, then their tasks, so `Parent` can be set): the row with its
   properties and body. Before a create the pass asks the data source for a row with that `Graphico ID`, so a crash between
   a create and its map row does not make a duplicate.
3. **Update** a changed item (a different `content_hash`): read the row's `Status`. A row whose Status is in the `to_do`
   group takes the plan's properties and body (`replace_markdown`). Any other row gets **one comment** listing the changed
   fields and is otherwise left alone; the stored content moves on, so the comment is not repeated.
4. **Remove**: an item that leaves the plan gets `Removed` checked and a comment, and `removed_at`; it is never deleted or
   archived. One that returns loses the check and gets a comment. A row a person deleted or archived in Notion is
   `notion_page_gone` and is never made again.

A row Notion refuses (400) fails that item only (`notion_item_invalid`, status `partial`). Every other refusal ends the pass
`failed` with one closed code and the next pass continues from the rows.

### Row cap (N9)

Each runner holds one pass slot for the whole process (`ExportRunner._slots`, `MAX_CONCURRENT_PASSES = 1`), so a long Notion
pass would hold back the Notion exports of every other project. A pass therefore **writes at most 120 rows**
(`MAX_ROWS_PER_PASS`). When rows are left (`counts.deferred`), the pass calls `start(project_id, "plan_run")` on its runner
before it ends: the start merges (sets `dirty`) and `ExportRunner._drain` runs one more pass after the slot was released.
There is no new trigger value (`NOTION_PLAN_TRIGGERS = plan_run, settings, recovery`) and `api/exports/runner.py` does not
change. With 180 requests a minute a 300-item first export takes about three passes.

## Runs

The same as the Jira sync ([../jira-sync/sync.md](../jira-sync/sync.md#triggers-and-runs)): triggers `plan_run` (the plan
fan-out calls `after_plan`), `settings` (saving a destination that was turned on or changed) and `recovery` (a start finds a
stored `running`); starts are merged, one pass runs per process, `running` is stored before the slot. Logs
`api.notion_plan.sync.*` carry ids, codes and counts only.

## Routes

| Route | Meaning |
|---|---|
| `GET /projects/{pid}/notion-plan` | the destination: `available`, the parent page, `database_url`, `enabled`, `sync` (status, counts), who it acts as |
| `PUT /projects/{pid}/notion-plan` `{link, enabled}` | resolve the parent page as the bound member, save the target, tell the runner after the commit; never waits for a pass |
| `POST /projects/{pid}/notion-plan/access-check` `{link}` | read the parent page as the bound member: `{result: ok, parent_page_title}` or `{result: failed, reason}` (a closed `notion_*` reason, including `notion_not_connected` and `notion_reconnect_required`); a database link is `notion_parent_not_a_page` |

`503 notion_unavailable` when the integration is off. No Notion call is made inside a transaction.

## In the canvas

The plus of the Plan stage lists **Notion** next to Jira and GitHub Projects; it needs the project's Notion integration, else
the card says "Set up first". The form asks for a **parent page link**. **Check access** runs as the member Notion is
connected by (an unbound project or an expired connection comes back as `{result: failed, reason: notion_not_connected |
notion_reconnect_required}`, not as an error; the save answers them with 409); **Add destination** saves it turned on and the pass runs in the background. The node is named
`Notion · <parent page title>`, waits for the plan, counts the rows made (`sync.counts.created`), and while `sync.status` is `running`
the canvas reads `GET` every 3 seconds. It stops only for what the API reports: `notion_not_connected`,
`notion_reconnect_required` or `notion_permissions_missing` offer Reconnect Notion; any other closed code says what it is.

## Limits

- A pass writes at most 120 rows (above); the request budget is shared with imports ([oauth.md](oauth.md#request-budget)).
- A pass waits at most 60 s in all for the request limit; a wait that does not fit ends it `notion_rate_limited`.
- Properties a person adds to the database are not touched; a person who renames or deletes a Graphico property makes the
  next write of it fail with `notion_item_invalid` for that row.
- A crash between a create and its map row is repaired by the `Graphico ID` query; an old row a person copied keeps its
  `Graphico ID` and may be found instead of the original (the first match wins).

## Not verified live

The create of the database with two-way self-relations and the `Status` groups were tried in the sandbox ("Specflow Plan
(trial)" in [discovery.md](discovery.md)). A full pass of 300 rows at the real limit, and the comment path on a row moved to
"In progress", are left to the owner's live check.
