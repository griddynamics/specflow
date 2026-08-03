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

## What counts as a blocker here

The spec is missing a decision wherever an entity has no defined end of life, or
a reference has no defined behaviour when its target disappears. These surface
in production months after launch, which is exactly why simulating the build
catches them and reading the happy path does not.

## Fill particularly carefully

- `entities[].fields[].references` — set it wherever a relationship exists, and
  raise a blocker for each one whose delete behaviour the spec does not state.
- `entities[].fields[].derived` — flag anything computed. A derived field that
  must also be historically stable is a contradiction worth naming.
- `operations` — include the delete and export paths even when the spec omits
  them; their absence is the finding.
