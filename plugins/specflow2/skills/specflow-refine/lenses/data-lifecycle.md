# Lens: data lifecycle

Simulate building this system for **the second year of its operation**, not the
first day. Specs describe creation; they rarely describe what happens to data
afterwards.

Work through the spec asking:

- For each entity: who creates it, who may change it, and what ends its life?
  Is it deleted, archived, anonymised, or kept forever? "Forever" is a valid
  answer only if someone chose it.
- What happens to records that reference a deleted record? Cascade, orphan,
  refuse the delete, or soft-delete the parent? Every foreign key is one of
  these decisions.
- Which fields are historical and must not change retroactively (the price at
  time of purchase) versus current (today's price)? Mutating a field that
  something historical points at is a common, quiet data bug.
- How does existing data get to the new shape? If the spec changes an entity,
  what happens to rows written under the old rules — backfilled, defaulted, or
  left mixed?
- Is anything unique, and over what window? Unique forever, or unique among
  active records? Reusing an identifier after deletion is a decision.
- What is the retention obligation? If the spec mentions personal data at all,
  deletion and export are requirements, not features.

## The matrix to fill

Name the axes before you answer anything, then put something in every cell.

- **rows** — every entity the spec names, including the ones it mentions only in
  passing as a field on something else.
- **cols** — the events that reach it after creation: *updated*, *the thing it
  references is deleted*, *it is deleted while referenced*, *retention window
  expires*, *a subject asks for export or erasure*, *restored from a backup taken
  before a schema change*.

Each cell says what happens to the data. "Kept forever" is a valid answer only
where someone chose it; if you are inferring it from silence, that is a guess.

A cell you cannot answer is the finding. Write `unanswerable` with the reason, for
example *the spec sets no retention period for this entity and it carries personal
data, so the expiry column has no answer at all*.

## What counts as a blocker here

The spec is missing a decision wherever an entity has no defined end of life, or
a reference has no defined behaviour when its target disappears. These surface
in production months after launch, which is exactly why simulating the build
catches them and reading the happy path does not.

## Decisions to record

Write these into `decisions` even where the spec is silent — that is what makes
another lens's different answer visible. Mark a guess `guessed: true`.

- For every relationship: what happens to the children when the parent is
  deleted?
- For every computed value: is it recalculated or frozen, and does anything
  depend on it staying historically stable?
- What is the retention period, and what does deletion actually mean — removed,
  or flagged?
- Are there delete and export paths at all? Include them even when the spec omits
  them; their absence is the finding.
