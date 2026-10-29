#!/usr/bin/env bash
# Validates the kit itself, from this folder only: the Semgrep rules against their
# fixtures, the self-tests (with branch coverage), and the kit's own code under its own
# ruff and mypy settings. Needs `uv`; leaves no cache behind.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
export PYTHONDONTWRITEBYTECODE=1

uv sync
COVERAGE_FILE="$(mktemp)"
export COVERAGE_FILE
trap 'rm -f "$COVERAGE_FILE"' EXIT
uv run semgrep --validate --metrics=off --config semgrep
uv run semgrep --test --metrics=off --config semgrep semgrep/fixtures
uv run sqlcheck run --config tests/fixtures/example_project/sqlcheck.toml
uv run pytest -p no:cacheprovider --cov=sqlcheck --cov-branch --cov-report=term-missing
uv run ruff format --no-cache --check .
uv run ruff check --no-cache .
uv run mypy --cache-dir=/dev/null
