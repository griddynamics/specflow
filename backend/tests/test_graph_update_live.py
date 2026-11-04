"""Live graph-store update and rebuild builds of the shop corpus with the scripted gateway."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from graph.build import (
    MAX_FOLLOW_UPS,
    BuildContext,
    BuildOptions,
    BuildResult,
    BuildSettings,
    FileCorpusSource,
    FileStores,
    ProjectBuildStatus,
    open_file_stores,
    project_status,
    run_build,
    steps,
)
from graph.build import edges as edge_extraction
from graph.build import persist as writing
from graph.build import run as runner
from graph.build.changes import EdgeWindowParams
from graph.build.conform import DEDUP_PROMPT
from graph.build.consolidate import HUBS_PROMPT
from graph.build.models import DiffArtifact, EdgesArtifact, NodesArtifact, PersistCursor, PlanArtifact, ResolveArtifact
from graph.build.prompts import ATTRIBUTES_PROMPT, EDGE_WINDOW_PROMPT, NODE_HUBS_PROMPT, NODE_STATEMENTS_PROMPT
from graph.core import ConfigurationError, Ontology
from graph.core.identity import SpecIdRow
from graph.core.writer import NodeRef
from graph_probes import requires_live_graph
from graph_probes.base import EdgeSpec, Match, ProbeNode
from graph_probes.views import domain_edges, identity_counts, spec_edges, ungrounded_spec_ids
from graph_probes.views import fragment_ids as fragment_ids_of
from graph_support import (
    GAP_ONTOLOGY,
    SHOP_DIR,
    GraphProject,
    ScriptedGateway,
    ScriptedRequest,
    ShopAnswers,
    known_terms_of,
    live_llm_config,
    load_frozen_ontology,
    run_shop_build,
    shop_answer_data,
    shop_answers,
    shop_corpus,
)
from logger.logger import INFO, get_logger, log_event

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, graph_project) live in graph_support.
pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()

logger = get_logger("tests.graph_update_live")

OPTIONS = shop_answers(1).options
assert shop_answers(2).options == shop_answers(3).options == OPTIONS  # one frozen option set for the chain

# Model calls per prompt (embeddings aside) of each build of the chain: the reuse contract. Both node
# passes (hubs, then statements) call every window with a dirty fragment.
FIRST_CALLS = {
    NODE_HUBS_PROMPT: 15,
    NODE_STATEMENTS_PROMPT: 15,
    EDGE_WINDOW_PROMPT: 5,
    ATTRIBUTES_PROMPT: 3,
    HUBS_PROMPT: 1,
    DEDUP_PROMPT: 1,
}
# 1 -> 2: only the two dirty checkout.md fragments (one window each) and the checkout unit; the hub
# block is sent because it holds a new hub; no dedup candidate is new.
UPDATE_2_CALLS = {
    NODE_HUBS_PROMPT: 2,
    NODE_STATEMENTS_PROMPT: 2,
    HUBS_PROMPT: 1,
    EDGE_WINDOW_PROMPT: 1,
    ATTRIBUTES_PROMPT: 1,
}
# 2 -> 3: chk.confirm, two overview runs and every orders.md fragment but the skipped one (the title
# rename changes every heading path, so the breadcrumb the prompt shows); the "Export details" unit
# keeps its content, yet its one edge window is called too: its fragments are dirty and the renamed
# title in their heading path names the renamed hub, so its window catalog changed.
UPDATE_3_CALLS = {
    NODE_HUBS_PROMPT: 10,
    NODE_STATEMENTS_PROMPT: 10,
    HUBS_PROMPT: 1,
    EDGE_WINDOW_PROMPT: 4,
    ATTRIBUTES_PROMPT: 2,
}
# rebuild: every planned window and unit again (notes.md has no node, so no edge call); the hub
# block is skipped, every pair in it decided by earlier builds.
REBUILD_CALLS = {NODE_HUBS_PROMPT: 13, NODE_STATEMENTS_PROMPT: 13, EDGE_WINDOW_PROMPT: 4, ATTRIBUTES_PROMPT: 2}


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

# Spec changes of the chain.
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
        project: GraphProject,
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
        corpus: Path | None = None,
    ) -> BuildContext:
        """``corpus``: a corpus folder in place of the snapshot's (an edited copy of it)."""

        return BuildContext(
            project_id=self.project_id,
            ontology=self.ontology,
            driver=self.project.store,
            gateway=gateway,
            registry=self.stores.registry,
            corpus=FileCorpusSource(corpus, self.root) if corpus else shop_corpus(snapshot, self.root),
            extraction=self.stores.extraction,
            builds=self.stores.builds,
            rebuild=rebuild,
            options=options,
            retry_lineages=retry,
            settings=BuildSettings(inference=False),  # judge answers are scripted by the inference tests (4.T1)
        )

    async def build(
        self,
        snapshot: int,
        *,
        answers: ShopAnswers | None = None,
        rebuild: bool = False,
        retry: tuple[str, ...] = (),
        corpus: Path | None = None,
        follow_ups: int = 0,
    ) -> Built:
        """``follow_ups``: the follow-up builds ``run_build`` may chain (``run_shop_build``); the
        result is the chain's last build."""

        answers = answers or shop_answers(snapshot)
        gateway = self.gateway(answers)
        result = await run_shop_build(
            self.context(snapshot, gateway, rebuild=rebuild, retry=retry, corpus=corpus), follow_ups=follow_ups
        )
        assert result.failed_step is None, (result.failed_step, result.error_code)
        return await self.observe(snapshot, answers, result, gateway)

    async def observe(
        self, snapshot: int, answers: ShopAnswers, result: BuildResult, gateway: ScriptedGateway
    ) -> Built:
        """The graph state after ``result``'s build, with the invariants checked."""

        ctx = self.context(snapshot, gateway).with_build_id(result.build_id)
        built = Built(snapshot, ctx, result, gateway, self.stores)
        plan = await built.artifact("plan", PlanArtifact)
        built.handles = answers.fragment_ids(plan)
        documents_of: dict[str, set[str]] = {}
        for e in await self.project.edges("REFERENCED_BY", project_id=self.project_id):
            if "Fragment" in e.target.labels and e.target.get("document_id") is not None:
                documents_of.setdefault(e.source.id, set()).add(e.target["document_id"])
        built.nodes = {
            n.get("spec_id"): {
                "spec_id": n.get("spec_id"),
                "uuid": n.get("uuid"),
                "name": n.get("name"),
                "key": n.get("identity_key"),
                "aliases": n.get("identity_aliases") or [],
                "spec_id_aliases": n.get("spec_id_aliases") or [],
                "label": n.kind,
                "documents": sorted(documents_of.get(n.id, ())),
            }
            for n in await self.project.nodes("SpecNode", project_id=self.project_id)
        }
        built.fragments = {
            f.get("fragment_id"): {
                "fragment_id": f.get("fragment_id"),
                "document": f.get("document_id"),
                "status": f.get("status"),
                "code": f.get("error_code"),
                "content": f.get("content_hash"),
                "extracted": f.get("extracted_hash"),
            }
            for f in await self.project.nodes("Fragment", project_id=self.project_id)
        }
        built.documents = {
            d.get("document_id"): d.get("status")
            for d in await self.project.nodes("Document", project_id=self.project_id)
        }
        built.registry = await self.stores.registry.taken(self.project_id)
        assert await invariant_violations(self.project, self.project_id, self.ontology) == {}
        return built


