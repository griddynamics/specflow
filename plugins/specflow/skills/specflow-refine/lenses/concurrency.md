# Lens: concurrency

Simulate building this system for **two things happening at once**.

Assume every operation can be invoked simultaneously by different actors, and
that no operation is instantaneous. Work through the spec asking:

- Which two operations, run at the same moment on the same record, produce a
  result neither one intended?
- What must be held while a multi-step operation is in flight? For how long?
  What happens to the second caller meanwhile — wait, fail, or proceed?
- Where does the spec assume it is the only writer? Check every read-then-write
  sequence: is the value still true when the write lands?
- Which invariants are stated as if they hold continuously ("stock is never
  negative", "a seat has one holder") and could be violated in the window
  between check and commit?
- What is the unit of atomicity? If a request touches three records, can it
  leave two updated and one not?

## What counts as a blocker here

The spec is missing a decision if you cannot answer, for any contended
operation: *who wins, and what does the loser see?* "The database handles it"
is not an answer — it names a mechanism, not a behaviour.

Note that an invariant the spec states without saying how it is enforced under
contention is a real gap even when the happy path is fully specified.

## Fill particularly carefully

- `state_machines` — the same event arriving twice, and events arriving while
  the entity is mid-transition. These are the matrix cells that get skipped.
- `operations[].idempotent` — decide it for every operation, not just the
  obvious ones.
- `failure_modes` — the scenario where two callers both believed they succeeded.
