# SpecFlow 2.0 — Plugin Skill Inventory

**Status**: DRAFT — build sheet for `plugins/specflow`
**Date**: 2026-08-03
**Companion**: `PLAN.md` (strategy). This document is the concrete skill-by-skill plan.

---

## 0. Two answers up front

**Yes, code ships with the plugin.** A skill is a *directory*, not a file — `SKILL.md` plus any assets and executables beside it. Proven in this repo: `.claude/skills/pr-loc-breakdown/` ships `count_py_loc.py` and the skill instructs the agent to run it. The marketplace plugin bundles skill directories wholesale, so scripts, JSON schemas, and lens prompts all ride along.

**The forcing function already exists.** `specflow-analysis/SKILL.md` is 488 lines of architectural dimensions framework:

- **Part A** — 6 universal dimensions, each "Pick exactly **ONE**" (persistence, infra complexity, scale, stack, quality level, scope boundaries)
- **Part B** — technology-specific dimensions by project type
- **Part C** — project-specific dimensions, explicitly headed *"Discover additional variance sources"*
- **Part D** — micro-level consistency locks, *"AGGRESSIVE ENFORCEMENT"* (naming conventions, code patterns — "Must specify ALL")

That is total-by-construction. An agent filling it cannot leave a dimension blank the way it can leave a prose blocker list incomplete. **2.0's job is not to invent this — it is to (a) replicate the fill across independent lenses, (b) make the fill machine-checkable, and (c) diff the filled values across agents.** Divergence on a locked dimension *is* a spec ambiguity, localized and named, with no scoring involved.

---

## 1. Current state

`.claude-plugin/marketplace.json` → one plugin, `./plugins/specflow`.

```
plugins/specflow/
  .claude-plugin/plugin.json          v0.1.0
  skills/specflow-analysis/           EMPTY
  skills/specflow-planning/           EMPTY
```

The real skill content lives in `mcp_server/services/skills/` (1108 lines across four skills), loaded at runtime by `_make_prompt_text()` with `<<PLACEHOLDER>>` substitution and returned as MCP tool output. **The migration is: those SKILL.md files move into the plugin and become directly invocable, and the MCP indirection goes away.**

| Existing skill | Lines | 2.0 disposition |
|---|---:|---|
| `specflow-analysis` | 488 | **Keep + extend.** The dimensions framework is the asset. Add machine-checkable output. |
| `specflow-planning` | 209 | **Keep + rework.** Role inverts — see §3.2. |
| `specflow-compare-variants` | 255 | **Repurpose** → `specflow-report` (§3.7). Reads 1.0 variance reports today. |
| `specflow-diagnose` | 156 | **Retire.** Reads backend failure state; there is no backend. |

⚠️ **`recommended-models` frontmatter goes inert.** Both existing skills list non-Anthropic models (`openai/gpt-5.3-codex`) for the backend's OpenRouter routing. Subagents are Claude-only, so these become advisory comments. This is the concrete form of the multi-vendor-diversity loss — adversarial lenses replace it (§3.3).

---

## 2. Target plugin layout

```
plugins/specflow/
  .claude-plugin/plugin.json
  lib/                                  # shared oracles — ONE copy, no duplication
    schema/
      interpretation.schema.json         # the per-lens artifact
      dimensions.schema.json             # Parts A–D as a machine-readable schema
      blocker.schema.json
    validate_artifact.py                 # JSON Schema conformance
    check_totality.py                    # every dimension filled, every matrix cell present
    contracts_oracle.py                  # SQL DDL / OpenAPI / type-def validation
    concordance.py                       # anchor-scoped cross-lens agreement
    rank_blockers.py                     # cost-asymmetry ordering + dedup vs resolved
    saturation.py                        # stop rule
  skills/
    specflow-analysis/       SKILL.md
    specflow-planning/       SKILL.md
    specflow-refine/         SKILL.md  lenses/*.md
    specflow-simulate/       SKILL.md
    specflow-resolve/        SKILL.md
    specflow-contracts/      SKILL.md
    specflow-report/         SKILL.md
```

**Why `lib/` and not per-skill scripts:** `concordance.py` is needed by `specflow-refine` and `specflow-report`; `validate_artifact.py` by four skills. Copies drift — the CLAUDE.md single-source-of-truth rule applies to shipped scripts exactly as it does to backend code.

⚠️ **Open item before P1: verify how a skill resolves a path to its plugin root.** Skills reference sibling assets, but I have not confirmed the supported mechanism for reaching *above* the skill directory to `lib/`. Options, in order of preference: a plugin-root environment variable if one is exposed; a documented relative path; or a tiny per-skill shim that imports from a single implementation. **Do not build on an assumed variable — check the plugin docs first.** This is a 15-minute verification that shapes the whole layout.

