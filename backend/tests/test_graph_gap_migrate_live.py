"""Live: the migration of first-slice findings on the selected graph store (docs/graph/gap/lifecycle.md "Migration of
first-slice findings", FR-GDEV-07, NFR-GPORT-01).

The shop gap corpus is written to a throwaway project, then first-slice ``Finding`` and ``ExternalRef`` nodes as the
first slice stored them (raw nodes and relations: the ontology no longer declares them): open, resolved and dismissed
findings, blocking and not, a note, one with external refs and one about a fragment. ``ensure_gap_schema`` with the
project id migrates them: every old id resolves through the ``aliases`` of a read gap, no ``Finding`` or
``ExternalRef`` is left, and a second run changes nothing. The test project is deleted afterwards.
"""

from __future__ import annotations

from typing import Any

from gap_acceptance_support import Planted, write_gap_corpus_graph
from gap_port_fakes import MemoryGapRecords
from graph.core import load_ontology
from graph.core.store import as_executor
from graph.gap import ensure_gap_schema
from graph.gap.gaps import read_gaps
from graph_probes import requires_live_graph
from graph_probes.base import EdgeSpec, NodeSpec, select
from graph_support import GraphProject, live_llm_config

pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()

CREATED = "2026-03-01T09:00:00.000+00:00"
CLOSED = "2026-03-02T10:30:00.000+00:00"
STORY = "STORY::ORDERS::CHECKOUT"
OPEN_BLOCKING = "FND::00000000LV01"
OPEN_WITH_REFS = "FND::00000000LV02"
RESOLVED = "FND::00000000LV03"
DISMISSED = "FND::00000000LV04"
NOTE = "FND::00000000LV05"
ABOUT_FRAGMENT = "FND::00000000LV06"


def _finding(finding_id: str, kind: str, status: str, text: str, about: list[str], **extra: Any) -> NodeSpec:
    props: dict[str, Any] = {
        "uuid": f"live-{finding_id}",
        "finding_id": finding_id,
        "kind": kind,
        "status": status,
        "text": text,
        "origin": "agent",
        "blocking": False,
        "about_spec_ids": about,
        "created_by": "agent-live",
        "created_at": CREATED,
        "updated_at": CREATED,
        "orphaned": False,
    }
    if status != "open":
        props |= {"resolved_by": "user-owner", "resolved_at": CLOSED, "updated_at": CLOSED}
    return NodeSpec(("Finding",), props | extra)


def _edge(relation: str, project_id: str, finding_id: str, target: Any, end: str) -> EdgeSpec:
    """A first-slice relation as the first slice wrote it (its ``group_id`` and ``edge_key`` set)."""

    key = {"group_id": project_id, "edge_key": f"{finding_id}|{relation}|{end}"}
    return EdgeSpec(relation, select("Finding", finding_id=finding_id), target, key)


def _about(project_id: str, finding_id: str, spec_id: str) -> EdgeSpec:
    return _edge("ABOUT", project_id, finding_id, select("SpecNode", spec_id=spec_id), spec_id)


