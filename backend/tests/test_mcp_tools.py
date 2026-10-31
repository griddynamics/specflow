"""The MCP tools over HTTP (docs/mcp-server/testing.md, HTTP boundary): the SDK client session, dev tokens, the real
tool registry over the REST ``FakeRepo`` and a recording graph executor (``ScriptedExecutor``).

Boundaries faked: Postgres (``FakeRepo``) and Neo4j (the executor, which records every graph query).
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from api.mcp.errors import MESSAGES
from api.mcp.tools.graph import encode_feature_cursor
from graph.core import ConfigurationError, GraphReader, InfrastructureUnavailableError
from mcp_support import (
    EXTREME_PROJECT_CURSOR,
    MethodRouter,
    assert_no_error_records,
    call,
    code,
    connected,
    cursor_of,
    feature_ids,
    log_text,
    mcp_dev_token,
    owned_project,
    sdk_session,
    structured,
    text,
    tool_app,
)
from test_graph_reader import ScriptedExecutor, node_row, paged_executor

FEATURE = "Feature"
ALICE = mcp_dev_token("alice@example.com")
BOB = mcp_dev_token("bob@example.com")
SEVEN = [f"FEAT::F{n}" for n in range(1, 8)]


def features(spec_ids: list[str] = SEVEN) -> ScriptedExecutor:
    return paged_executor(spec_ids, FEATURE)


# --- FR-MCP-ACC-01..03: tenant isolation ---------------------------------------------------------


async def test_fr_mcp_acc_01_list_projects_holds_only_the_callers_projects(monkeypatch, valid_env):
    """FR-MCP-ACC-01: alice owns two projects and bob one; each lists only his or her own."""

    app, repo = tool_app(monkeypatch, valid_env, features())
    mine = [await owned_project(repo, "alice@example.com", n) for n in ("Alpha", "Empty")]
    theirs = await owned_project(repo, "bob@example.com", "Beta")
    async with sdk_session(app, ALICE) as session:
        alice = await call(session, "list_projects")
    other, _ = tool_app(monkeypatch, valid_env, features(), repo=repo)  # an app runs its lifespan once
    async with sdk_session(other, BOB) as session:
        bob = await call(session, "list_projects")
    assert {i["project_id"] for i in structured(alice)["items"]} == {str(p.id) for p in mine}
    assert [i["project_id"] for i in structured(bob)["items"]] == [str(theirs.id)]
    assert str(theirs.id) not in text(alice)


async def test_fr_mcp_acc_02_a_foreign_project_runs_no_graph_query(monkeypatch, valid_env):
    """FR-MCP-ACC-02: alice reads bob's project: ``project_not_found`` and the recording executor saw nothing."""

    executor = features()
    app, repo = tool_app(monkeypatch, valid_env, executor)
    foreign = await owned_project(repo, "bob@example.com", "Beta")
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_features", project_id=str(foreign.id))
    assert code(result) == "project_not_found"
    assert executor.calls == []


async def test_fr_mcp_acc_03_foreign_unknown_and_malformed_ids_answer_byte_for_byte_alike(monkeypatch, valid_env):
    """FR-MCP-ACC-03: bob's id, a random UUID and ``not-a-uuid`` give identical results and zero graph queries."""

    executor = features()
    app, repo = tool_app(monkeypatch, valid_env, executor)
    foreign = await owned_project(repo, "bob@example.com", "Beta")
    async with sdk_session(app, ALICE) as session:
        results = [
            await call(session, "list_features", project_id=value)
            for value in (str(foreign.id), str(uuid.uuid4()), "not-a-uuid")
        ]
    dumps = {json.dumps(r.model_dump(mode="json", by_alias=True), sort_keys=True) for r in results}
    assert len(dumps) == 1
    assert {text(r) for r in results} == {text(results[0])}
    assert code(results[0]) == "project_not_found"
    assert text(results[0]) == json.dumps(
        {
            "error": {
                "code": "project_not_found",
                "message": MESSAGES["project_not_found"],
            }
        },
        separators=(",", ":"),
    )
    assert executor.calls == []


