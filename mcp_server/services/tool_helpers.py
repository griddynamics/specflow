"""Common helper patterns for MCP tool handlers."""

import json
import logging
import os
from pathlib import Path

from fastmcp import Context

from schemas.generation_workflow_enums import GenerationStatus
from services.specflow_backend import call_backend_endpoint
from services.file_sync import ensure_gain_json
from services.session import (
    apply_project_root_from_context,
    ensure_directory_exists,
    resolve_path,
    set_project_root,
)

logger = logging.getLogger(__name__)

# --- Workspace count env resolution ---

_DEFAULT_WORKSPACE_COUNT: int | None = None
_wc_env = os.getenv("WORKSPACE_COUNT")
if _wc_env is not None:
    try:
        _parsed = int(_wc_env)
        if _parsed not in (1, 2, 3):
            raise ValueError(f"out of range: {_parsed}")
        _DEFAULT_WORKSPACE_COUNT = _parsed
        logger.info("WORKSPACE_COUNT from environment: %s", _DEFAULT_WORKSPACE_COUNT)
    except ValueError:
        logger.warning("Invalid WORKSPACE_COUNT env var %r, ignoring (must be 1, 2, or 3)", _wc_env)


def resolve_workspace_count() -> int | None:
    """Return WORKSPACE_COUNT from MCP env, or None if unset (backend default applies)."""
    return _DEFAULT_WORKSPACE_COUNT


# --- Status helpers ---

async def check_status_safe(generation_id: str) -> dict | None:
    """Check generation status without raising. Returns response dict or None on error."""
    try:
        text = await call_backend_endpoint(
            endpoint=f"/api/v1/generation-sessions/{generation_id}/status",
            method="GET",
            timeout_seconds=10,
        )
        return json.loads(text)
    except Exception:
        return None


def is_generation_in_progress(
    status_data: dict | None,
    statuses: tuple[str, ...] = (GenerationStatus.RUNNING, GenerationStatus.INITIALIZING),
) -> bool:
    """True if status_data reports a blocking in-progress status (case-insensitive).

    Single source of truth for "is this generation already running?" so callers that
    build their own rejection payload (e.g. the run_generation / retry contract guards)
    share the exact same status detection as ``guard_if_running``. Blocks INITIALIZING
    as well as RUNNING: a session is reserved for work the moment it leaves PENDING.
    """
    if not status_data:
        return False
    return status_data.get("status", "").lower() in statuses


async def guard_if_running(
    generation_id: str,
    statuses: tuple[str, ...] = (GenerationStatus.RUNNING, GenerationStatus.INITIALIZING),
) -> str | None:
    """Return error JSON if generation is in a blocking status, else None."""
    status_data = await check_status_safe(generation_id)
    if not is_generation_in_progress(status_data, statuses):
        return None
    current = (status_data or {}).get("status", "").lower()
    return json.dumps({
        "error": f"Cannot proceed — generation is {current}",
        "status": current,
        "generation_id": generation_id,
        "message": (
            f"Generation is currently {current}. "
            "Use check_status to monitor progress."
        ),
    }, indent=2)


# --- Path resolution helper ---

async def resolve_spec_context(
    spec_dir: str,
    src_dir: str,
    ctx: Context,
) -> tuple[Path, Path, Path, str | None]:
    """Common preamble for tools that take spec_dir + src_dir.

    Sets _project_root to spec_dir.parent so subsequent relative-path resolution works.
    Returns (spec_dir_path, project_root, src_dir_path, error_json_or_none).
    On directory-not-found error only error_json is meaningful; callers return it immediately.
    """
    await apply_project_root_from_context(ctx)
    spec_dir_path = resolve_path(spec_dir)
    error = ensure_directory_exists(spec_dir_path, "Specification directory", original_path=spec_dir)
    project_root = spec_dir_path.parent
    if error is None:
        set_project_root(project_root)
        ensure_gain_json(project_root)
    src_p = Path(src_dir)
    src_path = src_p if src_p.is_absolute() else project_root / src_dir
    return spec_dir_path, project_root, src_path, error
