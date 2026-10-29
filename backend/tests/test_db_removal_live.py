"""Tier 2: ``remove_source`` on a real Postgres (SPECS 5, 11).

Requirements: FR-SRC-0016, FR-SRC-0017, FR-SRC-0021, NFR-REL-0003, SD3, SD7, R01, R11, R14.
``SPECFLOW_TEST_POSTGRES=1``; see specflow_db_live_support.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from db import removal, sources
from db.batches import LockConflictError, SourceNotFoundError
from db.removal import LockTimeoutError, RevisionChainError, remove_source
from specflow_live import requires_live
from specflow_sources_live_support import (
    add_doc,
    add_item,
    make_batch,
    make_drive,
)
from specflow_sources_write_support import (
    doc_row,
    item_count,
    put_drive_doc,
    put_item,
    rows,
    scalar,
    source_exists,
    tx,
    wait_until_blocked,
)
from storage import keys
from temporal.ids import drive_document_id

pytestmark = requires_live("POSTGRES")

MIN = timedelta(minutes=1)


def bin_key(project, source, ext) -> str:
    return keys.document_original_bin(str(project), str(drive_document_id(source, ext)))


def variants(project, source, ext) -> set[str]:
    did = str(drive_document_id(source, ext))
    return {
        keys.document_original(str(project), did),
        keys.document_original_json(str(project), did),
        keys.document_original_bin(str(project), did),
    }


async def removed_files(session, project) -> int:
    return int(await scalar(session, "SELECT removed_files FROM projects WHERE id = :p", p=project))


async def documents(session, project) -> int:
    return int(await scalar(session, "SELECT count(*) FROM documents WHERE project_id = :p", p=project))


# All or nothing ---------------------------------------------------------------------


async def test_a_drive_source_of_11_files_goes_with_its_rows_and_counts_them(sources_world) -> None:
    w, p = sources_world, sources_world.project_a

    async def build(s):
        src = await make_drive(s, p, w.user_id, state="importing")
        other = await make_drive(s, p, w.user_id, state="done", age=5 * MIN)
        held = [await put_drive_doc(s, p, src, f"p{n}", status="parsed") for n in range(3)]
        for n, doc in enumerate(held):
            await put_item(s, p, src, f"p{n}", "processed", document_id=doc)
        theirs = await put_drive_doc(s, p, other, "theirs", status="parsed")
        await put_item(s, p, other, "theirs", "processed", document_id=theirs)
        await put_item(s, p, src, "theirs", "duplicate", document_id=theirs)  # own duplicate item
        await put_item(s, p, src, "s1", "stored")
        await put_item(s, p, src, "s2", "skipped")
        await put_item(s, p, src, "e1", "error", error=True)
        await put_item(s, p, src, "w1", "pending")
        await put_item(s, p, src, "w2", "processing")
        await put_item(s, p, src, "w3", "processing")
        await put_item(s, p, src, "w4", "pending")
        return src, other, held, theirs

    src, other, held, theirs = await tx(w, build)
    result = await tx(w, lambda s: remove_source(s, p, src))

    assert (result.kind, result.file_count, result.drive_import_open) == ("gdrive", 11, True)
    assert result.handed_over == [] and result.reparse == []
    want = {bin_key(p, src, "s1")} | variants(p, src, "w2") | variants(p, src, "w3")
    want |= {f"projects/{p}/documents/{d}/original.pdf" for d in held}
    assert set(result.object_keys) == want
    assert set(result.sweep_document_ids) == set(held) | {drive_document_id(src, e) for e in ("s1", "w2", "w3")}

    async def check(s):
        assert not await source_exists(s, src) and await source_exists(s, other)
        assert await item_count(s, src) == 0 and await item_count(s, other) == 1
        assert [await doc_row(s, d) for d in held] == [None] * 3
        assert await doc_row(s, theirs) is not None  # a duplicate item never deletes its document
        assert await removed_files(s, p) == 11  # every item counts, duplicates too (SD3)

    await tx(w, check)


async def test_nothing_changes_when_the_transaction_rolls_back(sources_world) -> None:
    w, p = sources_world, sources_world.project_a

    async def build(s):
        src = await make_drive(s, p, w.user_id)
        doc = await put_drive_doc(s, p, src, "a")
        await put_item(s, p, src, "a", "processed", document_id=doc)
        return src, doc

    src, doc = await tx(w, build)

    class AbortError(Exception):
        pass

    async def remove_then_fail(s):
        await remove_source(s, p, src)
        raise AbortError

    with pytest.raises(AbortError):
        await tx(w, remove_then_fail)

    async def check(s):
        assert await source_exists(s, src) and await item_count(s, src) == 1
        assert await doc_row(s, doc) is not None and await removed_files(s, p) == 0

    await tx(w, check)


async def test_a_revision_chain_fails_the_removal_and_leaves_everything(sources_world) -> None:
    """R14: nothing writes previous_revision_id today; if it ever does, the removal fails."""

    w, p = sources_world, sources_world.project_a

    async def build(s):
        src = await make_drive(s, p, w.user_id)
        old = await put_drive_doc(s, p, src, "old")
        await put_item(s, p, src, "old", "processed", document_id=old)
        newer = await add_doc(s, p, "newer", status="parsed", origin="todo_answer")
        await s.execute(
            text("UPDATE documents SET previous_revision_id = :o, version = 2 WHERE id = :n"), {"o": old, "n": newer}
        )
        return src, old

    src, old = await tx(w, build)
    with pytest.raises(RevisionChainError):
        await tx(w, lambda s: remove_source(s, p, src))

    async def check(s):
        assert await source_exists(s, src) and await item_count(s, src) == 1
        assert await doc_row(s, old) is not None and await removed_files(s, p) == 0

    await tx(w, check)


async def test_an_upload_batch_is_removed_with_its_documents(sources_world) -> None:
    w, p = sources_world, sources_world.project_a

    async def build(s):
        batch = await make_batch(s, p, w.user_id, ended=True)
        docs = []
        for n in range(2):
            docs.append(await add_doc(s, p, f"u{n}", status="parsed", source_id=batch))
            await add_item(s, batch, p, f"u{n}", "pending", document_id=docs[-1])
        keys_ = [await scalar(s, "SELECT original_key FROM documents WHERE id = :i", i=d) for d in docs]
        return batch, docs, keys_

    batch, docs, original_keys = await tx(w, build)
    result = await tx(w, lambda s: remove_source(s, p, batch))
    assert (result.kind, result.file_count, result.drive_import_open) == ("upload", 2, False)
    assert result.object_keys == sorted(original_keys)
    assert set(result.sweep_document_ids) == set(docs)

    async def check(s):
        assert not await source_exists(s, batch) and await documents(s, p) == 0
        assert await removed_files(s, p) == 2

    await tx(w, check)


async def test_another_projects_or_an_unknown_source_is_not_found_and_nothing_changes(sources_world) -> None:
    w = sources_world

    async def build(s):
        return await make_batch(s, w.project_a, w.user_id, ended=True)

    batch = await tx(w, build)
    with pytest.raises(SourceNotFoundError):
        await tx(w, lambda s: remove_source(s, w.project_b, batch))
    with pytest.raises(SourceNotFoundError):
        await tx(w, lambda s: remove_source(s, w.project_a, uuid.uuid4()))
    assert await tx(w, lambda s: source_exists(s, batch))
    assert await tx(w, lambda s: removed_files(s, w.project_b)) == 0


async def test_an_unheld_orphan_is_adopted_before_the_removal(sources_world) -> None:
    w, p = sources_world, sources_world.project_a

    async def build(s):
        src = await make_drive(s, p, w.user_id)
        return src, await add_doc(s, p, "orphan", status="parsed", origin="upload")

    src, orphan = await tx(w, build)
    await tx(w, lambda s: remove_source(s, p, src))
    row = await tx(w, lambda s: doc_row(s, orphan))
    assert row is not None and row.source_id is not None


# Hand-over ---------------------------------------------------------------------------


async def build_listed(s, w, status, *, media="application/pdf", listers=2, age=None):
    """A source ``src`` that holds one document listed by ``listers`` newer sources."""

    p = w.project_a
    src = await make_drive(s, p, w.user_id, age=60 * MIN)
    doc = await put_drive_doc(s, p, src, "d", status=status, media_type=media, updated_ago=age)
    await put_item(s, p, src, "d", "processed", document_id=doc)
    targets = []
    for n in range(listers):
        target = await make_drive(s, p, w.user_id, age=(30 - 10 * n) * MIN)  # newest last created
        await put_item(s, p, target, "d", "duplicate", document_id=doc)
        targets.append(target)
    return src, doc, targets


async def test_the_document_goes_to_the_oldest_of_two_listers(sources_world) -> None:
    w, p = sources_world, sources_world.project_a
    src, doc, targets = await tx(w, lambda s: build_listed(s, w, "parsed"))
    oldest = targets[0]  # age 30 min: created first
    result = await tx(w, lambda s: remove_source(s, p, src))

    assert result.handed_over == [doc] and result.reparse == []
    assert result.object_keys == [] and result.sweep_document_ids == []
    row = await tx(w, lambda s: doc_row(s, doc))
    assert (row.source_id, row.status) == (oldest, "parsed")  # a Done file stays Done (SD7)
    assert await tx(w, lambda s: item_count(s, oldest)) == 1  # the listers keep their items


@pytest.mark.parametrize(
    ("status", "media", "reread", "after"),
    [
        ("failed", "application/pdf", True, "processing"),
        ("uploaded", "application/pdf", True, "processing"),
        ("processing", "application/pdf", True, "processing"),  # fresh: stays processing, always listed
        ("failed", "text/markdown", False, "failed"),
        ("ingested", "application/pdf", False, "ingested"),
    ],
)
async def test_a_handed_over_file_is_read_again_only_when_not_done(sources_world, status, media, reread, after) -> None:
    w, p = sources_world, sources_world.project_a
    src, doc, targets = await tx(w, lambda s: build_listed(s, w, status, media=media, listers=1))
    result = await tx(w, lambda s: remove_source(s, p, src))
    assert result.handed_over == [doc]
    assert result.reparse == ([doc] if reread else [])
    row = await tx(w, lambda s: doc_row(s, doc))
    assert (row.source_id, row.status) == (targets[0], after)


async def test_r01_a_processing_item_handed_over_keeps_its_object_key(sources_world) -> None:
    """The child parse of a processing Drive item may have just created its document; if a
    second source lists it, ``original.pdf`` must survive the removal."""

    w, p = sources_world, sources_world.project_a

    async def build(s):
        src = await make_drive(s, p, w.user_id, age=60 * MIN)
        second = await make_drive(s, p, w.user_id, age=20 * MIN)
        doc = await put_drive_doc(s, p, src, "busy", status="processing")
        await put_item(s, p, src, "busy", "processing", document_id=doc)
        await put_item(s, p, second, "busy", "duplicate", document_id=doc)
        # a second processing item whose document does not exist yet: all variants go
        await put_item(s, p, src, "early", "processing")
        return src, second, doc

    src, second, doc = await tx(w, build)
    result = await tx(w, lambda s: remove_source(s, p, src))

    assert result.handed_over == [doc] and result.reparse == [doc]
    assert set(result.object_keys) == variants(p, src, "early")  # nothing of "busy"
    assert drive_document_id(src, "busy") in result.sweep_document_ids  # the sweep re-checks
    row = await tx(w, lambda s: doc_row(s, doc))
    assert (row.source_id, row.status) == (second, "processing")


async def test_r01_an_id_that_still_has_a_document_row_contributes_no_key(sources_world) -> None:
    w, p = sources_world, sources_world.project_a

    async def build(s):
        src = await make_drive(s, p, w.user_id)
        keeper = await make_drive(s, p, w.user_id, age=10 * MIN)
        doc = await put_drive_doc(s, p, src, "x", status="processing", holder=keeper)
        await put_item(s, p, src, "x", "processing")  # no link; the row exists, held elsewhere
        return src, doc

    src, doc = await tx(w, build)
    result = await tx(w, lambda s: remove_source(s, p, src))
    assert result.object_keys == [] and await tx(w, lambda s: doc_row(s, doc)) is not None


async def test_a_listed_document_is_never_deleted_without_a_lister(sources_world) -> None:
    """C11: no cascade. A source's own duplicate item goes; the document it points to stays."""

    w, p = sources_world, sources_world.project_a

    async def build(s):
        a = await make_drive(s, p, w.user_id, age=30 * MIN)
        b = await make_drive(s, p, w.user_id, age=20 * MIN)
        doc = await put_drive_doc(s, p, a, "k", status="parsed")
        await put_item(s, p, a, "k", "processed", document_id=doc)
        await put_item(s, p, b, "k", "duplicate", document_id=doc)
        return a, b, doc

    a, b, doc = await tx(w, build)
    await tx(w, lambda s: remove_source(s, p, b))  # b is only a lister
    row = await tx(w, lambda s: doc_row(s, doc))
    assert row is not None and row.source_id == a
    await tx(w, lambda s: remove_source(s, p, a))  # now nobody lists it
    assert await tx(w, lambda s: doc_row(s, doc)) is None


