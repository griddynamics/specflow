"""The migration of first-slice findings (docs/graph/gap/lifecycle.md "Migration of first-slice findings", FR-GDEV-07).

The first slice stored ``Finding`` nodes with ``ABOUT`` relations and ``ExternalRef`` nodes with ``REFERS_TO``. The
gap ontology no longer declares those types, so the migration reads and deletes them with its own named queries
(``READ_FIRST_SLICE``, ``DELETE_FIRST_SLICE_FINDINGS``, ``DELETE_FIRST_SLICE_REFS``) and writes what replaces them
through ``OwnedWriter`` and ``GapRecords``:

- an open finding becomes an ``open`` development gap; a resolved one a ``resolved`` gap with an applied ``own``
  clarification holding the closing note;
- a dismissed finding becomes a ``gap_suppression`` row, a ``note`` finding (any status) a ``gap_note`` row;
- ``ABOUT`` to spec nodes stays ``ABOUT`` from the gap (current ids, ontology-legal pairs only), ``ABOUT`` to a
  ``Fragment`` becomes ``CITES`` with an empty quote;
- the ``FND::`` id is kept in the gap's ``aliases`` property and, with the external refs, in the ``detail`` of the
  gap's first event (``created``, ``migrated: true``).

The ``Finding`` and ``ExternalRef`` nodes are deleted after every gap write committed. A second run finds no
``Finding`` and does nothing; a run repeated after a failure before the delete writes no gap twice (gap ids, storage
keys and edge keys are deterministic, and a finding whose signature already has a gap only adds its alias there).
Finding text and notes are stored, never logged.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from graph.core.identity import validate_project_id
from graph.core.ontology import Ontology
from graph.core.ontology.compiled import BUILD_OWNER
from graph.core.store import Executor, as_executor
from graph.core.writer import FRAGMENT, NodeRef, OwnedWriter
from graph.gap.answers import ClarificationWrite, clarification_props, write_clarification
from graph.gap.gaps import StoredGap, cite_key, gap_uuid, link_key, quote_hash, read_gaps
from graph.gap.identity import DEVELOPMENT_DETECTOR, clr_id, development_signature, gap_id
from graph.gap.models import EventRow, NoteRow, SuppressionRow
from graph.gap.names import ABOUT, CITES, GAP, GRAPH_STATUSES, OWNER
from graph.gap.ports import GapRecords
from graph.gap.queries import DELETE_FIRST_SLICE_FINDINGS, DELETE_FIRST_SLICE_REFS, READ_FIRST_SLICE
from graph.gap.resolve import SpecRef, resolve_spec_ids
from logger.logger import INFO, get_logger, log_event

logger = get_logger("graph.gap")

MIGRATION_RUN: Final = "first-slice-migration"
"""The ``run_id`` of the ``gap_note`` rows the migration writes."""
MIGRATION_ACTOR: Final = "first-slice-migration"
"""The actor of a migration event when the finding names no person."""
QUESTION_CHARS: Final = 1000
"""The ontology's cap of ``Gap.question``: a longer finding text is cut (the full text stays in no other place)."""
TITLE_CHARS: Final = 160
NO_NOTE: Final = "Resolved as a first-slice finding; no closing note was given."
DISMISSED: Final = "Dismissed as a first-slice finding."
KINDS: Final[Mapping[str, str]] = {"question": "open_question", "conflict": "contradiction", "gap": "missing"}
NOTE: Final = "note"
YES: Final = "yes"
NO: Final = "no"


@dataclass(frozen=True)
class MigrationResult:
    """What one project's migration did: gaps written (``resolved`` of them closed), findings merged into a gap with
    the same signature, suppressions and notes written, and the first-slice nodes deleted."""

    gaps: int = 0
    resolved: int = 0
    merged: int = 0
    suppressed: int = 0
    notes: int = 0
    deleted: int = 0


@dataclass(frozen=True)
class _Finding:
    """One ``READ_FIRST_SLICE`` row."""

    props: Mapping[str, Any]
    spec_ids: tuple[str, ...]
    fragment_ids: tuple[str, ...]
    refs: tuple[Mapping[str, str], ...]

    @property
    def finding_id(self) -> str:
        return str(self.props["finding_id"])

    @property
    def text(self) -> str:
        return str(self.props.get("text") or "")

    @property
    def asked_ids(self) -> tuple[str, ...]:
        """The spec ids its ``ABOUT`` relations end at and the ids it stored (aliases resolve them later)."""

        return tuple(dict.fromkeys([*self.spec_ids, *(str(i) for i in self.props.get("about_spec_ids") or ())]))


