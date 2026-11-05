"""Stitch: setup, interface, integration, dependency, data, deployment and closing waits; cycle breaking."""

from __future__ import annotations

from pathlib import Path

from graph.plan.dag import find_cycle
from graph.plan.files import open_plan_store
from graph.plan.models import PlannedTask, SpecEdgeView, StitchArtifact
from graph.plan.stitch import stitch
from graph.plan.view import PlanView
from graph_plan_support import PlannedShop, planned_shop, shop_view


async def _stitched(tmp_path: Path, *extra: SpecEdgeView) -> tuple[PlannedShop, StitchArtifact]:
    view = await shop_view()
    if extra:
        snapshot = view.snapshot.model_copy(update={"edges": (*view.snapshot.edges, *extra)})
        view = PlanView(snapshot, view.partition, view.rules)
    shop = await planned_shop(view, open_plan_store(tmp_path / "plans"))
    return shop, stitch(view, shop.project, shop.tasks)


def _waits(artifact: StitchArtifact, rule: int | None = None) -> set[tuple[str, str]]:
    return {(wait.source, wait.target) for wait in artifact.waits if rule is None or wait.rule == rule}


def _edge(relation: str, source: str, target: str, basis: str = "stated") -> SpecEdgeView:
    key = f"{source}|{relation}|{target}".replace("::", "_")
    return SpecEdgeView(relation=relation, source=source, target=target, basis=basis, edge_key=key)


async def test_every_area_root_waits_for_the_setup_sinks(tmp_path: Path) -> None:
    shop, artifact = await _stitched(tmp_path)

    setup = _waits(artifact, 1)
    assert ("TASK::ORDERS_SERVICE::CONTRACT_ORDERS_API", "TASK::PROJECT::TOOLCHAIN") in setup
    assert ("TASK::WEB_SHOP::LOGIC_CHECKOUT", "TASK::PROJECT::TOOLCHAIN") in setup
    assert not [wait for wait in setup if wait[0] in {"TASK::PROJECT::TOOLCHAIN", "TASK::PROJECT::END_TO_END"}]
    assert ("TASK::PROJECT::TOOLCHAIN", "TASK::PROJECT::WORKSPACE") in _waits(artifact)
    assert {wait.kind for wait in artifact.waits if wait.rule == 0} == {"planner"}
    assert shop.project.setup


async def test_consumers_wait_for_the_contract_of_an_interface_of_another_area(tmp_path: Path) -> None:
    _, artifact = await _stitched(tmp_path)

    assert ("TASK::WEB_SHOP::LOGIC_CHECKOUT", "TASK::ORDERS_SERVICE::CONTRACT_ORDERS_API") in _waits(artifact, 2)


async def test_an_integration_without_an_interface_waits_for_the_callee_contracts(tmp_path: Path) -> None:
    _, artifact = await _stitched(tmp_path)

    integration = _waits(artifact, 3)
    assert ("TASK::WEB_SHOP::LOGIC_CHECKOUT", "TASK::PAYMENTS_SERVICE::CONTRACT_STRIPE_API") in integration
    assert ("TASK::WEB_SHOP::LOGIC_CHECKOUT", "TASK::PAYMENTS_SERVICE::CONTRACT_INTEGRATED") in integration


async def test_spec_dependencies_order_work_but_inferred_edges_never_do(tmp_path: Path) -> None:
    inferred = _edge("DEPENDS_ON", "REQ::FR::PLACE_ORDER", "REQ::FR::CARD_PAYMENT", basis="inferred")
    _, artifact = await _stitched(tmp_path, inferred)

    dependencies = _waits(artifact, 4)
    assert ("TASK::WEB_SHOP::LOGIC_CHECKOUT", "TASK::PAYMENTS_SERVICE::LOGIC_CARD_PAYMENT") in dependencies
    assert not [wait for wait in dependencies if wait[0].startswith("TASK::ORDERS_SERVICE::")]


async def test_work_on_a_node_owned_by_or_linked_to_an_entity_waits_for_its_data_task(tmp_path: Path) -> None:
    _, artifact = await _stitched(tmp_path, _edge("RELATES_TO", "REQ::FR::PLACE_ORDER", "ENT::ORDER"))

    assert ("TASK::ORDERS_SERVICE::LOGIC_PLACE_ORDER", "TASK::ORDERS_DATABASE::DATA_ORDER") in _waits(artifact, 5)


