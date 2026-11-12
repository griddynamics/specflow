"""``GraphReader.similar`` (stored-vector neighbours, no embedding call) and ``GraphReader.evidence`` (the source lines
behind a node) (api.md "Read and search")."""

from __future__ import annotations

from typing import Any

import pytest

from graph.core import ConfigurationError
from graph_reads_support import MODEL, PROJECT, Gateway, ReadsExecutor, cosine_score
from graph_trace_support import shop_graph

# --- similar --------------------------------------------------------------------------------------------------


def vector_executor() -> ReadsExecutor:
    executor = ReadsExecutor(shop_graph().alias("FEAT::CHECKOUT", "FEAT::PAY_AND_GO"))
    executor.vectors = {
        "FEAT::CHECKOUT": (MODEL, (1.0, 0.0, 0.0)),
        "REQ::FR::CHECKOUT": (MODEL, (0.9, 0.1, 0.0)),
        "FEAT::CHECKOUT_REVIEW": (MODEL, (0.95, 0.3, 0.0)),
        "FEAT::CARD_PAYMENT": (MODEL, (0.8, 0.6, 0.0)),  # has an edge to FEAT::CHECKOUT
        "REQ::FR::CARD_PAYMENT": (MODEL, (0.6, 0.8, 0.0)),
        "MOD::WEB_SHOP": (MODEL, (0.0, 0.0, 1.0)),
        "FEAT::GIFT_CARDS": ("other/model", (1.0, 0.0, 0.0)),
    }
    return executor


async def test_similar_lists_unlinked_nodes_that_read_like_a_source_best_first_with_their_best_source() -> None:
    executor = vector_executor()

    found = await executor.reader(Gateway()).similar(
        PROJECT, ["FEAT::PAY_AND_GO", "REQ::FR::CHECKOUT", "REQ::Q::CHECKOUT_SPEED", "FEAT::NOPE"]
    )

    assert found.sources == ("FEAT::CHECKOUT", "REQ::FR::CHECKOUT", "REQ::Q::CHECKOUT_SPEED")
    assert found.unknown == ("FEAT::NOPE",)
    assert [(item.spec_id, item.like) for item in found.items] == [
        ("FEAT::CHECKOUT_REVIEW", "REQ::FR::CHECKOUT"),
        ("REQ::FR::CARD_PAYMENT", "REQ::FR::CHECKOUT"),
    ]  # sources, the linked FEAT::CARD_PAYMENT, scores under 0.75 and other models are left out
    review = found.items[0]
    assert (review.label, review.name) == ("Feature", "Checkout review")
    assert review.score == pytest.approx(cosine_score((0.9, 0.1, 0.0), (0.95, 0.3, 0.0)))
    names = [name for name, _ in executor.calls]
    assert "embed" not in names and "read_neighbours" not in names
    assert names.count("read_trace_hop") == 1  # the linked nodes of every source in one read


async def test_similar_keeps_linked_nodes_on_request_and_applies_min_score_and_limit() -> None:
    reader = vector_executor().reader(Gateway())

    linked = await reader.similar(PROJECT, ["FEAT::CHECKOUT"], exclude_linked=False, min_score=0.5, limit=20)
    cut = await reader.similar(PROJECT, ["FEAT::CHECKOUT"], exclude_linked=False, min_score=0.85, limit=2)

    assert [item.spec_id for item in linked.items] == [
        "REQ::FR::CHECKOUT",
        "FEAT::CHECKOUT_REVIEW",
        "FEAT::CARD_PAYMENT",
        "REQ::FR::CARD_PAYMENT",
        "MOD::WEB_SHOP",
    ]
    assert [item.spec_id for item in cut.items] == ["REQ::FR::CHECKOUT", "FEAT::CHECKOUT_REVIEW"]


async def test_a_tie_names_the_lowest_source_and_items_tie_by_spec_id() -> None:
    executor = vector_executor()
    executor.vectors["REQ::FR::CHECKOUT"] = (MODEL, (1.0, 0.0, 0.0))
    executor.vectors["FEAT::CHECKOUT_REVIEW"] = (MODEL, (1.0, 0.1, 0.0))
    executor.vectors["FEAT::ORDER_HISTORY_EXPORT"] = (MODEL, (1.0, 0.1, 0.0))

    found = await executor.reader(Gateway()).similar(PROJECT, ["REQ::FR::CHECKOUT", "FEAT::CHECKOUT"], min_score=0.9)

    assert [(item.spec_id, item.like) for item in found.items] == [
        ("FEAT::CHECKOUT_REVIEW", "FEAT::CHECKOUT"),
        ("FEAT::ORDER_HISTORY_EXPORT", "FEAT::CHECKOUT"),
    ]


@pytest.mark.parametrize("gateway", [None, Gateway("another/model")])
async def test_without_a_gateway_or_vectors_of_its_model_similar_adds_nothing(gateway: Any) -> None:
    executor = vector_executor()

    found = await executor.reader(gateway).similar(PROJECT, ["FEAT::CHECKOUT"])

    assert found.sources == ("FEAT::CHECKOUT",) and found.items == ()
    assert "read_node_vectors" not in [name for name, _ in executor.calls]


