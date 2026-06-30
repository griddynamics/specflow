"""
Unit tests for commit metadata (git-derived) and component attribution.
"""
import logging
import subprocess
from pathlib import Path

import pytest

from app.services.p10y.p10y_lib import (
    KNOWN_COMPONENTS,
    CommitInfo,
    CodeGenerationMetadata,
    build_code_generation_metadata_from_git,
    _parse_component_from_subject,
    _subject_excluded_from_estimation,
)


def test_code_generation_metadata_str() -> None:
    """String representation lists commit messages."""
    metadata = CodeGenerationMetadata(
        commits=[
            CommitInfo(sha="abc", message="First commit", component=["common"]),
            CommitInfo(sha="def", message="Second commit", component=["backend"]),
        ]
    )
    string_repr = str(metadata)
    assert "First commit" in string_repr
    assert "Second commit" in string_repr
    assert "Code Generation Commit Messages:" in string_repr


def _git_init_with_user(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_build_code_generation_metadata_from_git_skips_skip_prefix(tmp_path: Path) -> None:
    """Commits with SKIP_ subject are omitted from metadata."""
    logger = logging.getLogger("test_commit_meta")
    _git_init_with_user(tmp_path)
    (tmp_path / "a.txt").write_text("a")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "SKIP_initial_user_source"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "b.txt").write_text("b")
    subprocess.run(["git", "add", "b.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "backend_add API route"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    meta = build_code_generation_metadata_from_git(str(tmp_path), logger)
    assert meta is not None
    assert len(meta.commits) == 1
    assert meta.commits[0].component == ["backend"]
    assert meta.commits[0].message == "backend_add API route"


def test_build_code_generation_metadata_from_git_no_underscore_uses_common(tmp_path: Path) -> None:
    """Subject without underscore falls back to component common."""
    logger = logging.getLogger("test_commit_meta2")
    _git_init_with_user(tmp_path)
    (tmp_path / "x.txt").write_text("x")
    subprocess.run(["git", "add", "x.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "noUnderscoreHere"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    meta = build_code_generation_metadata_from_git(str(tmp_path), logger)
    assert meta is not None
    assert meta.commits[0].component == ["common"]


@pytest.mark.asyncio
async def test_load_code_generation_metadata_async_uses_git(tmp_path: Path) -> None:
    from app.services.p10y.p10y_lib import load_code_generation_metadata

    logger = logging.getLogger("test_commit_meta_async")
    _git_init_with_user(tmp_path)
    (tmp_path / "f.txt").write_text("f")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "testing_add unit test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    meta = await load_code_generation_metadata(str(tmp_path), logger)
    assert meta is not None
    assert len(meta.commits) == 1
    assert meta.commits[0].component == ["testing"]


# ---------------------------------------------------------------------------
# _subject_excluded_from_estimation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subject,expected", [
    ("SKIP_initial_user_source", True),
    ("skip_something", True),           # case-insensitive
    ("SKIP_generation_baseline", True),
    ("backend_add feature", False),
    ("common_setup", False),
    ("", True),                         # empty → excluded
    ("   ", True),                      # whitespace-only → excluded
])
def test_subject_excluded_from_estimation(subject: str, expected: bool) -> None:
    assert _subject_excluded_from_estimation(subject) is expected


def test_subject_excluded_logs_debug_for_empty(caplog) -> None:
    logger = logging.getLogger("test_exclude_empty")
    with caplog.at_level(logging.DEBUG, logger="test_exclude_empty"):
        result = _subject_excluded_from_estimation("", logger)
    assert result is True
    assert "Empty commit subject" in caplog.text


# ---------------------------------------------------------------------------
# _parse_component_from_subject
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subject,expected_component", [
    ("backend_implement JWT", ["backend"]),
    ("frontend_add login form", ["frontend"]),
    ("BACKEND_uppercase token", ["backend"]),     # component lowercased
    ("common_setup project", ["common"]),
    ("_no_component_before_underscore", ["common"]),  # empty token → common
    ("no_underscore_at_all", ["no"]),               # first underscore splits
])
def test_parse_component_from_subject_known(subject: str, expected_component: list) -> None:
    logger = logging.getLogger("test_parse_comp")
    result = _parse_component_from_subject(subject, logger)
    assert result == expected_component


