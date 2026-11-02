"""Windowed edge extraction offline."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest

from graph.build.changes import DEFAULT_EDGE_WINDOWS, EdgeWindowParams, edge_windows
from graph.build.edges import CatalogVectors, catalog_vectors, extract_edges, referenced_entities, rich_text
from graph.build.extract import StoredResults, edge_fingerprint
from graph.build.models import (
    CanonicalNode,
    CatalogParts,
    ChangeSet,
    DocumentRec,
    EdgesArtifact,
    FragmentRec,
    PlanArtifact,
    ResolveArtifact,
    SourceQuote,
    Unit,
)
from graph.build.options import BuildOptions, StepServices
from graph.build.prompts import (
    ATTRIBUTES_PROMPT,
    EDGE_WINDOW_PROMPT,
    EDGE_WINDOW_WARM_UP_PROMPT,
    ReferencedEntity,
    window_fragments,
)
from graph.build.references import UnitReferences
from graph.build.vectors import VectorCache
from graph.core.errors import LLMError
from graph_extraction_store_support import MemoryStore
from graph_support import ScriptedGateway, ScriptedRequest, outside_untrusted, shipped_ontology, untrusted_blocks

pytest_plugins = ["graph_support"]

Factory = Callable[..., ScriptedGateway]
ONT = shipped_ontology()
WINDOWED = EdgeWindowParams(target_tokens=100)
"""Fragments of 50 tokens: a window holds one section of two fragments."""

# Document "a": three sections of two fragments each; its first fragment refers to section 4.2 of "b".
A_TEXTS = (
    "Checkout service takes the order. See section 4.2 for refunds.",
    "The cart holds at most 50 items.",
    "Payments go through the payment step.",
    "Stripe charges the card.",
    "The Ledger records every payment.",
    "Nightly jobs settle the books.",
)
A_HEADINGS = ("1 Intro", "1 Intro", "2 Pay", "2 Pay", "3 Ledger", "3 Ledger")
A_WINDOWS = ((0, 2), (2, 4), (4, 6))


def fragment(document: str, index: int, text: str, heading: str, content_hash: str | None = None) -> FragmentRec:
    return FragmentRec(
        fragment_id=f"{document}#f{index}",
        document_id=document,
        heading_path=(document.upper(), heading),
        ordinal=index,
        line_start=index + 1,
        line_end=index + 1,
        text=text,
        content_hash=content_hash or f"{document}-{index}",
        tokens=50,
    )


def document(document_id: str, texts: Sequence[str], headings: Sequence[str]) -> tuple[DocumentRec, list[FragmentRec]]:
    fragments = [fragment(document_id, index, text, headings[index]) for index, text in enumerate(texts)]
    return (
        DocumentRec(document_id=document_id, revision_id="r1", version=1, path=f"{document_id}.md", title=document_id),
        fragments,
    )


def unit(document_id: str, fragments: Sequence[FragmentRec]) -> Unit:
    return Unit(
        unit_id=f"{document_id}@u0",
        document_id=document_id,
        fragment_ids=tuple(item.fragment_id for item in fragments),
        tokens=sum(item.tokens for item in fragments),
        content_hash=f"unit-{document_id}",
    )


def first_plan(*documents: tuple[str, Sequence[str], Sequence[str]]) -> PlanArtifact:
    records, fragments, units = [], [], []
    for document_id, texts, headings in documents:
        record, items = document(document_id, texts, headings)
        records.append(record)
        fragments.extend(items)
        units.append(unit(document_id, items))
    return PlanArtifact(
        mode="first",
        change_set=ChangeSet(new=tuple(record.document_id for record in records)),
        documents=tuple(records),
        fragments=tuple(fragments),
        dirty_ids=tuple(item.fragment_id for item in fragments),
        units=tuple(units),
    )


def update_plan(plan: PlanArtifact, edited: Sequence[str] = ()) -> PlanArtifact:
    """``plan`` as an ``update`` plan: the fragments ``edited`` have new text (dirty), the rest are unchanged."""

    fragments = [
        item.model_copy(update={"text": item.text + " Edited.", "content_hash": f"edited-{item.content_hash}"})
        if item.fragment_id in edited
        else item.model_copy(update={"change": "unchanged", "extracted_hash": item.content_hash})
        for item in plan.fragments
    ]
    return plan.model_copy(update={"mode": "update", "fragments": tuple(fragments), "dirty_ids": tuple(edited)})


PLAN = first_plan(("a", A_TEXTS, A_HEADINGS), ("b", ["Refunds are paid within 5 days."], ["4.2 Refunds"]))


def node(cid: str, name: str, label: str, *fragments: str) -> CanonicalNode:
    return CanonicalNode(
        cid=cid,
        identity_key=f"{label}|surface|{name.casefold()}",
        name=name,
        label=label,
        member_ids=(cid,),
        fragment_ids=fragments,
    )


NODES = (
    node("m1", "Checkout service", "Module", "a#f0"),
    node("r1", "Cart holds at most 50 items", "FunctionalRequirement", "a#f1"),
    node("m2", "Stripe", "Module", "a#f3"),
    node("rb", "Refunds are paid within 5 days", "FunctionalRequirement", "b#f0"),
    node("hl", "Ledger", "Module", "b#f0"),
)
RESOLVED = ResolveArtifact(canonical=NODES)


def window_of(request: ScriptedRequest) -> tuple[int, ...]:
    """The 0-based fragment indices the request's window line names."""

    return tuple(number - 1 for number in window_fragments(outside_untrusted(request.user)))


