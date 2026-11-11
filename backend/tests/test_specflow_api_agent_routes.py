"""The API agent routes (docs/mcp-server/architecture.md, API agent routes; plan 04 Do 5): the six reads behind the MCP
tools, over the in-process API with the real verifiers and the real route code.

Boundaries faked: Postgres (``FakeRepo``), the graph (a scripted executor), the plan (the plan library over an
in-memory plan). Each test is named for the rule of plan 04 it holds: parity with the gate's goldens, the
caller-text project id, the cursor forms, the two paging styles, the bounds, the lazy reads and the user-id header.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

import api.agent_reads as agent_reads_module
import db.repositories as repo_module
from agent_contracts import AGENT_ROUTES, DEFAULT_LIMIT, MAX_CURSOR_CHARS
from api.routers.agent import decode_feature_cursor, encode_feature_cursor
from specflow_api_agent_support import (
    ALICE_SUBJECT,
    ALPHA,
    CALLS,
    FOREIGN,
    PROJECT_ROUTES,
    UNKNOWN_PROJECT,
    USER_HEADER,
    agent_env,
    alice,
    bearer_header,
    comparable,
    log_haystack,
    mcp_token,
    refuse_database,
    seed_many_projects,
    serve_agent,
    web_alice,
)

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "mcp" / "golden"
CURSORS = json.loads((GOLDEN / "cursors.json").read_text(encoding="utf-8"))
RESULTS = json.loads((GOLDEN / "tool_results.json").read_text(encoding="utf-8"))
PAGED = (
    "list_projects",
    "list_features",
    "list_tasks",
    "list_feature_tasks",
    "get_task_path",
)


@pytest.fixture
def env(valid_env: dict[str, str]) -> dict[str, str]:
    return agent_env(valid_env)


def cursor_of(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def task_ids(response: Any) -> list[str]:
    return [item["task_id"] for item in response.json()["items"]]


# --- parity with the MCP tools' goldens ---------------------------------------------------------------------


@pytest.mark.parametrize("tool", list(CALLS))
async def test_every_route_answers_the_json_of_the_golden_structured_content(monkeypatch, env, tool):
    """Plan 04 Done: page one of each route equals the tool's success golden (cursors included)."""

    async with serve_agent(monkeypatch, env) as rig:
        response = await rig.call(tool)
    assert response.status_code == 200, response.text
    assert response.json() == RESULTS[tool]["page_1"]["structuredContent"]


@pytest.mark.parametrize("tool", PAGED)
async def test_the_committed_cursors_of_the_gate_are_accepted_and_page_two_equals_the_golden(monkeypatch, env, tool):
    """Plan 04 Done: gate 02's fixture cursors are accepted by the matching routes."""

    async with serve_agent(monkeypatch, env) as rig:
        first = await rig.call(tool)
        second = await rig.call(tool, cursor=CURSORS[tool])
    assert first.json()["next_cursor"] == CURSORS[tool]
    assert second.status_code == 200, second.text
    assert second.json() == RESULTS[tool]["page_2_from_committed_cursor"]["structuredContent"]


# --- the project id is caller text ---------------------------------------------------------------------------

MALFORMED = [
    "",
    "x/../y",
    "not-a-uuid",
    " ",
    "00000000-0000-4000-8000-00000000000g",
    "%00",
    "a" * 300,
    "../../etc/passwd",
]


@pytest.mark.parametrize("tool", PROJECT_ROUTES)
async def test_a_malformed_empty_foreign_or_unknown_project_id_is_the_same_404_byte_for_byte(monkeypatch, env, tool):
    """Plan 04 Rules: ``project_id`` is caller text; every denial is ``project_not_found`` with the same bytes."""

    async with serve_agent(monkeypatch, env) as rig:
        denied = [await rig.call(tool, project_id=pid) for pid in (FOREIGN, UNKNOWN_PROJECT, *MALFORMED)]
    assert {r.status_code for r in denied} == {404}
    assert len({comparable(r) for r in denied}) == 1
    assert denied[0].json() == {
        "error": {
            "code": "project_not_found",
            "message": "The project does not exist.",
            "details": {},
        }
    }
    assert denied[0].headers[USER_HEADER]  # the user was provisioned before the denial


