"""Live Neo4j e2e of ``update`` and ``rebuild`` builds of the shop corpus with the scripted gateway
(plan 3.T2, 3.T3).

FR-BLD-02..06, 10, 12..14, 22, 23 (update half), 25, FR-CORE-06; NFR-PERF-01 is reported, not
asserted. Every model answer comes from the hand-written ``tests/fixtures/graph/shop/answers/{1,2,3}.yaml``
(``ShopAnswers``). An answer file holds only the requests its build should make: reused fragments
and units make no call, and a request the file does not hold fails the test (``scripted_gateway``),
so the exact calls per prompt below are the reuse contract (FR-BLD-03, FR-BLD-22). The graph is
asserted by Cypher.

The chain: ``first`` of snapshot 1, ``update`` to 2 (checkout.md edited, a section removed and one
added), ``update`` to 3 (admin.md removed, "Orders service" renamed), an ``update`` without change,
then a ``rebuild`` of 3. D1-D4 (data-model.md) are checked after every build.

Gate: ``SPECFLOW_TEST_NEO4J=1`` and ``uv run --env-file ../.env.local``. Every test builds into its
own ``pytest-graph-<hex>`` project ids; ``neo4j_project`` deletes them afterwards.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from graph.build import (
    BuildContext,
    BuildOptions,
    BuildResult,
    BuildSettings,
    FileStores,
    open_file_stores,
    run_build,
    steps,
)
from graph.build import persist as writing
from graph.build import run as runner
from graph.build.conform import DEDUP_PROMPT
from graph.build.consolidate import HUBS_PROMPT
from graph.build.graphiti import EDGES_PROMPT, NODES_PROMPT
from graph.build.models import DiffArtifact, EdgesArtifact, NodesArtifact, PersistCursor, PlanArtifact
from graph.build.prompts import ATTRIBUTES_PROMPT, HUB_CATALOG_PROMPT, KNOWN_TERMS_LABEL
from graph.core import ConfigurationError, Ontology, load_ontology
from graph.core.identity import SpecIdRow
from graph.core.writer import NodeRef
from graph_support import (
    GAP_ONTOLOGY,
    Neo4jProject,
    ScriptedGateway,
    ShopAnswers,
    live_llm_config,
    shop_answer_data,
    shop_answers,
    shop_corpus,
    untrusted_blocks,
)
from logger.logger import INFO, get_logger, log_event
from specflow_live import requires_live

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, neo4j_project) live in graph_support.
pytest_plugins = ["graph_support"]

pytestmark = requires_live("NEO4J")

logger = get_logger("tests.graph_update_live")

OPTIONS = shop_answers(1).options
assert shop_answers(2).options == shop_answers(3).options == OPTIONS  # one frozen option set for the chain

# Model calls per prompt (embeddings aside) of each build of the chain: the reuse contract.
FIRST_CALLS = {
    NODES_PROMPT: 15,
    EDGES_PROMPT: 5,
    ATTRIBUTES_PROMPT: 3,
    HUB_CATALOG_PROMPT: 1,
    HUBS_PROMPT: 1,
    DEDUP_PROMPT: 1,
}
# 1 -> 2: only the two dirty checkout.md fragments (one window each) and the checkout unit; the hub
# block is sent because it holds a new hub; no dedup candidate is new.
UPDATE_2_CALLS = {NODES_PROMPT: 2, HUBS_PROMPT: 1, EDGES_PROMPT: 1, ATTRIBUTES_PROMPT: 1}
# 2 -> 3: chk.confirm, two overview runs and every orders.md fragment but the skipped one (the title
# rename changes every heading path, so the breadcrumb the prompt shows); the "Export details" unit
# keeps its content and catalog, so its edges are reused (FR-BLD-22).
UPDATE_3_CALLS = {NODES_PROMPT: 10, HUBS_PROMPT: 1, EDGES_PROMPT: 3, ATTRIBUTES_PROMPT: 2}
# rebuild: every planned window and unit again (notes.md has no node, so no edge call); the hub
# block is skipped, every pair in it decided by earlier builds (FR-BLD-25).
REBUILD_CALLS = {HUB_CATALOG_PROMPT: 1, NODES_PROMPT: 13, EDGES_PROMPT: 4, ATTRIBUTES_PROMPT: 2}


def unit(*handles: str) -> tuple[str, ...]:
    """The key of an edge request: the sorted handles of the unit's fragments."""

    return tuple(sorted(handles))


CHECKOUT_UNIT = unit(
    "chk.intro",
    "chk.basket",
    "chk.payment",
    "chk.confirm",
    "chk.delivery",
    "chk.quality",
    "chk.stripe",
    "chk.retries",
    "chk.notes",
    "chk.notes2",
)
OVERVIEW_UNIT = unit(
    "ov.intro",
    "ov.scope",
    "ov.checkout",
    "ov.orders",
    "ov.db",
    "ov.erp",
    "ov.stripe",
    "ov.auth",
    "ov.audit",
    "ov.arch",
    "ov.glossary",
)
ORDERS_UNIT = unit("ord.intro", "ord.export", "ord.obligations", "ord.po", "ord.erp", "ord.details1", "ord.details2")
DETAILS_UNIT = unit("ord.details3", "ord.details4", "ord.details5")

# Spec changes of the chain (spec_ids follow the hand-written names, tech-specs 5.4).
BUSINESS_RULE = "REQ::BR::ORDER_OVER_1_000_NEEDS"
APPROVAL_FRS = ("REQ::FR::SYSTEM_RECORD_MANAGER_APPROVAL_AUDIT", "REQ::FR::SYSTEM_TELL_CUSTOMER_ORDER_AWAITS")
UPDATE_2_ADDED = (
    "FEAT::DELIVERY_OPTIONS",
    "REQ::FR::FR_CHK_007_SYSTEM_LET_CUSTOMER_CHOOSE",
    "REQ::FR::SYSTEM_OFFER_RETRY_ANOTHER_CARD",
    "REQ::FR::SYSTEM_SHOW_ESTIMATED_DELIVERY_DATE",
)
# The reworded requirement of each update: modified in place (same id, same spec_id).
REWORDED_2 = "REQ::FR::FR_CHK_004_SYSTEM_SHOW_ERROR_KEEP"
REWORDED_3 = "REQ::FR::FR_CHK_005_SYSTEM_SEND_CONFIRMED_ORDER"
ADMIN_ONLY = (
    "REQ::FR::STAFF_CANNOT_OPEN_ADMIN_SCREEN",
    "REQ::FR::STAFF_FINANCE_ROLE_REQUEST_ORDER",
    "REQ::FR::STAFF_REACH_DOWNLOAD_SCREEN_FINANCE",
    "REQ::FR::SYSTEM_DEFAULT_DATE_RANGE_LAST",
    "REQ::FR::SYSTEM_LEAVE_CANCELLED_ORDERS_OUT",
    "REQ::FR::SYSTEM_RECORD_EVERY_ORDER_FILE",
    "REQ::FR::SYSTEM_REVOKE_DOWNLOAD_ACCESS_WHEN",
)
# The admin.md CSV statement was deduplicated with the orders.md one in build 1 (its key won); with
# admin.md gone the node continues under its alias, the orders.md key.
CSV = "REQ::FR::STAFF_DOWNLOAD_PAST_ORDERS_CSV"
ORDERS_SERVICE = "MOD::ORDERS_SERVICE"
UPDATE_3_CARRIED = (
    ORDERS_SERVICE,
    "REQ::FR::SYSTEM_LOG_EVERY_EXPORT_AUDIT",
    "REQ::INT::ORDERS_SERVICE_SENDS_EACH_PURCHASE",
)


