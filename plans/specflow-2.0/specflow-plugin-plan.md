# SpecFlow 2.0 — Plugin Plan (final)

> ⚠️ **Sections 3 and 5–8 are superseded by the code that shipped.** The oracle
> library, the totality gate, the ranking and saturation scripts, the
> `plugins/specflow/lib/` layout and the Steel Commandments 2.0 proposal were all
> deliberately cut or reversed. So were four of the seven skills: what ships is
> `specflow-refine` and `specflow-resolve`, and there is **no stop rule** — §7's P6
> ("loop terminates on saturation") encodes an inference the design cannot support.
> Section 4 still holds and §1–§2 describe the shape, but the flow diagram in §1 and
> the skill inventory in §5 do not. See **`status.md`** in this directory for what is
> actually built, what was cut and why, and which gates remain unmet. Do not build
> from §3, §5, §6 or §7.

**Status**: DRAFT for approval
**Date**: 2026-08-03
**Supersedes**: `PLAN.md` and `PLUGIN-SKILLS.md` in this directory (earlier drafts, safe to delete once this is approved)
**Shape**: A Claude Code marketplace plugin. No backend, no MCP server, no Agent SDK, no hosted anything.

---

## 1. The flow

### Today (README §"Get started")

```
specs/  →  check_specification_completeness  →  run_planning  →  run_generation
           (local skill, free)                  (local skill, free)   (backend, 2–8 hrs, ~$400)
```

### SpecFlow 2.0

```
specs/  →  /specflow-analysis  →  /specflow-refine  →  /specflow-planning
           (gap detection)         (the product)        (now trustworthy)
                                        ↕
                                   you, resolving
                                   ranked blockers
```

**Yes — one new user-facing verb.** That is the whole UX change. `run_generation` is replaced by `/specflow-refine`, and everything the backend used to do dissolves into subagents inside that one skill.

### The one correction: planning moves *after* refine

You had it as analysis → planning → refine. I'd swap the last two, for the reason that broke 1.0's measurement.

**A plan is downstream of the spec.** If the spec is ambiguous, the plan is *one arbitrary resolution* of that ambiguity — and once it exists it anchors everything after it. That is precisely what `sync_plan_to_workspaces` (`workflow_steps.py:700`) did: one plan copied to all N workspaces, so the largest interpretation step ran exactly once and its arbitrariness became invisible to the statistics.

Planning before refining reintroduces that defect: you'd be refining against a spec whose ambiguity has already been silently resolved by the planner.

So planning takes on **two distinct roles**:

| Role | When | Why |
|---|---|---|
| **Internal, per-lens** | Inside each refine round | Attempting to sequence work is a strong forcing function — you cannot phase what you don't understand. Divergent phase decomposition across lenses *is* a blocker signal. |
| **Final, user-facing** | After refinement converges | One plan, generated from a spec whose ambiguities are resolved. Now worth trusting. |

This is also the better product story: *refine until the spec is unambiguous, then the plan you get is reliable.* Planning becomes the reward rather than a prerequisite.

`/specflow-planning` still runs standalone whenever the user wants — nothing stops them. But the documented happy path puts it last.

---

## 2. What `/specflow-refine` actually does

One skill, owning a loop. Each round:

**1. Fan out.** Spawn N lens subagents **in a single message** so they run concurrently. Each gets one adversarial lens and the spec. Blind to each other — no interpreter sees another's output, and there is no shared plan. Fresh context each.

**2. Each lens produces a *total* artifact.** Not a prose blocker list — a filled structure:

- the architectural dimensions (Parts A–D, every one "Pick exactly ONE")
- a state transition table
- a failure-mode matrix
- a phase decomposition (the internal planning role)
- blockers, each with a spec anchor

**Totality is the forcing function that replaces building.** A prose list is partial by nature; a filled matrix is total by construction. An agent filling a state table *cannot skip* the cell for "payment succeeded + reservation expired."

**3. Run the oracles** (scripts, not prose): schema conformance, totality check, contract validation.

**4. Triage.** Cross-lens concordance, then rank by cost asymmetry. Concordance is *not* a score shown to the user — it decides what is worth your attention. If 5 of 6 lenses independently ask the same question, it's real. If 1 of 6 asks, it's probably pedantry.

**5. Gate.** `AskUserQuestion` (native, supports multiSelect and previews). Prefer proposing over asking — "I'll assume X unless you object" clears most items at near-zero cost. Reserve blocking questions for consequential forks.

