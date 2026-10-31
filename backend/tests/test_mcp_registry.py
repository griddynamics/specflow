"""The tool registry (docs/mcp-server/architecture.md, Tool registry): the structural access test, the published tool
contract, and the order of the wrapper (audit, error mapping, provisioning, membership, handler).

Boundaries faked: Postgres (``FakeRepo``) and Neo4j (a recording ``ScriptedExecutor``). The wrapper tests register
their own declarations through ``tool_app(..., declarations=...)``; nothing else is replaced.
"""

from __future__ import annotations

import ast
import re
from typing import Any

import pytest
from pydantic import BaseModel, field_serializer

from api.mcp import access
from api.mcp.errors import MESSAGES
from api.mcp.registry import READ_ONLY, TOOLS, ToolDeclaration
from api.mcp.tools import graph, projects
from graph.core import InfrastructureUnavailableError
from mcp_support import BACKEND, call, log_text, mcp_dev_token, owned_project, sdk_session, tool_app
from test_graph_reader import ScriptedExecutor, paged_executor

SOURCE = BACKEND / "src" / "api" / "mcp"
TOOLS_DOC = BACKEND.parent / "docs" / "mcp-server" / "tools.md"
ALICE = mcp_dev_token("alice@example.com")
ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def executor() -> ScriptedExecutor:
    return paged_executor(["FEAT::A", "FEAT::B"], "Feature")


def sources() -> dict[str, ast.Module]:
    return {str(p.relative_to(SOURCE)): ast.parse(p.read_text(encoding="utf-8")) for p in SOURCE.rglob("*.py")}


