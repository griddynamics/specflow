"""The graph test probes: store selection, the probe contract, the canonical snapshot and the golden helpers.

The probe contract runs offline against ``MemoryProbe`` and, when a live graph store is selected
(``SPECFLOW_TEST_GRAPH_STORE`` or ``SPECFLOW_TEST_NEO4J=1``), against that store's probe: every probe
implementation must pass the same tests. The live golden parity is ``test_graph_golden_live.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

import graph_probes
from graph.stores.config import BACKENDS
from graph_probes import PROBES, open_probe, selected_backend, skip_reason, store_config_from_env
from graph_probes.base import (
    EdgeSpec,
    GraphProbe,
    NodeSpec,
    ProbeEdge,
    ProbeError,
    ProbeNode,
    _SpineWalk,
    canonical_props,
    identifier,
    select,
)
from graph_probes.golden import (
    GOLDEN_DIR,
    RANKINGS_FILE,
    compare_rankings,
    dump_json,
    load_rankings,
    load_snapshot,
    snapshot_differences,
    snapshot_path,
    write_rankings,
    write_snapshot,
)
from graph_probes.memory import MemoryProbe
from graph_probes.scenarios import SCENARIOS
from graph_support import PROJECT_PREFIX, live_llm_config, new_project_id, shipped_ontology

# A search vector of the dimension the live store's vector indexes hold (ArcadeDB refuses another length).
VECTOR = [0.25] * live_llm_config().embedding_dim

SWITCH_ENV = ("SPECFLOW_TEST_GRAPH_STORE", "SPECFLOW_TEST_NEO4J")
CONNECTION_ENV = (
    "GRAPH_STORE_URI",
    "GRAPH_STORE_USER",
    "GRAPH_STORE_PASSWORD",
    "GRAPH_STORE_NAME",
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
    "NEO4J_DATABASE",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (*SWITCH_ENV, *CONNECTION_ENV):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# Store selection -----------------------------------------------------------------------------------


def test_the_live_tier_is_off_without_a_switch(clean_env: pytest.MonkeyPatch) -> None:
    assert selected_backend() is None
    assert "SPECFLOW_TEST_GRAPH_STORE" in str(skip_reason())


def test_the_old_neo4j_switch_is_an_alias_of_neo4j(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SPECFLOW_TEST_NEO4J", "1")
    assert selected_backend() == "neo4j"
    assert skip_reason() is None


def test_the_switch_wins_over_the_alias(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SPECFLOW_TEST_NEO4J", "1")
    clean_env.setenv("SPECFLOW_TEST_GRAPH_STORE", "postgres")
    assert selected_backend() == "postgres"


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_store_is_selectable_and_skips_until_its_probe_exists(
    clean_env: pytest.MonkeyPatch, backend: str
) -> None:
    clean_env.setenv("SPECFLOW_TEST_GRAPH_STORE", backend.upper())
    assert selected_backend() == backend
    reason = skip_reason()
    if backend in PROBES:
        assert reason is None
    else:
        assert reason is not None and backend in reason and "no live probe" in reason


def test_a_mistyped_store_is_an_error_not_a_skip(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SPECFLOW_TEST_GRAPH_STORE", "neo4j5")
    with pytest.raises(ValueError, match="neo4j5"):
        selected_backend()


def test_a_store_without_a_probe_cannot_be_opened(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SPECFLOW_TEST_GRAPH_STORE", "postgres")
    clean_env.setenv("GRAPH_STORE_URI", "bolt://arcade.example.test:2480")
    clean_env.setenv("GRAPH_STORE_USER", "svc")
    with pytest.raises(RuntimeError, match="no live probe"):
        open_probe()


def test_neo4j_connection_defaults_and_neo4j_names(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SPECFLOW_TEST_NEO4J", "1")
    config = store_config_from_env()
    assert (config.backend, config.uri, config.user, config.name) == ("neo4j", "bolt://localhost:7687", "neo4j", None)
    clean_env.setenv("NEO4J_URI", "bolt://neo.example.test:7687")
    clean_env.setenv("NEO4J_USER", "reader")
    clean_env.setenv("NEO4J_PASSWORD", "legacy-pass")
    clean_env.setenv("NEO4J_DATABASE", "graph")
    config = store_config_from_env()
    assert (config.uri, config.user, config.name) == ("bolt://neo.example.test:7687", "reader", "graph")
    assert config.password.get_secret_value() == "legacy-pass"


def test_graph_store_names_win_over_neo4j_names(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SPECFLOW_TEST_GRAPH_STORE", "neo4j")
    clean_env.setenv("NEO4J_URI", "bolt://old.example.test:7687")
    clean_env.setenv("NEO4J_PASSWORD", "old-pass")
    clean_env.setenv("GRAPH_STORE_URI", "bolt://new.example.test:7687")
    clean_env.setenv("GRAPH_STORE_USER", "svc")
    clean_env.setenv("GRAPH_STORE_PASSWORD", "new-pass")
    clean_env.setenv("GRAPH_STORE_NAME", "specs")
    config = store_config_from_env()
    assert (config.uri, config.user, config.name) == ("bolt://new.example.test:7687", "svc", "specs")
    assert config.password.get_secret_value() == "new-pass"
    assert "new-pass" not in repr(config)


def test_other_stores_take_no_neo4j_fallback(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("NEO4J_URI", "bolt://old.example.test:7687")
    with pytest.raises(RuntimeError, match="GRAPH_STORE_URI"):
        store_config_from_env("postgres")
    clean_env.setenv("GRAPH_STORE_URI", "postgresql://pg.example.test/graph")
    clean_env.setenv("GRAPH_STORE_USER", "svc")
    config = store_config_from_env("postgres")
    assert (config.backend, config.uri, config.user) == ("postgres", "postgresql://pg.example.test/graph", "svc")


def test_the_registry_names_probe_classes() -> None:
    for backend, probe_class in PROBES.items():
        assert issubclass(probe_class, GraphProbe) and probe_class.backend == backend
    assert graph_probes.PROBES is PROBES


def test_identifiers_are_plain_names() -> None:
    assert identifier("PART_OF") == "PART_OF"
    for bad in ("", "a b", "X) OR 1=1 (n", "1abc", "a`b"):
        with pytest.raises(ValueError, match="graph identifier"):
            identifier(bad)


# The probe contract: the memory probe offline, the selected store's probe live ----------------------


@pytest.fixture(params=["memory", pytest.param("live", marks=graph_probes.requires_live_graph())])
async def probe(request: pytest.FixtureRequest) -> AsyncIterator[GraphProbe]:
    if request.param == "memory":
        yield MemoryProbe()
        return
    live = open_probe()
    try:
        yield live
    finally:
        await live.close()


@pytest_asyncio.fixture
async def project(probe: GraphProbe) -> AsyncIterator[str]:
    project_id = new_project_id()
    try:
        yield project_id
    finally:
        assert await probe.delete_projects([project_id]) == 0


async def test_created_nodes_carry_the_project_and_sorted_labels(probe: GraphProbe, project: str) -> None:
    assert (
        await probe.create_nodes(
            project,
            [
                NodeSpec(("SpecNode", "Module"), {"uuid": "u1", "name": "A"}),
                NodeSpec(("Fragment",), {"fragment_id": "f"}),
            ],
        )
        == 2
    )
    nodes = await probe.nodes(project)
    assert sorted(n.labels for n in nodes) == [("Fragment",), ("Module", "SpecNode")]
    assert all(n["group_id"] == project for n in nodes)
    assert [n.get("uuid") for n in await probe.nodes(project, "Module")] == ["u1"]
    assert await probe.nodes(project, "Module", uuid="other") == []
    assert await probe.count_nodes(project) == 2
    module = await probe.one_node(project, "Module")
    assert (module.kind, module.ref) == ("Module", "u1")  # no spec id: the uuid names it
    with pytest.raises(ProbeError):
        await probe.one_node(project)


async def test_property_contract_none_removes_lists_keep_order_scalars_keep_type(
    probe: GraphProbe, project: str
) -> None:
    values = {"s": "text", "i": 7, "f": 0.5, "b": True, "l": ["z", "a", "m"], "n": [3, 1, 2], "gone": None}
    await probe.create_nodes(project, [NodeSpec(("SpecNode", "Module"), {"uuid": "u", **values})])
    found = await probe.one_node(project, "Module")
    assert "gone" not in found.props
    assert found["l"] == ["z", "a", "m"] and found["n"] == [3, 1, 2]
    assert (type(found["s"]), type(found["i"]), type(found["f"]), type(found["b"])) == (str, int, float, bool)
    assert found["b"] is True and found["i"] == 7 and found["f"] == 0.5
    assert await probe.update_nodes(project, select("Module", uuid="u"), {"s": None, "i": 8, "extra": ["x"]}) == 1
    changed = await probe.one_node(project, "Module")
    assert "s" not in changed.props and changed["i"] == 8 and changed["extra"] == ["x"]
    assert all(not isinstance(v, dict) for v in changed.props.values())


async def test_a_map_valued_property_is_refused(probe: GraphProbe, project: str) -> None:
    with pytest.raises(ProbeError):
        await probe.create_nodes(project, [NodeSpec(("SpecNode", "Module"), {"uuid": "u", "bad": {"k": 1}})])
    await probe.create_nodes(
        project, [NodeSpec(("SpecNode", "Module"), {"uuid": "u"}), NodeSpec(("SpecNode", "Feature"), {"uuid": "f"})]
    )
    with pytest.raises(ProbeError):
        await probe.update_nodes(project, select("Module", uuid="u"), {"bad": [{"k": 1}]})
    with pytest.raises(ProbeError):
        await probe.create_edges(
            project, [EdgeSpec("PART_OF", select("Feature", uuid="f"), select("Module", uuid="u"), {"bad": {"k": 1}})]
        )
    assert await probe.count_nodes(project) == 2


async def test_labels_are_added_and_removed(probe: GraphProbe, project: str) -> None:
    await probe.create_nodes(project, [NodeSpec(("SpecNode", "Module"), {"uuid": "u"})])
    assert await probe.update_nodes(project, select("SpecNode", uuid="u"), add_labels=["Feature"]) == 1
    assert (await probe.one_node(project, "Feature")).labels == ("Feature", "Module", "SpecNode")
    await probe.update_nodes(project, select("SpecNode", uuid="u"), remove_labels=["Feature"])
    assert (await probe.one_node(project, "Module")).labels == ("Module", "SpecNode")
    assert await probe.update_nodes(project, select("SpecNode", uuid="none")) == 0


async def test_edges_join_one_node_to_one_node_and_are_read_with_their_ends(probe: GraphProbe, project: str) -> None:
    await probe.create_nodes(
        project,
        [
            NodeSpec(("SpecNode", "Module"), {"uuid": "m", "spec_id": "MOD::M", "embedding": VECTOR}),
            NodeSpec(("SpecNode", "Feature"), {"uuid": "f", "spec_id": "FEAT::F"}),
        ],
    )
    spec = EdgeSpec(
        "PART_OF",
        select("Feature", uuid="f"),
        select("Module", uuid="m"),
        {"group_id": project, "edge_key": "k", "basis": "stated", "source_fragment_ids": ["a", "b"], "gone": None},
    )
    assert await probe.create_edges(project, [spec]) == 1
    (edge,) = await probe.edges(project)
    assert (edge.type, edge.source["spec_id"], edge.target["spec_id"]) == ("PART_OF", "FEAT::F", "MOD::M")
    assert edge["source_fragment_ids"] == ["a", "b"] and "gone" not in edge.props
    assert "embedding" not in edge.target.props  # vectors are left off the ends of an edge
    assert len(await probe.edges(project, "PART_OF", edge_key="k")) == 1
    assert await probe.edges(project, "RELATES_TO") == []
    assert await probe.update_edges([edge.id], {"basis": "inferred", "edge_key": None}) == 1
    (changed,) = await probe.edges(project)
    assert changed["basis"] == "inferred" and "edge_key" not in changed.props
    assert (await probe.one_node(project, "Module"))["embedding"] == VECTOR  # a node read keeps them
    assert await probe.delete_edges([edge.id]) == 1
    assert await probe.edges(project) == []


async def test_an_edge_end_that_selects_nothing_or_two_nodes_is_an_error(probe: GraphProbe, project: str) -> None:
    await probe.create_nodes(
        project,
        [
            NodeSpec(("SpecNode", "Module"), {"uuid": "a", "k": 1}),
            NodeSpec(("SpecNode", "Module"), {"uuid": "b", "k": 1}),
        ],
    )
    with pytest.raises(ProbeError):
        await probe.create_edges(
            project, [EdgeSpec("PART_OF", select("Module", uuid="a"), select("Module", uuid="none"))]
        )
    with pytest.raises(ProbeError):
        await probe.create_edges(project, [EdgeSpec("PART_OF", select("Module", uuid="a"), select("Module", k=1))])


async def test_projects_are_isolated_and_deleting_a_node_takes_its_edges(probe: GraphProbe, project: str) -> None:
    other = new_project_id()
    try:
        for pid in (project, other):
            await probe.create_nodes(
                pid, [NodeSpec(("SpecNode", "Module"), {"uuid": "a"}), NodeSpec(("SpecNode", "Module"), {"uuid": "b"})]
            )
            await probe.create_edges(
                pid, [EdgeSpec("PART_OF", select("Module", uuid="a"), select("Module", uuid="b"), {"group_id": pid})]
            )
        assert await probe.count_nodes(project) == await probe.count_nodes(other) == 2
        assert len(await probe.edges(project)) == len(await probe.edges(other)) == 1
        assert await probe.delete_nodes(project, select("Module", uuid="a")) == 1
        assert await probe.edges(project) == [] and len(await probe.edges(other)) == 1
        assert await probe.update_nodes(project, select("Module", uuid="b"), {"x": 1}) == 1
        assert "x" not in (await probe.one_node(other, "Module", uuid="b")).props
    finally:
        assert await probe.delete_projects([other]) == 0


async def test_state_changes_with_anything_that_changes(probe: GraphProbe, project: str) -> None:
    await probe.create_nodes(
        project, [NodeSpec(("SpecNode", "Module"), {"uuid": "a"}), NodeSpec(("SpecNode", "Module"), {"uuid": "b"})]
    )
    before = await probe.state(project)
    assert await probe.state(project) == before
    await probe.update_nodes(project, select("Module", uuid="a"), {"x": 1})
    changed = await probe.state(project)
    assert changed != before
    await probe.create_edges(project, [EdgeSpec("PART_OF", select("Module", uuid="a"), select("Module", uuid="b"))])
    assert await probe.state(project) not in (before, changed)


async def test_outside_counts_see_what_is_not_a_test_project(probe: GraphProbe) -> None:
    foreign = "not-a-test-project-" + new_project_id()
    before = await probe.outside_counts(PROJECT_PREFIX)
    try:
        await probe.create_nodes(
            foreign, [NodeSpec(("SpecNode", "Module"), {"uuid": "x"}), NodeSpec(("Fragment",), {"fragment_id": "f"})]
        )
        after = await probe.outside_counts(PROJECT_PREFIX)
        assert after["foreign_spec_nodes"] == before["foreign_spec_nodes"] + 1
        assert after["foreign_fragments"] == before["foreign_fragments"] + 1
    finally:
        assert await probe.delete_projects([foreign]) == 0
    assert await probe.outside_counts(PROJECT_PREFIX) == before


# The canonical snapshot ------------------------------------------------------------------------------


async def seed_graph(probe: GraphProbe, project_id: str, *, reverse: bool = False, clock: str = "t0") -> None:
    nodes = [
        NodeSpec(
            ("SpecNode", "Module"),
            {
                "uuid": f"{project_id}-m",
                "spec_id": "MOD::M",
                "name": "M",
                "identity_key": "Module|m",
                "embedding": VECTOR,
                "created_build_id": clock,
                "last_build_id": clock,
                "created_at": clock,
                "statements": json.dumps([{"text": "m", "at": clock, "version": 1}]),
            },
        ),
        NodeSpec(
            ("SpecNode", "Feature"), {"uuid": f"{project_id}-f", "spec_id": "FEAT::F", "name": "F", "updated_at": clock}
        ),
        NodeSpec(("Fragment",), {"fragment_id": "frag-1", "spec_id": "FRG::1", "status": "active"}),
    ]
    if reverse:
        nodes.reverse()
    await probe.create_nodes(project_id, nodes)
    edges = [
        EdgeSpec(
            "PART_OF",
            select("Feature", spec_id="FEAT::F"),
            select("Module", spec_id="MOD::M"),
            {
                "group_id": project_id,
                "edge_key": "F|PART_OF|M",
                "uuid": f"{project_id}-e",
                "basis": "stated",
                "created_at": clock,
                "inferred_at": clock,
            },
        ),
        EdgeSpec(
            "REFERENCED_BY",
            select("Feature", spec_id="FEAT::F"),
            select("Fragment", fragment_id="frag-1"),
            {"group_id": project_id, "line_start": 1},
        ),
    ]
    if reverse:
        edges.reverse()
    await probe.create_edges(project_id, edges)


async def test_the_canonical_snapshot_ignores_ids_clocks_vectors_and_creation_order(probe: GraphProbe) -> None:
    first, second = new_project_id(), new_project_id()
    try:
        await seed_graph(probe, first, clock="2026-01-01")
        await seed_graph(probe, second, reverse=True, clock="2027-12-31")
        a, b = await probe.canonical_snapshot(first), await probe.canonical_snapshot(second)
        assert a == b
        assert dump_json(a) == dump_json(b)
        assert [n["spec_id"] for n in a["nodes"]] == sorted(n["spec_id"] for n in a["nodes"])
        module = next(n for n in a["nodes"] if n["spec_id"] == "MOD::M")
        assert module["labels"] == ["Module", "SpecNode"]
        assert module["props"] == {
            "identity_key": "Module|m",
            "name": "M",
            "statements": [{"text": "m", "version": 1}],
        }
        part_of = next(e for e in a["edges"] if e["type"] == "PART_OF")
        assert part_of == {
            "type": "PART_OF",
            "edge_key": "F|PART_OF|M",
            "source": "FEAT::F",
            "target": "MOD::M",
            "props": {"basis": "stated"},
        }
        assert {e["source"] for e in a["edges"]} == {"FEAT::F"} and "FRG::1" in {e["target"] for e in a["edges"]}
        # a real difference shows
        await probe.update_nodes(second, select("Module", spec_id="MOD::M"), {"name": "renamed"})
        assert await probe.canonical_snapshot(second) != a
    finally:
        await probe.delete_projects([first, second])


def test_canonical_props_drop_vectors_and_unstable_names_by_suffix() -> None:
    props = {
        "name": "n",
        "embedding": VECTOR,
        "name_embedding": VECTOR,
        "uuid": "u",
        "group_id": "g",
        "build_id": "b",
        "last_build_id": "b",
        "created_build_id": "b",
        "inferred_at": "t",
        "judge_model": "m",
        "statements": [1],
    }
    assert canonical_props(props) == {"name": "n", "judge_model": "m", "statements": [1]}
    assert canonical_props({"a": 1, "b": 2}, skip=("a",)) == {"b": 2}


async def test_spine_reachability_counts_sources_without_a_spine_path(probe: GraphProbe, project: str) -> None:
    ontology = shipped_ontology()
    await probe.create_nodes(
        project,
        [
            NodeSpec(("SpecNode", "Module"), {"uuid": "m"}),
            NodeSpec(("SpecNode", "Feature"), {"uuid": "f1"}),
            NodeSpec(("SpecNode", "Feature"), {"uuid": "f2"}),
            NodeSpec(("SpecNode", "FunctionalRequirement"), {"uuid": "r1"}),
            NodeSpec(("SpecNode", "FunctionalRequirement"), {"uuid": "r2"}),
        ],
    )
    assert await probe.spine_unreachable_count(project, ontology) == 2  # r1, r2 (a Feature is no spine source)

    def link(kind: str, a: str, b: str, **props: Any) -> EdgeSpec:
        return EdgeSpec(kind, select("SpecNode", uuid=a), select("SpecNode", uuid=b), {"group_id": project, **props})

    await probe.create_edges(project, [link("PART_OF", "f1", "m"), link("PART_OF", "r1", "f1", basis="inferred")])
    assert await probe.spine_unreachable_count(project, ontology) == 1  # r2
    assert await probe.spine_unreachable_count(project, ontology, bases=["stated"]) == 2  # r1's edge is inferred
    await probe.create_edges(project, [link("RELATES_TO", "m", "r2")])
    assert await probe.spine_unreachable_count(project, ontology) == 0


# The golden data -------------------------------------------------------------------------------------


def test_the_golden_files_exist_for_every_scenario_and_are_in_the_stable_format() -> None:
    assert GOLDEN_DIR.is_dir() and RANKINGS_FILE.is_file()
    for scenario in SCENARIOS:
        text = snapshot_path(scenario).read_text(encoding="utf-8")
        data = load_snapshot(scenario)
        assert data["scenario"] == scenario and data["nodes"] and data["edges"] and data["meta"]
        assert dump_json(data) == text, f"{scenario}.json is not what write_snapshot writes"
        for node in data["nodes"]:
            assert not {"embedding", "name_embedding", "uuid", "group_id"} & set(node["props"])
    assert dump_json(load_rankings(), pretty=3) == RANKINGS_FILE.read_text(encoding="utf-8")


def test_golden_snapshots_hold_no_project_id_or_clock() -> None:
    for scenario in SCENARIOS:
        text = snapshot_path(scenario).read_text(encoding="utf-8")
        assert PROJECT_PREFIX not in text
        assert "created_build_id" not in text and '"at"' not in text


def test_golden_data_is_written_from_neo4j_only(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graph_probes.golden.SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr("graph_probes.golden.RANKINGS_FILE", tmp_path / "rankings.json")
    for backend in ("arcadedb", "postgres", "tigergraph", "memory"):
        with pytest.raises(PermissionError):
            write_snapshot(backend, "build", {"nodes": [], "edges": []}, {})
        with pytest.raises(PermissionError):
            write_rankings(backend, {})
    path = write_snapshot("neo4j", "build", {"nodes": [], "edges": []}, {"k": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "scenario": "build",
        "meta": {"k": 1},
        "nodes": [],
        "edges": [],
    }


def snapshot(*nodes: dict[str, Any], edges: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    return {"nodes": list(nodes), "edges": list(edges)}


def graph_node(spec_id: str, **props: Any) -> dict[str, Any]:
    return {"labels": ["Module", "SpecNode"], "spec_id": spec_id, "props": props}


def test_snapshot_differences_name_missing_unexpected_and_changed_elements() -> None:
    edge = {"type": "PART_OF", "edge_key": "k", "source": "A", "target": "B", "props": {"basis": "stated"}}
    want = snapshot(graph_node("A", name="a"), graph_node("B"), edges=(edge,))
    assert snapshot_differences(want, want) == []
    have = snapshot(graph_node("A", name="changed"), graph_node("C"), edges=({**edge, "props": {"basis": "inferred"}},))
    assert snapshot_differences(want, have) == [
        "nodes: missing B|Module/SpecNode",
        "nodes: unexpected C|Module/SpecNode",
        "nodes: A|Module/SpecNode differs in ['name']",
        "edges: A|PART_OF|B|k differs in ['basis']",
    ]
    many = snapshot(*[graph_node(f"N{i}") for i in range(30)])
    lines = snapshot_differences(many, snapshot(), limit=3)
    assert len(lines) == 4 and lines[-1] == "... and 27 more"


def ranking(*ids: str, key: str = "spec_id") -> list[dict[str, str]]:
    return [{key: i} for i in ids]


def rankings(**modes: list[dict[str, str]]) -> dict[str, Any]:
    return {"queries": {"q": {"modes": list(modes), **modes}}}


def test_rankings_with_the_same_top_five_set_are_equal_up_to_rank_differences() -> None:
    want = rankings(fulltext=ranking("a", "b", "c", "d", "e", "f"))
    assert compare_rankings(want, want).ok and not compare_rankings(want, want).rank_differences
    reordered = rankings(fulltext=ranking("b", "a", "c", "d", "e", "g"))
    parity = compare_rankings(want, reordered)
    assert parity.ok and len(parity.rank_differences) == 1 and parity.queries == 1
    assert "fulltext 'q'" in parity.report()
    different = rankings(fulltext=ranking("a", "b", "c", "d", "x", "f"))
    parity = compare_rankings(want, different)
    assert not parity.ok and "['e', 'x']" in parity.set_mismatches[0]
    assert not compare_rankings(want, {"queries": {"other": {}}}).ok
    fragments = rankings(fragments=ranking("f1", "f2", key="fragment_id"))
    assert compare_rankings(fragments, fragments).ok


def test_the_golden_rankings_are_well_formed() -> None:
    data = load_rankings()
    assert data["limit"] == 10 and len(data["queries"]) >= 10
    for text, entry in data["queries"].items():
        assert {"fulltext", "fused_exact", "fragments"} <= set(entry["modes"]), text
        for mode in entry["modes"]:
            assert entry[mode], (text, mode)
            assert len(entry[mode]) <= data["limit"]
        if entry["near"]:
            assert "fused_index" in entry["modes"]
            assert {h["spec_id"] for h in entry["fused_index"]} <= set(entry["near"])
    assert compare_rankings(data, data).ok


def test_a_ranking_tolerance_lets_one_id_of_a_top_five_differ() -> None:
    def ranking(ids: list[str]) -> dict:
        return {"queries": {"q": {"modes": ["fulltext"], "fulltext": [{"spec_id": i, "score": 1.0} for i in ids]}}}

    golden = ranking(["a", "b", "c", "d", "e", "f"])
    swapped = ranking(["a", "b", "c", "d", "f", "x"])  # e left the top five, f came in: one id
    assert not compare_rankings(golden, swapped).ok
    tolerant = compare_rankings(golden, swapped, tolerance=1)
    assert tolerant.ok and len(tolerant.rank_differences) == 1
    two = ranking(["a", "b", "c", "x", "y", "z"])
    assert not compare_rankings(golden, two, tolerance=1).ok


def spine_node(identity: str, *labels: str) -> ProbeNode:
    return ProbeNode(identity, tuple(sorted(labels)), {})


@pytest.mark.parametrize(("middle_labels", "reaches"), [(("SpecNode", "Capability"), True), (("Capability",), False)])
def test_a_grouped_by_walk_needs_a_spec_node_in_the_middle(middle_labels: tuple[str, ...], *, reaches: bool) -> None:
    start, middle, module = spine_node("a", "SpecNode"), spine_node("b", *middle_labels), spine_node("c", "Module")
    edges = [
        ProbeEdge("1", "GROUPED_BY", start, middle, {}),
        ProbeEdge("2", "REALIZED_BY", middle, module, {}),
    ]
    assert _SpineWalk(edges).reaches_a_module(start) is reaches