async def test_fr_mcp_acc_03_a_malformed_id_runs_no_membership_query_and_the_others_run_one(monkeypatch, valid_env):
    """FR-MCP-ACC-03: ``not-a-uuid`` is denied without a Postgres query; an unknown and a foreign id each run exactly
    one ``get_member_project`` (the same work, so the answer carries no timing or query-count signal)."""

    app, repo = tool_app(monkeypatch, valid_env, features())
    foreign = await owned_project(repo, "bob@example.com", "Beta")
    queried: list[Any] = []
    original = repo.get_member_project

    async def counting(session: Any, user_id: Any, project_id: Any) -> Any:
        queried.append(project_id)
        return await original(session, user_id, project_id)

    monkeypatch.setattr("api.mcp.access.repositories.get_member_project", counting)
    unknown = uuid.uuid4()
    async with sdk_session(app, ALICE) as session:
        for value in ("not-a-uuid", "", "x" * 5000, str(unknown), str(foreign.id)):
            assert code(await call(session, "list_features", project_id=value)) == "project_not_found"
    assert queried == [unknown, foreign.id]


@pytest.mark.parametrize(
    "spell",
    [str.upper, lambda value: "{" + value + "}", lambda value: value.replace("-", "")],
)
async def test_fr_mcp_acc_03_another_spelling_of_an_own_id_reads_with_the_canonical_id(
    monkeypatch, valid_env, spell: Callable[[str], str]
):
    """FR-MCP-ACC-03: the graph gets ``str(project.id)`` of the membership result, never the argument."""

    executor = features()
    app, repo = tool_app(monkeypatch, valid_env, executor)
    own = await owned_project(repo, "alice@example.com", "Alpha")
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_features", project_id=spell(str(own.id)))
    assert not result.is_error
    assert structured(result)["project_id"] == str(own.id)
    assert set(executor.project_ids) == {str(own.id)}


async def test_fr_mcp_acc_03_an_unknown_argument_is_ignored_and_names_no_user(monkeypatch, valid_env):
    """FR-MCP-ACC-03 / TOOL (identity): ``user_id`` is no argument; the caller is the token's principal."""

    app, repo = tool_app(monkeypatch, valid_env, features())
    await owned_project(repo, "alice@example.com", "Alpha")
    theirs = await owned_project(repo, "bob@example.com", "Beta")
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_projects", user_id=str(theirs.created_by))
    assert [i["name"] for i in structured(result)["items"]] == ["Alpha"]


# --- FR-MCP-TOOL-01..05: paging and shaping ---------------------------------------------------


async def test_fr_mcp_tool_01_list_projects_pages_newest_first(monkeypatch, valid_env):
    """FR-MCP-TOOL-01: three projects, ``limit=2``: two then one, the last ``next_cursor`` null, newest first."""

    app, repo = tool_app(monkeypatch, valid_env, features())
    for name in ("one", "two", "three"):
        await owned_project(repo, "alice@example.com", name)
    async with sdk_session(app, ALICE) as session:
        first = await call(session, "list_projects", limit=2)
        second = await call(session, "list_projects", limit=2, cursor=structured(first)["next_cursor"])
    assert [i["name"] for i in structured(first)["items"]] == ["three", "two"]
    assert structured(first)["next_cursor"]
    assert [i["name"] for i in structured(second)["items"]] == ["one"]
    assert structured(second)["next_cursor"] is None
    assert set(structured(first)["items"][0]) == {"project_id", "name", "created_at"}


async def test_fr_mcp_tool_03_list_features_pages_in_spec_id_order_with_the_total(monkeypatch, valid_env):
    """FR-MCP-TOOL-03 / TOOL-02: seven features, ``limit=5``: five then two, ``total_count`` 7 both, in order."""

    executor = features()
    app, repo = tool_app(monkeypatch, valid_env, executor)
    project = await owned_project(repo, "alice@example.com", "Alpha")
    async with sdk_session(app, ALICE) as session:
        first = await call(session, "list_features", project_id=str(project.id), limit=5)
        second = await call(
            session,
            "list_features",
            project_id=str(project.id),
            limit=5,
            cursor=structured(first)["next_cursor"],
        )
    assert feature_ids(first) + feature_ids(second) == SEVEN
    assert (len(feature_ids(first)), len(feature_ids(second))) == (5, 2)
    assert (structured(first)["total_count"], structured(second)["total_count"]) == (
        7,
        7,
    )
    assert structured(first)["next_cursor"] and structured(second)["next_cursor"] is None