def _finding(row: Mapping[str, Any]) -> _Finding:
    return _Finding(
        props=dict(row["finding"]),
        spec_ids=tuple(str(item) for item in row.get("spec_ids") or () if item),
        fragment_ids=tuple(sorted(str(item) for item in row.get("fragment_ids") or () if item)),
        refs=tuple(
            sorted(
                (
                    {"type": str(ref.get("type") or ""), "value": str(ref.get("value") or "")}
                    for ref in row.get("refs") or ()
                    if ref
                ),
                key=lambda ref: (ref["type"], ref["value"]),
            )
        ),
    )


def _instant(value: Any, fallback: datetime) -> datetime:
    """A stored time (ISO text, or a driver date-time) as an aware ``datetime``; ``fallback`` when absent."""

    if value is None or value == "":
        return fallback
    if hasattr(value, "to_native"):
        value = value.to_native()
    found = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return found if found.tzinfo is not None else found.replace(tzinfo=UTC)


def _closing_note(item: _Finding) -> str:
    return str(item.props.get("resolution_note") or "").strip() or NO_NOTE


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def title(question: str) -> str:
    """The first line of a question, cut to ``TITLE_CHARS`` at a word break (as a raised gap's title)."""

    lines = question.strip().splitlines()
    line = lines[0].strip() if lines else ""
    if len(line) <= TITLE_CHARS:
        return line
    cut = line[: TITLE_CHARS - 1].rsplit(" ", 1)[0]
    return f"{cut}…"


def question_of(text: str) -> str:
    """The finding text as a gap question: trimmed, cut to the ontology's ``QUESTION_CHARS`` at a word break."""

    question = text.strip()
    if len(question) <= QUESTION_CHARS:
        return question
    cut = question[: QUESTION_CHARS - 1].rsplit(" ", 1)[0]
    return f"{cut}…"


