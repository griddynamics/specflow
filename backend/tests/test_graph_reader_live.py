"""Live graph-store tests of ``GraphReader`` search and traversal (plan 5.T1).

FR-READ-02..09 and m5-spec-edits H1-H7: fused search and its fallbacks, project scoping on the shared
indexes (paged fulltext, the exact-cosine and the index vector paths), query text handling, ``scope``
over the declared spine paths, ``impact`` and its truncation, ``nodes_for_fragment`` boundaries,
``find_fragments``, and reads without vectors. Graphs are written with raw Cypher (hand-written names
and vectors) or built from the shop corpus with the hand-written ``ShopAnswers`` (no model output).

Gate: ``SPECFLOW_TEST_GRAPH_STORE=<store>`` (or ``SPECFLOW_TEST_NEO4J=1``)
and ``uv run --env-file ../.env.local``. Every test works in its own
``pytest-graph-<hex>`` project ids; ``graph_project`` deletes them by ``group_id`` afterwards and
checks that nothing outside them changed.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from graph.build import BuildContext, BuildSettings, open_file_stores, run_build
from graph.core import ConfigurationError, GraphReader, LLMError, Ontology
from graph.core import reader as reader_module
from graph.core.queries import READ_NODES
from graph.core.reader import RRF_K, VECTOR_WEIGHT
from graph_probes import requires_live_graph
from graph_probes.base import EdgeSpec, NodeSpec, select
from graph_support import (
    GraphProject,
    ScriptedGateway,
    live_embedding_dim,
    live_llm_config,
    shop_answers,
    shop_corpus,
)

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, graph_project) live in graph_support.
pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()

MODULE = "Module"
FEATURE = "Feature"
CAPABILITY = "Capability"
FR = "FunctionalRequirement"
QR = "QualityRequirement"
FINDING = "Finding"  # a foreign (non-build) label
SEARCH_WORD = "zqxsaml"  # a search word no other data holds
ANSWERS = shop_answers(1)


# --- helpers -------------------------------------------------------------------------------------


class FixedGateway:
    """A gateway whose ``embed`` returns one hand-written vector; records the texts it got."""

    def __init__(self, vector: Sequence[float], *, model: str | None = None, fail: Exception | None = None) -> None:
        config = live_llm_config()
        self.config = config.model_copy(update={"embedding_model": model}) if model else config
        self.vector = [float(x) for x in vector]
        self.fail = fail
        self.texts: list[str] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        if self.fail is not None:
            raise self.fail
        return [self.vector for _ in texts]


def spy(reader: GraphReader) -> dict[str, int]:
    """Count the reader's queries by name."""

    counts: dict[str, int] = {}
    original = reader._executor.read

    async def read(query: Any, project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        counts[query.name] = counts.get(query.name, 0) + 1
        return await original(query, project_id, **params)

    reader._executor.read = read
    return counts


def flipped(key: str) -> str:
    """``key`` with every character mirrored, so uuids sort opposite to their ``spec_id``s."""

    return "".join(chr(0x2FFF - ord(char)) for char in key)


async def create(project: GraphProject, pid: str, label: str, rows: Sequence[Mapping[str, Any]]) -> None:
    """Spec nodes of ``label``: each row's ``key`` is its ``spec_id``; its uuid (``<pid>-`` and the key
    mirrored) sorts opposite to the ``spec_id``, so a tie-break by uuid would show. ``name`` defaults
    to the key; other row fields become properties."""

    await project.create_nodes(
        [
            NodeSpec(
                ("SpecNode", label),
                {
                    "uuid": f"{pid}-{flipped(row['key'])}",
                    "spec_id": row["key"],
                    "name": row.get("name") or row["key"],
                    **{k: v for k, v in row.items() if k not in ("key", "name")},
                },
            )
            for row in rows
        ],
        project_id=pid,
    )


async def link(project: GraphProject, pid: str, edges: Sequence[tuple[str, str, str]]) -> None:
    for source, relation, target in edges:
        await project.create_edges(
            [
                EdgeSpec(
                    relation,
                    select("SpecNode", spec_id=source),
                    select("SpecNode", spec_id=target),
                    {"group_id": pid, "edge_key": f"{source}|{target}"},
                )
            ],
            project_id=pid,
        )


def reader_of(project: GraphProject, gateway: Any = None) -> GraphReader:
    return project.reader(gateway=gateway)


def ids(nodes: Sequence[Any]) -> list[str]:
    return [node.spec_id for node in nodes]


# --- search fixtures ------------------------------------------------------------------------------

DIM = live_embedding_dim()
MODEL = live_llm_config().embedding_model
RNG = np.random.default_rng(20261023)
QUERY = RNG.standard_normal(DIM)
QUERY /= np.linalg.norm(QUERY)


def near(cosine: float) -> list[float]:
    """A unit vector at ``cosine`` to ``QUERY`` (deterministic per call order)."""

    other = RNG.standard_normal(DIM)
    other -= (other @ QUERY) * QUERY
    other /= np.linalg.norm(other)
    return [float(x) for x in cosine * QUERY + np.sqrt(1 - cosine * cosine) * other]


# V: vector rank 1, no text match. B: vector rank 2, fulltext rank 2. F: fulltext rank 1, far in
# vector space (below the vector floor). 20 fillers (cosine 0.80 down to 0.61, vector-only, above the
# floor) follow V and B in the vector list.
FUSION_VECTORS: dict[str, list[float]] = {
    "FUS::V": [float(x) for x in QUERY],
    "FUS::B": near(0.95),
    "FUS::F": near(0.0),
    **{f"FUS::FILL_{i:02d}": near(0.8 - i * 0.01) for i in range(20)},
}
FUSION_NAMES = {
    "FUS::V": "vector only node",
    "FUS::B": f"{SEARCH_WORD} login",
    "FUS::F": f"{SEARCH_WORD} federation {SEARCH_WORD} {SEARCH_WORD}",
}


async def fusion_project(project: GraphProject, pid: str | None = None) -> str:
    pid = pid or project.project_id
    rows = [
        {
            "key": key,
            "name": FUSION_NAMES.get(key, f"filler {key[-2:]}"),
            "statement_text": FUSION_NAMES.get(key, f"filler {key[-2:]}"),
            "embedding": vector,
            "embedding_model": MODEL,
            "is_assumption": "yes" if key == "FUS::B" else "no",
        }
        for key, vector in FUSION_VECTORS.items()
    ]
    await create(project, pid, FR, rows)
    await project.probe.await_indexes(60)
    return pid


async def crowd_project(project: GraphProject, count: int = 400) -> str:
    """Another project whose nodes all sit on the query vector and match the search word."""

    pid = project.new_project()
    await project.create_nodes(
        [
            NodeSpec(
                ("SpecNode", "FunctionalRequirement"),
                {
                    "uuid": f"{pid}-{i}",
                    "spec_id": f"CROWD::{i}",
                    "name": f"{SEARCH_WORD} crowd {i}",
                    "statement_text": f"{SEARCH_WORD} {SEARCH_WORD} {SEARCH_WORD}",
                    "embedding": [float(x) for x in QUERY],
                    "embedding_model": MODEL,
                },
            )
            for i in range(1, count + 1)
        ],
        project_id=pid,
    )
    await project.probe.await_indexes(60)
    return pid


# --- FR-READ-02, 03, 08 / H2, H5: fused search ---------------------------------------------------


async def test_node_hit_by_both_indexes_ranks_first(graph_project: GraphProject) -> None:
    pid = await fusion_project(graph_project)
    hits = await reader_of(graph_project, FixedGateway(QUERY)).find_nodes(pid, SEARCH_WORD, limit=3)
    assert [(h.node.spec_id, h.matched) for h in hits] == [
        ("FUS::B", "both"),
        ("FUS::V", "vector"),  # the vector list weighs double: vector rank 1 beats fulltext rank 1
        ("FUS::FILL_00", "vector"),
    ]
    assert hits[0].score == pytest.approx((VECTOR_WEIGHT + 1) / (RRF_K + 2))
    assert hits[1].score == pytest.approx(VECTOR_WEIGHT / (RRF_K + 1))
    assert hits[2].score == pytest.approx(VECTOR_WEIGHT / (RRF_K + 3))
    # Depth = limit: F (fulltext rank 1, 1 / 11) is below the third vector hit (2 / 13).
    hits = await reader_of(graph_project, FixedGateway(QUERY)).find_nodes(pid, SEARCH_WORD, limit=6)
    assert [h.node.spec_id for h in hits] == [
        "FUS::B",
        "FUS::V",
        "FUS::FILL_00",
        "FUS::FILL_01",
        "FUS::FILL_02",
        "FUS::FILL_03",
    ]


@pytest.mark.parametrize("gateway", [None, FixedGateway(QUERY, fail=LLMError("exhausted"))], ids=["none", "failing"])
async def test_without_a_working_gateway_search_is_fulltext_only(graph_project: GraphProject, gateway: Any) -> None:
    pid = await fusion_project(graph_project)
    hits = await reader_of(graph_project, gateway).find_nodes(pid, SEARCH_WORD, limit=3)
    assert [(h.node.spec_id, h.matched) for h in hits] == [("FUS::F", "fulltext"), ("FUS::B", "fulltext")]


async def test_embedding_model_mismatch_is_fulltext_only_without_an_embed_call(graph_project: GraphProject) -> None:
    pid = await fusion_project(graph_project)
    gateway = FixedGateway(QUERY, model="another/embedding-model")
    hits = await reader_of(graph_project, gateway).find_nodes(pid, SEARCH_WORD, limit=3)
    assert [(h.node.spec_id, h.matched) for h in hits] == [("FUS::F", "fulltext"), ("FUS::B", "fulltext")]
    assert gateway.texts == []


@pytest.mark.parametrize("exact_max", [2000, 0], ids=["exact", "index"])
async def test_vectors_of_another_embedding_model_are_never_compared(
    graph_project: GraphProject, monkeypatch: pytest.MonkeyPatch, exact_max: int
) -> None:
    pid = await fusion_project(graph_project)
    await create(
        graph_project,
        pid,
        FR,
        [{"key": "FUS::OTHER_MODEL", "embedding": [float(x) for x in QUERY], "embedding_model": "another/model"}],
    )
    await graph_project.probe.await_indexes(60)
    monkeypatch.setattr(reader_module, "EXACT_VECTOR_MAX", exact_max)
    hits = await reader_of(graph_project, FixedGateway(QUERY)).find_nodes(pid, SEARCH_WORD, limit=5)
    assert "FUS::OTHER_MODEL" not in ids([h.node for h in hits])
    assert ids([h.node for h in hits[:3]]) == ["FUS::B", "FUS::V", "FUS::FILL_00"]


async def test_candidate_vectors_are_counted_in_the_project_only(graph_project: GraphProject) -> None:
    pid, other = graph_project.project_id, graph_project.new_project()
    vector = [float(x) for x in QUERY]
    await create(
        graph_project,
        pid,
        FR,
        [
            {
                "key": "OWN",
                "name": SEARCH_WORD,
                "statement_text": SEARCH_WORD,
                "embedding": vector,
                "embedding_model": "another/model",
            }
        ],
    )
    await create(
        graph_project,
        other,
        FR,
        [{"key": "ELSEWHERE", "name": SEARCH_WORD, "embedding": vector, "embedding_model": MODEL}],
    )
    await graph_project.probe.await_indexes(60)
    gateway = FixedGateway(QUERY)
    hits = await reader_of(graph_project, gateway).find_nodes(pid, SEARCH_WORD, limit=3)
    assert [(h.node.spec_id, h.matched) for h in hits] == [("OWN", "fulltext")]
    assert gateway.texts == []  # the other project's vectors of the gateway's model do not count


async def test_find_nodes_filters_by_label_and_attribute(graph_project: GraphProject) -> None:
    pid = await fusion_project(graph_project)
    reader = reader_of(graph_project, FixedGateway(QUERY))
    assert ids([h.node for h in await reader.find_nodes(pid, SEARCH_WORD, where={"is_assumption": "yes"})]) == [
        "FUS::B"
    ]
    assert await reader.find_nodes(pid, SEARCH_WORD, labels=[MODULE]) == []
    with pytest.raises(ConfigurationError) as raised:
        await reader.find_nodes(pid, SEARCH_WORD, where={"no_such_attribute": "x"})
    assert raised.value.code == "unknown_attribute"


async def test_where_none_selects_nodes_without_the_property(graph_project: GraphProject) -> None:
    pid = graph_project.project_id
    await create(graph_project, pid, FR, [{"key": "W::SET", "acceptance_criteria": "x"}, {"key": "W::ABSENT"}])
    reader = reader_of(graph_project)
    assert ids(await reader.nodes(pid, where={"acceptance_criteria": None})) == ["W::ABSENT"]
    assert ids(await reader.nodes(pid, where={"acceptance_criteria": "x"})) == ["W::SET"]


async def test_search_never_returns_foreign_nodes_documents_or_fragments(graph_project: GraphProject) -> None:
    pid = await fusion_project(graph_project)
    text = f"{SEARCH_WORD} {SEARCH_WORD} {SEARCH_WORD} {SEARCH_WORD}"
    await graph_project.create_nodes(
        [
            NodeSpec(
                ("SpecNode", "Finding"),
                {
                    "uuid": f"{pid}-g",
                    "spec_id": "GAP::G",
                    "name": text,
                    "statement_text": text,
                    "embedding": [float(x) for x in QUERY],
                    "embedding_model": MODEL,
                },
            ),
            NodeSpec(("Document",), {"document_id": "d", "spec_id": "DOC::d", "title": text}),
            NodeSpec(("Fragment",), {"fragment_id": "f", "spec_id": "FRG::f", "status": "active", "text": text}),
        ],
        project_id=pid,
    )
    await graph_project.create_edges(
        [EdgeSpec("CONTAINS", select("Document", document_id="d"), select("Fragment", fragment_id="f"))], project_id=pid
    )
    await graph_project.probe.await_indexes(60)
    hits = await reader_of(graph_project, FixedGateway(QUERY)).find_nodes(pid, SEARCH_WORD, limit=50)
    assert {h.node.label for h in hits} == {FR}
    assert not {"GAP::G", "DOC::d", "FRG::f"} & {h.node.spec_id for h in hits}


# --- vector floor and stop words (M6 tuning) ----------------------------------------------------


@pytest.mark.parametrize("exact_max", [2000, 0], ids=["exact", "index"])
async def test_vector_floor_applies_in_the_same_scale_on_both_paths(
    graph_project: GraphProject, monkeypatch: pytest.MonkeyPatch, exact_max: int
) -> None:
    pid = graph_project.project_id
    rows = [
        {"key": "REL", "embedding": near(0.3), "embedding_model": MODEL},  # score 0.65
        {"key": "UNREL", "embedding": near(0.1), "embedding_model": MODEL},  # score 0.55
    ]
    await create(graph_project, pid, FR, rows)
    await graph_project.probe.await_indexes(60)
    monkeypatch.setattr(reader_module, "EXACT_VECTOR_MAX", exact_max)
    related = await reader_of(graph_project, FixedGateway(QUERY)).find_nodes(pid, "zqnomatch", limit=5)
    assert [(h.node.spec_id, h.matched) for h in related] == [("REL", "vector")]
    unrelated = np.random.default_rng(7).standard_normal(DIM)  # nearly orthogonal to both: about 0.5
    assert await reader_of(graph_project, FixedGateway(unrelated)).find_nodes(pid, "zqnomatch", limit=5) == []
    everything = GraphReader(
        graph_project.store,
        graph_project.ontology,
        FixedGateway(unrelated),
        vector_floor=0.0,
    )
    assert {h.node.spec_id for h in await everything.find_nodes(pid, "zqnomatch", limit=5)} == {"REL", "UNREL"}


async def test_vector_floor_is_validated(graph_project: GraphProject) -> None:
    for floor in (-0.1, 1.5, True):
        with pytest.raises(ConfigurationError) as raised:
            GraphReader(graph_project.store, graph_project.ontology, vector_floor=floor)
        assert raised.value.code == "invalid_vector_floor"


async def test_stop_words_are_left_out_of_fulltext_queries(graph_project: GraphProject) -> None:
    pid = graph_project.project_id
    await create(
        graph_project,
        pid,
        FR,
        [
            {
                "key": "CHECKOUT",
                "name": "Checkout recalculates totals",
                "statement_text": "Checkout recalculates totals",
            },
            {"key": "BASKET", "name": "The basket of the user", "statement_text": "The basket is in the session"},
        ],
    )
    await graph_project.create_nodes(
        [
            NodeSpec(("Document",), {"document_id": "d", "spec_id": "DOC::d", "path": "a.md"}),
            NodeSpec(
                ("Fragment",),
                {"fragment_id": "f", "document_id": "d", "status": "active", "text": "The basket of the user"},
            ),
        ],
        project_id=pid,
    )
    await graph_project.create_edges(
        [EdgeSpec("CONTAINS", select("Document", document_id="d"), select("Fragment", fragment_id="f"))], project_id=pid
    )
    reader = reader_of(graph_project)
    assert await reader.find_nodes(pid, "the") == []
    assert await reader.find_nodes(pid, "The AND of the") == []
    assert await reader.find_fragments(pid, "the") == []
    assert [(h.node.spec_id, h.matched) for h in await reader.find_nodes(pid, "the checkout")] == [
        ("CHECKOUT", "fulltext")
    ]


# --- H2: project scoping on the shared indexes ----------------------------------------------------


async def test_crowded_shared_indexes_return_only_own_hits_on_both_vector_paths(
    graph_project: GraphProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = await fusion_project(graph_project)
    await crowd_project(graph_project)
    expected = [("FUS::B", "both"), ("FUS::V", "vector"), ("FUS::FILL_00", "vector")]

    exact = reader_of(graph_project, FixedGateway(QUERY))
    exact_counts = spy(exact)
    assert [(h.node.spec_id, h.matched) for h in await exact.find_nodes(pid, SEARCH_WORD, limit=3)] == expected
    assert exact_counts.get("search_nodes_vector", 0) == 0 and exact_counts["search_nodes_vector_exact"] == 1

    monkeypatch.setattr(reader_module, "EXACT_VECTOR_MAX", 0)  # 23 candidates: force the index path
    for limit in (3, 5, 20):
        index = reader_of(graph_project, FixedGateway(QUERY))
        counts = spy(index)
        hits = await index.find_nodes(pid, SEARCH_WORD, limit=limit)
        assert counts["search_nodes_vector"] >= 1
        assert all(h.node.uuid.startswith(pid) for h in hits)
        assert len(hits) == min(limit, len(FUSION_VECTORS))
        # The approximate (quantised) index may swap fillers 0.01 apart; B and V are clear.
        assert [(h.node.spec_id, h.matched) for h in hits[:2]] == expected[:2]


async def test_fulltext_pages_past_another_projects_matches(graph_project: GraphProject) -> None:
    pid = await fusion_project(graph_project)
    await crowd_project(graph_project)  # 400 better-scoring matches of the word elsewhere
    reader = reader_of(graph_project)
    counts = spy(reader)
    # Only B passes the filter: the pages go past the 400 matches elsewhere and F.
    hits = await reader.find_nodes(pid, SEARCH_WORD, where={"is_assumption": "yes"}, limit=1)
    assert [(h.node.spec_id, h.matched) for h in hits] == [("FUS::B", "fulltext")]
    if graph_project.probe.lucene_semantics:  # database-wide indexes need several pages
        assert counts["search_nodes_fulltext"] > 1
    else:  # a store that scores per project returns the page already filtered: one page is enough
        assert counts["search_nodes_fulltext"] == 1


async def test_vector_index_path_above_2000_candidates(graph_project: GraphProject) -> None:
    pid = graph_project.project_id
    # 2,100 random vectors (components uniform in [-0.5, 0.5), seeded; nearly orthogonal to the query) and one target.
    noise = np.random.default_rng(2100).uniform(-0.5, 0.5, size=(2100, DIM))
    await graph_project.create_nodes(
        [
            NodeSpec(
                ("SpecNode", "FunctionalRequirement"),
                {
                    "uuid": f"{pid}-{i}",
                    "spec_id": f"BIG::{i}",
                    "name": f"bulk {i}",
                    "embedding": [float(x) for x in noise[i - 1]],
                    "embedding_model": MODEL,
                },
            )
            for i in range(1, 2101)
        ],
        project_id=pid,
    )
    await create(
        graph_project,
        pid,
        FR,
        [
            {
                "key": "BIG::TARGET",
                "name": f"{SEARCH_WORD} target",
                "statement_text": SEARCH_WORD,
                "embedding": [float(x) for x in QUERY],
                "embedding_model": MODEL,
            }
        ],
    )
    await graph_project.probe.await_indexes(120)
    reader = reader_of(graph_project, FixedGateway(QUERY))
    counts = spy(reader)
    hits = await reader.find_nodes(pid, SEARCH_WORD, limit=3)
    assert counts["search_nodes_vector"] >= 1
    # The bulk vectors are near cosine 0 (score about 0.5): vector-only hits below the floor are gone.
    assert [(h.node.spec_id, h.matched) for h in hits] == [("BIG::TARGET", "both")]


# --- H3: query text ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        ",".join(["w"] * 1100),
        " ".join(["a-b-c-d-e"] * 256),
        "中" * 2000,
        " ".join([SEARCH_WORD] * 3000),
        'FR-ADM-007 && || ! ^ ~ * ? : \\ / "x',
        f"{SEARCH_WORD} AND (login",
        "NOT OR TO",
        "???",
    ],
    ids=["commas", "hyphens", "cjk", "repeated", "operators", "unbalanced", "keywords", "only-syntax"],
)
async def test_punctuated_and_long_queries_search_without_error(graph_project: GraphProject, text: str) -> None:
    pid = await fusion_project(graph_project)
    gateway = FixedGateway(QUERY)
    hits = await reader_of(graph_project, gateway).find_nodes(pid, text, limit=3)
    assert len(hits) == 3  # the vector list always has the project's nodes
    assert len(gateway.texts[0]) <= reader_module.MAX_QUERY_CHARS
    assert isinstance(await reader_of(graph_project).find_fragments(pid, text), list)