# Builds


@dataclass
class Built:
    """One finished build of a project and the graph state right after it."""

    snapshot: int
    ctx: BuildContext
    result: BuildResult
    gateway: ScriptedGateway
    stores: FileStores
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Spec nodes by ``spec_id``."""
    fragments: dict[str, dict[str, Any]] = field(default_factory=dict)
    """``Fragment`` nodes by ``fragment_id``."""
    documents: dict[str, str] = field(default_factory=dict)
    """``Document`` status by ``document_id``."""
    registry: dict[str, SpecIdRow] = field(default_factory=dict)
    handles: dict[str, str] = field(default_factory=dict)
    """Fragment id of each answer-file handle in this build's plan."""

    def calls(self) -> dict[str, int]:
        """Model calls per prompt, embeddings aside."""

        return dict(Counter(call.prompt for call in self.gateway.calls if call.call_class != "embedding"))

    def keys(self, prompt: str) -> set[Any]:
        return set(self.gateway.keys_for(prompt))

    async def artifact(self, step: str, model: Any) -> Any:
        payload = await self.stores.builds.get_artifact(self.ctx.project_id, self.result.build_id, step)
        assert payload is not None, step
        return model.model_validate(payload)

    def named(self, name: str) -> dict[str, Any]:
        found = [node for node in self.nodes.values() if node["name"] == name]
        assert len(found) == 1, f"{len(found)} spec nodes named {name!r}"
        return found[0]

    def fragment(self, handle: str) -> dict[str, Any]:
        return self.fragments[self.handles[handle]]


class Shop:
    """Builds of one project from the shop snapshots, sharing file stores and the corpus index."""

    def __init__(
        self,
        project: Neo4jProject,
        gateways: Callable[..., ScriptedGateway],
        root: Path,
        *,
        project_id: str | None = None,
        ontology: Ontology | None = None,
    ) -> None:
        self.project = project
        self.gateways = gateways
        self.root = root
        self.project_id = project_id or project.project_id
        self.ontology = ontology or project.ontology
        self.stores = open_file_stores(root)

    def gateway(self, answers: ShopAnswers) -> ScriptedGateway:
        return answers.install(self.gateways(live_llm_config()))

    def context(
        self,
        snapshot: int,
        gateway: ScriptedGateway,
        *,
        rebuild: bool = False,
        retry: tuple[str, ...] = (),
        options: BuildOptions = OPTIONS,
    ) -> BuildContext:
        return BuildContext(
            project_id=self.project_id,
            ontology=self.ontology,
            driver=self.project.driver,
            gateway=gateway,
            registry=self.stores.registry,
            corpus=shop_corpus(snapshot, self.root),
            extraction=self.stores.extraction,
            builds=self.stores.builds,
            rebuild=rebuild,
            options=options,
            retry_lineages=retry,
            database=self.project.database,
            settings=BuildSettings(inference=False),  # judge answers are scripted by the inference tests (4.T1)
        )

    async def build(
        self,
        snapshot: int,
        *,
        answers: ShopAnswers | None = None,
        rebuild: bool = False,
        retry: tuple[str, ...] = (),
    ) -> Built:
        answers = answers or shop_answers(snapshot)
        gateway = self.gateway(answers)
        result = await run_build(self.context(snapshot, gateway, rebuild=rebuild, retry=retry))
        assert result.failed_step is None, (result.failed_step, result.error_code)
        return await self.observe(snapshot, answers, result, gateway)

    async def observe(
        self, snapshot: int, answers: ShopAnswers, result: BuildResult, gateway: ScriptedGateway
    ) -> Built:
        """The graph state after ``result``'s build, with the invariants checked (FR-BLD-08)."""

        ctx = self.context(snapshot, gateway).with_build_id(result.build_id)
        built = Built(snapshot, ctx, result, gateway, self.stores)
        plan = await built.artifact("plan", PlanArtifact)
        built.handles = answers.fragment_ids(plan)
        built.nodes = {
            row["spec_id"]: row
            for row in await self.project.run(
                "MATCH (n:SpecNode) WHERE n.group_id = $p "
                "RETURN n.spec_id AS spec_id, n.uuid AS uuid, n.name AS name, n.identity_key AS key, "
                "coalesce(n.identity_aliases, []) AS aliases, coalesce(n.spec_id_aliases, []) AS spec_id_aliases, "
                "[l IN labels(n) WHERE l <> 'SpecNode'][0] AS label, "
                "COLLECT { MATCH (n)-[:REFERENCED_BY]->(f:Fragment) RETURN DISTINCT f.document_id ORDER BY "
                "f.document_id } "
                "AS documents",
                p=self.project_id,
            )
        }
        built.fragments = {
            row["fragment_id"]: row
            for row in await self.project.run(
                "MATCH (f:Fragment) WHERE f.group_id = $p "
                "RETURN f.fragment_id AS fragment_id, f.document_id AS document, f.status AS status, "
                "f.error_code AS code, f.content_hash AS content, f.extracted_hash AS extracted",
                p=self.project_id,
            )
        }
        built.documents = {
            row["document"]: row["status"]
            for row in await self.project.run(
                "MATCH (d:Document) WHERE d.group_id = $p RETURN d.document_id AS document, d.status AS status",
                p=self.project_id,
            )
        }
        built.registry = await self.stores.registry.taken(self.project_id)
        assert await invariant_violations(self.project, self.project_id, self.ontology) == {}
        return built