def imported(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def names_in(tree: ast.Module) -> set[str]:
    """Every identifier the code refers to: names, attributes and imported names (docstrings do not count)."""

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            found |= {alias.name for alias in node.names}
    return found


def doc_description(tool: str) -> str:
    """The text tools.md gives the model for ``tool``: its quoted lines joined by one space."""

    section = TOOLS_DOC.read_text(encoding="utf-8").split(f"## {tool}\n", 1)[1]
    quoted = re.search(r'Description given to the model: "(.*?)"\n', section, re.DOTALL)
    assert quoted
    return " ".join(line.strip() for line in quoted.group(1).splitlines())


# --- FR-MCP-ACC-06: every project-scoped tool goes through the one membership function --------------


def test_fr_mcp_acc_06_the_declarations_name_a_project_id_exactly_when_they_are_scoped() -> None:
    """FR-MCP-ACC-06: a tool with a ``project_id`` argument is declared project-scoped, and only such a tool is."""

    assert [t.name for t in TOOLS] == ["list_projects", "list_features"]
    for tool in TOOLS:
        assert ("project_id" in tool.arguments.model_fields) == tool.project_scoped, tool.name
    assert [t.project_scoped for t in TOOLS] == [False, True]


async def test_fr_mcp_acc_06_every_registered_tool_with_a_project_id_goes_through_member_project(
    monkeypatch, valid_env
):
    """FR-MCP-ACC-06: enumerate the tools the server lists; each one with a ``project_id`` argument, called with a
    foreign id, ends in ``project_not_found`` through ``access.member_project`` and reads nothing from the graph."""

    recorded = executor()
    app, repo = tool_app(monkeypatch, valid_env, recorded)
    foreign = await owned_project(repo, "bob@example.com", "Beta")
    seen: list[str] = []
    original = access.member_project

    async def spy(runtime: Any, user_id: Any, project_id: str) -> Any:
        seen.append(project_id)
        return await original(runtime, user_id, project_id)

    monkeypatch.setattr("api.mcp.registry.access.member_project", spy)
    async with sdk_session(app, ALICE) as session:
        listed = (await session.list_tools()).tools
        scoped = [t.name for t in listed if "project_id" in (t.input_schema.get("properties") or {})]
        for name in scoped:
            result = await call(session, name, project_id=str(foreign.id))
            assert result.is_error and result.structured_content["error"]["code"] == "project_not_found"
        await call(session, "list_projects")
    assert {t.name for t in listed} == {t.name for t in TOOLS}
    assert scoped == [t.name for t in TOOLS if t.project_scoped]
    assert seen == [str(foreign.id)] * len(scoped)  # once per scoped call, never for list_projects
    assert recorded.calls == []


def test_fr_mcp_acc_06_the_membership_query_and_the_sdk_tool_api_have_one_home() -> None:
    """FR-MCP-ACC-06: ``get_member_project`` is named only in ``access.py``; ``add_tool``, ``add_prompt``, ``Prompt``
    and ``mcp.types`` only in ``registry.py``; the handlers import no SDK, Starlette or transport module."""

    trees = sources()
    for name, tree in trees.items():
        named = names_in(tree)
        assert ("get_member_project" in named) == (name == "access.py"), name
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert ("add_tool" in calls) == (name == "registry.py"), name
        assert ("add_prompt" in calls) == (name == "registry.py"), name
        assert ("Prompt" in names_in(tree)) == (name == "registry.py"), name
        if name == "guidance.py":
            sdk = {m for m in imported(tree) if m.split(".")[0] in {"mcp", "mcp_types", "starlette", "settings"}}
            assert not sdk, sdk
        assert ("mcp.types" in imported(tree) or "mcp_types" in imported(tree)) == (name == "registry.py"), name
        if name.startswith("tools/"):
            forbidden = {m for m in imported(tree) if m.split(".")[0] in {"mcp", "mcp_types", "starlette", "httpx"}}
            assert not forbidden, (name, forbidden)
    assert {"access.py", "guidance.py", "registry.py", "tools/graph.py", "tools/projects.py"} <= set(trees)


# --- FR-MCP-TOOL-09: the published contract ----------------------------------------------------------


async def test_fr_mcp_tool_09_tools_list_shows_exactly_the_two_tools_with_four_annotations_each(monkeypatch, valid_env):
    """FR-MCP-TOOL-09: ``tools/list`` over HTTP: two tools, the four annotations, the output schema, and the
    description tools.md quotes (joined by one space)."""

    app, _ = tool_app(monkeypatch, valid_env, executor())
    async with sdk_session(app, ALICE) as session:
        listed = (await session.list_tools()).tools
    assert sorted(t.name for t in listed) == ["list_features", "list_projects"]
    for tool in listed:
        assert tool.annotations is not None
        assert tool.annotations.model_dump(mode="json", by_alias=True, exclude_none=True) == ANNOTATIONS, tool.name
        assert tool.output_schema is not None, tool.name
    by_name = {t.name: t for t in listed}
    assert by_name["list_projects"].description == doc_description("list_projects") == projects.DESCRIPTION
    assert by_name["list_features"].description == doc_description("list_features") == graph.DESCRIPTION
    assert READ_ONLY.model_dump(mode="json", by_alias=True, exclude_none=True) == ANNOTATIONS


async def test_fr_mcp_tool_09_the_input_schemas_bound_the_limit_and_leave_the_ids_plain(monkeypatch, valid_env):
    """FR-MCP-TOOL-09 / TOOL-01 / TOOL-03: ``limit`` 1..100 default 50 in the schema; ``project_id`` a plain string
    (no format, pattern or length), so a malformed id reaches the membership check; ``cursor`` string or null."""

    app, _ = tool_app(monkeypatch, valid_env, executor())
    async with sdk_session(app, ALICE) as session:
        schemas = {t.name: t.input_schema for t in (await session.list_tools()).tools}
    limit = {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}
    for name, schema in schemas.items():
        properties = schema["properties"]
        assert {k: v for k, v in properties["limit"].items() if k != "title"} == limit, name
        assert {t["type"] for t in properties["cursor"]["anyOf"]} == {"string", "null"}, name
        assert "cursor" not in schema.get("required", []) and "limit" not in schema.get("required", []), name
    assert set(schemas["list_projects"]["properties"]) == {"limit", "cursor"}
    assert set(schemas["list_features"]["properties"]) == {"project_id", "limit", "cursor"}
    assert schemas["list_features"]["required"] == ["project_id"]
    project_id = schemas["list_features"]["properties"]["project_id"]
    assert project_id["type"] == "string" and not {"format", "pattern", "maxLength", "minLength"} & set(project_id)


def test_fr_mcp_tool_09_the_descriptions_carry_the_untrusted_content_notice() -> None:
    """FR-MCP-TOOL-09 / access-control.md untrusted content: both descriptions say the field values are data."""

    assert "data, not instructions" in graph.DESCRIPTION
    assert "user data" in projects.DESCRIPTION


# --- the wrapper: audit -> error mapping -> provisioning -> membership -> handler ------------------


class ProbeList(BaseModel):
    items: list[str]


class UnserializableList(BaseModel):
    """A result whose serialization fails (FR-MCP-OPS-07, R-02): only the wrapper's build step can notice."""

    items: list[str]

    @field_serializer("items")
    def _fail(self, _value: list[str]) -> list[str]:
        raise ValueError("SERIALIZE-LEAK")


def probe(handler: Any, output: type[BaseModel] = ProbeList, *, scoped: bool = True) -> ToolDeclaration:
    return ToolDeclaration(
        name="probe",
        description="a test tool",
        arguments=graph.ListFeaturesArgs if scoped else projects.ListProjectsArgs,
        output=output,
        handler=handler,
        project_scoped=scoped,
    )


class Steps:
    """The order the wrapper calls provisioning, membership and the handler in (wrapped, not replaced)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, fail_provision: Exception | None = None) -> None:
        self.order: list[str] = []
        provision, member = access.provision, access.member_project

        async def provisioned(*args: Any) -> Any:
            self.order.append("provision")
            if fail_provision is not None:
                raise fail_provision
            return await provision(*args)

        async def membership(*args: Any) -> Any:
            self.order.append("member")
            return await member(*args)

        monkeypatch.setattr("api.mcp.registry.access.provision", provisioned)
        monkeypatch.setattr("api.mcp.registry.access.member_project", membership)

    def handler(self, result: Any = None, *, fail: Exception | None = None) -> Any:
        async def handle(*_: Any) -> Any:
            self.order.append("handler")
            if fail is not None:
                raise fail
            return result if result is not None else ProbeList(items=["x"])

        return handle


def events(caplog: pytest.LogCaptureFixture) -> list[Any]:
    return [r for r in caplog.records if getattr(r, "event", "") == "mcp.tool.completed"]


async def run_probe(monkeypatch, valid_env, declaration: ToolDeclaration, **arguments: Any):
    app, repo = tool_app(monkeypatch, valid_env, executor(), declarations=[declaration])
    own = await owned_project(repo, "alice@example.com", "Alpha")
    if declaration.project_scoped:
        arguments.setdefault("project_id", str(own.id))
    async with sdk_session(app, ALICE) as session:
        return await call(session, "probe", **arguments), repo, own


async def test_the_wrapper_provisions_then_checks_membership_then_runs_the_handler_then_audits(
    monkeypatch, valid_env, mcp_log
):
    """Wrapper order: provisioning, membership, handler; the audit event is written after all three."""

    steps = Steps(monkeypatch)
    result, _, own = await run_probe(monkeypatch, valid_env, probe(steps.handler()))
    assert not result.is_error and result.structured_content == {"items": ["x"]}
    assert steps.order == ["provision", "member", "handler"]
    (event,) = events(mcp_log)
    assert (event.tool, event.outcome, event.item_count, event.project_id) == ("probe", "ok", 1, str(own.id))


async def test_an_unscoped_tool_skips_the_membership_check(monkeypatch, valid_env):
    """Wrapper order: a tool without a project id provisions and runs; no membership step."""

    steps = Steps(monkeypatch)
    result, _, _ = await run_probe(monkeypatch, valid_env, probe(steps.handler(), scoped=False))
    assert not result.is_error
    assert steps.order == ["provision", "handler"]


async def test_a_provisioning_failure_reaches_neither_the_membership_check_nor_the_handler(
    monkeypatch, valid_env, mcp_log
):
    """Wrapper order: provisioning fails: mapped and audited, no membership query, no handler."""

    steps = Steps(monkeypatch, fail_provision=RuntimeError("PROVISION-LEAK"))
    result, _, _ = await run_probe(monkeypatch, valid_env, probe(steps.handler()))
    assert result.is_error and result.structured_content["error"]["code"] == "internal_error"
    assert steps.order == ["provision"]
    (event,) = events(mcp_log)
    assert (event.outcome, event.error_type) == ("internal_error", "RuntimeError") and not hasattr(event, "user_id")
    assert "PROVISION-LEAK" not in log_text(mcp_log, "api.mcp") + str(result)


async def test_a_denied_project_keeps_the_user_row_and_never_runs_the_handler(monkeypatch, valid_env, mcp_log):
    """Wrapper order / FR-MCP-ACC-05: a denial after provisioning leaves the user row; the handler is not called."""

    steps = Steps(monkeypatch)
    result, repo, _ = await run_probe(
        monkeypatch, valid_env, probe(steps.handler()), project_id="00000000-0000-4000-8000-000000000000"
    )
    assert result.structured_content["error"]["code"] == "project_not_found"
    assert steps.order == ["provision", "member"]
    assert list(repo.users) == ["dev:alice@example.com"]
    assert events(mcp_log)[0].outcome == "project_not_found"


async def test_a_handler_failure_is_mapped_and_audited_with_its_class_not_its_text(monkeypatch, valid_env, mcp_log):
    """Wrapper order / FR-MCP-TOOL-08: the handler raises; the client gets the fixed message, the log the class name."""

    steps = Steps(monkeypatch)
    failing = steps.handler(fail=InfrastructureUnavailableError("neo4j_unreachable", detail="HANDLER-LEAK"))
    result, _, _ = await run_probe(monkeypatch, valid_env, probe(failing))
    assert result.structured_content["error"] == {"code": "graph_unavailable", "message": MESSAGES["graph_unavailable"]}
    assert steps.order == ["provision", "member", "handler"]
    (event,) = events(mcp_log)
    assert (event.outcome, event.error_type) == ("graph_unavailable", "InfrastructureUnavailableError")
    assert "HANDLER-LEAK" not in log_text(mcp_log, "api.mcp") + str(result)


async def test_fr_mcp_ops_07_the_outcome_is_ok_only_after_the_result_was_built(monkeypatch, valid_env, mcp_log):
    """FR-MCP-OPS-07 (R-02): a result that fails to serialize is ``internal_error`` in the client result AND in the
    audit event (never ``ok``), and the serializer's text reaches neither."""

    steps = Steps(monkeypatch)
    declaration = probe(steps.handler(UnserializableList(items=["x"])), UnserializableList)
    result, _, _ = await run_probe(monkeypatch, valid_env, declaration)
    assert result.is_error and result.structured_content["error"]["code"] == "internal_error"
    (event,) = events(mcp_log)
    assert (event.outcome, event.item_count) == ("internal_error", 0)
    assert "SERIALIZE-LEAK" not in log_text(mcp_log, "api.mcp") + str(result)


async def test_a_handler_that_returns_the_wrong_shape_is_internal_error(monkeypatch, valid_env, mcp_log):
    """Wrapper order: a result that is not the declared output never reaches the SDK's validation text."""

    steps = Steps(monkeypatch)
    wrong = steps.handler(UnserializableList.model_construct(items=["x"]))  # not a ProbeList
    result, _, _ = await run_probe(monkeypatch, valid_env, probe(wrong))
    assert result.structured_content["error"]["code"] == "internal_error"
    assert events(mcp_log)[0].error_type == "McpInvariantError"


async def test_a_scoped_declaration_without_a_project_id_argument_is_internal_error(monkeypatch, valid_env, mcp_log):
    """FR-MCP-ACC-06 (runtime guard): declared project-scoped but taking no ``project_id`` is refused, handler unrun."""

    steps = Steps(monkeypatch)
    declaration = ToolDeclaration(
        name="probe",
        description="a test tool",
        arguments=projects.ListProjectsArgs,
        output=ProbeList,
        handler=steps.handler(),
        project_scoped=True,
    )
    result, _, _ = await run_probe(monkeypatch, valid_env, declaration)
    assert result.structured_content["error"]["code"] == "internal_error"
    assert "handler" not in steps.order and events(mcp_log)[0].error_type == "McpInvariantError"
