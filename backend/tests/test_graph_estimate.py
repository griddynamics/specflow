"""Cost estimate offline (plan 5.T2; build.md "Cost estimate"; tech-specs 5.4 "Estimate"; FR-BLD-16).

``estimate_build`` on file stores, an ``OfflineExecutor`` and a ``ScriptedGateway`` without answers:
any model call would be recorded (and fail as unscripted), any write would fail the executor. The
shop corpus (``tests/fixtures/graph/shop``) and its hand-written options. The live half, against the
following scripted builds, is ``test_graph_estimate_live.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest

from graph.build import (
    BuildContext,
    FileCorpusSource,
    FileStores,
    estimate_build,
    open_file_stores,
    run_build,
)
from graph.build import estimate as estimation
from graph.build import run as runner
from graph.build.diff import CurrentEvidence, CurrentFragment, CurrentGraph, CurrentNode
from graph.build.extract import StoredResults, attribute_fingerprint
from graph.build.graphiti import (
    extract_edges_call,
    extract_nodes_call,
    graphiti_clients,
    render_edges_request,
    render_nodes_request,
)
from graph.build.models import BuildResult, ChangeSet, EstimateStep, FragmentRec, PlanArtifact
from graph.build.options import StepServices
from graph.build.ports import BuildRecord
from graph.build.prompts import edge_instructions, episode_text, node_instructions
from graph.build.queries import READ_PERSISTED_PRESENCE
from graph.core.errors import ConfigurationError
from graph.core.llm.gateway import request_tokens
from graph_support import (
    SHOP_DIR,
    OfflineExecutor,
    ScriptedGateway,
    shipped_ontology,
    shop_answers,
    tree_hash,
)

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, neo4j_project) live in graph_support.
pytest_plugins = ["graph_support"]

PROJECT = "pytest-graph-estimate"
PERSISTED = {READ_PERSISTED_PRESENCE.name: [{"documents": True, "spec_nodes": True}]}
OPTIONS = shop_answers(1).options
STEPS = get_args(EstimateStep)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "stores"


@pytest.fixture
def stores(root: Path) -> FileStores:
    return open_file_stores(root)


def context(
    stores: FileStores,
    root: Path,
    gateway: ScriptedGateway,
    *,
    folder: Path = SHOP_DIR / "1",
    executor: OfflineExecutor | None = None,
    **overrides: Any,
) -> BuildContext:
    values: dict[str, Any] = {
        "project_id": PROJECT,
        "ontology": shipped_ontology(),
        "driver": None,
        "gateway": gateway,
        "registry": stores.registry,
        "corpus": FileCorpusSource(folder, root),
        "extraction": stores.extraction,
        "builds": stores.builds,
        "options": OPTIONS,
        "executor": executor or OfflineExecutor(),
    }
    values.update(overrides)
    return BuildContext(**values)


async def finished_first(stores: FileStores, ctx: BuildContext) -> BuildRecord:
    """A finished ``first`` build record frozen with ``ctx``'s options, ontology and embedding model."""

    record = BuildRecord(
        build_id="first-0",
        project_id=PROJECT,
        mode="first",
        options=ctx.options,
        ontology_fingerprint=runner.ontology_fingerprint(ctx.ontology),
        label_set=ctx.ontology.labels,
        relation_set=runner.relation_set(ctx.ontology),
        embedding_model=ctx.gateway.config.embedding_model,
        started_at=T0,
    )
    await stores.builds.create_build(record)
    await stores.builds.finish_build(
        BuildResult(
            project_id=PROJECT,
            build_id=record.build_id,
            mode="first",
            status="succeeded",
            started_at=T0,
            finished_at=T0 + timedelta(seconds=30),
        )
    )
    return record


# FR-BLD-16: no call, no write, every step reported


