---
type: Design
title: "Graph gap lifecycle"
description: "Gap and clarification transitions, the close check, rekeys and merges, follow-ups, dismissals, the plan prerequisite and link, development gaps and the migration of first-slice findings"
status: draft
tags: [graph, gap]
---
# lifecycle.md: gap and clarification transitions, the close check, follow-ups, dismissals, the plan link, development gaps and migration

A gap moves from detection to resolution through a fixed set of states. A person, AI, a gap run, a build
returning or the corpus reader's reconcile moves it on. Every transition is written in one transaction
with its `gap_event` row ([data-model.md](data-model.md) "Relational store"). A gap never reopens: when an
answer does not hold, a follow-up gap opens and points back to it, so the history stays readable.

## Gap transitions

| From | To | Trigger | What happens |
|---|---|---|---|
| (none) | `open` | Gap run, route `ask` | A checked report is recorded: `Gap` node, `ABOUT`, `IMPACTS`, `CITES`, option rows ([answers.md](answers.md) "Routing") |
| (none) | `answered` | Gap run, route `ai` | The gap is recorded together with an AI `Clarification` (mode `ai`) in one transaction; it never shows as open |
| (none) | `open` | Person, `raise_gap` | A gap with origin `person`; a site-mode run investigates it at once for options |
| (none) | `open` | Coding agent, `add_finding` over MCP | A gap with origin `development` ("Development gaps" below) |
| `open` | `answered` | Person, `answer_gap` | A `Clarification` (mode `option` or `own`) with status `pending`; amendments are stored |
| `open` | `resolved` | Person or AI, pointer answer | The spec already answers it: a `pointer` clarification is `GROUNDED_IN` the answering fragment at once; resolution `pointer` |
| `answered` | `open` | Person, withdraws the answer before it is applied | The clarification becomes `superseded` without a successor; its amendments are deleted |
| `answered` | `answered` | Person, answers again before it is applied | A new clarification `REPLACES` the old one |
| `answered` | `applying` | Apply (quiet period ends, `apply_now`, or an immediate development answer) | Amendments become active, the clarifications document is regenerated, `BuildRequests.request_update_build` is called |
| `applying` | `resolved` | Close check, fixed | Clarification `applied`, `GROUNDED_IN` set; resolution `answered` or `ai_answered`; held tasks released |
| `applying` | `resolved` | Close check, not fixed | Resolution `follow_up`; a new open gap `FOLLOWS_UP` it with reason `not_fixed` |
| `resolved` | `resolved` | Reconcile, conflict or orphaned | The clarification becomes `conflict` or `orphaned`; a follow-up gap opens ([reconcile.md](reconcile.md)) |
| `resolved` | `resolved` | Person, `revert_clarification` | The clarification becomes `reverted`, its amendments are reverted, an update build is requested, and a follow-up gap with reason `reverted` opens for a person |
| `open` | dismissed | Person, `dismiss_gap` with a reason | The node and its relations are deleted; the signature goes to `gap_suppression` |
| `open` | obsolete | Gap run | Every node the gap was `ABOUT` left the graph, or its detector no longer fires on unchanged witness ids, before anyone answered; the node is deleted |

A gap with status `answered` or `applying` is never dismissed; the answer is withdrawn or reverted first.

## Clarification transitions

| From | To | Trigger |
|---|---|---|
| (none) | `pending` | `answer_gap`, or the run's AI route |
| `pending` | `superseded` | A newer answer to the same gap, or the answer is withdrawn |
| `pending` | `applied` | The close check after the update build finds the text grounded |
| `applied` | `absorbed` | Reconcile: the owner's new revision holds the text or the same meaning |
| `applied` | `conflict` | Reconcile: the owner's new revision says something else (decision GD3) |
| `applied` | `orphaned` | Reconcile: the anchor and its section are gone |
| `applied` | `reverted` | `revert_clarification` |
| `conflict` | `applied` | The follow-up gap is answered with "restore the earlier answer": the suspended amendment is re-anchored and applied again |
| `conflict` | `superseded` | The follow-up gap is answered with "keep the upload" or a new answer; the follow-up's clarification `REPLACES` it |
| `orphaned` | `superseded` | The follow-up gap is answered (moved to the clarifications document, or dropped) |

A pointer clarification is created `applied` and only changes to `superseded` or `reverted`.

## Close check

The close check is the first step of every gap run that follows a build (step `close`, [architecture.md](architecture.md)
"Run steps"). It looks at every gap with status `applying`, and at every resolved gap whose clarification
reconcile changed since the last run.

1. **Grounding.** For each clarification, find the fragment that now holds its text. The build change
   report lists the fragments the update build re-extracted for the clarification's lineage. Among them, the
   fragment whose text contains the clarification's text (normalised whitespace and case) is the ground; for a
   clarifications-document answer, it is the fragment of the clarification's own section. Write
   `GROUNDED_IN` with the build id, and set `applied_build_id`.
