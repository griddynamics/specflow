"""Offline tests of the reader's pure parts (plan 5.T1): query terms and escaping, the embedded
search text, reciprocal rank fusion and its tie-breaks, fulltext paging, the vector-path choice,
argument validation, and scope queries built from the spine paths (the gateway's overflow class for an
embedding endpoint's too-long 400, H9, is tested in ``test_graph_gateway.py``).

FR-READ-02, 03, 05, 07, 08, 09; m5-spec-edits H1-H5. The live behaviour (the same paths on
Neo4j) is in ``test_graph_reader_live.py``. No Neo4j: a scripted executor answers each query by name.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from graph.core import (
    ConfigurationError,
    ConformanceError,
    Executor,
    GraphReader,
    InfrastructureUnavailableError,
    LLMError,
    LLMFatalError,
    NodePage,
    load_ontology,
)
from graph.core import reader as reader_module
from graph.core.queries import scope_sequence, scope_via
from graph.core.reader import (
    DEFAULT_VECTOR_FLOOR,
    MAX_QUERY_CHARS,
    MAX_QUERY_TERMS,
    MAX_SEARCH_LIMIT,
    RRF_K,
    VECTOR_WEIGHT,
    _embedding_text,
    _lucene_query,
    _query_terms,
)
from graph_support import (
    RENAMED_FEATURE_LABEL,
    RENAMED_FEATURE_PREFIX,
    llm_config,
    load_feature_renamed_ontology,
    shipped_ontology,
)

ONT = load_ontology()
FR = "FunctionalRequirement"
PROJECT = "offline-reader"
PROCEDURE_FAILED = "Neo.ClientError.Procedure.ProcedureCallFailed"
# Characters with a meaning in Lucene query syntax.
LUCENE_SYNTAX = re.compile(r'[+\-!(){}\[\]^"~*?:\\/&|]')

Answer = Callable[[Mapping[str, Any]], list[dict[str, Any]]]


class ScriptedExecutor(Executor):
    """An ``Executor`` answering each read by query name with ``answers[name](params)``; records
    every call. An unscripted query fails the test."""

    def __init__(self, answers: Mapping[str, Answer]) -> None:
        self.answers = dict(answers)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.project_ids: list[str] = []

    async def read(self, query: Any, project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        assert query.binds_project_id()
        self.project_ids.append(project_id)
        self.calls.append((query.name, params))
        if query.name not in self.answers:
            raise AssertionError(f"unscripted query {query.name}")
        return self.answers[query.name](params)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeGateway:
    def __init__(self, model: str = "test-embedding", fail: Exception | None = None) -> None:
        self.config = llm_config(embedding_model=model)
        self.fail = fail
        self.texts: list[str] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        if self.fail is not None:
            raise self.fail
        return [[1.0, 0.0] for _ in texts]


def _flip(text: str) -> str:
    return "".join(chr(0x2FFF - ord(char)) for char in text)


def uuid_of(spec_id: str) -> str:
    """A uuid that sorts opposite to its ``spec_id`` (so a tie-break by uuid shows)."""

    return "u-" + _flip(spec_id)


def spec_of(uuid: str) -> str:
    return _flip(uuid.removeprefix("u-"))


def node_row(spec_id: str, label: str = FR, **props: Any) -> dict[str, Any]:
    return {
        "labels": ["SpecNode", label],
        "props": {"spec_id": spec_id, "uuid": uuid_of(spec_id), "name": spec_id, **props},
        "provenance": [],
    }


def hit(spec_id: str, score: float) -> dict[str, Any]:
    return {"uuid": uuid_of(spec_id), "spec_id": spec_id, "score": score}


def page(hits: list[dict[str, Any]], fetched: int | None = None) -> list[dict[str, Any]]:
    return [{"fetched": len(hits) if fetched is None else fetched, "hits": hits}]


def hydrate(params: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [node_row(spec_of(uuid)) for uuid in sorted(params["uuids"])]


def search_executor(
    fulltext: list[dict[str, Any]],
    vector: list[dict[str, Any]] | None = None,
    *,
    matching: int = 10,
    stored: int | None = None,
) -> ScriptedExecutor:
    """Fulltext answers ``fulltext`` in one page; the project has ``matching`` candidate vectors (so
    exact cosine answers ``vector``)."""

    return ScriptedExecutor(
        {
            "search_nodes_fulltext": lambda _params: page(fulltext),
            "count_search_vectors": lambda _params: [
                {"matching": matching, "stored": matching if stored is None else stored}
            ],
            "search_nodes_vector_exact": lambda _params: list(vector or []),
            "read_nodes_by_uuid": hydrate,
        }
    )


def reader(executor: Executor, gateway: Any = None) -> GraphReader:
    return GraphReader(executor, ONT, gateway)


# --- H3: query terms --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SAML login", "SAML login"),
        ("FR-ADM-007 && || (login)", "FR ADM 007 login"),
        ('a+b "quoted" ~x* y? path/to:z \\ ^2 {c} [d] !e', "b quoted x y path z 2 c d e"),
        ("AND OR NOT TO and", ""),
        ("the checkout", "checkout"),
        ("The Checkout of an Order is in THE basket", "Checkout Order basket"),
        ("the a an of to is it", ""),
        ("don't e.g. 3.14 x_y", "don't e.g 3.14 x_y"),
        ("Zażółć gęślą jaźń", "Zażółć gęślą jaźń"),
        ("???  ***", ""),
        ("", ""),
    ],
)
def test_query_text_is_split_like_the_standard_analyzer_into_plain_terms(text: str, expected: str) -> None:
    query = _lucene_query(text)
    assert query == expected
    assert not LUCENE_SYNTAX.search(query)


@pytest.mark.parametrize(
    "text",
    [
        ",".join(["w"] * 1100),
        " ".join(["a-b-c-d-e"] * 256),
        "中" * 2000,
        " ".join(["word"] * 3000),
        "x" * 5000,
    ],
)
def test_long_or_punctuated_text_stays_under_the_clause_limit(text: str) -> None:
    terms, end = _query_terms(text)
    assert len(terms) <= MAX_QUERY_TERMS
    assert sum(len(term) for term in terms) <= MAX_QUERY_CHARS
    assert end <= len(text)
    embedded = _embedding_text(text)
    assert 0 < len(embedded) <= MAX_QUERY_CHARS


def test_term_cap_counts_terms_then_characters() -> None:
    assert len(_query_terms(" ".join(["ab"] * 400))[0]) == MAX_QUERY_TERMS
    # 300 terms of 4 characters: the 1000-character cut comes first (250 terms).
    assert len(_query_terms(" ".join(["abcd"] * 300))[0]) == MAX_QUERY_CHARS // 4


def test_embedded_text_is_the_text_up_to_the_fulltext_cut() -> None:
    assert _embedding_text("  hello, world!  ") == "hello, world"
    text = " ".join(["ab"] * 400)  # cut at 256 terms, before 1000 characters
    terms, end = _query_terms(text)
    assert len(terms) == MAX_QUERY_TERMS and end < MAX_QUERY_CHARS
    assert _embedding_text(text) == text[:end]
    longer = " ".join(f"w{index:03d}" for index in range(400))  # cut text is over 1000 characters
    assert _embedding_text(longer) == longer[:MAX_QUERY_CHARS]
    assert _embedding_text("???") == "???"


# --- FR-READ-02 / H2 / H5: fusion ---------------------------------------------------------------


def test_tuned_fusion_constants() -> None:
    # M6 tuning (m6-eval-report 9(b)).
    assert (RRF_K, VECTOR_WEIGHT, reader_module.OVERFETCH, DEFAULT_VECTOR_FLOOR) == (10, 2.0, 1, 0.60)


async def test_node_in_both_lists_ranks_above_nodes_in_one() -> None:
    executor = search_executor(
        fulltext=[hit("F", 9.0), hit("B", 5.0)],
        vector=[hit("V", 0.99), hit("B", 0.95)],
    )
    hits = await reader(executor, FakeGateway()).find_nodes(PROJECT, "saml", limit=3)
    assert [(h.node.spec_id, h.matched) for h in hits] == [("B", "both"), ("V", "vector"), ("F", "fulltext")]
    assert hits[0].score == pytest.approx(VECTOR_WEIGHT / (RRF_K + 2) + 1 / (RRF_K + 2))
    assert hits[1].score == pytest.approx(VECTOR_WEIGHT / (RRF_K + 1))  # the vector list weighs double
    assert hits[2].score == pytest.approx(1 / (RRF_K + 1))


async def test_fusion_ties_break_by_spec_id_and_the_limit_cuts_after_fusion() -> None:
    # Vector rank 12 (2 / 22) ties fulltext rank 1 (1 / 11); uuids sort opposite to spec_ids.
    vector = [hit(f"V{index:02d}", 0.9) for index in range(1, 12)] + [hit("A", 0.9)]
    executor = search_executor(fulltext=[hit("Z", 3.0)], vector=vector)
    found = [h.node.spec_id for h in await reader(executor, FakeGateway()).find_nodes(PROJECT, "x", limit=13)]
    assert found == [f"V{index:02d}" for index in range(1, 12)] + ["A", "Z"]
    # The limit cuts after fusion: at 12, the tie's loser is the one left out.
    found = [h.node.spec_id for h in await reader(executor, FakeGateway()).find_nodes(PROJECT, "x", limit=12)]
    assert found[-1] == "A"


async def test_each_list_is_as_deep_as_the_limit() -> None:
    executor = search_executor(fulltext=[], vector=[])
    await reader(executor, FakeGateway()).find_nodes(PROJECT, "x", limit=7)
    params = dict(executor.calls)
    assert params["search_nodes_fulltext"]["page"] == 7
    assert params["search_nodes_vector_exact"]["limit"] == 7


# --- vector floor (M6 tuning) -------------------------------------------------------------------


async def test_vector_only_hits_below_the_floor_are_dropped() -> None:
    executor = search_executor(
        fulltext=[hit("T", 4.0), hit("BOTH_LOW", 3.0)],
        vector=[hit("HIGH", 0.61), hit("EDGE", DEFAULT_VECTOR_FLOOR), hit("BOTH_LOW", 0.52), hit("LOW", 0.59)],
    )
    hits = await reader(executor, FakeGateway()).find_nodes(PROJECT, "x", limit=10)
    found = {h.node.spec_id: h.matched for h in hits}
    assert found == {"HIGH": "vector", "EDGE": "vector", "BOTH_LOW": "both", "T": "fulltext"}


async def test_an_unrelated_query_returns_no_vector_only_hits() -> None:
    executor = search_executor(fulltext=[], vector=[hit("A", 0.51), hit("B", 0.50), hit("C", 0.47)])
    assert await reader(executor, FakeGateway()).find_nodes(PROJECT, "unrelated", limit=5) == []


@pytest.mark.parametrize(("floor", "kept"), [(0.0, ["A", "B", "C"]), (0.5, ["A", "B"]), (1.0, [])])
async def test_vector_floor_is_a_reader_option(floor: float, kept: list[str]) -> None:
    executor = search_executor(fulltext=[], vector=[hit("A", 0.51), hit("B", 0.50), hit("C", 0.47)])
    hits = await GraphReader(executor, ONT, FakeGateway(), vector_floor=floor).find_nodes(PROJECT, "x", limit=5)
    assert [h.node.spec_id for h in hits] == kept


@pytest.mark.parametrize("floor", [-0.01, 1.01, True, "0.6", None])
def test_vector_floor_must_be_0_to_1(floor: Any) -> None:
    with pytest.raises(ConfigurationError) as raised:
        GraphReader(ScriptedExecutor({}), ONT, vector_floor=floor)
    assert raised.value.code == "invalid_vector_floor"


# --- FR-READ-03 / H2: when the vector list is left out -------------------------------------------


async def test_without_gateway_results_are_fulltext_only() -> None:
    executor = search_executor(fulltext=[hit("F", 2.0), hit("B", 1.0)])
    hits = await reader(executor).find_nodes(PROJECT, "x")
    assert [(h.node.spec_id, h.matched) for h in hits] == [("F", "fulltext"), ("B", "fulltext")]
    assert "count_search_vectors" not in executor.names()


async def test_embedding_failure_falls_back_to_fulltext(monkeypatch: pytest.MonkeyPatch) -> None:
    logged = record_events(monkeypatch)
    executor = search_executor(fulltext=[hit("F", 2.0)], vector=[hit("V", 0.9)])
    gateway = FakeGateway(fail=LLMError("overflow", call_class="embedding"))
    hits = await reader(executor, gateway).find_nodes(PROJECT, "secret query text")
    assert [(h.node.spec_id, h.matched) for h in hits] == [("F", "fulltext")]
    assert "search_nodes_vector_exact" not in executor.names()
    assert logged == [("graph.read.vector_unavailable", {"error_code": "llm_overflow"})]


@pytest.mark.parametrize(
    "failure", [LLMError("exhausted"), LLMError("overflow"), InfrastructureUnavailableError("llm_unreachable")]
)
async def test_non_fatal_embedding_errors_fall_back_to_fulltext(failure: Exception) -> None:
    executor = search_executor(fulltext=[hit("F", 2.0)], vector=[hit("V", 0.9)])
    hits = await reader(executor, FakeGateway(fail=failure)).find_nodes(PROJECT, "x")
    assert [(h.node.spec_id, h.matched) for h in hits] == [("F", "fulltext")]


async def test_fatal_embedding_error_raises_from_find_nodes() -> None:
    executor = search_executor(fulltext=[hit("F", 2.0)], vector=[hit("V", 0.9)])
    with pytest.raises(LLMFatalError):
        await reader(executor, FakeGateway(fail=LLMFatalError(status=401))).find_nodes(PROJECT, "x")
    assert "search_nodes_vector_exact" not in executor.names()


async def test_embedding_model_mismatch_falls_back_to_fulltext_without_an_embed_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged = record_events(monkeypatch)
    executor = search_executor(fulltext=[hit("F", 2.0)], vector=[hit("V", 0.9)], matching=0, stored=5)
    gateway = FakeGateway(model="other-model")
    hits = await reader(executor, gateway).find_nodes(PROJECT, "x")
    assert [(h.node.spec_id, h.matched) for h in hits] == [("F", "fulltext")]
    assert gateway.texts == []
    assert dict(executor.calls)["count_search_vectors"]["model"] == "other-model"
    assert logged == [("graph.read.embedding_model_mismatch", {"vectors": 5})]


async def test_the_embedded_text_is_capped() -> None:
    gateway = FakeGateway()
    await reader(search_executor(fulltext=[], vector=[]), gateway).find_nodes(PROJECT, "word " * 5000)
    assert len(gateway.texts) == 1 and len(gateway.texts[0]) <= MAX_QUERY_CHARS


def record_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    logged: list[tuple[str, dict[str, Any]]] = []

    def capture(logger: Any, level: int, event: str, *args: Any, **fields: Any) -> None:
        if level >= 30:
            logged.append((event, fields))

    monkeypatch.setattr(reader_module, "log_event", capture)
    return logged


# --- H2: the vector path -----------------------------------------------------------------------


def vector_executor(matching: int, pages: list[tuple[int, list[dict[str, Any]]]]) -> ScriptedExecutor:
    """``matching`` candidates; the index answers ``pages`` in turn as ``(fetched, hits)``."""

    answers = iter(pages)
    return ScriptedExecutor(
        {
            "search_nodes_fulltext": lambda _params: page([]),
            "count_search_vectors": lambda _params: [{"matching": matching, "stored": matching}],
            "search_nodes_vector": lambda _params: page(*reversed(next(answers))),
            "search_nodes_vector_exact": lambda _params: [hit("EXACT", 0.9)],
            "read_nodes_by_uuid": hydrate,
        }
    )


async def test_at_most_2000_candidates_use_exact_cosine_without_the_index() -> None:
    executor = vector_executor(2000, [])
    hits = await reader(executor, FakeGateway()).find_nodes(PROJECT, "x", limit=2)
    assert "search_nodes_vector" not in executor.names()
    assert [h.node.spec_id for h in hits] == ["EXACT"]


async def test_index_fetch_doubles_while_hits_are_below_the_limit() -> None:
    executor = vector_executor(2001, [(2, []), (4, [hit("A", 0.9)]), (8, [hit("A", 0.9), hit("B", 0.8)])])
    hits = await reader(executor, FakeGateway()).find_nodes(PROJECT, "x", limit=2)
    ks = [params["k"] for name, params in executor.calls if name == "search_nodes_vector"]
    assert ks == [2, 4, 8]  # depth = limit
    assert "search_nodes_vector_exact" not in executor.names()
    assert [h.node.spec_id for h in hits] == ["A", "B"]


async def test_index_short_of_k_falls_back_to_exact_cosine() -> None:
    executor = vector_executor(5000, [(1, [hit("A", 0.9)])])
    hits = await reader(executor, FakeGateway()).find_nodes(PROJECT, "x", limit=2)
    assert executor.names().count("search_nodes_vector") == 1
    assert [h.node.spec_id for h in hits] == ["EXACT"]


async def test_index_after_three_doublings_falls_back_to_exact_cosine() -> None:
    executor = vector_executor(5000, [(2, []), (4, []), (8, []), (16, [hit("A", 0.9)])])
    await reader(executor, FakeGateway()).find_nodes(PROJECT, "x", limit=2)
    assert executor.names().count("search_nodes_vector") == 4
    assert executor.names()[-2:] == ["search_nodes_vector_exact", "read_nodes_by_uuid"]


# --- H2: fulltext paging -----------------------------------------------------------------------


def paged_fragments(pages: list[tuple[int, list[dict[str, Any]]]]) -> ScriptedExecutor:
    answers = iter(pages)

    def fragments(params: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "props": {"fragment_id": fragment_id, "document_id": "d", "status": "active"},
                "document": {"document_id": "d", "path": "a.md"},
            }
            for fragment_id in params["fragment_ids"]
        ]

    return ScriptedExecutor(
        {"search_fragments": lambda _params: page(*reversed(next(answers))), "read_fragment_hits": fragments}
    )


def fragment_hit(fragment_id: str, score: float) -> dict[str, Any]:
    return {"fragment_id": fragment_id, "score": score}


async def test_fulltext_pages_double_in_score_order_until_enough_project_hits() -> None:
    executor = paged_fragments(
        [(3, []), (6, [fragment_hit("b", 2.0)]), (12, [fragment_hit("a", 2.0), fragment_hit("c", 1.0)])]
    )
    hits = await reader(executor).find_fragments(PROJECT, "x", limit=3)
    pages = [(params["skip"], params["page"]) for name, params in executor.calls if name == "search_fragments"]
    assert pages == [(0, 3), (3, 6), (9, 12)]
    # Equal scores across pages break by fragment id.
    assert [h.fragment.fragment_id for h in hits] == ["a", "b", "c"]
    assert hits[0].document is not None and hits[0].document.path == "a.md"


async def test_fulltext_paging_stops_when_the_index_has_no_more_matches() -> None:
    executor = paged_fragments([(3, [fragment_hit("a", 1.0)]), (4, [])])
    hits = await reader(executor).find_fragments(PROJECT, "x", limit=3)
    assert executor.names().count("search_fragments") == 2
    assert [h.fragment.fragment_id for h in hits] == ["a"]


@pytest.mark.parametrize("text", ["?? !!", "the", "The AND of it"])
async def test_text_without_terms_runs_no_fulltext_query(text: str) -> None:
    executor = paged_fragments([])
    assert await reader(executor).find_fragments(PROJECT, text) == []
    assert executor.calls == []


async def test_stop_words_only_leave_the_vector_list() -> None:
    executor = search_executor(fulltext=[hit("F", 2.0)], vector=[hit("V", 0.9)])
    hits = await reader(executor, FakeGateway()).find_nodes(PROJECT, "the", limit=3)
    assert [(h.node.spec_id, h.matched) for h in hits] == [("V", "vector")]
    assert "search_nodes_fulltext" not in executor.names()


@pytest.mark.parametrize("method", ["find_nodes", "find_fragments"])
async def test_a_query_lucene_rejects_is_query_too_long(method: str) -> None:
    def rejected(params: Mapping[str, Any]) -> list[dict[str, Any]]:
        raise ConformanceError("neo4j_query_rejected", neo4j_code=PROCEDURE_FAILED)

    executor = ScriptedExecutor({"search_nodes_fulltext": rejected, "search_fragments": rejected})
    with pytest.raises(ConfigurationError) as raised:
        await getattr(reader(executor), method)(PROJECT, "x")
    assert raised.value.code == "query_too_long"


async def test_other_query_rejections_stay_conformance_errors() -> None:
    def rejected(params: Mapping[str, Any]) -> list[dict[str, Any]]:
        raise ConformanceError("neo4j_query_rejected", neo4j_code="Neo.ClientError.Statement.SyntaxError")

    with pytest.raises(ConformanceError):
        await reader(ScriptedExecutor({"search_fragments": rejected})).find_fragments(PROJECT, "x")


# --- H4 / FR-READ-08: validation (before any query) ---------------------------------------------


@pytest.mark.parametrize("limit", [0, -1, MAX_SEARCH_LIMIT + 1, True, 2.5])
@pytest.mark.parametrize("method", ["find_nodes", "find_fragments"])
async def test_search_limit_must_be_1_to_500(method: str, limit: Any) -> None:
    executor = ScriptedExecutor({})
    with pytest.raises(ConfigurationError) as raised:
        await getattr(reader(executor), method)(PROJECT, "x", limit=limit)
    assert raised.value.code == "invalid_limit"
    assert executor.calls == []


@pytest.mark.parametrize(
    ("where", "code"),
    [
        ({"no_such_attribute": "x"}, "unknown_attribute"),
        ({"is_assumption": "maybe"}, "invalid_attribute_value"),
        ({"module": "Checkout"}, "unknown_attribute"),  # declared, never stored
    ],
)
async def test_where_names_and_enum_values_are_checked(where: dict[str, Any], code: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        await reader(ScriptedExecutor({})).find_nodes(PROJECT, "x", labels=[FR], where=where)
    assert raised.value.code == code


async def test_where_none_means_property_absent_and_passes_validation() -> None:
    executor = ScriptedExecutor({"read_nodes": lambda _params: []})
    await reader(executor).nodes(PROJECT, labels=[FR], where={"is_assumption": None, "stmt": None})
    assert dict(executor.calls)["read_nodes"]["where"] == {"is_assumption": None, "stmt": None}


async def test_unknown_label_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as raised:
        await reader(ScriptedExecutor({})).find_nodes(PROJECT, "x", labels=["Finding"])
    assert raised.value.code == "unknown_label"


async def test_blank_text_searches_nothing() -> None:
    executor = ScriptedExecutor({})
    assert await reader(executor, FakeGateway()).find_nodes(PROJECT, "   ") == []
    assert await reader(executor).find_fragments(PROJECT, "") == []
    assert executor.calls == []


@pytest.mark.parametrize("depth", [0, 4, True, 1.0])
async def test_impact_depth_must_be_1_to_3(depth: Any) -> None:
    with pytest.raises(ConfigurationError) as raised:
        await reader(ScriptedExecutor({})).impact(PROJECT, ["X"], depth=depth)
    assert raised.value.code == "invalid_depth"


async def test_impact_relations_must_be_domain_relations() -> None:
    with pytest.raises(ConfigurationError) as raised:
        await reader(ScriptedExecutor({})).impact(PROJECT, ["X"], relations=["REFERENCED_BY"])
    assert raised.value.code == "unknown_relation"


@pytest.mark.parametrize(("start", "end"), [(0, 3), (5, 4), (1.5, 3), (True, 3), (1, None)])
async def test_line_range_must_be_valid(start: Any, end: Any) -> None:
    with pytest.raises(ConfigurationError) as raised:
        await reader(ScriptedExecutor({})).nodes_for_fragment(PROJECT, "d", start, end)
    assert raised.value.code == "invalid_line_range"


# --- FR-READ-05 / H5: impact truncation ---------------------------------------------------------


@pytest.mark.parametrize("reachable", [499, 500, 501, 600])
async def test_impact_returns_at_most_500_nearest_first_and_flags_truncation(reachable: int) -> None:
    def hop(params: Mapping[str, Any]) -> list[dict[str, Any]]:
        if params["frontier"] != [uuid_of("H")]:
            return []
        rows = [{"uuid": uuid_of(f"N{index:03d}"), "spec_id": f"N{index:03d}"} for index in range(1, reachable + 1)]
        return rows[: params["limit"]]

    executor = ScriptedExecutor(
        {
            "read_node_uuids_by_spec_id": lambda _params: [{"uuid": uuid_of("H"), "spec_id": "H"}],
            "read_impact_hop": hop,
            "read_nodes_by_uuid": hydrate,
        }
    )
    result = await reader(executor).impact(PROJECT, ["H"], depth=3)
    assert len(result.nodes) == min(reachable, 500)
    assert result.truncated is (reachable > 500)
    assert result.sources == ("H",)
    assert ids_in_order(result.nodes) == [f"N{index:03d}" for index in range(1, min(reachable, 500) + 1)]
    limits = [params["limit"] for name, params in executor.calls if name == "read_impact_hop"]
    assert limits[0] == 501
    # Truncated: no further hop; else the next hop starts from the found nodes.
    assert executor.names().count("read_impact_hop") == (1 if reachable > 500 else 2)
    assert set(result.distance.values()) == {1}


def ids_in_order(nodes: Any) -> list[str]:
    return [node.spec_id for node in nodes]


# --- H1 / FR-READ-07: scope queries from the spine paths ----------------------------------------


def test_scope_queries_follow_the_shipped_spine_paths() -> None:
    queries = reader(ScriptedExecutor({}))._scope_queries
    assert sorted(query.name for query in queries) == [
        "read_scope_sequence_GROUPED_BY",
        "read_scope_sequence_GROUPED_BY_REALIZED_BY",
        "read_scope_sequence_REALIZED_BY",
        "read_scope_via_PART_OF_1_3",
        "read_scope_via_RELATES_TO_1_1",
    ]
    assert all(query.binds_project_id() for query in queries)
    by_name = {query.name: query.text for query in queries}
    assert "-[r:PART_OF WHERE r.group_id = $project_id]-()" in by_name["read_scope_via_PART_OF_1_3"]
    assert "{1,3}(root)" in by_name["read_scope_via_PART_OF_1_3"]
    assert "{1,1}(root)" in by_name["read_scope_via_RELATES_TO_1_1"]
    sequence = by_name["read_scope_sequence_GROUPED_BY_REALIZED_BY"]
    assert "(n:SpecNode)-[r0:GROUPED_BY]->(x0:SpecNode)-[r1:REALIZED_BY]->(root)" in sequence
    assert "none(label IN labels(x0) WHERE label IN $roots)" in sequence


def test_sequence_parts_are_every_contiguous_run_and_symmetric_steps_go_either_way() -> None:
    query = scope_sequence(("GROUPED_BY", "RELATES_TO", "REALIZED_BY"), ("RELATES_TO",))
    assert "-[r0:GROUPED_BY]->(x0:SpecNode)-[r1:RELATES_TO]-(x1:SpecNode)-[r2:REALIZED_BY]->(root)" in query.text
    for index in (0, 1):
        assert f"none(label IN labels(x{index}) WHERE label IN $roots)" in query.text


def test_via_walks_each_edge_towards_the_root_unless_symmetric() -> None:
    text = scope_via(("PART_OF",), 1, 3).text
    assert "length(p) <= $max_hops" in text
    assert "all(i IN range(0, size(r) - 1) WHERE type(r[i]) IN $symmetric OR startNode(r[i]) = nodes(p)[i])" in text
    assert "all(b IN nodes(p)[1..] WHERE b = root OR none(label IN labels(b) WHERE label IN $roots))" in text


@pytest.mark.parametrize("bad", ["Bad name", "X) DETACH DELETE (n", ""])
def test_scope_factories_refuse_unsafe_names(bad: str) -> None:
    with pytest.raises(ConformanceError):
        scope_via((bad,), 1, 1)
    with pytest.raises(ConformanceError):
        scope_sequence(("PART_OF", bad), ())
    with pytest.raises(ConformanceError):
        scope_sequence(("PART_OF",), (bad,))


async def test_scope_unions_every_path_query_and_reads_each_node_once() -> None:
    answers: dict[str, Answer] = {
        "read_node_by_spec_id": lambda _params: [node_row("M", "Module")],
        "read_nodes_by_uuid": hydrate,
    }
    for query in reader(ScriptedExecutor({}))._scope_queries:
        answers[query.name] = lambda _params, name=query.name: [{"uuid": uuid_of("SHARED")}, {"uuid": uuid_of(name)}]
    executor = ScriptedExecutor(answers)
    nodes = await reader(executor).scope(PROJECT, "M")
    assert executor.names().count("read_nodes_by_uuid") == 1
    assert len(nodes) == len({node.spec_id for node in nodes}) == 6
    scope_params = [params for name, params in executor.calls if name.startswith("read_scope")]
    assert all(params["roots"] == ["Module"] and params["symmetric"] == ["RELATES_TO"] for params in scope_params)


# --- explicit ids and spec_ids in the query (scale-report F8) ------------------------------------


def id_key(explicit: str) -> str:
    """The identity key of an explicit id, as the identity rules form it."""

    label = ONT.label_for_id_family(explicit)
    assert label is not None
    return f"{ONT.identity_rule(label).scope}|id|{explicit}"


def named_row(spec_id: str, keys: list[str], aliases: list[str] = ()) -> dict[str, Any]:
    return {"uuid": uuid_of(spec_id), "spec_id": spec_id, "keys": keys, "spec_ids": [spec_id, *aliases]}


def id_executor(
    named: list[dict[str, Any]], fulltext: list[dict[str, Any]], vector: list[dict[str, Any]] | None = None
) -> ScriptedExecutor:
    executor = search_executor(fulltext=fulltext, vector=vector)
    executor.answers["read_nodes_by_ids"] = lambda _params: list(named)
    return executor


@pytest.mark.parametrize("gateway", [None, "fused"])
async def test_nodes_named_by_explicit_id_rank_first_in_query_order(gateway: Any) -> None:
    named = [
        named_row("REQ::FR::FR_AUTH_012", [id_key("FR-AUTH-012")]),
        named_row("REQ::NFR::NFR_7", [id_key("NFR-7")]),
    ]
    executor = id_executor(
        named, fulltext=[hit("OTHER", 9.0), hit("REQ::FR::FR_AUTH_012", 1.0)], vector=[hit("V", 0.9)]
    )
    reader_ = reader(executor, FakeGateway() if gateway else None)
    hits = await reader_.find_nodes(PROJECT, "how does NFR_7 relate to fr? FR-AUTH-012", limit=3)
    assert [(h.node.spec_id, h.matched) for h in hits] == [
        ("REQ::NFR::NFR_7", "id"),  # named first in the text
        ("REQ::FR::FR_AUTH_012", "id"),  # not repeated among the fused hits
        ("V" if gateway else "OTHER", "vector" if gateway else "fulltext"),
    ]
    assert hits[0].score == hits[1].score == reader_module.ID_HIT_SCORE
    params = dict(executor.calls)["read_nodes_by_ids"]
    assert params["keys"] == [id_key("NFR-7"), id_key("FR-AUTH-012")]
    assert params["spec_ids"] == []


async def test_spec_ids_and_aliases_in_the_query_name_nodes() -> None:
    named = [named_row("MOD::CHECKOUT", ["Module|surface|checkout"], aliases=["MOD::TILL"])]
    executor = id_executor(named, fulltext=[])
    hits = await reader(executor).find_nodes(PROJECT, "what is under MOD::TILL?")
    assert [(h.node.spec_id, h.matched) for h in hits] == [("MOD::CHECKOUT", "id")]
    assert dict(executor.calls)["read_nodes_by_ids"]["spec_ids"] == ["MOD::TILL"]


async def test_identity_alias_names_a_node() -> None:
    named = [named_row("REQ::FR::FR_A_1", [id_key("FR-A-1"), id_key("FR-OLD-9")])]
    hits = await reader(id_executor(named, fulltext=[])).find_nodes(PROJECT, "FR-OLD-9")
    assert [(h.node.spec_id, h.matched) for h in hits] == [("REQ::FR::FR_A_1", "id")]


async def test_the_limit_applies_to_id_hits_too() -> None:
    named = [named_row(f"REQ::FR::FR_{index}", [id_key(f"FR-{index}")]) for index in (1, 2, 3)]
    hits = await reader(id_executor(named, fulltext=[hit("X", 1.0)])).find_nodes(PROJECT, "FR-3 FR-1 FR-2", limit=2)
    assert [h.node.spec_id for h in hits] == ["REQ::FR::FR_3", "REQ::FR::FR_1"]


async def test_filters_are_passed_to_the_id_lookup() -> None:
    executor = id_executor([], fulltext=[])
    await reader(executor).find_nodes(PROJECT, "FR-1", labels=[FR], where={"is_assumption": "yes"})
    params = dict(executor.calls)["read_nodes_by_ids"]
    assert (params["labels"], params["where"]) == ([FR], {"is_assumption": "yes"})


@pytest.mark.parametrize("text", ["checkout totals", "fr-1 lower case", "SFR-1", "DATA-2024-q1", "XYZ::NOT_A_PREFIX"])
async def test_text_without_ids_runs_no_id_lookup(text: str) -> None:
    executor = id_executor([], fulltext=[hit("X", 1.0)])
    hits = await reader(executor).find_nodes(PROJECT, text)
    assert "read_nodes_by_ids" not in executor.names()
    assert [h.matched for h in hits] == ["fulltext"]


async def test_an_id_naming_no_node_falls_back_to_the_fused_results() -> None:
    hits = await reader(id_executor([], fulltext=[hit("X", 1.0)])).find_nodes(PROJECT, "FR-404")
    assert [(h.node.spec_id, h.matched) for h in hits] == [("X", "fulltext")]


# --- FR-MCP-TOOL-10: paged read -------------------------------------------------------------------


def paged_executor(spec_ids: list[str], label: str = FR) -> ScriptedExecutor:
    """Answers ``read_nodes_page`` like the database (ids after ``after``, sorted, the first ``limit``) and
    ``count_nodes`` with the number of ids."""

    def page_rows(params: Mapping[str, Any]) -> list[dict[str, Any]]:
        after = params["after"]
        ordered = sorted(i for i in spec_ids if after is None or i > after)
        return [node_row(i, label) for i in ordered[: params["limit"]]]

    return ScriptedExecutor({"read_nodes_page": page_rows, "count_nodes": lambda _params: [{"total": len(spec_ids)}]})


def fr_ids(count: int) -> list[str]:
    return [f"FR-{n:03d}" for n in range(1, count + 1)]


async def test_fr_mcp_tool_10_the_page_reads_limit_plus_one_rows_with_its_parameters() -> None:
    """FR-MCP-TOOL-10 / FR-MCP-TOOL-04: ``limit + 1`` is the query's limit, ``read_nodes`` is never used."""

    executor = paged_executor(fr_ids(3))
    result = await reader(executor).nodes_page(PROJECT, [FR])
    assert executor.calls == [("read_nodes_page", {"labels": [FR], "after": None, "limit": 51, "retired": "retired"})]
    assert executor.project_ids == [PROJECT]
    assert [n.spec_id for n in result.items] == fr_ids(3)
    assert "read_nodes" not in executor.names()


