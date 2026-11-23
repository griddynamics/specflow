"""The registry: one declaration per tool and per prompt, and the wrapper every tool runs in (architecture.md, Tool
registry).

The wrapper runs, from the outside in: the ``mcp.tool.completed`` audit event (FR-MCP-OPS-07), error mapping
(FR-MCP-TOOL-08, ``from_api``), the API call: one ``GET`` on the tool's agent route with the caller's token, or one
``POST`` with the JSON body for a write tool (its annotations are ``WRITE``).
Provisioning and the membership check run in the API (FR-MCP-ACC-02, ACC-05). This is the only module that uses the
SDK's tool and prompt API (FR-MCP-ACC-06). A tool error is RETURNED as a result with ``isError``: an exception that
left the wrapper would reach the client as SDK text.

A prompt (FR-MCP-BIND-02) is rendered from ``guidance`` text only: it runs no provisioning, no membership check and no
audit event, and reads no data; the guard in front of ``/mcp`` is its only access control. The SDK keeps the first of
two tools or prompts with one name and says nothing, so ``install`` refuses a duplicate.
"""

from __future__ import annotations

import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, cast

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, Elicit, MCPServer, Resolve
from mcp.server.mcpserver.prompts.base import Prompt
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request

from agent_contracts import (
    AGENT_ROUTES,
    ANSWER_INVALID,
    GAP_CARD_ROUTE,
    INTERNAL_ERROR,
    AddFindingArgs,
    AnswerGapArgs,
    AreaList,
    BriefExpansion,
    BrowseGapsArgs,
    ClaimStoryArgs,
    CompleteStoryArgs,
    Evidence,
    ExpandBriefArgs,
    FeatureList,
    FeatureTaskList,
    FindingResult,
    GapAnswerOutcome,
    GapAnswerRecord,
    GapCard,
    GapDetail,
    GapList,
    GapRowList,
    GetEvidenceArgs,
    GetNodeArgs,
    GetStoryBriefArgs,
    GetTaskBriefArgs,
    GetTaskPathArgs,
    ListAreasArgs,
    ListFeaturesArgs,
    ListFeatureTasksArgs,
    ListGapsArgs,
    ListProjectsArgs,
    ListStoriesArgs,
    ListTasksArgs,
    NodeDetail,
    NodePaths,
    OpenGapArgs,
    PathArgs,
    PreviewStoryArgs,
    ProjectArguments,
    ProjectList,
    ReleaseStoryArgs,
    ReportTaskArgs,
    SearchSpecArgs,
    SimilarArgs,
    SimilarNodes,
    SpecSearch,
    SpecTrace,
    StoryBriefResult,
    StoryClaim,
    StoryCompletion,
    StoryList,
    StoryPreview,
    StoryRelease,
    TaskBriefResult,
    TaskList,
    TaskPath,
    TaskReport,
    TraceArgs,
)
from logger.correlation import current_request_id
from logger.logger import INFO, get_logger, log_event
from mcp_server import descriptions, gap_dialog, guidance
from mcp_server.api_client import ApiResult, request_api_client
from mcp_server.errors import McpToolError, error_body, from_api
from mcp_server.http import McpInvariantError, required

logger = get_logger("mcp_server.registry")

# FR-MCP-TOOL-09: a read tool is read-only, idempotent and closed-world.
READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
# A write tool changes plan state or findings, never a spec node: not read-only, not destructive, idempotent (a
# repeat has no further effect, tools.md Repeats), closed-world. Its declaration has no method field: these
# annotations make its API call a ``POST``.
WRITE: Final = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


ANSWER_GAP: Final = "answer_gap"
INPUT_REQUIRED: Final = "input_required"  # the audit outcome of a round that only asks


@dataclass(frozen=True)
class ToolDeclaration:
    """A tool: its name, whether it takes a ``project_id``, its schemas, description and agent route.

    ``summary`` is the one line for people (the web app's guide); ``description`` is what agents read; ``route``
    is the path of the API's agent route that serves the tool (``AGENT_ROUTES``).
    """

    name: str
    summary: str
    description: str
    arguments: type[BaseModel]
    output: type[BaseModel]
    route: str
    project_scoped: bool
    annotations: ToolAnnotations = field(default_factory=lambda: READ_ONLY)
    asks_user: bool = False
    """The tool asks the person in a dialog of their client (elicitation) before it writes (``answer_gap``)."""

    @property
    def writes(self) -> bool:
        """Whether the tool changes data: its API call is a ``POST`` with a JSON body."""

        return self.annotations.read_only_hint is False


