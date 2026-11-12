"""Structure rules of the graph library."""

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
from typing import Any, Final, Literal

import pytest

from graph.build import queries as build_queries
from graph.core import queries as core_queries
from graph.core.errors import ConformanceError
from graph.core.ontology import BUILT_IN_NAMES, load_ontology
from graph.core.queries import CORE_DDL, DdlStatement, NodeShape, Query
from graph.gap import queries as gap_queries
from graph.plan import queries as plan_queries
from graph.plan.names import PLAN_LABELS, PLAN_RELATIONS, PlanRelation, plan_vocabularies
from graph.stores._bolt.cypher import render_cypher
from graph_support import shipped_ontology

GRAPH_ROOT: Final = Path(__file__).resolve().parents[1] / "src" / "graph"

# Packages any graph module may import; the vendor SDKs below are allowed only where ONLY_IN says.
THIRD_PARTY_ALLOWED: Final = frozenset(
    {
        "graph",
        "logger",
        "pydantic",
        "yaml",
        "numpy",
        "openai",
        "neo4j",
        "sqlalchemy",
        "asyncpg",
        "postgres",
        "httpx",
        "pyTigerGraph",
    }
)
HOST_PACKAGES: Final = frozenset(
    {"api", "temporal", "settings", "health", "db", "storage", "drive", "llm", "agent_runtime", "file_parsers", "cli"}
)
# A package listed here may be imported only by these modules (paths relative to src/graph); an entry
# that ends with "/" stands for every module of that adapter folder (design D3). One folder per SDK
# user: the Bolt layer and the Neo4j and ArcadeDB adapters speak Bolt, PostgreSQL has the SQL SDKs,
# ArcadeDB the HTTP client for its admin API, TigerGraph its own SDK.
ONLY_IN: Final = {
    "openai": frozenset({"core/llm/gateway.py"}),
    "neo4j": frozenset({"stores/_bolt/", "stores/neo4j/", "stores/arcadedb/"}),
    "sqlalchemy": frozenset({"stores/postgres/"}),
    "asyncpg": frozenset({"stores/postgres/"}),
    # the shared PostgreSQL access (src/postgres): engines and connection transactions, shared with the host's db
    "postgres": frozenset({"stores/postgres/"}),
    "httpx": frozenset({"stores/arcadedb/"}),
    "pyTigerGraph": frozenset({"stores/tigergraph/"}),
}
QUERY_MODULES: Final = frozenset({"core/queries.py", "build/queries.py", "plan/queries.py", "gap/queries.py"})
# The Cypher dialect renders the canonical files and builds the run-time parts of a statement; the ArcadeDB
# dialect also reads statements for the clauses it refuses a dynamic name in (its statements are files).
CYPHER_MODULES: Final = QUERY_MODULES | frozenset({"stores/_bolt/cypher.py", "stores/arcadedb/dialect.py"})
FILE_MODULES: Final = frozenset({"core/files.py", "build/files.py", "plan/files.py"})

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


# I1,: imports


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


def allowed_in(rel: str, entries: frozenset[str]) -> bool:
    """Whether the module path ``rel`` is one of ``entries`` (a file) or inside one (a folder ending in "/")."""

    return any(rel == entry or (entry.endswith("/") and rel.startswith(entry)) for entry in entries)


def test_i2_vendor_sdks_confined() -> None:
    violations = [
        f"{module.where(node)} imports {name!r}"
        for module in modules()
        for node, name in imports(module)
        if name in ONLY_IN and not allowed_in(module.rel, ONLY_IN[name])
    ]
    assert violations == []


@pytest.mark.parametrize(
    ("rel", "allowed"),
    [
        ("stores/_bolt/executor.py", True),
        ("stores/neo4j/store.py", True),
        ("stores/arcadedb/store.py", True),
        ("stores/neo4j_helpers.py", False),  # a folder entry never matches a sibling that shares its prefix
        ("stores/__init__.py", False),
        ("core/db.py", False),
    ],
)
def test_a_folder_entry_allows_only_its_own_modules(rel: str, *, allowed: bool) -> None:
    assert allowed_in(rel, ONLY_IN["neo4j"]) is allowed


def test_every_sdk_the_graph_may_import_is_confined() -> None:
    """A third-party package beyond the base set must have a place in ONLY_IN (no SDK is allowed everywhere)."""

    base = {"graph", "logger", "pydantic", "yaml", "numpy"}
    assert THIRD_PARTY_ALLOWED - base == set(ONLY_IN)


