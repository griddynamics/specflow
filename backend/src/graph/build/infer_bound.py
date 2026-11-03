"""Inference limits by mode and the persisted queue."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, NamedTuple

from graph.build.models import BacklogQueue, RuleLimit
from graph.core.ontology import InferenceRule, InferenceSpec

CHUNK: Final = 100
"""Fresh judge calls per chunk: judge memory is written after every ``CHUNK`` completed calls, and subjects are admitted
(fragment texts read, input counted) ``CHUNK`` at a time.
"""


@dataclass(frozen=True)
class RuleLimits:
    """One rule's bounds in one build: ``cap`` (every mode); in ``first`` and ``rebuild`` builds
    ``first_cap`` (never below ``cap``) and ``budget``, the build's inference input in estimated tokens
    (None: unbounded). ``first_cap`` None: not a ``first`` or ``rebuild`` build."""

    cap: int
    first_cap: int | None = None
    budget: int | None = None


def limits_of(spec: InferenceSpec, rule: InferenceRule, *, first: bool) -> RuleLimits:
    """The bounds of ``rule`` (``InferenceSpec.cap_of``, ``first_cap_of`` and ``first_budget``);
    ``first``: a ``first`` or ``rebuild`` build."""

    cap = spec.cap_of(rule)
    if not first:
        return RuleLimits(cap)
    return RuleLimits(cap, spec.first_cap_of(rule), spec.first_budget)


def stop(limits: RuleLimits, position: int, tokens: int, spent: int) -> RuleLimit | None:
    """None when the fresh subject at ``position`` (0-based, judging order) is judged, else the limit that stops the
    rule.
    """

    if position < limits.cap:
        return None
    if limits.first_cap is None or position >= limits.first_cap:
        return "first_cap" if limits.first_cap is not None and limits.first_cap > limits.cap else "cap"
    if limits.budget is not None and spent + tokens > limits.budget:
        return "budget"
    return None


class Bounded(NamedTuple):
    """What ``bound`` decided: the subjects judged (a prefix of the order), the build's spent input
    after them and the limit that stopped the rule (None: no fresh subject left)."""

    taken: int
    spent: int
    limit: RuleLimit | None


def bound(tokens: Sequence[int], limits: RuleLimits, *, spent: int = 0) -> Bounded:
    """``stop`` over fresh subjects in judging order, ``tokens`` their counted input each; ``spent``:
    the build's counted input before this rule."""

    for position, cost in enumerate(tokens):
        limit = stop(limits, position, cost, spent)
        if limit is not None:
            return Bounded(position, spent, limit)
        spent += cost
    return Bounded(len(tokens), spent, None)


def judging_parts[T](
    changed: Sequence[T], queued: Sequence[T], new: Sequence[T], *, cap: int, update: bool
) -> list[list[T]]:
    """The judging order in its parts: the head of (1) ``changed``, (2) ``queued``, the rest of (1), (3) ``new``."""

    reserved = min(len(queued), cap // 2) if update and queued and len(changed) * 2 > cap else 0
    head = max(cap - reserved, 0)
    return [list(changed[:head]), list(queued), list(changed[head:]), list(new)]


def judging_order[T](changed: Sequence[T], queued: Sequence[T], new: Sequence[T], *, cap: int, update: bool) -> list[T]:
    """``judging_parts`` in one list."""

    return [item for part in judging_parts(changed, queued, new, cap=cap, update=update) for item in part]


def next_queue(
    left: Iterable[str],
    failed: Iterable[str],
    previous: BacklogQueue | None,
    *,
    build_id: str,
    waiting: Iterable[str] = (),
) -> BacklogQueue | None:
    """The rule's queue after this build: ``failed`` (judgements that failed, in judging order) first, then ``left``
    (the fresh subjects a limit left, in judging order), then ``waiting`` (a build whose diff changed nothing: subjects
    judged before whose inputs changed, not judged by it), each ``spec_id`` once; None when empty.
    """

    spec_ids = tuple(dict.fromkeys((*failed, *left, *waiting)))
    if not spec_ids:
        return None
    if previous is not None and previous.spec_ids:
        return BacklogQueue(
            spec_ids=spec_ids, since_build=previous.since_build or build_id, age_builds=previous.age_builds + 1
        )
    return BacklogQueue(spec_ids=spec_ids, since_build=build_id, age_builds=1)


def carried(previous: BacklogQueue | None) -> BacklogQueue | None:
    """A failed rule's queue: the previous one as it was, one build older (None when empty)."""

    if previous is None or not previous.spec_ids:
        return None
    return previous.model_copy(update={"age_builds": previous.age_builds + 1})
