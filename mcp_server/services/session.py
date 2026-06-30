"""Session file and path resolution for the MCP server.

specflow_session.json lives in the project root (parent of the specs directory).
It is the single source of truth for the current generation_id and survives
MCP process restarts and client switches (Cursor ↔ Claude Code).

All paths are anchored to spec_dir.parent (project root), never to the
process working directory. MCP server cwd is unreliable — global Cursor
configs set it to the home directory, and other clients vary.
"""

import json
import logging
from pathlib import Path

from pydantic import FileUrl, ValidationError
from fastmcp import Context

from services.file_sync import SESSION_FILENAME

logger = logging.getLogger(__name__)

# Captured on the first check_specification_completeness or run_generation call.
# Used by check_status and retry_generation to locate specflow_session.json
# without requiring a spec_dir argument.
_project_root: Path | None = None


def set_project_root(root: Path) -> None:
    global _project_root
    _project_root = root


def session_file(project_root: Path | None = None) -> Path:
    """Return path to specflow_session.json. Raises RuntimeError if root unknown."""
    base = project_root if project_root is not None else _project_root
    if base is None:
        raise RuntimeError(
            "project_root is not known yet — call check_specification_completeness or run_generation first"
        )
    return base / SESSION_FILENAME


def load_session(project_root: Path | None = None) -> str | None:
    """Read specflow_session.json and return generation_id, or None.

    Side-effect: restores _project_root from the session file after an MCP restart
    when the caller has not provided a project_root.
    """
    global _project_root
    try:
        f = session_file(project_root)
        data = json.loads(f.read_text())
        eid = data.get("generation_id") or None
        if eid:
            logger.debug("Session loaded from %s: generation_id=%s", f, eid)
        if _project_root is None:
            stored_root = data.get("project_root")
            if stored_root:
                candidate = Path(stored_root)
                if candidate.is_dir():
                    _project_root = candidate
                    logger.info("Restored project_root from session file: %s", _project_root)
        return eid
    except (FileNotFoundError, RuntimeError):
        pass
    except Exception as e:
        logger.warning("Failed to load session file: %s", e)
    return None


def write_session(generation_id: str, project_root: Path) -> Path:
    """Write generation_id and project_root to specflow_session.json."""
    try:
        f = session_file(project_root)
        f.write_text(json.dumps(
            {"generation_id": generation_id, "project_root": str(project_root)},
            indent=2,
        ))
        logger.info("Session saved to %s: generation_id=%s", f, generation_id)
        return f
    except Exception as e:
        logger.warning("Failed to write session file: %s", e)
        return session_file(project_root)


def resolve_generation_id(
    generation_id: str | None,
    project_root: Path | None = None,
) -> str | None:
    """Return generation_id from explicit arg or session file (in that order)."""
    if generation_id:
        return generation_id
    return load_session(project_root)


async def apply_project_root_from_context(ctx: Context) -> None:
    """Set _project_root from the MCP client's first workspace root if not yet known."""
    global _project_root
    if _project_root is not None:
        logger.debug("apply_project_root_from_context: already set to %s, skipping", _project_root)
        return
    try:
        roots = await ctx.list_roots()
    except ValidationError as e:
        # Cursor sends workspace roots as raw paths instead of file:// URLs,
        # which fails pydantic's URL validation. Extract the raw path from the
        # error's input_value and use it directly.
        logger.warning(
            "apply_project_root_from_context: list_roots() raised ValidationError "
            "(likely raw path instead of file:// URL): %s", e
        )
        for err in e.errors():
            raw = err.get("input")
            if isinstance(raw, str) and raw.startswith("/"):
                candidate = Path(raw)
                logger.info("apply_project_root_from_context: extracted raw path from ValidationError: %s (is_dir=%s)", candidate, candidate.is_dir())
                if candidate.is_dir():
                    _project_root = candidate
                    logger.info("project_root set from raw path in ListRootsResult: %s", _project_root)
                    return
        logger.warning("apply_project_root_from_context: could not extract usable path from ValidationError")
        return
    except Exception as e:
        logger.warning("apply_project_root_from_context: list_roots() raised %s: %s", type(e).__name__, e)
        return
    logger.info("apply_project_root_from_context: list_roots() returned %d root(s): %s", len(roots), roots)
    if not roots:
        logger.warning("apply_project_root_from_context: no workspace roots from MCP client — relative paths will not resolve")
        return
    uri = getattr(roots[0], "uri", None)
    logger.info("apply_project_root_from_context: first root uri=%r (type=%s)", uri, type(uri).__name__)
    if not isinstance(uri, FileUrl):
        logger.warning("apply_project_root_from_context: uri is not a FileUrl, got %s — cannot set project_root", type(uri).__name__)
        return
    try:
        candidate = Path(uri.path)
        logger.info("apply_project_root_from_context: candidate path=%s is_dir=%s", candidate, candidate.is_dir())
        if candidate.is_dir():
            _project_root = candidate
            logger.info("project_root set from MCP workspace root: %s", _project_root)
        else:
            logger.warning("apply_project_root_from_context: candidate %s is not a directory", candidate)
    except Exception as e:
        logger.warning("apply_project_root_from_context: could not use MCP workspace root: %s", e)


def resolve_path(path: str) -> Path:
    """Resolve a path string to an absolute Path.

    Absolute paths are returned as-is. Relative paths are resolved against
    _project_root. Raises ValueError when _project_root is not yet set.
    """
    path_obj = Path(path)
    if path_obj.is_absolute():
        return path_obj
    if _project_root is not None:
        return _project_root / path
    logger.error(
        "resolve_path(%r): _project_root is None — list_roots() returned no usable root. "
        "Use an absolute path or ensure your IDE sends workspace roots to the MCP server.",
        path,
    )
    raise ValueError(
        f"Relative path {path!r} cannot be resolved: project root is not known yet. "
        "Use an absolute path in your MCP config, or call check_specification_completeness or run_generation first."
    )


def ensure_directory_exists(path: Path, name: str, original_path: str | None = None) -> str | None:
    """Return error JSON string if directory doesn't exist, None otherwise."""
    if not path.exists():
        error_data = {
            "error": f"{name} not found",
            "message": f"Please ensure {name} exists. Looked in: {path}",
        }
        if original_path and not Path(original_path).is_absolute():
            error_data["hint"] = (
                f"'{original_path}' is a relative path. "
                "Use an absolute path in your MCP config (e.g. /home/user/project/specs)."
            )
        else:
            error_data["hint"] = "Verify the path exists and is accessible."
        return json.dumps(error_data, indent=2)
    return None
