---
name: specflow-resolve
description: Walk through open specification decisions from a refinement round, write answers back into the spec files, and record exact blocker IDs for traceability.
argument-hint: "(optional) spec_dir outputs_dir — defaults: specs docs"
---

# SpecFlow Resolve

Take the open decisions from a refinement round, settle them with the user, and
**write the answers into the specification**.

That last part is the job. A loop that only reports blockers leaves all the work
with the user; the value is a spec that no longer has the hole.

## What to do

### 1. Read the open decisions

```bash
specflow refine status --outputs <outputs_dir> --json
```

That gives you the open blockers with attribution — which lenses raised each, and
where the readings disagreed. If the list is empty, say so and stop; do not
manufacture questions.

**Sorting these is your judgment, not the CLI's.** There is no ranking to defer
to, on purpose: a weighted score would have been invented numbers dressed up as
measurement. Decide it yourself, using:

- how expensive a wrong guess is to undo — architecture, data model, and security
  boundaries are the costly ones,
- whether the lenses disagreed, which is evidence the spec genuinely leaves it
  open rather than one agent being pedantic,
- how many independent lenses raised it.

Say your reasoning out loud when you present the list, so the user can push back
on your ordering rather than trusting it.

### 2. Handle the cheap ones without asking

Where a choice is genuinely reversible and low-impact, apply the recommendation,
record it, and mention it in your summary as a batch. Do not put these to the
user one by one.

```bash
specflow refine resolve --outputs <outputs_dir> --id <blocker-id> --choice "<option label>" --source assumed
```

### 3. Ask about the rest

Use `AskUserQuestion`. Batch related decisions into one call rather than a
sequence of prompts.

For each question, give:

- the **scenario** — the concrete situation that forces the decision,
- the **options** with their consequences, taken from the blocker,
- your **recommendation** as the first option, marked as such.

Rules that matter:

- **One line to answer.** If a question needs a paragraph of setup, the analysis
  is incomplete — say so rather than passing the confusion on.
- **Never show a score.** "Five of six independent readings hit this" is useful
  and true. A ratio or a readiness percentage is not — nothing here is calibrated,
  and there is no metric in this product to quote.
- **Offer the default.** "I'll assume X unless you'd rather Y" is usually cheaper
  for the user than an open question, and it is honest as long as you actually
  apply X.

### 4. Write the decision into the spec

This is the step that changes something. For each resolved decision:

- **Edit the spec file named in the blocker's `where`.** Add or amend the
  requirement so the ambiguity is gone. Write in the surrounding document's voice
  and format — match its heading style, numbering, and level of formality.
- **Never delete or rewrite the user's prose to make room.** Extend it. If the
  existing text is wrong rather than incomplete, point that out and ask before
  changing it.
- **Keep traceability.** Record which decision the edit implements, using
  whatever convention the spec already uses for requirement ids. If it has none,
  a short trailing marker is fine — but ask before introducing a new convention
  into someone's document.

Then record it, listing the files you actually changed:

```bash
specflow refine resolve --outputs <outputs_dir> --id <blocker-id> --choice "<option label>" \
  --applied-to specs/orders.md --source user
```

Recording suppresses that exact blocker id in current and later findings. Because
independent agents may give a semantically repeated lens-only finding a different
id, also pass `resolutions.json` to later lenses as `/specflow-refine` instructs;
the model must recognize the prior decision when exact identity is unavailable.
The CLI deliberately does not guess that two differently worded findings are the
same.

### 5. Report

Say what was decided, what you assumed, and which spec files changed. Then
suggest re-running `/specflow-refine` — the spec has changed, so a fresh round of
independent lenses may find something the previous one could not see past.

## When a decision reveals a bigger problem

Sometimes an answer invalidates part of the spec rather than completing it — the
user says "actually we don't do reservations at all". Do not quietly restructure
the document. Say what the answer implies, name the sections it affects, and let
the user decide the scope of the rewrite. Refining a spec is not licence to
redesign the product.