def entities(request: ScriptedRequest) -> list[str]:
    """The names of the request's catalog."""

    (body,) = [body for label, body in untrusted_blocks(request.user) if label == "entities"]
    return [line.split('"name": "', 1)[1].split('"', 1)[0] for line in body.split("\n")]


class Windows:
    """Answers each edge-window call with one fact stated in its first fragment, recording the window,
    the warm-ups sent before it and the catalog shown; ``fail`` windows (by first index) fail."""

    def __init__(self, gateway: ScriptedGateway, fail: Sequence[int] = ()) -> None:
        self.gateway = gateway
        self.fail = set(fail)
        self.seen: list[tuple[tuple[int, ...], int]] = []
        self.catalogs: list[list[str]] = []
        gateway.handle(EDGE_WINDOW_PROMPT, self)
        gateway.answer(ATTRIBUTES_PROMPT, None, LLMError("malformed"))  # attributes are not under test here

    def __call__(self, request: ScriptedRequest) -> Any:
        window = window_of(request)
        self.seen.append((window, len(self.gateway.warm_ups)))
        self.catalogs.append(entities(request))
        if window[0] in self.fail:
            return LLMError("malformed")
        names = set(entities(request))
        source, target = (
            ("Checkout service", "Stripe") if "Stripe" in names else ("Refunds are paid within 5 days", "Ledger")
        )
        relation = "INTEGRATES_WITH" if target == "Stripe" else "PART_OF"
        return {
            "edges": [
                {
                    "source": source,
                    "relation": relation,
                    "target": target,
                    "fact": f"fact {window[0]}",
                    "fragments": [window[0] + 1],
                }
            ]
        }


async def run(
    gateway: ScriptedGateway,
    plan: PlanArtifact,
    *,
    store: MemoryStore | None = None,
    resolved: ResolveArtifact = RESOLVED,
    params: EdgeWindowParams = WINDOWED,
) -> EdgesArtifact:
    results = StoredResults(store, "p-edges", read=plan.mode == "update") if store is not None else None
    return await extract_edges(
        plan,
        resolved,
        services=StepServices(gateway=gateway, ontology=ONT, options=BuildOptions()),
        vectors=CatalogVectors(),
        results=results,
        params=params,
    )


