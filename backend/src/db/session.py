from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from settings import DatabaseSettings


def build_engine(
    settings: DatabaseSettings,
    *,
    echo: bool = False,
) -> AsyncEngine:
    """Build a lazy async engine; no connection is made until first use.

    ``hide_parameters`` keeps bound values (emails, filenames, hashes) out of
    SQLAlchemy error messages, which the API and the worker may log.
    The caller owns ``await engine.dispose()``.
    """

    return create_async_engine(settings.url(), pool_pre_ping=True, echo=echo, hide_parameters=True)


def build_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Build a session factory whose objects stay readable after commit."""

    return async_sessionmaker(engine, expire_on_commit=False)