# I3,: Cypher only in the query modules; scoped queries bind $project_id


def test_i3_cypher_only_in_query_modules() -> None:
    violations = [
        f"{module.where(node)} {node.value[:60]!r}"
        for module in modules()
        if module.rel not in CYPHER_MODULES
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
    Literal[PlanRelation]: PLAN_RELATIONS,
}


def query_modules() -> list[Any]:
    return [core_queries, build_queries, plan_queries, gap_queries]


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
            if query.project_scoped and not PROJECT_ID_REFERENCE.search(render_cypher(query)):
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
    # Edge basis values are data-model vocabulary of the library; two of them
    # also occur in the shipped ontology's source_status vocabulary.
    return names - set(BUILT_IN_NAMES) - DATA_MODEL_VALUES, tuple(prefixes)


def plan_literals() -> tuple[set[str], set[str]]:
    """Ontology-owned names and the library-only values used beside them."""

    names = set(PLAN_LABELS) | set(PLAN_RELATIONS)
    for values in plan_vocabularies().values():
        names.update(values)
    library_only = {
        "first",
        "update",
        "replan",
        "spec_ambiguous",
        "spec_contradiction",
        "spec_missing",
        "environment",
        "other",
    }
    return names - DATA_MODEL_VALUES, library_only


def gap_literals() -> set[str]:
    """The names the gap ontology file declares: its labels, relations and vocabulary values, which ``graph.gap``
    may write as literals (gap.md "Rules", like the plan's)."""

    ontology = load_ontology()
    owned = {name for name in (*ontology.labels, *ontology.relations) if ontology.owner_of(name) == "gap"}
    for node_type in ontology.node_types.values():
        if ontology.owner_of(node_type.label) == "gap":
            for spec in node_type.attributes.values():
                owned.update(spec.values)
    return owned - DATA_MODEL_VALUES


def test_i4_no_ontology_names_in_code() -> None:
    names, prefixes = ontology_literals()
    plan_names, plan_library_values = plan_literals()
    own_gap = gap_literals()
    assert {"Module", "PART_OF", "FR", "MOD::", "yes"} <= names | set(prefixes)
    violations = [
        f"{module.where(node)} {node.value!r}"
        for module in modules()
        for node in string_constants(module)
        if (
            node.value in names
            and not (module.rel.startswith("plan/") and (node.value in plan_names | plan_library_values))
            and not (module.rel.startswith("gap/") and node.value in own_gap)
        )
        or node.value.startswith(prefixes)
    ]
    assert violations == []


def test_i4_gap_name_literals_are_declared_by_the_gap_ontology() -> None:
    own_gap = gap_literals()
    assert {"Finding", "ExternalRef", "ABOUT", "REFERS_TO", "open", "question", "commit", "file", "agent"} <= own_gap
    violations = [
        f"{module.where(node)} {node.value!r}"
        for module in modules()
        if module.rel.startswith("gap/")
        for node in string_constants(module)
        if node.value in ontology_literals()[0] and node.value not in own_gap
    ]
    assert violations == []


def test_i4_plan_name_literals_are_declared_by_the_plan_ontology() -> None:
    plan_names, plan_library_values = plan_literals()
    assert {"Plan", "Task", "IN_AREA", "WAITS_FOR", "pending", "spec_ambiguous"} <= (plan_names | plan_library_values)
    violations = [
        f"{module.where(node)} {node.value!r}"
        for module in modules()
        if module.rel.startswith("plan/")
        for node in string_constants(module)
        if not (node.value in plan_names or node.value in plan_library_values)
        and all(node.value not in values for values in plan_vocabularies().values())
        and _is_name_literal(node.value)
        and node.value.lower() not in _word_literals(module)
    ]
    assert violations == []


def test_i4_flags_a_spec_name_literal_in_a_plan_module_sample(tmp_path: Path) -> None:
    source = "X = 'PART_OF'\n"
    module = Module(tmp_path / "sample.py", "plan/sample.py", ast.parse(source))
    names, _ = ontology_literals()
    plan_names, _ = plan_literals()
    node = next(string_constants(module))
    assert node.value in names and node.value not in plan_names


def _word_literals(module: Module) -> set[str]:
    text = module.path.read_text(encoding="utf-8")
    return {word.lower() for word in re.findall(r'"([A-Za-z0-9_:-]+)"', text)}


def _is_name_literal(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_:-]+", value))


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
