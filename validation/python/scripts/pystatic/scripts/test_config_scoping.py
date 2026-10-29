"""Self-test for `config_scoping`: every suppression key is read and judged against a
reviewed table, and a reviewed entry the config no longer holds is stale."""

from __future__ import annotations

from pathlib import Path

import pytest

from pystatic.harness.docref import doc_anchor_exists
from pystatic.harness.models import Finding
from pystatic.scripts import config_scoping as rule

REVIEWED = """\
[ruff.global-ignore]
COM812 = "formatter conflict: ruff format owns trailing commas"

[ruff.per-file-ignores]
"tests/**" = ["S101", "PLR2004"]

[ruff.exclude]
"fixtures/**" = "deliberately invalid samples"

[mypy.exclude]
"^fixtures/" = "deliberately invalid samples"

[mypy]
disable-error-code = []
ignore-missing-imports = ["untyped_vendor_lib"]

[pytest]
filterwarnings = ["ignore:old alias is deprecated:DeprecationWarning"]
"""


def _findings(root: Path, pyproject: str, *, reviewed: str = REVIEWED) -> list[Finding]:
    (root / "pyproject.toml").write_text(pyproject)
    (root / rule.REVIEWED_NAME).write_text(reviewed)
    return rule.check(root)


def _rules(findings: list[Finding], *, stale: bool = False) -> set[str]:
    return {f.rule for f in findings if stale or f.rule != rule.STALE}


@pytest.mark.parametrize(
    ("pyproject", "expected"),
    [
        ('[tool.ruff.lint]\nignore = ["E501"]\n', "no-suppressions-ruff-global-ignore"),
        ('[tool.ruff.lint]\nextend-ignore = ["E501"]\n', "no-suppressions-ruff-global-ignore"),
        ('[tool.ruff]\nignore = ["E501"]\n', "no-suppressions-ruff-global-ignore"),
        (
            '[tool.ruff.lint.extend-per-file-ignores]\n"src/**" = ["S101"]\n',
            "no-suppressions-ruff-per-file-ignore",
        ),
        ('[tool.ruff]\nexclude = ["src/legacy/**"]\n', "no-suppressions-ruff-exclude"),
        ('[tool.mypy]\nexclude = ["^src/legacy/"]\n', "no-suppressions-mypy-exclude"),
        ('[tool.mypy]\nexclude = "^src/legacy/"\n', "no-suppressions-mypy-exclude"),
        (
            '[tool.mypy]\ndisable_error_code = ["assignment"]\n',
            "no-suppressions-mypy-disable-error-code",
        ),
        ("[tool.mypy]\nignore_errors = true\n", "no-suppressions-mypy-ignore-errors"),
        (
            '[[tool.mypy.overrides]]\nmodule = ["pkg.legacy"]\nignore_errors = true\n',
            "no-suppressions-mypy-ignore-errors",
        ),
        (
            '[[tool.mypy.overrides]]\nmodule = ["other_lib"]\nignore_missing_imports = true\n',
            "no-suppressions-mypy-ignore-missing-imports",
        ),
        (
            '[tool.pytest.ini_options]\nfilterwarnings = ["ignore::UserWarning"]\n',
            "no-suppressions-pytest-filterwarnings",
        ),
    ],
)
def test_unreviewed_suppression_is_flagged(tmp_path: Path, pyproject: str, expected: str) -> None:
    assert expected in _rules(_findings(tmp_path, pyproject))


def test_reviewed_suppressions_are_not_flagged(tmp_path: Path) -> None:
    pyproject = (
        '[tool.ruff]\nextend-exclude = ["fixtures/**"]\n'
        '[tool.ruff.lint]\nignore = ["COM812"]\n'
        '[tool.ruff.lint.per-file-ignores]\n"tests/**" = ["S101"]\n'
        '[tool.mypy]\nexclude = ["^fixtures/"]\n'
        '[[tool.mypy.overrides]]\nmodule = ["untyped_vendor_lib"]\nignore_missing_imports = true\n'
        "[tool.pytest.ini_options]\n"
        'filterwarnings = ["error", "ignore:old alias is deprecated:DeprecationWarning"]\n'
    )
    assert _findings(tmp_path, pyproject) == []


