"""
Tests for file utilities.
"""

import io
import tarfile

import pytest
from pathlib import Path

from app.utils.file_utils import (
    extract_workspace_sync_archive,
    normalize_path_parameter,
    tar_member_is_under_dot_git,
    tar_member_is_under_prefix,
)


def _make_tar(files: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    buf.seek(0)
    return buf


class TestTarMemberDotGitSkip:
    def test_tar_member_is_under_dot_git_root(self):
        assert tar_member_is_under_dot_git(".git/HEAD") is True

    def test_tar_member_github_not_skipped(self):
        assert tar_member_is_under_dot_git(".github/workflows/ci.yml") is False

    def test_extract_workspace_skips_git_keeps_specs(self, tmp_path: Path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, body in (
                ("specs/spec.md", b"# spec"),
                (".git/refs/heads/main", b"ref: bogus\n"),
                (".github/README.md", b"x"),
            ):
                info = tarfile.TarInfo(name=name)
                bio = io.BytesIO(body)
                info.size = len(body)
                tar.addfile(info, bio)
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar_in:
            skipped = extract_workspace_sync_archive(tar_in, tmp_path)

        assert skipped == [".git/refs/heads/main"]
        assert (tmp_path / "specs/spec.md").read_bytes() == b"# spec"
        assert (tmp_path / ".github/README.md").read_bytes() == b"x"
        assert not (tmp_path / ".git/refs/heads/main").exists()


class TestExtractOnlyUnderPrefix:
    """The pre-allocation contract preflight extracts only the outputs_dir markdown,
    not specs/ or src/, to avoid decompressing a large archive into a throwaway dir."""

    def test_only_under_extracts_just_the_prefix_tree(self, tmp_path: Path):
        buf = _make_tar(
            {
                "specs/spec.md": b"# spec",
                "src/main.py": b"print()",
                "docs/analysis/specification_completeness.md": b"# Part F\n\nLOCAL_ONLY\n",
                "docs/planning/IMPLEMENTATION_PLAN.md": b"## Phase 1\n\nwork\n",
            }
        )
        with tarfile.open(fileobj=buf, mode="r:gz") as tar_in:
            extract_workspace_sync_archive(tar_in, tmp_path, only_under="docs")

        assert (tmp_path / "docs/planning/IMPLEMENTATION_PLAN.md").exists()
        assert (tmp_path / "docs/analysis/specification_completeness.md").exists()
        # specs/ and src/ are validated but NOT extracted under the filter.
        assert not (tmp_path / "specs").exists()
        assert not (tmp_path / "src").exists()

    def test_only_under_supports_nested_prefix(self, tmp_path: Path):
        buf = _make_tar(
            {
                "specs/spec.md": b"# spec",
                "build/docs/planning/IMPLEMENTATION_PLAN.md": b"## Phase 1\n\nwork\n",
            }
        )
        with tarfile.open(fileobj=buf, mode="r:gz") as tar_in:
            extract_workspace_sync_archive(tar_in, tmp_path, only_under="build/docs")

        assert (tmp_path / "build/docs/planning/IMPLEMENTATION_PLAN.md").exists()
        assert not (tmp_path / "specs").exists()

    def test_under_prefix_matches_dir_and_descendants_segmentwise(self):
        assert tar_member_is_under_prefix("docs/analysis/x.md", "docs") is True
        assert tar_member_is_under_prefix("docs", "docs") is True
        assert tar_member_is_under_prefix("build/docs/x.md", "build/docs") is True
        # Sibling that merely shares a name prefix must not match.
        assert tar_member_is_under_prefix("docs-old/x.md", "docs") is False


class TestNormalizePathParameter:
    """Test path parameter normalization."""
    
    def test_normalize_absolute_path(self):
        """Test that absolute paths are normalized to basename."""
        result = normalize_path_parameter("/Users/someone/project/docs")
        assert result == "docs"
        
        result = normalize_path_parameter("/tmp/specflow-specs")
        assert result == "specflow-specs"
        
        result = normalize_path_parameter("/var/lib/outputs")
        assert result == "outputs"
    
    def test_normalize_relative_path_with_dot_slash(self):
        """Test that ./ prefix is stripped from relative paths."""
        result = normalize_path_parameter("./specs")
        assert result == "specs"
        
        result = normalize_path_parameter("./outputs/data")
        assert result == "outputs/data"
    
    def test_normalize_simple_relative_path(self):
        """Test that simple relative paths are unchanged."""
        result = normalize_path_parameter("docs")
        assert result == "docs"
        
        result = normalize_path_parameter("specifications")
        assert result == "specifications"
        
        result = normalize_path_parameter("outputs/subfolder")
        assert result == "outputs/subfolder"
    
    def test_normalize_empty_path(self):
        """Test that empty path is handled."""
        result = normalize_path_parameter("")
        assert result == ""
    
    def test_normalize_dot_path(self):
        """Test that . and ./ paths are handled (both become empty string)."""
        result = normalize_path_parameter(".")
        assert result == ""  # lstrip('./') removes the dot
        
        result = normalize_path_parameter("./")
        assert result == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
