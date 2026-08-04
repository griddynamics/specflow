# Testing the refinement loop

Four levels, cheapest first. They test different things, and only the last one
tests the product hypothesis — so read level 3 before concluding that a green
level 0–2 means the loop works.

| Level | What it proves | Cost |
|---|---|---|
| 0 — unit tests | the comparison is correct | seconds |
| 1 — CLI by hand | the plumbing works, with no model involved | ~5 minutes |
| 2 — the full loop | six lenses, a grid, coherence, a second round | a real run |
| 3 — planted ambiguity | **that the loop finds spec defects at all** | a real run + a spec you know |

Levels 0 and 1 are deterministic and belong in every change. Levels 2 and 3
involve a model, so they are observations rather than tests, and each one needs a
human to read the result.

There is no level for "did it converge". The loop has no stop rule and reports no
verdict — see the plugin README for why that inference was removed rather than
fixed.

---

## Level 0 — the unit tests

```bash
make unit-tests
```

That runs the backend suite and then the MCP-server suite. For just this feature:

```bash
cd mcp_server && uv run pytest tests/test_refine.py tests/test_refine_commands.py -v
```

- `tests/test_refine.py` — comparison: what counts as a disagreement, how blockers
  merge, grid coverage, coherence attribution, and how malformed readings are
  handled.
- `tests/test_refine_commands.py` — the command layer: exit codes, the payload keys
  the skills read, `--root-path` resolution, and (in `TestNoStopRule`) that no
  convergence signal has crept back in.

There is deliberately no test that a reading is "complete", that a spec is "ready",
or that a round converged. Those are judgments the skill makes out loud; a test
asserting them would only pin down an arbitrary threshold.

---

## Level 1 — drive the CLI by hand, with no model

**This is the level worth knowing.** It separates the deterministic half of the
product from the model half: if a round looks wrong, running it here tells you
whether the CLI or the subagents produced the wrong answer.

Everything below is copy-pasteable, in bash or zsh. From a repo checkout the CLI is
`uv run python -m cli` from inside `mcp_server/`; installed from PyPI it is
`specflow`. The examples call `sf`, which you define as whichever you have — a
function rather than a variable, because zsh does not word-split `$SF`.

### Set up a throwaway project

```bash
export DEMO=$(mktemp -d)
mkdir -p "$DEMO/specs" "$DEMO/docs"
cat > "$DEMO/specs/booking.md" <<'EOF'
# Booking

## Holds
A user may hold a seat while completing payment. A hold has a timer.
Payment is taken after the hold is confirmed.
EOF

# from a checkout:
cd mcp_server && sf() { uv run python -m cli "$@"; }
# or, installed:  sf() { specflow "$@"; }
```

That spec is deliberately underdetermined. It never says what happens when the
timer expires, what the losing caller sees on a contended seat, or which wins when
expiry and payment settlement race.

### Allocate a round

```bash
sf --root-path "$DEMO" refine new-round --outputs docs --lens concurrency ordering
```

It prints the round directory and the exact filename each lens must write. Note
that `--root-path` is a **global** flag, so it comes before `refine`.

### Write the grid

Normally one subagent writes this. By hand:

```bash
R="$DEMO/docs/refine/round-01"
cat > "$R/grid.json" <<'EOF'
{
  "cells": [
    {"id": "hold.timeout", "question": "A hold's timer expires — what happens to the seat?", "where": "specs/booking.md#Holds"},
    {"id": "hold.timeout.paid", "question": "The timer expires while payment is in flight — what happens?", "where": "specs/booking.md#Holds"},
    {"id": "hold.contended", "question": "Two users hold the same seat — what does the loser see?", "where": "specs/booking.md#Holds"}
  ]
}
EOF
```

### Write two readings that disagree

