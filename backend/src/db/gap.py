"""Application-role data access for the gap module's relational rows (docs/graph/gap/data-model.md "Relational store").

Same rules as ``db.repositories``: every function runs inside the caller's session and transaction, flushes and never
commits. Every statement filters on the project id it is given (FR-GOWN-02); rows are plain mappings of column values,
so this module does not import ``graph``. ``api.gap.records`` turns them into the gap library's row models.
Questions, quotes and answers live in the database only; nothing here logs (NFR-GSEC-02).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import RowMapping, delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    CorpusAmendment,
    GapEvent,
    GapNoiseRule,
    GapNote,
    GapOption,
    GapRun,
    GapSectionRelevance,
    GapSeed,
    GapSite,
    GapSuppression,
    GapVerdict,
    Project,
)

RUN_FINISHED: Final = "completed"
AMENDMENT_APPLIED: Final = ("active", "undecided")
# Text ids sort by code point, whatever the database's collation (the in-memory fakes sort the same way).
BYTE_ORDER: Final = "C"

Values = Mapping[str, Any]
PROJECTS_LIMIT: Final = 10_000  # the projects one process start migrates (first-slice findings exist only in a few)


def _scoped(project_id: uuid.UUID, rows: Sequence[Values]) -> list[dict[str, Any]]:
    return [{**row, "project_id": project_id} for row in rows]


def _set_excluded(statement: Any, keys: Sequence[str]) -> dict[str, Any]:
    """The ``ON CONFLICT DO UPDATE`` assignments: every column but the key takes the proposed value."""

    return {column.name: column for column in statement.excluded if column.name not in keys}


# gap_option


async def options(session: AsyncSession, project_id: uuid.UUID, gap_id: str) -> Sequence[RowMapping]:
    statement = (
        select(GapOption.__table__)
        .where(GapOption.project_id == project_id, GapOption.gap_id == gap_id)
        .order_by(GapOption.rank)
    )
    return (await session.execute(statement)).mappings().all()


async def replace_options(session: AsyncSession, project_id: uuid.UUID, gap_id: str, rows: Sequence[Values]) -> None:
    """The gap's options become ``rows`` (none: the gap has no options)."""

    await session.execute(delete(GapOption).where(GapOption.project_id == project_id, GapOption.gap_id == gap_id))
    if rows:
        await session.execute(pg_insert(GapOption), _scoped(project_id, [{**row, "gap_id": gap_id} for row in rows]))


# gap_run


async def upsert_run(session: AsyncSession, project_id: uuid.UUID, row: Values) -> None:
    statement = pg_insert(GapRun).values(_scoped(project_id, [row]))
    keys = ("project_id", "run_id")
    await session.execute(statement.on_conflict_do_update(index_elements=keys, set_=_set_excluded(statement, keys)))


async def finish_run(
    session: AsyncSession,
    project_id: uuid.UUID,
    run_id: str,
    counts: Values,
    cost: Values,
    *,
    status: str = RUN_FINISHED,
) -> bool:
    """Mark the run finished (``status``: completed, degraded or failed) with its counts and cost; ``False`` when the
    project has no such run."""

    statement = (
        update(GapRun)
        .where(GapRun.project_id == project_id, GapRun.run_id == run_id)
        .values(status=status, finished_at=func.now(), counts=dict(counts), cost=dict(cost))
        .returning(GapRun.run_id)
    )
    return (await session.execute(statement)).first() is not None


# gap_seed, gap_site, gap_note, gap_event


async def insert_seeds(session: AsyncSession, project_id: uuid.UUID, rows: Sequence[Values]) -> None:
    if rows:
        await session.execute(pg_insert(GapSeed), _scoped(project_id, rows))


async def upsert_sites(session: AsyncSession, project_id: uuid.UUID, rows: Sequence[Values]) -> None:
    """Insert the sites; a site already stored for the same run takes the new values."""

    if not rows:
        return
    statement = pg_insert(GapSite).values(_scoped(project_id, rows))
    keys = ("project_id", "run_id", "site_id")
    await session.execute(statement.on_conflict_do_update(index_elements=keys, set_=_set_excluded(statement, keys)))


async def insert_notes(session: AsyncSession, project_id: uuid.UUID, rows: Sequence[Values]) -> None:
    if rows:
        await session.execute(pg_insert(GapNote), _scoped(project_id, rows))


