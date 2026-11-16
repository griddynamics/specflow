---
type: Design
title: "Graph gap answers"
description: "Severity, routing and gates, the question card, clarifications, edits and placement, the clarifications document, applying answers, AI answers, revert and dismissal"
status: draft
tags: [graph, gap]
---
# answers.md: severity, routing, the question card, clarifications, edits, placement, applying, AI answers, revert and dismissal

A checked report ([investigation.md](investigation.md)) becomes a gap with options. This file says who
answers it, what a person sees, how an answer becomes a `Clarification` and corpus text, how answers are
applied through an update build, and how AI answers, reverts and dismissals work. What happens when a
source document changes after an answer is in [reconcile.md](reconcile.md); statuses and the close check are
in [lifecycle.md](lifecycle.md).

One rule runs through the whole file: **gap never edits the spec graph**. An answer is recorded as a
`Clarification` node and, for its text, as amendment rows. The text enters the spec only when the corpus
reader hands it to a normal update build, which extracts it like any other document text.

## Severity

The investigator proposes a severity; code accepts it, or lowers it when the report's evidence does not
support it (a gap that touches no planned node is never `blocker`). The four levels:

| Severity | Meaning | Who answers | Graph | Effect on the plan |
|---|---|---|---|---|
| `blocker` | The affected part cannot be built without a decision | A person, always | `Gap` node | Tasks that implement a node the gap is `ABOUT` cannot be claimed while it is open ([lifecycle.md](lifecycle.md) "Plan link") |
| `major` | Building it means guessing behaviour users will see | A person, with the recommended option preselected | `Gap` node | Shown in task and story briefs and on stories |
| `minor` | A local detail the text already implies | AI when every gate holds, otherwise a person | `Gap` node | Shown in briefs |
| `info` | Worth knowing, nothing to decide | No one | A `gap_note` row only | None |

Severity rules applied by code after the report is checked:

| Rule | Effect |
|---|---|
| No `ABOUT` or `IMPACTS` node is implemented by a plan task, and the run has a plan | `blocker` becomes `major` |
| The gap is `ABOUT` only out-of-scope nodes (`in_scope` `no`) | Any severity becomes `info` |
| Kind `contradiction` between two stated lines with different numbers | At least `major` |
| A development gap that blocks its task | At least `major`; `blocking` `yes` holds the reporting agent's claimed story regardless of severity ([lifecycle.md](lifecycle.md) "Development gaps") |
| The report's severity is `info` | Written as a `gap_note`; no node, no options |

## Routing

Routing is code, not a model. The model proposes the severity and the evidence for each gate; code reads
the thresholds from `routing` in [gap-rules.yaml](gap-rules.yaml) and decides the route (`ask` or `ai`).

```
route(gap):
  if severity == info:                      -> gap_note (no route)
  if severity in (blocker, major):          -> ask
  if kind not in routing.ai_kinds:          -> ask
  if origin == development:                 -> ask      # a person sees what an agent asked
  if any gate fails:                        -> ask
  if run.ai_answers >= limits.ai_answers_per_run
     or run.ai_touched_nodes / spec_nodes >= limits.ai_answers_max_node_share:
                                            -> ask      # the valve
  otherwise                                 -> ai
```

`routing.ai_kinds` is `missing`, `ambiguous` and `inconsistency`. AI never answers `contradiction` (choosing
a side is a decision), `open_question` (the authors asked a person), or `assumption` (only a person can
confirm one). The one exception is a pointer: when the investigation finds that the spec already answers
the gap, the route is `ai` with mode `pointer` for any kind except `contradiction`, because nothing new is
decided ("Pointer answers" below).

### Gates

| Gate | Holds when | Evidence |
|---|---|---|
| Entailed | The recommended option follows from text already in the spec | At least one basis quote on the recommended option, found in its fragment |
| Local | The gap's `ABOUT` and `IMPACTS` nodes together number at most `local_max_nodes` (5), in at most `local_max_areas` (1) work area or owning module | Counted from the checked report |
| Consistent | No stated line says otherwise | The second check found no contradicting line ([investigation.md](investigation.md) "Second check") |
| Confident | The second check agrees with confidence of at least `min_confidence` (0.85) | The second check's answer |

Every gate result is stored on the gap's `gap_event` "routed" entry, so a person can see why AI answered or
why it did not. Until the gates are built the entry holds the route, its mode (`pointer`), the reasons
(`severity`, `kind`, `ai_answers_off`, `ai_branch_off`, `pointer_confirmed`) and whether the gap is held back.

### The valve

