---
type: Design
title: "Linear as a plan destination"
description: "How a project's plan is exported to a Linear team and project as issues: target, mapping, rules of a pass, status mirror, runs, permissions, codes, limits"
status: draft
tags: [linear, plan, mission-control]
---
# plan.md: the Linear plan destination

A project can send its plan to one Linear team and one Linear project as issues. It mirrors the Jira plan sync
([../jira-sync/sync.md](../jira-sync/sync.md)) and the GitHub Projects destination ([../github/projects.md](../github/projects.md)):
the same ownership rules, the same "never delete", the same one pass per project. It runs on the lifted export code
(`api.exports`: runner, digest, run status) and reads the plan the way the Jira sync does (`api.jira.desired.build`, a
pure function). Code: `backend/src/api/linear_plan/` (`content.py`, `scaffold.py`, `sync.py`, `runner.py`, `state.py`,
`store.py`, `access.py`, `wiring.py`), `backend/src/db/linear_plan.py`, `backend/src/api/routers/linear_plan.py`,
`backend/src/api/routers/agent_linear.py`. Routes and shapes: [api.md](api.md#plan-destination-routes).

## Target

One row per project in `linear_targets` ([data-model.md](data-model.md#linear_targets)): the Linear team (`team_id`,
`team_key`), the Linear project (id, name, URL), `created_by_graphico`, `enabled` and the last pass.

- The target is **a team and a Linear project of that team**. The form picks an existing project, or leaves the project
  blank (AC2): Graphico then creates one Linear project named after the Graphico project, with the id
  `UUIDv5(project, "project", "-")`. A retry of the save adopts the project that exists.
- The export acts as the member the project's Linear is connected by (`PUT /projects/{pid}/integrations/linear`, rule
  R1). Who that is is read from `project_integrations` at the start of **every pass**, so a removed member, a removed
  binding or a take-over by another member takes effect on the next pass. No binding is `linear_not_connected`; an
  expired or revoked connection is `linear_reconnect_required`.
- Anyone on the project may save the destination (D6).
- A change of the team or of the Linear project deletes the project's map rows (`linear_issues`) in the same
  transaction, and the next pass creates the issues again in the new place. The old issues stay in Linear.
- Saving a target that is turned on, or whose team or project changed, starts a pass (`settings`). Turning it off, or
  saving it unchanged, does not.

## Mapping

The desired set is `api.jira.desired.build` with a placeholder epic: stories, their live tasks, labels, order, removed and
revived nodes. `api/linear_plan/content.py` turns each item into Linear's shapes. [`LINEAR-PLAN.md`](../../plans/LINEAR/LINEAR-PLAN.md)
section 3 is the intended mapping; this table says what the code does.

| Plan | Linear | Code |
|---|---|---|
| Graphico project | the target's Linear project | `routers/linear_plan.py` |
| release of a story | a project milestone, named after the release; stories only. Id `UUIDv5(project, "milestone", release)`; made on first use | `scaffold.milestone`, `sync._make` |
| work area | a team label `area-<slug>` inside the single-select group label "Graphico area" (the group is made when missing) | `scaffold.label_ids` |
| story | an issue in the team and the Linear project | `sync._create` |
| task (newest revision, live) | a sub-issue (`parentId` = the story's issue) | `sync._create` |
| points | the issue's estimate: a task low, medium, high = 1, 3, 5; a story = the sum of its tasks | `api.jira.desired.build` (`POINTS`) |
| WAITS_FOR | a "blocks" relation, blocker to blocked, at task level and at the story level that `build` derives from it | `sync._relate` |
| text | the description's first block | `content.render` |
| done when | a Markdown checklist under `### Done when` (`- [ ]` lines) | `content._checklist` |
| implements | a `**Specs:**` line: `name (spec id), ...` | `content._specs` |
| node id, kind, area, phase, revision, release, points | a `>>> Graphico` block of facts at the end of the description | `content._facts` |
| link to the node | a link attachment "Open in Graphico" to the project's canvas (`{PUBLIC_URL}/projects/{pid}`, the same address for every issue: the canvas has no per-node address); made only when `PUBLIC_URL` is set (see Limits) | `sync._link` |
| ownership | the label `graphico` on every issue (it replaces the builder's `specflow` label) | `content.canonical` |
| a note on every issue | a `> [!NOTE]` block: managed by Graphico; changes overwrite while the issue is in Triage, Backlog or Todo, and arrive as comments after work starts | `content._NOTE` |
| removed from the plan | the label `graphico-removed` and a comment; never deleted | `sync._apply` |

Labels are found by name first (a person may have made them; a team's labels are shared by every Graphico project of the
team) and made with a client id `UUIDv5(project, "label", "<parent id>/<name>")` when missing. A label that a person
added to an issue stays on it; the owned and area labels are the plan's.

The plan's story order and the tasks' order inside a story are **not** exported: the map column `position` is not written.
An issue's priority, assignee and state are never set by a pass.

## Rules of a pass

`push_once` (`api.linear_plan.sync`): skipped with `no_plan` or `plan_applying`, `linear_not_configured` without an
enabled target, `target_changed` when the destination changed during the pass. A pass that has nothing to do ends
`succeeded` with no Linear call. Otherwise, in order:

1. **Check the project.** The Linear project must still exist, else the pass ends `failed` with
   `linear_project_not_found`.
2. **Create** each missing story and task. The client id of an issue is `UUIDv5(project, "story" or "task", node id)`
   (`linear.ids`), so the pass is **idempotent**: a create whose row was never written (a crash) answers "already
   exists" and the pass reads the issue and adopts it. A pending row is written first, then the issue is created, then
   the row gets the issue id and the identifier, then the "Open in Graphico" link, then the content and its hash. A task
   waits for the next pass if its story has no issue (refused or gone).
3. **Update** an item whose content hash changed. The issue's state is read. An issue whose state type is `triage`,
   `backlog` or `unstarted` takes the plan's title, description, estimate and labels (and the milestone and parent when
   they changed). Any other issue gets **one comment** listing the changed fields and is otherwise left alone; the stored
   content moves on, so the comment is not repeated. The comment has a client id made of the node, the old hash and the
   change list, so a crashed pass does not post it twice.
4. **Remove**: an item that leaves the plan gets the label `graphico-removed` and a comment, in any state, and
   `removed_at`. It is never deleted or closed. An item that returns loses the label, gets a comment, and (when editable)
   is overwritten.
5. **Relate**: for each node with a blocker that has an issue and is not linked yet, `blocker blocks node` is created
   (id `UUIDv5(project, "relation", "<blocker>><node>")`; "already exists" is adopted). Relations are **only added**:
   the gateway has no delete, so a blocker that leaves the plan stays linked.

Items and errors:

- **Estimate refused (AC3).** If the team's estimate scale refuses the value (`linear_invalid` with field `estimate`),
  the issue is made or updated without an estimate, the item is marked `linear_estimate_refused` and counted in
  `estimate_refused`. This is not a failure.
- **Invalid.** Linear's validation refusal of any other field fails that item only (`linear_invalid`, counted in
  `failed`, status `partial`).
- **Gone.** A `not found` on an issue (or on the parent of a sub-issue, or on a relation end) means a person deleted
  it in Linear: the item gets `removed_at` and `linear_issue_gone` and is **never made again**.
- Any other refusal (rate limit, access, network) ends the pass `failed` with one closed code, and the next pass
  continues from the rows.
- A crash between an issue's create and its row is safe because of the client id. Linear calls never run inside a
  database transaction, and every row write checks that the destination is unchanged.

Progress is reported in two steps: `issues` (creates and updates, the total can grow once when updates are counted) and
`relations`.

## Runs

The runner is `api.exports.runner.ExportRunner`; `LinearPlanRunner` (`api/linear_plan/runner.py`) says how the target is
read and a pass is recorded. Triggers: `plan_run` (the plan fan-out, `PlanDestinations`), `mcp` (`sync_to_linear`),
`settings` (saving a destination that was turned on or changed) and `recovery` (a start finds a stored `running`, or a
rate-limit retry). Starts are merged, one pass runs per process, `running` is stored before the slot. The runner is
built only when `runtime.linear` is set (`wiring.py`), and its recovery starts with the api.

**Rate limit and recovery runner.** The gateway does not retry (AC-S2-1). A `LinearRateLimitedError` ends the pass
`failed` with `linear_rate_limited`; the runner then starts the project again as a `recovery` pass after the
`Retry-After` Linear sent (at least 1 s, at most 3600 s; 60 s when Linear gave none). There is one waiting retry per
project. The retries are cancelled when the api closes; the next start repeats them. The pass is idempotent, so a
recovery pass only finishes the work. Linear's limits for an OAuth member: 5,000 requests and 2,000,000 complexity points
an hour (LINEAR-PLAN.md section 2). Per-issue mutations are used, not batch creates (AC5).

A stored `running` without a live pass reads `failed` with `interrupted`; a disabled target reads `skipped` with
`linear_not_configured` (`api.exports.status`).

## Task status mirror

When an agent changes a task's status, the Linear sub-issue of the task follows (decision O4). Code:
`backend/src/api/linear_plan/mirror.py` (`mirror_changes`), `LinearPlanRunner.mirror_task_status` (`runner.py`),
`mirror_to_linear` (`api/write_support.py`) and its three callers in `api/routers/agent_stories.py` and
`agent_writes.py`. It runs after the agent's write has committed, in the background: the agent's call never waits for
Linear and never fails because of it.

| Agent call | Task status | Linear |
|---|---|---|
| claim a story (`agent_stories.py`) | each task that is now `in_progress` | the sub-issue moves to the team's first `started` state |
| complete a story (`agent_writes.py`) | each completed task | the sub-issue moves to the team's first `completed` state; the pull request URL, when given, is attached as a link "Pull request" |
| add a blocking finding (`agent_writes.py`) | each task the finding blocks (`blocked`) | one comment "Graphico: task blocked: finding `<id>`"; the state does not change |

Rules:

- **Forward only.** An issue whose state is already at or beyond the wanted type (`triage`, `backlog` and `unstarted` rank
  0, `started` 1, `completed` and `canceled` 2) is left alone, so a person who moved it further is not pulled back. The
  state is the one with the lowest position of its type in the issue's team. Without such a state nothing moves (log
  `api.linear_plan.mirror.no_state`).
