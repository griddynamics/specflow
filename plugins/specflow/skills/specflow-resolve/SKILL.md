---
name: specflow-resolve
description: Walk through the open specification decisions from a refinement round and write the answers back into the spec files, with traceability. Records each decision so later rounds stop asking.
argument-hint: "(optional) spec_dir outputs_dir — defaults: specs docs"
---

# SpecFlow Resolve

Take the ranked decisions from a refinement round, settle them with the user, and
**write the answers into the specification**.

That last part is the job. A loop that only reports blockers leaves all the work
with the user; the value is a spec that no longer has the hole.

```bash
SF="${CLAUDE_PLUGIN_ROOT:-$(pwd)/plugins/specflow}/lib/specflow_cli.py"
```

## What to do

### 1. Read the open decisions

```bash
python3 "$SF" status --outputs docs --json
```

The `ask` list is what needs a human. If it is empty, say so and stop — do not
manufacture questions.

Work in ranked order. The ranking already accounts for how far a wrong choice
propagates and how many independent lenses raised it.

### 2. Handle the cheap ones without asking

Anything the round classified `assume` is reversible and low-impact. Apply the
recommendation, record it, and mention it in your summary as a batch. Do not put
these to the user one by one.

```bash
python3 "$SF" resolve --outputs docs --id <blocker-id> --choice "<option label>" --source assumed
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
- **Never show a score.** "Five of six independent readings hit this" is useful.
  "Concordance 0.83" is not.
- **Offer the default.** "I'll assume X unless you'd rather Y" is usually cheaper
  for the user than an open question, and it is honest as long as you actually
  apply X.

### 4. Write the decision into the spec

This is the step that changes something. For each resolved decision:

- **Edit the spec file named in the blocker's `spec_anchor`.** Add or amend the
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
python3 "$SF" resolve --outputs docs --id <blocker-id> --choice "<option label>" \
  --applied-to specs/orders.md --source user
```

Recording is what stops the next round re-asking. Skip it and the loop will never
converge.

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
