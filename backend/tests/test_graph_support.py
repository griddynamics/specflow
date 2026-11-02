"""Self-tests of the ``graph_support`` doubles later milestones rely on: the ``ScriptedGateway`` and ``ShopAnswers``
(the shop answer file's parser and its coverage of the planned windows and units).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import Sequence
from typing import Any

import numpy as np
import pytest
from pydantic import BaseModel

from graph.build import extract, open_file_stores, steps
from graph.build.answers import HUBS_LIST, STATEMENTS_LIST, NodeList, edge_answer_model, node_answer_model
from graph.build.models import PlanArtifact
from graph.build.options import BuildContext
from graph.build.prompts import (
    NODE_PROMPTS,
    attribute_messages,
    breadcrumb,
    edge_window_messages,
    fragment_text,
    node_messages,
)
from graph.core import ConfigurationError, JudgeRunner, LLMError, Verdict
from graph.core.llm import Gateway, untrusted
from graph_support import (
    ATTRIBUTES_PROMPT,
    DEDUP_PROMPT,
    EDGE_WINDOW_PROMPT,
    HUBS_PROMPT,
    NO_ANSWER,
    OfflineExecutor,
    ScriptedGateway,
    ScriptedRequest,
    ScriptError,
    ShopAnswers,
    UnscriptedRequestError,
    assert_all_scripted,
    llm_config,
    shipped_ontology,
    shop_answer_data,
    shop_answers,
    shop_corpus,
)

USER = [{"role": "user", "content": "x"}]


class Pick(BaseModel):
    label: str
    names: list[str] = []


class Named(BaseModel):
    label: str
    note: str = ""
    names: list[str] = []
    by_label: dict[str, list[str]] = {}
    count: int = 0


def by_subject(request: Any) -> str:
    return request.user_json()["subject"]


# The double matches the real gateway


def public_names(cls: type) -> set[str]:
    return {name for name in dir(cls) if not name.startswith("_")}


def test_scripted_gateway_has_every_public_name_of_gateway_with_the_same_signature() -> None:
    missing = public_names(Gateway) - public_names(ScriptedGateway)
    assert missing == set()
    for name in sorted(public_names(Gateway)):
        real, double = inspect.getattr_static(Gateway, name), inspect.getattr_static(ScriptedGateway, name)
        assert type(real) is type(double), name  # property, staticmethod or plain function alike
        if isinstance(real, property):
            real, double = real.fget, double.fget
        elif isinstance(real, staticmethod):
            real, double = real.__func__, double.__func__
        assert inspect.signature(double) == inspect.signature(real), name
        assert inspect.iscoroutinefunction(double) == inspect.iscoroutinefunction(real), name


def test_scripted_gateway_untrusted_is_the_real_function() -> None:
    assert ScriptedGateway.untrusted is untrusted
    assert ScriptedGateway(llm_config()).untrusted("l", "t") == Gateway.untrusted("l", "t")


# Answers and recording


async def test_scripted_gateway_answers_by_semantic_key_and_records_calls(scripted_gateway) -> None:
    gateway = scripted_gateway(llm_config()).key_by("classify", by_subject)
    gateway.answer("classify", "Checkout", {"label": "Module", "names": ["Checkout"]})
    gateway.answer("classify", "Export", Pick(label="FunctionalRequirement"))
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": '{"subject": "Checkout"}'}]

    first = await gateway.structured("small", None, messages, Pick, prompt="classify")
    messages[1]["content"] = '{"subject": "Export"}'
    second = await gateway.structured("extraction", "other-model", messages, Pick, prompt="classify")

    assert first == Pick(label="Module", names=["Checkout"])
    assert second.label == "FunctionalRequirement"
    assert [(c.call_class, c.model, c.key) for c in gateway.calls] == [
        ("small", "test-small", "Checkout"),
        ("extraction", "other-model", "Export"),
    ]
    assert gateway.calls[0].messages[1]["content"] == '{"subject": "Checkout"}'
    usage = gateway.usage().total(prompt="classify")
    assert (usage.calls, usage.requests, usage.failures) == (2, 2, 0)


async def test_scripted_gateway_handler_fallback_and_scripted_errors(scripted_gateway) -> None:
    gateway = scripted_gateway(
        llm_config(),
        handlers={"h": lambda request: NO_ANSWER if "skip" in request.user else {"label": "Module"}},
        fallback=lambda request: {"label": f"fallback:{request.prompt}"},
        rename={"Module": "Component"},
    )
    gateway.answer("boom", None, LLMError("malformed"))
    assert (await gateway.structured("small", None, USER, Pick, prompt="h")).label == "Component"
    skipped = await gateway.structured("small", None, [{"role": "user", "content": "skip"}], Pick, prompt="h")
    assert skipped.label == "fallback:h"
    with pytest.raises(LLMError):
        await gateway.structured("judge", None, USER, Pick, prompt="boom")
    assert gateway.usage().total(prompt="boom").failures == 1
    assert gateway.unscripted == []


async def test_scripted_gateway_refuses_unusable_scripts_and_the_embedding_class() -> None:
    strict = ScriptedGateway(llm_config())
    strict.answer("bad", None, {"names": "not-a-list"})
    with pytest.raises(ScriptError):
        await strict.structured("small", None, USER, Pick, prompt="bad")
    with pytest.raises(ConfigurationError):
        await strict.structured("embedding", None, USER, Pick, prompt="bad")
    assert strict.unscripted == []


# Unscripted requests cannot be swallowed


async def test_an_unscripted_request_raises_and_is_recorded() -> None:
    gateway = ScriptedGateway(llm_config())
    with pytest.raises(UnscriptedRequestError, match="'nothing'"):
        await gateway.structured("small", None, USER, Pick, prompt="nothing")
    assert len(gateway.unscripted) == 1 and "'nothing'" in gateway.unscripted[0]


async def test_a_swallowed_unscripted_request_still_fails_the_test() -> None:
    gateway = ScriptedGateway(llm_config())
    with contextlib.suppress(Exception):  # library code that catches every error would hide it
        await gateway.structured("small", None, USER, Pick, prompt="nothing")
    with pytest.raises(pytest.fail.Exception, match="unscripted LLM requests"):
        assert_all_scripted(ScriptedGateway(llm_config()), gateway)
    assert_all_scripted(ScriptedGateway(llm_config()))  # a clean gateway passes


# rename: exact strings only


async def test_rename_replaces_exact_strings_and_keys_at_any_depth_only() -> None:
    gateway = ScriptedGateway(llm_config(), rename={"Module": "Component", "PART_OF": "BELONGS_TO"})
    gateway.answer(
        "p",
        None,
        {
            "label": "Module",
            "note": "Module service is PART_OF checkout",
            "names": ["Module", "module", "Modules", "PART_OF"],
            "by_label": {"Module": ["PART_OF", "x PART_OF"]},
            "count": 3,
        },
    )
    result = await gateway.structured("small", None, USER, Named, prompt="p")
    assert result == Named(
        label="Component",
        note="Module service is PART_OF checkout",  # substrings are never touched
        names=["Component", "module", "Modules", "BELONGS_TO"],  # case-sensitive, whole value only
        by_label={"Component": ["BELONGS_TO", "x PART_OF"]},  # mapping keys too
        count=3,
    )
    # an output-type instance is dumped and renamed as well
    gateway.answer("q", None, Named(label="Module"))
    assert (await gateway.structured("small", None, USER, Named, prompt="q")).label == "Component"


# Embeddings and concurrency


async def test_scripted_gateway_embeddings_are_deterministic_unit_vectors() -> None:
    gateway = ScriptedGateway(llm_config(embedding_dim=32), synonyms=[["Orders service", "Order management service"]])
    vectors = await gateway.embed(["Orders service", "Order management service", "Stripe", "Orders service"])
    assert all(len(vector) == 32 for vector in vectors)
    assert abs(sum(x * x for x in vectors[2]) - 1.0) < 1e-9
    assert vectors[0] == vectors[1] == vectors[3]
    assert vectors[0] != vectors[2]
    assert vectors[2] == ScriptedGateway(llm_config(embedding_dim=32)).vector("Stripe")
    assert gateway.calls_for(call_class="embedding")[0].texts[2] == "Stripe"


def cosine(first: Sequence[float], second: Sequence[float]) -> float:
    return float(np.dot(first, second) / (np.linalg.norm(first) * np.linalg.norm(second)))


async def test_relate_puts_texts_at_the_declared_cosine_to_an_anchor() -> None:
    plain = ScriptedGateway(llm_config(embedding_dim=32))
    gateway = ScriptedGateway(llm_config(embedding_dim=32)).relate(
        "Feature: Export", 0.7, texts=["export text"], matching=lambda text: text.startswith("Exports")
    )
    anchor, exact, matched, other = await gateway.embed(["Feature: Export", "export text", "Exports run nightly", "x"])
    assert anchor == plain.vector("Feature: Export")  # the anchor keeps its own vector
    assert other == plain.vector("x")  # an unrelated text too
    assert cosine(exact, anchor) == pytest.approx(0.7) and cosine(matched, anchor) == pytest.approx(0.7)
    assert all(abs(float(np.linalg.norm(vector)) - 1.0) < 1e-9 for vector in (exact, matched))
    assert exact != plain.vector("export text")
    assert gateway.vector("export text") == exact  # deterministic


async def test_relate_to_several_anchors_and_to_a_synonym_anchor() -> None:
    gateway = ScriptedGateway(llm_config(embedding_dim=32), synonyms=[["Module: A", "Module: A2"]])
    gateway.relate("Module: A2", 0.6, texts=["t"]).relate("Module: B", 0.5, texts=["t"])
    t, a, b = gateway.vector("t"), gateway.vector("Module: A"), gateway.vector("Module: B")
    # the anchor's synonym group shares its vector; later anchors hold up to the anchors' tiny overlap
    assert cosine(t, a) == pytest.approx(0.6)
    assert cosine(t, b) == pytest.approx(0.5, abs=0.05)


def test_relate_refuses_impossible_cosines() -> None:
    gateway = ScriptedGateway(llm_config(embedding_dim=32))
    for value in (1.0, -1.0, 1.5):
        with pytest.raises(ScriptError):
            gateway.relate("a", value, texts=["t"])
    gateway.relate("a", 0.8, texts=["t"]).relate("b", 0.7, texts=["t"])  # 0.64 + 0.49 >= 1
    with pytest.raises(ScriptError, match="square-sum"):
        gateway.vector("t")


def test_shop_answers_check_and_install_related_fragments() -> None:
    related = {**MINI, "related": [{"anchor": "Module: Alpha", "cosine": 0.7, "fragments": ["a"], "texts": ["extra"]}]}
    answers = ShopAnswers(related)
    assert answers.related[0].fragments == ("a",) and answers.related[0].texts == ("extra",)
    matches = answers.fragment_text_matcher(["a"])
    # an embedded fragment text, by its first body line (headings skipped)
    assert matches("## A\n\nAlpha starts here.") and not matches("## B\nBeta starts.")
    assert not answers.fragment_text_matcher([])("Alpha starts")
    gateway = answers.install(ScriptedGateway(llm_config(embedding_dim=32)))
    anchor = gateway.vector("Module: Alpha")
    assert cosine(gateway.vector("# A\nAlpha starts here."), anchor) == pytest.approx(0.7)
    assert cosine(gateway.vector("extra"), anchor) == pytest.approx(0.7)
    assert cosine(gateway.vector("Beta starts."), anchor) < 0.7
    for fragments, message in ((["zzz"], "is not declared"), (["b"], "shares its first line")):
        broken = {**MINI, "related": [{"anchor": "Module: Alpha", "cosine": 0.7, "fragments": fragments}]}
        with pytest.raises(ScriptError, match=message):
            ShopAnswers(broken)


async def test_scripted_gateway_tracks_concurrency(scripted_gateway) -> None:
    """Five calls held at a barrier inside the handler are all in flight at once."""

    calls = 5
    arrived = 0
    everyone_in = asyncio.Event()

    async def hold(request: Any) -> dict[str, str]:
        nonlocal arrived
        arrived += 1
        if arrived == calls:
            everyone_in.set()
        await everyone_in.wait()
        return {"label": "x"}

    gateway = scripted_gateway(llm_config(), handlers={"p": hold})
    await asyncio.wait_for(
        asyncio.gather(*(gateway.structured("small", None, USER, Pick, prompt="p") for _ in range(calls))), 1
    )
    assert gateway.max_in_flight == calls
    assert gateway.max_in_flight_by_class == {"small": calls}
    assert gateway.in_flight == 0


async def test_scripted_gateway_drives_judge_runner(scripted_gateway) -> None:
    gateway = scripted_gateway(llm_config())
    gateway.answer(
        "judge_endpoints.3",
        None,
        {"confidence": 0.9, "evidence": [{"fragment_id": "f1", "quote": "q"}]},
    )
    verdict = await JudgeRunner(gateway, shipped_ontology()).judge("judge_endpoints.3", {"subject": "S"}, Verdict)
    assert isinstance(verdict, Verdict)
    assert verdict.confidence == 0.9
    assert verdict.prompt_ref == "judge_endpoints.3"
    assert gateway.calls[0].call_class == "judge"
    assert gateway.calls[0].model == "test-main"


# ShopAnswers: coverage of the shop plans (each snapshot under its answer file's options)

SHOP_FRAGMENTS = {1: 43, 2: 43, 3: 36}


async def shop_plan(root: Any, snapshot: int = 1) -> PlanArtifact:
    """The real Plan step over a shop snapshot with its answers' options (no Neo4j: nothing persisted,
    so a ``first`` plan whose windows cover every fragment)."""

    answers = shop_answers(snapshot)
    stores = open_file_stores(root)
    ctx = BuildContext(
        project_id="pytest-graph-answers",
        ontology=shipped_ontology(),
        driver=None,
        gateway=ScriptedGateway(),
        registry=stores.registry,
        corpus=shop_corpus(snapshot, root),
        extraction=stores.extraction,
        builds=stores.builds,
        options=answers.options,
        executor=OfflineExecutor(),
    )
    build_id = await steps.plan(ctx)
    payload = await stores.builds.get_artifact(ctx.project_id, build_id, "plan")
    assert payload is not None
    return PlanArtifact.model_validate(payload)


@pytest.mark.parametrize("snapshot", sorted(SHOP_FRAGMENTS))
async def test_every_shop_handle_names_exactly_one_planned_fragment_and_every_fragment_has_one(
    tmp_path: Any, snapshot: int
) -> None:
    answers = shop_answers(snapshot)
    plan = await shop_plan(tmp_path, snapshot)
    titles = {document.document_id: document.title for document in plan.documents}
    by_id = {fragment.fragment_id: fragment for fragment in plan.fragments}
    handle_of: dict[str, str] = {}
    for unit in plan.units:
        seen: dict[tuple[str, str], int] = {}
        for fragment_id in unit.fragment_ids:
            fragment = by_id[fragment_id]
            crumb = breadcrumb(titles[unit.document_id], fragment.heading_path)
            first = next(
                line.strip() for line in fragment.text.split("\n") if line.strip() and not line.lstrip().startswith("#")
            )
            seen[(crumb, first)] = seen.get((crumb, first), 0) + 1
            handle = answers.handle_of(crumb, fragment.text, seen[(crumb, first)])
            assert handle is not None, f"no handle names {fragment_id}"
            handle_of[fragment_id] = handle
    assert len(handle_of) == len(plan.fragments) == len(answers.fragments) == SHOP_FRAGMENTS[snapshot]
    assert sorted(handle_of.values()) == sorted(answers.fragments)  # one fragment per handle
    # ``fragment_ids`` (what the live tests use) agrees with the request-side matching
    assert answers.fragment_ids(plan) == {handle: fragment_id for fragment_id, handle in handle_of.items()}

    planned_windows = [
        tuple(handle_of[fid] for fid in unit.fragment_ids[start:end])
        for unit in plan.units
        for start, end in unit.windows
    ]
    planned_units = {tuple(sorted(handle_of[fid] for fid in unit.fragment_ids)) for unit in plan.units}
    assert set(answers.edges) <= planned_units  # every answered unit is a planned unit
    # every answered window is a contiguous run of one planned window (an update narrows a window to
    # its dirty fragments)
    runs = {
        tuple(sorted(window[start:end]))
        for window in planned_windows
        for start in range(len(window))
        for end in range(start + 1, len(window) + 1)
    }
    assert set(answers.nodes) <= runs, sorted(set(answers.nodes) - runs)
    if snapshot == 1:  # the first build answers every planned window and unit
        assert set(answers.nodes) == {tuple(sorted(window)) for window in planned_windows}
        assert len(planned_windows) == len(answers.nodes)
        assert planned_units == set(answers.edges) and len(plan.units) == len(answers.edges)
    if snapshot == 3:  # 3.yaml also answers the rebuild of snapshot 3: every planned window
        assert {tuple(sorted(window)) for window in planned_windows} <= set(answers.nodes)


def test_shop_answer_data_is_a_fresh_copy_each_time() -> None:
    data = shop_answer_data(1)
    data["attributes"].clear()
    data["nodes"].append({"window": ["x"], "entities": []})
    again = shop_answer_data(1)
    assert again["attributes"] and again != data
    assert ShopAnswers(again, source="1.yaml").fragments == shop_answers(1).fragments


# ShopAnswers: the parser on hand-made data and requests

MINI: dict[str, Any] = {
    "snapshot": 9,
    "fragments": {
        "a": ["Doc > A", "Alpha starts"],
        "b": ["Doc > B", "Beta starts"],
        "b2": ["Doc > B", "Beta starts", 2],
        "c": ["Doc > C", "Gamma"],
    },
    "nodes": [
        {
            "window": ["b", "b2"],
            "entities": [["Beta", "Module", ["b2"], None, ["Beta service"]], ["Payments", "Feature", ["b", "b2"]]],
        },
        {"window": ["a"], "entities": [["Alpha", "Module"]]},
        {"window": ["c"], "error": "malformed"},
    ],
    "edges": [
        {"unit": ["a", "b", "b2", "c"], "facts": [["Alpha", "INTEGRATES_WITH", "Beta", "Alpha calls Beta.", ["a"]]]}
    ],
    "attributes": {"Alpha": {"quote": "Alpha starts", "stmt": "Alpha runs."}},
    "consolidation": [
        {"block": ["Beta", "Beta service"], "groups": [{"names": ["Beta", "Beta service"], "label": "Module"}]}
    ],
    "dedup": [{"pair": ["Staff export orders", "Staff can export orders"], "same": True, "confidence": "medium"}],
}
FRAGMENTS = [
    fragment_text("Doc", ["A"], "Alpha starts here."),
    fragment_text("Doc", ["B"], "Beta starts."),
    fragment_text("Doc", ["B"], "Beta starts."),
    fragment_text("Doc", ["C"], "Gamma."),
]


def node_request(window: tuple[int, int], node_list: NodeList = HUBS_LIST) -> ScriptedRequest:
    ontology = shipped_ontology()
    messages = node_messages(ontology, FRAGMENTS, [], window, node_list=node_list)
    return ScriptedRequest(
        "extraction", "m", NODE_PROMPTS[node_list], tuple(messages), node_answer_model(ontology, [node_list])
    )


def edge_request(names: Sequence[str]) -> ScriptedRequest:
    ontology = shipped_ontology()
    messages = edge_window_messages(ontology, FRAGMENTS, [(name, "Module") for name in names], (0, len(FRAGMENTS)))
    return ScriptedRequest("extraction", "m", EDGE_WINDOW_PROMPT, tuple(messages), edge_answer_model(ontology))


def test_shop_answers_refuse_repeated_and_undeclared_handles() -> None:
    repeated = {**MINI, "nodes": [{"window": ["a", "a"], "entities": []}]}
    with pytest.raises(ScriptError, match="repeats a fragment"):
        ShopAnswers(repeated)
    undeclared = {**MINI, "nodes": [{"window": ["a"], "entities": [["Alpha", "Module", ["zzz"]]]}]}
    with pytest.raises(ScriptError, match="undeclared fragment handles"):
        ShopAnswers(undeclared)


def test_shop_answers_key_requests_by_handles_and_the_library_window() -> None:
    answers = ShopAnswers(MINI)
    request = node_request((1, 3))
    assert [episode.handle for episode in answers.episodes(request)] == ["a", "b", "b2", "c"]  # occurrence 2 -> b2
    assert answers.node_key(request) == ("b", "b2")
    assert answers.node_key(node_request((0, 1))) == ("a",)
    assert answers.edge_key(edge_request(["Alpha", "Beta"])) == ("a", "b", "b2", "c")
    # a window beyond the request's fragments is a script error, never a silent empty key
    request = node_request((3, 4))
    system, unit, user = request.messages
    beyond = user["content"].replace("in fragment 4.", "in fragments 4 to 5.")
    assert beyond != user["content"]
    request = ScriptedRequest("extraction", "m", request.prompt, (system, unit, {**user, "content": beyond}), BaseModel)
    with pytest.raises(ScriptError, match="beyond"):
        answers.window(request, answers.episodes(request))


def test_shop_answers_build_node_answers_with_fragment_numbers_and_quotes() -> None:
    answers = ShopAnswers(MINI)
    assert answers._nodes(node_request((1, 3))) == {
        "hubs": [
            {"name": "Beta", "label": "Module", "quote": "", "fragments": [3], "aliases": ["Beta service"]},
            {"name": "Payments", "label": "Feature", "quote": "", "fragments": [2, 3], "aliases": []},
        ],
    }
    assert answers._nodes(node_request((1, 3), STATEMENTS_LIST)) == {"statements": []}
    # one-fragment window: the entity's fragment defaults to it, its quote to its attributes quote
    assert answers._nodes(node_request((0, 1))) == {
        "hubs": [{"name": "Alpha", "label": "Module", "quote": "Alpha starts", "fragments": [1], "aliases": []}],
    }
    error = answers._nodes(node_request((3, 4)))
    assert isinstance(error, LLMError) and error.reason == "malformed"
    assert answers._nodes(node_request((0, 2))) is NO_ANSWER

    quoted = ShopAnswers(
        {
            **MINI,
            "nodes": [
                {"window": ["a"], "entities": [["Alpha", "Module", ["a"], "Alpha here"], ["Alpha runs", "Constraint"]]}
            ],
        }
    )
    # each pass takes the entities of its list: the hub pass the hubs, the statement pass the rest
    answer = quoted._nodes(node_request((0, 1)))
    assert answer == {
        "hubs": [{"name": "Alpha", "label": "Module", "quote": "Alpha here", "fragments": [1], "aliases": []}]
    }
    answer = quoted._nodes(node_request((0, 1), STATEMENTS_LIST))
    assert answer == {"statements": [{"name": "Alpha runs", "label": "Constraint", "quote": "", "fragments": [1]}]}

    broken = ShopAnswers({**MINI, "nodes": [{"window": ["b", "b2"], "entities": [["Beta", "Module"]]}]})
    with pytest.raises(ScriptError, match="names no fragment"):
        broken._nodes(node_request((1, 3)))


def test_shop_answers_refuse_a_label_in_neither_node_list_while_each_pass_skips_the_others() -> None:
    mistyped = ShopAnswers(
        {**MINI, "nodes": [{"window": ["a"], "entities": [["Alpha", "Module"], ["Alpha runs", "Constraint"]]}]}
    )
    # each pass answers its own list and skips the other pass's entity
    assert [item["name"] for item in mistyped._nodes(node_request((0, 1)))["hubs"]] == ["Alpha"]
    assert [item["name"] for item in mistyped._nodes(node_request((0, 1), STATEMENTS_LIST))["statements"]] == [
        "Alpha runs"
    ]
    typo = ShopAnswers({**MINI, "nodes": [{"window": ["a"], "entities": [["Alpha", "Module"], ["Alpha", "Moduel"]]}]})
    for node_list in (HUBS_LIST, STATEMENTS_LIST):
        with pytest.raises(ScriptError, match="label 'Moduel' is in neither node list"):
            typo._nodes(node_request((0, 1), node_list))
    # the labels come from the build's ontology: one without the label refuses it too
    with pytest.raises(ScriptError, match="label 'Constraint' is in neither node list"):
        mistyped._nodes(node_request((0, 1)), labels=frozenset({"Module"}))
    # a renamed ontology: the label as written or renamed may be in the lists
    renamed = mistyped._nodes(
        node_request((0, 1)), rename={"Constraint": "Limit"}, labels=frozenset({"Module", "Limit"})
    )
    assert [item["name"] for item in renamed["hubs"]] == ["Alpha"]


def test_shop_answers_build_edge_answers_against_the_requests_entities() -> None:
    answers = ShopAnswers(MINI)
    (edge,) = answers._edges(edge_request(["Alpha", "Beta"]))["edges"]
    assert edge == {
        "source": "Alpha",
        "relation": "INTEGRATES_WITH",
        "target": "Beta",
        "fact": "Alpha calls Beta.",
        "fragments": [1],
    }
    with pytest.raises(ScriptError, match="not in the request"):
        answers._edges(edge_request(["Alpha"]))


def test_shop_answers_parse_attribute_consolidation_and_dedup_requests() -> None:
    answers = ShopAnswers(MINI)
    gateway = ScriptedGateway()
    messages = attribute_messages(
        shipped_ontology(),
        gateway,
        nodes=[("node_1", "Module", "Alpha", [1])],
        fragments=["Doc > A\n\nAlpha starts."],
        other_headings={"node_1": ['Doc > B "quoted" (part)']},  # listed after the fragments, still parsed
    )
    output = extract._batch_model(shipped_ontology(), ("Module",))
    request = ScriptedRequest("small", "m", ATTRIBUTES_PROMPT, tuple(messages), output)
    assert answers.attribute_nodes(request) == [("node_1", "Module", "Alpha")]
    # the quote is not a field of the attributes model (the node call returns it): left out
    assert answers._attributes(request) == {"node_1": {"stmt": "Alpha runs."}}
    bad = ScriptedRequest(
        "small",
        "m",
        ATTRIBUTES_PROMPT,
        ({"role": "user", "content": gateway.untrusted("nodes", "garbage")},),
        BaseModel,
    )
    with pytest.raises(ScriptError, match="unreadable"):
        answers.attribute_nodes(bad)

    hubs = "\n".join(
        json.dumps({"id": key, "label": "Module", "name": name})
        for key, name in (("h1", "Beta service"), ("h2", "Beta"))
    )
    consolidation = ScriptedRequest(
        "judge", "m", HUBS_PROMPT, ({"role": "user", "content": gateway.untrusted("hubs", hubs)},), BaseModel
    )
    assert answers.consolidation_key(consolidation) == ("Beta", "Beta service")
    # the judge's confidence defaults to high
    assert answers._consolidation(consolidation) == {
        "groups": [{"ids": ["h2", "h1"], "label": "Module", "confidence": "high"}]
    }

    statements = json.dumps({"statements": [{"name": "Staff can export orders"}, {"name": "Staff export orders"}]})
    dedup = ScriptedRequest(
        "small",
        "m",
        DEDUP_PROMPT,
        ({"role": "user", "content": gateway.untrusted("statements", statements)},),
        BaseModel,
    )
    assert answers.dedup_key(dedup) == ("Staff can export orders", "Staff export orders")
    assert answers._dedup(dedup) == {"same": True, "confidence": "medium"}


async def test_installed_shop_answers_leave_unknown_keys_unscripted() -> None:
    gateway = ShopAnswers(MINI).install(ScriptedGateway())
    request = node_request((0, 2))
    with pytest.raises(UnscriptedRequestError):
        await gateway.structured("extraction", None, request.messages, BaseModel, prompt=request.prompt)
    assert len(gateway.unscripted) == 1