async def insert_events(session: AsyncSession, project_id: uuid.UUID, rows: Sequence[Values]) -> None:
    if rows:
        await session.execute(pg_insert(GapEvent), _scoped(project_id, rows))


async def events(session: AsyncSession, project_id: uuid.UUID, gap_id: str) -> Sequence[RowMapping]:
    """The events of one gap, by ``at`` then id."""

    statement = (
        select(GapEvent.__table__)
        .where(GapEvent.project_id == project_id, GapEvent.gap_id == gap_id)
        .order_by(GapEvent.at, GapEvent.id)
    )
    return (await session.execute(statement)).mappings().all()


# gap_verdict


async def verdict(session: AsyncSession, project_id: uuid.UUID, input_hash: str) -> RowMapping | None:
    statement = select(GapVerdict.__table__).where(
        GapVerdict.project_id == project_id, GapVerdict.input_hash == input_hash
    )
    return (await session.execute(statement)).mappings().one_or_none()


async def upsert_verdict(session: AsyncSession, project_id: uuid.UUID, row: Values) -> None:
    statement = pg_insert(GapVerdict).values(_scoped(project_id, [row]))
    keys = ("project_id", "input_hash")
    await session.execute(statement.on_conflict_do_update(index_elements=keys, set_=_set_excluded(statement, keys)))


# gap_section_relevance


async def relevance(
    session: AsyncSession,
    project_id: uuid.UUID,
    keys: Sequence[tuple[str, Sequence[str], str]],
) -> Sequence[RowMapping]:
    """The cached rows of the given (lineage, heading path, text hash) keys."""

    if not keys:
        return []
    key = tuple_(GapSectionRelevance.lineage, GapSectionRelevance.heading_path, GapSectionRelevance.text_hash)
    statement = select(GapSectionRelevance.__table__).where(
        GapSectionRelevance.project_id == project_id,
        key.in_([(lineage, list(heading_path), text_hash) for lineage, heading_path, text_hash in keys]),
    )
    return (await session.execute(statement)).mappings().all()


async def upsert_relevance(session: AsyncSession, project_id: uuid.UUID, rows: Sequence[Values]) -> None:
    if not rows:
        return
    statement = pg_insert(GapSectionRelevance).values(_scoped(project_id, rows))
    keys = ("project_id", "lineage", "heading_path", "text_hash")
    await session.execute(statement.on_conflict_do_update(index_elements=keys, set_=_set_excluded(statement, keys)))


# gap_noise_rule, gap_suppression


async def noise_rules(session: AsyncSession, project_id: uuid.UUID) -> Sequence[RowMapping]:
    statement = (
        select(GapNoiseRule.__table__)
        .where(GapNoiseRule.project_id == project_id)
        .order_by(GapNoiseRule.created_at, GapNoiseRule.rule_id.collate(BYTE_ORDER))
    )
    return (await session.execute(statement)).mappings().all()


async def upsert_noise_rule(session: AsyncSession, project_id: uuid.UUID, row: Values) -> None:
    statement = pg_insert(GapNoiseRule).values(_scoped(project_id, [row]))
    keys = ("project_id", "rule_id")
    await session.execute(statement.on_conflict_do_update(index_elements=keys, set_=_set_excluded(statement, keys)))


async def delete_noise_rule(session: AsyncSession, project_id: uuid.UUID, rule_id: str) -> bool:
    """Delete the project's noise rule; True when it existed."""

    result = await session.execute(
        delete(GapNoiseRule)
        .where(GapNoiseRule.project_id == project_id, GapNoiseRule.rule_id == rule_id)
        .returning(GapNoiseRule.rule_id)
    )
    return result.first() is not None


async def suppressed(session: AsyncSession, project_id: uuid.UUID, signatures: Sequence[str]) -> Sequence[str]:
    """The given signatures that are suppressed in the project."""

    if not signatures:
        return []
    statement = select(GapSuppression.signature).where(
        GapSuppression.project_id == project_id, GapSuppression.signature.in_(list(signatures))
    )
    return (await session.execute(statement)).scalars().all()


async def upsert_suppression(session: AsyncSession, project_id: uuid.UUID, row: Values) -> None:
    statement = pg_insert(GapSuppression).values(_scoped(project_id, [row]))
    keys = ("project_id", "signature")
    await session.execute(statement.on_conflict_do_update(index_elements=keys, set_=_set_excluded(statement, keys)))


