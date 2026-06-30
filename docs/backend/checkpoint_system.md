# Generation Workflow Checkpoint System

**Status:** Describes the **current** (PR255) checkpoint model. For state-machine diagrams and workspace rules, see [`docs/state-management/state-transition-diagrams.md`](../state-management/state-transition-diagrams.md).

## Overview

Checkpoints record progress through the **backend** generation workflow after `run_generation` starts. Spec analysis and planning are **local** (IDE); they are not checkpoints on the generation session.

The workflow is **resumable**: `retry_generation` continues from the last checkpoint on the **same** workspace IDs when `code_archived == False`. `fail()` and `stuck_detected()` do **not** release workspaces (Steel Commandment II).

**SSOT in code:** `GenerationCheckpoint` and `CHECKPOINT_ORDER` in `backend/app/schemas/generation_workflow_enums.py`.

## Session status (lifecycle)

```
PENDING → INITIALIZING → RUNNING → COMPLETED | FAILED
```

There is no `ANALYSIS` status. Local spec/plan work does not create or advance a backend session until the first `run_generation` upload.

| Status | Meaning |
|--------|---------|
| `pending` | Session created; workspaces may be allocated; no active workflow (or returned here after **contract reject**) |
| `initializing` | Allocation in progress (`begin_allocation` → clone repos) |
| `running` | Workflow executing; checkpoint advances |
| `completed` | Archive confirmed; workspaces cleaning |
| `failed` | Real failure; workspaces **stay allocated** for retry |

## Checkpoints (strictly forward while `running`)

| Order | Checkpoint | What completed |
|-------|------------|----------------|
| 1 | `files_uploaded` | Tar extracted to primary workspace (sync) |
| 2 | `contract_validated` | Required markdown normalized; plans → JSON; `planning_data` / E2E plan written to Firestore |
| 3 | `kb_init_done` | Rosetta KB init on primary; specs/plans/`src` synced to all workspaces |
| 4 | `generation_started` | Codegen telemetry continuity |
| 5 | `generation_done` | All implementation phases done (all workspaces) |
| 6 | `deploy_and_e2e_done` | Deploy + E2E loop *(only when analysis Part F = `INTEGRATION_TESTS_READY`)* |
| 7 | `outputs_archived` | Artifact tarball stored |
| 8 | `estimation_done` | P10Y / comparative report done |

`DEPLOY_AND_E2E_DONE` is skipped on the `LOCAL_ONLY` path.

## Contract rejection (not a failure)

When the **contract validator** rejects uploaded artifacts:

- Transition: `reject_contract()` — `RUNNING` → `PENDING`
- **No** `failed_at`; user fixes files **locally** and calls `run_generation` again (not `retry_generation`)
- `workspace_ids` cleared; workspaces rolled back via `allocation_rollback`
- Checkpoint reset to **`files_uploaded`** so the validator cannot be skipped on retry
- Next `/workspace/sync` with the same `generation_id` only allowed while **`pending`**; `ensure_workspaces_for_sync` re-allocates workspaces

See `CLAUDE.md` (rejection catalog) and `mcp_server/services/run_generation_precheck.py` (Layer 1).

## Per-workspace phase tracking

`workspace_phases` on the generation document:

- Populated when the contract validator saves the implementation plan (`save_generation_plan` / `update_planning_data`)
- `last_completed_phase` advances during codegen per workspace
- On retry, each workspace resumes from its last completed phase (orchestrator skip logic)

`workspace_phases_deployment` tracks the E2E/deploy loop when integration-ready.

## Retry vs re-upload

| Situation | User action | Backend path |
|-----------|-------------|--------------|
| Contract / missing file after upload | Fix markdown locally | `run_generation` again → sync (session `pending`) → workflow |
| Workflow failed mid-codegen | `retry_generation` | `reset_for_retry` from `FAILED`; **same** workspaces |
| Completed run | `download_outputs` | Archived tarball |

## Migration

`backend/scripts/migrations/migrate_12_generation_session_workflow.py` maps legacy stored statuses/checkpoints (e.g. `analysis` → `pending`, `planning_done` → `contract_validated`) for existing Firestore documents.

## Related docs

- [`docs/state-management/state-transition-diagrams.md`](../state-management/state-transition-diagrams.md) — mermaid + cross-system trigger map (PR255)
- [`docs/mcp/API_REFERENCE.md`](../mcp/API_REFERENCE.md) — MCP tools and precheck
- [`CLAUDE.md`](../../CLAUDE.md) — file contract and rejection catalog