async def test_closing_work_waits_for_the_sinks_of_every_area(tmp_path: Path) -> None:
    _, artifact = await _stitched(tmp_path)

    closing = {target for source, target in _waits(artifact, 8) if source == "TASK::PROJECT::END_TO_END"}
    assert {
        "TASK::ORDERS_SERVICE::LOGIC_PLACE_ORDER",
        "TASK::WEB_SHOP::QUALITY_CHECKOUT",
        "TASK::PAYMENTS_SERVICE::LOGIC_CARD_PAYMENT",
    } <= closing


async def test_a_cycle_is_broken_by_dropping_the_last_added_stitch_wait(tmp_path: Path) -> None:
    shop, artifact = await _stitched(tmp_path, _edge("DEPENDS_ON", "FEAT::CARD_PAYMENT", "FEAT::CHECKOUT"))

    tasks = {task.task_key for task in (*shop.project.setup, *shop.project.closing, *shop.tasks.tasks)}
    assert find_cycle(tasks, _waits(artifact)) == ()
    assert len(artifact.dropped) == 1
    assert artifact.dropped[0].kind == "stitch" and artifact.dropped[0].rule == 4
    assert artifact.dropped[0].source.startswith("TASK::WEB_SHOP::")


def _task(key: str, area_id: str, phase: str, *, waits: tuple[str, ...] = ()) -> PlannedTask:
    return PlannedTask.model_validate(
        {
            "task_key": key,
            "area_id": area_id,
            "story_id": f"STORY::{area_id.split('::', 1)[1]}::DEPLOY",
            "action": key.rsplit("::", 1)[-1],
            "phase": phase,
            "title": f"Build {key}",
            "complexity": "medium",
            "planned_by": "model",
            "waits": waits,
        }
    )


async def _stitched_with(tmp_path: Path, *extra: PlannedTask) -> StitchArtifact:
    view = await shop_view()
    shop = await planned_shop(view, open_plan_store(tmp_path / "plans"))
    tasks = shop.tasks.model_copy(update={"tasks": (*shop.tasks.tasks, *extra)})
    return stitch(view, shop.project, tasks)


async def test_the_deployment_target_area_waits_for_setup_and_closing_acceptance_waits_for_it(tmp_path: Path) -> None:
    cluster = "TASK::PRODUCTION_CLUSTER::LOGIC_CLUSTER"
    check = "TASK::PRODUCTION_CLUSTER::QUALITY_CLUSTER"
    artifact = await _stitched_with(
        tmp_path,
        _task(cluster, "AREA::PRODUCTION_CLUSTER", "logic"),
        _task(check, "AREA::PRODUCTION_CLUSTER", "quality", waits=(cluster,)),
    )

    assert (cluster, "TASK::PROJECT::TOOLCHAIN") in _waits(artifact, 1)
    assert not [wait for wait in _waits(artifact, 1) if wait[0] == check]
    deployment = _waits(artifact, 6)
    assert ("TASK::PROJECT::END_TO_END", check) in deployment
    assert {target for source, target in deployment if source == "TASK::PROJECT::END_TO_END"} == {check}


async def test_an_acceptance_task_never_waits_for_another_areas_acceptance_task(tmp_path: Path) -> None:
    accept = "TASK::WEB_SHOP::ACCEPTANCE_CHECKOUT"
    artifact = await _stitched_with(
        tmp_path, _task(accept, "AREA::WEB_SHOP", "acceptance", waits=("TASK::WEB_SHOP::QUALITY_CHECKOUT",))
    )

    assert not [wait for wait in _waits(artifact) if wait == ("TASK::PROJECT::END_TO_END", accept)]
    assert (accept, "TASK::WEB_SHOP::QUALITY_CHECKOUT") in _waits(artifact)
    assert ("TASK::PROJECT::END_TO_END", "TASK::ORDERS_SERVICE::LOGIC_PLACE_ORDER") in _waits(artifact, 8)


async def test_closing_work_that_implements_an_area_node_waits_only_for_that_areas_sinks(tmp_path: Path) -> None:
    view = await shop_view()
    shop = await planned_shop(view, open_plan_store(tmp_path / "plans"))
    closing = shop.project.closing[0].model_copy(update={"implements": ("REQ::FR::CHECKOUT",)})
    project = shop.project.model_copy(update={"closing": (closing,)})

    artifact = stitch(view, project, shop.tasks)

    waited = {target for source, target in _waits(artifact) if source == closing.task_key}
    assert waited == {"TASK::WEB_SHOP::QUALITY_CHECKOUT"}
    assert {wait.rule for wait in artifact.waits if wait.source == closing.task_key} == {8}
