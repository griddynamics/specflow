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
| The snapshot meets a lineage with active, suspended or orphaned amendments whose `last_revision_id` differs from the lineage's current revision | Reconcile runs for that lineage before the build reads its text |
| The lineage's revision equals `last_revision_id` | No reconcile; active amendments are applied as stored |
| A lineage has no amendments | Untouched; the text is passed as it is |

`graph.gap` owns the logic; the corpus reader calls it:

```
reconcile_document(project_id, lineage, revision_text, amendments) -> ReconcileResult
```

| Field of `ReconcileResult` | Meaning |
|---|---|
| `text` | The effective text the build reads: the revision with every active amendment applied |
| `outcomes` | One per amendment: amendment id, outcome, the matched line (if any), the similarity, the judge verdict (if asked) |
| `changed` | Amendment rows to update (status, anchor, `last_revision_id`) |
| `follow_ups` | Follow-up gaps to open (conflict, orphaned), created by the next gap run |

The corpus reader stores `changed` through the `Amendments` port, records every outcome in `gap_event`, and
passes `follow_ups` to the gap run that follows the build. The judge calls happen inside
`reconcile_document` through the gateway role `gap_judge`; when the gateway is not available (a dry run, an
estimate), the "same meaning" question is skipped and the amendment is treated as a conflict candidate
without changing its status.

## The four questions

For each amendment of the lineage, in the order of the amendments' anchor positions in the old revision,
reconcile asks four questions and stops at the first "yes":

1. **Is the anchor still there?** The normalised anchor line is found in the section with the same heading
   path: exactly, or, under the same heading, a line with similarity of at least 0.85. Then the amendment
   is applied again (re-anchored when the line changed).
2. **Is the answer already in the new revision?** The normalised answer text (without the marker line) is
   contained in the section with the same heading path, or anywhere in the document when the heading
   changed. Then the owner adopted it literally.
3. **Is the section there but saying something else?** The section with the same heading path exists (or a
   section whose text is at least 0.6 similar to the old one). The `gap_judge` call compares the
   clarification's text with the section's text: same meaning, different meaning, or unrelated.
   Same meaning: adopted by meaning. Different meaning: conflict. Unrelated: go to question 4.
4. **Is the section gone?** No section matches. The answer has lost its place: orphaned.

Normalisation: Unicode NFC, lower case, collapsed whitespace, straight quotes, Markdown emphasis and list
markers removed. Similarity: normalised token-set ratio. The thresholds live in the reconcile settings of
`GapSettings` and are tuned on the test corpus.

## Outcomes

| Outcome | Found by | Amendment | Clarification | Gap | Build reads |
|---|---|---|---|---|---|
| Applied again | Question 1, exact | `active`, `last_revision_id` updated | Unchanged (`applied`) | Unchanged | Revision with the amendment |
| Re-anchored | Question 1, similar line under the same heading | `active`, new `anchor_text` and `anchor_hash` | Unchanged | Unchanged | Revision with the amendment on the new line |
| Adopted literally | Question 2 | `retired` | `absorbed`; `GROUNDED_IN` moves to the owner's fragment after the build | Unchanged | Revision as uploaded |
| Adopted by meaning | Question 3, same meaning | `retired` | `absorbed` | Unchanged | Revision as uploaded |
| Conflict | Question 3, different meaning | `suspended` | `conflict` | A follow-up gap opens | Revision as uploaded |
| Orphaned | Question 4 | `orphaned` | `orphaned` | A follow-up gap opens | Revision as uploaded |

A retired amendment is never applied again. A suspended or orphaned amendment is kept for its follow-up gap
and applied again only when that gap's answer says so.

### Conflict follow-up

Under decision GD3 the owner's newer text wins until someone decides: the build reads the upload, the
amendment is suspended, and the next gap run opens a follow-up gap:

| Property | Value |
|---|---|
| Kind, severity | `contradiction`; the original gap's severity, at least `major` |
| Relations | `FOLLOWS_UP` (reason `conflict`) to the original gap; `ABOUT` the nodes the new text makes, `CITES` the owner's new line |
| Question | "The new upload of <document> changes an earlier answer. Which one is right?" |
| Why | Both texts, who answered and when the answer was given, and the upload |
| Route | `ask`, always |

Its options:

