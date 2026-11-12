"""The agent routes of the spec graph (``get_story_brief``, ``trace``, ``get_node``, ``search_spec``) on the API: the
real app, verifiers, readers and libraries over the shop fixture (``spec_reads_support``). Each route answers its
contract model, names holders from the project's members (never an id), maps library codes to tool codes, checks
the arguments that span arguments before any library call, and reads nothing for a foreign project.
"""

from __future__ import annotations

import dataclasses
import itertools
from typing import Any, ClassVar

import pytest

from agent_contracts import (
    AGENT_ROUTES,
    NodeDetail,
    SpecSearch,
    SpecTrace,
    StoryBriefResult,
)
from api.agent_reads import agent_reads
from graph.core import ConfigurationError, GraphReader, InfrastructureUnavailableError
from graph.plan import PlanService
from graph_trace_support import shop_graph
from plan_trace_support import TASKS, task
from spec_reads_support import CHECKOUT, STRIPE_STORY, SpecRig, serve_spec, with_plan
from specflow_api_agent_support import (
    ALPHA,
    FOREIGN,
    UNKNOWN_PROJECT,
    agent_env,
    log_haystack,
    serve_agent,
    web_alice,
)
from test_graph_trace import AFFECTS_DEPTH_2, BASED_ON_DEPTH_3, OWNER, SEEDS

STORY, TRACE, NODE, SEARCH = (AGENT_ROUTES[name] for name in ("get_story_brief", "trace", "get_node", "search_spec"))


@pytest.fixture
async def spec(monkeypatch, valid_env):
    async with serve_spec(monkeypatch, agent_env(valid_env)) as rig:
        yield rig


def ids(items: list[dict[str, Any]], key: str = "spec_id") -> list[str]:
    return [item[key] for item in items]


def assert_no_private_values(spec: SpecRig, text: str) -> None:
    """No user id, email or claim token in an answer."""

    for private in (str(spec.alice), str(spec.ben), "alice@example.com", "ben@x.test", "claim_token", "u9"):
        assert private not in text, private


# --- get_story_brief ----------------------------------------------------------------------------------------


async def test_the_story_brief_answers_the_story_its_tasks_in_build_order_and_findings_with_named_authors(spec):
    response = await spec.get(STORY, project_id=ALPHA, story_id=CHECKOUT)

    assert response.status_code == 200
    body = StoryBriefResult.model_validate(response.json())
    brief = body.brief
    assert body.project_id == ALPHA
    assert (brief.story.story_id, brief.story.kind, brief.story.anchor, brief.story.state) == (
        CHECKOUT,
        "feature",
        "FEAT::CHECKOUT",
        "ready",
    )
    assert [(t.task_id, t.status, t.waits_for) for t in brief.tasks] == [
        ("TASK::WEB_SHOP::DATA_CHECKOUT", "completed", []),
        ("TASK::WEB_SHOP::LOGIC_CHECKOUT", "pending", ["TASK::PAYMENTS::CONTRACT", "TASK::WEB_SHOP::DATA_CHECKOUT"]),
    ]
    assert [n.spec_id for n in brief.implements] == ["FEAT::CHECKOUT", "REQ::FR::CHECKOUT", "REQ::Q::CHECKOUT_SPEED"]
    assert [c.task_id for c in brief.contracts] == ["TASK::PAYMENTS::CONTRACT"]
    assert brief.spec_changed is False and brief.missing == []
    # the blocking finding first; its author is a named member, the other one is the caller
    assert [(f.kind, f.blocking, f.author.name, f.author.you) for f in brief.findings] == [
        ("conflict", True, "Ben", False),
        ("question", False, "Alice", True),
    ]
    assert_no_private_values(spec, response.text)


