"""``FakeIntegrationsDb`` keeps every public function of ``db.integrations`` with an equal signature."""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime

import pytest

from db import integrations as integrations_module
from db.integrations import BindingHeldError, WorkspaceMismatchError
from integrations_db_fakes import PUBLIC_FUNCTIONS, FakeIntegrationsDb

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PROJECT, ANA, BO = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def public_functions() -> list[str]:
    return [
        name
        for name, member in inspect.getmembers(integrations_module, inspect.iscoroutinefunction)
        if member.__module__ == integrations_module.__name__ and not name.startswith("_")
    ]


def test_every_public_function_of_the_data_access_is_in_the_fake_with_an_equal_signature() -> None:
    assert sorted(PUBLIC_FUNCTIONS) == sorted(public_functions())
    for name in public_functions():
        assert inspect.signature(getattr(FakeIntegrationsDb(), name)) == inspect.signature(
            getattr(integrations_module, name)
        ), name


def test_install_patches_the_module_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeIntegrationsDb()
    fake.install(monkeypatch)

    for name in PUBLIC_FUNCTIONS:
        assert getattr(integrations_module, name) == getattr(fake, name)


async def test_a_binding_moves_to_another_member_of_its_workspace_but_not_to_another_workspace() -> None:
    fake = FakeIntegrationsDb()
    await fake.bind_integration(None, PROJECT, "github", user_id=ANA, workspace_id="a", workspace_name="A", now=NOW)

    with pytest.raises(BindingHeldError):  # Ana's connection is still active: Bo cannot take it over
        await fake.bind_integration(None, PROJECT, "github", user_id=BO, workspace_id="a", workspace_name="A", now=NOW)
    fake.inactive.add(ANA)
    await fake.bind_integration(None, PROJECT, "github", user_id=BO, workspace_id="a", workspace_name="A", now=NOW)
    with pytest.raises(WorkspaceMismatchError):
        await fake.bind_integration(None, PROJECT, "github", user_id=ANA, workspace_id="b", workspace_name="B", now=NOW)
    fake.release(PROJECT, BO)  # the member left: nothing acts, so any workspace may take over
    await fake.bind_integration(None, PROJECT, "github", user_id=ANA, workspace_id="b", workspace_name="B", now=NOW)

    assert (await fake.get_integration(None, PROJECT, "github")).workspace_id == "b"
