"""Structure rule: no Cypher string in the live tests or their support (backend-neutral live suites).

Live graph tests and everything they share talk to the graph through ``graph_probes`` (``GraphProbe``);
the only Cypher a test file may hold is in ``tests/graph_probes/neo4j*`` (the Neo4j probe) and in the
library's own query files under ``src/graph``. A new Cypher string anywhere else in ``tests`` fails here
unless its file is listed in ``DIALECT_TESTS`` (offline tests of the Cypher dialect and the Bolt
adapter, where the statement text is the subject) with its reason.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

import pytest

BACKEND = Path(__file__).resolve().parents[1]
TESTS = BACKEND / "tests"

# One pattern per statement shape a test would write by hand (Cypher keywords are upper case in this repo).
CYPHER: Final = re.compile(
    r"\b(MATCH\s*\(|MERGE\s*\(|UNWIND\s|DETACH\s+DELETE|OPTIONAL\s+MATCH|CREATE\s+(\(|INDEX|CONSTRAINT)|"
    r"SHOW\s+(\w+\s+)?(INDEXES|CONSTRAINTS)|DROP\s+(INDEX|CONSTRAINT)|CALL\s+(db|dbms)\.|RETURN\s+[\w\[\(\$'\"]+\s+AS\b)",
)

PROBE_FILES: Final = {"tests/graph_probes/neo4j.py", "tests/graph_probes/arcadedb.py"}

# Offline tests whose subject is the Cypher dialect, the Bolt adapter or the library's query modules
# (the text of a statement is what they assert). Live tests and shared support are never listed.
DIALECT_TESTS: Final = {
    "tests/test_graph_core.py": "Bolt executor and error mapping over a fake driver, statements as literals",
    "tests/test_graph_dialect.py": "dialect planning of literal statements",
    "tests/test_graph_query_files.py": "query-file template rendering",
    "tests/test_graph_reader.py": "reader against scripted executors",
    "tests/graph_ddl_support.py": "Cypher DDL fixture of the gap ontology for the schema tests",
    "tests/test_graph_schema.py": "Cypher DDL parsing of the schema step",
    "tests/test_graph_structure.py": "static rules over statement text",
    "tests/test_graph_writer.py": "writer statements rendered in Cypher",
}


# The examples are backend-neutral too: they use the library's queries. The one raw statement allowed is
# graph_eval.py's cleanup (a batched DETACH DELETE of a project, there is no library delete-project query
# yet); graph_eval.py refuses a store that is not Neo4j because of it. The statement is matched exactly.
EXAMPLES = BACKEND / "examples"
EXAMPLE_STATEMENTS: Final = {
    "examples/graph_eval.py": {
        "MATCH (n {group_id: $project_id}) WITH n LIMIT 2000 DETACH DELETE n RETURN count(*) AS c",
    },
    "examples/graph_bench.py": {
        "MATCH (n {group_id: $project_id}) WITH n LIMIT 2000 DETACH DELETE n RETURN count(*) AS c",
    },
}


def strings(path: Path) -> list[tuple[int, str]]:
    """String constants of a module outside docstrings, with their line."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def cypher_lines(path: Path) -> list[int]:
    return sorted({line for line, text in strings(path) if CYPHER.search(text)})


def relative(path: Path) -> str:
    return path.relative_to(BACKEND).as_posix()


def test_the_rule_covers_every_graph_test_file() -> None:
    names = {relative(p) for p in GRAPH_FILES}
    assert {"tests/test_graph_build_live.py", "tests/graph_support.py", "tests/graph_probes/views.py"} <= names
    assert "tests/test_graph_probes_structure.py" not in names


def test_the_detector_finds_the_statements_the_probe_runs() -> None:
    assert cypher_lines(TESTS / "graph_probes" / "neo4j.py")
    assert CYPHER.search("MATCH (n:SpecNode) WHERE n.group_id = $p RETURN n.spec_id AS s")
    assert CYPHER.search("UNWIND $rows AS row CREATE (n:X)")
    assert CYPHER.search("SHOW VECTOR INDEXES YIELD name")
    assert not CYPHER.search("Create a project and return nodes as plain data")
    assert not CYPHER.search("create a match for return value as text")


GRAPH_FILES: Final = sorted(
    p for p in TESTS.rglob("*.py") if "graph" in p.relative_to(TESTS).as_posix() and p.name != Path(__file__).name
)


@pytest.mark.parametrize("path", GRAPH_FILES, ids=relative)
def test_no_cypher_string_outside_the_neo4j_probe_and_the_dialect_tests(path: Path) -> None:
    name = relative(path)
    if name in PROBE_FILES or name in DIALECT_TESTS:
        return
    assert cypher_lines(path) == [], f"{name} holds Cypher: go through a GraphProbe method"


def test_the_exceptions_are_real_files_that_still_need_them() -> None:
    for name in sorted(DIALECT_TESTS):
        path = BACKEND / name
        assert path.is_file(), f"{name} no longer exists: remove it from DIALECT_TESTS"
        assert cypher_lines(path), f"{name} holds no Cypher any more: remove it from DIALECT_TESTS"


def test_live_modules_and_shared_support_are_never_exceptions() -> None:
    live = {relative(p) for p in TESTS.glob("*live*.py")} | {
        "tests/graph_support.py",
        "tests/graph_extraction_store_support.py",
    }
    live |= {relative(p) for p in (TESTS / "graph_probes").glob("*.py")} - PROBE_FILES
    assert live and not live & set(DIALECT_TESTS)
    for name in sorted(live):
        assert cypher_lines(BACKEND / name) == [], name


def test_only_probe_names_of_cypher_stores_are_exempt() -> None:
    assert all(name.startswith(("tests/graph_probes/neo4j", "tests/graph_probes/arcadedb")) for name in PROBE_FILES)


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.py")), ids=relative)
def test_no_cypher_string_in_the_examples_beyond_the_listed_statements(path: Path) -> None:
    allowed = EXAMPLE_STATEMENTS.get(relative(path), set())
    found = [text for _line, text in strings(path) if CYPHER.search(text) and text not in allowed]
    assert found == [], f"{relative(path)} holds Cypher: use a library query"


def test_the_example_statement_allowances_are_still_used() -> None:
    for name, statements in EXAMPLE_STATEMENTS.items():
        used = {text for _line, text in strings(BACKEND / name) if CYPHER.search(text)}
        assert statements == used, f"{name}: update EXAMPLE_STATEMENTS"
