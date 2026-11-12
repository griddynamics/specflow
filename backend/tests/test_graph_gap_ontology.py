"""The gap ontology file: merged by ``load_ontology``, owner and origin, ``ABOUT`` pairs, no effect on extraction,
package data and schema objects (gap.md "Ontology file")."""

from __future__ import annotations

from importlib.resources import files
from typing import get_args

import pytest

from graph.build.run import label_set, ontology_fingerprint, relation_set
from graph.core import OwnedWriter, load_ontology
from graph.core.errors import ConformanceError, OwnershipError
from graph.core.ontology import FingerprintPart
from graph.core.ontology.model import OWNER_ORIGINS
from graph.core.queries import Query
from graph.core.schema import foreign_labels, schema_objects
from graph.core.writer import NodeRef
from graph.gap.names import BLOCK_REASONS, KINDS, REF_TYPES, STATUSES, FindingKind
from graph.plan.names import BlockReason
from graph_support import shipped_ontology

CARD = "uuid-card"


def test_the_agent_origin_is_one_of_the_origins_of_the_gap_owner() -> None:
    assert OWNER_ORIGINS["gap"] == frozenset({"analysis", "human", "agent"})


def test_load_ontology_merges_the_gap_file_beside_the_build_and_plan_files() -> None:
    ontology = load_ontology()

    assert {
        name: ontology.owner_of(name) for name in ("Finding", "ExternalRef", "ABOUT", "REFERS_TO")
    } == dict.fromkeys(("Finding", "ExternalRef", "ABOUT", "REFERS_TO"), "gap")
    assert ontology.owner_of("Task") == "plan" and ontology.owner_of("Feature") == "build"
    finding, ref = ontology.node_type("Finding"), ontology.node_type("ExternalRef")
    assert (finding.origin, finding.tier, finding.role, finding.extracted) == (
        "agent",
        "governance",
        None,
        False,
    )
    assert (ref.origin, ref.tier, ref.role, ref.extracted) == (
        "agent",
        "context",
        None,
        False,
    )
    assert {ontology.relation(name).origin for name in ("ABOUT", "REFERS_TO")} == {"agent"}
    assert {ontology.relation(name).tier for name in ("ABOUT", "REFERS_TO")} == {"governance"}


def test_the_vocabularies_of_the_file_hold_the_values_the_library_names() -> None:
    ontology = load_ontology()
    kind = ontology.node_type("Finding").attributes["kind"]
    status = ontology.node_type("Finding").attributes["status"]

    assert tuple(kind.values) == KINDS == get_args(FindingKind)
    assert tuple(status.values) == STATUSES
    assert tuple(ontology.node_type("ExternalRef").attributes["type"].values) == REF_TYPES


def test_the_block_reasons_are_reasons_the_plan_knows() -> None:
    assert set(BLOCK_REASONS) == {"question", "conflict", "gap"}
    assert set(BLOCK_REASONS.values()) <= set(get_args(BlockReason))


def test_about_reaches_every_build_label_and_fragment_and_nothing_else() -> None:
    ontology = load_ontology()
    build_labels = {label for label in ontology.labels if ontology.owner_of(label) == "build"}

    reached = {
        label
        for label in (
            *build_labels,
            "Fragment",
            "Document",
            "Task",
            "Finding",
            "ExternalRef",
        )
        if ontology.is_legal("ABOUT", "Finding", label)
    }

    assert reached == build_labels | {"Fragment"}
    assert {
        "Module",
        "Feature",
        "Capability",
        "Interface",
        "DataEntity",
        "OpenQuestion",
        "FunctionalRequirement",
    } <= reached
    assert ontology.is_legal("REFERS_TO", "Finding", "ExternalRef")
    assert not ontology.is_legal("REFERS_TO", "Finding", "Feature")
    assert not ontology.is_legal("ABOUT", "ExternalRef", "Feature")