def called(artifact: EdgesArtifact) -> list[tuple[str, tuple[int, int], str | None]]:
    return [(call.unit_id, call.window, call.cause) for call in artifact.called_windows or ()]


def edge_view(artifact: EdgesArtifact) -> list[tuple[str, str, str, str | None, tuple[str, ...]]]:
    return [
        (edge.source_cid, edge.relation, edge.target_cid, edge.fact, edge.fragment_ids) for edge in artifact.raw_edges
    ]


def test_the_sections_make_the_windows() -> None:
    (a_unit, b_unit) = PLAN.units
    fragments = {item.fragment_id: item for item in PLAN.fragments}
    assert edge_windows(a_unit, fragments, WINDOWED) == A_WINDOWS
    assert edge_windows(b_unit, fragments, WINDOWED) == ((0, 1),)
    assert DEFAULT_EDGE_WINDOWS.target_tokens == 3000


# A first build


async def test_a_first_build_calls_every_window_after_one_warm_up_per_unit(scripted_gateway: Factory) -> None:
    gateway = scripted_gateway()
    answers = Windows(gateway)
    artifact = await run(gateway, PLAN, store=MemoryStore())
    assert called(artifact) == [*(("a@u0", window, "first") for window in A_WINDOWS), ("b@u0", (0, 1), "first")]
    counters = artifact.counters
    assert (counters.windows_called, counters.windows_reused, counters.windows_fallback, counters.warm_ups) == (
        4,
        0,
        0,
        1,
    )
    assert (counters.calls, counters.units_extracted, artifact.called_units) == (4, 2, ("a@u0", "b@u0"))
    # the one warm-up (unit a: three windows; b has one) went before any window call, and is the
    # exact prefix of each of a's window calls
    (warm,) = gateway.warm_ups
    assert warm.prompt == EDGE_WINDOW_WARM_UP_PROMPT
    assert all(sent == 1 for _, sent in answers.seen)
    a_calls = [call for call in gateway.calls_for(EDGE_WINDOW_PROMPT) if call.messages[:3] == warm.messages]
    assert len(a_calls) == 3 and all(len(call.messages) == 4 for call in a_calls)
    # each window's fact is tied to its own fragment
    assert edge_view(artifact) == [
        ("m1", "INTEGRATES_WITH", "m2", "fact 0", ("a#f0",)),
        ("m1", "INTEGRATES_WITH", "m2", "fact 2", ("a#f2",)),
        ("m1", "INTEGRATES_WITH", "m2", "fact 4", ("a#f4",)),
        ("rb", "PART_OF", "hl", "fact 0", ("b#f0",)),
    ]


async def test_every_warm_up_goes_before_the_first_window_call(scripted_gateway: Factory) -> None:
    plan = first_plan(("a", A_TEXTS, A_HEADINGS), ("c", A_TEXTS, A_HEADINGS))
    resolved = ResolveArtifact(
        canonical=(*NODES[:3], node("c1", "Checkout service", "Module", "c#f0"), node("c2", "Stripe", "Module", "c#f3"))
    )
    gateway = scripted_gateway()
    answers = Windows(gateway)
    artifact = await run(gateway, plan, resolved=resolved)
    assert artifact.counters.warm_ups == len(gateway.warm_ups) == 2
    assert len(answers.seen) == 6 and all(sent == 2 for _, sent in answers.seen)


async def test_the_unit_mode_makes_one_call_per_unit_and_no_warm_up(scripted_gateway: Factory) -> None:
    gateway = scripted_gateway()
    answers = Windows(gateway)
    artifact = await run(gateway, PLAN, params=EdgeWindowParams(target_tokens=None))
    assert called(artifact) == [("a@u0", (0, 6), "first"), ("b@u0", (0, 1), "first")]
    assert [window for window, _ in answers.seen] == [(0, 1, 2, 3, 4, 5), (0,)]
    assert (artifact.counters.calls, artifact.counters.warm_ups, gateway.warm_ups) == (2, 0, [])