def test_unreviewed_code_on_a_reviewed_glob_is_flagged(tmp_path: Path) -> None:
    pyproject = '[tool.ruff.lint.per-file-ignores]\n"tests/**" = ["S101", "E501"]\n'
    findings = _findings(tmp_path, pyproject)
    flagged = [f for f in findings if f.rule == "no-suppressions-ruff-per-file-ignore"]
    assert [f.message.split(" ", 1)[0] for f in flagged] == ["'E501'"]


def test_error_filterwarnings_is_not_a_suppression(tmp_path: Path) -> None:
    findings = _findings(tmp_path, '[tool.pytest.ini_options]\nfilterwarnings = ["error"]\n')
    assert _rules(findings) == set()


def test_reviewed_entry_missing_from_the_config_is_stale(tmp_path: Path) -> None:
    findings = _findings(tmp_path, '[tool.ruff.lint]\nignore = ["COM812"]\n')
    stale = {f.message for f in findings if f.rule == rule.STALE}
    assert not any("COM812" in m for m in stale)
    assert any("'tests/**'" in m for m in stale)
    assert any("'fixtures/**'" in m for m in stale)
    assert any("'^fixtures/'" in m for m in stale)


def _stale_messages(tmp_path: Path, pyproject: str, reviewed: str) -> set[str]:
    findings = _findings(tmp_path, pyproject, reviewed=reviewed)
    return {f.message for f in findings if f.rule == rule.STALE}


def test_reviewed_mypy_error_code_missing_from_the_config_is_stale(tmp_path: Path) -> None:
    reviewed = '[mypy]\ndisable-error-code = ["attr-defined"]\n'
    assert any("'attr-defined'" in m for m in _stale_messages(tmp_path, "[tool.mypy]\n", reviewed))
    held = '[tool.mypy]\ndisable_error_code = ["attr-defined"]\n'
    assert not _stale_messages(tmp_path, held, reviewed)


def test_reviewed_mypy_module_missing_from_the_config_is_stale(tmp_path: Path) -> None:
    reviewed = '[mypy]\nignore-missing-imports = ["vendor_lib"]\n'
    assert any("'vendor_lib'" in m for m in _stale_messages(tmp_path, "[tool.mypy]\n", reviewed))
    held = '[[tool.mypy.overrides]]\nmodule = ["vendor_lib"]\nignore_missing_imports = true\n'
    assert not _stale_messages(tmp_path, held, reviewed)


def test_reviewed_filterwarnings_missing_from_the_config_is_stale(tmp_path: Path) -> None:
    entry = "ignore:old alias:DeprecationWarning"
    reviewed = f'[pytest]\nfilterwarnings = ["{entry}"]\n'
    stale = _stale_messages(tmp_path, "[tool.pytest.ini_options]\n", reviewed)
    assert any(entry in m for m in stale)
    held = f'[tool.pytest.ini_options]\nfilterwarnings = ["{entry}"]\n'
    assert not _stale_messages(tmp_path, held, reviewed)


def test_absent_reviewed_table_reviews_nothing(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.ruff.lint]\nignore = ["COM812"]\n')
    assert _rules(rule.check(tmp_path)) == {"no-suppressions-ruff-global-ignore"}


def test_missing_pyproject_is_a_tool_error_and_has_no_findings(tmp_path: Path) -> None:
    assert rule.check(tmp_path) == []
    result = rule.run(tmp_path)
    assert result.exit_code == 2
    assert result.detail.startswith("tool-error:")


def test_run_reads_an_explicit_reviewed_path(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.ruff.lint]\nignore = ["COM812"]\n')
    reviewed = tmp_path / "elsewhere.toml"
    reviewed.write_text('[ruff.global-ignore]\nCOM812 = "formatter conflict"\n')
    assert rule.run(tmp_path, reviewed).exit_code == 0
    assert rule.run(tmp_path).exit_code == 1


def test_every_finding_cites_a_resolvable_anchor(tmp_path: Path) -> None:
    findings = _findings(tmp_path, "[tool.mypy]\nignore_errors = true\n")
    assert findings
    for f in findings:
        assert doc_anchor_exists(f.doc), f.doc
