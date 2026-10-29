#!/usr/bin/env bash
# SpecFlow database bootstrap. The official postgres image runs this ONCE, on an empty
# data volume only (/docker-entrypoint-initdb.d), as the admin POSTGRES_USER. It creates:
#   1. the application role DATABASE_USER: LOGIN, not a superuser, no CREATEROLE;
#   2. the database DATABASE_NAME, owned by that role, so the API's `specflow migrate`
#      can create the tables, enable row-level security and grant columns;
#   3. the row-level-security agent role DATABASE_AGENT_USER (C21): LOGIN, NOBYPASSRLS,
#      not a superuser, CONNECTION LIMIT 20, statement_timeout 30s,
#      idle_in_transaction_session_timeout 60s.
# The local stack mounts this folder (backend/postgres-init) into the official postgres:17.11
# image; any PostgreSQL deployment can run the same script on its first start.
# It never runs again: a later password or role change is manual SQL.
#
# Passwords never reach argv or output: psql reads them with \getenv and quotes them
# with format('%L'); errors are terse. Only role and database names are printed.
set -euo pipefail

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

main() {
  local missing=()
  local name
  for name in POSTGRES_USER DATABASE_NAME DATABASE_USER DATABASE_PASSWORD \
    DATABASE_AGENT_USER DATABASE_AGENT_PASSWORD; do
    [[ -n "${!name:-}" ]] || missing+=("$name")
  done
  if ((${#missing[@]})); then
    echo "10-specflow-roles: missing or empty: ${missing[*]}" >&2
    return 1
  fi
  # The admin is a superuser; the app and agent roles must be other roles.
  if [[ "$(lower "$DATABASE_USER")" == "$(lower "$POSTGRES_USER")" ]]; then
    echo "10-specflow-roles: DATABASE_USER must differ from POSTGRES_USER (the admin)" >&2
    return 1
  fi
  if [[ "$(lower "$DATABASE_AGENT_USER")" == "$(lower "$DATABASE_USER")" \
    || "$(lower "$DATABASE_AGENT_USER")" == "$(lower "$POSTGRES_USER")" ]]; then
    echo "10-specflow-roles: DATABASE_AGENT_USER must differ from DATABASE_USER and POSTGRES_USER" >&2
    return 1
  fi

  psql --no-psqlrc --quiet --set ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" --dbname "${POSTGRES_DB:-$POSTGRES_USER}" \
    --set app_user="$DATABASE_USER" --set db_name="$DATABASE_NAME" \
    --set agent_user="$DATABASE_AGENT_USER" <<'SQL'
\set VERBOSITY terse
\set SHOW_CONTEXT never
\getenv app_password DATABASE_PASSWORD
\getenv agent_password DATABASE_AGENT_PASSWORD
SELECT format('CREATE ROLE %I', :'app_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') \gexec
SELECT format(
  'ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT PASSWORD %L',
  :'app_user', :'app_password') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'db_name', :'app_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db_name') \gexec
-- Also when POSTGRES_DB named the same database: the owner gets schema public too.
SELECT format('ALTER DATABASE %I OWNER TO %I', :'db_name', :'app_user') \gexec
SELECT format('CREATE ROLE %I', :'agent_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'agent_user') \gexec
SELECT format(
  'ALTER ROLE %I LOGIN NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT CONNECTION LIMIT 20 PASSWORD %L',
  :'agent_user', :'agent_password') \gexec
SELECT format('ALTER ROLE %I SET statement_timeout = %L', :'agent_user', '30s') \gexec
SELECT format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', :'agent_user', '60s') \gexec
\unset app_password
\unset agent_password
SQL
  echo "10-specflow-roles: role ${DATABASE_USER} owns database ${DATABASE_NAME}; agent role ${DATABASE_AGENT_USER} ready"
}

main "$@"
