# SpecFlow 2.0 — where the plan and the code actually stand

**Date**: 2026-08-04
**Reads with**: `specflow-plugin-plan.md` in this directory, which is **stale in
its second half** — see §2. Read this file first.

---

## 1. What is built

A local refinement loop, shipped as its **own** Claude Code plugin plus four CLI
commands.

- **`plugins/specflow2/`** — a second plugin in the existing marketplace, holding
  **two** skills, `refine` and `resolve`, plus six lens prompts as data. Prose
  orchestration: `refine` fans out independent subagents over the spec and decides
  what reaches the user; `resolve` settles the decisions and writes them back into
  the spec files.
- **`mcp_server/services/refine_{compare,artifacts,commands}.py`** — ~680 lines of
  code that compare a round's readings.
  `specflow refine new-round|round|resolve|status`.
- **State** — readable JSON under `<outputs_dir>/refine/` in the user's own repo.

The split is one line: **code compares, a model judges.** Nothing in the CLI
scores, gates, or passes verdict.

**`plugins/specflow/` is untouched — byte-identical to `main`,** symlinks and all.
That is the 1.0 companion plugin: two skills symlinked to
`mcp_server/services/skills/`, which is also what the MCP server serves. The two
plugins are independent products in one marketplace, and `specflow plugin install`
installs `specflow2` because it is the only one whose skills need this CLI present.

### What was cut, and why

Everything not essential to *finding what a spec fails to determine* was removed
after the §5 argument below made it clear the loop cannot promise convergence:

| Cut | Reason |
|---|---|
| `specflow-simulate` | One lens cannot disagree with itself, so it produces none of the signal. It was a demo. |
| `specflow-report` | A renderer over `refine status`. The orchestrator reports inline. |
| `specflow-analysis`, `specflow-planning` (2.0 copies of them) | Analysis is subsumed by six lenses reading the same spec; planning and estimating are downstream of a spec, not part of reading one. The 1.0 originals are unaffected — the PR briefly de-symlinked and forked them, and that is what got reverted. |
| `novelty`, `record_round`, `state.json`, `counts.new`/`counts.repeat` | The round-to-round diff. It existed to feed a stop rule the design cannot justify — see §5. Its output was being read as convergence. |

What survived the cut is the part that does not depend on a convergence claim:
disagreement detection, grid coverage (uncovered cells and agreed-but-guessed),
blocker merging with attribution, and `resolutions.json` so a decision already
made stops being re-asked. That last one is a fact about the user's input, not a
claim about coverage, which is why it stayed.

## 2. The plan document is half-superseded

`specflow-plugin-plan.md` was committed in the same branch as the code that
reverses it. Sections 1, 2 and 4 still describe what was built. These do not:

| Plan says | Code does |
|---|---|
| §3 — an oracle library: `validate_artifact.py`, `check_totality.py`, `contracts_oracle.py`, `concordance.py`, `rank_blockers.py`, `saturation.py` | All cut (commit `6808a60`). No validator, no ranking, no saturation rule. |
| §3 — "Artifacts are total or rejected" as the forcing function | No totality gate. A reading is compared as far as it goes; the grid reports unfilled cells instead of rejecting. |
| §5 — `specflow-contracts` skill | Not built. |
| §5 — `specflow-mutate` as the internal QA harness | Not built. This was the P7 gate. |
| §5 — seven published skills | **Two.** See §1. Four were cut as non-essential; `specflow-contracts` was never built. |
| §6 — `plugins/specflow/lib/` with JSON schemas | Does not exist; the code lives in the CLI wheel instead. |
| §6 — `saturation.py` and the round loop's stop rule (P6) | Cut. The inference it encoded is unsupported — §5. |
| §7 — build order P0–P9, with P2 and P7 as the gates | P0/P8-ish done. **P2 and P7 not run.** |
| §8 — Steel Commandments 2.0, five replacements | Not ratified. `CLAUDE.md` still carries all eleven 1.0 commandments. |

The reversal was deliberate and, on the merits, right: a completeness gate over a
checklist the agent wrote itself and a weighted score deciding what to ask about
were both judgments wearing arithmetic. Cutting them is the strongest decision in
the branch. But the plan of record now contradicts the code, including its own
"decisions needed from you" list — so it must not be read as current.

**Action**: either rewrite §3/§5–§8 of the plan or mark it superseded. Leaving it
as-is means the next person builds `check_totality.py`.

## 3. The product hypothesis is untested

> Disagreement between independent readings of a spec tracks real spec defects.

There is **no evidence for this in the branch.** Not weak evidence — none. No run
on a real spec is recorded, no cost is measured, and the harness that would have
produced the evidence (§5's `specflow-mutate`, the plan's own P7 gate) was cut.
The plan says "nothing is deleted until P7 is green"; P7 was deleted instead.

