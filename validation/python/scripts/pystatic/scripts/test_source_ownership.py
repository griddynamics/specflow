"""Self-test for `source_ownership` over a throwaway git repository."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pystatic import config, files
from pystatic.harness.docref import doc_anchor_exists
from pystatic.scripts import source_ownership as rule

GIT = shutil.which("git") or "git"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """`billing` and `web` are projects; `scripts/` and `README.md` are not."""
    layout = {
        "billing/pyproject.toml": (
            '[tool.source-ownership]\nexternal = ["../scripts/billing_*.py"]\n'
        ),
        "billing/app.py": "VALUE = 1\n",
        "web/main.ts": "export const value = 1;\n",
        "scripts/billing_report.py": "VALUE = 2\n",
        "scripts/stray.sh": "echo stray\n",
        "README.md": "# readme\n",
    }
    for rel, text in layout.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run([GIT, "init", "-q"], cwd=tmp_path, check=True)
    cfg = tmp_path / "pystatic.toml"
    cfg.write_text('[source-ownership]\nprojects = ["billing", "web"]\n')
    files.configure(tmp_path)
    config.configure(cfg)
    return tmp_path


def test_doc_anchor_resolves() -> None:
    assert doc_anchor_exists(rule.DOC) is True


@pytest.mark.usefixtures("repo")
def test_only_sources_outside_every_project_and_undeclared_are_reported() -> None:
    findings = rule.check()
    assert [(f.path, f.rule) for f in findings] == [
        ("scripts/stray.sh", "source-without-validation-owner")
    ]


def test_a_missing_project_is_a_configuration_error(repo: Path) -> None:
    (repo / "pystatic.toml").write_text('[source-ownership]\nprojects = ["gone"]\n')
    with pytest.raises(config.ConfigError, match="do not exist"):
        rule.check()


def test_unowned_sources_honours_the_extension_list(tmp_path: Path) -> None:
    sources = [tmp_path / "a.py", tmp_path / "b.md"]
    assert rule.unowned_sources(tmp_path, sources, [], [".py"]) == [tmp_path / "a.py"]


def test_run_without_a_table_is_a_visible_skip(tmp_path: Path) -> None:
    files.configure(tmp_path)
    assert rule.run().detail.startswith("skipped:")


def _write_config(repo: Path, external: str) -> None:
    (repo / "pystatic.toml").write_text(
        '[source-ownership]\nprojects = ["billing", "web"]\n' + external
    )


def test_a_configured_external_entry_with_a_reason_claims_its_files(repo: Path) -> None:
    _write_config(
        repo,
        '[[source-ownership.external]]\npaths = ["scripts/*.sh"]\nowner = "billing"\n'
        'reason = "billing\'s tests run the deploy scripts"\n',
    )
    assert rule.check() == []


def test_a_recursive_pattern_claims_nested_files(repo: Path) -> None:
    vendored = repo / "vendor" / "lib" / "deep"
    vendored.mkdir(parents=True)
    (vendored / "bundle.js").write_text("export {};\n")
    (repo / "scripts" / "stray.sh").unlink()
    _write_config(
        repo,
        '[[source-ownership.external]]\npaths = ["vendor/**/*"]\n'
        'reason = "vendored read-only third-party build output"\n',
    )
    assert rule.check() == []


def test_an_external_entry_claims_only_what_it_names(repo: Path) -> None:
    (repo / "scripts" / "other.py").write_text("VALUE = 3\n")
    _write_config(
        repo,
        '[[source-ownership.external]]\npaths = ["scripts/stray.sh"]\nreason = "vendored"\n',
    )
    assert [f.path for f in rule.check()] == ["scripts/other.py"]


@pytest.mark.parametrize(
    ("external", "message"),
    [
        ('[[source-ownership.external]]\npaths = ["scripts/stray.sh"]\n', "written `reason`"),
        (
            '[[source-ownership.external]]\npaths = ["scripts/stray.sh"]\nreason = "  "\n',
            "written `reason`",
        ),
        ('[[source-ownership.external]]\npaths = []\nreason = "r"\n', "non-empty `paths`"),
        (
            '[[source-ownership.external]]\npaths = ["scripts/stray.sh"]\nreason = "r"\n'
            'owner = "gone"\n',
            "not one of the configured projects",
        ),
        (
            '[[source-ownership.external]]\npaths = ["scripts/stray.sh"]\nreason = "r"\n'
            "skip = true\n",
            "takes only",
        ),
        ('[[source-ownership.external]]\npaths = ["nothing/*.py"]\nreason = "r"\n', "matches no"),
        ('external = ["scripts/stray.sh"]\n', "takes only"),
        ('external = "scripts/stray.sh"\n', "array of tables"),
    ],
)
def test_an_incomplete_or_stale_external_entry_is_a_configuration_error(
    repo: Path, external: str, message: str
) -> None:
    _write_config(repo, external)
    with pytest.raises(config.ConfigError, match=message):
        rule.check()


def test_unowned_sources_honours_declared_files(tmp_path: Path) -> None:
    sources = [tmp_path / "a.py", tmp_path / "b.py"]
    declared = {tmp_path / "a.py"}
    assert rule.unowned_sources(tmp_path, sources, [], [".py"], declared=declared) == [
        tmp_path / "b.py"
    ]