@pytest.mark.parametrize("tool", PROJECT_ROUTES)
async def test_a_malformed_project_id_runs_no_membership_query_and_no_graph_query(monkeypatch, env, tool):
    """Plan 04 Rules: malformed -> ``project_not_found`` with no query (a foreign id does run the membership one)."""

    async with serve_agent(monkeypatch, env) as rig:
        asked: list[Any] = []
        original = rig.repo.get_member_project

        async def spy(session: Any, user_id: Any, project_id: Any) -> Any:
            asked.append(project_id)
            return await original(session, user_id, project_id)

        monkeypatch.setattr("api.deps.repo.get_member_project", spy)
        for bad in MALFORMED:
            await rig.call(tool, project_id=bad)
        assert asked == []
        await rig.call(tool, project_id=FOREIGN)
        assert [str(a) for a in asked] == [FOREIGN]
    assert rig.executor.calls == [] and rig.plan.projects == []  # no denial reached the graph or the plan


async def test_a_malformed_project_id_is_never_logged(monkeypatch, env, caplog):
    """Plan 04 Rules: the caller's text is in no log record of any agent route (S2)."""

    caplog.set_level("DEBUG")
    marker = "id-marker-9d41c7/../x"
    async with serve_agent(monkeypatch, env) as rig:
        for tool in PROJECT_ROUTES:
            assert (await rig.call(tool, project_id=marker)).status_code == 404
    assert "id-marker-9d41c7" not in log_haystack(caplog)


@pytest.mark.parametrize("tool", PROJECT_ROUTES)
async def test_the_graph_and_the_plan_get_the_project_row_id_never_the_argument(monkeypatch, env, tool):
    """Plan 04 Rules: graph/plan get ``str(project.id)`` of the membership row, not the text the caller sent."""

    async with serve_agent(monkeypatch, env) as rig:
        upper = ALPHA.upper()  # same UUID, other spelling: the argument is not the canonical form
        response = await rig.call(tool, project_id=upper)
    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == ALPHA
    seen = set(rig.executor.project_ids) | set(rig.plan.projects)
    assert seen == {ALPHA}, seen


# --- cursors -------------------------------------------------------------------------------------------------


def canonical_of_length(chars: int) -> str:
    """A canonical ``{"v":1,"after":...}`` cursor of exactly ``chars`` characters (``chars`` a multiple of 4)."""

    overhead = len('{"v":1,"after":""}')
    return encode_feature_cursor("T" * (chars * 3 // 4 - overhead))


FEATURE_CURSOR_ROUTES = (
    "list_features",
    "list_tasks",
    "list_feature_tasks",
    "get_task_path",
)


@pytest.mark.parametrize("tool", FEATURE_CURSOR_ROUTES)
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(encode_feature_cursor("FEAT::F1") + "=", id="padding"),
        pytest.param(cursor_of('{"after":"FEAT::F1","v":1}'), id="key-order"),
        pytest.param(cursor_of('{"v":1,"after":"FEAT::F1","x":1}'), id="extra-key"),
        pytest.param(cursor_of('{"v":2,"after":"FEAT::F1"}'), id="version-2"),
        pytest.param(cursor_of('{"v":1,"after":""}'), id="empty-position"),
        pytest.param(cursor_of('{"v":1,"after":7}'), id="non-string-position"),
        pytest.param(cursor_of('{"v": 1, "after": "FEAT::F1"}'), id="spaces"),
        pytest.param(
            base64.b64encode(b'{"v":1,"after":"FEAT::F1>>"}').decode(),
            id="standard-alphabet",
        ),
        pytest.param("not a cursor", id="garbage"),
        pytest.param("", id="empty"),
    ],
)
async def test_only_the_canonical_cursor_is_valid_everything_else_is_400_invalid_cursor(monkeypatch, env, tool, bad):
    """Plan 04 Rules: canonical base64url ``{"v":1,"after":...}`` only; padding, key order, extra key, ``v=2`` fail."""

    async with serve_agent(monkeypatch, env) as rig:
        response = await rig.call(tool, cursor=bad)
    assert response.status_code == 400, response.text
    assert response.json()["error"] == {
        "code": "invalid_cursor",
        "message": "The cursor is invalid.",
        "details": {},
    }
    assert response.headers[USER_HEADER] == rig.alice_id


