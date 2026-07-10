# CLAUDE.md — Development Instructions

## Project

AI SDLC accelerator. An agent harness that automates generation, deployment, and testing of full-stack codebases via Claude Code SDK on persistent NFS workspaces; measures complexity (P10Y/Compass ML) and produces variance-reduced estimates from parallel runs.

Backend: backend
MCP server: mcp_server

**Business constraints**: All external deps mocked. Sandboxed agents, no credentials. No infra provisioning, no customer system access, no deploys during generation.

**Key technical decisions**: K8s over Cloud Run (8+ hr tasks). Firestore (distributed locking, crash recovery). NFS/Filestore (git perf, persistence). State machines (workspace safety). Workspace pool (isolation, P10Y per-repo).

**Coding patterns**: TDD. Small functions. Do Not Repeat Yourself. Maximize code reuse. Patterns should enforce compile time and unit tests validation. Use Pydantic, dataclasses, OOP over simple Python collections and raw strings. Always use Enums, SRP and Open/Close principle to have precise changes.
Imports always on top of file! No lazy imports.

**New runtime/dependency support**: whenever adding support for a new language runtime, build tool, or SDK, adjust all three together — the agent **allowed tools** (`backend/app/core/tool_usage.py` `bash_usage`/scoped allowlists), the per-workspace **cache dirs** (`setup_workspace_cache_directories` + `_SKIP_MAKEDIRS` in `backend/app/services/claude_code.py`), and **heavy SDKs on the PV** (small binaries → `backend/Dockerfile`; large SDKs → `backend/scripts/init-mobile-sdk.sh` shared NFS cache).

**Testing**: `make unit-tests` for regular unit tests and `make integration-tests` for docker-compose based tests that are normally skipped. Only use those two commands.

**Complexity metrics for backend:** 
`FILE` is a path under `backend/` (e.g. `app/api/v1/auth.py` or a directory). 
`METRIC` is one of `cc` (McCabe / cyclomatic), `mi` (maintainability index), or `hal` (Halstead; the diff script uses per-file and per-function **volume** as the scalar).

- **After finishing substantive work** on the backend, run a **whole-tree** report vs `main`: 
`make check-complexity-diff` (default `METRIC=cc`) or `make check-complexity-diff METRIC=mi` / `METRIC=hal`. This surfaces average metrics and per-item deltas (cc: by line/symbol; mi: by file; hal: file total + per function).
- **While editing a specific file or package:** `make check-complexity-cc FILE=app/...` and `make check-complexity-mi FILE=app/...`.
- **Quick local average only** (no diff to `main`): `make check-complexity`.

**Key paths:**
- `backend/app/services/` — business logic (estimation, workspace_pool, crash_recovery, retry)
- `backend/app/workflows/` — orchestration (generate_poc, multi_workspace_estimation_p10y)
- `backend/app/state/` — state machines (status/checkpoint writes only here)
- `backend/app/core/app_lifecycle.py` — FastAPI startup/shutdown orchestration (graceful shutdown + boot recovery)
- `backend/app/schemas/estimation_enums.py` — EstimationStatus, EstimationCheckpoint, WorkspaceStatus
- `server.py` — MCP tool definitions
- `Makefile` — all commands (`make unit-tests`, `make check`, `make run`)

**Agent MCP policy (optional Playwright/Figma):** Two layers — (1) **session-wide** enablement in `mcp_servers_enabled`, (2) **per-phase** `applicable_agent_mcps` from `IMPLEMENTATION_PLAN.md` (written locally via `run_planning`, parsed at contract validation).

- **Candidate set:** `MCP_SERVERS_ENABLED` from the MCP client env is sent on workspace sync / `run_generation` and stored on the generation session.
- **Keyword prune (deterministic):** During contract validation (after upload, before KB init), `prune_enabled_mcps_keyword_only` greps `specification_index.md` (under `outputs_dir/analysis/`), other analysis markdown, and spec-tree `.md`/`.txt` files for configured keywords (`MCP_PRUNE_KEYWORDS_*`). MCPs with no hits are dropped; if nothing hits, the full candidate set is kept (conservative). Result is persisted to `mcp_servers_enabled`. Controlled by `MCP_AUTO_PRUNE_ENABLED` (default true). No LLM prune agent in this flow — keyword matching replicates the old `MCP_PRUNE_USE_LLM=false` path.
- **Resolution at run:** `run_generation` uses **stored** `mcp_servers_enabled` when non-empty (`prefer_stored=True`) so post-prune values are not overwritten by a repeated env string.
- **Codegen:** Each phase uses `applicable_agent_mcps ∩ enabled_mcps ∩ SUPPORTED_MCPS`. Omit `**Agent MCPs**:` on a phase to inherit session enablement; `none` disables optional MCPs for that phase only.

