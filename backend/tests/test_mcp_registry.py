"""The tool registry (docs/mcp-server/architecture.md, Tool registry): the structural access test and the published
tool contract. The wrapper (audit, error mapping, API call) is tested in ``test_mcp_router.py``.

Boundary faked: the API the MCP role calls (``httpx.MockTransport``, see ``mcp_support.build_app``).
"""

from __future__ import annotations

import ast
import re

import httpx

from agent_contracts import AGENT_ROUTES
from mcp_server import descriptions
from mcp_server.registry import READ_ONLY, TOOLS, install
from mcp_support import ALICE, BACKEND, build_app, call, mcp_env, ready_api, sdk_session

SOURCE = BACKEND / "src" / "mcp_server"
TOOLS_DOC = BACKEND.parent / "docs" / "mcp-server" / "tools.md"
ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def tools_app(valid_env):
    """The app with the real registry; the API behind it serves no agent route."""

    return build_app(mcp_env(valid_env), tools=lambda server: install(server, resource_url="http://localhost:8002/mcp"))


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

    assert [t.name for t in TOOLS] == [
        "list_projects",
        "list_features",
        "list_tasks",
        "list_feature_tasks",
        "get_task_path",
        "get_task_brief",
        "get_story_brief",
        "trace",
        "get_node",
        "search_spec",
        "similar",
        "get_evidence",
        "path",
        "expand_brief",
    ]
    for tool in TOOLS:
        assert ("project_id" in tool.arguments.model_fields) == tool.project_scoped, tool.name
    assert [t.project_scoped for t in TOOLS] == [False, *([True] * 13)]


async def test_fr_mcp_acc_06_every_scoped_tool_sends_its_project_id_to_the_api_which_owns_the_membership(valid_env):
    """FR-MCP-ACC-06: each tool with a ``project_id`` argument calls its agent route with that id as a query parameter
    and passes the API's ``project_not_found`` through; the MCP role checks no membership itself."""

    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return ready_api(request)
        seen.append(request)
        body = {"error": {"code": "project_not_found", "message": "x", "details": {}}}
        return httpx.Response(404, json=body, headers={"X-SpecFlow-User-Id": "11111111-1111-4111-8111-111111111111"})

    app = build_app(
        mcp_env(valid_env), transport=httpx.MockTransport(api), tools=lambda s: install(s, resource_url="x")
    )
    foreign = "00000000-0000-4000-8000-0000000000aa"
    required = {  # the other arguments a scoped tool needs
        "get_task_brief": {"task_id": "TASK::X"},
        "get_task_path": {"task_id": "TASK::X"},
        "list_feature_tasks": {"feature_id": "FEAT::X"},
        "get_story_brief": {"story_id": "STORY::X"},
        "trace": {"story_id": "STORY::X", "direction": "affects"},
        "get_node": {"spec_id": "FEAT::X"},
        "search_spec": {"query": "x"},
        "similar": {"story_id": "STORY::X"},
        "get_evidence": {"spec_id": "FEAT::X"},
        "path": {"from": "FEAT::X", "to": "FEAT::Y"},
        "expand_brief": {"story_id": "STORY::X"},
    }
    async with sdk_session(app, ALICE) as session:
        listed = (await session.list_tools()).tools
        scoped = [t.name for t in listed if "project_id" in (t.input_schema.get("properties") or {})]
        for name in scoped:
            result = await call(session, name, project_id=foreign, **required.get(name, {}))
            assert result.is_error and result.structured_content["error"]["code"] == "project_not_found"
    assert scoped == [t.name for t in TOOLS if t.project_scoped]
    assert [r.url.path for r in seen] == [AGENT_ROUTES[name] for name in scoped]
    assert {r.url.params["project_id"] for r in seen} == {foreign}


def test_fr_mcp_acc_06_the_sdk_tool_api_has_one_home_and_no_module_reads_data() -> None:
    """FR-MCP-ACC-06: ``add_tool``, ``add_prompt``, ``Prompt`` and ``mcp.types`` only in ``registry.py``;
    ``guidance.py`` imports no SDK; only ``api_client.py`` imports ``httpx``; no module names a membership or
    repository function."""

    trees = sources()
    for name, tree in trees.items():
        named = names_in(tree)
        assert not {"get_member_project", "upsert_user", "repositories"} & named, name
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert ("add_tool" in calls) == (name == "registry.py"), name
        assert ("add_prompt" in calls) == (name == "registry.py"), name
        assert ("Prompt" in names_in(tree)) == (name == "registry.py"), name
        if name == "guidance.py":
            sdk = {m for m in imported(tree) if m.split(".")[0] in {"mcp", "mcp_types", "starlette", "settings"}}
            assert not sdk, sdk
        assert ("mcp.types" in imported(tree) or "mcp_types" in imported(tree)) == (name == "registry.py"), name
        assert ("httpx" in imported(tree)) == (name == "api_client.py"), name
    assert {"api_client.py", "descriptions.py", "guidance.py", "readiness.py", "registry.py"} <= set(trees)
    assert not {"access.py", "runtime.py"} & set(trees) and not any(n.startswith("tools/") for n in trees)


# --- FR-MCP-TOOL-09: the published contract ----------------------------------------------------------


