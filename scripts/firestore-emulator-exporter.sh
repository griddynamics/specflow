#!/bin/sh
set -eu

DATA_DIR="${FIRESTORE_EMULATOR_DATA_DIR:-/firestore-data}"
CURRENT_EXPORT_DIR="${FIRESTORE_CURRENT_EXPORT_DIR:-${DATA_DIR}/current}"
NEXT_EXPORT_DIR="${FIRESTORE_NEXT_EXPORT_DIR:-${DATA_DIR}/next}"
PREVIOUS_EXPORT_DIR="${FIRESTORE_PREVIOUS_EXPORT_DIR:-${DATA_DIR}/previous}"
EMULATOR_HOST="${FIRESTORE_EMULATOR_INTERNAL_HOST:-firestore-emulator:8080}"
PROJECT_ID="${GCP_PROJECT_ID:-local-dev}"
DATABASE_ID="${FIRESTORE_DATABASE_ID:-${FIRESTORE_DATABASE_NAME:-default}}"
EXPORT_INTERVAL_SECONDS="${FIRESTORE_EXPORT_INTERVAL_SECONDS:-60}"
EXPORT_TIMEOUT_SECONDS="${FIRESTORE_EXPORT_TIMEOUT_SECONDS:-120}"

if [ "${DATABASE_ID}" = "default" ]; then
  DATABASE_ID="(default)"
fi

DATABASE_PATH="projects/${PROJECT_ID}/databases/${DATABASE_ID}"
EXPORT_ENDPOINT="http://${EMULATOR_HOST}/emulator/v1/projects/${PROJECT_ID}:export"

log() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

has_export_metadata() {
  find "$1" -maxdepth 4 -name "*overall_export_metadata" -print -quit 2>/dev/null | grep -q .
}

wait_for_emulator() {
  until curl -sf --max-time 2 "http://${EMULATOR_HOST}" >/dev/null; do
    log "Waiting for Firestore emulator at ${EMULATOR_HOST} ..."
    sleep 2
  done
}

export_once() {
  log "Exporting Firestore emulator database ${DATABASE_PATH}"
  rm -rf "${NEXT_EXPORT_DIR}"
  mkdir -p "${NEXT_EXPORT_DIR}"

  payload=$(printf '{"database":"%s","export_directory":"%s"}' "${DATABASE_PATH}" "${NEXT_EXPORT_DIR}")
  curl -sf --max-time "${EXPORT_TIMEOUT_SECONDS}" \
    -X POST "${EXPORT_ENDPOINT}" \
    -H "Content-Type: application/json" \
    -d "${payload}" >/tmp/firestore-export-response.json

  if ! has_export_metadata "${NEXT_EXPORT_DIR}"; then
    log "Export API returned successfully but no export metadata was written"
    return 1
  fi

  rm -rf "${PREVIOUS_EXPORT_DIR}"
  if [ -e "${CURRENT_EXPORT_DIR}" ]; then
    mv "${CURRENT_EXPORT_DIR}" "${PREVIOUS_EXPORT_DIR}"
  fi
  mv "${NEXT_EXPORT_DIR}" "${CURRENT_EXPORT_DIR}"
  log "Firestore emulator export persisted to ${CURRENT_EXPORT_DIR}"
}

shutdown() {
  log "Stop signal received; attempting final Firestore emulator export"
  if ! export_once; then
    log "Final Firestore emulator export failed; keeping previous snapshot"
  fi
  exit 0
}

trap shutdown TERM INT

mkdir -p "${DATA_DIR}"
wait_for_emulator

if ! export_once; then
  log "Initial Firestore emulator export failed; will retry on interval"
fi

while :; do
  sleep "${EXPORT_INTERVAL_SECONDS}" &
  wait "$!" || true
  if ! export_once; then
    log "Periodic Firestore emulator export failed; will retry"
  fi
done
