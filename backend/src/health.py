"""Dependency health checks shared by ``/readyz`` and ``specflow worker --check``.

A check never reports exception text: ``detail`` is one of the fixed codes
below. Each probe has its own 2 s timeout and the API caches a whole snapshot
for 10 s (SPECS AC-7.11, AC-4.5, AC-2.3).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import text

from db import session as db_session
from graph import core as graph_core
from logger.logger import INFO, WARNING, get_logger, log_event
from settings import Role, RoleSettings, load_settings
from storage import factory as storage_factory
from temporal import client as temporal_client

logger = get_logger("health")

CHECK_TIMEOUT_SECONDS: Final = 2.0
CACHE_SECONDS: Final = 10.0
CLI_TIMEOUT_SECONDS: Final = 5.0  # `worker --check` connects from scratch
DETAIL_OK: Final = "ok"
DETAIL_TIMEOUT: Final = "timeout"
DETAIL_UNAVAILABLE: Final = "unavailable"
CHECK_NAMES: Final = ("database", "temporal", "storage", "neo4j")

Probe = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One dependency check: ``ok`` plus a fixed ``detail`` code (never exception text)."""

    name: str
    ok: bool
    detail: str
    duration_ms: int


class CheckReport(dict[str, CheckResult]):
    """``dict[str, CheckResult]`` whose ``int()`` is the process exit code (0 all ok, else 1)."""

    @property
    def all_ok(self) -> bool:
        return all(result.ok for result in self.values())

    def __int__(self) -> int:
        return 0 if self.all_ok else 1


class _ProbeFailedError(Exception):
    """A probe failure reduced to its exception type name; its text is never kept here."""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


async def _await_probe(probe: Probe, limit_seconds: float) -> None:
    """Await ``probe`` within the limit: a timeout stays ``TimeoutError``, any other failure
    becomes ``_ProbeFailedError`` (only its type name is carried on)."""

    try:
        await asyncio.wait_for(probe(), limit_seconds)
    except TimeoutError:
        raise
    except Exception as error:  # any failure means not ready
        raise _ProbeFailedError(type(error).__name__) from error


async def run_probe(name: str, probe: Probe, limit_seconds: float = CHECK_TIMEOUT_SECONDS) -> CheckResult:
    started = time.perf_counter()
    ok, detail = True, DETAIL_OK
    try:
        await _await_probe(probe, limit_seconds)
    except TimeoutError:
        ok, detail = False, DETAIL_TIMEOUT
    except _ProbeFailedError as failure:  # text is never exposed: only the type name is logged
        ok, detail = False, DETAIL_UNAVAILABLE
        log_event(
            logger,
            WARNING,
            "health.check.failed",
            check=name,
            error_type=failure.error_type,
        )
    return CheckResult(name, ok, detail, int((time.perf_counter() - started) * 1000))


async def run_probes(probes: dict[str, Probe], limit_seconds: float = CHECK_TIMEOUT_SECONDS) -> CheckReport:
    results = await asyncio.gather(*(run_probe(name, probe, limit_seconds) for name, probe in probes.items()))
    return CheckReport({result.name: result for result in results})


class Readiness:
    """Runs the probes at most once per ``ttl`` seconds; concurrent callers share one run."""

    def __init__(
        self,
        probes: dict[str, Probe],
        *,
        ttl: float = CACHE_SECONDS,
        timeout: float = CHECK_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probes, self._ttl, self._timeout, self._clock = (
            probes,
            ttl,
            timeout,
            clock,
        )
        self._lock = asyncio.Lock()
        self._at: float | None = None
        self._report = CheckReport()

    async def snapshot(self) -> CheckReport:
        async with self._lock:
            now = self._clock()
            if self._at is None or now - self._at >= self._ttl:
                self._report = await run_probes(self._probes, self._timeout)
                self._at = self._clock()
            return self._report


def database_probe(engine: Any) -> Probe:
    async def probe() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    return probe


def temporal_probe(client: Any) -> Probe:
    async def probe() -> None:
        if not await client.service_client.check_health(timeout=timedelta(seconds=CHECK_TIMEOUT_SECONDS)):
            raise RuntimeError("temporal is not serving")

    return probe


def storage_probe(store: Any) -> Probe:
    check: Probe = store.check  # a cached listing, never an object read
    return check


def neo4j_probe(driver: Any, database: str | None) -> Probe:
    async def probe() -> None:
        await graph_core.check(driver, database)

    return probe


async def run_checks(role: Role, *, settings: RoleSettings | None = None) -> CheckReport:
    """Check every dependency ``role`` needs, with throwaway connections.

    Loads the settings from the environment unless given. ``int(report)`` is the
    exit code ``specflow worker --check`` returns.
    """

    if settings is None:
        settings = load_settings(role)
    cleanups: list[Callable[[], Awaitable[None]]] = []
    probes: dict[str, Probe] = {}
    try:
        engine = db_session.build_engine(settings.database)
        cleanups.append(engine.dispose)
        probes["database"] = database_probe(engine)
        if settings.temporal is not None:
            probes["temporal"] = _temporal_check(settings)
        if settings.storage is not None:
            probes["storage"] = storage_probe(
                storage_factory.build_object_store(settings.storage, app_env=settings.app.app_env)
            )
        if settings.neo4j is not None:
            driver = graph_core.connect(settings.neo4j.to_graph_config())
            cleanups.append(driver.close)
            probes["neo4j"] = neo4j_probe(driver, settings.neo4j.database)
        report = await run_probes(probes, CLI_TIMEOUT_SECONDS)
    finally:
        for cleanup in cleanups:
            await asyncio.shield(cleanup())
    for result in report.values():
        log_event(
            logger,
            INFO,
            "health.check.completed",
            role=role,
            check=result.name,
            ok=result.ok,
            detail=result.detail,
            duration_ms=result.duration_ms,
        )
    return report


def _temporal_check(settings: RoleSettings) -> Probe:
    async def probe() -> None:
        connection = await temporal_client.open_connection(settings)
        try:
            await temporal_probe(connection.client)()
        finally:
            await asyncio.to_thread(connection.tracing.shutdown)

    return probe
