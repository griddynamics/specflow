.PHONY: run run-process stop-process stop stop-test clean help base build ops-retry-run ops-cancel-run check-complexity check-complexity-diff check-complexity-cc check-complexity-mi secret-scan secret-scan-history secret-scan-gitleaks secret-scan-trufflehog skip-mode-e2e-tests contract-validation-e2e-tests shutdown-recovery-e2e-tests real-e2e-tests quickstart require-e2e-workspace-config

# Default target
.DEFAULT_GOAL := build

# One-command local quickstart: bootstrap backend, seed the database, emit MCP config
quickstart:
	@./specflow-init.sh $(ARGS)

# Allow WORKSPACE_MOUNT_PATH to be passed as a parameter
WORKSPACE_MOUNT_PATH ?= ./workspaces

BACKEND_URL ?= http://localhost:8000
# Local/Docker default. Override to "firestore" to connect to an already-hosted, GCP-managed
# Firestore instance, or "emulator" to connect to a manually-run Firestore emulator process —
# SpecFlow itself never deploys/manages either locally.
DATABASE_TYPE ?= sqlite
# Central SQLite file, bind-mounted into the backend container at the same path (see
# docker-compose.yml) — one database shared across every local project/MCP session, matching
# the old shared Firestore-emulator model. Host-side scripts (init_db.py, tests) read/write
# the exact same file directly; no docker exec needed.
SPECFLOW_HOME_PATH ?= $(HOME)/.specflow
SQLITE_DB_PATH ?= $(SPECFLOW_HOME_PATH)/db/specflow.db
# Only the emulator backend talks to a Firestore emulator. Default a host for it, but leave
# it unset for the sqlite/firestore backends so the emulator-only tests skip (they probe this
# host for a live process) instead of dialing a dead port — no emulator container runs anymore.
ifeq ($(DATABASE_TYPE),emulator)
FIRESTORE_EMULATOR_HOST ?= localhost:8080
else
FIRESTORE_EMULATOR_HOST ?=
endif
# Must match docker-compose.yml defaults so host-side seeding/tests see the same
# named Firestore database as the backend container (quickstart sets these via specflow-init.sh).
GCP_PROJECT_ID ?= local-dev
FIRESTORE_DATABASE_NAME ?= specflow
E2E_WORKSPACE_COUNT ?= 3

# Workspace-pool repos used to prefill the test database (init_db.py). REQUIRED: there are
# no default repos, and SKIP_MODE still clones each repo during allocation — so point this at a JSON
# list of YOUR test repos. The e2e targets refuse to run without it.
# Schema: [{"workspace_id": "ws-01-1", "repo_url": "https://github.com/org/repo",
#           "p10y_repository_id": 12345, "workspace_pool": "default"}, ...]
# Convention: copy the template to e2e-workspace-config.json (gitignored) at the repo root and it
# is auto-detected below — no flag needed:
#   cp e2e-workspace-config.example.json e2e-workspace-config.json   # then edit repo_url
#   make skip-mode-e2e-tests
# Or point at any path explicitly:  make skip-mode-e2e-tests E2E_WORKSPACE_CONFIG=my-test-repos.json
E2E_WORKSPACE_CONFIG ?= $(wildcard e2e-workspace-config.json)
# --yes keeps re-runs non-interactive; --workspace-config supplies the (required) workspace pool.
INIT_DB_ARGS := --yes
ifneq ($(strip $(E2E_WORKSPACE_CONFIG)),)
INIT_DB_ARGS += --workspace-config $(abspath $(E2E_WORKSPACE_CONFIG))
endif

