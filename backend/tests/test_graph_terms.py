"""Known terms, the hub catalog, name occurrences and the edge catalog offline (build.md steps 2 and 4;
tech-specs 5.4; FR-BLD-21, FR-BLD-23).

Vectors are hand-picked 4-dimensional rows, so every cosine below is exact; the hub catalog runs on
a ``ScriptedGateway`` keyed by the titles of each batch.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest

from graph.build import extract, terms
from graph.build.corpus import estimate_tokens
from graph.build.models import CanonicalNode, ChangeSet, DocumentRec, FragmentRec, PlanArtifact, ResolveArtifact, Unit
from graph.build.options import BuildOptions, StepServices
from graph.build.ports import CorpusRevision
from graph.build.prompts import HUB_CATALOG_PROMPT
from graph.build.queries import READ_PERSISTED_HUBS
from graph.build.resolve import hub_words
from graph.build.terms import EdgeCatalog, HubName, KnownTerms, NameIndex, known_terms
from graph.core.errors import LLMError, LLMFatalError
from graph_support import (
    OfflineExecutor,
    ScriptedGateway,
    ShopAnswers,
    shipped_ontology,
)
from runtime_defaults_support import RUNTIME_DEFAULTS

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, neo4j_project) live in graph_support.
pytest_plugins = ["graph_support"]

ONT = shipped_ontology()
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
    # "Orders database" wins over "Orders" where both start; occurrences never overlap; plurals,
    # hyphens and underscores fold; "Reorders" and "ordersX" are other tokens
    assert index.count(hub_words(text)) == {"db": 1, "orders": 1, "service": 2}
    assert index.count(hub_words("database orders")) == {"orders": 1}  # words in order, contiguous
    assert index.count([]) == {}
    assert not NameIndex([("  ", "blank")])


def test_name_index_counts_every_key_of_names_with_equal_words() -> None:
    index = NameIndex([("Orders service", "a"), ("the orders-services", "b"), ("Orders service", "a")])
    assert index.count(hub_words("The Orders service.")) == {"a": 1, "b": 1}


# Known terms (build.md step 2)

HUBS = [
    HubName(name="orders service", label="Module"),  # same surface as "Orders service": one term
    HubName(name="Orders service", label="Module"),
    HubName(name="Stripe", label="Module"),
    HubName(name="Ghost", label="Module"),  # no vector: found by mention only
    HubName(name="Audit logging", label="Capability"),
    HubName(name="Search", label="Feature"),
    HubName(name="Reporting", label="Feature"),
    HubName(name="Inventory", label="Feature"),
]
HUB_VECTORS = {
    "Orders service": [0.0, 0.0, 0.0, 1.0],
    "orders service": [0.0, 0.0, 0.0, 1.0],
    "Stripe": [0.0, 0.0, 0.0, 1.0],
    "Audit logging": [0.9, 0.1, 0.0, 0.0],
    "Search": [0.6, 0.0, 0.8, 0.0],  # ties with Reporting at cosine 0.6
    "Reporting": [0.6, 0.0, 0.0, 0.8],
    "Inventory": [0.0, 0.0, -1.0, 0.0],
}
TERM_FRAGMENTS = {
    "a#s0": fragment("a#s0", "The Orders service calls Stripe.", 0),
    "a#s1": fragment("a#s1", "The ghost of an idea.", 1),
}
TERM_UNIT = unit_of(*TERM_FRAGMENTS.values())
TERM_VECTORS = {"a#s0": S0, "a#s1": S1}


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        (0, []),
        (2, ["Ghost", "Orders service"]),  # more mentions than room: the first by name
        (3, ["Ghost", "Orders service", "Stripe"]),
        (5, ["Ghost", "Orders service", "Stripe", "Audit logging", "Reporting"]),  # tie by name
        (20, ["Ghost", "Orders service", "Stripe", "Audit logging", "Reporting", "Search", "Inventory"]),
    ],
)
def test_known_terms_rank_mentions_by_name_then_the_nearest(limit: int, expected: list[str]) -> None:
    ranking = KnownTerms(HUBS, HUB_VECTORS)
    assert ranking.for_unit(TERM_UNIT, TERM_FRAGMENTS, TERM_VECTORS, limit) == expected


def test_known_terms_without_fragment_vectors_are_the_mentions_only() -> None:
    assert KnownTerms(HUBS, HUB_VECTORS).for_unit(TERM_UNIT, TERM_FRAGMENTS, {}, 10) == [
        "Ghost",
        "Orders service",
        "Stripe",
    ]


def test_known_terms_follow_the_known_terms_limit_option() -> None:
    assert BuildOptions().known_terms_limit == RUNTIME_DEFAULTS["KNOWN_TERMS_LIMIT"]
    found = known_terms(
        TERM_UNIT, TERM_FRAGMENTS, HUBS, HUB_VECTORS, TERM_VECTORS, limit=BuildOptions().known_terms_limit
    )
    assert len(found) == 7  # every distinct hub fits the default limit
    limit = BuildOptions(known_terms_limit=4).known_terms_limit
    assert known_terms(TERM_UNIT, TERM_FRAGMENTS, HUBS, HUB_VECTORS, TERM_VECTORS, limit=limit) == [
        "Ghost",
        "Orders service",
        "Stripe",
        "Audit logging",
    ]
    # another hub list object is never answered from the cached ranking
    assert known_terms(TERM_UNIT, TERM_FRAGMENTS, HUBS[:3], HUB_VECTORS, TERM_VECTORS, limit=limit) == [
        "Orders service",
        "Stripe",
    ]


# Hub catalog (FR-BLD-23)


def revisions_and_fragments(count: int) -> tuple[list[CorpusRevision], list[FragmentRec]]:
    revisions, fragments = [], []
    for index in range(count):
        lineage = f"doc{index}"
        text = f"Document {index} describes the Module {index} service in some detail."
        revisions.append(
            CorpusRevision(
                lineage_id=lineage,
                revision_id=f"r{index}",
                version=1,
                path=f"{lineage}.md",
                title=f"Title {index}",
                markdown=text,
            )
        )
        fragments.append(fragment(f"{lineage}#s0", text))
    return revisions, fragments


def catalog_gateway(factory: Factory, answers: dict[tuple[str, ...], Any]) -> ScriptedGateway:
    gateway = factory()
    gateway.key_by(HUB_CATALOG_PROMPT, ShopAnswers.catalog_key)
    for key, answer in answers.items():
        gateway.answer(HUB_CATALOG_PROMPT, key, answer)
    return gateway


def hubs(*names: str) -> dict[str, Any]:
    return {"hubs": [{"name": name, "label": "Module"} for name in names]}


async def test_hub_catalog_batches_outlines_by_the_token_budget(
    scripted_gateway: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    revisions, fragments = revisions_and_fragments(3)
    sizes = [estimate_tokens(outline.text()) for outline in terms.outlines(revisions, fragments)]
    monkeypatch.setattr(terms, "HUB_BATCH_TOKENS", sizes[0] + sizes[1])
    gateway = catalog_gateway(
        scripted_gateway,
        {("Title 0", "Title 1"): hubs("Module 1 service", "module 0 SERVICE"), ("Title 2",): hubs("Module 0 service")},
    )
    found = await terms.hub_catalog(revisions, fragments, gateway, ONT, BuildOptions())
    assert gateway.keys_for(HUB_CATALOG_PROMPT) == [("Title 0", "Title 1"), ("Title 2",)]
    # one name per normalised surface and label across batches, sorted by name
    assert [hub.name for hub in found] == ["Module 1 service", "module 0 SERVICE"]

    # an outline larger than the budget still gets a batch of its own
    monkeypatch.setattr(terms, "HUB_BATCH_TOKENS", 1)
    gateway = catalog_gateway(scripted_gateway, {(f"Title {index}",): hubs(f"M{index}") for index in range(3)})
    assert [hub.name for hub in await terms.hub_catalog(revisions, fragments, gateway, ONT, BuildOptions())] == [
        "M0",
        "M1",
        "M2",
    ]
    assert len(gateway.calls_for(HUB_CATALOG_PROMPT)) == 3


async def test_a_failed_hub_catalog_batch_keeps_the_successful_ones(
    scripted_gateway: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    revisions, fragments = revisions_and_fragments(3)
    monkeypatch.setattr(terms, "HUB_BATCH_TOKENS", 1)
    gateway = catalog_gateway(
        scripted_gateway,
        {("Title 0",): hubs("M0"), ("Title 1",): LLMError("malformed"), ("Title 2",): hubs("M2")},
    )
    assert [hub.name for hub in await terms.hub_catalog(revisions, fragments, gateway, ONT, BuildOptions())] == [
        "M0",
        "M2",
    ]

    gateway = catalog_gateway(scripted_gateway, {(f"Title {index}",): LLMError("refused") for index in range(3)})
    assert await terms.hub_catalog(revisions, fragments, gateway, ONT, BuildOptions()) == []

    gateway = catalog_gateway(
        scripted_gateway, {("Title 0",): hubs("M0"), ("Title 1",): LLMFatalError(), ("Title 2",): hubs("M2")}
    )
    with pytest.raises(LLMFatalError):
        await terms.hub_catalog(revisions, fragments, gateway, ONT, BuildOptions())


async def test_hub_catalog_off_makes_no_call(scripted_gateway: Factory) -> None:
    revisions, fragments = revisions_and_fragments(2)
    gateway = scripted_gateway()
    assert await terms.hub_catalog(revisions, fragments, gateway, ONT, BuildOptions(hub_catalog=False)) == []
    assert await terms.hub_catalog([], fragments, gateway, ONT, BuildOptions()) == []
    assert gateway.calls == []


# Edge catalog (build.md step 4, FR-BLD-21)

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


# Edge catalog: the similarity floor (tech-specs 5.4 "Edge catalog"; FR-BLD-22)

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


# Edge catalog of a clean unit in an update build (build.md step 4; E20)


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
    edge_print = extract.edge_fingerprint(ONT, gateway, options)
    # the unit's last answer took h4 and h1 by nearness; its edges are stored for that catalog now
    last = {"edges": [], "near": [by_cid["h4"].identity_key, by_cid["h1"].identity_key]}
    await store.put("edges", results.last_edge_key(FLOOR_UNIT.content_hash), edge_print, last)
    catalog_keys = [by_cid[cid].identity_key for cid in ("r1", "h5", "h4", "h1")]
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
    await store.put("edges", results.edge_key(FLOOR_UNIT.content_hash, catalog_keys), edge_print, stored_edges)
    attribute_print = extract.attribute_fingerprint(ONT, gateway)
    hashes = {item.fragment_id: item.content_hash for item in fragments}
    for node in nodes:
        key = results.attribute_key(node.identity_key, node.label, [hashes[fid] for fid in node.fragment_ids])
        await store.put("attributes", key, attribute_print, {"values": {}})

    artifact = await extract.extract_edges(
        plan,
        resolved,
        services=StepServices(gateway=gateway, ontology=ONT, options=options),
        fragment_vectors={"u#f0": S0, "u#f1": S1, "x#f0": [0.0, 0.0, 1.0, 0.0]},
        name_vectors=vectors,
        results=results,
    )
    # local, then h5 newly named in the text, then the kept nearness hubs; the new near hub h7 stays out
    assert artifact.catalogs == {FLOOR_UNIT.unit_id: ("r1", "h5", "h4", "h1")}
    assert (artifact.counters.catalogs_kept, artifact.counters.units_reused, artifact.called_units) == (1, 1, ())
    assert [(edge.source_cid, edge.relation, edge.target_cid, edge.reused) for edge in artifact.raw_edges] == [
        ("r1", "PART_OF", "h1", True)
    ]
    assert [call for call in gateway.calls if call.call_class != "embedding"] == []


# Known terms from persisted hubs (update builds, FR-BLD-23)


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
    # names equal after hub normalisation count once in the ranking
    ranking = KnownTerms(found, {"Audit logging": S1, "Orders service": S0})
    assert ranking.for_unit(TERM_UNIT, TERM_FRAGMENTS, TERM_VECTORS, 10) == ["Orders service", "Audit logging"]
    assert await terms.persisted_hubs(OfflineExecutor(), "p-terms", ONT) == []
