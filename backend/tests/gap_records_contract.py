"""The contract of the relational ports ``GapRecords`` and ``Amendments`` (docs/graph/gap/api.md "Ports").

Written against the protocols only, so it runs on the in-memory fakes (``gap_port_fakes``, offline) and on the
PostgreSQL adapters (``api.gap.records``, live). A ``Backend`` bundles the two ports, two project ids and a ``Probe``
that reads back what the write-only calls stored (runs, seeds, sites, notes, events) and stores noise rules
directly (the port's ``put_noise_rule`` has its own case). Every case shows the two-project isolation of its calls
(FR-GOWN-02); none of the rows is a graph node (FR-GOWN-04).

A test module parametrizes over ``CASES``: ``await case(backend)``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

import pytest

from graph.core.errors import ConformanceError
from graph.gap.models import (
    AmendmentRow,
    Edit,
    EditTarget,
    EventRow,
    NoiseRuleRow,
    NoteRow,
    OptionRow,
    Quote,
    RelevanceRow,
    RunRow,
    SectionKey,
    SeedRow,
    SiteRow,
    SuppressionRow,
    VerdictRow,
    Witness,
)
from graph.gap.ports import Amendments, GapRecords

NOW: Final = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)
LATER: Final = NOW + timedelta(minutes=5)


class Probe(Protocol):
    async def runs(self, project_id: str) -> Sequence[RunRow]: ...

    async def seeds(self, project_id: str) -> Sequence[SeedRow]: ...

    async def sites(self, project_id: str) -> Sequence[SiteRow]: ...

    async def notes(self, project_id: str) -> Sequence[NoteRow]: ...

    async def events(self, project_id: str) -> Sequence[EventRow]: ...

    async def add_noise_rule(self, project_id: str, row: NoiseRuleRow) -> None: ...


@dataclass(frozen=True)
class Backend:
    records: GapRecords
    amendments: Amendments
    probe: Probe
    a: str
    b: str


Case = Callable[[Backend], Awaitable[None]]


def option(rank: int, *, recommended: bool = False, gap_id: str = "GAP::A1B2C3D4E5F6") -> OptionRow:
    edit = Edit(
        placement="amend",
        target=EditTarget(fragment_id="FRAG::orders::1", anchor_quote="Orders are placed by customers."),
        operation="insert_after",
        text=f"Option {rank} text.",
    )
    return OptionRow(
        gap_id=gap_id,
        rank=rank,
        label=f"Option {rank}",
        detail="Why this option.",
        recommended=recommended,
        edits=(edit,),
        basis=(Quote(fragment_id="FRAG::orders::1", quote="Orders are placed by customers."),),
        created_run="run-1",
    )


def run(run_id: str = "run-1") -> RunRow:
    return RunRow(
        run_id=run_id,
        build_id="build-1",
        mode="update",
        status="running",
        started_at=NOW,
        rules_version="1",
        prompt_versions={"gap_investigate": "v1"},
    )


def seed(run_id: str = "run-1", detector: str = "OQ-TBD") -> SeedRow:
    return SeedRow(
        run_id=run_id,
        detector=detector,
        spec_ids=("REQ::FR::PLACE_ORDER",),
        witness=(
            Witness(fragment_id="FRAG::orders::1", quote="TBD", line_start=3, line_end=3),
            Witness(fact="REQ::FR::PLACE_ORDER has no owner"),
        ),
        strength="strong",
        relevance="spec",
        corroborated=True,
        site_id="SITE::1",
    )


def site(site_id: str = "SITE::1", run_id: str = "run-1", outcome: str = "deferred") -> SiteRow:
    return SiteRow.model_validate(
        {
            "site_id": site_id,
            "run_id": run_id,
            "input_hash": f"hash-{site_id}",
            "rank_score": 0.75,
            "outcome": outcome,
            "gap_id": None,
        }
    )


def verdict(input_hash: str = "hash-SITE::1", model: str = "model-a") -> VerdictRow:
    return VerdictRow(
        input_hash=input_hash,
        report={"verdict": "gap", "options": [{"label": "Yes"}], "confidence": 0.8},
        model=model,
        prompt_version="v1",
        created_at=NOW,
    )


def relevance(heading: str, text_hash: str = "t1", value: str = "spec") -> RelevanceRow:
    return RelevanceRow.model_validate(
        {
            "lineage": "upload:orders-spec",
            "heading_path": ["Orders", heading],
            "text_hash": text_hash,
            "relevance": value,
            "signals": ["requirement_words"],
            "by": "rules",
        }
    )


def amendment(
    amendment_id: str,
    *,
    lineage: str = "upload:orders-spec",
    clr_id: str = "CLR::1",
    status: str = "active",
    created_at: datetime = NOW,
) -> AmendmentRow:
    return AmendmentRow.model_validate(
        {
            "amendment_id": amendment_id,
            "clr_id": clr_id,
            "lineage": lineage,
            "heading_path": ["Orders", "Place order"],
            "anchor_text": "Orders are placed by customers.",
            "anchor_hash": "anchor-hash",
            "operation": "insert_after",
            "text": "Guests can place orders too.",
            "status": status,
            "last_revision_id": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
    )


# Options


async def options_are_replaced_and_read_by_rank(backend: Backend) -> None:
    records, a, b = backend.records, backend.a, backend.b
    first = [option(3), option(1, recommended=True), option(2)]

    await records.put_options(a, "GAP::A1B2C3D4E5F6", first)
    assert tuple(await records.options(a, "GAP::A1B2C3D4E5F6")) == (option(1, recommended=True), option(2), option(3))
    assert tuple(await records.options(b, "GAP::A1B2C3D4E5F6")) == ()

    await records.put_options(b, "GAP::A1B2C3D4E5F6", [option(1), option(2, recommended=True)])
    await records.put_options(a, "GAP::A1B2C3D4E5F6", [option(1), option(2, recommended=True)])
    assert tuple(await records.options(a, "GAP::A1B2C3D4E5F6")) == (option(1), option(2, recommended=True))

    await records.put_options(a, "GAP::A1B2C3D4E5F6", [])
    assert tuple(await records.options(a, "GAP::A1B2C3D4E5F6")) == ()
    assert len(await records.options(b, "GAP::A1B2C3D4E5F6")) == 2


async def invalid_option_sets_are_refused(backend: Backend) -> None:
    records, a = backend.records, backend.a
    await records.put_options(a, "GAP::A1B2C3D4E5F6", [option(1, recommended=True), option(2)])
    invalid = (
        [option(1, recommended=True), option(2, recommended=True)],
        [option(1, recommended=True), option(1)],
        [option(1, recommended=True), option(2, gap_id="GAP::OTHER")],
    )

    for rows in invalid:
        with pytest.raises(ConformanceError, match="gap_options_invalid"):
            await records.put_options(a, "GAP::A1B2C3D4E5F6", rows)
    assert len(await records.options(a, "GAP::A1B2C3D4E5F6")) == 2


# Runs, seeds, sites, notes, events


async def runs_start_and_finish_in_their_project(backend: Backend) -> None:
    records, probe, a, b = backend.records, backend.probe, backend.a, backend.b

    await records.start_run(a, run())
    await records.start_run(b, run())
    assert tuple(await probe.runs(a)) == (run(),)

    await records.finish_run(a, "run-1", {"seeds": {"OQ-TBD": 2}, "gaps_new": 1}, {"gap_investigate": {"calls": 3}})
    (finished,) = await probe.runs(a)
    assert finished.status == "completed"
    assert finished.finished_at is not None
    assert dict(finished.counts) == {"seeds": {"OQ-TBD": 2}, "gaps_new": 1}
    assert dict(finished.cost) == {"gap_investigate": {"calls": 3}}
    assert tuple(await probe.runs(b)) == (run(),)

    with pytest.raises(ConformanceError, match="gap_run_unknown"):
        await records.finish_run(b, "run-2", {}, {})
    assert tuple(await probe.runs(b)) == (run(),)

    await records.start_run(b, run().model_copy(update={"mode": "full"}))
    assert [row.mode for row in await probe.runs(b)] == ["full"]

    # FR-GRUN-08: a run whose every investigation failed finishes degraded; a running status is not a finish.
    await records.finish_run(b, "run-1", {}, {}, status="degraded")
    assert [row.status for row in await probe.runs(b)] == ["degraded"]
    running: Any = "running"
    with pytest.raises(ConformanceError, match="gap_run_status_invalid"):
        await records.finish_run(b, "run-1", {}, {}, status=running)
    assert [row.status for row in await probe.runs(b)] == ["degraded"]


async def seeds_and_sites_belong_to_their_run(backend: Backend) -> None:
    records, probe, a, b = backend.records, backend.probe, backend.a, backend.b

    await records.put_seeds(a, "run-1", [seed(), seed(detector="MS-OWNER")])
    await records.put_sites(a, "run-1", [site("SITE::1"), site("SITE::2")])
    assert sorted(row.detector for row in await probe.seeds(a)) == ["MS-OWNER", "OQ-TBD"]
    assert seed() in await probe.seeds(a)
    assert tuple(await probe.seeds(b)) == ()
    assert tuple(await probe.sites(b)) == ()

    with pytest.raises(ConformanceError, match="gap_rows_of_another_run"):
        await records.put_seeds(a, "run-1", [seed("run-2")])
    with pytest.raises(ConformanceError, match="gap_rows_of_another_run"):
        await records.put_sites(a, "run-1", [site(run_id="run-2")])

    await records.put_sites(b, "run-9", [site("SITE::5", "run-9"), site("SITE::5", "run-9", outcome="skipped")])
    assert [row.outcome for row in await probe.sites(b)] == ["skipped"]
    await records.put_sites(b, "run-9", [])

    investigated = site("SITE::1", outcome="gap").model_copy(update={"gap_id": "GAP::A1B2C3D4E5F6"})
    await records.put_sites(a, "run-1", [investigated])
    await records.put_sites(b, "run-1", [site("SITE::1")])
    assert sorted(await probe.sites(a), key=lambda row: row.site_id) == [investigated, site("SITE::2")]
    assert site("SITE::1") in await probe.sites(b)

    await records.put_seeds(a, "run-1", [])
    await records.put_sites(a, "run-1", [])
    assert len(await probe.seeds(a)) == 2


async def notes_take_the_run_and_events_are_logged(backend: Backend) -> None:
    records, probe, a, b = backend.records, backend.probe, backend.a, backend.b
    note = NoteRow(detector="OQ-STATED", spec_ids=("QST::A",), text="An open question is stated.")
    events = [
        EventRow(gap_id="GAP::A1B2C3D4E5F6", at=NOW, actor="gap-run", event="opened", detail={"run_id": "run-1"}),
        EventRow(gap_id="GAP::A1B2C3D4E5F6", clr_id="CLR::1", at=LATER, actor="ann", event="answered"),
    ]

    await records.put_notes(a, "run-7", [note])
    await records.log(a, events)
    await records.log(a, [])
    await records.put_notes(a, "run-7", [])

    assert tuple(await probe.notes(a)) == (note.model_copy(update={"run_id": "run-7"}),)
    assert sorted(await probe.events(a), key=lambda row: row.at) == events
    assert tuple(await probe.notes(b)) == ()
    assert tuple(await probe.events(b)) == ()


async def events_are_read_per_gap_in_time_order(backend: Backend) -> None:
    """``events`` (GapView history) returns one gap's events of the project, oldest first."""

    records, a, b = backend.records, backend.a, backend.b
    gap, other = "GAP::A1B2C3D4E5F6", "GAP::0000000000AA"
    late = EventRow(gap_id=gap, at=LATER, actor="ann", event="answered", detail={"clr_id": "CLR::1"})
    early = EventRow(gap_id=gap, at=NOW, actor="gap-run", event="created", detail={"run_id": "run-1"})
    await records.log(a, [late, EventRow(gap_id=other, at=NOW, actor="gap-run", event="created"), early])
    await records.log(b, [EventRow(gap_id=gap, at=NOW, actor="gap-run", event="created")])

    assert tuple(await records.events(a, gap)) == (early, late)
    assert [row.actor for row in await records.events(b, gap)] == ["gap-run"]
    assert tuple(await records.events(a, "GAP::FFFFFFFFFFFF")) == ()