**6. Write decisions back into the specs**, with traceability, and record them so later rounds don't re-ask.

**7. Converge or loop.** Stop when a fresh round produces no new high-concordance blockers. Saturation, not a threshold — directly observable, no scoring, honest completion signal.

### The lenses

| Lens | Attacks |
|---|---|
| `concurrency` | simultaneous access, races, lock scope |
| `partial-failure` | half-completed operations, compensations, retries |
| `data-lifecycle` | migration, retention, deletion, backfill |
| `auth-boundaries` | who can do what to whose data |
| `idempotency` | replay, duplicate delivery, at-least-once |
| `ordering` | sequence assumptions, out-of-order arrival |

These are the failure classes physical building surfaced and that naive "think about blockers" misses. **Lens count is the cost dial.**

They ship as `lenses/*.md` assets, not as separate skills — nobody types "run the idempotency lens." Marketplace entries should be things a user would actually invoke.

### On "all the parallelism by subagents?" — yes

Spawned concurrently in one message, fresh context each, blind to each other. Two honest caveats:

- **Subagents are Claude-only** (opus/sonnet/haiku/fable). No GPT-5.5, no GLM. The existing `recommended-models: openai/gpt-5.3-codex` frontmatter goes inert. Adversarial lenses replace vendor diversity — deliberate attack angles beat hoping three vendors have different blind spots — but it *is* a real reduction.
- **N is tunable, and practical concurrency has limits.** Treat lens count as the cost/coverage dial and measure actual behavior at P2 rather than assuming all six run truly simultaneously.

---

## 3. Prose for orchestration, code for oracles

The architecture in one line.

**Orchestration is prose** — a skill spawns subagents, sequences rounds, decides when to ask you. Few steps, judgment calls, fine for a model.

**Oracles are code.** An oracle's entire value is that it is *not* a language model. "Verify the state table is complete" as an instruction is advisory; a script that exits non-zero on a blank cell is a forcing function.

**Code ships with the plugin.** A skill is a directory — `SKILL.md` plus assets and executables. Already proven in this repo: `.claude/skills/pr-loc-breakdown/` ships `count_py_loc.py` and the skill runs it via Bash.

| Script | Job |
|---|---|
| `validate_artifact.py` | JSON Schema conformance — malformed fails loudly, not silently |
| `check_totality.py` | Every dimension filled, every matrix cell present. **The gate.** |
| `contracts_oracle.py` | Real SQL DDL / OpenAPI / type-def validators |
| `concordance.py` | Anchor-scoped cross-lens agreement |
| `rank_blockers.py` | Cost-asymmetry ordering, dedup against resolved |
| `saturation.py` | The stop rule |

~1–2k LOC of pure functions over files. No server, no persistent state, no network — assertable in a test.

### The asset we already have

`specflow-analysis/SKILL.md` is 488 lines and already contains the total-artifact framework:

- **Part A** — 6 universal dimensions, each "Pick exactly **ONE**"
- **Part B** — technology-specific dimensions by project type
- **Part C** — project-specific dimensions, headed *"Discover additional variance sources"*
- **Part D** — micro-level consistency locks, *"AGGRESSIVE ENFORCEMENT"*, "Must specify ALL"

2.0 does not invent this. It (a) replicates the fill across independent lenses, (b) makes the fill machine-checkable, (c) diffs the filled values. **Divergence on a locked dimension is a named, localized spec ambiguity** — no scoring involved.

---

## 4. Why no backend, no MCP server, no Agent SDK

**The Agent SDK is Claude Code packaged as a library** — built-in tools, agent loop, context management, subagents, permissions. It supplies the **harness only; deployment is yours.** That is exactly what `backend/app/services/claude_code.py` + workspace pool + NFS + K8s exist to do: run the harness *somewhere other than the user's machine*.

Once the product runs in the user's IDE, **their Claude Code session is the harness.** Nothing to host, so nothing the SDK provides is needed. Same for `mcp_server/` — it exists to precheck and call a backend that won't exist.

Consequences worth stating plainly:

