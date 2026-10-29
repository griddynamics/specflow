from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import URL, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from db.models import Document
from settings import DatabaseSettings

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# No stdlib logging setup here: that is reserved for src/logger.

# MetaData for 'autogenerate' support.
# Importing the models registers every table on the shared Base.metadata.
target_metadata = Document.metadata

DEFAULT_AGENT_USER = "specflow_agent"


def database_url() -> str | URL:
    """Return the database URL for this run.

    Programmatic callers (tests) set ``config.attributes["database_url"]`` to
    a ``str`` or a ``URL``; otherwise it comes from ``DATABASE_*`` through
    ``DatabaseSettings``. alembic.ini never holds a URL or a password.
    """

    url: str | URL | None = config.attributes.get("database_url")
    if url is not None:
        return url
    return DatabaseSettings.from_environment().url()


def agent_user() -> str:
    """Return the agent role name that revision 0001_initial grants to.

    ``config.attributes["agent_user"]`` (tests, ``specflow migrate``) wins,
    then ``DATABASE_AGENT_USER``, then ``specflow_agent``. Only the name is
    read; the agent password never reaches the migrations.
    """

    name = config.attributes.get("agent_user")
    if name:
        return str(name)
    return os.environ.get("DATABASE_AGENT_USER", "").strip() or DEFAULT_AGENT_USER


config.attributes["agent_user"] = agent_user()


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = create_async_engine(database_url(), poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