async def test_referenced_requirements_and_mentioned_hubs_reach_the_prompt(scripted_gateway: Factory) -> None:
    gateway = scripted_gateway()
    answers = Windows(gateway)
    artifact = await run(gateway, PLAN)
    # "See section 4.2" in a#f0 finds b's section "4.2 Refunds" and its requirement; "Ledger" is named
    assert artifact.catalog_parts is not None
    assert artifact.catalog_parts["a@u0"] == CatalogParts(mentioned=("hl",), referenced=("rb",))
    assert artifact.catalogs["a@u0"] == ("m1", "m2", "r1", "hl", "rb")
    assert all("Refunds are paid within 5 days" in catalog for catalog in a_catalogs(answers))
    # without references the part stays empty and the requirement never reaches a's prompt
    gateway = scripted_gateway()
    answers = Windows(gateway)
    artifact = await run(gateway, PLAN, params=EdgeWindowParams(target_tokens=100, references=False))
    assert artifact.catalog_parts is not None
    assert artifact.catalog_parts["a@u0"].referenced == ()
    assert not any("Refunds are paid within 5 days" in catalog for catalog in a_catalogs(answers))


def a_catalogs(answers: Windows) -> list[list[str]]:
    """The catalogs unit a's calls showed (calls of all units run concurrently)."""

    found = [catalog for catalog in answers.catalogs if "Checkout service" in catalog]
    assert len(found) == len(A_WINDOWS)
    return found


# Update builds: reuse per window


async def first_build(scripted_gateway: Factory, store: MemoryStore) -> EdgesArtifact:
    gateway = scripted_gateway()
    Windows(gateway)
    return await run(gateway, PLAN, store=store)


async def test_an_unchanged_update_reuses_every_window(scripted_gateway: Factory) -> None:
    store = MemoryStore()
    first = await first_build(scripted_gateway, store)
    gateway = scripted_gateway()
    Windows(gateway)
    again = await run(gateway, update_plan(PLAN), store=store)
    assert (called(again), gateway.calls_for(EDGE_WINDOW_PROMPT), gateway.warm_ups) == ([], [], [])
    assert (again.counters.windows_reused, again.counters.units_reused, again.counters.catalogs_kept) == (4, 2, 2)
    assert edge_view(again) == edge_view(first)
    assert all(edge.reused for edge in again.raw_edges)


async def test_an_update_calls_only_the_window_of_a_dirty_fragment(scripted_gateway: Factory) -> None:
    store = MemoryStore()
    await first_build(scripted_gateway, store)
    gateway = scripted_gateway()
    answers = Windows(gateway)
    artifact = await run(gateway, update_plan(PLAN, ["a#f3"]), store=store)
    assert called(artifact) == [("a@u0", (2, 4), "dirty")]
    assert [window for window, _ in answers.seen] == [(2, 3)]
    counters = artifact.counters
    assert (counters.windows_called, counters.windows_reused, counters.warm_ups) == (1, 3, 0)
    assert (counters.units_extracted, counters.units_reused) == (1, 1)
    assert [(edge.fact, edge.reused) for edge in artifact.raw_edges] == [
        ("fact 0", True),
        ("fact 2", False),
        ("fact 4", True),
        ("fact 0", True),
    ]


async def test_a_clean_window_whose_window_catalog_changed_is_called(scripted_gateway: Factory) -> None:
    store = MemoryStore()
    await first_build(scripted_gateway, store)
    # a new hub from another document, named in a#f2: only the window holding a#f2 has a new window catalog
    resolved = ResolveArtifact(canonical=(*NODES, node("hp", "Payment step", "Module", "z#f0")))
    gateway = scripted_gateway()
    Windows(gateway)
    artifact = await run(gateway, update_plan(PLAN), store=store, resolved=resolved)
    assert called(artifact) == [("a@u0", (2, 4), "catalog_changed")]
    assert artifact.counters.windows_reused == 3
    assert artifact.catalog_parts is not None
    assert artifact.catalog_parts["a@u0"].mentioned == ("hl", "hp")


