# Lens: ordering and sequence

Simulate building this system on the assumption that **events do not arrive in
the order they happened**. Specs are written as narratives, so they inherit an
implied sequence that nothing enforces.

Work through the spec asking:

- Which parts of the spec read as "first this, then that"? For each, what
  actually guarantees the order — a transaction, a queue with ordering
  guarantees, a timestamp, or nothing?
- What happens if a later event arrives before an earlier one? A cancellation
  before the booking it cancels; an update for a record not yet created; a
  payment for an order that has not been placed.
- Where are timestamps used to order things? Whose clock produced them? Two
  events from different machines can carry impossible relative times.
- Which operations assume a prior operation completed? Is the precondition
  checked, or assumed? An unchecked precondition is a decision to trust the
  caller.
- If an event arrives that is no longer relevant (superseded, stale, for a
  deleted record), is it dropped, queued, or an error? Silence is a choice.
- For anything batched or scheduled: what happens when a run overlaps the
  previous one because it took longer than the interval?

## What counts as a blocker here

The spec is missing a decision wherever it implies a sequence without stating a
mechanism that enforces it, and wherever an out-of-order arrival has no defined
handling. "Events are processed in order" needs to name what provides that
guarantee, or it is an assumption rather than a requirement.

## Fill particularly carefully

- `state_machines` — every cell where an event fires against a state that should
  logically come later. These are precisely the out-of-order cases, and they are
  the cells most often left blank.
- `operations[].inputs` — note where an input references something that may not
  exist yet.
- `phases` — if the build order matters and the spec does not imply one, your
  decomposition is a hypothesis. Divergence from other lenses on that is a
  signal in itself.
