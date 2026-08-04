# Lens: partial failure

Simulate building this system on the assumption that **anything can fail halfway
through, including the thing recording the failure**.

Every operation that touches more than one place — two tables, a table and a
queue, a database and a payment provider — can complete some parts and not
others. Work through the spec asking:

- For each multi-step operation, what is the state of the world if it stops
  after step 1? After step 2? Is that state one the system can recognise and
  recover from, or is it silently inconsistent?
- Which external calls can succeed while the caller believes they failed
  (timeout after the remote side committed)? What does the spec say to do when
  you cannot tell whether the money moved?
- What compensates a completed step when a later step fails? Who runs the
  compensation, and what if the compensation itself fails?
- Which failures are retried, how many times, and with what backoff? Which are
  terminal? Retrying a non-idempotent operation is a decision, not a detail.
- What does the user see mid-failure? A spinner that never resolves is a
  specified behaviour if nobody chose otherwise.

## The matrix to fill

Name the axes before you answer anything, then put something in every cell.

- **rows** — every operation that touches more than one place: two tables, a table
  and a queue, a database and an external provider.
- **cols** — where it stopped: *after the first write*, *after the external call
  but before recording it*, *after the external call succeeded but the response was
  lost*, *while writing the failure record itself*.

Each cell describes the state of the world and answers one thing: **can the system
recognise it later, and recover?** A state nothing can detect is worse than a
crash.

A cell you cannot answer is the finding. Write `unanswerable` with the reason, for
example *the spec describes no record of this step having started, so this state is
indistinguishable from never having been attempted*.

## What counts as a blocker here

The spec is missing a decision wherever an operation can leave the system in a
state the spec never names. If a state is reachable and unnamed, no
implementer can handle it consistently — two builds will handle it two ways.

"Roll back the transaction" only closes this when every step is inside the same
transaction. Say so explicitly, or treat it as open.

## Decisions to record

Write these into `decisions` even where the spec is silent — that is what makes
another lens's different answer visible. Mark a guess `guessed: true`.

- For every reachable bad state: what does the spec say happens? Record "nothing"
  honestly when it says nothing — that is the finding, and it deserves a blocker.
- What intermediate states do real failures create (`pending_confirmation`,
  `partially_applied`) that the spec never names?
- Who cleans up a half-finished operation, and when?
- What does the caller see, and what can they safely retry?
