"""Self-test for `registry_files`: an enum of report kinds, one template per kind."""

from __future__ import annotations

from pathlib import Path

import pytest

from pystatic import config, files
from pystatic.harness.docref import doc_anchor_exists
from pystatic.scripts import registry_files as rule

ENUM_SOURCE = """\
from enum import StrEnum


class ReportKind(StrEnum):
    INVOICE = "INVOICE"
    RECEIPT = "RECEIPT"
    RAW: str = "RAW"
"""


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    """A target tree with a template for every non-excluded `ReportKind` member."""
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "kinds.py").write_text(ENUM_SOURCE)
    directory = tmp_path / "src" / "app" / "templates"
    directory.mkdir()
    for name in ("INVOICE", "RECEIPT"):
        (directory / f"{name}.md").write_text(f"# {name}\n")
    cfg = tmp_path / "pystatic.toml"
    cfg.write_text(
        "[[registry-files]]\n"
        'enum = "src/app/kinds.py:ReportKind"\n'
        'directory = "src/app/templates"\n'
        'exclude = ["RAW"]\n'
    )
    files.configure(tmp_path)
    config.configure(cfg)
    return directory


def test_doc_anchor_resolves() -> None:
    assert doc_anchor_exists(rule.DOC) is True


def test_enum_members_are_read_without_importing(tmp_path: Path) -> None:
    path = tmp_path / "kinds.py"
    path.write_text(ENUM_SOURCE)
    assert rule.enum_members(path, "ReportKind") == {
        "INVOICE": "INVOICE",
        "RECEIPT": "RECEIPT",
        "RAW": "RAW",
    }


@pytest.mark.usefixtures("templates")
def test_one_file_per_member_is_clean() -> None:
    assert rule.run().exit_code == 0


def test_a_member_without_its_file_is_flagged(templates: Path) -> None:
    (templates / "RECEIPT.md").unlink()
    findings = rule.check()
    assert [f.rule for f in findings] == ["registry-files-missing"]
    assert "ReportKind.RECEIPT has no RECEIPT.md" in findings[0].message


@pytest.mark.parametrize(
    "stray",
    ["INVOICE/v1.md", "v2.md", "UNKNOWN.md", "RAW.md"],
    ids=["version-dir", "version-file", "unknown-member", "excluded-member"],
)
def test_a_version_or_stray_file_is_flagged(templates: Path, stray: str) -> None:
    path = templates / stray
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stray\n")
    assert {f.rule for f in rule.check()} == {"registry-files-stray"}


def test_a_missing_class_is_a_configuration_error(templates: Path) -> None:
    (templates.parent / "kinds.py").write_text("VALUE = 1\n")
    with pytest.raises(config.ConfigError, match="no top-level class ReportKind"):
        rule.check()


def test_run_without_entries_is_a_visible_skip(tmp_path: Path) -> None:
    files.configure(tmp_path)
    assert rule.run().detail.startswith("skipped:")
