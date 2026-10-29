"""Self-test for `docref`: GitHub slugs, frontmatter blanking and anchor resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from pystatic.harness.docref import doc_anchor_exists, heading_anchor


@pytest.mark.parametrize(
    ("title", "anchor"),
    [
        ("Validation", "validation"),
        ("No suppressions", "no-suppressions"),
        ("SQL rules (sqlfluff)", "sql-rules-sqlfluff"),
        ("  Trailing  ", "trailing"),
        ("blocking-in-async", "blocking-in-async"),
    ],
)
def test_heading_anchor_is_the_github_slug(title: str, anchor: str) -> None:
    assert heading_anchor(title) == anchor


def test_existing_heading_resolves(tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text("# Guide\n\n## Error handling\n\ntext\n")
    assert doc_anchor_exists("/guide.md#error-handling", tmp_path) is True


def test_missing_anchor_file_or_shape_does_not_resolve(tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text("# Guide\n")
    assert doc_anchor_exists("/guide.md#nowhere", tmp_path) is False
    assert doc_anchor_exists("/absent.md#guide", tmp_path) is False
    assert doc_anchor_exists("guide.md#guide", tmp_path) is False
    assert doc_anchor_exists("/guide.md", tmp_path) is False


def test_hash_inside_frontmatter_or_a_fence_is_not_a_heading(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text(
        "---\ntitle: x\n# not-a-heading\n---\n# Real\n\n```\n# fenced\n```\n"
    )
    assert doc_anchor_exists("/doc.md#real", tmp_path) is True
    assert doc_anchor_exists("/doc.md#not-a-heading", tmp_path) is False
    assert doc_anchor_exists("/doc.md#fenced", tmp_path) is False


def test_the_kit_readme_holds_every_check_anchor() -> None:
    for anchor in (
        "blocking-in-async",
        "no-dict-schemas",
        "enum-literals",
        "config-literals",
        "registry-files",
        "config-scoping",
        "json-schemas",
        "deployment-files",
        "file-size",
        "source-ownership",
        "runtime-defaults",
        "docs-bundle",
        "package-data",
        "openapi-snapshot",
        "length-limits",
        "dead-code",
        "clone-inputs",
    ):
        assert doc_anchor_exists(f"/README.md#{anchor}") is True, anchor
