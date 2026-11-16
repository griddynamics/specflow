"""The agent routes over the shop gap corpus after a gap run (FR-GPLN-02, FR-GWEB-03; docs/graph/gap/lifecycle.md
"Plan link").

``serve_gaps`` runs one offline ``full`` gap run on the shop gap corpus for project ``ALPHA`` (recorded gateway, seed
investigator: no model call), builds the real ``GapService`` over its stores and serves the API with:

- ``Runtime.gaps``: a stand-in runner whose ``service()`` is that ``GapService`` (the routes and the findings port
  read gaps through it, as through ``api.gap.runner.GapRunner``);
- ``Runtime.agent_reads``: the shop gap corpus reader and a ``PlanService`` over the claim tests' mini plan
  (``plan_claims_support.mini_plan``) whose task ``TASK::A::API_LOGIC`` of story ``STORY::A::API`` implements
  ``REQ::FR::PLACE_ORDER``; its findings port is the host's ``OpenGaps`` (no first-slice findings store).

The corpus has no plan of its own (fixture README); the mini plan is that plan, and says so here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

from api.agent_reads import AgentReads
from api.gap.findings import OpenGaps
from gap_acceptance_support import Planted, gap_corpus_graph
from gap_run_support import GapWorld, corpus_world
from graph.gap import GapService, run_gap
from graph.plan import PlanService
from graph_plan_support import plan_ontology, plan_rules
from plan_claims_support import mini_plan
from specflow_api_agent_support import ALPHA, AgentRig, serve_agent

__all__ = ["ALPHA", "API_LOGIC", "API_STORY", "QUESTION", "GapRig", "ServiceRunner", "serve_gaps"]

API_STORY = "STORY::A::API"
API_LOGIC = "TASK::A::API_LOGIC"
QUESTION = "Can a customer cancel a paid order?"  # the stated open question of the corpus (orders-spec.md)


class ServiceRunner:
    """Stands in for ``api.gap.runner.GapRunner`` where the routes and the findings port use it: ``service()``."""

    def __init__(self, service: GapService) -> None:
        self._service = service

    async def service(self) -> GapService:
        return self._service


@dataclass
class GapRig:
    rig: AgentRig
    world: GapWorld
    gaps: GapService
    plan: PlanService
    port: OpenGaps

    async def get(self, path: str, **params: object) -> object:
        return await self.rig.get(path, **params)

    def question_gap(self) -> str:
        """The id of the gap of the stated question about cancelling a paid order (``ABOUT`` Place order)."""

        return next(
            gap_id
            for gap_id, props in self.world.gap_nodes().items()
            if props["kind"] == "open_question" and Planted.PLACE_ORDER in self.world.about(gap_id)
        )


@asynccontextmanager
async def serve_gaps(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> AsyncIterator[GapRig]:
    world = await corpus_world(project_id=ALPHA)
    await run_gap(world.context(), mode="full")
    gaps = GapService(world.executor, world.reader, world.ontology, world.rules, world.records, world.amendments)
    store = mini_plan(ALPHA)
    store.set_task(API_LOGIC, implements=[Planted.PLACE_ORDER])
    ontology = plan_ontology()
    async with serve_agent(monkeypatch, env) as rig:
        runner = ServiceRunner(gaps)
        port = OpenGaps(None, lambda: runner)
        plan = PlanService(store, ontology, plan_rules(ontology), graph=gap_corpus_graph().reader, findings=port)
        rig.runtime.gaps = runner
        rig.runtime.agent_reads = AgentReads(
            ontology=ontology, reader=world.reader, plan=plan, findings=None, open_findings=port
        )
        yield GapRig(rig, world, gaps, plan, port)