DOC_CURSOR = "eyJ2IjoxLCJhZnRlciI6IkZFQVQ6OkNIRUNLT1VUIn0"  # docs/mcp-server/tools.md, the list_features example


@pytest.mark.parametrize("tool", ["list_features", "list_tasks", "list_feature_tasks"])
@pytest.mark.parametrize(
    ("cursor", "after"),
    [
        pytest.param(DOC_CURSOR, "FEAT::CHECKOUT", id="doc-example-literal"),
        pytest.param(encode_feature_cursor("FEAT::ŁÓDŹ-日本"), "FEAT::ŁÓDŹ-日本", id="non-ascii-round-trip"),
    ],
)
async def test_the_documented_cursor_and_a_non_ascii_position_are_accepted_and_decode_to_their_position(
    monkeypatch, env, tool, cursor, after
):
    """The doc literal decodes to ``FEAT::CHECKOUT``; a non-ASCII ``spec_id`` survives encode, the wire and decode.
    (``get_task_path`` also needs the position to name an item of the path: its own refusal, tested above.)"""

    assert decode_feature_cursor(cursor) == after
    assert encode_feature_cursor(after) == cursor
    async with serve_agent(monkeypatch, env) as rig:
        response = await rig.call(tool, cursor=cursor)
    assert response.status_code == 200, response.text
    if tool == "list_features":
        assert [params["after"] for name, params in rig.executor.calls if name == "read_nodes_page"] == [after]


@pytest.mark.parametrize("tool", FEATURE_CURSOR_ROUTES)
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(cursor_of('{"v":1,"after":"\\ud800"}'), id="lone-surrogate-escaped"),
        pytest.param(
            base64.urlsafe_b64encode('{"v":1,"after":"\ud800"}'.encode("utf-8", "surrogatepass")).decode().rstrip("="),
            id="lone-surrogate-raw-bytes",
        ),
        pytest.param(cursor_of('{"v":true,"after":"FEAT::F1"}'), id="version-true"),
        pytest.param(cursor_of('{"v":1}'), id="no-after"),
        pytest.param(cursor_of('["x"]'), id="json-array"),
        pytest.param("Zm9yZ2Vk", id="not-json-forged"),
    ],
)
async def test_the_malformed_cursor_forms_dropped_with_the_old_mcp_tests_are_400_invalid_cursor(
    monkeypatch, env, tool, bad
):
    async with serve_agent(monkeypatch, env) as rig:
        response = await rig.call(tool, cursor=bad)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_cursor"
    assert response.headers[USER_HEADER] == rig.alice_id


@pytest.mark.parametrize("tool", ["list_projects", *FEATURE_CURSOR_ROUTES])
async def test_a_cursor_of_513_characters_is_refused_and_one_of_512_is_not_too_long(monkeypatch, env, tool):
    """Plan 04 Rules: more than ``MAX_CURSOR_CHARS`` -> ``invalid_cursor`` before any decoding; 512 is within bounds."""

    assert MAX_CURSOR_CHARS == 512
    exactly = canonical_of_length(512)
    assert len(exactly) == 512
    async with serve_agent(monkeypatch, env) as rig:
        over = await rig.call(tool, cursor="A" * 513)
        over_canonical = await rig.call(tool, cursor=encode_feature_cursor("T" * 400))
        edge = await rig.call(tool, cursor=exactly)
    assert over.status_code == over_canonical.status_code == 400
    assert over.json()["error"]["code"] == over_canonical.json()["error"]["code"] == "invalid_cursor"
    assert len(encode_feature_cursor("T" * 400)) > 512
    if tool == "list_projects":
        assert edge.status_code == 400  # within the length bound, but not a project cursor
    elif tool == "get_task_path":
        assert edge.status_code == 400  # canonical, names no item of the path
    else:
        assert edge.status_code == 200, edge.text  # canonical and long, merely past every item


