from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast, override

from llm.agent import AgentInput
from llm.result import AgentResult

DAGStopReason = Literal["end", "max_steps"]


class _Named(Protocol):
    @property
    def name(self) -> str: ...


class _StructuredAgentRunner(Protocol):
    async def run(
        self,
        prompt_or_messages: AgentInput,
        *,
        output_format: Mapping[str, Any] | None = None,
    ) -> AgentResult: ...


class _End:
    __slots__ = ()

    @override
    def __repr__(self) -> str:
        return "END"


END = _End()

type Destination = str | _End


@dataclass(frozen=True, slots=True)
class DAGResult[ContextT, OutputT]:
    """Current or final state of an AgentDAG execution."""

    context: ContextT
    history: tuple[tuple[str, OutputT], ...] = ()
    stop_reason: DAGStopReason | None = None

    @property
    def outputs(self) -> Mapping[str, tuple[OutputT, ...]]:
        grouped: dict[str, list[OutputT]] = {}
        for node_name, output in self.history:
            grouped.setdefault(node_name, []).append(output)
        return MappingProxyType({name: tuple(values) for name, values in grouped.items()})

    @property
    def ended(self) -> bool:
        return self.stop_reason == "end"

    @property
    def exhausted(self) -> bool:
        return self.stop_reason == "max_steps"


@dataclass(frozen=True, slots=True)
class AgentNode[ContextT, OutputT]:
    """One async execution callable followed by a named decision."""

    name: str
    execute: Callable[
        [DAGResult[ContextT, OutputT]],
        Awaitable[OutputT],
    ]
    decision: str

    def __post_init__(self) -> None:
        _validate_name(self.name, "agent node")
        _validate_name(self.decision, "agent node decision")
        if not callable(self.execute):
            raise TypeError("agent node execute must be callable")

    @classmethod
    def structured[StructuredContextT](
        cls,
        name: str,
        *,
        agent: _StructuredAgentRunner,
        output_schema: Mapping[str, Any],
        decision: str,
        prompt: AgentInput
        | Callable[
            [DAGResult[StructuredContextT, Mapping[str, Any]]],
            AgentInput | Awaitable[AgentInput],
        ]
        | None = None,
    ) -> AgentNode[StructuredContextT, Mapping[str, Any]]:
        """Create a node that returns native SDK structured output."""

        if not callable(getattr(agent, "run", None)):
            raise TypeError("structured agent must provide an async run method")
        schema = dict(output_schema)
        if not schema:
            raise ValueError("output_schema must not be empty")
        output_format = {"type": "json_schema", "schema": schema}

        async def execute(
            state: DAGResult[StructuredContextT, Mapping[str, Any]],
        ) -> Mapping[str, Any]:
            selected_prompt: Any = state.context if prompt is None else prompt
            if callable(selected_prompt):
                selected_prompt = selected_prompt(state)
            if inspect.isawaitable(selected_prompt):
                selected_prompt = await cast(Awaitable[AgentInput], selected_prompt)
            result = await agent.run(
                cast(AgentInput, selected_prompt),
                output_format=output_format,
            )
            if result.is_error:
                message = result.error.message if result.error else "Agent failed"
                raise RuntimeError(message)
            if result.structured_output is None:
                raise TypeError("Agent did not return the requested structured output")
            return MappingProxyType(dict(result.structured_output))

        return AgentNode(name=name, execute=execute, decision=decision)