---

## 3. Skill-by-skill

### 3.1 `specflow-analysis` — keep, extend

*Description*: Analyze spec completeness locally — gap detection across all architectural dimensions.

Unchanged in intent. Two additions:

- **Emit the filled dimensions as JSON alongside the markdown.** Today Part A–D output is prose in `specification_completeness.md`. 2.0 needs it machine-readable so `check_totality.py` can verify completeness and `concordance.py` can diff it across lenses. Markdown stays for humans; JSON is the contract.
- **Run `check_totality.py` before declaring done.** Currently "fill every dimension" is an instruction the model may partially satisfy. A script that exits non-zero on a blank turns it into a gate.

Part F (`INTEGRATION_TESTS_READY` / `LOCAL_ONLY`) can go — it exists to tell the backend whether to run E2E.

### 3.2 `specflow-planning` — keep, rework

*Description*: Create a phased implementation plan from specs and analysis output.

**The role inverts.** In 1.0 this produced *the one plan* every workspace implemented — which is why 1.0's variance was confounded (`sync_plan_to_workspaces`, `workflow_steps.py:700`, copied one plan to all N). In 2.0 planning is one of the **replicated** behaviours: each lens plans independently, and disagreement about phase decomposition is itself a signal that the spec underdetermines the build.

Keep the phase-sizing discipline and the locked-values sections — they're good. Drop the single-artifact assumption and the `applicable_agent_mcps` annotation (no backend to consume it).

### 3.3 `specflow-refine` — NEW. The product.

*Description*: Autonomously refine a specification — simulate building it across independent adversarial lenses, then resolve the blockers with you.

The orchestrator and the main entry point. Owns the round loop:

1. Spawn N lens subagents **in a single message** so they run concurrently, blind to each other.
2. Run the oracles: `validate_artifact.py`, `check_totality.py`, `contracts_oracle.py`.
3. `concordance.py` → `rank_blockers.py`.
4. Hand off to the HITL gate (`AskUserQuestion`, native — supports multiSelect and previews).
5. `saturation.py` → loop or stop.

**Lenses ship as `lenses/*.md` assets, not as separate skills.** They aren't user-invocable — nobody types "run the idempotency lens." Marketplace entries should be things a user would actually invoke; internal prompts are assets. Initial set:

| Lens | Attacks |
|---|---|
| `concurrency.md` | simultaneous access, races, lock scope |
| `partial-failure.md` | half-completed operations, compensations, retries |
| `data-lifecycle.md` | migration, retention, deletion, backfill |
| `auth-boundaries.md` | who can do what to whose data |
| `idempotency.md` | replay, duplicate delivery, at-least-once |
| `ordering.md` | sequence assumptions, out-of-order arrival |

These are the failure classes physical building surfaced and naive simulation misses. Lens count is the cost dial.

Each lens produces the same **total** artifact — filled dimensions, state transition table, failure-mode matrix, blockers with spec anchors. Same schema, different attack angle, so the outputs are diffable.

### 3.4 `specflow-simulate` — NEW

*Description*: Simulate building your spec and report what would block you. Single pass, no loop.

Standalone single-lens run. Users will want the cheap answer without committing to the full loop, and it's the natural first-touch skill — low cost, immediate value, demonstrates the product. Shares the artifact schema and validators with `specflow-refine`, so nothing is duplicated.

### 3.5 `specflow-resolve` — NEW

*Description*: Walk through ranked spec blockers and write the decisions back into your specification files.

Separate from finding blockers, because *applying* decisions is an edit operation with its own hazards:

- Don't clobber the user's prose — insert, annotate, or extend.
- Keep traceability: each written decision records which blocker it resolves and which lenses raised it.
- Record resolutions where `rank_blockers.py` can dedup against them, so later rounds don't re-ask.
- **Prefer proposing over asking**: "I'll assume X unless you object" clears most items at near-zero human cost. Reserve blocking questions for consequential forks.

This is where the "autonomous refinement" promise is actually kept — a loop that only *reports* blockers leaves all the work with the user.

### 3.6 `specflow-contracts` — NEW

*Description*: Generate the data model and API contract from your spec as real schemas, then validate them.

Keeps the compiler and drops the application. The LLM emits SQL DDL / OpenAPI / type definitions; `contracts_oracle.py` runs **real validators** on them. Near-zero cost, no runtime, no deploy — but a genuine oracle rather than a model's opinion. Spec contradictions frequently surface as schema-level impossibilities (a field that must be both required and derived, a relation that must be both 1:1 and 1:N).

