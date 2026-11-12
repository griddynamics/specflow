"""The rig of the people tools and the story writes (``list_areas``, ``list_stories``, ``preview_story``,
``claim_story``, ``release_story``): the API app over fakes, with the story claim tests' plan (``mini_plan``: a project
area with a completed setup story, ready, waiting, cycle and blocked stories), the REAL ``PlanService`` over the
in-memory plan executor (locks and rollback included), the findings library over an in-memory store, and the project's
members by name (Alice is the caller, Ben a member).

Faked as in ``specflow_api_agent_support``: Postgres (``FakeRepo``) and the graph store (here the plan's in-memory
executor); everything between the route and them is the product's.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Final

import pytest

from api.agent_reads import AgentReads
from gap_support import Clock, MemoryGapExecutor
from graph.core import GraphReader
from graph.core.reader import GraphNode
from graph.gap import Findings
from graph.plan import PlanService
from graph.plan.lifecycle import TaskLifecycle
from graph_plan_support import plan_ontology, plan_rules
from plan_claims_support import MemoryPlanExecutor, mini_plan
from specflow_api_agent_support import ALPHA, AgentRig, serve_agent

API: Final = "STORY::A::API"
UI: Final = "STORY::A::UI"
LATE: Final = "STORY::A::LATE"
STUCK: Final = "STORY::A::STUCK"
SETUP: Final = "STORY::P::SETUP"
X: Final = "STORY::A::X"
Y: Final = "STORY::A::Y"
API_NODE: Final = "FEAT::API"


class GapSpecs:
    """The spec graph the plan service reads besides the plan (``check_block`` resolves aliases through ``nodes``,
    ``complete_story`` marks the changed nodes through ``node``): the spec nodes of the findings' in-memory store with
    their aliases; ``builds`` gives a node the build that last wrote it (a test's "a build changed it")."""

    def __init__(self, gap: MemoryGapExecutor) -> None:
        self._gap = gap
        self.builds: dict[str, str] = {}

    def _node(self, spec_id: str) -> GraphNode:
        row = self._gap.specs[spec_id]
        return GraphNode(
            spec_id=spec_id,
            uuid=row["uuid"],
            label=row["label"],
            name=spec_id,
            last_build_id=self.builds.get(spec_id),
            spec_id_aliases=tuple(row["aliases"]),
        )

    async def nodes(self, _project_id: str) -> list[GraphNode]:
        return [self._node(spec_id) for spec_id in self._gap.specs]

    async def node(self, _project_id: str, spec_id: str) -> GraphNode | None:
        return self._node(spec_id) if spec_id in self._gap.specs else None


@dataclass
class PeopleRig:
    rig: AgentRig
    memory: MemoryPlanExecutor
    plan: PlanService
    findings: Findings
    gap: MemoryGapExecutor
    specs: GapSpecs
    alice: uuid.UUID
    ben: uuid.UUID

    async def get(self, path: str, **params: Any) -> Any:
        return await self.rig.get(path, **params)

    async def post(self, path: str, body: dict[str, Any], **params: Any) -> Any:
        return await self.rig.post(path, body, **params)

    async def claim_task(self, task_id: str, by: uuid.UUID) -> None:
        """``by`` claims one task on its own (a task claim, not a story claim)."""

        ontology = plan_ontology()
        await TaskLifecycle(self.memory, ontology, plan_rules(ontology)).claim_task(ALPHA, task_id, claimed_by=str(by))

    async def hold(self, story_id: str, by: uuid.UUID, label: str | None = None, **more: Any) -> str:
        """``by`` claims ``story_id`` through the library; the claim token."""

        claim = await self.plan.claim_story(ALPHA, story_id, claimed_by=str(by), agent_label=label, **more)
        return claim.claim_token


@asynccontextmanager
async def serve_people(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], *, memory: MemoryPlanExecutor | None = None
) -> AsyncIterator[PeopleRig]:
    """The API over the story plan of ``mini_plan(ALPHA)``; ``memory`` replaces the plan (a test's own)."""

    async with serve_agent(monkeypatch, env) as rig:
        repo = rig.repo
        alice = rig.users["alice"].id
        repo.users["user:alice@example.com"].display_name = "Alice"
        ben = await repo.upsert_user(
            None, SimpleNamespace(subject="user:ben@example.com", email="ben@example.com", name="Ben")
        )
        repo.members.add((uuid.UUID(ALPHA), ben.id))
        plan_memory = memory or mini_plan(ALPHA)
        for task_id in ("TASK::A::API_CONTRACT", "TASK::A::API_LOGIC"):
            plan_memory.set_task(task_id, implements=[API_NODE])
        ontology = plan_ontology()
        gap = MemoryGapExecutor(ALPHA)
        gap.spec(API_NODE, "Feature", [])
        counter = itertools.count(1)  # FND::1 is the finding that blocks the STUCK story of the plan
        monkeypatch.setattr("graph.gap.findings.new_finding_id", lambda: f"FND::{next(counter)}")
        findings = Findings(gap, ontology, clock=Clock())
        await findings.add(
            ALPHA,
            created_by=str(alice),
            kind="question",
            text="Which currencies does the API accept?",
            about=[API_NODE],
            story_id=STUCK,
            blocking=True,
        )
        await findings.add(
            ALPHA, created_by=str(ben.id), kind="note", text="The API is slow | very.\nReally.", about=[API_NODE]
        )
        specs = GapSpecs(gap)
        plan = PlanService(
            plan_memory, ontology, plan_rules(ontology), findings=findings, clock=Clock(), graph=specs
        )  # +1 s a call
        rig.runtime.agent_reads = AgentReads(
            ontology=ontology, reader=GraphReader(rig.executor, ontology), plan=plan, findings=findings
        )
        yield PeopleRig(rig, plan_memory, plan, findings, gap, specs, alice, ben.id)
