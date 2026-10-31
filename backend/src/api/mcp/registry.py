"""The registry: one declaration per tool and per prompt, and the wrapper every tool runs in (architecture.md, Tool
registry).

The wrapper runs, from the outside in: the ``mcp.tool.completed`` audit event (FR-MCP-OPS-07), error mapping
(FR-MCP-TOOL-08), user provisioning (FR-MCP-ACC-05), the membership check of project-scoped tools (FR-MCP-ACC-02),
the handler. This is the only module that uses the SDK's tool and prompt API (FR-MCP-ACC-06). A tool error is
RETURNED as a result with ``isError``: an exception that left the wrapper would reach the client as SDK text.

A prompt (FR-MCP-BIND-02) is rendered from ``guidance`` text only: it runs no provisioning, no membership check and no
audit event, and reads no data; the guard in front of ``/mcp`` is its only access control. The SDK keeps the first of
two tools or prompts with one name and says nothing, so ``install`` refuses a duplicate.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, cast

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.prompts.base import Prompt
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request

from api.mcp import access, guidance
from api.mcp.auth import request_principal
from api.mcp.errors import INTERNAL_ERROR, McpToolError, error_body, to_tool_error
from api.mcp.http import McpInvariantError
from api.mcp.runtime import request_runtime
from api.mcp.tools import Listing, ProjectArguments, graph, projects
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.mcp.registry")

# FR-MCP-TOOL-09: every version 1 tool is read-only, idempotent and closed-world.
READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


@dataclass(frozen=True)
class ToolDeclaration:
    """A tool: its name, whether it takes a ``project_id``, its schemas, description and handler."""

    name: str
    description: str
    arguments: type[BaseModel]
    output: type[BaseModel]
    handler: Callable[..., Awaitable[Listing]]
    project_scoped: bool
    annotations: ToolAnnotations = field(default_factory=lambda: READ_ONLY)


TOOLS: Final[tuple[ToolDeclaration, ...]] = (
    ToolDeclaration(
        name="list_projects",
        description=projects.DESCRIPTION,
        arguments=projects.ListProjectsArgs,
        output=projects.ProjectList,
        handler=projects.list_projects,
        project_scoped=False,
    ),
    ToolDeclaration(
        name="list_features",
        description=graph.DESCRIPTION,
        arguments=graph.ListFeaturesArgs,
        output=graph.FeatureList,
        handler=graph.list_features,
        project_scoped=True,
    ),
)


@dataclass(frozen=True)
class PromptDeclaration:
    """A prompt: its name and description, its one optional argument, and the render of its text.

    ``render(resource_url, argument)`` gets ``None`` when the caller sent no argument.
    """

    name: str
    description: str
    argument: str
    argument_description: str
    render: Callable[[str, str | None], str]


PROMPTS: Final[tuple[PromptDeclaration, ...]] = (
    PromptDeclaration(
        name=guidance.SELECT_PROJECT_NAME,
        description=guidance.SELECT_PROJECT_DESCRIPTION,
        argument=guidance.PROJECT_ARGUMENT,
        argument_description=guidance.PROJECT_ARGUMENT_DESCRIPTION,
        render=guidance.render_select_project,
    ),
)


def _result(model: BaseModel) -> CallToolResult:
    """Structured content plus one text block holding the same JSON (FR-MCP-TOOL-05)."""

    return CallToolResult(
        content=[TextContent(type="text", text=model.model_dump_json())],
        structured_content=model.model_dump(mode="json"),
    )


def _error_result(error: McpToolError) -> CallToolResult:
    """``isError`` with ``{"error": {"code", "message"}}``: fixed text, never an argument or exception text."""

    body = error_body(error.code)
    return CallToolResult(
        content=[TextContent(type="text", text=body.model_dump_json())],
        structured_content=body.model_dump(mode="json"),
        is_error=True,
    )


class _ToolFunction:
    """The callable the SDK registers for one declaration.

    The SDK builds the input and output schemas from the signature and annotations, so both are
    synthesized from the declaration's models: ``Context`` plus one keyword per argument field.
    """

    def __init__(self, declaration: ToolDeclaration) -> None:
        self.declaration = declaration
        parameters = [inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context)]
        annotations: dict[str, Any] = {"ctx": Context}
        for name, info in declaration.arguments.model_fields.items():
            extras = [*info.metadata, *([Field(description=info.description)] if info.description else [])]
            annotation = Annotated[(info.annotation, *extras)] if extras else info.annotation
            default = inspect.Parameter.empty if info.is_required() else info.default
            parameters.append(
                inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation)
            )
            annotations[name] = annotation
        returns = Annotated[CallToolResult, declaration.output]  # publishes the output schema
        annotations["return"] = returns
        self.__name__ = declaration.name
        self.__signature__ = inspect.Signature(parameters, return_annotation=returns)
        self.__annotations__ = annotations

    async def __call__(self, **raw: Any) -> CallToolResult:
        ctx: Context = raw.pop("ctx")
        declaration = self.declaration
        started = time.monotonic()
        fields: dict[str, Any] = {"tool": declaration.name}
        outcome: str = INTERNAL_ERROR  # what an escaping BaseException (cancellation) leaves
        item_count = 0
        try:
            try:
                result, count = await self._run(ctx, raw, fields)  # builds the result: a failure is mapped too
            except Exception as error:  # the one boundary: nothing but a code leaves
                raise to_tool_error(error) from error
        except McpToolError as error:
            outcome = error.code
            fields["error_type"] = error.error_type
            return _error_result(error)
        else:
            outcome, item_count = "ok", count  # only after the result was built
            return result
        finally:
            log_event(
                logger,
                INFO,
                "mcp.tool.completed",
                outcome=outcome,
                item_count=item_count,
                duration_ms=round((time.monotonic() - started) * 1000),
                **fields,
            )

    async def _run(self, ctx: Context, raw: dict[str, Any], fields: dict[str, Any]) -> tuple[CallToolResult, int]:
        declaration = self.declaration
        request = ctx.request_context.request
        if not isinstance(request, Request):
            raise McpInvariantError("tools run over HTTP, behind the guard")
        principal = request_principal(request)
        runtime = request_runtime(request)
        args = declaration.arguments.model_validate(raw)
        scoped = args if isinstance(args, ProjectArguments) else None
        if declaration.project_scoped and scoped is None:
            raise McpInvariantError("a project-scoped tool takes a project_id")
        if scoped is not None:
            parsed = access.parse_project_id(scoped.project_id)
            if parsed is not None:  # a malformed id is raw caller text: never logged
                fields["project_id"] = str(parsed)
        user_id = await access.provision(runtime, principal)
        fields["user_id"] = str(user_id)
        caller = access.Caller(principal, user_id)
        if scoped is not None:
            project = await access.member_project(runtime, user_id, scoped.project_id)
            result = await declaration.handler(caller, runtime, args, project)
        else:
            result = await declaration.handler(caller, runtime, args)
        if not isinstance(result, declaration.output):  # a wrong shape would reach the SDK's validation text
            raise McpInvariantError("a handler returns its declared output")
        return _result(result), len(cast(Listing, result).items)  # shape checked above


def _prompt_function(declaration: PromptDeclaration, resource_url: str) -> Callable[..., str]:
    """The function the SDK registers for one prompt: one optional string keyword named by the declaration.

    The SDK reads the argument list from the signature, so it is synthesized, as ``_ToolFunction`` does; a real
    function, because the SDK wraps it in ``validate_call``.
    """

    annotation = Annotated[str | None, Field(description=declaration.argument_description)]

    def prompt(**raw: str | None) -> str:
        return declaration.render(resource_url, raw.get(declaration.argument))

    parameter = inspect.Parameter(
        declaration.argument, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=annotation
    )
    prompt.__name__ = declaration.name
    cast(Any, prompt).__signature__ = inspect.Signature([parameter], return_annotation=str)
    prompt.__annotations__ = {declaration.argument: annotation, "return": str}
    return prompt


def _unique(names: Sequence[str], kind: str) -> None:
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise McpInvariantError(f"duplicate {kind} names: {', '.join(duplicates)}")


def install(
    server: MCPServer,
    tools: Sequence[ToolDeclaration] = TOOLS,
    *,
    resource_url: str,
    prompts: Sequence[PromptDeclaration] = PROMPTS,
) -> None:
    """Register ``tools`` and ``prompts`` on ``server`` (the ``tools`` hook of ``create_mcp_app``).

    ``resource_url`` is the deployment's ``MCP_RESOURCE_URL``, which the prompt text carries.
    """

    _unique([t.name for t in tools], "tool")
    _unique([p.name for p in prompts], "prompt")
    for declaration in tools:
        server.add_tool(
            _ToolFunction(declaration),
            name=declaration.name,
            description=declaration.description,
            annotations=declaration.annotations,
        )
    for prompt in prompts:
        server.add_prompt(
            Prompt.from_function(
                _prompt_function(prompt, resource_url), name=prompt.name, description=prompt.description
            )
        )
