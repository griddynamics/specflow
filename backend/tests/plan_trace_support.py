"""Offline fixtures of the plan overlay tests: the shop fixture's spec graph behind the ``PlanService`` graph port,
and a small persisted plan whose tasks implement the nodes of ``trace.md``'s worked example."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from graph.core import GraphReader
from graph.core.reader import GraphEdge, GraphNode, Statement
from graph.plan.models import CurrentPlan, CurrentTask, PlanInfo, PlanLink, StoryInfo, WorkAreaInfo
from graph.plan.reader import LoadedPlan
from graph_plan_support import plan_ontology
from graph_support import OfflineExecutor
from graph_trace_support import TraceExecutor, TraceGraph, shop_graph

T0 = datetime(2026, 1, 1, tzinfo=UTC)
PROJECT = "shop"


class ShopSpecGraph:
    """What ``PlanService`` reads of the spec graph (``nodes``, ``edges``, ``trace``) over the shop fixture."""

    def __init__(self, graph: TraceGraph | None = None) -> None:
        self.graph = graph or shop_graph()
        self.reader = GraphReader(TraceExecutor(self.graph), plan_ontology())

    async def nodes(self, _project_id: str) -> list[GraphNode]:
        return [
            GraphNode(
                spec_id=spec_id,
                uuid=node["uuid"],
                label=node["label"],
                name=node["name"],
                last_build_id="build-1",
                spec_id_aliases=tuple(node["aliases"]),
                statements=(Statement(fragment_id="f1", text=node["name"]),),
            )
            for spec_id, node in sorted(self.graph.nodes.items())
        ]

    async def edges(self, _project_id: str, **_options: Any) -> list[GraphEdge]:
        return [
            GraphEdge(
                relation=edge["relation"],
                source=edge["source"],
                target=edge["target"],
                basis=edge["props"].get("basis", "stated"),
            )
            for edge in self.graph.edges
        ]

    async def node(self, project_id: str, spec_id: str) -> GraphNode | None:
        return next((node for node in await self.nodes(project_id) if spec_id == node.spec_id), None)

    async def scope(self, _project_id: str, _spec_id: str) -> list[GraphNode]:
        return []

    async def trace(self, project_id: str, spec_ids: Any, **options: Any) -> Any:
        return await self.reader.trace(project_id, spec_ids, **options)


def task(
    task_id: str, story_id: str, implements: tuple[str, ...], status: str = "pending", **values: Any
) -> CurrentTask:
    area = story_id.split("::")[1]
    base: dict[str, Any] = {
        "task_id": task_id,
        "area_id": f"AREA::{area}",
        "action": task_id.rsplit("::", 1)[-1],
        "revision": 1,
        "story_id": story_id,
        "phase": "logic",
        "title": f"Task {task_id}",
        "complexity": "medium",
        "planned_by": "model",
        "status": status,
        "implements": implements,
        "created_at": T0,
        "updated_at": T0,
    }
    return CurrentTask(**(base | values))


COMMIT = "ab" * 20
CHECKOUT, STRIPE, PAYMENTS = "STORY::WEB_SHOP::CHECKOUT", "STORY::STRIPE::CORE", "STORY::PAYMENTS_SERVICE::CARD_PAYMENT"
STORIES = (
    (CHECKOUT, "Checkout", "FEAT::CHECKOUT"),
    (STRIPE, "Stripe integration", None),
    (PAYMENTS, "Card payment", "FEAT::CARD_PAYMENT"),
)
TASKS = (
    task("TASK::WEB_SHOP::LOGIC_CHECKOUT", CHECKOUT, ("FEAT::CHECKOUT", "REQ::FR::CHECKOUT", "REQ::Q::CHECKOUT_SPEED")),
    task("TASK::WEB_SHOP::DATA_CHECKOUT", CHECKOUT, ("FEAT::CHECKOUT",), "completed", commit=COMMIT, claimed_by="u9"),
    task("TASK::STRIPE::SETUP", STRIPE, ("MOD::STRIPE",), "in_progress", claimed_by="u2", commit=COMMIT),
    task("TASK::STRIPE::WIRE", STRIPE, ("MOD::STRIPE",), "in_progress", claimed_by="u3"),
    task("TASK::STRIPE::OLD", STRIPE, ("MOD::WEB_SHOP",), "superseded"),
    task(
        "TASK::PAYMENTS::CONTRACT", PAYMENTS, ("IFC::STRIPE_API", "MOD::PAYMENTS_SERVICE"), "completed", commit=COMMIT
    ),
    task("TASK::PAYMENTS::UNSHARED", PAYMENTS, ("IFC::UNSHARED",), "blocked", block_reason="spec_missing"),
    task("TASK::PAYMENTS::LOGIC", PAYMENTS, ("FEAT::CARD_PAYMENT", "REQ::FR::CARD_PAYMENT", "CON::STRIPE")),
)
WAITS = (
    ("TASK::WEB_SHOP::LOGIC_CHECKOUT", "TASK::WEB_SHOP::DATA_CHECKOUT"),
    ("TASK::WEB_SHOP::LOGIC_CHECKOUT", "TASK::PAYMENTS::CONTRACT"),
)


def shop_plan(*, tasks: tuple[CurrentTask, ...] = TASKS, retired: tuple[str, ...] = ()) -> LoadedPlan:
    areas = tuple(
        WorkAreaInfo(
            area_id=f"AREA::{name}",
            kind="module",
            title=name,
            planned_by="model",
            status="active",
            created_at=T0,
            updated_at=T0,
        )
        for name in ("WEB_SHOP", "STRIPE", "PAYMENTS_SERVICE")
    )
    stories = tuple(
        StoryInfo(
            story_id=story_id,
            area_id=f"AREA::{story_id.split('::')[1]}",
            kind="feature",
            title=title,
            anchor=anchor,
            status="retired" if story_id in retired else "active",
            created_at=T0,
            updated_at=T0,
        )
        for story_id, title, anchor in STORIES
    )
    links = tuple(PlanLink(type="WAITS_FOR", source=a, target=b, edge_key=f"{a}>{b}") for a, b in WAITS)
    plan = PlanInfo(plan_id="PLAN::MAIN", consumed_build_ids=("build-1",), created_at=T0, updated_at=T0)
    return LoadedPlan(CurrentPlan(plan=plan, areas=areas, stories=stories, tasks=tasks, links=links))


def offline() -> OfflineExecutor:
    return OfflineExecutor()
