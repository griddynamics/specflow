"""
Tests for the cooperative-cancellation helper (app.state.cancellation).
"""
import pytest

from app.schemas.generation_workflow_enums import GenerationStatus
from app.state.cancellation import raise_if_cancelled, reset_cancellation_check
from app.state.exceptions import GenerationCancelledError


class FakeAdapter:
    """Minimal async DB adapter exposing get_generation_session + a call counter."""
    def __init__(self, status=None):
        self.status = status
        self.calls = 0

    async def get_generation_session(self, generation_id: str) -> dict:
        self.calls += 1
        return {"status": self.status} if self.status is not None else {}


@pytest.fixture(autouse=True)
def _reset():
    reset_cancellation_check("est-1")
    yield
    reset_cancellation_check("est-1")


@pytest.mark.asyncio
async def test_raises_when_cancelled():
    adapter = FakeAdapter(status=GenerationStatus.CANCELLED.value)
    with pytest.raises(GenerationCancelledError):
        await raise_if_cancelled(adapter, "est-1", min_interval_s=0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    GenerationStatus.RUNNING.value,
    GenerationStatus.PENDING.value,
    GenerationStatus.COMPLETED.value,
])
async def test_noop_when_not_cancelled(status):
    adapter = FakeAdapter(status=status)
    await raise_if_cancelled(adapter, "est-1", min_interval_s=0.0)  # does not raise


@pytest.mark.asyncio
async def test_noop_when_missing_doc():
    adapter = FakeAdapter(status=None)
    await raise_if_cancelled(adapter, "est-1", min_interval_s=0.0)


@pytest.mark.asyncio
async def test_noop_when_db_adapter_is_none():
    # No DB wired (DB-less unit-test path) — must be a silent no-op, never raise.
    await raise_if_cancelled(None, "est-1", min_interval_s=0.0)


@pytest.mark.asyncio
async def test_throttles_repeated_reads():
    adapter = FakeAdapter(status=GenerationStatus.RUNNING.value)
    # First call hits the DB; the immediate second call is throttled (no DB read).
    await raise_if_cancelled(adapter, "est-1", min_interval_s=1000.0)
    await raise_if_cancelled(adapter, "est-1", min_interval_s=1000.0)
    assert adapter.calls == 1
