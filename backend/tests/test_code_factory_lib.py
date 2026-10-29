from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import llm.code_factory_lib as code_factory
from llm.code_factory_lib import (
    PhaseDAG,
    build_coding_dag_factory,
    resume_prompt,
    run_code_factory,
)
from llm.dag import END, AgentDAG, AgentNode, DAGResult, Decision
from llm.file_formats import CheckpointStore, PlanPhase, parse_markdown_plan
from llm.permissions import AgentPermissions
from llm.result import AgentProgress, AgentResult
from llm.retry_policy import FailureDisposition
from llm.settings import AgentSettings
from llm.validated_agent_dag import (
    ValidatedRunContext,
    ValidatedRunOutcome,
    ValidatedRunPolicy,
    ValidatedRunPrompts,
    ValidatedRunRecord,
    ValidatedRunRole,
    build_validated_agent_dag,
)


def agent_result(output: str) -> AgentResult:
    return AgentResult(
        output=output,
        session_id=f"session-{output}",
        is_error=False,
        progress=AgentProgress(num_messages=1),
    )


class FakeAgent:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls: list[Any] = []

    async def run(self, prompt: Any) -> AgentResult:
        self.calls.append(prompt)
        return agent_result(self.outputs.pop(0))


class NoProgress:
    async def snapshot(self) -> None:
        return None

    def saved_progress(self, *_: Any) -> bool:
        return False


def terminal_dag(
    phase: PlanPhase,
    calls: list[int],
    outcome: ValidatedRunOutcome,
) -> PhaseDAG:
    async def execute(
        _: DAGResult[ValidatedRunContext, ValidatedRunRecord],
    ) -> ValidatedRunRecord:
        calls.append(phase.number)
        return ValidatedRunRecord(
            role=ValidatedRunRole.VALIDATOR,
            outcome=outcome,
            result=agent_result(outcome),
            failure=None,
            resumes_used=0,
            crash_streak=0,
        )

    return AgentDAG(
        [AgentNode("phase", execute, "done")],
        [Decision("done", lambda _: "end", {"end": END})],
        start_node="phase",
        max_steps=1,
    )


@pytest.mark.asyncio
async def test_coding_dag_factory_forwards_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[Any] = []
    observer_roles: list[tuple[int, ValidatedRunRole]] = []
    observers: list[Any] = []

    def observer_factory(
        phase: PlanPhase,
        role: ValidatedRunRole,
    ) -> Any:
        observer_roles.append((phase.number, role))

        def observer(_: object) -> None:
            return None

        observers.append(observer)
        return observer

    class FakeCodingAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.calls: list[tuple[Any, Any]] = []
            instances.append(self)

        async def run(
            self,
            prompt: Any,
            *,
            output_format: Any = None,
        ) -> AgentResult:
            self.calls.append((prompt, output_format))
            return AgentResult(
                output="complete",
                session_id="session",
                is_error=False,
                progress=AgentProgress(num_messages=1),
                final_result=SimpleNamespace(structured_output={"complete": True})
                if output_format is not None
                else None,
            )

    monkeypatch.setattr(code_factory, "CodingAgent", FakeCodingAgent)
    permissions = AgentPermissions()
    settings = AgentSettings()
    phase = parse_markdown_plan("## Phase 1 — One\nImplement it.")[0]
    dag = build_coding_dag_factory(
        workspace=tmp_path,
        model="test-model",
        permissions=permissions,
        settings=settings,
        max_resumes=2,
        max_steps=6,
        message_observer_factory=observer_factory,
    )(phase)

    result = await dag.run(ValidatedRunContext("prompt"))

    assert result.history[-1][1].outcome is ValidatedRunOutcome.COMPLETED
    assert dag.max_steps == 6
    assert len(instances) == 2
    assert all(agent.kwargs["model"] == "test-model" for agent in instances)
    assert all(agent.kwargs["cwd"] == tmp_path for agent in instances)
    assert all(agent.kwargs["permissions"] is permissions for agent in instances)
    assert all(agent.kwargs["settings"] is settings for agent in instances)
    assert observer_roles == [
        (1, ValidatedRunRole.INITIAL),
        (1, ValidatedRunRole.VALIDATOR),
    ]
    assert [agent.kwargs["message_observer"] for agent in instances] == observers
    assert instances[1].calls[0][1]["schema"]["required"] == ["complete"]


@pytest.mark.asyncio
async def test_code_factory_runs_fresh_phase_dags_in_order(
    tmp_path: Path,
) -> None:
    phases = parse_markdown_plan("## Phase 1 — One\nFirst.\n## Phase 2 — Two\nSecond.\n## Phase 3 — Three\nThird.")
    calls: list[int] = []
    starts: list[tuple[int, int]] = []
    checkpoint = CheckpointStore(tmp_path / "state.json", phases)

    results = await run_code_factory(
        phases=phases,
        workspace=tmp_path,
        dag_factory=lambda phase: terminal_dag(
            phase,
            calls,
            ValidatedRunOutcome.COMPLETED,
        ),
        checkpoint=checkpoint,
        on_phase_start=lambda index, phase: starts.append((index, phase.number)),
    )

    assert calls == [1, 2, 3]
    assert starts == [(0, 1), (1, 2), (2, 3)]
    assert [result.step.name for result in results] == [phase.name for phase in phases]
    assert checkpoint.completed_phases() == (1, 2, 3)


