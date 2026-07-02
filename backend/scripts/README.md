# Database Initialization Scripts

This directory contains scripts for initializing and managing the SpecFlow database
(SQLite locally by default, or Firestore for production / an already-hosted GCP instance).

## Overview

The SpecFlow backend manages:
- **Workspace pool**: configured workspace sets for running generations
- **Generations**: Generation jobs with state tracking and crash recovery

## Scripts

### `init_db.py`

Initializes the active database's workspace pool from a required workspace-config JSON
file. Backend-agnostic — it goes through the `IDatabase` abstraction, so the same script
seeds sqlite, a manually-run emulator, or Firestore.

**Usage:**

`--workspace-config FILE` is **required** in every invocation (see *Before running* below).

```bash
# IMPORTANT: Run from the backend/ directory (where pyproject.toml is located)

# SQLite (local/Docker default — no separate process needed)
cd backend
export DATABASE_TYPE=sqlite
uv run scripts/init_db.py --workspace-config ../my-test-repos.json

# Dry run (show what would be done)
cd backend
uv run scripts/init_db.py --dry-run --workspace-config ../my-test-repos.json

# Manually-run Firestore emulator
cd backend
export FIRESTORE_EMULATOR_HOST=localhost:8080
# DATABASE_TYPE=emulator is auto-set when FIRESTORE_EMULATOR_HOST is set
uv run scripts/init_db.py --workspace-config ../my-test-repos.json

# Production, or an already-hosted GCP instance (BE CAREFUL!)
cd backend
export GCP_PROJECT_ID=your-project-id
export DATABASE_TYPE=firestore
uv run scripts/init_db.py --prod --workspace-config ../my-test-repos.json
```

**Alternative (from project root):**
```bash
# Using uv's --directory flag
uv run --directory backend scripts/init_db.py --dry-run --workspace-config my-test-repos.json
```

**Important Notes:**
- Defaults to `sqlite` unless `DATABASE_TYPE` is set or `FIRESTORE_EMULATOR_HOST` triggers
  emulator auto-detect
- The script will show debug output about how many keys it finds in the database

**What it does:**
1. Creates one workspace document per workspace-config entry
2. Sets all to "available" status with `clean_verified: true`
3. Configures P10Y repository IDs for each workspace
4. Is idempotent (safe to run multiple times)

**Before running:**

`--workspace-config` is **required** — there are no default repos. Workspace allocation clones each
`repo_url` even in SKIP_MODE, so the pool must point at repos you control. The script refuses to run
without it:

```bash
# Copy the template at the repo root (next to .env.example) and edit repo_url / p10y_repository_id
cp ../e2e-workspace-config.example.json my-test-repos.json

cd backend
export FIRESTORE_EMULATOR_HOST=localhost:8080
uv run scripts/init_firestore.py --yes --workspace-config ../my-test-repos.json
```

The `make` targets thread this through via `E2E_WORKSPACE_CONFIG` (see
[docs/backend/DEVELOPMENT.md](../../docs/backend/DEVELOPMENT.md) → *Skip-mode E2E tests*):

```bash
make skip-mode-e2e-tests E2E_WORKSPACE_CONFIG=my-test-repos.json
```

JSON schema (one object per workspace):

```json
[
  {"workspace_id": "ws-01-1", "repo_url": "https://github.com/your-org/repo",
   "p10y_repository_id": 10001, "workspace_pool": "default"}
]
```

There is no in-script fallback list: the workspace pool always comes from the `--workspace-config`
file. Set real GitHub repository URLs and P10Y repository IDs there.

The root `e2e-workspace-config.example.json` includes an optional `testpool` set that demonstrates
an additional workspace pool. Delete those `workspace_pool: "testpool"` entries if you only need the
default pool; the optional `extra_pool_user` key is only seeded when that pool is present.

## Database Schema

The initialization script follows the schema defined in `docs/deployment/state-management.md`.

### Workspace Schema

```python
{
    # Core fields
    "repo_url": str,                    # GitHub repository URL
    "p10y_repository_id": int,          # P10Y repository ID
    "set_number": int,                  # Set number (1-10)
    
    # Allocation state
    "status": str,                      # "available" | "allocated" | "cleaning" | "stuck"
    "locked_by": Optional[str],         # Generation ID using this workspace
    "locked_at": Optional[datetime],    # When allocated
    "lease_expires_at": Optional[datetime],  # Lease expiration (crash detection)
    
    # Safety fields
    "clean_verified": bool,             # CRITICAL: must be true to allocate
    "last_used_by": Optional[str],      # Previous generation (debugging)
    "last_cleaned_at": datetime,        # Last cleanup timestamp
    
    # Audit trail
    "allocation_history": List[dict],   # History of allocations
    
    # Error tracking
    "error": Optional[str],             # Error message if stuck
}
```

### Generation Schema

Generations are created by the API and follow this schema:

```python
{
    # Core fields
    "user_id": str,
    "status": str,  # "pending" | "initializing" | "running" | "completed" | "failed"
    "workspace_ids": List[str],
    "parameters": dict,  # Original request parameters
    
    # Timestamps
    "created_at": datetime,
    "status_changed_at": datetime,
    "started_at": Optional[datetime],
    "completed_at": Optional[datetime],
    
    # Safety fields (crash detection)
    "last_heartbeat": datetime,
    "lease_expires_at": Optional[datetime],
    "instance_id": Optional[str],
    
    # Retry handling
    "retry_count": int,
    "max_retries": int,
    
    # Progress tracking (for retry continuity)
    "progress": dict,
    
    # Audit trail
    "state_history": List[dict],
    
    # Results
    "result": Optional[dict],
    "error": Optional[str],
}
```

## Workspace Configuration

Each workspace set requires 3 repositories configured in P10Y. Example configuration:

```python
# Set 1
{"repo_url": "https://github.com/your-org/specflow-workspace-01-1", "p10y_id": 74901},
{"repo_url": "https://github.com/your-org/specflow-workspace-01-2", "p10y_id": 74902},
{"repo_url": "https://github.com/your-org/specflow-workspace-01-3", "p10y_id": 74903},

# Set 2
{"repo_url": "https://github.com/your-org/specflow-workspace-02-1", "p10y_id": 74911},
...
```

**Important:**
- Each workspace needs its own GitHub repository
- Each repository needs to be registered in P10Y
- P10Y repository IDs must be unique
- Workspaces are grouped in sets of 3 (all 3 allocated together per generation)

## Development Workflow

### 1. Start Firestore Emulator

```bash
# Install emulator if not already installed
firebase setup:emulators:firestore

# Start emulator
firebase emulators:start --only firestore
```

### 2. Initialize Database

```bash
export FIRESTORE_EMULATOR_HOST=localhost:8080
python backend/scripts/init_firestore.py --workspace-config my-test-repos.json
```

### 3. Verify Initialization

```bash
# Check startup validation
curl http://localhost:8000/health

# Check workspace pool status
curl http://localhost:8000/api/v1/workspace/pool/status
```

## Troubleshooting

### "ERROR: FIRESTORE_EMULATOR_HOST not set"

Set the environment variable:
```bash
export FIRESTORE_EMULATOR_HOST=localhost:8080
```

### "ERROR: Failed to connect to database"

Ensure Firestore emulator is running:
```bash
firebase emulators:start --only firestore
```

### "WARNING: Not all sets are fully available"

Some workspaces may be in allocated/cleaning state. Check:
```bash
python backend/scripts/init_firestore.py --dry-run --workspace-config my-test-repos.json
```

Then run cleanup or reset:
```bash
python backend/scripts/init_firestore.py --workspace-config my-test-repos.json
```

### Workspace stuck in "stuck" state

Manually reset the workspace in Firestore console, or delete and recreate:
```bash
python backend/scripts/init_firestore.py --workspace-config my-test-repos.json
# Answer "yes" to update/recreate
```

## Production Deployment

### Prerequisites

1. **GitHub Repositories**: Create one repository per workspace-config entry
2. **P10Y Registration**: Register each repository in P10Y
3. **Firestore**: Enable Firestore in your GCP project
4. **Service Account**: Create service account with Firestore permissions

### Initialization Steps

1. **Build the workspace-config file**:
   ```json
   // prod-repos.json — one object per workspace
   [
     {"workspace_id": "ws-01-1", "repo_url": "https://github.com/your-org/specflow-workspace-01-1",
      "p10y_repository_id": YOUR_ID, "workspace_pool": "default"}
   ]
   ```

2. **Authenticate**:
   ```bash
   export GCP_PROJECT_ID=your-project-id
   gcloud auth application-default login
   ```

3. **Run initialization**:
   ```bash
   python backend/scripts/init_firestore.py --prod --workspace-config prod-repos.json
   ```

4. **Verify**:
   ```bash
   # Check health endpoint
   curl https://your-backend.run.app/health
   
   # Check workspace pool
   curl https://your-backend.run.app/api/v1/workspace/pool/status
   ```

## Schema Import/Export

To import the schema into your codebase:

```python
from app.database.factory import create_database

db = create_database()

# Get workspace schema from existing workspace
workspace = db.get("workspaces", "ws-01-1")
print(workspace)

# Get generation schema from existing generation
generation = db.get("generations", "est-123")
print(generation)
```

The schemas are NOT hard-coded in the script - they're imported from the backend code. This ensures consistency between:
- Database initialization
- Service layer operations
- API responses
- State management logic

## References

- **State Management**: `docs/deployment/state-management.md`
- **Implementation Plan**: `docs/deployment/WORKSPACE_AND_ESTIMATION_SERVICES_PLAN.md`
- **Database Layer**: `backend/app/database/`
- **Services**: `backend/app/services/`