async def test_fr_mcp_tool_10_the_page_limit_parameter_is_limit_plus_one_and_after_is_passed() -> None:
    """FR-MCP-TOOL-10 / FR-MCP-TOOL-04: ``limit=2`` binds 3, ``after`` is passed as given."""

    executor = paged_executor(fr_ids(3))
    await reader(executor).nodes_page(PROJECT, [FR], limit=2, after="FR-001")
    assert executor.calls[0][1]["limit"] == 3
    assert executor.calls[0][1]["after"] == "FR-001"
    assert "read_nodes" not in executor.names()


async def test_fr_mcp_tool_10_count_binds_the_project_and_labels() -> None:
    """FR-MCP-TOOL-10: ``count_nodes`` sends the labels, binds the project, and returns the total."""

    executor = paged_executor(fr_ids(7))
    assert await reader(executor).count_nodes(PROJECT, [FR]) == 7
    assert executor.calls == [("count_nodes", {"labels": [FR]})]
    assert executor.project_ids == [PROJECT]


async def test_fr_mcp_tool_10_count_of_no_rows_is_zero() -> None:
    """FR-MCP-TOOL-10: an answer without rows counts 0."""

    executor = ScriptedExecutor({"count_nodes": lambda _params: []})
    assert await reader(executor).count_nodes(PROJECT, [FR]) == 0


