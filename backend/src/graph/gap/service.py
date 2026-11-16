"""``GapService``: the reads of the Gaps inbox, the routes and the plan's findings port (docs/graph/gap/api.md "Gap
service"; lifecycle.md "Plan link").

Session 05 implements the read calls: ``list_gaps``, ``get_gap``, ``open_counts`` and ``findings``. Answers,
dismissals, raising gaps and noise rules come with later sessions. Every read binds and filters the project id
(FR-GOWN-02); spec ids asked for resolve through aliases (the core alias read), so a merged or rekeyed node's old id
still finds its gaps. Reach is computed at read time (trace from the ``ABOUT`` and ``IMPACTS`` nodes along
``affects`` to depth 2, and the plan's tasks and stories of all of them), never stored.

``open_counts`` and ``findings`` satisfy the plan's ``FindingsReads`` structurally: the plan imports neither
``graph.gap`` nor this module.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, TypedDict, Unpack, get_args

from pydantic import ValidationError

from graph.core.identity import validate_project_id
from graph.core.ontology import Ontology
from graph.core.ontology.compiled import BUILD_OWNER
from graph.core.store import Executor, as_executor
from graph.gap.errors import AnswerInvalidError, GapNotFoundError
from graph.gap.gaps import StoredGap, read_gaps
from graph.gap.models import (
    ClarificationView,
    GapFinding,
    GapFindingPage,
    GapHistory,
    GapPage,
    GapView,
    NodeRef,
    OptionRow,
    Quote,
    Reach,
    WitnessView,
)
from graph.gap.names import (
    GRAPH_STATUSES,
    OPEN_STATUSES,
    RUN_ACTOR,
    GapKind,
    GapOrigin,
    GapRoute,
    GapSeverity,
    GapStatus,
)
from graph.gap.ports import Amendments, BuildRequests, GapRecords, Investigator, PlanView, StoryHolds
from graph.gap.resolve import resolve_spec_ids
from graph.gap.rules import GapRules
from graph.gap.settings import GapSettings

PAGE_LIMITS: Final = (1, 200)
REACH_DEPTH: Final = 2
TRACE_IDS: Final = 50
SEVERITY_ORDER: Final[dict[str, int]] = {"blocker": 0, "major": 1, "minor": 2}
CURRENT_CLARIFICATIONS: Final = frozenset({"pending", "applied"})
YES: Final = "yes"
CURSOR_PARTS: Final = 3

Position = tuple[int, int, str]


def _instant(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if hasattr(value, "to_native"):
        return _instant(value.to_native())
    return _instant(datetime.fromisoformat(str(value)))


def _micros(value: Any) -> int:
    return int(_instant(value).timestamp() * 1_000_000)


def position(gap: StoredGap) -> Position:
    """Inbox order: severity (blocker, major, minor), newest first, then gap id."""

    severity = SEVERITY_ORDER.get(str(gap.props.get("severity")), len(SEVERITY_ORDER))
    return (severity, -_micros(gap.props.get("created_at")), gap.gap_id)


def encode_cursor(key: Position) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(key)).encode()).decode().rstrip("=")


def decode_cursor(token: str) -> Position:
    """The position a cursor names; ``AnswerInvalidError(field="cursor")`` for anything else."""

    try:
        raw = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        if isinstance(raw, list) and len(raw) == CURSOR_PARTS and isinstance(raw[2], str):
            return int(raw[0]), int(raw[1]), raw[2]
    except (binascii.Error, ValueError, TypeError):
        pass
    raise AnswerInvalidError(field="cursor", reason="invalid")


def _filter(value: str | Sequence[str] | None, field: str, allowed: Collection[str]) -> frozenset[str] | None:
    if value is None:
        return None
    values = frozenset((value,) if isinstance(value, str) else value)
    if not values or values - set(allowed):
        raise AnswerInvalidError(field=field, reason="unknown_value")
    return values


def _blocking(gap: StoredGap) -> bool:
    flag = gap.props.get("blocking")
    return flag is True or flag == YES


def _clarification(gap: StoredGap) -> ClarificationView | None:
    """The current clarification (pending or applied; the latest answered one), None when none reads."""

    current: list[ClarificationView] = []
    for props in gap.clarifications:
        if props.get("status") not in CURRENT_CLARIFICATIONS:
            continue
        basis = props.get("basis")
        try:
            loaded = json.loads(basis) if isinstance(basis, str) else basis or ()
            current.append(
                ClarificationView(
                    clr_id=props["clr_id"],
                    mode=props["mode"],
                    placement=props["placement"],
                    status=props["status"],
                    option_label=props.get("option_label"),
                    text=props.get("text"),
                    answered_by=props["answered_by"],
                    answered_at=_instant(props["answered_at"]),
                    applied_build_id=props.get("applied_build_id"),
                    basis=tuple(Quote.model_validate(item) for item in loaded),
                    confidence=props.get("confidence"),
                )
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            continue
    return max(current, key=lambda item: item.answered_at, default=None)


def gap_view(
    gap: StoredGap,
    *,
    options: Sequence[OptionRow] = (),
    history: GapHistory | None = None,
    reach: Reach | None = None,
) -> GapView:
    """A stored gap as reads return it."""

    props = gap.props
    resolved = props.get("resolved_at")
    return GapView(
        gap_id=gap.gap_id,
        kind=props["kind"],
        severity=props["severity"],
        status=props["status"],
        origin=props.get("origin") or "analysis",
        route=props.get("route") or "ask",
        resolution=props.get("resolution"),
        title=str(props.get("title") or ""),
        question=str(props.get("question") or ""),
        why=str(props.get("why") or ""),
        detector=str(props.get("detector") or ""),
        signature=str(props.get("signature") or ""),
        blocking=_blocking(gap),
        run_id=props.get("run_id"),
        created_by=str(props.get("created_by") or RUN_ACTOR),
        created_at=_instant(props["created_at"]),
        updated_at=_instant(props.get("updated_at") or props["created_at"]),
        resolved_at=None if resolved is None else _instant(resolved),
        about=tuple(NodeRef(spec_id=link.spec_id, label=link.label, name=link.name) for link in gap.about),
        impacts=tuple(NodeRef(spec_id=link.spec_id, label=link.label, name=link.name) for link in gap.impacts),
        witness=tuple(
            WitnessView(
                fragment_id=cite.fragment_id, quote=cite.quote, line_start=cite.line_start, line_end=cite.line_end
            )
            for cite in gap.cites
        ),
        options=tuple(options),
        clarification=_clarification(gap),
        history=history,
        reach=reach,
    )


def gap_finding(gap: StoredGap) -> GapFinding:
    """An open gap as the plan's ``FindingLike`` (lifecycle.md "Plan link")."""

    props = gap.props
    return GapFinding(
        finding_id=gap.gap_id,
        kind=str(props["kind"]),
        text=str(props.get("question") or ""),
        blocking=props.get("severity") == "blocker" or _blocking(gap),
        about_spec_ids=gap.about_ids,
        story_id=props.get("story_id"),
        created_by=str(props.get("created_by") or RUN_ACTOR),
        created_at=_instant(props["created_at"]),
        orphaned=not gap.about,
    )


