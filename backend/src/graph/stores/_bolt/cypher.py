"""The Cypher dialect: renders the canonical ``.cypher`` files of the query catalogs (design D2, D3).

Every template of ``graph.core.queries`` and ``graph.build.queries`` is a canonical Cypher file under
their ``queries/`` folders (a template id ``core.<stem>`` or ``build.<stem>``). A template whose label,
relationship type, hop count or pattern is only known at run time is a file with ``__NAME__`` tokens and
``/*__NAME__*/default/*__NAME__*/`` slots; the renderer of that template fills them from the query's
semantic arguments. Every name rendered into text passes the safe-name check again here, so a query
built by hand cannot smuggle Cypher in. The text is byte-identical to what the catalogs produced before
the dialect existed (the golden test pins it).

The dialect also holds the ``$project_id`` guard: a scoped query whose statement does not reference
the parameter is refused (``query_missing_project_id``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from functools import cache
from typing import Any, Final

from graph.core.dialect import ExecutionPlan
from graph.core.errors import ConformanceError
from graph.core.queries import (
    DdlText,
    EndpointSubjects,
    ForeignLabel,
    LabelKey,
    LiteralText,
    NodeShape,
    Query,
    RealizationSubjects,
    Relation,
    RelationEnds,
    ScopeSequence,
    ScopeVia,
    SimilarTargets,
    safe_name,
)
from graph.core.query_files import load_build_query, load_core_query, render_query

_PROJECT_ID_REFERENCE: Final = re.compile(r"\$project_id\b")
_STATIC: Final[Mapping[str, Callable[[], str]]] = {
    "core.await_indexes": lambda: load_core_query("await_indexes.cypher"),
    "core.count_search_vectors": lambda: load_core_query("count_search_vectors.cypher"),
    "core.count_nodes": lambda: load_core_query("count_nodes.cypher"),
    "core.delete_spec_node": lambda: load_core_query("delete_spec_node.cypher"),
    "core.move_foreign_relationships": lambda: load_core_query("move_foreign_relationships.cypher"),
    "core.ping": lambda: load_core_query("ping.cypher"),
    "core.read_documents": lambda: load_core_query("read_documents.cypher"),
    "core.read_edges": lambda: load_core_query("read_edges.cypher"),
    "core.read_fragment_hits": lambda: load_core_query("read_fragment_hits.cypher"),
    "core.read_fragments": lambda: load_core_query("read_fragments.cypher"),
    "core.read_impact_hop": lambda: load_core_query("read_impact_hop.cypher"),
    "core.read_neighbours": lambda: load_core_query("read_neighbours.cypher"),
    "core.read_node_by_alias": lambda: load_core_query("read_node_by_alias.cypher"),
    "core.read_node_by_spec_id": lambda: load_core_query("read_node_by_spec_id.cypher"),
    "core.read_node_uuids_by_spec_id": lambda: load_core_query("read_node_uuids_by_spec_id.cypher"),
    "core.read_nodes": lambda: load_core_query("read_nodes.cypher"),
    "core.read_nodes_by_ids": lambda: load_core_query("read_nodes_by_ids.cypher"),
    "core.read_nodes_by_uuid": lambda: load_core_query("read_nodes_by_uuid.cypher"),
    "core.read_nodes_for_lines": lambda: load_core_query("read_nodes_for_lines.cypher"),
    "core.read_nodes_page": lambda: load_core_query("read_nodes_page.cypher"),
    "core.read_owner_values": lambda: load_core_query("read_owner_values.cypher"),
    "core.search_fragments": lambda: load_core_query("search_fragments.cypher"),
    "core.search_nodes_fulltext": lambda: load_core_query("search_nodes_fulltext.cypher"),
    "core.search_nodes_vector": lambda: load_core_query("search_nodes_vector.cypher"),
    "core.search_nodes_vector_exact": lambda: load_core_query("search_nodes_vector_exact.cypher"),
    "core.server_components": lambda: load_core_query("server_components.cypher"),
    "core.show_constraints": lambda: load_core_query("show_constraints.cypher"),
    "core.show_indexes": lambda: load_core_query("show_indexes.cypher"),
    "core.spec_node_exists": lambda: load_core_query("spec_node_exists.cypher"),
    "core.spec_node_relationship_types": lambda: load_core_query("spec_node_relationship_types.cypher"),
    "core.upsert_document": lambda: load_core_query("upsert_document.cypher"),
    "core.upsert_fragment": lambda: load_core_query("upsert_fragment.cypher"),
    "core.upsert_spec_node": lambda: load_core_query("upsert_spec_node.cypher"),
    "build.build_create_spec_nodes": lambda: load_build_query("build_create_spec_nodes.cypher"),
    "build.build_delete_spec_nodes": lambda: load_build_query("build_delete_spec_nodes.cypher"),
    "build.build_merge_spec_nodes": lambda: load_build_query("build_merge_spec_nodes.cypher"),
    "build.build_read_current_fragments": lambda: load_build_query("build_read_current_fragments.cypher"),
    "build.build_read_current_links": lambda: load_build_query("build_read_current_links.cypher"),
    "build.build_read_current_nodes": lambda: load_build_query("build_read_current_nodes.cypher"),
    "build.build_read_infer_fragments": lambda: load_build_query("build_read_infer_fragments.cypher"),
    "build.build_read_infer_nodes": lambda: load_build_query("build_read_infer_nodes.cypher"),
    "build.build_read_persisted_documents": lambda: load_build_query("build_read_persisted_documents.cypher"),
    "build.build_read_persisted_fragments": lambda: load_build_query("build_read_persisted_fragments.cypher"),
    "build.build_read_persisted_hubs": lambda: load_build_query("build_read_persisted_hubs.cypher"),
    "build.build_read_persisted_presence": lambda: load_build_query("build_read_persisted_presence.cypher"),
    "build.build_read_spine_edges": lambda: load_build_query("build_read_spine_edges.cypher"),
    "build.build_read_spine_nodes": lambda: load_build_query("build_read_spine_nodes.cypher"),
    "build.build_repair_delete_edges": lambda: load_build_query("build_repair_delete_edges.cypher"),
    "build.build_repair_delete_nodes": lambda: load_build_query("build_repair_delete_nodes.cypher"),
    "build.build_repair_edge_evidence": lambda: load_build_query("build_repair_edge_evidence.cypher"),
    "build.build_repair_node_evidence": lambda: load_build_query("build_repair_node_evidence.cypher"),
    "build.build_repair_set_labels": lambda: load_build_query("build_repair_set_labels.cypher"),
    "build.build_repair_set_spec_ids": lambda: load_build_query("build_repair_set_spec_ids.cypher"),
    "build.build_replace_node_links": lambda: load_build_query("build_replace_node_links.cypher"),
    "build.build_set_document_status": lambda: load_build_query("build_set_document_status.cypher"),
    "build.build_set_fragment_status": lambda: load_build_query("build_set_fragment_status.cypher"),
    "build.build_update_spec_nodes": lambda: load_build_query("build_update_spec_nodes.cypher"),
    "build.build_upsert_documents": lambda: load_build_query("build_upsert_documents.cypher"),
    "build.build_upsert_fragments": lambda: load_build_query("build_upsert_fragments.cypher"),
    "build.build_verify_domain_edges": lambda: load_build_query("build_verify_domain_edges.cypher"),
    "build.build_verify_fragments": lambda: load_build_query("build_verify_fragments.cypher"),
    "build.build_verify_spec_nodes": lambda: load_build_query("build_verify_spec_nodes.cypher"),
    "build.infer_xdoc_documents": lambda: load_build_query("infer_xdoc_documents.cypher"),
    "build.infer_xdoc_embeddings": lambda: load_build_query("infer_xdoc_embeddings.cypher"),
    "build.infer_xdoc_members": lambda: load_build_query("infer_xdoc_members.cypher"),
}


def _load(template: str) -> str:
    """The statement of the canonical file of a template without run-time parts."""

    loader = _STATIC.get(template)
    if loader is None:
        raise ConformanceError("query_template_unknown", template=template)
    return loader()


def _arrow(*, symmetric: bool) -> str:
    return "-" if symmetric else "->"


def _match(shape: NodeShape, var: str) -> str:
    """``MATCH`` of one node of the project by ``$<var>_key`` (and ``$<var>_label``); the fragment
    file's ``RETURN`` tail, which only makes it a complete statement, is left out."""

    label, key = safe_name(shape.label), safe_name(shape.key)
    fragment = (
        load_core_query("match_node_checked.cypher") if shape.check_label else load_core_query("match_node.cypher")
    )
    return render_query(fragment, label=label, key=key, var=var, tail="")