- The name of the state set is stored in `linear_issues.mirrored_state`.
- A comment has a deterministic id from the node, the status and the task's attempt, so a retried call does not post it
  twice. The pull request link is idempotent at Linear.
- Only a project with an **enabled** target is mirrored, and only a task that has an issue row that is not removed.
  The mirror acts as the member the project's Linear is connected by, like a pass.
- One mirror run at a time per process. A Linear rate limit ends the run (log `api.linear_plan.mirror.rate_limited`); the next
  status change, or a full pass, catches up. Any other failure is logged as its code (`api.linear_plan.mirror.failed`)
  and never reaches the agent.
- The mirror also handles a `failed` task (a comment, no move). **The API exposes no `failed` task status**, so nothing
  produces it today.

## In the canvas

The plus of the Plan stage lists **Linear** next to Jira and GitHub Projects; it needs the project's Linear integration,
else the card says "Set up first". The form (`frontend/components/v2/canvas/LinearPlanForm.tsx`,
`linear-plan-logic.ts`, `nodes-linear-plan.ts`) lists the teams of the connected member, then their projects, or lets
Graphico create the project. **Check access** runs as the bound member. **Add destination** saves it turned on and the
pass runs in the background; the canvas reads the status while it runs. The exact wording of the card is not specified
here.

