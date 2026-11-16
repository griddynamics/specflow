"""The gaps of a project for people (docs/graph/gap/api.md "Web routes"): reads, answers, dismissals, raised gaps,
noise rules and the gap runs.

``GET /gaps`` and ``GET /gaps/{gap_id}`` are ``GapService.list_gaps`` and ``get_gap``. ``POST /gap/runs`` queues a
``full`` gap run in this process (``api.gap.runner``; ``202`` at once, then poll ``GET /gap/runs/latest``, as the plan),
and ``GET /gap/runs/{run_id}/noise`` reads a finished run's noise report. Project membership is the access rule: a
non-member gets ``404 project_not_found``, as on every project route. ``503 gap_unavailable`` while gap runs are not
configured (no graph build settings, or ``GAP_ENABLED`` off). Logs carry ids, counts and codes only (NFR-GSEC-02).

The writes are ``GapService`` calls with the member as actor (FR-GANS-13: membership is the only access rule):
``POST /gaps`` raises a gap (origin ``person``) and queues a ``site`` run on it; ``POST /gaps/{gap_id}/edits_preview``
and ``/answer``; an answer schedules the apply at the end of the quiet period (``POST /gaps/apply`` applies at once);
``POST /gaps/{gap_id}/dismiss``; ``GET``/``POST /gaps/noise_rules`` and ``DELETE /gaps/noise_rules/{rule_id}``
(registered before ``/gaps/{gap_id}``). Refusals: ``400 answer_invalid`` (``details`` name the field and the check),
``409 gap_wrong_state``, ``409 gap_stale`` (reload the gap), ``404 gap_not_found``, ``404 noise_rule_not_found``.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Path, Query, Response

from api.deps import RuntimeDep, UserDep
from api.errors import ApiError, invalid_arguments, invalid_cursor
from api.gap.state import GapRunnerReads, GapRunState
from api.gap_models import (
    MAX_ID,
    GapAnswerBody,
    GapDismissBody,
    GapEditsPreviewBody,
    GapListQuery,
    GapRaiseBody,
    GapRunStatus,
    NoiseRuleBody,
    NoiseRuleCreated,
    NoiseRuleList,
)
from api.graph_build.corpus import document_lineage
from api.plan.state import NoGraphBuildError
from api.routers.documents import require_project
from api.routers.plan import no_graph_build
from api.runtime import Runtime
from graph.gap import (
    AnswerInput,
    AnswerInvalidError,
    AnswerResult,
    ApplyResult,
    EditPreview,
    GapNotFoundError,
    GapPage,
    GapStateError,
    GapView,
    NewGap,
    NoiseReport,
    RaisedGap,
    StaleGapError,
)
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.routers.gaps")
router = APIRouter(prefix="/projects/{pid}", tags=["gaps"])

RUN_ID_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
RULE_ID_PATTERN: Final = r"^NR::[0-9A-F]{12}$"
GapId = Annotated[str, Path(min_length=1, max_length=MAX_ID)]
CURSOR: Final = "cursor"


def gap_unavailable() -> ApiError:
    return ApiError(503, "gap_unavailable", "Gap runs are not configured.")


def gap_not_found() -> ApiError:
    return ApiError(404, "gap_not_found", "The gap does not exist.")


def gap_wrong_state(error: GapStateError) -> ApiError:
    return ApiError(
        409,
        "gap_wrong_state",
        "The gap is not in a state this action applies to.",
        details={"status": error.status, "allowed": list(error.allowed)},
    )


def gap_stale() -> ApiError:
    return ApiError(409, "gap_stale", "The gap changed since it was read. Reload it.")


def answer_invalid(error: AnswerInvalidError) -> ApiError:
    return ApiError(
        400, "answer_invalid", "The answer is not valid.", details={"field": error.field, "reason": error.reason}
    )


def one_answer_needed() -> ApiError:
    return answer_invalid(AnswerInvalidError(field="answer", reason="one_of_option_or_text"))


def noise_rule_not_found() -> ApiError:
    return ApiError(404, "noise_rule_not_found", "The noise rule does not exist.")


def gap_run_not_found() -> ApiError:
    return ApiError(404, "gap_run_not_found", "The gap run does not exist.")


def _runner(runtime: Runtime) -> GapRunnerReads:
    if runtime.gaps is None:
        log_event(logger, WARNING, "api.gap.unavailable")
        raise gap_unavailable()
    runner: GapRunnerReads = runtime.gaps
    return runner


def _lineage(document: str | None) -> str | None:
    """The graph's document id of the ``document`` filter: an uploaded document's id (a UUID, as the documents routes
    name it) becomes its lineage ``doc:<id>``; any other value is taken as a lineage id as it is."""

    if document is None:
        return None
    try:
        return document_lineage(uuid.UUID(document))
    except ValueError:
        return document


def _out(state: GapRunState) -> GapRunStatus:
    return GapRunStatus(**{item.name: getattr(state, item.name) for item in dataclasses.fields(state)})


def _refused(error: AnswerInvalidError) -> ApiError:
    return invalid_cursor() if error.field == CURSOR else invalid_arguments(error.field)


@router.get("/gaps", response_model=GapPage)
async def list_gaps(
    pid: uuid.UUID, runtime: RuntimeDep, user: UserDep, query: Annotated[GapListQuery, Query()]
) -> GapPage:
    """The project's gaps: open, answered and applying ones unless ``status`` says otherwise; blockers first, then
    newest first. ``about`` keeps gaps about the spec ids (aliases resolve), ``story_id`` those about the nodes the
    story's tasks implement, ``document`` those citing or about the document lineage. ``held_back=false`` is the
    inbox's default view: it leaves out the new questions a run held back beyond its limit until the next run."""

    await require_project(runtime, user.id, pid)
    service = await _runner(runtime).service()
    try:
        return await service.list_gaps(str(pid), **{**query.model_dump(), "document": _lineage(query.document)})
    except AnswerInvalidError as error:
        raise _refused(error) from None