def test_parse_component_no_underscore_logs_warning(caplog) -> None:
    logger = logging.getLogger("test_parse_no_underscore")
    with caplog.at_level(logging.WARNING, logger="test_parse_no_underscore"):
        result = _parse_component_from_subject("noUnderscoreHere", logger)
    assert result == ["common"]
    assert "no underscore component prefix" in caplog.text


def test_parse_component_unknown_token_logs_warning(caplog) -> None:
    logger = logging.getLogger("test_parse_unknown")
    with caplog.at_level(logging.WARNING, logger="test_parse_unknown"):
        result = _parse_component_from_subject("typo_implement thing", logger)
    assert result == ["typo"]
    assert "unknown component token" in caplog.text


def test_parse_component_known_token_no_warning(caplog) -> None:
    logger = logging.getLogger("test_parse_known")
    with caplog.at_level(logging.WARNING, logger="test_parse_known"):
        _parse_component_from_subject("backend_add feature", logger)
    assert "unknown component token" not in caplog.text


def test_known_components_matches_standards() -> None:
    """KNOWN_COMPONENTS must include the tokens listed in commit_standards.md."""
    expected = {"backend", "frontend", "database", "api", "auth",
                "infrastructure", "testing", "documentation", "pipeline", "ml", "common"}
    assert expected == set(KNOWN_COMPONENTS)


# ---------------------------------------------------------------------------
# build_code_generation_metadata_from_git — edge cases
# ---------------------------------------------------------------------------

def test_build_metadata_not_a_directory(tmp_path: Path) -> None:
    """Non-existent path returns None."""
    logger = logging.getLogger("test_meta_no_dir")
    result = build_code_generation_metadata_from_git(str(tmp_path / "nonexistent"), logger)
    assert result is None


def test_build_metadata_not_a_git_repo(tmp_path: Path) -> None:
    """Existing directory without .git returns None."""
    logger = logging.getLogger("test_meta_no_git")
    result = build_code_generation_metadata_from_git(str(tmp_path), logger)
    assert result is None


def test_build_metadata_empty_repo_returns_none(tmp_path: Path) -> None:
    """Git repo with no commits returns None (git log produces no output)."""
    logger = logging.getLogger("test_meta_empty")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    result = build_code_generation_metadata_from_git(str(tmp_path), logger)
    assert result is None


def test_build_metadata_all_skipped_returns_none(tmp_path: Path) -> None:
    """Git repo whose only commits are SKIP_* returns None."""
    logger = logging.getLogger("test_meta_all_skip")
    _git_init_with_user(tmp_path)
    (tmp_path / "a.txt").write_text("a")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "SKIP_initial_user_source"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "SKIP_generation_baseline"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    result = build_code_generation_metadata_from_git(str(tmp_path), logger)
    assert result is None


def test_build_metadata_two_baseline_commits_excluded(tmp_path: Path) -> None:
    """Both SKIP_initial_user_source and SKIP_generation_baseline are excluded; agent commits included."""
    logger = logging.getLogger("test_meta_two_skip")
    _git_init_with_user(tmp_path)
    # seed
    (tmp_path / "src.txt").write_text("source")
    subprocess.run(["git", "add", "src.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "SKIP_initial_user_source"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    # baseline
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "SKIP_generation_baseline"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    # agent commits
    (tmp_path / "api.py").write_text("api")
    subprocess.run(["git", "add", "api.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "api_add health endpoint"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    (tmp_path / "db.py").write_text("db")
    subprocess.run(["git", "add", "db.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "database_create users table"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    meta = build_code_generation_metadata_from_git(str(tmp_path), logger)
    assert meta is not None
    assert len(meta.commits) == 2
    subjects = [c.message for c in meta.commits]
    assert "api_add health endpoint" in subjects
    assert "database_create users table" in subjects
    assert all(not s.upper().startswith("SKIP_") for s in subjects)
