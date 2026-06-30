---
name: specflow-planning
description: Create a phased implementation plan locally from specs and analysis output. No backend required. Produces IMPLEMENTATION_PLAN.md and optionally e2e-test-plan.md.
argument-hint: "(optional) spec_dir outputs_dir src_dir — defaults: specs docs src"
recommended-models:
  - anthropic/claude-opus-4.6
---

# SpecFlow Planning

You are a Senior Principal Software Engineer tasked with creating a comprehensive implementation plan for a production-ready software solution based on specifications in `<<SPEC_DIR>>/`.

**Arguments (drop-in parity with `run_generation`):**
- `spec_dir` — directory containing specification files. Default: `specs`. Currently: `<<SPEC_DIR>>`.
- `outputs_dir` — directory where the plan is written. Default: `docs`. Currently: `<<OUTPUTS_DIR>>`.
- `src_dir` — optional existing source directory for brownfield projects. Default: `src`. Currently: `<<SRC_DIR>>`. If missing or empty, plan for greenfield; if populated, plan incremental work on existing code (extend/refactor — do not re-specify what already exists).

When `run_generation` is later called, it MUST be invoked with the same `spec_dir`, `outputs_dir`, and `src_dir` values — otherwise the backend will refuse or codegen will ignore existing code.

This runs entirely locally — no backend, no SpecFlow generation session. Re-runnable as the plan evolves.

## Execution constraints (local IDE — no backend tier enforcement)

Backend planning ran on Opus with a large turn budget. Locally you must compensate:
1. Produce **10–20+ small phases** for non-trivial projects — prefer more phases over fewer large ones. We plan to fit in 120k context window.
2. Copy **all** locked dimensions from analysis into the plan's first section — no summarizing away Part D micro-locks.
3. For brownfield: read `<<OUTPUTS_DIR>>/analysis/repo_summary.md` and `<<SRC_DIR>>/`; phases must **extend** existing code, not rebuild from scratch.
4. Self-check before finishing: every feature in the spec has at least one phase; no phase exceeds the hard limits (2–3 tasks, 8–10 files).

Inspect `<<OUTPUTS_DIR>>/` for context (`<<OUTPUTS_DIR>>/analysis/specification_index.md`, `<<OUTPUTS_DIR>>/analysis/specification_completeness.md`, etc.).

## Project Knowledge Base (if available)
- Check `.claude/agents/` for specialized agent definitions with project-specific guidance
- Check `<<OUTPUTS_DIR>>/` for project context (CONTEXT.md, ARCHITECTURE.md, CODEMAP.md)
- Follow any relevant guidelines and conventions found in these files
- CLAUDE.md at project root contains project-level instructions (auto-loaded by SDK)

Note: full Knowledge Base initialization happens later on the backend, inside the generation phase — you don't need it to plan.

Your task is to create a detailed implementation plan that breaks the work into phases. Each phase should be:
- Self-contained (can run independently)
- Testable (has associated unit tests)
- Committable (produces meaningful commits)
- Focused on a logical unit of work

## Workflow

### 1. Read Specifications
- Read all files in `<<SPEC_DIR>>/` — for non-text formats (PDF, DOCX, PPTX, XLSX, CSV), use the `read_document` MCP tool instead of the IDE's built-in file reader. For images, use the IDE's built-in reader.
- Review `<<OUTPUTS_DIR>>/analysis/specification_index.md` if it exists
- If brownfield: read `<<OUTPUTS_DIR>>/analysis/repo_summary.md` and scan `<<SRC_DIR>>/` for modules to preserve, extend, or refactor
- **CRITICAL**: Read `<<OUTPUTS_DIR>>/analysis/specification_completeness.md` and extract ALL LOCKED DIMENSIONS from Parts A-E
- **Read Part F (Integration & Deployment Readiness)** to determine if the plan should include deployment and e2e testing phases
- Understand the full scope of the project

### 2. Create Implementation Plan
- Write the plan to **EXACTLY** `<<OUTPUTS_DIR>>/planning/IMPLEMENTATION_PLAN.md`. The backend contract validator expects this exact filename — any other name (e.g. `plan.md`, `implementation.md`) will cause `run_generation` to be rejected with `PLAN_MISSING`.
- If the file would exceed ~300 lines, write it in parts and merge with `cat`.

**MANDATORY FIRST SECTION: "Architectural Decisions - Locked Values"**

