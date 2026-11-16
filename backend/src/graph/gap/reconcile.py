"""Reconcile: amendments against a new source revision (docs/graph/gap/reconcile.md; FR-GREC-01..08).

``reconcile_document`` decides, for every amendment of one lineage whose stored revision is not the lineage's newest,
which of the four questions of reconcile.md answers "yes" first, and returns the rows to store and the effective text:

1. **Anchor still there**: the anchor line is in the section with the same heading path (``applied``), or a line of
   that section is at least ``ReconcileSettings.anchor_similarity`` similar to it (``reanchored``: the row's anchor
   moves to that line).
2. **Answer already there**: the answer text (marker lines left out) is in that section, or anywhere in the document
   when the heading is gone (``absorbed``, the row ``retired``).
3. **Section says something else**: the section with the same heading path, else the section at least
   ``section_similarity`` similar to the old one, goes to the ``gap_judge`` same-meaning call (role ``gap_judge``,
   prompt ``RECONCILE_PROMPT``, cached in ``GapRecords`` by input hash): ``same`` (with at least
   ``judge_min_confidence``) is ``absorbed``; ``different`` is ``conflict`` (the row ``suspended``, anchored on the
   owner's line most like the answer, so a "Restore" option can name it); ``unrelated`` goes on to question 4. When
   the call fails, or no gateway is given (a dry run), the outcome is ``undecided``: the row keeps its status and
   anchor (``undecided``; a dry run stores nothing) and its text is applied after the section's last line for this
   read, so the build still reads the answer (FR-GREC-08); a row already ``undecided`` whose judge fails again
   becomes a ``conflict``.
4. **Section gone**: ``orphaned`` (the row ``orphaned``).

The old section of an amendment is found in ``previous_text`` (the text the last build read: the graph's active
fragments of the lineage) by the amendment's own first line, else its anchor line, else the row's ``heading_path``.
Normalisation: quote marks, case, whitespace, Markdown emphasis and list markers. Similarity: the Dice coefficient of
the content-word sets (stop words left out, light suffix stemming): a reworded question keeps its content words, a
changed rule adds many new ones.

``reconcile_corpus`` is the corpus reader's side: per lineage with ``active`` or ``undecided`` amendments it reconciles
the rows whose ``last_revision_id`` is not the newest revision (a lineage that left the corpus orphans every row),
stores the changed rows through ``Amendments`` and one ``reconciled`` event per outcome through ``GapRecords`` (not
in a dry run), and returns the rows to apply by lineage. Logs carry ids and counts only (NFR-GSEC-02).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from graph.core.errors import GraphError
from graph.core.llm.gateway import untrusted
from graph.gap.corpus import apply_amendments, effective_revision_id
from graph.gap.identity import normalise_quote, sha256_hex
from graph.gap.models import (
    AmendmentAnchor,
    AmendmentResult,
    AmendmentRow,
    EventRow,
    ReconcileResult,
    VerdictRow,
)
from graph.gap.names import AmendmentStatus, ReconcileOutcome
from graph.gap.ports import Amendments, GapRecords
from graph.gap.prompts import RECONCILE_PROMPT, prompt_text
from graph.gap.settings import ReconcileSettings
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger(__name__)

APPLIED_STATUSES: Final = ("active", "undecided")
"""Amendment statuses the corpus reader applies (an ``undecided`` one until the next reconcile, FR-GREC-08)."""
ALL_STATUSES: Final = get_args(AmendmentStatus)
RECONCILE_EVENT: Final = "reconciled"
RECONCILE_ACTOR: Final = "reconcile"
JUDGE_CALL_CLASS: Final = "judge"
JUDGE_MODEL: Final = "gap_judge"
VERDICT_FAILED: Final = "failed"
_MARKER: Final = re.compile(r"^>\s*clarified by gap::[0-9a-f]+$")
_HEADING: Final = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE: Final = re.compile(r"^\s{0,3}(```|~~~)")
_LIST: Final = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_EMPHASIS: Final = re.compile(r"[*_`~]+")
_WORD: Final = re.compile(r"[0-9a-z]+(?:'[a-z]+)?")
_SUFFIXES: Final = ("ing", "ed", "es", "s")
_STEM_MIN: Final = 3
_STOP_WORDS: Final = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "then",
        "than",
        "that",
        "this",
        "these",
        "those",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "done",
        "can",
        "could",
        "may",
        "might",
        "must",
        "shall",
        "should",
        "will",
        "would",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "for",
        "from",
        "with",
        "as",
        "into",
        "onto",
        "it",
        "its",
        "there",
        "their",
        "they",
        "them",
        "he",
        "she",
        "we",
        "you",
        "i",
        "our",
        "your",
        "his",
        "her",
        "not",
        "no",
        "so",
        "such",
        "very",
        "also",
        "only",
        "just",
        "any",
        "all",
        "each",
        "every",
        "some",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
    ]
)


# Text: normalisation, similarity and sections


def plain(text: str) -> str:
    """``text`` normalised for comparison (module docstring): no marker lines, list markers or emphasis, quote marks
    plain, lower case, single spaces."""

    lines = []
    for line in text.split("\n"):
        cleaned = normalise_quote(_EMPHASIS.sub("", _LIST.sub("", line))).casefold()
        if cleaned.startswith(">"):
            cleaned = cleaned if _MARKER.match(cleaned) else cleaned.lstrip("> ")
        if cleaned and not _MARKER.match(cleaned):
            lines.append(cleaned)
    return " ".join(lines)


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= _STEM_MIN and not word.endswith("ss"):
            word = word[: -len(suffix)]
            break
    if len(word) > _STEM_MIN and word[-1] == word[-2]:
        word = word[:-1]
    if len(word) > _STEM_MIN and word.endswith("e"):
        word = word[:-1]
    return word


def content_words(text: str) -> frozenset[str]:
    """The stemmed content words of ``text`` (stop words left out)."""

    return frozenset(_stem(word) for word in _WORD.findall(plain(text)) if word not in _STOP_WORDS)


_NEGATIONS: Final = frozenset({"not", "no", "never", "cannot", "can't", "don't", "doesn't", "won't", "isn't", "nor"})


def negated(text: str) -> bool:
    """Whether a line says no (a negation word or ``n't``)."""

    words = _WORD.findall(plain(text))
    return any(word in _NEGATIONS or word.endswith("n't") for word in words)


def same_kind(anchor: str, line: str) -> bool:
    """Whether ``line`` can stand for ``anchor`` when re-anchoring: both questions or both statements, and both
    negated or neither. A question the owner answered in the new upload, or a rule the owner turned round, is not a
    reworded anchor (GD3: the owner's text is never replaced without a question)."""

    asks = (plain(anchor).rstrip().endswith("?"), plain(line).rstrip().endswith("?"))
    return asks[0] == asks[1] and negated(anchor) == negated(line)


def similarity(first: str, second: str) -> float:
    """The Dice coefficient of the content-word sets of two texts (0 when either has none)."""

    left, right = content_words(first), content_words(second)
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


@dataclass(frozen=True)
class Section:
    """The lines directly under one heading path (sub-sections are their own sections)."""

    path: tuple[str, ...]
    """The normalised heading path (case-folded)."""
    headings: tuple[str, ...]
    """The heading texts as written."""
    lines: tuple[str, ...]
    """Body lines as written (blank lines left out)."""

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _key(heading: str) -> str:
    return normalise_quote(heading).casefold()


def sections(markdown: str) -> list[Section]:
    """The sections of ``markdown`` in order (code fences hold no heading)."""

    found: dict[tuple[str, ...], tuple[tuple[str, ...], list[str]]] = {}
    stack: list[tuple[int, str]] = []
    fenced = False
    for line in markdown.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
        heading = None if fenced else _HEADING.match(line)
        if heading is not None:
            level = len(heading.group(1))
            stack = [item for item in stack if item[0] < level]
            stack.append((level, heading.group(2)))
            path = tuple(_key(text) for _, text in stack)
            found.setdefault(path, (tuple(text for _, text in stack), []))
            continue
        if line.strip():
            path = tuple(_key(text) for _, text in stack)
            found.setdefault(path, (tuple(text for _, text in stack), []))[1].append(line)
    return [Section(path, headings, tuple(lines)) for path, (headings, lines) in found.items()]


def _row_lines(row: AmendmentRow) -> list[str]:
    return [line for line in row.text.split("\n") if plain(line)]


def answer_plain(row: AmendmentRow) -> str:
    """The answer text of an amendment, normalised, without its marker line."""

    return plain(row.text)


def _old_path(row: AmendmentRow, previous: Sequence[Section]) -> tuple[str, ...]:
    """The heading path the amendment's section had: where the previous text holds the amendment's first line (or,
    when it was not applied, its anchor line), else the row's ``heading_path``."""

    for wanted in (plain(next(iter(_row_lines(row)), "")), plain(row.anchor_text)):
        if not wanted:
            continue
        for section in previous:
            if any(plain(line) == wanted for line in section.lines):
                return section.path
    return tuple(_key(heading) for heading in row.heading_path)


def _find_section(path: tuple[str, ...], current: Sequence[Section]) -> Section | None:
    """The section with the same heading path, else the only one with the same last heading."""

    same = next((section for section in current if section.path == path), None)
    if same is not None or not path:
        return same
    last = [section for section in current if section.path and section.path[-1] == path[-1]]
    return last[0] if len(last) == 1 else None


# The judge


class AnswerDecision(BaseModel):
    """One decision of the answer and whether the owner's section states it (``stated``), leaves it out or only
    implies it (``missing``), or says otherwise (``contradicted``)."""

    model_config = ConfigDict(extra="forbid")

    decision: str
    in_section: Literal["stated", "missing", "contradicted"]


class SameMeaningAnswer(BaseModel):
    """The ``gap_judge`` answer of reconcile's question 3: the answer's decisions, each looked up in the section, then
    the verdict. ``decided`` holds code to the list: a ``same`` verdict with no decision listed, or with a decision the
    section does not state, is ``different`` (a partial adoption is a conflict, reconcile.md "The four questions")."""

    model_config = ConfigDict(extra="forbid")

    decisions: list[AnswerDecision] = Field(default_factory=list)
    verdict: Literal["same", "different", "unrelated"]
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""

    def decided(self) -> Literal["same", "different", "unrelated"]:
        """The verdict, ``different`` when it says ``same`` while a listed decision is missing or contradicted."""

        if self.verdict == "same" and (
            not self.decisions or any(item.in_section != "stated" for item in self.decisions)
        ):
            return "different"
        return self.verdict


def judge_messages(row: AmendmentRow, section: Section) -> list[dict[str, Any]]:
    """The same-meaning call: the answer, the line it replaced (or follows) and the owner's section; no names."""

    answer = "\n".join(line for line in _row_lines(row) if not _MARKER.match(plain(line)))
    blocks = [
        untrusted("answer", answer),
        untrusted("replaced line" if row.operation == "replace" else "line before the answer", row.anchor_text),
        untrusted("section", section.text),
    ]
    head = '{"question": "Does the section say what the answer says?"}'
    user = "\n\n".join(["Compare the answer with the section (JSON, then data blocks).", head, *blocks])
    return [{"role": "system", "content": prompt_text("reconcile")}, {"role": "user", "content": user}]


Judge = Callable[[AmendmentRow, Section], Awaitable[SameMeaningAnswer | None]]
"""Asks the same-meaning question; None when the call failed."""


def gateway_judge(gateway: Any, records: GapRecords | None, project_id: str, now: Callable[[], datetime]) -> Judge:
    """A ``Judge`` over the gateway, cached in ``GapRecords`` verdicts by the hash of its input (a failed call is not
    cached)."""

    async def judge(row: AmendmentRow, section: Section) -> SameMeaningAnswer | None:
        messages = judge_messages(row, section)
        digest = sha256_hex({"role": JUDGE_MODEL, "prompt": RECONCILE_PROMPT, "messages": messages})
        if records is not None:
            cached = await records.verdict(project_id, digest)
            if cached is not None:
                return SameMeaningAnswer.model_validate(cached.report)
        try:
            answer: SameMeaningAnswer = await gateway.structured(
                JUDGE_CALL_CLASS, None, messages, SameMeaningAnswer, prompt=RECONCILE_PROMPT
            )
        except GraphError as error:
            log_event(
                logger,
                WARNING,
                "graph.gap.reconcile.judge_failed",
                project_id=project_id,
                amendment_id=row.amendment_id,
                code=error.code,
            )
            return None
        if records is not None:
            await records.put_verdict(
                project_id,
                VerdictRow(
                    input_hash=digest,
                    report=answer.model_dump(mode="json"),
                    model=JUDGE_MODEL,
                    prompt_version=RECONCILE_PROMPT,
                    created_at=now(),
                ),
            )
        return answer

    return judge


# One document


def _anchored(row: AmendmentRow, section: Section, line: str, revision_id: str, now: datetime) -> AmendmentRow:
    text = normalise_quote(line)
    return row.model_copy(
        update={
            "heading_path": section.headings,
            "anchor_text": text,
            "anchor_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "last_revision_id": revision_id,
            "updated_at": now,
        }
    )


def _best_line(section: Section, text: str) -> tuple[str | None, float]:
    best: tuple[str | None, float] = (None, 0.0)
    for line in section.lines:
        if _HEADING.match(line):
            continue
        score = similarity(line, text)
        if score > best[1]:
            best = (line, score)
    return best


def _owner_line(section: Section, row: AmendmentRow) -> tuple[str | None, float]:
    """The owner's line that took the answer's place: the line most like the replaced (anchor) line, then most like
    the answer."""

    answer = "\n".join(_row_lines(row))
    scored = [
        (similarity(line, row.anchor_text), similarity(line, answer), -index, line)
        for index, line in enumerate(section.lines)
        if not _HEADING.match(line)
    ]
    if not scored:
        return None, 0.0
    best = max(scored)
    return best[3], best[0]


@dataclass
class _Document:
    revision_id: str
    current: list[Section]
    previous: list[Section]
    settings: ReconcileSettings
    judge: Judge | None
    now: datetime
    results: list[AmendmentResult] = field(default_factory=list)
    changed: list[AmendmentRow] = field(default_factory=list)
    applied: list[AmendmentRow] = field(default_factory=list)

    def outcome(self, row: AmendmentRow, outcome: ReconcileOutcome, stored: AmendmentRow | None, **detail: Any) -> None:
        self.results.append(
            AmendmentResult(amendment_id=row.amendment_id, clr_id=row.clr_id, outcome=outcome, **detail)
        )
        if stored is not None:
            self.changed.append(stored)

    def status(self, row: AmendmentRow, status: str) -> AmendmentRow:
        return row.model_copy(update={"status": status, "last_revision_id": self.revision_id, "updated_at": self.now})

    async def decide(self, row: AmendmentRow) -> None:
        path = _old_path(row, self.previous)
        same = _find_section(path, self.current)
        section = same or self._similar_section(path, row)
        if section is not None and await self._anchor(row, section):
            return
        answer = answer_plain(row)
        holder = same.text if same is not None else "\n".join(item.text for item in self.current)
        if answer and answer in plain(holder):
            self.outcome(row, "absorbed", self.status(row, "retired"), line=(same.headings[-1] if same else None))
            return
        if section is not None and await self._judged(row, section):
            return
        self.outcome(row, "orphaned", self.status(row, "orphaned"))

    async def _anchor(self, row: AmendmentRow, section: Section) -> bool:
        wanted = plain(row.anchor_text)
        exact = next((line for line in section.lines if plain(line) == wanted), None)
        if exact is not None:
            stored = self.status(row, "active").model_copy(update={"heading_path": section.headings})
            self.applied.append(stored)
            self.outcome(row, "applied", stored, line=normalise_quote(exact), similarity=1.0)
            return True
        line, score = _best_line(section, row.anchor_text)
        if line is None or score < self.settings.anchor_similarity or not same_kind(row.anchor_text, line):
            return False
        stored = _anchored(row, section, line, self.revision_id, self.now).model_copy(update={"status": "active"})
        self.applied.append(stored)
        anchor = AmendmentAnchor(
            heading_path=stored.heading_path, anchor_text=stored.anchor_text, anchor_hash=stored.anchor_hash
        )
        self.outcome(row, "reanchored", stored, anchor=anchor, line=stored.anchor_text, similarity=round(score, 3))
        return True

    def _similar_section(self, path: tuple[str, ...], row: AmendmentRow) -> Section | None:
        """The section most like the old one as its source said it (the amendment's text taken out, its anchor line
        back in), when at least ``section_similarity`` alike."""

        old = next((section for section in self.previous if section.path == path), None)
        if old is None:
            return None
        own = {plain(line) for line in _row_lines(row)}
        source = "\n".join([*(line for line in old.lines if plain(line) not in own), row.anchor_text])
        scored = [(similarity(source, section.text), index) for index, section in enumerate(self.current)]
        best = max(scored, default=(0.0, -1))
        return self.current[best[1]] if best[0] >= self.settings.section_similarity else None

    async def _judged(self, row: AmendmentRow, section: Section) -> bool:
        """Question 3; False when the judge finds the section unrelated (question 4 follows)."""

        answer = None if self.judge is None else await self.judge(row, section)
        if answer is None:
            failed = self.judge is not None
            if failed and row.status == "undecided":  # the second failure: a person decides
                self._conflict(row, section, verdict=VERDICT_FAILED)
                return True
            stored = self.status(row, "undecided").model_copy(update={"last_revision_id": row.last_revision_id})
            last = section.lines[-1] if section.lines else None
            if last is not None:
                self.applied.append(
                    row.model_copy(
                        update={
                            "status": "active",
                            "operation": "insert_after",
                            "heading_path": section.headings,
                            "anchor_text": normalise_quote(last),
                        }
                    )
                )
            self.outcome(
                row,
                "undecided",
                stored if failed else None,
                line=section.headings[-1] if section.headings else None,
                verdict=VERDICT_FAILED if failed else None,
            )
            return True
        verdict = answer.decided()
        if verdict == "same" and answer.confidence < self.settings.judge_min_confidence:
            verdict = "different"
        if verdict == "unrelated":
            return False
        detail = {"verdict": verdict, "confidence": answer.confidence}
        if verdict == "same":
            self.outcome(row, "absorbed", self.status(row, "retired"), line=section.headings[-1], **detail)
        else:
            self._conflict(row, section, **detail)
        return True

    def _conflict(self, row: AmendmentRow, section: Section, **detail: Any) -> None:
        line, score = _owner_line(section, row)
        owner = line if line is not None else (section.lines[0] if section.lines else row.anchor_text)
        stored = _anchored(row, section, owner, self.revision_id, self.now).model_copy(update={"status": "suspended"})
        anchor = AmendmentAnchor(
            heading_path=stored.heading_path, anchor_text=stored.anchor_text, anchor_hash=stored.anchor_hash
        )
        self.outcome(
            row, "conflict", stored, anchor=anchor, line=stored.anchor_text, similarity=round(score, 3), **detail
        )


@dataclass(frozen=True)
class LineageText:
    """The newest revision of one lineage as the corpus reader read it."""

    lineage: str
    revision_id: str
    text: str


def _old_position(row: AmendmentRow, previous_text: str | None) -> int:
    if not previous_text:
        return 0
    wanted = plain(next(iter(_row_lines(row)), "")) or plain(row.anchor_text)
    lines = previous_text.split("\n")
    return next((index for index, line in enumerate(lines) if wanted and plain(line) == wanted), len(lines))


async def reconcile_document(
    project_id: str,
    revision: LineageText,
    amendments: Sequence[AmendmentRow],
    *,
    previous_text: str | None = None,
    judge: Judge | None = None,
    settings: ReconcileSettings | None = None,
    now: datetime | None = None,
) -> ReconcileResult:
    """Every amendment's outcome against the newest ``revision`` of a lineage and the effective text (module
    docstring). The rows are taken as given (the caller passes the rows to reconcile); nothing is stored."""

    document = _Document(
        revision_id=revision.revision_id,
        current=sections(revision.text),
        previous=sections(previous_text or ""),
        settings=settings or ReconcileSettings(),
        judge=judge,
        now=now or datetime.now(UTC),
    )
    ordered = sorted(
        amendments,
        key=lambda row: (
            _old_position(row, previous_text),
            row.operation != "replace",
            row.created_at,
            row.amendment_id,
        ),
    )
    for row in ordered:
        await document.decide(row)
    text = apply_amendments(revision.text, document.applied).markdown
    follow_ups = sorted({row.clr_id for row in document.changed if row.status in {"suspended", "orphaned"}})
    log_event(
        logger,
        INFO,
        "graph.gap.reconcile.document",
        project_id=project_id,
        lineage=revision.lineage,
        outcomes=",".join(
            f"{name}:{count}" for name, count in sorted(Counter(item.outcome for item in document.results).items())
        ),
    )
    return ReconcileResult(
        amendments=tuple(document.results),
        text=text,
        changed=tuple(document.changed),
        applied=tuple(document.applied),
        follow_ups=tuple(follow_ups),
    )


# The corpus


@dataclass(frozen=True)
class CorpusReconcile:
    """What ``reconcile_corpus`` decided: the rows to apply by lineage and every outcome."""

    rows: Mapping[str, tuple[AmendmentRow, ...]]
    outcomes: tuple[AmendmentResult, ...] = ()


@dataclass(frozen=True)
class ReconcileInputs:
    """The ports and readers of ``reconcile_corpus``: the amendment and event stores, the judge (None: the same-meaning
    question is skipped), the previous text of a lineage and the gap of each clarification id."""

    amendments: Amendments
    records: GapRecords | None = None
    judge: Judge | None = None
    previous_text: Callable[[str], Awaitable[str | None]] | None = None
    gap_ids: Callable[[], Awaitable[Mapping[str, str]]] | None = None
    settings: ReconcileSettings = field(default_factory=ReconcileSettings)
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


def _event(
    result: AmendmentResult, row: AmendmentRow, gap_of: Mapping[str, str], at: datetime, **detail: Any
) -> EventRow:
    return EventRow(
        gap_id=gap_of.get(row.clr_id),
        clr_id=row.clr_id,
        at=at,
        actor=RECONCILE_ACTOR,
        event=RECONCILE_EVENT,
        detail={
            "amendment_id": row.amendment_id,
            "outcome": result.outcome,
            "lineage": row.lineage,
            "previous_revision_id": row.last_revision_id,
            "old_anchor": row.anchor_text,
            "new_anchor": None if result.anchor is None else result.anchor.anchor_text,
            "line": result.line,
            "similarity": result.similarity,
            "verdict": result.verdict,
            "confidence": result.confidence,
            **detail,
        },
    )


def _stale(stored: Sequence[AmendmentRow], revision: LineageText) -> tuple[list[AmendmentRow], list[AmendmentRow]]:
    """The rows to reconcile, and the rows that are current although their ``last_revision_id`` is not the source
    revision: an answer stores the revision id the graph's document has, which is the effective id when the lineage
    already had answers. Such a row is current when that id is the effective id of this source revision with the
    lineage's current rows applied (the source has not changed since); it gets the source id, with no outcome."""

    current = [row for row in stored if row.last_revision_id == revision.revision_id and row.status == "active"]
    previous = effective_revision_id(revision.revision_id, apply_amendments(revision.text, current).applied)
    stale: list[AmendmentRow] = []
    fresh: list[AmendmentRow] = []
    for row in stored:
        if row in current:
            continue
        if row.status == "active" and row.last_revision_id == previous:
            fresh.append(row.model_copy(update={"last_revision_id": revision.revision_id}))
        else:
            stale.append(row)
    return stale, fresh


async def _unchanged_since(
    ports: Amendments, project_id: str, changed: Sequence[AmendmentRow], read: Mapping[str, AmendmentRow]
) -> list[AmendmentRow]:
    """The changed rows whose stored row is still as reconcile read it: an answer, an apply or a revert that wrote a
    row meanwhile (they do not take the corpus reader's turn) wins, and reconcile meets that row again next read."""

    lineages = sorted({row.lineage for row in changed})
    now = {
        row.amendment_id: row
        for lineage in lineages
        for row in await ports.for_lineage(project_id, lineage, statuses=ALL_STATUSES)
    }
    kept = []
    for row in changed:
        before, stored = read.get(row.amendment_id), now.get(row.amendment_id)
        if (
            before is not None
            and stored is not None
            and (stored.status, stored.updated_at)
            == (
                before.status,
                before.updated_at,
            )
        ):
            kept.append(row)
    return kept


async def reconcile_corpus(
    project_id: str, revisions: Sequence[LineageText], inputs: ReconcileInputs, *, dry_run: bool = False
) -> CorpusReconcile:
    """Reconcile every lineage with applied amendments against ``revisions`` (module docstring)."""

    ports = inputs.amendments
    by_lineage = {revision.lineage: revision for revision in revisions}
    rows: dict[str, tuple[AmendmentRow, ...]] = {}
    read: dict[str, AmendmentRow] = {}
    outcomes: list[AmendmentResult] = []
    changed: list[AmendmentRow] = []
    events: list[tuple[AmendmentResult, AmendmentRow, str]] = []
    for lineage in await ports.pending_lineages(project_id):
        stored = tuple(await ports.for_lineage(project_id, lineage, statuses=APPLIED_STATUSES))
        read |= {row.amendment_id: row for row in stored}
        revision = by_lineage.get(lineage)
        if revision is None:  # the document left the corpus: every amendment loses its place
            at = inputs.now()
            for row in stored:
                lost = AmendmentResult(amendment_id=row.amendment_id, clr_id=row.clr_id, outcome="orphaned")
                outcomes.append(lost)
                changed.append(row.model_copy(update={"status": "orphaned", "updated_at": at}))
                events.append((lost, row, ""))
            continue
        stale, fresh = _stale(stored, revision)
        changed += fresh  # answered on the text the last build read: current, only its revision id is updated
        kept = tuple(row for row in stored if row not in stale)
        if not stale:
            rows[lineage] = kept
            continue
        previous = None if inputs.previous_text is None else await inputs.previous_text(lineage)
        result = await reconcile_document(
            project_id,
            revision,
            stale,
            previous_text=previous,
            judge=inputs.judge,
            settings=inputs.settings,
            now=inputs.now(),
        )
        rows[lineage] = (*kept, *result.applied)
        changed += result.changed
        originals = {row.amendment_id: row for row in stale}
        for item in result.amendments:
            outcomes.append(item)
            events.append((item, originals[item.amendment_id], revision.revision_id))
    if not dry_run and changed:
        await ports.put(project_id, await _unchanged_since(ports, project_id, changed, read))
        if inputs.records is not None:
            gap_of = {} if inputs.gap_ids is None else await inputs.gap_ids()
            at = inputs.now()
            await inputs.records.log(
                project_id, [_event(item, row, gap_of, at, revision_id=rev or None) for item, row, rev in events]
            )
    log_event(
        logger,
        INFO,
        "graph.gap.reconcile.corpus",
        project_id=project_id,
        dry_run=dry_run,
        lineages=len(rows),
        outcomes=len(outcomes),
        changed=len(changed),
    )
    return CorpusReconcile(rows=rows, outcomes=tuple(outcomes))
