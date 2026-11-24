"""Worker registration of the ``agents`` queue (``temporal.registry`` contract).

One app-role session factory and one agent-role session factory (RLS) per
process, both lazy (no connection until the first activity); ``close``
releases them. The model activities are added by the plugin. The queue opens
no Neo4j driver: no summarize activity reads or writes the graph.

The GitHub clone check (``GithubSnapshotWorkflow``, docs/github/clone.md) runs
here too, with one object store and one ``GitHubConnections`` per process
(none when GitHub is not configured: every check then ends
``github_auth_failed``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from db.agent_scope import (
    build_agent_session_factory,
    dispose_agent_session_factory,
)
from db.github import connection_store, session_transaction
from db.session import build_engine, build_session_factory
from github.connections import GitHubConnections
from settings import GitHubSettings, RoleSettings
from storage import ObjectStore
from storage import factory as storage_factory
from temporal.agents.activities import SummarizeActivities
from temporal.agents.workflows import SummarizeDocumentWorkflow
from temporal.github.activities import GitHubSnapshotActivities, SessionSnapshotRows
from temporal.github.workflows import GithubSnapshotWorkflow
from temporal.registry import Registration


def build_registration(
    app_engine: AsyncEngine,
    agent_sessions: Any,
    *,
    store: ObjectStore | None = None,
    github: GitHubSettings | None = None,
) -> Registration:
    """The registration over ready parts (tests pass their own). Without ``store`` the queue runs no clone check."""

    app_sessions = build_session_factory(app_engine)
    activities = SummarizeActivities(app_sessions, agent_sessions)
    workflows: list[type] = [SummarizeDocumentWorkflow]
    registered: list[Any] = [
        activities.read_document_chunk,
        activities.save_summarization,
        activities.mark_summarization_failed,
    ]
    connections: GitHubConnections | None = None
    if store is not None:
        if github is not None:
            connections = GitHubConnections(github, store=connection_store(session_transaction(app_sessions)))
        snapshots = GitHubSnapshotActivities(SessionSnapshotRows(app_sessions), store, connections)
        workflows.append(GithubSnapshotWorkflow)
        registered.extend(snapshots.activities)

    async def close() -> None:
        try:
            if connections is not None:
                await connections.aclose()
        finally:
            try:
                await dispose_agent_session_factory(agent_sessions)
            finally:
                await app_engine.dispose()
                store_close = getattr(store, "close", None)
                if store_close is not None:
                    await store_close()

    return Registration(workflows=workflows, activities=registered, close=close)


def register(settings: RoleSettings) -> Registration:
    """Lazy engines, built once per process from the worker settings."""

    if settings.agent_database is None:
        raise ValueError(f"Role {settings.role!r} has no agent database settings")
    if settings.storage is None:
        raise ValueError(f"Role {settings.role!r} has no storage settings")
    return build_registration(
        build_engine(settings.database),
        build_agent_session_factory(settings.database, settings.agent_database),
        store=storage_factory.build_object_store(settings.storage, app_env=settings.app.app_env),
        github=settings.github,
    )
