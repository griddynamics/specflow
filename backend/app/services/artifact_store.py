"""
ArtifactStore — durable archive of generation outputs, workspace snapshots, and analysis.

Archive layout under ARTIFACTS_BASE_PATH/{generation_id}/:
  {workspace_id}/     full workspace snapshot (source code, minus build artifacts/caches)
  analysis/           spec analysis outputs (completeness report, index, etc.)
  report/             combined multi-workspace comparison report

Public methods:
  archive_workspace(generation_id, workspace_id, workspace_root_path)
      Copy full workspace filesystem to artifacts/{generation_id}/{workspace_id}/.
      Excludes build artifacts/caches per settings.EXCLUDED_ARTIFACT_PATTERNS.
      Idempotent. Does NOT update Firestore.

  archive_analysis(generation_id, outputs_dir_path)
      Copy spec analysis outputs to artifacts/{generation_id}/analysis/.
      Idempotent. Does NOT update Firestore.

  archive_report(generation_id, report_source_path)
      Copy combined multi-workspace report to artifacts/{generation_id}/report/.
      Accepts either a file or a directory. Idempotent. Does NOT update Firestore.

  build_tarball(generation_id)
      Package artifacts/{generation_id}/ as a tar.gz for download.
      Requires outputs_archived=True OR emergency_archived=True on the Firestore document.

Two distinct archive flags
--------------------------
outputs_archived  — set ONLY by GenerationSessionStateMachine.advance_checkpoint(OUTPUTS_ARCHIVED)
                    after the generation workflow completes. Guards complete().
emergency_archived — set by emergency_archive() on every call (re-runnable). Enables
                    download of partial/failed generation snapshots without blocking
                    complete() or conflating partial snapshots with full archives.
"""
import datetime
import shutil
import tarfile
import logging
from pathlib import Path

from app.core.artifact_subdirs import ANALYSIS_SUBDIR, PLANNING_SUBDIR, REPORT_SUBDIR
from app.core.config import settings
from app.prompts.agents_claude_code import PARTIAL_OUTPUT_WARNING_FILE
from app.schemas.generation_workflow_enums import WorkspaceStatus

logger = logging.getLogger(__name__)

# Module-level constant derived from config so callers can import it.
# Using a property-like approach via a function would be cleaner, but this
# module constant is imported by generation.py — keep it compatible.
ARTIFACTS_BASE = Path(settings.ARTIFACTS_BASE_PATH)


