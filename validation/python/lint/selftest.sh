#!/usr/bin/env bash
# Validates the kit itself: the semgrep rules (shipped and configurable) against their
# fixtures, the self-tests (which also run run.sh end to end on examples/clean_project), and
# the kit's own Python under its own ruff and mypy configuration.
# The tools come from PATH: run `uv sync` here once, then `uv run ./selftest.sh`.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
export PYTHONDONTWRITEBYTECODE=1
semgrep --test --metrics=off --config semgrep semgrep/fixtures
semgrep --test --metrics=off --config configurable/semgrep configurable/semgrep/fixtures
pytest -c pyproject.toml -q -p no:cacheprovider tests
ruff format --config ruff.toml --no-cache --check --extend-exclude examples/clean_project .
ruff check --config ruff.toml --no-cache --ignore-noqa --extend-exclude examples/clean_project .
(cd examples/clean_project && ruff check --config ../../ruff.toml --no-cache --ignore-noqa .)
mypy --config-file mypy.ini --cache-dir=/dev/null tests kit_tools
