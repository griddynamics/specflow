"""Extracted service functions shared between the MCP tool wrappers and the CLI.

These plain async functions contain the reusable logic that was previously
inline in the @mcp.tool decorated functions. Both the MCP server (server.py)
and the CLI (cli.py) call these functions directly — no ctx: Context parameter
— after the caller has set project_root via set_project_root().

This keeps the MCP tool behavior byte-identical while enabling the CLI to
call the same code paths without an MCP Context object.
"""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import logging
import os
import subprocess
import sys
import tarfile
from typing import Any

from services.specflow_backend import (
    SpecFlowBackendService,
    call_backend_endpoint,
    call_backend_endpoint_bytes,
)
from services.session import resolve_path

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"completed", "failed"})
_NO_SUITABLE_IMPLEMENTATION = "no suitable implementation"
_MACOS_BUNDLE_ID = "com.griddynamics.specflow"
_MACOS_BUNDLE_NAME = "SpecFlow"

# macOS app metadata is process-global state; prepare it once, not per notification.
_macos_plyer_prepared = False


# ---------------------------------------------------------------------------
# Desktop notifications (best-effort; never raises)
# ---------------------------------------------------------------------------


class _PlyerCaptureHandler(logging.Handler):
    """Capture plyer diagnostics so they never paint over the TUI (discarded)."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _prepare_macos_plyer_notification() -> None:
    """Prepare the macOS app metadata plyer needs when running as a CLI.

    Mutates process-global ``NSBundle`` state and initializes the shared
    ``NSApplication``, so it runs at most once per process; failures are
    swallowed and leave the flag unset so a later attempt can retry.
    """
    global _macos_plyer_prepared
    if sys.platform != "darwin" or _macos_plyer_prepared:
        return
    try:
        from pyobjus import autoclass, objc_str
        from pyobjus.dylib_manager import INCLUDE, load_framework

        load_framework(INCLUDE.Foundation)
        load_framework(INCLUDE.AppKit)
        ns_bundle = autoclass("NSBundle")
        ns_application = autoclass("NSApplication")
        bundle = ns_bundle.mainBundle()
        info = bundle.infoDictionary
        info.setObject_forKey_(objc_str(_MACOS_BUNDLE_ID), objc_str("CFBundleIdentifier"))
        info.setObject_forKey_(objc_str(_MACOS_BUNDLE_NAME), objc_str("CFBundleName"))
        ns_application.sharedApplication()
        _macos_plyer_prepared = True
    except Exception:
        logger.debug("macOS plyer preparation failed", exc_info=True)


def _notify_with_plyer(title: str, message: str) -> bool:
    """Attempt a plyer notification; return True if it was delivered.

    plyer's backends are chatty (stdout/stderr + a 'plyer' logger). Inside the
    TUI that output would corrupt the render, so it is captured and discarded.
    A captured "no suitable implementation" message means plyer has no working
    backend, so we report failure and let the caller fall back.
    """
    try:
        from plyer import notification  # type: ignore[import-untyped]
    except Exception:
        return False

    _prepare_macos_plyer_notification()
    plyer_logger = logging.getLogger("plyer")
    old_level = plyer_logger.level
    old_propagate = plyer_logger.propagate
    capture = _PlyerCaptureHandler()
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        plyer_logger.addHandler(capture)
        plyer_logger.setLevel(logging.DEBUG)
        plyer_logger.propagate = False
        with redirect_stdout(stdout), redirect_stderr(stderr):
            notification.notify(title=title, message=message, app_name="SpecFlow", timeout=10)
    except Exception:
        return False
    finally:
        plyer_logger.removeHandler(capture)
        plyer_logger.setLevel(old_level)
        plyer_logger.propagate = old_propagate

    combined = " ".join([*capture.messages, stdout.getvalue(), stderr.getvalue()]).lower()
    return _NO_SUITABLE_IMPLEMENTATION not in combined


def notify_desktop(title: str, message: str) -> None:
    """Fire a best-effort desktop notification; never raises."""
    if _notify_with_plyer(title, message):
        return

    # Fallback: platform-specific subprocess when plyer is unavailable or fails.
    try:
        if sys.platform == "darwin":
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f"display notification {json.dumps(message)} with title {json.dumps(title)}",
                ],
                timeout=5,
                check=False,
                capture_output=True,
            )
        elif sys.platform.startswith("linux"):
            subprocess.run(
                ["notify-send", title, message],
                timeout=5,
                check=False,
                capture_output=True,
            )
    except Exception:
        logger.debug("Desktop notification fallback failed", exc_info=True)


# ---------------------------------------------------------------------------
# Pool-status & capacity helpers
# ---------------------------------------------------------------------------


async def fetch_pool_status() -> dict[str, Any]:
    """Fetch GET /api/v1/workspace/pool/status and return parsed JSON dict."""
    text = await call_backend_endpoint(
        endpoint="/api/v1/workspace/pool/status",
        method="GET",
        timeout_seconds=15,
    )
    return json.loads(text)


def format_grace(seconds: int) -> str:
    """Format a grace-period duration as 'Hh Mmin' (e.g. '1h 30min', '26min', '0min').

    Pure function; no I/O.
    """
    if seconds <= 0:
        return "0min"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0 and minutes > 0:
        return f"{hours}h {minutes}min"
    if hours > 0:
        return f"{hours}h 0min"
    return f"{minutes}min"


def render_capacity_message(cleaning_sets: list[dict[str, Any]]) -> str:
    """Render the no-availability capacity message for a list of cleaning sets.

    Each entry must have 'set_number' (int) and 'remaining_grace_seconds' (int).
    Returns a human-readable multi-line string.
    """
    lines = ["No workspaces available. Auto-clear grace period is 2h:"]
    for entry in cleaning_sets:
        n = entry.get("set_number", "?")
        secs = int(entry.get("remaining_grace_seconds", 0))
        lines.append(f"  Set {n} (left {format_grace(secs)})")
    lines.append("If you have already downloaded your outputs, free a set now:")
    if cleaning_sets:
        first_n = cleaning_sets[0].get("set_number", 1)
        lines.append(f"  specflow clear-workspace --set {first_n}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# download_outputs — shared extraction logic
# ---------------------------------------------------------------------------


async def download_and_extract_outputs(
    generation_id: str,
    outputs_dir: str,
) -> dict[str, Any]:
    """Download and extract generation outputs; returns a result dict.

    Callers (MCP tool and CLI) print the result themselves.
    Raises on I/O errors; returns an error dict on backend-level soft errors.
    """
    archive_bytes = await call_backend_endpoint_bytes(
        endpoint=f"/api/v1/generation-sessions/{generation_id}/outputs",
        timeout_seconds=120,
    )

    # Backend returns 200 JSON (not a tarball) when outputs not ready yet.
    if archive_bytes[:1] == b"{":
        return json.loads(archive_bytes.decode("utf-8"))

    out_path = resolve_path(outputs_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    extracted_files: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        resolved_base = out_path.resolve()
        base_str = str(resolved_base) + os.sep
        for member in tar.getmembers():
            target = (out_path / member.name).resolve()
            if target != resolved_base and not str(target).startswith(base_str):
                raise ValueError(f"Path traversal detected in archive: {member.name}")
            tar.extract(member, path=out_path, filter="data")
            if member.isfile():
                extracted_files.append(member.name)

    return {
        "status": "success",
        "generation_id": generation_id,
        "outputs_dir": str(out_path),
        "files_extracted": len(extracted_files),
        "files": extracted_files,
        "message": f"Downloaded {len(extracted_files)} files to {out_path}.",
    }


# ---------------------------------------------------------------------------
# Workspace clear (set of 3)
# ---------------------------------------------------------------------------

_SET_MEMBER_COUNT = 3


def workspace_ids_for_set(set_number: int, prefix: str = "ws") -> list[str]:
    """Return the 3 workspace IDs for a given set number.

    Uses the same ws-{set:02d}-{idx} convention as provisioning.
    """
    return [f"{prefix}-{set_number:02d}-{i}" for i in range(1, _SET_MEMBER_COUNT + 1)]


async def clear_workspace_set(
    set_number: int,
    backend_service: SpecFlowBackendService | None = None,
) -> list[dict[str, Any]]:
    """Call POST /api/v1/workspace/{id}/clear for each of the 3 members of a set.

    Returns a list of result dicts (one per workspace member), each with
    'workspace_id', 'success' (bool), and 'message' (str).
    """
    if backend_service is None:
        backend_service = SpecFlowBackendService()

    ws_ids = workspace_ids_for_set(set_number)
    results: list[dict[str, Any]] = []
    for ws_id in ws_ids:
        try:
            text = await backend_service.call_backend(
                endpoint=f"/api/v1/workspace/{ws_id}/clear",
                method="POST",
                timeout_seconds=30,
            )
            results.append({"workspace_id": ws_id, "success": True, "message": text})
        except Exception as exc:
            results.append({"workspace_id": ws_id, "success": False, "message": str(exc)})
    return results


# ---------------------------------------------------------------------------
# sessions — compose /auth/me + per-session /status
# ---------------------------------------------------------------------------


async def _fetch_session_status(gid: str) -> dict[str, Any]:
    """Fetch one session's status. Never raises — degrades to status 'unknown'.

    A single unreachable/slow/404 session must not blank the whole listing, so
    every failure (HTTP error, timeout, unparseable body) is contained here.
    """
    try:
        status_text = await call_backend_endpoint(
            endpoint=f"/api/v1/generation-sessions/{gid}/status",
            method="GET",
            timeout_seconds=10,
        )
        status_data = json.loads(status_text)
    except Exception as exc:
        logger.warning("Failed to fetch status for session %s: %s", gid, exc)
        status_data = {}
    return {
        "generation_id": gid,
        "status": status_data.get("status", "unknown"),
        "checkpoint": status_data.get("checkpoint", ""),
        "current_phase": status_data.get("current_phase", ""),
        "workspace_phases": status_data.get("workspace_phases")
        or (status_data.get("progress") or {}).get("workspace_phases")
        or {},
    }


async def fetch_sessions(exclude: set[str] | None = None) -> list[dict[str, Any]]:
    """List generation sessions (active and recently completed) via GET /generation-sessions/.

    Returns a list of dicts with 'generation_id', 'status', 'checkpoint',
    'created_at'. Completed and failed sessions are included so the TUI sessions
    screen can show the full recent history, not just in-flight runs.

    Falls back to the /auth/me + per-session status approach when the list
    endpoint is unavailable (older backends).

    ``exclude`` omits the given generation ids from the result (used by the TUI
    when a session is already being polled by the dashboard).
    """
    skip = exclude or set()
    try:
        list_text = await call_backend_endpoint(
            endpoint="/api/v1/generation-sessions/",
            method="GET",
            timeout_seconds=15,
        )
        sessions: list[dict[str, Any]] = json.loads(list_text)
        if not isinstance(sessions, list):
            raise ValueError("unexpected response shape")
        return [s for s in sessions if s.get("generation_id") not in skip]
    except Exception as exc:
        logger.warning("Session list endpoint unavailable, falling back to /auth/me: %s", exc)

    # Fallback: compose /auth/me + per-session /status (active sessions only).
    auth_text = await call_backend_endpoint(
        endpoint="/api/v1/auth/me",
        method="GET",
        timeout_seconds=15,
    )
    auth_data = json.loads(auth_text)
    active = auth_data.get("active_generation_sessions") or []

    gids: list[str] = []
    for entry in active:
        gid = entry.get("generation_id") if isinstance(entry, dict) else str(entry)
        if gid and gid not in skip:
            gids.append(gid)

    return list(await asyncio.gather(*(_fetch_session_status(gid) for gid in gids)))