async def _plant(project: GraphProject, fragment_id: str) -> None:
    project_id = project.project_id
    await project.create_nodes(
        [
            _finding(
                OPEN_BLOCKING,
                "question",
                "open",
                "May a paid order be cancelled?",
                [Planted.PLACE_ORDER],
                blocking=True,
                story_id=STORY,
            ),
            _finding(OPEN_WITH_REFS, "gap", "open", "Which refund API version applies?", [Planted.REFUND_INTERFACE]),
            _finding(
                RESOLVED,
                "conflict",
                "resolved",
                "Two review limits disagree.",
                [Planted.REVIEW_LIMIT_ORDERS],
                resolution_note="The 1,000 EUR limit holds.",
            ),
            _finding(DISMISSED, "gap", "dismissed", "Is CSV export needed?", [Planted.OWNERLESS_CSV_EXPORT]),
            _finding(NOTE, "note", "open", "The payment SDK pins TLS 1.2.", [Planted.PLACE_ORDER_FEATURE]),
            _finding(ABOUT_FRAGMENT, "gap", "open", "Which section owns the fee?", []),
            NodeSpec(("ExternalRef",), {"uuid": "live-ref-pr", "type": "pr", "value": "412"}),
            NodeSpec(("ExternalRef",), {"uuid": "live-ref-file", "type": "file", "value": "src/refunds.py"}),
        ]
    )
    await project.create_edges(
        [
            _about(project_id, OPEN_BLOCKING, Planted.PLACE_ORDER),
            _about(project_id, OPEN_WITH_REFS, Planted.REFUND_INTERFACE),
            _about(project_id, RESOLVED, Planted.REVIEW_LIMIT_ORDERS),
            _about(project_id, DISMISSED, Planted.OWNERLESS_CSV_EXPORT),
            _about(project_id, NOTE, Planted.PLACE_ORDER_FEATURE),
            _edge("ABOUT", project_id, ABOUT_FRAGMENT, select("Fragment", fragment_id=fragment_id), fragment_id),
            *(
                _edge("REFERS_TO", project_id, OPEN_WITH_REFS, select("ExternalRef", uuid=ref), ref)
                for ref in ("live-ref-pr", "live-ref-file")
            ),
        ]
    )


async def test_fr_gdev_07_first_slice_findings_are_migrated_on_the_live_store(graph_project: GraphProject) -> None:
    project_id = graph_project.project_id
    ontology = load_ontology()
    graph = await write_gap_corpus_graph(graph_project, project_id)
    fragment_id = str(graph.data["fragments"][1]["fragment_id"])
    await _plant(graph_project, fragment_id)
    assert len(await graph_project.nodes("Finding")) == 6
    records = MemoryGapRecords()
    config = live_llm_config()

    await ensure_gap_schema(graph_project.store, ontology, config, records=records, project_ids=[project_id])

    executor = as_executor(graph_project.store)
    gaps = await read_gaps(executor, project_id, ontology)
    by_alias = {alias: gap for gap in gaps for alias in gap.props.get("aliases") or ()}
    assert set(by_alias) == {OPEN_BLOCKING, OPEN_WITH_REFS, RESOLVED, ABOUT_FRAGMENT}
    assert (by_alias[OPEN_BLOCKING].status, by_alias[OPEN_BLOCKING].props["blocking"]) == ("open", "yes")
    assert by_alias[OPEN_BLOCKING].about_ids == (Planted.PLACE_ORDER,)
    assert by_alias[OPEN_BLOCKING].props["origin"] == "development"
    assert by_alias[OPEN_WITH_REFS].props["blocking"] == "no"
    resolved = by_alias[RESOLVED]
    assert (resolved.status, resolved.props["resolution"]) == ("resolved", "answered")
    assert [(item["mode"], item["status"], item["text"]) for item in resolved.clarifications] == [
        ("own", "applied", "The 1,000 EUR limit holds.")
    ]
    assert [(cite.fragment_id, cite.quote) for cite in by_alias[ABOUT_FRAGMENT].cites] == [(fragment_id, "")]
    (created,) = await records.events(project_id, by_alias[OPEN_WITH_REFS].gap_id)
    assert created.detail["refs"] == [{"type": "file", "value": "src/refunds.py"}, {"type": "pr", "value": "412"}]
    assert len(records.suppressions[project_id]) == 1 and len(records.notes[project_id]) == 1
    assert await graph_project.nodes("Finding") == [] and await graph_project.nodes("ExternalRef") == []

    before = await graph_project.snapshot()
    events = len(records.event_rows[project_id])
    await ensure_gap_schema(graph_project.store, ontology, config, records=records, project_ids=[project_id])
    assert await graph_project.snapshot() == before
    assert len(records.event_rows[project_id]) == events