# ── Isolated local-testing stack ───────────────────────────────────────────────────────────
# Contributor test runs (e2e + integration) use a SEPARATE docker-compose project, container
# names, host ports, and workspace/database paths from a self-hosted quickstart deployment
# (quickstart uses the default project + ./workspaces + ~/.specflow). So a test run never
# clobbers a running quickstart OR the real central SQLite database: `make stop` tears down
# the test stack and removes its ephemeral ./.specflow-test state (which nests its own
# isolated specflow-home/db/specflow.db, cleaned up the same way).
#
# Applied as target-specific *exported* vars so they also reach prerequisite targets
# (run-detached / run-detached-skip) and sub-makes (`$(MAKE) stop`, `$(MAKE) e2e-setup`).
SPECFLOW_BACKEND_CONTAINER ?= specflow-backend
# Absolute (not repo-root-relative): several e2e targets do `cd backend &&` before using
# vars derived from this (e.g. SQLITE_DB_PATH for init_db.py). A relative path would then
# resolve against backend/ instead of the repo root, silently writing/reading a second,
# never-cleaned sqlite file that diverges from the one docker-compose bind-mounts into the
# container — the container sees an empty db while init_db.py reports data "already exists".
TEST_WORKSPACE_MOUNT_PATH := $(CURDIR)/.specflow-test
TEST_SPECFLOW_HOME_PATH := $(TEST_WORKSPACE_MOUNT_PATH)/specflow-home
TEST_STACK_TARGETS := e2e-setup skip-mode-e2e-tests contract-validation-e2e-tests shutdown-recovery-e2e-tests real-e2e-tests integration-tests stop stop-test
$(TEST_STACK_TARGETS): export COMPOSE_PROJECT_NAME := specflow-test
$(TEST_STACK_TARGETS): export WORKSPACE_MOUNT_PATH := $(TEST_WORKSPACE_MOUNT_PATH)
$(TEST_STACK_TARGETS): export SPECFLOW_BACKEND_CONTAINER := specflow-test-backend
$(TEST_STACK_TARGETS): export SPECFLOW_MCP_CONTAINER := specflow-test-mcp-server
$(TEST_STACK_TARGETS): export SPECFLOW_BACKEND_PORT := 18000
$(TEST_STACK_TARGETS): export BACKEND_URL := http://localhost:18000
$(TEST_STACK_TARGETS): export SPECFLOW_HOME_MOUNT_PATH := $(TEST_SPECFLOW_HOME_PATH)
$(TEST_STACK_TARGETS): export SQLITE_DB_PATH := $(TEST_SPECFLOW_HOME_PATH)/db/specflow.db
$(TEST_STACK_TARGETS): export GCP_PROJECT_ID := $(GCP_PROJECT_ID)
$(TEST_STACK_TARGETS): export FIRESTORE_DATABASE_NAME := $(FIRESTORE_DATABASE_NAME)

# Guard reused by every e2e target: fail fast (before building/starting anything) with a clear,
# actionable message when the user hasn't supplied their own test repos.
define require-e2e-workspace-config
	@if [ -z "$(strip $(E2E_WORKSPACE_CONFIG))" ]; then \
		echo "ERROR: no workspace config found — there are no default test repos."; \
		echo "       Copy the template to the auto-detected path and point it at repos you control:"; \
		echo "         cp e2e-workspace-config.example.json e2e-workspace-config.json"; \
		echo "         # edit repo_url / p10y_repository_id in e2e-workspace-config.json"; \
		echo "         make $@"; \
		echo "       (or pass any path explicitly: make $@ E2E_WORKSPACE_CONFIG=my-test-repos.json)"; \
		exit 1; \
	fi
endef

require-e2e-workspace-config:
	$(require-e2e-workspace-config)

# Parameterizable workspace and logs directories for run-mounts targets
WORKSPACES_LOCAL_DIR ?= $(HOME)/Documents
AGENT_LOGS_DIR ?= $(HOME)/Documents/specflow_logs

# Run all services
build:
	@echo "🔨 Building service images..."
	docker-compose build

run:
	@echo "🚀 Starting services in DEV mode (DATABASE_TYPE=$(DATABASE_TYPE))..."
	@echo "📁 Using WORKSPACE_MOUNT_PATH: $(WORKSPACE_MOUNT_PATH)"
	@echo "💾 Database: SQLite at $(SQLITE_DB_PATH) (no GCP credentials needed)"
	WORKSPACE_MOUNT_PATH=$(WORKSPACE_MOUNT_PATH) docker-compose up --no-build

# Run with SKIP_MODE enabled (agents return immediately without execution)
run-skip:
	@echo "🚀 Starting services in DEV mode with SKIP_MODE enabled..."
	@echo "📁 Using WORKSPACE_MOUNT_PATH: $(WORKSPACE_MOUNT_PATH)"
	@echo "💾 Database: SQLite at $(SQLITE_DB_PATH) (no GCP credentials needed)"
	@echo "⏭️  Agent execution: SKIPPED (testing mode)"
	WORKSPACE_MOUNT_PATH=$(WORKSPACE_MOUNT_PATH) SKIP_AGENT_EXECUTION=true docker-compose up --no-build