# Caches: verdicts, section relevance


async def verdicts_are_cached_per_project(backend: Backend) -> None:
    records, a, b = backend.records, backend.a, backend.b

    assert await records.verdict(a, "hash-SITE::1") is None
    await records.put_verdict(a, verdict())
    assert await records.verdict(a, "hash-SITE::1") == verdict()
    assert await records.verdict(b, "hash-SITE::1") is None

    await records.put_verdict(a, verdict(model="model-b"))
    assert await records.verdict(a, "hash-SITE::1") == verdict(model="model-b")


async def relevance_is_read_by_section_key(backend: Backend) -> None:
    records, a, b = backend.records, backend.a, backend.b
    rows = [relevance("Place order"), relevance("Order export", value="mixed"), relevance("Place order", "t2")]
    missing = SectionKey(lineage="upload:orders-spec", heading_path=("Orders",), text_hash="t1")

    await records.put_relevance(a, rows)
    found = await records.relevance(a, [rows[0].key, rows[1].key, missing])
    assert dict(found) == {rows[0].key: rows[0], rows[1].key: rows[1]}
    assert dict(await records.relevance(b, [row.key for row in rows])) == {}
    assert dict(await records.relevance(a, [])) == {}

    by_person = relevance("Order export", value="tracking").model_copy(update={"by": "person"})
    await records.put_relevance(a, [rows[1], by_person])
    assert dict(await records.relevance(a, [by_person.key])) == {by_person.key: by_person}
    await records.put_relevance(a, [])