class CallRecorder:
    """An LLM gateway that records every call (FR-MCP-READ-01: a brief makes none)."""

    def __init__(self) -> None:
        self.config = type("Config", (), {"embedding_model": "test/embedding"})()
        self.calls: list[str] = []

    async def embed(self, texts: Any) -> list[list[float]]:
        self.calls.append("embed")
        return [[1.0] for _ in texts]

    async def complete(self, *_: Any, **__: Any) -> Any:
        self.calls.append("complete")


async def test_fr_mcp_read_01_three_tasks_sharing_a_node_merge_into_one_brief_without_a_model_call(spec):
    shared = "FEAT::CHECKOUT"
    with_plan(
        spec,
        (
            task(
                "TASK::WEB_SHOP::DATA_CHECKOUT", CHECKOUT, (shared,), done_when='[{"text": "Saved", "source": "spec"}]'
            ),
            task("TASK::WEB_SHOP::LOGIC_CHECKOUT", CHECKOUT, (shared, "REQ::FR::CHECKOUT")),
            task(
                "TASK::WEB_SHOP::UI_CHECKOUT", CHECKOUT, (shared,), done_when='[{"text": "Shown", "source": "planner"}]'
            ),
        ),
    )
    gateway = CallRecorder()
    reads = spec.rig.runtime.agent_reads
    spec.rig.runtime.agent_reads = dataclasses.replace(
        reads, reader=GraphReader(spec.executor, reads.ontology, gateway)
    )

    response = await spec.get(STORY, project_id=ALPHA, story_id=CHECKOUT)

    brief = StoryBriefResult.model_validate(response.json()).brief
    order = [t.task_id for t in brief.tasks]
    assert len(order) == 3 and order.index("TASK::WEB_SHOP::DATA_CHECKOUT") < order.index(
        "TASK::WEB_SHOP::LOGIC_CHECKOUT"
    )
    sources = {t.task_id: [d.source for d in t.done_when] for t in brief.tasks}
    assert sources["TASK::WEB_SHOP::DATA_CHECKOUT"] == ["spec"] and sources["TASK::WEB_SHOP::UI_CHECKOUT"] == [
        "planner"
    ]
    assert [n.spec_id for n in brief.implements].count(shared) == 1
    assert gateway.calls == []


async def test_the_story_brief_limits_the_neighbours_and_shows_a_task_progress(spec):
    plain = (await spec.get(STORY, project_id=ALPHA, story_id=CHECKOUT)).json()["brief"]
    none = (await spec.get(STORY, project_id=ALPHA, story_id=CHECKOUT, neighbours=0)).json()["brief"]

    assert plain["neighbours"] != [] and none["neighbours"] == []
    assert all("progress" in task for task in plain["tasks"])


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [
        ({"story_id": "STORY::NOPE"}, 404, "story_not_found"),
        ({"story_id": STRIPE_STORY, "neighbours": 101}, 422, "validation_failed"),
        ({"story_id": STRIPE_STORY, "neighbours": -1}, 422, "validation_failed"),
    ],
)
async def test_the_story_brief_refuses_an_unknown_story_and_out_of_range_neighbours(spec, params, status, code):
    response = await spec.get(STORY, project_id=ALPHA, **params)

    assert response.status_code == status and response.json()["error"]["code"] == code


async def test_a_story_brief_without_a_plan_is_no_plan(monkeypatch, valid_env):
    async with serve_spec(monkeypatch, agent_env(valid_env), has_plan=False) as spec:
        response = await spec.get(STORY, project_id=ALPHA, story_id=CHECKOUT)

    assert response.status_code == 404 and response.json()["error"]["code"] == "no_plan"


# --- trace --------------------------------------------------------------------------------------------------


