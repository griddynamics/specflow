"""Tier 2: the read model on a migrated scratch database: SQL == the Python mirror.

Requirements: DATA-SRC-0001, INT-SRC-0001, INT-SRC-0002, NFR-PERF-0002.
``SPECFLOW_TEST_POSTGRES=1``; see specflow_db_live_support.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from db import source_files as sf
from db.source_files import file_state
from specflow_live import requires_live
from specflow_sources_live_support import (
    SourcesWorld,
    add_doc,
    add_item,
    make_batch,
    make_drive,
)

pytestmark = requires_live("POSTGRES")

STALE = sf.UPLOAD_STALE_AFTER + timedelta(minutes=1)
FRESH = timedelta(minutes=5)
OLD = timedelta(hours=13)  # past DRIVE_IMPORT_EXECUTION_TIMEOUT and UPLOAD_OPEN_FOR
ERROR_REASONS = (
    "no_access",
    "drive_unavailable",
    "convert_failed",
    "parse_failed",
    "store_failed",
    "import_failed",
    "unknown_error",
)

# (label, item state, item error reason, doc status, doc error, doc age, holds, doc held elsewhere)
ITEM_SCENARIOS: list[tuple[str, str, str | None, str | None, str | None, timedelta | None, bool]] = [
    *[
        (f"nodoc-{s}", s, None, None, None, None, False)
        for s in ("pending", "processing", "processed", "stored", "skipped")
    ],
    *[(f"err-{r}", "error", r, None, None, None, False) for r in ERROR_REASONS],
    ("hold-uploaded-59", "pending", None, "uploaded", None, timedelta(minutes=59), True),
    ("hold-uploaded-61", "pending", None, "uploaded", None, timedelta(minutes=61), True),
    ("hold-uploaded-61-parse", "pending", None, "uploaded", "pdf_parse_failed", timedelta(minutes=61), True),
    ("hold-processing-fresh", "pending", None, "processing", None, FRESH, True),
    ("hold-processing-stale", "pending", None, "processing", None, STALE, True),
    ("hold-processing-stale-parse", "pending", None, "processing", "pdf_parse_failed", STALE, True),
    ("hold-parsed", "pending", None, "parsed", None, FRESH, True),
    ("hold-ingested", "pending", None, "ingested", None, FRESH, True),
    ("hold-partial", "pending", None, "partially_ingested", None, FRESH, True),
    ("hold-failed", "pending", None, "failed", None, FRESH, True),
    ("hold-failed-parse", "pending", None, "failed", "pdf_parse_failed", FRESH, True),
    ("hold-failed-other", "pending", None, "failed", "workflow_start_failed", FRESH, True),
    ("hold-processed-item", "processed", None, "uploaded", None, FRESH, True),
    ("hold-error-item", "error", "parse_failed", "parsed", None, FRESH, True),
    ("elsewhere-processed", "processed", None, "failed", None, FRESH, False),
    ("elsewhere-pending", "pending", None, "uploaded", None, FRESH, False),
]


async def fill(
    session: AsyncSession,
    world: SourcesWorld,
    source: uuid.UUID,
    other: uuid.UUID,
    tag: str,
    *,
    item_age: timedelta | None,
) -> dict[str, tuple[str, str | None, bool]]:
    """Every scenario in one source; label -> (item state, reason, holds)."""

    created: dict[str, tuple[str, str | None, bool]] = {}
    for label, state, reason, status, doc_error, age, holds in ITEM_SCENARIOS:
        document = None
        if status is not None:
            document = await add_doc(
                session,
                world.project_a,
                f"{tag}-{label}",
                status=status,
                error=doc_error,
                updated_ago=age,
                source_id=source if holds else other,
            )
        await add_item(
            session,
            source,
            world.project_a,
            f"{tag}-{label}",
            state,
            error_reason=reason,
            document_id=document,
            age=item_age,
            name=f"{tag}-{label}.pdf",
        )
        created[f"{tag}-{label}.pdf"] = (state, reason, holds)
    # A duplicate: the holder is the other source, or no source at all.
    held = await add_doc(session, world.project_a, f"{tag}-dup", status="parsed", source_id=other)
    unheld = await add_doc(session, world.project_a, f"{tag}-dup-unheld", status="parsed")
    await add_item(
        session,
        source,
        world.project_a,
        f"{tag}-dup",
        "duplicate",
        document_id=held,
        age=item_age,
        name=f"{tag}-dup.pdf",
    )
    await add_item(
        session,
        source,
        world.project_a,
        f"{tag}-dup-unheld",
        "duplicate",
        document_id=unheld,
        age=item_age,
        name=f"{tag}-dup-unheld.pdf",
    )
    created[f"{tag}-dup.pdf"] = ("duplicate", None, False)
    created[f"{tag}-dup-unheld.pdf"] = ("duplicate", None, False)
    return created


def expected_for(
    scenarios: dict[str, tuple[str, str | None, bool]], *, source_failed: bool
) -> dict[str, sf.FileOutcome]:
    doc = {s[0]: s for s in ITEM_SCENARIOS}
    out: dict[str, sf.FileOutcome] = {}
    for name, (state, reason, holds) in scenarios.items():
        label = name.split("-", 1)[1].removesuffix(".pdf")
        _, _, _, status, doc_error, age, _ = doc.get(label, (label, state, None, None, None, None, False))
        out[name] = file_state(
            source_failed=source_failed,
            item_state=state,
            item_error_reason=reason,
            holds=holds,
            doc_status=status,
            doc_error=doc_error,
            doc_age=age,
        )
    return out


async def every_file(session: AsyncSession, world: SourcesWorld, source: uuid.UUID) -> dict[str, sf.FileRow]:
    rows: dict[str, sf.FileRow] = {}
    cursor = None
    while True:
        page = await sf.file_page(session, world.project_a, source, cursor, sf.PAGE_LIMIT)
        rows.update({row.name: row for row in page.items})
        if page.next_cursor is None:
            return rows
        cursor = sf.decode_file_cursor(page.next_cursor)


@pytest.mark.parametrize(
    ("kind", "state", "age", "failure_reason", "source_failed"),
    [
        ("gdrive", "importing", None, None, False),
        ("gdrive", "failed", None, "no_access", True),
        (
            "gdrive",
            "importing",
            OLD,
            None,
            True,
        ),  # stale: failed by the clock, never written
        (
            "upload",
            "importing",
            OLD,
            None,
            False,
        ),  # R03: an upload's state is never read
    ],
    ids=["drive-running", "drive-failed", "drive-stale", "upload-importing-13h"],
)
async def test_the_sql_equals_the_mirror_for_every_scenario(
    sources_world: SourcesWorld,
    kind: str,
    state: str,
    age: timedelta | None,
    failure_reason: str | None,
    *,
    source_failed: bool,
) -> None:
    world = sources_world
    async with world.factory() as session, session.begin():
        other = await make_drive(session, world.project_a, world.user_id, state="done", name="Elsewhere")
        if kind == "gdrive":
            source = await make_drive(
                session, world.project_a, world.user_id, state=state, failure_reason=failure_reason, age=age
            )
        else:
            source = await make_batch(session, world.project_a, world.user_id, age=age, state=state)
        scenarios = await fill(session, world, source, other, "s", item_age=age)
    expected = expected_for(scenarios, source_failed=source_failed)

    async with world.factory() as session, session.begin():
        found = await every_file(session, world, source)
        summary = await sf.source_summary(session, world.project_a, source)

    assert set(found) == set(expected)
    for name, outcome in expected.items():
        assert (found[name].state, found[name].reason) == outcome, name
    assert summary is not None
    assert summary.counts == {s: Counter(o.state for o in expected.values()).get(s, 0) for s in sf.FILE_STATES}
    assert summary.total == len(expected) == sum(summary.counts.values())
    assert summary.open is False and summary.listing_finished is True
    if kind == "upload":
        assert summary.failure_reason is None and summary.folder_id is None and summary.link is None
        assert summary.counts["failed"] == Counter(o.state for o in expected.values())["failed"]
    elif source_failed:
        assert summary.failure_reason == (failure_reason or "unknown_error")
    # a held duplicate shows its holder; an unheld one shows none (R15)
    assert found["s-dup.pdf"].holder is not None and found["s-dup.pdf"].holder.name == "Elsewhere"
    assert found["s-dup-unheld.pdf"].holder is None
    assert found["s-nodoc-pending.pdf"].holder is None


async def test_an_upload_batch_importing_for_13h_with_all_files_done_reads_done(
    sources_world: SourcesWorld,
) -> None:
    world = sources_world
    async with world.factory() as session, session.begin():
        batch = await make_batch(session, world.project_a, world.user_id, age=OLD, state="importing")
        for n in range(3):
            document = await add_doc(session, world.project_a, f"d{n}", status="parsed", source_id=batch)
            await add_item(
                session,
                batch,
                world.project_a,
                f"d{n}",
                "pending",
                document_id=document,
                age=OLD,
            )
        summary = await sf.source_summary(session, world.project_a, batch)

    assert summary is not None
    assert (summary.counts["done"], summary.counts["failed"], summary.total) == (
        3,
        0,
        3,
    )
    assert summary.open is False and summary.failure_reason is None and summary.listing_finished


async def test_an_open_batch_adds_the_files_still_to_come_to_waiting(
    sources_world: SourcesWorld,
) -> None:
    world = sources_world
    async with world.factory() as session, session.begin():
        batch = await make_batch(session, world.project_a, world.user_id, expected=10)
        for n in range(2):
            document = await add_doc(session, world.project_a, f"o{n}", status="parsed", source_id=batch)
            await add_item(
                session,
                batch,
                world.project_a,
                f"o{n}",
                "pending",
                document_id=document,
            )
        document = await add_doc(session, world.project_a, "o2", status="uploaded", source_id=batch)
        await add_item(session, batch, world.project_a, "o2", "pending", document_id=document)

        summary = await sf.source_summary(session, world.project_a, batch)

    assert summary is not None and summary.open and summary.listing_finished
    assert (summary.total, summary.counts["done"], summary.counts["waiting"]) == (
        10,
        2,
        1 + 7,
    )
    assert sum(summary.counts.values()) == summary.total
    assert summary.added_by_name == "Name u" and summary.added_by_id == world.user_id


async def test_an_ended_or_idle_batch_is_closed_and_total_is_the_files_it_has(
    sources_world: SourcesWorld,
) -> None:
    world = sources_world
    async with world.factory() as session, session.begin():
        ended = await make_batch(session, world.project_a, world.user_id, expected=10, ended=True)
        idle = await make_batch(
            session,
            world.project_a,
            world.user_id,
            expected=10,
            age=timedelta(minutes=20),
        )
        for batch, age in ((ended, None), (idle, timedelta(minutes=20))):
            document = await add_doc(session, world.project_a, f"c-{batch}", status="parsed", source_id=batch)
            await add_item(
                session,
                batch,
                world.project_a,
                f"c-{batch}",
                "pending",
                document_id=document,
                age=age,
            )
        summaries = {s.id: s for s in await sf.source_summaries(session, world.project_a)}

    for batch in (ended, idle):
        assert (
            summaries[batch].open,
            summaries[batch].total,
            summaries[batch].counts["waiting"],
        ) == (False, 1, 0)


async def test_a_batch_without_files_is_hidden_unless_it_is_open_and_a_drive_source_never_is(
    sources_world: SourcesWorld,
) -> None:
    world = sources_world
    async with world.factory() as session, session.begin():
        hidden_old = await make_batch(session, world.project_a, world.user_id, expected=5, age=timedelta(hours=1))
        hidden_ended = await make_batch(session, world.project_a, world.user_id, expected=5, ended=True)
        open_empty = await make_batch(session, world.project_a, world.user_id, expected=5)
        listing = await make_drive(session, world.project_a, world.user_id, state="listing")
        empty_folder = await make_drive(session, world.project_a, world.user_id, state="done")

        listed = {s.id: s for s in await sf.source_summaries(session, world.project_a)}
        gone = [await sf.source_summary(session, world.project_a, b) for b in (hidden_old, hidden_ended)]

    assert set(listed) == {open_empty, listing, empty_folder}
    assert gone == [None, None]
    assert (listed[open_empty].total, listed[open_empty].counts["waiting"]) == (5, 5)
    assert (listed[listing].total, listed[listing].listing_finished) == (0, False)
    assert (listed[empty_folder].total, listed[empty_folder].listing_finished) == (
        0,
        True,
    )


async def test_sources_are_listed_oldest_first_and_scoped_to_the_project(
    sources_world: SourcesWorld,
) -> None:
    world = sources_world
    async with world.factory() as session, session.begin():
        first = await make_drive(session, world.project_a, world.user_id, age=timedelta(minutes=3))
        second = await make_drive(session, world.project_a, world.user_id, age=timedelta(minutes=2))
        foreign = await make_drive(session, world.project_b, world.user_id)

        listed = await sf.source_summaries(session, world.project_a)
        assert [s.id for s in listed] == [first, second]
        assert await sf.source_summary(session, world.project_a, foreign) is None
        assert await sf.source_summary(session, world.project_a, uuid.uuid4()) is None
        page = await sf.file_page(session, world.project_a, foreign, None, 10)

    assert page.items == () and page.next_cursor is None


async def test_the_paging_of_450_files_is_200_200_50_in_group_then_name_order(
    sources_world: SourcesWorld,
) -> None:
    world = sources_world
    async with world.factory() as session, session.begin():
        source = await make_drive(session, world.project_a, world.user_id)
        for n in range(450):
            if n < 100:
                await add_item(
                    session,
                    source,
                    world.project_a,
                    f"p{n:03d}",
                    "error",
                    error_reason="no_access",
                    name=f"F{n:03d}.pdf",
                )
            elif n < 200:
                await add_item(
                    session,
                    source,
                    world.project_a,
                    f"p{n:03d}",
                    "processed",
                    name=f"f{n:03d}.pdf",
                )
            else:
                await add_item(
                    session,
                    source,
                    world.project_a,
                    f"p{n:03d}",
                    "pending",
                    name=f"F{n:03d}.pdf",
                )

    sizes: list[int] = []
    seen: list[sf.FileRow] = []
    cursor = None
    async with world.factory() as session, session.begin():
        while True:
            page = await sf.file_page(session, world.project_a, source, cursor, 200)
            sizes.append(len(page.items))
            seen.extend(page.items)
            if page.next_cursor is None:
                break
            cursor = sf.decode_file_cursor(page.next_cursor)

    assert sizes == [200, 200, 50]
    assert len({row.id for row in seen}) == 450
    ranks = [sf.state_rank(row.state) for row in seen]
    assert ranks == sorted(ranks)  # problems (100), then waiting (250), then done (100)
    assert [r.state for r in seen[:100]] == ["failed"] * 100
    assert [r.name.lower() for r in seen[:100]] == sorted(r.name.lower() for r in seen[:100])
    assert Counter(r.state for r in seen) == {
        "failed": 100,
        "waiting": 250,
        "done": 100,
    }


async def test_sweep_candidates_reports_the_source_and_the_ids_without_a_document(
    sources_world: SourcesWorld,
) -> None:
    world = sources_world
    async with world.factory() as session, session.begin():
        source = await make_drive(session, world.project_a, world.user_id)
        kept = await add_doc(session, world.project_a, "kept", source_id=source)
        gone = [uuid.uuid4(), uuid.uuid4()]

        present, missing = await sf.sweep_candidates(session, world.project_a, source, [kept, *gone])
        absent, none_missing = await sf.sweep_candidates(session, world.project_a, uuid.uuid4(), [kept])
        other_project, _ = await sf.sweep_candidates(session, world.project_b, source, [])

    assert present is True and sorted(missing) == sorted(gone)
    assert absent is False and none_missing == []
    assert other_project is False  # another project's id is never "present"