class _Migration:
    """One project's migration over one executor."""

    def __init__(
        self,
        source: Any,
        ontology: Ontology,
        project_id: str,
        records: GapRecords,
        *,
        database: str | None,
        clock: Callable[[], datetime],
    ) -> None:
        self.source = source
        self.database = database
        self.executor: Executor = as_executor(source, database)
        self.ontology = ontology
        self.project_id = project_id
        self.records = records
        self.clock = clock
        self.labels = tuple(label for label in ontology.labels if ontology.owner_of(label) == BUILD_OWNER)
        self.by_signature: dict[str, StoredGap] = {}
        self.taken: dict[str, str] = {}

    def times(self, item: _Finding) -> tuple[datetime, datetime, datetime]:
        """When the finding was created, last written and closed (the earlier stamp when a later one is absent)."""

        created_at = _instant(item.props.get("created_at"), self.clock())
        updated_at = _instant(item.props.get("updated_at"), created_at)
        return created_at, updated_at, _instant(item.props.get("resolved_at"), updated_at)

    def writer(self) -> OwnedWriter:
        return OwnedWriter(self.source, self.ontology, OWNER, self.project_id, database=self.database)

    async def run(self) -> MigrationResult:
        rows = await self.executor.read(READ_FIRST_SLICE, self.project_id)
        if not rows:
            return MigrationResult()
        findings = sorted((_finding(row) for row in rows), key=lambda item: item.finding_id)
        existing = await read_gaps(self.executor, self.project_id, self.ontology, statuses=GRAPH_STATUSES)
        self.by_signature = {gap.signature: gap for gap in existing if gap.signature}
        self.taken = {gap.gap_id: gap.signature for gap in existing}
        asked = tuple(dict.fromkeys(spec_id for item in findings for spec_id in item.asked_ids))
        nodes = await resolve_spec_ids(self.executor, self.project_id, asked, self.labels) if asked else {}
        counts = {"gaps": 0, "resolved": 0, "merged": 0, "suppressed": 0, "notes": 0}
        notes: list[NoteRow] = []
        for item in findings:
            current = sorted(
                {nodes[spec_id].spec_id: nodes[spec_id] for spec_id in item.asked_ids if spec_id in nodes}.values(),
                key=lambda ref: ref.spec_id,
            )
            status = str(item.props.get("status") or "open")
            if item.props.get("kind") == NOTE:
                notes.append(
                    NoteRow(
                        detector=DEVELOPMENT_DETECTOR, spec_ids=tuple(ref.spec_id for ref in current), text=item.text
                    )
                )
            elif status == "dismissed":
                await self.suppress(item, current)
                counts["suppressed"] += 1
            else:
                outcome = await self.gap(item, current, resolved=status == "resolved")
                counts[outcome] += 1
                counts["resolved"] += int(outcome == "gaps" and status == "resolved")
        if notes:
            await self.records.put_notes(self.project_id, MIGRATION_RUN, notes)
            counts["notes"] = len(notes)
        async with self.executor.tx(self.project_id) as tx:
            deleted = await tx.run(DELETE_FIRST_SLICE_FINDINGS)
            await tx.run(DELETE_FIRST_SLICE_REFS)
        result = MigrationResult(**counts, deleted=int(deleted[0]["deleted"]) if deleted else 0)
        log_event(
            logger,
            INFO,
            "graph.gap.first_slice_migrated",
            project_id=self.project_id,
            findings=len(findings),
            gaps=result.gaps,
            resolved=result.resolved,
            merged=result.merged,
            suppressed=result.suppressed,
            notes=result.notes,
            deleted=result.deleted,
        )
        return result

    def signature(self, item: _Finding, current: Sequence[SpecRef]) -> str:
        story = str(item.props.get("story_id") or "")
        return development_signature(story, [ref.spec_id for ref in current], question_of(item.text))

    async def suppress(self, item: _Finding, current: Sequence[SpecRef]) -> None:
        _, _, closed_at = self.times(item)
        await self.records.suppress(
            self.project_id,
            SuppressionRow(
                signature=self.signature(item, current),
                question=question_of(item.text),
                reason=str(item.props.get("resolution_note") or "").strip() or DISMISSED,
                dismissed_by=str(item.props.get("resolved_by") or item.props.get("created_by") or MIGRATION_ACTOR),
                dismissed_at=closed_at,
            ),
        )

    async def gap(self, item: _Finding, current: Sequence[SpecRef], *, resolved: bool) -> str:
        """Write the gap of an open or resolved finding; ``gaps`` when written (or repaired after an earlier failed
        run), ``merged`` when another gap already has its signature."""

        signature = self.signature(item, current)
        same = self.by_signature.get(signature)
        if same is not None:
            aliases = [str(alias) for alias in same.props.get("aliases") or ()]
            if item.finding_id in aliases:  # written by an earlier run that failed before the delete
                await self.log_once(same.gap_id, item, resolved=resolved)
                return "gaps"
            await self.writer().upsert_node(
                GAP, gap_uuid(self.project_id, same.gap_id), {"aliases": [*aliases, item.finding_id]}
            )
            self.by_signature[signature] = StoredGap(
                gap_id=same.gap_id, props={**same.props, "aliases": [*aliases, item.finding_id]}
            )
            await self.records.log(
                self.project_id,
                [self.event(same.gap_id, item, "aliased", self.times(item)[0], about=len(current))],
            )
            return "merged"
        new_id = gap_id(signature, self.taken)
        await self.write(new_id, signature, item, current, resolved=resolved)
        self.taken[new_id] = signature
        self.by_signature[signature] = StoredGap(
            gap_id=new_id, props={"signature": signature, "aliases": [item.finding_id]}
        )
        await self.log_once(new_id, item, resolved=resolved)
        return "gaps"

    async def write(
        self, new_id: str, signature: str, item: _Finding, current: Sequence[SpecRef], *, resolved: bool
    ) -> None:
        props = item.props
        created_at, updated_at, resolved_at = self.times(item)
        question = question_of(item.text)
        gap_props: dict[str, Any] = {
            "gap_id": new_id,
            "kind": KINDS.get(str(props.get("kind")), "missing"),
            "severity": "major",
            "status": "resolved" if resolved else "open",
            "origin": "development",
            "route": "ask",
            "title": title(question),
            "question": question,
            "why": "",
            "detector": DEVELOPMENT_DETECTOR,
            "signature": signature,
            "blocking": YES if props.get("blocking") else NO,
            "held_back": NO,
            "aliases": [item.finding_id],
            "created_by": str(props.get("created_by") or MIGRATION_ACTOR),
            "created_at": _iso(created_at),
            "updated_at": _iso(updated_at),
        }
        if props.get("story_id"):
            gap_props["story_id"] = str(props["story_id"])
        if resolved:
            gap_props |= {"resolution": "answered", "resolved_at": _iso(resolved_at)}
        owner = NodeRef(GAP, gap_uuid(self.project_id, new_id))
        async with self.writer().transaction() as tx:
            await tx.upsert_node(GAP, owner.key, gap_props)
            for ref in current:
                if self.ontology.is_legal(ABOUT, GAP, ref.label):
                    await tx.upsert_relationship(
                        ABOUT, owner, NodeRef(ref.label, ref.uuid), link_key(new_id, ABOUT, ref.spec_id), {}
                    )
            digest = quote_hash("")
            for fragment_id in item.fragment_ids:
                await tx.upsert_relationship(
                    CITES,
                    owner,
                    NodeRef(FRAGMENT, fragment_id),
                    cite_key(new_id, fragment_id, digest),
                    {"quote": "", "quote_hash": digest},
                )
            if resolved:
                clr, clr_props = self.clarification(new_id, item, resolved_at)
                stored = StoredGap(gap_id=new_id, props=gap_props)
                await write_clarification(tx, self.project_id, ClarificationWrite(clr, stored, clr_props, ()))

    def clarification(self, new_id: str, item: _Finding, answered_at: datetime) -> tuple[str, dict[str, Any]]:
        text = _closing_note(item)
        clr = clr_id(new_id, text, answered_at)
        props = clarification_props(
            clr,
            mode="own",
            placement="none",
            option_label="",
            text=text,
            answered_by=str(item.props.get("resolved_by") or MIGRATION_ACTOR),
            answered_at=answered_at,
        ) | {"status": "applied"}
        return clr, props

    def event(self, gap: str, item: _Finding, name: str, at: datetime, **detail: Any) -> EventRow:
        return EventRow(
            gap_id=gap,
            at=at,
            actor=str(item.props.get("created_by") or MIGRATION_ACTOR),
            event=name,
            detail={
                "origin": "development",
                "kind": KINDS.get(str(item.props.get("kind")), "missing"),
                "alias": item.finding_id,
                "migrated": True,
                "refs": [dict(ref) for ref in item.refs],
                **detail,
            },
        )

    async def log_once(self, gap: str, item: _Finding, *, resolved: bool) -> None:
        """The ``created`` event (and ``resolved`` for a resolved finding) of a migrated gap, unless an earlier run
        logged them already."""

        known = await self.records.events(self.project_id, gap)
        if any(row.detail.get("alias") == item.finding_id for row in known):
            return
        created_at, _, resolved_at = self.times(item)
        events = [
            self.event(
                gap,
                item,
                "created",
                created_at,
                about=len(item.asked_ids),
                cites=len(item.fragment_ids),
                blocking=bool(item.props.get("blocking")),
            )
        ]
        if resolved:
            events.append(
                EventRow(
                    gap_id=gap,
                    clr_id=clr_id(gap, _closing_note(item), resolved_at),
                    at=resolved_at,
                    actor=str(item.props.get("resolved_by") or MIGRATION_ACTOR),
                    event="resolved",
                    detail={"resolution": "answered", "alias": item.finding_id, "migrated": True},
                )
            )
        await self.records.log(self.project_id, events)


async def migrate_first_slice(
    source: Any,
    ontology: Ontology,
    project_id: str,
    records: GapRecords,
    *,
    database: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> MigrationResult:
    """Migrate the first-slice findings of one project (module docstring); a project without ``Finding`` nodes is
    left as it is. ``source`` is a ``GraphStore`` (or, deprecated, a driver with ``database``); ``ontology`` must hold
    the gap ontology file; ``clock`` gives UTC times for values a finding does not hold (tests inject one)."""

    migration = _Migration(
        source,
        ontology,
        validate_project_id(project_id),
        records,
        database=database,
        clock=clock or (lambda: datetime.now(UTC)),
    )
    return await migration.run()
