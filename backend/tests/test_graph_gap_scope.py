"""The scope step of a gap run: change reports to an alias map and touched ids, and gap relations moved to the
surviving key after a merge or a rekey (docs/graph/gap/detection.md "Run modes and scope", lifecycle.md "Rekeys and
merges"; FR-GRUN-02)."""

from __future__ import annotations

from typing import Any

from gap_detection_support import PROJECT
from gap_support import MemoryGapExecutor
from graph.core import load_ontology
from graph.core.writer import NodeRef, OwnedWriter
from graph.gap.gaps import gap_uuid, link_key, read_gaps
from graph.gap.models import GapChangeReport
from graph.gap.steps.scope import alias_map, scope, stale_links, touched_ids

GAP_ID = "GAP::A1B2C3D4E5F6"


def report(build_id: str, **fields: Any) -> GapChangeReport:
    return GapChangeReport(build_id=build_id, **fields)


def test_the_alias_map_follows_chains_in_build_order() -> None:
    changes = [
        report("b1", merged={"REQ::A": "REQ::B"}, rekeyed={"REQ::X": "REQ::Y"}),
        report("b2", rekeyed={"REQ::B": "REQ::C", "REQ::SAME": "REQ::SAME"}),
        report("b3", merged={"REQ::P": "REQ::Q", "REQ::Q": "REQ::P"}),
    ]

    aliases = alias_map(changes)

    assert aliases["REQ::A"] == "REQ::C" and aliases["REQ::B"] == "REQ::C"
    assert aliases["REQ::X"] == "REQ::Y" and "REQ::SAME" not in aliases
    assert aliases["REQ::P"] == "REQ::Q" and aliases["REQ::Q"] == "REQ::P"  # a malformed cycle stops


def test_touched_ids_are_every_class_of_the_reports_mapped_through_aliases() -> None:
    changes = [
        report(
            "b1",
            added=("REQ::NEW",),
            removed=("REQ::GONE",),
            changed_meaning=("REQ::A",),
            changed_links=("REQ::L",),
            changed_evidence=("REQ::E",),
        ),
        report("b2", merged={"REQ::A": "REQ::B"}),
    ]

    touched = touched_ids(changes, alias_map(changes))

    assert touched == ("REQ::B", "REQ::E", "REQ::GONE", "REQ::L", "REQ::NEW")


async def gap_about(executor: MemoryGapExecutor, *spec_ids: str) -> None:
    ontology = load_ontology()
    writer = OwnedWriter(executor, ontology, "gap", PROJECT)
    owner = NodeRef("Gap", gap_uuid(PROJECT, GAP_ID))
    await writer.upsert_node("Gap", owner.key, {"gap_id": GAP_ID, "status": "open", "signature": "s"})
    for spec_id in spec_ids:
        node = executor.specs[spec_id]
        await writer.upsert_relationship(
            "ABOUT",
            owner,
            NodeRef(node["label"], node["uuid"]),
            link_key(GAP_ID, "ABOUT", spec_id),
            {"added_by_run": "r1"},
        )


async def test_a_merge_moves_the_gap_relation_to_the_surviving_key() -> None:
    executor = MemoryGapExecutor(PROJECT)
    executor.spec("REQ::FR::OLD")
    executor.spec("REQ::FR::NEW")
    executor.spec("REQ::FR::KEPT")
    await gap_about(executor, "REQ::FR::OLD", "REQ::FR::KEPT")
    executor.merge_spec("REQ::FR::OLD", "REQ::FR::NEW")  # the build moves the relation, its key still names OLD
    ontology = load_ontology()
    gaps = await read_gaps(executor, PROJECT, ontology)
    assert [link.spec_id for link in stale_links(gaps[0])] == ["REQ::FR::NEW"]

    outcome = await scope(executor, ontology, PROJECT, [report("b1", merged={"REQ::FR::OLD": "REQ::FR::NEW"})], gaps)

    assert outcome.moved == 1 and outcome.aliases == {"REQ::FR::OLD": "REQ::FR::NEW"}
    assert executor.edge_keys("ABOUT") == {
        link_key(GAP_ID, "ABOUT", "REQ::FR::NEW"),
        link_key(GAP_ID, "ABOUT", "REQ::FR::KEPT"),
    }
    assert executor.edge_props[("ABOUT", link_key(GAP_ID, "ABOUT", "REQ::FR::NEW"))]["added_by_run"] == "r1"
    assert executor.gaps()[GAP_ID]["signature"] == "s"
    again = await scope(executor, ontology, PROJECT, [], await read_gaps(executor, PROJECT, ontology))
    assert again.moved == 0


async def test_a_merge_into_a_node_the_gap_is_already_about_only_drops_the_old_relation() -> None:
    executor = MemoryGapExecutor(PROJECT)
    executor.spec("REQ::FR::OLD")
    executor.spec("REQ::FR::NEW")
    await gap_about(executor, "REQ::FR::OLD", "REQ::FR::NEW")
    executor.merge_spec("REQ::FR::OLD", "REQ::FR::NEW")
    ontology = load_ontology()

    outcome = await scope(executor, ontology, PROJECT, [], await read_gaps(executor, PROJECT, ontology))

    assert outcome.moved == 1
    assert executor.edge_keys("ABOUT") == {link_key(GAP_ID, "ABOUT", "REQ::FR::NEW")}


async def test_a_rekey_rewrites_the_relation_under_the_new_id_and_nothing_without_persist() -> None:
    executor = MemoryGapExecutor(PROJECT)
    executor.spec("REQ::FR::OLD", "FunctionalRequirement")
    await gap_about(executor, "REQ::FR::OLD")
    executor.rekey_spec("REQ::FR::OLD", "REQ::FR::RENAMED")
    ontology = load_ontology()
    changes = [report("b1", rekeyed={"REQ::FR::OLD": "REQ::FR::RENAMED"})]
    gaps = await read_gaps(executor, PROJECT, ontology)

    dry = await scope(executor, ontology, PROJECT, changes, gaps, persist=False)
    assert dry.moved == 0 and executor.edge_keys("ABOUT") == {link_key(GAP_ID, "ABOUT", "REQ::FR::OLD")}

    outcome = await scope(executor, ontology, PROJECT, changes, gaps)

    assert outcome.moved == 1 and outcome.touched == ("REQ::FR::RENAMED",)
    assert executor.edge_keys("ABOUT") == {link_key(GAP_ID, "ABOUT", "REQ::FR::RENAMED")}
    assert outcome.payload()["aliases"] == {"REQ::FR::OLD": "REQ::FR::RENAMED"}
