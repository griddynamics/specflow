"""Edge windows, the edge-window prompt and its calls offline."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable, Sequence

import pytest

from graph.build import changes
from graph.build.answers import edge_answer_model
from graph.build.calls import extract_edge_window_call, warm_up_edge_windows
from graph.build.changes import EdgeWindowParams, edge_windows
from graph.build.models import FragmentRec, Unit, Window
from graph.build.prompts import (
    EDGE_WINDOW_PROMPT,
    EDGE_WINDOW_WARM_UP_PROMPT,
    ENTITIES_LABEL,
    REFERENCE_TEXT_MAX_CHARS,
    REFERENCED_IN_KEY,
    SECTION_HINT_KEY,
    STATEMENT_KEY,
    ReferencedEntity,
    edge_catalog_message,
    edge_window_messages,
    edge_window_system_message,
    edge_window_warm_up_messages,
    fragment_text,
    integration_relations,
    node_messages,
    node_warm_up_messages,
    ownership_targets,
    provider_pairs,
    reference_relations,
    unit_message,
)
from graph.core.errors import ConformanceError
from graph.core.llm import untrusted
from graph.core.ontology import Ontology
from graph_support import ScriptedGateway, frozen_ontology, shipped_ontology

pytest_plugins = ["graph_support"]

Factory = Callable[..., ScriptedGateway]


# Edge windows (changes.edge_windows)


def fragment(index: int, tokens: int, heading: tuple[str, ...] = ("Doc",), salt: str = "") -> FragmentRec:
    return FragmentRec(
        fragment_id=f"d#f{index}",
        document_id="d",
        heading_path=heading,
        ordinal=index,
        line_start=index + 1,
        line_end=index + 1,
        text=f"text {index}",
        content_hash=hashlib.sha256(f"{index}{salt}".encode()).hexdigest(),
        tokens=tokens,
    )


def unit_of(fragments: Sequence[FragmentRec]) -> Unit:
    return Unit(
        unit_id=f"unit:{fragments[0].fragment_id}",
        document_id="d",
        fragment_ids=tuple(item.fragment_id for item in fragments),
        tokens=sum(item.tokens for item in fragments),
        content_hash="unit",
    )


def windows_of(fragments: Sequence[FragmentRec], params: EdgeWindowParams) -> tuple[Window, ...]:
    return edge_windows(unit_of(fragments), {item.fragment_id: item for item in fragments}, params)


def sizes(fragments: Sequence[FragmentRec], windows: Sequence[Window]) -> list[int]:
    return [sum(item.tokens for item in fragments[start:end]) for start, end in windows]


class Draws:
    """Seeded, deterministic draws (sha256 of the seed and a counter): the property tests' random units."""

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._count = itertools.count()

    def _next(self) -> int:
        digest = hashlib.sha256(f"{self._seed}:{next(self._count)}".encode()).digest()
        return int.from_bytes(digest[:8], "big")

    def random(self) -> float:
        return self._next() / 2**64

    def randint(self, low: int, high: int) -> int:
        return low + self._next() % (high - low + 1)

    def randrange(self, stop: int) -> int:
        return self._next() % stop

    def choice(self, items: Sequence[int]) -> int:
        return items[self.randrange(len(items))]


def random_unit(rng: Draws, count: int, most: int) -> list[FragmentRec]:
    section = 0
    fragments = []
    for index in range(count):
        if rng.random() < 0.3:
            section += 1
        heading = ("Doc", f"Section {section}") if rng.random() < 0.8 else ("Doc",)
        fragments.append(fragment(index, rng.randint(20, most), heading))
    return fragments


def covers(windows: Sequence[Window], count: int) -> bool:
    return windows[0][0] == 0 and windows[-1][1] == count and all(a[1] == b[0] for a, b in itertools.pairwise(windows))


def test_the_defaults_are_the_test_4_ones() -> None:
    params = EdgeWindowParams()
    assert (params.target_tokens, params.max_windows, params.nearness) == (3000, 8, "off")
    assert (params.section_hints, params.references) == (False, True)


