"""
Shared file sync orchestration service.

Provides reusable file sync functionality for both specification completeness
check and generation run workflows.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from services.archive_builder import ArchiveBuilder
from services.specflow_backend import BackendContractRejection, SpecFlowBackendService
from services.llm_tiers import MCP_SERVERS_ENABLED_ENV, MCP_SERVERS_ENABLED_DEFAULT

logger = logging.getLogger(__name__)


class FileSyncOrchestrator:
    """Orchestrates file syncing to backend workspaces."""

    @staticmethod
    async def sync_files_to_backend(
        spec_dir: Path,
        src_dir: Path,
        generation_id: Optional[str] = None,
        workflow_type: str = "spec_completeness_check",
        sync_to_all: bool = False,
        outputs_dir: Optional[Path] = None,
        workspace_count: Optional[int] = None,
    ) -> dict:
        """
        Sync specification and source files to backend workspace.

        Builds an archive whose internal paths are always relative to the common
        ancestor of spec_dir and src_dir, then uploads it. The backend extracts
        the archive verbatim — no path fixups needed.

        If outputs_dir is provided, it's included in the archive (for user-edited
        plans or analysis outputs).

        Args:
            spec_dir: Absolute path to the specification directory.
            src_dir:  Absolute path to the source directory (may not exist).
            generation_id: Optional generation ID to reuse.
            workflow_type: Workflow type identifier.
            sync_to_all: Sync to all workspaces (True) or primary only (False).
            outputs_dir: Optional absolute path to outputs directory.
            workspace_count: Number of active workspaces to allocate (1, 2, or 3).
                             Only used when creating a new generation (no generation_id).
                             Passed to backend so allocation respects the intended count.

        Returns:
            Response data with generation_id, workspace_ids, spec_path, src_dir.

        Raises:
            Exception: If sync fails.
        """
        # 1. Build archive; get back the workspace-relative paths
        archive_data, has_src, relative_spec_path, relative_src_dir, relative_outputs_dir = (
            ArchiveBuilder.create_archive_for_analysis(spec_dir, src_dir, outputs_dir)
        )
        logger.info(
            f"Archive ready: {len(archive_data)} bytes  has_src={has_src}  "
            f"spec_path={relative_spec_path}  src_dir={relative_src_dir}"
        )

        # 2. Prepare form data — spec_path and src_dir are now workspace-relative.
        # Values are mixed (str + int workspace_count) and JSON-serialized below.
        params: dict[str, object] = {
            "spec_path": relative_spec_path,
            "src_dir": relative_src_dir,
            "workflow_type": workflow_type,
        }
        # Send the archive-relative outputs path (not outputs_dir.name) so backend
        # validation locates analysis/planning artifacts even when outputs_dir is nested
        # below the archive root. relative_outputs_dir is None when nothing was archived.
        if relative_outputs_dir is not None:
            params["outputs_dir"] = relative_outputs_dir

        if generation_id:
            params["generation_id"] = generation_id
            logger.info(f"Reusing generation {generation_id}")

        if workspace_count is not None and not generation_id:
            # Only send workspace_count for new generations. When reusing an existing
            # generation_id the backend skips allocation, so the value is irrelevant.
            params["workspace_count"] = workspace_count
            logger.info(f"Requesting workspace_count={workspace_count} for new generation")

        if not generation_id:
            mcp_raw = os.getenv(MCP_SERVERS_ENABLED_ENV, MCP_SERVERS_ENABLED_DEFAULT)
            if mcp_raw.strip():
                params["mcp_servers_enabled"] = mcp_raw.strip()
                logger.info("Including mcp_servers_enabled for new generation from MCP env")

        user_email = os.getenv("USER_EMAIL", "mcp-client")
        if user_email == "mcp-client":
            logger.warning("USER_EMAIL environment variable not set, using default 'mcp-client'")

        form_data = {
            "params": json.dumps(params),
            "user_id": user_email,
            "sync_to_all": "true" if sync_to_all else "false",
        }

        # 3. Upload
        backend_service = SpecFlowBackendService()
        try:
            logger.info(f"Uploading archive to backend (sync_to_all={sync_to_all})")
            response_text = await backend_service.upload_file(
                endpoint="/api/v1/workspace/sync",
                file_data=archive_data,
                filename="specs.tar.gz",
                form_data=form_data,
                timeout_seconds=120,
            )
        except BackendContractRejection:
            # A structured contract rejection must reach run_generation intact so the
            # user gets the same actionable message + code as an MCP-side precheck.
            raise
        except Exception as upload_error:
            logger.error(f"Failed to sync files to backend: {upload_error}", exc_info=True)
            raise Exception(f"Failed to sync files to backend: {upload_error}")

        # 4. Parse response
        if not response_text or not response_text.strip():
            raise Exception("Backend returned empty response")

        try:
            response_data = json.loads(response_text)
        except json.JSONDecodeError as json_error:
            logger.error(f"Backend returned non-JSON response: {response_text[:200]}")
            raise Exception(f"Invalid JSON response from backend: {json_error}")

        logger.info(
            f"Files synced: generation_id={response_data.get('generation_id')}  "
            f"workspaces={response_data.get('workspace_ids', [])}"
        )

        # Attach the relative paths so callers can forward them to the run endpoint
        response_data["spec_path"] = relative_spec_path
        response_data["src_dir"] = relative_src_dir
        if relative_outputs_dir is not None:
            response_data["outputs_dir"] = relative_outputs_dir
        return response_data
