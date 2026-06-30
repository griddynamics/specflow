"""
Durable output protection after code generation / deploy (STEEL XI).

Single responsibility: flush outstanding git work, push ``archive/{generation_id}``,
and snapshot workspaces to the artifact store. Used only from the generation
workflow (`run_archive_outputs_step`); P10Y workflows must not call this layer.
"""
from __future__ import annotations

from pathlib import Path

from app.services.artifact_store import ArtifactStore
from app.services.git_archive_service import GitArchiveService
from app.schemas.workspace import WorkspaceSettings
from app.services.workspace_manager import WorkspaceManager


async def protect_workspace_after_generation(
    workspace: WorkspaceSettings,
    *,
    generation_id: str,
    workspace_manager: WorkspaceManager,
    git_archive: GitArchiveService,
    artifact_store: ArtifactStore,
) -> None:
    """
    Idempotent-friendly sequence per workspace: commit+push, git archive branch, artifact copy.

    ``commit_and_push_outstanding`` no-ops when clean; ``archive_branch`` / ``archive_workspace``
    are safe to repeat for the same HEAD / paths.
    """
    wid = workspace.name
    if not wid:
        raise ValueError("protect_workspace_after_generation: workspace.name is required")

    msg = f"[{generation_id}] Post-generation output protection"
    await workspace_manager.commit_and_push_outstanding(workspace, msg)

    ok = await git_archive.archive_branch(wid, generation_id)
    if not ok:
        raise RuntimeError(
            f"Git archive branch failed for workspace {wid} (generation {generation_id})"
        )

    await artifact_store.archive_workspace(
        generation_id,
        wid,
        Path(workspace.workspace_path),
    )
