# SpecFlow plugin

Spec refinement that runs entirely in the user's Claude Code session.

**This plugin is prose.** Skills orchestrate — spawning subagents, sequencing
rounds, deciding when to ask the user. Every count, ranking and verdict comes
from the SpecFlow CLI, which ships separately on PyPI as `gd-specflow`.

That split is deliberate and it is what keeps the design honest. An oracle's
whole value is that it is not a language model: "check the state table is
complete" as an instruction is advisory, while a command that exits non-zero on
an empty cell is a forcing function. Keeping the two in different artifacts
means prose cannot quietly reimplement a check, and a check cannot come to
depend on a prompt.

## Install

```bash
uv tool install gd-specflow          # the CLI — the oracles live here
specflow plugin install --target claude
```

The second command points Claude Code at the published marketplace and installs
this plugin. Installing in that order matters: the skills call `specflow`, so
the CLI has to exist first. If you added the marketplace by hand instead, the
skills will tell you what is missing.

## Skills

| Skill | Job |
|---|---|
| `specflow-analysis` | lock every architectural dimension; emit a report plus a checkable `dimensions.json` |
| `specflow-refine` | the loop — fan out independent lenses, merge, rank, resolve, repeat until saturated |
| `specflow-simulate` | one lens, one pass, no loop |
| `specflow-resolve` | record decisions and write them back into the spec |
| `specflow-contracts` | emit the data model and API contract as real artifacts, then check them |
| `specflow-planning` | phase the work, after refinement rather than before |
| `specflow-report` | current state of a refinement, read-only |

The six adversarial lenses ship as data (`skills/specflow-refine/lenses/*.md`),
not as skills. Nobody types "run the idempotency lens", `/specflow-simulate`
reads the same files so the two paths cannot drift, and a seventh lens is one
markdown file. Lens count is the cost dial.

## The commands the skills call

```
specflow refine new-round         allocate the next round directory
specflow refine round             validate, merge, rank, decide  <- the workhorse
specflow refine validate          schema + totality only
specflow refine resolve           record a decision
specflow refine status            current state, for reporting
specflow refine contracts         model contradictions + emitted SQL/API checks
specflow refine check-dimensions  gate the analysis artifact
specflow refine schema NAME       print an artifact contract
specflow refine mutate            inject a known defect, verify it is caught (internal QA)
```

Exit codes: `0` success, `1` checks failed, `2` bad usage. Source lives in
`mcp_server/services/oracles/` (library) and `mcp_server/services/refine_commands.py`
(the command group).

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
