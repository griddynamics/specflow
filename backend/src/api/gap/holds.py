"""The gap module's ``StoryHolds`` port in the host: the plan service's hold, block and release calls.

``graph.gap`` never imports ``graph.plan`` (docs/graph/gap/architecture.md); the host hands it ``PlanHolds``, which maps
each port call onto one ``PlanService`` call (docs/graph/gap/api.md "Ports", lifecycle.md "Plan link", "Development
gaps"):

- ``hold_tasks`` -> ``PlanService.hold_tasks``: an open blocker gap blocks the open tasks implementing its nodes, with
  ``blocked_by`` the gap id (decision GD2);
- ``release_gap`` -> ``PlanService.unblock_finding``: the tasks a gap holds return to ``pending``;
- ``check_block`` and ``block_story``: the plan's guard and block call of a blocking development gap.

The plan service is built on first use with the shipped plan rules (blocking: a thread).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, cast

from graph.core import Ontology
from graph.core.store import GraphStore
from graph.gap import HoldResult
from graph.plan.names import BlockReason
from graph.plan.rules import load_plan_rules
from graph.plan.service import PlanService


class PlanHolds:
    """``StoryHolds`` over the plan service of one graph store."""

    def __init__(self, plan: PlanService) -> None:
        self._plan = plan

    async def hold_tasks(
        self, project_id: str, gap_id: str, spec_ids: Sequence[str], *, reason: str = "spec_ambiguous"
    ) -> HoldResult:
        held = await self._plan.hold_tasks(project_id, gap_id, spec_ids, reason=cast(BlockReason, reason))
        return HoldResult(held=held.held, in_progress=held.in_progress)

    async def release_gap(self, project_id: str, gap_id: str) -> Sequence[str]:
        return await self._plan.unblock_finding(project_id, gap_id)

    async def check_block(
        self, project_id: str, story_id: str, *, claimed_by: str, claim_token: str, spec_ids: Sequence[str]
    ) -> None:
        await self._plan.check_block(
            project_id, story_id, claimed_by=claimed_by, claim_token=claim_token, spec_ids=spec_ids
        )

    async def block_story(
        self, project_id: str, story_id: str, *, claim_token: str, reason: str, finding_id: str
    ) -> Any:
        return await self._plan.block_story(
            project_id, story_id, claim_token=claim_token, reason=cast(BlockReason, reason), finding_id=finding_id
        )


def _plan_service(store: GraphStore, ontology: Ontology) -> PlanService:
    return PlanService(store, ontology, load_plan_rules(ontology=ontology))


async def plan_holds(store: GraphStore, ontology: Ontology) -> PlanHolds:
    """The holds of the plan on ``store`` (reads the shipped plan rules in a thread)."""

    return PlanHolds(await asyncio.to_thread(_plan_service, store, ontology))
