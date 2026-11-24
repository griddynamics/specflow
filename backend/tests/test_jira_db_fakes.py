"""``FakeJiraDb`` keeps every public function of ``db.jira`` with an equal signature; the hook and the transaction."""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime

import pytest

from db import jira as jira_module
from jira_db_fakes import PUBLIC_FUNCTIONS, FakeJiraDb

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PROJECT = uuid.uuid4()


def public_functions() -> list[str]:
    return [
        name
        for name, member in inspect.getmembers(jira_module, inspect.iscoroutinefunction)
        if member.__module__ == jira_module.__name__ and not name.startswith("_")
    ]


def test_every_public_function_of_the_repository_is_in_the_fake_with_an_equal_signature() -> None:
    assert sorted(PUBLIC_FUNCTIONS) == sorted(public_functions())
    for name in public_functions():
        assert inspect.signature(getattr(FakeJiraDb(), name)) == inspect.signature(getattr(jira_module, name)), name


def test_install_patches_the_module_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeJiraDb()
    fake.install(monkeypatch)

    for name in PUBLIC_FUNCTIONS:
        assert getattr(jira_module, name) == getattr(fake, name)


async def test_a_jira_project_change_deletes_the_map_and_a_pending_row_takes_the_new_request_time() -> None:
    fake = FakeJiraDb()
    saved = await fake.save_target(None, PROJECT, jira_project="AAA", epic_key=None, enabled=True, now=NOW)
    assert (saved.project_changed, saved.turned_on, saved.keys_changed) == (False, True, True)
    await fake.add_pending(None, PROJECT, node_id="STORY::a::b", kind="story", requested_at=NOW)
    later = datetime(2026, 1, 2, tzinfo=UTC)
    await fake.add_pending(None, PROJECT, node_id="STORY::a::b", kind="story", requested_at=later)
    await fake.update_issue(None, PROJECT, "STORY::a::b", jira_key="AAA-1")
    await fake.add_pending(None, PROJECT, node_id="STORY::a::b", kind="story", requested_at=NOW)
    [row] = await fake.list_issues(None, PROJECT)
    assert (row.requested_at, row.jira_key) == (later, "AAA-1")

    saved = await fake.save_target(None, PROJECT, jira_project="BBB", epic_key=None, enabled=True, now=NOW)

    assert (saved.project_changed, saved.turned_on, saved.keys_changed) == (True, False, True)
    assert await fake.list_issues(None, PROJECT) == []


async def test_the_hook_changes_the_target_between_calls_and_the_transaction_is_recorded() -> None:
    fake = FakeJiraDb()
    await fake.save_target(None, PROJECT, jira_project="AAA", epic_key=None, enabled=True, now=NOW)
    fake.hook = lambda name: fake.set_target(PROJECT, jira_project="BBB") if name == "update_issue" else None
    assert await fake.target_unchanged(None, PROJECT, jira_project="AAA", epic_key=None)

    async with fake.transaction():
        assert fake.in_transaction
        await fake.update_issue(None, PROJECT, "EPIC", jira_key="AAA-1")

    assert not fake.in_transaction
    assert not await fake.target_unchanged(None, PROJECT, jira_project="AAA", epic_key=None)
