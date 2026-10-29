"""Tier 2: batch start / add / end on a real Postgres (SPECS 3.4, 4.4, 11).

Requirements: INT-SRC-0004, FR-SRC-0020, FR-SRC-0021, FR-SRC-0022, DATA-SRC-0003,
NFR-REL-0003, R02, R06, R10, R11, M1 (review-01).
``SPECFLOW_TEST_POSTGRES=1``; see specflow_db_live_support.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from db import batches, source_files
from db.batches import BatchEndedError, LockConflictError, SourceNotFoundError
from db.removal import remove_source
from specflow_live import requires_live
from specflow_sources_live_support import (
    add_doc,
    add_item,
    make_batch,
    make_drive,
)
from specflow_sources_write_support import (
    add_file,
    doc_row,
    item_count,
    rows,
    scalar,
    source_exists,
    tx,
    wait_until_blocked,
)

pytestmark = requires_live("POSTGRES")

MIN = timedelta(minutes=1)


# Lifecycle: start, add, end ------------------------------------------------------------


async def test_start_add_end_makes_a_card_with_the_files(sources_world) -> None:
    w = sources_world

    async def start(s):
        return (await batches.start_batch(s, w.project_a, w.user_id, 3)).id

    batch = await tx(w, start)

    async def add(s):
        return [await add_file(s, w, batch, f"f{n}") for n in range(3)]

    outcomes = await tx(w, add)
    assert [o.outcome for o in outcomes] == ["added"] * 3
    assert all(o.reparse_document_id == o.document.id and not o.discard_upload for o in outcomes)

    async def check(s):
        for o in outcomes:
            row = await doc_row(s, o.document.id)
            assert (row.status, row.source_id) == ("processing", batch)
        summary = await source_files.source_summary(s, w.project_a, batch)
        assert (summary.total, summary.open, summary.counts["processing"]) == (3, True, 3)

    await tx(w, check)

    async def end(s):
        return await batches.end_batch(s, w.project_a, batch)

    ended = await tx(w, end)
    assert ended is not None and ended.ended_at is not None and ended.state == "done"
    again = await tx(w, end)  # idempotent
    assert again is not None and again.ended_at == ended.ended_at

    async def after(s):
        summary = await source_files.source_summary(s, w.project_a, batch)
        assert summary is not None and summary.total == 3 and not summary.open

    await tx(w, after)


async def test_the_end_of_an_empty_batch_deletes_it(sources_world) -> None:
    w = sources_world
    batch = await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 2))
    assert await tx(w, lambda s: batches.end_batch(s, w.project_a, batch.id)) is None
    assert not await tx(w, lambda s: source_exists(s, batch.id))
    with pytest.raises(SourceNotFoundError):
        await tx(w, lambda s: batches.end_batch(s, w.project_a, batch.id))


async def test_end_refuses_another_projects_batch_and_a_drive_source(sources_world) -> None:
    w = sources_world
    batch = await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 1))
    drive = await tx(w, lambda s: make_drive(s, w.project_a, w.user_id))
    with pytest.raises(SourceNotFoundError):
        await tx(w, lambda s: batches.end_batch(s, w.project_b, batch.id))
    with pytest.raises(SourceNotFoundError):
        await tx(w, lambda s: batches.end_batch(s, w.project_a, drive))


async def test_same_minute_batches_get_the_next_suffix_under_the_project_lock(sources_world) -> None:
    w = sources_world

    async def two(s):
        first = await batches.start_batch(s, w.project_a, w.user_id, 1)
        second = await batches.start_batch(s, w.project_a, w.user_id, 1)
        return first.name_suffix, second.name_suffix

    assert await tx(w, two) == (1, 2)

    async def concurrent():
        return await asyncio.gather(
            *(tx(w, lambda s: batches.start_batch(s, w.project_b, w.user_id, 1)) for _ in range(4))
        )

    started = await concurrent()
    by_minute: dict[datetime, list[int]] = {}
    for batch in started:
        by_minute.setdefault(batch.created_at.replace(second=0, microsecond=0), []).append(batch.name_suffix)
    for suffixes in by_minute.values():  # a minute boundary may split the four
        assert sorted(suffixes) == list(range(1, len(suffixes) + 1))


async def test_a_batch_closes_after_15_minutes_and_refuses_files(sources_world) -> None:
    w = sources_world

    async def build(s):
        old = await make_batch(s, w.project_a, w.user_id, expected=2, age=16 * MIN)
        await add_item(s, old, w.project_a, "old", "pending", age=16 * MIN)
        recent = await make_batch(s, w.project_a, w.user_id, expected=2, age=16 * MIN)
        await add_item(s, recent, w.project_a, "recent", "pending", age=5 * MIN)
        return old, recent

    old, recent = await tx(w, build)
    with pytest.raises(BatchEndedError):
        await tx(w, lambda s: add_file(s, w, old, "late"))
    assert (await tx(w, lambda s: add_file(s, w, recent, "ontime"))).outcome == "added"

    async def summary(s):
        return await source_files.source_summary(s, w.project_a, old)

    closed = await tx(w, summary)
    assert closed is not None and not closed.open and closed.total == 1


async def test_an_ended_or_drive_source_refuses_files_unless_legacy(sources_world) -> None:
    w = sources_world

    async def build(s):
        return (
            await make_batch(s, w.project_a, w.user_id, ended=True),
            await make_drive(s, w.project_a, w.user_id),
        )

    ended, drive = await tx(w, build)
    with pytest.raises(BatchEndedError):
        await tx(w, lambda s: add_file(s, w, ended, "x"))
    with pytest.raises(BatchEndedError):
        await tx(w, lambda s: add_file(s, w, drive, "x"))
    assert (await tx(w, lambda s: add_file(s, w, ended, "x", allow_ended=True))).outcome == "added"
    with pytest.raises(SourceNotFoundError):
        await tx(w, lambda s: add_file(s, w, ended, "y", project_id=w.project_b))


# Hidden batches ----------------------------------------------------------------------


async def test_start_deletes_hidden_upload_batches_but_never_a_drive_source(sources_world) -> None:
    w = sources_world

    async def build(s):
        ids = {
            "stale_empty": await make_batch(s, w.project_a, w.user_id, age=20 * MIN),
            "ended_empty": await make_batch(s, w.project_a, w.user_id, ended=True),
            "open_empty": await make_batch(s, w.project_a, w.user_id, age=2 * MIN),
            "stale_with_file": await make_batch(s, w.project_a, w.user_id, age=20 * MIN),
            "listing": await make_drive(s, w.project_a, w.user_id, state="listing", age=30 * MIN),
            "listed_empty": await make_drive(s, w.project_a, w.user_id, state="done", age=30 * MIN),
            "other_project": await make_batch(s, w.project_b, w.user_id, age=20 * MIN),
        }
        await add_item(s, ids["stale_with_file"], w.project_a, "kept", "pending", age=20 * MIN)
        return ids

    ids = await tx(w, build)
    await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 1))

    async def present(s):
        return {name: await source_exists(s, sid) for name, sid in ids.items()}

    assert await tx(w, present) == {
        "stale_empty": False,
        "ended_empty": False,
        "open_empty": True,
        "stale_with_file": True,
        "listing": True,  # R02
        "listed_empty": True,  # R02
        "other_project": True,
    }


async def test_start_adopts_old_uploads_into_their_day_batch(sources_world) -> None:
    w = sources_world
    doc = await tx(w, lambda s: add_doc(s, w.project_a, "old", status="parsed", origin="upload"))
    await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 1))
    held = await tx(
        w,
        lambda s: scalar(
            s, "SELECT c.external_id FROM documents d JOIN connections c ON c.id = d.source_id WHERE d.id = :i", i=doc
        ),
    )
    assert held.startswith("legacy-")


# Outcomes of add ---------------------------------------------------------------------


async def test_the_same_content_twice_in_one_pick_is_added_once(sources_world) -> None:
    w = sources_world
    batch = (await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 2))).id
    first = await tx(w, lambda s: add_file(s, w, batch, "same"))
    second = await tx(w, lambda s: add_file(s, w, batch, "same"))
    assert (first.outcome, second.outcome) == ("added", "duplicate")
    assert second.discard_upload and second.reparse_document_id is None
    assert second.document.id == first.document.id
    assert await tx(w, lambda s: item_count(s, batch)) == 1
    assert await tx(w, lambda s: scalar(s, "SELECT count(*) FROM documents WHERE project_id = :p", p=w.project_a)) == 1


async def test_held_elsewhere_is_already_in_project_and_twice_in_a_pick_is_one_item(sources_world) -> None:
    w = sources_world

    async def build(s):
        other = await make_batch(s, w.project_a, w.user_id, ended=True)
        doc = await add_doc(s, w.project_a, "shared", status="parsed", source_id=other)
        await add_item(s, other, w.project_a, "shared", "pending", document_id=doc)
        return other, doc

    other, doc = await tx(w, build)
    batch = (await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 2))).id
    first = await tx(w, lambda s: add_file(s, w, batch, "shared"))
    assert first.outcome == "already_in_project" and first.holder.id == other and first.discard_upload
    assert (first.item.state, first.item.document_id) == ("duplicate", doc)
    second = await tx(w, lambda s: add_file(s, w, batch, "shared"))
    assert second.outcome == "duplicate"
    assert await tx(w, lambda s: item_count(s, batch)) == 1
    assert (await tx(w, lambda s: doc_row(s, doc))).source_id == other  # nothing moved


@pytest.mark.parametrize("status", ["failed", "processing"])
async def test_a_failed_drive_duplicate_is_already_in_project(sources_world, status) -> None:
    """SD6: a Failed file of a Drive source is never moved by an upload."""

    w = sources_world

    async def build(s):
        drive = await make_drive(s, w.project_a, w.user_id)
        age = timedelta(minutes=90) if status == "processing" else None
        doc = await add_doc(
            s,
            w.project_a,
            "gd",
            status=status,
            origin="connection",
            source_id=drive,
            updated_ago=age,
            error="pdf_parse_failed" if status == "failed" else None,
        )
        await add_item(s, drive, w.project_a, "gd", "processed", document_id=doc)
        return drive, doc

    drive, doc = await tx(w, build)
    batch = (await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 1))).id
    outcome = await tx(w, lambda s: add_file(s, w, batch, "gd"))
    assert outcome.outcome == "already_in_project" and outcome.holder.id == drive
    assert (await tx(w, lambda s: doc_row(s, doc))).source_id == drive


@pytest.mark.parametrize(
    ("status", "age_min", "moved"),
    [
        ("failed", 0, True),
        ("processing", 61, True),  # R10
        ("uploaded", 61, True),  # M1
        ("processing", 59, False),
        ("uploaded", 59, False),
        ("parsed", 500, False),
    ],
)
async def test_a_failed_upload_moves_to_the_new_batch_and_the_empty_old_batch_goes(
    sources_world, status, age_min, moved
) -> None:
    w = sources_world

    async def build(s):
        old = await make_batch(s, w.project_a, w.user_id, ended=True)
        doc = await add_doc(
            s,
            w.project_a,
            "again",
            status=status,
            source_id=old,
            updated_ago=age_min * MIN,
            error="pdf_parse_failed" if status == "failed" else None,
        )
        await add_item(s, old, w.project_a, "again", "pending", document_id=doc)
        return old, doc

    old, doc = await tx(w, build)
    batch = (await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 1))).id
    outcome = await tx(w, lambda s: add_file(s, w, batch, "again"))

    async def check(s):
        row = await doc_row(s, doc)
        if moved:
            assert outcome.outcome == "read_again" and outcome.holder is None
            assert outcome.reparse_document_id == doc and outcome.discard_upload
            assert (row.status, row.source_id, row.error) == ("processing", batch, None)
            assert await item_count(s, batch) == 1
            assert not await source_exists(s, old)  # empty and not open: deleted
        else:
            assert outcome.outcome == "already_in_project" and outcome.holder.id == old
            assert row.source_id == old and row.status == status
            assert await source_exists(s, old)

    await tx(w, check)


async def test_a_move_keeps_the_old_batch_while_it_has_files_or_is_open(sources_world) -> None:
    w = sources_world

    async def build(s):
        full = await make_batch(s, w.project_a, w.user_id, ended=True)
        opened = await make_batch(s, w.project_a, w.user_id, age=1 * MIN)
        docs = []
        for batch, label in ((full, "m1"), (opened, "m2")):
            docs.append(
                await add_doc(s, w.project_a, label, status="failed", source_id=batch, error="pdf_parse_failed")
            )
            await add_item(s, batch, w.project_a, label, "pending", document_id=docs[-1])
        await add_item(s, full, w.project_a, "other", "pending")
        return full, opened

    full, opened = await tx(w, build)
    batch = (await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 2))).id
    assert (await tx(w, lambda s: add_file(s, w, batch, "m1"))).outcome == "read_again"
    assert (await tx(w, lambda s: add_file(s, w, batch, "m2"))).outcome == "read_again"
    assert await tx(w, lambda s: source_exists(s, full))
    assert await tx(w, lambda s: source_exists(s, opened))  # open, though empty now


async def test_an_orphan_document_is_adopted_on_add(sources_world) -> None:
    w = sources_world
    batch = (await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 1))).id
    orphan = await tx(w, lambda s: add_doc(s, w.project_a, "orphan", status="parsed", origin="upload"))
    outcome = await tx(w, lambda s: add_file(s, w, batch, "orphan"))
    assert outcome.outcome == "already_in_project"
    assert outcome.holder is not None and outcome.holder.external_id.startswith("legacy-")
    assert (await tx(w, lambda s: doc_row(s, orphan))).source_id == outcome.holder.id


async def test_the_legacy_day_batch_is_found_or_created_once_and_takes_files(sources_world) -> None:
    w = sources_world
    now = datetime(2026, 10, 21, 23, 59, 30, tzinfo=UTC)

    async def one(s):
        return await batches.legacy_day_batch(s, w.project_a, w.user_id, now)

    first, second = await asyncio.gather(tx(w, one), tx(w, one))
    assert first.id == second.id and first.external_id == "legacy-20261021"
    assert first.ended_at is not None and first.link is None and first.name_suffix == 1
    outcome = await tx(w, lambda s: add_file(s, w, first.id, "late", allow_ended=True))
    assert outcome.outcome == "added"
    later = await tx(w, lambda s: batches.legacy_day_batch(s, w.project_a, w.user_id, now - timedelta(minutes=5)))
    assert later.id == first.id
    next_day = await tx(w, lambda s: batches.legacy_day_batch(s, w.project_a, w.user_id, now + timedelta(minutes=1)))
    assert next_day.external_id == "legacy-20261022" and next_day.name_suffix == 1


# Locks -------------------------------------------------------------------------------


async def test_a_move_and_the_removal_of_the_old_batch_never_end_in_a_partial_state(sources_world) -> None:
    """R11: whichever runs first, one document row exists afterwards, held by the new batch."""

    w = sources_world

    async def build(s):
        old = await make_batch(s, w.project_a, w.user_id, ended=True)
        doc = await add_doc(s, w.project_a, "race", status="failed", source_id=old, error="pdf_parse_failed")
        await add_item(s, old, w.project_a, "race", "pending", document_id=doc)
        return old

    old = await tx(w, build)
    batch = (await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 1))).id

    results = await asyncio.gather(
        tx(w, lambda s: add_file(s, w, batch, "race")),
        tx(w, lambda s: remove_source(s, w.project_a, old)),
        return_exceptions=True,
    )
    for result in results:
        assert not isinstance(result, Exception) or isinstance(result, (SourceNotFoundError, LockConflictError))

    async def check(s):
        found = await rows(s, "SELECT source_id, status FROM documents WHERE project_id = :p", p=w.project_a)
        assert len(found) <= 1
        if found:
            assert found[0].source_id == batch
        assert not await source_exists(s, old)

    await tx(w, check)


async def test_a_deadlock_is_a_typed_lock_conflict_with_nothing_half_done(sources_world) -> None:
    """Session 1 holds a connection row and then asks for the project lock; the add holds the
    project lock and then asks for the same connection row. Postgres aborts the add (it waited
    first), and the add reports ``LockConflictError``."""

    w = sources_world

    async def build(s):
        old = await make_batch(s, w.project_a, w.user_id, ended=True)
        doc = await add_doc(s, w.project_a, "dead", status="failed", source_id=old, error="pdf_parse_failed")
        await add_item(s, old, w.project_a, "dead", "pending", document_id=doc)
        return old, doc

    old, doc = await tx(w, build)
    batch = (await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 1))).id

    async with w.factory() as blocker, blocker.begin():
        await blocker.execute(text("SELECT 1 FROM connections WHERE id = :i FOR UPDATE"), {"i": old})
        adder = asyncio.create_task(tx(w, lambda s: add_file(s, w, batch, "dead")))
        await wait_until_blocked(w)  # the add holds the project lock and waits for `old`
        grab = asyncio.create_task(
            blocker.execute(text("SELECT 1 FROM projects WHERE id = :p FOR NO KEY UPDATE"), {"p": w.project_a})
        )
        with pytest.raises(LockConflictError):
            await asyncio.wait_for(adder, 20)
        await asyncio.wait_for(grab, 20)

    async def check(s):
        row = await doc_row(s, doc)
        assert (row.status, row.source_id) == ("failed", old)
        assert await item_count(s, batch) == 0

    await tx(w, check)


async def test_a_write_queued_behind_the_project_lock_gets_a_lock_timeout(sources_world, monkeypatch) -> None:
    """M1 (review-02): add, start and legacy batch wait for the project lock only up to the limit."""

    w = sources_world
    monkeypatch.setattr(batches, "LOCK_TIMEOUT", "1s")
    batch = (await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 2))).id
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)

    async with w.factory() as holder, holder.begin():
        await holder.execute(text("SELECT 1 FROM projects WHERE id = :p FOR NO KEY UPDATE"), {"p": w.project_a})
        started = asyncio.get_running_loop().time()
        with pytest.raises(batches.LockTimeoutError):
            await tx(w, lambda s: add_file(s, w, batch, "queued"))
        with pytest.raises(batches.LockTimeoutError):
            await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 1))
        with pytest.raises(batches.LockTimeoutError):
            await tx(w, lambda s: batches.legacy_day_batch(s, w.project_a, w.user_id, now))
        assert asyncio.get_running_loop().time() - started < 15

    async def check(s):
        assert await item_count(s, batch) == 0
        assert await scalar(s, "SELECT count(*) FROM documents WHERE project_id = :p", p=w.project_a) == 0

    await tx(w, check)
    assert (await tx(w, lambda s: add_file(s, w, batch, "queued"))).outcome == "added"  # free again


async def test_an_add_and_the_removal_of_the_same_batch_are_serialised(sources_world) -> None:
    w = sources_world

    async def build(s):
        batch = await make_batch(s, w.project_a, w.user_id, age=1 * MIN)
        doc = await add_doc(s, w.project_a, "first", status="parsed", source_id=batch)
        await add_item(s, batch, w.project_a, "first", "pending", document_id=doc)
        return batch

    batch = await tx(w, build)
    added, removed = await asyncio.gather(
        tx(w, lambda s: add_file(s, w, batch, "second")),
        tx(w, lambda s: remove_source(s, w.project_a, batch)),
        return_exceptions=True,
    )
    assert not isinstance(removed, Exception)
    if isinstance(added, Exception):  # the removal went first
        assert isinstance(added, SourceNotFoundError) and removed.file_count == 1
    else:  # the add went first and the removal took both files
        assert added.outcome == "added" and removed.file_count == 2

    async def check(s):
        assert not await source_exists(s, batch)
        assert await scalar(s, "SELECT count(*) FROM documents WHERE project_id = :p", p=w.project_a) == 0

    await tx(w, check)


async def test_two_sessions_adding_the_same_content_give_one_added_and_one_already_in_project(sources_world) -> None:
    w = sources_world
    first = (await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 1))).id
    second = (await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 1))).id
    results = await asyncio.gather(
        tx(w, lambda s: add_file(s, w, first, "twin")),
        tx(w, lambda s: add_file(s, w, second, "twin")),
    )
    assert sorted(r.outcome for r in results) == ["added", "already_in_project"]
    winner = next(r for r in results if r.outcome == "added")
    loser = next(r for r in results if r.outcome == "already_in_project")
    assert loser.holder.id == winner.document.source_id
    assert await tx(w, lambda s: scalar(s, "SELECT count(*) FROM documents WHERE project_id = :p", p=w.project_a)) == 1


async def test_already_in_project_and_the_removal_of_its_holder_end_in_one_held_document(sources_world) -> None:
    w = sources_world

    async def build(s):
        holder = await make_batch(s, w.project_a, w.user_id, ended=True)
        doc = await add_doc(s, w.project_a, "kept", status="parsed", source_id=holder)
        await add_item(s, holder, w.project_a, "kept", "pending", document_id=doc)
        return holder

    holder = await tx(w, build)
    batch = (await tx(w, lambda s: batches.start_batch(s, w.project_a, w.user_id, 1))).id
    added, removed = await asyncio.gather(
        tx(w, lambda s: add_file(s, w, batch, "kept")),
        tx(w, lambda s: remove_source(s, w.project_a, holder)),
        return_exceptions=True,
    )
    assert not isinstance(added, Exception) and not isinstance(removed, Exception), (added, removed)
    # add first: the item lists the document, so it is handed over; removal first: it is deleted
    # and the add stores the content again
    assert (added.outcome, removed.handed_over != []) in (("already_in_project", True), ("added", False))

    async def check(s):
        assert not await source_exists(s, holder)
        found = await rows(s, "SELECT source_id FROM documents WHERE project_id = :p", p=w.project_a)
        assert [r.source_id for r in found] == [batch]
        assert await item_count(s, batch) == 1

    await tx(w, check)