def _upsert_relationship(args: RelationEnds) -> str:
    return render_query(
        load_core_query("upsert_relationship.cypher"),
        source=_match(args.source, "s"),
        target=_match(args.target, "t"),
        type=safe_name(args.type),
    )


def _scope_via(args: ScopeVia) -> str:
    return render_query(
        load_core_query("scope_via.cypher"),
        types="|".join(safe_name(name) for name in args.relations),
        min_hops=str(int(args.min_hops)),
        max_hops=str(int(args.max_hops)),
    )


def _scope_sequence(args: ScopeSequence) -> str:
    either_way = {safe_name(name) for name in args.symmetric}
    pattern = "(n:SpecNode)"
    conditions = []
    for index, relation in enumerate(args.relations):
        last = index == len(args.relations) - 1
        end = "(root)" if last else f"(x{index}:SpecNode)"
        arrow = "-" if relation in either_way else "->"
        pattern += f"-[r{index}:{safe_name(relation)}]{arrow}{end}"
        conditions.append(f"r{index}.group_id = $project_id")
        if not last:
            conditions.append(
                f"x{index}.group_id = $project_id AND none(label IN labels(x{index}) WHERE label IN $roots)"
            )
    return render_query(
        load_core_query("scope_sequence.cypher"),
        pattern=pattern,
        conditions=" AND ".join(conditions),
    )