```bash
cat > "$R/reading.concurrency.json" <<'EOF'
{
  "lens": "concurrency",
  "cells": [
    {"id": "hold.timeout", "value": "seat returns to the pool", "guessed": true},
    {"id": "hold.contended", "value": "rejected with 409", "guessed": true}
  ],
  "decisions": [
    {"question": "Does the hold expire before the payment settles?", "value": "yes",
     "where": "specs/booking.md#Holds", "guessed": true}
  ],
  "blockers": [
    {"id": "contended-seat-loser", "title": "What the losing caller sees",
     "question": "Reject with 409, or queue?", "where": "specs/booking.md#Holds",
     "options": [{"label": "reject-409", "consequence": "caller retries"},
                 {"label": "queue", "consequence": "unbounded wait"}],
     "recommended": "reject-409", "impact": "changes_behaviour", "reversible": false}
  ]
}
EOF

cat > "$R/reading.ordering.json" <<'EOF'
{
  "lens": "ordering",
  "cells": [
    {"id": "hold.timeout", "value": "seat stays held until payment resolves", "guessed": true}
  ],
  "decisions": [
    {"question": "does a hold expire before payment settles", "value": "no",
     "where": "specs/booking.md#Holds", "guessed": true}
  ],
  "blockers": []
}
EOF
```

Note the two readings word the same question differently and answer it opposite
ways. That is the signal the whole design rests on.

### Optionally, the coherence pass

```bash
cat > "$R/coherence.json" <<'EOF'
{
  "blockers": [
    {"id": "timeout-races-payment",
     "title": "Expiry and payment settlement have no defined order",
     "question": "Which wins when both fire?", "where": "specs/booking.md#Holds",
     "options": [{"label": "expiry-wins", "consequence": "paid user loses the seat"},
                 {"label": "payment-wins", "consequence": "hold outlives its timer"}],
     "recommended": "payment-wins", "impact": "changes_architecture", "reversible": false}
  ]
}
EOF
```

### Compare

```bash
sf --root-path "$DEMO" refine round --outputs docs
```

Six things in that output are worth checking, because each is a behaviour that has
been wrong at some point:

1. **`2 lenses: concurrency, ordering`** — the count is of readings that could be
   compared, not files on disk. Break one file and it becomes `1 of 2 readings
   compared`.
2. **The differently worded question is one disagreement**, not two, and the
   answer from each lens carries its own phrasing (`asked as: …`).
3. **Three open blockers, not one** — the cell disagreement attaches to the
   blocker at the same location as evidence; the prose disagreement, which has no
   blocker of its own there, becomes one. N gaps at one location stay N items.
4. **`hold.timeout.paid` is listed as answered by nobody.** A cell no reading
   reached never shows up as a disagreement — this is the only place it appears.
5. **`hold.contended` is listed as agreed-but-guessed.** Consensus over a silent
   spec is not evidence.
6. **The coherence blocker is attributed to `coherence`** and is *not* counted as
   a third lens.

### Re-run it

```bash
sf --root-path "$DEMO" refine round --outputs docs --json > /tmp/a.json
sf --root-path "$DEMO" refine round --outputs docs --json > /tmp/b.json
diff /tmp/a.json /tmp/b.json && echo "identical"
```

Byte-identical. Re-running a round replaces its findings and keeps no history of
having been run, so nothing about the *number of times you ran it* can leak into
what it reports.

### Record a decision

```bash
sf --root-path "$DEMO" refine resolve --outputs docs \
    --id timeout-races-payment --choice payment-wins \
    --applied-to specs/booking.md --source user
sf --root-path "$DEMO" refine status --outputs docs
```

`Open blockers` drops to 2 immediately, without re-running the round. Running the
same `resolve` twice is refused with exit code 2.

### Check the failure paths

None of these may produce a traceback. Two of them exit `2` with a message naming
the file; the middle one deliberately does not.

```bash
# no such round → exit 2
sf --root-path "$DEMO" refine round --outputs docs --round 9; echo "exit=$?"

# one broken reading → exit 0, "1 of 2 readings compared", the other lens still counted
echo '{not json' > "$R/reading.ordering.json"
sf --root-path "$DEMO" refine round --outputs docs; echo "exit=$?"

# broken grid → exit 2
echo '{"cells": 1}' > "$R/grid.json"
sf --root-path "$DEMO" refine round --outputs docs; echo "exit=$?"
```

