---
name: specflow-compare-variants
description: Compare and assemble code from 1–3 SpecFlow-generated workspace repos. Produces per-workspace inventory, a side-by-side comparison matrix, interactive component selection, and a concrete file-level assembly plan for migration to a production repo.
argument-hint: "<ws-dir-1> [ws-dir-2] [ws-dir-3] [--target=production-dir]"
---

# SpecFlow Compare Variants

You are helping a user compare and assemble code from SpecFlow-generated workspace repos.

## Input

The user provided: `$ARGUMENTS`

Parse `$ARGUMENTS` for:
- **Workspace directories**: positional args that are NOT `--target=...` (1–3 paths, relative to cwd)
- **Target directory**: optional `--target=<path>` — the production repo destination

If no arguments are provided, scan the current directory for subdirectories that look like workspace repos (contain `backend/` or `frontend/` or `helm/`). If found, ask the user to confirm before proceeding. If nothing found, print usage and stop.

Store parsed values:
- `WORKSPACES` = list of workspace dir paths (resolved relative to cwd)
- `TARGET` = target dir path (or null if not provided)
- `COMPARE_DIR` = `.specflow-compare-variants/` in the current working directory (create if missing)

---

## Phase 1 — Inventory (parallel Haiku scouts)

Launch one Haiku scout per workspace **in parallel** (`subagent_type: "Explore"`, `model: "haiku"`). Each scout receives the workspace directory path and must produce a structured inventory card.

**Scout prompt template** (repeat for each workspace `W` at path `P`):

> Read the codebase at `P`. Produce a structured inventory card covering:
>
> 1. **Tech stack**: languages, frameworks, key libraries (backend + frontend + DB)
> 2. **Directory structure**: top-level dirs with one-line purpose each
> 3. **Schema**: list all DB tables/collections with their column/field names and types (read migration files or model definitions)
> 4. **API surface**: list all HTTP endpoints (method + path + brief purpose). Read router files.
> 5. **Key design decisions**: notable patterns (e.g. service layer architecture, auth method, ORM choice, state management)
> 6. **Test inventory**: count of unit test files, total test functions, E2E test files, Playwright test count
> 7. **Mock services**: list all in-cluster mock services (read mock-services/ or equivalent)
> 8. **Deployment**: note any unusual Helm config, resource limits, probe settings
> 9. **Code quality signals**: presence of type annotations, error handling patterns, any obvious TODOs or dead code
> 10. **Last E2E result**: check `.github/workflows/deploy.yml` last run status if inferable from git log; otherwise mark as unknown
>
> Write the card to `COMPARE_DIR/inventory-W.md` using this structure:
> ```
> # Inventory: W
> ## Tech Stack
> ## Directory Structure
> ## Schema
> ## API Surface
> ## Key Design Decisions
> ## Test Inventory
> ## Mock Services
> ## Deployment Notes
> ## Code Quality Signals
> ## E2E Status
> ```
> Be exhaustive in Schema and API Surface — these are the most important sections.

Wait for all scouts to complete before proceeding.

---

## Phase 2 — Comparison matrix (Sonnet subagent)

Launch **1 subagent** (`subagent_type: "Explore"`, `model: "sonnet"`).

**Prompt**:

> Read all inventory cards in `COMPARE_DIR/inventory-*.md`. Produce a comparison matrix at `COMPARE_DIR/comparison.md`.
>
> The matrix must cover these dimensions. For each dimension, show what each workspace chose and classify the divergence.
>
> **Divergence classes**:
> - `CONVERGENT` — all workspaces made the same choice (high confidence, take any)
> - `DIVERGENT` — workspaces differ; describe each and note tradeoffs
> - `UNIQUE` — only one workspace has this; flag if valuable
>
> **Required dimensions**:
>
> ### Schema
> For each table/model: list columns per workspace in a table. Mark added, missing, or differently-typed columns.
>
> ### API Surface
> For each endpoint family (auth, resources, etc.): list methods/paths per workspace. Mark missing endpoints and shape differences.
>
> ### Backend Architecture
> Service layering, error handling approach, async/sync patterns, dependency injection.
>
> ### Frontend Architecture
> State management choice, component hierarchy depth, routing pattern, API client approach.
>
> ### Test Coverage
> Unit test counts, E2E scenario counts per workspace. Note gaps (scenarios present in one but missing in others).
>
> ### Deployment Quality
> Probe configuration, resource limits, secret handling, idempotency of migrations.
>
> ### Code Quality
> Type annotation coverage, error handling completeness, logging presence.
>
> ### Unique innovations
> Features, safety checks, or patterns present in one workspace that others lack. Each gets a YES/NO/PARTIAL per workspace.
>
> At the end of comparison.md, write a **Recommendation** section with:
> - Suggested dominant workspace (best overall) with reasoning
> - Component-level winners: for each dimension, which workspace wins and why
> - Red flags: anything in any workspace that should NOT be included (security issue, broken pattern, dead code)

