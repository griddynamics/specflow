---
name: specflow-refine
description: Refine a specification before you build it. Independent subagents each read the spec under a different adversarial lens; where they disagree, the spec is ambiguous. You resolve the decisions that matter and the answers are written back into the spec. Runs entirely locally; no backend, nothing leaves the machine.
argument-hint: "(optional) spec_dir outputs_dir — defaults: specs docs"
---

# SpecFlow Refine

You are orchestrating a specification refinement loop. Several subagents each
read the same spec under one adversarial lens, with no knowledge of each other.
Where independent readings land on different answers, the spec did not determine
the answer — that is the finding.

**You do not write code and you do not commit anything.** The point is to find
what the spec fails to determine, before anyone spends days building one arbitrary
reading of it.

## Arguments

- `spec_dir` — specification root. Default `specs`.
- `outputs_dir` — where artifacts are written. Default `docs`.

Check the CLI is reachable once, at the start:

```bash
specflow refine --help >/dev/null
```

If it is missing, tell the user to install it and stop:

```
uv tool install gd-specflow
```

---

## What the CLI does, and what you do

Read this before running anything. Getting it backwards is the main way this loop
goes wrong.

**The CLI compares and remembers.** It groups answers to the same question across
lenses, tells you which differ, merges blockers by id so you can see how many
lenses independently raised each, and diffs this round against every previous one.
That is list work over more items than you can track reliably by eye — you *will*
lose the fourteenth of twenty.

**You judge.** Run this skill on a best-in-class model — the calls below are
yours, not the CLI's. Is this spec good enough to build from? Is this architecture
coherent? Is this decision worth interrupting the user over? Has the loop found
everything worth finding? None of that is checkable by a script, and there is
deliberately no gate, no score, and no completeness check pretending otherwise.

So: never say "the validator confirmed the spec is ready" — nothing validates
that. Say what you concluded and why, and let the user disagree with you.

**Independence is the one rule you must not weaken.** Subagents never see each
other's output, and there is no shared plan. Two lenses reaching different
conclusions from the same spec is the entire signal; shared context destroys it.

The rule that makes the grid below legal: **share the questions, never the
answers.** Every lens may know which cells exist, because that came from the
spec. No lens may know what another put in one.

---

## What replaces building

The predecessor to this loop found gaps by building the system — hours of it.
That worked because code will not compile half a decision: every field needs a
type, every branch a body, every error path a return. Reading a spec forces
nothing, and an agent slides past a gap without noticing.

Four things below restore that forcing, at a fraction of the cost. They are listed
in order of how much they recover:

- **The per-lens matrix** (§ the reading format) is the strongest, and the reason
  the loop reaches decisions nobody knew to ask about. Each lens names its own rows
  and columns *before* answering anything, then must put something in every
  intersection. A cell exists whether or not anyone wants to answer it — that is
  the property a compiler had and prose does not. Six lenses declare six different
  cross-products, so a case invisible from one angle is often forced by another.
- **The grid** (step 2) does the same over one list, shared by every lens, which is
  what makes its cells comparable *across* lenses. Narrower than the matrices —
  one author, one set of blind spots — but it is the only thing here with a
  denominator you can count against.
- **The coherence pass** (step 4) asks whether the answers can all be true at
  once. Disagreement finds gaps; it never finds two lenses that agreed and were
  jointly wrong. Building found those when the code did not run.
- **The next round** (step 7) is seeded with your resolutions, so it reaches the
  gaps that exist *because* of how you resolved the last one. That is the
  dependency ordering a build gets for free.

**What none of them restore is execution.** A matrix forces the *question* to
exist; only running code proves an answer wrong. So this loop cannot find what
appears when data actually flows, and it cannot show that a decision is impossible
— a lens can argue two requirements conflict, but nothing here fails to compile.
See step 7 for the one place you should still spend half an hour building.

---

## The loop

### Step 1 — allocate a round

```bash
specflow refine new-round --outputs <outputs_dir> --lens concurrency partial-failure data-lifecycle auth-boundaries idempotency ordering
```

Note the round number and directory it prints, along with the paths for the grid
and the coherence file.

Six lenses is the default. Fewer is cheaper and finds less; more costs
proportionally. This is the cost dial — start with three on a first pass if the
spec is large.

### Step 2 — sketch the grid

Spawn **one** subagent to enumerate the decisions the spec implies, and to answer
none of them. It writes `<round dir>/grid.json`:

```json
{
  "cells": [
    {
      "id": "hold.timeout",
      "question": "A seat hold is open and its timer expires — what happens?",
      "where": "specs/booking.md#Holds"
    }
  ]
}
```

Cells come from the spec's own vocabulary, not from any lens:

- every entity the spec names × every event that can reach it,
- every role × every protected resource × every action,
- every stored field that can be absent, expire, or change.

Aim for a few dozen. If the spec implies hundreds, narrow this round to a
subsystem and say which one — a grid too large to fill is one nobody fills.

Two rules make this worth doing. **The enumerator must not answer** — a cell
carrying a suggested value contaminates every lens that reads it. And **the grid
is written once, for all lenses**; a lens that picks its own cells has scoped its
own exam, which is exactly how the deleted completeness gate failed.

Skipping this step is allowed. The loop then works as it did before, and finds
less.

### Step 3 — fan out

Read each lens file from `lenses/` in this skill's directory. Then spawn **one
subagent per lens, all in a single message** so they run concurrently.

Give each subagent:

- the full contents of its lens file,
- the spec directory to read,
- the grid from step 2, with the instruction to fill every cell its lens has a
  view on and to leave the rest alone,
- the reading format below,
- **the three-pass instruction below, verbatim**,
- the exact output path: `<round dir>/reading.<lens>.json`.

**Tell every lens to work in three passes, in this order.** The order is what
makes the decisions appear at all; a lens that answers as it goes only ever finds
what it already thought to look for.

> **1. Enumerate, answer nothing.** Name your matrix axes first — the rows and
> columns your lens attacks along, from the lens file. Write the axes down before
> you know a single answer. If you answer while enumerating you will only list the
> cases you can already handle, which is the exact failure this pass exists to
> prevent.
>
> **2. Fill every intersection.** Go cell by cell. Say what the system does. Where
> the spec did not tell you, answer anyway and mark it `guessed: true`. Where you
> genuinely cannot answer, write `unanswerable` with one line on why — that is a
> finding, not a failure, and it is the most useful thing you can produce.
>
> **3. Re-read your own matrix and account for every cell.** Any cell still empty
> is one you enumerated and walked away from: go back and either answer it, or say
> why it cannot be answered. Then ask of your filled cells: *which of these did I
> assert more confidently than the spec justifies?* Downgrade those to `guessed`,
> and promote to a blocker any guess that would be expensive to get wrong.

**Never tell a subagent what another lens found, and never pass it a plan.** Each
one reads the spec cold. If a previous round produced resolutions, pass
`<outputs_dir>/refine/resolutions.json` — those are now part of the spec's
meaning, so all lenses may see them equally.

**Which model runs a lens.** SpecFlow does not choose — a subagent inherits
whatever your harness gives it. A lens needs a general-purpose model or better;
small and cheap under-reports, agreeing with the spec it was sent to attack. If
your harness can give different subagents different models, spread the lenses
across model families — one model disagrees with itself less than several do.

### Step 4 — check coherence

Once every lens has written its reading, spawn **one** subagent over the whole
round directory. Its question is not "is anything missing" but:

> Take these answers as given. Which two of them cannot both be true?

It is looking for the failure disagreement cannot see — a locking rule that makes
the retry policy unreachable, a retention window shorter than the dispute window,
an idempotency key that does not survive the partition the ordering lens assumed.
Building caught these when the code did not run. Nothing else here does.

It writes `<round dir>/coherence.json`, blockers only, same format as below:

```json
{ "blockers": [ { "id": "locking-blocks-retry", "...": "..." } ] }
```

This pass reads every lens's output, so it is **not** an independent reading. It
runs after the lenses are done, never before, and it never contributes decisions
— only blockers. The CLI enforces the second half of that: coherence blockers are
attributed to `coherence` and left out of the lens count.

Skipping it is allowed and costs you this class of finding entirely.

### Step 5 — compare

```bash
specflow refine round --outputs <outputs_dir>
```

This prints where the readings disagree, the merged blockers with attribution —
which lenses independently raised each, and which you have already decided — and,
if there was a grid, which cells no lens answered and which were answered by every
lens guessing. It writes `findings.json`, which `refine status` reads back.

It passes no verdict. There is no score, no readiness call, and no signal that the
loop is done; see step 7.

Read those last two carefully. **A cell nobody answered is a gap so complete that
no reading even reached it**, and it will never appear as a disagreement. **A cell
every lens guessed at is agreement that is not evidence** — the spec was silent
and they converged anyway. Both are findings; neither is a disagreement.

**Then read the matrix section, and treat its two lines differently.**

- **`spec cannot say`** — a lens reached a cell in its own cross-product and
  reported that the spec cannot answer it, with a reason. These are the decisions
  that only exist because something forced the question, and they are the closest
  this loop gets to what building used to surface. Promote the expensive ones to
  the user in step 6 even when no other lens mentioned them; a single lens is weak
  evidence for a *judgment*, but "this intersection has no answer" is a fact about
  the spec, not an opinion about it.
