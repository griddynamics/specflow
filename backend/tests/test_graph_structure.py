"""Structure rules of the graph library (plan 1.T6; docs/graph/architecture.md I1-I5, FR-CORE-07, NFR-SEC-02).

Static checks over every module of ``backend/src/graph/`` (AST; only the query modules are
imported), so they also hold for the submodules later milestones add.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import re
import sys
import typing
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import pytest

from graph.build import queries as build_queries
from graph.core import queries as core_queries
from graph.core.errors import ConformanceError
from graph.core.ontology import BUILT_IN_NAMES
from graph.core.queries import CORE_DDL, DdlStatement, NodeShape, Query
from graph_support import shipped_ontology

GRAPH_ROOT: Final = Path(__file__).resolve().parents[1] / "src" / "graph"

THIRD_PARTY_ALLOWED: Final = frozenset(
    {"graph", "logger", "pydantic", "yaml", "numpy", "neo4j", "openai", "graphiti_core"}
)
HOST_PACKAGES: Final = frozenset(
    {"api", "temporal", "settings", "health", "db", "storage", "drive", "llm", "agent_runtime", "file_parsers", "cli"}
)
# A package listed here may be imported only by these modules (paths relative to src/graph).
ONLY_IN: Final = {
    "openai": frozenset({"core/llm/gateway.py"}),
    "graphiti_core": frozenset({"build/graphiti.py"}),
    "httpx": frozenset(),
}
QUERY_MODULES: Final = frozenset({"core/queries.py", "build/queries.py"})
FILE_MODULES: Final = frozenset({"core/files.py", "build/files.py"})

# A string looks like Cypher when a clause keyword (any case) stands in a Cypher shape: followed by a
# pattern, a property access, a procedure, a schema noun or another clause. A keyword alone in prose
# ("match the name", "set of labels", "return the node") does not count.
CYPHER_SHAPE: Final = re.compile(
    r"\b(?:optional\s+)?match\s*\("
    r"|\b(?:merge|create)\s*\("
    r"|\b(?:create|drop)\s+(?:or\s+replace\s+)?(?:(?:vector|fulltext|range|text|point|lookup)\s+)?(?:index|constraint)\b"
    r"|\bshow\s+(?:constraints|indexes|indices|databases|procedures)\b"
    r"|\bdetach\s+delete\b"
    r"|\bdelete\s+[a-z_]\w*\s*(?:,|$|return\b)"
    r"|\bunwind\s+[$\[]"
    r"|\bcall\s+(?:db|dbms|apoc|gds|genai|tx)\.\w"
    r"|\bcall\s*(?:\([^)]*\)\s*)?\{"
    r"|\bset\s+[a-z_]\w*\s*(?:\.\w+\s*=|:\w|\+=)"
    r"|\bwhere\s+(?:not\s+)?[a-z_]\w*\.\w+\s*(?:=|<>|<|>|in\b|is\b|starts\b|ends\b|contains\b)"
    r"|\breturn\s+(?:distinct\s+)?(?:count|collect|labels|properties|type)\s*\("
    r"|\breturn\s+[a-z_]\w*(?:\.\w+)?\s+as\s+\w"
    r"|\byield\s+\w+(?:\s*,\s*\w+)*\s+(?:return|where)\b"
    r"|\bforeach\s*\("
    r"|\bon\s+(?:create|match)\s+set\b"
    r"|\[\s*\w*\s*:\s*\w+\s*(?:\{[^}]*\}\s*)?\]\s*->",
    re.IGNORECASE,
)
PROJECT_ID_REFERENCE: Final = re.compile(r"\$project_id\b")
ENV_NAMES: Final = frozenset({"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"})
WRITE_CALLS: Final = frozenset(
    {
        "write_text",
        "write_bytes",
        "fdopen",
        "mkstemp",
        "mkdtemp",
        "NamedTemporaryFile",
        "TemporaryFile",
        "unlink",
        "rmdir",
        "rmtree",
        "mkdir",
        "makedirs",
        "touch",
    }
)
WRITE_MODE: Final = re.compile(r"[wax+]")


@dataclass(frozen=True)
class Module:
    path: Path
    rel: str
    tree: ast.Module

    def where(self, node: ast.AST) -> str:
        return f"{self.rel}:{getattr(node, 'lineno', 0)}"


@lru_cache(maxsize=1)
def modules() -> tuple[Module, ...]:
    found = []
    for path in sorted(GRAPH_ROOT.rglob("*.py")):
        rel = path.relative_to(GRAPH_ROOT).as_posix()
        found.append(Module(path, rel, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))))
    return tuple(found)


def imports(module: Module) -> Iterator[tuple[ast.AST, str]]:
    """Every imported top-level package name; a relative import yields ``"."``."""

    for node in ast.walk(module.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            yield node, "." if node.level else (node.module or "").split(".")[0]


def string_constants(module: Module) -> Iterator[ast.Constant]:
    """String constants, f-string parts included, docstrings and bare string statements excluded."""

    bare = {
        id(node.value)
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in bare:
            yield node


def call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def test_graph_package_has_modules() -> None:
    rels = {module.rel for module in modules()}
    assert {"core/queries.py", "core/db.py", "core/writer.py", "core/reader.py"} <= rels


# I1, FR-CORE-07: imports


def test_i1_only_allowed_imports() -> None:
    allowed = set(sys.stdlib_module_names) | THIRD_PARTY_ALLOWED
    violations = [
        f"{module.where(node)} imports {name!r}"
        for module in modules()
        for node, name in imports(module)
        if name not in allowed
    ]
    assert violations == []


def test_fr_core_07_no_host_package_imports() -> None:
    violations = [
        f"{module.where(node)} imports host package {name!r}"
        for module in modules()
        for node, name in imports(module)
        if name in HOST_PACKAGES
    ]
    assert violations == []


def test_i2_openai_graphiti_and_httpx_confined() -> None:
    violations = [
        f"{module.where(node)} imports {name!r}"
        for module in modules()
        for node, name in imports(module)
        if name in ONLY_IN and module.rel not in ONLY_IN[name]
    ]
    assert violations == []


def test_i2_graphiti_never_in_core() -> None:
    violations = [
        module.where(node)
        for module in modules()
        if module.rel.startswith("core/")
        for node, name in imports(module)
        if name == "graphiti_core"
    ]
    assert violations == []


# I3, NFR-SEC-02: Cypher only in the query modules; scoped queries bind $project_id


def test_i3_cypher_only_in_query_modules() -> None:
    violations = [
        f"{module.where(node)} {node.value[:60]!r}"
        for module in modules()
        if module.rel not in QUERY_MODULES
        for node in string_constants(module)
        if CYPHER_SHAPE.search(node.value)
    ]
    assert violations == []


def test_the_query_modules_are_recognised_as_cypher() -> None:
    """The detector is not blind: most query files of the query modules look like Cypher."""

    for module in modules():
        if module.rel in QUERY_MODULES:
            files = sorted((module.path.parent / "queries").glob("*.cypher"))
            hits = [path.name for path in files if CYPHER_SHAPE.search(path.read_text(encoding="utf-8"))]
            assert hits, module.rel
            if module.rel.endswith("core/queries.py"):
                assert len(hits) > 20, module.rel


@pytest.mark.parametrize(
    "text",
    [
        "MATCH (n:SpecNode) RETURN n",
        "match (n) return n",
        "Optional Match (a)-[r]->(b)",
        "merge (n {k: $k})",
        "CREATE (a)-[:X]->(b)",
        "create index foo if not exists for (n:Bar) on (n.x)",
        "Create Vector Index v for (n:X) on (n.e)",
        "drop constraint graph_x",
        "show indexes yield name return name",
        "detach delete n",
        "with r delete r return count(*)",
        "unwind $rows as row",
        "call db.awaitIndexes(300)",
        "CALL { with x return x }",
        "set n.name = $name",
        "set n += $props",
        "SET n:Label",
        "with n where n.group_id = $p",
        "RETURN count(n) AS c",
        "return labels(n)",
        "return n.spec_id as id",
        "foreach (_ in [1] | create (a))",
        "on create set n.x = 1",
        "()-[r:PART_OF]->()",
        "-[x:FLAGS {group_id: $p}]->",
    ],
)
def test_cypher_shapes_are_detected_in_any_case(text: str) -> None:
    assert CYPHER_SHAPE.search(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Match the requirement to its module by name.",
        "a set of labels",
        "return the node unchanged",
        "Create or update the node",
        "the call class and the prompt name",
        "show the user where the problem is",
        "delete the node and its relationships",
        "node_label_mismatch",
        "graph.writer.relationships_moved",
        "{label} must match {pattern}",
        "where: the file and the key path",
        "Yield control to the loop",
        "drop the index first",
    ],
)
def test_prose_with_clause_words_is_not_cypher(text: str) -> None:
    assert CYPHER_SHAPE.search(text) is None, text


def query_aliases(module: Module) -> set[str]:
    """Local names bound to ``Query``: ``Query`` itself and every ``import ... Query as X`` alias."""

    names = {"Query"}
    for node in ast.walk(module.tree):
        if isinstance(node, ast.ImportFrom):
            names |= {alias.asname for alias in node.names if alias.name == "Query" and alias.asname}
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id in names:
            names |= {target.id for target in node.targets if isinstance(target, ast.Name)}
    return names


def builds_query(module: Module) -> Iterator[ast.Call]:
    """Calls that construct a ``Query``: by name, alias or attribute (``queries.Query(...)``)."""

    aliases = query_aliases(module)
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.Call):
            continue
        if (isinstance(node.func, ast.Name) and node.func.id in aliases) or (
            isinstance(node.func, ast.Attribute) and node.func.attr == "Query"
        ):
            yield node


def test_i3_query_objects_built_only_in_query_modules() -> None:
    violations = [
        module.where(node) for module in modules() if module.rel not in QUERY_MODULES for node in builds_query(module)
    ]
    assert violations == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from graph.core.queries import Query\nQuery('a', 'b')", 1),
        ("from graph.core.queries import Query as Q\nQ('a', 'b')", 1),
        ("from graph.core import Query as CypherText\nCypherText('a', 'b')", 1),
        ("from graph.core.queries import Query\nQ = Query\nQ('a', 'b')", 1),
        ("import graph.core.queries as queries\nqueries.Query('a', 'b')", 1),
        ("from graph.core.queries import PING\nPING.binds_project_id()", 0),
        ("from graph.core.queries import upsert_relationship\nupsert_relationship('X', a, b)", 0),
    ],
)
def test_query_construction_detection(source: str, expected: int) -> None:
    module = Module(Path("x.py"), "x.py", ast.parse(source))
    assert len(list(builds_query(module))) == expected


# Sample arguments for query factories, by parameter annotation. A factory with a parameter type not
# listed here fails the test: extend the table so its queries are checked too.
SAMPLES: Final[dict[Any, tuple[Any, ...]]] = {
    str: ("Sample",),
    bool: (True, False),
    int: (1,),
    tuple[str, ...]: (("group_id", "edge_key"), ("text",)),
    NodeShape: (NodeShape("SpecNode", "uuid", check_label=True), NodeShape("Document", "document_id")),
    DdlStatement: (DdlStatement("graph_spec_node_spec_id", CORE_DDL[0]),),
}


def query_modules() -> list[Any]:
    return [core_queries, build_queries]


def call_factory(factory: Any, parameters: Sequence[inspect.Parameter], arguments: Sequence[Any]) -> Any:
    """Call ``factory`` with one argument per parameter, keyword-only parameters by name."""

    pairs = list(zip(parameters, arguments, strict=True))
    positional = [value for parameter, value in pairs if parameter.kind is not inspect.Parameter.KEYWORD_ONLY]
    named = {parameter.name: value for parameter, value in pairs if parameter.kind is inspect.Parameter.KEYWORD_ONLY}
    return factory(*positional, **named)


def factory_outputs(module: Any) -> Iterator[tuple[str, Query]]:
    """Every ``Query`` constant of ``module`` and every output of its ``-> Query`` factories."""

    for name, value in vars(module).items():
        if isinstance(value, Query):
            yield name, value
    for name, value in vars(module).items():
        function = getattr(value, "__wrapped__", value)
        if not inspect.isfunction(function) or function.__module__ != module.__name__:
            continue
        hints = typing.get_type_hints(function)
        if hints.get("return") is not Query:
            continue
        parameters = [
            parameter
            for parameter in inspect.signature(function).parameters.values()
            if parameter.default is inspect.Parameter.empty
        ]
        choices = []
        for parameter in parameters:
            annotation = hints.get(parameter.name)
            assert annotation in SAMPLES, f"{module.__name__}.{name}: add samples for {annotation!r}"
            choices.append(SAMPLES[annotation])
        for arguments in itertools.product(*choices):
            yield f"{name}{arguments}", call_factory(value, parameters, arguments)


def test_nfr_sec_02_scoped_queries_bind_project_id() -> None:
    checked = 0
    violations = []
    for module in query_modules():
        for name, query in factory_outputs(module):
            checked += 1
            if query.project_scoped and not PROJECT_ID_REFERENCE.search(query.text):
                violations.append(f"{module.__name__}.{name} ({query.name})")
    assert violations == []
    assert checked > 20


UNSAFE_NAMES: Final = ("Bad name", "X) DETACH DELETE (n", "a`b", "", "1abc")


def factories(module: Any) -> Iterator[tuple[str, Any, list[inspect.Parameter], dict[str, Any]]]:
    for name, value in vars(module).items():
        function = getattr(value, "__wrapped__", value)
        if inspect.isfunction(function) and function.__module__ == module.__name__:
            hints = typing.get_type_hints(function)
            if hints.get("return") is Query:
                yield name, value, list(inspect.signature(function).parameters.values()), hints


@pytest.mark.parametrize(
    ("name", "text"),
    [
        *[(unsafe, CORE_DDL[0]) for unsafe in UNSAFE_NAMES],
        ("graph_spec_node_uuid", CORE_DDL[0]),  # the statement creates another object
        ("graph_x", "MATCH (n) DETACH DELETE n"),
        ("graph_x", "CREATE INDEX graph_x FOR (n:SpecNode) ON (n.group_id, n.x)"),  # not idempotent
    ],
)
def test_nfr_sec_02_a_ddl_statement_must_create_the_safe_name_it_carries(name: str, text: str) -> None:
    with pytest.raises(ConformanceError):
        DdlStatement(name, text)


def test_nfr_sec_02_factories_refuse_unsafe_names() -> None:
    """A name rendered into Cypher text must pass the safe-name pattern, whatever the caller checked."""

    checked = 0
    for module in query_modules():
        for name, factory, parameters, hints in factories(module):
            base = [SAMPLES[hints[parameter.name]][0] for parameter in parameters]
            for index, parameter in enumerate(parameters):
                if hints[parameter.name] not in (str, tuple[str, ...]):
                    continue
                for unsafe in UNSAFE_NAMES:
                    arguments = list(base)
                    arguments[index] = unsafe if hints[parameter.name] is str else ("group_id", unsafe)
                    checked += 1
                    try:
                        call_factory(factory, parameters, arguments)
                    except ConformanceError:
                        continue
                    pytest.fail(f"{module.__name__}.{name} accepted {parameter.name}={unsafe!r}")
    assert checked > 0


# I4: no ontology names in code


DATA_MODEL_VALUES = frozenset({"stated", "implied", "inferred"})


def ontology_literals() -> tuple[set[str], tuple[str, ...]]:
    """Exact names (labels, relations, id families, vocabulary values) and id prefixes of the shipped ontology."""

    ontology = shipped_ontology()
    names = set(ontology.labels) | set(ontology.relations)
    prefixes = []
    for node_type in ontology.node_types.values():
        names |= set(node_type.id_families)
        for spec in node_type.attributes.values():
            names |= set(spec.values)
        if node_type.id_prefix:
            prefixes.append(node_type.id_prefix)
    # Edge basis values are data-model vocabulary of the library (data-model.md "Edges"); two of them
    # also occur in the shipped ontology's source_status vocabulary.
    return names - set(BUILT_IN_NAMES) - DATA_MODEL_VALUES, tuple(prefixes)


def test_i4_no_ontology_names_in_code() -> None:
    names, prefixes = ontology_literals()
    assert {"Module", "PART_OF", "FR", "MOD::", "yes"} <= names | set(prefixes)
    violations = [
        f"{module.where(node)} {node.value!r}"
        for module in modules()
        for node in string_constants(module)
        if node.value in names or node.value.startswith(prefixes)
    ]
    assert violations == []


# I5: no print, no environment, disk writes only in the file modules


def test_i5_no_print() -> None:
    violations = [
        module.where(node)
        for module in modules()
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print"
    ]
    assert violations == []


def test_i5_fr_core_07_no_environment_access() -> None:
    violations = []
    for module in modules():
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Attribute) and node.attr in ENV_NAMES:
                violations.append(f"{module.where(node)} .{node.attr}")
            elif isinstance(node, ast.Name) and node.id in ENV_NAMES:
                violations.append(f"{module.where(node)} {node.id}")
            elif isinstance(node, ast.ImportFrom) and any(alias.name in ENV_NAMES for alias in node.names):
                violations.append(f"{module.where(node)} import from {node.module}")
            elif isinstance(node, ast.Constant) and node.value in {"environ", "getenv"}:
                violations.append(f"{module.where(node)} {node.value!r}")
    assert violations == []


def opens_for_writing(node: ast.Call) -> bool:
    """``open``/``.open`` with a write mode, or with a mode the test cannot read."""

    if call_name(node) != "open":
        return False
    is_builtin = isinstance(node.func, ast.Name)
    mode: ast.expr | None = None
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    position = 1 if is_builtin else 0
    if mode is None and len(node.args) > position:
        mode = node.args[position]
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return WRITE_MODE.search(mode.value) is not None
    return True


def test_i5_disk_writes_only_in_file_modules() -> None:
    violations = [
        f"{module.where(node)} {call_name(node)}"
        for module in modules()
        if module.rel not in FILE_MODULES
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Call) and (opens_for_writing(node) or call_name(node) in WRITE_CALLS)
    ]
    assert violations == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("open(p, 'w')", True),
        ("open(p, mode='ab')", True),
        ("open(p, 'r+')", True),
        ("open(p, m)", True),
        ("open(p)", False),
        ("open(p, 'rb')", False),
        ("path.open('w')", True),
        ("path.open()", False),
    ],
)
def test_write_mode_detection(source: str, *, expected: bool) -> None:
    node = ast.parse(source).body[0].value
    assert opens_for_writing(node) is expected
