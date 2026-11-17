"""``GapService``: the reads of the Gaps inbox, the routes and the plan's findings port (docs/graph/gap/api.md "Gap
service"; lifecycle.md "Plan link").

The reads (``list_gaps``, ``get_gap``, ``open_counts``, ``findings``) and the writes of a person (``preview_edits``,
``answer_gap``, ``apply_now``, ``dismiss_gap``, ``raise_gap``, the noise-rule calls; answers.md). Every read binds and
filters the project id
(FR-GOWN-02); spec ids asked for resolve through aliases (the core alias read), so a merged or rekeyed node's old id
still finds its gaps. Reach is computed at read time (trace from the ``ABOUT`` and ``IMPACTS`` nodes along
``affects`` to depth 2, and the plan's tasks and stories of all of them), never stored.

``open_counts`` and ``findings`` satisfy the plan's ``FindingsReads`` structurally: the plan imports neither
``graph.gap`` nor this module.

Every write is one transaction in the graph store (the gap owned writer) and relational rows through the ports; the
relational rows the graph write depends on are written first and removed (or set back) when the graph write fails
(api.md "Gap service"), and the event rows follow the graph write (the log is append-only). A ``StaleGapError`` refuses
an answer to a gap whose ``updated_at`` moved since the caller read it. Logs carry ids, codes and counts only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from collections import defaultdict
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TypedDict, Unpack, get_args

from pydantic import ValidationError

from graph.core.errors import ConfigurationError, GraphError
from graph.core.identity import validate_project_id
from graph.core.ontology import Ontology
from graph.core.ontology.compiled import BUILD_OWNER
from graph.core.store import Executor, as_executor
from graph.core.writer import FRAGMENT, OwnedWriter
from graph.core.writer import NodeRef as WriterRef
from graph.gap.answers import (
    ANSWER_TEXT_CHARS,
    REASON_CHARS,
    AnswerGraph,
    ClarificationWrite,
    amendment_rows,
    check_edits,
    clarification_props,
    document_edit,
    draft_edits,
    noise_rule_id,
    option_of,
    place,
    section_preview,
    text_ok,
    write_clarification,
)
from graph.gap.corpus import clarifications_lineage, document_clarifications
from graph.gap.errors import AnswerInvalidError, GapNotFoundError, GapStateError, StaleGapError
from graph.gap.follow_ups import (
    APPLIED_ROWS,
    REVERTED_ROWS,
    SET_ASIDE_ROWS,
    Predecessor,
    continued,
    predecessors,
    reconcile_reason,
    settles_at_once,
)
from graph.gap.gaps import (
    StoredGap,
    StoredLink,
    answer_key,
    cite_key,
    clarification_uuid,
    gap_uuid,
    link_key,
    quote_hash,
    read_gaps,
)
from graph.gap.holds import aliases_of, hold, hold_blockers, hold_reason, release
from graph.gap.identity import clr_id, development_signature, gap_id, normalise_quote, person_signature
from graph.gap.models import (
    AiAnswer,
    AiAnswerList,
    AnswerInput,
    AnswerResult,
    ApplyResult,
    ClarificationView,
    DocumentAnswer,
    DocumentAnswers,
    Edit,
    EditPreview,
    EventRow,
    GapFinding,
    GapFindingPage,
    GapHistory,
    GapPage,
    GapView,
    HoldResult,
    LineageAnswers,
    NewGap,
    NodeRef,
    NoiseRuleRow,
    NoteRow,
    OptionRow,
    Quote,
    RaisedGap,
    Reach,
    RevertResult,
    SuppressionRow,
    WitnessView,
)
from graph.gap.names import (
    ABOUT,
    AI_ACTOR,
    CITES,
    CLARIFICATION,
    FOLLOWS_UP,
    GAP,
    GAP_PREFIX,
    GRAPH_STATUSES,
    GROUNDED_IN,
    OPEN_STATUSES,
    OWNER,
    RUN_ACTOR,
    EditPlacement,
    GapKind,
    GapOrigin,
    GapRoute,
    GapSeverity,
    GapStatus,
    NoiseScope,
)
from graph.gap.ports import Amendments, BuildRequests, GapRecords, Investigator, PlanView, StoryHolds
from graph.gap.resolve import SpecRef, resolve_spec_ids
from graph.gap.rules import GapRules
from graph.gap.settings import GapSettings
from graph.gap.steps.close import follow_up_signature
from graph.gap.steps.snapshot import heading_path_of
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger(__name__)

PAGE_LIMITS: Final = (1, 200)
REACH_DEPTH: Final = 2
TRACE_IDS: Final = 50
SEVERITY_ORDER: Final[dict[str, int]] = {"blocker": 0, "major": 1, "minor": 2}
CURRENT_CLARIFICATIONS: Final = frozenset({"pending", "applied"})
YES: Final = "yes"
CURSOR_PARTS: Final = 4
ANSWERABLE: Final = ("open", "answered", "resolved")
"""A resolved gap is answered again only while its clarification is applied (answers.md "Changing an answer")."""
APPLIED_CLARIFICATIONS: Final = frozenset({"applied", "absorbed"})
PENDING: Final = "pending"
APPLY_MARK: Final = "apply_requested_at"
"""Set on a pending clarification when its build is requested, so a later apply does not take it again (a re-answer of
a resolved gap stays ``pending`` until the close check)."""
QUESTION_CHARS: Final = (10, 1000)
NOTE_CHARS: Final = (1, 2000)
NOTE_PREFIX: Final = "NOTE::"
DEVELOPMENT: Final = "development"
BLOCKER: Final = "blocker"
DEVELOPMENT_OPEN_LIMIT: Final = 50
"""The most open development gaps one agent may hold on one story (the first slice's open-findings limit)."""
TITLE_CHARS: Final = 160
ABOUT_MAX: Final = 20
NOISE_VALUE_CHARS: Final = (1, 500)
APPLY_REASON: Final = "gap_answers"
REVERT_REASON: Final = "gap_revert"
REVERTED: Final = "reverted"
REVIEWED_MODES: Final = frozenset({"ai", "pointer"})
REVERTIBLE: Final = ("pending", "applied", "absorbed")
IN_CORPUS_ROWS: Final = frozenset({"active", "undecided"})
"""Amendment statuses whose text the build reads: a revert of one of them needs an update build."""

Position = tuple[int, int, int, str]


def _instant(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if hasattr(value, "to_native"):
        return _instant(value.to_native())
    return _instant(datetime.fromisoformat(str(value)))


def _micros(value: Any) -> int:
    return int(_instant(value).timestamp() * 1_000_000)


def position(gap: StoredGap, tasks: int = 0) -> Position:
    """Inbox order (answers.md "The question card"): severity (blocker, major, minor), then reach (``tasks``, the plan
    tasks behind the gap's ``ABOUT`` and ``IMPACTS`` nodes, most first), newest first, then gap id."""

    severity = SEVERITY_ORDER.get(str(gap.props.get("severity")), len(SEVERITY_ORDER))
    return (severity, -tasks, -_micros(gap.props.get("created_at")), gap.gap_id)


def encode_cursor(key: Position) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(key)).encode()).decode().rstrip("=")


def decode_cursor(token: str) -> Position:
    """The position a cursor names; ``AnswerInvalidError(field="cursor")`` for anything else."""

    try:
        raw = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        if isinstance(raw, list) and len(raw) == CURSOR_PARTS and isinstance(raw[3], str):
            return int(raw[0]), int(raw[1]), int(raw[2]), raw[3]
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