async def test_the_512_character_bound_of_list_projects_is_checked_before_the_repository_is_called(monkeypatch, env):
    """The 513-character cursor never reaches ``repo.list_projects`` (a repository refusal would also be a 400, so
    the answer alone cannot tell); a canonical 512-character one does. The task path's bound is not observable the
    same way (no call site to spy on: its cursor names an item of a plan already loaded)."""

    seen: list[str] = []
    original = repo_module.list_projects

    async def spy(session: Any, user_id: Any, limit: int, cursor: str | None) -> Any:
        seen.append(cursor or "")
        return await original(session, user_id, limit, cursor)

    async with serve_agent(monkeypatch, env) as rig:
        monkeypatch.setattr("api.routers.agent.repo.list_projects", spy)
        over = await rig.call("list_projects", cursor="A" * 513)
        assert (over.status_code, over.json()["error"]["code"]) == (400, "invalid_cursor") and seen == []
        edge = await rig.call("list_projects", cursor=canonical_of_length(512))
    assert seen == [canonical_of_length(512)]
    assert (edge.status_code, edge.json()["error"]["code"]) == (400, "invalid_cursor")  # the repository's refusal


async def test_the_feature_cursor_pages_through_all_features_and_is_the_canonical_form(monkeypatch, env):
    async with serve_agent(monkeypatch, env) as rig:
        first = await rig.call("list_features", limit=1)
        second = await rig.call("list_features", limit=1, cursor=first.json()["next_cursor"])
    assert first.json()["next_cursor"] == encode_feature_cursor("FEAT::F1")
    assert [i["spec_id"] for i in second.json()["items"]] == ["FEAT::F2"]
    assert second.json()["next_cursor"] == encode_feature_cursor("FEAT::F2")


async def test_a_project_cursor_is_passed_to_the_repository_not_checked_as_a_feature_cursor(monkeypatch, env):
    """``list_projects`` has its own cursor (``<timestamp>|<id>``); a garbled one is ``invalid_cursor`` (repo)."""

    async with serve_agent(monkeypatch, env) as rig:
        bad = await rig.call("list_projects", cursor=cursor_of("no-bar-here"))
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_cursor"
    assert bad.headers[USER_HEADER] == rig.alice_id


# --- the two paging styles -----------------------------------------------------------------------------------


async def test_the_task_path_pages_with_page_and_a_cursor_naming_no_item_is_invalid(monkeypatch, env):
    """Plan 04 Rules (``_page``): the cursor must name an item of the path, else ``invalid_cursor``."""

    async with serve_agent(monkeypatch, env) as rig:
        named = await rig.call("get_task_path", cursor=encode_feature_cursor("TASK::ORDERS::A"))
        vanished = await rig.call("get_task_path", cursor=encode_feature_cursor("TASK::GONE"))
        not_in_path = await rig.call("get_task_path", cursor=encode_feature_cursor("TASK::PAY::E"))
    assert named.status_code == 200 and task_ids(named) == [
        "TASK::ORDERS::B",
        "TASK::ORDERS::C",
    ]
    assert vanished.status_code == 400 and vanished.json()["error"]["code"] == "invalid_cursor"
    assert not_in_path.status_code == 400