async def test_fr_mcp_tool_03_an_empty_graph_answers_zero_and_no_items(monkeypatch, valid_env):
    """FR-MCP-TOOL-03: a project whose graph was never built: ``total_count`` 0, no items, no cursor."""

    app, repo = tool_app(monkeypatch, valid_env, features([]))
    project = await owned_project(repo, "alice@example.com", "Empty")
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_features", project_id=str(project.id))
    assert (
        structured(result)["total_count"],
        structured(result)["items"],
        structured(result)["next_cursor"],
    ) == (
        0,
        [],
        None,
    )


async def test_fr_mcp_tool_04_the_page_reads_limit_plus_one_and_never_the_whole_graph(monkeypatch, valid_env):
    """FR-MCP-TOOL-04: ``read_nodes_page`` gets ``limit + 1`` as its ``limit``; ``GraphReader.nodes`` never runs."""

    executor = features()
    app, repo = tool_app(monkeypatch, valid_env, executor)
    project = await owned_project(repo, "alice@example.com", "Alpha")
    async with sdk_session(app, ALICE) as session:
        await call(session, "list_features", project_id=str(project.id), limit=3)
    pages = [params for name, params in executor.calls if name == "read_nodes_page"]
    assert [p["limit"] for p in pages] == [4]
    assert pages[0]["labels"] == [FEATURE] and pages[0]["after"] is None
    assert sorted(set(executor.names())) == ["count_nodes", "read_nodes_page"]
    assert "read_nodes" not in executor.names()


async def test_fr_mcp_tool_04_nodes_is_never_called_on_the_reader(monkeypatch, valid_env):
    """FR-MCP-TOOL-04: ``GraphReader.nodes`` is not used by ``list_features`` (patched to fail the test)."""

    async def forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("GraphReader.nodes must not be called")

    monkeypatch.setattr(GraphReader, "nodes", forbidden)
    app, repo = tool_app(monkeypatch, valid_env, features())
    project = await owned_project(repo, "alice@example.com", "Alpha")
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_features", project_id=str(project.id))
    assert not result.is_error and structured(result)["total_count"] == 7


async def test_fr_mcp_tool_05_an_item_holds_only_spec_id_name_and_stored_attributes(monkeypatch, valid_env):
    """FR-MCP-TOOL-05 / TOOL-02: embeddings, an unknown property, statements and provenance never reach the result."""

    row = node_row(
        "FEAT::CHECKOUT",
        FEATURE,
        stmt="Shoppers pay in one step.",
        quote="The checkout shall complete in a single page.",
        name_embedding=[0.1, 0.2],
        embedding=[0.3],
        mystery="UNKNOWN-PROP",
        statements=json.dumps([{"fragment_id": "f1", "text": "STATEMENT-TEXT"}]),
    )
    row["provenance"] = [
        {
            "document_id": "d1",
            "path": "PROVENANCE-PATH.md",
            "fragment_id": "f1",
            "line_start": 3,
        }
    ]
    executor = ScriptedExecutor({"read_nodes_page": lambda _: [row], "count_nodes": lambda _: [{"total": 1}]})
    app, repo = tool_app(monkeypatch, valid_env, executor)
    project = await owned_project(repo, "alice@example.com", "Alpha")
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_features", project_id=str(project.id))
    (item,) = structured(result)["items"]
    assert set(item) == {"spec_id", "name", "attributes"}
    assert item["attributes"] == {
        "stmt": "Shoppers pay in one step.",
        "quote": "The checkout shall complete in a single page.",
    }
    assert json.loads(text(result)) == structured(result)  # one text block holding the same JSON
    assert len(result.content) == 1
    for leaked in (
        "UNKNOWN-PROP",
        "STATEMENT-TEXT",
        "PROVENANCE-PATH",
        "0.1",
        "embedding",
        "uuid",
    ):
        assert leaked not in text(result)