async def invariant_violations(project: Neo4jProject, project_id: str, ontology: Ontology) -> dict[str, list[Any]]:
    """D1-D4 (data-model.md) of one project by query; an empty mapping when all hold."""

    found: dict[str, list[Any]] = {}
    domain = set(ontology.labels)
    # D1: SpecNode plus exactly one domain label
    rows = await project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p AND size([l IN labels(n) WHERE l <> 'SpecNode']) <> 1 "
        "RETURN n.spec_id AS spec_id, labels(n) AS labels",
        p=project_id,
    )
    rows += [
        row
        for row in await project.run(
            "MATCH (n:SpecNode) WHERE n.group_id = $p RETURN n.spec_id AS spec_id, labels(n) AS labels", p=project_id
        )
        if not set(row["labels"]) - {"SpecNode"} <= domain
    ]
    found["D1 labels"] = rows
    # D2: every build-owned domain edge on a legal pair; edge keys and uuids unique
    legal = ontology.edge_type_map()
    edges = await project.run(
        "MATCH (s:SpecNode)-[r]->(t:SpecNode) WHERE s.group_id = $p "
        "RETURN type(r) AS relation, [l IN labels(s) WHERE l <> 'SpecNode'][0] AS source, "
        "[l IN labels(t) WHERE l <> 'SpecNode'][0] AS target, r.group_id AS group_id, r.uuid AS uuid, "
        "r.edge_key AS edge_key, r.basis AS basis, r.source_fragment_ids AS fragments",
        p=project_id,
    )
    owned = [row for row in edges if ontology.owner_of(row["relation"]) == "build"]
    found["D2 illegal edges"] = [
        row
        for row in owned
        if row["relation"] not in legal.get((row["source"], row["target"]), ()) or row["group_id"] != project_id
    ]
    found["D2 duplicate edge keys"] = [
        key for key, n in Counter(row["edge_key"] for row in owned).items() if n > 1 or not key
    ]
    found["D2 duplicate edge uuids"] = [
        key for key, n in Counter(row["uuid"] for row in owned).items() if n > 1 or not key
    ]
    # D3: spec_id, uuid and identity key unique per project
    keys = await project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p "
        "RETURN count(n) AS nodes, count(DISTINCT n.spec_id) AS spec_ids, count(DISTINCT n.uuid) AS uuids, "
        "count(DISTINCT n.identity_key) AS identity_keys",
        p=project_id,
    )
    if len(set(keys[0].values())) != 1:
        found["D3 duplicates"] = keys
    # D4: every spec node grounded in an active or skipped fragment, never pointing at a retired one;
    # every stated or implied edge with evidence in non-retired fragments of the project
    found["D4 ungrounded nodes"] = await project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p AND NOT EXISTS { "
        "MATCH (n)-[:REFERENCED_BY]->(f:Fragment) WHERE f.group_id = $p AND f.status IN ['active', 'skipped'] } "
        "RETURN n.spec_id AS spec_id",
        p=project_id,
    )
    found["D4 links to retired fragments"] = await project.run(
        "MATCH (n:SpecNode)-[r:REFERENCED_BY|MENTIONED_IN]->(f:Fragment) WHERE n.group_id = $p AND f.status = "
        "'retired' "
        "RETURN n.spec_id AS spec_id, type(r) AS link, f.fragment_id AS fragment",
        p=project_id,
    )
    live = {
        row["id"]
        for row in await project.run(
            "MATCH (f:Fragment) WHERE f.group_id = $p AND f.status <> 'retired' RETURN f.fragment_id AS id",
            p=project_id,
        )
    }
    found["D4 edges without live evidence"] = [
        row
        for row in owned
        if row["basis"] not in ("stated", "implied") or not row["fragments"] or not set(row["fragments"]) <= live
    ]
    found["cross-project relationships"] = await project.run(
        "MATCH (s)-[r]-(t) WHERE s.group_id = $p AND (t.group_id <> $p OR r.group_id <> $p) RETURN type(r) AS type",
        p=project_id,
    )
    return {name: rows for name, rows in found.items() if rows}


@dataclass
class Chain:
    first: Built
    second: Built
    third: Built
    unchanged: Built
    rebuilt: Built


@pytest.fixture
async def chain(neo4j_project: Neo4jProject, scripted_gateway: Any, tmp_path: Path) -> Chain:
    shop = Shop(neo4j_project, scripted_gateway, tmp_path / "stores")
    first = await shop.build(1)
    second = await shop.build(2)
    third = await shop.build(3)
    unchanged = await shop.build(3)
    rebuilt = await shop.build(3, rebuild=True)
    return Chain(first, second, third, unchanged, rebuilt)


async def reextracted_documents(built: Built) -> set[str]:
    """Documents of the units whose nodes or edges this build asked the model for."""

    plan = await built.artifact("plan", PlanArtifact)
    nodes = await built.artifact("extract_nodes", NodesArtifact)
    edges = await built.artifact("extract_edges", EdgesArtifact)
    document_of = {unit.unit_id: unit.document_id for unit in plan.units}
    units = {raw.unit_id for raw in nodes.raw_nodes if not raw.reused} | set(edges.called_units)
    return {document_of[unit_id] for unit_id in units}


async def check_modified(built: Built, documents: set[str], reworded: str) -> None:
    """``modified`` holds the reworded node and only nodes grounded in a re-extracted document."""

    modified = built.result.spec_changes.modified
    assert await reextracted_documents(built) == documents
    assert reworded in modified
    assert {
        spec_id: built.nodes[spec_id]["documents"]
        for spec_id in modified
        if not documents & set(built.nodes[spec_id]["documents"])
    } == {}


def kept(before: Built, after: Built, spec_ids: Any = None) -> dict[str, tuple[str, str]]:
    """``(uuid before, uuid after)`` of the spec_ids of ``before`` (or ``spec_ids``) whose uuid differs
    in ``after`` or that are gone from it (empty when every one kept its spec_id and uuid)."""

    wanted = before.nodes if spec_ids is None else spec_ids
    return {
        spec_id: (before.nodes[spec_id]["uuid"], after.nodes.get(spec_id, {}).get("uuid"))
        for spec_id in wanted
        if after.nodes.get(spec_id, {}).get("uuid") != before.nodes[spec_id]["uuid"]
    }


def report(request: pytest.FixtureRequest, line: str, **fields: Any) -> None:
    """A report line in the test output and the log (NFR-PERF-01 is reported, never asserted)."""

    log_event(logger, INFO, "tests.graph_update.report", line, **fields)
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"\n{line}")


# FR-BLD-03, FR-BLD-22, FR-BLD-25, NFR-PERF-01: modes and the calls each build makes


