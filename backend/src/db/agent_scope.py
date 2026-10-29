"""Sessions as the row-level-security agent role, scoped to one project.

This module is the only way code obtains an agent session: agent tool
activities call ``agent_scope(factory, project_id)``; nothing else uses the
factory directly (a static test enforces this).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from db.session import build_engine, build_session_factory
from settings import AgentDatabaseSettings, DatabaseSettings

SET_PROJECT_SCOPE = text("SELECT set_config('app.project_id', :pid, true)")


def build_agent_session_factory(
    db: DatabaseSettings,
    agent: AgentDatabaseSettings,
) -> async_sessionmaker[AsyncSession]:
    """A session factory on a lazy engine that logs in as the agent role.

    Host, port and database come from ``db``; user and password from
    ``agent``. Release it with ``dispose_agent_session_factory``.
    """

    return build_session_factory(build_engine(agent.as_database_settings(db)))


async def dispose_agent_session_factory(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Close the pooled connections of a factory from ``build_agent_session_factory``."""

    engine = factory.kw.get("bind")
    if isinstance(engine, AsyncEngine):
        await engine.dispose()


@asynccontextmanager
async def agent_scope(
    factory: async_sessionmaker[AsyncSession],
    project_id: uuid.UUID,
) -> AsyncIterator[AsyncSession]:
    """One transaction whose first statement scopes RLS to ``project_id``.

    ``set_config(..., true)`` is transaction-local, so a pooled connection
    never carries the scope to its next user. The transaction commits when the
    block exits normally and rolls back on an exception.
    """

    if not isinstance(project_id, uuid.UUID):
        raise TypeError("project_id must be a uuid.UUID")
    async with factory() as session, session.begin():
        await session.execute(SET_PROJECT_SCOPE, {"pid": str(project_id)})
        yield session