# Run in detached mode
run-detached: build
	@echo "🚀 Starting services in background (DEV mode)..."
	@echo "📁 Using WORKSPACE_MOUNT_PATH: $(WORKSPACE_MOUNT_PATH)"
	@echo "💾 Database: SQLite at $(SQLITE_DB_PATH)"
	WORKSPACE_MOUNT_PATH=$(WORKSPACE_MOUNT_PATH) docker-compose up -d --no-build

# Run in detached mode with SKIP_MODE (same as run-detached: build then up — cache-friendly)
run-detached-skip: build
	@echo "🚀 Starting services in background (DEV mode) with SKIP_MODE..."
	@echo "📁 Using WORKSPACE_MOUNT_PATH: $(WORKSPACE_MOUNT_PATH)"
	@echo "💾 Database: SQLite at $(SQLITE_DB_PATH)"
	@echo "⏭️  Agent execution: SKIPPED (testing mode)"
	WORKSPACE_MOUNT_PATH=$(WORKSPACE_MOUNT_PATH) SKIP_AGENT_EXECUTION=true docker-compose up -d --no-build

# Bare-metal backend (BACKEND_RUNTIME=process) — no Docker. Requires the developer
# environment already installed (Python 3.14, uv, `cd backend && uv sync`) and the
# host OS sandbox (bubblewrap on Linux / Seatbelt on macOS). Starts detached with a
# fail-closed sandbox preflight; see docs/backend/backend-runtime.md.
run-process:
	@echo "🚀 Starting backend bare-metal (BACKEND_RUNTIME=process)..."
	@cd mcp_server && uv run python -c "import asyncio, sys; from pathlib import Path; from services import local_env; sys.exit(asyncio.run(local_env.run_backend_process_cli(Path('$(CURDIR)'))))"

# Stop a detached bare-metal backend started by run-process (or the TUI).
stop-process:
	@cd mcp_server && uv run python -c "from pathlib import Path; from services import local_env; print('🛑 stopped' if local_env.stop_backend_process(Path('$(CURDIR)')) else 'ℹ️  no running backend process')"

# Stop ONLY the isolated local-testing stack (project: specflow-test) and wipe its ephemeral
# workspace/database state. Quickstart is stopped outside this Make target.
stop:
	@echo "🛑 Stopping the isolated local-testing stack (project: specflow-test)..."
	docker-compose down --timeout 90
	@if [ "$(WORKSPACE_MOUNT_PATH)" != "$(TEST_WORKSPACE_MOUNT_PATH)" ]; then \
		echo "Refusing to remove unexpected test workspace path: $(WORKSPACE_MOUNT_PATH)"; \
		exit 1; \
	fi
	rm -rf "$(WORKSPACE_MOUNT_PATH)"

# Backward-compatible alias.
stop-test:
	@$(MAKE) stop

# Clean up containers, images, and volumes
clean:
	@echo "🧹 Cleaning up..."
	docker-compose down -v
	docker rmi -f specflow-backend:latest specflow-mcp-server:latest 2>/dev/null || true

# Show logs
logs:
	docker-compose logs -f

# Static checks
check:
	@echo "🔍 Running static checks..."
	@echo "1️⃣  Checking syntax..."
	@cd backend && uv run python -m compileall -q app/
	@echo "✅ Syntax check passed"
	@echo "\n2️⃣  Running ruff linter..."
	@cd backend && uv run ruff check .
	@echo "✅ Ruff check passed"
	@echo "\n3️⃣  Running mypy type checker..."
	@cd backend && uv run mypy app/ --ignore-missing-imports
	@echo "✅ Type check passed"
	@echo "\n4️⃣   Checking for dead code..."	
	@cd backend && uv run vulture app/ --min-confidence 80
	@echo "✅ Dead code check passed"
	@echo "\n✨ All checks passed!"

# Secret scanning (offline, read-only; requires local CLI tools).
define require-secret-scanner
	@command -v $(1) >/dev/null 2>&1 || { \
		echo "ERROR: $(1) not found. Install with: brew install $(2)"; \
		exit 1; \
	}
endef

secret-scan: secret-scan-gitleaks secret-scan-trufflehog

secret-scan-history:
	@$(MAKE) secret-scan-gitleaks SECRET_SCAN_GIT_HISTORY=1
	@$(MAKE) secret-scan-trufflehog SECRET_SCAN_GIT_HISTORY=1

secret-scan-gitleaks:
	$(call require-secret-scanner,gitleaks,gitleaks)
	@echo "🔍 gitleaks: working tree..."
	@gitleaks detect --source . --no-git --verbose --redact