@pytest.mark.parametrize("tool", ["list_tasks", "list_feature_tasks"])
async def test_the_list_routes_page_with_after_and_a_vanished_task_id_is_fine(monkeypatch, env, tool):
    """Plan 04 Rules (``_after``): the list routes continue after a ``task_id`` that need not be listed any more."""

    gone = encode_feature_cursor("TASK::ORDERS::BZ")  # sorts between B and C; no such task
    async with serve_agent(monkeypatch, env) as rig:
        response = await rig.call(
            tool,
            cursor=gone,
            limit=100,
            **({"ready_only": "false"} if tool == "list_tasks" else {}),
        )
    assert response.status_code == 200, response.text
    expected = ["TASK::ORDERS::C", "TASK::ORDERS::D", "TASK::PAY::E"]
    assert task_ids(response) == (expected if tool == "list_tasks" else [*expected[:1], "TASK::PAY::E"])


async def test_a_task_that_became_ready_or_unlisted_does_not_break_a_list_cursor(monkeypatch, env):
    """The cursor of a ready-only page names ``TASK::ORDERS::C`` (not ready): the next page still continues after it."""

    async with serve_agent(monkeypatch, env) as rig:
        response = await rig.call("list_tasks", cursor=encode_feature_cursor("TASK::ORDERS::C"), limit=100)
    assert task_ids(response) == [
        "TASK::ORDERS::D",
        "TASK::PAY::E",
    ]  # ready ones after C


# --- bounds and defaults ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("neighbours", "status"), [(0, 200), (1, 200), (100, 200), (-1, 422), (101, 422)])
async def test_neighbours_is_bounded_0_to_100(monkeypatch, env, neighbours, status):
    async with serve_agent(monkeypatch, env) as rig:
        response = await rig.call("get_task_brief", neighbours=neighbours)
    assert response.status_code == status, response.text
    if status == 200:
        assert rig.plan.briefs == [(ALPHA, "TASK::ORDERS::B", neighbours)]
    else:
        assert rig.plan.briefs == [] and response.json()["error"]["code"] == "validation_failed"
        assert response.headers[USER_HEADER] == rig.alice_id


async def test_neighbours_defaults_to_30(monkeypatch, env):
    async with serve_agent(monkeypatch, env) as rig:
        assert (await rig.call("get_task_brief")).status_code == 200
    assert rig.plan.briefs == [(ALPHA, "TASK::ORDERS::B", 30)]


@pytest.mark.parametrize(
    "tool",
    [
        "list_projects",
        "list_features",
        "list_tasks",
        "list_feature_tasks",
        "get_task_path",
    ],
)
@pytest.mark.parametrize(("limit", "status"), [(1, 200), (100, 200), (0, 422), (101, 422), (-5, 422)])
async def test_limit_is_bounded_1_to_100(monkeypatch, env, tool, limit, status):
    async with serve_agent(monkeypatch, env) as rig:
        response = await rig.call(tool, limit=limit)
    assert response.status_code == status, response.text
    if status == 422:
        assert response.json()["error"]["code"] == "validation_failed"


async def test_limit_defaults_to_50_and_ready_only_to_true(monkeypatch, env):
    async with serve_agent(monkeypatch, env) as rig:
        path, params = CALLS["list_tasks"]
        bare = {"project_id": params["project_id"]}
        default = await rig.get(path, **bare)
        everything = await rig.get(path, ready_only="false", **bare)
        ready_false_done = await rig.get(path, ready_only="false", status="completed", **bare)
    assert task_ids(default) == [
        "TASK::ORDERS::B",
        "TASK::ORDERS::D",
        "TASK::PAY::E",
    ]  # ready only, no limit given
    assert task_ids(everything) == [
        "TASK::ORDERS::A",
        "TASK::ORDERS::B",
        "TASK::ORDERS::C",
        "TASK::ORDERS::D",
        "TASK::PAY::E",
    ]
    assert task_ids(ready_false_done) == ["TASK::ORDERS::A"]


async def test_the_default_page_size_is_50(monkeypatch, env):
    """No ``limit``: 50 items per page (the contract models' default), proven on 55 projects."""

    async with serve_agent(monkeypatch, env) as rig:
        await seed_many_projects(rig.repo, rig.users["alice"], 52)  # 3 seeded + 52
        first = await rig.get(AGENT_ROUTES["list_projects"])
    assert DEFAULT_LIMIT == 50
    assert first.status_code == 200 and len(first.json()["items"]) == 50 and first.json()["next_cursor"]


