---
name: specflow-mutate
description: Internal QA. Inject a known ambiguity into a spec, run the refinement loop against the damaged copy, and verify the loop both detects and localizes the defect. This is how we validate the instrument — not a customer feature.
argument-hint: "(optional) spec_dir kind — kind defaults to drop_constraint"
---

# SpecFlow Mutate (internal)

**This skill is for SpecFlow engineers and is deliberately not shipped in the
marketplace plugin.** It validates the product; it is not part of it.

## The problem it solves

SpecFlow 2.0 rests on an unproven hypothesis: that divergence between simulated
builds tracks real specification defects. With no real builds, there is nothing
to check that against.

So we manufacture the ground truth. Take a spec that refines cleanly,
programmatically remove a constraint or introduce a contradiction, and assert two
things:

1. **Detection** — the loop raises a blocker.
2. **Localization** — the blocker lands on the requirement we damaged.

Localization is the part that matters. A loop that complained about everything
would score perfectly on detection alone and be worthless. Detection without
localization is not a pass.

This is also the regression suite for the whole pipeline. A mutation that stops
being caught is a concrete bug with a reproducible input.

```bash
SF="${CLAUDE_PLUGIN_ROOT:-$(pwd)/plugins/specflow}/lib/specflow_cli.py"
```

## Available mutations

| Kind | What it does |
|---|---|
| `drop_constraint` | deletes a line carrying a hard constraint (`must`, `never`, `exactly`, `at least`) |
| `contradict` | inverts a modal in place, so the spec asserts both a rule and its negation |
| `vague_quantity` | replaces a specific number with "several" |
| `drop_error_case` | deletes a line describing failure handling |
| `blur_enum` | replaces an explicit list of allowed values with "an appropriate value" |

Selection is index-based, not random, so any run is reproducible from its
manifest alone.

## What to do

### 1. Establish a clean baseline

Run `/specflow-refine` against the unmodified spec first and let it converge. A
mutation test is only meaningful against a spec the loop already handles — if the
baseline has open blockers, you cannot tell your injected defect from the noise.

### 2. Inject one defect

```bash
python3 "$SF" mutate apply \
  --spec-dir specs \
  --into /tmp/specflow-mutation \
  --kind drop_constraint \
  --index 0
```

This copies the spec tree, applies exactly one mutation, and writes
`mutation-manifest.json` recording what was damaged and where.

Read the manifest and confirm the mutation is genuinely a defect. Some lines
match the pattern but carry no real constraint, and deleting one of those tests
nothing. If so, increment `--index` and try again.

### 3. Run the loop against the damaged copy

Run `/specflow-refine` with `spec_dir` pointed at the mutated tree, and a
separate `outputs_dir` so you do not overwrite the baseline run.

### 4. Verify

```bash
python3 "$SF" mutate verify \
  --outputs /tmp/specflow-mutation/docs \
  --manifest /tmp/specflow-mutation/mutation-manifest.json
```

Exit 0 means detected and localized. Non-zero means the loop missed it.

### 5. Interpret a miss honestly

A miss is a finding about our product, so resist explaining it away. Work out
which it is:

- **The lens set has a blind spot.** No lens attacks the class of defect that was
  injected. Fix: add or sharpen a lens.
- **The artifact does not force the question.** The structure let the lens skip
  the damaged area. Fix: extend the schema or the totality checks — this is the
  strongest kind of fix, because it applies mechanically to every future run.
- **The mutation was not really a defect.** The spec determined the value
  elsewhere, so nothing was lost. Not a miss; pick another line.

Record misses. A mutation that used to be caught and now is not is a regression,
and it is the cheapest signal we have that a prompt change made the loop worse.

## Coverage

One mutation proves one thing. Sweep the kinds, and several indices per kind, to
say anything general about the loop's sensitivity. Each run is a full fan-out, so
sweeps are the expensive part of developing this product — budget for them
deliberately rather than running them ad hoc.