def _endpoint_subjects(args: EndpointSubjects) -> str:
    either = {safe_name(name) for name in args.symmetric}
    branches = [
        render_query(
            load_build_query("infer_endpoint_via.cypher"),
            type=safe_name(name),
            arrow=_arrow(symmetric=name in either),
        )
        for name in args.via
    ]
    if args.mentions:
        branches.append(load_build_query("infer_endpoint_mentions.cypher"))
    return render_query(
        load_build_query("infer_endpoint_subjects.cypher"),
        branches=" UNION ".join(branches),
    )


def _container_pattern(args: RealizationSubjects) -> str:
    """The pattern from a member to a container: one edge of each relation of a sequence (through spec
    nodes), else ``min_hops``..``max_hops`` edges of any of the relations, in their one direction."""

    if args.sequence:
        return "(:SpecNode)".join(
            f"-[:{safe_name(name)}]{_arrow(symmetric=either)}"
            for name, either in zip(args.relations, args.symmetric, strict=True)
        )
    names = "|".join(safe_name(name) for name in args.relations)
    return f"-[:{names}*{int(args.min_hops)}..{int(args.max_hops)}]{_arrow(symmetric=all(args.symmetric))}"


def _realization_subjects(args: RealizationSubjects) -> str:
    return render_query(
        load_build_query("infer_realization_subjects.cypher"),
        member=safe_name(args.member),
        member_arrow=_arrow(symmetric=args.member_symmetric),
        pattern=_container_pattern(args),
    )


def _similar_targets(args: SimilarTargets) -> str:
    return render_query(
        load_build_query("infer_similar_targets.cypher"),
        type=safe_name(args.type),
        arrow=_arrow(symmetric=args.symmetric),
    )


def _typed(load: Callable[[], str]) -> Callable[[Any], str]:
    """Renderer of a template that only needs its relationship type; ``load`` reads its file."""

    def render(args: Relation) -> str:
        return render_query(load(), type=safe_name(args.type))

    return render


