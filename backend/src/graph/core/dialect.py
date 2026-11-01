"""Query dialects and execution plans (design D2).

A ``Query`` is neutral (name, template, semantic arguments). A store's ``Dialect`` turns it into an
``ExecutionPlan``: ordered statements (text and bind parameters) plus a reducer that folds the
statements' results back to the rows the template promises (``written``, ``refs``, ``conflict``, ...).
The default plan is one statement with the identity reducer; a backend whose language needs several
statements for one template (per-type statements, merge or move in more than one step) returns more.
The executor runs a plan inside the caller's transaction.

The dialect also owns the project-id guard: it refuses a scoped statement that does not bind the
project id (``query_missing_project_id``), whatever that parameter looks like in its language.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

Record = dict[str, Any]
Reducer = Callable[[Sequence[list[Record]]], list[Record]]


def identity_reducer(results: Sequence[list[Record]]) -> list[Record]:
    """The rows of the plan's only (or first) statement."""

    return results[0] if results else []


@dataclass(frozen=True, slots=True)
class PlannedStatement:
    """One statement of a plan: its text and the parameters it binds."""

    text: str
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Ordered statements of one query and the reducer of their results to the template's rows."""

    statements: tuple[PlannedStatement, ...]
    reduce: Reducer = identity_reducer

    @classmethod
    def single(cls, text: str, params: Mapping[str, Any]) -> ExecutionPlan:
        """The default plan: one statement, rows returned as they are."""

        return cls((PlannedStatement(text, params),))


class Dialect(Protocol):
    """Renders queries for one backend language."""

    name: str

    def plan(self, query: Any, params: Mapping[str, Any]) -> ExecutionPlan:
        """The plan of ``query`` with ``params`` (the project id already injected for a scoped query).

        Raises ``ConformanceError`` for a template the dialect does not know, a name that is not
        safe to render, or a scoped statement that does not bind the project id."""
        ...


async def run_plan(
    plan: ExecutionPlan,
    run_statement: Callable[[str, Mapping[str, Any]], Awaitable[list[Record]]],
) -> list[Record]:
    """Run each statement of ``plan`` in order with ``run_statement`` and reduce the results."""

    results = [
        await run_statement(statement.text, statement.params)
        for statement in plan.statements
    ]
    return plan.reduce(results)
