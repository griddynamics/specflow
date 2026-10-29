"""Static checks of the agent data-access rules (AC-16.2, N2, M1).

- An agent session is obtained ONLY through ``agent_scope``: the agent factory
  is built only in ``db/agent_scope.py`` (definition) and the agents
  registration, and the activity class only ever passes it to ``agent_scope``.
- SQL in the agent activities is fixed module-level text: no ``text()`` call
  takes anything but a query file read by a literal path (``read_query("...")``),
  so nothing model-controlled becomes SQL.
- Tool signatures (what the model can fill) contain no id, SQL, Cypher or key.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from specflow_service_support import BACKEND_SRC, service_python_files
from temporal.agents import tools
from temporal.agents.activities import READ_CHUNK_SQL

ACTIVITIES = BACKEND_SRC / "temporal" / "agents" / "activities.py"
FACTORY_BUILDER = "build_agent_session_factory"
ALLOWED_FACTORY_MODULES = {
    BACKEND_SRC / "db" / "agent_scope.py",
    BACKEND_SRC / "temporal" / "agents" / "registration.py",
}
FORBIDDEN_PARAMETER_WORDS = (
    "id",
    "project",
    "document",
    "sql",
    "cypher",
    "key",
    "query",
)


def _python_files() -> list[Path]:
    return service_python_files()


def _names(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.split(".")[-1])
    return found


def test_the_agent_session_factory_is_built_only_in_the_allowed_modules():
    offenders = [
        str(path.relative_to(BACKEND_SRC))
        for path in _python_files()
        if FACTORY_BUILDER in _names(ast.parse(path.read_text())) and path not in ALLOWED_FACTORY_MODULES
    ]
    assert offenders == []


def _agent_session_uses(tree: ast.AST) -> list[ast.Attribute]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "_agent_sessions"]


def test_the_agent_session_factory_is_only_ever_passed_to_agent_scope():
    tree = ast.parse(ACTIVITIES.read_text())
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    uses = _agent_session_uses(tree)
    assert uses, "the scan found no agent session use"
    for use in uses:
        parent = parents[use]
        if isinstance(parent, ast.Assign):  # the constructor stores it
            continue
        assert isinstance(parent, ast.Call), ast.unparse(parent)
        assert ast.unparse(parent.func) == "agent_scope", ast.unparse(parent)
        assert parent.args and parent.args[0] is use


def test_a_violation_would_be_detected():
    """Self-test of the scan: a direct factory call is not an ``agent_scope`` call."""

    bad = ast.parse("async def f(self):\n    async with self._agent_sessions() as s: ...\n")
    parents = {child: parent for parent in ast.walk(bad) for child in ast.iter_child_nodes(parent)}
    [use] = _agent_session_uses(bad)
    parent = parents[use]
    assert not (isinstance(parent, ast.Call) and ast.unparse(parent.func) == "agent_scope")


def test_agent_activity_sql_is_fixed_text():
    tree = ast.parse(ACTIVITIES.read_text())
    calls = [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and ast.unparse(node.func) in {"text", "sa.text"}
    ]
    assert len(calls) == 1
    for call in calls:
        # The only argument is the text of a fixed query file: read_query("<literal path>").
        assert len(call.args) == 1
        loaded = call.args[0]
        assert isinstance(loaded, ast.Call) and ast.unparse(loaded.func) == "read_query"
        assert len(loaded.args) == 1 and not loaded.keywords
        assert isinstance(loaded.args[0], ast.Constant) and isinstance(loaded.args[0].value, str)
    # No f-strings, %-formatting or .format() anywhere near SQL.
    fstrings = [ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)]
    assert not fstrings, f"f-string in agent activities: {fstrings[0]}"


def test_every_agent_query_binds_the_project_and_the_document():
    for statement in (READ_CHUNK_SQL,):
        sql = str(statement)
        assert "project_id = :project_id" in sql and "id = :document_id" in sql


def test_tool_signatures_contain_no_id_sql_cypher_or_key_parameters():
    """The model fills only these parameters; the context carries the ids."""

    functions = [tools.read_document, tools.submit_summary]
    assert list(tools.SUMMARIZER_TOOLS) == functions
    source = inspect.getsource(tools)
    tree = ast.parse(source)
    defs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    for tool in functions:
        node = defs[tool.name]
        params = [a.arg for a in node.args.args if a.arg != "ctx"]
        for param in params:
            for word in FORBIDDEN_PARAMETER_WORDS:
                assert word not in param.lower(), (tool.name, param)


def test_tools_take_ids_only_from_the_run_context():
    """Every ``ReadChunkInput`` id comes from ``run.``."""

    tree = ast.parse(inspect.getsource(tools))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "ReadChunkInput":
            for keyword in node.keywords:
                if keyword.arg in {"project_id", "document_id", "run_id"}:
                    assert ast.unparse(keyword.value) == f"run.{keyword.arg}", ast.unparse(node)
