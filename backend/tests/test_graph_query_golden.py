"""Golden text of every query and DDL statement the graph library renders (design D2, plan step 0).

``fixtures/graph/golden_queries.json`` was generated from the code as it stood before the store port
refactor and is committed on its own. It maps a call descriptor to the ``Query.name``, the exact text,
the ``project_scoped`` flag and whether the text binds ``$project_id``. The test regenerates the same
mapping from the current code and compares it entry by entry, so any change of rendered text, name or
scoping fails. The fixture is never regenerated to make a change pass.

What is recorded:

- every module-level ``Query`` constant of ``graph.core.queries``, ``graph.build.queries`` and
  ``graph.plan.queries``, and the plan's link queries of every plan relation;
- ``CORE_DDL``, the ``ddl_query`` of every object of the shipped core schema and ``foreign_node_constraint``;
- for the shipped ontology, the ``gap`` test ontology (foreign owner) and the renamed test ontology, every
  factory output called as its callers call it: the reader's scope queries, every ``OwnedWriter``
  operation, the persist, diff and infer relation loops and the infer rule selections.

Rendering goes through ``render`` (the text the Cypher dialect hands the driver). Regenerate only for a
deliberate text change: ``uv run python tests/test_graph_query_golden.py --write``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from graph.build import queries as build_queries
from graph.build.infer import _select
from graph.build.queries import (
    delete_edges,
    infer_delete_edges,
    infer_exclusive_holders,
    infer_similar_targets,
    infer_write_edges,
    read_current_edges,
    update_edges,
    upsert_edges,
)
from graph.core import GraphReader, OwnedWriter, Query
from graph.core import queries as core_queries
from graph.core.errors import GraphError
from graph.core.ontology import Ontology, load_ontology
from graph.core.queries import CORE_DDL, foreign_node_constraint
from graph.core.schema import foreign_labels, schema_objects
from graph.core.writer import OWNERS, NodeRef
from graph.plan import queries as plan_queries
from graph.plan.names import PLAN_RELATIONS
from graph.stores._bolt.cypher import binds_project_id as binds_project_text
from graph.stores._bolt.cypher import render_cypher
from graph_support import load_gap_ontology, shipped_ontology
from test_graph_renamed_ontology_live import write_renamed_ontology

FIXTURE = Path(__file__).parent / "fixtures" / "graph" / "golden_queries.json"
PROJECT = "golden"
BUILD = "build"
_ROW = {
    "written": 1,
    "label_mismatch": False,
    "spec_id_changed": False,
    "conflict": False,
    "types": [],
    "deleted": 1,
    "found": 1,
    "moved": 0,
}


def render(query: Query) -> str:
    """The text the driver receives for ``query``."""

    return render_cypher(query)


def binds_project_id(query: Query) -> bool:
    return binds_project_text(render(query))


def entry(query: Query) -> dict[str, Any]:
    return {
        "name": query.name,
        "text": render(query),
        "project_scoped": query.project_scoped,
        "binds_project_id": binds_project_id(query),
    }


class _Transaction:
    def __init__(self, sink: _Recorder) -> None:
        self._sink = sink

    async def run(self, query: Query, /, **_params: Any) -> list[dict[str, Any]]:
        self._sink.seen.append(query)
        return [dict(_ROW)]


class _Recorder:
    """An executor that records each query it is given and answers a row every caller accepts."""

    def __init__(self) -> None:
        self.seen: list[Query] = []

    async def read(self, query: Query, _project_id: str, /, **_params: Any) -> list[dict[str, Any]]:
        self.seen.append(query)
        return []

    async def write(self, query: Query, _project_id: str, /, **_params: Any) -> list[dict[str, Any]]:
        self.seen.append(query)
        return [dict(_ROW)]

    @asynccontextmanager
    async def tx(self, _project_id: str) -> AsyncIterator[Any]:
        yield _Transaction(self)

    def take(self) -> list[Query]:
        seen, self.seen = self.seen, []
        return seen


def _constants() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for module in (core_queries, build_queries, plan_queries):
        for name, value in vars(module).items():
            if isinstance(value, Query) and name.isupper():
                found[f"const:{module.__name__}.{name}"] = entry(value)
    for relation in PLAN_RELATIONS:
        found[f"plan:read_links:{relation}"] = entry(plan_queries.read_links(relation))
        found[f"plan:delete_links:{relation}"] = entry(plan_queries.delete_links(relation))
    return found


def _ddl() -> dict[str, Any]:
    shipped = shipped_ontology()
    return {
        "CORE_DDL": list(CORE_DDL),
        "ddl_query": {item.name: entry(item.query()) for item in schema_objects(shipped, CORE_DDL)},
        "foreign_node_constraint": {
            label: foreign_node_constraint(label) for label in ("Finding", "Note", "Zeta", *foreign_labels(shipped))
        },
    }


async def _record_call(
    recorder: _Recorder, tag: str, out: dict[str, dict[str, Any]], call: str, operation: Any
) -> None:
    try:
        await operation
    except GraphError:
        recorder.take()
        return
    for index, query in enumerate(recorder.take()):
        out[f"{tag}:{call}#{index}"] = entry(query)


async def _writer_calls(ontology: Ontology, tag: str, out: dict[str, dict[str, Any]]) -> None:
    recorder = _Recorder()
    labels = sorted([*ontology.labels, "Document", "Fragment"])
    relations = sorted(ontology.relations)
    record = partial(_record_call, recorder, tag, out)

    for owner in sorted(OWNERS):
        writer = OwnedWriter(recorder, ontology, owner, PROJECT)
        for label in labels:
            await record(
                f"writer.upsert_node({owner},{label})",
                writer.upsert_node(label, "k", {"p": 1}),
            )
            await record(f"writer.delete_node({owner},{label})", writer.delete_node(label, "k"))
        for relation in relations:
            await record(
                f"writer.delete_relationship({owner},{relation})",
                writer.delete_relationship(relation, "k"),
            )
            for source in labels:
                for target in labels:
                    if not ontology.is_legal(relation, source, target):
                        continue
                    await record(
                        f"writer.upsert_relationship({owner},{relation},{source},{target})",
                        writer.upsert_relationship(
                            relation,
                            NodeRef(source, "a"),
                            NodeRef(target, "b"),
                            "k",
                            {},
                        ),
                    )
    builder = OwnedWriter(recorder, ontology, BUILD, PROJECT)
    spec = sorted(label for label in ontology.labels if ontology.owner_of(label) == BUILD)
    if len(spec) > 1:
        await record(
            "writer.move_foreign_relationships",
            builder.move_foreign_relationships(NodeRef(spec[0], "a"), NodeRef(spec[1], "b")),
        )


async def _infer_calls(ontology: Ontology, tag: str, out: dict[str, dict[str, Any]]) -> None:
    recorder = _Recorder()
    for rule in ontology.inference.rules:
        await _select(recorder, PROJECT, ontology, rule)
        for index, query in enumerate(recorder.take()):
            out[f"{tag}:infer.select({rule.id})#{index}"] = entry(query)
        if rule.template == "attach_orphan":
            for query in (
                infer_exclusive_holders(rule.writes),
                infer_similar_targets(rule.writes, symmetric=ontology.is_symmetric(rule.writes)),
            ):
                out[f"{tag}:infer.orphan({rule.id}):{query.name}"] = entry(query)


def _relation_loops(ontology: Ontology, tag: str, out: dict[str, dict[str, Any]]) -> None:
    owned = sorted(name for name in ontology.relations if ontology.owner_of(name) == BUILD)
    for relation in owned:
        for factory in (
            upsert_edges,
            update_edges,
            delete_edges,
            read_current_edges,
            infer_write_edges,
            infer_delete_edges,
        ):
            out[f"{tag}:{factory.__name__}({relation})"] = entry(factory(relation))


def _reader_scope(ontology: Ontology, tag: str, out: dict[str, dict[str, Any]]) -> None:
    reader = GraphReader(_Recorder(), ontology)
    for index, query in enumerate(reader._scope_queries):
        out[f"{tag}:reader.scope#{index}"] = entry(query)


async def _per_ontology(ontology: Ontology, tag: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    _reader_scope(ontology, tag, out)
    _relation_loops(ontology, tag, out)
    await _writer_calls(ontology, tag, out)
    await _infer_calls(ontology, tag, out)
    return out


def build_golden(directory: Path) -> dict[str, Any]:
    """The mapping the fixture holds, rendered from the current code (ontology files go to ``directory``)."""

    ontologies: Mapping[str, Ontology] = {
        "shipped": shipped_ontology(),
        "gap": load_gap_ontology(_mkdir(directory / "gap")),
        "renamed": load_ontology(write_renamed_ontology(_mkdir(directory / "renamed")), submodule_files=[]),
    }
    queries = _constants()
    for tag, ontology in ontologies.items():
        queries.update(asyncio.run(_per_ontology(ontology, tag)))
    return {"queries": queries, "ddl": _ddl()}


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _dump(golden: Mapping[str, Any]) -> str:
    return json.dumps(golden, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


@pytest.fixture(scope="module")
def golden(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return build_golden(tmp_path_factory.mktemp("golden"))


@pytest.fixture(scope="module")
def recorded() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def test_the_fixture_is_not_trivial(recorded: dict[str, Any]) -> None:
    assert len(recorded["queries"]) > 400
    assert len(recorded["ddl"]["CORE_DDL"]) > 10


def test_every_recorded_query_renders_the_same_name_and_text(golden: dict[str, Any], recorded: dict[str, Any]) -> None:
    now, before = golden["queries"], recorded["queries"]
    assert sorted(now) == sorted(before)
    changed = [key for key in before if now[key] != before[key]]
    assert changed == []


def test_the_ddl_is_unchanged(golden: dict[str, Any], recorded: dict[str, Any]) -> None:
    assert golden["ddl"] == recorded["ddl"]


if __name__ == "__main__":
    import tempfile

    if "--write" not in sys.argv:
        raise SystemExit("pass --write to regenerate the golden fixture")
    with tempfile.TemporaryDirectory() as scratch:
        FIXTURE.write_text(_dump(build_golden(Path(scratch))), encoding="utf-8")
