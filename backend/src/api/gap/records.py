"""``GapRecords`` and ``Amendments`` over PostgreSQL (docs/graph/gap/api.md "Ports", data-model.md "Relational store").

The host's adapters of the two relational ports of ``graph.gap``: the rows live in the application database
(``db.models`` gap tables, migration ``0001_initial``) and the statements in ``db.gap``. Each port call is one
transaction of its own, opened by the ``transaction`` callable the composition root hands in (``Runtime.transaction``).
Every call takes the project id first and every statement filters on it, so one project never reads or changes
another's rows (FR-GOWN-02); the rows are never graph nodes (FR-GOWN-04). Port semantics shared with the in-memory
fakes (``tests/gap_port_fakes.py``) and proven by ``tests/gap_records_contract.py``:

- ``put_options`` replaces the gap's options; ``options`` returns them by rank. A set with a repeated rank, more
  than one recommended row or a row of another gap is refused (``ConformanceError("gap_options_invalid")``).
- ``set_status`` refuses a status outside the amendment vocabulary (``gap_amendment_status_unknown``); a project id
  that is not a uuid is refused (``gap_project_id_invalid``). A batch naming a key twice keeps its last row.
- ``start_run`` stores the run (again: replaced); ``finish_run`` marks it ``completed`` with ``finished_at``, counts
  and cost, and raises ``ConformanceError("gap_run_unknown")`` when the project has no such run.
- ``put_seeds``/``put_sites`` refuse a row of another run (``ConformanceError("gap_rows_of_another_run")``); a site
  stored again for its run takes the new values. ``put_notes`` files every note under the given run.
- ``verdict``, ``relevance``, ``suppress`` and amendments ``put`` are keyed upserts.
- ``Amendments.set_status`` changes only the project's amendments among the ids (others are ignored) and moves
  ``updated_at``; ``for_lineage``/``for_clarification`` return rows by ``created_at`` then id.

Nothing here logs: questions, quotes and answers stay in the database (NFR-GSEC-02).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Final, get_args

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from db import gap as repo
from graph.core.errors import ConformanceError
from graph.gap.models import (
    AmendmentRow,
    EventRow,
    NoiseRuleRow,
    NoteRow,
    OptionRow,
    RelevanceRow,
    RunRow,
    SectionKey,
    SeedRow,
    SiteRow,
    SuppressionRow,
    VerdictRow,
)
from graph.gap.names import AmendmentStatus

RUN_UNKNOWN: Final = "gap_run_unknown"
ROWS_OF_ANOTHER_RUN: Final = "gap_rows_of_another_run"
OPTIONS_INVALID: Final = "gap_options_invalid"
AMENDMENT_STATUS_UNKNOWN: Final = "gap_amendment_status_unknown"
PROJECT_ID_INVALID: Final = "gap_project_id_invalid"
AMENDMENT_STATUSES: Final = frozenset(get_args(AmendmentStatus))

Transaction = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def _project(project_id: str) -> uuid.UUID:
    """The application database keys projects by uuid; the gap library passes its text form."""

    try:
        return uuid.UUID(project_id)
    except ValueError:
        raise ConformanceError(PROJECT_ID_INVALID) from None


def _check_options(gap_id: str, rows: Sequence[OptionRow]) -> None:
    """Refuse an option set the table would reject, before any statement: the database's refusal text would
    carry the row (labels, quotes) into error logs (NFR-GSEC-02)."""

    ranks = [row.rank for row in rows]
    recommended = sum(row.recommended for row in rows)
    if len(set(ranks)) != len(ranks) or recommended > 1 or any(row.gap_id != gap_id for row in rows):
        raise ConformanceError(OPTIONS_INVALID, gap_id=gap_id)


def _check_status(status: str) -> None:
    if status not in AMENDMENT_STATUSES:
        raise ConformanceError(AMENDMENT_STATUS_UNKNOWN, status=status)


def _last_per_key[R](rows: Sequence[R], key: Callable[[R], Any]) -> list[R]:
    """One row per key, the last one given (an upsert statement may not touch a row twice)."""

    return list({key(row): row for row in rows}.values())


def _values(row: BaseModel, *, exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    """A row model as column values: timestamps stay datetimes, everything else is JSON-ready (the JSON columns)."""

    plain = row.model_dump(exclude=set(exclude))
    ready = row.model_dump(mode="json", exclude=set(exclude))
    return {name: value if isinstance(value, datetime) else ready[name] for name, value in plain.items()}


def _row[M: BaseModel](model: type[M], values: Mapping[Any, Any]) -> M:
    return model.model_validate({name: values[name] for name in model.model_fields})


def _same_run(run_id: str, rows: Sequence[SeedRow] | Sequence[SiteRow]) -> None:
    if any(row.run_id != run_id for row in rows):
        raise ConformanceError(ROWS_OF_ANOTHER_RUN, run_id=run_id)


class PostgresGapRecords:
    """``GapRecords`` on the application database."""

    def __init__(self, transaction: Transaction) -> None:
        self._transaction = transaction

    async def options(self, project_id: str, gap_id: str) -> Sequence[OptionRow]:
        async with self._transaction() as session:
            rows = await repo.options(session, _project(project_id), gap_id)
        return tuple(_row(OptionRow, row) for row in rows)

    async def put_options(self, project_id: str, gap_id: str, rows: Sequence[OptionRow]) -> None:
        _check_options(gap_id, rows)
        values = [_values(row, exclude=frozenset({"gap_id"})) for row in rows]
        async with self._transaction() as session:
            await repo.replace_options(session, _project(project_id), gap_id, values)

    async def start_run(self, project_id: str, run: RunRow) -> None:
        async with self._transaction() as session:
            await repo.upsert_run(session, _project(project_id), _values(run))

    async def finish_run(
        self,
        project_id: str,
        run_id: str,
        counts: Mapping[str, Any],
        cost: Mapping[str, Any],
    ) -> None:
        async with self._transaction() as session:
            found = await repo.finish_run(session, _project(project_id), run_id, counts, cost)
        if not found:
            raise ConformanceError(RUN_UNKNOWN, run_id=run_id)

    async def put_seeds(self, project_id: str, run_id: str, seeds: Sequence[SeedRow]) -> None:
        _same_run(run_id, seeds)
        async with self._transaction() as session:
            await repo.insert_seeds(session, _project(project_id), [_values(seed) for seed in seeds])

    async def put_sites(self, project_id: str, run_id: str, sites: Sequence[SiteRow]) -> None:
        _same_run(run_id, sites)
        async with self._transaction() as session:
            unique = _last_per_key(sites, lambda site: site.site_id)
            await repo.upsert_sites(session, _project(project_id), [_values(site) for site in unique])

    async def verdict(self, project_id: str, input_hash: str) -> VerdictRow | None:
        async with self._transaction() as session:
            row = await repo.verdict(session, _project(project_id), input_hash)
        return None if row is None else _row(VerdictRow, row)

    async def put_verdict(self, project_id: str, row: VerdictRow) -> None:
        async with self._transaction() as session:
            await repo.upsert_verdict(session, _project(project_id), _values(row))

    async def relevance(self, project_id: str, keys: Sequence[SectionKey]) -> Mapping[SectionKey, RelevanceRow]:
        wanted = [(key.lineage, key.heading_path, key.text_hash) for key in keys]
        async with self._transaction() as session:
            rows = await repo.relevance(session, _project(project_id), wanted)
        found = (_row(RelevanceRow, row) for row in rows)
        return {row.key: row for row in found}

    async def put_relevance(self, project_id: str, rows: Sequence[RelevanceRow]) -> None:
        async with self._transaction() as session:
            unique = _last_per_key(rows, lambda row: row.key)
            await repo.upsert_relevance(session, _project(project_id), [_values(row) for row in unique])

    async def noise_rules(self, project_id: str) -> Sequence[NoiseRuleRow]:
        async with self._transaction() as session:
            rows = await repo.noise_rules(session, _project(project_id))
        return tuple(_row(NoiseRuleRow, row) for row in rows)

    async def suppressed(self, project_id: str, signatures: Sequence[str]) -> frozenset[str]:
        async with self._transaction() as session:
            return frozenset(await repo.suppressed(session, _project(project_id), list(signatures)))

    async def suppress(self, project_id: str, row: SuppressionRow) -> None:
        async with self._transaction() as session:
            await repo.upsert_suppression(session, _project(project_id), _values(row))

    async def put_notes(self, project_id: str, run_id: str, notes: Sequence[NoteRow]) -> None:
        values = [_values(note) | {"run_id": run_id} for note in notes]
        async with self._transaction() as session:
            await repo.insert_notes(session, _project(project_id), values)

    async def log(self, project_id: str, events: Sequence[EventRow]) -> None:
        async with self._transaction() as session:
            await repo.insert_events(session, _project(project_id), [_values(event) for event in events])


class PostgresAmendments:
    """``Amendments`` on the application database, beside the documents."""

    def __init__(self, transaction: Transaction) -> None:
        self._transaction = transaction

    async def for_lineage(
        self, project_id: str, lineage: str, *, statuses: Sequence[str] = ("active",)
    ) -> Sequence[AmendmentRow]:
        async with self._transaction() as session:
            rows = await repo.amendments_for_lineage(session, _project(project_id), lineage, list(statuses))
        return tuple(_row(AmendmentRow, row) for row in rows)

    async def for_clarification(self, project_id: str, clr_id: str) -> Sequence[AmendmentRow]:
        async with self._transaction() as session:
            rows = await repo.amendments_for_clarification(session, _project(project_id), clr_id)
        return tuple(_row(AmendmentRow, row) for row in rows)

    async def put(self, project_id: str, rows: Sequence[AmendmentRow]) -> None:
        async with self._transaction() as session:
            unique = _last_per_key(rows, lambda row: row.amendment_id)
            await repo.upsert_amendments(session, _project(project_id), [_values(row) for row in unique])

    async def set_status(
        self,
        project_id: str,
        amendment_ids: Sequence[str],
        status: str,
        *,
        revision_id: str | None = None,
    ) -> None:
        _check_status(status)
        async with self._transaction() as session:
            await repo.set_amendment_status(session, _project(project_id), list(amendment_ids), status, revision_id)

    async def pending_lineages(self, project_id: str) -> Sequence[str]:
        async with self._transaction() as session:
            return tuple(await repo.pending_lineages(session, _project(project_id)))
