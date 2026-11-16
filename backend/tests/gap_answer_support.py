"""Answers on the shop gap corpus for offline tests: a full gap run with the structured loop over the recorded gateway
(docs/graph/gap/answers.md), then a ``GapService`` with every write port over the same stores.

``AnswerWorld`` keeps the run's world, the service, the build requests it made, the plan's holds and the clock; the
planted gaps are found by their ``ABOUT`` nodes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from gap_acceptance_support import Planted
from gap_investigation_support import loop_world
from gap_port_fakes import MemoryBuildRequests, MemoryStoryHolds
from gap_run_support import GapWorld
from gap_support import Clock
from graph.gap import run_gap
from graph.gap.models import Edit, EditTarget, GapView
from graph.gap.service import GapService
from graph.gap.settings import GapSettings

ORDERS_REVIEW = "upload:orders-spec#orders-service-specification/order-review#2abe6ad8"
PAYMENTS_REVIEW = "upload:payments-spec#payments-service-specification/payment-review#8a2d474f"
PLACE = "upload:orders-spec#orders-service-specification/place-order#e1486afe"
ORDERS_LINEAGE = "upload:orders-spec"
PAYMENTS_LINEAGE = "upload:payments-spec"
MEMBER = "user-member-2"


class ScriptedEdits:
    """A gateway whose ``gap_edits`` call answers ``edits`` (or raises ``error``); records the prompts it saw."""

    def __init__(self, edits: Sequence[Mapping[str, Any]] = (), error: Exception | None = None) -> None:
        self.edits = list(edits)
        self.error = error
        self.prompts: list[str] = []
        self.messages: list[Sequence[Mapping[str, Any]]] = []

    async def structured(
        self,
        call_class: str,
        model: str | None,
        messages: Sequence[Mapping[str, Any]],
        output_type: Any,
        *,
        prompt: str,
    ) -> Any:
        del call_class, model
        self.prompts.append(prompt)
        self.messages.append(messages)
        if self.error is not None:
            raise self.error
        return output_type.model_validate({"edits": self.edits})


@dataclass
class AnswerWorld:
    world: GapWorld
    service: GapService
    builds: MemoryBuildRequests
    holds: MemoryStoryHolds
    clock: Clock
    gateway: Any = None
    gaps: dict[str, GapView] = field(default_factory=dict)

    @property
    def project_id(self) -> str:
        return self.world.project_id

    async def gap_about(self, *spec_ids: str) -> GapView:
        """The open gap ``ABOUT`` exactly ``spec_ids``, read with options."""

        page = await self.service.list_gaps(self.project_id, status=("open", "answered", "applying", "resolved"))
        found = next(gap for gap in page.gaps if {node.spec_id for node in gap.about} == set(spec_ids))
        return await self.service.get_gap(self.project_id, found.gap_id)

    async def cancel_gap(self) -> GapView:
        return await self.gap_about(Planted.STATED_QUESTION, Planted.PLACE_ORDER)

    async def review_gap(self) -> GapView:
        return await self.gap_about(Planted.REVIEW_LIMIT_PAYMENTS, Planted.REVIEW_LIMIT_ORDERS)


def service_of(world: GapWorld, **overrides: Any) -> tuple[GapService, MemoryBuildRequests, MemoryStoryHolds, Clock]:
    builds = overrides.pop("builds", MemoryBuildRequests())
    holds = overrides.pop("holds", MemoryStoryHolds())
    clock = overrides.pop("clock", Clock())
    service = GapService(
        world.executor,
        world.reader,
        world.ontology,
        world.rules,
        world.records,
        world.amendments,
        builds=builds,
        holds=holds,
        clock=clock,
        settings=overrides.pop("settings", GapSettings()),
        **overrides,
    )
    return service, builds, holds, clock


async def answer_world(*, gateway: Any = None, **overrides: Any) -> AnswerWorld:
    """The corpus after one full gap run (8 open gaps with options) and a service over its stores."""

    world = await loop_world()
    await run_gap(world.context(), mode="full")
    service, builds, holds, clock = service_of(world, gateway=gateway, **overrides)
    return AnswerWorld(world, service, builds, holds, clock, gateway)


def amend(fragment_id: str, anchor: str, text: str, operation: str = "replace") -> Edit:
    return Edit.model_validate(
        {
            "placement": "amend",
            "target": EditTarget(fragment_id=fragment_id, anchor_quote=anchor),
            "operation": operation,
            "text": text,
        }
    )
