# SpecFlow 2.0 — Pivot Plan

**Status**: DRAFT — awaiting approval
**Date**: 2026-08-03
**Shape**: A Claude Code plugin. No backend, no Agent SDK, no hosted anything.

---

## 1. What the product is now

**Autonomous specification refinement with targeted human-in-the-loop.** N independent agents simulate building the system, surface the blockers they hit, and the loop converges with the human resolving only what genuinely needs resolving.

What changed from 1.0, and why:

| 1.0 | 2.0 | Reason |
|---|---|---|
| Build 3 apps, measure variance, report a score | Simulate building, name the blockers, fix the spec with the human | A metric is only worth computing when the thing you care about is unobservable. If an agent can point at the hole, the proxy is overhead — and an uncalibrated score launders judgment as measurement. |
| Estimate as deliverable | No user-facing metric at all | `CV=0.23, Rejected` is not actionable. "Requirement 4.2 doesn't say what happens when payment succeeds but the reservation expired" is. |
| Backend runs the harness on NFS/K8s | User's Claude Code session **is** the harness | §2 |
| No HITL possible mid-run | HITL is the core mechanic | 1.0's own constraint: "no opportunity to prompt the user." HITL *requires* the local architecture. |

**Multiplicity survives, demoted.** N independent agents were never primarily about sigma — they give independent sampling of the interpretation space. Union of blockers = better recall. Concordance across agents = the triage function that decides what is worth a human's attention. **Never shown as a score.** Human attention is the scarce resource; concordance is how it gets spent well.

The one thing that remains genuinely unobservable is *"have we found all the holes?"* — you cannot see what you haven't found. That is multiplicity's residual job: recall, not grading. Clean line, and it says exactly what to keep.

---

## 2. Do we need the Agent SDK? No.

The **Claude Agent SDK** is Claude Code packaged as a library — built-in tools, agent loop, context management, subagents, permissions, hooks. It supplies the **harness only; deployment is yours.**

That is exactly what the current backend is for: `claude_code.py` + workspace pool + NFS + K8s exist to run the Claude Code harness *somewhere other than the user's machine*. The K8s-over-Cloud-Run decision was justified by 8-hour tasks; an interpretation session takes minutes.

Once the product runs in the user's IDE, **the user's Claude Code session is the harness.** There is nothing to host, so the SDK provides nothing we need. Same for `mcp_server/` — it exists to precheck and call a backend that no longer exists.

### Consequences

- **COGS → ~0.** Runs on the user's own subscription. Business model shifts from consumption to licensing. This is a bigger change than the 10x we started with.
- **Zero egress, no server to audit.** The compliance story that killed P10Y is now airtight — nothing leaves the machine because there is no machine to leave for.
- **State = files in the user's repo.** Git-tracked, human-readable, human-editable. No Firestore, no SQLite, no NFS. The artifacts *are* the state, which is strictly better than an opaque database the user can't inspect.
- **Distribution = plugin install.** `plugins/specflow/` is already scaffolded (`plugin.json` + two empty skill dirs).

---

## 3. Prose for orchestration, code for oracles

This is the whole architecture in one line.

**Orchestration is prose** — a skill spawns subagents, sequences rounds, decides when to ask the human. Model-driven control flow, which is fine here because the steps are few and the decisions are judgment calls.

**Oracles are code.** An oracle's entire value is that it is *not* a language model. A prose instruction saying "verify the state table is complete" is advisory; a script that exits non-zero on an empty cell is a forcing function. Skills ship executable files and the agent runs them via Bash — proven in this repo by `.claude/skills/pr-loc-breakdown/count_py_loc.py`.

### The oracle set (must be code, ~1–2k LOC of pure functions)