def test_the_gap_file_changes_no_extraction_fingerprint_label_set_or_version() -> None:
    # The build-only ontology and the merged one (build, plan and gap files) are equal for everything extraction reads.
    build_only, merged = shipped_ontology(), load_ontology()

    assert {part: merged.fingerprint(part) for part in get_args(FingerprintPart)} == {
        part: build_only.fingerprint(part) for part in get_args(FingerprintPart)
    }
    assert ontology_fingerprint(merged) == ontology_fingerprint(build_only)
    assert label_set(merged) == label_set(build_only) and relation_set(merged) == relation_set(build_only)
    assert merged.version == build_only.version


def test_the_gap_file_alone_does_not_change_the_fingerprint_either() -> None:
    gap = files("graph.gap") / "ontology.yaml"
    plan = files("graph.plan") / "ontology.yaml"

    without_gap = load_ontology(submodule_files=[plan])
    with_gap = load_ontology(submodule_files=[plan, gap])

    assert ontology_fingerprint(with_gap) == ontology_fingerprint(without_gap)
    assert with_gap.is_declared("Finding") and not without_gap.is_declared("Finding")


def test_the_gap_file_is_package_data() -> None:
    package_file = files("graph.gap") / "ontology.yaml"

    assert package_file.is_file()
    assert package_file.read_text(encoding="utf-8").startswith("# ontology.yaml: the ontology file graph.gap ships")


def test_default_schema_objects_cover_the_gap_ontology() -> None:
    ontology = load_ontology()

    assert {
        "graph_gap_finding_id",
        "graph_foreign_uuid_finding",
        "graph_foreign_uuid_externalref",
        "graph_gap_finding_status",
        "graph_gap_finding_dedupe",
        "graph_edge_key_about",
        "graph_edge_key_refers_to",
    } <= {item.name for item in schema_objects(ontology)}
    assert {"Finding", "ExternalRef"} <= set(foreign_labels(ontology))


class _Recorder:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def read(self, query: Query, project_id: str, /, **params: object) -> list[dict[str, object]]:
        del project_id, params
        raise AssertionError(f"no read expected ({query.name})")

    async def write(self, query: Query, project_id: str, /, **params: object) -> list[dict[str, object]]:
        del project_id, params
        self.seen.append(query.name)
        return [{"written": 1, "conflict": False}]

    def tx(self, project_id: str) -> object:
        raise AssertionError(f"no transaction expected ({project_id})")


async def test_a_gap_writer_writes_its_own_types_and_refuses_a_spec_label_or_relation() -> None:
    # FR-GAP-08: graph.gap never writes a spec node, edge, document or fragment.
    recorder = _Recorder()
    writer = OwnedWriter(recorder, load_ontology(), "gap", "shop")

    await writer.upsert_node("Finding", "u1", {"status": "open"})
    await writer.upsert_relationship(
        "ABOUT",
        NodeRef("Finding", "u1"),
        NodeRef("FunctionalRequirement", CARD),
        "k",
        {},
    )

    assert recorder.seen == ["upsert_node_Finding", "upsert_relationship_ABOUT"]
    for call in (
        writer.upsert_node("FunctionalRequirement", CARD, {"name": "x"}),
        writer.upsert_node("Fragment", "f1", {}),
        writer.upsert_node("Document", "d1", {}),
        writer.upsert_relationship("DEPENDS_ON", NodeRef("Feature", "a"), NodeRef("Feature", "b"), "k", {}),
        writer.delete_node("Feature", "a"),
        writer.delete_relationship("PART_OF", "k"),
    ):
        with pytest.raises(OwnershipError):
            await call
    with pytest.raises(ConformanceError):  # an ABOUT edge from a finding to a task is not a legal pair
        await writer.upsert_relationship("ABOUT", NodeRef("Finding", "u1"), NodeRef("Task", "t1"), "k", {})
    assert recorder.seen == ["upsert_node_Finding", "upsert_relationship_ABOUT"]