async def invariant_violations(project: GraphProject, project_id: str, ontology: Ontology) -> dict[str, list[Any]]:
    """D1-D4 of one project by query; an empty mapping when all hold."""

    found: dict[str, list[Any]] = {}
    domain = set(ontology.labels)
    # D1: SpecNode plus exactly one domain label
    spec_nodes = [
        {"spec_id": n.get("spec_id"), "labels": list(n.labels)}
        for n in await project.nodes("SpecNode", project_id=project_id)
    ]
    rows = [row for row in spec_nodes if len(set(row["labels"]) - {"SpecNode"}) != 1]
    rows += [row for row in spec_nodes if not set(row["labels"]) - {"SpecNode"} <= domain]
    found["D1 labels"] = rows
    # D2: every build-owned domain edge on a legal pair; edge keys and uuids unique
    legal = ontology.edge_type_map()
    edges = await domain_edges(project.probe, project_id)
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
    keys = await identity_counts(project.probe, project_id)
    if len(set(keys.values())) != 1:
        found["D3 duplicates"] = [keys]
    # D4: every spec node grounded in an active or skipped fragment, never pointing at a retired one
    # every stated or implied edge with evidence in non-retired fragments of the project
    found["D4 ungrounded nodes"] = await ungrounded_spec_ids(project.probe, project_id)
    found["D4 links to retired fragments"] = [
        {"spec_id": e.source.get("spec_id"), "link": e.type, "fragment": e.target.get("fragment_id")}
        for e in await project.edges(project_id=project_id)
        if e.type in ("REFERENCED_BY", "MENTIONED_IN")
        and "SpecNode" in e.source.labels
        and e.source.get("group_id") == project_id
        and "Fragment" in e.target.labels
        and e.target.get("status") == "retired"
    ]
    live = await fragment_ids_of(project.probe, project_id, not_status="retired")
    found["D4 edges without live evidence"] = [
        row
        for row in owned
        if row["basis"] not in ("stated", "implied") or not row["fragments"] or not set(row["fragments"]) <= live
    ]
    found["cross-project relationships"] = [
        {"type": e.type}
        for e in await project.edges(project_id=project_id)
        for near, far in ((e.source, e.target), (e.target, e.source))  # the pattern is undirected
        if near.get("group_id") == project_id
        and (far.get("group_id") not in (None, project_id) or e.get("group_id") not in (None, project_id))
    ]
    return {name: rows for name, rows in found.items() if rows}


@dataclass
class Chain:
    first: Built
    second: Built
    third: Built
    unchanged: Built
    rebuilt: Built


@pytest.fixture
async def chain(graph_project: GraphProject, scripted_gateway: Any, tmp_path: Path) -> Chain:
    shop = Shop(graph_project, scripted_gateway, tmp_path / "stores")
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
    """A report line in the test output and the log."""

    log_event(logger, INFO, "tests.graph_update.report", line, **fields)
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"\n{line}")


