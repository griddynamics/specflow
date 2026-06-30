# State Transition Diagrams

Each diagram combines three dimensions into one view:
- **Generation status** — what the generation record says
- **Workspace status** — what the workspace record says
- **Filesystem state** — what is actually on the persistent volume

The interaction between these three is where data loss has historically occurred.
All diagrams reflect the current production state of the system.

**PR255 (local analysis/planning):** Spec completeness and implementation planning run in the
user's IDE (MCP tools `check_specification_completeness`, `run_planning`). The backend is not involved
until `run_generation`. There is no `GenerationStatus.ANALYSIS` and no `begin_analysis()` transition.

---

## 0 — User journey vs backend (PR255)

```mermaid
flowchart LR
    classDef local fill:#e67e22,stroke:#935116,color:#fff
    classDef mcp   fill:#2e86c1,stroke:#1a5276,color:#fff
    classDef api   fill:#7d3c98,stroke:#512e5f,color:#fff

    L1["IDE: check_specification_completeness
writes analysis/*.md"]:::local
    L2["IDE: run_planning
writes planning/*.md"]:::local
    P["MCP: run_generation
Layer 1 precheck"]:::mcp
    S["POST /workspace/sync
tar → primary WS
create/reuse session"]:::api
    R["POST /generation-sessions/run
background workflow"]:::api

    L1 --> L2 --> P --> S --> R
```

| Step | Session status | Workspaces |
|------|----------------|------------|
| After `create()` or first sync | `pending` | Allocated on first sync (or reused if still `ALLOCATED` + `locked_by` match) |
| `generation-sessions/run` starts | `pending` → `initializing` → `running` | Stay allocated |
| Contract validator rejects | `running` → `pending` via `reject_contract()` | Released (`workspace_ids` cleared); next sync re-allocates |
| Real workflow failure | → `failed` | **Stay allocated** (Commandment II) |

**`/workspace/sync` reuse rules (same `generation_id`):**
- Allowed only while generation status is **`pending`** (e.g. after contract reject, or before first `/run`).
- Rejected with **409** if status is `running`, `failed`, `completed`, or `initializing`.
- `ensure_workspaces_for_sync`: reuse workspaces still `ALLOCATED` and `locked_by` this session; otherwise allocate a fresh set (no workspace theft).

---

## 1 — Generation Lifecycle

`fail()` and `stuck_detected()` do not release the workspace. Filesystem is only wiped
after a confirmed archive to a dedicated branch. Retry always resumes on the same workspace.

Checkpoints while `RUNNING` (strictly forward): `files_uploaded` → `contract_validated` →
`kb_init_done` → `generation_started` → `generation_done` → `deploy_and_e2e_done`* →
`outputs_archived` → `estimation_done` (*skipped on `LOCAL_ONLY`).