| Script | Job | Why it can't be prose |
|---|---|---|
| `validate_artifact.py` | JSON Schema validation of each agent's output | A malformed artifact must fail loudly, not silently under-count |
| `check_totality.py` | Every cell of the state table / failure matrix is filled | **The forcing function.** §4 — this is what replaces building |
| `contracts_oracle.py` | Parse the emitted SQL DDL / OpenAPI / type defs with real validators | A genuine oracle at ~zero cost. Spec contradictions often surface as schema-level impossibilities |
| `concordance.py` | Anchor-scoped matching + agreement counts across N artifacts | Set operations. Reproducible, auditable, not generated |
| `rank_blockers.py` | Order by cost-asymmetry; dedup against already-resolved | Decides what the human sees — must be deterministic |
| `saturation.py` | Has a round produced new high-concordance blockers? | The stop rule (§6) |
| `mutate_spec.py` | Ambiguity mutation harness | §7 — validates the instrument |

All pure functions over files. No server, no persistent state, no network. Assertable in a test.

---

## 4. Restoring the forcing function

**Building was a falsification mechanism.** The compiler and runtime don't care what the agent believes, and building *compelled* decisions: you cannot proceed past an underspecified point.

The asymmetry to respect: **building surfaces unknown unknowns; simulation surfaces known unknowns.** An agent asked "would you hit blockers?" produces a *plausible* list, not an exhaustive one. It finds the legible gaps (missing field types, undefined error cases) and misses the ones that only bite at integration time — ordering, idempotency, partial failure, concurrent access, state that must survive restart. Those are exactly what destroys estimates, and exactly what the physical build was buying.

Naive simulation loses that class. Four substitutions recover most of it:

1. **Demand total artifacts, not narrative.** This is the key move. A prose blocker list is partial by nature; a filled matrix is total by construction. Building's power came from being compelled to produce a *total* artifact — so require the state transition table, the failure-mode matrix, the actual data model, the actual API contract. An agent filling a state table **cannot skip** the cell for "payment succeeded + reservation expired". The structure of the artifact is the forcing function, and `check_totality.py` enforces it.

2. **Adversarial lenses, not model diversity.** Assign each agent a specific attack angle: concurrency, partial failure, data lifecycle/migration, auth boundaries, idempotency/retry, ordering. Deliberate, cheaper, and more targeted than hoping three vendors have different blind spots. *(This also happens to neutralize the main limitation of skills-only — see §8.)*

3. **Trace-level walkthroughs.** Not "would you hit blockers" but "execute this end-to-end scenario step by step; at each step state what you read, what you write, what you return." Ambiguity surfaces as *"I cannot complete step 4 without knowing X."*

4. **Keep the compiler, drop the application.** Emit the data model and API contract as *real* schemas and run real validators on them. No runtime, no deploy, but a genuine oracle.

⚠️ **Do not lean the report primarily on self-reported blockers.** An agent that silently assumes something won't report it. Divergence in the *total artifacts* is the objective backstop — it doesn't depend on introspection.

---

## 5. Session topology (all skills + subagents)

| Stage | Mechanism | LLM? |
|---|---|---|
| **Orchestrator** | `/specflow-refine` skill — owns the round loop | prose |
| **Interpreters** | N subagents, one per lens, spawned in a single message so they run concurrently; blind to each other | yes |
| **Validate** | `validate_artifact.py` + `check_totality.py` + `contracts_oracle.py` via Bash | **no** |
| **Triage** | `concordance.py` + `rank_blockers.py` | **no** |
| **HITL gate** | `AskUserQuestion` — native, supports multiSelect and previews | prose |
| **Converge** | `saturation.py` → loop or stop | **no** |

Everything that produces a number or a verdict is a script. The LLM interprets and communicates; it never adjudicates.

### Independence hygiene

- Blind and parallel. No interpreter sees another's output. No sequential refinement.
- **No shared plan.** This was 1.0's defect: `sync_plan_to_workspaces` (`workflow_steps.py:700`) copied *one* plan to all workspaces, so the largest interpretation step — spec→plan — ran once and its arbitrariness was invisible. In 2.0 interpretation *is* the replicated step.
- Shared input is the spec alone, plus a fixed neutral artifact schema.
- **Shared context that resolves an ambiguity artificially lowers measured ambiguity.** We are refining the spec as delivered.

