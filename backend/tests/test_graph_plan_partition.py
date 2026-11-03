"""Partition areas, hubs and persisted-area statuses."""

from __future__ import annotations

from datetime import UTC, datetime

from graph.plan.models import AreaPlan, CurrentPlan, WorkAreaInfo
from graph.plan.partition import partition
from graph_plan_support import plan_ontology, plan_rules, shop_partition, shop_snapshot

CREATED = datetime(2026, 1, 1, 12, tzinfo=UTC)
UPDATED = datetime(2026, 1, 2, 12, tzinfo=UTC)


async def test_internal_modules_anchor_areas_and_external_modules_are_not_placed() -> None:
    artifact = await shop_partition()

    assert {(area.area_id, area.kind, area.anchor, area.status) for area in artifact.areas} == {
        ("AREA::PROJECT", "project", None, "active"),
        ("AREA::ORDERS_SERVICE", "module", "MOD::ORDERS_SERVICE", "active"),
        ("AREA::PAYMENTS_SERVICE", "module", "MOD::PAYMENTS_SERVICE", "active"),
        ("AREA::WEB_SHOP", "module", "MOD::WEB_SHOP", "active"),
        ("AREA::ORDERS_DATABASE", "module", "MOD::ORDERS_DATABASE", "active"),
        ("AREA::PRODUCTION_CLUSTER", "module", "MOD::PRODUCTION_CLUSTER", "active"),
    }
    assert "MOD::STRIPE" not in artifact.placements


async def test_interfaces_are_placed_by_provider_or_the_single_internal_consumer() -> None:
    artifact = await shop_partition()

    assert artifact.placements["IFC::ORDERS_API"].area_id == "AREA::ORDERS_SERVICE"
    assert artifact.placements["IFC::ORDERS_API"].rule == 3
    assert artifact.placements["IFC::ORDERS_API"].via == ("IFC_ORDERS_API|PROVIDED_BY|MOD_ORDERS_SERVICE",)
    assert artifact.placements["IFC::STRIPE_API"].area_id == "AREA::PAYMENTS_SERVICE"
    assert artifact.placements["IFC::STRIPE_API"].via == ("IFC_STRIPE_API|CONSUMED_BY|MOD_PAYMENTS_SERVICE",)
    assert artifact.placements["IFC::INTEGRATED"].area_id == "AREA::PAYMENTS_SERVICE"
    assert artifact.placements["IFC::INTEGRATED"].via == ("MOD_STRIPE|INTEGRATES_WITH|MOD_PAYMENTS_SERVICE",)
    assert artifact.placements["IFC::UNSHARED"].area_id == "AREA::PROJECT"
    assert artifact.placements["IFC::UNSHARED"].via == ()


async def test_entities_are_placed_by_the_single_internal_store() -> None:
    artifact = await shop_partition()

    assert artifact.placements["ENT::ORDER"].area_id == "AREA::ORDERS_DATABASE"
    assert artifact.placements["ENT::ORDER"].rule == 4
    assert artifact.placements["ENT::ORDER"].via == ("ENT_ORDER|STORED_IN|MOD_ORDERS_DATABASE",)
    assert artifact.placements["ENT::DUAL_STORED"].area_id == "AREA::PROJECT"
    assert artifact.placements["ENT::DUAL_STORED"].via == ()


async def test_partition_is_deterministic_for_the_same_snapshot() -> None:
    snapshot = await shop_snapshot()
    ontology = plan_ontology()
    rules = plan_rules(ontology)

    assert partition(snapshot, rules, ontology) == partition(snapshot, rules, ontology)


async def test_capabilities_are_placed_by_the_single_internal_realiser() -> None:
    artifact = await shop_partition()

    assert artifact.placements["CAP::AUTHENTICATION"].area_id == "AREA::PROJECT"
    assert artifact.placements["CAP::AUTHENTICATION"].rule == 5
    assert artifact.placements["CAP::UNREALISED"].area_id == "AREA::PROJECT"


