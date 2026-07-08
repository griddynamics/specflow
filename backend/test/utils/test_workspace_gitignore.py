"""Tests for workspace .gitignore seeding."""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.utils.workspace_gitignore import (
    WORKSPACE_GITIGNORE_ENTRIES,
    _normalize_pattern,
    ensure_workspace_gitignore,
    read_gitignore_patterns,
)


class TestNormalizePattern:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("node_modules/", "node_modules"),
            ("  .venv/  ", ".venv"),
            ("# comment", ""),
            ("", ""),
            ("*.pyc", "*.pyc"),
        ],
    )
    def test_normalize_pattern(self, line: str, expected: str) -> None:
        assert _normalize_pattern(line) == expected


def test_creates_gitignore_when_missing(tmp_path: Path) -> None:
    changed = ensure_workspace_gitignore(tmp_path)

    assert changed is True
    content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "SpecFlow workspace defaults" in content
    for entry in WORKSPACE_GITIGNORE_ENTRIES:
        assert entry in content
    assert "android-sdk-local/" in content
    assert "cmdline-tools.zip" in content
    assert "setup-sdk.sh" in content


def test_idempotent_when_all_patterns_present(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("\n".join(WORKSPACE_GITIGNORE_ENTRIES) + "\n", encoding="utf-8")

    changed = ensure_workspace_gitignore(tmp_path)

    assert changed is False


def test_appends_missing_patterns_without_removing_user_entries(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("custom-build/\n.env\n", encoding="utf-8")

    changed = ensure_workspace_gitignore(tmp_path)

    assert changed is True
    content = gitignore.read_text(encoding="utf-8")
    assert "custom-build/" in content
    assert ".env" in content
    assert "node_modules/" in content
    assert "SpecFlow defaults" in content


def test_swallows_write_errors_without_raising(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("custom-build/\n", encoding="utf-8")

    with patch.object(
        Path,
        "write_text",
        side_effect=OSError("permission denied"),
    ):
        changed = ensure_workspace_gitignore(tmp_path)

    assert changed is False
    assert gitignore.read_text(encoding="utf-8") == "custom-build/\n"


def test_swallows_read_errors_without_raising(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("custom-build/\n", encoding="utf-8")

    with patch.object(
        Path,
        "read_text",
        side_effect=OSError("permission denied"),
    ):
        changed = ensure_workspace_gitignore(tmp_path)

    assert changed is False


def test_appends_only_missing_entries_when_partially_present(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("node_modules/\n.env\n", encoding="utf-8")

    changed = ensure_workspace_gitignore(tmp_path)

    assert changed is True
    content = gitignore.read_text(encoding="utf-8")
    assert content.count("node_modules/") == 1
    assert ".venv/" in content
    assert ".env" in content


class TestReadGitignorePatterns:
    def test_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        assert read_gitignore_patterns(tmp_path) == []

    def test_parses_simple_and_skips_unsupported(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text(
            "\n".join(
                [
                    "# comment",
                    "",
                    "node_modules/",  # trailing slash -> node_modules
                    ".env",
                    "/dist",  # leading anchor -> dist
                    "*.log",
                    "!keep.log",  # negation -> skipped
                    "src/generated",  # multi-segment -> skipped
                    "**/foo",  # multi-segment -> skipped
                    "*",  # match-all -> skipped
                    "node_modules/",  # duplicate -> deduped
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        assert read_gitignore_patterns(tmp_path) == [
            "node_modules",
            ".env",
            "dist",
            "*.log",
        ]

    def test_swallows_read_errors(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")

        with patch.object(Path, "read_text", side_effect=OSError("boom")):
            assert read_gitignore_patterns(tmp_path) == []
