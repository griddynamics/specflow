# Skills Comparison: SpecFlow Skills vs. CTO Marketplace Estimator Skills

Comparison of five skills across two ecosystems, produced to support onboarding the presales
team (currently using CTO Marketplace) onto SpecFlow Skills and the SpecFlow harness.

**Compared skills:**
- **A — SpecFlow set** (this repo): `mcp_server/services/skills/specflow-analysis/SKILL.md`,
  `mcp_server/services/skills/specflow-planning/SKILL.md`,
  `mcp_server/services/skills/specflow-compare-variants/SKILL.md`
- **B — CTO Marketplace**: `gd-scope-estimator/SKILL.md`
- **C — CTO Marketplace**: `gd-presales-estimator/SKILL.md`

**Dimensions requested:**
1. Domain, scope, applicability, use cases
2. Workflow steps and size
3. Results — outputs and consumers
4. Workflow structure — nesting, subagents, HITL
5. Quality — noise, redundancy, agent-readiness

---

## Framing

These are two unrelated ecosystems that happen to both start with "read a spec, find gaps":

- **SpecFlow set (A)** — feeds an *autonomous coding harness*. Every artifact is contract-validated
  by `backend/app/services/contract_validator.py` and consumed by machines (`run_generation`,
  phase agents), not humans reading a proposal.
- **CTO Marketplace pair (B, C)** — feeds a *human business decision*. Artifacts are read by
  presales/delivery people and eventually a client, in Work Units and man-months, never touching
  a coding agent.

## 1. Domain, scope, applicability, use cases

| Skill | Domain | Scope | Applicability | Primary use case |
|---|---|---|---|---|
| **specflow-analysis** | Requirements/architecture completeness | Single spec tree → gap report | Any SpecFlow project, pre-codegen | Gate: "is this spec deterministic enough that 3 teams would build the same thing?" |
| **specflow-planning** | Implementation phase design | Spec + analysis → phased plan | Any SpecFlow project, pre-codegen | Produce the exact phase list autonomous coding agents will execute |
| **specflow-compare-variants** | Post-hoc code reconciliation | 1–3 *generated* repos → assembly plan | Only after parallel SpecFlow generation runs exist | Pick best-of-breed components across variant runs, migrate to a production repo |
| **gd-scope-estimator** | Business scoping & sizing | Spec/brief → WU estimate | Any software project needing a sizing decision (not tied to SpecFlow at all) | "How big is this?" for budget/planning decisions |
| **gd-presales-estimator** | Proposal/SOW generation | WU estimate → man-months, team, timeline | Client-facing bids, SOWs, staffing plans | Turn an internal WU estimate into a document a client or PM can act on |

Note the scope jump: A's three skills are strictly pre-codegen/post-codegen bookends around
SpecFlow's own execution; B/C never touch code generation — they exist upstream of *any* delivery
decision, SpecFlow or not.

## 2. Workflow steps & size

| Skill | Structure | Size (lines) | Passes |
|---|---|---|---|
| specflow-analysis | Single-pass, no phases | ~489 | 1 (with optional index sub-step) |
| specflow-planning | Single-pass, no phases | ~210 | 1 |
| specflow-compare-variants | 5 explicit phases | ~256 | Multi-agent pipeline (Haiku ×N → Sonnet → Opus), one HITL gate |
| gd-scope-estimator | 3 phases + a "2e" specialist-lens pass, fully resumable | ~300 (+ external templates/references) | Multi-session, state persisted to `.estimation/` |
| gd-presales-estimator | Phase 0 (verify/invoke dependency) + Phase 1 (6 sub-steps) | ~194 (+ external references) | Single session, but conditionally invokes all of B first |

B/C externalize bulk into `assets/`/`references/` loaded on demand ("Loading discipline" tables) —
a materially better context-economy pattern than A1/A2, which inline the entire framework every
run.

## 3. Results — outputs and consumers

