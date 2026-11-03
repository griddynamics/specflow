"""Update stability of hub identities and edge window catalogs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Final

import pytest

from graph.build.changes import EdgeWindowParams
from graph.build.consolidate import HUBS_PROMPT, DecisionMemory
from graph.build.hubjudge import ConsolidationParams, modifier_of
from graph.build.models import (
    CanonicalNode,
    ConsolidationCounters,
    HubMerge,
    HubName,
    NodesArtifact,
    PlanArtifact,
    RawNode,
    ResolveArtifact,
    Unit,
)
from graph.build.resolve import PersistedIdentity, persisted_identities, resolve
from graph.build.terms import EdgeCatalog
from graph.build.vectors import VectorCache
from graph_extraction_store_support import MemoryStore
from graph_support import ScriptedGateway, frozen_ontology
from test_graph_resolve import (
    DEFAULT_PLAN,
    OMS,
    OMS_CATALOG,
    OMS_FULL,
    OMS_SYNONYMS,
    SPLIT,
    candidates_by_keys,
    judge_groups,
    no_groups,
    oms_raws,
    raw,
)

pytest_plugins = ["graph_support"]

ONT = frozen_ontology()
PROJECT: Final = "p-stability"
JUDGE_MODEL: Final = "test-judge"
Factory = Callable[..., ScriptedGateway]
BACKEND: Final = "Module|surface|backend"
SERVICE: Final = "Module|surface|backend service"
SYNONYMS: Final = [("Module: Backend", "Module: Backend service")]


async def run(
    raws: Sequence[RawNode],
    gateway: ScriptedGateway,
    store: MemoryStore,
    *,
    plan: PlanArtifact = DEFAULT_PLAN,
    persisted: dict[str, PersistedIdentity] | None = None,
) -> tuple[tuple[CanonicalNode, ...], ConsolidationCounters]:
    result = await resolve(
        plan,
        NodesArtifact(raw_nodes=tuple(raws)),
        gateway=gateway,
        ontology=ONT,
        vectors=VectorCache(gateway, store, PROJECT),
        memory=DecisionMemory(store, PROJECT, JUDGE_MODEL),
        persisted=persisted,
    )
    return result.canonical, result.counters.consolidation


def edited(plan: PlanArtifact, fragment_id: str, text: str) -> PlanArtifact:
    """``plan`` with ``fragment_id``'s text replaced (and a new content hash)."""

    fragments = tuple(
        fragment.model_copy(update={"text": text, "content_hash": f"{fragment.content_hash}-edited"})
        if fragment.fragment_id == fragment_id
        else fragment
        for fragment in plan.fragments
    )
    return plan.model_copy(update={"fragments": fragments, "dirty_ids": (fragment_id,)})


# Evidence hash: what the judge is shown


async def test_an_edit_that_leaves_the_judge_lines_keeps_the_decisions(scripted_gateway: Factory) -> None:
    store = MemoryStore()
    raws = [raw("Backend", "Module"), raw("Backend service", "Module")]
    first = scripted_gateway(synonyms=SYNONYMS)
    first.handle(HUBS_PROMPT, judge_groups((["Backend", "Backend service"], "Module")))
    one, _ = await run(raws, first, store)
    assert len(one) == 1

    # A line that names neither hub is added: the fragment's content hash changes, the judge lines do not.
    text = DEFAULT_PLAN.fragments[0].text + "\nRefunds are manual."
    second = scripted_gateway(synonyms=SYNONYMS)  # no judge script: any call fails the test
    two, counters = await run(raws, second, store, plan=edited(DEFAULT_PLAN, "doc#s0", text))
    assert second.calls_for(HUBS_PROMPT) == []
    assert [node.identity_key for node in two] == [node.identity_key for node in one]
    assert (counters.decisions_expired, counters.blocks_skipped) == (0, 1)


