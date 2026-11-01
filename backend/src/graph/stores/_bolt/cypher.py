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
from pathlib import Path
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
from graph.core.query_files import (
    BUILD_QUERY_DIRECTORY,
    CORE_QUERY_DIRECTORY,
    read_query_file,
    render_query,
)

_PROJECT_ID_REFERENCE: Final = re.compile(r"\$project_id\b")
_DIRECTORIES: Final = {"core": CORE_QUERY_DIRECTORY, "build": BUILD_QUERY_DIRECTORY}


def _load(template: str) -> str:
    """The statement of the canonical file of ``template`` (``ConformanceError`` for an unknown one)."""

    prefix, _, stem = template.partition(".")
    directory = _DIRECTORIES.get(prefix)
    if directory is None or not stem or not (directory / f"{stem}.cypher").is_file():
        raise ConformanceError("query_template_unknown", template=template)
    return _file(directory, stem)


def _file(directory: Path, stem: str) -> str:
    return read_query_file(directory, f"{stem}.cypher")


def _core(stem: str) -> str:
    return _file(CORE_QUERY_DIRECTORY, stem)


def _build(stem: str) -> str:
    return _file(BUILD_QUERY_DIRECTORY, stem)


def _arrow(*, symmetric: bool) -> str:
    return "-" if symmetric else "->"


def _match(shape: NodeShape, var: str) -> str:
    """``MATCH`` of one node of the project by ``$<var>_key`` (and ``$<var>_label``); the fragment
    file's ``RETURN`` tail, which only makes it a complete statement, is left out."""

    label, key = safe_name(shape.label), safe_name(shape.key)
    stem = "match_node_checked" if shape.check_label else "match_node"
    return render_query(_core(stem), label=label, key=key, var=var, tail="")


def _upsert_relationship(args: RelationEnds) -> str:
    return render_query(
        _core("upsert_relationship"),
        source=_match(args.source, "s"),
        target=_match(args.target, "t"),
        type=safe_name(args.type),
    )


def _scope_via(args: ScopeVia) -> str:
    return render_query(
        _core("scope_via"),
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
        _core("scope_sequence"), pattern=pattern, conditions=" AND ".join(conditions)
    )


def _endpoint_subjects(args: EndpointSubjects) -> str:
    either = {safe_name(name) for name in args.symmetric}
    branches = [
        render_query(
            _build("infer_endpoint_via"),
            type=safe_name(name),
            arrow=_arrow(symmetric=name in either),
        )
        for name in args.via
    ]
    if args.mentions:
        branches.append(_build("infer_endpoint_mentions"))
    return render_query(
        _build("infer_endpoint_subjects"), branches=" UNION ".join(branches)
    )


def _realization_subjects(args: RealizationSubjects) -> str:
    return render_query(
        _build("infer_realization_subjects"),
        member=safe_name(args.member),
        member_arrow=_arrow(symmetric=args.member_symmetric),
        container=safe_name(args.container),
        container_arrow=_arrow(symmetric=args.container_symmetric),
        hops=str(int(args.max_hops)),
    )


def _similar_targets(args: SimilarTargets) -> str:
    return render_query(
        _build("infer_similar_targets"),
        type=safe_name(args.type),
        arrow=_arrow(symmetric=args.symmetric),
    )


def _typed(stem: str, *, build: bool = False) -> Callable[[Any], str]:
    """Renderer of a template that only needs its relationship type."""

    def render(args: Relation) -> str:
        return render_query(
            _build(stem) if build else _core(stem), type=safe_name(args.type)
        )

    return render


def _label_key(stem: str) -> Callable[[Any], str]:
    def render(args: LabelKey) -> str:
        return render_query(
            _core(stem), label=safe_name(args.label), key=safe_name(args.key)
        )

    return render


def _foreign(args: ForeignLabel) -> str:
    return render_query(
        _core("upsert_foreign_node"), foreign_label=safe_name(args.label)
    )


def _ddl(args: DdlText) -> str:
    return args.statement


def _literal(args: LiteralText) -> str:
    return args.text


# Templates with run-time parts: template id -> (type of its arguments, renderer of them).
_RENDERERS: Final[Mapping[str, tuple[type, Callable[[Any], str]]]] = {
    "core.upsert_relationship": (RelationEnds, _upsert_relationship),
    "core.delete_relationship": (Relation, _typed("delete_relationship")),
    "core.upsert_foreign_node": (ForeignLabel, _foreign),
    "core.delete_keyed_node": (LabelKey, _label_key("delete_keyed_node")),
    "core.keyed_node_relationship_types": (
        LabelKey,
        _label_key("keyed_node_relationship_types"),
    ),
    "core.scope_via": (ScopeVia, _scope_via),
    "core.scope_sequence": (ScopeSequence, _scope_sequence),
    "core.ddl": (DdlText, _ddl),
    "core.literal": (LiteralText, _literal),
    "build.read_current_edges": (Relation, _typed("read_current_edges", build=True)),
    "build.upsert_edges": (Relation, _typed("upsert_edges", build=True)),
    "build.update_edges": (Relation, _typed("update_edges", build=True)),
    "build.delete_edges": (Relation, _typed("delete_edges", build=True)),
    "build.infer_exclusive_holders": (
        Relation,
        _typed("infer_exclusive_holders", build=True),
    ),
    "build.infer_write_edges": (Relation, _typed("infer_write_edges", build=True)),
    "build.infer_delete_edges": (Relation, _typed("infer_delete_edges", build=True)),
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


def render_cypher(query: Query) -> str:
    """The Cypher text of ``query`` (no guard, no parameters): what the golden test and tests compare."""

    return _render(query.template, query.args)


def binds_project_id(text: str) -> bool:
    """Whether a Cypher text references the ``$project_id`` parameter."""

    return _PROJECT_ID_REFERENCE.search(text) is not None


class CypherDialect:
    """Plans every query as one Cypher statement, refusing a scoped one that does not bind ``$project_id``."""

    name: Final = "cypher"

    def plan(self, query: Query, params: Mapping[str, Any]) -> ExecutionPlan:
        text = render_cypher(query)
        if query.project_scoped and not binds_project_id(text):
            raise ConformanceError("query_missing_project_id", query=query.name)
        return ExecutionPlan.single(text, params)


CYPHER_DIALECT: Final = CypherDialect()