---

## 6. HITL design

**Human attention is the scarce resource.** Every question has a cost.

- **Prefer proposing over asking.** "I'll assume X unless you object" clears most items at near-zero cost. Reserve blocking questions for consequential forks.
- **Rank by cost asymmetry.** Wrong assumption cheap → assume and log. Expensive or irreversible → block and ask. `rank_blockers.py` owns this; concordance feeds it.
- **One-line answerable.** Give the scenario, why it blocks, candidate answers, recommended default. If the agent needs a paragraph to ask, it hasn't finished its own work.
- Match the existing rejection-catalog philosophy: specific, actionable, no internal jargon.

### Stopping rule

One legitimate job of a metric is knowing when to stop. Without one: **stop when a fresh round of independent agents produces no new high-concordance blockers.** Saturation, not a threshold — directly observable, needs no scoring, and it's an honest completion signal.

---

## 7. Validating the instrument

The core hypothesis — that simulated-build divergence tracks real spec defects — is unproven, and with no builds there's nothing to check against.

**Ambiguity mutation testing manufactures ground truth.** Take a clean spec, programmatically strip a constraint or introduce a contradiction, assert that (a) blockers are raised and (b) they localize to the mutated requirement. Offline, repeatable, no historical data, no builds.

It doubles as the regression suite for the whole pipeline: a mutation the loop fails to catch is a concrete bug.

---

## 8. What is genuinely lost

Stated plainly rather than discovered later.

| Loss | Severity | Assessment |
|---|---|---|
| **Multi-vendor model diversity** | Low | Subagents are Claude-only (opus/sonnet/haiku/fable) — no GPT-5.5, no GLM. But §4.2 already replaced model diversity with adversarial lenses, so the pivot happens to make skills-only viable. Lenses × Claude tiers still gives real prior diversity. |
| **Guaranteed orchestration** | **High** | Prose instructions are advisory; a skill cannot *force* a step. Mitigations: artifact-passing (a stage's input is the prior stage's output file, so a skipped stage is visible), validator scripts that exit non-zero, and hooks (harness-executed, so genuinely enforcing). **You cannot reach the Python orchestrator's hard guarantees.** This is the real cost of the architecture. |
| **Integration-class defects** | **High** | Some defects only physical building surfaces. §4 recovers much of it; not all. Accepted, not solved. |
| **Telemetry** | Medium | No Langfuse, no agent metrics — slower product improvement because you can't see customer runs. For the compliance-sensitive buyer, *not* collecting telemetry is a feature. Substitute: the artifacts are the record, in the user's repo. |
| **Cursor support** | Medium | ⚠️ **Skills and subagents are Claude Code only.** `docs/IDE-SETUP.md` currently supports Cursor too. A skills-only product drops Cursor users unless you keep a thin shim or write a parallel `.cursor/rules` implementation. **This is a commercial decision, not a technical one — flagging for your call.** |
| **CI-enforced invariants** | Low | Commandment VII's guard has no skills equivalent. Mostly moot — the invariants it protects disappear with the state machine. |

---

## 9. Codebase impact

**Deleted in full** — `backend/` (37.6k LOC app + 40.9k LOC tests), `mcp_server/`, `server.py`, `docker-compose.yml`, the K8s/NFS/Firestore/SQLite layer, `Dockerfile`, `scripts/init-mobile-sdk.sh`.

Everything justified by "run the harness on our infra" or "generated code takes hours and is irreplaceable" — both premises are now false. Retry = rerun. Crash recovery = rerun.

**Kept and grown** — `plugins/specflow/`:

```
plugins/specflow/
  .claude-plugin/plugin.json          # exists
  skills/
    specflow-analysis/                # exists, empty
    specflow-planning/                # exists, empty
    specflow-refine/                  # NEW — the orchestrator
      SKILL.md
      lenses/*.md                     # adversarial lens prompts
      schemas/*.json                  # artifact schemas
      scripts/                        # THE ORACLES (§3)
```

**Contract simplification** — Part F / `INTEGRATION_TESTS_READY` / `e2e-test-plan.md` and the whole rejection catalog collapse: with no backend there is no upload gate. Validation happens locally, inline, where the user can fix it immediately.

**`run_planning`'s role inverts.** Today it produces the one plan everyone implements. In 2.0 a single shared plan is the thing to avoid (§5) — planning becomes one of the replicated interpreter behaviours.

---

## 10. Steel Commandments 2.0 (proposed — needs ratification)

The constitution is premised on *"generated code takes hours to produce and is irreplaceable."* Nothing precious is produced now.

- **I–VI** (workspace sanctity, no-release-on-fail, archive-as-precondition, retry-reuses-workspace, no-background-touch) — **retire.** There are no workspaces.
- **VII–X** (state machine sole writer, forward-only checkpoints, transitions logged, invalid transitions raise) — **retire.** There is no state machine; state is files in the user's repo.
- **XI** — **retire**, superseded.

Proposed replacements, each protecting a property 2.0 actually depends on:

1. **No step may perform network I/O beyond the model call.** Protects the compliance property that is now the product's main asset.
2. **No interpreter may observe another interpreter's output.** Protects statistical independence.
3. **Every verdict, count, and validation is produced by a script, never by a model.** Protects auditability — this is what makes findings evidence rather than opinion.
4. **Artifacts are total or rejected.** The forcing function that replaces building (§4).
5. **Samples are ephemeral and reproducible; never build machinery to preserve them.** The anti-pattern that produced 9k LOC of preservation code.

---

## 11. Migration phases

| Phase | Work | Exit criterion |
|---|---|---|
| **P1** | Artifact schemas + `validate_artifact.py` + `check_totality.py`. Pure addition. | Totality check rejects a deliberately-incomplete state table |
| **P2** | `specflow-refine` SKILL.md + lens prompts. Run N=5 lenses on one real spec. | 5 total artifacts pass validation; **measured cost confirms the economics** |
| **P3** | `contracts_oracle.py` — real SQL/OpenAPI/type validators | Catches a planted spec contradiction as a schema impossibility |
| **P4** | `concordance.py` + `rank_blockers.py` + `AskUserQuestion` gate | End-to-end round with a human resolving ranked blockers |
| **P5** | `saturation.py` + the round loop | Loop terminates on saturation, not a fixed count |
| **P6** | `mutate_spec.py` harness (§7) | Injected ambiguity detected **and localized** |
| **P7** | Delete `backend/`, `mcp_server/`, infra. Ratify §10. Decide Cursor (§8). | Plugin installs clean; no network I/O outside model calls, asserted in a test |

**P2 and P6 are the gates.** P2 validates the economics, P6 validates that the loop measures anything real. Both precede the irreversible deletion in P7 — the sequence front-loads cheap reversible work deliberately.

---

## 12. Open risks

| Risk | Severity | Handling |
|---|---|---|
| Simulated-build divergence may not track real spec defects | **High** | P6 mutation harness. The core product hypothesis, currently unproven. |
| Prose orchestration can't guarantee steps ran | **High** | Artifact-passing + non-zero-exit validators + hooks. Cannot fully close (§8). |
| Self-reported blockers depend on agent introspection | **High** | Total-artifact divergence is the backstop; don't build the report on `blockers[]` alone. |
| Losing Cursor support | Medium | Needs a commercial decision (§8). |
| No telemetry → slower iteration | Medium | Artifacts in the user's repo; ask design partners to share them. |
| Sales narrative weakens ("we build 3 prototypes") | Medium | Counter: runs on your own subscription, nothing leaves your machine, no vendor, per-requirement findings 1.0 couldn't produce. |
