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

---

## The loop

### Step 1 — allocate a round

```bash
specflow refine new-round --outputs <outputs_dir> --lens concurrency partial-failure data-lifecycle auth-boundaries idempotency ordering
```

Note the round number and directory it prints.

Six lenses is the default. Fewer is cheaper and finds less; more costs
proportionally. This is the cost dial — start with three on a first pass if the
spec is large.

### Step 2 — fan out

Read each lens file from `lenses/` in this skill's directory. Then spawn **one
subagent per lens, all in a single message** so they run concurrently.

Give each subagent:

- the full contents of its lens file,
- the spec directory to read,
- the reading format below,
- the exact output path: `<round dir>/reading.<lens>.json`.

**Never tell a subagent what another lens found, and never pass it a plan.** Each
one reads the spec cold. If a previous round produced resolutions, pass
`<outputs_dir>/refine/resolutions.json` — those are now part of the spec's
meaning, so all lenses may see them equally.

**Which model runs a lens.** SpecFlow does not choose — a subagent inherits
whatever your harness gives it. A lens needs a general-purpose model or better;
small and cheap under-reports, agreeing with the spec it was sent to attack. If
your harness can give different subagents different models, spread the lenses
across model families — one model disagrees with itself less than several do.

### Step 3 — compare

```bash
specflow refine round --outputs <outputs_dir>
```

This prints where the readings disagree, the merged blockers with attribution,
and which are new since previous rounds. It writes `findings.json` for the
reporting skill.

### Step 4 — decide what reaches the user

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

### Step 5 — loop or stop

The command tells you whether this round raised anything the previous rounds did
not. **Whether that means you are done is your call.** Nothing new in one round
is decent evidence the well is dry; it is not proof. If the spec is high-stakes,
run another.

When you stop, say plainly what happened and what it does not mean. Then suggest
`/specflow-planning` — the spec is now more determinate, so a plan built from it
is worth more.

---

## The reading format

Give this to every subagent verbatim. One JSON object per lens:

```json
{
  "lens": "concurrency",
  "spec_root": "specs",
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

**`decisions`** — every question the lens had to answer to proceed, with the
answer it settled on. This is where the comparison happens, so it matters that
the lens writes down what it decided *even when the spec was silent* — set
`guessed: true` and the reading stays honest. Phrase `question` as a question;
two lenses will word it differently and the CLI matches on content.

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
