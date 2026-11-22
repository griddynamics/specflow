---
type: Requirements
title: "Jira sync requirements"
description: "Requirement units of the Jira sync (compact form): settings, runs, mapping, update rules, reliability, MCP tools, Settings UI, non-functional"
status: stable
tags: [jira]
---
# requirements.md: requirement units of the Jira sync (compact form), functional and non-functional

Each row is one testable requirement in EARS form with one acceptance criterion. Status of every unit: implemented
(2026-11-22); the live Jira parts of FR-JIRA-MAP-06 and MAP-07 were verified against a real Jira site on 2026-11-22,
and only the live Postgres parts (the live repository test of FR-JIRA-RUN-08 and the NFR-JIRA-05 live database tier,
which need Docker) have not run.
Verification is by offline test with a fake Jira
unless the row says otherwise; "live" rows are checked against a real Jira site in a dedicated test Epic. The
rows state behaviour and link the section that defines it; values live only there. Decisions are in
[questions.md](questions.md).

## Settings ([api.md](api.md#web-routes))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-JIRA-CFG-01 | Every member shall read the project's Jira settings with `available` and `editable` (D14). | Given a collaborator, `GET /projects/{pid}/jira` is 200 with `editable` false; the owner gets `editable` true. |
| FR-JIRA-CFG-02 | When a collaborator saves settings or runs the access check, the API shall answer 403 `owner_required` and change nothing (D9). | Given a collaborator, `PUT` is 403 and the target row is unchanged. |
| FR-JIRA-CFG-03 | If a key breaks the format rules, then the API shall answer 422 `validation_failed` (D9). | `jira_project` `abc-1`, or `epic_key` `XYZ-1` with project `ABC`, is 422; ` abc ` saves as `ABC`. |
| FR-JIRA-CFG-04 | When the owner changes `jira_project`, the API shall delete the project's map rows in the same transaction (D21). | Given 3 map rows, a `PUT` with another project leaves 0 rows; a `PUT` that changes only `enabled` keeps 3. |
| FR-JIRA-CFG-05 | The access check shall answer `ok` or the first failing step with its reason and field ([api.md](api.md#access-check), D29). | With the fake: bad token `jira_auth_failed`/null; unknown project `jira_not_found`/`jira_project`; no sub-task type `jira_issue_types_missing`; missing link permission `jira_forbidden`/`jira_project`; an Epic read refused with 403 `jira_forbidden`/`epic_key`; a Story key as Epic `jira_epic_invalid`/`epic_key`; valid input `ok` with the names. |
| FR-JIRA-CFG-06 | When a `PUT` turns the target on or changes its keys while it stays on, the API shall start a sync with trigger `settings`. | Given a saved disabled target, `PUT enabled=true` starts one pass with trigger `settings`; a second identical `PUT` starts none. |
| FR-JIRA-CFG-07 | While the `ATLASSIAN_*` settings are unset, the API shall answer `GET` with `available` false and `sync` built from the stored target, and every other Jira route with 503 `jira_unavailable` (D16). | Given no `ATLASSIAN_*`, `GET` is 200 `available: false`; with a stored target whose row says `running`, `sync.status` is `failed` with `interrupted`; `PUT`, access check and both agent routes are 503. |
| FR-JIRA-CFG-08 | If only some of the three `ATLASSIAN_*` settings are set, then the API shall refuse to start and name the missing ones. | `load_settings("api")` with only `ATLASSIAN_SITE_URL` raises a configuration error naming the other two. |

## Runs ([sync.md](sync.md#triggers-and-runs))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-JIRA-RUN-01 | When a plan run ends `succeeded` or `partially_succeeded` and the target is enabled, Specflow shall start a sync with trigger `plan_run` (D30). | A finished plan run (unchanged included) starts one pass; a `failed` run starts none. |
| FR-JIRA-RUN-02 | While the target is missing or disabled, no trigger shall reach Jira (D15). | Plan run: no pass; `sync_to_jira`: 409 `jira_not_configured`; the runner's start answers `skipped`/`jira_not_configured` and queues nothing (D36); the fake records no call. |
| FR-JIRA-RUN-03 | When a start arrives while the project's pass runs, Specflow shall answer `queued` true and run exactly one more pass after it (D18). | Three starts during one pass give exactly two passes in total; the second reads the plan after the third start. |
| FR-JIRA-RUN-04 | Specflow shall run at most one pass at a time in the process (D18). | Two projects started together: the second pass starts after the first ends. |
| FR-JIRA-RUN-05 | When the API starts and an enabled target's last status is `running`, Specflow shall start a pass with trigger `recovery`; until then the status reads `failed` with `interrupted` (a disabled target: `skipped` with `jira_not_configured`). A queued pass shall be stored `running` before it waits for the process slot ([sync.md](sync.md#triggers-and-runs)). | A stored `running` row reads `failed`/`interrupted`, or `skipped`/`jira_not_configured` when disabled; a pass waiting behind another project's pass is already stored `running`; after `start_recovery` one pass with trigger `recovery` runs. |
| FR-JIRA-RUN-06 | If the project has no plan or its plan is being written, then the pass shall end `skipped` with `no_plan` or `plan_applying` and make no Jira call. | Both cases: status `skipped`, the code, zero fake calls. |
| FR-JIRA-RUN-07 | A failure to start or run a sync shall never fail or delay the plan run. | A runner whose start raises: the plan run's status is unchanged and `api.jira.sync.start_failed` is logged. |
| FR-JIRA-RUN-08 | When the owner changes `jira_project` or `epic_key` while a pass runs, the pass shall stop before its next map-row write and end `skipped` with `target_changed`, and one more pass shall run with the new target (D31). | The fake gateway holds a create while a `PUT` to another Jira project commits: the pass ends `skipped`/`target_changed`, the next pass maps only issues of the new project, no row of the old one is left; live repository test: the target lock orders a map-row write and the `PUT` both ways. |
| FR-JIRA-RUN-09 | A failure of startup recovery shall never stop the API. | A database error in recovery: `build_runtime` returns, `api.jira.sync.start_failed` with trigger `recovery` is logged. |

## Mapping ([sync.md](sync.md#mapping))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-JIRA-MAP-01 | When `epic_key` is blank and no Epic was created, the first pass shall create an Epic named after the project with label `specflow` and map it as `EPIC`; a configured Epic shall never be edited (D2, D21). | Blank: one Epic create with only the [create fields](sync.md#mapping), row `EPIC`; configured: zero calls on that key besides Stories' `parent`. |
| FR-JIRA-MAP-02 | Each active story shall be one Story under the Epic and each live task one Sub-task under its Story (D2). | A plan of 2 stories and 3 tasks gives 1 + 2 + 3 creates with the right types and parents. |
| FR-JIRA-MAP-03 | Each synced issue shall carry the labels `specflow` and, for Stories and Sub-tasks, `area-<slug>` (D3). | Area `AREA::WEB_SHOP` gives `area-web-shop`; a label a person added survives an overwrite. |
| FR-JIRA-MAP-04 | Story points shall follow [Owned fields](sync.md#owned-fields) (D4, D28). | Tasks low, high give Sub-tasks 1 and 5 and their Story 6; without a `Story Points` field no points are sent. |
| FR-JIRA-MAP-05 | Descriptions shall follow the [layout](sync.md#description-layout) (D4). | The ADF of a task with 2 done-when items has a task list with 2 `TODO` items, the phase line and the spec names. |
| FR-JIRA-MAP-06 | WAITS_FOR shall become `Blocks` links at task and story level as [Links](sync.md#links) defines (D4). | Task B waits for A in another story: Sub-task A blocks B, Story(A) blocks Story(B); live: the waiter shows "is blocked by". |
| FR-JIRA-MAP-07 | Specflow shall rank as [Rank](sync.md#rank) defines (D20). | First sync in plan order: zero rank calls; swapping two independent stories: one rank call; two unchanged stories swapped, one In Progress: its status comes from the pass's one batched search and it gets no rank call; live: Jira order equals plan order. |
| FR-JIRA-MAP-08 | While the api reads `PUBLIC_URL` (with `MCP_RESOURCE_URL` set), each created issue shall get one remote link to the plan page ([Remote link](sync.md#remote-link)). | With `PUBLIC_URL`: one remote-link upsert per create, global id `specflow:<pid>:<node>`; without, or without `MCP_RESOURCE_URL`: none. |
| FR-JIRA-MAP-09 | A redone task (a new revision) shall stay the same Sub-task (D17). | Task `X` completed then redone as `X_R2`: no create; To Do: overwritten `Task id`; else one comment. |
| FR-JIRA-MAP-10 | When `epic_key` is set after Specflow created an Epic, the created Epic shall be handled as removed and the Stories moved to the configured Epic; clearing `epic_key` again shall revive it ([Mapping](sync.md#mapping), D21). | With the fake: set `epic_key` → the created Epic gets `specflow-removed` and a comment (To Do), every Story a `parent` change; cleared again → label removed, Stories back. |
| FR-JIRA-MAP-11 | Specflow shall keep every sent field within the [field limits](sync.md#owned-fields). | A title with a line break is sent with one space; a 300-character title is 255 characters ending `…`; a 40,000-character description text is cut to 30,000; an area that slugs to nothing gives no area label. |

## Update rules ([sync.md](sync.md#update-rules))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-JIRA-UPD-01 | When an issue's content changed and its status category is `new`, Specflow shall overwrite the owned fields that differ and post no comment (D5). | A To Do Sub-task whose title changed gets one `PUT` and no comment. |
| FR-JIRA-UPD-02 | When an issue's content changed and its status category is not `new`, Specflow shall leave its fields and links and post one comment listing every change ([format](sync.md#comment-format), D5). | An In Progress Sub-task whose title and points changed gets no `PUT` and one comment with exactly those two items. |
| FR-JIRA-UPD-03 | Specflow shall never change status, assignee or comments it did not post, and never delete or transition an issue (D5, D6). | The fake records no `/transitions`, no issue `DELETE`, no `assignee` or `status` field; `DELETE` only on issue links Specflow created. |
| FR-JIRA-UPD-04 | When a mapped item leaves the plan, Specflow shall label it `specflow-removed` and comment when in To Do, and only comment otherwise (D6, D13). | Removed To Do issue: label op + 1 comment; removed In Progress issue: 1 comment, no `PUT`; the next pass: no call. |
| FR-JIRA-UPD-05 | When a removed item is live again, Specflow shall take `specflow-removed` off and overwrite in To Do, or comment otherwise. | Revived To Do issue: label removed, fields overwritten, 1 comment. |
| FR-JIRA-UPD-06 | When nothing changed since the last pass, Specflow shall make no Jira call (D7, D19). | Two passes over the same plan: the second records `succeeded`, `unchanged` = all items and the fake records zero calls. |
| FR-JIRA-UPD-07 | When only the layout changed, Specflow shall post no comment and store the new content (D19). | Bumping `MAPPING_VERSION`: To Do issues get a description `PUT`, others nothing, and no comment anywhere. |
| FR-JIRA-UPD-08 | When Jira no longer finds a mapped Story or Sub-task, Specflow shall mark it removed with `jira_not_found` and never call Jira for it again (D32). | A changed Sub-task whose key answers 404 (and a status search page refused with 400): row `removed_at` + `jira_not_found`, `removed` 1, status not `partial`; the next pass makes no call for it. |
| FR-JIRA-UPD-09 | When a live task's story is no longer an active story, Specflow shall handle its Sub-task as removed (D33). | Story retired, its task still `todo`: the Sub-task gets the removal handling of FR-JIRA-UPD-04. |

## Reliability ([sync.md](sync.md#failure-handling))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-JIRA-REL-01 | Specflow shall commit a pending map row before each create and adopt a created issue found by its property, only when the property names this Specflow project, instead of creating it again (D23). | Crash after the fake created the issue and before the key commit: the next pass searches, adopts the key, creates nothing. Two Specflow projects on one Jira project, both with a pending `EPIC`: each adopts only the issue whose property has its own project id. |
| FR-JIRA-REL-02 | If one item fails with 400 or 404, then the pass shall go on, mark the row and end `partial`. | One Sub-task's create answers 400: the others are created, status `partial`, `failed` 1, row `jira_invalid_field`. |
| FR-JIRA-REL-03 | If Jira answers 401, 403 or a missing project, then the pass shall stop and end `failed` with the code. | A 401 on the first call: status `failed`, `jira_auth_failed`, no further call. |
| FR-JIRA-REL-04 | The gateway shall wait `Retry-After` on 429 (at most 5 tries; over 60 s ends at once) and retry 5xx and transport errors 3 times, except a create, a comment and a link add, which are never sent again after a 5xx or timeout (D35). | Fake 429 with `Retry-After: 2` then 201: one sleep of 2 s and one success; 6 times 429: `jira_unreachable`; `Retry-After: 120`: no sleep, `jira_unreachable` at once; a create answered 503: one request, `jira_unreachable`; a search answered 503 then 200: two requests. |
| FR-JIRA-REL-05 | A comment shall be posted at least once per change; a crash between post and store may repeat it once. | Crash after the comment and before the content commit: the next pass posts the same comment once, then nothing. |

## MCP tools ([../mcp-server/tools.md](../mcp-server/tools.md#sync_to_jira))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-JIRA-MCP-01 | `sync_to_jira` shall start or merge a sync for any member and answer the status (D14). | A collaborator's call answers `running`; a second call while it runs answers `queued: true`; `jira_sync_status` right after it answers `running`. |
| FR-JIRA-MCP-02 | `jira_sync_status` shall answer the last status, counts and the node-to-key items, paged as the list tools page (D34). | After a pass of 6 issues: counts `created` 6; `limit` 4 gives 4 items with keys and URLs and a `next_cursor`, the next page 2 items and `next_cursor` null; a forged cursor is `invalid_cursor`. |
| FR-JIRA-MCP-03 | The tools shall answer `jira_unavailable` and `jira_not_configured` as tool errors with the messages of [tools.md](../mcp-server/tools.md#error-codes). | Both codes reach the agent as those codes, not `internal_error`. |

## Settings UI ([ui.md](ui.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-JIRA-UI-01 | The Settings header shall offer the tab "Jira" and `?tab=jira` shall open it. | Render and browser e2e: the header has "General" and "Jira"; the URL selects the tab. |
| FR-JIRA-UI-02 | The primary button shall read "Check access" until a check of the current values passes, then "Save" (D22). | Render: after an ok check the button reads "Save"; typing in a field brings "Check access" back. |
| FR-JIRA-UI-03 | The Switch shall be disabled until the target is saved and shall save `enabled` at once (D22). | Render: no target, the switch is disabled; browser e2e: typing the keys, "Check access", "Save", then the switch on sends one `PUT` with `enabled` true. |
| FR-JIRA-UI-04 | The status line shall show the status as a word, never by colour alone. | Render: `partial` shows "Synced with errors". |
| FR-JIRA-UI-05 | A collaborator shall see the tab read-only, and every user shall see the not available notice while `available` is false. | Render: collaborator view has no enabled control; `available: false` shows the notice and disables the form. |

## Non-functional

| ID | Requirement | Acceptance |
|---|---|---|
| NFR-JIRA-01 | The Atlassian token shall live in the environment only: never in the database, a response, a log or an exception text (D1). | Inspection: no token column; a test with a canary token finds it in no log record, error text or `repr` of the gateway, of the Jira settings or of the role settings. |
| NFR-JIRA-02 | Jira sync log events shall carry ids, codes and counts only ([api.md](api.md#log-events)). | Tests capture the events of a pass and find no summary, description or Jira body. |
| NFR-JIRA-03 | The feature shall add no configuration beyond `ATLASSIAN_SITE_URL`, `ATLASSIAN_EMAIL` and `ATLASSIAN_API_TOKEN` (D16). | The settings environment test lists exactly those three new optional names. |
| NFR-JIRA-04 | No database transaction shall be open while a Jira call runs. | A fake gateway asserts on each call that no transaction of the fake session is open. |
| NFR-JIRA-05 | The repository gates shall pass. | `make check`, `make validate`, `make frontend-build`, `make e2e-production` pass. |
| NFR-JIRA-06 | The sync shall assume one API replica, as the plan and gap runs do. | [overview.md](overview.md#boundaries) states it; the chart's own tests refuse an api with more than one replica or without the `Recreate` strategy. |