# modes and the calls each build makes


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
    assert chain.second.keys(NODE_HUBS_PROMPT) == chain.second.keys(NODE_STATEMENTS_PROMPT)
    assert chain.second.keys(NODE_HUBS_PROMPT) == {("chk.payment",), ("chk.delivery",)}
    assert chain.second.keys(EDGE_WINDOW_PROMPT) == {CHECKOUT_UNIT}
    # attributes only of the new and changed nodes (one batch)
    (batch,) = chain.second.gateway.keys_for(ATTRIBUTES_PROMPT)
    assert set(batch) == set(shop_answers(2).attributes)
    # update 3: the changed overview runs, chk.confirm and orders.md
    assert chain.third.keys(NODE_HUBS_PROMPT) == chain.third.keys(NODE_STATEMENTS_PROMPT)
    assert chain.third.keys(NODE_HUBS_PROMPT) == {
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
    # every unit is one edge window, each holding a dirty fragment; the "Export details" window's
    # catalog took the renamed hub its heading path names
    assert chain.third.keys(EDGE_WINDOW_PROMPT) == {CHECKOUT_UNIT, OVERVIEW_UNIT, ORDERS_UNIT, DETAILS_UNIT}
    third_edges = await chain.third.artifact("extract_edges", EdgesArtifact)
    details_unit = next(
        item.unit_id
        for item in (await chain.third.artifact("plan", PlanArtifact)).units
        if item.fragment_ids[0] == chain.third.handles["ord.details3"]
    )
    assert [call.cause for call in third_edges.called_windows or ()] == ["dirty"] * 4
    assert third_edges.catalog_parts is not None
    third_names = {node.cid: node.name for node in (await chain.third.artifact("resolve", ResolveArtifact)).canonical}
    assert [third_names[cid] for cid in third_edges.catalog_parts[details_unit].heading] == ["Order management service"]
    assert chain.rebuilt.keys(EDGE_WINDOW_PROMPT) == {CHECKOUT_UNIT, OVERVIEW_UNIT, ORDERS_UNIT, DETAILS_UNIT}

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
    assert [artifact.counters.units_reused for artifact in edges] == [0, 4, 0, 4, 0]
    # every shop unit is one edge window: windows called and reused per build, warm-ups none
    window_counts = [
        (artifact.counters.windows_called, artifact.counters.windows_reused, artifact.counters.warm_ups)
        for artifact in edges
    ]
    assert window_counts == [(5, 0, 0), (1, 4, 0), (4, 0, 0), (0, 4, 0), (4, 0, 0)]

    # reported, not asserted (a one-section edit of a 5-document corpus has a fixed floor)
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


# (update half) and


async def test_update_known_terms_come_from_persisted_hubs_and_stored_decisions_hold(chain: Chain) -> None:
    second = await chain.second.artifact("extract_nodes", NodesArtifact)
    assert second.known_terms_source == "persisted_and_catalog"
    hubs = {node["name"] for node in chain.first.nodes.values() if node["label"] in chain.first.ctx.ontology.hub_labels}
    assert second.persisted_hubs is not None and {hub.name for hub in second.persisted_hubs} == hubs
    # the hub pass of an update is shown the persisted hubs its unit names (the checkout unit names
    # the Checkout service); the statement pass the persisted hubs merged with the hub pass catalog
    for call in chain.second.gateway.calls_for(NODE_HUBS_PROMPT):
        shown = {name for name, _, _ in known_terms_of(call.messages[-1]["content"])}
        assert "Checkout service" in shown and shown <= hubs
    statement_shown = [
        {name for name, _, _ in known_terms_of(call.messages[-1]["content"])}
        for call in chain.second.gateway.calls_for(NODE_STATEMENTS_PROMPT)
    ]
    # per window: the delivery window is shown the new hub of the hub pass
    assert all("Checkout service" in shown for shown in statement_shown)
    assert any("Delivery options" in shown for shown in statement_shown)
    # update 3: the hub pass is never offered the new name, the text's new name wins and the node is
    # carried over; the statement pass sees it from the hub pass catalog
    third = await chain.third.artifact("extract_nodes", NodesArtifact)
    for call in chain.third.gateway.calls_for(NODE_HUBS_PROMPT):
        assert "Order management service" not in {name for name, _, _ in known_terms_of(call.messages[-1]["content"])}
    assert "Order management service" in {hub.name for hub in third.hub_catalog}
    statement_terms = [
        {name for name, _, _ in known_terms_of(call.messages[-1]["content"])}
        for call in chain.third.gateway.calls_for(NODE_STATEMENTS_PROMPT)
    ]
    assert any("Order management service" in shown for shown in statement_terms)
    # the hub answer of update 2 groups nothing, yet "Audit trail" stays merged into "Audit logging": the
    # decision stored by build 1 overrides the model; update 3 changed the evidence of "Audit logging"
    # so that decision expired and the judge grouped the two again, which the rebuild reuses
    second = (await chain.second.artifact("resolve", ResolveArtifact)).counters.consolidation
    third = (await chain.third.artifact("resolve", ResolveArtifact)).counters.consolidation
    assert second.decision_overrides == 1  # (a decision on a hub of the edited checkout.md fragments expired)
    assert third.decision_overrides == 0 and third.decisions_expired >= 1
    for built in (chain.second, chain.third, chain.rebuilt):
        assert built.named("Audit logging")["aliases"] == ["Capability|surface|audit trail"]
        assert built.named("Audit logging")["uuid"] == chain.first.named("Audit logging")["uuid"]
        assert all(node["name"] != "Audit trail" for node in built.nodes.values())


# 04, 05, 06: snapshot 2


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
    # every unchanged section keeps its fragment id, the edited one too (same heading path and first line)
    assert {handle: second.handles[handle] for handle in first.handles if handle != "chk.rules"} == {
        handle: fragment for handle, fragment in first.handles.items() if handle != "chk.rules"
    }
    # the removed section's fragment retires
    assert plan.retired_ids == (first.handles["chk.rules"],)
    assert second.fragments[first.handles["chk.rules"]]["status"] == "retired"
    assert Counter(row["status"] for row in second.fragments.values()) == {"active": 42, "skipped": 1, "retired": 1}
    assert second.fragment("ord.details2")["status"] == "skipped"  # not changed, not retried: still skipped

    # the business rule and the FRs stated only in the removed section are deleted
    assert not {BUSINESS_RULE, *APPROVAL_FRS} & set(second.nodes)
    # every other node keeps spec_id and uuid
    assert kept(first, second, set(first.nodes) - {BUSINESS_RULE, *APPROVAL_FRS}) == {}
    unchanged = set(first.nodes) - set(changes.removed) - set(changes.modified)
    assert all(second.nodes[spec_id]["name"] == first.nodes[spec_id]["name"] for spec_id in unchanged)
    # the reworded keeps its id and spec_id with the new name; is keyed by its id
    reworded = second.nodes["REQ::FR::FR_CHK_004_SYSTEM_SHOW_ERROR_KEEP"]
    assert (reworded["key"], reworded["name"]) == (
        "requirement|id|FR-CHK-004",
        "System can keep the basket and name the reason when a payment is declined",
    )
    assert second.nodes["REQ::FR::FR_CHK_007_SYSTEM_LET_CUSTOMER_CHOOSE"]["key"] == "requirement|id|FR-CHK-007"


# 05, 06 and the rename: snapshot 3


async def test_update_3_removes_a_document_and_carries_the_renamed_module_over(
    chain: Chain, graph_project: GraphProject
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
    # the other overview.md and checkout.md sections keep their ids, changed or not
    same = [
        handle
        for handle in third.handles
        if handle.startswith(("ov.", "chk.", "nts.")) and handle not in ("ov.orders", "ov.db")
    ]
    assert {handle: third.handles[handle] for handle in same} == {handle: second.handles[handle] for handle in same}
    assert third.fragment("ord.details2")["status"] == "skipped"
    assert not [
        e
        for e in await graph_project.edges("REFERENCED_BY")
        if "SpecNode" in e.source.labels
        and e.source.get("group_id") == graph_project.project_id
        and "Fragment" in e.target.labels
        and e.target.get("document_id") == "admin.md"
    ]

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
    rows = sorted(
        (
            {"source": e.source.get("name"), "relation": e.type}
            for e in await spec_edges(graph_project.probe, graph_project.project_id)
            if e.target.get("spec_id") == ORDERS_SERVICE and e.target.get("group_id") == graph_project.project_id
        ),
        key=lambda row: (row["source"], row["relation"]),
    )
    assert {(row["source"], row["relation"]) for row in rows} >= {
        ("Order history export", "PART_OF"),
        ("Checkout service", "INTEGRATES_WITH"),
        ("Authentication", "REALIZED_BY"),
    }


# no-change update and rebuild


async def test_no_change_update_changes_nothing_and_rebuild_keeps_every_identity(
    chain: Chain, graph_project: GraphProject
) -> None:
    third, unchanged, rebuilt = chain.third, chain.unchanged, chain.rebuilt
    empty = dict.fromkeys(unchanged.result.spec_changes.model_dump(), ())
    assert unchanged.result.spec_changes.model_dump() == empty
    assert unchanged.nodes == third.nodes and unchanged.fragments == third.fragments
    assert (await unchanged.artifact("plan", PlanArtifact)).dirty_ids == ()

    # a rebuild re-extracts everything, reuses only vectors and decisions, and keeps every identity
    nodes = await rebuilt.artifact("extract_nodes", NodesArtifact)
    assert (nodes.known_terms_source, nodes.counters.fragments_reused) == ("hub_pass", 0)
    assert nodes.persisted_hubs is None  # a rebuild never shows the persisted hubs
    assert not any(node.reused for node in nodes.raw_nodes)
    assert rebuilt.result.spec_changes.model_dump() == empty
    assert kept(unchanged, rebuilt) == {} and set(rebuilt.nodes) == set(unchanged.nodes)
    assert {spec_id: node["name"] for spec_id, node in rebuilt.nodes.items()} == {
        spec_id: node["name"] for spec_id, node in unchanged.nodes.items()
    }
    # the hub catalog of the rebuild names the module as the text does
    assert rebuilt.named("Order management service")["spec_id"] == ORDERS_SERVICE


# the registry


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


# a foreign relationship survives updates and moves to the survivor of a merge


def gap_variant(directory: Path) -> Ontology:
    """The frozen v1.3 ontology (the shop answers' one, ``live_ontology``) with a copy of the test ``gap``
    file whose ``FLAGS`` may also end at a capability and start at one (towards a module or another
    capability)."""

    data = copy.deepcopy(GAP_ONTOLOGY)
    data["edge_types"]["FLAGS"]["pairs"] += [
        {"from": "finding", "to": "capability"},
        {"from": "capability", "to": "module"},
        {"from": "capability", "to": "capability"},
    ]
    path = directory / "gap-flags-capability.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_frozen_ontology(submodule_files=[path])


FINDING = "finding"
"""``foreign_flags``' name of the gap ``Finding`` end."""


async def test_foreign_relationships_survive_updates_and_move_to_a_merge_survivor(
    graph_project: GraphProject, scripted_gateway: Any, tmp_path: Path
) -> None:
    ontology = gap_variant(tmp_path)
    shop = Shop(graph_project, scripted_gateway, tmp_path / "stores", ontology=ontology)
    # build 1: the judge is unsure whether "Audit trail" is "Audit logging" (a low group: not merged
    # not stored, judged again next build), so orders.md links its requirement to it. Two settled hubs
    # are never regrouped by a later answer, so the merge of update 2 needs the
    # pair still undecided.
    data = shop_answer_data(1)
    data["consolidation"][0]["groups"] = [
        {"label": "Capability", "names": ["Audit logging", "Audit trail"], "confidence": "low"}
    ]
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
    gap = graph_project.writer("gap", ontology=ontology)
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
    (both,) = [
        e
        for e in await graph_project.edges("FLAGS", group_id=graph_project.project_id, edge_key="gap|both")
        if "Finding" in e.source.labels and "SpecNode" in e.target.labels
    ]
    await graph_project.create_edges(
        [
            EdgeSpec(
                "FLAGS",
                Match("Finding", {"uuid": both.source["uuid"]}),
                Match("SpecNode", {"uuid": logging_["uuid"]}),
                both.props,
            )
        ]
    )
    await gap.upsert_relationship(
        "FLAGS", finding, NodeRef("Module", orders["uuid"]), "gap|orders", {"severity": "high"}
    )
    assert len(await foreign_flags(graph_project)) == 6

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
    flags = await foreign_flags(graph_project)
    # each moved to the survivor with its direction and properties; the equal one is not duplicated
    # the one between the merged nodes is dropped
    assert flags == [
        ("gap|both", FINDING, logging_["uuid"], "high"),
        ("gap|orders", FINDING, orders["uuid"], "high"),
        ("gap|trail", FINDING, logging_["uuid"], "low"),
        ("gap|trail-out", logging_["uuid"], orders["uuid"], "mid"),
    ]
    assert not await graph_project.nodes("SpecNode", uuid=trail["uuid"])

    # update 3: the renamed module is carried over and keeps its foreign relationships, both directions
    third = await shop.build(3)
    assert third.nodes[ORDERS_SERVICE]["uuid"] == orders["uuid"]
    assert await foreign_flags(graph_project) == flags
    assert [{"title": f.get("title")} for f in await graph_project.nodes("Finding")] == [{"title": "Audit coverage"}]


async def foreign_flags(project: GraphProject) -> list[tuple[str, str, str, str]]:
    """Every ``FLAGS`` relationship of the project as ``(edge_key, source, target, severity)``, sorted
    (a list, so a duplicate shows); a spec node end is its uuid, the finding ``FINDING``."""

    rows = [
        {
            "key": e.get("edge_key"),
            "source": e.source.get("uuid") if "SpecNode" in e.source.labels else FINDING,
            "target": e.target.get("uuid") if "SpecNode" in e.target.labels else FINDING,
            "severity": e.get("severity"),
        }
        for e in await project.edges("FLAGS", group_id=project.project_id)
    ]
    return sorted((row["key"], row["source"], row["target"], row["severity"]) for row in rows)


# an interrupted persist resumes from its cursor


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


async def project_graph(project: GraphProject, project_id: str) -> tuple[list[str], list[str]]:
    def ref(n: ProbeNode) -> Any:
        return next(
            (n.get(name) for name in ("spec_id", "fragment_id", "document_id") if n.get(name) is not None), None
        )

    nodes = [{"labels": list(n.labels), "props": n.props} for n in await project.nodes(project_id=project_id)]
    edges = [
        {"source": ref(e.source), "type": e.type, "target": ref(e.target), "props": e.props}
        for e in await project.edges(project_id=project_id)
        if e.source.get("group_id") == project_id
    ]
    return canonical_graph(nodes, edges)


async def test_interrupted_update_persist_resumes_to_the_uninterrupted_graph(
    graph_project: GraphProject, scripted_gateway: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batches = writing.batches
    monkeypatch.setattr(writing, "batches", lambda operations, *_: batches(operations, PERSIST_TEST_BATCH))
    # uninterrupted: 1 -> 2 in one project
    plain = Shop(graph_project, scripted_gateway, tmp_path / "plain")
    await plain.build(1)
    expected = await plain.build(2)
    cursor = await expected.artifact("persist", PersistCursor)
    assert cursor.done and cursor.batches >= 4

    # interrupted: the same builds in another project, the update's persist crashing after batch 2
    crashed = Shop(graph_project, scripted_gateway, tmp_path / "crashed", project_id=graph_project.new_project())
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
            await run_shop_build(ctx)
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
        result = await run_shop_build(ctx.with_build_id(build_id))
    assert result.failed_step is None and result.settled
    assert resumed_applied == list(range(3, stopped.batches))
    assert len(gateway.calls) == calls
    assert result.spec_changes == expected.result.spec_changes
    await crashed.observe(2, shop_answers(2), result, gateway)
    resumed_graph = await project_graph(graph_project, crashed.project_id)
    assert len(resumed_graph[0]) == len(expected.nodes) + len(expected.fragments) + len(expected.documents)
    assert resumed_graph == await project_graph(graph_project, plain.project_id)


# a failed window keeps the fragment's last extracted version, a retry recovers it


async def test_failed_window_keeps_the_last_version_until_a_retry_recovers_it(
    graph_project: GraphProject, scripted_gateway: Any, tmp_path: Path
) -> None:
    shop = Shop(graph_project, scripted_gateway, tmp_path / "stores")
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
    show_error = "REQ::FR::FR_CHK_004_SYSTEM_SHOW_ERROR_KEEP"
    evidence = [
        {"fragment": e.target.get("fragment_id"), "start": e.get("line_start"), "end": e.get("line_end")}
        for e in await graph_project.edges("REFERENCED_BY")
        if e.source.get("spec_id") == show_error
        and e.source.get("group_id") == graph_project.project_id
        and "Fragment" in e.target.labels
    ]
    assert evidence == [{"fragment": failed.handles["chk.payment"], "start": 22, "end": 22}]
    assert [
        {"target": e.target.get("name"), "fact": e.get("fact")}
        for e in await graph_project.edges("PART_OF")
        if e.source.get("spec_id") == show_error
        and e.source.get("group_id") == graph_project.project_id
        and "SpecNode" in e.target.labels
    ] == [
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
    assert retried.keys(NODE_HUBS_PROMPT) == retried.keys(NODE_STATEMENTS_PROMPT) == {("chk.payment",)}
    assert (retried.fragment("chk.payment")["status"], retried.fragment("chk.payment")["code"]) == ("active", None)
    assert retried.handles["chk.payment"] not in retried.result.skipped_fragments
    node = retried.nodes["REQ::FR::FR_CHK_004_SYSTEM_SHOW_ERROR_KEEP"]
    assert (node["uuid"], node["name"]) == (
        old["uuid"],
        "System can keep the basket and name the reason when a payment is declined",
    )
    assert "REQ::FR::SYSTEM_OFFER_RETRY_ANOTHER_CARD" in retried.nodes


# follow-up builds inside run_build, the settlement and the building status. Shop
# snapshot 1 skips the "Export details" window (ord.details2, malformed output: not retryable, no
# follow-up). With the window failing retryably (``llm_exhausted``) the first build ends with orders.md
# to retry, so run_build chains update builds retrying it while they recover something, at most
# MAX_FOLLOW_UPS.

DETAILS_2 = ("ord.details2",)


def exhausted_answers(source: str) -> ShopAnswers:
    """Snapshot 1's answers with ord.details2's window failing retryably (retries exhausted)."""

    answers = ShopAnswers(shop_answer_data(1), source=source)
    answers.nodes[DETAILS_2] = {"window": list(DETAILS_2), "error": "exhausted"}
    return answers


def recovering_answers() -> ShopAnswers:
    """``exhausted_answers``; ord.details2's window turns valid (no entity) once a build retries a
    lineage, so the first follow-up recovers it."""

    return exhausted_answers("1.yaml (ord.details2 exhausted, recovers in a follow-up)")


def watch_follow_ups(
    patch: pytest.MonkeyPatch, shop: Shop, statuses: list[ProjectBuildStatus], recover: ShopAnswers | None = None
) -> None:
    """Before the ``plan`` step of each follow-up (a build retrying lineages): record the project's
    building status and, with ``recover``, make its ord.details2 window valid."""

    plan_step = runner.STEP_FUNCTIONS["plan"]

    async def plan_watched(ctx: BuildContext) -> None:
        if ctx.retry_lineages:
            statuses.append(await project_status(shop.stores.builds, shop.project_id))
            if recover is not None:
                recover.nodes[DETAILS_2] = {"window": list(DETAILS_2), "entities": []}
        await plan_step(ctx)

    patch.setitem(runner.STEP_FUNCTIONS, "plan", plan_watched)


async def chain_records(shop: Shop, last: BuildResult) -> list[Any]:
    """The build records of ``last``'s follow-up chain, its first build first."""

    record = await shop.stores.builds.get_build(shop.project_id, last.build_id)
    assert record is not None
    first_id = record.follow_up_of or record.build_id
    ids = (first_id, *(last.settlement.follow_up_builds if last.settlement else ()))
    return [await shop.stores.builds.get_build(shop.project_id, build_id) for build_id in ids]


async def test_a_skipped_fragment_is_recovered_by_the_first_follow_up_and_settles(
    graph_project: GraphProject, scripted_gateway: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = Shop(graph_project, scripted_gateway, tmp_path / "stores")
    answers = recovering_answers()
    statuses: list[ProjectBuildStatus] = []
    watch_follow_ups(monkeypatch, shop, statuses, recover=answers)
    built = await shop.build(1, answers=answers, follow_ups=MAX_FOLLOW_UPS)

    first, follow_up = await chain_records(shop, built.result)
    assert (first.mode, first.follow_up_of, first.follow_up_attempt) == ("first", None, 0)
    assert (follow_up.mode, follow_up.follow_up_of, follow_up.follow_up_attempt) == ("update", first.build_id, 1)
    stored = await shop.stores.builds.get_result(shop.project_id, first.build_id)
    assert stored is not None and stored.settlement is not None and stored.status == "partially_succeeded"
    assert (stored.settlement.state, stored.settlement.reasons, stored.settlement.documents) == (
        "not_settled",
        ("documents_incomplete",),
        ("orders.md",),
    )
    # the follow-up retried orders.md only, extracted the skipped window again and settled the chain
    result = built.result
    assert (result.build_id, result.mode, result.status, result.settled) == (
        follow_up.build_id,
        "update",
        "succeeded",
        True,
    )
    assert result.settlement is not None
    assert (result.settlement.state, result.settlement.reasons, result.settlement.follow_up_builds) == (
        "settled",
        (),
        (follow_up.build_id,),
    )
    plan = await built.artifact("plan", PlanArtifact)
    assert plan.change_set.retried == ("orders.md",)
    assert built.gateway.keys_for(NODE_HUBS_PROMPT).count(DETAILS_2) == 2  # the first build's and the follow-up's
    assert built.fragment("ord.details2")["status"] == "active" and result.skipped_fragments == {}
    # D8: while the follow-up ran, the project read building with its attempt and the first build's settlement
    (running,) = statuses
    assert (running.building, running.build_id, running.mode, running.step, running.follow_up_attempt) == (
        True,
        follow_up.build_id,
        "update",
        "plan",
        1,
    )
    assert running.settlement == stored.settlement
    done = await project_status(shop.stores.builds, shop.project_id)
    assert (done.building, done.build_id, done.settlement) == (False, None, result.settlement)


async def test_a_fragment_failing_the_same_way_stops_after_one_follow_up_not_settled(
    graph_project: GraphProject, scripted_gateway: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window fails retryably in every build: the first follow-up recovers nothing (same documents,
    same skipped fragment and code), so no second one starts; the lineage stays listed with its code."""

    shop = Shop(graph_project, scripted_gateway, tmp_path / "stores")
    statuses: list[ProjectBuildStatus] = []
    watch_follow_ups(monkeypatch, shop, statuses)
    answers = exhausted_answers("1.yaml (ord.details2 exhausted in every build)")
    built = await shop.build(1, answers=answers, follow_ups=MAX_FOLLOW_UPS)

    records = await chain_records(shop, built.result)
    assert [(record.mode, record.follow_up_attempt) for record in records] == [("first", 0), ("update", 1)]
    assert records[1].follow_up_of == records[0].build_id
    assert [status.follow_up_attempt for status in statuses] == [1]
    result = built.result
    assert (result.build_id, result.mode, result.status) == (records[-1].build_id, "update", "partially_succeeded")
    assert result.settlement is not None
    assert result.settlement.model_dump(
        include={"state", "reasons", "documents", "document_codes", "follow_up_builds"}
    ) == {
        "state": "not_settled",
        "reasons": ("documents_incomplete",),
        "documents": ("orders.md",),
        "document_codes": {"orders.md": ("llm_exhausted",)},
        "follow_up_builds": (records[1].build_id,),
    }
    # both builds asked for the window; it stays skipped
    assert built.gateway.keys_for(NODE_HUBS_PROMPT).count(DETAILS_2) == 2
    assert built.fragment("ord.details2")["status"] == "skipped"
    status = await project_status(shop.stores.builds, shop.project_id)
    assert (status.building, status.settlement) == (False, result.settlement)


async def test_a_fragment_failing_without_a_retryable_code_starts_no_follow_up(
    graph_project: GraphProject, scripted_gateway: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshot 1's ord.details2 answers malformed (after the repair request): retrying would fail
    the same way, so the build starts no follow-up and names orders.md with the code."""

    shop = Shop(graph_project, scripted_gateway, tmp_path / "stores")
    statuses: list[ProjectBuildStatus] = []
    watch_follow_ups(monkeypatch, shop, statuses)
    built = await shop.build(1, follow_ups=MAX_FOLLOW_UPS)

    assert statuses == [] and [record.follow_up_attempt for record in await chain_records(shop, built.result)] == [0]
    assert built.result.settlement is not None
    assert (built.result.settlement.documents, built.result.settlement.document_codes) == (
        ("orders.md",),
        {"orders.md": ("llm_malformed",)},
    )


# an update calls only the edge windows of the edit; every clean window reuses its edges

# A small edge window target, so the shop units have several windows each: admin.md 2, checkout.md 3
# orders.md 4, its "Export details" unit 3 and overview.md 4 (notes.md has no node, so no call).
SMALL_EDGE_WINDOWS = EdgeWindowParams(target_tokens=250)
CHECKOUT_WINDOWS = (
    ("chk.intro", "chk.basket", "chk.payment"),
    ("chk.confirm", "chk.quality", "chk.rules", "chk.stripe"),
)
EDITED_WINDOW = ("chk.retries", "chk.notes", "chk.notes2")
CHECKOUT_UNIT_1 = unit(*(handle for window in (*CHECKOUT_WINDOWS, EDITED_WINDOW) for handle in window))
"""The checkout unit of snapshot 1 (snapshot 2 adds "chk.delivery")."""


async def test_a_one_fragment_edit_calls_only_the_edge_window_that_holds_it(
    graph_project: GraphProject, scripted_gateway: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(edge_extraction, "DEFAULT_EDGE_WINDOWS", SMALL_EDGE_WINDOWS)
    shop = Shop(graph_project, scripted_gateway, tmp_path / "stores")
    first = await shop.build(1)
    edges = await first.artifact("extract_edges", EdgesArtifact)
    assert (edges.counters.windows_called, edges.counters.windows_reused, edges.counters.warm_ups) == (16, 0, 5)
    assert {call.cause for call in edges.called_windows or ()} == {"first"}
    assert first.calls()[EDGE_WINDOW_PROMPT] == 16
    # every window of a unit is shown the whole unit; each answers the facts of its own fragments
    checkout = [call for call in first.gateway.calls_for(EDGE_WINDOW_PROMPT) if call.key == CHECKOUT_UNIT_1]
    windows = sorted(tuple(episode.handle for episode in edited_window(shop_answers(1), call)) for call in checkout)
    assert windows == sorted([*CHECKOUT_WINDOWS, EDITED_WINDOW])

    # the second "Notes" section of checkout.md reworded below its first line (same handle, same nodes)
    corpus = tmp_path / "edited"
    shutil.copytree(SHOP_DIR / "1", corpus)
    path = corpus / "checkout.md"
    text = path.read_text(encoding="utf-8")
    assert text.count("owned by the content team") == 1
    path.write_text(text.replace("owned by the content team", "owned by the content design team"), encoding="utf-8")
    data = shop_answer_data(1)
    data["nodes"].append({"window": ["chk.notes2"], "entities": []})  # the update's window: the edit alone
    edited = await shop.build(1, answers=ShopAnswers(data, source="1.yaml (notes edited)"), corpus=corpus)
    # one edge call, for the window holding the edit; the other 15 windows (two of them in its unit) reuse
    assert edited.calls() == {NODE_HUBS_PROMPT: 1, NODE_STATEMENTS_PROMPT: 1, EDGE_WINDOW_PROMPT: 1}
    (call,) = edited.gateway.calls_for(EDGE_WINDOW_PROMPT)
    assert call.key == CHECKOUT_UNIT_1
    assert tuple(episode.handle for episode in edited_window(shop_answers(1), call)) == EDITED_WINDOW
    again = await edited.artifact("extract_edges", EdgesArtifact)
    assert [(call.cause, call.window) for call in again.called_windows or ()] == [("dirty", (7, 10))]
    assert (again.counters.windows_called, again.counters.windows_reused, again.counters.warm_ups) == (1, 15, 0)
    assert again.counters.windows_fallback == 0
    # no edge changed: the reused windows keep theirs, the called one answered the same facts
    assert edited.result.spec_changes.model_dump() == dict.fromkeys(edited.result.spec_changes.model_dump(), ())


def edited_window(answers: ShopAnswers, call: Any) -> list[Any]:
    """The fragment blocks (``Episode``) an edge call's window line names."""

    request = ScriptedRequest(call.call_class, call.model, call.prompt, call.messages, call.output_type)
    return answers.window(request, answers.episodes(request))


# frozen options


@pytest.mark.parametrize(
    "change",
    [
        {"fragment_tokens": {**OPTIONS.fragment_tokens.model_dump(), "target": OPTIONS.fragment_tokens.target + 31}},
        {"known_terms_limit": 60},
        {"locality": "strict"},
        {"catalog_limit": 10},
    ],
)
async def test_update_with_changed_options_raises_before_any_call(
    graph_project: GraphProject, scripted_gateway: Any, tmp_path: Path, change: dict[str, Any]
) -> None:
    shop = Shop(graph_project, scripted_gateway, tmp_path / "stores")
    first = await shop.build(1)
    before = await graph_project.snapshot()
    gateway = shop.gateway(shop_answers(2))
    options = BuildOptions.model_validate({**OPTIONS.model_dump(), **change})
    with pytest.raises(ConfigurationError) as raised:
        await run_build(shop.context(2, gateway, options=options))
    assert raised.value.code == "frozen_options_changed"
    assert gateway.calls == []
    assert (await shop.stores.builds.latest_build(shop.project_id)).build_id == first.result.build_id
    assert await graph_project.snapshot() == before


# Scale report F1: stored nodes on several fragments come back once each


def spanning_answers() -> ShopAnswers:
    """Shop snapshot 1 where one node-extraction call gives nodes on two of its fragments: hubs, an
    id-less requirement (span key) and a hub one call names as a Feature and two others as a
    Capability (the consolidation judge joins the two)."""

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
        # two more calls name "Payment flow" as a Capability (2 raw nodes to 1, one per fragment: 3 to 2)
        if window["window"][0] in ("chk.quality", "ord.intro") and "entities" in window:
            window["entities"].append(["Payment flow", "Capability", [window["window"][-1]]])
    # One name under two hub labels is two items until the judge groups them (it keeps Capability).
    (block,) = data["consolidation"]
    block["block"] = [*block["block"], "Payment flow"]
    block["groups"] = [*block["groups"], {"label": "Capability", "names": ["Payment flow"]}]
    return ShopAnswers(data, source="1.yaml (spanning)")


async def test_a_no_change_update_over_nodes_on_several_fragments_is_a_no_op(
    graph_project: GraphProject, scripted_gateway: Any, tmp_path: Path
) -> None:
    shop = Shop(graph_project, scripted_gateway, tmp_path)
    answers = spanning_answers()
    first = await shop.build(1, answers=answers)
    raw = await first.artifact("extract_nodes", NodesArtifact)
    assert any(len(node.fragment_ids) > 1 for node in raw.raw_nodes)
    assert first.named("Payment flow")["label"] == "Capability"
    graph = await project_graph(graph_project, shop.project_id)

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
    assert await project_graph(graph_project, shop.project_id) == graph