# Concurrency --------------------------------------------------------------------------


async def test_two_removals_of_one_source_give_one_success_and_one_not_found(sources_world) -> None:
    w, p = sources_world, sources_world.project_a

    async def build(s):
        src = await make_drive(s, p, w.user_id)
        doc = await put_drive_doc(s, p, src, "a")
        await put_item(s, p, src, "a", "processed", document_id=doc)
        return src

    src = await tx(w, build)
    results = await asyncio.gather(
        tx(w, lambda s: remove_source(s, p, src)),
        tx(w, lambda s: remove_source(s, p, src)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, SourceNotFoundError) for r in results) == 1
    assert sum(isinstance(r, removal.Removal) for r in results) == 1
    assert await tx(w, lambda s: removed_files(s, p)) == 1  # counted once


async def test_removals_of_two_sources_listing_each_others_files_both_finish(sources_world) -> None:
    w, p = sources_world, sources_world.project_a

    async def build(s):
        a = await make_drive(s, p, w.user_id, age=30 * MIN)
        b = await make_drive(s, p, w.user_id, age=20 * MIN)
        da = await put_drive_doc(s, p, a, "a")
        db = await put_drive_doc(s, p, b, "b")
        await put_item(s, p, a, "a", "processed", document_id=da)
        await put_item(s, p, b, "b", "processed", document_id=db)
        await put_item(s, p, a, "b", "duplicate", document_id=db)
        await put_item(s, p, b, "a", "duplicate", document_id=da)
        return a, b

    a, b = await tx(w, build)
    results = await asyncio.gather(
        tx(w, lambda s: remove_source(s, p, a)),
        tx(w, lambda s: remove_source(s, p, b)),
        return_exceptions=True,
    )
    assert all(isinstance(r, removal.Removal) for r in results), results
    assert await tx(w, lambda s: documents(s, p)) == 0
    assert await tx(w, lambda s: removed_files(s, p)) == 4


