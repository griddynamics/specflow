"""Diff: task matching, the task status table, cascade, stories, relationships and the plan invariants."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from graph.core.errors import ConformanceError
from graph.plan.diff import DiffInputs, diff
from graph.plan.files import open_plan_store
from graph.plan.models import CurrentPlan, DiffArtifact, PartitionArtifact
from graph.plan.stitch import stitch
from graph.plan.view import PlanView
from graph_plan_support import applied_plan, next_view, planned_shop, shop_view

INPUTS = DiffInputs(ontology_version="2.0", plan_ontology_version="2.0", rules_version="2.0")
COMMIT = "c" * 40
PLACE = "TASK::ORDERS_SERVICE::LOGIC_PLACE_ORDER"
EXPORT = "TASK::ORDERS_SERVICE::LOGIC_ORDER_HISTORY_EXPORT"
CHECKOUT = "TASK::WEB_SHOP::LOGIC_CHECKOUT"
API = "TASK::ORDERS_SERVICE::CONTRACT_ORDERS_API"


async def _diff(view: PlanView, tmp_path: Path, *, inputs: DiffInputs = INPUTS) -> DiffArtifact:
    shop = await planned_shop(view, open_plan_store(tmp_path / "plans"))
    stitched = stitch(view, shop.project, shop.tasks)
    return diff(view, describe=shop.describe, project=shop.project, tasks=shop.tasks, stitch=stitched, inputs=inputs)


async def _first(tmp_path: Path) -> tuple[PlanView, DiffArtifact]:
    view = await shop_view()
    return view, await _diff(view, tmp_path)


def _done(*task_ids: str) -> dict[str, dict[str, Any]]:
    return {task_id: {"status": "completed", "commit": COMMIT} for task_id in task_ids}


def _ops(artifact: DiffArtifact) -> dict[str, str]:
    return {op.id: op.op for op in artifact.tasks}


async def _second(
    tmp_path: Path, states: dict[str, dict[str, Any]], *, changed: dict[str, str] | None = None, **view: Any
) -> tuple[CurrentPlan, DiffArtifact]:
    first_view, first = await _first(tmp_path)
    current = applied_plan(first_view, first, states=states)
    return current, await _diff(next_view(first_view, current, changed=changed, **view), tmp_path)


async def test_a_first_plan_creates_every_area_story_and_task_with_links(tmp_path: Path) -> None:
    view, artifact = await _first(tmp_path)

    assert {op.op for op in (*artifact.tasks, *artifact.areas, *artifact.stories)} == {"create"}
    assert all(op.properties["status"] == "pending" for op in artifact.tasks)
    in_area = {link.source for link in artifact.links_create if link.type == "IN_AREA"}
    assert in_area == {node.uuid for node in view.snapshot.plannable}
    assert artifact.links_delete == ()
    key = f"{PLACE}|IN_STORY|STORY::ORDERS_SERVICE::PLACE_ORDER"
    assert key in {link.key for link in artifact.links_create}
    assert artifact.plan_properties["last_build_id"] == "build-2"
    assert set(artifact.summary.tasks_added) == {op.id for op in artifact.tasks}


async def test_an_unchanged_second_run_keeps_completed_work_and_writes_no_link(tmp_path: Path) -> None:
    _, artifact = await _second(tmp_path, _done(PLACE))

    assert PLACE not in _ops(artifact) and PLACE in artifact.summary.tasks_kept
    assert artifact.tasks == ()
    assert artifact.links_create == () and artifact.links_delete == ()
    assert artifact.stale_commits == {}


async def test_a_meaning_change_supersedes_completed_work_with_a_new_revision(tmp_path: Path) -> None:
    _, artifact = await _second(tmp_path, _done(PLACE), changed={"REQ::FR::PLACE_ORDER": "changed_meaning"})

    ops = _ops(artifact)
    assert ops[PLACE] == "supersede" and ops[f"{PLACE}_R2"] == "create"
    assert artifact.summary.tasks_superseded[0].model_dump() == {
        "task_id": PLACE,
        "successor": f"{PLACE}_R2",
        "cause": "changed_meaning",
    }
    assert artifact.stale_commits == {PLACE: COMMIT}
    assert f"{PLACE}_R2|SUPERSEDES|{PLACE}" in {link.key for link in artifact.links_create}


@pytest.mark.parametrize(("status", "op"), [("failed", "reset"), ("blocked", "reset"), ("pending", "update")])
async def test_open_work_whose_spec_changed_is_set_back_to_pending(tmp_path: Path, status: str, op: str) -> None:
    states = {PLACE: {"status": status, "block_reason": "spec_missing" if status == "blocked" else None}}

    _, artifact = await _second(tmp_path, states, changed={"REQ::FR::PLACE_ORDER": "changed_meaning"})

    write = next(item for item in artifact.tasks if item.id == PLACE)
    assert write.op == op and write.expected_status == status
    assert op == "update" or write.properties["status"] == "pending"


async def test_in_progress_work_is_never_touched_but_listed_when_its_spec_changed(tmp_path: Path) -> None:
    claim = {"status": "in_progress", "claimed_by": "agent", "claimed_at": "2026-01-01T12:00:00+00:00"}

    _, artifact = await _second(tmp_path, {PLACE: claim}, changed={"REQ::FR::PLACE_ORDER": "changed_meaning"})

    assert PLACE not in _ops(artifact)
    assert artifact.in_progress_changed == (PLACE,)
    assert not [link for link in (*artifact.links_create, *artifact.links_delete) if PLACE in (link.source, link.key)]


async def test_work_with_no_target_is_retired_and_its_commit_listed_as_stale(tmp_path: Path) -> None:
    first_view, first = await _first(tmp_path)
    current = applied_plan(first_view, first, states=_done(EXPORT))
    nodes = tuple(
        node
        for node in first_view.snapshot.plannable
        if node.spec_id not in {"FEAT::ORDER_HISTORY_EXPORT", "REQ::FR::ORDER_HISTORY_EXPORT", "REQ::LOGIC_EXPORT"}
    )
    view = next_view(
        first_view,
        current,
        changed={"REQ::FR::ORDER_HISTORY_EXPORT": "removed"},
        snapshot_updates={"plannable": nodes},
    )

    artifact = await _diff(view, tmp_path)

    assert _ops(artifact)[EXPORT] == "retire"
    assert artifact.stale_commits == {EXPORT: COMMIT}
    assert "STORY::ORDERS_SERVICE::ORDER_HISTORY_EXPORT" in artifact.summary.stories_retired


async def test_returning_retired_work_is_revived_or_redone_with_a_new_revision(tmp_path: Path) -> None:
    _, revived = await _second(tmp_path, {PLACE: {"status": "retired"}})
    _, redone = await _second(tmp_path, {PLACE: {"status": "retired", "commit": COMMIT}})

    assert _ops(revived)[PLACE] == "revive"
    assert _ops(redone)[f"{PLACE}_R2"] == "create" and PLACE not in _ops(redone)


async def test_a_renamed_action_with_the_same_implements_keeps_the_current_task(tmp_path: Path) -> None:
    first_view, first = await _first(tmp_path)
    renamed = [
        op.model_copy(
            update={
                "id": "TASK::ORDERS_SERVICE::OLD",
                "properties": {**op.properties, "task_id": "TASK::ORDERS_SERVICE::OLD", "action": "OLD"},
            }
        )
        if op.id == PLACE
        else op
        for op in first.tasks
    ]
    current = applied_plan(
        first_view, first.model_copy(update={"tasks": tuple(renamed)}), states=_done("TASK::ORDERS_SERVICE::OLD")
    )

    artifact = await _diff(next_view(first_view, current), tmp_path)

    assert "TASK::ORDERS_SERVICE::OLD" in artifact.summary.tasks_kept
    assert PLACE not in _ops(artifact) and PLACE not in artifact.summary.tasks_added


async def test_redone_work_cascades_to_completed_planner_dependants_inside_the_area(tmp_path: Path) -> None:
    states = _done(API, PLACE)
    first_view, first = await _first(tmp_path)
    current = applied_plan(first_view, first, states=states)
    planner_wait = next(
        link for link in current.links if link.type == "WAITS_FOR" and dict(link.properties)["source"] == "planner"
    )
    states = _done(planner_wait.source, planner_wait.target)
    current = applied_plan(first_view, first, states=states)
    prerequisite = next(task for task in current.tasks if task.task_id == planner_wait.target)

    artifact = await _diff(
        next_view(first_view, current, changed=dict.fromkeys(prerequisite.implements, "changed_meaning")), tmp_path
    )

    causes = {item.task_id: item.cause for item in artifact.summary.tasks_superseded}
    assert causes[planner_wait.target] == "changed_meaning"
    assert causes[planner_wait.source] == "cascade"


async def test_a_superseded_cross_area_prerequisite_leaves_completed_dependants_listed(tmp_path: Path) -> None:
    current, artifact = await _second(tmp_path, _done(API, CHECKOUT), changed={"IFC::ORDERS_API": "changed_meaning"})

    assert CHECKOUT in artifact.summary.tasks_kept
    assert artifact.affected_dependants == (CHECKOUT,)
    assert f"{CHECKOUT}|WAITS_FOR|{API}_R2" in {link.key for link in artifact.links_create}
    assert any(link.target == API and link.source == CHECKOUT for link in artifact.links_delete)
    assert current.plan is not None


async def test_a_replan_without_reports_keeps_completed_work_it_cannot_verify(tmp_path: Path) -> None:
    first_view, first = await _first(tmp_path)
    current = applied_plan(first_view, first, states=_done(PLACE))
    nodes = tuple(
        node.model_copy(update={"last_build_id": "build-9"}) if node.spec_id == "REQ::FR::PLACE_ORDER" else node
        for node in first_view.snapshot.plannable
    )
    view = next_view(
        first_view,
        current,
        mode="replan",
        changed={"REQ::FR::PLACE_ORDER": "changed_meaning"},
        snapshot_updates={"plannable": nodes},
    )

    artifact = await _diff(view, tmp_path, inputs=DiffInputs("2.0", "2.0", "2.0", replan_without_reports=True))

    assert PLACE in artifact.summary.tasks_kept
    assert artifact.unverified_completed == (PLACE,)


async def test_a_target_that_breaks_an_invariant_is_refused(tmp_path: Path) -> None:
    view = await shop_view()
    placements = {key: value for key, value in view.partition.placements.items() if key != "REQ::FR::PLACE_ORDER"}
    broken_partition: PartitionArtifact = view.partition.model_copy(update={"placements": placements})
    broken = PlanView(view.snapshot, broken_partition, view.rules)

    with pytest.raises(ConformanceError) as raised:
        await _diff(broken, tmp_path)

    assert raised.value.code == "plan_invariant" and raised.value.fields["check"] == "membership"


async def test_an_area_holding_claimed_work_stays_active_after_its_anchor_left(tmp_path: Path) -> None:
    first_view, first = await _first(tmp_path)
    claim = {"status": "in_progress", "claimed_by": "agent", "claimed_at": "2026-01-01T12:00:00+00:00"}
    current = applied_plan(first_view, first, states={CHECKOUT: claim})
    gone = {"MOD::WEB_SHOP", "FEAT::CHECKOUT", "REQ::FR::CHECKOUT", "REQ::Q::CHECKOUT_SPEED"}
    nodes = tuple(node for node in first_view.snapshot.plannable if node.spec_id not in gone)
    view = next_view(first_view, current, changed=dict.fromkeys(gone, "removed"), snapshot_updates={"plannable": nodes})

    artifact = await _diff(view, tmp_path)

    assert "AREA::WEB_SHOP" not in {op.id for op in artifact.areas if op.op == "retire"}
    assert CHECKOUT not in _ops(artifact) and CHECKOUT in artifact.in_progress_changed


async def test_a_kept_completed_task_whose_story_changed_is_written(tmp_path: Path) -> None:
    first_view, first = await _first(tmp_path)
    moved = [
        op.model_copy(update={"properties": {**op.properties, "title": "An older title"}}) if op.id == PLACE else op
        for op in first.tasks
    ]
    current = applied_plan(first_view, first.model_copy(update={"tasks": tuple(moved)}), states=_done(PLACE))

    artifact = await _diff(next_view(first_view, current), tmp_path)

    write = next(op for op in artifact.tasks if op.id == PLACE)
    assert write.op == "keep" and write.properties["title"] != "An older title"
    assert set(write.properties) == {
        "area_id",
        "title",
        "description",
        "done_when",
        "story_id",
        "release",
        "spec_build_id",
    }


async def test_an_unchanged_open_task_is_not_written_again(tmp_path: Path) -> None:
    _, artifact = await _second(tmp_path, {})

    assert artifact.tasks == () and artifact.summary.tasks_updated == ()