# Noise rules, suppressions


async def noise_rules_are_listed_per_project(backend: Backend) -> None:
    records, probe, a, b = backend.records, backend.probe, backend.a, backend.b
    late = NoiseRuleRow(
        rule_id="NR-2", scope="pattern", value="^Sprint", reason="tracking", created_by="ann", created_at=LATER
    )
    early = NoiseRuleRow(
        rule_id="NR-1", scope="document", value="upload:roadmap", reason="plan", created_by="ann", created_at=NOW
    )

    await probe.add_noise_rule(a, late)
    await probe.add_noise_rule(a, early)

    assert tuple(await records.noise_rules(a)) == (early, late)
    assert tuple(await records.noise_rules(b)) == ()


async def suppressions_answer_per_project(backend: Backend) -> None:
    records, a, b = backend.records, backend.a, backend.b
    row = SuppressionRow(
        signature="sig-1",
        gap_id="GAP::A1B2C3D4E5F6",
        question="Can guests place orders?",
        reason="out of scope",
        dismissed_by="ann",
        dismissed_at=NOW,
    )

    await records.suppress(a, row)
    await records.suppress(a, row.model_copy(update={"reason": "still out of scope"}))

    assert await records.suppressed(a, ["sig-1", "sig-2"]) == frozenset({"sig-1"})
    assert await records.suppressed(b, ["sig-1"]) == frozenset()
    assert await records.suppressed(a, []) == frozenset()