@router.get("/gaps/noise_rules", response_model=NoiseRuleList)
async def list_noise_rules(pid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> NoiseRuleList:
    """The sections, documents and patterns a member marked "not spec"; the relevance filter treats them as tracking."""

    await require_project(runtime, user.id, pid)
    service = await _runner(runtime).service()
    return NoiseRuleList(rules=list(await service.noise_rules(str(pid))))


@router.post("/gaps/noise_rules", status_code=201, response_model=NoiseRuleCreated)
async def add_noise_rule(pid: uuid.UUID, body: NoiseRuleBody, runtime: RuntimeDep, user: UserDep) -> NoiseRuleCreated:
    """Add a noise rule; the next gap run's relevance filter applies it. A pattern must compile."""

    await require_project(runtime, user.id, pid)
    service = await _runner(runtime).service()
    try:
        rule_id = await service.add_noise_rule(str(pid), body.scope, body.value, body.reason, str(user.id))
    except AnswerInvalidError as error:
        raise answer_invalid(error) from None
    log_event(logger, INFO, "api.gap.noise_rule_added", user_id=str(user.id), project_id=str(pid), rule_id=rule_id)
    return NoiseRuleCreated(rule_id=rule_id)


@router.delete("/gaps/noise_rules/{rule_id}", status_code=204)
async def remove_noise_rule(
    pid: uuid.UUID,
    rule_id: Annotated[str, Path(pattern=RULE_ID_PATTERN)],
    runtime: RuntimeDep,
    user: UserDep,
) -> Response:
    """Remove a noise rule."""

    await require_project(runtime, user.id, pid)
    service = await _runner(runtime).service()
    if not await service.remove_noise_rule(str(pid), rule_id, str(user.id)):
        raise noise_rule_not_found()
    return Response(status_code=204)


@router.post("/gaps", status_code=201, response_model=RaisedGap)
async def raise_gap(pid: uuid.UUID, body: GapRaiseBody, runtime: RuntimeDep, user: UserDep) -> RaisedGap:
    """Raise a gap by hand (origin ``person``): it is open at once and a ``site`` run adds options, the nodes an
    answer impacts and a severity. The same open question about the same ids is that gap (``created`` false)."""

    await require_project(runtime, user.id, pid)
    runner = _runner(runtime)
    service = await runner.service()
    new = NewGap(
        kind=body.kind,
        question=body.question,
        about=tuple(body.about),
        quotes=tuple(body.quotes),
        severity=body.severity,
        origin="person",
        created_by=str(user.id),
    )
    try:
        raised = await service.raise_gap(str(pid), new)
    except AnswerInvalidError as error:
        raise answer_invalid(error) from None
    if raised.created:
        await runner.start_site(str(pid), raised.gap_id)
    log_event(
        logger,
        INFO,
        "api.gap.raised",
        user_id=str(user.id),
        project_id=str(pid),
        gap_id=raised.gap_id,
        created=raised.created,
    )
    return raised


@router.post("/gaps/apply", response_model=ApplyResult)
async def apply_answers(pid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> ApplyResult:
    """Apply the project's pending answers now ("Apply now"): one update build is requested."""

    await require_project(runtime, user.id, pid)
    runner = _runner(runtime)
    service = await runner.service()
    runner.applied(str(pid))  # before the apply: an answer recorded during it schedules its own apply
    try:
        result = await service.apply_now(str(pid), by=str(user.id))
    except GapStateError as error:
        raise gap_wrong_state(error) from None
    log_event(
        logger,
        INFO,
        "api.gap.applied",
        user_id=str(user.id),
        project_id=str(pid),
        gaps=len(result.gap_ids),
        build=result.build,
    )
    return result


@router.get("/gaps/{gap_id}", response_model=GapView)
async def get_gap(pid: uuid.UUID, gap_id: GapId, runtime: RuntimeDep, user: UserDep) -> GapView:
    """One gap with its options, history and reach."""

    await require_project(runtime, user.id, pid)
    service = await _runner(runtime).service()
    try:
        return await service.get_gap(str(pid), gap_id)
    except GapNotFoundError:
        raise gap_not_found() from None


@router.post("/gaps/{gap_id}/edits_preview", response_model=EditPreview)
async def preview_edits(
    pid: uuid.UUID, gap_id: GapId, body: GapEditsPreviewBody, runtime: RuntimeDep, user: UserDep
) -> EditPreview:
    """The edits an answer would make, each target section before and after; nothing is stored. Own text is turned
    into edits by the model (``drafted``), or goes to the clarifications document as written (``fallback``)."""

    await require_project(runtime, user.id, pid)
    service = await _runner(runtime).service()
    if not body.one_answer:
        raise one_answer_needed()
    try:
        return await service.preview_edits(
            str(pid), gap_id, option_rank=body.option_rank, own_text=body.own_text, placement=body.placement
        )
    except GapNotFoundError:
        raise gap_not_found() from None
    except GapStateError as error:
        raise gap_wrong_state(error) from None
    except AnswerInvalidError as error:
        raise answer_invalid(error) from None


@router.post("/gaps/{gap_id}/answer", response_model=AnswerResult)
async def answer_gap(
    pid: uuid.UUID, gap_id: GapId, body: GapAnswerBody, runtime: RuntimeDep, user: UserDep
) -> AnswerResult:
    """Answer a gap with an option or own text: a pending clarification and its amendments; the answers of the
    project are applied after the quiet period (or at once for a development gap that blocks its story)."""

    await require_project(runtime, user.id, pid)
    runner = _runner(runtime)
    service = await runner.service()
    if not body.one_answer:
        raise one_answer_needed()
    answer = AnswerInput(
        gap_id=gap_id,
        option_rank=body.option_rank,
        own_text=body.own_text,
        placement=body.placement,
        edits=None if body.edits is None else tuple(body.edits),
        answered_by=str(user.id),
        updated_at=body.updated_at,
    )
    try:
        result = await service.answer_gap(str(pid), answer)
    except GapNotFoundError:
        raise gap_not_found() from None
    except GapStateError as error:
        raise gap_wrong_state(error) from None
    except StaleGapError:
        raise gap_stale() from None
    except AnswerInvalidError as error:
        raise answer_invalid(error) from None
    if not result.immediate:
        runner.schedule_apply(str(pid), result.apply_at)
    log_event(
        logger,
        INFO,
        "api.gap.answered",
        user_id=str(user.id),
        project_id=str(pid),
        gap_id=gap_id,
        clr_id=result.clr_id,
        placement=result.placement,
        amendments=len(result.amendments),
        immediate=result.immediate,
    )
    return result


@router.post("/gaps/{gap_id}/dismiss", status_code=204)
async def dismiss_gap(
    pid: uuid.UUID, gap_id: GapId, body: GapDismissBody, runtime: RuntimeDep, user: UserDep
) -> Response:
    """Close an open gap that is not one: the node goes, its signature stays quiet until its inputs change."""

    await require_project(runtime, user.id, pid)
    service = await _runner(runtime).service()
    try:
        await service.dismiss_gap(str(pid), gap_id, reason=body.reason, by=str(user.id))
    except GapNotFoundError:
        raise gap_not_found() from None
    except GapStateError as error:
        raise gap_wrong_state(error) from None
    except AnswerInvalidError as error:
        raise answer_invalid(error) from None
    log_event(logger, INFO, "api.gap.dismissed", user_id=str(user.id), project_id=str(pid), gap_id=gap_id)
    return Response(status_code=204)


@router.post("/gap/runs", status_code=202, response_model=GapRunStatus)
async def start_gap_run(pid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> GapRunStatus:
    """Queue a full gap run on the latest finished build (a run already queued or running is joined)."""

    await require_project(runtime, user.id, pid)
    runner = _runner(runtime)
    project_id = str(pid)
    try:
        state = await runner.start_full(project_id)
    except NoGraphBuildError:
        raise no_graph_build() from None
    log_event(
        logger,
        INFO,
        "api.gap.requested",
        user_id=str(user.id),
        project_id=project_id,
        mode="full",
        status=state.status,
        queued=state.queued,
    )
    return _out(state)


@router.get("/gap/runs/latest", response_model=GapRunStatus)
async def latest_gap_run(pid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> GapRunStatus:
    """The project's running or latest gap run, with the latest finished run's result."""

    await require_project(runtime, user.id, pid)
    return _out(await _runner(runtime).status(str(pid)))


@router.get("/gap/runs/{run_id}/noise", response_model=NoiseReport)
async def gap_run_noise(
    pid: uuid.UUID,
    run_id: Annotated[str, Path(pattern=RUN_ID_PATTERN)],
    runtime: RuntimeDep,
    user: UserDep,
) -> NoiseReport:
    """The sections a finished run judged tracking or mixed, and the nodes they removed from structural seeds."""

    await require_project(runtime, user.id, pid)
    report = await _runner(runtime).noise(str(pid), run_id)
    if report is None:
        raise gap_run_not_found()
    return report
