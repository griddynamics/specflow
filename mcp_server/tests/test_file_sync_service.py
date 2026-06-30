"""Unit tests for FileSyncService exclusion + collection logic (services/file_sync.py)."""

import tarfile

import pytest

from services.file_sync import FileSyncService, load_exclusion_patterns


def _touch(path, content: bytes = b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class TestExclusionPatterns:
    def test_default_excludes_git_and_node_modules_dirs(self, tmp_path):
        svc = FileSyncService(str(tmp_path))
        assert svc._should_exclude(".git/") is True
        assert svc._should_exclude("node_modules/") is True
        assert svc._should_exclude("src/app.py") is False

    def test_gitignore_patterns_are_loaded(self, tmp_path):
        (tmp_path / ".gitignore").write_text("# comment\n*.log\nbuild/\n\n")
        svc = FileSyncService(str(tmp_path))
        assert svc._should_exclude("server.log") is True
        assert svc._should_exclude("build/") is True
        # Comments and blank lines are not turned into patterns.
        assert "# comment" not in svc.exclusion_patterns
        assert "" not in svc.exclusion_patterns

    def test_directory_segment_match_excludes_nested_files(self, tmp_path):
        (tmp_path / ".gitignore").write_text("dist/\n")
        svc = FileSyncService(str(tmp_path))
        # `dist/` should match a file nested under a dist directory.
        assert svc._should_exclude("frontend/dist/main.js") is True

    def test_match_all_and_negation_lines_are_skipped(self, tmp_path):
        """H1: a bare '*' (or '!' negation) must not become an exclusion pattern."""
        (tmp_path / ".gitignore").write_text("*\n!keep.txt\n**\n*.*\nbuild/\n")
        patterns = load_exclusion_patterns(tmp_path)
        assert "*" not in patterns
        assert "**" not in patterns
        assert "*.*" not in patterns
        assert "!keep.txt" not in patterns
        # Legitimate patterns still load.
        assert "build/" in patterns


class TestCollectFiles:
    def test_collects_non_excluded_skips_git(self, tmp_path):
        _touch(tmp_path / "specs" / "spec.md")
        _touch(tmp_path / "src" / "main.py")
        _touch(tmp_path / ".git" / "HEAD", b"ref: x")
        _touch(tmp_path / "debug.log")
        (tmp_path / ".gitignore").write_text("*.log\n")

        svc = FileSyncService(str(tmp_path))
        rels = {f["relative_path"] for f in svc.collect_files()}

        assert "specs/spec.md" in rels
        assert "src/main.py" in rels
        assert not any(r.startswith(".git/") for r in rels)
        assert "debug.log" not in rels

    def test_skips_files_over_max_file_size(self, tmp_path, monkeypatch):
        _touch(tmp_path / "small.txt", b"ok")
        _touch(tmp_path / "big.bin", b"x" * 100)
        monkeypatch.setattr(FileSyncService, "MAX_FILE_SIZE", 10)

        svc = FileSyncService(str(tmp_path))
        rels = {f["relative_path"] for f in svc.collect_files()}

        assert "small.txt" in rels
        assert "big.bin" not in rels

    def test_raises_when_total_size_exceeds_archive_limit(self, tmp_path, monkeypatch):
        for i in range(3):
            _touch(tmp_path / f"f{i}.bin", b"x" * 50)
        monkeypatch.setattr(FileSyncService, "MAX_ARCHIVE_SIZE", 60)

        svc = FileSyncService(str(tmp_path))
        with pytest.raises(ValueError, match="Total size too large"):
            svc.collect_files()


class TestCreateArchive:
    def test_archive_contains_collected_files(self, tmp_path):
        _touch(tmp_path / "specs" / "spec.md", b"# spec")
        _touch(tmp_path / ".git" / "HEAD", b"ref")

        svc = FileSyncService(str(tmp_path))
        buf = svc.create_archive()

        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            names = tar.getnames()

        assert "specs/spec.md" in names
        assert not any(n.startswith(".git/") for n in names)
