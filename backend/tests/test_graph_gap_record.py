"""The record step of a gap run: Gap nodes with ABOUT, IMPACTS and CITES through the owned writer, re-checks instead
of duplicates, notes, site rows, verdicts and events (docs/graph/gap/data-model.md "Graph model", "Identity";
FR-GOWN-01, FR-GOWN-02, FR-GDET-09)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gap_detection_support import PROJECT, MiniGraph
from gap_port_fakes import MemoryGapRecords
from gap_support import MemoryGapExecutor
from graph.core import load_ontology
from graph.gap.gaps import cite_key, link_key, quote_hash, read_gaps
from graph.gap.identity import gap_signature, witness_entry
from graph.gap.models import GapReport, Witness, WitnessRef
from graph.gap.steps.detect import Seed
from graph.gap.steps.record import RecordRun, SiteDecision, counted, outcome_counts, record_sites, report_signature
from graph.gap.steps.sites import Site

QUESTION = "Can a customer cancel a paid order?"
START = datetime(2026, 11, 1, tzinfo=UTC)


class Ticks:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def graph() -> tuple[MiniGraph, str]:
    mini = MiniGraph()
    fragment = mini.section("Orders", "Orders are placed by customers.", QUESTION)
    mini.node("QST::CANCEL", "OpenQuestion", "Cancel?", quote=QUESTION)
    mini.node("REQ::FR::PLACE", "FunctionalRequirement", "Place", quote="Orders are placed by customers.")
    mini.node("IFC::REFUND", "Interface", "Refund API")
    return mini, fragment


def executor(mini: MiniGraph) -> MemoryGapExecutor:
    memory = MemoryGapExecutor(PROJECT)
    for node in mini.nodes:
        memory.spec(node.spec_id, node.label, name=node.name, documents=["upload:spec"])
    for fragment in mini.fragments:
        memory.fragment(fragment.fragment_id, fragment.document_id)
    return memory


def site(fragment: str, *, gap_id: str | None = None, site_id: str = "SITE::1") -> Site:
    seed = Seed(
        detector="OQ-STATED",
        kind="open_question",
        family="text",
        strength="strong",
        about=("QST::CANCEL", "REQ::FR::PLACE"),
        witness=(Witness(fragment_id=fragment, quote=QUESTION, line_start=3, line_end=3),),
        context=("IFC::REFUND",),
        rule="present",
    )
    return Site(
        site_id=site_id,
        seeds=(seed,),
        about=seed.about,
        context=seed.context,
        input_hash=f"hash-{site_id}",
        rank_score=6.0,
        reach=1,
        gap_id=gap_id,
    )


def report(fragment: str, **update: object) -> GapReport:
    base = GapReport(
        verdict="gap",
        kind="open_question",
        severity="major",
        title="Open question",
        question=QUESTION,
        about=("QST::CANCEL", "REQ::FR::PLACE"),
        impacts=("IFC::REFUND",),
        witness=(WitnessRef(fragment_id=fragment, quote=QUESTION),),
    )
    return base.model_copy(update=update)


CLOCK = Ticks()


def record_run(memory: MemoryGapExecutor, records: MemoryGapRecords, run_id: str = "run-1") -> RecordRun:
    return RecordRun(
        source=memory,
        executor=memory,
        ontology=load_ontology(),
        records=records,
        project_id=PROJECT,
        run_id=run_id,
        now=CLOCK,
        model="stub",
        prompt_version="stub.1",
    )


async def test_a_gap_is_written_with_about_impacts_and_cites_and_its_rows() -> None:
    mini, fragment = graph()
    memory, records = executor(mini), MemoryGapRecords()
    decision = SiteDecision(site(fragment), "gap", report(fragment), "major", "ask")

    outcome = await record_sites(record_run(memory, records), mini.snapshot(), (), [decision])

    ((gap_id, props),) = memory.gaps().items()
    signature = gap_signature(
        "OQ-STATED", ("QST::CANCEL", "REQ::FR::PLACE"), [witness_entry(fragment_id=fragment, quote=QUESTION)]
    )
    assert props["signature"] == signature == site(fragment).seeds[0].signature
    assert gap_id == f"GAP::{signature[:12].upper()}"
    assert props["status"] == "open" and props["origin"] == "analysis" and props["detector"] == "OQ-STATED"
    assert props["severity"] == "major" and props["route"] == "ask" and props["run_id"] == "run-1"
    assert props["blocking"] == "no" and props["created_at"] == props["updated_at"]
    assert memory.edge_keys("ABOUT") == {
        link_key(gap_id, "ABOUT", "QST::CANCEL"),
        link_key(gap_id, "ABOUT", "REQ::FR::PLACE"),
    }
    assert memory.edge_keys("IMPACTS") == {link_key(gap_id, "IMPACTS", "IFC::REFUND")}
    key = cite_key(gap_id, fragment, quote_hash(QUESTION))
    assert memory.edge_keys("CITES") == {key}
    assert memory.edge_props[("CITES", key)] == {
        "quote": QUESTION,
        "quote_hash": quote_hash(QUESTION),
        "line_start": 3,
        "line_end": 3,
        "added_by_run": "run-1",
        "edge_key": key,
    }
    assert all(memory.edge_props[("ABOUT", item)]["added_by_run"] == "run-1" for item in memory.edge_keys("ABOUT"))
    assert outcome.outcomes == {"SITE::1": ("gap", gap_id)}
    (row,) = records.sites[PROJECT]
    assert row.outcome == "gap" and row.gap_id == gap_id and row.input_hash == "hash-SITE::1"
    assert records.verdicts[PROJECT]["hash-SITE::1"].report["gap_id"] == gap_id
    event, routed = records.event_rows[PROJECT]
    assert event.event == "created" and event.gap_id == gap_id
    assert set(event.detail) == {
        "run_id",
        "site_id",
        "kind",
        "severity",
        "route",
        "about",
        "impacts",
        "cites",
        "options",
    }
    assert routed.event == "routed" and routed.detail["route"] == "ask" and routed.detail["held_back"] is False
    assert QUESTION not in str(event.detail) and QUESTION not in str(routed.detail)
    assert counted(outcome.payload()["gaps_new"]) == {
        "total": 1,
        "by_kind": {"open_question": 1},
        "by_severity": {"major": 1},
    }


async def test_a_report_with_an_existing_signature_rechecks_that_gap() -> None:
    mini, fragment = graph()
    memory, records = executor(mini), MemoryGapRecords()
    decision = SiteDecision(site(fragment), "gap", report(fragment), "major", "ask")
    await record_sites(record_run(memory, records), mini.snapshot(), (), [decision])
    (gap_id,) = memory.gaps()
    memory.edges.pop(("IMPACTS", link_key(gap_id, "IMPACTS", "IFC::REFUND")))
    before = dict(memory.gaps()[gap_id])
    existing = await read_gaps(memory, PROJECT, load_ontology())

    outcome = await record_sites(record_run(memory, records, "run-2"), mini.snapshot(), existing, [decision])

    assert list(memory.gaps()) == [gap_id]
    assert outcome.outcomes["SITE::1"] == ("gap", gap_id)
    assert sum(outcome.gaps_updated.values()) == 1 and not outcome.gaps_new
    after = memory.gaps()[gap_id]
    assert after["run_id"] == "run-1" and after["created_at"] == before["created_at"]
    assert after["updated_at"] != before["updated_at"]  # it got its IMPACTS relation back
    assert link_key(gap_id, "IMPACTS", "IFC::REFUND") in memory.edge_keys("IMPACTS")
    rechecked = records.event_rows[PROJECT][-1]
    assert rechecked.event == "rechecked" and rechecked.detail["added"] == 1


async def test_a_site_joined_to_a_gap_that_is_not_open_writes_nothing_to_it() -> None:
    mini, fragment = graph()
    memory, records = executor(mini), MemoryGapRecords()
    decision = SiteDecision(site(fragment), "gap", report(fragment), "major", "ask")
    await record_sites(record_run(memory, records), mini.snapshot(), (), [decision])
    (gap_id,) = memory.gaps()
    for key, props in memory.nodes.items():
        if key[0] == "Gap":
            props["status"] = "answered"
    existing = await read_gaps(memory, PROJECT, load_ontology())
    calls = len(memory.calls)
    joined = SiteDecision(
        site(fragment, gap_id=gap_id), "gap", report(fragment, about=("QST::CANCEL",)), "minor", "ask"
    )

    outcome = await record_sites(record_run(memory, records, "run-2"), mini.snapshot(), existing, [joined])

    assert outcome.outcomes["SITE::1"] == ("gap", gap_id)
    assert not [name for name, _ in memory.calls[calls:] if name.startswith("upsert")]
    assert counted(outcome.payload()["gaps_updated"])["by_severity"] == {"major": 1}


async def test_info_reports_become_notes_and_other_outcomes_only_site_rows() -> None:
    mini, fragment = graph()
    memory, records = executor(mini), MemoryGapRecords()
    decisions = [
        SiteDecision(site(fragment, site_id="SITE::INFO"), "gap", report(fragment), "info", None),
        SiteDecision(site(fragment, site_id="SITE::NOT"), "not_a_gap", report(fragment, verdict="not_a_gap")),
        SiteDecision(site(fragment, site_id="SITE::LATER"), "deferred"),
    ]

    outcome = await record_sites(record_run(memory, records), mini.snapshot(), (), decisions)

    assert memory.gaps() == {}
    assert outcome.outcomes == {
        "SITE::INFO": ("not_a_gap", None),
        "SITE::NOT": ("not_a_gap", None),
        "SITE::LATER": ("deferred", None),
    }
    (note,) = records.notes[PROJECT]
    assert note.detector == "OQ-STATED" and note.run_id == "run-1" and QUESTION not in note.text
    assert set(records.verdicts[PROJECT]) == {"hash-SITE::INFO", "hash-SITE::NOT"}
    assert outcome_counts(outcome.payload()["outcomes"]) == {
        "gap": 0,
        "not_a_gap": 2,
        "not_spec": 0,
        "duplicate": 0,
        "deferred": 1,
    }


async def test_unknown_nodes_are_skipped_and_a_gap_without_any_anchor_is_deferred() -> None:
    mini, fragment = graph()
    memory, records = executor(mini), MemoryGapRecords()
    lost = report(fragment, about=("REQ::FR::NOT_IN_GRAPH",), impacts=(), witness=())
    partial = report(fragment, about=("QST::CANCEL", "REQ::FR::NOT_IN_GRAPH"), impacts=(), witness=())
    decisions = [
        SiteDecision(site(fragment, site_id="SITE::LOST"), "gap", lost, "major", "ask"),
        SiteDecision(site(fragment, site_id="SITE::PART"), "gap", partial, "major", "ask"),
    ]

    outcome = await record_sites(record_run(memory, records), mini.snapshot(), (), decisions)

    assert outcome.outcomes["SITE::LOST"] == ("deferred", None)
    (gap_id,) = memory.gaps()
    assert outcome.outcomes["SITE::PART"] == ("gap", gap_id)
    assert memory.edge_keys("ABOUT") == {link_key(gap_id, "ABOUT", "QST::CANCEL")}
    assert outcome.skipped_nodes == 2
    assert "hash-SITE::LOST" not in records.verdicts[PROJECT]


async def test_fr_gown_01_the_record_step_writes_only_gap_nodes_and_gap_relations() -> None:
    mini, fragment = graph()
    memory, records = executor(mini), MemoryGapRecords()
    specs = {spec_id: dict(node) for spec_id, node in memory.specs.items()}
    decision = SiteDecision(site(fragment), "gap", report(fragment), "major", "ask")

    await record_sites(record_run(memory, records), mini.snapshot(), (), [decision])

    written = [name for name, _ in memory.calls if not name.startswith("read_")]
    assert {name.removeprefix("upsert_node_") for name in written if name.startswith("upsert_node_")} == {"Gap"}
    relations = {name.removeprefix("upsert_relationship_") for name in written if "relationship" in name}
    assert relations == {"ABOUT", "IMPACTS", "CITES"}
    assert memory.specs == specs and set(memory.fragments) == {item.fragment_id for item in mini.fragments}


def test_a_report_signature_counts_each_graph_fact_of_the_seed_it_names() -> None:
    seed = Seed(
        detector="MS-PROVIDER",
        kind="missing",
        family="structure",
        strength="strong",
        about=("IFC::REFUND",),
        witness=(Witness(fact="no provider"),),
    )
    one = Site("SITE::F", (seed,), seed.about, (), "h", 1.0, 1)
    found = GapReport(verdict="gap", kind="missing", question="?", about=seed.about, witness=(WitnessRef(fact=0),))

    assert report_signature(one, found) == seed.signature