async def delete_suppression(session: AsyncSession, project_id: uuid.UUID, signature: str) -> None:
    await session.execute(
        delete(GapSuppression).where(GapSuppression.project_id == project_id, GapSuppression.signature == signature)
    )


# corpus_amendment


async def amendments_for_lineage(
    session: AsyncSession,
    project_id: uuid.UUID,
    lineage: str,
    statuses: Sequence[str],
) -> Sequence[RowMapping]:
    statement = (
        select(CorpusAmendment.__table__)
        .where(
            CorpusAmendment.project_id == project_id,
            CorpusAmendment.lineage == lineage,
            CorpusAmendment.status.in_(list(statuses)),
        )
        .order_by(CorpusAmendment.created_at, CorpusAmendment.amendment_id.collate(BYTE_ORDER))
    )
    return (await session.execute(statement)).mappings().all()


async def amendments_for_clarification(
    session: AsyncSession, project_id: uuid.UUID, clr_id: str
) -> Sequence[RowMapping]:
    statement = (
        select(CorpusAmendment.__table__)
        .where(CorpusAmendment.project_id == project_id, CorpusAmendment.clr_id == clr_id)
        .order_by(CorpusAmendment.created_at, CorpusAmendment.amendment_id.collate(BYTE_ORDER))
    )
    return (await session.execute(statement)).mappings().all()


async def upsert_amendments(session: AsyncSession, project_id: uuid.UUID, rows: Sequence[Values]) -> None:
    if not rows:
        return
    statement = pg_insert(CorpusAmendment).values(_scoped(project_id, rows))
    keys = ("project_id", "amendment_id")
    await session.execute(statement.on_conflict_do_update(index_elements=keys, set_=_set_excluded(statement, keys)))


async def set_amendment_status(
    session: AsyncSession,
    project_id: uuid.UUID,
    amendment_ids: Sequence[str],
    status: str,
    revision_id: str | None,
) -> None:
    """Set the status (and, when given, the last revision) of the project's amendments among ``amendment_ids``."""

    if not amendment_ids:
        return
    values: dict[str, Any] = {"status": status, "updated_at": func.now()}
    if revision_id is not None:
        values["last_revision_id"] = revision_id
    await session.execute(
        update(CorpusAmendment)
        .where(CorpusAmendment.project_id == project_id, CorpusAmendment.amendment_id.in_(list(amendment_ids)))
        .values(**values)
    )


async def delete_amendments(session: AsyncSession, project_id: uuid.UUID, amendment_ids: Sequence[str]) -> None:
    """Delete the project's amendments among ``amendment_ids`` (a pending answer replaced or written back)."""

    if not amendment_ids:
        return
    await session.execute(
        delete(CorpusAmendment).where(
            CorpusAmendment.project_id == project_id, CorpusAmendment.amendment_id.in_(list(amendment_ids))
        )
    )


async def amendments_with_status(
    session: AsyncSession, project_id: uuid.UUID, statuses: Sequence[str]
) -> Sequence[RowMapping]:
    """The project's amendments in any of ``statuses``, of every lineage, in creation order."""

    statement = (
        select(CorpusAmendment.__table__)
        .where(CorpusAmendment.project_id == project_id, CorpusAmendment.status.in_(list(statuses)))
        .order_by(CorpusAmendment.created_at, CorpusAmendment.amendment_id.collate(BYTE_ORDER))
    )
    return (await session.execute(statement)).mappings().all()


async def pending_lineages(session: AsyncSession, project_id: uuid.UUID) -> Sequence[str]:
    """The lineages with at least one amendment the corpus reader applies (``active`` or ``undecided``), in order."""

    statement = (
        select(CorpusAmendment.lineage)
        .where(CorpusAmendment.project_id == project_id, CorpusAmendment.status.in_(AMENDMENT_APPLIED))
        .group_by(CorpusAmendment.lineage)
        .order_by(CorpusAmendment.lineage.collate(BYTE_ORDER))
    )
    return (await session.execute(statement)).scalars().all()


async def project_ids(session: AsyncSession) -> Sequence[uuid.UUID]:
    """Every project's id, by id: the projects whose first-slice findings ``ensure_gap_schema`` migrates."""

    return list((await session.scalars(select(Project.id).order_by(Project.id).limit(PROJECTS_LIMIT))).all())
