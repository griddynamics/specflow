from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
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


@asynccontextmanager
async def transaction(session_factory: Any) -> AsyncIterator[AsyncSession]:
    """One session and one transaction, committed on success and rolled back on an exception.

    The unit of work of a caller that owns no request-scoped session (the MCP role's tools).
    """

    async with session_factory() as session, session.begin():
        yield session


_ASYNCPG_CONNECTION_ERRORS: Final = frozenset({"PostgresConnectionError", "InterfaceError"})


def _is_asyncpg_connection_error(error: BaseException) -> bool:
    """``isinstance`` of ``asyncpg.PostgresConnectionError | asyncpg.InterfaceError`` by class name.

    asyncpg ships no type information, so its classes are recognised along the error's MRO.
    """

    return any(
        cls.__module__.split(".", 1)[0] == "asyncpg" and cls.__name__ in _ASYNCPG_CONNECTION_ERRORS
        for cls in type(error).__mro__
    )


def is_database_unreachable(error: BaseException) -> bool:
    """True only for connectivity failures; a programming error stays a bug (500)."""

    if isinstance(error, OperationalError | InterfaceError | OSError):
        return True  # OSError covers ConnectionError, timeouts, DNS failures
    if isinstance(error, DBAPIError):
        return error.connection_invalidated
    return _is_asyncpg_connection_error(error)