async def test_every_plannable_node_is_placed_and_shop_rule6_paths_are_correct() -> None:
    artifact = await shop_partition()
    plannable = (await shop_snapshot()).plannable

    assert set(artifact.placements) == {node.spec_id for node in plannable if node.label != "Module"}
    assert artifact.placements["REQ::FR::PLACE_ORDER"].area_id == "AREA::ORDERS_SERVICE"
    assert artifact.placements["REQ::FR::PLACE_ORDER"].rule == 6
    assert artifact.placements["REQ::FR::PLACE_ORDER"].via == (
        "REQ_FR_PLACE_ORDER|PART_OF|FEAT_PLACE_ORDER",
        "FEAT_PLACE_ORDER|PART_OF|MOD_ORDERS_SERVICE",
    )
    assert artifact.placements["REQ::INT::CALL_STRIPE"].area_id == "AREA::PAYMENTS_SERVICE"
    assert artifact.placements["REQ::INT::CALL_STRIPE"].via == (
        "REQ_INT_CALL_STRIPE|PART_OF|IFC_STRIPE_API",
        "IFC_STRIPE_API|CONSUMED_BY|MOD_PAYMENTS_SERVICE",
    )
    assert artifact.placements["REQ::DATA::ORDER_FIELDS"].area_id == "AREA::ORDERS_DATABASE"
    assert artifact.placements["CON::STRIPE"].area_id == "AREA::PAYMENTS_SERVICE"
    assert artifact.placements["CON::STRIPE"].rule == 6
    assert artifact.placements["CON::EU_REGION"].area_id == "AREA::PROJECT"
    assert artifact.placements["CON::EU_REGION"].rule == 6
    assert artifact.counts["cross_cutting"] == 4
    assert artifact.counts["unplaced"] == 5
    assert artifact.counts["ambiguous_membership"] == 0


async def test_rule6_uses_grouping_then_reference_and_counts_unplaced() -> None:
    snapshot = await shop_snapshot()
    nodes = tuple(snapshot.plannable)
    grouped = next(node for node in nodes if node.spec_id == "BR::RELEASE")
    grouped = grouped.model_copy(update={"spec_id": "BR::GROUPED", "uuid": "node-0100"})
    orphan = grouped.model_copy(update={"spec_id": "BR::ORPHAN", "uuid": "node-0101"})
    edges = [edge for edge in snapshot.edges if edge.source != "BR::RELEASE"]
    edge_template = edges[0]
    edges.append(
        edge_template.model_copy(
            update={
                "relation": "GROUPED_BY",
                "source": "BR::GROUPED",
                "target": "CAP::SINGLE",
                "edge_key": "BR_GROUPED|GROUPED_BY|CAP_SINGLE",
            }
        )
    )
    edges.append(
        edge_template.model_copy(
            update={
                "relation": "REALIZED_BY",
                "source": "CAP::SINGLE",
                "target": "MOD::ORDERS_SERVICE",
                "edge_key": "CAP_SINGLE|REALIZED_BY|MOD_ORDERS_SERVICE",
            }
        )
    )
    edges = [edge for edge in edges if edge.source != "CAP::AUTHENTICATION"]
    capability = next(node for node in nodes if node.spec_id == "CAP::AUTHENTICATION")
    capability = capability.model_copy(update={"spec_id": "CAP::SINGLE", "uuid": "node-0102"})
    snapshot = snapshot.model_copy(
        update={
            "plannable": (*nodes, grouped, orphan, capability),
            "edges": tuple(edges),
        }
    )

    artifact = partition(snapshot, plan_rules(plan_ontology()), plan_ontology())

    assert artifact.placements["BR::GROUPED"].area_id == "AREA::ORDERS_SERVICE"
    assert artifact.placements["BR::ORPHAN"].area_id == "AREA::PROJECT"
    assert artifact.counts["unplaced"] == 8


async def test_rule6_counts_ties_to_the_lowest_anchor_as_ambiguous_membership() -> None:
    snapshot = await shop_snapshot()
    nodes = tuple(snapshot.plannable)
    requirement = next(node for node in nodes if node.spec_id == "REQ::FR::A")
    requirement = requirement.model_copy(update={"spec_id": "REQ::AMBIGUOUS", "uuid": "node-0103"})
    edges = [edge for edge in snapshot.edges if edge.source not in {"REQ::FR::A", "REQ::FR::B"}]
    edge_template = edges[0]
    edges.extend(
        edge_template.model_copy(
            update={
                "relation": "PART_OF",
                "source": "REQ::AMBIGUOUS",
                "target": target,
                "edge_key": f"REQ_AMBIGUOUS|PART_OF|{target.replace('::', '_')}",
            }
        )
        for target in ("MOD::ORDERS_SERVICE", "MOD::WEB_SHOP")
    )
    snapshot = snapshot.model_copy(update={"plannable": (*nodes, requirement), "edges": tuple(edges)})

    artifact = partition(snapshot, plan_rules(plan_ontology()), plan_ontology())

    assert artifact.placements["REQ::AMBIGUOUS"].area_id == "AREA::ORDERS_SERVICE"
    assert artifact.placements["REQ::AMBIGUOUS"].rule == 6
    assert artifact.counts["ambiguous_membership"] == 1


