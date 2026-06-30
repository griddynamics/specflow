import logging
import os
import tarfile
from datetime import datetime
from pathlib import Path
import shutil

logger = logging.getLogger(__name__)

DT_FORMAT = "%Y%m%d%H%M%S"


def normalize_path_parameter(path: str) -> str:
    """
    Normalize a path parameter to be workspace-relative.
    
    If an absolute path is passed, extracts just the basename.
    If a relative path is passed, strips the leading ./ prefix.
    
    This ensures that paths like "/Users/.../docs" or "/tmp/specs" 
    are normalized to just "docs" or "specs" for use within workspaces.
    
    Args:
        path: Path string (absolute or relative)
        
    Returns:
        Normalized relative path (just the basename for absolute paths)
        
    Examples:
        >>> normalize_path_parameter("/Users/someone/project/docs")
        "docs"
        >>> normalize_path_parameter("./specs")
        "specs"
        >>> normalize_path_parameter("outputs")
        "outputs"
    """
    path_obj = Path(path)
    if path_obj.is_absolute():
        # Extract just the basename for absolute paths
        return path_obj.name
    else:
        # Strip leading ./ for relative paths
        return path.lstrip('./')


def validate_tar_members(tar: tarfile.TarFile, extract_path: Path) -> None:
    """
    Validate all tar archive members for path traversal attacks (CVE-2007-4559).

    Checks that no member would extract outside the target directory via:
    - Absolute paths
    - Relative paths with .. components
    - Symlinks pointing outside the extract directory

    Args:
        tar: Open tarfile to validate
        extract_path: Directory where files will be extracted

    Raises:
        ValueError: If any member would escape the extract directory
    """
    resolved_base = extract_path.resolve()
    base_str = str(resolved_base) + os.sep

    for member in tar.getmembers():
        target = (extract_path / member.name).resolve()
        if target != resolved_base and not str(target).startswith(base_str):
            raise ValueError(f"Path traversal detected in archive member: {member.name}")

        if member.issym() or member.islnk():
            link_target = (extract_path / member.linkname).resolve()
            if link_target != resolved_base and not str(link_target).startswith(base_str):
                raise ValueError(f"Symlink traversal detected in archive member: {member.name}")


def tar_member_is_under_dot_git(member_name: str) -> bool:
    """True if path has a segment `.git` (not `.github`). Used when extracting workspace sync archives."""
    return any(part == ".git" for part in Path(member_name).parts)


def tar_member_is_under_prefix(member_name: str, prefix: str) -> bool:
    """True if the member path is the prefix dir itself or lives under it (segment-wise)."""
    prefix_parts = Path(prefix).parts
    member_parts = Path(member_name).parts
    return member_parts[: len(prefix_parts)] == prefix_parts


def extract_workspace_sync_archive(
    tar: tarfile.TarFile,
    extract_path: Path,
    only_under: str | None = None,
) -> list[str]:
    """Extract sync archive into workspace; skip `.git/` so managed clone refs are not overwritten.

    When ``only_under`` is given, only members at or below that archive-relative path are
    extracted. The pre-allocation contract preflight uses this to materialize just the
    ``outputs_dir`` markdown it inspects, instead of decompressing the whole archive
    (specs/ + src/) into a throwaway temp dir. All members are still validated for path
    traversal regardless of the filter.
    """
    validate_tar_members(tar, extract_path)
    skipped: list[str] = []
    for member in tar.getmembers():
        if tar_member_is_under_dot_git(member.name):
            skipped.append(member.name)
            continue
        if only_under is not None and not tar_member_is_under_prefix(member.name, only_under):
            continue
        tar.extract(member, path=extract_path, filter="data")
    if skipped:
        logger.info(
            "Workspace sync: skipped %s tar member(s) under .git/",
            len(skipped),
        )
    return skipped


def read_file(file_path: str) -> str:
    """Read the contents of a file."""
    return Path(file_path).read_text()

def check_file_exists(file_path: str) -> bool:
    """Check if a file exists."""
    return Path(file_path).exists()

def delete_file(file_path: str):
    """Delete a file if it exists."""
    path = Path(file_path)
    if path.exists():
        path.unlink()

def delete_directory(directory_path: str):
    """Delete a directory if it exists."""
    path = Path(directory_path)
    if path.exists():
        shutil.rmtree(path)

def move_file(source_path: str, destination_path: str):
    """Move a file to a new location."""
    source = Path(source_path)
    if source.exists():
        source.replace(destination_path)

def archive_file(file_path: str, archive_directory: str):
    """Archive a file by renaming and moving to archive directory."""
    archive_dir = Path(archive_directory)
    archive_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime(DT_FORMAT)
    file = Path(file_path)
    
    archive_name = f"{file.name}_{timestamp}"
    archive_path = archive_dir / archive_name
    move_file(file_path, str(archive_path))