def _label_key(load: Callable[[], str]) -> Callable[[Any], str]:
    def render(args: LabelKey) -> str:
        return render_query(load(), label=safe_name(args.label), key=safe_name(args.key))

    return render


def _foreign(args: ForeignLabel) -> str:
    return render_query(
        load_core_query("upsert_foreign_node.cypher"),
        foreign_label=safe_name(args.label),
    )


def _ddl(args: DdlText) -> str:
    return args.statement


def _literal(args: LiteralText) -> str:
    return args.text


# Templates with run-time parts: template id -> (type of its arguments, renderer of them).
_RENDERERS: Final[Mapping[str, tuple[type, Callable[[Any], str]]]] = {
    "core.upsert_relationship": (RelationEnds, _upsert_relationship),
    "core.delete_relationship": (
        Relation,
        _typed(lambda: load_core_query("delete_relationship.cypher")),
    ),
    "core.upsert_foreign_node": (ForeignLabel, _foreign),
    "core.delete_keyed_node": (
        LabelKey,
        _label_key(lambda: load_core_query("delete_keyed_node.cypher")),
    ),
    "core.keyed_node_relationship_types": (
        LabelKey,
        _label_key(lambda: load_core_query("keyed_node_relationship_types.cypher")),
    ),
    "core.scope_via": (ScopeVia, _scope_via),
    "core.scope_sequence": (ScopeSequence, _scope_sequence),
    "core.ddl": (DdlText, _ddl),
    "core.literal": (LiteralText, _literal),
    "build.read_current_edges": (
        Relation,
        _typed(lambda: load_build_query("read_current_edges.cypher")),
    ),
    "build.upsert_edges": (
        Relation,
        _typed(lambda: load_build_query("upsert_edges.cypher")),
    ),
    "build.update_edges": (
        Relation,
        _typed(lambda: load_build_query("update_edges.cypher")),
    ),
    "build.delete_edges": (
        Relation,
        _typed(lambda: load_build_query("delete_edges.cypher")),
    ),
    "build.infer_exclusive_holders": (
        Relation,
        _typed(lambda: load_build_query("infer_exclusive_holders.cypher")),
    ),
    "build.infer_write_edges": (
        Relation,
        _typed(lambda: load_build_query("infer_write_edges.cypher")),
    ),
    "build.infer_delete_edges": (
        Relation,
        _typed(lambda: load_build_query("infer_delete_edges.cypher")),
    ),
    "build.infer_endpoint_subjects": (EndpointSubjects, _endpoint_subjects),
    "build.infer_realization_subjects": (RealizationSubjects, _realization_subjects),
    "build.infer_similar_targets": (SimilarTargets, _similar_targets),
}


@cache
def _render(template: str, args: Any) -> str:
    entry = _RENDERERS.get(template)
    if entry is None:
        return render_query(_load(template))
    expected, renderer = entry
    if not isinstance(args, expected):
        raise ConformanceError("query_arguments_invalid", template=template)
    return renderer(args)


def template_ids() -> frozenset[str]:
    """Every template id the Cypher dialect knows (a store dialect built on it covers exactly these)."""

    return frozenset(_STATIC) | frozenset(_RENDERERS)


def render_cypher(query: Query) -> str:
    """The Cypher text of ``query`` (no guard, no parameters): what the golden test and tests compare."""

    return _render(query.template, query.args)


def binds_project_id(text: str) -> bool:
    """Whether a Cypher text references the ``$project_id`` parameter."""

    return _PROJECT_ID_REFERENCE.search(text) is not None


class CypherDialect:
    """Plans every query as one Cypher statement, refusing a scoped one that does not bind ``$project_id``."""

    name: Final = "cypher"

    def require_project_binding(self, query: Query) -> None:
        if query.project_scoped and not binds_project_id(render_cypher(query)):
            raise ConformanceError("query_missing_project_id", query=query.name)

    def plan(self, query: Query, params: Mapping[str, Any]) -> ExecutionPlan:
        self.require_project_binding(query)
        text = render_cypher(query)
        return ExecutionPlan.single(text, params)


CYPHER_DIALECT: Final = CypherDialect()
