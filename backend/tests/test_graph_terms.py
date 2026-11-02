"""Known terms, the hub catalog of the hub pass, name occurrences and the edge catalog offline."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest

from graph.build import edges, extract, terms
from graph.build.changes import EdgeWindowParams
from graph.build.models import (
    CanonicalNode,
    CatalogParts,
    ChangeSet,
    DocumentRec,
    FragmentRec,
    KnownTermsShown,
    PlanArtifact,
    RawNode,
    ResolveArtifact,
    Unit,
)
from graph.build.options import BuildOptions, StepServices
from graph.build.prompts import KnownTerm
from graph.build.queries import READ_PERSISTED_HUBS
from graph.build.resolve import hub_words
from graph.build.terms import EdgeCatalog, HubName, KnownTerms, NameIndex, body_lines, heading_section
from graph.build.vectors import VectorCache
from graph_support import (
    OfflineExecutor,
    ScriptedGateway,
    frozen_ontology,
)
from runtime_defaults_support import RUNTIME_DEFAULTS

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, neo4j_project) live in graph_support.
pytest_plugins = ["graph_support"]

# Mechanics tests: pinned to the frozen v1.3 ontology (graph_support.FROZEN_ONTOLOGY_FILE), not the shipped one.
ONT = frozen_ontology()
Factory = Callable[..., ScriptedGateway]
S0 = [1.0, 0.0, 0.0, 0.0]
S1 = [0.0, 1.0, 0.0, 0.0]


def fragment(fragment_id: str, text: str, ordinal: int = 0) -> FragmentRec:
    document_id = fragment_id.split("#", maxsplit=1)[0]
    return FragmentRec(
        fragment_id=fragment_id,
        document_id=document_id,
        heading_path=(document_id.title(), f"Section {ordinal}"),
        ordinal=ordinal,
        line_start=ordinal * 10 + 1,
        line_end=ordinal * 10 + 1,
        text=text,
        content_hash=f"hash-{fragment_id}",
        tokens=max(1, len(text) // 4),
    )


def unit_of(*fragments: FragmentRec) -> Unit:
    return Unit(
        unit_id=f"unit:{fragments[0].document_id}",
        document_id=fragments[0].document_id,
        fragment_ids=tuple(item.fragment_id for item in fragments),
        tokens=sum(item.tokens for item in fragments),
        content_hash="unit",
        windows=((0, len(fragments)),),
    )


def canonical(cid: str, name: str, label: str, fragments: Sequence[str]) -> CanonicalNode:
    return CanonicalNode(
        cid=cid,
        identity_key=f"{label}|surface|{name.casefold()}",
        name=name,
        label=label,
        member_ids=(cid,),
        fragment_ids=tuple(fragments),
        name_embedding_text=f"{label}: {name}",
    )


# NameIndex


def test_name_index_takes_the_longest_name_at_each_position_on_whole_tokens() -> None:
    index = NameIndex([("Orders", "orders"), ("Orders database", "db"), ("Order service", "service")])
    text = "The Orders database stores orders; the order-service and Order_Service. Reorders and ordersX."
    # "Orders database" wins over "Orders" where both start; occurrences never overlap; plurals
    # hyphens and underscores fold; "Reorders" and "ordersX" are other tokens
    assert index.count(hub_words(text)) == {"db": 1, "orders": 1, "service": 2}
    assert index.count(hub_words("database orders")) == {"orders": 1}  # words in order, contiguous
    assert index.count([]) == {}
    assert not NameIndex([("  ", "blank")])


def test_name_index_counts_every_key_of_names_with_equal_words() -> None:
    index = NameIndex([("Orders service", "a"), ("the orders-services", "b"), ("Orders service", "a")])
    assert index.count(hub_words("The Orders service.")) == {"a": 1, "b": 1}


def test_name_index_finds_hub_names_in_their_name_forms() -> None:
    """dotted and upper-case ``_`` names, ``-s`` singulars and mixed-case names meet the
    text in one form (``graph.build.hubforms``)."""

    index = NameIndex(
        [("order.paid", "event"), ("ORDER_ITEMS", "items"), ("Alias service", "alias"), ("DevOps", "devops")],
        [("cors", "cors")],  # a ``surface`` alias value
    )
    text = (
        "On order.paid (and Order.Paid) the ORDER_ITEMS rows, order items and Order_Item move; "
        "aliases service and the Alias Service; DevOps or devops; CORS"
    )
    assert index.count(hub_words(text)) == {"event": 2, "items": 3, "alias": 2, "devops": 2, "cors": 1}
    assert index.count(hub_words("order paid; orders paid; alia service")) == {"event": 2}


# Known terms

HUBS = [
    HubName(name="orders service", label="Module"),  # same surface and label as "Orders service": one term
    HubName(name="Orders service", label="Module", aliases=("OMS", "order backend")),
    HubName(name="Stripe", label="Module"),
    HubName(name="Ghost", label="Module"),  # no vector: found by mention only
    HubName(name="Audit logging", label="Capability", aliases=("audit trail", "AL", "audit log", "logs", "trail")),
    HubName(name="Search", label="Feature"),
    HubName(name="Reporting", label="Feature"),
    HubName(name="Inventory", label="Feature"),  # cosine 0 to every fragment: below the floor
    HubName(name="Billing", label="Module", aliases=("Payment ledger",)),  # mentioned by its alias only
]
HUB_VECTORS = {
    "Orders service": [0.0, 0.0, 0.0, 1.0],
    "orders service": [0.0, 0.0, 0.0, 1.0],
    "Stripe": [0.0, 0.0, 0.0, 1.0],
    "Audit logging": [0.9, 0.1, 0.0, 0.0],
    "Search": [0.6, 0.0, 0.8, 0.0],  # ties with Reporting at cosine 0.6
    "Reporting": [0.6, 0.0, 0.0, 0.8],
    "Inventory": [0.0, 0.0, -1.0, 0.0],
    "Billing": [0.0, 0.0, 0.0, 1.0],
}
TERM_FRAGMENTS = {
    "a#s0": fragment("a#s0", "The Orders service calls Stripe; the OMS writes to the payment ledger.", 0),
    "a#s1": fragment("a#s1", "The ghost of an idea.", 1),
}
TERM_UNIT = unit_of(*TERM_FRAGMENTS.values())
TERM_VECTORS = {"a#s0": S0, "a#s1": S1}
MENTIONED = [
    KnownTerm("Billing", "Module", ("Payment ledger",), mentioned=True),
    KnownTerm("Ghost", "Module", (), mentioned=True),
    KnownTerm("Orders service", "Module", ("OMS", "order backend"), mentioned=True),  # the alias it names first
    KnownTerm("Stripe", "Module", (), mentioned=True),
]


def nearby(*names: str) -> list[KnownTerm]:
    shown = {"Audit logging": ("AL", "logs", "trail", "audit log")}  # shortest first, at most four
    labels = {"Audit logging": "Capability"}
    return [KnownTerm(name, labels.get(name, "Feature"), shown.get(name, ())) for name in names]


@pytest.mark.parametrize(
    ("limit", "near"),
    [
        (0, []),  # the limit caps the nearby terms only: every mention is shown
        (1, ["Audit logging"]),
        (2, ["Audit logging", "Reporting"]),  # Reporting and Search tie at 0.6: by name
        (20, ["Audit logging", "Reporting", "Search"]),  # Inventory stays below CATALOG_MIN_COSINE
    ],
)
def test_known_terms_are_every_mention_then_the_nearest_above_the_floor(limit: int, near: list[str]) -> None:
    ranking = KnownTerms(HUBS, HUB_VECTORS)
    assert ranking.for_unit(TERM_UNIT, TERM_FRAGMENTS, TERM_VECTORS, limit) == MENTIONED + nearby(*near)


def test_known_terms_without_fragment_vectors_are_the_mentions_only() -> None:
    assert KnownTerms(HUBS, HUB_VECTORS).for_unit(TERM_UNIT, TERM_FRAGMENTS, {}, 10) == MENTIONED


def test_a_mentioned_alias_is_shown_first_and_at_most_four_aliases_are_shown() -> None:
    aliases = ("audit trail", "AL", "audit log", "logs", "zz")
    hubs = [HubName(name="Audit logging", label="Capability", aliases=aliases)]
    unit_fragments = {"a#s0": fragment("a#s0", "Every export is written to the audit trail.")}
    (term,) = KnownTerms(hubs, {}).for_unit(unit_of(*unit_fragments.values()), unit_fragments, {}, 5)
    assert term == KnownTerm("Audit logging", "Capability", ("audit trail", "AL", "zz", "logs"), mentioned=True)


def test_known_terms_follow_the_known_terms_limit_option() -> None:
    assert BuildOptions().known_terms_limit == RUNTIME_DEFAULTS["KNOWN_TERMS_LIMIT"] == 30
    ranking = KnownTerms(HUBS, HUB_VECTORS)
    found = ranking.for_unit(TERM_UNIT, TERM_FRAGMENTS, TERM_VECTORS, BuildOptions().known_terms_limit)
    assert [term.name for term in found if not term.mentioned] == ["Audit logging", "Reporting", "Search"]


def test_the_known_terms_shown_are_stored_by_name() -> None:
    shown = terms.shown_terms([*MENTIONED, *nearby("Audit logging")])
    mentioned = ("Billing", "Ghost", "Orders service", "Stripe")
    assert shown == KnownTermsShown(mentioned=mentioned, nearby=("Audit logging",))


async def test_the_term_ranker_reads_fragment_vectors_once_and_ranks_only_the_units_asked(
    scripted_gateway: Factory,
) -> None:
    gateway = scripted_gateway()
    ranker = terms.TermRanker(list(TERM_FRAGMENTS.values()), VectorCache(gateway, MemoryExtractionStore(), "p"), 5)
    whole, second = (TERM_UNIT, (0, 2)), (TERM_UNIT, (1, 2))
    assert await ranker([], [whole]) == {}
    assert await ranker(HUBS, []) == {}
    assert gateway.calls == []
    found = await ranker(HUBS, [whole, second])
    key = terms.window_key(TERM_UNIT.unit_id, (0, 2))
    assert [term.name for term in found[key] if term.mentioned] == [term.name for term in MENTIONED]
    # a window is shown what its own fragments mention (the ghost is named in the second fragment only)
    assert [term.name for term in found[terms.window_key(TERM_UNIT.unit_id, (1, 2))] if term.mentioned] == ["Ghost"]
    again = await ranker(HUBS[:3], [whole])
    assert [term.name for term in again[key] if term.mentioned] == ["Orders service", "Stripe"]
    embedded = [text for call in gateway.calls for text in call.texts]
    assert sorted(set(embedded)) == sorted(embedded)  # every text embedded once


# Hub catalog from the hub pass

M, F = "Module", "Feature"
LABELS = {"Module", "Feature", "Capability"}


def entry(name: str, label: str = M, *aliases: str, document: str = "d1") -> terms.CatalogEntry:
    return terms.CatalogEntry(name, label, tuple(aliases), document)


def merged(*entries: terms.CatalogEntry) -> terms.CatalogMerge:
    return terms.merge_catalog(entries, LABELS)


def test_the_merge_folds_whitespace_and_drops_empty_names_unknown_labels_and_self_aliases() -> None:
    result = merged(
        entry("  Orders   service ", M, "orders-services", "  ", "The Orders service"),
        entry("   "),
        entry("Not a hub", "FunctionalRequirement"),
    )
    assert result.hubs == (HubName(name="Orders service", label=M, document_ids=("d1",)),)
    assert (result.aliases, result.links_refused, result.aliases_dropped) == (0, 0, 0)


def test_the_merge_groups_by_label_and_hub_surface_and_names_by_frequency_then_documents_then_lowest() -> None:
    result = merged(
        entry("Orders services", document="d1"),
        entry("Orders service", document="d2"),
        entry("Orders service", document="d3"),
        entry("orders service", document="d1"),
        entry("Orders service", F, document="d4"),  # another label: another hub
    )
    assert result.hubs == (
        HubName(name="Orders service", label=F, document_ids=("d4",)),
        HubName(name="Orders service", label=M, document_ids=("d1", "d2", "d3")),
    )
    # a tie in frequency goes to the variant named in more documents, then to the lowest
    tie = merged(entry("Billing", document="d1"), entry("billing", document="d2"))
    assert [hub.name for hub in tie.hubs] == ["Billing"]
    # "billing" twice in one document, "Billing" twice in two: equal counts, more documents win
    more_documents = merged(
        entry("billing", document="d1"),
        entry("billing", document="d1"),
        entry("Billing", document="d2"),
        entry("Billing", document="d3"),
    )
    assert [hub.name for hub in more_documents.hubs] == ["Billing"]


def test_an_alias_naming_another_hub_of_its_label_links_them_and_the_other_names_become_aliases() -> None:
    result = merged(
        entry("Order management service", M, "OMS", "Orders service"),
        entry("Order management service"),
        entry("Orders service", M, "order backend"),
        entry("OMS"),
    )
    assert result.hubs == (
        HubName(
            name="Order management service",
            label=M,
            aliases=("OMS", "Orders service", "order backend"),
            document_ids=("d1",),
        ),
    )
    assert (result.aliases, result.links_refused, result.aliases_dropped) == (3, 0, 0)


def test_a_link_making_a_component_larger_than_the_group_max_is_refused_and_counted() -> None:
    names = [f"Service {letter}" for letter in "ABCDE"]
    result = merged(entry(names[0], M, *names[1:]), *(entry(name) for name in names[1:]))
    assert terms.CATALOG_GROUP_MAX == 4
    assert result.links_refused == 1
    (joined,) = [hub for hub in result.hubs if hub.aliases]
    assert len(joined.aliases) == 3  # four names in one hub
    assert [hub.name for hub in result.hubs if not hub.aliases] == ["Service E"]
    assert result.aliases_dropped == 1  # the refused link's alias names another hub: dropped


def test_an_alias_naming_a_hub_of_another_label_or_claimed_by_two_hubs_is_dropped() -> None:
    result = merged(
        entry("Orders service", M, "Order history", "OMS"),
        entry("Order history", F),
        entry("Billing service", M, "OMS", "ledger"),
    )
    assert result.hubs == (
        HubName(name="Billing service", label=M, aliases=("ledger",), document_ids=("d1",)),
        HubName(name="Order history", label=F, document_ids=("d1",)),
        HubName(name="Orders service", label=M, document_ids=("d1",)),
    )
    assert result.aliases_dropped == 3  # "Order history" (a Feature's name), "OMS" twice


def test_weak_aliases_are_dropped_and_counted() -> None:
    result = merged(entry("Orders service", M, "OS", "the service", "The system", "OMS", "it"))
    assert result.hubs == (HubName(name="Orders service", label=M, aliases=("OMS",), document_ids=("d1",)),)
    assert result.aliases_dropped == 4
    assert terms.weak_alias("IQ") and terms.weak_alias("the app") and not terms.weak_alias("IQS")


def test_a_linked_full_name_beats_its_abbreviation_given_up_to_twice_as_often() -> None:
    abbreviation = [entry("IQS"), entry("IQS"), entry("IQS")]
    full = entry("Image QA Service", M, "IQS")
    result = merged(*abbreviation, full)
    assert [(hub.name, hub.aliases) for hub in result.hubs] == [("Image QA Service", ("IQS",))]
    # given far more often (5 vs 1 item plus its link), the abbreviation stays the name
    result = merged(*abbreviation, entry("IQS"), entry("IQS"), full)
    assert [(hub.name, hub.aliases) for hub in result.hubs] == [("IQS", ("Image QA Service",))]


def test_a_one_word_alias_is_a_mention_only_as_the_same_word_case_exact() -> None:
    hubs = [
        HubName(name="Audit logging", label="Capability", aliases=("AL",)),
        HubName(name="Image QA Service", label="Module", aliases=("IQS", "image checker")),
    ]
    ranking = KnownTerms(hubs, {})

    def mentioned(text: str) -> list[str]:
        unit_fragments = {"a#s0": fragment("a#s0", text)}
        found = ranking.for_unit(unit_of(*unit_fragments.values()), unit_fragments, {}, 5)
        return [term.name for term in found if term.mentioned]

    assert mentioned("An al dente pasta; iqs and Al.") == []  # not the same word as written
    assert mentioned("AL records it; the IQS checks images.") == ["Audit logging", "Image QA Service"]
    assert mentioned("The Image-Checker rejects blurry photos.") == ["Image QA Service"]  # two words: folded


def test_the_shortest_linked_full_name_that_meets_the_ratio_is_the_name() -> None:
    abbreviation = [entry("IQS"), entry("IQS")]
    short = entry("Image QA Service", M, "IQS")
    long = entry("Image quality assurance service", M, "IQS")
    result = merged(*abbreviation, short, long)
    (hub,) = result.hubs
    assert hub.name == "Image QA Service"
    assert hub.aliases == ("IQS", "Image quality assurance service")


def test_mentioned_terms_are_capped_logged_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terms, "MENTIONED_TERMS_MAX", 2)
    unit_fragments = {"a#s0": fragment("a#s0", "Stripe, Stripe, Ghost and the Orders service.")}
    ranking = KnownTerms(HUBS, HUB_VECTORS)
    found = ranking.for_unit(unit_of(*unit_fragments.values()), unit_fragments, {}, 5)
    # the most mentioned first (Stripe twice), ties by name; shown in name order
    assert [term.name for term in found] == ["Ghost", "Stripe"]
    assert ranking.capped_units == 1


def test_names_differing_only_in_spaces_and_punctuation_are_one_hub_and_suffixed_names_stay_apart() -> None:
    result = merged(
        entry("Creative Force", M, document="d1"),
        entry("Creative Force", M, document="d2"),
        entry("CreativeForce", M),
        entry("Agent run", "Feature"),
        entry("AgentRun", "Capability"),  # another label: never joined
        entry("Neo4j"),
        entry("Neo4j database"),  # a name plus a word: joined only by an alias the text gives
    )
    assert [(hub.name, hub.label, hub.aliases) for hub in result.hubs] == [
        ("Agent run", "Feature", ()),
        ("AgentRun", "Capability", ()),
        ("Creative Force", M, ("CreativeForce",)),
        ("Neo4j", M, ()),
        ("Neo4j database", M, ()),
    ]
    assert terms.squashed("creative force") == terms.squashed("creativeforce") == "creativeforce"


def test_the_merge_is_deterministic_and_sorted() -> None:
    items = [
        entry("Stripe", M, "Stripe payments", document="d2"),
        entry("Audit logging", "Capability", "audit trail"),
        entry("Orders service", M, "OMS"),
        entry("stripe", M, document="d3"),
    ]
    first = terms.merge_catalog(items, LABELS)
    assert first == terms.merge_catalog(list(reversed(items)), LABELS)
    assert [hub.name for hub in first.hubs] == ["Audit logging", "Orders service", "Stripe"]


def test_the_catalog_of_the_hub_pass_reads_hub_raw_nodes_with_their_document() -> None:
    def raw(name: str, label: str, unit: str, *aliases: str) -> RawNode:
        return RawNode(
            raw_id=name,
            name=name,
            label=label,
            unit_id=unit,
            call_id="c",
            window=(0, 1),
            fragment_ids=(),
            aliases=aliases,
        )

    nodes = [
        raw("ERP", M, "u1", "enterprise resource planning system"),
        raw("ERP", M, "u2"),
        raw("Pay", "FunctionalRequirement", "u1"),
    ]
    result = terms.catalog_from_hub_pass(nodes, {"u1": "overview", "u2": "orders"}, ONT.hub_labels)
    assert result.hubs == (
        HubName(
            name="ERP", label=M, aliases=("enterprise resource planning system",), document_ids=("orders", "overview")
        ),
    )


def test_persisted_hubs_win_and_take_the_catalog_names_as_aliases() -> None:
    persisted = [
        HubName(name="Orders service", label=M, aliases=("order service",)),
        HubName(name="Billing", label=M),
        HubName(name="Ledger", label=M),
        HubName(name="Reporting", label=F),
    ]
    catalog = [
        HubName(name="Order management service", label=M, aliases=("Orders service", "OMS")),  # one match
        HubName(name="Billing ledger", label=M, aliases=("Billing", "Ledger")),  # two matches: ambiguous
        HubName(name="Reporting", label=M),  # another label: no match, added
        HubName(name="Stripe", label=M),  # no match: added
    ]
    hubs, ambiguous = terms.with_catalog(persisted, catalog)
    assert ambiguous == 1
    assert hubs == [
        HubName(name="Billing", label=M),
        HubName(name="Ledger", label=M),
        HubName(name="Orders service", label=M, aliases=("order service", "Order management service", "OMS")),
        HubName(name="Reporting", label=F),
        HubName(name="Reporting", label=M),
        HubName(name="Stripe", label=M),
    ]


# Edge catalog

EDGE_FRAGMENTS = {
    "a#s0": fragment("a#s0", "The Orders service calls the Payment gateway. Customer can export order history.", 0),
    "a#s1": fragment("a#s1", "Nothing else here.", 1),
    "b#s0": fragment("b#s0", "Elsewhere.", 0),
}
EDGE_UNIT = unit_of(EDGE_FRAGMENTS["a#s0"], EDGE_FRAGMENTS["a#s1"])
EDGE_NODES = [
    canonical("c10", "Checkout service", "Module", ["a#s0"]),  # local hub
    canonical("c11", "Customer can export order history", "FunctionalRequirement", ["a#s1", "b#s0"]),  # local
    canonical("c05", "Orders service", "Module", ["b#s0"]),  # foreign, mentioned
    canonical("c03", "Payment gateway", "Module", ["b#s0"]),  # foreign, mentioned
    canonical("c01", "Audit logging", "Capability", ["b#s0"]),  # nearest
    canonical("c04", "Reporting", "Feature", ["b#s0"]),  # tie with c02
    canonical("c02", "Search", "Feature", ["b#s0"]),
    canonical("c06", "Inventory", "Feature", ["b#s0"]),  # far
    # a foreign non-hub: named in the unit's text and nearest of all, never pulled in
    canonical("c07", "Customer can export order history", "FunctionalRequirement", ["b#s0"]),
]
EDGE_HUBS = frozenset({"c10", "c05", "c03", "c01", "c04", "c02", "c06"})
EDGE_NAME_VECTORS = {
    "c10": S0,
    "c05": [0.0, 0.0, 0.0, 1.0],
    "c03": [0.0, 0.0, 0.0, 1.0],
    "c01": [0.9, 0.1, 0.0, 0.0],
    "c04": [0.6, 0.0, 0.0, 0.8],
    "c02": [0.6, 0.0, 0.8, 0.0],
    "c06": [0.0, 0.0, -1.0, 0.0],
    "c07": S0,
}
EDGE_FRAGMENT_VECTORS = {"a#s0": S0, "a#s1": S1, "b#s0": [0.0, 0.0, 1.0, 0.0]}
LOCAL = ["c10", "c11"]


def edge_catalog(limit: int, nodes: Sequence[CanonicalNode] = EDGE_NODES) -> EdgeCatalog:
    return EdgeCatalog(
        fragments_by_id=EDGE_FRAGMENTS,
        canonical=nodes,
        hub_cids=EDGE_HUBS,
        name_vectors=EDGE_NAME_VECTORS,
        fragment_vectors=EDGE_FRAGMENT_VECTORS,
        limit=limit,
    )


@pytest.mark.parametrize(
    ("limit", "foreign"),
    [
        (0, []),
        (1, ["c03"]),  # mentioned hubs first, in cid order, cut at the limit
        (2, ["c03", "c05"]),
        (3, ["c03", "c05", "c01"]),  # then the nearest
        # c02 and c04 (cosine 0.6 to a#s0) stay below that fragment's floor, 0.8 x 0.994 (c01); a#s1's
        # best foreign hub (c01, 0.110) leaves its floor at the absolute 0.40: no hub joins by nearness
        (4, ["c03", "c05", "c01"]),
        (50, ["c03", "c05", "c01"]),
    ],
)
def test_edge_catalog_keeps_locals_then_mentioned_then_nearest_foreign_hubs(limit: int, foreign: list[str]) -> None:
    catalog = edge_catalog(limit).for_unit(EDGE_UNIT)
    assert catalog == LOCAL + foreign
    assert len(catalog) - len(LOCAL) <= limit
    assert "c07" not in catalog  # a non-hub from elsewhere: neither by name nor by vector


def test_edge_catalog_is_independent_of_input_order() -> None:
    expected = edge_catalog(4).for_unit(EDGE_UNIT)
    assert edge_catalog(4, list(reversed(EDGE_NODES))).for_unit(EDGE_UNIT) == expected
    assert edge_catalog(4, EDGE_NODES[3:] + EDGE_NODES[:3]).for_unit(EDGE_UNIT) == expected


def test_edge_catalog_without_vectors_offers_locals_and_mentioned_hubs() -> None:
    catalog = EdgeCatalog(
        fragments_by_id=EDGE_FRAGMENTS,
        canonical=EDGE_NODES,
        hub_cids=EDGE_HUBS,
        name_vectors={},
        fragment_vectors={},
        limit=5,
    )
    assert catalog.for_unit(EDGE_UNIT) == [*LOCAL, "c03", "c05"]


# Edge catalog: the similarity floor

FLOOR_FRAGMENTS = {
    "u#f0": fragment("u#f0", "Payments are booked nightly.", 0),
    "u#f1": fragment("u#f1", "Refunds follow the same path.", 1),
    "x#f0": fragment("x#f0", "Elsewhere.", 0),
}
FLOOR_UNIT = unit_of(FLOOR_FRAGMENTS["u#f0"], FLOOR_FRAGMENTS["u#f1"])
# cosine to u#f0 (S0) and u#f1 (S1); every hub is grounded in x#f0 only
FLOOR_VECTORS = {
    "h1": [1.0, 0.0, 0.0, 0.0],  # f0 1.00: the best of f0, floor 0.8
    "h2": [0.85, 0.0, 0.5268, 0.0],  # f0 0.85 >= 0.8
    "h3": [0.79, 0.0, 0.0, 0.6131],  # f0 0.79 < 0.8: below the relative floor
    "h4": [0.0, 0.45, 0.893, 0.0],  # f1 0.45: the best of f1, floor max(0.40, 0.36)
    "h5": [0.0, 0.39, 0.0, 0.9208],  # f1 0.39 < 0.40: below the absolute floor
    "h6": [0.85, 0.0, -0.5268, 0.0],  # f0 0.85, ties with h2
}
FLOOR_NAMES = {
    "h1": "Ledger",
    "h2": "Billing",
    "h3": "Payouts",
    "h4": "Refund desk",
    "h5": "Archive",
    "h6": "Invoicing",
}


def floor_catalog(
    vectors: dict[str, list[float]] | None = None,
    names: dict[str, str] | None = None,
    local: Sequence[str] = (),
    limit: int = 50,
) -> EdgeCatalog:
    vectors = vectors or FLOOR_VECTORS
    names = {**FLOOR_NAMES, **(names or {})}
    nodes = [canonical(cid, names[cid], "Module", ["u#f0"] if cid in local else ["x#f0"]) for cid in sorted(vectors)]
    return EdgeCatalog(
        fragments_by_id=FLOOR_FRAGMENTS,
        canonical=nodes,
        hub_cids=frozenset(vectors),
        name_vectors=vectors,
        fragment_vectors={"u#f0": S0, "u#f1": S1, "x#f0": [0.0, 0.0, 1.0, 0.0]},
        limit=limit,
    )


def test_a_hub_joins_by_nearness_only_above_the_absolute_and_relative_floor() -> None:
    assert (terms.CATALOG_MIN_COSINE, terms.CATALOG_RELATIVE_COSINE) == (0.40, 0.8)
    selection = floor_catalog().select(FLOOR_UNIT)
    assert (selection.local, selection.mentioned) == ([], [])
    # by best cosine, ties (h2, h6 at 0.85) by cid; h3 misses f0's relative floor, h5 f1's absolute one
    assert selection.near == ["h1", "h2", "h6", "h4"]
    assert floor_catalog(limit=2).select(FLOOR_UNIT).near == ["h1", "h2"]


def test_an_unrelated_new_hub_leaves_the_catalog_unchanged() -> None:
    before = floor_catalog().for_unit(FLOOR_UNIT)
    unrelated = {**FLOOR_VECTORS, "h9": [0.3, 0.3, 0.0, 0.9055]}  # cosine 0.30 to both fragments
    assert floor_catalog(unrelated, {"h9": "Warehouse"}).for_unit(FLOOR_UNIT) == before


def test_mentioned_hubs_raise_the_floor_and_local_hubs_do_not() -> None:
    # h1 named in f0: it joins as mentioned and still sets f0's floor (0.8), so h3 stays out
    mentioned = floor_catalog(names={"h1": "Payments"}).select(FLOOR_UNIT)
    assert (mentioned.mentioned, mentioned.near) == (["h1"], ["h2", "h6", "h4"])
    # h1 grounded in the unit: local hubs are not foreign, f0's floor drops to 0.8 x 0.85 = 0.68
    local = floor_catalog(local=["h1"]).select(FLOOR_UNIT)
    assert (local.local, local.near) == (["h1"], ["h2", "h6", "h3", "h4"])


# Edge catalog of a clean unit in an update build


def test_a_clean_unit_keeps_its_nearness_hubs_and_takes_new_hubs_only_by_mention() -> None:
    vectors = {**FLOOR_VECTORS, "h7": [1.0, 0.0, 0.0, 0.0]}  # a new hub as near as h1
    catalog = floor_catalog(vectors, {"h7": "Clearing", "h5": "Refunds"})
    assert catalog.select(FLOOR_UNIT).near == ["h1", "h7", "h2", "h6", "h4"]  # the full rule takes h7
    # kept from the last catalog: h4, h1 and h8 (gone since); h5 is now named in f1
    kept = catalog.select(FLOOR_UNIT, kept=["h4", "h1", "h8", "h5", "h4"])
    assert (kept.mentioned, kept.near) == (["h5"], ["h4", "h1"])
    assert catalog.select(FLOOR_UNIT, kept=[]).near == []  # a last catalog without nearness hubs
    # the kept hubs share the room left by the mentioned ones
    narrow = floor_catalog(vectors, {"h7": "Clearing", "h5": "Refunds"}, limit=2)
    assert narrow.select(FLOOR_UNIT, kept=["h4", "h1"]).near == ["h4"]


class MemoryExtractionStore:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}

    async def get(self, kind: str, key: str, fingerprint: str) -> dict[str, Any] | None:
        found = self.entries.get((kind, key))
        return found[1] if found is not None and found[0] == fingerprint else None

    async def put(self, kind: str, key: str, fingerprint: str, payload: dict[str, Any]) -> None:
        self.entries[(kind, key)] = (fingerprint, payload)


async def test_an_update_build_reuses_a_clean_units_edges_under_its_kept_catalog(scripted_gateway: Factory) -> None:

    gateway = scripted_gateway()
    store = MemoryExtractionStore()
    results = extract.StoredResults(store, "p-terms", read=True)
    fragments = [
        item.model_copy(update={"change": "unchanged", "extracted_hash": item.content_hash})
        for item in FLOOR_FRAGMENTS.values()
    ]
    plan = PlanArtifact(
        mode="update",
        change_set=ChangeSet(unchanged=("u", "x")),
        documents=tuple(
            DocumentRec(document_id=doc, revision_id="r1", version=1, path=doc, title=doc) for doc in ("u", "x")
        ),
        fragments=tuple(fragments),
        units=(FLOOR_UNIT.model_copy(update={"windows": ()}),),
    )
    vectors = {**FLOOR_VECTORS, "h7": [1.0, 0.0, 0.0, 0.0]}
    names = {**FLOOR_NAMES, "h7": "Clearing", "h5": "Refunds"}
    nodes = [canonical("r1", "Payments are booked nightly", "FunctionalRequirement", ["u#f0"])]
    nodes += [canonical(cid, names[cid], "Module", ["x#f0"]) for cid in sorted(vectors)]
    resolved = ResolveArtifact(canonical=tuple(nodes))
    by_cid = {node.cid: node for node in nodes}
    options = BuildOptions()
    params = EdgeWindowParams(nearness="name")
    edge_print = extract.edge_fingerprint(ONT, gateway, options, params)
    # the unit's one window: its last answer took h4 and h1 by nearness; its edges are stored for its
    # window catalog now (the node grounded in it and h5, named in its text; nearness hubs are not in it)
    digest = edges.window_hash(FLOOR_UNIT, (0, 2), FLOOR_FRAGMENTS)
    last = {"edges": [], "near": [by_cid["h4"].identity_key, by_cid["h1"].identity_key]}
    await store.put("edges", results.last_edge_key(digest), edge_print, last)
    catalog_keys = [by_cid[cid].identity_key for cid in ("r1", "h5")]
    stored_edges = {
        "edges": [
            {
                "relation": "PART_OF",
                "source": by_cid["r1"].identity_key,
                "target": by_cid["h1"].identity_key,
                "fact": "Payments are booked nightly.",
                "positions": [0],
                "fragments": ["u#f0"],
            }
        ],
        "near": last["near"],
    }
    await store.put("edges", results.edge_key(digest, catalog_keys), edge_print, stored_edges)
    attribute_print = extract.attribute_fingerprint(ONT, gateway)
    hashes = {item.fragment_id: item.content_hash for item in fragments}
    for node in nodes:
        key = results.attribute_key(node.identity_key, node.label, [hashes[fid] for fid in node.fragment_ids])
        await store.put("attributes", key, attribute_print, {"values": {}})

    artifact = await edges.extract_edges(
        plan,
        resolved,
        services=StepServices(gateway=gateway, ontology=ONT, options=options),
        vectors=edges.CatalogVectors(fragments={"u#f0": S0, "u#f1": S1, "x#f0": [0.0, 0.0, 1.0, 0.0]}, hubs=vectors),
        params=params,
        results=results,
    )
    # local, then h5 newly named in the text, then the kept nearness hubs; the new near hub h7 stays out
    assert artifact.catalogs == {FLOOR_UNIT.unit_id: ("r1", "h5", "h4", "h1")}
    assert (artifact.counters.catalogs_kept, artifact.counters.units_reused, artifact.called_units) == (1, 1, ())
    assert (artifact.counters.windows_reused, artifact.counters.windows_called, artifact.called_windows) == (1, 0, ())
    assert artifact.catalog_parts == {FLOOR_UNIT.unit_id: CatalogParts(mentioned=("h5",), near=("h4", "h1"))}
    assert [(edge.source_cid, edge.relation, edge.target_cid, edge.reused) for edge in artifact.raw_edges] == [
        ("r1", "PART_OF", "h1", True)
    ]
    assert [call for call in gateway.calls if call.call_class != "embedding"] == []


# Known terms from persisted hubs


async def test_known_terms_of_an_update_build_come_from_the_persisted_hub_nodes() -> None:

    rows = [
        {"name": "Orders  service", "label": "Module"},
        {"name": "Orders service", "label": "Module"},  # equal after whitespace folding: once
        {"name": "Orders service", "label": "Feature"},
        {"name": "Audit logging", "label": "Capability"},
        {"name": "", "label": "Module"},
        {"name": "Not a hub", "label": "FunctionalRequirement"},
    ]
    executor = OfflineExecutor({READ_PERSISTED_HUBS.name: rows})
    found = await terms.persisted_hubs(executor, "p-terms", ONT)
    assert found == [
        HubName(name="Audit logging", label="Capability"),
        HubName(name="Orders service", label="Feature"),
        HubName(name="Orders service", label="Module"),
    ]
    assert executor.reads == [(READ_PERSISTED_HUBS.name, "p-terms")]
    # one term per name and label: both "Orders service" hubs are mentioned, "Audit logging" is near
    ranking = KnownTerms(found, {"Audit logging": S1, "Orders service": S0})
    assert ranking.for_unit(TERM_UNIT, TERM_FRAGMENTS, TERM_VECTORS, 10) == [
        KnownTerm("Orders service", "Feature", (), mentioned=True),
        KnownTerm("Orders service", "Module", (), mentioned=True),
        KnownTerm("Audit logging", "Capability", (), mentioned=False),
    ]
    assert await terms.persisted_hubs(OfflineExecutor(), "p-terms", ONT) == []


# Edge catalog parts and window catalogs


def headed(fragment_id: str, text: str, heading: tuple[str, ...], ordinal: int) -> FragmentRec:
    return fragment(fragment_id, text, ordinal).model_copy(update={"heading_path": heading})


PART_FRAGMENTS = {
    "p#0": headed("p#0", "Cards are charged once.", ("Shop", "Payments API"), 0),
    "p#1": headed("p#1", "The Ledger records each charge.", ("Shop", "Payments API", "Errors"), 1),
    "p#2": headed("p#2", "Returns are manual.", ("Shop", "Refunds"), 2),
    "x#0": headed("x#0", "Elsewhere.", ("Other",), 0),
}
PART_UNIT = unit_of(PART_FRAGMENTS["p#0"], PART_FRAGMENTS["p#1"], PART_FRAGMENTS["p#2"])
PART_NODES = [
    canonical("h1", "Payments API", "Interface", ["x#0"]),  # named only in a heading path
    canonical("h2", "Ledger", "Module", ["x#0"]),  # named in the text of p#1
    canonical("h3", "Refunds", "Feature", ["x#0"]),  # named in p#2's heading
    canonical("h4", "Archive", "Module", ["x#0"]),  # near p#0 only
    canonical("l1", "Card charging", "Feature", ["p#1", "p#0"]),  # local hub
    canonical("l2", "Cards are charged once", "FunctionalRequirement", ["p#0"]),  # local requirement
    canonical("r1", "Refund within 30 days", "FunctionalRequirement", ["x#0"]),  # referenced
]
PART_VECTORS = {"h1": S1, "h2": S1, "h3": S1, "h4": S0}


WINDOW_PARAMS = EdgeWindowParams(nearness="name")


def part_catalog(params: EdgeWindowParams | None = WINDOW_PARAMS, limit: int = 10) -> EdgeCatalog:
    return EdgeCatalog(
        fragments_by_id=PART_FRAGMENTS,
        canonical=PART_NODES,
        hub_cids=frozenset({"h1", "h2", "h3", "h4", "l1"}),
        name_vectors=PART_VECTORS,
        fragment_vectors={"p#0": S0},
        limit=limit,
        params=params,
    )


def test_hubs_named_only_in_a_heading_path_form_the_heading_part() -> None:
    selection = part_catalog().select(PART_UNIT, referenced=["r1", "l2", "h2", "gone", "r1"])
    assert (selection.local, selection.mentioned, selection.heading) == (["l1", "l2"], ["h2"], ["h1", "h3"])
    assert selection.near == ["h4"]
    # referenced entries on top, each once, never one already in the catalog or unknown
    assert selection.referenced == ["r1"]
    assert selection.cids == ["l1", "l2", "h2", "h1", "h3", "h4", "r1"]
    assert selection.parts() == CatalogParts(mentioned=("h2",), heading=("h1", "h3"), near=("h4",), referenced=("r1",))
    # mentioned and heading share the limit; referenced does not count
    narrow = part_catalog(limit=2).select(PART_UNIT, referenced=["r1"])
    assert (narrow.mentioned, narrow.heading, narrow.near, narrow.referenced) == (["h2"], ["h1"], [], ["r1"])


def test_a_hub_named_only_in_headings_of_real_shaped_fragments_is_in_the_heading_part() -> None:
    """A fragment's text starts with its own heading line, which repeats its heading path's last element: a hub named
    there (or only in an ancestor element) is named in a heading, not in the body, so it forms the heading part; one
    also named in a body line stays mentioned (the heading part was always empty because every heading line counted as
    text).
    """

    shaped = {
        "b#0": headed("b#0", "## 4. Payments API\n\nCards are charged once.", ("Billing spec", "4. Payments API"), 0),
        "b#1": headed(
            "b#1",
            "### 4.2 Errors\n\nA declined card is retried by the Ledger.\n\n```\n# Archive\n```",
            ("Billing spec", "4. Payments API"),
            1,
        ),
        "b#2": headed("b#2", "Returns follow the policy.", ("Billing spec", "5. Refund handling"), 2),
        "x#0": headed("x#0", "Elsewhere.", ("Other",), 0),
    }
    nodes = [
        canonical("h1", "Payments API", "Interface", ["x#0"]),  # heading line of b#0 and b#1's heading path
        canonical("h2", "Ledger", "Module", ["x#0"]),  # a body line of b#1
        canonical("h3", "Refunds", "Feature", ["x#0"]),  # b#2's heading path ("Refund handling") only
        canonical("h4", "Errors", "Module", ["x#0"]),  # the merged heading line "### 4.2 Errors" only
        canonical("h5", "Archive", "Module", ["x#0"]),  # a "#" line inside fenced code: body, not a heading
        canonical("l1", "Card charging", "Feature", ["b#0"]),
    ]
    catalog = EdgeCatalog(
        fragments_by_id=shaped,
        canonical=nodes,
        hub_cids=frozenset({"h1", "h2", "h3", "h4", "h5", "l1"}),
        name_vectors={},
        fragment_vectors={},
        limit=10,
        params=EdgeWindowParams(),
    )
    selection = catalog.select(unit_of(shaped["b#0"], shaped["b#1"], shaped["b#2"]))
    assert (selection.local, selection.mentioned, selection.heading) == (["l1"], ["h2", "h5"], ["h1", "h3", "h4"])
    # the catalog before edge windows reads heading lines as text: the same hubs but h3 (path only)
    before = EdgeCatalog(
        fragments_by_id=shaped,
        canonical=nodes,
        hub_cids=frozenset({"h1", "h2", "h3", "h4", "h5", "l1"}),
        name_vectors={},
        fragment_vectors={},
        limit=10,
    )
    legacy = before.select(unit_of(shaped["b#0"], shaped["b#1"], shaped["b#2"]))
    assert (legacy.mentioned, legacy.heading) == (["h1", "h2", "h4", "h5"], [])
    body = "\nA declined card is retried by the Ledger.\n\n```\n# Archive\n```"
    assert body_lines(shaped["b#1"].text) == (body, ("4.2 Errors",))


def test_without_edge_window_params_the_catalog_is_the_one_before_edge_windows() -> None:
    selection = part_catalog(params=None).select(PART_UNIT)
    assert (selection.mentioned, selection.heading, selection.referenced) == (["h2"], [], [])
    assert part_catalog(params=None).for_unit(PART_UNIT) == [*selection.local, *selection.mentioned, *selection.near]


def test_nearness_off_leaves_the_near_part_empty() -> None:
    catalog = part_catalog(EdgeWindowParams(nearness="off"))
    assert catalog.select(PART_UNIT).near == []
    assert catalog.select(PART_UNIT, kept=["h4"]).near == []
    assert part_catalog(EdgeWindowParams(nearness="rich")).select(PART_UNIT).near == ["h4"]


def test_a_window_catalog_holds_what_the_window_grounds_names_and_references() -> None:
    catalog = part_catalog()
    selection = catalog.select(PART_UNIT, referenced=["r1"])
    referenced = {"p#2": ("r1",)}
    key = {node.cid: node.identity_key for node in PART_NODES}
    # p#0: grounds l1, l2; its heading names h1 (Payments API)
    assert catalog.window_catalog(PART_UNIT, (0, 1), selection, referenced) == tuple(
        sorted({key["l1"], key["l2"], key["h1"]})
    )
    # p#1: grounds l1; text names h2, heading names h1
    assert catalog.window_catalog(PART_UNIT, (1, 2), selection, referenced) == tuple(
        sorted({key["l1"], key["h2"], key["h1"]})
    )
    # p#2: heading names h3; references r1
    assert catalog.window_catalog(PART_UNIT, (2, 3), selection, referenced) == tuple(sorted({key["h3"], key["r1"]}))
    # a hub or reference outside the unit's catalog does not count
    assert catalog.window_catalog(PART_UNIT, (2, 3), part_catalog().select(PART_UNIT)) == (key["h3"],)


def test_section_hints_name_the_heading_path_of_local_owner_targets() -> None:
    catalog = part_catalog()
    selection = catalog.select(PART_UNIT)
    assert catalog.section_hints(PART_UNIT, selection, {"Feature", "Module"}) == {"l1": "Shop > Payments API"}
    assert catalog.section_hints(PART_UNIT, selection, {"Interface"}) == {}


@pytest.mark.parametrize(
    ("path", "lines", "section"),
    [
        (["Spec", "6. Access"], [], "Spec > 6. Access"),
        (["Spec", "6. Access"], ["### 6.4 Members", "text", "### 6.5 Rules"], "Spec > 6. Access > 6.5 Rules"),
        (["Spec", "6. Access"], ["### 6.5 Rules", "#### Table 6.3", "### 6.6 Roles"], "Spec > 6. Access > 6.6 Roles"),
        (["Spec", "6. Access"], ["### 6.5 Rules", "#### Table 6.3"], "Spec > 6. Access > 6.5 Rules > Table 6.3"),
        (["Spec", "6. Access"], ["## 6. Access", "```", "# shell comment", "```"], "Spec > 6. Access"),
        ([" ", "Notes"], ["#", "## Call notes ##"], "Notes > Call notes"),
        ([], [], ""),
    ],
)
def test_heading_section_is_the_path_and_the_headings_still_open_at_the_line(
    path: list[str], lines: list[str], section: str
) -> None:
    assert heading_section(path, lines) == section
