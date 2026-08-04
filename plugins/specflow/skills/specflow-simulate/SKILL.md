---
name: specflow-simulate
description: Read your spec under one adversarial lens and report what would block a build. Single pass, no loop — the cheap way to see whether a specification is ready. Runs locally; nothing leaves the machine.
argument-hint: "(optional) lens spec_dir outputs_dir — lens defaults to partial-failure"
---

# SpecFlow Simulate

One pass, one lens, no loop. Use this to find out quickly whether a specification
has holes worth a full refinement round, or to interrogate one specific risk.

For the full loop — several independent lenses and the disagreement between them
— use `/specflow-refine`.

## Arguments

- `lens` — which attack angle. One of `concurrency`, `partial-failure`,
  `data-lifecycle`, `auth-boundaries`, `idempotency`, `ordering`.
  Defaults to `partial-failure`, which finds the most on a first look.
- `spec_dir` — default `specs`.
- `outputs_dir` — default `docs`.

## What to do

1. **Read the lens.** Load `../specflow-refine/lenses/<lens>.md` from the plugin.
   That file is the attack angle; it is shared with the full loop so the two
   never drift.

2. **Allocate a round.**

   ```bash
   specflow refine new-round --outputs <outputs_dir> --lens <lens>
   ```

3. **Work through the spec yourself** — no subagent needed for a single lens.
   Answer the lens's questions, and write one reading to
   `<round dir>/reading.<lens>.json` in the format given in `/specflow-refine`
   (§ The reading format).

   Record what you actually concluded, including the answers you had to guess —
   mark those `guessed: true`. Nothing here gates on a full-looking artifact, so
   there is no reason to pad one, and an honest guess is more useful than a
   confident invention.

4. **Merge and record it.**

   ```bash
   specflow refine round --outputs <outputs_dir>
   ```

5. **Report to the user.** Lead with the blockers that need a decision, then what
   you would assume and why. Counts and observations, never a score.

## Being honest about one lens

A single lens is a genuinely partial view, and you should say so. It finds what
its angle is built to find and is blind to the rest. Two things it structurally
cannot give you:

- **No disagreement between readings.** The strongest signal in the full loop is
  two independent readings answering the same question differently. With one
  reading there is nothing to compare — you cannot disagree with yourself, and
  your own confidence is not evidence.
- **No sense of whether more remains.** You cannot know whether another pass
  would find more.

So frame the result as "here is what the *`<lens>`* lens found", and if it found
anything substantive, recommend `/specflow-refine` rather than implying the spec
is now clear.