class GapFilters(TypedDict, total=False):
    """The filters of ``GapService.list_gaps`` (keyword arguments)."""

    status: str | Sequence[str] | None
    kind: str | Sequence[str] | None
    severity: str | Sequence[str] | None
    origin: str | Sequence[str] | None
    route: str | Sequence[str] | None
    about: Sequence[str] | None
    story_id: str | None
    document: str | None


@dataclass
class GapService:
    """Gap reads (session 05) over the graph store (``executor``), the core reader (``reader``, for reach) and the
    relational ports. ``plan`` (``PlanView``) adds the story filter and the stories and tasks of reach; ``clock``
    gives UTC instants to the write calls of later sessions. Built as
    ``GapService(executor, reader, ontology, rules, records, amendments, *, gateway, investigator, holds, builds,
    settings, plan, clock)``."""

    executor: Any
    reader: Any
    ontology: Ontology
    rules: GapRules
    records: GapRecords
    amendments: Amendments
    gateway: Any = field(default=None, kw_only=True)
    investigator: Investigator | None = field(default=None, kw_only=True)
    holds: StoryHolds | None = field(default=None, kw_only=True)
    builds: BuildRequests | None = field(default=None, kw_only=True)
    settings: GapSettings | None = field(default=None, kw_only=True)
    plan: PlanView | None = field(default=None, kw_only=True)
    clock: Callable[[], datetime] | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        self._executor: Executor = as_executor(self.executor, None)
        self._reader = self.reader
        self._ontology = self.ontology
        self._records = self.records
        self._plan = self.plan
        self._settings = self.settings or GapSettings()
        self._clock: Callable[[], datetime] = self.clock or (lambda: datetime.now(UTC))
        self._labels = tuple(label for label in self.ontology.labels if self.ontology.owner_of(label) == BUILD_OWNER)

    async def list_gaps(
        self,
        project_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        **filters: Unpack[GapFilters],
    ) -> GapPage:
        """A page of gaps (no history, options or reach: ``get_gap`` has them), by severity (blocker, major, minor),
        newest first, then gap id. Filters combine; ``status`` defaults to the open statuses (open, answered,
        applying); ``about`` matches the gaps ``ABOUT`` any of the ids (aliases resolved); ``story_id`` the gaps
        ``ABOUT`` a node the story's tasks implement (an empty page without a plan); ``document`` (a document id)
        the gaps citing a fragment of it or ``ABOUT`` a node with evidence in it. ``AnswerInvalidError`` names a bad
        filter value, ``limit`` (1 to 200) or ``cursor``."""

        project_id = validate_project_id(project_id)
        if not PAGE_LIMITS[0] <= limit <= PAGE_LIMITS[1]:
            raise AnswerInvalidError(field="limit", reason="out_of_range")
        start = None if cursor is None else decode_cursor(cursor)
        statuses = _filter(filters.get("status"), "status", get_args(GapStatus)) or frozenset(OPEN_STATUSES)
        kinds = _filter(filters.get("kind"), "kind", get_args(GapKind))
        severities = _filter(filters.get("severity"), "severity", get_args(GapSeverity))
        origins = _filter(filters.get("origin"), "origin", get_args(GapOrigin))
        routes = _filter(filters.get("route"), "route", get_args(GapRoute))
        about, story_id, document = filters.get("about"), filters.get("story_id"), filters.get("document")
        gaps = [
            gap
            for gap in await read_gaps(self._executor, project_id, self._ontology, statuses=sorted(statuses))
            if (kinds is None or gap.props.get("kind") in kinds)
            and (severities is None or gap.props.get("severity") in severities)
            and (origins is None or gap.origin in origins)
            and (routes is None or gap.props.get("route") in routes)
            and (document is None or document in gap.documents)
        ]
        if about is not None:
            wanted = await self._current(project_id, about)
            gaps = [gap for gap in gaps if wanted & set(gap.about_ids)]
        if story_id is not None:
            gaps = await self._of_story(project_id, story_id, gaps)
        ordered = [gap for gap in sorted(gaps, key=position) if start is None or position(gap) > start]
        page = ordered[:limit]
        return GapPage(
            gaps=tuple(gap_view(gap) for gap in page),
            next=encode_cursor(position(page[-1])) if len(ordered) > limit else None,
        )

    async def get_gap(self, project_id: str, gap_id: str) -> GapView:
        """The gap with its options, history (events and follow-up chain) and reach; ``GapNotFoundError`` when the
        project has no such gap in the graph."""

        project_id = validate_project_id(project_id)
        found = await read_gaps(self._executor, project_id, self._ontology, statuses=GRAPH_STATUSES, gap_ids=[gap_id])
        if not found:
            raise GapNotFoundError(gap_id=gap_id)
        gap = found[0]
        options = await self._records.options(project_id, gap_id)
        events = await self._records.events(project_id, gap_id)
        return gap_view(
            gap,
            options=options,
            history=GapHistory(events=tuple(events), follow_ups=gap.follow_ups),
            reach=await self.reach(project_id, gap),
        )

    async def reach(self, project_id: str, gap: StoredGap) -> Reach:
        """Stories and tasks behind the gap's ``ABOUT`` and ``IMPACTS`` nodes and the nodes trace reaches from them
        (``affects``, depth 2); empty without a plan."""

        if self._plan is None:
            return Reach()
        ids = list(dict.fromkeys((*gap.about_ids, *gap.impact_ids)))
        reached: list[str] = []
        if ids and self._reader is not None:
            result = await self._reader.trace(project_id, ids[:TRACE_IDS], direction="affects", depth=REACH_DEPTH)
            reached = [node.spec_id for node in result.nodes if node.spec_id not in ids]
        every = [*ids, *reached]
        if not every:
            return Reach()
        tasks = await self._plan.tasks_for(project_id, every)
        stories = await self._plan.stories_for(project_id, ids)
        found_tasks = {task.task_id for refs in tasks.values() for task in refs}
        found_stories = {task.story_id for refs in tasks.values() for task in refs if task.story_id}
        found_stories |= {story for refs in stories.values() for story in refs}
        return Reach(stories=tuple(sorted(found_stories)), tasks=tuple(sorted(found_tasks)))

    async def open_counts(self, project_id: str, spec_ids: Sequence[str]) -> dict[str, int]:
        """Open gaps (open, answered, applying) ``ABOUT`` each asked id (aliases resolve; keys as asked)."""

        project_id = validate_project_id(project_id)
        ids = tuple(dict.fromkeys(spec_ids))
        if not ids:
            return {}
        resolved = await resolve_spec_ids(self._executor, project_id, ids, self._labels)
        names = {
            spec_id: {spec_id} | ({resolved[spec_id].spec_id} if spec_id in resolved else set()) for spec_id in ids
        }
        gaps = await read_gaps(self._executor, project_id, self._ontology, statuses=OPEN_STATUSES)
        return {spec_id: sum(1 for gap in gaps if names[spec_id] & set(gap.about_ids)) for spec_id in ids}

    async def findings(
        self, project_id: str, *, spec_ids: Sequence[str] | None = None, limit: int = 50
    ) -> GapFindingPage:
        """The open gaps (about any of ``spec_ids``, aliases resolved, when given) as findings: blocking first, then
        newest first, at most ``limit`` (1 to 200)."""

        project_id = validate_project_id(project_id)
        if not PAGE_LIMITS[0] <= limit <= PAGE_LIMITS[1]:
            raise AnswerInvalidError(field="limit", reason="out_of_range")
        gaps = await read_gaps(self._executor, project_id, self._ontology, statuses=OPEN_STATUSES)
        if spec_ids is not None:
            wanted = await self._current(project_id, spec_ids)
            gaps = tuple(gap for gap in gaps if wanted & set(gap.about_ids))
        found = sorted(
            (gap_finding(gap) for gap in gaps),
            key=lambda item: (not item.blocking, -int(item.created_at.timestamp() * 1_000_000), item.finding_id),
        )
        return GapFindingPage(findings=tuple(found[:limit]))

    async def _current(self, project_id: str, spec_ids: Sequence[str]) -> set[str]:
        """The ids asked for and the current ids they resolve to."""

        ids = tuple(dict.fromkeys(spec_ids))
        resolved = await resolve_spec_ids(self._executor, project_id, ids, self._labels) if ids else {}
        return {*ids, *(ref.spec_id for ref in resolved.values())}

    async def _of_story(self, project_id: str, story_id: str, gaps: Sequence[StoredGap]) -> list[StoredGap]:
        if self._plan is None:
            return []
        ids = sorted({spec_id for gap in gaps for spec_id in gap.about_ids})
        stories = await self._plan.stories_for(project_id, ids) if ids else {}
        nodes = {spec_id for spec_id, found in stories.items() if story_id in found}
        return [gap for gap in gaps if nodes & set(gap.about_ids)]
