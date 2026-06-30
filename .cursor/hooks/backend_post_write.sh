#!/usr/bin/env bash
# postToolUse hook: after Write, add ruff/radon context for backend/app Python files.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
export IDE_HOOK_FORMAT=json
exec uv run python scripts/ide_backend_quality.py --print-json