async def test_a_trace_from_a_story_lists_what_it_is_based_on_with_plan_state_and_finding_counts(spec):
    response = await spec.get(TRACE, project_id=ALPHA, story_id=CHECKOUT, direction="based_on")

    assert response.status_code == 200
    trace = SpecTrace.model_validate(response.json())
    assert trace.direction == "based_on" and trace.sources[0] == "FEAT::CHECKOUT"
    nodes = {n.spec_id: n for n in trace.nodes}
    assert nodes["FEAT::CARD_PAYMENT"].hops == 1 and nodes["FEAT::CARD_PAYMENT"].plan_state == "open"
    assert nodes["FEAT::CARD_PAYMENT"].findings == 1
    assert nodes["MOD::PAYMENTS_SERVICE"].plan_state == "completed"
    assert [c.spec_id for c in trace.context] == ["MOD::WEB_SHOP"]
    assert trace.summary is not None and sum(trace.summary.values()) == len(trace.nodes)
    assert CHECKOUT not in [s.story_id for s in trace.stories]  # the traced story is left out of its own list
    assert_no_private_values(spec, response.text)


@pytest.mark.parametrize(
    ("direction", "depth", "expected"), [("based_on", 3, BASED_ON_DEPTH_3), ("affects", 2, AFFECTS_DEPTH_2)]
)
async def test_a_trace_of_spec_ids_answers_the_worked_example_of_trace_md(spec, direction, depth, expected):
    response = await spec.get(TRACE, project_id=ALPHA, spec_ids=SEEDS, direction=direction, depth=depth)

    assert response.status_code == 200
    trace = SpecTrace.model_validate(response.json())
    assert [(n.hops, n.spec_id, n.parent, n.relation, n.step, n.via_owner) for n in trace.nodes] == expected
    assert [item.spec_id for item in trace.context] == [OWNER]


async def test_a_trace_names_the_holders_of_tasks_and_stories_and_lists_unknown_ids(spec):
    response = await spec.get(
        TRACE, project_id=ALPHA, spec_ids=["FEAT::CARD_PAYMENT", "NOPE::X"], direction="affects", depth=1
    )

    trace = response.json()
    assert trace["unknown"] == ["NOPE::X"]
    stripe = next(n for n in trace["nodes"] if n["spec_id"] == "MOD::STRIPE")
    assert [(t["task_id"], t["holder"]) for t in stripe["tasks"]] == [
        ("TASK::STRIPE::SETUP", {"name": "Alice", "you": True, "agent_label": None}),
        ("TASK::STRIPE::WIRE", {"name": "Ben", "you": False, "agent_label": None}),
    ]
    story = next(s for s in trace["stories"] if s["story_id"] == STRIPE_STORY)
    assert [h["name"] for h in story["holders"]] == ["Alice", "Ben"]
    assert_no_private_values(spec, response.text)


@pytest.mark.parametrize(
    "params",
    [
        {"direction": "affects"},  # neither a story nor spec ids
        {"direction": "affects", "story_id": CHECKOUT, "spec_ids": ["FEAT::CHECKOUT"]},  # both
    ],
)
async def test_a_trace_needs_exactly_one_of_story_and_spec_ids_and_asks_the_library_nothing(spec, monkeypatch, params):
    async def boom(*_: Any, **__: Any) -> None:
        raise AssertionError("the library was called")

    monkeypatch.setattr(PlanService, "trace", boom)

    response = await spec.get(TRACE, project_id=ALPHA, **params)

    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_arguments"
    assert spec.executor.calls == []


async def test_a_trace_with_a_relation_the_ontology_lacks_is_unknown_relation_before_any_read(spec, monkeypatch):
    response = await spec.get(TRACE, project_id=ALPHA, story_id=CHECKOUT, direction="affects", relations=["NOPE"])

    assert response.status_code == 400 and response.json()["error"]["code"] == "unknown_relation"
    assert spec.executor.calls == []