**Generation lifecycle (user journey):**
Spec analysis and implementation planning happen **locally in the user's IDE** via MCP tools (`check_specification_completeness`, `run_planning`) — the backend is not involved until `run_generation` is called. Once invoked, the backend runs as one continuous, locked execution — the user cannot edit plans or intervene while the workflow is active:

0. **Local (no backend)** — user calls `check_specification_completeness` and `run_planning` in their IDE; both produce markdown files in the user's project directory. Repeatable, free, no session created.
1. **File upload + contract validation** — `run_generation` uploads the user's `specs/`, optional `src/`, and `outputs_dir/` to the primary workspace. The contract validator (`backend/app/services/contract_validator.py` + `run_contract_validator` in `workflow_steps.py`) fuzzy-matches required files, runs keyword-only MCP prune, converts plans markdown→JSON, and writes Firestore plan data. If any required file is missing or unparseable, `run_generation` fails immediately with a human-readable message. No spec/planning/KB agents in this step (plan conversion agent only).
2. **KB init + Generation** — the provisioned Rosetta plugin initializes the knowledge base as the first generation step (not before). Then code generation runs across all workspaces in parallel, committing incrementally.
3. **Deploy & E2E** — deploy loop starts immediately after generation; no user pause in between.

Consequences that must be upheld in code:
- The backend has **no** spec-analysis or planning agent. The only place markdown plans are read is the contract validator's JSON conversion step (called from `generate_app_workflow`, before KB init).
- `update_planning_data` and `save_e2e_plan` are called **once**, by the contract validator, after JSON conversion succeeds. They must **always write to all allocated workspaces**.
- `init_deployment_phases` is a no-op when the field is already populated — by deploy-loop start, the contract validator has already written the plan, so the guard exists only for retries where workspace_ids were not yet allocated at validation time.

---

## File/Directory Contract (local skills ↔ backend)

The local MCP tools (`check_specification_completeness`, `run_planning`) and the backend must agree exactly on file names and paths. The contract validator enforces this on upload.

**Constants (single source of truth):**
- `ANALYSIS_SUBDIR = "analysis"`, `PLANNING_SUBDIR = "planning"` — `backend/app/core/artifact_subdirs.py`
- `SPEC_COMPLETENESS_FILE = "specification_completeness.md"` — `backend/app/core/artifact_files.py`
- `IMPLEMENTATION_PLAN_FILE = "IMPLEMENTATION_PLAN.md"` — same
- `E2E_TEST_PLAN_FILE = "e2e-test-plan.md"` — same
- `outputs_dir` default = `"docs"` (MCP tool default). User can override; both skill and `run_generation` MUST receive the same value.

**Required files in `<project_root>/<outputs_dir>/` before `run_generation`:**

| File | Producer | Required when |
|------|----------|---------------|
| `analysis/specification_completeness.md` | `check_specification_completeness` | Always |
| `planning/IMPLEMENTATION_PLAN.md` | `run_planning` | Always |
| `planning/e2e-test-plan.md` | `run_planning` | Only if `specification_completeness.md` Part F = `INTEGRATION_TESTS_READY` |

**Contract-validator behavior (deterministic, no LLM):**
- Case-insensitive filename match (e.g. `implementation_plan.md`, `Implementation-Plan.md` → normalized to canonical name).
- Whitespace/separator normalization: `_`, `-`, ` ` treated as equivalent for matching.
- Files found in wrong subdirectory (e.g. `IMPLEMENTATION_PLAN.md` at outputs_dir root, or under `analysis/`) are moved to the canonical location.
- Multiple candidate files matching the same canonical name → error (ambiguous, refuse to guess).
- Missing required file → `run_generation` returns short error: "Missing required files: ... — run the relevant MCP tool (`check_specification_completeness` or `run_planning`) to produce them."
- After normalization, the validator runs markdown→JSON conversion and writes `planning_data` / `e2e_planning_data` to Firestore. If conversion fails, return a short error referencing the offending file.

**Skill ↔ MCP-tool argument parity:** Skills are drop-in replacements for backend tools. They MUST accept the same arguments (`spec_path`, `outputs_dir`) and write to paths derived from those arguments. No hardcoded `docs/...` in skill instructions.

---

## `run_generation` rejection contract

`run_generation` launches multiple autonomous agents for 2–8 hours. There is **no opportunity to prompt the user** mid-run. Therefore the gate at the entrance must be exhaustive: if anything is wrong, we refuse synchronously and return a short, specific, actionable message. The local skills are designed to satisfy this contract in the 99% case; this gate exists for the 1%.

