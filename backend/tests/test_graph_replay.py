"""The A/B replay tool (``examples/graph_replay.py``)."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import graph_replay as replay_tool
from graph.build import BuildContext, open_file_stores, steps
from graph.build import infer as inference
from graph.build.changes import DEFAULT_EDGE_WINDOWS
from graph.build.consolidate import HUBS_PROMPT
from graph.build.models import ArtifactModel, EdgesArtifact, InferenceReport, ResolveArtifact, TargetGraph
from graph.build.prompts import ATTRIBUTES_PROMPT, EDGE_WINDOW_PROMPT
from graph.core.ontology import Ontology
from graph_probes import requires_live_graph
from graph_replay import BuildSpec, InferSettings, ReplayRequest, ReplayStep, parse_build_spec, replay
from graph_support import (
    SHOP_NEARNESS,
    GraphProject,
    OfflineExecutor,
    ScriptedGateway,
    live_llm_config,
    shipped_ontology,
    shop_edge_windows,
    tree_hash,
)
from graph_v2_support import ShopV2Builds, nearness, script_conform_judge, shop_v2_answers, shop_v2_corpus

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, graph_project) live in graph_support.
pytest_plugins = ["graph_support"]

PROJECT = "pytest-graph-replay"
BUILD_ID = "0123456789abcdef0123456789abcdef"
REPLAYS: tuple[tuple[ReplayStep, type[ArtifactModel]], ...] = (("resolve", ResolveArtifact), ("conform", TargetGraph))
SHOP_WINDOWS = dataclasses.replace(DEFAULT_EDGE_WINDOWS, nearness=SHOP_NEARNESS)
UNIT_WINDOWS = dataclasses.replace(SHOP_WINDOWS, target_tokens=None)
JUDGE_COUNTS = ("rule", "subjects", "judged", "accepted", "declined", "rejected", "invalid", "items_by_outcome")


def shop_gateway(factory: Callable[..., ScriptedGateway], ontology: Ontology) -> ScriptedGateway:
    """A gateway scripted for a shop-v2 snapshot-1 build up to Conform."""

    answers = shop_v2_answers(1)
    gateway = answers.install(factory(live_llm_config()), ontology)
    for near in nearness(answers):
        gateway.relate(near.anchor, near.cosine, matching=lambda text, prefix=near.prefix: text.startswith(prefix))
    return script_conform_judge(gateway, ontology)


@pytest.fixture
async def built(tmp_path: Path, scripted_gateway: Callable[..., ScriptedGateway]) -> tuple[Path, str]:
    """The state folder and build id of a scripted shop-v2 first build, run through Conform."""

    ontology, state = shipped_ontology(), tmp_path / "source"
    stores = open_file_stores(state)
    ctx = BuildContext(
        project_id=PROJECT,
        ontology=ontology,
        driver=None,
        gateway=shop_gateway(scripted_gateway, ontology),
        registry=stores.registry,
        corpus=shop_v2_corpus(1, state),
        extraction=stores.extraction,
        builds=stores.builds,
        options=shop_v2_answers(1).options,
        executor=OfflineExecutor(),
    )
    ctx = ctx.with_build_id(await steps.plan(ctx))
    with shop_edge_windows():
        for step in (steps.extract_nodes, steps.resolve, steps.extract_edges, steps.conform):
            await step(ctx)
    assert ctx.build_id is not None
    return state, ctx.build_id


async def test_replays_give_the_build_s_own_artifacts_and_leave_the_source_untouched(
    built: tuple[Path, str], tmp_path: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    state, build_id = built
    ontology, before = shipped_ontology(), tree_hash(state)
    source = open_file_stores(state).builds
    spec = BuildSpec(state, PROJECT, build_id)

    for step, model in REPLAYS:
        out = tmp_path / f"replay-{step}"
        gateway = shop_gateway(scripted_gateway, ontology)
        metrics = await replay(ReplayRequest(spec, step, out), gateway, ontology)

        stored = model.model_validate(await source.get_artifact(PROJECT, build_id, step))
        written = json.loads((out / f"{step}.json").read_text())
        assert model.model_validate(written) == stored
        assert metrics["same_as_build"] is True
        assert metrics["build"] == metrics["replay"]
        assert metrics["warnings"] == []
        assert metrics["extraction_copied"] == ["vectors"]
        assert json.loads((out / "metrics.json").read_text()) == metrics
        # stored decisions are left out, so the judges are asked again; every vector is cached
        assert gateway.calls, f"{step}: no judge call"
        assert "embed" not in metrics["usage"]["by_prompt"]
        # the report state holds the build with the replayed artifact
        scratch = open_file_stores(out / "state").builds
        assert model.model_validate(await scratch.get_artifact(PROJECT, build_id, step)) == stored
        assert metrics["report_state"] == f"{out / 'state'}:{PROJECT}:{build_id}"

    assert tree_hash(state) == before


async def test_resolve_replay_with_kept_decisions_asks_no_hub_judge(
    built: tuple[Path, str], tmp_path: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    state, build_id = built
    ontology = shipped_ontology()
    gateway = shop_gateway(scripted_gateway, ontology)
    request = ReplayRequest(BuildSpec(state, PROJECT, build_id), "resolve", tmp_path / "kept", keep_decisions=True)

    metrics = await replay(request, gateway, ontology)

    assert metrics["extraction_copied"] == ["vectors", "decisions"]
    assert HUBS_PROMPT not in metrics["usage"]["by_prompt"]
    assert not [call for call in gateway.calls if call.prompt == HUBS_PROMPT]


async def test_a_resolve_replay_persisted_from_a_build_resolves_against_its_conform_nodes(
    built: tuple[Path, str], tmp_path: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    state, build_id = built
    ontology = shipped_ontology()
    args = replay_tool.parse_args(["s:p", "--step", "resolve", "--out", "o", "--persisted-from", build_id])
    assert replay_tool.request_of(args).persisted_from == build_id
    request = ReplayRequest(
        BuildSpec(state, PROJECT, build_id), "resolve", tmp_path / "chained", persisted_from=build_id
    )

    metrics = await replay(request, shop_gateway(scripted_gateway, ontology), ontology)

    assert metrics["warnings"] == [f"persisted graph: the conform nodes of build {build_id} (identities only)"]
    target = TargetGraph.model_validate(await open_file_stores(state).builds.get_artifact(PROJECT, build_id, "conform"))
    persisted = await replay_tool.persisted_from(await replay_tool.open_source(request.spec), build_id)
    assert {row.identity_key for row in persisted.values()} == {node.identity_key for node in target.nodes}
    assert metrics["replay"]["canonical"] == metrics["build"]["canonical"]


async def test_an_output_inside_the_state_or_a_used_one_is_refused(
    built: tuple[Path, str], tmp_path: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    state, build_id = built
    ontology, spec = shipped_ontology(), BuildSpec(state, PROJECT, build_id)
    gateway = shop_gateway(scripted_gateway, ontology)
    with pytest.raises(ValueError, match="overlap"):
        await replay(ReplayRequest(spec, "resolve", state / "replay"), gateway, ontology)
    (tmp_path / "used" / "state").mkdir(parents=True)
    with pytest.raises(ValueError, match="exists"):
        await replay(ReplayRequest(spec, "resolve", tmp_path / "used"), gateway, ontology)
    assert gateway.calls == []


def test_the_build_spec_is_read_like_graph_report_s() -> None:
    assert parse_build_spec(f"a:b/state:proj:{BUILD_ID}") == BuildSpec(Path("a:b/state"), "proj", BUILD_ID)
    assert parse_build_spec("a:b/state:proj") == BuildSpec(Path("a:b/state"), "proj")
    assert parse_build_spec("state:proj:not-an-id") == BuildSpec(Path("state:proj"), "not-an-id")


def test_the_flags_give_the_consolidation_params() -> None:
    args = replay_tool.parse_args(
        ["s:p", "--step", "resolve", "--out", "o", "--evidence-lines", "3", "--block-size", "120", "--no-pair-pass"]
    )
    request = replay_tool.request_of(args)
    assert (request.params.evidence_lines, request.params.block_size, request.params.pair_pass) == (3, 120, False)
    assert request.params.pair_call_cap == replay_tool.DEFAULTS.pair_call_cap
    assert request.spec == BuildSpec(Path("s"), "p")


@pytest.mark.parametrize("flags", [["--block-size", "10", "--block-overlap", "10"], ["--block-size", "1"]])
def test_flags_of_blocks_that_never_advance_are_refused(flags: list[str]) -> None:
    with pytest.raises(SystemExit):
        replay_tool.parse_args(["s:p", "--step", "resolve", "--out", "o", "--block-overlap", "0", *flags])


@pytest.mark.parametrize("windows", [UNIT_WINDOWS, SHOP_WINDOWS], ids=["unit", "default"])
async def test_an_edges_replay_gives_the_build_s_edges_and_conform_without_asking_attributes(
    built: tuple[Path, str],
    tmp_path: Path,
    scripted_gateway: Callable[..., ScriptedGateway],
    windows: replay_tool.EdgeWindowParams,
) -> None:
    state, build_id = built
    ontology, before = shipped_ontology(), tree_hash(state)
    source = open_file_stores(state).builds
    gateway = shop_gateway(scripted_gateway, ontology)
    out = tmp_path / "replay-edges"

    metrics = await replay(
        ReplayRequest(BuildSpec(state, PROJECT, build_id), "edges", out, edges=windows), gateway, ontology
    )

    scratch = open_file_stores(out / "state").builds
    for step, model in (("extract_edges", EdgesArtifact), ("conform", TargetGraph)):
        stored = model.model_validate(await source.get_artifact(PROJECT, build_id, step))
        assert model.model_validate(json.loads((out / f"{step}.json").read_text())) == stored, step
        assert model.model_validate(await scratch.get_artifact(PROJECT, build_id, step)) == stored, step
    assert metrics["same_as_build"] is True
    assert metrics["build"] == metrics["replay"]
    assert metrics["warnings"] == []
    assert metrics["params"]["target_tokens"] == windows.target_tokens
    side = metrics["replay"]
    assert side["raw_edges"] > 0
    assert side["edge_counters"]["windows_called"] == side["edge_counters"]["calls"] > 0
    assert set(side["catalog_sizes"]) == {"units", "catalog", "mentioned", "heading", "near", "referenced"}
    assert {"requirements_without_edge", "foreign_edges", "windows_called"} <= set(side["edge_metrics"])
    # every window is called again, the attributes are the build's (never asked), every vector is cached
    by_prompt = metrics["usage"]["by_prompt"]
    assert by_prompt[EDGE_WINDOW_PROMPT]["calls"] == side["edge_counters"]["calls"]
    assert ATTRIBUTES_PROMPT not in by_prompt
    assert "embed" not in by_prompt
    total = metrics["input"]["total"]
    assert total["uncached"] + total["cached"] == sum(item["input_tokens"] for item in by_prompt.values())
    assert json.loads((out / "metrics.json").read_text()) == metrics
    assert tree_hash(state) == before


def test_the_edge_flags_give_the_window_params() -> None:
    args = replay_tool.parse_args(
        ["s:p", "--step", "edges", "--out", "o", "--edge-window-tokens", "unit", "--nearness", "rich"]
    )
    request = replay_tool.request_of(args)
    assert request.edges == dataclasses.replace(DEFAULT_EDGE_WINDOWS, target_tokens=None, nearness="rich")
    args = replay_tool.parse_args(
        ["s:p", "--step", "edges", "--out", "o", "--edge-window-tokens", "3000", "--section-hints", "--no-references"]
    )
    edges = replay_tool.request_of(args).edges
    assert (edges.target_tokens, edges.section_hints, edges.references) == (3000, True, False)
    assert replay_tool.request_of(replay_tool.parse_args(["s:p", "--step", "edges", "--out", "o"])).edges == (
        DEFAULT_EDGE_WINDOWS
    )


@pytest.mark.parametrize(
    "flags",
    [["--step", "infer"], ["--step", "edges", "--edge-window-tokens", "0"], ["--step", "edges", "--nearness", "x"]],
)
def test_an_infer_replay_without_dry_run_or_a_bad_edge_flag_is_refused(flags: list[str]) -> None:
    with pytest.raises(SystemExit):
        replay_tool.parse_args(["s:p", "--out", "o", *flags])


def test_the_infer_flags_select_rules_in_ontology_order_with_overrides() -> None:
    ontology = shipped_ontology()
    ids = [rule.id for rule in ontology.inference.rules]
    args = replay_tool.parse_args(
        ["s:p", "--step", "infer", "--dry-run", "--out", "o", "--rules", f"{ids[-1]},{ids[0]}", "--cap", "7"]
    )
    settings = replay_tool.request_of(args).infer
    assert settings == InferSettings((ids[-1], ids[0]), 7, None)
    chosen = replay_tool.selected_ontology(ontology, dataclasses.replace(settings, pair_floor=0.4))
    assert [rule.id for rule in chosen.inference.rules] == [ids[0], ids[-1]]
    assert {(rule.cap, rule.pair_floor) for rule in chosen.inference.rules} == {(7, 0.4)}
    assert [rule.id for rule in replay_tool.selected_ontology(ontology, InferSettings()).inference.rules] == ids
    with pytest.raises(ValueError, match="unknown inference rules"):
        replay_tool.selected_ontology(ontology, InferSettings(("NO-SUCH-RULE",)))


def test_the_floor_test_flags_lower_floors_take_first_limits_sample_and_batch() -> None:
    """: --floor, --first, --sample with --seed and --batch."""

    ontology = shipped_ontology()
    rules = {rule.template: rule for rule in ontology.inference.rules}
    orphan, xdoc = rules["attach_orphan"], rules["cross_document"]
    flags = ["--floor", "0.6", "--first", "--sample", f"{orphan.id}=300,{xdoc.id}=40", "--seed", "3", "--batch", "6"]
    args = replay_tool.parse_args(["s:p", "--step", "infer", "--dry-run", "--out", "o", *flags])
    settings = replay_tool.request_of(args).infer
    assert settings == InferSettings(floor=0.6, first=True, sample=((orphan.id, 300), (xdoc.id, 40)), seed=3, batch=6)
    chosen = replay_tool.selected_ontology(ontology, settings)
    assert chosen.inference.floor == 0.6
    floors = {rule.id: rule.floor for rule in chosen.inference.rules}
    assert floors[xdoc.id] == ontology.inference.floor_of(xdoc) == 0.85  # the link floor stays
    assert {floor for rule_id, floor in floors.items() if rule_id != xdoc.id} == {0.6}
    batches = {rule.id: rule.batch for rule in chosen.inference.rules}
    assert {rule_id for rule_id, batch in batches.items() if batch == 6} == {
        rule.id for rule in ontology.inference.rules if rule.template == "attach_orphan"
    }
    assert {batch for batch in batches.values() if batch != 6} == {None}  # the other templates: none
    changes = replay_tool.changes_of(chosen, settings)
    assert changes.first and changes.sample is not None and changes.sample.seed == 3
    assert dict(changes.sample.sizes) == {orphan.id: 300, xdoc.id: 40}
    every = replay_tool.changes_of(chosen, InferSettings(sample=(("", 5),)))
    assert every.sample is not None and set(every.sample.sizes) == {rule.id for rule in chosen.inference.rules}
    assert replay_tool.changes_of(chosen, InferSettings()) == inference.BuildChanges()
    alone = replay_tool.selected_ontology(ontology, InferSettings((xdoc.id,)))
    with pytest.raises(ValueError, match="--sample names"):
        replay_tool.changes_of(alone, InferSettings(sample=((orphan.id, 3),)))


@pytest.mark.parametrize(
    "flags",
    [["--floor", "1.5"], ["--batch", "1"], ["--sample", "x"], ["--sample", "INF-CONT-01=-2"]],
)
def test_bad_floor_test_flags_are_refused(flags: list[str]) -> None:
    with pytest.raises(SystemExit):
        replay_tool.parse_args(["s:p", "--step", "infer", "--dry-run", "--out", "o", *flags])


async def test_an_infer_replay_needs_an_executor(
    built: tuple[Path, str], tmp_path: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    state, build_id = built
    ontology = shipped_ontology()
    gateway = shop_gateway(scripted_gateway, ontology)
    with pytest.raises(ValueError, match="executor"):
        await replay(ReplayRequest(BuildSpec(state, PROJECT, build_id), "infer", tmp_path / "infer"), gateway, ontology)
    assert gateway.calls == []


def judge_counts(rules: Any) -> list[dict[str, Any]]:
    return [{key: rule[key] for key in JUDGE_COUNTS} for rule in rules]


@requires_live_graph()
async def test_a_dry_run_infer_replay_judges_as_the_build_did_and_writes_nothing(
    graph_project: GraphProject, tmp_path: Path, scripted_gateway: Callable[..., ScriptedGateway]
) -> None:
    builds = ShopV2Builds(graph_project, scripted_gateway, tmp_path / "source")
    result, _ = await builds.build(1)
    report = InferenceReport.model_validate(
        await builds.stores.builds.get_artifact(builds.project_id, result.build_id, "infer")
    )
    assert any(rule.judged for rule in report.rules)
    graph, state = await graph_project.snapshot(), tree_hash(builds.root)
    gateway = builds.gateway(1)
    spec = BuildSpec(builds.root, builds.project_id, result.build_id)

    metrics = await replay(
        ReplayRequest(spec, "infer", tmp_path / "dry-run"), gateway, builds.ontology, executor=graph_project.executor
    )

    assert await graph_project.snapshot() == graph
    assert tree_hash(builds.root) == state
    assert metrics["dry_run"] is True
    assert metrics["warnings"] == []
    replayed = InferenceReport.model_validate(json.loads((tmp_path / "dry-run" / "infer.json").read_text()))
    built_rules = [rule.model_dump(mode="json") for rule in report.rules]
    assert judge_counts(rule.model_dump(mode="json") for rule in replayed.rules) == judge_counts(built_rules)
    sides = metrics["replay"]
    assert judge_counts(sides.values()) == judge_counts(built_rules)
    calls = sum(side["calls"] for side in sides.values())
    assert calls == sum(side["judged"] for side in sides.values()) == len(gateway.calls) > 0
    assert all(side["input"]["uncached"] + side["input"]["cached"] > 0 for side in sides.values() if side["calls"])