async def test_a_trace_restricted_to_relations_walks_only_those(spec):
    everything = await spec.get(TRACE, project_id=ALPHA, spec_ids=["FEAT::CHECKOUT"], direction="based_on")
    only = await spec.get(
        TRACE, project_id=ALPHA, spec_ids=["FEAT::CHECKOUT"], direction="based_on", relations=["DEPENDS_ON"]
    )

    assert {n["relation"] for n in only.json()["nodes"]} == {"DEPENDS_ON"}
    assert len(only.json()["nodes"]) < len(everything.json()["nodes"])


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [
        ({"direction": "sideways", "story_id": CHECKOUT}, 422, "validation_failed"),
        ({"direction": "affects", "story_id": CHECKOUT, "depth": 4}, 422, "validation_failed"),
        ({"direction": "affects", "story_id": CHECKOUT, "limit": 201}, 422, "validation_failed"),
        ({"direction": "affects", "story_id": CHECKOUT, "basis": "all"}, 422, "validation_failed"),
        ({"direction": "affects", "spec_ids": [f"S{n}" for n in range(21)]}, 422, "validation_failed"),
        ({"direction": "affects", "story_id": "STORY::NOPE"}, 404, "story_not_found"),
    ],
)
async def test_a_trace_refuses_out_of_range_arguments_and_an_unknown_story(spec, params, status, code):
    response = await spec.get(TRACE, project_id=ALPHA, **params)

    assert response.status_code == status and response.json()["error"]["code"] == code


async def test_a_trace_from_a_story_needs_a_plan_but_one_from_spec_ids_does_not(monkeypatch, valid_env):
    async with serve_spec(monkeypatch, agent_env(valid_env), has_plan=False) as spec:
        story = await spec.get(TRACE, project_id=ALPHA, story_id=CHECKOUT, direction="affects")
        nodes = await spec.get(TRACE, project_id=ALPHA, spec_ids=["IFC::STRIPE_API"], direction="affects")

    assert story.status_code == 404 and story.json()["error"]["code"] == "no_plan"
    assert nodes.status_code == 200
    assert nodes.json()["summary"] is None and {n["plan_state"] for n in nodes.json()["nodes"]} == {None}


# --- get_node -----------------------------------------------------------------------------------------------


async def test_a_node_answers_its_parts_with_holders_and_open_findings(spec):
    response = await spec.get(NODE, project_id=ALPHA, spec_id="MOD::STRIPE")

    assert response.status_code == 200
    node = NodeDetail.model_validate(response.json())
    assert (node.node.spec_id, node.node.label, node.node.attributes) == (
        "MOD::STRIPE",
        "Module",
        {"external": "yes", "type": "other"},
    )
    assert node.edges is not None and node.edges.total == 4 and len(node.edges.items) == 4
    assert node.plan is not None and node.plan.plan_state == "in_progress"
    assert [(t.task_id, t.holder.name if t.holder else None) for t in node.plan.tasks] == [
        ("TASK::STRIPE::SETUP", "Alice"),
        ("TASK::STRIPE::WIRE", "Ben"),
    ]
    assert node.provenance == []
    assert node.findings is not None
    assert [(f.kind, f.author.name) for f in node.findings] == [("conflict", "Ben"), ("note", None)]  # the leaver
    assert_no_private_values(spec, response.text)


async def test_a_hub_summarises_the_edges_past_edge_limit_per_relation_and_direction(spec):
    response = await spec.get(NODE, project_id=ALPHA, spec_id="MOD::ORDERS_SERVICE", edge_limit=3)

    edges = response.json()["edges"]
    assert len(edges["items"]) == 3 and edges["total"] > 3
    counts = {(c["relation"], c["direction"]): c["count"] for c in edges["by_relation"]}
    assert sum(counts.values()) == edges["total"]
    assert counts[("PART_OF", "in")] >= 3  # features and rules sit under the module
    stated_first = [item["basis"] for item in edges["items"]]
    assert stated_first == sorted(stated_first, key=lambda basis: basis != "stated")
    names = [name for name, _ in spec.executor.calls]
    # the node (read again by ``neighbours``) and the shown edges' other ends in one read (no read per end)
    assert (names.count("read_node_by_spec_id"), names.count("read_node_uuids_by_spec_id")) == (2, 1)
    assert all(item["label"] is not None for item in edges["items"])