def _open_development(gaps: Sequence[StoredGap], new: NewGap) -> int:
    """The open development gaps the asker raised on the same story."""

    return sum(
        1
        for gap in gaps
        if gap.origin == DEVELOPMENT
        and gap.status in OPEN_STATUSES
        and gap.props.get("created_by") == new.created_by
        and gap.props.get("story_id") == new.story_id
    )


def _held_back(gap: StoredGap) -> bool:
    return gap.props.get("held_back") == YES


def clarification_view(props: Mapping[str, Any]) -> ClarificationView | None:
    """A stored clarification as reads return it, None when its properties do not read."""

    basis = props.get("basis")
    try:
        loaded = json.loads(basis) if isinstance(basis, str) else basis or ()
        return ClarificationView(
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
    except (KeyError, TypeError, ValueError, ValidationError):
        return None


def _clarification(gap: StoredGap) -> ClarificationView | None:
    """The current clarification (pending or applied; the latest answered one), None when none reads."""

    current = [
        view
        for props in gap.clarifications
        if props.get("status") in CURRENT_CLARIFICATIONS and (view := clarification_view(props)) is not None
    ]
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
        held_back=_held_back(gap),
        run_id=props.get("run_id"),
        story_id=props.get("story_id"),
        aliases=aliases_of(props),
        created_by=str(props.get("created_by") or RUN_ACTOR),
        created_at=_instant(props["created_at"]),
        updated_at=_instant(props.get("updated_at") or props["created_at"]),
        resolved_at=None if resolved is None else _instant(resolved),
        about=tuple(_node_ref(link) for link in gap.about),
        impacts=tuple(_node_ref(link) for link in gap.impacts),
        witness=tuple(
            WitnessView(
                fragment_id=cite.fragment_id,
                quote=cite.quote,
                line_start=cite.line_start,
                line_end=cite.line_end,
                document_id=cite.document_id,
            )
            for cite in gap.cites
        ),
        options=tuple(options),
        clarification=_clarification(gap),
        history=history,
        reach=reach,
    )


