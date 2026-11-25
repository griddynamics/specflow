"""``FakeJiraGapsDb`` keeps every public function of ``db.jira_gaps`` with an equal signature."""

from __future__ import annotations

import inspect

import pytest

from db import jira_gaps as module
from jira_gaps_support import PROJECT, PUBLIC_FUNCTIONS, START, FakeJiraGapsDb


def public_functions() -> list[str]:
    return [
        name
        for name, member in inspect.getmembers(module, inspect.iscoroutinefunction)
        if member.__module__ == module.__name__ and not name.startswith("_")
    ]


def test_every_public_function_of_the_data_access_is_in_the_fake_with_an_equal_signature() -> None:
    assert sorted(PUBLIC_FUNCTIONS) == sorted(public_functions())
    for name in public_functions():
        assert inspect.signature(getattr(FakeJiraGapsDb(), name)) == inspect.signature(getattr(module, name)), name


def test_install_patches_the_module_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeJiraGapsDb()
    fake.install(monkeypatch)

    for name in PUBLIC_FUNCTIONS:
        assert getattr(module, name) == getattr(fake, name)


async def test_a_project_change_forgets_the_rows_the_story_and_the_cursor() -> None:
    fake = FakeJiraGapsDb()
    await fake.save_target(None, PROJECT, jira_project="AAA", grouping="one", enabled=True, now=START)
    await fake.set_story(None, PROJECT, "AAA-1")
    await fake.add_issue(None, PROJECT, gap_id="G", issue_key="AAA-2", area="G", number=1, now=START)

    saved = await fake.save_target(None, PROJECT, jira_project="BBB", grouping="area", enabled=True, now=START)

    target = await fake.get_target(None, PROJECT)
    assert saved.project_changed and target is not None and target.story_key is None
    assert await fake.list_issues(None, PROJECT) == []