## Permissions

| Who | May |
|---|---|
| Any member of the project | read the target and its status, list teams and projects, check access, save or turn off the target, call both MCP tools |
| The bound member (their Linear account) | is the one Linear sees: every create, edit, comment and relation is made as them |
| A Linear member without write rights on the team | the pass fails with `linear_forbidden` (the check cannot try a write) |

The OAuth scope is `read,write`; no `admin` scope is used.

## Codes

Codes of the API answers: [api.md](api.md#codes). Codes of a pass or an item (`linear_targets.last_error_code`,
`linear_issues.error_code`, the status `error_code`):

| Code | Where | Meaning |
|---|---|---|
| `linear_not_configured` | pass, MCP | no enabled target (status `skipped`) |
| `no_plan`, `plan_applying` | pass | the project has no plan, or the plan is being applied (status `skipped`) |
| `target_changed` | pass | the destination changed during the pass (status `skipped`) |
| `linear_not_connected`, `linear_reconnect_required` | pass | no binding, or the member's connection cannot act |
| `linear_rate_limited` | pass | Linear asked to wait; a recovery pass follows |
| `linear_project_not_found` | pass | the Linear project is gone or not visible |
| `linear_forbidden`, `linear_not_found`, `linear_invalid`, `linear_unreachable` | pass, item | the Linear codes of [oauth.md](oauth.md#codes) |
| `linear_issue_gone` | item | a person deleted the issue; it is never made again |
| `linear_estimate_refused` | item | the team's estimate scale refused the points; no estimate was set |
| `unexpected_error` | pass | an unexpected failure; the log has the error type only |
| `interrupted` | status | a stored `running` with no live pass |

## Limits

- The "Open in Graphico" link is built from the Linear settings' `PUBLIC_URL` (`runtime.settings.linear.public_url`,
  `wiring.py`), the web app's address that the OAuth callback also uses. It opens the project's canvas, not a node.
- A task that moves to another story is re-parented only when the issue is still editable.
- Relations are never removed; labels a person added stay.
- A change in a state other than Triage, Backlog or Todo is a comment, never an overwrite.
- A team's workflow is not changed: no state is created or moved by a pass.
- The first live use against a real workspace is the owner's to confirm; the tests use a fake gateway and recorded-shape
  parity tests.
