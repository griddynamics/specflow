# Backend Development Guide

> **For Backend Developers**: Local setup, testing, code quality, and development workflow.
>
> **Just want to run SpecFlow locally, not develop it?** See the
> [Local Self-Host Quickstart](../../QUICKSTART.md) — a one-command
> bootstrap (`./specflow-init.sh`) that starts the stack and seeds the emulator without
> the manual steps below. This guide is for contributors working on the backend itself.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Local Development Setup](#local-development-setup)
3. [Environment Variables](#environment-variables)
4. [Running the Backend](#running-the-backend)
5. [Testing](#testing)
6. [Code Quality](#code-quality)
7. [Project Structure](#project-structure)
8. [Development Workflow](#development-workflow)
9. [Debugging](#debugging)
10. [Common Tasks](#common-tasks)

---

## Prerequisites

### Required

- **Docker** - For Firestore emulator and containerization
- **Make** - For command shortcuts
- **Python 3.13+** - Latest Python version
- **uv** - Fast Python package manager ([install](https://github.com/astral-sh/uv))

### API Keys

- **ANTHROPIC_API_KEY** - Claude API key (required)
- **GITHUB_TOKEN** - GitHub access (repo + admin:repo_hook scopes) (required)
- **P10Y_API_KEY** - P10Y API key for code measurement (required)
- **OPENROUTER_API_KEY** - For multi-provider support (optional)

---

## Local Development Setup

### 1. Clone Repository

```bash
git clone https://github.com/griddynamics/specflow.git
cd specflow
```

### 2. Configure Environment

```bash
# Copy example environment file
cp .env.example .env

# Edit with your API keys
nano .env
```

Required variables:
```env
ANTHROPIC_API_KEY=<anthropic-api-key>
GITHUB_TOKEN=<github-token>
P10Y_API_KEY=<p10y-api-key>
```

See [Environment Variables](#environment-variables) section for complete list.

### 3. Start Services

```bash
# Build and start all services
make run
```

This starts:
- **Backend API** on `http://localhost:8000`
- **Firestore Emulator** on `http://localhost:8080`
- **Firestore UI** on `http://localhost:4000` (optional)

### 4. Verify Health

```bash
curl http://localhost:8000/health

# Expected response:
# {"status": "healthy", "version": "1.0"}
```

---

## Environment Variables

### Required Variables

```env
# AI Provider (required)
ANTHROPIC_API_KEY=<anthropic-api-key>

# GitHub Integration (required)
GITHUB_TOKEN=<github-token>

# Code Measurement (required)
P10Y_API_KEY=<p10y-api-key>
```

### Optional Variables

```env
# Multi-Provider Support
OPENROUTER_API_KEY=<openrouter-api-key>

# Notifications
SLACK_WEBHOOK_URL=<slack-webhook-url>
EMAIL_USERNAME=your-email@example.com
EMAIL_PASSWORD=<smtp-password>
EMAIL_FROM=noreply@example.com

# Database Configuration
DATABASE_TYPE=emulator  # Options: emulator, firestore, memory
FIRESTORE_EMULATOR_HOST=localhost:8080
GCP_PROJECT_ID=local-dev

# Logging
LOG_LEVEL=INFO  # Options: DEBUG, INFO, WARNING, ERROR

# Testing
SKIP_AGENT_EXECUTION=false  # Set to true for fast workflow testing
```

### Database Type Options

- **memory** - In-memory database (fast, for unit tests)
- **emulator** - Firestore emulator (local development, integration tests)
- **firestore** - Real Firestore backend for self-hosted deployments

---

## Running the Backend

### Quick Commands

```bash
# Build Docker images
make build

# Start the default local dev stack
make run

# Start with SKIP_MODE (fast testing without AI execution)
make run-skip

# Stop the default local dev stack started by make run / make run-skip
docker compose down --timeout 90

# View logs
make logs

# Clean up containers and volumes
make clean

# Run tests
make test

# Run code quality checks
make check
```

### Manual Startup (without Docker)

```bash
cd backend

# Install dependencies
uv sync

# Start Firestore emulator (in separate terminal)
docker run -p 8080:8080 google/cloud-sdk:latest \
  gcloud beta emulators firestore start --host-port=0.0.0.0:8080

# Set environment
export DATABASE_TYPE=emulator
export FIRESTORE_EMULATOR_HOST=localhost:8080

# Run backend
uv run uvicorn app.main:app --reload --port 8000
```

### SKIP_MODE for Fast Testing

Test workflow orchestration without triggering real AI execution:

```bash
# Start with SKIP_MODE enabled
make run-skip

# Or set environment variable manually
export SKIP_AGENT_EXECUTION=true
make run
```

In SKIP_MODE:
- `agent_query()` returns immediately with `"SKIP_MODE"` response
- No Claude Code SDK agents executed
- No API costs incurred
- Workflow logic and database operations work normally

**Use cases:**
- Test MCP tools without API costs
- Debug workflow orchestration
- Validate database operations
- Test request/response handling

### Skip-mode E2E tests (workspace pool setup)

`make skip-mode-e2e-tests` boots the isolated test stack in SKIP_MODE, prefills the test
container's Firestore with a workspace pool, and drives the MCP tool sequence end to end.
The test stack is ephemeral: every test setup starts by running `make stop`, which tears down
the `specflow-test` compose project and removes `./.specflow-test` before starting again.



---

## Testing

### Unit Tests (Fast)

```bash
cd backend
uv run pytest test/ -v
```

**Characteristics:**
- Uses in-memory database
- No external dependencies

### Integration Tests Setup
Important: even in SKIP_MODE, workspace allocation **clones each pool repo's `repo_url`**. There
are no default repos, so the run fails before setup unless you supply your own repos.

Provide a JSON list of repos you control and pass it via `E2E_WORKSPACE_CONFIG`:

```bash
# 1. Copy the template (lives at the repo root, next to .env.example)
cp e2e-workspace-config.example.json my-test-repos.json

# 2. Edit my-test-repos.json — set repo_url to repos the backend can clone
#    (public, or private with GITHUB_TOKEN exported; e2e-setup forwards GITHUB_TOKEN).
#    p10y_repository_id can be any integers in SKIP_MODE (P10Y is mocked).

# 3. Run, pointing the pool at your repos
make skip-mode-e2e-tests E2E_WORKSPACE_CONFIG=my-test-repos.json
```

`E2E_WORKSPACE_CONFIG` is required by `make e2e-setup`, `make init-firestore`, and
`make init-firestore-dry`. Schema:

```json
[
  {
    "workspace_id": "ws-01-1", 
    "repo_url": "https://github.com/your-org/repo",
    "p10y_repository_id": 10001, 
    "workspace_pool": "default"
  }
]
```

The root `e2e-workspace-config.example.json` includes three `default` pool entries and three
optional `testpool` entries to demonstrate an additional workspace pool. If you do not need the
extra pool, delete the `workspace_pool: "testpool"` entries before running setup.

Override the allocated set size with `E2E_WORKSPACE_COUNT` (default 3); provide at least that many
repos in the same `workspace_pool`.

You can quickly create some repositories with `make create-repos`.

### Integration Tests (Realistic)

```bash
# Starts the isolated specflow-test stack, runs tests, then wipes ./.specflow-test.
make integration-tests
```

**Characteristics:**
- Uses Firestore emulator
- Uses the isolated, ephemeral `specflow-test` Docker Compose project
- Tests complete workflows
- Validates database transactions
- Tests auth middleware

### Test Coverage

```bash
cd backend
uv run pytest test/ --cov=app --cov-report=html
open htmlcov/index.html
```

**Coverage targets:**
- Overall: >80%
- Database abstraction: 100%
- Auth middleware: 100%
- API endpoints: >90%

### Writing Tests

**Example unit test:**
```python
import pytest
from app.database import get_database

@pytest.mark.asyncio
async def test_create_generation():
    db = get_database("memory")

    generation_data = {
        "user_id": "test-user",
        "status": "pending"
    }

    generation_id = await db.create_generation(generation_data)
    assert generation_id.startswith("est-")

    generation = await db.get_generation(generation_id)
    assert generation["status"] == "pending"
```

---

## Code Quality

### Linting and Formatting

```bash
# Run all checks
make check

# Individual tools
cd backend
uv run ruff check app/          # Linting
uv run ruff format app/         # Formatting
uv run mypy app/                # Type checking
```

### Code Standards

**Python:**
- Type hints required for all functions
- Docstrings for public APIs
- Ruff linting (enforced)
- mypy type checking (strict mode)

**Formatting:**
- Line length: 120 characters
- Import sorting: isort via ruff
- String quotes: Double quotes preferred


### Pre-commit Hooks (Recommended)

```bash
# Install pre-commit
pip install pre-commit

# Setup hooks
cd backend
pre-commit install

# Run manually
pre-commit run --all-files
```

---

## Project Structure

```
backend/
├── app/
│   ├── api/                  # REST API endpoints
│   │   ├── v1/
│   │   │   ├── auth.py      # API key management
│   │   │   ├── generations.py  # Generation endpoints
│   │   │   ├── specifications.py  # Spec analysis endpoints
│   │   │   └── workspaces.py    # Workspace management
│   │   └── router.py        # API router configuration
│   ├── database/            # Database abstraction layer
│   │   ├── base.py          # Abstract database interface
│   │   ├── memory.py        # In-memory implementation
│   │   ├── firestore.py     # Firestore implementation
│   │   └── emulator.py      # Emulator implementation
│   ├── middleware/          # FastAPI middleware
│   │   ├── auth.py          # API key authentication
│   │   └── logging.py       # Request/response logging
│   ├── models/              # Data models
│   │   ├── generation.py    # Generation model
│   │   ├── workspace.py     # Workspace model
│   │   └── api_key.py       # API key model
│   ├── schemas/             # Pydantic schemas
│   │   ├── generation.py    # Generation request/response
│   │   ├── workspace.py     # Workspace request/response
│   │   └── auth.py          # Auth request/response
│   ├── services/            # Business logic
│   │   ├── generation/      # Generation service
│   │   ├── workspace/       # Workspace service
│   │   ├── auth/            # Auth service
│   │   └── notifications/   # Notification service
│   ├── workflows/           # AI agent workflows
│   │   ├── planning/        # Planning agent
│   │   ├── phase_execution/ # Phase execution agent
│   │   ├── validator/       # Validator agent
│   │   └── git_janitor/     # Git janitor agent
│   └── main.py              # FastAPI application
├── test/                    # Test suite
│   ├── test_database/       # Database tests
│   ├── test_api/            # API endpoint tests
│   ├── test_services/       # Service tests
│   └── conftest.py          # Pytest fixtures
├── scripts/                 # Utility scripts
│   ├── create_generation_repos.py
│   ├── fix_stuck_workspaces.py
├── pyproject.toml           # Python project config
├── uv.lock                  # Dependency lock file
└── Dockerfile               # Docker image definition
```

---

## Development Workflow

### 1. Create Feature Branch

```bash
git checkout -b feature/your-feature-name
```

### 2. Make Changes

```bash
# Edit code
nano backend/app/services/your_service.py

# Run tests continuously
cd backend
uv run pytest test/ -v --watch
```

### 3. Test Locally

```bash
# Run full test suite
make test

# Run specific test
cd backend
uv run pytest test/test_services/test_your_service.py -v

# Test with Firestore emulator in the isolated ephemeral stack
make integration-tests
```

### 4. Check Code Quality

```bash
# Run all checks
make check

# Fix formatting issues
cd backend
uv run ruff format app/
uv run ruff check app/ --fix
```

### 5. Commit Changes

```bash
git add .
git commit -m "feat: add your feature description"
```

**Commit message format:**
- `feat:` - New feature
- `fix:` - Bug fix
- `docs:` - Documentation changes
- `test:` - Test changes
- `refactor:` - Code refactoring
- `chore:` - Build/tool changes

### 6. Push and Create PR

```bash
git push origin feature/your-feature-name
# Create PR on GitHub
```

---

## Debugging

### Backend Logs

```bash
# View live logs
make logs

# Or with Docker Compose
docker-compose logs backend -f

# Filter by level
docker-compose logs backend | grep ERROR
```

### Debug Mode

```bash
# Enable debug logging
export LOG_LEVEL=DEBUG
make run

# Or edit .env
echo "LOG_LEVEL=DEBUG" >> .env
make run
```

### Interactive Debugging

```python
# Add breakpoint in code
import pdb; pdb.set_trace()

# Or use ipdb (better)
import ipdb; ipdb.set_trace()

# Run without Docker for debugging
cd backend
uv run uvicorn app.main:app --reload
```

### Database Inspection

**Firestore UI** (when using emulator):
- Open http://localhost:4000
- Browse collections: `generations`, `workspaces`, `api_keys`
- View documents, queries, indexes

**Firestore CLI queries:**
```bash
# Get all generations
curl http://localhost:8000/api/v1/generations \
  -H "X-API-Key: gain_xxxxx..."

# Get workspace pool status
curl http://localhost:8000/api/v1/workspace/pool/status \
  -H "X-API-Key: gain_xxxxx..."

# Get system status
curl http://localhost:8000/status \
  -H "X-API-Key: gain_xxxxx..."
```

---

## Common Tasks

### Add New API Endpoint

1. **Create endpoint in `backend/app/api/v1/your_endpoint.py`:**
   ```python
   from fastapi import APIRouter, Depends
   from app.middleware.auth import require_api_key

   router = APIRouter()

   @router.get("/your-endpoint")
   async def your_endpoint(user_id: str = Depends(require_api_key)):
       return {"message": "Hello"}
   ```

2. **Register router in `backend/app/api/v1/__init__.py`:**
   ```python
   from .your_endpoint import router as your_router
   api_router.include_router(your_router, prefix="/your-endpoint", tags=["your-tag"])
   ```

3. **Write tests in `backend/test/test_api/test_your_endpoint.py`**

### Add New Service

1. **Create service in `backend/app/services/your_service/`:**
   ```python
   class YourService:
       def __init__(self, db):
           self.db = db

       async def do_something(self, data):
           # Business logic here
           pass
   ```

2. **Add tests in `backend/test/test_services/test_your_service.py`**

### Add Database Migration

SpecFlow uses Firestore (NoSQL), so migrations are schema-less. However, for data migrations:

1. **Create migration script in `backend/scripts/migrate_xxx.py`**
2. **Run manually via:**
   ```bash
   cd backend
   uv run python scripts/migrate_xxx.py
   ```

### Update Dependencies

```bash
cd backend

# Add dependency
uv add package-name

# Add dev dependency
uv add --dev package-name

# Update all dependencies
uv lock --upgrade

# Sync dependencies
uv sync
```

---

## Next Steps

- **Architecture**: See [ARCHITECTURE.md](./ARCHITECTURE.md) for technical deep dive
- **API Reference**: See [API_REFERENCE.md](./API_REFERENCE.md) for REST API documentation
- **Deployment**: See [../kubernetes/DEPLOYMENT.md](../kubernetes/DEPLOYMENT.md) for deployment guide

---

**Need Help?**
- Check logs: `make logs`
- Review tests: `backend/test/`
- See troubleshooting: [../operations/TROUBLESHOOTING.md](../operations/TROUBLESHOOTING.md)
