"""Shared tier-2 Postgres fixtures for every step (no tests here).

Gate: ``SPECFLOW_TEST_POSTGRES=1`` and ``uv run --env-file ../.env.local``.

Usage from another test module (``tests/conftest.py`` registers this module as a pytest
plugin, so the fixture names need no import)::

    from specflow_live import requires_live

    pytestmark = requires_live("POSTGRES")

    async def test_x(migrated_database, agent_role):
        app_settings = migrated_database              # DatabaseSettings, app role
        agent_settings = agent_role.settings()        # AgentDatabaseSettings
        # build_agent_session_factory(migrated_database, agent_settings) ...

Rules (parallel-safe; AC-3.2b):
- ``migrated_database`` / ``scratch_database``: a fresh database
  ``specflow_pytest_<hex12>`` per test, dropped (WITH (FORCE)) afterwards. The
  dev database ``DATABASE_NAME`` is never migrated or written.
- ``agent_role``: the agent role of THIS pytest session,
  ``specflow_agent_test_<hex8>`` (``SESSION_AGENT_ROLE``), with a random
  password, created once and dropped at session end. Every session (and every
  xdist worker) has its own name, so parallel sessions never touch each
  other's role. Never create, alter or drop roles yourself; never use
  ``specflow_agent``.
- ``alembic_config(db)`` binds a scratch database and the session role.
- Creating and dropping databases and roles uses the Postgres admin
  (``postgres`` with ``POSTGRES_ADMIN_PASSWORD``, as in the local stack's
  Postgres image). The app role ``DATABASE_USER`` stays a plain role and owns
  each scratch database, as in a managed deployment.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from settings import AgentDatabaseSettings, DatabaseSettings

ALEMBIC_INI: Final = Path(__file__).resolve().parents[1] / "alembic.ini"
SCRATCH_PREFIX: Final = "specflow_pytest_"
MAINTENANCE_DATABASE: Final = "postgres"
ADMIN_USER: Final = "postgres"
TEST_AGENT_ROLE_PREFIX: Final = "specflow_agent_test_"
# Chosen once per pytest process (session / xdist worker).
SESSION_AGENT_ROLE: Final = f"{TEST_AGENT_ROLE_PREFIX}{uuid.uuid4().hex[:8]}"
FORBIDDEN_ROLE: Final = "specflow_agent"


@dataclass(frozen=True)
class AgentRole:
    name: str
    password: str

    def settings(self) -> AgentDatabaseSettings:
        return AgentDatabaseSettings(DATABASE_AGENT_USER=self.name, DATABASE_AGENT_PASSWORD=self.password)


def quote(name: str) -> str:
    return postgresql.dialect().identifier_preparer.quote_identifier(name)


def admin_url(configured: DatabaseSettings):
    """The Postgres admin on the maintenance database of the configured server."""

    password = os.environ.get("POSTGRES_ADMIN_PASSWORD", "")
    if not password:
        pytest.fail("POSTGRES_ADMIN_PASSWORD is needed for the live Postgres tests")
    return configured.url().set(username=ADMIN_USER, password=password, database=MAINTENANCE_DATABASE)


def run_admin(configured: DatabaseSettings, *statements: str, params: dict | None = None) -> None:
    """Run statements as the Postgres admin, in autocommit, on the maintenance database."""

    async def execute() -> None:
        admin = create_async_engine(admin_url(configured), isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as connection:
                for statement in statements:
                    await connection.execute(text(statement), params or {})
        finally:
            await admin.dispose()

    asyncio.run(execute())


def query_column(settings: DatabaseSettings, statement: str) -> list:
    assert settings.name.startswith(SCRATCH_PREFIX), "not a scratch database"

    async def query() -> list:
        engine = create_async_engine(settings.url())
        try:
            async with engine.connect() as connection:
                return [row[0] for row in await connection.execute(text(statement))]
        finally:
            await engine.dispose()

    return asyncio.run(query())


def _set_role(configured: DatabaseSettings, role: AgentRole) -> None:
    assert role.name.startswith(TEST_AGENT_ROLE_PREFIX)

    async def execute() -> None:
        admin = create_async_engine(
            admin_url(configured),
            isolation_level="AUTOCOMMIT",
            hide_parameters=True,  # the role password is a bound parameter
        )
        try:
            async with admin.connect() as connection:
                exists = await connection.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": role.name})
                if exists is None:
                    await connection.execute(text(f"CREATE ROLE {quote(role.name)}"))
                statement = await connection.scalar(
                    text(
                        "SELECT format('ALTER ROLE %I LOGIN NOBYPASSRLS NOSUPERUSER "
                        "NOCREATEDB NOCREATEROLE PASSWORD %L', "
                        "CAST(:n AS text), CAST(:p AS text))"
                    ),
                    {"n": role.name, "p": role.password},
                )
                try:
                    await connection.execute(text(statement))
                except DBAPIError:
                    # The statement text holds the password: never surface it.
                    raise RuntimeError(f"could not configure the role {role.name}") from None
        finally:
            await admin.dispose()

    asyncio.run(execute())


@pytest.fixture(scope="session")
def configured_database() -> DatabaseSettings:
    return DatabaseSettings.from_environment()


@pytest.fixture(scope="session")
def agent_role(configured_database: DatabaseSettings) -> Iterator[AgentRole]:
    """The test agent role with a fresh random password; dropped at session end."""

    role = AgentRole(SESSION_AGENT_ROLE, secrets.token_urlsafe(24))
    _set_role(configured_database, role)
    try:
        yield role
    finally:
        try:
            run_admin(configured_database, f"DROP ROLE IF EXISTS {quote(role.name)}")
        except DBAPIError:
            # Only a leaked scratch database can still hold its grants.
            warnings.warn(f"could not drop the test role {role.name}", stacklevel=1)


@pytest.fixture
def scratch_database(configured_database: DatabaseSettings) -> Iterator[DatabaseSettings]:
    """An empty scratch database on the configured server; dropped afterwards."""

    name = f"{SCRATCH_PREFIX}{uuid.uuid4().hex[:12]}"
    scratch = configured_database.model_copy(update={"name": name})
    assert scratch.name != configured_database.name
    run_admin(
        configured_database,
        f"CREATE DATABASE {quote(name)} OWNER {quote(configured_database.user)}",
    )
    try:
        yield scratch
    finally:
        run_admin(configured_database, f"DROP DATABASE IF EXISTS {quote(name)} WITH (FORCE)")


def alembic_config(scratch: DatabaseSettings, agent_user: str = SESSION_AGENT_ROLE) -> Config:
    """Alembic config bound to the scratch database (a URL object keeps the password)."""

    assert scratch.name.startswith(SCRATCH_PREFIX), "not a scratch database"
    assert agent_user != FORBIDDEN_ROLE
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["database_url"] = scratch.url()
    cfg.attributes["agent_user"] = agent_user
    return cfg


@pytest.fixture
def migrated_database(scratch_database: DatabaseSettings, agent_role: AgentRole) -> DatabaseSettings:
    """A scratch database at head, with RLS bound to the test agent role."""

    command.upgrade(alembic_config(scratch_database, agent_role.name), "head")
    return scratch_database