@pytest.mark.parametrize("status", ["superseded", "bogus"])
async def test_a_status_outside_the_filter_is_422(monkeypatch, env, status):
    async with serve_agent(monkeypatch, env) as rig:
        response = await rig.call("list_tasks", status=status)
    assert response.status_code == 422


# --- the plan reads' errors by their own codes -------------------------------------------------------------------


async def test_a_project_without_a_plan_is_404_no_plan_on_each_plan_route(monkeypatch, env):
    async with serve_agent(monkeypatch, env, has_plan=False) as rig:
        answers = {
            tool: await rig.call(tool)
            for tool in (
                "list_tasks",
                "list_feature_tasks",
                "get_task_path",
                "get_task_brief",
            )
        }
    assert {t: (r.status_code, r.json()["error"]["code"]) for t, r in answers.items()} == dict.fromkeys(
        answers, (404, "no_plan")
    )


@pytest.mark.parametrize(
    ("tool", "over", "code"),
    [
        ("get_task_brief", {"task_id": "TASK::NOPE"}, "task_not_found"),
        ("get_task_brief", {"task_id": "TASK::ORDERS::OLD"}, "task_not_found"),
        ("get_task_path", {"task_id": "TASK::NOPE"}, "task_not_found"),
        ("list_feature_tasks", {"feature_id": "FEAT::NOPE"}, "feature_not_found"),
    ],
)
async def test_an_unknown_task_or_feature_is_404_with_its_own_code(monkeypatch, env, tool, over, code):
    async with serve_agent(monkeypatch, env) as rig:
        response = await rig.call(tool, **over)
    assert response.status_code == 404 and response.json()["error"]["code"] == code
    assert response.headers[USER_HEADER] == rig.alice_id


async def test_every_error_body_is_the_api_envelope_with_fixed_text_and_no_exception_text(monkeypatch, env):
    async with serve_agent(monkeypatch, env, scenario="bug") as rig:
        response = await rig.call("list_features")
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "An internal error occurred.",
            "details": {},
        }
    }
    assert "unknown_label" not in response.text


# --- infrastructure errors in the handler -------------------------------------------------------------------------


async def test_a_graph_outage_is_503_graph_unavailable(monkeypatch, env):
    async with serve_agent(monkeypatch, env, scenario="graph_down") as rig:
        response = await rig.call("list_features")
    assert response.status_code == 503 and response.json()["error"]["code"] == "graph_unavailable"
    assert response.headers[USER_HEADER] == rig.alice_id


async def test_a_database_outage_in_the_handler_is_503_database_unavailable(monkeypatch, env):
    async with serve_agent(monkeypatch, env) as rig:

        async def refused(*_: Any, **__: Any) -> Any:
            raise ConnectionRefusedError("[Errno 111] Connect call failed")

        monkeypatch.setattr("api.routers.agent.repo.list_projects", refused)
        response = await rig.call("list_projects")
    assert response.status_code == 503 and response.json()["error"]["code"] == "database_unavailable"
    assert response.headers[USER_HEADER] == rig.alice_id


async def test_a_database_outage_during_the_membership_query_is_503_with_the_user_id_header(monkeypatch, env):
    """The user is provisioned (so named) before ``agent_project`` asks the repository whether it is a member."""

    async def refused(*_: Any, **__: Any) -> Any:
        raise ConnectionRefusedError("[Errno 111] Connect call failed")

    async with serve_agent(monkeypatch, env) as rig:
        monkeypatch.setattr("api.deps.repo.get_member_project", refused)
        response = await rig.call("list_features")
    assert response.status_code == 503 and response.json()["error"]["code"] == "database_unavailable"
    assert response.headers[USER_HEADER] == rig.alice_id