async def test_a_lock_wait_over_the_limit_is_a_lock_timeout_and_changes_nothing(sources_world, monkeypatch) -> None:
    w, p = sources_world, sources_world.project_a
    monkeypatch.setattr(removal, "LOCK_TIMEOUT", "1s")

    async def build(s):
        src = await make_drive(s, p, w.user_id)
        await put_item(s, p, src, "a", "pending")
        return src

    src = await tx(w, build)
    async with w.factory() as holder, holder.begin():
        await holder.execute(text("SELECT 1 FROM connections WHERE id = :i FOR UPDATE"), {"i": src})
        with pytest.raises(LockTimeoutError):
            await tx(w, lambda s: remove_source(s, p, src))

    async def check(s):
        assert await source_exists(s, src) and await item_count(s, src) == 1
        assert await removed_files(s, p) == 0

    await tx(w, check)


async def test_a_deadlock_with_another_writer_is_a_lock_conflict(sources_world) -> None:
    """Another writer holds a lister's connection row and then asks for the project lock; the
    removal holds the project lock and waits for that lister. The removal is the one that
    waited first, so Postgres aborts it with a deadlock, reported as ``LockConflictError``."""

    w, p = sources_world, sources_world.project_a

    async def build(s):
        src = await make_drive(s, p, w.user_id, age=30 * MIN)
        lister = await make_drive(s, p, w.user_id, age=10 * MIN)
        doc = await put_drive_doc(s, p, src, "a", status="parsed")
        await put_item(s, p, src, "a", "processed", document_id=doc)
        await put_item(s, p, lister, "a", "duplicate", document_id=doc)
        return src, lister, doc

    src, lister, doc = await tx(w, build)
    async with w.factory() as blocker, blocker.begin():
        await blocker.execute(text("SELECT 1 FROM connections WHERE id = :i FOR UPDATE"), {"i": lister})
        remover = asyncio.create_task(tx(w, lambda s: remove_source(s, p, src)))
        await wait_until_blocked(w)
        grab = asyncio.create_task(
            blocker.execute(text("SELECT 1 FROM projects WHERE id = :p FOR NO KEY UPDATE"), {"p": p})
        )
        with pytest.raises(LockConflictError):
            await asyncio.wait_for(remover, 20)
        await asyncio.wait_for(grab, 20)

    async def check(s):
        assert await source_exists(s, src) and await item_count(s, src) == 1
        assert (await doc_row(s, doc)).source_id == src and await removed_files(s, p) == 0

    await tx(w, check)
    rows_left = await tx(w, lambda s: rows(s, "SELECT count(*) AS n FROM connection_items WHERE project_id = :p", p=p))
    assert rows_left[0].n == 2


