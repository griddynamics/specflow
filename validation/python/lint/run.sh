#!/usr/bin/env bash
# Runs every Python lint of the kit over one project and fails if any of them finds anything.
# Usage: run.sh [PROJECT_DIR]   (default: the current directory)
#
# The tools come from PATH: run `uv sync` in the kit folder once, then call
# `uv run --project <kit dir> <kit dir>/run.sh <project dir>` (or activate the kit's .venv).
# Every step runs even after a failure. Exit status: 0 clean, 1 a step failed, 2 no such project.
#
# Steps, in order (names are the ones KIT_SKIP takes):
#   ruff-format             ruff format --check
#   ruff                    ruff check --ignore-noqa (a `# noqa` hides nothing)
#   mypy                    mypy, strict, pydantic plugin
#   semgrep                 the kit's semgrep/ rules, `--disable-nosem` (a `# nosemgrep` hides nothing)
#   import-linter           lint-imports with the import contracts file
#   import-linter-coverage  kit_tools/importlinter_source_coverage.py (only when configured)
#   deptry                  dependency hygiene (only when the project has a pyproject.toml)
#   jsonschema              check-jsonschema (only when a checks file exists)
#
# Environment:
#   IMPORTLINTER_CONFIG         import contracts file (default: the kit's importlinter.toml)
#   IMPORTLINTER_COVERAGE_ARGS  arguments of kit_tools/importlinter_source_coverage.py, e.g.
#                               "--contracts importlinter.toml --source-root src --contract vendor-sdk-confined --exempt gateway"
#                               (vendor-sdk-confined: copy it from configurable/importlinter.confinement.example.toml first)
#                               (unset: the step is skipped)
#   KIT_SEMGREP_CONFIGURABLE    a semgrep rule file or folder added to semgrep/, normally the
#                               adopter's edited copy of configurable/semgrep (unset: not added)
#   JSONSCHEMA_CHECKS           file of `<schema> <file or glob>...` lines, one schema per line;
#                               a schema named `vendor.<name>` is a check-jsonschema built-in
#                               (default: <project>/jsonschema-checks.txt; absent: step skipped)
#   KIT_TARGET_PYTHON           interpreter of the TARGET project's environment, handed to mypy
#                               (`--python-executable`) so third-party imports resolve
#                               (default: <project>/.venv/bin/python when it exists). The target's
#                               dependencies must be installed in it; deptry's DEP001/DEP003
#                               mapping is most reliable when deptry itself runs in that environment.
#   KIT_SKIP                    space-separated step names to skip
#
# A kit folder located inside the project is excluded from ruff, mypy, semgrep and deptry
# (for deptry it is added to the project's own `[tool.deptry] extend_exclude`, not replacing it).
set -uo pipefail

kit_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${1:-.}" 2>/dev/null && pwd)" || {
    echo "run.sh: no such project directory: ${1:-.}" >&2
    exit 2
}
importlinter_config="${IMPORTLINTER_CONFIG:-$kit_dir/importlinter.toml}"
jsonschema_checks="${JSONSCHEMA_CHECKS:-$project_dir/jsonschema-checks.txt}"
skip=" ${KIT_SKIP:-} "
status=0

step() {
    local name="$1"
    shift
    if [[ "$skip" == *" $name "* ]]; then
        echo "==> $name: skipped (KIT_SKIP)"
        return 0
    fi
    echo "==> $name: $*"
    "$@" || status=1
}

cd "$project_dir" || exit 2

# A kit folder vendored inside the project is never scanned: its fixtures are deliberate
# violations and its tests and examples are not project code.
ruff_exclude=()
mypy_exclude=()
semgrep_exclude=()
deptry_exclude=()
if [[ "$kit_dir" == "$project_dir"/* ]]; then
    kit_rel="${kit_dir#"$project_dir"/}"
    ruff_exclude=(--extend-exclude "$kit_rel")
    mypy_exclude=(--exclude "(^|/)(semgrep/fixtures|examples/clean_project)/|^$kit_rel/")
    semgrep_exclude=(--exclude "$kit_rel")
    # A command-line --extend-exclude replaces the pyproject.toml setting, so the project's own
    # entries (for example the colocated-test pattern) are re-passed next to the kit folder.
    deptry_exclude=(--extend-exclude "$kit_rel")
    if [[ -f pyproject.toml ]]; then
        while IFS= read -r pattern; do
            deptry_exclude+=(--extend-exclude "$pattern")
        done < <(python -c 'import sys, tomllib
with open("pyproject.toml", "rb") as f:
    entries = tomllib.load(f).get("tool", {}).get("deptry", {}).get("extend_exclude", [])
sys.stdout.write("".join(f"{e}\n" for e in entries))')
    fi
fi

# mypy resolves third-party imports in the target's interpreter, not in the kit's environment.
mypy_python=()
target_python="${KIT_TARGET_PYTHON:-$project_dir/.venv/bin/python}"
if [[ -x "$target_python" ]]; then
    mypy_python=(--python-executable "$target_python")
fi

step ruff-format ruff format --config "$kit_dir/ruff.toml" --check ${ruff_exclude[@]+"${ruff_exclude[@]}"} .
step ruff ruff check --config "$kit_dir/ruff.toml" --ignore-noqa ${ruff_exclude[@]+"${ruff_exclude[@]}"} .
step mypy mypy --config-file "$kit_dir/mypy.ini" ${mypy_python[@]+"${mypy_python[@]}"} ${mypy_exclude[@]+"${mypy_exclude[@]}"} .

semgrep_configs=(--config "$kit_dir/semgrep")
if [[ -n "${KIT_SEMGREP_CONFIGURABLE:-}" ]]; then
    semgrep_configs+=(--config "$KIT_SEMGREP_CONFIGURABLE")
fi
step semgrep semgrep scan "${semgrep_configs[@]}" ${semgrep_exclude[@]+"${semgrep_exclude[@]}"} --project-root . \
    --error --disable-nosem --metrics=off --quiet .

step import-linter lint-imports --config "$importlinter_config"

if [[ -n "${IMPORTLINTER_COVERAGE_ARGS:-}" ]]; then
    read -r -a coverage_args <<<"$IMPORTLINTER_COVERAGE_ARGS"
    step import-linter-coverage python "$kit_dir/kit_tools/importlinter_source_coverage.py" \
        "${coverage_args[@]}"
fi

if [[ -f pyproject.toml ]]; then
    step deptry deptry ${deptry_exclude[@]+"${deptry_exclude[@]}"} .
else
    echo "==> deptry: skipped (no pyproject.toml in $project_dir)"
fi

if [[ -f "$jsonschema_checks" ]]; then
    while read -r -u 3 schema files; do
        if [[ -z "$schema" || "$schema" == \#* ]]; then
            continue
        fi
        # Unquoted on purpose: the file list is split into words and its globs expanded.
        targets=($files)
        if [[ "$schema" == vendor.* ]]; then
            step jsonschema check-jsonschema --builtin-schema "$schema" "${targets[@]}"
        else
            step jsonschema check-jsonschema --schemafile "$schema" "${targets[@]}"
        fi
    done 3<"$jsonschema_checks"
fi

exit "$status"