ifneq ($(SECRET_SCAN_GIT_HISTORY),)
	@echo "🔍 gitleaks: full local git history..."
	@gitleaks detect --source . --log-opts="--all" --verbose --redact
endif

secret-scan-trufflehog:
	$(call require-secret-scanner,trufflehog,trufflehog)
	@echo "🔍 trufflehog: working tree..."
	@bash -o pipefail -c 'trufflehog filesystem . --exclude-paths .secret-scan-exclude-paths.txt --force-skip-binaries --no-verification --results=verified,unverified,unknown --json | python3 scripts/redact-trufflehog-json.py'
ifneq ($(SECRET_SCAN_GIT_HISTORY),)
	@echo "🔍 trufflehog: full local git history..."
	@bash -o pipefail -c 'trufflehog git file://. --no-verification --results=verified,unverified,unknown --json | python3 scripts/redact-trufflehog-json.py'
endif

# Cyclomatic complexity (backend app/) — summary only
check-complexity:
	@echo "📊 Cyclomatic complexity (radon cc, averages)..."
	@cd backend && uv run radon cc app -a 2>/dev/null | tail -n 2
	@echo "✅ Done"

# Radon diff metric for check-complexity-diff: cc | mi | hal
METRIC ?= cc

# vs main: repo-wide diff + averages (METRIC=cc | mi | hal). Default cc.
check-complexity-diff:
	@cd backend && uv run python scripts/radon_diff_vs_main.py --metric $(METRIC)

# Radon on one path (FILE relative to backend/, e.g. app/api/v1/auth.py)
check-complexity-cc:
ifndef FILE
	$(error FILE is required, e.g. make check-complexity-cc FILE=app/main.py)
endif
	@cd backend && uv run radon cc -s $(FILE)

check-complexity-mi:
ifndef FILE
	$(error FILE is required, e.g. make check-complexity-mi FILE=app/main.py)
endif
	@cd backend && uv run radon mi -s $(FILE)

# Format code
format:
	@echo "🎨 Formatting code..."
	@cd backend && uv run ruff check . --fix
	@echo "✅ Code formatted"

# Initialize the active database backend (sqlite by default; override DATABASE_TYPE for
# firestore/emulator). Runs host-side against the same file the backend container has
# bind-mounted (sqlite) or the same emulator host:port (emulator) — no docker exec needed.
init-db:
	$(require-e2e-workspace-config)
	@echo "🔧 Initializing database (DATABASE_TYPE=$(DATABASE_TYPE))..."
	@cd backend && \
		DATABASE_TYPE=$(DATABASE_TYPE) \
		SQLITE_DB_PATH=$(SQLITE_DB_PATH) \
		FIRESTORE_EMULATOR_HOST=$(FIRESTORE_EMULATOR_HOST) \
		uv run scripts/init_db.py $(INIT_DB_ARGS)

# Backward-compatible alias.
init-firestore:
	@$(MAKE) init-db

# Initialize the active database backend (dry run)
init-db-dry:
	$(require-e2e-workspace-config)
	@echo "🔧 Dry run: Initializing database (DATABASE_TYPE=$(DATABASE_TYPE))..."
	@cd backend && \
		DATABASE_TYPE=$(DATABASE_TYPE) \
		SQLITE_DB_PATH=$(SQLITE_DB_PATH) \
		FIRESTORE_EMULATOR_HOST=$(FIRESTORE_EMULATOR_HOST) \
		uv run scripts/init_db.py --dry-run $(INIT_DB_ARGS)

# Backward-compatible alias.
init-firestore-dry:
	@$(MAKE) init-db-dry

# Create estimation repositories
# Usage: make create-repos START=7 END=9
# Usage: make create-repos START=1 END=3 PREFIX=test-workspace
# Usage: make create-repos START=7 END=9 SKIP_FIRESTORE=1
create-repos:
	@if [ -z "$(START)" ] || [ -z "$(END)" ]; then \
		echo "❌ Error: START and END are required"; \
		echo "Usage: make create-repos START=7 END=9"; \
		echo "       make create-repos START=1 END=3 PREFIX=test-workspace"; \
		exit 1; \
	fi
	@echo "🚀 Creating generation workspace repositories $(START)-$(END)..."
	@cd backend && uv run python scripts/create_generation_session_repos.py \
		--start $(START) \
		--end $(END) \
		$(if $(PREFIX),--prefix $(PREFIX),) \
		$(if $(DELAY),--delay $(DELAY),) \
		$(if $(POLL_TIMEOUT),--poll-timeout $(POLL_TIMEOUT),) \
		$(if $(POLL_INTERVAL),--poll-interval $(POLL_INTERVAL),) \
		$(if $(SKIP_GITHUB),--skip-github,) \
		$(if $(SKIP_FIRESTORE),--skip-firestore,)