- **COGS → ~0.** Runs on the user's own subscription. This shifts the business model from consumption to licensing — a bigger change than the 10x we started from.
- **Zero egress, no server to audit.** Strictly stronger than the compliance story that killed P10Y.
- **State = files in the user's repo.** Git-tracked, human-readable, human-editable. No Firestore, SQLite, or NFS. Better than an opaque database the user can't inspect.
- **HITL becomes possible at all.** 1.0's own constraint was "no opportunity to prompt the user" mid-run. The HITL pivot *requires* the local architecture.

---

## 5. Skill inventory

**7 published, 4 net new.**

| Skill | Status | Role |
|---|---|---|
| `specflow-analysis` | extend | Gap detection. Add JSON output + totality gate. Drop Part F (`INTEGRATION_TESTS_READY` — it exists to tell the backend whether to run E2E). |
| `specflow-refine` | **new** | The orchestrator and entry point. §2. |
| `specflow-simulate` | **new** | Single-lens run, no loop. Cheap first touch, natural demo, immediate value. |
| `specflow-resolve` | **new** | Walk ranked blockers, write decisions into the spec files with traceability. |
| `specflow-contracts` | **new** | Emit data model + API contract as real schemas; validate with real validators. Keeps the compiler, drops the application. |
| `specflow-planning` | rework | Per-lens internally; final artifact after convergence. §1. |
| `specflow-report` | repurpose `specflow-compare-variants` (255 lines) | Current state: resolved, open, ranked. **Counts, never a score.** |

Retired: `specflow-diagnose` (156 lines, reads backend failure state — nothing to salvage).
Internal only: `specflow-mutate` → `.claude/skills/`, not published. It's our QA harness for validating the loop, not a customer feature.

**`specflow-resolve` is deliberately separate from finding blockers.** Applying decisions to spec files is an edit operation with its own hazards — don't clobber the user's prose, keep traceability, record resolutions for dedup. A loop that only *reports* blockers leaves all the work with the user, which isn't autonomous refinement.

---

## 6. Plugin layout

```
plugins/specflow/
  .claude-plugin/plugin.json            → v0.2.0, keywords += spec-refinement, blocker-detection
  lib/                                  # shared oracles — ONE copy
    schema/
      interpretation.schema.json
      dimensions.schema.json            # Parts A–D, machine-readable — the source of truth
      blocker.schema.json
    validate_artifact.py
    check_totality.py
    contracts_oracle.py
    concordance.py
    rank_blockers.py
    saturation.py
  skills/
    specflow-analysis/      SKILL.md
    specflow-planning/      SKILL.md
    specflow-refine/        SKILL.md   lenses/*.md
    specflow-simulate/      SKILL.md
    specflow-resolve/       SKILL.md
    specflow-contracts/     SKILL.md
    specflow-report/        SKILL.md
```

**Shared `lib/`, not per-skill copies.** `concordance.py` is needed by two skills, `validate_artifact.py` by four. Copies drift — the single-source-of-truth rule in CLAUDE.md applies to shipped scripts too.

Moving the dimensions framework into `lib/schema/dimensions.schema.json` does two things at once: shrinks the 488-line skill, and makes the framework machine-checkable.

⚠️ **P0 open item.** I have not verified the supported mechanism for a skill to resolve a path *above* its own directory to reach `lib/`. Do not build on an assumed environment variable — check the plugin docs first. Fallback is a thin per-skill shim over one implementation. 15 minutes, and it shapes the layout.

---

## 7. Build order

Each phase leaves the plugin installable and prior phases working.

| Phase | Work | Exit criterion |
|---|---|---|
| **P0** | Verify plugin-root path resolution. Move the four SKILL.md files from `mcp_server/services/skills/` into `plugins/specflow/skills/`. Drop the `<<PLACEHOLDER>>` substitution layer — skills take arguments directly. | `/specflow-analysis` runs from the installed plugin with no MCP server |
| **P1** | `lib/schema/*.json` + `validate_artifact.py` + `check_totality.py`. Extend `specflow-analysis` to emit JSON and call the gate. | Totality check rejects a deliberately-blank dimension |
| **P2** | `specflow-simulate` + the six lens prompts. Single lens end-to-end on a real spec. | Artifact validates; blockers carry spec anchors; **measured cost and real concurrency confirmed** |
| **P3** | `contracts_oracle.py` + `specflow-contracts`. | Catches a planted contradiction as a schema impossibility |
| **P4** | `concordance.py` + `rank_blockers.py` + `specflow-refine` fan-out (no loop yet). | N lenses run concurrently; blockers ranked and deduped |
| **P5** | `specflow-resolve` + the `AskUserQuestion` gate. | A human resolves ranked blockers; specs updated with traceability |
| **P6** | `saturation.py` + the round loop. `specflow-report`. | Loop terminates on saturation, not a fixed count |
| **P7** | `specflow-mutate` (internal). | Injected ambiguity detected **and localized** to the mutated requirement |
| **P8** | Rework `specflow-planning` for per-lens + final roles. Retire `specflow-diagnose`. Bump to `0.2.0`, update README flow. | Marketplace install delivers the full 2.0 experience |
| **P9** | Delete `backend/`, `mcp_server/`, `server.py`, docker-compose, infra scripts. | No network I/O outside model calls, asserted in a test |

