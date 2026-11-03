"""Cost estimate offline."""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any, Final, cast, get_args

import pytest

from graph.build import (
    BuildContext,
    BuildSettings,
    FileCorpusSource,
    FileStores,
    estimate_build,
    infer_bound,
    open_file_stores,
    run_build,
)
from graph.build import answered as answering
from graph.build import edges as edge_extraction
from graph.build import estimate as estimation
from graph.build import run as runner
from graph.build.answers import HUBS_LIST, STATEMENTS_LIST
from graph.build.calls import extract_edge_window_call, extract_nodes_call, warm_up_nodes
from graph.build.changes import DEFAULT_EDGE_WINDOWS, EdgeWindowParams, edge_windows
from graph.build.consolidate import HUBS_PROMPT, blocks
from graph.build.diff import CurrentEvidence, CurrentFragment, CurrentGraph, CurrentNode
from graph.build.edges import unique_names
from graph.build.extract import CallEnv, StoredResults, attribute_fingerprint
from graph.build.hubforms import hub_surface
from graph.build.hubjudge import (
    EVIDENCE_LINES,
    PAIR_BATCH_ITEMS,
    REQUIREMENTS_SHOWN,
    Clusters,
    ConsolidationParams,
    GroupFacts,
    HubItem,
    HubJoiner,
    HubJudge,
    JudgeItem,
    consolidate_hubs,
    hub_block_messages,
)
from graph.build.infer import ENDPOINTS, REALIZATION, VERDICT_TYPES, _call, _judgement, _Node, _Run, _Subject
from graph.build.models import (
    BacklogQueue,
    BuildResult,
    CanonicalNode,
    ChangeSet,
    DocumentRec,
    EstimateStep,
    FragmentRec,
    HubName,
    InferenceReport,
    PlanArtifact,
    RuleReport,
    StepEstimate,
    Unit,
)
from graph.build.normalise import ConformJudge
from graph.build.options import StepServices
from graph.build.ports import BuildRecord
from graph.build.prompts import (
    EDGE_WINDOW_PROMPT,
    EDGE_WINDOW_WARM_UP_PROMPT,
    NODE_HUBS_PROMPT,
    NODE_STATEMENTS_PROMPT,
    NODE_WARM_UP_PROMPT,
    KnownTerm,
    edge_window_messages,
    edge_window_warm_up_messages,
    fragment_text,
    node_messages,
    node_warm_up_messages,
)
from graph.build.queries import READ_PERSISTED_PRESENCE
from graph.build.references import REFERENCED_CAP, requirement_labels
from graph.build.vectors import VectorCache
from graph.core.errors import ConfigurationError
from graph.core.llm import JudgeRunner
from graph.core.llm.gateway import request_tokens
from graph.core.llm.judge import FRAGMENTS_KEY
from graph.core.ontology import InferenceRule, Ontology
from graph.core.ontology.model import CROSS_DOCUMENT
from graph_extraction_store_support import MemoryStore
from graph_support import (
    SHOP_DIR,
    OfflineExecutor,
    ScriptedGateway,
    frozen_ontology,
    shipped_ontology,
    shop_answers,
    tree_hash,
)

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, graph_project) live in graph_support.
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
        "ontology": frozen_ontology(),  # mechanics tests: the frozen v1.3 ontology
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


# no call, no write, every step reported


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
    for step in ("node_extraction", "edge_extraction", "attributes", "embeddings"):
        assert estimate.steps[step].calls > 0 and estimate.steps[step].input_tokens > 0, step
    assert estimate.steps["hub_catalog"] == StepEstimate()  # the hub pass is the catalog


async def test_a_first_build_of_the_shop_has_its_exact_node_windows_in_both_passes(
    stores: FileStores, root: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    gateway = scripted_gateway()
    estimate = await estimate_build(context(stores, root, gateway))
    assert gateway.calls == []
    # the reuse contract of the scripted first build (test_graph_update_live FIRST_CALLS)
    assert estimate.steps["hub_catalog"].calls == 0
    # the hub pass and the statement pass, each with a warm-up per unit of two or more windows (five)
    assert estimate.steps["node_extraction"].calls == 2 * 15 + 2 * 5
    assert estimate.steps["hub_consolidation"].calls == 1  # a new project has seen no hub
    # fragments, hub names, hubs, hub candidate texts, names, dedup candidate texts, search texts
    assert estimate.steps["embeddings"].calls == 7
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
        estimated = await estimation.estimate_steps(
            plan,
            mode=mode,
            services=StepServices(gateway=gateway, ontology=ctx.ontology, options=OPTIONS),
            hubs=[],
            persisted=estimation.PersistedNodes(),
            lookups=lookups,
        )
        return dict(estimated.steps)

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


# Request rendering: the estimate counts the very messages of the call


async def test_rendered_requests_are_the_messages_a_real_call_sends(
    scripted_gateway: Callable[..., ScriptedGateway],
) -> None:
    ontology = frozen_ontology()
    gateway = scripted_gateway()
    gateway.answer(NODE_HUBS_PROMPT, None, {"hubs": [], "statements": []})
    gateway.answer(NODE_STATEMENTS_PROMPT, None, {"hubs": [], "statements": []})
    gateway.answer(EDGE_WINDOW_PROMPT, None, {"edges": []})
    texts = [
        fragment_text("Shop", ("Shop", "Checkout"), "The customer pays by card."),
        fragment_text("Shop", ("Shop", "Orders"), "Orders go to the ERP."),
    ]
    catalog = [("Checkout service", "Module"), ("ERP", "Module")]
    terms = [KnownTerm("Checkout service", "Module", ("checkout",), mentioned=True)]
    rendered_hubs = node_messages(ontology, texts, [], (1, 2), node_list=HUBS_LIST)
    rendered_nodes = node_messages(ontology, texts, terms, (1, 2), node_list=STATEMENTS_LIST)
    rendered_edges = edge_window_messages(ontology, texts, catalog, (0, 2))

    rendered_warm_up = node_warm_up_messages(ontology, texts)

    await extract_nodes_call(gateway, ontology, texts, [], (1, 2), node_list=HUBS_LIST)
    await extract_nodes_call(gateway, ontology, texts, terms, (1, 2), node_list=STATEMENTS_LIST)
    await extract_edge_window_call(gateway, ontology, texts, catalog, (0, 2))
    await warm_up_nodes(gateway, ontology, texts)
    assert [call.prompt for call in gateway.calls] == [NODE_HUBS_PROMPT, NODE_STATEMENTS_PROMPT, EDGE_WINDOW_PROMPT]
    assert [list(call.messages) for call in gateway.calls] == [rendered_hubs, rendered_nodes, rendered_edges]
    assert [(call.prompt, list(call.messages)) for call in gateway.warm_ups] == [
        (NODE_WARM_UP_PROMPT, rendered_warm_up)
    ]
    assert request_tokens(rendered_nodes) > request_tokens(rendered_warm_up) > 0

    # Edge windows: a unit's run (``edges._EdgeUnitRun``: the warm-up, then one call per
    # window, its catalog under ``edges.catalog_names``) sends the requests the estimate renders over
    # the same entries under ``edges.unique_names`` (``estimate.edge_requests``).
    entries = [("Checkout service", "Module"), ("ERP", "Module"), ("ERP", "Feature")]
    nodes = [
        CanonicalNode(cid=f"{index:06d}", identity_key=f"k{index}", name=name, label=label, member_ids=())
        for index, (name, label) in enumerate(entries)
    ]
    names = unique_names([(f"{index:06d}", name, label) for index, (name, label) in enumerate(entries)])
    catalog = [(names[f"{index:06d}"], label) for index, (_, label) in enumerate(entries)]
    assert [name for name, _ in catalog] == ["Checkout service", "ERP (Module)", "ERP (Feature)"]
    windows = [(0, 1), (1, 2)]
    edge_gateway = scripted_gateway().answer(EDGE_WINDOW_PROMPT, None, {"edges": []})
    unit = Unit(unit_id="u", document_id="Shop", fragment_ids=("f1", "f2"), tokens=10, content_hash="h")
    run = edge_extraction._EdgeUnitRun(
        unit,
        ordinal=0,
        texts=texts,
        catalog=nodes,
        hints={},
        env=CallEnv(set(unit.fragment_ids), {}, edge_gateway, ontology),
        tally=edge_extraction._EdgeTally(),
    )
    await run.run_all(windows)
    await run.warm_up.stop()
    rendered = estimation.edge_requests(ontology, texts, catalog, windows)
    assert [call.prompt for call in edge_gateway.warm_ups] == [EDGE_WINDOW_WARM_UP_PROMPT]
    assert [call.prompt for call in edge_gateway.calls] == [EDGE_WINDOW_PROMPT] * 2
    sent = [list(call.messages) for call in [*edge_gateway.warm_ups, *edge_gateway.calls]]
    assert sent == rendered
    assert rendered[0] == edge_window_warm_up_messages(ontology, texts, catalog)
    # one window called: no warm-up
    assert estimation.edge_requests(ontology, texts, catalog, [(0, 2)]) == [
        edge_window_messages(ontology, texts, catalog, (0, 2))
    ]


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
    expected = estimation._Expected(frozen_ontology(), persisted.by_fragment)
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
            hubs=[],
            persisted=persisted,
            lookups=estimation.Lookups(
                vectors=runner.vector_cache(ctx),
                decisions=runner.decision_memory(ctx),
                results=results,
                attribute_fingerprint=fingerprint,
            ),
        )
        return steps.steps["attributes"]

    asked = await attributes()
    key = results.attribute_key(*persisted.attribute_inputs[node])
    await stores.extraction.put("attributes", key, fingerprint, {"values": {}})
    reused = await attributes()
    assert reused.calls == asked.calls and reused.input_tokens < asked.input_tokens
    assert gateway.calls == []