async def test_each_build_calls_the_model_only_for_what_changed(chain: Chain, request: pytest.FixtureRequest) -> None:
    builds = (chain.first, chain.second, chain.third, chain.unchanged, chain.rebuilt)
    assert [built.result.mode for built in builds] == ["first", "update", "update", "update", "rebuild"]
    assert [built.result.status for built in builds] == ["partially_succeeded"] * 5  # the malformed window stays
    assert all(built.result.settled for built in builds)
    assert [built.calls() for built in builds] == [FIRST_CALLS, UPDATE_2_CALLS, UPDATE_3_CALLS, {}, REBUILD_CALLS]
    # the no-change update makes no model call and no embedding call: known-term vectors are computed
    # only when some unit is extracted
    assert chain.unchanged.gateway.calls == []

    # update 2: the windows cover only the dirty fragments; only the checkout unit's edges are asked
    assert chain.second.keys(NODES_PROMPT) == {("chk.payment",), ("chk.delivery",)}
    assert chain.second.keys(EDGES_PROMPT) == {CHECKOUT_UNIT}
    # attributes only of the new and changed nodes (one batch)
    (batch,) = chain.second.gateway.keys_for(ATTRIBUTES_PROMPT)
    assert set(batch) == set(shop_answers(2).attributes)
    # update 3: the changed overview runs, chk.confirm and orders.md; the "Export details" unit reuses its edges
    assert chain.third.keys(NODES_PROMPT) == {
        ("chk.confirm",),
        ("ov.checkout", "ov.db", "ov.orders"),
        ("ov.arch", "ov.audit", "ov.auth"),
        ("ord.export", "ord.intro"),
        ("ord.obligations",),
        ("ord.erp", "ord.po"),
        ("ord.details1",),
        ("ord.details3",),
        ("ord.details4",),
        ("ord.details5",),
    }
    assert chain.third.keys(EDGES_PROMPT) == {CHECKOUT_UNIT, OVERVIEW_UNIT, ORDERS_UNIT}
    assert DETAILS_UNIT not in chain.third.keys(EDGES_PROMPT)
    assert chain.rebuilt.keys(EDGES_PROMPT) == {CHECKOUT_UNIT, OVERVIEW_UNIT, ORDERS_UNIT, DETAILS_UNIT}

    metrics = [(built.result.metrics.units_extracted, built.result.metrics.units_reused) for built in builds]
    assert metrics == [(6, 0), (1, 5), (4, 1), (0, 5), (5, 0)]
    nodes = [await built.artifact("extract_nodes", NodesArtifact) for built in builds]
    # fragments whose stored nodes were reused: every fragment neither dirty nor skipped
    plans = [await built.artifact("plan", PlanArtifact) for built in builds]
    reusable = [
        sum(1 for f in plan.fragments if f.fragment_id not in plan.dirty_ids and f.status != "skipped")
        for plan in plans
    ]
    assert [artifact.counters.fragments_reused for artifact in nodes] == reusable
    assert reusable[0] == reusable[-1] == 0 and all(reusable[1:-1])
    edges = [await built.artifact("extract_edges", EdgesArtifact) for built in builds]
    assert [artifact.counters.units_reused for artifact in edges] == [0, 4, 1, 4, 0]

    # NFR-PERF-01: reported, not asserted (a one-section edit of a 5-document corpus has a fixed floor)
    first_calls, update_calls = len(chain.first.gateway.calls), len(chain.second.gateway.calls)
    first_model, update_model = sum(FIRST_CALLS.values()), sum(UPDATE_2_CALLS.values())
    report(
        request,
        f"NFR-PERF-01 update/first after a one-document edit: model calls {update_model}/{first_model} = "
        f"{update_model / first_model:.1%}; all calls incl. embeddings {update_calls}/{first_calls} = "
        f"{update_calls / first_calls:.1%}",
        model_calls=[update_model, first_model],
        calls=[update_calls, first_calls],
    )


# FR-BLD-23 (update half) and FR-BLD-25


async def test_update_known_terms_come_from_persisted_hubs_and_stored_decisions_hold(chain: Chain) -> None:
    second = await chain.second.artifact("extract_nodes", NodesArtifact)
    assert second.known_terms_source == "persisted"
    hubs = {node["name"] for node in chain.first.nodes.values() if node["label"] in chain.first.ctx.ontology.hub_labels}
    for call in chain.second.gateway.calls_for(NODES_PROMPT):
        (terms,) = [
            body for label, body in untrusted_blocks(call.messages[-1]["content"]) if label == KNOWN_TERMS_LABEL
        ]
        assert set(terms.split("\n")) == hubs
    # update 3 offers the persisted name; the text's new name wins and the node is carried over
    for call in chain.third.gateway.calls_for(NODES_PROMPT):
        (terms,) = [
            body for label, body in untrusted_blocks(call.messages[-1]["content"]) if label == KNOWN_TERMS_LABEL
        ]
        assert "Orders service" in terms.split("\n") and "Order management service" not in terms.split("\n")
    # the hub answers of both updates group nothing, yet "Audit trail" stays merged into "Audit logging":
    # the decision stored by build 1 overrides the model
    for built in (chain.second, chain.third, chain.rebuilt):
        assert built.named("Audit logging")["aliases"] == ["Capability|surface|audit trail"]
        assert built.named("Audit logging")["uuid"] == chain.first.named("Audit logging")["uuid"]
        assert all(node["name"] != "Audit trail" for node in built.nodes.values())


# FR-BLD-02, 04, 05, 06: snapshot 2


async def test_update_2_edits_removes_and_adds_with_stable_identities(chain: Chain) -> None:
    first, second = chain.first, chain.second
    changes = second.result.spec_changes
    assert changes.added == UPDATE_2_ADDED
    assert changes.removed == tuple(sorted((BUSINESS_RULE, *APPROVAL_FRS)))
    await check_modified(second, {"checkout.md"}, REWORDED_2)
    assert (changes.rekeyed, changes.carried_over) == ((), ())

    plan = await second.artifact("plan", PlanArtifact)
    assert plan.change_set.model_dump() == {
        "new": (),
        "revised": ("checkout.md",),
        "unchanged": ("admin.md", "notes.md", "orders.md", "overview.md"),
        "removed": (),
        "retried": (),
    }
    by_id = {fragment.fragment_id: fragment for fragment in plan.fragments}
    assert by_id[second.handles["chk.payment"]].change == "changed"
    assert by_id[second.handles["chk.delivery"]].change == "new"
    assert set(plan.dirty_ids) == {second.handles["chk.payment"], second.handles["chk.delivery"]}
    # FR-BLD-04: every unchanged section keeps its fragment id, the edited one too (same heading path and first line)
    assert {handle: second.handles[handle] for handle in first.handles if handle != "chk.rules"} == {
        handle: fragment for handle, fragment in first.handles.items() if handle != "chk.rules"
    }
    # FR-BLD-05: the removed section's fragment retires
    assert plan.retired_ids == (first.handles["chk.rules"],)
    assert second.fragments[first.handles["chk.rules"]]["status"] == "retired"
    assert Counter(row["status"] for row in second.fragments.values()) == {"active": 42, "skipped": 1, "retired": 1}
    assert second.fragment("ord.details2")["status"] == "skipped"  # not changed, not retried: still skipped

    # the business rule and the FRs stated only in the removed section are deleted
    assert not {BUSINESS_RULE, *APPROVAL_FRS} & set(second.nodes)
    # FR-BLD-06: every other node keeps spec_id and uuid
    assert kept(first, second, set(first.nodes) - {BUSINESS_RULE, *APPROVAL_FRS}) == {}
    unchanged = set(first.nodes) - set(changes.removed) - set(changes.modified)
    assert all(second.nodes[spec_id]["name"] == first.nodes[spec_id]["name"] for spec_id in unchanged)
    # the reworded FR-CHK-004 keeps its id and spec_id with the new name; FR-CHK-007 is keyed by its id
    reworded = second.nodes["REQ::FR::FR_CHK_004_SYSTEM_SHOW_ERROR_KEEP"]
    assert (reworded["key"], reworded["name"]) == (
        "requirement|id|FR-CHK-004",
        "System can keep the basket and name the reason when a payment is declined",
    )
    assert second.nodes["REQ::FR::FR_CHK_007_SYSTEM_LET_CUSTOMER_CHOOSE"]["key"] == "requirement|id|FR-CHK-007"