async def test_fr_mcp_tool_05_a_result_holds_structured_content_and_the_same_json_text(monkeypatch, valid_env):
    """FR-MCP-TOOL-05: ``list_projects`` too: ``created_at`` is ISO text in both forms."""

    app, repo = tool_app(monkeypatch, valid_env, features())
    await owned_project(repo, "alice@example.com", "Alpha")
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_projects")
    assert not result.is_error and json.loads(text(result)) == structured(result)


# --- FR-MCP-TOOL-07: cursors through both tools ----------------------------------------------------


FEATURE_CURSORS = [
    "a" * 513,
    "!!!",
    "Zm9yZ2Vk",  # base64 of "forged"
    cursor_of('{"v":2,"after":"FEAT::A"}'),
    cursor_of('{"v":1}'),
    cursor_of('{"v":1,"after":""}'),
    cursor_of('{"v":1,"after":"\\ud800"}'),  # R-01: a lone surrogate
    base64.urlsafe_b64encode(b'{"v":1,"after":"FEAT::A"}').decode(),  # padded form
]


@pytest.mark.parametrize("cursor", FEATURE_CURSORS)
async def test_fr_mcp_tool_07_a_bad_feature_cursor_is_invalid_cursor_with_the_fixed_message(
    monkeypatch, valid_env, cursor: str
):
    """FR-MCP-TOOL-07: forged, 513-character, padded and lone-surrogate (R-01) cursors; no graph page read."""

    executor = features()
    app, repo = tool_app(monkeypatch, valid_env, executor)
    project = await owned_project(repo, "alice@example.com", "Alpha")
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_features", project_id=str(project.id), cursor=cursor)
    assert code(result) == "invalid_cursor"
    assert structured(result)["error"]["message"] == MESSAGES["invalid_cursor"]
    assert executor.calls == []


@pytest.mark.parametrize("cursor", ["a" * 513, "Zm9yZ2Vk", EXTREME_PROJECT_CURSOR])
async def test_fr_mcp_tool_07_a_bad_project_cursor_is_invalid_cursor(monkeypatch, valid_env, cursor: str):
    """FR-MCP-TOOL-07: ``list_projects``: 513 characters, forged, and a timestamp that overflows in UTC (F1)."""

    app, repo = tool_app(monkeypatch, valid_env, features())
    await owned_project(repo, "alice@example.com", "Alpha")
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_projects", cursor=cursor)
    assert code(result) == "invalid_cursor"


async def test_fr_mcp_tool_07_a_valid_cursor_of_another_position_pages_on(monkeypatch, valid_env):
    """FR-MCP-TOOL-07: a well-formed feature cursor continues after its ``spec_id`` (position only)."""

    app, repo = tool_app(monkeypatch, valid_env, features())
    project = await owned_project(repo, "alice@example.com", "Alpha")
    async with sdk_session(app, ALICE) as session:
        result = await call(
            session,
            "list_features",
            project_id=str(project.id),
            cursor=encode_feature_cursor("FEAT::F5"),
        )
    assert feature_ids(result) == ["FEAT::F6", "FEAT::F7"]


async def test_fr_mcp_tool_07_a_foreign_project_with_a_bad_cursor_stays_project_not_found(monkeypatch, valid_env):
    """FR-MCP-TOOL-07 / ACC-03: the cursor is checked after the membership check."""

    app, repo = tool_app(monkeypatch, valid_env, features())
    foreign = await owned_project(repo, "bob@example.com", "Beta")
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_features", project_id=str(foreign.id), cursor="!!!")
    assert code(result) == "project_not_found"


# --- FR-MCP-TOOL-08: error mapping through the wrapper ------------------------------------------


async def test_fr_mcp_tool_08_a_graph_outage_is_graph_unavailable_without_driver_text(monkeypatch, valid_env, mcp_log):
    """FR-MCP-TOOL-08: ``InfrastructureUnavailableError`` answers ``graph_unavailable`` and the fixed message; nothing
    is logged at ERROR or with ``exc_info`` by any server-side logger and the driver text is in no record."""

    def down(_: Any) -> list[dict[str, Any]]:
        raise InfrastructureUnavailableError("neo4j_unreachable", uri="bolt://DRIVER-HOST:7687")

    app, repo = tool_app(monkeypatch, valid_env, ScriptedExecutor({"count_nodes": down}))
    project = await owned_project(repo, "alice@example.com", "Alpha")
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_features", project_id=str(project.id))
        projects = await call(session, "list_projects")
    assert code(result) == "graph_unavailable"
    assert structured(result)["error"]["message"] == MESSAGES["graph_unavailable"]
    assert "DRIVER-HOST" not in text(result) and "neo4j_unreachable" not in text(result)
    assert not projects.is_error  # Postgres tools stay up
    assert_no_error_records(mcp_log)
    assert "DRIVER-HOST" not in log_text(mcp_log, "api.mcp") + log_text(mcp_log, "mcp.server")


