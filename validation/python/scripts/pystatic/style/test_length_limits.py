"""Long production modules and long comments fail; test modules and short comments pass."""

from __future__ import annotations

from pathlib import Path

import pytest

from pystatic.style.length_limits import (
    MAX_COMMENT_LINES,
    MAX_MODULE_LINES,
    PEP695_RULE,
    check_project,
    comment_length_findings,
    is_production,
    module_length_findings,
)


def _project(root: Path, files: dict[str, str], manifest: str = "") -> Path:
    project = root / "tools" / "owner"
    for name, source in {"pyproject.toml": manifest, **files}.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return project


def _long_module(lines: int) -> str:
    return "".join(f"VALUE_{index} = {index}\n" for index in range(lines))


def _comment(lines: int) -> str:
    return "".join(f"# line {index}\n" for index in range(lines))


def _docstring(lines: int) -> str:
    body = "\n".join(f"    line {index}" for index in range(lines - 2))
    return f'    """Summary.\n{body}\n    """\n'


@pytest.mark.parametrize(
    ("relative", "production"),
    [
        ("tools/owner/module.py", True),
        ("tools/owner/typings/stub.pyi", True),
        ("tools/owner/tests/helper.py", False),
        ("tests/acceptance/support.py", False),
        ("tools/owner/test_module.py", False),
        ("tools/owner/module_test.py", False),
        ("tools/owner/conftest.py", False),
        ("svc/owner/checks/semgrep/fixtures/suppressions.py", False),
        ("svc/owner/src/fixtures_loader.py", True),
    ],
)
def test_production_scope_excludes_test_directories_and_modules(
    tmp_path: Path, relative: str, production: bool
) -> None:
    assert is_production(tmp_path / relative, tmp_path) is production


def test_module_over_the_ceiling_is_a_finding(tmp_path: Path) -> None:
    source = tmp_path / "huge.py"
    source.write_text(_long_module(MAX_MODULE_LINES + 1), encoding="utf-8")
    findings = module_length_findings([source], tmp_path)
    assert [finding.render() for finding in findings] == [
        "huge.py:1:1: pylint-too-many-lines: Too many lines in module (3001/3000) "
        "(fix: split the module by responsibility behind a delegating entry point; "
        "doc: /README.md#length-limits)"
    ]


def test_module_at_the_ceiling_passes(tmp_path: Path) -> None:
    source = tmp_path / "full.py"
    source.write_text(_long_module(MAX_MODULE_LINES), encoding="utf-8")
    assert module_length_findings([source], tmp_path) == []


def test_inline_disable_of_the_ceiling_is_itself_a_finding(tmp_path: Path) -> None:
    source = tmp_path / "suppressed.py"
    pragma = "# pylint: disable=too-many-lines\n"
    source.write_text(f"{pragma}{_long_module(MAX_MODULE_LINES + 1)}", encoding="utf-8")
    findings = module_length_findings([source], tmp_path)
    assert [(finding.line, finding.rule, finding.message) for finding in findings] == [
        (1, "pylint-locally-disabled", "Locally disabling too-many-lines (C0302)"),
        (1, "pylint-suppressed-message", "Suppressed 'too-many-lines' (from line 1)"),
    ]
    assert {finding.fix for finding in findings} == {
        "remove the pylint pragma; the length limits admit no suppression"
    }


def test_skipped_file_is_itself_a_finding(tmp_path: Path) -> None:
    source = tmp_path / "skipped.py"
    source.write_text(f"# pylint: skip-file\n{_long_module(MAX_MODULE_LINES + 1)}")
    findings = module_length_findings([source], tmp_path)
    assert [(finding.rule, finding.message) for finding in findings] == [
        ("pylint-file-ignored", "Ignoring entire file")
    ]


def test_comment_block_over_the_limit_is_a_finding(tmp_path: Path) -> None:
    source = tmp_path / "commented.py"
    source.write_text(f"VALUE = 1\n\n{_comment(MAX_COMMENT_LINES + 1)}VALUE_2 = 2\n")
    findings = comment_length_findings(source, tmp_path)
    assert [finding.render() for finding in findings] == [
        "commented.py:3:1: comment-length: comment block spans 6 lines (limit 5) "
        "(fix: say what and why in at most 5 lines and link the owning heading; "
        "doc: /README.md#length-limits)"
    ]


def test_short_comments_and_trailing_comments_pass(tmp_path: Path) -> None:
    trailing = "".join(f"VALUE_{index} = {index}  # trailing\n" for index in range(8))
    separated = f"{_comment(MAX_COMMENT_LINES)}\n{_comment(MAX_COMMENT_LINES)}"
    source = tmp_path / "short.py"
    source.write_text(f"{trailing}{separated}VALUE = 1\n", encoding="utf-8")
    assert comment_length_findings(source, tmp_path) == []


def test_hash_inside_a_string_is_not_a_comment(tmp_path: Path) -> None:
    source = tmp_path / "text.py"
    lines = "\n".join("# not a comment" for _ in range(MAX_COMMENT_LINES + 3))
    source.write_text(f'TEXT = """\n{lines}\n"""\n', encoding="utf-8")
    assert comment_length_findings(source, tmp_path) == []


