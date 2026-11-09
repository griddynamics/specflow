from __future__ import annotations

from graph.core.errors import ConfigurationError, GraphError
from graph.plan import PlanRulesError, TaskStateError


def test_task_state_error_is_a_graph_error_with_blockers() -> None:
    error = TaskStateError("not_ready", blockers=["TASK::A::FIRST", "TASK::B::SECOND"], status="pending")

    assert isinstance(error, GraphError)
    assert error.code == "not_ready"
    assert error.blockers == ("TASK::A::FIRST", "TASK::B::SECOND")
    assert error.fields == {"blockers": 2, "status": "pending"}
    assert str(error) == "not_ready blockers=2 status=pending"


def test_task_state_error_without_blockers_has_zero_field() -> None:
    error = TaskStateError("nothing_ready")

    assert error.blockers == ()
    assert error.fields == {"blockers": 0}


def test_plan_rules_error_lists_problems() -> None:
    error = PlanRulesError(["unknown type feature", "type requirement is not an IN_AREA source"])

    assert isinstance(error, ConfigurationError)
    assert error.code == "invalid_plan_rules"
    assert error.problems == (
        "unknown type feature",
        "type requirement is not an IN_AREA source",
    )
    assert error.fields == {"problems": 2}