# Run unit tests (in-memory database)
unit-tests:
	@echo "🧪 Running unit tests (in-memory database)..."
	@cd backend && \
		DATABASE_TYPE=memory \
		AUTH_MODE=api_key \
		uv run pytest test/ -v
	@echo "🧪 Running MCP server unit tests..."
	@cd mcp_server && uv run pytest tests/ -v
	@echo "✅ Unit tests passed"

# Run integration tests (sqlite by default; override DATABASE_TYPE=emulator/firestore)
integration-tests:
	@$(MAKE) stop
	@$(MAKE) run-detached
	@echo "⏳ Waiting for services to be ready..."
	@sleep 5
	@echo "🧪 Running integration tests (DATABASE_TYPE=$(DATABASE_TYPE))..."
	@cd backend && \
		DATABASE_TYPE=$(DATABASE_TYPE) \
		SQLITE_DB_PATH=$(SQLITE_DB_PATH) \
		FIRESTORE_EMULATOR_HOST=$(FIRESTORE_EMULATOR_HOST) \
		AUTH_MODE=api_key \
		RUN_GIT_INTEGRATION_TESTS=1 \
		uv run pytest test/ -v --cov=app
	@echo "✅ Integration tests passed"
	@$(MAKE) stop

# Setup E2E testing environment (starts services, initializes Firestore, creates example specs)
e2e-setup:
	$(require-e2e-workspace-config)
	@$(MAKE) stop
	@$(MAKE) run-detached-skip
	@echo "Run this with GITHUB_TOKEN to populate also users github tokens that use the non-default workspace pools"
	@echo "⏳ Waiting for services to be ready..."
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
		if curl -sf $(BACKEND_URL)/health > /dev/null 2>&1; then \
			echo "✅ Backend is ready"; \
			break; \
		fi; \
		if [ $$i -eq 10 ]; then \
			echo "⚠️  Backend not ready after 10 attempts. Check logs with: make logs"; \
			exit 1; \
		fi; \
		echo "   Attempt $$i/10..."; \
		sleep 2; \
	done
	@echo "🔧 Initializing database (DATABASE_TYPE=$(DATABASE_TYPE))..."
	@cd backend && \
		GITHUB_TOKEN=$${GITHUB_TOKEN:-} \
		DATABASE_TYPE=$(DATABASE_TYPE) \
		SQLITE_DB_PATH=$(SQLITE_DB_PATH) \
		FIRESTORE_EMULATOR_HOST=$(FIRESTORE_EMULATOR_HOST) \
		uv run scripts/init_db.py $(INIT_DB_ARGS) || (echo "⚠️  Database initialization failed. Services may still be starting. Retry with: make init-db" && exit 1)
	@echo "🔑 Fetching API key..."
	@cd backend && \
		uv run python ../scripts/get-api-key.py || (echo "⚠️  Could not fetch API key" && exit 1)
	@echo "📝 Creating example specifications..."
	@./scripts/create-example-specs.sh /tmp/specflow-e2e-specs
	@echo ""
	@echo "=========================================="
	@echo "✅ E2E Environment Ready!"
	@echo "=========================================="
	@echo ""
	@echo "📋 Services running:"
	@echo "  - Backend API: $(BACKEND_URL)"
	@echo "  - Database:    $(DATABASE_TYPE) ($(SQLITE_DB_PATH))"
	@echo ""
	@echo "📁 Example specifications created at:"
	@echo "  /tmp/specflow-e2e-specs"
	@echo ""
	@echo "🔌 To connect Cursor MCP:"
	@echo "  1. Configure Cursor MCP (~/.cursor/mcp.json):"
	@echo "     {"
	@echo "       \"mcpServers\": {"
	@echo "         \"specflow\": {"
	@echo "           \"command\": \"uv\","
	@echo "           \"args\": [\"run\", \"python\", \"-m\", \"server\"],"
	@echo "           \"cwd\": \"$(shell pwd)/mcp_server\","
	@echo "           \"env\": {"
	@echo "             \"BACKEND_URL\": \"$(BACKEND_URL)\""
	@echo "           }"
	@echo "         }"
	@echo "       }"
	@echo "     }"
	@echo "  2. Restart Cursor IDE"
	@echo "  3. Use spec_path: /tmp/specflow-e2e-specs when calling MCP tools"
	@echo ""
	@echo "🧪 Test the setup:"
	@echo "  curl $(BACKEND_URL)/health"
	@echo ""
	@echo "🛑 To stop services:"
	@echo "  make stop"
	@echo ""

