"""Plan options, context and run models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest

from graph.build.models import BuildResult, ChangedEdge, ChangedNode, ChangeReport
from graph.core import load_ontology
from graph.core.db import Executor
from graph.core.errors import ConfigurationError
from graph.plan.models import (
    BuildChangeReport,
    ChangedSpecEdge,
    ChangedSpecNode,
    MeaningRevisions,
    PlanRunRecord,
)
from graph.plan.options import PlanContext, PlanOptions
from graph.plan.ports import PlanStore
from graph.plan.rules import load_plan_rules
from graph_plan_support import change_reports, ontology_labels, plan_ontology, plan_rules, shop_graph

SHOP_NODES: Final = {
    "Orders service": "MOD::ORDERS_SERVICE",
    "Payments service": "MOD::PAYMENTS_SERVICE",
    "Web shop": "MOD::WEB_SHOP",
    "Orders database": "MOD::ORDERS_DATABASE",
    "Production cluster": "MOD::PRODUCTION_CLUSTER",
    "Stripe": "MOD::STRIPE",
    "Orders API": "IFC::ORDERS_API",
    "Stripe API": "IFC::STRIPE_API",
    "Order": "ENT::ORDER",
    "Place order": "FEAT::PLACE_ORDER",
    "Order history export": "FEAT::ORDER_HISTORY_EXPORT",
    "Card payment": "FEAT::CARD_PAYMENT",
    "Checkout": "FEAT::CHECKOUT",
    "Checkout review": "FEAT::CHECKOUT_REVIEW",
    "Requirement A": "REQ::FR::A",
    "Requirement B": "REQ::FR::B",
    "Orphan requirement": "REQ::FR::ORPHAN",
    "Gift cards": "FEAT::GIFT_CARDS",
    "Authentication": "CAP::AUTHENTICATION",
    "All services run in the EU region": "CON::EU_REGION",
    "Release - the first release is MVP": "BR::RELEASE",
    "Which payment providers are supported?": "OQ::PAYMENT_PROVIDERS",
}

SHOP_EDGES: Final = {
    ("Orders API", "PROVIDED_BY", "Orders service"),
    ("Orders API", "CONSUMED_BY", "Web shop"),
    ("Stripe API", "PROVIDED_BY", "Stripe"),
    ("Stripe API", "CONSUMED_BY", "Payments service"),
    ("Order", "STORED_IN", "Orders database"),
    ("Stripe rejects repeated payment attempts", "PART_OF", "Stripe"),
    ("Web shop", "INTEGRATES_WITH", "Payments service"),
    ("Checkout", "DEPENDS_ON", "Card payment"),
    ("Requirement A", "DEPENDS_ON", "Requirement B"),
    ("Authentication", "REALIZED_BY", "Orders service"),
    ("Authentication", "REALIZED_BY", "Web shop"),
    ("All services run in the EU region", "RELATES_TO", "Orders service"),
    ("All services run in the EU region", "RELATES_TO", "Payments service"),
    ("All services run in the EU region", "RELATES_TO", "Web shop"),
    ("Which payment providers are supported?", "RELATES_TO", "Order"),
    ("Orders service", "DEPLOYED_TO", "Production cluster"),
}


class FakeGateway:
    pass


class FakeStore(PlanStore):
    pass


def make_context(**overrides: Any) -> PlanContext:
    ontology = load_ontology()
    values: dict[str, Any] = {
        "project_id": "project-1",
        "ontology": ontology,
        "rules": load_plan_rules(ontology=ontology),
        "driver": object(),
        "gateway": FakeGateway(),
        "store": FakeStore(),
        "build_id": "build-1",
    }
    values.update(overrides)
    return PlanContext(**values)


def test_plan_options_hold_the_documented_defaults() -> None:
    options = PlanOptions()

    assert options.review
    assert options.task_size == 5
    assert options.planner_members == 40
    assert options.max_attempts == 2
    assert options.review_model is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_size", 0),
        ("planner_members", 0),
        ("max_attempts", 0),
        ("max_attempts", 6),
    ],
)
def test_plan_options_bounds_are_configuration_errors(field: str, value: int) -> None:
    with pytest.raises(ConfigurationError, match="invalid_options") as raised:
        PlanOptions(**{field: value})

    assert raised.value.code == "invalid_options"


def test_plan_context_validates_the_project_and_defaults_the_executor() -> None:
    context = make_context()

    assert isinstance(context.query_executor, Executor)
    with pytest.raises(ConfigurationError, match="invalid_project_id"):
        make_context(project_id="../bad")


def test_with_run_id_shares_the_executor() -> None:
    before = make_context()
    after = before.with_run_id("run-1")

    assert after.run_id == "run-1"
    assert after.query_executor is before.query_executor


def test_build_change_report_round_trips_and_refuses_unknown_fields() -> None:
    report = BuildChangeReport(
        build_id="build-2",
        previous_build_id="build-1",
        nodes=(ChangedSpecNode(spec_id="REQ-1", classes=("added",)),),
    )
    restored = BuildChangeReport.model_validate_json(report.model_dump_json())

    assert restored == report
    with pytest.raises(ValueError, match="extra_forbidden"):
        BuildChangeReport.model_validate({"build_id": "build-2", "unknown": True})


def test_plan_run_record_round_trips_and_reports_finished() -> None:
    record = PlanRunRecord(
        project_id="project-1",
        run_id="run-1",
        mode="first",
        replan=False,
        build_id="build-1",
        options=PlanOptions(),
        rules_version="2.0",
        plan_ontology_version="1",
        fingerprint=None,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        progress={"snapshot": "done"},
        status="running",
    )
    restored = PlanRunRecord.model_validate_json(record.model_dump_json())

    assert restored == record
    assert not restored.finished
    for status in ("succeeded", "partially_succeeded", "failed"):
        assert record.model_copy(update={"status": status}).finished


def test_meaning_revisions_round_trip() -> None:
    revisions = MeaningRevisions(nodes={"REQ-2": "build-2", "REQ-1": "build-1"}, consumed=("build-1", "build-2"))
    restored = MeaningRevisions.model_validate_json(revisions.model_dump_json())

    assert restored == revisions
    assert restored.model_dump() == {
        "nodes": {"REQ-1": "build-1", "REQ-2": "build-2"},
        "consumed": ("build-1", "build-2"),
    }


def test_plan_test_support_loads_the_shipped_ontology_and_rules() -> None:
    ontology = plan_ontology()

    assert ontology.owner_of("Plan") == "plan"
    assert plan_rules(ontology).version


async def test_shop_fixture_is_reader_shaped_and_complete() -> None:
    reader = shop_graph()
    nodes = await reader.nodes("project-1")
    edges = await reader.edges("project-1")
    documents = await reader.documents("project-1")
    fragments = await reader.fragments("project-1")

    by_name = {node.name: node for node in nodes}
    by_id = {node.spec_id: node for node in nodes}
    assert set(by_name).issuperset(SHOP_NODES)
    assert {node.spec_id for node in nodes}.issuperset(SHOP_NODES.values())
    assert all(node.uuid and node.last_build_id == "build-1" for node in nodes)
    assert len({node.spec_id for node in nodes}) == len(nodes)

    by_names = {(by_id[edge.source].name, edge.relation, by_id[edge.target].name) for edge in edges}
    assert by_names >= SHOP_EDGES
    assert all(edge.edge_key and edge.basis for edge in edges)

    assert [document.document_id for document in documents] == ["shop"]
    assert documents[0].title == "Shop requirements"
    assert [fragment.fragment_id for fragment in fragments] == [
        "shop.intro",
        "shop.modules",
        "shop.interfaces",
        "shop.data",
        "shop.features",
        "shop.requirements",
        "shop.quality",
        "shop.constraints",
        "shop.questions",
    ]


async def test_shop_reader_filters_labels_attributes_neighbours_and_fragments() -> None:
    reader = shop_graph()
    rules = plan_rules()
    modules = await reader.nodes("project-1", labels=rules.labels_of("module"))
    external = await reader.nodes("project-1", labels=rules.labels_of("module"), where={"external": "yes"})
    neighbours = await reader.neighbours("project-1", "IFC::ORDERS_API")
    fragments = await reader.fragments("project-1", "shop")

    assert {node.name for node in modules} == {
        "Orders service",
        "Payments service",
        "Web shop",
        "Orders database",
        "Production cluster",
        "Stripe",
    }
    assert [node.name for node in external] == ["Stripe"]
    assert {(edge.relation, edge.target) for edge in neighbours} == {
        ("PROVIDED_BY", "MOD::ORDERS_SERVICE"),
        ("CONSUMED_BY", "MOD::WEB_SHOP"),
    }
    assert len(fragments) == 9


async def test_shop_graph_named_mutation_is_deterministic() -> None:
    plain = await shop_graph().edges("project-1")
    mutated = await shop_graph("orphan_in_web_shop").edges("project-1")

    assert ("REQ::FR::ORPHAN", "PART_OF", "MOD::WEB_SHOP") not in {
        (edge.source, edge.relation, edge.target) for edge in plain
    }
    assert ("REQ::FR::ORPHAN", "PART_OF", "MOD::WEB_SHOP") in {
        (edge.source, edge.relation, edge.target) for edge in mutated
    }


async def test_shop_fixture_covers_every_spec_type_of_the_rules() -> None:
    ontology = plan_ontology()
    rules = plan_rules(ontology)

    labels = {label for key in rules.types for label in ontology_labels(rules, key)}
    found = {node.label for node in await shop_graph().nodes("project-1")}

    assert labels <= found


def test_change_reports_convert_shop_v2_results_and_split_refs() -> None:
    result = BuildResult(
        project_id="project-1",
        build_id="build-2",
        mode="first",
        status="succeeded",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        finished_at=datetime(2026, 1, 2, tzinfo=UTC),
        change_report=ChangeReport(
            nodes=(ChangedNode(spec_id="REQ-1", label="FunctionalRequirement", classes=("changed_meaning",)),),
            edges=(
                ChangedEdge(
                    ref="REQ-1|PART_OF|MOD-1",
                    change="added",
                    basis="stated",
                ),
            ),
        ),
    )

    reports = change_reports((result,))

    assert reports == (
        BuildChangeReport(
            build_id="build-2",
            previous_build_id=None,
            nodes=(ChangedSpecNode(spec_id="REQ-1", classes=("changed_meaning",)),),
            edges=(
                ChangedSpecEdge(
                    source="REQ-1",
                    relation="PART_OF",
                    target="MOD-1",
                    change="added",
                    basis="stated",
                ),
            ),
        ),
    )
