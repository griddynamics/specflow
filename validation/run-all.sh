#!/bin/sh
# Run every track of the validation kit against one project, each from its own folder.
# Stops on the first failing track. POSIX sh.
#
# Usage:
#   sh run-all.sh --project /path/to/project [options]
#   sh run-all.sh --selftest [--tracks LIST]
#
# Options:
#   --project DIR     The project to validate (required unless --selftest).
#   --tracks LIST     Comma-separated subset, in the order given. Default: all.
#                     Names: python-lint, python-scripts, python-arch,
#                            typescript-lint, typescript-scripts, cypher, sql
#   --skip LIST       Comma-separated tracks to leave out.
#   --install         Install each track's tools first (uv sync / npm install).
#   --static-only     Cypher: skip the steps that need Docker.
#   --selftest        Run each track's own self-test instead of checking a project.
#   --list            Print the track names and exit.
#   -h, --help        Print this help and exit.
#
# Inputs looked up inside the project (a track whose input is missing is skipped
# with a notice, never failed):
#   python-scripts      pystatic.toml (optional), reviewed-suppressions.toml (optional)
#   python-arch         archkit.toml (required); runs with KIT_TARGET_PYTHON, else
#                       PROJECT/.venv/bin/python, which must have archkit installed
#   typescript-scripts  validation.config.json (optional; otherwise --root is used)
#   cypher              cypher-checks.toml (required)
#   sql                 sqlcheck.toml (required)

set -u

KIT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ALL="python-lint python-scripts python-arch typescript-lint typescript-scripts cypher sql"

PROJECT=""
TRACKS=""
SKIP=""
INSTALL=0
STATIC_ONLY=0
SELFTEST=0

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --project) [ $# -ge 2 ] || { echo "--project needs a value" >&2; exit 2; }; PROJECT=$2; shift 2 ;;
    --tracks) [ $# -ge 2 ] || { echo "--tracks needs a value" >&2; exit 2; }; TRACKS=$(echo "$2" | tr ',' ' '); shift 2 ;;
    --skip) [ $# -ge 2 ] || { echo "--skip needs a value" >&2; exit 2; }; SKIP=$(echo "$2" | tr ',' ' '); shift 2 ;;
    --install) INSTALL=1; shift ;;
    --static-only) STATIC_ONLY=1; shift ;;
    --selftest) SELFTEST=1; shift ;;
    --list) for t in $ALL; do echo "$t"; done; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -n "$TRACKS" ] || TRACKS=$ALL

for t in $TRACKS $SKIP; do
  case " $ALL " in
    *" $t "*) ;;
    *) echo "unknown track: $t" >&2; exit 2 ;;
  esac
done

if [ "$SELFTEST" -eq 0 ]; then
  [ -n "$PROJECT" ] || { echo "--project is required (or use --selftest)" >&2; exit 2; }
  [ -d "$PROJECT" ] || { echo "not a directory: $PROJECT" >&2; exit 2; }
  PROJECT=$(CDPATH= cd -- "$PROJECT" && pwd)
fi

skipped() {
  case " $SKIP " in *" $1 "*) return 0 ;; esac
  return 1
}

# Print and run a command; the caller decides what a failure means.
run() {
  echo "+ $*"
  "$@"
}

# install DIR KIND: prepare a track's tools.
install_track() {
  case "$1" in
    uv) run uv sync || return 1 ;;
    uv-extras) run uv sync --all-extras || return 1 ;;
    npm) run npm install --workspaces=false || return 1 ;;
    npm-uv) run npm install --workspaces=false || return 1; run uv sync || return 1 ;;
  esac
}

# Each track function runs inside the track's own folder.
track_python_lint() {
  [ "$INSTALL" -eq 0 ] || install_track uv || return 1
  if [ "$SELFTEST" -eq 1 ]; then run uv run bash selftest.sh; else run uv run bash run.sh "$PROJECT"; fi
}

