"""``FakeLinearGapsDb`` keeps every public function of ``db.linear_gaps`` with an equal signature."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from db import linear_gaps as module
from linear_gaps_support import PROJECT, PUBLIC_FUNCTIONS, FakeLinearGapsDb

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def public_functions() -> list[str]:
    return [
        name
        for name, member in inspect.getmembers(module, inspect.iscoroutinefunction)
        if member.__module__ == module.__name__ and not name.startswith("_")
    ]


def test_every_public_function_of_the_data_access_is_in_the_fake_with_an_equal_signature() -> None:
    assert sorted(PUBLIC_FUNCTIONS) == sorted(public_functions())
    for name in public_functions():
        assert inspect.signature(getattr(FakeLinearGapsDb(), name)) == inspect.signature(getattr(module, name)), name


def test_install_patches_the_module_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLinearGapsDb()
    fake.install(monkeypatch)

    for name in PUBLIC_FUNCTIONS:
        assert getattr(module, name) == getattr(fake, name)


async def test_a_destination_change_forgets_the_rows_and_the_parent() -> None:
    fake = FakeLinearGapsDb()
    kwargs = {"team_key": "ENG", "linear_project_id": None, "grouping": "one", "enabled": True, "now": NOW}
    await fake.save_target(None, PROJECT, team_id="t1", **kwargs)
    issue = PROJECT
    await fake.set_parent(None, PROJECT, issue, "ENG-1")
    await fake.add_issue(None, PROJECT, gap_id="G", issue_id=issue, identifier="ENG-2", area="G", number=1, now=NOW)

    saved = await fake.save_target(None, PROJECT, team_id="t2", **kwargs)

    target = await fake.get_target(None, PROJECT)
    assert saved.destination_changed and target is not None and target.parent_issue_id is None
    assert await fake.list_issues(None, PROJECT) == []
