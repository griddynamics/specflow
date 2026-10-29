from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from db.session import build_engine, build_session_factory
from settings import DatabaseSettings

# `.invalid` is reserved (RFC 2606) and never resolves. The engine is lazy, so
# building it must succeed without any lookup or connection; if it ever
# connected at build time, these tests would fail instead of reaching a server.
PLACEHOLDER_CRED = "p"
SETTINGS = DatabaseSettings(
    host="invalid.invalid",
    port=5432,
    name="d",
    user="u",
    password=PLACEHOLDER_CRED,
)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    built = build_engine(SETTINGS)
    try:
        yield built
    finally:
        await built.dispose()


# T7 ------------------------------------------------------------------------


async def test_build_engine_returns_a_lazy_async_asyncpg_engine(
    engine: AsyncEngine,
) -> None:
    assert isinstance(engine, AsyncEngine)
    assert engine.dialect.is_async
    assert engine.dialect.driver == "asyncpg"
    assert engine.url.drivername == "postgresql+asyncpg"
    assert engine.url.host == "invalid.invalid"
    assert engine.url.port == 5432
    # No connection has been opened or even attempted.
    assert engine.pool.checkedin() == 0
    assert engine.pool.checkedout() == 0


async def test_build_engine_pings_connections_and_does_not_echo(
    engine: AsyncEngine,
) -> None:
    assert engine.echo is False
    # No public accessor exists for pool_pre_ping.
    assert engine.sync_engine.pool._pre_ping is True


async def test_session_factory_builds_sessions_that_survive_commit(
    engine: AsyncEngine,
) -> None:
    factory = build_session_factory(engine)

    assert isinstance(factory, async_sessionmaker)
    assert factory.class_ is AsyncSession
    assert factory.kw["expire_on_commit"] is False
    assert factory.kw["bind"] is engine

    async with factory() as session:
        assert isinstance(session, AsyncSession)
        assert session.sync_session.expire_on_commit is False
        assert session.bind is engine