@pytest.mark.parametrize("scenario", ["ok", "graph_down"])
async def test_a_safe_inbound_request_id_is_adopted_and_echoed_on_success_and_on_a_mapped_503(
    monkeypatch, env, scenario
):
    """``X-Request-ID`` (short plain text) comes back unchanged: on a 200 and on the ``AgentRoute``-mapped 503."""

    inbound = "agent-req-0123456789"
    async with serve_agent(monkeypatch, env, scenario=scenario) as rig:
        path, params = CALLS["list_features"]
        response = await rig.client.get(
            path, params=params, headers={**bearer_header(alice()), "X-Request-ID": inbound}
        )
    assert response.status_code == (200 if scenario == "ok" else 503), response.text
    assert response.headers["x-request-id"] == inbound


# --- the lazy reads ---------------------------------------------------------------------------------------------


async def test_the_reads_load_once_on_the_first_agent_read_and_not_for_list_projects(monkeypatch, env):
    loads: list[str] = []
    original = agent_reads_module.graph_core.load_ontology

    def counting() -> Any:
        loads.append("ontology")
        return original()

    monkeypatch.setattr(agent_reads_module.graph_core, "load_ontology", counting)
    async with serve_agent(monkeypatch, env, lazy=True) as rig:
        assert (await rig.call("list_projects")).status_code == 200
        assert loads == [] and rig.runtime.agent_reads is None
        await rig.call("list_tasks")  # loads; its own answer (the scripted graph's) is not what is asserted
        await rig.call("list_features")
        assert rig.runtime.agent_reads is not None
    assert loads == ["ontology"]


async def test_the_agent_reads_do_not_depend_on_the_graph_build_switches(monkeypatch, env):
    """``Runtime.plans`` is ``None`` here (no build switches): the agent routes read all the same."""

    async with serve_agent(monkeypatch, env) as rig:
        assert rig.runtime.plans is None and rig.runtime.graph_builds is None
        assert (await rig.call("list_tasks")).status_code == 200


# --- the user-id header --------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", list(CALLS))
async def test_every_success_names_the_provisioned_user(monkeypatch, env, tool):
    async with serve_agent(monkeypatch, env) as rig:
        response = await rig.call(tool)
    assert response.status_code == 200
    assert response.headers[USER_HEADER] == rig.alice_id == str(rig.repo.users[ALICE_SUBJECT].id)


async def test_a_caller_sent_user_id_header_is_ignored(monkeypatch, env):
    async with serve_agent(monkeypatch, env) as rig:
        path, params = CALLS["list_projects"]
        spoofed = await rig.client.get(
            path,
            params=params,
            headers={
                "Authorization": f"Bearer {alice()}",
                USER_HEADER: "00000000-dead-beef-0000-000000000000",
            },
        )
        denied = await rig.client.get(
            path, headers={USER_HEADER: "00000000-dead-beef-0000-000000000000"}
        )  # no token: the sent header is not echoed either
    assert spoofed.headers[USER_HEADER] == rig.alice_id
    assert denied.status_code == 401 and USER_HEADER not in denied.headers


async def test_the_header_is_on_400_404_422_and_500_after_provisioning_and_only_there(monkeypatch, env):
    async with serve_agent(monkeypatch, env) as rig:
        by_status = {
            400: await rig.call("list_features", cursor="x"),
            404: await rig.call("list_features", project_id=UNKNOWN_PROJECT),
            422: await rig.call("list_features", limit=0),
            "405-unrouted-method": await rig.client.post(
                AGENT_ROUTES["list_projects"],
                headers={"Authorization": f"Bearer {alice()}"},
            ),
        }
    for status in (400, 404, 422):
        assert by_status[status].status_code == status
        assert by_status[status].headers[USER_HEADER] == rig.alice_id, status
    assert USER_HEADER not in by_status["405-unrouted-method"].headers  # method check precedes the dependencies

    async with serve_agent(monkeypatch, env, scenario="bug") as rig:
        failed = await rig.call("list_features")
    assert failed.status_code == 500 and failed.headers[USER_HEADER] == rig.alice_id


