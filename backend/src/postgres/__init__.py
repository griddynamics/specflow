"""Shared PostgreSQL access: the one place that builds an async engine and runs a connection transaction.

Every PostgreSQL connection of the service goes through here: the application database (``db.session``,
``db.migrate``) and the PostgreSQL graph store (``graph.stores.postgres``). The module imports SQLAlchemy
only, never a project package, so the graph library and the host can both use it.

Every engine checks a pooled connection before use (``pool_pre_ping``) and keeps bound values (emails, file
names, document text, vectors) out of SQLAlchemy's error messages (``hide_parameters``), which callers log.
``ssl_argument`` turns a libpq ``sslmode`` and CA bundle into the TLS connect argument of asyncpg; every engine
passes one, so asyncpg never reads ``PGSSLMODE`` or ``PGSSLROOTCERT`` from the environment itself.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final

from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

__all__ = ["BUNDLE_MODES", "SSL_MODES", "VERIFY_MODES", "new_engine", "ssl_argument", "transaction"]

# The libpq ``sslmode`` values; unset means ``prefer``, as in libpq.
SSL_MODES: Final = frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"})
VERIFY_MODES: Final = frozenset({"verify-ca", "verify-full"})
# The modes that use a CA bundle: optional for ``require``, needed by the verify modes.
BUNDLE_MODES: Final = frozenset({"require", *VERIFY_MODES})


def ssl_argument(mode: str | None, rootcert: str | None = None) -> bool | str | ssl.SSLContext:
    """The asyncpg ``ssl`` connect argument of the libpq ``sslmode`` ``mode`` (``None`` is ``prefer``).

    ``disable`` is no TLS; ``allow`` and ``prefer`` stay asyncpg's (TLS when the server offers it, no certificate
    check). ``require``, ``verify-ca`` and ``verify-full`` get a context that trusts ``rootcert`` alone, never the
    system store, and offers the ``postgresql`` ALPN that direct TLS needs: ``require`` checks the chain only when
    given a ``rootcert``, the verify modes need one and ``verify-full`` also checks the host name. Raises
    ``ValueError`` for an unknown mode, a verify mode without ``rootcert`` or a ``rootcert`` with a mode that
    does not use one, and ``OSError`` (``ssl.SSLError`` too) when the bundle is missing, unreadable or empty.
    """

    mode = mode or "prefer"
    if mode not in SSL_MODES:
        raise ValueError(f"unknown sslmode {mode!r}")
    if rootcert and mode not in BUNDLE_MODES:
        raise ValueError(f"sslmode {mode} uses no CA bundle")
    if mode in VERIFY_MODES and not rootcert:
        raise ValueError(f"sslmode {mode} needs a CA bundle")
    if mode == "disable":
        return False
    if mode not in BUNDLE_MODES:
        return mode
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.set_alpn_protocols(["postgresql"])
    context.check_hostname = mode == "verify-full"
    if rootcert:
        context.load_verify_locations(cafile=rootcert)
    else:
        context.verify_mode = ssl.CERT_NONE
    return context


def new_engine(
    url: str | URL,
    *,
    echo: bool = False,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    pool_timeout: float | None = None,
    connect_args: Mapping[str, Any] | None = None,
) -> AsyncEngine:
    """A lazy async engine for ``url``: no connection is made until first use. The caller owns
    ``await engine.dispose()``. The pool options left out keep SQLAlchemy's defaults; ``connect_args`` without
    ``ssl`` gets ``prefer``."""

    pool: dict[str, Any] = {
        name: value
        for name, value in (("pool_size", pool_size), ("max_overflow", max_overflow), ("pool_timeout", pool_timeout))
        if value is not None
    }
    return create_async_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"ssl": ssl_argument(None), **(connect_args or {})},
        **pool,
    )


@asynccontextmanager
async def transaction(
    engine: AsyncEngine,
    *,
    read_only: bool = False,
    on_rollback_error: Callable[[BaseException], None] | None = None,
) -> AsyncIterator[AsyncConnection]:
    """One connection and one transaction: committed when the block ends, rolled back when it raises.

    ``read_only`` opens the transaction read only (``postgresql_readonly``), so a read cannot change data
    whatever its statement says. A rollback that fails itself is handed to ``on_rollback_error`` (and then
    dropped), so the error that ended the block is the one that propagates; without a handler it propagates.
    """

    async with engine.connect() as opened:
        connection = await opened.execution_options(postgresql_readonly=True) if read_only else opened
        work = await connection.begin()
        try:
            yield connection
        except BaseException:
            if work.is_active:
                try:
                    await work.rollback()
                except (SQLAlchemyError, OSError) as error:
                    if on_rollback_error is None:
                        raise
                    on_rollback_error(error)
            raise
        await work.commit()