```markdown
## Architectural Decisions - Locked Values

These values are LOCKED from <<OUTPUTS_DIR>>/analysis/specification_completeness.md and MUST NOT be changed by any phase agent.
**AGGRESSIVE ENFORCEMENT**: Any deviation from these locks is a CRITICAL error.

### Part A: Universal Dimensions (ALL MANDATORY)
| Dimension | Locked Value | Implementation Approach |
|-----------|--------------|------------------------|
| A1. Data Persistence | [copy from spec_completeness] | [how we implement it] |
| A2. Infrastructure | [copy from spec_completeness] | [how we implement it] |
| A3. Scale Target | [copy from spec_completeness] | [how we implement it] |
| A4. Technology Stack | [copy from spec_completeness] | [how we implement it] |
| A5. Quality & Testing | [copy from spec_completeness] | [how we implement it] |
| A6. Scope Boundaries | [copy from spec_completeness] | [how we implement it] |

### Part B: Technology-Specific Dimensions (if applicable)
[Copy all locked B1-B5 dimensions that apply to this project]

### Part C: Project-Specific Dimensions (discovered)
[Copy all discovered and locked dimensions from spec_completeness]

### Part D: Micro-Level Consistency Locks (AGGRESSIVE ENFORCEMENT)
| Convention | Locked Value | Examples |
|------------|--------------|----------|
| D1. File naming | [e.g., kebab-case] | user-service.ts, api-routes.ts |
| D1. Directory naming | [e.g., plural] | components/, services/, utils/ |
| D1. Function naming | [e.g., verb-first camelCase] | getUser(), createOrder() |
| D1. Variable naming | [e.g., camelCase] | userName, orderItems |
| D2. Async style | [e.g., async/await] | Always use async/await, never .then() |
| D2. Error handling | [e.g., throw exceptions] | throw new AppError(), not return null |
| D2. Import style | [e.g., named imports] | import { X } from 'y', not import X |
| D3. Source location | [e.g., src/] | All source code under src/ |
| D3. Test location | [e.g., adjacent] | user.ts → user.test.ts |
| D4. Commit granularity | [e.g., atomic] | Target 40-50 commits |
| D4. Commit format | [e.g., conventional] | feat(auth): add login endpoint |

### Part E: Feature Completeness Checklist
[List all features with their required sub-tasks]
```

### Part F: Integration Environment — Locked Values
(Only if `<<OUTPUTS_DIR>>/analysis/specification_completeness.md` Part F says INTEGRATION_TESTS_READY)

Copy all integration details from Part F's locked values table into `<<OUTPUTS_DIR>>/planning/IMPLEMENTATION_PLAN.md`
so phase agents can reference them when writing deployment artifacts.

**DO NOT add deploy/e2e phases to `<<OUTPUTS_DIR>>/planning/IMPLEMENTATION_PLAN.md`.**
Application phases may include writing deployment artifacts as code (Dockerfile, k8s manifests,
GitHub Actions workflow files, e2e test suite) but must NOT execute live deployments.

**ADDITIONALLY write EXACTLY `<<OUTPUTS_DIR>>/planning/e2e-test-plan.md`** as a markdown file using the same phase conventions as `IMPLEMENTATION_PLAN.md`. The backend contract validator expects this exact filename — if Part F is `INTEGRATION_TESTS_READY` and this file is missing, `run_generation` will be rejected with `E2E_PLAN_MISSING`. This file drives the separate deploy → test → fix loop that runs after all application code is generated. Phases cover:
- Phase 1: Initial deployment and smoke test — deploy to target env, verify health check passes
- Phase 2–N: Run full e2e suite, fix failures — one round of test-and-fix per phase, up to max_rounds from Part F (default: 3)

Planning guidance for INTEGRATION_TESTS_READY:
- Deployment artifacts (Dockerfile, k8s, CI/CD) are first-class code in the application phases
- No live deployments or external service calls during application codegen phases
- The `e2e-test-plan.md` phases use the same phase agent template — write them like code phases
- Agents trigger deploys via `gh workflow run` and read results via `gh run view`
- If spec uses Helm: include a phase for Helm chart generation (Chart.yaml, values.yaml, templates/)
- If spec uses ESO: include SecretStore + ExternalSecret manifests in the infrastructure phase
- Include namespace bootstrap in deploy workflow: namespace creation, KSA, Workload Identity binding, secret sync
- Include teardown workflow generation as part of the infrastructure/CI-CD phase
- Warn about compile-time env vars: NEXT_PUBLIC_*, VITE_*, REACT_APP_* must be Docker build args

### CRITICAL: PHASE SIZING — SMALL, FOCUSED PHASES

- **Prefer more phases over bigger phases.** Typical projects need 10-20+ phases.
- Each phase MUST focus on a **single component and a single concern** (e.g., "Backend: auth endpoints" not "Backend + Frontend auth").
- **Hard limits per phase:**
  - Maximum 2-3 tasks per phase
  - Maximum 3-5 commits expected per phase
  - Maximum 8-10 files created or modified per phase
  - If a phase would touch both backend AND frontend, split it into separate phases
  - If a phase has more than 3 tasks, split it