async def test_a_changed_fingerprint_calls_every_window_once(scripted_gateway: Factory) -> None:
    store = MemoryStore()
    await first_build(scripted_gateway, store)
    gateway = scripted_gateway()
    Windows(gateway)
    params = EdgeWindowParams(target_tokens=100, section_hints=True)
    artifact = await run(gateway, update_plan(PLAN), store=store, params=params)
    assert called(artifact) == [
        *(("a@u0", window, "not_stored") for window in A_WINDOWS),
        ("b@u0", (0, 1), "not_stored"),
    ]
    again = await run(gateway, update_plan(PLAN), store=store, params=params)
    assert called(again) == []


async def test_a_failed_window_falls_back_to_its_stored_edges_by_identity(scripted_gateway: Factory) -> None:
    store = MemoryStore()
    await first_build(scripted_gateway, store)
    gateway = scripted_gateway()
    Windows(gateway, fail=[2])
    artifact = await run(gateway, update_plan(PLAN, ["a#f3"]), store=store)
    assert called(artifact) == [("a@u0", (2, 4), "dirty")]
    # its content changed, so the edges of the same window identity (document and first fragment)
    assert [(edge.fact, edge.fragment_ids, edge.reused) for edge in artifact.raw_edges if edge.fact == "fact 2"] == [
        ("fact 2", ("a#f2",), True)
    ]
    counters = artifact.counters
    assert (counters.windows_fallback, counters.units_fallback, counters.failed_calls) == (1, 1, 1)
    assert (artifact.skipped, artifact.edge_failed_fragments) == ({}, ())


async def test_a_failed_window_without_stored_edges_skips_its_dirty_fragments(scripted_gateway: Factory) -> None:
    gateway = scripted_gateway()
    Windows(gateway, fail=[2])
    artifact = await run(gateway, update_plan(PLAN, ["a#f3"]), store=MemoryStore())
    assert artifact.skipped == {"a#f3": "llm_malformed"}
    assert artifact.edge_failed_fragments == ("a#f2", "a#f3")  # the failed window's fragments only
    assert artifact.counters.windows_fallback == 0
    # the other windows (no stored answer: not_stored) answered
    assert sorted(edge.fact for edge in artifact.raw_edges) == ["fact 0", "fact 0", "fact 4"]


# Document "c": eight fragments of 50 tokens in sections 1 (f0-f3), 2 (f4-f5) and 3 (f6-f7). At a target
# of 200 tokens its first window ends before f4 or f6, whichever has the lower content hash, so an edit
# of f4 re-cuts the unit while the first window keeps its identity (document and first fragment).
C_PARAMS = EdgeWindowParams(target_tokens=200)
C_HEADINGS = ("1 Gate",) * 4 + ("2 Vault",) * 2 + ("3 Audit",) * 2
C_RESOLVED = ResolveArtifact(canonical=(node("g1", "Gateway", "Module", "c#f0"), node("v1", "Vault", "Module", "c#f7")))


def c_plan(hashes: dict[int, str]) -> PlanArtifact:
    plan = first_plan(("c", [f"Text {index}." for index in range(8)], C_HEADINGS))
    fragments = [
        item.model_copy(update={"content_hash": hashes[item.ordinal]}) if item.ordinal in hashes else item
        for item in plan.fragments
    ]
    return plan.model_copy(update={"fragments": tuple(fragments)})