async def test_a_422_for_a_missing_required_argument_carries_the_header(monkeypatch, env):
    async with serve_agent(monkeypatch, env) as rig:
        missing = await rig.get(AGENT_ROUTES["get_task_brief"], project_id=ALPHA)  # no task_id
    assert missing.status_code == 422 and missing.headers[USER_HEADER] == rig.alice_id


async def test_no_header_on_401_and_on_503_before_the_user_is_provisioned(monkeypatch, env):
    async with serve_agent(monkeypatch, env) as rig:
        no_token = await rig.call("list_projects", raw=None)
        web = await rig.call("list_projects", raw=web_alice())
    assert [no_token.status_code, web.status_code] == [401, 401]
    assert USER_HEADER not in no_token.headers and USER_HEADER not in web.headers


async def test_no_header_on_403_domain_not_allowed(monkeypatch, env):
    """A verified token of a domain outside ``AUTH_ALLOWED_EMAIL_DOMAINS`` is refused before provisioning."""

    async with serve_agent(monkeypatch, env) as rig:
        response = await rig.call("list_projects", mcp_token("mallory@other.example"))
    assert response.status_code == 403 and response.json()["error"]["code"] == "domain_not_allowed"
    assert USER_HEADER not in response.headers
    assert "user:mallory@other.example" not in rig.repo.users  # nothing was provisioned


@pytest.mark.parametrize("tool", list(CALLS))
async def test_a_postgres_outage_during_provisioning_is_503_database_unavailable_without_a_header(
    monkeypatch, env, tool
):
    """Plan 04 Done: the generic 500 never answers it, on any of the six routes; the user is unknown, so no header."""

    async with serve_agent(monkeypatch, env) as rig:
        refuse_database(monkeypatch)
        response = await rig.call(tool)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "database_unavailable"
    assert USER_HEADER not in response.headers


# --- REST is untouched ----------------------------------------------------------------------------------------


async def test_rest_responses_never_carry_the_user_id_header(monkeypatch, env):
    async with serve_agent(monkeypatch, env) as rig:
        web = {
            "Authorization": f"Bearer {web_alice()}",
            USER_HEADER: "00000000-dead-beef-0000-000000000000",
        }
        answers = [
            await rig.client.get("/api/v1/me", headers=web),
            await rig.client.get("/api/v1/projects", headers=web),
            await rig.client.get(f"/api/v1/projects/{UNKNOWN_PROJECT}/documents", headers=web),  # a REST 404
            await rig.client.get("/api/v1/projects", params={"limit": 0}, headers=web),  # a REST 422
            await rig.client.get("/api/v1/projects"),  # a REST 401
        ]
    assert [r.status_code for r in answers] == [200, 200, 404, 422, 401]
    assert all(USER_HEADER not in r.headers for r in answers)


async def test_the_rest_projects_route_keeps_422_validation_failed_for_a_bad_cursor(monkeypatch, env):
    """Plan 03 Rules: the REST list is not aligned with the agent route's ``400 invalid_cursor``."""

    async with serve_agent(monkeypatch, env) as rig:
        rest = await rig.client.get(
            "/api/v1/projects",
            params={"cursor": "x"},
            headers={"Authorization": f"Bearer {web_alice()}"},
        )
        agent = await rig.call("list_projects", cursor="x")
    assert (rest.status_code, rest.json()["error"]["code"]) == (
        422,
        "validation_failed",
    )
    assert (agent.status_code, agent.json()["error"]["code"]) == (400, "invalid_cursor")


async def test_the_rest_routes_still_work_with_the_agent_routes_on(monkeypatch, env):
    async with serve_agent(monkeypatch, env) as rig:
        response = await rig.client.get("/api/v1/projects", headers={"Authorization": f"Bearer {web_alice()}"})
    assert response.status_code == 200
    assert {item["name"] for item in response.json()["items"]} == {
        "Alpha",
        "Beta",
        "Gamma",
    }
