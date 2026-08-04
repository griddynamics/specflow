# specflow2 — the spec-refinement plugin

Spec refinement that runs entirely in the user's Claude Code session.

**Separate from the `specflow` plugin in the same marketplace.** That one carries
the two skills of the 1.0 backend flow — `specflow-analysis` and
`specflow-planning`, symlinked to the templates the MCP server serves for
`check_specification_completeness` and `run_planning`. This one carries the local
refinement loop and nothing else. Installing either does not affect the other, and
they write to different places: 1.0 owns `<outputs_dir>/{analysis,planning}/`, this
plugin owns `<outputs_dir>/refine/`.

**This plugin is prose.** Skills do the work: they spawn independent subagents,
sequence rounds, and decide what reaches the user. The SpecFlow CLI — shipped
separately on PyPI as `gd-specflow` — does the part prose is bad at.

The split follows one rule:

**Code compares.** Which lenses answered the same question differently. Which
cells nobody filled. Which blockers three lenses raised independently. Which
decisions you have already made, so they stop being re-asked. This is list work
over more items than a model tracks reliably, and it must give the same answer
twice.

**A model judges.** Whether the architecture is sound, whether the spec is ready,
whether a decision is worth interrupting someone over, whether another round is
worth running. None of that is checkable, and earlier versions of this plugin tried
anyway — a completeness gate over a checklist the agent wrote itself, a weighted
score deciding what to ask about, and a round diff read as convergence. All three
were judgments wearing arithmetic. They are gone, and the skills now make those
calls out loud where a user can disagree with them.

So there is no validator, no readiness score, no stop rule, and nothing that will
tell you your spec passed. What you get is: here is where independent readings of
your spec disagreed, and here is what the agent concluded from that.

## What replaces building

SpecFlow 1.0 found gaps by building the system — hours per variant. That worked for
one reason: a compiler never had to be told which decisions existed. It walked the
graph and stopped at every undecided thing. Reading a spec walks nothing, and a
paragraph can omit a case without looking incomplete.

Four things restore that forcing, in order of how much they recover:

- **Each lens's own matrix.** Before answering anything, a lens names its rows and
  columns — held resources × colliding operations, entities × lifecycle events,
  operations × who is calling — and then must account for **every intersection**. A
  cell exists whether or not anyone wants to answer it. A cell the lens reaches and
  cannot answer, with a reason, is the single most useful thing this loop produces.
  Six lenses declare six different cross-products, so a case invisible from one
  angle is often forced by another.
- **A shared grid.** One pass enumerates decisions for *all* lenses, so those cells
  are comparable across readings — narrower than the matrices, but the only thing
  here with a denominator. A cell nobody filled is a countable gap; a cell every
  lens filled *by guessing* is agreement that is not evidence.
- **A coherence pass.** After the lenses finish, one agent asks whether their
  answers can all be true at once. Disagreement finds gaps but never finds lenses
  that agreed and were jointly wrong; building found those when the code did not
  run.
- **The next round.** Seeded with your resolutions, so it reaches the questions that
  exist only because of how you answered the last ones.

**None of it executes anything.** A matrix forces the *question* to exist; only
running code proves an answer wrong. So this cannot find what appears when data
flows, and it cannot show a decision is impossible — a lens can argue two
requirements conflict, but nothing here fails to compile. For the one decision that
is expensive, irreversible, and split, the skill tells you to spike it: half an hour
of real code, not eight hours of it.

## Install

```bash
uv tool install gd-specflow          # the CLI
specflow plugin install --target claude
```

The second command points Claude Code at the published marketplace and installs
`specflow2` from it. Installing in that order matters: the skills call `specflow`,
so the CLI has to exist first. If you added the marketplace by hand instead, the
skills will tell you what is missing — and note the plugin name:

```bash
claude plugin marketplace add griddynamics/specflow
claude plugin install specflow2@specflow-marketplace   # not `specflow`
```

## Skills

Two.

| Skill | Job |
|---|---|
| `specflow-refine` | the loop — fan out independent lenses over the spec, compare, repeat |
| `specflow-resolve` | settle the decisions it found and write them back into the spec |

The six adversarial lenses ship as data (`skills/specflow-refine/lenses/*.md`),
not as skills. Nobody types "run the idempotency lens", and a seventh lens is one
markdown file. Lens count is the cost dial.

Nothing else is here on purpose. Earlier drafts of this plugin shipped a
single-lens pass, a read-only status renderer, a spec-analysis pass and a planning
skill. Each was
either a wrapper over one CLI command, or work this loop is not for: one lens
cannot disagree with itself, so it produces none of the signal, and planning and
estimating are downstream of a spec rather than part of reading one. The
deliverable here is a spec with fewer holes in it and a list of the holes that
remain.

