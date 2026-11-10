"""A small persisted plan for the plan read tests (``graph.plan.service`` and the MCP plan tools), built in memory.

Orders: ``A`` (completed) <- ``B`` <- ``C`` in the story that anchors ``FEAT::PLACE``; ``D`` of the rules in
another orders story; payments ``E`` implements ``REQ::PAY_ON_PLACE``, a node under ``FEAT::PLACE``; ``OLD`` is
superseded history. Ready: ``B``, ``D`` and ``E``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from graph.core.reader import GraphNode
from graph.plan.models import CurrentPlan, CurrentTask, PlanInfo, PlanLink, StoryInfo, WorkAreaInfo
from graph.plan.reader import LoadedPlan, spec_aliases
from graph_support import shipped_ontology

T0 = datetime(2026, 1, 1, tzinfo=UTC)
READY = ("TASK::ORDERS::B", "TASK::ORDERS::D", "TASK::PAY::E")
OPEN_TASK_IDS = ("TASK::ORDERS::A", "TASK::ORDERS::B", "TASK::ORDERS::C", "TASK::ORDERS::D", "TASK::PAY::E")


def _task(task_id: str, status: str = "pending", **values: Any) -> CurrentTask:
    defaults: dict[str, Any] = {
        "task_id": task_id,
        "area_id": "AREA::ORDERS",
        "action": task_id.rsplit("::", 1)[-1],
        "revision": 1,
        "story_id": "STORY::ORDERS::PLACE",
        "phase": "logic",
        "title": f"Task {task_id}",
        "complexity": "medium",
        "planned_by": "model",
        "status": status,
        "created_at": T0,
        "updated_at": T0,
    }
    return CurrentTask(**(defaults | values))


TASKS = (
    _task("TASK::ORDERS::A", "completed", phase="setup"),
    _task("TASK::ORDERS::B"),
    _task("TASK::ORDERS::C"),
    _task("TASK::ORDERS::D", planned_by="rules", story_id="STORY::ORDERS::TRACK"),
    _task("TASK::PAY::E", area_id="AREA::PAY", story_id="STORY::PAY::CORE", implements=("REQ::PAY_ON_PLACE",)),
    _task("TASK::ORDERS::OLD", "superseded"),
)
WAITS = (("TASK::ORDERS::B", "TASK::ORDERS::A"), ("TASK::ORDERS::C", "TASK::ORDERS::B"))


def shop_plan(*, has_plan: bool = True) -> LoadedPlan:
    """The plan above, or an empty one without a ``Plan`` node."""

    if not has_plan:
        return LoadedPlan(CurrentPlan(plan=None))
    areas = tuple(
        WorkAreaInfo(
            area_id=area_id,
            kind="module",
            title=title,
            planned_by="model",
            status="active",
            created_at=T0,
            updated_at=T0,
        )
        for area_id, title in (("AREA::ORDERS", "Orders"), ("AREA::PAY", "Payments"))
    )
    stories = tuple(
        StoryInfo(
            story_id=story_id,
            area_id=area_id,
            kind="feature",
            title=title,
            anchor=anchor,
            status="active",
            created_at=T0,
            updated_at=T0,
        )
        for story_id, area_id, title, anchor in (
            ("STORY::ORDERS::PLACE", "AREA::ORDERS", "Place an order", "FEAT::PLACE"),
            ("STORY::ORDERS::TRACK", "AREA::ORDERS", "Track an order", "FEAT::TRACK"),
            ("STORY::PAY::CORE", "AREA::PAY", "Payments", "FEAT::PAY"),
        )
    )
    links = tuple(
        PlanLink(type="WAITS_FOR", source=source, target=target, edge_key=f"{source}>{target}")
        for source, target in WAITS
    )
    return LoadedPlan(
        CurrentPlan(
            plan=PlanInfo(plan_id="plan-1", created_at=T0, updated_at=T0),
            areas=areas,
            stories=stories,
            tasks=TASKS,
            links=links,
        )
    )


class ShopGraph:
    """The spec graph reads of the plan service: ``FEAT::PLACE`` (alias ``FEAT::ORDER``) with ``REQ::PAY_ON_PLACE``
    under it."""

    def __init__(self) -> None:
        feature = shipped_ontology().node_types["feature"].label
        self.by_id = {
            "FEAT::PLACE": GraphNode(
                spec_id="FEAT::PLACE",
                uuid="u1",
                label=feature,
                name="Place an order",
                spec_id_aliases=("FEAT::ORDER",),
            ),
            "REQ::PAY_ON_PLACE": GraphNode(spec_id="REQ::PAY_ON_PLACE", uuid="u2", label="Requirement", name="Pay"),
        }

    async def node(self, _project_id: str, spec_id: str) -> GraphNode | None:
        found = self.by_id.get(spec_id)
        return found or next((node for node in self.by_id.values() if spec_id in node.spec_id_aliases), None)

    async def nodes(self, _project_id: str) -> list[GraphNode]:
        return list(self.by_id.values())

    async def spec_aliases(self, _project_id: str) -> dict[str, str]:
        return spec_aliases(self.by_id.values())

    async def scope(self, _project_id: str, spec_id: str) -> list[GraphNode]:
        return [self.by_id["REQ::PAY_ON_PLACE"]] if spec_id == "FEAT::PLACE" else []