# FR-BLD-02, 05, 06 and the rename: snapshot 3


async def test_update_3_removes_a_document_and_carries_the_renamed_module_over(
    chain: Chain, neo4j_project: Neo4jProject
) -> None:
    second, third = chain.second, chain.third
    changes = third.result.spec_changes
    assert changes.added == ()
    assert changes.removed == ADMIN_ONLY
    assert changes.rekeyed == (CSV,)
    assert changes.carried_over == UPDATE_3_CARRIED
    await check_modified(third, {"checkout.md", "orders.md", "overview.md"}, REWORDED_3)

    plan = await third.artifact("plan", PlanArtifact)
    assert plan.change_set.model_dump() == {
        "new": (),
        "revised": ("checkout.md", "orders.md", "overview.md"),
        "unchanged": ("notes.md",),
        "removed": ("admin.md",),
        "retried": (),
    }
    assert third.documents == dict.fromkeys(("checkout.md", "notes.md", "orders.md", "overview.md"), "active") | {
        "admin.md": "removed"
    }
    # every admin.md fragment retires, and so does every orders.md fragment id (the title is in the
    # heading path) and every overview.md id whose heading or first body line names the module
    admin = [handle for handle in second.handles if handle.startswith("adm.")]
    assert all(third.fragments[second.handles[handle]]["status"] == "retired" for handle in admin)
    assert set(plan.retired_ids) == {
        *(second.handles[handle] for handle in admin),
        *(second.handles[handle] for handle in second.handles if handle.startswith("ord.")),
        second.handles["ov.orders"],
        second.handles["ov.db"],
    }
    # the other overview.md and checkout.md sections keep their ids, changed or not (FR-BLD-04)
    same = [
        handle
        for handle in third.handles
        if handle.startswith(("ov.", "chk.", "nts.")) and handle not in ("ov.orders", "ov.db")
    ]
    assert {handle: third.handles[handle] for handle in same} == {handle: second.handles[handle] for handle in same}
    assert third.fragment("ord.details2")["status"] == "skipped"
    assert not await neo4j_project.run(
        "MATCH (n:SpecNode)-[:REFERENCED_BY]->(f:Fragment) WHERE n.group_id = $p AND f.document_id = 'admin.md' "
        "RETURN n",
        p=neo4j_project.project_id,
    )

    # only-admin nodes are gone; the node admin.md shared with orders.md stays, under its orders.md key
    assert not set(ADMIN_ONLY) & set(third.nodes)
    assert kept(second, third, set(second.nodes) - set(ADMIN_ONLY)) == {}
    csv = third.nodes[CSV]
    assert (csv["name"], csv["key"], csv["aliases"]) == (
        "Staff can download the order history as a CSV file",
        "requirement|span|staff users must be able to download the order history as a csv file",
        ["requirement|span|staff users must be able to download the list of past orders as a csv file"],
    )

    # the rename follows the text: same uuid and spec_id, the new name, the old key kept as an alias
    module = third.nodes[ORDERS_SERVICE]
    assert (module["uuid"], module["name"], module["key"], module["aliases"]) == (
        second.nodes[ORDERS_SERVICE]["uuid"],
        "Order management service",
        "Module|surface|order management service",
        ["Module|surface|order service"],
    )
    assert not [node for node in third.nodes.values() if "Orders service" in node["name"]]
    # "Orders database" is a different module and stays one
    assert third.named("Orders database")["uuid"] == second.named("Orders database")["uuid"]
    # the module keeps its relationships: PART_OF from the export feature, INTEGRATES_WITH from checkout
    rows = await neo4j_project.run(
        "MATCH (s:SpecNode)-[r]->(t:SpecNode {spec_id: $module}) WHERE t.group_id = $p "
        "RETURN s.name AS source, type(r) AS relation ORDER BY source, relation",
        p=neo4j_project.project_id,
        module=ORDERS_SERVICE,
    )
    assert {(row["source"], row["relation"]) for row in rows} >= {
        ("Order history export", "PART_OF"),
        ("Checkout service", "INTEGRATES_WITH"),
        ("Authentication", "REALIZED_BY"),
    }


# FR-BLD-06: no-change update and rebuild


async def test_no_change_update_changes_nothing_and_rebuild_keeps_every_identity(
    chain: Chain, neo4j_project: Neo4jProject
) -> None:
    third, unchanged, rebuilt = chain.third, chain.unchanged, chain.rebuilt
    empty = dict.fromkeys(unchanged.result.spec_changes.model_dump(), ())
    assert unchanged.result.spec_changes.model_dump() == empty
    assert unchanged.nodes == third.nodes and unchanged.fragments == third.fragments
    assert (await unchanged.artifact("plan", PlanArtifact)).dirty_ids == ()

    # a rebuild re-extracts everything, reuses only vectors and decisions, and keeps every identity
    nodes = await rebuilt.artifact("extract_nodes", NodesArtifact)
    assert (nodes.known_terms_source, nodes.counters.fragments_reused) == ("hub_catalog", 0)
    assert not any(node.reused for node in nodes.raw_nodes)
    assert rebuilt.result.spec_changes.model_dump() == empty
    assert kept(unchanged, rebuilt) == {} and set(rebuilt.nodes) == set(unchanged.nodes)
    assert {spec_id: node["name"] for spec_id, node in rebuilt.nodes.items()} == {
        spec_id: node["name"] for spec_id, node in unchanged.nodes.items()
    }
    # the hub catalog of the rebuild names the module as the text does
    assert rebuilt.named("Order management service")["spec_id"] == ORDERS_SERVICE


# FR-CORE-06: the registry


