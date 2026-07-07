#!/usr/bin/env bash
# specflow-init.sh — noninteractive local quickstart bootstrap
#
# Usage:
#   ./specflow-init.sh [--max-parallel-runs K] [--dry-run]
#                      [--skip-build] [--provide-own-repos REPO_LIST] [--reset-local-db]
#   ./specflow-init.sh [--help | -h | -H]
#
# Responsibilities:
#   1. Load .env (user must have copied .env.quickstart.example → .env)
#   2. Generate TOKEN_ENCRYPTION_KEY if blank (via Fernet, never echoed)
#   3. Create .specflow-local/ and write init.log, mcp-config.json
#   4. Start the backend stack only (backend + sqlite; NOT mcp-server profile)
#   5. Health-gate: poll /health/ready before seeding
#   6. Provision repos + seed the workspace pool straight into the SQLite database, then
#      seed the API key + local identity via init_db.py
#   7. Write .specflow-local/mcp-config.json (keyless IDE MCP-client snippet)
#   8. Install the local SpecFlow CLI entry point
#   9. Print manual-install instruction for the user
#
# NFR-4: this script never echoes secrets to stdout, and its own log() calls redact
# token-shaped values. Subprocess output captured into init.log is routed through
# the same redaction path before it is written.

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_DIR="${SCRIPT_DIR}/.specflow-local"
LOG_FILE="${LOCAL_DIR}/init.log"
MCP_CONFIG_JSON="${LOCAL_DIR}/mcp-config.json"
BACKEND_HEALTH_URL="http://localhost:8000/health/ready"
HEALTH_RETRIES=60
HEALTH_INTERVAL=2  # seconds

# ---------------------------------------------------------------------------
# Defaults for flags
# ---------------------------------------------------------------------------
# --max-parallel-runs K: how many integral sets of 3 workspace repos to provision
# (= how many concurrent generations). K sets => K*3 repos.
MAX_PARALLEL_RUNS=3
DRY_RUN=false
SKIP_BUILD=false
OWN_REPOS=""  # comma-separated bare repo names (or org/repo) when --provide-own-repos is used
RESET_LOCAL_DB=false

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Noninteractive local quickstart bootstrap for SpecFlow.

Options:
  --max-parallel-runs K   Provision K integral sets of 3 workspace repos
                          (K concurrent generations; default: 1)
  --dry-run               Print planned actions without starting services or seeding
  --skip-build            Skip docker compose build (reuse existing images)
  --provide-own-repos LIST
                          Comma-separated bare repo names (or org/repo) instead of
                          creating new repos; requires at least K×3 entries
  --reset-local-db        Reset the local SQLite database before seeding
  -h, -H, --help          Show this help message and exit

Examples:
  $0
  $0 --max-parallel-runs 2 --dry-run
  $0 --provide-own-repos my-org/ws-1,my-org/ws-2,my-org/ws-3
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing (supports --flag value and --flag=value)
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --max-parallel-runs=*) MAX_PARALLEL_RUNS="${1#*=}"; shift ;;
        --max-parallel-runs)   MAX_PARALLEL_RUNS="$2"; shift 2 ;;
        --dry-run)             DRY_RUN=true; shift ;;
        --skip-build)              SKIP_BUILD=true; shift ;;
        --provide-own-repos=*)     OWN_REPOS="${1#*=}"; shift ;;
        --provide-own-repos)       OWN_REPOS="$2"; shift 2 ;;
        --reset-local-db)          RESET_LOCAL_DB=true; shift ;;
        -h|-H|--help)                usage; exit 0 ;;
        *)
            echo "ERROR: Unknown flag: $1" >&2
            echo "Run '$0 --help' for usage." >&2
            exit 1
            ;;
    esac
done

