"""Integration test: drive the REAL FastAPI lifespan end-to-end for the
graceful-shutdown + boot-recovery feature.

This exercises the actual wiring in ``app.main.lifespan`` against a real
``InMemoryDatabase`` (via the real ``StateMachineDBAdapter``), the real state
machine, and the real in-process registry. Only the heavy/external startup pieces
(startup validation, background job loops, github secrets, tracer) and the workflow
re-fire are stubbed — everything this feature actually does runs for real:

  Half A (shutdown):  a registered in-flight session is marked FAILED + Slack-notified.
  Half B (boot):      on the next startup the FAILED+marker session is auto-retried.

Determinism: the heavy startup machinery is neutralised so a lifespan enter/exit is a
few milliseconds; the boot-recovery task is awaited via a short poll helper.
"""
import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Importing app.main is safe here: the conftest chain (integration → jobs.conftest)
# has already replaced app.core.logging.configure_logging with a Mock.
from app import main as app_main
from app.core import app_lifecycle
from app.database.factory import get_database, reset_database
from app.schemas.generation_workflow_enums import GenerationStatus
from app.services import shutdown_handler
from app.jobs import shutdown_interrupted_recovery as recovery_job

GEN_ID = "gen-lifespan-itest"
COL = "generation_sessions"


async def _wait_until(predicate, *, attempts: int = 100, interval: float = 0.02) -> bool:
    """Poll until predicate() is truthy (lets the boot-recovery task run)."""
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


@pytest.fixture
def memory_db(monkeypatch):
    """Fresh real InMemoryDatabase shared by the test and the lifespan singleton."""
    monkeypatch.setattr(app_main.settings, "DATABASE_TYPE", "memory")
    reset_database()
    db = get_database()  # InMemoryDatabase — same singleton the lifespan will get()
    yield db
    reset_database()


@pytest.fixture
def neutralise_lifespan():
    """Stub the heavy/external startup pieces + the workflow re-fire, yielding the
    handles the test asserts on (slack notify, rerun)."""
    with ExitStack() as stack:
        # lifespan + these helpers now live in app.core.app_lifecycle.
        stack.enter_context(patch.object(app_lifecycle, "init_github_platform_secrets", MagicMock()))
        stack.enter_context(patch.object(app_lifecycle, "run_startup_validation", AsyncMock()))
        stack.enter_context(patch.object(app_lifecycle, "run_job_loop", AsyncMock()))
        stack.enter_context(patch.object(app_lifecycle, "_cleanup_queue_logging", MagicMock()))
        mock_tracer = MagicMock()
        mock_tracer.flush_async = AsyncMock()
        stack.enter_context(patch.object(app_lifecycle, "tracer", mock_tracer))

        # The re-fire must not run a real generation. AsyncMock(...) returns a coroutine
        # so the recovery job's asyncio.create_task(rerun(...)) is a harmless no-op.
        rerun = AsyncMock(name="rerun_generation_session")
        stack.enter_context(patch.object(recovery_job, "rerun_generation_session", rerun))

        slack = stack.enter_context(patch.object(shutdown_handler.notifications, "notify"))
        yield MagicMock(slack=slack, rerun=rerun)


def _seed_running_session(db):
    db.set(COL, GEN_ID, {
        "status": GenerationStatus.RUNNING,
        "user_email": "u@example.io",
        "parameters": {"spec_path": "specs/", "outputs_dir": "docs"},
        "workspace_ids": [],          # empty → HR-2 reuse check skipped on recovery
        "retry_count": 0,
        "max_retries": 3,
        "code_archived": False,
        "state_history": [],
    })


@pytest.mark.asyncio
async def test_shutdown_then_boot_recovery_via_real_lifespan(memory_db, neutralise_lifespan):
    db = memory_db
    mocks = neutralise_lifespan

    # A RUNNING session in Firestore — the shutdown handler finds it by query.
    _seed_running_session(db)

    # ---- Half A: a graceful shutdown of the running pod ----
    async with app_main.lifespan(app_main.app):
        # Let this boot's one-shot recovery task run to completion while the session
        # is still RUNNING — it must find nothing (status != FAILED), so it cannot
        # interfere with the shutdown that follows.
        await asyncio.sleep(0.05)
        assert db.get(COL, GEN_ID)["status"] == GenerationStatus.RUNNING
        assert mocks.rerun.call_count == 0
    # Exiting the context ran the shutdown block.

    doc = db.get(COL, GEN_ID)
    assert doc["status"] == GenerationStatus.FAILED
    assert doc["shutdown_interrupted"] is True
    assert doc["failed_at"] is not None
    # one Slack message, naming the interrupted session
    mocks.slack.assert_called_once()
    assert GEN_ID in mocks.slack.call_args.args[0]

    # ---- Half B: the pod reboots ----
    mocks.rerun.reset_mock()

    async with app_main.lifespan(app_main.app):
        # The one-shot boot-recovery task runs during startup — wait for it to land.
        recovered = await _wait_until(
            lambda: db.get(COL, GEN_ID)["status"] == GenerationStatus.PENDING
        )
        assert recovered, "boot recovery did not reset the interrupted session to PENDING"

    doc = db.get(COL, GEN_ID)
    assert doc["status"] == GenerationStatus.PENDING          # FAILED → PENDING
    assert doc["shutdown_interrupted"] is False               # marker cleared
    assert doc["retry_count"] == 1                            # one retry consumed
    # re-fired through the shared runner with the right id
    mocks.rerun.assert_called_once()
    assert mocks.rerun.call_args.args[0] == GEN_ID
    # state history records the boot-recovery trigger
    assert doc["state_history"][-1]["triggered_by"] == "system:server_boot_recovery"

    # let the (mocked) re-fire task settle so no pending-task warnings leak
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_genuine_failure_not_recovered_on_boot(memory_db, neutralise_lifespan):
    """A session FAILED without the shutdown marker must be left alone on boot."""
    db = memory_db
    mocks = neutralise_lifespan
    db.set(COL, GEN_ID, {
        "status": GenerationStatus.FAILED,
        "user_email": "u@example.io",
        "error": "genuine bug",
        "failed_at": None,
        "state_history": [],
    })

    async with app_main.lifespan(app_main.app):
        await asyncio.sleep(0.1)  # give the recovery task a chance to (not) act

    doc = db.get(COL, GEN_ID)
    assert doc["status"] == GenerationStatus.FAILED   # untouched
    mocks.rerun.assert_not_called()