**Two-layer gate:**
1. **MCP-side precheck** (`mcp_server/`) — runs before any upload. Catches missing files, missing dirs, obviously wrong arguments. Cheap, runs locally, gives instant feedback.
2. **Backend contract validator** — runs after upload, before KB init. Performs fuzzy-match normalization, JSON conversion of the markdown plans, and structural validation of the JSON. Returns a structured rejection through the same error channel.

Both layers return the same error shape so the user experience is identical regardless of which gate caught the problem.

**Every rejection includes (1) what's wrong, (2) which file, (3) which MCP tool to re-run, (4) the canonical path expected.** No internal jargon ("checkpoint", "Firestore", "workspace"), no stack traces, no "see logs". The user is in their IDE — they only know about specs, plans, and the two local MCP tools.

**Rejection catalog** (this is the SSOT — implementations must match):

| Code | Trigger | Message |
|------|---------|---------|
| `SPEC_DIR_MISSING` | `<project_root>/<spec_path>` does not exist or is empty | "No specs found at `{spec_path}/`. Add your specification files there and try again." |
| `OUTPUTS_DIR_MISSING` | `<project_root>/<outputs_dir>` does not exist | "No `{outputs_dir}/` directory found. Run `check_specification_completeness` and `run_planning` first to produce the required files." |
| `ANALYSIS_MISSING` | `specification_completeness.md` not present (after fuzzy match) | "Missing analysis file `{outputs_dir}/analysis/specification_completeness.md`. Run `check_specification_completeness` to produce it." |
| `PLAN_MISSING` | `IMPLEMENTATION_PLAN.md` not present (after fuzzy match) | "Missing implementation plan `{outputs_dir}/planning/IMPLEMENTATION_PLAN.md`. Run `run_planning` to produce it." |
| `E2E_PLAN_MISSING` | analysis says `INTEGRATION_TESTS_READY` but `e2e-test-plan.md` not present | "Your analysis is marked `INTEGRATION_TESTS_READY` but `{outputs_dir}/planning/e2e-test-plan.md` is missing. Re-run `run_planning` to produce it, or update the analysis to `LOCAL_ONLY`." |
| `AMBIGUOUS_FILE` | Two or more files fuzzy-match the same canonical name | "Found multiple candidates for `{canonical_name}`: {list}. Keep only one and delete the others." |
| `ANALYSIS_UNREADABLE` | `specification_completeness.md` exists but Part F section missing or malformed | "Couldn't read integration readiness from `specification_completeness.md`. Re-run `check_specification_completeness` — the file is missing Part F." |
| `PLAN_NO_PHASES` | `IMPLEMENTATION_PLAN.md` parses to zero phases | "Your implementation plan has no phases. Re-run `run_planning` — the plan must contain at least one phase." |
| `PLAN_UNPARSEABLE` | JSON conversion of the plan fails | "Couldn't parse `IMPLEMENTATION_PLAN.md` into phases. Re-run `run_planning` — check that each phase has a heading, description, and task list." |
| `E2E_PLAN_UNPARSEABLE` | JSON conversion of the e2e plan fails | "Couldn't parse `e2e-test-plan.md` into rounds. Re-run `run_planning` — check that each round has a heading and verification steps." |
| `GENERATION_ALREADY_RUNNING` | A generation for this project is already in progress | "A generation is already running. Wait for the email notification before starting another one." |
| `MODEL_UNAVAILABLE` | A configured LLM tier has a model that isn't available on the active provider (OpenRouter/Anthropic per `DEFAULT_PROVIDER`) | "The model(s) configured for {tier} aren't available on {provider}: {models}. Did you mean '{suggestion}'? Fix {tier} in your MCP config and try again." |

