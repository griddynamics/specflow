# Architecture - SpecFlow

```
┌──────────────────────────────────────────────────────────────────┐
│ CLIENT LAYER                                                      │
│  Cursor IDE (MCP Client)  /  Claude Desktop (MCP Client)         │
└───────────────────────────┬──────────────────────────────────────┘
                            │ MCP (stdio / HTTP)
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│ MCP SERVER (mcp_server/server.py, FastMCP, local process)        │
│  Local: check_specification_completeness, run_planning,          │
│         read_document → bundled SKILL templates (PR255)          │
│  Remote: run_generation, check_status, download_outputs, retry   │
│  Session: specflow_session.json (after first run_generation)     │
└───────────────────────────┬──────────────────────────────────────┘
                            │ HTTPS (run_generation onward)
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│ BACKEND (Kubernetes GKE, FastAPI)                                │
│  API (backend/app/api/v1/) → Services → State Machines → DB     │
│  Middleware: Auth → Logging → ErrorHandler                       │
│  NFS: /workspaces (500Gi), /agent_logs (100Gi)                  │
└───────────────┬───────────────────────┬──────────────────────────┘
                │                       │
                ▼                       ▼
┌──────────────────────────┐  ┌────────────────────────────────────┐
│ Firestore                │  │ External Services                  │
│  generations, workspaces │  │  Anthropic Claude API (codegen)    │
│  api_keys, est_requests  │  │  P10Y/Compass (code metrics)      │
│  (distributed locking)   │  │  GitHub API (workspace repos)      │
└──────────────────────────┘  └────────────────────────────────────┘
```

## Layers

- **MCP server** (`mcp_server/`): Local spec analysis and planning via tool-returned SKILL templates; upload orchestration and precheck for `run_generation`; no persistent business state except `specflow_session.json`.
- **Services** (`backend/app/services/`): GenerationService, WorkspacePoolService, GenerationRetryService, ContractValidator, CrashRecoveryService, GitArchiveService, ClaudeCodeService.
- **State Machines** (`backend/app/state/`): GenerationSM (PENDING→INITIALIZING→RUNNING→COMPLETED/FAILED), WorkspaceSM (AVAILABLE→ALLOCATED→CLEANING→AVAILABLE). Only writer of status/checkpoint. CI enforced.
- **Workflows** (`backend/app/workflows/`): `generate_app_workflow` (KB init, parallel codegen, deploy/E2E, P10Y). No backend spec-analysis or planning agents after PR255.
- **Database** (`backend/app/database/`): IDatabase interface — SQLite (local/Docker default), Firestore (prod, or connecting to an already-hosted GCP instance), Emulator (manually-run Firestore emulator, not started by docker-compose), InMemory (tests).

## Data Flow

### PR255 user journey (local then backend)

```
1. IDE: check_specification_completeness → agent writes
      {outputs_dir}/analysis/specification_completeness.md
      (optional: specification_index.md, repo_summary.md)

2. IDE: run_planning → agent writes
      {outputs_dir}/planning/IMPLEMENTATION_PLAN.md
      (+ e2e-test-plan.md if INTEGRATION_TESTS_READY)

3. MCP: run_generation → precheck → POST generation-sessions/run
      → upload specs, src, outputs_dir → contract validator
      → markdown plans → JSON → Firestore (all workspaces)

4. Backend: KB init → parallel codegen → deploy/E2E → P10Y → archive

5. User: check_status (optional) → download_outputs (optional)
```

Contract validation is **deterministic** (file paths, fuzzy names, JSON conversion, keyword MCP prune). Failures roll the session back to **PENDING** (rejection), not FAILED.

### Generation (backend happy path)

```
1. run_generation uploads artifacts and allocates workspaces
2. Contract validator normalizes paths, converts plans, writes planning_data / e2e_planning_data
3. WorkflowOrchestrator: KB init → generation phases → deploy loop → P10Y
4. complete() → archive_and_release() → email USER_EMAIL
```

Removed endpoints (PR255): `POST /api/v1/specification/*`, `POST /api/v1/generation/plan` — analysis and planning are not backend-driven.

### Crash Recovery