TOOLS: Final[tuple[ToolDeclaration, ...]] = (
    ToolDeclaration(
        name="list_projects",
        summary="Lists the SpecFlow projects you are a member of, newest first.",
        description=descriptions.PROJECTS_DESCRIPTION,
        arguments=ListProjectsArgs,
        output=ProjectList,
        route=AGENT_ROUTES["list_projects"],
        project_scoped=False,
    ),
    ToolDeclaration(
        name="list_features",
        summary="Lists the features in a project's specification graph.",
        description=descriptions.FEATURES_DESCRIPTION,
        arguments=ListFeaturesArgs,
        output=FeatureList,
        route=AGENT_ROUTES["list_features"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="list_tasks",
        summary="Lists the tasks of a project's implementation plan; by default the ones ready to start.",
        description=descriptions.TASKS_DESCRIPTION,
        arguments=ListTasksArgs,
        output=TaskList,
        route=AGENT_ROUTES["list_tasks"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="list_feature_tasks",
        summary="Lists every plan task that works on one feature, ready or not.",
        description=descriptions.FEATURE_TASKS_DESCRIPTION,
        arguments=ListFeatureTasksArgs,
        output=FeatureTaskList,
        route=AGENT_ROUTES["list_feature_tasks"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="get_task_path",
        summary="Lists the tasks to complete before one plan task, in build order, then the task itself.",
        description=descriptions.PATH_DESCRIPTION,
        arguments=GetTaskPathArgs,
        output=TaskPath,
        route=AGENT_ROUTES["get_task_path"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="get_task_brief",
        summary="Reads what a coding agent needs to build one plan task: done-when, spec sources, prerequisites.",
        description=descriptions.BRIEF_DESCRIPTION,
        arguments=GetTaskBriefArgs,
        output=TaskBriefResult,
        route=AGENT_ROUTES["get_task_brief"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="list_areas",
        summary=(
            "Lists the work areas of a project's plan with their ready, in-progress, waiting, blocked and done stories."
        ),
        description=descriptions.AREAS_DESCRIPTION,
        arguments=ListAreasArgs,
        output=AreaList,
        route=AGENT_ROUTES["list_areas"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="list_stories",
        summary=(
            "Lists the stories of a project's plan in recommended order, or the ones you hold, or the finished ones."
        ),
        description=descriptions.STORIES_DESCRIPTION,
        arguments=ListStoriesArgs,
        output=StoryList,
        route=AGENT_ROUTES["list_stories"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="preview_story",
        summary="Shows one story before you take it: state, holder, tasks in build order, what it waits for, findings.",
        description=descriptions.PREVIEW_DESCRIPTION,
        arguments=PreviewStoryArgs,
        output=StoryPreview,
        route=AGENT_ROUTES["preview_story"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="claim_story",
        summary="Takes one story for you and your agent; one claim wins.",
        description=descriptions.CLAIM_DESCRIPTION,
        arguments=ClaimStoryArgs,
        output=StoryClaim,
        route=AGENT_ROUTES["claim_story"],
        project_scoped=True,
        annotations=WRITE,
    ),
    ToolDeclaration(
        name="release_story",
        summary="Gives a story you hold back, with an optional note.",
        description=descriptions.RELEASE_DESCRIPTION,
        arguments=ReleaseStoryArgs,
        output=StoryRelease,
        route=AGENT_ROUTES["release_story"],
        project_scoped=True,
        annotations=WRITE,
    ),
    ToolDeclaration(
        name="get_story_brief",
        summary="Reads what an agent needs to start a story: tasks, done-when, spec sources, prerequisites, findings.",
        description=descriptions.STORY_BRIEF_DESCRIPTION,
        arguments=GetStoryBriefArgs,
        output=StoryBriefResult,
        route=AGENT_ROUTES["get_story_brief"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="trace",
        summary="Traces what a story or spec nodes depend on, or what depends on them, with plan state.",
        description=descriptions.TRACE_DESCRIPTION,
        arguments=TraceArgs,
        output=SpecTrace,
        route=AGENT_ROUTES["trace"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="get_node",
        summary="Reads one spec node: edges, plan state, provenance and findings.",
        description=descriptions.NODE_DESCRIPTION,
        arguments=GetNodeArgs,
        output=NodeDetail,
        route=AGENT_ROUTES["get_node"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="search_spec",
        summary="Searches the spec graph by id, text and meaning, optionally near a story or node.",
        description=descriptions.SEARCH_DESCRIPTION,
        arguments=SearchSpecArgs,
        output=SpecSearch,
        route=AGENT_ROUTES["search_spec"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="similar",
        summary="Finds spec nodes whose text reads like a story's nodes but that have no edge to them.",
        description=descriptions.SIMILAR_DESCRIPTION,
        arguments=SimilarArgs,
        output=SimilarNodes,
        route=AGENT_ROUTES["similar"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="get_evidence",
        summary="Reads the exact source lines behind one spec node.",
        description=descriptions.EVIDENCE_DESCRIPTION,
        arguments=GetEvidenceArgs,
        output=Evidence,
        route=AGENT_ROUTES["get_evidence"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="path",
        summary="Shows how two spec nodes connect: up to three shortest directed chains.",
        description=descriptions.PATH_TOOL_DESCRIPTION,
        arguments=PathArgs,
        output=NodePaths,
        route=AGENT_ROUTES["path"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="expand_brief",
        summary="Ranks what to read next beyond a story's brief, within a token budget.",
        description=descriptions.EXPAND_DESCRIPTION,
        arguments=ExpandBriefArgs,
        output=BriefExpansion,
        route=AGENT_ROUTES["expand_brief"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="list_gaps",
        summary="Lists the open gaps in the spec about a story or spec nodes: question, severity, status and answer.",
        description=descriptions.LIST_GAPS_DESCRIPTION,
        arguments=ListGapsArgs,
        output=GapList,
        route=AGENT_ROUTES["list_gaps"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="browse_gaps",
        summary="Lists a project's gaps as table rows, filtered as on the web Gaps screen: status, kind, severity, "
        "assignee, story.",
        description=descriptions.BROWSE_GAPS_DESCRIPTION,
        arguments=BrowseGapsArgs,
        output=GapRowList,
        route=AGENT_ROUTES["browse_gaps"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name="open_gap",
        summary="Shows one gap whole, as its web card does: question, quotes, options, answer, history and reach.",
        description=descriptions.OPEN_GAP_DESCRIPTION,
        arguments=OpenGapArgs,
        output=GapDetail,
        route=AGENT_ROUTES["open_gap"],
        project_scoped=True,
    ),
    ToolDeclaration(
        name=ANSWER_GAP,
        summary="Lets you answer one open gap in a dialog of your coding agent; the answer is recorded as yours.",
        description=descriptions.ANSWER_GAP_DESCRIPTION,
        arguments=AnswerGapArgs,
        output=GapAnswerOutcome,
        route=AGENT_ROUTES["answer_gap"],
        project_scoped=True,
        annotations=WRITE,
        asks_user=True,
    ),
    ToolDeclaration(
        name="add_finding",
        summary="Records a gap, conflict, question or note about the spec, linked to nodes and code; people answer it.",
        description=descriptions.ADD_FINDING_DESCRIPTION,
        arguments=AddFindingArgs,
        output=FindingResult,
        route=AGENT_ROUTES["add_finding"],
        project_scoped=True,
        annotations=WRITE,
    ),
    ToolDeclaration(
        name="report_task",
        summary="Reports progress on one task of your claimed story; its status stays until the story completes.",
        description=descriptions.REPORT_TASK_DESCRIPTION,
        arguments=ReportTaskArgs,
        output=TaskReport,
        route=AGENT_ROUTES["report_task"],
        project_scoped=True,
        annotations=WRITE,
    ),
    ToolDeclaration(
        name="complete_story",
        summary="Closes your claimed story with the merge commit and the pull request.",
        description=descriptions.COMPLETE_STORY_DESCRIPTION,
        arguments=CompleteStoryArgs,
        output=StoryCompletion,
        route=AGENT_ROUTES["complete_story"],
        project_scoped=True,
        annotations=WRITE,
    ),
)


@dataclass(frozen=True)
class PromptDeclaration:
    """A prompt: its name, summary and description, its one optional argument, and the render of its text.

    ``summary`` is the one line for people; ``description`` is what agents read.

    ``render(resource_url, argument)`` gets ``None`` when the caller sent no argument.
    """

    name: str
    summary: str
    description: str
    argument: str
    argument_description: str
    render: Callable[[str, str | None], str]


PROMPTS: Final[tuple[PromptDeclaration, ...]] = (
    PromptDeclaration(
        name=guidance.SELECT_PROJECT_NAME,
        summary="Binds the current repository to one of your SpecFlow projects.",
        description=guidance.SELECT_PROJECT_DESCRIPTION,
        argument=guidance.PROJECT_ARGUMENT,
        argument_description=guidance.PROJECT_ARGUMENT_DESCRIPTION,
        render=guidance.render_select_project,
    ),
    PromptDeclaration(
        name=guidance.START_NAME,
        summary="Walks you from a project to a claimed story: areas, stories, preview, claim.",
        description=guidance.START_DESCRIPTION,
        argument=guidance.AREA_ARGUMENT,
        argument_description=guidance.AREA_ARGUMENT_DESCRIPTION,
        render=guidance.render_start,
    ),
    PromptDeclaration(
        name=guidance.ANSWER_GAPS_NAME,
        summary="Walks you through the open gaps of your project; you answer each one in a dialog.",
        description=guidance.ANSWER_GAPS_DESCRIPTION,
        argument=guidance.GAP_ARGUMENT,
        argument_description=guidance.GAP_ARGUMENT_DESCRIPTION,
        render=guidance.render_answer_gaps,
    ),
)


def render_manifest(
    tools: Sequence[ToolDeclaration] = TOOLS, prompts: Sequence[PromptDeclaration] = PROMPTS
) -> dict[str, Any]:
    """The manifest the web app's guide reads, as plain data in declaration order.

    Names, summaries and flags only: no description (it carries the untrusted-content notices) and no schema.
    """

    return {
        "tools": [
            {"name": t.name, "summary": t.summary, "read_only": t.annotations.read_only_hint is True} for t in tools
        ],
        "prompts": [
            {"name": p.name, "summary": p.summary, "arguments": [{"name": p.argument, "required": False}]}
            for p in prompts
        ],
    }


def render_manifest_json(
    tools: Sequence[ToolDeclaration] = TOOLS, prompts: Sequence[PromptDeclaration] = PROMPTS
) -> str:
    """The committed file's exact text: sorted keys, two-space indent, UTF-8 characters, one trailing newline."""

    return json.dumps(render_manifest(tools, prompts), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _result(model: BaseModel) -> CallToolResult:
    """Structured content plus one text block holding the same JSON (FR-MCP-TOOL-05)."""

    return CallToolResult(
        content=[TextContent(type="text", text=model.model_dump_json())],
        structured_content=model.model_dump(mode="json"),
    )


def _error_result(error: McpToolError) -> CallToolResult:
    """``isError`` with ``{"error": {"code", "message"}}``: fixed text, never an argument or exception text."""

    body = error_body(error.code, error.details)
    return CallToolResult(
        content=[TextContent(type="text", text=body.model_dump_json())],
        structured_content=body.model_dump(mode="json"),
        is_error=True,
    )


def _parameters(arguments: type[BaseModel]) -> tuple[list[inspect.Parameter], dict[str, Any]]:
    """One keyword per argument field, as the SDK reads a signature (it builds the input schema from them)."""

    parameters: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for name, info in arguments.model_fields.items():
        # ``alias``: a wire name that is no Python name (``from``)
        described = info.description or info.alias
        extras = [*info.metadata, *([Field(description=info.description, alias=info.alias)] if described else [])]
        annotation = Annotated[(info.annotation, *extras)] if extras else info.annotation
        default = inspect.Parameter.empty if info.is_required() else info.get_default(call_default_factory=True)
        parameters.append(
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation)
        )
        annotations[name] = annotation
    return parameters, annotations


def _log_completed(fields: dict[str, Any], outcome: str, item_count: int, started: float) -> None:
    log_event(
        logger,
        INFO,
        "mcp.tool.completed",
        outcome=outcome,
        item_count=item_count,
        duration_ms=round((time.monotonic() - started) * 1000),
        **fields,
    )


async def _completed(
    fields: dict[str, Any], run: Callable[[], Awaitable[tuple[CallToolResult, int]]]
) -> CallToolResult:
    """The wrapper every tool runs in: ``run`` builds the result, any failure becomes the error result of its code,
    and the ``mcp.tool.completed`` audit event is logged with ``fields``."""

    started = time.monotonic()
    outcome: str = INTERNAL_ERROR  # what an escaping BaseException (cancellation) leaves
    item_count = 0
    try:
        try:
            result, count = await run()  # builds the result: a failure is mapped too
        except McpToolError:
            raise
        except Exception as error:  # the one boundary: nothing but a code leaves
            raise McpToolError(INTERNAL_ERROR, type(error).__name__) from error
    except McpToolError as error:
        outcome = error.code
        fields["error_type"] = error.error_type
        return _error_result(error)
    else:
        outcome, item_count = "ok", count  # only after the result was built
        return result
    finally:
        _log_completed(fields, outcome, item_count, started)


async def _api_call(
    ctx: Context, route: str, params: Mapping[str, str | list[str]], body: dict[str, Any] | None = None
) -> ApiResult:
    """One call of an agent route with the caller's token and the MCP request id."""

    request = ctx.request_context.request
    if not isinstance(request, Request):
        raise McpInvariantError("tools run over HTTP, behind the guard")
    authorization = required(request.headers.get("authorization"), "the request passed the MCP guard")
    return await request_api_client(request).call(
        route, params, authorization, required(current_request_id(), "the request has an id"), body=body
    )


def _scope(fields: dict[str, Any], project_id: str) -> None:
    project = _canonical_uuid(project_id)
    if project is not None:  # a malformed id is raw caller text: never logged
        fields["project_id"] = project


class _ToolFunction:
    """The callable the SDK registers for one declaration.

    The SDK builds the input and output schemas from the signature and annotations, so both are
    synthesized from the declaration's models: ``Context`` plus one keyword per argument field.
    """

    def __init__(self, declaration: ToolDeclaration) -> None:
        self.declaration = declaration
        fields, annotations = _parameters(declaration.arguments)
        parameters = [inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context), *fields]
        returns = Annotated[CallToolResult, declaration.output]  # publishes the output schema
        self.__name__ = declaration.name
        self.__signature__ = inspect.Signature(parameters, return_annotation=returns)
        self.__annotations__ = {"ctx": Context, **annotations, "return": returns}

    async def __call__(self, **raw: Any) -> CallToolResult:
        ctx: Context = raw.pop("ctx")
        fields: dict[str, Any] = {"tool": self.declaration.name}
        return await _completed(fields, lambda: self._run(ctx, raw, fields))

    async def _run(self, ctx: Context, raw: dict[str, Any], fields: dict[str, Any]) -> tuple[CallToolResult, int]:
        declaration = self.declaration
        args = declaration.arguments.model_validate(raw)
        scoped = args if isinstance(args, ProjectArguments) else None
        if declaration.project_scoped and scoped is None:
            raise McpInvariantError("a project-scoped tool takes a project_id")
        if scoped is not None:
            _scope(fields, scoped.project_id)
        # a write sends the project in the query, the other arguments as the JSON body (architecture.md, Write routes)
        params = {"project_id": scoped.project_id} if declaration.writes and scoped is not None else _query(args)
        body = _body(args) if declaration.writes else None
        reply = await _api_call(ctx, declaration.route, params, body)
        if reply.user_id is not None:  # the API names the provisioned user on every answer after provisioning
            fields["user_id"] = reply.user_id
        result = from_api(reply, declaration.output)
        return _result(result), _item_count(result)


# --- answer_gap: the answer dialog (docs/mcp-server/apps.md "Terminal dialogs", D6) --------------------------------

# The first protocol whose tools/call carries the dialog as ``input_required``: a stateless server has no session to
# send ``elicitation/create`` on during an older call.
DIALOG_PROTOCOL: Final = "2026-07-28"
NOT_SHOWN: Final = (
    "Nothing was recorded. If the user saw no dialog (the Claude Desktop Code tab and the VS Code extension do not "
    "show it), give them web_url to answer on the web card."
)
NO_DIALOG: Final = "Nothing was recorded: {reason} Give the user web_url to answer on the web card."
RETRY: Final = "Nothing was recorded: {problem} Call answer_gap again so that the user can answer."
CONTRADICTION: Final = "contradiction"
CONTRADICTION_OWN: Final = (
    "Nothing was recorded: a contradiction is answered by keeping one of its two lines, so an own answer cannot go to "
    "the clarifications document. Call answer_gap again so that the user can tick an option, or give the user web_url "
    "to answer in their own words on the web card, which turns the text into edits of the losing line."
)
STAGED: Final = (
    "Staged as the user's answer. It reaches the specification when someone presses Resolve gaps on the web Gaps "
    "screen (web_url)."
)
APPLIED_AT_ONCE: Final = (
    "Recorded as the user's answer and applied at once, because a coding agent waits on this gap: the specification "
    "is being rebuilt."
)


@dataclass(frozen=True)
class GapRead:
    """What ``read_gap`` found: the card, or the error that ends the call; ``user_id`` and ``started`` for the audit."""

    started: float
    card: GapCard | None = None
    error: McpToolError | None = None
    user_id: str | None = None


@dataclass(frozen=True)
class NoDialog:
    """Why the client gets no dialog; empty when the card could not be read (the tool reports that error)."""

    reason: str = ""


async def read_gap(ctx: Context, project_id: str, gap_id: str) -> GapRead:
    """The first resolver of ``answer_gap``: the gap's card through the API with the caller's token. It runs on every
    round of the call and never raises: a failure becomes the tool's error result, not SDK text."""

    started = time.monotonic()
    try:
        try:
            reply = await _api_call(ctx, GAP_CARD_ROUTE, {"project_id": project_id, "gap_id": gap_id})
            return GapRead(started, card=from_api(reply, GapCard), user_id=reply.user_id)
        except McpToolError:
            raise
        except Exception as error:  # as in ``_completed``: nothing but a code leaves
            raise McpToolError(INTERNAL_ERROR, type(error).__name__) from error
    except McpToolError as error:
        return GapRead(started, error=error)


def _shows_forms(ctx: Context) -> bool:
    """As the SDK decides: a bare ``elicitation`` capability counts as form support, url-only does not."""

    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


async def ask_gap_answer(
    ctx: Context, read: Annotated[GapRead, Resolve(read_gap)], project_id: str
) -> Elicit[BaseModel] | NoDialog:
    """The second resolver: the dialog, when the card was read and the client can show it on this stateless server.
    The first round of a call only asks; it is audited here, the last round by the tool."""

    if read.card is None:
        return NoDialog()
    version = ctx.protocol_version
    if version is None or version < DIALOG_PROTOCOL:  # protocol versions are dates: YYYY-MM-DD
        return NoDialog(f"this client speaks MCP protocol {version or 'unknown'}; the dialog needs {DIALOG_PROTOCOL}.")
    if not _shows_forms(ctx):
        return NoDialog("this client shows no forms.")
    if ctx.request_state is None:
        fields: dict[str, Any] = {"tool": ANSWER_GAP}
        _scope(fields, project_id)
        if read.user_id is not None:
            fields["user_id"] = read.user_id
        _log_completed(fields, INPUT_REQUIRED, 0, read.started)
    return Elicit(gap_dialog.message(read.card), gap_dialog.form(read.card))


async def _answer(
    declaration: ToolDeclaration,
    *,
    ctx: Context,
    read: GapRead,
    answer: ElicitationResult[BaseModel],
    raw: dict[str, Any],
    fields: dict[str, Any],
) -> tuple[CallToolResult, int]:
    """The last round: what the person did, checked; an answer is written through the agent route."""

    args = AnswerGapArgs.model_validate(raw)
    _scope(fields, args.project_id)
    if read.user_id is not None:
        fields["user_id"] = read.user_id
    if read.error is not None:
        raise read.error
    card = required(read.card, "a read without an error has a card")

    def outcome(**values: Any) -> CallToolResult:
        return _result(GapAnswerOutcome(project_id=args.project_id, gap_id=card.gap_id, web_url=card.web_url, **values))

    # a plain NoDialog from the resolver arrives as accepted data, beside the form of an accepted dialog
    data: object = answer.data if isinstance(answer, AcceptedElicitation) else None
    if isinstance(data, NoDialog):
        fields["dialog"] = "no_dialog"
        return outcome(answered=False, action="no_dialog", note=NO_DIALOG.format(reason=data.reason)), 1
    if not isinstance(data, BaseModel):  # declined or cancelled: the dialog, if any, recorded nothing
        fields["dialog"] = answer.action
        return outcome(answered=False, action=answer.action, note=NOT_SHOWN), 1
    fields["dialog"] = "accept"
    sent = gap_dialog.checked(card, data)
    if sent.problem is not None:
        return outcome(answered=False, action="accept", note=RETRY.format(problem=sent.problem)), 1
    chosen = {"option_rank": sent.option.rank} if sent.option is not None else {"own_text": sent.own_text}
    body = {"gap_id": card.gap_id, "updated_at": card.updated_at.isoformat(), **chosen}
    reply = await _api_call(ctx, declaration.route, {"project_id": args.project_id}, body)
    try:
        record = from_api(reply, GapAnswerRecord)
    except McpToolError as error:  # the service refuses an own answer that does not amend a contradiction
        if error.code != ANSWER_INVALID or sent.own_text is None or card.kind != CONTRADICTION:
            raise
        return outcome(answered=False, action="accept", note=CONTRADICTION_OWN), 1
    return (
        outcome(
            answered=True,
            action="accept",
            choice=sent.choice,
            placement=record.placement,
            clr_id=record.clr_id,
            apply_at=record.apply_at,
            note=APPLIED_AT_ONCE if record.immediate else STAGED,
        ),
        1,
    )


def _dialog_function(declaration: ToolDeclaration) -> Callable[..., Awaitable[CallToolResult]]:
    """The function the SDK registers for a tool that asks the person (``answer_gap``): ``Context``, the two resolved
    parameters (the card, then the person's answer) and one keyword per argument field. A real function, so that the
    SDK finds the ``Resolve`` markers in its annotations."""

    async def tool(**raw: Any) -> CallToolResult:
        ctx: Context = raw.pop("ctx")
        read: GapRead = raw.pop("read")
        answer: ElicitationResult[BaseModel] = raw.pop("answer")
        fields: dict[str, Any] = {"tool": declaration.name}
        return await _completed(
            fields, lambda: _answer(declaration, ctx=ctx, read=read, answer=answer, raw=raw, fields=fields)
        )

    resolved = {
        "read": Annotated[GapRead, Resolve(read_gap)],
        "answer": Annotated[ElicitationResult[BaseModel], Resolve(ask_gap_answer)],
    }
    fields, annotations = _parameters(declaration.arguments)
    parameters = [
        inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context),
        *(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=kind) for name, kind in resolved.items()),
        *fields,
    ]
    returns = Annotated[CallToolResult, declaration.output]
    tool.__name__ = declaration.name
    cast(Any, tool).__signature__ = inspect.Signature(parameters, return_annotation=returns)
    tool.__annotations__ = {"ctx": Context, **resolved, **annotations, "return": returns}
    return tool


def _canonical_uuid(value: str) -> str | None:
    """The canonical form of ``value`` when it spells a UUID, else ``None``."""

    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _query_value(value: object) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def _query(args: BaseModel) -> Mapping[str, str | list[str]]:
    """The validated arguments as query parameters: booleans ``true``/``false``, integers as decimal strings, a list
    as a repeated parameter, ``None`` omitted."""

    return {
        name: [_query_value(item) for item in value] if isinstance(value, list) else _query_value(value)
        for name, value in args.model_dump(exclude_none=True).items()
    }


def _body(args: BaseModel) -> dict[str, Any]:
    """The JSON body of a write: the arguments besides the project, ``None`` left out."""

    return args.model_dump(mode="json", exclude={"project_id"}, exclude_none=True)


def _item_count(result: BaseModel) -> int:
    """What the audit counts: the items of a listing or the nodes of a trace, else 1 (one object, such as a brief)."""

    for name in ("items", "nodes"):
        found = getattr(result, name, None)
        if isinstance(found, list):
            return len(found)
    return 1


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
            _dialog_function(declaration) if declaration.asks_user else _ToolFunction(declaration),
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