def refusing(target: str) -> Callable[[pytest.MonkeyPatch], None]:
    """A step that makes ``target`` (a dotted attribute) fail like an unreachable Postgres (``OSError``)."""

    def install(monkeypatch: pytest.MonkeyPatch) -> None:
        async def refused(*_: Any) -> None:
            raise ConnectionRefusedError("[Errno 111] Connect call failed ('10.9.8.7', 5432)")

        monkeypatch.setattr(target, refused)

    return install


@pytest.mark.parametrize(
    ("step", "tool", "has_user"),
    [
        (refusing("api.mcp.access.repositories.upsert_user"), "list_projects", False),
        (refusing("api.mcp.access.repositories.upsert_user"), "list_features", False),
        (refusing("api.mcp.access.repositories.get_member_project"), "list_features", True),
        (refusing("api.mcp.tools.projects.repositories.list_projects"), "list_projects", True),
    ],
    ids=["provisioning-unscoped", "provisioning-scoped", "membership", "list_projects-handler"],
)
async def test_fr_mcp_tool_08_an_unreachable_database_is_database_unavailable_at_every_step(
    monkeypatch, valid_env, mcp_log, *, step: Callable[..., Any], tool: str, has_user: bool
):
    """FR-MCP-TOOL-08: ``ConnectionRefusedError`` (an ``OSError``) at provisioning, at the membership step or inside the
    ``list_projects`` handler: ``database_unavailable``, no graph query, the audit event carries ``user_id`` exactly
    when provisioning had succeeded, and no server-side logger writes an ERROR record or a traceback."""

    executor = features()
    app, repo = tool_app(monkeypatch, valid_env, executor)
    project = await owned_project(repo, "alice@example.com", "Alpha")
    step(monkeypatch)
    async with sdk_session(app, ALICE) as session:
        result = await call(
            session,
            tool,
            **({"project_id": str(project.id)} if tool == "list_features" else {}),
        )
    assert code(result) == "database_unavailable"
    assert structured(result)["error"]["message"] == MESSAGES["database_unavailable"]
    assert "10.9.8.7" not in text(result) and "Errno" not in text(result)
    assert executor.calls == []
    (event,) = audit(mcp_log)
    assert event.outcome == "database_unavailable" and event.error_type == "ConnectionRefusedError"
    assert hasattr(event, "user_id") is has_user
    assert_no_error_records(mcp_log)
    assert "10.9.8.7" not in log_text(mcp_log, "api.mcp") + log_text(mcp_log, "mcp.server")


async def test_fr_mcp_tool_08_a_graph_library_bug_is_internal_error_without_its_text(monkeypatch, valid_env, mcp_log):
    """FR-MCP-TOOL-08: ``ConfigurationError`` from the reader is a bug: ``internal_error``, no exception text, no
    ERROR record or ``exc_info`` from any server-side logger."""

    def broken(_: Any) -> list[dict[str, Any]]:
        raise ConfigurationError("unknown_label", label="LEAKY-LABEL")

    app, repo = tool_app(monkeypatch, valid_env, ScriptedExecutor({"count_nodes": broken}))
    project = await owned_project(repo, "alice@example.com", "Alpha")
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_features", project_id=str(project.id))
    assert code(result) == "internal_error"
    assert "LEAKY-LABEL" not in text(result) and "unknown_label" not in text(result)
    assert_no_error_records(mcp_log)
    assert "LEAKY-LABEL" not in log_text(mcp_log, "api.mcp") + log_text(mcp_log, "mcp.server")


# --- FR-MCP-OPS-03: no session affinity -------------------------------------------------------------