# Conform judge


async def test_the_conform_judge_is_estimated_from_the_new_requirements_and_capped(
    stores: FileStores, root: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    gateway = scripted_gateway()
    frozen = await estimate_build(context(stores, root, gateway))
    assert frozen.steps["conform_judge"] == StepEstimate()  # no conform section: no judge

    ontology = shipped_ontology()
    ctx = context(stores, root, gateway, ontology=ontology)
    plan = await runner._plan_artifact(ctx, "first", OPTIONS, dry_run=True)
    lookups = estimation.Lookups(vectors=runner.vector_cache(ctx), decisions=runner.decision_memory(ctx))

    async def judged(target: Ontology) -> StepEstimate:
        steps = await estimation.estimate_steps(
            plan,
            mode="first",
            services=StepServices(gateway=gateway, ontology=target, options=OPTIONS),
            hubs=[],
            persisted=estimation.PersistedNodes(),
            lookups=lookups,
        )
        assert tuple(steps.steps) == STEPS
        return steps.steps["conform_judge"]

    step = await judged(ontology)
    expected = estimation._Expected(ontology, {})
    requirements = [
        node
        for fragment in plan.fragments
        for node in dict.fromkeys(expected.of(fragment))
        if node.label in ontology.spine.sources
    ]
    calls = round(estimation.CONFORM_JUDGE_GROUPS_PER_REQUIREMENT * len(set(requirements)))
    assert step.calls == calls > 0
    dirty = [fragment for fragment in plan.fragments if fragment.fragment_id in set(plan.dirty_ids)]
    label = max(
        sorted({node.label for node in requirements}),
        key=lambda name: sum(1 for node in set(requirements) if node.label == name),
    )
    assert step.input_tokens == calls * request_tokens(estimation.conform_judge_messages(ontology, label, dirty))
    spec = ontology.conform_spec()
    assert spec is not None
    capped = dataclasses.replace(ontology, conform=dataclasses.replace(spec, cap=1))
    assert (await judged(capped)).calls == 1
    assert gateway.calls == []


async def test_the_priced_conform_judge_request_is_the_one_the_judge_sends(
    scripted_gateway: Callable[..., ScriptedGateway],
) -> None:
    ontology = shipped_ontology()
    spec = ontology.conform_spec()
    assert spec is not None
    fragments = [
        FragmentRec(
            fragment_id="doc#a",
            document_id="doc",
            heading_path=("Doc",),
            ordinal=0,
            line_start=1,
            line_end=1,
            text="The Checkout service exposes the Checkout API.",
            content_hash="h",
            tokens=10,
        )
    ]
    label = "FunctionalRequirement"
    group = estimation.conform_judge_group(ontology, label, fragments)
    assert group is not None
    assert [loser["id"] for loser in group.payload["losers"]] == ["L1"] and "owner" in group.payload
    gateway = scripted_gateway().handle(spec.judge_prompt, lambda _: {"confidence": 0.9, "evidence": [], "answers": []})
    await ConformJudge(gateway, ontology).decide([group])
    (call,) = gateway.calls
    rendered = estimation.conform_judge_messages(ontology, label, fragments)
    assert list(call.messages) == rendered
    assert request_tokens(rendered) > 0


# Edge windows, references and the answered check


def window_plan(dirty: tuple[str, ...] | None = None) -> PlanArtifact:
    """A billing unit of four large fragments (two or more edge windows) whose first fragment is
    section 2.1, and a one-fragment shop unit that references that section."""

    def fragment(fid: str, document: str, heading: tuple[str, ...], text: str, tokens: int) -> FragmentRec:
        return FragmentRec(
            fragment_id=fid,
            document_id=document,
            heading_path=heading,
            ordinal=int(fid[-1]),
            line_start=1,
            line_end=1,
            text=text,
            content_hash=f"h-{fid}",
            tokens=tokens,
        )

    billing = [
        fragment(
            f"a{index}", "a.md", ("Billing specification", f"2.{index} Part {index}"), f"Billing rule {index}.", 3000
        )
        for index in range(1, 5)
    ]
    shop = [fragment("b1", "b.md", ("Shop",), "Refunds follow section 2.1 of the Billing specification.", 50)]
    units = (
        Unit(unit_id="ua", document_id="a.md", fragment_ids=("a1", "a2", "a3", "a4"), tokens=12000, content_hash="ua"),
        Unit(unit_id="ub", document_id="b.md", fragment_ids=("b1",), tokens=50, content_hash="ub"),
    )
    documents = (
        DocumentRec(document_id="a.md", revision_id="r", version=1, path="a.md", title="Billing specification"),
        DocumentRec(document_id="b.md", revision_id="r", version=1, path="b.md", title="Shop"),
    )
    fragments = (*billing, *shop)
    return PlanArtifact(
        mode="first",
        change_set=ChangeSet(),
        documents=documents,
        fragments=fragments,
        dirty_ids=dirty if dirty is not None else tuple(item.fragment_id for item in fragments),
        units=units,
    )


async def test_edge_windows_warm_ups_and_references_are_estimated_as_the_build_cuts_them(
    stores: FileStores, root: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    gateway = scripted_gateway()
    ontology = shipped_ontology()
    ctx = context(stores, root, gateway, ontology=ontology)
    lookups = estimation.Lookups(vectors=runner.vector_cache(ctx), decisions=runner.decision_memory(ctx))
    plan = window_plan()
    by_id = {fragment.fragment_id: fragment for fragment in plan.fragments}
    windows = edge_windows(plan.units[0], by_id, DEFAULT_EDGE_WINDOWS)
    assert len(windows) >= 2

    async def edges(target: PlanArtifact, mode: Any, params: EdgeWindowParams = DEFAULT_EDGE_WINDOWS) -> StepEstimate:
        steps = await estimation.estimate_steps(
            target,
            mode=mode,
            services=StepServices(gateway=gateway, ontology=ontology, options=OPTIONS),
            hubs=[],
            persisted=estimation.PersistedNodes(),
            lookups=lookups,
            edge_params=params,
        )
        return steps.steps["edge_extraction"]

    # first: every window of the billing unit plus its one warm-up, the shop unit's single window
    first = await edges(plan, "first")
    assert first.calls == len(windows) + 1 + 1
    # update: only the window holding the dirty fragment, so no warm-up; the shop unit is clean
    dirty = plan.model_copy(update={"dirty_ids": ("a2",)})
    update = await edges(dirty, "update")
    assert update.calls == 1 and 0 < update.input_tokens < first.input_tokens
    # one window per unit (target None): one call each, no warm-up
    assert (await edges(plan, "first", EdgeWindowParams(target_tokens=None))).calls == 2

    # the shop unit references section 2.1: the expected requirements of fragment a1 join its catalog
    expected = estimation._Expected(ontology, {})
    shop = plan.units[1]
    referenced = estimation._referenced(plan, [shop], expected, ontology, DEFAULT_EDGE_WINDOWS)
    requirements = requirement_labels(ontology)
    wanted = [(node.name, node.label) for node in expected.of(by_id["a1"]) if node.label in requirements]
    assert {name: [entry[:2] for entry in found] for name, found in referenced.items()} == {
        "ub": wanted[:REFERENCED_CAP]
    } and wanted
    # each with the referring sentence the catalog line shows (``referenced_in``)
    assert {entry[2] for entry in referenced["ub"]} == {by_id["b1"].text.strip()}
    assert estimation._referenced(plan, [shop], expected, ontology, EdgeWindowParams(references=False)) == {}
    shop_only = plan.model_copy(update={"dirty_ids": ("b1",)})
    with_references = await edges(shop_only, "first")
    without = await edges(shop_only, "first", EdgeWindowParams(references=False))
    assert with_references.calls == without.calls == 1
    assert with_references.input_tokens > without.input_tokens
    # in an update without persisted hubs the shop unit's one expected node has a pair only through them
    assert (await edges(shop_only, "update")).calls == 1
    assert (await edges(shop_only, "update", EdgeWindowParams(references=False))).calls == 0
    assert gateway.calls == [] and gateway.warm_ups == []


async def test_the_answered_check_is_estimated_from_the_new_questions_and_capped(
    stores: FileStores, root: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    gateway = scripted_gateway()
    frozen = await estimate_build(context(stores, root, gateway))
    assert frozen.steps["answered_check"] == StepEstimate()  # no answered_elsewhere flag: no call

    ontology = shipped_ontology()
    spec = ontology.conform_spec()
    assert spec is not None and spec.answered_elsewhere is not None
    flag = spec.answered_elsewhere
    ctx = context(stores, root, gateway, ontology=ontology)
    plan = await runner._plan_artifact(ctx, "first", OPTIONS, dry_run=True)
    lookups = estimation.Lookups(vectors=runner.vector_cache(ctx), decisions=runner.decision_memory(ctx))

    async def checked(target: Ontology) -> StepEstimate:
        steps = await estimation.estimate_steps(
            plan,
            mode="first",
            services=StepServices(gateway=gateway, ontology=target, options=OPTIONS),
            hubs=[],
            persisted=estimation.PersistedNodes(),
            lookups=lookups,
        )
        return steps.steps["answered_check"]

    expected = estimation._Expected(ontology, {})
    questions = {node for fragment in plan.fragments for node in expected.of(fragment) if node.label == flag.label}
    calls = min(flag.cap, round(estimation.ANSWERED_CALLS_PER_QUESTION * len(questions)))
    dirty = [fragment for fragment in plan.fragments if fragment.fragment_id in set(plan.dirty_ids)]
    step = await checked(ontology)
    assert step.calls == calls > 0
    assert step.input_tokens == calls * request_tokens(estimation.answered_check_messages(ontology, dirty))
    capped = dataclasses.replace(
        ontology, conform=dataclasses.replace(spec, answered_elsewhere=dataclasses.replace(flag, cap=1))
    )
    assert (await checked(capped)).calls == 1
    off = dataclasses.replace(ontology, conform=dataclasses.replace(spec, answered_elsewhere=None))
    assert await checked(off) == StepEstimate()
    assert gateway.calls == []


async def test_the_priced_answered_check_request_is_the_one_the_check_sends(
    scripted_gateway: Callable[..., ScriptedGateway],
) -> None:
    ontology = shipped_ontology()
    spec = ontology.conform_spec()
    assert spec is not None and spec.answered_elsewhere is not None
    flag = spec.answered_elsewhere
    fragments = window_plan().fragments
    request = estimation.answered_check_request(ontology, fragments)
    assert request is not None
    question, passages = request
    assert len(passages) == flag.candidates
    gateway = scripted_gateway().handle(flag.prompt, lambda _: {"answer": "open", "confidence": 0.5})
    await answering._ask_all([question], {question.key: passages}, flag, gateway, ontology)
    (call,) = gateway.calls
    rendered = estimation.answered_check_messages(ontology, fragments)
    assert list(call.messages) == rendered
    assert request_tokens(rendered) > 0
    assert estimation.answered_check_messages(frozen_ontology(), fragments) == []


# Hub consolidation: the priced block and pair batch are what the hub judge sends


async def judge_hub_items(
    gateway: ScriptedGateway, shown: list[JudgeItem], params: ConsolidationParams, catalog: tuple[HubName, ...] = ()
) -> None:
    """Send ``shown`` through the hub pass as items of one label part (no decision memory)."""

    ontology = frozen_ontology()
    items = [
        HubItem(
            root=index,
            key=f"{item.label}|surface|{hub_surface(item.name)}",
            label=item.label,
            name=item.name,
            surfaces=frozenset({hub_surface(item.name)}),
            shown=item,
            hash=f"h{index}",
        )
        for index, item in enumerate(shown)
    ]
    clusters = Clusters([GroupFacts(frozenset(), None, item.label, (item.label,)) for item in shown], 12)
    joiner = HubJoiner(clusters, {}, items, hubs=ontology.hub_labels, apart=frozenset())
    store = MemoryStore()
    judge = HubJudge(gateway, ontology, VectorCache(gateway, store, PROJECT), None, params, catalog=catalog)
    await consolidate_hubs(items, joiner, judge)


def sent_items(call_messages: Any, by_name: dict[str, JudgeItem]) -> list[JudgeItem]:
    body = call_messages[-1]["content"].split(">>>\n", 1)[1].rsplit("\n", 1)[0]
    return [by_name[json.loads(line)["name"]] for line in body.split("\n")]


async def test_the_priced_hub_block_is_the_block_the_hub_judge_sends(
    scripted_gateway: Callable[..., ScriptedGateway],
) -> None:
    ontology = frozen_ontology()
    shown = [
        estimation.hub_judge_item(estimation.Expected("Module", "Orders service", quote="It keeps orders.")),
        estimation.hub_judge_item(estimation.Expected("Feature", "Order export")),
    ]
    assert all(len(item.requirements) == REQUIREMENTS_SHOWN for item in shown)
    assert all(len(item.evidence) == EVIDENCE_LINES and item.heading for item in shown)
    assert shown[0].quote == "It keeps orders." and shown[1].quote
    gateway = scripted_gateway().handle(HUBS_PROMPT, lambda _: {"groups": []})
    await judge_hub_items(gateway, shown, ConsolidationParams(pair_pass=False))
    (call,) = gateway.calls_for(HUBS_PROMPT)
    rendered = hub_block_messages(ontology, gateway, sent_items(call.messages, {item.name: item for item in shown}))
    assert list(call.messages) == rendered
    assert request_tokens(rendered) == request_tokens(hub_block_messages(ontology, gateway, shown))


async def test_the_priced_pair_batch_is_the_batch_the_hub_judge_sends(
    scripted_gateway: Callable[..., ScriptedGateway],
) -> None:
    ontology = frozen_ontology()
    shown = estimation.pair_batch_items("Module")
    assert len(shown) == estimation.PAIR_BATCH_PRICED_ITEMS <= PAIR_BATCH_ITEMS
    by_name = {item.name: item for item in shown}
    # One catalog entry names them all, and blocks of two keep most of them apart: one pair batch of all.
    catalog = (HubName(name=shown[0].name, label="Module", aliases=tuple(item.name for item in shown[1:])),)
    gateway = scripted_gateway().handle(HUBS_PROMPT, lambda _: {"groups": []})
    await judge_hub_items(gateway, shown, ConsolidationParams(block_size=2, block_overlap=0), catalog)
    batch = next(call for call in gateway.calls_for(HUBS_PROMPT) if len(sent_items(call.messages, by_name)) > 2)
    rendered = hub_block_messages(ontology, gateway, sent_items(batch.messages, by_name))
    assert list(batch.messages) == rendered
    assert request_tokens(rendered) == request_tokens(hub_block_messages(ontology, gateway, shown))


async def test_pair_calls_follow_the_unseen_hubs_and_the_cap(scripted_gateway: Callable[..., ScriptedGateway]) -> None:
    ontology = frozen_ontology()
    gateway = scripted_gateway()
    memory = estimation.DecisionMemory(MemoryStore(), PROJECT, "judge")
    few = [estimation.Expected("Module", f"Hub {index:03d}") for index in range(100)]
    one_block = await estimation._consolidation(few, estimation._Judging(memory, (), gateway, ontology))
    block = request_tokens(hub_block_messages(ontology, gateway, [estimation.hub_judge_item(hub) for hub in few]))
    # one block holds every candidate pair of its part: no pair call
    assert (one_block.calls, one_block.input_tokens) == (1, block)
    hubs = [estimation.Expected("Module", f"Hub {index:03d}") for index in range(300)]
    step = await estimation._consolidation(hubs, estimation._Judging(memory, (), gateway, ontology))
    block_list = blocks(list(range(300)))
    pair_calls = round(estimation.PAIR_CALLS_PER_HUB * 300)
    assert step.calls == len(block_list) + pair_calls  # the blocks of the 300 hubs, then the pair batches
    batch = request_tokens(hub_block_messages(ontology, gateway, estimation.pair_batch_items("Module")))
    blocks_tokens = sum(
        request_tokens(hub_block_messages(ontology, gateway, [estimation.hub_judge_item(hubs[i]) for i in block]))
        for block in block_list
    )
    assert step.input_tokens == blocks_tokens + pair_calls * batch
    many = [estimation.Expected("Module", f"Hub {index}") for index in range(5000)]
    capped = await estimation._consolidation(many, estimation._Judging(memory, (), gateway, ontology))
    assert capped.calls - len(blocks(list(range(5000)))) == estimation.PAIR_CALL_CAP
    assert gateway.calls == []


async def test_hubs_of_labels_kept_apart_are_priced_in_blocks_of_their_own(
    scripted_gateway: Callable[..., ScriptedGateway],
) -> None:
    ontology = shipped_ontology()
    gateway = scripted_gateway()
    memory = estimation.DecisionMemory(MemoryStore(), PROJECT, "judge")
    hubs = [estimation.Expected("Module", "Orders service"), estimation.Expected("Feature", "Order export")]
    hubs += [estimation.Expected("DataEntity", "Order"), estimation.Expected("DataEntity", "Invoice")]
    hubs += [estimation.Expected("Interface", "Orders API")]
    step = await estimation._consolidation(hubs, estimation._Judging(memory, (), gateway, ontology))
    assert step.calls == 2  # the data entities, the others; the one interface needs no block


def test_dedup_calls_grow_with_the_pool_of_requirements_each_new_one_is_compared_with(
    scripted_gateway: Callable[..., ScriptedGateway],
) -> None:
    """judged dedup pairs per new requirement grow with the square root of the id-less
    requirements in all (a persisted span-keyed one is in the pool, costs no call of its own)."""

    ontology = shipped_ontology()
    gateway = scripted_gateway()
    label = "FunctionalRequirement"

    def new(count: int) -> list[estimation.Expected]:
        return [estimation.Expected(label, f"New {index:04d}") for index in range(count)]

    persisted = [
        estimation.Expected(label, f"Old {index:04d}", persisted=True, key=f"requirement|span|old {index:04d}")
        for index in range(900)
    ]
    rate = estimation.DEDUP_CALLS_PER_REQUIREMENT
    assert estimation.DEDUP_REFERENCE_REQUIREMENTS == 1000
    assert estimation._dedup(new(1000), gateway, ontology).calls == round(rate * 1000)
    assert estimation._dedup(new(4000), gateway, ontology).calls == round(rate * 4000 * 2)  # 4x pool: x8 calls
    alone = estimation._dedup(new(100), gateway, ontology).calls
    among = estimation._dedup(new(100) + persisted, gateway, ontology).calls
    assert (alone, among) == (round(rate * 100 * 0.1**0.5), round(rate * 100))
    assert estimation._dedup(persisted, gateway, ontology).calls == 0
    assert gateway.calls == []


# Recorded hub and dedup input for a large and a small corpus, under two prompt settings.
# Tuple: expected hubs (unnamed part, DataEntity, Interface), id-less requirements,
# hub consolidation (calls, input), requirement dedup (calls, input).
MEASURED_BUILDS: Final = {
    "large-a": ((251, 72, 3), 2828, (34, 204_019), (172, 151_752)),
    "small-a": ((40, 10, 1), 443, (3, 15_145), (17, 14_730)),
    "large-b": ((251, 72, 3), 2828, (51, 284_677), (205, 180_414)),
    "small-b": ((40, 10, 1), 443, (4, 19_330), (11, 9_887)),
}
ESTIMATE_TOLERANCE: Final = 0.25


@pytest.mark.parametrize("build", sorted(MEASURED_BUILDS))
async def test_hub_and_dedup_estimates_are_within_a_quarter_of_recorded_builds(
    scripted_gateway: Callable[..., ScriptedGateway], build: str
) -> None:
    ontology = shipped_ontology()
    gateway = scripted_gateway()
    (anchors, entities, interfaces), requirements, hubs_real, dedup_real = MEASURED_BUILDS[build]
    hubs = [estimation.Expected("Feature", f"Hub {index:03d}") for index in range(anchors)]
    hubs += [estimation.Expected("DataEntity", f"Entity {index:03d}") for index in range(entities)]
    hubs += [estimation.Expected("Interface", f"Interface {index:03d}") for index in range(interfaces)]
    memory = estimation.DecisionMemory(MemoryStore(), PROJECT, "judge")
    consolidation = await estimation._consolidation(hubs, estimation._Judging(memory, (), gateway, ontology))
    every = [estimation.Expected("FunctionalRequirement", f"Requirement {index:04d}") for index in range(requirements)]
    dedup = estimation._dedup(every, gateway, ontology)
    for estimated, (_, tokens) in ((consolidation, hubs_real), (dedup, dedup_real)):
        assert abs(estimated.input_tokens - tokens) <= ESTIMATE_TOLERANCE * tokens, (build, estimated, tokens)
    assert gateway.calls == []


# Inference


NO_LINK: Final[dict[str, dict[str, Any]]] = {
    "attach_orphan": {"target": None},
    "endpoints": {"links": []},
    "realization": {"targets": []},
    "cross_document": {"links": [], "owners": []},
}
"""A valid verdict proposing nothing, per template (the scripted judge's answer)."""


def rule_of(ontology: Ontology, template: str) -> InferenceRule:
    return next(rule for rule in ontology.inference.rules if rule.template == template)


def with_limits(ontology: Ontology, **limits: Any) -> Ontology:
    return dataclasses.replace(ontology, inference=dataclasses.replace(ontology.inference, **limits))


def placeholders(ontology: Ontology, nodes: int, hubs: int) -> list[estimation.Expected]:
    """``nodes`` non-hub and ``hubs`` hub placeholders (a first build's expected nodes: labelled by position)."""

    other = min(label for label in ontology.extracted_labels if label not in ontology.hub_labels)
    hub = min(ontology.hub_labels)
    return [estimation.Expected(other, f"n{index}") for index in range(nodes)] + [
        estimation.Expected(hub, f"h{index}") for index in range(hubs)
    ]


def persisted(counts: dict[str, int], tag: str = "p") -> list[estimation.Expected]:
    return [
        estimation.Expected(label, f"{tag}-{label}-{index}", persisted=True)
        for label, count in counts.items()
        for index in range(count)
    ]


def inference_inputs(
    mode: Any,
    every: list[estimation.Expected],
    *,
    changed: list[estimation.Expected] | None = None,
    scope: list[estimation.Expected] | None = None,
    length: int = 2000,
    state: estimation.InferenceState | None = None,
) -> estimation._InferenceInputs:
    changed = every if changed is None else changed
    return estimation._InferenceInputs(
        mode=mode,
        every=every,
        changed=changed,
        scope=changed if scope is None else scope,
        length=length,
        state=state or estimation.InferenceState(),
    )


def placeholder_node(uuid: str, tag: str, label: str, fragments: tuple[int, ...], ontology: Ontology) -> _Node:
    """The infer step's node for an estimate placeholder (``estimate._shown``) grounded in the
    placeholder fragments ``fragments`` (``estimate._fragment_id``), in the placeholder section of an
    ``attach_orphan`` node (``estimate._owner_shown``) in each."""

    view = estimation._shown(tag, label, ontology)
    (section,) = estimation._owner_shown(view)["sections"]
    return _Node(
        uuid=uuid,
        spec_id=f"SPEC::{uuid}",
        name=view["name"],
        label=label,
        identity_key=uuid,
        statements=tuple(view["statements"]),
        fragments=tuple((estimation._fragment_id(index), f"h{index}") for index in fragments),
        sections=(section,) * len(fragments),
    )


def texts_of(fragment_ids: Any, length: int) -> dict[str, str]:
    """The placeholder texts the estimate shows for the fragments of a payload, in its order."""

    return {fragment_id: estimation._placeholder(f"f{index}", length) for index, fragment_id in enumerate(fragment_ids)}


def infer_step_request(ontology: Ontology, rule: InferenceRule, length: int, pairs: int) -> dict[str, Any]:
    """The payload (with fragments) ``graph.build.infer`` builds for the nodes the estimate's
    placeholder request shows (``infer._judgement``)."""

    target = min(rule.targets)
    subject_label = min(rule.subject)
    if rule.template == CROSS_DOCUMENT:
        hub = placeholder_node("hub", "hub", subject_label, (0,), ontology)
        members = [
            placeholder_node(f"member{index:02d}", f"member{index:02d}", target, (index,), ontology)
            for index in range(1, 2 * pairs + 1)
        ]
        nodes = {node.uuid: node for node in (hub, *members)}
        subject = _Subject(
            "hub",
            "SPEC::hub",
            targets=tuple(nodes),
            members=tuple(member.uuid for member in members),
            pairs=tuple((members[index].uuid, members[index + 1].uuid) for index in range(0, 2 * pairs, 2)),
            candidates=("hub",),
            via={members[0].uuid: ("hub",)},
            owners={member.uuid: "hub" for member in members[1:]},
            documents={
                member.uuid: estimation._placeholder(f"document{index % 2}", estimation.PLACEHOLDER_HEADING_CHARS)
                for index, member in enumerate(members)
            },
        )
        found = _judgement(rule, subject, nodes, "judge", ontology=ontology)
    else:
        subject_node = placeholder_node("subject", "subject", subject_label, (0,), ontology)
        if rule.template == ENDPOINTS:
            ids = [f"T{index}" for index in range(1, max(rule.min_targets or 1, estimation.PAIR_TARGETS) + 1)]
            others = [
                placeholder_node(local, f"target{local}", target, (index,), ontology)
                for index, local in enumerate(ids, 1)
            ]
            subject = _Subject(
                "subject",
                "SPEC::subject",
                targets=tuple(node.uuid for node in others),
                pairs=tuple(combinations((node.uuid for node in others), 2)),
            )
        elif rule.template == REALIZATION:
            member = min(label for label in ontology.extracted_labels if label not in ontology.hub_labels)
            others = [placeholder_node("M1", "memberM1", member, (1,), ontology)]
            others.append(placeholder_node("C1", "candidate1", target, (2,), ontology))
            subject = _Subject("subject", "SPEC::subject", candidates=("C1",), members=("M1",), via={"C1": ("M1",)})
        else:
            others = [
                placeholder_node(f"c{index}", f"candidate{index}", target, (index,), ontology)
                for index in range(1, (rule.max_candidates or 1) + 1)
            ]
            subject = _Subject("subject", "SPEC::subject", candidates=tuple(node.uuid for node in others))
        nodes = {node.uuid: node for node in (subject_node, *others)}
        found = _judgement(rule, subject, nodes, "judge")
    assert found is not None
    return {**found.payload, FRAGMENTS_KEY: texts_of(found.fragments, length)}


@pytest.mark.parametrize("template", ["attach_orphan", "endpoints", "realization", "cross_document"])
@pytest.mark.parametrize("pairs", [1, 3, 8])
async def test_each_inference_template_is_priced_with_the_request_the_infer_step_sends(
    scripted_gateway: Callable[..., ScriptedGateway], template: str, pairs: int
) -> None:
    ontology = shipped_ontology()
    rule = rule_of(ontology, template)
    length = 900
    sent = infer_step_request(ontology, rule, length, pairs if template == CROSS_DOCUMENT else 1)
    priced = estimation.inference_request(ontology, rule, length, pairs=pairs)
    assert priced == sent
    gateway = scripted_gateway().fallback(lambda _: {"confidence": 0.9, "evidence": [], **NO_LINK[template]})
    await JudgeRunner(gateway, ontology).judge(rule.prompt, sent, VERDICT_TYPES[template])
    (call,) = gateway.calls
    rendered = estimation.inference_messages(ontology, rule, length, pairs=pairs)
    assert list(call.messages) == rendered and call.prompt == rule.prompt
    assert request_tokens(rendered) > 0


async def test_a_batched_orphan_call_is_priced_as_the_infer_step_builds_and_sends_it(
    scripted_gateway: Callable[..., ScriptedGateway],
) -> None:
    """Orphans of one section, each with its own candidates, overlapping as ``BATCH_NEW_CANDIDATES`` says:
    the priced payload is the one ``infer._call`` builds for them."""

    ontology = shipped_ontology()
    rule = dataclasses.replace(rule_of(ontology, "attach_orphan"), batch=8)
    assert rule.batch_prompt is not None and estimation.batch_size(rule) == round(8 * estimation.BATCH_FILL)
    assert estimation.batch_size(rule_of(ontology, "attach_orphan")) == 1  # the shipped rule sets no batch
    length, subjects = 700, 3
    own, step = rule.max_candidates or 1, estimation.BATCH_NEW_CANDIDATES
    target, label = min(rule.targets), min(rule.subject)
    shown = own + step * (subjects - 1)
    candidates = [
        placeholder_node(f"c{index}", f"candidate{index}", target, (index,), ontology) for index in range(1, shown + 1)
    ]
    members = [
        placeholder_node(f"s{index}", f"subject{index}", label, (0,), ontology) for index in range(1, subjects + 1)
    ]
    nodes = {node.uuid: node for node in (*members, *candidates)}
    items = []
    for index, member in enumerate(members):
        mine = tuple(node.uuid for node in candidates[step * index : step * index + own])
        subject = _Subject(member.uuid, member.spec_id, candidates=mine)
        found = _judgement(rule, subject, nodes, "judge")
        assert found is not None
        items.append((subject, found))
    texts = {estimation._fragment_id(index): estimation._placeholder(f"f{index}", length) for index in range(shown + 1)}
    run = _Run(ontology, rule, nodes, validator=cast(Any, None), owners={}, memory=cast(Any, None))
    call = _call(run, items, texts)
    assert call.prompt == rule.batch_prompt
    assert estimation.inference_request(ontology, rule, length, subjects=subjects) == call.payload
    rendered = estimation.inference_messages(ontology, rule, length, subjects=subjects)
    assert request_tokens(rendered) == call.tokens
    gateway = scripted_gateway().fallback(lambda _: {"confidence": 0.9, "evidence": [], "answers": []})
    await JudgeRunner(gateway, ontology).judge(call.prompt, call.payload, call.verdict_type)
    assert list(gateway.calls[0].messages) == rendered


def test_batched_orphans_are_priced_batch_fill_subjects_per_call() -> None:
    ontology = shipped_ontology()
    orphans = rule_of(ontology, "attach_orphan")
    single = with_limits(ontology, rules=(orphans,), cap=1000)
    batched = with_limits(ontology, rules=(dataclasses.replace(orphans, batch=8),), cap=1000)
    every = placeholders(ontology, 1000, 0)
    fresh = estimation._fresh_subjects(orphans, inference_inputs("first", every), ontology)
    one = estimation._inference(inference_inputs("first", every), single).inference
    four = estimation._inference(inference_inputs("first", every), batched).inference
    assert one.calls == fresh and four.calls == math.ceil(fresh / 4)  # batch 8, half filled
    full = request_tokens(estimation.inference_messages(ontology, orphans, 2000, subjects=4))
    rest = fresh % 4
    last = request_tokens(estimation.inference_messages(ontology, orphans, 2000, subjects=rest)) if rest else 0
    assert four.input_tokens == fresh // 4 * full + last < one.input_tokens


def test_first_build_subjects_follow_the_rates_of_the_expected_subject_labels() -> None:
    ontology = shipped_ontology()
    orphans = next(rule for rule in ontology.inference.rules if rule.template == "attach_orphan")
    hubs = [label for label in ontology.extracted_labels if label in ontology.hub_labels]
    others = [label for label in ontology.extracted_labels if label not in ontology.hub_labels]
    every = placeholders(ontology, 700, 100)
    nodes, hub_nodes = estimation._subject_nodes(orphans, every, ontology)
    # a placeholder counts as its kind's share of the subject labels, whatever label it was given
    assert nodes == pytest.approx(700 * sum(label in orphans.subject for label in others) / len(others))
    assert hub_nodes == pytest.approx(100 * sum(label in orphans.subject for label in hubs) / len(hubs))
    # a persisted node counts by its own label
    real = persisted({min(orphans.subject): 5, min(ontology.hub_labels - orphans.subject): 7})
    assert sum(estimation._subject_nodes(orphans, real, ontology)) == 5
    rate = estimation.SUBJECT_RATES["attach_orphan"].nodes
    assert rate.scaled
    four = estimation._subjects(orphans, persisted({min(orphans.subject): 4000}), ontology)
    one = estimation._subjects(orphans, persisted({min(orphans.subject): 1000}), ontology)
    assert one == pytest.approx(rate.rate * 1000) and four == pytest.approx(8 * one)  # 4x the pool: x8


def test_the_infer_step_limits_cut_a_first_build_and_predict_its_queue() -> None:
    ontology = shipped_ontology()
    every = placeholders(ontology, 3000, 300)
    inputs = inference_inputs("first", every)
    rules = ontology.inference.rules
    fresh = {rule.id: estimation._fresh_subjects(rule, inputs, ontology) for rule in rules}
    priced = {
        rule.id: request_tokens(
            estimation.inference_messages(
                ontology, rule, inputs.length, pairs=estimation._priced_pairs(rule, ontology, every)
            )
        )
        for rule in rules
    }
    unbounded = estimation._inference(inputs, with_limits(ontology, first_budget=None))
    assert unbounded.backlog == {}
    assert unbounded.inference.calls == sum(fresh[rule.id] for rule in rules if rule.template != CROSS_DOCUMENT)
    # a budget for the first rule's subjects only: every later rule still judges its cap
    first_rule = rules[0]
    budget = fresh[first_rule.id] * priced[first_rule.id]
    limited = with_limits(ontology, first_budget=budget)
    cut = estimation._inference(inputs, limited)
    for rule in rules[1:]:
        taken = infer_bound.bound(
            [priced[rule.id]] * fresh[rule.id],
            infer_bound.limits_of(limited.inference, rule, first=True),
            spent=budget,
        ).taken
        assert taken == min(fresh[rule.id], ontology.inference.cap_of(rule))
        assert cut.backlog.get(rule.id, 0) == fresh[rule.id] - taken
    assert first_rule.id not in cut.backlog and cut.backlog
    # first_cap: past the cap, at most first_cap subjects per rule
    capped = estimation._inference(inputs, with_limits(ontology, first_budget=None, first_cap=150, cap=100))
    assert capped.backlog[first_rule.id] == fresh[first_rule.id] - 150
    # an update keeps the cap
    update = estimation._inference(inference_inputs("update", every), ontology)
    assert update.inference.calls <= sum(
        ontology.inference.cap_of(rule) for rule in rules if rule.template != CROSS_DOCUMENT
    )


def test_an_update_drains_the_exact_queue_plus_the_changed_subjects_and_the_cross_document_scope() -> None:
    ontology = shipped_ontology()
    orphans, xdoc = rule_of(ontology, "attach_orphan"), rule_of(ontology, CROSS_DOCUMENT)
    requirement, hub = min(orphans.subject), min(xdoc.subject)
    every = persisted({requirement: 400, hub: 60})
    changed = persisted({requirement: 6, hub: 2}, tag="changed")
    scope = [*changed, *persisted({hub: 8}, tag="mentioned")]
    state = estimation.InferenceState(queued={orphans.id: 30, xdoc.id: 5}, unqueued={orphans.id: 999})
    inputs = inference_inputs("update", every, changed=changed, scope=scope, state=state)
    assert estimation._fresh_subjects(orphans, inputs, ontology) == 30 + round(estimation.CHANGED_SUBJECTS_PER_NODE * 6)
    assert estimation._fresh_subjects(xdoc, inputs, ontology) == 5 + round(estimation.CROSS_DOCUMENT_SCOPE_PER_HUB * 10)
    # the queue alone, exactly, when no subject node changed; over the cap it is the predicted queue
    queued = inference_inputs("update", every, changed=persisted({hub: 1}, tag="c"), scope=[], state=state)
    assert estimation._fresh_subjects(orphans, queued, ontology) == 30
    big = estimation.InferenceState(queued={orphans.id: 250})
    judged = estimation._inference(inference_inputs("update", every, changed=[], scope=[], state=big), ontology)
    cap = ontology.inference.cap_of(orphans)
    assert judged.inference.calls == cap and judged.backlog == {orphans.id: 250 - cap}


def test_an_update_judges_again_the_share_of_judged_subjects_that_show_a_target_in_scope() -> None:
    ontology = shipped_ontology()
    orphans = rule_of(ontology, "attach_orphan")
    requirement, target = min(orphans.subject - ontology.hub_labels), min(orphans.targets)
    every = persisted({requirement: 400, target: 100})
    changed = persisted({requirement: 6, target: 2}, tag="changed")
    scope = [*changed, *persisted({target: 3}, tag="mentioned")]
    new = [estimation.Expected(requirement, "new-0"), estimation.Expected(requirement, "new-1")]
    state = estimation.InferenceState(queued={orphans.id: 30}, judged={orphans.id: 300})
    inputs = inference_inputs("update", every, changed=[*changed, *new], scope=scope, state=state)
    share = estimation.CHANGED_INPUT_SHARE.nodes.rate * 5 / 100  # 5 of the 100 targets in scope
    added = round(estimation._subjects(orphans, new, ontology, pool=every))
    assert estimation._fresh_subjects(orphans, inputs, ontology) == 30 + added + round(300 * share)
    # at most every subject judged before; none judged before: the queue and the new subjects only
    wide = dataclasses.replace(inputs, scope=persisted({target: 100}, tag="all"))
    assert estimation._fresh_subjects(orphans, wide, ontology) == 30 + added + 300
    none = dataclasses.replace(inputs, state=estimation.InferenceState(queued={orphans.id: 30}, judged={orphans.id: 0}))
    assert estimation._fresh_subjects(orphans, none, ontology) == 30 + added
    # the state counts the subjects a report judged: its subjects less its deferred ones
    report = InferenceReport(rules=(RuleReport(rule="A", subjects=1267, deferred=219),))
    assert dict(estimation.InferenceState.of(report).judged) == {"A": 1048}


def test_an_update_without_a_dirty_fragment_drains_the_queue_and_the_never_judged_subjects() -> None:
    ontology = shipped_ontology()
    xdoc = rule_of(ontology, CROSS_DOCUMENT)
    state = estimation.InferenceState(queued={xdoc.id: 10}, unqueued={xdoc.id: 40})
    inputs = inference_inputs("update", persisted({min(xdoc.subject): 50}), changed=[], scope=[], state=state)
    assert estimation._fresh_subjects(xdoc, inputs, ontology) == 50
    judged = estimation._inference(inputs, ontology)
    cap = ontology.inference.cap_of(xdoc)
    assert judged.cross_document.calls == cap and judged.backlog == {xdoc.id: 50 - cap}
    assert judged.inference == StepEstimate()
    off = estimation._inference(dataclasses.replace(inputs, state=dataclasses.replace(state, enabled=False)), ontology)
    assert (off.inference, off.cross_document, off.backlog) == (StepEstimate(), StepEstimate(), {})


def test_the_inference_state_is_the_latest_finished_builds_queue_and_its_unqueued_deferrals() -> None:
    report = InferenceReport(
        rules=(
            RuleReport(rule="A", deferred=6, failed=1),  # queue: 1 failed + 6 left by a limit
            RuleReport(rule="X", deferred=50),  # queue 10: 40 never judged outside it (an update's XDOC)
            RuleReport(rule="B", deferred=0),
        ),
        backlog={
            "A": BacklogQueue(spec_ids=tuple(f"a{index}" for index in range(7)), since_build="b", age_builds=1),
            "X": BacklogQueue(spec_ids=tuple(f"x{index}" for index in range(10)), since_build="b", age_builds=2),
        },
    )
    state = estimation.InferenceState.of(report)
    assert dict(state.queued) == {"A": 7, "X": 10} and dict(state.unqueued) == {"X": 40}
    assert estimation.InferenceState.of(None, enabled=False) == estimation.InferenceState(enabled=False)


async def test_estimate_build_reads_the_queue_of_the_latest_finished_build_and_predicts_the_next(
    stores: FileStores, root: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    gateway = scripted_gateway()
    first = await estimate_build(context(stores, root, gateway))
    assert first.inference_backlog == {} and first.steps["inference"].calls > 0
    off = await estimate_build(context(stores, root, gateway, settings=BuildSettings(inference=False)))
    assert off.steps["inference"] == off.steps["cross_document_inference"] == StepEstimate()
    ctx = context(stores, root, gateway, executor=OfflineExecutor(PERSISTED))
    record = await finished_first(stores, ctx)
    orphans = rule_of(ctx.ontology, "attach_orphan")
    queue = BacklogQueue(
        spec_ids=tuple(f"SPEC::R-{index}" for index in range(500)), since_build="first-0", age_builds=1
    )
    await stores.builds.put_artifact(
        PROJECT, record.build_id, "infer", InferenceReport(backlog={orphans.id: queue}).model_dump(mode="json")
    )
    state = await estimation.inference_state(stores.builds, PROJECT)
    assert dict(state.queued) == {orphans.id: 500}
    update = await estimate_build(ctx)
    assert update.mode == "update" and gateway.calls == []
    cap = ctx.ontology.inference.cap_of(orphans)
    assert update.inference_backlog is not None and update.inference_backlog[orphans.id] >= 500 - cap


# Older limits: cap 50, no first cap and no first budget. A full drain is what the shipped limits project.
OLDER_LIMITS: Final = {"cap": 50, "first_cap": None, "first_budget": None}
FIRST_TOLERANCE: Final = 0.30
UPDATE_TOLERANCE: Final = 0.25


@dataclasses.dataclass(frozen=True)
class MeasuredFirst:
    nodes: int
    hubs: int
    length: int
    actual: dict[str, tuple[int, int]]
    subjects: dict[str, int]


@dataclasses.dataclass(frozen=True)
class MeasuredUpdate:
    pool: dict[str, int]
    changed: dict[str, int]
    mentioned: dict[str, int]
    length: int
    queued: dict[str, int]
    actual: dict[str, tuple[int, int]]
    judged: dict[str, int]
    """Subjects judged before per rule (``InferenceState.judged``: the first build's subjects less deferred)."""
    limits: dict[str, Any] | None = None
    """The limits the build ran under (None: the shipped ones)."""


MEASURED_FIRST: Final = {
    "large": MeasuredFirst(
        2828,
        326,
        2276,
        {"inference": (102, 410_140), "cross_document_inference": (36, 617_562)},
        {"inference": 1452, "cross_document_inference": 212},
    ),
    "small": MeasuredFirst(
        443,
        51,
        2359,
        {"inference": (81, 303_494), "cross_document_inference": (14, 121_364)},
        {"inference": 117, "cross_document_inference": 14},
    ),
}
MEASURED_UPDATE: Final = {
    "large": MeasuredUpdate(
        {
            "FunctionalRequirement": 1666,
            "QualityRequirement": 168,
            "DataRequirement": 71,
            "Constraint": 45,
            "IntegrationRequirement": 36,
            "BusinessRule": 27,
            "OpenQuestion": 9,
            "Feature": 152,
            "Module": 91,
            "Interface": 69,
            "DataEntity": 44,
            "Capability": 17,
        },
        {"FunctionalRequirement": 21, "DataEntity": 3, "Module": 2, "Feature": 1},
        {"Feature": 10, "Interface": 5, "DataEntity": 4, "Module": 1, "Capability": 1},
        2276,
        {"INF-CONT-01": 1295, "INF-CONT-02": 55},
        {"inference": (100, 421_994), "cross_document_inference": (16, 286_204)},
        {"INF-CONT-01": 50, "INF-CONT-02": 50, "INF-INT-01": 0, "INF-CAP-01": 2, "INF-XDOC-01": 36},
        OLDER_LIMITS,
    ),
    "small": MeasuredUpdate(
        {
            "FunctionalRequirement": 84,
            "OpenQuestion": 52,
            "QualityRequirement": 46,
            "Constraint": 14,
            "IntegrationRequirement": 10,
            "BusinessRule": 2,
            "DataRequirement": 2,
            "Feature": 42,
            "Capability": 17,
            "Module": 15,
            "DataEntity": 2,
            "Interface": 1,
        },
        {
            "FunctionalRequirement": 12,
            "Feature": 8,
            "Constraint": 5,
            "QualityRequirement": 2,
            "Module": 2,
            "BusinessRule": 1,
        },
        {"Feature": 3, "Capability": 2, "Module": 1},
        2359,
        {"INF-CONT-01": 36},
        {"inference": (74, 291_188), "cross_document_inference": (12, 100_882)},
        {"INF-CONT-01": 50, "INF-CONT-02": 25, "INF-INT-01": 5, "INF-CAP-01": 1, "INF-XDOC-01": 14},
        OLDER_LIMITS,
    ),
    "large-full": MeasuredUpdate(
        {
            "FunctionalRequirement": 1607,
            "QualityRequirement": 162,
            "DataRequirement": 78,
            "IntegrationRequirement": 56,
            "Constraint": 54,
            "BusinessRule": 48,
            "OpenQuestion": 15,
            "Feature": 130,
            "Module": 98,
            "Interface": 55,
            "DataEntity": 40,
            "Capability": 38,
        },
        {"FunctionalRequirement": 21, "Module": 4, "Capability": 1},
        {"Feature": 9, "DataEntity": 7, "Interface": 5, "Capability": 3, "Module": 1},
        2276,
        {},
        {"inference": (149, 715_036), "cross_document_inference": (36, 579_608)},
        {"INF-CONT-01": 1321, "INF-CONT-02": 83, "INF-INT-01": 3, "INF-CAP-01": 9, "INF-XDOC-01": 176},
    ),
    "small-full": MeasuredUpdate(
        {
            "FunctionalRequirement": 81,
            "OpenQuestion": 53,
            "QualityRequirement": 43,
            "Constraint": 18,
            "IntegrationRequirement": 10,
            "DataRequirement": 2,
            "BusinessRule": 1,
            "Feature": 49,
            "Module": 17,
            "Capability": 7,
            "DataEntity": 2,
            "Interface": 1,
        },
        {
            "FunctionalRequirement": 12,
            "Feature": 7,
            "Constraint": 3,
            "QualityRequirement": 2,
            "Module": 1,
            "BusinessRule": 1,
        },
        {"Feature": 4, "Capability": 1, "Module": 1},
        2359,
        {},
        {"inference": (74, 333_911), "cross_document_inference": (8, 64_443)},
        {"INF-CONT-01": 90, "INF-CONT-02": 26, "INF-INT-01": 6, "INF-CAP-01": 5, "INF-XDOC-01": 10},
    ),
}


def within(estimated: StepEstimate, actual: tuple[int, int], tolerance: float) -> bool:
    calls, tokens = actual
    return (
        abs(estimated.calls - calls) <= tolerance * calls and abs(estimated.input_tokens - tokens) <= tolerance * tokens
    )


@pytest.mark.parametrize("corpus", sorted(MEASURED_FIRST))
def test_first_build_inference_is_within_thirty_percent_of_older_limits(corpus: str) -> None:
    """A first build under the older limits stays near the recorded call counts."""

    ontology = shipped_ontology()
    measured = MEASURED_FIRST[corpus]
    inputs = inference_inputs("first", placeholders(ontology, measured.nodes, measured.hubs), length=measured.length)
    older = estimation._inference(inputs, with_limits(ontology, **OLDER_LIMITS))
    assert within(older.cross_document, measured.actual["cross_document_inference"], FIRST_TOLERANCE), corpus
    calls = measured.actual["inference"][0]
    assert abs(older.inference.calls - calls) <= FIRST_TOLERANCE * calls, (corpus, older.inference)
    shipped = estimation._inference(inputs, ontology)
    assert shipped.backlog == {}
    for step, estimated in (("inference", shipped.inference), ("cross_document_inference", shipped.cross_document)):
        subjects = measured.subjects[step]
        assert abs(estimated.calls - subjects) <= FIRST_TOLERANCE * subjects, (corpus, step, estimated)
    total = shipped.inference.input_tokens + shipped.cross_document.input_tokens
    assert ontology.inference.first_budget is not None and total <= ontology.inference.first_budget


MEASURED_FIRST_SHIPPED: Final = {
    "large": (1416, 6_205_615),
    "small": (127, 518_370),
}


@pytest.mark.parametrize("corpus", sorted(MEASURED_FIRST_SHIPPED))
def test_first_build_inference_is_within_thirty_percent_of_shipped_limits(corpus: str) -> None:
    """A first build under the shipped limits stays near the recorded calls and input."""

    ontology = shipped_ontology()
    measured = MEASURED_FIRST[corpus]
    inputs = inference_inputs("first", placeholders(ontology, measured.nodes, measured.hubs), length=measured.length)
    found = estimation._inference(inputs, ontology)
    assert within(found.inference, MEASURED_FIRST_SHIPPED[corpus], FIRST_TOLERANCE), (corpus, found.inference)


@pytest.mark.parametrize("corpus", sorted(MEASURED_UPDATE))
def test_update_inference_is_within_a_quarter_of_recorded_builds(corpus: str) -> None:
    """An update stays near the recorded inference of that corpus."""

    ontology = shipped_ontology()
    measured = MEASURED_UPDATE[corpus]
    changed = persisted(measured.changed, tag="changed")
    hubs = {label: count for label, count in measured.mentioned.items() if label in ontology.hub_labels}
    inputs = inference_inputs(
        "update",
        persisted(measured.pool),
        changed=changed,
        scope=[*changed, *persisted(hubs, tag="mentioned")],
        length=measured.length,
        state=estimation.InferenceState(queued=measured.queued, judged=measured.judged),
    )
    limited = ontology if measured.limits is None else with_limits(ontology, **measured.limits)
    found = estimation._inference(inputs, limited)
    steps = ["inference"]
    if corpus != "large-full":
        steps.append("cross_document_inference")
    for step in steps:
        estimated = found.inference if step == "inference" else found.cross_document
        actual = measured.actual[step]
        if measured.limits is not None or step == "cross_document_inference":
            assert abs(estimated.calls - actual[0]) <= UPDATE_TOLERANCE * actual[0], (corpus, step, estimated)
            continue
        assert within(estimated, actual, UPDATE_TOLERANCE), (corpus, step, estimated)