async def test_stored_raw_nodes_reused_in_another_order_keep_the_decisions(scripted_gateway: Factory) -> None:
    """An update reuses stored raw nodes in another order than the first build's calls gave them: the
    judge lines (requirements, quote ties, the leading name) and so the hashes stay (live no-op test)."""

    store = MemoryStore()
    raws = [
        raw("Backend", "Module", quote="The Backend handles every request."),
        raw("backend", "Module", quote="The Backend service is deployed twice."),
        raw("Backend service", "Module"),
        raw("Backend handles every request", "FunctionalRequirement"),
        raw("Backend is deployed twice", "FunctionalRequirement"),
        raw("Stripe takes payments", "FunctionalRequirement"),
        raw("Payments are taken by Stripe", "FunctionalRequirement"),
    ]
    first = scripted_gateway(synonyms=SYNONYMS)
    first.handle(HUBS_PROMPT, no_groups)
    await run(raws, first, store)
    second = scripted_gateway(synonyms=SYNONYMS)  # no judge script: any call fails the test
    _, counters = await run(list(reversed(raws)), second, store)
    assert second.calls_for(HUBS_PROMPT) == []
    assert (counters.decisions_expired, counters.blocks_skipped) == (0, 1)


async def test_an_edit_to_a_judge_line_expires_the_decisions(scripted_gateway: Factory) -> None:
    store = MemoryStore()
    raws = [raw("Backend", "Module"), raw("Backend service", "Module")]
    first = scripted_gateway(synonyms=SYNONYMS)
    first.handle(HUBS_PROMPT, judge_groups((["Backend", "Backend service"], "Module")))
    await run(raws, first, store)

    # The evidence line of "Backend service" changes: its entries expire and the block is judged again.
    text = "The Backend handles every request.\nThe Backend service is deployed three times.\nStripe takes payments."
    second = scripted_gateway(synonyms=SYNONYMS)
    second.handle(HUBS_PROMPT, no_groups)
    two, counters = await run(raws, second, store, plan=edited(DEFAULT_PLAN, "doc#s0", text))
    assert len(second.calls_for(HUBS_PROMPT)) == 1
    assert len(two) == 2
    assert counters.decisions_expired > 0


# Settled items keep their grouping


async def test_a_block_sent_again_never_joins_two_settled_items_no_build_judged(scripted_gateway: Factory) -> None:
    store = MemoryStore()
    raws = [
        raw("Backend", "Module"),
        raw("Backend service", "Module"),
        raw("Stripe", "Module"),
        raw("Ledger", "Module"),
    ]
    first = scripted_gateway(synonyms=SYNONYMS)
    first.handle(HUBS_PROMPT, no_groups)
    one, _ = await run(raws, first, store)
    assert len(one) == 4

    # "Backend service" is edited (its judge line changes), so the block goes to the judge again; the
    # answer groups the edited hub with "Backend" (allowed: one side changed) and the two settled hubs
    # "Stripe" and "Ledger", which no build judged as a pair (kept apart).
    text = "The Backend handles every request.\nThe Backend service is deployed three times.\nStripe takes payments."
    second = scripted_gateway(synonyms=SYNONYMS)
    second.handle(
        HUBS_PROMPT, judge_groups((["Backend", "Backend service"], "Module"), (["Stripe", "Ledger"], "Module"))
    )
    two, counters = await run(raws, second, store, plan=edited(DEFAULT_PLAN, "doc#s0", text))
    assert len(second.calls_for(HUBS_PROMPT)) == 1
    assert sorted(node.identity_key for node in two) == [SERVICE, "Module|surface|ledger", "Module|surface|stripe"]
    assert counters.kept_apart == 1
    decisions = store.of("decisions")
    assert f"{PROJECT}|hubs|Module|surface|ledger|Module|surface|stripe" not in decisions  # nothing stored


async def pair_run(
    gateway: ScriptedGateway, store: MemoryStore, *, catalog: Sequence[HubName] = (), plan: PlanArtifact = DEFAULT_PLAN
) -> ResolveArtifact:
    """OMS raws in blocks of two (OMS and its full name in different blocks), with a hub catalog."""

    return await resolve(
        plan,
        NodesArtifact(raw_nodes=tuple(oms_raws()), hub_catalog=tuple(catalog)),
        gateway=gateway,
        ontology=ONT,
        vectors=VectorCache(gateway, store, PROJECT),
        memory=DecisionMemory(store, PROJECT, JUDGE_MODEL),
        params=SPLIT,
    )


