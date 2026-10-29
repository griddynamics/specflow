"""Self-test for `param_names`: a run call passes only the `$params` its loaded query declares."""

from __future__ import annotations

from pathlib import Path

import pytest

from cypher_checks import param_names
from cypher_checks.__main__ import main

_QUERY = "// One movie by title.\nMATCH (m:Movie {title: $title})\nRETURN m.id AS id\n"


def _tree(tmp_path: Path, call: str, query: str = _QUERY) -> param_names.Config:
    """A source tree with one query file, one loader assignment and one run call."""
    queries = tmp_path / "queries"
    queries.mkdir()
    (queries / "movie_by_title.cypher").write_text(query, encoding="utf-8")
    (tmp_path / "movies.py").write_text(
        'MOVIE_BY_TITLE = load_query("movie_by_title.cypher")\n\n\n'
        f"def find(tx, title):\n    return {call}\n",
        encoding="utf-8",
    )
    return param_names.Config(root=tmp_path, loaders={"load_query": queries})


def test_matching_keywords_are_clean(tmp_path: Path) -> None:
    assert param_names.check(_tree(tmp_path, "tx.run(MOVIE_BY_TITLE, title=title)")) == []


def test_an_undeclared_keyword_is_flagged(tmp_path: Path) -> None:
    findings = param_names.check(_tree(tmp_path, "tx.run(MOVIE_BY_TITLE, name=title)"))
    assert [(f.path, f.rule) for f in findings] == [
        ("queries/movie_by_title.cypher", "cypher-lint-param-mismatch")
    ]
    assert "movies.py:5" in findings[0].message
    assert "'name'" in findings[0].message


def test_a_parameter_named_only_in_a_comment_is_not_declared(tmp_path: Path) -> None:
    query = "// Takes $name too.\nMATCH (m:Movie {title: $title})\nRETURN m.id AS id\n"
    config = _tree(tmp_path, "tx.run(MOVIE_BY_TITLE, title=title, name=title)", query)
    assert [f.rule for f in param_names.check(config)] == ["cypher-lint-param-mismatch"]


def test_a_constant_no_loader_assigned_is_ignored(tmp_path: Path) -> None:
    assert param_names.check(_tree(tmp_path, "tx.run(OTHER_QUERY, anything=title)")) == []


def test_an_unconfigured_loader_is_ignored(tmp_path: Path) -> None:
    config = _tree(tmp_path, "tx.run(MOVIE_BY_TITLE, name=title)")
    other = param_names.Config(root=tmp_path, loaders={"load_cypher": tmp_path / "queries"})
    assert param_names.check(other) == []
    assert param_names.check(config) != []


def test_the_cli_reports_with_exit_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _tree(tmp_path, "tx.run(MOVIE_BY_TITLE, name=title)")
    assert main(["param-names", str(tmp_path), "--loader", "load_query=queries"]) == 1
    assert "cypher-lint-param-mismatch" in capsys.readouterr().out


def test_the_cli_requires_a_loader(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["param-names", str(tmp_path)])
