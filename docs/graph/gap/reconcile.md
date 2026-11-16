---
type: Design
title: "Graph gap reconcile"
description: "How amendments meet new source revisions: applied again, re-anchored, adopted, in conflict or orphaned; effective revisions; download with answers"
status: draft
tags: [graph, gap]
---
# reconcile.md: amendments against new source revisions, effective revisions, and download with answers

## Why reconcile exists

Specflow never writes to a customer's sources: Drive files and uploaded originals stay as they are
([overview.md](overview.md) "Non-goals"). An answer placed in a source section is kept beside the document as
an amendment ([answers.md](answers.md) "Amendments"), and the corpus reader applies it whenever it reads the
document. Sooner or later the owner uploads a new version, or Drive syncs a changed file. The new revision
may touch the very line an answer changed.

Four things can have happened to that line, and each needs a different response:
- nothing: the old file came back (Drive never changed), so the answer must be applied again;
- the owner adopted the answer, word for word or in their own words, so the amendment must retire;
- the owner wrote something else, so the answer and the owner's text disagree;
- the section is gone, so the answer has no place.

Reconcile tells them apart, for every active amendment, every time a new revision arrives. It never
overwrites a person's newer text without a question (decision GD3 in [questions.md](questions.md)).

## Where it runs

Reconcile runs in the host's corpus reader (`PostgresCorpusSource`), which builds the corpus snapshot of a
build: the newest document of each lineage (`doc:<document id>` for uploads, `drive:<connection id>:<file
id>` for Drive files), with its Markdown text.