@pytest.mark.asyncio
async def test_validated_phase_routes_through_resume_before_checkpoint(
    tmp_path: Path,
) -> None:
    phases = parse_markdown_plan("## Phase 1 — One\nImplement it.")
    initial = FakeAgent(["implemented"])
    resume = FakeAgent(["continued"])
    validator_outputs = ["incomplete", "complete"]
    validators: list[FakeAgent] = []
    checkpoint = CheckpointStore(tmp_path / "state.json", phases)

    def dag_factory(_: PlanPhase) -> PhaseDAG:
        def validator_factory() -> FakeAgent:
            validator = FakeAgent([validator_outputs.pop(0)])
            validators.append(validator)
            return validator

        return build_validated_agent_dag(
            initial_agent=initial,
            validator_agent_factory=validator_factory,
            resume_agent_factory=lambda: resume,
            prompts=ValidatedRunPrompts(validator=lambda context, _: context.prompt, resume=resume_prompt),
            completion_predicate=lambda result: result.output == "complete",
            progress_detector=NoProgress(),
            classify_error=lambda _: FailureDisposition.RETRY,
            policy=ValidatedRunPolicy(max_resumes=1, max_steps=4),
        )

    results = await run_code_factory(
        phases=phases,
        workspace=tmp_path,
        dag_factory=dag_factory,
        checkpoint=checkpoint,
    )

    assert [name for name, _ in results[0].result.history] == [
        "initial",
        "validator",
        "resume",
        "validator",
    ]
    assert len(validators) == 2
    assert len(resume.calls) == 1
    assert checkpoint.completed_phases() == (1,)


@pytest.mark.asyncio
async def test_unsuccessful_phase_stops_without_checkpoint(
    tmp_path: Path,
) -> None:
    phases = parse_markdown_plan("## Phase 1 — One\nFirst.\n## Phase 2 — Two\nSecond.")
    calls: list[int] = []
    checkpoint = CheckpointStore(tmp_path / "state.json", phases)

    results = await run_code_factory(
        phases=phases,
        workspace=tmp_path,
        dag_factory=lambda phase: terminal_dag(
            phase,
            calls,
            ValidatedRunOutcome.RESUME_EXHAUSTED,
        ),
        checkpoint=checkpoint,
    )

    assert calls == [1]
    assert len(results) == 1
    assert checkpoint.completed_phases() == ()


@pytest.mark.asyncio
async def test_dag_step_exhaustion_is_not_checkpointed(tmp_path: Path) -> None:
    phases = parse_markdown_plan("## Phase 1 — One\nFirst.")
    checkpoint = CheckpointStore(tmp_path / "state.json", phases)

    def exhausted_dag(_: PlanPhase) -> PhaseDAG:
        async def execute(
            state: DAGResult[ValidatedRunContext, ValidatedRunRecord],
        ) -> ValidatedRunRecord:
            return ValidatedRunRecord(
                role=ValidatedRunRole.VALIDATOR,
                outcome=ValidatedRunOutcome.COMPLETED,
                result=agent_result("complete"),
                failure=None,
                resumes_used=0,
                crash_streak=0,
            )

        return AgentDAG(
            [AgentNode("phase", execute, "repeat")],
            [Decision("repeat", lambda _: "again", {"again": "phase"})],
            start_node="phase",
            max_steps=1,
        )

    results = await run_code_factory(
        phases=phases,
        workspace=tmp_path,
        dag_factory=exhausted_dag,
        checkpoint=checkpoint,
    )

    assert results[0].result.exhausted
    assert checkpoint.completed_phases() == ()


@pytest.mark.asyncio
async def test_restart_begins_with_first_incomplete_phase(tmp_path: Path) -> None:
    phases = parse_markdown_plan("## Phase 1 — One\nFirst.\n## Phase 2 — Two\nSecond.\n## Phase 3 — Three\nThird.")
    checkpoint = CheckpointStore(tmp_path / "state.json", phases)
    checkpoint.mark_completed(phases[0])
    calls: list[int] = []

    results = await run_code_factory(
        phases=phases,
        workspace=tmp_path,
        dag_factory=lambda phase: terminal_dag(
            phase,
            calls,
            ValidatedRunOutcome.COMPLETED,
        ),
        checkpoint=checkpoint,
    )

    assert calls == [2, 3]
    assert [result.index for result in results] == [1, 2]
    assert checkpoint.completed_phases() == (1, 2, 3)