# Skip-mode E2E tests (fast, ~2 min): validates MCP tool sequence against local stack with SKIP_AGENT_EXECUTION=true.
# Override workspace count:  make skip-mode-e2e-tests E2E_WORKSPACE_COUNT=1
# Provide your own pool repos (REQUIRED — no defaults, and repos are cloned even in SKIP_MODE):
#   make skip-mode-e2e-tests E2E_WORKSPACE_CONFIG=my-test-repos.json   (see e2e-workspace-config.example.json)
skip-mode-e2e-tests:
	$(require-e2e-workspace-config)
	@$(MAKE) e2e-setup
	@echo "🧪 Running skip-mode E2E tests (workspace_count=$(E2E_WORKSPACE_COUNT))..."
	@cd mcp_server && WORKSPACE_COUNT=$(E2E_WORKSPACE_COUNT) \
	  BACKEND_URL=$(BACKEND_URL) \
	  uv run python -m tests.e2e.scenarios.skip_mode
	# skip_mode leaves its workspace set in CLEANING (a completed generation only ever
	# transitions ALLOCATED -> CLEANING; the sole reclaim path is the 2h stuck-cleaning
	# job). With a single-set pool the next scenario would then find nothing AVAILABLE, so
	# re-seed to reset the pool between scenarios. --replace overwrites the CLEANING rows
	# back to available/clean_verified. Reuses the init-db target (single source of truth
	# for the seeding invocation); it inherits the test-stack SQLITE_DB_PATH via the env.
	@echo "♻️  Re-seeding workspace pool so contract-validation starts with a fresh set..."
	@$(MAKE) init-db INIT_DB_ARGS="$(INIT_DB_ARGS) --replace"
	@echo "🧪 Running contract-validation E2E tests (reject-before-allocate)..."
	@cd mcp_server && WORKSPACE_COUNT=$(E2E_WORKSPACE_COUNT) \
	  BACKEND_URL=$(BACKEND_URL) \
	  uv run python -m tests.e2e.scenarios.contract_validation
	@echo "✅ Skip-mode E2E tests passed"

# Contract-validation E2E only (reject-before-allocate gate), without the full skip-mode run.
contract-validation-e2e-tests:
	$(require-e2e-workspace-config)
	@$(MAKE) e2e-setup
	@echo "🧪 Running contract-validation E2E tests (workspace_count=$(E2E_WORKSPACE_COUNT))..."
	@cd mcp_server && WORKSPACE_COUNT=$(E2E_WORKSPACE_COUNT) \
	  BACKEND_URL=$(BACKEND_URL) \
	  uv run python -m tests.e2e.scenarios.contract_validation
	@echo "✅ Contract-validation E2E tests passed"

# Shutdown + boot-recovery E2E (fast, ~3 min): starts the stack in SKIP mode, drives the
# backend HTTP API to run a generation, restarts the backend container mid-run (SIGTERM →
# boot), and verifies the interrupted session is auto-recovered.
shutdown-recovery-e2e-tests:
	$(require-e2e-workspace-config)
	@SKIP_AGENT_EXECUTION=true $(MAKE) e2e-setup
	@echo "🧪 Running shutdown + boot-recovery E2E (backend HTTP-driven)..."
	@cd backend && RUN_SHUTDOWN_RECOVERY_E2E=1 \
	   BACKEND_URL=$(BACKEND_URL) BACKEND_CONTAINER=$(SPECFLOW_BACKEND_CONTAINER) \
	   FIRESTORE_EMULATOR_HOST=$(FIRESTORE_EMULATOR_HOST) \
	   uv run pytest test/e2e/test_shutdown_recovery_live.py -v -s
	@echo "✅ Shutdown + boot-recovery E2E passed"

# Real E2E tests (slow, 30–90 min): full agent execution with 3 workspaces.
# Requires SPECFLOW_API_KEY and a real or dev backend (set BACKEND_URL).
# Override workspace count: make real-e2e-tests E2E_WORKSPACE_COUNT=1
real-e2e-tests:
	$(require-e2e-workspace-config)
	@$(MAKE) e2e-setup
	@echo "🧪 Running real E2E tests (workspace_count=$(E2E_WORKSPACE_COUNT))..."
	@cd mcp_server && WORKSPACE_COUNT=$(E2E_WORKSPACE_COUNT) \
	  BACKEND_URL=$(BACKEND_URL) \
	  SKIP_AGENT_EXECUTION= \
	  uv run python -m tests.e2e.scenarios.real_run
	@echo "✅ Real E2E tests passed"

