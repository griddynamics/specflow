"""Tier 2: ``adopt_unheld`` (the B1-B5 repair) on a real Postgres.

Requirements: DATA-SRC-0001, DATA-SRC-0002, DATA-SRC-0003, FR-SRC-0021.
``SPECFLOW_TEST_POSTGRES=1``; see specflow_db_live_support.

The fixture rows are written with plain SQL and no ``documents.source_id``: they are the
unheld documents an old pod writes, which the repair gives a holder.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db import source_repair
from settings import DatabaseSettings
from specflow_db_live_support import (
    AgentRole,
    alembic_config,
)
from specflow_live import requires_live
from specflow_sources_live_support import (
    SourcesWorld,
    add_doc,
    add_item,
    make_batch,
    make_drive,
    make_project,
    make_user,
    run_in_database,
)

pytestmark = requires_live("POSTGRES")


def utc(day: int, hour: int = 12, minute: int = 0, second: int = 0, month: int = 3) -> datetime:
    return datetime(2026, month, day, hour, minute, second, tzinfo=UTC)


async def rows(session: AsyncSession, sql: str, **params: Any) -> list[Any]:
    return list((await session.execute(text(sql), params)).all())


async def scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar_one()


async def build(session: AsyncSession) -> dict[str, Any]:
    """The §3.2 fixtures, one project each; returns the ids the assertions need."""

    u1, u2 = await make_user(session, "creator"), await make_user(session, "uploader")
    ids: dict[str, Any] = {"u1": u1, "u2": u2}
    for key in ("a", "b", "h", "d", "e", "f", "g"):
        ids[f"p_{key}"] = await make_project(session, u1, f"P{key}")

    # A: an upload-only day (three files in one UTC day, the last one a second before midnight)
    for n, when in enumerate((utc(10, 10), utc(10, 11), utc(10, 23, 59, 59)), start=1):
        ids[f"a{n}"] = await add_doc(
            session,
            ids["p_a"],
            f"a{n}",
            created_at=when,
            uploaded_by=u2,
            status="parsed",
        )
    # B: two days, split at UTC midnight; one non-PDF file; the first file has no uploader
    ids["b1"] = await add_doc(session, ids["p_b"], "b1", created_at=utc(10, 23, 59, 59), status="failed")
    ids["b2"] = await add_doc(session, ids["p_b"], "b2", created_at=utc(11, 0, 0, 1), uploaded_by=u2)
    ids["b3"] = await add_doc(
        session,
        ids["p_b"],
        "b3",
        created_at=utc(11, 9),
        uploaded_by=u2,
        media_type="text/plain",
    )
    # H: an upload with a NULL uploaded_by (connected_by falls back to the project creator)
    ids["h1"] = await add_doc(session, ids["p_h"], "h1", created_at=utc(12, 8))
    # D: a Drive file linked to an upload
    ids["d_upload"] = await add_doc(
        session,
        ids["p_d"],
        "d-up",
        created_at=utc(10, 9),
        uploaded_by=u2,
        status="parsed",
    )
    ids["d_drive"] = await make_drive(session, ids["p_d"], u1, state="done", age=timedelta(days=2))
    ids["d_item"] = await add_item(
        session,
        ids["d_drive"],
        ids["p_d"],
        "d-it",
        "processed",
        document_id=ids["d_upload"],
    )
    # E: a cross-folder Drive duplicate: the earliest source holds the document
    ids["e1"] = await make_drive(session, ids["p_e"], u1, state="done", age=timedelta(hours=2), name="First")
    ids["e2"] = await make_drive(
        session,
        ids["p_e"],
        u1,
        state="importing",
        age=timedelta(hours=1),
        name="Second",
    )
    ids["e_doc"] = await add_doc(session, ids["p_e"], "e-doc", origin="connection", status="parsed")
    ids["e_item1"] = await add_item(session, ids["e1"], ids["p_e"], "e-1", "processed", document_id=ids["e_doc"])
    ids["e_item2"] = await add_item(session, ids["e2"], ids["p_e"], "e-2", "processed", document_id=ids["e_doc"])
    ids["e_pending"] = await add_item(session, ids["e2"], ids["p_e"], "e-p", "pending")
    ids["e_stored"] = await add_item(session, ids["e2"], ids["p_e"], "e-s", "stored")
    # F: an old error/parse_failed link to a document another source holds
    ids["f1"] = await make_drive(session, ids["p_f"], u1, state="done", age=timedelta(hours=2))
    ids["f2"] = await make_drive(session, ids["p_f"], u1, state="done", age=timedelta(hours=1))
    ids["f_doc"] = await add_doc(session, ids["p_f"], "f-doc", origin="connection", status="parsed")
    ids["f_own"] = await add_item(
        session,
        ids["f1"],
        ids["p_f"],
        "f-own",
        "error",
        document_id=ids["f_doc"],
        error_reason="parse_failed",
        error_step="parse",
    )
    ids["f_link"] = await add_item(
        session,
        ids["f2"],
        ids["p_f"],
        "f-link",
        "error",
        document_id=ids["f_doc"],
        error_reason="parse_failed",
        error_step="parse",
    )
    ids["f_plain"] = await add_item(
        session, ids["f2"], ids["p_f"], "f-plain", "error", error_reason="no_access", error_step="download"
    )
    # G: a project with no uploads: only a Drive source
    ids["g1"] = await make_drive(session, ids["p_g"], u1, state="done")
    ids["g_doc"] = await add_doc(session, ids["p_g"], "g-doc", origin="connection", status="parsed")
    await add_item(session, ids["g1"], ids["p_g"], "g-1", "processed", document_id=ids["g_doc"])
    return ids


COUNT_SQL = {
    "connections": "SELECT count(*) FROM connections",
    "connection_items": "SELECT count(*) FROM connection_items",
    "documents": "SELECT count(*) FROM documents",
    "projects": "SELECT count(*) FROM projects",
    "users": "SELECT count(*) FROM users",
}


async def counts(session: AsyncSession) -> dict[str, int]:
    return {table: await scalar(session, sql) for table, sql in COUNT_SQL.items()}


async def logical(session: AsyncSession) -> list[tuple[Any, ...]]:
    """The result of a repair without generated ids, in a stable order."""

    batches = await rows(
        session,
        "SELECT p.name, c.external_id, c.name, c.state, c.link IS NULL, c.ended_at = c.created_at, "
        "c.connected_by, c.name_suffix, c.expected_files "
        "FROM connections c JOIN projects p ON p.id = c.project_id WHERE c.kind = 'upload' "
        "ORDER BY p.name, c.external_id",
    )
    documents = await rows(
        session,
        "SELECT p.name, d.original_filename, h.kind, h.external_id FROM documents d "
        "JOIN projects p ON p.id = d.project_id LEFT JOIN connections h ON h.id = d.source_id "
        "ORDER BY p.name, d.original_filename",
    )
    items = await rows(
        session,
        "SELECT p.name, i.name, i.state, i.error_reason, i.error_step, c.external_id "
        "FROM connection_items i JOIN projects p ON p.id = i.project_id "
        "JOIN connections c ON c.id = i.connection_id ORDER BY p.name, i.name, c.external_id",
    )
    return [tuple(r) for r in (*batches, *documents, *items)]


async def check_repair(session: AsyncSession, ids: dict[str, Any]) -> None:
    """Every §3.2 fixture case."""

    batches = {
        (r.project, r.external_id): r
        for r in await rows(
            session,
            "SELECT p.name AS project, c.* FROM connections c JOIN projects p ON p.id = c.project_id "
            "WHERE c.kind = 'upload'",
        )
    }
    assert sorted(batches) == [
        ("Pa", "legacy-20260310"),
        ("Pb", "legacy-20260310"),
        ("Pb", "legacy-20260311"),
        ("Pd", "legacy-20260310"),
        ("Ph", "legacy-20260312"),
    ]

    # A: one batch for the day, shaped as SPECS 3.1 says
    a = batches[("Pa", "legacy-20260310")]
    assert (a.name, a.state, a.link, a.expected_files, a.name_suffix) == (
        "Local files",
        "done",
        None,
        None,
        1,
    )
    assert a.created_at == utc(10, 10) and a.ended_at == a.created_at  # the first file of the day
    assert a.connected_by == ids["u2"]  # the first file's uploader
    for key in ("a1", "a2", "a3"):
        assert await scalar(session, "SELECT source_id FROM documents WHERE id = :i", i=ids[key]) == a.id
    assert (
        await scalar(
            session,
            "SELECT count(*) FROM connection_items WHERE connection_id = :c",
            c=a.id,
        )
        == 3
    )
    item = (
        await rows(
            session,
            "SELECT * FROM connection_items WHERE document_id = :d",
            d=ids["a1"],
        )
    )[0]
    assert (item.external_id, item.name, item.mime_type, item.category, item.processing_class) == (
        (await scalar(session, "SELECT content_hash FROM documents WHERE id = :i", i=ids["a1"])),
        "a1.pdf",
        "application/pdf",
        "pdf",
        "processed",
    )
    assert (item.state, item.size_bytes, item.project_id, item.connection_id) == (
        "pending",
        123,
        ids["p_a"],
        a.id,
    )

    # B: two days (the UTC boundary), a non-PDF is other_files, a NULL first uploader falls back
    day1, day2 = batches[("Pb", "legacy-20260310")], batches[("Pb", "legacy-20260311")]
    assert await scalar(session, "SELECT source_id FROM documents WHERE id = :i", i=ids["b1"]) == day1.id
    assert {await scalar(session, "SELECT source_id FROM documents WHERE id = :i", i=ids[k]) for k in ("b2", "b3")} == {
        day2.id
    }
    assert day1.connected_by == ids["u1"] and day2.connected_by == ids["u2"]
    assert (
        await scalar(
            session,
            "SELECT category FROM connection_items WHERE document_id = :d",
            d=ids["b3"],
        )
        == "other_files"
    )
    assert (
        await scalar(
            session,
            "SELECT mime_type FROM connection_items WHERE document_id = :d",
            d=ids["b3"],
        )
        == "text/plain"
    )

    # H: connected_by is NOT NULL, so a NULL uploader falls back to the project creator
    assert batches[("Ph", "legacy-20260312")].connected_by == ids["u1"]

    # D: the Drive item that linked an upload now says "already in the project"
    d_batch = batches[("Pd", "legacy-20260310")]
    assert await scalar(session, "SELECT source_id FROM documents WHERE id = :i", i=ids["d_upload"]) == d_batch.id
    assert await rows(
        session,
        "SELECT state, error_reason FROM connection_items WHERE id = :i",
        i=ids["d_item"],
    ) == [("duplicate", None)]

    # E: the earliest Drive source holds the document; the later linker is a duplicate
    assert await scalar(session, "SELECT source_id FROM documents WHERE id = :i", i=ids["e_doc"]) == ids["e1"]
    states = {
        k: await scalar(session, "SELECT state FROM connection_items WHERE id = :i", i=ids[k])
        for k in ("e_item1", "e_item2", "e_pending", "e_stored")
    }
    assert states == {
        "e_item1": "processed",
        "e_item2": "duplicate",
        "e_pending": "pending",
        "e_stored": "stored",
    }

    # F: an old error/parse_failed link becomes a duplicate and loses its error; the others keep theirs
    assert await scalar(session, "SELECT source_id FROM documents WHERE id = :i", i=ids["f_doc"]) == ids["f1"]
    assert await rows(
        session,
        "SELECT state, error_reason, error_step FROM connection_items WHERE id = :i",
        i=ids["f_link"],
    ) == [("duplicate", None, None)]
    assert await rows(
        session,
        "SELECT state, error_reason, error_step FROM connection_items WHERE id = :i",
        i=ids["f_own"],
    ) == [("error", "parse_failed", "parse")]
    assert await rows(
        session,
        "SELECT state, error_reason, error_step FROM connection_items WHERE id = :i",
        i=ids["f_plain"],
    ) == [("error", "no_access", "download")]

    # G: no uploads, no batch; its Drive document is held by the Drive source
    assert (
        await scalar(
            session,
            "SELECT count(*) FROM connections WHERE project_id = :p AND kind = 'upload'",
            p=ids["p_g"],
        )
        == 0
    )
    assert await scalar(session, "SELECT source_id FROM documents WHERE id = :i", i=ids["g_doc"]) == ids["g1"]

    # nothing stays unheld
    assert await scalar(session, "SELECT count(*) FROM documents WHERE source_id IS NULL") == 0


async def adopt_everything(session: AsyncSession) -> int:
    return await source_repair.adopt_unheld(session, None)


def test_the_global_repair_covers_every_fixture_case_and_is_idempotent(
    scratch_database: DatabaseSettings, agent_role: AgentRole
) -> None:
    command.upgrade(alembic_config(scratch_database), "head")
    ids = run_in_database(scratch_database, build)
    before = run_in_database(scratch_database, counts)

    adopted = run_in_database(scratch_database, adopt_everything)

    assert adopted == before["documents"]  # every fixture document was unheld
    after = run_in_database(scratch_database, counts)
    assert after["connections"] == before["connections"] + 5  # A, B x 2, D, H
    assert after["connection_items"] == before["connection_items"] + 3 + 3 + 1 + 1  # a1-3, b1-3, h1, d-up
    assert (after["documents"], after["projects"], after["users"]) == (
        before["documents"],
        before["projects"],
        before["users"],
    )
    run_in_database(scratch_database, lambda s: check_repair(s, ids))
    first = run_in_database(scratch_database, logical)

    # B1-B5 again: nothing changes (idempotent)
    assert run_in_database(scratch_database, adopt_everything) == 0
    assert run_in_database(scratch_database, logical) == first
    assert run_in_database(scratch_database, counts) == after


# adopt_unheld at head --------------------------------------------------------------------------


async def test_adopt_unheld_is_idempotent_scoped_and_counts_the_documents(
    sources_world: SourcesWorld,
) -> None:
    world = sources_world
    async with world.factory() as session, session.begin():
        a1 = await add_doc(
            session,
            world.project_a,
            "old-1",
            created_at=utc(10, 10),
            uploaded_by=world.user_id,
        )
        a2 = await add_doc(session, world.project_a, "old-2", created_at=utc(10, 11))
        drive = await make_drive(session, world.project_a, world.user_id, state="done")
        drive_doc = await add_doc(session, world.project_a, "old-drive", origin="connection", status="parsed")
        await add_item(
            session,
            drive,
            world.project_a,
            "old-drive",
            "processed",
            document_id=drive_doc,
        )
        elsewhere = await add_doc(session, world.project_b, "b-old", created_at=utc(10, 10))

    async with world.factory() as session, session.begin():
        adopted = await source_repair.adopt_unheld(session, world.project_a)
        holders = {
            d: await scalar(session, "SELECT source_id FROM documents WHERE id = :i", i=d)
            for d in (a1, a2, drive_doc, elsewhere)
        }
        batch = await scalar(
            session,
            "SELECT id FROM connections WHERE project_id = :p AND kind = 'upload'",
            p=world.project_a,
        )

    assert adopted == 3  # two uploads and the Drive document
    assert holders == {
        a1: batch,
        a2: batch,
        drive_doc: drive,
        elsewhere: None,
    }  # project B untouched

    async with world.factory() as session, session.begin():
        assert await source_repair.adopt_unheld(session, world.project_a) == 0
        assert await source_repair.adopt_unheld(session, None) == 1  # now project B's upload
        assert await source_repair.adopt_unheld(session, None) == 0
        assert await scalar(session, "SELECT count(*) FROM connections WHERE kind = 'upload'") == 2


async def test_a_late_old_pod_upload_of_a_known_day_joins_that_days_batch(
    sources_world: SourcesWorld,
) -> None:
    world = sources_world
    async with world.factory() as session, session.begin():
        first = await add_doc(session, world.project_a, "day-1", created_at=utc(10, 10))
        await source_repair.adopt_unheld(session, world.project_a)
        batch = await scalar(session, "SELECT source_id FROM documents WHERE id = :i", i=first)
        late = await add_doc(session, world.project_a, "day-2", created_at=utc(10, 20))

        assert await source_repair.adopt_unheld(session, world.project_a) == 1

        assert await scalar(session, "SELECT source_id FROM documents WHERE id = :i", i=late) == batch
        assert await scalar(session, "SELECT count(*) FROM connections WHERE kind = 'upload'") == 1
        assert (
            await scalar(
                session,
                "SELECT count(*) FROM connection_items WHERE connection_id = :c",
                c=batch,
            )
            == 2
        )


async def test_a_batch_started_in_the_same_minute_gives_the_day_batch_suffix_2(
    sources_world: SourcesWorld,
) -> None:
    world = sources_world
    async with world.factory() as session, session.begin():
        new_batch = await make_batch(session, world.project_a, world.user_id, ended=True)
        await session.execute(
            text("UPDATE connections SET created_at = :t WHERE id = :i"),
            {"t": utc(10, 10, 0, 20), "i": new_batch},
        )
        old = await add_doc(session, world.project_a, "same-minute", created_at=utc(10, 10, 0, 5))

        await source_repair.adopt_unheld(session, world.project_a)

        batch = await scalar(session, "SELECT source_id FROM documents WHERE id = :i", i=old)
        assert batch != new_batch
        assert await scalar(session, "SELECT name_suffix FROM connections WHERE id = :i", i=batch) == 2
        assert (
            await scalar(
                session,
                "SELECT name_suffix FROM connections WHERE id = :i",
                i=new_batch,
            )
            == 1
        )


async def test_adopt_unheld_marks_a_link_to_a_document_another_source_holds_as_duplicate(
    sources_world: SourcesWorld,
) -> None:
    world = sources_world
    async with world.factory() as session, session.begin():
        uploaded = await add_doc(session, world.project_a, "shared", created_at=utc(10, 10))
        drive = await make_drive(session, world.project_a, world.user_id, state="done")
        linker = await add_item(
            session,
            drive,
            world.project_a,
            "shared",
            "error",
            document_id=uploaded,
            error_reason="parse_failed",
            error_step="parse",
        )

        await source_repair.adopt_unheld(session, world.project_a)

        assert await rows(
            session,
            "SELECT state, error_reason, error_step FROM connection_items WHERE id = :i",
            i=linker,
        ) == [("duplicate", None, None)]
        assert await scalar(
            session,
            "SELECT d.source_id <> :d FROM documents d WHERE d.id = :i",
            d=drive,
            i=uploaded,
        )