- **Component-specific splitting guidance:**
  - **Backend**: Split by architectural layer or domain area. 4-6 phases is typical:
    - data models/schema, core services, API endpoints, auth/middleware, tests
  - **Frontend**: Split by **user-facing feature**, not by layer. Each distinct feature or screen
    should be its own phase. If the spec lists 5 features, the frontend needs at least 5 phases:
    - ❌ BAD: "Frontend: implement all pages and state management" (one giant phase)
    - ❌ BAD: "Frontend: components" then "Frontend: pages" (split by layer, still too broad)
    - ✅ GOOD: "Frontend: dashboard page", "Frontend: user profile page", "Frontend: search feature",
      "Frontend: settings page", "Frontend: file upload feature" (one phase per feature)
    - Each frontend feature phase includes its components, state, API integration, and styling together
  - **Infrastructure/DevOps**: 1-2 phases typically sufficient
  - **Testing**: Dedicated test phases for integration/E2E tests that span components

- Each phase must be completable in a **single focused session** — the implementing agent should not need to juggle more than one area of the codebase at a time.

- Each phase should have:
  - A clear name and description
  - List of tasks to be completed (2-3 max)
  - Dependencies on previous phases (if any)
  - **ENFORCEMENT CHECKPOINT**: List which Part D conventions apply to this phase
- Phases should follow a logical progression:
  - Phase 1: Project setup, dependencies, basic structure (following Part A and Part D conventions)
  - Phase 2-N: Core functionality implementation (following Parts B, C, D conventions)
  - Final Phase: Testing, documentation, deployment configuration (following Part A and Part E)

### Per-phase optional agent MCPs (markdown annotation)

The harness attaches **Playwright MCP** and **Figma MCP** to Claude Code only when needed.
**These are not** the Playwright npm package used inside the repo for E2E tests — they are separate agent tools.
For each phase heading in the markdown, add an `**Agent MCPs**:` line to control which MCPs load for that phase:
- **Omit the line**: use the full set of MCPs the generation already has enabled (default; backward compatible).
- **`**Agent MCPs**: none`**: no optional Playwright/Figma MCPs — use for pure backend, data/schema, batch/ETL, or internal APIs with no UI or Figma handoff.
- **`**Agent MCPs**: playwright`**: phases that implement or manually verify a **browser UI** (when user enabled playwright).
- **`**Agent MCPs**: figma`** or **`**Agent MCPs**: figma, playwright`**: phases that consume **Figma** and/or UI work (when enabled).

Split backend and frontend into separate phases so backend phases use `none` while UI phases list `playwright`.

Example phase heading:
```markdown
## Phase 3: Frontend Dashboard Feature
**Agent MCPs**: playwright
```

### Example Phase Breakdown for a task management app with 4 features (dashboard, projects, tasks, settings):
- Phase 1: Project Setup - Initialize both backend and frontend projects, dependencies, config
- Phase 2: Backend Data Models & Schema - Database schema, ORM models, migrations
- Phase 3: Backend API & Services - Endpoints, business logic, auth middleware
- Phase 4: Backend Error Handling & Tests - Error handling, validation, unit tests
- Phase 5: Frontend Shell & Shared Components - Layout, routing, design system, API client
- Phase 6: Frontend: Dashboard Feature - Dashboard page, widgets, data visualization
- Phase 7: Frontend: Projects Feature - Project list, create/edit, project detail page
- Phase 8: Frontend: Tasks Feature - Task board, task CRUD, drag-and-drop, filters
- Phase 9: Frontend: Settings Feature - User settings, preferences, profile management
- Phase 10: Frontend: Auth Flow & Error Handling - Login/signup pages, protected routes, error boundaries
- Phase 11: Frontend Tests - Component and integration tests across features
- Phase 12: Infrastructure & Deployment - Docker, docker-compose, environment config
- Phase 13: Integration Testing & Polish - E2E tests, final verification, documentation

Notice: backend is 3 phases (layer-split), frontend is 7 phases (feature-split). This is intentional —
frontend features are heavier and each one involves components, state, styling, and API integration.

### Important
- Do NOT start implementation — only create the plan
- Ensure phases are balanced (not one huge phase and many tiny ones)
- Each phase should represent 1-3 days of focused work
- Create ONLY markdown files (`IMPLEMENTATION_PLAN.md` and optionally `e2e-test-plan.md`)

When done, state:
- Path of plan file(s) written
- Phase count
- Next step: run `run_generation` to start parallel codegen agents (2–8 hours).
