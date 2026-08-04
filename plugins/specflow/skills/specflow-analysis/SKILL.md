---
name: specflow-analysis
description: Analyze spec completeness locally — gap detection across every architectural dimension. Emits a human report plus a machine-checkable dimensions file. No backend, nothing leaves the machine, repeatable as specs evolve.
argument-hint: "(optional) spec_dir outputs_dir src_dir — defaults: specs docs src"
---

# SpecFlow Analysis

You are a software architect analyzing whether a specification determines enough
to build from. Read everything in `spec_dir`, then lock every architectural
dimension to exactly one value — and where the spec does not determine one, say
so rather than choosing quietly.

## Arguments

- `spec_dir` — default `specs`
- `outputs_dir` — default `docs`
- `src_dir` — existing code, if any. Default `src`.

## The dimensions to lock

Work through these. They are the decisions that must be settled before anyone can
build, and the ones that silently diverge between two independent readings of the
same spec.

- **Universal** — persistence, infrastructure complexity, scale target, technology
  stack, quality level, scope boundaries. Every project has all six.
- **Technology-specific** — whatever the chosen stack forces a decision about.
- **Project-specific** — the ones you discover that no checklist anticipated.
  These are often the most valuable part of an analysis, so do not stop at the
  list above.
- **Consistency locks** — naming conventions and code patterns: file and
  identifier casing, database and API path conventions, error handling,
  validation boundary, async style, config source.

This is a prompt for your attention, not a form to complete. Nothing checks that
you filled every slot, because a filled slot proves nothing about whether the
reading is any good — and a checklist you can satisfy is a checklist you can
satisfy without thinking. Judge whether you have understood the spec, and say
what you are unsure of.

## What to do

### 1. Read the spec properly

If the spec tree is large, build an index first — file, purpose, and the
requirements each file carries — and write it to
`<outputs_dir>/analysis/specification_index.md`. Work from the index rather than
re-reading everything repeatedly.

Read `src_dir` if it exists. Existing code is evidence about intent and sometimes
settles a dimension the prose leaves open. Where code and spec disagree, that is
itself a finding.

### 2. Lock every dimension

For each dimension, record the value **and where it came from** — the file, and
the section if you can name one.

When the spec does not determine a value, say so directly: name the gap, say what
you would assume, and say what it would cost to assume wrong. An unresolved
dimension stated plainly is a finding. The same dimension filled in with `TBD`,
`unknown`, `varies`, or `N/A` is worse than either — it looks answered while
telling the reader nothing.

### 3. Write the report

**`<outputs_dir>/analysis/specification_completeness.md`**:

- what the spec determines, dimension by dimension, with where you read it,
- what it does not, with the specific requirement each gap belongs to,
- contradictions, where two parts of the spec cannot both hold,
- what you would need to ask to close each gap.

Prose, because this is for a human to read and argue with. There is no machine
format here and nothing scores it — a second file in a schema's shape would only
be checkable for *shape*, which was never the question worth answering.

## Report honestly

Lead with what is missing, not with what is present — the gaps are why the user
ran this.

Be specific about each one: which requirement, what decision is absent, what
would settle it. "The spec is vague about persistence" is not useful.
"Requirement 3.2 assumes records survive a restart but no storage is named" is.

Say what this skill does **not** do. It reads the spec; it does not simulate
building from it. So it finds the gaps visible on careful reading and misses the
ones that only appear when everything has to be made concrete at once. If the
spec looks broadly complete here, `/specflow-refine` is the next step — it runs
independent readings against each other and finds what a single careful pass
cannot.