Per run, AI answers at most `ai_answers_per_run` gaps (20), and the nodes those answers touch (`ABOUT` and
`IMPACTS`) are at most `ai_answers_max_node_share` (5 %) of the project's spec nodes. Gaps over the valve are
routed `ask`, with the reason "AI answer limit reached" in their event. The valve protects a project from a
run that would rewrite much of its spec without anyone looking.

## The question card

The Gaps inbox shows one card per open gap with route `ask`, ordered by severity, then reach (the number of
plan tasks behind the `ABOUT` and `IMPACTS` nodes), then age. At most `new_questions_per_run` (25) new cards
per run are shown as new; the rest are listed under "More found in this run". The route step ranks a run's new
`ask` gaps by severity, then reach (plan tasks behind the site), then the site's rank score; the gaps beyond the
limit are written with `held_back` `yes` and left out of the default view (`list_gaps(held_back=False)`) until the
next run's record step clears the mark.

| Part | Content | Source |
|---|---|---|
| Chips | Severity, kind, origin (found by analysis, reported by a coding agent, raised by a person), the stories behind it | `Gap` properties; trace from `ABOUT` to plan tasks |
| Title | One line, at most 160 characters | `Gap.title` |
| Question | What has to be decided, as a question | `Gap.question` |
| Why | Why this is a gap and what depends on it | `Gap.why` |
| Affected nodes | One chip per `ABOUT` node and per `IMPACTS` node, marked as such, each opening the node view | Graph |
| Quotes | Each witness line with document, section and line numbers | `CITES` |
| Options | 2 to 4 offered answers; exactly one marked **Recommended** and preselected for `major`; each with label, detail and a preview of its edits | `gap_option` rows |
| Other | A free-text answer of the person's own | Always offered |
| Placement | "Amend the source section" or "Clarifications document"; preset from the option's edits; the person may switch | Option edits |
| Actions | Submit answer; Not a gap (with a reason); Open in graph | |

A card never shows an option without edits, and never more than one recommendation. When the investigation
of a development gap is still running, the card shows the agent's question with "Options are being
prepared" and accepts an own answer at once.

## Answering

### AnswerInput

`GapService.answer_gap(project_id, gap_id, AnswerInput)` records an answer. `AnswerInput`:

| Field | Meaning |
|---|---|
| `mode` | `option` or `own` (people); `ai` and `pointer` are set only by a gap run |
| `option_rank` | The chosen option, for `option` |
| `text` | The person's own text, for `own` |
| `edits` | Optional: the edits confirmed in the preview; for `own` they are required |
| `placement` | Optional override: `amend` or `document` |
| `answered_by` | The user id, from the host's session; `ai` for AI answers |

Checks, each a `GapError` with its code:

| Check | Code |
|---|---|
| The gap exists in the project and is `open` or `answered` | `gap_not_answerable` |
| `option_rank` names one of the gap's options | `gap_option_unknown` |
| `text` is 1 to 4000 characters for `own` | `gap_answer_length` |
| Every edit's target resolves to an active fragment of the project, or the clarifications document | `gap_edit_target` |
| The caller is a member of the project (host check) | host error |

### Clarification

A valid answer creates a `Clarification` and its relation `ANSWERS` to the gap, in one transaction with
the amendment rows:

| Property | Value |
|---|---|
| `clr_id` | As in [data-model.md](data-model.md) "Identity" |
| `mode` | `option`, `own`, `ai` or `pointer` |
| `option_label` | The chosen option's label (`option`, `ai`); empty for `own` |
| `text` | The answer text the edits carry, joined |
| `placement` | `amend`, `document` or `none` (pointer) |
| `status` | `pending` (`applied` at once for a pointer, see below) |
| `answered_by`, `answered_at` | Who and when |
| `basis`, `confidence` | For `ai` and `pointer`: the basis quotes and the second check's confidence |

`PLACED_IN` links the clarification to each document its edits go into: the source document of every
amendment, or the clarifications document. The gap moves to `answered`.

### Own answers become edits

An own answer is free text; the corpus needs edits. `GapService.preview_edits(project_id, gap_id, text,
placement)` asks the gateway role `gap_edits` to turn the text into edits for this gap. The call sees the
gap's question, why, the witness lines and the sections they are in (as untrusted data), and the text. It
returns edits in the same shape as an option's edits. Code checks every edit ("Edits" below), and the card
shows the result as a diff before the person submits. The person can change the placement, or go back to
the text; nothing is stored until Submit.

When the `gap_edits` call fails or its edits do not check, the preview offers the clarifications document
placement with the text as written, which needs no anchor.

### Changing an answer