async def test_a_new_candidate_pair_of_two_settled_hubs_is_kept_apart_in_an_update(scripted_gateway: Factory) -> None:
    """a pair the first build never judged became a candidate (a neighbour vanished) and the
    update's pair pass merged two unchanged hubs. Here the catalog alias makes the new candidate."""

    store = MemoryStore()
    first = scripted_gateway(synonyms=OMS_SYNONYMS)
    first.handle(HUBS_PROMPT, no_groups)
    one = await pair_run(first, store)
    assert (OMS, OMS_FULL) not in candidates_by_keys(one)

    second = scripted_gateway(synonyms=OMS_SYNONYMS)  # no judge script: any call fails the test
    two = await pair_run(second, store, catalog=OMS_CATALOG)
    assert second.calls_for(HUBS_PROMPT) == []
    assert two.merges == ()
    assert candidates_by_keys(two)[(OMS, OMS_FULL)].outcome == "kept_apart"
    assert two.counters.consolidation.kept_apart == 1
    decisions = store.of("decisions")
    assert f"{PROJECT}|hubs|{OMS}|{OMS_FULL}" not in decisions  # nothing stored: kept apart next time too


async def test_a_new_candidate_pair_with_a_changed_side_is_judged_in_an_update(scripted_gateway: Factory) -> None:
    store = MemoryStore()
    first = scripted_gateway(synonyms=OMS_SYNONYMS)
    first.handle(HUBS_PROMPT, no_groups)
    await pair_run(first, store)

    # A line naming the Order management service changes its judge line: one side changed.
    text = DEFAULT_PLAN.fragments[0].text + "\nThe Order management service keeps every order."
    second = scripted_gateway(synonyms=OMS_SYNONYMS)
    second.handle(HUBS_PROMPT, judge_groups((["OMS", "Order management service"], "Module")))
    two = await pair_run(second, store, catalog=OMS_CATALOG, plan=edited(DEFAULT_PLAN, "doc#s0", text))
    assert two.merges == (HubMerge(anchor_key=OMS, member_key=OMS_FULL, source="pair", confidence="high"),)
    assert candidates_by_keys(two)[(OMS, OMS_FULL)].outcome == "same"
    assert two.counters.consolidation.kept_apart == 0


async def test_a_pair_left_over_the_cap_is_still_judged_although_both_hubs_are_settled(
    scripted_gateway: Factory,
) -> None:
    store = MemoryStore()
    first = scripted_gateway(synonyms=OMS_SYNONYMS)
    first.handle(HUBS_PROMPT, no_groups)
    capped = ConsolidationParams(block_size=2, block_overlap=0, pair_call_cap=0)
    one = await resolve(
        DEFAULT_PLAN,
        NodesArtifact(raw_nodes=tuple(oms_raws()), hub_catalog=OMS_CATALOG),
        gateway=first,
        ontology=ONT,
        vectors=VectorCache(first, store, PROJECT),
        memory=DecisionMemory(store, PROJECT, JUDGE_MODEL),
        params=capped,
    )
    assert candidates_by_keys(one)[(OMS, OMS_FULL)].outcome == "over_cap"
    assert f"{PROJECT}|hubs.queued|{OMS}|{OMS_FULL}" in store.of("decisions")

    second = scripted_gateway(synonyms=OMS_SYNONYMS)
    second.handle(HUBS_PROMPT, judge_groups((["OMS", "Order management service"], "Module")))
    two = await pair_run(second, store, catalog=OMS_CATALOG)
    assert len(second.calls_for(HUBS_PROMPT)) == 1  # the blocks are decided: only the pair batch
    assert candidates_by_keys(two)[(OMS, OMS_FULL)].outcome == "same"
    assert two.counters.consolidation.kept_apart == 0


async def test_items_judged_for_the_first_time_are_never_kept_apart(scripted_gateway: Factory) -> None:
    gateway = scripted_gateway(synonyms=SYNONYMS)
    gateway.handle(HUBS_PROMPT, judge_groups((["Stripe", "Ledger"], "Module")))
    nodes, counters = await run([raw("Stripe", "Module"), raw("Ledger", "Module")], gateway, MemoryStore())
    assert len(nodes) == 1
    assert counters.kept_apart == 0