`MODEL_UNAVAILABLE` semantics (catalog fetch, block-on-any-invalid policy, the two gate locations, and why it doesn't release workspaces) are documented in `docs/backend/model-validation.md`.

**Rules for implementers:**
- Return shape from MCP tool: `{"error": "<message>", "code": "<CODE>", "missing_files": [...], "ambiguous": [...]}`. The IDE displays the message; the structured fields exist for tooling.
- Never proceed to backend call if MCP-side precheck failed.
- Never start workspace allocation if contract validator failed.
- A rejection is **not** a state-machine `fail()` — no `failed_at` timestamp, no FAILED status. Workspaces are released and `workspace_ids` cleared; the user fixes files locally and calls **`run_generation` again** (not `retry_generation`). The next sync re-allocates workspaces on the same `generation_id`.
- Validator errors include the canonical filename and the canonical path verbatim so the user can paste it into their IDE.

**What is explicitly NOT a rejection condition:**
- Wrong-case filename → normalize and proceed.
- Wrong subdirectory (e.g. plan file at `outputs_dir/` root) → move and proceed.
- Extra files in `outputs_dir/` → ignore.
- Missing `src/` → fine, generation runs from scratch.

---

## ⛔ STEEL COMMANDMENTS

Absolute rules. Generated code takes hours to produce and is irreplaceable.

### I — Generated code on workspaces is sacred
Workspace filesystems hold generated code. Git is a backup, not a guarantee. Treat workspace filesystems as the only copy.

### II — `fail()` and `stuck_detected()` NEVER release workspaces
On failure/stuck: workspaces stay `ALLOCATED`, code preserved. Only side-effect: `failed_at` timestamp.

### III — Only `archive_and_release()` exits ALLOCATED
`WorkspaceStateMachine.archive_and_release()` transitions ALLOCATED→CLEANING. Called from `EstimationStateMachine.complete()` after archive preconditions confirmed. Only other exit: `force_release()` (operator-only, audit trail).

### IV — Archive is hard precondition for completion
`complete()` requires: `outputs_archived == True` AND `archive/{generation_id}` branch confirmed. Else raises, estimation stays RUNNING.

### V — Retry MUST reuse workspace if `code_archived == False`
Retry with unarchived code reuses exact same `workspace_ids`. If inaccessible, raises. Never silently falls back to fresh workspaces.

### VI — No background job may touch ALLOCATED workspace
Background jobs call `stuck_detected()` on estimation only. Workspace stays ALLOCATED with code intact.

### VII — State machine is only writer of status/checkpoint/workspace_phases
Nothing outside `backend/app/state/` may write these to Firestore. CI guard enforces. Direct write outside `state/` = bug.

### VIII — Checkpoints never go backward
`advance_checkpoint()` validates strictly forward. Retry resumes from saved checkpoint.

### IX — Every state transition is logged
Each state machine call appends to `state_history`: status, at (UTC), triggered_by (TriggeredBy constants), metadata.

### X — Invalid transitions raise immediately
`InvalidEstimationStateError` / `InvalidWorkspaceStateError` raised on disallowed transitions. No silent corruption.

### XI — Coding/deploy phases protect outputs; P10Y only measures them (SRP)
The workflows that run **code generation** and **deploy/QA** (the phase loops where agents commit and push incrementally) own **preserving** generated outputs to durable storage: remote git, artifact store snapshots, and advancing **`archive_outputs` / `outputs_archived`** per **III–IV** once those phases finish successfully. The **P10Y / multi-workspace estimation** workflow owns **only** reading git commit metadata, calling Compass/P10Y, producing estimation reports and variance analysis, and persisting **estimation result** data (e.g. Firestore). It must **not** be the sole or mandatory place for full-workspace artifact archival—partial or total failure in P10Y metrics must **never** leave coding outputs unprotected.

---

## IDEs (Cursor and Claude Code)

In-repo configuration is aligned for both: see `docs/IDE-SETUP.md` (`.cursor/rules` and hooks; `.claude/commands` and skills; **hooks are Cursor-only** — use the `backend-quality-gate` skill or `make check` in Claude Code).

---

## Development Protocol

**Before**: Read implementation plan phase. Run `make unit-tests` — record baseline.

**During**: Run tests after each sub-step. Fix before proceeding. Never comment out tests. Never delete code until the phase that removes it.

**On test failure**: Fix implementation, not test (unless test is buggy). Don't retry same approach >2x — ask.

**On completion**: All acceptance tests pass. Pass count ≥ baseline.

---

## Modifying Existing Code

These are the defects that pass tests and violate no invariant above, so only review catches them. They cluster at the **seam where new code meets existing code** — a new preflight, a fallback, a second validator. When changing code, reconcile the new path with the one that already exists:

1. **Single source of truth.** If a value is validated, parsed, or formatted in more than one place, those places share one function/constant. Never add a second validator (regex, schema, pre-check) for something an existing validator or agent already covers — they drift. Generalizing the existing mechanism beats adding a special case beside it.
2. **Fix every path, not the observed one.** When fixing a bug, grep for every site with the same shape — fallbacks, error/except branches, retries — and fix or consciously exempt each. The bug usually survives in the fallback (e.g. a truncation fixed in the main path but left in the `or ...` default).
3. **A check must match the message it emits.** Acceptance criteria and the rejection/error text are one contract (`any` vs `all`, "each phase" vs "some phase"). If the message says "each", the check is `all`.
4. **Tests assert the contract, not current behavior.** When you change behavior, do not just rewrite the test to green — confirm the new behavior is what the contract (rejection catalog, file contract, STEEL COMMANDMENTS) requires. An edited suite proves consistency, not correctness.
5. **A new guard reuses or narrows downstream work — it does not duplicate it.** A preflight that re-does what a later step already does (re-extract, re-parse) is wrong altitude; extract a shared helper or narrow the scope. Mind blocking work added to `async` handlers and hot paths.