**P2 and P7 are the gates.** P2 proves the economics and the concurrency assumption on a real spec. P7 proves the loop detects anything real. **Nothing is deleted until P7 is green** — the sequence front-loads cheap reversible work on purpose.

---

## 8. What gets deleted (P9, not before)

`backend/` (37.6k LOC app + 40.9k LOC tests), `mcp_server/`, `server.py`, `docker-compose.yml`, the K8s/NFS/Firestore/SQLite layer, `Dockerfile`, `scripts/init-mobile-sdk.sh`.

Everything justified by *"run the harness on our infra"* or *"generated code takes hours and is irreplaceable"* — both premises are now false. Retry = rerun. Crash recovery = rerun.

### Steel Commandments 2.0 (needs your ratification)

The constitution rests on those same two premises.

- **I–VI** (workspace sanctity, no-release-on-fail, archive-as-precondition, retry-reuses-workspace, no-background-touch) — **retire.** There are no workspaces.
- **VII–X** (state machine sole writer, forward-only checkpoints, transitions logged, invalid transitions raise) — **retire.** No state machine; state is files in the user's repo.
- **XI** — **retire**, superseded.

Proposed replacements, each guarding a property 2.0 actually depends on:

1. **No step performs network I/O beyond the model call.** Guards the compliance property that is now the product's main asset.
2. **No interpreter observes another interpreter's output.** Guards independence.
3. **Every verdict, count, and validation is produced by a script, never by a model.** Guards auditability — this is what makes output evidence rather than opinion.
4. **Artifacts are total or rejected.** The forcing function that replaces building.
5. **Samples are ephemeral and reproducible; never build machinery to preserve them.** The anti-pattern that produced ~9k LOC of preservation code.

---

## 9. Risks

| Risk | Severity | Handling |
|---|---|---|
| Simulated-build divergence may not track real spec defects | **High** | P7 mutation harness. This is the core product hypothesis and it is currently unproven. |
| Prose orchestration cannot guarantee a step ran | **High** | Artifact-passing (a stage's input is the prior stage's output file, so a skipped gate shows as a missing file) + non-zero-exit validators + hooks. **Cannot fully close** — this is the honest price of the architecture. |
| Self-reported blockers depend on agent introspection | **High** | An agent that silently assumes something won't report it. Divergence in the *total artifacts* is the objective backstop; do not build the report on self-reported blockers alone. |
| Integration-class defects only real building surfaces | **High** | §2's total artifacts + lenses + contract oracle recover much of it. Not all. Accepted, not solved. |
| Losing Cursor support | Medium | ⚠️ Skills and subagents are Claude Code only; `docs/IDE-SETUP.md` supports Cursor today. Either keep a thin shim or write parallel `.cursor/rules`. **Commercial decision — your call, not a technical blocker.** |
| No telemetry → slower iteration | Medium | For the compliance-sensitive buyer, not collecting telemetry is a feature. Substitute: artifacts live in the user's repo; ask design partners to share them. |
| Plugin-root path resolution for `lib/` | Medium | P0 verification. Per-skill shim as fallback. |
| Sales narrative weakens ("we build 3 prototypes") | Medium | Counter: runs on your own subscription, nothing leaves your machine, no third-party vendor, and per-requirement findings 1.0 could not produce. |

---

## 10. Decisions needed from you

1. **Ratify the planning/refine order swap** (§1) — or tell me to keep your original order and I'll note why it's weaker.
2. **Ratify Steel Commandments 2.0** (§8) — I proposed retiring all eleven and replacing them with five. That's your constitution; I won't edit it unilaterally.
3. **Cursor: keep or drop** (§9).