async def test_fr_mcp_ops_03_initialize_on_one_instance_and_tools_call_on_another(monkeypatch, valid_env):
    """FR-MCP-OPS-03: two app instances behind one client; ``initialize`` on A, every other request on B."""

    first, repo = tool_app(monkeypatch, valid_env, features())
    second, _ = tool_app(monkeypatch, valid_env, features(), repo=repo)  # one Postgres behind both instances
    await owned_project(repo, "alice@example.com", "Alpha")
    router = MethodRouter(first, second)
    async with connected(router, ALICE, [first, second]) as session:
        result = await call(session, "list_projects")
        tools = await session.list_tools()
    assert [i["name"] for i in structured(result)["items"]] == ["Alpha"]
    assert {t.name for t in tools.tools} == {"list_projects", "list_features"}
    assert ("initialize", "first") in router.routed
    assert ("tools/call", "second") in router.routed
    assert all(side == "second" for method, side in router.routed if method != "initialize")


# --- FR-MCP-OPS-07: the audit event ----------------------------------------------------------------


AUDIT_FIELDS = {"tool", "outcome", "item_count", "duration_ms", "user_id"}


def audit(caplog: pytest.LogCaptureFixture) -> list[Any]:
    return [r for r in caplog.records if getattr(r, "event", "") == "mcp.tool.completed"]


async def test_fr_mcp_ops_07_three_calls_write_three_events_without_content(monkeypatch, valid_env, mcp_log):
    """FR-MCP-OPS-07: ``list_projects``, ``list_features`` (own) and ``list_features`` (bob's): three events with the
    fields, the last ``project_not_found``; no feature text, name, cursor or email in any record. A call the
    input schema refuses (``limit=0``) never reaches the wrapper and adds no event."""

    row = node_row("FEAT::SECRETSPEC", FEATURE, stmt="STMT-SECRET", quote="QUOTE-SECRET")
    executor = ScriptedExecutor({"read_nodes_page": lambda _: [row], "count_nodes": lambda _: [{"total": 1}]})
    app, repo = tool_app(monkeypatch, valid_env, executor)
    own = await owned_project(repo, "alice@example.com", "AuditedName")
    foreign = await owned_project(repo, "bob@example.com", "BobName")
    async with sdk_session(app, ALICE) as session:
        await call(session, "list_projects")
        ok = await call(session, "list_features", project_id=str(own.id))
        denied = await call(session, "list_features", project_id=str(foreign.id), cursor="CURSOR-SECRET")
        refused = await call(session, "list_features", project_id=str(own.id), limit=0)
    events = audit(mcp_log)
    assert [e.tool for e in events] == [
        "list_projects",
        "list_features",
        "list_features",
    ]
    assert [e.outcome for e in events] == ["ok", "ok", "project_not_found"]
    assert [e.item_count for e in events] == [1, 1, 0]
    assert all(set(vars(e)) >= AUDIT_FIELDS and isinstance(e.duration_ms, int) for e in events)
    user_ids = {e.user_id for e in events}
    assert user_ids == {str(repo.users["dev:alice@example.com"].id)}
    assert [getattr(e, "project_id", None) for e in events] == [
        None,
        str(own.id),
        str(foreign.id),
    ]
    assert not hasattr(events[0], "error_type") and events[2].error_type == "McpToolError"
    assert not ok.is_error and denied.is_error and refused.is_error
    everything = log_text(mcp_log, "api.mcp")
    for leaked in (
        "SECRETSPEC",
        "STMT-SECRET",
        "QUOTE-SECRET",
        "AuditedName",
        "BobName",
        "CURSOR-SECRET",
        "@example.com",
    ):
        assert leaked not in everything
    assert_no_error_records(mcp_log)


