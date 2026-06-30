"""Tests for tarfile path traversal validation (CVE-2007-4559)."""

import io
import tarfile
from pathlib import Path

import pytest

from app.utils.file_utils import validate_tar_members


def _make_tar_with_member(name: str, is_symlink: bool = False, linkname: str = "") -> tarfile.TarFile:
    """Create an in-memory tar archive with a single member."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=name)
        if is_symlink:
            info.type = tarfile.SYMTYPE
            info.linkname = linkname
        else:
            info.size = 5
        tar.addfile(info, io.BytesIO(b"hello") if not is_symlink else None)
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r:gz")


class TestValidateTarMembers:
    """Test validate_tar_members for path traversal detection."""

    def test_normal_archive_passes(self, tmp_path: Path):
        tar = _make_tar_with_member("src/main.py")
        validate_tar_members(tar, tmp_path)
        tar.close()

    def test_nested_path_passes(self, tmp_path: Path):
        tar = _make_tar_with_member("a/b/c/file.txt")
        validate_tar_members(tar, tmp_path)
        tar.close()

    def test_dotdot_escape_raises(self, tmp_path: Path):
        tar = _make_tar_with_member("../escape.txt")
        with pytest.raises(ValueError, match="Path traversal detected"):
            validate_tar_members(tar, tmp_path)
        tar.close()

    def test_nested_dotdot_escape_raises(self, tmp_path: Path):
        tar = _make_tar_with_member("a/../../escape.txt")
        with pytest.raises(ValueError, match="Path traversal detected"):
            validate_tar_members(tar, tmp_path)
        tar.close()

    def test_absolute_path_raises(self, tmp_path: Path):
        tar = _make_tar_with_member("/etc/passwd")
        with pytest.raises(ValueError, match="Path traversal detected"):
            validate_tar_members(tar, tmp_path)
        tar.close()

    def test_symlink_outside_raises(self, tmp_path: Path):
        tar = _make_tar_with_member("link", is_symlink=True, linkname="/etc/passwd")
        with pytest.raises(ValueError, match="Symlink traversal detected"):
            validate_tar_members(tar, tmp_path)
        tar.close()

    def test_symlink_inside_passes(self, tmp_path: Path):
        tar = _make_tar_with_member("link", is_symlink=True, linkname="src/main.py")
        validate_tar_members(tar, tmp_path)
        tar.close()

    def test_symlink_dotdot_escape_raises(self, tmp_path: Path):
        tar = _make_tar_with_member("link", is_symlink=True, linkname="../secret")
        with pytest.raises(ValueError, match="Symlink traversal detected"):
            validate_tar_members(tar, tmp_path)
        tar.close()
