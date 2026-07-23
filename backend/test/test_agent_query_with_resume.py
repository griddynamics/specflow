"""agent_query_with_resume: the unified attempt loop.

Contract under test (single source: ResumeGeneration):
- crashes with saved progress keep retrying (incident regression — the old code
  died after 2 resume attempts no matter how much work each one saved);
- zero-progress crashes wait out the 1/3/5/10/30/60-minute schedule then give up;
- validator-driven resumes keep their old budget of 2;
- a dead validator retries instead of silently marking the phase complete;
- completion on the final allowed attempt emits NO give-up warning/event;
- crash/give-up/model-fallback events are recorded; RETRYING is set around waits;
- everything is silent (but functional) when no recorder is configured.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.core.telemetry_context import TelemetryContext
from app.schemas.agent import AgentQueryError, AgentQueryProgress, AgentResult
from app.schemas.agent_error_events import AgentErrorEventKind, WorkspaceAgentState
from app.services.claude_code import agent_query_with_resume

_SOCKET_ERR = "API Error: The socket connection was closed unexpectedly."
_COMPLETE = AgentResult(result="All subtasks done.\nWORK_COMPLETE", session_id="v1")
_INCOMPLETE = AgentResult(result="Remaining: wire the API route.", session_id="v1")
_CODING_OK = AgentResult(result="did some work", session_id="s1")


def _crash(tool_uses: int) -> AgentQueryError:
    return AgentQueryError(_SOCKET_ERR, progress=AgentQueryProgress(num_messages=tool_uses, num_tool_uses=tool_uses))


@pytest.fixture(autouse=True)
def _clean_context():
    TelemetryContext.clear_context()
    yield
    TelemetryContext.clear_context()


@pytest.fixture
def recorders():
    """Wire event/state handlers + identity so events are observable."""
    events = AsyncMock()
    states = AsyncMock()
    TelemetryContext.set_agent_error_event_handler(events)
    TelemetryContext.set_workspace_agent_state_handler(states)
    TelemetryContext.set_user_context(generation_id="est-1", workspace_name="ws-1")
    return events, states


def _recorded_kinds(events: AsyncMock) -> list[AgentErrorEventKind]:
    return [call.args[1].kind for call in events.await_args_list]


async def _run(**kwargs):
    defaults = dict(
        system_prompt="do phase work",
        workspace_path="/tmp/ws",
        outputs_dir="docs",
        spec_path="specs",
        model="m1",
        logger=Mock(),
        phase_number=12,
    )
    defaults.update(kwargs)
    return await agent_query_with_resume(**defaults)


def _patch_loop(agent_side_effects):
    """Patch agent_query (side-effect sequence), sleep, and the HEAD reader."""
    sleep = AsyncMock()
    agent_query = AsyncMock(side_effect=agent_side_effects)
    head = AsyncMock(return_value="same-sha")
    patches = [
        patch("app.services.claude_code.agent_query", agent_query),
        patch("app.services.claude_code.asyncio.sleep", sleep),
        patch("app.services.claude_code._read_workspace_head", head),
    ]
    return patches, agent_query, sleep, head


@pytest.mark.asyncio
async def test_incident_regression_progress_crashes_keep_retrying(recorders):
    """Four productive crashes (old cap: 2) then success — the phase completes."""
    events, states = recorders
    effects = []
    for _ in range(4):
        effects.append(_crash(tool_uses=50))  # coding query crashes after real work
    effects += [_CODING_OK, _COMPLETE]  # attempt 5 succeeds and validates complete
    patches, agent_query, sleep, _ = _patch_loop(effects)
    with patches[0], patches[1], patches[2]:
        result = await _run()

    assert result.result == "did some work"
    assert result.is_error is False
    assert agent_query.await_count == 6  # 5 coding + 1 validator
    # Every productive crash waits exactly the first (1-minute) step — schedule keeps resetting.
    assert [c.args[0] for c in sleep.await_args_list] == [60.0] * 4
    kinds = _recorded_kinds(events)
    assert kinds.count(AgentErrorEventKind.AGENT_CRASH) == 4
    assert AgentErrorEventKind.PHASE_GAVE_UP not in kinds
    assert AgentErrorEventKind.PHASE_INCOMPLETE not in kinds


@pytest.mark.asyncio
async def test_zero_progress_crashes_sleep_schedule_then_give_up(recorders):
    """Seven consecutive zero-progress crashes: six scheduled waits, then stop."""
    events, states = recorders
    patches, agent_query, sleep, _ = _patch_loop([_crash(tool_uses=0)] * 7)
    with patches[0], patches[1], patches[2]:
        result = await _run()

    assert result.is_error is True
    assert result.result == _SOCKET_ERR  # classifiable by execute_all_phases (#47)
    assert agent_query.await_count == 7  # all coding attempts, no validator ran
    assert [c.args[0] for c in sleep.await_args_list] == [60.0, 180.0, 300.0, 600.0, 1800.0, 3600.0]
    kinds = _recorded_kinds(events)
    assert kinds.count(AgentErrorEventKind.AGENT_CRASH) == 6  # exhausting crash isn't a retry
    assert kinds[-1] == AgentErrorEventKind.PHASE_GAVE_UP
    gave_up = events.await_args_list[-1].args[1]
    assert "zero-progress" in gave_up.message
    assert gave_up.crash_backoff == "6/6"


@pytest.mark.asyncio
async def test_progress_reset_then_re_exhaust(recorders):
    """A productive crash resets the streak; the follow-up outage still exhausts."""
    events, _ = recorders
    effects = [_crash(tool_uses=30)] + [_crash(tool_uses=0)] * 7
    patches, agent_query, sleep, _ = _patch_loop(effects)
    with patches[0], patches[1], patches[2]:
        result = await _run()

    assert result.is_error is True
    assert agent_query.await_count == 8
    assert [c.args[0] for c in sleep.await_args_list] == [60.0, 60.0, 180.0, 300.0, 600.0, 1800.0, 3600.0]


@pytest.mark.asyncio
async def test_validator_budget_unchanged_at_two_resumes(recorders):
    """Clean-but-incomplete attempts: initial + 2 resumes, then phase_incomplete."""
    events, _ = recorders
    patches, agent_query, sleep, _ = _patch_loop([_CODING_OK, _INCOMPLETE] * 3)
    with patches[0], patches[1], patches[2]:
        result = await _run()

    assert agent_query.await_count == 6  # 3 attempts x (coding + validator)
    assert result.result == "did some work"
    assert result.is_error is False  # checkpoint semantics preserved
    sleep.assert_not_awaited()  # validator-driven resumes retry immediately
    kinds = _recorded_kinds(events)
    assert kinds == [AgentErrorEventKind.PHASE_INCOMPLETE]
    assert "completion check is still unsatisfied" in events.await_args.args[1].message


@pytest.mark.asyncio
async def test_completion_on_final_allowed_attempt_emits_no_warning(recorders):
    """The old code logged 'Reached maximum resume attempts' even on success."""
    events, _ = recorders
    effects = [_CODING_OK, _INCOMPLETE, _CODING_OK, _INCOMPLETE, _CODING_OK, _COMPLETE]
    patches, agent_query, sleep, _ = _patch_loop(effects)
    with patches[0], patches[1], patches[2]:
        result = await _run()

    assert result.is_error is False
    assert _recorded_kinds(events) == []  # no give-up, no incomplete — nothing to report


@pytest.mark.asyncio
async def test_dead_validator_retries_instead_of_silently_passing(recorders):
    """Old behavior: validator None -> phase marked complete. New: crash-equivalent retry."""
    events, _ = recorders
    effects = [
        _CODING_OK, AgentResult(result=None, session_id=None),  # validator unusable
        _CODING_OK, _COMPLETE,                                   # retry verifies properly
    ]
    patches, agent_query, sleep, _ = _patch_loop(effects)
    with patches[0], patches[1], patches[2]:
        result = await _run()

    assert result.is_error is False
    assert agent_query.await_count == 4
    assert [c.args[0] for c in sleep.await_args_list] == [60.0]
    kinds = _recorded_kinds(events)
    assert kinds == [AgentErrorEventKind.AGENT_CRASH]
    assert "Completion check" in events.await_args_list[0].args[1].message


@pytest.mark.asyncio
async def test_validator_crash_counts_coding_progress_via_head(recorders):
    """A validator exception after a committing coding run resets the schedule."""
    events, _ = recorders
    effects = [
        _CODING_OK, _crash(tool_uses=0),  # validator crashed with no tool uses...
        _CODING_OK, _COMPLETE,
    ]
    patches, agent_query, sleep, head = _patch_loop(effects)
    head.side_effect = ["sha-before", "sha-after", "sha-after", "sha-after"]
    with patches[0], patches[1], patches[2]:
        result = await _run()

    assert result.is_error is False
    # ...but the coding query committed (HEAD advanced), so the crash kept the 1m wait.
    assert [c.args[0] for c in sleep.await_args_list] == [60.0]
    crash_event = events.await_args_list[0].args[1]
    assert "saved work" in crash_event.message


@pytest.mark.asyncio
async def test_model_routing_crash_triggers_fallback_event(recorders):
    events, _ = recorders
    routing_crash = AgentQueryError(
        "API returned an empty response", progress=AgentQueryProgress(0, 0)
    )
    effects = [routing_crash, _CODING_OK, _COMPLETE]
    patches, agent_query, sleep, _ = _patch_loop(effects)
    with patches[0], patches[1], patches[2], patch(
        "app.services.claude_code.apply_model_fallback_if_routing_failure",
        Mock(return_value=("fallback-model", None)),
    ):
        result = await _run()

    assert result.is_error is False
    assert result.active_model == "fallback-model"
    kinds = _recorded_kinds(events)
    assert kinds[0] == AgentErrorEventKind.MODEL_FALLBACK
    fallback = events.await_args_list[0].args[1]
    assert fallback.previous_model == "m1"
    assert fallback.model == "fallback-model"


@pytest.mark.asyncio
async def test_tool_call_failure_stops_immediately(recorders):
    events, _ = recorders
    effects = [AgentQueryError("model does not support tools", progress=AgentQueryProgress(0, 0))]
    patches, agent_query, sleep, _ = _patch_loop(effects)
    with patches[0], patches[1], patches[2]:
        result = await _run()

    assert result.is_error is True
    assert agent_query.await_count == 1
    sleep.assert_not_awaited()
    kinds = _recorded_kinds(events)
    assert kinds == [AgentErrorEventKind.PHASE_GAVE_UP]
    assert "tool failure" in events.await_args.args[1].message


@pytest.mark.asyncio
async def test_retrying_badge_set_and_cleared_around_waits(recorders):
    _, states = recorders
    effects = [_crash(tool_uses=0), _CODING_OK, _COMPLETE]
    patches, agent_query, sleep, _ = _patch_loop(effects)
    with patches[0], patches[1], patches[2]:
        await _run()

    badge_calls = [call.args[2] for call in states.await_args_list]
    assert badge_calls == [WorkspaceAgentState.RETRYING, None]


@pytest.mark.asyncio
async def test_silent_without_recorder_configured():
    """No handlers, no identity: the loop still works and raises nothing."""
    effects = [_crash(tool_uses=0), _CODING_OK, _COMPLETE]
    patches, agent_query, sleep, _ = _patch_loop(effects)
    with patches[0], patches[1], patches[2]:
        result = await _run()
    assert result.is_error is False


@pytest.mark.asyncio
async def test_model_routing_give_up_recommends_model_change(recorders):
    """Part F: a model-caused give-up names the remedy (Settings -> retry)."""
    events, _ = recorders
    routing_crash = AgentQueryError(
        "API returned an empty response", progress=AgentQueryProgress(0, 0)
    )
    patches, agent_query, sleep, _ = _patch_loop([routing_crash] * 7)
    with patches[0], patches[1], patches[2], patch(
        "app.services.claude_code.apply_model_fallback_if_routing_failure",
        Mock(side_effect=lambda err, active, cand, log, label: (active, cand)),
    ):
        result = await _run()

    assert result.is_error is True
    gave_up = events.await_args_list[-1].args[1]
    assert gave_up.kind == AgentErrorEventKind.PHASE_GAVE_UP
    assert "change the model in Settings" in gave_up.message


@pytest.mark.asyncio
async def test_connection_give_up_does_not_recommend_model_change(recorders):
    events, _ = recorders
    patches, agent_query, sleep, _ = _patch_loop([_crash(tool_uses=0)] * 7)
    with patches[0], patches[1], patches[2]:
        await _run()

    gave_up = events.await_args_list[-1].args[1]
    assert gave_up.kind == AgentErrorEventKind.PHASE_GAVE_UP
    assert "change the model" not in gave_up.message
    assert "code is preserved" in gave_up.message
