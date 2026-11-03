"""``estimate_plan``: calls and input tokens without a model call or a write; reused answers count zero."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from graph.plan.describe import describe
from graph.plan.estimate import estimate_with
from graph.plan.files import open_plan_store
from graph.plan.models import PlanOptions
from graph.plan.options import PlanContext
from graph.plan.ports import PlanStore
from graph.plan.project import project
from graph.plan.tasks import plan_tasks
from graph_plan_support import (
    SnapshotQueries,
    plan_ontology,
    plan_rules,
    planning_context,
    script_plan,
    shop_answers,
    shop_graph,
    shop_view,
)
from graph_support import ScriptedGateway


def _context(store: PlanStore, gateway: ScriptedGateway, *, options: PlanOptions | None = None) -> PlanContext:
    ontology = plan_ontology()
    fields: dict[str, Any] = {
        "project_id": "shop",
        "ontology": ontology,
        "rules": plan_rules(ontology),
        "driver": None,
        "gateway": gateway,
        "store": store,
        "build_id": "build-2",
        "options": options or PlanOptions(),
        "query_executor": SnapshotQueries(),
    }
    return PlanContext(**fields)


async def test_an_estimate_counts_one_call_per_area_part_and_planner_without_calling_or_writing(tmp_path: Path) -> None:
    store = open_plan_store(tmp_path / "plans")
    gateway = ScriptedGateway()
    view = await shop_view()

    estimate = await estimate_with(_context(store, gateway), shop_graph())

    assert gateway.calls == []
    assert not (tmp_path / "plans").exists() or not any((tmp_path / "plans").rglob("*.gz"))
    assert estimate.mode == "first"
    assert estimate.steps["describe"].calls == len(view.active_areas)
    assert estimate.steps["project"].calls == 1
    assert estimate.steps["tasks"].calls == len(view.partition.parts)
    assert estimate.steps["review"].calls == 1
    assert all(step.input_tokens > 0 for step in estimate.steps.values())


async def test_stored_answers_count_as_no_call_and_review_off_counts_nothing(tmp_path: Path) -> None:
    store = open_plan_store(tmp_path / "plans")
    view = await shop_view()
    gateway = script_plan(ScriptedGateway(), shop_answers(view))
    ctx = planning_context(view, store, gateway)
    described = await describe(ctx, view, mode="first")
    await plan_tasks(ctx, view, described, await project(ctx, view, described, mode="first"), mode="first")

    estimate = await estimate_with(_context(store, ScriptedGateway(), options=PlanOptions(review=False)), shop_graph())

    assert estimate.steps["describe"].calls == 0
    assert estimate.steps["project"].calls == 0
    assert estimate.steps["tasks"].calls == 0
    assert estimate.steps["review"].calls == 0
