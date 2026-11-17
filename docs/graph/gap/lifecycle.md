---
type: Design
title: "Graph gap lifecycle"
description: "Gap and clarification transitions, the close check, rekeys and merges, follow-ups, dismissals, the plan prerequisite and link, development gaps and the migration of first-slice findings"
status: stable
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
| `answered`, `applying` | `open` | Person, `revert_clarification` of the pending answer | The clarification becomes `reverted`, its amendments are reverted, the gap's route becomes `ask` (an AI answer goes to a person); an update build is requested when its amendments were already active |
| `answered` | `answered` | Person, answers again before it is applied | The new pending clarification replaces the old one, which is deleted with its amendments (answers.md "Changing an answer") |
| `resolved` | `resolved` | Person, answers again after it is applied | A new pending clarification `REPLACES` the applied one, which becomes `superseded` when the answers are applied |
| `answered` | `applying` | Apply (quiet period ends, `apply_now`, or an immediate development answer) | Amendments become active, the clarifications document holds the answer from the next corpus read, `BuildRequests.request_update_build` is called |
| `applying` | `resolved` | Close check, fixed | Clarification `applied`, `GROUNDED_IN` set; resolution `answered` or `ai_answered`; held tasks released |
| `applying` | `resolved` | Close check, not fixed | Resolution `follow_up`; a new open gap `FOLLOWS_UP` it with reason `not_fixed` |
| `resolved` | `resolved` | Reconcile, conflict or orphaned (read by the next run's close step) | The clarification becomes `conflict` or `orphaned`; a follow-up gap opens with fixed options ([reconcile.md](reconcile.md)) |
| `resolved` | `resolved` | Reconcile, applied again or re-anchored, and the detector fires again | Resolution `follow_up`; a follow-up gap with reason `not_fixed` opens (once) |
| `open` | `resolved` | Person, an option without edits on a reconcile follow-up ("Keep the upload", "Drop it") | Resolves at once without a build: a clarification `applied` with placement `none` (mode `pointer` grounded in the owner's line for "Keep the upload", resolution `pointer`; else mode `option`, resolution `answered`) |
| `resolved` | `resolved` | Person, `revert_clarification` | The clarification becomes `reverted`, its amendments are reverted, an update build is requested when its text was in the corpus, and a follow-up gap with reason `reverted` (route `ask`, the old question, nodes, quotes and options) opens for a person |
| `open` | dismissed | Person, `dismiss_gap` with a reason | The node and its relations are deleted; the signature goes to `gap_suppression` |
| `open` | obsolete | Gap run | Every node the gap was `ABOUT` left the graph, or its detector no longer fires on unchanged witness ids, before anyone answered; the node is deleted |
| `open` | closed as a note | Gap run, a development gap's investigation answers `not_spec` | The node is deleted, a `gap_note` keeps the agent's question, its held tasks are released (event `obsolete`, reason `not_spec`) |

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
| `applied`, `absorbed` | `reverted` | `revert_clarification` (the gap stays `resolved`; a follow-up opens) |
| `pending` | `reverted` | `revert_clarification` while the gap is `answered` or `applying` (the gap returns to `open`) |
| `conflict` | `superseded` | The follow-up gap is answered: "keep the upload" at once (its clarification `REPLACES` this one); "restore the earlier answer" (a new amendment on the owner's line) or a new answer when it is applied. The suspended amendment becomes `reverted` |
| `conflict`, `orphaned` | (kept) | The follow-up gap is dismissed: the clarification stays as history, its suspended or orphaned amendment becomes `reverted` |
| `orphaned` | `superseded` | The follow-up gap is answered: "drop it" at once, "move it to the clarifications document" or another placement when applied; the orphaned amendment becomes `reverted` |
| `pending` (never grounded) | `superseded` | The gap followed up with `not_fixed` after two builds did not ground it, and the follow-up's answer is applied: the old answer never reached the spec, so the follow-up's answer takes its place and its amendments become `reverted` (one question has one answer in the corpus) |

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

How the shipped close step (`steps/close.py`) decides:

| Question | Rule |
|---|---|
| Which clarification | The gap's clarification that is `pending` with `apply_requested_at` (the latest one) |
| Grounded (`amend`) | Every `active` amendment's text, the marker line left out, whitespace and case normalised, is in an active fragment of the amendment's lineage; `GROUNDED_IN` goes to each such fragment with the build id |
| Grounded (`document`) | A fragment of the clarifications document holds the text (its own section, headed by the gap id, first); `PLACED_IN` the clarifications document is written too (GI4) |
| Detector re-check | The detectors run over the graph with every section read as `spec` (the gap was relevant when found); the gap's detector still fires when a seed has one of the gap's `seed_signatures`, is about a node of the detector's types the gap is `ABOUT`, or quotes a line the gap cites (with none of these to compare, the step cannot tell and leaves the gap as it is). Detectors that read the build's facts (`refused_merge`, `cycle`) still fire while two of the gap's nodes of their types are left, because an update build lists only the refusals and cuts it made itself. The contradiction judge is not called yet |
| Not grounded twice | The first close check writes the event `not_grounded`; the next one resolves with resolution `follow_up` and opens a follow-up; the clarification stays `pending` |
| Obsolete | An `open` gap whose `ABOUT` nodes all left and whose quotes no longer hold (`nodes_gone`), or whose detector no longer fires on it (`detector_silent`); its option rows go with it; event `obsolete` |
| Follow-up | Kind, severity, origin, title, question, why and detector of the old gap; its `ABOUT` and `IMPACTS` nodes that are left and its quotes that still hold; route `ask`. The run's `sites` step seeds it like a raised gap, so detector seeds on its nodes join its site and it is investigated in the same run; the resolved gap it continues joins no site any more |
| Held tasks | After the step, the run releases the holds of every gap it resolved or took out as obsolete (`StoryHolds.release_gap` for the gap id and each of its `aliases`; the run's artifact lists the released task ids); a release the plan refuses is logged and left |
| Reconciled answers | Every `resolved` gap whose current clarification is `applied` with placement `amend`: the step reads its amendments and compares their ids, statuses, anchor hashes and revisions with the clarification's `reconcile_state` (written when it was resolved or last looked at); only a changed state is acted on. A `suspended` amendment: clarification `conflict` and the conflict follow-up; an `orphaned` one: clarification `orphaned` and the orphaned follow-up; all `retired`: clarification `absorbed`, `GROUNDED_IN` the fragment holding the answer text, else the fragments under its heading; otherwise (applied again, re-anchored, undecided) the re-check of an applying gap: grounded again, and a `not_fixed` follow-up when the detector fires again and the gap has none yet ([reconcile.md](reconcile.md) "Outcomes") |
| Reconciled before applied | An `applying` gap whose clarification's amendments an upload in the same build made `suspended`, `orphaned` or all `retired`: the same outcome as for an applied answer, and the gap resolves (resolution `follow_up` with the conflict or orphaned follow-up, or `answered` when absorbed) |
| Reconcile follow-ups | Have no detector and are never obsolete; get the fixed options of reconcile.md, not a site; are not counted in `follow_ups` (counts `close.reconciled`) |
| Revert follow-ups | Keep the old gap's detector and seed signatures (a seed of the old gap joins their site, so a run re-checks them) but are never obsolete: they wait for a person (the reverted text may be in the graph until the revert's build). The same holds for an `open` gap that a revert reopened (it holds a `reverted` clarification) |

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
deletes the `Gap` node and its relations and writes `gap_suppression` rows: for the gap's signature and for each of
its `seed_signatures` (the seeds of its detector that formed or re-checked it, [data-model.md](data-model.md)
"Identity"), each with the gap id, the question, the reason and who dismissed it. A later run drops a seed with a
suppressed signature before it forms a site (counted `suppressed`) and asks nothing; a report whose signature is
suppressed is dropped by the check step. A changed signature (other nodes, another witness line) is a new gap. Seeds
of other detectors that shared the gap's site are not suppressed: their problem may still be one.

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
configuration fault is logged and left out). A coding agent's findings are development gaps, so they show through the
same port; a `note` is no gap and shows nowhere in the briefs. The plan's `OpenFinding` has no severity, so the host's agent views look the severities of the shown gaps up
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

As built: the existing block calls need a claim (`check_block`, `block_story`), so they cannot hold tasks nobody
claimed; the plan got one host call for GD2, `PlanService.hold_tasks(project_id, gap_id, spec_ids, *, reason)`
([../plan/api.md](../plan/api.md)): in one locked transaction every `pending` or `failed` task implementing a node
of `spec_ids` (merged ids resolved) becomes `blocked` with the reason of the gap's kind (`missing` →
`spec_missing`, `contradiction` and `inconsistency` → `spec_contradiction`, else `spec_ambiguous`) and `blocked_by`
the gap id; `in_progress` tasks are left alone. The plan ontology does not change. Holding is idempotent and is
repeated (`graph.gap.holds.hold_blockers`, every open gap of severity `blocker`, development gaps included: GD2 holds
the tasks of a blocker's nodes whatever its origin, while a development gap below blocker holds only its reporting
story through the block call): when a person raises a gap with severity `blocker`, at the end of every gap run's
`record` step (the run result's `held_tasks`), and before every `claim_story` (the agent route holds the open
blockers about the story's nodes first). So a task a plan run reset (its nodes changed), a task that was
`in_progress` when the hold came and was released later, and the follow-up that inherits a blocker's severity are
held again before anyone claims them. A claim of a story with a held task is refused `not_available` (the plan's
`blocked` state) and the refusal's `details.findings` name the gap; the plan's own `claim_task` and `claim_next`
(no agent route) refuse a blocked task without naming it. Releases (`graph.gap.holds.release`, for the gap id and
every alias the gap keeps, read before the step that deletes the gap): the close step's resolved and obsolete
gaps, a gap the `record` step resolved by a pointer, closed as not spec or refined below `blocker` (a raised
blocker the investigation rates `major`), and `dismiss_gap`. A hold or release the plan refuses (`plan_applying`
during a persist) or a store error is logged and left; the next repetition holds again, a release is repeated by the
next end of the gap or a person's unblock in the plan.

**Change reports.** When the update build that applied an answer returns, its change report reaches the plan
as for any edit, and the plan re-plans only the planning parts whose members changed; every other part is reused.

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

As built (`GapService.raise_gap` with origin `development`, the MCP route `POST /agent/findings`):

| Step | Rule |
|---|---|
| Arguments | The route checks `refers_to` (forms of [../../mcp-server/tools.md](../../mcp-server/tools.md) "add_finding") and that a blocking finding has a kind other than `note`, a story and a claim token, before any read; the question is 10 to 1000 characters (the `answers.md` limits); unknown ids are `node_not_found` |
| Note | `GapService.add_note`: a `gap_note` row (detector `development`, no run); the answer's id is `NOTE::` and 12 hex |
| Dedupe | The development signature (story, current `ABOUT` ids, normalised question): an open gap with it is returned (`created` false), nothing held |
| Limit | At most 50 open development gaps per agent and story (`AnswerInvalidError` field `limit`, the tool's `finding_limit`) |
| Hold | `blocking`: `StoryHolds.check_block` before anything is written, then the gap, then `block_story` with `finding_id` the gap id; a failed block deletes the gap again and its error passes; `RaisedGap.held` lists the blocked tasks |
| Refs | The checked `refers_to` go into the `detail` of the gap's `created` event |
| Investigate | The route queues the `site` run at the head of the project's queue (`GapRunner.start_site`); the run refines kind, severity, title and why, adds `ABOUT`, `IMPACTS` and options and keeps the agent's question; the route is always `ask` (routing never answers a gap of origin other than `analysis` by AI) |
| Pointer | A confirmed pointer on the raised gap (the `record` step's re-check): the gap resolves (resolution `pointer`) with a `pointer` clarification `GROUNDED_IN` the answering fragment; its holds are released; `list_gaps` shows the answering lines as its `answer` |
| Not spec | A `not_spec` verdict on an open development gap: the gap is deleted, a `gap_note` keeps its question, its holds are released (event `obsolete`, reason `not_spec`); its nodes are not marked as noise |
| Answer | An answer to a development gap with `blocking` `yes` is applied at once (`AnswerResult.immediate`); the close check after the update build resolves it and releases the story |

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

The `Finding` and `ExternalRef` nodes are deleted after the migration commits. As built (`graph/gap/migrate.py`, `migrate_first_slice`
through `ensure_gap_schema`):

| Rule | As built |
|---|---|
| Reads and deletes | Its own named queries (`gap_read_first_slice`, `gap_delete_first_slice_findings`, `gap_delete_first_slice_refs`, each with its PostgreSQL port): the ontology no longer declares `Finding`, `ExternalRef` or `REFERS_TO`; the deletes run in one transaction after the gap writes |
| Question and title | The text, trimmed and cut to 1000 characters at a word break (the `question` limit); the title its first line cut to 160 |
| Identity | Signature `development_signature(story_id, current ids, question)`; the `FND::` id goes into the gap's `aliases` property and the first event's `alias`. Plan tasks are not rewritten: the gap's aliases keep the blocking link (`get_gap` and the release of holds resolve them) |
| Resolved | A clarification of mode `own`, placement `none`, status `applied` with the closing note (or a fixed sentence without one), answered by the person who resolved it, not grounded (no fragment holds it) |
| Events | `created` (actor and time of the finding; `migrated: true`, the alias, the refs, counts) and, for a resolved finding, `resolved` |
| Same signature | A finding whose signature an open gap already has adds its id to that gap's `aliases` (event `aliased`); a finding already migrated by an earlier attempt is skipped, so a second run changes nothing |
| Notes and dismissals | A `note` is a `gap_note` row (run id `first-slice-migration`, detector `development`); a dismissed finding a `gap_suppression` row of its signature |
| Nothing to point at | A finding whose ids resolve to no node and that is about no fragment still becomes a gap (with no `ABOUT`); the close check takes it out as obsolete when it is open | The MCP tool, the story
blocking and the plan's findings port keep working throughout, now backed by `GapService`.