| Option | Edits | Effect |
|---|---|---|
| Keep the upload (recommended when the upload is newer than the answer's build) | None | The original clarification stays `conflict` as history; the follow-up resolves with mode `pointer` to the owner's line |
| Restore the earlier answer | One `replace` of the owner's line with the earlier answer text | A new clarification `REPLACES` the old one; a new amendment anchors on the owner's line |
| Write a new answer | From the person's own text | As any own answer |

### Orphaned follow-up

| Property | Value |
|---|---|
| Kind, severity | The original kind; the original severity |
| Relations | `FOLLOWS_UP` (reason `orphaned`) to the original gap; `ABOUT` the nodes the answer made, while they exist |
| Question | "The section that held the answer to <title> is gone. Where does the answer go now?" |

| Option | Effect |
|---|---|
| Move it to the clarifications document (recommended) | The answer text becomes a section of the clarifications document; the rule stays in the spec |
| Drop it: the section was removed on purpose | No edit; the follow-up resolves; the next build no longer has the answer's nodes, and the plan retires their tasks |
| Place it in another section | A person picks a section; an `insert_after` edit there |

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

A clarification placed in the clarifications document has no anchor in a source; its section is generated
by Specflow and never conflicts with itself. A newer upload can still contradict it: the owner may now state
the rule differently in the source. Two mechanisms catch this:

| Mechanism | What it does |
|---|---|
| Reconcile of the sections the clarification is about | When a lineage changes, reconcile also takes every `document` clarification whose `ABOUT` nodes are evidenced in that lineage, and asks the `gap_judge` the same-meaning question against the changed sections. Different meaning: conflict, as above, and the clarifications document drops the section while the follow-up gap is open (the upload wins) |
| Contradiction detectors | CT-VALUE and CT-CROSS see the generated section and the source as two documents; a contradiction they find is matched to the clarification by its `ABOUT` nodes and becomes the same follow-up |

## Applying amendments

After reconcile, the corpus reader applies every `active` amendment of the lineage to the revision text:

| Rule | Detail |
|---|---|
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
  changed fragment;
- an old file synced again with the same amendments gives the same effective revision id as before, and
  the build sees no change;
- retiring an amendment because the owner adopted it changes the effective revision id, and the build
  re-reads the section, which now says the same in the owner's words.

The `CorpusRevision` the build receives keeps its fields (`lineage_id`, `revision_id`, `version`, `path`,
`title`, `markdown`); only the values of `revision_id` and `markdown` are the effective ones. The build is
unchanged.

## Events

Every outcome is a `gap_event` row on the clarification and its gap: the amendment id, the outcome, the
old and new anchor lines, the similarity, the judge's verdict and confidence, and the revision ids. The gap's
history in the app shows them in order, so a person can see when an answer was adopted, re-anchored or
challenged.

## Download with answers

Amendments are meant to fade into the sources, so Specflow does not stay a second source of truth:

| Feature | Detail |
|---|---|
| Banner | Each document view shows "N answers are not in the source file yet", with the list of active amendments and their gaps |
| Download | `GET /documents/{id}/export?answers=1` returns the effective text (Markdown) of the document's lineage: the newest revision with every active amendment applied, marker lines included |
| Adoption | When the owner pastes the text into their file and the file comes back, reconcile finds the answers (question 2) and retires the amendments; the banner count falls |
| Clarifications document | Downloadable the same way, for owners who want to move the answers into their own documents |

## Edge cases

| Case | Handling |
|---|---|
| Several amendments in one section | Each is reconciled on its own, in anchor order; outcomes may differ (one adopted, one in conflict) |
| Two amendments on the same anchor (an `insert_after` and a `replace` from different answers) | The `replace` is applied first; the `insert_after` anchors on the replaced text's first line; reconcile treats them in the same order |
| Partial adoption: the owner copied one sentence of a two-sentence answer | Question 2 fails (the whole text is not there); question 3 asks the judge, who sees the missing part: different meaning, so conflict; the follow-up's "Restore" option restores the whole answer |
| The whole document deleted (the lineage leaves the corpus) | Every amendment of the lineage becomes `orphaned`; one follow-up gap per clarification |
| A Drive file renamed or moved | The lineage is kept (`drive:<connection id>:<file id>` does not change); reconcile runs as for any revision |
| A Drive file replaced by a new file id, or an upload of the same document as a new file | A new lineage: the old lineage leaves the corpus (all orphaned), and the orphaned follow-ups offer "Place it in another section" with the new document's matching section preselected when a section with the same heading exists |
| The anchor line appears twice in the section | The occurrence nearest to the stored anchor position wins |
| The judge call fails | The amendment is kept `active` but not applied for this build; the outcome is "undecided", and the next build retries; after two failures it becomes a conflict for a person |
| A suspended amendment's follow-up gap is dismissed | The amendment becomes `reverted`; the upload stays what the build reads |
