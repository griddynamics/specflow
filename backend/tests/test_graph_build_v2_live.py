"""Live builds of the shop-v2 corpus on the shipped ontology (any graph store)."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
import pytest_asyncio

from graph.build import BuildResult
from graph.build.conform import DEDUP_PROMPT
from graph.build.consolidate import HUBS_PROMPT
from graph.build.hubjudge import PAIR_FLOOR, ConsolidationParams
from graph.build.metrics import (
    CATALOG_METRICS,
    EDGE_COUNTER_METRICS,
    IDENTITY_METRICS,
    INFERENCE_METRICS,
    KNOWN_TERM_METRICS,
    NAMING_METRICS,
    TERM_USE_METRICS,
    WINDOW_METRICS,
    StepArtifacts,
    consolidated_metrics,
    identity_metrics,
    naming_metrics,
)
from graph.build.models import (
    AnsweredElsewhereRef,
    DiffArtifact,
    EdgesArtifact,
    HubMerge,
    InferenceReport,
    NodesArtifact,
    NodeUpdate,
    PairCandidate,
    PlanArtifact,
    ResolveArtifact,
    TargetGraph,
)
from graph.build.prompts import (
    ATTRIBUTES_PROMPT,
    EDGE_WINDOW_PROMPT,
    ENTITIES_LABEL,
    NODE_HUBS_PROMPT,
    NODE_STATEMENTS_PROMPT,
)
from graph_probes import requires_live_graph
from graph_probes.base import SPEC_NODE, GraphState, ProbeEdge
from graph_support import (
    GraphProject,
    ScriptedGateway,
    ScriptedRequest,
    assert_all_scripted,
    judge_payload,
    open_graph_project,
    shipped_ontology,
    untrusted_blocks,
)
from graph_v2_support import (
    ANSWERED_VERDICTS,
    CHARGE_RECORD,
    CHARGE_RESULT,
    CONFORM_VERDICTS,
    REFUND_CURRENCY,
    RETRY_QUESTION,
    SAVED_CARD,
    SAVED_CARD_RULE,
    XDOC_VERDICTS,
    ShopV2Builds,
    has_short_cycle,
    shop_v2_answers,
    shop_v2_without,
)

pytestmark = [requires_live_graph(), pytest.mark.asyncio(loop_scope="module")]

ONTOLOGY: Final = shipped_ontology()
CONFORM_PROMPT: Final = "judge_owner_conflict"
ORPHAN_PROMPT: Final = "judge_attach_orphan"

CSV_FR: Final = "Customer can export order history as CSV"
COUPON_RULE: Final = "Order: at most one coupon"
STACKING: Final = "Customer can stack coupons"
QUESTION: Final = "Which coupon provider is used?"
EU_RULE: Final = "All services must run in the EU region, as the client contract requires"
EXPORT_FRAGMENT: Final = "orders.md#orders-service-requirements/order-history-export#7241d067"
CHECKOUT_DOCUMENT: Final = "checkout.md"

# 7 modules, 6 features; the entity "Invoice" has no supporting edge and is dropped.
NODES_BY_LABEL: Final = {
    "BusinessRule": 1,
    "Constraint": 2,
    "DataEntity": 1,
    "DataRequirement": 1,
    "Feature": 6,
    "FunctionalRequirement": 2,
    "IntegrationRequirement": 1,
    "Interface": 1,
    "Module": 7,
    "OpenQuestion": 1,
}
# Every domain edge as (source name, relation, target name, basis); RELATES_TO is symmetric, so its two
# edges are listed apart, by their ends.
DIRECTED_EDGES: Final = {
    ("Checkout API", "PROVIDED_BY", "Checkout service", "stated"),
    ("Checkout API", "CONSUMED_BY", "Web app", "stated"),
    ("Web app calls Checkout API to place an order", "PART_OF", "Checkout API", "stated"),
    ("Web app", "INTEGRATES_WITH", "Checkout service", "implied"),  # derived consumer -> provider
    ("Stripe API calls must stay at most 100 per minute", "PART_OF", "Stripe", "stated"),
    ("Purchase order holds id, lines, currency", "PART_OF", "Purchase order", "stated"),
    ("Purchase order", "STORED_IN", "Orders database", "stated"),
    ("Orders service", "DEPLOYED_TO", "Production environment", "stated"),
    ("Checkout service", "DEPLOYED_TO", "Production environment", "stated"),  # extracted INTEGRATES_WITH
    (EU_RULE, "PART_OF", "Production environment", "stated"),
    ("Coupons", "PART_OF", "Checkout service", "stated"),
    (COUPON_RULE, "PART_OF", "Coupons", "stated"),
    (STACKING, "PART_OF", "Coupons", "stated"),
    ("Order history export", "PART_OF", "Orders service", "stated"),
    (CSV_FR, "PART_OF", "Order history export", "stated"),
    ("Refunds", "PART_OF", "Orders service", "stated"),
    ("Refunds", "DEPENDS_ON", "Order cancellation", "stated"),
    ("Order cancellation", "PART_OF", "Orders service", "stated"),
    ("Partial refunds", "PART_OF", "Orders service", "inferred"),  # INF-CONT-02
}
RELATED: Final = {
    (frozenset({QUESTION, "Coupons"}), "stated"),
    (frozenset({CSV_FR, "Reporting service"}), "stated"),  # the conform judge's `relates`
}
# (reason, relation, source spec_id, target spec_id)
QUARANTINE: Final = {
    ("cycle_broken", "DEPENDS_ON", "FEAT::ORDER_CANCELLATION", "FEAT::REFUNDS"),
    ("illegal_pair", "PART_OF", "FEAT::PARTIAL_REFUNDS", "FEAT::REFUNDS"),
    ("judge_rejected", "PART_OF", "REQ::FR::CUSTOMER_EXPORT_ORDER_HISTORY_CSV", "MOD::WEB_APP"),
    ("owner_conflict", "PART_OF", "FEAT::ORDER_HISTORY_EXPORT", "MOD::REPORTING_SERVICE"),
}
QUARANTINE_BY_PAIR: Final = {
    "DEPENDS_ON|Feature|Feature": 1,
    "PART_OF|Feature|Feature": 1,
    "PART_OF|Feature|Module": 1,
    "PART_OF|FunctionalRequirement|Module": 1,
}
EXCLUSIVE_CONFLICTS: Final = 4
# The model calls of the first build (embeddings aside): one window per document in both node passes
# one edge call per document, no dedup candidate, one conform judge group, one orphan feature judged
# two hub blocks (the data entities are a label part of their own; the one interface needs no block).
FIRST_CALLS: Final = {
    NODE_HUBS_PROMPT: 3,
    NODE_STATEMENTS_PROMPT: 3,
    EDGE_WINDOW_PROMPT: 3,
    ATTRIBUTES_PROMPT: 2,
    HUBS_PROMPT: 2,
    CONFORM_PROMPT: 1,
    ORPHAN_PROMPT: 1,
}


@dataclass
class Built:
    """The module's two builds of one project (the project stays open while the tests run)."""

    project: GraphProject
    builds: ShopV2Builds
    first: BuildResult
    first_gateway: ScriptedGateway
    first_graph: GraphState
    update: BuildResult
    update_gateway: ScriptedGateway
    update_graph: GraphState

    @property
    def project_id(self) -> str:
        return self.builds.project_id

    async def artifact(self, result: BuildResult, step: str) -> dict[str, Any]:
        payload = await self.builds.stores.builds.get_artifact(self.project_id, result.build_id, step)
        assert payload is not None, step
        return dict(payload)


