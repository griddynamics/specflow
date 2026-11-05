"""Run result metrics computed from the step artifacts."""

from __future__ import annotations

from graph.plan.models import CurrentPlan, CurrentTask, DiffArtifact, NodeOp, PersistArtifact, SnapshotArtifact
from graph.plan.run import _tasks_by_status


def _task(task_id: str, status: str) -> CurrentTask:
    return CurrentTask.model_construct(task_id=task_id, status=status)


def test_tasks_by_status_applies_the_diff_writes_except_the_persist_conflicts() -> None:
    current = CurrentPlan.model_construct(
        tasks=(
            _task("TASK::A::KEEP", "completed"),
            _task("TASK::A::OLD", "pending"),
            _task("TASK::A::CLAIMED", "pending"),
            _task("TASK::A::GONE", "failed"),
        )
    )
    artifacts = {
        "snapshot": SnapshotArtifact.model_construct(current=current),
        "diff": DiffArtifact(
            tasks=(
                NodeOp(op="create", label="Task", id="TASK::A::NEW", properties={"status": "pending"}),
                NodeOp(op="supersede", label="Task", id="TASK::A::OLD", properties={"status": "superseded"}),
                NodeOp(op="supersede", label="Task", id="TASK::A::CLAIMED", properties={"status": "superseded"}),
                NodeOp(op="retire", label="Task", id="TASK::A::GONE", properties={"status": "retired"}),
                NodeOp(op="update", label="Task", id="TASK::A::KEEP", properties={"title": "Kept"}),
            )
        ),
        "persist": PersistArtifact(conflicts=("TASK::A::CLAIMED",)),
    }

    assert _tasks_by_status(artifacts) == {"completed": 1, "pending": 2, "retired": 1, "superseded": 1}


def test_tasks_by_status_is_empty_without_artifacts() -> None:
    assert _tasks_by_status({}) == {}