- **`never filled`** — a lens enumerated a cell and never came back to it. That is
  a hole in the reading, not in the spec. Re-run that lens before you conclude
  anything about the area it covers, and do not report those cells to the user as
  findings.

**Check what the round actually saw before you read anything into it.** Two
sections of the output decide how much the rest is worth:

- **`Readings that could not be compared`** — a lens whose file is missing or
  malformed did not participate. The headline says `N of M readings compared`
  when they differ. Re-run that lens before treating the round as a full one; the
  gaps it would have found are simply absent, not shown to be absent.
- **`Worth knowing about this round`** — a lens that answered one question two
  ways, or two files claiming the same lens name (which means the fan-out sent
  one lens twice, and you have fewer independent readings than you think).

If a command exits non-zero it tells you which file is wrong. Fix the file and
re-run; re-running a round simply replaces its findings.

### Step 6 — decide what reaches the user

The command does **not** sort blockers into ask/assume for you. That is your
judgment, and here is how to make it:

- **Ask** when a wrong guess is expensive to undo — it changes the architecture,
  or the data model, or a security boundary. Also ask when the lenses disagreed,
  because that is evidence the spec genuinely underdetermines it.
- **Assume** when the choice is cheap to reverse. Apply the recommendation,
  record it with `--source assumed`, and tell the user in a batch afterwards.
- **Drop** a lone cosmetic nitpick. One lens being pedantic is not a finding.

Use the blocker's own `impact` and `reversible` fields as input, not as gospel —
a subagent that marked everything `blocks_build` was being defensive, and you
should say so rather than flooding the user.

Then hand off to `/specflow-resolve`, or handle it here with `AskUserQuestion`.

### Step 7 — loop or stop

**Nothing tells you when to stop. That is deliberate, and it is your call.**

There used to be a signal here — the command diffed each round against the
previous ones and reported what was new. It was removed, because "this round
raised nothing new" has two causes that leave identical artifacts: the spec has
nothing left to give, or *this round's lenses found less*. Nothing holds lens
effort constant between rounds, so the second reading is always available, and a
loop that stopped on the first was reporting a guess as a measurement.

So decide out loud, from what you can actually see:

- **Was the round whole?** Six readings compared, or four? A partial round found
  less because it *saw* less. Fix the missing lenses and re-run before concluding
  anything — re-running a round is safe and overwrites its findings.
- **Did you resolve anything substantial?** If so, run another round: the spec has
  changed, and round two reaches the questions that exist only downstream of how
  you answered the last ones — the ones a build would have hit in hour six because
  of a decision made in hour two. **Round two is not a retry.**
- **Are the remaining open blockers ones you are willing to build on?** That is
  the actual question, and it is a judgment about your risk, not about the spec's
  completeness.

Whatever you conclude, say it as your own assessment and say what it does not
mean. "The last round surfaced nothing I judge worth blocking on" is honest. "The
spec is complete" is not a claim this loop can support, and nothing in it will
tell you otherwise.

**Spike the one decision worth executing.** If a blocker is expensive and
irreversible and the lenses split on it, no amount of reading settles it — that
is precisely what building was for. Write only that one interface and its state
transitions, half an hour, and let it fail. Recommend this rather than pretending
the round covered it.

When you stop, report the decisions resolved, what is still open, what you
assumed, and where the readings disagreed — then hand the user back their spec.
Planning, estimating and building are not this skill's job and there is no
follow-on skill to point at; the deliverable is a spec with fewer holes in it and
a list of the holes that remain.

---

## The reading format

Give this to every subagent verbatim. One JSON object per lens:

```json
{
  "lens": "concurrency",
  "spec_root": "specs",
  "matrices": [
    {
      "name": "held resource × concurrent operation",
      "rows": ["seat hold", "payment intent", "seat inventory"],
      "cols": ["second hold attempt", "cancel", "timer expiry"],
      "cells": [
        {"row": "seat hold", "col": "second hold attempt",
         "value": "rejected with 409", "guessed": true},
        {"row": "seat hold", "col": "timer expiry",
         "unanswerable": "spec never says who owns the timer, so two owners give two answers"}
      ]
    }
  ],
  "cells": [
    {"id": "hold.timeout", "value": "seat returns to the pool", "guessed": true}
  ],
  "decisions": [
    {
      "question": "What does the second caller see while a seat is held?",
      "value": "blocks until the hold is released",
      "where": "specs/booking.md#Holds",
      "guessed": true
    }
  ],
  "blockers": [
    {
      "id": "seat-contention-loser",
      "title": "What the losing caller sees on a contended seat",
      "question": "Reject with 409, or queue the caller?",
      "where": "specs/booking.md#Holds",
      "options": [
        {"label": "reject-409", "consequence": "caller must retry"},
        {"label": "queue", "consequence": "unbounded wait under load"}
      ],
      "recommended": "reject-409",
      "impact": "changes_behaviour",
      "reversible": false
    }
  ]
}
```