# Help
help:
	@echo "Available commands:"
	@echo ""
	@echo "Build & Run:"
	@echo "  make quickstart                                 - Bootstrap local quickstart (build, start, seed, emit MCP config)"
	@echo "  make quickstart ARGS='--dry-run'                - Dry run (no containers started, keyless mcp-config.json written)"
	@echo "  make quickstart ARGS='--skip-repos'             - Skip GitHub repo creation (supply .specflow-local/workspaces.json)"
	@echo "  make build                                      - Build base image and all services (default)"
	@echo "  make base                                       - Build only the base image"
	@echo "  make run                                        - Start in DEV mode (SQLite, no GCP credentials needed)"
	@echo "  make run-skip                                   - Start in DEV mode with SKIP_MODE (agents return immediately)"
	@echo "  make run WORKSPACE_MOUNT_PATH=/path             - Start with custom workspace mount"
	@echo "  make run-detached                               - Start in background (DEV mode)"
	@echo "  make run-detached-skip                          - Start in background (DEV mode) with SKIP_MODE"
	@echo ""
	@echo "Control:"
	@echo "  make stop                                       - Stop tests and wipe ephemeral ./.specflow-test state"
	@echo "  make stop-test                                  - Alias for make stop"
	@echo "  make logs                                       - Show and follow logs"
	@echo "  make clean                                      - Remove all containers, images, and volumes"
	@echo ""
	@echo "Development:"
	@echo "  make check                                      - Run static checks (syntax + ruff + mypy)"
	@echo "  make check-complexity                           - Radon complexity summary (averages, current app/)"
	@echo "  make check-complexity-diff METRIC=cc            - Radon vs main (repo-wide); METRIC=cc|mi|hal"
	@echo "  make check-complexity-cc FILE=app/foo.py        - Radon cyclomatic complexity (-s) on one path"
	@echo "  make check-complexity-mi FILE=app/foo.py        - Radon maintainability index (-s) on one path"
	@echo "  make secret-scan                                - Run local read-only secret scans"
	@echo "  make secret-scan-history                        - Run secret scans including full local git history"
	@echo "  make format                                     - Format code with ruff"
	@echo "  make unit-tests                                 - Run unit tests (in-memory database, fast)"
	@echo "  make integration-tests                          - Run integration tests (SQLite by default; DATABASE_TYPE=emulator|firestore to override)"
	@echo "  (e2e + integration tests run in an isolated ephemeral stack: project specflow-test, mount ./.specflow-test)"
	@echo "  make e2e-setup                                  - Setup E2E environment (starts services, initializes the database, creates example specs)"
	@echo "  make skip-mode-e2e-tests                        - Fast E2E of the MCP tool sequence + contract gate (SKIP mode)"
	@echo "      E2E_WORKSPACE_CONFIG=path.json              - REQUIRED: prefill the test pool with your own repos (no defaults)"
	@echo "  make contract-validation-e2e-tests              - E2E: contract rejections reject before allocating (no orphan workspaces)"
	@echo "  make shutdown-recovery-e2e-tests                - E2E: restart backend mid-run, verify graceful shutdown + boot recovery"
	@echo "  make real-e2e-tests                             - Full real-agent E2E (slow, 30-90 min)"
	@echo "  make init-db-dry                                - Initialize the active database (dry run, shows what would be done)"
	@echo "  make init-db                                    - Initialize the active database (SQLite by default; DATABASE_TYPE to override)"
	@echo "  make create-repos START=7 END=9                 - Create generation workspace repositories"
	@echo "  make create-repos START=1 END=3 PREFIX=test     - Create repos with custom prefix"
	@echo ""
	@echo "Workspace Management:"
	@echo "  make check-stuck-workspaces                     - Check for stuck workspaces (dry run)"
	@echo "  make fix-stuck-workspaces                       - Fix stuck workspaces automatically"
	@echo "  make check-workspaces-to-clean                  - Check available workspaces for stale data"
	@echo ""
	@echo "Ops:"
	@echo "  make ops-retry-run generation_id=est-abc123     - Retry a failed estimation (reads creds from .env)"
	@echo "  make ops-cancel-run generation_id=est-abc123    - Cancel a running estimation (reads creds from .env)"
	@echo "  make ops-retry-run generation_id=X BACKEND_URL=http://host:8000  - Override backend URL"
	@echo ""
	@echo "Database Modes:"
	@echo "  DEV mode:    Uses SQLite (no GCP credentials needed, single central db at ~/.specflow/db/specflow.db)"
	@echo "  Override:    DATABASE_TYPE=firestore to connect to an already-hosted GCP Firestore instance"
	@echo "               DATABASE_TYPE=emulator to connect to a manually-run Firestore emulator process"
	@echo ""
	@echo "SKIP_MODE:"
	@echo "  When enabled, agent_query returns immediately with 'SKIP_MODE' response"
	@echo "  Useful for testing workflows and MCP tools without triggering real agent execution"
	@echo "  Can also be set manually: export SKIP_AGENT_EXECUTION=true"
	@echo ""
	@echo "Isolated Workspace Model:"
	@echo "  Docker mounts:  /workspace1, /workspace2, /workspace3"
	@echo "  Each agent sees only its isolated workspace root"
	@echo "  Standards are copied at runtime to <workspace>/standards/"

