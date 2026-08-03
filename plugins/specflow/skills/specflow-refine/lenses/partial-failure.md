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

## What counts as a blocker here

The spec is missing a decision wherever an operation can leave the system in a
state the spec never names. If a state is reachable and unnamed, no
implementer can handle it consistently — two builds will handle it two ways.

"Roll back the transaction" only closes this when every step is inside the same
transaction. Say so explicitly, or treat it as open.

## Fill particularly carefully

- `failure_modes` — this is your lens's primary output. One entry per reachable
  bad state, with `spec_says` set honestly. `"nothing"` is the correct value
  when the spec is silent, and it obliges you to raise a matching blocker.
- `state_machines` — add the intermediate states real failures create
  (`pending_confirmation`, `partially_applied`). If the spec only names the
  clean states, that itself is the finding.
