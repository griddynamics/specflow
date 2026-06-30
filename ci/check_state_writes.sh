#!/usr/bin/env bash
# ci/check_state_writes.sh
#
# Enforcement: no file outside backend/app/state/ may write status directly
# using EstimationStatus or WorkspaceStatus enum values OR bare string literals.
# Also enforces that StateMachineDBAdapter (_esm._db / _db) is only accessed
# from within backend/app/state/.
#
# NOTE: "progress" field writes are explicitly exempted — the workflow layer
# owns the progress display map. Only status/checkpoint writes are guarded here.
#
# Four grep passes:
#
#   Pass 1 — inline enum status write:
#     db.update("estimations", id, {"status": EstimationStatus.RUNNING.value})
#
#   Pass 2 — multi-line enum status write:
#     db.update("estimations", id, {
#         "status": EstimationStatus.RUNNING.value,
#     })
#
#   Pass 3 — bare string-literal status write:
#     db.update("workspaces", id, {"status": "available"})
#     {"status": "cleaning"}
#
#   Pass 4 — direct adapter access outside state/:
#     self._esm._db.get_estimation(...)   ← state machine internals leaking out
#     self._esm._db.update_estimation(...)
#
# Usage:
#   bash ci/check_state_writes.sh          # exits 1 if violations found
#   bash ci/check_state_writes.sh --report # exits 0, just prints findings

set -euo pipefail

REPORT_ONLY=0
if [[ "${1:-}" == "--report" ]]; then
    REPORT_ONLY=1
fi

# Pass 1: inline dict — status key and enum value on the same line as update()
PASS1=$(grep -rn \
    --include="*.py" \
    'update.*"status".*EstimationStatus\|update.*"status".*WorkspaceStatus' \
    backend/app/ \
    | grep -v '/state/' \
    || true)

# Pass 2: multi-line dict — "status": EnumName on its own line outside state/
PASS2=$(grep -rn \
    --include="*.py" \
    '"status": EstimationStatus\|"status": WorkspaceStatus' \
    backend/app/ \
    | grep -v '/state/' \
    || true)

# Pass 3: string-literal status values (catches bare "available", "cleaning", etc.)
# Exclusions:
#   /state/       — authoritative writer, excluded like passes 1+2
#   /database/    — abstract interface; only docstring examples, never writes status
#   # state-ok   — inline marker for legitimate non-Firestore uses (e.g. batch result dicts)
PASS3=$(grep -rn \
    --include="*.py" \
    '"status": "available"\|"status": "cleaning"\|"status": "stuck"\|"status": "allocated"\|"status": "pending"\|"status": "running"\|"status": "completed"\|"status": "failed"\|"status": "initializing"' \
    backend/app/ \
    | grep -v '/state/' \
    | grep -v '/database/' \
    | grep -v '# state-ok' \
    || true)

# Pass 4: direct StateMachineDBAdapter access outside state/
# Catches: self._esm._db.get_estimation(...) / self._esm._db.update_estimation(...)
# The adapter is an internal implementation detail of the state machine layer.
PASS4=$(grep -rn \
    --include="*.py" \
    '_esm\._db\.' \
    backend/app/ \
    | grep -v '/state/' \
    || true)

# Pass 5: COL_API_KEYS (api_keys txn writes for generation sessions) only in state/
PASS5=$(grep -rn \
    --include="*.py" \
    'COL_API_KEYS' \
    backend/app/ \
    | grep -v '/state/' \
    || true)

# Pass 6: string-literal field names for generation-session fields outside state/
# Catches: db.update({"active_generation_sessions": ...}) or {"max_concurrent_sessions": ...}
# written via string literals instead of the COL_API_KEYS constant path.
# Use '# state-ok' to mark deliberate exceptions (e.g. initial key creation in auth.py).
PASS6=$(grep -rn \
    --include="*.py" \
    '"active_generation_sessions"\|"max_concurrent_sessions"' \
    backend/app/ \
    | grep -v '/state/' \
    | grep -v '# state-ok' \
    || true)

MATCHES="${PASS1}${PASS2}${PASS3}${PASS4}${PASS5}${PASS6}"

if [[ -n "$MATCHES" ]]; then
    echo "ERROR: State layer violations found outside backend/app/state/:"
    echo "$MATCHES"
    if [[ $REPORT_ONLY -eq 0 ]]; then
        exit 1
    fi
else
    echo "OK: No rogue state writes or adapter leaks found outside backend/app/state/"
fi
