"""Unit tests for .gitignore seeding during initialize_git_repo."""

from unittest.mock import AsyncMock, patch

import pytest

from app.database.memory import InMemoryDatabase
from app.services.workspace_pool import WorkspacePoolService
from app.state.db_adapter import COL_GENERATION_SESSIONS


@pytest.fixture
def db():
    database = InMemoryDatabase()
    yield database
    database.clear()


@pytest.fixture(autouse=True)
def _generation_session(db):
    db.set(
        COL_GENERATION_SESSIONS,
        "est-gitignore",
        {
            "generation_id": "est-gitignore",
            "workspace_pool": "default",
            "key_uid": "uid-gitignore",
        },
    )
    db.set(
        "api_keys",
        "gain_testkey_gitignore",
        {
            "api_key": "gain_testkey_gitignore",
            "key_uid": "uid-gitignore",
            "workspace_pool": "default",
            "user_id": "unit@test.com",
            "is_active": True,
        },
    )


@pytest.fixture
def pool(db, tmp_path):
    from app.core.github_platform_secrets import (
        init_github_platform_secrets_for_tests,
        reset_github_platform_secrets,
    )
    from cryptography.fernet import Fernet

    init_github_platform_secrets_for_tests(
        fernet_key=Fernet.generate_key(),
        github_token_default="unit-test-default-github-token",
        git_user_name_default="unit-test-git-user",
    )
    svc = WorkspacePoolService(db, workspace_base_path=str(tmp_path))
    yield svc
    reset_github_platform_secrets()


@pytest.mark.asyncio
async def test_initialize_git_repo_seeds_gitignore_before_commit(pool, tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "readme.txt").write_text("hello")

    with patch("app.services.workspace_pool.run_git", new_callable=AsyncMock):
        await pool.initialize_git_repo(ws, "est-gitignore")

    gitignore = ws / ".gitignore"
    assert gitignore.exists()
    assert "node_modules/" in gitignore.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_initialize_git_repo_continues_when_gitignore_seed_returns_false(pool, tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "readme.txt").write_text("hello")

    with patch(
        "app.services.workspace_pool.ensure_workspace_gitignore",
        return_value=False,
    ):
        with patch("app.services.workspace_pool.run_git", new_callable=AsyncMock) as mock_git:
            await pool.initialize_git_repo(ws, "est-gitignore")

    assert mock_git.called


@pytest.mark.asyncio
async def test_initialize_git_repo_invokes_gitignore_seeding(pool, tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()

    with patch(
        "app.services.workspace_pool.ensure_workspace_gitignore",
    ) as mock_ensure:
        with patch("app.services.workspace_pool.run_git", new_callable=AsyncMock):
            await pool.initialize_git_repo(ws, "est-gitignore")

    mock_ensure.assert_called_once_with(ws)