# Validate numeric flags and derive the provisioning range (K integral sets of 3).
if ! [[ "${MAX_PARALLEL_RUNS}" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: --max-parallel-runs must be a positive integer (got '${MAX_PARALLEL_RUNS}')." >&2
    exit 1
fi
if [[ -n "${OWN_REPOS}" ]]; then
    _OWN_REPO_COUNT=$(echo "${OWN_REPOS}" | tr ',' '\n' | grep -c '[^[:space:]]' || true)
    _REQUIRED=$(( MAX_PARALLEL_RUNS * 3 ))
    if [[ "${_OWN_REPO_COUNT}" -lt "${_REQUIRED}" ]]; then
        echo "ERROR: --provide-own-repos requires at least ${_REQUIRED} repos for --max-parallel-runs ${MAX_PARALLEL_RUNS} (${MAX_PARALLEL_RUNS}×3). Got ${_OWN_REPO_COUNT}." >&2
        exit 1
    fi
fi
REPO_RANGE_START=1
REPO_RANGE_END=$(( MAX_PARALLEL_RUNS * 3 ))

# ---------------------------------------------------------------------------
# Logging helpers (NFR-4: redact secrets in log file; never echo to stdout)
# ---------------------------------------------------------------------------
# Patterns that should be redacted in log output
_REDACT_PATTERNS=(
    "TOKEN_ENCRYPTION_KEY"
    "GITHUB_TOKEN"
    "P10Y_API_KEY"
    "OPENROUTER_API_KEY"
    "ANTHROPIC_API_KEY"
    "ROSETTA_API_KEY"
)

log() {
    local msg="$1"
    local redacted="$msg"
    for pattern in "${_REDACT_PATTERNS[@]}"; do
        # Redact lines that assign a value to any secret var: VAR=<value>
        redacted="$(echo "$redacted" | sed -E "s/(${pattern}=)[^ \t]*/\1[REDACTED]/g")"
        redacted="$(echo "$redacted" | sed -E "s/(\"${pattern}\"[[:space:]]*:[[:space:]]*\")[^\"]*/\1[REDACTED]/g")"
    done
    redacted="$(echo "$redacted" | sed -E "s/(input_value=)'[^']*'/\1'[REDACTED]'/g")"
    redacted="$(echo "$redacted" | sed -E "s/(input_value=\")[^\"]*\"/\1[REDACTED]\"/g")"
    # Defense-in-depth: scrub any leaked bearer token (e.g. a client error dump that
    # prints the Authorization header) — these do not match the VAR=value patterns above.
    redacted="$(echo "$redacted" | sed -E "s#(Bearer )[A-Za-z0-9._~+/=-]+#\1[REDACTED]#g")"
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] ${redacted}" >> "${LOG_FILE}"
}

log_stream() {
    local line
    while IFS= read -r line || [[ -n "${line}" ]]; do
        log "${line}"
    done
}

info() {
    echo "$1"
    log "INFO: $1"
}

warn() {
    echo "WARNING: $1" >&2
    log "WARN: $1"
}

error() {
    echo "ERROR: $1" >&2
    log "ERROR: $1"
    exit 1
}

clear_local_sqlite_db() {
    if [[ -z "${SPECFLOW_HOME_PATH:-}" ]]; then
        error "SPECFLOW_HOME_PATH is empty; refusing to clear the local database."
    fi
    if [[ "$(basename "${SPECFLOW_HOME_PATH}")" != ".specflow" ]]; then
        error "Refusing to clear unexpected SpecFlow home path: ${SPECFLOW_HOME_PATH}"
    fi

    info "Clearing local SQLite database at ${SPECFLOW_HOME_PATH}/db/specflow.db ..."
    log "INFO: rm -f <sqlite db + wal/shm sidecars>"
    rm -f "${SPECFLOW_HOME_PATH}/db/specflow.db" \
          "${SPECFLOW_HOME_PATH}/db/specflow.db-wal" \
          "${SPECFLOW_HOME_PATH}/db/specflow.db-shm"
}

