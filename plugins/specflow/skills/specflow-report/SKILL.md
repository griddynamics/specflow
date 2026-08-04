---
name: specflow-report
description: Show the current state of a specification refinement — what is resolved, what is still open, where independent readings disagreed, and what was assumed on your behalf.
argument-hint: "(optional) outputs_dir — default: docs"
---

# SpecFlow Report

Render the current state of refinement. Read-only: this skill runs no lenses and
changes nothing.

```bash
specflow refine status --outputs <outputs_dir> --json
```

That payload carries everything below — the open blockers, the disagreements, and
the decisions already recorded. Read it rather than the state files behind it.

## What to show

Five sections, in this order:

1. **Where we are.** Rounds run, and whether the last round raised anything the
   earlier ones did not.

2. **Open decisions.** For each: where in the spec it belongs, the question, and
   which independent lenses raised it. This is the actionable part, so put it
   high. Order it by your own read of what is costly to get wrong, and say that
   is what you did — the CLI does not rank these.

3. **Where readings disagreed.** Questions two or more lenses answered
   differently, with each lens's answer. These are the most concrete findings
   available — an ambiguity with a location and two specific readings attached.

4. **Unanswered, and agreed-but-guessed.** From `coverage`: the grid cells no
   lens answered, and the ones every lens answered by guessing. Neither appears
   as a disagreement, and both are gaps — the first is a question no reading
   reached, the second is consensus over a spec that said nothing. Say which is
   which; they need different fixes.

5. **Assumed on your behalf.** What was applied without asking, from the
   `--source assumed` records, and what each default was. Users should be able to
   audit these, disagree, and reopen one. Do not bury this section; a decision
   made silently is only acceptable if it is easy to find afterwards.

## Reporting rules

**Counts, never a score.** "7 open decisions, 3 architectural" is derived from
observation. "Spec readiness: 68%" is a number with no calibration behind it, and
it would make a judgment look like a measurement. There is deliberately no
composite metric in this product — do not compute one.

**Agreement as a count, not a ratio.** "Five of six independent readings hit this"
carries the same information and cannot be mistaken for a measurement.

**Name what is not known.** If the last round found something new, say another
round may find more. If it found nothing new, say exactly that — a fresh set of
independent readings surfaced nothing the earlier rounds had not. It does not mean
the spec is complete, and it does not mean an implementation will work: several
agents reading a spec is not the same as building it, so defects that only appear
when code runs are outside what this can see.

**Attribute your judgments to yourself.** Nothing in the CLI decides whether the
spec is ready or how the decisions rank. If you say the spec looks sound, that is
your assessment — phrase it that way so the user knows what they are trusting.

## If there is nothing to report

If `<outputs_dir>/refine/` does not exist, say so and point at `/specflow-refine`
(full loop) or `/specflow-simulate` (single pass). Do not invent a status.