Wait for this subagent to complete before proceeding.

---

## Phase 3 — Interactive selection

Read `COMPARE_DIR/comparison.md` yourself. Then present the user with a structured summary:

```
## SpecFlow Compare Variants — Selection

### Overall recommendation: [workspace]
[1-2 sentence reasoning]

### Component winners
| Component       | Recommended | Why |
|-----------------|-------------|-----|
| Schema          | ws-X        | ... |
| API layer       | ws-X        | ... |
| Frontend        | ws-X        | ... |
| E2E tests       | ws-X        | ... |
| Helm/deployment | ws-X        | ... |

### Red flags (do NOT include)
- [list any]

### Unique innovations worth including
- [ws-X]: [what it has]

---
Accept these selections? (yes / or tell me what to change)
```

Wait for user response.

- If user says `yes` or `y` or just hits enter: proceed with recommendations as-is
- If user specifies overrides (e.g. "use ws-04-2 for frontend"): update the selections and confirm back before proceeding
- If user asks to see more detail on a specific dimension: read the relevant section from comparison.md and show it, then re-ask

Store the final selections as the **assembly plan input**.

---

## Phase 4 — Assembly plan (Opus subagent)

Launch **1 subagent** (`subagent_type: "Plan"`, `model: "opus"`).

**Prompt**:

> You are producing a concrete migration assembly plan for SpecFlow-generated code.
>
> Context:
> - Workspace inventory cards: [read all COMPARE_DIR/inventory-*.md]
> - Comparison matrix: [read COMPARE_DIR/comparison.md]
> - User selections: [list the final per-component selections from Phase 3]
> - Target: [TARGET if provided, else "new production repo"]
>
> Produce `COMPARE_DIR/assembly-plan.md` with:
>
> ## Assembly Plan
>
> ### Strategy
> [Which pattern: Dominant+patches / Best-of-class / Single workspace. Why.]
>
> ### Base workspace
> [Which workspace is the starting point]
>
> ### Component migrations
> For each component where we take from a non-base workspace:
> ```
> #### [Component name]
> Source: [workspace-dir]/[path]
> Destination: production/[path]
> Command:
>   cp -r [workspace-dir]/[path] production/[path]
> Notes: [anything the user needs to know about wiring, config changes, import updates]
> ```
>
> ### Files to exclude
> [List of files/dirs from the base workspace that should NOT be in the production repo]
> Examples: CLAUDE.md, agents/, specs/ (or move to docs/), .specflow-compare-variants/
>
> ### Config changes needed after assembly
> [List of files that need edits: env URLs, secret names, service names, repo-specific values]
>
> ### Post-assembly validation steps
> 1. [Ordered steps to verify the assembled codebase works]
>
> ### Estimated integration effort
> [Small/Medium/Large and why]
>
> ### Shell script
> At the end, write a complete executable shell script `COMPARE_DIR/assemble.sh` that:
> - Creates the production directory if TARGET was provided and dir doesn't exist
> - Copies the base workspace
> - Applies all component patches (cp commands)
> - Removes excluded files
> - Prints a summary of what was done
> The script must be idempotent (safe to re-run).

Wait for this subagent to complete.

---

## Phase 5 — Output and next steps

After the assembly plan is complete:

1. Print the assembly plan summary (Strategy + Component migrations table)
2. Print the path to `COMPARE_DIR/assemble.sh`
3. Ask the user: `Run the assembly script now? (yes/no)`
   - If yes: run `bash COMPARE_DIR/assemble.sh` and report the output
   - If no: tell them to run it manually when ready
4. Print the post-migration checklist:

```
Post-migration checklist:
[ ] Environment config adjusted (localhost → production URLs)
[ ] Secrets references updated (paths, secret names)
[ ] CI/CD pipeline adapted for the new repo
[ ] README updated with accurate setup instructions
[ ] Specs/generation artifacts cleaned up
[ ] First local build passes
[ ] Unit tests pass locally
[ ] Team review scheduled (now it's a manageable PR, not a 1000-file blast)
```

---

## Cleanup

Do NOT delete `.specflow-compare-variants/` — it is the audit trail of the comparison decision. The user can commit it to their production repo as `docs/specflow-compare-variants/` for traceability.

Print the final summary:
```
Compare variants complete.
  Workspaces compared: [N]
  Assembly strategy: [strategy]
  Output: .specflow-compare-variants/assembly-plan.md
  Script: .specflow-compare-variants/assemble.sh

Next: run the assembly script, then follow the post-migration checklist.
```
