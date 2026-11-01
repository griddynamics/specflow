"""The PostgreSQL dialect: one ``.sql`` file per template of the query catalog (design D2, D7).

Every core template of ``graph.core.queries`` is a file under ``queries/core/`` (a template id
``core.<file stem>``); a template whose relationship type, node shape, hop count or vector size is only
known at run time is a file with ``/*__NAME__*/default/*__NAME__*/`` slots that the renderer fills from the
query's semantic arguments. Labels and relationship types are bind values (``:rel_type``, ``:foreign_label``,
``:types``), never part of the text; only integers (hop counts, the vector size) and the choice of a node
match fragment are rendered, after validation.

The dialect also

- renames the schema: the files say ``graph.`` and the dialect writes the configured schema name,
- converts the parameters the library passes to what the statements bind (a mapping is JSON text, a vector
  is the pgvector text form, the Lucene search string is the term list of design D6),
- puts the transaction-local settings of a vector query in front of it (``plan_cache_mode``, design risk
  1 of the spike: after five runs a prepared statement falls back to a generic plan that cannot use the
  partial HNSW index),
- holds the ``:project_id`` guard: a scoped statement that does not bind the parameter is refused.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from graph.core.dialect import ExecutionPlan, PlannedStatement, Record
from graph.core.errors import ConformanceError
from graph.core.queries import (
    LITERAL_TEMPLATE,
    ForeignLabel,
    LabelKey,
    LiteralText,
    NodeShape,
    Query,
    Relation,
    RelationEnds,
    ScopeSequence,
    ScopeVia,
    safe_name,
)
from graph.core.query_files import render_query
from graph.stores.postgres.sqlfiles import load_core_sql

DEFAULT_SCHEMA: Final = "graph"
BACKEND: Final = "postgres"
PROJECT_ID: Final = "project_id"
SCHEMA_PARAMETER: Final = "schema"
SCHEMA_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
MAX_HOPS: Final = 32
MAX_VECTOR_DIMENSIONS: Final = 16000
# The BM25 constants of the Lucene similarity (design D6); the files carry them as slots.
BM25_K: Final = "1.2"
BM25_B: Final = "0.75"
# Settings of a vector query (transaction local). The ef_search is the page size k within these bounds.
PLAN_CACHE_MODE: Final = "force_custom_plan"
ITERATIVE_SCAN: Final = "relaxed_order"
MIN_EF_SEARCH: Final = 40
MAX_EF_SEARCH: Final = 1000
MAX_SCAN_TUPLES: Final = "40000"

_PARAMETER: Final = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
_SCHEMA_PREFIX: Final = re.compile(r'(?<![\w."])graph\.(?=[a-z_])|(?<=SCHEMA IF NOT EXISTS )graph\b')
_PROJECT_ID_REFERENCE: Final = re.compile(r"(?<![:\w]):project_id\b")

# Core templates that are one fixed statement: template id -> loader of its file.
_STATIC: Final[Mapping[str, Callable[[], str]]] = {
    "core.await_indexes": lambda: load_core_sql("await_indexes.sql"),
    "core.count_search_vectors": lambda: load_core_sql("count_search_vectors.sql"),
    "core.delete_spec_node": lambda: load_core_sql("delete_spec_node.sql"),
    "core.move_foreign_relationships": lambda: load_core_sql("move_foreign_relationships.sql"),
    "core.ping": lambda: load_core_sql("ping.sql"),
    "core.read_documents": lambda: load_core_sql("read_documents.sql"),
    "core.read_edges": lambda: load_core_sql("read_edges.sql"),
    "core.read_fragment_hits": lambda: load_core_sql("read_fragment_hits.sql"),
    "core.read_fragments": lambda: load_core_sql("read_fragments.sql"),
    "core.read_impact_hop": lambda: load_core_sql("read_impact_hop.sql"),
    "core.read_neighbours": lambda: load_core_sql("read_neighbours.sql"),
    "core.read_node_by_alias": lambda: load_core_sql("read_node_by_alias.sql"),
    "core.read_node_by_spec_id": lambda: load_core_sql("read_node_by_spec_id.sql"),
    "core.read_node_uuids_by_spec_id": lambda: load_core_sql("read_node_uuids_by_spec_id.sql"),
    "core.read_nodes": lambda: load_core_sql("read_nodes.sql"),
    "core.read_nodes_by_ids": lambda: load_core_sql("read_nodes_by_ids.sql"),
    "core.read_nodes_by_uuid": lambda: load_core_sql("read_nodes_by_uuid.sql"),
    "core.read_nodes_for_lines": lambda: load_core_sql("read_nodes_for_lines.sql"),
    "core.search_fragments": lambda: load_core_sql("search_fragments.sql"),
    "core.search_nodes_fulltext": lambda: load_core_sql("search_nodes_fulltext.sql"),
    "core.search_nodes_vector": lambda: load_core_sql("search_nodes_vector.sql"),
    "core.search_nodes_vector_exact": lambda: load_core_sql("search_nodes_vector_exact.sql"),
    "core.server_components": lambda: load_core_sql("server_components.sql"),
    "core.show_constraints": lambda: load_core_sql("show_constraints.sql"),
    "core.show_indexes": lambda: load_core_sql("show_indexes.sql"),
    "core.spec_node_exists": lambda: load_core_sql("spec_node_exists.sql"),
    "core.spec_node_relationship_types": lambda: load_core_sql("spec_node_relationship_types.sql"),
    "core.upsert_document": lambda: load_core_sql("upsert_document.sql"),
    "core.upsert_fragment": lambda: load_core_sql("upsert_fragment.sql"),
    "core.upsert_spec_node": lambda: load_core_sql("upsert_spec_node.sql"),
}
# Fragment and helper files that are not templates of their own.
FRAGMENT_FILES: Final = frozenset(
    {
        "match_spec_node.sql",
        "match_spec_node_unchecked.sql",
        "match_document_node.sql",
        "match_fragment_node.sql",
        "match_foreign_node.sql",
        "vector_settings.sql",
        "plan_settings.sql",
    }
)
# Templates the dialect runs with a custom plan cache mode (a vector search).
_CUSTOM_PLAN: Final = frozenset({"core.search_nodes_vector", "core.search_nodes_vector_exact"})
_FULLTEXT: Final = frozenset({"core.search_nodes_fulltext", "core.search_fragments"})
_SPEC_NODE: Final = "SpecNode"
_DOCUMENT: Final = "Document"
_FRAGMENT: Final = "Fragment"
_UUID: Final = "uuid"


def templates() -> frozenset[str]:
    """Every template id the dialect plans (the keys of ``_STATIC`` and ``_RENDERERS``)."""

    return frozenset({*_STATIC, *_RENDERERS})


@dataclass(frozen=True)
class _Rendered:
    text: str
    extra: Mapping[str, Any] = field(default_factory=dict)
    setup: tuple[str, Mapping[str, Any]] | None = None  # transaction-local settings statement and its parameters


def _unsafe(template: str) -> ConformanceError:
    return ConformanceError("query_arguments_invalid", template=template)


def _expect(args: Any, kind: type, template: str) -> Any:
    if not isinstance(args, kind):
        raise _unsafe(template)
    return args


_FRAGMENTS: Final[Mapping[str, Callable[[], str]]] = {
    "spec": lambda: load_core_sql("match_spec_node.sql"),
    "spec_unchecked": lambda: load_core_sql("match_spec_node_unchecked.sql"),
    "document": lambda: load_core_sql("match_document_node.sql"),
    "fragment": lambda: load_core_sql("match_fragment_node.sql"),
    "foreign": lambda: load_core_sql("match_foreign_node.sql"),
}


def _match_fragment(label: str, key: str, template: str, *, check_label: bool) -> tuple[str, bool]:
    """The fragment that finds a node by its label and key property, and whether it binds the label."""

    if label == _SPEC_NODE and key == _UUID:
        return ("spec" if check_label else "spec_unchecked"), False
    if label == _DOCUMENT and key == "document_id":
        return "document", False
    if label == _FRAGMENT and key == "fragment_id":
        return "fragment", False
    if label not in (_SPEC_NODE, _DOCUMENT, _FRAGMENT) and key == _UUID:
        return "foreign", True
    raise _unsafe(template)


def _node_match(
    label: str, key: str, *, check_label: bool, key_parameter: str, label_parameter: str, template: str
) -> tuple[str, dict[str, Any]]:
    """The statement that selects the ids of one node, and the parameters it needs beyond its key."""

    safe_label, safe_key = safe_name(label), safe_name(key)
    name, foreign = _match_fragment(safe_label, safe_key, template, check_label=check_label)
    text = render_query(
        _FRAGMENTS[name](),
        key=key_parameter,
        label=label_parameter,
        shape=f"{key_parameter}_shape",
    )
    extra: dict[str, Any] = {f"{key_parameter}_shape": safe_label} if foreign else {}
    return text, extra


def _shape_match(shape: NodeShape, var: str, template: str) -> tuple[str, dict[str, Any]]:
    checked = shape.checked()
    return _node_match(
        checked.label,
        checked.key,
        check_label=checked.check_label,
        key_parameter=f"{var}_key",
        label_parameter=f"{var}_label",
        template=template,
    )


def _upsert_relationship(args: Any) -> _Rendered:
    ends = _expect(args, RelationEnds, "core.upsert_relationship")
    source, source_extra = _shape_match(ends.source, "s", "core.upsert_relationship")
    target, target_extra = _shape_match(ends.target, "t", "core.upsert_relationship")
    text = render_query(load_core_sql("upsert_relationship.sql"), source=source, target=target)
    return _Rendered(text, {"rel_type": safe_name(ends.type), **source_extra, **target_extra})


def _delete_relationship(args: Any) -> _Rendered:
    relation = _expect(args, Relation, "core.delete_relationship")
    return _Rendered(load_core_sql("delete_relationship.sql"), {"rel_type": safe_name(relation.type)})


def _upsert_foreign_node(args: Any) -> _Rendered:
    foreign = _expect(args, ForeignLabel, "core.upsert_foreign_node")
    return _Rendered(load_core_sql("upsert_foreign_node.sql"), {"foreign_label": safe_name(foreign.label)})


def _keyed(load: Callable[[], str], key_parameter: str, template: str) -> Callable[[Any], _Rendered]:
    def render(args: Any) -> _Rendered:
        keyed = _expect(args, LabelKey, template)
        node, extra = _node_match(
            keyed.label,
            keyed.key,
            check_label=False,
            key_parameter=key_parameter,
            label_parameter=f"{key_parameter}_label",
            template=template,
        )
        return _Rendered(render_query(load(), node=node), extra)

    return render


def _hops(value: Any, template: str) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_HOPS:
        raise _unsafe(template)
    return str(value)


def _scope_via(args: Any) -> _Rendered:
    via = _expect(args, ScopeVia, "core.scope_via")
    low, high = _hops(via.min_hops, "core.scope_via"), _hops(via.max_hops, "core.scope_via")
    if int(low) > int(high):
        raise _unsafe("core.scope_via")
    text = render_query(load_core_sql("scope_via.sql"), min_hops=low, max_hops=high)
    return _Rendered(text, {"types": [safe_name(name) for name in via.relations]})


def _scope_sequence(args: Any) -> _Rendered:
    sequence = _expect(args, ScopeSequence, "core.scope_sequence")
    relations = [safe_name(name) for name in sequence.relations]
    either = {safe_name(name) for name in sequence.symmetric}
    if not relations:
        raise _unsafe("core.scope_sequence")
    extra = {"types": relations, "either_way": [name in either for name in relations]}
    return _Rendered(load_core_sql("scope_sequence.sql"), extra)


_RENDERERS: Final[Mapping[str, Callable[[Any], _Rendered]]] = {
    "core.upsert_relationship": _upsert_relationship,
    "core.delete_relationship": _delete_relationship,
    "core.upsert_foreign_node": _upsert_foreign_node,
    "core.delete_keyed_node": _keyed(lambda: load_core_sql("delete_keyed_node.sql"), "key", "core.delete_keyed_node"),
    "core.keyed_node_relationship_types": _keyed(
        lambda: load_core_sql("keyed_node_relationship_types.sql"), "n_key", "core.keyed_node_relationship_types"
    ),
    "core.scope_via": _scope_via,
    "core.scope_sequence": _scope_sequence,
}


def vector_text(vector: Sequence[float]) -> str:
    """The pgvector text form of ``vector`` (``[0.1,0.2]``)."""

    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def json_text(value: Any) -> str:
    """The JSON text of a mapping or list parameter (a ``jsonb`` cast on the statement side)."""

    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def parameter_value(name: str, value: Any) -> Any:
    """What the statement binds for the library's parameter ``name``."""

    if name == "vector" and isinstance(value, (list, tuple)):
        return vector_text(value)
    if isinstance(value, Mapping):
        return json_text(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list) and any(isinstance(item, Mapping) for item in value):
        return json_text(value)
    return value


