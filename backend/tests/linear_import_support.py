"""Shared fakes of the Linear source tests (no tests in here): the member connections the importer reads through (one
gateway over a ``FakeLinear``), a seeded workspace (a project of five issues with comments, sub-issues, a label, an
assignee and two documents); the recording writer is ``atlassian_import_support.RecordingWriter``. Every title, text
and id is made up."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from linear.fake import FakeLinear
from linear.gateway import LinearGateway
from linear.models import Issue, NewIssue, Project, Team

ACTING = uuid.UUID(int=77)


class FakeLinearConnections:
    """``runtime.linear``: one member's connection (``ACTING`` by default) and the gateway acting as them."""

    def __init__(self, fake: FakeLinear, *, status: str = "active", user_id: uuid.UUID = ACTING) -> None:
        self.fake = fake
        self.status = status
        self.user_id = user_id
        self.opened: list[uuid.UUID] = []

    async def get(self, user_id: uuid.UUID) -> SimpleNamespace | None:
        if user_id != self.user_id:
            return None
        return SimpleNamespace(
            status=self.status,
            linear_user_id=self.fake.user_id,
            organization_id=self.fake.organization.id,
            organization_name=self.fake.organization.name,
        )

    @asynccontextmanager
    async def gateway(self, user_id: uuid.UUID) -> AsyncIterator[LinearGateway]:
        self.opened.append(user_id)
        yield self.fake


@dataclass
class Workspace:
    """What ``seed_workspace`` made: the fake, its team and project and the issues in creation order."""

    fake: FakeLinear
    team: Team
    project: Project
    issues: list[Issue]


async def make_issue(fake: FakeLinear, team: Team, title: str, **fields: Any) -> Issue:
    return await fake.create_issue(NewIssue(str(uuid.uuid4()), team.id, title, **fields))


async def seed_workspace(*, page_size: int = 2) -> Workspace:
    """Project ``Checkout`` of team ``GRA``: GRA-1..GRA-5 (GRA-3 and GRA-4 are sub-issues of GRA-1; GRA-5 of GRA-3, a
    second level for an issue source), comments on GRA-1 and GRA-2, two documents."""

    fake = FakeLinear(page_size=page_size, user_name="Ada Lovelace")
    team = fake.add_team("GRA")
    project = fake.add_project(team.id, "Checkout")
    label = await fake.create_label(team.id, str(uuid.uuid4()), "payments")
    first = await make_issue(
        fake, team, "Card flow", project_id=project.id, description="Pay by card.", label_ids=(label.id,), priority=2
    )
    second = await make_issue(fake, team, "Wallet flow", project_id=project.id, estimate=3)
    third = await make_issue(fake, team, "Card retries", project_id=project.id, parent_id=first.id)
    fourth = await make_issue(fake, team, "Card refunds", project_id=project.id, parent_id=first.id)
    fifth = await make_issue(fake, team, "Retry backoff", project_id=project.id, parent_id=third.id)
    fake.assign_issue(first.id, "Grace Hopper")
    await fake.create_comment(first.id, "Looks good")
    fake.add_comment_as(second.id, fake.add_user("Alan Turing"), "Needs a design")
    fake.add_document(project.id, "Payment glossary", "Authorisation, capture and refund.")
    fake.add_document(project.id, "Rollout notes", "Ship behind a flag.")
    fake.writes.clear()
    fake.calls.clear()
    return Workspace(fake, team, project, [first, second, third, fourth, fifth])