Independently useful outside the loop, which is why it's its own skill.

### 3.7 `specflow-report` — repurpose `specflow-compare-variants`

*Description*: Show the current refinement state — resolved requirements, open blockers, ranked.

The existing 255-line skill reads 1.0's variance reports. Retarget it at the 2.0 artifacts: per-requirement status, what's still open, which lenses disagree and how.

**Reports counts, never a score.** "7 open blockers, 3 high-impact" is derived from observation and is trackable across projects. A composite readiness number would launder judgment as measurement.

### 3.8 `specflow-diagnose` — retire

Reads backend failure state (checkpoints, workspace status, agent errors). All of that disappears. Nothing to salvage.

### 3.9 `specflow-mutate` — NEW, **internal only**

*Description*: Inject known ambiguities into a spec and verify the refinement loop detects and localizes them.

The instrument-validation harness from `PLAN.md` §7. **This belongs in the repo's own `.claude/skills/`, not the published plugin** — it's a QA tool for us, not a customer feature, and shipping it invites confusion about what the product does.

---

## 4. Published footprint

| Skill | Status |
|---|---|
| `specflow-analysis` | extend |
| `specflow-planning` | rework |
| `specflow-refine` | new — entry point |
| `specflow-simulate` | new |
| `specflow-resolve` | new |
| `specflow-contracts` | new |
| `specflow-report` | repurposed |

**7 published skills, 4 net new.** Plus `specflow-mutate` internal and `specflow-diagnose` retired.

`plugin.json` needs `version` → `0.2.0` and updated `keywords` (`spec-refinement`, `blocker-detection` alongside the existing `spec-analysis`, `implementation-planning`).

---

## 5. Build order

Each phase leaves the plugin installable and the previous phases working.

| Phase | Work | Exit criterion |
|---|---|---|
| **P0** | Verify the plugin-root path mechanism (§2). Move the four existing SKILL.md files from `mcp_server/services/skills/` into `plugins/specflow/skills/`. Resolve `<<PLACEHOLDER>>` substitution — skills take arguments directly, so the MCP substitution layer isn't needed. | `/specflow-analysis` runs from the installed plugin with no MCP server |
| **P1** | `lib/schema/*.json` + `validate_artifact.py` + `check_totality.py`. Extend `specflow-analysis` to emit JSON and call the totality gate. | Totality check rejects a deliberately-blank dimension |
| **P2** | `specflow-simulate` + the six lens prompts. Single-lens end-to-end on a real spec. | Artifact validates; blockers carry spec anchors |
| **P3** | `contracts_oracle.py` + `specflow-contracts`. | Catches a planted spec contradiction as a schema impossibility |
| **P4** | `concordance.py` + `rank_blockers.py` + `specflow-refine` (fan-out, no loop yet). | N lenses run concurrently; blockers ranked and deduped |
| **P5** | `specflow-resolve` + the `AskUserQuestion` gate. | A human resolves ranked blockers and the specs are updated with traceability |
| **P6** | `saturation.py` + the round loop in `specflow-refine`. `specflow-report`. | Loop terminates on saturation, not a fixed count |
| **P7** | `specflow-mutate` (internal). | Injected ambiguity detected **and localized** to the mutated requirement |
| **P8** | Rework `specflow-planning` for per-lens replication. Retire `specflow-diagnose`. Bump to `0.2.0`. | Marketplace install gives the full 2.0 experience |

**P2 and P7 are the gates.** P2 proves the economics on a real spec; P7 proves the loop detects anything real. Both land before any backend deletion — that stays sequenced in `PLAN.md` §11 and should not start until P7 is green.

---

## 6. Risks specific to the skill build

| Risk | Severity | Handling |
|---|---|---|
| Plugin-root path resolution for `lib/` may not work as assumed | Medium | **P0 verification.** Reshapes §2 if it fails; fallback is a per-skill shim over one implementation. |
| Prose can't guarantee a skill ran its validator | **High** | Make the validator's output a required input to the next step, so a skipped gate is visible in a missing file rather than silently absent. Cannot fully close — see `PLAN.md` §8. |
| 488-line `specflow-analysis` grows past useful size when extended | Medium | Move the dimensions framework into `lib/schema/dimensions.schema.json` as the source of truth and have SKILL.md reference it, rather than restating it in prose. Shrinks the skill and makes the framework machine-checkable in one move. |
| Seven skills crowd the marketplace listing | Low | `specflow-refine` is the documented entry point; the rest are described as steps you can also run individually. |
| Lens prompts drift from the shared artifact schema | Medium | Schema lives in `lib/schema/`; every lens references it and `validate_artifact.py` enforces it. |