@pytest.mark.parametrize(
    ("header", "owner"),
    [
        ("class Owner:\n", "'Owner'"),
        ("def owner():\n", "'owner'"),
        ("async def owner():\n", "'owner'"),
    ],
)
def test_long_docstring_is_a_finding(tmp_path: Path, header: str, owner: str) -> None:
    source = tmp_path / "documented.py"
    source.write_text(f"{header}{_docstring(MAX_COMMENT_LINES + 1)}", encoding="utf-8")
    findings = comment_length_findings(source, tmp_path)
    assert [(finding.line, finding.col, finding.message) for finding in findings] == [
        (2, 5, f"docstring of {owner} spans 6 lines (limit 5)")
    ]


def test_long_module_docstring_is_a_finding_and_a_short_one_passes(tmp_path: Path) -> None:
    long_source = tmp_path / "long.py"
    long_source.write_text('"""One.\n2\n3\n4\n5\n6\n"""\n', encoding="utf-8")
    short_source = tmp_path / "short.py"
    short_source.write_text('"""One.\n2\n3\n4\n"""\n\n\ndef owner():\n    """Short."""\n')
    assert [finding.message for finding in comment_length_findings(long_source, tmp_path)] == [
        "docstring of module spans 7 lines (limit 5)"
    ]
    assert comment_length_findings(short_source, tmp_path) == []


def test_project_checks_owned_and_external_sources_but_not_test_length(tmp_path: Path) -> None:
    external = tmp_path / "tests" / "fixtures" / "builder.py"
    external.parent.mkdir(parents=True)
    external.write_text(_comment(MAX_COMMENT_LINES + 1), encoding="utf-8")
    project = _project(
        tmp_path,
        {
            "huge.py": _long_module(MAX_MODULE_LINES + 1),
            "test_huge.py": _long_module(MAX_MODULE_LINES + 1),
            "tests/helper.py": f"{_comment(MAX_COMMENT_LINES + 1)}VALUE = 1\n",
            ".venv/lib/vendored.py": _long_module(MAX_MODULE_LINES + 1),
        },
        '[tool.source-ownership]\nexternal = ["../../tests/fixtures/**/*.py", "**/*.sh"]\n',
    )
    findings = check_project(project, tmp_path)
    assert [(finding.path, finding.rule) for finding in findings] == [
        ("tests/fixtures/builder.py", "comment-length"),
        ("tools/owner/huge.py", "pylint-too-many-lines"),
        ("tools/owner/tests/helper.py", "comment-length"),
    ]


def test_project_without_python_files_is_an_error(tmp_path: Path) -> None:
    project = _project(tmp_path, {"readme.txt": "no sources\n"})
    with pytest.raises(ValueError, match="no owned Python"):
        check_project(project, tmp_path)


def test_unparsable_production_source_is_an_error(tmp_path: Path) -> None:
    project = _project(tmp_path, {"broken.py": "def broken(:\n"})
    with pytest.raises(ValueError, match="pylint syntax-error"):
        check_project(project, tmp_path)


def test_unparsable_test_source_is_an_error(tmp_path: Path) -> None:
    project = _project(tmp_path, {"test_broken.py": "def broken(:\n"})
    with pytest.raises(SyntaxError):
        check_project(project, tmp_path)


@pytest.mark.parametrize(
    ("source", "subject", "line", "col"),
    [
        ("def first[T](values: list[T]) -> T:\n    return values[0]\n", "of 'first'", 1, 1),
        ("async def wait[T](value: T) -> T:\n    return value\n", "of 'wait'", 1, 1),
        ("class Box[T]:\n    pass\n", "of 'Box'", 1, 1),
        ("class Owner:\n    def wrap[**P](self) -> None:\n        pass\n", "of 'wrap'", 2, 5),
        (
            "def outer():\n    def inner[T](value: T) -> T:\n        return value\n",
            "of 'inner'",
            2,
            5,
        ),
        ("type RecordId = str\n", "statement 'RecordId'", 1, 1),
        ("type Pair[T] = tuple[T, T]\n", "statement 'Pair'", 1, 1),
        ("if True:\n    type Nested = int\n", "statement 'Nested'", 2, 5),
    ],
)
def test_pep695_generic_syntax_is_a_finding(
    tmp_path: Path, source: str, subject: str, line: int, col: int
) -> None:
    project = _project(tmp_path, {"generic.py": source})
    findings = check_project(project, tmp_path)
    assert [(found.path, found.line, found.col, found.rule) for found in findings] == [
        ("tools/owner/generic.py", line, col, PEP695_RULE)
    ]
    rendered = findings[0].render()
    assert subject in findings[0].message
    assert rendered.endswith(
        "(fix: declare a module-level TypeVar or ParamSpec, or a TypeAliasType alias; "
        "doc: /README.md#length-limits)"
    )


def test_typing_generics_and_type_named_values_pass(tmp_path: Path) -> None:
    source = (
        "from typing import Generic, ParamSpec, TypeAliasType, TypeVar\n"
        'T = TypeVar("T")\nP = ParamSpec("P")\n'
        'RecordId = TypeAliasType("RecordId", str)\ntype_name = "x"\n'
        "def first(values: list[T]) -> T:\n    return values[0]\n"
        "class Box(Generic[T]):\n    pass\n"
    )
    project = _project(tmp_path, {"generic.py": source, "tests/test_generic.py": source})
    assert check_project(project, tmp_path) == []


def test_comment_block_starting_on_the_first_line_is_measured(tmp_path: Path) -> None:
    source = tmp_path / "first.py"
    source.write_text(f"{_comment(MAX_COMMENT_LINES + 1)}VALUE = 1\n", encoding="utf-8")
    assert [finding.line for finding in comment_length_findings(source, tmp_path)] == [1]