| Skill | Output file(s) | Consumer |
|---|---|---|
| specflow-analysis | `analysis/specification_completeness.md` (+ optional index/repo_summary) | Machine: `specflow-planning` skill, then backend contract validator |
| specflow-planning | `planning/IMPLEMENTATION_PLAN.md`, `planning/e2e-test-plan.md` | Machine: `run_generation`'s phase agents |
| specflow-compare-variants | `comparison.md`, `assembly-plan.md`, `assemble.sh` | Human: engineer doing manual migration into a production repo |
| gd-scope-estimator | `scope.md`, `context.md`, `breakdown.md`, `questions.md`, `estimate.md` | Human: delivery/presales team; also machine-consumed by `gd-presales-estimator` |
| gd-presales-estimator | `project-planning.md` | Human: proposal writer, ultimately client-facing |

This is the sharpest distinction: **A1/A2 outputs are validated by code and gate an automated
pipeline** (wrong filename literally causes `ANALYSIS_MISSING`/`PLAN_MISSING` rejections per this
repo's CLAUDE.md contract). **B/C/A3 outputs are read by people** — no downstream machine enforces
their shape.

## 4. Workflow structure — nesting, subagents, HITL

| Skill | Subagents | HITL | Persistence |
|---|---|---|---|
| specflow-analysis | None | None — fully autonomous single pass | Stateless, re-runnable |
| specflow-planning | None | None — fully autonomous single pass | Stateless, re-runnable |
| specflow-compare-variants | Yes — Haiku scouts (parallel) → Sonnet matrix → Opus assembly plan | Yes — explicit accept/override gate (Phase 3), execute-or-not gate (Phase 5) | `.specflow-compare-variants/` audit trail |
| gd-scope-estimator | None | Yes — validation gate at end of every phase (1c, 2d, 2e specialist pass) | `.estimation/<slug>/` resumable across sessions, plus shared team-wide catalogs |
| gd-presales-estimator | None (skill-calls-skill: invokes gd-scope-estimator wholesale if prerequisites missing) | Yes — Phase 0d raises engagement-model questions before calculating | Writes into the same `.estimation/<slug>/` folder |

specflow-compare-variants is architecturally the closest of the five to a proper multi-model
Workflow (fan-out scouts → synthesis → judgment → execution gate). A1/A2 are deliberately
HITL-free — they're meant to run unattended inside an IDE turn. B is the most session-resilient
design (explicit resume protocol, shared catalogs edited by a team over time).

## 5. Quality — noise, redundancy, agent-readiness

- **specflow-analysis**: Thorough to the point of heavy repetition — Parts A–F dimensions are
  restated in the framework, then again in "Dimension Discovery Checklist," then again in required
  output structure. Likely intentional (it's a "BLOCKING REQUIREMENT" enforcement skill), but at
  489 lines inlined every run it's the least context-economical of the five. Deterministic
  classification (Part F) is a good pattern — reduces model judgment variance.
- **specflow-planning**: Well-scoped, concrete anti-pattern examples (❌/✅), hard numeric limits
  (2-3 tasks, 8-10 files/phase) — good agent-readiness, low ambiguity. Some duplicated boilerplate
  with A1 (both restate the same arguments/file-contract block).
- **specflow-compare-variants**: Cleanest of the three — subagent prompts are literal,
  copy-pasteable templates, which minimizes dispatch ambiguity. Minor looseness: the "no
  arguments → scan cwd for `backend/`/`frontend`/`helm`" heuristic is a soft guess rather than a
  deterministic check.
- **gd-scope-estimator**: Best context-discipline of all five (explicit "Loading discipline"
  table, seeding protocol for shared catalogs, don't-scaffold-empty-files rule). Redundant
  emphasis on "never silently assume" recurs across phases, but that's a deliberate guardrail, not
  sloppiness.
- **gd-presales-estimator**: Tightest skill of the five — properly delegates to B instead of
  duplicating scope-completeness logic (good DRY), fails loud with a specific missing-files
  message rather than guessing.

## Overlaps — what's actually redundant vs. just adjacent

1. **specflow-analysis vs. gd-scope-estimator** — both ask "what's missing from this spec," but
   they check *entirely different dimensions* (architectural determinism: persistence/infra/stack/
   naming conventions vs. business scope: ownership, ceremony, third-party seams, contract
   conflicts) and gate *different systems* (autonomous codegen determinism vs. human cost
   estimation). **Not redundant** — but worth flagging explicitly to presales so they don't assume
   a `specification_completeness.md` "READY" verdict says anything about cost/timeline. It says
   nothing about effort size at all.
2. **specflow-planning vs. gd-scope-estimator/gd-presales-estimator** — both "break the work
   down," but A2's phases carry no effort unit (no WU, no time estimate) — they're sized in
   files/tasks/commits for an agent's working-memory limit, not effort. **There is currently no
   bridge**: nothing converts `IMPLEMENTATION_PLAN.md` phases into WU that `gd-presales-estimator`
   could consume. If presales wants to use SpecFlow-generated plans as the basis of an estimate,
   that conversion doesn't exist yet.
3. **specflow-compare-variants** has no CTO Marketplace analogue — it's unique to reconciling
   parallel SpecFlow generation runs.
4. **gd-scope-estimator vs. gd-presales-estimator** are not overlapping — they're a designed
   pipeline (C invokes B if prerequisites are missing), the cleanest relationship in the set.

## For the presales onboarding question

Presales' current tool (B→C) answers "how much will this cost, who staffs it, can we hit the
date" — SpecFlow's tools (A1→A2) answer "is this spec unambiguous enough to hand to autonomous
coding agents." They're complementary, not substitutes: onboarding presales to SpecFlow doesn't
replace B/C, since nothing in A converts to WU/man-months today. If the intent is for presales to
eventually estimate *from* a SpecFlow plan, that requires a new bridge skill
(`IMPLEMENTATION_PLAN.md` phases → WU-sized work items) — currently a gap, not an existing
overlap.

---

## Collaboration — sequencing both toolchains on one customer spec

**Question: could a manager take a customer specification and use both SpecFlow and CTO
Marketplace skills, in sequence, on the same deal? Yes — as a manual, human-sequenced relay
across two distinct moments in the deal lifecycle, not as an automated pipeline.** A manager reads
the same customer specification twice, through two unrelated toolchains: first CTO Marketplace, to
decide whether and how much to bid; then SpecFlow, to prepare the same spec for autonomous code
generation once the deal is won. Nothing today carries data automatically between the two.

### The sequence

**Input**
0. Customer sends a specification / brief / RFP.

**Presales phase — "should we bid, and for how much?" (CTO Marketplace)**
1. `gd-scope-estimator` — scopes the customer spec into a validated breakdown and a Work-Unit estimate.
2. `gd-presales-estimator` — turns the estimate into man-months, team plan, timeline verdict, and a proposal document.
3. **Decision: is the deal won?** No → engagement ends here. Yes → continue to delivery.

**Manual handoff (no automatic data transfer)**
The manager re-supplies the *same* spec to SpecFlow — there is no automatic conversion. Any
in/out-of-scope calls already made in `scope.md` §6a/§6b should be carried over by hand into
specflow-analysis's Part A6 (Scope Boundaries), or architecture locking may silently re-open
decisions already agreed with the customer.

**Delivery phase — "prepare the same spec for autonomous codegen" (SpecFlow)**
4. `specflow-analysis` — gap-checks the same spec across all architectural dimensions; a different completeness question than `gd-scope-estimator` asked.
5. `specflow-planning` — turns the locked spec into a phased implementation plan for autonomous coding agents.
6. `run_generation` (backend, not a skill) — autonomous multi-hour codegen + deploy + E2E loop.
7. `specflow-compare-variants` *(optional)* — only if generation was run as several parallel variants; reconciles and assembles the best of each.

**Output**
8. Delivered codebase → customer.

### What a manager should double-check manually

The sequence above is real and usable today, but two points are not automatic and are the
manager's responsibility to bridge:
- **The handoff between step 3 and step 4** — re-supplying the spec and manually carrying forward
  scope decisions (see above).
- **Step 7 is conditional** — only relevant if the delivery team chose to run parallel generation
  variants; skip it for a single-workspace run.

Everything else in the sequence is a normal, independent invocation of an existing skill — no new
tooling is required to run both toolchains back to back, only manual diligence at the one junction
where they meet.
