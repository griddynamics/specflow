"""
File sync service for creating archives and uploading to backend.
"""

import functools
import io
import json
import logging
import os
import tarfile
from pathlib import Path
from typing import Any, List
import fnmatch

from services.server_instructions import SERVICE_DESCRIPTION, SUPPORTED_CODING_AGENTS
from schemas.gain_json import SpecFlow_JSON_FILENAME, GainJson, GainVersions


logger = logging.getLogger(__name__)

SESSION_FILENAME = "specflow_session.json"


@functools.lru_cache(maxsize=1)
def _canonical_gain_dict() -> dict[str, Any]:
    return GainJson(
        description="SpecFlow project context. This file describes shared context for services.",
        services_description={
            "specflow": SERVICE_DESCRIPTION,
            "rosetta": "Rosetta is a knowledge base for the SpecFlow service. Contains instructions of usage and workflows that utilise SpecFlow",
        },
        coding_agents=SUPPORTED_CODING_AGENTS,
        versions=GainVersions(),
    ).model_dump(mode="json", by_alias=True)


def _merge_gain_json(existing: dict, canonical: dict) -> dict:
    """Apply our schema on top of existing: set known fields; keep every other key and nested key."""
    merged: dict = {**existing}
    merged["description"] = canonical["description"]
    old_sd = existing.get("servicesDescription")
    if not isinstance(old_sd, dict):
        old_sd = {}
    merged["servicesDescription"] = {**old_sd, **canonical["servicesDescription"]}
    if "codingAgents" not in existing:
        merged["codingAgents"] = canonical["codingAgents"]
    old_v = existing.get("versions")
    if not isinstance(old_v, dict):
        old_v = {}
    merged["versions"] = {**old_v, **canonical["versions"]}
    return merged


def ensure_gain_json(project_root: Path) -> None:
    """Create or update gain.json with our canonical fields; preserve extra top-level and nested keys."""
    path = project_root / SpecFlow_JSON_FILENAME
    canonical = _canonical_gain_dict()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        out = canonical
        try:
            path.write_text(json.dumps(out, indent=2))
            logger.info("Created %s at %s", SpecFlow_JSON_FILENAME, project_root)
        except (IOError, OSError) as e:
            logger.error("Failed to write %s: %s", SpecFlow_JSON_FILENAME, e)
        return
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in %s (line %d): %s; not modifying", path, e.lineno, e.msg)
        return

    if not isinstance(raw, dict):
        logger.error("%s: top level must be a JSON object (got %s); not modifying",
                    path, type(raw).__name__)
        return

    out = _merge_gain_json(raw, canonical)
    try:
        path.write_text(json.dumps(out, indent=2))
        logger.debug("Merged %s at %s", SpecFlow_JSON_FILENAME, project_root)
    except (IOError, OSError) as e:
        logger.error("Failed to write %s: %s", SpecFlow_JSON_FILENAME, e)

# Default exclusions (from file-sync-protocol.md)
DEFAULT_EXCLUSIONS = [
    # Version control
    ".git/",
    ".svn/",
    ".hg/",
    # Dependencies
    "node_modules/",
    "venv/",
    ".venv/",
    "env/",
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".mypy_cache/",
    "target/",
    "build/",
    "dist/",
    ".gradle/",
    ".m2/",
    # IDE/Editor
    ".idea/",
    ".vscode/",
    "*.swp",
    "*.swo",
    ".DS_Store",
    "Thumbs.db",
    # Secrets
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.crt",
    "credentials.json",
    "secrets.yaml",
    "*.secrets",
    # SpecFlow session file — local only, never uploaded
    SESSION_FILENAME,
    # Large/binary files
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.rar",
    "*.7z",
    "*.exe",
    "*.dll",
    "*.so",
    "*.dylib",
    # Logs
    "*.log",
    "logs/",
]


# Whole-tree wildcards that would make should_exclude match every path — feeding one of these
# into the upload archive (e.g. a bare "*" from a "!"-allowlist .gitignore) would silently
# produce an empty spec/generation archive. Never honor them.
_GITIGNORE_MATCH_ALL: frozenset[str] = frozenset({"*", "**", "*.*"})