# Amendments


async def amendments_are_read_by_lineage_and_clarification(backend: Backend) -> None:
    amendments, a, b = backend.amendments, backend.a, backend.b
    second = amendment("AMD::2", created_at=LATER)
    first = amendment("AMD::1")
    other_doc = amendment("AMD::3", lineage="upload:payments-spec", clr_id="CLR::2")
    reverted = amendment("AMD::4", status="reverted")

    await amendments.put(a, [second, first, other_doc, reverted])
    await amendments.put(b, [amendment("AMD::9", lineage="upload:account-spec", clr_id="CLR::9")])

    assert tuple(await amendments.for_lineage(a, "upload:orders-spec")) == (first, second)
    statuses = ("active", "reverted")
    assert tuple(await amendments.for_lineage(a, "upload:orders-spec", statuses=statuses)) == (first, reverted, second)
    assert tuple(await amendments.for_clarification(a, "CLR::1")) == (first, reverted, second)
    assert tuple(await amendments.for_lineage(b, "upload:orders-spec")) == ()
    assert tuple(await amendments.for_clarification(b, "CLR::1")) == ()
    assert tuple(await amendments.pending_lineages(a)) == ("upload:orders-spec", "upload:payments-spec")
    assert tuple(await amendments.pending_lineages(b)) == ("upload:account-spec",)
    assert tuple(await amendments.with_status(a, ("active",))) == (first, other_doc, second)
    assert tuple(await amendments.with_status(a, ("reverted", "pending"))) == (reverted,)
    assert tuple(await amendments.with_status(b, ("reverted",))) == ()

    changed = first.model_copy(update={"text": "Guests and members can place orders."})
    await amendments.put(a, [first, changed])
    assert (await amendments.for_lineage(a, "upload:orders-spec"))[0] == changed
    await amendments.put(a, [])