class PerFragment:
    """Answers each edge-window call with one fact per fragment of its window (``fact <index>``, tied to
    that fragment); ``fail`` windows (by first index) fail; ``fragments`` overrides an index's numbers."""

    def __init__(
        self, gateway: ScriptedGateway, fail: Sequence[int] = (), fragments: dict[int, list[int]] | None = None
    ) -> None:
        self.fail = set(fail)
        self.fragments = fragments or {}
        gateway.handle(EDGE_WINDOW_PROMPT, self)
        gateway.answer(ATTRIBUTES_PROMPT, None, LLMError("malformed"))

    def __call__(self, request: ScriptedRequest) -> Any:
        window = window_of(request)
        if window[0] in self.fail:
            return LLMError("malformed")
        return {
            "edges": [
                {
                    "source": "Gateway",
                    "relation": "INTEGRATES_WITH",
                    "target": "Vault",
                    "fact": f"fact {index}",
                    "fragments": self.fragments.get(index, [index + 1]),
                }
                for index in window
            ]
        }


async def c_run(gateway: ScriptedGateway, plan: PlanArtifact, store: MemoryStore) -> EdgesArtifact:
    return await run(gateway, plan, store=store, resolved=C_RESOLVED, params=C_PARAMS)


def answered(scripted_gateway: Factory) -> ScriptedGateway:
    gateway = scripted_gateway()
    PerFragment(gateway)
    return gateway


def facts(artifact: EdgesArtifact) -> list[tuple[str | None, tuple[str, ...], bool]]:
    return sorted((edge.fact, edge.fragment_ids, edge.reused) for edge in artifact.raw_edges)


async def test_an_identity_fallback_of_a_wider_window_leaves_the_fragments_it_did_not_hold_failed(
    scripted_gateway: Factory,
) -> None:
    store = MemoryStore()
    plan = c_plan({})  # "c-4" < "c-6": windows [0-4], [4-8]
    first = await c_run(answered(scripted_gateway), plan, store)
    assert called(first) == [("c@u0", (0, 4), "first"), ("c@u0", (4, 8), "first")]
    # f4 edited ("edited-c-4" > "c-6"): windows [0-6], [6-8]; [0-6] fails and falls back by identity to [0-4]
    gateway = scripted_gateway()
    PerFragment(gateway, fail=[0])
    artifact = await c_run(gateway, update_plan(plan, ["c#f4"]), store)
    assert called(artifact) == [("c@u0", (0, 6), "dirty"), ("c@u0", (6, 8), "not_stored")]
    assert artifact.counters.windows_fallback == 1
    assert facts(artifact) == [
        *((f"fact {index}", (f"c#f{index}",), True) for index in range(4)),
        *((f"fact {index}", (f"c#f{index}",), False) for index in (6, 7)),
    ]
    # f4 and f5 have no answer this build: Diff keeps their persisted edges
    assert (artifact.edge_failed_fragments, artifact.skipped) == (("c#f4", "c#f5"), {})


async def test_an_identity_fallback_of_a_narrower_window_drops_the_edges_outside_it(
    scripted_gateway: Factory,
) -> None:
    store = MemoryStore()
    plan = c_plan({4: "g-4", 6: "f-6"})  # "f-6" < "g-4": windows [0-6], [6-8]
    first = await c_run(answered(scripted_gateway), plan, store)
    assert called(first) == [("c@u0", (0, 6), "first"), ("c@u0", (6, 8), "first")]
    # f4 edited ("edited-g-4" < "f-6"): windows [0-4], [4-8]; the clean [0-4] (new content) fails and falls
    # back by identity to [0-6], whose facts on f4 and f5 are about another window's text
    gateway = scripted_gateway()
    PerFragment(gateway, fail=[0])
    artifact = await c_run(gateway, update_plan(plan, ["c#f4"]), store)
    assert called(artifact) == [("c@u0", (0, 4), "not_stored"), ("c@u0", (4, 8), "dirty")]
    assert facts(artifact) == [
        *((f"fact {index}", (f"c#f{index}",), True) for index in range(4)),
        *((f"fact {index}", (f"c#f{index}",), False) for index in range(4, 8)),
    ]
    assert (artifact.edge_failed_fragments, artifact.skipped) == ((), {})