def scripted_factory(gateways: list[ScriptedGateway]) -> Any:
    """A gateway factory that keeps every gateway it makes (``assert_all_scripted``)."""

    def factory(config: Any = None, **options: Any) -> ScriptedGateway:
        gateway = ScriptedGateway(config, **options)
        gateways.append(gateway)
        return gateway

    return factory


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def built(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Built]:
    """A ``first`` build of shop-v2 snapshot 1, then an ``update`` without change, into one project."""

    gateways: list[ScriptedGateway] = []
    async with open_graph_project() as project:
        builds = ShopV2Builds(project, scripted_factory(gateways), Path(tmp_path_factory.mktemp("shop-v2")))
        first, first_gateway = await builds.build(1)
        first_graph = await project.snapshot()
        update, update_gateway = await builds.build(1)
        assert_all_scripted(*gateways)
        yield Built(
            project, builds, first, first_gateway, first_graph, update, update_gateway, await project.snapshot()
        )


@dataclass
class Shared:
    """Builds of another project: snapshot 1, snapshot 2 (checkout.md rewords CON-001, which
    overview.md states too) and snapshot 2 without checkout.md, with the CON-001 node read after each."""

    builds: ShopV2Builds
    first: BuildResult
    first_node: dict[str, Any]
    revised: BuildResult
    revised_gateway: ScriptedGateway
    revised_node: dict[str, Any]
    deleted: BuildResult
    deleted_gateway: ScriptedGateway
    deleted_node: dict[str, Any]

    async def artifact(self, result: BuildResult, step: str) -> dict[str, Any]:
        payload = await self.builds.stores.builds.get_artifact(self.builds.project_id, result.build_id, step)
        assert payload is not None, step
        return dict(payload)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def shared(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Shared]:
    gateways: list[ScriptedGateway] = []
    async with open_graph_project() as project:
        builds = ShopV2Builds(project, scripted_factory(gateways), Path(tmp_path_factory.mktemp("shop-v2-shared")))
        first, _ = await builds.build(1)
        first_node = await shared_node(project, builds.project_id)
        revised, revised_gateway = await builds.build(2)
        revised_node = await shared_node(project, builds.project_id)
        without = shop_v2_without(2, [CHECKOUT_DOCUMENT], tmp_path_factory.mktemp("shop-v2-corpus"))
        deleted, deleted_gateway = await builds.build(2, without)
        deleted_node = await shared_node(project, builds.project_id)
        assert_all_scripted(*gateways)
        yield Shared(
            builds, first, first_node, revised, revised_gateway, revised_node, deleted, deleted_gateway, deleted_node
        )


async def shared_node(project: GraphProject, project_id: str) -> dict[str, Any]:
    """The shared constraint's ``spec_id``, statements and evidence fragments (by document) as persisted."""

    nodes = await project.nodes(SPEC_NODE, project_id=project_id, name=EU_RULE)
    assert len(nodes) == 1, nodes
    (node,) = nodes
    fragments = [
        e.target.get("fragment_id")
        for e in await project.edges("REFERENCED_BY", project_id=project_id)
        if e.source.id == node.id and "Fragment" in e.target.labels and e.target.get("fragment_id") is not None
    ]
    return {
        "spec_id": node.get("spec_id"),
        "statements": json.loads(node["statements"]),
        "quote": node.get("quote"),
        "fragments": sorted(fragments),
    }


async def spec_edges(project: GraphProject, project_id: str) -> list[ProbeEdge]:
    """The edges between two spec nodes of the project (any type)."""

    return [
        e
        for e in await project.edges(project_id=project_id)
        if SPEC_NODE in e.source.labels
        and SPEC_NODE in e.target.labels
        and e.source.get("group_id") == project_id
        and e.target.get("group_id") == project_id
    ]


async def domain_edges(built: Built) -> list[dict[str, Any]]:
    return [
        {
            "source": e.source.get("name"),
            "relation": e.type,
            "target": e.target.get("name"),
            "basis": e.get("basis"),
            "rule": e.get("inference_rule"),
            "fact": e.get("fact"),
        }
        for e in await spec_edges(built.project, built.project_id)
        if e.type in ONTOLOGY.relations
    ]


async def node_props(built: Built, name: str) -> dict[str, Any]:
    nodes = await built.project.nodes(SPEC_NODE, project_id=built.project_id, name=name)
    assert len(nodes) == 1, (name, nodes)
    return dict(nodes[0].props)


def prompts(gateway: ScriptedGateway) -> Counter[str]:
    return Counter(call.prompt for call in gateway.calls if call.call_class != "embedding")


async def test_first_build_succeeds_verifies_and_makes_the_scripted_calls(built: Built) -> None:
    result = built.first
    assert (result.mode, result.status, result.failed_step, result.error_code) == ("first", "succeeded", None, None)
    assert result.settled and result.skipped_fragments == {}
    verification = result.verification
    assert verification is not None
    assert verification.ok, verification
    # nothing blocking was found, so no v2_rule either (Conform guarantees the v2.0 rules)
    assert verification.found == {} and verification.problems == {} and verification.repairs == 0
    assert verification.metrics.spine_unreachable == verification.metrics.spine_unreachable_before == 0
    assert prompts(built.first_gateway) == FIRST_CALLS
    assert DEDUP_PROMPT not in prompts(built.first_gateway)