This is the single thing worth doing next, and it needs no new code — the manual
procedure is in `docs/specflow-2.0/testing-the-refine-loop.md` §"Level 3". Plant
one ambiguity in a spec you have already built from, run the loop, and score
*detected* and *localized* separately. A finding that says "the spec is unclear
about holds" for a mutation in the payment section is a miss dressed as a hit.

Until that has been done a few times, the honest claim is "it surfaces places
independent readings diverged", not "it finds spec defects". The skills are careful
to say exactly that, which is to their credit and is also the problem in §5.

## 4. Independence is load-bearing and unenforceable

The entire signal is that lenses read the spec with no knowledge of each other.
Nothing checks it, and nothing can: a round where six subagents shared context
produces artifacts byte-identical in shape to a round where they did not. Proposed
Steel Commandment 2.0 #2 ("no interpreter observes another interpreter's output")
states it as an invariant with no mechanism behind it.

It is also not **recorded**, which is the cheaper half of the problem. A reading
does not say which model produced it or what its prompt contained, so a leaky
fan-out cannot be caught even after the fact. One provenance field per reading
would make it auditable without pretending to enforce it — worth doing before any
measurement in §3, because otherwise a good result cannot be distinguished from a
lucky one.

## 5. The commercial tension worth naming

The engineering integrity here — no score, no gate, no readiness percentage, every
judgment attributed to the model out loud — is directly at odds with having
something to sell. 1.0 could say "we built it three ways and measured the
variance". 2.0's most defensible claim is "here is where independent readings of
your spec diverged, and here is what an agent concluded from that", with an
explicit disclaimer that nothing was executed and nothing was proven.

That may well be enough, especially with COGS at ~0 and nothing leaving the
customer's machine. But it is a *narrower* claim than §1 of the plan implies
("refine until the spec is unambiguous, then the plan you get is reliable" — the
code deliberately refuses to support that sentence), and the gap should be closed
deliberately rather than by a demo that overstates it. §3 is what closes it: a
measured detection rate on planted defects is a claim that survives contact with a
sceptical buyer, and it is the only one on offer.

## 6. Is it a dead end?

**No — but it is one experiment away from being one, and that experiment has not
been run.**

What is genuinely new and worth keeping regardless of how §3 turns out:

- **Located, attributed findings.** "This question, at this anchor, answered two
  ways by these two lenses" is a categorically better artifact than a prose
  blocker list, because it can be checked by a human in seconds.
- **The grid.** Enumerating the decisions before anyone answers them turns a gap
  from something you must notice into something you can count. Cheap, and it is
  the closest thing here to the forcing function a compiler provided.
- **Agreed-but-guessed.** Naming "consensus over a silent spec" as a distinct
  finding is the sharpest idea in the branch — it is the failure mode a
  disagreement-only design structurally cannot see, and most tools in this space
  do not have a name for it.

What would make it a dead end:

- §3 comes back negative — divergence turns out to be noise about wording rather
  than signal about spec defects. Plausible; the lenses are prompted to be
  adversarial, and adversarial readings diverge on determinate specs too. The
  false-positive rate on a spec you consider unambiguous is as important as the
  detection rate, which is why Level 3 measures both.
- Or the loop never terminates on anything but a judgment call. That is now
  **settled, not open**: the stop rule is gone, the loop reports no verdict, and
  the skill says out loud that stopping is the user's call. It costs the product
  its most natural-sounding claim, and the honest replacement is the measured
  detection rate in §3 rather than a convergence promise.

## 7. Smaller things carried over

- **`CLAUDE.md` describes only the 1.0 flow.** It has no mention of the local
  channel, and nothing there records the two rules that keep the marketplace's two
  plugins from colliding: `specflow2` ships no analysis or planning skill, and
  nothing in it writes to `analysis/` or `planning/`. Those rules are currently
  written only in `plugins/specflow2/README.md` and `bundled_skills.py`.
- **Prose is most of the diff and none of it is testable.** ~680 lines of code
  against ~930 of skills and README. The product lives in the prose, so
  the review surface is mostly unverifiable by construction. Stated as accepted
  risk in the plan (§9, "cannot fully close") and it is the right call — but it
  means Level 2–3 human observation is not optional, it is the only coverage the
  orchestration has.
- **The plugin path serves 1.0 templates unsubstituted.** Pre-existing on `main`
  and out of scope here, but worth recording: the symlinked skills contain
  `<<SPEC_DIR>>` / `<<OUTPUTS_DIR>>` / `<<SRC_DIR>>` placeholders that
  `server.py:_make_prompt_text` fills in for the MCP tool responses. Nothing fills
  them on the plugin path, so a `specflow` plugin user running `/specflow-analysis`
  sees the literal tokens. Unchanged by this PR either way.
