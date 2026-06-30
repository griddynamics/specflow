"""
Generation workflow orchestration service.

Handles the generation run workflow for MCP:
- Sync files (specs + src) to backend workspace
- Backend will sync workspace 1 to workspaces 2 & 3
- Async generation start
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from services.file_sync_orchestrator import FileSyncOrchestrator
from services.specflow_backend import SpecFlowBackendService
from services.llm_tiers import apply_llm_tier_overrides, apply_mcp_servers_enabled_form

logger = logging.getLogger(__name__)


def resolve_relative_outputs_dir(synced: Optional[str], outputs_dir_arg: str) -> str:
    """Workspace-relative outputs path to send to the backend.

    Prefer the archive-relative path the sync step computed (matches where files actually
    landed). Fall back to the caller's argument only when sync omitted it — and even then
    preserve a nested relative path (e.g. "build/docs"); only an absolute path is reduced
    to its basename. Using ``Path(...).name`` unconditionally would drop the parent segment
    of a nested outputs_dir, the exact bug the archive-relative plumbing fixes.
    """
    if synced:
        return synced
    arg = Path(outputs_dir_arg)
    return arg.name if arg.is_absolute() else outputs_dir_arg.lstrip("./")


@dataclass(frozen=True)
class AuthMeSnapshot:
    """Typed subset of GET /api/v1/auth/me for MCP generation-session gating."""

    recoverable_session: Optional[str]
    at_capacity: bool
    max_concurrent_sessions: int
    active_generation_sessions: tuple[dict[str, Any], ...]

    @classmethod
    def from_json(cls, data: dict) -> "AuthMeSnapshot":
        raw = data.get("active_generation_sessions") or []
        rows = tuple(r for r in raw if isinstance(r, dict))
        return cls(
            # backend computes this on-the-fly from active_generation_sessions;
            # it is not stored in Firestore — use only as a recovery hint
            recoverable_session=data.get("current_process"),
            at_capacity=bool(data.get("at_capacity")),
            max_concurrent_sessions=int(data.get("max_concurrent_sessions") or 5),
            active_generation_sessions=rows,
        )


class GenerationOrchestrator:
    """Orchestrates generation workflows."""

    @staticmethod
    async def get_auth_me_snapshot() -> Optional[AuthMeSnapshot]:
        """
        Fetch typed generation-session snapshot from GET /api/v1/auth/me.
        """
        try:
            backend_service = SpecFlowBackendService()
            timeout = httpx.Timeout(10.0, connect=5.0)
            headers = backend_service._get_headers()

            async with httpx.AsyncClient(timeout=timeout) as client:
                url = f"{backend_service.base_url}/api/v1/auth/me"
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
                return AuthMeSnapshot.from_json(data)
        except Exception as e:
            logger.warning("Failed to get auth/me generation-session snapshot: %s", e)
            return None

    @staticmethod
    async def _sync_and_call(
        spec_dir: Path,
        src_dir: Path,
        outputs_dir: str,
        generation_id: Optional[str],
        workflow_type: str,
        backend_endpoint: str,
        extra_form_data: Optional[dict] = None,
        workspace_count: Optional[int] = None,
    ) -> dict:
        """
        Shared workflow: sync files to backend, then POST to a backend endpoint.

        1. Resolves outputs_dir to absolute path (if it exists on disk).
        2. Syncs specs + src + outputs to primary workspace.
        3. Builds form data with workspace-relative paths + LLM overrides.
        4. POSTs to the given backend endpoint.

        Args:
            spec_dir: Absolute path to the specification directory.
            src_dir:  Absolute path to the source directory (may not exist).
            outputs_dir: Outputs directory name (e.g. "docs" or "specflow").
            generation_id: Optional generation ID to reuse.
            workflow_type: Workflow type for sync (e.g. "planning", "generation_run").
            backend_endpoint: Backend URL path (e.g. "/api/v1/generation/plan").
            extra_form_data: Optional extra fields to include in the POST form data.
            workspace_count: Number of active workspaces to allocate (1, 2, or 3).
                             Forwarded to the sync step so new generations respect the
                             configured count. Ignored when reusing an existing generation.

        Returns:
            Response data dictionary from the backend.
        """
        logger.info(f"Step 1/2: Syncing files (generation_id={generation_id or 'new'})...")

        # Resolve outputs_dir relative to project root (spec_dir.parent), not process cwd.
        # Process cwd is unreliable in MCP clients (often the home directory), so using
        # Path.resolve() on a relative string can grab a completely unrelated directory
        # (e.g. ~/docs) and corrupt the archive's common-ancestor calculation.
        project_root = spec_dir.parent
        _outputs_path = Path(outputs_dir) if Path(outputs_dir).is_absolute() else project_root / outputs_dir
        outputs_dir_path = _outputs_path if _outputs_path.exists() and _outputs_path.is_dir() else None

        sync_response = await FileSyncOrchestrator.sync_files_to_backend(
            spec_dir=spec_dir,
            src_dir=src_dir,
            generation_id=generation_id,
            workflow_type=workflow_type,
            sync_to_all=False,
            outputs_dir=outputs_dir_path,
            workspace_count=workspace_count,
        )

        # Validate generation_id from sync response
        synced_generation_id = sync_response.get("generation_id")
        if not synced_generation_id:
            raise Exception("File sync did not return an generation_id")
        if generation_id and synced_generation_id != generation_id:
            logger.warning(f"Sync returned different generation_id: {synced_generation_id} vs {generation_id}")
        generation_id = synced_generation_id

        logger.info("Files synced successfully")

        # Build form data with workspace-relative paths
        relative_spec_path = sync_response.get("spec_path")
        relative_src_dir = sync_response.get("src_dir")
        relative_outputs_dir = resolve_relative_outputs_dir(
            sync_response.get("outputs_dir"), outputs_dir
        )

        logger.info(f"Step 2/2: Calling {backend_endpoint}...")

        form_data = {
            "spec_path": relative_spec_path,
            "outputs_dir": relative_outputs_dir,
            "src_dir": relative_src_dir,
            "generation_id": generation_id,
        }
        if extra_form_data:
            form_data.update(extra_form_data)

        # Propagate LLM tier overrides from MCP environment
        apply_llm_tier_overrides(form_data)
        apply_mcp_servers_enabled_form(form_data)

        # Call backend endpoint
        backend_service = SpecFlowBackendService()
        timeout = httpx.Timeout(30.0, connect=10.0)
        headers = backend_service._get_headers()

        async with httpx.AsyncClient(timeout=timeout) as client:
            url = f"{backend_service.base_url}{backend_endpoint}"
            logger.info(f"Calling {url} with generation_id={generation_id}")
            response = await client.post(url, data=form_data, headers=headers)
            if response.is_error:
                try:
                    body = response.json()
                    detail = body.get("detail") or body.get("message") or response.text
                except Exception:
                    detail = response.text
                raise httpx.HTTPStatusError(
                    f"Backend returned {response.status_code}: {detail}",
                    request=response.request,
                    response=response,
                )
            response_text = response.text

        response_data = json.loads(response_text)
        logger.info(f"Backend response: {response_data}")
        return response_data

    @staticmethod
    async def run_generation(
        spec_dir: Path,
        src_dir: Path,
        outputs_dir: str,
        generation_id: Optional[str] = None,
        notification_email: Optional[str] = None,
        workspace_count: Optional[int] = None,
    ) -> dict:
        """
        Run generation workflow (MCP flow).

        1. Syncs specs + src + outputs to workspace 1 (archive paths are always relative).
        2. Starts async generation workflow on the backend.

        Args:
            spec_dir: Absolute path to the specification directory.
            src_dir:  Absolute path to the source directory (may not exist).
            outputs_dir: Outputs directory name (e.g. "specflow").
            generation_id: Optional generation ID to reuse.
            notification_email: Optional email for completion notification.
            workspace_count: Number of parallel workspaces (1, 2, or 3). None → backend default.

        Returns:
            Response data dictionary with generation_id and status.
        """
        extra: dict = {}
        if notification_email:
            extra["notification_email"] = notification_email
        if workspace_count is not None:
            extra["workspace_count"] = str(workspace_count)

        return await GenerationOrchestrator._sync_and_call(
            spec_dir=spec_dir,
            src_dir=src_dir,
            outputs_dir=outputs_dir,
            generation_id=generation_id,
            workflow_type="generation_run",
            backend_endpoint="/api/v1/generation-sessions/run",
            extra_form_data=extra if extra else None,
            workspace_count=workspace_count,
        )