async def test_d6_and_d7_hold_in_the_persisted_graph(built: Built) -> None:
    # D6: one owner per requirement and feature (every PART_OF from them is on an owner pair, inferred
    # edges included), one provider per interface, one parent module per module.
    owners = {label for label in ONTOLOGY.labels if ONTOLOGY.owner_pairs(label)}
    assert {pair.relation for label in owners for pair in ONTOLOGY.owner_pairs(label)} == {"PART_OF"}
    nodes = await built.project.nodes(SPEC_NODE)
    # edges from a spec node of the project to any spec node
    edges = [
        e
        for e in await built.project.edges()
        if SPEC_NODE in e.source.labels
        and SPEC_NODE in e.target.labels
        and e.source.get("group_id") == built.project_id
    ]
    targets: dict[str, set[str]] = {}
    for n in nodes:
        owned = bool(owners.intersection(n.labels))
        for e in edges:
            if e.source.id == n.id and (
                (owned and e.type == "PART_OF")
                or ("Interface" in n.labels and e.type == "PROVIDED_BY")
                or ("Module" in n.labels and "Module" in e.target.labels and e.type == "PART_OF")
            ):
                targets.setdefault(n.id, set()).add(e.target.id)
    rows = [{"name": n.get("name"), "targets": len(targets[n.id])} for n in nodes if len(targets.get(n.id, ())) > 1]
    assert rows == []
    owned = {e.source.id for e in edges if e.type == "PART_OF"}
    assert len(owned) == 12  # every requirement (7) and every feature but the out-of-scope Gift cards
    # D7: DEPENDS_ON (the shipped ontology's acyclic relation) has no directed cycle.
    assert ONTOLOGY.acyclic_relations() == {"DEPENDS_ON"}
    depends_on = [e for e in edges if e.type == "DEPENDS_ON"]
    assert not has_short_cycle(depends_on, {n.id for n in nodes}, 10)


async def test_nodes_per_label_and_the_unsupported_entity_is_dropped(built: Built) -> None:
    labels = Counter(label for n in await built.project.nodes(SPEC_NODE) for label in n.labels if label != SPEC_NODE)
    assert dict(labels) == NODES_BY_LABEL
    assert built.first.metrics.nodes_by_label == NODES_BY_LABEL
    assert await built.project.nodes(SPEC_NODE, name="Invoice") == []
    counters = TargetGraph.model_validate(await built.artifact(built.first, "conform")).counters
    assert counters.unsupported_nodes == 1
    assert built.first.verification is not None
    assert built.first.verification.metrics.unsupported_entities_dropped == 1


async def test_domain_edges_and_their_basis(built: Built) -> None:
    rows = await domain_edges(built)
    directed = {
        (row["source"], row["relation"], row["target"], row["basis"]) for row in rows if row["relation"] != "RELATES_TO"
    }
    related = {
        (frozenset({row["source"], row["target"]}), row["basis"]) for row in rows if row["relation"] == "RELATES_TO"
    }
    assert directed == DIRECTED_EDGES
    assert related == RELATED
    assert len(rows) == len(DIRECTED_EDGES) + len(RELATED)
    # one DEPENDS_ON direction survives the cycle break
    assert [r for r in rows if r["relation"] == "DEPENDS_ON"] == [
        r for r in rows if (r["source"], r["relation"]) == ("Refunds", "DEPENDS_ON")
    ]
    inferred = [row for row in rows if row["basis"] == "inferred"]
    assert [(row["source"], row["rule"]) for row in inferred] == [("Partial refunds", "INF-CONT-02")]
    judged = next(row for row in rows if row["relation"] == "RELATES_TO" and CSV_FR in (row["source"], row["target"]))
    # the stated fact, snapped to source lines by its tokens, is kept (the judge's quote only adds evidence)
    assert "The file is generated by the Reporting" in " ".join(judged["fact"].split())
    assert built.first.metrics.edges_by_relation == dict(Counter(row["relation"] for row in rows))


async def test_quarantine_by_reason(built: Built) -> None:
    result = built.first
    found = {(q.reason, q.relation, q.source_spec_id, q.target_spec_id) for q in result.quarantine}
    assert found == QUARANTINE
    assert len(result.quarantine) == len(QUARANTINE)
    assert result.metrics.quarantine_by_reason == Counter(reason for reason, *_ in QUARANTINE)
    assert result.metrics.quarantine_by_pair == QUARANTINE_BY_PAIR
    # dropped edges per relation and reason: the quarantine reasons, plus the CSV requirement's PART_OF its
    # feature's own module (redundant, never quarantined)
    assert result.metrics.dropped_edges == {
        "DEPENDS_ON:cycle_broken": 1,
        "PART_OF:illegal_pair": 1,
        "PART_OF:judge_rejected": 1,
        "PART_OF:owner_conflict": 1,
        "PART_OF:redundant": 1,
    }
    # quarantine lives in the BuildStore only: no quarantined edge is in the graph
    rows = await domain_edges(built)
    assert not {(r["source"], r["relation"], r["target"]) for r in rows} & {
        ("Order cancellation", "DEPENDS_ON", "Refunds"),
        ("Partial refunds", "PART_OF", "Refunds"),
        (CSV_FR, "PART_OF", "Web app"),
        ("Order history export", "PART_OF", "Reporting service"),
    }