async def test_fr_mcp_tool_10_120_nodes_page_as_50_50_20() -> None:
    """FR-MCP-TOOL-10: 120 nodes by 50 give 50, 50, 20; ``next_after`` is None at the end; count 120."""

    executor = paged_executor(fr_ids(120))
    r = reader(executor)
    pages: list[NodePage] = []
    after: str | None = None
    while True:
        current = await r.nodes_page(PROJECT, [FR], limit=50, after=after)
        pages.append(current)
        after = current.next_after
        if after is None:
            break
    assert [len(p.items) for p in pages] == [50, 50, 20]
    assert [p.next_after for p in pages] == ["FR-050", "FR-100", None]
    assert [n.spec_id for p in pages for n in p.items] == fr_ids(120)
    assert await r.count_nodes(PROJECT, [FR]) == 120


async def test_fr_mcp_tool_10_exactly_limit_rows_left_gives_no_next_after() -> None:
    """FR-MCP-TOOL-10: exactly ``limit`` rows left is the last page."""

    result = await reader(paged_executor(fr_ids(50))).nodes_page(PROJECT, [FR], limit=50)
    assert (len(result.items), result.next_after) == (50, None)


async def test_fr_mcp_tool_10_one_row_more_than_limit_gives_the_last_kept_spec_id() -> None:
    """FR-MCP-TOOL-10: ``limit + 1`` rows give ``limit`` items and ``next_after`` their last ``spec_id``."""

    result = await reader(paged_executor(fr_ids(51))).nodes_page(PROJECT, [FR], limit=50)
    assert (len(result.items), result.next_after) == (50, "FR-050")


