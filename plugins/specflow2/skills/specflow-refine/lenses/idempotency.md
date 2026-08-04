# Lens: idempotency and replay

Simulate building this system on the assumption that **every message arrives at
least once, and sometimes more than once**. Networks retry, users double-click,
queues redeliver, and clients resend after a timeout they could not interpret.

Work through the spec asking:

- For each operation: what happens if it runs twice with identical input? Twice
  is the minimum — assume it can run five times.
- Where does the caller supply an idempotency key, and where must the system
  derive one? If neither, the operation is not safe to retry, and the spec
  should say retries are forbidden.
- Which effects are not naturally idempotent — charging a card, sending an
  email, incrementing a counter, appending to a log? Each needs an explicit
  dedup story.
- How long is a duplicate recognised as a duplicate? A dedup window is a
  decision with a number in it; if the spec has no number, that is the gap.
- What does the second caller receive — the original result, a conflict error,
  or a fresh execution? Returning the original result requires storing it.
- Is the *response* replayable? A caller that retried because it lost the
  response needs the same answer, not a "already done" error it cannot act on.

## The matrix to fill

Name the axes before you answer anything, then put something in every cell.

- **rows** — every operation the spec describes that changes something. Reads too,
  where a read has a side effect such as issuing a token or consuming a quota.
- **cols** — the replay conditions: *same input twice*, *same key with a different
  payload*, *retried after a timeout where the caller cannot know if it landed*,
  *replayed after a later operation already happened*.

Each cell says what the second and fifth attempt do — not just the second. Assume
five.

A cell you cannot answer is the finding. Write `unanswerable` with the reason, for
example *the spec names no key for this operation and no natural one exists in its
input, so nothing can tell a retry from a new request*.

## What counts as a blocker here

The spec is missing a decision for every operation whose second execution is
observably different from its first and which the spec does not mark as
non-retryable. This class of defect is invisible in a happy-path read and
routinely reaches production.

## Decisions to record

Write these into `decisions` even where the spec is silent — that is what makes
another lens's different answer visible. Mark a guess `guessed: true`.

- For every operation: is calling it twice with the same input safe, and if so
  what makes it safe — a key, a version, a natural uniqueness?
- What happens when an event arrives against a state that already consumed it?
- On a retry that succeeded the first time invisibly, what does the caller see?
- Which side effects are not replayable — mail, payment, external calls?
