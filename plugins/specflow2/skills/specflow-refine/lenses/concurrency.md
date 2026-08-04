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

## The matrix to fill

Name the axes before you answer anything, then put something in every cell.

- **rows** — every resource the spec says can be held, reserved, decremented or
  claimed. Take the names from the spec, not from your own model of it.
- **cols** — every operation that can reach one of those resources while another
  operation already has it. Include the operation colliding with *itself*.

Each cell answers one question: **who wins, and what does the loser see?** "The
database handles it" is not a cell value — it names a mechanism, not a behaviour.

A cell you cannot answer is the finding. Write `unanswerable` with the reason in
one line, for example *the spec never says who owns the timer, so two owners give
two different answers here*.

## What counts as a blocker here

The spec is missing a decision if you cannot answer, for any contended
operation: *who wins, and what does the loser see?* "The database handles it"
is not an answer — it names a mechanism, not a behaviour.

Note that an invariant the spec states without saying how it is enforced under
contention is a real gap even when the happy path is fully specified.

## Decisions to record

Write these into `decisions` even where the spec is silent — that is what makes
another lens's different answer visible. Mark a guess `guessed: true`.

- For every contended operation: *who wins, and what does the loser see?*
- For every operation: is running it twice at once safe?
- For each stated invariant: what enforces it in the window between check and
  commit?
- What is the unit of atomicity when one request touches several records?

Raise a blocker where you would not be willing to pick for the user. An invariant
the spec states without saying how it holds under contention is a real gap even
when the happy path is fully specified.