async def test_fr_mcp_tool_10_an_empty_project_gives_an_empty_page() -> None:
    """FR-MCP-TOOL-10: no nodes give no items, no ``next_after``, count 0."""

    r = reader(paged_executor([]))
    assert await r.nodes_page(PROJECT, [FR]) == NodePage(items=[], next_after=None)
    assert await r.count_nodes(PROJECT, [FR]) == 0


async def test_fr_mcp_tool_10_a_skipped_malformed_row_cannot_hide_the_next_page() -> None:
    """FR-MCP-TOOL-10: ``next_after`` follows the raw rows; a row without a spec label is skipped from items only."""

    rows = [node_row("FR-001"), {**node_row("FR-002"), "labels": ["SpecNode"]}, node_row("FR-003")]
    executor = ScriptedExecutor({"read_nodes_page": lambda params: rows[: params["limit"]]})
    result = await reader(executor).nodes_page(PROJECT, [FR], limit=2)
    assert [n.spec_id for n in result.items] == ["FR-001"]
    assert result.next_after == "FR-002"


@pytest.mark.parametrize("limit", [0, -1, 501, True, 2.5, "10", None])
async def test_fr_mcp_tool_10_an_invalid_limit_raises_before_any_query(limit: Any) -> None:
    """FR-MCP-TOOL-10: ``limit`` is an integer from 1 to 500, else ``invalid_limit``."""

    executor = paged_executor(fr_ids(3))
    with pytest.raises(ConfigurationError) as raised:
        await reader(executor).nodes_page(PROJECT, [FR], limit=limit)
    assert raised.value.code == "invalid_limit"
    assert executor.calls == []