| Moment | What happens |
|---|---|
| The snapshot meets a lineage with `active` or `undecided` amendments whose `last_revision_id` differs from the lineage's current (source) revision | Reconcile runs for those amendments before the build reads the text |
| An `undecided` amendment (the judge failed last time) | Reconciled again on every read until it is decided |
| The lineage's revision equals `last_revision_id` | No reconcile; active amendments are applied as stored |
| A lineage with amendments is no longer in the snapshot (the document was deleted, or replaced by a new file id) | Every amendment of the lineage becomes `orphaned` |
| A lineage has no amendments | Untouched; the text is passed as it is |
| `suspended`, `orphaned`, `retired`, `reverted` amendments | Never reconciled: they are kept for their follow-up gap and history, and applied again only through a new answer |
| An `active` amendment whose `last_revision_id` is the effective revision id the last build read (an answer stores the graph document's revision id, which is the effective one when the lineage already had answers) and that id is the effective id of the current source revision with the lineage's current amendments | Current, not reconciled: its `last_revision_id` becomes the source revision id, with no outcome |
| A row an answer, apply or revert changed while reconcile ran | Not written back (reconcile writes a row only while its stored status and `updated_at` are as it read them); the next read meets it again |

`graph.gap` owns the logic; the corpus reader calls it:

```
reconcile_document(project_id, revision, amendments, *, previous_text, judge, settings, now) -> ReconcileResult
reconcile_corpus(project_id, revisions, inputs, *, dry_run) -> CorpusReconcile
```

`revision` is the lineage, its source revision id and text (`LineageText`); `previous_text` is the text the
last build read (the graph's active fragments of the lineage, joined), used to find each amendment's old section.
`reconcile_corpus` runs `reconcile_document` for every lineage with `active` or `undecided` amendments
(`Amendments.pending_lineages`), stores the changed rows through `Amendments.put` and one `reconciled` event per
outcome through `GapRecords.log`, and returns the rows to apply by lineage. The host's gap runner calls it from
`corpus_answers(project_id, revisions, dry_run=...)`, the `CorpusAnswers` port of the corpus reader; a dry run
stores nothing and asks no judge.

| Field of `ReconcileResult` | Meaning |
|---|---|
| `text` | The effective text the build reads: the revision with every active amendment applied |
| `outcomes` | One per amendment: amendment id, outcome, the matched line (if any), the similarity, the judge verdict (if asked) |
| `changed` | Amendment rows to update (status, anchor, `last_revision_id`) |
| `applied` | The rows applied to `text` (an undecided amendment as an insertion after its section, never stored so) |
| `follow_ups` | The clarifications whose amendments became `suspended` or `orphaned` |

The corpus reader stores `changed` through the `Amendments` port and records every outcome in `gap_event`.
Nothing is handed over in memory: the gap run that follows the build reads the amendment statuses in its close
step and moves the clarifications on and opens the follow-ups ([lifecycle.md](lifecycle.md) "Close check"). The
judge calls happen inside `reconcile_document` through the gateway role `gap_judge` (prompt
`gap.judge.1.reconcile.2`, system text `prompts/reconcile.md`), cached in `gap_verdict` by input hash; the host
opens the gateway only when a question needs it. When no judge is given (a dry run, an estimate), the "same
meaning" question is skipped: the outcome is `undecided` and nothing is stored.

## The four questions

For each amendment of the lineage, in the order of the amendments' positions in the previous text (a `replace`
before an `insert_after` on the same line, then answer time), reconcile asks four questions and stops at the first
"yes". The amendment's section: its old heading path is where the previous text holds the amendment's first line
(else its anchor line, else the row's `heading_path`); the new revision's section with that heading path (or the
only section with the same last heading), else the section at least `section_similarity` similar to the old one
as its source said it (the amendment's text taken out, its anchor line back in).

1. **Is the anchor still there?** The normalised anchor line is in that section: exactly, or a line of it with
   similarity of at least `anchor_similarity` that is of the same kind (both questions or both statements) and the
   same polarity (both negated or neither). Then the amendment is applied again (re-anchored when the line
   changed: `anchor_text`, `anchor_hash` and `heading_path` move to the new line). A question the owner answered in
   place ("A customer cannot cancel a paid order.") is never re-anchored over: it goes on to questions 2 and 3.
2. **Is the answer already in the new revision?** The normalised answer text (without the marker line) is
   contained in the section with the same heading path, or anywhere in the document when that heading is
   gone. Then the owner adopted it literally.
3. **Is the section there but saying something else?** The section exists. The `gap_judge` call compares the
   answer (and the line it replaced) with the section's text: first the answer's `decisions` (each rule, limit,
   number, actor, time, scope and consequence, marked `stated`, `missing` or `contradicted` in the section), then
   `same`, `different` or `unrelated`, with a confidence. Code holds the verdict to the list: `same` with any
   decision not `stated` is `different`, so a section that copies part of the answer (a partial adoption) is a
   conflict. Same meaning with at least `judge_min_confidence`: adopted by meaning (below it: different).
   Different meaning: conflict. Unrelated: go to question 4. The call fails: undecided.
4. **Is the section gone?** No section matches. The answer has lost its place: orphaned.

Normalisation: quote marks made plain, lower case, collapsed whitespace, Markdown emphasis, list markers and
quote marks (`>`) removed, answer marker lines left out. Similarity: the Dice coefficient of the content-word sets
(stop words left out, light suffix stemming), so a reworded question keeps its content words and a new rule adds
many. The thresholds live in `GapSettings.reconcile` (`ReconcileSettings`), tuned on the shop corpus's
re-uploads: `anchor_similarity` 0.7, `section_similarity` 0.6, `judge_min_confidence` 0.7.

## Outcomes

| Outcome | Found by | Amendment | Clarification | Gap | Build reads |
|---|---|---|---|---|---|
| Applied again | Question 1, exact | `active`, `last_revision_id` updated | Unchanged (`applied`) | Unchanged | Revision with the amendment |
| Re-anchored | Question 1, similar line under the same heading | `active`, new `anchor_text` and `anchor_hash` | Unchanged | Unchanged | Revision with the amendment on the new line |
| Adopted literally | Question 2 | `retired` | `absorbed`; `GROUNDED_IN` moves to the owner's fragment after the build | Unchanged | Revision as uploaded |
| Adopted by meaning | Question 3, same meaning | `retired` | `absorbed` | Unchanged | Revision as uploaded |
| Conflict | Question 3, different meaning | `suspended`, anchored on the owner's line most like the answer | `conflict` | A follow-up gap opens | Revision as uploaded |
| Orphaned | Question 4 | `orphaned` | `orphaned` | A follow-up gap opens | Revision as uploaded |
| Undecided | Question 3, the judge call failed | `undecided` (anchor and `last_revision_id` kept, so the next read asks again); a second failure is a conflict | Unchanged | Unchanged | Revision with the answer inserted after the section's last line (the owner's text is kept) |

A retired amendment is never applied again. A suspended or orphaned amendment is kept for its follow-up gap
and is not applied again; an answer to that gap writes new amendments when it needs any. The clarification and
gap columns are written by the close step of the next gap run, which reads the amendment statuses.

### Conflict follow-up

Under decision GD3 the owner's newer text wins until someone decides: the build reads the upload, the
amendment is suspended, and the next gap run opens a follow-up gap:

| Property | Value |
|---|---|
| Kind, severity | `contradiction`; the original gap's severity, at least `major` |
| Relations | `FOLLOWS_UP` (reason `conflict`) to the original gap; `ABOUT` the nodes evidenced in the fragment that holds the owner's line and the original gap's nodes still in the graph, `CITES` the owner's new line |
| Question | "The new upload of <document> changes an earlier answer. Which one is right?" |
| Why | Both texts, who answered and when the answer was given, and the upload |
| Route | `ask`, always; no detector, so a later run never takes it out as obsolete; not investigated (its options are fixed) |

Its options (option rows; the third choice is the card's own-text answer, which every gap has):

| Option | Edits | Effect |
|---|---|---|
| Keep the upload (recommended) | None | Resolves at once, no build: a clarification with mode `pointer`, placement `none`, `GROUNDED_IN` the owner's line, `REPLACES` the original one, which becomes `superseded`; the suspended amendment becomes `reverted` (and its other amendments still applied: then an update build is requested); the gap's resolution is `pointer` |
| Restore the earlier answer | One `replace` of the owner's line with the earlier answer text (a clarifications-document section when the owner's line is not in the graph yet) | Applied like any answer: a new amendment anchors on the owner's line; when it is applied the original clarification becomes `superseded` and the suspended amendment `reverted` |
| Write a new answer (own text) | From the person's own text | As any own answer; applied the same way |

### Orphaned follow-up

| Property | Value |
|---|---|
| Kind, severity | The original kind; the original severity |
| Relations | `FOLLOWS_UP` (reason `orphaned`) to the original gap; `ABOUT` the original gap's `ABOUT` and `IMPACTS` nodes, while they exist |
| Question | "The section that held the answer to <title> is gone. Where does the answer go now?" |
| Route | `ask`; no detector (never obsolete); not investigated |

| Option | Effect |
|---|---|
| Move it to the clarifications document (recommended) | One `append_section` edit with the answer text: the answer becomes a section of the clarifications document; the rule stays in the spec. When applied, the orphaned clarification becomes `superseded` and its amendment `reverted` |
| Drop it: the section was removed on purpose | No edit; the follow-up resolves at once (resolution `answered`), the orphaned clarification becomes `superseded` and its amendment `reverted`; the next build no longer has the answer's nodes, and the plan retires their tasks |
| Place it in the section <heading> | Offered when another document has a section with the lost section's heading (the new file of a replaced document): an `insert_after` edit after that section's last line. Any other section: the person's own text with an edit there |

## Worked examples

All examples start from the same answer. The original section of orders-spec.md:

```
## Place order
Payment is captured when the order is placed.
Can a customer cancel a paid order?
```

The amendment Specflow keeps (operation `replace`, anchor "Can a customer cancel a paid order?"):

```
A customer can cancel a paid order within 30 minutes of payment. The full amount is refunded.
> Clarified by GAP::7C2A91E04B3D
```

### Same file synced again

Upload (Drive never changed; other sections were edited):

```
## Place order
Payment is captured when the order is placed.
Can a customer cancel a paid order?
```

Question 1 finds the anchor exactly: applied again. The build reads the amended section, which is identical
to what it read before, so this section has no change and nothing is re-planned.

### Question line reworded

Upload:

```
## Place order
Payment is captured when the order is placed.
Can an order that is already paid be cancelled?
```

Question 1 finds a line under the same heading with similarity above 0.85: re-anchored. The amendment now
anchors on the new line and replaces it. The build reads the same effective text as before.

### Owner pasted the answer in

Upload (the owner used "Download with answers" or copied the text):

```
## Place order
Payment is captured when the order is placed.
A customer can cancel a paid order within 30 minutes of payment. The full amount is refunded.
```

Question 2 finds the answer text: adopted literally. The amendment retires and the clarification becomes
`absorbed`. The build reads the upload; the only difference from before is the missing marker line, so the
requirement keeps its meaning, at most its evidence changes, and nothing is re-planned.

### Owner wrote it in their own words

Upload:

```
## Place order
Payment is captured when the order is placed.
Paid orders may be cancelled for half an hour after payment; the customer gets a full refund.
```

Question 2 does not match; question 3 asks the judge, which answers "same meaning": adopted by meaning. As
above, the amendment retires and the build reads the upload.

### Owner changed the rule

Upload:

```
## Place order
Payment is captured when the order is placed.
Paid orders can be cancelled until the order ships. A 5% fee is kept.
```

Question 3: the judge answers "different meaning": conflict. The build reads the upload; the requirement
changes meaning, and only the Place order story is re-planned. The follow-up gap shows both texts and asks
which is right. If the person restores the earlier answer, a new amendment replaces the owner's line, the
next build changes the requirement back, and the story is re-planned once more.

### Section deleted

Upload:

```
## Order history
Customers can see their past orders.
```

No section with the heading "Place order" and no similar section: orphaned. The build reads the upload; the
cancellation requirement is no longer in the text, so the build removes it and the plan retires its tasks.
The follow-up gap asks whether to keep the rule in the clarifications document (recommended) or drop it.

## Clarifications document answers

Not built yet (both mechanisms below wait for the contradiction judge of the close check). A clarification placed
in the clarifications document has no anchor in a source; its section is generated by Specflow and never conflicts
with itself. A newer upload can still contradict it: the owner may now state
the rule differently in the source. Two mechanisms catch this:

| Mechanism | What it does |
|---|---|
| Reconcile of the sections the clarification is about | When a lineage changes, reconcile also takes every `document` clarification whose `ABOUT` nodes are evidenced in that lineage, and asks the `gap_judge` the same-meaning question against the changed sections. Different meaning: conflict, as above, and the clarifications document drops the section while the follow-up gap is open (the upload wins) |
| Contradiction detectors | CT-VALUE and CT-CROSS see the generated section and the source as two documents; a contradiction they find is matched to the clarification by its `ABOUT` nodes and becomes the same follow-up |

## Applying amendments

After reconcile, the corpus reader applies every `active` amendment of the lineage to the revision text:

| Rule | Detail |
|---|---|
| Which rows | `active` amendments, and `undecided` ones as reconcile placed them for this read |
| Order | By anchor position in the revision; then by answer time for amendments on the same anchor |
| `replace` | Replaces the anchor line with the text |
| `insert_after` | Inserts the text after the anchor line, or after the text of an earlier amendment on the same line |
| Two `replace` amendments on the same anchor | The newer clarification wins only if it `REPLACES` the older; otherwise this cannot happen, because an edit check refuses a second `replace` of a line that an active amendment already replaces ([answers.md](answers.md) "Edits") |
| Line endings and headings | Untouched; the amended text never holds a heading |
| Finding the anchor | A line equal to the amendment's anchor text (normalised like a quote), under the last heading of its `heading_path` when that heading holds one, else the first equal line of the document. An amendment whose anchor is not found is not applied for this read and is logged (`api.graph_build.amendment_unanchored`); reconcile decides its outcome when it runs |

### Effective revision id

The build decides what changed by revision id. A lineage with active amendments gets the effective
revision id: SHA-256 of the revision id and the sorted (amendment id, text hash) pairs that were applied. A
lineage without active amendments keeps the revision id it has today. So:
- a build that applies a new answer sees a new revision of the same lineage, and re-extracts only the
  changed fragment. This needs fragments no bigger than a section: the host gives a project's first build one
  fragment per section (`api.graph_build.runner.SECTION_FRAGMENTS`, `fragment_tokens` min 1, target 1); a project
  built before keeps its frozen fragment sizes (a small document is one fragment) until a rebuild. With section
  fragments a real update build extracts only the amended section; the build's later steps (links, attributes) can
  still report other nodes of the same document as changed (session 10 spike; open item);
- an old file synced again with the same amendments gives the same effective revision id as before, and
  the build sees no change;
- retiring an amendment because the owner adopted it changes the effective revision id, and the build
  re-reads the section, which now says the same in the owner's words.

The `CorpusRevision` the build receives keeps its fields (`lineage_id`, `revision_id`, `version`, `path`,
`title`, `markdown`); only the values of `revision_id` and `markdown` are the effective ones. The build is
unchanged.

## Events

Every outcome is a `gap_event` row (event `reconciled`, actor `reconcile`) on the clarification and its gap: the
amendment id, the outcome, the lineage, the old and new anchor lines, the line or heading that decided it, the
similarity, the judge's verdict (`failed` when the call failed) and confidence, and the revision ids
(`previous_revision_id`, `revision_id`). The gap's
history in the app shows them in order, so a person can see when an answer was adopted, re-anchored or
challenged.

## Download with answers

Amendments are meant to fade into the sources, so Specflow does not stay a second source of truth:

| Feature | Detail |
|---|---|
| Banner | Each document view shows "N answers are not in the source file yet", with the list of active amendments and their gaps |
| Download | `GET /documents/{id}/export?answers=1` returns the effective text (Markdown, as an attachment) of the document's lineage: the newest revision with its answers applied, marker lines included, as a build reads it (the snapshot is reconciled as a dry run: nothing stored, no judge asked, so an undecided answer reads as the build read it); without `answers=1`, or without the gap module, the source text; `404 document_not_found` when the document is not in the corpus |
| Adoption | When the owner pastes the text into their file and the file comes back, reconcile finds the answers (question 2) and retires the amendments; the banner count falls |
| Clarifications document | Downloadable the same way, for owners who want to move the answers into their own documents (comes with the document view; it is not a `documents` row) |

## Edge cases

| Case | Handling |
|---|---|
| Several amendments in one section | Each is reconciled on its own, in anchor order; outcomes may differ (one adopted, one in conflict) |
| Two amendments on the same anchor (an `insert_after` and a `replace` from different answers) | The `replace` is applied first; the `insert_after` anchors on the replaced text's first line; reconcile treats them in the same order |
| Partial adoption: the owner copied one sentence of a two-sentence answer | Question 2 fails (the whole text is not there); question 3 asks the judge, who sees the missing part: different meaning, so conflict; the follow-up's "Restore" option restores the whole answer |
| The whole document deleted (the lineage leaves the corpus) | Every amendment of the lineage becomes `orphaned`; one follow-up gap per clarification |
| A Drive file renamed or moved | The lineage is kept (`drive:<connection id>:<file id>` does not change); reconcile runs as for any revision |
| A Drive file replaced by a new file id, or an upload of the same document as a new file | A new lineage: the old lineage leaves the corpus (all orphaned), and the orphaned follow-ups offer "Place it in another section" with the new document's matching section preselected when a section with the same heading exists |
| The anchor line appears twice in the section | The first occurrence under the heading wins (an amendment keeps no line position) |
| The judge call fails | The amendment becomes `undecided` and stays applied (after the section's last line) for this build (FR-GREC-08); the next build asks again; after two failures it becomes a conflict for a person |
| A suspended or orphaned amendment's follow-up gap is dismissed | The amendment becomes `reverted`; the upload stays what the build reads; the clarification keeps `conflict` or `orphaned` as history |