async def test_a_node_leaves_out_the_parts_not_asked_for_and_reads_only_what_it_returns(spec):
    response = await spec.get(NODE, project_id=ALPHA, spec_id="REQ::LOGIC_EXPORT", include=["provenance"])

    body = response.json()
    assert (body["edges"], body["plan"], body["findings"]) == (None, None, None)
    assert [p["path"] for p in body["provenance"]] == ["docs/shop.md"]
    assert [name for name, _ in spec.executor.calls if name == "read_neighbours"] == []


async def test_an_alias_resolves_and_an_unknown_node_is_node_not_found(spec):
    spec.executor.nodes["FEAT::CHECKOUT"]["aliases"] = ["FEAT::CHECKOUT_OLD"]

    aliased = await spec.get(NODE, project_id=ALPHA, spec_id="FEAT::CHECKOUT_OLD")
    unknown = await spec.get(NODE, project_id=ALPHA, spec_id="FEAT::NOPE")

    assert aliased.json()["node"]["spec_id"] == "FEAT::CHECKOUT"
    assert aliased.json()["node"]["spec_id_aliases"] == ["FEAT::CHECKOUT_OLD"]
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "node_not_found"


async def test_a_node_without_a_plan_has_a_null_plan_part(monkeypatch, valid_env):
    async with serve_spec(monkeypatch, agent_env(valid_env), has_plan=False) as spec:
        response = await spec.get(NODE, project_id=ALPHA, spec_id="MOD::STRIPE")

    assert response.status_code == 200 and response.json()["plan"] is None and response.json()["edges"] is not None


@pytest.mark.parametrize(
    "params", [{"edge_limit": 0}, {"edge_limit": 51}, {"include": ["everything"]}, {"include": ["edges", "nope"]}]
)
async def test_a_node_refuses_out_of_range_arguments(spec, params):
    response = await spec.get(NODE, project_id=ALPHA, spec_id="MOD::STRIPE", **params)

    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_failed"


# --- search_spec --------------------------------------------------------------------------------------------


async def test_a_search_without_near_is_the_hybrid_search_in_the_librarys_order(spec):
    response = await spec.get(SEARCH, project_id=ALPHA, query="checkout")

    assert response.status_code == 200
    found = SpecSearch.model_validate(response.json())
    assert found.project_id == ALPHA
    assert [h.spec_id for h in found.items] == [
        "FEAT::CHECKOUT",
        "FEAT::CHECKOUT_REVIEW",
        "REQ::FR::CHECKOUT",
        "REQ::Q::CHECKOUT_SPEED",
    ]
    assert {h.hops_from_near for h in found.items} == {None} and {h.matched for h in found.items} == {"text"}
    assert "read_edges" not in [name for name, _ in spec.executor.calls]  # no hops without near


async def test_a_search_by_an_explicit_id_is_an_id_hit_and_labels_filter(spec):
    by_id = await spec.get(SEARCH, project_id=ALPHA, query="what is FEAT::CARD_PAYMENT")
    only = await spec.get(SEARCH, project_id=ALPHA, query="checkout", labels=["Feature"])

    assert (by_id.json()["items"][0]["spec_id"], by_id.json()["items"][0]["matched"]) == ("FEAT::CARD_PAYMENT", "id")
    assert {h["label"] for h in only.json()["items"]} == {"Feature"}


async def test_a_search_near_a_node_ranks_hits_within_three_hops_first_by_hops_then_score(spec):
    near = await spec.get(SEARCH, project_id=ALPHA, query="payment order", near="FEAT::PLACE_ORDER", limit=3)

    items = near.json()["items"]
    assert len(items) == 3
    reached = [h["hops_from_near"] for h in items if h["hops_from_near"] is not None]
    assert reached == sorted(reached) and items[0]["hops_from_near"] is not None
    assert items[0]["spec_id"] in {"FEAT::PLACE_ORDER", "REQ::FR::PLACE_ORDER"}


