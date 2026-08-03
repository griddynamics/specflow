---
name: specflow-report
description: Show the current state of a specification refinement — what is resolved, what is still open, where independent readings disagreed, and what was assumed on your behalf.
argument-hint: "(optional) outputs_dir — default: docs"
---

# SpecFlow Report

Render the current state of refinement. Read-only: this skill runs no lenses and
changes nothing.

```bash
SF="${CLAUDE_PLUGIN_ROOT:-$(pwd)/plugins/specflow}/lib/specflow_cli.py"
python3 "$SF" status --outputs <outputs_dir> --json
```

That payload carries everything below — the `ask` and `assume` lists, the located
disagreements, and the contract issues from the latest round. Read it rather than
the state files behind it.

## What to show

Four sections, in this order:

1. **Where we are.** Rounds run, whether the loop has converged, and — if not —
   what the last round was still waiting on.

2. **Open decisions.** The `ask` list in ranked order. For each: the requirement
   it belongs to, the question, and how many independent lenses raised it. This
   is the actionable part, so put it high.

3. **Located disagreements.** Where independent readings locked the same
   architectural dimension to different values, with the values each lens chose.
   These are the most concrete findings available — an ambiguity with a file, a
   dimension, and two specific readings attached.

4. **Assumed on your behalf.** What was applied without asking, and what each
   default was. Users should be able to audit these, disagree, and reopen one.
   Do not bury this section; a decision made silently is only acceptable if it
   is easy to find afterwards.

## Reporting rules

**Counts, never a score.** "7 open decisions, 3 architectural" is derived from
observation and comparable across projects. "Spec readiness: 68%" is a number
with no calibration behind it, and it would launder judgment as measurement.
There is deliberately no composite metric in this product.

**Never show concordance as a ratio.** "Five of six independent readings hit
this" carries the same information and cannot be mistaken for a measurement.

**Name what is not known.** If the loop has not converged, say that another round
may find more. If it has converged, say what that means precisely: a fresh set of
independent readings surfaced nothing new to ask about. It does not mean the spec
is complete, and it does not mean an implementation will work — this simulates
the build, so defects that only appear when code actually runs are outside what
it can see.

## If there is nothing to report

If `<outputs_dir>/refine/` does not exist, say so and point at `/specflow-refine`
(full loop) or `/specflow-simulate` (single pass). Do not invent a status.