@dataclass(frozen=True, slots=True)
class Decision[ContextT, OutputT, RouteT: Hashable]:
    """Route an execution state to another agent node or END."""

    name: str
    condition: Callable[
        [DAGResult[ContextT, OutputT]],
        RouteT | Awaitable[RouteT],
    ]
    routes: Mapping[RouteT, str | _End]

    def __post_init__(self) -> None:
        _validate_name(self.name, "decision")
        if not callable(self.condition):
            raise TypeError("decision condition must be callable")
        if not self.routes:
            raise ValueError("decision routes must not be empty")
        object.__setattr__(self, "routes", MappingProxyType(dict(self.routes)))

    @classmethod
    def from_output[RouteContextT](
        cls,
        name: str,
        *,
        key: str,
        routes: Mapping[RouteT, str | _End],
    ) -> Decision[RouteContextT, Mapping[str, Any], RouteT]:
        """Route using one key from the latest structured node output."""

        _validate_name(key, "structured output key")

        def condition(
            state: DAGResult[RouteContextT, Mapping[str, Any]],
        ) -> RouteT:
            if not state.history:
                raise RuntimeError(f"decision {name!r} requires a prior node output")
            output = state.history[-1][1]
            try:
                return cast(RouteT, output[key])
            except KeyError:
                raise KeyError(f"structured output has no decision key {key!r}") from None

        return Decision(name=name, condition=condition, routes=routes)


class AgentDAG[ContextT, OutputT]:
    """Execute a validated graph of async agent nodes and decisions."""

    def __init__(
        self,
        nodes: Sequence[AgentNode[ContextT, OutputT]],
        decisions: Sequence[Decision[ContextT, OutputT, Hashable]],
        *,
        start_node: str,
        max_steps: int,
    ) -> None:
        _validate_name(start_node, "start node")
        if isinstance(max_steps, bool) or not isinstance(max_steps, int):
            raise TypeError("max_steps must be an integer")
        if max_steps <= 0:
            raise ValueError("max_steps must be positive")

        self.nodes = tuple(nodes)
        self.decisions = tuple(decisions)
        self.start_node = start_node
        self.max_steps = max_steps
        self._node_by_name = _index_unique(self.nodes, "agent node")
        self._decision_by_name = _index_unique(self.decisions, "decision")
        duplicate_names = self._node_by_name.keys() & self._decision_by_name.keys()
        if duplicate_names:
            names = ", ".join(sorted(duplicate_names))
            raise ValueError(f"duplicate graph names: {names}")
        if start_node not in self._node_by_name:
            raise ValueError(f"unknown start node: {start_node}")

        for node in self.nodes:
            if node.decision not in self._decision_by_name:
                raise ValueError(f"agent node {node.name!r} references unknown decision {node.decision!r}")
        for decision in self.decisions:
            for destination in decision.routes.values():
                if destination is not END and destination not in self._node_by_name:
                    raise ValueError(f"decision {decision.name!r} routes to unknown agent node {destination!r}")

    async def run(self, context: ContextT) -> DAGResult[ContextT, OutputT]:
        """Run from the configured start node until END or step exhaustion."""

        state: DAGResult[ContextT, OutputT] = DAGResult(context=context)
        node_name = self.start_node

        for step in range(self.max_steps):
            node = self._node_by_name[node_name]
            output = await node.execute(state)
            state = replace(
                state,
                history=(*state.history, (node.name, output)),
            )

            decision = self._decision_by_name[node.decision]
            route = decision.condition(state)
            if inspect.isawaitable(route):
                route = await route
            try:
                destination = decision.routes[route]
            except KeyError:
                raise KeyError(f"decision {decision.name!r} has no route for {route!r}") from None

            if isinstance(destination, _End):
                return replace(state, stop_reason="end")
            if step + 1 == self.max_steps:
                return replace(state, stop_reason="max_steps")
            node_name = destination

        raise AssertionError("positive max_steps must execute or return")


def _validate_name(name: str, kind: str) -> None:
    if not isinstance(name, str):
        raise TypeError(f"{kind} name must be a string")
    if not name.strip():
        raise ValueError(f"{kind} name must not be empty")


def _index_unique[NamedT: _Named](
    items: Sequence[NamedT],
    kind: str,
) -> dict[str, NamedT]:
    indexed: dict[str, NamedT] = {}
    for item in items:
        if item.name in indexed:
            raise ValueError(f"duplicate {kind} name: {item.name}")
        indexed[item.name] = item
    return indexed