async def test_a_stale_processing_file_is_listed_and_its_clock_restarts(sources_world) -> None:
    w, p = sources_world, sources_world.project_a
    src, doc, _targets = await tx(w, lambda s: build_listed(s, w, "processing", listers=1, age=61 * MIN))
    result = await tx(w, lambda s: remove_source(s, p, src))
    assert result.reparse == [doc]
    row = await tx(
        w,
        lambda s: rows(
            s, "SELECT status, updated_at > now() - interval '5 minutes' AS fresh FROM documents WHERE id = :i", i=doc
        ),
    )
    assert (row[0].status, row[0].fresh) == ("processing", True)


async def test_drive_import_open_is_read_with_the_source_locked(sources_world) -> None:
    """m1 (review-02): a sync that starts while the removal waits for the lock still counts."""

    w, p = sources_world, sources_world.project_a
    src = await tx(w, lambda s: make_drive(s, p, w.user_id, state="done"))
    async with w.factory() as syncer, syncer.begin():
        await syncer.execute(text("SELECT 1 FROM connections WHERE id = :i FOR UPDATE"), {"i": src})
        remover = asyncio.create_task(tx(w, lambda s: remove_source(s, p, src)))
        await wait_until_blocked(w)
        await syncer.execute(text("UPDATE connections SET state = 'importing' WHERE id = :i"), {"i": src})
    assert (await asyncio.wait_for(remover, 20)).drive_import_open is True


async def test_a_worker_share_lock_makes_the_removal_wait_and_the_worker_then_sees_no_source(sources_world) -> None:
    w, p = sources_world, sources_world.project_a
    src = await tx(w, lambda s: make_drive(s, p, w.user_id, state="importing"))
    async with w.factory() as worker, worker.begin():
        assert await sources.lock_importing_connection(worker, p, src) is not None
        remover = asyncio.create_task(tx(w, lambda s: remove_source(s, p, src)))
        await wait_until_blocked(w)
        assert not remover.done()
    assert (await asyncio.wait_for(remover, 20)).drive_import_open is True
    async with w.factory() as later, later.begin():
        assert await sources.lock_importing_connection(later, p, src) is None