def test_no_target_or_one_fragment_is_one_window() -> None:
    fragments = [fragment(index, 4000) for index in range(6)]
    assert windows_of(fragments, EdgeWindowParams(target_tokens=None)) == ((0, 6),)
    assert windows_of(fragments[:1], EdgeWindowParams(target_tokens=100)) == ((0, 1),)
    assert windows_of(fragments[:2], EdgeWindowParams(target_tokens=6000)) == ((0, 2),)  # 8,000 fits 1.5 x 6,000
    assert windows_of(fragments[:3], EdgeWindowParams(target_tokens=6000)) == ((0, 1), (1, 3))


@pytest.mark.parametrize("seed", range(40))
def test_windows_cover_the_unit_within_half_to_one_and_a_half_times_the_target(seed: int) -> None:
    rng = Draws(seed)
    target = rng.choice([1000, 3000, 6000])
    fragments = random_unit(rng, rng.randint(8, 80), target // 4)  # small fragments: a cut always fits
    if sum(item.tokens for item in fragments) <= 1.5 * target:
        fragments += [fragment(len(fragments) + index, target // 4) for index in range(8)]
    windows = windows_of(fragments, EdgeWindowParams(target_tokens=target, max_windows=100))
    assert covers(windows, len(fragments))
    assert all(0.5 * target <= size <= 1.5 * target for size in sizes(fragments, windows))


def test_a_cut_prefers_a_shallower_heading_then_the_lowest_content_hash() -> None:
    # 10 fragments of 100 tokens, target 400: the first window may end before fragment 2..6
    headings = [("A", "x"), ("A", "x"), ("A", "x"), ("A", "x"), ("B",), ("B",), ("B",), ("C",), ("C",), ("C",)]
    fragments = [fragment(index, 100, heading) for index, heading in enumerate(headings)]
    assert windows_of(fragments, EdgeWindowParams(target_tokens=400))[0] == (0, 4)  # "B" shares no heading
    flat = [fragment(index, 100) for index in range(10)]
    first_end = windows_of(flat, EdgeWindowParams(target_tokens=400))[0][1]
    assert first_end == min(range(2, 7), key=lambda index: flat[index].content_hash)


def test_the_same_input_gives_the_same_windows() -> None:
    fragments = random_unit(Draws(3), 60, 800)
    params = EdgeWindowParams(target_tokens=2000, max_windows=100)
    first = windows_of(fragments, params)
    reordered = dict(reversed([(item.fragment_id, item) for item in fragments]))
    assert edge_windows(unit_of(fragments), reordered, params) == first == windows_of(fragments, params)


def test_a_unit_never_gets_more_than_max_windows() -> None:
    fragments = [fragment(index, 500) for index in range(40)]  # 20,000 tokens
    windows = windows_of(fragments, EdgeWindowParams(target_tokens=1000))
    assert len(windows) <= changes.MAX_EDGE_WINDOWS
    assert covers(windows, 40)
    assert len(windows_of(fragments, EdgeWindowParams(target_tokens=1000, max_windows=3))) <= 3


def test_an_oversized_fragment_is_a_window_of_its_own() -> None:
    fragments = [fragment(0, 300), fragment(1, 5000), fragment(2, 300), fragment(3, 300), fragment(4, 300)]
    windows = windows_of(fragments, EdgeWindowParams(target_tokens=600, max_windows=100))
    assert covers(windows, 5)
    assert (1, 2) in windows


@pytest.mark.parametrize("seed", range(120))
def test_an_edit_moves_only_the_cuts_near_it(seed: int) -> None:
    """Property: change one fragment (its hash, and its size by up to 20%); every cut that moves lies
    within 3 x 1.5 x target tokens of the edited fragment; all cuts further away stay."""

    rng = Draws(seed)
    target = rng.choice([1000, 3000, 6000])
    fragments = random_unit(rng, rng.randint(20, 120), target // 3)
    params = EdgeWindowParams(target_tokens=target, max_windows=1000)
    before = windows_of(fragments, params)
    edited_at = rng.randrange(len(fragments))
    old = fragments[edited_at]
    tokens = max(1, old.tokens + rng.randint(-old.tokens // 5, old.tokens // 5))
    edited = [
        *fragments[:edited_at],
        fragment(edited_at, tokens, old.heading_path, "edit"),
        *fragments[edited_at + 1 :],
    ]
    after = windows_of(edited, params)
    position = [0]
    for item in fragments:
        position.append(position[-1] + item.tokens)
    moved = {start for start, _ in before[1:]} ^ {start for start, _ in after[1:]}
    reach = 3 * 1.5 * target
    assert all(abs(position[cut] - position[edited_at]) <= reach for cut in moved)


@pytest.mark.parametrize("seed", range(60))
def test_at_the_window_cap_an_edit_that_keeps_the_step_moves_only_the_cuts_near_it(seed: int) -> None:
    """Property at the cap (``max_windows`` 8, units of 12 to 60 target sizes): the raised target is a step ``target x
    1.25 ** k``, not ``tokens / 8``, so a size edit keeps it (here, every seed) and the cuts away from the edit stay,
    within 3 x 1.5 x the raised target (at most ``tokens / 8`` x 1.25).
    """

    rng = Draws(seed)
    target = rng.choice([1000, 3000])
    fragments = random_unit(rng, rng.randint(60, 160), target // 3)
    while sum(item.tokens for item in fragments) <= 12 * target:
        fragments.append(fragment(len(fragments), target // 4))
    params = EdgeWindowParams(target_tokens=target)
    before = windows_of(fragments, params)
    assert len(before) <= params.max_windows
    edited_at = rng.randrange(len(fragments))
    old = fragments[edited_at]
    tokens = max(1, old.tokens + rng.randint(-old.tokens // 5, old.tokens // 5))
    edited = [
        *fragments[:edited_at],
        fragment(edited_at, tokens, old.heading_path, "edit"),
        *fragments[edited_at + 1 :],
    ]
    after = windows_of(edited, params)
    position = list(itertools.accumulate((item.tokens for item in fragments), initial=0))
    moved = {start for start, _ in before[1:]} ^ {start for start, _ in after[1:]}
    reach = 3 * 1.5 * 1.25 * position[-1] / params.max_windows
    assert all(abs(position[cut] - position[edited_at]) <= reach for cut in moved)


def test_at_the_window_cap_an_edit_that_changes_the_step_may_move_every_cut() -> None:
    # 41 fragments of 300 tokens, target 1,000: eight windows at the cap; fragment 33 grows to 360 tokens
    # the unit now fits a smaller step in seven windows, and the cut before fragment 4 (29 fragments away) moves
    fragments = [fragment(index, 300) for index in range(41)]
    params = EdgeWindowParams(target_tokens=1000)
    before = windows_of(fragments, params)
    after = windows_of([*fragments[:33], fragment(33, 360, salt="edit"), *fragments[34:]], params)
    assert (len(before), len(after)) == (8, 7)
    assert before[0] == (0, 4) and after[0] == (0, 9)


# The edge-window prompt (prompts.edge_window_*)

UNIT = [
    fragment_text("Shop", ["Checkout"], "The cart must hold at most 50 items."),
    fragment_text("Shop", ["Checkout", "Payments API"], "Payments are taken through Stripe."),
    fragment_text("Shop", ["Orders"], "Customers can export their order history as CSV."),
    fragment_text("Shop", ["Orders"], "Exports run nightly."),
]
CATALOG = [("Checkout", "Feature"), ("Payments API", "Interface"), ("Orders service", "Module")]


@pytest.mark.parametrize("make", [frozen_ontology, shipped_ontology], ids=["frozen-v1.3", "shipped-v2.0"])
def test_every_window_of_a_unit_shares_the_system_unit_and_catalog_messages(make: Callable[[], Ontology]) -> None:
    ontology = make()
    calls = [edge_window_messages(ontology, UNIT, CATALOG, window) for window in ((0, 2), (2, 3), (3, 4), (0, 4))]
    prefix = edge_window_warm_up_messages(ontology, UNIT, CATALOG)
    assert [message["role"] for message in prefix] == ["system", "user", "user"]
    for call in calls:
        assert len(call) == 4
        assert json.dumps(call[:3]) == json.dumps(prefix)  # byte-identical cache prefix
        assert call[3] == {"role": "user", "content": call[3]["content"]}
    assert [call[3]["content"] for call in calls] == [
        "Window: extract only facts stated in fragments 1 to 2.",
        "Window: extract only facts stated in fragment 3.",
        "Window: extract only facts stated in fragment 4.",
        "Window: extract only facts stated in fragments 1 to 4.",
    ]
    # the unit message is byte-identical to the node calls' (one cached block of the fragments)
    assert prefix[1] == unit_message(UNIT) == node_warm_up_messages(ontology, UNIT)[1]
    assert node_messages(ontology, UNIT, [], (0, 2), node_list="hubs")[1] == prefix[1]
    assert prefix[0]["content"] == edge_window_system_message(ontology)


def test_the_catalog_is_an_untrusted_block_of_json_lines_with_optional_sections() -> None:
    message = edge_catalog_message(CATALOG, {"Checkout": "Checkout > Cart", "Unknown": "x"})
    lines = [
        {"name": "Checkout", "type": "Feature", SECTION_HINT_KEY: "Checkout > Cart"},
        {"name": "Payments API", "type": "Interface"},
        {"name": "Orders service", "type": "Module"},
    ]
    body = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines)
    assert message == {"role": "user", "content": untrusted(ENTITIES_LABEL, body)}
    assert edge_catalog_message(CATALOG) == edge_catalog_message(CATALOG, {})
    with pytest.raises(ConformanceError) as raised:
        edge_catalog_message([("A", "Module"), ("A", "Feature")])
    assert raised.value.code == "prompt_catalog_names_not_unique"


def test_a_referenced_entry_shows_its_statement_and_referring_sentence() -> None:
    referenced = {
        "Payments API": ReferencedEntity("Cards are charged once.", "See section 4.2 of the Billing spec."),
        "Orders service": ReferencedEntity("", "Orders follow FR-7. " + "x" * 300),
        "Unknown": ReferencedEntity("a", "b"),
    }
    message = edge_catalog_message(CATALOG, None, referenced)
    lines = [
        {"name": "Checkout", "type": "Feature"},
        {
            "name": "Payments API",
            "type": "Interface",
            STATEMENT_KEY: "Cards are charged once.",
            REFERENCED_IN_KEY: "See section 4.2 of the Billing spec.",
        },
        {
            "name": "Orders service",
            "type": "Module",
            REFERENCED_IN_KEY: ("Orders follow FR-7. " + "x" * 300)[:REFERENCE_TEXT_MAX_CHARS],
        },
    ]
    body = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines)
    assert message == {"role": "user", "content": untrusted(ENTITIES_LABEL, body)}
    # every window of a unit shares it (the warm-up's catalog message is the windows')
    ontology = shipped_ontology()
    warm = edge_window_warm_up_messages(ontology, UNIT, CATALOG, referenced=referenced)
    assert edge_window_messages(ontology, UNIT, CATALOG, (0, 2), referenced=referenced)[:3] == warm
    assert warm[2] == message


def test_the_reference_line_names_the_relations_from_the_ontology() -> None:
    ontology = shipped_ontology()
    assert reference_relations(ontology) == (("RELATES_TO",), ("DEPENDS_ON",), ("PART_OF",))
    system = edge_window_system_message(ontology)
    line = next(part for part in system.split("\n\n") if part.startswith("References:"))
    assert f'"{REFERENCED_IN_KEY}"' in line and f'"{STATEMENT_KEY}"' in line
    assert "by section, chapter or id" in line and "never each entity of the section" in line
    assert line.endswith(
        "RELATES_TO by default; DEPENDS_ON only when the sentence says one needs, requires or waits for the "
        "other; never PART_OF from a reference alone."
    )
    assert system.index("References:") < system.index("Entities: use each name")
    # the frozen v1.3 ontology flags no id-reference relation: no line
    assert "References:" not in edge_window_system_message(frozen_ontology())


def test_the_window_line_is_checked_against_the_unit() -> None:
    ontology = frozen_ontology()
    with pytest.raises(ConformanceError) as raised:
        edge_window_messages(ontology, UNIT, CATALOG, (2, 5))
    assert raised.value.code == "prompt_window_invalid"


def test_the_system_message_carries_the_ownership_and_provider_lines_from_the_ontology() -> None:
    ontology = shipped_ontology()
    assert ownership_targets(ontology) == {"PART_OF": ("Feature", "Module")}
    assert provider_pairs(ontology) == (("PROVIDED_BY", "Interface", "Module"),)
    system = edge_window_system_message(ontology)
    assert "PART_OF to a Feature or Module: only when the fact is in that entity's own section" in system
    (provider,) = [line for line in system.split("\n\n") if line.startswith("Each Interface")]
    assert provider.startswith(
        "Each Interface the window names gets PROVIDED_BY to the Module when the text, its section heading or a "
        "Provider, Owner or Service column names that Module. A row of a table (or an item of a list) of "
        "endpoints, paths, operations, tools, routes or queues gets its Module by its place: the Module a column "
        "of the row names as serving or running it"
    )
    assert "opening text of that section or chapter names as offering the API, catalog or base path" in provider
    assert "may be in a context fragment; the row is the fact. Never the Module that only calls or uses it" in provider
    # a queue's provider is the worker that serves it, not the module that sends work to it
    assert provider.endswith(
        "a queue's Module is the worker or deployment that its row names as serving or running it, never the "
        "Module that only sends work to it."
    )
    assert f'"{SECTION_HINT_KEY}"' in system and "third user message is the window line" in system
    # the lines come after the relation rules and before the entities rule
    assert system.index("DEPENDS_ON: only when") < system.index("PART_OF to a") < system.index("Lists are not")
    assert system.index("Lists are not") < system.index("Each Interface") < system.index("Entities: use each name")
    # an ontology without owner or exclusive pairs gets none of the three lines
    frozen = edge_window_system_message(frozen_ontology())
    assert "only when the fact is in that entity's own section" not in frozen and "Each " not in frozen
    assert "Lists are not owners" not in frozen


def test_the_list_line_names_the_owner_and_integration_relations_from_the_ontology() -> None:
    # owners and integrations taken from lists of tools in use and from scope or goal bullets
    ontology = shipped_ontology()
    assert integration_relations(ontology) == ("INTEGRATES_WITH",)
    (line,) = [part for part in edge_window_system_message(ontology).split("\n\n") if part.startswith("Lists are")]
    assert "a list or table of tools, systems or products already in use (or that existing tools may provide)" in line
    assert "a scope, goal or summary bullet naming an area of work, do not make the statements near them PART_OF" in (
        line
    )
    assert "PART_OF needs the statement to be in the item's own section (under its heading)" in line
    assert "Two items listed side by side get no INTEGRATES_WITH for that: INTEGRATES_WITH needs the text" in line
    assert " GOOD: " in line and " BAD: " in line and line.index(" GOOD: ") < line.index(" BAD: ")


def test_the_prompt_names_are_versioned() -> None:
    assert (EDGE_WINDOW_PROMPT, EDGE_WINDOW_WARM_UP_PROMPT) == ("edges.8", "edges.warmup.4")


# Calls


async def test_the_warm_up_is_a_strict_prefix_of_each_window_call(scripted_gateway: Factory) -> None:
    ontology = shipped_ontology()
    gateway = scripted_gateway()
    gateway.answer(EDGE_WINDOW_PROMPT, None, {"edges": []})
    sections = {"Checkout": "Checkout"}
    await warm_up_edge_windows(gateway, ontology, UNIT, CATALOG, sections=sections)
    for window in ((0, 2), (2, 4)):
        await extract_edge_window_call(gateway, ontology, UNIT, CATALOG, window, sections=sections)
    (warm,) = gateway.warm_ups
    windows = gateway.calls
    assert warm.prompt == EDGE_WINDOW_WARM_UP_PROMPT and warm.output_type is edge_answer_model(ontology)
    assert [call.prompt for call in windows] == [EDGE_WINDOW_PROMPT, EDGE_WINDOW_PROMPT]
    for call in windows:
        assert list(call.messages[: len(warm.messages)]) == list(warm.messages)
        assert len(call.messages) == len(warm.messages) + 1
        assert call.output_type is warm.output_type


async def test_a_window_call_keeps_facts_inside_the_window_and_drops_unknown_names(scripted_gateway: Factory) -> None:
    ontology = shipped_ontology()
    gateway = scripted_gateway()
    gateway.answer(
        EDGE_WINDOW_PROMPT,
        None,
        {
            "edges": [
                {
                    "source": "Payments API",
                    "relation": "PROVIDED_BY",
                    "target": "Orders service",
                    "fact": "Payments are taken through Stripe.",
                    "fragments": [2, 4],
                },
                {"source": "Nope", "relation": "PART_OF", "target": "Checkout", "fact": "x", "fragments": [2]},
            ]
        },
    )
    call = await extract_edge_window_call(gateway, ontology, UNIT, CATALOG, (1, 3))
    assert [(edge.source_name, edge.relation, edge.target_name, edge.indices) for edge in call.edges] == [
        ("Payments API", "PROVIDED_BY", "Orders service", (1,))
    ]
    assert call.unknown_names == 1