# ============================================================
# Workspace Management
# ============================================================

.PHONY: check-stuck-workspaces
check-stuck-workspaces:
	@echo "🔍 Checking for stuck workspaces (dry run)..."
	cd backend && uv run python scripts/fix_stuck_workspaces.py --dry-run

.PHONY: fix-stuck-workspaces
fix-stuck-workspaces:
	@echo "🔧 Fixing stuck workspaces..."
	cd backend && uv run python scripts/fix_stuck_workspaces.py

.PHONY: check-workspaces-to-clean
check-workspaces-to-clean:
	@echo "🔍 Checking available workspaces for stale data..."
	@echo "⚠️  Ensure BACKEND_URL is set (default: http://localhost:8000)"
	cd backend && uv run python scripts/check-workspaces-to-clean.py

# ============================================================
# Ops: Estimation Management
# ============================================================

# Retry a failed estimation.
# Usage: make ops-retry-run generation_id=est-abc123
# Usage: make ops-retry-run generation_id=est-abc123 BACKEND_URL=http://prod:8000
ops-retry-run:
	@[ -n "$(generation_id)" ] || (echo "❌ Error: generation_id is required"; echo "Usage: make ops-retry-run generation_id=est-abc123"; exit 1)
	@API_KEY=$$(grep '^SPECFLOW_API_KEY=' .env | cut -d'=' -f2); \
	 EMAIL=$$(grep '^USER_EMAIL=' .env | cut -d'=' -f2); \
	 echo "🔄 Retrying generation $(generation_id) as $$EMAIL..."; \
	 BODY=$$(curl -s -X POST "$(BACKEND_URL)/api/v1/generation-sessions/$(generation_id)/retry" \
	   -H "X-API-Key: $$API_KEY" \
	   -H "X-User-Email: $$EMAIL" \
	   -H "Content-Type: application/json"); \
	 if [ -z "$$BODY" ]; then echo "(empty response — check BACKEND_URL and that the service is running)"; \
	 else echo "$$BODY" | python3 -m json.tool 2>/dev/null || echo "$$BODY"; \
	 fi

# Cancel a running estimation.
# Usage: make ops-cancel-run generation_id=est-abc123
# Usage: make ops-cancel-run generation_id=est-abc123 BACKEND_URL=http://prod:8000
ops-cancel-run:
	@[ -n "$(generation_id)" ] || (echo "❌ Error: generation_id is required"; echo "Usage: make ops-cancel-run generation_id=est-abc123"; exit 1)
	@API_KEY=$$(grep '^SPECFLOW_API_KEY=' .env | cut -d'=' -f2); \
	 EMAIL=$$(grep '^USER_EMAIL=' .env | cut -d'=' -f2); \
	 echo "🗑️  Cancelling generation $(generation_id) as $$EMAIL..."; \
	 BODY=$$(curl -s -X DELETE "$(BACKEND_URL)/api/v1/generation-sessions/$(generation_id)" \
	   -H "X-API-Key: $$API_KEY" \
	   -H "X-User-Email: $$EMAIL"); \
	 if [ -z "$$BODY" ]; then echo "(empty response — check BACKEND_URL and that the service is running)"; \
	 else echo "$$BODY" | python3 -m json.tool 2>/dev/null || echo "$$BODY"; \
	 fi