async def test_three_new_documents_are_estimated_without_any_call_or_write(
    stores: FileStores, root: Path, tmp_path: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    folder = tmp_path / "corpus"
    folder.mkdir()
    for name in ("checkout.md", "orders.md", "overview.md"):
        (folder / name).write_text((SHOP_DIR / "1" / name).read_text(encoding="utf-8"), encoding="utf-8")
    gateway = scripted_gateway()
    executor = OfflineExecutor()
    before = tree_hash(tmp_path)

    estimate = await estimate_build(context(stores, root, gateway, folder=folder, executor=executor))

    assert gateway.calls == [] and gateway.usage().total().calls == 0
    assert tree_hash(tmp_path) == before  # no store, record, artifact or corpus-index write
    assert estimate.mode == "first"
    assert estimate.change_set.new == ("checkout.md", "orders.md", "overview.md")
    assert tuple(estimate.steps) == STEPS
    assert estimate.fragments_by_change == {"new": estimate.dirty_fragments}
    assert estimate.units_to_extract > 0 and estimate.units_reused == 0
    for step in ("hub_catalog", "node_extraction", "edge_extraction", "attributes", "embeddings"):
        assert estimate.steps[step].calls > 0 and estimate.steps[step].input_tokens > 0, step


async def test_a_first_build_of_the_shop_has_its_exact_hub_catalog_and_node_windows(
    stores: FileStores, root: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    gateway = scripted_gateway()
    estimate = await estimate_build(context(stores, root, gateway))
    assert gateway.calls == []
    # the reuse contract of the scripted first build (test_graph_update_live FIRST_CALLS)
    assert estimate.steps["hub_catalog"].calls == 1
    assert estimate.steps["node_extraction"].calls == 15
    assert estimate.steps["hub_consolidation"].calls == 1  # a new project has seen no hub
    assert estimate.steps["embeddings"].calls == 5  # fragments, hub names, hubs, names, search texts
    assert estimate.units_to_extract == 6


# Mode selection: the same as run_build


async def test_the_mode_is_the_one_run_build_selects(
    stores: FileStores, root: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    gateway = scripted_gateway()
    assert (await estimate_build(context(stores, root, gateway))).mode == "first"
    assert (await estimate_build(context(stores, root, gateway, rebuild=True))).mode == "rebuild"
    # a persisted graph without a full build record: both refuse with the same error, before any call
    persisted = context(stores, root, gateway, executor=OfflineExecutor(PERSISTED))
    for call in (estimate_build, run_build):
        with pytest.raises(ConfigurationError) as raised:
            await call(persisted)
        assert raised.value.code == "rebuild_required"
    await finished_first(stores, persisted)
    update = context(stores, root, gateway, executor=OfflineExecutor(PERSISTED))
    assert (await estimate_build(update)).mode == await runner.select_mode(update) == "update"
    assert gateway.calls == []


@pytest.mark.parametrize(
    ("overrides", "code", "fields"),
    [
        (
            {"options": OPTIONS.model_copy(update={"known_terms_limit": 7})},
            "frozen_options_changed",
            {"fields": "known_terms_limit"},
        ),
        (
            {"options": OPTIONS.model_copy(update={"locality": "strict"})},
            "frozen_options_changed",
            {"fields": "locality"},
        ),
    ],
)
async def test_a_frozen_option_change_refuses_the_estimate_like_the_build(
    stores: FileStores,
    root: Path,
    scripted_gateway: Callable[..., ScriptedGateway],
    overrides: dict[str, Any],
    code: str,
    *,
    fields: dict[str, str],
) -> None:
    gateway = scripted_gateway()
    await finished_first(stores, context(stores, root, gateway))
    changed = context(stores, root, gateway, executor=OfflineExecutor(PERSISTED), **overrides)
    for call in (estimate_build, run_build):
        with pytest.raises(ConfigurationError) as raised:
            await call(changed)
        assert (raised.value.code, raised.value.fields) == (code, fields)
    assert gateway.calls == []
    rebuild = context(stores, root, gateway, executor=OfflineExecutor(PERSISTED), rebuild=True, **overrides)
    assert (await estimate_build(rebuild)).mode == "rebuild"


async def test_a_context_bound_to_a_build_is_refused(
    stores: FileStores, root: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    gateway = scripted_gateway()
    ctx = context(stores, root, gateway).with_build_id("b-1")
    with pytest.raises(ConfigurationError) as raised:
        await estimate_build(ctx)
    assert (raised.value.code, raised.value.fields) == ("estimate_build_id_set", {"build_id": "b-1"})
    assert ctx.executor.reads == []


# Reads of an update: nodes, evidence and fragments, never edges


async def test_an_update_estimate_reads_no_edge_and_writes_nothing(
    stores: FileStores, root: Path, tmp_path: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    gateway = scripted_gateway()
    executor = OfflineExecutor(PERSISTED)
    ctx = context(stores, root, gateway, executor=executor)
    await finished_first(stores, ctx)
    before = tree_hash(tmp_path)

    estimate = await estimate_build(ctx)

    assert estimate.mode == "update" and gateway.calls == []
    names = {name for name, _ in executor.reads}
    assert not any(name.startswith("build_read_current_edges") for name in names), sorted(names)
    assert {"build_read_current_nodes", "build_read_current_links", "build_read_current_fragments"} <= names
    assert tree_hash(tmp_path) == before


# Placeholders (expected nodes of fragments without a persisted version)


async def test_placeholder_hubs_are_known_terms_and_catalog_entries_only_in_a_first_build(
    stores: FileStores, root: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    gateway = scripted_gateway()
    ctx = context(stores, root, gateway)
    plan = await runner._plan_artifact(ctx, "first", OPTIONS, dry_run=True)
    lookups = estimation.Lookups(vectors=runner.vector_cache(ctx), decisions=runner.decision_memory(ctx))

    async def steps(mode: Any) -> dict[str, Any]:
        return await estimation.estimate_steps(
            plan,
            mode=mode,
            services=StepServices(gateway=gateway, ontology=ctx.ontology, options=OPTIONS),
            revisions=runner.active_revisions(plan) if mode != "update" else [],
            hubs=[],
            persisted=estimation.PersistedNodes(),
            lookups=lookups,
        )

    first, update, rebuild = await steps("first"), await steps("update"), await steps("rebuild")
    # without persisted hubs an update or rebuild shows no known term and no hub from elsewhere
    assert first["node_extraction"].calls == update["node_extraction"].calls == rebuild["node_extraction"].calls
    assert first["node_extraction"].input_tokens > update["node_extraction"].input_tokens
    assert update["node_extraction"].input_tokens == rebuild["node_extraction"].input_tokens
    assert first["edge_extraction"].input_tokens > update["edge_extraction"].input_tokens
    assert update["edge_extraction"] == rebuild["edge_extraction"]
    assert gateway.calls == []


async def test_persisted_nodes_stand_in_for_a_fragment_and_its_renamed_or_replaced_version() -> None:

    def fragment(fid: str, content: str, previous: str | None = None) -> FragmentRec:
        return FragmentRec(
            fragment_id=fid,
            document_id="d.md",
            heading_path=("D",),
            ordinal=0,
            line_start=1,
            line_end=1,
            text="x",
            content_hash=content,
            tokens=1,
            change="changed" if previous else "unchanged",
            previous_id=previous,
        )

    current = CurrentGraph(
        nodes=(
            # span-keyed: Conform keyed it by its defining sentence, which is not its name
            CurrentNode(
                uuid="u1",
                spec_id="S1",
                label="FunctionalRequirement",
                identity_key="req|span|the customer can pay for the basket by card",
                name="Customer can pay",
                properties={"stmt": "Pays."},
                evidence=(CurrentEvidence("old", "d.md", 1, 1, ("Customer can pay.",)),),
            ),
            CurrentNode(
                uuid="u2",
                spec_id="S2",
                label="FunctionalRequirement",
                identity_key="req|id|FR-CHK-001",
                name="FR-CHK-001: Customer can see the basket",
                evidence=(CurrentEvidence("old", "d.md", 1, 1, ()),),
            ),
        ),
        fragments=(
            CurrentFragment("old", "d.md", content_hash="h1"),
            CurrentFragment("empty", "d.md", content_hash="h2"),
        ),
    )
    found = estimation.persisted_nodes(
        [
            fragment("old", "h1"),
            fragment("new", "h9", previous="old"),
            fragment("renamed", "h1"),
            fragment("e2", "h2"),
            fragment("x", "h7"),
        ],
        current,
    )
    assert set(found.by_fragment) == {"old", "new", "renamed", "e2"}  # "x": no persisted version
    assert [node.name for node in found.by_fragment["new"]] == [
        "Customer can pay",
        "FR-CHK-001: Customer can see the basket",
    ]
    assert found.by_fragment["e2"] == []
    span, explicit = found.by_fragment["old"]
    # the span-keyed requirement is keyed as Resolve keys it before Conform: its provisional key from
    # the normalised name, not its persisted sentence key; any other key stays as persisted
    assert span.key == "req|span|the customer can pay for the basket by card"
    assert found.attribute_inputs[span] == ("req|span|customer can pay", "FunctionalRequirement", ("h1", "h9", "h1"))
    assert found.attribute_inputs[explicit] == ("req|id|FR-CHK-001", "FunctionalRequirement", ("h1", "h9", "h1"))
    assert span.persisted and span.stmt == "Pays." and span.evidence == ("Customer can pay.",)


# Graphiti request rendering: the very messages of the call, and nothing left behind


async def test_rendered_requests_are_the_messages_a_real_call_sends(
    scripted_gateway: Callable[..., ScriptedGateway],
) -> None:
    ontology = shipped_ontology()
    gateway = scripted_gateway()
    gateway.answer("extract_nodes.extract_text", None, {"extracted_entities": []})
    gateway.answer("extract_edges.edge", None, {"edges": []})
    episodes = [
        episode_text(gateway, "Shop", ("Shop", "Checkout"), "The customer pays by card."),
        episode_text(gateway, "Shop", ("Shop", "Orders"), "Orders go to the ERP."),
    ]
    nodes = node_instructions(ontology, gateway, known_terms=["Checkout service"], window=(1, 2), episodes=2)
    edges = edge_instructions(ontology, window=(0, 2), episodes=2)
    catalog = [("Checkout service", "Module"), ("ERP", "Module")]

    rendered_nodes = await render_nodes_request(episodes, ontology.entity_models(), nodes)
    rendered_edges = await render_edges_request(
        episodes, catalog, ontology.edge_models(), ontology.edge_type_map(), edges
    )
    assert gateway.calls == []  # rendering never reaches the gateway

    clients = graphiti_clients(gateway)
    await extract_nodes_call(clients, episodes, ontology.entity_models(), nodes)
    await extract_edges_call(
        clients, episodes, catalog, ontology.edge_models(), ontology.edge_type_map(), instructions=edges
    )
    assert [call.prompt for call in gateway.calls] == ["extract_nodes.extract_text", "extract_edges.edge"]
    assert [list(call.messages) for call in gateway.calls] == [rendered_nodes, rendered_edges]
    assert request_tokens(rendered_nodes) > 0


async def test_a_persisted_hub_is_keyed_and_asked_by_the_fragments_naming_it() -> None:
    """The estimate reads a hub as the attributes pass does (``extract.attribute_scope``): its reuse
    key and its shown fragments are those naming it, by name or by a persisted ``surface`` alias."""

    texts = {"a": "The Orders DB holds orders.", "b": "Rows are kept a year.", "c": "The Sales Ledger is copied."}
    fragments = [
        FragmentRec(
            fragment_id=fid,
            document_id="d.md",
            heading_path=("D",),
            ordinal=index,
            line_start=index + 1,
            line_end=index + 1,
            text=text,
            content_hash=f"h-{fid}",
            tokens=5,
            change="unchanged",
        )
        for index, (fid, text) in enumerate(texts.items())
    ]
    hub = CurrentNode(
        uuid="u1",
        spec_id="M1",
        label="Module",
        identity_key="Module|surface|order db",
        name="Orders DB",
        identity_aliases=("Module|surface|sale ledger",),
        evidence=tuple(CurrentEvidence(fid, "d.md", 1, 1, ()) for fid in texts),
    )
    current = CurrentGraph(
        nodes=(hub,), fragments=tuple(CurrentFragment(fid, "d.md", content_hash=f"h-{fid}") for fid in texts)
    )
    (node,) = estimation.persisted_nodes(fragments, current, ("Module",)).attribute_inputs.items()
    assert node[1] == ("Module|surface|order db", "Module", ("h-a", "h-c"))
    # it is asked over the same fragments when one of them is dirty, and not for an edit of "b"
    persisted = estimation.persisted_nodes(fragments, current, ("Module",))
    expected = estimation._Expected(shipped_ontology(), persisted.by_fragment)
    plan = PlanArtifact(mode="update", change_set=ChangeSet(), documents=(), fragments=tuple(fragments))
    (target,) = estimation._attribute_targets(plan, {"c"}, expected, set(), ("Module",))
    assert target.fragment_ids == ("a", "c")
    assert estimation._attribute_targets(plan, {"b"}, expected, set(), ("Module",)) == []
    # without hub labels (as before the rule) every fragment keys it
    (plain,) = estimation.persisted_nodes(fragments, current).attribute_inputs.values()
    assert plain[2] == ("h-a", "h-b", "h-c")


async def test_persisted_nodes_with_stored_attributes_are_not_asked_again(
    stores: FileStores, root: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:

    gateway = scripted_gateway()
    ctx = context(stores, root, gateway)
    plan = await runner._plan_artifact(ctx, "first", OPTIONS, dry_run=True)
    fragment = plan.fragments[0]
    node = estimation.Expected("FunctionalRequirement", "Customer can pay by card", persisted=True, key="k")
    persisted = estimation.PersistedNodes(
        by_fragment={fragment.fragment_id: [node]},
        attribute_inputs={node: ("req|span|customer can pay by card", node.label, (fragment.content_hash,))},
    )
    results = StoredResults(stores.extraction, PROJECT, read=True)
    fingerprint = attribute_fingerprint(ctx.ontology, gateway)

    async def attributes() -> Any:
        steps = await estimation.estimate_steps(
            plan,
            mode="update",
            services=StepServices(gateway=gateway, ontology=ctx.ontology, options=OPTIONS),
            revisions=[],
            hubs=[],
            persisted=persisted,
            lookups=estimation.Lookups(
                vectors=runner.vector_cache(ctx),
                decisions=runner.decision_memory(ctx),
                results=results,
                attribute_fingerprint=fingerprint,
            ),
        )
        return steps["attributes"]

    asked = await attributes()
    key = results.attribute_key(*persisted.attribute_inputs[node])
    await stores.extraction.put("attributes", key, fingerprint, {"values": {}})
    reused = await attributes()
    assert reused.calls == asked.calls and reused.input_tokens < asked.input_tokens
    assert gateway.calls == []