async def test_registry_follows_the_graph_and_never_reissues_a_spec_id(chain: Chain) -> None:
    builds = (chain.first, chain.second, chain.third, chain.unchanged, chain.rebuilt)
    for built in builds:
        active = {spec_id for spec_id, row in built.registry.items() if row.state == "active"}
        assert active == set(built.nodes), built.result.mode
        for spec_id, node in built.nodes.items():
            row = built.registry[spec_id]
            assert (row.uuid, row.identity_key) == (node["uuid"], node["key"])
    retired = {spec_id for spec_id, row in chain.rebuilt.registry.items() if row.state == "retired"}
    assert retired == {BUSINESS_RULE, *APPROVAL_FRS, *ADMIN_ONLY}
    # a spec_id names one node for good: never another uuid, and a retired one is never active again
    # under another identity key
    uuids: dict[str, set[str]] = {}
    for built in builds:
        for spec_id, row in built.registry.items():
            uuids.setdefault(spec_id, set()).add(row.uuid)
    assert {spec_id: found for spec_id, found in uuids.items() if len(found) > 1} == {}
    retired_keys = {spec_id: chain.second.registry[spec_id].identity_key for spec_id in (BUSINESS_RULE, *APPROVAL_FRS)}
    retired_keys |= {spec_id: chain.third.registry[spec_id].identity_key for spec_id in ADMIN_ONLY}
    for built in (chain.third, chain.unchanged, chain.rebuilt):
        for spec_id, key in retired_keys.items():
            assert built.registry[spec_id].identity_key == key


# FR-BLD-10: a foreign relationship survives updates and moves to the survivor of a merge


def gap_variant(directory: Path) -> Ontology:
    """The shipped ontology with a copy of the test ``gap`` file whose ``FLAGS`` may also end at a
    capability and start at one (towards a module or another capability)."""

    data = copy.deepcopy(GAP_ONTOLOGY)
    data["edge_types"]["FLAGS"]["pairs"] += [
        {"from": "finding", "to": "capability"},
        {"from": "capability", "to": "module"},
        {"from": "capability", "to": "capability"},
    ]
    path = directory / "gap-flags-capability.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_ontology(submodule_files=[path])


FINDING = "finding"
"""``foreign_flags``' name of the gap ``Finding`` end."""


async def test_foreign_relationships_survive_updates_and_move_to_a_merge_survivor(
    neo4j_project: Neo4jProject, scripted_gateway: Any, tmp_path: Path
) -> None:
    ontology = gap_variant(tmp_path)
    shop = Shop(neo4j_project, scripted_gateway, tmp_path / "stores", ontology=ontology)
    # build 1: the judge keeps "Audit trail" apart, so orders.md links its requirement to it
    data = shop_answer_data(1)
    data["consolidation"][0]["groups"] = []
    data["attributes"]["Audit trail"] = {
        "quote": "The Orders service must log every export in the audit trail.",
        "stmt": "The audit trail records every export.",
    }
    for entry in data["edges"]:
        for fact in entry["facts"]:
            if fact[0] == "System can log every export in the audit trail" and fact[2] == "Audit logging":
                fact[2] = "Audit trail"
    first = await shop.build(1, answers=ShopAnswers(data, source="1.yaml (Audit trail kept apart)"))
    trail, logging_ = first.named("Audit trail"), first.named("Audit logging")
    orders = first.named("Orders service")
    gap = neo4j_project.writer("gap", ontology=ontology)
    finding = NodeRef("Finding", "gap-finding-1")
    trail_ref, logging_ref = NodeRef("Capability", trail["uuid"]), NodeRef("Capability", logging_["uuid"])
    await gap.upsert_node("Finding", finding.key, {"title": "Audit coverage"})
    # incoming and outgoing foreign relationships of the loser-to-be
    await gap.upsert_relationship("FLAGS", finding, trail_ref, "gap|trail", {"severity": "low"})
    await gap.upsert_relationship(
        "FLAGS", trail_ref, NodeRef("Module", orders["uuid"]), "gap|trail-out", {"severity": "mid"}
    )
    # one between the two nodes that will merge (it would become a self-loop)
    await gap.upsert_relationship("FLAGS", trail_ref, logging_ref, "gap|between", {"severity": "low"})
    # one the survivor already has an equal copy of (same type, direction, other end and properties)
    await gap.upsert_relationship("FLAGS", finding, trail_ref, "gap|both", {"severity": "high"})
    await neo4j_project.run(
        "MATCH (f:Finding)-[r:FLAGS {edge_key: 'gap|both'}]->(:SpecNode) WHERE r.group_id = $p "
        "MATCH (s:SpecNode {group_id: $p, uuid: $survivor}) CREATE (f)-[x:FLAGS]->(s) SET x = properties(r)",
        p=neo4j_project.project_id,
        survivor=logging_["uuid"],
    )
    await gap.upsert_relationship(
        "FLAGS", finding, NodeRef("Module", orders["uuid"]), "gap|orders", {"severity": "high"}
    )
    assert len(await foreign_flags(neo4j_project)) == 6

    # update 2: the judge now groups the two capabilities, so two current nodes meet one target: a merge
    # (the merged node's attributes are asked again, and the two orders.md units whose catalogs held
    # "Audit trail" are asked again: a hub they used is gone)
    data = shop_answer_data(2)
    data["consolidation"][0]["groups"] = [{"label": "Capability", "names": ["Audit logging", "Audit trail"]}]
    data["attributes"]["Audit logging"] = shop_answer_data(1)["attributes"]["Audit logging"]
    data["edges"] += [
        entry for entry in shop_answer_data(1)["edges"] if entry["unit"][0] in ("ord.intro", "ord.details3")
    ]
    second = await shop.build(2, answers=ShopAnswers(data, source="2.yaml (Audit trail merged)"))
    survivor = second.named("Audit logging")
    assert (survivor["uuid"], survivor["spec_id"]) == (logging_["uuid"], logging_["spec_id"])
    assert trail["spec_id"] in survivor["spec_id_aliases"]
    assert trail["spec_id"] in second.result.spec_changes.rekeyed
    assert second.registry[trail["spec_id"]].state == "alias"
    flags = await foreign_flags(neo4j_project)
    # each moved to the survivor with its direction and properties; the equal one is not duplicated;
    # the one between the merged nodes is dropped
    assert flags == [
        ("gap|both", FINDING, logging_["uuid"], "high"),
        ("gap|orders", FINDING, orders["uuid"], "high"),
        ("gap|trail", FINDING, logging_["uuid"], "low"),
        ("gap|trail-out", logging_["uuid"], orders["uuid"], "mid"),
    ]
    assert not await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, uuid: $uuid}) RETURN n", p=neo4j_project.project_id, uuid=trail["uuid"]
    )

    # update 3: the renamed module is carried over and keeps its foreign relationships, both directions
    third = await shop.build(3)
    assert third.nodes[ORDERS_SERVICE]["uuid"] == orders["uuid"]
    assert await foreign_flags(neo4j_project) == flags
    assert await neo4j_project.run(
        "MATCH (f:Finding) WHERE f.group_id = $p RETURN f.title AS title", p=neo4j_project.project_id
    ) == [{"title": "Audit coverage"}]


async def foreign_flags(project: Neo4jProject) -> list[tuple[str, str, str, str]]:
    """Every ``FLAGS`` relationship of the project as ``(edge_key, source, target, severity)``, sorted
    (a list, so a duplicate shows); a spec node end is its uuid, the finding ``FINDING``."""

    rows = await project.run(
        "MATCH (s)-[r:FLAGS]->(t) WHERE r.group_id = $p "
        "RETURN r.edge_key AS key, CASE WHEN s:SpecNode THEN s.uuid ELSE $finding END AS source, "
        "CASE WHEN t:SpecNode THEN t.uuid ELSE $finding END AS target, r.severity AS severity",
        p=project.project_id,
        finding=FINDING,
    )
    return sorted((row["key"], row["source"], row["target"], row["severity"]) for row in rows)