async def test_a_search_near_a_story_starts_from_its_nodes(spec):
    response = await spec.get(SEARCH, project_id=ALPHA, query="checkout", near=CHECKOUT, limit=2)

    items = response.json()["items"]
    assert [h["hops_from_near"] for h in items] == [0, 0]  # the story's own nodes are at distance 0


async def test_a_hit_four_hops_from_near_has_no_hop_count_and_follows_the_reached_ones(spec):
    graph = shop_graph()
    chain = [f"FEAT::LANTERN{index}" for index in range(5)]
    for spec_id in chain:
        graph.node(spec_id, "Feature", name=f"Lantern {spec_id[-1]}")
    for first, second in itertools.pairwise(chain):
        graph.edge("DEPENDS_ON", first, second, basis="stated")
    with_plan(spec, TASKS, graph)

    response = await spec.get(SEARCH, project_id=ALPHA, query="lantern", near="FEAT::LANTERN4", limit=5)

    items = response.json()["items"]
    assert [(h["spec_id"], h["hops_from_near"]) for h in items] == [
        ("FEAT::LANTERN4", 0),
        ("FEAT::LANTERN3", 1),
        ("FEAT::LANTERN2", 2),
        ("FEAT::LANTERN1", 3),
        ("FEAT::LANTERN0", None),  # 4 hops away: past the 3 hops a search near a node ranks
    ]


@pytest.mark.parametrize(
    ("limit", "near", "fetched"),
    [(2, None, 2), (2, "FEAT::CHECKOUT", 6), (15, "FEAT::CHECKOUT", 45), (20, "FEAT::CHECKOUT", 60)],
)
async def test_a_search_fetches_limit_hits_or_three_times_that_up_to_sixty_near_a_node(spec, limit, near, fetched):
    params = {"near": near} if near else {}

    response = await spec.get(SEARCH, project_id=ALPHA, query="checkout", limit=limit, **params)

    assert response.status_code == 200 and len(response.json()["items"]) <= limit
    pages = [call["page"] for name, call in spec.executor.calls if name == "search_nodes_fulltext"]
    assert pages[0] == fetched


@pytest.mark.parametrize(
    "params",
    [{"near": "NOPE"}, {"labels": ["NoSuchLabel"]}, {"labels": ["Feature", "STORY"]}],
)
async def test_a_search_near_nothing_or_with_an_unknown_label_is_invalid_arguments(spec, params):
    response = await spec.get(SEARCH, project_id=ALPHA, query="checkout", **params)

    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize(
    "params", [{"query": ""}, {"query": "x" * 501}, {"query": "a", "limit": 0}, {"query": "a", "limit": 21}]
)
async def test_a_search_refuses_out_of_range_arguments(spec, params):
    response = await spec.get(SEARCH, project_id=ALPHA, **params)

    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_failed"


async def test_near_a_story_without_a_plan_is_invalid_arguments(monkeypatch, valid_env):
    async with serve_spec(monkeypatch, agent_env(valid_env), has_plan=False) as spec:
        response = await spec.get(SEARCH, project_id=ALPHA, query="checkout", near=CHECKOUT)

    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_arguments"


# --- every route: membership, infrastructure errors, logs ----------------------------------------------------

CALLS = {
    "story": (STORY, {"story_id": CHECKOUT}),
    "trace": (TRACE, {"story_id": CHECKOUT, "direction": "affects"}),
    "node": (NODE, {"spec_id": "MOD::STRIPE"}),
    "search": (SEARCH, {"query": "checkout"}),
}


@pytest.mark.parametrize("name", CALLS)
@pytest.mark.parametrize("project", [FOREIGN, UNKNOWN_PROJECT, "not-a-uuid"])
async def test_a_route_answers_a_project_the_caller_is_no_member_of_as_not_found_and_reads_nothing(spec, name, project):
    path, params = CALLS[name]

    response = await spec.get(path, project_id=project, **params)

    assert response.status_code == 404 and response.json()["error"]["code"] == "project_not_found"
    assert spec.executor.calls == [] and spec.memory.calls == []