async def test_conform_judge_sees_one_group_and_its_verdict_is_reused_by_a_no_change_update(built: Built) -> None:
    calls = built.first_gateway.calls_for(CONFORM_PROMPT)
    assert [call.key for call in calls] == [CSV_FR]
    assert all(call.call_class == "judge" for call in calls)
    payload = judge_payload(_request(calls[0]))
    assert payload["relation"] == "RELATES_TO"
    assert payload["subject"]["name"] == CSV_FR
    assert payload["owner"]["name"] == "Order history export"
    assert sorted(loser["name"] for loser in payload["losers"]) == sorted(CONFORM_VERDICTS[CSV_FR])
    assert EXPORT_FRAGMENT in {label for label, _ in untrusted_blocks(_request(calls[0]).user)}
    first = TargetGraph.model_validate(await built.artifact(built.first, "conform")).counters
    assert (first.conform_judge_calls, first.conform_judge_reused, first.owner_conflicts_judged) == (1, 0, 1)
    assert (first.conform_judge_failed, first.conform_judge_invalid_quotes, first.cycles_broken) == (0, 0, 1)

    update = built.update
    assert (update.mode, update.status, update.failed_step) == ("update", "succeeded", None)
    assert update.verification is not None and update.verification.ok
    # no model call at all, the judge included; the stored verdict decides the same group again
    assert built.update_gateway.calls == []
    again = TargetGraph.model_validate(await built.artifact(update, "conform")).counters
    # owner_conflicts_judged counts fresh verdicts only; the stored one is counted as reused
    assert (again.conform_judge_calls, again.conform_judge_reused, again.owner_conflicts_judged) == (0, 1, 0)
    assert (built.first.metrics.conform_judge_calls, update.metrics.conform_judge_calls) == (1, 0)
    assert update.spec_changes.model_dump() == dict.fromkeys(update.spec_changes.model_dump(), ())
    assert {(q.reason, q.relation, q.source_spec_id, q.target_spec_id) for q in update.quarantine} == QUARANTINE
    assert built.update_graph == built.first_graph


async def test_reader_inherits_release_and_filters_in_scope(built: Built) -> None:
    reader = built.project.reader(ontology=ONTOLOGY)
    by_name = {node.name: node for node in await reader.nodes(built.project_id)}
    assert by_name["Coupons"].attributes["release"] == "Phase 2"
    # requirements without a release read their feature's (computed when read, never stored)
    assert by_name[COUPON_RULE].attributes["release"] == "Phase 2"
    assert by_name[STACKING].attributes["release"] == "Phase 2"
    assert by_name[CSV_FR].attributes.get("release") is None
    assert (await node_props(built, COUPON_RULE)).get("release") is None
    rule = await reader.node(built.project_id, by_name[COUPON_RULE].spec_id)
    assert rule is not None and rule.attributes["release"] == "Phase 2"
    # in_scope: "no" only where the text says so; the default "yes" is stored
    out = await reader.nodes(built.project_id, labels=["Feature", "FunctionalRequirement"], where={"in_scope": "no"})
    assert sorted(node.name for node in out) == sorted(["Gift cards", STACKING])
    kept = await reader.nodes(built.project_id, labels=["FunctionalRequirement"], where={"in_scope": "yes"})
    assert [node.name for node in kept] == [CSV_FR]
    assert (await node_props(built, CSV_FR))["in_scope"] == "yes"


async def test_inf_cont_02_attaches_the_orphan_feature_only(built: Built) -> None:
    report = InferenceReport.model_validate(await built.artifact(built.first, "infer"))
    rules = {rule.rule: rule for rule in report.rules}
    assert [rule.rule for rule in report.rules] == [
        "INF-CONT-01",
        "INF-CONT-02",
        "INF-INT-01",
        "INF-CAP-01",
        "INF-XDOC-01",
    ]
    cont = rules["INF-CONT-02"]
    # Partial refunds lost its only container (illegal feature PART_OF feature); Gift cards is out of scope
    assert (cont.subjects, cont.judged, cont.accepted, cont.edges_added) == (1, 1, 1, 1)
    assert all(rule.subjects == 0 for name, rule in rules.items() if name != "INF-CONT-02")
    assert report.inferred_added == ("FEAT::PARTIAL_REFUNDS|PART_OF|MOD::ORDERS_SERVICE",)
    assert built.first.spec_changes.inferred_added == report.inferred_added
    assert [call.key for call in built.first_gateway.calls_for(ORPHAN_PROMPT)] == ["Partial refunds"]
    gift = {n.id for n in await built.project.nodes("Feature", name="Gift cards")}
    relations = [e.type for e in await built.project.edges() if e.source.id in gift]
    assert [relation for relation in relations if relation in ONTOLOGY.relations] == []
    # the update judged nothing again and kept the inferred edge
    again = InferenceReport.model_validate(await built.artifact(built.update, "infer"))
    assert again.inferred_added == () and again.inferred_removed == ()


async def test_consolidated_v2_metrics_are_present(built: Built) -> None:
    result = built.first
    assert result.verification is not None
    metrics = result.verification.metrics
    expected = {
        "owner_conflicts_judged": 1,
        "cycles_broken": 1,
        "interfaces_without_provider": 0,
        "features_named_like_statements": 0,
        "unsupported_entities_dropped": 1,
        "entity_relates_fan_out": {},
        "relates_fan_out": {1: 1},
        "deployed_to_fan_out": {1: 2},
        "ownerless_requirements": 0,
        "in_scope_disagreements": 0,
        "quarantine_by_reason": result.metrics.quarantine_by_reason,
        "quarantine_by_pair": QUARANTINE_BY_PAIR,
    }
    assert {name: getattr(metrics, name) for name in expected} == expected
    # Conform's pre-normalisation count: the CSV requirement's 3 extra owners and the feature's 1
    assert metrics.exclusive_conflicts == result.metrics.exclusive_conflicts == EXCLUSIVE_CONFLICTS
    assert metrics.redundant_after_inference == 0

    target = TargetGraph.model_validate(await built.artifact(result, "conform"))
    plan = PlanArtifact.model_validate(await built.artifact(result, "plan"))
    nodes = NodesArtifact.model_validate(await built.artifact(result, "extract_nodes"))
    resolved = ResolveArtifact.model_validate(await built.artifact(result, "resolve"))
    assert (metrics.label_collapses, metrics.derive_fallback_matches) == (0, 0)
    # the hub-name metrics come from the stored extract-nodes and resolve artifacts, in Verify too
    naming = naming_metrics(nodes, resolved, ONTOLOGY, plan)
    assert {name: getattr(metrics, name) for name in NAMING_METRICS} == naming
    assert naming["hub_surfaces_per_hub"] is not None
    # the identity metrics come from the stored resolve and conform artifacts, in Verify too
    identity = identity_metrics(resolved, target, result)
    assert {name: getattr(metrics, name) for name in IDENTITY_METRICS} == identity
    assert all(value is not None for value in identity.values()), identity
    assert sum(identity["hub_merges_by_source"].values()) == resolved.counters.consolidation.merges
    for prefer in (True, False):
        consolidated = consolidated_metrics(
            target,
            result,
            ONTOLOGY,
            plan=plan,
            prefer_verification=prefer,
            steps=StepArtifacts(resolved=resolved, nodes=nodes),
        )
        values = consolidated.model_dump()
        assert {name: values[name] for name in expected} == expected, prefer
        # without a spine view, the per-label count over every edge comes only from Verify
        missing = [] if prefer else ["spine_unreachable_by_label"]

        # the edge metrics of windows, entry paths, answered questions, rule items and modified causes
        # until the build records them, and the orphan cosines without vectors; the inference
        # metrics of limits, backlog, reads, settlement and change report until the build records them
        optional = {
            *CATALOG_METRICS,
            *KNOWN_TERM_METRICS,
            *TERM_USE_METRICS,
            *EDGE_COUNTER_METRICS,
            *WINDOW_METRICS,
            *INFERENCE_METRICS,
            "windows_called_by_cause",
            "items_by_outcome",
            "modified_causes",
            "orphans_best_elsewhere",
        }
        assert [name for name, value in values.items() if value is None and name not in optional] == missing, prefer
        assert consolidated.nodes_by_label == NODES_BY_LABEL
        assert consolidated.exclusive_conflicts == EXCLUSIVE_CONFLICTS, prefer
        owners = consolidated.owners_by_source or {}
        assert sum(owners.values()) > 0, prefer
        if not prefer:
            assert owners["none"] == consolidated.ownerless_requirements


