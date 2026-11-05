"""Plan options and the context every plan step takes.

``PlanOptions`` are per-run planning inputs; invalid values become ``ConfigurationError("invalid_options")``.
``PlanContext`` holds live objects and is never serialised. It mirrors ``BuildContext``: an invalid
project is refused before a query; ``driver`` is the ``GraphStore`` of the plan (a Neo4j driver still works,
deprecated) and ``query_executor`` None becomes the store's executor. ``on_progress`` (optional) is told how
many items of a long step are done: the Tasks step reports its planning parts, ``(step, done, total)``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from graph.core.identity import validate_project_id
from graph.core.llm import Gateway
from graph.core.ontology import Ontology
from graph.core.store import Executor, as_executor
from graph.plan.models import BuildChangeReport, PlanOptions, PlanStepName
from graph.plan.ports import PlanStore
from graph.plan.rules import PlanRules

__all__ = ["PlanContext", "PlanOptions"]


@dataclass(frozen=True, slots=True)
class PlanContext:
    """Everything a plan step needs; ``with_run_id`` binds it to a run without recreating its executor."""

    project_id: str
    ontology: Ontology
    rules: PlanRules
    driver: Any
    gateway: Gateway
    store: PlanStore
    build_id: str
    changes: tuple[BuildChangeReport, ...] = ()
    options: PlanOptions = field(default_factory=PlanOptions)
    replan: bool = False
    run_id: str | None = None
    database: str | None = None
    query_executor: Executor | None = None
    on_progress: Callable[[PlanStepName, int, int], None] | None = None

    def report_progress(self, step: PlanStepName, done: int, total: int) -> None:
        """Tell ``on_progress`` (when set) that ``done`` of ``total`` items of ``step`` are done."""

        if self.on_progress is not None:
            self.on_progress(step, done, total)

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        if self.query_executor is None:
            object.__setattr__(self, "query_executor", as_executor(self.driver, self.database))

    def with_run_id(self, run_id: str) -> PlanContext:
        """The same context bound to ``run_id``; the executor is shared."""

        return replace(self, run_id=run_id)
