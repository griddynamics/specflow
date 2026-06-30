#!/bin/sh
set -eu

DATA_DIR="${FIRESTORE_EMULATOR_DATA_DIR:-/firestore-data}"
CURRENT_EXPORT_DIR="${FIRESTORE_CURRENT_EXPORT_DIR:-${DATA_DIR}/current}"
PROJECT_ID="${GCP_PROJECT_ID:-local-dev}"
DATABASE_ID="${FIRESTORE_DATABASE_ID:-${FIRESTORE_DATABASE_NAME:-default}}"

if [ "${DATABASE_ID}" = "default" ]; then
  DATABASE_ID="(default)"
fi

export CLOUDSDK_CORE_PROJECT="${PROJECT_ID}"
export FIRESTORE_DATABASE_ID="${DATABASE_ID}"

export_metadata_file() {
  find "$1" -maxdepth 4 -type f -name "*overall_export_metadata" -print -quit 2>/dev/null
}

mkdir -p "${DATA_DIR}" "${CURRENT_EXPORT_DIR}"

IMPORT_ARGS=""
CURRENT_METADATA_FILE="$(export_metadata_file "${CURRENT_EXPORT_DIR}")"
LEGACY_METADATA_FILE="$(export_metadata_file "${DATA_DIR}")"
if [ -n "${CURRENT_METADATA_FILE}" ]; then
  echo "Restoring Firestore emulator data from ${CURRENT_METADATA_FILE}"
  IMPORT_ARGS="--import-data=${CURRENT_METADATA_FILE}"
elif [ -n "${LEGACY_METADATA_FILE}" ]; then
  echo "Restoring legacy Firestore emulator data from ${LEGACY_METADATA_FILE}"
  IMPORT_ARGS="--import-data=${LEGACY_METADATA_FILE}"
else
  echo "No Firestore emulator export found; starting empty"
fi

echo "Using Firestore emulator project ${PROJECT_ID} database ${DATABASE_ID}"

# shellcheck disable=SC2086
exec gcloud emulators firestore start \
  --project="${PROJECT_ID}" \
  --host-port=0.0.0.0:8080 \
  ${IMPORT_ARGS} \
  --export-on-exit="${CURRENT_EXPORT_DIR}"