| When | What happens |
|---|---|
| Before it is applied (gap `answered`, clarification `pending`) | A new answer replaces the pending clarification: the old one is deleted with its amendment rows, and the event log keeps it |
| After it is applied (gap `resolved`) | A person answers again from the gap's history: a new clarification `REPLACES` the old one, the old one becomes `superseded`, its amendments `reverted`, and the new amendments take their place in the same update build. The gap stays `resolved`; the close check runs again |

## Edits

An edit says where an answer's text goes:

| Field | Meaning |
|---|---|
| `target` | `fragment_id` and `anchor_quote` (a line in that fragment), or `clarifications_document` |
| `operation` | `replace` (the anchor line), `insert_after` (after the anchor line), or `append_section` (a new section, only in the clarifications document) |
| `text` | The text to place; plain Markdown, one or more sentences |

Checks on every edit, from an option, from `gap_edits` or from AI:

| Check | Effect when it fails |
|---|---|
| The fragment is active and the anchor quote is found in it (normalised whitespace and quotes) | The edit is rejected |
| `replace` targets a line the gap `CITES`, or a line of a node the gap is `ABOUT` | Rejected: an answer replaces only what the gap is about |
| The text is 1 to 2000 characters per edit and holds no heading line | Rejected |
| At most one edit per anchor line per answer | Rejected |
| No edit targets a document of another project | Rejected |

### Placement rules

| Case | Placement | Operation |
|---|---|---|
| A gap that lives in one section of one document (one `CITES` fragment, or all `ABOUT` nodes evidenced in one section) | `amend` (default) | `replace` the question or vague line; `insert_after` the line it extends |
| A `contradiction` | `amend`, always | `replace` the losing line in its own document; the winning line stays untouched |
| An `inconsistency` across documents (one name for one thing) | `amend` | One `replace` per document that uses the losing name |
| A system-wide rule, a new requirement across modules, or a gap with no source line (structure only) | `document` | `append_section` |
| A pointer | `none` | No edit |

A person may switch an `amend` answer to `document`. A `contradiction` cannot be switched: with the
document placement both statements would stay in the graph.

### One answer, several edits

One answer may carry several edits, in one or more documents. Example: the gap "Order review limit is 1,000
EUR in orders-spec and 500 EUR in payments-spec", answered "500 EUR everywhere", has one edit: `replace` in
orders-spec.md › Order review. The payments line stays as it is. A naming gap "Customer or Client" answered
"Customer" has one `replace` per document that says "Client". Each edit becomes one amendment row; all of
them belong to one clarification and are applied by the same build.

### Answer marker

Every amended text ends with a marker line naming the gap, so a reader of the effective text can see why it
changed:

```
> Clarified by GAP::7C2A91E04B3D
```

The marker is a blockquote line the build's fragment parser keeps as part of the section text. The ontology's
extraction guidance treats such a line as provenance, not as a requirement; if a build extracts it as a node,
the test corpus check of slice 3 fails ([lifecycle.md](lifecycle.md) "Close check"). The clarifications
document carries the gap id in each section heading instead.

## Amendments

Each `amend` edit becomes one `corpus_amendment` row ([data-model.md](data-model.md) "Relational store"):

| Column | Value |
|---|---|
| `lineage` | The lineage of the target fragment's document: `doc:<document id>` or `drive:<connection id>:<file id>` |
| `heading_path` | The heading path of the target section, from the document title down |
| `anchor_text`, `anchor_hash` | The anchor line, normalised, and its SHA-256 |
| `operation`, `text` | From the edit; `text` includes the answer marker |
| `status` | `active` |
| `last_revision_id` | The revision id of the document the anchor was found in |

The corpus reader applies active amendments in [reconcile.md](reconcile.md) "Applying amendments".

## Clarifications document

Answers with no single home go into one generated document per project:

| Property | Value |
|---|---|
| Storage | A `documents` row with origin `todo_answer` (the origin the host already reserves), lineage `doc:<its id>`, title "Clarifications" |
| Content | One section per clarification with status `applied` or `pending` (once an update build is requested) and placement `document`, ordered by answer time |
| Section heading | `## <title of the gap> (GAP::<id>)`, naming the parts it is about in plain words, so the build's hub catalogs link the section to the nodes it names |
| Section body | The answer text, then the line "Answers: <question>" |
| Rebuilt | From the current clarifications every time an update build is requested; a reverted or superseded clarification simply disappears |
| Revision id | The content hash of the generated text, so the build sees a change only when a section changed |

The document is read-only in the app. It is listed with the other documents, marked "Generated by
Specflow".

A clarification placed in the document does not replace the stated line it answers. For an `open_question`
the build's answered-elsewhere check then drops the question node, because another fragment answers it.

