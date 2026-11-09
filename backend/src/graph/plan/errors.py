"""Typed failures of the plan library."""

from __future__ import annotations

from collections.abc import Sequence
from typing import override

from graph.core.errors import ConfigurationError, GraphError


class TaskStateError(GraphError):
    """A lifecycle call does not apply; ``blockers`` names unfinished tasks."""

    def __init__(self, code: str, *, blockers: Sequence[str] = (), **fields: str | int | None) -> None:
        self.blockers: tuple[str, ...] = tuple(blockers)
        super().__init__(code, blockers=len(self.blockers), **fields)


class PlanReadError(GraphError):
    """A plan read finds nothing to answer: ``no_plan``, ``feature_not_found`` or ``task_not_found``."""


class PlanRulesError(ConfigurationError):
    """Invalid plan rules; ``problems`` lists every problem found."""

    def __init__(self, problems: Sequence[str], **fields: str | int | float | bool | None) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        super().__init__("invalid_plan_rules", problems=len(self.problems), **fields)

    @override
    def __str__(self) -> str:
        return "\n".join([super().__str__(), *(f"- {problem}" for problem in self.problems)])