```mermaid
flowchart TD
    classDef normal  fill:#2e86c1,stroke:#1a5276,color:#fff
    classDef archive fill:#d4ac0d,stroke:#7d6608,color:#fff
    classDef safe    fill:#27ae60,stroke:#1a5e36,color:#fff
    classDef failed  fill:#5d6d7e,stroke:#2c3e50,color:#fff
    classDef pool    fill:#7d3c98,stroke:#512e5f,color:#fff
    classDef reject  fill:#e74c3c,stroke:#922b21,color:#fff

    A["Est: PENDING
WS: none or ALLOCATED
FS: — or specs/plans synced"]:::normal

    B["Est: INITIALIZING
WS: ALLOCATED
FS: cloning repos"]:::normal

    C["Est: RUNNING
WS: ALLOCATED
FS: contract → KB → codegen 💻
loop 1: plan phases
loop 2: e2e phases
(INTEGRATION_TESTS_READY only)"]:::normal

    R["Est: PENDING
WS: released → re-alloc on next sync
checkpoint: files_uploaded
NOT failed_at"]:::reject

    D["Est: RUNNING → archiving
WS: ALLOCATED
FS: pushing archive branch 📦"]:::archive

    E["Est: COMPLETED
WS: CLEANING
FS: archived ✓ — safe to wipe"]:::safe

    F["Est: FAILED
WS: ALLOCATED ← preserved
FS: CODE INTACT 💾
same workspace, nothing touched"]:::failed

    G["WS: AVAILABLE
FS: empty"]:::pool

    A  -->|"POST /generation-sessions/run
begin_allocation()"| B
    B  -->|"allocation_failed()
allocation_rollback() per workspace"| A
    B  -->|"allocation_succeeded()"| C

    C  -->|"reject_contract()
validate_contract failed"| R
    R  -->|"POST /workspace/sync
ensure_workspaces_for_sync
(user fixed local files)"| A

    C  -->|"advance_checkpoint()
per workflow step"| C
    C  -->|"complete()
trigger archive step"| D
    D  -->|"all archives confirmed
→ archive_and_release() [txn]"| E
    D  -->|"archive failed
stays in archiving, retry archive"| D

    C  -->|"fail() or stuck_detected()
workspace stays ALLOCATED"| F
    F  -->|"reset_for_retry()
SAME workspace
resume from last checkpoint ♻️"| C

    E  -->|"cleanup_workspace()
→ mark_clean()"| G
```

**Key invariants:**
1. `fail()` and `stuck_detected()` **never** release a workspace — code on the filesystem is preserved
2. The only normal exit from ALLOCATED is `archive_and_release()`, called exclusively from `complete()` after archive is confirmed across all workspaces
3. Retry (`reset_for_retry` from `FAILED`) reuses the exact same workspace IDs if `code_archived == False`
4. `complete()` raises if either `outputs_archived != True` or archive branch is missing from any workspace repo
5. **`reject_contract()`** is not `fail()` — no `failed_at`; session returns to `PENDING`, workspaces released, checkpoint reset to `files_uploaded` so the validator cannot be skipped on retry
6. Local spec/planning does not create a generation session — first backend touch is `run_generation` → `/workspace/sync`

---

## 2 — Workspace States

Normal lifecycle paths are solid. Operator escape hatches are dashed.

```mermaid
stateDiagram-v2
    direction LR

    [*] --> AVAILABLE

    AVAILABLE --> ALLOCATED : allocate() [txn]
    AVAILABLE --> CLEANING  : admin_clean_available() [operator]

    ALLOCATED --> CLEANING  : allocation_rollback() [clone failed]
    ALLOCATED --> CLEANING  : archive_and_release() [archive confirmed, txn]
    ALLOCATED --> CLEANING  : execute_scheduled_wipe() [7-day schedule elapsed]
    ALLOCATED --> CLEANING  : force_release() [operator, audit trail required\nalso fails owning generation → FAILED]
    ALLOCATED --> AVAILABLE : admin_deallocate() [operator, filesystem verified clean]

    CLEANING  --> AVAILABLE : mark_clean() [cleanup verified]
    CLEANING  --> STUCK     : mark_stuck() [cleanup failed or timed out]

    STUCK     --> CLEANING  : begin_recovery() [stuck_cleaning_recovery job]
    STUCK     --> AVAILABLE : admin_release_stuck() [operator, manual cleanup confirmed]

    note right of ALLOCATED
        Stays ALLOCATED through fail(), stuck_detected(), reset_for_retry().
        No heartbeat or lease — code on filesystem is preserved.
        Only normal exit: archive_and_release() after archive confirmed.
        ALLOCATED → STUCK is explicitly blocked (Invariant 2).
    end note

    note right of CLEANING
        Unambiguous: archive confirmed, files safe to delete.
        cleaning_started_at set on entry.
        Workspaces stuck in CLEANING > 2h are recovered by
        stuck_cleaning_recovery: CLEANING → STUCK → CLEANING + cleanup.
    end note
```

---

## 3 — Background Jobs

