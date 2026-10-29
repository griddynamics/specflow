"""Offline tests of the query-file templates (graph.core.query_files): ``__NAME__`` tokens and
``/*__NAME__*/default/*__NAME__*/`` slots are filled by ``render_query``, and every query file is a
complete statement as it stands (a slot's default keeps it one), so the Cypher checks need no rendering.
No Neo4j.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from graph.core.errors import ConformanceError
from graph.core.query_files import read_query_file, render_query

GRAPH: Final = Path(__file__).resolve().parents[1] / "src" / "graph"
QUERY_DIRECTORIES: Final = (GRAPH / "core" / "queries", GRAPH / "build" / "queries")
SLOT_MARKER: Final = re.compile(r"/\*(__[A-Z][A-Z_]*__)\*/")
# Cypher takes a token only where it takes a name; these parts are never names, so they are slots.
NON_NAME_TOKENS: Final = (
    "__ARROW__",
    "__MEMBER_ARROW__",
    "__CONTAINER_ARROW__",
    "__HOPS__",
    "__MIN_HOPS__",
    "__MAX_HOPS__",
    "__BRANCHES__",
    "__SOURCE__",
    "__TARGET__",
    "__PATTERN__",
    "__CONDITIONS__",
    "__TAIL__",
)


def query_files() -> list[tuple[Path, str]]:
    return [(path.parent, path.name) for directory in QUERY_DIRECTORIES for path in sorted(directory.glob("*.cypher"))]


def test_a_slot_is_replaced_whole_and_a_token_by_its_name() -> None:
    template = "MATCH (n)-[r:__TYPE__]/*__ARROW__*/->/*__ARROW__*/(m) RETURN m"

    assert render_query(template, type="PART_OF", arrow="-") == "MATCH (n)-[r:PART_OF]-(m) RETURN m"
    assert render_query(template, type="PART_OF", arrow="->") == "MATCH (n)-[r:PART_OF]->(m) RETURN m"


def test_a_slot_may_be_rendered_empty_and_span_several_clauses() -> None:
    template = "MATCH (s:X {k: $k})/*__TAIL__*/ RETURN s.k AS k/*__TAIL__*/"

    assert render_query(template, tail="") == "MATCH (s:X {k: $k})"


def test_each_slot_of_a_name_is_filled() -> None:
    template = (
        "{/*__MIN_HOPS__*/1/*__MIN_HOPS__*/,/*__MAX_HOPS__*/3/*__MAX_HOPS__*/} {/*__MIN_HOPS__*/1/*__MIN_HOPS__*/}"
    )

    assert render_query(template, min_hops="2", max_hops="5") == "{2,5} {2}"


@pytest.mark.parametrize(
    ("template", "names"),
    [
        ("MATCH (n)-[r:__TYPE__]->(m) RETURN m", {}),
        ("MATCH (n)-[r:X]/*__ARROW__*/->/*__ARROW__*/(m) RETURN m", {}),
        ("MATCH (n)-[r:X]/*__ARROW__*/->/*__ARROW__*/(m) RETURN m", {"type": "X"}),
    ],
)
def test_an_unfilled_slot_or_token_is_refused(template: str, names: dict[str, str]) -> None:
    with pytest.raises(ConformanceError) as raised:
        render_query(template, **names)
    assert raised.value.code == "cypher_template_unfilled"


@pytest.mark.parametrize(("directory", "name"), query_files(), ids=str)
def test_a_query_file_holds_non_name_parts_only_as_slots(directory: Path, name: str) -> None:
    """A token where Cypher takes no name would leave the file an invalid statement for the checks."""

    text = read_query_file(directory, name)
    outside_markers = SLOT_MARKER.sub(" ", text)
    assert [token for token in NON_NAME_TOKENS if token in outside_markers] == []
    for token in set(SLOT_MARKER.findall(text)):
        assert text.count(f"/*{token}*/") % 2 == 0, f"{name}: unpaired slot marker {token}"
