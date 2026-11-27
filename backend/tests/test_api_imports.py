"""``ImportRunner`` (docs/mission-control/integrations-api.md "Importer seam"): the states of a source through an
import, the closed reasons of a failure, the bound on concurrent imports, and the recovery of sources a restart
interrupted. The database is a recorder of the state each source is given; the writer and the corpus are covered by
``test_api_imports_live``.

Sequence of one import::

    caller -> runner.add_source (connections row, ``listing``) -> runner.start(job, importer)
    task: slot -> state ``importing`` -> importer(job, writer) -> state ``done``
    importer raises ImportFailedError(reason) -> items ``error``, state ``failed`` with the reason
    importer raises any other error           -> items ``error``, state ``failed`` with ``unknown_error``
    importer raises SourceGoneError           -> nothing more is written (the source is gone)
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, get_args

import pytest

from api.imports import (
    IMPORT_KINDS,
    ImportFailedError,
    ImportJob,
    ImportRunner,
    ImportRunnerClosedError,
    InvalidFailureReasonError,
    NotImportedKindError,
    SourceGoneError,
)
from api.imports.runner import CLOCK_BACKOFF
from api.schemas import SourceKindName
from db import imports as imports_db
from db import sources as sources_db
from db.models import CONNECTION_KINDS
from storage.memory import InMemoryObjectStore

PROJECT, USER = uuid.UUID(int=1), uuid.UUID(int=2)
ERROR_TEXT = "text-of-the-error-canary"


class Database:
    """What the runner asks of ``db.sources`` and ``db.imports``, recorded; ``gone`` sources answer ``False``."""

    def __init__(self) -> None:
        self.states: list[tuple[uuid.UUID, str, str | None]] = []
        self.unfinished: list[uuid.UUID] = []
        self.created: list[dict[str, Any]] = []
        self.gone: set[uuid.UUID] = set()
        self.interrupted: list[tuple[str, ...]] = []
        self.before: list[datetime] = []
        self.clock = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)  # the database clock
        self.ended_by_restart: list[uuid.UUID] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def set_state(
            _session: Any, source_id: uuid.UUID, state: str, *, failure_reason: str | None = None
        ) -> bool:
            if source_id in self.gone:
                return False
            self.states.append((source_id, state, failure_reason))
            return True

        async def fail_unfinished(_session: Any, source_id: uuid.UUID, step: str | None = None) -> int:
            self.unfinished.append(source_id)
            return 0

        async def create_connection(_session: Any, **values: Any) -> tuple[Any, bool]:
            self.created.append(values)
            return SimpleNamespace(id=uuid.uuid4()), True

        async def fail_interrupted(_session: Any, kinds: Any, *, before: datetime) -> list[uuid.UUID]:
            self.interrupted.append(tuple(kinds))
            self.before.append(before)
            return self.ended_by_restart

        async def database_now(_session: Any) -> datetime:
            return self.clock

        monkeypatch.setattr(imports_db, "database_now", database_now)
        monkeypatch.setattr(sources_db, "set_connection_state", set_state)
        monkeypatch.setattr(sources_db, "fail_unfinished_items", fail_unfinished)
        monkeypatch.setattr(sources_db, "create_connection", create_connection)
        monkeypatch.setattr(imports_db, "fail_interrupted", fail_interrupted)

    def states_of(self, source_id: uuid.UUID) -> list[tuple[str, str | None]]:
        return [(state, reason) for source, state, reason in self.states if source == source_id]


@asynccontextmanager
async def transaction():
    yield None


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database()
    database.install(monkeypatch)
    return database


@pytest.fixture
async def runner(database: Database):
    made = ImportRunner(transaction=transaction, store=InMemoryObjectStore(app_env="local"))
    yield made
    await made.aclose()


def job(kind: str = "confluence") -> ImportJob:
    return ImportJob(PROJECT, uuid.uuid4(), USER, kind)


async def finished(runner: ImportRunner) -> None:
    await asyncio.wait_for(asyncio.gather(*runner._tasks.values()), timeout=2)


async def test_an_import_goes_importing_then_done_and_its_task_is_released(runner, database) -> None:
    seen: list[ImportJob] = []

    async def importer(this: ImportJob, writer: Any) -> None:
        seen.append(this)
        assert writer is not None

    this = job()
    runner.start(this, importer)
    await finished(runner)
    await asyncio.sleep(0)

    assert seen == [this]
    assert database.states_of(this.source_id) == [("importing", None), ("done", None)]
    assert runner.running() == 0


async def test_a_database_error_when_the_import_starts_ends_the_source_instead_of_leaving_it_listing(
    runner, database, monkeypatch
) -> None:
    original = sources_db.set_connection_state
    broken = {"once": True}

    async def flaky(session: Any, source_id: uuid.UUID, state: str, *, failure_reason: str | None = None) -> bool:
        if state == "importing" and broken["once"]:
            broken["once"] = False
            raise RuntimeError(ERROR_TEXT)
        return await original(session, source_id, state, failure_reason=failure_reason)

    monkeypatch.setattr(sources_db, "set_connection_state", flaky)
    ran: list[ImportJob] = []

    async def importer(this: ImportJob, _writer: Any) -> None:
        ran.append(this)

    this = job()
    runner.start(this, importer)
    await finished(runner)

    assert ran == [] and database.states_of(this.source_id) == [("failed", "unknown_error")]
    assert runner.running() == 0


@pytest.mark.parametrize("reason", ["no_access", "too_slow"])
async def test_an_importer_that_names_a_reason_fails_the_source_with_it_and_ends_its_items(
    runner, database, reason
) -> None:
    async def importer(_job: ImportJob, _writer: Any) -> None:
        raise ImportFailedError(reason)

    this = job("jira_epic")
    runner.start(this, importer)
    await finished(runner)

    assert database.states_of(this.source_id) == [("importing", None), ("failed", reason)]
    assert database.unfinished == [this.source_id]


async def test_any_other_error_fails_the_source_with_unknown_error_and_the_text_is_never_logged(
    runner, database, caplog
) -> None:
    async def importer(_job: ImportJob, _writer: Any) -> None:
        raise ValueError(ERROR_TEXT)

    this = job()
    with caplog.at_level(logging.INFO):
        runner.start(this, importer)
        await finished(runner)

    assert database.states_of(this.source_id) == [("importing", None), ("failed", "unknown_error")]
    [logged] = [r for r in caplog.records if getattr(r, "event", None) == "api.imports.failed"]
    assert (logged.kind, logged.reason, logged.error_type) == ("confluence", "unknown_error", "ValueError")
    assert ERROR_TEXT not in caplog.text


async def test_a_reason_outside_the_closed_set_cannot_be_named() -> None:
    with pytest.raises(InvalidFailureReasonError):
        ImportFailedError("anything I like")


async def test_a_source_removed_while_importing_stops_the_import_without_ending_it_again(runner, database) -> None:
    async def importer(_job: ImportJob, _writer: Any) -> None:
        raise SourceGoneError

    this = job()
    runner.start(this, importer)
    await finished(runner)

    assert database.states_of(this.source_id) == [("importing", None)]
    assert database.unfinished == []


async def test_a_source_removed_before_its_turn_is_never_imported(runner, database) -> None:
    called: list[int] = []

    async def importer(_job: ImportJob, _writer: Any) -> None:
        called.append(1)

    this = job()
    database.gone.add(this.source_id)
    runner.start(this, importer)
    await finished(runner)

    assert called == [] and database.states == []


async def test_at_most_the_bound_of_imports_run_at_once_and_the_rest_wait_for_a_slot(database) -> None:
    runner = ImportRunner(transaction=transaction, store=InMemoryObjectStore(app_env="local"), max_concurrent=2)
    gate, two_running, active, peak = asyncio.Event(), asyncio.Event(), [0], [0]

    async def importer(_job: ImportJob, _writer: Any) -> None:
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        if active[0] == 2:
            two_running.set()
        await gate.wait()
        active[0] -= 1

    jobs = [job() for _ in range(5)]
    for this in jobs:
        runner.start(this, importer)
    await two_running.wait()
    for _ in range(20):  # the tasks that wait for a slot get every chance to start
        await asyncio.sleep(0)
    assert (active[0], runner.running()) == (2, 5)
    gate.set()
    await finished(runner)

    assert peak[0] == 2
    assert all(database.states_of(this.source_id)[-1] == ("done", None) for this in jobs)
    await runner.aclose()


async def test_one_task_per_source_a_second_start_changes_nothing(runner, database) -> None:
    gate, started, calls = asyncio.Event(), asyncio.Event(), []

    async def importer(_job: ImportJob, _writer: Any) -> None:
        calls.append(1)
        started.set()
        await gate.wait()

    this = job()
    runner.start(this, importer)
    runner.start(this, importer)
    await started.wait()
    for _ in range(20):
        await asyncio.sleep(0)
    gate.set()
    await finished(runner)

    assert calls == [1]


async def test_closing_cancels_the_running_imports_and_refuses_new_ones(database) -> None:
    runner = ImportRunner(transaction=transaction, store=InMemoryObjectStore(app_env="local"))
    started = asyncio.Event()

    async def importer(_job: ImportJob, _writer: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    this = job()
    runner.start(this, importer)
    await started.wait()

    await runner.aclose()

    assert runner.running() == 0
    assert database.states_of(this.source_id) == [("importing", None)]  # the next start's recovery ends it
    with pytest.raises(ImportRunnerClosedError):
        runner.start(job(), importer)


async def test_the_start_ends_the_sources_of_the_imported_kinds_a_restart_interrupted(runner, database) -> None:
    database.ended_by_restart = [uuid.uuid4(), uuid.uuid4()]

    await runner.start_recovery()
    await asyncio.wait_for(runner._recovery, timeout=2)

    assert database.interrupted == [IMPORT_KINDS] and IMPORT_KINDS == (
        "confluence",
        "jira_epic",
        "linear_project",
        "box",
        "dropbox",
        "drawio",
    )
    assert imports_db.INTERRUPTED_REASON == "unknown_error"


async def test_a_source_is_added_with_a_batch_style_external_id_and_the_importing_member(runner, database) -> None:
    source_id = await runner.add_source(
        PROJECT, USER, kind="jira_epic", name="Epic ABC-1", link="https://x/browse/ABC-1"
    )

    [values] = database.created
    assert (values["project_id"], values["kind"], values["connected_by"], values["name"]) == (
        PROJECT,
        "jira_epic",
        USER,
        "Epic ABC-1",
    )
    assert re.fullmatch(r"[A-Za-z0-9_-]{10,}", values["external_id"])  # the check constraint of connections
    assert isinstance(source_id, uuid.UUID)
    with pytest.raises(NotImportedKindError):
        await runner.add_source(PROJECT, USER, kind="gdrive", name="n", link="l")


def test_the_imported_kinds_are_source_kinds_the_database_and_the_api_accept() -> None:
    assert set(IMPORT_KINDS) <= set(CONNECTION_KINDS) and "upload" not in IMPORT_KINDS
    assert set(get_args(SourceKindName)) == set(CONNECTION_KINDS)  # the sources status can name every kind


async def test_the_recovery_ends_only_sources_touched_before_the_database_clock_of_the_start(runner, database) -> None:
    await runner.start_recovery()
    await asyncio.wait_for(runner._recovery, timeout=2)

    # the bound is one instant of the database clock, read once: a new import of this process, or of another replica,
    # was touched after it
    assert database.before == [database.clock]


async def test_a_database_that_cannot_give_its_clock_ends_no_source_and_the_start_goes_on(
    runner, database, monkeypatch, caplog
) -> None:
    async def broken(_session: Any) -> datetime:
        raise OSError(ERROR_TEXT)

    monkeypatch.setattr(imports_db, "database_now", broken)
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    runner._sleep = sleep

    with caplog.at_level(logging.WARNING):
        await runner.start_recovery()

    assert runner._recovery is None and database.interrupted == []
    assert "api.imports.recovery_failed" in caplog.text and ERROR_TEXT not in caplog.text
    assert waits == list(CLOCK_BACKOFF)  # it tried again after each wait, then gave up, logging once
    assert caplog.text.count("api.imports.recovery_failed") == 1


async def test_a_database_that_is_late_at_the_start_gives_its_clock_on_a_new_try(runner, database, monkeypatch) -> None:
    tries: list[int] = []

    async def late(_session: Any) -> datetime:
        tries.append(1)
        if len(tries) < 3:
            raise OSError(ERROR_TEXT)
        return database.clock

    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(imports_db, "database_now", late)
    runner._sleep = sleep

    await runner.start_recovery()
    await asyncio.wait_for(runner._recovery, timeout=2)

    assert len(tries) == 3 and waits == list(CLOCK_BACKOFF[:2])
    assert database.before == [database.clock]  # the recovery ran with the clock it finally read


async def test_a_lost_race_on_an_item_fails_the_source_with_the_closed_unknown_reason(runner, database) -> None:
    async def importer(_job: ImportJob, _writer: Any) -> None:
        raise imports_db.ItemDeclareError

    this = job()
    runner.start(this, importer)
    await finished(runner)
    await asyncio.sleep(0)

    assert database.states_of(this.source_id) == [("importing", None), ("failed", "unknown_error")]
    assert database.unfinished == [this.source_id]