## Models

SpecFlow never calls a model. The skills run in your coding agent and every
subagent inherits that agent's model, so the choice is yours and you make it
where you already make it. Any model your agent can run, SpecFlow runs on.

| Job | Needs |
|---|---|
| the `/specflow-refine` orchestrator — decides what reaches you | best-in-class model |
| the lenses, and `/specflow-resolve` | general purpose |

Small and cheap under-reports as a lens: sent to attack a spec, it agrees with
it, and a lens that finds nothing still costs a round.

If your harness can give different subagents different models, spread the lenses
across model families — disagreement between readings is the signal, and one
model disagrees with itself less than several do.

(The 1.0 backend flow is unchanged: OpenRouter, configured per tier with
`LLM_HIGH` / `LLM_MEDIUM` / `LLM_LOW` in your MCP client.)

## The commands the skills call

Four, and each exists only because a model doing the same job by eye would be
less reliable — not more authoritative.

```
specflow refine new-round   allocate the next round directory and name its files
specflow refine round       compare this round's readings against each other and
                            against the grid
specflow refine resolve     record a decision so later rounds stop asking it
specflow refine status      read the last round's findings back, minus anything
                            resolved since
```

The grid and the coherence file are optional inputs to `round`: write them and it
reports unanswered cells and folds in the coherence blockers, omit them and it
compares the readings alone.

Exit codes: `0` success, `2` bad usage — a missing round, a round with no
readings, a file that is not the JSON it should be. Every message names the file.
Nothing fails a run on a judgment call, and re-running a round simply replaces its
findings.

**There is no stop rule, and that is the deliberate part.** An earlier version
diffed each round against the previous ones, and the skill read "nothing new" as
convergence. That inference does not hold: `new == 0` is equally consistent with
this round's lenses finding less, and nothing holds lens effort constant between
rounds. Making it a threshold would need the false-negative rate of a single
round, which is unmeasured — so the diff, its counts and the round ledger behind
them are gone. When to stop is a judgment the skill makes out loud, where you can
disagree with it.

Source: `mcp_server/services/refine_compare.py` (comparison),
`refine_artifacts.py` (file layout), `refine_commands.py` (the command group).
About 680 lines of code, a third of it argparse wiring and output formatting. If
the comparison module starts growing, check whether a judgment has crept into it.

## Trying it out

`docs/specflow-2.0/testing-the-refine-loop.md` walks the whole thing, cheapest
first. Level 1 drives the four commands with hand-written artifacts and **no model
at all**, which is the fastest way to see what the loop does and the only way to
tell a CLI bug from a subagent that read the spec badly.

## Where the artifacts go

Everything the loop reads or writes lives under `<outputs_dir>/refine/`, one
directory the local flow owns outright:

```
docs/refine/
  resolutions.json      decisions made, cumulative
  findings.json         the latest round's merged view
  round-01/             grid.json, reading.<lens>.json, coherence.json
```

Readable JSON in your own repo — git-tracked, diffable, and editable with the
tools you already have. There is no database and no server to query.

Two things are absent by design. There is **no round ledger**: it existed only to
feed the stop rule described above, and rounds are simply the directories present.
And the loop writes **no markdown report** — the skill reports to you in the
conversation, which is where you can argue with it.

## Why this plugin writes no `analysis/` or `planning/` files

Those two directories belong to the 1.0 flow. Its contract reserves
`analysis/specification_completeness.md` and `planning/IMPLEMENTATION_PLAN.md`, and
its validator rejects an analysis file with no Part F readiness section — a section
that exists only to tell that backend whether to run E2E, and one this loop has no
reason to produce.

An earlier draft of the 2.0 skills lived in the `specflow` plugin alongside the 1.0
ones and wrote those same filenames with different content, so a user could run the
local skill and then have `run_generation` reject the file they never meant to hand
it. Splitting the plugins fixed that at the root: `specflow` has the 1.0 skills and
only those, `specflow2` has the loop and writes only under `refine/`, which is
outside the three directories that validator searches.

Two rules keep it that way. **Don't add an analysis or planning skill here** — if
you want to change what 1.0 produces, edit
`mcp_server/services/skills/specflow-{analysis,planning}/SKILL.md`, which is the
single source for both the MCP tool responses and the `specflow` plugin. And
**don't write into `analysis/` or `planning/`** from any skill in this plugin.
