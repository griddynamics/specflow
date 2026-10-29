"""Worker registration of the ``agents`` queue (``temporal.registry`` contract).

One app-role session factory and one agent-role session factory (RLS) per
process, both lazy (no connection until the first activity); ``close``
releases them. The model activities are added by the plugin. The queue opens
no Neo4j driver: no summarize activity reads or writes the graph.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from db.agent_scope import (
    build_agent_session_factory,
    dispose_agent_session_factory,
)
from db.session import build_engine, build_session_factory
from settings import RoleSettings
from temporal.agents.activities import SummarizeActivities
from temporal.agents.workflows import SummarizeDocumentWorkflow
from temporal.registry import Registration


def build_registration(app_engine: AsyncEngine, agent_sessions: Any) -> Registration:
    """The registration over ready parts (tests pass their own)."""

    activities = SummarizeActivities(build_session_factory(app_engine), agent_sessions)

    async def close() -> None:
        try:
            await dispose_agent_session_factory(agent_sessions)
        finally:
            await app_engine.dispose()

    return Registration(
        workflows=[SummarizeDocumentWorkflow],
        activities=[
            activities.read_document_chunk,
            activities.save_summarization,
            activities.mark_summarization_failed,
        ],
        close=close,
    )


def register(settings: RoleSettings) -> Registration:
    """Lazy engines, built once per process from the worker settings."""

    if settings.agent_database is None:
        raise ValueError(f"Role {settings.role!r} has no agent database settings")
    return build_registration(
        build_engine(settings.database),
        build_agent_session_factory(settings.database, settings.agent_database),
    )
