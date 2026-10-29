"""Tier 2 (AC-3.5): the roles the Postgres image created on its first start.

Needs ``SPECFLOW_TEST_POSTGRES=1`` and the local stack's ``postgres`` service
(``postgres:17.11`` with ``backend/postgres-init/`` mounted). Its init script
``10-specflow-roles.sh`` creates the app role (owner of the database, not a superuser)
and the agent role (row-level security applies, limited). This test only reads
``pg_roles`` and ``pg_database`` as the app role; it changes nothing.
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from settings import DatabaseSettings
from specflow_live import requires_live

pytestmark = requires_live("POSTGRES")

FLAGS = (
    "SELECT rolcanlogin, rolbypassrls, rolsuper, rolcreatedb, rolcreaterole, "
    "rolconnlimit, rolconfig FROM pg_roles WHERE rolname = :n"
)


def role(configured: DatabaseSettings, name: str) -> tuple:
    async def query() -> tuple:
        engine = create_async_engine(configured.url())
        try:
            async with engine.connect() as connection:
                row = (await connection.execute(text(FLAGS), {"n": name})).one()
                return (*row[:6], sorted(row[6] or []))
        finally:
            await engine.dispose()

    return asyncio.run(query())


def database_owner(configured: DatabaseSettings) -> str:
    async def query() -> str:
        engine = create_async_engine(configured.url())
        try:
            async with engine.connect() as connection:
                return await connection.scalar(
                    text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()")
                )
        finally:
            await engine.dispose()

    return asyncio.run(query())


def test_the_app_role_owns_the_database_and_is_a_plain_role(
    configured_database: DatabaseSettings,
) -> None:
    login, bypassrls, superuser, createdb, createrole, _, _ = role(configured_database, configured_database.user)

    assert (login, bypassrls, superuser, createdb, createrole) == (
        True,
        False,
        False,
        False,
        False,
    )
    assert database_owner(configured_database) == configured_database.user


def test_the_agent_role_is_limited_and_subject_to_row_level_security(
    configured_database: DatabaseSettings,
) -> None:
    name = os.environ.get("DATABASE_AGENT_USER") or "specflow_agent"

    assert role(configured_database, name) == (
        True,
        False,
        False,
        False,
        False,
        20,
        ["idle_in_transaction_session_timeout=60s", "statement_timeout=30s"],
    )
