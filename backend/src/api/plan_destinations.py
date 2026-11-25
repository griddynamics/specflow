"""``PlanDestinations``: the one object the plan runner calls after a returned plan run (``after_plan``), which calls
each registered destination of the plan in turn (the Jira sync today; docs/mission-control/integrations-api.md "Plan
destinations").

The plan runner holds one hook and is not changed: this object is that hook. A destination only enqueues its work (as
``JiraSyncRunner.after_plan`` does) and one that raises is logged by its class and never reaches the plan run or the
destinations after it.
"""

from __future__ import annotations

from typing import Protocol

from api.contained import Contained
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("api.plan.destinations")


class PlanDestination(Protocol):
    """A place the plan is sent to; ``after_plan`` is told that the project's plan run returned."""

    async def after_plan(self, project_id: str) -> None: ...


class PlanDestinations:
    """The registered destinations, in registration order."""

    def __init__(self) -> None:
        self._destinations: list[PlanDestination] = []

    def register(self, destination: PlanDestination) -> None:
        self._destinations.append(destination)

    async def after_plan(self, project_id: str) -> None:
        for destination in self._destinations:
            with Contained() as called:
                await destination.after_plan(project_id)
            if called.failed:
                log_event(
                    logger,
                    WARNING,
                    "api.plan.destination_failed",
                    project_id=project_id,
                    destination=type(destination).__name__,
                    error_type=called.error_type,
                )