track_python_scripts() {
  [ "$INSTALL" -eq 0 ] || install_track uv-extras || return 1
  if [ "$SELFTEST" -eq 1 ]; then run uv run pytest; return; fi
  set -- python -m pystatic.run --root "$PROJECT" --level all
  [ ! -f "$PROJECT/pystatic.toml" ] || set -- "$@" --config "$PROJECT/pystatic.toml"
  [ ! -f "$PROJECT/reviewed-suppressions.toml" ] || set -- "$@" --reviewed "$PROJECT/reviewed-suppressions.toml"
  run uv run "$@"
}

track_python_arch() {
  [ "$INSTALL" -eq 0 ] || install_track uv || return 1
  if [ "$SELFTEST" -eq 1 ]; then run uv run pytest; return; fi
  if [ ! -f "$PROJECT/archkit.toml" ]; then echo "skip python-arch: $PROJECT/archkit.toml not found"; return 0; fi
  # The checks import the project's app: run archkit in the project's
  # own environment (archkit installed there), not in this kit's venv.
  target_python="${KIT_TARGET_PYTHON:-$PROJECT/.venv/bin/python}"
  if [ ! -x "$target_python" ]; then
    echo "python-arch: no interpreter at $target_python; set KIT_TARGET_PYTHON to the project's Python (archkit installed there)" >&2
    return 1
  fi
  run "$target_python" -m archkit run --config "$PROJECT/archkit.toml"
}

track_typescript_lint() {
  [ "$INSTALL" -eq 0 ] || install_track npm-uv || return 1
  if [ "$SELFTEST" -eq 1 ]; then run npm run selftest; else run node run.mjs --project "$PROJECT"; fi
}

track_typescript_scripts() {
  [ "$INSTALL" -eq 0 ] || install_track npm-uv || return 1
  if [ "$SELFTEST" -eq 1 ]; then run npm test; return; fi
  if [ -f "$PROJECT/validation.config.json" ]; then
    run node run.mjs --config "$PROJECT/validation.config.json"
  else
    run node run.mjs --root "$PROJECT"
  fi
}

track_cypher() {
  [ "$INSTALL" -eq 0 ] || install_track uv || return 1
  if [ "$SELFTEST" -eq 1 ]; then run uv run pytest -m "not integration"; return; fi
  if [ ! -f "$PROJECT/cypher-checks.toml" ]; then echo "skip cypher: $PROJECT/cypher-checks.toml not found"; return 0; fi
  if [ "$STATIC_ONLY" -eq 1 ]; then
    run uv run cypher-checks all --config "$PROJECT/cypher-checks.toml" --static-only
  else
    run uv run cypher-checks all --config "$PROJECT/cypher-checks.toml"
  fi
}

track_sql() {
  [ "$INSTALL" -eq 0 ] || install_track uv || return 1
  if [ "$SELFTEST" -eq 1 ]; then run bash selftest.sh; return; fi
  if [ ! -f "$PROJECT/sqlcheck.toml" ]; then echo "skip sql: $PROJECT/sqlcheck.toml not found"; return 0; fi
  run bash run.sh "$PROJECT"
}

folder_of() {
  case "$1" in
    python-lint) echo python/lint ;;
    python-scripts) echo python/scripts ;;
    python-arch) echo python/arch ;;
    typescript-lint) echo typescript/lint ;;
    typescript-scripts) echo typescript/scripts ;;
    cypher) echo cypher ;;
    sql) echo sql ;;
  esac
}

for t in $TRACKS; do
  if skipped "$t"; then echo "== $t: skipped"; continue; fi
  dir="$KIT/$(folder_of "$t")"
  echo "== $t ($dir)"
  fn="track_$(echo "$t" | tr '-' '_')"
  if ! (cd "$dir" && "$fn"); then
    echo "== $t: FAILED" >&2
    exit 1
  fi
  echo "== $t: ok"
done

echo "== all selected tracks passed"