# Anchors and window catalogs


def window_catalog(nodes: Sequence[CanonicalNode]) -> tuple[str, ...]:
    """The window catalog of ``DEFAULT_PLAN``'s first fragment (its own unit) over ``nodes``."""

    fragment = DEFAULT_PLAN.fragments[0]
    unit = Unit(unit_id="u", document_id="doc", fragment_ids=(fragment.fragment_id,), tokens=8, content_hash="u")
    catalog = EdgeCatalog(
        fragments_by_id={fragment.fragment_id: fragment},
        canonical=nodes,
        hub_cids=frozenset(node.cid for node in nodes),
        name_vectors={},
        fragment_vectors={},
        limit=10,
        params=EdgeWindowParams(),
    )
    return catalog.window_catalog(unit, (0, 1), catalog.select(unit))


async def test_a_node_continuing_a_persisted_node_keeps_its_anchor_and_window_catalog(
    scripted_gateway: Factory,
) -> None:
    grouped = judge_groups((["Backend", "Backend service"], "Module"))
    first = scripted_gateway(synonyms=SYNONYMS)
    first.handle(HUBS_PROMPT, grouped)
    built, _ = await run(
        [raw("Backend service", "Module"), raw("Backend", "Module"), raw("Backend", "Module")], first, MemoryStore()
    )
    (node,) = built
    assert (node.identity_key, node.name) == (BACKEND, "Backend")  # the most frequent name leads

    # The next build sees "Backend service" more often: without the persisted graph the name and key
    # would flip, and every window naming the hub would change its window catalog.
    raws = [raw("Backend service", "Module"), raw("Backend service", "Module"), raw("Backend", "Module")]
    fresh = scripted_gateway(synonyms=SYNONYMS)
    fresh.handle(HUBS_PROMPT, grouped)
    (flipped,), _ = await run(raws, fresh, MemoryStore())
    assert flipped.identity_key == SERVICE
    kept = scripted_gateway(synonyms=SYNONYMS)
    kept.handle(HUBS_PROMPT, grouped)
    persisted = persisted_identities([(node.identity_key, node.identity_aliases, node.label)])
    (update,), _ = await run(raws, kept, MemoryStore(), persisted=persisted)
    assert (update.identity_key, update.name, update.identity_aliases) == (BACKEND, "Backend", (SERVICE,))
    assert window_catalog([update]) == window_catalog([node]) != window_catalog([flipped])


# wrong hub merges in the update


async def test_a_hub_stored_different_from_two_settled_hubs_never_bridges_them(scripted_gateway: Factory) -> None:
    """a block re-sent for an edited hub grouped two settled hubs no build judged together with
    a third settled hub stored ``different`` from both; the joiner left the third out and merged the two."""

    store = MemoryStore()
    raws = [raw("Backend", "Module"), raw("Backend service", "Module")] + [
        raw(name, "Module") for name in ("Billing", "Invoicing", "Payments")
    ]

    def gateway() -> ScriptedGateway:
        made = scripted_gateway(synonyms=SYNONYMS)
        # Payments is close to both (judged, stored different); Billing and Invoicing are not (never judged).
        return made.relate("Module: Payments", 0.9, texts=["Module: Billing", "Module: Invoicing"])

    first = gateway()
    first.handle(HUBS_PROMPT, no_groups)
    one, _ = await run(raws, first, store)
    assert len(one) == 5
    decisions = store.of("decisions")
    assert f"{PROJECT}|hubs|Module|surface|billing|Module|surface|payment" in decisions
    assert f"{PROJECT}|hubs|Module|surface|billing|Module|surface|invoicing" not in decisions

    text = "The Backend handles every request.\nThe Backend service is deployed three times.\nStripe takes payments."
    second = gateway()
    second.handle(HUBS_PROMPT, judge_groups((["Billing", "Invoicing", "Payments"], "Module")))
    two, counters = await run(raws, second, store, plan=edited(DEFAULT_PLAN, "doc#s0", text))
    assert len(second.calls_for(HUBS_PROMPT)) == 1
    assert len(two) == 5
    assert counters.kept_apart == 1
    assert f"{PROJECT}|hubs|Module|surface|billing|Module|surface|invoicing" not in store.of("decisions")