set_env_value() {
    local key="$1"
    local value="$2"

    if grep -q "^${key}=" "${ENV_FILE}"; then
        TMPENV="$(mktemp)"
        while IFS= read -r line || [[ -n "${line}" ]]; do
            if [[ "${line}" == "${key}="* ]]; then
                printf '%s\n' "${key}=${value}"
            else
                printf '%s\n' "${line}"
            fi
        done < "${ENV_FILE}" > "${TMPENV}"
        mv "${TMPENV}" "${ENV_FILE}"
    else
        printf '\n%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
    fi

    export "${key}=${value}"
}

get_git_config_value() {
    local key="$1"
    git config get "${key}" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Step 0: Create .specflow-local/ early so the log file can be written
# ---------------------------------------------------------------------------
mkdir -p "${LOCAL_DIR}"
# Touch the log file now (subsequent calls append)
: >> "${LOG_FILE}"

info "specflow-init.sh starting (dry-run=${DRY_RUN}, max-parallel-runs=${MAX_PARALLEL_RUNS} [=${REPO_RANGE_END} repos], provide-own-repos=${OWN_REPOS:-none}, skip-build=${SKIP_BUILD}, reset-local-db=${RESET_LOCAL_DB})"

# ---------------------------------------------------------------------------
# Step 1: Load .env
# ---------------------------------------------------------------------------
ENV_FILE="${SCRIPT_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
    error ".env file not found at ${ENV_FILE}. Copy .env.quickstart.example → .env and fill in required values."
fi

# Export variables from .env, skipping comments and blank lines
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

log "INFO: Loaded .env from ${ENV_FILE}"

_WORKSPACE_MOUNT_PATH="${WORKSPACE_MOUNT_PATH:-./workspaces}"
export SPECFLOW_HOME_PATH="${SPECFLOW_HOME_MOUNT_PATH:-${HOME}/.specflow}"
export FIRESTORE_DATABASE_NAME="${FIRESTORE_DATABASE_NAME:-specflow}"

# Host-side DB target for the provisioning + seeding subshells (uv run on the host). These
# are plain (non-exported) vars so `docker compose up` still gives the CONTAINER its own
# SQLITE_DB_PATH from .env — host writes must go to the bind-mount SOURCE
# (${SPECFLOW_HOME_PATH}/db/specflow.db), never the container-internal /root/.specflow path.
_DATABASE_TYPE="${DATABASE_TYPE:-sqlite}"
_SQLITE_DB_PATH="${SPECFLOW_HOME_PATH}/db/specflow.db"

log "INFO: SpecFlow home (central SQLite db) dir: ${SPECFLOW_HOME_PATH}"
log "INFO: Firestore database name (hosted-connect mode only): ${FIRESTORE_DATABASE_NAME}"

# ---------------------------------------------------------------------------
# Step 1c: Derive local git identity
# ---------------------------------------------------------------------------
if [[ -z "${GIT_USER_NAME:-}" ]]; then
    # The repo owner is always the authenticated GitHub account, resolved from
    # its login (GET /user). `git config user.name` is only a display name and
    # not necessarily a valid GitHub namespace, so it is NOT used; if the API
    # cannot resolve the login, the user must set GIT_USER_NAME in .env.
    # The API/uv call is skipped in dry-run (no network/venv work), matching the
    # P10Y and key-generation steps.
    if [[ "${DRY_RUN}" == "true" ]]; then
        info "[DRY RUN] Would resolve GIT_USER_NAME from the GitHub API login (GET /user) and persist it to .env"
    else
        if [[ -z "${GITHUB_TOKEN:-${GITHUB_TOKEN_DEFAULT:-}}" ]]; then
            error "GIT_USER_NAME is blank and GITHUB_TOKEN is not set, so your GitHub login cannot be resolved. Set GITHUB_TOKEN (or set GIT_USER_NAME) in .env."
        fi
        _GIT_USER_NAME="$(cd "${SCRIPT_DIR}/backend" && uv run python -m scripts.github_resolve_login 2>/dev/null || true)"
        if [[ -z "${_GIT_USER_NAME}" ]]; then
            error "Could not resolve your GitHub login from GITHUB_TOKEN (GET /user). Verify the token is valid, or set GIT_USER_NAME in .env."
        fi
        set_env_value "GIT_USER_NAME" "${_GIT_USER_NAME}"
        info "Resolved GitHub login '${_GIT_USER_NAME}' (GET /user) and wrote GIT_USER_NAME to .env."
        log "INFO: GIT_USER_NAME resolved from GitHub API login"
    fi
fi

if [[ -z "${GIT_USER_EMAIL:-}" ]]; then
    _GIT_USER_EMAIL="$(get_git_config_value "user.email")"
    if [[ -n "${_GIT_USER_EMAIL}" ]]; then
        if [[ "${DRY_RUN}" == "true" ]]; then
            export GIT_USER_EMAIL="${_GIT_USER_EMAIL}"
            info "[DRY RUN] Detected git user.email '${_GIT_USER_EMAIL}' - would persist it as GIT_USER_EMAIL in .env"
        else
            set_env_value "GIT_USER_EMAIL" "${_GIT_USER_EMAIL}"
            info "Detected git user.email '${_GIT_USER_EMAIL}' and wrote GIT_USER_EMAIL to .env."
            log "INFO: GIT_USER_EMAIL resolved from git config user.email"
        fi
    else
        error "GIT_USER_EMAIL is blank and 'git config get user.email' returned nothing. Set GIT_USER_EMAIL in .env."
    fi
fi

if [[ -z "${USER_EMAIL:-}" && -n "${GIT_USER_EMAIL:-}" ]]; then
    if [[ "${DRY_RUN}" == "true" ]]; then
        export USER_EMAIL="${GIT_USER_EMAIL}"
        info "[DRY RUN] USER_EMAIL is blank - would reuse GIT_USER_EMAIL for local identity and MCP config"
    else
        set_env_value "USER_EMAIL" "${GIT_USER_EMAIL}"
        info "USER_EMAIL was blank, so it now uses GIT_USER_EMAIL for local identity and MCP config."
        log "INFO: USER_EMAIL populated from GIT_USER_EMAIL"
    fi
fi

# ---------------------------------------------------------------------------
# Step 1d: Fail fast on P10Y access
# ---------------------------------------------------------------------------
# The P10Y organisation id is bound to the API key and resolved at runtime via
# Compass GET /api/user/self. Verifying it here — before the Docker build and the
# provisioning run — turns a late, generic failure into an immediate, actionable one.
if [[ "${DRY_RUN}" == "true" ]]; then
    info "[DRY RUN] Would verify P10Y access by resolving the organisation id via /api/user/self"
else
    if [[ -z "${P10Y_API_KEY:-}" ]]; then
        error "P10Y_API_KEY is not set in .env. Add your Compass API token (Compass → Settings → API Tokens)."
    fi
    info "Verifying P10Y access (resolving organisation id from the API key) ..."
    # The client's HTTP error path can print the Authorization header, so suppress its
    # stderr here and surface only a clean message plus the (non-secret) organisation id.
    if _P10Y_ORG_ID="$(cd "${SCRIPT_DIR}/backend" && uv run python -m scripts.p10y_resolve_organisation_id 2>/dev/null)"; then
        info "P10Y access verified — organisation id ${_P10Y_ORG_ID}."
        log "INFO: P10Y organisation id resolved: ${_P10Y_ORG_ID}"
        # Persist to .env so Settings reads it as a normal variable at runtime (prod
        # provides it via deployment env). Refreshed every run, so a rotated API key
        # cannot leave a stale organisation id behind.
        set_env_value "P10Y_ORGANISATION_ID" "${_P10Y_ORG_ID}"
        info "P10Y organisation id written to .env."
    else
        error "Could not obtain a P10Y organisation id with the provided P10Y_API_KEY. Verify the token is valid and active (Compass → Settings → API Tokens)."
    fi
fi

# ---------------------------------------------------------------------------
# Step 2: Generate TOKEN_ENCRYPTION_KEY if blank (NFR-4: never echo the key)
# ---------------------------------------------------------------------------
if [[ -z "${TOKEN_ENCRYPTION_KEY:-}" ]]; then
    if [[ "${DRY_RUN}" == "true" ]]; then
        info "[DRY RUN] TOKEN_ENCRYPTION_KEY is blank — would generate Fernet key and persist to .env"
    else
        info "TOKEN_ENCRYPTION_KEY is blank — generating a Fernet key and persisting to .env ..."
        # Generate via the backend venv where cryptography is available
        GENERATED_KEY="$(cd "${SCRIPT_DIR}/backend" && uv run python -c \
            'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
        set_env_value "TOKEN_ENCRYPTION_KEY" "${GENERATED_KEY}"
        # DO NOT print GENERATED_KEY to stdout
        info "TOKEN_ENCRYPTION_KEY generated and persisted to .env (value not shown)."
        log "INFO: TOKEN_ENCRYPTION_KEY=<generated and persisted to .env>"
        unset GENERATED_KEY
    fi
fi

# ---------------------------------------------------------------------------
# Step 3: Start the backend stack (backend + sqlite; NOT mcp-server profile)
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" == "true" ]]; then
    info "[DRY RUN] Would start backend stack: docker compose up -d (backend)"
    info "[DRY RUN] Would skip mcp-server profile (IDE-side only)."
    if [[ "${RESET_LOCAL_DB}" == "true" ]]; then
        info "[DRY RUN] Would clear the local SQLite database at ${SPECFLOW_HOME_PATH}/db/specflow.db"
    fi
else
    BUILD_FLAG=""
    if [[ "${SKIP_BUILD}" == "false" ]]; then
        info "Building images ..."
        log "INFO: Running docker compose build"
        docker compose -f "${SCRIPT_DIR}/docker-compose.yml" build > >(log_stream) 2> >(log_stream) \
            || error "docker compose build failed. Check ${LOG_FILE} for details."
    fi

    # Determine reset behaviour
    if [[ "${RESET_LOCAL_DB}" == "true" ]]; then
        info "Resetting local state (--reset-local-db): stopping and removing containers+volumes ..."
        log "INFO: docker compose down -v"
        docker compose -f "${SCRIPT_DIR}/docker-compose.yml" down -v > >(log_stream) 2> >(log_stream) || true
        clear_local_sqlite_db
    fi

    info "Starting backend (sqlite; no mcp-server profile) ..."
    log "INFO: docker compose up -d"
    docker compose -f "${SCRIPT_DIR}/docker-compose.yml" up -d > >(log_stream) 2> >(log_stream) \
        || error "docker compose up failed. Check ${LOG_FILE} for details."
fi

# ---------------------------------------------------------------------------
# Step 4: Health-gated readiness wait
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" == "true" ]]; then
    info "[DRY RUN] Would poll ${BACKEND_HEALTH_URL} (${HEALTH_RETRIES} retries × ${HEALTH_INTERVAL}s)"
else
    info "Waiting for backend to be ready at ${BACKEND_HEALTH_URL} ..."
    log "INFO: Polling ${BACKEND_HEALTH_URL} (max ${HEALTH_RETRIES} retries, ${HEALTH_INTERVAL}s interval)"
    _attempt=0
    _ready=false
    while [[ $_attempt -lt ${HEALTH_RETRIES} ]]; do
        _attempt=$(( _attempt + 1 ))
        if curl -sf --max-time 3 "${BACKEND_HEALTH_URL}" > /dev/null 2>&1; then
            _ready=true
            break
        fi
        sleep "${HEALTH_INTERVAL}"
    done
    if [[ "${_ready}" != "true" ]]; then
        error "Backend did not become healthy within $(( HEALTH_RETRIES * HEALTH_INTERVAL )) seconds. Check 'docker compose logs' for details."
    fi
    info "Backend is ready."
    log "INFO: Backend health check passed after ${_attempt} attempt(s)"
fi

# ---------------------------------------------------------------------------
# Step 5: Produce durable workspace config
# ---------------------------------------------------------------------------
# Resolve the GitHub org once and pass it explicitly to provisioning. It is required to
# disambiguate P10Y repository IDs: P10Y `repository_name` is the bare name and is NOT
# unique across orgs, so the lookup matches on `git_url` (`<org>/<name>`). All workspace
# repos must live in this single org.
# GITHUB_ORG is optional: when unset, the user owns the workspace repos under their own
# personal account, so the org is their GitHub username (GIT_USER_NAME).
_GH_ORG="${GITHUB_ORG:-${GITHUB_ORG_DEFAULT:-${GIT_USER_NAME:-}}}"
if [[ -z "${_GH_ORG}" ]]; then
    if [[ "${DRY_RUN}" == "true" ]]; then
        # In a real run GIT_USER_NAME is resolved from the GitHub API in Step 1c;
        # dry-run skips that network call, so use a clearly-marked placeholder.
        _GH_ORG="<github-login>"
        info "[DRY RUN] GITHUB_ORG and GIT_USER_NAME are unset; the real run resolves GIT_USER_NAME from the GitHub API (GET /user). Using placeholder '${_GH_ORG}' for the preview."
    else
        error "GITHUB_ORG is unset and GIT_USER_NAME could not be resolved. Set GITHUB_ORG to the org that owns the workspace repos, or set GIT_USER_NAME in .env to use your personal account."
    fi
fi

if [[ -n "${OWN_REPOS}" ]]; then
    # User supplied their own repos. Entries are bare names — the owner is the resolved
    # _GH_ORG (GITHUB_ORG, else the user's account), so `org/name` is auto-resolved from it.
    # An explicit `org/` prefix is still accepted for backward compatibility but must match
    # _GH_ORG (the org is needed to resolve P10Y IDs, which match on `<org>/<name>`); it is
    # then stripped. Repo creation is skipped; repos must already exist on GitHub and be
    # discoverable in P10Y/Compass.
    while IFS= read -r _entry; do
        [[ -z "${_entry}" ]] && continue
        if [[ "${_entry}" == */* && "${_entry%/*}" != "${_GH_ORG}" ]]; then
            error "--provide-own-repos entry '${_entry}' is in org '${_entry%/*}', but repos are owned by '${_GH_ORG}'. Provide bare repo names, or prefix them with '${_GH_ORG}/'."
        fi
    done < <(printf '%s\n' "${OWN_REPOS}" | tr ',' '\n')
    # Strip any org/ prefix to bare names; `paste -sd ',' -` (explicit stdin) joins them
    # back portably — BSD/macOS paste errors without the trailing '-'.
    _OWN_REPO_NAMES=$(echo "${OWN_REPOS}" | tr ',' '\n' | sed 's|.*/||' | paste -sd ',' -)
    if [[ "${DRY_RUN}" == "true" ]]; then
        info "[DRY RUN] --provide-own-repos: would run create_generation_session_repos.py --repos ${_OWN_REPO_NAMES} --github-org ${_GH_ORG} --skip-metrics (seeds the ${_DATABASE_TYPE} workspace pool directly)"
    else
        info "Looking up P10Y IDs for provided repos and seeding the workspace pool ..."
        log "INFO: Running create_generation_session_repos.py --repos ${_OWN_REPO_NAMES} --github-org ${_GH_ORG} --skip-metrics (DATABASE_TYPE=${_DATABASE_TYPE})"
        (
            cd "${SCRIPT_DIR}/backend"
            DATABASE_TYPE="${_DATABASE_TYPE}" \
            SQLITE_DB_PATH="${_SQLITE_DB_PATH}" \
            FIRESTORE_EMULATOR_HOST="${FIRESTORE_EMULATOR_HOST:-localhost:8080}" \
            uv run python scripts/create_generation_session_repos.py \
                --repos "${_OWN_REPO_NAMES}" \
                --github-org "${_GH_ORG}" \
                --skip-metrics
        ) > >(log_stream) 2> >(log_stream) \
            || error "Failed to look up P10Y IDs / seed provided repos. Check ${LOG_FILE}. Ensure the repos exist in GitHub and are synced in Compass."
        info "Workspace pool seeded from provided repos."
    fi
else
    _REPO_PREFIX="${WORKSPACE_REPO_PREFIX:-specflow-workspace}"
    if [[ "${DRY_RUN}" == "true" ]]; then
        info "[DRY RUN] Would run create_generation_session_repos.py --start ${REPO_RANGE_START} --end ${REPO_RANGE_END} --prefix ${_REPO_PREFIX} --github-org ${_GH_ORG} (seeds the ${_DATABASE_TYPE} workspace pool directly; ${MAX_PARALLEL_RUNS} set(s) of 3 = ${REPO_RANGE_END} repos)"
    else
        info "Ensuring ${MAX_PARALLEL_RUNS} set(s) of 3 workspace repos (${REPO_RANGE_END} total) and P10Y metrics ..."
        log "INFO: Running create_generation_session_repos.py --start ${REPO_RANGE_START} --end ${REPO_RANGE_END} --prefix ${_REPO_PREFIX} --github-org ${_GH_ORG} (DATABASE_TYPE=${_DATABASE_TYPE})"
        # H (fail-loud): a provisioning failure is a hard error — never fall through to an
        # empty pool reported as success.
        (
            cd "${SCRIPT_DIR}/backend"
            DATABASE_TYPE="${_DATABASE_TYPE}" \
            SQLITE_DB_PATH="${_SQLITE_DB_PATH}" \
            FIRESTORE_EMULATOR_HOST="${FIRESTORE_EMULATOR_HOST:-localhost:8080}" \
            uv run python scripts/create_generation_session_repos.py \
                --start "${REPO_RANGE_START}" \
                --end "${REPO_RANGE_END}" \
                --prefix "${_REPO_PREFIX}" \
                --github-org "${_GH_ORG}"
        ) > >(log_stream) 2> >(log_stream) \
            || error "Workspace provisioning failed (create_generation_session_repos.py). Check ${LOG_FILE}."
        info "Workspace pool seeded (${REPO_RANGE_END} repos)."
    fi
fi

# ---------------------------------------------------------------------------
# Step 6: Seed the bootstrap API key + local-auth identity sentinel.
# The workspace pool was already seeded straight into the database by the provisioning step
# above (no flat-file handoff), so init_db.py runs without a workspace-config file here.
# ---------------------------------------------------------------------------

# Array form (not a string) so paths containing spaces stay a single argument.
_SEED_FLAGS=(--yes)
if [[ "${RESET_LOCAL_DB}" == "true" ]]; then
    _SEED_FLAGS+=(--replace)
fi

if [[ "${DRY_RUN}" == "true" ]]; then
    info "[DRY RUN] Would run: DATABASE_TYPE=${_DATABASE_TYPE} uv run scripts/init_db.py --dry-run ${_SEED_FLAGS[*]}"
    log "INFO: [DRY RUN] init_db.py --dry-run ${_SEED_FLAGS[*]}"
else
    info "Seeding API key + local identity into the ${_DATABASE_TYPE} database ..."
    log "INFO: Running init_db.py ${_SEED_FLAGS[*]} (DATABASE_TYPE=${_DATABASE_TYPE})"
    (
        cd "${SCRIPT_DIR}/backend"
        DATABASE_TYPE="${_DATABASE_TYPE}" \
        SQLITE_DB_PATH="${_SQLITE_DB_PATH}" \
        FIRESTORE_EMULATOR_HOST="${FIRESTORE_EMULATOR_HOST:-localhost:8080}" \
            uv run scripts/init_db.py "${_SEED_FLAGS[@]}"
    ) > >(log_stream) 2> >(log_stream) \
        || error "init_db.py failed. Check ${LOG_FILE} for details."
    info "Database seeded successfully."
fi

# ---------------------------------------------------------------------------
# Step 7: Write keyless MCP-client install snippet
# ---------------------------------------------------------------------------
# USER_EMAIL is derived from git config user.email during quickstart when blank.
_MCP_USER_EMAIL="${USER_EMAIL:-}"

# MCP launch form — uses uvx --from <local-path> so the installed entry point is used.
_MCP_SERVER_PATH="$(cd "${SCRIPT_DIR}/mcp_server" && pwd)"

if [[ "${DRY_RUN}" == "true" ]]; then
    info "[DRY RUN] Would write keyless MCP config to ${MCP_CONFIG_JSON}"
    log "INFO: [DRY RUN] Would write mcp-config.json (USER_EMAIL=${_MCP_USER_EMAIL})"
else
    # Build the JSON snippet — no SPECFLOW_API_KEY (D4/FR-19)
    cat > "${MCP_CONFIG_JSON}" <<EOF
{
  "mcpServers": {
    "specflow": {
      "command": "uvx",
      "args": ["--refresh", "--no-cache", "--from", "${_MCP_SERVER_PATH}", "specflow-mcp"],
      "env": {
        "USER_EMAIL": "${_MCP_USER_EMAIL}",
        "WORKSPACE_COUNT": "3"
      }
    }
  }
}
EOF

    info "Wrote keyless MCP config to ${MCP_CONFIG_JSON}"
    log "INFO: Wrote mcp-config.json (USER_EMAIL=${_MCP_USER_EMAIL})"
fi

# ---------------------------------------------------------------------------
# Step 8: Install the local CLI entry point
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" == "true" ]]; then
    info "[DRY RUN] Would install local CLI: uv tool install --force --editable ${_MCP_SERVER_PATH}"
else
    info "Installing local SpecFlow CLI as 'specflow' ..."
    log "INFO: Running uv tool install --force --editable <mcp_server>"
    uv tool install --force --editable "${_MCP_SERVER_PATH}" > >(log_stream) 2> >(log_stream) \
        || error "Failed to install local SpecFlow CLI. Check ${LOG_FILE} for details."
    info "Local CLI installed. You can now run: specflow sessions"
fi

# ---------------------------------------------------------------------------
# Step 9: Final instructions — MANUAL step for the user
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" == "true" ]]; then
    cat <<'INSTRUCTIONS'

========================================================================
  specflow-init.sh dry run complete
========================================================================

No Docker services were started, no repositories were provisioned, and no MCP
config was written. Re-run without --dry-run to apply these steps.

INSTRUCTIONS
else
    cat <<'INSTRUCTIONS'

========================================================================
  specflow-init.sh complete
========================================================================

Next step (manual): add the MCP server to your IDE.

  Cursor:
    Open Settings → MCP and paste the contents of
    .specflow-local/mcp-config.json
    (or merge into ~/.cursor/mcp.json / .cursor/mcp.json).

  Claude Desktop:
    Open Settings → Developer → Edit Config and merge the
    "mcpServers" block from .specflow-local/mcp-config.json
    into ~/Library/Application Support/Claude/claude_desktop_config.json

The MCP server runs as an IDE-side process — do NOT start it via
Docker Compose. The specflow backend is already running in Docker.

Tip: for a live, glanceable view of a run from the terminal, launch the
interactive TUI from your project directory:

    specflow tui

INSTRUCTIONS
fi

log "INFO: specflow-init.sh completed successfully"
