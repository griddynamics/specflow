"""One planner answer: reuse, model call, validation, one repair round and fallback.

A stored answer is reused when its reuse key matches: in every mode an answer the same run stored (a
resume repeats no call), and in ``first`` and ``update`` any stored answer. Otherwise the planner is
called on the ``extraction`` call class. Each answer first gets the call's deterministic repairs
(``normalise``, graph.plan.normalise), then its checks; an invalid answer is sent back once with its
problems, each explained in words (``max_attempts`` calls in all). The invalid and fallback logs count
the problem codes. An answer that stays invalid, or a call that fails with a non-fatal
``LLMError``, ends in ``None``: the caller applies its fallback, which is never stored.
``LLMFatalError`` and ``InfrastructureUnavailableError`` propagate.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from graph.core.errors import LLMError, LLMFatalError
from graph.plan.models import PlannerOutcome
from graph.plan.names import PlanMode
from graph.plan.options import PlanContext
from graph.plan.ports import PlanAnswerKind
from graph.plan.prompts import gateway_prompt, repair_messages
from graph.plan.validate import problem_counts
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.plan")


@dataclass(frozen=True)
class PlannerCall[AnswerT: BaseModel]:
    """One planner answer to obtain: ``key`` names it in the store (an area, part or the project)."""

    kind: PlanAnswerKind
    key: str
    reuse_key: str
    messages: Sequence[dict[str, Any]]
    answer_type: type[AnswerT]
    validate: Callable[[AnswerT], list[str]]
    normalise: Callable[[AnswerT], AnswerT] | None = None


async def run_planner[AnswerT: BaseModel](
    context: PlanContext, call: PlannerCall[AnswerT], *, mode: PlanMode
) -> tuple[AnswerT | None, PlannerOutcome]:
    """The valid answer (reused or from the model) and how it came about; ``None`` asks for the fallback."""

    if context.run_id is None:
        raise ValueError("run_planner needs a context bound to a run")
    reused = await _reused(context, call, mode=mode)
    if reused is not None:
        return reused, PlannerOutcome(reused=True)
    messages = list(call.messages)
    calls = invalid = 0
    problems: list[str] = []
    for _ in range(context.options.max_attempts):
        calls += 1
        try:
            answer = await context.gateway.structured(
                "extraction", None, messages, call.answer_type, prompt=gateway_prompt(call.kind)
            )
        except LLMFatalError:
            raise
        except LLMError as error:
            _log_fallback(call, "llm_error", calls=calls, invalid=invalid, code=error.code)
            return None, PlannerOutcome(calls=calls, fallback=True, invalid=invalid, problems=(error.code,))
        answer = _normalised(call, answer)
        problems = call.validate(answer)
        if not problems:
            await context.store.put_answer(
                context.project_id,
                call.kind,
                call.key,
                call.reuse_key,
                context.run_id,
                payload=answer.model_dump(mode="json"),
            )
            return answer, PlannerOutcome(calls=calls, invalid=invalid)
        invalid += 1
        log_event(
            logger,
            INFO,
            "graph.plan.planner_invalid",
            planner=call.kind,
            key=call.key,
            problems=len(problems),
            codes=dict(problem_counts(problems)),
        )
        messages = repair_messages(call.messages, answer.model_dump_json(), problems)
    _log_fallback(call, "invalid_answer", calls=calls, invalid=invalid, problems=problems)
    return None, PlannerOutcome(calls=calls, fallback=True, invalid=invalid, problems=tuple(problems))


async def _reused[AnswerT: BaseModel](
    context: PlanContext, call: PlannerCall[AnswerT], *, mode: PlanMode
) -> AnswerT | None:
    store, project_id = context.store, context.project_id
    payload = await store.get_answer(project_id, call.kind, call.key, call.reuse_key, run_id=context.run_id)
    if payload is None and mode != "replan":
        payload = await store.get_answer(project_id, call.kind, call.key, call.reuse_key)
    if payload is None:
        return None
    try:
        answer = call.answer_type.model_validate(payload)
    except ValidationError:
        return None
    answer = _normalised(call, answer)
    return answer if not call.validate(answer) else None


def _normalised[AnswerT: BaseModel](call: PlannerCall[AnswerT], answer: AnswerT) -> AnswerT:
    return call.normalise(answer) if call.normalise is not None else answer


def _log_fallback(
    call: PlannerCall[Any],
    cause: str,
    *,
    calls: int,
    invalid: int,
    code: str | None = None,
    problems: Sequence[str] = (),
) -> None:
    log_event(
        logger,
        WARNING,
        "graph.plan.planner_fallback",
        planner=call.kind,
        key=call.key,
        cause=cause,
        calls=calls,
        invalid=invalid,
        code=code,
        codes=dict(problem_counts(problems)),
    )