def _node_ref(link: StoredLink) -> NodeRef:
    return NodeRef(spec_id=link.spec_id, label=link.label, name=link.name, aliases=link.aliases)


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
    held_back: bool | None


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
        # Answers, applies and dismissals of one project run one at a time in this process: each reads the gap, then
        # writes on what it read (two answers on one gap would both commit a pending clarification).
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

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
        the gaps citing a fragment of it or ``ABOUT`` a node with evidence in it; ``held_back`` ``False`` is the inbox's
        default view (the new questions of the latest run beyond its limit are left out, FR-GRTE-06), ``True`` only
        those, None both. ``AnswerInvalidError`` names a bad filter value, ``limit`` (1 to 200) or ``cursor``."""

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
        held = filters.get("held_back")
        gaps = [
            gap
            for gap in await read_gaps(self._executor, project_id, self._ontology, statuses=sorted(statuses))
            if (kinds is None or gap.props.get("kind") in kinds)
            and (severities is None or gap.props.get("severity") in severities)
            and (origins is None or gap.origin in origins)
            and (routes is None or gap.props.get("route") in routes)
            and (document is None or document in gap.documents)
            and (held is None or _held_back(gap) == held)
        ]
        if about is not None:
            wanted = await self._current(project_id, about)
            gaps = [gap for gap in gaps if wanted & set(gap.about_ids)]
        if story_id is not None:
            gaps = await self._of_story(project_id, story_id, gaps)
        tasks = await self._tasks_behind(project_id, gaps)
        keys = {gap.gap_id: position(gap, tasks.get(gap.gap_id, 0)) for gap in gaps}
        ordered = [
            gap for gap in sorted(gaps, key=lambda item: keys[item.gap_id]) if start is None or keys[gap.gap_id] > start
        ]
        page = ordered[:limit]
        return GapPage(
            gaps=tuple(gap_view(gap) for gap in page),
            next=encode_cursor(keys[page[-1].gap_id]) if len(ordered) > limit else None,
        )

    async def _tasks_behind(self, project_id: str, gaps: Sequence[StoredGap]) -> dict[str, int]:
        """Per gap the number of plan tasks implementing its ``ABOUT`` and ``IMPACTS`` nodes (one plan read for the
        list; none without a plan)."""

        ids = list(dict.fromkeys(spec_id for gap in gaps for spec_id in (*gap.about_ids, *gap.impact_ids)))
        if self._plan is None or not ids:
            return {}
        found = await self._plan.tasks_for(project_id, ids)
        return {
            gap.gap_id: len(
                {task.task_id for spec_id in (*gap.about_ids, *gap.impact_ids) for task in found.get(spec_id, ())}
            )
            for gap in gaps
        }

    async def get_gap(self, project_id: str, gap_id: str) -> GapView:
        """The gap with its options, history (events and follow-up chain) and reach; ``gap_id`` may be an alias the
        gap keeps (a migrated first-slice finding's ``FND::`` id). ``GapNotFoundError`` when the project has no such
        gap in the graph."""

        project_id = validate_project_id(project_id)
        gap = await self._by_id_or_alias(project_id, gap_id)
        options = await self._records.options(project_id, gap.gap_id)
        events = await self._records.events(project_id, gap.gap_id)
        view = gap_view(
            gap,
            options=options,
            history=GapHistory(events=tuple(events), follow_ups=gap.follow_ups),
            reach=await self.reach(project_id, gap),
        )
        return view.model_copy(update={"witness": await self._located(project_id, view.witness)})

    async def _by_id_or_alias(self, project_id: str, gap_id: str) -> StoredGap:
        """The gap of ``gap_id``, else the gap that keeps ``gap_id`` among its ``aliases`` (lifecycle.md "Migration of
        first-slice findings"); ``GapNotFoundError`` when neither exists."""

        found = await read_gaps(self._executor, project_id, self._ontology, statuses=GRAPH_STATUSES, gap_ids=[gap_id])
        if found:
            return found[0]
        if not gap_id.startswith(GAP_PREFIX):
            every = await read_gaps(self._executor, project_id, self._ontology, statuses=GRAPH_STATUSES)
            aliased = next((gap for gap in every if gap_id in aliases_of(gap.props)), None)
            if aliased is not None:
                return aliased
        raise GapNotFoundError(gap_id=gap_id)

    async def _located(self, project_id: str, witness: Sequence[WitnessView]) -> tuple[WitnessView, ...]:
        """The witness lines with their document's title and path and their section's heading path (one documents read
        and one fragments read per cited document); a line whose fragment is gone keeps what it has."""

        documents = sorted({item.document_id for item in witness if item.document_id})
        if self._reader is None or not documents:
            return tuple(witness)
        known = {document.document_id: document for document in await self._reader.documents(project_id)}
        sections: dict[str, tuple[str, ...]] = {}
        for document_id in documents:
            for fragment in await self._reader.fragments(project_id, document_id):
                sections[fragment.fragment_id] = heading_path_of(fragment)
        located = []
        for item in witness:
            document = known.get(item.document_id or "")
            located.append(
                item.model_copy(
                    update={
                        "document_title": None if document is None else document.title,
                        "document_path": None if document is None else document.path,
                        "heading_path": sections.get(item.fragment_id, item.heading_path),
                    }
                )
            )
        return tuple(located)

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

    # Writes: answers, dismissals, raised gaps, noise rules

    async def ai_answers(self, project_id: str, *, limit: int = 50) -> AiAnswerList:
        """The review list "Answered by AI" (answers.md "Review list", FR-GANS-10): every clarification of mode
        ``ai`` or ``pointer`` with its gap, newest first (then clarification id), at most ``limit`` (1 to 200);
        reverted ones too, with the follow-up a revert opened. Nothing waits for a review."""

        project_id = validate_project_id(project_id)
        if not PAGE_LIMITS[0] <= limit <= PAGE_LIMITS[1]:
            raise AnswerInvalidError(field="limit", reason="range")
        gaps = await read_gaps(self._executor, project_id, self._ontology, statuses=GRAPH_STATUSES)
        found: list[AiAnswer] = []
        for gap in gaps:
            reverted = next(
                (
                    item.gap_id
                    for item in gap.follow_ups
                    if item.direction == "continued_by" and item.reason == REVERTED
                ),
                None,
            )
            for props in gap.clarifications:
                view = clarification_view(props) if props.get("mode") in REVIEWED_MODES else None
                if view is None:
                    continue
                found.append(
                    AiAnswer(
                        gap_id=gap.gap_id,
                        title=str(gap.props.get("title") or ""),
                        question=str(gap.props.get("question") or ""),
                        gap_status=gap.props["status"],
                        clarification=view,
                        follow_up=reverted if view.status == REVERTED else None,
                    )
                )
        found.sort(key=lambda item: (-_millis(item.clarification.answered_at), item.clarification.clr_id))
        return AiAnswerList(answers=tuple(found[:limit]))

    async def document_answers(self, project_id: str) -> DocumentAnswers:
        """The answers each document's build text holds that its source file does not (reconcile.md "Download with
        answers", FR-GWEB-05): per lineage the clarifications of its ``active`` and ``undecided`` amendments (one entry
        per clarification, with its gap and the number of its amendments there), and the sections of the generated
        clarifications document (``applied`` ones, and ``pending`` ones sent to a build). Lineages in path-free id
        order; answers by gap title, then clarification id."""

        project_id = validate_project_id(project_id)
        gaps = await read_gaps(
            self._executor, project_id, self._ontology, statuses=("answered", "applying", "resolved")
        )
        owners = {str(props.get("clr_id")): gap for gap in gaps for props in gap.clarifications}
        lineages: list[LineageAnswers] = []
        for lineage in sorted(await self.amendments.pending_lineages(project_id)):
            rows = await self.amendments.for_lineage(project_id, lineage, statuses=sorted(IN_CORPUS_ROWS))
            by_clr: dict[str, list[str]] = defaultdict(list)
            for row in rows:
                by_clr[row.clr_id].append(row.status)
            answers = [
                _document_answer(
                    clr, owners.get(clr), "undecided" if "undecided" in statuses else "active", len(statuses)
                )
                for clr, statuses in by_clr.items()
            ]
            if answers:
                lineages.append(LineageAnswers(lineage=lineage, answers=_sorted_answers(answers)))
        sections = [
            _document_answer(
                entry.clr_id, owners.get(entry.clr_id), _clarification_status(owners.get(entry.clr_id), entry.clr_id), 0
            )
            for entry in await document_clarifications(self._executor, project_id, self._ontology)
        ]
        return DocumentAnswers(
            lineages=tuple(lineages),
            clarifications_lineage=clarifications_lineage(project_id),
            clarifications=_sorted_answers(sections),
        )

    async def preview_edits(
        self,
        project_id: str,
        gap_id: str,
        *,
        option_rank: int | None = None,
        own_text: str | None = None,
        placement: EditPlacement | None = None,
    ) -> EditPreview:
        """The edits an answer would make and each target section before and after; nothing is stored. An option's
        edits are checked as they are; an own text is turned into edits by one ``gap_edits`` call, and when the call
        fails or its edits do not check the text goes to the clarifications document as written (``fallback``)."""

        project_id = validate_project_id(project_id)
        if (option_rank is None) == (own_text is None):
            raise AnswerInvalidError(field="answer", reason="one_of_option_or_text", gap_id=gap_id)
        gap = await self._answerable(project_id, gap_id)
        graph = await AnswerGraph.read(self._reader, project_id, self._ontology, gap)
        drafted, fallback = False, None
        if option_rank is not None:
            edits = option_of(await self._records.options(project_id, gap_id), option_rank, gap_id).edits
        else:
            text = _own_text(own_text, gap_id)
            found, fallback = await draft_edits(self.gateway, graph, text, placement)
            drafted = found is not None
            edits = found if found is not None else (document_edit(gap, text),)
        checked, chosen = place(gap, check_edits(graph, edits), placement)
        checked = check_edits(graph, checked)
        log_event(
            logger,
            INFO,
            "graph.gap.answer.previewed",
            project_id=project_id,
            gap_id=gap_id,
            edits=len(checked),
            drafted=drafted,
            fallback=fallback,
        )
        return EditPreview(
            gap_id=gap_id,
            placement=chosen,
            edits=checked,
            sections=tuple(section_preview(graph, edit) for edit in checked),
            drafted=drafted,
            fallback=fallback,
        )

    async def answer_gap(self, project_id: str, answer: AnswerInput) -> AnswerResult:
        """Record an answer (FR-GANS-01..04, FR-GANS-09): one ``pending`` clarification that ``ANSWERS`` the gap,
        ``PLACED_IN`` each source document it amends, one ``pending`` amendment row per amend edit; the gap moves to
        ``answered``. A pending answer of the gap is replaced (deleted with its amendments; the event log keeps it);
        an answer to a resolved gap ``REPLACES`` its applied clarification when the answers are applied. A development
        gap that blocks its story is applied at once (FR-GANS-08)."""

        project_id = validate_project_id(project_id)
        async with self._locks[project_id]:
            return await self._answer(project_id, answer)

    async def _answer(self, project_id: str, answer: AnswerInput) -> AnswerResult:
        gap = await self._answerable(project_id, answer.gap_id)
        if answer.updated_at is not None and _millis(answer.updated_at) != _millis(_updated_at(gap)):
            raise StaleGapError(gap_id=gap.gap_id)
        graph = await AnswerGraph.read(self._reader, project_id, self._ontology, gap)
        mode, label, edits = await self._answer_edits(project_id, gap, answer)
        if settles_at_once(gap, mode, edits):
            return await self._settle(project_id, gap, answer, label)
        checked, placement = place(gap, check_edits(graph, edits), answer.placement)
        checked = check_edits(graph, checked)
        now = self._clock()
        body = "\n\n".join(edit.text for edit in checked)
        clr = clr_id(gap.gap_id, body, now)
        rows = amendment_rows(graph, clr, checked, now)
        pending = [props for props in gap.clarifications if props.get("status") == PENDING]
        applied = _latest(props for props in gap.clarifications if props.get("status") in APPLIED_CLARIFICATIONS)
        replaces = applied if gap.status == "resolved" else None
        props = clarification_props(
            clr,
            mode=mode,
            placement=placement,
            option_label=label,
            text=body,
            answered_by=answer.answered_by,
            answered_at=now,
        )
        await self.amendments.put(project_id, rows)
        try:
            async with self._writer(project_id).transaction() as tx:
                write = ClarificationWrite(clr, gap, props, tuple(sorted({row.lineage for row in rows})), replaces)
                await write_clarification(tx, project_id, write)
                for old in pending:
                    await tx.delete_node(CLARIFICATION, clarification_uuid(project_id, str(old["clr_id"])))
                changed: dict[str, Any] = {"updated_at": _iso(now)}
                if gap.status != "resolved":
                    changed["status"] = "answered"
                if gap.props.get("route") == "ai":  # a person's answer takes the place of AI's
                    changed["route"] = "ask"
                await tx.upsert_node(GAP, gap_uuid(project_id, gap.gap_id), changed)
        except BaseException:
            await self.amendments.delete(project_id, [row.amendment_id for row in rows])
            raise
        events = await self._replaced(project_id, gap, pending, by=answer.answered_by, now=now)
        events.append(
            EventRow(
                gap_id=gap.gap_id,
                clr_id=clr,
                at=now,
                actor=answer.answered_by,
                event="answered",
                detail={"mode": mode, "placement": placement, "amendments": len(rows), "replaces": replaces},
            )
        )
        await self._records.log(project_id, events)
        immediate = gap.origin == "development" and _blocking(gap)
        log_event(
            logger,
            INFO,
            "graph.gap.answer.recorded",
            project_id=project_id,
            gap_id=gap.gap_id,
            clr_id=clr,
            mode=mode,
            placement=placement,
            amendments=len(rows),
            replaced=len(pending),
            immediate=immediate,
        )
        if immediate:
            try:
                await self._apply(project_id, by=answer.answered_by, immediate=True)
            except GraphError as error:  # the answer is recorded: it waits for the quiet period instead
                log_event(
                    logger, WARNING, "graph.gap.answer.apply_failed", project_id=project_id, error_code=error.code
                )
                immediate = False
        return AnswerResult(
            gap_id=gap.gap_id,
            clr_id=clr,
            replaced=str(pending[-1]["clr_id"]) if pending else None,
            placement=placement,
            amendments=tuple(row.amendment_id for row in rows),
            apply_at=now if immediate else now + timedelta(seconds=self.quiet_seconds()),
            immediate=immediate,
        )

    def quiet_seconds(self) -> int:
        """The quiet period after the last answer of a project before its answers are applied."""

        return self._settings.effective_limits(self.rules.limits).apply_quiet_seconds

    async def apply_now(self, project_id: str, *, by: str, immediate: bool = False) -> ApplyResult:
        """Apply the project's pending answers in one update build (FR-GANS-08): their amendments become ``active``,
        the gaps ``answered`` move to ``applying``, a resolved gap's applied clarification that a pending one replaces
        becomes ``superseded`` (its amendments ``reverted``), then ``BuildRequests.request_update_build``.
        ``GapStateError`` when nothing is pending; ``ConfigurationError`` without a ``BuildRequests`` port."""

        project_id = validate_project_id(project_id)
        async with self._locks[project_id]:
            return await self._apply(project_id, by=by, immediate=immediate)

    async def _apply(self, project_id: str, *, by: str, immediate: bool) -> ApplyResult:
        if self.builds is None:
            raise ConfigurationError("gap_builds_missing")
        gaps = await read_gaps(self._executor, project_id, self._ontology, statuses=("answered", "resolved"))
        batch = [
            (gap, props)
            for gap in gaps
            for props in gap.clarifications
            if props.get("status") == PENDING and not props.get(APPLY_MARK)  # a marked one waits for its build
        ]
        if not batch:
            raise GapStateError(status="nothing_pending", allowed=("answered",))
        new_ids = [str(props["clr_id"]) for _, props in batch]
        old_ids = [
            str(props["clr_id"])
            for gap, _ in batch
            if gap.status == "resolved"
            for props in gap.clarifications
            if props.get("status") in APPLIED_CLARIFICATIONS
        ]
        activate = [
            row.amendment_id
            for clr in new_ids
            for row in await self.amendments.for_clarification(project_id, clr)
            if row.status == PENDING
        ]
        revert = [
            row.amendment_id
            for clr in old_ids
            for row in await self.amendments.for_clarification(project_id, clr)
            if row.status == "active"
        ]
        by_id = {gap.gap_id: gap for gap in gaps}
        replaced = [old for gap, _ in batch for old in predecessors(gap, by_id)]
        restore = await self._revertible(project_id, replaced)
        old_ids += [old.clr_id for old in replaced]
        now = self._clock()
        moved = [gap.gap_id for gap, _ in batch if gap.status == "answered"]
        try:
            await self.amendments.set_status(project_id, activate, "active")
            await self.amendments.set_status(project_id, revert, "reverted")
            await self.amendments.set_status(project_id, [key for ids in restore.values() for key in ids], "reverted")
            async with self._writer(project_id).transaction() as tx:
                for clr in new_ids:
                    await tx.upsert_node(CLARIFICATION, clarification_uuid(project_id, clr), {APPLY_MARK: _iso(now)})
                for gap_id in dict.fromkeys(moved):
                    await tx.upsert_node(
                        GAP, gap_uuid(project_id, gap_id), {"status": "applying", "updated_at": _iso(now)}
                    )
                for clr in old_ids:
                    await tx.upsert_node(CLARIFICATION, clarification_uuid(project_id, clr), {"status": "superseded"})
        except BaseException:
            await self.amendments.set_status(project_id, activate, PENDING)
            await self.amendments.set_status(project_id, revert, "active")
            for status, ids in restore.items():
                await self.amendments.set_status(project_id, ids, status)
            raise
        events = [
            EventRow(
                gap_id=gap.gap_id,
                clr_id=str(props["clr_id"]),
                at=now,
                actor=by,
                event="applying",
                detail={"immediate": immediate, "status": gap.status if gap.status == "resolved" else "applying"},
            )
            for gap, props in batch
        ]
        events += [
            EventRow(gap_id=gap.gap_id, clr_id=str(props["clr_id"]), at=now, actor=by, event="superseded")
            for gap, _ in batch
            if gap.status == "resolved"
            for props in gap.clarifications
            if props.get("status") in APPLIED_CLARIFICATIONS
        ]
        events += [
            EventRow(
                gap_id=old.gap_id,
                clr_id=old.clr_id,
                at=now,
                actor=by,
                event="superseded",
                detail={"reason": old.reason},
            )
            for old in replaced
        ]
        await self._records.log(project_id, events)
        build = await self.builds.request_update_build(project_id, APPLY_REASON, immediate=immediate)
        log_event(
            logger,
            INFO,
            "graph.gap.answers.applied",
            project_id=project_id,
            gaps=len(set(moved)),
            clarifications=len(new_ids),
            amendments=len(activate),
            superseded=len(old_ids),
            immediate=immediate,
            build=build,
        )
        return ApplyResult(
            gap_ids=tuple(dict.fromkeys(gap.gap_id for gap, _ in batch)),
            clr_ids=tuple(new_ids),
            amendments=tuple(activate),
            build=build,
        )

    async def dismiss_gap(self, project_id: str, gap_id: str, *, reason: str, by: str) -> tuple[str, ...]:
        """Close an open gap a person judges not to be one (FR-GANS-11): its signature becomes a suppression, the
        node and its relations are deleted, its options too; the event log keeps the reason. The gap's holds end
        (``StoryHolds.release_gap``, FR-GPLN-04): the task ids released."""

        project_id = validate_project_id(project_id)
        if not text_ok(reason, REASON_CHARS):
            raise AnswerInvalidError(field="reason", reason="length", gap_id=gap_id)
        async with self._locks[project_id]:
            return await self._dismiss(project_id, gap_id, reason=reason, by=by)

    async def _dismiss(self, project_id: str, gap_id: str, *, reason: str, by: str) -> tuple[str, ...]:
        gap = await self._stored(project_id, gap_id)
        if gap.status != "open":
            raise GapStateError(status=gap.status, allowed=("open",), gap_id=gap_id)
        # The holds end first: a plan that refuses (a persist runs) leaves the gap open, so the dismissal can be retried.
        released = await release(self.holds, project_id, gap.gap_id, aliases_of(gap.props), strict=True)
        now = self._clock()
        signatures = suppressed_signatures(gap)
        row = SuppressionRow(
            signature=signatures[0],
            gap_id=gap.gap_id,
            question=str(gap.props.get("question") or ""),
            reason=reason.strip(),
            dismissed_by=by,
            dismissed_at=now,
        )
        known = await self._records.suppressed(project_id, signatures)  # kept when the delete fails
        for signature in signatures:
            await self._records.suppress(project_id, row.model_copy(update={"signature": signature}))
        try:
            async with self._writer(project_id).transaction() as tx:
                for props in gap.clarifications:
                    await tx.delete_node(CLARIFICATION, clarification_uuid(project_id, str(props["clr_id"])))
                await tx.delete_node(GAP, gap_uuid(project_id, gap.gap_id))
        except BaseException:
            for signature in signatures:
                if signature not in known:
                    await self._records.unsuppress(project_id, signature)
            raise
        if reconcile_reason(gap) is not None:  # the upload stays what the build reads (reconcile.md "Edge cases")
            olds = await self._predecessors(project_id, gap)
            restore = await self._revertible(project_id, olds, SET_ASIDE_ROWS)  # its applied rows still hold
            await self.amendments.set_status(project_id, [key for ids in restore.values() for key in ids], "reverted")
        await self._records.put_options(project_id, gap.gap_id, ())
        await self._records.log(
            project_id,
            [
                EventRow(
                    gap_id=gap.gap_id,
                    at=now,
                    actor=by,
                    event="dismissed",
                    detail={"reason": row.reason, "signatures": len(signatures)},
                )
            ],
        )
        log_event(logger, INFO, "graph.gap.dismissed", project_id=project_id, gap_id=gap.gap_id, released=len(released))
        return released

    async def revert_clarification(self, project_id: str, clr_id: str, *, by: str, reason: str) -> RevertResult:
        """Withdraw an answer, a person's or AI's (answers.md "Revert", FR-GANS-10): the clarification becomes
        ``reverted`` and its amendments ``reverted``; an update build is requested when text the build reads leaves
        the corpus. A resolved gap stays resolved as history and a follow-up gap (``FOLLOWS_UP`` reason ``reverted``)
        opens with its question, nodes, witness lines and options, route ``ask``; a gap still ``answered`` or
        ``applying`` returns to ``open`` with route ``ask``. Either way AI never answers that question again unless a
        person sends it back. ``GapNotFoundError`` for an unknown clarification, ``GapStateError`` unless it is
        ``pending`` on an ``answered`` or ``applying`` gap, or ``applied`` or ``absorbed``; ``AnswerInvalidError``
        (``reason``) for a reason outside 1 to 500 characters."""

        project_id = validate_project_id(project_id)
        if not text_ok(reason, REASON_CHARS):
            raise AnswerInvalidError(field="reason", reason="length")
        async with self._locks[project_id]:
            return await self._revert(project_id, clr_id, by=by, reason=reason.strip())

    async def _revert(self, project_id: str, clr: str, *, by: str, reason: str) -> RevertResult:
        gaps = await read_gaps(self._executor, project_id, self._ontology, statuses=GRAPH_STATUSES)
        found = next(((gap, props) for gap in gaps for props in gap.clarifications if props.get("clr_id") == clr), None)
        if found is None:
            raise GapNotFoundError(clr_id=clr)
        gap, props = found
        status = str(props.get("status") or "")
        resolved = gap.status == "resolved" and status in APPLIED_CLARIFICATIONS
        if not resolved and not (status == PENDING and gap.status in ("answered", "applying")):
            raise GapStateError(status=status, allowed=REVERTIBLE, gap_id=gap.gap_id)
        rows = await self.amendments.for_clarification(project_id, clr)
        changed = {row.amendment_id: row.status for row in rows if row.status != REVERTED}
        in_document = props.get("placement") == "document" and (status != PENDING or bool(props.get(APPLY_MARK)))
        needs_build = in_document or any(value in IN_CORPUS_ROWS for value in changed.values())
        if needs_build and self.builds is None:
            raise ConfigurationError("gap_builds_missing")
        now = self._clock()
        follow_up: str | None = None
        await self.amendments.set_status(project_id, list(changed), REVERTED)
        try:
            async with self._writer(project_id).transaction() as tx:
                await tx.upsert_node(
                    CLARIFICATION,
                    clarification_uuid(project_id, clr),
                    {"status": REVERTED, "reverted_at": _iso(now), "reverted_by": by},
                )
                if resolved:
                    follow_up = await self._revert_follow_up(tx, project_id, gap, gaps, now=now, clr=clr)
                else:
                    await tx.upsert_node(
                        GAP,
                        gap_uuid(project_id, gap.gap_id),
                        {"status": "open", "route": "ask", "updated_at": _iso(now)},
                    )
        except BaseException:
            by_status: dict[str, list[str]] = defaultdict(list)
            for key, was in changed.items():
                by_status[was].append(key)
            for earlier, keys in by_status.items():
                await self.amendments.set_status(project_id, keys, earlier)
            raise
        # The build first: once the graph says reverted, a retry is refused, so the text must leave the corpus now.
        build = (
            await self.builds.request_update_build(project_id, REVERT_REASON, immediate=False)
            if needs_build and self.builds is not None
            else None
        )
        if follow_up is not None:
            options = await self._records.options(project_id, gap.gap_id)
            await self._records.put_options(
                project_id, follow_up, [row.model_copy(update={"gap_id": follow_up}) for row in options]
            )
        events = [
            EventRow(
                gap_id=gap.gap_id,
                clr_id=clr,
                at=now,
                actor=by,
                event=REVERTED,
                detail={
                    "reason": reason,
                    "mode": props.get("mode"),
                    "amendments": len(changed),
                    "follow_up": follow_up,
                    "answered_by_ai": props.get("answered_by") == AI_ACTOR,
                },
            )
        ]
        if follow_up is not None:
            events.append(
                EventRow(
                    gap_id=follow_up,
                    at=now,
                    actor=by,
                    event="created",
                    detail={"follows_up": gap.gap_id, "reason": REVERTED, "route": "ask"},
                )
            )
        await self._records.log(project_id, events)
        log_event(
            logger,
            INFO,
            "graph.gap.answer.reverted",
            project_id=project_id,
            gap_id=gap.gap_id,
            clr_id=clr,
            amendments=len(changed),
            follow_up=follow_up,
            build=build,
        )
        return RevertResult(
            clr_id=clr,
            gap_id=follow_up or gap.gap_id,
            follow_up=follow_up is not None,
            amendments=tuple(changed),
            build=build,
        )

    async def _revert_follow_up(
        self, tx: OwnedWriter, project_id: str, gap: StoredGap, gaps: Sequence[StoredGap], *, now: datetime, clr: str
    ) -> str:
        """The follow-up of a reverted answer: a new ``open`` gap, route ``ask``, ``FOLLOWS_UP`` the old one with
        reason ``reverted``, with the old gap's properties, ``ABOUT``, ``IMPACTS`` and ``CITES`` (lifecycle.md
        "Follow-ups and history"). Its signature is new (``follow_up_signature`` of the old gap and the reverted
        clarification, so a second revert of the same gap opens a second follow-up), so no run maps it to AI again."""

        signature = follow_up_signature(gap, f"{REVERTED}|{clr}")
        new = gap_id(signature, {item.gap_id: item.signature for item in gaps})
        names = ("kind", "severity", "origin", "title", "question", "why", "detector", "seed_signatures")
        props: dict[str, Any] = {name: gap.props[name] for name in names if gap.props.get(name) is not None}
        props |= {
            "gap_id": new,
            "status": "open",
            "route": "ask",
            "signature": signature,
            "blocking": "no",
            "held_back": "no",
            "created_by": RUN_ACTOR,
            "created_at": _iso(now),
            "updated_at": _iso(now),
        }
        owner = WriterRef(GAP, gap_uuid(project_id, new))
        await tx.upsert_node(GAP, owner.key, props)
        old = WriterRef(GAP, gap_uuid(project_id, gap.gap_id))
        await tx.upsert_relationship(
            FOLLOWS_UP, owner, old, link_key(new, FOLLOWS_UP, gap.gap_id), {"reason": REVERTED}
        )
        for link in (*gap.about, *gap.impacts):
            if link.uuid and link.label:
                await tx.upsert_relationship(
                    link.relation,
                    owner,
                    WriterRef(link.label, link.uuid),
                    link_key(new, link.relation, link.spec_id),
                    {},
                )
        for cite in gap.cites:
            digest = cite.quote_hash or quote_hash(cite.quote)
            cited: dict[str, Any] = {"quote": cite.quote, "quote_hash": digest}
            if cite.line_start is not None:
                cited |= {"line_start": cite.line_start, "line_end": cite.line_end or cite.line_start}
            await tx.upsert_relationship(
                CITES, owner, WriterRef(FRAGMENT, cite.fragment_id), cite_key(new, cite.fragment_id, digest), cited
            )
        return new

    async def raise_gap(self, project_id: str, new: NewGap) -> RaisedGap:
        """Open a gap by hand (FR-GANS-12) or for a coding agent (origin ``development``, FR-GDEV-01): after the checks
        of a report (the ids resolve, the quotes are found in their active fragments), an ``open`` gap with the asker's
        question, severity ``major`` unless given, route ``ask``, ``ABOUT`` the ids and ``CITES`` the quotes; the refs
        of a development gap go into its ``created`` event. The host then runs a ``site`` run on it for options,
        ``IMPACTS`` and a severity. The same question about the same ids while it is open is that gap (nothing held).

        Holds (lifecycle.md "Development gaps", "Plan link"): a ``blocking`` development gap passes the plan's guard
        (the agent holds the story under its token and one of its tasks implements a node of the gap) before anything
        is written, then the plan's block call blocks the claimed tasks with ``blocked_by`` the gap id and ends the
        claim (FR-GDEV-02); when the block fails the gap is taken out again and the plan's error passes. A raised
        ``blocker`` gap holds the tasks implementing its nodes (GD2). ``AnswerInvalidError`` (``limit``) when the agent
        already has ``DEVELOPMENT_OPEN_LIMIT`` open development gaps on the story."""

        project_id = validate_project_id(project_id)
        question = new.question.strip()
        if not text_ok(question, QUESTION_CHARS):
            raise AnswerInvalidError(field="question", reason="length")
        async with self._locks[project_id]:  # two equal findings at once: one gap, one block
            return await self._raise(project_id, new, question)

    async def _raised_nodes(self, project_id: str, new: NewGap) -> list[SpecRef]:
        """The current nodes of a raised gap's ``about`` ids; ``AnswerInvalidError`` for a bad count or unknown id."""

        ids = tuple(dict.fromkeys(item.strip() for item in new.about if item.strip()))
        if len(ids) > ABOUT_MAX or (not ids and not new.quotes):
            raise AnswerInvalidError(field="about", reason="count")
        refs = await resolve_spec_ids(self._executor, project_id, ids, self._labels) if ids else {}
        if set(ids) - set(refs):
            raise AnswerInvalidError(field="about", reason="unknown_ids")
        return sorted({ref.spec_id: ref for ref in refs.values()}.values(), key=lambda ref: ref.spec_id)

    async def _raise(self, project_id: str, new: NewGap, question: str) -> RaisedGap:
        nodes = await self._raised_nodes(project_id, new)
        quotes = await self._quoted(project_id, new)
        about = [ref for ref in nodes if self._ontology.is_legal(ABOUT, GAP, ref.label)]
        current = [ref.spec_id for ref in nodes]
        signature = (
            development_signature(new.story_id or "", current, question)
            if new.origin == "development"
            else person_signature(current, question)
        )
        existing = await read_gaps(self._executor, project_id, self._ontology, statuses=GRAPH_STATUSES)
        same = next((gap for gap in existing if gap.signature == signature and gap.status in OPEN_STATUSES), None)
        if same is not None:
            return RaisedGap(gap_id=same.gap_id, created=False)
        if new.origin == "development" and _open_development(existing, new) >= DEVELOPMENT_OPEN_LIMIT:
            raise AnswerInvalidError(field="limit", reason="open_limit")
        if new.blocking:
            if self.holds is None:
                raise ConfigurationError("gap_holds_missing")
            await self.holds.check_block(
                project_id,
                new.story_id or "",
                claimed_by=new.created_by,
                claim_token=new.claim_token or "",
                spec_ids=current,
            )
        new_id = gap_id(signature, {gap.gap_id: gap.signature for gap in existing})
        now = self._clock()
        props = self._raised_props(new, new_id, question, signature, now)
        owner = WriterRef(GAP, gap_uuid(project_id, new_id))
        async with self._writer(project_id).transaction() as tx:
            await tx.upsert_node(GAP, owner.key, props)
            for ref in about:
                await tx.upsert_relationship(
                    ABOUT, owner, WriterRef(ref.label, ref.uuid), link_key(new_id, ABOUT, ref.spec_id), {}
                )
            for fragment_id, quote, line in quotes:
                digest = quote_hash(quote)
                cite: dict[str, Any] = {"quote": quote, "quote_hash": digest}
                if line is not None:
                    cite |= {"line_start": line, "line_end": line}
                await tx.upsert_relationship(
                    CITES, owner, WriterRef(FRAGMENT, fragment_id), cite_key(new_id, fragment_id, digest), cite
                )
        held = await self._hold_raised(project_id, new, new_id, [ref.spec_id for ref in about])
        detail: dict[str, Any] = {"origin": new.origin, "kind": new.kind, "about": len(about), "cites": len(quotes)}
        if new.refs:
            detail["refs"] = [ref.model_dump(mode="json") for ref in new.refs]
        if new.blocking:
            detail |= {"story_id": new.story_id, "held": len(held)}
        await self._records.log(
            project_id, [EventRow(gap_id=new_id, at=now, actor=new.created_by, event="created", detail=detail)]
        )
        log_event(
            logger,
            INFO,
            "graph.gap.raised",
            project_id=project_id,
            gap_id=new_id,
            origin=new.origin,
            about=len(about),
            blocking=new.blocking,
            held=len(held),
        )
        return RaisedGap(gap_id=new_id, held=held)

    def _raised_props(self, new: NewGap, new_id: str, question: str, signature: str, now: datetime) -> dict[str, Any]:
        props: dict[str, Any] = {
            "gap_id": new_id,
            "kind": new.kind,
            "severity": new.severity or "major",
            "status": "open",
            "origin": new.origin,
            "route": "ask",
            "title": _title(question),
            "question": question,
            "why": "",
            "detector": new.origin,
            "signature": signature,
            "blocking": YES if new.blocking else "no",
            "held_back": "no",
            "created_by": new.created_by,
            "created_at": _iso(now),
            "updated_at": _iso(now),
        }
        if new.story_id:
            props["story_id"] = new.story_id
        return props

    async def _hold_raised(self, project_id: str, new: NewGap, new_id: str, current: Sequence[str]) -> tuple[str, ...]:
        """Hold what a new raised gap holds (``raise_gap``); a failed block takes the gap out again."""

        if new.blocking and self.holds is not None:
            try:
                blocked = await self.holds.block_story(
                    project_id,
                    new.story_id or "",
                    claim_token=new.claim_token or "",
                    reason=hold_reason(new.kind),
                    finding_id=new_id,
                )
            except BaseException:
                async with self._writer(project_id).transaction() as tx:
                    await tx.delete_node(GAP, gap_uuid(project_id, new_id))
                raise
            return tuple(getattr(blocked, "released", ()) or ())
        if (new.severity or "major") == BLOCKER:
            return (await hold(self.holds, project_id, new_id, current, new.kind)).held
        return ()

    async def hold_blockers(self, project_id: str, spec_ids: Sequence[str] | None = None) -> HoldResult:
        """Hold the tasks of every open ``blocker`` gap (``ABOUT`` one of ``spec_ids``, aliases resolved, when given):
        decision GD2 at claim time, so a task a plan run reset or a claim left alone is held again before anyone claims
        it (``graph.gap.holds``). Nothing without ``holds``."""

        project_id = validate_project_id(project_id)
        if self.holds is None:
            return HoldResult()
        wanted = None if spec_ids is None else await self._current(project_id, spec_ids)
        gaps = await read_gaps(self._executor, project_id, self._ontology, statuses=OPEN_STATUSES)
        return await hold_blockers(self.holds, project_id, gaps, wanted)

    async def add_note(
        self, project_id: str, *, spec_ids: Sequence[str], text: str, created_by: str, story_id: str | None = None
    ) -> str:
        """A coding agent's ``note`` finding (lifecycle.md "Development gaps": kind ``note`` is no gap): a ``gap_note``
        row (detector ``development``) about the resolved ids; its id (``NOTE::`` and 12 hex of the note). The same
        note twice is one id. ``AnswerInvalidError`` for a text outside 1 to 2000 characters or an unknown id."""

        project_id = validate_project_id(project_id)
        body = text.strip()
        if not text_ok(body, NOTE_CHARS):
            raise AnswerInvalidError(field="text", reason="length")
        ids = tuple(dict.fromkeys(item.strip() for item in spec_ids if item.strip()))
        if not ids or len(ids) > ABOUT_MAX:
            raise AnswerInvalidError(field="about", reason="count")
        refs = await resolve_spec_ids(self._executor, project_id, ids, self._labels)
        if set(ids) - set(refs):
            raise AnswerInvalidError(field="about", reason="unknown_ids")
        current = tuple(sorted({ref.spec_id for ref in refs.values()}))
        note_id = NOTE_PREFIX + development_signature(story_id or "", current, body)[:12].upper()
        await self._records.put_notes(
            project_id, None, [NoteRow(run_id=None, detector=DEVELOPMENT, spec_ids=current, text=body)]
        )
        log_event(logger, INFO, "graph.gap.note_added", project_id=project_id, about=len(current), by=created_by)
        return note_id

    async def noise_rules(self, project_id: str) -> Sequence[NoiseRuleRow]:
        """The project's noise rules: sections and patterns a person marked "not spec" (detection.md)."""

        return await self._records.noise_rules(validate_project_id(project_id))

    async def add_noise_rule(self, project_id: str, scope: NoiseScope, value: str, reason: str, by: str) -> str:
        """Add a noise rule (the next run's relevance filter treats what it names as tracking); the same scope and
        value is one rule. ``AnswerInvalidError`` for an empty value or reason, or a pattern that does not compile."""

        project_id = validate_project_id(project_id)
        if scope not in get_args(NoiseScope):
            raise AnswerInvalidError(field="scope", reason="unknown_value")
        clean = value.strip()
        if not text_ok(clean, NOISE_VALUE_CHARS):
            raise AnswerInvalidError(field="value", reason="length")
        if not text_ok(reason, REASON_CHARS):
            raise AnswerInvalidError(field="reason", reason="length")
        if scope == "pattern":
            try:
                re.compile(clean)
            except re.error:
                raise AnswerInvalidError(field="value", reason="pattern_invalid") from None
        rule_id = noise_rule_id(scope, clean)
        row = NoiseRuleRow(
            rule_id=rule_id, scope=scope, value=clean, reason=reason.strip(), created_by=by, created_at=self._clock()
        )
        await self._records.put_noise_rule(project_id, row)
        log_event(logger, INFO, "graph.gap.noise_rule.added", project_id=project_id, rule_id=rule_id, scope=scope)
        return rule_id

    async def remove_noise_rule(self, project_id: str, rule_id: str, by: str) -> bool:
        """Remove a noise rule; False when the project has no such rule."""

        project_id = validate_project_id(project_id)
        removed = await self._records.delete_noise_rule(project_id, rule_id)
        log_event(
            logger, INFO, "graph.gap.noise_rule.removed", project_id=project_id, rule_id=rule_id, removed=removed, by=by
        )
        return removed

    # Write helpers

    def _writer(self, project_id: str) -> OwnedWriter:
        return OwnedWriter(self._executor, self._ontology, OWNER, project_id)

    async def _stored(self, project_id: str, gap_id: str) -> StoredGap:
        found = await read_gaps(self._executor, project_id, self._ontology, statuses=GRAPH_STATUSES, gap_ids=[gap_id])
        if not found:
            raise GapNotFoundError(gap_id=gap_id)
        return found[0]

    async def _predecessors(self, project_id: str, gap: StoredGap) -> tuple[Predecessor, ...]:
        ids = [old for old, _ in continued(gap)]
        if not ids:
            return ()
        olds = await read_gaps(self._executor, project_id, self._ontology, statuses=GRAPH_STATUSES, gap_ids=ids)
        return predecessors(gap, {old.gap_id: old for old in olds})

    async def _revertible(
        self, project_id: str, olds: Sequence[Predecessor], statuses: Collection[str] = REVERTED_ROWS
    ) -> dict[str, list[str]]:
        """The amendments of replaced clarifications an answer reverts, by their current status (to set back)."""

        found: dict[str, list[str]] = defaultdict(list)
        for old in olds:
            for row in await self.amendments.for_clarification(project_id, old.clr_id):
                if row.status in statuses:
                    found[row.status].append(row.amendment_id)
        return dict(found)

    async def _settle(self, project_id: str, gap: StoredGap, answer: AnswerInput, label: str) -> AnswerResult:
        """An option without edits on a reconcile follow-up (``graph.gap.follow_ups``): the follow-up resolves now.
        "Keep the upload" is a ``pointer`` grounded in the owner's lines the gap cites; "Drop it" an ``option``
        answer. The answer it follows up becomes ``superseded`` and its amendments ``reverted``; no build is
        needed (the build already reads the upload)."""

        olds = await self._predecessors(project_id, gap)
        keep = reconcile_reason(gap) == "conflict"
        now = self._clock()
        clr = clr_id(gap.gap_id, label, now)
        props = clarification_props(
            clr,
            mode="pointer" if keep else "option",
            placement="none",
            option_label=label,
            text=label,
            answered_by=answer.answered_by,
            answered_at=now,
        ) | {"status": "applied"}
        pending = [item for item in gap.clarifications if item.get("status") == PENDING]
        restore = await self._revertible(project_id, olds)
        await self.amendments.set_status(project_id, [key for ids in restore.values() for key in ids], "reverted")
        try:
            async with self._writer(project_id).transaction() as tx:
                replaces = olds[0].clr_id if olds else None
                await write_clarification(tx, project_id, ClarificationWrite(clr, gap, props, (), replaces))
                node = WriterRef(CLARIFICATION, clarification_uuid(project_id, clr))
                for cite in gap.cites if keep else ():
                    await tx.upsert_relationship(
                        GROUNDED_IN,
                        node,
                        WriterRef(FRAGMENT, cite.fragment_id),
                        answer_key(clr, GROUNDED_IN, cite.fragment_id),
                        {},
                    )
                for old in olds:
                    await tx.upsert_node(
                        CLARIFICATION, clarification_uuid(project_id, old.clr_id), {"status": "superseded"}
                    )
                for item in pending:
                    await tx.delete_node(CLARIFICATION, clarification_uuid(project_id, str(item["clr_id"])))
                await tx.upsert_node(
                    GAP,
                    gap_uuid(project_id, gap.gap_id),
                    {
                        "status": "resolved",
                        "resolution": "pointer" if keep else "answered",
                        "resolved_at": _iso(now),
                        "updated_at": _iso(now),
                    },
                )
        except BaseException:
            for status, ids in restore.items():
                await self.amendments.set_status(project_id, ids, status)
            raise
        events = await self._replaced(project_id, gap, pending, by=answer.answered_by, now=now)
        events += [
            EventRow(
                gap_id=gap.gap_id,
                clr_id=clr,
                at=now,
                actor=answer.answered_by,
                event="answered",
                detail={"mode": props["mode"], "placement": "none", "amendments": 0, "replaces": replaces},
            ),
            EventRow(
                gap_id=gap.gap_id,
                clr_id=clr,
                at=now,
                actor=answer.answered_by,
                event="resolved",
                detail={"resolution": "pointer" if keep else "answered"},
            ),
            *(
                EventRow(
                    gap_id=old.gap_id,
                    clr_id=old.clr_id,
                    at=now,
                    actor=answer.answered_by,
                    event="superseded",
                    detail={"reason": old.reason},
                )
                for old in olds
            ),
        ]
        await self._records.log(project_id, events)
        if self.builds is not None and set(restore) & APPLIED_ROWS:  # a part of the old answer leaves the corpus
            await self.builds.request_update_build(project_id, APPLY_REASON, immediate=False)
        log_event(
            logger,
            INFO,
            "graph.gap.answer.settled",
            project_id=project_id,
            gap_id=gap.gap_id,
            clr_id=clr,
            reverted=sum(len(ids) for ids in restore.values()),
        )
        return AnswerResult(gap_id=gap.gap_id, clr_id=clr, placement="none", apply_at=now, immediate=True)

    async def _answerable(self, project_id: str, gap_id: str) -> StoredGap:
        """The gap, when an answer applies to it: ``open`` or ``answered``, or ``resolved`` with an applied
        clarification (answered again from its history); else ``GapStateError``."""

        gap = await self._stored(project_id, gap_id)
        applied = any(props.get("status") in APPLIED_CLARIFICATIONS for props in gap.clarifications)
        if gap.status not in ANSWERABLE or (gap.status == "resolved" and not applied):
            raise GapStateError(status=gap.status, allowed=ANSWERABLE, gap_id=gap_id)
        return gap

    async def _answer_edits(
        self, project_id: str, gap: StoredGap, answer: AnswerInput
    ) -> tuple[str, str, tuple[Edit, ...]]:
        """The mode, the option label and the edits of an answer before placement and checks."""

        if answer.option_rank is not None:
            option = option_of(await self._records.options(project_id, gap.gap_id), answer.option_rank, gap.gap_id)
            edits = answer.edits if answer.edits is not None else option.edits
            return "option", option.label, tuple(edits)
        text = _own_text(answer.own_text, gap.gap_id)
        return "own", "", tuple(answer.edits) if answer.edits else (document_edit(gap, text),)

    async def _replaced(
        self, project_id: str, gap: StoredGap, pending: Sequence[Any], *, by: str, now: datetime
    ) -> list[EventRow]:
        """Delete the amendments of the pending clarifications an answer replaced; their ``replaced`` events."""

        events: list[EventRow] = []
        for props in pending:
            old = str(props["clr_id"])
            rows = await self.amendments.for_clarification(project_id, old)
            await self.amendments.delete(project_id, [row.amendment_id for row in rows])
            events.append(
                EventRow(
                    gap_id=gap.gap_id,
                    clr_id=old,
                    at=now,
                    actor=by,
                    event="replaced",
                    detail={"mode": props.get("mode"), "placement": props.get("placement"), "amendments": len(rows)},
                )
            )
        return events

    async def _quoted(self, project_id: str, new: NewGap) -> list[tuple[str, str, int | None]]:
        """The quotes of a raised gap with the document line each starts on; ``AnswerInvalidError`` for a quote not
        found in its active fragment."""

        if not new.quotes:
            return []
        fragments = {fragment.fragment_id: fragment for fragment in await self._reader.fragments(project_id)}
        found: list[tuple[str, str, int | None]] = []
        for item in new.quotes:
            fragment = fragments.get(item.fragment_id)
            wanted = normalise_quote(item.quote)
            active = fragment is not None and (fragment.status or "active") == "active"
            if fragment is None or not active or not wanted or wanted not in normalise_quote(fragment.text):
                raise AnswerInvalidError(field="quotes", reason="not_found")
            offset = next(
                (n for n, line in enumerate(fragment.text.splitlines()) if wanted in normalise_quote(line)), None
            )
            start = fragment.line_start
            found.append((item.fragment_id, item.quote, None if offset is None or start is None else start + offset))
        return found

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


def _document_answer(clr: str, gap: StoredGap | None, status: str, amendments: int) -> DocumentAnswer:
    title = "" if gap is None else str(gap.props.get("title") or gap.props.get("question") or "")
    return DocumentAnswer(
        clr_id=clr, gap_id=None if gap is None else gap.gap_id, title=title, status=status, amendments=amendments
    )


def _clarification_status(gap: StoredGap | None, clr: str) -> str:
    props = next((item for item in (() if gap is None else gap.clarifications) if item.get("clr_id") == clr), {})
    return str(props.get("status") or PENDING)


def _sorted_answers(answers: Sequence[DocumentAnswer]) -> tuple[DocumentAnswer, ...]:
    return tuple(sorted(answers, key=lambda item: (item.title, item.clr_id)))


def suppressed_signatures(gap: StoredGap) -> list[str]:
    """What a dismissal suppresses (lifecycle.md "Dismissals and suppression"): the gap's signature first, then the
    signatures of the detector seeds its sites held (``seed_signatures``), so a later run on unchanged nodes and witness
    drops those seeds before they form a site (FR-GDET-07, FR-GANS-11)."""

    seeds = [str(item) for item in gap.props.get("seed_signatures") or () if item]
    return list(dict.fromkeys([gap.signature or gap.gap_id, *seeds]))


def _own_text(text: str | None, gap_id: str) -> str:
    if not text_ok(text, ANSWER_TEXT_CHARS):
        raise AnswerInvalidError(field="own_text", reason="length", gap_id=gap_id)
    return str(text).strip()


def _updated_at(gap: StoredGap) -> datetime:
    return _instant(gap.props.get("updated_at") or gap.props["created_at"])


def _millis(value: datetime) -> int:
    return int(_instant(value).timestamp() * 1000)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _latest(clarifications: Any) -> str | None:
    found = sorted(clarifications, key=lambda props: str(props.get("answered_at") or ""))
    return str(found[-1]["clr_id"]) if found else None


def _title(question: str) -> str:
    """The first line of a raised question, cut to ``TITLE_CHARS`` at a word break."""

    line = question.strip().splitlines()[0].strip()
    if len(line) <= TITLE_CHARS:
        return line
    cut = line[: TITLE_CHARS - 1].rsplit(" ", 1)[0]
    return f"{cut}\u2026"