The asymmetry is deliberate. A malformed **reading** — bad JSON or the wrong
container shape — is reported under `Readings that could not be compared` and the
round continues, because six subagents write those concurrently and one bad file
must not throw away five good ones. Only a round where *nothing* is comparable
refuses. A malformed **grid** refuses outright, because the grid is the exam every
lens sat: quietly treating a broken one as absent would report a round with no
coverage as a round with nothing to cover.

### Tear down

```bash
rm -rf "$DEMO"
```

---

## Level 2 — the full loop

```
/specflow-refine specs docs
```

What to look at afterwards, in order:

1. **`docs/refine/round-01/` — did every lens write a file?** A missing file is a
   subagent that failed, and the round saw proportionally less. The output says
   `N of M readings compared` when they differ.
2. **Are `guessed: true` markers present in the readings?** A reading with no
   guesses on an underdetermined spec is a lens that filled in confident
   inventions, and every count downstream inherits that.
3. **Do the blockers carry a `where` pointing at a real section?** Location is what
   groups a disagreement with the blocker it belongs to; a vague `where` degrades
   the grouping.
4. **The `Worth knowing about this round` section.** Two files claiming the same
   lens name means the fan-out sent one lens twice, so there are fewer independent
   readings than the header suggests.
5. **Did the lenses actually diverge?** Zero disagreements across six lenses on a
   real spec is more likely a fan-out that leaked shared context than a spec that
   is unambiguous. Check that no subagent prompt contained another's output.
6. **Round two.** Run it after resolving something substantial. It should reach
   questions that exist *because* of how you resolved round one; if it returns the
   same blockers unchanged, check that the resolutions were recorded (they drop out
   of `refine status` when they were).

Independence is the load-bearing claim and **nothing in the CLI can check it** — a
round where six subagents shared context produces artifacts identical to a round
where they did not. It is guaranteed only by the skill's prose, and the only way
to verify it is to read the subagent prompts.

---

## Level 3 — does it find anything real?

Levels 0–2 all test whether the machinery runs. None of them tests the product
hypothesis: **that disagreement between independent readings tracks real spec
defects.** That is unproven, and it is the thing worth measuring.

The procedure, which needs no new code:

1. **Take a spec you consider unambiguous** — ideally one you have already built
   from, so you know where the real gaps were.
2. **Run the loop and record what it finds.** These are your false positives:
   findings on a spec you believe is determinate.
3. **Plant one ambiguity.** Delete a sentence that settles something, or change
   one requirement so it contradicts another elsewhere. Write down exactly what
   you changed and where.
4. **Run the loop again on the mutated spec.**
5. **Score two things separately:**
   - **Detected** — did any blocker or disagreement correspond to the planted
     defect?
   - **Localized** — did it point at the requirement you actually mutated, or
     somewhere adjacent? A finding that says "the spec is unclear about holds" for
     a mutation in the payment section is a miss dressed as a hit.
6. **Repeat with a mutation of a different class** — a removed constraint, a
   contradiction between two files, an unstated ordering assumption. The lenses
   are built around failure classes, so per-class detection is what matters, not
   an overall rate.

Until step 5 has been run several times, the honest claim about this loop is "it
surfaces places independent readings diverged", not "it finds spec defects". The
`plans/specflow-2.0/` plan calls this gate P7 and says nothing should be deleted
until it is green; see `plans/specflow-2.0/status.md` for what has and has not
been done against that plan.

---

## What none of these levels test

- **Anything that only appears when code runs.** Six agents reading a spec is not
  building from it. Integration defects, performance, and anything that depends on
  a library actually behaving as documented are all outside what this can see.
- **Whether a resolution was any good.** The loop records the decision you made;
  nothing checks it was the right one.
- **Cost.** Lens count is the cost dial and nothing measures it. If you care,
  record token spend per round yourself — the plan's P2 gate asked for that
  measurement and it has not been taken.