## Applying answers

Answers are applied in batches by one update build:

```
answer recorded (gap answered, clarification pending)
  -> quiet period: limits.apply_quiet_seconds (120) after the last answer of the project,
     or "Apply now" in the inbox (GapService.apply_now),
     or at once for a development gap that blocks a task
  -> amendments active, clarifications document rebuilt
  -> BuildRequests.request_update_build(project_id, reason="gap_answers", immediate=...)
  -> every answered gap in the batch moves to applying
  -> the host runs the update build when no other build of the project runs
  -> the build returns; the host runs a gap run; its close check settles each applying gap
```

| Rule | Detail |
|---|---|
| One build at a time | `BuildRequests` queues the request when a build of the project is running; one queued request covers every answer recorded before it starts |
| `immediate` | Skips the quiet period; it never starts a second concurrent build |
| A failed build | Gaps stay `applying`; the next successful update build settles them. After two failed builds the event log records "apply failed" and the inbox shows it |
| Answers recorded while a build runs | Wait for the next request |

## AI answers

A gap routed `ai` is answered by the gap run that created it, with the recommended option:

1. A clarification with mode `ai`, `answered_by` `ai`, the option label, the basis quotes and the second
   check's confidence.
2. Its edits become amendments or a clarifications document section, as for a person's answer.
3. The gap moves to `answered`, then `applying` with the next batch; AI answers never use `immediate`.

### Review list

The inbox has a tab "Answered by AI" listing every clarification with mode `ai` or `pointer`: the gap's
title, the answer, the basis quotes, the confidence and the build that applied it. Nothing waits for a
review; the list exists so a person can check and undo.

### Revert

`GapService.revert_clarification(project_id, clr_id, reason)` withdraws an answer, from a person's or AI's:

| Step | Effect |
|---|---|
| Clarification | Status `reverted`; the event log records who and why |
| Amendments | Status `reverted`; the clarifications document drops its section at the next rebuild |
| Build | An update build is requested (quiet period applies); it removes the text and the nodes it made |
| Gap | If `resolved`: a follow-up gap opens (`FOLLOWS_UP` reason `reverted`) with the same question and options, route `ask`; the old gap stays `resolved` as history. If still `answered` or `applying`: the gap returns to `open` |

A reverted AI answer always goes to a person: its follow-up gap has route `ask`, and the same signature
never routes `ai` again unless the person sends it back.

## Pointer answers

When the investigation finds lines that already answer the gap (the build missed them, or a coding agent
did not see them), the clarification has mode `pointer`:

| Property | Value |
|---|---|
| Placement | `none`; no edit, no amendment, no build request |
| `GROUNDED_IN` | The fragment(s) holding the answering lines, set at once |
| Status | `applied` at once |
| Gap | `resolved` with resolution `pointer` by the same run, after the second check confirms the lines answer the question |

For an `open_question`, the next build's answered-elsewhere check drops the question node if it now sees
the answer; if it does not, OQ-STATED fires again on the same signature and the run finds the existing
pointer, so no new gap opens. For a development gap, the held story is released at once with the answering lines
in its brief ([lifecycle.md](lifecycle.md) "Development gaps").

## Dismissing

`GapService.dismiss_gap(project_id, gap_id, reason)` closes a gap a person judges not to be one:

| Step | Effect |
|---|---|
| Reason | Required, 1 to 500 characters; one of "not a gap", "not spec", "duplicate", "won't decide now" or free text |
| Suppression | A `gap_suppression` row with the signature, question, reason and who; detectors that produce the same signature stay quiet |
| Graph | The `Gap` node and its relations are deleted; its options are deleted; the event log keeps the record |
| "not spec" | Also offers to add a noise rule for the section ([detection.md](detection.md) "Relevance filter") |
| Plan | A blocker hold on its tasks ends |

A suppression ends when the signature changes: other `ABOUT` nodes, another detector, or a changed witness
line. "Won't decide now" is a suppression too; the gap returns when its lines change.

## Who answers

Any member of the project may answer, dismiss or revert a gap; the host checks membership. `answered_by`
and every event record the user id. There are no roles in this release ([later.md](later.md)).

## Raising a gap by hand

`GapService.raise_gap(project_id, RaiseInput)` lets a person (origin `person`) or a coding agent (origin
`development`, through MCP) open a gap: question, optional kind, the spec ids it is about, optional quoted
lines, and, for an agent, the task and whether it blocks. Code checks the ids and quotes as for a report;
an investigation of the site starts at once and adds options, `IMPACTS` and a severity. Until it finishes,
the gap is `open` with severity `major` and route `ask`.