2. **Re-check.** Re-run the gap's detector, with the gap's rules, on the gap's `ABOUT` nodes as they are now
   (after aliases). An open-question gap is fixed when its `OpenQuestion` nodes are gone; a structural gap
   when the graph fact no longer holds; a text gap when the witness line is gone or no longer matches the
   rule. A contradiction is fixed when the two lines no longer disagree (the refused-merge pair is gone, or
   the judge finds them consistent). Development and person gaps have no detector: they are fixed when the
   clarification is grounded.
3. **Outcome.** Fixed and grounded: status `resolved`, resolution `answered` (or `ai_answered`),
   clarification `applied`, held tasks released ("Plan link"). Not grounded: the build did not pick the text
   up; the gap stays `applying` for one more build, then follows up with reason `not_fixed`. Grounded but
   not fixed: status `resolved`, resolution `follow_up`, and a new open gap `FOLLOWS_UP` it with reason
   `not_fixed`; the new gap is investigated in the same run and its card shows the previous answer.
4. **Stale witnesses.** A `CITES` relation whose fragment no longer exists, or no longer holds the quote, is
   deleted; the quote stays in the gap event log.

The close check never calls a model except the contradiction judge of step 2, and that call is cached by the
input hash like every verdict.

## Rekeys and merges

Builds rekey and merge spec nodes. The build change report (classes `rekeyed`, `merged`, `removed`) reaches
the gap run as `GapChangeReport` ([api.md](api.md) "Models"). In step `scope`, the run:

- moves every `ABOUT` and `IMPACTS` relation from a rekeyed or merged id to its new id (no duplicates: a
  gap about both merged nodes keeps one relation);
- drops nothing for `removed` ids: the relations went away with the nodes; a gap left without `ABOUT` and
  without `CITES` becomes obsolete (when `open`) or is checked by the close check (when `applying`);
- keeps the signature: it is computed once from the ids at detection time, and the build's alias table
  maps old ids when a later run compares signatures.

## Follow-ups and history

A follow-up is a new gap: new id, status `open`, origin of the old gap, `FOLLOWS_UP` with a reason
(`not_fixed`, `orphaned`, `conflict`, `reverted`). Its `ABOUT` set starts from the old gap's, and its card
shows the old question, the old answer and why it did not hold. The old gap stays `resolved` and keeps its
clarification, so walking `FOLLOWS_UP` backwards gives the full history of one topic. A follow-up of a
follow-up is allowed; the inbox shows the chain as one thread.

## Dismissals and suppression

`dismiss_gap` needs a reason (free text, at most 500 characters) and is allowed only on `open` gaps. It
deletes the `Gap` node and its relations and writes a `gap_suppression` row: the signature, the gap id, the
question, the reason and who dismissed it. A later run that produces the same signature records the seed
with outcome `skipped` and asks nothing. A changed signature (other nodes, another witness line) is a new gap.

A dismissal with reason category "not spec" also offers to add a noise rule for the section
([detection.md](detection.md) "Relevance filter"); the rule is a separate, explicit action.

## Plan prerequisite

Only the affected part of the plan may change when an answer is applied. `graph.plan` re-plans only what the
build change reports mark as changed; the API host passes them (shipped before the gap module, independent of it):

1. Each build's change report is stored with the build (its `BuildResult` in the file build store, under the build
   id); `BuildStore.list_builds` gives the project's builds in build order.
2. The plan runner passes, as `PlanContext.changes`, the change reports of every build after the plan's
   `last_build_id` up to the latest finished build, converted to the plan-side `BuildChangeReport`
   (`api/graph_build/reports.py`, `api/plan/changes.py`).
3. A full re-plan happens only when a report is missing (a gap in the chain) or on an explicit `replan`
   request; the run logs `api.plan.change_reports_missing` with the reason.

It helps every document edit, not only gap answers. The gap runner reads the same stored reports for its own
`scope` step.

## Plan link

**Order of runs.** After a build returns, the host runs the gap run, then the plan run (unless the build's
settlement lists `documents_incomplete`, as for the plan). The plan run does not wait for answers. A gap run
failure is logged and never stops the plan run.

**Reads.** The plan's structural port for findings (`FindingsReads`: `open_counts`, `findings`) is filled by
`GapService`. Every open gap (status `open`, `answered`, `applying`) is shown as an open finding:

| `OpenFinding` field | From the gap |
|---|---|
| `finding_id` | `gap_id` |
| `kind` | `kind` |
| `text` | `question` |
| `blocking` | `yes` when severity is `blocker`, or a development gap holds its story |
| `about_spec_ids` | the `ABOUT` ids, aliases resolved |
| `story_id` | the reporting story of a development gap; none otherwise |
| `created_by`, `created_at` | as recorded |
| `orphaned` | `yes` when the gap has no `ABOUT` relation left |