async def amendment_status_changes_only_in_its_project(backend: Backend) -> None:
    amendments, a, b = backend.amendments, backend.a, backend.b
    await amendments.put(a, [amendment("AMD::1"), amendment("AMD::2")])
    await amendments.put(b, [amendment("AMD::1")])

    await amendments.set_status(a, ["AMD::1", "AMD::404"], "suspended", revision_id="rev-2")
    await amendments.set_status(b, ["AMD::2"], "orphaned")
    await amendments.set_status(a, [], "orphaned")
    with pytest.raises(ConformanceError, match="gap_amendment_status_unknown"):
        await amendments.set_status(a, ["AMD::1"], "bogus")

    (suspended,) = await amendments.for_lineage(a, "upload:orders-spec", statuses=("suspended",))
    assert (suspended.amendment_id, suspended.last_revision_id) == ("AMD::1", "rev-2")
    assert suspended.updated_at > NOW
    assert [row.amendment_id for row in await amendments.for_lineage(a, "upload:orders-spec")] == ["AMD::2"]
    assert tuple(await amendments.for_lineage(b, "upload:orders-spec")) == (amendment("AMD::1"),)

    await amendments.set_status(a, ["AMD::1"], "active")
    await amendments.set_status(a, ["AMD::2"], "retired")
    rows = await amendments.for_clarification(a, "CLR::1")
    assert [(row.amendment_id, row.status, row.last_revision_id) for row in rows] == [
        ("AMD::1", "active", "rev-2"),
        ("AMD::2", "retired", None),
    ]
    assert tuple(await amendments.pending_lineages(a)) == ("upload:orders-spec",)
    await amendments.set_status(a, ["AMD::1"], "undecided")  # the corpus reader still applies it (FR-GREC-08)
    assert tuple(await amendments.pending_lineages(a)) == ("upload:orders-spec",)
    await amendments.set_status(a, ["AMD::1"], "retired")
    assert tuple(await amendments.pending_lineages(a)) == ()


async def noise_rules_are_put_and_deleted_per_project(backend: Backend) -> None:
    records, a, b = backend.records, backend.a, backend.b
    rule = NoiseRuleRow(
        rule_id="NR-1", scope="section", value="Release plan", reason="plan", created_by="ann", created_at=NOW
    )

    await records.put_noise_rule(a, rule)
    await records.put_noise_rule(a, rule.model_copy(update={"reason": "release plan"}))

    assert [row.reason for row in await records.noise_rules(a)] == ["release plan"]
    assert not await records.delete_noise_rule(b, "NR-1")
    assert tuple(await records.noise_rules(a))
    assert await records.delete_noise_rule(a, "NR-1")
    assert not await records.delete_noise_rule(a, "NR-1")
    assert tuple(await records.noise_rules(a)) == ()


async def suppressions_are_removed_per_project(backend: Backend) -> None:
    records, a, b = backend.records, backend.a, backend.b
    row = SuppressionRow(
        signature="sig-1", gap_id=None, question="q?", reason="not a gap", dismissed_by="ann", dismissed_at=NOW
    )
    await records.suppress(a, row)
    await records.suppress(b, row)

    await records.unsuppress(a, "sig-1")
    await records.unsuppress(a, "sig-404")

    assert await records.suppressed(a, ["sig-1"]) == frozenset()
    assert await records.suppressed(b, ["sig-1"]) == frozenset({"sig-1"})


async def amendments_are_deleted_only_in_their_project(backend: Backend) -> None:
    amendments, a, b = backend.amendments, backend.a, backend.b
    await amendments.put(a, [amendment("AMD::1", status="pending"), amendment("AMD::2")])
    await amendments.put(b, [amendment("AMD::1")])

    await amendments.delete(a, ["AMD::1", "AMD::404"])
    await amendments.delete(a, [])

    assert [row.amendment_id for row in await amendments.for_clarification(a, "CLR::1")] == ["AMD::2"]
    assert [row.amendment_id for row in await amendments.for_clarification(b, "CLR::1")] == ["AMD::1"]


CASES: Final[tuple[Case, ...]] = (
    options_are_replaced_and_read_by_rank,
    invalid_option_sets_are_refused,
    runs_start_and_finish_in_their_project,
    seeds_and_sites_belong_to_their_run,
    notes_take_the_run_and_events_are_logged,
    events_are_read_per_gap_in_time_order,
    verdicts_are_cached_per_project,
    relevance_is_read_by_section_key,
    noise_rules_are_listed_per_project,
    suppressions_answer_per_project,
    amendments_are_read_by_lineage_and_clarification,
    amendment_status_changes_only_in_its_project,
    noise_rules_are_put_and_deleted_per_project,
    suppressions_are_removed_per_project,
    amendments_are_deleted_only_in_their_project,
)
