"""``save_target`` against a session that keeps an identity map: the locking SELECT must refresh a
``JiraTarget`` the session loaded earlier, or ``jira_project`` changes made meanwhile go unseen. Offline; the live
twin is in test_db_jira_live."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy.sql import Delete

from db import jira

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def target(jira_project: str) -> Any:
    return SimpleNamespace(jira_project=jira_project, epic_key=None, enabled=True, updated_at=NOW)


class IdentityMapSession:
    """What SQLAlchemy does with an entity already in the session: a SELECT returns the cached object, and only
    ``populate_existing`` makes it show the row as the SELECT found it."""

    def __init__(self, cached: Any, row: Any) -> None:
        self.cached, self.row, self.statements = cached, row, []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        refreshed = statement.get_execution_options().get("populate_existing", False)
        return SimpleNamespace(scalar_one_or_none=lambda: self.row if refreshed else self.cached)

    async def flush(self) -> None:
        return None


async def test_save_target_sees_a_jira_project_changed_after_the_session_loaded_the_target():
    # Scenario: tab 2 loaded the target (project A); tab 1 committed A to B; tab 2 now saves A. The stored project
    # is B, so this is a change and the map rows of B must go.
    session: Any = IdentityMapSession(cached=target("A"), row=target("B"))
    saved = await jira.save_target(session, uuid.uuid4(), jira_project="A", epic_key=None, enabled=True, now=NOW)
    assert saved.project_changed is True
    assert any(isinstance(statement, Delete) for statement in session.statements)