```
CrashRecoveryService: heartbeat TTL expired → stuck_detected()
  → failed_at written, workspaces stay ALLOCATED
Retry: code_archived==False → reuse workspaces; True → fresh allocation
```

## AI Agent Pipeline

Prompts in `backend/app/prompts/agents_claude_code.py`. **Local** analysis/planning instructions in `mcp_server/services/skills/`.

### Local (IDE, no backend)

| Step | MCP tool | Output |
|------|----------|--------|
| Index (optional) | `check_specification_completeness` template | `{outputs_dir}/analysis/specification_index.md` if spec tree > 3 files |
| Completeness | same | `{outputs_dir}/analysis/specification_completeness.md` |
| Repo summary (brownfield) | same | `{outputs_dir}/analysis/repo_summary.md` |
| Planning | `run_planning` template | `IMPLEMENTATION_PLAN.md`, optional `e2e-test-plan.md` |

### Backend (after upload)

| # | Agent | When | Duration |
|---|-------|------|----------|
| 0 | KB Init | First workflow step post-validation | 5–15m |
| 1 | CodeGen (per phase) | Parallel across workspaces | 2–8h |
| 2 | Deploy / E2E | If `INTEGRATION_TESTS_READY` | 1–2h |
| 3 | P10Y | Per workspace, then aggregate | 10–20m |

Planning and spec-completeness **backend** agents were removed; plan JSON is produced only by the contract validator’s markdown→JSON step.

### Two Use Cases

**Spec analysis & planning (local):** Repeatable in the IDE; no API session until `run_generation`.

**Generation (`run_generation`):** 1–3 parallel workspaces, fire-and-forget, 2–8h.
1. Contract validation + KB init on primary workspace
2. Parallel code generation (independent variants)
3. Optional deploy/E2E per integration readiness
4. P10Y per workspace → aggregate
5. Archive → release → notify

### Workflow States (generation session)

```
PENDING
    ↓ run_generation: begin_allocation()
INITIALIZING
    ↓ allocation_succeeded()
RUNNING
    ↓ checkpoints: contract_validated → kb_init_done → generation_* →
      deploy_and_e2e_done → outputs_archived → estimation_done
    ↓ complete()
COMPLETED
```

Workspace states: AVAILABLE → ALLOCATED → CLEANING → AVAILABLE.

Legacy checkpoints (`planning_done`, `plan_synced`, `spec_check_done`) are not used on new sessions.

### Multi-pool workspaces and API keys

- **Workspace documents** include `workspace_pool` (e.g. `default`, `hf`). Allocation queries filter by pool so dedicated hardware/repos stay isolated.
- **API keys** carry `workspace_pool` (set at admin create) and a non-secret **`key_uid`** (UUID). The active key is always the **`X-API-Key`** header.
- **Generations** store `workspace_pool` and `key_uid` at creation; generation-scoped APIs enforce email ownership **and** matching `workspace_pool` **and** `key_uid` when present.
- **GitHub auth**: per-key encrypted PAT in Firestore (`github_token_ciphertext`) via **`PUT /api/v1/auth/github-token`**; default pool falls back to in-memory **`GITHUB_TOKEN_DEFAULT`** + **`GIT_USER_NAME_DEFAULT`** loaded at startup (Kubernetes API in production, `.env` locally). Dedicated pools have **no** default PAT fallback.

## Deployment

**Self-hosted backend**: the backend can run on Kubernetes or Docker Compose with persistent workspace and log volumes sized for long-running code generation.

**Local**: `make run` → Docker Compose (backend:8000, SQLite). The backend container
bind-mounts the host's `~/.specflow/` directory — one central SQLite database shared
across every local project/MCP session on the machine (the local-dev analogue of the
old shared Firestore emulator), with the db file itself nested at `~/.specflow/db/specflow.db`
so it stays separate from other files (e.g. `config.json`) in that directory. Neither
directory needs to pre-exist: Docker creates the host `~/.specflow/` bind-mount path on
first `docker compose up` if missing, and `SqliteDatabase.__init__` creates the `db/`
subdirectory on first backend connection. Generated outputs use `./workspaces/artifacts/`.
`DATABASE_TYPE=firestore` connects to an already-hosted, GCP-managed Firestore instance
instead — SpecFlow never deploys or manages Firestore itself locally.