USERS: Final = "Feature|surface|user"
PROVISIONING: Final = "Feature|surface|user provisioning"
WHOLE_AND_PART: Final = judge_groups((["Users", "User provisioning"], "Feature"))


def persisted_hubs(*keys: str) -> dict[str, PersistedIdentity]:
    return persisted_identities([(key, (), key.partition("|")[0]) for key in keys])


async def test_two_persisted_hubs_of_a_whole_and_its_part_never_merge_in_an_update(scripted_gateway: Factory) -> None:
    """an edit gave the persisted Users page provisioning lines and the update merged it into
    the persisted User Provisioning and Profile."""

    store = MemoryStore()
    gateway = scripted_gateway(synonyms=[("Feature: Users", "Feature: User provisioning")])
    gateway.handle(HUBS_PROMPT, WHOLE_AND_PART)
    raws = [raw("Users", "Feature"), raw("User provisioning", "Feature")]
    nodes, counters = await run(raws, gateway, store, persisted=persisted_hubs(USERS, PROVISIONING))
    assert sorted(node.identity_key for node in nodes) == [USERS, PROVISIONING]
    assert counters.vetoes == 1
    assert f"{PROJECT}|hubs|{USERS}|{PROVISIONING}" not in store.of("decisions")  # never judged, never stored

    # A first build (nothing persisted) judges the pair as before.
    first = scripted_gateway(synonyms=[("Feature: Users", "Feature: User provisioning")])
    first.handle(HUBS_PROMPT, WHOLE_AND_PART)
    merged, _ = await run(raws, first, MemoryStore())
    assert len(merged) == 1


@pytest.mark.parametrize(
    ("names", "persisted"),
    [
        (("Users", "User provisioning"), (USERS,)),  # a new hub joins a persisted one
        (("Users", "Users for admins"), (USERS, "Feature|surface|user for admin")),  # still names the users
        (("User provisioning", "User provisioning service"), ()),  # no persisted graph
    ],
)
async def test_a_whole_and_its_part_still_merge_with_a_new_hub_or_a_qualifier(
    scripted_gateway: Factory, names: tuple[str, str], persisted: tuple[str, ...]
) -> None:
    gateway = scripted_gateway(synonyms=[tuple(f"Feature: {name}" for name in names)])
    gateway.handle(HUBS_PROMPT, judge_groups((list(names), "Feature")))
    nodes, counters = await run(
        [raw(name, "Feature") for name in names], gateway, MemoryStore(), persisted=persisted_hubs(*persisted)
    )
    assert len(nodes) == 1
    assert counters.vetoes == 0


async def test_a_new_hub_never_chains_a_persisted_whole_and_its_part(scripted_gateway: Factory) -> None:
    gateway = scripted_gateway()
    gateway.handle(HUBS_PROMPT, judge_groups((["Accounts", "Users", "User provisioning"], "Feature")))
    raws = [raw("Accounts", "Feature"), raw("Users", "Feature"), raw("User provisioning", "Feature")]
    nodes, counters = await run(raws, gateway, MemoryStore(), persisted=persisted_hubs(USERS, PROVISIONING))
    keys = [{node.identity_key, *node.identity_aliases} for node in nodes]
    assert not any({USERS, PROVISIONING} <= found for found in keys)
    assert counters.vetoes == 1


@pytest.mark.parametrize(
    ("short", "long", "expected"),
    [
        ("user", "user provisioning and profile", True),
        ("atlassian", "atlassian cloud", True),
        ("connector", "connector sync", True),
        ("todo item", "todo item with question json", False),
        ("dcr", "dcr for mcp client", False),
        ("keycloak", "keycloak and identity provider", False),
        ("administration", "install administration", False),
        ("user", "user", False),
        ("user", "users", False),
        ("", "user", False),
    ],
)
def test_a_name_is_a_modifier_of_a_longer_name_that_continues_it_without_a_link_word(
    short: str, long: str, *, expected: bool
) -> None:
    assert modifier_of(short, long) is expected
