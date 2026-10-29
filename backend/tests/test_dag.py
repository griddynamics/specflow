from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from llm import (
    END,
    AgentDAG,
    AgentNode,
    DAGResult,
    Decision,
)


async def output(value: str, _: DAGResult[Any, str]) -> str:
    await asyncio.sleep(0)
    return value


@pytest.mark.asyncio
async def test_straight_through_routing_preserves_history_and_outputs() -> None:
    seen: list[DAGResult[dict[str, str], str]] = []

    async def first(state: DAGResult[dict[str, str], str]) -> str:
        seen.append(state)
        return state.context["input"]

    async def second(state: DAGResult[dict[str, str], str]) -> str:
        seen.append(state)
        return f"{state.outputs['first'][-1]}-second"

    dag = AgentDAG(
        [
            AgentNode("first", first, "continue"),
            AgentNode("second", second, "finish"),
        ],
        [
            Decision("continue", lambda _state: "next", {"next": "second"}),
            Decision("finish", lambda _state: "done", {"done": END}),
        ],
        start_node="first",
        max_steps=2,
    )

    result = await dag.run({"input": "first"})

    assert result.history == (
        ("first", "first"),
        ("second", "first-second"),
    )
    assert result.outputs == {
        "first": ("first",),
        "second": ("first-second",),
    }
    assert seen[0].history == ()
    assert seen[1].history == (("first", "first"),)
    assert result.ended
    assert not result.exhausted
    assert result.stop_reason == "end"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "expected"),
    [("left", "left"), ("right", "right")],
)
async def test_decision_branches_to_selected_node(
    choice: str,
    expected: str,
) -> None:
    dag = AgentDAG(
        [
            AgentNode("start", lambda state: output("start", state), "branch"),
            AgentNode("left", lambda state: output("left", state), "finish"),
            AgentNode("right", lambda state: output("right", state), "finish"),
        ],
        [
            Decision(
                "branch",
                lambda state: state.context,
                {"left": "left", "right": "right"},
            ),
            Decision("finish", lambda _state: True, {True: END}),
        ],
        start_node="start",
        max_steps=2,
    )

    result = await dag.run(choice)

    assert result.history[-1] == (expected, expected)
    assert result.ended


@pytest.mark.asyncio
async def test_cycles_are_allowed_and_repeated_outputs_are_grouped() -> None:
    async def count(state: DAGResult[None, int]) -> int:
        return len(state.history) + 1

    dag = AgentDAG(
        [AgentNode("count", count, "again")],
        [
            Decision(
                "again",
                lambda state: state.history[-1][1] < 3,
                {True: "count", False: END},
            )
        ],
        start_node="count",
        max_steps=3,
    )

    result = await dag.run(None)

    assert result.history == (("count", 1), ("count", 2), ("count", 3))
    assert result.outputs["count"] == (1, 2, 3)
    assert result.ended


@pytest.mark.asyncio
async def test_cycle_returns_typed_exhaustion_after_max_steps() -> None:
    dag = AgentDAG(
        [AgentNode("loop", lambda state: output("again", state), "again")],
        [Decision("again", lambda _state: "loop", {"loop": "loop"})],
        start_node="loop",
        max_steps=2,
    )

    result = await dag.run(None)

    assert result.history == (("loop", "again"), ("loop", "again"))
    assert result.exhausted
    assert not result.ended
    assert result.stop_reason == "max_steps"


@pytest.mark.asyncio
async def test_async_decision_receives_current_output() -> None:
    async def decide(state: DAGResult[None, str]) -> str:
        await asyncio.sleep(0)
        return state.history[-1][1]

    dag = AgentDAG(
        [AgentNode("agent", lambda state: output("finished", state), "finish")],
        [Decision("finish", decide, {"finished": END})],
        start_node="agent",
        max_steps=1,
    )

    result = await dag.run(None)

    assert result.ended


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["node", "decision"])
async def test_cancellation_propagates_unchanged(failure_point: str) -> None:
    async def cancel_node(_: DAGResult[None, str]) -> str:
        raise asyncio.CancelledError

    async def cancel_decision(_: DAGResult[None, str]) -> str:
        raise asyncio.CancelledError

    execute = cancel_node if failure_point == "node" else lambda state: output("done", state)
    condition = cancel_decision if failure_point == "decision" else lambda _state: "done"
    dag = AgentDAG(
        [AgentNode("agent", execute, "finish")],
        [Decision("finish", condition, {"done": END})],
        start_node="agent",
        max_steps=1,
    )

    with pytest.raises(asyncio.CancelledError):
        await dag.run(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["node", "decision"])
