"""
Unit tests for ArchiveBuilder.

Covers:
- create_spec_archive: basic spec-only archive
- create_archive_for_analysis: all three directory combinations
  (spec+src+outputs, spec+outputs without src, spec-only)
- _add_dir_to_tar: hidden file/directory exclusion
"""

import io
import tarfile
from pathlib import Path

import pytest

from services.archive_builder import ArchiveBuilder, _common_ancestor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tar_names(data: bytes) -> set[str]:
    """Return the set of member names inside a tar.gz blob."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return set(tar.getnames())


def _write(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ---------------------------------------------------------------------------
# _common_ancestor
# ---------------------------------------------------------------------------


def test_common_ancestor_siblings():
    assert _common_ancestor(Path("/a/b/specs"), Path("/a/b/src")) == Path("/a/b")


def test_common_ancestor_nested():
    assert _common_ancestor(Path("/a/b/c/specs"), Path("/a/b/src")) == Path("/a/b")


def test_common_ancestor_no_meaningful_ancestor():
    with pytest.raises(ValueError, match="no meaningful common ancestor"):
        _common_ancestor(Path("/specs"), Path("/src"))


# ---------------------------------------------------------------------------
# create_spec_archive
# ---------------------------------------------------------------------------


def test_create_spec_archive_basic(tmp_path):
    spec_dir = tmp_path / "specs"
    _write(spec_dir / "req.md")
    _write(spec_dir / "sub" / "detail.md")

    data = ArchiveBuilder.create_spec_archive(spec_dir, "specs")
    names = _tar_names(data)

    assert "specs/req.md" in names
    assert "specs/sub/detail.md" in names


def test_create_spec_archive_excludes_hidden(tmp_path):
    spec_dir = tmp_path / "specs"
    _write(spec_dir / "visible.md")
    _write(spec_dir / ".hidden_file")
    _write(spec_dir / ".hidden_dir" / "nested.md")

    names = _tar_names(ArchiveBuilder.create_spec_archive(spec_dir, "specs"))

    assert "specs/visible.md" in names
    assert not any(".hidden" in n for n in names)


# ---------------------------------------------------------------------------
# create_archive_for_analysis — spec + src + outputs
# ---------------------------------------------------------------------------


def test_full_archive_spec_src_outputs(tmp_path):
    project = tmp_path / "project"
    spec_dir = project / "specs"
    src_dir = project / "src"
    outputs_dir = project / "specflow"

    _write(spec_dir / "spec.md")
    _write(src_dir / "main.py")
    _write(outputs_dir / "plan.md")

    data, has_src, rel_spec, rel_src, rel_outputs = ArchiveBuilder.create_archive_for_analysis(
        spec_dir, src_dir, outputs_dir
    )
    names = _tar_names(data)

    assert has_src is True
    assert rel_spec == "specs"
    assert rel_src == "src"
    assert rel_outputs == "specflow"
    assert "specs/spec.md" in names
    assert "src/main.py" in names
    assert "specflow/plan.md" in names


def test_full_archive_excludes_git(tmp_path):
    """The .git directory must never appear in the archive."""
    project = tmp_path / "project"
    spec_dir = project / "specs"
    src_dir = project / "src"

    _write(spec_dir / "spec.md")
    _write(src_dir / "main.py")
    # Simulate a .git directory at the project root (would be pulled in by old impl)
    _write(project / ".git" / "HEAD", "ref: refs/heads/main")

    data, has_src, _, _, _ = ArchiveBuilder.create_archive_for_analysis(spec_dir, src_dir)
    names = _tar_names(data)

    assert has_src is True
    assert not any(".git" in n for n in names), f"Found .git entries: {names}"


def test_full_archive_excludes_dependency_and_build_dirs(tmp_path):
    """node_modules / build / __pycache__ / *.pyc inside selected dirs must not be uploaded."""
    project = tmp_path / "project"
    spec_dir = project / "specs"
    src_dir = project / "src"

    _write(spec_dir / "spec.md")
    _write(src_dir / "main.py")
    _write(src_dir / "node_modules" / "left-pad" / "index.js")
    _write(src_dir / "build" / "bundle.js")
    _write(src_dir / "__pycache__" / "main.cpython-313.pyc")
    _write(src_dir / "app.pyc")

    data, _, _, _, _ = ArchiveBuilder.create_archive_for_analysis(spec_dir, src_dir)
    names = _tar_names(data)

    assert "src/main.py" in names
    assert "specs/spec.md" in names
    assert not any("node_modules" in n for n in names)
    assert not any("/build/" in n or n.endswith("/build") for n in names)
    assert not any("__pycache__" in n for n in names)
    assert not any(n.endswith(".pyc") for n in names)


def test_archive_not_emptied_by_match_all_gitignore(tmp_path):
    """A project .gitignore containing a bare '*' must NOT empty the upload archive."""
    project = tmp_path / "project"
    spec_dir = project / "specs"
    src_dir = project / "src"

    _write(project / ".gitignore", "*\n!keep.txt\n")  # match-all + negation
    _write(spec_dir / "spec.md")
    _write(src_dir / "main.py")

    data, _, _, _, _ = ArchiveBuilder.create_archive_for_analysis(spec_dir, src_dir)
    names = _tar_names(data)

    assert "specs/spec.md" in names
    assert "src/main.py" in names


def test_archive_honors_project_gitignore(tmp_path):
    """Patterns from the project-root .gitignore are excluded on top of the defaults."""
    project = tmp_path / "project"
    spec_dir = project / "specs"
    src_dir = project / "src"

    _write(project / ".gitignore", "secrets.txt\ntmpdir/\n")
    _write(spec_dir / "spec.md")
    _write(src_dir / "main.py")
    _write(src_dir / "secrets.txt", "shh")
    _write(src_dir / "tmpdir" / "scratch.bin")

    data, _, _, _, _ = ArchiveBuilder.create_archive_for_analysis(spec_dir, src_dir)
    names = _tar_names(data)

    assert "src/main.py" in names
    assert not any(n.endswith("secrets.txt") for n in names)
    assert not any("tmpdir" in n for n in names)


# ---------------------------------------------------------------------------
# create_archive_for_analysis — outputs only (no src)
# ---------------------------------------------------------------------------


def test_archive_outputs_without_src(tmp_path):
    """has_outputs=True, has_src=False: spec + outputs archived, src absent."""
    project = tmp_path / "project"
    spec_dir = project / "specs"
    outputs_dir = project / "specflow"
    src_dir = project / "src"  # intentionally does NOT exist

    _write(spec_dir / "spec.md")
    _write(outputs_dir / "plan.md")

    data, has_src, _, rel_src, rel_outputs = ArchiveBuilder.create_archive_for_analysis(
        spec_dir, src_dir, outputs_dir
    )
    names = _tar_names(data)

    assert has_src is False
    # rel_src is a placeholder (src_dir.name) when has_src=False
    assert rel_src == "src"
    assert rel_outputs == "specflow"
    assert "specs/spec.md" in names
    assert "specflow/plan.md" in names
    # src must not appear — it doesn't exist
    assert not any(n.startswith("src/") for n in names)


# ---------------------------------------------------------------------------
# create_archive_for_analysis — spec only
# ---------------------------------------------------------------------------


def test_archive_spec_only(tmp_path):
    spec_dir = tmp_path / "project" / "specs"
    src_dir = tmp_path / "project" / "src"  # does not exist
    _write(spec_dir / "spec.md")

    data, has_src, rel_spec, rel_src, rel_outputs = ArchiveBuilder.create_archive_for_analysis(
        spec_dir, src_dir
    )
    names = _tar_names(data)

    assert has_src is False
    assert rel_spec == "specs"
    assert rel_src == "src"  # placeholder
    assert rel_outputs is None  # no outputs dir archived
    assert "specs/spec.md" in names


def test_archive_spec_only_excludes_hidden(tmp_path):
    spec_dir = tmp_path / "project" / "specs"
    src_dir = tmp_path / "project" / "src"
    _write(spec_dir / "visible.md")
    _write(spec_dir / ".DS_Store")

    data, _, _, _, _ = ArchiveBuilder.create_archive_for_analysis(spec_dir, src_dir)
    names = _tar_names(data)

    assert "specs/visible.md" in names
    assert not any(".DS_Store" in n for n in names)


# ---------------------------------------------------------------------------
# create_archive_for_analysis — nested outputs dir (regression: outputs path
# must be the archive-relative path, not outputs_dir.name)
# ---------------------------------------------------------------------------


def test_nested_outputs_dir_returns_relative_path(tmp_path):
    """A nested outputs_dir (e.g. build/docs) must report its archive-relative path so
    the backend locates analysis/planning artifacts — outputs_dir.name would lose the
    parent segment and break contract validation."""
    project = tmp_path / "project"
    spec_dir = project / "specs"
    outputs_dir = project / "build" / "docs"
    src_dir = project / "src"  # does not exist

    _write(spec_dir / "spec.md")
    _write(outputs_dir / "plan.md")

    data, has_src, rel_spec, _, rel_outputs = ArchiveBuilder.create_archive_for_analysis(
        spec_dir, src_dir, outputs_dir
    )
    names = _tar_names(data)

    assert has_src is False
    assert rel_spec == "specs"
    assert rel_outputs == "build/docs"
    assert "build/docs/plan.md" in names
