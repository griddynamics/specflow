# SpecFlow plugin

Spec refinement that runs entirely in the user's Claude Code session.

**This plugin is prose.** Skills do the work: they spawn independent subagents,
sequence rounds, and decide what reaches the user. The SpecFlow CLI — shipped
separately on PyPI as `gd-specflow` — does the part prose is bad at.

The split follows one rule:

**Code compares and remembers.** Which lenses answered the same question
differently. Which blockers three lenses raised independently. What this round
found that no previous round did. This is list work over more items than a model
tracks reliably, and it must give the same answer twice.

**A model judges.** Whether the architecture is sound, whether the spec is ready,
whether a decision is worth interrupting someone over. None of that is checkable,
and an earlier version of this plugin tried anyway — a completeness gate over a
checklist the agent wrote itself, and a weighted score deciding what to ask about.
Both were judgments wearing arithmetic. They are gone, and the skills now make
those calls out loud where a user can disagree with them.

So there is no validator, no readiness score, and nothing that will tell you your
spec passed. What you get is: here is where independent readings of your spec
disagreed, and here is what the agent concluded from that.

## Install

```bash
uv tool install gd-specflow          # the CLI
specflow plugin install --target claude
```

The second command points Claude Code at the published marketplace and installs
this plugin. Installing in that order matters: the skills call `specflow`, so
the CLI has to exist first. If you added the marketplace by hand instead, the
skills will tell you what is missing.

## Skills

| Skill | Job |
|---|---|
| `specflow-analysis` | read the spec and report what it does and does not determine |
| `specflow-refine` | the loop — fan out independent lenses, compare, resolve, repeat |
| `specflow-simulate` | one lens, one pass, no loop |
| `specflow-resolve` | record decisions and write them back into the spec |
| `specflow-planning` | phase the work, after refinement rather than before |
| `specflow-report` | current state of a refinement, read-only |

The six adversarial lenses ship as data (`skills/specflow-refine/lenses/*.md`),
not as skills. Nobody types "run the idempotency lens", `/specflow-simulate`
reads the same files so the two paths cannot drift, and a seventh lens is one
markdown file. Lens count is the cost dial.

## Models

SpecFlow never calls a model. The skills run in your coding agent and every
subagent inherits that agent's model, so the choice is yours and you make it
where you already make it. Any model your agent can run, SpecFlow runs on.

| Job | Needs |
|---|---|
| the `/specflow-refine` orchestrator — decides what reaches you | best-in-class model |
| lenses, `/specflow-analysis`, `/specflow-simulate`, `/specflow-planning` | general purpose |
| `/specflow-report` | small and cheap |

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
specflow refine round       compare this round's readings; diff against previous rounds
specflow refine resolve     record a decision so later rounds stop asking it
specflow refine status      read that state back, for reporting and planning
```

Exit codes: `0` success, `2` bad usage. Nothing fails a run on a judgment call.

Source: `mcp_server/services/refine_compare.py` (comparison and bookkeeping),
`refine_artifacts.py` (file layout), `refine_commands.py` (the command group).
About 700 lines, half of it argparse wiring and output formatting. If the
comparison module starts growing, check whether a judgment has crept into it.

## Two skills exist twice in this repo, on purpose

`specflow-analysis` and `specflow-planning` also exist under
`mcp_server/services/skills/`, and the two sets are **not** shared.

They used to be: the paths here were symlinks into `mcp_server/`, which was
right while both channels served the same flow. They now serve different ones.
The `mcp_server` copies are templates with `<<SPEC_DIR>>` substitution tokens,
served as MCP tool responses into the backend `run_generation` flow. The copies
here drive the local refinement loop.

Do not re-link them. Doing so would hand 2.0 instructions to users of the live
backend flow. They converge again only when one of the two flows is retired.