async def test_callable_exceptions_propagate_unchanged(
    failure_point: str,
) -> None:
    error = RuntimeError(f"{failure_point} failed")

    async def fail_node(_: DAGResult[None, str]) -> str:
        raise error

    def fail_decision(_: DAGResult[None, str]) -> str:
        raise error

    execute = fail_node if failure_point == "node" else lambda state: output("done", state)
    condition = fail_decision if failure_point == "decision" else lambda _: "done"
    dag = AgentDAG(
        [AgentNode("agent", execute, "finish")],
        [Decision("finish", condition, {"done": END})],
        start_node="agent",
        max_steps=1,
    )

    with pytest.raises(RuntimeError) as raised:
        await dag.run(None)

    assert raised.value is error


@pytest.mark.asyncio
async def test_unmapped_decision_result_has_actionable_error() -> None:
    dag = AgentDAG(
        [AgentNode("agent", lambda state: output("done", state), "finish")],
        [Decision("finish", lambda _state: "unknown", {"done": END})],
        start_node="agent",
        max_steps=1,
    )

    with pytest.raises(KeyError, match=r"finish.*unknown"):
        await dag.run(None)


def test_graph_rejects_duplicate_node_and_decision_names() -> None:
    node = AgentNode("same", lambda state: output("done", state), "finish")
    finish = Decision("finish", lambda _state: "done", {"done": END})

    with pytest.raises(ValueError, match="duplicate agent node"):
        AgentDAG(
            [node, node],
            [finish],
            start_node="same",
            max_steps=1,
        )
    with pytest.raises(ValueError, match="duplicate decision"):
        AgentDAG(
            [node],
            [finish, finish],
            start_node="same",
            max_steps=1,
        )
    with pytest.raises(ValueError, match="duplicate graph names"):
        AgentDAG(
            [AgentNode("same", node.execute, "same")],
            [Decision("same", lambda _state: "done", {"done": END})],
            start_node="same",
            max_steps=1,
        )


def test_graph_rejects_unknown_references() -> None:
    node = AgentNode("agent", lambda state: output("done", state), "finish")
    finish = Decision("finish", lambda _state: "done", {"done": END})

    with pytest.raises(ValueError, match="unknown start node"):
        AgentDAG([node], [finish], start_node="missing", max_steps=1)
    with pytest.raises(ValueError, match="unknown decision"):
        AgentDAG(
            [AgentNode("agent", node.execute, "missing")],
            [finish],
            start_node="agent",
            max_steps=1,
        )
    with pytest.raises(ValueError, match="unknown agent node"):
        AgentDAG(
            [node],
            [Decision("finish", lambda _state: "next", {"next": "missing"})],
            start_node="agent",
            max_steps=1,
        )


@pytest.mark.parametrize(
    ("max_steps", "error"),
    [(0, ValueError), (-1, ValueError), (True, TypeError), (1.5, TypeError)],
)
def test_graph_requires_positive_integer_max_steps(
    max_steps: Any,
    error: type[Exception],
) -> None:
    with pytest.raises(error, match="max_steps"):
        AgentDAG([], [], start_node="agent", max_steps=max_steps)


def test_components_validate_names_callables_and_routes() -> None:
    def execute(state: DAGResult[Any, str]) -> Coroutine[Any, Any, str]:
        return output("done", state)

    with pytest.raises(ValueError, match="name"):
        AgentNode(" ", execute, "finish")
    with pytest.raises(ValueError, match="decision"):
        AgentNode("agent", execute, " ")
    with pytest.raises(TypeError, match="execute"):
        AgentNode("agent", None, "finish")
    with pytest.raises(ValueError, match="name"):
        Decision(" ", lambda _state: "done", {"done": END})
    with pytest.raises(TypeError, match="condition"):
        Decision("finish", None, {"done": END})
    with pytest.raises(ValueError, match="routes"):
        Decision("finish", lambda _state: "done", {})


def test_decision_copies_routes_before_graph_validation() -> None:
    routes = {"done": END}
    decision = Decision("finish", lambda _state: "done", routes)
    routes["done"] = "missing"

    dag = AgentDAG(
        [AgentNode("agent", lambda state: output("done", state), "finish")],
        [decision],
        start_node="agent",
        max_steps=1,
    )

    assert dag.decisions[0].routes["done"] is END