@pytest.mark.parametrize("limit", [1, 500])
async def test_fr_mcp_tool_10_the_limit_bounds_are_accepted(limit: int) -> None:
    """FR-MCP-TOOL-10: 1 and 500 are valid."""

    executor = paged_executor(fr_ids(3))
    await reader(executor).nodes_page(PROJECT, [FR], limit=limit)
    assert executor.calls[0][1]["limit"] == limit + 1


@pytest.mark.parametrize("labels", [["Finding"], ["Document"], [FR, "Nope"], "SpecNode"])
async def test_fr_mcp_tool_10_unknown_and_non_spec_labels_raise_for_both_methods(labels: Any) -> None:
    """FR-MCP-TOOL-10: labels are validated as in ``nodes`` (spec labels only); no query runs."""

    executor = paged_executor(fr_ids(3))
    r = reader(executor)
    with pytest.raises(ConfigurationError) as page_error:
        await r.nodes_page(PROJECT, labels)
    with pytest.raises(ConfigurationError) as count_error:
        await r.count_nodes(PROJECT, labels)
    assert (page_error.value.code, count_error.value.code) == ("unknown_label", "unknown_label")
    assert executor.calls == []


async def test_fr_mcp_tool_10_none_means_every_spec_label_and_an_empty_list_none() -> None:
    """FR-MCP-TOOL-10: ``labels`` None selects every spec label, ``[]`` matches nothing (as in ``nodes``)."""

    executor = paged_executor([])
    r = reader(executor)
    await r.nodes_page(PROJECT, None)
    await r.nodes_page(PROJECT, [])
    assert executor.calls[0][1]["labels"] == list(r._spec_labels)
    assert executor.calls[1][1]["labels"] == []


