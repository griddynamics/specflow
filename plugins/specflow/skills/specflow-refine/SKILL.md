---
name: specflow-refine
description: Autonomously refine a specification — independent subagents simulate building it under different adversarial lenses, then you resolve the blockers they surface. Writes decisions back into the spec. Runs entirely locally; no backend, no data leaves the machine.
argument-hint: "(optional) spec_dir outputs_dir — defaults: specs docs"
---

# SpecFlow Refine

You are orchestrating a specification refinement loop. Independent subagents each
simulate building the system under one adversarial lens, deterministic scripts
merge and rank what they find, and you bring the user the small number of
decisions that genuinely need a human.

**You do not write code and you do not commit anything.** The point of
simulating the build is to find what the spec fails to determine, at a fraction
of the cost of building it.

## Arguments

- `spec_dir` — specification root. Default `specs`.
- `outputs_dir` — where artifacts are written. Default `docs`.

Check the toolkit is reachable once, at the start:

```bash
specflow refine --help >/dev/null
```

The oracles ship in the SpecFlow CLI, not in this plugin — this skill is prose,
and every verdict comes from that binary. If the command is not found, stop and
tell the user to install it:

```
uv tool install gd-specflow
```

**Do not work around a missing CLI by checking the artifacts yourself.** An
advisory check is precisely what this design rejects; a skill that falls back to
reading the JSON has silently turned the gates back into suggestions.

---

## Why this works — read before running

A real build is a *forcing function*: you cannot run code past a point the spec
left undefined. Simulation has no such compulsion, and an agent asked "what
would block you?" produces a plausible list rather than an exhaustive one. It
finds the legible gaps and quietly skips the awkward ones.

Three things restore the compulsion. Do not weaken any of them:

1. **Total artifacts, not prose.** Each lens fills a *structure* — a state
   transition matrix, a typed data model, an authorization rule per operation. A
   prose list is partial by nature; a filled matrix is total by construction. The
   validator rejects a partial one.
2. **Independence.** Subagents never see each other's output, and there is no
   shared plan. Two lenses reaching different conclusions from the same spec is
   the primary signal; shared context destroys it.
3. **Scripts decide, not you.** Every count, ranking and verdict comes from
   the CLI. Do not eyeball concordance or estimate a score. If a gate exits
   non-zero, stop and fix — do not proceed and mention it.

---

## The loop

### Step 1 — allocate a round

```bash
specflow refine new-round --outputs <outputs_dir> --lens concurrency partial-failure data-lifecycle auth-boundaries idempotency ordering
```

Note the round number and directory it prints.

### Step 2 — fan out

Read each lens file from `lenses/` in this skill's directory. Then spawn **one
subagent per lens, all in a single message** so they run concurrently.

Give each subagent:

- the full contents of its lens file,
- the spec directory to read,
- the artifact contract below,
- the exact output path: `<round dir>/interpretation.<lens>.json`.

**Never tell a subagent what another lens found, and never pass it a plan.**
Each one reads the spec cold. If a previous round produced resolutions, pass
`<outputs_dir>/refine/resolutions.json` — those are now part of the spec's
meaning, so all lenses may see them equally.

Six lenses is the default. Fewer is cheaper and finds less; more costs
proportionally. This is the cost dial.

### Step 3 — validate, merge, rank, decide

```bash
specflow refine round --outputs <outputs_dir>
```

This validates every artifact, merges them, finds located disagreements, ranks
blockers by cost asymmetry, and reports whether the loop has converged.

**If it exits non-zero, the artifacts are not total.** It prints exactly what is
missing. Send the failing lens back to fix its own artifact, then re-run. Do not
edit the artifact yourself to make the gate pass — that defeats the check.

### Step 4 — bring the user the decisions

The command prints three groups. Treat them differently:

- **`assume`** — apply the recommendation and record it. Do not ask.
- **`note`** — nothing to do; already in the audit trail.
- **`ask`** — these need the user.

For the `ask` group, hand off to `/specflow-resolve`, or handle it here with
`AskUserQuestion`. Either way, follow the rules in **Asking well** below.

### Step 5 — loop or stop

Re-run from Step 1 while the command reports `NOT CONVERGED`. It converges when
a fresh round of independent lenses surfaces nothing new to ask about —
saturation, not a threshold.

When it converges, tell the user plainly what happened, then suggest
`/specflow-planning`: the spec is now unambiguous, so a plan built from it is
worth trusting.

---

## The artifact contract

Give this to every subagent verbatim. The schema is authoritative — print it and paste it in:

```bash
specflow refine schema interpretation
```

Each lens writes one JSON object with these keys:

| Key | What goes in it |
|---|---|
| `lens` | the lens name |
| `spec_root` | the spec directory read |
| `dimensions` | Parts A–D locked to exactly one value each, every value carrying a `spec_anchor` |
| `entities` | the data model: every field typed, `required` set, `derived` and `references` where they apply |
| `operations` | every transaction, with `kind`, `entity`, `idempotent`, and `authorization` |
| `state_machines` | every entity with a lifecycle — **the matrix must cover every state × event pair** |
| `failure_modes` | what goes wrong, and what the spec says about it (`"nothing"` is a valid, obliging answer) |
| `phases` | how this lens would sequence the build |
| `blockers` | decisions the spec does not determine |
| `assumptions` | small choices made, recorded so they are auditable |

Two rules the validator enforces mechanically, so tell the subagents up front:

- **No evasions.** `TBD`, `unknown`, `varies`, `N/A` and similar are rejected in
  any filled field. If a value cannot be determined, that is a blocker, not a
  cell filler.
- **Every admitted gap must be raised.** Marking an anchor `inferred: true`, or a
  matrix outcome `undefined_in_spec`, or a failure mode `spec_says: "nothing"`,
  obliges a matching blocker against the same file. Otherwise the escape hatches
  become a quiet way to skip the hard cells.

Each blocker needs: a stable slug `id` (so the same finding from two lenses
collides), `title` stated as the missing decision, `spec_anchor`, `scenario`,
a one-line `question`, at least two `options` with consequences, a
`recommended` option, `impact` (`blocks_build` / `changes_architecture` /
`changes_behaviour` / `cosmetic`), and `reversible`.

`impact` and `reversible` decide whether the user is asked at all, so instruct
subagents to set them honestly rather than defensively. Marking everything
`blocks_build` floods the user and makes the ranking useless.

---

## Asking well

Human attention is the scarce resource in this design. Every question has a
cost, and the ranking exists to spend it well.

- **Prefer proposing.** "I'll assume X unless you object" clears most items for
  free. Reserve blocking questions for consequential forks.
- **One line to answer.** Give the scenario, why it blocks, the options with
  consequences, and your recommendation. If a question needs a paragraph of
  setup, the lens has not finished its work — send it back.
- **Batch.** Present related decisions together rather than one at a time.
- **Never show a score.** The numbers order the list and then stop existing. Say
  "five of six lenses independently hit this", not "concordance 0.83".

## Reporting

Report counts, not a readiness score: decisions resolved, still open, assumed.
A composite number would launder judgment as measurement, and this loop has no
calibration to justify one.

Say plainly what the loop cannot do: it simulates the build, so it will not catch
every defect a real build would. That honesty is what makes the findings it
*does* report worth acting on.
