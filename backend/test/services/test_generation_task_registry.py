"""
Tests for the process-local generation task registry.
"""
import asyncio

import pytest

from app.services.generation_task_registry import GenerationTaskRegistry


@pytest.mark.asyncio
async def test_register_and_get_current_task():
    reg = GenerationTaskRegistry()

    async def body():
        reg.register_current_task("est-1")
        assert reg.get_task("est-1") is asyncio.current_task()

    await asyncio.create_task(body())


@pytest.mark.asyncio
async def test_cancel_task_cancels_running_task():
    reg = GenerationTaskRegistry()
    started = asyncio.Event()

    async def body():
        reg.register_current_task("est-1")
        started.set()
        await asyncio.sleep(3600)  # would hang without cancellation

    task = asyncio.create_task(body())
    await started.wait()
    assert reg.cancel_task("est-1") is True
    with pytest.raises(asyncio.CancelledError):
        await task


def test_cancel_task_returns_false_when_absent():
    reg = GenerationTaskRegistry()
    assert reg.cancel_task("nope") is False


@pytest.mark.asyncio
async def test_deregister_only_removes_own_task():
    # A stale entry (a *different* task object) must not be clobbered by another
    # task's deregister — models a re-fired run that already re-registered itself.
    reg = GenerationTaskRegistry()
    sentinel = object()
    reg._tasks["est-1"] = sentinel  # type: ignore[assignment]

    async def body():
        reg.deregister_task("est-1")  # current task != sentinel → no-op

    await asyncio.create_task(body())
    assert reg.get_task("est-1") is sentinel


@pytest.mark.asyncio
async def test_deregister_removes_own_task():
    reg = GenerationTaskRegistry()

    async def body():
        reg.register_current_task("est-1")
        assert reg.get_task("est-1") is not None
        reg.deregister_task("est-1")
        assert reg.get_task("est-1") is None

    await asyncio.create_task(body())
