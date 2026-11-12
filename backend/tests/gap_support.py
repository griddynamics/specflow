"""An in-memory stand-in for the graph store under ``graph.gap`` (offline tests of the findings library).

``MemoryGapExecutor`` implements the ``Executor`` surface (``read``, ``write``, ``tx``) over dictionaries and answers
the queries the library runs by name with the semantics of their Cypher and SQL files: the four gap queries, the
foreign-node and relationship writes ``OwnedWriter`` issues, and the two spec-node reads the alias resolution uses. A
transaction that raises undoes its writes. Every query is also rendered by the Cypher and PostgreSQL dialects, so a
template either store lacks, or a statement that does not bind the project id, fails the test.

``spec_graph`` builds the spec nodes the findings are about; ``rebuild_*`` helpers change them as a build would.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from graph.core.errors import GraphWriteError
from graph.core.queries import Query
from graph.stores._bolt.cypher import binds_project_id, render_cypher
from graph.stores.postgres.dialect import PostgresDialect

PROJECT: Final = "shop"
START: Final = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
_POSTGRES: Final = PostgresDialect()


class Clock:
    """A clock that advances one second per reading."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class MemoryGapExecutor:
    """The findings of one project, and the spec nodes they are about, behind the executor surface."""

    def __init__(self, project_id: str = PROJECT) -> None:
        self.project_id = project_id
        self.specs: dict[str, dict[str, Any]] = {}
        self.nodes: dict[tuple[str, str], dict[str, Any]] = {}  # (label, uuid) -> properties
        self.edges: dict[tuple[str, str], tuple[tuple[str, str], tuple[str, str]]] = {}  # (type, key) -> ends
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_on: str | None = None

    # The spec nodes

    def spec(
        self,
        spec_id: str,
        label: str = "FunctionalRequirement",
        aliases: Sequence[str] = (),
    ) -> None:
        self.specs[spec_id] = {
            "uuid": f"uuid-{spec_id}",
            "label": label,
            "aliases": list(aliases),
        }

    def delete_spec(self, spec_id: str) -> None:
        """A build deletes the node: its relationships go with it."""

        node = self.specs.pop(spec_id)
        self.edges = {key: ends for key, ends in self.edges.items() if node["uuid"] not in {e[1] for e in ends}}

    def merge_spec(self, gone: str, into: str) -> None:
        """A build merges ``gone`` into ``into``: the survivor keeps the alias and the foreign edges move to it."""

        old, new = self.specs.pop(gone), self.specs[into]
        new["aliases"] = [*new["aliases"], gone, *old["aliases"]]
        for key, (source, target) in list(self.edges.items()):
            if target[1] == old["uuid"]:
                self.edges[key] = (source, (new["label"], new["uuid"]))

    # Reading the result

    def findings(self) -> dict[str, dict[str, Any]]:
        return {props["finding_id"]: props for (label, _), props in self.nodes.items() if label == "Finding"}

    def refs(self) -> list[dict[str, Any]]:
        return [props for (label, _), props in self.nodes.items() if label == "ExternalRef"]

    def edge_keys(self, relation: str) -> set[str]:
        return {key for (kind, key) in self.edges if kind == relation}

    def called(self, name: str) -> int:
        return sum(1 for called, _ in self.calls if called == name)

    # The Executor surface

    async def read(self, query: Query, project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        return self._run(query, project_id, params)

    async def write(self, query: Query, project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        return self._run(query, project_id, params)

    @asynccontextmanager
    async def tx(self, project_id: str) -> AsyncIterator[_Transaction]:
        assert project_id == self.project_id
        saved = (copy.deepcopy(self.nodes), copy.deepcopy(self.edges))
        try:
            yield _Transaction(self)
        except BaseException:
            self.nodes, self.edges = saved
            raise

    # Statements

    def _run(self, query: Query, project_id: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        assert project_id == self.project_id
        assert binds_project_id(render_cypher(query)), query.name
        if query.project_scoped and query.template.partition(".")[0] in {"gap", "core"}:
            _POSTGRES.plan(query, {**params, "project_id": project_id})
        name = query.name
        self.calls.append((name, dict(params)))
        if self.fail_on == name:
            raise GraphWriteError("store_write_failed", query=name)
        if name.startswith("upsert_node_"):
            return self._upsert_node(name.removeprefix("upsert_node_"), params)
        if name.startswith("upsert_relationship_"):
            return self._upsert_edge(name.removeprefix("upsert_relationship_"), params)
        if name.startswith("delete_relationship_"):
            return self._delete_edge(name.removeprefix("delete_relationship_"), params)
        handler = getattr(self, f"_q_{name}", None)
        if handler is None:
            raise AssertionError(f"the memory executor does not run {name}")
        rows: list[dict[str, Any]] = handler(params)
        return rows

    def _q_read_node_uuids_by_spec_id(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        wanted = set(params["spec_ids"])
        return [
            {"uuid": node["uuid"], "spec_id": spec_id}
            for spec_id, node in sorted(self.specs.items())
            if node["label"] in params["labels"] and (spec_id in wanted or wanted & set(node["aliases"]))
        ]

    def _q_read_nodes_by_uuid(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "labels": ["SpecNode", node["label"]],
                "props": {
                    "uuid": node["uuid"],
                    "spec_id": spec_id,
                    "spec_id_aliases": list(node["aliases"]),
                },
                "provenance": [],
            }
            for spec_id, node in sorted(self.specs.items())
            if node["uuid"] in params["uuids"]
        ]

    def _upsert_node(self, label: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        key = (label, params["key"])
        merged = {**self.nodes.get(key, {}), **params["props"], "uuid": params["key"]}
        self.nodes[key] = {name: value for name, value in merged.items() if value is not None}
        return [{"written": 1}]

    def _upsert_edge(self, relation: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        source = (params["s_label"], params["s_key"])
        target = (params["t_label"], params["t_key"])
        if source not in self.nodes or (target not in self.nodes and not self._spec_has(target)):
            return []
        self.edges[(relation, params["key"])] = (source, target)
        return [{"conflict": False}]

    def _delete_edge(self, relation: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [{"deleted": int(self.edges.pop((relation, params["key"]), None) is not None)}]

    def _spec_has(self, target: tuple[str, str]) -> bool:
        return any(node["uuid"] == target[1] and node["label"] == target[0] for node in self.specs.values())

    def _q_gap_read_findings(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for (label, uuid), props in sorted(self.nodes.items(), key=lambda item: item[1].get("finding_id", "")):
            if label != "Finding" or props["status"] not in params["statuses"]:
                continue
            about = set(props.get("about_spec_ids") or ())
            if (
                (params["finding_id"] is not None and props["finding_id"] != params["finding_id"])
                or (params["created_by"] is not None and props["created_by"] != params["created_by"])
                or (params["dedupe_key"] is not None and props.get("dedupe_key") != params["dedupe_key"])
                or (params["spec_ids"] and not about & set(params["spec_ids"]))
            ):
                continue
            refs = [
                [self.nodes[end]["type"], self.nodes[end]["value"]]
                for (relation, _), (source, end) in self.edges.items()
                if relation == "REFERS_TO" and source == (label, uuid)
            ]
            rows.append({"finding": copy.deepcopy(props), "refs": refs})
        return rows

    def _q_gap_set_open_finding(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        for key, props in self.nodes.items():
            if key[0] == "Finding" and props["finding_id"] == params["finding_id"] and props["status"] == "open":
                merged = {**props, **params["props"], "updated_at": params["now"]}
                self.nodes[key] = {name: value for name, value in merged.items() if value is not None}
                return [{"written": 1}]
        return [{"written": 0}]

    def _q_gap_delete_open_finding(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        for key, props in list(self.nodes.items()):
            if key[0] == "Finding" and props["finding_id"] == params["finding_id"] and props["status"] == "open":
                del self.nodes[key]
                self.edges = {name: ends for name, ends in self.edges.items() if key not in ends}
                return [{"deleted": 1}]
        return [{"deleted": 0}]

    def _q_gap_delete_unused_refs(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        used = {ends[1] for (relation, _), ends in self.edges.items() if relation == "REFERS_TO"}
        gone = [key for key in self.nodes if key[0] == "ExternalRef" and key[1] in params["uuids"] and key not in used]
        for key in gone:
            del self.nodes[key]
        return [{"deleted": len(gone)}]


class _Transaction:
    def __init__(self, executor: MemoryGapExecutor) -> None:
        self._executor = executor

    async def run(self, query: Query, /, **params: Any) -> list[dict[str, Any]]:
        return self._executor._run(query, self._executor.project_id, params)


def spec_graph(executor: MemoryGapExecutor) -> MemoryGapExecutor:
    """The spec nodes the tests write findings about."""

    executor.spec("REQ::FR::CARD_PAYMENT", "FunctionalRequirement")
    executor.spec("FEAT::CARD_PAYMENT", "Feature")
    executor.spec("MOD::PAYMENTS_SERVICE", "Module")
    executor.spec("REQ::FR::REFUND", "FunctionalRequirement")
    return executor
