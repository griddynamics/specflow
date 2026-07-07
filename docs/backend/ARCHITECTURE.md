# Backend Architecture

> **Technical deep dive** into SpecFlow backend architecture, state machines, AI agents, and system design.

## Table of Contents

1. [System Overview](#system-overview)
2. [FastAPI Application](#fastapi-application)
3. [Database Abstraction Layer](#database-abstraction-layer)
4. [State Machines](#state-machines)
5. [AI Agent System](#ai-agent-system)
6. [Workspace Management](#workspace-management)
7. [Checkpoint System](#checkpoint-system)
8. [Retry and Stability](#retry-and-stability)
9. [File Persistence](#file-persistence)
10. [Design Decisions](#design-decisions)

---

## System Overview

### Architecture Diagram

```
┌────────────────────────────────────────────────────────────────┐
│  MCP Client (Cursor IDE / Claude Desktop)                      │
│  └─ SpecFlow MCP Server (local Python process)                    │
└────────────────────────┬───────────────────────────────────────┘
                         │ HTTPS (REST API)
                         ▼
┌────────────────────────────────────────────────────────────────┐
│  FastAPI Backend (Kubernetes GKE)                              │
│  ├─ API Layer (v1 routes)                                     │
│  ├─ Middleware (auth, logging)                                │
│  ├─ Services (business logic)                                 │
│  └─ Workflows (AI agents)                                     │
└─────────┬──────────────────────────┬───────────────────────────┘
          │                          │
          ▼                          ▼
┌─────────────────────┐    ┌────────────────────────────────────┐
│  Firestore          │    │  Persistent Volumes (K8s)          │
│  (State & Metadata) │    │  └─ /workspaces                    │
│  ├─ generations     │    │     ├─ ws-01-1/                    │
│  ├─ workspaces      │    │     ├─ ws-01-2/                    │
│  └─ api_keys        │    │     └─ ws-01-3/                    │
└─────────────────────┘    └────────────────────────────────────┘
```

### Key Components

1. **API Layer** - FastAPI REST endpoints with OpenAPI docs
2. **Middleware** - Authentication, logging, error handling
3. **Services** - Business logic (generations, workspaces, auth)
4. **Workflows** - AI agent orchestration (planning, execution, validation)
5. **Database** - Firestore for state, persistent volumes for files
6. **AI Agents** - Claude Code SDK for autonomous implementation

---

## FastAPI Application

### Application Structure

```python
# app/main.py
from fastapi import FastAPI
from app.api.router import api_router
from app.middleware.auth import AuthMiddleware
from app.middleware.logging import LoggingMiddleware

app = FastAPI(
    title="SpecFlow Backend API",
    version="1.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Middleware (order matters - executed bottom-to-top)
app.add_middleware(LoggingMiddleware)
app.add_middleware(AuthMiddleware)

# API routes
app.include_router(api_router, prefix="/api/v1")

# Health endpoints (public, no auth)
@app.get("/health")
async def health():
    return {"status": "healthy"}
```

### Middleware Stack

**1. LoggingMiddleware** (outermost)
- Logs all requests/responses
- Structured logging for Cloud Logging
- Includes request ID, duration, status code

**2. AuthMiddleware**
- Validates API keys for protected routes
- Injects `user_id` into request state
- Skips auth for public endpoints (`/health`, `/docs`)

**3. Route Handler**
- Executes endpoint logic
- Can depend on `require_api_key` for explicit auth

### Route Organization

```python
# app/api/v1/__init__.py
from fastapi import APIRouter
from .auth import router as auth_router
from .generations import router as generations_router
from .specifications import router as specifications_router
from .workspaces import router as workspaces_router

api_router = APIRouter()

api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(generations_router, prefix="/generations", tags=["generations"])
api_router.include_router(specifications_router, prefix="/specifications", tags=["specifications"])
api_router.include_router(workspaces_router, prefix="/workspace", tags=["workspaces"])
```

### Dependency Injection

```python
from fastapi import Depends
from app.middleware.auth import require_api_key
from app.database import get_database

@router.post("/generations")
async def create_generation(
    data: GenerationCreate,
    user_id: str = Depends(require_api_key),
    db = Depends(get_database)
):
    # user_id injected by auth middleware
    # db injected by dependency
    generation_id = await db.create_generation(user_id, data)
    return {"generation_id": generation_id}
```

---

## Database Abstraction Layer

### Abstract Interface

All database implementations extend `DatabaseInterface`:

```python
# app/database/base.py
from abc import ABC, abstractmethod

class DatabaseInterface(ABC):
    @abstractmethod
    async def create_generation(self, data: dict) -> str:
        """Create new generation record"""
        pass

    @abstractmethod
    async def get_generation(self, generation_id: str) -> dict | None:
        """Get generation by ID"""
        pass

    @abstractmethod
    async def update_generation(self, generation_id: str, updates: dict) -> None:
        """Update generation fields"""
        pass

    @abstractmethod
    async def transaction(self, func):
        """Execute function in transaction"""
        pass
```

### Implementations

**1. MemoryDatabase** - In-memory storage
- Fast (no I/O)
- Used for unit tests
- No persistence
- Thread-safe (asyncio locks)

**2. SqliteDatabase** - Local file, no separate process
- Local/Docker-dev default
- Single-writer, WAL mode, real ACID transactions
- Persists across restarts (bind-mounted at `~/.specflow/db/specflow.db`)
- Not a production replacement for Firestore — no cross-node distributed locking
- **Relational storage.** Known collections (`api_keys`, `generation_sessions`,
  `workspaces`) each get a dedicated table. The fields actually filtered/ordered by
  services and background jobs (e.g. `status`, `key_uid`, `workspace_pool`,
  `set_number`, `scheduled_for_wipe`, and the timestamp fields) are *promoted* to
  typed, indexed columns; the full document also lives in a `data` JSON column, which
  stays the source of truth on read. Timestamps are stored as fixed-width ISO-8601 UTC
  text so lexical order equals chronological order in both the columns and the blob.
  The layout is declared once in `app/database/sqlite_schema.py`, which drives both DDL
  and query routing — adding a collection later is additive. There is no generic
  catch-all table: an unregistered collection is rejected loudly (register it first),
  so a new collection can't silently land in an unindexed blob. Firestore-style
  subcollections follow the same rule — each known one gets its own named child table
  (e.g. `workspace_model_usage`, keyed by `generation_id` + `workspace_id`), and an
  unregistered subcollection is rejected too.

**3. EmulatorDatabase** - Firestore emulator
- Connects to a manually-run Firestore emulator process (docker-compose does not
  start one)
- Realistic Firestore behavior for anyone testing against real Firestore semantics

**4. FirestoreDatabase** - Google Cloud Firestore
- Production, or connecting to an already-hosted GCP-managed instance
- Native transactions
- Distributed locking

### Factory Pattern

```python
# app/database/factory.py
def get_database() -> IDatabase:
    db_type = settings.DATABASE_TYPE
    if db_type == DatabaseType.MEMORY:
        return InMemoryDatabase()
    elif db_type == DatabaseType.SQLITE:
        return SqliteDatabase(db_path=settings.SQLITE_DB_PATH)
    elif db_type == DatabaseType.EMULATOR:
        return EmulatorDatabase(...)
    elif db_type == DatabaseType.FIRESTORE:
        return FirestoreDatabase(...)
```

### Transaction Support

All databases support transactions for atomic operations:

```python
async def allocate_workspace(generation_id: str, db: DatabaseInterface):
    async with db.transaction() as txn:
        # Find available workspace
        workspace = await txn.find_workspace(status="available")
        if not workspace:
            raise ValueError("No workspaces available")

        # Atomic updates
        await txn.update_workspace(
            workspace["id"],
            {"status": "allocated", "generation_id": generation_id}
        )
        await txn.update_generation(
            generation_id,
            {"workspace_ids": [workspace["id"]]}
        )
```

---

## State Machines

SpecFlow uses database-driven state machines for reliability and crash recovery.

### Generation State Machine

**States:**
- `pending` - Queued, waiting for workspace allocation
- `initializing` - Allocating workspaces, syncing files
- `running` - AI agents actively working (heartbeat monitored)
- `completed` - Successfully finished
- `failed` - Permanently failed (can be retried)

**Transitions:**
```
pending → initializing → running → completed
                          ↓
                        failed
```

**State Enforcement:**
```python
# app/services/generation/state.py
ALLOWED_TRANSITIONS = {
    "pending": ["initializing", "failed"],
    "initializing": ["running", "failed"],
    "running": ["completed", "failed"],
    "completed": [],  # Terminal state
    "failed": ["pending"]  # Allow retry
}

async def transition_generation_state(
    generation_id: str,
    new_state: str,
    db: DatabaseInterface
):
    generation = await db.get_generation(generation_id)
    current_state = generation["status"]

    if new_state not in ALLOWED_TRANSITIONS[current_state]:
        raise ValueError(
            f"Invalid transition: {current_state} → {new_state}"
        )

    await db.update_generation(
        generation_id,
        {"status": new_state, "updated_at": datetime.utcnow()}
    )
```

### Workspace State Machine

**States:**
- `available` - Clean, verified, ready for allocation
- `allocated` - Locked by an generation (lease-based)
- `cleaning` - Being reset after use
- `stuck` - Cleanup failed, requires manual intervention

**Transitions:**
```
available → allocated → cleaning → available
             ↓             ↓
           stuck ←────────┘
```

**Lease-Based Locking:**
```python
# Workspaces auto-release if process dies
async def allocate_workspace(generation_id: str):
    workspace = await db.find_workspace(status="available")

    # Set lease expiration (e.g., 24 hours)
    lease_expires_at = datetime.utcnow() + timedelta(hours=24)

    await db.update_workspace(workspace["id"], {
        "status": "allocated",
        "generation_id": generation_id,
        "lease_expires_at": lease_expires_at
    })

    return workspace

# Startup validation releases expired leases
async def release_expired_leases():
    now = datetime.utcnow()
    expired_workspaces = await db.find_workspaces(
        status="allocated",
        lease_expires_at__lt=now
    )

    for workspace in expired_workspaces:
        await db.update_workspace(workspace["id"], {
            "status": "available",
            "generation_id": None,
            "lease_expires_at": None
        })
```

---

## AI Agent System

### Agent Architecture

SpecFlow uses **Claude Code SDK** for autonomous implementation:

```python
from anthropic import Anthropic

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

async def agent_query(
    system_prompt: str,
    user_prompt: str,
    context_files: list[str],
    max_turns: int = 50
):
    """Execute AI agent with Claude Code SDK"""
    response = await client.agents.run(
        model="claude-sonnet-4.5",
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        context_files=context_files,
        max_turns=max_turns,
        tools=["read", "write", "edit", "bash", "grep"]
    )

    return response.output
```

### Agent Types

**1. Planning Agent**
- Analyzes specification
- Creates implementation plan
- Identifies components and dependencies
- Estimates complexity

**2. Phase Execution Agent**
- Implements one component at a time
- Writes code, tests, documentation
- Uses git for version control
- Self-corrects based on feedback

**3. Validator Agent**
- Reviews generated code
- Runs tests and linters
- Checks for completeness
- Provides feedback for improvements

**4. Git Janitor Agent**
- Cleans up git history
- Squashes unnecessary commits
- Writes meaningful commit messages
- Maintains repository hygiene

### Agent Workflow

```python
# app/workflows/generation_workflow.py
async def run_generation_workflow(generation_id: str, workspace_id: str):
    # Phase 1: Planning
    plan = await planning_agent.analyze_and_plan(
        spec_path=f"/workspaces/{workspace_id}/specs",
        context_files=[
            f"/workspaces/{workspace_id}/specs/**/*.md",
            f"/workspaces/{workspace_id}/src/**/*.py"
        ]
    )

    # Phase 2: Implementation (per component)
    for component in plan["components"]:
        implementation = await phase_execution_agent.implement(
            component=component,
            workspace_path=f"/workspaces/{workspace_id}",
            plan=plan
        )

        # Checkpoint after each component
        await save_checkpoint(generation_id, workspace_id, {
            "phase": "implementation",
            "component": component["name"],
            "status": "completed"
        })

    # Phase 3: Validation
    validation = await validator_agent.validate(
        workspace_path=f"/workspaces/{workspace_id}",
        plan=plan
    )

    # Phase 4: Cleanup
    await git_janitor_agent.cleanup_history(
        workspace_path=f"/workspaces/{workspace_id}"
    )

    return {"status": "completed", "validation": validation}
```

---

## Workspace Management

### Workspace Pool

SpecFlow pre-allocates workspaces for parallel generations:

```python
# Workspace pool configuration
WORKSPACE_POOL = [
    {"id": "ws-01-1", "github_repo": "your-org/specflow-workspace-ws-01-1"},
    {"id": "ws-01-2", "github_repo": "your-org/specflow-workspace-ws-01-2"},
    {"id": "ws-01-3", "github_repo": "your-org/specflow-workspace-ws-01-3"},
    # ... up to ws-05-3
]
```

**Each generation gets 3 workspaces** for variance reduction:
- Parallel execution with different models
- Results averaged to reduce bias
- Increases confidence in estimates

### Workspace Allocation

```python
async def allocate_workspaces(generation_id: str, count: int = 3):
    workspaces = []

    for i in range(count):
        workspace = await db.find_workspace(
            status="available",
            pool_group=f"ws-{(i % 5) + 1:02d}"  # Round-robin across pools
        )

        if not workspace:
            raise ValueError(f"Insufficient workspaces (need {count})")

        await db.update_workspace(workspace["id"], {
            "status": "allocated",
            "generation_id": generation_id,
            "allocated_at": datetime.utcnow()
        })

        workspaces.append(workspace)

    return workspaces
```

### Workspace Cleanup

```python
async def cleanup_workspace(workspace_id: str):
    """Reset workspace to clean state"""
    await db.update_workspace(workspace_id, {"status": "cleaning"})

    try:
        # Reset git repository
        await run_command(f"cd /workspaces/{workspace_id} && git reset --hard HEAD")
        await run_command(f"cd /workspaces/{workspace_id} && git clean -fdx")

        # Remove all files except .git
        await run_command(f"find /workspaces/{workspace_id} -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {{}} +")

        # Verify cleanup
        if await verify_workspace_clean(workspace_id):
            await db.update_workspace(workspace_id, {
                "status": "available",
                "generation_id": None,
                "cleaned_at": datetime.utcnow()
            })
        else:
            raise ValueError("Cleanup verification failed")

    except Exception as e:
        # Mark as stuck for manual intervention
        await db.update_workspace(workspace_id, {
            "status": "stuck",
            "error": str(e)
        })
        raise
```

---

## Checkpoint System

See [checkpoint_system.md](./checkpoint_system.md) for complete documentation.

### Purpose

- **Progress preservation** - Save intermediate results
- **Crash recovery** - Resume from last checkpoint
- **Retry optimization** - Skip completed phases

### Checkpoint Data

```python
{
    "generation_id": "est-abc123",
    "workspace_id": "ws-01-1",
    "phase": "implementation",
    "component": "backend-api",
    "status": "completed",
    "timestamp": "2026-02-10T12:30:00Z",
    "data": {
        "files_created": ["app.py", "models.py"],
        "tests_passed": 15,
        "coverage": 0.85
    }
}
```

### Checkpoint Usage

```python
# Save checkpoint
await save_checkpoint(generation_id, workspace_id, {
    "phase": "implementation",
    "component": "backend-api",
    "status": "completed"
})

# Load checkpoint
checkpoint = await load_checkpoint(generation_id, workspace_id)
if checkpoint["phase"] == "implementation":
    # Resume from implementation phase
    completed_components = checkpoint["data"]["completed_components"]
    remaining_components = [c for c in plan["components"] if c not in completed_components]
```

---

## Retry and Stability

### Heartbeat Monitoring

```python
async def monitor_generation_heartbeat(generation_id: str):
    """Detect crashed generations via heartbeat timeout"""
    generation = await db.get_generation(generation_id)

    if generation["status"] != "running":
        return

    last_heartbeat = generation.get("heartbeat_at")
    timeout = timedelta(minutes=30)

    if last_heartbeat and datetime.utcnow() - last_heartbeat > timeout:
        # Generation crashed - mark as failed
        await db.update_generation(generation_id, {
            "status": "failed",
            "error": "Heartbeat timeout - process likely crashed"
        })
```

### Retry Service

```python
# app/services/generation/retry.py
async def retry_generation(
    generation_id: str,
    force_new_workspaces: bool = False
):
    """Retry failed generation with progress preservation"""
    generation = await db.get_generation(generation_id)

    # Validate status
    if generation["status"] not in ["pending", "failed", "completed"]:
        raise ValueError(f"Cannot retry generation with status: {generation['status']}")

    if force_new_workspaces:
        # Release old workspaces
        await release_workspaces(generation_id)

        # Allocate new workspaces
        workspaces = await allocate_workspaces(generation_id)
    else:
        # Reuse existing workspaces (preserves git history)
        workspaces = generation.get("workspace_ids", [])

    # Reset status to pending
    await db.update_generation(generation_id, {
        "status": "pending",
        "retry_count": generation.get("retry_count", 0) + 1,
        "retried_at": datetime.utcnow()
    })

    # Re-queue generation
    await queue_generation(generation_id)
```

### Startup Validation

```python
async def startup_validation():
    """Recover from crashes on startup"""
    # Release expired workspace leases
    await release_expired_leases()

    # Detect orphaned generations
    orphaned = await db.find_generations(
        status="running",
        heartbeat_at__lt=datetime.utcnow() - timedelta(minutes=30)
    )

    for generation in orphaned:
        await db.update_generation(generation["id"], {
            "status": "failed",
            "error": "Process crashed (detected on startup)"
        })
```

---

## File Persistence

### Persistent Volumes (Kubernetes)

```yaml
# k8s/persistent-volume.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: specflow-workspaces
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 500Gi
  storageClassName: standard
```

**Mounted at:** `/workspaces`

### Workspace Structure

```
/workspaces/
├── ws-01-1/
│   ├── .git/              # Git repository
│   ├── specs/             # Uploaded specifications
│   ├── src/               # Generated source code
│   ├── tests/             # Generated tests
│   └── docs/              # Generated documentation
├── ws-01-2/
│   └── ...
└── ws-01-3/
    └── ...
```

### Why Persistent Volumes?

- **Crash recovery** - Files survive container restarts
- **Fast file operations** - Native filesystem performance (not object storage)
- **Git integration** - Standard git commands work
- **Multi-container access** - Shared across pods (if needed)

---

## Design Decisions

### Why Database for State Management?

**Problem:** Multiple backend instances need to coordinate.

**Solution:** Database-driven state machine.

**Benefits:**
- ✅ Distributed locking via Firestore transactions
- ✅ Crash recovery - state persists across restarts
- ✅ Multi-tenancy - isolated state per user
- ✅ Atomic operations - prevent race conditions

### Why NFS/Persistent Volumes for Files?

**Problem:** Object storage (Cloud Storage) is slow for git operations.

**Solution:** Persistent volumes with native filesystem.

**Benefits:**
- ✅ Fast file operations (git clone, read, write)
- ✅ Standard tools work (git, editors)
- ✅ Persistence across container restarts
- ✅ Shared access (if needed for multi-pod scaling)

### Why Workspace Pool?

**Problem:** Each generation needs isolated git repositories.

**Solution:** Pre-allocated workspace pool.

**Benefits:**
- ✅ Isolation - Each generation gets dedicated repos
- ✅ Concurrency - Multiple users work simultaneously
- ✅ P10Y integration - Each repo registered separately
- ✅ Cleanup - Automated workspace recycling

**Planned extension (not yet implemented):** Named **workspace pools** (e.g. `default` vs customer-specific), per-API-key encrypted GitHub PATs, K8s Secret API loading for platform credentials, and strict **key_uid** binding on generations. Full spec: [workspace-pool-segregation-plan.md](workspace-pool-segregation-plan.md).

### Why Claude Code SDK?

**Problem:** Need autonomous implementation agents.

**Solution:** Claude Code SDK with tool use.

**Benefits:**
- ✅ Autonomous agents - Complete implementations without human intervention
- ✅ Context awareness - Understands full codebase
- ✅ Tool usage - Can run tests, check syntax, debug
- ✅ Iterative refinement - Self-corrects based on feedback

### Why Kubernetes over Cloud Run?

**Problem:** Cloud Run has 60-minute timeout limit.

**Solution:** Kubernetes with no timeout limits.

**Benefits:**
- ✅ Long-running tasks - Generation takes 8+ hours
- ✅ Persistent volumes - Direct filesystem access
- ✅ No timeout limits - Requests can run indefinitely
- ✅ Fine-grained control - Resource limits, scaling, networking

---

## Next Steps

- **Development Guide**: See [DEVELOPMENT.md](./DEVELOPMENT.md) for local setup
- **API Reference**: See [API_REFERENCE.md](./API_REFERENCE.md) for REST API docs
- **State Transitions**: See [STATE_TRANSITIONS_AUDIT.md](./STATE_TRANSITIONS_AUDIT.md) for detailed state machine audit
- **Checkpoint System**: See [checkpoint_system.md](./checkpoint_system.md) for checkpoint documentation

---

**Architecture Questions?**
- Review code: `backend/app/`
- Check tests: `backend/test/`
- See examples: `backend/scripts/`