async def test_fr_mcp_ops_07_the_project_id_is_audited_only_when_valid_and_canonical(monkeypatch, valid_env, mcp_log):
    """FR-MCP-OPS-07: a malformed id (``RAW-MALFORMED-ID``) gives an event without ``project_id`` and the raw text is in
    no record; an upper-case spelling of the caller's own id is audited as the canonical lower-case string."""

    app, repo = tool_app(monkeypatch, valid_env, features())
    own = await owned_project(repo, "alice@example.com", "Alpha")
    async with sdk_session(app, ALICE) as session:
        malformed = await call(session, "list_features", project_id="RAW-MALFORMED-ID")
        upper = await call(session, "list_features", project_id=str(own.id).upper())
    assert code(malformed) == "project_not_found" and not upper.is_error
    first, second = audit(mcp_log)
    assert not hasattr(first, "project_id") and first.outcome == "project_not_found"
    assert second.project_id == str(own.id) and second.project_id == second.project_id.lower()
    everything = log_text(mcp_log, "api.mcp")
    assert "RAW-MALFORMED-ID" not in everything and str(own.id).upper() not in everything
    assert_no_error_records(mcp_log)


# --- Input-schema refusals: before any provisioning, membership or graph step (access audit row 5) ---------


@pytest.mark.parametrize(
    "arguments",
    [{"limit": 101}, {"project_id": 7}, {"cursor": 7}],
    ids=["limit-101", "int-project-id", "non-string-cursor"],
)
async def test_the_input_schema_refuses_bad_arguments_before_any_step_and_unaudited(
    monkeypatch, valid_env, mcp_log, arguments: dict[str, Any]
):
    """The SDK refuses ``limit=101``, an int ``project_id`` and a non-string ``cursor``: ``is_error``, zero provisioning
    (no user row), zero membership queries, zero graph queries, and no ``mcp.tool.completed`` event."""

    executor = features()
    app, repo = tool_app(monkeypatch, valid_env, executor)
    project = await owned_project(repo, "alice@example.com", "Alpha")
    repo.users.clear()  # owned_project provisioned alice; the refusal must not provision again
    membership: list[Any] = []

    async def counting(*args: Any) -> Any:
        membership.append(args)

    monkeypatch.setattr("api.mcp.access.repositories.get_member_project", counting)
    monkeypatch.setattr("api.mcp.access.repositories.upsert_user", counting)
    sent = {"project_id": str(project.id), **arguments}
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_features", **sent)
    assert result.is_error
    assert executor.calls == [] and membership == [] and repo.users == {}
    assert audit(mcp_log) == []


async def test_the_input_schema_refuses_limit_101_on_list_projects_before_any_step(monkeypatch, valid_env, mcp_log):
    """``list_projects`` with ``limit=101``: refused, no user row, no event."""

    app, repo = tool_app(monkeypatch, valid_env, features())
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_projects", limit=101)
    assert result.is_error and repo.users == {} and audit(mcp_log) == []


async def test_fr_mcp_ops_07_a_failure_is_audited_with_its_class_name_and_the_user_when_known(
    monkeypatch, valid_env, mcp_log
):
    """FR-MCP-OPS-07 / TOOL-08: a graph outage: outcome ``graph_unavailable``, ``error_type`` the class, no text."""

    def down(_: Any) -> list[dict[str, Any]]:
        raise InfrastructureUnavailableError("neo4j_unreachable", detail="DRIVER-TEXT")

    app, repo = tool_app(monkeypatch, valid_env, ScriptedExecutor({"count_nodes": down}))
    project = await owned_project(repo, "alice@example.com", "Alpha")
    async with sdk_session(app, ALICE) as session:
        await call(session, "list_features", project_id=str(project.id))
    (event,) = audit(mcp_log)
    assert (event.outcome, event.error_type, event.item_count) == (
        "graph_unavailable",
        "InfrastructureUnavailableError",
        0,
    )
    assert "DRIVER-TEXT" not in log_text(mcp_log, "api.mcp") + log_text(mcp_log, "mcp.server")
    assert_no_error_records(mcp_log)


async def test_fr_mcp_ops_07_a_database_failure_before_provisioning_has_no_user_id(monkeypatch, valid_env, mcp_log):
    """FR-MCP-OPS-07: provisioning failed, so the event carries no ``user_id``."""

    app, _ = tool_app(monkeypatch, valid_env, features())

    async def refused(*_: Any) -> None:
        raise ConnectionRefusedError("no route")

    monkeypatch.setattr("api.mcp.access.repositories.upsert_user", refused)
    async with sdk_session(app, ALICE) as session:
        await call(session, "list_projects")
    (event,) = audit(mcp_log)
    assert event.outcome == "database_unavailable" and not hasattr(event, "user_id")
