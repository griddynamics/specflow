"""Self-test for `query_files`: the stem binding, glob matching and statement splitting."""

from __future__ import annotations

from pathlib import Path

import pytest

from cypher_checks import query_files


class TestDocumentedNameFor:
    def test_separator_and_case_are_not_part_of_the_binding(self) -> None:
        normalize = query_files.normalize_query_name
        assert normalize("Follower network") == normalize("follower_network")
        assert normalize("follower_network") == normalize("FOLLOWER-NETWORK")

    def test_backticks_are_dropped(self) -> None:
        assert query_files.normalize_query_name("`all_genres`") == "all genres"

    def test_a_matching_stem_resolves_to_the_registered_name(self) -> None:
        names = ["Follower network", "All genres"]
        assert query_files.documented_name_for("q/follower_network.cypher", names) == (
            "Follower network"
        )

    def test_an_unknown_stem_binds_to_nothing(self) -> None:
        assert query_files.documented_name_for("q/unrelated.cypher", ["All genres"]) is None

    def test_the_result_does_not_depend_on_iteration_order(self) -> None:
        names = ["Follower network", "follower-network", "FOLLOWER NETWORK"]
        expected = "FOLLOWER NETWORK"
        assert query_files.documented_name_for("q/follower_network.cypher", names) == expected
        assert query_files.documented_name_for("q/follower_network.cypher", names[::-1]) == (
            expected
        )


@pytest.mark.parametrize(
    ("path", "glob", "expected"),
    [
        ("/a/b/schema/x.cypher", "**/schema/*.cypher", True),
        ("schema/x.cypher", "**/schema/*.cypher", True),
        ("/a/schema/sub/x.cypher", "**/schema/*.cypher", False),
        ("/a/b/write/x.cypher", "**/write/*.cypher", True),
        ("/a/rewrite/x.cypher", "**/write/*.cypher", False),
        ("tests/a.py", "**/tests/**", True),
        ("src/test_helpers/a.py", "**/test_*.py", False),
        ("repoint_acted_in.cypher", "repoint_*.cypher", True),
    ],
)
def test_glob_matches(path: str, glob: str, *, expected: bool) -> None:
    assert query_files.glob_matches(path, glob) is expected


def test_matches_any_tries_the_bare_file_name(tmp_path: Path) -> None:
    path = tmp_path / "write" / "repoint_acted_in.cypher"
    assert query_files.matches_any(path, ["repoint_*.cypher"])
    assert not query_files.matches_any(path, ["merge_*.cypher"])


def test_split_statements_drops_comment_lines_and_blanks() -> None:
    text = (
        "// header\nCREATE INDEX a FOR (n:A) ON (n.x);\n\n"
        "// b\nCREATE INDEX b FOR (n:B) ON (n.y);\n"
    )
    assert query_files.split_statements(text) == (
        "CREATE INDEX a FOR (n:A) ON (n.x)",
        "CREATE INDEX b FOR (n:B) ON (n.y)",
    )


def test_statements_in_keeps_a_query_file_whole(tmp_path: Path) -> None:
    path = tmp_path / "read" / "q.cypher"
    path.parent.mkdir()
    path.write_text("// q\nMATCH (n:A {id: $id})\nRETURN n.id AS id\n", encoding="utf-8")
    assert query_files.statements_in(path, ["**/schema/*.cypher"]) == [path.read_text()]


def test_statements_in_splits_a_ddl_file(tmp_path: Path) -> None:
    path = tmp_path / "schema" / "s.cypher"
    path.parent.mkdir()
    path.write_text("CREATE INDEX a FOR (n:A) ON (n.x);\nCREATE INDEX b FOR (n:B) ON (n.y);\n")
    assert len(query_files.statements_in(path, ["**/schema/*.cypher"])) == 2


def test_expand_targets_walks_directories(tmp_path: Path) -> None:
    (tmp_path / "read").mkdir()
    first = tmp_path / "read" / "a.cypher"
    first.write_text("// a\n")
    (tmp_path / "read" / "notes.txt").write_text("x")
    second = tmp_path / "b.cypher"
    second.write_text("// b\n")
    assert query_files.expand_targets([tmp_path / "read", second]) == sorted([first, second])
