"""The agent routes ``similar``, ``get_evidence``, ``path`` and ``expand_brief`` on the API: the real app, verifiers,
readers and libraries over the shop fixture (``spec_reads_support``, with stored vectors and statements). Each route
answers its contract model, checks the arguments that span arguments before any library call, maps library answers
to tool codes, and reads nothing for a foreign project.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_contracts import AGENT_ROUTES, BriefExpansion, Evidence, NodePaths, SimilarNodes, StoryBriefResult
from graph.core import ConfigurationError, InfrastructureUnavailableError
from graph.core.expand import estimate_tokens
from graph_trace_support import shop_graph
from plan_trace_support import TASKS, task
from spec_reads_support import CHECKOUT, STRIPE_STORY, serve_spec, with_plan
from specflow_api_agent_support import ALPHA, FOREIGN, UNKNOWN_PROJECT, agent_env, log_haystack, web_alice

SIMILAR, EVIDENCE, PATH, EXPAND, STORY = (
    AGENT_ROUTES[name] for name in ("similar", "get_evidence", "path", "expand_brief", "get_story_brief")
)


@pytest.fixture
async def spec(monkeypatch, valid_env):
    async with serve_spec(monkeypatch, agent_env(valid_env)) as rig:
        yield rig


def code_of(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def read_names(spec: Any) -> list[str]:
    return [name for name, _ in spec.executor.calls]


# --- similar --------------------------------------------------------------------------------------------------


async def test_similar_to_a_story_compares_its_implemented_nodes_and_shows_plan_state(spec):
    response = await spec.get(SIMILAR, project_id=ALPHA, story_id=CHECKOUT)

    assert response.status_code == 200
    body = SimilarNodes.model_validate(response.json())
    assert body.project_id == ALPHA and body.unknown == []
    assert body.sources == ["FEAT::CHECKOUT", "REQ::FR::CHECKOUT", "REQ::Q::CHECKOUT_SPEED"]
    # FEAT::CARD_PAYMENT reads alike but has an edge to FEAT::CHECKOUT; MOD::WEB_SHOP scores 0.5
    assert [(item.spec_id, item.like, item.plan_state) for item in body.items] == [
        ("FEAT::CHECKOUT_REVIEW", "REQ::FR::CHECKOUT", "unplanned"),
        ("REQ::FR::CARD_PAYMENT", "REQ::FR::CHECKOUT", "open"),
    ]
    assert all(0.75 <= item.score <= 1 for item in body.items)
    assert "embed" not in read_names(spec)


async def test_similar_to_spec_ids_keeps_linked_nodes_on_request_and_lists_unknown_ids(spec):
    response = await spec.get(
        SIMILAR,
        project_id=ALPHA,
        spec_ids=["FEAT::CHECKOUT", "FEAT::NOPE"],
        exclude_linked="false",
        min_score=0.85,
        limit=3,
    )

    body = SimilarNodes.model_validate(response.json())
    assert (body.sources, body.unknown) == (["FEAT::CHECKOUT"], ["FEAT::NOPE"])
    assert [item.spec_id for item in body.items] == ["REQ::FR::CHECKOUT", "FEAT::CHECKOUT_REVIEW", "FEAT::CARD_PAYMENT"]


async def test_similar_without_a_plan_has_null_plan_states_and_a_story_needs_one(monkeypatch, valid_env):
    async with serve_spec(monkeypatch, agent_env(valid_env), has_plan=False) as spec:
        by_ids = await spec.get(SIMILAR, project_id=ALPHA, spec_ids=["FEAT::CHECKOUT"])
        by_story = await spec.get(SIMILAR, project_id=ALPHA, story_id=CHECKOUT)

    assert {item["plan_state"] for item in by_ids.json()["items"]} == {None}
    assert code_of(by_story) == (404, "no_plan")


async def test_similar_with_nothing_to_compare_answers_no_items_without_a_vector_read(spec, monkeypatch):
    async def empty_brief(*_: Any, **__: Any) -> Any:
        return type("Brief", (), {"implements": ()})()

    monkeypatch.setattr(spec.rig.runtime.agent_reads.plan, "story_brief", empty_brief)

    response = await spec.get(SIMILAR, project_id=ALPHA, story_id=CHECKOUT)

    assert response.json() == {"project_id": ALPHA, "sources": [], "unknown": [], "items": []}
    assert spec.executor.calls == []


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [
        ({}, 400, "invalid_arguments"),
        ({"story_id": CHECKOUT, "spec_ids": ["FEAT::CHECKOUT"]}, 400, "invalid_arguments"),
        ({"story_id": "STORY::NOPE"}, 404, "story_not_found"),
        ({"spec_ids": ["X"] * 21}, 422, "validation_failed"),
        ({"spec_ids": ["FEAT::CHECKOUT"], "limit": 21}, 422, "validation_failed"),
        ({"spec_ids": ["FEAT::CHECKOUT"], "min_score": 1.1}, 422, "validation_failed"),
    ],
)
async def test_similar_refuses_bad_arguments_and_an_unknown_story(spec, params, status, code):
    response = await spec.get(SIMILAR, project_id=ALPHA, **params)

    assert code_of(response) == (status, code)
    if code == "invalid_arguments":
        assert spec.executor.calls == []


# --- get_evidence ---------------------------------------------------------------------------------------------


async def test_evidence_quotes_the_lines_with_context_and_the_other_nodes_on_them(spec):
    response = await spec.get(EVIDENCE, project_id=ALPHA, spec_id="CON::EU_REGION", context_lines=1)

    assert response.status_code == 200
    body = Evidence.model_validate(response.json())
    assert (body.project_id, body.spec_id) == (ALPHA, "CON::EU_REGION")
    [item] = body.items
    assert (item.document_id, item.path, item.fragment_id, item.line_start, item.line_end) == (
        "shop",
        "docs/shop.md",
        "shop.quality",
        7,
        7,
    )
    assert item.quote == "Checkout responds quickly and all services run in the EU region."
    assert item.before == [
        "Requirements name behaviour, data, integration, business rules and cross-area dependencies."
    ]
    assert item.after == ["Stripe limits retries. Gift cards are out of scope for this release."]
    assert [(other.spec_id, other.label) for other in item.others] == [("REQ::Q::CHECKOUT_SPEED", "QualityRequirement")]


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [
        ({"spec_id": "FEAT::NOPE"}, 404, "node_not_found"),
        ({"spec_id": "CON::EU_REGION", "context_lines": 6}, 422, "validation_failed"),
        ({"spec_id": "CON::EU_REGION", "context_lines": -1}, 422, "validation_failed"),
    ],
)
async def test_evidence_refuses_an_unknown_node_and_out_of_range_context(spec, params, status, code):
    assert code_of(await spec.get(EVIDENCE, project_id=ALPHA, **params)) == (status, code)


# --- path -----------------------------------------------------------------------------------------------------


async def test_path_answers_the_chains_with_the_ends_as_given(spec):
    response = await spec.get(PATH, project_id=ALPHA, **{"from": "MOD::PAYMENTS_SERVICE", "to": "MOD::STRIPE"})

    assert response.status_code == 200
    raw = response.json()
    assert (raw["from"], raw["to"], raw["direction"]) == ("MOD::PAYMENTS_SERVICE", "MOD::STRIPE", "based_on")
    body = NodePaths.model_validate(raw)
    assert [(chain.hops, [step.spec_id for step in chain.steps]) for chain in body.items] == [
        (2, ["IFC::STRIPE_API", "MOD::STRIPE"])
    ]


async def test_path_is_affects_for_the_reverse_and_null_with_a_reason_and_a_link_without_a_chain(spec):
    reverse = await spec.get(PATH, project_id=ALPHA, **{"from": "FEAT::CARD_PAYMENT", "to": "FEAT::CHECKOUT"})
    shared_provider = await spec.get(
        PATH, project_id=ALPHA, **{"from": "IFC::STRIPE_API", "to": "IFC::INTEGRATED", "max_hops": 6}
    )

    assert (reverse.json()["direction"], reverse.json()["reason"], reverse.json()["link"]) == ("affects", None, None)
    body = shared_provider.json()
    assert (body["direction"], body["items"], body["reason"]) == (None, [], "no_directed_chain")
    assert [(step["spec_id"], step["relation"], step["step"]) for step in body["link"]["steps"]] == [
        ("MOD::STRIPE", "PROVIDED_BY", "forward"),
        ("IFC::INTEGRATED", "PROVIDED_BY", "backward"),
    ]


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [
        ({"from": "FEAT::NOPE", "to": "FEAT::CHECKOUT"}, 404, "node_not_found"),
        ({"from": "FEAT::CHECKOUT", "to": "FEAT::NOPE"}, 404, "node_not_found"),
        ({"from": "FEAT::CHECKOUT"}, 422, "validation_failed"),
        ({"from": "FEAT::CHECKOUT", "to": "FEAT::CARD_PAYMENT", "max_hops": 7}, 422, "validation_failed"),
    ],
)
async def test_path_refuses_an_unknown_end_and_bad_arguments(spec, params, status, code):
    assert code_of(await spec.get(PATH, project_id=ALPHA, **params)) == (status, code)


# --- expand_brief ---------------------------------------------------------------------------------------------


async def test_expand_brief_ranks_beyond_the_brief_within_the_budget(spec):
    response = await spec.get(EXPAND, project_id=ALPHA, story_id=CHECKOUT)

    assert response.status_code == 200
    body = BriefExpansion.model_validate(response.json())
    assert (body.project_id, body.story_id, body.left_out) == (ALPHA, CHECKOUT, 0)
    ids = [item.spec_id for item in body.items]
    # the story's own nodes and the brief's neighbours (FEAT::CARD_PAYMENT among them) are left out
    assert not {"FEAT::CHECKOUT", "REQ::FR::CHECKOUT", "REQ::Q::CHECKOUT_SPEED", "FEAT::CARD_PAYMENT"} & set(ids)
    assert [(item.spec_id, item.via) for item in body.items] == [
        ("REQ::FR::CARD_PAYMENT", "both"),
        ("MOD::PAYMENTS_SERVICE", "graph"),
        ("FEAT::CHECKOUT_REVIEW", "similar"),
    ]
    payments = body.items[1]
    assert (payments.text, payments.tokens) == (
        "Takes card payments through Stripe.",
        estimate_tokens("Payments service", "Takes card payments through Stripe."),
    )
    assert body.tokens == sum(item.tokens for item in body.items)
    assert "embed" not in read_names(spec)


async def test_expand_brief_at_a_small_budget_answers_the_same_twice(spec):
    first = await spec.get(EXPAND, project_id=ALPHA, story_id=CHECKOUT, budget_tokens=500)
    second = await spec.get(EXPAND, project_id=ALPHA, story_id=CHECKOUT, budget_tokens=500)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert BriefExpansion.model_validate(first.json()).items


async def test_expand_brief_without_implemented_nodes_is_empty_without_a_graph_read(spec, monkeypatch):
    async def empty_brief(*_: Any, **__: Any) -> Any:
        return type("Brief", (), {"implements": (), "neighbours": ()})()

    monkeypatch.setattr(spec.rig.runtime.agent_reads.plan, "story_brief", empty_brief)

    response = await spec.get(EXPAND, project_id=ALPHA, story_id=STRIPE_STORY)

    assert response.json() == {"project_id": ALPHA, "story_id": STRIPE_STORY, "items": [], "tokens": 0, "left_out": 0}
    assert spec.executor.calls == []


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [
        ({"story_id": "STORY::NOPE"}, 404, "story_not_found"),
        ({"story_id": CHECKOUT, "budget_tokens": 499}, 422, "validation_failed"),
        ({"story_id": CHECKOUT, "budget_tokens": 8001}, 422, "validation_failed"),
    ],
)
async def test_expand_brief_refuses_an_unknown_story_and_a_budget_out_of_range(spec, params, status, code):
    assert code_of(await spec.get(EXPAND, project_id=ALPHA, **params)) == (status, code)


async def test_expand_brief_without_a_story_points_to_search_spec_before_any_read(spec):
    """QA F35: an agent without a story gets a typed error naming the argument and the tool to use instead."""

    response = await spec.get(EXPAND, project_id=ALPHA, query="Which revision is binding?")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_arguments"
    assert response.json()["error"]["details"] == {"argument": "story_id", "next": "search_spec"}
    assert spec.executor.calls == []


async def test_expand_brief_takes_a_query_and_answers_the_story_alone_when_it_cannot_be_embedded(spec):
    # the rig's gateway fails every embed call: the expansion falls back to the story's own vectors
    aimed = await spec.get(EXPAND, project_id=ALPHA, story_id=CHECKOUT, query="How are card payments taken?")
    plain = await spec.get(EXPAND, project_id=ALPHA, story_id=CHECKOUT)

    assert aimed.status_code == plain.status_code == 200
    assert aimed.json() == plain.json()
    assert code_of(await spec.get(EXPAND, project_id=ALPHA, story_id=CHECKOUT, query="q" * 501)) == (
        422,
        "validation_failed",
    )


async def test_the_story_brief_hints_what_expand_brief_would_add(spec):
    """QA F24: the brief result says how much more an expansion holds, its first three and the tool to call."""

    brief = StoryBriefResult.model_validate((await spec.get(STORY, project_id=ALPHA, story_id=CHECKOUT)).json()).brief
    expansion = BriefExpansion.model_validate((await spec.get(EXPAND, project_id=ALPHA, story_id=CHECKOUT)).json())

    assert brief.expansion is not None and brief.expansion.next == "expand_brief"
    assert brief.expansion.more == len(expansion.items) > 0
    assert [(top.spec_id, top.title) for top in brief.expansion.top] == [
        (item.spec_id, item.name) for item in expansion.items[:3]
    ]


async def test_the_brief_of_a_story_without_implemented_nodes_hints_nothing_more(spec, monkeypatch):
    plan = spec.rig.runtime.agent_reads.plan
    real = plan.story_brief

    async def bare(*args: Any, **kwargs: Any) -> Any:
        return (await real(*args, **kwargs)).model_copy(update={"implements": ()})

    monkeypatch.setattr(plan, "story_brief", bare)
    spec.executor.calls.clear()

    response = await spec.get(STORY, project_id=ALPHA, story_id=CHECKOUT)

    assert response.json()["brief"]["expansion"] == {"more": 0, "top": [], "next": "expand_brief"}
    assert "read_trace_hop" not in read_names(spec)


@pytest.mark.parametrize("error", [InfrastructureUnavailableError("neo4j_unreachable"), ConfigurationError("x")])
async def test_the_brief_answers_with_a_null_hint_when_the_expansion_read_fails(spec, monkeypatch, caplog, error):
    """QA F36: the hint is an extra; its failure is logged by type and the brief still answers."""

    async def failing(*_: Any, **__: Any) -> Any:
        raise error

    monkeypatch.setattr(spec.rig.runtime.agent_reads.reader, "expand", failing)

    response = await spec.get(STORY, project_id=ALPHA, story_id=CHECKOUT)

    assert response.status_code == 200 and response.json()["brief"]["expansion"] is None
    assert response.json()["brief"]["implements"]
    failed = [record for record in caplog.records if getattr(record, "event", None) == "api.agent.expansion_failed"]
    assert [record.error_type for record in failed] == [type(error).__name__]


async def test_expand_brief_without_a_plan_is_no_plan(monkeypatch, valid_env):
    async with serve_spec(monkeypatch, agent_env(valid_env), has_plan=False) as spec:
        response = await spec.get(EXPAND, project_id=ALPHA, story_id=CHECKOUT)

    assert code_of(response) == (404, "no_plan")


# --- the source and seed caps of a story --------------------------------------------------------------------


def many_implemented(spec: Any, count: int) -> None:
    """The Checkout story gains a task implementing ``count`` more features."""

    graph = shop_graph()
    ids = [f"FEAT::MANY{index:03d}" for index in range(count)]
    for spec_id in ids:
        graph.node(spec_id, "Feature", name=spec_id)
    with_plan(spec, (*TASKS, task("TASK::WEB_SHOP::MANY", CHECKOUT, tuple(ids))), graph)


async def test_similar_to_a_story_of_many_nodes_compares_the_first_50(spec):
    many_implemented(spec, 51)

    response = await spec.get(SIMILAR, project_id=ALPHA, story_id=CHECKOUT)

    assert response.status_code == 200, response.text
    assert len(response.json()["sources"]) == 50


async def test_expand_brief_of_a_story_of_many_nodes_seeds_the_first_500(spec):
    many_implemented(spec, 501)

    response = await spec.get(EXPAND, project_id=ALPHA, story_id=CHECKOUT)

    assert response.status_code == 200, response.text
    seeds = [call["spec_ids"] for name, call in spec.executor.calls if name == "read_node_uuids_by_spec_id"]
    assert len(seeds[0]) == 500


# --- every route: membership, infrastructure errors, logs ----------------------------------------------------

CALLS = {
    "similar": (SIMILAR, {"story_id": CHECKOUT}),
    "evidence": (EVIDENCE, {"spec_id": "CON::EU_REGION"}),
    "path": (PATH, {"from": "MOD::PAYMENTS_SERVICE", "to": "MOD::STRIPE"}),
    "expand": (EXPAND, {"story_id": CHECKOUT}),
}


@pytest.mark.parametrize("name", CALLS)
@pytest.mark.parametrize("project", [FOREIGN, UNKNOWN_PROJECT, "not-a-uuid"])
async def test_a_route_answers_a_project_the_caller_is_no_member_of_as_not_found_and_reads_nothing(spec, name, project):
    path, params = CALLS[name]

    response = await spec.get(path, project_id=project, **params)

    assert code_of(response) == (404, "project_not_found")
    assert spec.executor.calls == [] and spec.memory.calls == []


@pytest.mark.parametrize("name", CALLS)
async def test_a_route_without_a_token_or_with_a_web_token_is_refused(spec, name):
    path, params = CALLS[name]

    assert (await spec.rig.get(path, None, project_id=ALPHA, **params)).status_code == 401
    assert (await spec.rig.get(path, web_alice(), project_id=ALPHA, **params)).status_code == 401


FAILURES = {
    "graph_down": (InfrastructureUnavailableError("neo4j_unreachable"), 503, "graph_unavailable"),
    "bug": (ConfigurationError("unknown_label", label="X"), 500, "internal_error"),
}


@pytest.mark.parametrize("name", CALLS)
@pytest.mark.parametrize("failure", FAILURES)
async def test_a_failing_graph_is_graph_unavailable_and_a_bug_is_internal_error(monkeypatch, valid_env, name, failure):
    path, params = CALLS[name]
    error, status, code = FAILURES[failure]
    async with serve_spec(monkeypatch, agent_env(valid_env), fail=error) as spec:
        response = await spec.get(path, project_id=ALPHA, **params)

    assert code_of(response) == (status, code)


async def test_the_reads_log_no_text_of_the_documents_or_the_spec(spec, caplog):
    caplog.set_level("DEBUG")
    for path, params in CALLS.values():
        assert (await spec.get(path, project_id=ALPHA, **params)).status_code == 200

    haystack = log_haystack(caplog)
    for text in ("EU region", "Requirements name behaviour", "Checkout review", "Takes card payments", "Stripe API"):
        assert text not in haystack, text