def load_exclusion_patterns(project_root: Path) -> List[str]:
    """DEFAULT_EXCLUSIONS plus safe non-comment lines from ``<project_root>/.gitignore``.

    Skips comments, negations (``!`` — fnmatch can't un-ignore, so honoring them as literals
    would wrongly exclude), and match-all wildcards (would empty the archive).
    """
    patterns = DEFAULT_EXCLUSIONS.copy()
    gitignore = project_root / ".gitignore"
    if gitignore.exists():
        try:
            for raw in gitignore.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith("!"):
                    continue
                if line.rstrip("/") in _GITIGNORE_MATCH_ALL:
                    continue
                patterns.append(line)
        except OSError as exc:
            logger.warning("Could not read .gitignore in %s (non-fatal): %s", project_root, exc)
    return patterns


def should_exclude(relative_path: str, patterns: List[str]) -> bool:
    """True if ``relative_path`` matches any pattern by full path, a directory segment, or filename."""
    path_parts = Path(relative_path).parts
    filename = Path(relative_path).name
    for pattern in patterns:
        if fnmatch.fnmatch(relative_path, pattern):
            return True
        if pattern.endswith("/") and pattern.rstrip("/") in path_parts:
            return True
        if fnmatch.fnmatch(filename, pattern):
            return True
    return False


class FileSyncService:
    """Handles file sync from local machine to cloud backend."""
    
    MAX_ARCHIVE_SIZE = 100 * 1024 * 1024  # 100 MB
    MAX_FILE_SIZE = 10 * 1024 * 1024      # 10 MB
    MAX_FILE_COUNT = 10_000
    
    def __init__(self, project_path: str):
        """
        Initialize file sync service.
        
        Args:
            project_path: Path to local project directory
        """
        self.project_path = Path(project_path).resolve()
        self.exclusion_patterns = self._load_exclusion_patterns()
    
    def _load_exclusion_patterns(self) -> List[str]:
        """Load exclusion patterns from defaults and .gitignore."""
        return load_exclusion_patterns(self.project_path)

    def _should_exclude(self, relative_path: str) -> bool:
        """Check if a file should be excluded."""
        return should_exclude(relative_path, self.exclusion_patterns)
    
    def collect_files(self) -> List[dict]:
        """
        Collect all files to be synced.
        Returns list of {path, size, relative_path}.
        """
        files = []
        total_size = 0
        
        for root, dirs, filenames in os.walk(self.project_path):
            # Filter directories in-place to skip excluded dirs
            dirs[:] = [
                d for d in dirs
                if not self._should_exclude(d + "/")
            ]
            
            for filename in filenames:
                full_path = Path(root) / filename
                relative_path = full_path.relative_to(self.project_path)
                relative_str = str(relative_path)
                
                # Check exclusion
                if self._should_exclude(relative_str):
                    continue
                
                # Check file size
                file_size = full_path.stat().st_size
                if file_size > self.MAX_FILE_SIZE:
                    print(f"Warning: Skipping large file: {relative_str} ({file_size} bytes)")
                    continue
                
                files.append({
                    "path": str(full_path),
                    "relative_path": relative_str,
                    "size": file_size
                })
                total_size += file_size
                
                # Check limits
                if len(files) > self.MAX_FILE_COUNT:
                    raise ValueError(
                        f"Too many files ({len(files)}). Max: {self.MAX_FILE_COUNT}"
                    )
                if total_size > self.MAX_ARCHIVE_SIZE:
                    raise ValueError(
                        f"Total size too large ({total_size} bytes). Max: {self.MAX_ARCHIVE_SIZE}"
                    )
        
        return files
    
    def create_archive(self) -> io.BytesIO:
        """
        Create tar.gz archive of project files.
        Returns in-memory bytes buffer.
        """
        files = self.collect_files()
        
        print(f"Archiving {len(files)} files...")
        
        buffer = io.BytesIO()
        
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for file_info in files:
                tar.add(
                    file_info["path"],
                    arcname=file_info["relative_path"]
                )
        
        buffer.seek(0)
        archive_size = buffer.getbuffer().nbytes
        
        print(f"Archive created: {archive_size / 1024 / 1024:.2f} MB")
        
        return buffer