Each job is scoped strictly. No background job may read from, write to,
or transition an ALLOCATED workspace (Commandment VI).

```mermaid
flowchart TD
    classDef job     fill:#1a5276,stroke:#0a2f4d,color:#fff
    classDef est     fill:#2e86c1,stroke:#1a5276,color:#fff
    classDef ws      fill:#7d3c98,stroke:#512e5f,color:#fff

    J1["job: stuck_running_detector
(every 5 min)
Detects RUNNING generations
with no checkpoint advance
for > 12 hours"]:::job

    J2["job: stuck_initializing_detector
(every 5 min)
INITIALIZING stuck > threshold
→ allocation_failed + rollback

Also: orphaned PENDING + ALLOCATED
workspaces → rollback"]:::job

    J3["job: stuck_cleaning_recovery
(every 30 min)
Detects CLEANING workspaces
where cleaning_started_at
is > 2 hours ago"]:::job

    J4["job: scheduled_wipe
(every 1 hour)
Executes pending wipes where
scheduled_for_wipe_at has elapsed"]:::job

    E1["esm.stuck_detected()
RUNNING → FAILED
workspace untouched"]:::est

    E2["esm.allocation_failed()
INITIALIZING → PENDING"]:::est

    W2["wsm.allocation_rollback()
ALLOCATED → CLEANING
per workspace"]:::ws

    W3a["wsm.mark_stuck()
CLEANING → STUCK"]:::ws
    W3b["wsm.begin_recovery()
STUCK → CLEANING"]:::ws
    W3c["ws_pool.cleanup_workspace()
git reset + verify + mark_clean()"]:::ws

    W4["wsm.execute_scheduled_wipe()
ALLOCATED → CLEANING"]:::ws

    J1 --> E1
    J2 --> E2
    J2 --> W2
    J3 --> W3a --> W3b --> W3c
    J4 --> W4
```

---

## 4 — Filesystem State

```mermaid
flowchart LR
    classDef normal  fill:#2e86c1,stroke:#1a5276,color:#fff
    classDef archive fill:#d4ac0d,stroke:#7d6608,color:#fff
    classDef safe    fill:#27ae60,stroke:#1a5e36,color:#fff
    classDef pool    fill:#7d3c98,stroke:#512e5f,color:#fff

    f1["empty"]:::pool
    f2["repos cloned
(git init)"]:::normal
    f2b["specs + outputs_dir synced
contract_validated
plans → JSON in Firestore"]:::normal
    f3["KB init + sync to all workspaces
loop 1: code generation phases
IMPLEMENTATION_PLAN.md
unit tests only, no live deployments"]:::normal
    f3b["loop 2: deploy → e2e tests → fix
e2e-test-plan.md phases
INTEGRATION_TESTS_READY only
checkpoint: DEPLOY_AND_E2E_DONE"]:::normal
    f4["archive branch pushed
✓ confirmed on all 3 workspaces"]:::archive
    f5["PRESERVED 💾
on fail / stuck_detected
workspace stays ALLOCATED
code intact for retry"]:::safe
    f6["WIPED
after archive confirmed
— safe —"]:::safe

    f1 --> f2 --> f2b --> f3
    f3 -->|"INTEGRATION_TESTS_READY
→ deploy_and_e2e step"| f3b
    f3b -->|"complete()
→ archive step"| f4
    f3 -->|"LOCAL_ONLY
→ generation step
complete() → archive step"| f4
    f4 --> f6
    f3 -->|"fail() / stuck_detected()
WS stays ALLOCATED"| f5
    f3b -->|"fail() / stuck_detected()
WS stays ALLOCATED"| f5
    f5 -->|"retry()
resume from last checkpoint
(GENERATION_DONE or DEPLOY_AND_E2E_DONE)"| f3
```

---

## 5 — Cross-System Trigger Map

