"""Live Neo4j tests of ``GraphReader`` search and traversal (plan 5.T1).

FR-READ-02..09 and m5-spec-edits H1-H7: fused search and its fallbacks, project scoping on the shared
indexes (paged fulltext, the exact-cosine and the index vector paths), query text handling, ``scope``
over the declared spine paths, ``impact`` and its truncation, ``nodes_for_fragment`` boundaries,
``find_fragments``, and reads without vectors. Graphs are written with raw Cypher (hand-written names
and vectors) or built from the shop corpus with the hand-written ``ShopAnswers`` (no model output).

Gate: ``SPECFLOW_TEST_NEO4J=1`` and ``uv run --env-file ../.env.local``. Every test works in its own
``pytest-graph-<hex>`` project ids; ``neo4j_project`` deletes them by ``group_id`` afterwards and
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
from graph_support import (
    Neo4jProject,
    ScriptedGateway,
    live_embedding_dim,
    live_llm_config,
    shop_answers,
    shop_corpus,
)
from specflow_live import requires_live

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, neo4j_project) live in graph_support.
pytest_plugins = ["graph_support"]

pytestmark = requires_live("NEO4J")

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


async def create(project: Neo4jProject, pid: str, label: str, rows: Sequence[Mapping[str, Any]]) -> None:
    """Spec nodes of ``label``: each row's ``key`` is its ``spec_id``; its uuid (``<pid>-`` and the key
    mirrored) sorts opposite to the ``spec_id``, so a tie-break by uuid would show. ``name`` defaults
    to the key; other row fields become properties."""

    await project.run(
        f"UNWIND $rows AS row CREATE (n:SpecNode:{label} {{group_id: $p, uuid: $p + '-' + row.flipped, "
        "spec_id: row.key, name: coalesce(row.name, row.key)}) SET n += row.props",
        p=pid,
        rows=[
            {
                "key": row["key"],
                "flipped": flipped(row["key"]),
                "name": row.get("name"),
                "props": {k: v for k, v in row.items() if k not in ("key", "name")},
            }
            for row in rows
        ],
    )


async def link(project: Neo4jProject, pid: str, edges: Sequence[tuple[str, str, str]]) -> None:
    for source, relation, target in edges:
        await project.run(
            "MATCH (s:SpecNode {group_id: $p, spec_id: $s}), (t:SpecNode {group_id: $p, spec_id: $t}) "
            f"CREATE (s)-[:{relation} {{group_id: $p, edge_key: $s + '|' + $t}}]->(t)",
            p=pid,
            s=source,
            t=target,
        )


def reader_of(project: Neo4jProject, gateway: Any = None) -> GraphReader:
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


async def fusion_project(project: Neo4jProject, pid: str | None = None) -> str:
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
    await project.run("CALL db.awaitIndexes(60)")
    return pid


async def crowd_project(project: Neo4jProject, count: int = 400) -> str:
    """Another project whose nodes all sit on the query vector and match the search word."""

    pid = project.new_project()
    await project.run(
        "UNWIND range(1, $count) AS i CREATE (:SpecNode:FunctionalRequirement {group_id: $p, uuid: $p + '-' + "
        "toString(i), "
        "spec_id: 'CROWD::' + toString(i), name: $token + ' crowd ' + toString(i), "
        "statement_text: $token + ' ' + $token + ' ' + $token, embedding: $vector, embedding_model: $model})",
        p=pid,
        count=count,
        token=SEARCH_WORD,
        vector=[float(x) for x in QUERY],
        model=MODEL,
    )
    await project.run("CALL db.awaitIndexes(60)")
    return pid


# --- FR-READ-02, 03, 08 / H2, H5: fused search ---------------------------------------------------


async def test_node_hit_by_both_indexes_ranks_first(neo4j_project: Neo4jProject) -> None:
    pid = await fusion_project(neo4j_project)
    hits = await reader_of(neo4j_project, FixedGateway(QUERY)).find_nodes(pid, SEARCH_WORD, limit=3)
    assert [(h.node.spec_id, h.matched) for h in hits] == [
        ("FUS::B", "both"),
        ("FUS::V", "vector"),  # the vector list weighs double: vector rank 1 beats fulltext rank 1
        ("FUS::FILL_00", "vector"),
    ]
    assert hits[0].score == pytest.approx((VECTOR_WEIGHT + 1) / (RRF_K + 2))
    assert hits[1].score == pytest.approx(VECTOR_WEIGHT / (RRF_K + 1))
    assert hits[2].score == pytest.approx(VECTOR_WEIGHT / (RRF_K + 3))
    # Depth = limit: F (fulltext rank 1, 1 / 11) is below the third vector hit (2 / 13).
    hits = await reader_of(neo4j_project, FixedGateway(QUERY)).find_nodes(pid, SEARCH_WORD, limit=6)
    assert [h.node.spec_id for h in hits] == [
        "FUS::B",
        "FUS::V",
        "FUS::FILL_00",
        "FUS::FILL_01",
        "FUS::FILL_02",
        "FUS::FILL_03",
    ]


@pytest.mark.parametrize("gateway", [None, FixedGateway(QUERY, fail=LLMError("exhausted"))], ids=["none", "failing"])
async def test_without_a_working_gateway_search_is_fulltext_only(neo4j_project: Neo4jProject, gateway: Any) -> None:
    pid = await fusion_project(neo4j_project)
    hits = await reader_of(neo4j_project, gateway).find_nodes(pid, SEARCH_WORD, limit=3)
    assert [(h.node.spec_id, h.matched) for h in hits] == [("FUS::F", "fulltext"), ("FUS::B", "fulltext")]


async def test_embedding_model_mismatch_is_fulltext_only_without_an_embed_call(neo4j_project: Neo4jProject) -> None:
    pid = await fusion_project(neo4j_project)
    gateway = FixedGateway(QUERY, model="another/embedding-model")
    hits = await reader_of(neo4j_project, gateway).find_nodes(pid, SEARCH_WORD, limit=3)
    assert [(h.node.spec_id, h.matched) for h in hits] == [("FUS::F", "fulltext"), ("FUS::B", "fulltext")]
    assert gateway.texts == []


@pytest.mark.parametrize("exact_max", [2000, 0], ids=["exact", "index"])
async def test_vectors_of_another_embedding_model_are_never_compared(
    neo4j_project: Neo4jProject, monkeypatch: pytest.MonkeyPatch, exact_max: int
) -> None:
    pid = await fusion_project(neo4j_project)
    await create(
        neo4j_project,
        pid,
        FR,
        [{"key": "FUS::OTHER_MODEL", "embedding": [float(x) for x in QUERY], "embedding_model": "another/model"}],
    )
    await neo4j_project.run("CALL db.awaitIndexes(60)")
    monkeypatch.setattr(reader_module, "EXACT_VECTOR_MAX", exact_max)
    hits = await reader_of(neo4j_project, FixedGateway(QUERY)).find_nodes(pid, SEARCH_WORD, limit=5)
    assert "FUS::OTHER_MODEL" not in ids([h.node for h in hits])
    assert ids([h.node for h in hits[:3]]) == ["FUS::B", "FUS::V", "FUS::FILL_00"]


async def test_candidate_vectors_are_counted_in_the_project_only(neo4j_project: Neo4jProject) -> None:
    pid, other = neo4j_project.project_id, neo4j_project.new_project()
    vector = [float(x) for x in QUERY]
    await create(
        neo4j_project,
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
        neo4j_project,
        other,
        FR,
        [{"key": "ELSEWHERE", "name": SEARCH_WORD, "embedding": vector, "embedding_model": MODEL}],
    )
    await neo4j_project.run("CALL db.awaitIndexes(60)")
    gateway = FixedGateway(QUERY)
    hits = await reader_of(neo4j_project, gateway).find_nodes(pid, SEARCH_WORD, limit=3)
    assert [(h.node.spec_id, h.matched) for h in hits] == [("OWN", "fulltext")]
    assert gateway.texts == []  # the other project's vectors of the gateway's model do not count


async def test_find_nodes_filters_by_label_and_attribute(neo4j_project: Neo4jProject) -> None:
    pid = await fusion_project(neo4j_project)
    reader = reader_of(neo4j_project, FixedGateway(QUERY))
    assert ids([h.node for h in await reader.find_nodes(pid, SEARCH_WORD, where={"is_assumption": "yes"})]) == [
        "FUS::B"
    ]
    assert await reader.find_nodes(pid, SEARCH_WORD, labels=[MODULE]) == []
    with pytest.raises(ConfigurationError) as raised:
        await reader.find_nodes(pid, SEARCH_WORD, where={"no_such_attribute": "x"})
    assert raised.value.code == "unknown_attribute"


async def test_where_none_selects_nodes_without_the_property(neo4j_project: Neo4jProject) -> None:
    pid = neo4j_project.project_id
    await create(neo4j_project, pid, FR, [{"key": "W::SET", "acceptance_criteria": "x"}, {"key": "W::ABSENT"}])
    reader = reader_of(neo4j_project)
    assert ids(await reader.nodes(pid, where={"acceptance_criteria": None})) == ["W::ABSENT"]
    assert ids(await reader.nodes(pid, where={"acceptance_criteria": "x"})) == ["W::SET"]


async def test_search_never_returns_foreign_nodes_documents_or_fragments(neo4j_project: Neo4jProject) -> None:
    pid = await fusion_project(neo4j_project)
    await neo4j_project.run(
        "CREATE (:SpecNode:Finding {group_id: $p, uuid: $p + '-g', spec_id: 'GAP::G', name: $t, statement_text: $t, "
        "embedding: $v, embedding_model: $m}) "
        "CREATE (:Document {group_id: $p, document_id: 'd', spec_id: 'DOC::d', title: $t})-[:CONTAINS]->"
        "(:Fragment {group_id: $p, fragment_id: 'f', spec_id: 'FRG::f', status: 'active', text: $t})",
        p=pid,
        t=f"{SEARCH_WORD} {SEARCH_WORD} {SEARCH_WORD} {SEARCH_WORD}",
        v=[float(x) for x in QUERY],
        m=MODEL,
    )
    await neo4j_project.run("CALL db.awaitIndexes(60)")
    hits = await reader_of(neo4j_project, FixedGateway(QUERY)).find_nodes(pid, SEARCH_WORD, limit=50)
    assert {h.node.label for h in hits} == {FR}
    assert not {"GAP::G", "DOC::d", "FRG::f"} & {h.node.spec_id for h in hits}


# --- vector floor and stop words (M6 tuning) ----------------------------------------------------


@pytest.mark.parametrize("exact_max", [2000, 0], ids=["exact", "index"])
async def test_vector_floor_applies_in_the_same_scale_on_both_paths(
    neo4j_project: Neo4jProject, monkeypatch: pytest.MonkeyPatch, exact_max: int
) -> None:
    pid = neo4j_project.project_id
    rows = [
        {"key": "REL", "embedding": near(0.3), "embedding_model": MODEL},  # score 0.65
        {"key": "UNREL", "embedding": near(0.1), "embedding_model": MODEL},  # score 0.55
    ]
    await create(neo4j_project, pid, FR, rows)
    await neo4j_project.run("CALL db.awaitIndexes(60)")
    monkeypatch.setattr(reader_module, "EXACT_VECTOR_MAX", exact_max)
    related = await reader_of(neo4j_project, FixedGateway(QUERY)).find_nodes(pid, "zqnomatch", limit=5)
    assert [(h.node.spec_id, h.matched) for h in related] == [("REL", "vector")]
    unrelated = np.random.default_rng(7).standard_normal(DIM)  # nearly orthogonal to both: about 0.5
    assert await reader_of(neo4j_project, FixedGateway(unrelated)).find_nodes(pid, "zqnomatch", limit=5) == []
    everything = GraphReader(
        neo4j_project.driver,
        neo4j_project.ontology,
        FixedGateway(unrelated),
        database=neo4j_project.database,
        vector_floor=0.0,
    )
    assert {h.node.spec_id for h in await everything.find_nodes(pid, "zqnomatch", limit=5)} == {"REL", "UNREL"}


async def test_vector_floor_is_validated(neo4j_project: Neo4jProject) -> None:
    for floor in (-0.1, 1.5, True):
        with pytest.raises(ConfigurationError) as raised:
            GraphReader(neo4j_project.driver, neo4j_project.ontology, vector_floor=floor)
        assert raised.value.code == "invalid_vector_floor"


async def test_stop_words_are_left_out_of_fulltext_queries(neo4j_project: Neo4jProject) -> None:
    pid = neo4j_project.project_id
    await create(
        neo4j_project,
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
    await neo4j_project.run(
        "CREATE (:Document {group_id: $p, document_id: 'd', spec_id: 'DOC::d', path: 'a.md'})-[:CONTAINS]->"
        "(:Fragment {group_id: $p, fragment_id: 'f', document_id: 'd', status: 'active', text: 'The basket of the "
        "user'})",
        p=pid,
    )
    reader = reader_of(neo4j_project)
    assert await reader.find_nodes(pid, "the") == []
    assert await reader.find_nodes(pid, "The AND of the") == []
    assert await reader.find_fragments(pid, "the") == []
    assert [(h.node.spec_id, h.matched) for h in await reader.find_nodes(pid, "the checkout")] == [
        ("CHECKOUT", "fulltext")
    ]


# --- H2: project scoping on the shared indexes ----------------------------------------------------


async def test_crowded_shared_indexes_return_only_own_hits_on_both_vector_paths(
    neo4j_project: Neo4jProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = await fusion_project(neo4j_project)
    await crowd_project(neo4j_project)
    expected = [("FUS::B", "both"), ("FUS::V", "vector"), ("FUS::FILL_00", "vector")]

    exact = reader_of(neo4j_project, FixedGateway(QUERY))
    exact_counts = spy(exact)
    assert [(h.node.spec_id, h.matched) for h in await exact.find_nodes(pid, SEARCH_WORD, limit=3)] == expected
    assert exact_counts.get("search_nodes_vector", 0) == 0 and exact_counts["search_nodes_vector_exact"] == 1

    monkeypatch.setattr(reader_module, "EXACT_VECTOR_MAX", 0)  # 23 candidates: force the index path
    for limit in (3, 5, 20):
        index = reader_of(neo4j_project, FixedGateway(QUERY))
        counts = spy(index)
        hits = await index.find_nodes(pid, SEARCH_WORD, limit=limit)
        assert counts["search_nodes_vector"] >= 1
        assert all(h.node.uuid.startswith(pid) for h in hits)
        assert len(hits) == min(limit, len(FUSION_VECTORS))
        # The approximate (quantised) index may swap fillers 0.01 apart; B and V are clear.
        assert [(h.node.spec_id, h.matched) for h in hits[:2]] == expected[:2]


async def test_fulltext_pages_past_another_projects_matches(neo4j_project: Neo4jProject) -> None:
    pid = await fusion_project(neo4j_project)
    await crowd_project(neo4j_project)  # 400 better-scoring matches of the word elsewhere
    reader = reader_of(neo4j_project)
    counts = spy(reader)
    # Only B passes the filter: the pages go past the 400 matches elsewhere and F.
    hits = await reader.find_nodes(pid, SEARCH_WORD, where={"is_assumption": "yes"}, limit=1)
    assert [(h.node.spec_id, h.matched) for h in hits] == [("FUS::B", "fulltext")]
    assert counts["search_nodes_fulltext"] > 1


async def test_vector_index_path_above_2000_candidates(neo4j_project: Neo4jProject) -> None:
    pid = neo4j_project.project_id
    # 2,100 random positive vectors (made in Neo4j, nearly orthogonal to the query) and one target.
    await neo4j_project.run(
        "UNWIND range(1, 2100) AS i CREATE (:SpecNode:FunctionalRequirement {group_id: $p, uuid: $p + '-' + "
        "toString(i), "
        "spec_id: 'BIG::' + toString(i), name: 'bulk ' + toString(i), embedding: [x IN range(1, $dim) | rand() - 0.5], "
        "embedding_model: $m})",
        p=pid,
        dim=DIM,
        m=MODEL,
    )
    await create(
        neo4j_project,
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
    await neo4j_project.run("CALL db.awaitIndexes(120)")
    reader = reader_of(neo4j_project, FixedGateway(QUERY))
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
async def test_punctuated_and_long_queries_search_without_error(neo4j_project: Neo4jProject, text: str) -> None:
    pid = await fusion_project(neo4j_project)
    gateway = FixedGateway(QUERY)
    hits = await reader_of(neo4j_project, gateway).find_nodes(pid, text, limit=3)
    assert len(hits) == 3  # the vector list always has the project's nodes
    assert len(gateway.texts[0]) <= reader_module.MAX_QUERY_CHARS
    assert isinstance(await reader_of(neo4j_project).find_fragments(pid, text), list)


@pytest.mark.parametrize("method", ["find_nodes", "find_fragments"])
async def test_text_lucene_rejects_raises_query_too_long(
    neo4j_project: Neo4jProject, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    monkeypatch.setattr(reader_module, "_lucene_query", lambda _text: " ".join(["w"] * 1100))
    with pytest.raises(ConfigurationError) as raised:
        await getattr(reader_of(neo4j_project), method)(neo4j_project.project_id, "x")
    assert raised.value.code == "query_too_long"


@pytest.mark.parametrize("limit", [0, 501])
async def test_search_limits_are_capped(neo4j_project: Neo4jProject, limit: int) -> None:
    for method in ("find_nodes", "find_fragments"):
        with pytest.raises(ConfigurationError) as raised:
            await getattr(reader_of(neo4j_project), method)(neo4j_project.project_id, SEARCH_WORD, limit=limit)
        assert raised.value.code == "invalid_limit"


async def test_limit_500_returns_every_node_of_a_small_project(neo4j_project: Neo4jProject) -> None:
    pid = await fusion_project(neo4j_project)
    hits = await reader_of(neo4j_project, FixedGateway(QUERY)).find_nodes(pid, SEARCH_WORD, limit=500)
    assert len(hits) == len(FUSION_VECTORS)


# --- FR-READ-07 / H1: scope ----------------------------------------------------------------------


async def scope_graph(project: Neo4jProject, pid: str) -> None:
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
    await project.run(
        "MATCH (m:SpecNode {group_id: $p, spec_id: 'M'}) "
        "CREATE (:SpecNode:Finding {group_id: $p, uuid: $p + '-x', spec_id: 'GAP::X', name: 'x'})-[:PART_OF "
        "{group_id: $p}]->(m) "
        "CREATE (:Finding {group_id: $p, spec_id: 'GAP::Y'})-[:FLAGS {group_id: $p}]->(m) "
        "CREATE (m)-[:REFERENCED_BY {line_start: 1, line_end: 2}]->(:Fragment {group_id: $p, fragment_id: 'f'})",
        p=pid,
    )


async def test_scope_of_a_module_is_its_spine_paths(neo4j_project: Neo4jProject) -> None:
    pid = neo4j_project.project_id
    await scope_graph(neo4j_project, pid)
    reader = reader_of(neo4j_project)
    # FR-READ-07: the feature, the capability and the 3 requirements; F2, F3 by 2-3 PART_OF hops.
    assert ids(await reader.scope(pid, "M")) == ["C", "F", "F2", "F3", "R1", "R2", "R3"]
    await link(neo4j_project, pid, [("M", "RELATES_TO", "R4")])  # symmetric, stored from the module
    assert ids(await reader.scope(pid, "M")) == ["C", "F", "F2", "F3", "R1", "R2", "R3", "R4"]
    assert ids(await reader.scope(pid, "M", labels=[FR])) == ["R1", "R2", "R3", "R4"]
    assert await reader.scope(pid, "NOPE") == []


async def test_scope_takes_every_part_of_a_sequence_path(neo4j_project: Neo4jProject) -> None:
    pid = neo4j_project.project_id
    await scope_graph(neo4j_project, pid)
    reader = reader_of(neo4j_project)
    assert ids(await reader.scope(pid, "C")) == ["R3"]  # GROUPED_BY alone
    assert "C" in ids(await reader.scope(pid, "M"))  # REALIZED_BY alone


async def test_scope_never_reaches_other_modules_or_passes_through_a_root(neo4j_project: Neo4jProject) -> None:
    pid = neo4j_project.project_id
    await create(neo4j_project, pid, MODULE, [{"key": key} for key in ("M1", "M2", "SM")])
    await create(neo4j_project, pid, QR, [{"key": "RSYS"}])
    await create(neo4j_project, pid, FEATURE, [{"key": "F1"}, {"key": "F2"}])
    await create(neo4j_project, pid, FR, [{"key": "RF2"}, {"key": "RSM"}])
    await link(
        neo4j_project,
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
    reader = reader_of(neo4j_project)
    assert ids(await reader.scope(pid, "M1")) == ["F1", "RSYS", "SM"]
    assert ids(await reader.scope(pid, "M2")) == ["F2", "RF2", "RSYS"]


async def test_scope_is_project_scoped(neo4j_project: Neo4jProject) -> None:
    pid, other = neo4j_project.project_id, neo4j_project.new_project()
    await scope_graph(neo4j_project, pid)
    await create(neo4j_project, other, MODULE, [{"key": "M"}])
    await create(neo4j_project, other, FR, [{"key": "R9"}])
    await link(neo4j_project, other, [("R9", "PART_OF", "M")])
    assert ids(await reader_of(neo4j_project).scope(other, "M")) == ["R9"]


# --- FR-READ-05 / H5: impact ---------------------------------------------------------------------


@pytest.mark.parametrize("reachable", [500, 501, 600])
async def test_impact_returns_at_most_500_and_flags_truncation(neo4j_project: Neo4jProject, reachable: int) -> None:
    pid = neo4j_project.project_id
    await neo4j_project.run(
        "CREATE (h:SpecNode:FunctionalRequirement {group_id: $p, uuid: $p + '-H', spec_id: 'H', name: 'H', "
        "spec_id_aliases: ['OLD_H']}) WITH h UNWIND range(1, $reachable) AS i "
        "CREATE (n:SpecNode:FunctionalRequirement {group_id: $p, uuid: $p + '-' + toString(i), "
        "spec_id: 'N' + right('000' + toString(i), 3), name: 'n'}) "
        "CREATE (h)-[:RELATES_TO {group_id: $p, edge_key: toString(i)}]->(n)",
        p=pid,
        reachable=reachable,
    )
    reader = reader_of(neo4j_project)
    result = await reader.impact(pid, ["H"], depth=2)
    assert len(result.nodes) == 500
    assert result.truncated is (reachable > 500)
    assert (result.nodes[0].spec_id, result.nodes[-1].spec_id) == ("N001", "N500")
    aliased = await reader.impact(pid, ["OLD_H"], depth=1)
    assert aliased.sources == ("H",) and len(aliased.nodes) == 500


async def test_impact_is_nearest_first_in_both_directions(neo4j_project: Neo4jProject) -> None:
    pid = neo4j_project.project_id
    await create(neo4j_project, pid, FEATURE, [{"key": f"A{i}"} for i in range(5)])
    await create(neo4j_project, pid, FR, [{"key": "B1"}])
    await link(neo4j_project, pid, [(f"A{i}", "PART_OF", f"A{i + 1}") for i in range(4)] + [("B1", "RELATES_TO", "A1")])
    await neo4j_project.run(
        "MATCH (a:SpecNode {group_id: $p, spec_id: 'A1'}) "
        "CREATE (a)-[:REFERENCED_BY]->(:Fragment {group_id: $p, fragment_id: 'f'}) "
        "CREATE (:SpecNode:Finding {group_id: $p, uuid: $p + '-g', spec_id: 'GAP::G', name: 'g'})-[:RELATES_TO "
        "{group_id: $p}]->(a)",
        p=pid,
    )
    reader = reader_of(neo4j_project)
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


async def test_nodes_for_fragment_overlaps_evidence_lines_of_active_fragments(neo4j_project: Neo4jProject) -> None:
    pid = neo4j_project.project_id
    await neo4j_project.run(
        "CREATE (d:Document {group_id: $p, document_id: 'doc1', spec_id: 'DOC::doc1', path: 'a.md'}) "
        "CREATE (d2:Document {group_id: $p, document_id: 'doc2', spec_id: 'DOC::doc2', path: 'b.md'}) "
        "CREATE (d)-[:CONTAINS]->(f1:Fragment {group_id: $p, fragment_id: 'f1', document_id: 'doc1', status: "
        "'active', line_start: 5, line_end: 20}) "
        "CREATE (d)-[:CONTAINS]->(fs:Fragment {group_id: $p, fragment_id: 'fs', document_id: 'doc1', status: "
        "'skipped', line_start: 30, line_end: 40}) "
        "CREATE (d)-[:CONTAINS]->(f0:Fragment {group_id: $p, fragment_id: 'f0', document_id: 'doc1', status: "
        "'retired', line_start: 5, line_end: 20}) "
        "CREATE (d2)-[:CONTAINS]->(f2:Fragment {group_id: $p, fragment_id: 'f2', document_id: 'doc2', status: "
        "'active', line_start: 1, line_end: 30}) "
        "CREATE (:SpecNode:FunctionalRequirement {group_id: $p, uuid: $p + '-n', spec_id: 'N', name: "
        "'n'})-[:REFERENCED_BY {line_start: 10, line_end: 12}]->(f1) "
        "CREATE (:SpecNode:FunctionalRequirement {group_id: $p, uuid: $p + '-s', spec_id: 'S', name: "
        "'s'})-[:REFERENCED_BY]->(fs) "
        "CREATE (:SpecNode:FunctionalRequirement {group_id: $p, uuid: $p + '-r', spec_id: 'RETIRED', name: "
        "'r'})-[:REFERENCED_BY {line_start: 10, line_end: 12}]->(f0) "
        "CREATE (:SpecNode:FunctionalRequirement {group_id: $p, uuid: $p + '-o', spec_id: 'OTHER', name: "
        "'o'})-[:REFERENCED_BY {line_start: 10, line_end: 12}]->(f2) "
        "CREATE (:SpecNode:Finding {group_id: $p, uuid: $p + '-g', spec_id: 'GAP::F', name: 'g'})-[:REFERENCED_BY "
        "{line_start: 10, line_end: 12}]->(f1)",
        p=pid,
    )
    reader = reader_of(neo4j_project)

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


async def fragment_corpus(project: Neo4jProject, pid: str) -> None:
    await project.run(
        "CREATE (d:Document {group_id: $p, document_id: 'd1', spec_id: 'DOC::d1', path: 'auth.md', status: 'active'}) "
        "CREATE (d2:Document {group_id: $p, document_id: 'd2', spec_id: 'DOC::d2', path: 'other.md', status: "
        "'active'}) "
        "CREATE (d)-[:CONTAINS]->(:Fragment {group_id: $p, fragment_id: 'a', document_id: 'd1', status: 'active', "
        "text: 'Users sign in with ' + $t + ' single sign-on.'}) "
        "CREATE (d)-[:CONTAINS]->(:Fragment {group_id: $p, fragment_id: 'r', document_id: 'd1', status: 'retired', "
        "text: 'Old ' + $t + ' text.'}) "
        "CREATE (d)-[:CONTAINS]->(:Fragment {group_id: $p, fragment_id: 's', document_id: 'd1', status: 'skipped', "
        "text: 'Skipped ' + $t + ' text.'}) "
        "CREATE (d2)-[:CONTAINS]->(:Fragment {group_id: $p, fragment_id: 'b', document_id: 'd2', status: 'active', "
        "text: $t + ' ' + $t + ' metadata.'})",
        p=pid,
        t=SEARCH_WORD.upper(),
    )


async def test_find_fragments_returns_active_fragments_of_the_project(neo4j_project: Neo4jProject) -> None:
    pid, other = neo4j_project.project_id, neo4j_project.new_project()
    await fragment_corpus(neo4j_project, pid)
    await fragment_corpus(neo4j_project, other)
    reader = reader_of(neo4j_project)
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


async def test_find_fragments_pages_past_many_matches_elsewhere(neo4j_project: Neo4jProject) -> None:
    pid, crowd = neo4j_project.project_id, neo4j_project.new_project()
    await neo4j_project.run(
        "UNWIND range(1, 1500) AS i CREATE (:Fragment {group_id: $p, fragment_id: toString(i), document_id: 'd', "
        "status: 'active', text: 'zqcommon zqcommon zqcommon ' + toString(i)})",
        p=crowd,
    )
    await neo4j_project.run(
        "CREATE (:Document {group_id: $p, document_id: 'd1', spec_id: 'DOC::d1', path: 'a.md'})-[:CONTAINS]->"
        "(:Fragment {group_id: $p, fragment_id: 'mine', document_id: 'd1', status: 'active', "
        "text: 'one zqcommon here, among many other words of a longer fragment'})",
        p=pid,
    )
    reader = reader_of(neo4j_project)
    counts = spy(reader)
    assert [h.fragment.fragment_id for h in await reader.find_fragments(pid, "zqcommon", limit=5)] == ["mine"]
    assert counts["search_fragments"] > 1


# --- shop graph: independent walks, two identical projects, no vectors (FR-READ-04..07, H7) ------


async def build(project: Neo4jProject, gateway: ScriptedGateway, root: Path, pid: str) -> None:
    stores = open_file_stores(root)
    ctx = BuildContext(
        project_id=pid,
        ontology=project.ontology,
        driver=project.driver,
        gateway=gateway,
        registry=stores.registry,
        corpus=shop_corpus(1, root),
        extraction=stores.extraction,
        builds=stores.builds,
        options=ANSWERS.options,
        database=project.database,
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
    neo4j_project: Neo4jProject, scripted_gateway: Any, tmp_path: Path
) -> None:
    pid, twin = neo4j_project.project_id, neo4j_project.new_project()
    await build(neo4j_project, ANSWERS.install(scripted_gateway(live_llm_config())), tmp_path / "a", pid)
    await build(neo4j_project, ANSWERS.install(scripted_gateway(live_llm_config())), tmp_path / "b", twin)
    gateway = ANSWERS.install(scripted_gateway(live_llm_config()))
    reader = reader_of(neo4j_project, gateway)
    nodes = await reader.nodes(pid)
    edges = await reader.edges(pid)
    labels = {node.spec_id: node.label for node in nodes}
    own = {node.uuid for node in nodes}
    twin_uuids = {node.uuid for node in await reader.nodes(twin)}
    assert len(nodes) > 30 and not own & twin_uuids

    for node in nodes:
        assert set(ids(await reader.scope(pid, node.spec_id))) == spine_scope(
            neo4j_project.ontology, edges, labels, node.spec_id
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


async def test_reads_never_carry_vectors(neo4j_project: Neo4jProject, scripted_gateway: Any, tmp_path: Path) -> None:
    pid = neo4j_project.project_id
    await build(neo4j_project, ANSWERS.install(scripted_gateway(live_llm_config())), tmp_path / "a", pid)
    stored = await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p}) WHERE n.embedding IS NOT NULL AND n.name_embedding IS NOT NULL "
        "RETURN count(n) AS n",
        p=pid,
    )
    reader = reader_of(neo4j_project)
    rows = await reader._executor.read(READ_NODES, pid, labels=list(reader._spec_labels), where={}, retired="retired")
    assert stored[0]["n"] == len(rows) > 0
    assert all(row["props"]["embedding"] is None and row["props"]["name_embedding"] is None for row in rows)
    for node in await reader.nodes(pid):
        assert not {"embedding", "name_embedding", "embedding_model"} & set(node.attributes)
        assert "embedding" not in node.model_dump_json()


# --- explicit ids and spec_ids in the query (scale-report F8) ------------------------------------


def id_key(project: Neo4jProject, explicit: str) -> str:
    ontology = project.ontology
    label = ontology.label_for_id_family(explicit)
    assert label is not None
    return f"{ontology.identity_rule(label).scope}|id|{explicit}"


async def id_project(project: Neo4jProject, pid: str) -> None:
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
    await project.run("CALL db.awaitIndexes(60)")


@pytest.mark.parametrize("fused", [True, False], ids=["fused", "fulltext"])
async def test_a_node_named_by_explicit_id_ranks_first(neo4j_project: Neo4jProject, *, fused: bool) -> None:
    pid = neo4j_project.project_id
    await id_project(neo4j_project, pid)
    reader = reader_of(neo4j_project, FixedGateway(QUERY) if fused else None)
    hits = await reader.find_nodes(pid, "FR-AUTH-012", limit=5)
    assert (hits[0].node.name, hits[0].matched) == ("FR-AUTH-012: Users log in", "id")
    assert len(hits) == 5 and len({h.node.uuid for h in hits}) == 5
    assert all(h.matched != "id" for h in hits[1:])


async def test_several_ids_rank_in_query_order(neo4j_project: Neo4jProject) -> None:
    pid = neo4j_project.project_id
    await id_project(neo4j_project, pid)
    prefix = neo4j_project.ontology.id_prefix(FR)
    reader = reader_of(neo4j_project, FixedGateway(QUERY))
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


async def test_an_id_of_another_project_or_no_node_falls_back_to_normal_results(neo4j_project: Neo4jProject) -> None:
    pid, other = neo4j_project.project_id, neo4j_project.new_project()
    await id_project(neo4j_project, other)
    await create(
        neo4j_project, pid, FR, [{"key": "LOCAL", "name": "auth session", "statement_text": "FR AUTH session"}]
    )
    reader = reader_of(neo4j_project)
    for limit in (1, 5):  # another project's node never takes a place of the limit
        hits = await reader.find_nodes(pid, "FR-AUTH-012", limit=limit)
        assert [(h.node.spec_id, h.matched) for h in hits] == [("LOCAL", "fulltext")]
    hits = await reader.find_nodes(other, "FR-AUTH-404 auth", limit=3)
    assert hits and all(h.matched != "id" for h in hits)


async def test_filters_apply_to_id_hits(neo4j_project: Neo4jProject) -> None:
    pid = neo4j_project.project_id
    await id_project(neo4j_project, pid)
    reader = reader_of(neo4j_project)
    assert all(h.matched != "id" for h in await reader.find_nodes(pid, "FR-AUTH-012", labels=[MODULE, QR]))
    hits = await reader.find_nodes(pid, "FR-AUTH-012 FR-AUTH-013", where={"is_assumption": "yes"})
    assert [(h.node.name, h.matched) for h in hits] == [("FR-AUTH-013: Users log out", "id")]
