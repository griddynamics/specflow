"""Typed failures of the plan library."""

from __future__ import annotations

from collections.abc import Sequence
from typing import override

from graph.core.errors import ConfigurationError, GraphError


class TaskStateError(GraphError):
    """A lifecycle or story call does not apply; ``blockers`` names unfinished tasks, ``alternatives`` the claimable
    stories offered instead of a held one. ``claimed_by``, ``claimed_at`` and ``task_id`` come with the refusals that
    name a holder or a task."""

    def __init__(
        self,
        code: str,
        *,
        blockers: Sequence[str] = (),
        alternatives: Sequence[str] | None = None,
        **fields: str | int | None,
    ) -> None:
        self.blockers: tuple[str, ...] = tuple(blockers)
        self.alternatives: tuple[str, ...] = tuple(alternatives or ())
        counts = {} if alternatives is None else {"alternatives": len(self.alternatives)}
        super().__init__(code, blockers=len(self.blockers), **counts, **fields)

    @property
    def claimed_by(self) -> str | None:
        """The holder of the story or task the refusal names."""

        return self._text("claimed_by")

    @property
    def claimed_at(self) -> str | None:
        """When the holder claimed it (UTC, ISO 8601)."""

        return self._text("claimed_at")

    @property
    def task_id(self) -> str | None:
        """The task a ``partly_claimed`` refusal names."""

        return self._text("task_id")

    def _text(self, name: str) -> str | None:
        value = self.fields.get(name)
        return None if value is None else str(value)


class PlanReadError(GraphError):
    """A plan read finds nothing to answer: ``no_plan``, ``feature_not_found``, ``task_not_found``,
    ``story_not_found`` or ``area_not_found``."""


class PlanRulesError(ConfigurationError):
    """Invalid plan rules; ``problems`` lists every problem found."""

    def __init__(self, problems: Sequence[str], **fields: str | int | float | bool | None) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        super().__init__("invalid_plan_rules", problems=len(self.problems), **fields)

    @override
    def __str__(self) -> str:
        return "\n".join([super().__str__(), *(f"- {problem}" for problem in self.problems)])