async def test_a_persisted_area_is_retired_when_its_anchor_is_gone() -> None:
    persisted = CurrentPlan(
        plan=None,
        areas=(
            WorkAreaInfo(
                area_id="AREA::REMOVED",
                kind="module",
                anchor="MOD::REMOVED",
                planned_by="rules",
                status="active",
                created_at=CREATED,
                updated_at=UPDATED,
            ),
        ),
    )
    snapshot = await shop_snapshot()
    snapshot = snapshot.model_copy(update={"current": persisted})
    ontology = plan_ontology()
    artifact = partition(snapshot, plan_rules(ontology), ontology)

    assert (
        AreaPlan(
            area_id="AREA::REMOVED",
            kind="module",
            anchor="MOD::REMOVED",
            status="retired",
            members=(),
        )
        in artifact.areas
    )


async def test_a_persisted_area_keeps_its_id_through_an_anchor_alias() -> None:
    snapshot = await shop_snapshot()
    persisted = CurrentPlan(
        plan=None,
        areas=(
            WorkAreaInfo(
                area_id="AREA::ORDERS_SERVICE",
                kind="module",
                anchor="MOD::ORDERS_V1",
                planned_by="rules",
                status="active",
                created_at=CREATED,
                updated_at=UPDATED,
            ),
        ),
    )
    snapshot = snapshot.model_copy(update={"current": persisted, "aliases": {"MOD::ORDERS_V1": "MOD::ORDERS_SERVICE"}})

    artifact = partition(snapshot, plan_rules(plan_ontology()), plan_ontology())

    assert next(area for area in artifact.areas if area.anchor == "MOD::ORDERS_SERVICE") == AreaPlan(
        area_id="AREA::ORDERS_SERVICE",
        kind="module",
        anchor="MOD::ORDERS_SERVICE",
        status="active",
        members=(
            "BR::RELEASE",
            "FEAT::ORDER_HISTORY_EXPORT",
            "FEAT::PLACE_ORDER",
            "IFC::ORDERS_API",
            "REQ::BR::ORDER_LIMIT",
            "REQ::FR::ORDER_HISTORY_EXPORT",
            "REQ::FR::PLACE_ORDER",
            "REQ::LOGIC_EXPORT",
        ),
    )


async def test_a_persisted_anchor_resolving_to_another_anchor_retires_its_area() -> None:
    snapshot = await shop_snapshot()
    persisted = CurrentPlan(
        plan=None,
        areas=(
            WorkAreaInfo(
                area_id="AREA::ORDERS_SERVICE",
                kind="module",
                anchor="MOD::ORDERS_SERVICE",
                planned_by="rules",
                status="active",
                created_at=CREATED,
                updated_at=UPDATED,
            ),
            WorkAreaInfo(
                area_id="AREA::ORDERS_V1",
                kind="module",
                anchor="MOD::ORDERS_V1",
                planned_by="rules",
                status="active",
                created_at=CREATED,
                updated_at=UPDATED,
            ),
        ),
    )
    snapshot = snapshot.model_copy(update={"current": persisted, "aliases": {"MOD::ORDERS_V1": "MOD::ORDERS_SERVICE"}})

    artifact = partition(snapshot, plan_rules(plan_ontology()), plan_ontology())

    assert next(area for area in artifact.areas if area.area_id == "AREA::ORDERS_V1").status == "retired"
    assert next(area for area in artifact.areas if area.area_id == "AREA::ORDERS_SERVICE").status == "active"


async def test_new_area_ids_are_collision_suffixed_deterministically() -> None:
    snapshot = await shop_snapshot()
    nodes = list(snapshot.plannable)
    node = next(item for item in nodes if item.spec_id == "MOD::ORDERS_DATABASE")
    nodes.append(node.model_copy(update={"spec_id": "AREA::ORDERS_DATABASE", "uuid": "node-0099"}))
    snapshot = snapshot.model_copy(update={"plannable": tuple(nodes)})

    artifact = partition(snapshot, plan_rules(plan_ontology()), plan_ontology())

    assert {area.area_id for area in artifact.areas} >= {"AREA::ORDERS_DATABASE", "AREA::ORDERS_DATABASE_2"}