def schema_identifier(name: str) -> str:
    """``name`` as a schema identifier of statement text; ``ConformanceError`` when it is not a plain name."""

    if not SCHEMA_NAME.fullmatch(name):
        raise ConformanceError("schema_name_unsafe")
    return name if name == name.lower() else f'"{name}"'


def render_schema(text: str, schema: str) -> str:
    """``text`` with the default schema prefix ``graph.`` written as ``schema`` (unchanged for ``graph``)."""

    if schema == DEFAULT_SCHEMA:
        return text
    identifier = schema_identifier(schema)
    return _SCHEMA_PREFIX.sub(lambda found: identifier if found[0] == DEFAULT_SCHEMA else f"{identifier}.", text)


def binds_project_id(text: str) -> bool:
    """Whether a statement text references the ``:project_id`` parameter."""

    return _PROJECT_ID_REFERENCE.search(text) is not None


def referenced_parameters(text: str) -> tuple[str, ...]:
    """The parameter names a statement text binds, in order of first use."""

    return tuple(dict.fromkeys(_PARAMETER.findall(text)))


class PostgresDialect:
    """Plans every query as the statements of its ``.sql`` file in schema ``schema``."""

    name: Final = BACKEND

    def __init__(self, schema: str = DEFAULT_SCHEMA) -> None:
        schema_identifier(schema)
        self.schema = schema

    def render(self, query: Query, params: Mapping[str, Any]) -> str:
        """The statement text of ``query`` (schema applied, no parameters): what tests compare."""

        return render_schema(self._rendered(query, params).text, self.schema)

    def plan(self, query: Query, params: Mapping[str, Any]) -> ExecutionPlan:
        rendered = self._rendered(query, params)
        text = render_schema(rendered.text, self.schema)
        if query.project_scoped and not binds_project_id(text):
            raise ConformanceError("query_missing_project_id", query=query.name)
        available = {**params, **rendered.extra, SCHEMA_PARAMETER: self.schema}
        statements = [PlannedStatement(text, self._bind(query, text, available))]
        if rendered.setup is not None:
            setup_text, setup_params = rendered.setup
            statements.insert(0, PlannedStatement(setup_text, self._bind(query, setup_text, setup_params)))
        return ExecutionPlan(tuple(statements), _last)

    def _rendered(self, query: Query, params: Mapping[str, Any]) -> _Rendered:
        template = query.template
        if template in _STATIC:
            rendered = _Rendered(_STATIC[template]())
        elif template == LITERAL_TEMPLATE:
            # A literal query is one fixed statement (tests and probes); here it is SQL, and the guard
            # below still requires a scoped one to bind :project_id.
            rendered = _Rendered(_expect(query.args, LiteralText, template).text)
        else:
            renderer = _RENDERERS.get(template)
            if renderer is None:
                raise ConformanceError("query_template_unknown", template=template, store=BACKEND)
            rendered = renderer(query.args)
        if template in _FULLTEXT:
            rendered = self._fulltext(rendered, params)
        elif template == "core.search_nodes_vector":
            rendered = self._vector(rendered, params)
        elif template == "core.search_nodes_vector_exact":
            rendered = _with_setup(rendered, load_core_sql("plan_settings.sql"), {"plan_cache_mode": PLAN_CACHE_MODE})
        return rendered

    @staticmethod
    def _fulltext(rendered: _Rendered, params: Mapping[str, Any]) -> _Rendered:
        """The BM25 constants into the text; the term list of design D6 from the Lucene search string."""

        text = render_query(rendered.text, bm25_k=BM25_K, bm25_b=BM25_B)
        extra = dict(rendered.extra)
        if "terms" not in params:
            query_text = params.get("query")
            if not isinstance(query_text, str):
                raise ConformanceError("query_parameter_missing", parameter="query")
            extra["terms"] = query_text.split()
        return _Rendered(text, extra, rendered.setup)

    @staticmethod
    def _vector(rendered: _Rendered, params: Mapping[str, Any]) -> _Rendered:
        """The vector size into the text and the HNSW settings of the page size ``k`` before it."""

        vector = params.get("vector")
        if not isinstance(vector, (list, tuple)) or not 1 <= len(vector) <= MAX_VECTOR_DIMENSIONS:
            raise ConformanceError("query_parameter_invalid", parameter="vector")
        k = params.get("k")
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise ConformanceError("query_parameter_invalid", parameter="k")
        text = render_query(rendered.text, dim=str(len(vector)))
        settings = {
            "plan_cache_mode": PLAN_CACHE_MODE,
            "iterative_scan": ITERATIVE_SCAN,
            "ef_search": str(min(MAX_EF_SEARCH, max(MIN_EF_SEARCH, k))),
            "max_scan_tuples": MAX_SCAN_TUPLES,
        }
        return _with_setup(_Rendered(text, rendered.extra), load_core_sql("vector_settings.sql"), settings)

    @staticmethod
    def _bind(query: Query, text: str, available: Mapping[str, Any]) -> dict[str, Any]:
        bound: dict[str, Any] = {}
        for name in referenced_parameters(text):
            if name not in available:
                raise ConformanceError("query_parameter_missing", query=query.name, parameter=name)
            bound[name] = parameter_value(name, available[name])
        return bound


def _with_setup(rendered: _Rendered, text: str, params: Mapping[str, Any]) -> _Rendered:
    return _Rendered(rendered.text, rendered.extra, (text, params))


def _last(results: Sequence[list[Record]]) -> list[Record]:
    """The rows of the plan's last statement (the ones before it are settings)."""

    return results[-1] if results else []


POSTGRES_DIALECT: Final = PostgresDialect()