@pytest.mark.parametrize(
    ("spec_ids", "options", "code"),
    [
        ([], {}, "invalid_spec_ids"),
        (["X"] * 51, {}, "invalid_spec_ids"),
        ("FEAT::CHECKOUT", {}, "invalid_spec_ids"),
        (["FEAT::CHECKOUT"], {"min_score": 1.5}, "invalid_min_score"),
        (["FEAT::CHECKOUT"], {"min_score": True}, "invalid_min_score"),
        (["FEAT::CHECKOUT"], {"limit": 0}, "invalid_limit"),
    ],
)
async def test_similar_checks_its_arguments_before_any_read(spec_ids: Any, options: Any, code: str) -> None:
    executor = vector_executor()

    with pytest.raises(ConfigurationError) as error:
        await executor.reader(Gateway()).similar(PROJECT, spec_ids, **options)

    assert error.value.code == code and executor.calls == []


# --- evidence -------------------------------------------------------------------------------------------------


def fragment(fragment_id: str, line_start: int, text: str, status: str = "active", document: str = "d") -> Any:
    lines = text.count("\n")
    return {
        "fragment_id": fragment_id,
        "document_id": document,
        "text": text,
        "line_start": line_start,
        "line_end": line_start + lines,
        "status": status,
    }


def evidence_executor() -> ReadsExecutor:
    executor = ReadsExecutor(shop_graph())
    executor.fragments = [
        fragment("f1", 1, "# Shop\nIntro line"),
        fragment("f2", 3, "Checkout is fast.\nIt runs in the EU.\nCards are accepted."),
        fragment("f3", 6, "Old text", status="retired"),
        fragment("f4", 7, "Last line"),
        fragment("g1", 1, "Another document", document="e"),
    ]
    at = {"document_id": "d", "path": "docs/shop.md"}
    executor.provenance = {
        "CON::EU_REGION": [
            {**at, "fragment_id": "f2", "line_start": 4, "line_end": 4},
            {**at, "fragment_id": "f1", "line_start": None, "line_end": None},
        ],
        "REQ::Q::CHECKOUT_SPEED": [{**at, "fragment_id": "f2", "line_start": 3, "line_end": 4}],
        "FEAT::GIFT_CARDS": [{**at, "fragment_id": "f3", "line_start": 6, "line_end": 6}],
    }
    return executor


async def test_evidence_quotes_the_lines_with_context_from_active_fragments_and_names_the_other_nodes() -> None:
    found = await evidence_executor().reader().evidence(PROJECT, "CON::EU_REGION")

    assert found is not None
    assert [(item.fragment_id, item.line_start, item.line_end) for item in found] == [("f1", 1, 2), ("f2", 4, 4)]
    first, second = found
    assert (first.quote, first.before, first.after) == (
        "# Shop\nIntro line",
        (),
        ("Checkout is fast.", "It runs in the EU."),
    )
    assert (second.quote, second.before) == ("It runs in the EU.", ("Intro line", "Checkout is fast."))
    assert second.after == ("Cards are accepted.",)  # line 6 is retired: no context from it, so line 7 is not next
    assert [(other.spec_id, other.label) for other in second.others] == [
        ("REQ::Q::CHECKOUT_SPEED", "QualityRequirement")
    ]
    assert (second.document_id, second.path) == ("d", "docs/shop.md")


async def test_context_lines_bound_the_context_and_never_pass_the_document() -> None:
    reader = evidence_executor().reader()

    none = await reader.evidence(PROJECT, "REQ::Q::CHECKOUT_SPEED", context_lines=0)
    five = await reader.evidence(PROJECT, "REQ::Q::CHECKOUT_SPEED", context_lines=5)

    assert none is not None and five is not None
    assert (none[0].quote, none[0].before, none[0].after) == ("Checkout is fast.\nIt runs in the EU.", (), ())
    assert five[0].before == ("# Shop", "Intro line")
    assert five[0].after == ("Cards are accepted.", "Last line")
    assert [other.spec_id for other in five[0].others] == ["CON::EU_REGION"]


async def test_a_retired_own_fragment_gives_no_item() -> None:
    # FEAT::GIFT_CARDS is grounded only on f3, a retired fragment (FR-READ-19)
    assert await evidence_executor().reader().evidence(PROJECT, "FEAT::GIFT_CARDS") == []


async def test_the_recorded_quote_stays_when_no_line_of_the_range_is_known() -> None:
    executor = evidence_executor()
    at = {"document_id": "d", "path": "docs/shop.md", "fragment_id": "gone"}
    executor.provenance["MOD::STRIPE"] = [{**at, "line_start": 20, "line_end": 21}]
    executor.attributes["MOD::STRIPE"] = {"statements": '[{"fragment_id": "gone", "text": "Stripe takes the card."}]'}

    found = await executor.reader().evidence(PROJECT, "MOD::STRIPE")

    assert found is not None
    assert [(item.quote, item.before, item.after) for item in found] == [("Stripe takes the card.", (), ())]


async def test_a_node_without_evidence_has_no_items_and_an_unknown_node_is_none() -> None:
    reader = evidence_executor().reader()

    assert await reader.evidence(PROJECT, "MOD::STRIPE") == []
    assert await reader.evidence(PROJECT, "FEAT::NOPE") is None


@pytest.mark.parametrize("context_lines", [-1, 6, True, "2"])
async def test_context_lines_outside_zero_to_five_are_refused_before_any_read(context_lines: Any) -> None:
    executor = evidence_executor()

    with pytest.raises(ConfigurationError) as error:
        await executor.reader().evidence(PROJECT, "CON::EU_REGION", context_lines=context_lines)

    assert error.value.code == "invalid_context_lines" and executor.calls == []