class ArtifactStore:

    def __init__(self, db):
        self._db = db

    # ------------------------------------------------------------------
    # New namespaced archive methods
    # ------------------------------------------------------------------

    async def archive_workspace(
        self,
        generation_id: str,
        workspace_id: str,
        workspace_root_path: str | Path,
    ) -> Path:
        """
        Copy the full workspace filesystem snapshot to the artifact store,
        excluding build artifacts and dependency caches defined in
        settings.EXCLUDED_ARTIFACT_PATTERNS.

        Destination: ARTIFACTS_BASE / generation_id / workspace_id /
        Idempotent: calling twice with the same args overwrites (dirs_exist_ok=True).
        Does NOT update Firestore — the caller is responsible for that.
        """
        src = Path(workspace_root_path)
        dst = ARTIFACTS_BASE / generation_id / workspace_id

        if not src.exists():
            raise FileNotFoundError(
                f"archive_workspace: workspace path does not exist: {src}"
            )

        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            src, dst,
            ignore=shutil.ignore_patterns(*settings.EXCLUDED_ARTIFACT_PATTERNS),
            dirs_exist_ok=True,
        )
        logger.info(
            "generation %s workspace %s archived to %s",
            generation_id, workspace_id, dst,
        )
        return dst

    async def _archive_outputs(
        self,
        generation_id: str,
        src: Path,
        subdir: str,
    ) -> Path:
        """
        Copy output directory to artifact store subdirectory.
        Shared helper for archive_analysis and archive_planning.
        """
        dst = ARTIFACTS_BASE / generation_id / subdir
        if not src.exists():
            logger.warning(
                "_archive_outputs: source does not exist: %s — skipping", src
            )
            return dst
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        logger.info(
            "generation %s %s outputs archived to %s", generation_id, subdir, dst
        )
        return dst

    async def archive_analysis(
        self,
        generation_id: str,
        outputs_dir_path: str | Path,
    ) -> Path:
        """
        Copy spec analysis outputs to the artifact store.

        Destination: ARTIFACTS_BASE / generation_id / "analysis" /
        Idempotent. Does NOT update Firestore.
        If the source directory does not exist, logs a warning and returns the
        (not-yet-created) destination path without raising — analysis archival
        is best-effort.
        """
        return await self._archive_outputs(
            generation_id, Path(outputs_dir_path), ANALYSIS_SUBDIR
        )

    async def archive_planning(
        self,
        generation_id: str,
        outputs_dir_path: str | Path,
    ) -> Path:
        """
        Copy planning outputs (IMPLEMENTATION_PLAN.md, e2e-test-plan.md, etc.)
        to the artifact store.

        Destination: ARTIFACTS_BASE / generation_id / "planning" /
        Idempotent. Does NOT update Firestore.
        """
        return await self._archive_outputs(
            generation_id, Path(outputs_dir_path), PLANNING_SUBDIR
        )

    async def archive_report(
        self,
        generation_id: str,
        report_source_path: str | Path,
    ) -> Path:
        """
        Copy the combined multi-workspace comparison report to the artifact store.
        Accepts either a single file or a directory.

        Destination: ARTIFACTS_BASE / generation_id / "report" /
        Idempotent. Does NOT update Firestore.
        If the source does not exist, logs a warning and returns without raising.
        """
        src = Path(report_source_path)
        dst = ARTIFACTS_BASE / generation_id / REPORT_SUBDIR

        if not src.exists():
            logger.warning(
                "archive_report: source path does not exist: %s — skipping", src
            )
            return dst

        dst.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst / src.name)
        logger.info(
            "generation %s combined report archived to %s", generation_id, dst
        )
        return dst

    # ------------------------------------------------------------------
    # Emergency archive (used by download endpoint for FAILED generations)
    # ------------------------------------------------------------------

    async def emergency_archive(
        self,
        generation_id: str,
        workspace_base_path: str | Path | None = None,
    ) -> dict:
        """
        Copy workspace filesystems to the artifact store for a FAILED or stuck
        RUNNING generation where the normal archive_outputs step never ran.

        Re-runnable: may be called multiple times; each call overwrites the
        previous snapshot with fresher workspace content. Sets emergency_archived=True
        (never outputs_archived — that flag is reserved for the generation workflow
        via GenerationSessionStateMachine.advance_checkpoint(OUTPUTS_ARCHIVED)).

        Skip guard: if outputs_archived is already True (full generation archive),
        returns immediately — emergency snapshots must not overwrite a complete archive.

        Robust: a workspace that is empty, missing, CLEANING, or raises OSError
        is recorded as a warning and skipped — it does not abort other workspaces.

        Does NOT release workspaces and does NOT advance the checkpoint.
        Writes PARTIAL_OUTPUT_WARNING.txt at the root of the artifact directory.

        Returns:
            dict with key "workspace_warnings": list[str] — per-workspace issues.

        Raises:
            RuntimeError: if no workspace could be archived at all.
        """
        doc = await self._db.get_generation_session(generation_id)
        if doc.get("outputs_archived"):
            # Full generation archive exists — do not overwrite with a partial snapshot.
            return {"workspace_warnings": doc.get("partial_archive_warnings", [])}

        workspace_ids = doc.get("workspace_ids", [])
        if not workspace_ids:
            raise RuntimeError(
                f"emergency_archive: generation {generation_id} has no workspace_ids"
            )

        if workspace_base_path is None:
            base = Path(settings.WORKSPACE_BASE_PATH)
        else:
            base = Path(workspace_base_path)

        est_status = doc.get("status", "unknown")
        checkpoint = doc.get("checkpoint", "unknown")
        timestamp = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")

        workspace_warnings: list[str] = []
        archived_count = 0

        for ws_id in workspace_ids:
            ws_doc = await self._db.get_workspace(ws_id)
            ws_status = ws_doc.get("status", "unknown")
            if ws_status != WorkspaceStatus.ALLOCATED.value:
                msg = (
                    f"workspace {ws_id}: status={ws_status} (expected allocated) — skipped"
                )
                workspace_warnings.append(msg)
                logger.warning("emergency_archive[%s]: %s", generation_id, msg)
                continue

            workspace_root = base / ws_id
            try:
                is_non_empty = workspace_root.exists() and any(workspace_root.iterdir())
            except OSError:
                is_non_empty = False

            if not is_non_empty:
                msg = (
                    f"workspace {ws_id}: filesystem root missing or empty "
                    f"at {workspace_root} — skipped"
                )
                workspace_warnings.append(msg)
                logger.warning("emergency_archive[%s]: %s", generation_id, msg)
                continue

            try:
                await self.archive_workspace(generation_id, ws_id, workspace_root)
                archived_count += 1
            except OSError as exc:
                msg = f"workspace {ws_id}: OSError during archive — {exc}"
                workspace_warnings.append(msg)
                logger.warning("emergency_archive[%s]: %s", generation_id, msg)

        if archived_count == 0:
            raise RuntimeError(
                f"emergency_archive: could not archive any workspace for generation "
                f"{generation_id}. Warnings: {workspace_warnings}"
            )

        artifact_dir = ARTIFACTS_BASE / generation_id
        artifact_dir.mkdir(parents=True, exist_ok=True)

        warning_lines = [
            "PARTIAL OUTPUT WARNING",
            "======================",
            "",
            "This archive was created from an incomplete or failed generation.",
            f"Generation status : {est_status}",
            f"Last checkpoint   : {checkpoint}",
            f"Archived at       : {timestamp}",
            "",
        ]
        if workspace_warnings:
            warning_lines.append("Workspace issues:")
            for w in workspace_warnings:
                warning_lines.append(f"  - {w}")
        else:
            warning_lines.append("All available workspaces archived without issues.")
        (artifact_dir / PARTIAL_OUTPUT_WARNING_FILE).write_text(
            "\n".join(warning_lines) + "\n", encoding="utf-8"
        )

        await self._db.update_generation_session(generation_id, {
            "emergency_archived": True,
            "artifact_path": str(artifact_dir),
            "partial_archive_warnings": workspace_warnings,
        })

        logger.info(
            "emergency_archive[%s]: archived %d/%d workspaces, warnings=%d",
            generation_id, archived_count, len(workspace_ids), len(workspace_warnings),
        )

        return {"workspace_warnings": workspace_warnings}

    # ------------------------------------------------------------------
    # Tarball builder (used by download endpoint)
    # ------------------------------------------------------------------

    async def build_tarball(self, generation_id: str) -> Path:
        """
        Build a tar.gz from the archived artifact directory for download.
        Used by GET /generation-sessions/{id}/outputs.

        Tarball layout:
          {generation_id}/
            {workspace_id}/     workspace snapshot (source code + outputs)
            analysis/           spec analysis outputs (if present)
            report/             combined generation report (if present)

        Requires outputs_archived=True OR emergency_archived=True on the Firestore document.
        """
        doc = await self._db.get_generation_session(generation_id)
        if not doc.get("outputs_archived") and not doc.get("emergency_archived"):
            raise ValueError(
                f"generation {generation_id} outputs have not been archived yet"
            )

        artifact_path_str = doc.get("artifact_path")
        if not artifact_path_str:
            raise ValueError(
                f"generation {generation_id} has no artifact_path recorded "
                f"— archive may not have run via ArtifactStore"
            )
        artifact_path = Path(artifact_path_str)
        if not artifact_path.exists():
            raise FileNotFoundError(
                f"Artifact directory not found: {artifact_path}"
            )

        return self._build_tarball_from_path(generation_id, artifact_path)

    async def build_tarball_from_dir(self, generation_id: str) -> Path | None:
        """
        Build a tar.gz from available artifact subdirectories (analysis/, planning/).
        Used by GET /generation-sessions/{id}/outputs when outputs_archived=False.

        Files are added with their subdirectory prefix preserved so that extraction
        mirrors the backend layout — e.g. analysis/specification_index.md lands at
        outputs_dir/analysis/specification_index.md.

        Returns None if neither directory exists or both are empty.
        """
        artifact_base = ARTIFACTS_BASE / generation_id
        subdirs = [ANALYSIS_SUBDIR, PLANNING_SUBDIR]

        collected: list[tuple[Path, str]] = []
        for subdir_name in subdirs:
            subdir = artifact_base / subdir_name
            for file_path in subdir.rglob("*"):  # returns [] if subdir missing
                if file_path.is_file():
                    arcname = str(file_path.relative_to(artifact_base))
                    collected.append((file_path, arcname))

        if not collected:
            return None

        tarball_path = ARTIFACTS_BASE / f"{generation_id}.tar.gz"
        try:
            with tarfile.open(tarball_path, "w:gz") as tar:
                for file_path, arcname in collected:
                    tar.add(file_path, arcname=arcname)
            return tarball_path
        except Exception:
            tarball_path.unlink(missing_ok=True)
            raise

    def _build_tarball_from_path(self, generation_id: str, artifact_path: Path) -> Path:
        tarball_path = ARTIFACTS_BASE / f"{generation_id}.tar.gz"
        try:
            with tarfile.open(tarball_path, "w:gz") as tar:
                for file_path in artifact_path.rglob("*"):
                    if file_path.is_file():
                        tar.add(file_path, arcname=str(file_path.relative_to(artifact_path)))
            return tarball_path
        except Exception:
            # Clean up partial tarball on failure before re-raising
            tarball_path.unlink(missing_ok=True)
            raise
