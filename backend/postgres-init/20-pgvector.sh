#!/usr/bin/env bash
# Local only: the pgvector extension for the PostgreSQL graph store (GRAPH_STORE=postgres).
# The official image runs this once, on an empty data volume only (/docker-entrypoint-initdb.d), after
# 10-specflow-roles.sh, as the admin POSTGRES_USER: it creates the extension `vector` in the database
# DATABASE_NAME. The extension needs an administrator, so the app role cannot do it (the graph store only
# checks for it); a managed deployment runs `CREATE EXTENSION vector;` once per database instead.
# When the server does not ship pgvector the script says so and does nothing, so a plain PostgreSQL image
# still starts (the graph store then answers store_extension_missing). A data volume that already exists
# is not touched: `make postgres-vector` creates the extension there.
set -euo pipefail

main() {
  local name
  for name in POSTGRES_USER DATABASE_NAME; do
    [[ -n "${!name:-}" ]] || {
      echo "20-pgvector: missing or empty: ${name}" >&2
      return 1
    }
  done
  local available
  available="$(psql --no-psqlrc --quiet --tuples-only --no-align --set ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" --dbname "${POSTGRES_DB:-$POSTGRES_USER}" \
    --command "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")"
  if [[ -z "$available" ]]; then
    echo "20-pgvector: this server has no pgvector; the PostgreSQL graph store needs it" >&2
    return 0
  fi
  psql --no-psqlrc --quiet --set ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" --dbname "$DATABASE_NAME" \
    --command "CREATE EXTENSION IF NOT EXISTS vector"
  echo "20-pgvector: extension vector ready in database ${DATABASE_NAME}"
}

main "$@"