@pytest.mark.parametrize("method", ["find_nodes", "find_fragments"])
async def test_text_lucene_rejects_raises_query_too_long(
    graph_project: GraphProject, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    monkeypatch.setattr(reader_module, "_lucene_query", lambda _text: " ".join(["w"] * 1100))
    if not graph_project.probe.lucene_semantics:  # no clause limit: the store answers a long term list, no match here
        assert await getattr(reader_of(graph_project), method)(graph_project.project_id, "x") == []
        return
    with pytest.raises(ConfigurationError) as raised:
        await getattr(reader_of(graph_project), method)(graph_project.project_id, "x")
    assert raised.value.code == "query_too_long"


@pytest.mark.parametrize("limit", [0, 501])
async def test_search_limits_are_capped(graph_project: GraphProject, limit: int) -> None:
    for method in ("find_nodes", "find_fragments"):
        with pytest.raises(ConfigurationError) as raised:
            await getattr(reader_of(graph_project), method)(graph_project.project_id, SEARCH_WORD, limit=limit)
        assert raised.value.code == "invalid_limit"


async def test_limit_500_returns_every_node_of_a_small_project(graph_project: GraphProject) -> None:
    pid = await fusion_project(graph_project)
    hits = await reader_of(graph_project, FixedGateway(QUERY)).find_nodes(pid, SEARCH_WORD, limit=500)
    assert len(hits) == len(FUSION_VECTORS)


# --- FR-READ-07 / H1: scope ----------------------------------------------------------------------


async def scope_graph(project: GraphProject, pid: str) -> None:
    await create(project, pid, MODULE, [{"key": key} for key in ("M", "MP", "M2")])
    await create(project, pid, FEATURE, [{"key": key} for key in ("F", "F2", "F3")])
    await create(project, pid, CAPABILITY, [{"key": "C"}])
    await create(project, pid, FR, [{"key": key} for key in ("R1", "R2", "R3", "R4", "R6")])
    await link(
        project,
        pid,
        [
            ("F", "PART_OF", "M"),
            ("R1", "PART_OF", "F"),
            ("R2", "PART_OF", "F"),
            ("C", "REALIZED_BY", "M"),
            ("R3", "GROUPED_BY", "C"),
            ("M", "PART_OF", "MP"),
            ("M2", "INTEGRATES_WITH", "M"),
            ("F2", "PART_OF", "F"),
            ("F3", "PART_OF", "F2"),
            ("R6", "PART_OF", "F3"),  # 4 PART_OF hops from M
        ],
    )
    await project.create_nodes(
        [
            NodeSpec(("SpecNode", "Finding"), {"uuid": f"{pid}-x", "spec_id": "GAP::X", "name": "x"}),
            NodeSpec(("Finding",), {"spec_id": "GAP::Y"}),
            NodeSpec(("Fragment",), {"fragment_id": "f"}),
        ],
        project_id=pid,
    )
    module = select("SpecNode", spec_id="M")
    await project.create_edges(
        [
            EdgeSpec("PART_OF", select("Finding", spec_id="GAP::X"), module, {"group_id": pid}),
            EdgeSpec("FLAGS", select("Finding", spec_id="GAP::Y"), module, {"group_id": pid}),
            EdgeSpec("REFERENCED_BY", module, select("Fragment", fragment_id="f"), {"line_start": 1, "line_end": 2}),
        ],
        project_id=pid,
    )


async def test_scope_of_a_module_is_its_spine_paths(graph_project: GraphProject) -> None:
    pid = graph_project.project_id
    await scope_graph(graph_project, pid)
    reader = reader_of(graph_project)
    # FR-READ-07: the feature, the capability and the 3 requirements; F2, F3 by 2-3 PART_OF hops.
    assert ids(await reader.scope(pid, "M")) == ["C", "F", "F2", "F3", "R1", "R2", "R3"]
    await link(graph_project, pid, [("M", "RELATES_TO", "R4")])  # symmetric, stored from the module
    assert ids(await reader.scope(pid, "M")) == ["C", "F", "F2", "F3", "R1", "R2", "R3", "R4"]
    assert ids(await reader.scope(pid, "M", labels=[FR])) == ["R1", "R2", "R3", "R4"]
    assert await reader.scope(pid, "NOPE") == []


async def test_scope_takes_every_part_of_a_sequence_path(graph_project: GraphProject) -> None:
    pid = graph_project.project_id
    await scope_graph(graph_project, pid)
    reader = reader_of(graph_project)
    assert ids(await reader.scope(pid, "C")) == ["R3"]  # GROUPED_BY alone
    assert "C" in ids(await reader.scope(pid, "M"))  # REALIZED_BY alone


async def test_scope_never_reaches_other_modules_or_passes_through_a_root(graph_project: GraphProject) -> None:
    pid = graph_project.project_id
    await create(graph_project, pid, MODULE, [{"key": key} for key in ("M1", "M2", "SM")])
    await create(graph_project, pid, QR, [{"key": "RSYS"}])
    await create(graph_project, pid, FEATURE, [{"key": "F1"}, {"key": "F2"}])
    await create(graph_project, pid, FR, [{"key": "RF2"}, {"key": "RSM"}])
    await link(
        graph_project,
        pid,
        [
            ("M1", "RELATES_TO", "RSYS"),  # a system-wide requirement of both modules
            ("RSYS", "RELATES_TO", "M2"),
            ("F2", "PART_OF", "M2"),
            ("RF2", "PART_OF", "F2"),
            ("F1", "PART_OF", "M1"),
            ("SM", "PART_OF", "M1"),  # a sub-module
            ("RSM", "PART_OF", "SM"),
        ],
    )
    reader = reader_of(graph_project)
    assert ids(await reader.scope(pid, "M1")) == ["F1", "RSYS", "SM"]
    assert ids(await reader.scope(pid, "M2")) == ["F2", "RF2", "RSYS"]


async def test_scope_is_project_scoped(graph_project: GraphProject) -> None:
    pid, other = graph_project.project_id, graph_project.new_project()
    await scope_graph(graph_project, pid)
    await create(graph_project, other, MODULE, [{"key": "M"}])
    await create(graph_project, other, FR, [{"key": "R9"}])
    await link(graph_project, other, [("R9", "PART_OF", "M")])
    assert ids(await reader_of(graph_project).scope(other, "M")) == ["R9"]


# --- FR-READ-05 / H5: impact ---------------------------------------------------------------------


@pytest.mark.parametrize("reachable", [500, 501, 600])
async def test_impact_returns_at_most_500_and_flags_truncation(graph_project: GraphProject, reachable: int) -> None:
    pid = graph_project.project_id
    await graph_project.create_nodes(
        [
            NodeSpec(
                ("SpecNode", "FunctionalRequirement"),
                {"uuid": f"{pid}-H", "spec_id": "H", "name": "H", "spec_id_aliases": ["OLD_H"]},
            ),
            *[
                NodeSpec(
                    ("SpecNode", "FunctionalRequirement"),
                    {"uuid": f"{pid}-{i}", "spec_id": f"N{i:03d}", "name": "n"},
                )
                for i in range(1, reachable + 1)
            ],
        ],
        project_id=pid,
    )
    await graph_project.create_edges(
        [
            EdgeSpec(
                "RELATES_TO",
                select("SpecNode", spec_id="H"),
                select("SpecNode", spec_id=f"N{i:03d}"),
                {"group_id": pid, "edge_key": str(i)},
            )
            for i in range(1, reachable + 1)
        ],
        project_id=pid,
    )
    reader = reader_of(graph_project)
    result = await reader.impact(pid, ["H"], depth=2)
    assert len(result.nodes) == 500
    assert result.truncated is (reachable > 500)
    assert (result.nodes[0].spec_id, result.nodes[-1].spec_id) == ("N001", "N500")
    aliased = await reader.impact(pid, ["OLD_H"], depth=1)
    assert aliased.sources == ("H",) and len(aliased.nodes) == 500


async def test_impact_is_nearest_first_in_both_directions(graph_project: GraphProject) -> None:
    pid = graph_project.project_id
    await create(graph_project, pid, FEATURE, [{"key": f"A{i}"} for i in range(5)])
    await create(graph_project, pid, FR, [{"key": "B1"}])
    await link(graph_project, pid, [(f"A{i}", "PART_OF", f"A{i + 1}") for i in range(4)] + [("B1", "RELATES_TO", "A1")])
    await graph_project.create_nodes(
        [
            NodeSpec(("Fragment",), {"fragment_id": "f"}),
            NodeSpec(("SpecNode", "Finding"), {"uuid": f"{pid}-g", "spec_id": "GAP::G", "name": "g"}),
        ],
        project_id=pid,
    )
    a1 = select("SpecNode", spec_id="A1")
    await graph_project.create_edges(
        [
            EdgeSpec("REFERENCED_BY", a1, select("Fragment", fragment_id="f")),
            EdgeSpec("RELATES_TO", select("Finding", spec_id="GAP::G"), a1, {"group_id": pid}),
        ],
        project_id=pid,
    )
    reader = reader_of(graph_project)
    result = await reader.impact(pid, ["A2"], depth=2)
    assert [(n.spec_id, result.distance[n.spec_id]) for n in result.nodes] == [
        ("A1", 1),
        ("A3", 1),
        ("A0", 2),
        ("A4", 2),
        ("B1", 2),
    ]
    assert not result.truncated and result.sources == ("A2",)
    assert ids((await reader.impact(pid, ["A2"], depth=2, relations=["PART_OF"])).nodes) == ["A1", "A3", "A0", "A4"]
    assert ids((await reader.impact(pid, ["A0", "A4"], depth=1)).nodes) == ["A1", "A3"]
    assert (await reader.impact(pid, ["NOPE"])).nodes == ()


# --- FR-READ-04 / H6: nodes_for_fragment ---------------------------------------------------------


async def test_nodes_for_fragment_overlaps_evidence_lines_of_active_fragments(graph_project: GraphProject) -> None:
    pid = graph_project.project_id

    def fragment(fragment_id: str, document_id: str, status: str, start: int, end: int) -> NodeSpec:
        return NodeSpec(
            ("Fragment",),
            {
                "fragment_id": fragment_id,
                "document_id": document_id,
                "status": status,
                "line_start": start,
                "line_end": end,
            },
        )

    def spec(uuid: str, spec_id: str, name: str, label: str = "FunctionalRequirement") -> NodeSpec:
        return NodeSpec(("SpecNode", label), {"uuid": f"{pid}-{uuid}", "spec_id": spec_id, "name": name})

    await graph_project.create_nodes(
        [
            NodeSpec(("Document",), {"document_id": "doc1", "spec_id": "DOC::doc1", "path": "a.md"}),
            NodeSpec(("Document",), {"document_id": "doc2", "spec_id": "DOC::doc2", "path": "b.md"}),
            fragment("f1", "doc1", "active", 5, 20),
            fragment("fs", "doc1", "skipped", 30, 40),
            fragment("f0", "doc1", "retired", 5, 20),
            fragment("f2", "doc2", "active", 1, 30),
            spec("n", "N", "n"),
            spec("s", "S", "s"),
            spec("r", "RETIRED", "r"),
            spec("o", "OTHER", "o"),
            spec("g", "GAP::F", "g", "Finding"),
        ],
        project_id=pid,
    )
    doc1, doc2 = select("Document", document_id="doc1"), select("Document", document_id="doc2")
    spans = {"line_start": 10, "line_end": 12}
    await graph_project.create_edges(
        [
            EdgeSpec("CONTAINS", doc1, select("Fragment", fragment_id="f1")),
            EdgeSpec("CONTAINS", doc1, select("Fragment", fragment_id="fs")),
            EdgeSpec("CONTAINS", doc1, select("Fragment", fragment_id="f0")),
            EdgeSpec("CONTAINS", doc2, select("Fragment", fragment_id="f2")),
            EdgeSpec("REFERENCED_BY", select("SpecNode", spec_id="N"), select("Fragment", fragment_id="f1"), spans),
            EdgeSpec("REFERENCED_BY", select("SpecNode", spec_id="S"), select("Fragment", fragment_id="fs")),
            EdgeSpec(
                "REFERENCED_BY", select("SpecNode", spec_id="RETIRED"), select("Fragment", fragment_id="f0"), spans
            ),
            EdgeSpec("REFERENCED_BY", select("SpecNode", spec_id="OTHER"), select("Fragment", fragment_id="f2"), spans),
            EdgeSpec(
                "REFERENCED_BY", select("SpecNode", spec_id="GAP::F"), select("Fragment", fragment_id="f1"), spans
            ),
        ],
        project_id=pid,
    )
    reader = reader_of(graph_project)

    async def lines(start: int, end: int) -> list[str]:
        return ids(await reader.nodes_for_fragment(pid, "doc1", start, end))

    assert await lines(12, 20) == ["N"]
    assert await lines(13, 20) == []
    assert await lines(1, 10) == ["N"]
    assert await lines(1, 9) == []
    assert await lines(11, 11) == ["N"]
    assert await lines(35, 35) == ["S"]  # no relationship lines: the fragment's lines
    assert await lines(1, 100) == ["N", "S"]
    with pytest.raises(ConfigurationError):
        await reader.nodes_for_fragment(pid, "doc1", 5, 4)


# --- FR-READ-09: find_fragments ------------------------------------------------------------------


async def fragment_corpus(project: GraphProject, pid: str) -> None:
    t = SEARCH_WORD.upper()

    def fragment(fragment_id: str, document_id: str, status: str, text: str) -> NodeSpec:
        return NodeSpec(
            ("Fragment",),
            {"fragment_id": fragment_id, "document_id": document_id, "status": status, "text": text},
        )

    await project.create_nodes(
        [
            NodeSpec(
                ("Document",),
                {"document_id": "d1", "spec_id": "DOC::d1", "path": "auth.md", "status": "active"},
            ),
            NodeSpec(
                ("Document",),
                {"document_id": "d2", "spec_id": "DOC::d2", "path": "other.md", "status": "active"},
            ),
            fragment("a", "d1", "active", f"Users sign in with {t} single sign-on."),
            fragment("r", "d1", "retired", f"Old {t} text."),
            fragment("s", "d1", "skipped", f"Skipped {t} text."),
            fragment("b", "d2", "active", f"{t} {t} metadata."),
        ],
        project_id=pid,
    )
    d1, d2 = select("Document", document_id="d1"), select("Document", document_id="d2")
    await project.create_edges(
        [
            EdgeSpec("CONTAINS", d1, select("Fragment", fragment_id="a")),
            EdgeSpec("CONTAINS", d1, select("Fragment", fragment_id="r")),
            EdgeSpec("CONTAINS", d1, select("Fragment", fragment_id="s")),
            EdgeSpec("CONTAINS", d2, select("Fragment", fragment_id="b")),
        ],
        project_id=pid,
    )


async def test_find_fragments_returns_active_fragments_of_the_project(graph_project: GraphProject) -> None:
    pid, other = graph_project.project_id, graph_project.new_project()
    await fragment_corpus(graph_project, pid)
    await fragment_corpus(graph_project, other)
    reader = reader_of(graph_project)
    hits = await reader.find_fragments(pid, SEARCH_WORD)
    assert [(h.fragment.fragment_id, h.document.path if h.document else None) for h in hits] == [
        ("b", "other.md"),
        ("a", "auth.md"),
    ]
    assert hits[0].score >= hits[1].score > 0
    assert all(h.fragment.status == "active" for h in hits)
    assert [h.fragment.fragment_id for h in await reader.find_fragments(pid, SEARCH_WORD, document_id="d1")] == ["a"]
    assert len(await reader.find_fragments(pid, SEARCH_WORD, limit=1)) == 1
    assert (await reader.find_fragments(pid, f'{SEARCH_WORD} "single sign-on" (AND) ~?'))[0].fragment.fragment_id == "a"


async def test_find_fragments_pages_past_many_matches_elsewhere(graph_project: GraphProject) -> None:
    pid, crowd = graph_project.project_id, graph_project.new_project()
    await graph_project.create_nodes(
        [
            NodeSpec(
                ("Fragment",),
                {
                    "fragment_id": str(i),
                    "document_id": "d",
                    "status": "active",
                    "text": f"zqcommon zqcommon zqcommon {i}",
                },
            )
            for i in range(1, 1501)
        ],
        project_id=crowd,
    )
    await graph_project.create_nodes(
        [
            NodeSpec(("Document",), {"document_id": "d1", "spec_id": "DOC::d1", "path": "a.md"}),
            NodeSpec(
                ("Fragment",),
                {
                    "fragment_id": "mine",
                    "document_id": "d1",
                    "status": "active",
                    "text": "one zqcommon here, among many other words of a longer fragment",
                },
            ),
        ],
        project_id=pid,
    )
    await graph_project.create_edges(
        [EdgeSpec("CONTAINS", select("Document", document_id="d1"), select("Fragment", fragment_id="mine"))],
        project_id=pid,
    )
    reader = reader_of(graph_project)
    counts = spy(reader)
    assert [h.fragment.fragment_id for h in await reader.find_fragments(pid, "zqcommon", limit=5)] == ["mine"]
    if graph_project.probe.lucene_semantics:
        assert counts["search_fragments"] > 1
    else:
        assert counts["search_fragments"] == 1


# --- shop graph: independent walks, two identical projects, no vectors (FR-READ-04..07, H7) ------


async def build(project: GraphProject, gateway: ScriptedGateway, root: Path, pid: str) -> None:
    stores = open_file_stores(root)
    ctx = BuildContext(
        project_id=pid,
        ontology=project.ontology,
        driver=project.store,
        gateway=gateway,
        registry=stores.registry,
        corpus=shop_corpus(1, root),
        extraction=stores.extraction,
        builds=stores.builds,
        options=ANSWERS.options,
        settings=BuildSettings(inference=False),
    )
    await run_build(ctx)


def sequence_reach(steps: Sequence[str], target: str, back: Callable[..., set[str]]) -> set[str]:
    """The nodes reaching ``target`` over any contiguous part of the relation sequence ``steps``."""

    reach: set[str] = set()
    for start in range(len(steps)):
        for end in range(start + 1, len(steps) + 1):
            frontier = {target}
            for depth, relation in enumerate(reversed(steps[start:end])):
                frontier = back(frontier, [relation], first=depth == 0)
            reach |= frontier
    return reach


def spine_scope(ontology: Ontology, edges: Sequence[Any], labels: Mapping[str, str], target: str) -> set[str]:
    """``scope`` computed in memory from the ontology's spine paths (H1)."""

    roots = ontology.spine.roots
    into: dict[str, dict[str, set[str]]] = {}
    for edge in edges:
        into.setdefault(edge.relation, {}).setdefault(edge.target, set()).add(edge.source)
        if ontology.is_symmetric(edge.relation):
            into[edge.relation].setdefault(edge.source, set()).add(edge.target)

    def back(frontier: set[str], relations: Sequence[str], *, first: bool) -> set[str]:
        return {
            source
            for node in frontier
            if first or labels.get(node) not in roots
            for relation in relations
            for source in into.get(relation, {}).get(node, set())
        }

    reach: set[str] = set()
    for path in ontology.spine.paths:
        if path.sequence:
            reach |= sequence_reach(list(path.relations), target, back)
        else:
            frontier = {target}
            for hop in range(1, path.max_hops + 1):
                frontier = back(frontier, path.relations, first=hop == 1)
                if hop >= path.min_hops:
                    reach |= frontier
    reach.discard(target)
    return reach


def bfs(edges: Sequence[Any], start: str, depth: int) -> dict[str, int]:
    around: dict[str, set[str]] = {}
    for edge in edges:
        around.setdefault(edge.source, set()).add(edge.target)
        around.setdefault(edge.target, set()).add(edge.source)
    distance, queue = {start: 0}, deque([start])
    while queue:
        node = queue.popleft()
        if distance[node] == depth:
            continue
        for other in around.get(node, ()):
            if other not in distance:
                distance[other] = distance[node] + 1
                queue.append(other)
    distance.pop(start)
    return distance


async def test_shop_reads_match_independent_computations(
    graph_project: GraphProject, scripted_gateway: Any, tmp_path: Path
) -> None:
    pid, twin = graph_project.project_id, graph_project.new_project()
    await build(graph_project, ANSWERS.install(scripted_gateway(live_llm_config())), tmp_path / "a", pid)
    await build(graph_project, ANSWERS.install(scripted_gateway(live_llm_config())), tmp_path / "b", twin)
    gateway = ANSWERS.install(scripted_gateway(live_llm_config()))
    reader = reader_of(graph_project, gateway)
    nodes = await reader.nodes(pid)
    edges = await reader.edges(pid)
    labels = {node.spec_id: node.label for node in nodes}
    own = {node.uuid for node in nodes}
    twin_uuids = {node.uuid for node in await reader.nodes(twin)}
    assert len(nodes) > 30 and not own & twin_uuids

    for node in nodes:
        assert set(ids(await reader.scope(pid, node.spec_id))) == spine_scope(
            graph_project.ontology, edges, labels, node.spec_id
        ), node.spec_id
        expected = bfs(edges, node.spec_id, 3)
        result = await reader.impact(pid, [node.spec_id], depth=3)
        assert result.distance == expected, node.spec_id
        assert ids(result.nodes) == sorted(expected, key=lambda spec_id: (expected[spec_id], spec_id))

    for document in await reader.documents(pid):
        for start, end in ((1, 5), (6, 15), (10, 40), (1, 10_000)):
            expected_ids = sorted(
                node.spec_id
                for node in nodes
                if any(
                    item.document_id == document.document_id
                    and item.line_start is not None
                    and item.line_end is not None
                    and item.line_start <= end
                    and item.line_end >= start
                    for item in node.provenance
                )
            )
            assert ids(await reader.nodes_for_fragment(pid, document.document_id, start, end)) == expected_ids

    module = next(node for node in nodes if node.label == MODULE)
    hits = await reader.find_nodes(pid, module.name, limit=10)
    assert hits and all(h.node.uuid in own for h in hits)
    assert hits[0].node.spec_id == module.spec_id
    assert all(h.node.label == MODULE for h in await reader.find_nodes(pid, module.name, labels=[MODULE]))


async def test_reads_never_carry_vectors(graph_project: GraphProject, scripted_gateway: Any, tmp_path: Path) -> None:
    pid = graph_project.project_id
    await build(graph_project, ANSWERS.install(scripted_gateway(live_llm_config())), tmp_path / "a", pid)
    stored = [
        {
            "n": len(
                [
                    n
                    for n in await graph_project.nodes("SpecNode", project_id=pid)
                    if n.get("embedding") is not None and n.get("name_embedding") is not None
                ]
            )
        }
    ]
    reader = reader_of(graph_project)
    rows = await reader._executor.read(READ_NODES, pid, labels=list(reader._spec_labels), where={}, retired="retired")
    assert stored[0]["n"] == len(rows) > 0
    assert all(row["props"]["embedding"] is None and row["props"]["name_embedding"] is None for row in rows)
    for node in await reader.nodes(pid):
        assert not {"embedding", "name_embedding", "embedding_model"} & set(node.attributes)
        assert "embedding" not in node.model_dump_json()


# --- explicit ids and spec_ids in the query (scale-report F8) ------------------------------------


def id_key(project: GraphProject, explicit: str) -> str:
    ontology = project.ontology
    label = ontology.label_for_id_family(explicit)
    assert label is not None
    return f"{ontology.identity_rule(label).scope}|id|{explicit}"


async def id_project(project: GraphProject, pid: str) -> None:
    """Requirements with explicit ids, and many nodes that only mention them (they win fused search)."""

    prefix = project.ontology.id_prefix(FR)
    await create(
        project,
        pid,
        FR,
        [
            {
                "key": f"{prefix}FR_AUTH_012_LOGIN",
                "name": "FR-AUTH-012: Users log in",
                "identity_key": id_key(project, "FR-AUTH-012"),
                "statement_text": "FR-AUTH-012: Users log in",
                "is_assumption": "no",
                "embedding": near(0.3),
                "embedding_model": MODEL,
            },
            {
                "key": f"{prefix}FR_AUTH_013_LOGOUT",
                "name": "FR-AUTH-013: Users log out",
                "identity_key": id_key(project, "FR-AUTH-013"),
                "identity_aliases": [id_key(project, "FR-AUTH-099")],
                "spec_id_aliases": [f"{prefix}OLD_LOGOUT"],
                "statement_text": "FR-AUTH-013: Users log out",
                "is_assumption": "yes",
            },
        ]
        + [
            {
                "key": f"MENTION_{index:02d}",
                "name": f"Session rules {index}",
                "statement_text": "see FR-AUTH-012 and FR-AUTH-013; FR AUTH 012 auth auth 012 012",
                "embedding": [float(x) for x in QUERY],
                "embedding_model": MODEL,
            }
            for index in range(12)
        ],
    )
    await project.probe.await_indexes(60)


@pytest.mark.parametrize("fused", [True, False], ids=["fused", "fulltext"])
async def test_a_node_named_by_explicit_id_ranks_first(graph_project: GraphProject, *, fused: bool) -> None:
    pid = graph_project.project_id
    await id_project(graph_project, pid)
    reader = reader_of(graph_project, FixedGateway(QUERY) if fused else None)
    hits = await reader.find_nodes(pid, "FR-AUTH-012", limit=5)
    assert (hits[0].node.name, hits[0].matched) == ("FR-AUTH-012: Users log in", "id")
    assert len(hits) == 5 and len({h.node.uuid for h in hits}) == 5
    assert all(h.matched != "id" for h in hits[1:])


async def test_several_ids_rank_in_query_order(graph_project: GraphProject) -> None:
    pid = graph_project.project_id
    await id_project(graph_project, pid)
    prefix = graph_project.ontology.id_prefix(FR)
    reader = reader_of(graph_project, FixedGateway(QUERY))
    hits = await reader.find_nodes(pid, "compare FR_AUTH_013 with FR-AUTH-012", limit=4)
    assert [(h.node.spec_id, h.matched) for h in hits[:2]] == [
        (f"{prefix}FR_AUTH_013_LOGOUT", "id"),
        (f"{prefix}FR_AUTH_012_LOGIN", "id"),
    ]
    # A spec_id alias before an explicit id: text order across both kinds of name.
    hits = await reader.find_nodes(pid, f"{prefix}OLD_LOGOUT versus FR-AUTH-012", limit=4)
    assert [(h.node.spec_id, h.matched) for h in hits[:2]] == [
        (f"{prefix}FR_AUTH_013_LOGOUT", "id"),
        (f"{prefix}FR_AUTH_012_LOGIN", "id"),
    ]
    # An identity alias, a spec_id alias, and both together (the node once).
    for text in ("FR-AUTH-099", f"{prefix}OLD_LOGOUT", f"FR-AUTH-099 {prefix}OLD_LOGOUT FR-AUTH-013"):
        hits = await reader.find_nodes(pid, text, limit=3)
        assert [h.node.spec_id for h in hits if h.matched == "id"] == [f"{prefix}FR_AUTH_013_LOGOUT"], text


async def test_an_id_of_another_project_or_no_node_falls_back_to_normal_results(graph_project: GraphProject) -> None:
    pid, other = graph_project.project_id, graph_project.new_project()
    await id_project(graph_project, other)
    await create(
        graph_project, pid, FR, [{"key": "LOCAL", "name": "auth session", "statement_text": "FR AUTH session"}]
    )
    reader = reader_of(graph_project)
    for limit in (1, 5):  # another project's node never takes a place of the limit
        hits = await reader.find_nodes(pid, "FR-AUTH-012", limit=limit)
        assert [(h.node.spec_id, h.matched) for h in hits] == [("LOCAL", "fulltext")]
    hits = await reader.find_nodes(other, "FR-AUTH-404 auth", limit=3)
    assert hits and all(h.matched != "id" for h in hits)


async def test_filters_apply_to_id_hits(graph_project: GraphProject) -> None:
    pid = graph_project.project_id
    await id_project(graph_project, pid)
    reader = reader_of(graph_project)
    assert all(h.matched != "id" for h in await reader.find_nodes(pid, "FR-AUTH-012", labels=[MODULE, QR]))
    hits = await reader.find_nodes(pid, "FR-AUTH-012 FR-AUTH-013", where={"is_assumption": "yes"})
    assert [(h.node.name, h.matched) for h in hits] == [("FR-AUTH-013: Users log out", "id")]
