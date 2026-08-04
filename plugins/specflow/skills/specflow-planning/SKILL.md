---
name: specflow-planning
description: Create a phased implementation plan from a refined specification. Best run after /specflow-refine, once the spec's ambiguities are resolved — a plan built on an ambiguous spec silently encodes one arbitrary reading of it.
argument-hint: "(optional) spec_dir outputs_dir src_dir — defaults: specs docs src"
---

# SpecFlow Planning

You are a senior engineer turning a specification into a phased implementation
plan.

## Run this last, not first

A plan is downstream of the spec. If the spec is ambiguous, the plan is **one
arbitrary resolution** of that ambiguity — and once written, it anchors
everything after it. Nobody revisits the decision, because it no longer looks
like a decision.

So the order matters:

```
/specflow-analysis  →  /specflow-refine  →  /specflow-planning
```

Before you start, check the refinement state:

```bash
specflow refine status --outputs <outputs_dir>
```

- **Converged** — good. Build the plan; the spec's ambiguities have been settled
  and recorded.
- **Open decisions** — say so plainly, list what is still open, and recommend
  `/specflow-refine` first. If the user wants the plan anyway, produce it, but
  state clearly which open decisions you had to resolve yourself and how. Those
  are the parts most likely to be wrong.
- **No refinement at all** — proceed if asked, and be explicit that this plan
  rests on your own reading of an unrefined spec.

The `resolutions` in that payload are now part of what the spec means, and the
plan must honour them.

## What to produce

Write `<outputs_dir>/planning/IMPLEMENTATION_PLAN.md`.

### Locked values first

Open with the architectural dimensions, taken from
`<outputs_dir>/analysis/dimensions.json` and the recorded resolutions — not
re-derived. Re-deriving them here would reintroduce exactly the variance
refinement removed.

### Then the phases

Small and focused beats large and vague. Each phase gets:

- **a number and a name**,
- **what it delivers** — concrete artifacts, not activities. "User can log in and
  the session survives a restart", not "work on authentication".
- **what it depends on** — earlier phase numbers.
- **how you know it is done** — an observable condition. If you cannot state one,
  the phase is too vague to be a phase.

Sizing guidance: a phase should be a single coherent piece of work with a
demonstrable outcome. If describing what it delivers needs the word "and" more
than twice, split it. If a phase cannot be verified without building the next
one, merge them.

Order by dependency, not by layer. "All the models, then all the endpoints, then
all the UI" defers every integration risk to the end, which is where it does the
most damage.

## Being honest about the plan

State what the plan assumes. Every phase boundary is a judgment call, and a
reader deciding whether to trust it needs to know which calls were forced by the
spec and which were yours.

If the spec left something open and you resolved it to make the plan work, say so
in the phase where it matters — do not bury it in the assumptions section. That
is where a plan quietly becomes a design document.

Do not estimate durations unless asked. Phase count and phase content are what
the spec supports; hours are a different claim resting on facts about the team
that are not in the spec.
