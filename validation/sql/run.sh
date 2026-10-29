#!/usr/bin/env bash
# Runs every configured SQL validation over one project with the kit's own environment.
# Usage: run.sh [PROJECT_DIR]   (default: the current directory)
# PROJECT_DIR/sqlcheck.toml is read when it exists (see sqlcheck.example.toml). The
# dynamic steps (schema-parity, migrations, sqlite-pragmas) import the project's code: run
# them from the project's own environment instead (`sqlcheck run` after installing this
# kit there), or list only the static steps in `[run].steps`.
set -euo pipefail

kit_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${1:-.}" && pwd)"
config_args=()
if [[ -f "$project_dir/sqlcheck.toml" ]]; then
    config_args=(--config "$project_dir/sqlcheck.toml")
fi

cd "$project_dir"
exec uv run --project "$kit_dir" sqlcheck run ${config_args[@]+"${config_args[@]}"}
