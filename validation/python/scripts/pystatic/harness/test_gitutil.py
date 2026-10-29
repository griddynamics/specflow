from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pystatic.harness.gitutil import (
    changed_files,
    glob_tracked,
    merge_base,
)

_GIT = shutil.which("git") or "git"


class GitHelperTests(unittest.TestCase):
    def test_merge_base_returns_a_sha_or_none(self) -> None:
        result = merge_base("origin/main")
        if result is not None:
            assert len(result) == 40

    def test_changed_files_returns_a_set(self) -> None:
        assert isinstance(changed_files(), set)


def _init_repo(root: Path) -> None:
    subprocess.run([_GIT, "init", "-q"], cwd=root, check=True)
    subprocess.run([_GIT, "config", "user.email", "t@t.com"], cwd=root, check=True)
    subprocess.run([_GIT, "config", "user.name", "t"], cwd=root, check=True)


class GlobTrackedTests(unittest.TestCase):
    """`glob_tracked`: tracked and untracked-but-not-ignored files -- git's own
    `.gitignore` engine instead of a hand-rolled excluded-dirs walk, so a
    vendored/build directory can never drift out of sync between `.gitignore`
    and a second, Python-side list."""

    def test_finds_tracked_and_untracked_but_not_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "src").mkdir()
            (root / "src" / "a.sql").write_text("select 1;\n")
            subprocess.run([_GIT, "add", "src/a.sql"], cwd=root, check=True)
            (root / "src" / "b.sql").write_text("select 2;\n")  # untracked, not ignored
            result = glob_tracked("*.sql", cwd=root)
            assert result == ["src/a.sql", "src/b.sql"]

    def test_excludes_a_gitignored_vendored_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / ".gitignore").write_text("node_modules/\n")
            vendored = root / "node_modules"
            vendored.mkdir()
            (vendored / "vendored.sql").write_text("select 1;\n")
            (root / "src").mkdir()
            (root / "src" / "a.sql").write_text("select 1;\n")
            subprocess.run([_GIT, "add", "src/a.sql", ".gitignore"], cwd=root, check=True)
            assert glob_tracked("*.sql", cwd=root) == ["src/a.sql"]

    def test_deleted_but_still_indexed_file_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "a.sql").write_text("select 1;\n")
            subprocess.run([_GIT, "add", "a.sql"], cwd=root, check=True)
            subprocess.run([_GIT, "commit", "-q", "-m", "x"], cwd=root, check=True)
            (root / "a.sql").unlink()
            assert glob_tracked("*.sql", cwd=root) == []

    def test_no_matches_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            assert glob_tracked("*.sql", cwd=root) == []


if __name__ == "__main__":
    unittest.main()