Task and story briefs, story lists and previews therefore show open gaps on their nodes, blocking first,
with no change to `graph.plan`. The host fills the port with `api.gap.findings.OpenGaps`: the gap runner's
`GapService` (read on each call; none while gap runs are not configured, and a service that cannot be built for a
configuration fault is logged and left out) merged with the first-slice findings store, which keeps serving
development findings until the first-slice migration; counts add up and findings are united by id in the port's
order. The plan's `OpenFinding` has no severity, so the host's agent views look the severities of the shown gaps up
with one `list_gaps(about=...)` read. `IMPACTS` nodes are not counted as "about"; briefs list them under the
gap as "an answer will change".

**Blocker holds (decision GD2).** A task that implements a node an open blocker gap is `ABOUT` cannot be
claimed, and the refusal names the gap. The plan sees this through the same port: a claim of a story whose
tasks implement a node with an open blocking finding is refused with `TaskStateError` naming the gap. The
gap module calls the plan's existing hold and release calls through the `StoryHolds` port
([api.md](api.md) "Ports"): when a blocker gap is recorded, `hold_tasks` marks the tasks implementing its
`ABOUT` nodes `blocked` with `blocked_by` set to the gap id (tasks already `in_progress` are left alone and
listed in the run result); when the gap resolves, is dismissed or becomes obsolete, `release_gap` returns
those tasks to `pending`. Major and minor gaps never hold a task.

**Change reports.** When the update build that applied an answer returns, its change report reaches the plan
as for any edit, and the plan re-plans only the stories whose members changed.

## Development gaps

A coding agent that hits a hole in the spec while it builds a story reports a finding with the MCP tool
`add_finding`. In this specification a finding is a gap: the host maps the call to `GapService.raise_gap`
with origin `development`.

| `add_finding` kind | Gap |
|---|---|
| `question` | kind `open_question` |
| `conflict` | kind `contradiction` |
| `gap` | kind `missing` |
| `note` | no gap: a `gap_note` row, shown in the story brief as a note |

1. **Check.** The spec ids must resolve, quoted lines must be found in their fragments, and the limits of
   [answers.md](answers.md) apply. The agent's text becomes the question.
2. **Hold.** With `blocking`, the existing blocking link of the plan applies: the plan's guard checks that
   the agent holds the story and that a node of the finding is implemented by one of its tasks; then the
   claimed tasks of the story become `blocked` with `blocked_by` the gap id and the claim ends. Without
   `blocking`, nothing is held.
3. **Investigate now.** A site-mode gap run (`run_gap(ctx, mode="site", site_ids=...)`) investigates the gap
   at once, without waiting for a build. It adds `ABOUT` and `IMPACTS`, options and a recommendation; the
   agent's own text stays as the question.
4. **Already answered.** If the investigation finds the answer in the spec, AI resolves the gap with a
   `pointer` clarification that cites the answering lines. No build runs; the held tasks are released at once
   and the brief shows the answering lines.
5. **Answer.** Otherwise the gap tops the inbox. When a person answers a development gap that holds tasks,
   the answer is applied immediately (`request_update_build(..., immediate=True)`), with no quiet period.
6. **Release.** When the close check resolves the gap, the held tasks return to `pending`. The agent claims
   again and reads a brief built from the updated spec.
7. **Not spec.** If the investigation returns `not_spec` (a library bug, a failing test, a tooling issue),
   the gap is closed as obsolete with a `gap_note`, and the held tasks are released.

A development gap uses the same inbox, card, answer path and close check as any other gap. The differences:
it is investigated and applied at once, and it holds only the tasks of the story that reported it (even when
its severity is not blocker).

## Migration of first-slice findings

The first slice ([../gap.md](../gap.md)) stores `Finding` nodes with `ABOUT` relations, `ExternalRef` nodes with
`REFERS_TO`, and a blocking link to plan stories. A one-time migration, run by `ensure_gap_schema` when it
finds `Finding` nodes in a project:

| First slice | Becomes |
|---|---|
| `Finding` with status `open` | `Gap`, origin `development`, kind mapped as in the table above, severity `major`, status `open`, title from the first line of the text, question the full text |
| `Finding` with status `resolved` | `Gap` status `resolved`, resolution `answered`, with a clarification of mode `own` holding the closing note |
| `Finding` with status `dismissed` | A `gap_suppression` row; no node |
| `Finding` kind `note` | A `gap_note` row; no node |
| `ABOUT` to spec nodes | `ABOUT` from the new gap |
| `ABOUT` to a `Fragment` | `CITES` with no quote |
| `ExternalRef` and `REFERS_TO` | The refs (type and value) in the `detail` of the gap's first `gap_event` |
| `FND::` id | Kept as `alias` in the first `gap_event`; plan tasks with `blocked_by` an `FND::` id are rewritten to the new gap id |

The `Finding` and `ExternalRef` nodes are deleted after the migration commits. The MCP tool, the story
blocking and the plan's findings port keep working throughout, now backed by `GapService`.
