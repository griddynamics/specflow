"""The gaps of a project for people (docs/graph/gap/api.md "Web routes"): the reads the canvas polls.

``GET /gaps`` is ``GapService.list_gaps`` and ``GET /gap/runs/latest`` the running or latest gap run with the latest
finished run's result (the noise report ships inside it). Project membership is the access rule: a non-member gets
``404 project_not_found``, as on every project route. ``503 gap_unavailable`` while gap runs are not configured (no
graph build settings, or ``GAP_ENABLED`` off). Logs carry ids, counts and codes only (NFR-GSEC-02).

The writes go through the service layer (``api.gap.answering.record_answer``, ``GapService``): the Slack bot
(``api.slack.app``) answers and applies, the agent routes (``api.routers.agent_gaps``, ``agent_writes``) answer,
raise and assign, and a build's follow-up run starts on its own (``api.gap.runner``).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Query

from api.deps import RuntimeDep, UserDep
from api.errors import ApiError, invalid_arguments, invalid_cursor
from api.gap.state import GapRunnerReads, GapRunState
from api.gap_models import GapListQuery, GapRunStatus
from api.graph_build.corpus import document_lineage, lineage_of
from api.routers.documents import require_project
from api.runtime import Runtime
from db import sources as sources_repo
from graph.gap import AnswerInvalidError, GapPage
from graph.gap.assignment import NO_ASSIGNEE
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("api.routers.gaps")
router = APIRouter(prefix="/projects/{pid}", tags=["gaps"])

CURSOR: Final = "cursor"
ME: Final = "me"


def gap_unavailable() -> ApiError:
    return ApiError(503, "gap_unavailable", "Gap runs are not configured.")


def _runner(runtime: Runtime) -> GapRunnerReads:
    if runtime.gaps is None:
        log_event(logger, WARNING, "api.gap.unavailable")
        raise gap_unavailable()
    runner: GapRunnerReads = runtime.gaps
    return runner


async def _lineage(runtime: Runtime, pid: uuid.UUID, document: str | None) -> str | None:
    """The graph's document id of the ``document`` filter: a document's id (a UUID, as the documents routes name it)
    becomes its corpus lineage (``drive:<connection>:<file>`` for a Drive document, else ``doc:<id>``); any other
    value is taken as a lineage id as it is."""

    if document is None:
        return None
    try:
        document_id = uuid.UUID(document)
    except ValueError:
        return document
    async with runtime.transaction() as session:
        rows = await sources_repo.graph_corpus_document(session, pid, document_id)
    return lineage_of(rows) or document_lineage(document_id)


def _out(state: GapRunState) -> GapRunStatus:
    return GapRunStatus(**{item.name: getattr(state, item.name) for item in dataclasses.fields(state)})


def _refused(error: AnswerInvalidError) -> ApiError:
    return invalid_cursor() if error.field == CURSOR else invalid_arguments(error.field)


def _assignee(value: str | None, caller: uuid.UUID) -> str | None:
    """The ``assigned_to`` filter as ``list_gaps`` takes it: ``me`` is the caller's user id, ``none`` stays, a user id
    is written as the users table writes it."""

    if value == ME:
        return str(caller)
    if value is None or value == NO_ASSIGNEE:
        return value
    return str(uuid.UUID(value))


@router.get("/gaps", response_model=GapPage)
async def list_gaps(
    pid: uuid.UUID, runtime: RuntimeDep, user: UserDep, query: Annotated[GapListQuery, Query()]
) -> GapPage:
    """The project's gaps: open, answered and applying ones unless ``status`` says otherwise; blockers first, then
    newest first. ``about`` keeps gaps about the spec ids (aliases resolve), ``story_id`` those about the nodes the
    story's tasks implement, ``document`` those citing or about the document lineage. ``assigned_to`` keeps the gaps
    of one assignee: ``me`` (the caller), ``none`` (nobody) or a member's user id."""

    await require_project(runtime, user.id, pid)
    service = await _runner(runtime).service()
    try:
        document = await _lineage(runtime, pid, query.document)
        filters = query.model_dump() | {"document": document, "assigned_to": _assignee(query.assigned_to, user.id)}
        return await service.list_gaps(str(pid), **filters)
    except AnswerInvalidError as error:
        raise _refused(error) from None


@router.get("/gap/runs/latest", response_model=GapRunStatus)
async def latest_gap_run(pid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> GapRunStatus:
    """The project's running or latest gap run, with the latest finished run's result."""

    await require_project(runtime, user.id, pid)
    return _out(await _runner(runtime).status(str(pid)))