@pytest.mark.parametrize("name", CALLS)
async def test_a_route_without_a_token_or_with_a_web_token_is_refused(spec, name):
    path, params = CALLS[name]

    assert (await spec.rig.get(path, None, project_id=ALPHA, **params)).status_code == 401
    assert (await spec.rig.get(path, web_alice(), project_id=ALPHA, **params)).status_code == 401


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (InfrastructureUnavailableError("neo4j_unreachable"), 503, "graph_unavailable"),
        (ConfigurationError("unknown_label", label="X"), 500, "internal_error"),
    ],
)
async def test_a_failing_graph_is_graph_unavailable_and_a_bug_is_internal_error(
    monkeypatch, valid_env, error, status, code
):
    async with serve_spec(monkeypatch, agent_env(valid_env), fail=error) as spec:
        node = await spec.get(NODE, project_id=ALPHA, spec_id="MOD::STRIPE")
        search = await spec.get(SEARCH, project_id=ALPHA, query="checkout")

    for response in (node, search):
        assert response.status_code == status and response.json()["error"]["code"] == code


async def test_the_reads_log_no_text_of_the_spec_the_plan_or_the_findings(spec, caplog):
    caplog.set_level("DEBUG")
    for path, params in CALLS.values():
        await spec.get(path, project_id=ALPHA, **params)

    haystack = log_haystack(caplog)
    for text in ("Which currencies", "Customer can complete checkout", "Stripe retries", "Alice", "Ben"):
        assert text not in haystack, text


# --- the lazy reads: findings and the query embedding gateway -----------------------------------------------

GRAPH_ENV = {
    "GRAPH_LLM_MODEL": "main/model",
    "GRAPH_SMALL_MODEL": "small/model",
    "GRAPH_EMBEDDING_MODEL": "embed/model",
    "GRAPH_EMBEDDING_DIM": "1536",
}


class RecordingGateway:
    """The LLM gateway boundary: records its configs and closes."""

    made: ClassVar[list[Any]] = []
    closed = 0

    def __init__(self, config: Any) -> None:
        type(self).made.append(config)

    async def aclose(self) -> None:
        type(self).closed += 1


async def test_the_lazy_reads_hold_the_findings_store_and_no_gateway_without_the_build_switches(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env), lazy=True) as rig:
        reads = await agent_reads(rig.runtime)

    assert reads.findings is not None and reads.plan._findings is reads.findings
    assert reads.reader._gateway is None


async def test_the_search_embeds_its_query_with_the_builds_gateway_which_closes_at_shutdown(monkeypatch, valid_env):
    RecordingGateway.made, RecordingGateway.closed = [], 0
    monkeypatch.setattr("api.agent_reads.Gateway", RecordingGateway)
    env = agent_env(valid_env, **GRAPH_ENV)
    async with serve_agent(monkeypatch, env, lazy=True) as rig:
        first = await agent_reads(rig.runtime)
        again = await agent_reads(rig.runtime)
        assert first is again and len(RecordingGateway.made) == 1
        assert first.reader._gateway is not None and RecordingGateway.closed == 0
    assert RecordingGateway.closed == 1


async def test_a_load_that_fails_closes_the_gateway_it_opened_and_is_tried_again(monkeypatch, valid_env):
    RecordingGateway.made, RecordingGateway.closed = [], 0
    monkeypatch.setattr("api.agent_reads.Gateway", RecordingGateway)

    def broken(*_: Any) -> Any:
        raise ConfigurationError("agent_reads_unreadable")

    monkeypatch.setattr("api.agent_reads._load", broken)
    async with serve_agent(monkeypatch, agent_env(valid_env, **GRAPH_ENV), lazy=True) as rig:
        for attempt in (1, 2):
            with pytest.raises(ConfigurationError):
                await agent_reads(rig.runtime)
            assert (len(RecordingGateway.made), RecordingGateway.closed) == (attempt, attempt)
        assert rig.runtime.agent_reads is None