async def test_fr_mcp_tool_10_an_invalid_project_id_raises_before_any_query() -> None:
    """FR-MCP-TOOL-10: the project id is validated first."""

    executor = paged_executor(fr_ids(1))
    with pytest.raises(ConfigurationError):
        await reader(executor).nodes_page("", [FR])
    with pytest.raises(ConfigurationError):
        await reader(executor).count_nodes("", [FR])
    assert executor.calls == []


async def test_fr_mcp_tool_10_the_renamed_feature_ontology_loads_and_pages_its_own_label(tmp_path: Path) -> None:
    """FR-MCP-TOOL-10 / FR-MCP-TOOL-06: only the feature label and prefix change; the old label is unknown."""

    renamed = load_feature_renamed_ontology(tmp_path)
    shipped = shipped_ontology()
    feature = renamed.node_types["feature"]
    assert (feature.label, feature.id_prefix) == (RENAMED_FEATURE_LABEL, RENAMED_FEATURE_PREFIX)
    assert shipped.node_types["feature"].label not in renamed.labels
    for key, node_type in shipped.node_types.items():
        if key != "feature":
            assert (renamed.node_types[key].label, renamed.node_types[key].id_prefix) == (
                node_type.label,
                node_type.id_prefix,
            )
    executor = paged_executor(["FUNK::A"], RENAMED_FEATURE_LABEL)
    r = GraphReader(executor, renamed)
    result = await r.nodes_page(PROJECT, [RENAMED_FEATURE_LABEL])
    assert [n.label for n in result.items] == [RENAMED_FEATURE_LABEL]
    assert executor.calls[0][1]["labels"] == [RENAMED_FEATURE_LABEL]
    with pytest.raises(ConfigurationError) as raised:
        await r.nodes_page(PROJECT, [shipped.node_types["feature"].label])
    assert raised.value.code == "unknown_label"