async def test_an_edge_attributed_outside_its_window_is_kept_on_the_window_and_counted(
    scripted_gateway: Factory,
) -> None:
    gateway = scripted_gateway()
    # window [0-4]: fact 1 names f7 only (another window's text), fact 2 names no fragment
    PerFragment(gateway, fragments={1: [8], 2: []})
    artifact = await c_run(gateway, c_plan({}), MemoryStore())
    window = tuple(f"c#f{index}" for index in range(4))
    assert [edge.fragment_ids for edge in artifact.raw_edges if edge.fact in {"fact 1", "fact 2"}] == [window, window]
    assert (artifact.counters.attribution_fallbacks, artifact.counters.outside_window_edges) == (2, 1)


# Referenced entries


def test_a_referenced_entry_carries_its_first_quote_and_referring_sentence() -> None:
    quoted = node("r1", "Cart holds at most 50 items", "FunctionalRequirement", "b#f0").model_copy(
        update={"quotes": (SourceQuote(quote="A cart holds at most 50 items.", fragment_ids=("b#f0",)),)}
    )
    bare = node("r2", "Totals are rounded", "FunctionalRequirement", "b#f1")
    references = UnitReferences(
        cids=("r1", "r2", "r3"),
        sentences={"r1": "See section 2.1 of the Cart spec.", "r2": "As FR-2 says.", "r3": "Gone."},
    )
    found = referenced_entities(["r1", "r2", "r3"], references, {"r1": quoted, "r2": bare})
    assert found == {
        "r1": ReferencedEntity("A cart holds at most 50 items.", "See section 2.1 of the Cart spec."),
        "r2": ReferencedEntity("", "As FR-2 says."),
    }  # r3: no node any more


# Nearness vectors and the fingerprint


async def test_the_nearness_mode_picks_the_hub_vectors(scripted_gateway: Factory) -> None:
    named = ResolveArtifact(
        canonical=(
            node("m1", "Checkout service", "Module", "a#f0").model_copy(
                update={"name_embedding_text": "Module: Checkout"}
            ),
            node("r1", "Cart holds at most 50 items", "FunctionalRequirement", "a#f1"),
        )
    )
    gateway = scripted_gateway()
    cache = VectorCache(gateway, MemoryStore(), "p-edges")
    off = await catalog_vectors(PLAN, named, ONT, cache, EdgeWindowParams(nearness="off"))
    assert (off, gateway.calls) == (CatalogVectors(), [])
    assert await catalog_vectors(PLAN, named, ONT, cache) == CatalogVectors()  # the default is off
    by_name = await catalog_vectors(PLAN, named, ONT, cache, EdgeWindowParams(nearness="name"))
    assert set(by_name.hubs) == {"m1"} and set(by_name.fragments) == {item.fragment_id for item in PLAN.fragments}
    assert list(by_name.hubs["m1"]) == pytest.approx(gateway.vector("Module: Checkout"))
    rich = await catalog_vectors(PLAN, named, ONT, cache, EdgeWindowParams(nearness="rich"))
    text = rich_text(named.canonical[0], {item.fragment_id: item for item in PLAN.fragments})
    assert text == "Checkout service. A > 1 Intro"  # name and heading path (no quote)
    assert set(rich.hubs) == {"m1"} and list(rich.hubs["m1"]) == pytest.approx(gateway.vector(text))


def test_the_window_parameters_are_part_of_the_edge_fingerprint(scripted_gateway: Factory) -> None:
    gateway = scripted_gateway()
    options = BuildOptions()
    base = edge_fingerprint(ONT, gateway, options)
    assert base == edge_fingerprint(ONT, gateway, options, DEFAULT_EDGE_WINDOWS)
    variants = [
        EdgeWindowParams(target_tokens=None),
        EdgeWindowParams(target_tokens=6000),
        EdgeWindowParams(nearness="name"),
        EdgeWindowParams(nearness="rich"),
        EdgeWindowParams(section_hints=True),
        EdgeWindowParams(references=False),
    ]
    prints = {edge_fingerprint(ONT, gateway, options, params) for params in variants}
    assert base not in prints and len(prints) == len(variants)
