"""
Shared fixtures and utilities for API router tests.

Provides reusable mocks and fixtures for testing FastAPI routers with dependencies.
"""

import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Patch logging configuration BEFORE any app imports
# This prevents filesystem issues during test collection
import app.core.logging as logging_module
original_configure = logging_module.configure_logging
logging_module.configure_logging = Mock()

from app.database.factory import clear_test_data  # noqa: E402
from app.state.api_key_session_concurrency import (  # noqa: E402
    SessionAtCapacityError,
    SessionBeginOutcome,
    SessionEndReason,
)


@pytest.fixture
def test_app():
    """Create a minimal FastAPI app for testing."""
    # Clear any existing test data
    clear_test_data()
    
    # Ensure we're using in-memory database for unit tests
    if "DATABASE_TYPE" not in os.environ:
        os.environ["DATABASE_TYPE"] = "memory"
    
    # Create minimal app
    app = FastAPI()
    
    yield app
    
    # Cleanup after test
    clear_test_data()


@pytest.fixture
def client(test_app):
    """Create test client for FastAPI app."""
    return TestClient(test_app)


@pytest.fixture
def temp_workspace_dir():
    """Create a temporary directory for workspace operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_workspace_pool(temp_workspace_dir):
    """Create a mock WorkspacePoolService."""
    mock_pool = Mock()
    mock_pool.workspace_base_path = temp_workspace_dir
    
    # Default mock behavior
    async def allocate_workspace_set(*args, **kwargs):
        return ["ws-01-1", "ws-01-2", "ws-01-3"]

    mock_pool.allocate_workspace_set = AsyncMock(side_effect=allocate_workspace_set)

    return mock_pool


@pytest.fixture
def mock_generation_session_service():
    """Create a mock GenerationSessionService."""
    mock_service = Mock()

    async def _try_begin(*, api_key_doc_id, generation_id, operation, **kwargs):
        snap = SimpleNamespace(
            active=(),
            max_concurrent_sessions=5,
            at_capacity=False,
            api_key_doc_id=api_key_doc_id,
        )
        sess = SimpleNamespace(
            generation_id=generation_id,
            operation=operation,
        )
        return SimpleNamespace(
            outcome=SessionBeginOutcome.ACQUIRED,
            session=sess,
            snapshot=snap,
        )

    sessions = MagicMock()
    sessions.try_begin = AsyncMock(side_effect=_try_begin)
    sessions.end = AsyncMock()
    sessions.refresh_lease = AsyncMock()

    @asynccontextmanager
    async def _begin_or_raise(*, api_key_doc_id, generation_id, operation, **kwargs):
        result = await sessions.try_begin(
            api_key_doc_id=api_key_doc_id,
            generation_id=generation_id,
            operation=operation,
        )
        if result.outcome == SessionBeginOutcome.AT_CAPACITY:
            raise SessionAtCapacityError()
        if result.outcome == SessionBeginOutcome.ALREADY_HELD:
            await sessions.refresh_lease(generation_id=generation_id, operation=operation)
        acquired = result.outcome == SessionBeginOutcome.ACQUIRED
        try:
            yield result.session
        except Exception:
            if acquired:
                await sessions.end(generation_id=generation_id, reason=SessionEndReason.BEGIN_ROLLBACK)
            raise

    @asynccontextmanager
    async def _task_slot(*, generation_id, **kwargs):
        try:
            yield
        except Exception:
            await sessions.end(generation_id=generation_id, reason=SessionEndReason.FAILED)
            raise
        else:
            await sessions.end(generation_id=generation_id, reason=SessionEndReason.COMPLETED)

    sessions.begin_or_raise = _begin_or_raise
    sessions.task_slot = _task_slot
    mock_service.api_key_sessions = sessions

    esm = MagicMock()
    mock_service.generation_session_sm = esm

    mock_service.merge_generation_session_parameters = Mock()
    mock_service.add_agent_query_token_usage = AsyncMock()
    mock_service.fail_generation_session = AsyncMock()
    mock_service.db_adapter = MagicMock()

    async def create_generation_session(*args, **kwargs):
        return "est-test-123"

    async def update_checkpoint(*args, **kwargs):
        pass

    mock_service.create_generation_session = AsyncMock(side_effect=create_generation_session)
    mock_service.update_checkpoint = AsyncMock(side_effect=update_checkpoint)

    return mock_service


@pytest.fixture
def mock_request_state():
    """Create a mock request state with user_email."""
    state = Mock()
    state.user_email = "test@example.com"
    return state


@pytest.fixture
def mock_spec_analysis_result():
    """Create a mock result from spec_analysis_workflow."""
    result = Mock()
    result.model_dump = Mock(return_value={
        "completeness_score": 0.85,
        "missing_sections": [],
        "recommendations": ["Add more detail to API section"]
    })
    return result


@pytest.fixture
def sample_tar_archive():
    """Create a sample tar.gz archive in memory."""
    import io
    import tarfile
    
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as tar:
        # Add a sample file
        file_data = b"sample specification content"
        tarinfo = tarfile.TarInfo(name="spec.md")
        tarinfo.size = len(file_data)
        tar.addfile(tarinfo, io.BytesIO(file_data))
    
    archive_buffer.seek(0)
    return archive_buffer.read()


def create_mock_upload_file(content: bytes, filename: str = "archive.tar.gz"):
    """Create a mock UploadFile for testing."""
    from fastapi import UploadFile
    from io import BytesIO
    
    file_obj = BytesIO(content)
    upload_file = UploadFile(file=file_obj, filename=filename)
    return upload_file
