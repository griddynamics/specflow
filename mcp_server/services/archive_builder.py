"""
Archive builder service for creating project archives.

Handles creating tar.gz archives from specification directories,
source directories, and outputs directories (selective inclusion only).
"""

import io
import logging
import os
import tarfile
from pathlib import Path
from typing import List, Optional

from services.file_sync import load_exclusion_patterns, should_exclude


logger = logging.getLogger(__name__)


def _common_ancestor(p1: Path, p2: Path) -> Path:
    """Return the longest common ancestor of two absolute paths."""
    common = []
    for a, b in zip(p1.parts, p2.parts):
        if a == b:
            common.append(a)
        else:
            break
    if len(common) <= 1:
        raise ValueError(
            f"Paths share no meaningful common ancestor (would be filesystem root): "
            f"{p1}, {p2}. Ensure spec_dir and src_dir are in the same project."
        )
    return Path(*common)


class ArchiveBuilder:
    """Build archives for specification analysis and generation runs."""

    @staticmethod
    def _add_dir_to_tar(
        tar: tarfile.TarFile,
        dir_path: Path,
        arcname_prefix: str,
        patterns: Optional[List[str]] = None,
    ) -> None:
        """Walk dir_path and add files to tar under arcname_prefix/, skipping hidden entries and
        any path matching ``patterns`` (build artifacts/deps/caches). Matching is relative to
        dir_path, so a selected dir whose own prefix matches a pattern (e.g. outputs at build/docs)
        is still archived — only junk *inside* it is filtered.
        """
        patterns = patterns or []
        prefix = Path(arcname_prefix)
        for root, dirs, files in os.walk(dir_path):
            kept_dirs = []
            for d in dirs:
                if d.startswith("."):
                    continue
                rel_dir = (Path(root) / d).relative_to(dir_path)
                if should_exclude(f"{rel_dir}/", patterns):
                    continue
                kept_dirs.append(d)
            dirs[:] = kept_dirs
            for filename in files:
                if filename.startswith("."):
                    continue
                full_path = Path(root) / filename
                rel = full_path.relative_to(dir_path)
                if should_exclude(str(rel), patterns):
                    continue
                tar.add(full_path, arcname=str(prefix / rel))

    @staticmethod
    def create_spec_archive(spec_dir: Path, spec_dir_name: str) -> bytes:
        """
        Create archive with only specification files.

        Args:
            spec_dir: Path to specification directory
            spec_dir_name: Directory name to use in archive (e.g., "specs")

        Returns:
            Archive content as bytes
        """
        patterns = load_exclusion_patterns(spec_dir)
        archive_buffer = io.BytesIO()
        with tarfile.open(fileobj=archive_buffer, mode="w:gz") as tar:
            ArchiveBuilder._add_dir_to_tar(tar, spec_dir, spec_dir_name, patterns)
        archive_buffer.seek(0)
        return archive_buffer.read()

    @staticmethod
    def create_archive_for_analysis(
        spec_dir: Path,
        src_dir: Path,
        outputs_dir: Optional[Path] = None,
    ) -> tuple[bytes, bool, str, str, Optional[str]]:
        """
        Create archive for specification analysis or generation run.

        Both spec_dir and src_dir must be absolute. When src_dir exists, the archive
        root is set to the common ancestor of spec_dir and src_dir so that both land
        at their natural relative positions. When src_dir is absent, only spec files
        are archived under spec_dir.name/.

        If outputs_dir is provided and exists, it's also included in the archive
        (for user-edited plans or analysis outputs).

        Args:
            spec_dir: Absolute path to the specification directory.
            src_dir: Absolute path to the source directory (may not exist).
            outputs_dir: Optional absolute path to outputs directory (e.g., specflow/).

        Returns:
            (archive_data, has_src, relative_spec_path, relative_src_dir, relative_outputs_dir)
            - relative_spec_path: spec location within the archive (e.g. "specs")
            - relative_src_dir:   src location within the archive when has_src=True
              (e.g. "src"); a placeholder directory name when has_src=False — callers
              must check has_src before treating this as a meaningful archive path.
            - relative_outputs_dir: outputs location within the archive (e.g. "docs" or
              "sub/docs"), or None when no outputs dir was archived. This is the path the
              backend must use to locate analysis/planning artifacts — it can differ from
              ``outputs_dir.name`` when outputs_dir is nested below the archive root.
              All paths are safe to pass directly to the backend as workspace-relative.
        """
        has_src = src_dir.exists() and src_dir.is_dir()
        outputs_path = (
            outputs_dir
            if outputs_dir is not None and outputs_dir.exists() and outputs_dir.is_dir()
            else None
        )
        logger.info(
            f"src_dir={src_dir}  has_src={has_src}  "
            f"outputs_dir={outputs_dir}  has_outputs={outputs_path is not None}"
        )

        if has_src or outputs_path is not None:
            dirs_to_include = [spec_dir]
            if has_src:
                dirs_to_include.append(src_dir)
            if outputs_path is not None:
                dirs_to_include.append(outputs_path)

            # Common ancestor is NOT used as the archive root (we archive dirs
            # selectively), but we still need it to compute stable workspace-relative
            # arcname prefixes (relative_to) that the backend uses as path hints.
            project_dir = dirs_to_include[0]
            for d in dirs_to_include[1:]:
                project_dir = _common_ancestor(project_dir, d)

            relative_spec_path = str(spec_dir.relative_to(project_dir))
            # When src is absent it won't appear in the archive; return its bare name
            # as a placeholder so the return tuple shape stays consistent for callers.
            relative_src_dir = str(src_dir.relative_to(project_dir)) if has_src else src_dir.name
            logger.info(
                f"Common ancestor (project_dir): {project_dir}  "
                f"spec={relative_spec_path}  src={relative_src_dir}"
            )

            # Archive only the three specific directories — never the whole project
            # root (avoids including .git and other large unrelated content).
            selected_dirs: list[tuple[Path, str]] = [(spec_dir, relative_spec_path)]
            if has_src:
                selected_dirs.append((src_dir, relative_src_dir))
            relative_outputs_dir: Optional[str] = None
            if outputs_path is not None:
                relative_outputs_dir = str(outputs_path.relative_to(project_dir))
                selected_dirs.append((outputs_path, relative_outputs_dir))

            # .gitignore is read from the project root (common ancestor) on top of the
            # built-in defaults, so user-ignored junk under the selected dirs is dropped too.
            patterns = load_exclusion_patterns(project_dir)
            archive_buffer = io.BytesIO()
            with tarfile.open(fileobj=archive_buffer, mode="w:gz") as tar:
                for dir_path, arcname_prefix in selected_dirs:
                    ArchiveBuilder._add_dir_to_tar(tar, dir_path, arcname_prefix, patterns)
            archive_buffer.seek(0)
            archive_data = archive_buffer.read()

            logger.info(
                f"Selective archive: {len(archive_data)} bytes  "
                f"spec={relative_spec_path}  src={relative_src_dir}  outputs={relative_outputs_dir}"
            )
            return archive_data, has_src, relative_spec_path, relative_src_dir, relative_outputs_dir
        else:
            spec_dir_name = spec_dir.name
            archive_data = ArchiveBuilder.create_spec_archive(spec_dir, spec_dir_name)
            logger.info(f"Spec-only archive: {len(archive_data)} bytes  spec={spec_dir_name}")
            return archive_data, False, spec_dir_name, src_dir.name, None