| Event | Generation → | Workspace → | Filesystem → |
|---|---|---|---|
| `create()` | → PENDING | no workspace yet | — |
| `POST /workspace/sync` (new session) | → PENDING (if new) | AVAILABLE → ALLOCATED (set) | tar extracted to primary WS |
| `POST /workspace/sync` (reuse `generation_id`) | must be PENDING | reuse ALLOCATED+locked or fresh allocate | tar overwrites primary WS |
| `begin_allocation()` | PENDING → INITIALIZING | already ALLOCATED from sync | — |
| `allocation_failed()` | INITIALIZING → PENDING | ALLOCATED → CLEANING | → cleanup |
| `allocation_succeeded()` | INITIALIZING → RUNNING | stays ALLOCATED | → workflow steps |
| `reject_contract()` | RUNNING → PENDING; checkpoint → `files_uploaded`; `workspace_ids` cleared | ALLOCATED → CLEANING (rollback per WS) | user fixes files locally; not a failure |
| `advance_checkpoint()` — contract / KB | RUNNING (→ `contract_validated`, `kb_init_done`) | no change | plans normalized + JSON stored |
| `advance_checkpoint()` — generation steps | RUNNING (→ `generation_done`) | no change | loop 1 code being written |
| `advance_checkpoint()` — deploy_and_e2e | RUNNING (→ `deploy_and_e2e_done`) | no change | loop 2 *(INTEGRATION_TESTS_READY only)* |
| `advance_checkpoint()` — archive / P10Y | RUNNING (→ `outputs_archived`, `estimation_done`) | no change | reports / archives |
| `fail()` | PENDING / INITIALIZING / RUNNING → FAILED | **stays ALLOCATED** ✓ | **preserved** ✓ |
| `stuck_detected()` | RUNNING → FAILED | **stays ALLOCATED** ✓ | **preserved** ✓ |
| `reset_for_retry()` | FAILED → PENDING | **same ALLOCATED** ✓ | **resumes** ✓ |
| `complete()` archive step | RUNNING → archiving | stays ALLOCATED | → archive branch pushed |
| `archive_and_release()` | RUNNING → COMPLETED | **ALLOCATED → CLEANING** ✓ | archive confirmed |
| `mark_clean()` | stays COMPLETED | CLEANING → AVAILABLE | → wiped |
| `mark_stuck()` | — | CLEANING → STUCK | — |
| `begin_recovery()` | — | STUCK → CLEANING | — |
| `cleanup_workspace()` | — | CLEANING → AVAILABLE (via `mark_clean`) | → git reset → empty |
| `schedule_wipe()` | — | ALLOCATED (flag set, no status change) | — |
| `execute_scheduled_wipe()` | — | ALLOCATED → CLEANING | — |
| `force_release()` *(operator)* | **RUNNING/INITIALIZING/PENDING → FAILED** (if locked_by set) | ALLOCATED → CLEANING | — |
| `admin_clean_available()` *(operator)* | — | AVAILABLE → CLEANING | — |
| `admin_deallocate()` *(operator)* | — | ALLOCATED → AVAILABLE *(filesystem clean check required)* | — |
| `admin_release_stuck()` *(operator)* | — | STUCK → AVAILABLE | — |

---

## 6 — Transition Authority

All state writes are funnelled through two state machines in `backend/app/state/`.
Nothing outside `state/` may write `status`, `checkpoint`, or `workspace_phases` to Firestore.
The CI guard (`ci/check_state_writes.sh`) enforces this on every commit.

| State machine | File | Owns |
|---|---|---|
| `GenerationStateMachine` | `state/generation_state_machine.py` | `status`, `checkpoint`, `workspace_phases` on generation docs |
| `WorkspaceStateMachine` | `state/workspace_state_machine.py` | `status`, `locked_by`, `last_used_by`, `cleaning_started_at` on workspace docs |

`triggered_by` values use constants from `TriggeredBy` in `state/transitions.py`:
- `api:*` — API / service layer calls
- `orchestrator:*` — workflow orchestrator steps
- `job:*` — background job detectors
- `admin:*` — operator escape hatches