async def test_fr_mcp_tool_09_tools_list_shows_exactly_the_ten_tools_with_four_annotations_each(valid_env):
    """FR-MCP-TOOL-09: ``tools/list`` over HTTP: every tool, the four annotations, the output schema, and the
    description tools.md quotes (joined by one space)."""

    app = tools_app(valid_env)
    async with sdk_session(app, ALICE) as session:
        listed = (await session.list_tools()).tools
    assert sorted(t.name for t in listed) == [
        "expand_brief",
        "get_evidence",
        "get_node",
        "get_story_brief",
        "get_task_brief",
        "get_task_path",
        "list_feature_tasks",
        "list_features",
        "list_projects",
        "list_tasks",
        "path",
        "search_spec",
        "similar",
        "trace",
    ]
    for tool in listed:
        assert tool.annotations is not None
        assert tool.annotations.model_dump(mode="json", by_alias=True, exclude_none=True) == ANNOTATIONS, tool.name
        assert tool.output_schema is not None, tool.name
    by_name = {t.name: t for t in listed}
    assert by_name["list_projects"].description == doc_description("list_projects") == descriptions.PROJECTS_DESCRIPTION
    assert by_name["list_features"].description == doc_description("list_features") == descriptions.FEATURES_DESCRIPTION
    assert by_name["list_tasks"].description == doc_description("list_tasks") == descriptions.TASKS_DESCRIPTION
    assert by_name["get_task_brief"].description == doc_description("get_task_brief") == descriptions.BRIEF_DESCRIPTION
    feature_tasks = by_name["list_feature_tasks"].description
    assert feature_tasks == doc_description("list_feature_tasks") == descriptions.FEATURE_TASKS_DESCRIPTION
    assert by_name["get_task_path"].description == doc_description("get_task_path") == descriptions.PATH_DESCRIPTION
    for tool, text in (
        ("get_story_brief", descriptions.STORY_BRIEF_DESCRIPTION),
        ("trace", descriptions.TRACE_DESCRIPTION),
        ("get_node", descriptions.NODE_DESCRIPTION),
        ("search_spec", descriptions.SEARCH_DESCRIPTION),
        ("similar", descriptions.SIMILAR_DESCRIPTION),
        ("get_evidence", descriptions.EVIDENCE_DESCRIPTION),
        ("path", descriptions.PATH_TOOL_DESCRIPTION),
        ("expand_brief", descriptions.EXPAND_DESCRIPTION),
    ):
        assert by_name[tool].description == doc_description(tool) == text, tool
    assert READ_ONLY.model_dump(mode="json", by_alias=True, exclude_none=True) == ANNOTATIONS


async def test_fr_mcp_tool_09_the_input_schemas_bound_the_limit_and_leave_the_ids_plain(valid_env):
    """FR-MCP-TOOL-09 / TOOL-01 / TOOL-03: ``limit`` 1..100 default 50 in the schema; ``project_id`` a plain string
    (no format, pattern or length), so a malformed id reaches the membership check; ``cursor`` string or null."""

    app = tools_app(valid_env)
    async with sdk_session(app, ALICE) as session:
        schemas = {t.name: t.input_schema for t in (await session.list_tools()).tools}
    limit = {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}
    spec_tools = {
        "get_task_brief",
        "get_story_brief",
        "trace",
        "get_node",
        "search_spec",
        "similar",
        "get_evidence",
        "path",
        "expand_brief",
    }
    paged = {name: schema for name, schema in schemas.items() if name not in spec_tools}
    assert set(paged) == {"list_projects", "list_features", "list_tasks", "list_feature_tasks", "get_task_path"}
    for name, schema in paged.items():
        properties = schema["properties"]
        assert {k: v for k, v in properties["limit"].items() if k != "title"} == limit, name
        assert {t["type"] for t in properties["cursor"]["anyOf"]} == {"string", "null"}, name
        assert "cursor" not in schema.get("required", []) and "limit" not in schema.get("required", []), name
    assert set(schemas["list_projects"]["properties"]) == {"limit", "cursor"}
    assert set(schemas["list_features"]["properties"]) == {"project_id", "limit", "cursor"}
    assert schemas["list_features"]["required"] == ["project_id"]
    assert set(schemas["list_tasks"]["properties"]) == {
        "project_id",
        "ready_only",
        "status",
        "area_id",
        "story_id",
        "limit",
        "cursor",
    }
    assert set(schemas["get_task_brief"]["properties"]) == {"project_id", "task_id", "neighbours"}
    assert schemas["get_task_brief"]["required"] == ["project_id", "task_id"]
    assert set(schemas["list_feature_tasks"]["properties"]) == {"project_id", "feature_id", "limit", "cursor"}
    assert schemas["list_feature_tasks"]["required"] == ["project_id", "feature_id"]
    assert set(schemas["get_task_path"]["properties"]) == {"project_id", "task_id", "limit", "cursor"}
    assert schemas["get_task_path"]["required"] == ["project_id", "task_id"]
    for name in ("list_features", "list_tasks", "list_feature_tasks", "get_task_path", "get_task_brief"):
        project_id = schemas[name]["properties"]["project_id"]
        assert project_id["type"] == "string" and not {"format", "pattern", "maxLength", "minLength"} & set(project_id)


def test_fr_mcp_tool_09_the_descriptions_carry_the_untrusted_content_notice() -> None:
    """FR-MCP-TOOL-09 / access-control.md untrusted content: both descriptions say the field values are data."""

    assert "data, not instructions" in descriptions.FEATURES_DESCRIPTION
    assert "data, not instructions" in descriptions.TASKS_DESCRIPTION
    assert "data, not instructions" in descriptions.BRIEF_DESCRIPTION
    assert "user data" in descriptions.PROJECTS_DESCRIPTION