# FR-BLD-13: an interrupted persist resumes from its cursor


PERSIST_TEST_BATCH = 7
"""Batch size of the persist test, so the update diff spans several batches."""


class CrashError(RuntimeError):
    """Injected after a committed persist batch."""


def canonical_graph(rows_nodes: list[dict[str, Any]], rows_edges: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Nodes and relationships of a project without what differs between two projects built from one
    corpus: ids derived from the project id, build ids, write stamps and statement times."""

    volatile = {"uuid", "group_id", "created_at", "updated_at", "created_build_id", "last_build_id"}

    def clean(props: Mapping[str, Any]) -> dict[str, Any]:
        result = {key: value for key, value in props.items() if key not in volatile}
        if isinstance(result.get("statements"), str):
            result["statements"] = [
                {key: value for key, value in statement.items() if key != "at"}
                for statement in json.loads(result["statements"])
            ]
        for name in ("embedding", "name_embedding"):
            if isinstance(result.get(name), list):
                result[name] = hashlib.sha256(json.dumps([round(x, 6) for x in result[name]]).encode()).hexdigest()
        return result

    nodes = sorted(json.dumps([row["labels"], clean(row["props"])], sort_keys=True, default=str) for row in rows_nodes)
    edges = sorted(
        json.dumps([row["source"], row["type"], row["target"], clean(row["props"])], sort_keys=True, default=str)
        for row in rows_edges
    )
    return nodes, edges


async def project_graph(project: Neo4jProject, project_id: str) -> tuple[list[str], list[str]]:
    nodes = await project.run(
        "MATCH (n) WHERE n.group_id = $p RETURN labels(n) AS labels, properties(n) AS props", p=project_id
    )
    edges = await project.run(
        "MATCH (s)-[r]->(t) WHERE s.group_id = $p "
        "RETURN coalesce(s.spec_id, s.fragment_id, s.document_id) AS source, type(r) AS type, "
        "coalesce(t.spec_id, t.fragment_id, t.document_id) AS target, properties(r) AS props",
        p=project_id,
    )
    return canonical_graph(nodes, edges)


async def test_interrupted_update_persist_resumes_to_the_uninterrupted_graph(
    neo4j_project: Neo4jProject, scripted_gateway: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batches = writing.batches
    monkeypatch.setattr(writing, "batches", lambda operations, *_: batches(operations, PERSIST_TEST_BATCH))
    # uninterrupted: 1 -> 2 in one project
    plain = Shop(neo4j_project, scripted_gateway, tmp_path / "plain")
    await plain.build(1)
    expected = await plain.build(2)
    cursor = await expected.artifact("persist", PersistCursor)
    assert cursor.done and cursor.batches >= 4

    # interrupted: the same builds in another project, the update's persist crashing after batch 2
    crashed = Shop(neo4j_project, scripted_gateway, tmp_path / "crashed", project_id=neo4j_project.new_project())
    await crashed.build(1)
    applied: list[int] = []

    def crash_after_second(index: int) -> None:
        applied.append(index)
        if index == 2:
            raise CrashError("injected")

    async def persist_with_crash(ctx: BuildContext) -> None:
        await steps.persist(ctx, on_batch=crash_after_second)

    gateway = crashed.gateway(shop_answers(2))
    ctx = crashed.context(2, gateway)
    with monkeypatch.context() as patch:
        patch.setitem(runner.STEP_FUNCTIONS, "persist", persist_with_crash)
        with pytest.raises(CrashError):
            await run_build(ctx)
    assert applied == [0, 1, 2]
    record = await crashed.stores.builds.latest_build(crashed.project_id)
    assert record is not None and not record.finished and record.mode == "update"
    build_id = record.build_id
    stopped = PersistCursor.model_validate(
        await crashed.stores.builds.get_artifact(crashed.project_id, build_id, "persist")
    )
    assert (stopped.batch_index, stopped.done) == (2, False)

    # resume: the build continues at persist, batch 3, without any model call
    calls = len(gateway.calls)
    resumed_applied: list[int] = []

    async def persist_recording(ctx: BuildContext) -> None:
        await steps.persist(ctx, on_batch=resumed_applied.append)

    with monkeypatch.context() as patch:
        patch.setitem(runner.STEP_FUNCTIONS, "persist", persist_recording)
        result = await run_build(ctx.with_build_id(build_id))
    assert result.failed_step is None and result.settled
    assert resumed_applied == list(range(3, stopped.batches))
    assert len(gateway.calls) == calls
    assert result.spec_changes == expected.result.spec_changes
    await crashed.observe(2, shop_answers(2), result, gateway)
    resumed_graph = await project_graph(neo4j_project, crashed.project_id)
    assert len(resumed_graph[0]) == len(expected.nodes) + len(expected.fragments) + len(expected.documents)
    assert resumed_graph == await project_graph(neo4j_project, plain.project_id)


# FR-BLD-12: a failed window keeps the fragment's last extracted version, a retry recovers it


async def test_failed_window_keeps_the_last_version_until_a_retry_recovers_it(
    neo4j_project: Neo4jProject, scripted_gateway: Any, tmp_path: Path
) -> None:
    shop = Shop(neo4j_project, scripted_gateway, tmp_path / "stores")
    first = await shop.build(1)
    # update 2 with the "Payment flow" window failing: the edge and attribute requests show the
    # fragment's stored (last extracted) nodes, so they are answered without the new payment nodes
    data = shop_answer_data(2)
    payment = next(item for item in data["nodes"] if item["window"] == ["chk.payment"])
    payment.pop("entities")
    payment["error"] = "malformed"
    new_payment = {
        "System can keep the basket and name the reason when a payment is declined",
        "System can offer a retry with another card from the error message",
    }
    for entry in data["edges"]:
        entry["facts"] = [fact for fact in entry["facts"] if not {fact[0], fact[2]} & new_payment]
    for name in new_payment:
        data["attributes"].pop(name)
    old_name = "System can show an error and keep the basket when a payment is declined"
    data["attributes"][old_name] = shop_answer_data(1)["attributes"][old_name]
    failed = await shop.build(2, answers=ShopAnswers(data, source="2.yaml (payment window fails)"))
    assert failed.result.status == "partially_succeeded"
    fragment = failed.fragment("chk.payment")
    assert (fragment["status"], fragment["code"]) == ("skipped", "llm_malformed")
    assert failed.result.skipped_fragments[failed.handles["chk.payment"]] == "llm_malformed"
    # the fragment's text is the new one; the extracted version is still the old one
    assert fragment["extracted"] == first.fragment("chk.payment")["content"] != fragment["content"]
    old = first.nodes["REQ::FR::FR_CHK_004_SYSTEM_SHOW_ERROR_KEEP"]
    kept_node = failed.nodes["REQ::FR::FR_CHK_004_SYSTEM_SHOW_ERROR_KEEP"]
    assert (kept_node["uuid"], kept_node["name"]) == (old["uuid"], old["name"])
    assert {"FEAT::PAYMENT_FLOW", "REQ::FR::FR_CHK_003_SYSTEM_CREATE_STRIPE_PAYMENT"} <= set(failed.nodes)
    assert "REQ::FR::SYSTEM_OFFER_RETRY_ANOTHER_CARD" not in failed.nodes
    assert kept(first, failed, set(first.nodes) - {BUSINESS_RULE, *APPROVAL_FRS}) == {}
    # its evidence is the old version's line in the skipped fragment, its edge to the feature stays
    evidence = await neo4j_project.run(
        "MATCH (n:SpecNode {spec_id: 'REQ::FR::FR_CHK_004_SYSTEM_SHOW_ERROR_KEEP'})-[r:REFERENCED_BY]->(f:Fragment) "
        "WHERE n.group_id = $p RETURN f.fragment_id AS fragment, r.line_start AS start, r.line_end AS end",
        p=neo4j_project.project_id,
    )
    assert evidence == [{"fragment": failed.handles["chk.payment"], "start": 22, "end": 22}]
    assert await neo4j_project.run(
        "MATCH (n:SpecNode {spec_id: 'REQ::FR::FR_CHK_004_SYSTEM_SHOW_ERROR_KEEP'})-[r:PART_OF]->(t:SpecNode) "
        "WHERE n.group_id = $p RETURN t.name AS target, r.fact AS fact",
        p=neo4j_project.project_id,
    ) == [
        {
            "target": "Payment flow",
            "fact": "FR-CHK-004: The Checkout service must show a clear error message and keep the basket when a "
            "payment is declined.",
        }
    ]

    # the next update without a retry leaves it skipped and makes no call for it
    again = await shop.build(2, answers=ShopAnswers(data, source="2.yaml (payment window fails)"))
    assert again.calls() == {} and again.fragment("chk.payment")["status"] == "skipped"

    # a retry of checkout.md extracts it again and it recovers
    retried = await shop.build(2, retry=("checkout.md",))
    assert retried.keys(NODES_PROMPT) == {("chk.payment",)}
    assert (retried.fragment("chk.payment")["status"], retried.fragment("chk.payment")["code"]) == ("active", None)
    assert retried.handles["chk.payment"] not in retried.result.skipped_fragments
    node = retried.nodes["REQ::FR::FR_CHK_004_SYSTEM_SHOW_ERROR_KEEP"]
    assert (node["uuid"], node["name"]) == (
        old["uuid"],
        "System can keep the basket and name the reason when a payment is declined",
    )
    assert "REQ::FR::SYSTEM_OFFER_RETRY_ANOTHER_CARD" in retried.nodes


# FR-BLD-14: frozen options


@pytest.mark.parametrize(
    "change",
    [
        {"fragment_tokens": {**OPTIONS.fragment_tokens.model_dump(), "target": OPTIONS.fragment_tokens.target + 30}},
        {"known_terms_limit": 60},
        {"locality": "strict"},
        {"catalog_limit": 10},
    ],
)
async def test_update_with_changed_options_raises_before_any_call(
    neo4j_project: Neo4jProject, scripted_gateway: Any, tmp_path: Path, change: dict[str, Any]
) -> None:
    shop = Shop(neo4j_project, scripted_gateway, tmp_path / "stores")
    first = await shop.build(1)
    before = await neo4j_project.snapshot()
    gateway = shop.gateway(shop_answers(2))
    options = BuildOptions.model_validate({**OPTIONS.model_dump(), **change})
    with pytest.raises(ConfigurationError) as raised:
        await run_build(shop.context(2, gateway, options=options))
    assert raised.value.code == "frozen_options_changed"
    assert gateway.calls == []
    assert (await shop.stores.builds.latest_build(shop.project_id)).build_id == first.result.build_id
    assert await neo4j_project.snapshot() == before


# Scale report F1: stored nodes on several fragments come back once each


def spanning_answers() -> ShopAnswers:
    """Shop snapshot 1 where one node-extraction call gives nodes on two of its fragments: hubs, an
    id-less requirement (span key) and a hub one call names as a Feature and two others as a
    Capability (the label vote)."""

    data = shop_answer_data(1)
    spans = {
        "Checkout service": ["chk.intro", "chk.basket"],
        "Payment flow": ["chk.basket", "chk.payment", "chk.confirm"],
        "System can record the manager approval as an audit event": ["chk.rules", "chk.stripe"],
        "System can retry a Stripe authorisation up to two times": ["chk.stripe", "chk.retries"],
        "Stripe": ["chk.stripe", "chk.retries"],
    }
    for window in data["nodes"]:
        for entity in window.get("entities") or ():
            if entity[0] in spans and set(spans[entity[0]]) <= set(window["window"]):
                entity[2] = spans[entity[0]]
        # two more calls name "Payment flow" as a Capability: it wins 2 to 1 (one per fragment: 3 to 2)
        if window["window"][0] in ("chk.quality", "ord.intro") and "entities" in window:
            window["entities"].append(["Payment flow", "Capability", [window["window"][-1]]])
    return ShopAnswers(data, source="1.yaml (spanning)")


async def test_a_no_change_update_over_nodes_on_several_fragments_is_a_no_op(
    neo4j_project: Neo4jProject, scripted_gateway: Any, tmp_path: Path
) -> None:
    shop = Shop(neo4j_project, scripted_gateway, tmp_path)
    answers = spanning_answers()
    first = await shop.build(1, answers=answers)
    raw = await first.artifact("extract_nodes", NodesArtifact)
    assert any(len(node.fragment_ids) > 1 for node in raw.raw_nodes)
    assert first.named("Payment flow")["label"] == "Capability"
    graph = await project_graph(neo4j_project, shop.project_id)

    unchanged = await shop.build(1, answers=answers)
    reused = await unchanged.artifact("extract_nodes", NodesArtifact)
    assert unchanged.result.mode == "update"
    # every stored raw node comes back once, on all its fragments
    view = sorted((node.label, node.name, node.fragment_ids) for node in raw.raw_nodes)
    assert sorted((node.label, node.name, node.fragment_ids) for node in reused.raw_nodes) == view
    assert all(node.reused for node in reused.raw_nodes)
    assert unchanged.gateway.calls == []
    assert unchanged.result.spec_changes.model_dump() == dict.fromkeys(unchanged.result.spec_changes.model_dump(), ())
    assert (await unchanged.artifact("diff", DiffArtifact)).operations == ()
    assert await project_graph(neo4j_project, shop.project_id) == graph