**`matrices`** — the lens's own cross-product, and **the reason this loop finds
anything a careful read would not.** Each lens names its own axes, from its own
angle of attack, and then must put something in every intersection. That is the
one property building had for free: a compiler never needed to be told which
decisions existed, because it walked the graph and stopped at each one. Prose walks
nothing and can omit a case without looking incomplete. A grid of rows × columns
cannot — the cell exists whether anyone wants to answer it or not.

Three things may go in a cell, and they mean different things:

- **an answer** — `value`, plus `guessed: true` if the spec did not supply it,
- **`unanswerable`** — you reached the cell, cannot answer it, and say why in one
  line. **This is the most valuable output of the whole reading**: a question the
  spec's own vocabulary forced into existence and cannot settle,
- **nothing at all** — no entry. Reported as a cell you enumerated and never came
  back to, which is a hole in your reading rather than in the spec.

Nothing gates on filling it. There is no score and no pass mark, so padding a cell
with a confident invention buys you nothing and costs the round a real finding.

Axes come from the lens file (§ *the matrix to fill*). Keep each matrix small
enough to actually complete — two axes of three to six values each. Two small
matrices beat one 12 × 12 nobody finishes.

**`lens`** — write it, but **the filename decides**. `reading.ordering.json` is the
`ordering` lens whatever its body says, because a body written by hand across six
concurrent prompts is exactly where a copy-paste puts the same name on two files —
and two readings sharing a lens name collapse into one, taking the disagreement
between them with it. A mismatch is reported, not silently obeyed.

**`cells`** — the grid, answered. One short value per cell the lens has a view
on, keyed by the id the grid gave it, and `guessed: true` whenever the spec did
not say. Omit a cell rather than filling it from nothing; a concurrency lens has
no business answering an authorization cell, and a lens that fills everything
tells you less than one that fills what it knows. Comparison here needs no word
matching — the id already says two lenses answered the same question.

**`decisions`** — every question the lens had to answer to proceed, with the
answer it settled on. This is where the comparison happens, so it matters that
the lens writes down what it decided *even when the spec was silent* — set
`guessed: true` and the reading stays honest.

Phrase `question` as a question, and **name the subject before the object**: two
lenses will word it differently, and the CLI collides them on their significant
words *in order*. So "does the hold expire before the payment" matches a lens that
wrote "when the holds expire, is that before payment", and does **not** match "does
the payment expire before the hold" — which is a different question with a
different answer. Ordering is what keeps the second from being reported as
agreement-turned-disagreement with the first. Where two lenses phrase one question
differently, both phrasings are shown.

A lens answering the same question twice with different values is reported as a
note; the first answer stands. If a lens needs to change its mind, it should
answer once.

**`blockers`** — decisions the lens could not responsibly make alone. Needs a
stable slug `id` (so the same finding from three lenses collides), a `question`
answerable in one line, at least two `options` with consequences, a
`recommended`, `impact` (`blocks_build` / `changes_architecture` /
`changes_behaviour` / `cosmetic`), and `reversible`.

**`where`** — the spec file and section. Used to group a disagreement with the
blocker at the same place, so one gap does not show up as two items.

Tell subagents plainly: **a guessed answer recorded honestly is more useful than a
confident one.** The comparison only works if each lens reports what it actually
concluded. There is no gate rewarding a full-looking artifact, so there is no
reason to pad one.

---

## Asking well

Human attention is the scarce resource here.

- **Prefer proposing.** "I'll assume X unless you object" clears most items for
  free. Reserve real questions for consequential forks.
- **One line to answer.** Give the scenario, the options with consequences, and
  your recommendation. If a question needs a paragraph of setup, the lens did not
  finish its work — say so rather than passing the confusion on.
- **Batch.** Present related decisions together.
- **Say who found it, not a number.** "Five of six independent readings hit this"
  is useful and true. A ratio or a score is not — there is no calibration behind
  one.

## Reporting

Report counts and observations: decisions resolved, still open, assumed, and
where the readings disagreed. No composite metric — this loop has no calibration
to justify one, and a number would make a judgment look like a measurement.

Say plainly what the loop cannot do: several agents reading a spec is not the same
as building it, so it will miss defects that only appear when code runs. That
honesty is what makes the findings it *does* report worth acting on.
