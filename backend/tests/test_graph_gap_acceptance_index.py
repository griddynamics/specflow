"""The acceptance modules of graph.gap cover docs/graph/gap/requirements.md: exactly one test per FR and NFR unit,
its docstring starting with the unit's acceptance text, and every skipped one naming the session that enables it."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from gap_acceptance_support import pending, requirement_units, unit_id_of

TESTS: Final = Path(__file__).resolve().parent
MODULES: Final = tuple(
    path for path in sorted(TESTS.glob("test_graph_gap_acceptance_*.py")) if path.name != Path(__file__).name
)
UNITS: Final = {unit.unit_id: unit for unit in requirement_units()}


@dataclass(frozen=True)
class Stub:
    module: str
    name: str
    docstring: str
    sessions: tuple[str, ...]


def _pending_sessions(function: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    sessions = []
    for decorator in function.decorator_list:
        if isinstance(decorator, ast.Call) and getattr(decorator.func, "id", None) == "pending":
            first = decorator.args[0]
            sessions.append(first.value if isinstance(first, ast.Constant) else "?")
    return tuple(sessions)


def _stubs() -> list[Stub]:
    stubs = []
    for path in MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_"):
                docstring = ast.get_docstring(node) or ""
                stubs.append(Stub(path.name, node.name, docstring, _pending_sessions(node)))
    return stubs


def _words(text: str) -> str:
    """Whitespace collapsed; the heading-path mark of requirements.md written as ">" (lint RUF002)."""

    return " ".join(text.replace("\u203a", ">").split())


def test_the_requirements_file_lists_every_unit_once() -> None:
    ids = [unit.unit_id for unit in requirement_units()]
    assert len(ids) == len(set(ids)) == 103


def test_every_unit_has_exactly_one_acceptance_test_and_every_test_names_a_unit() -> None:
    found: dict[str, list[str]] = {}
    for stub in _stubs():
        unit_id = unit_id_of(stub.name)
        assert unit_id in UNITS, f"{stub.module}::{stub.name} names no unit of requirements.md"
        found.setdefault(unit_id, []).append(f"{stub.module}::{stub.name}")
    assert sorted(UNITS) == sorted(found)
    assert {unit_id: names for unit_id, names in found.items() if len(names) > 1} == {}


def test_every_docstring_starts_with_the_acceptance_text_of_its_unit() -> None:
    for stub in _stubs():
        unit = UNITS[unit_id_of(stub.name) or ""]
        assert _words(stub.docstring).startswith(_words(unit.acceptance)), stub.name


def test_a_pending_test_names_one_implementation_session() -> None:
    for stub in _stubs():
        assert len(stub.sessions) <= 1, stub.name
        assert all(re.fullmatch(r"\d\d", session) and int(session) <= 15 for session in stub.sessions), stub.name


def test_the_pending_marker_skips_with_the_session_in_the_reason() -> None:
    marker = pending("04", "partial")
    assert marker.mark.name == "skip"
    assert marker.mark.kwargs["reason"] == "session 04 (partial)"
    with pytest.raises(ValueError, match="two digits"):
        pending("4")


def test_no_acceptance_test_is_pending_any_more() -> None:
    """Session 15 enabled every unit: each has a green test (or, for a deferral the user approved, a recorded reason in
    the coverage matrix; none so far)."""

    assert [f"{stub.module}::{stub.name}" for stub in _stubs() if stub.sessions] == []