def _request(call: Any) -> ScriptedRequest:
    """A recorded call as the ``ScriptedRequest`` the support helpers read."""

    return ScriptedRequest(call.call_class, call.model, call.prompt, call.messages, call.output_type)


# the constraint CON-001 is stated in overview.md and in checkout.md, in other words


def _by_document(node: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {statement["document_id"]: statement for statement in node["statements"]}


def _fragments_by_document(node: dict[str, Any]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for fragment in node["fragments"]:
        found.setdefault(fragment.split("#", 1)[0], []).append(fragment)
    return found


async def test_a_requirement_stated_in_two_documents_has_evidence_and_a_statement_in_each(shared: Shared) -> None:
    node = shared.first_node
    statements = _by_document(node)
    assert set(statements) == {"overview.md", CHECKOUT_DOCUMENT}
    assert (
        statements["overview.md"]["text"]
        == "CON-001: All services must run in the EU region, as the client contract requires."
    )
    assert statements[CHECKOUT_DOCUMENT]["text"] == (
        "CON-001: The Checkout service runs only in the EU region, because the client contract requires it."
    )
    assert sorted(fragment.split("#", 1)[0] for fragment in node["fragments"]) == [CHECKOUT_DOCUMENT, "overview.md"]
    counters = TargetGraph.model_validate(await shared.artifact(shared.first, "conform")).counters
    grounding = (
        counters.unsnapped_quote_nodes,
        counters.unsnapped_source_quotes,
        counters.provisional_keys_left,
        counters.attributed_fragments_dropped,
        counters.nodes_missing_source_documents,
    )
    assert grounding == ({}, 0, 0, 0, 0)
    assert shared.first.verification is not None
    metrics = shared.first.verification.metrics
    assert (metrics.unsnapped_source_quotes, metrics.nodes_missing_source_documents) == (0, 0)
    assert (metrics.in_scope_disagreements, metrics.in_scope_dirty_wins) == (0, 0)


async def test_rewording_it_in_one_document_updates_the_node_and_keeps_the_other_documents_evidence(
    shared: Shared,
) -> None:
    result = shared.revised
    assert (result.mode, result.status, result.failed_step) == ("update", "succeeded", None)
    assert result.verification is not None and result.verification.ok
    # only the edited section and its unit are asked again, and the attributes of the nodes it grounds
    assert prompts(shared.revised_gateway) == {
        NODE_HUBS_PROMPT: 1,
        NODE_STATEMENTS_PROMPT: 1,
        EDGE_WINDOW_PROMPT: 1,
        ATTRIBUTES_PROMPT: 1,
    }
    spec_id = shared.first_node["spec_id"]
    assert shared.revised_node["spec_id"] == spec_id
    assert spec_id in result.spec_changes.modified
    assert (result.spec_changes.added, result.spec_changes.removed) == ((), ())
    diff = DiffArtifact.model_validate(await shared.artifact(result, "diff"))
    update = next(op for op in diff.operations if isinstance(op, NodeUpdate) and op.spec_id == spec_id)
    assert "statements" in update.properties
    before, after = _by_document(shared.first_node), _by_document(shared.revised_node)
    assert after["overview.md"] == before["overview.md"]  # same text, revision and time
    assert after[CHECKOUT_DOCUMENT]["text"] == (
        "CON-001: The Checkout service must be hosted in the EU region only; the client contract names no other region."
    )
    assert after[CHECKOUT_DOCUMENT]["version"] == before[CHECKOUT_DOCUMENT]["version"] + 1
    # the overview.md evidence stays; the checkout.md evidence is the edited section's new fragment
    first, revised = _fragments_by_document(shared.first_node), _fragments_by_document(shared.revised_node)
    assert revised["overview.md"] == first["overview.md"]
    assert len(revised[CHECKOUT_DOCUMENT]) == 1 and revised[CHECKOUT_DOCUMENT] != first[CHECKOUT_DOCUMENT]


async def test_deleting_the_other_document_drops_only_its_evidence(shared: Shared) -> None:
    result = shared.deleted
    assert (result.mode, result.status, result.failed_step) == ("update", "succeeded", None)
    assert result.verification is not None and result.verification.ok
    # nothing is extracted again; the nodes whose evidence changed get their attributes asked again, and
    # the hub block is judged again: its hubs' stored decisions expired with their evidence
    assert prompts(shared.deleted_gateway) == {ATTRIBUTES_PROMPT: 1, HUBS_PROMPT: 1}
    spec_id = shared.first_node["spec_id"]
    assert spec_id in result.spec_changes.modified and spec_id not in result.spec_changes.removed
    # the nodes grounded only in checkout.md go
    assert set(result.spec_changes.removed) == {
        "FEAT::COUPONS",
        "FEAT::GIFT_CARDS",
        "QST::WHICH_COUPON_PROVIDER_USED",
        "REQ::BR::ORDER_MOST_ONE_COUPON",
        "REQ::FR::CUSTOMER_STACK_COUPONS",
    }
    node = shared.deleted_node
    assert node["spec_id"] == spec_id
    assert node["statements"] == [_by_document(shared.revised_node)["overview.md"]]
    assert [fragment.split("#", 1)[0] for fragment in node["fragments"]] == ["overview.md"]
    assert node["quote"] == "CON-001: All services must run in the EU region, as the client contract requires."


# the identity corpus (shop-v2/3): an alias pair merged by the pair pass, an unsure pair

IDENTITY_SNAPSHOT: Final = 3
# Blocks of 2: each block holds one name of each hub, so only the pair pass sees a hub's two names.
IDENTITY_PARAMS: Final = ConsolidationParams(block_size=2, block_overlap=0)
OMS: Final = "Module|surface|oms"
ORDERS_SERVICE: Final = "Module|surface|order service"
REPORT_GENERATOR: Final = "Module|surface|report generator"
REPORTING_SERVICE: Final = "Module|surface|reporting service"
MERGED_PAIR: Final = (OMS, ORDERS_SERVICE)
UNSURE_PAIR: Final = (REPORT_GENERATOR, REPORTING_SERVICE)


@dataclass
class Identity:
    """A ``first`` build of the identity corpus, then an ``update`` without change, into one project."""

    builds: ShopV2Builds
    first: BuildResult
    first_gateway: ScriptedGateway
    first_graph: GraphState
    update: BuildResult
    update_gateway: ScriptedGateway
    update_graph: GraphState

    async def resolved(self, result: BuildResult) -> ResolveArtifact:
        payload = await self.builds.stores.builds.get_artifact(self.builds.project_id, result.build_id, "resolve")
        assert payload is not None
        return ResolveArtifact.model_validate(payload)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def identity(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Identity]:
    gateways: list[ScriptedGateway] = []
    async with open_graph_project() as project:
        root = Path(tmp_path_factory.mktemp("shop-v2-identity"))
        builds = ShopV2Builds(project, scripted_factory(gateways), root, hub_params=IDENTITY_PARAMS)
        first, first_gateway = await builds.build(IDENTITY_SNAPSHOT)
        first_graph = await project.snapshot()
        update, update_gateway = await builds.build(IDENTITY_SNAPSHOT)
        assert_all_scripted(*gateways)
        yield Identity(builds, first, first_gateway, first_graph, update, update_gateway, await project.snapshot())


def _candidates(resolved: ResolveArtifact) -> dict[tuple[str, str], PairCandidate]:
    return {(candidate.key_a, candidate.key_b): candidate for candidate in resolved.pair_candidates}


async def test_an_alias_pair_across_far_vectors_is_merged_by_the_pair_pass(identity: Identity) -> None:
    result = identity.first
    assert (result.mode, result.status, result.failed_step, result.error_code) == ("first", "succeeded", None, None)
    resolved = await identity.resolved(result)
    # the catalog links each overview.md hub to its other name (an alias of one entry)
    nodes = NodesArtifact.model_validate(
        await identity.builds.stores.builds.get_artifact(identity.builds.project_id, result.build_id, "extract_nodes")
    )
    aliases = {frozenset({hub.name, *hub.aliases}) for hub in nodes.hub_catalog}
    assert aliases == {frozenset({"OMS", "Orders service"}), frozenset({"Report generator", "Reporting service"})}
    # two blocks of 2 (nothing to decide in either), then one pair batch with both alias pairs
    hub_calls = identity.first_gateway.calls_for(HUBS_PROMPT)
    assert sorted(call.key for call in hub_calls) == [
        ("OMS", "Orders service", "Report generator", "Reporting service"),
        ("OMS", "Report generator"),
        ("Orders service", "Reporting service"),
    ]
    candidates = _candidates(resolved)
    assert set(candidates) == {MERGED_PAIR, UNSURE_PAIR}
    merged = candidates[MERGED_PAIR]
    # found by the alias only: the candidate vectors are far apart
    assert (merged.source, merged.outcome) == ("alias", "same")
    assert merged.rich_cos is not None and merged.rich_cos < PAIR_FLOOR
    assert resolved.merges == (HubMerge(anchor_key=OMS, member_key=ORDERS_SERVICE, source="pair", confidence="high"),)
    counters = resolved.counters.consolidation
    assert (counters.blocks, counters.calls, counters.pair_batches, counters.pair_calls) == (2, 3, 1, 1)
    assert (counters.pair_candidates_alias, counters.pair_candidates_vector, counters.pair_over_cap) == (2, 0, 0)
    assert (counters.merges, counters.pair_merges, counters.alias_merges) == (1, 1, 1)
    (node,) = [node for node in resolved.canonical if node.identity_key in MERGED_PAIR]
    assert {node.identity_key, *node.identity_aliases} == set(MERGED_PAIR)
    assert result.metrics.nodes_by_label == {"Module": 3}
    assert result.verification is not None
    assert result.verification.metrics.hub_merges_by_source == {"block": 0, "pair": 1, "stored": 0}


async def test_an_unsure_pair_is_not_merged_nor_stored_and_the_no_change_update_judges_it_again(
    identity: Identity,
) -> None:
    first = await identity.resolved(identity.first)
    assert _candidates(first)[UNSURE_PAIR].outcome == "unsure"
    assert first.counters.consolidation.unsure_groups == 1
    assert not {merge.member_key for merge in first.merges} & set(UNSURE_PAIR)
    modules = await identity.builds.project.nodes("Module", project_id=identity.builds.project_id)
    assert {"Report generator", "Reporting service"} <= {n.get("name") for n in modules}

    update = identity.update
    assert (update.mode, update.status, update.failed_step) == ("update", "succeeded", None)
    # the only model call: the unsure pair, judged again (nothing was stored for it)
    assert [(call.prompt, call.key) for call in identity.update_gateway.calls if call.call_class != "embedding"] == [
        (HUBS_PROMPT, ("Report generator", "Reporting service"))
    ]
    again = await identity.resolved(update)
    counters = again.counters.consolidation
    # the blocks are skipped; the merged pair's stored decision is the only decision read
    assert (counters.blocks, counters.blocks_skipped, counters.calls, counters.pair_calls) == (2, 2, 1, 1)
    assert (counters.decisions_read, counters.unsure_groups, counters.merges) == (1, 1, 1)
    assert {key: candidate.outcome for key, candidate in _candidates(again).items()} == {
        MERGED_PAIR: "same",
        UNSURE_PAIR: "unsure",
    }
    assert again.merges == (HubMerge(anchor_key=OMS, member_key=ORDERS_SERVICE, source="stored", confidence="high"),)
    assert update.spec_changes.model_dump() == dict.fromkeys(update.spec_changes.model_dump(), ())
    assert identity.update_graph == identity.first_graph


# the cross-document corpus (shop-v2/4)

CROSSDOC_SNAPSHOT: Final = 4
ANSWERED_PROMPT: Final = "judge_answered_elsewhere"
XDOC_PROMPT: Final = "judge_cross_document"
PAYMENT_SERVICE: Final = "Payment service"
CARD_BRANDS: Final = "Card: Visa or Mastercard only"
CHECKOUT_BRANDS: Final = "Checkout service accepts only listed card brands"
# One node window and one edge window per unit (4 units: payment-service.md is two), one attributes batch
# a hub block per label part (modules, interfaces), one answered-elsewhere call per open question (each
# has one candidate fragment) and one INF-XDOC-01 subject; no owner conflict, no dedup candidate.
CROSSDOC_CALLS: Final = {
    NODE_HUBS_PROMPT: 4,
    NODE_STATEMENTS_PROMPT: 4,
    EDGE_WINDOW_PROMPT: 4,
    ATTRIBUTES_PROMPT: 1,
    HUBS_PROMPT: 2,
    ANSWERED_PROMPT: 2,
    XDOC_PROMPT: 1,
}


@dataclass
class CrossDoc:
    """A ``first`` build of the cross-document corpus into a project of its own."""

    project: GraphProject
    builds: ShopV2Builds
    result: BuildResult
    gateway: ScriptedGateway

    @property
    def project_id(self) -> str:
        return self.builds.project_id

    async def artifact(self, step: str) -> dict[str, Any]:
        payload = await self.builds.stores.builds.get_artifact(self.project_id, self.result.build_id, step)
        assert payload is not None, step
        return dict(payload)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def crossdoc(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[CrossDoc]:
    gateways: list[ScriptedGateway] = []
    async with open_graph_project() as project:
        builds = ShopV2Builds(project, scripted_factory(gateways), Path(tmp_path_factory.mktemp("shop-v2-crossdoc")))
        result, gateway = await builds.build(CROSSDOC_SNAPSHOT)
        assert_all_scripted(*gateways)
        yield CrossDoc(project, builds, result, gateway)


def crossdoc_names(resolved: ResolveArtifact) -> dict[str, str]:
    """Node name by cid of the build's resolve artifact."""

    return {node.cid: node.name for node in resolved.canonical}


def unit_of(edges: EdgesArtifact, prefix: str) -> str:
    """The one unit of the edges artifact whose id starts with ``prefix``."""

    assert edges.catalog_parts is not None
    (found,) = [unit_id for unit_id in edges.catalog_parts if unit_id.startswith(prefix)]
    return found


async def crossdoc_edges(crossdoc: CrossDoc) -> set[tuple[str, str, str, str, str | None]]:
    return {
        (e.source.get("name"), e.type, e.target.get("name"), e.get("basis"), e.get("inference_rule"))
        for e in await spec_edges(crossdoc.project, crossdoc.project_id)
        if e.type in ONTOLOGY.relations
    }


async def test_crossdoc_build_succeeds_verifies_and_makes_the_scripted_calls(crossdoc: CrossDoc) -> None:
    result = crossdoc.result
    assert (result.mode, result.status, result.failed_step, result.error_code) == ("first", "succeeded", None, None)
    assert result.verification is not None and result.verification.ok, result.verification
    assert result.verification.found == {} and result.verification.problems == {}
    assert prompts(crossdoc.gateway) == CROSSDOC_CALLS
    assert result.quarantine == ()
    # one edge window per unit (the units are far below the window size), all called in a first build
    edges = EdgesArtifact.model_validate(await crossdoc.artifact("extract_edges"))
    assert [call.cause for call in edges.called_windows or ()] == ["first"] * 4
    assert (edges.counters.windows_called, edges.counters.windows_reused, edges.counters.warm_ups) == (4, 0, 0)


async def test_a_section_reference_across_documents_links_a_referenced_requirement(crossdoc: CrossDoc) -> None:
    edges = EdgesArtifact.model_validate(await crossdoc.artifact("extract_edges"))
    names = crossdoc_names(ResolveArtifact.model_validate(await crossdoc.artifact("resolve")))
    assert edges.catalog_parts is not None
    checkout = edges.catalog_parts[unit_of(edges, "unit:checkout.md")]
    # "§1.1 of the Payment service specification": the title scopes the number to payment-service.md, whose
    # "1.1 Card brands" fragment grounds the rule; the module the text names is a mentioned hub
    assert [names[cid] for cid in checkout.referenced] == [CARD_BRANDS]
    assert [names[cid] for cid in checkout.mentioned] == [PAYMENT_SERVICE]
    assert (checkout.heading, checkout.near) == ((), ())
    # the referenced rule is shown to the checkout unit's edge call by name
    (call,) = [call for call in crossdoc.gateway.calls_for(EDGE_WINDOW_PROMPT) if "chk.card" in call.key]
    entities = [body for label, body in untrusted_blocks(_request(call).user) if label == ENTITIES_LABEL]
    assert CARD_BRANDS in {json.loads(line)["name"] for body in entities for line in body.split("\n") if line}
    assert (CARD_BRANDS, "RELATES_TO", CHECKOUT_BRANDS, "stated", None) in await crossdoc_edges(crossdoc)
    # foreign-end raw edges by entry path: the reference kept; six PART_OF/PROVIDED_BY edges to the
    # Payment service by heading: four in the second payment-service.md unit (its heading path) and the
    # first unit's two (its title line "# Payment service" is a heading line, not a body line, since
    # by mention the two questions' RELATES_TO, the answered question's one dropped with it
    counters = TargetGraph.model_validate(await crossdoc.artifact("conform")).counters
    assert counters.foreign_edges == {
        "heading": {"kept": 6},
        "mentioned": {"dropped": 1, "kept": 1},
        "referenced": {"kept": 1},
    }
    assert crossdoc.result.verification is not None
    assert crossdoc.result.verification.metrics.cross_document_edges == {
        "PART_OF": {"stated": 5},
        "PROVIDED_BY": {"stated": 1},
        "RELATES_TO": {"inferred": 2, "stated": 2},
    }


async def test_an_open_question_answered_in_another_document_is_dropped_and_listed(crossdoc: CrossDoc) -> None:
    result = crossdoc.result
    plan = PlanArtifact.model_validate(await crossdoc.artifact("plan"))
    handles = shop_v2_answers(CROSSDOC_SNAPSHOT).fragment_ids(plan)
    # never persisted (dropped in its first build): no spec_id
    assert result.answered_elsewhere == (
        AnsweredElsewhereRef(
            spec_id=None, fragment_id=handles["ps.refunds"], quote=ANSWERED_VERDICTS[REFUND_CURRENCY][1]
        ),
    )
    counters = TargetGraph.model_validate(await crossdoc.artifact("conform")).counters
    assert (counters.oq_answer_judged, counters.oq_partly_kept, counters.oq_answered_dropped) == (2, 1, 1)
    # one small call per question, each shown only the fragment near it (another document's)
    calls = crossdoc.gateway.calls_for(ANSWERED_PROMPT)
    assert sorted(call.key for call in calls) == sorted([REFUND_CURRENCY, RETRY_QUESTION])
    assert {call.call_class for call in calls} == {"small"}
    shown = {
        call.key: [label for label, _ in untrusted_blocks(_request(call).user) if label != "payload"] for call in calls
    }
    assert shown == {REFUND_CURRENCY: [handles["ps.refunds"]], RETRY_QUESTION: [handles["ps.retries"]]}
    # the answered question is gone with its edge; the partly answered one stays with its own
    questions = await crossdoc.project.nodes("OpenQuestion", project_id=crossdoc.project_id)
    assert [n.get("name") for n in questions] == [RETRY_QUESTION]
    edges = await crossdoc_edges(crossdoc)
    assert (PAYMENT_SERVICE, "RELATES_TO", RETRY_QUESTION, "stated", None) in edges
    assert not [edge for edge in edges if REFUND_CURRENCY in edge]


async def test_interface_providers_by_heading_by_fallback_and_a_suppressed_derive(crossdoc: CrossDoc) -> None:
    edges = EdgesArtifact.model_validate(await crossdoc.artifact("extract_edges"))
    names = crossdoc_names(ResolveArtifact.model_validate(await crossdoc.artifact("resolve")))
    assert edges.catalog_parts is not None
    # the second payment-service.md unit names the Payment service only in its fragments' heading path
    card_api = edges.catalog_parts[unit_of(edges, "unit:payment-service.md#payment-service/2-card-api")]
    assert ([names[cid] for cid in card_api.heading], card_api.mentioned) == ([PAYMENT_SERVICE], ())
    # its first unit holds the title line "# Payment service", a heading line, and no body line names the
    # module: it is named only in headings there too (heading lines are no longer body text)
    first = edges.catalog_parts[unit_of(edges, "unit:payment-service.md#payment-service#")]
    assert ([names[cid] for cid in first.heading], first.mentioned) == ([PAYMENT_SERVICE], ())
    provided = {edge for edge in await crossdoc_edges(crossdoc) if edge[1] == "PROVIDED_BY"}
    # stated from the heading; the Refund API's from its `provider` attribute (the fallback derive); the
    # Card API's attribute (Stripe) is dropped: the interface already has a stated provider
    assert provided == {
        ("Card API", "PROVIDED_BY", PAYMENT_SERVICE, "stated", None),
        ("Refund API", "PROVIDED_BY", PAYMENT_SERVICE, "implied", None),
    }
    counters = TargetGraph.model_validate(await crossdoc.artifact("conform")).counters
    assert counters.derive_suppressed_by_edge == 1
    assert crossdoc.result.verification is not None
    assert crossdoc.result.verification.metrics.interfaces_without_provider == 0


async def test_inf_xdoc_01_accepts_one_link_and_downgrades_a_dependency_without_a_cue(crossdoc: CrossDoc) -> None:
    report = InferenceReport.model_validate(await crossdoc.artifact("infer"))
    rules = {rule.rule: rule for rule in report.rules}
    xdoc = rules["INF-XDOC-01"]
    # one hub (the Payment service) with members in checkout.md and payment-service.md and two near pairs
    assert (xdoc.subjects, xdoc.judged, xdoc.accepted, xdoc.edges_added) == (1, 1, 1, 2)
    assert xdoc.items_by_outcome == {"accepted": 1, "downgraded": 1}
    assert all(rule.subjects == 0 for name, rule in rules.items() if name != "INF-XDOC-01")
    (call,) = crossdoc.gateway.calls_for(XDOC_PROMPT)
    assert call.key == PAYMENT_SERVICE
    payload = judge_payload(_request(call))
    shown = {member["id"]: member["name"] for member in payload["members"]}
    assert sorted(sorted(shown[local] for local in pair) for pair in payload["pairs"]) == sorted(
        sorted((link.a, link.b)) for link in XDOC_VERDICTS[PAYMENT_SERVICE]
    )
    assert set(payload["relations"]) == {"RELATES_TO", "DEPENDS_ON"}
    # both written as RELATES_TO (the DEPENDS_ON had no dependency cue in its source-side quote)
    spec_ids = {
        n.get("spec_id"): n.get("name") for n in await crossdoc.project.nodes(SPEC_NODE, project_id=crossdoc.project_id)
    }
    items = {
        (
            item.outcome,
            item.relation,
            frozenset({spec_ids[item.source_spec_id or ""], spec_ids[item.target_spec_id or ""]}),
        )
        for item in report.judged_items
    }
    assert items == {
        ("accepted", "RELATES_TO", frozenset({CHARGE_RESULT, CHARGE_RECORD})),
        ("downgraded", "RELATES_TO", frozenset({SAVED_CARD, SAVED_CARD_RULE})),
    }
    # each item cites a quote from both documents
    for item in report.judged_items:
        assert {fragment.split("#", 1)[0] for fragment in item.fragment_ids} == {"checkout.md", "payment-service.md"}
    inferred = {edge for edge in await crossdoc_edges(crossdoc) if edge[3] == "inferred"}
    assert {(frozenset({edge[0], edge[2]}), edge[1], edge[4]) for edge in inferred} == {
        (frozenset({CHARGE_RESULT, CHARGE_RECORD}), "RELATES_TO", "INF-XDOC-01"),
        (frozenset({SAVED_CARD, SAVED_CARD_RULE}), "RELATES_TO", "INF-XDOC-01"),
    }
    assert not [edge for edge in await crossdoc_edges(crossdoc) if edge[1] == "DEPENDS_ON"]
