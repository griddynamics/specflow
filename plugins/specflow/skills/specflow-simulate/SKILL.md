---
name: specflow-simulate
description: Simulate building your spec and report what would block you. Single pass, one lens, no loop — the cheap way to see whether a specification is ready. Runs locally; nothing leaves the machine.
argument-hint: "(optional) lens spec_dir outputs_dir — lens defaults to partial-failure"
---

# SpecFlow Simulate

One pass, one lens, no loop. Use this to find out quickly whether a
specification has holes worth a full refinement round, or to interrogate one
specific risk.

For the full loop — six independent lenses, cross-lens disagreement, ranked
decisions, and fixes written back into the spec — use `/specflow-refine`.

## Arguments

- `lens` — which attack angle. One of `concurrency`, `partial-failure`,
  `data-lifecycle`, `auth-boundaries`, `idempotency`, `ordering`.
  Defaults to `partial-failure`, which finds the most on a first look.
- `spec_dir` — default `specs`.
- `outputs_dir` — default `docs`.

```bash
SF="${CLAUDE_PLUGIN_ROOT:-$(pwd)/plugins/specflow}/lib/specflow_cli.py"
```

## What to do

1. **Read the lens.** Load `../specflow-refine/lenses/<lens>.md` from the
   plugin. That file is the attack angle; it is shared with the full loop so the
   two never drift.

2. **Allocate a round.**

   ```bash
   python3 "$SF" new-round --outputs <outputs_dir> --lens <lens>
   ```

3. **Simulate the build yourself** — no subagent needed for a single lens. Read
   the spec, work through the lens's questions, and write one artifact to
   `<round dir>/interpretation.<lens>.json` following the contract in
   `/specflow-refine` (§ The artifact contract). The schema is at
   `lib/specflow/schema/interpretation.schema.json`.

   The structure is the point. You are filling a state transition matrix and a
   typed data model, not writing a list of concerns — a prose list is partial by
   nature and will skip the awkward cases. Two rules the validator enforces:
   no evasions (`TBD`, `unknown`, `varies`), and every admitted gap
   (`inferred: true`, `undefined_in_spec`, `spec_says: "nothing"`) must have a
   matching blocker.

4. **Check it.**

   ```bash
   python3 "$SF" round --outputs <outputs_dir>
   ```

   Non-zero exit means your artifact is not total. It prints exactly what is
   missing — fix it and re-run. Do not proceed past a failing gate.

5. **Report to the user.** Lead with the blockers that need a decision, then the
   contract issues, then what you assumed. Counts, never a score.

## Being honest about one lens

A single lens is a genuinely partial view, and you should say so. It finds what
its angle is built to find and is blind to the rest. Two things it structurally
cannot give you:

- **No cross-lens disagreement.** The strongest signal in the full loop is two
  independent readings locking the same architectural dimension to different
  values. With one reading there is nothing to compare.
- **No convergence.** You cannot know whether another pass would find more.

So frame the result as "here is what the *`<lens>`* lens found", and if it found
anything substantive, recommend `/specflow-refine` rather than implying the spec
is now clear.
