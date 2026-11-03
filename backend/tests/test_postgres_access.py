"""The shared PostgreSQL access (``postgres``): one engine factory and one connection transaction helper."""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

import postgres


def test_every_engine_pings_its_connections_and_hides_bound_values(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(postgres, "create_async_engine", lambda url, **kwargs: seen.append({"url": url, **kwargs}))
    postgres.new_engine("postgresql+asyncpg://h/d")
    postgres.new_engine(
        "postgresql+asyncpg://h/g", pool_size=3, max_overflow=1, pool_timeout=2.0, connect_args={"a": 1}
    )
    plain, pooled = seen
    for kwargs in (plain, pooled):
        assert kwargs["pool_pre_ping"] is True and kwargs["hide_parameters"] is True
    assert "pool_size" not in plain and plain["connect_args"] == {}
    assert (pooled["pool_size"], pooled["max_overflow"], pooled["pool_timeout"]) == (3, 1, 2.0)
    assert pooled["connect_args"] == {"a": 1}


class FakeTransaction:
    def __init__(self, events: list[str], *, rollback_fails: bool = False) -> None:
        self.events, self.is_active, self.rollback_fails = events, True, rollback_fails

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")
        if self.rollback_fails:
            raise OperationalError("rollback", {}, OSError("gone"))


class FakeConnection:
    def __init__(self, events: list[str], **options: bool) -> None:
        self.events, self.options = events, options

    async def execution_options(self, **options: Any) -> FakeConnection:
        self.events.append(f"options {sorted(options)}")
        return self

    async def begin(self) -> FakeTransaction:
        self.events.append("begin")
        return FakeTransaction(self.events, **self.options)

    async def __aenter__(self) -> FakeConnection:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.events.append("close")


class FakeEngine:
    def __init__(self, **options: bool) -> None:
        self.events: list[str] = []
        self.options = options

    def connect(self) -> FakeConnection:
        return FakeConnection(self.events, **self.options)


async def test_a_transaction_commits_when_its_block_ends() -> None:
    engine = FakeEngine()
    async with postgres.transaction(cast(AsyncEngine, engine)) as connection:
        assert isinstance(connection, FakeConnection)
    assert engine.events == ["begin", "commit", "close"]


async def test_a_read_only_transaction_says_so_before_it_begins() -> None:
    engine = FakeEngine()
    async with postgres.transaction(cast(AsyncEngine, engine), read_only=True):
        pass
    assert engine.events == ["options ['postgresql_readonly']", "begin", "commit", "close"]


async def test_a_failing_block_rolls_back_and_its_own_error_propagates() -> None:
    engine = FakeEngine(rollback_fails=True)
    dropped: list[BaseException] = []
    with pytest.raises(ValueError, match="block"):
        async with postgres.transaction(cast(AsyncEngine, engine), on_rollback_error=dropped.append):
            raise ValueError("block")
    assert engine.events == ["begin", "rollback", "close"] and len(dropped) == 1


async def test_a_failing_rollback_without_a_handler_propagates() -> None:
    engine = FakeEngine(rollback_fails=True)
    with pytest.raises(OperationalError):
        async with postgres.transaction(cast(AsyncEngine, engine)):
            raise ValueError("block")
