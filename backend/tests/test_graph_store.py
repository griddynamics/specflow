"""Offline tests of the store port (graph.core.store): retry helper, project binding, protocol detection."""

from __future__ import annotations

from typing import Any

import pytest

from graph.core.errors import (
    ConformanceError,
    GraphError,
    InfrastructureUnavailableError,
)
from graph.core.queries import PING, Query
from graph.core.store import (
    RETRY_DELAYS_SECONDS,
    Executor,
    GraphStore,
    StoreCapabilities,
    StoreInfo,
    as_executor,
    bind_project,
    is_executor,
    is_store,
    retry_transient,
)

SCOPED = Query("scoped", "core.read_nodes")


class Boom(Exception):
    def __init__(self, transient: bool) -> None:
        super().__init__("boom")
        self.transient = transient


def mapped(error: BaseException, attempts: int) -> GraphError:
    return InfrastructureUnavailableError(
        "store_read_failed", attempts=attempts, error_type=type(error).__name__
    )


async def run(
    failures: list[bool], *, delays: tuple[float, ...] = (0.1, 0.3)
) -> tuple[Any, list[float], list[int]]:
    """Run an operation that raises ``Boom(transient)`` per entry of ``failures``, then returns ``"ok"``."""

    waits: list[float] = []
    retried: list[int] = []
    pending = list(failures)

    async def operation() -> str:
        if pending:
            raise Boom(pending.pop(0))
        return "ok"

    async def sleep(delay: float) -> None:
        waits.append(delay)

    result = await retry_transient(
        operation,
        errors=(Boom,),
        is_transient=lambda error: isinstance(error, Boom) and error.transient,
        map_error=mapped,
        delays=delays,
        sleep=sleep,
        on_retry=lambda _error, attempt: retried.append(attempt),
    )
    return result, waits, retried


def test_the_default_delays_are_those_of_the_neo4j_executor() -> None:
    assert RETRY_DELAYS_SECONDS == (0.1, 0.3, 1.0)


async def test_a_call_without_failure_returns_its_result() -> None:
    assert await run([]) == ("ok", [], [])


async def test_a_transient_failure_is_retried_after_each_delay() -> None:
    assert await run([True, True]) == ("ok", [0.1, 0.3], [1, 2])


async def test_a_transient_failure_after_the_last_retry_is_mapped_with_the_attempt_count() -> (
    None
):
    with pytest.raises(InfrastructureUnavailableError) as raised:
        await run([True, True, True])
    assert raised.value.fields["attempts"] == 3
    assert raised.value.code == "store_read_failed"


async def test_a_permanent_failure_is_not_retried() -> None:
    with pytest.raises(InfrastructureUnavailableError) as raised:
        await run([False])
    assert raised.value.fields["attempts"] == 1


async def test_a_failure_outside_errors_propagates_unmapped() -> None:
    async def operation() -> None:
        raise KeyError("x")

    with pytest.raises(KeyError):
        await retry_transient(
            operation,
            errors=(Boom,),
            is_transient=lambda _error: True,
            map_error=mapped,
            delays=(0.0,),
        )


def test_bind_project_injects_the_validated_project_id() -> None:
    assert bind_project(SCOPED, "proj-1", {"limit": 3}) == {
        "limit": 3,
        "project_id": "proj-1",
    }


def test_bind_project_refuses_an_unscoped_query() -> None:
    with pytest.raises(ConformanceError) as raised:
        bind_project(PING, "proj-1", {})
    assert raised.value.code == "query_not_project_scoped"


def test_bind_project_refuses_a_caller_project_id() -> None:
    with pytest.raises(ConformanceError) as raised:
        bind_project(SCOPED, "proj-1", {"project_id": "other"})
    assert raised.value.code == "project_id_parameter_reserved"


def test_bind_project_validates_the_project_id() -> None:
    with pytest.raises(GraphError):
        bind_project(SCOPED, "bad id!", {})


def test_store_info_defaults_to_a_transactional_store_without_version() -> None:
    info = StoreInfo("neo4j")
    assert info.capabilities == StoreCapabilities(multi_statement_tx=True)
    assert info.server_version is None


class FakeExecutor:
    async def read(
        self, query: Query, project_id: str, /, **params: Any
    ) -> list[dict[str, Any]]:
        return []

    async def write(
        self, query: Query, project_id: str, /, **params: Any
    ) -> list[dict[str, Any]]:
        return []

    def tx(self, project_id: str) -> Any:
        raise NotImplementedError


class FakeStore:
    def __init__(self) -> None:
        self.info = StoreInfo("fake", StoreCapabilities(multi_statement_tx=False))
        self._executor = FakeExecutor()

    def executor(self) -> FakeExecutor:
        return self._executor

    async def check(self) -> None:
        return None

    async def ensure_schema(
        self, ontology: Any, embedding_dim: int, *, ddl: Any = None
    ) -> tuple[str, ...]:
        return ()

    async def close(self) -> None:
        return None


def test_protocol_detection() -> None:
    store, executor = FakeStore(), FakeExecutor()
    assert is_store(store) and not is_executor(store)
    assert is_executor(executor) and not is_store(executor)
    assert isinstance(store, GraphStore) and isinstance(executor, Executor)


def test_as_executor_takes_a_store_or_an_executor() -> None:
    store, executor = FakeStore(), FakeExecutor()
    assert as_executor(store) is store.executor()
    assert as_executor(executor) is executor
